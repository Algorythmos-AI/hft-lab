"""The paper-only lock as a pure function: every rule, every way round it that could be thought of."""
from __future__ import annotations

import dataclasses
import os
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.safety import PAPER_PORTS, ConnectionFacts, PaperOnlyError, assert_paper, paper_violations
from hftlab.safety.lock import LIVE_PORTS

ENV = {"MODE": "paper"}
GOOD = ConnectionFacts(port=4002, managed_accounts=("DU1234567",))
WIDE_ONE = "\N{FULLWIDTH DIGIT ONE}"


def found(environ=ENV, **changes) -> list[str]:
    return paper_violations(environ, dataclasses.replace(GOOD, **changes))


# ---- the paper case ------------------------------------------------------------------------------------------------


def test_a_paper_gateway_with_a_paper_account_passes():
    assert found() == []
    assert_paper(GOOD, ENV)


@pytest.mark.parametrize("port", [4002, 7497])
def test_both_paper_ports_pass(port):
    assert {4002, 7497} == PAPER_PORTS
    assert found(port=port) == []


@pytest.mark.parametrize("flag", ["", "0", "false", "FALSE", "False", "no", "off", " false ", "OFF"])
def test_an_explicit_false_live_flag_passes(flag):
    assert found({"MODE": "paper", "LIVE_TRADING_ENABLED": flag}) == []


def test_several_paper_accounts_and_a_distinct_username_pass():
    assert found(managed_accounts=("DU1", "DU22", "DU333"), username="paperlogin", live_username="otherlogin") == []
    assert found(managed_accounts=["DU1", "DU22"], username="paperlogin") == []


# ---- MODE ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode", [None, "", "live", "Paper", "PAPER", " paper", "paper ", "paper\n", "papertrade", "sim", "0"]
)
def test_mode_must_be_exactly_paper(mode):
    environ = {} if mode is None else {"MODE": mode}
    violations = found(environ)
    assert len(violations) == 1 and violations[0].startswith("MODE is ")


@given(st.text())
def test_any_mode_but_paper_is_a_violation(mode):
    assert (found({"MODE": mode}) == []) == (mode == "paper")


# ---- LIVE_TRADING_ENABLED ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag", ["1", "true", "TRUE", "True", "yes", "on", " true ", "enabled", "y", "t", "2", "maybe", "-", "null"]
)
def test_a_live_flag_that_is_not_an_explicit_false_refuses(flag):
    assert found({"MODE": "paper", "LIVE_TRADING_ENABLED": flag}) == [
        "LIVE_TRADING_ENABLED is set to something other than an explicit false"
    ]


@given(st.text())
def test_only_the_listed_false_values_pass_the_live_flag(flag):
    passes = found({"MODE": "paper", "LIVE_TRADING_ENABLED": flag}) == []
    assert passes == (flag.strip().lower() in {"", "0", "false", "no", "off"})


# ---- the gateway port ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("port", [4001, 7496])
def test_the_live_ports_are_refused_by_name(port):
    assert found(port=port) == [f"port {port} is a live trading port"]
    assert {4001, 7496} == LIVE_PORTS and not LIVE_PORTS & PAPER_PORTS


@pytest.mark.parametrize("port", [0, -1, 80, 4000, 4003, 7495, 7498, 14002, 40020, 2**31])
def test_any_other_port_is_refused(port):
    assert found(port=port) == [f"port {port} is not a paper port (4002, 7497)"]


@pytest.mark.parametrize("port", ["4002", 4002.0, True, None, b"4002", (4002,)])
def test_a_port_that_is_not_an_int_is_refused_even_if_it_equals_a_paper_port(port):
    assert found(port=port) == ["the gateway port is not an integer"]


@given(st.integers())
def test_only_the_two_paper_ports_ever_pass(port):
    assert (found(port=port) == []) == (port in (4002, 7497))


# ---- managed accounts ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("accounts", [(), []])
def test_no_managed_accounts_is_a_violation(accounts):
    assert found(managed_accounts=accounts) == ["the broker reported no managed accounts"]


def test_a_live_account_id_is_refused_with_both_messages():
    assert found(managed_accounts=("U1234567",)) == [
        "managed account 1 of 1 is a live account id",
        "managed account 1 of 1 is not a paper account id (DU and digits)",
    ]


def test_one_live_account_among_paper_accounts_refuses_the_connection():
    violations = found(managed_accounts=("DU1234567", "U7654321", "DU99"))
    assert violations == [
        "managed account 2 of 3 is a live account id",
        "managed account 2 of 3 is not a paper account id (DU and digits)",
    ]


@pytest.mark.parametrize(
    "account",
    ["du1234567", "DU", "DUX123", "DU12 ", " DU12", "DU12\n", "DU1.2", "DU-12", f"DU{WIDE_ONE}2", "DU12,DU34",
     "DU12,U34", "F1234567", "DF1234567", "I123", "", "PA123", "D U12", "UDU12", 12, None, b"DU12", ("DU12",)],
)  # fmt: skip
def test_anything_that_is_not_du_and_digits_is_not_a_paper_account(account):
    assert "managed account 1 of 1 is not a paper account id (DU and digits)" in found(managed_accounts=(account,))


@pytest.mark.parametrize("account", ["U1", " U1234567", "U1234567 ", "U1234567\n"])
def test_a_live_id_is_recognised_through_stray_white_space(account):
    assert "managed account 1 of 1 is a live account id" in found(managed_accounts=(account,))


