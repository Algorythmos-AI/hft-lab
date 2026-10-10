"""docs/requirements/requirements.yaml keeps its schema, and a requirement marked verified names real tests."""
from __future__ import annotations

import re

import yaml

from tests.support import ROOT

PATH = ROOT / "docs" / "requirements" / "requirements.yaml"
FIELDS = {"id", "title", "statement", "basis", "status", "verified_by", "rationale_cites", "kb_claims"}
AREAS = {"SAFE", "CORE", "RISK", "MKT", "CONN", "BOOK", "REC", "TEST", "LEARN"}


def requirements() -> list[dict]:
    return yaml.safe_load(PATH.read_text())["requirements"]


def test_the_file_documents_its_schema():
    text = PATH.read_text()
    assert text.startswith("#") and "Schema" in text.split("version:")[0]
    for field in FIELDS:
        assert re.search(rf"^#   {field}\b", text, re.MULTILINE), f"the schema comment does not explain {field}"


def test_there_are_ten_to_fifteen_seed_requirements_with_unique_ids():
    ids = [r["id"] for r in requirements()]
    assert 10 <= len(ids) <= 15 and len(set(ids)) == len(ids)
    for requirement_id in ids:
        match = re.fullmatch(r"REQ-([A-Z]+)-[0-9]{3}", requirement_id)
        assert match and match.group(1) in AREAS, requirement_id


def test_every_requirement_has_exactly_the_schema_fields():
    for r in requirements():
        assert set(r) == FIELDS, r["id"]
        assert isinstance(r["title"], str) and r["title"].strip()
        assert re.search(r"\bmust\b", r["statement"]), f"{r['id']} states no obligation"
        assert r["status"] in ("seed", "verified")


def test_the_seed_requirements_rest_on_engineering_judgement_and_cite_nothing_yet():
    for r in requirements():
        assert r["basis"] == "engineering_judgement", r["id"]
        assert r["rationale_cites"] == [] and r["kb_claims"] == [], r["id"]


def test_a_verified_requirement_names_tests_that_exist_and_a_seed_names_none():
    verified = [r for r in requirements() if r["status"] == "verified"]
    assert verified, "F0 proves at least the lock and the core types"
    for r in requirements():
        if r["status"] == "verified":
            assert r["verified_by"], r["id"]
            for test_path in r["verified_by"]:
                assert (ROOT / test_path).is_file() and test_path.startswith("tests/"), f"{r['id']}: {test_path}"
        else:
            assert r["verified_by"] == [], r["id"]
