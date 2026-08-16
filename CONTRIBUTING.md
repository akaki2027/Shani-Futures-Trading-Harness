# Contributing

Contributions welcome. Shani is alpha and there is plenty to do.

## Setup

```bash
git clone https://github.com/akaki2027/shani.git
cd shani
uv sync
cp .env.example .env
uv run shani init
uv run pytest
```

Portal:

```bash
cd portal && npm install && npm run dev
```

## Before opening a PR

```bash
uv run ruff check shani tests
uv run mypy shani
uv run pytest
cd portal && npm run typecheck && npm run build
```

## Rules specific to this project

These are not style preferences. Each exists because breaking it causes a bug
that does not announce itself.

### 1. Never relax the dependency pins without reading the comment above them

`tradingview-screener` is pinned to exactly `3.0.0`. From 3.2.0 it injects a
default equity preset that makes futures queries return **zero rows silently** —
no error, no warning, just an empty watchlist. `mcp[cli]` is capped below 2.0
for a similar reason.

If you have a genuine need to move a pin, fix the underlying incompatibility in
the same PR and update the comment.

### 2. Contract specs need an independent source

Adding an instrument means adding a row to `EXCHANGE_SPECS` in
`tests/test_instruments.py` — transcribed from the exchange's published contract
specification, **not** by running the code and pasting the output.

A test that derives its expectation from the code under test verifies only that
the code is self-consistent, which it always is. The point of that table is to
catch a typo, not confirm one.

### 3. Money is `Decimal`, never `float`

Prices are exact multiples of a tick and ticks are decimal fractions. Floats
drift, and the drift shows up as dollar figures that are subtly, confidently
wrong. Ratios and statistics may be `float`; anything representing money or a
price may not.

The API serialises money as **strings** for the same reason — JSON numbers are
IEEE doubles.

### 4. Do not weaken the live-trading gate

Live venues are unregistered, not guarded, and enabling them requires both a
flag and an exact confirmation phrase. If that is inconvenient during
development, build against the paper broker — which is the point of it.

See [docs/safety.md](docs/safety.md).

### 5. Changes that cross a boundary need a seam test

`tests/test_integration.py` exists because of a specific, repeated failure: this
project's first week produced a string of bugs that all lived at boundaries
between components, while every unit test stayed green the entire time.

Config didn't override from the environment. The portal called a path the API
didn't serve. The API client read a response body twice and replaced every real
error with a misleading one. A key saved to `.env` was never read back. The
TradingView client used a global that doesn't exist on the page Desktop loads.

Not one of those was subtle, and not one was catchable by a suite that only ever
exercises a single module.

**The rule: if a failure could make the portal show wrong or empty data while
every unit test stays green, it needs a test in `test_integration.py`.**

Before opening a PR that touches the API, the portal client, or config:

```bash
uv run pytest tests/test_integration.py
uv run shani verify        # against a running server
```

### 6. Tests must not depend on the network, the clock, or market hours

The paper broker takes time and price as arguments precisely so tests can replay
an exact sequence. Tests that hit live endpoints or need TradingView Desktop go
behind the `network` and `desktop` markers, which are excluded by default.

A suite that fails at 3am on a Sunday because the market is shut is a suite
people stop running.

## Where help is most useful

- **A NinjaTrader adapter.** See [docs/brokers/ninjatrader.md](docs/brokers/ninjatrader.md).
  The read-only trade-capture version is the higher-value, lower-risk half.
- **Plane B verification.** It is written but unverified against a live
  TradingView Desktop. Reports of what actually breaks are valuable.
- **More instruments.** Rule 2 applies.
- **Holiday calendars.** `shani/sessions.py` needs extending each year, and the
  early-close table is easy to get subtly wrong.
- **Setup-card validation.** Walk-forward backtesting a learned card before the
  agent presents it as an edge is designed for and not built.

## Things to be careful about

This project touches people's money. Two habits matter more than usual:

**Prefer refusing to guessing.** An unknown instrument raises rather than
defaulting to a guessed tick size, because a wrong tick value produces
plausible-looking figures that are silently off by a constant factor. Follow
that pattern.

**Do not make the tool sound more confident than the data.** Sample sizes are
attached to every statistic and small ones are labelled provisional. If you add
a number to the portal, add its denominator too.

## Commits and PRs

Explain *why*, not just what. If you fixed a bug, say what it would have caused
— the commit history here is meant to be readable by someone deciding whether to
trust the code with an account.
