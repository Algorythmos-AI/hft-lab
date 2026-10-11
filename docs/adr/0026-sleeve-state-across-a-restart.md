# ADR 0026: Sleeve state across a restart, and the two commands the registered experiments name

- **Status:** accepted, 2026-10-11. Built and tested on synthetic streams and against the fake broker only.
  Nothing here has run on the host, and no state has been saved from or restored on a real feed. Changed the
  same day, before it was merged: an entry is tied to a tape that is a head and parts (point 3).
- **Context:** F3, F5 and F6. [ADR 0019](0019-trader-loop-and-staged-rollout.md) left the first open: "a
  sleeve that means to hold across the rollover needs its state kept". `HFT-EXP-0001` and `HFT-EXP-0005` each
  name a command that did not exist.

## Context

A sleeve's state is everything it remembers ([ADR 0014](0014-sleeves-as-pure-functions.md)), and the trader
kept it in memory only. Every start began from nothing, and a session ends every day at the rollover, so:

- a Directional Change machine was back in its warm-up at every start
  ([ADR 0024](0024-directional-change-sleeve.md)), and the events after a start were thrown away;
- a model's sleeve had no feature history, and gave no row until its longest window was full again;
- a position held over a start was closed by the trader, at the market, in the window where spreads are
  widest, because no sleeve remembered why it was taken.

The Directional Change detector could already write its state out and read it back exactly. Nothing called
it. Keeping state is easy. The risk is the other way round: a state read back when it should not have been
is a sleeve that believes something untrue about the market or about what it holds, and decides on it.

Two registered experiments could not be run as written. `HFT-EXP-0001` measures the control sleeve's round
trips against the cost model's figure for the quotes at their two ends, and said that no tool joins a journal
with a recording. `HFT-EXP-0005` sets a Directional Change sleeve's day beside walks of the same volatility,
and said that the command which hands the helper a recording and the shadow runner was not built. A sleeve may
not import what runs it ([ADR 0014](0014-sleeves-as-pure-functions.md)), so neither could live with the
sleeves.

## Decision

### Sleeve state

1. **A sleeve may say how its state is written down** (`hftlab.strategy.Saves`):

   ```
   save(state)     -> plain values a JSON line holds exactly: whole numbers, text, lists and mappings
   restore(saved)  -> the state `save` was given, or StateError
   ```

   - The two are exact inverses. Nothing is recomputed, rounded or filled in on the way back.
   - What is saved carries the sleeve's kind, a version and every setting the state was reached under. A
     state written under other settings is refused: another threshold, another seed, another model by its
     hash, other feature windows.
   - A saved state that is damaged is refused, or is exactly another state the sleeve could have written. It
     is never read as something near what was meant.
   - It is optional. The control, the ML and the Directional Change sleeve have it. The idle sleeve does not
     and needs none; a sleeve without it starts cold, as before.
   - The sleeve still touches no file. Whoever runs it decides where a state is kept and whether it is
     believed.

2. **The trader keeps the states in a hash-chained journal,** `sleeves.jsonl` in its state directory
   ([`hftlab.trader.resume`](../../src/hftlab/trader/resume.py)). One entry is written when a session ends on
   purpose: at the day's close and at a clean stop. It is written last, when every journal is closed and
   the tape has its last line, and it is tied to both:

   - where each journal the books and the stages are built from stood: the outbox, the risk, reconciliation,
     stage and shadow journals, and the day's decision journal;
   - the session's tape: its day, the name of its head, and where it ends. A tape is a head and a run of
     parts ([ADR 0027](0027-deploy-gate-and-reports.md), section 2), so its end is three things: how many
     parts it has, the bytes of the head and the parts together, and the SHA-256 of those bytes in order.
     The bytes are compared as they are once unpacked and are never read as anything.

