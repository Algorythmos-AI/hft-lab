"""Market data from the paper gateway, and only market data.

The recorder and the shadow runner get their live quotes from here. What they receive is a `MarketDataView`:
the adapter is built inside this function and never leaves it whole, so neither of them ever holds an object
that can place an order.
"""
from __future__ import annotations

from hftlab.core import Clock
from hftlab.venues.ibkr.adapter import IbkrSettings, IbkrVenue
from hftlab.venues.ibkr.client import IbAsyncClient
from hftlab.venues.supervisor import Backoff
from hftlab.venues.view import MarketDataView


def market_data(clock: Clock, *, host: str = "127.0.0.1", port: int = 4002, client_id: int = 79) -> MarketDataView:
    """A view of the paper gateway's quotes. Connecting still goes through the paper-only lock."""
    settings = IbkrSettings(host=host, port=port, client_id=client_id)
    return MarketDataView(IbkrVenue(clock=clock, client=IbAsyncClient(), settings=settings, backoff=Backoff()))
