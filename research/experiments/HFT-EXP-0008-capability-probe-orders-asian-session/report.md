# HFT-EXP-0008: the capability probe's second run against the real paper gateway, with orders, in the Asian session

**Status: run once, on 2026-10-12. This is the record of that run and of nothing else.**

It is a measurement of the venue. It tests no hypothesis, compares no strategy and counts as no trial. No
protocol was registered for it beforehand. It is the second run of the probe and names the first,
[HFT-EXP-0007](../HFT-EXP-0007-capability-probe-weekly-open/report.md), which was read-only and taken in the
minutes after the weekly open. The run's own report file stays on the owner's machine and is not in this
repository: it describes an account.

## What was run

The capability probe (`python -m hftlab.venues.ibkr.probe --orders`,
[runbook](../../../docs/runbooks/ibkr-probe.md)), by the owner, against the broker's paper account through its
gateway, with the settings in [`config.yaml`](config.yaml). The gateway's Read-Only API was off. The connection
passed the paper-only lock: one managed account, a paper id.

The probe sent **four orders, one per pair**: a BUY of one unit in the spot form, as a limit about five per
cent under the bid so that it could not trade. **The broker rejected all four. No order was left working and
no position changed.**

It ran from the owner's laptop in Sydney, **not from the host** the platform is to run on.

## When in the trading week

It started on Monday 2026-10-12 at 01:41:35 UTC: 12:41 in Sydney, 21:41 on Sunday in New York. That is the
Asian session, more than four hours after the weekly open and well outside the window after the 17:00
New York rollover in which the shadow cost model treats spreads as widened
([ADR 0013](../../../docs/adr/0013-shadow-cost-model.md)). It is an ordinary session, and not the busiest one:
London and New York were closed. The four pairs were sampled one after the other, thirty seconds each.

## What was measured

### The top of the book in an ordinary session

Thirty seconds per pair, spot. An update is a change of the best bid or ask, in price or in size; an unchanged
book is silent. One spread unit is 0.00001 for the three dollar-quoted pairs and 0.001 for USD.JPY, and a pip
is ten units.

| Pair | Updates in 30 s | Updates per second | Median gap between updates | Longest gap | Spread: least | Spread: median | Spread: most | Median, in pips | Mean, in units | Crossed or locked |
|---|---|---|---|---|---|---|---|---|---|---|
| EUR.USD | 60 | 1.99 | 345 ms | 3.3 s | 0 | 2 | 4 | 0.2 | 1.87 | 1 |
| USD.JPY | 71 | 2.36 | 342 ms | 3.2 s | 3 | 4 | 6 | 0.4 | 4.24 | 0 |
| AUD.USD | 32 | 1.06 | 423 ms | 5.5 s | 1 | 2 | 5 | 0.2 | 1.81 | 0 |
| GBP.USD | 88 | 2.93 | 339 ms | 3.4 s | 2 | 4 | 6 | 0.4 | 4.39 | 0 |

The shortest gaps were well under a millisecond (45 to 217 microseconds): updates arrive in small bursts, and
then nothing for a third of a second or for several seconds. One EUR.USD quote, the first of its window, was
locked: bid equal to ask. The sizes quoted at the top, in the first and last quote of each window, were
between one and six million units.

### What is on offer

| Pair | Resolves as spot | Resolves as a CFD | Market depth | Levels | Depth snapshots in the 5 s depth window | Ticks returned for 100 asked |
|---|---|---|---|---|---|---|
| EUR.USD | yes | yes | available | 5 | 27 | 111 |
| USD.JPY | yes | yes | available | 5 | 20 | 106 |
| AUD.USD | yes | yes | available | 5 | 24 | 101 |
| GBP.USD | yes | yes | available | 5 | 20 | 103 |

