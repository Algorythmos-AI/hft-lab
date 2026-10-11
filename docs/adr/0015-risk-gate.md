# ADR 0015: The independent risk gate

- **Status:** accepted, 2026-10-11. The gate's shape is decided here. Every limit in `config/risk.yaml` is
  still a null placeholder and the file is not in force; real values need a decision record.
- **Context:** F4, paper trading

## Context

From F4 an order can reach a broker. A sleeve is a pure function that holds no venue
([ADR 0014](0014-sleeves-as-pure-functions.md)), so something else decides whether what a sleeve asks for
is sent. Four things have to be true of that something.

- **It does not believe the sleeve.** A strategy that is wrong about what it holds is exactly the strategy
  that needs stopping. The gate cannot take its positions from the thing it is guarding.
- **It replays.** A verdict that depends on a hidden clock or a file read at an odd moment cannot be
  reproduced, and a refusal nobody can reproduce cannot be reviewed
  ([ADR 0005](0005-deterministic-replay.md)).
- **Its answers can be counted.** A refusal explained in a sentence can be read once. A refusal with a code
  can be compared between a live run and its replay, totalled per day and published.
- **A stop survives the process.** A loss latch that a restart clears is a latch with a reset button anyone
  can press by accident.

## Decision

### 1. One gate, one verdict

`hftlab.risk.RiskGate.check(order)` rules on every order before it can be sent, and the order manager is the
only caller ([ADR 0016](0016-order-manager-and-outbox.md)). The verdict is one of three:

| Outcome | Meaning |
|---|---|
| `ALLOW` | Nothing stands in the way |
| `REFUSE` | No order of this kind may go, whichever way it points |
| `EXITS_ONLY` | Entries are stopped. The order may go only if it reduces the sleeve's position |

Every verdict but a plain `ALLOW` carries codes from `hftlab.risk.Reason`, a closed list, the deciding one
first. There is no free text anywhere in a verdict.

"Reduces" has one meaning: with every order of that sleeve still in flight assumed to fill in full, the new
order cannot leave the sleeve holding more than it holds, or holding the other way. A sell of 20,000
against a long of 20,000 is an exit; a second one behind it is not.

### 2. What the gate knows, and from where

The gate is shown events (`observe`) and asked for verdicts (`check`). It keeps its own `Book`, built from
the fills it is shown. An allowed order is not a position, and neither is a report that says an order
filled: only a fill is. Its verdict is a function of the events it has been shown, the injected clock, the
calendar, its limits and what its journal held when it was built. The switches are read through a function
that is handed in. The same script gives the same verdicts and the same journal, byte for byte, and a test
holds that for generated scripts.

### 3. The checks

| Check | Code | Kind |
|---|---|---|
| The venue is READY, as far as the gate has been shown | `VENUE_NOT_READY` | refuse |
| The id is ours, names the order's sleeve, and is ruled on once | `ORDER_ID`, `DUPLICATE_ORDER` | refuse |
| The pair has limits | `PAIR_NOT_CONFIGURED` | refuse |
| The sleeve has limits | `SLEEVE_NOT_CONFIGURED` | exits only |
| `KILL`, `FLATTEN`, or switches that cannot be read | `KILL_SWITCH`, `FLATTEN_SWITCH`, `SWITCHES_UNREADABLE` | exits only |
| The trader's event tape is under its soft limit (addendum 3) | `TAPE_SOFT_LIMIT`, `TAPE_HARD_LIMIT` | exits only |
| The market is open | `MARKET_SHUT` | refuse |
| There is a quote, the venue confirmed it lately, and it is not crossed | `NO_QUOTE`, `STALE_QUOTE`, `CROSSED_QUOTE` | refuse |
| The price is within the collar of the mid: the entry collar, or the wider exit collar for an order that can only reduce | `PRICE_COLLAR` | refuse |
| The spread is within its guard | `SPREAD_TOO_WIDE` | exits only |
| No macro-event blackout for the pair's currencies | `MACRO_BLACKOUT` | exits only |
| Outside the window around the 17:00 New York rollover | `ROLLOVER_WINDOW` | exits only |
| Not in the last minutes before the weekly close | `WEEKLY_CLOSE` | exits only |
| Order size | `ORDER_SIZE` | refuse |
| Position per pair, for the sleeve and for the desk | `POSITION_LIMIT` | refuse |
| Pairs open at once | `MAX_OPEN_PAIRS` | exits only |
| Orders per minute, per sleeve and for the desk | `ORDER_RATE_SLEEVE`, `ORDER_RATE_DESK` | refuse |
| Order-to-trade ratio over a rolling window, per sleeve and for the desk | `ORDER_TO_TRADE_SLEEVE`, `ORDER_TO_TRADE_DESK` | refuse |
| Daily loss and drawdown latches, per sleeve and for the desk | `DAILY_LOSS_*`, `DRAWDOWN_*` | exits only |
| A loss in a currency with no latch; a position with no quote to value it | `UNLATCHED_CURRENCY`, `UNMARKED_POSITION` | exits only |
| The reject-streak breaker | `REJECT_STREAK` | exits only |
| A reconciliation break | `RECONCILIATION_BREAK` | exits only |
| The order manager, or the gate's own book, saw something that cannot happen | `ORDER_FAULT` | exits only |

