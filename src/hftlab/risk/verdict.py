"""What the risk gate says about an order: allow, refuse, or allow only if it reduces the position.

A refusal always carries a code from `Reason`, a closed list. There is no free text in a verdict: a code can be
counted, compared between a live run and its replay, and published; a sentence cannot.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique


@unique
class Outcome(Enum):
    ALLOW = "ALLOW"  # nothing stands in the way
    EXITS_ONLY = "EXITS_ONLY"  # entries are stopped; an order that only reduces the sleeve's position may go
    REFUSE = "REFUSE"  # no order of this kind may go, whichever way it points


@unique
class Reason(Enum):
    # the order itself
    DUPLICATE_ORDER = "DUPLICATE_ORDER"  # the gate has already ruled on this client order id
    ORDER_ID = "ORDER_ID"  # the id is not this platform's, or names another sleeve than the order does
    # switches
    KILL_SWITCH = "KILL_SWITCH"
    FLATTEN_SWITCH = "FLATTEN_SWITCH"
    SWITCHES_UNREADABLE = "SWITCHES_UNREADABLE"
    # the record
    TAPE_SOFT_LIMIT = "TAPE_SOFT_LIMIT"  # the event tape is at its soft limit: nothing is opened on a filling record
    TAPE_HARD_LIMIT = "TAPE_HARD_LIMIT"  # it is at its hard limit: what is left of it is for closing what is held
    # limits
    PAIR_NOT_CONFIGURED = "PAIR_NOT_CONFIGURED"
    SLEEVE_NOT_CONFIGURED = "SLEEVE_NOT_CONFIGURED"
    ORDER_SIZE = "ORDER_SIZE"
    PRICE_COLLAR = "PRICE_COLLAR"
    POSITION_LIMIT = "POSITION_LIMIT"
    MAX_OPEN_PAIRS = "MAX_OPEN_PAIRS"
    ORDER_RATE_SLEEVE = "ORDER_RATE_SLEEVE"
    ORDER_RATE_DESK = "ORDER_RATE_DESK"
    ORDER_TO_TRADE_SLEEVE = "ORDER_TO_TRADE_SLEEVE"
    ORDER_TO_TRADE_DESK = "ORDER_TO_TRADE_DESK"
    # a cash account with a float (ADR 0031)
    PAIR_NOT_TRADABLE = "PAIR_NOT_TRADABLE"  # the account cannot hold one of the pair's currencies: shadow only
    ODD_LOT = "ODD_LOT"  # an entry smaller than the pair's minimum, which the venue prices worse
    CASH_RESERVE = "CASH_RESERVE"  # with every order on its way filled, it would take a currency under its reserve
    NOT_FUNDED = "NOT_FUNDED"  # no float stands: there is nothing a sleeve's position could be a deviation from
    FUNDING_NOT_OPEN = "FUNDING_NOT_OPEN"  # a conversion of the float, with no funding an operator asked for
    # market guards
    NO_QUOTE = "NO_QUOTE"
    STALE_QUOTE = "STALE_QUOTE"
    CROSSED_QUOTE = "CROSSED_QUOTE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    MARKET_SHUT = "MARKET_SHUT"
    MACRO_BLACKOUT = "MACRO_BLACKOUT"
    ROLLOVER_WINDOW = "ROLLOVER_WINDOW"
    WEEKLY_CLOSE = "WEEKLY_CLOSE"
    # loss control
    DAILY_LOSS_SLEEVE = "DAILY_LOSS_SLEEVE"
    DAILY_LOSS_DESK = "DAILY_LOSS_DESK"
    DRAWDOWN_SLEEVE = "DRAWDOWN_SLEEVE"
    DRAWDOWN_DESK = "DRAWDOWN_DESK"
    REJECT_STREAK = "REJECT_STREAK"
    UNLATCHED_CURRENCY = "UNLATCHED_CURRENCY"  # a loss in a currency the configuration sets no latch for
    UNMARKED_POSITION = "UNMARKED_POSITION"  # a position is open in a pair with no quote to value it at
    # connection and books
    VENUE_NOT_READY = "VENUE_NOT_READY"
    RECONCILIATION_BREAK = "RECONCILIATION_BREAK"
    ORDER_FAULT = "ORDER_FAULT"  # the order manager, or the gate's own book, saw something that cannot happen


@dataclass(frozen=True, slots=True)
class Verdict:
    """The gate's answer to one order.

    `outcome` is where the gate stands; `allowed` is whether this order may be sent. They differ only for
    EXITS_ONLY, where the answer depends on the order: allowed when it reduces the sleeve's position, refused
    when it does not. `reasons` lists every code that applied, the deciding one first; it is empty only for a
    plain ALLOW.
    """

    outcome: Outcome
    allowed: bool
    reasons: tuple[Reason, ...] = ()

    def __post_init__(self) -> None:
        plain = self.outcome is Outcome.ALLOW
        if plain != (not self.reasons) or (plain and not self.allowed):
            raise ValueError("a plain ALLOW has no reason and is allowed; every other verdict has a reason")
        if self.outcome is Outcome.REFUSE and self.allowed:
            raise ValueError("a refusal cannot allow the order")

    @property
    def reason(self) -> Reason | None:
        """The deciding code; None for a plain ALLOW."""
        return self.reasons[0] if self.reasons else None

    @property
    def code(self) -> str:
        """The deciding code as text, for a journal line: `ALLOW` when nothing applied."""
        return self.reasons[0].value if self.reasons else Outcome.ALLOW.value
