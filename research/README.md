# Research registry

Every hypothesis, experiment and decision about a strategy or about how results are measured is a file here,
in git. Nothing about a strategy's parameters or about the evaluation changes without an entry. The registry
is also the source of the **global trial count**: every configuration evaluated on development data is a
trial, and the count deflates every later performance statistic.

```
research/
 ├── hypotheses/    HFT-HYP-NNNN-<slug>.yaml   what we believe, why, and what would show it is wrong
 ├── experiments/   HFT-EXP-NNNN-<slug>/       config, results and a report; frozen once it exists
 └── decisions/     HFT-DEC-NNNN-<slug>.md     what was decided, when, on which experiments
```

## Records

| Record | What it is | State |
|---|---|---|
| [HFT-HYP-0001](hypotheses/HFT-HYP-0001-shadow-accounting-is-honest.yaml) | The shadow accounting is honest: a sleeve that never trades earns exactly zero, and one that trades at random loses the modelled cost | Registered. No evidence yet |
| [HFT-EXP-0001](experiments/HFT-EXP-0001-control-shadow/protocol.md) | The control sleeve in shadow on five recorded days, with its seed, entry rate, holding time and waiting time fixed | Registered. **Not run**: nothing has been recorded from the real feed yet. The trip-by-trip tool its protocol says is not built has been written since (`python -m hftlab.review.trip_costs`); the protocol is frozen |

| [HFT-HYP-0002](hypotheses/HFT-HYP-0002-order-path-agrees-with-the-broker.yaml) | The order path agrees with the broker, and the shadow venue fills no better than the paper account | Registered. No evidence yet |
| [HFT-EXP-0002](experiments/HFT-EXP-0002-control-min-size-paper/protocol.md) | The control sleeve at minimum size on the paper account for five trading days: zero reconciliation breaks expected, and the shadow-versus-broker slippage reported with the direction the cost model is meant to err in | Registered. **Not run**: no order has been placed, the risk limits are not in force, and the only connections to the paper gateway so far were the capability probe's (HFT-EXP-0007, read-only; HFT-EXP-0008, four test orders, every one rejected by the broker) |
| [HFT-HYP-0003](hypotheses/HFT-HYP-0003-first-ridge-challenger-has-no-advantage.yaml) | The first ridge challenger shows no advantage over the control sleeve after costs. Stated as the expected outcome, so that a week that seems to show an edge has something to refute | Registered. No evidence yet |
| [HFT-EXP-0003](experiments/HFT-EXP-0003-first-ridge-challenger-shadow-week/protocol.md) | The first ridge challenger's week in shadow beside the control sleeve: trained on Saturday, weighed the Sunday after, with the numbers it is measured with fixed and no promotion rule | Registered. **Not run**: nothing has been recorded or trained on real data |
| [HFT-HYP-0004](hypotheses/HFT-HYP-0004-first-gbm-challenger-has-no-advantage.yaml) | The first gradient-boosted challenger shows no advantage over the control sleeve or the ridge challenger after costs. Stated as the expected outcome | Registered. No evidence yet |
| [HFT-EXP-0004](experiments/HFT-EXP-0004-first-gbm-challenger-shadow-week/protocol.md) | A ridge and a gradient-boosted challenger in shadow for the same week beside the control sleeve, with the tree settings and the measuring numbers fixed, no search and no promotion rule | Registered. **Not run**: nothing has been recorded or trained on real data |
| [HFT-HYP-0005](hypotheses/HFT-HYP-0005-directional-change-baselines-have-no-advantage.yaml) | The two Directional Change baselines, trend and contrarian, show no advantage over the control sleeve after costs and do not stand out of a random-walk null. Stated as the expected outcome | Registered. No evidence yet |
| [HFT-EXP-0005](experiments/HFT-EXP-0005-directional-change-shadow/protocol.md) | Both variants at three thresholds in shadow on five recorded days, gross and net of cost for every threshold, beside the control sleeve and 99 walks of matched volatility | Registered. **Not run**: nothing has been recorded. Its protocol says the command that runs the null on a recording is not built; the protocol is frozen, and the command has been written since (`python -m hftlab.review.dc_null`) |
| [HFT-HYP-0006](hypotheses/HFT-HYP-0006-first-seq-challenger-has-no-advantage.yaml) | The first sequence-network challenger shows no advantage over the control sleeve, the ridge challenger or the gradient-boosted challenger after costs. Stated as the expected outcome | Registered. No evidence yet |
| [HFT-EXP-0006](experiments/HFT-EXP-0006-first-seq-challenger-shadow-week/protocol.md) | A ridge, a gradient-boosted and a sequence-network challenger in shadow for the same week beside the control sleeve, with the network's settings, its one seed and the measuring numbers fixed, no search and no promotion rule | Registered. **Not run**: nothing has been recorded or trained on real data |
| [HFT-EXP-0007](experiments/HFT-EXP-0007-capability-probe-weekly-open/report.md) | The capability probe's first run against the real paper gateway: read-only, four pairs, in the minutes after the weekly open. A measurement of the venue, with no hypothesis and no trial | **Run once, 2026-10-11, with results.** Four pairs resolve as spot and as a CFD, with five levels of depth and historical ticks. Tradability, commissions, the order round trip and normal-hours spreads are not measured, and its spreads are not inputs to the cost model |
| [HFT-EXP-0008](experiments/HFT-EXP-0008-capability-probe-orders-asian-session/report.md) | The capability probe's second run against the real paper gateway: with orders, four pairs, in the Asian session, from the owner's laptop. A measurement of the venue, with no hypothesis and no trial. It names HFT-EXP-0007 | **Run once, 2026-10-12, with results.** The top of the book changes about one to three times a second, with median spreads of 0.2 to 0.4 pip. The broker rejected all four one-unit spot BUY orders: the account holds none of the paying currency and may not borrow it, and may not hold yen at all. Nothing was left working. Commissions, the SELL side, CFDs, funded positions and the host's order timing are not measured |
| [HFT-DEC-0001](decisions/HFT-DEC-0001-paper-risk-limits-and-float.md) | The risk limits for the first paper trading on spot, and the float of a cash account: EUR.USD and AUD.USD on paper at the venue's minimum size for good pricing, GBP.USD and USD.JPY on the shadow venue only | **Proposed**; in force when the owner merges it. No configuration was evaluated |
| [HFT-DEC-0002](decisions/HFT-DEC-0002-shadow-cost-model-from-the-probe.md) | The shadow cost model stops being a placeholder: minimum sizes and latency bounds from the probe, the commission from the broker's published schedule and marked not measured, slippage not measured | **Proposed**; in force when the owner merges it. `decided`, not `measured` |
| [HFT-DEC-0003](decisions/HFT-DEC-0003-control-sleeve-to-minimum-size.md) | The control sleeve goes from shadow to minimum size on paper after one clean trading day, once the float is funded. Every other sleeve stays in shadow | **Proposed**; in force when the owner merges it. The plan said five days |

