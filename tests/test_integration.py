"""Seam tests — the class of test that was missing.

Every bug that reached the user in this project's first week lived at a boundary
between two components, not inside one. The unit tests were green throughout:
the paper broker, the contract specs, the sessions, and the risk gate were right
the first time and stayed right. What broke was always the join.

- config ↔ environment: YAML silently outranked ``SHANI_*`` env vars, so every
  documented override was ignored.
- portal ↔ API: a relative ``/../health`` path the browser normalised away, and
  an error handler that read the response body twice and so replaced every real
  error with a misleading one.
- Shani ↔ TradingView: the client used ``window.tvWidget``, which does not exist
  on the page TradingView Desktop actually loads.
- saved key ↔ running process: a key written to ``.env`` was never read back, so
  it worked until the next restart and then silently stopped.

None of those were subtle. All of them were invisible to a test suite that only
ever exercised one module at a time. This file exercises the joins.

The rule for adding to it: if a failure could make the portal show wrong or
empty data while every unit test stays green, it belongs here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shani.api.app import build_app
from shani.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Exactly what the portal requests on load. Kept in this order deliberately —
#: it is the sequence in Portal.tsx's refresh(), and a 404 or 401 on any one of
#: them empties the whole dashboard.
PORTAL_CALLS: tuple[str, ...] = (
    "/api/health",
    "/api/watchlist",
    "/api/account",
    "/api/positions",
    "/api/trades?limit=60",
    "/api/stats",
    "/api/equity",
    "/api/playbook",
    "/api/evaluation",
    "/api/settings/model",
)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.database_path = tmp_path / "seam.db"
    cfg.screenshot_dir = tmp_path / "shots"
    # Upstream market data is not a seam we control; disabled so these stay
    # hermetic and runnable on a plane.
    cfg.tradingview.screener_enabled = False
    cfg.tradingview.webhook_secret = "seam-test-secret"
    cfg.model.provider = "none"
    cfg.broker.enforce_market_hours = False
    cfg.broker.slippage_ticks = 0
    return cfg


@pytest.fixture
def client(config: Config) -> TestClient:
    return TestClient(build_app(config))


class TestPortalContract:
    """The portal and the API must agree on what exists."""

    def test_every_call_the_portal_makes_on_load_succeeds(self, client: TestClient) -> None:
        """One 404 here empties the entire dashboard.

        This is the test that would have caught the `/../health` bug, where a
        relative path the browser normalised away 404'd and the portal rendered
        empty panels instead of an error.
        """
        failures = [(p, client.get(p).status_code) for p in PORTAL_CALLS]
        bad = [(p, code) for p, code in failures if code != 200]
        assert not bad, f"portal calls failing: {bad}"

    def test_portal_client_references_no_endpoint_that_does_not_exist(
        self, client: TestClient
    ) -> None:
        """Parse the real API client and check every path against the router.

        A contract test rather than a mock: it reads ``portal/lib/api.ts`` and
        fails if the frontend calls something the backend does not serve.
        """
        source = (REPO_ROOT / "portal" / "lib" / "api.ts").read_text(encoding="utf-8")
        called = set(re.findall(r"request<[^>]*>\(\s*[`'\"]([^`'\"$?]+)", source))
        assert called, "found no API calls to check — has api.ts been restructured?"

        routes = {getattr(r, "path", "") for r in client.app.routes}
        missing = []
        for path in called:
            full = "/api" + path.rstrip("/")
            prefix = full.split("{")[0]
            if full not in routes and not any(r.startswith(prefix) for r in routes):
                missing.append(path)
        assert not missing, f"portal calls endpoints that do not exist: {missing}"

    def test_no_portal_path_relies_on_url_normalisation(self) -> None:
        """``/../`` in a fetch path is silently rewritten by the browser."""
        source = (REPO_ROOT / "portal" / "lib" / "api.ts").read_text(encoding="utf-8")
        assert "/../" not in source, (
            "a request path contains '/../', which browsers normalise before the "
            "proxy sees it — use an absolute route instead"
        )

    def test_error_handler_reads_the_response_body_exactly_once(self) -> None:
        """A Response body is a single-use stream.

        ``try json() catch text()`` throws from inside the catch and replaces
        the real server error with "body stream already read", which sends the
        user debugging entirely the wrong thing.
        """
        source = (REPO_ROOT / "portal" / "lib" / "api.ts").read_text(encoding="utf-8")
        assert "const raw = await response.text();" in source
        assert "await response.json()" not in source.split("if (!response.ok)")[1][:600]


class TestMoneyOverTheWire:
    """JSON numbers are IEEE doubles. Money must never become one."""

    def _seed_closed_trade(self, client: TestClient) -> None:
        # Every price here sits on ES's 0.25 tick grid. Writing 5010.10 as a
        # target — as an earlier version of this test did — is correctly
        # rejected by the broker, and the trade then never closes.
        client.post("/api/price", json={"symbol": "ES", "price": "5000.00"})
        response = client.post("/api/orders", json={
            "symbol": "ES", "side": "buy", "quantity": 1,
            "stop_loss": "4990.00", "take_profit": "5010.25",
        })
        assert response.status_code == 201, f"seed order rejected: {response.json()}"
        client.post("/api/price", json={"symbol": "ES", "price": "5010.25"})

    def test_account_money_fields_are_strings(self, client: TestClient) -> None:
        body = client.get("/api/account").json()
        for field in ("balance", "equity", "realized_pnl", "unrealized_pnl",
                      "commission_paid", "realized_today", "remaining_daily_loss"):
            assert isinstance(body[field], str), f"{field} serialised as {type(body[field])}"

    def test_trade_money_survives_the_round_trip_exactly(self, client: TestClient) -> None:
        """$400.10 must not come back as 400.09999999999997."""
        self._seed_closed_trade(client)
        trade = client.get("/api/trades").json()[0]
        for field in ("net_pnl", "gross_pnl", "commission", "entry_price"):
            assert isinstance(trade[field], str), f"{field} is not a string"
        # 10.25 points * $50 = $512.50 exactly, with no float drift.
        assert trade["gross_pnl"] == "512.50"
        assert trade["net_pnl"] == "507.50"   # less $5.00 round turn

    def test_stats_money_fields_are_strings(self, client: TestClient) -> None:
        self._seed_closed_trade(client)
        stats = client.get("/api/stats").json()
        for field in ("net_pnl", "gross_pnl", "commission", "max_drawdown"):
            assert isinstance(stats[field], str)


class TestConfigPrecedence:
    """Environment variables must beat config.yaml, as documented everywhere."""

    def test_env_overrides_a_yaml_backed_value(self, monkeypatch: Any) -> None:
        """The bug: init kwargs outranked env, so every SHANI_* was ignored.

        Silent, and it disabled exactly the mechanism ``.env.example`` and the
        docs tell people to use for secrets.
        """
        from shani.config import load_config

        monkeypatch.setenv("SHANI_RISK__MAX_DAILY_LOSS", "4242")
        assert str(load_config().risk.max_daily_loss) == "4242"

    def test_env_overrides_a_nested_boolean(self, monkeypatch: Any) -> None:
        from shani.config import load_config

        monkeypatch.setenv("SHANI_TRADINGVIEW__DESKTOP_ENABLED", "true")
        assert load_config().tradingview.desktop_enabled is True

    def test_live_trading_needs_both_flag_and_phrase(self) -> None:
        from shani.config import LIVE_CONFIRMATION_PHRASE, Config

        cfg = Config()
        cfg.broker.allow_live = True
        assert not cfg.broker.live_enabled, "flag alone must not enable live"
        cfg.broker.live_confirmation = LIVE_CONFIRMATION_PHRASE
        assert cfg.broker.live_enabled


class TestSecretResolution:
    """A saved key must survive a restart."""

    def test_key_resolves_from_dotenv_without_the_process_environment(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Nothing loads ``.env`` into ``os.environ``.

        Pydantic reads it when building settings, but an API key is not a
        settings field — the SDKs look it up from the environment. Without a
        fallback, a key saved through the portal worked until the next restart
        and then silently stopped.
        """
        import shani.settings_store as store
        from shani.agent.llm import LLM
        from shani.config import ModelConfig

        env_file = tmp_path / ".env"
        env_file.write_text("OPENROUTER_API_KEY=sk-or-test-value\n", encoding="utf-8")
        monkeypatch.setattr(store, "ENV_PATH", env_file)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        llm = LLM(config=ModelConfig(provider="openrouter"))
        assert llm._resolve_key("OPENROUTER_API_KEY") == "sk-or-test-value"

    def test_a_stored_key_is_never_returned_by_the_api(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """The settings endpoint must not become a credential-read endpoint."""
        import shani.settings_store as store

        env_file = tmp_path / ".env"
        env_file.write_text("OPENROUTER_API_KEY=sk-or-supersecret-1234\n", encoding="utf-8")
        monkeypatch.setattr(store, "ENV_PATH", env_file)

        cfg = Config()
        cfg.model.provider = "openrouter"
        body = store.read_model_env(cfg)

        assert body["has_key"] is True
        assert body["key_hint"] == "…1234"
        assert "sk-or-supersecret-1234" not in str(body)

    def test_env_writes_preserve_unrelated_lines_and_comments(self, tmp_path: Path) -> None:
        from shani.settings_store import write_env_values

        env_file = tmp_path / ".env"
        env_file.write_text(
            "# a comment worth keeping\nEXISTING=keepme\nOPENROUTER_API_KEY=old\n",
            encoding="utf-8",
        )
        write_env_values({"OPENROUTER_API_KEY": "new"}, path=env_file)
        text = env_file.read_text(encoding="utf-8")

        assert "# a comment worth keeping" in text
        assert "EXISTING=keepme" in text
        assert "OPENROUTER_API_KEY=new" in text
        assert "old" not in text


class TestFullTradeLifecycleOverHttp:
    """The whole path, through the API rather than the objects."""

    def test_signal_to_closed_trade_to_interview(self, client: TestClient) -> None:
        import json as jsonlib

        # Plane C: an alert arrives.
        alert = jsonlib.dumps({
            "secret": "seam-test-secret", "symbol": "ES", "action": "buy",
            "price": "5000.00", "strategy": "Seam test",
        })
        signal = client.post("/webhook/tradingview", content=alert,
                             headers={"Content-Type": "application/json"})
        assert signal.status_code == 200
        assert signal.json()["signal_id"]

        # A signal alone must never place an order.
        assert client.get("/api/orders").json() == []

        # The human places the trade.
        client.post("/api/price", json={"symbol": "ES", "price": "5000.00"})
        order = client.post("/api/orders", json={
            "symbol": "ES", "side": "buy", "quantity": 1,
            "stop_loss": "4990.00", "take_profit": "5020.00",
        })
        assert order.status_code == 201
        assert order.json()["status"] == "filled"

        # Price reaches the target; OCO cancels the stop and the trade closes.
        client.post("/api/price", json={"symbol": "ES", "price": "5020.00"})
        assert client.get("/api/positions").json() == []

        trade = client.get("/api/trades").json()[0]
        assert trade["is_open"] is False
        assert trade["outcome"] == "win"
        assert trade["net_pnl"] == "995.00"   # 20pts * $50 - $5 round turn

        # The interview attaches and records an answer.
        detail = client.post(f"/api/trades/{trade['id']}/interview",
                             json={"index": 0, "answer": "Seam test answer."})
        assert detail.status_code == 200
        assert detail.json()["has_interview"] is True

        # And the statistics pick it up.
        stats = client.get("/api/stats").json()
        assert stats["total_trades"] == 1
        assert stats["wins"] == 1

    def test_risk_gate_rejection_reaches_the_portal_as_structured_detail(
        self, client: TestClient
    ) -> None:
        """The portal renders `detail.reasons`; a bare string would show nothing."""
        client.post("/api/price", json={"symbol": "ES", "price": "5000.00"})
        response = client.post("/api/orders", json={
            "symbol": "ES", "side": "buy", "quantity": 1,
        })
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["rule"] == "require_stop_loss"
        assert isinstance(detail["reasons"], list) and detail["reasons"]


class TestAuthSeam:
    def test_blank_token_means_no_auth_on_loopback(self, config: Config) -> None:
        config.server.api_token = ""
        client = TestClient(build_app(config))
        assert client.get("/api/trades").status_code == 200

    def test_a_configured_token_is_enforced_on_every_portal_call(
        self, config: Config
    ) -> None:
        config.server.api_token = "seam-token"
        client = TestClient(build_app(config))
        for path in PORTAL_CALLS:
            if path == "/api/health":
                continue  # health is deliberately open for probes
            assert client.get(path).status_code == 401, f"{path} is not protected"

    def test_the_webhook_stays_reachable_without_a_bearer_token(
        self, config: Config
    ) -> None:
        """TradingView cannot send one — that is why it carries an HMAC."""
        import json as jsonlib

        config.server.api_token = "seam-token"
        client = TestClient(build_app(config))
        response = client.post(
            "/webhook/tradingview",
            content=jsonlib.dumps({
                "secret": "seam-test-secret", "symbol": "ES", "action": "buy",
            }),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200


class TestPlaneBIsolation:
    """All TradingView page coupling must stay in one file."""

    def test_no_tradingview_internals_leak_outside_the_bridge(self) -> None:
        """The whole point of the one-file rule is that a TradingView update is
        a one-file fix. This test is what keeps that true."""
        bridge = REPO_ROOT / "shani" / "market" / "tradingview_cdp.py"
        offenders = []
        for path in (REPO_ROOT / "shani").rglob("*.py"):
            if path == bridge:
                continue
            text = path.read_text(encoding="utf-8")
            if "tvWidget" in text or "TradingViewApi" in text:
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
        assert not offenders, f"TradingView internals leaked into: {offenders}"

    def test_the_bridge_checks_the_application_global_first(self) -> None:
        """`tvWidget` belongs to the embeddable Charting Library and does NOT
        exist on tradingview.com, which is what Desktop loads. Checking it first
        is what made this fail against every real install.

        Asserted against the resolver expression itself rather than the file
        text — the module docstring mentions both names while explaining the
        distinction, so a naive whole-file ordering check tests prose.
        """
        from shani.market.tradingview_cdp import _JS_WIDGET

        assert _JS_WIDGET.index("window.TradingViewApi") < _JS_WIDGET.index("window.tvWidget")

    def test_every_evaluated_expression_uses_a_shared_resolver(self) -> None:
        """A new expression that hardcodes a global reintroduces the bug.

        There are two entry points, and they are not interchangeable:

        ``_JS_WIDGET`` resolves the *chart*, and must try ``TradingViewApi``
        before ``tvWidget`` for the reason above.

        ``_JS_BROKER`` resolves the *trading account*, and deliberately names
        only ``TradingViewApi``. The trading API is part of the application;
        an embeddable Charting Library widget has no broker at all, so there is
        no second global to fall back to and pretending otherwise would only
        hide a real failure behind a confusing one.
        """
        from shani.market import tradingview_cdp as bridge

        expressions = {
            name: value for name, value in vars(bridge).items()
            if name.startswith("_JS_")
            and name not in {"_JS_WIDGET", "_JS_BROKER"}
            and isinstance(value, str)
        }
        assert expressions, "no evaluated expressions found — has the module moved?"
        for name, expression in expressions.items():
            if "window.tvWidget" not in expression and "window.TradingViewApi" not in expression:
                continue
            uses_widget = bridge._JS_WIDGET.strip() in expression
            uses_broker = bridge._JS_BROKER.strip() in expression
            assert uses_widget or uses_broker, (
                f"{name} names a TradingView global directly instead of "
                f"embedding the _JS_WIDGET or _JS_BROKER resolver"
            )

    def test_the_broker_resolver_goes_through_the_trading_api(self) -> None:
        """Reading the account must not depend on the Account Manager DOM.

        The grid is virtualised — only the selected tab is rendered — so a DOM
        read returns whatever tab the trader happens to have up, which is how an
        early attempt came back with the watchlist instead of any trade. Going
        through the broker object needs no tab to be active.
        """
        from shani.market import tradingview_cdp as bridge

        assert "trading()" in bridge._JS_BROKER
        assert "_activeBroker" in bridge._JS_BROKER
        for name in ("_JS_EXECUTIONS", "_JS_ORDER_HISTORY", "_JS_ACCOUNT"):
            expression = getattr(bridge, name)
            assert bridge._JS_BROKER.strip() in expression, f"{name} bypasses _JS_BROKER"
            assert "querySelector" not in expression, (
                f"{name} reads the DOM; the account manager grid is virtualised "
                f"and only renders the selected tab"
            )


class TestTradingViewImportIsIdempotent:
    """Importing the trade history twice must not double the trade table.

    This is the seam the whole import feature turns on. Every unit test of the
    pairing can pass while this fails, because pairing is a pure function and
    knows nothing about the database it lands in — and the failure is silent.
    Nothing errors; the trader just opens the portal and sees fifty trades where
    there should be twenty-five, with every win rate and expectancy computed off
    the doubled table.

    The import deliberately re-reads the *entire* history each time rather than
    tracking a high-water mark, so "run it twice" is the normal case, not an
    edge case.
    """

    @staticmethod
    def _fills() -> list[Any]:
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        from shani.market.tradingview_cdp import TradingViewExecution

        base = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
        prices = [("7764", 1), ("7772.25", -1), ("7805", 1), ("7803.25", -1)]
        return [
            TradingViewExecution(
                id=f"e{i}", symbol="CME_MINI:MESU2026", side=side, quantity=10,
                price=Decimal(price), time=base + timedelta(minutes=10 * i),
            )
            for i, (price, side) in enumerate(prices)
        ]

    def test_importing_twice_leaves_one_copy_of_each_trade(self, tmp_path: Path) -> None:
        from shani.db import Database
        from shani.ingest.tradingview import build_trades, save_trades

        db = Database(tmp_path / "import.db")
        fills = self._fills()

        first = build_trades(fills, account="34862113")
        inserted, updated = save_trades(db, first)
        assert (inserted, updated) == (2, 0)
        assert db.trades.count() == 2

        second = build_trades(fills, account="34862113")
        inserted, updated = save_trades(db, second)
        assert (inserted, updated) == (0, 2), "re-import inserted instead of updating"
        assert db.trades.count() == 2, "the trade table doubled on re-import"
        db.close()

    def test_reimport_does_not_erase_the_interview(self, tmp_path: Path) -> None:
        """The half of idempotency that a row count cannot catch.

        Not duplicating is not enough — a re-import that *overwrites* is just as
        destructive, because the interview is the one field that cannot be
        regenerated. The venue can always be re-read; what the trader said about
        the trade at the time cannot.
        """
        from shani.db import Database
        from shani.ingest.tradingview import build_trades, save_trades
        from shani.models import InterviewAnswer

        db = Database(tmp_path / "import.db")
        fills = self._fills()
        save_trades(db, build_trades(fills, account="34862113"))

        trade = db.trades.all()[0]
        trade.interview = [
            InterviewAnswer(question="What did you see?", answer="Failed breakdown at the low.")
        ]
        trade.notes = "Waited for the reclaim."
        trade.tags = ["reclaim"]
        db.trades.save(trade)

        save_trades(db, build_trades(fills, account="34862113"))

        after = db.trades.get(trade.id)
        assert after is not None
        assert after.notes == "Waited for the reclaim."
        assert after.tags == ["reclaim"]
        assert [a.answer for a in after.interview] == ["Failed breakdown at the low."]
        db.close()

    def test_a_new_trade_is_added_without_disturbing_the_old_ones(self, tmp_path: Path) -> None:
        """The realistic case: import, trade again, import again."""
        from datetime import timedelta
        from decimal import Decimal

        from shani.db import Database
        from shani.ingest.tradingview import build_trades, save_trades
        from shani.market.tradingview_cdp import TradingViewExecution

        db = Database(tmp_path / "import.db")
        fills = self._fills()
        save_trades(db, build_trades(fills, account="34862113"))
        original_ids = {t.id for t in db.trades.all()}

        later = fills[-1].time + timedelta(hours=1)
        fills += [
            TradingViewExecution(
                id="e98", symbol="CME_MINI:MESU2026", side=-1, quantity=10,
                price=Decimal("7807"), time=later,
            ),
            TradingViewExecution(
                id="e99", symbol="CME_MINI:MESU2026", side=1, quantity=10,
                price=Decimal("7801.75"), time=later + timedelta(minutes=20),
            ),
        ]
        inserted, updated = save_trades(db, build_trades(fills, account="34862113"))
        assert (inserted, updated) == (1, 2)
        assert db.trades.count() == 3
        assert original_ids < {t.id for t in db.trades.all()}
        db.close()


class TestImportEndpoint:
    """The import route, where it sits in the router and how it fails."""

    def test_import_path_is_not_swallowed_by_the_trade_detail_route(
        self, client: TestClient
    ) -> None:
        """``/api/trades/import`` must not be parsed as a trade id.

        ``/api/trades/{trade_id}`` is declared first and would happily try to
        read "import" as a UUID. It does not today only because that route is a
        GET and this one is a POST — which is a load-bearing detail that a
        future refactor could quietly remove.
        """
        response = client.post("/api/trades/import")
        assert response.status_code != 422, (
            "'import' was parsed as a trade_id — the detail route is shadowing "
            "the import route"
        )

    def test_unreachable_tradingview_is_503_with_instructions(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        """Not a 500. Nothing is broken; TradingView just is not running.

        The distinction matters because the error text is the only place the
        user is told to relaunch Desktop with the debug flag.
        """
        from shani.market import tradingview_cdp

        async def refuse(self: Any) -> Any:
            raise tradingview_cdp.TradingViewUnavailableError(
                "Cannot reach the TradingView debug port at http://localhost:9222."
            )

        monkeypatch.setattr(tradingview_cdp.TradingViewDesktop, "account_id", refuse)
        response = client.post("/api/trades/import")
        assert response.status_code == 503
        assert "debug port" in response.json()["detail"]

    def test_successful_import_reports_what_it_did(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        from shani.market import tradingview_cdp
        from shani.market.tradingview_cdp import TradingViewExecution

        base = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
        fills = [
            TradingViewExecution(
                id="e0", symbol="CME_MINI:MESU2026", side=1, quantity=10,
                price=Decimal("7764"), time=base,
            ),
            TradingViewExecution(
                id="e1", symbol="CME_MINI:MESU2026", side=-1, quantity=10,
                price=Decimal("7772.25"), time=base + timedelta(minutes=20),
            ),
        ]

        async def account(self: Any) -> str:
            return "34862113"

        async def executions(self: Any) -> Any:
            return fills

        async def orders(self: Any) -> Any:
            return []

        monkeypatch.setattr(tradingview_cdp.TradingViewDesktop, "account_id", account)
        monkeypatch.setattr(tradingview_cdp.TradingViewDesktop, "executions", executions)
        monkeypatch.setattr(tradingview_cdp.TradingViewDesktop, "order_history", orders)

        body = client.post("/api/trades/import").json()
        assert body["imported"] == 1
        assert body["gross_pnl"] == "412.50"
        assert body["skipped"] == {}

        # And the trade is actually queryable through the portal's own route.
        trades = client.get("/api/trades").json()
        assert any(t["symbol"] == "MES" for t in trades)

        # Second import must not double it.
        assert client.post("/api/trades/import").json()["imported"] == 1
        assert len([t for t in client.get("/api/trades").json() if t["symbol"] == "MES"]) == 1


class TestDemoDataIsReversible:
    """`shani demo` must not be a one-way door.

    Seeding synthetic history is how a new user finds out whether the portal is
    any good. Before this was reversible the only documented way to undo it was
    "delete the database", which takes the user's real journal with it — so the
    safe move was to never try the demo at all, which defeats its purpose.

    The property under test is not "clear removes the demo data". It is "clear
    removes the demo data *and nothing else*".
    """

    def _seed_mixed(self, tmp_path: Path) -> Any:
        from datetime import UTC, datetime
        from decimal import Decimal

        from shani.cli import DEMO_SETUP_SLUG, DEMO_TAG
        from shani.db import Database
        from shani.models import SetupCard, Side, Trade

        db = Database(tmp_path / "mixed.db")
        for _ in range(3):
            db.trades.save(Trade(
                symbol="ES", side=Side.BUY, quantity=1,
                entry_price=Decimal("5000"), exit_price=Decimal("5004"),
                entry_at=datetime(2026, 8, 1, 14, 0, tzinfo=UTC),
                exit_at=datetime(2026, 8, 1, 15, 0, tzinfo=UTC),
                gross_pnl=Decimal("200"), tags=[DEMO_TAG],
            ))
        # A real trade that looks exactly like the synthetic ones. A cleanup
        # that matched on price shape rather than the tag would eat this.
        real = Trade(
            symbol="ES", side=Side.BUY, quantity=1,
            entry_price=Decimal("5000"), exit_price=Decimal("5004"),
            entry_at=datetime(2026, 8, 2, 14, 0, tzinfo=UTC),
            exit_at=datetime(2026, 8, 2, 15, 0, tzinfo=UTC),
            gross_pnl=Decimal("200"), notes="my actual trade",
        )
        db.trades.save(real)
        db.setups.save(SetupCard(name="Opening drive", slug=DEMO_SETUP_SLUG))
        db.setups.save(SetupCard(name="Mine", slug="my-own-setup"))
        db.close()
        return real

    def test_clear_removes_only_the_seeded_rows(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from shani import cli
        from shani.config import Config
        from shani.db import Database

        real = self._seed_mixed(tmp_path)
        cfg = Config()
        cfg.data_dir = tmp_path
        cfg.database_path = tmp_path / "mixed.db"
        monkeypatch.setattr(cli, "load_config", lambda: cfg)

        cli._clear_demo_data()

        db = Database(tmp_path / "mixed.db")
        remaining = db.trades.all()
        assert len(remaining) == 1, "clear took more than the seeded trades"
        assert remaining[0].id == real.id
        assert remaining[0].notes == "my actual trade"

        slugs = {c.slug for c in db.setups.all()}
        assert slugs == {"my-own-setup"}, "clear took a setup card that was not seeded"
        db.close()

    def test_clear_is_safe_to_run_when_nothing_was_seeded(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from shani import cli
        from shani.config import Config
        from shani.db import Database

        cfg = Config()
        cfg.data_dir = tmp_path
        cfg.database_path = tmp_path / "empty.db"
        monkeypatch.setattr(cli, "load_config", lambda: cfg)
        Database(cfg.database_path).close()

        cli._clear_demo_data()  # must not raise

    def test_a_surviving_card_does_not_cite_deleted_trades(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Sample size is a claim about evidence, and must not outlive it.

        A card left reporting "7 trades" whose trades have all been deleted is
        worse than one reporting zero: the portal renders the number, and the
        agent leans on it.
        """
        from datetime import UTC, datetime
        from decimal import Decimal

        from shani import cli
        from shani.config import Config
        from shani.db import Database
        from shani.models import SetupCard, Side, Trade

        db = Database(tmp_path / "cite.db")
        seeded = Trade(
            symbol="ES", side=Side.BUY, quantity=1,
            entry_price=Decimal("5000"), exit_price=Decimal("5004"),
            entry_at=datetime(2026, 8, 1, 14, 0, tzinfo=UTC),
            gross_pnl=Decimal("200"), tags=[cli.DEMO_TAG],
        )
        db.trades.save(seeded)
        mine = SetupCard(name="Mine", slug="my-own-setup", trade_ids=[seeded.id])
        db.setups.save(mine)
        db.close()

        cfg = Config()
        cfg.data_dir = tmp_path
        cfg.database_path = tmp_path / "cite.db"
        monkeypatch.setattr(cli, "load_config", lambda: cfg)
        cli._clear_demo_data()

        db = Database(tmp_path / "cite.db")
        survivor = db.setups.all()[0]
        assert survivor.trade_ids == []
        assert survivor.sample_size == 0
        db.close()


class TestLiveFillHookIsSingleSubscription:
    """The page-side half of not double-counting fills.

    TradingView's `executionUpdate` is a Delegate:

        subscribe(object, member, singleShot) {
          this._listeners.push({object, member, singleShot: !!singleShot, skip: false})
        }

    It calls *every* registered listener. So if the injected hook subscribes a
    second time — on a reconnect, or after a page reload — each fill is reported
    twice at source, before any Python code gets a say. The database side would
    absorb a replayed fill idempotently, but two subscriptions also means two
    screenshots and two notifications per fill.

    Verified live against Desktop 3.3.0: listener count went 1 → 2 on install and
    stayed at 2 when the install expression was evaluated again.
    """

    def test_the_hook_refuses_to_subscribe_twice(self) -> None:
        from shani.market import tradingview_cdp as bridge

        js = bridge._JS_INSTALL_EXEC_HOOK
        assert "window.__shaniExecHook" in js, "no guard flag in the injected hook"
        guard = js.index("if (!window.__shaniExecHook)")
        subscribe = js.index("executionUpdate.subscribe")
        assert guard < subscribe, (
            "executionUpdate.subscribe is reached without first checking the "
            "guard flag — a reinstall would deliver every fill twice"
        )

    def test_the_hook_asks_for_a_stream_as_well_as_listening(self) -> None:
        """Both calls are required, and they do different jobs.

        `subscribeExecutions(symbol)` takes no callback — it only tells the
        broker connection to start sending. Subscribing to the delegate without
        it can leave a silent stream; calling it without subscribing means
        nobody is listening.
        """
        from shani.market import tradingview_cdp as bridge

        js = bridge._JS_INSTALL_EXEC_HOOK
        assert "subscribeExecutions(" in js
        assert "executionUpdate.subscribe(" in js

    def test_a_reporting_failure_cannot_break_the_traders_app(self) -> None:
        """The hook runs inside the user's live trading application.

        An exception thrown out of a delegate listener runs in TradingView's own
        event dispatch. Shani is a guest in that process and must not be able to
        interfere with it.
        """
        from shani.market import tradingview_cdp as bridge

        send = bridge._JS_INSTALL_EXEC_HOOK[
            bridge._JS_INSTALL_EXEC_HOOK.index("const send") :
            bridge._JS_INSTALL_EXEC_HOOK.index("b.executionUpdate.subscribe")
        ]
        assert "try {" in send and "catch" in send, (
            "the fill reporter is not wrapped in try/catch"
        )

    def test_the_binding_name_is_shared_between_python_and_the_page(self) -> None:
        """A literal on either side that drifts leaves a stream nobody receives."""
        from shani.market import tradingview_cdp as bridge

        assert bridge.EXECUTION_BINDING in bridge._JS_INSTALL_EXEC_HOOK
        assert f"window.{bridge.EXECUTION_BINDING}" in bridge._JS_INSTALL_EXEC_HOOK

    def test_live_and_batch_parse_a_fill_through_the_same_function(self) -> None:
        """One fill, one interpretation.

        The live stream and the batch read receive the same execution shape. If
        they parsed it separately, the two paths could disagree about a side or a
        price and the trade table would hold both answers.
        """
        import inspect

        from shani.market import tradingview_cdp as bridge

        source = inspect.getsource(bridge.TradingViewDesktop.executions)
        assert "_execution_from" in source
        assert "_execution_from" in inspect.getsource(bridge.TradingViewDesktop.watch_executions)
