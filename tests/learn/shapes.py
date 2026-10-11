"""Synthetic streams for the tests of a model that is not a straight line: a planted band, and noise.

Both are the still book of `tests.learn.kit.planted`, three seconds to a block, with a second of one-sided size
in the middle of each and a jump of the mid at the start of the third second. They differ in two things: how
one-sided the size is, and which way the jump goes.

  band    The size is three to one in six blocks of every ten and seven to one in the other four, each half on
          the bid and half on the ask. The mid jumps TOWARDS the heavier side after three to one, and AWAY from
          it after seven to one. Over every ten blocks the size imbalance and the jump are exactly uncorrelated
          (6 x 1/2 = 4 x 3/4), so a straight line through the imbalance has no slope to find. The effect is all
          there for a model that can ask "how one-sided", not only "which side".
  noise   The same sizes, and a jump whose direction is a coin of its own. Nothing known beforehand says which
          way it goes. Whatever a model finds in it, it has made up.
"""
from __future__ import annotations

import random

from hftlab.core import Quote
from tests.learn.kit import DAY, JUMP, OPEN_DAYS, M
from tests.venues.kit import MS, T0

BAND, NOISE = "band", "noise"
MILD, STRONG = 3, 7  # the heavier side's size over the lighter's: an imbalance of 1/2, and of 3/4
CYCLE = [(MILD, True)] * 3 + [(MILD, False)] * 3 + [(STRONG, True)] * 2 + [(STRONG, False)] * 2  # (ratio, on the bid)


def shaped(seed: int, blocks: int, law: str, *, start_ns: int = T0, first_seq: int = 1) -> list[Quote]:
    """`blocks` blocks of the law, ten at a time in a seeded order. `blocks` is a multiple of ten."""
    assert blocks % len(CYCLE) == 0 and law in (BAND, NOISE)
    rng, out, mid = random.Random(seed), [], 108_505
    for cycle in range(blocks // len(CYCLE)):
        order = list(CYCLE)
        rng.shuffle(order)
        for place, (ratio, on_bid) in enumerate(order):
            block = cycle * len(CYCLE) + place
            up = (on_bid == (ratio == MILD)) if law == BAND else rng.random() < 0.5
            for tick in range(30):
                if tick == 20:
                    mid += JUMP if up else -JUMP
                heavy = 10 <= tick < 20
                sizes = (ratio * M if heavy and on_bid else M, ratio * M if heavy and not on_bid else M)
                at = start_ns + (block * 30 + tick) * 100 * MS
                out.append(Quote("EUR.USD", mid - 5, mid + 5, *sizes, at, first_seq + block * 30 + tick))
    return out


def shaped_weeks(seed: int, law: str, blocks: int = 60, *, on=OPEN_DAYS) -> list[Quote]:
    """A burst of the law on each of some open days: three trading weeks of two days by default."""
    out: list[Quote] = []
    for number, day in enumerate(on):
        out += shaped(seed * 100 + number, blocks, law, start_ns=T0 + day * DAY, first_seq=len(out) + 1)
    return out