The contract descriptions (price increments, the gateway's smallest order of one unit, the size increment) are
the same as in HFT-EXP-0007. "Resolves" still means only that the gateway describes the contract.

### The four orders, and what the broker said

Each was a BUY of 1 unit, spot, limit about five per cent under the bid, good for the day.

| Pair | The broker's verdict | The broker's number | Class, in this platform's words | What it shows |
|---|---|---|---|---|
| EUR.USD | rejected | 201 | insufficient cash in the quote currency | To buy euros the order pays US dollars. The account holds no US dollars and may not borrow them |
| AUD.USD | rejected | 201 | insufficient cash in the quote currency | The same, paying US dollars for Australian dollars |
| GBP.USD | rejected | not captured | not captured | The order ended rejected. The message that says why is not in the report: see "What the probe got wrong" |
| USD.JPY | rejected | 201 | currency not permitted on the account | The account may not open a position in yen at all |

For EUR.USD and AUD.USD the shortfall the broker named was the whole cost of the one-unit order, in US dollars.
So on this account **spot currency is unleveraged conversion of cash the account already holds**: an order is
accepted only if the currency it pays in is already there.

Each order also drew a warning (the gateway's number 399) that its size is below the venue's minimum and is
routed as an odd lot. The minimum the warning names is in the base currency of the pair:

| Pair | Minimum named by the warning | In |
|---|---|---|
| EUR.USD | 20,000 | EUR |
| GBP.USD | 20,000 | GBP |
| USD.JPY | 25,000 | USD |
| AUD.USD | 25,000 | AUD |

That is the size below which an order is an odd lot, by the broker's own message. It is the first figure this
platform has for the size from which the venue's pricing is good, and it comes from a warning text, not from
a fill.

### Timing, from a laptop in Sydney

| Pair | First answer to the order | The rejection seen this long after the cancel was asked |
|---|---|---|
| EUR.USD | 384 ms | 7.3 ms |
| USD.JPY | 313 ms | 6.9 ms |
| AUD.USD | 200 ms | 6.7 ms |
| GBP.USD | 206 ms | 5.1 ms |

**These are not the platform's order timings**, for three reasons:

- they were taken from a laptop in Sydney, and the host is in the United States, a different distance from the
  broker's servers. They are not representative of it;
- the first column times the first sign of life from an order that was rejected a moment later. It is not the
  time to the broker's acceptance, because there was none;
- the second column does not time a cancel. Each order was already dead when its cancel went out, and the
  figure is how long after that request the rejection was seen. It does bound one thing: the rejection followed
  the first answer within a few milliseconds.

Nothing here goes into `order_round_trip_ms` in the capability file.

### The session

| | |
|---|---|
| Managed accounts | 1, a paper id |
| Gateway clock against ours | about -1.3 s. The gateway answers in whole seconds, so this says only that the two agree to within a second or two |
| Clock request round trip | about 0.5 ms |
| Disconnects during the run | 0 |
| Messages from the gateway | 5 saying that a data service was connected, then the warnings and rejections above, and 1 saying that a cancel came too late |

## An earlier attempt the same morning

A first attempt that morning measured nothing. The gateway answered lookups and sent no quotes, with the
broker's message that there is no market data during a competing session of the other login. The paper account
takes its market data from the login it belongs to, and while that login is active anywhere else the paper
session gets no quotes. The owner logged the other session out and ran again; this record is that second run.
No report of the first attempt is kept here. The number the gateway gives that message was not written down.

## What the probe got wrong

The probe that made this run reported things about the orders that are not so. All are visible in its report,
and all are corrected in the probe since (report schema 2):

- it said `placed: true` and `state_after_placing: WORKING` for each order. The gateway had acknowledged the
  order locally, and the broker rejected it a moment later. An order is placed when the broker accepts it;
- its `ack_us` therefore times the gateway's own first answer, not the broker's verdict;
- it then cancelled orders that were already dead, and reported the rejection as the state "after cancelling";
- it stopped listening as soon as the last order's state was known. The message that carries the reason for a
  rejection arrives just after the state, so the reason for the last order, GBP.USD, is not in the report. It
  is the other dollar-quoted pair of the three and most likely failed as EUR.USD and AUD.USD did, but **the
  report does not show that**, and the table above says so.

## Findings

Stated as findings. None of them is a decision.

**(a) The architecture's assumption about the quote rate is wrong for this feed.** The architecture document
says quotes update every few milliseconds. On this feed, in an ordinary session, the top of the book of a
major pair changed about **one to three times a second**, with a median gap of a third of a second and gaps of
several seconds. Updates within a burst are microseconds apart, but the feed as a whole is two orders of
magnitude slower than assumed.

**(b) On this account spot currency cannot be shorted, and cannot be bought without holding the currency
that pays for it.** A strategy that goes long and short one pair symmetrically, from a flat start, cannot run
on spot as the account is. Every sleeve this platform has that trades at all (the control sleeve, the ML
places, the Directional Change baseline) enters long or short from flat. Such a strategy could run only:

- from **cash balances held in each currency** of the pair (inventory): it could then sell what it holds and
  buy back, never going below zero in either currency; or
- on **CFDs**, which the gateway describes for all four pairs. Trading them needs a permission the account
  does not have. That permission belongs to the owner's real account, and asking for it is the owner's
  decision alone.

Whether either would work is not shown here. The SELL side was not tested.

**(c) Yen cannot be held, so USD.JPY is recordable and not tradable on spot here.** Its quotes, depth and
history arrive like any other pair's. An order that would leave the account holding or owing yen is refused.

## Not measured

- **Commissions.** A commission is reported on a fill, and there was none.
- **The SELL side.** The probe's orders were all buys. Whether the account may sell a currency it holds, and
  that it may not sell one it does not hold, are both untested.
- **Positions funded with cash in the right currency.** The account held none of the paying currency, so the
  run shows a refusal and not what an order with the cash behind it would do.
- **CFDs.** They resolve. The account has no permission to trade them, none has been requested, and no CFD
  order was sent.
- **The host's own order timing.** Everything here was timed from a laptop in Sydney.
- **The weekly login:** whether the session needs a fresh login each week, and what the weekly reset looks
  like from the socket.
- **The busiest hours.** London and New York were closed. The update rate in their overlap may be higher; this
  run does not say.
- A fill, a partial fill, and the round trip of an order the broker accepts.

## Seen and not explained

- As in HFT-EXP-0007, quoted prices in the spot form were finer than the spot contract's price increment:
  the three dollar-quoted pairs, whose spot increment is 0.00005, were quoted at prices ending in 2, 4, 6 and 8
  in the fifth decimal, and USD.JPY, whose spot increment is 0.005, at prices ending in 2 and 7 in the third.
  The probe's orders were priced on the contract's grid and were refused for other reasons, so whether the
  increment binds an order is still not shown.
- Four cancels were sent after dead orders and the gateway complained about one. It said nothing about the
  other three.

## What this record changes

- [`config/venues/ibkr.yaml`](../../../config/venues/ibkr.yaml): the update interval per pair with the session
  it was taken in, what the account did with a spot BUY per pair, and the venue's minimum for good pricing
  from the warning. Commission, the order round trip, the access route and the instrument form stay null.
- `config/cost_model.yaml`: no number. One thirty-second sample moves nothing; a decision record sets them.
- The architecture document, the README and the ADRs that state or lean on the assumed quote rate get a dated
  note that cites this record.
- No decision follows from it by itself. The two decisions F1 waits for, the access route and the instrument
  form, now depend on the owner's choice in finding (b).
