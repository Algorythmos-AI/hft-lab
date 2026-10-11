"""The model file, and the directory models are kept in (ADR 0022).

A model file is one line of canonical JSON (sorted keys, no spaces, integers and text only):

    {"cost_model": {...}, "dataset_hash": "...", "features": [...], "hash": "...", "kind": "ridge",
     "label": {...}, "pair": "EUR.USD", "params": {...}, "size": 20000,
     "sources": {"days": {"recorded": 5}, "rows": {"recorded": 61200}}, "version": 2}

`hash` is the SHA-256 of the line without it, so a model is named by what it is: its parameters, the feature
keys it reads in order, the label it was trained to predict, the cost model that label was priced with (and
its placeholder flag), the order size, the hash of the dataset it was fitted to, and where the rows it was
fitted to came from. A number that is not whole is kept as text ("3/4"), never as a float.

`sources` is the split of the rows the model was fitted to, by where their days came from (ADR 0028): how many
rows, and how many days, under `recorded` for this platform's own recordings, `imported:<label>` for imported
history, and `unstated` for a stream nobody vouched for. It is written at fit time from the dataset
(`Dataset.split`), and it is under the hash like everything else. A file of version 2 without it is refused. A
file of version 1 has no such field and is still read: its split is unknown (`None`), which is never the same
as no rows. Nothing writes version 1 any more.

`ModelRegistry` is a directory of such files, each called by its hash. A file is written whole under another
name and then linked into place, which fails if the name exists: a model is never overwritten, and putting the
same model twice changes nothing. `load` refuses a file that is not what its name says, and a model whose
feature keys, label or pair are not the ones the caller expects: a model must never score rows built to another
definition.

Model files are artefacts. They live outside git, and the leak check refuses them (ADR 0006, ADR 0008).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from hftlab.core import _checks
from hftlab.journal import JournalError, canonical

VERSION: Final = 2
BEFORE_SOURCES: Final = 1  # the version that did not say where a model's rows came from
KINDS: Final = ("ridge", "gbm", "seq", "flat", "random")
UNKNOWN: Final = "unknown"  # how the split of a version 1 file is said: not known, which is not none
_HASH = re.compile(r"[0-9a-f]{64}")
_SOURCE = re.compile(r"recorded|unstated|imported:[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_FIELDS: Final = frozenset(
    {"kind", "version", "pair", "features", "label", "cost_model", "size", "dataset_hash", "params"}
)
Split = Mapping[str, Mapping[str, int]]  # {"rows": {source: count}, "days": {source: count}}


class ModelError(ValueError):
    """A model file that cannot be kept or read as what it claims to be."""


class ModelMismatch(ModelError):
    """The model is whole, and is not the one asked for: another hash, other features, another label."""


def _split(found: object) -> dict[str, dict[str, int]]:
    """A model's `sources`, checked: rows and days by source, a count for every source in both, days within rows."""
    if not isinstance(found, Mapping) or set(found) != {"rows", "days"}:
        raise ModelError("a model's sources are its rows and its days, each by where they came from")
    rows, days = found["rows"], found["days"]
    if not isinstance(rows, Mapping) or not isinstance(days, Mapping) or set(rows) != set(days):
        raise ModelError("a model's sources name the same sources for its rows and for its days")
    for source in rows:
        if not isinstance(source, str) or not _SOURCE.fullmatch(source):
            raise ModelError(f"a source is recorded, unstated or imported:<label>, not {source!r}")
        if type(rows[source]) is not int or type(days[source]) is not int or not 1 <= days[source] <= rows[source]:
            raise ModelError(f"source {source}: its rows and days are whole, and each day gave at least one row")
    return {"rows": {key: rows[key] for key in sorted(rows)}, "days": {key: days[key] for key in sorted(days)}}


