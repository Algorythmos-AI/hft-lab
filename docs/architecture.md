# Architecture

HFT Lab trades the currency market on an Interactive Brokers **paper** account, fully automatically, with
guardrails that are always on. This page is the map. The reasons behind each choice are in the
[ADRs](adr/), and the order of work and what is built today are in the [roadmap](roadmap.md). The page
describes the whole design; much of it is not built, and nothing built has met the real market yet.

## Constraints that shape everything

- **Forex has no exchange.** The platform sees one broker's quote stream, not the whole market, and there is
  no public trade tape. The broker simulates paper fills from the top of its book, and those fills are
  optimistic. That is why the shadow book, not the broker's paper P&L, is the result of record.
- **Speed tier.** Quotes update every few milliseconds and an order round trip takes tens of milliseconds.
  "High frequency" here means many trades a day held for seconds to minutes, not microsecond races.
- **Costs decide everything.** Spread, commission with a per-order minimum, the minimum size for good
  pricing, and the 17:00 New York rollover when spreads widen. The first analysis is a feasibility map: for
  each pair and holding time, is the best possible move bigger than the cost? Strategies are built only
  where it is.
- **What may be traded is measured, not assumed.** A paper account mirrors the rules of the live account it
  belongs to. The capability probe (F1) records the tradable pairs, their form (spot or CFD), sizes and
  commissions in [`config/venues/ibkr.yaml`](../config/venues/ibkr.yaml).
- **The host is small and shared.** The platform lives beside two other desks and must never hurt them
  ([ADR 0007](adr/0007-shared-host-isolation.md)).

## Processes

On the host everything runs as the Unix user `hft`, in units prefixed `hft-`, inside one capped `hft.slice`.

| Unit | Role |
|---|---|
| `hft-gateway` | The broker's gateway in paper mode, kept alive by a standard controller; one login a week. Dropped if the probe shows the broker's keyed web API is good enough |
| `hft-recorder` | Sole job is to record; keeps running if the trader crashes |
| `hft-trader` | Features, models, sleeves, risk, order manager |
| `hft-reconciler` | Every minute: broker positions and fills against our books |
| `hft-train`, `hft-promote` | Weekend learning and the weekly promotion decision |
| `hft-report`, `hft-backup` | Summaries; journal and model backups |

## Package map

`src/hftlab/`. A package marked with a phase exists today; the others arrive with the phase that needs them
([roadmap](roadmap.md)).

```
core/         types, injected clock, ids; prices as integers at each pair's quote precision             F0
safety/       paper-only lock, venue registry                                                           F0
registry/     hypotheses, experiments, decisions, trial count                                           F0
venues/       port, guard, supervisor, a market-data view with no order methods;                        F1
              ibkr/ (translation, client seam, adapter, capability probe);
              fake/ (a deterministic broker for tests);
              sim/ (the shadow venue and its cost model)                                                F3
calendar/     forex week, rollover window, holidays, macro-event blackout                               F2
record/       recorder, recording format, store with quota and offload, gap journal                     F2
replay/       a recorded day read back as the stream the live process saw; the day check                F2
book/         positions, cash and profit from fills: the shadow book; the broker book and the           F3
              reconciler that compares it with the venue's positions and executions                     F4
strategy/     the sleeve interface, the idle and the control sleeve (directional-change, ml, dl: F5)    F3
shadow/       the shadow runner and its hash-chained journal                                            F3
feasibility/  the feasibility map: moves against the modelled cost, by pair and holding time            F3
features/  labels/  models/ (registry, hashed model files, scorer in its own process)
risk/         the independent gate: limits, market guards, latches, switches                            F4
exec/         the order manager: idempotent order ids, order state machine, write-ahead outbox          F4
journal       the hash-chained journal of the shadow runner, the outbox, the latches, the reconciler    F3
learn/        datasets, training, validation, promotion
ops/          jobs, units, deploy gate, heartbeat, alerts, backup
```

Two boundaries are enforced by tests from the first commit:

- **Only `venues/ibkr/` may import a network library or the broker's client library.** Everything else, the
  venue port, the fake broker, the recorder and the shadow venue included, is computation on events.
- **Only `safety/lock.py` may name a live port or read the variables that select the trading mode.**

## Data flow