3. **At a start the last entry is restored from only when nothing moved since.** All of these, or every
   sleeve starts cold:

   | Reason journalled | What did not hold |
   |---|---|
   | `NONE` | There is no entry: a first start |
   | `UNVERIFIED` | The file's chain does not verify |
   | `NOT_A_STATE` | The last whole line is not an entry of this kind and version |
   | `JOURNALS` | A journal does not stand where the entry left it: something was written since |
   | `TAPE` | The entry's tape is not the last tape there is, or does not end where the entry says |

   A session that was killed wrote no entry. The entry before it then fails on the journals and on the tape,
   so it is never read as current: stale is the ordinary case this rule is for.

   **The tape in parts.** This record first tied an entry to one file: its name, its length and the hash of
   its last line. A session's tape is several files now, the older ones may be compressed, and the last
   line is in the last part. `resume` does not work the file names out again: it asks the tape module two
   things, which head is the last one there is (`tape.last_head`) and where that session's tape ends
   (`tape.ending`), and compares the answer with the one the entry keeps. An entry of the first form is of
   another version and is `NOT_A_STATE`.

   | What happened to the tape since the entry | What the start does |
   |---|---|
   | Nothing | Restores |
   | The housekeeping job compressed closed parts, the last one among them (a session that said `stop` has no open part) | Restores: the bytes are compared unpacked |
   | A part is there both plain and compressed, or a half-written compressed copy lies beside it (the job was stopped) | Restores: the plain one is read, once |
   | A later session left a head, in this day's folder or a later one, with or without a turn | Cold, `TAPE` |
   | The tape has a part more than the entry counts: it went on after the entry. An empty one counts | Cold, `TAPE` |
   | A line was added to the last part, or the last part is cut short, lost a line, or is gone | Cold, `TAPE` |
   | An older part is missing (a hole), cannot be read, or does not unpack | Cold, `TAPE` |
   | A part or the head was written again, even to the same length and the same last line | Cold, `TAPE` |
   | The tapes are at their quota | No session is begun, so nothing is restored or journalled. The entry is left as it is, and is believed at the start after room was made if nothing above happened |
   | The session was ended by the quota | It ended as a stop does: its last word, then its entry. As the row above |
   | Room was made by moving the last session's own tape away | Cold, `TAPE`. Moving older sessions' tapes changes nothing |

   A state "saved before a part rolled" is the fourth and fifth rows. The entry is written after the tape's
   last word, so a session that ended on purpose cannot leave one; what looks like one is a tape something
   else wrote to, and it is not believed.

4. **Then each sleeve, on its own:**

   | Reason journalled | What did not hold |
   |---|---|
   | `IN_FLIGHT` | The sleeve had an order on its way when the entry was written, on either venue. It has no state in the entry |
   | `STAGE` | Its stage is not the one it ran under. A stage decides which book its positions are read from |
   | `SETTINGS` | The sleeve refused the state: other settings, another model, or not whole |
   | `ABSENT` | The entry holds nothing under its name |

5. **Cold means cold.** A sleeve that is not restored starts from its initial state, warm-up flags and all,
   and the trader closes what it holds, as before. Nothing of a refused state is used.

6. **A restored sleeve owns what it holds.** The trader does not close a restored sleeve's position as
   inherited. The sleeve remembers taking it and leaves it by its own rule.

7. **Every start says what it did.** One line of the decision journal names the sleeves restored and the
   reason for each one that was not, and any reason but `NONE` pages the owner.

8. **Nothing is repaired, and nothing depends on the file.** A file whose chain is broken is set aside under
   another name when the next entry is due, as it was found, and a new chain begins. A file that cannot be
   read does not stop a start, and an entry that cannot be written does not fail a clean stop: the owner is
   paged, and the next start is a cold one.

9. **A replay restores what the day restored.** The state file is copied by its mark like the other
   journals. A scratch folder has no tapes to look at, so the session writes whether the entry was believed
   into the head of its own tape, and a replay reads it there.

### The two commands

They live in a package of their own, [`hftlab.review`](../../src/hftlab/review/__init__.py): reading a run
back. It may import the shadow runner and the sleeves; nothing imports it. No import rule was loosened.