Details that decide outcomes:

- **Position.** An order the gate allowed is counted in full against the limit from the verdict on. It is
  released by a terminal order event, and then only down to what that event says was filled and the book
  has not seen yet. Time never releases it. So a FILLED report that overtakes its fill does not free room
  the position is about to take.
- **Collar.** A limit order is measured at its limit price. A market order is measured at the touch it
  would take, so a book whose half-spread is beyond the collar takes no market order at all. An order that
  can only reduce the sleeve's position is measured against `exit_collar_ticks` instead (addendum 1).
- **Rates and the trade ratio.** Every allowed order counts, entry or exit, and so does every cancel. The
  ratio allows `ratio` messages per order that traded in the window, and `ratio` before the first trade.
  There is no exemption: these limits are also what makes the patterns on the
  [never-build list](../never-build.md) hard to produce by mistake.

### 4. Loss control

- **Per scope and per currency.** A result is the book marked at the last quotes (a long at the bid, a
  short at the ask), less the fees charged in that currency, for one sleeve or for the desk. Amounts in
  different currencies are never added. The configuration sets a latch for the quote currency of every
  pair, for the desk and for every sleeve, and the loader refuses a file that does not.
- **Daily loss** is measured from the result at the last 17:00 New York rollover. **Drawdown** is measured
  from the highest result reached.
- **A latch is sticky.** It stays tripped through a recovery of the price, the end of the day and a restart,
  until `reset(name, operator=...)`. The trip, the reset and the marks a loss is measured from (the day's
  opening result, the high-water mark) are written to a hash-chained journal, which the gate reads when it
  is built. A reset starts the measurement again from the result as it stands.
- **The reject-streak breaker** counts the venue's first answer to each order the gate allowed: a reject
  adds one, any acceptance sets the count to nothing, and at the limit it latches.
- The gate looks at every quote that can move a result and before every verdict.

### 5. Switches

`KILL` and `FLATTEN` are files in the platform's state directory, on while they exist. The gate is handed a
function that says which are on; `hftlab.risk.file_switches` is that function over a directory. If the
directory cannot be read, entries stop.

While `FLATTEN` is on, `flatten_orders()` proposes one closing order for each line the book holds: a market
order for what is booked, never more, no larger than the pair's order limit, and none for a line that has an
order in flight. They are proposals. Each passes `check` like any other order, as an exit.

### 6. The clock

The gate reads the injected clock and never lets its own time go back. A quote is as fresh as its stamp
says and never fresher than the gate's time. An unchanged book is silent on the venue port, so the trader
confirms a quiet book with `quiet(pair, at)` when the venue has answered; without that, a quiet book goes
stale and nothing is sent on it. A quote from before a lost or new connection cannot be confirmed at all:
it still values positions, and only a new quote makes the pair tradable again.

### 7. The limits file

`config/risk.yaml` is read by a strict loader. A key it does not define is refused at every level; a whole
number is an `int`; money is quoted decimal text converted exactly. While `in_force` is false the file is
checked for its shape and then refused, so no gate can be built from placeholders. With `in_force` true
every limit must be set, the status must be `decided` and a decision record must be named.

## Consequences

**What the gate is pessimistic about**

- An order it allowed can fill in full until it is shown otherwise.
- A quote is old unless the venue has said otherwise lately, and the clock cannot make it younger.
- A switch it cannot read is a switch that is on.
- A loss is not forgiven by time, by a restart, or by the price coming back.
- An exit still has to respect the venue: rate limits, the trade ratio, a collar and the stale-quote stop
  hold for exits too. A stuck exit waits for the window or the book; it is not forced through. Its collar
  is wider than an entry's (addendum 1).
