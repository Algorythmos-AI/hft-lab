"""One session of the trader: a single loop, on one thread, from the venue's events to the two books (ADR 0019).

    a turn (hftlab.trader.tape.Step): the venue's events, the time, and now and then a beat
        -> the order manager takes every event: the risk gate sees it first, then the broker book
        -> a beat: the switches as read, a quiet book confirmed, what an operator asked for
        -> the connection is made safe (the manager's `reconcile`), or the reconciler runs on its schedule
        -> the shadow venue is fed the quote and says what became of earlier orders: the shadow book
        -> the trader's own closes: FLATTEN, a position inherited from before a restart, a shadow book ahead
        -> each sleeve is shown its news and the events, and says what it wants
             an intent -> the risk gate (through the order manager) -> the guarded venue,
             and the same order, at the same moment, on the shadow venue

Two books, always (ADR 0003). The shadow book is the result of record. The broker book is evidence: it is what
the order manager built from the venue's fills. In a stage that places paper orders a sleeve's position view
is the broker book's, because that is what can be closed, and its results are the shadow book's.

Nothing here reads a clock, a file or the venue. The edge hands every turn its time and its inputs, live or
from a recording, so the decisions are a function of the tape: `hftlab.trader.replay` runs a recorded day
through this same class and gets the same journal, byte for byte (ADR 0005).

No order reaches a broker from this module. The one venue it places on is the shadow venue it builds itself;
everything meant for the broker is proposed to the order manager, which asks the risk gate
(tests/trader/test_structure.py).
"""
from __future__ import annotations

import json
import os
from collections import deque
from collections.abc import Sequence
from dataclasses import replace
from enum import Enum, unique
from pathlib import Path
from typing import Any, Final, Protocol

from hftlab.book import DESK, Book, Reconciler, SleeveView
from hftlab.calendar import Calendar, new_york
from hftlab.core import (
    Fill,
    Instrument,
    Order,
    OrderEvent,
    OrderState,
    OrderType,
    Quote,
    Side,
    SimClock,
    TimeInForce,
    client_order_id,
    sleeve_of,
)
from hftlab.exec import TERMINAL, OrderManager, Outbox, Stage
from hftlab.exec.manager import Asked
from hftlab.exec.outbox import commission_from, fill_fields, fill_from
from hftlab.journal import Journal, JournalError, JournalState, read, verify
from hftlab.lineup import FILE as LINEUP
from hftlab.lineup.journal import LineupError
from hftlab.lineup.serve import lineup_or_none, make
from hftlab.record.format import Heartbeat, describe
from hftlab.risk import Reason, RiskConfig, RiskGate
from hftlab.scoring.client import READY, Scorers, Times
from hftlab.scoring.record import SUFFIX as ABSTAINED
from hftlab.shadow.__main__ import make_sleeves, minimum_sizes
from hftlab.strategy import Action, Cancel, Event, Intent, Placed, Refused, Sleeve
from hftlab.trader.config import Settings
from hftlab.trader.slippage import Matched, report
from hftlab.trader.stages import BREAK, LATCH, MIN_SIZE, OFF, PAPER, SHADOW, STEPPED_DOWN, Change, Stages, day_of
from hftlab.trader.tape import Beat, Edge, Opened, Request, Step
from hftlab.venues.port import (
    CommissionEvent,
    ConnectionEvent,
    ConnectionState,
    OpenOrder,
    VenueError,
    VenueNotice,
    VenuePosition,
)
from hftlab.venues.sim import CostModel, SimVenue

ANSWERS_PER_EVENT: Final = 64
STATE: Final = ("outbox", "risk", "reconcile", "stages", "shadow")  # the journals that outlive a day, by file name
SEEDS: Final = 1_000_000  # the shadow venue's seed is the configured one times this, plus the session's number
SLEEVE, FLATTEN, INHERIT, ALIGN = "sleeve", "flatten", "inherit", "align"  # where an order came from
UNVERIFIED: Final = -1  # in a mark, where the number of entries would be: the journal's chain does not verify
_SLEEVE_LATCHES: Final = (Reason.DAILY_LOSS_SLEEVE.value, Reason.DRAWDOWN_SLEEVE.value)
_ENDED: Final = {
    Stage.FILLED: OrderState.FILLED,
    Stage.CANCELLED: OrderState.CANCELLED,
    Stage.EXPIRED: OrderState.EXPIRED,
    Stage.REJECTED: OrderState.REJECTED,
}


class TraderRefused(Exception):
    """The trader will not start as it is set up. A person is needed; a restart is not."""


@unique
class Why(Enum):
    """Why the trader itself took an intent no further. The risk gate's and the order manager's codes are theirs."""

    UNKNOWN_PAIR = "UNKNOWN_PAIR"
    NOT_AN_ORDER = "NOT_AN_ORDER"  # the intent cannot be written as an order
    STAGE_EXITS_ONLY = "STAGE_EXITS_ONLY"  # the sleeve is stepped down: only an order that reduces may go
    INHERITED_POSITION = "INHERITED_POSITION"  # the pair holds a position from before this session; it is being closed
    SHADOW_REFUSED = "SHADOW_REFUSED"  # the shadow venue would not take it
    NOT_THIS_SLEEVES_ORDER = "NOT_THIS_SLEEVES_ORDER"
    NOTHING_TO_REDUCE = "NOTHING_TO_REDUCE"  # the shadow book holds nothing this exit could close


class Notify(Protocol):
    """How the owner is told. `hftlab.ops.alerts.Alerts` is one; a replay is handed one that says nothing."""

    def fire(self, name: str, title: str, message: str, priority: int = 4) -> bool: ...

    def resolve(self, name: str, title: str, message: str) -> bool: ...


class Silent:
    def fire(self, name: str, title: str, message: str, priority: int = 4) -> bool:
        return False

    def resolve(self, name: str, title: str, message: str) -> bool:
        return False