10. **Round trips are read out of a journal by one function**
    ([`trips`](../../src/hftlab/review/trips.py)), for both experiments. A round trip is two fills of one
    quantity that take a pair from flat and back. A position that came back to flat any other way is counted
    as irregular, one still open is reported, and neither is measured.

11. **`python -m hftlab.review.trip_costs --journal J --day D...`** (`HFT-EXP-0001`) reads the journal and the
    recorded days and nothing else. The cost model and the instruments are the ones the journal's first entry
    keeps.
    - For each round trip: `result`, `cost` (`CostModel.round_trip_cost_micro` at the two quotes the fills
      met, each widened as the shadow venue widened it) and `excess = result + cost`, per pair.
    - The quote a fill met is the last quote of its pair the runner had been shown by the venue's time of
      the fill. The fill's price is worked out again from it. A fill that was not made at it is `unmatched`:
      the recording is not the journal's, the trip is not measured, and the command exits 1.
    - It prints the count, the sums, the mean and the variance of the excess, exactly: integers in doubled
      millionths, fractions as a numerator and a denominator. The registered verdict of each pair (pass,
      fail, inconclusive) is decided on squares, so no root is taken.
    - `not_exact` counts the trips whose excess is not exactly their direction times the move of the mid
      times their size: the protocol's third check.

12. **`python -m hftlab.review.dc_null --config C --day D`** (`HFT-EXP-0005`) runs each Directional Change
    sleeve of the configuration alone on the day, and on N walks of it, each run a session of the shadow
    runner started flat. It prints the sleeve's result on the day, the N null results in the walks' order,
    and the share at or above the day's, as an exact fraction.
    - A result is the sum of the completed round trips' results. What is open at the end is reported and is
      in no result.
    - Only the path of the pair's mid is replaced. Heartbeats, connection changes and other pairs' quotes
      stay in their places, so a quiet stretch is as quiet on a walk as it was on the day.
    - N and the seed are the configuration's (`measure.null_walks`) unless both are given on the command
      line, and the report says which. The same day, configuration and seed print the same bytes.

13. **The two protocols are not edited.** An experiment on `main` is frozen (research/README.md, rule 4; the
    registry lint enforces it against the base branch), and each protocol's note that its tool is "not built"
    is part of what was registered. This record and the roadmap say that the tools exist now. The run of
    each experiment is recorded as a new experiment that names the old one, as both protocols already say.

## Consequences

**What holds, each with a test**

- A sleeve whose state went through a file's text into a new sleeve, at any event of a stream, makes every
  later decision the uncut sleeve makes and ends in the same state. Shown for the control sleeve at every
  event, and for the ML and the Directional Change sleeve at every few, with orders on their way at many of
  the cuts (`tests/strategy/test_saved_state.py`).
- A trader stopped and started again at six points of a session decides, for the Directional Change sleeve,
  exactly as a trader that was never stopped, with the limits in force and without; a trader that was killed
  does not, and closes what its sleeves held (`tests/trader/test_resume.py`).
- A stale entry, a broken chain, a tape that is not the last or ends otherwise, and a line written to any
  journal since: each starts every sleeve cold and is reported (`tests/trader/test_resume.py`).
- With the tape in parts: every row of the table in point 3, one test case each. A trader cut just before
  and just after its tape went on to a new part, with every part compressed between the stop and the start
  and without, decides exactly as the trader that was never stopped, and every session replays byte for
  byte. A session the quota ended is restored from once room is made (`tests/trader/test_resume.py`).
- A saved state damaged in any one place is refused or is exactly another state; one written under any
  other setting is refused (`tests/strategy/test_saved_state.py`).
- Every session of those tests replays byte for byte, the ones that restored and the one that found the
  file broken among them.
- On a book the test wrote itself, two round trips give the sums, the mean and the variance worked out by
  hand; a still book gives minus the modelled cost exactly and passes; trips that all lose the same move
  fail; a widened quote is costed as widened; a fill that does not meet the recording is found
  (`tests/review/test_trip_costs.py`).
