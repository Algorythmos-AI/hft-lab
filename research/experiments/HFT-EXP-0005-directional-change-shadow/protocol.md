# HFT-EXP-0005: the Directional Change sleeve's first run in shadow, on five recorded days

**Status: registered on 2026-10-11. Not run.** No quote has been recorded from the real feed yet, so there is
nothing to run it on. The sleeve has seen no real data. This folder holds no result, and no result of it
exists anywhere else.

It tests HFT-HYP-0005. The sleeve, its detector and its null are described in
[ADR 0024](../../../docs/adr/0024-directional-change-sleeve.md).

**Expected outcome: no advantage.** The likeliest result is that neither variant, at any threshold, beats the
control sleeve after costs, and that none stands out of its random-walk null. That would support the
hypothesis. The sleeve is a baseline to be judged, not a claimed edge.

## What is fixed

| | |
|---|---|
| Runner configuration | [`config.yaml`](config.yaml) in this folder, read as it stands |
| Sleeves | `idle`; `control` with seed 1, 2,000 per million, 60 seconds, as in HFT-EXP-0001; and six Directional Change sleeves |
| Variants | `trend`: enter in the direction of a confirmed event. `contrarian`: enter against the last event once its provisional overshoot is one threshold. Both leave on the next confirmed event the other way, or after four hours |
| Thresholds | 0.05, 0.10 and 0.20 percent of the price, each with both variants: six sleeves, each with its own book |
| Price series | The mid: half the sum of the best bid and ask |
| Warm-up | The first two events after a start or a gap are discarded. A gap is ten seconds without a confirmed book, or any moment the market was shut |
| Spread | No entry unless the threshold is worth at least five quoted spreads on the quote in hand |
| Rollover | No entry in the cost model's widened or no-fill window |
| Orders | Fill-or-kill market orders at the minimum size, on the shadow venue only. At most two entry orders an event, ten seconds apart |
| Shadow venue seed | 1 |
| Cost model, calendar | `config/cost_model.yaml` and `config/calendar/` as of the commit the run is made from. The cost model's parameters are the first entry of the journal |
| Null | 99 random walks of matched volatility for each recorded day and pair, seed 1 (`hftlab.strategy.dc.null`) |

These six configurations are six trials. The decision that closes this experiment adds them to
`config/trial_registry.yaml`.

## What has to exist first

1. Five recorded trading days (below).
2. The command that runs the null over a recorded day. The helper exists and is tested on synthetic streams;
   the command that hands it a recording and the shadow runner is **not built**.
3. The tool that reads round trips out of a journal, which HFT-EXP-0001 also waits for.

If the cost model is still a placeholder when the run is made, every figure of it is marked as not a result,
and the run is a test of the plumbing only.

## Data

The first five consecutive trading days that the recorder completes and that pass
`python -m hftlab.replay --day DIR --check`: the days the F2 gate counts, and the days of HFT-EXP-0001. The
pair is EUR.USD. The days are named by this rule before they exist; they are not chosen after looking at
them.

## The run

```
python -m hftlab.shadow --config research/experiments/HFT-EXP-0005-directional-change-shadow/config.yaml \
    --day <day 1> <day 2> <day 3> <day 4> <day 5>
python -m hftlab.shadow --verify var/shadow/HFT-EXP-0005/<day 1>.jsonl
```

One session over the five days, into a new journal. It is run once. The settings each sleeve detected its
events with are printed at the start, the price series among them. The null is then run day by day with the
same configuration, each of the six sleeves alone on each walk.

## What is measured

For each of the six sleeves, in millionths of the pair's quote currency:

- the number of events its detector confirmed, how many were flagged as warm-up, and the number of gaps;
- its completed round trips, and its orders that came to nothing;
- **gross:** the sum over its round trips of what the two fills made plus the modelled cost of the trip
  (`CostModel.round_trip_cost_micro`, halved, at the two quotes the fills met): the move of the mid it held
  through;
- **net of cost:** the same round trips after the spread, the slippage and both commissions, as the shadow
  book has them. A position still open at the end is reported and is not a round trip;
- the mean net result per round trip, and its day-block bootstrap interval at the level, draws and seed of
  `config.yaml`;
- the same for the mean net result per round trip less the control sleeve's;
- its net result on each day beside the 99 results of its null for that day, and the share of the null at
  or above it, as an exact fraction.

The control sleeve's and the idle sleeve's lines are reported as in HFT-EXP-0001. Gross and net are given
for every threshold tested, whatever they show. None is left out.

## What is expected

1. **The idle sleeve ends on exactly zero.** Anything else fails the experiment.
2. **No advantage, for every one of the six sleeves:** the lower bound of its mean net result per round trip
   is not above zero, or the lower bound of its mean less the control sleeve's is not above zero, or at
   least one in twenty of its null does as well. Row by row:
   - **supported:** any of the three holds, with at least 30 round trips on 5 days;
   - **refuted for that row:** both lower bounds above zero and a place among its null of one in a hundred,
     with at least 30 round trips on 5 days, on a cost model that is not a placeholder;
   - **inconclusive:** fewer round trips or days, or a placeholder cost model.
3. **Gross results near zero.** Before costs a baseline with no edge earns nothing on average. A gross
   result far from zero in either direction, in a sleeve whose net result is ordinary, is a reason to look
   for a defect first.
4. **The session replays identically.** A session that does not makes the run inconclusive and is a defect.

## What this run cannot show

- That either variant is profitable, in either direction: five days of one pair, in a shadow book.
- That a threshold is the right one. Three were fixed beforehand and all three are reported. Picking the best
  of six rows afterwards is what the trial count is there to pay for.
- Anything about a live account, and nothing that moves a sleeve out of `shadow`. No promotion rule exists.
- Whether events carry information about liquidity. No such score is built
  ([ADR 0024](../../../docs/adr/0024-directional-change-sleeve.md)).

## Afterwards

An experiment that is on `main` is frozen (research/README.md, rule 4), so the results do not come into this
folder. The run is recorded as a new experiment that names HFT-EXP-0005 and holds its figures and the
journal's last hash, never the journal or any market data. A decision record then cites both.
