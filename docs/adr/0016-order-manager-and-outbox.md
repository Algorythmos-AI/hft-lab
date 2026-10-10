# ADR 0016: The order manager, its outbox, and reconciliation

- **Status:** accepted, 2026-10-11. Built and tested against the fake broker and the shadow venue only. What
  a real gateway does differently is not known yet: the F4 exit gate is five days on the paper account.
- **Context:** F4, paper trading

## Context

Between "the risk gate allowed this order" and "the book says what it did" there are four ways to be wrong
that no strategy test will find.

- **The process dies at the wrong moment.** An order that was sent and not remembered is a position nobody
  is watching. An order that is remembered and sent again is two.
- **The connection dies at the wrong moment.** A send that failed part way may or may not be at the venue.
- **Reports lie about order.** A fill arrives before the acknowledgement of its order, a FILLED report
  before the fills it counts, an old partial report after the final one, and any of them twice
  ([ADR 0009](0009-venue-port-guard-and-fake-broker.md)).
- **The venue and the book drift apart** without anything failing loudly: a report that never came, an
  order placed by another hand.

## Decision

### 1. One way to the venue

`hftlab.exec.OrderManager` is the only code that calls `place` or `cancel` on a venue the platform trades
on. A scan of the package holds that line. Two earlier callers are named in the test and neither can reach
the trader's venue: the shadow runner on the shadow venue it builds itself, and the capability probe the
owner runs.

| Method | What it does |
|---|---|
| `propose(order)` | The risk gate rules; an allowed order is journalled, durably, and then sent |
| `withdraw(order_id)` | Asks the venue to cancel a working order of ours |
| `handle(event)` | Every venue event: the gate sees it first and as it came, then the state machine and the book |
| `reconcile()` | One step of making a connection safe to trade on; True once it is |

The names are not the venue's on purpose: any call spelt `place` or `cancel` outside the package is a
violation, whatever it is called on. The gate is on the path by construction. `propose` asks the gate itself,
and the manager can only be built on the `RiskGate` class, not on something shaped like it
([ADR 0015](0015-risk-gate.md)).

A client order id is a pure function of the decision ([ADR 0014](0014-sleeves-as-pure-functions.md)). The
same order proposed again is the same order and is not sent again. Different terms under a used id are a
fault.

### 2. The outbox: written before it is sent

The outbox is a hash-chained journal (`hftlab.journal`, the chain the shadow runner writes). An allowed
order is appended to it in full, with the gate's code, and that line is on the disk (fsync, and the
directory entry of a new file) before `venue.place` is called. Then every move of every order, every fill
booked, every commission, every cancel sent and every fault is appended.

After a kill at any point, a new manager on the same outbox knows every order that could be at the venue,
under the id the venue knows it by. It does not know which are. So every order that was in flight starts as
UNKNOWN, and the manager starts blind.

A torn last line is cut off on opening. A whole line that does not verify, or an entry about an order the
outbox never recorded, stops the start.

### 3. One table of moves

```
INTENDED -> JOURNALLED -> SENT -> WORKING -> PARTIALLY_FILLED -> FILLED
                                     \-> CANCELLED | REJECTED | EXPIRED
UNKNOWN: in flight when sight of it was lost
```

`hftlab.exec.MOVES` is the whole table, and a move that is not in it is never made. Because reports arrive
in any order, the stage does not follow the order of arrival. It follows two numbers that only grow, what
has been booked for the order and the most any report has claimed, and the first terminal report. A report
that adds nothing is ignored. Any reordering, with repeats, of the same true history ends in the same stage
with the same fills; a test holds that for generated reorderings.

A report that contradicts what is established is a fault (`hftlab.exec.Fault`): it is written to the outbox
and it trips the gate's `ORDER_FAULT` latch, so entries stop until an operator resets it. A fill is booked
all the same. The venue says it happened, and the book has to say what the venue says.

### 4. Blind until reconciled

A manager is blind from every start, and again after every lost or new connection and every send that
failed part way. While blind nothing is proposed to the venue. `reconcile()` ends it, and it is a step to be
called until it returns True, with `poll` and `handle` in between:

1. Every working order the venue lists is asked to be cancelled, whatever the manager believed about it.
   Nothing more happens until the venue lists none.
2. The venue's executions since the last known time, less an allowance for its clock, are booked, each once.
3. Every order still in flight is settled by that evidence, under its client order id: FILLED if its fills
   add up, CANCELLED if some did or the venue ever held it, EXPIRED if the venue shows no trace of it.
4. The venue's positions must equal the book's. While they do not, the connection is not marked, and the
   difference is written to the outbox once.
