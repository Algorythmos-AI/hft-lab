# HFT-DEC-0001: the risk limits and the float for the first paper trading on spot

- **Date:** 2026-10-12
- **Status:** proposed. It is in force from the merge of the pull request that carries it.
- **approved_by: owner**, by merging that pull request. Until then it is a proposal and nothing more. The
  owner may change any value: each is one line of `config/risk.yaml`, with this record amended before the
  change reaches `main`, or a new record written after.
- **Rests on:** [HFT-EXP-0007](../experiments/HFT-EXP-0007-capability-probe-weekly-open/report.md) and
  [HFT-EXP-0008](../experiments/HFT-EXP-0008-capability-probe-orders-asian-session/report.md), the capability
  probe's two runs. Both are measurements of the venue. Neither is a test of a strategy.
- **Trials:** none. No configuration was evaluated to choose these numbers, so `config/trial_registry.yaml`
  gets no row.
- **Sets:** every value of `config/risk.yaml`, which it puts in force.

## What is decided

The risk gate ([ADR 0015](../../docs/adr/0015-risk-gate.md)) is put in force for a paper account that is run as
a cash account with a float ([ADR 0031](../../docs/adr/0031-spot-inventory-on-a-cash-account.md)). The limits
are sized for one purpose: the control sleeve placing orders of the venue's minimum size for good pricing, so
that the order path, the reconciliation and the cost model can be checked against real paper fills. They are
not sized for a strategy, and nothing here says a strategy has an edge.

No number below was fitted to a result. Where a number comes from a measurement it says which; the others are
choices, and say so.

## The pairs

| Pair | Where it trades | Why |
|---|---|---|
| EUR.USD | paper | Its test order was refused for want of US dollars and for nothing else. A float answers that |
| AUD.USD | paper | The same |
| GBP.USD | shadow only | Its test order was refused and the run did not capture why. Nothing shows that cash answers it |
| USD.JPY | shadow only | The account may not hold yen at all |

GBP.USD moves to paper by a probe run that captures the reason, and a new decision record.

## The values

One price increment is half a pip for both paper pairs.

| Limit | Value | Reasoning |
|---|---|---|
| Minimum entry | 20,000 EUR.USD, 25,000 AUD.USD | Measured: the size the venue's own warning names, under which an order is an odd lot and is priced worse. A paper fill priced like no normal fill would say nothing about normal fills |
| Position and order size per pair | two minimum sizes: 40,000 and 50,000 | Chosen. Room for a second sleeve later, and the largest position closes in one order |
| Position of the control sleeve | 25,000 in a pair | Chosen: one minimum size in either pair, which is all the `min_size` stage places |
| Price collar | 4 increments (2 pips) | Chosen. About ten times the measured median spread (0.2 pip in both pairs): wide enough not to refuse an ordinary market order, narrow enough to refuse a bad quote |
| Exit collar | 20 increments (10 pips) | Chosen. An exit must not be refused in a fast market; the loader refuses one narrower than the entry's |
| Spread guard | 2 increments (1 pip) | From the measurement: the widest spread seen in thirty ordinary seconds was 0.4 and 0.5 pip. Twice that, so that only an unusual book stops entries |
| Stale quote | 3,000 ms | From the measurement: the book changed one to three times a second, with gaps of 3 to 5.5 seconds. The trader confirms a quiet book every second, so this is three missed confirmations and not a quiet market |
| Open pairs | 2 | The two paper pairs |
| Orders per minute | 12 for the desk, 6 for the control sleeve | Chosen. The control sleeve is meant to send a few orders an hour. The desk's figure is also what a funding is paced by |
| Order-to-trade ratio | 10, over 600 seconds | Chosen. Every order is an immediate-or-cancel market order and is expected to trade |
| Reject streak | 3 | Chosen, and low on purpose: whether the broker takes a sell paid from the float's base currency is not yet shown by any order. Three refusals in a row stop entries until an operator looks |
| Loss latches in USD | desk 300 a day, 400 from the peak; control 200 and 300 | Chosen. One minimum-size position moves about 2 US dollars a pip, so the sleeve's daily latch is a hundred pips of adverse movement, or far more than its fees. The desk's is wider than the sleeve's so that the sleeve's trips first |
| Loss latches in AUD | desk 400 and 500; control 300 and 400 | The broker is expected to charge commission in the account's own currency, and a fee is a loss in that currency. Without a latch there the first fill would stop entries. Which currency it is charged in is not measured; the first paper fill shows it, and a commission in a third currency stops entries by itself (`UNLATCHED_CURRENCY`) |
| No entries around the rollover | 15 minutes before, 30 after | Chosen: the cost model widens spreads in that window, and no run has sampled one |
| No entries before the weekly close | 60 minutes | Chosen: nothing is carried over a weekend on purpose |
| Reconciliation grace | 240 seconds | Chosen. The gateway is expected to send a changed balance within three minutes; this is that and a minute |
| `max_gross_exposure`, `max_leverage` | null | Not enforced: nothing in the platform converts a currency |

## The float

| | Value | Reasoning |
|---|---|---|
| Hub | USD | The quote currency of both paper pairs: what every buy is paid with |
| Float of USD | 110,000 | Two minimum sizes long in both pairs at once cost 92,000 at 1.30 and 0.80, rates well above any recent one, and the reserve is on top. A test holds the file to this |
| Float of EUR | 50,000 | Two minimum sizes short (40,000), the reserve, and 5,000 over |
| Float of AUD | 60,000 | Two minimum sizes short (50,000), the reserve, and 5,000 over |
| Reserve | 10,000 USD, 5,000 EUR, 5,000 AUD | Chosen. It pays for an exit that costs more than the entry brought. Ten pips against the largest position is about 50 US dollars, so it is many times what an exit can need |
| Funding buffer | 50 basis points | Chosen: how much more of the hub is raised than the funding's buys are priced at, at the edge of their collar |
| Cash tolerance | 5.00 of each currency | Chosen. Balances are compared to the millionth; this allows for rounding of a balance by the broker and for nothing larger |
| Settle time | 10 seconds | Chosen: how long after a fill the broker's balance is taken to include it |

The float is worth roughly 210,000 US dollars and carries positions of at most 50,000 units a pair. On a paper
account that idle cash costs nothing. It is the reason a real account would not be run this way.

## What is not known, and what shows it

- Whether the broker takes a sell paid from the base currency of the float. The first paper sell shows it.
- Whether the gateway lists a spot conversion as a position in its pair, and reports a cash balance per
  currency promptly. The first funding shows both: if not, a reconciliation break opens within the grace
  time, nothing more is sent, and the float is taken back (ADR 0031, section 4).
- Which currency the commission is charged in. The first paper fill shows it.

## What would change this

A reconciliation break with no innocent cause, a refusal by the broker that the gate did not foresee, or a
latch that trips on fees alone. Each is a reason to stop, look, and write the next record.
