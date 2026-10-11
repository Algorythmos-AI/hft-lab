# ADR 0024: The Directional Change detector, two baseline sleeves on it, and the null they are read against

- **Status:** accepted, 2026-10-11. Built and tested on synthetic streams only. It has seen no real data, no
  number in its configuration is chosen, and it stays at stage `shadow`.
- **Context:** F5, strategies

## Context

Directional Change is a published way of reading a price series in its own time. Nothing is sampled on a
clock. An event is recorded each time the price has turned back from its running extreme by a fixed share of
that extreme, so a busy hour holds many events and a quiet night few.

Three things about the published method decide what is built here.

- **It describes events, not trades.** It contains no trading rule and no profit figure. Whatever trades on
  the events is ours, and it starts with no evidence at all.
- **Its best-known regularity also appears where there is nothing to earn.** After a confirmed turn of one
  threshold the price runs on, on average, by about one threshold more. A driftless random walk does exactly
  the same. Seeing the pattern in a recording therefore says nothing about an edge.
- **At a small threshold the costs take most of the move.** A turn of a few spreads is paid for with one
  spread and two commissions ([ADR 0013](0013-shadow-cost-model.md)).

The detector is also the easiest place in the platform to read ahead by accident: the size of a move is
known only once the next one has begun.

## Decision

### 1. The detector is a pure fold over ticks, one machine per threshold

`hftlab.strategy.dc.detector`. It trades nothing and imports nothing that could.

1. **The price is the mid, as an exact rational:** half the sum of the best bid and ask. The series is a
   named setting, `mid` is the only value it takes, and it is part of the settings a run logs. A float is
   refused.
2. **A machine has a mode, up or down, and the running extreme of that mode:** the highest price while up,
   the lowest while down. On a tick, a price beyond the extreme becomes the extreme and nothing else happens.
3. **Otherwise the move against the mode is divided by the extreme, and at the threshold or more an event
   confirms.** The comparison is inclusive. The mode switches and the confirming price becomes the new
   extreme. There is one event for each switch, so events alternate.
4. **An event is booked at the tick that crossed:** that tick's own price and receive time, never the level
   where the threshold stood. It carries its direction, the time and price of the confirmation, the time and
   price of the extreme it turned from, the ticks since the event before, and the machine's event counter.
5. **A tick that repeats the price of the tick before is dropped** before any machine sees it. Ticks are
   taken in the order they arrived and are never resampled to a clock.
6. **The move after a confirmation, the overshoot, has two forms, and they are two types.**
   - `Provisional`: from the last confirmation to the extreme so far. It changes with the ticks and is read
     from the state.
   - `Completed`: from a confirmation to the extreme the next event turned from. It is built in one place,
     the confirmation of that next event, and travels on that event. No function returns one from a state.
   Both are a share of the confirming price. The total move, from extreme to extreme, is a share of the first
   extreme and exists only on a `Completed`.
7. **The first events are warm-up.** The starting mode is arbitrary, so the first events of a machine say
   more about the start than about the market. A configured number of them (two by default) is flagged, and a
   flagged event is not traded on. The same holds after a gap: the extreme is forgotten, the overshoot in
   hand is dropped and completed by nothing, and the mode and the counter go on.
8. **A state is written out and read back exactly,** as whole numbers, so a restart does not put a machine
   with a large threshold back into its warm-up. A state written under other settings is refused.
9. **A threshold is an exact share of the price.** Configuration writes it in percent, as a quoted decimal,
   says so in the key's name, and converts once.

### 2. Two sleeves, both baselines

`hftlab.strategy.dc.sleeve`, through the sleeve interface of [ADR 0014](0014-sleeves-as-pure-functions.md).
One sleeve is one variant on one threshold; several thresholds are several sleeves, each with its own book.
Both variants read only confirmed events that are not warm-up, and the provisional state.

| | Enters | Leaves |
|---|---|---|
| `trend` | In the direction of a confirmed event | On the next confirmed event the other way, or after the maximum holding time |
| `contrarian` | Against the last event, once its provisional overshoot is a configured multiple of the threshold | The same |

