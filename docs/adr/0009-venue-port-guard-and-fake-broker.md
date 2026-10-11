# ADR 0009: The venue port, its guard and the fake broker

- **Status:** accepted, 2026-10-10
- **Context:** F1, broker probe and adapter

## Context

From F1 on the platform talks to a broker. Three things have to be true before any strategy is written on top
of that.

- **The rest of the platform must not know which broker it is.** Strategy, risk and book code is a pure
  function of events and a clock ([ADR 0005](0005-deterministic-replay.md)). A broker library leaking into it
  would end that, and would tie every later phase to one access route ([ADR 0010](0010-access-route.md)).
- **Tests must not need a broker.** The paper account is slow, stateful, needs a login and never gives the
  same quotes twice. Fault injection (a disconnect in the middle of an order, a duplicated execution report)
  cannot be asked of it at all.
- **The paper-only lock must be impossible to walk around.** F0 left a timing problem: the lock needs the
  account ids, and those exist only after connecting. "Call the lock before you send" is a convention, and a
  convention fails the first time someone adds a second code path.

## Decision

### 1. One port, in core types

`hftlab.venues.port.Venue` is the only thing the rest of the platform depends on. It offers:

| | |
|---|---|
| Connection | `connect`, `disconnect`, `mark_reconciled`, the state, and a `ConnectionEvent` for every change |
| Identity | `managed_accounts`, `server_time_ns` |
| Instruments | `instrument(pair, kind)`: price scale and increment, minimum size and size increment, CASH or CFD |
| Market data | `subscribe_quotes` (delivering `Quote`), `subscribe_depth` and `historical_quotes` where the venue has them (`capabilities` says) |
| Orders | `place`, `cancel`, with `OrderEvent`, `Fill` and `CommissionEvent` in the event stream |
| Reconciliation | `open_orders`, `positions`, `executions_since` |

There is no modify. An amended order is a new order with a new id, which keeps the id a pure function of the
decision and leaves one fewer state to get wrong.

### 2. A sealed guard that every venue shares

`hftlab.venues.guard.GuardedVenue` holds everything that decides *whether* to talk to the broker. A venue
subclasses it and supplies only the methods that talk. The public methods are sealed: a subclass that
defines one of them is refused when its class is created.

- **The lock, in three stages** (`hftlab.safety.gate.PaperGate`):
  1. before a socket is opened: mode, live flag, gateway port, username;
  2. after the handshake and before anything else is asked: the full lock on the account ids the broker
     itself reported. Passing it mints a permit for that one connection;
  3. before every order and every cancel: the full lock again, with the permit of the connection in use.
- **The port being dialled must be the port the lock was given.** Otherwise the lock could judge 4002 while
  the socket goes somewhere else.
- **A refusal is terminal.** It is a statement about where the platform is pointed, not a network error, so
  nothing retries it. The venue is `REFUSED` until the process is replaced.
- **A permit dies with its connection.** A reconnect goes through the same path as a first connection and is
  unchecked until stage two passes again.
- **A new connection is `RECONCILING`, never `READY`.** Orders are refused until the caller has looked at
  open orders, positions and executions and called `mark_reconciled()`. Cancels are allowed in between,
  because cancelling stale orders is what reconciling consists of. This holds for the first connection too:
  an earlier process may have left orders working.
- **Nothing is sent while disconnected.** A lost connection is retried on a fixed backoff schedule with no
  jitter and no limit.
- **One path to the broker.** `place` checks, in order: `READY`; the instrument came from this venue; the
  order fits its minimum size and both increments; the client order id has not been sent before; stage
  three. Only then does it call `_send`, and it is the only caller. A scan of the package asserts that.

### 3. A fake broker behind the same guard

`hftlab.venues.fake` implements the port in process, on the same guard, over a small market
(`FakeMarket`) that holds quotes, resting orders and executions.

| It can | How |
|---|---|
| Be deterministic | One event queue, ties broken in scheduling order, seeded quotes, no clock of its own |
| Quote | Scripted quotes, or a seeded random walk on the price grid |
| Fill | From the top of the book after a configurable delay; resting limit orders fill when the quote reaches them |
| Fill partially | Several executions per fill; or at most the displayed size per quote |
| Reject | Any rule the test supplies |
| Misreport | Every Nth execution report duplicated; every Nth swapped with the one after it |
| Drop the link | Outage windows, including one that starts between an order and its acknowledgement |
| Look live | Any account list, so the lock is tested against a live-looking id |
| Drive the clock | With a `SimClock`, `poll(timeout)` moves time to the next thing that happens |

Its fills are optimistic on purpose, like a paper account's. It exercises plumbing. The conservative fill
model is the shadow venue's job ([ADR 0003](0003-two-books.md)) and is not built yet. (It has been since:
[ADR 0013](0013-shadow-cost-model.md).)

The fake broker is not a registered venue. The registry still holds exactly `ibkr_paper` and `sim`.

### 4. A contract suite that both must pass

`tests/venues/test_contract.py` runs one set of scenarios against the fake broker and against the broker
adapter. The adapter runs over a stub of the library's client that is driven by the same `FakeMarket` and
speaks in the library's own objects. One test then requires the two to produce identical events for a long
seeded session with partial fills, rejects, faulty reports and an outage.

### 5. What an event means

- A `Quote` is emitted when the top of the book changes. An unchanged book is silent.
- An `OrderEvent` with state `SUBMITTED` is emitted by the guard when `place` returns.
- A `Fill` may arrive more than once, and before or after the `OrderEvent` that counts it. `fill_id` is the
  key. Its commission arrives separately and may be late.
- Events lost while disconnected are not replayed.

## Consequences

- Later phases can be developed and fault-tested against the fake broker, and the contract suite is the
  reason that is sound. It found two real differences while it was being written (how often a quote is
  reported, and what a client knows about its orders after a reconnect); both are now part of the contract.
- The suite proves the fake broker and the adapter agree with each other and with our reading of the
  library. It cannot prove that reading matches what a real gateway sends. Each such assumption is marked
  `UNVERIFIED` in the code and listed in [the probe runbook](../runbooks/ibkr-probe.md); the probe is how it
  is confirmed.
- Sequence numbers are per venue object. The recorder (F2) will own the sequence that a replay follows.
- "Sealed" is as strong as Python allows: a subclass cannot override, a permit cannot be built by hand, and
  a scan fails on a second caller. A contributor determined to bypass it can still write code that does;
  review is the last layer, as in [ADR 0001](0001-paper-only-lock.md).