6 hypotheses and 8 experiments are recorded: 6 experiments are registered and not run, and 2 are records of
measurements that were made. 3 decision records are written: the
risk limits and the float, the cost model, and the control sleeve's first step. Each is a proposal that the
owner approves by merging it. None evaluated a configuration, so the trial registry is empty.

## Rules

1. **Pre-registration.** The hypothesis and the experiment's config are committed before any affected result
   is viewed. A decision cites the experiments it rests on.
2. **Ids.** `HFT-HYP-NNNN`, `HFT-EXP-NNNN`, `HFT-DEC-NNNN`: four digits, sequential within each kind, never
   reused. The slug is lower-case words joined by hyphens.
3. **One id, one record.** A hypothesis or decision is one file. An experiment is one folder (or one file).
   A registered experiment holds a protocol and its frozen config. The results of a run, and a measurement
   that had no protocol, are an experiment of their own with a `report.md`, which names the registered one
   if there is one.
4. **Experiments are append-only.** Once an experiment is on `main`, none of its files is edited, deleted,
   renamed or added to. A re-run, a corrected run or a longer run is a new experiment with a new id that
   names the old one.
5. **Hold-out.** A hold-out run is its own experiment and happens once per frozen config.
6. **Other repositories.** A record of another platform is cited as `repo:ID`, for example
   `options-lab:DEC-0011`. Records of this repository are cited bare: `HFT-EXP-0003`.
7. **Trials.** A decision that changes the number of evaluated configurations adds a row to
   [`config/trial_registry.yaml`](../config/trial_registry.yaml). Totals never decrease.
8. **Owner approvals** (a gate passed, a sleeve allowed to place paper orders) are decisions with
   `approved_by: owner`.
9. **Our own words.** Records quote nothing from course material, books or the private knowledge base. They
   cite it by id once citations exist.

## The lint

```
make test                                                                    # runs the tests, then the lint
PYTHONPATH=src .venv/bin/python -m hftlab.registry lint                      # ids, locations, uniqueness, references, trial rows
PYTHONPATH=src .venv/bin/python -m hftlab.registry lint --base origin/main   # also: no experiment on main was changed
```

`make test` runs the lint without a base. CI runs the second against the base of the pull request, which is how rule 4
is enforced.

| Finding | Meaning |
|---|---|
| `id` | A file in a record folder is not named `HFT-HYP-NNNN`, `HFT-EXP-NNNN` or `HFT-DEC-NNNN`, with an optional `-slug` |
| `location` | A record is in the folder of another kind, or a file named like a record is outside the three folders |
| `unique` | Two records share an id |
| `append-only` | An experiment that exists in the base commit was modified, deleted, renamed or added to |
| `reference` | A record cites another repository's id without `repo:` |
| `trials` | A trial-registry row names a decision that does not exist |
