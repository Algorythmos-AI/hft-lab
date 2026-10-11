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
  inputs, would break the replay silently. The owner is paged at 90%.
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
  measured.
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
- **Old releases and old environments are never removed.** Thinning them is the owner's, by hand.
- **The status screen does not show the tape quota.** The trader pages at 90% and the daily summary shows
  the figure.