5. Only then `mark_reconciled()`.

**An order whose fate cannot be established is never sent again.** It is settled as dead by step 3. If the
venue later speaks of it after all, that is a fault and the venue's word stands: the order is in flight
again, a fill is booked, and entries are stopped.

A working order that carries no id of ours cannot be cancelled through the port. It is a fault, and the
connection stays unmarked while it works.

### 5. The broker book and the reconciler

The manager's `book` is the broker book of [ADR 0003](0003-two-books.md): the same `Book` class as the
shadow book, fed by the venue's fills and commissions and by nothing else. A fill reported live carries no
commission; the same fill listed later by the venue does. A repeat that is the first to say what a fill
cost has that recorded, once, in the book and in the outbox.

`hftlab.book.Reconciler` runs on the caller's schedule while trading. It sees the venue through a protocol
with two questions and no order method. Executions the book has not seen are handed to the manager's
`handle`, so a recovered fill takes the path of a live one. A position difference that outlives a
configured grace time is a break: a flag, a `BreakEvent` the gate stops entries on, and a journal line. The
flag clears only when a later check finds the positions agreeing and that agreement has been written to the
journal first. A break, and a grace time that has started, survive a restart.

The two position checks differ on purpose. While trading, a fill can be in the venue's position and not yet
in its execution list for a moment, so the reconciler waits out a grace time and then stops entries. At
step 4 nothing is working and every execution has been asked for, so a difference there has no innocent
explanation and no order of any kind is sent.

### 6. How it is tested

- The manager is killed before, after and half way through every line it writes, every order and cancel it
  sends and every report it takes, and started again on the same files against the same broker. After each:
  every journalled order is settled to what the broker holds, the broker holds nothing the outbox does not
  know, each fill is booked once, and the books equal the broker's positions and cash.
- Against the fake broker, through the gate, the manager and the reconciler together: a disconnect in a
  partly filled order, a gateway away for minutes, a second drop while reconciling, duplicated, reordered,
  late and repeated reports with partial fills and rejects, a clock that jumps, quotes that stop, the
  weekend gap, and a spread that blows out at the rollover.
- Generated sessions with generated faults, kills and restarts hold the accounting invariants, and the
  position limit measured at the broker with every working order counted in full.
- The same manager runs unchanged on the shadow venue.

## Consequences

**What the design is pessimistic about**

- **The send.** It is unknown until the venue confirms it. A failure part way leaves the order UNKNOWN and
  the manager blind.
- **Reports.** Any order, any repeat. Nothing is concluded from the absence of one.
- **The journal.** An order that could not be written is not sent, and entries stop.
- **A reconnect.** Every working order is cancelled before anything resumes. A resting order loses its
  place; that is accepted.
- **Its own conclusions.** An order written off as never having arrived is still listened for.

**What it does not cover**

- **An outbox that is lost or replaced.** The manager then knows of no orders. Step 4 would notice a
  position, and a working order of ours it does not hold is cancelled and is a fault, but an order that
  filled and was closed again leaves no trace it could find.
- **Two processes on one outbox.** Nothing locks the file. Two writers break the chain, which the next start
  refuses; running one trader is the host layer's job.
- **An order that arrives after it was written off.** An order settled as never having arrived frees its
  size at the gate at once. If it reaches the venue afterwards it is booked, it is a fault and entries stop,
  but for that moment the position limit was one order looser than it looked. Holding the size instead would
  block a sleeve's own exits behind an order that most likely does not exist.
- **Fills from before the outbox.** On a first start the venue is asked for every execution it still lists.
  One that belongs to no order in the outbox is booked, so the book says what the venue says, and it is a
  fault: an operator looks before anything trades.
- **Executions the venue no longer lists.** The manager asks from before the earliest order in doubt. A
  venue that keeps one day of executions cannot explain an older one, and the position check is what is
  left.
- **A venue clock more than two minutes from ours**, the default allowance.
- **Size.** The outbox is replayed whole at every start and is never rotated. Rotation needs a rule for
  carrying open state across files and is not built.
- **Speed.** One fsync before every order. It has not been measured against the round trip.
- **Paging.** A fault and a break stop entries and are journalled. Nobody is told yet: alerts are the host
  layer.
- **A real gateway.** How it reports a cancelled immediate order, whether its execution list carries
  commissions, how far its positions lag its executions, and what it lists as open after a reconnect are
  assumptions of the fake broker until the paper account has been run for the five days of the F4 gate.
- **The trader.** The loop that polls, calls `handle`, runs the sleeves, calls `propose`, steps `reconcile`
  and schedules the reconciler is the next piece of F4.