```
 broker quote stream
        |
        v
 hft-recorder ----> append-only store ----> compaction ----> offload (never to git)
        |                  |
        |                  +--> replay: the same events, a SimClock, the same decisions offline
        v
 hft-trader
   quotes --> features --> model scores --> sleeves (each with its own book and limits)
                                               |
                                        order intents
                                               v
                                   independent risk gate  <--- KILL, FLATTEN, latches, market guards
                                               |
                                               v
                                 order manager (idempotent ids, outbox)
                                      |                         |
                          paper-only lock, then            shadow venue
                          the paper broker                 (our latency, spread, slippage model)
                                      |                         |
                                 broker fills              shadow fills
                                      v                         v
                                 broker book               shadow book  <== result of record
                                      \                         /
                                       hft-reconciler, every minute
                                               |
                                 alerts, daily summary, weekly scorecard
```

The strategy, risk and shadow-book code is a pure function of the event stream and the clock. Every event
carries an integer receive timestamp and a sequence number, time comes from an injected clock, and prices
are integers, so a recorded day replays to exactly the same decisions offline. That one property carries
most of the testing ([ADR 0005](adr/0005-deterministic-replay.md)).

## The two books

Every fill is booked twice, by two independent paths.

| | Broker book | Shadow book |
|---|---|---|
| Source of fills | The broker's paper account | Our own simulated venue, fed the same orders and the recorded quotes |
| Fill model | The broker's: filled from the top of its book, immediately | Ours: order latency, the quoted spread at arrival, slippage, the commission schedule with its minimum |
| What it is for | Proving the plumbing: orders, executions, reconciliation | Deciding whether anything works |
| Bias | Optimistic | Conservative by design |

**The shadow book is the result of record.** A paper account fills a market order at the displayed price,
with no queue, no impact and no rejection for size, so its P&L flatters every strategy that trades often,
which is every strategy here. A result that exists only in the broker's book is not a result. Promotion
tests, the weekly scorecard and every research record read the shadow book. The gap between the two books is
itself reported as shadow-versus-broker slippage, and a disagreement about *positions* between the broker
and our books is a reconciliation break: entries stop and an alert pages
([ADR 0003](adr/0003-two-books.md)).

The check that the shadow book is honest comes before any strategy: a flat strategy must earn exactly zero,
and a control sleeve that enters at random must lose about the modelled cost (F3 exit gate).

## Guardrails, always on

| Guard | What it does |
|---|---|
| Paper-only lock | Checked at start-up and before every order ([ADR 0001](adr/0001-paper-only-lock.md)) |
| Limits | Position per pair, order size, price collar against mid, orders per minute, order-to-trade ratio, pairs open at once. Gross exposure and leverage are not enforced yet: they need a currency conversion ([ADR 0015](adr/0015-risk-gate.md)) |
| Market guards | Stale-quote stop, spread guard, flat and no-trade around rollover, macro-event blackout, flat before the Friday close |
| Loss control | Daily loss latch and drawdown latch per sleeve and for the desk, per currency; reject-streak breaker. A latch stays tripped until an operator resets it |
| Switches | `KILL` (no new entries, exits managed) and `FLATTEN` |
| Connection loss | No orders while blind; on reconnect, cancel working orders and reconcile before resuming ([ADR 0016](adr/0016-order-manager-and-outbox.md)) |
| Reconciliation break | Entries stop and an alert pages. The alert is not built yet; the break is a flag, an event and a journal line |

Each has a requirement in [`requirements/requirements.yaml`](requirements/requirements.yaml). What is never
built is listed in [`never-build.md`](never-build.md).

## Learning loop

Weekend, market closed, trading processes stopped. Datasets are rebuilt from the platform's own recordings;
candidate models are trained and validated walk-forward by week with purge and embargo and untouched
hold-out weeks; every trial is counted. A challenger runs in shadow for a week and replaces the champion
only if it passes a gate written down beforehand, and it is rolled back automatically if it then breaches
its loss latch ([ADR 0008](adr/0008-learning-loop.md)).

## How bugs are kept rare, loud and contained

| Layer | Practice |
|---|---|
| Types and money | `mypy --strict` on the whole package; integer prices and sizes; exhaustive enums |
| Unit and property tests | Accounting invariants (cash conserved, position equals the sum of fills, booking exactly once) under generated inputs |
| Replay parity | Live decisions equal an offline replay of the recording |
| Fake broker | A protocol-level stand-in for the broker, so CI needs no network and no account |
| Fault injection | Disconnect mid-order, duplicate and out-of-order execution reports, partial fills, rejects, gateway restart, clock jump, stale quotes, weekend gap, rollover blow-out, disk full, process killed mid-write |
| Crash recovery | Kill at every step of the order outbox; state must come back consistent |
| Soak | Five trading days in shadow before the first paper order |
| Deploy | Only a CI-green commit on `main`; smoke tests; automatic rollback; deploys only when flat |
| Observability | Heartbeats, dead-man checks, latency from quote to fill, reject rate, shadow-versus-broker slippage |
