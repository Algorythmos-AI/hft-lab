# HFT-EXP-0009: the first funding of the float on the real paper account

**Status: run once, on 2026-10-12 at 22:43 UTC. This is the record of that run, and of what the same trader
session showed in the hours after it, and of nothing else.**

It is a measurement of the venue and of this platform's order path. It tests no hypothesis, compares no
strategy and counts as no trial. No protocol was registered for it beforehand. These are the first orders of
this platform that the broker accepted, and the first fills. It names
[HFT-EXP-0008](../HFT-EXP-0008-capability-probe-orders-asian-session/report.md), the probe's run whose four
test orders were all rejected. The journals and the tape of the run stay on the owner's machine and are not in
this repository: they describe an account.

## What was run

The owner started the recorder and the trader with `make paper-local`
([runbook](../../../docs/runbooks/paper-trading-on-spot.md)) against the broker's paper account through its
gateway on the same machine, with the limits of
[HFT-DEC-0001](../../decisions/HFT-DEC-0001-paper-risk-limits-and-float.md) in force, and fourteen minutes
later asked the running trader to fund the float (`python -m hftlab.trader --fund --operator NAME`). The
settings are in [`config.yaml`](config.yaml). The design is
[ADR 0031](../../../docs/adr/0031-spot-inventory-on-a-cash-account.md).

The account held Australian dollars only. The plan was to sell 239,049 AUD.USD and buy 50,000 EUR.USD, which
leaves the float of each currency that `config/risk.yaml` asks for. The trader sent **seven market orders,
immediate-or-cancel, one at a time**, each through the risk gate and written to the outbox before it was sent:
five sells of AUD.USD, then two buys of EUR.USD. **The broker accepted all seven and filled each in full, in
one fill.** Nothing was rejected, nothing was partly filled, nothing was left working. Every sleeve was in
`shadow` throughout, so no sleeve order went to the broker.

It ran from the owner's laptop in Sydney, **not from the host** the platform is to run on.

## When in the trading week

The first order was written at 22:43:13 UTC on Monday 2026-10-12: 09:43 on Tuesday in Sydney, 18:43 on Monday
in New York. That is early in the Asian session, 1 h 43 min after the 17:00 New York rollover and outside the
hour after it in which the shadow cost model treats spreads as widened. London and New York were closed. It is
a quiet hour, and the orders were small against what was quoted: at most 47,810 units against one to five
million at the top of the book.

## What was measured

### The seven orders

Prices are as the tape holds them. "Quote before" is the best bid and ask the trader had last heard when it
sent the order; an unchanged book is silent, so that quote was up to eight seconds old. "Against the touch" is
the fill price against the side of that quote the order takes: the bid for a sell, the ask for a buy.

| # | Written (UTC) | Order | Quote before | Filled at | Against the touch | Commission |
|---|---|---|---|---|---|---|
| 1 | 22:43:13.175 | SELL 47,810 AUD.USD | 0.69593 / 0.69595 | 0.69593 | at the bid | 2.874 AUD |
| 2 | 22:43:18.282 | SELL 47,810 AUD.USD | 0.69593 / 0.69595 | 0.69593 | at the bid | 2.874 AUD |
| 3 | 22:43:23.413 | SELL 47,810 AUD.USD | 0.69593 / 0.69595 | 0.69593 | at the bid | 2.874 AUD |
| 4 | 22:43:28.540 | SELL 47,810 AUD.USD | 0.69593 / 0.69595 | 0.69593 | at the bid | 2.874 AUD |
| 5 | 22:43:33.665 | SELL 47,809 AUD.USD | 0.69592 / 0.69595 | 0.69593 | 0.00001 better than the bid | 2.874 AUD |
| 6 | 22:43:34.716 | BUY 25,000 EUR.USD | 1.11581 / 1.11585 | 1.11585 | at the ask | 2.874 AUD |
| 7 | 22:43:39.878 | BUY 25,000 EUR.USD | 1.11580 / 1.11585 | 1.11585 | at the ask | 2.874 AUD |

- **Slippage against the quote: none.** Six fills were at the touch and one was a tenth of a pip better. That
  is seven small orders in a quiet hour. It is not a measurement of slippage for the cost model: see "Not
  measured".
- The quoted spread at the time was 0.2 to 0.3 pip in AUD.USD and 0.4 to 0.5 pip in EUR.USD.
- All seven were reported as taking liquidity.
- The orders of one pair are about five seconds apart because the desk's limit is twelve orders a minute. The
  whole funding took **27.2 s** from the first order written to the last fill, and the trader closed it in the
  float's journal 0.6 s later: 27.7 s in all.

### The commission

- **Charged in Australian dollars, the account's own currency**, on a pair quoted in US dollars and on a pair
  that has no Australian dollar in it.
