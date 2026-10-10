"""The governance files say what was agreed, and no tracked file breaks the rules of a public repository."""
from __future__ import annotations

import re
import subprocess

import pytest
import yaml

from tests.support import ROOT
from tools import commitmsg

DESCRIPTION = (
    "HFT Lab: high-frequency paper trading for the currency market — recorder, strategy sleeves, risk gate, "
    "weekly retraining (Python)"
)
LOOPS = ["Record", "Decide", "Guard", "Trade", "Account", "Learn", "Promote", "Report"]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def tracked() -> list[str]:
    """Tracked files, plus untracked ones that are not ignored: what the next commit could contain."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, check=True,
    )  # fmt: skip
    return sorted(p for p in listing.stdout.decode().split("\x00") if p and (ROOT / p).is_file())


def test_the_readme_opens_with_the_agreed_description_and_the_header_table():
    text = read("README.md")
    assert DESCRIPTION.startswith("HFT Lab") and DESCRIPTION in text
    for row in ("**Status**", "**Owner**", "**Runs at**", "**Run locally**", "**Context**"):
        assert f"| {row}" in text, row
    assert "`make bootstrap && make ci`" in text
    assert "@Algorythmos-AI/maintainers" in text


def test_the_readme_lists_the_eight_weekly_loops_in_order():
    rows = re.findall(r"^\| \*\*(\w+)\*\* \| [^|]+ \| ", read("README.md"), re.MULTILINE)
    assert [r for r in rows if r in LOOPS] == LOOPS


def test_the_readme_says_paper_only_and_promises_no_profit():
    text = read("README.md")
    assert "Paper trading only. No promise of profit." in text
    assert "financial advice" in text


def test_the_licence_is_proprietary_and_names_the_company():
    text = read("LICENSE")
    assert text.startswith("HFT Lab — Proprietary Software License\n")
    assert "Algorythmos Pty Ltd" in text and "ACN 701 006 626" in text and "ALL RIGHTS RESERVED." in text
    assert "NOTICE" not in text, "this repository has no third-party course material and no NOTICE file"


def test_the_pointer_file_holds_only_the_pointer():
    pointers = [p for p in ROOT.glob("*.md") if p.read_text() == "@AGENTS.md\n"]
    assert len(pointers) == 1


def test_codeowners_names_the_maintainers_team_for_everything():
    lines = [line for line in read(".github/CODEOWNERS").splitlines() if line and not line.startswith("#")]
    assert lines == ["* @Algorythmos-AI/maintainers"]


def test_dependabot_watches_the_lock_files_and_the_actions():
    config = yaml.safe_load(read(".github/dependabot.yml"))
    assert config["version"] == 2
    assert {u["package-ecosystem"] for u in config["updates"]} == {"pip", "github-actions"}


@pytest.mark.parametrize(
    "path", [".venv/x", "var/state.json", "data/ticks.csv", "logs/run.log", "models/m.bin", "a/b/ticks.parquet", ".env",
             "KILL", "FLATTEN", "src/__pycache__/x.pyc", ".pytest_cache/x", ".mypy_cache/x", ".ruff_cache/x"],
)  # fmt: skip
def test_runtime_and_private_paths_are_git_ignored(path):
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
    assert result.returncode == 0, f"{path} is not ignored"


@pytest.mark.parametrize("path", ["src/hftlab/models/registry.py", "src/hftlab/data.py", "docs/var.md", ".env.example"])
def test_source_paths_that_resemble_them_are_not_ignored(path):
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
    assert result.returncode == 1, f"{path} would be ignored"


def test_agents_md_states_every_hard_rule():
    text = read("AGENTS.md")
    for needle in (
        "**Paper only.**", "`MODE` is exactly `paper`", "`LIVE_TRADING_ENABLED`", "4002", "7497", "`DU` and digits",
        "One platform per pull request", "no imports from other platforms", "**Pre-registration.**",
        "`HFT-HYP-NNNN`", "`HFT-EXP-NNNN`", "`HFT-DEC-NNNN`", "append-only", "`repo:ID`", "## Data classes",
        "Market data", "recordings", "model files", "knowledge base", "credentials", "No `Co-Authored-By` trailer",
        "D-016", "skalaliya <skalaliya@gmail.com>", "docs/never-build.md", "Algorythmos-AI/.github-private",
        "docs/trading-platforms.md",
    ):  # fmt: skip
        assert needle in text, needle


def test_the_design_records_exist_in_order():
    records = sorted(p.name for p in (ROOT / "docs" / "adr").glob("*.md"))
    topics = ["paper-only", "integer", "two-books", "recorder", "replay", "leak", "host", "learning"]
    topics += ["venue-port", "access-route", "synchronous", "recording-format", "cost-model", "sleeves"]
    assert [name[:4] for name in records] == [f"{n:04d}" for n in range(1, len(topics) + 1)]
    for name, topic in zip(records, topics, strict=True):
        text = read(f"docs/adr/{name}")
        assert topic in name and text.startswith(f"# ADR {name[:4]}: "), name
        for section in ("- **Status:** accepted", "## Context", "## Decision", "## Consequences"):
            assert section in text, f"{name} has no {section!r}"


def test_every_design_record_that_is_cited_anywhere_exists():
    """A docstring that sends the reader to a design record sends them to one that is there."""
    written = {path.name[:4] for path in (ROOT / "docs" / "adr").glob("*.md")}
    cited: dict[str, set[str]] = {}
    for path in tracked():
        text = (ROOT / path).read_bytes().decode("utf-8", errors="replace")
        for number in re.findall(r"\bADR[ -]?([0-9]{4})\b", text) + re.findall(r"\badr/([0-9]{4})-", text):
            cited.setdefault(number, set()).add(path)
    in_code = {number for number, paths in cited.items() if any(p.startswith(("src/", "tests/")) for p in paths)}
    assert {"0001", "0005", "0012", "0013", "0014"} <= in_code, "the scan reads the source, where the citations are"
    assert {number: sorted(paths) for number, paths in cited.items() if number not in written} == {}
    for path in tracked():  # and a link to a record names the file as it is called
        for name in re.findall(r"\badr/([0-9]{4}-[a-z0-9-]+\.md)", (ROOT / path).read_text(errors="replace")):
            assert (ROOT / "docs" / "adr" / name).is_file(), f"{path} links to docs/adr/{name}"


def test_every_document_the_code_and_the_config_point_to_exists():
    document = re.compile(r"\bdocs/[A-Za-z0-9_./-]+\.(?:md|yaml)\b")
    pointed = {}
    for path in tracked():
        if path.startswith(("src/", "config/", "tools/", ".githooks/")) or path == "Makefile":
            for target in document.findall((ROOT / path).read_text(errors="replace")):
                pointed.setdefault(target, path)
    assert "docs/runbooks/ibkr-probe.md" in pointed and "docs/architecture.md" in pointed
    assert {target: path for target, path in pointed.items() if not (ROOT / target).is_file()} == {}


def test_the_roadmap_has_all_eight_phases_and_the_documents_the_readme_links_exist():
    roadmap = read("docs/roadmap.md")
    assert [n for n in range(8) if f"| F{n} " in roadmap] == list(range(8))
    for link in re.findall(r"\]\(([^)#]+)\)", read("README.md")):
        if not link.startswith("http"):
            assert (ROOT / link).exists(), f"README links to {link}, which does not exist"


def test_the_never_build_list_names_what_the_plan_rules_out():
    text = read("docs/never-build.md").lower()
    for item in ("layering", "quote stuffing", "spoofing", "intent to cancel", "co-location", "live"):
        assert item in text, item


def test_the_placeholders_say_they_are_not_in_force():
    risk = yaml.safe_load(read("config/risk.yaml"))
    assert risk["status"] == "placeholder" and risk["in_force"] is False
    assert "NOT YET IN FORCE" in read("config/risk.yaml")
    venue = yaml.safe_load(read("config/venues/ibkr.yaml"))
    assert venue["venue"] == "ibkr_paper" and venue["status"] == "placeholder" and venue["pairs"] == []
    assert venue["probed_at"] is None and venue["access_route"] is None and venue["instrument_form"] is None
    for field in ("forms", "update_interval_ms", "depth_levels", "min_order_size", "commission", "order_round_trip_ms"):
        assert field in read("config/venues/ibkr.yaml"), f"the capability file does not list {field}"
    assert set(venue["weekly_login"]) == {"required", "observed_behaviour"}


def test_no_tracked_file_names_an_assistant_or_tool():
    """Org decision D-016 covers file contents too. The refused names are the commit-msg hook's digests."""
    found = {}
    for path in tracked():
        words = commitmsg.denied_words((ROOT / path).read_bytes().decode("utf-8", errors="replace"))
        if words:
            found[path] = words
    assert found == {}


def test_no_tracked_file_carries_a_co_author_trailer_line():
    trailer = re.compile(r"^\s*co-authored-by\s*:", re.IGNORECASE | re.MULTILINE)
    carriers = [p for p in tracked() if trailer.search((ROOT / p).read_bytes().decode("utf-8", errors="replace"))]
    # The tests of the rule itself have to spell an example of what is refused.
    assert set(carriers) <= {"tests/tools/test_commitmsg.py", "tests/tools/test_hooks.py"}

