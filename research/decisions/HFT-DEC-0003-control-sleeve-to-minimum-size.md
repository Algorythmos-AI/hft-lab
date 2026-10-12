# HFT-DEC-0003: the control sleeve goes from shadow to minimum size on paper after one clean trading day

- **Date:** 2026-10-12
- **Status:** proposed. It is in force from the merge of the pull request that carries it.
- **approved_by: owner**, by merging that pull request. Until then it is a proposal and nothing more. The
  owner may change it: the number of days is one line of `config/trader.yaml`.
- **Rests on:** HFT-DEC-0001 (the limits in force and the float) and HFT-DEC-0002 (the cost model), and on
  [HFT-EXP-0008](../experiments/HFT-EXP-0008-capability-probe-orders-asian-session/report.md) for what the
  account does with an order. It is the step that lets
  [HFT-EXP-0002](../experiments/HFT-EXP-0002-control-min-size-paper/protocol.md) begin; that experiment's
  protocol is frozen and is not changed by this.
- **Trials:** none. The control sleeve trades at random by construction and no configuration of it was
  evaluated, so `config/trial_registry.yaml` gets no row.
- **Sets:** in `config/trader.yaml`, the control sleeve's rule for `min_size`, its three numbers, and
  `clean_day_minutes`.

## What is decided

| | Value | Reasoning |
|---|---|---|
| Control sleeve, shadow to `min_size` | after **1** clean trading day | The plan said 5. The owner asked for paper orders sooner. One day still shows the things a day can show: the trader ran for a trading day without an unclean stop, a latch or a reconciliation break. Changing it back is one line |
| A day counts when the trader ran | 480 minutes of it | Chosen: eight hours, so that a short test run does not earn the step |
| Control sleeve to `normal` | no rule | Not decided. A null rule is never met |
| Every other sleeve | stays in shadow | Not decided. Their rules stay null |
| Control sleeve's entry rate | 50 per million quote events | Chosen, for cost: at the measured one to three changes a second a pair it is about one entry an hour over the four pairs, and so about ten round trips a day on the two paper pairs. At the commission minimum that is well under the sleeve's loss latch. In a busier session the rate of quotes, and so of entries, is several times higher |
| Holding time, waiting time after a refusal | 60 seconds, 10 seconds | Chosen |

## What has to be true as well

The rule is not the only gate. The step is taken at the close of a trading day only if all of these hold, and
each is checked by the trader, not by this record:

- the risk limits are in force (HFT-DEC-0001);
- no latch is tripped and no reconciliation break stands;
- the account can open what the sleeve needs: the float is funded and anchored
  ([ADR 0031](../../docs/adr/0031-spot-inventory-on-a-cash-account.md), section 9). Until the owner funds it,
  the step is held and the journal says `ACCOUNT_CANNOT_OPEN`.

So the order of events is: start the trader, fund the float, one clean trading day, and the control sleeve
places its first paper order in the next session.

## What the control sleeve is, and is not

It enters at random and holds for a fixed time. It has no edge and is expected to lose the modelled cost. It
is on paper to exercise the order path against a real paper account: fills, commissions, reconciliation of
positions and of cash, and the difference between a shadow fill and a paper fill. Its result is not a finding
about any strategy.

## What takes it back down

A latch that concerns it or a reconciliation break steps it down at once, by the trader's own rule. The owner
takes it down with `python -m hftlab.trader --demote control shadow --operator NAME`.