- On a day that never moves every walk is the day and the sleeve stands level with all of them; on a planted
  zig-zag the trend sleeve earns the figure worked out by hand and no walk of nineteen does as well; each null
  result is the shadow runner's own on that walk; the registered configuration of `HFT-EXP-0005` is read as
  it stands (`tests/review/test_dc_null.py`).
- The package holds no venue, calls none, and only the null command runs a sleeve
  (`tests/review/test_structure.py`).

**What it costs, and what it does not cover**

- **A sleeve with an order on its way starts cold.** What became of that order is news the next session
  cannot give the sleeve the way the ended one would have. A sleeve that trades on nearly every turn is
  nearly always in that case, and gains nothing from this. The slow sleeves it is for seldom are.
- **A restored sleeve keeps its position through the rollover.** That was the point for a sleeve that means
  to hold; it also means the trader no longer closes it there. The risk gate, the switches and the stage
  rules still apply to it as to any running sleeve.
- **The time that passed is the sleeve's to notice.** A restart is not an event. The Directional Change
  sleeve treats a book unconfirmed for longer than its gap as a gap, and the features drop a stale history;
  the control sleeve holds for its time from the fill, however long the trader was away.
- **The control sleeve draws on the venue's sequence number,** which starts again with a connection. Its
  state is restored exactly; its entries after a reconnect are other entries than an unbroken connection
  would have given. That is the connection's doing and was so before.
- **A day's decision journal is compared on that day only.** Across the rollover the next day has another
  file. The journals that outlive a day each get a line from every session, and they are compared always.
- **The check is of what was written, not of who wrote it.** The chain shows a line was not changed. It
  does not show that the trader wrote it: the host's file permissions do that
  ([ADR 0007](0007-shared-host-isolation.md)).
- **The file grows by one entry a session** and is not rotated. An entry holds every feature history in
  full.
- **The backup holds the file and not the tapes.** The trader's folder is taken whole
  ([ADR 0027](0027-deploy-gate-and-reports.md), section 1), `sleeves.jsonl` and a chain that was set aside
  with it; the tapes are left out. So a state file brought back from a backup names a tape that is not
  there and is not believed: a lost disk is a cold start, by the rule and not by luck.
- **Where a tape ends costs a read of the whole of it.** Every byte of the session's tape is hashed, the
  compressed parts unpacked, once when a session ends on purpose and once at the next start: nothing less
  shows that an older part was not written again. On the fake broker's market that is nothing. On a real
  day it is as long as the day's tape is large, and it has not been measured. A clean stop that the service
  manager cuts off for taking too long (30 seconds as shipped) has written no entry, and the next start is
  a cold one: slower is never wrong.
- **The housekeeping job can pass while the tape is read.** A part it replaces with its compressed copy
  between the listing and the read is looked for once more. Anything else that goes wrong while reading is
  an end without a hash, which is never believed.
- **One reason is journalled for all of it.** `TAPE` does not say which row of the table it was; the files
  in the day's folder do.
- **The null is slow, and nobody knows how slow.** Each sleeve is run through the shadow runner once for
  the day and once for each walk, in one process, in exact arithmetic: six sleeves and 99 walks are 600 passes
  over a day's quotes. The time that takes on a real day has not been measured.
- **The null keeps the day's spreads, sizes and tick times.** It is a null for the path of the mid alone.
- **The comparison of round trips trusts the journal for its own cost model.** That is the point: the run is
  compared with the model it was made under. Whether that model is right is not something it can show.
- **The mean and the variance are of whatever round trips there were.** The registered thresholds of
  `HFT-EXP-0001` decide; the command adds no judgement of its own.
- **What the day-block bootstrap of `HFT-EXP-0005` needs is not here.** The command gives each day's result
  and null. The interval over five days is `hftlab.learn.validate`'s, and joining the two is left to the
  report of the run.
- **None of it has run on the host,** and neither command has read a recording of the real feed.
