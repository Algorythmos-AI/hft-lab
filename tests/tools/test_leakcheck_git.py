"""The leak check against real (temporary) git repositories: what is scanned, history, and the attestation."""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from tools import leakcheck as lc

PDF = b"%PDF-1.7\n1 0 obj\n"
ATTEST = lc.ATTEST_PATH


def run(repo, *args) -> int:
    return lc.main(["--repo", str(repo.root), *args])


def attested(repo) -> dict:
    return json.loads((repo.root / ATTEST).read_text())


def attest_and_commit(repo, message="attested") -> str:
    assert run(repo, "--write-attest") == 0
    repo.git("add", ATTEST)
    return repo.commit(message)


# ---- what is scanned ------------------------------------------------------------------------------------------


def test_a_clean_staged_tree_passes(repo, capsys):
    repo.add("README.md", "hello\n")
    repo.add("src/a.py", "x = 1\n")
    assert run(repo) == 0
    assert "leakcheck: ok (2 files, terms off, corpus off)" in capsys.readouterr().out


def test_a_staged_denied_file_fails_and_is_named(repo, capsys):
    repo.add("README.md")
    repo.add("docs/paper.pdf", PDF)
    assert run(repo) == 1
    err = capsys.readouterr().err
    assert "extension: docs/paper.pdf: .pdf files are denied" in err
    assert "magic: docs/paper.pdf: content is pdf" in err
    assert "FAILED, 2 finding(s)" in err


def test_only_the_tracked_tree_is_scanned(repo):
    repo.add("README.md")
    repo.write("scratch/untracked.pdf", PDF)
    assert run(repo) == 0


def test_the_staged_content_is_scanned_not_the_working_copy(repo):
    repo.add("notes.txt", PDF)
    repo.write("notes.txt", "cleaned up, but not staged\n")
    assert run(repo) == 1


def test_a_commit_can_be_scanned_by_revision(repo):
    repo.add("docs/paper.pdf", PDF)
    bad = repo.commit("bad")
    repo.git("rm", "-q", "docs/paper.pdf")
    repo.add("README.md")
    good = repo.commit("good")
    assert run(repo, "--rev", bad) == 1
    assert run(repo, "--rev", good) == 0


def test_an_unknown_revision_cannot_run(repo):
    repo.add("README.md")
    repo.commit()
    assert run(repo, "--rev", "no-such-branch") == 2


def test_an_empty_repository_passes(repo):
    assert run(repo) == 0
    assert run(repo, "--all-history") == 0


def test_an_unmerged_index_cannot_be_scanned(repo):
    repo.add("a.txt", "base\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "other")
    repo.add("a.txt", "theirs\n")
    repo.commit("theirs")
    repo.git("checkout", "-q", "main")
    repo.add("a.txt", "ours\n")
    repo.commit("ours")
    assert repo.git("merge", "other", check=False).returncode != 0
    assert run(repo) == 2


def test_a_path_that_is_not_valid_text_is_still_judged_and_reported(repo, capsys):
    """Such a name cannot be made on every file system, so the index entry is written directly."""
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo.root, input=PDF, capture_output=True, check=True
    ).stdout.strip()
    entry = b"100644," + blob + b",docs/caf\xe9.pdf"
    subprocess.run([b"git", b"update-index", b"--add", b"--cacheinfo", entry], cwd=repo.root, check=True)
    assert run(repo) == 1
    err = capsys.readouterr().err
    assert "extension: docs/caf" in err and "magic: docs/caf" in err


def test_outside_a_repository_it_cannot_run(tmp_path):
    assert lc.main(["--repo", str(tmp_path)]) == 2


# ---- history --------------------------------------------------------------------------------------------------


def test_a_file_deleted_long_ago_is_still_found_in_history(repo, capsys):
    repo.add("data/ticks.parquet", b"PAR1" + bytes(40) + b"PAR1")
    repo.commit("add data")
    repo.git("rm", "-q", "data/ticks.parquet")
    repo.add("README.md")
    repo.commit("remove data")
    assert run(repo) == 0
    capsys.readouterr()
    assert run(repo, "--all-history") == 1
    err = capsys.readouterr().err
    assert "extension: data/ticks.parquet (blob " in err and "magic: data/ticks.parquet (blob " in err


def test_history_on_another_branch_is_scanned_too(repo):
    repo.add("README.md")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "side")
    repo.add("model.pkl", b"\x80\x04N.")
    repo.commit("side")
    repo.git("checkout", "-q", "main")
    assert run(repo) == 0
    assert run(repo, "--all-history") == 1


def test_a_clean_history_passes(repo, capsys):
    repo.add("README.md", "one\n")
    repo.commit("one")
    repo.add("README.md", "two\n")
    repo.commit("two")
    assert run(repo, "--all-history") == 0
    assert "1 more path/blob pairs in history" in capsys.readouterr().out


