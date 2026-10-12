# ADR 0013: The shadow venue's cost model

- **Status:** accepted, 2026-10-11. The model's shape is decided here. Every number in
  `config/cost_model.yaml` is still a placeholder, to be replaced by the capability probe's measurements.
- **Context:** F3, shadow venue and control

## Context

[ADR 0003](0003-two-books.md) made the shadow book the result of record and said its fill model is
conservative on purpose. It did not say what the model is. A paper account answers the question "what would
this order have got?" in the most flattering way: the displayed price, at once, for any size. A platform that
trades many times a day lives or dies on the few price units between that answer and an honest one.

There is no tape to calibrate against. So the model is a short list of explicit rules, each one leaning
against us, with every parameter in a dated file.

## Decision

The shadow venue (`hftlab.venues.sim`) implements the venue port, is fed quotes from outside (a recording or
a live quote source), and answers orders by these rules.

1. **Latency.** An order reaches the venue after a delay drawn, with a seed, from a configured range. It
   meets the quote that exists then, not the one the decision saw.
2. **A marketable order crosses the spread and pays slippage.** A buy pays the ask plus a configured number of
   price increments; a sell gets the bid less the same. If that breaks the order's limit, nothing fills.
3. **Displayed size is all there is.** A marketable order gets no more than the size on display. The rest of
   an immediate order is cancelled, and a fill-or-kill order fills whole or not at all. The rest of a resting
   one waits for the next quote and takes that quote's displayed size.
4. **A resting limit order fills only when it is traded through.**
   - Not at the touch: our place in the queue is unknown, so a quote at our price proves nothing.
   - At its own price, never a better one.
   - For no more than the size displayed on the side that came through. A buy that the ask has fallen
     through is held to the ask's size; a sell to the bid's. The rest goes on resting. A quote through our
     price says someone would deal there for that size; it does not say more traded.
5. **An order is aggressive or passive by the first book it can be judged against.** Normally that is the
   book it arrives to. If there is nothing to judge by on arrival (rule 6), an immediate order is cancelled
   with the reason, and a resting one is judged by the first usable quote after. A limit order judged late
   and found marketable is filled at its own limit, not at the better price then on the screen: it may have
   rested and been taken at its limit while nobody could see, and the model takes the worse of the two.
6. **Sometimes nothing fills:**
   - on a quote the source has not confirmed, by a new quote or a heartbeat, within a configured time;
   - inside a window around the daily rollover at 17:00 New York;
   - while the market is shut: the weekend, or a holiday from the calendar.
7. **Near the rollover the spread is taken as wider than quoted.** In a larger window around it, the quoted
   spread is multiplied by a configured factor: each side moves away by half the difference, rounded up to
   the price grid.
8. **Commission is a rate on the order's traded value with a minimum per order.** An order that fills in
   pieces pays the rate once and the minimum once. It is charged in the pair's quote currency.
9. **A size floor.** An order below the model's minimum order size is rejected on arrival.
10. **Exact, and rounded against us.** Prices are integer price units and money is integer millionths
    ([ADR 0002](0002-integer-prices-and-nanosecond-timestamps.md)). Where a fraction must be dropped, the
    cost is rounded up. The model can be too costly by less than a millionth and never too cheap.
11. **The parameters are dated configuration.** `config/cost_model.yaml` carries a version, an
    `effective_from` date and a status. Any status but `measured` loads as a placeholder. The parameters and
    that flag are written at the head of every shadow journal and into every feasibility map, so a figure
    cannot part from the costs it was computed under.
12. **One function states what a round trip costs.** `CostModel.round_trip_cost_micro` is half the spread at
    each end, the slippage at each end and both commissions, doubled so that it stays an integer. The F3
    check and the feasibility map both read it; neither keeps a second copy of the arithmetic.
13. **Changing the model is an evaluation change.** It needs a hypothesis, an experiment and a decision
    record, like a change to a strategy.

## Consequences

- The model is checked before any strategy is trusted: a sleeve that never trades earns exactly zero, and a
  sleeve that trades at random loses the modelled round-trip cost ([ADR 0014](0014-sleeves-as-pure-functions.md)).
  Today that holds on synthetic streams. On recorded days it is the F3 exit gate.
- No result computed on the placeholder numbers is a result. The probe measures latency, minimum size and
  commission; the rollover windows and the slippage need recorded days.
- What the model leaves out, stated so that nobody assumes it is there:
  - market impact, and any size beyond the top of the book;
  - financing of a position held over the rollover;
  - a venue that rejects for reasons other than size;
  - commission in the account's base currency. A broker charges it there. The book converts no currency: a
    fee in another currency is carried beside the net result, by currency, and is not in it.
- Rule 5 leaves one thing unknown and says so: what happened to a resting order while nothing could be judged.
  A market order judged late gets the first usable quote's price, as there is no other price to give it. A
  limit order judged late gets its own limit, the worst it could have got. No sleeve rests orders today.
- Rule 4 can understate a passive strategy: real resting orders are sometimes filled at the touch. That is
  the direction the model is meant to err in.
- If measured paper fills ever show the model to be optimistic, that is a defect in the model
  ([ADR 0003](0003-two-books.md)), fixed here and recorded as a decision.

## Note, 2026-10-12: the first measurements, and one thing the model does not know

The capability probe's second run ([`HFT-EXP-0008`](../../research/experiments/HFT-EXP-0008-capability-probe-orders-asian-session/report.md)) measured, in one thirty-second sample per
pair in the Asian session: median spreads of 0.2 pip (EUR.USD, AUD.USD) and 0.4 pip (USD.JPY, GBP.USD); a
median of a third of a second between changes of the top of the book; and the size below which the venue
routes an order as an odd lot, 20,000 units for EUR and GBP base and 25,000 for USD and AUD base. It measured
no commission and no latency that describes the host. **No number in `config/cost_model.yaml` is moved by
it:** one sample is not a calibration, and a decision record sets them.

"A venue that rejects for reasons other than size" is on the list above of what the model leaves out. The same
run showed how much that is on this account: the broker refuses a spot order that would short a currency, or
buy one without holding the currency that pays. The shadow venue fills both. A shadow result of a sleeve that
goes long and short on spot therefore describes trades this account would not take, whatever its costs say.
The model is not changed here. The trader refuses any stage above `shadow` while the capability file says so.
