"""Plane C — Pine alert ingestion.

TradingView alerts POST here when your script fires. This is the only
TradingView-sanctioned path from a chart to an action, and it is also the only
part of Shani exposed to the open internet.

## Security posture

The endpoint must be publicly reachable for TradingView's servers to POST to it,
which means anyone who learns the URL can POST to it too. Two consequences shape
this module:

**Every payload is HMAC-verified.** Unsigned or badly-signed requests are
rejected before parsing. The comparison uses :func:`hmac.compare_digest`, since a
plain ``==`` on a signature leaks its prefix through timing. TradingView cannot
compute an HMAC itself, so the shared secret travels *inside* the alert message
body — which is why the secret must be one you generated for this purpose and
used nowhere else.

**The payload is untrusted, and it flows toward a model that proposes trades.**
A webhook body is attacker-controlled text heading for an LLM prompt. It is
stored verbatim for the audit trail but is fenced as data before it reaches any
prompt (see :mod:`shani.agent.propose`), and nothing in it may select an
instrument Shani does not already know, or a side other than buy or sell.

A signal arriving here never places an order by itself. It creates a
:class:`~shani.models.Signal`, which the agent may turn into a proposal, which
the risk gate may approve, which the trader confirms. Four gates, and the
internet controls only the first.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from shani.audit import AuditLog, EventType
from shani.db import Database
from shani.instruments import UnknownInstrumentError, get_instrument
from shani.models import Side, Signal, SignalSource

__all__ = ["WebhookRejected", "expected_signature", "parse_alert", "verify_signature"]

#: Refuse anything larger. A Pine alert is a few hundred bytes; a megabyte of
#: JSON is either a misconfiguration or someone probing.
MAX_PAYLOAD_BYTES = 16 * 1024


class WebhookRejected(ValueError):
    """The payload was refused. The reason is deliberately vague to the caller."""


def expected_signature(secret: str, body: bytes) -> str:
    """HMAC-SHA256 of the raw body, hex encoded."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, provided: str | None) -> bool:
    """Constant-time signature check.

    Returns ``False`` for a missing secret rather than passing: an unconfigured
    secret must fail closed, or enabling the webhook before setting one would
    silently accept anything from anyone.
    """
    if not secret or not provided:
        return False
    return hmac.compare_digest(expected_signature(secret, body), provided.strip())


def parse_alert(body: bytes, *, secret: str, signature: str | None = None) -> Signal:
    """Validate, verify, and parse a TradingView alert into a Signal.

    The signature may arrive either as an ``X-Signature`` header or inside the
    JSON as a ``secret`` field, because TradingView's alert UI cannot set custom
    headers on every plan. Both are compared in constant time.
    """
    if len(body) > MAX_PAYLOAD_BYTES:
        raise WebhookRejected("Payload too large")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WebhookRejected(f"Body is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WebhookRejected("Body must be a JSON object")

    # Accept the signature from the header, or from a `secret` field in the
    # body. The body form compares the shared secret directly, since TradingView
    # cannot compute an HMAC over its own message.
    if signature is not None:
        if not verify_signature(secret, body, signature):
            raise WebhookRejected("Invalid signature")
    else:
        supplied = str(payload.get("secret", ""))
        if not secret or not hmac.compare_digest(secret, supplied):
            raise WebhookRejected("Invalid or missing secret")

    symbol_raw = str(payload.get("symbol") or payload.get("ticker") or "").strip()
    if not symbol_raw:
        raise WebhookRejected("Alert has no symbol")

    # Resolve against the known instrument table. An unknown symbol is refused
    # rather than passed through, so a malformed or hostile alert cannot cause
    # Shani to reason about an instrument it has no contract spec for.
    try:
        instrument = get_instrument(symbol_raw)
    except UnknownInstrumentError as exc:
        raise WebhookRejected(f"Unknown instrument: {symbol_raw}") from exc

    side: Side | None = None
    action = str(payload.get("action") or payload.get("side") or "").strip().lower()
    if action in {"buy", "long"}:
        side = Side.BUY
    elif action in {"sell", "short"}:
        side = Side.SELL
    elif action:
        raise WebhookRejected(f"Unrecognised action: {action!r}")

    price = _decimal(payload.get("price") or payload.get("close"))
    if price is not None and price <= 0:
        raise WebhookRejected("Price must be positive")

    return Signal(
        source=SignalSource.PINE_WEBHOOK,
        symbol=instrument.root,
        side=side,
        price=price,
        timeframe=_clean(payload.get("interval") or payload.get("timeframe"), 16),
        strategy_name=_clean(payload.get("strategy") or payload.get("name"), 128),
        message=_clean(payload.get("message") or payload.get("comment"), 2000) or "",
        # Stored verbatim for the audit trail. Treated as untrusted data
        # everywhere downstream, and fenced before reaching any prompt.
        raw_payload={k: v for k, v in payload.items() if k != "secret"},
    )


def ingest(
    body: bytes,
    *,
    secret: str,
    db: Database,
    audit: AuditLog,
    signature: str | None = None,
) -> Signal:
    """Parse, persist, and log an incoming alert."""
    try:
        signal = parse_alert(body, secret=secret, signature=signature)
    except WebhookRejected as exc:
        audit.warn(
            EventType.SIGNAL_REJECTED,
            f"Rejected webhook: {exc}",
            payload={"bytes": len(body)},
        )
        raise

    db.signals.save(signal)
    audit.record(
        EventType.SIGNAL_RECEIVED,
        f"{signal.strategy_name or 'Alert'} on {signal.symbol}"
        + (f" ({signal.side.value})" if signal.side else ""),
        payload={"symbol": signal.symbol, "timeframe": signal.timeframe},
        signal_id=signal.id,
    )
    return signal


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _clean(value: Any, limit: int) -> str | None:
    """Coerce to a bounded string. Length caps keep a hostile alert from
    smuggling a wall of text into a prompt or the portal."""
    if value is None:
        return None
    return str(value)[:limit]
