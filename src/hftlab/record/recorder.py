"""The recorder: every quote of the configured pairs, appended to the store. It does nothing else (ADR 0004).

One loop, on one thread: poll the source, write what arrived, write a heartbeat for each pair whose book did
not change, flush a block at least every `flush_ns`, and say it is alive in a status file. It is handed a
`QuoteSource`, which has no order methods, so it cannot trade; disconnects are the source's supervisor's job
and reach it only as connection events, which it records.

A quote gets its receive time when the loop next polls, so whatever the loop does between two polls (above
all the flush, which syncs to disk on this same thread) delays the receive times of what arrived meanwhile.
The loop measures that time and reports the longest and the recent one in the status file.

It stops by itself for two reasons, each with its own exit code and status: the quota (this platform's own
limit on the store) and a write the operating system refused (no space left, an I/O error).
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hftlab.core import Clock, Instrument, InstrumentKind, Quote
from hftlab.record import format as fmt
from hftlab.record.store import QuotaExceeded, Store, StoreIOError
from hftlab.venues.port import ConnectionEvent, ConnectionState, VenueError, VenueNotice, VenueUnavailable
from hftlab.venues.view import QuoteSource

MS = 1_000_000
_UP = (ConnectionState.RECONCILING, ConnectionState.READY)
EXIT_OK, EXIT_QUOTA, EXIT_IO = 0, 3, 4


@dataclass(frozen=True, slots=True)
class RecorderSettings:
    pairs: tuple[tuple[str, InstrumentKind], ...]
    poll_ns: int = 50 * MS
    flush_ns: int = 250 * MS  # a kill loses at most this much, or one block, whichever comes first
    heartbeat_ns: int = 1_000 * MS
    status_ns: int = 5_000 * MS


class Recorder:
    def __init__(
        self,
        source: QuoteSource,
        store: Store,
        clock: Clock,
        settings: RecorderSettings,
        *,
        status_path: Path | None = None,
        on_event: Callable[[fmt.RecordedEvent], None] | None = None,
    ) -> None:
        self._source, self._store, self._clock, self._settings = source, store, clock, settings
        self._status_path = status_path
        self._on_event = on_event
        self._subscribed: list[Instrument] = []
        self._last_quote: dict[str, int] = {}
        self._beat_at = self._status_at = 0
        self._buffered_since: int | None = None
        self._left_poll_at: int | None = None
        self._between_longest = self._between_recent = 0
        self._status_refused = False
        self.records = 0
        self.polls = 0

    # ---- life ---------------------------------------------------------------------------------------------------

    def start(self) -> None:
        self._store.open()
        now = self._clock.now_ns()
        self._beat_at = self._status_at = now
        self._write(fmt.START, now, 0, None, self._store.run)
        self._store.note({"type": "start", "run": self._store.run, "at_ns": now, "next_index": self._store.next_index})
        with contextlib.suppress(VenueUnavailable):  # the source's supervisor retries; the gap is journalled
            self._source.connect()
        self._subscribe()

    def stop(self, reason: str = "requested") -> None:
        """Write the STOP record and the journal's last line, close the store and let go of the source.

        Nothing here raises for the quota or for the disk: a store that can take no more is closed as it is.
        After a refused write the STOP record is not written at all, and the journal line still is, if it can be.
        """
        now = self._clock.now_ns()
        with contextlib.suppress(QuotaExceeded, StoreIOError):
            self._write(fmt.STOP, now, 0, None, self._store.run)
        with contextlib.suppress(StoreIOError):
            self._store.note({"type": "stop", "reason": reason, "at_ns": now})
        with contextlib.suppress(QuotaExceeded, StoreIOError):
            self._store.close()
        self._source.disconnect()
        self._status("stopped")

    def run(self, should_stop: Callable[[], bool]) -> int:
        """Record until asked to stop. Returns the exit code.

        0 it was asked to stop; 3 the quota stopped the recording; 4 the operating system refused a write. The
        last two leave their own status ("quota", "io-error") and their own line in the journal, when the disk
        still takes one. Anything else that goes wrong is not caught here: it is not a full store.
        """
        code, reason, started = EXIT_OK, "requested", False
        try:
            self.start()
            started = True
            while not should_stop():
                self.step()
        except QuotaExceeded as exc:
            code, reason = EXIT_QUOTA, "quota"
            print(f"RECORDER STOPPED: QUOTA REACHED. {exc}", file=sys.stderr, flush=True)
            self._note({"type": "quota", "at_ns": self._clock.now_ns(), "used_bytes": self._store.used_bytes})
        except StoreIOError as exc:
            code, reason = EXIT_IO, "io-error"
            print(f"RECORDER STOPPED: I/O ERROR. The system refused a write: {exc}", file=sys.stderr, flush=True)
            self._note({"type": "io-error", "at_ns": self._clock.now_ns(), "error": exc.code})
        finally:
            if started or code != EXIT_OK:
                self.stop(reason)
            if code != EXIT_OK:
                self._status(reason)
        return code

    def _note(self, entry: dict[str, object]) -> None:
        """Journal why the recording stopped, if the disk still takes a line."""
        with contextlib.suppress(StoreIOError):
            self._store.note(entry)

    # ---- one turn of the loop -----------------------------------------------------------------------------------

    def step(self) -> None:
        if self._left_poll_at is not None:
            between = self._clock.now_ns() - self._left_poll_at
            self._between_longest = max(self._between_longest, between)
            self._between_recent = max(self._between_recent, between)
        events = self._source.poll(self._settings.poll_ns)
        self._left_poll_at = self._clock.now_ns()
        self.polls += 1
        for event in events:
            if isinstance(event, Quote):
                self._last_quote[event.pair] = event.recv_ts_ns
                book = (event.bid, event.ask, event.bid_size, event.ask_size)
                self._write(fmt.QUOTE, event.recv_ts_ns, event.seq, event.pair, *book)
            elif isinstance(event, ConnectionEvent):
                self._write(fmt.CONNECTION, event.ts_ns, event.seq, None, fmt.STATE_CODES[event.state])
                if event.state is ConnectionState.RECONCILING:
                    self._subscribe()
            elif isinstance(event, VenueNotice):
                self._store.note({"type": "notice", "code": event.code, "message": event.message, "at_ns": event.ts_ns})
        now = self._clock.now_ns()
        if now - self._beat_at >= self._settings.heartbeat_ns:
            self._heartbeats(now)
        if self._buffered_since is not None and now - self._buffered_since >= self._settings.flush_ns:
            self._flush()
        if now - self._status_at >= self._settings.status_ns:
            self._status("recording")

    def _subscribe(self) -> None:
        """Look the pairs up and subscribe, once there is a connection. The source re-subscribes after a drop."""
        if self._subscribed or self._source.state not in _UP:
            return
        try:
            instruments = [self._source.instrument(pair, kind) for pair, kind in self._settings.pairs]
            for instrument in instruments:
                self._source.subscribe_quotes(instrument)
        except VenueUnavailable:
            return
        self._subscribed = instruments
        self._store.describe({"type": "instruments", "instruments": [fmt.describe(i) for i in instruments]})

    def _heartbeats(self, now: int) -> None:
        """For each pair with no quote since the last beat, record that the venue answered and the book was still."""
        quiet = [pair for pair, _ in self._settings.pairs if self._last_quote.get(pair, -1) < self._beat_at]
        self._beat_at = now
        if not quiet or not self._subscribed or self._source.state not in _UP:
            return
        try:
            answered = self._source.server_time_ns()
        except VenueError:
            return  # no answer, no heartbeat: the silence that follows is a gap, and is journalled as one
        for pair in quiet:
            self._write(fmt.HEARTBEAT, now, 0, pair, answered)

    def _write(self, kind: int, at_ns: int, seq: int, pair: str | None, *values: int) -> None:
        record = self._store.write(kind, at_ns, seq, pair, *values)
        self.records += 1
        if self._buffered_since is None and self._store.buffered:
            self._buffered_since = self._clock.now_ns()
        elif not self._store.buffered:
            self._buffered_since = None
        if self._on_event is not None:
            self._on_event(fmt.to_event(record, self._store.pairs))

    def _flush(self) -> None:
        self._store.flush()
        self._buffered_since = None

    def _status(self, status: str) -> None:
        """Replace the status file in one step, so a reader never sees half of it.

        `loop` is the time between leaving one poll and entering the next: `longest_between_polls_ns` since
        this run started, `recent_between_polls_ns` since the status was last written.

        The status file is not the recording. When it cannot be written, that is said once and the recorder
        carries on: a status that has stopped moving is what the dead-man check is for.
        """
        self._status_at = self._clock.now_ns()
        if self._status_path is None:
            return
        body = {
            "status": status,
            "at_ns": self._status_at,
            "pid": os.getpid(),
            "venue": self._source.name,
            "connection": self._source.state.value,
            "run": self._store.run,
            "next_index": self._store.next_index,
            "records_this_run": self.records,
            "last_record_ns": self._store.last_ts_ns,
            "used_bytes": self._store.used_bytes,
            "quota_bytes": self._store.settings.quota_bytes,
            "silent": self._store.silent(self._status_at),
            "loop": {
                "polls": self.polls,
                "longest_between_polls_ns": self._between_longest,
                "recent_between_polls_ns": self._between_recent,
            },
        }
        partial = self._status_path.with_name(self._status_path.name + ".partial")
        try:
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            partial.write_text(json.dumps(body, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(partial, self._status_path)
        except OSError as exc:
            if not self._status_refused:
                why = type(exc).__name__
                print(f"record: the status file could not be written ({why})", file=sys.stderr, flush=True)
            self._status_refused = True
            with contextlib.suppress(OSError):
                partial.unlink()
            return
        self._status_refused = False
        self._between_recent = 0
