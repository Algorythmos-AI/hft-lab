# ADR 0027: A deploy gate, a tape that is bounded, a backup that is whole, and two reports

- **Status:** accepted, 2026-10-11. Written and tested off the host, on temporary folders, synthetic journals
  and the fake broker. **Nothing here is installed on any host, and no deploy has been made with it.**
- **Context:** the host layer ([ADR 0017](0017-host-layer.md)). The number 0026 was left for a record written
  in parallel.

## Context

Four things were missing before the platform could run unattended for a week.

- **The backup was not whole.** It held the recording's journals, the shadow journals and the alert state.
  It did not hold which model is champion, the models, the stage of each sleeve, the order outbox or either
  book's journal: after a lost disk the platform would not have known what it held or what it was allowed.
- **The trader's tape was one file that only grew** ([ADR 0019](0019-trader-loop-and-staged-rollout.md),
  "What is not here"), written to the operating system and never to the disk until the session stopped. On a
  host shared with two desks that must win under pressure ([ADR 0007](0007-shared-host-isolation.md)), a
  file without a bound is a way to take them down.
- **Nobody was told how a day or a week went** unless something broke. The exit gates of F2, F4 and F6 count
  days and weeks, and nothing counted them.
- **A deploy was a page of commands** typed by the owner in the right order (the rollback runbook), with the
  rules in a table above them: only green commits of `main`, only when flat. A rule a person has to remember
  at an inconvenient hour is not a gate.

## Decision

### 1. The backup takes the trader's and the learning folders whole

`hftlab.ops.backup` lists every file under the trader's folder and the learning folder, and then leaves out
what `LEFT_OUT` names, each rule with its reason. The other way round, a list of what to take, is how the
gap arose: a journal added later was not on it. A file nobody has listed is now backed up.

Four kinds of file are left out because they are rebuilt (the status file, the lock, a request not yet
taken, anything half written). One is left out although it cannot be rebuilt: the tapes. A tape is large, as
the recordings are, and is evidence for one check, the replay of its day; the record of what was decided and
booked is the journals, which are backed up. The nightly backup is a whole new copy every night, and a copy
of every tape every night would be the disk problem again. This is said in the list, in the runbook and in
the test that holds a real trader and a real training run to the list.

### 2. The tape is cut into parts, compressed, and held under a quota

A session's tape is a small head, written once, and a run of parts. A new part begins between two turns:
when the UTC day of a turn is not the part's, or the part has reached its size. The parts read in order are
the tape as one file would have held it, so a replay does not know where the cuts were, and
[ADR 0005](0005-deterministic-replay.md) holds across them.

- The head keeps the name the session counts its tapes by, and never moves. The session module was not
  changed.
- Closed parts are compressed by the hourly housekeeping unit, with the function the recording's segments
  are compressed with, after the recording's own housekeeping and without being able to fail it.
- All tapes are counted against `tape.quota_bytes`. No session is begun at the quota, and one that reaches
  it takes no further turn and ends as one told to stop: journals flushed, orders left to the order
  manager's rules, **positions left as they are**. The alternative, to go on trading without recording
  inputs, would break the replay silently. The owner is paged at 90%. **This rule is replaced by the
  addendum below:** the quota still bounds the tapes, and no longer ends a session with a position open.
- A closed file is synced before the next is opened. With `fsync` on, the part being written is synced on
  every beat, so a power cut loses at most a beat of turns. Recovery never reads the tape.

### 3. Two reports, from the journals and from nothing else

`python -m hftlab.ops.report` makes a daily summary after the 17:00 New York rollover and a weekly scorecard
after the Friday close. Each is a function of journal lines, a calendar and a window. The rules are in the
module and are tested as properties: exact integers and no float; a part the journals do not hold is absent
and said to be absent, never zero; nothing private and no free text.

- A sleeve's result is the shadow book's ([ADR 0003](0003-two-books.md)), and it is the figure the trader
  itself journalled when the day's last session stopped. The report does not rebuild a book from fills: a
  second implementation of the accounting would be a second answer. A day whose last session did not say it
  stopped has its fills counted and no result.
