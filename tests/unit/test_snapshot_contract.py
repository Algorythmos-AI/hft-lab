"""The status snapshot contract (contracts/): closed, free of free text, paper only, and its example conforms.

The contract is what this platform may tell the outside about itself. The tests hold it to three rules a reader
of the file could miss: nothing can be added to an object, no string can carry prose, and the mode can only be
paper. The small checker below understands exactly the keywords the contract uses, and a test fails if the
contract starts using one it does not know, so a rule can never be silently ignored.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contracts" / "hft-snapshot.v1.schema.json"
EXAMPLE_PATH = ROOT / "contracts" / "examples" / "hft-snapshot.v1.json"
SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
EXAMPLE: dict[str, Any] = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))

ANNOTATIONS = {"$schema", "$id", "title", "description"}
UNDERSTOOD = ANNOTATIONS | {
    "type", "enum", "pattern", "maxLength", "minimum", "maxItems", "uniqueItems", "items", "properties", "required",
    "additionalProperties",
}  # fmt: skip
TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def nodes(schema: dict[str, Any], path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Every schema in the contract, with the path to it."""
    yield path or "/", schema
    for name, child in schema.get("properties", {}).items():
        yield from nodes(child, f"{path}/{name}")
    if isinstance(schema.get("items"), dict):
        yield from nodes(schema["items"], f"{path}/[]")


def types_of(schema: dict[str, Any]) -> list[str]:
    declared = schema.get("type", [])
    return [declared] if isinstance(declared, str) else list(declared)


def problems(value: object, schema: dict[str, Any], path: str = "") -> list[str]:
    """Why `value` does not conform, as paths. An empty list means it conforms."""
    where = path or "/"
    allowed = types_of(schema)
    matching = [t for t in allowed if isinstance(value, TYPES[t]) and not (isinstance(value, bool) and t != "boolean")]
    if allowed and not matching:
        return [f"{where}: must be {' or '.join(allowed)}"]
    out: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: not one of {schema['enum']}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{where}: does not match the pattern")
        if len(value) > schema.get("maxLength", len(value)):
            out.append(f"{where}: too long")
    if isinstance(value, int | float) and not isinstance(value, bool) and value < schema.get("minimum", value):
        out.append(f"{where}: below the minimum")
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", len(value)):
            out.append(f"{where}: too many items")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            out.append(f"{where}: an item is repeated")
        for number, item in enumerate(value):
            out += problems(item, schema.get("items", {}), f"{path}/{number}")
    if isinstance(value, dict):
        known = schema.get("properties", {})
        out += [f"{where}: missing {name}" for name in schema.get("required", []) if name not in value]
        for name, item in value.items():
            if name in known:
                out += problems(item, known[name], f"{path}/{name}")
            elif schema.get("additionalProperties") is False:
                out.append(f"{path}/{name}: not a field of the contract")
    return out


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
