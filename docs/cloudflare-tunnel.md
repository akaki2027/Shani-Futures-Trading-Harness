# Exposing the webhook with Cloudflare Tunnel

TradingView's servers have to reach your machine for Plane C alerts to arrive,
and `127.0.0.1:8420` is not reachable from the internet. A tunnel solves that
without opening a port on your router.

Cloudflare Tunnel is the recommendation here: free, no inbound firewall rule, no
port forwarding, TLS by default, and a hostname that survives restarts.

> [!CAUTION]
> Expose **only the webhook path**. The rest of the Shani API is your journal,
> your positions, and your order entry. It has no business on the public
> internet, and the configuration below is written to keep it off.

## Install

```bash
# macOS
brew install cloudflared

# Windows
winget install --id Cloudflare.cloudflared

# Linux — see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
```

## Quick tunnel — for testing only

```bash
cloudflared tunnel --url http://127.0.0.1:8420
```

You get a random `https://<words>.trycloudflare.com` hostname. Fine for
verifying the pipe works; useless day to day, because the hostname changes on
every restart and you would re-paste it into every alert.

**It also exposes your entire API.** Use it to test, then shut it down.

## Named tunnel — the real setup

Requires a free Cloudflare account with a domain.

```bash
cloudflared tunnel login
cloudflared tunnel create shani
cloudflared tunnel route dns shani shani.yourdomain.com
```

Then write the config — this is the part that matters:

```yaml
# ~/.cloudflared/config.yml
tunnel: shani
credentials-file: /home/you/.cloudflared/<tunnel-id>.json

ingress:
  # The ONLY path reachable from outside.
  - hostname: shani.yourdomain.com
    path: ^/webhook/tradingview$
    service: http://127.0.0.1:8420

  # Everything else is refused at the edge, before it reaches Shani.
  - service: http_status:404
```

That two-rule ingress is the whole security posture: the webhook is public, the
journal is not, and the refusal happens at Cloudflare rather than relying on
Shani to say no.

Run it:

```bash
cloudflared tunnel run shani
```

Install it as a service so it survives a reboot:

```bash
sudo cloudflared service install     # Linux/macOS
cloudflared service install          # Windows, from an elevated shell
```

## Point TradingView at it

In the alert dialog, set **Webhook URL** to:

```
https://shani.yourdomain.com/webhook/tradingview
```

## Verify

With the tunnel up and `shani serve` running:

```bash
curl -X POST https://shani.yourdomain.com/webhook/tradingview \
  -H "Content-Type: application/json" \
  -d '{"secret":"YOUR_SECRET","symbol":"ES","action":"buy","price":"5000.00"}'
```

A `signal_id` in the response means the whole path works. Then confirm the other
half of the posture:

```bash
curl https://shani.yourdomain.com/api/trades   # must return 404
```

If that returns your trades, the ingress rules are wrong. Fix them before doing
anything else.

## Defence in depth

The tunnel is one layer. The others are already in place:

- **HMAC on every payload.** Even if someone finds the URL, an unsigned request
  is rejected. Constant-time comparison, so the signature cannot be guessed a
  byte at a time through response timing.
- **Fail closed.** An unconfigured secret rejects everything rather than
  accepting everything.
- **A signal is not an order.** Reaching the webhook gets you one `Signal`
  record. Execution still requires the agent, the risk gate, and your click.
- **Rotate the secret** if you ever paste it into a screenshot, an issue, or a
  chat. Regenerate with `shani init --force` and update your alerts.

## ngrok, if you prefer

```bash
ngrok http 8420
```

One command, works immediately. Two costs: the free tier rotates your URL on
every restart, and it exposes the whole API — there is no per-path ingress rule
like the Cloudflare config above. Acceptable for a ten-minute test, not for
something left running.