@dataclass(frozen=True, slots=True)
class Model:
    kind: str
    pair: str
    features: tuple[str, ...]
    label: Mapping[str, int]
    cost_model: Mapping[str, object]
    size: int
    dataset_hash: str
    params: Mapping[str, object]
    sources: Split | None = None  # the rows it was fitted to, by source; None only in a file of version 1: unknown
    version: int = VERSION

    def __post_init__(self) -> None:
        _checks.pair(self.pair)
        _checks.positive("size", self.size)
        if self.kind not in KINDS:
            raise ModelError(f"a model is one of {KINDS}, not {self.kind!r}")
        if self.version == BEFORE_SOURCES:
            if self.sources is not None:
                raise ModelError(f"a version {BEFORE_SOURCES} model file has no sources: it cannot be given any")
        elif self.version != VERSION:
            raise ModelError(f"a model file is of version {BEFORE_SOURCES} or {VERSION}, not {self.version!r}")
        elif self.sources is None:
            raise ModelError(f"a version {VERSION} model says where the rows it was fitted to came from")
        else:
            object.__setattr__(self, "sources", _split(self.sources))
        if not _HASH.fullmatch(self.dataset_hash):
            raise ModelError("a model names the dataset it was fitted to by its SHA-256")
        if not self.features or len(set(self.features)) != len(self.features):
            raise ModelError("a model reads at least one feature, and each one once")
        try:
            canonical(self.body())
        except JournalError as exc:
            raise ModelError(f"a model file holds integers and text only: {exc}") from exc

    def body(self) -> dict[str, object]:
        said: dict[str, object] = {
            "kind": self.kind,
            "version": self.version,
            "pair": self.pair,
            "features": list(self.features),
            "label": dict(self.label),
            "cost_model": dict(self.cost_model),
            "size": self.size,
            "dataset_hash": self.dataset_hash,
            "params": dict(self.params),
        }
        if self.sources is not None:
            said["sources"] = {part: dict(counts) for part, counts in self.sources.items()}
        return said

    def fitted(self, part: str) -> dict[str, int] | None:
        """The `rows` or the `days` it was fitted to, by source. None for a version 1 file: unknown, never zero."""
        return None if self.sources is None else dict(self.sources[part])

    @property
    def fitted_to(self) -> str:
        """Where the rows it was fitted to came from, as a line says it."""
        if self.sources is None:
            return f"{UNKNOWN}: a version {BEFORE_SOURCES} model file does not say"
        rows, days = self.sources["rows"], self.sources["days"]
        return ", ".join(f"{key} {rows[key]} row(s) of {days[key]} day(s)" for key in rows) or "no rows"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(canonical(self.body()).encode("ascii")).hexdigest()

    @property
    def placeholder(self) -> bool:
        """Was it fitted to outcomes priced on placeholder costs? Then what it predicts is not a result."""
        return self.cost_model.get("placeholder") is not False

    def data(self) -> bytes:
        """The file: one canonical line, its own hash among its keys."""
        return (canonical({"hash": self.content_hash, **self.body()}) + "\n").encode("ascii")


def parse(data: bytes) -> Model:
    """A model from the bytes of its file. Raises `ModelError` unless they are exactly what `Model.data` writes."""
    try:
        loaded = json.loads(data)
    except ValueError as exc:
        raise ModelError("the model file is not JSON") from exc
    version = loaded.get("version") if isinstance(loaded, dict) else None
    if type(version) is not int or version not in (BEFORE_SOURCES, VERSION):
        raise ModelError(f"the model file is not a version {BEFORE_SOURCES} or a version {VERSION} model")
    if version == VERSION and "sources" not in loaded:
        raise ModelError(f"the model file is of version {VERSION} and does not say where its rows came from")
    if set(loaded) != _FIELDS | {"hash"} | ({"sources"} if version == VERSION else set()):
        raise ModelError(f"the model file does not hold exactly the fields of a version {version} model")
    features, label, costs, params = (loaded[key] for key in ("features", "label", "cost_model", "params"))
    if not isinstance(features, list) or not all(isinstance(key, str) for key in features):
        raise ModelError("the model file's features are not a list of keys")
    if not all(isinstance(part, dict) for part in (label, costs, params)):
        raise ModelError("the model file's label, cost model and parameters are mappings")
    try:
        model = Model(
            loaded["kind"], loaded["pair"], tuple(features), label, costs, loaded["size"], loaded["dataset_hash"],
            params, loaded.get("sources"), version,
        )  # fmt: skip
    except (TypeError, ValueError) as exc:
        raise ModelError(f"the model file cannot be read: {exc}") from exc
    if model.data() != data:
        raise ModelError("the model file does not match its own hash, or is not in canonical form")
    return model


class ModelRegistry:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, digest: str) -> Path:
        if not _HASH.fullmatch(digest):
            raise ModelError(f"a model is addressed by its SHA-256, not by {digest!r}")
        return self.directory / f"{digest}.json"

    def hashes(self) -> list[str]:
        """The models held, in name order."""
        if not self.directory.is_dir():
            return []
        return sorted(path.stem for path in self.directory.iterdir() if _HASH.fullmatch(path.stem) and path.suffix == ".json")

    def put(self, model: Model) -> str:
        """Keep the model under its hash and return the hash. What is already there is never written over."""
        digest, data = model.content_hash, model.data()
        final = self._path(digest)
        self.directory.mkdir(parents=True, exist_ok=True)
        partial = final.with_name(f".{digest}.partial")
        with partial.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(partial, final)  # fails if the name is taken: there is no moment at which a model is replaced
        except FileExistsError:
            if final.read_bytes() != data:
                raise ModelError(f"{final.name} already holds something else; it is not written over") from None
        finally:
            partial.unlink()
        return digest

    def load(self, digest: str, *, pair: str, features: Sequence[str], label: Mapping[str, int]) -> Model:
        """The model with this hash, if it is whole and reads these features to predict this label for this pair."""
        path = self._path(digest)
        try:
            model = parse(path.read_bytes())
        except OSError as exc:
            raise ModelError(f"there is no model {digest}: {exc}") from exc
        if model.content_hash != digest:
            raise ModelMismatch(f"{path.name} holds model {model.content_hash}, not the one it is named for")
        if model.pair != pair or model.features != tuple(features) or dict(model.label) != dict(label):
            raise ModelMismatch(f"model {digest} was trained for another pair, other features or another label")
        return model
