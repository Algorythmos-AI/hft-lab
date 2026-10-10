"""What keeps the platform on paper: the lock, its staged gate and the venue registry."""
from hftlab.safety.gate import PaperGate, Permit
from hftlab.safety.lock import (
    PAPER_PORTS,
    ConnectionFacts,
    PaperOnlyError,
    assert_paper,
    declared_usernames,
    paper_violations,
    preconnect_violations,
)
from hftlab.safety.venues import VENUES, UnknownVenueError, Venue, venue

__all__ = [
    "PAPER_PORTS",
    "VENUES",
    "ConnectionFacts",
    "PaperGate",
    "PaperOnlyError",
    "Permit",
    "UnknownVenueError",
    "Venue",
    "assert_paper",
    "declared_usernames",
    "paper_violations",
    "preconnect_violations",
    "venue",
]
