"""The research-registry lint: ids, locations, uniqueness, append-only experiments, cross-repository references."""
from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.registry import __main__ as cli
from hftlab.registry import records
from hftlab.registry.trials import TrialRow
from tests.support import ROOT

HYP = "research/hypotheses/HFT-HYP-0001-control-cost-floor.yaml"
EXP = "research/experiments/HFT-EXP-0001-control-shadow"
DEC = "research/decisions/HFT-DEC-0001-access-route.md"
CLEAN = {
    "README.md": "r",
    "research/README.md": "r",
    "research/hypotheses/.gitkeep": "",
    HYP: "h1",
    f"{EXP}/config.yaml": "c1",
    f"{EXP}/results.json": "r1",
    f"{EXP}/report.md": "p1",
    DEC: "d1",
}


def rules(findings) -> list[str]:
    return [f.rule for f in findings]


# ---- id and location -----------------------------------------------------------------------------------------------


def test_a_well_formed_registry_is_clean():
    assert records.lint(CLEAN) == []
    assert records.records(CLEAN) == {
        "HFT-HYP-0001": {"hypotheses/HFT-HYP-0001-control-cost-floor.yaml"},
        "HFT-EXP-0001": {"experiments/HFT-EXP-0001-control-shadow"},
        "HFT-DEC-0001": {"decisions/HFT-DEC-0001-access-route.md"},
    }


def test_the_id_pattern_is_the_agreed_one():
    assert records.ID.pattern == r"HFT-(HYP|EXP|DEC)-\d{4}"
    assert records.ID.fullmatch("HFT-HYP-0001") and records.ID.fullmatch("HFT-DEC-9999")
    assert not records.ID.fullmatch("HFT-HYP-001") and not records.ID.fullmatch("HYP-0001")


@pytest.mark.parametrize(
    "name",
    ["HFT-HYP-001-short.yaml", "HFT-HYP-00001-long.yaml", "hft-hyp-0001-lower.yaml", "HYP-0001-no-prefix.yaml",
     "HFT-XYZ-0001-kind.yaml", "HFT-HYP-0001_underscore.yaml", "HFT-HYP-0001-Upper.yaml", "HFT-HYP-0001-.yaml",
     "notes.md", "HFT-HYP-0001 copy.yaml", "CRY-HYP-0001-other-desk.yaml", "HFT-HYP-000A-hex.yaml"],
)  # fmt: skip
def test_a_record_with_a_malformed_name_is_flagged(name):
    assert rules(records.lint({**CLEAN, f"research/hypotheses/{name}": "x"})) == ["id"]


@pytest.mark.parametrize(
    "path", ["research/hypotheses/HFT-HYP-0002.yaml", "research/hypotheses/HFT-HYP-0002-a-b-c.md",
             "research/experiments/HFT-EXP-0002/results.json", "research/experiments/HFT-EXP-0002-x/deep/er/file.csv",
             "research/decisions/HFT-DEC-0002-two-words.md"],
)  # fmt: skip
def test_the_accepted_name_shapes(path):
    assert records.lint({**CLEAN, path: "x"}) == []


@pytest.mark.parametrize(
    "path",
    ["research/hypotheses/HFT-DEC-0002-wrong-folder.md", "research/decisions/HFT-EXP-0002-wrong-folder/report.md",
     "research/experiments/HFT-HYP-0002-wrong-folder.yaml"],
)  # fmt: skip
def test_a_record_in_the_folder_of_another_kind_is_flagged(path):
    assert rules(records.lint({**CLEAN, path: "x"})) == ["location"]


@pytest.mark.parametrize(
    "path",
    ["research/HFT-HYP-0002-top-level.yaml", "docs/HFT-DEC-0002-notes.md", "HFT-EXP-0002/results.json",
     "research/forward/HFT-EXP-0002-run/report.md", "research/hypotheses/drafts/HFT-HYP-0002-nested.yaml",
     "docs/hyp-0002-draft.md", "src/hftlab/DEC-0003.py"],
)  # fmt: skip
def test_a_record_outside_the_three_folders_is_flagged(path):
    found = records.lint({**CLEAN, path: "x"})
    assert "location" in rules(found) or rules(found) == ["id"], found


def test_files_that_are_not_records_are_left_alone():
    extra = [
        "research/hypotheses/README.md",
        "research/decisions/.gitkeep",
        "docs/adr/0001-paper-only-lock.md",
        "src/hftlab/registry/records.py",
        "docs/decisions.md",
        "research/notes/experiment-ideas.md",
    ]
    assert records.lint({**CLEAN, **dict.fromkeys(extra, "x")}) == []


