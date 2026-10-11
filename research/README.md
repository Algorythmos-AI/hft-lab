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
| [HFT-EXP-0001](experiments/HFT-EXP-0001-control-shadow/protocol.md) | The control sleeve in shadow on five recorded days, with its seed, entry rate, holding time and waiting time fixed | Registered. **Not run**: nothing has been recorded from the real feed yet |

| [HFT-HYP-0002](hypotheses/HFT-HYP-0002-order-path-agrees-with-the-broker.yaml) | The order path agrees with the broker, and the shadow venue fills no better than the paper account | Registered. No evidence yet |
| [HFT-EXP-0002](experiments/HFT-EXP-0002-control-min-size-paper/protocol.md) | The control sleeve at minimum size on the paper account for five trading days: zero reconciliation breaks expected, and the shadow-versus-broker slippage reported with the direction the cost model is meant to err in | Registered. **Not run**: nothing has connected to a gateway, and the risk limits are not in force |
| [HFT-HYP-0003](hypotheses/HFT-HYP-0003-first-ridge-challenger-has-no-advantage.yaml) | The first ridge challenger shows no advantage over the control sleeve after costs. Stated as the expected outcome, so that a week that seems to show an edge has something to refute | Registered. No evidence yet |
| [HFT-EXP-0003](experiments/HFT-EXP-0003-first-ridge-challenger-shadow-week/protocol.md) | The first ridge challenger's week in shadow beside the control sleeve: trained on Saturday, weighed the Sunday after, with the numbers it is measured with fixed and no promotion rule | Registered. **Not run**: nothing has been recorded or trained on real data |
| [HFT-HYP-0004](hypotheses/HFT-HYP-0004-directional-change-baselines-have-no-advantage.yaml) | The two Directional Change baselines, trend and contrarian, show no advantage over the control sleeve after costs and do not stand out of a random-walk null. Stated as the expected outcome | Registered. No evidence yet |
| [HFT-EXP-0004](experiments/HFT-EXP-0004-directional-change-shadow/protocol.md) | Both variants at three thresholds in shadow on five recorded days, gross and net of cost for every threshold, beside the control sleeve and 99 walks of matched volatility | Registered. **Not run**: nothing has been recorded, and the command that runs the null on a recording is not built |

There is no decision record yet, and the trial registry is empty.

## Rules

1. **Pre-registration.** The hypothesis and the experiment's config are committed before any affected result
   is viewed. A decision cites the experiments it rests on.
2. **Ids.** `HFT-HYP-NNNN`, `HFT-EXP-NNNN`, `HFT-DEC-NNNN`: four digits, sequential within each kind, never
   reused. The slug is lower-case words joined by hyphens.
3. **One id, one record.** A hypothesis or decision is one file. An experiment is one folder (or one file).
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
