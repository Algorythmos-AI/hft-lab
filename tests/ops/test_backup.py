"""The backup: what must outlive the disk is listed, copied whole and tagged as ours, and nothing is ever deleted."""
from __future__ import annotations

import ast
import hashlib
import json

import pytest

from hftlab.core import SimClock
from hftlab.ops import backup, host, status
from hftlab.ops.alerts import Alerts
from hftlab.ops.backup import BackupError, Entry, manifest, to_directory, to_tool, tool_command, verify
from tests.ops.kit import SATURDAY, Host, a_shadow_session, sources
from tests.support import ROOT, called_name

STAMP = "20261014T030000Z"


def a_host_with_history(tmp_path) -> Host:
    rig = Host(tmp_path).record(6).stop()
    a_shadow_session(tmp_path)
    (tmp_path / "var" / "alerts").mkdir()
    (tmp_path / "var" / "alerts" / "state.json").write_text("{}\n")
    return rig


def listed(tmp_path) -> list[Entry]:
    places = sources(tmp_path)
    return manifest(places.record_root, places.shadow_journal.parent, places.state)


# ---- what is on the list ---------------------------------------------------------------------------------------------


def test_the_list_is_the_journals_the_run_counter_the_shadow_journals_and_the_alert_state(tmp_path):
    a_host_with_history(tmp_path)
    assert [entry.name for entry in listed(tmp_path)] == [
        "record/state.json", "record/2026-10-14/journal.jsonl", "shadow/live.jsonl", "alerts/state.json",
    ]  # fmt: skip
    assert all(entry.path.is_file() for entry in listed(tmp_path))


def test_the_recordings_themselves_and_the_status_file_are_not_on_it(tmp_path):
    a_host_with_history(tmp_path)
    names = " ".join(entry.name for entry in listed(tmp_path))
    assert ".seg" not in names and "status.json" not in names, "recordings leave through the store's own offload"
    assert (tmp_path / "var" / "record" / "2026-10-14" / "03.seg").is_file()


def test_on_a_host_with_nothing_yet_the_list_is_empty(tmp_path):
    assert listed(tmp_path) == []


# ---- into a directory ------------------------------------------------------------------------------------------------


def test_a_run_writes_a_new_tagged_folder_with_every_file_and_a_manifest_that_matches(tmp_path):
    a_host_with_history(tmp_path)
    away = tmp_path / "away"
    away.mkdir()
    folder = to_directory(listed(tmp_path), away, host.BACKUP_TAG, STAMP)
    assert folder == away / "hft-lab" / STAMP
    document = json.loads((folder / "MANIFEST.json").read_text())
    assert document["tag"] == "hft-lab" and document["taken_at"] == STAMP
    assert [item["name"] for item in document["files"]] == [entry.name for entry in listed(tmp_path)]
    for entry, item in zip(listed(tmp_path), document["files"], strict=True):
        copy = (folder / entry.name).read_bytes()
        assert copy == entry.path.read_bytes() and len(copy) == item["size"]
        assert hashlib.sha256(copy).hexdigest() == item["sha256"]


def test_it_never_deletes_and_never_writes_over_a_backup(tmp_path):
    a_host_with_history(tmp_path)
    away = tmp_path / "away"
    away.mkdir()
    before = {path: path.read_bytes() for path in (tmp_path / "var").rglob("*") if path.is_file()}
    first = to_directory(listed(tmp_path), away, host.BACKUP_TAG, STAMP)
    kept = {path: path.read_bytes() for path in first.rglob("*") if path.is_file()}
    with pytest.raises(BackupError, match="never written over"):
        to_directory(listed(tmp_path), away, host.BACKUP_TAG, STAMP)
    second = to_directory(listed(tmp_path), away, host.BACKUP_TAG, "20261015T030000Z")
    assert first.is_dir() and second.is_dir() and first != second
    assert {path: path.read_bytes() for path in first.rglob("*") if path.is_file()} == kept
    assert {path: path.read_bytes() for path in (tmp_path / "var").rglob("*") if path.is_file()} == before


