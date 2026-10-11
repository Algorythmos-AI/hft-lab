"""The trial ledger: every configuration that was evaluated, written down by the code that evaluated it.

`config/trial_registry.yaml` is the count a reviewer signs: one row per decision record. This is the count the
code keeps itself, on the host, so that the first cannot fall behind unnoticed. It is a hash-chained journal
(`hftlab.journal`), appended to and never edited:

    {"type": "trial", "config": "<hash of the frozen configuration>", "what": "walk_forward", "subject": "..."}

A trial is a distinct configuration: evaluating the same frozen configuration twice on development data is one
trial. `what` is `walk_forward` for development data and `hold_out` for a read of the untouched weeks, and a
hold-out is read once per configuration and subject: `spent` says whether it has been.

The number that deflates a statistic is `counted_trials`: the larger of the signed registry's total and this
ledger's, so neither a forgotten registry row nor a missing ledger can make a result look better than it is.
"""
from __future__ import annotations

from pathlib import Path
from typing import Final

from hftlab.core import _checks
from hftlab.journal import Journal, read
from hftlab.registry.trials import global_trial_count

KINDS: Final = ("walk_forward", "hold_out")


class TrialLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[dict[str, object]]:
        """Every trial written so far, oldest first. A ledger whose chain is broken is an error, never repaired."""
        return [entry for entry in read(self.path)[1] if entry.get("type") == "trial"]

    def record(self, config: str, what: str, subject: str) -> None:
        """Write one evaluation down, durably, before its result is looked at."""
        _checks.token("config", config)
        _checks.token("subject", subject)
        if what not in KINDS:
            raise ValueError(f"a trial is one of {KINDS}, not {what!r}")
        journal = Journal(self.path, fsync=True)
        journal.open()
        try:
            journal.write("trial", config=config, what=what, subject=subject)
            journal.flush()
        finally:
            journal.close()

    def trials(self) -> int:
        """How many distinct configurations have been evaluated."""
        return len({entry["config"] for entry in self.entries()})

    def spent(self, config: str, subject: str) -> bool:
        """Has this configuration already read this hold-out?"""
        wanted = (config, "hold_out", subject)
        return any((entry.get("config"), entry.get("what"), entry.get("subject")) == wanted for entry in self.entries())


def counted_trials(ledger: TrialLedger | None = None, registry: Path | None = None) -> int:
    """The trial count in force: the signed registry's total or the ledger's own, whichever is larger."""
    return max(global_trial_count(registry), ledger.trials() if ledger is not None else 0)
