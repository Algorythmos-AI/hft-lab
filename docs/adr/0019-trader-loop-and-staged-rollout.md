# ADR 0019: The trader loop, its tape, and the staged rollout

- **Status:** accepted, 2026-10-11. Built and tested against the fake broker and the shadow venue only. With
  the shipped configuration the trader runs in shadow-only mode. Nothing has traded on the paper account.
- **Context:** F4, paper trading

## Context

The risk gate ([ADR 0015](0015-risk-gate.md)) and the order manager ([ADR 0016](0016-order-manager-and-outbox.md))
each ended on the same line: the process that wires them to the sleeves is the next piece. That process is
where four promises made earlier have to be kept at once.

- **Two books** ([ADR 0003](0003-two-books.md)). Every order is answered twice, and the two answers differ.
  A sleeve holds one position, and somebody has to say which book it reads.
- **Replay** ([ADR 0005](0005-deterministic-replay.md)). The gate and the manager are deterministic given
  their inputs. Their inputs now include a broker, a clock and two files an operator can touch.
- **Restart.** The outbox makes a restart safe for orders. A sleeve keeps its state in memory
  ([ADR 0014](0014-sleeves-as-pure-functions.md)), so a restart leaves a position nobody remembers taking.
- **A first paper order.** Something has to decide when a sleeve that has only traded in shadow may send
  one, and it must not be a default.

## Decision

### 1. One loop, fed by an edge

`hftlab.trader.Trader` is one loop on one thread. A turn has a time and the venue's events, and now and then
a beat: the switches as they were read, the venue's answer to "are you there", and what an operator asked
for. On a turn, in this order:

1. every venue event goes to the order manager, which shows it to the gate first and then books it;
2. on a beat, a quiet book is confirmed to the gate and to the shadow venue;
3. while the manager is blind it takes one step of `reconcile`; otherwise the reconciler runs on its
   schedule. A latch that tripped or a break that was flagged is journalled and paged;
4. the shadow venue is fed the quote and reports on earlier orders: the shadow book;
5. on a beat, the trader's own closes are proposed (section 4);
6. each sleeve is shown its news and the market's events, and its intents are routed (section 2).

The trader reads nothing of the world itself. A turn's time is taken once, by the edge, and is the only time
anything decided on that turn knows: the gate, the manager, the reconciler and the shadow venue all run on a
simulated clock the trader moves to it.

### 2. Where an intent goes

| Stage | The sleeve is run | An order that adds | An order that reduces |
|---|---|---|---|
| `off` | no | | |
| `shadow` | yes | shadow venue only | shadow venue only |
| `min_size` | yes | gate, then broker, at the pair's minimum size; mirrored | gate, then broker; mirrored |
| `normal` | yes | gate, then broker, as asked; mirrored | the same |
| `stepped_down` | yes | refused, on both venues | gate, then broker; mirrored |

"Reduces" has the gate's meaning: with every order of the sleeve still on its way counted as filled, the
order cannot leave the sleeve holding more, or holding the other way.

