"""The table that sorts the broker's refusals into classes, and what it reads from the odd-lot warning."""
from __future__ import annotations

import pytest

from hftlab.venues.fake.market import ODD_LOT_TEXT, REJECTION_CODE, REJECTION_TEXTS, WARNING_CODE
from hftlab.venues.ibkr import refusals
from hftlab.venues.ibkr.refusals import odd_lot_minimum, reason_class


def test_the_classes_are_a_small_fixed_set_each_with_a_meaning_in_our_own_words():
    assert set(refusals.REASON_CLASSES) == {"insufficient_cash_quote_currency", "currency_not_permitted", "other"}
    assert all(meaning and meaning[0].islower() for meaning in refusals.REASON_CLASSES.values())
    assert (refusals.ORDER_REJECTED_CODE, refusals.ORDER_WARNING_CODE) == (201, 399) == (REJECTION_CODE, WARNING_CODE)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Order rejected - reason: THERE IS INSUFFICIENT CASH [-1.00 USD].", "insufficient_cash_quote_currency"),
        ("Order rejected - reason:Not allowed to open position in JPY.", "currency_not_permitted"),
        ("Order rejected - reason: something this table has never seen", "other"),
        ("", "other"),
        (None, "other"),
    ],
)  # fmt: skip
def test_a_rejection_is_classed_by_a_few_words_of_its_message_whatever_the_case(message, expected):
    assert reason_class(201, message) == expected
    assert reason_class(None, message) == expected, "with no number the words alone decide"


def test_the_words_under_another_number_are_not_a_known_refusal():
    assert reason_class(399, "insufficient cash") == "other"
    assert reason_class(202, "not allowed to open position in X") == "other"


@pytest.mark.parametrize("kind", sorted(REJECTION_TEXTS))
def test_each_refusal_the_fake_broker_can_play_is_classed_as_what_it_plays(kind):
    assert reason_class(REJECTION_CODE, REJECTION_TEXTS[kind].format(quote="USD")) == kind
    assert set(REJECTION_TEXTS) == set(refusals.REASON_CLASSES)


def test_the_odd_lot_warning_names_a_currency_and_a_size_and_nothing_else_does():
    assert odd_lot_minimum("size is below the EUR 20000 venue minimum and is routed as an odd lot") == ("EUR", 20_000)
    assert odd_lot_minimum(ODD_LOT_TEXT.format(base="AUD", minimum=25_000)) == ("AUD", 25_000)
    assert odd_lot_minimum("order rejected - reason: not allowed to open position in JPY") is None
    assert odd_lot_minimum("below the minimum") is None and odd_lot_minimum("") is None
