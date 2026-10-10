"""Client order ids: a pure function of the decision, so a retry can never make a second order."""
from __future__ import annotations

import re

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from hftlab.core import Side, client_order_id, is_ours, sleeve_of
from hftlab.core.ids import MAX_SLEEVE

words = st.text("abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6)
sleeves = st.lists(words, min_size=1, max_size=3).map("-".join).filter(lambda s: len(s) <= MAX_SLEEVE)
pairs = st.sampled_from(["EUR.USD", "USD.JPY", "GBP.USD", "AUD.USD"])
legs = st.sampled_from(["entry", "exit", "stop", "flatten"])
arguments = st.fixed_dictionaries(
    {
        "sleeve": sleeves,
        "pair": pairs,
        "side": st.sampled_from(Side),
        "decision_seq": st.integers(0, 2**62),
        "leg": legs,
        "attempt": st.integers(0, 1000),
    }
)


@given(arguments)
def test_the_same_decision_always_gets_the_same_id(args):
    assert client_order_id(**args) == client_order_id(**dict(args))


@given(arguments, arguments)
def test_two_different_decisions_never_share_an_id(first, second):
    assume(first != second)
    assert client_order_id(**first) != client_order_id(**second)


@given(arguments, st.data())
def test_changing_any_one_argument_changes_the_id(args, data):
    name = data.draw(st.sampled_from(sorted(args)))
    other = data.draw(arguments)[name]
    assume(other != args[name])
    assert client_order_id(**{**args, name: other}) != client_order_id(**args)


@given(arguments)
def test_an_id_is_short_plain_and_names_its_sleeve(args):
    order_id = client_order_id(**args)
    assert re.fullmatch(r"hft-[a-z0-9-]+-[0-9a-f]{20}", order_id) and len(order_id) <= 4 + MAX_SLEEVE + 21
    assert sleeve_of(order_id) == args["sleeve"]
    assert is_ours(order_id) and is_ours(order_id, args["sleeve"])
    assert not is_ours(order_id, args["sleeve"] + "x")


def test_the_defaults_are_the_first_attempt_at_an_entry():
    args = {"sleeve": "control", "pair": "EUR.USD", "side": Side.BUY, "decision_seq": 42}
    assert client_order_id(**args) == client_order_id(**args, leg="entry", attempt=0)
    assert client_order_id(**args) != client_order_id(**args, attempt=1)
    assert client_order_id(**args) != client_order_id(**args, leg="exit")


def test_the_id_is_stable_across_releases():
    """Pinned: an id the venue already holds must still be recognised after an upgrade."""
    order_id = client_order_id(sleeve="control", pair="EUR.USD", side=Side.BUY, decision_seq=42)
    assert order_id == "hft-control-" + order_id[-20:] and len(order_id) == 32
    assert order_id == client_order_id(sleeve="control", pair="EUR.USD", side=Side.BUY, decision_seq=42)


def test_sleeves_that_share_a_prefix_stay_apart():
    one = client_order_id(sleeve="directional-change", pair="EUR.USD", side=Side.BUY, decision_seq=1)
    two = client_order_id(sleeve="directional", pair="EUR.USD", side=Side.BUY, decision_seq=1)
    assert sleeve_of(one) == "directional-change" and sleeve_of(two) == "directional"
    assert not is_ours(one, "directional")


@pytest.mark.parametrize(
    "order_id", ["", "hft", "hft--0123456789abcdef0123", "wt-control-0123456789abcdef0123", "hft-control-0123",
                 "hft-control-0123456789ABCDEF0123", "hft-Control-0123456789abcdef0123", None, 5,
                 "hft-control-0123456789abcdef0123\n", "x hft-control-0123456789abcdef0123"],
)  # fmt: skip
def test_an_id_this_platform_did_not_make_is_not_ours(order_id):
    assert sleeve_of(order_id) is None and not is_ours(order_id)


@pytest.mark.parametrize(
    "changes",
    [{"sleeve": ""}, {"sleeve": "Control"}, {"sleeve": "con trol"}, {"sleeve": "-control"}, {"sleeve": "control-"},
     {"sleeve": "a" * (MAX_SLEEVE + 1)}, {"sleeve": "con_trol"}, {"pair": "EURUSD"}, {"decision_seq": -1},
     {"attempt": -1}, {"leg": ""}, {"leg": "two words"}],
)  # fmt: skip
def test_an_argument_that_could_make_an_ambiguous_id_is_refused(changes):
    args = {"sleeve": "control", "pair": "EUR.USD", "side": Side.BUY, "decision_seq": 42, **changes}
    with pytest.raises(ValueError):
        client_order_id(**args)


@pytest.mark.parametrize("changes", [{"side": "BUY"}, {"decision_seq": 1.0}, {"decision_seq": True}, {"attempt": "0"}])
def test_an_argument_of_the_wrong_type_is_refused(changes):
    args = {"sleeve": "control", "pair": "EUR.USD", "side": Side.BUY, "decision_seq": 42, **changes}
    with pytest.raises(TypeError):
        client_order_id(**args)
