# HFT Lab

> HFT Lab: high-frequency paper trading for the currency market — recorder, strategy sleeves, risk gate, weekly retraining (Python)

A fully automatic trading platform for the forex market that runs against an Interactive Brokers **paper**
account. It records every price update, decides, guards every order with an independent risk gate, books the
results twice, retrains at the weekend and promotes a new model only when it passes tests written down in advance.

|                 |                                                                                                              |
| --------------- | ------------------------------------------------------------------------------------------------------------ |
| **Status**      | Code for F1 to F4 is written and tested against a fake broker and synthetic streams only · the F1, F2, F3 and F4 gates are open; F4's needs five days on the real paper account · foundations of F5 and a Directional Change baseline sleeve are written, and F6's weekly learning loop is written with its gate open, with ridge, gradient-boosted trees and a small sequence network as the kinds of model it can fit, all tested on synthetic streams only: no model has been trained on real data, no promotion rule exists and nothing here is evidence of an edge · nothing has recorded or traded yet, and as shipped the trader runs in shadow-only mode · the host layer, with its deploy gate, its daily and weekly reports, a tape held under a quota and a backup of everything that cannot be rebuilt, is written and tested off the host, and nothing is installed on any host · paper only · no live trading |
| **Owner**       | [@Algorythmos-AI/maintainers](https://github.com/orgs/Algorythmos-AI/teams/maintainers)                      |
| **Runs at**     | Nothing runs yet and nothing is installed on any host. Target: a shared Linux host, against an Interactive Brokers **paper** account. The units, the pre-flight gate and the install script are written and tested off the host only |
| **Run locally** | `make bootstrap && make ci`                                                                                  |
| **Context**     | [Architecture](docs/architecture.md) · [Roadmap](docs/roadmap.md) · [ADRs](docs/adr/) · [Requirements](docs/requirements/requirements.yaml) · [Research registry](research/README.md) · [Runbooks](docs/runbooks/README.md) · [Never build](docs/never-build.md) · [Security](SECURITY.md) · [Licence](LICENSE) |

---

> **Paper trading only. No promise of profit.** This is a research platform that trades simulated money on a
> broker's paper account. A paper-only lock (`hftlab.safety`) refuses any connection that is not provably a
> paper connection. Nothing here is financial advice, and nothing here claims the strategies make money: the
> first thing the platform measures is what trading costs, and a result only counts in the conservative shadow
> book.

## What it does, every week

| Loop | When | What |
|---|---|---|
| **Record** | Sunday 17:15 to Friday 17:00 New York | Every price update for the traded pairs, written to an append-only store |
| **Decide** | continuously | Features, then model scores, then strategy "sleeves", each with its own book and limits |
| **Guard** | before every order | An independent risk gate that rebuilds positions from fills and owns the kill switch |
| **Trade** | continuously | Paper orders on the broker's paper account; the adapter cannot reach a live account |
| **Account** | continuously | Two books: what the broker's paper account reports, and a conservative shadow book with our own latency, spread and slippage model. **The shadow book is the result of record** |
| **Learn** | weekend, market closed | Rebuild datasets, train candidate models, validate, register |
| **Promote** | weekly, automatic | A challenger that ran in shadow all week replaces the champion only if it passes pre-registered tests; automatic rollback if it then breaches its loss latch |
| **Report** | daily and on every change | Alerts, a daily summary, a weekly scorecard |

"High frequency" here means many trades a day held for seconds to minutes on a quote stream that updates every
few milliseconds. It does not mean microsecond races or co-location.

## What exists today

Code, and tests of it. **One read-only run of the capability probe aside, nothing here has met the real market.**

| Piece | Where |
|---|---|
| Domain types with integer prices, an injected clock, deterministic order ids | `src/hftlab/core/` |
| The paper-only lock, applied in stages over a connection, and the venue registry | `src/hftlab/safety/` |
| The venue port and the guard every venue shares | `src/hftlab/venues/port.py`, `guard.py` |
| The Interactive Brokers adapter (paper only) and the capability probe | `src/hftlab/venues/ibkr/` |
| A deterministic fake broker for tests and fault injection | `src/hftlab/venues/fake/` |
| The forex week, the rollover, and dated holidays and macro events (both lists still empty) | `src/hftlab/calendar/`, `config/calendar/` |
| The recorder, its checksummed store, and replay with a day check | `src/hftlab/record/`, `src/hftlab/replay/` |
| The shadow venue and its cost model (every number a placeholder) | `src/hftlab/venues/sim/`, `config/cost_model.yaml` |
| The book: positions, cash and profit from fills only | `src/hftlab/book/` |
| The sleeve interface, the idle sleeve and the random control sleeve | `src/hftlab/strategy/` |
| The shadow runner with a hash-chained journal | `src/hftlab/shadow/` |
| The feasibility map: moves against the modelled cost, by pair and holding time | `src/hftlab/feasibility/` |
| Reading a run back: round trips against the cost model trip by trip, and a Directional Change sleeve's day beside its random-walk null | `src/hftlab/review/` |
| Features of the quote stream: causal, exact integers, the same from a recording and live, as single rows or as windows of the last rows | `src/hftlab/features/` |
| Labels: cost-aware outcomes priced as the shadow venue prices, and triple-barrier outcomes | `src/hftlab/labels/` |
| Datasets with a look-ahead check, walk-forward validation with a hold-out read once, the promotion decision as a function | `src/hftlab/learn/` |
| An import of tick files the owner exported himself, read from disk into a store of its own, with provenance in every day (nothing is downloaded; no file has been imported yet) | `src/hftlab/history/` |
| The model file and its registry (a file says, under its own hash, how many of the rows it was fitted to were recorded and how many imported), an exact ridge regression, gradient-boosted trees and a small sequence network kept and scored as whole numbers, two baselines, a scorer (no model ships) | `src/hftlab/models/` |
| The lineup: a hash-chained journal of which model is champion and which are challengers, the trader's loading of them by hash, the weekly decision and the drift look | `src/hftlab/lineup/` |
| An ML sleeve skeleton that asks a scorer and reports that it may not leave shadow on placeholder costs | `src/hftlab/strategy/ml.py` |
| The scorer in a process of its own: a versioned message format, a child that holds no venue, a timeout, a restart with back-off, and a record a replay abstains by (off as shipped) | `src/hftlab/scoring/` |
| The independent risk gate (every limit still a null placeholder, not in force) | `src/hftlab/risk/`, `config/risk.yaml` |
| The order manager with its write-ahead outbox, and reconciliation | `src/hftlab/exec/`, `src/hftlab/book/reconcile.py` |
| The trader: one loop to two books, a staged rollout that starts and stays in shadow as shipped, a tape and a replay | `src/hftlab/trader/`, `config/trader.yaml` |
| Sleeve state kept across a restart: an exact save and restore, believed only when no journal and no tape moved since | `src/hftlab/trader/resume.py`, `src/hftlab/strategy/sleeve.py` |
| The research registry: its lint, the trial count, 6 hypotheses, 6 experiments registered and not run, 1 record of a measurement (the capability probe's first real run), and 0 decision records | `src/hftlab/registry/`, `research/`, `config/trial_registry.yaml` |
| The leak check for a public repository, with its hooks | `tools/leakcheck.py`, `.githooks/` |
| The status snapshot contract, and its publisher (nothing has been sent yet) | `contracts/`, `src/hftlab/publish/` |
| The host layer: systemd units rendered from code, the pre-flight gate, the status command with its dead-man rule, alerts, backup, the daily and weekly reports, and the deploy gate with its pruning of old checkouts (none of it installed on any host) | `src/hftlab/ops/`, `config/ops.yaml`, `config/preflight.json` |
| What the owner runs on the host: the pre-flight sampler, the install script, the status wrapper, the unit files | `deploy/` |
| Design records | `docs/` |

What that does and does not mean:

- **Proven against:** a deterministic fake broker, a stub of the broker library, synthetic quote streams, and
  recordings the tests make themselves.
- **Measured, and how little:** the capability probe has run against the real paper gateway, read-only, on
  2026-10-11 in the minutes after the weekly open
  ([`HFT-EXP-0007`](research/experiments/HFT-EXP-0007-capability-probe-weekly-open/report.md)). EUR.USD,
  USD.JPY, AUD.USD and GBP.USD each resolve as spot and as a CFD, with five levels of market depth. Nothing
  about costs, tradability or order timing is measured: no order was placed, and the spreads of those minutes
  are not inputs to anything.
- **Not done:** that run is all that has touched anything real. Nothing has been recorded from the real
  feed, and no experiment on a strategy has been run on real data. **Nothing has traded.** The recorder and the shadow runner cannot
  place a broker order. The trader can only once the risk limits are in force and a sleeve has earned a
  stage above `shadow`; as shipped neither holds, and it runs in shadow-only mode. The capability probe can,
  on the paper account, when the owner runs it with `--orders`.
- **The host layer exists and is tested off the host only:** the units as text, the scripts in dry runs
  against a temporary directory, the publisher against a stand-in for the network. Nothing is installed on
  any host, no unit has been loaded by a service manager, and nothing has been published. Recording starts
  only after the owner has run the capability probe, the pre-flight day and the install
  ([runbook](docs/runbooks/install.md)).
- **F1 is open.** Its gate needs a second read-only run of the capability probe in liquid hours, a run with
  orders for the round trip and for what the account may trade, and then the two decision records on the
  access route and the instrument form ([runbook](docs/runbooks/ibkr-probe.md),
  [roadmap](docs/roadmap.md)). None of those exists.
- **F2 and F3 are open** and cannot be closed by code: both need five recorded trading days. The command that
  compares the control run with its recording trip by trip, which the registered experiment names, is
  written (`python -m hftlab.review.trip_costs`,
  [ADR 0026](docs/adr/0026-sleeve-state-across-a-restart.md)) and has read synthetic recordings only.
- **F4 is open:** its code is complete against the fake broker, and its gate is five trading days of the
  control sleeve at minimum size on the real paper account with zero reconciliation breaks
  ([`HFT-EXP-0002`](research/experiments/HFT-EXP-0002-control-min-size-paper/protocol.md), registered, not
  run).

- **F5 has not started as a phase.** Its foundations are written: features, labels, datasets, validation, a
  model file format, three kinds of model (below) and an ML sleeve skeleton. So is the Directional Change sleeve
  ([ADR 0024](docs/adr/0024-directional-change-sleeve.md)): a detector, a rule-based baseline in two
  variants and a random-walk null to read it against. It is tested on synthetic streams, has seen no real
  data, sits at stage `shadow` with placeholder numbers, and is expected to show no advantage over the
  control sleeve after costs
  ([`HFT-EXP-0005`](research/experiments/HFT-EXP-0005-directional-change-shadow/protocol.md), not run).
- **F6 is open:** the weekly loop is written. Weekend training registers a candidate, the trader serves a
  champion and challengers named by hash in a hash-chained lineup journal, and the weekly decision promotes,
  keeps or rolls back ([ADR 0023](docs/adr/0023-weekly-learning-loop.md),
  [runbook](docs/runbooks/learning.md)). As shipped it promotes nothing: no promotion rule exists, and the
  decision says `keep: rule not in force`. Its gate is two full weekly cycles without manual help and one
  rollback drill on the host; neither has happened. The scorer can run in a process of its own
  (`src/hftlab/scoring/`), written and tested on synthetic rows; it is off as shipped and has not run on the
  host.
- **A sleeve's state is kept across a restart** ([ADR 0026](docs/adr/0026-sleeve-state-across-a-restart.md)):
  saved when a session ends on purpose, and restored at the next start only when no journal and no tape moved
  since. Otherwise the sleeve starts cold and the owner is told why. The Directional Change sleeve's
  random-walk null can be run on a recorded day (`python -m hftlab.review.dc_null`). Both are written and
  tested on synthetic data; nothing of either has run on a host or seen real data.
- All of it has been run on synthetic streams the tests make, some with an effect planted in them on
  purpose, and against the fake broker. **No real data exists yet, no model has been trained on real data,
  and nothing here is evidence of an edge.** The first challenger's week is registered
  ([`HFT-EXP-0003`](research/experiments/HFT-EXP-0003-first-ridge-challenger-shadow-week/protocol.md), not
  run), and its expected outcome is no advantage over the control sleeve after costs.
- **History can be imported from files, and only from files**
  ([ADR 0028](docs/adr/0028-imported-history.md), [runbook](docs/runbooks/history-import.md)). The platform's
  own recordings come first. Before enough of them exist, the owner can export tick files himself, under
  terms he accepts and nobody here has verified, and `python -m hftlab.history import` reads them from a
  folder into a second store. It downloads nothing. Every imported day carries `source: imported`, a label,
  the SHA-256 of its input files and `terms_verified: false`; every dataset, report and map says how many of
  its rows were recorded and how many imported; and the weekly rule counts no imported day towards a
  promotion. It has read hand-written rows in tests only: no real export has been imported and no model has
  been fitted to one.
- **Gradient boosting is a second kind of model beside ridge**
  ([ADR 0025](docs/adr/0025-gradient-boosting-optional-set-and-whole-number-trees.md)). It is trained with one
  library from an optional requirement set (`make ml`), which the recorder and the trader never install or
  import, and the trained trees are kept and scored as whole numbers by the platform's own code. Ridge stays
  the default. It has been fitted to synthetic streams only; its first week is registered
  ([`HFT-EXP-0004`](research/experiments/HFT-EXP-0004-first-gbm-challenger-shadow-week/protocol.md), not run)
  and its expected outcome is no advantage over the control sleeve or the ridge challenger after costs.
- **A small sequence network is a third kind**
  ([ADR 0029](docs/adr/0029-sequence-network-windows-and-fixed-point-scoring.md)): one 1-D convolution and one
  hidden layer over a window of the last rows of a pair, about a thousand weights. Nothing is installed for
  it: it is fitted on the arrays the base requirements already hold, with its gradients written out and
  checked. Its weights are kept as whole numbers at a fixed scale and scored by integer sums and shifts, and
  the scorer abstains wherever that rounding could change a decision. A window never spans a gap, a closed
  market or a day's rollover. It trades through the same ML sleeve, starts as a challenger in shadow, and is
  promoted by the same rule or not at all. It has been fitted to synthetic streams only, on one kind of
  machine; its first week is registered
  ([`HFT-EXP-0006`](research/experiments/HFT-EXP-0006-first-seq-challenger-shadow-week/protocol.md), not run)
  and its expected outcome is no advantage after costs over the control sleeve, the ridge challenger or the
  tree challenger.

The [roadmap](docs/roadmap.md) has the state of each part.

## Run it locally

You need [uv](https://docs.astral.sh/uv/) and git. Python 3.12 is installed by uv if it is missing.

```
make bootstrap     # one .venv from the hash-locked requirement files
make ml            # optional: add the gradient-boosting library, needed only to train a gbm model
make hooks         # use the tracked git hooks: pre-commit, commit-msg, pre-push
make ci            # lint + typecheck + test + leakcheck: what CI runs
```

| Command | What it does |
|---|---|
| `make lint` | ruff, rules in `pyproject.toml` |
| `make typecheck` | `mypy --strict` on the whole `hftlab` package and `tools/` |
| `make test` | unit and property tests, then the research-registry lint |
| `make leakcheck` | scan the tracked tree and all history, then check the attestation of `HEAD` |
| `make attest` | rewrite and stage the leak attestation by hand (after a rebase or a merge) |
| `make lock` | recompile both lock files from the `.in` files, with hashes |

## Rules of the house

- **Paper only.** See [ADR 0001](docs/adr/0001-paper-only-lock.md).
- **One platform, one repository.** Nothing here imports another trading platform's code or reads its state.
- **Research is pre-registered.** A hypothesis, an experiment and a decision are written before any affected
  result is viewed. See the [research registry](research/README.md).
- **The repository is public.** No market data, recordings, model files, knowledge-base text or credentials are
  ever committed. See [ADR 0006](docs/adr/0006-public-repo-and-leak-mechanism.md).
- Automated contributors read [AGENTS.md](AGENTS.md) first.
