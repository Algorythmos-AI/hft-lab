"""Execution: the order manager, its state machine and its write-ahead outbox (ADR 0016).

Nothing outside this package calls `place` or `cancel` on a venue the platform trades on, and a scan of the
package holds that line (tests/venues/test_guard.py).
"""
from hftlab.exec.manager import Cause, Held, OrderManager, Proposal
from hftlab.exec.outbox import Outbox, OutboxError
from hftlab.exec.states import IN_FLIGHT, MOVES, TERMINAL, Fault, Stage, Tracked

__all__ = [
    "IN_FLIGHT",
    "MOVES",
    "TERMINAL",
    "Cause",
    "Fault",
    "Held",
    "OrderManager",
    "Outbox",
    "OutboxError",
    "Proposal",
    "Stage",
    "Tracked",
]
