"""The closed vocabularies of the order path. Each is exhaustive: a new member is a reviewed change."""
from __future__ import annotations

from enum import Enum, unique


@unique
class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for a buy, -1 for a sell: the direction a fill moves the base-currency position."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


@unique
class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@unique
class TimeInForce(Enum):
    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


@unique
class OrderState(Enum):
    NEW = "NEW"  # created here, not sent
    SUBMITTED = "SUBMITTED"  # sent, not acknowledged
    WORKING = "WORKING"  # acknowledged by the venue
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        """No further event may change the order."""
        return self in _TERMINAL


_TERMINAL = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED})


@unique
class Liquidity(Enum):
    MAKER = "MAKER"
    TAKER = "TAKER"
    UNKNOWN = "UNKNOWN"


@unique
class InstrumentKind(Enum):
    CASH = "CASH"  # spot forex
    CFD = "CFD"  # forex contract for difference
