# Runbook: the trader

For the owner. The trader is `hft-trader.service`: the sleeves, the risk gate, the order manager and the
reconciler in one process ([ADR 0019](../adr/0019-trader-loop-and-staged-rollout.md)). It trades on the
broker's **paper** account only, and with the configuration as shipped it trades nothing there at all.

**Nothing on this page has been followed on the host.** The trader has run against the fake broker only.

Where things are, on the host:

| What | Where |
|---|---|
| Its journals, tapes and status | `/home/hft/hft-lab/var/trader/` |
| A day's decisions and tapes | `/home/hft/hft-lab/var/trader/days/<New York date>/` |
| The switches | `/home/hft/hft-lab/var/KILL`, `/home/hft/hft-lab/var/FLATTEN` |
| Its settings | `config/trader.yaml`, `config/risk.yaml`, `config/cost_model.yaml` |

Every command below is run from `/home/hft/hft-lab` as the user `hft`, with `PYTHONPATH=src` and the
installed interpreter: `sudo -u hft env PYTHONPATH=src MODE=paper .venv/bin/python -m hftlab.trader ...`,
written `trader ...` from here on.

## 1. Start it, in shadow

The unit has no install section. Nothing can enable it, the install script included; it runs while you have
started it.

1. Check that the gateway is up and logged in to the paper account ([weekly login](weekly-login.md)).
2. `sudo systemctl start hft-trader.service`
3. `sudo journalctl -u hft-trader.service -n 20`

**You should see** these lines, in this order:

```
trader: SHADOW-ONLY: the risk limits are not in force. No order can reach a broker from this run.
trader: session 1 of 2026-10-14; stages {"control": "shadow", "idle": "shadow"}
```

`sudo hft-status` then shows a `trader` line: `running; shadow-only`, and each sleeve with its stage.

**If it says `refused`,** it stopped on purpose and will not be restarted. The line says why:

| It says | It means |
|---|---|
| `refused by the paper-only lock` | The gateway is not a paper gateway, or the mode is not paper. Do not work round it |
| `... may place paper orders by the stage journal, and the risk limits are not in force` | A sleeve's stage is above `shadow` and `config/risk.yaml` is not in force. One of the two is wrong |
| `... may place paper orders by the stage journal, and the capability file does not show that the account can open the positions they need: PAIR: ...` | A sleeve's stage is above `shadow` and `config/venues/ibkr.yaml` does not show, for that pair, an order that opens a position accepted on both sides. Today it shows the opposite ([`HFT-EXP-0008`](../../research/experiments/HFT-EXP-0008-capability-probe-orders-asian-session/report.md)): the account refuses a spot order from flat. Only shadow is allowed. The file changes only from a probe run that is recorded; do not edit it to get past this. With a float in the limits this refusal is for a pair held whatever is funded; see the note of 2026-10-13 under section 2 |
| `cannot read the capability file ...` | The venue's capability file is missing or is not one. The trader does not start without it |
| `another trader holds ...` | One is already running |
| `a journal or a tape cannot be used` | A journal does not verify. It is never repaired: keep the file, and read section 6 |
| `tape.quota_bytes of N cannot hold the reserve ... the quota must be at least M` | The quota is too small for the limits: closing what they allow could not be recorded. Section 8, "If the start is refused" |
| `tape.flatten_seconds is too short for the limits` | The largest book the limits allow cannot be closed inside the time limit. The same section |

Full tapes are **not** a reason it refuses. With the tapes at a limit it starts and says
`trader: EXITS ONLY: the tapes hold N bytes, at their soft limit (soft S, hard H, quota Q)`: section 8.

**If it says `PAPER: limits in force by HFT-DEC-NNNN`** and you did not expect that, stop it (section 4).
Paper orders are possible only after a decision record set the limits and another approved the sleeve's
step.

## 2. Read the stages

`trader --stages`

```
control: shadow; 0 clean trading day(s) in a row in this stage
idle: shadow; 0 clean trading day(s) in a row in this stage
```

| Stage | What the sleeve's orders do |
|---|---|
| `off` | The sleeve is not run |
| `shadow` | Shadow venue only |
| `min_size` | Paper orders; everything that adds to a position at the pair's minimum size |
| `normal` | Paper orders at the size the sleeve asks for |
| `stepped_down` | Paper orders that reduce a position only |