# ---- uniqueness ----------------------------------------------------------------------------------------------------


def test_two_records_with_one_id_are_flagged():
    found = records.lint({**CLEAN, "research/hypotheses/HFT-HYP-0001-second-claim.yaml": "x"})
    assert rules(found) == ["unique"] and "HFT-HYP-0001" in found[0].message


def test_the_files_of_one_experiment_share_its_id_without_a_clash():
    assert records.lint({**CLEAN, f"{EXP}/plots/equity.csv": "x"}) == []


def test_the_same_number_in_different_kinds_is_not_a_clash():
    assert "HFT-HYP-0001" in records.records(CLEAN) and "HFT-DEC-0001" in records.records(CLEAN)
    assert records.lint(CLEAN) == []


def test_an_experiment_as_a_file_and_as_a_folder_with_one_id_clash():
    found = records.lint({**CLEAN, "research/experiments/HFT-EXP-0001-rerun.yaml": "x"})
    assert rules(found) == ["unique"]


# ---- append-only experiments ---------------------------------------------------------------------------------------


def test_an_unchanged_registry_with_a_new_experiment_is_clean():
    head = {**CLEAN, "research/experiments/HFT-EXP-0002-rerun/results.json": "new", HYP: "edited hypothesis"}
    assert records.lint(head, base=CLEAN) == []


def test_a_modified_experiment_is_flagged():
    head = {**CLEAN, f"{EXP}/results.json": "better numbers"}
    found = records.lint(head, base=CLEAN)
    assert rules(found) == ["append-only"] and found[0].path == f"{EXP}/results.json"
    assert "modified" in found[0].message


def test_a_deleted_experiment_file_is_flagged():
    head = {k: v for k, v in CLEAN.items() if k != f"{EXP}/report.md"}
    found = records.lint(head, base=CLEAN)
    assert rules(found) == ["append-only"] and "deleted or renamed" in found[0].message


def test_a_renamed_experiment_is_flagged_file_by_file():
    head = {k.replace("HFT-EXP-0001-control-shadow", "HFT-EXP-0001-renamed"): v for k, v in CLEAN.items()}
    assert rules(records.lint(head, base=CLEAN)) == ["append-only"] * 3


def test_a_file_added_to_an_existing_experiment_is_flagged():
    head = {**CLEAN, f"{EXP}/addendum.md": "one more thing"}
    found = records.lint(head, base=CLEAN)
    assert rules(found) == ["append-only"] and "added to an experiment that already exists" in found[0].message


def test_without_a_base_the_append_only_rule_is_not_checked():
    assert records.lint({**CLEAN, f"{EXP}/results.json": "better numbers"}) == []


def test_only_experiments_are_frozen():
    head = {**CLEAN, HYP: "edited", DEC: "status: accepted"}
    assert records.lint(head, base=CLEAN) == []


experiment_files = st.dictionaries(
    st.sampled_from(["config.yaml", "results.json", "report.md", "plots/a.csv", "plots/b.csv"]),
    st.text("abcdef0123456789", min_size=1, max_size=8),
    min_size=1,
)


@given(experiment_files, st.data())
def test_any_single_edit_to_an_existing_experiment_is_caught(files, data):
    base = {f"{EXP}/{name}": content for name, content in files.items()}
    assert records.lint_append_only(base, dict(base)) == []
    victim = data.draw(st.sampled_from(sorted(base)))
    edited = {**base, victim: base[victim] + "!"}
    removed = {k: v for k, v in base.items() if k != victim}
    assert [f.path for f in records.lint_append_only(base, edited)] == [victim]
    assert [f.path for f in records.lint_append_only(base, removed)] == [victim]


# ---- cross-repository references -----------------------------------------------------------------------------------


def references(text: str) -> list[str]:
    return [f.message for f in records.lint_references({DEC: text})]


@pytest.mark.parametrize(
    "text",
    ["Builds on HFT-HYP-0001 and HFT-EXP-0001.", "See options-lab:DEC-0011 for the method.",
     "Follows crypto-lab:CRY-DEC-0003, crypto-lab:CRY-EXP-0020 and hft-lab:HFT-DEC-0001.",
     "No references at all.", "The code DECODE-0001 and the word EXPERT-2024 are not ids."],
)  # fmt: skip
def test_local_ids_and_qualified_foreign_ids_pass(text):
    assert references(text) == []


