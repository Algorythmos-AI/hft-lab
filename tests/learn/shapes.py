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

And one for a model that reads a sequence (`ordered`): a still book whose size takes one of three states for
a second, then one of the three for another second, and whose mid then jumps by a rule of the ORDER of the two.

  order   Call the states 0, 1 and 2. The mid jumps up when the second is the one after the first (0 then 1,
          1 then 2, 2 then 0), down when it is the one before, and stays when they are the same. Swap the two
          and the jump turns round. Every nine blocks hold each of the nine pairs once, so whatever state a
          row shows, the jump after it is up, down and none equally often: a model of one row has nothing to
          find, whatever its shape, and a trade on any single row expects a loss.
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


ORDER = "order"
STATES = [(1, 3), (3, 1), (39, 1)]  # (bid size, ask size) in millions: an imbalance of -1/2, +1/2 and +19/20
PAIRS = [(first, second) for first in range(3) for second in range(3)]
TICK = 200 * MS  # between quotes: five to the second, so that a one-second label holds exactly the second state


def ordered(seed: int, blocks: int, law: str, *, start_ns: int = T0, first_seq: int = 1) -> list[Quote]:
    """`blocks` blocks of six seconds, nine at a time in a seeded order. `blocks` is a multiple of nine.

    Thirty quotes to a block. Quotes 10 to 14 show the first state, 15 to 19 the second, and the rest an even
    book. On quote 20 the mid jumps: by the order of the two states (`ORDER`), or by a draw of its own with
    the same three outcomes (`NOISE`). A round trip of one second entered on quotes 15 to 19 holds the jump.
    """
    assert blocks % len(PAIRS) == 0 and law in (ORDER, NOISE)
    rng, out, mid = random.Random(seed), [], 108_505
    for cycle in range(blocks // len(PAIRS)):
        order = list(PAIRS)
        rng.shuffle(order)
        for place, (first, second) in enumerate(order):
            block = cycle * len(PAIRS) + place
            turn = (second - first) % 3 if law == ORDER else rng.randrange(3)
            for tick in range(30):
                if tick == 20:
                    mid += (0, JUMP, -JUMP)[turn]
                sizes = STATES[first] if 10 <= tick < 15 else STATES[second] if 15 <= tick < 20 else (1, 1)
                at = start_ns + (block * 30 + tick) * TICK
                seq = first_seq + block * 30 + tick
                out.append(Quote("EUR.USD", mid - 5, mid + 5, sizes[0] * M, sizes[1] * M, at, seq))
    return out


def ordered_weeks(seed: int, law: str, blocks: int = 63, *, on=OPEN_DAYS) -> list[Quote]:
    """A burst of the law on each of some open days: three trading weeks of two days by default."""
    out: list[Quote] = []
    for number, day in enumerate(on):
        out += ordered(seed * 100 + number, blocks, law, start_ns=T0 + day * DAY, first_seq=len(out) + 1)
    return out
