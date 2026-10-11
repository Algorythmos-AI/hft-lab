"""The null benchmark of the Directional Change sleeve (ADR 0024): random walks as volatile as the stream in hand.

The pattern the published method is known for, a confirmed turn followed on average by a move of the same size
again, is also what a driftless random walk does. So a result of the sleeve on a stream says nothing until it
is set beside what the same sleeve does on walks with no structure at all and the same volatility.

`walk` makes one such walk from a stream of one pair's quotes. It keeps every tick: its time, its sequence
number, its sizes and its spread. Only the path of the mid is replaced. On each tick the mid moves up or down
by a fixed whole number of price increments, or stays, and the probability of a move is the one at which the
mean squared change of the mid per tick equals the stream's own, as an exact rational. The step is the smallest
number of increments for which that probability is not above one. The first tick keeps its mid.

Both sides of a quote have to lie on the price grid, and the mid of a walk keeps the first tick's position on
it. Where a tick's spread does not fit that position, the spread is widened by one increment, never narrowed:
the walk is at least as costly to trade as the stream.

Nothing here holds a generator. Whether tick `k` of walk `number` moves, and which way, is a hash of the seed,
the walk's number and `k`, so a walk is the same on every machine and from any starting point. A probability
is applied as a whole number of 2^63 parts, rounded down: it is too small by less than one part in 2^63.

`results` runs a sleeve on a number of walks. It is handed the function that runs a sleeve over a stream and
returns its result, because what runs a sleeve (the shadow runner) is not this package's to import.
`at_or_above` then says where the result on the real stream stands among them.
"""
from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Sequence
from fractions import Fraction
from math import isqrt
from typing import Final

from hftlab.core import Instrument, Quote

_PARTS: Final = 2**63


class NullError(ValueError):
    """A stream no walk can be matched to, or a walk that left the prices a quote can have."""


def mean_square_x4(stream: Sequence[Quote]) -> Fraction:
    """The mean squared change per tick of twice the mid: four times the mid's own, and exact."""
    if len(stream) < 2:
        raise NullError("a stream of fewer than two quotes has no change to match")
    doubled = [quote.mid_x2 for quote in stream]
    squares = sum((after - before) ** 2 for before, after in itertools.pairwise(doubled))
    return Fraction(squares, len(doubled) - 1)


def step_and_share(stream: Sequence[Quote], instrument: Instrument) -> tuple[int, Fraction]:
    """How far twice the mid moves when it moves, and how often it does, for a walk matched to the stream."""
    grid = 2 * instrument.price_increment  # the mid moves a whole increment, so twice the mid moves two
    wanted = mean_square_x4(stream) / (grid * grid)
    steps = max(1, isqrt(wanted.numerator // wanted.denominator))
    while steps * steps < wanted:
        steps += 1
    return steps * grid, wanted / (steps * steps)


def draw(seed: int, number: int, tick: int) -> tuple[int, int]:
    """A number below 2^63 and a sign, fixed by the seed, the walk and the tick. No generator, no hidden state."""
    digest = hashlib.blake2b(f"{seed}|{number}|{tick}".encode("ascii"), digest_size=8).digest()
    found = int.from_bytes(digest, "big")
    return found >> 1, 1 if found & 1 else -1


def walk(stream: Sequence[Quote], instrument: Instrument, *, seed: int, number: int) -> list[Quote]:
    """Walk `number` for this seed: the stream's ticks, with a driftless walk of the same volatility as its mid."""
    increment, pair = instrument.price_increment, instrument.pair
    if any(quote.pair != pair or quote.bid % increment or quote.ask % increment for quote in stream):
        raise NullError(f"a null walk is made from quotes of {pair} alone, on its price grid")
    step, share = step_and_share(stream, instrument)
    cut = share.numerator * _PARTS // share.denominator
    doubled, place = stream[0].mid_x2, stream[0].mid_x2 % (2 * increment)
    out = []
    for tick, quote in enumerate(stream):
        if tick:
            below, sign = draw(seed, number, tick)
            if below < cut:
                doubled += sign * step
        spread = quote.spread + (place - quote.spread) % (2 * increment)
        bid = (doubled - spread) // 2
        if bid <= 0:
            raise NullError("the walk reached a price that is not above zero")
        out.append(Quote(pair, bid, bid + spread, quote.bid_size, quote.ask_size, quote.recv_ts_ns, quote.seq))
    return out


def results(
    result_of: Callable[[Sequence[Quote]], int], stream: Sequence[Quote], instrument: Instrument, *, walks: int,
    seed: int,
) -> tuple[int, ...]:  # fmt: skip
    """What `result_of` returns on each of `walks` null walks of the stream, in the walks' order."""
    if type(walks) is not int or walks < 1:
        raise NullError("the number of walks is a whole number of at least one")
    return tuple(result_of(walk(stream, instrument, seed=seed, number=number)) for number in range(walks))


def at_or_above(observed: int, null: Sequence[int]) -> Fraction:
    """The share of results that are at least `observed`, the observed one counted among them.

    With 19 walks it is 1/20 when no walk did as well as the stream, and it is never zero: a finite number of
    walks cannot show that something never happens.
    """
    if not null:
        raise NullError("there is no null to read a result against")
    return Fraction(1 + sum(result >= observed for result in null), 1 + len(null))
