# HFT Lab

> HFT Lab: high-frequency paper trading for the currency market — recorder, strategy sleeves, risk gate, weekly retraining (Python)

A fully automatic trading platform for the forex market that runs against an Interactive Brokers **paper**
account. It records every price update, decides, guards every order with an independent risk gate, books the
results twice, retrains at the weekend and promotes a new model only when it passes tests written down in advance.

|                 |                                                                                                              |
| --------------- | ------------------------------------------------------------------------------------------------------------ |
| **Status**      | Code for F1 to F3 is written and tested against a fake broker and synthetic streams only · the F1, F2 and F3 gates are open · nothing has recorded or traded yet · the host layer is written and tested off the host, and nothing is installed on any host · paper only · no live trading |
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

Code, and tests of it. **Nothing here has met the real market yet.**

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
| The research registry: its lint, the trial count, one hypothesis and one experiment, registered and not run | `src/hftlab/registry/`, `research/`, `config/trial_registry.yaml` |
| The leak check for a public repository, with its hooks | `tools/leakcheck.py`, `.githooks/` |
| The status snapshot contract, and its publisher (nothing has been sent yet) | `contracts/`, `src/hftlab/publish/` |
| The host layer: systemd units rendered from code, the pre-flight gate, the status command with its dead-man rule, alerts, backup | `src/hftlab/ops/`, `config/ops.yaml`, `config/preflight.json` |
| What the owner runs on the host: the pre-flight sampler, the install script, the status wrapper, the unit files | `deploy/` |
| Design records | `docs/` |

What that does and does not mean:

- **Proven against:** a deterministic fake broker, a stub of the broker library, synthetic quote streams, and
  recordings the tests make themselves.
- **Not done:** nothing has connected to a real gateway, nothing has been recorded from the real feed, and no
  experiment has been run on real data. There is no order manager yet. The recorder and the shadow runner
  cannot place a broker order; the one thing that can is the capability probe, on the paper account, when
  the owner runs it with `--orders`.
- **The host layer exists and is tested off the host only:** the units as text, the scripts in dry runs
  against a temporary directory, the publisher against a stand-in for the network. Nothing is installed on
  any host, no unit has been loaded by a service manager, and nothing has been published. Recording starts
  only after the owner has run the capability probe, the pre-flight day and the install
  ([runbook](docs/runbooks/install.md)).
- **F1 is open** until the owner runs the capability probe against the real paper account
  ([runbook](docs/runbooks/ibkr-probe.md)).
- **F2 and F3 are open** and cannot be closed by code: both need five recorded trading days.

The [roadmap](docs/roadmap.md) has the state of each part.

## Run it locally

You need [uv](https://docs.astral.sh/uv/) and git. Python 3.12 is installed by uv if it is missing.

```
make bootstrap     # one .venv from the hash-locked requirement files
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
