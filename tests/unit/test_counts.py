"""The counts the documents state are the counts of the tree: design records, research records, requirements, units.

A count in prose goes stale without anybody deciding that it should. Each sentence below is found by its
words, and the number in it is compared with what is in the repository. A sentence that was reworded so that
it is no longer found fails as well: the count is then checked nowhere.
"""
from __future__ import annotations

import re

import yaml

from tests.support import ROOT


def said(path: str) -> str:
    """A document as one line of words, so that a sentence is found wherever its lines break."""
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def numbers(path: str, sentence: str) -> tuple[int, ...]:
    found = re.search(sentence, said(path))
    assert found is not None, f"{path} no longer says: {sentence}"
    return tuple(int(number) for number in found.groups())


def research() -> tuple[int, int, int]:
    """(hypotheses, experiments, decisions) in research/, one record a file or a folder."""
    count = lambda kind, tag: len([p for p in (ROOT / "research" / kind).glob(f"HFT-{tag}-*")])  # noqa: E731
    return count("hypotheses", "HYP"), count("experiments", "EXP"), count("decisions", "DEC")


def requirements(name: str) -> tuple[int, int, int]:
    """(verified, all, not verified) of a requirements file."""
    listed = yaml.safe_load((ROOT / "docs" / "requirements" / name).read_text(encoding="utf-8"))["requirements"]
    verified = sum(one["status"] == "verified" for one in listed)
    return verified, len(listed), len(listed) - verified


def test_the_research_records_are_counted_the_same_wherever_a_document_counts_them():
    hypotheses, experiments, decisions = research()
    assert hypotheses >= 6 and experiments >= 6, "the folders are read: records are added, never removed"
    roadmap = "docs/roadmap.md"
    assert numbers(roadmap, r"registry holds (\d+) hypotheses and (\d+) experiments, all registered and none run, "
                            r"and (\d+) decision records") == (hypotheses, experiments, decisions)  # fmt: skip
    assert numbers(roadmap, r"It holds (\d+) records: (\d+) hypotheses, (\d+) experiments and (\d+) decisions") == (
        hypotheses + experiments + decisions, hypotheses, experiments, decisions,
    )  # fmt: skip
    assert numbers("README.md", r"(\d+) hypotheses and (\d+) experiments, registered and not run, and (\d+) decision "
                                r"records") == (hypotheses, experiments, decisions)  # fmt: skip
    index = "research/README.md"
    assert numbers(index, r"(\d+) hypotheses and (\d+) experiments are registered") == (hypotheses, experiments)
    listed = re.findall(r"\| \[(HFT-(?:HYP|EXP|DEC)-\d{4})\]\(", (ROOT / index).read_text(encoding="utf-8"))
    assert len(listed) == len(set(listed)) == hypotheses + experiments + decisions, "and its table has a row for each"
    assert ("There is no decision record yet" in said(index)) == (decisions == 0)


def test_the_design_records_are_as_many_as_the_roadmap_says_and_it_names_the_last_one():
    records = sorted(path.name for path in (ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert numbers("docs/roadmap.md", r"ADRs \| Done: 0001 to 0008, and (\d+) records now") == (len(records),)
    row = re.search(r"\| ADRs \| (.*?) \|", said("docs/roadmap.md"))
    assert row is not None and max(re.findall(r"\b(\d{4})\b", row.group(1))) == records[-1][:4]


def test_the_requirements_are_counted_as_the_two_files_hold_them():
    ours, theirs = requirements("requirements.yaml"), requirements("kb-requirements.yaml")
    roadmap = "docs/roadmap.md"
    assert numbers(roadmap, r"(\d+) of the first file's (\d+) requirements are verified by tests they name and the "
                            r"other (\d+) are open") == ours  # fmt: skip
    assert numbers(roadmap, r"(\d+) of the second file's (\d+) requirements are verified, all of them by tests on "
                            r"synthetic streams; the other (\d+) are open") == theirs  # fmt: skip


def test_the_units_the_architecture_page_counts_are_the_unit_files_there_are():
    services = sorted(path.name for path in (ROOT / "deploy" / "systemd").glob("*.service"))
    assert numbers("docs/architecture.md", r"That is (\d+) services\.") == (len(services),)
    page = said("docs/architecture.md")
    assert [name for name in services if f"`{name.removesuffix('.service')}`" not in page] == []


def test_the_places_for_models_are_as_many_as_the_roadmap_and_the_settings_say():
    sleeves = yaml.safe_load((ROOT / "config" / "trader.yaml").read_text(encoding="utf-8"))["sleeves"]
    places = [entry["slot"] for entry in sleeves if entry.get("kind") == "ml"]
    assert places == ["champion", "challenger-1", "challenger-2", "challenger-3"]
    assert "four places for it, a champion's and three challengers'" in said("docs/roadmap.md")