A stage goes up only at a day's close, by the rule in `config/trader.yaml`: the number of clean trading days
in a row, the decision record that approved the step, and the risk limits in force. It never goes up by a
command. The stage journal, `var/trader/stages.jsonl`, has a `day` line for every trading day and sleeve,
with `clean` and, when it was not, `causes`:

| Cause | What happened that day |
|---|---|
| `LATCH`, `BREAK` | A latch tripped, or a reconciliation break was flagged |
| `UNCLEAN_STOP` | The trader was killed, not stopped |
| `NOT_RUNNING` | Nobody saw the day close |
| `TOO_SHORT` | It ran for less than `clean_day_minutes`, or that is not set yet |
| `STAGE_CHANGED` | The sleeve's stage changed during the day |

A line of type `held` says a rule was met and the step was refused, and why: `ACCOUNT_CANNOT_OPEN` (its
`detail` names the pair and what the capability file says of it), `LIMITS_NOT_IN_FORCE`, `STILL_LATCHED` or
`STILL_BROKEN`. At a start the trader also says `SHADOW IS THE HIGHEST STAGE`, with the same pairs and reasons,
for as long as the capability file holds any of them.

Note of 2026-10-13. That is the hold without a float. The paper account is now run with one
([ADR 0031](../adr/0031-spot-inventory-on-a-cash-account.md), section 9), and then, while no pair is held for
another reason, the trader says `ON THE FLOAT` at a start and not `SHADOW IS THE HIGHEST STAGE`: a pair that was refused only for want of cash, or
whose side was not tested, is held until the float is funded and anchored, and after that the risk gate's cash
rules decide each order. A pair the account may not hold at all, or whose refusal was not captured, is still
held whatever is funded, unless the limits keep it on the shadow venue alone. The float was funded on the real
paper account on 2026-10-12, and the same session ended in a reconciliation break after a reconnection
([HFT-EXP-0009](../../research/experiments/HFT-EXP-0009-first-float-funding-paper/report.md));
[paper-trading-on-spot.md](paper-trading-on-spot.md), section 8, says what that run showed.

## 3. Take a sleeve down

`trader --demote control shadow --operator <your name>` (or `off`).

The running trader acts on it within a second and writes a `request` line into the day's decisions. It is
done only when the sleeve is flat and has no order on its way. If the line says `"refused": "NOT_FLAT"`, set
`FLATTEN`, wait for it to finish (section 4), clear it, and ask again.

There is no command that takes a sleeve up.

## 4. Kill and flatten

| To | Run | What happens |
|---|---|---|
| Stop new entries | `sudo -u hft touch /home/hft/hft-lab/var/KILL` | Within a second: no order that adds to a position, on paper or in shadow. Exits go on |
| Close everything | `sudo -u hft touch /home/hft/hft-lab/var/FLATTEN` | No entries, and one market order for each open line, through the risk gate. A close that is refused is tried again after `flatten_retry_seconds` |
| Stop the process | `sudo systemctl stop hft-trader.service` | It takes no new turn, cancels nothing, closes its journals and exits. Positions and working orders stay where they are |

**FLATTEN has finished when** `sudo hft-status` shows `0 open position(s)` on the trader line. Then, and not
before, remove the file.

The trader closes everything by this same path, with no file set, when its tapes reach their hard limit
(section 8). `sudo hft-status` then shows `AT THE HARD LIMIT: closing everything` and `FLATTEN off`.

**If positions stay open under FLATTEN,** the closes are being refused. The day's decisions say with which
code: `grep '"verdict"' var/trader/days/<date>/decisions.jsonl | tail`. `STALE_QUOTE` and `MARKET_SHUT` mean
there is no market to close into; `PRICE_COLLAR` means the book is beyond even the exit collar. They are
tried again by themselves. `NOT_RECONCILED` means the connection is not safe yet: see section 5.

**Stopping the process does not make you flat.** Set `FLATTEN` first if flat is what you want. What a stop
leaves is taken up by the next start: working orders are cancelled and the books are reconciled before
anything is sent, and a position left from before is closed by the trader itself.

## 5. A reconciliation break

A break means the broker's positions are not the ones the trader's book of the broker's fills adds up to.

**What it looks like.** A page titled `Reconciliation break`. `sudo hft-status` shows
`1 reconciliation break(s) today` and the sleeves that were placing paper orders at `stepped_down`. The
day's decisions hold a line `"type":"break","broken":1`, and `var/trader/reconcile.jsonl` holds
`break_opened` with the pair and the two quantities, ours first.

