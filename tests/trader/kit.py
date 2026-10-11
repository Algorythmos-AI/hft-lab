"""Shared pieces for the trader tests: a fake broker that lives on, and a trader that can be killed and restarted.

Everything is wired the way `python -m hftlab.trader` wires it: a `LiveEdge` over the venue, the real risk
gate, order manager and reconciler, the stage journal, and the tape. The clock is simulated and the fake broker
moves it, so a day of trading takes a moment and happens the same way every time.
"""
from __future__ import annotations

import json
from pathlib import Path

from hftlab.calendar import Calendar
from hftlab.core import InstrumentKind, SimClock
from hftlab.journal import Journal, read
from hftlab.safety import PaperGate
from hftlab.trader import LiveEdge, Trader
from hftlab.trader.config import Settings, parse
from hftlab.trader.replay import replay_day
from hftlab.venues.fake import FakeBroker, FakeConfig, FakeMarket
from hftlab.venues.supervisor import Backoff
from hftlab.venues.view import MarketDataView
from tests.exec.kit import WIDE
from tests.risk.kit import limits
from tests.venues.kit import EURUSD, MS, SECOND, T0, USDJPY, plain_costs

TRADED = (EURUSD, USDJPY)
PAIRS = (("EUR.USD", InstrumentKind.CASH), ("USD.JPY", InstrumentKind.CASH))
DIGEST = "the-test-settings"
NEVER = {"clean_days": None, "decision": None}


def raw_settings(**changes) -> dict:
    """The trader's settings as a loaded file: a busy control sleeve, and no promotion rule that could be met."""
    loaded = {
        "status": "placeholder", "state_dir": "unused", "venue": "ibkr_paper", "seed": 1,
        "pairs": [{"pair": "EUR.USD", "form": "cash"}, {"pair": "USD.JPY", "form": "cash"}],
        "poll_ms": 50, "beat_ms": 1_000, "reconcile_seconds": 60, "close_retry_seconds": 5, "fsync": False,
        "sleeves": [
            {"name": "idle", "kind": "idle"},
            {"name": "control", "kind": "control", "seed": 7, "entry_per_million": 150_000, "hold_seconds": 2,
             "retry_seconds": 1},
        ],
        "clean_day_minutes": None,
        "rollout": {
            "idle": {"start": "shadow", "promote": {"min_size": dict(NEVER), "normal": dict(NEVER)}},
            "control": {"start": "shadow", "promote": {"min_size": dict(NEVER), "normal": dict(NEVER)}},
        },
    }  # fmt: skip
    return {**loaded, **changes}


def put_stage(folder: Path, sleeve: str, to: str, *, at_ns: int = T0) -> None:
    """Write a sleeve's stage into the stage journal, as a promotion earned earlier would have left it."""
    journal = Journal(folder / "stages.jsonl")
    journal.open()
    journal.write("stage", sleeve=sleeve, before="shadow", to=to, cause="PROMOTED", detail="HFT-DEC-9999", at_ns=at_ns)
    journal.close()


class Pages:
    """Stands where the owner's pager would be, and keeps what it was asked to send."""

    def __init__(self) -> None:
        self.fired: list[str] = []
        self.resolved: list[str] = []

    def fire(self, name: str, title: str, message: str, priority: int = 4) -> bool:
        self.fired.append(name)
        return True

    def resolve(self, name: str, title: str, message: str) -> bool:
        self.resolved.append(name)
        return True


