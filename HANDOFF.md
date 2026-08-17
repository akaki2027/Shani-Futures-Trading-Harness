# Where Shani stands

Written at the end of the first build session. This is the state of things and
what to do next, so a fresh session can start working rather than rediscovering.

**Owner:** akaki2027 · **Repo:** <https://github.com/akaki2027/Shani-Futures-Trading-Harness>
(public) · **Trades:** futures, mainly MES/ES

The repository is public, which is a standing constraint rather than a fact
about one day. Anything that works only because of the author's local state —
his database, his keys, his TradingView session — is a defect, not a shortcut.
Judge a change by what it does on a stranger's fresh clone with no data and no
keys.

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
| Trade import | **Built and run.** `shani import` reads the TradingView account and writes round trips. Reconciled to the cent against the account's own realized P&L |
| The journal | **Real trades only.** The 60 demo rows and 6 `shani verify` artefacts were deleted on 2026-08-17; what remains is 25 imported trades. Stats are honest: net $4,717.50, 43.5% win rate, expectancy $188.70 |
| Verification | `shani doctor` (components) and `shani verify` (seams). 390 tests, ruff + mypy strict clean |

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
- **Real-time fill capture.** Import is a pull, run by hand. `subscribeExecutions`
  on the broker object is the event stream that would make a fill trigger the
  screenshot and interview automatically, while it is fresh. Not wired up.
- **NinjaTrader.** Documented stub only.
- **Plane C end to end.** HMAC verified locally; no real inbound alert from
  TradingView's servers has been confirmed. Needs a tunnel.
- **FRED drivers.** Coded and mapped, silently absent until `FRED_API_KEY` is in
  `.env`. Free key: <https://fredaccount.stlouisfed.org/apikeys>
- **Mobile app.** Schema and API are shaped for it; nothing built.

---

## The trade importer, as built

`shani import` — add `--dry-run` to see it without writing. Also
`POST /api/trades/import`.

Against the owner's account it reads 56 fills and 97 orders, produces 25 round
trips, and skips 2 (see below).

### Do not go back to the DOM

The previous session concluded the fix was to click the Order history tab and
scrape the grid. **That works but is the wrong door.** `_activeBroker` exposes
`allExecutions()`, `ordersHistory()` and `currentAccount()`, which return
structured objects — real numbers, epoch milliseconds, stable ids — and need no
tab to be active at all. The virtualised-grid problem simply does not arise, and
neither does parsing `"7,801.75"` or guessing which timezone
`"2026-08-17 09:50:03"` was rendered in. A seam test now fails if any of those
expressions reaches for `querySelector`.

The DOM was still worth reading once: its tab counts (All 97, Filled 56,
Cancelled 40, Rejected 1) are what confirmed the numeric status codes.

### The bug that mattered

The first pairing implementation ran **one position across every symbol**. The
account holds MES near 7,700, MNQ near 29,000, SPY near 765 — so positions never
closed against their own instrument, and it produced round trips with an entry
of 4,234.945 and a P&L of **-$346,879** on an account that had made $4,722.78.
Nothing threw. Pair per symbol; prices from two instruments must never meet in
the same subtraction. `test_symbols_are_paired_independently` pins it.

### How it was verified

Not by the tests passing. The algorithm was run against the real account and its
total realized P&L reproduced TradingView's own figure for that account —
**$4,722.78** — exactly, across 25 round trips in 5 symbols. That is the check
worth repeating after any change to the pairing.

Then imported twice: `25 new, 0 updated` followed by `0 new, 25 updated`, with
all 66 pre-existing interviews intact.

### Decisions worth knowing before changing it

- **Round trips are flat-to-flat, per symbol.** Scale-ins and scale-outs collapse
  into one trade with size-weighted prices. A single fill that reverses a
  position is split into a close and a new open.
- **Ids are derived, not looked up.** `trade_uuid(external_id)` is a `uuid5`, so
  a re-import lands on the same row. Duplicates are impossible by construction
  rather than prevented by a check someone can forget. Changing that namespace
  re-imports the entire history as new rows.
- **Re-import merges, never replaces.** `PRESERVED_ON_REIMPORT` lists what is
  carried across — interview, notes, tags, setup card, screenshot. The venue can
  always be re-read; what the trader said cannot.
- **Commission is `None`, not zero.** The paper account charges nothing.
  Synthesising a plausible commission would make imported P&L disagree with the
  number TradingView shows the owner.
- **SPY and SPXX are skipped, loudly.** Shani prices futures from a contract spec
  and will not invent a multiplier. Consequence: imported P&L is $4,717.50, short
  of the account's $4,722.78 by exactly the equities. That gap is expected, and
  the CLI says so rather than letting it look like a discrepancy.
- **Brackets are matched heuristically, and decline when ambiguous.** 20 of 25
  trades get a stop and therefore an R. The other 5 are genuine: two had no
  bracket at all, three were fired within six minutes at nearly identical prices
  with overlapping brackets. An unknown R is `None` and is excluded from
  statistics; a guessed one would be averaged in as fact.

---

## Then, in rough priority

1. **Real-time capture via `subscribeExecutions`.** The natural next step now
   that fills parse cleanly: a fill fires the event, Shani grabs the chart and
   screenshot and opens the interview while the trade is still fresh. Import
   already proves the data shape; this only changes the trigger.
2. **Fix CME open interest.** OI distinguishes new money committing from an
   unwind; they look identical on a chart.
3. **FRED key** — one paste, lights up VIX, breakevens and the dollar.
4. **Plane C end to end** — Cloudflare tunnel, one real Pine alert.
5. **NinjaTrader read-only capture** — lower value now that TradingView import
   is understood, but still the path for anyone trading through NT8.
6. **A first-run path for people who pull it.** The repo is public now. `shani
   init` exists, but nobody has walked the clone → keys → first chart route from
   scratch to see where it breaks.

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
- **TradingView writes contract years with four digits** in the account manager
  (`CME_MINI:MESU2026`) while its charts use `MES1!`. `root_of` parses 4-, 2- and
  1-digit years, widest first, or `MESU2026` reads as root `MESU2` and raises.
- **An order's `closeDate` is not its fill time** — it trails by about 11 seconds
  on a limit entry, so matching the two for equality silently finds nothing.
- **A bracket is placed when the entry is *submitted*, not when it fills.** A
  resting limit entry can sit for minutes first; anchoring the bracket search on
  the fill time appears to work on market entries and misses every limit one.
- **The console commands need a UTF-8 stdout.** `shani doctor`, `verify` and
  `import` all print box-drawing characters, and piping them anywhere on Windows
  raises `UnicodeEncodeError` under the cp1252 default. Fine in Windows Terminal;
  set `PYTHONIOENCODING=utf-8` when redirecting. Pre-existing, not yet fixed.
- **Every bug that reached the user lived at a seam**, while unit tests stayed
  green throughout. That is why `tests/test_integration.py` and `shani verify`
  exist. Add to them when changing a boundary.