- **2.874 AUD on every fill**, 20.118 AUD for the seven. The broker's published schedule is 0.20 basis points
  of the trade value with a minimum of USD 2.00 an order. The largest of these orders was worth about
  USD 33,300, where 0.20 basis points is USD 0.67, so every one was charged the minimum, and USD 2.00 at the
  AUD.USD rate of the moment is 2.874 AUD. **The minimum is measured. The rate above the minimum is not**: no
  order here was large enough to reach it (that takes a trade worth more than USD 100,000).
- The commission is not on the fill. It arrived as a report of its own, 2 to 4 ms after the fill it belongs
  to, and the same seven reports came again each time the executions were asked for. The outbox holds each
  once.

### Timing, from a laptop in Sydney

From the moment the order was written to the outbox, which is the moment it was sent.

| # | The broker's acceptance seen after | The fill seen after | Fill after acceptance |
|---|---|---|---|
| 1 | 1,812 ms | 2,052 ms | 240 ms |
| 2 | 658 ms | 665 ms | 6.4 ms |
| 3 | 532 ms | 540 ms | 7.6 ms |
| 4 | 595 ms | 603 ms | 8.1 ms |
| 5 | 479 ms | 486 ms | 7.1 ms |
| 6 | 803 ms | 811 ms | 7.9 ms |
| 7 | 462 ms | 467 ms | 4.6 ms |

The median from sending to the fill is **603 ms**, and the first order of the connection took 2.1 s. **These
are not the platform's order timings**: they were taken from a laptop in Sydney, and the host is in the United
States, a different distance from the broker's servers. Nothing here goes into `order_round_trip_ms` in the
capability file. The shadow cost model's latency bound is 100 to 400 ms; all seven acceptances took longer
than its upper bound, from this machine.

### What the account accepted

- **A sell of spot was accepted and paid from the base currency the account held.** Five sells of AUD.USD,
  with Australian dollars in the account. HFT-EXP-0008 had tested buys only.
- **A buy of spot was accepted once the account held the quote currency.** Two buys of EUR.USD, paid from the
  US dollars the sells had just brought in. In HFT-EXP-0008 the same buy, without the dollars, was refused.
- Not shown: a sell of EUR.USD, a buy of AUD.USD, anything in GBP.USD, and that a sell of a currency the
  account does not hold is refused.

### Reconciliation while the connection lasted

The reconciler asks the venue once a minute for its executions, its positions and its cash, and compares the
positions with the book and the cash with the anchor plus what the book's fills moved, per currency, within
the tolerance of 5.00. It writes to its journal only when something differs.

- **The reconciliation journal is empty.** From the funding until the connection was lost, 3 h 31 min later,
  no check found a difference in positions or in cash. That is 211 checks after the funding.
- **Positions.** The first check after the funding, 31 s after the last fill, and every check after it, was
  answered with a position of -239,049 in AUD.USD and +50,000 in EUR.USD: the book's float line in each pair,
  exactly. So the gateway listed the conversions as positions in their pairs, as the design assumed.
- **Cash.** The same 211 answers held the same three balances. Against the trader's own figure the Australian
  dollars and the euros were equal to the millionth, and the US dollars were 0.00003 over: the gateway gives
  that balance to four decimal places. The Australian dollar figure includes the seven commissions, so the
  commissions are accounted in the right currency to the last digit.
- **A changed balance arrived within 31 s of the last fill.** That is the first check after it. The checks are
  a minute apart, so nothing finer is shown; the ten seconds the limits take for a balance to settle is not
  tested by this.

### After the gateway lost its connection to the broker: a reconciliation break

This is the same trader session, and it bears on the same assumption, so it is recorded here.

- At 02:15:10 UTC on 2026-10-13, 3 h 31 min after the funding, the gateway lost its connection to the broker's
  servers. It was back at 02:24:50, nine minutes and forty seconds later. Just before it came back the gateway
  said, for all four pairs, that there is no market data during a competing session of the login the paper
  account belongs to ([runbook](../../../docs/runbooks/paper-trading-on-spot.md), section 7).
- The trader treats that as a new connection and makes it safe before anything may be sent: it asks for the
  working orders, the executions and the positions, and compares the positions with the book.
- **From then on the positions were answered with nothing.** Each answer is a fresh request to the gateway.
  The executions were still answered with the seven fills, and the working orders with none. The book holds
  the float's two lines, so the positions differ: -239,049 against 0 in AUD.USD, and +50,000 against 0 in
  EUR.USD.
- The trader journalled the difference, opened a **reconciliation break**, marked the trading day as not
  clean, and stayed in the reconciling state. When this record was written, eight hours later, it was still
  there and the answer was still empty. No order was sent in that time. No sleeve was above `shadow`, so none
  was stepped down.
- While the trader is in that state the scheduled reconciler does not run, so the cash was last asked for at
  02:15:06. **Whether the cash still agrees after the reconnection is not in the journals.**
- Why the positions are no longer listed is not known. The tape holds what the adapter answered and nothing
  about the reason: whether the gateway lists such a position only during the connection it was made in, or
  only until some time of day, or whether something else happened in those ten minutes, a later run has to
  show.
