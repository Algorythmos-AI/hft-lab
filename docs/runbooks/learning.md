# Runbook: the weekly learning loop

For the owner. The loop is two jobs at the weekend and one journal
([ADR 0023](../adr/0023-weekly-learning-loop.md)):

| When | Unit | What it does |
|---|---|---|
| Saturday | `hft-train.service` | Builds the dataset from the recordings, evaluates walk-forward, and registers a **candidate** model. It serves nothing |
| Sunday, before 17:00 New York | `hft-promote.service` | First the drift look, then the weekly decision: **promote**, **keep** or **roll back** |
| All week | `hft-trader.service` | Serves the champion and the challengers the lineup journal names, each in its own place |

**Nothing on this page has been followed on the host.** The loop has run on synthetic streams and against the
fake broker only. No model has been trained on real data and none has served.

**As shipped, the loop promotes nothing.** Every number of the promotion rule and every drift threshold in
`config/learn.yaml` is null. The decision says `keep: rule not in force` (section 4). A candidate still gets
its week in shadow; it cannot become the champion.

Where things are, on the host:

| What | Where |
|---|---|
| The lineup journal | `/home/hft/hft-lab/var/trader/lineup.jsonl` |
| Models and reports, each named by its SHA-256 | `/home/hft/hft-lab/var/learn/models/`, `/home/hft/hft-lab/var/learn/reports/` |
| The last training run's word | `/home/hft/hft-lab/var/learn/last-run.json` |
| The trial ledger | `/home/hft/hft-lab/var/learn/trials.jsonl` |
| Settings | `config/learn.yaml`, `config/trader.yaml` (the places), `config/ops.yaml` (the two times) |

Every command below is run from `/home/hft/hft-lab` as the user `hft`, with `PYTHONPATH=src` and the
installed interpreter: `sudo -u hft env PYTHONPATH=src MODE=paper .venv/bin/python -m hftlab.learn.promote ...`,
written `promote ...` from here on, and `train ...` and `drift ...` in the same way.

## 0. Which kind of model is trained

`model:` in `config/learn.yaml` says which kind a training run fits: `ridge` (the shipped value) or `gbm`
([ADR 0025](../adr/0025-gradient-boosting-optional-set-and-whole-number-trees.md)). One run fits one kind.
Changing it, or any number under `boosting:`, is a change of configuration: it goes through a pull request,
and the run that follows is counted as another trial.

**A `gbm` run needs the optional ML set on the host. Nothing else does:** not the recorder, not the trader,
not the weekly decision, and not the scoring of a gbm model that is already trained. Install it once
([install runbook](install.md), "The optional ML set"):

```
sudo sh deploy/bin/hft-install --with-ml
```

| You see | Cause | What to do |
|---|---|---|
| `train: refused: a gbm model is trained with lightgbm, which is in the optional ML set and is not installed` | `model: gbm` and the set is not in the environment. Nothing was counted and nothing registered | Install it as above, or set `model: ridge` |
| The same, with `libgomp` or `libomp` in the brackets | The library is installed and the system's OpenMP runtime is not | `sudo apt install libgomp1`, then run training again |
| `train: FAILED: the exported trees and the library's part ways on training row N` | The whole-number copy of the fitted trees does not send a row where the library does. Nothing was registered | Do not retry. It is a defect: keep the message and the commit, and open an issue |

To see what a `gbm` run would register without registering it: `train --dry-run`. It prints the report, and
says the memory the process peaked at. A real run writes that peak to
`var/learn/last-run.json` under `memory`, with the kind under `model`.

**Two kinds in one week** (as `HFT-EXP-0004` asks) are two runs on the same Saturday: the timer's, and one
more by hand with a settings file that differs in `model` alone, `train --config <that file>`. Each
registers its own candidate, and the trader needs a place for each (`challenger-1`, `challenger-2` in
`config/trader.yaml`).

## 1. Set the two times and enable the timers

The install script refuses to enable either timer. They are yours to enable, once.

1. Choose the two times. Training must not overlap the other desks' weekend jobs on this host. The decision
   must come after training has finished and before Sunday 17:00 New York.
2. Write them into `config/ops.yaml` under `learning:`, in this form and no other:

   ```
   train_at: "Sat *-*-* 03:00:00 America/New_York"
   promote_at: "Sun *-*-* 12:00:00 America/New_York"
   ```

3. `PYTHONPATH=src .venv/bin/python -m hftlab.ops.units render --out deploy/systemd`, commit the result through
   a pull request, and deploy it ([rollback runbook](rollback.md), "Steps").
