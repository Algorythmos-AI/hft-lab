# ADR 0011: A synchronous, pumped port; asyncio stays inside the adapter

- **Status:** accepted, 2026-10-10
- **Context:** F1, broker probe and adapter

## Context

The broker library is built on asyncio. The port in front of it ([ADR 0009](0009-venue-port-guard-and-fake-broker.md))
has to pick a shape, and whatever it picks spreads: an `async` port makes every caller `async`, a callback
port puts the caller's code inside the library's call stack.

The constraint that matters is [ADR 0005](0005-deterministic-replay.md): strategy, risk and book code is a
pure function of an event stream and a clock, so that a recorded day replays to the same decisions.

| Shape | What it would mean here |
|---|---|
| `async` methods and async iterators | Strategy and risk code becomes coroutines, or needs a bridge. Ordering between two streams depends on the scheduler. The fake broker needs an event loop to be deterministic in |
| Callbacks | The caller's code runs wherever the library decides, re-entrantly. An exception in a handler lands in the library |
| A thread and queues | Locks, and an order of events that depends on thread timing |
| Synchronous and pumped | The caller owns one loop: poll, decide, send |

## Decision

1. **The port is synchronous.** A command (`place`, `cancel`, `subscribe_quotes`) returns at once.
2. **Events are pulled.** `poll(timeout_ns)` lets the venue do its I/O for at most that long and returns
   every event since the last call, as an ordered list, each stamped with its receive time and a sequence
   number. Nothing is delivered any other way.
3. **Reconciliation queries block** (`open_orders`, `positions`, `executions_since`, `instrument`). They are
   asked while nothing is being traded, and what arrives meanwhile is queued, not lost.
4. **asyncio stays inside `hftlab.venues.ibkr`.** The adapter drives the library's event loop from `poll`,
   which the library supports as its synchronous mode. The isolation scan already confines `asyncio` with
   the other network libraries.
5. **One thread.** The library's handlers run only inside `poll`, on the caller's thread.

The trader's loop is then the same three lines live and in a replay:

```
for event in venue.poll(timeout):      # live: the adapter; replay: the recording; tests: the fake broker
    for command in core.on(event):     # pure: events and a clock in, commands out
        command.send(venue)
```

## Consequences

- The fake broker is deterministic without an event loop, and a polling loop written for it runs unchanged
  against the adapter. The capability probe is the first such loop.
- Latency has a floor: an event waits until the caller polls. With a blocking `poll` that returns on the
  first update this is the cost of one loop iteration, small beside an order round trip of tens of
  milliseconds. The probe measures quote gaps and round trips so that the claim is checked, not assumed.
- A blocking query stalls the stream for as long as the gateway takes to answer. A request timeout turns a
  gateway that never answers into `VenueUnavailable` instead of a hang.
- One venue object per thread. Two things that must not wait on each other, the recorder and the trader, are
  already separate processes ([ADR 0004](0004-recorder-separate-from-trader.md)).
- If the loop is ever shown to be the bottleneck, the adapter can change underneath: the port's contract is
  the event list, not how it was gathered.

## Note, 2026-10-12: the quote gaps have been measured

The claim above is now checked for one half. In an ordinary session the top of the book of a major pair
changed about one to three times a second on this feed, with a median of a third of a second between changes
([`HFT-EXP-0008`](../../research/experiments/HFT-EXP-0008-capability-probe-orders-asian-session/report.md)). The platform was designed for quotes every few milliseconds; against the
measured rate one loop iteration is smaller still, and the decision stands. The order round trip of tens of
milliseconds is not measured: the one timing taken is from a laptop, of orders the broker rejected.