**What the trader has already done.** Stopped every entry, stepped the paper sleeves down, and left exits
alone. If the line says `"blind":1`, the difference was found while making a connection safe, and nothing
at all is sent until it is gone.

**What to do.**

1. Do not restart the trader to make it go away. A restart finds the same difference.
2. Read the difference: `grep break_opened var/trader/reconcile.jsonl | tail -1`, or, for a blind one,
   `grep positions_differ var/trader/outbox.jsonl | tail -1`.
3. Look at the paper account in the broker's own screen. An order placed by hand, a position carried from
   before the trader ever ran, and a corporate adjustment by the broker are the usual explanations.
4. If the broker holds a position the trader does not know, close it by hand in the broker's screen. That is
   yours to do; the trader will not trade a position it cannot explain.
5. When the two agree again the trader writes `break_cleared` and pages `Reconciliation break over`. The
   sleeves stay `stepped_down`: the way back to `min_size` is the rule, counted from the step down.
6. Write down what it was. A break with no explanation is not closed by going away.

Note of 2026-10-13. The first break on the real paper account was a blind one of another kind: the broker's
side held nothing the trader did not know, and the gateway had stopped listing the float's own conversions as
positions after it lost and regained its connection to the broker
([HFT-EXP-0009](../../research/experiments/HFT-EXP-0009-first-float-funding-paper/report.md)). The book was right and
the list was empty. Steps 3 and 4 do not end that one, and while the connection is not made safe a request to
take the float back or to anchor again is answered `NOT_SETTLED`. What ends it is not written yet: see
[paper-trading-on-spot.md](paper-trading-on-spot.md), section 8.

## 6. A latch

A latch is the risk gate's stop: a loss limit, a streak of rejects, or something the order manager saw that
cannot happen (`ORDER_FAULT`). It stops entries until **you** reset it. It survives a restart, the end of
the day and the price coming back.

**What it looks like.** A page titled `Latch tripped: <name>`. `sudo hft-status` shows
`latched: <name>`. `var/trader/risk.jsonl` has the `trip` line with the numbers.

**Before you reset it,** find out why it tripped. For `ORDER_FAULT`, the reason is the last `fault` line of
`var/trader/outbox.jsonl`. For a loss latch, it is the loss.

**To reset it:** `trader --reset <name> --operator <your name>`, with the name exactly as the status shows
it, for example `DAILY_LOSS_SLEEVE/control/USD`.

The running trader does the reset, because only it knows the positions and the quotes the latch starts
again from. Within a second the day's decisions hold a `request` line with `"done":1`, `risk.jsonl` holds
a `reset` line with your name, and you get a page that it is over. `"done":0` means no such latch was
tripped, or a position it measures has no quote yet.

Only the owner resets a latch. An automated contributor never does.

## 7. Replay a day

`trader --replay var/trader/days/2026-10-14`

It runs each session of that day again from its tape, in a scratch folder, and compares the decisions with
the ones the day left. Nothing is sent and nobody is paged.

**You should see** every session with `"identical": true`, and exit code 0. A session that was killed says
`"ended": "killed"` and is still identical as far as it got.

**If one is not identical,** that is a defect to report with the day's folder: something the trader decided
depended on something it did not record. `refused: ... recorded under other settings` means the
configuration changed since that day; check out the commit the day ran on and replay there.

A day replays the same whether its tape is in one part or many, and whether the parts are compressed or
not. `refused: ... one is missing, and the tape has a hole` means a part of that session was moved away or
removed: bring it back beside the others and replay again.

A session that says `"ended": "dark"` ran out of the tapes' reserve and went on unrecorded (section 8). It is
identical as far as its tape goes, and `replayed_bytes` is smaller than `live_bytes`: the rest of that
session was decided and journalled, and cannot be replayed.

## 8. The tapes and their quota

The tape is what the trader was shown from outside, one line each: it is what a replay runs on. On disk, in
the day's folder:

| File | What it is |
|---|---|
| `session-001.tape.jsonl` | the head: how the session began. Two lines, written once. The session number comes from counting these, so **never move or rename a head** |
| `session-001.part-0001.2026-10-14.jsonl` | the turns. A new part begins at each UTC midnight and when one has grown to `tape.part_bytes` (64 MB as shipped), always between two turns |
| `session-001.part-0001.2026-10-14.jsonl.gz` | a closed part, compressed |

