# Safety model

Shani can place orders. This document is what stops that from being reckless.

## Live trading is unreachable, not merely disabled

The important distinction: a *guarded* live adapter is one inverted boolean away
from sending a real order. An *unregistered* one cannot be constructed at all.

While `allow_live` is false — the default — live venues are never added to the
broker registry. Asking for one raises rather than returning a disabled object
that politely refuses. There is no flag deep in the order path that a refactor
could invert by accident, and no code path from a signal to a live venue that
exists but happens not to run.

Enabling it requires **two independent things**:

```yaml
broker:
  allow_live: true
  live_confirmation: "I accept full responsibility for live orders"
```

The phrase must match exactly. A lone boolean is too easy to flip while skimming
a config file; a sentence has to be read. If the flag is set and the phrase is
not, Shani logs a loud warning — because a trader who believes live trading is
on when it is not is in a worse position than one who knows it is off.

**No live adapter is implemented in this release.** Even with both set, orders
continue to route to the paper broker. Shipping an unverified live adapter that
*looks* ready is worse than shipping none.

## The risk gate

Every order passes through it. There is no bypass.

| Limit | Default | What it does |
|---|---|---|
| `kill_switch` | off | Master off. Rejects everything, ignoring all other settings |
| `max_daily_loss` | $1,000 | Halts trading for the session |
| `max_position_contracts` | 5 | Per instrument |
| `max_open_positions` | 3 | Concurrent instruments |
| `max_orders_per_minute` | 10 | Catches runaway loops |
| `max_risk_per_trade` | $500 | Planned entry-to-stop risk |
| `require_stop_loss` | on | Refuses entries with no protective stop |

Three design decisions worth knowing:

**Limits refuse; they do not warn.** A limit that only logs is a preference. The
moment you most want to override a daily loss limit is three losers deep, certain
the next one comes back — which is exactly the moment it is doing the most good.
Encoding that judgement while calm is the entire value.

**The daily loss limit uses the trading day, not the calendar day.** The session
boundary is 18:00 ET. A calendar-day limit would reset at midnight, in the middle
of the Asian session, quietly granting a fresh loss allowance to someone already
past their limit.

**Position limits check the resulting position, not the order.** Otherwise five
separate one-lots build exactly the position the limit was written to prevent.
Reducing is always allowed — the gate must never trap you in a position.

## The four gates

An alert from the internet cannot reach your account. It passes through:

1. **Webhook** — HMAC-verified, size-capped, unknown instruments rejected.
2. **Agent** — produces a *proposal*. It cannot execute anything.
3. **Risk gate** — evaluates against every limit above.
4. **You** — a human click. Always, on anything live.

The internet controls the first. You control the last.

## Prompt injection

Alert payloads and news text are attacker-controllable and flow toward a model
that proposes trades. Shani fences that text explicitly as reference-only data
before it reaches a prompt, and strips delimiters so a payload cannot close its
own fence.

Fencing helps. It is not a guarantee — nothing is. The protection that actually
holds is structural: **the model cannot execute anything.** The worst outcome
from a successful injection is a bad proposal that the risk gate evaluates and
you decline.

## The audit log

Every signal, proposal, gate decision, and order is written to an append-only
log. Refusals are recorded as carefully as approvals, because "why did it *not*
trade" is a real question.

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8420/api/audit
```

This matters most on the day a session goes badly and you need to reconstruct
what happened — which is precisely when memory is least reliable.

## Network posture

- The API binds `127.0.0.1`. Your journal is your edge written down; your order
  entry is on the same port.
- Bearer-token auth, constant-time comparison.
- If you tunnel, expose **only** `/webhook/tradingview`. See
  [cloudflare-tunnel.md](cloudflare-tunnel.md).
- Never expose the TradingView debug port (9222). It grants full control of the
  application.

## What the paper broker does not model

It models slippage and commission. It does **not** model queue position, partial
fills in a thin book, or a genuine fast market where your stop *is* the
liquidity.

Paper results flatter you. They always do. Size accordingly when you move to
real money — and read [DISCLAIMER.md](../DISCLAIMER.md) first.
