# ADR 0014: A sleeve is a pure function; the idle and the control sleeve come first

- **Status:** accepted, 2026-10-11
- **Context:** F3, shadow venue and control

## Context

A sleeve is one strategy with its own book and its own limits. Three things have to be true of every sleeve
before the first one is written.

- **It replays.** A recorded day must lead to the same decisions offline
  ([ADR 0005](0005-deterministic-replay.md)). A sleeve that reads a clock, a file or a random generator
  breaks that without any test noticing.
- **It cannot trade by itself.** A sleeve that holds a venue can send an order that no risk gate saw.
- **It cannot be wrong about what it holds.** A strategy that keeps its own count of its position drifts from
  the truth at the first partial fill or lost acknowledgement.

And before any sleeve's profit is believed, the profit calculation itself has to be checked against a case
whose answer is known.

## Decision

1. **A sleeve is one function, folded over the events it is shown:**

   ```
   decide(state, event, now_ns, view) -> (state, actions)
   ```

   | | |
   |---|---|
   | `state` | Everything the sleeve remembers. A value it returns and is handed back; it keeps nothing anywhere else |
   | `event` | A quote, a heartbeat, a connection change, or news of its own orders: placed, refused, an order event, a fill |
   | `now_ns` | The receive time of that event. The only clock a sleeve has |
   | `view` | Its own book, built from fills: what it holds, not what it believes it holds |
   | `actions` | What it wants done: an `Intent` (an order) or a `Cancel` of one of its own orders |

2. **A sleeve is handed no venue, no clock object, no file and no random generator.** Whoever runs it decides
   where an intent goes: the shadow venue today, the risk gate and then the paper broker from F4. A scan of
   the package fails on an import or a call that would let a sleeve reach round this.
3. **The runner makes the order id** from the decision: sleeve, pair, side, the number of the event it was
   decided on, leg and attempt. The same decision is the same order. A deliberate second try is a new attempt
   number and so a new id.
4. **A sleeve hears about its own orders, first.** News of its orders is shown to it before the market event
   that came with it, and it is never shown another sleeve's.
5. **A sleeve that answers its own acknowledgements without end is stopped** by the runner after a fixed
   number of answers to one event.
6. **The idle sleeve never trades.** Its result must be exactly zero, to the last unit.
7. **The control sleeve trades at random.** On a seeded share of the quotes of a flat pair it enters at the
   minimum size, long or short by a fair coin, with a fill-or-kill market order; it holds for a fixed time
   and gets out the same way.
   - Its randomness is a hash of the seed and of the quote in hand (pair, receive time, sequence number), not
     a generator with a position. The same seed and events give the same decisions from any starting point.
   - It exits what its book says it holds, not what it remembers ordering.
   - An order that came to nothing is not sent again at once. The pair's state keeps the time of the last
     attempt, and the next one waits a configured time. Without that, a market that fills nothing, such as
     the window around the rollover, costs an order for every quote.
8. **A control run that is compared with anything is pre-registered** in `research/`, with its seed, entry
   rate, holding time and waiting time fixed there before any result is seen.

## Consequences

- The check of the whole accounting chain is two statements: the idle sleeve ends on zero, and the control
  sleeve's mean result per round trip is minus the modelled cost
  ([ADR 0013](0013-shadow-cost-model.md)). On a book that does not move the second is exact; on one that
  moves it holds in the mean. If either fails, no profit figure from the platform means anything.
- Every later sleeve is measured against the control: one that cannot beat random entries after costs has
  shown nothing.
- A sleeve that needs history carries it in its state, and bounding that state is the sleeve's job.
- The shadow runner is the only runner so far. It builds its own shadow venue and can be handed no other,
  so nothing a sleeve returns can reach a broker ([ADR 0003](0003-two-books.md)).
- The scan reads syntax, like the isolation scan of [ADR 0001](0001-paper-only-lock.md). Review is the last
  layer here too.

## Addendum, 2026-10-11: a state may be written down

A sleeve may now say how its state is saved and restored, exactly
([ADR 0026](0026-sleeve-state-across-a-restart.md)). Nothing of this record changes: the sleeve still touches
no file, and what is kept, where, and whether it is believed are the runner's.
