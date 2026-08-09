# Connecting Shani to TradingView

TradingView has **no official API for trading.** Every integration in this space
— including the well-known open-source ones — works around that in one of three
ways, and each has different requirements, different reliability, and different
things it can actually do.

Shani implements all three. They are independent: you can run any one of them
alone, and Plane A needs nothing from you at all.

| | What it is | Needs | Gives you | Reliability |
|---|---|---|---|---|
| **A** | Public screener endpoints | Nothing | Quotes, screeners, technical ratings | Good — undocumented but stable |
| **B** | Chrome DevTools Protocol into TradingView **Desktop** | Paid TV Desktop | The chart *you* are on, screenshots, Pine editor | Fragile — breaks on TV updates |
| **C** | Pine alert → webhook | A tunnel | Real-time signals from your scripts | Excellent — the sanctioned path |

> [!IMPORTANT]
> Shani is not affiliated with TradingView. Plane B talks to an instance **you**
> installed, logged into, and paid for, on your own machine, using a standard
> Chromium debug flag **you** enable. It bypasses no paywall and redistributes
> no data. Your use of TradingView is still governed by their terms — read them,
> and if your market-data agreement prohibits automated access, do not enable
> Plane B.

---

## Plane A — market data (no setup)

Works out of the box. Nothing to configure, no account, no key.

```yaml
# config.yaml
tradingview:
  screener_enabled: true
  watchlist: [ES, NQ, CL, GC]
  screener_cache_seconds: 30
```

Verify:

```bash
uv run shani doctor
```

### How it works, and the one thing that will bite you

Shani uses the `tradingview-screener` library, which wraps TradingView's public
`/screener` endpoint — the same one their own web screener calls.

**That library is pinned to exactly `3.0.0`, and the pin is load-bearing.** From
3.2.0 onward, every bare `Query()` injects a default *equity* preset. Against
the futures scanner that preset matches nothing, so your queries return **zero
rows with no error** — no exception, no warning, just an empty watchlist and a
screener that appears to be working.