- The counts towards F2, F4 and F6 are evidence, and are worded as such. A day counts as recorded when the
  recorder ran in it at all. Whether a gate is met stays the owner's to say in the roadmap.
- The report is sent through the sender the alerts use and written to a file. It keeps no state: the same
  journals give the same report, so it is rebuilt, not backed up.
- `hft-report.timer` is rendered like every unit and is not enabled by the install script.

### 4. A deploy gate: `python -m hftlab.ops.deploy`

The host's code changes through this command and through nothing else, and the owner runs it.

**The layout.** The install root becomes a symbolic link to one of several whole checkouts, and a deploy
changes the link in one rename. A checkout is never changed after it is made. Its environment is a link to
one built from its hash-locked files and named by their hash, so the same lock files are built once. The
state directory moves out of the checkout and is linked into each. A plain checkout, as the install script
leaves it, is turned into this once, by `adopt`, with everything stopped.

That is the only way to have what the brief asks for: a kill at any step leaves the old checkout or the new
one, whole. Changing a checkout in place cannot give it.

**What it refuses on.** A commit that is not on `main`. Required checks (`test`, `leakcheck`) that are not
green: looked up with the `gh` program when the host has it; when they cannot be looked up, the owner's
explicit word on the command line stands in, and never for a check that was looked up and is red. A broker
book that is not flat, or an order that has not ended, read from the order outbox; an outbox that does not
verify is refused, not taken as flat. A trader that is running while the market is open. A recorder restart
inside thirty minutes of the rollover. Unit files that are not the installed ones.

**What it does,** each step printed before it runs, and none run in a dry run: fetch `main` into a mirror;
check the commit out into a new folder; build or reuse the environment; run the smoke tests in that folder
with its own interpreter; swap the link; restart the units named; check the platform's status, and if a
problem is there that was not there before, swap back and restart the same units, by itself.

**What it can restart.** The recorder and the shadow runner, each only if it is running. Neither holds an
object that could place an order ([ADR 0004](0004-recorder-separate-from-trader.md), ADR 0003). **It never
restarts, starts or stops the trader.** A trader that is running, which the gate allows only with the market
shut and the book flat, goes on running the checkout it was started in, which is still there and untouched,
and the command says that restarting it is the owner's. So no path through this command restarts a unit
that holds a position.

**The smoke tests** are a list of commands: every unit's own module started as far as importing everything
it needs, the unit files compared with the rendered ones, the trader reading its settings, the status
command reading the settings it shares. The host's environment has no test runner, by design, so the deploy
runs the list itself; the tests marked `smoke` run the same list in CI.

**Programs, not connections.** The module starts `git`, `uv`, `systemctl` and `gh`. It opens no connection
itself: the rule that only `hftlab/ops/post.py` and the broker adapter do is unchanged, and the structure
test now names the deploy beside the backup as a module that may start a program.

## Consequences

- A lost disk is recoverable to the last night: what was held, what each sleeve was allowed, which model
  served. It is not replayable for the days before the loss.
- The trader can stop itself, at the tape quota, with a position open. That is chosen over a full disk and
  over an unrecorded input. The quota in `config/trader.yaml` is a placeholder until the host's disk is
  measured. (No longer: see the addendum.)
- The owner gets at most one page a trading day and one more on a Friday, when the timer is enabled.
- A deploy is one command and going back is one command, and both ask the same gate.

## What is not here

- **Nothing has run on the host.** The link layout has been exercised on temporary folders only. Whether
  the service manager resolves the links in `WorkingDirectory` and `ReadWritePaths` as expected is to be
  seen in the first `adopt`, which is a drill with everything stopped.
- **The install script does not know the layout.** It installs a plain checkout, and `adopt` converts it.
  After that the install script must not be used to move the code: the deploy does that.
- **Unit files are not installed by the deploy.** It refuses when they differ and the runbook gives the two
  commands. Installing them needs the service manager told, as root, and is left the owner's step.
- **The real look-up of the checks, the real environment build and the real status check have not been
  run.** Their parsing and their commands are tested; `gh`, `uv` and `systemctl` are not called in a test.
- **The reports are not on the dashboard.** They are a page and a file. The status snapshot's contract is
  unchanged.
