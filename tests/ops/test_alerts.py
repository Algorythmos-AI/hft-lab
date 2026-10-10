"""Alerts: a page when a problem starts, a page when it is over, nothing in between, and no secret in a log.

The HTTPS request under them is tested here too, against an opener the test supplies: no socket is opened.
"""
from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from hftlab.core import SimClock
from hftlab.ops import alerts, host
from hftlab.ops.alerts import AlertError, Alerts, LogSender, Page, PushSender, sender_from
from hftlab.ops.post import Outcome, post, refusal
from tests.ops.kit import OPEN, Transport

ADDRESS = "https://push.example.invalid/a-topic-nobody-may-learn"


class Pager:
    """A sender for tests: it keeps the pages and delivers, or not, as told."""

    def __init__(self, delivers: bool = True) -> None:
        self.pages: list[Page] = []
        self.delivers = delivers

    def __call__(self, page: Page) -> bool:
        self.pages.append(page)
        return self.delivers


def some_alerts(tmp_path, pager: Pager | None = None) -> tuple[Alerts, Pager]:
    pager = pager or Pager()
    return Alerts(tmp_path / "alerts", pager, SimClock(OPEN)), pager


# ---- once per key ---------------------------------------------------------------------------------------------------


def test_a_problem_pages_when_it_starts_and_not_again_while_it_lasts(tmp_path):
    alerts_, pager = some_alerts(tmp_path)
    assert alerts_.fire("recorder_stale", "Recorder silent", "No status for 95 s.") is True
    assert alerts_.fire("recorder_stale", "Recorder silent", "No status for 400 s.") is False
    assert alerts_.fire("recorder_stale", "Recorder silent", "No status for 900 s.") is False
    assert [(p.key, p.title, p.priority, p.resolved) for p in pager.pages] == [
        ("hft:recorder_stale", "Recorder silent", 4, False),
    ]  # fmt: skip
    assert alerts_.firing() == ["hft:recorder_stale"]


def test_the_state_is_on_disk_so_the_next_process_does_not_page_again(tmp_path):
    first, _ = some_alerts(tmp_path)
    first.fire("recorder_quota", "Quota", "The recorder stopped at its quota.")
    second, other = some_alerts(tmp_path)  # another run of the job, minutes later
    assert second.fire("recorder_quota", "Quota", "The recorder stopped at its quota.") is False and other.pages == []
    state = json.loads((tmp_path / "alerts" / "state.json").read_text())
    assert state == {"hft:recorder_quota": {"delivered": True, "firing": True, "since_ns": OPEN}}
    assert not list((tmp_path / "alerts").glob("*.partial"))


def test_the_end_of_a_problem_pages_once_and_only_if_its_start_was_paged(tmp_path):
    alerts_, pager = some_alerts(tmp_path)
    assert alerts_.resolve("recorder_stale", "Recorder back", "Its status is 3 s old.") is False, "nothing had started"
    alerts_.fire("recorder_stale", "Recorder silent", "No status for 95 s.")
    assert alerts_.resolve("recorder_stale", "Recorder back", "Its status is 3 s old.") is True
    assert alerts_.resolve("recorder_stale", "Recorder back", "Its status is 3 s old.") is False
    assert [(p.resolved, p.priority) for p in pager.pages] == [(False, 4), (True, 2)] and alerts_.firing() == []
    assert alerts_.fire("recorder_stale", "Recorder silent", "Again.") is True, "a new start is a new page"


def test_a_page_that_could_not_be_delivered_is_tried_again_and_not_called_sent(tmp_path):
    offline = Pager(delivers=False)
    alerts_, _ = some_alerts(tmp_path, offline)
    assert alerts_.fire("recorder_io_error", "I/O error", "A write was refused.") is False
    assert alerts_.fire("recorder_io_error", "I/O error", "A write was refused.") is False
    assert len(offline.pages) == 2 and alerts_.firing() == ["hft:recorder_io_error"]
    offline.delivers = True
    assert alerts_.fire("recorder_io_error", "I/O error", "A write was refused.") is True
    assert alerts_.fire("recorder_io_error", "I/O error", "A write was refused.") is False and len(offline.pages) == 3


def test_a_start_that_never_reached_the_owner_needs_no_all_clear(tmp_path):
    offline = Pager(delivers=False)
    alerts_, _ = some_alerts(tmp_path, offline)
    alerts_.fire("connection_down", "Gateway down", "No connection.")
    assert alerts_.resolve("connection_down", "Gateway back", "Connected.") is False
    assert len(offline.pages) == 1 and alerts_.firing() == []


