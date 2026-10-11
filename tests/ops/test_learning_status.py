"""The learning part of the status screen and of the snapshot: there once training has registered, absent before.

The lineup journal is written by the functions the training run and the weekly decision write it with. The
snapshot is held to the contract file as it stands; the contract is not changed here.
"""
from __future__ import annotations

from hftlab import lineup
from hftlab.lineup import Lineup, Named
from hftlab.ops import status
from hftlab.ops.status import read, render
from hftlab.publish import contract
from hftlab.publish.snapshot import build
from tests.ops.kit import OPEN, SECOND, sources
from tests.support import ROOT
from tests.trader.kit import Pages

SCHEMA = contract.load(ROOT / contract.CONTRACT)
PHASES = {f"F{n}": "open" for n in range(8)}
A, B = Named("a" * 64, "1" * 64), Named("b" * 64, "2" * 64)
HOUR = 3_600 * SECOND


def host(tmp_path):
    path = tmp_path / "var" / "trader" / lineup.FILE
    return path, sources(tmp_path, lineup=path)


def snap(reading) -> dict:
    return build(reading, phases=PHASES, version="0123456789ab")


def test_before_anything_is_registered_there_is_no_learning_part(tmp_path):
    places = host(tmp_path)[1]
    for reading in (read(sources(tmp_path), OPEN), read(places, OPEN)):
        assert reading.learning is None and reading.problems == ()
        assert "learning" not in snap(reading), "absent, never zeros"
        assert "learning   nothing registered" in render(reading)
    assert status.load_sources(state=tmp_path).lineup.name == lineup.FILE, "beside the trader's journals"


def test_the_lineup_is_read_and_published_as_the_contract_defines_it(tmp_path):
    path, places = host(tmp_path)
    found = lineup.append(path, Lineup().registered(A), action=lineup.REGISTER, at_ns=OPEN)
    reading = read(places, OPEN + HOUR)
    assert reading.learning == status.LearningReading(None, 0, 1, OPEN, None, None, None, None)
    part = snap(reading)["learning"]
    assert part == {"champion": None, "challengers": 0, "last_training_at": "2026-10-14T03:00:00Z",
                    "last_promotion_at": None, "last_rollback_at": None}  # fmt: skip
    assert contract.problems(snap(reading), SCHEMA) == []

    found = lineup.append(path, found.kept(), action=lineup.KEEP, at_ns=OPEN + HOUR)
    found = lineup.append(path, found.promoted(A), action=lineup.PROMOTE, at_ns=OPEN + 2 * HOUR)
    found = lineup.append(path, found.registered(B), action=lineup.REGISTER, at_ns=OPEN + 3 * HOUR)
    found = lineup.append(path, found.kept(), action=lineup.KEEP, at_ns=OPEN + 4 * HOUR)
    lineup.append(path, found, action=lineup.DRILL, at_ns=OPEN + 5 * HOUR, reasons=("passed",))
    reading = read(places, OPEN + 6 * HOUR)
    assert reading.learning == status.LearningReading(
        "a" * 12, 1, 0, OPEN + 3 * HOUR, OPEN + 2 * HOUR, None, OPEN + 5 * HOUR, True
    )
    published = snap(reading)
    assert published["learning"] == {
        "champion": "aaaaaaaaaaaa", "challengers": 1, "last_training_at": "2026-10-14T06:00:00Z",
        "last_promotion_at": "2026-10-14T05:00:00Z", "last_rollback_at": None,
    }  # fmt: skip
    assert contract.problems(published, SCHEMA) == [] and reading.problems == ()
    assert "a" * 13 not in str(published), "an id of twelve characters, and nothing more of the model"
    shown = render(reading)
    assert "learning   champion aaaaaaaaaaaa; 1 challenger(s) in shadow; 0 candidate(s) waiting" in shown
    assert "rollback never; drill passed" in shown

    lineup.append(path, lineup.read(path).rolled_back(), action=lineup.ROLL_BACK, at_ns=OPEN + 7 * HOUR)
    after = snap(read(places, OPEN + 8 * HOUR))["learning"]
    assert (after["champion"], after["last_rollback_at"]) == (None, "2026-10-14T10:00:00Z")


def test_a_failed_drill_warns_and_a_journal_that_does_not_verify_is_bad_paged_and_publishes_no_part(tmp_path):
    path, places = host(tmp_path)
    found = lineup.append(path, Lineup().registered(A), action=lineup.REGISTER, at_ns=OPEN)
    lineup.append(path, found, action=lineup.DRILL, at_ns=OPEN + HOUR, reasons=("failed",))
    reading = read(places, OPEN + 2 * HOUR)
    assert [(p.code, p.level) for p in reading.problems] == [("rollback_drill_failed", "warn")]
    assert "drill FAILED" in render(reading) and contract.problems(snap(reading), SCHEMA) == []
    path.write_bytes(path.read_bytes().replace(b'"register"', b'"promote"', 1))  # a line was edited
    reading = read(places, OPEN + 2 * HOUR)
    assert reading.learning is None and [(p.code, p.level) for p in reading.problems] == [
        ("lineup_journal_broken", "bad")
    ]
    published = snap(reading)
    assert "learning" not in published and published["health"]["reasons"] == ["lineup_journal_broken"]
    assert contract.problems(published, SCHEMA) == [] and "lineup_journal_broken" in status.PAGED

    class Alerts(Pages):
        def firing(self) -> list[str]:
            return []

    pages = Alerts()
    status.page(reading, pages)
    assert pages.fired == ["lineup_journal_broken"]
