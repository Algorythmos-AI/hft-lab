"""When to try again after a connection is lost."""
from __future__ import annotations

from dataclasses import dataclass

from hftlab.core.money import require_int

SECOND_NS = 1_000_000_000


@dataclass(frozen=True, slots=True)
class Backoff:
    """Delays between reconnection attempts: a first delay, multiplied each time, up to a cap.

    There is no jitter and no giving up. Jitter spreads a crowd of clients, and there is one; and a schedule
    that is a pure function of the attempt number can be replayed. A gateway that restarts every day comes back
    within a few attempts, and while it is away the platform is disconnected, which already means no orders.
    """

    first_ns: int = SECOND_NS
    factor: int = 2
    cap_ns: int = 60 * SECOND_NS

    def __post_init__(self) -> None:
        if require_int("first_ns", self.first_ns) <= 0 or require_int("cap_ns", self.cap_ns) < self.first_ns:
            raise ValueError("a backoff needs a positive first delay and a cap that is not below it")
        if require_int("factor", self.factor) < 1:
            raise ValueError("a backoff factor below 1 would retry faster and faster")

    def delay_ns(self, attempt: int) -> int:
        """The wait before attempt number `attempt` (1 is the first retry)."""
        if require_int("attempt", attempt) < 1:
            raise ValueError(f"attempts are numbered from 1, not {attempt}")
        delay = self.first_ns
        for _ in range(attempt - 1):
            delay *= self.factor
            if delay >= self.cap_ns:
                return self.cap_ns
        return min(delay, self.cap_ns)
