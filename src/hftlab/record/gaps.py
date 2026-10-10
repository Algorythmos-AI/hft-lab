"""The gap journal: every silence in the recording, and why.

A pair has data when it has a quote or a heartbeat. A silence longer than the threshold is a gap, and each gap
gets one of four reasons, tried in this order:

  market_closed    the market was open for no more than the threshold during it (a weekend, a holiday)
  process_down     the recorder was started again during it
  connection_lost  the connection was down at some point during it
  unexplained      none of the above. This is the number the F2 exit gate counts.

The tracker is a pure function of the records in order, so the recorder that writes the journal and the check
that re-reads a day arrive at the same gaps from the same records.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from hftlab.calendar import Calendar
from hftlab.record.format import CONNECTION, HEARTBEAT, QUOTE, START, STATES, Record
from hftlab.venues.port import ConnectionState

MARKET_CLOSED: Final = "market_closed"
PROCESS_DOWN: Final = "process_down"
CONNECTION_LOST: Final = "connection_lost"
UNEXPLAINED: Final = "unexplained"
_UP: Final = (ConnectionState.RECONCILING, ConnectionState.READY)


@dataclass(frozen=True, slots=True)
class Gap:
    pair: str
    start_ns: int
    end_ns: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        span = {"start_ns": self.start_ns, "end_ns": self.end_ns}
        return {"type": "gap", "pair": self.pair, **span, "reason": self.reason}


class GapTracker:
    def __init__(
        self,
        pairs: Iterable[str],
        calendar: Calendar,
        threshold_ns: int,
        *,
        last_data_ns: Mapping[str, int] | None = None,
        connected: bool = False,
    ) -> None:
        self._pairs = tuple(pairs)
        self._calendar = calendar
        self._threshold = threshold_ns
        self._last = {pair: (last_data_ns or {}).get(pair) for pair in self._pairs}
        self._connected = connected
        self._lost = {pair: not connected for pair in self._pairs}
        self._down = dict.fromkeys(self._pairs, False)

    def carry(self) -> dict[str, object]:
        """What a later tracker needs to continue from here: written at the top of each day's journal."""
        return {
            "type": "carry",
            "last_data_ns": {pair: at for pair, at in self._last.items() if at is not None},
            "connected": self._connected,
            "lost": sorted(pair for pair, lost in self._lost.items() if lost),
            "down": sorted(pair for pair, down in self._down.items() if down),
        }

    @classmethod
    def resume(
        cls, pairs: Iterable[str], calendar: Calendar, threshold_ns: int, carry: Mapping[str, object]
    ) -> GapTracker:
        last = carry.get("last_data_ns")
        tracker = cls(
            pairs, calendar, threshold_ns,
            last_data_ns=last if isinstance(last, dict) else None, connected=bool(carry.get("connected")),
        )  # fmt: skip
        for name, flags in (("lost", tracker._lost), ("down", tracker._down)):
            listed = carry.get(name)
            for pair in tracker._pairs:
                flags[pair] = isinstance(listed, list) and pair in listed
        return tracker

    def observe(self, record: Record, pairs: tuple[str, ...]) -> list[Gap]:
        """Take the next record; return the gaps it closes (a quote or heartbeat after a long silence)."""
        if record.kind == START:
            self._connected = False
            for pair in self._pairs:
                self._down[pair] = self._lost[pair] = True
        elif record.kind == CONNECTION:
            self._connected = STATES.get(record.a) in _UP
            if not self._connected:
                for pair in self._pairs:
                    self._lost[pair] = True
        elif record.kind in (QUOTE, HEARTBEAT):
            pair = pairs[record.pair]
            if pair in self._last:
                return self._data(pair, record.recv_ts_ns)
        return []

    def _data(self, pair: str, at_ns: int) -> list[Gap]:
        before = self._last[pair]
        out = []
        if before is not None and at_ns - before > self._threshold:
            out.append(Gap(pair, before, at_ns, self._reason(pair, before, at_ns)))
        self._last[pair] = at_ns
        self._down[pair] = False
        self._lost[pair] = not self._connected
        return out

    def _reason(self, pair: str, start_ns: int, end_ns: int) -> str:
        if self._calendar.open_ns_between(start_ns, end_ns) <= self._threshold:
            return MARKET_CLOSED
        if self._down[pair]:
            return PROCESS_DOWN
        if self._lost[pair]:
            return CONNECTION_LOST
        return UNEXPLAINED

    def silent(self, now_ns: int) -> list[Gap]:
        """The silences still open at `now_ns`: gaps that no data has closed yet."""
        return [
            Gap(pair, before, now_ns, self._reason(pair, before, now_ns))
            for pair, before in self._last.items()
            if before is not None and now_ns - before > self._threshold
        ]