4. `sudo systemctl enable --now hft-train.timer hft-promote.timer`
5. `systemctl list-timers 'hft-*'`

**You should see** both timers with the next Saturday and Sunday. **If the render says `units: ... is not`,**
the time is not a Saturday, or not a Sunday before 17:00, or not in New York time. Nothing was written.

## 2. The weekly cycle

| Step | What happens | What you should see |
|---|---|---|
| Saturday | Training registers a candidate | A `register` line in the lineup journal; `hft-status` shows `1 candidate(s) waiting` |
| Sunday | The decision keeps; the candidate becomes a challenger | A `keep` line; a page `Lineup: keep`; `1 challenger(s) in shadow` |
| The week | The trader serves the challenger in its place, in `shadow` | `trader --stages` lists `ml-challenger-1: shadow`; its trades are in the shadow book only |
| Next Sunday | The challenger's week is weighed | A page: `promote`, `keep` or `roll back`, with the first characters of the report it rests on |

A decision that changes nothing writes nothing and pages nobody. A week is decided once: a second run in the
same weekend says `refused: the week of ... is already decided`.

A running trader is asked, by a request file, to end its session; its next session reads the new lineup. A
place whose model changed starts again at the bottom: if its stage was `min_size` or `normal` it is
`stepped_down`, and the way up is its stage rule again ([trader runbook](trader.md), section 2).

## 3. Read a report

Every line of the lineup journal that a decision wrote names a report by its hash:

```
tail -n 1 var/trader/lineup.jsonl
python3 -m json.tool var/learn/reports/<report>.json
```

| In a decision report | It means |
|---|---|
| `action`, `reasons` | What was decided, and every reason code |
| `rule.in_force` | 1 only when every number is written **and** a decision record is named |
| `gates` | `record`: the named decision record exists with `approved_by: owner`. `limits`: the risk limits are in force. `unfit`: challengers whose own report cannot carry a promotion |
| `fitted_days_by_source` | For each challenger: the days its model was fitted to, by where they came from. `recorded` is the platform's own recordings; `imported:<label>` is history read from files ([history import](history-import.md)) |
| `challengers[].evidence` | `own_days`: the recorded days the model was fitted to, which must reach the rule's `min_days` (`too_few_own_days` when they do not; an imported day is never counted). `trades`, `days`, and the lower bounds of the day-block bootstrap: `advantage_lower_micro` against the champion (or the control sleeve while there is no champion) and `result_lower_micro` of its own result, per trade, in millionths of the pair's quote currency |
| `challengers[].reasons` | Empty only for a challenger that passed the rule and every gate |
| `reconciliation_breaks`, `latched` | The week's breaks, and whose loss latch tripped while serving which model |
| `lineup_before`, `lineup_after` | The lineup the decision was made on, by the journal's head hash, and the one it wrote |

An **evaluation report** (the one training writes, named beside the model in the lineup) says
`not_a_result: 1` while the cost model, the feasibility settings or the learning settings are placeholders.
A model with such a report is never promoted: `report_rests_on_placeholders`. The same holds for a run that
read an imported day without sizes (`placeholders.sizes: 1`).

Under `sources` the evaluation report says, for each of its results by name (`three_way`, `interval`,
`deflated_sharpe`, `hold_out`, `model`), how many of the rows it was computed on, and of the days they are
from, were `recorded` and how many `imported:<label>`. Training prints the same on one line per candidate.
Read it before reading the result: a number that rests on imported rows is not a number about this venue.

Two sleeves are compared in the challenger's pair only. Results in two currencies are never added.

## 4. What `keep: rule not in force` means

The rule in `config/learn.yaml` names no decision record, or one of its numbers is null. Nothing can be
promoted, however good a week was. This is the shipped state and it is not a fault.

What still happens while the rule is not in force:

- candidates move into shadow and have their week;
- a champion that trips its own loss latch is rolled back (section 6): a rollback needs no rule;
- if every number is written and only `decision` is null, the report holds the week's evidence measured with
  those numbers. It is a measurement. It promotes nothing.

## 5. How a promotion rule comes into force

Never by editing a number. In this order:

