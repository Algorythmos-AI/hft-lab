"""Shared pieces for the import tests: a folder of a few hand-written rows, its description, and a run of it."""
from __future__ import annotations

from pathlib import Path

import yaml

from hftlab.calendar import Calendar
from hftlab.history.importer import Imported, Importer
from hftlab.record import format as fmt
from tests.venues.kit import EURUSD, USDJPY

LABEL = "vendor-a"
# Wednesday 2026-10-14, 03:00 UTC: the middle of a forex week. Every row below is written by hand.
GENERIC = """Time,Bid,Ask
2026-10-14 03:00:00.000,1.08500,1.08510
2026-10-14 03:00:01.250,1.08501,1.08511
2026-10-14 03:00:02.500,1.08502,1.08512
"""
SIZED = """Time,Bid,Ask,BidSize,AskSize
2026-10-14 03:00:00.000,1.08500,1.08510,1.5,2
2026-10-14 03:00:01.000,1.08501,1.08511,0.75,1
"""
INSTRUMENTS = [fmt.describe(EURUSD), fmt.describe(USDJPY)]
COLUMNS = {"time": "Time", "bid": "Bid", "ask": "Ask"}
SIZE_COLUMNS = {**COLUMNS, "bid_size": "BidSize", "ask_size": "AskSize"}


def described(**changes) -> dict:
    """A description of the generic layout, in UTC, with a header. `changes` replace a key or, with None, remove it."""
    said = {
        "version": 1, "layout": "generic", "time_zone": "UTC", "header": True, "delimiter": ",",
        "time_format": "%Y-%m-%d %H:%M:%S.%f", "columns": dict(COLUMNS), "instruments": list(INSTRUMENTS),
    }  # fmt: skip
    said.update(changes)
    return {key: value for key, value in said.items() if value is not None}


def a_folder(folder: Path, files: dict[str, str] | None = None, pairs: dict | None = None, **changes) -> Path:
    """Write the files and the description that lists them. Every file holds EUR.USD unless `pairs` says otherwise."""
    files = {"ticks.csv": GENERIC} if files is None else files
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text, encoding="ascii")
    listed = [{"name": name, "pair": (pairs or {}).get(name, "EUR.USD")} for name in files]
    said = described(**{"files": listed, **changes})
    (folder / "import.yaml").write_text(yaml.safe_dump(said), encoding="utf-8")
    return folder


def run(root: Path, folder: Path, label: str = LABEL, **options) -> Imported:
    calendar = options.pop("calendar", Calendar())
    recorder_root = options.pop("recorder_root", None)
    return Importer(root, calendar, recorder_root=recorder_root).run(folder, label, **options)


def tree(root: Path) -> dict[str, bytes]:
    """Every file under a store, by its path from the root, with its bytes."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}
