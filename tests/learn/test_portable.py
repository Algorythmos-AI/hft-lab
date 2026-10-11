"""The same tiny fit on any machine: the model file of each kind against hashes written down on one machine.

`expected_model_hashes.json`, beside this file, holds for each kind of model the hash of one small synthetic
dataset, the hash of the parameters fitted to it and the hash of the model file, as they came out on the
machine named in it (macOS, arm64). The tests below make the same stream, the same dataset and the same fit,
and compare. CI runs on Linux x86-64, so a pass there shows what the records only argue (ADR 0025 section 4,
ADR 0029 section 4): that the fit writes the same file on another processor and another build.

How to read a failure. Each kind is one test, and the three hashes are compared in order:

  dataset   differs: the stream, the features or the labels differ. No fit has been made yet; it is not the
            fit's arithmetic, and it would fail on the machine the fixture was made on as well.
  params    differs while the dataset agrees: the fit itself came out differently on this machine.
  model     differs while both agree: the file's format changed and the fixture was not made again.

The message of a failure gives both hashes, this machine and its libraries. Nothing here is marked as
expected to fail, for any kind: a disagreement is a finding about the fit, to be read, not a test to quiet.

Everything is stated here, down to the numbers: the stream is written out by whole-number arithmetic with
no generator, and the settings are literals, not the shipped placeholders. To make the fixture again, on the
machine it names: `python -m tests.learn.test_portable > tests/learn/expected_model_hashes.json`.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from hftlab.core import Quote
from hftlab.features import FeatureConfig
from hftlab.journal import canonical
from hftlab.learn import train
from hftlab.learn.config import Boosting, Network
from hftlab.learn.dataset import Dataset, DaySource, build
from hftlab.models import Model, parse
from tests.learn.kit import M, features, labels
from tests.venues.kit import MS, SECOND, T0

FIXTURE = Path(__file__).with_name("expected_model_hashes.json")
KINDS = ("ridge", "seq", "gbm")
BLOCKS = 40  # of three seconds, ten quotes to the second: 1,200 quotes
COIN = 0x9E3779B97F4A7C15  # block b goes up when bit b of this is set: stated, not drawn
JUMP = 100  # price units
STREAM = "fa6f40e2025f6cf9a41e328e906e6f2011cec51dd294b40e7687125fecddcb9b"  # of the quotes, as whole numbers
ROWS = FeatureConfig(lookbacks_ns=(500 * MS, SECOND), rate_window_ns=SECOND, range_window_ns=2 * SECOND,
                     stale_after_ns=5 * SECOND)  # fmt: skip
WINDOWS = FeatureConfig(ROWS.lookbacks_ns, ROWS.rate_window_ns, ROWS.range_window_ns, ROWS.stale_after_ns, 4)
PENALTY = Fraction(1, 1000)
NETWORK = Network(window=4, kernel=2, filters=4, hidden=8, epochs=12, batch_rows=64, learning_rate_ppm=10_000,
                  l2_ppm=1_000, clip=3, cap=8, seed=1)  # fmt: skip
BOOSTING = Boosting(trees=10, max_depth=3, leaves=8, learning_rate_ppm=200_000, min_leaf_rows=5, bins=63,
                    l2_ppm=1_000_000, leaf_scale=1000, seed=1)  # fmt: skip


def stream() -> list[Quote]:
    """A still book. In a block's second second one side shows three times the other's size; then the mid jumps."""
    out, mid = [], 108_505
    for block in range(BLOCKS):
        up = bool(COIN >> block & 1)
        for tick in range(30):
            if tick == 20:
                mid += JUMP if up else -JUMP
            heavy = 10 <= tick < 20
            sizes = (3 * M if heavy and up else M, 3 * M if heavy and not up else M)
            number = block * 30 + tick
            out.append(Quote("EUR.USD", mid - 5, mid + 5, *sizes, T0 + number * 100 * MS, number + 1))
    return out


def dataset(kind: str) -> Dataset:
    quotes = stream()
    said = [DaySource("2025-10-09", "recorded", len(quotes))]
    return build(quotes, features(WINDOWS if kind == "seq" else ROWS), labels(), sources=said)


def fit(kind: str, made: Dataset) -> Model:
    rows = range(len(made.rows))
    if kind == "seq":
        return train.seq_model(made, rows, NETWORK)
    if kind == "gbm":
        return train.gbm_model(made, rows, BOOSTING)
    return train.ridge_model(made, rows, PENALTY)


def hashes(kind: str) -> dict[str, object]:
    made = dataset(kind)
    model = fit(kind, made)
    assert parse(model.data()) == model
    params = hashlib.sha256(canonical(dict(model.params)).encode("ascii")).hexdigest()
    return {"rows": len(made.rows), "dataset": made.content_hash, "params": params, "model": model.content_hash}


def machine() -> str:
    """This machine and the libraries a fit used on it, for the message of a failure and for the fixture."""
    parts = [platform.system(), platform.machine(), f"python {platform.python_version()}"]
    for name in ("numpy", "lightgbm"):
        found = sys.modules.get(name)
        if found is not None:
            parts.append(f"{name} {found.__version__}")
    return ", ".join(parts)


def needs(kind: str) -> None:
    if kind == "seq":
        pytest.importorskip("numpy", reason="the array library is not installed (make bootstrap)")
    if kind == "gbm":
        pytest.importorskip(
            "lightgbm", reason="the optional ML set is not installed (make ml): the trees are compared in the ml job"
        )


@pytest.mark.parametrize("kind", KINDS)
def test_the_same_tiny_fit_writes_the_model_file_the_fixture_names_on_this_machine_too(kind):
    needs(kind)
    expected = json.loads(FIXTURE.read_text())
    wanted, found = expected["kinds"][kind], hashes(kind)
    where = f"here ({machine()}), against the fixture's machine ({expected['produced_on']})"
    assert found["rows"] == wanted["rows"] and found["dataset"] == wanted["dataset"], (
        f"{kind}: the DATASET differs, before any fit: {found['dataset']} with {found['rows']} rows {where}, "
        f"not {wanted['dataset']} with {wanted['rows']}"
    )
    assert found["params"] == wanted["params"], (
        f"{kind}: the same dataset, and the FIT came out differently: params {found['params']} {where}, "
        f"not {wanted['params']}. This is a finding about the fit on this machine, not a fault of the test"
    )
    assert found["model"] == wanted["model"], (
        f"{kind}: the same dataset and the same parameters, and another model FILE: {found['model']} {where}, "
        f"not {wanted['model']}. The file's format changed; make the fixture again"
    )


def test_the_fixture_names_every_kind_and_where_it_was_made_and_holds_nothing_but_hashes_and_counts():
    expected = json.loads(FIXTURE.read_text())
    assert set(expected) == {"what", "produced_on", "kinds"} and set(expected["kinds"]) == set(KINDS)
    assert expected["produced_on"].startswith("Darwin, arm64, python 3.12"), "one machine, and it is said which"
    for kind in KINDS:
        entry = expected["kinds"][kind]
        assert set(entry) == {"rows", "dataset", "params", "model"} and type(entry["rows"]) is int
        assert all(len(entry[key]) == 64 and int(entry[key], 16) >= 0 for key in ("dataset", "params", "model"))
    assert expected["kinds"]["ridge"]["dataset"] == expected["kinds"]["gbm"]["dataset"], "one row a decision"
    assert expected["kinds"]["seq"]["dataset"] != expected["kinds"]["ridge"]["dataset"], "and windows of four"
    assert len({expected["kinds"][kind]["model"] for kind in KINDS}) == 3


def test_the_stream_is_written_out_by_whole_numbers_and_is_the_same_every_time():
    quotes = stream()
    assert len(quotes) == BLOCKS * 30 and quotes == stream()
    plain = [[q.bid, q.ask, q.bid_size, q.ask_size, q.recv_ts_ns, q.seq] for q in quotes]
    assert hashlib.sha256(canonical(plain).encode("ascii")).hexdigest() == STREAM, (
        "the stream itself, before any feature or label: when this fails the generator above was changed"
    )
    ups = sum(COIN >> block & 1 for block in range(BLOCKS))
    assert 0 < ups < BLOCKS and quotes[-1].bid + 5 == 108_505 + JUMP * (2 * ups - BLOCKS)


if __name__ == "__main__":
    made = {kind: hashes(kind) for kind in KINDS}
    said = {
        "what": "tests/learn/test_portable.py: one tiny synthetic fit of each kind, as it came out on one machine",
        "produced_on": machine(),
        "kinds": made,
    }
    print(json.dumps(said, indent=2, sort_keys=True))