**An order goes to the shadow venue when, and only when, it went to the broker** (or, in `shadow`, would
have been the sleeve's only order). The same order, with the same client order id, on the same turn. An
order the gate refused is refused on both venues, so the shadow book is the book of the orders that were
actually sent, under our fill model.

The broker's venue is handed to the order manager and to nothing else. The one venue the trader places on is
the shadow venue it builds itself; a scan pins that call site by its exact location, beside the shadow
runner's and the probe's.

### 3. Which book a sleeve reads

In a stage that places paper orders, **a sleeve's position is the broker book's**, and the news of its
orders is the broker's. That is what can actually be closed: a sleeve that believed the shadow venue would
try to sell what the broker never bought. **Its results are the shadow book's and nobody else's**: the view
a sleeve is handed takes its positions from the broker book and its realised result and fees from the shadow
book, and the summary, the status and the snapshot report sleeves from the shadow book. In `shadow` both come
from the shadow book.

The two books answer the same orders differently, so the trader keeps the shadow book from drifting:

- **An exit closes what the shadow book holds, and no more.** A reducing order is mirrored for at most the
  quantity the shadow book holds the other way; with nothing to close it is not mirrored at all. So an
  entry the broker filled and the shadow venue did not never becomes a position the other way round.
- **A shadow position the broker never had is closed on the shadow venue.** Where the shadow book holds more
  than the broker book, or the other way in sign, and nothing is on its way, the trader places a closing
  order on the shadow venue alone, at the shadow venue's cost. The sleeve cannot do it: it does not know.

Both are journalled with the two quantities, and the session's slippage report counts the orders the two
venues did not fill alike.

### 4. Orders that are not a sleeve's

Three, all of them closes, all journalled with where they came from:

- **`FLATTEN`.** On a beat the trader passes on what the gate proposes; each close passes the gate's own
  check. For a sleeve in `shadow` the trader closes the shadow book's lines itself, and the switches stop
  its entries the way the gate would. The event tape at its hard limit is closed for by this same path, with
  no switch set ([ADR 0027](0027-deploy-gate-and-reports.md), addendum).
- **Inherited positions.** A session starts every sleeve from its initial state. A line the books hold at
  that moment is closed by the trader: one closing order at a time, through the gate like any exit, tried
  again no sooner than a configured interval. Until it is flat the sleeve may not trade that pair. A line
  under a name no sleeve here answers for is not touched: it is journalled and paged, and `FLATTEN` closes it.
- **Aligning the shadow book**, as in section 3. It never reaches the broker.

### 5. The tape

What the trader is shown from outside is recorded as it enters, one line each (`hftlab.trader.tape`): the
connection and its instruments; every turn with its time, its events and its beat; every question put to
the venue with its answer or its failure; and how every send and cancel went.

Two small additions to the order manager make that possible without a second caller of the venue's order
methods. It asks its three questions through an object the trader hands it, which records the answers; and
it tells a witness how each send and each cancel went. It still calls `place` and `cancel` itself, on the
venue it was built with.

`python -m hftlab.trader --replay DAY` runs each session of a day again. Every journal a session read at its
start is copied, up to a mark the tape holds, into a scratch folder; the same `Trader` runs there on the
recording, with a stand-in venue that answers as the recording says; and the decision journal it writes is
compared with the one the day left.

- A session that said it stopped is reproduced **byte for byte**.
- A session that was killed is reproduced as far as it got: what it wrote is the beginning of what the
  replay writes.
- A replay under other settings is refused: the tape holds a hash of the files the decisions depend on.

A session ends at the 17:00 New York rollover, and the next one starts in the same process the way a restart
would. So a day's folder holds whole sessions, each replayable from the journals alone.

### 6. The staged rollout

A sleeve's stage lives in a hash-chained journal (`stages.jsonl`) and changes by rule and by nothing else.

- **Up one step, at a day's close**, when all of this holds: the configured number of clean trading days in
  a row in the present stage; the decision record that approved the step, named in the configuration; the
  risk limits in force; no latch tripped; no break standing. A rule with its number or its record missing
  is never met. The shipped file leaves every one null and starts every sleeve in `shadow`, and the loader
  refuses a ladder that starts higher. When everything but the limits is there, the refusal is journalled.
- **Down at once**, to `stepped_down`, when a latch that concerns the sleeve trips or a reconciliation break
  is flagged while it places paper orders. A reset of the latch does not give the stage back: the way up is
  the first rule again, counted from the step down.
- **Down by an operator**, to `shadow` or `off`, with the operator's name in the journal, and only when the
  sleeve is flat. Never up.

A trading day is the time between two rollovers in which the market was open at all. It is clean when the
trader saw its close, ran for at least the configured part of it, was never stopped uncleanly in it, and no
latch, break or change of the sleeve's stage fell in it. With that part not configured, no day counts.

Every change is in the stage journal, in the decision journal beside the orders it explains, and raises a
page through `hftlab.ops.alerts`.

### 7. What the trader refuses

It does not start on a venue that is not the registered paper venue, when the paper-only lock does not pass,
when the settings or the risk file cannot be read, or when the stage journal lets a sleeve place paper
orders while the limits are not in force. With the limits not in force and every sleeve in `shadow` it runs
in **shadow-only mode** and says so: it holds a view of the venue with no order methods and builds no gate
and no order manager.

On SIGTERM it takes no new turn, cancels nothing, flushes and closes its journals and exits 0. Working
orders are left to the manager's rules: the next start takes them back and reconciles before anything is
sent.

## Consequences

**What holds, each with a test**

- No order reaches the broker that the gate did not allow: every order the fake broker holds is an intent in
  the outbox with the gate's code (`tests/trader/test_trader.py`).
- A fill is booked once in each book, across a kill at any step: the tape, the outbox, the shadow book's
  journal, the decision journal and every send (`tests/faults/test_trader_kill.py`). The next start
  reconciles before it sends.
- A synthetic day with a disconnect between the two fills of an order replays byte for byte
  (`tests/trader/test_replay.py`).
- No default promotes anything (`tests/trader/test_stages.py`, `tests/trader/test_command.py`).

**What it costs, and what it does not cover**

- **A restart closes what it finds.** A position held over a restart, or over the rollover, is closed by
  the trader, at the market, in the window where spreads are widest. That is acceptable for the control
  sleeve, which holds for a minute. A sleeve that means to hold across the rollover needs its state kept,
  and that is a decision for F5.
- **A restart cancels working orders**, at every session start, the rollover included.
- **The shadow book is not the book of the sleeve's intents.** It is the book of the orders that were sent,
  less what the alignment closes. An entry the gate refused never happened in either book.
- **The tape is large.** One line a quote, in text. Since this record was written it is cut into parts by UTC
  day and by size, its closed parts are compressed by the housekeeping job and all tapes are held under a
  quota (`hftlab.trader.tape`, the trader runbook). Nothing removes a tape, the backup does not hold them,
  and the host's disk budget has not been measured with a trader running. The quota stops entries and then
  closes positions; it does not stop the trader watching one (ADR 0027, addendum).
- **The tape survives a kill, and all but its last beat survives a power cut.** Its lines reach the operating
  system at once. A closed part is synced before the next is opened, and with `fsync` on the part being
  written is synced on every beat. After a power cut the outbox, which is synced before every send, can
  hold up to a beat more than the tape, and that tail cannot be replayed.
- **A replay needs the day's journals as they were.** It copies their beginnings by a mark. An outbox that
  was replaced or rotated since cannot be replayed from.
- **Clean days are counted, not judged.** The rule counts days without a latch or a break. Whether the
  sleeve's result on those days earns a step is the owner's decision record, which the rule only names.
- **Requests are files.** A reset or a demotion is a file the running trader takes at its next beat. Nothing
  authenticates who wrote it beyond the host's own file permissions.
- **A real gateway.** Everything here has run against the fake broker. The five days of the F4 gate
  (`HFT-EXP-0002`) are what show whether the paper account behaves as the fake does.

## Addendum, 2026-10-11: a restart no longer has to close what it finds

"A restart closes what it finds" above now holds for a sleeve that starts cold. A sleeve whose state is saved
at the end of a session and may be believed at the next start is restored, and keeps what it holds
([ADR 0026](0026-sleeve-state-across-a-restart.md)). A restart still cancels working orders.

## Addendum, 2026-10-12: no stage above shadow while the account cannot open the positions

The capability probe's second run against the real paper account
([`HFT-EXP-0008`](../../research/experiments/HFT-EXP-0008-capability-probe-orders-asian-session/report.md))
showed that the account refuses a spot order that opens a position from flat: it holds none of the currency
that pays and may not borrow it, and it may not hold yen at all. Every sleeve enters long or short from flat.
A sleeve promoted to `min_size` would have sent orders the broker rejects, each one a fault for the order
manager and a reason for a latch. So the rollout gets one more condition, read from a file and not chosen:

- The trader reads the venue's capability file at every start (`hftlab.venues.capability`,
  `config/venues/ibkr.yaml`). For each configured pair, in its configured form, the file must show an order
  that opens a position as `accepted` on both sides, buy and sell. Anything else holds: refused, not tested,
  not described, or a form about which nothing is measured.