- **One position per pair, at the minimum size,** with fill-or-kill market orders, like the control sleeve.
- **An event is good for one position.** It can be entered on while it is the last confirmed event and no
  entry for it has filled. The event that ends a position of the `trend` variant is therefore also the one it
  enters on next, as soon as its book is flat.
- **A warm-up event causes no order, entry or exit.** A position that a gap leaves without its event is ended
  by the next event that counts, or by the maximum holding time.
- **No entry when the threshold is small against the spread.** The threshold's share of the mid must be at
  least a configured multiple of the quoted spread on the quote in hand, and a multiple below one is refused
  as a setting. A crossed book is never entered on.
- **No entry in the cost model's widened or no-fill windows around the rollover, and none while the market is
  shut.** Exits are still sent: a position is never left alone because the hour is bad.
- **The control sleeve's retry discipline.** After an order that came to nothing the pair waits a fixed time.
  On top of that an event gets a bounded number of entry orders. An exit is retried until the book is flat,
  one order per wait.
- **A gap is decided by the data's own times:** a book unconfirmed for longer than a configured time, or a
  moment between two ticks at which the calendar says the market was shut. A change of connection or a
  restart is not a gap by itself; the time it took may be.

They are baselines because the published method gives no rule to trade by. These two are the plainest things
one can do with an event, written down before anything is measured. The expected outcome is no advantage
over the control sleeve after costs.

### 3. Every result is read against a random walk of the same volatility

`hftlab.strategy.dc.null`. From a stream of quotes it makes driftless walks that keep every tick's time and
sizes and its spread (widened by one price increment where that is needed to keep both sides on the price
grid), and replace the path of the mid: each tick moves it up or down by a whole number of increments, or
not at all, with the probability at which the mean squared change per tick equals the stream's. The draws
are a hash of a seed, the walk's number and the tick's number, so a walk is the same from any starting
point. A helper runs a sleeve on a number of such walks and returns its results, and a second one places the
result on the real stream among them as an exact fraction.

A result on a recording is reported beside that distribution, and beside the control sleeve's cost on the
same days. A result inside the distribution has shown nothing.

### 4. What is deliberately not built

- **No liquidity score built on the events.** The published evidence for one is thin: too thin to gate a
  trade on. It would first have to be tested on our own recordings, as its own registered experiment.
- **No scaling laws as trading targets.** The check that the mean overshoot on a walk is about one threshold
  is a test of the detector and nothing else.
- **No position sizing, no pyramiding, no stop other than the next event and the clock.**
- **No sleeve that trades several thresholds as one book.**

## Consequences

- The detector's tests state its definition: hand-worked sequences in both directions, the confirming tick's
  own price when a tick jumps past the level, the inclusive comparison, dropped repeats, warm-up after the
  start and after a gap, an exact restore, and a property over generated series that events alternate and
  that the events of a prefix are a prefix of the events.
- On a seeded walk the mean completed overshoot is within a stated tolerance of the threshold and the mean
  total move of twice it. On a grid a move confirms at the first grid point at or beyond the level, which
  alone puts both figures a known few percent above the continuous ones; the tolerance says so.
- On a driftless walk the sleeves' results are indistinguishable from the cost of their round trips, as the
  control sleeve's are. On a stream with a reversal pattern planted in it the `trend` variant is separated
  from the null, and on a plain walk it is not. That checks the plumbing and says nothing about any market.
- Each sleeve keeps its detector's state in its own state, so a session over the same events writes the same
  journal, byte for byte ([ADR 0005](0005-deterministic-replay.md)).
- A sleeve starts each session from an empty state, like every sleeve, and so warms up again at every start
  of the trader. The detector can be restored; carrying a sleeve's state across sessions is not built.
- The command that runs the null on recorded days is not built. The helper takes the function that runs a
  sleeve over a stream, and the tests hand it the shadow runner.
- Every number in the configuration of this sleeve is a placeholder. Its first shadow run is registered in
  `research/` before it is made, with both variants and every threshold it tests.
