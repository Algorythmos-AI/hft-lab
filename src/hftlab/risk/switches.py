"""The two switches an operator has: files in the platform's state directory.

  KILL      no new entries; exits are still managed
  FLATTEN   no new entries, and every open position is closed

A switch is on while its file exists. What is in the file does not matter. The gate does not read files
itself: it is handed a function that says which switches are on, so a test hands it a set and the trader
hands it `file_switches` over the state directory. A directory that cannot be read raises `OSError`, which the
gate takes as a reason to stop entries: an unreadable switch is not an open one.
"""
from __future__ import annotations

from collections.abc import Callable, Collection
from pathlib import Path


def file_switches(directory: Path, names: Collection[str]) -> Callable[[], frozenset[str]]:
    """A reader of the switches in `directory`: each call returns the names whose file exists at that moment."""
    wanted = tuple(sorted(names))

    def read() -> frozenset[str]:
        if not directory.is_dir():
            raise FileNotFoundError(f"the state directory {directory} is not there, so its switches cannot be read")
        return frozenset(name for name in wanted if (directory / name).exists())

    return read
