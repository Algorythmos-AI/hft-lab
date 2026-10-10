"""Exact conversions: round trips hold for every value, nothing rounds, and a float never gets in."""
from __future__ import annotations

import decimal
import re
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.core import money

scales = st.integers(0, money.MAX_SCALE)
integers = st.integers(-(10**40), 10**40)
digits = st.text("0123456789", min_size=1, max_size=30)


@given(integers, scales)
def test_a_scaled_integer_survives_the_trip_through_text(value, scale):
    assert money.to_scaled(money.from_scaled(value, scale), scale) == value


@given(integers, scales)
def test_a_scaled_integer_survives_the_trip_through_decimal(value, scale):
    assert money.to_scaled(money.to_decimal(value, scale), scale) == value
    with decimal.localcontext() as context:
        context.prec = 200
        assert money.to_decimal(value, scale) * 10**scale == value


@given(integers, scales)
def test_the_text_form_is_canonical(value, scale):
    text = money.from_scaled(value, scale)
    fraction = rf"\.[0-9]{{{scale}}}" if scale else ""
    assert re.fullmatch(rf"-?(0|[1-9][0-9]*){fraction}", text)
    assert text.startswith("-") == (value < 0)


@given(st.booleans(), digits, st.data())
def test_any_decimal_text_the_scale_can_hold_converts_exactly(negative, whole, data):
    scale = data.draw(scales)
    fraction = data.draw(st.text("0123456789", max_size=scale))
    text = ("-" if negative else "") + whole + (f".{fraction}" if fraction else "")
    scaled = money.to_scaled(text, scale)
    assert money.to_decimal(scaled, scale) == Decimal(text)  # Decimal equality is exact
    assert money.to_scaled(Decimal(text), scale) == scaled


@given(digits, st.data())
def test_a_digit_the_scale_cannot_hold_is_an_error_never_a_rounding(whole, data):
    scale = data.draw(st.integers(0, money.MAX_SCALE - 1))
    kept = data.draw(st.text("0123456789", min_size=scale, max_size=scale))
    lost = data.draw(st.integers(1, 9))
    with pytest.raises(money.InexactError):
        money.to_scaled(f"{whole}.{kept}{lost}", scale)
    assert money.to_scaled(f"{whole}.{kept}0", scale) == money.to_scaled(f"{whole}.{kept}0000", scale)


@given(st.integers(-(10**18), 10**18), st.integers(-(10**18), 10**18), scales)
def test_scaling_keeps_order(a, b, scale):
    assert (a < b) == (money.to_scaled(a, scale) < money.to_scaled(b, scale))


def test_the_examples_in_the_design():
    assert money.to_scaled("1.08512", 5) == 108512
    assert money.to_scaled("151.234", 3) == 151234
    assert money.to_scaled("-0.5", 1) == -5
    assert money.to_scaled("+2", 2) == 200
    assert money.to_scaled(Decimal("1E+2"), 2) == 10000
    assert money.to_scaled(7, 3) == 7000
    assert money.from_scaled(108512, 5) == "1.08512"
    assert money.from_scaled(5, 5) == "0.00005"
    assert money.from_scaled(-5, 5) == "-0.00005"
    assert money.from_scaled(0, 3) == "0.000"
    assert money.from_scaled(42, 0) == "42"


@given(st.floats(), scales)
def test_no_float_is_ever_a_price(value, scale):
    with pytest.raises(TypeError):
        money.to_scaled(value, scale)
    with pytest.raises(TypeError):
        money.from_scaled(value, scale)
    with pytest.raises(TypeError):
        money.parse_quantity(value)
    with pytest.raises(TypeError):
        money.notional(value, 1)


@pytest.mark.parametrize("value", [True, False, None, b"1.5", 1.5, [1], Decimal])
def test_only_text_decimal_and_int_are_accepted(value):
    with pytest.raises(TypeError):
        money.to_scaled(value, 2)


FULLWIDTH_ONE = "\N{FULLWIDTH DIGIT ONE}"
ARABIC_ONE = "\N{ARABIC-INDIC DIGIT ONE}"


@pytest.mark.parametrize(
    "text",
    ["", " ", ".", "1.", ".5", "+", "-", "1e5", "1E5", "1.0e0", " 1.0", "1.0 ", "1.0\n", "1,000", "1_000", "NaN",
     "Infinity", "-inf", "0x10", "1.2.3", "--1", "1/2", FULLWIDTH_ONE, ARABIC_ONE, "1.0f"],
)  # fmt: skip
def test_text_that_is_not_a_plain_decimal_number_is_refused(text):
    with pytest.raises(ValueError):
        money.to_scaled(text, 5)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("sNaN")])
def test_a_decimal_that_is_not_finite_is_refused(value):
    with pytest.raises(ValueError):
        money.to_scaled(value, 5)


@pytest.mark.parametrize("scale", [-1, money.MAX_SCALE + 1, 100])
def test_a_scale_out_of_range_is_refused(scale):
    with pytest.raises(ValueError):
        money.to_scaled("1", scale)
    with pytest.raises(ValueError):
        money.from_scaled(1, scale)


@pytest.mark.parametrize("scale", [True, 2.0, "2", None])
def test_a_scale_that_is_not_an_int_is_refused(scale):
    with pytest.raises(TypeError):
        money.to_scaled("1", scale)


@given(st.integers(0, 10**18))
def test_a_quantity_round_trips_through_text(units):
    assert money.parse_quantity(str(units)) == units == money.parse_quantity(units)


@pytest.mark.parametrize(
    "value", ["25000.0", "2.5e4", "-1", "+5", "", " 1", "1 ", "1_000", "25,000", "1.5", FULLWIDTH_ONE]
)
def test_a_quantity_must_be_a_whole_number_of_units(value):
    with pytest.raises(ValueError):
        money.parse_quantity(value)


@pytest.mark.parametrize("value", [True, None, 25000.0, Decimal("25000"), b"25000"])
def test_a_quantity_must_be_text_or_int(value):
    with pytest.raises(TypeError):
        money.parse_quantity(value)


def test_a_negative_quantity_is_refused():
    with pytest.raises(ValueError):
        money.parse_quantity(-1)


@given(st.integers(-(10**30), 10**30), st.integers(-(10**30), 10**30))
def test_notional_is_the_exact_product(price, quantity):
    assert money.notional(price, quantity) == price * quantity


def test_notional_refuses_bool():
    with pytest.raises(TypeError):
        money.notional(True, 1)
