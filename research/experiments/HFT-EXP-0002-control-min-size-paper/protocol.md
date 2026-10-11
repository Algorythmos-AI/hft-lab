# HFT-EXP-0002: the control sleeve at minimum size on the paper account, for five trading days

**Status: registered on 2026-10-11. Not run.** Nothing has connected to a gateway, no order has been placed
on the paper account, and the risk limits are not in force. This folder holds no result, and no result of it
exists anywhere else.

It tests HFT-HYP-0002. It is the run the F4 exit gate asks for.

## What is fixed

| | |
|---|---|
| Sleeves | [`config.yaml`](config.yaml) in this folder: `idle`, and `control` with seed 1, 2,000 entries per million quotes, a holding time of 60 seconds and a waiting time of 10 seconds |
| Stage | `control` at `min_size` on all five days. Every order that adds to a position is the pair's minimum size |
| Orders | Fill-or-kill market orders, both ways, each through the risk gate and the order manager, and the same order on the shadow venue on the same turn |
| Process | `python -m hftlab.trader`, as `hft-trader.service`, at one commit of `main` for all five days |
| Shadow venue seed | The `seed` of `config/trader.yaml` as shipped: 1 |

## What has to exist first, and is not chosen here

1. The capability probe of F1 has run, and `config/venues/ibkr.yaml` lists the tradable pairs.
2. A decision record has put `config/risk.yaml` in force.
3. A decision record, approved by the owner, has named the control sleeve's step to `min_size`, and the
   rollout rule in `config/trader.yaml` names that record. The sleeve reaches the stage by the rule, at a
   day's close; nobody sets it by hand.

The run is made on whatever those three decide. It does not choose any of them, and their ids are in the
first entry of every session of the run.

## Data

The first five consecutive trading days, after the control sleeve's stage became `min_size`, that the stage
journal closes with a `day` line for `control`. The days are named by this rule before they exist. A day
that is not clean by the stage journal is still one of the five: it is a finding, not a day to replace.

## The run

Nothing is run by hand. The trader runs; after the fifth day:

```
python -m hftlab.trader --stages
python -m hftlab.trader --replay var/trader/days/<day 1>      # and the same for each of the five
```

## What is measured

Money of different currencies is never added, so everything below is per pair.

- **Reconciliation breaks:** the `break` lines of the five decision journals with `broken` 1, and the
  `break_opened` lines of the reconciliation journal.
- **Faults:** the `fault` lines of the outbox, and every latch the risk journal shows tripped.
- **Clean days:** the five `day` lines of the stage journal for `control`, with their causes.
- **Replay:** for each day, whether every session is reproduced.
- **Slippage, shadow against broker:** the `slippage` line that ends each session, summed over the five
  days for each pair. For an order both venues filled for the same quantity, the difference is the side
  times what the shadow venue charged less what the broker charged, per unit, in price increments, as an
  exact integer; positive means the shadow venue filled worse. Reported: `count`, `sum_increments`, the mean
  as `sum_increments` over `count`, `lowest_increments`, `highest_increments`, `inexact`, and the orders that
  were not compared (`broker_only`, `shadow_only`, `unequal`).
- **The idle sleeve:** its lines of each session's result, in both books.

## What is expected

1. **Zero reconciliation breaks and zero faults** in the five days. A single one fails the experiment.
2. **Five clean days** for `control`. A day that is not clean fails the experiment, with its cause.
3. **Every session of every day replays identically.** One that does not fails the experiment: something
   decided depended on something not recorded.
4. **The slippage is reported, and the cost model errs against us.** For each pair, `sum_increments` is
   zero or positive: on average the shadow venue filled no better than the paper account. That is the
   direction the cost model is meant to err in (ADR 0003, ADR 0013).
   - **pass:** `sum_increments >= 0` in every pair with a `count` of at least 100;
   - **fail:** `sum_increments < 0` in any such pair. The model is optimistic somewhere; that is a defect in
     the model, to be found before any result of the shadow book is read, and never a reason to read the
     broker's book instead;
   - **inconclusive:** a pair with a `count` below 100. A longer run is a new experiment.

   `lowest_increments` below zero is reported in every case: it is one order the shadow venue filled better
   than the broker, and each one is looked at.
5. **The idle sleeve ends on exactly zero** in both books: no decision, order or fill.

The experiment passes when 1, 2, 3 and 5 hold and every pair passes 4.

## What this run cannot show

- Whether trading is profitable. The control sleeve has no edge and is expected to lose the cost of trading.
- Anything about a live account. A paper fill at the displayed price is not evidence of a live fill, which
  is why the shadow book and not the broker's is the result of record.
- How large the cost model's error is. Only its direction is tested.
- It evaluates no strategy configuration and adds no row to the trial registry; the decision that closes it
  confirms that.

## Afterwards

An experiment that is on `main` is frozen (research/README.md, rule 4), so the results do not come into this
folder. The run is recorded as a new experiment that names HFT-EXP-0002 and holds the five days' figures. A
decision record then cites both and says whether the F4 gate is met; that decision is the owner's.
