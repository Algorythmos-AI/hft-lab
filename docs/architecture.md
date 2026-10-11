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
| `hft-trader` | Features, models, sleeves, risk, order manager, and the reconciler: every minute, broker positions and fills against our books. The plan drew the reconciler as a unit of its own; it runs inside the trader's loop, because it needs the order manager's book and one outbox has one writer ([ADR 0019](adr/0019-trader-loop-and-staged-rollout.md)) |
| `hft-train`, `hft-promote` | Weekend learning and the weekly promotion decision |
| `hft-report`, `hft-backup` | Summaries; journal and model backups |

Seven of these are rendered as unit files today, from code ([ADR 0017](adr/0017-host-layer.md)): the gateway
as a placeholder that cannot be enabled, the recorder with its hourly `hft-housekeep`, `hft-shadow`,
`hft-trader` (which has no install section: the owner starts it, nothing enables it), `hft-publish` (the
status check, the pages and the status snapshot) and `hft-backup`. None is installed on any host. The others
arrive with their phases.

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
strategy/     the sleeve interface, the idle and the control sleeve, and the ML sleeve skeleton        F3
              dc/ (the Directional Change detector, baseline sleeve and null: F5, synthetic data only;
              dl: not built)
shadow/       the shadow runner and its hash-chained journal                                            F3
feasibility/  the feasibility map: moves against the modelled cost, by pair and holding time            F3
review/       reading a run back: round trips against the cost model, a sleeve's day beside its null    F3
features/     causal, exact features of one pair's quote stream, the same offline and live             F5
labels/       cost-aware outcomes and triple-barrier outcomes, each with the time its window ends      F5
models/       the hashed model file, the registry, an exact ridge model, boosted trees as whole        F5
              numbers, two baselines, the scorer (neural networks are not built)
scoring/      the scorer in a process of its own: the message format, the child, the trader's side     F6
              of the pipe and the record a replay abstains by (off as shipped; synthetic data only)
risk/         the independent gate: limits, market guards, latches, switches                            F4
exec/         the order manager: idempotent order ids, order state machine, write-ahead outbox          F4
trader/       the loop that wires them: stages, two books, the tape of what it saw, the replay,         F4
              and the sleeves' states kept across a restart
journal       the hash-chained journal of the shadow runner, the outbox, the latches, the reconciler    F3
learn/        datasets with a look-ahead check, walk-forward validation, the promotion decision        F6
              (foundations only: no training run, no weekly promotion, no process)
ops/          the host layer: its names, units, pre-flight gate, status and dead-man rule, alerts, backup    F2
publish/      the status snapshot: built from the status reading, checked, signed and sent                 F2
```

Two boundaries are enforced by tests from the first commit:

- **Only `venues/ibkr/` may import a network library or the broker's client library.** Everything else, the
  venue port, the fake broker, the recorder and the shadow venue included, is computation on events. The one
  exception is `ops/post.py`, the host layer's HTTPS POST for pages and the status snapshot: it may import
  five names of the standard library's HTTP client, sends to `https` addresses only, and never to this machine.
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
| Reconciliation break | Entries stop, the sleeves placing paper orders are stepped down, and an alert pages |
| Staged rollout | A sleeve places a paper order only after it earned the stage: clean trading days, the owner's decision record, limits in force. A latch or a break steps it down at once ([ADR 0019](adr/0019-trader-loop-and-staged-rollout.md)) |

Each has a requirement in [`requirements/requirements.yaml`](requirements/requirements.yaml). What is never
built is listed in [`never-build.md`](never-build.md).

## Learning loop

Weekend, market closed, trading processes stopped. Datasets are rebuilt from the platform's own recordings;
candidate models are trained and validated walk-forward by week with purge and embargo and untouched
hold-out weeks; every trial is counted. A challenger runs in shadow for a week and replaces the champion
only if it passes a gate written down beforehand, and it is rolled back automatically if it then breaches
its loss latch ([ADR 0008](adr/0008-learning-loop.md)).

That is the plan. What exists of it is tested on synthetic streams and against the fake broker only: features
and labels with their timing rule ([ADR 0020](adr/0020-features-and-labels-timing.md)), the splits, the
hold-out, the trial count and the promotion decision as a function
([ADR 0021](adr/0021-validation-splits-trials-and-the-three-numbers.md)), the model file, an exact ridge
model and the scorer ([ADR 0022](adr/0022-model-file-registry-and-scorer.md)), gradient-boosted trees fitted
with an optional library and kept and scored as whole numbers
([ADR 0025](adr/0025-gradient-boosting-optional-set-and-whole-number-trees.md)), and the weekly loop as
processes: the training run, the hash-chained lineup journal the trader reads its models from, the weekly
decision with its rollback and drill, and the drift look ([ADR 0023](adr/0023-weekly-learning-loop.md)).
The scorer can run in a process of its own, behind a timeout, a restart with back-off and a record that lets
a day replay without it; as shipped it runs in the trader's process. Nothing has trained on a real recording,
no promotion rule exists so nothing can be promoted, and no model has been fitted to real data.

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
