"""What the broker says when it turns an order down, sorted into a small fixed table in our own words.

The gateway acknowledges an order itself, at once, and the broker's servers judge it a moment later. A refusal
then arrives twice: as the order's last state, and as a message with a number. One number, 201, covers every
refusal seen so far, so the class is told from a few words of the message. The message itself is the broker's
free text and may name amounts: it stays in the notices of a report the owner keeps, and nothing that could
be published carries more than the number and the class below.

Seen on the real paper gateway on 2026-10-12: 201 with each of the two texts below, and 399, a
warning that an order under the venue's minimum for good pricing is routed as an odd lot.

No broker library is imported here: the codes arrive as notices (`ib:<code>`) through the venue port.
"""
from __future__ import annotations

import re
from typing import Final

INSUFFICIENT_CASH: Final = "insufficient_cash_quote_currency"
CURRENCY_NOT_PERMITTED: Final = "currency_not_permitted"
OTHER: Final = "other"

# class -> what it means, in our words
REASON_CLASSES: Final[dict[str, str]] = {
    INSUFFICIENT_CASH: "the account does not hold enough of the currency the order pays in, and may not borrow it",
    CURRENCY_NOT_PERMITTED: "the account may not hold a position in one of the pair's currencies at all",
    OTHER: "a refusal this table does not know; the broker's own words are in the report's notices",
}
ORDER_REJECTED_CODE: Final = 201
ORDER_WARNING_CODE: Final = 399  # a warning about an order that is still sent: not a refusal
# (the broker's number, a few words of its message in lower case, the class). The first match wins.
_TABLE: Final[tuple[tuple[int, str, str], ...]] = (
    (ORDER_REJECTED_CODE, "insufficient cash", INSUFFICIENT_CASH),
    (ORDER_REJECTED_CODE, "not allowed to open position in", CURRENCY_NOT_PERMITTED),
)
_ODD_LOT: Final = re.compile(r"below the ([A-Z]{3}) ([0-9]+)\b[^.]{0,40}?minimum")


def reason_class(code: int | None, message: str | None) -> str:
    """The class of a refusal with this number and these words. Without a number the words alone decide."""
    said = (message or "").casefold()
    for number, words, found in _TABLE:
        if (code is None or code == number) and words in said:
            return found
    return OTHER


def odd_lot_minimum(message: str) -> tuple[str, int] | None:
    """The currency and the size the broker's odd-lot warning names as the venue's minimum, or None."""
    found = _ODD_LOT.search(message)
    return (found.group(1), int(found.group(2))) if found else None