1. A hypothesis and an experiment are registered in `research/` before the affected result is viewed
   (`HFT-HYP-0003` and `HFT-EXP-0003` are the first challenger's).
2. A decision record `research/decisions/HFT-DEC-NNNN-<slug>.md` sets every number of the rule and holds the
   line `approved_by: owner`. It lands on `main` through a pull request.
3. The same pull request writes the numbers and `decision: HFT-DEC-NNNN` into `promotion:` of
   `config/learn.yaml`.
4. The risk limits are in force by their own decision record (`config/risk.yaml`).

A promotion then needs all of: the rule's own checks, the record found with the owner's approval, the limits
in force, a week with no reconciliation break, a report that does not rest on placeholders, and the same
model served in the challenger's place in every session of the week. One missing: `keep`, with the reason.

## 6. A rollback, and the rollback drill

**A real rollback needs nobody.** When the champion's own loss latch trips, the trader steps its place down
at once and pages (`Latch tripped: DAILY_LOSS_SLEEVE/ml/...`). At the next decision the lineup goes back to
the champion before it, with a page `Lineup: roll_back`. Resetting the latch is still yours
([trader runbook](trader.md)); do it after reading the week's report, not before.

**The drill** proves the same path without a loss. It works on a copy of the journal and leaves the lineup
in force as it was.

1. The market must be shut. `promote --drill`
2. **You should see** `Rollback drill passed on <model>: {"gone": 1, "loads": 1, "restored": 1, "rolled_back": 1}.
   The lineup in force is unchanged.` and a page `Rollback drill`.
3. `tail -n 1 var/trader/lineup.jsonl` shows a `drill` line with the report's hash. `hft-status` shows
   `drill passed`.

| If it says | Then |
|---|---|
| `refused: the lineup holds no challenger and no candidate to drill with` | Nothing is registered. Run after the first training |
| `refused: the market is open` | Wait for the weekend |
| `FAILED` with `"loads": 0` | A model file or report of the lineup is missing or is not what its name says. Never rename a file to fit. The nightly backup does not hold `var/learn/` yet (see the end of this page): a model that is gone is trained again, and until then its place runs idle |
| `FAILED` otherwise | Keep the journal and the drill's report, stop, and open an issue. `hft-status` warns `rollback_drill_failed` until a drill passes |

The gate of F6 asks for one drill on the host, after two full weekly cycles that needed no manual help.

## 7. The drift look

`hft-promote.service` runs it before the decision. By hand: `drift --dry-run` prints the report and asks for
nothing.

Three monitors per sleeve with a model: the shift of each feature against the bins kept at training, what the
model expected of its trades against what they made, and the median spread against the training median. With
any threshold in `config/learn.yaml` null (as shipped) everything is computed and nothing is breached.

With thresholds in force, a breach asks the trader to take the sleeve down to `shadow` and pages
`Drift: <sleeve>`. The trader journals what came of it (`request` lines of the day's decisions). It refuses
while the sleeve holds a position on paper (`NOT_FLAT`); the next look asks again. The way back up is the
sleeve's stage rule.

A threshold is set like the promotion rule: by a decision record, never by hand.

## 8. When training fails

`sudo journalctl -u hft-train.service -n 30`, and `cat var/learn/last-run.json`.

| It says | It means | Do |
|---|---|---|
| `refused: the market is open` | It was started on a trading day | Nothing. It runs on Saturday |
| `refused: ... holds no recorded day` or `does not describe` | The recordings are missing or a day cannot be read | [Recorder runbook](recorder.md). No candidate this week |
| `refused: the dataset was not made` | The look-ahead check failed: a feature saw the future | Stop. Open an issue. Do not train until it is understood |
| `FAILED: peaked at ... bytes, over the budget` | The run needed more memory than `memory_budget_mb` | Nothing was registered. The budget and the slice's cap are the owner's to change, with the host's measurements |
| The unit was killed (`oom-kill` in the journal) | The slice's cap bound before the run's own budget | The same |
| `FAILED` otherwise | A journal or a file could not be used | Keep the files. Open an issue |

A failed training registers nothing. The week's decision still runs: with no candidate it keeps, and the
champion goes on serving. A week without training is not an incident; two in a row is worth an issue.

**If the lineup journal does not verify** (`hft-status` says `lineup_journal_broken`, and pages): the trader
serves no model from it, every ML place runs idle, and the other sleeves go on. The file is never repaired.
Keep it, stop both timers, and open an issue with the file.

## Not here yet

- **The backup does not hold the lineup journal, the models or the reports.** `hft-backup.service` hands over
  the recordings and the shadow journal. Until it also takes `var/trader/lineup.jsonl` and `var/learn/`, a
  lost disk loses the lineup's history, and every model is trained again from the recordings.
- **The scorer in its own process** (`scorer: process` on an ML place) is described in ADR 0023 and its state
  is said there. As shipped every place scores in the trader's process.
