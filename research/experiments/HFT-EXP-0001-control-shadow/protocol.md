# HFT-EXP-0001: the control sleeve in shadow, on five recorded days

**Status: registered on 2026-10-11. Not run.** No quote has been recorded from the real feed yet, so there is
nothing to run it on. This folder holds no result, and no result of it exists anywhere else.

It tests HFT-HYP-0001.

## What is fixed

| | |
|---|---|
| Runner configuration | [`config.yaml`](config.yaml) in this folder, read as it stands |
| Sleeves | `idle`, and `control` with seed 1 |
| Entry rate | 2,000 per million: a flat pair enters on that share of its quotes |
| Holding time | 60 seconds from the venue's time of the entry fill |
| Waiting time after an order that came to nothing | 10 seconds |
| Size | The minimum: the larger of the instrument's and the cost model's minimum order size |
| Orders | Fill-or-kill market orders, both ways |
| Shadow venue seed | 1 |
| Cost model | `config/cost_model.yaml` as of the commit the run is made from. Its parameters are the first entry of the journal |
| Calendar | `config/calendar/` as of the same commit |

## Data

The first five consecutive trading days that the recorder completes and that pass
`python -m hftlab.replay --day DIR --check`: the days the F2 gate counts. Every pair they hold. The days are
named by this rule before they exist; they are not chosen after looking at them.

## The run

```
python -m hftlab.shadow --config research/experiments/HFT-EXP-0001-control-shadow/config.yaml \
    --day <day 1> <day 2> <day 3> <day 4> <day 5>
python -m hftlab.shadow --verify var/shadow/HFT-EXP-0001/<day 1>.jsonl
```

One session over the five days, into a new journal. It is run once.

## What is measured

Money of different currencies is never added, so everything below is per pair.

- **Idle sleeve:** its line of the session summary.
- **Control sleeve**, for each completed round trip (an entry fill and the exit fill that closes it), from the
  journal and the recording:
  - `result`: what the two fills made, less both commissions, in millionths of the quote currency;
  - `cost`: half of `CostModel.round_trip_cost_micro` at the two quotes the fills met, each widened as the
    shadow venue widens it near the rollover;
  - `excess = result + cost`.
- A position still open at the end of the fifth day is reported and is not a round trip.

## What is expected

1. **The idle sleeve ends on exactly zero:** no decision, order or fill; quantity, realised, unrealised, fees
   and net all 0 in every pair. Exact. Anything else fails the experiment.
2. **The control sleeve's mean result per round trip is minus the mean modelled cost, within 10% of that
   cost.** For each pair, with sums over its round trips:
   - **pass:** `10 * |sum of excess| <= sum of cost`, and three standard errors of the mean excess are no
     more than 10% of the mean cost (the run was long enough to see a difference of that size);
   - **fail:** `10 * |sum of excess| > sum of cost` and the mean excess is more than three standard errors
     from zero;
   - **inconclusive:** anything else. A longer run is a new experiment.

   The experiment passes when every pair passes, and fails when any pair fails.
3. **Exact check, trip by trip.** While the per-order commission minimum applies, the excess of a round trip
   is exactly its direction times the move of the mid between its two fills times its size. The number of
   round trips for which that does not hold is reported. It is expected to be 0, and a single one is a defect
   to be found before anything else is read.

## What this run cannot show

- Whether the cost model's numbers are right. If the model is still a placeholder at the run, the run checks
  that the accounting agrees with the model and says nothing about what trading costs.
- Anything about a strategy. It evaluates no strategy configuration and adds no row to the trial registry;
  the decision that closes it confirms that.

## Afterwards

An experiment that is on `main` is frozen (research/README.md, rule 4), so the results do not come into this
folder. The run is recorded as a new experiment that names HFT-EXP-0001 and uses this `config.yaml` byte for
byte. A decision record then cites both and says whether the F3 gate is met; that decision is the owner's.

## Not built yet

The trip-by-trip comparison joins the journal's fills with the recording's quotes. No tool does that yet. It
is written, and tested on synthetic streams where the answer is known, before the run, and it reads the
journal and the recording and nothing else.
