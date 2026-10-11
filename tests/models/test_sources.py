"""A model file says where the rows it was fitted to came from, under its own hash, for every kind of model.

The split is written at fit time from the dataset, by source: `recorded` for this platform's own recordings,
`imported:<label>` for imported history (ADR 0028). A file of version 2 without it is refused. A file of
version 1 never had it and is still read: its split is unknown, which is not the same as no rows.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from fractions import Fraction

import pytest

from hftlab.journal import canonical
from hftlab.learn import train
from hftlab.learn.dataset import DaySource, build
from hftlab.models import Model, ModelError, ModelRegistry, parse, scorer_for
from hftlab.models.file import BEFORE_SOURCES, UNKNOWN, VERSION
from tests.learn.kit import DAY, features, labels, planted
from tests.lineup.kit import BOOSTING, NETWORK, WINDOWS
from tests.venues.kit import T0

THEIRS = "imported:vendor-a"
BLOCKS = 6  # of the planted stream a day: 180 quotes
KINDS = ("ridge", "gbm", "seq")


def three_days():
    """Three short days of the planted stream: this platform's own, an imported one, and its own again."""
    stream, said = [], []
    days = (("2025-10-09", "recorded", 0), ("2025-10-10", THEIRS, 1), ("2025-10-13", "recorded", 4))
    for number, (day, source, after) in enumerate(days):
        stream += planted(number + 1, BLOCKS, start_ns=T0 + after * DAY, first_seq=len(stream) + 1)
        said.append(DaySource(day, source, BLOCKS * 30))
    return stream, said


def fitted(kind: str) -> tuple[Model, dict[str, dict[str, int]]]:
    """A model of this kind fitted to the first two days alone, and the split of those rows as the dataset says it."""
    stream, said = three_days()
    if kind == "gbm":
        pytest.importorskip("lightgbm", reason="the optional ML set is not installed (make ml)")
    if kind == "seq":
        pytest.importorskip("numpy", reason="the array library is not installed (make bootstrap)")
    made = build(stream, features(WINDOWS) if kind == "seq" else features(), labels(), sources=said)
    rows = [position for position, row in enumerate(made.rows) if row.n <= 2 * BLOCKS * 30]
    assert 0 < len(rows) < len(made.rows), "some rows are fitted to and some are not"
    if kind == "gbm":
        return train.gbm_model(made, rows, BOOSTING), made.split(rows)
    if kind == "seq":
        return train.seq_model(made, rows, NETWORK), made.split(rows)
    return train.ridge_model(made, rows, Fraction(1, 1000)), made.split(rows)


def rewritten(model: Model, **changes) -> bytes:
    """The model's file with fields changed or (None) left out, and the hash that such a line has."""
    body = {**json.loads(model.data()), **changes}
    body = {key: value for key, value in body.items() if value is not None and key != "hash"}
    digest = hashlib.sha256(canonical(body).encode("ascii")).hexdigest()
    return (canonical({"hash": digest, **body}) + "\n").encode("ascii")


@pytest.fixture(scope="module", params=KINDS)
def made(request):
    return fitted(request.param)


def test_the_split_of_the_rows_it_was_fitted_to_is_written_at_fit_time_by_source(made):
    model, split = made
    assert model.sources == split and model.version == VERSION == 2
    assert set(split["rows"]) == {"recorded", THEIRS} and split["days"] == {THEIRS: 1, "recorded": 1}, (
        "the third day is this platform's own too, and no row of it was fitted to: it is not counted"
    )
    assert model.fitted("rows") == split["rows"] and model.fitted("days") == split["days"]
    assert sum(split["rows"].values()) > 100 and min(split["rows"].values()) > 30
    days, rows = split["days"], split["rows"]
    assert model.fitted_to == (
        f"{THEIRS} {rows[THEIRS]} row(s) of {days[THEIRS]} day(s), recorded {rows['recorded']} row(s) of 1 day(s)"
    ), "what a line that describes the model says"
    data = model.data()
    assert json.loads(data)["sources"] == split and parse(data) == model and parse(data).sources == split