def test_an_all_clear_that_could_not_be_delivered_is_tried_again(tmp_path):
    pager = Pager()
    alerts_, _ = some_alerts(tmp_path, pager)
    alerts_.fire("connection_down", "Gateway down", "No connection.")
    pager.delivers = False
    assert alerts_.resolve("connection_down", "Gateway back", "Connected.") is False
    assert alerts_.firing() == ["hft:connection_down"], "the owner still believes it is down"
    pager.delivers = True
    assert alerts_.resolve("connection_down", "Gateway back", "Connected.") is True and alerts_.firing() == []


# ---- the keys are this platform's own --------------------------------------------------------------------------------


def test_every_key_carries_this_platforms_prefix_and_is_never_another_desks(tmp_path):
    alerts_, pager = some_alerts(tmp_path)
    assert alerts_.key("backup_failed") == alerts_.key("hft:backup_failed") == "hft:backup_failed"
    assert host.ALERT_PREFIX == "hft:"
    alerts_.fire("backup_failed", "Backup", "It failed.")
    assert all(key.startswith("hft:") for key in json.loads((tmp_path / "alerts" / "state.json").read_text()))
    for wrong in ("", "Recorder Stale", "wt:paper-b", "evidence:chain", "a key with spaces", "x" * 60, "UPPER"):
        with pytest.raises(AlertError):
            alerts_.fire(wrong, "t", "m")
    assert len(pager.pages) == 1


def test_a_state_file_that_cannot_be_read_pages_again_rather_than_staying_silent(tmp_path):
    alerts_, pager = some_alerts(tmp_path)
    alerts_.fire("recorder_stale", "Recorder silent", "No status.")
    (tmp_path / "alerts" / "state.json").write_text("[not a state")
    assert alerts_.firing() == [] and alerts_.fire("recorder_stale", "Recorder silent", "No status.") is True
    assert len(pager.pages) == 2


# ---- what a page may carry -------------------------------------------------------------------------------------------


def test_account_ids_are_masked_and_a_page_is_one_bounded_line(tmp_path):
    alerts_, pager = some_alerts(tmp_path)
    alerts_.fire("x", "Account DU1234567 refused", "DU1234567 and U7654321\nare ids. " + "word " * 400, priority=9)
    page = pager.pages[0]
    assert page.title == "Account [account] refused" and "[account] and [account] are ids." in page.message
    assert "DU1234567" not in page.title + page.message and "U7654321" not in page.message
    assert "\n" not in page.message and len(page.message) <= 1_000 and page.priority == 5
    assert alerts.scrub("EUR.USD at 108500, run 12") == "EUR.USD at 108500, run 12", "ordinary numbers are left alone"


# ---- the senders -----------------------------------------------------------------------------------------------------


def test_with_no_address_configured_the_page_is_logged_once_and_nothing_is_sent(tmp_path):
    lines: list[str] = []
    transport = Transport()
    sender = sender_from({"HFT_ALERT_URL": ""}, lines.append, transport)
    assert isinstance(sender, LogSender)
    alerts_ = Alerts(tmp_path / "alerts", sender, SimClock(OPEN))
    assert alerts_.fire("recorder_stale", "Recorder silent", "No status for 95 s.") is True
    assert alerts_.fire("recorder_stale", "Recorder silent", "No status for 95 s.") is False
    assert transport.sent == [] and len(lines) == 1
    assert lines[0].startswith("alerts: no address is configured, so not sent. ALERT [hft:recorder_stale] ")


def test_the_push_sender_posts_the_text_with_a_title_and_a_priority_and_never_logs_the_address(tmp_path):
    lines: list[str] = []
    transport = Transport()
    sender = sender_from({"HFT_ALERT_URL": ADDRESS}, lines.append, transport)
    assert isinstance(sender, PushSender)
    alerts_ = Alerts(tmp_path / "alerts", sender, SimClock(OPEN))
    alerts_.fire("recorder_stale", "Recorder silent", "No status for 95 s while the market is open.", 5)
    alerts_.resolve("recorder_stale", "Recorder back", "Its status is 3 s old.")
    (url, body, headers), (_, _, cleared) = transport.sent
    assert url == ADDRESS and body == b"No status for 95 s while the market is open."
    assert headers == {"Title": "[hft] Recorder silent", "Priority": "5", "Tags": "warning"}
    assert cleared == {"Title": "[hft] Recorder back", "Priority": "2", "Tags": "white_check_mark"}
    assert lines == ["alerts: [hft:recorder_stale] sent", "alerts: [hft:recorder_stale] sent"]
    assert "a-topic-nobody-may-learn" not in "".join(lines) and "example" not in "".join(lines)


def test_a_failed_post_is_logged_by_its_kind_without_the_address(tmp_path):
    lines: list[str] = []
    for transport in (Transport(503), Transport(None, "TimeoutError")):
        sender = PushSender(host.Secret(ADDRESS), lines.append, transport)
        assert sender(Page("hft:x", "t", "m", 3)) is False
    assert lines == ["alerts: [hft:x] NOT sent (HTTP 503)", "alerts: [hft:x] NOT sent (TimeoutError)"]


