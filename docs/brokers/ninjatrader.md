# NinjaTrader

> [!WARNING]
> **Not implemented.** `shani/brokers/ninjatrader.py` is a documented stub that
> raises. No NinjaTrader instance was available during development, so nothing
> here has been verified against a real NT8 installation. This page describes
> the two viable routes so the work can be done properly rather than guessed at.

Shani's broker interface is one protocol — `submit`, `cancel`, `position`,
`positions`, `open_orders`, `account`, `on_price` — so an NT8 adapter is a
self-contained piece of work. See [`shani/brokers/base.py`](../../shani/brokers/base.py)
and [`paper.py`](../../shani/brokers/paper.py) for the reference implementation.

## Route 1 — the ATI (free, native, awkward)

NinjaTrader's built-in **Automated Trading Interface** accepts instructions two
ways:

- **OIF files** — drop a text file into `Documents/NinjaTrader 8/incoming/`.
  One instruction per file, fire-and-forget.
- **A DLL / socket interface** — `NTDirect.dll`, the older Windows-native path.

```
PLACE;<account>;<instrument>;<action>;<qty>;<order type>;<limit>;<stop>;<tif>;<oco>;<name>;<strategy>;<template>
```

**Pros:** free, ships with the platform, no third party.

**Cons:** fire-and-forget. There is no clean acknowledgement channel — you learn
about fills by reading NinjaTrader's exported trade files or by watching the
platform. For a system whose entire premise is capturing trade context at the
moment of execution, a write-only interface is a significant problem: Shani would
know it *asked* for a trade without reliably knowing what happened.

Anyone implementing this should solve the read path first, not the write path.

## Route 2 — CrossTrade REST (clean, paid, third party)

[CrossTrade](https://crosstrade.io) is an NT8 add-on exposing a proper REST API:
submit orders, and pull account state, positions, unrealised P&L, and working
orders in one call.

**Pros:** a real request/response API, remotely reachable, and — critically for
Shani — a read path that actually reports fills.

**Cons:** a paid third-party service, and another dependency between you and your
orders.

## Requirements either way

- NT8 running on **Windows**, on the same machine or reachable from it.
- ATI enabled: *Tools → Options → Automated trading interface*.
- A configured account (Sim101 for simulated; a funded account for live).

## Notes for whoever implements this

Four things matter more than the transport:

1. **Fills, not acknowledgements.** The adapter must report what actually
   executed. A trade Shani thinks happened but did not — or vice versa — corrupts
   the journal, and the journal is the whole product.
2. **Contract rollover.** NT8 reports the dated contract (`ESZ5`); TradingView
   charts the continuous one (`ES1!`). `Trade.contract` exists for exactly this.
   Pooling statistics across a rollover without it goes wrong every quarter.
3. **Tick values come from Shani, not NT8.** `shani/instruments.py` is the single
   source of truth, and it is independently verified against exchange spec sheets
   in the test suite.
4. **Live stays gated.** An NT8 adapter is a *live* venue, so it will not be
   registered at all unless `allow_live` and the confirmation phrase are both
   set. See [safety.md](../safety.md). Please do not weaken that to make
   development easier — build against the paper broker instead.

## Trade capture without execution

Worth considering as a first step: NT8 export is a *read-only* integration, and
most of Shani's value is in the journal rather than the order routing.

An adapter that only watches NT8's trade export and reconciles fills into the
journal would let someone keep trading exactly as they do today while still
getting the interview, the playbook, and the statistics. Lower risk, no order
routing, and it delivers the part that actually compounds.
