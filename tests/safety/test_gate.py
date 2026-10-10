"""The lock in three stages: a connection is unchecked until the broker's own account list has passed."""
from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.safety import PaperGate, PaperOnlyError, Permit, declared_usernames, preconnect_violations
from hftlab.safety import gate as gate_module

PAPER = ("DU1234567",)


def a_gate(environ=None, **changes) -> PaperGate:
    settings = {"port": 4002, "env": {"MODE": "paper"} if environ is None else environ, **changes}
    return PaperGate(**settings)


# ---- stage one: before a socket is opened ---------------------------------------------------------------------------


def test_stage_one_passes_on_a_paper_environment_and_port_without_any_account():
    assert preconnect_violations({"MODE": "paper"}, port=4002) == []
    a_gate().before_connect()


@pytest.mark.parametrize(
    ("environ", "changes", "needle"),
    [
        ({}, {}, "MODE is None"),
        ({"MODE": "live"}, {}, "MODE is 'live'"),
        ({"MODE": "paper", "LIVE_TRADING_ENABLED": "true"}, {}, "LIVE_TRADING_ENABLED"),
        ({"MODE": "paper"}, {"port": 4001}, "port 4001 is a live trading port"),
        ({"MODE": "paper"}, {"port": 7496}, "port 7496 is a live trading port"),
        ({"MODE": "paper"}, {"port": 4003}, "not a paper port"),
        ({"MODE": "paper"}, {"username": "trader1", "live_username": "trader1"}, "is the declared live username"),
        ({"MODE": "paper"}, {"live_username": "trader1"}, "configured username is unknown"),
    ],
)
def test_stage_one_refuses_before_anything_is_connected(environ, changes, needle):
    gate = a_gate(environ, **changes)
    with pytest.raises(PaperOnlyError, match=needle):
        gate.before_connect()
    assert needle in gate.refusal


def test_stage_one_says_nothing_about_accounts():
    assert not any("account" in v for v in preconnect_violations({}, port=4001, username="a", live_username="a"))


# ---- stage two: the broker's own account list -----------------------------------------------------------------------


def test_stage_two_mints_a_permit_for_a_paper_account_list():
    gate = a_gate()
    gate.before_connect()
    permit = gate.after_handshake(PAPER)
    assert isinstance(permit, Permit) and gate.holds(permit) and gate.refusal is None


@pytest.mark.parametrize("accounts", [(), ("U1234567",), ("DU1", "U2"), ("DF123",), "DU1234567", None])
def test_stage_two_refuses_any_account_list_that_is_not_all_paper(accounts):
    gate = a_gate()
    gate.before_connect()
    with pytest.raises(PaperOnlyError):
        gate.after_handshake(accounts)
    assert gate.refusal and not gate.holds(None)


def test_stage_two_checks_the_environment_again():
    environ = {"MODE": "paper"}
    gate = a_gate(environ)
    gate.before_connect()
    environ["LIVE_TRADING_ENABLED"] = "true"
    with pytest.raises(PaperOnlyError, match="LIVE_TRADING_ENABLED"):
        gate.after_handshake(PAPER)


# ---- stage three: before every order --------------------------------------------------------------------------------


def test_stage_three_passes_with_the_current_permit():
    gate = a_gate()
    permit = gate.after_handshake(PAPER)
    gate.before_order(permit, PAPER)
    gate.before_order(permit, PAPER)


def test_no_order_without_a_permit():
    gate = a_gate()
    gate.before_connect()
    with pytest.raises(PaperOnlyError, match="has not passed the paper-only lock"):
        gate.before_order(None, PAPER)
    assert gate.refusal is None, "a missing permit is a refused order, not a verdict on the environment"


def test_a_permit_dies_with_its_connection():
    gate = a_gate()
    old = gate.after_handshake(PAPER)
    gate.connection_closed()
    assert not gate.holds(old)
    with pytest.raises(PaperOnlyError, match="has not passed"):
        gate.before_order(old, PAPER)


def test_a_new_connection_starts_unchecked_and_the_old_permit_does_not_carry_over():
    gate = a_gate()
    old = gate.after_handshake(PAPER)
    gate.connection_closed()
    gate.before_connect()  # the socket is open again, the handshake check has not run
    with pytest.raises(PaperOnlyError, match="has not passed"):
        gate.before_order(old, PAPER)
    new = gate.after_handshake(PAPER)
    assert new is not old and gate.holds(new) and not gate.holds(old)
    gate.before_order(new, PAPER)
    with pytest.raises(PaperOnlyError):
        gate.before_order(old, PAPER)


