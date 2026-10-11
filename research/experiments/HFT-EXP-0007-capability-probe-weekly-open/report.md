# HFT-EXP-0007: the capability probe's first run against the real paper gateway, read-only, at the weekly open

**Status: run once, on 2026-10-11. This is the record of that run and of nothing else.**

It is a measurement of the venue. It tests no hypothesis, compares no strategy and counts as no trial. No
protocol was registered for it beforehand, so it names no earlier experiment; a later run of the probe is a
new experiment that names this one. The run's own report file stays on the owner's machine and is not in this
repository: it describes an account.

## What was run

The capability probe (`python -m hftlab.venues.ibkr.probe`, [runbook](../../../docs/runbooks/ibkr-probe.md)),
by the owner, against the broker's paper account through its gateway, with the settings in
[`config.yaml`](config.yaml). The gateway's Read-Only API was on and the probe was not asked to place orders.
**No order was placed.** The connection passed the paper-only lock: one managed account, a paper id.

## When in the trading week

It started on Sunday 2026-10-11 at 21:34:40 UTC, which is 17:34 in New York: about twenty minutes after the
weekly open, and inside the window after the 17:00 rollover in which the shadow cost model already treats
spreads as widened ([ADR 0013](../../../docs/adr/0013-shadow-cost-model.md)). The four pairs were sampled one
after the other, sixty seconds each.

**These are the worst minutes of the week to sample.** The update counts and the spreads below describe those
minutes and nothing else. They are not representative of normal hours, and **they are not inputs to the cost
model**: `config/cost_model.yaml` is unchanged by this record and is still a placeholder.

## What was measured

Contract descriptions, as the gateway gave them. "Resolves" means the gateway describes the contract in that
form. It does not show that the account may trade it.

| Pair | Resolves as spot | Resolves as a CFD | Price increment, spot | Price increment, CFD | Smallest order | Size increment |
|---|---|---|---|---|---|---|
| EUR.USD | yes | yes | 0.00005 | 0.00001 | 1 unit | 1 unit |
| USD.JPY | yes | yes | 0.005 | 0.001 | 1 unit | 1 unit |
| AUD.USD | yes | yes | 0.00005 | 0.00001 | 1 unit | 1 unit |
| GBP.USD | yes | yes | 0.00005 | 0.00001 | 1 unit | 1 unit |

The smallest order is the gateway's own minimum. It reports less than one unit for these pairs, and the
adapter takes that as one whole unit. It is the smallest order the gateway takes, not the size from which its
pricing is good.

What is on offer, sampled in the spot form:

| Pair | Market depth | Levels | Depth snapshots in 5 s | Historical ticks | Ticks returned for 100 asked |
|---|---|---|---|---|---|
| EUR.USD | available | 5 | 1 | available | 102 |
| USD.JPY | available | 5 | 4 | available | 102 |
| AUD.USD | available | 5 | 2 | available | 114 |
| GBP.USD | available | 5 | 1 | available | 107 |

The top of the book over sixty seconds, **at the weekly open, not an input to anything**. One spread unit is
0.00001 for the three dollar-quoted pairs and 0.001 for USD.JPY.

| Pair | Changes of the top of the book | Spread: least | Spread: median | Spread: most | Median, in pips | Crossed or locked |
|---|---|---|---|---|---|---|
| EUR.USD | 7 | 65 | 65 | 65 | 6.5 | 0 |
| USD.JPY | 76 | 68 | 78 | 80 | 7.8 | 0 |
| AUD.USD | 6 | 87 | 96 | 101 | 9.6 | 0 |
| GBP.USD | 3 | 139 | 139 | 139 | 13.9 | 0 |

The session:

| | |
|---|---|
| Managed accounts | 1, a paper id |
| Gateway clock against ours | about -0.8 s. The gateway answers in whole seconds, so this says only that the two agree to within a second |
| Clock request round trip | about 13 ms |
| Disconnects during the run | 0 |
| Messages from the gateway | 6, each saying that one of its data services was connected or connecting. None was an error |

## Not measured

- **Tradability.** No order was placed. Whether the account may trade any of the four pairs, in either form,
  is not known.
- **Commissions.** A commission is reported on a fill, and there was none.
- **The order round trip.** Nothing was sent, so nothing was acknowledged or cancelled.
- **The update rate and the spreads in normal hours.** The sample is of the minutes after the weekly open.
- **The weekly login:** whether the session needs a fresh login each week, and what the weekly reset looks
  like from the socket.
- **Whether the paper account mirrors the permissions of the live account** it belongs to. What the paper
  account is described and offered says nothing about that.
- The size from which the venue's pricing is good, and how a fill, a partial fill and a rejected order are
  reported.

## Seen and not explained

Quoted prices in the spot form were finer than the spot contract's price increment. USD.JPY, whose spot
increment is 0.005, was quoted at prices ending in 3, 4 and 6 in the third decimal; AUD.USD and GBP.USD,
whose spot increment is 0.00005, at prices ending in 1 and 2 in the fifth. EUR.USD happened to be on its
grid. The quote steps match the CFD increments. The adapter refuses an order whose limit price is off the
contract's increment, so a limit taken straight from a quote could be refused. Whether the increment the
gateway reports binds orders, quotes or neither is for the order round trip to show.

## What this record changes

- [`config/venues/ibkr.yaml`](../../../config/venues/ibkr.yaml) moves from `placeholder` to `partly_measured`
  and holds the two tables of contract descriptions and offers above. Its costs and timings stay null.
- `config/cost_model.yaml`: nothing.
- No decision follows from it. The access route and the instrument form need a run in liquid hours and an
  order round trip first, and neither exists.
