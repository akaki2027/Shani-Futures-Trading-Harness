# Where Shani stands

Written at the end of the first build session. This is the state of things and
what to do next, so a fresh session can start working rather than rediscovering.

**Repo:** <https://github.com/akaki2027/Shani-Futures-Trading-Harness> (public)

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
| Live fill capture | **Built and run.** `shani watch` streams fills over CDP, screenshots the chart at the fill, and opens the interview when a round trip closes |
| The journal | **Real trades only.** The seeded demo rows and `shani verify` artefacts were cleared with `shani demo --clear`; what remains is imported history. Stats are honest |
| Verification | `shani doctor` (components) and `shani verify` (seams). 406 tests, ruff + mypy strict clean |

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

Against a live account it reads the full fill and order history, produces one
round trip per flat-to-flat position, and skips anything it has no contract spec
for (see below).

### Do not go back to the DOM

The previous session concluded the fix was to click the Order history tab and
scrape the grid. **That works but is the wrong door.** `_activeBroker` exposes
`allExecutions()`, `ordersHistory()` and `currentAccount()`, which return
structured objects — real numbers, epoch milliseconds, stable ids — and need no
tab to be active at all. The virtualised-grid problem simply does not arise, and
neither does parsing a thousands-separated price string or guessing which
timezone a rendered timestamp was in. A seam test now fails if any of those
expressions reaches for `querySelector`.

The DOM was still worth reading once: the per-status counts on its tabs are what
confirmed the numeric status codes, by cross-checking them against the counts the
broker object reports.

### The bug that mattered

The first pairing implementation ran **one position across every symbol**. The
account held several instruments at very different price levels — so positions never
closed against their own instrument, and it produced round trips wrong by orders
of magnitude — a six-figure phantom loss on an account that was up four figures.
Nothing threw. Pair per symbol; prices from two instruments must never meet in
the same subtraction. `test_symbols_are_paired_independently` pins it.

### How it was verified

Not by the tests passing. The algorithm was run against the real account and its
total realized P&L reproduced TradingView's own figure for that account —
exactly, across every round trip in five symbols. That is the check worth
repeating after any change to the pairing — run it against your own account.

Then imported twice: every trade new on the first run, every trade *updated* and
none inserted on the second, with all pre-existing interviews intact.

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
- **Equities are skipped, loudly.** Shani prices futures from a contract spec and
  will not invent a multiplier. Consequence: imported P&L falls short of the
  account's own figure by exactly the skipped rows. That gap is expected, and the
  CLI says so rather than letting it look like a discrepancy.
- **Brackets are matched heuristically, and decline when ambiguous.** Most trades
  resolve a stop and therefore an R. The rest are genuine misses: some entries
  carried no bracket at all, and rapid sequences at nearly identical prices have
  overlapping brackets that cannot be attributed. An unknown R is `None` and is
  excluded from statistics; a guessed one would be averaged in as fact.

---

## Live capture, as built

`shani watch`. Leave it running while you trade.

### The two calls, and why both

`subscribeExecutions` is **not** the callback API its name suggests:

```js
subscribeExecutions(e) {
  void 0 !== this._brokerConnection.subscribeExecutions
    && this._brokerConnection.subscribeExecutions(e)
}
```

It takes a *symbol* and no callback — it only asks the connection to start
sending. Fills arrive on `executionUpdate`, a Delegate whose contract is
`subscribe(object, member, singleShot)`. Subscribe without `subscribeExecutions`
and you may listen to a stream nobody is sending; call it without subscribing and
nobody is listening.

Page → Python uses CDP `Runtime.addBinding`, which makes calls to a page function
arrive as `Runtime.bindingCalled` events on a socket held open. A genuine push,
so no fill can fall between two polls.

### Things that will bite

- **The delegate calls every listener.** Subscribing twice reports each fill
  twice, at source, before Python sees it. The injected JS guards on
  `window.__shaniExecHook`; verified live, listeners went 1 → 2 on install and
  stayed at 2 on reinstall. Do not remove that guard.
- **A fill is only a trigger.** Every number comes from re-reading
  `allExecutions()` and re-pairing through `shani.ingest.tradingview`, so live
  capture and `shani import` cannot disagree — they are the same function of the
  same data. Do not be tempted to keep a running position here instead; that is
  two implementations of the pairing and one silent day where they differ.
- **Priming must import first.** On a fresh database nothing is closed, so the
  first fill re-reads the account and reports the entire history as having just
  closed — every historical trade at once, each with an interview and a
  notification. `prime()` now imports and takes that as the starting line.
- **The hook dies on page reload.** `watch_executions` reinstalls on
  `Runtime.executionContextCreated`; without it a trader hitting refresh silently
  stops the capture and the first sign is a missing trade.

---

## Then, in rough priority

1. **Run the watcher from `shani serve`.** Today it is a separate terminal. For
   anyone who pulls the repo, "start the server and it captures your trades"
   is the shape they expect; this needs a supervised background task with the
   reconnect behaviour `watch()` already has.
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