Closed parts are compressed by the hourly `hft-housekeep.service`, after it has done the recording's own
housekeeping. By hand: `trader --housekeep`.

**You should see** `trader: tapes: compressed N closed part(s); B bytes on disk, soft limit S, hard limit H,
a quota of Q`. It never
touches the part a running trader is writing, and it deletes nothing but the plain copy of a part whose
compressed copy it has read back and found the same.

All tapes together are held under `tape.quota_bytes` in `config/trader.yaml`. That number, and the two
beside it, are **placeholders**: set them from the host's measured disk budget, by pull request, before the
trader runs. The decision journals and the other journals are not counted: they are the record, and they
are small.

**The quota never stops the trader watching a position**
([ADR 0027](../adr/0027-deploy-gate-and-reports.md), addendum). There are two limits under it:

```
0 ............ soft .............. hard ............................ quota
  everything     exits only           closing only: the reserve
```

| The tapes reach | The trader | You get | You do |
|---|---|---|---|
| the soft limit: `tape.soft_percent` of the quota, 90 as shipped | opens nothing more. Exits go on. Stages are not touched | one page, `hft:tape_near_quota`, "Tapes at their soft limit: exits only" | **Make room now** (below). It trades again by itself within a second of there being room |
| the hard limit: the quota less a reserve worked out from `config/risk.yaml` | closes every position, as under `FLATTEN` but with no file set, and ends the session when the broker book is flat. Then it waits, running, with no session | one page at the highest priority, `hft:tape_hard_limit` | Make room. It begins a session by itself when the tapes are under the hard limit |
| the hard limit, and a position is still open after `tape.flatten_seconds` (300 as shipped) | **stays up**, on exits only, and goes on proposing the closes | one page at the highest priority, `hft:tape_flatten_overdue`, "NOT FLAT at the tapes' hard limit" | Read what is open and why (below). Make room |
| the quota itself: the reserve is used up | **stays up** as above, and stops writing its tape. Everything else is still written | one page at the highest priority, `hft:tape_reserve_exhausted` | Make room at once. That session can be replayed only as far as its tape goes |

**Where it stands.** `sudo hft-status` has a line under the trader:

```
             tapes: 912,345,678 bytes; soft limit 900,000,000, hard limit 950,000,000, quota 1,000,000,000 (91%); AT THE SOFT LIMIT: exits only
```

The bytes are counted when you run it; the limits are the trader's last word. The status shows a warning at
either limit; the pages are the trader's own. `trader --housekeep` prints the same three numbers, and the
daily summary shows them on its `disk` line.

**To make room:** run `trader --housekeep` first. If that is not enough, move the oldest day folders'
`session-*` files to another disk, whole sessions at a time, head and parts together. The days you move can
be replayed again only after they are moved back. Nothing in this platform removes a tape, and the backup
does not hold them ([backup](backup.md)). You do not restart anything: the running trader counts the tapes
again every second while it is at a limit, writes a `tape` line into the day's decisions when the room
changes, and takes back its page.

**What the day's decisions say.** `grep -E '"type":"tape' var/trader/days/<date>/decisions.jsonl`:

| Line | It means |
|---|---|
| `"type":"tape","room":"soft","code":"TAPE_SOFT_LIMIT"` | From this turn on nothing is opened. Refused entries carry that code |
| `"type":"tape","room":"hard","code":"TAPE_HARD_LIMIT"` | From this turn on everything is closed |
| `"type":"tape","room":"room"` | There is room again: back to normal |
| `"type":"tape_flat"` | The broker book is flat at the hard limit: the session ends. `shadow` lists what the shadow book still held, if the time limit passed first |
| `"type":"tape_overdue","open":[...],"in_flight":[...],"blind":0` | The time limit passed. `open` is every line still held, with its quantity; `in_flight` every order on its way; `blind: 1` means the connection is not safe yet and nothing can be sent (section 5) |

**If it says NOT FLAT,** the closes are being refused, and the reason is the same as under `FLATTEN`
(section 4): `grep '"verdict"' var/trader/days/<date>/decisions.jsonl | tail`. `PRICE_COLLAR` is a book
beyond even the exit collar, `STALE_QUOTE` and `MARKET_SHUT` are no market to close into. They are tried
again by themselves, every `flatten_retry_seconds`, and the position is closed when the book allows it.
**Do not stop the trader:** it is the thing that is watching the position. Make room, so that it goes on
being recorded.

