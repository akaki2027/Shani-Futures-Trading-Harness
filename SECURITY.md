# Security policy

## Reporting a vulnerability

Please report privately through GitHub's **Report a vulnerability** button under
the Security tab, rather than opening a public issue.

Include what it affects, how to reproduce it, and what an attacker gets. A
response should come within a few days.

## What is in scope

Shani handles money and holds a journal that is, in a real sense, its user's
trading edge in written form. The things that matter most:

- **The webhook** (`/webhook/tradingview`) — the only component intended to face
  the internet. HMAC bypass, signature forgery, or anything letting an unsigned
  payload become a `Signal`.
- **The risk gate** — any path that reaches a broker without passing through
  `RiskPolicy.evaluate`.
- **The live-trading gate** — anything that lets a live venue be constructed or
  reached while `allow_live` is false.
- **Prompt injection with consequences.** Injection into a proposal is expected
  and bounded — the model cannot execute anything. Injection that causes an
  order, exfiltrates journal data, or escapes the four-gate flow is in scope.
- **API auth** — bypassing the bearer token on any route other than `/health`
  and the webhook.
- **Data exposure** — anything writing the journal database, API token, or
  webhook secret somewhere it could be committed or shared.

## Out of scope

- Losing money on a trade. Shani is not an advisor; see [DISCLAIMER.md](DISCLAIMER.md).
- The TradingView debug port (9222) being dangerous. It is, by design — it grants
  full control of the application. Keeping it on `localhost` is documented and
  is the user's responsibility.
- Running the API on `0.0.0.0` and being reachable. The default is loopback and
  changing it prints a warning.
- Rate limiting on TradingView's public endpoints.

## Notes for users

- **Rotate your webhook secret** if it appears in a screenshot, an issue, or a
  chat: `shani init --force`, then update your alerts.
- **Never commit the database.** `.gitignore` blocks it and CI fails the build if
  one is tracked. An API key can be rotated; a published playbook cannot.
- **Never expose port 9222.**
- **Expose only `/webhook/tradingview`** through any tunnel. See
  [docs/cloudflare-tunnel.md](docs/cloudflare-tunnel.md).

## Known limitations

Stated plainly rather than discovered later:

- **Live execution is untested.** It ships disabled and no live adapter is
  implemented.
- **Plane B is unverified against a live TradingView Desktop.** Error handling is
  tested; a real chart read is not.
- **The paper broker is optimistic.** It does not model queue position, partial
  fills in a thin book, or a fast market.
- **Prompt-injection fencing is mitigation, not a guarantee.** The structural
  protection — that the model cannot execute anything — is the one that holds.
