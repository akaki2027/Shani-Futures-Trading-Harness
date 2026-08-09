## What this changes

## Why

<!-- Explain the reasoning, not just the diff. If it fixes a bug, say what that
     bug would have caused. -->

## Checks

- [ ] `uv run ruff check shani tests`
- [ ] `uv run mypy shani`
- [ ] `uv run pytest`
- [ ] `cd portal && npm run typecheck && npm run build` (if the portal changed)

## Project rules

<!-- See CONTRIBUTING.md. Tick what applies; delete what does not. -->

- [ ] I did not relax a dependency pin. (If I did, I fixed the underlying
      incompatibility and updated the comment above it.)
- [ ] Any new instrument has a row in `EXCHANGE_SPECS` transcribed from the
      exchange spec sheet, not from this code's output.
- [ ] Money and prices are `Decimal`, and the API serialises them as strings.
- [ ] I did not weaken the live-trading gate.
- [ ] New tests do not depend on the network, the wall clock, or market hours.

## If this touches money

<!-- Order handling, fills, P&L, risk limits, statistics. -->

- [ ] I worked the arithmetic by hand and asserted the literal in a test.
- [ ] I checked the short side as well as the long side.
- [ ] Any new statistic is reported with its sample size.
