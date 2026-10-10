"""A venue with its order side taken away.

The recorder and the shadow runner need quotes and nothing else. They are handed a `MarketDataView`, which has
no `place`, no `cancel` and no `mark_reconciled`, so there is no method on the object they hold that could
send an order. The venue behind it is kept in a name-mangled slot; a scan of the package fails on any code
that spells the mangled name. And because nothing can call `mark_reconciled`, the venue underneath never
leaves RECONCILING, the state in which it refuses orders by itself.
"""
from __future__ import annotations

from typing import Protocol

from hftlab.core import Instrument, InstrumentKind
from hftlab.venues.port import ConnectionState, Venue, VenueEvent


class QuoteSource(Protocol):
    """What a consumer of market data may ask for. There is nothing here that trades."""

    @property
    def name(self) -> str: ...

    @property
    def state(self) -> ConnectionState: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def poll(self, timeout_ns: int = 0) -> list[VenueEvent]: ...

    def instrument(self, pair: str, kind: InstrumentKind) -> Instrument: ...

    def subscribe_quotes(self, instrument: Instrument) -> None: ...

    def unsubscribe_quotes(self, instrument: Instrument) -> None: ...

    def server_time_ns(self) -> int: ...


class MarketDataView:
    __slots__ = ("__venue",)

    def __init__(self, venue: Venue) -> None:
        self.__venue = venue

    @property
    def name(self) -> str:
        return self.__venue.name

    @property
    def state(self) -> ConnectionState:
        return self.__venue.state

    def connect(self) -> None:
        self.__venue.connect()

    def disconnect(self) -> None:
        self.__venue.disconnect()

    def poll(self, timeout_ns: int = 0) -> list[VenueEvent]:
        return self.__venue.poll(timeout_ns)

    def instrument(self, pair: str, kind: InstrumentKind) -> Instrument:
        return self.__venue.instrument(pair, kind)

    def subscribe_quotes(self, instrument: Instrument) -> None:
        self.__venue.subscribe_quotes(instrument)

    def unsubscribe_quotes(self, instrument: Instrument) -> None:
        self.__venue.unsubscribe_quotes(instrument)

    def server_time_ns(self) -> int:
        return self.__venue.server_time_ns()


def _is_a_source(view: MarketDataView) -> QuoteSource:
    return view  # checked by mypy