- In that state the trader asks the three questions on every beat and the tape keeps every answer, each with
  the seven fills in it. The tape grew by about 85 MB an hour, against a quota of 1 GB for all the tape that is kept.

So the assumption ADR 0031 called the one that matters most held for as long as one connection lasted and did
not hold across the first reconnection.

## Findings

Stated as findings. None of them is a decision.

**(a) The account does trade spot on a float.** A sell is paid from the base currency and a buy from the quote
currency, as the probe's refusals implied, and both were accepted once the currency was there.

**(b) The commission is charged in the account's own currency, and its minimum is the published one.**
2.874 AUD an order, which is USD 2.00. On an order of the minimum size for good pricing (20,000 EUR.USD or
25,000 AUD.USD) that is about 0.9 to 1.2 basis points of the trade value, one way and not a round trip:
several times the published rate of 0.20.

**(c) Cash reconciles exactly, commissions included.** Anchor plus fills equalled the venue's cash in all
three currencies for three and a half hours.

**(d) Positions reconciled only until the first reconnection.** The gateway listed each conversion as a
position while the connection it was made on lasted, and listed none after the gateway had lost and regained
its connection to the broker. Reconciliation of positions as built cannot tell that from a position that has
gone, so it stops the trader: the safe side, and not a state paper trading can go on in. Reconciliation on
cash alone, which ADR 0031 names as the way out, is not built.

**(e) The quote shows our own order.** While the first sell was working, the size quoted at the ask rose from
1,000,000 to 1,047,810: by the size of the order. While the first buy was working, the best bid stood at the
ask price with a size of 25,000, for eight milliseconds. A sleeve that reads the top of the book will see its
own order in it.

## Not measured

- **The commission above the minimum.** Every order was charged the minimum.
- **Slippage on sleeve-sized orders in busy hours.** Seven fills in a quiet hour say nothing about London or
  New York, about a moving market, or about an order sent when a sleeve wants it and not when an operator
  does. `slippage_increments` in the cost model stays chosen, not measured.
- **Why GBP.USD was refused.** No order in GBP.USD was sent. It stays on the shadow venue only.
- **The weekly reset**, and the gateway's daily logout: what happens to the session, to the balances and to
  the listing of positions.
- **A round trip from the host.** Every timing here is from a laptop in Sydney.
- A sleeve's order, an entry and its exit, a partial fill, a reject by the broker, a limit order, a cancel.
- Taking the float back (`--unfund`), and a new anchor (`--reanchor`).
- How soon after a fill the balance changes, more finely than within 31 s.
- Whether the cash agrees after a reconnection, and what the gateway lists as positions on a fresh start of
  the trader or of the gateway.

## Seen and not explained

- The positions that are no longer listed after the reconnection; see above.
- The five sells filled at 0.69593. The spot contract's price increment is 0.00005, so a market order was
  filled at a price that is not on that grid. HFT-EXP-0007 and HFT-EXP-0008 saw quotes finer than the
  increment. Whether the increment binds the price of a limit order is still not shown.
- The fifth sell filled 0.00001 above the last bid the trader had heard. The bid had been at the fill price
  until 0.4 s before the order was sent.
- The first order took 1.8 s to be accepted and the others half a second or so. One first order is not a
  finding.
- The fill for the first order came 240 ms after its acceptance, the other six within 8 ms.

## What this record changes

Notes with their date, where a statement was waiting for this run. No limit, float size, stage rule, cost or
decision record is changed by it.

- [ADR 0031](../../../docs/adr/0031-spot-inventory-on-a-cash-account.md): what became of each point it marked
  UNVERIFIED.
- [`config/venues/ibkr.yaml`](../../../config/venues/ibkr.yaml) and
  [`config/cost_model.yaml`](../../../config/cost_model.yaml): a note in each that points here. No value in
  either is changed: the capability file's values are a probe's to set, and the cost model's are
  [HFT-DEC-0002](../../decisions/HFT-DEC-0002-shadow-cost-model-from-the-probe.md)'s.
- [ADR 0019](../../../docs/adr/0019-trader-loop-and-staged-rollout.md), the
  [trader runbook](../../../docs/runbooks/trader.md) and the
  [runbook for paper trading on spot](../../../docs/runbooks/paper-trading-on-spot.md): what the hold on the
  account's capability is today, and what the first real run showed.

## What it leaves open

- The break of 2026-10-13 is open as this is written. Ending it is the owner's to do and is not part of this
  record.
- [HFT-DEC-0003](../../decisions/HFT-DEC-0003-control-sleeve-to-minimum-size.md) takes the control sleeve to
  minimum size after one clean trading day. The trading day of 2026-10-13 is not one.
- [HFT-EXP-0002](../HFT-EXP-0002-control-min-size-paper/protocol.md) expects zero reconciliation breaks over
  five days. It is not run, and (d) says what would break it as things are.
