# HFT-EXP-0003: the first ridge challenger's week in shadow

**Status: registered on 2026-10-11. Not run.** Nothing has been recorded from the real feed, no model has
been trained on real data and none has served. This folder holds no result, and no result of it exists
anywhere else.

It tests HFT-HYP-0003. It is the first full weekly cycle of the learning loop
([ADR 0023](../../../docs/adr/0023-weekly-learning-loop.md)).

**Expected outcome: no advantage.** On real data the likeliest result is that the challenger does not beat
the control sleeve after costs, and the decision says `keep: rule not in force`. That would support the
hypothesis and would be the loop working as designed.

## What is fixed

| | |
|---|---|
| Settings | [`config.yaml`](config.yaml) in this folder: one pair, a holding time of 60 seconds, the feature windows, the label's wait and worst case, a ridge penalty of 1, and the numbers the week is measured with |
| Model | The ridge candidate that `python -m hftlab.learn.train` registers on the Saturday of the run, fitted to every recorded week up to then. It is named by its hash in the lineup journal, with the hash of its evaluation report |
| Place | `ml-challenger-1`, at `shadow` for the whole week. The champion's place `ml` is empty, so the comparison is with the `control` sleeve |
| Orders | Fill-or-kill market orders on the shadow venue only. Nothing is sent to the broker by any sleeve |
| Process | `hft-train.service`, `hft-promote.service` and `hft-trader.service` at one commit of `main` for the whole cycle |

## What has to exist first, and is not chosen here

1. The recorder has recorded at least two trading weeks of the pair: one to train on and to fold, one more
   so that the walk-forward has a test week.
2. The timers of the two weekend jobs are enabled by the owner ([runbook](../../../docs/runbooks/learning.md)).

The cost model and the feasibility settings are whatever the commit holds. If they are still placeholders,
the model's report says `not_a_result: 1`, and so does this experiment's result: the cycle is then a test of
the loop and not a measurement of the model.

## Data

The first trading week, Sunday's open to Friday's close, that begins after a Sunday decision has moved the
first registered candidate into the challenger's place. The week is named by this rule before it exists. A
week in which the trader did not run on all five trading days is still the week: it is a finding, and the
longer run is a new experiment.

## The run

Nothing is run by hand.

1. Saturday: `hft-train.service` registers the candidate.
2. Sunday: `hft-promote.service` keeps; the candidate becomes the challenger.
3. The trading week: the trader serves it in shadow.
4. The Sunday after: `hft-promote.service` weighs the week and writes a decision report, named by its hash in
   the lineup journal.

## What is measured

Everything is read from that one decision report (`challengers[0]`), in the challenger's pair and in
millionths of its quote currency. Results in two currencies are never added.

- `evidence.trades` and `evidence.days` of the challenger, and the control sleeve's count in `trades`.
- `evidence.result_lower_micro`: the lower bound, at the level and with the draws and seed of `config.yaml`,
  of the day-block bootstrap of the challenger's mean result per round trip after costs.
- `evidence.advantage_lower_micro`: the same bound for the challenger's mean less the control sleeve's.
- `evidence.drawdown_micro`: reported, not judged.
- `reconciliation_breaks`, `latched`, `sessions`, and whether the challenger's place served one model in
  every session (`model_not_served_all_week` among the reasons, or not).
- From the model's evaluation report: `not_a_result`, the three numbers of the walk-forward and the trial count.
- From the drift look of the same Sunday: every finding, with every threshold null.
- Whether every session of the five days replays identically.

## What is expected

1. **`action` is `keep`, with `rule_not_in_force` among the reasons.** Anything else fails the experiment:
   a promotion without a rule is a defect, whatever the numbers.
2. **`result_lower_micro <= 0` or `advantage_lower_micro <= 0`:** no advantage over the control sleeve after
   costs. This is the expected reading and it supports HFT-HYP-0003.
   - **supported:** either bound is zero or below, with at least 100 trades on 5 days;
   - **refuted for this week:** both bounds above zero, with at least 100 trades on 5 days, on a report with
     `not_a_result: 0`;
   - **inconclusive:** fewer than 100 trades or fewer than 5 days, the days of the two sleeves differ, the
     place served more than one model, or `not_a_result: 1`.
3. **Every session replays identically.** One that does not makes the week inconclusive and is a defect.
4. **Zero reconciliation breaks.** The run is in shadow, so one would be a defect of the trader.

## What this run cannot show

- That the model is profitable, in either direction: one week of one pair, in a shadow book.
- Anything about a live account.
- That the model should be promoted. No promotion rule exists. If the hypothesis is refuted, the next step
  is a registered rule (a decision record) and a longer experiment, with this configuration counted as a trial.
- The training run's evaluation is one configuration evaluated on development data. The decision that
  closes this experiment adds its row to the trial registry.

## Afterwards

An experiment that is on `main` is frozen (research/README.md, rule 4), so the results do not come into this
folder. The run is recorded as a new experiment that names HFT-EXP-0003 and holds the report's figures and
hashes, never the report, a model or any market data. A decision record then cites both.
