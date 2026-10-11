"""Shared pieces for the learning tests: synthetic streams with a planted, known effect, and datasets of them."""
from __future__ import annotations

import random
from pathlib import Path

import yaml

from hftlab.calendar import Calendar
from hftlab.core import Quote
from hftlab.features import FeatureBuilder, FeatureConfig
from hftlab.labels import Labeller, LabelSpec
from hftlab.learn.dataset import Dataset, build
from hftlab.record import format as fmt
from tests.record.kit import a_store
from tests.venues.kit import EURUSD, MS, SECOND, T0, USDJPY, plain_costs

SIZE = 20_000
FEE = 2_000_000
COST = 10 * SIZE * 10 + 2 * FEE  # a round trip on a still book: one spread of 10 price units and two commissions
JUMP = 100  # price units: 20.00 USD on 20,000, against a round trip that costs 6.00
WIN = JUMP * SIZE * 10 - COST
HORIZON = SECOND
WEEK = 7 * 24 * 3_600 * SECOND
M = 1_000_000
CONFIG = FeatureConfig(lookbacks_ns=(500 * MS, SECOND), rate_window_ns=SECOND, range_window_ns=2 * SECOND,
                       stale_after_ns=5 * SECOND)  # fmt: skip
IMBALANCE = 4  # where a row keeps its size imbalance, in millionths


def planted(seed: int, blocks: int, *, effect: bool = True, start_ns: int = T0, first_seq: int = 1) -> list[Quote]:
    """A book that stands still but for one thing: a second of one-sided size, and then, if `effect`, a jump.

    Each block is three seconds of quotes a tenth of a second apart. In its second second the bid shows three
    times the ask's size, or the ask three times the bid's, by a seeded coin. At the start of its third second
    the mid jumps by `JUMP` towards the heavier side. So, after costs, a round trip of one second entered on a
    one-sided book makes exactly `WIN` in that direction, and every other one loses exactly `COST`.
    """
    rng, out, mid = random.Random(seed), [], 108_505
    for block in range(blocks):
        up = rng.random() < 0.5
        for tick in range(30):
            if tick == 20 and effect:
                mid += JUMP if up else -JUMP
            heavy = 10 <= tick < 20
            sizes = (3 * M if heavy and up else M, 3 * M if heavy and not up else M)
            at = start_ns + (block * 30 + tick) * 100 * MS
            out.append(Quote("EUR.USD", mid - 5, mid + 5, *sizes, at, first_seq + block * 30 + tick))
    return out


def features(config: FeatureConfig = CONFIG, calendar: Calendar | None = None) -> FeatureBuilder:
    return FeatureBuilder("EUR.USD", config, calendar or Calendar())


def labels(horizon_ns: int = HORIZON, costs=None, **barriers) -> Labeller:
    return Labeller(LabelSpec(horizon_ns, **barriers), EURUSD, costs or plain_costs(), Calendar())


def dataset(stream, **options) -> Dataset:
    return build(stream, features(), labels(), **options)


def record(root: Path, stream) -> Path:
    """Write a stream of quotes into a recorder's store, as day folders that say what their instruments are."""
    store = a_store(root)
    description = {"type": "instruments", "instruments": [fmt.describe(EURUSD), fmt.describe(USDJPY)]}
    for number, quote in enumerate(stream):
        store.write(fmt.QUOTE, quote.recv_ts_ns, quote.seq, quote.pair, quote.bid, quote.ask, quote.bid_size,
                    quote.ask_size)  # fmt: skip
        if number == 0:
            store.describe(description)
    store.close()
    return root


DAY = 24 * 3_600 * SECOND
SATURDAY = T0 + 16 * DAY  # 2025-10-25 08:53 UTC: the market is closed
OPEN_DAYS = (0, 1, 4, 5, 11, 12)  # days after T0 on which the market is open: two in each of three trading weeks


def weeks(seed: int, blocks: int = 30, *, on=OPEN_DAYS, effect: bool = True, last: int | None = None) -> list[Quote]:
    """A burst of the planted stream on each of some open days: three trading weeks of two days by default.

    `last` seeds the bursts of the last week differently, for two stores that agree on every week but that one.
    """
    out = []
    for number, day in enumerate(on):
        burst = seed * 100 + number if last is None or day < on[-2] else last * 100 + number
        out += planted(burst, blocks, effect=effect, start_ns=T0 + day * DAY, first_seq=len(out) + 1)
    return out


def learn_settings(folder: Path, **changes) -> Path:
    """A config/learn.yaml for the planted stream, with everything it writes under `folder`. Returns its path."""
    loaded = {
        "version": 1, "status": "placeholder", "state_dir": str(folder / "learn"),
        "lineup": str(folder / "trader" / "lineup.jsonl"), "pairs": ["EUR.USD"], "horizons_seconds": [1],
        "features": {"lookbacks_ms": [500, 1000], "rate_window_ms": 1000, "range_window_ms": 2000,
                     "stale_after_ms": 5000},
        "label": {"exit_wait_seconds": 2, "stuck_increments": 40}, "ridge_penalty": "1", "model": "ridge",
        "boosting": {"trees": 20, "max_depth": 3, "leaves": 8, "learning_rate_ppm": 200_000, "min_leaf_rows": 5,
                     "bins": 63, "l2_ppm": 1_000_000, "leaf_scale": 1000, "seed": 1},
        "validation": {"min_train_weeks": 1, "hold_out_weeks": 0, "seed": 1, "bootstrap_draws": 200,
                       "level_ppm": 950_000, "leak_checks": 4, "summary_bins": 5},
        "memory_budget_mb": 1000,
        "promotion": {"decision": None, "baseline_sleeve": "control", "min_trades": None, "min_days": None,
                      "max_drawdown_micro": None, "draws": None, "level_ppm": None, "seed": None},
        "drift": {"min_rows": None, "feature_shift_ppm": None, "calibration_gap_micro": None,
                  "spread_ratio_x100": None},
        "scorer": {"timeout_ms": 20, "restart_first_ms": 100, "restart_last_ms": 30_000},
    }  # fmt: skip
    for key, value in changes.items():
        loaded[key] = {**loaded[key], **value} if isinstance(loaded[key], dict) else value
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "learn.yaml"
    path.write_text(yaml.safe_dump(loaded), encoding="utf-8")
    return path
