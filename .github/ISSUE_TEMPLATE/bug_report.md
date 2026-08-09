---
name: Bug report
about: Something behaves incorrectly
labels: bug
---

> [!WARNING]
> **Do not paste your API token, webhook secret, or journal database.**
> `config.yaml` is safe to share — secrets are redacted from it by design.
> `.env` is not.

## What happened

## What you expected

## Steps to reproduce

1.
2.

## `shani doctor` output

```
paste here
```

## Environment

- OS:
- Python version:
- Shani version:
- Broker: paper / other
- Model provider: anthropic / openai / openrouter / ollama / none

## Which planes are enabled

- [ ] Plane A — market data
- [ ] Plane B — TradingView Desktop
- [ ] Plane C — Pine webhook

## Anything about money

If this involved an order, a fill, or a P&L figure, please say so explicitly and
include the audit log entries (`GET /api/audit`) around the event. Arithmetic
bugs are the highest-priority class in this project.
