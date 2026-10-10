"""Shared pieces for the host-layer tests: a host directory on tmp_path with a real recorder writing into it."""
from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from hftlab.calendar import Calendar
from hftlab.core import InstrumentKind, SimClock
from hftlab.ops.post import Outcome
from hftlab.ops.status import Sources
from hftlab.record.recorder import Recorder, RecorderSettings
from hftlab.strategy import ControlSleeve, IdleSleeve
from hftlab.venues.fake import FakeConfig, build
from hftlab.venues.supervisor import Backoff
from hftlab.venues.view import MarketDataView
from tests.record.kit import MS, ROOMY, SECOND, T0, a_store
from tests.shadow.kit import session, walk
from tests.support import ROOT
from tests.venues.kit import INSTRUMENTS

DAY = 86_400 * SECOND
OPEN = T0  # Wednesday 2026-10-14 03:00 UTC: the middle of a forex week
SATURDAY = T0 + 3 * DAY  # Saturday 2026-10-17 03:00 UTC: the market is shut
SETTINGS = RecorderSettings(pairs=(("EUR.USD", InstrumentKind.CASH), ("USD.JPY", InstrumentKind.CASH)))
WALK = {"interval_ns": 10 * MS, "mid": 108_505, "spread": 10, "step": 5}


class Named:
    """A quote source under another name. A status file on the host names the paper venue; the fake broker is `fake`."""

    def __init__(self, view: MarketDataView, name: str) -> None:
        self._view, self.name = view, name

    @property
    def state(self):
        return self._view.state

    def __getattr__(self, item: str):
        return getattr(self._view, item)


def sources(tmp_path: Path, **changes) -> Sources:
    """Where a host keeps things, under tmp_path, with a calendar that has no holidays."""
    state = tmp_path / "var"
    places = {
        "record_root": state / "record", "shadow_journal": state / "shadow" / "live.jsonl", "state": state,
        "calendar": Calendar(), "dead_man_ns": 60 * SECOND, "disk_warn_percent": 90,
    }  # fmt: skip
    return Sources(**{**places, **changes})


class Host:
    """A recorder over the fake broker writing into tmp_path/var/record, as on the host. The test moves the clock."""

    def __init__(
        self, tmp_path: Path, *, venue: str = "ibkr_paper", start_ns: int = OPEN, config=None, store=ROOMY, **options
    ) -> None:
        self.clock = SimClock(start_ns)
        self.broker = build(self.clock, INSTRUMENTS, config or FakeConfig(), seed=5, backoff=Backoff())
        self.sources = sources(tmp_path)
        self.root = self.sources.record_root
        self.store = a_store(self.root, store, opened=False, **options)
        source = Named(MarketDataView(self.broker), venue)
        self.recorder = Recorder(source, self.store, self.clock, SETTINGS, status_path=self.root / "status.json")

    def record(self, seconds: int, *, quotes: bool = True) -> Host:
        """Start if need be, and record `seconds` of a moving EUR.USD book (USD.JPY stays quiet: heartbeats)."""
        if self.store.run == 0:
            self.recorder.start()
        if quotes:
            walk = {**WALK, "count": seconds * 100}
            self.broker.market.generate_quotes("EUR.USD", start_ns=self.clock.now_ns() + MS, **walk)
        end = self.clock.now_ns() + seconds * SECOND
        while self.clock.now_ns() < end:
            self.clock.advance(20 * MS)
            self.recorder.step()
        return self

    def stop(self) -> Host:
        self.recorder.stop()
        return self


class Transport:
    """A stand-in for the network: it keeps what it was asked to send and answers as the test says."""

    def __init__(self, status: int | None = 200, error: str = "") -> None:
        self.answer = Outcome(status, error)
        self.sent: list[tuple[str, bytes, dict[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> Outcome:
        self.sent.append((url, body, dict(headers)))
        return self.answer


def a_shadow_session(tmp_path: Path, seed: int = 1):
    """One session of the idle and the control sleeve into the host's shadow journal. Returns its summary."""
    control = ControlSleeve(
        "control", seed=seed, sizes={"EUR.USD": 20_000, "USD.JPY": 20_000}, entry_per_million=20_000,
        hold_ns=5 * SECOND, retry_ns=SECOND,
    )  # fmt: skip
    return session(sources(tmp_path).shadow_journal, [IdleSleeve(), control], walk(seed, 2_000))[0]


@pytest.fixture
def configured(tmp_path, monkeypatch) -> list[str]:
    """The shipped settings as command-line options, with the recorder and the shadow runner pointed into

    tmp_path/var as on a host, and no address or key in the environment.
    """
    state = tmp_path / "var"
    recorder = yaml.safe_load((ROOT / "config" / "recorder.yaml").read_text())
    shadow = yaml.safe_load((ROOT / "config" / "shadow.yaml").read_text())
    (tmp_path / "recorder.yaml").write_text(yaml.safe_dump({**recorder, "root": str(state / "record")}))
    (tmp_path / "shadow.yaml").write_text(yaml.safe_dump({**shadow, "journal_dir": str(state / "shadow")}))
    shutil.copy(ROOT / "config" / "ops.yaml", tmp_path / "ops.yaml")
    monkeypatch.setenv("HFT_STATE_DIR", str(state))
    for name in ("HFT_ALERT_URL", "HFT_PUBLISH_URL", "HFT_PUBLISH_KEY_ID", "HFT_PUBLISH_KEY", "HFT_BACKUP_DIR"):
        monkeypatch.delenv(name, raising=False)
    return [
        "--config", str(tmp_path / "ops.yaml"), "--recorder-config", str(tmp_path / "recorder.yaml"),
        "--shadow-config", str(tmp_path / "shadow.yaml"), "--calendar", str(ROOT / "config" / "calendar"),
    ]  # fmt: skip
