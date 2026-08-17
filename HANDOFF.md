# Where Shani stands

Written at the end of the first build session. This is the state of things and
what to do next, so a fresh session can start working rather than rediscovering.

**Owner:** akaki2027 · **Repo:** local, not yet pushed · **Trades:** futures, mainly MES/ES

---

## What exists and works

Verified by running it, not just by tests passing.

| Area | State |
|---|---|
| Futures paper broker | ES/NQ/CL/GC + micros. Market/limit/stop/stop-limit/bracket OCO, slippage, commission, MAE/MFE, contract rollover. P&L hand-checked against exchange specs |
| Risk gate | Kill switch, daily loss on the 18:00 ET trading day, position and concurrency caps, rate limit, mandatory stop. Refuses rather than warns |
| Live trading | **Structurally unreachable.** Live venues are never registered while `allow_live` is false — asking for one raises |
| Journal + interview | Five questions, attaches on close, desktop notification |
| Playbook | Versioned setup cards from interviews, FTS5 retrieval, sample sizes attached, "is this helping?" comparison that can report failure |
| Plane A | Screener quotes and TA. `tradingview-screener` pinned to 3.0.0 — see below |
| Plane B | CDP into TradingView Desktop. **Verified live** against Desktop 3.3.0 |
| Plane C | Pine webhook, HMAC-verified, fails closed |
| Price charts | Candles + volume, 5m/15m/1h/4h/1d, SMA 20/50, EMA 20, session-anchored VWAP |
| News desk | RSS + Yahoo (no key), Reddit/X connectors, LLM lean per item, per-market reads, colour-coded titles |
| Market drivers | CFTC COT and US Treasury curve, mapped per market by exact contract code |
| Model settings | OpenRouter picker over 411 live models with pricing, two tiers, key write-only |
| Portal | Next.js, dark, tabular figures, themed browser surfaces |
| Verification | `shani doctor` (components) and `shani verify` (seams). 365 tests, ruff + mypy strict clean |

Run it:

```bash
uv run shani doctor && uv run shani verify
uv run shani serve      # api on 8420
cd portal && npm run dev  # portal on 3000
```

---

## What does not work

Stated plainly so nobody rediscovers it as a surprise.

- **CME open interest.** Failing. `/CmeWS/mvc/Quotes/Front` does not exist;
  `Volume/Details` answers HTTP 200 with a body of zeros. It fails *visibly* in
  `/api/drivers` rather than silently, which is the right failure mode but not a
  fix. Next thing to try is the settlements service with a valid trade date.
- **TradingView trade import.** Not built. Mechanism now understood — see below.
- **NinjaTrader.** Documented stub only.
- **Plane C end to end.** HMAC verified locally; no real inbound alert from
  TradingView's servers has been confirmed. Needs a tunnel.
- **FRED drivers.** Coded and mapped, silently absent until `FRED_API_KEY` is in
  `.env`. Free key: <https://fredaccount.stlouisfed.org/apikeys>
- **Mobile app.** Schema and API are shaped for it; nothing built.

---

## Next goal: import TradingView trades

The feature the owner wants most, and the one with the highest blast radius if
done carelessly — every statistic, setup card and "you have taken this seven
times" claim is computed from the trade table.

### What was established

- The owner's instrument is **`MESU2026`** — Micro E-mini S&P 500, Sep 2026 →
  Shani's `MES`. Contract specs already exist.
- Correct CDP target is the `/chart/...` page where the account manager is
  mounted. Desktop exposes ~12 targets including internal `file://` shells; only
  one has the panel.
- `TradingViewApi.trading()` resolves and exposes `_activeBroker`,
  `_ordersService`, `_positionService`, `_account`.
- `_activeBroker` has **`subscribeExecutions` / `unsubscribeExecutions`** — the
  fill event stream, and the right trigger for the whole loop.
- **`_orders` and `_individualPositions` are live state, not history.** They are
  legitimately empty when nothing is resting or open. Closed trades live in the
  **Order history** tab.

### The root cause of every failed read

**TradingView's grids are virtualised — only the active tab is rendered.** If
Order history is not the selected tab when you read, its rows do not exist in
the DOM at all, regardless of what is in the account. This is why probe results
flip-flopped: each read hit whatever tab happened to be up. An early attempt to
find "the biggest repeating row group" returned the *watchlist* (DJI, SPX, VIX)
rather than any trade.

### Build order

1. **Activate the tab, then read.** Click Order history, wait for rows, read,
   then restore the tab the user had selected. Do not leave their UI changed.
2. **Dump one real row verbatim** before writing any mapping. Field names, price
   types and partial-fill representation are all still unverified. Do not guess.
3. **Extend Plane B only**, in `shani/market/tradingview_cdp.py`. The one-file
   rule exists because TradingView will change these internals.
4. **Map to `Trade`.** `Trade.contract` already exists for the dated-versus-
   continuous distinction (`MESU2026` vs `MES`). Tick values and P&L must keep
   coming from `shani/instruments.py` — one source of truth for money.
5. **Reconcile, never duplicate.** Needs a stable external id so a re-read
   updates rather than inserts. Highest-risk part of the feature.
6. **Then subscribe** via `subscribeExecutions` for real time, so a fill triggers
   chart capture, screenshot, and the interview while it is fresh.
7. **Seam tests.** Import is a boundary; a silent double-count would pass every
   unit test. See `CONTRIBUTING.md` rule 5.

### Opening move for the next session

> build the TradingView importer — click Order history, read the grid, map
> MESU2026 to MES

---

## Then, in rough priority

1. **Fix CME open interest.** OI distinguishes new money committing from an
   unwind; they look identical on a chart.
2. **FRED key** — one paste, lights up VIX, breakevens and the dollar.
3. **Plane C end to end** — Cloudflare tunnel, one real Pine alert.
4. **NinjaTrader read-only capture** — lower value now that TradingView import
   is understood, but still the path for anyone trading through NT8.
5. **Push to GitHub.** Repo is clean: no `.env` or database tracked, CI fails the
   build if either ever is.

---

## Things worth not relearning

- **`tradingview-screener` must stay `==3.0.0`.** 3.2+ injects a default equity
  preset that makes futures queries return zero rows *silently*.
- **TradingView Desktop from the Microsoft Store** has no `TradingView.exe` under
  `%LOCALAPPDATA%`. Launch it from the package path via `Get-AppxPackage`, and
  quit it fully first or the flag is ignored.
- **`window.tvWidget` does not exist** on tradingview.com — that global belongs
  to the embeddable Charting Library. The application uses
  `window.TradingViewApi`. Most published guides get this wrong.
- **Never edit source with PowerShell `Set-Content`.** It re-encodes and turns
  every em dash into mojibake. Cost 24 corrupted characters in `drivers.py`.
- **Every bug that reached the user lived at a seam**, while unit tests stayed
  green throughout. That is why `tests/test_integration.py` and `shani verify`
  exist. Add to them when changing a boundary.
