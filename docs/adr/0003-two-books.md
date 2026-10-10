# ADR 0003: Two books; the shadow book is the result of record

- **Status:** accepted, 2026-10-10
- **Context:** F0 foundation; built in F3 and F4

## Context

A paper account is the only safe place to run an automatic strategy, and its P&L is the obvious number to
report. It is also wrong in a known direction. The broker fills a paper market order from the top of its own
book at once: no queue, no rejection for size, no impact, and a spread that may not be the one a real order
would have met. Forex has no public tape to check a fill against. For a strategy that trades many times a
day, a small flattering error per trade is the whole result.

## Decision

1. **Every fill is booked twice.**
   - The **broker book** holds what the paper account reports.
   - The **shadow book** holds fills from our own simulated venue, which is fed the same orders and the
     recorded quotes and applies our model of order latency, the quoted spread at arrival, slippage, and the
     commission schedule with its minimum.
2. **The shadow book is the result of record.** Promotion tests, the weekly scorecard and every research
   record read it. A profit that exists only in the broker book is not a result.
3. **The shadow model is conservative on purpose**, and it is checked before any strategy is trusted: a flat
   strategy earns exactly zero, and a control sleeve that enters at random loses about the modelled cost.
4. **The broker book is for the plumbing.** It proves that orders, executions and positions agree with the
   broker. A reconciler compares broker positions and fills with our books every minute.
5. **The difference between the books is reported**, as shadow-versus-broker slippage. A disagreement about
   positions is a reconciliation break: entries stop and an alert pages.

## Consequences

- Reported performance will look worse than the paper account's. That is the point.
- The cost model becomes a first-class, versioned part of the platform. Changing it is an evaluation change
  and needs a hypothesis, an experiment and a decision.
- The shadow venue is listed in the venue registry as `sim` and is exempt from the paper-only lock because it
  never talks to a broker.
- If the shadow model is ever shown to be optimistic against measured paper fills, that is a defect in the
  model, not a reason to switch the result of record.