class _Listening:
    """The venue's questions, passed on, with every execution it lists kept for the trader to look at."""

    def __init__(self, asks: Asked, heard: list[Fill]) -> None:
        self._asks, self._heard = asks, heard

    def open_orders(self) -> tuple[OpenOrder, ...]:
        return self._asks.open_orders()

    def positions(self) -> tuple[VenuePosition, ...]:
        return self._asks.positions()

    def executions_since(self, ts_ns: int) -> tuple[Fill, ...]:
        found = self._asks.executions_since(ts_ns)
        self._heard.extend(found)
        return found


def day_label(day: int) -> str:
    """A trading day as a folder name: the New York date of the rollover that closes it."""
    return new_york(day).date().isoformat()


def mark(path: Path) -> list[object]:
    """Where a journal stands: its entries, its last hash and its length in bytes, without a torn last line."""
    state: JournalState = verify(path)
    return [state.entries, state.last_hash, state.good_bytes]


def lineup_mark(path: Path) -> list[object]:
    """Where the lineup journal stands. One that does not verify is marked as such, by its length: it serves nothing."""
    try:
        return mark(path)
    except JournalError:
        return [UNVERIFIED, "", path.stat().st_size]


def _opposes(side: Side, held: int, quantity: int) -> bool:
    """Would this order only bring a position of `held` towards zero, and not through it?"""
    return held != 0 and side.sign * held < 0 and quantity <= abs(held)


