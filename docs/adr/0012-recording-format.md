# ADR 0012: The recording format: fixed-width records in checksummed blocks

- **Status:** accepted, 2026-10-11
- **Context:** F2, recorder

## Context

The recorder ([ADR 0004](0004-recorder-separate-from-trader.md)) writes every quote update for days on end, on
a small host it shares ([ADR 0007](0007-shared-host-isolation.md)). What it writes is the input of every
replay ([ADR 0005](0005-deterministic-replay.md)) and the evidence behind every later result. A gap can never
be bought back. So the file has to survive three things, and say so when it did not:

- **A kill in the middle of a write.** The recorder is built to be killed and restarted.
- **Damage afterwards.** A flipped byte in a price is a wrong backtest that nobody notices.
- **A full disk.** On this host that takes the other desks down too.

A format that is finalised when the file is closed fails the first: after a kill, the file is unreadable or
its end is ambiguous. A text log passes it and fails the second.

## Decision

1. **A record is 64 bytes, fixed, little endian, integers only.**

   | Field | Bytes | Meaning |
   |---|---|---|
   | `index` | 8 | The recorder's own counter: one sequence across every pair and every restart, with no holes |
   | `recv_ts_ns` | 8 | The injected clock's time when this process received the event |
   | `venue_seq` | 8 | The venue's sequence number for it |
   | `kind` | 1 | Quote, heartbeat, connection change, start, stop |
   | `pair` | 2 | The pair's number in the segment header's table |
   | reserved | 5 | Zero |
   | `a` to `d` | 4 × 8 | A quote: bid, ask, bid size, ask size. A heartbeat: the venue's own clock. A connection change: the state. A start: the run number |

2. **Records are written in blocks, and a block is one append.** A block is a marker, a count and a CRC-32 over
   the count and the records. It is flushed when it holds a configured number of records or after a
   configured time, whichever comes first. That is the most a kill can lose.
3. **A segment is one UTC hour**, in a folder per UTC day, named by the receive time. Its header carries a
   marker, the format version, the run number, the opening time and the pair table, under its own checksum.
4. **A torn tail and corruption are different things.**
   - A last block that is short, or fails its checksum with nothing after it, is a write that was
     interrupted. A reader stops before it. On opening, the writer cuts the file back to the last whole block
     and continues the index from the last record that is really on disk, so a restart never duplicates and
     never reorders.
   - A block that fails its checksum with data after it is corruption. It is an error. Nothing reads past it
     and nothing repairs it.
5. **A quiet book is recorded as quiet.** When the venue answers and a pair's book has not changed, a
   heartbeat is written. "No change" and "no data" are then different records.
6. **Every silence has a reason, in a journal beside the segments.** A silence longer than a threshold is a
   gap: the market was closed, the recorder was restarted, the connection was down, or it is unexplained. The
   gap tracker is a pure function of the records in order, so the check that re-reads a day arrives at the
   same gaps and compares them with the journal.
7. **A day folder can be priced by itself.** The journal holds the instruments (price scale, increments,
   minimum size) and repeats them every hour.
8. **The store never grows past its quota.** A block that would not fit is not written; the recorder stops
   and says so with its own exit code.
9. **Nothing is deleted until it is somewhere else.** Compressing closed hours and handing old ones to an
   offload is a separate housekeeping command. A compressed copy is read back and compared before the
   original goes. A segment is removed here only after the offload has confirmed its size and SHA-256. With
   no offload configured, nothing is ever deleted.
10. **The format has a version**, and a reader refuses a version it does not know. Reading and writing need
    the standard library only, and no float.

## Consequences

- `python -m hftlab.replay --day DIR --check` can verify a day without trusting it: every checksum, no hole
  in the index, a venue sequence that never goes back within a run, and a gap journal that matches the
  records.
- A day's size is known in advance: 64 bytes for every update before compression. The quota is set from the
  host's measured disk budget, not from a guess; the value in `config/recorder.yaml` is still a placeholder.
- This is a log, not a query format. Analysis that wants columns converts a day first, and what it converts
  to never enters git ([ADR 0006](0006-public-repo-and-leak-mechanism.md)).
- A wall clock that was corrected backwards is recorded as it was. The check counts those steps, and a
  replay delivers the event at the time already reached instead of moving its clock back.
- The format has been exercised on the fake broker's quotes and on synthetic streams, including a kill at
  every byte of a block. It has not yet recorded the real feed: the F2 exit gate needs five recorded trading
  days and cannot be closed by code.

## Addendum (host layer): a write the operating system refuses

A review before the first install found three things the decision above did not say.

1. **A full disk is not the quota.** The quota is this platform's own limit and is reached on purpose. A write
   the operating system refuses (no space left, an I/O error) is something else. It used to leave the recorder
   as a crash, which a service manager answers with a restart loop. It now ends the store's writing for that
   process: `StoreIOError`, the status `io-error`, a line in the journal when the disk still takes one, and
   exit code 4. The unit's restart back-off does the rest.
2. **Nothing is appended after a write that failed.** The piece of a block that got out is taken back if the
   disk allows it, what was buffered is dropped, and no STOP record is written. A good block after a torn one
   would turn the torn one into corruption (decision 4), so the next writer of that file is a new process.
   The journal follows the same rule: a line is appended whole or taken back, a reader skips a last line
   that has no newline, and the next append cuts it off. A record whose gap could not be journalled does not
   reach the disk, so the day check still finds the journal and the records in agreement.
3. **A segment appears with its header whole or not at all.** The header is written beside the segment and
   moved into place in one step. A header cut short by a kill or a full disk would have been a file that
   nothing could read, and the store could not have been opened again.

The loop also reports how long it is away from the source between two polls, the longest and the recent one,
in the status file. The flush syncs to disk on the loop's own thread (decision 2), and what that costs in
receive time is now measured. The flush policy itself is unchanged.