def test_the_module_has_no_call_that_removes_anything():
    tree = ast.parse((ROOT / "src" / "hftlab" / "ops" / "backup.py").read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    removing = {"unlink", "remove", "rmdir", "rmtree", "removedirs", "rename", "truncate", "ftruncate"}
    assert not {called_name(node) for node in calls} & removing
    assert "os.replace" not in {ast.unparse(node.func) for node in calls}, "nor moves one file over another"


def test_a_copy_that_does_not_read_back_the_same_fails_the_run_and_leaves_no_manifest(tmp_path, monkeypatch):
    a_host_with_history(tmp_path)
    away = tmp_path / "away"
    away.mkdir()
    monkeypatch.setattr(backup, "_digest", lambda path: ("0" * 64, 0))
    with pytest.raises(BackupError, match="does not read back the same"):
        to_directory(listed(tmp_path), away, host.BACKUP_TAG, STAMP)
    assert not (away / "hft-lab" / STAMP / "MANIFEST.json").exists(), "a folder without a manifest is not a backup"


def test_a_backup_directory_that_is_not_there_is_not_created_on_this_disk(tmp_path):
    a_host_with_history(tmp_path)
    with pytest.raises(BackupError, match="the owner's to create or mount"):
        to_directory(listed(tmp_path), tmp_path / "not-mounted", host.BACKUP_TAG, STAMP)
    assert not (tmp_path / "not-mounted").exists()


def test_a_backup_folder_can_be_checked_against_its_own_manifest(tmp_path, capsys):
    a_host_with_history(tmp_path)
    away = tmp_path / "away"
    away.mkdir()
    folder = to_directory(listed(tmp_path), away, host.BACKUP_TAG, STAMP)
    assert verify(folder) == [] and backup.main(["verify", "--folder", str(folder)]) == 0
    assert "matches its manifest" in capsys.readouterr().out
    journal = folder / "record" / "2026-10-14" / "journal.jsonl"
    journal.write_bytes(journal.read_bytes()[:-1])
    (folder / "shadow" / "live.jsonl").rename(folder / "shadow" / "moved.jsonl")
    assert verify(folder) == [
        "record/2026-10-14/journal.jsonl: does not match its manifest", "shadow/live.jsonl: not in the folder",
    ]  # fmt: skip
    assert backup.main(["verify", "--folder", str(folder)]) == 1
    (folder / "MANIFEST.json").unlink()
    assert verify(folder) == [f"{folder} has no manifest that can be read: it is not a whole backup"]
    with pytest.raises(SystemExit):
        backup.main(["verify"])


# ---- to a tool -------------------------------------------------------------------------------------------------------

RESTIC = ["restic", "backup", "--tag", "{tag}", "--files-from", "{list}"]


def test_a_tool_is_handed_the_tag_and_a_file_that_lists_what_to_back_up(tmp_path):
    a_host_with_history(tmp_path)
    ran = []
    listing = tmp_path / "var" / "backup" / "files.txt"
    to_tool(listed(tmp_path), RESTIC, host.BACKUP_TAG, listing, lambda command: ran.append(list(command)) or 0)
    assert ran == [["restic", "backup", "--tag", "hft-lab", "--files-from", str(listing)]]
    assert listing.read_text().splitlines() == [str(entry.path.resolve()) for entry in listed(tmp_path)]


def test_a_tool_that_fails_fails_the_run(tmp_path):
    with pytest.raises(BackupError, match="ended with status 3"):
        to_tool([], RESTIC, host.BACKUP_TAG, tmp_path / "files.txt", lambda command: 3)


@pytest.mark.parametrize(
    "command",
    [["restic", "forget", "--tag", "{tag}", "--files-from", "{list}"],
     ["restic", "backup", "--prune", "{tag}", "{list}"],
     ["rclone", "sync", "--delete-during", "{tag}", "{list}"], ["rsync", "--remove-source-files", "{tag}", "{list}"],
     ["sh", "-c", "rm", "{tag}", "{list}"], ["restic", "backup", "--files-from", "{list}"], ["restic", "backup"]],
)  # fmt: skip
def test_a_command_that_could_destroy_a_backup_or_takes_no_tag_and_list_is_refused(command, tmp_path):
    ran = []
    with pytest.raises(BackupError):
        to_tool([], command, host.BACKUP_TAG, tmp_path / "files.txt", lambda words: ran.append(words) or 0)
    assert ran == [] and not (tmp_path / "files.txt").exists(), "refused before anything is written or run"
    assert tool_command(RESTIC, "hft-lab", tmp_path / "f")[3] == "hft-lab"


# ---- the command -----------------------------------------------------------------------------------------------------


def test_the_command_lists_and_hands_over_to_the_configured_directory(configured, tmp_path, monkeypatch, capsys):
    a_host_with_history(tmp_path)
    assert backup.main(["list", *configured]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith("record/state.json\t")
    monkeypatch.delenv("HFT_BACKUP_DIR", raising=False)
    assert backup.main(["run", *configured]) == 2
    assert "nowhere to hand it to" in capsys.readouterr().err
    (tmp_path / "away").mkdir()
    monkeypatch.setenv("HFT_BACKUP_DIR", str(tmp_path / "away"))
    assert backup.main(["run", *configured]) == 0
    assert "4 file(s) handed to" in capsys.readouterr().out
    (folder,) = (tmp_path / "away" / "hft-lab").iterdir()
    assert (folder / "MANIFEST.json").is_file() and (folder / "record" / "2026-10-14" / "journal.jsonl").is_file()


def test_a_failed_hand_over_is_left_for_the_status_check_which_pages_once_and_takes_it_back(
    configured, tmp_path, monkeypatch, capsys
):
    a_host_with_history(tmp_path)
    monkeypatch.setenv("HFT_BACKUP_DIR", str(tmp_path / "not-mounted"))
    rig_sources = sources(tmp_path)
    assert status.read(rig_sources, SATURDAY).backup is None
    assert backup.main(["run", *configured]) == 1
    assert "backup: FAILED" in capsys.readouterr().err
    failed = status.read(rig_sources, SATURDAY)  # the job has no network: the status check is what pages
    assert [p.code for p in failed.problems] == ["backup_failed"] and failed.level == "bad"
    screen = status.render(failed)
    assert not failed.backup.ok and "backup     last run 20" in screen and "FAILED" in screen
    pages = []

    def sender(page) -> bool:
        pages.append((page.key, page.resolved))
        return True

    alerts = Alerts(tmp_path / "var" / "alerts", sender, SimClock(SATURDAY))
    status.page(failed, alerts)
    status.page(status.read(rig_sources, SATURDAY), alerts)
    (tmp_path / "not-mounted").mkdir()
    assert backup.main(["run", *configured]) == 0
    good = status.read(rig_sources, SATURDAY)
    assert good.problems == () and good.backup.ok and good.backup.files == 4
    status.page(good, alerts)
    assert pages == [("hft:backup_failed", False), ("hft:backup_failed", True)], "once at the start, once at the end"


def test_the_shipped_settings_name_no_tool_and_a_malformed_one_is_refused(tmp_path):
    assert backup.load_tool(ROOT / "config" / "ops.yaml") is None
    (tmp_path / "ops.yaml").write_text("backup:\n  tool: restic backup\n")
    with pytest.raises(BackupError, match="a command as a list of words"):
        backup.load_tool(tmp_path / "ops.yaml")
    (tmp_path / "ops.yaml").write_text("backup:\n  tool: [restic, backup, --tag, '{tag}', --files-from, '{list}']\n")
    assert backup.load_tool(tmp_path / "ops.yaml") == RESTIC
