"""The status snapshot contract (contracts/): closed, free of free text, paper only, and its example conforms.

The contract is what this platform may tell the outside about itself. The tests hold it to three rules a reader
of the file could miss: nothing can be added to an object, no string can carry prose, and the mode can only be
paper. The checker is the publisher's own (hftlab.publish.contract): it understands exactly the keywords the
contract uses, and a test fails if the contract starts using one it does not know, so a rule can never be
silently ignored. What is checked here and what is checked before a snapshot is sent is the same code.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from hftlab.publish import contract
from hftlab.publish.contract import TYPES, UNDERSTOOD, nodes, problems, types_of

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contracts" / "hft-snapshot.v1.schema.json"
EXAMPLE_PATH = ROOT / "contracts" / "examples" / "hft-snapshot.v1.json"
SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
EXAMPLE: dict[str, Any] = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


# ---- the contract itself ---------------------------------------------------------------------------------------


def test_it_names_itself_and_its_version() -> None:
    assert SCHEMA["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert SCHEMA["$id"] == "hft-lab/snapshot"
    assert SCHEMA["properties"]["schema"]["enum"] == ["hft-lab/snapshot"]
    assert SCHEMA["properties"]["schema_version"]["enum"] == [1]
    assert SCHEMA_PATH.name == "hft-snapshot.v1.schema.json"


def test_it_uses_only_keywords_the_checker_understands() -> None:
    for path, node in nodes(SCHEMA):
        assert set(node) <= UNDERSTOOD, f"{path} uses {sorted(set(node) - UNDERSTOOD)}"
        assert set(types_of(node)) <= set(TYPES) and types_of(node), f"{path} needs a known type"
    assert contract.not_understood(SCHEMA) == []


def test_the_publisher_loads_this_file_and_refuses_a_contract_it_could_not_check_whole(tmp_path) -> None:
    assert contract.load(SCHEMA_PATH) == SCHEMA and SCHEMA_PATH == ROOT / contract.CONTRACT
    assert SCHEMA["$id"] == contract.SCHEMA_ID
    assert SCHEMA["properties"]["schema_version"]["enum"] == [contract.SCHEMA_VERSION]
    changes: list[dict[str, Any]] = [
        {"minProperties": 1},
        {"properties": {**SCHEMA["properties"], "extra": {"type": "string", "format": "uri"}}},
        {"properties": {**SCHEMA["properties"], "extra": {"type": "decimal"}}},
        {"properties": {**SCHEMA["properties"], "extra": {"enum": ["a"]}}},
        {"$id": "trading-lab/snapshot"},
    ]
    for change in changes:
        (tmp_path / "contract.json").write_text(json.dumps({**SCHEMA, **change}))
        with pytest.raises(contract.ContractError):
            contract.load(tmp_path / "contract.json")
    (tmp_path / "contract.json").write_text("[]")
    with pytest.raises(contract.ContractError):
        contract.load(tmp_path / "contract.json")
    with pytest.raises(contract.ContractError, match="cannot read"):
        contract.load(tmp_path / "missing.json")


def test_every_object_is_closed() -> None:
    for path, node in nodes(SCHEMA):
        if "object" in types_of(node):
            assert node.get("additionalProperties") is False, f"{path} accepts fields the contract does not name"
            assert set(node.get("required", [])) <= set(node.get("properties", {})), path


def test_no_string_can_carry_prose() -> None:
    """A string is one of a fixed list, or it is short and matches a pattern with no room for a space."""
    for path, node in nodes(SCHEMA):
        if "string" not in types_of(node) or "enum" in node:
            continue
        pattern = node.get("pattern", "")
        assert pattern.startswith("^") and pattern.endswith("$"), f"{path} has no anchored pattern"
        assert node.get("maxLength", 10**9) <= 80, f"{path} has no short length limit"
        assert not re.search(pattern, "two words"), f"{path} would accept free text"


def test_every_list_is_bounded() -> None:
    for path, node in nodes(SCHEMA):
        if "array" in types_of(node):
            assert node.get("maxItems", 10**9) <= 40, f"{path} is an unbounded list"


def test_the_mode_can_only_be_paper() -> None:
    assert SCHEMA["properties"]["desk"]["properties"]["mode"]["enum"] == ["paper"]
    assert "mode" in SCHEMA["properties"]["desk"]["required"]
    assert "desk" in SCHEMA["required"]


# ---- the example, and what the contract refuses ------------------------------------------------------------------


def test_the_example_conforms() -> None:
    assert problems(EXAMPLE, SCHEMA) == []


def test_the_smallest_snapshot_conforms() -> None:
    """Before the recorder runs there is nothing to report but identity, windows, stage and health."""
    smallest = {name: EXAMPLE[name] for name in SCHEMA["required"]}
    assert problems(smallest, SCHEMA) == []


def changed(path: tuple[str | int, ...], value: object) -> dict[str, Any]:
    out = copy.deepcopy(EXAMPLE)
    target: Any = out
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return out


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("desk", "mode"), "live"),
        (("desk", "venue"), "ibkr_live"),
        (("schema",), "trading-lab/snapshot"),
        (("schema_version",), 2),
        (("note",), "anything"),
        (("desk", "account"), "DU0000000"),
        (("health", "reasons"), ["the recorder stopped at noon"]),
        (("health", "reasons"), ["recorder_stopped", "recorder_stopped"]),
        (("run_id",), "has a space"),
        (("as_of",), "yesterday"),
        (("recorder", "events_today"), -1),
        (("recorder", "events_today"), True),
        (("phases",), [{"id": "F9", "state": "done"}]),
        (("sleeves", 0, "name"), "martingale"),
        (("pairs", 0, "pair"), "EURUSD"),
    ],
)
def test_it_refuses(path: tuple[str | int, ...], value: object) -> None:
    assert problems(changed(path, value), SCHEMA) != []


def test_it_refuses_a_snapshot_with_a_required_part_missing() -> None:
    for name in SCHEMA["required"]:
        without = {k: v for k, v in EXAMPLE.items() if k != name}
        assert any(name in problem for problem in problems(without, SCHEMA)), name
