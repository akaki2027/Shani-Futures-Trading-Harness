# How the learning loop works

Shani's premise is that your trade history is a curriculum nobody has bothered
to read back to you. This is the mechanism.

The architecture is borrowed from [Hermes](https://github.com/NousResearch/hermes-agent),
which turns completed work into reusable procedure and retrieves it later.
Shani narrows that to exactly one domain.

## The loop

```
  trade closes
       │
       ▼
  interview          ← within seconds, while you still remember
       │
       ▼
  extraction         ← your answers become a setup card, in your words
       │
       ▼
  playbook           ← versioned, indexed, searchable
       │
       ▼
  retrieval          ← next matching signal pulls it back with your stats
```

## 1. Capture

A trade record holds more than the numbers. Entry, exit and P&L are what every
journal already stores and what has never been enough. Shani also records:

- **Session and time of day** — the 09:30 opening drive and the 12:15 lunch chop
  are different markets that print the same symbol.
- **Chart context from Plane B** — your timeframe, your indicators, and a
  screenshot of the screen at entry.
- **Planned risk, captured at entry** — the denominator of R. Recorded at entry
  rather than recomputed later, because you may have moved the stop, and
  recomputing would silently flatter every R multiple in the journal.
- **MAE and MFE** — how far it went against you before it worked. A string of
  winners that each went $400 underwater first is a very different account from
  one where they never did, and no P&L column distinguishes them.

## 2. Interview

Five questions, each answerable in a sentence:

1. What did you see that made you take this trade?
2. What would have told you the idea was wrong?
3. Where did you plan to exit, and did you actually exit there?
4. Would you take this again tomorrow in the same conditions?
5. Was there anything about how you were feeling that affected this one?

Short on purpose. Five brief questions get answered; fifteen thorough ones get
skipped, and a skipped interview teaches nothing.

**Speed matters more than depth.** An answer given four hours later is a
reconstruction — tidy, flattering, and useless. An answer given ninety seconds
after the fill is what actually happened, including the parts you would rather
not write down. That is why a desktop notification fires on close whether or not
the portal is open, and why answer latency is recorded.

Questions 3 and 5 are the ones that matter most, and the ones people least want
to answer honestly. The gap between the plan and the execution is where the
repeatable mistakes live.

## 3. Extraction

Answers plus trade context go to the strong model tier and come back as a
**setup card**:

| Field | What it holds |
|---|---|
| `trigger` | What has to happen for this setup to be present |
| `context` | Conditions under which it applies |
| `invalidation` | What tells you the idea is wrong |
| `management` | How it gets handled once you are in |

Two rules govern this step:

**Your language, not textbook language.** If you call it "the failed push", that
is the name. The card has to be recognisable to you at 09:31 next Tuesday, and a
card rewritten into standard terminology is a card about someone else's trading.

**A vague interview produces no card.** If extraction confidence is low, Shani
writes nothing and says so. A card that matches everything means nothing, and it
would pollute retrieval for months.

## 4. Versioning

Cards are versioned, never mutated. Guidance changes as the sample grows, and
*how* it changed is itself informative — a card revised four times toward "only
before 11:00" is telling you something a current-state row would erase.

A second trade on the same setup revises the existing card rather than creating
a near-duplicate. Forty variations on one idea is not a playbook.

## 5. Retrieval

When a signal arrives, Shani looks for matching history. Matching is layered from
most to least specific:

1. Strategy name matches a card slug (strongest — name your Pine alerts after
   your setups)
2. Instrument overlap
3. Timeframe overlap
4. Full-text search over card text and past interview answers

Deliberately simple and debuggable. An embedding model would rank better and be
much harder to explain when it ranked something absurd — and this output goes
into a decision about money.

The result goes into the agent's prompt, so a proposal reads:

> You have taken this 7 times: 4W/3L, avg +1.2R, net +$840.
> Worst time of day: Lunch (2 trades, −$410).

## Honesty mechanisms

Three things exist specifically to stop this from being a confidence machine.

**Sample size is always attached.** A card built from eleven trades showing 73%
is noise. Statistics under 30 trades are labelled `provisional` everywhere they
appear — in the API, in the prompt, and in the portal. Reporting a bare 73% is
how a tool talks someone into over-sizing.

**Ungrounded proposals are marked.** A proposal that cites no setup card is
flagged, and the portal renders it differently. The difference between "you have
done this 40 times and it works" and "this looks plausible to a language model"
is the difference between the product and a chatbot.

**The loop is measured, and can fail.** `/api/evaluation` compares on-playbook
trades against off-playbook trades and reports the difference — including when
the playbook did worse.

That comparison is observational, not an experiment. You choose which trades to
take, so a difference may reflect that playbook setups occur in easier conditions
rather than that the playbook helps. That caveat ships with the verdict and is
displayed, not buried.

## Cold start

With no trades, the agent has nothing, and it says so rather than inventing
plausible technical-analysis prose.

To see the shape of the system before you have history:

```bash
uv run shani demo
```

This seeds synthetic trades with a deliberate structure — lunch loses money, the
opening drive makes it — so the statistics have something true to find and you
can confirm the analysis works before trusting it on your own trades.

## Choosing a model

Two tiers, because the cost profiles differ by orders of magnitude:

- **Triage** runs on every signal. Wants speed and cheapness. Default:
  `claude-haiku-4-5-20251001`.
- **Reasoning** runs once per trade, at extraction. Wants the strongest model
  available, because a badly-extracted card poisons the playbook for months.
  Default: `claude-opus-5`.

```yaml
model:
  provider: anthropic   # anthropic | openai | openrouter | ollama | none
  triage_model: claude-haiku-4-5-20251001
  reasoning_model: claude-opus-5
```

**Local-first is a genuine option.** Your journal is your edge written down.
Setting `provider: ollama` keeps every trade note on your machine, and the loop
works identically — local models are weaker at extraction, which is the one step
where that hurts, so consider running extraction against an API and triage
locally if you want both.

`provider: none` disables the agent entirely. The journal, statistics, and paper
broker all work without any model.
