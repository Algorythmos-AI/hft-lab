"""The backup: what must outlive the disk is listed, copied whole and tagged as ours, and nothing is ever deleted."""
from __future__ import annotations

import ast
import hashlib
import json

import pytest

from hftlab import lineup
from hftlab.core import SimClock
from hftlab.journal import verify as verify_journal
from hftlab.learn import train
from hftlab.ops import backup, host, status
from hftlab.ops.alerts import Alerts
from hftlab.ops.backup import (
    LEFT_OUT,
    BackupError,
    Entry,
    learning_places,
    left_out,
    manifest,
    to_directory,
    to_tool,
    tool_command,
    verify,
)
from hftlab.trader import resume
from hftlab.trader.__main__ import only_one
from hftlab.trader.session import STATE
from tests.learn import kit as learning
from tests.ops.kit import SATURDAY, Host, a_shadow_session, sources
from tests.support import ROOT, called_name
from tests.trader.kit import Desk, put_stage

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


# ---- the trader's folder and the learning folder: taken whole, but for what is named ---------------------------------

SHIPPED = ["--cost-model", str(ROOT / "config/cost_model.yaml"), "--calendar", str(ROOT / "config/calendar"),
           "--feasibility", str(ROOT / "config/feasibility.yaml")]  # fmt: skip


@pytest.fixture(scope="module")
def lived(tmp_path_factory):
    """A folder a training run has written into, and a trader on paper has run in, stopped and been asked something.

    Nothing is written by hand: the files are the ones the commands' own code leaves.
    """
    folder = tmp_path_factory.mktemp("lived")
    learning.record(folder / "rec", learning.weeks(1))
    (folder / "registry.yaml").write_text("history: []\n")
    argv = ["--config", str(learning.learn_settings(folder)), *SHIPPED, "--store", str(folder / "rec")]
    assert train.main([*argv, "--trial-registry", str(folder / "registry.yaml")], SimClock(learning.SATURDAY)) == 0
    put_stage(folder / "trader", "control", "min_size")
    with only_one(folder / "trader"):
        desk = Desk(folder / "trader")
        desk.run(12)
        desk.ask("reset", "KILL_SWITCH")  # left for a beat that never comes
        desk.stop()
    (folder / "trader.yaml").write_text(f"state_dir: {folder / 'trader'}\nregistry: {folder / 'learn'}\n")
    return folder


def kept(folder) -> list[Entry]:
    places, journal = learning_places(folder / "trader.yaml", folder / "learn.yaml")
    return manifest(folder / "none", folder / "none", folder / "none", trader=folder / "trader", learn=places,
                    lineup=journal)  # fmt: skip


def test_what_the_trader_the_lineup_and_training_cannot_make_again_is_on_the_list(lived):
    names = {entry.name for entry in kept(lived)}
    (day,) = (lived / "trader" / "days").iterdir()
    assert {f"trader/{name}.jsonl" for name in STATE} <= names, "the outbox, both books' journals, latches, stages"
    assert {"trader/outbox.jsonl", "trader/shadow.jsonl", "trader/stages.jsonl", "trader/lineup.jsonl"} <= names
    assert f"trader/days/{day.name}/decisions.jsonl" in names
    assert (lived / "trader" / resume.FILE).is_file(), "the trader that was stopped saved its sleeves' states"
    assert f"trader/{resume.FILE}" in names, "and that file is on the list, by name and not by luck"
    (model,) = (lived / "learn" / "models").glob("*.json")
    (report,) = (lived / "learn" / "reports").glob("*.json")
    assert {f"learn/models/{model.name}", f"learn/reports/{report.name}", "learn/trials.jsonl"} <= names
    assert "learn/last-run.json" in names
    champion_or_candidate = lineup.read(lived / "trader" / lineup.FILE).candidates[0]
    assert (champion_or_candidate.model, champion_or_candidate.report) == (model.stem, report.stem)


