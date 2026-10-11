"""Publishing the snapshot: it conforms in every state, it says only what the contract names, and it is sent

only when it may be: signed as the site expects, to an address and with a key that come from the environment.
The network is a stand-in that keeps what it was asked to send. No request leaves the machine.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import inspect
import json
import re
import subprocess

import pytest

from hftlab.core import SimClock
from hftlab.ops import status
from hftlab.ops.post import Outcome
from hftlab.ops.status import read
from hftlab.publish import __main__ as cli
from hftlab.publish import contract, send, snapshot
from hftlab.publish.send import Refused
from hftlab.publish.snapshot import SnapshotError, build, commit_of, load_phases
from tests.ops.kit import MS, OPEN, SATURDAY, SECOND, Host, Transport, a_shadow_session, sources
from tests.support import ROOT, GitRepo, called_name

SCHEMA = contract.load(ROOT / contract.CONTRACT)
PHASES = load_phases(ROOT / "config" / "ops.yaml")
VERSION = "0f1e2d3c4b5a"
SITE = "https://status.example.invalid/api/ingest"
KEY = "a-test-key-that-is-not-a-real-one"
SETTINGS = {"HFT_PUBLISH_URL": SITE, "HFT_PUBLISH_KEY_ID": "hft-test", "HFT_PUBLISH_KEY": KEY}
OPTIONAL = {"recorder", "pairs", "sleeves", "books", "learning"}
MINUTE = 60 * SECOND


def snap(reading, version: str | None = VERSION) -> dict:
    return build(reading, phases=PHASES, version=version)


# ---- a conforming snapshot in each state -----------------------------------------------------------------------------


def test_before_any_recording_it_holds_identity_windows_stage_phases_and_health_and_nothing_else(tmp_path):
    found = snap(read(sources(tmp_path), OPEN))
    assert contract.problems(found, SCHEMA) == []
    assert set(found) == set(SCHEMA["required"]), "no part that does not exist yet, not even as zeros"
    assert found["desk"] == {
        "mode": "paper", "stage": "foundation", "venue": "ibkr_paper", "market_open": True, "kill": False,
        "flatten": False,
    }  # fmt: skip
    assert found["health"] == {"level": "ok", "reasons": []}
    assert found["as_of"] == found["generated_at"] == "2026-10-14T03:00:00Z" and found["version"] == VERSION
    assert found["run_id"] == f"20261014T030000Z-{VERSION}"


def test_with_the_recorder_running_it_reports_the_recorder(tmp_path):
    rig = Host(tmp_path).record(12)
    reading = read(rig.sources, rig.clock.now_ns())
    found = snap(reading)
    assert contract.problems(found, SCHEMA) == [] and set(found) == set(SCHEMA["required"]) | {"recorder"}
    assert found["desk"]["stage"] == "recording" and found["health"] == {"level": "ok", "reasons": []}
    recorder = found["recorder"]
    assert set(recorder) == {"state", "events_today", "last_event_at", "gaps_unexplained_today", "disk_used_mb"}
    assert recorder["state"] == "running" and recorder["events_today"] == reading.recorder.records_today > 1_000
    assert recorder["last_event_at"] == "2026-10-14T03:00:10Z" and recorder["gaps_unexplained_today"] == 0
    assert recorder["disk_used_mb"] == round(reading.recorder.used_bytes / 1_000_000, 1) > 0


def test_with_the_recorder_dead_it_says_stopped_and_gives_the_reason_as_a_code(tmp_path):
    rig = Host(tmp_path).record(12)
    reading = read(rig.sources, rig.clock.now_ns() + 5 * MINUTE)
    found = snap(reading)
    assert contract.problems(found, SCHEMA) == []
    assert found["recorder"]["state"] == "stopped" and found["desk"]["stage"] == "recording"
    assert found["health"] == {"level": "bad", "reasons": ["recorder_stale"]}
    assert found["as_of"] == "2026-10-14T03:05:12Z", "the time it was built, not the recorder's last beat"


def test_with_the_recorder_stopped_at_the_weekend_it_is_stopped_and_healthy(tmp_path):
    rig = Host(tmp_path).record(6).stop()
    found = snap(read(rig.sources, SATURDAY))
    assert contract.problems(found, SCHEMA) == [] and found["recorder"]["state"] == "stopped"
    assert found["health"] == {"level": "ok", "reasons": []} and found["desk"]["market_open"] is False
    assert found["recorder"]["events_today"] == 0, "Saturday's folder, not Wednesday's"


def test_a_shadow_run_moves_the_stage_and_adds_no_part_that_is_not_real_yet(tmp_path):
    rig = Host(tmp_path).record(6)
    a_shadow_session(tmp_path)
    found = snap(read(rig.sources, rig.clock.now_ns()))
    assert contract.problems(found, SCHEMA) == [] and found["desk"]["stage"] == "shadow"
    assert not set(found) & {"pairs", "sleeves", "books", "learning"}, "absent, never zeros"


def test_a_store_written_by_a_dry_run_is_not_published_as_the_recorder(tmp_path):
    rig = Host(tmp_path, venue="fake").record(6)
    found = snap(read(rig.sources, rig.clock.now_ns()))
    assert contract.problems(found, SCHEMA) == [] and "recorder" not in found
    assert found["desk"]["stage"] == "foundation" and found["health"]["reasons"] == ["recorder_not_on_paper_venue"]


def test_the_switches_are_published_as_they_are(tmp_path):
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "KILL").touch()
    desk = snap(read(sources(tmp_path), OPEN))["desk"]
    assert (desk["kill"], desk["flatten"]) == (True, False)


# ---- what it may and may not say -------------------------------------------------------------------------------------


def test_the_mode_is_paper_and_nothing_handed_in_can_choose_another():
    assert list(inspect.signature(build).parameters) == ["reading", "phases", "version"]
    tree = ast.parse((ROOT / "src" / "hftlab" / "publish" / "snapshot.py").read_text())
    modes = [ast.unparse(v) for node in ast.walk(tree) if isinstance(node, ast.Dict)
             for k, v in zip(node.keys, node.values, strict=True) if ast.unparse(k) == "'mode'"]  # fmt: skip
    assert modes == ["'paper'"], "a literal, written once"


def test_expected_windows_are_the_week_that_is_open_or_next_and_the_one_after(tmp_path):
    open_now = snap(read(sources(tmp_path), OPEN))["expected_windows"]
    assert open_now == [
        {"session": "2026-W42", "start": "2026-10-11T21:15:00Z", "end": "2026-10-16T21:00:00Z"},
        {"session": "2026-W43", "start": "2026-10-18T21:15:00Z", "end": "2026-10-23T21:00:00Z"},
    ]
    weekend = snap(read(sources(tmp_path), SATURDAY))["expected_windows"]
    assert [week["session"] for week in weekend] == ["2026-W43", "2026-W44"]
    assert weekend[0]["start"] > "2026-10-17T03:00:00Z", "at the weekend the first window is still to come"


def test_a_problems_sentence_and_every_path_stay_on_the_host(tmp_path):
    rig = Host(tmp_path).record(12)
    reading = read(rig.sources, rig.clock.now_ns() + 5 * MINUTE)
    body = send.body_of(snap(reading)).decode()
    assert reading.problems[0].text not in body and "silent" not in body
    assert str(tmp_path) not in body and "/var/" not in body and "status.json" not in body
    assert not re.search(r"\s", body), "one canonical form, with no space for prose"


def test_every_reason_the_status_can_give_is_a_code_the_contract_accepts():
    tree = ast.parse((ROOT / "src" / "hftlab" / "ops" / "status.py").read_text())
    made = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and called_name(n) == "Problem"]
    reasons = [ast.literal_eval(n.args[0]) for n in made]
    allowed = SCHEMA["properties"]["health"]["properties"]["reasons"]
    assert 10 <= len(reasons) <= allowed["maxItems"] and len(set(reasons)) == len(reasons)
    assert all(re.search(allowed["items"]["pattern"], code) for code in reasons)


def test_without_a_commit_the_version_is_null_and_the_snapshot_still_conforms(tmp_path):
    found = snap(read(sources(tmp_path), OPEN), version=None)
    assert contract.problems(found, SCHEMA) == [] and found["version"] is None
    assert found["run_id"] == "20261014T030000Z-unversioned"
    later = snap(read(sources(tmp_path), OPEN + SECOND), version=None)
    assert later["run_id"] != found["run_id"], "the site takes an equal run id for the same snapshot"


def test_the_phases_come_from_the_settings_and_agree_with_the_roadmap():
    assert list(PHASES) == ["F0", "F1", "F2", "F3", "F4", "F5", "F6", "F7"]
    assert snap(read(sources(ROOT / "nowhere"), OPEN))["phases"][0] == {"id": "F0", "state": "done"}
    roadmap = (ROOT / "docs" / "roadmap.md").read_text()
    gates = dict(re.findall(r"^\| (F[0-7]) \| (Open|Met) \|", roadmap, re.MULTILINE))
    assert gates, "the roadmap's table of gates is where a phase's state is written down"
    for phase, state in gates.items():
        assert PHASES[phase] == ("open" if state == "Open" else "done"), f"{phase}: the roadmap says {state}"


@pytest.mark.parametrize(
    "text",
    ["phases: {F0: done}",
     "phases: {F0: done, F1: open, F2: open, F3: open, F4: open, F5: open, F6: open, F7: finished}",
     "phases: [done]", "status: {}", ": not yaml :"],
)  # fmt: skip
def test_phases_that_are_not_the_eight_in_their_three_states_are_refused(tmp_path, text):
    (tmp_path / "ops.yaml").write_text(text)
    with pytest.raises(SnapshotError):
        load_phases(tmp_path / "ops.yaml")


# ---- which commit is running -----------------------------------------------------------------------------------------


def test_the_commit_is_read_from_gits_own_files_in_a_checkout_a_detached_head_and_a_worktree(tmp_path):
    repo = GitRepo(tmp_path / "repo")
    repo.add("a.txt")
    head = repo.commit("one")
    assert commit_of(repo.root) == head[:12]
    repo.git("pack-refs", "--all")
    assert commit_of(repo.root) == head[:12], "a branch kept in packed-refs"
    repo.git("worktree", "add", "-q", "-b", "side", str(tmp_path / "tree"))
    assert commit_of(tmp_path / "tree") == head[:12], "a worktree, whose .git is a file"
    repo.git("checkout", "-q", "--detach")
    assert commit_of(repo.root) == head[:12]
    assert commit_of(tmp_path) is None and commit_of(tmp_path / "nothing") is None
    (repo.root / ".git" / "HEAD").write_text("ref: refs/heads/gone\n")
    assert commit_of(repo.root) is None


def test_here_it_is_the_commit_git_itself_reports():
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
    assert commit_of(ROOT) == head.stdout.strip()[:12]


# ---- the signature ---------------------------------------------------------------------------------------------------


def test_the_signature_is_hmac_sha256_over_the_timestamp_a_dot_and_the_body():
    body = b'{"schema":"hft-lab/snapshot"}'
    assert send.sign("a-test-key", 1_791_946_800, body) == (
        "sha256=a9cc9e2a4e00fd4cbdfafd7cd775ceca6e794171dc46863f7643df386a8f64ff"
    )
    by_hand = hmac.new(KEY.encode(), b"1791946800." + body, hashlib.sha256).hexdigest()
    assert send.sign(KEY, 1_791_946_800, body) == f"sha256={by_hand}"
    assert send.sign(KEY, 1_791_946_801, body) != send.sign(KEY, 1_791_946_800, body)
    assert send.sign(KEY, 1_791_946_800, body + b" ") != send.sign(KEY, 1_791_946_800, body)


def test_what_is_sent_is_the_snapshot_in_one_canonical_form_with_the_headers_the_site_reads(tmp_path):
    rig = Host(tmp_path).record(12)
    found = snap(read(rig.sources, rig.clock.now_ns()))
    transport = Transport()
    outcome = send.send(found, SCHEMA, SETTINGS, transport, now_s=1_791_946_812)
    assert outcome.ok and len(transport.sent) == 1
    url, body, headers = transport.sent[0]
    assert url == SITE and json.loads(body) == found and body == send.body_of(found)
    assert body == json.dumps(found, sort_keys=True, separators=(",", ":")).encode()
    assert headers == {
        "content-type": "application/json",
        "x-wt-timestamp": "1791946812",
        "x-wt-signature": send.sign(KEY, 1_791_946_812, body),
        "x-wt-key-id": "hft-test",
    }
    assert re.fullmatch(r"sha256=[0-9a-f]{64}", headers["x-wt-signature"])
    assert KEY not in body.decode() and KEY not in json.dumps(headers)


def test_the_hosting_providers_bypass_token_is_sent_only_when_one_is_configured(tmp_path):
    found = snap(read(sources(tmp_path), OPEN))
    transport = Transport()
    send.send(found, SCHEMA, {**SETTINGS, "HFT_PUBLISH_BYPASS": "a-bypass-token"}, transport, now_s=1)
    assert transport.sent[0][2]["x-vercel-protection-bypass"] == "a-bypass-token" and len(transport.sent[0][2]) == 5


# ---- when it is not sent ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("missing", [("HFT_PUBLISH_URL",), ("HFT_PUBLISH_KEY_ID",), ("HFT_PUBLISH_KEY",),
                                     ("HFT_PUBLISH_URL", "HFT_PUBLISH_KEY_ID", "HFT_PUBLISH_KEY")])  # fmt: skip
def test_without_the_address_the_key_id_or_the_key_it_refuses_and_says_which(tmp_path, missing):
    found = snap(read(sources(tmp_path), OPEN))
    transport = Transport()
    for blank in ("", "   ", None):
        settings = {k: v for k, v in SETTINGS.items() if k not in missing}
        settings |= {} if blank is None else dict.fromkeys(missing, blank)
        with pytest.raises(Refused, match=r"not sending: .*not set") as refused:
            send.send(found, SCHEMA, settings, transport, now_s=1)
        assert re.findall(r"HFT_[A-Z_]+", str(refused.value)) == list(missing), "it names what is missing"
    assert transport.sent == []


@pytest.mark.parametrize(
    ("change", "why"),
    [({"HFT_PUBLISH_KEY_ID": "default"}, "must be this platform's own key id"),
     ({"HFT_PUBLISH_KEY_ID": "two words"}, "must be this platform's own key id"),
     ({"HFT_PUBLISH_KEY_ID": "x" * 41}, "must be this platform's own key id"),
     ({"HFT_PUBLISH_URL": "http://status.example.invalid/api/ingest"}, "HFT_PUBLISH_URL is not an https address"),
     ({"HFT_PUBLISH_URL": "https://127.0.0.1:4002/api/ingest"}, "HFT_PUBLISH_URL is an address on this machine")],
)  # fmt: skip
def test_a_key_id_that_is_another_senders_or_an_address_it_may_not_use_is_refused(tmp_path, change, why):
    transport = Transport()
    with pytest.raises(Refused, match=why) as refused:
        send.send(snap(read(sources(tmp_path), OPEN)), SCHEMA, {**SETTINGS, **change}, transport, now_s=1)
    assert transport.sent == [] and KEY not in str(refused.value) and "example" not in str(refused.value)


@pytest.mark.parametrize(
    ("path", "value"),
    [(("note",), "the recorder was restarted by hand at noon"), (("desk", "mode"), "live"),
     (("desk", "account"), "DU0000000"), (("health", "reasons"), ["free text is not a code"]),
     (("host",), "a-host-name"), (("recorder",), {"state": "running", "log": "a line of the log"})],
)  # fmt: skip
def test_nothing_but_contract_fields_is_ever_sent(tmp_path, path, value):
    found = snap(read(sources(tmp_path), OPEN))
    target = found
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    transport = Transport()
    with pytest.raises(Refused, match="does not conform to its contract") as refused:
        send.send(found, SCHEMA, SETTINGS, transport, now_s=1)
    assert transport.sent == [] and str(value) not in str(refused.value), "the refusal names the place, not the value"


# ---- the command -----------------------------------------------------------------------------------------------------


def test_a_dry_run_is_the_default_it_prints_the_snapshot_and_sends_nothing(configured, tmp_path, capsys):
    rig = Host(tmp_path).record(12)
    for arguments in (configured, [*configured, "--dry-run"]):
        transport = Transport()
        assert cli.main(arguments, settings=SETTINGS, clock=rig.clock, poster=transport) == 0
        out = capsys.readouterr()
        printed = json.loads(out.out)
        assert transport.sent == [] and "DRY RUN, nothing is sent" in out.err
        assert contract.problems(printed, SCHEMA) == [] and printed["recorder"]["state"] == "running"
        assert printed["version"] == commit_of(cli.Path.cwd()), "the commit of the code that built it"


def test_send_signs_with_the_clocks_time_and_reports_without_the_address_or_the_key(configured, tmp_path, capsys):
    rig = Host(tmp_path).record(12)
    transport = Transport()
    assert cli.main([*configured, "--send"], settings=SETTINGS, clock=rig.clock, poster=transport) == 0
    out = capsys.readouterr()
    (url, body, headers), = transport.sent
    assert url == SITE and headers["x-wt-timestamp"] == str(rig.clock.now_ns() // SECOND)
    assert headers["x-wt-signature"] == send.sign(KEY, rig.clock.now_ns() // SECOND, body)
    assert contract.problems(json.loads(body), SCHEMA) == []
    assert out.out.startswith("publish: sent 20261014T030012Z-") and "(HTTP 200)" in out.out
    assert KEY not in out.out + out.err and "example" not in out.out + out.err


@pytest.mark.parametrize(
    ("answer", "code", "said"),
    [((200, ""), 0, "publish: sent"), ((409, ""), 0, "already holds a newer snapshot"),
     ((401, ""), 1, "NOT sent: HTTP 401"), ((422, ""), 1, "NOT sent: HTTP 422"), ((503, ""), 1, "NOT sent: HTTP 503"),
     ((403, ""), 1, "NOT sent: HTTP 403"), ((None, "TimeoutError"), 1, "NOT sent: TimeoutError")],
)  # fmt: skip
def test_the_exit_code_follows_the_sites_answer(configured, tmp_path, capsys, answer, code, said):
    transport = Transport(*answer)
    assert cli.main([*configured, "--send"], settings=SETTINGS, clock=SimClock(OPEN), poster=transport) == code
    out = capsys.readouterr()
    assert said in out.out + out.err and len(transport.sent) == 1, "one try: the timer is the retry"


@pytest.mark.parametrize(
    ("status", "said"),
    [(401, "does not know this key id or this key, or the two clocks are more than five minutes apart"),
     (403, "key-scope: the site's scope for this key id does not cover this desk's snapshot; or its deployment"),
     (422, "does not hold this schema, or holds an older copy of the contract"),
     (503, "key-scope-config: the site's scope setting cannot be read")],
)  # fmt: skip
def test_a_refusal_of_the_site_is_said_with_what_it_means_to_this_sender(configured, capsys, status, said):
    transport = Transport(status, "")
    assert cli.main([*configured, "--send"], settings=SETTINGS, clock=SimClock(OPEN), poster=transport) == 1
    out = capsys.readouterr()
    (line,) = out.err.splitlines()
    assert line.startswith(f"publish: NOT sent: HTTP {status}: ") and said in line and out.out == ""
    assert KEY not in line and "example" not in line and SETTINGS["HFT_PUBLISH_KEY_ID"] not in line


def test_an_answer_with_no_known_meaning_is_said_as_it_came_and_a_status_with_two_causes_names_both():
    assert send.meaning(Outcome(500)) == "HTTP 500" and send.meaning(Outcome(None, "TimeoutError")) == "TimeoutError"
    assert send.meaning(Outcome(200)) == "HTTP 200" and set(send.MEANS) == {401, 403, 422, 503}
    assert "deployment protection" in send.MEANS[403] and "not serving" in send.MEANS[503]
    assert all(text.isascii() and "\n" not in text for text in send.MEANS.values())


def test_send_without_a_key_refuses_says_so_and_sends_nothing(configured, tmp_path, capsys):
    transport = Transport()
    settings = {**SETTINGS, "HFT_PUBLISH_KEY": ""}
    assert cli.main([*configured, "--send"], settings=settings, clock=SimClock(OPEN), poster=transport) == 2
    assert "publish: not sending: HFT_PUBLISH_KEY is not set" in capsys.readouterr().err and transport.sent == []
    with pytest.raises(SystemExit):
        cli.main([*configured, "--send", "--dry-run"], settings=SETTINGS, clock=SimClock(OPEN), poster=transport)
    assert transport.sent == []


def test_a_contract_or_a_setting_it_cannot_read_refuses_before_anything_is_built(configured, tmp_path, capsys):
    transport = Transport()
    (tmp_path / "other.json").write_text(json.dumps({**SCHEMA, "minProperties": 1}))
    for extra in (["--contract", str(tmp_path / "other.json")], ["--contract", str(tmp_path / "missing.json")],
                  ["--calendar", str(tmp_path / "nothing")]):  # fmt: skip
        assert cli.main([*configured, "--send", *extra], settings=SETTINGS, clock=SimClock(OPEN), poster=transport) == 2
    said = capsys.readouterr().err
    assert "the keyword minProperties" in said and "cannot read the contract" in said and transport.sent == []


def test_the_command_as_shipped_prints_a_conforming_snapshot_of_this_checkout(capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    for name in ("HFT_STATE_DIR", "HFT_PUBLISH_URL", "HFT_PUBLISH_KEY_ID", "HFT_PUBLISH_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main([]) == 0
    assert contract.problems(json.loads(capsys.readouterr().out), SCHEMA) == []
    assert cli.main(["--send"]) == 2 and "not set" in capsys.readouterr().err


def test_the_timeout_of_a_send_comes_from_the_settings(tmp_path):
    assert cli.timeout_of(ROOT / "config" / "ops.yaml") == 20
    (tmp_path / "ops.yaml").write_text("publish: {timeout_seconds: 0}\n")
    with pytest.raises(SnapshotError):
        cli.timeout_of(tmp_path / "ops.yaml")
    assert snapshot.MAX_REASONS == 20 and status.VENUE == "ibkr_paper" and MS * 1_000 == SECOND
