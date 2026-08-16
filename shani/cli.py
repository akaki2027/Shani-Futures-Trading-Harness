"""Command line interface.

Four commands: ``init``, ``doctor``, ``demo``, ``serve``.

``doctor`` is the important one. Shani has several independent moving parts —
three TradingView planes, a model provider, a database — and each fails in its
own way. A single "something went wrong" would be useless, so every check prints
its own pass or fail line *with the fix in it*. Nobody should have to read
source to find out that TradingView Desktop needed a command-line flag.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from shani.config import CONFIG_PATH, Config, load_config

app = typer.Typer(
    name="shani",
    help="A trading harness that learns how you trade.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

OK = "[green]  ok  [/green]"
FAIL = "[red] fail [/red]"
WARN = "[yellow] warn [/yellow]"


@app.command()
def init(
    force: Annotated[bool, typer.Option(help="Overwrite an existing config.")] = False,
) -> None:
    """Create the configuration file and generate secrets."""
    if CONFIG_PATH.exists() and not force:
        console.print(f"[yellow]Config already exists at[/yellow] {CONFIG_PATH}")
        console.print("Use [bold]--force[/bold] to overwrite.")
        raise typer.Exit(1)

    config = Config()
    config.ensure_dirs()
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    api_token = secrets.token_urlsafe(32)
    webhook_secret = secrets.token_urlsafe(32)
    CONFIG_PATH.write_text(config.to_yaml(), encoding="utf-8")

    console.print(Panel.fit(
        f"[bold green]Shani is set up.[/bold green]\n\n"
        f"Config    {CONFIG_PATH}\n"
        f"Data      {config.data_dir}\n"
        f"Database  {config.db_path}",
        title="init",
    ))

    console.print("\n[bold]Add these to a .env file — they are NOT written to config.yaml:[/bold]")
    console.print(f"  SHANI_SERVER__API_TOKEN={api_token}")
    console.print(f"  SHANI_TRADINGVIEW__WEBHOOK_SECRET={webhook_secret}")
    console.print(
        "\n[dim]Secrets stay out of config.yaml because that is the file people "
        "paste into bug reports.[/dim]"
    )
    console.print("\nNext: [bold]shani doctor[/bold], then [bold]shani demo[/bold].")


@app.command()
def doctor() -> None:
    """Check every component and say exactly how to fix what is broken."""
    config = load_config()
    console.print("\n[bold]shani · doctor[/bold]")
    console.print("─" * 62)

    failures = 0

    # Database
    try:
        from shani.db import Database

        db = Database(config.db_path)
        trades = db.trades.count()
        console.print(f"{OK}  database — {config.db_path.name}, {trades} trades")
    except Exception as exc:
        failures += 1
        console.print(f"{FAIL}  database — {exc}")
        console.print("        Delete the file to start fresh, or check permissions.")
        db = None

    # Broker
    try:
        from shani.brokers.registry import build_registry

        registry = build_registry(config, db) if db else None
        if registry:
            live = "LIVE ENABLED" if config.broker.live_enabled else "paper only"
            style = WARN if config.broker.live_enabled else OK
            console.print(f"{style}  broker — {', '.join(registry.names())} ({live})")
    except Exception as exc:
        failures += 1
        console.print(f"{FAIL}  broker — {exc}")

    # Plane A
    if config.tradingview.screener_enabled:
        try:
            from shani.market.screener import ScreenerProvider

            quotes = ScreenerProvider().quotes(config.tradingview.watchlist[:2])
            got = sum(1 for q in quotes if q.last is not None)
            if got:
                console.print(f"{OK}  plane A — market data reachable ({got} quotes)")
            else:
                console.print(f"{WARN}  plane A — reachable but returned no prices")
                console.print("        Markets may be closed, or upstream is rate-limiting.")
        except Exception as exc:
            console.print(f"{WARN}  plane A — {str(exc)[:100]}")
            console.print("        Non-fatal. Journal and paper broker work without it.")
    else:
        console.print(f"{OK}  plane A — disabled in config")

    # Plane B
    if config.tradingview.desktop_enabled:
        from shani.market.tradingview_cdp import TradingViewDesktop

        client = TradingViewDesktop(
            host=config.tradingview.cdp_host,
            port=config.tradingview.cdp_port,
            timeout=config.tradingview.cdp_timeout_seconds,
        )
        healthy, detail = asyncio.run(client.check())
        if healthy:
            console.print(f"{OK}  plane B — TradingView Desktop {detail}")
        else:
            console.print(f"{FAIL}  plane B — {detail}")
            console.print(
                f"        Quit TradingView, then relaunch it with "
                f"--remote-debugging-port={config.tradingview.cdp_port}"
            )
            console.print("        See docs/tradingview-setup.md.")
            failures += 1
    else:
        console.print(f"{OK}  plane B — disabled (enable for chart context capture)")

    # Plane C
    if config.tradingview.webhook_enabled:
        if config.tradingview.webhook_secret:
            console.print(f"{OK}  plane C — webhook configured")
        else:
            console.print(f"{FAIL}  plane C — no webhook secret set")
            console.print("        Unsigned payloads are rejected, so alerts cannot arrive.")
            console.print("        Set SHANI_TRADINGVIEW__WEBHOOK_SECRET in .env.")
            failures += 1
    else:
        console.print(f"{OK}  plane C — disabled in config")

    # Model
    try:
        from shani.agent.llm import build_llm

        healthy, detail = build_llm(config.model).check()
        console.print(f"{OK if healthy else WARN}  model — {detail}")
    except Exception as exc:
        console.print(f"{WARN}  model — {str(exc)[:100]}")

    # Risk
    risk = config.risk
    state = "ENGAGED" if risk.kill_switch else "off"
    console.print(
        f"{WARN if risk.kill_switch else OK}  risk — kill switch {state}, "
        f"max daily loss ${risk.max_daily_loss:,.0f}, "
        f"max {risk.max_position_contracts} contracts"
    )

    console.print("─" * 62)
    if failures:
        console.print(f"[red]{failures} problem(s) found.[/red]\n")
        raise typer.Exit(1)
    console.print("[green]ready.[/green]\n")


@app.command()
def demo(
    trades: Annotated[int, typer.Option(help="How many synthetic trades to seed.")] = 60,
) -> None:
    """Seed synthetic history so the portal has something to show.

    A dashboard with no data teaches nothing about whether the dashboard is any
    good, and waiting months to find out is not a reasonable onboarding.

    The generated history has a deliberate shape: the lunch session loses money
    and the opening drive makes it. That is both realistic and useful — it gives
    the statistics something true to find, so you can confirm the analysis works
    before trusting it on your own trades.
    """
    import random

    from shani.db import Database
    from shani.memory.playbook import Playbook
    from shani.models import InterviewAnswer, SetupCard, Side, Trade
    from shani.sessions import EASTERN, Session, TimeOfDay

    config = load_config()
    db = Database(config.db_path)
    playbook = Playbook(db)
    random.seed(20260809)

    card = playbook.create(SetupCard(
        name="Opening drive continuation",
        slug="opening-drive-continuation",
        description="Join the first sustained push after the cash open.",
        trigger="Price breaks the opening range high and the first pullback holds VWAP.",
        context="RTH only, first hour, on a day that gaps in the direction of the trend.",
        invalidation="A close back inside the opening range.",
        management="Stop below the pullback low, target the measured move.",
        instruments=["ES", "NQ"], timeframes=["5m", "15m"],
    ))

    profiles = [
        (TimeOfDay.OPENING_DRIVE, Session.RTH, 9, 45, 0.62, 1.4),
        (TimeOfDay.LATE_MORNING, Session.RTH, 11, 0, 0.50, 0.9),
        (TimeOfDay.LUNCH, Session.RTH, 12, 30, 0.32, 0.6),   # the money pit
        (TimeOfDay.AFTERNOON, Session.RTH, 14, 0, 0.54, 1.1),
        (TimeOfDay.ASIA, Session.OVERNIGHT, 21, 0, 0.45, 0.8),
    ]
    answers = [
        "Broke the opening range and the pullback held. Took the continuation.",
        "Was bored and it looked like it was going. Not a real setup.",
        "Failed auction at the overnight high, absorption on the tape.",
        "Revenge trade after the last one stopped me out.",
        "Clean trend day, joined the third push.",
    ]

    created = 0
    for _ in range(trades):
        tod, session, hour, minute, win_rate, r_scale = random.choice(profiles)
        symbol = random.choice(["ES", "ES", "NQ", "CL"])
        instrument_risk = Decimal("250")
        won = random.random() < win_rate
        r = random.uniform(0.6, 2.4) * r_scale if won else -random.uniform(0.6, 1.1)
        pnl = (instrument_risk * Decimal(str(round(r, 2)))).quantize(Decimal("0.01"))

        entry_at = (
            datetime.now(EASTERN).replace(hour=hour, minute=minute, second=0, microsecond=0)
            - timedelta(days=random.randint(1, 90))
        )
        followed = tod is TimeOfDay.OPENING_DRIVE and random.random() < 0.75

        trade = Trade(
            symbol=symbol, side=random.choice([Side.BUY, Side.SELL]), quantity=1,
            entry_price=Decimal("5000"), exit_price=Decimal("5004"),
            entry_at=entry_at.astimezone(UTC),
            exit_at=(entry_at + timedelta(minutes=random.randint(4, 90))).astimezone(UTC),
            gross_pnl=pnl, commission=Decimal("5.00"),
            planned_risk=instrument_risk, session=session, time_of_day=tod,
            followed_playbook=followed,
            setup_card_id=card.id if followed else None,
            interview=[InterviewAnswer(
                question="What did you see that made you take this trade?",
                answer=random.choice(answers),
                answered_at=entry_at.astimezone(UTC) + timedelta(minutes=3),
            )],
        )
        db.trades.save(trade)
        if followed:
            card.trade_ids.append(trade.id)
        created += 1

    db.setups.save(card)

    from shani.memory.stats import compute_stats

    stats = compute_stats(db)
    worst = stats.worst_time_of_day

    table = Table(title=f"Seeded {created} synthetic trades", show_header=True)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Net P&L", f"${stats.net_pnl:,.2f}")
    table.add_row("Win rate", f"{stats.win_rate:.1%}" if stats.win_rate else "n/a")
    table.add_row("Expectancy", f"${stats.expectancy:,.2f}" if stats.expectancy else "n/a")
    table.add_row("Max drawdown", f"${stats.max_drawdown:,.2f}")
    if worst:
        table.add_row("Worst time of day", f"{worst.label} (${worst.net_pnl:,.2f})")
    console.print(table)
    console.print(
        "\n[dim]This is synthetic data for exercising the portal. "
        "Delete the database to clear it.[/dim]"
    )
    console.print("Next: [bold]shani serve[/bold]\n")


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Bind address.")] = None,
    port: Annotated[int | None, typer.Option(help="Port.")] = None,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes.")] = False,
) -> None:
    """Run the API server."""
    import uvicorn

    config = load_config()
    bind_host = host or config.server.host
    bind_port = port or config.server.port

    if bind_host not in {"127.0.0.1", "localhost"}:
        console.print(Panel.fit(
            f"[yellow]Binding {bind_host}, not loopback.[/yellow]\n\n"
            f"Your journal, your positions, and order entry will be reachable from\n"
            f"the network. Only do this behind a tunnel or a firewall you trust.",
            title="warning",
        ))

    console.print(f"\n[bold]Shani[/bold] → http://{bind_host}:{bind_port}")
    console.print(f"[dim]Broker: {config.broker.default} · "
                  f"Live: {'ENABLED' if config.broker.live_enabled else 'disabled'}[/dim]\n")

    uvicorn.run(
        "shani.api.app:build_app", host=bind_host, port=bind_port,
        reload=reload, factory=True,
    )


@app.command()
def verify(
    url: Annotated[str | None, typer.Option(help="Base URL of a running server.")] = None,
) -> None:
    """Walk the full path against a running server and fail loudly.

    ``doctor`` checks that each component *can* work. This checks that they
    actually work *together*, which is a different question and the one that
    kept being answered wrongly: every bug that reached a user in this project's
    first week lived at a seam, while every unit test stayed green.

    Placing a real (paper) trade is the point — it exercises the risk gate, the
    broker, position accounting, the journal, and the statistics in one pass.
    """
    import httpx

    config = load_config()
    base = url or f"http://{config.server.host}:{config.server.port}"
    token = config.server.api_token
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = httpx.Client(base_url=base, headers=headers, timeout=30)

    console.print(f"\n[bold]shani · verify[/bold]  →  {base}")
    console.print("─" * 62)

    failures = 0

    def step(label: str, fn: object) -> object:
        nonlocal failures
        try:
            result = fn()  # type: ignore[operator]
        except Exception as exc:
            failures += 1
            console.print(f"{FAIL}  {label}")
            console.print(f"        {str(exc)[:150]}")
            return None
        console.print(f"{OK}  {label}")
        return result

    def reachable() -> str:
        r = client.get("/api/health")
        r.raise_for_status()
        return str(r.json()["broker"])

    broker = step("server reachable", reachable)
    if broker is None:
        console.print("─" * 62)
        console.print("[red]Server is not answering. Start it with [bold]shani serve[/bold].[/red]\n")
        raise typer.Exit(1)

    def portal_calls() -> int:
        paths = ["/api/watchlist", "/api/account", "/api/positions", "/api/trades",
                 "/api/stats", "/api/equity", "/api/playbook", "/api/evaluation",
                 "/api/settings/model"]
        bad = [(p, client.get(p).status_code) for p in paths]
        broken = [(p, c) for p, c in bad if c != 200]
        if broken:
            raise RuntimeError(f"failing endpoints: {broken}")
        return len(paths)

    step("every portal endpoint answers", portal_calls)

    def round_trip() -> str:
        """A real paper trade, opened and closed.

        The expected P&L is derived from the *actual* fill price rather than
        hardcoded, because slippage and commission are configurable. A check
        that only passes on the author's settings is not a check.
        """
        from decimal import Decimal

        from shani.instruments import get_instrument

        px = Decimal("5000.00")
        target = px + 20
        client.post("/api/price", json={"symbol": "ES", "price": str(px)})
        order = client.post("/api/orders", json={
            "symbol": "ES", "side": "buy", "quantity": 1,
            "stop_loss": str(px - 10), "take_profit": str(target),
        })
        if order.status_code != 201:
            raise RuntimeError(f"order rejected: {order.json().get('detail')}")

        fill = order.json().get("average_fill_price")
        if fill is None:
            raise RuntimeError("order reported no fill price")

        client.post("/api/price", json={"symbol": "ES", "price": str(target)})
        if client.get("/api/positions").json():
            raise RuntimeError("position did not close when price reached the target")

        trade = client.get("/api/trades", params={"limit": 1}).json()[0]
        es = get_instrument("ES")
        expected_gross = es.pnl(Decimal(fill), target, 1, is_long=True)
        expected_net = expected_gross - es.commission(1)

        if Decimal(trade["gross_pnl"]) != expected_gross:
            raise RuntimeError(
                f"gross P&L wrong: filled {fill}, exited {target}, "
                f"expected {expected_gross}, got {trade['gross_pnl']}"
            )
        if Decimal(trade["net_pnl"]) != expected_net:
            raise RuntimeError(
                f"net P&L wrong: expected {expected_net}, got {trade['net_pnl']}"
            )
        return str(trade["id"])

    trade_id = step("paper trade: fill → target → close, P&L exact", round_trip)

    if trade_id:
        def interview() -> bool:
            r = client.post(f"/api/trades/{trade_id}/interview",
                            json={"index": 0, "answer": "shani verify"})
            r.raise_for_status()
            return bool(r.json()["has_interview"])

        step("interview attaches and records", interview)

    def risk_gate() -> str:
        r = client.post("/api/orders", json={
            "symbol": "ES", "side": "buy", "quantity": 1,
        })
        if r.status_code != 422:
            raise RuntimeError(f"a stopless entry was not refused (got {r.status_code})")
        return str(r.json()["detail"]["rule"])

    step("risk gate refuses a stopless entry", risk_gate)

    def money_types() -> str:
        body = client.get("/api/account").json()
        floats = [k for k, v in body.items() if isinstance(v, float)]
        if floats:
            raise RuntimeError(f"money serialised as float: {floats}")
        return "all strings"

    step("money never crosses the wire as a float", money_types)

    def model() -> str:
        r = client.post("/api/settings/model/test")
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("detail", "model not reachable"))
        return str(body["detail"])

    if config.model.enabled:
        step("model provider responds", model)
    else:
        console.print(f"{OK}  model provider — disabled in config")

    console.print("─" * 62)
    if failures:
        console.print(f"[red]{failures} check(s) failed.[/red]\n")
        raise typer.Exit(1)
    console.print("[green]every seam healthy.[/green]\n")


@app.command()
def stats() -> None:
    """Print performance statistics to the terminal."""
    from shani.db import Database
    from shani.memory.stats import compute_stats, evaluate_playbook

    config = load_config()
    db = Database(config.db_path)
    s = compute_stats(db)

    if s.total_trades == 0:
        console.print("[yellow]No closed trades yet.[/yellow] Try [bold]shani demo[/bold].")
        raise typer.Exit()

    table = Table(title="Performance", show_header=False)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Trades", str(s.total_trades))
    table.add_row("Win rate", f"{s.win_rate:.1%}" if s.win_rate else "n/a")
    table.add_row("Net P&L", f"${s.net_pnl:,.2f}")
    table.add_row("Expectancy", f"${s.expectancy:,.2f}" if s.expectancy else "n/a")
    table.add_row("Average R", f"{s.avg_r:+.2f}" if s.avg_r else "n/a")
    table.add_row("Profit factor", f"{s.profit_factor:.2f}" if s.profit_factor else "n/a")
    table.add_row("Max drawdown", f"${s.max_drawdown:,.2f}")
    console.print(table)

    if s.by_time_of_day:
        tod = Table(title="By time of day")
        tod.add_column("Session")
        tod.add_column("Trades", justify="right")
        tod.add_column("Win rate", justify="right")
        tod.add_column("Net P&L", justify="right")
        for row in s.by_time_of_day:
            colour = "green" if row.net_pnl >= 0 else "red"
            tod.add_row(
                row.label, str(row.trades),
                f"{row.win_rate:.0%}" if row.win_rate else "n/a",
                f"[{colour}]${row.net_pnl:,.2f}[/{colour}]",
            )
        console.print(tod)

    console.print(f"\n[bold]Playbook check:[/bold] {evaluate_playbook(db).verdict()}\n")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