def test_a_reconnect_that_reports_a_live_account_is_refused_for_good():
    gate = a_gate()
    gate.after_handshake(PAPER)
    gate.connection_closed()
    gate.before_connect()
    with pytest.raises(PaperOnlyError, match="live account id"):
        gate.after_handshake(("U7654321",))
    for stage in (gate.before_connect, lambda: gate.after_handshake(PAPER)):
        with pytest.raises(PaperOnlyError, match="refused earlier and stays refused"):
            stage()


def test_stage_three_sees_an_environment_that_changed_after_the_connection_was_checked():
    environ = {"MODE": "paper"}
    gate = a_gate(environ)
    permit = gate.after_handshake(PAPER)
    environ["MODE"] = "live"
    with pytest.raises(PaperOnlyError, match="MODE is 'live'"):
        gate.before_order(permit, PAPER)
    environ["MODE"] = "paper"
    with pytest.raises(PaperOnlyError, match="stays refused"):
        gate.before_order(permit, PAPER)


def test_stage_three_sees_an_account_list_that_changed():
    gate = a_gate()
    permit = gate.after_handshake(PAPER)
    with pytest.raises(PaperOnlyError, match="live account id"):
        gate.before_order(permit, ("DU1234567", "U7654321"))


def test_a_permit_cannot_be_built_by_hand_or_borrowed_from_another_gate():
    gate, other = a_gate(), a_gate()
    with pytest.raises(PaperOnlyError, match="minted by the gate"):
        Permit(gate, 1, object())
    with pytest.raises(TypeError):
        Permit(gate, 1)
    borrowed = other.after_handshake(PAPER)
    gate.after_handshake(PAPER)
    with pytest.raises(PaperOnlyError, match="has not passed"):
        gate.before_order(borrowed, PAPER)
    assert not hasattr(borrowed, "__dict__") and "Permit(connection 1)" in repr(borrowed)


def test_a_failed_handshake_check_revokes_the_previous_permit_too():
    gate = a_gate()
    old = gate.after_handshake(PAPER)
    with pytest.raises(PaperOnlyError):
        gate.after_handshake(("U1",))
    assert not gate.holds(old)


def test_the_process_environment_is_read_afresh_at_every_stage(monkeypatch):
    monkeypatch.setenv("MODE", "paper")
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    gate = PaperGate(port=4002)
    gate.before_connect()
    permit = gate.after_handshake(PAPER)
    gate.before_order(permit, PAPER)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    with pytest.raises(PaperOnlyError):
        gate.before_order(permit, PAPER)


def test_the_declared_usernames_come_from_the_environment(monkeypatch):
    assert declared_usernames({}) == (None, None)
    declared = {"HFT_IBKR_USERNAME": "paperlogin", "HFT_IBKR_LIVE_USERNAME": ""}
    assert declared_usernames(declared) == ("paperlogin", None)
    monkeypatch.setenv("HFT_IBKR_USERNAME", "paperlogin")
    monkeypatch.setenv("HFT_IBKR_LIVE_USERNAME", "trader1")
    assert declared_usernames() == ("paperlogin", "trader1")


STEPS = ["connect", "handshake", "live handshake", "close", "order", "old order"]
steps = st.lists(st.sampled_from(STEPS), max_size=12)


@given(steps)
def test_an_order_passes_only_right_after_a_paper_handshake_on_the_open_connection(sequence):
    """Whatever the order of events, stage three passes exactly when a model of the rule says it should."""
    gate = a_gate()
    current, stale, checked, refused = None, None, False, False
    for step in sequence:
        try:
            if step == "connect":
                gate.before_connect()
            elif step == "handshake":
                stale, current = current or stale, gate.after_handshake(PAPER)
                checked = True
            elif step == "live handshake":
                gate.after_handshake(("U1",))
            elif step == "close":
                gate.connection_closed()
                stale, current, checked = current or stale, None, False
            elif step == "order":
                gate.before_order(current, PAPER)
                assert checked and not refused
            elif step == "old order":
                gate.before_order(stale, PAPER)
                raise AssertionError("an order passed on a permit of a closed or replaced connection")
        except PaperOnlyError:
            if step == "live handshake":
                refused, checked, current = True, False, None
            elif step == "order":
                assert refused or not checked
            elif step in ("connect", "handshake"):
                assert refused
    assert (gate.refusal is not None) == refused


def test_only_the_gate_mints_permits():
    source = Path(gate_module.__file__).read_text()
    assert source.count("Permit(self, self._epoch, _MINT)") == 1 and source.count("_MINT)") == 1
