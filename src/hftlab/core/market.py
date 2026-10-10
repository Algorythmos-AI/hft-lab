"""Market data as it arrives."""
from __future__ import annotations

from dataclasses import dataclass

from hftlab.core import _checks


@dataclass(frozen=True, slots=True)
class Quote:
    """The top of the book for one pair, as received.

    Prices are integers at the pair's scale; sizes are base-currency units. `recv_ts_ns` is when this process
    received the update, in integer UTC nanoseconds from the injected clock (ADR 0002). `seq` is the recorder's
    own counter, so a replay orders events exactly as they were seen.

    A crossed or locked quote is kept as it came: the type records what the venue sent, and the market guards
    decide what to do with it.
    """

    pair: str
    bid: int
    ask: int
    bid_size: int
    ask_size: int
    recv_ts_ns: int
    seq: int

    def __post_init__(self) -> None:
        _checks.pair(self.pair)
        _checks.positive("bid", self.bid)
        _checks.positive("ask", self.ask)
        _checks.non_negative("bid_size", self.bid_size)
        _checks.non_negative("ask_size", self.ask_size)
        _checks.non_negative("recv_ts_ns", self.recv_ts_ns)
        _checks.non_negative("seq", self.seq)

    @property
    def spread(self) -> int:
        """Ask minus bid, in price units. Negative when the quote is crossed."""
        return self.ask - self.bid

    @property
    def mid_x2(self) -> int:
        """Twice the mid price. The mid itself can fall between two price units, so it is never stored."""
        return self.bid + self.ask

    @property
    def is_crossed(self) -> bool:
        return self.bid > self.ask
