"""The cash balance is read under the tag the paper gateway was measured to send, and under the documented one."""
from __future__ import annotations

import pytest
from ib_async import AccountValue

from hftlab.venues.ibkr import translate
from hftlab.venues.ibkr.translate import TranslationError
from hftlab.venues.port import VenueCash


def value(tag: str, amount: str, currency: str) -> AccountValue:
    return AccountValue("PAPER", tag, amount, currency, "")


def test_the_measured_tag_is_read_and_the_base_row_is_not_a_holding() -> None:
    values = [value("$LEDGER-CashBalance", "1000000.25", "AUD"), value("$LEDGER-CashBalance", "1000000.25", "BASE")]
    assert translate.cash_from(values) == (VenueCash("AUD", 1_000_000_250_000),)


def test_the_documented_tag_is_read_the_same_way() -> None:
    assert translate.cash_from([value("CashBalance", "12.5", "USD")]) == (VenueCash("USD", 12_500_000),)


@pytest.mark.parametrize("order", [0, 1])
def test_where_both_tags_come_the_ledger_row_is_kept(order: int) -> None:
    values = [value("CashBalance", "1", "EUR"), value("$LEDGER-CashBalance", "2", "EUR")]
    assert translate.cash_from(values[::-1] if order else values) == (VenueCash("EUR", 2_000_000),)


def test_other_ledger_rows_are_not_cash() -> None:
    values = [value("$LEDGER-TotalCashBalance", "5", "AUD"), value("$LEDGER-FxCashBalance", "5", "AUD")]
    assert translate.cash_from(values) == ()


def test_a_balance_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(TranslationError):
        translate.cash_from([value("$LEDGER-CashBalance", "n/a", "AUD")])