def test_history_judges_each_path_a_blob_ever_had(repo):
    repo.add("notes.txt", "same content\n")
    repo.commit("as text")
    repo.git("mv", "notes.txt", "notes.zip")
    repo.commit("renamed")
    repo.git("mv", "notes.zip", "notes.md")
    repo.commit("renamed back")
    assert run(repo) == 0
    assert run(repo, "--all-history") == 1


# ---- allow-list, terms and corpus through the command line -------------------------------------------------------


def test_the_repository_allow_list_is_honoured(repo):
    logo = b"\x89PNG\r\n\x1a\n" + bytes(20)
    repo.add("docs/logo.png", logo)
    assert run(repo) == 1
    repo.add(lc.ALLOW_PATH, f"{hashlib.sha256(logo).hexdigest()}  docs/logo.png\n")
    assert run(repo) == 0
    repo.add("docs/logo.png", logo + b"changed")
    assert run(repo) == 1


def test_a_malformed_allow_list_cannot_run(repo):
    repo.add(lc.ALLOW_PATH, "docs/logo.png\n")
    assert run(repo) == 2


def test_terms_from_the_flag_and_from_the_environment(repo, tmp_path, monkeypatch, capsys):
    terms = tmp_path / "terms.txt"
    terms.write_text("nightjar\n")
    repo.add("docs/plan.md", "The Nightjar plan.\n")
    assert run(repo) == 0
    assert run(repo, "--terms", str(terms)) == 1
    monkeypatch.setenv(lc.TERMS_ENV, str(terms))
    assert run(repo) == 1
    repo.add("docs/plan.md", "The plan.\n")
    capsys.readouterr()
    assert run(repo) == 0
    assert "terms on, corpus off" in capsys.readouterr().out


def test_corpus_from_the_flag_and_from_the_environment(repo, tmp_path, monkeypatch, capsys):
    source = tmp_path / "private.txt"
    source.write_text("one two three four five six seven eight nine ten")
    index = tmp_path / "index.txt"
    assert lc.main(["--repo", str(repo.root), "--build-corpus", str(index), str(source)]) == 0
    repo.add("docs/plan.md", "Copied: two three four five six seven eight nine.\n")
    assert run(repo) == 0
    assert run(repo, "--corpus", str(index)) == 1
    monkeypatch.setenv(lc.CORPUS_ENV, str(index))
    assert run(repo) == 1
    repo.add("docs/plan.md", "Our own words about the same idea, nothing copied at all.\n")
    capsys.readouterr()
    assert run(repo) == 0
    assert "terms off, corpus on" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--terms", "--corpus"])
def test_a_named_private_file_that_is_missing_cannot_run(repo, tmp_path, flag):
    repo.add("README.md")
    assert run(repo, flag, str(tmp_path / "absent.txt")) == 2


@pytest.mark.parametrize("name", [lc.TERMS_ENV, lc.CORPUS_ENV])
def test_an_environment_variable_pointing_nowhere_cannot_run(repo, tmp_path, monkeypatch, name):
    repo.add("README.md")
    monkeypatch.setenv(name, str(tmp_path / "absent.txt"))
    assert run(repo) == 2


def test_the_index_is_never_written_inside_the_repository(repo, tmp_path):
    source = tmp_path / "private.txt"
    source.write_text("one two three four five six seven eight nine ten")
    assert run(repo, "--build-corpus", str(repo.root / "index.txt"), str(source)) == 2
    assert not (repo.root / "index.txt").exists()
    assert run(repo, "--build-corpus", str(tmp_path / "index.txt")) == 2  # no inputs
    assert run(repo, "stray-argument") == 2


# ---- attestation ----------------------------------------------------------------------------------------------


def test_write_attest_records_the_tree_without_the_attestation_file(repo):
    repo.add("README.md", "hello\n")
    assert run(repo, "--write-attest") == 0
    doc = attested(repo)
    assert doc == {
        "schema": 1,
        "tree": repo.git("write-tree").stdout.strip(),
        "result": "pass",
        "files": 1,
        "terms_checked": False,
        "corpus_checked": False,
    }


def test_committing_the_attestation_does_not_change_what_it_attests(repo, capsys):
    repo.add("README.md", "hello\n")
    before = repo.git("write-tree").stdout.strip()
    head = attest_and_commit(repo)
    assert attested(repo)["tree"] == before != repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
    assert lc.Repo(repo.root).attested_tree(f"{head}^{{tree}}") == before
    capsys.readouterr()
    assert run(repo, "--verify-attest") == 0
    out = capsys.readouterr().out
    assert f"attestation is current for tree {before[:12]}" in out
    assert "files=1, terms_checked=false, corpus_checked=false" in out


def test_rewriting_the_attestation_is_idempotent(repo):
    repo.add("README.md", "hello\n")
    attest_and_commit(repo)
    first = (repo.root / ATTEST).read_bytes()
    assert run(repo, "--write-attest") == 0
    assert (repo.root / ATTEST).read_bytes() == first
    assert repo.git("status", "--porcelain").stdout == ""


