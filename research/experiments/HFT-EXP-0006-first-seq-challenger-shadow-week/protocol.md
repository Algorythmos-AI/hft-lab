# HFT-EXP-0006: the first sequence-network challenger's week in shadow

**Status: registered on 2026-10-11. Not run.** Nothing has been recorded from the real feed, no model of any
kind has been trained on real data and none has served. This folder holds no result, and no result of it
exists anywhere else.

It tests HFT-HYP-0006. It is the first weekly cycle with a third kind of model
([ADR 0029](../../../docs/adr/0029-sequence-network-windows-and-fixed-point-scoring.md)), and it runs after
the weeks of HFT-EXP-0003 and HFT-EXP-0004, never in them.

**Expected outcome: no advantage.** On real data the likeliest result is that the sequence-network
challenger beats none of the control sleeve, the ridge challenger and the gradient-boosted challenger after
costs, and the decision says `keep: rule not in force`. That would support the hypothesis and would be the
loop working as designed.

## What is fixed

| | |
|---|---|
| Settings | [`config.yaml`](config.yaml) in this folder: the pair, the holding time, the feature windows, the label's wait and worst case, the tree settings and the measuring numbers of HFT-EXP-0004, and the network: a window of 8 rows, a kernel of 3, 8 filters, 16 hidden units, 30 passes, one seed |
| Models | Three candidates that `python -m hftlab.learn.train` registers on the Saturday of the run from the same recordings: `model: ridge`, `model: gbm` and `model: seq`, the settings otherwise the same. Each is named by its hash in the lineup journal, with the hash of its evaluation report |
| Places | `ml-challenger-1`, `ml-challenger-2` and `ml-challenger-3`, all at `shadow` for the whole week. The champion's place `ml` is empty, so each is compared with the `control` sleeve by the weekly decision |
| Orders | Fill-or-kill market orders on the shadow venue only. Nothing is sent to the broker by any sleeve |
| Process | `hft-train.service`, `hft-promote.service` and `hft-trader.service` at one commit of `main` for the whole cycle, with the optional ML set installed from that commit's lock for the tree candidate (`hft-install --with-ml`). The network needs nothing installed beyond the base requirements |

## What has to exist first, and is not chosen here

1. The weeks of HFT-EXP-0003 and HFT-EXP-0004 have been recorded as their own experiments.
2. The recorder has recorded at least two trading weeks of the pair.
3. `config/trader.yaml` has the third challenger's place and its rollout entry, through a reviewed commit.
4. A dry run of the `seq` settings on those recordings (`train --dry-run`) is not refused for memory. If it
   is, this experiment is not run as written: a shorter window or fewer weeks is another experiment.

The cost model and the feasibility settings are whatever the commit holds. If they are still placeholders,
each model's report says `not_a_result: 1`, and so does this experiment's result: the cycle is then a test of
the loop and not a measurement of a model.

## Data

The first trading week, Sunday's open to Friday's close, that begins after a Sunday decision has moved all
three registered candidates into the three challengers' places. The week is named by this rule before it
exists. A week in which the trader did not run on all five trading days is still the week.

## The run

1. Saturday: `hft-train.service` registers the ridge candidate, as every week.
2. **The two steps by hand.** After it ends, and still with the market closed, the owner runs the same
   command twice more: once with a settings file that differs in `model: gbm` alone, and once with one that
   differs in `model: seq` alone. The weekly unit trains one kind a week; another candidate is another run.
   The command refuses while the market is open.
3. Sunday: `hft-promote.service` keeps; the three candidates become the three challengers, in the order they
   were registered.
4. The trading week: the trader serves all three in shadow, beside the control sleeve.
5. The Sunday after: `hft-promote.service` weighs the week and writes a decision report, named by its hash in
   the lineup journal.

## Trials

The three candidates are three configurations evaluated on the same development weeks. Each is written into
the trial ledger before it is scored, and the last report says how many were counted. Nothing is searched:
the network's settings and its one seed are the ones in `config.yaml`, and a fit with another seed is another
trial and another experiment. The decision that closes this experiment adds its row to the trial registry.

## What is measured

From the one decision report, for each of `challengers[0]` (ridge), `challengers[1]` (gbm) and
`challengers[2]` (seq), in the pair they traded and in millionths of its quote currency. Results in two
currencies are never added.

- `evidence.trades` and `evidence.days` of each challenger, and the control sleeve's count in `trades`.
- `evidence.result_lower_micro`: the lower bound, at the level and with the draws and seed of `config.yaml`,
  of the day-block bootstrap of the challenger's mean result per round trip after costs. Part (a).
- `evidence.advantage_lower_micro`: the same bound for the challenger's mean less the control sleeve's.
  Part (b).
- **The seq challenger's mean less the ridge challenger's, and less the gbm challenger's.** Parts (c) and
  (d). The same bound, by `hftlab.learn.validate.advantage` with the same level, draws and seed, over the
  challengers' round trips by trading day as `hftlab.lineup.decide.read_week` reads them from the week's
  journals. No command prints these numbers today. If one is written, it is written and merged before the
  week begins.
- `evidence.drawdown_micro`: reported, not judged.
- `reconciliation_breaks`, `latched`, `sessions`, and whether each place served one model in every session.
- From each model's evaluation report: `not_a_result`, the three numbers of the walk-forward, and the trial
  count. From the seq model's report: `rows.dropped`, with how many rows ended no window and why.
- From the seq run's `last-run.json`: the memory it peaked at and how long it took.
- From the seq model's file: what it was fitted with, and its hash. If the same recordings are ever fitted on
  another machine, whether that hash is the same.
- From the drift look of the same Sunday: every finding, with every threshold null.
- Whether every session of the five days replays identically.

## What is expected

1. **`action` is `keep`, with `rule_not_in_force` among the reasons.** Anything else fails the experiment:
   a promotion without a rule is a defect, whatever the numbers.
2. **At least one of the four lower bounds is zero or below:** no advantage. This is the expected reading
   and it supports HFT-HYP-0006.
   - **supported:** any of (a), (b), (c), (d) is zero or below, with at least 100 trades on 5 days for the
     seq challenger, and for (b), (c) and (d) for the sleeve it is compared with;
   - **refuted for this week:** all four above zero, with at least 100 trades on 5 days for each of the four
     sleeves, on reports with `not_a_result: 0`;
   - **inconclusive:** fewer trades or days than that, the days of the sleeves differ, a place served more
     than one model, the seq run was refused for memory, or `not_a_result: 1`.
3. **Every session replays identically.** One that does not makes the week inconclusive and is a defect.
4. **Zero reconciliation breaks.** The run is in shadow, so one would be a defect of the trader.

## What this run cannot show

- That any of the models is profitable, in either direction: one week of one pair, in a shadow book.
- Anything about a live account.
- That a model should be promoted. No promotion rule exists.
- That a sequence is better or worse than a single row in general: one setting and one seed of the network,
  all placeholders.
- With three challengers there are three comparisons with the control sleeve and three between them. The
  level of the intervals is not adjusted for their number, and a rule that judged several challengers would
  have to say so in its decision record (ADR 0023).

## Afterwards

An experiment that is on `main` is frozen (research/README.md, rule 4), so the results do not come into this
folder. The run is recorded as a new experiment that names HFT-EXP-0006 and holds the reports' figures and
hashes, never a report, a model or any market data. A decision record then cites it.