- **Old releases and old environments are never removed.** Thinning them is the owner's, by hand. (No longer:
  see the addendum on pruning.)
- **The status screen does not show the tape quota.** The trader pages at 90% and the daily summary shows
  the figure. (It does since the addendum.)

## Addendum, 2026-10-11: a full tape closes positions; it does not stop watching them

Built and tested against the fake broker only, like the rest of this record. Nothing here has run on a host.

### What was wrong

Section 2 ended a session at the quota with its positions left as they were, and refused the next start. A
byte budget ran out, and the answer was to stop watching what was held: the one thing a trader that is short
of anything should do last. The reason given, that trading on without recording would break the replay, is a
reason not to open anything. It is not a reason to look away.

### The rule

There are two limits under the quota, and the tape says on a turn's own line when the room changes, so a
replay sees the same room at the same turn ([ADR 0005](0005-deterministic-replay.md)).

```
0 ............ soft .............. hard ............................ quota
  everything     exits only           closing only: the reserve
```

| The tapes hold | What the trader does | How |
|---|---|---|
| less than the soft limit (`tape.soft_percent` of the quota, 90 as shipped, a placeholder) | everything its stages allow | |
| the soft limit or more | **opens nothing; exits go on.** One line in the decision journal, one page (`hft:tape_near_quota`) | The risk gate is shown the room (`hftlab.risk.TapeRoom`) and its verdict is `EXITS_ONLY` under one code, `TAPE_SOFT_LIMIT`, for every sleeve. A sleeve that trades on the shadow venue alone is stopped the way the switches stop it. No stage is touched |
| the hard limit or more: the quota less the reserve | **closes everything, then ends the session.** One line, one page (`hft:tape_hard_limit`) | The gate proposes the closes it proposes under `FLATTEN`, with no switch set and even when the switches cannot be read. Each passes `check` as an exit, against the exit collar, and a refused one comes again after `flatten_retry_seconds` ([ADR 0015](0015-risk-gate.md)). The trader passes them on, on every beat, by the path it already had |

No stage is used for this, on purpose: a stage goes up only by its rule and a decision record, and room on a
disk comes back when the housekeeping job has passed. The gate's verdict follows the room both ways.

**How the session ends at the hard limit.**

1. **Flat, and it stops.** When the broker book holds no line, no order is on its way and the connection has
   been made safe, the session writes `tape_flat` and ends as a stop does: the closes, their fills and the
   `stop` are on the tape, in the reserve, and the session replays byte for byte. The shadow book is closed
   too, as under `FLATTEN`, and waited for, but no longer than the time limit: it holds nobody's money, and
   what it still holds then is written into that line. The process does not exit. It waits, with no session
   and nothing written, counting the tapes on every beat, and begins the next session by itself when they
   are under the hard limit.
2. **The time limit passes with something open** (`tape.flatten_seconds`, 300 as shipped, a placeholder): a
   close is being refused, by the exit collar on a blown-out book, by a stale quote, by a market that is
   shut. The owner is paged at the highest priority (`hft:tape_flatten_overdue`), the decision journal says
   exactly what is open (`tape_overdue`: each line with its quantity, each order on its way, and whether the
   connection is safe), and **the session, the reconciler and the closes go on.** Nothing is forced through
   a bad book, and nothing is left unwatched. When the book comes back it is closed, and 1 applies.
3. **The reserve itself is used up.** This is the one case in which the trader stops writing its tape: a
   line that would not fit under the quota is not written, and the tape's last line is `["dark"]`, whose few
   bytes are held back from every line before it. What is guaranteed then: the tapes are not past the
   quota; the session goes on exactly as in 2, with the decision journal, the outbox and the books' journals
   written as always; the owner is paged at the highest priority (`hft:tape_reserve_exhausted`); and the
   replay of that session is the same as far as its tape goes (`"ended": "dark"`). What is lost is the
   replay of the rest of that one session. When room is made the session ends and the next one is recorded.

**The next start is not refused.** It begins a session whatever the tapes hold, because only a session
reconciles and only a session closes what a kill left open. At the soft limit it starts on exits only and
says so. At the hard limit it finishes the closing and ends as in 1. With no room at all it writes its head,
two lines, and its `["dark"]`, and runs unrecorded: that head is the only thing that can pass the quota, by
about a kilobyte for each start. What the trader still refuses to start on is a quota that cannot hold the
reserve.