class Desk:
    """One broker that lives on, and a trader process on top of it that can be stopped, killed and started again."""

    def __init__(
        self,
        folder: Path,
        config: FakeConfig | None = None,
        *,
        paper: bool = True,
        settings: dict | None = None,
        changes: dict[str, object] | None = None,
        costs=None,
        reject=None,
        at_ns: int = T0,
        start: bool = True,
        scorer_times=None,
        edge: dict[str, object] | None = None,
    ) -> None:
        self.folder, self.paper, self.scorer_times = folder, paper, scorer_times
        self.edge_options = edge or {}  # how the tape is cut and counted: part_bytes, quota_bytes, fsync
        self.settings: Settings = parse(settings or raw_settings())
        self.risk = limits({**WIDE, **(changes or {})}) if paper else None
        self.costs = costs or plain_costs()
        self.clock = SimClock(at_ns)
        self.market = FakeMarket(TRADED, config, reject=reject, seed=3)
        self.switched: set[str] = set()
        self.pages = Pages()
        self.until = at_ns
        self.quoted = at_ns
        if start:
            self.start()

    # ---- the process ----------------------------------------------------------------------------------------------

    def start(self) -> Trader:
        """A process start: a new connection, a new trader, on the same files and the same broker."""
        lock = PaperGate(port=self.market.config.port, env={"MODE": "paper"})
        self.broker = FakeBroker(self.clock, self.market, gate=lock, backoff=Backoff(), auto_advance=True)
        source = self.broker if self.paper else MarketDataView(self.broker)
        self.edge = LiveEdge(
            source, self.clock, pairs=PAIRS, switches=lambda: frozenset(self.switched),
            requests=self.folder / "requests", should_stop=lambda: self.clock.now_ns() >= self.until,
            venue=self.broker if self.paper else None, **self.edge_options,
        )  # fmt: skip
        return self.session()

    def session(self) -> Trader:
        """A new session on the connection in hand: what the process does by itself after a rollover."""
        self.trader = Trader(
            self.edge, self.settings, self.edge.open(), model=self.costs, calendar=Calendar(), risk=self.risk,
            notify=self.pages, config_digest=DIGEST, folder=self.folder, scorer_times=self.scorer_times,
        )  # fmt: skip
        return self.trader

    def quotes(self, seconds: int, *, every_ms: int = 100, size: int | None = None) -> None:
        """A walking EUR.USD book and a still USD.JPY one, from where the last call left off.

        With `size`, every quote displays exactly that much on each side: too little, and a fill-or-kill order
        fills at the fake broker, which does not look at the size, and not on the shadow venue, which does.
        """
        count = seconds * 1_000 // every_ms
        if size is None:
            walk = {"count": count, "interval_ns": every_ms * MS, "mid": 108_505, "spread": 10, "step": 5}
            self.market.generate_quotes("EUR.USD", start_ns=self.quoted + MS, **walk)
        else:
            for number in range(count):
                bid = 108_500 + 5 * (number % 2)
                at = self.quoted + MS + number * every_ms * MS
                self.market.script_quote(at, "EUR.USD", bid, bid + 10, size, size)
        self.market.script_quote(self.quoted + MS, "USD.JPY", 151_230, 151_240)
        self.quoted += seconds * SECOND

    def run(self, seconds: int, *, quotes: bool = True, size: int | None = None) -> bool:
        """Let the trader take its turns for this long. True when it saw the day close."""
        if quotes:
            self.quotes(seconds, size=size)
        self.until = self.clock.now_ns() + seconds * SECOND
        return self.trader.run()

    def stop(self) -> dict[str, object]:
        """SIGTERM: the session is ended on purpose."""
        return self.trader.close()

    def restart(self) -> Trader:
        """The process is gone without a word (a kill), and a new one starts on the same files."""
        self.trader.abandon()
        return self.start()

    # ---- reading --------------------------------------------------------------------------------------------------

    def day(self) -> Path:
        return self.folder / "days" / self.trader.label

    def decisions(self) -> list[dict[str, object]]:
        return read(self.day() / "decisions.jsonl")[1]

    def kinds(self) -> list[str]:
        return [str(entry["type"]) for entry in self.decisions()]

    def of(self, kind: str) -> list[dict[str, object]]:
        return [entry for entry in self.decisions() if entry["type"] == kind]

    def outbox(self) -> list[dict[str, object]]:
        return read(self.folder / "outbox.jsonl")[1]

    def replay(self, scratch: Path):
        return replay_day(
            self.day(), scratch, self.settings, model=self.costs, calendar=Calendar(), risk=self.risk,
            config_digest=DIGEST,
        )  # fmt: skip

    def ask(self, what: str, subject: str, value: str = "", operator: str = "owner") -> None:
        """Leave a request for the running trader, as `python -m hftlab.trader --reset` or `--demote` does."""
        folder = self.folder / "requests"
        folder.mkdir(parents=True, exist_ok=True)
        said = {"what": what, "subject": subject, "value": value, "operator": operator}
        (folder / f"{len(list(folder.iterdir())) + 1:04d}-{what}.json").write_text(json.dumps(said))

    def held(self, pair: str = "EUR.USD", sleeve: str = "control") -> tuple[int, int, int]:
        """(the broker book's, the shadow book's, the broker's own) position of a sleeve's pair."""
        broker = self.trader.broker_book.position(sleeve, pair).quantity if self.trader.broker_book else 0
        shadow = self.trader.shadow_book.position(sleeve, pair).quantity
        return broker, shadow, self.positions().get(pair, 0)

    def positions(self) -> dict[str, int]:
        """What the broker itself holds, by pair."""
        return {pair: quantity for (pair, _), quantity in self.market.positions().items() if quantity}
