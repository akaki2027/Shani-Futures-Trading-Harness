"""FastAPI application.

**API-first, strictly.** The portal contains no business logic — it is a client
of this API and nothing else. That is what makes the planned mobile companion a
second client rather than a rewrite, and it is why ``/changes`` exists before
anything needs it.

**Loopback by default.** The server binds ``127.0.0.1``. Your journal is your
edge written down and your order entry is on the same port; neither belongs on
the local network by accident. The only endpoint intended for public exposure is
the webhook, which is HMAC-verified and meant to be reached through a tunnel
that forwards *that path alone*.

**Token auth from day one**, even though this release is single-user. Pairing a
phone later should be issuing a token, not inventing an authentication system.
The webhook is exempt — TradingView cannot send a bearer token, which is exactly
why it carries an HMAC instead.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from shani.agent.catalogue import ModelCatalogue, ModelCatalogueError
from shani.agent.llm import build_llm
from shani.agent.reasoning import Agent
from shani.audit import AuditLog, EventType
from shani.brokers.registry import build_registry
from shani.config import Config, load_config
from shani.db import Database
from shani.ingest.webhook import WebhookRejected, ingest
from shani.instruments import INSTRUMENTS, get_instrument
from shani.market.bars import BarsProvider, BarsUnavailableError
from shani.market.screener import ScreenerProvider, ScreenerUnavailableError
from shani.memory.playbook import Playbook
from shani.memory.stats import compute_stats, daily_pnl, equity_curve, evaluate_playbook
from shani.models import Order, OrderType, Side
from shani.news.service import NewsService
from shani.notify import Notifier
from shani.risk.policy import RiskPolicy
from shani.settings_store import (
    PROVIDER_KEYS,
    read_model_env,
    write_config_values,
    write_env_values,
)

__all__ = ["build_app"]


class OrderRequest(BaseModel):
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None


class AnswerRequest(BaseModel):
    index: int
    answer: str


class PriceRequest(BaseModel):
    symbol: str
    price: Decimal


class ConnectorRequest(BaseModel):
    #: Env-var name to value. Write-only, stored in .env and never echoed back.
    values: dict[str, str]


class ModelSettingsRequest(BaseModel):
    provider: Literal["anthropic", "openai", "openrouter", "ollama", "none"]
    triage_model: str = ""
    reasoning_model: str = ""
    base_url: str | None = None
    #: Write-only. Persisted to .env and never echoed back by any endpoint.
    api_key: str | None = None


def build_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    cfg.ensure_dirs()

    db = Database(cfg.db_path)
    audit = AuditLog(db)
    registry = build_registry(cfg, db, audit)
    broker = registry.get(cfg.broker.default)
    policy = RiskPolicy(config=cfg.risk, db=db, audit=audit)
    playbook = Playbook(db)
    screener = ScreenerProvider(cache_seconds=cfg.tradingview.screener_cache_seconds)
    bars_provider = BarsProvider()
    model_catalogue = ModelCatalogue()
    news_service = NewsService()
    agent = Agent(db, build_llm(cfg.model), audit)
    notifier = Notifier()

    app = FastAPI(
        title="Shani",
        description="A trading harness that learns how you trade.",
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.server.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        """Bearer auth. Skipped entirely when no token is configured.

        A blank token means single-user loopback, which is the default and is
        fine; the danger is a *non-blank* token compared with ``==``, so the
        comparison is constant-time.
        """
        if not cfg.server.api_token:
            return
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(supplied, cfg.server.api_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API token")

    auth = [Depends(require_token)]

    # ── health ───────────────────────────────────────────────────────────────

    # Exposed at both paths on purpose. ``/health`` is the conventional location
    # for a container or uptime probe; ``/api/health`` lets browser clients reach
    # it through the same single proxy prefix as everything else, rather than
    # needing a second rewrite rule or a relative-path trick that the browser
    # will normalise out from under them.
    @app.get("/health")
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "broker": broker.name,
            "live_enabled": cfg.broker.live_enabled,
            "model": cfg.model.provider if cfg.model.enabled else "disabled",
        }

    # ── model settings ───────────────────────────────────────────────────────

    @app.get("/api/settings/model", dependencies=auth)
    def get_model_settings() -> dict[str, Any]:
        """Current model configuration. The API key is masked, never returned."""
        return read_model_env(load_config())

    @app.get("/api/settings/models", dependencies=auth)
    def list_models(provider: str = "openrouter", refresh: bool = False) -> dict[str, Any]:
        """Live model catalogue for the provider.

        Fetched rather than hardcoded: OpenRouter alone lists several hundred
        models and the set changes weekly, so a baked-in list is stale before it
        ships. Pricing comes back with it so the cost of a choice is visible at
        the point the choice is made — the triage tier runs on every signal, and
        picking an expensive model there is an easy and costly mistake.
        """
        if provider != "openrouter":
            return {"provider": provider, "models": [], "error": None}
        try:
            models = model_catalogue.fetch(refresh=refresh)
        except ModelCatalogueError as exc:
            return {"provider": provider, "models": [], "error": str(exc)}
        return {"provider": provider, "models": models, "error": None}

    @app.post("/api/settings/model", dependencies=auth)
    def update_model_settings(body: ModelSettingsRequest) -> dict[str, Any]:
        """Persist model settings. Secrets to .env, everything else to config.yaml."""
        non_secret: dict[str, object] = {"provider": body.provider}
        if body.triage_model:
            non_secret["triage_model"] = body.triage_model
        if body.reasoning_model:
            non_secret["reasoning_model"] = body.reasoning_model
        if body.base_url is not None:
            non_secret["base_url"] = body.base_url or None
        write_config_values("model", non_secret)

        if body.api_key:
            env_var = PROVIDER_KEYS.get(body.provider, "")
            if not env_var:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"{body.provider} takes no API key.",
                )
            write_env_values({env_var: body.api_key.strip()})
            # The running process was started before the key existed, so make it
            # visible now rather than requiring a restart to use what was just
            # saved.
            os.environ[env_var] = body.api_key.strip()

        model_catalogue.invalidate()
        audit.record(
            EventType.CONFIG_CHANGED,
            f"Model settings updated — {body.provider}",
            payload={
                "provider": body.provider,
                "triage_model": body.triage_model,
                "reasoning_model": body.reasoning_model,
                "key_changed": bool(body.api_key),
            },
        )
        return read_model_env(load_config())

    @app.post("/api/settings/model/test", dependencies=auth)
    def test_model_settings() -> dict[str, Any]:
        """Round-trip a real completion so 'saved' and 'working' are distinguishable."""
        healthy, detail = build_llm(load_config().model).check()
        return {"ok": healthy, "detail": detail}

    # ── news ─────────────────────────────────────────────────────────────────

    @app.get("/api/news", dependencies=auth)
    def news(refresh: bool = False, limit: int = 40) -> dict[str, Any]:
        """Aggregated headlines with a directional read on each.

        Partial failure is reported per connector rather than hidden. A feed
        that quietly shows less than the trader believes it is showing is worse
        than one that says "Reddit is down".
        """
        return news_service.fetch(
            cfg.tradingview.watchlist,
            build_llm(load_config().model),
            limit=limit,
            refresh=refresh,
        )

    @app.get("/api/news/connectors", dependencies=auth)
    def news_connectors() -> list[dict[str, Any]]:
        return news_service.connectors()

    @app.post("/api/news/connectors/{connector_id}", dependencies=auth)
    def configure_connector(connector_id: str, body: ConnectorRequest) -> dict[str, Any]:
        """Store a connector credential. Write-only, same as the model key."""
        known = {c["id"]: c for c in news_service.connectors()}
        if connector_id not in known:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No connector {connector_id!r}")

        connector = known[connector_id]
        if not connector["requires_key"]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{connector['name']} needs no credentials."
            )

        updates = {k: v.strip() for k, v in body.values.items() if v and v.strip()}
        if not updates:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No values supplied.")

        write_env_values(updates)
        for key, value in updates.items():
            os.environ[key] = value

        news_service.invalidate()
        audit.record(
            EventType.CONFIG_CHANGED,
            f"News connector configured — {connector['name']}",
            payload={"connector": connector_id, "keys": sorted(updates)},
        )
        return {"connectors": news_service.connectors()}

    # ── market data (Plane A) ────────────────────────────────────────────────

    @app.get("/api/instruments", dependencies=auth)
    def instruments() -> list[dict[str, Any]]:
        return [
            {
                "root": i.root, "name": i.name, "exchange": i.exchange,
                "tv_symbol": i.tv_symbol, "tick_size": str(i.tick_size),
                "tick_value": str(i.tick_value), "multiplier": str(i.multiplier),
                "asset_class": i.asset_class.value, "is_micro": i.is_micro,
            }
            for i in INSTRUMENTS.values()
        ]

    @app.get("/api/watchlist", dependencies=auth)
    def watchlist() -> dict[str, Any]:
        """Quotes for the configured watchlist.

        Returns an ``error`` field rather than a 5xx when upstream is
        unavailable, so the portal can render the watchlist with a staleness
        notice instead of an error page. A trader losing their whole dashboard
        because a quote endpoint rate-limited is a worse outcome.
        """
        try:
            quotes = screener.quotes(cfg.tradingview.watchlist)
        except ScreenerUnavailableError as exc:
            return {"quotes": [], "error": str(exc)}
        return {
            "quotes": [
                {
                    "symbol": q.symbol, "name": q.name, "tv_symbol": q.tv_symbol,
                    "last": str(q.last) if q.last is not None else None,
                    "change": str(q.change) if q.change is not None else None,
                    "change_percent": q.change_percent,
                    "high": str(q.high) if q.high is not None else None,
                    "low": str(q.low) if q.low is not None else None,
                    "volume": q.volume, "as_of": q.as_of,
                }
                for q in quotes
            ],
            "error": None,
        }

    @app.get("/api/bars/{symbol}", dependencies=auth)
    def bars(symbol: str, interval: str = "15m") -> dict[str, Any]:
        """OHLCV candles for charting.

        Deliberately *not* sourced from Plane B: drawing an arbitrary symbol
        there would mean changing the chart the trader is working from, and the
        portal must never reach over and move it.
        """
        try:
            candles = bars_provider.bars(symbol, interval)
        except BarsUnavailableError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

        instrument = get_instrument(symbol)
        return {
            "symbol": instrument.root,
            "name": instrument.name,
            "interval": interval,
            "tick_size": str(instrument.tick_size),
            "bars": [
                {
                    "time": b.time, "open": b.open, "high": b.high,
                    "low": b.low, "close": b.close, "volume": b.volume,
                }
                for b in candles
            ],
        }

    @app.get("/api/analysis/{symbol}", dependencies=auth)
    def analysis(symbol: str, interval: str = "15m") -> dict[str, Any]:
        try:
            return screener.analysis(symbol, interval)
        except ScreenerUnavailableError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    # ── account and positions ────────────────────────────────────────────────

    @app.get("/api/account", dependencies=auth)
    def account() -> dict[str, Any]:
        snapshot = broker.account()
        return {
            "balance": str(snapshot.balance),
            "equity": str(snapshot.equity),
            "realized_pnl": str(snapshot.realized_pnl),
            "unrealized_pnl": str(snapshot.unrealized_pnl),
            "commission_paid": str(snapshot.commission_paid),
            "open_positions": snapshot.open_positions,
            "broker": broker.name,
            "is_live": broker.is_live,
            "realized_today": str(policy.realized_pnl_today()),
            "remaining_daily_loss": str(policy.remaining_daily_loss()),
        }

    @app.get("/api/positions", dependencies=auth)
    def positions() -> list[dict[str, Any]]:
        return [
            {
                "symbol": p.symbol, "quantity": p.quantity,
                "average_price": str(p.average_price),
                "realized_pnl": str(p.realized_pnl),
                "mae": str(p.max_adverse_excursion),
                "mfe": str(p.max_favorable_excursion),
            }
            for p in broker.positions()
        ]

    @app.get("/api/orders", dependencies=auth)
    def orders() -> list[dict[str, Any]]:
        return [_order_json(o) for o in db.orders.all(limit=100)]

    # ── trading ──────────────────────────────────────────────────────────────

    @app.post("/api/price", dependencies=auth)
    def push_price(body: PriceRequest) -> dict[str, Any]:
        """Feed the simulator a price.

        The paper broker deliberately owns no clock and no feed, which is what
        makes it deterministic and testable. The portal pushes prices here.
        """
        fills = broker.on_price(body.symbol, body.price, datetime.now(UTC))
        return {"fills": len(fills)}

    @app.post("/api/orders", dependencies=auth, status_code=status.HTTP_201_CREATED)
    async def submit_order(body: OrderRequest) -> dict[str, Any]:
        """Place an order — through the risk gate, always.

        There is no path to a broker that skips this.
        """
        instrument = get_instrument(body.symbol)
        now = datetime.now(UTC)

        planned_risk = None
        if body.stop_loss is not None:
            reference = body.limit_price or broker.last_price(body.symbol)
            if reference is not None:
                planned_risk = abs(
                    instrument.pnl(reference, body.stop_loss, body.quantity,
                                   is_long=body.side is Side.BUY)
                )

        probe = Order(
            symbol=body.symbol, side=body.side, quantity=body.quantity,
            order_type=body.order_type, limit_price=body.limit_price,
        )
        decision = policy.evaluate(
            probe, broker.account(), at=now,
            planned_risk=planned_risk, has_stop=body.stop_loss is not None,
        )
        if not decision.approved:
            await notifier.risk_limit(decision.rule or "risk", decision.message)
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {"rule": decision.rule, "reasons": list(decision.reasons)},
            )

        try:
            if body.stop_loss is not None and body.take_profit is not None:
                entry, _, _ = broker.submit_bracket(
                    symbol=body.symbol, side=body.side, quantity=body.quantity,
                    stop_loss=body.stop_loss, take_profit=body.take_profit,
                    at=now, entry_type=body.order_type, entry_price=body.limit_price,
                )
            else:
                entry = broker.submit(probe, at=now)
        except Exception as exc:
            audit.warn(EventType.ORDER_REJECTED, f"Broker rejected order: {exc}")
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

        audit.record(
            EventType.ORDER_SUBMITTED,
            f"{body.side.value} {body.quantity} {body.symbol}",
            order_id=entry.id,
        )
        return _order_json(entry)

    @app.delete("/api/orders/{order_id}", dependencies=auth)
    def cancel_order(order_id: UUID) -> dict[str, Any]:
        try:
            return _order_json(broker.cancel(order_id))
        except Exception as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    # ── journal ──────────────────────────────────────────────────────────────

    @app.get("/api/trades", dependencies=auth)
    def trades(limit: int = 100) -> list[dict[str, Any]]:
        return [_trade_json(t) for t in db.trades.all(limit=limit)]

    @app.get("/api/trades/{trade_id}", dependencies=auth)
    def trade_detail(trade_id: UUID) -> dict[str, Any]:
        trade = db.trades.get(trade_id)
        if trade is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such trade")
        return _trade_json(trade, full=True)

    @app.post("/api/trades/{trade_id}/interview", dependencies=auth)
    def answer_interview(trade_id: UUID, body: AnswerRequest) -> dict[str, Any]:
        trade = db.trades.get(trade_id)
        if trade is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such trade")
        if not trade.interview:
            agent.start_interview(trade)
        try:
            agent.record_answer(trade, body.index, body.answer)
        except IndexError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return _trade_json(trade, full=True)

    @app.post("/api/trades/{trade_id}/extract", dependencies=auth)
    def extract(trade_id: UUID) -> dict[str, Any]:
        """Turn an answered interview into a setup card."""
        trade = db.trades.get(trade_id)
        if trade is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such trade")
        card = agent.extract_setup(trade)
        if card is None:
            return {
                "card": None,
                "reason": "The interview was too vague to produce a setup card. "
                          "A card that matches everything is worse than no card.",
            }
        return {"card": _card_json(card), "reason": None}

    # ── playbook ─────────────────────────────────────────────────────────────

    @app.get("/api/playbook", dependencies=auth)
    def get_playbook() -> list[dict[str, Any]]:
        return [
            {**_card_json(card), "stats": _stats_json(playbook.stats_for(card))}
            for card in playbook.current()
        ]

    # ── statistics ───────────────────────────────────────────────────────────

    @app.get("/api/stats", dependencies=auth)
    def stats() -> dict[str, Any]:
        s = compute_stats(db)
        worst, best = s.worst_time_of_day, s.best_time_of_day
        return {
            "total_trades": s.total_trades, "wins": s.wins, "losses": s.losses,
            "breakeven": s.breakeven, "win_rate": s.win_rate,
            "net_pnl": str(s.net_pnl), "gross_pnl": str(s.gross_pnl),
            "commission": str(s.commission),
            "largest_win": str(s.largest_win), "largest_loss": str(s.largest_loss),
            "avg_r": s.avg_r,
            "expectancy": str(s.expectancy) if s.expectancy is not None else None,
            "profit_factor": s.profit_factor,
            "max_drawdown": str(s.max_drawdown),
            "by_time_of_day": [_slice_json(x) for x in s.by_time_of_day],
            "by_session": [_slice_json(x) for x in s.by_session],
            "by_symbol": [_slice_json(x) for x in s.by_symbol],
            "worst_time_of_day": _slice_json(worst) if worst else None,
            "best_time_of_day": _slice_json(best) if best else None,
        }

    @app.get("/api/equity", dependencies=auth)
    def equity() -> list[dict[str, Any]]:
        return [
            {"at": p.at.isoformat(), "equity": str(p.equity), "trade_id": p.trade_id}
            for p in equity_curve(db, cfg.broker.starting_balance)
        ]

    @app.get("/api/daily", dependencies=auth)
    def daily() -> dict[str, str]:
        return {day: str(value) for day, value in sorted(daily_pnl(db).items())}

    @app.get("/api/evaluation", dependencies=auth)
    def evaluation() -> dict[str, Any]:
        """Is the playbook actually helping? Allowed to say no."""
        comparison = evaluate_playbook(db)
        return {
            "followed": _slice_json(comparison.followed),
            "unfollowed": _slice_json(comparison.unfollowed),
            "has_enough_data": comparison.has_enough_data,
            "verdict": comparison.verdict(),
            "caveat": comparison.caveat,
        }

    # ── audit ────────────────────────────────────────────────────────────────

    @app.get("/api/audit", dependencies=auth)
    def audit_log(limit: int = 100) -> list[dict[str, Any]]:
        return [
            {
                "at": e.created_at.isoformat(), "type": e.event_type,
                "summary": e.summary, "severity": e.severity, "payload": e.payload,
            }
            for e in audit.recent(limit)
        ]

    # ── sync (for the future mobile client) ──────────────────────────────────

    @app.get("/api/changes", dependencies=auth)
    def changes(since: datetime) -> dict[str, Any]:
        """Delta since a timestamp, tombstones included.

        Nothing consumes this yet. It exists because the schema was built for it
        and leaving it unexercised is how sync-readiness quietly rots.
        """
        return db.changed_since(since)

    # ── webhook (Plane C) ────────────────────────────────────────────────────

    @app.post(cfg.tradingview.webhook_path)
    async def tradingview_webhook(
        request: Request,
        x_signature: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Pine alert ingress. Deliberately outside the bearer-token dependency —
        TradingView cannot send one, which is why it carries an HMAC instead."""
        if not cfg.tradingview.webhook_enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook disabled")

        body = await request.body()
        try:
            signal = ingest(
                body, secret=cfg.tradingview.webhook_secret,
                db=db, audit=audit, signature=x_signature,
            )
        except WebhookRejected as exc:
            # Deliberately terse. This endpoint faces the internet and a
            # detailed rejection is a probing oracle.
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Rejected") from exc

        proposal = agent.propose(signal, max_quantity=cfg.risk.max_position_contracts)
        if proposal is not None:
            await notifier.proposal(
                proposal.symbol, proposal.side.value, proposal.is_grounded
            )
        return {
            "signal_id": str(signal.id),
            "proposal_id": str(proposal.id) if proposal else None,
            "grounded": proposal.is_grounded if proposal else None,
        }

    app.state.db = db
    app.state.config = cfg
    app.state.broker = broker
    return app