def test_every_file_those_commands_wrote_is_backed_up_or_left_out_by_a_rule_that_says_why(lived):
    backed_up = {entry.path for entry in kept(lived)}
    rules = set()
    for where in ("trader", "learn"):
        for path in sorted(p for p in (lived / where).rglob("*") if p.is_file()):
            rule = left_out(where, path.relative_to(lived / where).as_posix())
            assert (rule is None) == (path in backed_up), f"{path} is neither backed up nor left out by a rule"
            rules |= {rule.pattern} if rule else set()
    assert rules == {"days/*/session-*", "status.json", "trader.lock", "requests/*"}, "what a real run leaves out"
    assert all(len(rule.why) > 40 for rule in LEFT_OUT), "every rule gives its reason"
    not_rebuilt = [rule.pattern for rule in LEFT_OUT if not rule.why.startswith("rebuilt: ")]
    assert not_rebuilt == ["days/*/session-*"], "the tapes alone are left out without being rebuilt, and say so"


def test_a_file_nobody_listed_is_backed_up_and_a_half_written_one_is_not(lived, tmp_path):
    for where in ("trader", "learn"):
        (tmp_path / where / "deep").mkdir(parents=True)
        (tmp_path / where / "deep" / "new-kind.jsonl").write_text("{}\n")
        (tmp_path / where / ".0a.partial").write_text("half")
    names = [e.name for e in manifest(tmp_path, tmp_path, tmp_path, trader=tmp_path / "trader",
                                      learn=[("learn", tmp_path / "learn")])]  # fmt: skip
    assert names == ["trader/deep/new-kind.jsonl", "learn/deep/new-kind.jsonl"]


def test_a_backup_of_those_folders_reads_back_whole_and_its_journals_still_verify(lived, tmp_path):
    folder = to_directory(kept(lived), tmp_path, host.BACKUP_TAG, STAMP)
    assert verify(folder) == []
    for name in (*STATE, "lineup"):
        assert verify_journal(folder / "trader" / f"{name}.jsonl") == verify_journal(lived / "trader" / f"{name}.jsonl")
    assert lineup.read(folder / "trader" / lineup.FILE) == lineup.read(lived / "trader" / lineup.FILE)
    assert not list(folder.rglob("session-*")) and not list(folder.rglob("status.json"))


def test_the_shipped_settings_put_the_models_in_one_folder_and_the_lineup_in_the_traders(tmp_path):
    places, journal = learning_places(ROOT / "config" / "trader.yaml", ROOT / "config" / "learn.yaml")
    assert [(name, str(folder)) for name, folder in places] == [("learn", "var/learn")]
    assert str(journal) == "var/trader/lineup.jsonl"
    (tmp_path / "t.yaml").write_text("registry: elsewhere/models\n")
    apart = tmp_path / "apart" / "lineup.jsonl"
    (tmp_path / "l.yaml").write_text(f"state_dir: {tmp_path / 'learn'}\nlineup: {apart}\n")
    places, journal = learning_places(tmp_path / "t.yaml", tmp_path / "l.yaml")
    assert [name for name, _ in places] == ["learn", "registry"], "two folders: both are taken"
    lineup.append(journal, lineup.Lineup(), action=lineup.KEEP, at_ns=1)
    names = [e.name for e in manifest(tmp_path, tmp_path, tmp_path, trader=tmp_path / "trader", lineup=journal)]
    assert names == ["lineup/lineup.jsonl"], "a lineup journal kept outside the trader's folder is listed by itself"
    assert learning_places(tmp_path / "none.yaml", tmp_path / "none.yaml") == ([], None)


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


def test_the_command_takes_the_traders_and_the_learning_folders_from_their_settings(lived, configured, capsys):
    options = ["--trader-config", str(lived / "trader.yaml"), "--learn-config", str(lived / "learn.yaml")]
    assert backup.main(["list", *configured, *options]) == 0
    listed_names = [line.split("\t")[0] for line in capsys.readouterr().out.splitlines()]
    assert listed_names == [entry.name for entry in kept(lived)]
    assert "trader/lineup.jsonl" in listed_names and "learn/trials.jsonl" in listed_names


def test_the_shipped_settings_name_no_tool_and_a_malformed_one_is_refused(tmp_path):
    assert backup.load_tool(ROOT / "config" / "ops.yaml") is None
    (tmp_path / "ops.yaml").write_text("backup:\n  tool: restic backup\n")
    with pytest.raises(BackupError, match="a command as a list of words"):
        backup.load_tool(tmp_path / "ops.yaml")
    (tmp_path / "ops.yaml").write_text("backup:\n  tool: [restic, backup, --tag, '{tag}', --files-from, '{list}']\n")
    assert backup.load_tool(tmp_path / "ops.yaml") == RESTIC
