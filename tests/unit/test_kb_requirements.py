"""The requirements taken from the private knowledge base: well formed, cited by id only, and pinned to a release."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
AREAS = {"data", "features", "labels", "models", "evaluation", "simulation", "execution", "risk", "ops", "governance"}


def _load() -> list[dict[str, Any]]:
    doc = yaml.safe_load((ROOT / "docs/requirements/kb-requirements.yaml").read_text(encoding="utf-8"))
    assert doc["version"] == 1
    items: list[dict[str, Any]] = doc["requirements"]
    return items


def test_every_requirement_is_well_formed() -> None:
    items = _load()
    ids = [r["id"] for r in items]
    assert ids == sorted(set(ids)), "ids are unique and in order"
    for r in items:
        assert re.fullmatch(r"KBREQ-\d{3}", r["id"])
        assert r["area"] in AREAS
        assert r["strength_of_basis"] in {"strong", "moderate", "weak"}
        assert r["statement"].endswith(".") and (" must " in r["statement"] or " should " in r["statement"]
                                                 or " may " in r["statement"])
        assert r["status"] in {"open", "verified"}
        assert (r["status"] == "verified") == bool(r["verified_by"])
        for path in r["verified_by"]:
            assert (ROOT / path).is_file(), f"{r['id']}: {path} does not exist"


def test_claims_are_cited_by_id_only() -> None:
    for r in _load():
        assert r["kb_claims"], f"{r['id']} cites no claim"
        for claim in r["kb_claims"]:
            match = re.fullmatch(r"CLM-([a-z0-9]+)-\d{4}", claim)
            assert match, claim
            assert match.group(1) in r["sources"]
        assert r["sources"] == sorted(set(r["sources"]))


def test_the_release_is_pinned() -> None:
    lock = json.loads((ROOT / "kb.lock.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"hft-\d{4}\.\d{2}\.\d{2}(\.\d+)?", lock["release"])
    assert lock["verified"] == lock["claims"], "requirements are taken only from a fully verified release"
    taken = lock["requirements_taken"]
    for other in lock.get("also", []):
        assert re.fullmatch(r"[a-z]+-\d{4}\.\d{2}\.\d{2}(\.\d+)?", other["release"])
        assert other["verified"] == other["claims"], "and so is every further release"
        taken += other["requirements_taken"]
    assert taken == len(_load())
