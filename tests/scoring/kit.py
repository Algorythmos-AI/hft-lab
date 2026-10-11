"""Shared pieces for the tests of the scorer process: two model files, rows by the thousand, and stand-in children."""
from __future__ import annotations

import functools
import sys
import time
from collections.abc import Callable
from pathlib import Path

from hftlab.core import SimClock
from hftlab.features import FeatureRow
from hftlab.models import BUY, SELL, Model, ModelRegistry, Tree, Trees
from hftlab.models.baseline import number
from hftlab.scoring.client import ProcessScorer, Times
from tests.learn.kit import IMBALANCE, dataset, planted
from tests.models.test_models import a_model as ridge_model
from tests.models.test_trees import a_model as tree_model
from tests.venues.kit import MS, T0

WIDTH = 10
PATIENT = Times(timeout_ns=20_000 * MS, start_ns=20_000 * MS, restart_first_ns=100 * MS, restart_last_ns=800 * MS)
QUICK = Times(timeout_ns=150 * MS, start_ns=20_000 * MS, restart_first_ns=100 * MS, restart_last_ns=800 * MS)

# Four trees a side over the ten features, written by hand: no library is involved (ADR 0025).
#        f4 <= 0                 f0 <= -3              f9 <= 7
#        /     \                 /      \              /     \
#   f1 <= -2   leaf 2        leaf 0   f2 <= 5       leaf 0  leaf 1
#    /    \                            /    \
# leaf 0  leaf 1                   leaf 1  leaf 2
_A = Tree(feature=(4, 1), threshold=(0, -2), left=(1, ~0), right=(~2, ~1), leaves=(9_500, -4_000, 2_250))
_B = Tree(feature=(0, 2), threshold=(-3, 5), left=(~0, ~1), right=(1, ~2), leaves=(-7_000, 6_500, 1_000))
_C = Tree(feature=(9,), threshold=(7,), left=(~0,), right=(~1,), leaves=(-3_333, 4_444))
_D = Tree((), (), (), (), (-1_500,))


def trees() -> Model:
    """A model file of boosted trees whose two sides disagree, agree and tie on different rows."""
    fit = Trees(1000, 10, WIDTH, {BUY: (_A, _B, _D), SELL: (_C, _B, _D)}, {"trees": 3, "leaf_scale": 1000}, "by hand")
    return tree_model(fit)


def ridge() -> Model:
    """A ridge model fitted exactly to the planted stream."""
    return ridge_model()


@functools.cache
def _trained_on() -> tuple[tuple[int, ...], ...]:
    return tuple(row.features for row in dataset(planted(3, 6)).rows)


def rows(count: int, seed: int = 1) -> list[FeatureRow]:
    """`count` rows of ten whole numbers each, fixed by the seed.

    Every other row is of small numbers, so that every leaf of the hand-written trees is reached. The rows
    between are rows of the planted stream with the size imbalance moved about, where the ridge model takes
    either side or neither.
    """
    seen, out = _trained_on(), []
    for n in range(1, count + 1):
        if n % 2:
            values = tuple(number(seed, n, k) % 21 - 10 for k in range(WIDTH))
        else:
            base = seen[number(seed, n) % len(seen)]
            values = tuple(
                value + (number(seed, n, k) % 200_001 - 100_000 if k == IMBALANCE else number(seed, n, k) % 5 - 2)
                for k, value in enumerate(base)
            )
        out.append(FeatureRow("EUR.USD", n, T0 + n, values))
    return out


def kept(folder: Path, model: Model) -> Path:
    """A folder of model files holding this one, as the trader's registry keeps them."""
    ModelRegistry(folder).put(model)
    return folder


class Missed:
    """Stands where the session's record would be, and keeps the rows that were not scored."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, str]] = []

    def __call__(self, row: FeatureRow, why: str) -> None:
        self.rows.append((row.n, why))

    @property
    def reasons(self) -> list[str]:
        return [why for _, why in self.rows]


def started(folder: Path, model: Model, times: Times = PATIENT, **options) -> tuple[ProcessScorer, SimClock, Missed]:
    """A scorer process for this model, the clock its back-off is measured on, and what it did not score."""
    clock, missed = SimClock(T0), Missed()
    return ProcessScorer(model, kept(folder, model), times, clock=clock, missed=missed, **options), clock, missed


def until(condition: Callable[[], bool], seconds: int = 20) -> bool:
    """Wait in real time, which is what a real process takes to start, until the condition holds."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def ready(scorer: ProcessScorer) -> bool:
    """Let a starting child's answer be taken, and say whether it has come."""
    scorer.tend()
    return scorer.health == "ready"


# A child that speaks the format and misbehaves in one chosen way. It answers the load truthfully unless told not to.
STAND_IN = '''
import json, sys, time
from hftlab.scoring import wire
how, model = sys.argv[1], sys.argv[2]
out = sys.stdout.buffer
def send(raw):
    out.write(raw)
    out.flush()
if how == "asleep":
    time.sleep(600)
line = wire.read_frame(sys.stdin.buffer.read)
send(wire.frame(wire.loaded("f" * 64 if how == "another_model_loaded" else model)))
while True:
    line = wire.read_frame(sys.stdin.buffer.read)
    if line is None:
        break
    if how == "mute":
        continue
    reply = {"model": model, "side": "BUY", "expected_micro": 7}
    if how == "garbage":
        send(wire.frame("not json at all"))
    elif how == "another_version":
        send(bytes([9]) + (2).to_bytes(4, "big") + b"{}")
    elif how == "too_long":
        send(bytes([wire.VERSION]) + (wire.LIMIT + 1).to_bytes(4, "big"))
        time.sleep(600)
    elif how == "another_model":
        send(wire.frame(json.dumps({**reply, "model": "e" * 64})))
    elif how == "a_float":
        send(wire.frame(json.dumps({**reply, "expected_micro": 7.5})))
    elif how == "another_key":
        send(wire.frame(json.dumps({**reply, "also": 1})))
    elif how == "no_side":
        send(wire.frame(json.dumps({**reply, "side": "UP"})))
    elif how == "a_list":
        send(wire.frame("[1,2,3]"))
    elif how == "refuses":
        send(wire.frame(wire.refused("no")))
    elif how == "leaves":
        sys.exit(1)
'''


def stand_in(folder: Path, how: str, model: Model) -> list[str]:
    """The command that starts a stand-in child misbehaving in this way."""
    script = folder / "stand_in.py"
    script.write_text(STAND_IN)
    return [sys.executable, str(script), how, model.content_hash]
