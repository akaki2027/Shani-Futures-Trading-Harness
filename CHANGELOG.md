# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **TradingView trade import.** `shani import` (and `POST /api/trades/import`)
  reads the connected TradingView account and turns its fills into round trips.
  Reconciled against the account's own realized P&L to the cent before being
  trusted. Re-runnable: the whole history is re-read each time and each round
  trip is keyed on the fill that opened it, so a second run updates rather than
  duplicates, and any interview, notes or tags on an imported trade survive.
- `Trade.external_id`, recording a trade's identity at the venue it came from.

### Fixed

- `root_of` and `parse_contract` now understand four-digit contract years, so
  `CME_MINI:MESU2026` resolves to `MES` instead of raising. TradingView's
  account manager uses this form even though its charts use `MES1!`.

## [0.1.0] — 2026-08-09

First release. Alpha.

### Added

**Core**
- Futures contract specifications for ES, NQ, CL, GC, RTY, YM, NG, SI and their
  micros, with `Decimal` arithmetic throughout and tick values verified against
  independently transcribed exchange specs.
- Timezone-aware session classification (RTH / overnight / closed) anchored to
  `America/New_York`, with time-of-day bucketing and an 18:00 ET trading-day
  boundary.
- SQLite persistence with UUID keys, `updated_at`, tombstones, and FTS5.

**Trading**
- Futures paper broker: market, limit, stop, stop-limit, and bracket (OCO)
  orders; realistic slippage and commission; weighted-average adds, partial
  closes, and position flips; MAE/MFE tracking.
- Risk gate with kill switch, daily loss limit, position and concurrency caps,
  order rate limiting, per-trade risk cap, and a mandatory stop-loss rule.
- Append-only audit log of every signal, proposal, gate decision, and order.

**TradingView**
- Plane A — headless market data, screeners, and multi-timeframe technical
  ratings, with TTL caching.
- Plane B — Chrome DevTools Protocol bridge to TradingView Desktop: chart state,
  OHLCV, screenshots, and Pine editor read/write/compile.
- Plane C — HMAC-verified Pine alert webhook.

**Learning**
- Versioned setup cards extracted from post-trade interviews.
- FTS5 retrieval surfacing matching history at signal time.
- Statistics by time of day, session, and instrument; equity curve, expectancy,
  R-multiples, and drawdown.
- Playbook evaluation comparing on-playbook against off-playbook trades, with an
  explicit observational caveat.

**Interface**
- FastAPI service, loopback by default, bearer auth, delta-sync endpoint.
- CLI: `init`, `doctor`, `demo`, `serve`, `stats`.
- Next.js portal: watchlist, trade ticket, equity curve, time-of-day
  performance, journal, interview, and playbook.
- Cross-platform desktop notifications on trade close.

**Project**
- MIT licensed, with attribution in `NOTICE.md`.
- 337 tests; CI across Linux, macOS, and Windows on Python 3.11 and 3.13.
- Documentation for all three TradingView planes, tunnelling, safety, and the
  learning loop.

### Not included

Stated explicitly rather than left to be discovered:

- **Live broker execution.** Ships disabled; no live adapter is implemented.
- **NinjaTrader.** A documented stub. No NT8 instance was available.
- **Plane B verification.** Written and unit-tested for failure handling, but not
  confirmed against a live TradingView Desktop.
- **Plane C end-to-end.** HMAC verification is tested locally; no real inbound
  alert from TradingView's servers has been confirmed.
- **Setup-card walk-forward validation.** Designed for, not built.
- **Mobile companion.** The schema and API are shaped for it; the app is not
  built.

[0.1.0]: https://github.com/akaki2027/shani/releases/tag/v0.1.0
