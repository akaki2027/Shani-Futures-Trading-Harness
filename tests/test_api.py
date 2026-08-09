"""API tests, including the full signal-to-proposal loop.

The important case is :class:`TestTheLoop`, which walks the path the whole
project exists to enable: a webhook arrives, the agent proposes with the
trader's own history attached, the order goes through the risk gate, the trade
closes, the interview is answered, a setup card is written — and then the *same*
webhook arrives again and the proposal now cites the prior trade.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shani.api.app import build_app
from shani.config import Config
from shani.models import InterviewAnswer, Side, Trade
from shani.sessions import Session, TimeOfDay

SECRET = "webhook-secret-for-tests"
TOKEN = "api-token-for-tests"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.database_path = tmp_path / "api.db"
    cfg.tradingview.webhook_secret = SECRET
    cfg.tradingview.screener_enabled = False
    cfg.server.api_token = TOKEN
    cfg.model.provider = "none"
    cfg.broker.enforce_market_hours = False
    cfg.broker.slippage_ticks = 0
    return cfg


@pytest.fixture
def client(config: Config) -> TestClient:
    return TestClient(build_app(config))


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class TestAuth:
    def test_health_needs_no_token(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200

    def test_protected_route_rejects_a_missing_token(self, client: TestClient) -> None:
        assert client.get("/api/trades").status_code == 401

    def test_protected_route_rejects_a_wrong_token(self, client: TestClient) -> None:
        response = client.get("/api/trades", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401

    def test_valid_token_is_accepted(self, client: TestClient, auth: dict[str, str]) -> None:
        assert client.get("/api/trades", headers=auth).status_code == 200


class TestHealthAndReference:
    def test_health_reports_live_is_disabled(self, client: TestClient) -> None:
        assert client.get("/health").json()["live_enabled"] is False

    def test_instruments_expose_tick_values(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        data = client.get("/api/instruments", headers=auth).json()
        es = next(i for i in data if i["root"] == "ES")
        assert es["tick_value"] == "12.50"

    def test_money_is_serialised_as_strings_not_floats(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """JSON numbers are IEEE doubles; $400.10 must not become 400.09999..."""
        body = client.get("/api/account", headers=auth).json()
        for field in ("balance", "equity", "realized_pnl", "unrealized_pnl"):
            assert isinstance(body[field], str), f"{field} must serialise as a string"


class TestWebhook:
    def _post(self, client: TestClient, **fields: Any) -> Any:
        return client.post(
            "/webhook/tradingview",
            content=json.dumps({"secret": SECRET, **fields}),
            headers={"Content-Type": "application/json"},
        )

    def test_valid_alert_creates_a_signal(self, client: TestClient) -> None:
        response = self._post(client, symbol="ES", action="buy", price="5000.00")
        assert response.status_code == 200
        assert response.json()["signal_id"]

    def test_webhook_needs_no_bearer_token(self, client: TestClient) -> None:
        """TradingView cannot send one, which is why it carries an HMAC."""
        assert self._post(client, symbol="ES", action="buy").status_code == 200

    def test_unsigned_alert_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/webhook/tradingview", content=json.dumps({"symbol": "ES", "action": "buy"})
        )
        assert response.status_code == 400

    def test_rejection_is_terse(self, client: TestClient) -> None:
        """A detailed rejection on an internet-facing endpoint is an oracle."""
        response = client.post("/webhook/tradingview", content=json.dumps({"symbol": "ES"}))
        assert response.json()["detail"] == "Rejected"

    def test_unknown_instrument_is_rejected(self, client: TestClient) -> None:
        assert self._post(client, symbol="TSLA", action="buy").status_code == 400

    def test_signal_alone_places_no_order(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """The internet controls the first of four gates and no more."""
        self._post(client, symbol="ES", action="buy", price="5000.00")
        assert client.get("/api/orders", headers=auth).json() == []


class TestOrdersAndRiskGate:
    def _price(self, client: TestClient, auth: dict[str, str], price: str = "5000.00") -> None:
        client.post("/api/price", headers=auth, json={"symbol": "ES", "price": price})

    def test_order_without_a_stop_is_refused(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        self._price(client, auth)
        response = client.post(
            "/api/orders", headers=auth,
            json={"symbol": "ES", "side": "buy", "quantity": 1},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["rule"] == "require_stop_loss"

    def test_bracket_order_is_accepted_and_fills(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        self._price(client, auth)
        response = client.post(
            "/api/orders", headers=auth,
            json={"symbol": "ES", "side": "buy", "quantity": 1,
                  "stop_loss": "4990.00", "take_profit": "5020.00"},
        )
        assert response.status_code == 201
        assert response.json()["status"] == "filled"

    def test_oversized_order_is_refused_with_the_rule_named(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        self._price(client, auth)
        response = client.post(
            "/api/orders", headers=auth,
            json={"symbol": "ES", "side": "buy", "quantity": 99,
                  "stop_loss": "4990.00", "take_profit": "5020.00"},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["rule"] == "max_position_contracts"

    def test_off_tick_price_is_refused(self, client: TestClient, auth: dict[str, str]) -> None:
        self._price(client, auth)
        response = client.post(
            "/api/orders", headers=auth,
            json={"symbol": "ES", "side": "buy", "quantity": 1,
                  "stop_loss": "4990.13", "take_profit": "5020.00"},
        )
        assert response.status_code == 422

    def test_position_appears_after_a_fill(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        self._price(client, auth)
        client.post("/api/orders", headers=auth,
                    json={"symbol": "ES", "side": "buy", "quantity": 1,
                          "stop_loss": "4990.00", "take_profit": "5020.00"})
        positions = client.get("/api/positions", headers=auth).json()
        assert positions[0]["quantity"] == 1


class TestJournalAndStats:
    def _seed(self, client: TestClient) -> Trade:
        from datetime import UTC, datetime

        db = client.app.state.db
        trade = Trade(
            symbol="ES", side=Side.BUY, quantity=1,
            entry_price=Decimal("5000"), exit_price=Decimal("5004"),
            entry_at=datetime(2026, 3, 10, 9, 45, tzinfo=UTC),
            exit_at=datetime(2026, 3, 10, 10, 15, tzinfo=UTC),
            gross_pnl=Decimal("400"), commission=Decimal("5"),
            planned_risk=Decimal("200"),
            session=Session.RTH, time_of_day=TimeOfDay.OPENING_DRIVE,
        )
        db.trades.save(trade)
        return trade

    def test_trades_are_listed(self, client: TestClient, auth: dict[str, str]) -> None:
        self._seed(client)
        trades = client.get("/api/trades", headers=auth).json()
        assert trades[0]["net_pnl"] == "395"

    def test_trade_detail_includes_interview_and_excursions(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        trade = self._seed(client)
        detail = client.get(f"/api/trades/{trade.id}", headers=auth).json()
        assert "interview" in detail
        assert "mae" in detail

    def test_recording_an_interview_answer(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        trade = self._seed(client)
        response = client.post(
            f"/api/trades/{trade.id}/interview", headers=auth,
            json={"index": 0, "answer": "Failed auction at the overnight high"},
        )
        assert response.status_code == 200
        assert response.json()["has_interview"] is True

    def test_stats_surface_the_worst_time_of_day(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        self._seed(client)
        stats = client.get("/api/stats", headers=auth).json()
        assert stats["total_trades"] == 1
        assert "by_time_of_day" in stats

    def test_equity_curve(self, client: TestClient, auth: dict[str, str]) -> None:
        self._seed(client)
        assert len(client.get("/api/equity", headers=auth).json()) == 1

    def test_evaluation_reports_insufficient_data_honestly(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        self._seed(client)
        body = client.get("/api/evaluation", headers=auth).json()
        assert body["has_enough_data"] is False
        assert "Observational" in body["caveat"]

    def test_changes_endpoint_exists_for_future_sync(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        self._seed(client)
        response = client.get(
            "/api/changes", headers=auth, params={"since": "2020-01-01T00:00:00Z"}
        )
        assert response.status_code == 200
        assert len(response.json()["trades"]) == 1


class TestTheLoop:
    """The path the whole project exists to enable."""

    def test_second_identical_signal_is_grounded_by_the_first_trade(
        self, config: Config
    ) -> None:
        """Signal → trade → interview → setup card → the next signal cites it.

        The agent is stubbed, so this asserts the *plumbing*: that a card
        written from an interview is retrieved and offered to the agent the next
        time a matching signal arrives. Without this, Shani is a trade log.
        """
        from datetime import UTC, datetime

        from shani.agent.reasoning import Agent
        from shani.audit import AuditLog
        from shani.memory.playbook import Playbook
        from shani.models import SetupCard, Signal, SignalSource
        from tests.test_memory import StubLLM

        app = build_app(config)
        db = app.state.db
        playbook = Playbook(db)

        signal = Signal(
            source=SignalSource.PINE_WEBHOOK, symbol="ES",
            side=Side.BUY, price=Decimal("5000"),
            strategy_name="Opening drive continuation",
        )

        # First time through: nothing known.
        assert playbook.recall(signal).is_empty
        assert "No matching history" in playbook.recall(signal).brief()

        # A trade happens and gets interviewed.
        trade = Trade(
            symbol="ES", side=Side.BUY, quantity=1,
            entry_price=Decimal("5000"), exit_price=Decimal("5010"),
            entry_at=datetime(2026, 3, 10, 9, 45, tzinfo=UTC),
            exit_at=datetime(2026, 3, 10, 10, 5, tzinfo=UTC),
            gross_pnl=Decimal("500"), commission=Decimal("5"),
            planned_risk=Decimal("250"),
            session=Session.RTH, time_of_day=TimeOfDay.OPENING_DRIVE,
            interview=[InterviewAnswer(
                question="What did you see?",
                answer="Broke the opening range and the pullback held VWAP.",
                answered_at=datetime.now(UTC),
            )],
        )
        db.trades.save(trade)

        agent = Agent(db, StubLLM({
            "name": "Opening drive continuation",
            "description": "Join the first sustained push after the open.",
            "trigger": "Breaks the opening range high, pullback holds VWAP",
            "context": "RTH, first hour", "invalidation": "Close back inside the range",
            "management": "Stop below the pullback low",
            "timeframes": ["5m"], "confidence": 0.9,
        }), AuditLog(db))

        card = agent.extract_setup(trade)
        assert card is not None
        assert isinstance(card, SetupCard)

        # Second time through: the same signal now recalls the trade.
        recall = playbook.recall(signal)
        assert not recall.is_empty, "The setup card must be retrieved by the next signal"
        assert recall.setups[0].slug == "opening-drive-continuation"

        brief = recall.brief()
        assert "1 trades" in brief
        assert "provisional" in brief, "A single trade must be labelled provisional"
