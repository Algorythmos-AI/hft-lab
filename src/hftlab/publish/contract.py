"""A checker for the status snapshot contract (contracts/hft-snapshot.v1.schema.json).

The repository has no JSON-schema library and does not need one: the contract uses a dozen keywords, and this
understands exactly those. A contract that uses a keyword this does not know is refused when it is loaded,
so a rule can never be silently ignored. The publisher checks every snapshot with it before the snapshot is
printed or sent, and tests/unit/test_snapshot_contract.py holds the contract file itself to its rules with the
same code: there is one checker, not two.

`problems` names where a value does not conform and never repeats the value: what it says may be logged.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

CONTRACT: Final = Path("contracts/hft-snapshot.v1.schema.json")
SCHEMA_ID: Final = "hft-lab/snapshot"
SCHEMA_VERSION: Final = 1
ANNOTATIONS: Final = frozenset({"$schema", "$id", "title", "description"})
UNDERSTOOD: Final = ANNOTATIONS | {
    "type", "enum", "pattern", "maxLength", "minimum", "maxItems", "uniqueItems", "items", "properties", "required",
    "additionalProperties",
}  # fmt: skip
TYPES: Final[dict[str, tuple[type, ...]]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


class ContractError(ValueError):
    """The contract file cannot be read, or asks for something this checker does not understand."""


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


def not_understood(schema: dict[str, Any]) -> list[str]:
    """Every place where the contract uses a keyword or a type this checker does not know."""
    out = []
    for path, node in nodes(schema):
        out += [f"{path}: the keyword {word}" for word in sorted(set(node) - UNDERSTOOD)]
        out += [f"{path}: the type {kind}" for kind in types_of(node) if kind not in TYPES]
        if not types_of(node):
            out.append(f"{path}: no type")
    return out


def load(path: Path = CONTRACT) -> dict[str, Any]:
    """The contract, refused unless it is this contract and every one of its rules can be checked."""
    try:
        schema: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"cannot read the contract {path}: {exc}") from exc
    if not isinstance(schema, dict) or schema.get("$id") != SCHEMA_ID:
        raise ContractError(f"{path} is not the {SCHEMA_ID} contract")
    if unknown := not_understood(schema):
        raise ContractError(f"{path} uses what the checker does not understand: {'; '.join(unknown)}")
    return schema


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
