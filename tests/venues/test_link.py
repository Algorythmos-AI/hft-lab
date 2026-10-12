"""The table of the gateway's messages about its own link to the broker, and the state read from them."""
from __future__ import annotations

import pytest

from hftlab.venues.ibkr.link import (
    COMPETING_REASON,
    COMPETING_SESSION_CODE,
    DOWN_REASON,
    GATEWAY_LINK_CODES,
    UPSTREAM_DOWN_CODES,
    UpstreamLink,
    gateway_code,
    says_competing_session,
)


def heard(*messages: tuple[int, str]) -> UpstreamLink:
    link = UpstreamLink()
    for code, text in messages:
        link.hear(f"ib:{code}", text)
    return link


def test_the_codes_that_say_the_link_is_cut_are_named_in_one_table_each_with_a_meaning():
    assert set(UPSTREAM_DOWN_CODES) == {1100, 2110, 2103, 2105, 2157}
    assert {code for code, entry in GATEWAY_LINK_CODES.items() if not entry.down} == {1101, 1102, 2104, 2106, 2158}
    assert all(entry.meaning and entry.meaning[0].islower() for entry in GATEWAY_LINK_CODES.values())
    assert "connected to us" in DOWN_REASON and "broker's servers" in DOWN_REASON


def test_nothing_heard_is_a_link_that_is_not_known_to_be_down():
    assert heard().down is False and heard().codes == []


@pytest.mark.parametrize("code", sorted([1100, 2110, 2103, 2105, 2157]))
def test_each_cut_message_alone_puts_the_link_down(code):
    assert heard((code, "cut:somefarm")).down is True and heard((code, "cut:somefarm")).codes == [code]


@pytest.mark.parametrize(("down", "up"), [(2103, 2104), (2105, 2106), (2157, 2158)])
def test_a_service_is_back_when_the_same_service_says_so_and_not_when_another_does(down, up):
    assert heard((down, "cut:farm-a"), (up, "there:farm-a")).down is False
    assert heard((down, "cut:farm-a"), (up, "there:farm-b")).down is True
    assert heard((up, "there:farm-a"), (down, "cut:farm-a")).down is True, "the latest word counts"


@pytest.mark.parametrize("back", [1101, 1102])
def test_the_gateway_coming_back_clears_everything_said_before(back):
    assert heard((1100, "lost"), (2103, "cut:farm-a"), (back, "restored")).down is False


def test_a_service_that_reports_itself_connected_shows_the_servers_are_reachable():
    link = heard((2110, "link to the servers cut"), (2103, "cut:farm-a"), (2104, "there:farm-b"))
    assert link.down is True and link.codes == [2103], "the servers are back; one service is still cut"


def test_other_notices_change_nothing():
    link = heard((2110, "cut"))
    for code, text in (("ib:2119", "connecting:farm-a"), ("ib:2108", "idle:farm-a"), ("translation", "x"),
                       ("cancel-too-late", "y"), ("ib:", "z"), ("ib:abc", "z"), ("fake:2104", "there")):  # fmt: skip
        link.hear(code, text)
    assert link.codes == [2110]
    assert (gateway_code("ib:2110"), gateway_code("translation"), gateway_code("ib:x")) == (2110, None, None)


# ---- the other login's session ---------------------------------------------------------------------------------------


def test_a_competing_session_is_known_by_its_number_or_by_its_words_and_is_not_a_link_that_is_down():
    assert COMPETING_SESSION_CODE == 10197 and COMPETING_SESSION_CODE not in GATEWAY_LINK_CODES
    assert says_competing_session("ib:10197", "anything at all")
    assert says_competing_session("ib:354", "No market data during COMPETING live SESSION"), "the number is unverified"
    assert not says_competing_session("ib:2104", "a quote service is there:farm-a")
    assert not says_competing_session("ib:354", "no market data: not subscribed")
    assert not says_competing_session("translation", "competing session"), "only the gateway's own messages count"
    link = heard((10197, "no market data during competing session"))
    assert link.down is False and link.codes == []
    assert "other login" in COMPETING_REASON and COMPETING_REASON != DOWN_REASON