@pytest.mark.parametrize("accounts", ["DU1234567", b"DU1234567", None, 5, {"DU1234567"}, {"DU1": 1}, iter(["DU1"])])
def test_managed_accounts_must_be_a_list_of_ids(accounts):
    assert found(managed_accounts=accounts) == ["the managed accounts are not a list of account ids"]


def test_a_message_never_echoes_an_account_id():
    violations = found(managed_accounts=("U7654321", "DX9988776"))
    assert violations and not any(re.search(r"7654321|9988776", v) for v in violations)


@given(st.lists(st.text(max_size=12), min_size=1, max_size=5))
def test_every_account_must_match_for_the_list_to_pass(accounts):
    passes = found(managed_accounts=tuple(accounts)) == []
    assert passes == all(re.fullmatch(r"DU[0-9]+", a, re.ASCII) for a in accounts)


@given(st.lists(st.integers(0, 10**9), min_size=1, max_size=5), st.integers(0, 4), st.integers(0, 10**9))
def test_a_single_live_id_anywhere_in_the_list_is_always_caught(numbers, position, live):
    accounts = [f"DU{n}" for n in numbers]
    accounts.insert(min(position, len(accounts)), f"U{live}")
    assert any("is a live account id" in v for v in found(managed_accounts=tuple(accounts)))


# ---- usernames -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("username", ["trader1", "TRADER1", " Trader1 ", "trader1\n"])
def test_the_declared_live_username_is_refused_whatever_its_case_or_spacing(username):
    violations = found(username=username, live_username="trader1")
    assert violations == ["the configured username is the declared live username"]


@pytest.mark.parametrize("username", [None, "", "   "])
def test_an_unknown_username_cannot_be_shown_to_differ_from_the_live_one(username):
    assert found(username=username, live_username="trader1") == [
        "a live username is declared but the configured username is unknown"
    ]


@pytest.mark.parametrize("declared", ["", "  "])
def test_an_empty_live_declaration_is_a_violation_not_a_pass(declared):
    assert found(username="paperlogin", live_username=declared) == ["a live username is declared but empty"]


def test_without_a_declared_live_username_the_rule_is_silent():
    assert found(username=None) == [] and found(username="anything") == []


# ---- the function as a whole ---------------------------------------------------------------------------------------


def test_every_violation_is_reported_at_once():
    facts = ConnectionFacts(port=4001, managed_accounts=("U1",), username="trader1", live_username="trader1")
    violations = paper_violations({"MODE": "live", "LIVE_TRADING_ENABLED": "true"}, facts)
    assert len(violations) == 6


def test_the_function_is_pure_it_reads_only_its_arguments(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    assert paper_violations({"MODE": "paper"}, GOOD) == []
    monkeypatch.setenv("MODE", "paper")
    monkeypatch.delenv("LIVE_TRADING_ENABLED")
    assert paper_violations({"MODE": "live"}, GOOD) != []
    before = dict(os.environ)
    paper_violations({}, GOOD)
    assert dict(os.environ) == before


def test_assert_paper_raises_with_every_reason():
    facts = ConnectionFacts(port=7496, managed_accounts=())
    with pytest.raises(PaperOnlyError) as caught:
        assert_paper(facts, {"MODE": "live"})
    message = str(caught.value)
    assert "MODE is 'live'" in message and "port 7496 is a live trading port" in message
    assert "no managed accounts" in message


def test_assert_paper_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("MODE", "paper")
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    assert_paper(GOOD)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    with pytest.raises(PaperOnlyError):
        assert_paper(GOOD)
    monkeypatch.delenv("LIVE_TRADING_ENABLED")
    monkeypatch.delenv("MODE")
    with pytest.raises(PaperOnlyError):
        assert_paper(GOOD)


def test_an_empty_mapping_passed_explicitly_is_not_replaced_by_the_process_environment(monkeypatch):
    monkeypatch.setenv("MODE", "paper")
    with pytest.raises(PaperOnlyError):
        assert_paper(GOOD, {})


def test_the_facts_cannot_be_changed_after_they_are_checked():
    with pytest.raises(dataclasses.FrozenInstanceError):
        GOOD.port = 4001
    assert isinstance(PaperOnlyError("x"), RuntimeError)


good_environments = st.fixed_dictionaries(
    {"MODE": st.just("paper")}, optional={"LIVE_TRADING_ENABLED": st.sampled_from(["", "0", "false", "no", "off"])}
)
good_facts = st.builds(
    ConnectionFacts,
    port=st.sampled_from([4002, 7497]),
    managed_accounts=st.lists(st.integers(0, 10**9).map(lambda n: f"DU{n}"), min_size=1, max_size=4).map(tuple),
)
breakages = st.sampled_from(
    [
        ("env", {"MODE": "live"}),
        ("env", {"LIVE_TRADING_ENABLED": "true"}),
        ("facts", {"port": 4001}),
        ("facts", {"port": 7496}),
        ("facts", {"managed_accounts": ()}),
        ("facts", {"managed_accounts": ("U1234567",)}),
        ("facts", {"username": "trader1", "live_username": "trader1"}),
    ]
)


@given(good_environments, good_facts, breakages)
def test_a_good_connection_passes_and_any_single_fault_breaks_it(environ, facts, breakage):
    assert paper_violations(environ, facts) == []
    where, change = breakage
    if where == "env":
        assert paper_violations({**environ, **change}, facts) != []
    else:
        assert paper_violations(environ, dataclasses.replace(facts, **change)) != []