### The reserve, and where its size comes from

From the hard limit on, the tape is thinned so that the reserve lasts: a turn is taken on a beat and on
nothing else, and of each pair's quotes since the last turn only the newest is kept. What is dropped was
never shown to the trader, so the tape is still everything it decided on.

The reserve is the most the tape can take in `tape.flatten_seconds`, with the worst of everything at once
(`hftlab.trader.room`):

```
reserve = STARTS x head  +  turns x (step + asked)  +  orders x order  +  the last line
```

| Term | What it is | Where the number comes from |
|---|---|---|
| `turns` | one a beat for the whole of the time limit, and one at each end | `tape.flatten_seconds`, `beat_ms` |
| `step` | a turn's line: the newest quote of every pair, and a beat | `pairs` |
| `asked` | the venue's whole answer when the connection is made safe on that turn: every order that can be listed as working, every fill that can be listed, a position a pair | the desk's `max_orders_per_minute`, over the time limit and the five minutes before it that the reconciler asks back |
| `orders` | every order the gate can let through, exits, retries and the sleeves' own orders alike | the desk's `max_orders_per_minute`, over the time limit and the minute before it. The gate counts every order against that limit and refuses the next: no order is outside it |
| `order` | its send and its cancel as the order manager saw them, its reports, its fills and their commissions | assumed, below |
| `head` | a session's two first lines, for the start in hand and two restarts | assumed, below |

The largest book the limits allow is every sleeve at its cap (the pair's `max_position` or its own,
whichever is less) in `max_open_pairs` pairs; sleeves may hold against each other, so the desk's cap does
not make it smaller. Closing it takes that many orders of at most `max_order_size`, each filled in full. **A
start is refused** unless the reserve's `orders` covers them, the deepest line can be closed one order a
beat inside the time limit, and the hard limit lies above the soft limit by the sends of one turn's
entries. A turn whose own line ends at or past the soft limit is already on exits only, so no entry is taken
on a turn that reaches the reserve.

So the reserve holds the closing of every book the limits allow, and every retry the rate limit lets through
on top of it, for the whole of the time limit.

Three things are not limits and are assumed, generously for what the fake broker and the adapter send: no
number on a line is larger than 2^63, no text a venue writes into one field is longer than 96 bytes, and one
order has at most four reports and four fills. Where one of them does not hold, or the closing takes longer
than its time limit (case 2), the reserve runs out sooner, and that is case 3.

**It is large on purpose.** `asked` is the term that decides it: a connection that is lost for the whole of
the time limit is made safe again on every turn, and each time the venue may list everything. With thirty
orders a minute, one pair and 300 seconds that is about 150 MB; with six a minute and 60 seconds, 4 MB. A
quota that cannot hold it above the soft limit is refused at the start with the least quota that would, and
the ways out are in that message: a larger quota, a lower `soft_percent`, a shorter `flatten_seconds`, fewer
orders a minute.

### What holds, each with a test

- At the soft limit the only order that goes out closes what is held, every entry is refused under one
  code, there is one line and one page, and no stage moves; the same for a sleeve on the shadow venue alone
  (`tests/trader/test_tape_room.py`).
- At the hard limit a held position is closed by one order of the gate's, its send and its fill are on the
  tape after the line that says `hard`, the tape ends with `stop`, and from that line on every turn has a
  beat and at most one quote a pair.
- A spread blown out beyond the exit collar at the hard limit: the closes are refused at the gate's own
  interval and no faster, the owner is paged once the time limit has passed and not before, the journal
  names the open line, the session goes on taking turns and the reconciler goes on asking; when the spread
  returns the position is closed and the session ends.
- With a reserve too small: the tapes never pass the quota, the tape ends with `["dark"]`, the session goes
  on deciding and closes the position when it can, and its replay is identical as far as the tape goes. A
  process killed then, with a position open and no room at all, starts again, writes its head, reconciles
  and closes.
- A kill before and after every step of the closing, from the turn that says `hard` to the line that says
  flat: the next start reconciles first, finishes the closing and stops; the broker executed one entry and
  one close, each once; both sessions replay (`tests/faults/test_trader_kill.py`).