If you upgrade it, futures data silently stops. Don't, unless you also clear the
stock preset. Credit to [atilaahmettaner/tradingview-mcp](https://github.com/atilaahmettaner/tradingview-mcp)
for documenting this.

### Rate limiting

The endpoint is undocumented and will throttle you. Shani caches responses for
30 seconds by default. If you see repeated failures in `doctor`, raise
`screener_cache_seconds` and back off — there is no published quota to appeal
to.

---

## Plane B — your TradingView Desktop

This is the one that makes Shani different from a trade log. It reads **the
chart you are actually looking at**: your symbol, your timeframe, your
indicators, and a screenshot of the screen at the moment you entered.

That context is what an ordinary journal throws away, and it is the single most
valuable thing in the corpus your playbook gets built from.

### Requirements

- **TradingView Desktop** (the installed application, not the website).
- A **paid TradingView subscription**. This tool does not work around that.

### 1. Quit TradingView completely

The debug flag is read at startup. If an instance is already running, launching
again with the flag just focuses the existing window and the flag is ignored —
which is the most common reason this step appears to fail.

Check your system tray on Windows; TradingView often keeps running there after
you close the window.

### 2. Relaunch with the debug port

**Windows (PowerShell):**

```powershell
& "$env:LOCALAPPDATA\Programs\TradingView\TradingView.exe" --remote-debugging-port=9222
```

**macOS:**

```bash
open -a "TradingView" --args --remote-debugging-port=9222
```

**Linux:**

```bash
tradingview --remote-debugging-port=9222
```

`--remote-debugging-port` is a standard Chromium flag. TradingView Desktop is an
Electron application, so it embeds Chromium — the same debug interface exists in
VS Code, Slack, Discord, and every other Electron app. It is off unless you
explicitly pass the flag.

### 3. Open a chart

Shani needs a chart loaded to read anything. A blank workspace produces
`tvWidget not available`.

### 4. Enable it in config

```yaml
tradingview:
  desktop_enabled: true
  cdp_host: localhost
  cdp_port: 9222
  capture_entry_screenshot: true
```

### 5. Verify

```bash
uv run shani doctor
```

You want:

```
  ok    plane B — TradingView Desktop connected — NASDAQ:AAPL on 1h
```

### Security note

The debug port gives **full control of the TradingView application** to anything
that can reach it. Keep `cdp_host` on `localhost`. Never expose port 9222 to a
network or forward it through a tunnel. Close it when you are not using Shani by
restarting TradingView without the flag.

### When it breaks

It will, eventually. TradingView's internal page API is not a public contract,
and any Desktop release can change it.

Everything Shani knows about TradingView's internals lives in exactly one file:
[`shani/market/tradingview_cdp.py`](../shani/market/tradingview_cdp.py). If tools
start failing after an update, that is the only file that needs fixing — the JS
expressions at the top of it are the whole surface area.

That single-file discipline, and the specific internals to target
(`window.tvWidget.activeChart()`, and the Monaco editor whose language id is
`pinescript`), come from [pueschel88/Tradingview-MCP](https://github.com/pueschel88/Tradingview-MCP).

If you want the same capability as a standalone MCP server for Claude Code
rather than inside Shani, use that project directly — it does this well and is
smaller to read.

---

## Plane C — Pine alerts via webhook

The only TradingView-sanctioned path from a chart to an action. Your Pine script
fires an alert; TradingView POSTs it to Shani.

### 1. Set a secret

```bash
# .env — never in config.yaml
SHANI_TRADINGVIEW__WEBHOOK_SECRET=<the value shani init generated>
```

Unsigned payloads are rejected. An unset secret **fails closed** — the endpoint
accepts nothing rather than accepting everything.

### 2. Expose the endpoint

TradingView's servers must reach your machine, so `127.0.0.1:8420` is not enough
on its own. See [cloudflare-tunnel.md](cloudflare-tunnel.md).

### 3. Write the alert

In your Pine script:

```pine
//@version=5
strategy("Opening drive continuation", overlay=true)

// ... your logic ...

if (longCondition)
    alert('{"secret":"YOUR_SECRET","symbol":"{{ticker}}","action":"buy",' +
          '"price":"{{close}}","interval":"{{interval}}",' +
          '"strategy":"Opening drive continuation",' +
          '"message":"Broke opening range, pullback held VWAP"}',
          alert.freq_once_per_bar_close)
```

Then create the alert in TradingView with **Webhook URL** pointing at
`https://your-tunnel-hostname/webhook/tradingview`.

### Payload fields

| Field | Required | Notes |
|---|---|---|
| `secret` | yes | Must match your configured secret exactly |
| `symbol` / `ticker` | yes | `ES`, `ESZ5`, `CME:ES1!` all resolve. Unknown instruments are **rejected** |
| `action` / `side` | no | `buy`/`long`/`sell`/`short`. Omit for informational alerts |
| `price` / `close` | no | Must be positive |
| `interval` / `timeframe` | no | Used to match against your setup cards |
| `strategy` / `name` | no | **Match this to a setup card name** — it is the strongest retrieval signal |
| `message` / `comment` | no | Free text, capped at 2000 characters |

`strategy` is worth getting right: naming it identically to a setup card is what
lets Shani say "you have taken this seven times" instead of "this looks new".

### What happens when an alert arrives

Nothing executes. The alert becomes a `Signal`, the agent may turn it into a
proposal, the risk gate evaluates that, and **you** confirm it. Four gates, and
the internet controls only the first.

### Security

- HMAC-verified with a constant-time comparison, so the signature cannot leak
  its prefix through response timing.
- Payloads over 16 KB are rejected before parsing.
- Unknown instruments are rejected rather than passed through — a hostile alert
  cannot make Shani reason about a contract it has no specification for.
- Rejections are deliberately terse. A detailed error on an internet-facing
  endpoint is an oracle for whoever is probing it.
- The alert body is stored verbatim for the audit trail but is fenced as
  untrusted data before it ever reaches a language model.

---

## The three planes together

Individually these are useful. Chained, they close a loop none of them can
manage alone:

1. You take a trade. **Plane B** captures the chart you were on.
2. It closes. Shani asks why, while you still remember.
3. Your answer becomes a versioned setup card.
4. Shani writes that setup as a **Pine script** and pushes it into your editor
   through **Plane B**.
5. The alert fires and arrives over **Plane C**.
6. Shani proposes it back with **your own statistics** attached — from **Plane A**
   for market context and from your journal for everything that matters.

That is the whole idea. Step 4 is what turns a journal into a system.

---

## Alternatives worth knowing about

If Shani is more than you want, these do parts of this well:

- **[atilaahmettaner/tradingview-mcp](https://github.com/atilaahmettaner/tradingview-mcp)** —
  Plane A as a standalone MCP server, with ~40 tools including screeners,
  multi-timeframe analysis, and walk-forward backtesting. Python, MIT.
- **[pueschel88/Tradingview-MCP](https://github.com/pueschel88/Tradingview-MCP)** —
  Plane B as a standalone MCP server. Small, strictly typed, easy to read in an
  afternoon. TypeScript, MIT.

Both are credited in [NOTICE.md](../NOTICE.md).