- Its own book refusing a fill, or the order manager reporting a fault, stops entries until an operator has
  looked.

**What it does not cover**

- **Gross exposure and leverage.** Both need positions valued in one currency, and nothing in the platform
  converts a currency. The two keys stay in the file, must stay null, and the loader refuses a value. Until
  they are built, exposure is bounded pair by pair and by the number of pairs open at once.
- **Exposure across pairs.** A long in one pair and a long in another that shares a currency are two
  positions to the gate.
- **Closing a position.** A latch stops entries. It sends nothing. Getting flat is a sleeve's exit or an
  operator's `FLATTEN`, whose closes the trader passes on once a second.
- **A loss between two quotes.** A result is only as recent as the last quote.
- **Memory across a restart** is no longer a gap (addendum 2). What is still lost with the process is the
  last second of a rising high-water mark, as below.
- **The first look.** A gate with no journal takes the result it first sees as the day's opening result and
  as the high-water mark. A high-water mark that is still rising is journalled at most once a second, so a
  kill can lose the last second of it.
- **The power going.** The gate writes its latches through the journal it is handed and flushes after
  every trip, reset and mark. Whether that reaches the disk or only the operating system is the journal's
  setting; the trader has to build it with fsync on.
- **Whether the decision record named in the file exists.** The loader checks the id's form; the registry
  lint checks records.
- **The trader** is written since ([ADR 0019](0019-trader-loop-and-staged-rollout.md)). It hands the gate
  the switches as they were read at its last beat, so that a recorded day replays to the same verdicts.

A daily latch that does not clear with the day is stricter than its name. That is deliberate: the first
runs on a paper account are where a loss should bring a person, not a calendar.

## Addendum, 2026-10-11: two defects of the first version, settled with the trader

1. **Exits have a collar of their own.** The first version measured every order against one collar, so a
   spread that blew out refused the `FLATTEN` closes together with the entries: the guard blocked the very
   orders that take risk off. Now an order that can only reduce the sleeve's position is measured against
   `per_pair.<pair>.exit_collar_ticks`. It is a collar, never none, and the loader refuses a file in which
   it is narrower than `price_collar_ticks`. "Can only reduce" is the meaning of section 1, with every order
   of the sleeve still in flight counted as filled, so the wider collar cannot be used to open a position,
   to go through zero, or by a second exit behind a first. A `FLATTEN` close that was not allowed is proposed
   again for its line only after `switches.flatten_retry_seconds`; a close that was allowed is followed by
   the rest as soon as it has ended. Tests: `tests/risk/test_gate.py` (the exit collar and the interval) and
   `tests/faults/test_faults.py` (the rollover blow-out).
2. **A restart is not a reset.** The first version kept the orders-per-minute windows, the trade ratio, the
   running count of rejects and the ids already ruled on in memory only, so a crash loop emptied them. Now
   the order manager, which replays its outbox at every start, hands the gate back when each order was
   allowed, each cancel sent and each order first traded, and which orders may still fill
   (`RiskGate.recall`): the windows hold what they held, an id is ruled on once across restarts, and an
   order in flight holds its size against the position limit until reconciliation has settled it. The
   running count of rejects is written to the gate's own journal whenever it changes, beside the latches.
   The outbox is the one record of what was sent, and it is on the disk before every send, so nothing is
   written twice. Tests: `tests/risk/test_gate.py` and `tests/exec/test_manager.py` (a restart is not a
   reset).
3. **The record running out of room is a reason to stop entries, and then to close** (settled with
   [ADR 0027](0027-deploy-gate-and-reports.md)'s addendum). The gate is shown the room on the trader's event
   tape as an event, `TapeRoom`, the way it is shown a reconciliation break: it measures nothing itself, so a
   replayed day shows it the same room at the same turn. At the soft limit the verdict is `EXITS_ONLY` under
   `TAPE_SOFT_LIMIT`. At the hard limit it is `EXITS_ONLY` under `TAPE_HARD_LIMIT`, and `flatten_orders()`
   proposes what it proposes under `FLATTEN`, with no switch set and when the switches cannot be read: the
   same orders, the same `check`, the same exit collar and the same retry interval. Nothing of it is
   journalled or kept across a restart; the trader shows a new gate the room on its first turn. Tests:
   `tests/risk/test_gate.py` (the room on the event tape).
