# ADR 0026: Sleeve state across a restart: an exact save and restore, believed only when nothing moved since

- **Status:** accepted, 2026-10-11. Built and tested on synthetic streams and against the fake broker only.
  Nothing here has run on the host, and no state has been saved from or restored on a real feed.
- **Context:** F5 and F6. [ADR 0019](0019-trader-loop-and-staged-rollout.md) left it open: "a sleeve that
  means to hold across the rollover needs its state kept".

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

## Decision

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
   - the session's tape: its day, its name, its length and the hash of its last line. The line is compared
     as bytes and never read as anything.

3. **At a start the last entry is restored from only when nothing moved since.** All of these, or every
   sleeve starts cold:

   | Reason journalled | What did not hold |
   |---|---|
   | `NONE` | There is no entry: a first start |
   | `UNVERIFIED` | The file's chain does not verify |
   | `NOT_A_STATE` | The last whole line is not an entry of this kind and version |
   | `JOURNALS` | A journal does not stand where the entry left it: something was written since |
   | `TAPE` | The entry's tape is not the last tape there is, or does not end as the entry says |

   A session that was killed wrote no entry. The entry before it then fails on the journals and on the tape,
   so it is never read as current: stale is the ordinary case this rule is for.

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

8. **Nothing is repaired.** A file whose chain is broken is set aside under another name when the next entry
   is due, as it was found, and a new chain begins.

9. **A replay restores what the day restored.** The state file is copied by its mark like the other
   journals. A scratch folder has no tapes to look at, so the session writes whether the entry was believed
   into the head of its own tape, and a replay reads it there.

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
- A saved state damaged in any one place is refused or is exactly another state; one written under any
  other setting is refused (`tests/strategy/test_saved_state.py`).
- Every session of those tests replays byte for byte, the ones that restored and the one that found the
  file broken among them.

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
- **The backup does not hold it,** like the other journals of the trader. A lost disk is a cold start.
- **None of it has run on the host.**