**If the trader is restarted while the tapes are at a limit,** it starts. It reconciles, closes what a
kill left open if the tapes are at their hard limit, and waits for room as above. With no room at all
it still starts: it writes its head, about a kilobyte, which is the only thing that can pass the quota, and
runs unrecorded until there is room.

**If the start is refused** with `tape.quota_bytes of N cannot hold the reserve`, the settings do not fit
together, and no trader should run until they do. The reserve is what the tape can take in
`tape.flatten_seconds` when everything goes wrong at once, and most of it is the venue's answers while a
lost connection is made safe: it grows with the desk's `max_orders_per_minute` and with the time limit.
The message names the least quota that works. The ways out, each a pull request: a larger `quota_bytes`
inside the disk budget, a lower `soft_percent`, a shorter `flatten_seconds`, or fewer orders a minute in
`config/risk.yaml` (which needs its decision record). `tape.flatten_seconds is too short for the limits`
is the other way round: the largest book the limits allow cannot be closed in that time.

**What a power cut can lose.** A kill of the process loses no line of the tape. A power cut is different:

- The head and every closed part are on the disk before the next file is opened. A power cut loses none of
  them.
- With `fsync: true`, as shipped, the part being written is synced on every beat (`beat_ms`, one second as
  shipped). A power cut can lose the lines written since the last beat: at most a second of turns and the
  turn in progress. With `fsync: false` it can lose everything in the part being written.
- The trader never reads the tape to recover. After a power cut it starts from the journals and asks the
  broker, as after any restart. What the lost lines cost is the end of that one session's replay: it is
  shown as `"ended": "killed"`, and it can report `"identical": false` with `replayed_bytes` smaller than
  `live_bytes`, because the tape ends up to a beat before the decision journal does. That is the power cut,
  not a defect in the trader. Every earlier session of the day still replays whole.

## 9. Sleeve state, and "started cold"

A sleeve remembers things: its warm-up, a model's feature history, why it holds what it holds. A session that
ends on purpose (the day's close, a stop) writes them into `var/trader/sleeves.jsonl`, and the next start
gives them back ([ADR 0026](../adr/0026-sleeve-state-across-a-restart.md)). Every start writes one
`sleeve_state` line into the day's decision journal: the sleeves restored, and for each other one the reason.

**Started cold** means a sleeve began from nothing, as every sleeve did before this file existed: it warms
up again, and the trader closes what it was holding, at the market. **It is safe.** It is what the trader does
whenever it cannot show that the saved state is still true, and a state that might be stale is never used.
What it costs is the sleeve's warm-up, and on paper the spread on the position that is closed. You get one
page, `hft:sleeve-state`, naming each sleeve and its reason; it resolves at the next start that restores.

| Reason | What it means | What to do |
|---|---|---|
| `NONE` | No state was ever saved: a first start. No page | Nothing |
| `JOURNALS` | Something was written to a journal after the state was saved. The ordinary case: the session before this one was killed, or lost power, and saved nothing | Nothing. Look for why it was killed |
| `TAPE` | The tape of the session that saved the state is not the last tape, or is not as it was: a part was added, is missing, or was changed | Nothing, if you moved that session's tape away to make room (section 8). Otherwise say what happened to the folder in an issue |
| `UNVERIFIED`, `NOT_A_STATE` | The file's chain is broken, or its last line is not a state of this version | Nothing by hand. The file is set aside as `sleeves.jsonl.unverified` at the next clean stop and a new one begins. Open an issue with the file that was set aside |
| `IN_FLIGHT`, `STAGE`, `SETTINGS`, `ABSENT` | One sleeve only: it had an order on its way, its stage or its settings changed, or it is new | Nothing |

Things that do **not** make a start cold: the housekeeping job compressing the tape's parts, the last part
among them; moving older sessions' tapes to another disk; the tapes being at a limit (section 8), whether
the session before ended there or this one starts there.

Never edit, shorten or delete `sleeves.jsonl` to "fix" a cold start, and never copy one in from a backup to
get a warm one: a restored file names a tape the backup does not hold, and is not believed.

To tell whether the state was saved, the trader reads the whole tape of the session that just ended, once
at the stop and once at the next start. On a large day that takes seconds. If a stop is cut off by the
service manager before it finishes, no state was saved, and the next start is a cold one.
