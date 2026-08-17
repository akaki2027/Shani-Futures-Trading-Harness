# Importing TradingView trades into Shani

**Status: built and running.** `shani import`, or `POST /api/trades/import`.

This page is kept as the record of the spike that made it possible. Where the
finished importer diverged from the plan below, the plan was wrong:

- **It reads the broker object, not the Account Manager grid.** The plan was to
  click the Order history tab and scrape rows. `_activeBroker.allExecutions()`
  and `.ordersHistory()` return structured objects with real numbers and epoch
  milliseconds, need no tab to be active, and make the virtualised-grid problem
  and every timezone question disappear.
- **Pairing must be per symbol.** The first attempt ran one position across
  every symbol in the account and produced round trips wrong by orders of
  magnitude, without raising anything. See `HANDOFF.md`.
- **Correctness was established by reconciliation**, not by tests: total imported
  P&L reproduces TradingView's own realized P&L for the account to the cent.

The original spike follows.

---

This page records a spike that overturned an earlier conclusion. Shani's docs
previously said trades placed in TradingView could not reach the journal. That
was wrong, and it was wrong because nobody had checked — the claim was inferred
from "TradingView has no public trading API", which is true and turns out not to
be the relevant fact.

The relevant fact is that TradingView Desktop is an Electron application whose
trading panel keeps its state in ordinary JavaScript objects, and Plane B already
has a verified channel into that page.

## What was verified, live

Against TradingView Desktop 3.3.0 (Microsoft Store build), on a real chart page,
through the existing CDP bridge:

```js
const trading = await window.TradingViewApi.trading();   // resolves
trading._ordersService                                   // exists
trading._positionService                                 // exists
trading._account                                         // account id (string)
trading._activeBroker                                    // broker handle
```

And crucially, the accessors are real and callable:

```js
trading._ordersService.orders()        // → array(0)
trading._ordersService.getCurrency()   // → "USD"
```

`orders()` returned an **empty array, not an error**. That distinction is the
whole finding: the interface is present and working, and it was empty only
because no broker was connected to that TradingView account at probe time.

`_ordersService` also exposes what look like event streams:

```
activeOrdersUpdated
activeOrdersRemoved
orderRejected
```

So this can be **subscribed to** rather than polled, which matters — an event
carries the fill at the moment it happens, and the moment it happens is exactly
when Shani wants to capture chart context and start the interview.

## What is still unknown

Honestly stated, because the next person should not have to rediscover it:

- **The shape of an order or position object.** The arrays were empty, so no
  sample was captured. Field names, price types, and how a partial fill is
  represented are all unverified.
- **Whether executions are readable, or only working orders.** A journal needs
  *fills*, not intentions. `_positionService` is the likely source but its
  accessors were not exercised against real data.
- **Whether the paper-trading account exposes the same interface** as a
  connected live broker. TradingView's paper broker may be a different
  implementation behind the same panel.
- **Symbol mapping.** TradingView will report something like `CME_MINI:ESZ2026`;
  Shani needs the root plus the dated contract. Chart symbols and broker symbols
  are not necessarily the same string.

## To make this work, one thing is needed from the trader

**Connect a broker in TradingView, or enable Paper Trading**, and place at least
one trade. Until an order exists, the services return empty arrays and there is
nothing to map.

TradingView's own Paper Trading account is the sensible way to develop against
this: no money involved, and it populates the same panel.

## Build plan

1. **Capture real shapes.** With one order present, dump
   `_ordersService.orders()` and the `_positionService` accessors verbatim.
   Everything below depends on this and nothing should be guessed.
2. **Extend Plane B**, in `shani/market/tradingview_cdp.py` and nowhere else.
   The one-file rule exists for exactly this: TradingView will change these
   internals, and when they do the fix must be in one place.
3. **Map to `Trade`.** Shani's model already carries `contract` separately from
   `symbol` for the continuous-versus-dated distinction, which is what this
   needs. Tick values and P&L must continue to come from
   `shani/instruments.py`, never from TradingView — one source of truth for
   money.
4. **Reconcile, do not duplicate.** An imported trade needs a stable external id
   so a re-poll updates rather than inserts. This is the single highest-risk part
   of the feature: a journal that double-counts is worse than no journal, because
   every statistic downstream inherits the error.
5. **Subscribe rather than poll**, using `activeOrdersUpdated`. A fill event is
   the trigger for the whole loop — capture the chart, screenshot it, and open
   the interview while the trader still remembers.
6. **Seam tests.** Per `CONTRIBUTING.md`: anything crossing a boundary needs a
   test in `tests/test_integration.py`. Import is a boundary, and a silent
   double-count would pass every unit test.

## Why this is worth doing properly rather than quickly

Shani's premise is that the journal is the curriculum. Every statistic, every
setup card, and every "you have taken this seven times" claim is computed from
the trade table. An importer that duplicates fills, misses partials, or attaches
the wrong contract does not degrade the product gracefully — it silently
poisons the one thing the product is for.

Which is why this page exists instead of a half-finished importer.
