"""The paper gateway as a venue that takes orders. Built for the trader, and for nothing else.

What comes back is the adapter itself, and the adapter is a `GuardedVenue`: connecting passes the paper-only
lock in its stages, and every order and cancel passes it again (ADR 0001, ADR 0009). The trader hands it to
the order manager, which is the only caller of its order methods (ADR 0016).
"""
from __future__ import annotations

from hftlab.core import Clock
from hftlab.venues.ibkr.adapter import IbkrSettings, IbkrVenue
from hftlab.venues.ibkr.client import IbAsyncClient
from hftlab.venues.supervisor import Backoff


def paper_venue(clock: Clock, *, host: str = "127.0.0.1", port: int = 4002, client_id: int = 77) -> IbkrVenue:
    """The paper gateway, not yet connected. The port is judged by the lock like any other."""
    settings = IbkrSettings(host=host, port=port, client_id=client_id)
    return IbkrVenue(clock=clock, client=IbAsyncClient(), settings=settings, backoff=Backoff())
