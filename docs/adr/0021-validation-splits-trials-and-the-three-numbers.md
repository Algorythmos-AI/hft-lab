# ADR 0021: Validation: splits by week, a hold-out read once, counted trials, three numbers

- **Status:** accepted, 2026-10-11. Built and tested on synthetic rows and streams only. Nothing has been
  validated on real data, and nothing here is evidence of an edge.
- **Context:** F5 and F6, foundations

## Context

[ADR 0008](0008-learning-loop.md) listed what validation must contain and left the definitions open. Each of
them has a careless version that reports a result which is not there:

- a split that lets a label train a model before the label's own window has ended;
- a hold-out that is looked at, adjusted for, and looked at again;
- a best-of-many result reported as if it were the only one tried;
- a model's result reported with nothing beside it, so that nobody can see it is what a coin earns.

## Decision

### 1. Walk-forward by trading week, with purge and embargo

`hftlab.learn.validate.walk_forward` makes one fold per trading week: test on that week, train on the weeks
before it. A week is named by the Friday close that ends it (`hftlab.calendar`).

- **Purge.** A training row is kept only if its label's window had ended before the first decision of the
  test week. The purge goes by each row's own `end_ns`, however long its window is.
- **Embargo.** On top of that, a whole embargo must have passed. The embargo is the longest label window the
  dataset was built with: the longest horizon and one latency.
- A plan records, for every fold, where its training and test rows begin and end, as times and row counts.
- The tests plant a label whose window reaches into the next fold and show it removed from that fold's
  training rows and back in the fold after; a property test holds every fold of generated rows to the rule.

### 2. Hold-out weeks are read once per frozen configuration

The last weeks of a dataset are in no fold. `HoldOut.read(config)` hands them over for the configuration with
that hash, and writes the read into the trial ledger before it happens. A second read by the same
configuration of the same hold-out is refused, by another process as well: the ledger is a file.

### 3. Every evaluated configuration is counted, twice

- `config/trial_registry.yaml` is the count a reviewer signs, one row per decision record.
- The trial ledger (`hftlab.registry.ledger`) is the count the code keeps: a hash-chained journal on the host,
  one line per evaluation, written before the evaluation is scored. The same frozen configuration evaluated
  twice is one trial.
- The number that deflates a statistic is the larger of the two (`counted_trials`). A registry row that was
  forgotten, or a ledger that is missing, cannot make a result look better.

### 4. Three numbers, on the same rows, every time

`three_way` scores on the same rows and under the same costs:

| | |
|---|---|
| Oracle | In hindsight, the better side wherever one pays and no trade elsewhere. No strategy can have it; nothing scored on the same rows can beat it. It is marked as hindsight in the report |
| Model | The side the model takes, or none. The model is handed the feature row and nothing else of the dataset row |
| Random | A side by a seeded coin on every row |

The no-trade baseline is the fourth line and is zero. Each line carries its trades, total, wins, losses and
the sums won and lost, so the win rate and the payoff are both there and neither stands alone. If the oracle
makes nothing, no row pays after costs and there is nothing to model.

### 5. A day-block bootstrap, exact

The confidence interval of the mean result per trade after costs resamples whole trading days. Trades of one
day stand or fall together, because they are not independent: label windows overlap, and a day has a mood.

- Every replicate is a fraction of two integers; the bounds are nearest-rank values that were drawn.
- The draws are a hash of the seed, the draw and the slot. There is no generator and no position in a
  sequence, so the interval is the same on any machine.
- The same function gives the interval of a challenger's advantage over a champion, drawing the same days for
  both.

### 6. The deflated Sharpe ratio takes the number of trials counted

`deflated_sharpe(results, trials=..., trial_variance=...)` is the probability that the Sharpe ratio of the
results beats the best that this many worthless trials would be expected to show (Bailey and López de Prado,
2014). `trials` has no default. A Sharpe ratio here is per period, says which period, and is not annualised.

### 7. Where floats are

The Sharpe statistics are floats: a square root and the normal distribution cannot be avoided. They are
confined to those functions of `learn/validate.py`, and a scan holds that. Their moments are computed exactly
and each ratio is converted once. They are numbers for a report. No decision of the platform compares them,
and no replay depends on them.

Everything else in validation, and all of the promotion decision, is integers and fractions.

### 8. The promotion decision is a function

`hftlab.learn.promote.decide(champion, challenger, rule)` returns promote, keep or roll back, with reason
codes, from the shadow results of the two by trading day and a rule that names the decision record which
registered it:

- a minimum of trades and of days;
- the bootstrap lower bound of the challenger's advantage after costs above zero;
- the lower bound of the challenger's own result above zero;
- a drawdown bound;
- no reconciliation break and no breached latch.

A champion that breached its latch is rolled back whatever the challenger did, and nothing is promoted on that
decision. A rule that names no decision record passes nothing. The function reads no file and no clock, moves
no model and sets no stage.

## Consequences

- The promotion gate of [ADR 0008](0008-learning-loop.md) now has a definition that can be tested, and a
  forced rollback is one of its tests. The process that would call it weekly, move a model and tell the trader
  is not built.
- The two-sided trial count costs a file on the host. Without the ledger the registry's number is used, which
  is the weaker of the two guards.
- The hold-out guard is a discipline of the code path, not a lock on the data: a dataset's rows are in the
  hands of whoever built it. It stops the accident, and it leaves a line in a chained file for the rest.
- The check that the challenger's own result is above zero is stricter than ADR 0008's list. A challenger that
  loses less than a losing champion is kept out by it.
- The deflated Sharpe ratio needs the variance of the trials' own ratios. Where those were not kept, the
  variance of a ratio with no skill over the same number of periods is offered instead, and says what it is.
- A float statistic can differ in its last digit between two builds of the mathematics library. That is why
  nothing is decided on one.