- A start at the soft limit begins on exits only, and opens positions again, with a journalled line, once
  the housekeeping job has made room. Flat at the hard limit the process waits with no session, and begins
  one when there is room.
- For generated limits, any book they allow, closed with every order the rate limit lets through, each
  with the most reports and fills assumed, on every turn of the time limit with everything listed, written
  with the tape's own encoders, fits the reserve, and the reserve is less than twice that
  (`tests/trader/test_tape_reserve.py`). A quota that cannot hold the reserve, and a time limit too short
  for the limits, are refused by the arithmetic, by the trader and by the command.
- The status screen shows the tapes' bytes, counted when it is run, against the soft limit, the hard limit
  and the quota, with a warning at either limit, as for a latch: the pages are the trader's own; the daily summary shows
  the same against the limits the day's last session journalled (`tests/ops/`).

### What it costs, and what it does not cover

- **Closing at the hard limit is closing at the market, whenever that is.** A sleeve's position is closed
  for a reason that has nothing to do with the sleeve. The soft limit and its page exist so that room is
  made first: the hard limit is the stop, not the plan.
- **A sleeve whose state was restored loses its position there** like any other. `FLATTEN` does the same.
- **The time limit is counted by a session.** A session that ends at the rollover while it is closing is
  followed by another, which is told `hard` on its first turn and counts from there.
- **While the process waits for room nobody watches the account.** It waits only when its book was flat,
  nothing was on its way and the connection had been reconciled. A position opened by another hand in that
  time is found by the next session's reconciliation, not before.
- **The status file is not refreshed while it waits.** The screen counts the tapes itself, so the bytes and
  the level are current; the trader shows as stopped by the age of its last word.
- **A session that went dark cannot be replayed past its tape,** and the sleeves' records of rows a scorer
  process did not score are counted with the tapes and are not thinned.
- **An operator's requests during a closing are on the tape and are not in the arithmetic.** Each is a
  short line.
- **Nothing removes a tape, still.** Room is made by the housekeeping job compressing closed parts, the
  last part of a session that stopped or went dark among them, and by the owner moving old sessions away.
- **A market that is shut is a closing that waits.** At the hard limit over a weekend the closes are refused
  until the market opens: the owner is paged when the time limit has passed, and two days of beats, about
  a hundred bytes each, come out of the reserve. If that uses it up, it is case 3.
- **The reserve is a bound on bytes, not on time or money.** That the closing can finish inside the time
  limit is checked against the limits; that the market lets it is not something a limit can promise.

## Addendum, 2026-10-11: old checkouts are pruned by a command

"What is not here" said that old releases and environments are never removed. `python -m hftlab.ops.deploy
prune` removes them now; `run` and `back` still remove nothing.

**What it keeps.** The checkout the install root links to. Both commits of the last swap's record, so that
`back` always has its target, before a step back and after one. Every checkout that a running process has its
working directory in: section 4 leaves the trader running the checkout it was started in, however many
deploys ago, and removing that checkout would take the code from under a process that may hold a position.
And of the rest the newest, as many as `deploy.keep_more_releases` in `config/ops.yaml`, in the order of the
deploy journal. An environment goes only when no kept checkout links to it.

**What it refuses on.** Not knowing. Where the running units run is looked up from the service manager and
the process table; when a running unit's folder cannot be read, or the service manager cannot be asked,
nothing is removed. A deploy journal that does not verify gives no order, and nothing is removed. A root
that is not a link into the layout: nothing is removed. A deploy that is running holds the lock.

**How it removes.** Everything it will keep and remove is printed first, then each removal before it is
done, and `--dry-run` does none. A folder is moved out of `releases/` in one rename and removed after that,
oldest first. So a checkout is whole or gone, whenever the command is killed, and what is left is always the
newer part; the next run finishes what a kill left.

**What it does not cover.** Where the units run is read on Linux, from `/proc`; the test hands in a made-up
one, and the real look-up has not run on a host. A process that reads a checkout without having its working
directory in it is not seen. The mirror keeps every commit, so a removed checkout can be deployed again.