def test_a_secret_never_prints_its_value():
    secret = host.Secret(ADDRESS)
    assert f"{secret} {secret!r} {[secret]}" == "<set> <set> [<set>]" and secret.reveal() == ADDRESS
    assert str(host.Secret("")) == "<not set>" and not host.Secret("") and secret


def test_the_settings_are_read_by_name_and_an_unset_one_is_empty(monkeypatch):
    for name in host.SETTINGS:
        monkeypatch.delenv(name, raising=False)
    assert host.settings() == dict.fromkeys(host.SETTINGS, "")
    monkeypatch.setenv("HFT_ALERT_URL", ADDRESS)
    monkeypatch.setenv("MODE", "paper")
    assert host.settings()["HFT_ALERT_URL"] == ADDRESS and set(host.settings()) == set(host.SETTINGS)
    assert not [name for name in host.SETTINGS if "MODE" in name or "LIVE" in name], "none is the lock's"


def test_the_command_sends_a_test_page_and_lists_what_is_firing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HFT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("HFT_ALERT_URL", raising=False)
    assert alerts.main(["test"]) == 0
    assert "no address is configured, so not sent. ALERT [hft:test] Test page" in capsys.readouterr().err
    assert alerts.main(["firing"]) == 0 and capsys.readouterr().out.strip() == "nothing is firing"
    Alerts(tmp_path / "alerts", Pager(), SimClock(OPEN)).fire("recorder_quota", "t", "m")
    assert alerts.main(["firing"]) == 0 and capsys.readouterr().out.strip() == "hft:recorder_quota"


# ---- the one HTTPS request -------------------------------------------------------------------------------------------


class Answer:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *details) -> None:
        return None


class Opener:
    """What stands where the network would be: it keeps the request and answers, or fails, as told."""

    def __init__(self, answer=None, error: Exception | None = None) -> None:
        self.answer, self.error, self.requests = answer, error, []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.answer


def test_a_post_is_one_request_with_the_body_the_headers_and_a_timeout():
    opener = Opener(Answer(200))
    outcome = post(ADDRESS, b"hello", {"Title": "t"}, timeout_s=7, opener=opener)
    assert outcome == Outcome(200) and outcome.ok and str(outcome) == "HTTP 200"
    (request, timeout), = opener.requests
    assert request.get_method() == "POST" and request.full_url == ADDRESS and request.data == b"hello"
    assert request.get_header("Title") == "t" and timeout == 7


@pytest.mark.parametrize(
    ("error", "outcome"),
    [(HTTPError(ADDRESS, 422, "Unprocessable", None, io.BytesIO()), Outcome(422)),
     (HTTPError(ADDRESS, 307, "Temporary Redirect", None, io.BytesIO()), Outcome(307)),
     (URLError(TimeoutError("timed out")), Outcome(None, "TimeoutError")),
     (URLError("no route"), Outcome(None, "URLError")),
     (ConnectionResetError("reset"), Outcome(None, "ConnectionResetError"))],
)  # fmt: skip
def test_a_failure_of_the_network_is_an_outcome_never_an_exception_and_never_names_the_address(error, outcome):
    found = post(ADDRESS, b"x", {}, opener=Opener(error=error))
    assert found == outcome and not found.ok and "example" not in str(found) and "topic" not in str(found)


@pytest.mark.parametrize(
    ("url", "why"),
    [("http://push.example.invalid/topic", "not an https address"), ("ftp://example.invalid/x", "not an https address"),
     ("https://localhost/topic", "an address on this machine"),
     ("https://127.0.0.1:4002/", "an address on this machine"),
     ("https://[::1]/x", "an address on this machine"), ("https://0.0.0.0/x", "an address on this machine"),
     ("https://gateway.localhost/x", "an address on this machine"),
     ("https://user:pass@push.example.invalid/x", "a user name or password in the address"),
     ("https:///topic", "no host in the address"), ("", "not an https address"), ("a topic", "not an https address")],
)  # fmt: skip
def test_it_sends_to_https_addresses_elsewhere_and_to_nothing_else(url, why):
    """So it cannot be pointed at the gateway, and a mistyped setting cannot send a page in the clear."""
    opener = Opener(Answer(200))
    assert refusal(url) == f"refused: {why}"
    assert post(url, b"x", {}, opener=opener) == Outcome(None, f"refused: {why}") and opener.requests == []


def test_a_redirect_is_not_followed():
    from hftlab.ops.post import _NoRedirect

    elsewhere = "https://elsewhere.invalid/"
    assert _NoRedirect().redirect_request(None, None, 307, "Temporary Redirect", {}, elsewhere) is None
    assert refusal(ADDRESS) == "" and refusal("https://203.0.113.9/x") == ""