def test_the_split_is_under_the_files_hash(made, tmp_path):
    model, split = made
    moved = {"rows": {"recorded": sum(split["rows"].values())}, "days": {"recorded": 2}}
    other = dataclasses.replace(model, sources=moved)
    assert other.content_hash != model.content_hash, "the same weights fitted to other sources are another model"
    assert other.params == model.params and other.dataset_hash == model.dataset_hash
    edited = model.data().replace(f'"{THEIRS}":'.encode(), b'"recorded":', 1)
    assert edited != model.data()
    with pytest.raises(ModelError, match="cannot be read|does not match its own hash"):
        parse(edited)
    count = str(split["rows"][THEIRS]).encode()
    with pytest.raises(ModelError, match="does not match its own hash"):
        parse(model.data().replace(b'"rows":{"%b":%b' % (THEIRS.encode(), count), b'"rows":{"%b":1' % THEIRS.encode()))
    registry = ModelRegistry(tmp_path)
    assert registry.put(model) != registry.put(other) and len(registry.hashes()) == 2


def test_a_file_of_the_new_version_that_does_not_say_is_refused(made):
    model, _ = made
    silent = rewritten(model, sources=None)
    assert b'"version":2' in silent and b"sources" not in silent
    with pytest.raises(ModelError, match="is of version 2 and does not say where its rows came from"):
        parse(silent)
    with pytest.raises(ModelError, match="a version 2 model says where the rows it was fitted to came from"):
        dataclasses.replace(model, sources=None)
    with pytest.raises(ModelError, match="not a version 1 or a version 2 model"):
        parse(rewritten(model, version=3))


def test_a_file_of_the_older_version_still_loads_and_its_split_is_unknown_never_zero(made, tmp_path):
    model, _ = made
    old = rewritten(model, version=BEFORE_SOURCES, sources=None)
    before = parse(old)
    assert before.version == 1 and before.sources is None and before.data() == old, "read back byte for byte"
    assert before.fitted("rows") is None and before.fitted("days") is None, "not known: not an empty split"
    assert before.fitted_to == f"{UNKNOWN}: a version 1 model file does not say" and "0" not in before.fitted_to
    assert before.content_hash == json.loads(old)["hash"] != model.content_hash
    assert (before.kind, before.params, before.features) == (model.kind, model.params, model.features)
    assert type(scorer_for(before)) is type(scorer_for(model)), "and it is scored as it always was"
    registry = ModelRegistry(tmp_path)
    assert registry.put(before) == before.content_hash
    assert registry.load(before.content_hash, pair=model.pair, features=model.features, label=model.label) == before
    with pytest.raises(ModelError, match="does not hold exactly the fields of a version 1 model"):
        parse(rewritten(model, version=BEFORE_SOURCES))
    with pytest.raises(ModelError, match="a version 1 model file has no sources"):
        dataclasses.replace(before, sources={"rows": {}, "days": {}})


@pytest.mark.parametrize(
    "wrong",
    [{"rows": {"recorded": 5}}, {"rows": {"recorded": 5}, "days": {THEIRS: 1}},
     {"rows": {"recorded": 1}, "days": {"recorded": 2}},
     {"rows": {"recorded": 0}, "days": {"recorded": 0}}, {"rows": {"elsewhere": 5}, "days": {"elsewhere": 1}},
     {"rows": {"imported:": 5}, "days": {"imported:": 1}}, {"rows": {"recorded": True}, "days": {"recorded": True}},
     {"rows": {"recorded": "5"}, "days": {"recorded": 1}}, {"rows": {"recorded": 5}, "days": {"recorded": 1}, "weeks": {}},
     {"rows": [5], "days": [1]}, "recorded", []],
)  # fmt: skip
def test_a_split_that_is_not_rows_and_days_by_source_is_refused_by_the_model_and_by_the_file(wrong):
    model, _ = fitted("ridge")
    with pytest.raises(ModelError):
        dataclasses.replace(model, sources=wrong)
    with pytest.raises(ModelError):
        parse(rewritten(model, sources=wrong))
    empty = dataclasses.replace(model, sources={"rows": {}, "days": {}})
    assert parse(empty.data()) == empty and empty.fitted_to == "no rows", "fitted to nothing is said, and is not unknown"
