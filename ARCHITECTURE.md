# Architecture

```
                      TradingView
        ┌──────────────────┼──────────────────┐
   Plane A            Plane B            Plane C
   screener API     Desktop (CDP)     Pine → webhook
        │                  │                  │
        └──────────────────┼──────────────────┘
                           ▼
    ┌──────────────────────────────────────────────┐
    │                   Signal                     │
    │                     ▼                        │
    │        Agent ──── Playbook (FTS5)            │  ← your own history
    │                     ▼                        │
    │                  Proposal                    │
    │                     ▼                        │
    │              Risk gate ── Audit log          │
    │                     ▼                        │
    │              Broker (paper)                  │
    │                     ▼                        │
    │              Fill → Trade                    │
    │                     ▼                        │
    │        Interview ── Extraction ──┐           │
    └──────────────────────────────────┼───────────┘
                                       │
                        back into the playbook
```

Everything above sits behind a FastAPI service. The portal is a client of that
service and holds no business logic.

## Layers

| Module | Responsibility |
|---|---|
| `instruments.py` | Contract specs. Every dollar figure derives from here |
| `sessions.py` | RTH/overnight/closed, time-of-day, the 18:00 ET trading-day boundary |
| `models.py` | Domain records. UUID keys, `updated_at`, tombstones |
| `db.py` | SQLite. JSON as source of truth, promoted columns as indexes, FTS5 |
| `market/screener.py` | Plane A |
| `market/tradingview_cdp.py` | Plane B. **All** TradingView page coupling |
| `ingest/webhook.py` | Plane C |
| `brokers/` | Execution venues behind one protocol |
| `risk/policy.py` | The gate every order passes |
| `audit.py` | Append-only decision log |
| `memory/` | Playbook, retrieval, statistics, evaluation |
| `agent/` | LLM providers, propose, interview, extract |
| `api/` | HTTP surface |
| `portal/` | Next.js client |

## Decisions worth knowing

**Money is `Decimal`.** Futures prices are exact tick multiples; floats drift.
The API serialises money as strings because JSON numbers are IEEE doubles.

**`tick_value` is derived, never stored.** `tick_size × multiplier` always holds,
and storing both invites them to disagree. The test suite checks the derived
value against an independently transcribed exchange table.

**The paper broker owns no clock and no feed.** Time and price arrive through
`on_price()`. That single decision makes the simulator deterministic, so tests
replay exact sequences with no sleeping, no network, and no dependence on market
hours.

**Live venues are unregistered, not guarded.** While `allow_live` is false they
are never constructed, so there is no boolean deep in the order path that a
refactor could invert. See [docs/safety.md](docs/safety.md).

**All TradingView internals live in one file.** Their page API is undocumented
and will change; when it does, exactly one file needs fixing.

**The database is JSON plus promoted columns.** A field must be promoted to a
real column before it can be queried efficiently — a small, local change.
Migrating a normalised schema on every model edit during alpha is not.

**Sync-shaped from day one.** UUID keys, `updated_at`, and tombstones on every
record, with `/api/changes` already implemented. Retrofitting that onto a
populated journal is genuinely painful; adopting it now costs nothing. See the
mobile section of the plan.

**Two model tiers.** Cheap for per-signal triage, strong for extraction — a
badly-extracted setup card poisons the playbook for months. That boundary is
also where an on-device model would slot in for a phone client.

## The flow, concretely

1. A Pine alert POSTs to `/webhook/tradingview`. HMAC-verified, size-capped,
   unknown instruments rejected. Becomes a `Signal`.
2. `Playbook.recall()` finds matching setup cards and past trades.
3. The agent produces a `Proposal` with that history in the prompt, citing the
   cards it used. Citing nothing marks it ungrounded.
4. `RiskPolicy.evaluate()` approves or refuses. Either way it is logged.
5. The broker fills. A `Trade` opens, capturing session, time of day, and chart
   context.
6. On close, a notification fires and the interview opens.
7. Answers are extracted into a versioned `SetupCard`, indexed in FTS5.
8. The next matching signal retrieves it — and step 3 now has your own numbers
   in it.