class Trader:
    def __init__(
        self,
        edge: Edge,
        settings: Settings,
        opened: Opened,
        *,
        model: CostModel,
        calendar: Calendar,
        risk: RiskConfig | None,
        notify: Notify,
        config_digest: str,
        folder: Path | None = None,
        status: bool = True,
        scorer_times: Times | None = None,
        abstained: Path | None = None,
    ) -> None:
        self._edge, self._settings, self._model, self._risk, self._notify = edge, settings, model, risk, notify
        self._folder = settings.state_dir if folder is None else folder
        self._clock = SimClock(opened.at_ns)
        self._instruments = {instrument.pair: instrument for instrument in opened.instruments}
        self._day = day_of(opened.at_ns)
        self.label = day_label(self._day)
        days = self._folder / "days" / self.label
        paths = {"decisions": days / "decisions.jsonl", **{name: self._folder / f"{name}.jsonl" for name in STATE}}
        marks = {name: mark(path) for name, path in paths.items()}  # before anything of this session is written
        marks["lineup"] = lineup_mark(self._folder / LINEUP)  # written by the weekly decision (ADR 0023)
        fsync = settings.fsync

        self._stages = Stages(
            Journal(paths["stages"], fsync=fsync), settings.rollout, calendar=calendar, in_force=risk is not None,
            min_day_ns=settings.clean_day_ns,
        )  # fmt: skip
        above = self._stages.above_shadow()
        if above and risk is None:
            said = f"{', '.join(above)} may place paper orders by the stage journal"
            raise TraderRefused(f"{said}, and the risk limits are not in force")
        if risk is not None and edge.venue is None:
            raise TraderRefused("the risk limits are in force and this run was given no venue to send orders on")
        self.paper = risk is not None
        tapes = len(list(days.glob("session-*.tape.jsonl"))) if days.is_dir() else 0
        head = {"journals": marks, "config": config_digest, "day": self.label, "paper": self.paper}
        edge.begin(days / f"session-{tapes + 1:03d}.tape.jsonl", head)
        self._stages.begin(opened.at_ns)

        # ---- the shadow side: its own venue, its own book, its own record of fills ------------------------------
        self._shadow_book = Book(self._instruments.values())
        self._shadow_fills: dict[str, int] = {}  # fills ever booked in the shadow book, by sleeve
        before = self._restore_shadow(paths["shadow"])
        self._shadow_journal = Journal(paths["shadow"], fsync=fsync)
        self._shadow_journal.open()
        self._shadow_journal.write("start", at_ns=opened.at_ns, session=before + 1)
        self._shadow_journal.flush()
        seed = settings.seed * SEEDS + before + 1
        self._shadow = SimVenue(self._clock, self._instruments.values(), model, calendar=calendar, seed=seed)
        self._shadow.connect()
        self._shadow.mark_reconciled()
        self._shadow.poll()

        # ---- the broker side: the risk gate, the order manager and the reconciler, when the limits are in force --
        self._heard: list[Fill] = []
        self._gate: RiskGate | None = None
        self._manager: OrderManager | None = None
        self._reconciler: Reconciler | None = None
        self._switched: frozenset[str] | None = frozenset()
        if risk is not None:
            assert edge.venue is not None
            asks = _Listening(edge.asks, self._heard)
            self._gate = RiskGate(
                risk, self._instruments.values(), calendar=calendar, clock=self._clock, switches=self._switches,
                journal=Journal(paths["risk"], fsync=fsync),
            )  # fmt: skip
            self._manager = OrderManager(
                edge.venue, self._gate, Outbox(paths["outbox"], fsync=fsync), clock=self._clock,
                instruments=self._instruments.values(), asks=asks, witness=edge.witness,
            )  # fmt: skip
            self._reconciler = Reconciler(
                asks, self._manager.book, Journal(paths["reconcile"], fsync=fsync), clock=self._clock,
                grace_ns=risk.reconciliation_grace_ns, sink=self._from_reconciler,
            )  # fmt: skip
            if opened.state is ConnectionState.READY:
                # The connection outlived the session before this one, so the venue will not announce it again.
                self._gate.observe(ConnectionEvent(ConnectionState.READY, opened.at_ns, 0))
        known = self._manager.book.snapshot()["fills"] if self._manager else []
        self._known_before: frozenset[str] = frozenset(known if isinstance(known, list) else [])
        self._noted: set[str] = set()

        # ---- the sleeves ------------------------------------------------------------------------------------------
        sizes = minimum_sizes(list(self._instruments.values()), model)
        self._sizes = sizes
        lineup, problem = lineup_or_none(self._folder / LINEUP)
        # A place whose scorer is a process (ADR 0023): the child is started here, with `scorer_times`. A replay
        # is handed `abstained` instead, the day's record of the rows a child did not score, and starts none.
        self._scorers = Scorers(
            times=scorer_times, record=days / f"session-{tapes + 1:03d}{ABSTAINED}", replayed=abstained,
            clock=self._clock, heard=self._abstained,
        )  # fmt: skip
        self._scoring: dict[str, str] = {}
        try:
            served = make(
                settings.sleeves, lineup, registry=settings.registry, calendar=calendar,
                others=lambda entry: make_sleeves([entry], sizes, costs=model, calendar=calendar)[0],
                problem=problem, scoring=self._scorers.make,
            )  # fmt: skip
        except LineupError as exc:
            self._scorers.close()
            raise TraderRefused(str(exc)) from exc
        self._sleeves: list[Sleeve[Any]] = served.sleeves
        self._renew = False  # the lineup changed: this session ends and the next one reads it
        self._names = [sleeve.name for sleeve in self._sleeves]
        self._states: dict[str, Any] = {sleeve.name: sleeve.initial() for sleeve in self._sleeves}
        self._views: dict[str, SleeveView] = {}
        self._stale_views = True
        self._news: dict[str, list[Event]] = {}
        self._watching: dict[str, str] = {}  # a paper order a sleeve has not been shown the end of -> its sleeve
        self._last: dict[str, Quote] = {}
        self._quoted_at: dict[str, int] = {}
        self._beat_at = opened.at_ns
        self._checked_at = opened.at_ns
        self._latched: set[str] = set()
        self._broken = False
        self._breaks = 0
        self._inherited: dict[tuple[str, str], int] | None = None  # (sleeve, pair) -> closes tried; None: not known yet
        self._tried: dict[tuple[str, str, str], int] = {}  # (source, sleeve, pair) -> when a close was last proposed
        self._attempts: dict[tuple[str, str, str], int] = {}
        self._both: dict[str, tuple[str, str, Side]] = {}  # orders sent to both venues this session
        self._got: dict[str, dict[str, list[tuple[int, int]]]] = {"broker": {}, "shadow": {}}
        self._counts = {name: {"decisions": 0, "orders": 0, "refused": 0, "fills": 0} for name in self._names}
        self._n = 0
        self._day_over = False
        self._status_path = self._folder / "status.json" if status else None

        # ---- the decision journal of the day ------------------------------------------------------------------------
        earlier = read(paths["decisions"])[1]
        self._journal = Journal(paths["decisions"])
        found = self._journal.open()
        self.session = found.sessions + 1
        self._today_fills = dict.fromkeys(self._names, 0)
        opening: object = None
        for entry in earlier:
            if entry.get("type") == "start" and opening is None:
                opening = entry.get("opening")
            if entry.get("type") == "shadow_fill" and entry.get("sleeve") in self._today_fills:
                self._today_fills[str(entry["sleeve"])] += 1
            self._breaks += entry.get("type") == "break" and entry.get("broken") == 1
        self._opening: dict[str, Any] = opening if isinstance(opening, dict) else self._nets(realised_only=True)
        self._write(
            "start", session=self.session, day=self.label, venue=opened.venue, sleeves=self._names,
            mode="paper" if self.paper else "shadow_only", stages=self._stages.all(), opening=self._opening,
            limits=risk.decision if risk else None, cost_model=model.as_dict(), config=config_digest,
            instruments=[describe(instrument) for instrument in self._instruments.values()],
            serving=served.serving,
        )  # fmt: skip
        for name in sorted(served.serving):
            if name in served.refused:
                self._write("lineup_refused", sleeve=name)
                text = f"{name} runs idle: {served.refused[name]}"
                self._notify.fire(_key("lineup", name), f"No model is served in {name}", text, 4)
            else:
                self._notify.resolve(_key("lineup", name), f"A model is served in {name}", "Its place is filled.")
            change = self._stages.serve(name, served.serving[name], opened.at_ns)
            if change is not None:
                self._changed(change)
        if not self.paper:
            self._inherited = {key: 0 for key in self._shadow_book.open_lines() if key[0] in self._names}
            self._note_inherited()
        self._journal.push()

    # ---- reading ------------------------------------------------------------------------------------------------

    @property
    def stages(self) -> Stages:
        return self._stages

    @property
    def shadow_book(self) -> Book:
        """The result of record."""
        return self._shadow_book

    @property
    def broker_book(self) -> Book | None:
        """The order manager's book of the venue's fills; None in a run that sends nothing to a broker."""
        return self._manager.book if self._manager else None

    @property
    def manager(self) -> OrderManager | None:
        return self._manager

    @property
    def gate(self) -> RiskGate | None:
        return self._gate

    @property
    def day_over(self) -> bool:
        """True once a turn at or after the day's closing rollover has been taken: the session is to end."""
        return self._day_over

    def _switches(self) -> frozenset[str]:
        """What the risk gate is told about the switches: as they were read at the last beat, never the files."""
        if self._switched is None:
            raise PermissionError("the switches could not be read at the last beat")
        return self._switched

    # ---- the loop -----------------------------------------------------------------------------------------------

    def run(self) -> bool:
        """Take turns until the edge has no more or the day is over. True when the day's close was seen."""
        try:
            while not (self._day_over or self._renew):
                step = self._edge.next(urgent=self._manager is not None and self._manager.blind)
                if step is None:
                    break
                self.step(step)
        except Exception:
            self.abandon()
            raise
        return self._day_over

    def step(self, step: Step) -> None:
        """One turn. Its time is the time of everything decided on it."""
        now = self._clock.advance_to(max(self._clock.now_ns(), step.at_ns))
        self._n += 1
        market: list[Event] = []
        for event in step.events:
            self._from_venue(event, now, market)
        if step.beat is not None:
            self._on_beat(step.beat, now, market)
        if self._manager is not None:
            self._keep_safe(now)
        self._settle_shadow(now)
        if step.beat is not None:
            self._own_closes(now)
        for sleeve in self._sleeves:
            if self._stages.stage(sleeve.name) == OFF:
                continue
            for item in (*self._news.pop(sleeve.name, ()), *market):
                self._show(sleeve, item, now)
        self._news.clear()
        self._scorers.tend()
        if step.beat is not None:
            self._watch_scorers()
        if step.beat is not None and self._status_path is not None:
            self._write_status(now, step.state)
        if now >= self._day:
            self._day_over = True
        self._journal.push()

    # ---- what comes in --------------------------------------------------------------------------------------------

    def _from_venue(self, event: object, now: int, market: list[Event]) -> None:
        if isinstance(event, Quote):
            if event.pair not in self._instruments:
                return
            stamped = event if event.recv_ts_ns == now else replace(event, recv_ts_ns=now)
            if self._manager is not None:
                self._manager.handle(stamped)
            self._shadow.feed(stamped)
            self._last[event.pair] = stamped
            self._quoted_at[event.pair] = now
            market.append(stamped)
            return
        if self._manager is not None:
            self._manager.handle(event)
            if isinstance(event, Fill):
                self._heard.append(event)
            elif isinstance(event, OrderEvent):
                owner = self._watching.get(event.client_order_id)
                if owner is not None:
                    self._news.setdefault(owner, []).append(event)
                    if event.state.is_terminal:
                        del self._watching[event.client_order_id]
        if isinstance(event, ConnectionEvent):
            self._write("connection", state=event.state.value)
            market.append(event)

    def _from_reconciler(self, event: object) -> None:
        """What the reconciler found goes the way a live event goes: through the order manager."""
        assert self._manager is not None
        self._manager.handle(event)
        if isinstance(event, Fill):
            self._heard.append(event)

    def _on_beat(self, beat: Beat, now: int, market: list[Event]) -> None:
        if beat.switches != self._switched:
            self._switched = beat.switches
            self._write("switches", on=None if beat.switches is None else sorted(beat.switches))
        if beat.server_time_ns is not None:
            for pair in self._instruments:
                if self._quoted_at.get(pair, -1) < self._beat_at and pair in self._last:
                    # The venue answered and this pair's book has not changed since the last beat: quiet, not dead.
                    if self._gate is not None:
                        self._gate.quiet(pair, now)
                    self._shadow.quiet(pair, now)
                    market.append(Heartbeat(pair, now, beat.server_time_ns, 0))
        self._beat_at = now
        for request in beat.requests:
            self._on_request(request, now)

    def _on_request(self, request: Request, now: int) -> None:
        """An operator asked for something. It is done here, with what the trader knows, and written down."""
        done, said = 0, ""
        if request.what == "reset" and self._gate is not None:
            try:
                self._gate.reset(request.subject, operator=request.operator)
                done = 1
            except ValueError as refusal:  # no such latch is tripped, or it cannot start again yet
                said = type(refusal).__name__
        elif request.what == "stage" and request.subject in self._names:
            lines = self._manager.book.open_lines() if self._manager else {}
            busy = any(self._busy(request.subject, pair) for pair in self._instruments)
            if busy or any(name == request.subject for name, _ in lines):
                said = "NOT_FLAT"  # a sleeve is taken down flat: FLATTEN first, then ask again
            else:
                change = self._stages.demote(request.subject, request.value, request.operator, now)
                done = int(change is not None)
                if change is not None:
                    self._stale_views = True
                    self._changed(change)
        elif request.what == "lineup":
            self._renew, done = True, 1  # the turn is finished; the next session reads the lineup as it is now
        self._write(
            "request", what=request.what, subject=request.subject, value=request.value, operator=request.operator,
            done=done, refused=said,
        )  # fmt: skip

    # ---- the connection, the latches and the break ----------------------------------------------------------------

    def _keep_safe(self, now: int) -> None:
        assert self._manager is not None and self._reconciler is not None and self._gate is not None
        if self._manager.blind:
            self._manager.reconcile()
        elif now - self._checked_at >= self._settings.reconcile_ns:
            self._checked_at = now
            self._reconciler.check()
        self._take_heard()
        for order_id, owner in list(self._watching.items()):
            tracked = self._manager.tracked(order_id)
            if tracked is not None and tracked.stage in TERMINAL:
                # Settled by the manager's own conclusion, not by a report: the sleeve hears of it all the same.
                ended = OrderEvent(order_id, _ENDED[tracked.stage], now, 0, filled_quantity=tracked.booked)
                self._news.setdefault(owner, []).append(ended)
                del self._watching[order_id]
        if self._inherited is None and not self._manager.blind:
            lines = self._manager.book.open_lines()
            self._inherited = {key: 0 for key in lines if key[0] in self._names}
            self._note_inherited(sorted(key for key in lines if key[0] not in self._names))
        self._watch(now)

    def _take_heard(self) -> None:
        """Every fill the venue spoke of this turn, once the order manager has booked it: journalled, and told on."""
        assert self._manager is not None
        for fill in self._heard:
            fresh = fill.fill_id not in self._known_before and fill.fill_id not in self._noted
            if not fresh or not self._manager.book.has(fill.fill_id):
                continue
            self._noted.add(fill.fill_id)
            self._stale_views = True
            owner = sleeve_of(fill.client_order_id) or ""
            self._write(
                "broker_fill", sleeve=owner, fill_id=fill.fill_id, client_order_id=fill.client_order_id,
                pair=fill.pair, side=fill.side.value, quantity=fill.quantity, price=fill.price,
            )  # fmt: skip
            self._got["broker"].setdefault(fill.client_order_id, []).append((fill.quantity, fill.price))
            if owner in self._names and self._stages.stage(owner) in PAPER:
                self._news.setdefault(owner, []).append(fill)
        self._heard.clear()

    def _watch(self, now: int) -> None:
        """A latch that tripped, a break that was flagged: written down, paged, and the sleeves stepped down."""
        assert self._gate is not None and self._manager is not None and self._reconciler is not None
        latched = set(self._gate.latches())
        for name in sorted(latched - self._latched):
            parts = name.split("/")
            concerns = [parts[1]] if parts[0] in _SLEEVE_LATCHES else self._names
            self._write("latch", latch=name, tripped=1)
            self._stages.mark(now, LATCH, concerns[0] if len(concerns) == 1 else "")
            text = "Entries are stopped until an operator resets it; see the trader runbook."
            self._notify.fire(_key("latch", name), f"Latch tripped: {name}", text, 5)
            for sleeve in concerns:
                self._step_down(sleeve, LATCH, name, now)
        for name in sorted(self._latched - latched):
            self._write("latch", latch=name, tripped=0)
            self._notify.resolve(_key("latch", name), f"Latch reset: {name}", "An operator reset it.")
        self._latched = latched
        broken = self._reconciler.broken or bool(self._manager.differing)
        if broken != self._broken:
            self._broken = broken
            self._write("break", broken=int(broken), blind=int(self._manager.blind))
            if broken:
                self._breaks += 1
                self._stages.mark(now, BREAK)
                text = "The venue's positions are not the book's. Entries are stopped; see the trader runbook."
                self._notify.fire("reconciliation_break", "Reconciliation break", text, 5)
                for sleeve in self._names:
                    self._step_down(sleeve, BREAK, "", now)
            else:
                self._notify.resolve("reconciliation_break", "Reconciliation break over", "The books agree again.")

    def _step_down(self, sleeve: str, cause: str, detail: str, now: int) -> None:
        if sleeve in self._names:
            change = self._stages.step_down(sleeve, cause, detail, now)
            if change is not None:
                self._changed(change)

    def _changed(self, change: Change) -> None:
        """A sleeve's stage changed: in the decision journal beside the orders it explains, and the owner is told."""
        self._write(
            "stage", sleeve=change.sleeve, before=change.before, to=change.to, cause=change.cause,
            detail=change.detail,
        )  # fmt: skip
        text = f"{change.sleeve}: {change.before} to {change.to} ({change.cause} {change.detail})".rstrip()
        self._notify.fire(f"stage.{change.sleeve}.{change.number}", f"Stage of {change.sleeve}: {change.to}", text, 4)

    # ---- the shadow venue -----------------------------------------------------------------------------------------

    def _settle_shadow(self, now: int) -> None:
        """What the shadow venue has to report: booked, journalled, and told to the sleeves that trade there only."""
        for item in self._shadow.poll():
            if isinstance(item, Fill):
                owner = sleeve_of(item.client_order_id) or ""
                if self._shadow_book.apply(item):
                    self._shadow_journal.write("fill", **fill_fields(item))
                    self._stale_views = True
                    self._shadow_fills[owner] = self._shadow_fills.get(owner, 0) + 1
                    if owner in self._today_fills:
                        self._today_fills[owner] += 1
                        self._counts[owner]["fills"] += 1
                self._got["shadow"].setdefault(item.client_order_id, []).append((item.quantity, item.price))
                self._write(
                    "shadow_fill", sleeve=owner, fill_id=item.fill_id, client_order_id=item.client_order_id,
                    pair=item.pair, side=item.side.value, quantity=item.quantity, price=item.price,
                    liquidity=item.liquidity.value, venue_ts_ns=item.venue_ts_ns,
                )  # fmt: skip
                self._shadow_news(owner, item)
            elif isinstance(item, CommissionEvent):
                self._shadow_book.charge(item)
                self._shadow_journal.write(
                    "commission", fill_id=item.fill_id, commission=item.commission, currency=item.currency
                )
                self._stale_views = True
                self._write(
                    "shadow_commission", fill_id=item.fill_id, commission=item.commission, currency=item.currency
                )
            elif isinstance(item, OrderEvent):
                self._write(
                    "shadow_event", client_order_id=item.client_order_id, state=item.state.value,
                    filled_quantity=item.filled_quantity, reason=item.reason,
                )  # fmt: skip
                self._shadow_news(sleeve_of(item.client_order_id) or "", item)
            elif isinstance(item, VenueNotice):
                self._write("shadow_notice", code=item.code)
        self._shadow_journal.flush()

    def _shadow_news(self, owner: str, item: Event) -> None:
        if owner in self._names and self._stages.stage(owner) == SHADOW:
            self._news.setdefault(owner, []).append(item)

    def _restore_shadow(self, path: Path) -> int:
        """The shadow book as its own journal left it. Returns the number of sessions that wrote to it."""
        sessions = 0
        for entry in read(path)[1]:
            kind = entry.get("type")
            if kind == "fill":
                fill = fill_from(entry)
                if self._shadow_book.apply(fill):
                    owner = sleeve_of(fill.client_order_id) or ""
                    self._shadow_fills[owner] = self._shadow_fills.get(owner, 0) + 1
            elif kind == "commission":
                self._shadow_book.charge(commission_from(entry))
            sessions += kind == "start"
        return sessions

    # ---- the trader's own closes ----------------------------------------------------------------------------------

    def _own_closes(self, now: int) -> None:
        """On a beat: the FLATTEN closes, what was inherited from before this session, and a shadow book ahead."""
        switched = self._switched
        if self._gate is not None and self._manager is not None:
            for order in self._gate.flatten_orders():
                self._close_on_paper(order, now)
        if switched is not None and self._risk_flatten(switched):
            for (name, pair), held in sorted(self._shadow_book.open_lines().items()):
                if name in self._names and self._stages.stage(name) == SHADOW:
                    self._close_line(FLATTEN, name, pair, held, now)
        for name, pair in sorted(self._inherited or {}):
            held = self._view_book(name).position(name, pair).quantity
            if held == 0:
                assert self._inherited is not None
                del self._inherited[name, pair]
            elif self._stages.stage(name) != OFF:
                self._close_line(INHERIT, name, pair, held, now)
        if self._manager is not None and not self._manager.blind:
            self._align(now)

    def _risk_flatten(self, switched: frozenset[str]) -> bool:
        name = self._risk.flatten_switch if self._risk else "FLATTEN"
        return name in switched

    def _busy(self, name: str, pair: str) -> bool:
        """Is an order of this sleeve in this pair still on its way, on either venue?"""
        if self._manager is not None:
            for order_id in self._manager.in_flight():
                tracked = self._manager.tracked(order_id)
                if tracked is not None and (tracked.order.sleeve, tracked.order.pair) == (name, pair):
                    return True
        return any(
            found.pair == pair and sleeve_of(found.client_order_id) == name for found in self._shadow.open_orders()
        )

    def _due(self, key: tuple[str, str, str], now: int) -> bool:
        wait = self._settings.close_retry_ns
        return now - self._tried.get(key, -wait) >= wait

    def _try(self, key: tuple[str, str, str], now: int) -> int:
        self._tried[key] = now
        self._attempts[key] = self._attempts.get(key, 0) + 1
        return self._attempts[key]

    def _close_line(self, source: str, name: str, pair: str, held: int, now: int) -> None:
        """Propose one closing order for a line, unless one is on its way or the last try was too short a time ago."""
        key = (source, name, pair)
        if self._busy(name, pair) or not self._due(key, now):
            return
        attempt = self._try(key, now)
        side = Side.SELL if held > 0 else Side.BUY
        size = abs(held)
        if self._risk is not None and pair in self._risk.pairs:
            size = min(size, self._risk.pairs[pair].max_order_size)
        self._decide(name, Intent(pair, side, size, source, attempt, TimeInForce.IOC), source, now)

    def _close_on_paper(self, order: Order, now: int) -> None:
        """A close the risk gate proposed while FLATTEN is set. It passes the gate's `check` like any other order."""
        self._write(
            "decision", sleeve=order.sleeve, source=FLATTEN, pair=order.pair, side=order.side.value,
            quantity=order.quantity, leg=FLATTEN, attempt=0, tif=order.tif.value, limit_price=None,
        )  # fmt: skip
        instrument = self._instruments.get(order.pair)
        if instrument is not None:
            self._to_paper(order, instrument, FLATTEN, reducing=True)

    def _align(self, now: int) -> None:
        """Where the shadow book holds more than the broker book, or the other way round in sign: close the excess.

        A sleeve on paper is shown the broker's fills, so it cannot know that the shadow venue filled an order
        the broker did not. Left alone, that position would sit in the result of record for good. It is closed
        on the shadow venue, at the shadow venue's cost, and the difference is in the journal.
        """
        assert self._manager is not None
        for name in self._names:
            if self._stages.stage(name) not in PAPER:
                continue
            for pair in self._instruments:
                broker = self._manager.book.position(name, pair).quantity
                shadow = self._shadow_book.position(name, pair).quantity
                same_way = broker * shadow > 0
                target = (1 if shadow > 0 else -1) * min(abs(broker), abs(shadow)) if same_way else 0
                key = (ALIGN, name, pair)
                if shadow == target or not self._due(key, now) or self._busy(name, pair):
                    continue
                attempt = self._try(key, now)
                self._write("align", sleeve=name, pair=pair, broker=broker, shadow=shadow, target=target)
                side = Side.BUY if target > shadow else Side.SELL
                intent = Intent(pair, side, abs(target - shadow), ALIGN, attempt, TimeInForce.IOC)
                self._decide(name, intent, ALIGN, now)

    def _note_inherited(self, unowned: Sequence[tuple[str, str]] = ()) -> None:
        for name, pair in sorted(self._inherited or {}):
            held = self._view_book(name).position(name, pair).quantity
            self._write("inherited", sleeve=name, pair=pair, quantity=held)
        for name, pair in unowned:
            self._write("unowned", sleeve=name, pair=pair)
            text = f"The broker book holds {pair} under {name!r}, which no sleeve here answers for. FLATTEN closes it."
            self._notify.fire(_key("unowned", f"{name}.{pair}"), "A position nobody owns", text, 4)

    # ---- the sleeves ----------------------------------------------------------------------------------------------

    def _abstained(self, name: str, row: int, known_ns: int, why: str) -> None:
        """A scorer process did not score this row, and the sleeve abstains on it. A replay writes the same line."""
        self._write("abstained", sleeve=name, row=row, known_ns=known_ns, why=why)

    def _watch_scorers(self) -> None:
        """On a beat: the owner is told when a scorer process stops answering, and when it answers again.

        Not journalled: when a process comes back is not a function of the tape. The rows it missed are.
        """
        for name, health in self._scorers.health().items():
            if health == self._scoring.get(name, READY):
                continue
            self._scoring[name] = health
            if health == READY:
                self._notify.resolve(_key("scorer", name), f"The scorer of {name} answers again", "It was restarted.")
            else:
                text = f"The scorer process of {name} is {health}. The sleeve abstains until it answers."
                self._notify.fire(_key("scorer", name), f"The scorer of {name} is {health}", text, 4)

    def _view_book(self, name: str) -> Book:
        """The book a sleeve's position is read from: the broker's once it places paper orders, else the shadow's."""
        if self._manager is not None and name in self._names and self._stages.stage(name) in PAPER:
            return self._manager.book
        return self._shadow_book

    def _view(self, name: str) -> SleeveView:
        if self._stale_views:
            self._views.clear()
            self._stale_views = False
        found = self._views.get(name)
        if found is None:
            shadow = self._shadow_book.view(name)
            book = self._view_book(name)
            # Positions from the book that can be closed; results from the shadow book and from nowhere else.
            found = shadow if book is self._shadow_book else replace(shadow, positions=book.view(name).positions)
            self._views[name] = found
        return found

    def _show(self, sleeve: Sleeve[Any], item: Event, now: int) -> None:
        """Show a sleeve one event, do what it asks, and show it how that went, until it has nothing more to ask."""
        name = sleeve.name
        waiting: deque[Event] = deque([item])
        shown = 0
        while waiting:
            shown += 1
            if shown > ANSWERS_PER_EVENT:
                raise TraderRefused(f"sleeve {name} keeps answering its own acknowledgements; stopped at {shown}")
            self._states[name], actions = sleeve.decide(self._states[name], waiting.popleft(), now, self._view(name))
            for action in actions:
                answer = self._act(name, action, now)
                if answer is not None:
                    waiting.append(answer)

    def _act(self, name: str, action: Action, now: int) -> Placed | Refused | None:
        if isinstance(action, Cancel):
            return self._take_back(name, action)
        if not isinstance(action, Intent):
            raise TraderRefused(f"sleeve {name} returned {action!r}, which is neither an intent nor a cancel")
        if (name, action.pair) in (self._inherited or {}):
            self._counts[name]["decisions"] += 1
            self._decision(name, action, SLEEVE)
            return self._refuse(name, action, Why.INHERITED_POSITION.value)
        return self._decide(name, action, SLEEVE, now)

    def _take_back(self, name: str, action: Cancel) -> Refused | None:
        order_id = action.client_order_id
        if sleeve_of(order_id) != name:
            return self._refuse(name, action, Why.NOT_THIS_SLEEVES_ORDER.value)
        sent = 0
        if self._manager is not None and self._stages.stage(name) in PAPER:
            sent = int(self._manager.withdraw(order_id))
        try:
            self._shadow.cancel(order_id)
        except VenueError:
            if not sent:
                return self._refuse(name, action, Why.SHADOW_REFUSED.value)
        self._write("cancel", sleeve=name, client_order_id=order_id, paper=sent)
        return None

    def _decision(self, name: str, intent: Intent, source: str) -> None:
        self._write(
            "decision", sleeve=name, source=source, pair=intent.pair, side=intent.side.value,
            quantity=intent.quantity, leg=intent.leg, attempt=intent.attempt, tif=intent.tif.value,
            limit_price=intent.limit_price, expected_micro=intent.expected_micro,
        )  # fmt: skip

    def _decide(self, name: str, intent: Intent, source: str, now: int) -> Placed | Refused:
        """One intent: the stage decides what it may be, and it goes to the venues its stage allows."""
        if name in self._counts:
            self._counts[name]["decisions"] += 1
        self._decision(name, intent, source)
        instrument = self._instruments.get(intent.pair)
        if instrument is None:
            return self._refuse(name, intent, Why.UNKNOWN_PAIR.value)
        stage = self._stages.stage(name)
        on_paper = stage in PAPER and source != ALIGN
        reducing = self._reduces(name, intent, on_paper)
        quantity = intent.quantity
        if on_paper and not reducing:
            if stage == STEPPED_DOWN:
                return self._refuse(name, intent, Why.STAGE_EXITS_ONLY.value)
            if stage == MIN_SIZE:
                quantity = self._sizes[intent.pair]
        elif not on_paper and not reducing and source == SLEEVE:
            stopped = self._stopped_by_switch()
            if stopped is not None:
                return self._refuse(name, intent, stopped)
        kind = OrderType.MARKET if intent.limit_price is None else OrderType.LIMIT
        try:
            order_id = client_order_id(
                sleeve=name, pair=intent.pair, side=intent.side, decision_seq=now, leg=intent.leg,
                attempt=intent.attempt,
            )  # fmt: skip
            order = Order(order_id, name, intent.pair, intent.side, quantity, kind, intent.tif, now, intent.limit_price)
        except ValueError:
            return self._refuse(name, intent, Why.NOT_AN_ORDER.value)
        self._write(
            "order", sleeve=name, client_order_id=order_id, pair=order.pair, side=order.side.value,
            quantity=order.quantity, order_type=kind.value, tif=order.tif.value, limit_price=order.limit_price,
            stage=stage, reducing=int(reducing),
        )  # fmt: skip
        if on_paper:
            code = self._to_paper(order, instrument, source, reducing=reducing)
        else:
            code = self._to_shadow(order, instrument)
        if code is not None:
            return self._refuse(name, intent, code)
        if name in self._counts:
            self._counts[name]["orders"] += 1
        return Placed(intent, order_id)

    def _reduces(self, name: str, intent: Intent, on_paper: bool) -> bool:
        """Can this order only bring the sleeve's position towards zero, with every order on its way filled?

        The same pessimism as the risk gate's: a second exit behind a first that has not ended could go through
        zero, so it is not a reduction, and gets no treatment a reduction gets.
        """
        held = self._view_book(name).position(name, intent.pair).quantity
        ahead = 0
        if on_paper and self._manager is not None:
            for order_id in self._manager.in_flight():
                tracked = self._manager.tracked(order_id)
                if tracked is None or tracked.order.side is not intent.side:
                    continue
                if (tracked.order.sleeve, tracked.order.pair) == (name, intent.pair):
                    ahead += max(0, tracked.order.quantity - tracked.booked)
        elif not on_paper:
            for found in self._shadow.open_orders():
                mine = sleeve_of(found.client_order_id) == name and found.pair == intent.pair
                if mine and found.side is intent.side:
                    ahead += found.quantity - found.filled_quantity
        return _opposes(intent.side, held, intent.quantity + ahead)

    def _stopped_by_switch(self) -> str | None:
        """For a sleeve that trades on the shadow venue only: the switches stop its entries as the gate would."""
        if self._switched is None:
            return Reason.SWITCHES_UNREADABLE.value
        kill = self._risk.kill_switch if self._risk else "KILL"
        if kill in self._switched:
            return Reason.KILL_SWITCH.value
        return Reason.FLATTEN_SWITCH.value if self._risk_flatten(self._switched) else None

    def _to_paper(self, order: Order, instrument: Instrument, source: str, *, reducing: bool) -> str | None:
        """Propose an order to the order manager, which asks the risk gate. Sent, it is mirrored on the shadow venue.

        Returns None when the order went to the broker, or the code that stopped it.
        """
        assert self._manager is not None
        proposal = self._manager.propose(order)
        stage = proposal.stage.value if proposal.stage else None
        sent = int(proposal.sent)
        self._write("verdict", client_order_id=order.client_order_id, code=proposal.code, sent=sent, at=stage)
        if not proposal.sent:
            return proposal.code
        if order.sleeve in self._names:
            self._watching[order.client_order_id] = order.sleeve
        self._both[order.client_order_id] = (order.sleeve, order.pair, order.side)
        mirrored = order
        if reducing:
            # An exit closes what the shadow book holds, and no more: it must never open the other way there.
            room = max(0, -order.side.sign * self._shadow_book.position(order.sleeve, order.pair).quantity)
            if room == 0:
                self._write("shadow_skipped", client_order_id=order.client_order_id, code=Why.NOTHING_TO_REDUCE.value)
                return None
            mirrored = replace(order, quantity=min(order.quantity, room))
        refused = self._to_shadow(mirrored, instrument)
        if refused is not None:
            self._write("shadow_skipped", client_order_id=order.client_order_id, code=refused)
        return None

    def _to_shadow(self, order: Order, instrument: Instrument) -> str | None:
        """Place an order on the shadow venue this trader built. None when it took it."""
        try:
            self._shadow.place(order, instrument)
        except VenueError:
            return Why.SHADOW_REFUSED.value
        self._write("shadow_order", client_order_id=order.client_order_id, quantity=order.quantity)
        return None

    def _refuse(self, name: str, action: Action, code: str) -> Refused:
        if name in self._counts:
            self._counts[name]["refused"] += 1
        self._write("refused", sleeve=name, code=code)
        return Refused(action, code)

    # ---- the record -----------------------------------------------------------------------------------------------

    def _write(self, kind: str, **fields: object) -> None:
        self._journal.write(kind, n=self._n, at_ns=self._clock.now_ns(), **fields)

    def _nets(self, *, realised_only: bool = False) -> dict[str, Any]:
        """Net result by book, scope and currency, in millionths. None where an open position has no quote.

        `realised_only` leaves what is still open out of it: the figure a day opens on.
        """
        books: dict[str, Book] = {"shadow": self._shadow_book}
        if self._manager is not None:
            books["broker"] = self._manager.book
        out: dict[str, Any] = {}
        for label, book in books.items():
            out[label] = {scope: self._net(book, scope, realised_only) for scope in (DESK, *self._names)}
        return out

    def _net(self, book: Book, scope: str, realised_only: bool) -> dict[str, int] | None:
        totals: dict[str, int] = {}
        for pair, instrument in self._instruments.items():
            last = self._last.get(pair)
            if last is None and book.position(scope, pair).quantity and not realised_only:
                return None
            result = book.pnl(scope, pair, *((last.bid, last.ask) if last else (1, 1)))
            net = result.net_micro
            if realised_only:
                net -= result.unrealised * int(10 ** (6 - instrument.price_scale))
            totals[instrument.quote] = totals.get(instrument.quote, 0) + net
        return totals

    def summary(self) -> dict[str, object]:
        """Where the session stands: counts, stages and both books' results, in exact integers."""
        nets = self._nets()
        open_lines = set(self._shadow_book.open_lines())
        if self._manager is not None:
            open_lines |= set(self._manager.book.open_lines())
        sleeves = []
        for name in self._names:
            total, opening = nets["shadow"][name], self._opening.get("shadow", {}).get(name)
            today = None
            if total is not None and isinstance(opening, dict):
                today = {code: amount - int(opening.get(code, 0)) for code, amount in total.items()}
            sleeves.append({
                "name": name, "stage": self._stages.stage(name), **self._counts[name],
                "trades_today": self._today_fills[name], "trades_total": self._shadow_fills.get(name, 0),
                "net_today_micro": today, "net_total_micro": total,
            })  # fmt: skip
        books: dict[str, object] = {"reconciliation_breaks_today": self._breaks, "open_positions": len(open_lines)}
        for label in ("shadow", "broker"):
            if label not in nets:
                books[label] = None
                continue
            total, opening = nets[label][DESK], self._opening.get(label, {}).get(DESK)
            today = None
            if total is not None and isinstance(opening, dict):
                today = {code: amount - int(opening.get(code, 0)) for code, amount in total.items()}
            books[label] = {"net_today_micro": today, "net_total_micro": total}
        return {
            "session": self.session, "day": self.label, "mode": "paper" if self.paper else "shadow_only",
            "turns": self._n, "sleeves": sleeves, "books": books,
        }  # fmt: skip

    def slippage(self) -> list[dict[str, object]]:
        """Shadow against broker for the orders this session sent to both (hftlab.trader.slippage)."""
        matched = [
            Matched(
                name, pair, side, self._instruments[pair].price_increment,
                tuple(self._got["broker"].get(order_id, ())), tuple(self._got["shadow"].get(order_id, ())),
            )
            for order_id, (name, pair, side) in self._both.items()
        ]  # fmt: skip
        return report(matched)

    def _write_status(self, now: int, state: ConnectionState) -> None:
        """What the status command reads. Replaced whole, so a reader never sees half of it."""
        assert self._status_path is not None
        said = {**self.summary(), "at_ns": now, "connection": state.value, "latched": sorted(self._latched)}
        partial = self._status_path.with_suffix(".partial")
        partial.write_text(json.dumps(said, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(partial, self._status_path)

    # ---- the end ----------------------------------------------------------------------------------------------------

    def close(self) -> dict[str, object]:
        """End the session on purpose: the day's close if it was seen, the slippage report, the result, the journals.

        Nothing is cancelled and nothing is closed. A working order is left to the order manager's rules: the
        next start asks the venue about it, takes it back and reconciles before anything is sent.
        """
        now = self._clock.now_ns()
        if self._day_over:
            latched = bool(self._gate.latches()) if self._gate else False
            for change in self._stages.close_day(self._day, now, latched=latched, broken=self._broken):
                self._changed(change)
        self._write("slippage", lines=self.slippage())
        result = self.summary()
        self._write("stop", summary=result, day_closed=int(self._day_over))
        self._stages.end(now)
        self._edge.stop(now)
        self._shut()
        return result

    def abandon(self) -> None:
        """Something raised. The journals are closed as they are, with no word that the session ended well."""
        self._shut()

    def _shut(self) -> None:
        self._scorers.close()
        self._journal.close()
        self._shadow_journal.close()
        if self._manager is not None:
            self._manager.close()


def _key(kind: str, name: str) -> str:
    """An alert key: lower case, with nothing in it but what a key may hold."""
    return f"{kind}.{name.lower().replace('/', '.').replace('_', '-')}"[:59]


__all__ = ["STATE", "UNVERIFIED", "Notify", "Silent", "Trader", "TraderRefused", "Why", "day_label", "mark"]
