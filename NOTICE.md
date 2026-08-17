# Notices and attribution

Shani stands on other people's work. This file records what came from where.

Everything listed below is MIT licensed, which permits commercial use,
modification, and redistribution. The obligation MIT imposes is retention of the
copyright notice — which is what this file is for.

---

## Ideas and architecture

### Hermes Agent — Nous Research

<https://github.com/NousResearch/hermes-agent> · MIT · Copyright (c) Nous Research

Shani's learning loop is a deliberate narrowing of Hermes' design onto a single
domain. Borrowed *concepts* (no code):

- Closing the loop by turning completed work into reusable, versioned procedure
  rather than leaving it as an undifferentiated log.
- Full-text search over past sessions as the retrieval substrate.
- Provider-agnostic model configuration with no vendor lock-in.
- Periodic reinforcement of important context rather than one-shot capture.

Where Hermes generates general-purpose skills from arbitrary task trajectories,
Shani generates *setup cards* from trade trajectories, and nothing else.

---

## Techniques and knowledge

### tradingview-mcp — Atila Ahmettaner

<https://github.com/atilaahmettaner/tradingview-mcp> · MIT · Copyright (c) 2025 Ahmet Taner Atila

No code copied. Borrowed knowledge:

- **The `tradingview-screener==3.0.0` pin and the reason for it.** Versions 3.2+
  inject a default equity preset that makes futures queries return zero rows
  *silently*. This failure mode is documented in that repo's `futures_service.py`
  and would otherwise have cost us a long debugging session. See the annotated
  pin in `pyproject.toml`.
- The futures symbol universe layout (`CME:ES1!`, `NYMEX:CL1!`, `COMEX:GC1!`, …)
  used to seed the default watchlist.
- The approach of keeping walk-forward validation available specifically to
  detect overfitting, rather than reporting in-sample backtest results alone.

### Tradingview-MCP — Harshil Patel

<https://github.com/pueschel88/Tradingview-MCP> · MIT · Copyright (c) 2026 Harshil Patel

No code copied — that project is TypeScript and Shani's backend is Python — but
`shani/market/tradingview_cdp.py` is a direct port of its *technique*, and the
JavaScript expressions evaluated in the TradingView page are derived from its
`src/connection/tradingview.ts`. Borrowed:

- Reaching TradingView Desktop through the standard Chromium remote debugging
  port rather than scraping or reverse-engineering network traffic.
- The idea of driving the chart through its own internal page API, and the Monaco
  editor instance whose language id is `pinescript` for Pine Editor access.

  Note that the *specific* global that project uses, `window.tvWidget`, does not
  exist on tradingview.com — it belongs to the embeddable Charting Library, not
  the application Desktop actually loads, which exposes `window.TradingViewApi`.
  Shani checks the application global first. The trading-account internals
  (`trading()._activeBroker`, `allExecutions()`, `ordersHistory()`, and the
  `executionUpdate` delegate behind live fill capture) were found independently
  and are not part of what was borrowed.
- The architectural discipline of isolating *all* page coupling in exactly one
  file, so that a TradingView update is a one-file fix.
- The `doctor` diagnostic-command pattern, with per-check pass/fail lines and
  actionable messages.

---

## Runtime dependencies

| Package | License | Role |
|---|---|---|
| `tradingview-screener` | MIT | Plane A — TradingView screener API wrapper |
| `tradingview-ta` | MIT | Plane A — technical analysis ratings |
| `fastapi`, `starlette` | MIT | HTTP API |
| `uvicorn` | BSD-3-Clause | ASGI server |
| `pydantic`, `pydantic-settings` | MIT | Models and configuration |
| `httpx` | BSD-3-Clause | HTTP client |
| `websockets` | BSD-3-Clause | Plane B — CDP transport |
| `typer` | MIT | CLI |
| `rich` | MIT | Terminal output |
| `platformdirs` | MIT | Per-OS data/config paths |
| `desktop-notifier` | MIT | Post-trade interview notifications |
| `pyyaml` | MIT | Config files |

Portal dependencies (`portal/`) are recorded in `portal/package.json`: `next`,
`react` and `react-dom` are MIT; `lightweight-charts` is Apache-2.0, published by
TradingView.

Dependencies are not vendored — `node_modules/` and the Python environment are
both ignored, so this repository distributes source only and each user installs
its dependencies under their own licences. A dependency audit at the time of
writing found no GPL, AGPL or SSPL anywhere in either tree. Four Python packages
(`certifi`, `bidict`, `pathspec`, `tqdm`) are MPL-2.0, which is file-level
copyleft and explicitly permits a larger work under other terms (MPL §3.3), and
`@img/sharp-win32-x64` — pulled in transitively by Next.js — bundles LGPL-3.0
libvips as a separate binary.

### Keep the chart attribution

`lightweight-charts` renders a "Charting by TradingView" link, and it is left at
the library default deliberately. It is a licence condition, not decoration. Do
not pass `attributionLogo: false` to remove it.

---

## Not affiliated with TradingView

Shani is not affiliated with, endorsed by, or associated with TradingView Inc.,
NinjaTrader, or any broker or exchange. All trademarks belong to their
respective owners. See [DISCLAIMER.md](DISCLAIMER.md).
