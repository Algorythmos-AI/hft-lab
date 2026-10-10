"""Made-up pre-flight samples, as the sampler writes them, for the tests of the gate and of the install script."""
from __future__ import annotations

import time
from pathlib import Path

from hftlab.ops.preflight import Sample, stamp

START = 1_791_763_200  # Monday 2026-10-12 00:00:00 UTC


def a_day(count: int = 1_440, *, every: int = 60, start: int = START, **numbers: int) -> list[Sample]:
    """One sample a minute for a day on a host with room to spare: 2.4 GB available of 4 GB, 14 GB of disk."""
    base = {"mem_available_kb": 2_400_000, "mem_total_kb": 4_028_000, "swap_free_kb": 0, "load_centi": 42,
            "disk_free_kb": 14_000_000}  # fmt: skip
    return [Sample(at_s=start + n * every, **{**base, **numbers}) for n in range(count)]


def line(sample: Sample) -> str:
    load = f"{sample.load_centi // 100}.{sample.load_centi % 100:02d}"
    return (
        f"{stamp(sample.at_s)} mem_available_kb={sample.mem_available_kb} "
        f"mem_total_kb={sample.mem_total_kb} swap_free_kb={sample.swap_free_kb} "
        f"load1={load} load5={load} load15={load} disk_free_kb={sample.disk_free_kb}"
    )


def recent(**numbers: int) -> list[Sample]:
    """A day of samples that ended an hour ago by the real clock, which is what the command reads."""
    return a_day(start=int(time.time()) - 86_400 - 3_600, **numbers)


def write(directory: Path, samples: list[Sample]) -> str:
    path = directory / "hft-preflight.log"
    path.write_text("\n".join(line(sample) for sample in samples) + "\n")
    return str(path)
