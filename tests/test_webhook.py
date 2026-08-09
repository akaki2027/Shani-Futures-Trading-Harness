"""Plane C webhook tests.

This endpoint faces the open internet, so these tests are security tests as much
as parsing tests. The properties that matter: unsigned payloads never parse, an
unconfigured secret fails closed, and nothing in an attacker-controlled body can
name an instrument Shani has no contract spec for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shani.audit import AuditLog, EventType
from shani.db import Database
from shani.ingest.webhook import (
    WebhookRejected,
    expected_signature,
    ingest,
    parse_alert,
    verify_signature,
)
from shani.models import Side, SignalSource

SECRET = "test-secret-do-not-use"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "wh.db")
    yield database
    database.close()


@pytest.fixture
def audit(db: Database) -> AuditLog:
    return AuditLog(db)


def body(**fields: object) -> bytes:
    return json.dumps(fields).encode()


def signed(**fields: object) -> bytes:
    return body(secret=SECRET, **fields)


class TestSignatureVerification:
    def test_valid_header_signature_passes(self) -> None:
        payload = body(symbol="ES", action="buy")
        assert verify_signature(SECRET, payload, expected_signature(SECRET, payload))

    def test_wrong_signature_fails(self) -> None:
        assert not verify_signature(SECRET, body(symbol="ES"), "deadbeef")

    def test_missing_signature_fails(self) -> None:
        assert not verify_signature(SECRET, body(symbol="ES"), None)

    def test_unconfigured_secret_fails_closed(self) -> None:
        """Enabling the webhook before setting a secret must not accept anything."""
        payload = body(symbol="ES")
        assert not verify_signature("", payload, expected_signature("", payload))

    def test_signature_covers_the_body(self) -> None:
        """A signature for one payload must not validate another."""
        original = body(symbol="ES", action="buy")
        tampered = body(symbol="ES", action="sell")
        assert not verify_signature(SECRET, tampered, expected_signature(SECRET, original))


class TestRejection:
    def test_unsigned_payload_is_rejected(self) -> None:
        with pytest.raises(WebhookRejected, match="Invalid or missing secret"):
            parse_alert(body(symbol="ES", action="buy"), secret=SECRET)

    def test_wrong_secret_in_body_is_rejected(self) -> None:
        with pytest.raises(WebhookRejected):
            parse_alert(body(secret="wrong", symbol="ES"), secret=SECRET)

    def test_bad_header_signature_is_rejected(self) -> None:
        with pytest.raises(WebhookRejected, match="Invalid signature"):
            parse_alert(body(symbol="ES"), secret=SECRET, signature="deadbeef")

    def test_malformed_json_is_rejected(self) -> None:
        with pytest.raises(WebhookRejected, match="not valid JSON"):
            parse_alert(b"{not json", secret=SECRET)

    def test_non_object_json_is_rejected(self) -> None:
        with pytest.raises(WebhookRejected, match="JSON object"):
            parse_alert(b"[1,2,3]", secret=SECRET)

    def test_oversized_payload_is_rejected_before_parsing(self) -> None:
        with pytest.raises(WebhookRejected, match="too large"):
            parse_alert(b"x" * 20_000, secret=SECRET)

    def test_missing_symbol_is_rejected(self) -> None:
        with pytest.raises(WebhookRejected, match="no symbol"):
            parse_alert(signed(action="buy"), secret=SECRET)

    def test_unknown_instrument_is_rejected(self) -> None:
        """A hostile alert must not make Shani reason about an unknown contract."""
        with pytest.raises(WebhookRejected, match="Unknown instrument"):
            parse_alert(signed(symbol="TSLA", action="buy"), secret=SECRET)

    def test_unrecognised_action_is_rejected(self) -> None:
        with pytest.raises(WebhookRejected, match="Unrecognised action"):
            parse_alert(signed(symbol="ES", action="liquidate"), secret=SECRET)

    def test_negative_price_is_rejected(self) -> None:
        with pytest.raises(WebhookRejected, match="must be positive"):
            parse_alert(signed(symbol="ES", price=-5), secret=SECRET)


class TestParsing:
    def test_parses_a_typical_alert(self) -> None:
        signal = parse_alert(
            signed(symbol="CME:ES1!", action="buy", price="5000.25",
                   interval="15", strategy="ORB", message="Opening range break"),
            secret=SECRET,
        )
        assert signal.symbol == "ES"          # normalised to the root
        assert signal.side is Side.BUY
        assert str(signal.price) == "5000.25"
        assert signal.strategy_name == "ORB"
        assert signal.source is SignalSource.PINE_WEBHOOK

    @pytest.mark.parametrize(
        ("action", "expected"),
        [("buy", Side.BUY), ("long", Side.BUY), ("sell", Side.SELL),
         ("short", Side.SELL), ("BUY", Side.BUY)],
    )
    def test_action_synonyms(self, action: str, expected: Side) -> None:
        signal = parse_alert(signed(symbol="ES", action=action), secret=SECRET)
        assert signal.side is expected

    def test_informational_alert_without_a_side(self) -> None:
        signal = parse_alert(signed(symbol="ES", message="Volume spike"), secret=SECRET)
        assert signal.side is None

    def test_accepts_ticker_as_a_symbol_alias(self) -> None:
        """TradingView's own placeholder is {{ticker}}."""
        assert parse_alert(signed(ticker="NQ", action="buy"), secret=SECRET).symbol == "NQ"

    def test_header_signature_path_works(self) -> None:
        payload = body(symbol="GC", action="sell")
        signal = parse_alert(
            payload, secret=SECRET, signature=expected_signature(SECRET, payload)
        )
        assert signal.symbol == "GC"

    def test_secret_is_stripped_from_the_stored_payload(self) -> None:
        """The audit trail must not become a place the secret is written down."""
        signal = parse_alert(signed(symbol="ES", action="buy"), secret=SECRET)
        assert "secret" not in signal.raw_payload

    def test_raw_payload_is_preserved_for_audit(self) -> None:
        signal = parse_alert(
            signed(symbol="ES", action="buy", custom_field="strategy detail"),
            secret=SECRET,
        )
        assert signal.raw_payload["custom_field"] == "strategy detail"

    def test_long_message_is_truncated(self) -> None:
        """Caps stop a hostile alert smuggling a wall of text into a prompt."""
        signal = parse_alert(signed(symbol="ES", message="x" * 9000), secret=SECRET)
        assert len(signal.message) == 2000


class TestIngestion:
    def test_accepted_signal_is_persisted_and_logged(
        self, db: Database, audit: AuditLog
    ) -> None:
        signal = ingest(signed(symbol="ES", action="buy"), secret=SECRET, db=db, audit=audit)
        assert db.signals.get(signal.id) is not None
        assert any(e.event_type == EventType.SIGNAL_RECEIVED for e in audit.recent())

    def test_rejected_signal_is_logged_but_not_persisted(
        self, db: Database, audit: AuditLog
    ) -> None:
        with pytest.raises(WebhookRejected):
            ingest(body(symbol="ES"), secret=SECRET, db=db, audit=audit)
        assert db.signals.all() == []
        assert any(e.event_type == EventType.SIGNAL_REJECTED for e in audit.recent())

    def test_a_signal_does_not_place_an_order(self, db: Database, audit: AuditLog) -> None:
        """The internet controls the first gate only. Nothing executes here."""
        ingest(signed(symbol="ES", action="buy"), secret=SECRET, db=db, audit=audit)
        assert db.orders.all() == []
        assert db.trades.all() == []
