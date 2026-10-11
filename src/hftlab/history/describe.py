"""The description file: what the owner states about a folder of exported tick files (ADR 0028).

A folder to import holds the files and one `import.yaml` beside them. Nothing about a file is worked out from
its contents or its name: what is not stated here is refused.

    version: 1
    layout: generic              # generic | utc_ask_bid | local_bid_ask (hftlab.history.parsers)
    time_zone: UTC               # UTC, an offset like -05:00, or a zone of the time-zone database
    header: true                 # is the first line of every file a header line?
    size_unit: "1000000"         # base-currency units per 1 of a size column; only for a layout with sizes
    instruments:                 # as a recorded day's journal describes them, and as the venue's own
      - {pair: EUR.USD, kind: CASH, price_scale: 5, min_order_size: 20000, size_increment: 1000, price_increment: 5}
    files:
      - {name: ticks-a.csv, pair: EUR.USD}

and, for the generic layout only:

    delimiter: ","
    time_format: "%Y-%m-%d %H:%M:%S.%f"
    columns: {time: Time, bid: Bid, ask: Ask, bid_size: BidSize, ask_size: AskSize}

`columns` gives header names when `header` is true and positions counted from 1 when it is false. The two
size columns are named together or not at all. The instruments are written out because a day folder must be
priced by itself (ADR 0012), and because a dataset refuses days that describe one pair in two ways: to be read
beside the platform's own recordings, an imported pair is described exactly as the recorder describes it.

Every file in the folder is listed, and every listed file is there. A file nobody described is not skipped:
the import is refused, so that what was imported is exactly what was stated.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from hftlab.core import Instrument, to_scaled
from hftlab.core._checks import pair as check_pair
from hftlab.history.times import TimeError, reader, zone_of
from hftlab.record.format import CorruptSegment, describe, instrument_of

SIDECAR: Final = "import.yaml"
VERSION: Final = 1
GENERIC, UTC_ASK_BID, LOCAL_BID_ASK = "generic", "utc_ask_bid", "local_bid_ask"
SIZE_SCALE: Final = 9  # a size and its unit are read to this many decimals, exactly
_COMMON: Final = frozenset({"version", "layout", "time_zone", "header", "instruments", "files"})
_KEYS: Final = {
    GENERIC: _COMMON | {"delimiter", "time_format", "columns", "size_unit"},
    UTC_ASK_BID: _COMMON | {"size_unit"},
    LOCAL_BID_ASK: _COMMON,
}
_REQUIRED: Final = ("time", "bid", "ask")
_SIZES: Final = ("bid_size", "ask_size")


class DescriptionError(ValueError):
    """The description file is missing, or leaves something unstated, or does not match the folder."""


@dataclass(frozen=True, slots=True)
class Described:
    """One file of the folder: where it is, and the pair the owner says it holds."""

    path: Path
    pair: str

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True, slots=True)
class Description:
    layout: str
    time_zone: str  # as stated
    header: bool
    instruments: Mapping[str, Instrument]
    files: tuple[Described, ...]
    delimiter: str = ","
    time_format: str = ""  # of the generic layout; the two fixed layouts carry their own
    columns: Mapping[str, str | int] | None = None
    size_unit: str | None = None  # as stated; None when the layout has no sizes
    size_unit_scaled: int = 0  # the same, as a whole number of 10**-SIZE_SCALE

    @property
    def has_sizes(self) -> bool:
        return self.size_unit is not None

    def stated(self, pairs: Mapping[str, object]) -> dict[str, object]:
        """What was stated that decides how a file is read, for the pairs of one day: kept in its provenance."""
        said: dict[str, object] = {
            "layout": self.layout,
            "time_zone": self.time_zone,
            "header": self.header,
            "size_unit": self.size_unit,
            "instruments": [describe(self.instruments[pair]) for pair in sorted(pairs)],
        }
        if self.layout == GENERIC:
            said |= {"delimiter": self.delimiter, "time_format": self.time_format, "columns": dict(self.columns or {})}
        return said


def _columns(found: object, header: bool) -> tuple[dict[str, str | int], bool]:
    if not isinstance(found, dict) or not set(_REQUIRED) <= set(found) <= {*_REQUIRED, *_SIZES}:
        raise DescriptionError("`columns` names time, bid and ask, and may name bid_size and ask_size")
    sizes = [key in found for key in _SIZES]
    if any(sizes) != all(sizes):
        raise DescriptionError("`columns` names both size columns or neither")
    for key, where in found.items():
        named = isinstance(where, str) and where != ""
        placed = type(where) is int and where >= 1
        if (named if header else placed) is False:
            how = "by its header name" if header else "by its position, counted from 1"
            raise DescriptionError(f"`columns.{key}` is given {how}, not as {where!r}")
    if len(set(found.values())) != len(found):
        raise DescriptionError("`columns` names one column twice")
    return dict(found), all(sizes)


def _unit(found: object) -> tuple[str, int]:
    if not isinstance(found, str):
        raise DescriptionError('`size_unit` is a quoted number of base-currency units, like "1000000" or "1"')
    try:
        scaled = to_scaled(found, SIZE_SCALE)
    except ValueError as exc:
        raise DescriptionError(f"`size_unit`: {exc}") from exc
    if scaled <= 0:
        raise DescriptionError("`size_unit` is above zero")
    return found, scaled


def parse(loaded: object, folder: Path) -> Description:
    """A description from what the file holds. Raises `DescriptionError` for anything unstated or unknown."""
    if not isinstance(loaded, dict) or loaded.get("version") != VERSION:
        raise DescriptionError(f"{SIDECAR} is not a version {VERSION} description")
    layout = loaded.get("layout")
    if layout not in _KEYS:
        raise DescriptionError(f"`layout` is one of {', '.join(sorted(_KEYS))}, not {layout!r}")
    missing, unknown = sorted(_COMMON - set(loaded)), sorted(set(loaded) - _KEYS[layout])
    if missing or unknown:
        raise DescriptionError(f"{SIDECAR} for the {layout} layout: missing {missing}, not understood {unknown}")
    header = loaded["header"]
    if type(header) is not bool:
        raise DescriptionError("`header` is true or false: whether the first line of every file is a header")
    try:
        zone_of(loaded["time_zone"])
    except TimeError as exc:
        raise DescriptionError(f"`time_zone`: {exc}") from exc
    if layout == UTC_ASK_BID and loaded["time_zone"] != "UTC":
        raise DescriptionError("the utc_ask_bid layout is in UTC; `time_zone` must say UTC, and says something else")
    more: dict[str, Any] = {}
    sized = layout == UTC_ASK_BID
    if layout == GENERIC:
        for key in ("delimiter", "time_format", "columns"):
            if key not in loaded:
                raise DescriptionError(f"the generic layout needs `{key}`")
        delimiter = loaded["delimiter"]
        if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in '"\r\n' or delimiter.isalnum():
            raise DescriptionError("`delimiter` is one character that is not a letter, a digit or a quote")
        columns, sized = _columns(loaded["columns"], header)
        try:
            reader(loaded["time_format"], loaded["time_zone"])
        except TimeError as exc:
            raise DescriptionError(f"`time_format`: {exc}") from exc
        more = {"delimiter": delimiter, "time_format": loaded["time_format"], "columns": columns}
    if sized != ("size_unit" in loaded):
        raise DescriptionError(
            "`size_unit` is stated when the layout has size columns and only then: a size is never read in a "
            "guessed unit"
        )
    if sized:
        more["size_unit"], more["size_unit_scaled"] = _unit(loaded["size_unit"])
    instruments = _instruments(loaded["instruments"])
    files = _files(loaded["files"], folder, instruments)
    return Description(layout, loaded["time_zone"], header, instruments, files, **more)


def _instruments(found: object) -> dict[str, Instrument]:
    out: dict[str, Instrument] = {}
    for entry in found if isinstance(found, list) else []:
        if not isinstance(entry, dict):
            raise DescriptionError(f"an instrument is a mapping, not {entry!r}")
        try:
            instrument = instrument_of(entry)
        except CorruptSegment as exc:
            raise DescriptionError(f"`instruments`: {exc}") from exc
        if out.setdefault(instrument.pair, instrument) is not instrument:
            raise DescriptionError(f"`instruments` describes {instrument.pair} twice")
    if not out:
        raise DescriptionError("`instruments` describes at least one pair: its scale, increments and minimum size")
    return out


def _files(found: object, folder: Path, instruments: Mapping[str, Instrument]) -> tuple[Described, ...]:
    out: list[Described] = []
    for entry in found if isinstance(found, list) else []:
        if not isinstance(entry, dict) or set(entry) != {"name", "pair"}:
            raise DescriptionError(f"a file is written {{name: ticks.csv, pair: EUR.USD}}, not {entry!r}")
        name = entry["name"]
        if not isinstance(name, str) or Path(name).name != name or name.startswith(".") or name == SIDECAR:
            raise DescriptionError(f"a file is named as it is called in the folder, without a path: not {name!r}")
        try:
            pair = check_pair(entry["pair"])
        except ValueError as exc:
            raise DescriptionError(str(exc)) from exc
        if pair not in instruments:
            raise DescriptionError(f"{name} is said to hold {pair}, which `instruments` does not describe")
        out.append(Described(folder / name, pair))
    names = [one.name for one in out]
    if not out or len(set(names)) != len(names):
        raise DescriptionError("`files` lists at least one file, and each one once")
    there = sorted(p.name for p in folder.iterdir() if p.is_file() and not p.name.startswith(".") and p.name != SIDECAR)
    if there != sorted(names):
        absent, extra = sorted(set(names) - set(there)), sorted(set(there) - set(names))
        raise DescriptionError(f"the folder and `files` differ: listed and absent {absent}, there and unlisted {extra}")
    return tuple(sorted(out, key=lambda one: one.name))


def load(folder: Path) -> Description:
    """The description of a folder of files to import."""
    path = folder / SIDECAR
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DescriptionError(f"cannot read {path}: {exc}") from exc
    return parse(loaded, folder)