@pytest.mark.parametrize(
    ("text", "bare"),
    [("Same method as DEC-0011.", "DEC-0011"), ("Compare EXP-0007 (liquid dev).", "EXP-0007"),
     ("As in CRY-DEC-0003.", "CRY-DEC-0003"), ("HYP-0020,EXP-0021", "HYP-0020"),
     ("see research/decisions/DEC-0011-method.md", "DEC-0011")],
)  # fmt: skip
def test_a_bare_id_of_another_repository_is_flagged(text, bare):
    messages = references(text)
    assert messages and f"{bare} is another repository's id; write it as repo:{bare}" in messages[0]


def test_a_reference_finding_gives_the_line():
    found = records.lint_references({DEC: "line one\nline two cites EXP-0007\n"})
    assert found[0].path == DEC and found[0].message.startswith("line 2: ")


def test_references_are_part_of_the_whole_lint():
    assert rules(records.lint(CLEAN, texts={DEC: "As in DEC-0011."})) == ["reference"]


# ---- trial registry rows -------------------------------------------------------------------------------------------


def test_a_trial_row_must_name_a_decision_that_exists():
    assert records.lint(CLEAN, trials=[TrialRow("HFT-DEC-0001", 12, "accepted")]) == []
    found = records.lint(CLEAN, trials=[TrialRow("HFT-DEC-0007", 12, "accepted")])
    assert rules(found) == ["trials"] and "HFT-DEC-0007" in found[0].message


# ---- tree states and the command -----------------------------------------------------------------------------------


def populate(repo) -> None:
    for path, content in CLEAN.items():
        repo.add(path, content or "\n")
    repo.add("config/trial_registry.yaml", "history: []\n")


def test_a_working_tree_and_its_commit_give_the_same_state(repo):
    populate(repo)
    repo.write("var/runtime.json", "{}")
    repo.write(".venv/lib/x.py", "x")
    head = repo.commit("records")
    assert records.worktree_state(repo.root) == records.commit_state(repo.root, head)
    assert records.blob_hash(b"h1") == repo.git("rev-parse", f"HEAD:{HYP}").stdout.strip()


def test_the_command_passes_a_clean_registry(repo, capsys):
    populate(repo)
    assert cli.main(["lint", "--root", str(repo.root)]) == 0
    assert "registry lint: ok (3 records)" in capsys.readouterr().out


def test_the_command_fails_on_a_finding_and_names_it(repo, capsys):
    populate(repo)
    repo.write("research/hypotheses/HFT-HYP-0001-second-claim.yaml", "x")
    repo.write(DEC, "Same method as DEC-0011.\n")
    assert cli.main(["lint", "--root", str(repo.root)]) == 1
    err = capsys.readouterr().err
    assert "registry lint: unique: " in err and "registry lint: reference: " in err and "FAILED, 2 finding(s)" in err


def test_the_command_compares_experiments_with_a_base_commit(repo, capsys):
    populate(repo)
    base = repo.commit("records")
    assert cli.main(["lint", "--root", str(repo.root), "--base", base]) == 0
    repo.write(f"{EXP}/results.json", "better numbers")
    assert cli.main(["lint", "--root", str(repo.root)]) == 0
    assert cli.main(["lint", "--root", str(repo.root), "--base", base]) == 1
    assert "append-only" in capsys.readouterr().err


def test_the_command_checks_trial_rows_against_the_decisions(repo, capsys):
    populate(repo)
    repo.write("config/trial_registry.yaml", "history:\n  - {decision: HFT-DEC-0009, total: 4, status: accepted}\n")
    assert cli.main(["lint", "--root", str(repo.root)]) == 1
    assert "trials: config/trial_registry.yaml: HFT-DEC-0009" in capsys.readouterr().err


@pytest.mark.parametrize("registry", ["history: [1]\n", "nothing: here\n", "history: [\n"])
def test_the_command_cannot_run_on_a_broken_trial_registry(repo, registry):
    populate(repo)
    repo.write("config/trial_registry.yaml", registry)
    assert cli.main(["lint", "--root", str(repo.root)]) == 2


def test_the_command_cannot_run_against_an_unknown_base(repo):
    populate(repo)
    repo.commit("records")
    assert cli.main(["lint", "--root", str(repo.root), "--base", "no-such-branch"]) == 2


def test_this_repository_passes_its_own_lint():
    head = records.worktree_state(ROOT)
    assert records.lint(head, texts=records.record_texts(ROOT, head)) == []
    for folder in ("hypotheses", "experiments", "decisions"):
        assert (ROOT / "research" / folder).is_dir()