# ─── serialisation ───────────────────────────────────────────────────────────
#
# Decimals are serialised as strings, not floats. JSON numbers are IEEE doubles,
# and round-tripping money through one is how $400.10 becomes 400.09999999999997
# on a dashboard.


def _order_json(o: Order) -> dict[str, Any]:
    return {
        "id": str(o.id), "symbol": o.symbol, "side": o.side.value,
        "quantity": o.quantity, "order_type": o.order_type.value,
        "status": o.status.value,
        "limit_price": str(o.limit_price) if o.limit_price else None,
        "stop_price": str(o.stop_price) if o.stop_price else None,
        "filled_quantity": o.filled_quantity,
        "average_fill_price": str(o.average_fill_price) if o.average_fill_price else None,
        "reject_reason": o.reject_reason,
        "created_at": o.created_at.isoformat(),
    }


def _trade_json(t: Any, *, full: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(t.id), "symbol": t.symbol, "contract": t.contract,
        "side": t.side.value, "quantity": t.quantity,
        "entry_price": str(t.entry_price),
        "exit_price": str(t.exit_price) if t.exit_price else None,
        "entry_at": t.entry_at.isoformat(),
        "exit_at": t.exit_at.isoformat() if t.exit_at else None,
        "net_pnl": str(t.net_pnl), "gross_pnl": str(t.gross_pnl),
        "commission": str(t.commission),
        "r_multiple": t.r_multiple, "outcome": t.outcome.value,
        "is_open": t.is_open,
        "session": t.session.value if t.session else None,
        "time_of_day": t.time_of_day.label if t.time_of_day else None,
        "followed_playbook": t.followed_playbook,
        "has_interview": t.has_interview,
        "setup_card_id": str(t.setup_card_id) if t.setup_card_id else None,
    }
    if full:
        data.update({
            "mae": str(t.max_adverse_excursion),
            "mfe": str(t.max_favorable_excursion),
            "planned_risk": str(t.planned_risk) if t.planned_risk else None,
            "initial_stop": str(t.initial_stop) if t.initial_stop else None,
            "chart_timeframe": t.chart_timeframe,
            "chart_studies": t.chart_studies,
            "entry_screenshot": t.entry_screenshot,
            "notes": t.notes, "tags": t.tags,
            "interview": [
                {
                    "question": a.question, "answer": a.answer,
                    "answered": a.answered_at is not None,
                    "latency_seconds": a.latency_seconds,
                }
                for a in t.interview
            ],
        })
    return data


def _card_json(c: Any) -> dict[str, Any]:
    return {
        "id": str(c.id), "name": c.name, "slug": c.slug, "version": c.version,
        "description": c.description, "trigger": c.trigger, "context": c.context,
        "invalidation": c.invalidation, "management": c.management,
        "instruments": c.instruments, "timeframes": c.timeframes,
        "sample_size": c.sample_size,
        "is_meaningful": c.is_statistically_meaningful,
        "validated": c.validated,
    }


def _stats_json(s: Any) -> dict[str, Any]:
    return {
        "sample_size": s.sample_size, "wins": s.wins, "losses": s.losses,
        "win_rate": s.win_rate, "net_pnl": str(s.net_pnl), "avg_r": s.avg_r,
        "is_provisional": s.is_provisional, "summary": s.summary(),
        "by_time_of_day": [
            {"label": label, "trades": count, "net_pnl": str(pnl)}
            for label, count, pnl in s.by_time_of_day
        ],
    }


def _slice_json(s: Any) -> dict[str, Any]:
    return {
        "label": s.label, "trades": s.trades, "wins": s.wins, "losses": s.losses,
        "win_rate": s.win_rate, "net_pnl": str(s.net_pnl), "avg_r": s.avg_r,
    }
