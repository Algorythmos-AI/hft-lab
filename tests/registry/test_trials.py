"""The trial registry: one number code can read, append-only, never decreasing."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.registry import global_trial_count, trial_history
from hftlab.registry.trials import REGISTRY, TrialRow
from tests.support import ROOT


def registry(tmp_path, body: str):
    path = tmp_path / "trial_registry.yaml"
    path.write_text(body)
    return path


def test_the_shipped_registry_is_where_the_code_looks_and_starts_at_zero():
    assert REGISTRY == ROOT / "config" / "trial_registry.yaml" and REGISTRY.is_file()
    assert trial_history() == [] and global_trial_count() == 0


def test_the_count_is_the_newest_accepted_total(tmp_path):
    path = registry(
        tmp_path,
        "history:\n"
        "  - {decision: HFT-DEC-0003, total: 12, status: accepted}\n"
        "  - {decision: HFT-DEC-0005, total: 20, status: accepted}\n"
        "  - {decision: HFT-DEC-0008, total: 26, status: proposed}\n",
    )
    assert trial_history(path)[0] == TrialRow("HFT-DEC-0003", 12, "accepted")
    assert global_trial_count(path) == 20


def test_a_proposed_row_does_not_count_yet(tmp_path):
    path = registry(tmp_path, "history:\n  - {decision: HFT-DEC-0003, total: 12, status: proposed}\n")
    assert global_trial_count(path) == 0


@pytest.mark.parametrize(
    "body",
    [
        "history:\n  - {decision: HFT-DEC-0003, total: 12, status: accepted}\n"
        "  - {decision: HFT-DEC-0004, total: 11, status: accepted}\n",
        "history:\n  - {decision: DEC-0003, total: 12, status: accepted}\n",
        "history:\n  - {decision: HFT-EXP-0003, total: 12, status: accepted}\n",
        "history:\n  - {decision: HFT-DEC-0003, total: 12.0, status: accepted}\n",
        "history:\n  - {decision: HFT-DEC-0003, total: '12', status: accepted}\n",
        "history:\n  - {decision: HFT-DEC-0003, total: true, status: accepted}\n",
        "history:\n  - {decision: HFT-DEC-0003, total: -1, status: accepted}\n",
        "history:\n  - {decision: HFT-DEC-0003, total: 12, status: done}\n",
        "history:\n  - {decision: HFT-DEC-0003, total: 12}\n",
        "history:\n  - {decision: HFT-DEC-0003, total: 12, status: accepted, note: extra}\n",
        "history:\n  - just text\n",
        "history: {}\n",
        "rows: []\n",
        "- a list\n",
        "",
    ],
)
def test_a_malformed_registry_is_refused(tmp_path, body):
    with pytest.raises(ValueError):
        trial_history(registry(tmp_path, body))


@given(st.lists(st.tuples(st.integers(0, 50), st.sampled_from(["accepted", "proposed"])), max_size=8))
def test_totals_accumulate_and_the_count_never_decreases(steps):
    total, lines, accepted = 0, [], 0
    for number, (step, status) in enumerate(steps, 1):
        total += step
        lines.append(f"  - {{decision: HFT-DEC-{number:04d}, total: {total}, status: {status}}}\n")
        if status == "accepted":
            accepted = total
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trial_registry.yaml"
        path.write_text("history:\n" + "".join(lines) if lines else "history: []\n")
        assert global_trial_count(path) == accepted
        assert [row.total for row in trial_history(path)] == sorted(row.total for row in trial_history(path))
