"""Seeded randomness without a generator: a number fixed by the seed and by what it is drawn for.

The random baseline, the bootstrap and the coin of every three-way report draw from here. There is no state and
no position in a sequence, so the same seed and the same row give the same number on any machine, in any order,
from any starting point.
"""
from __future__ import annotations

import hashlib


def number(seed: int, *parts: int) -> int:
    """A 64-bit number fixed by the seed and the parts."""
    text = "|".join(str(part) for part in (seed, *parts))
    return int.from_bytes(hashlib.blake2b(text.encode("ascii"), digest_size=8).digest(), "big")
