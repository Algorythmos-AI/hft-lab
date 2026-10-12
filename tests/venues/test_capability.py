"""The capability file, read for the trader: which pairs the account cannot be shown to open, long and short."""
from __future__ import annotations

import pytest
import yaml

from hftlab.core import InstrumentKind
from hftlab.venues import capability
from hftlab.venues.capability import CapabilityError, cannot_open, load, path_for, said
from tests.support import ROOT

CASH, CFD = InstrumentKind.CASH, InstrumentKind.CFD
SHIPPED = ROOT / "config" / "venues" / "ibkr.yaml"


def a_file(**opening) -> dict:
    block = {"buy": None, "buy_refusal": None, "sell": None, "sell_refusal": None, **opening}
    return {"probe_record": "HFT-EXP-9999", "pairs": [{"pair": "EUR.USD", "spot_position_opening": block}]}


def test_the_shipped_file_shows_no_pair_the_account_can_open_and_says_why_for_each():
    shipped = load(SHIPPED)
    names = [entry["pair"] for entry in shipped["pairs"]]
    blocked = cannot_open(shipped, [(name, CASH) for name in names])
    assert sorted(blocked) == sorted(names) == ["AUD.USD", "EUR.USD", "GBP.USD", "USD.JPY"]
    cash = "buy refused (insufficient_cash_quote_currency), sell not tested, by HFT-EXP-0008"
    assert blocked["EUR.USD"] == blocked["AUD.USD"] == cash
    assert blocked["USD.JPY"] == "buy refused (currency_not_permitted), sell not tested, by HFT-EXP-0008"
    assert blocked["GBP.USD"] == "buy refused, sell not tested, by HFT-EXP-0008"
    as_cfd = cannot_open(shipped, [(name, CFD) for name in names])
    assert set(as_cfd) == set(names) and all("nothing is measured" in why and "CFD" in why for why in as_cfd.values())


def test_a_pair_is_open_only_when_both_sides_were_accepted():
    both = a_file(buy="accepted", sell="accepted")
    assert cannot_open(both, [("EUR.USD", CASH)]) == {}
    assert cannot_open(both, [("EUR.USD", CFD)]) != {}, "what spot showed says nothing of the other form"
    assert cannot_open(a_file(buy="accepted"), [("EUR.USD", CASH)]) == {"EUR.USD": "sell not tested, by HFT-EXP-9999"}
    one = a_file(buy="accepted", sell="refused", sell_refusal="other")
    assert cannot_open(one, [("EUR.USD", CASH)]) == {"EUR.USD": "sell refused (other), by HFT-EXP-9999"}
    odd = a_file(buy="maybe", sell="accepted")
    assert cannot_open(odd, [("EUR.USD", CASH)]) == {"EUR.USD": "buy maybe, by HFT-EXP-9999"}, "only `accepted` opens"


def test_a_pair_the_file_does_not_describe_or_says_nothing_about_is_not_open():
    assert "does not describe the pair" in cannot_open(a_file(), [("NZD.USD", CASH)])["NZD.USD"]
    bare = {"pairs": [{"pair": "EUR.USD"}]}
    assert cannot_open(bare, [("EUR.USD", CASH)]) == {"EUR.USD": "nothing is measured about opening a spot position"}
    assert cannot_open(a_file(), []) == {}


def test_the_reasons_are_said_in_one_line_pair_by_pair():
    assert said({"USD.JPY": "b", "EUR.USD": "a"}) == "EUR.USD: a; USD.JPY: b" and said({}) == ""


def test_a_file_that_is_missing_or_is_no_capability_file_is_an_error_not_an_open_account(tmp_path):
    with pytest.raises(CapabilityError, match="cannot read"):
        load(tmp_path / "none.yaml")
    for text in ("[1, 2]", "venue: x", "pairs: 3", "{"):
        (tmp_path / "bad.yaml").write_text(text)
        with pytest.raises(CapabilityError):
            load(tmp_path / "bad.yaml")


def test_a_venues_file_is_found_by_its_name_and_the_shipped_one_is_there():
    assert path_for("ibkr_paper") == capability.FOLDER / "ibkr.yaml" and (ROOT / path_for("ibkr_paper")) == SHIPPED
    assert yaml.safe_load(SHIPPED.read_text())["venue"] == "ibkr_paper"
