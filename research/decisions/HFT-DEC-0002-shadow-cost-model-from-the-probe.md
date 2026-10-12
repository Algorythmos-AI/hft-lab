# HFT-DEC-0002: the shadow cost model, from the probe's measurements and the broker's published commission

- **Date:** 2026-10-12
- **Status:** proposed. It is in force from the merge of the pull request that carries it.
- **approved_by: owner**, by merging that pull request. Until then it is a proposal and nothing more. The
  owner may change any value: each is one line of `config/cost_model.yaml`, with this record amended before
  the change reaches `main`, or a new record written after.
- **Rests on:** [HFT-EXP-0007](../experiments/HFT-EXP-0007-capability-probe-weekly-open/report.md) and
  [HFT-EXP-0008](../experiments/HFT-EXP-0008-capability-probe-orders-asian-session/report.md).
- **Trials:** none. No configuration was evaluated, so `config/trial_registry.yaml` gets no row.
- **Sets:** every value of `config/cost_model.yaml`, with `status: decided` and `effective_from: 2026-10-12`.

## What is decided

The shadow venue's cost model ([ADR 0013](../../docs/adr/0013-shadow-cost-model.md)) stops being a placeholder.
It becomes `decided`, not `measured`: no order of ours has filled, so no commission, no slippage and no
latency of the platform's own has been measured. Each value says which of three things it is.

| Value | Set to | What it is |
|---|---|---|
| Minimum order size | 20,000, and 25,000 where the base currency is USD or AUD | **Measured**, from the venue's own odd-lot warning on four test orders (HFT-EXP-0008) |
| Spread | not a number of the model | **Measured**, and already in every fill: the shadow venue fills against the recorded quote. The medians were 0.2 pip (EUR.USD, AUD.USD) and 0.4 pip (GBP.USD, USD.JPY), one thirty-second sample a pair in the Asian session |
| Latency, upper bound | 400 ms | **Measured, loosely**: just over the slowest first answer to an order in that run (384 ms), from a laptop far from the host. It is a round trip, so it overstates the way there |
| Latency, lower bound | 100 ms | **Chosen**: half the fastest first answer seen (200 ms) |
| Commission rate | 0.20 basis points of the trade value | **Published, not measured**: the broker's published schedule for spot currency |
| Commission minimum | 2.00 USD an order; 320 JPY for a yen-quoted pair | **Published, not measured.** The yen figure is the dollar minimum at the rate the probe sampled, rounded up. The broker charges in the account's own currency; the shadow book charges in the pair's quote currency |
| Slippage | 0 increments | **Chosen, NOT MEASURED.** Nothing has filled. The trader's slippage report (shadow fill against paper fill) measures it once the control sleeve is on paper |
| Stale quote | 3,000 ms | **Chosen from the measured quote rate**: one to three changes a second, gaps of up to 5.5 seconds |
| Rollover window | no fills 5 minutes before to 15 after; spreads three times wider from 30 before to 60 after | **Chosen.** No run has sampled a rollover |

## Why now, on so little

One thirty-second sample a pair is not a cost study. The alternative was to keep round placeholder figures
that were known to be wrong in one direction: a latency of 20 to 80 ms where the first measured answer took
200 to 385 ms, and a minimum size under the venue's own. A model that says where each number comes from, and
which were not measured, is more honest than a placeholder, and the control sleeve's first paper days are what
replaces the unmeasured ones.

## What it means for results

- A result computed with this model is still not a measurement of cost. It becomes one when the commission
  and the slippage come from fills, and the status becomes `measured` by a later record.
- A change of cost model is a change of how results are measured. Results before and after
  `effective_from` are not compared without saying so.
- At 20,000 units the commission minimum applies: 2.00 USD is about 0.9 basis points of such an order, more
  than four times the published rate. Minimum-size results are dominated by it.

## What would change this

The first paper fills (the commission as charged, and in which currency), the slippage report after the first
paper days, and a probe run from the host for the latency.