- While any pair is held, **no step up is taken**. A rule that is otherwise met is refused at the day's close
  and the refusal is journalled as `held`, cause `ACCOUNT_CANNOT_OPEN`, with the pair and the reason. It is
  named before the limits: it is the reason that would remain.
- While any pair is held, **the trader refuses to start with a sleeve above `shadow`**, whatever the stage
  journal says, and names the sleeves, the pairs and the reasons.
- **Shadow is untouched.** A sleeve in `shadow` runs as before, and the shadow venue still fills what this
  account would refuse ([ADR 0013](0013-shadow-cost-model.md), its note of the same date).
- The file is one of the settings a session's decisions depend on: it is in the digest, and a replay under
  another capability file is refused.

Every sleeve trades every configured pair, so one held pair holds every sleeve. Nothing here trades from cash
balances held in each currency, and nothing trades CFDs: neither is built, and which of them is to be built is
the owner's choice.

## Note of 2026-10-13: the hold, now that the account is run with a float

The addendum above describes the hold as it was built that day: any pair the capability file does not show as
accepted on both sides holds every sleeve at `shadow`. That is no longer the whole rule.
[ADR 0031](0031-spot-inventory-on-a-cash-account.md), section 9, reads the reason for each hold. With a float
in the limits, a pair whose sides were refused for want of cash, or were not tested, is held only until the
float is funded and anchored; from then on the pair is opened on the float and the risk gate's cash rules
decide each order. A pair refused because the account may not hold the currency, or for a reason that was not
captured, is held whatever is funded, unless the limits keep it on the shadow venue alone. Without a float in
the limits the addendum stands as written.

The float was funded on the real paper account on 2026-10-12
([HFT-EXP-0009](../../research/experiments/HFT-EXP-0009-first-float-funding-paper/report.md)). The capability
file still says what the probe measured, and is not what lifts the hold.