def test_the_attestation_says_which_private_rules_ran(repo, tmp_path, monkeypatch, capsys):
    source = tmp_path / "private.txt"
    source.write_text("one two three four five six seven eight nine ten")
    index = tmp_path / "index.txt"
    lc.build_corpus(index, [source])
    terms = tmp_path / "terms.txt"
    terms.write_text("nightjar\n")
    repo.add("README.md", "hello\n")
    monkeypatch.setenv(lc.CORPUS_ENV, str(index))
    monkeypatch.setenv(lc.TERMS_ENV, str(terms))
    attest_and_commit(repo)
    assert attested(repo)["corpus_checked"] is True and attested(repo)["terms_checked"] is True
    monkeypatch.delenv(lc.CORPUS_ENV)
    monkeypatch.delenv(lc.TERMS_ENV)
    capsys.readouterr()
    assert run(repo, "--verify-attest") == 0  # CI has neither file: it reports the flags, it does not need them
    assert "terms_checked=true, corpus_checked=true" in capsys.readouterr().out


def test_nothing_is_attested_when_the_scan_fails(repo):
    repo.add("docs/paper.pdf", PDF)
    assert run(repo, "--write-attest") == 1
    assert not (repo.root / ATTEST).exists()


def test_write_attest_is_for_the_staged_tree_only(repo):
    repo.add("README.md")
    repo.commit()
    assert run(repo, "--write-attest", "--rev", "HEAD") == 2
    assert run(repo, "--write-attest", "--all-history") == 2


def test_a_missing_attestation_fails_verification(repo, capsys):
    repo.add("README.md")
    repo.commit()
    assert run(repo, "--verify-attest") == 1
    assert "is missing from HEAD" in capsys.readouterr().err


def test_a_stale_attestation_fails_verification(repo, capsys):
    repo.add("README.md", "hello\n")
    first = attest_and_commit(repo)
    repo.add("README.md", "changed after the scan\n")
    repo.commit("not re-attested")
    assert run(repo, "--verify-attest") == 1
    assert "is stale" in capsys.readouterr().err
    assert run(repo, "--verify-attest", "--rev", first) == 0


def test_an_attestation_copied_from_another_tree_fails_verification(repo):
    repo.add("README.md", "hello\n")
    attest_and_commit(repo)
    stolen = (repo.root / ATTEST).read_bytes()
    repo.add("extra.txt", "more\n")
    repo.add(ATTEST, stolen)
    repo.commit("carries the old attestation")
    assert run(repo, "--verify-attest") == 1


def tamper(doc: dict, how: str) -> bytes:
    canonical = lambda d: (json.dumps(d, indent=2, sort_keys=True) + "\n").encode()  # noqa: E731
    if how == "extra key":
        return canonical({**doc, "note": "anything could hide here"})
    if how == "missing key":
        return canonical({k: v for k, v in doc.items() if k != "corpus_checked"})
    if how == "failed result":
        return canonical({**doc, "result": "fail"})
    if how == "other schema":
        return canonical({**doc, "schema": 2})
    if how == "count as text":
        return canonical({**doc, "files": "1"})
    if how == "flag as number":
        return canonical({**doc, "corpus_checked": 1})
    if how == "not canonical":
        return json.dumps(doc).encode()
    if how == "trailing text":
        return canonical(doc) + b"# appended\n"
    if how == "not json":
        return b"pass\n"
    if how == "not an object":
        return b"[]\n"
    raise AssertionError(how)


@pytest.mark.parametrize(
    "how",
    [
        "extra key", "missing key", "failed result", "other schema", "count as text", "flag as number",
        "not canonical", "trailing text", "not json", "not an object",
    ],
)  # fmt: skip
def test_a_tampered_attestation_fails_verification(repo, how):
    repo.add("README.md", "hello\n")
    assert run(repo, "--write-attest") == 0
    good = attested(repo)
    repo.add(ATTEST, tamper(good, how))
    repo.commit(how)
    assert good["tree"] == lc.Repo(repo.root).attested_tree("HEAD^{tree}")  # only the file's form is wrong
    assert run(repo, "--verify-attest") == 1


def test_an_attestation_that_is_a_symlink_does_not_count(repo):
    repo.add("README.md", "hello\n")
    assert run(repo, "--write-attest") == 0
    (repo.root / "elsewhere.json").write_bytes((repo.root / ATTEST).read_bytes())
    (repo.root / ATTEST).unlink()
    (repo.root / ATTEST).symlink_to("elsewhere.json")
    repo.git("add", "-A")
    repo.commit("symlinked")
    assert run(repo, "--verify-attest") == 1


def test_verification_needs_a_commit(repo):
    assert run(repo, "--verify-attest") == 2


def test_the_attested_hash_ignores_only_the_attestation_file(repo):
    repo.add("README.md", "hello\n")
    repo.add("docs/.leakcheck-attest.json", "a file of the same name elsewhere is ordinary content\n")
    one = lc.Repo(repo.root).attested_tree(repo.git("write-tree").stdout.strip())
    repo.add("docs/.leakcheck-attest.json", "changed\n")
    two = lc.Repo(repo.root).attested_tree(repo.git("write-tree").stdout.strip())
    assert one != two
