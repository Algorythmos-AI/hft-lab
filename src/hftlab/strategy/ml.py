"""The ML sleeve: a registered model's score turned into intents, through the same interface as any sleeve (ADR 0014).

A skeleton. No model ships with it; it is handed a scorer, a feature builder and a feasibility map, and it is
as much a pure function of the events as the control sleeve:

  * It trades one pair at one horizon, and only if the feasibility map marks that cell viable. On any other
    map it never enters.
  * On a quote or a heartbeat it brings its features up to date, one event at a time, exactly as a dataset was
    built. If the event completes a row and the pair is flat, it asks the scorer; it enters, at the size the
    model's outcomes are for, only when the outcome the model expects after costs is above its margin.
  * It leaves at the label's horizon: on the first event at least one horizon after the decision. It closes
    what its book says it holds, not what it remembers ordering.
  * An order that came to nothing is not sent again at once: the same waiting time between attempts as the
    control sleeve, kept in the state so that a replay waits as long.

Stages are the trader's (ADR 0019), not a sleeve's. What the sleeve can do is say what it knows:
`shadow_only` lists why its results may not leave shadow. A model fitted to placeholder costs, or a
feasibility map computed on placeholder costs or a placeholder rule, is on that list, and so is a cell the map
does not mark viable. Whoever sets a stage reads it; nothing here can set one.

The scorer's arithmetic is exact, so the decisions are the same in a replay (ADR 0022).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from hftlab.book import SleeveView
from hftlab.core import Fill, OrderEvent, OrderState, Quote, Side
from hftlab.core.ids import check_sleeve
from hftlab.features import FeatureBuilder, FeatureState
from hftlab.models.scorer import Scorer
from hftlab.record.format import Heartbeat, Marker
from hftlab.strategy.sleeve import (
    ENTRY,
    EXIT,
    Action,
    Event,
    Intent,
    Placed,
    Refused,
    StateError,
    one_of,
    opened,
    text_or_none,
    whole,
    whole_or_none,
)
from hftlab.venues.port import ConnectionEvent

FLAT, ENTERING, HOLDING, EXITING = "flat", "entering", "holding", "exiting"
PHASES = (FLAT, ENTERING, HOLDING, EXITING)
KIND, VERSION = "ml", 1  # of a saved state
_SAVED = frozenset({"features", "phase", "order_id", "decided_ns", "trips", "exits", "tried_ns"})
_DEAD = (OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED)


@dataclass(frozen=True, slots=True)
class MlState:
    features: FeatureState
    phase: str = FLAT
    order_id: str | None = None  # the order whose outcome the sleeve is waiting for
    decided_ns: int = 0  # when the position in hand was decided: the label's horizon runs from here
    trips: int = 0  # round trips completed
    exits: int = 0  # exit orders sent for the current position
    tried_ns: int | None = None  # when the order on its way, or the last that came to nothing, was sent


def viable_cell(feasibility: Mapping[str, object], pair: str, horizon_ns: int) -> bool:
    """Does this feasibility map mark the pair at this holding time viable?"""
    cells = feasibility.get("viable_cells")
    return feasibility.get("kind") == "feasibility-map" and isinstance(cells, list) and [pair, horizon_ns] in cells


class MlSleeve:
    def __init__(
        self,
        name: str,
        *,
        scorer: Scorer,
        features: FeatureBuilder,
        feasibility: Mapping[str, object],
        margin_micro: int,
        retry_ns: int,
    ) -> None:
        model = scorer.model
        if features.pair != model.pair or features.keys != model.features:
            raise ValueError("the sleeve's features are not the ones its model was trained on, in the same order")
        if margin_micro < 0 or retry_ns < 0:
            raise ValueError("the margin and the time between two attempts are not negative")
        self._name = check_sleeve(name)
        self._scorer, self._features = scorer, features
        self.pair, self.size, self.horizon_ns = model.pair, model.size, model.label["horizon_ns"]
        self.margin_micro, self.retry_ns = margin_micro, retry_ns
        self.viable = viable_cell(feasibility, self.pair, self.horizon_ns)
        reasons = []
        if model.placeholder:
            reasons.append("the model was fitted to outcomes priced on a placeholder cost model")
        if feasibility.get("placeholder") is not False:
            reasons.append("the feasibility map was computed on placeholder costs or a placeholder rule")
        if not self.viable:
            reasons.append(f"the feasibility map does not mark {self.pair} at {self.horizon_ns} ns viable")
        self.shadow_only: tuple[str, ...] = tuple(reasons)

    @property
    def name(self) -> str:
        return self._name

    @property
    def may_leave_shadow(self) -> bool:
        """False while anything it was built on is a placeholder. A report, not a switch: stages are the trader's."""
        return not self.shadow_only

    def initial(self) -> MlState:
        return MlState(self._features.initial())

    # ---- written down and read back (ADR 0026) --------------------------------------------------------------------

    def settings(self) -> dict[str, object]:
        """What a saved state was reached under: the model by its hash, its features and windows, and the rules.

        A place whose model changed has other settings, so the state of the model before it is never read.
        """
        return {
            "model": self._scorer.model.content_hash, "features": self._features.settings(), "size": self.size,
            "horizon_ns": self.horizon_ns, "margin_micro": self.margin_micro, "retry_ns": self.retry_ns,
            "viable": int(self.viable),
        }  # fmt: skip

    def save(self, state: MlState) -> dict[str, object]:
        plain = {
            "features": self._features.dump(state.features), "phase": state.phase, "order_id": state.order_id,
            "decided_ns": state.decided_ns, "trips": state.trips, "exits": state.exits, "tried_ns": state.tried_ns,
        }  # fmt: skip
        return {"kind": KIND, "version": VERSION, "settings": self.settings(), "state": plain}

    def restore(self, saved: Mapping[str, object]) -> MlState:
        plain = opened(saved, KIND, VERSION, self.settings())
        if not isinstance(plain, Mapping) or set(plain) != _SAVED:
            raise StateError("not a saved state of the ML sleeve")
        try:
            features = self._features.load(plain["features"])
        except ValueError as exc:
            raise StateError(str(exc)) from exc
        return MlState(
            features, one_of(plain["phase"], PHASES, "phase"), text_or_none(plain["order_id"], "order"),
            whole(plain["decided_ns"], "decided_ns"), whole(plain["trips"], "trips"), whole(plain["exits"], "exits"),
            whole_or_none(plain["tried_ns"], "tried_ns"),
        )  # fmt: skip

    def decide(self, state: MlState, event: Event, now_ns: int, view: SleeveView) -> tuple[MlState, Sequence[Action]]:
        if isinstance(event, Quote | Heartbeat | ConnectionEvent | Marker):
            return self._on_market(state, event, now_ns, view)
        if isinstance(event, Placed):
            return (replace(state, order_id=event.client_order_id) if event.intent.pair == self.pair else state), ()
        if isinstance(event, Refused):
            return (self._back(state) if isinstance(event.action, Intent) else state), ()
        if isinstance(event, Fill):
            return self._on_fill(state, event, view), ()
        if isinstance(event, OrderEvent) and event.state in _DEAD and state.order_id == event.client_order_id:
            return self._back(state), ()
        return state, ()

    # ---- the market ---------------------------------------------------------------------------------------------

    def _on_market(
        self, state: MlState, event: Quote | Heartbeat | ConnectionEvent | Marker, now_ns: int, view: SleeveView
    ) -> tuple[MlState, Sequence[Action]]:
        known, row = self._features.step(state.features, event, now_ns)
        state = replace(state, features=known)
        if not isinstance(event, Quote | Heartbeat) or event.pair != self.pair:
            return state, ()
        held = view.quantity(self.pair)
        if state.tried_ns is not None and now_ns - state.tried_ns < self.retry_ns:
            return state, ()  # the last attempt came to nothing too short a time ago
        if state.phase == FLAT and held == 0 and row is not None and self.viable:
            found = self._scorer.score(row)
            if found is not None and found.expected_micro > self.margin_micro:
                waiting = replace(state, phase=ENTERING, order_id=None, decided_ns=now_ns, tried_ns=now_ns)
                return waiting, (Intent(self.pair, found.side, self.size, ENTRY, expected_micro=found.expected_micro),)
        elif state.phase == HOLDING and held != 0 and now_ns - state.decided_ns >= self.horizon_ns:
            side = Side.SELL if held > 0 else Side.BUY
            waiting = replace(state, phase=EXITING, order_id=None, exits=state.exits + 1, tried_ns=now_ns)
            return waiting, (Intent(self.pair, side, abs(held), EXIT, attempt=state.exits),)
        return state, ()

    # ---- news of its own orders ---------------------------------------------------------------------------------

    def _on_fill(self, state: MlState, fill: Fill, view: SleeveView) -> MlState:
        if fill.pair != self.pair:
            return state
        if view.quantity(self.pair) == 0:
            return MlState(state.features, trips=state.trips + 1)
        if state.phase == ENTERING:
            return replace(state, phase=HOLDING, order_id=None, tried_ns=None)
        return state

    def _back(self, state: MlState) -> MlState:
        """An order came to nothing: an entry leaves the pair flat, an exit leaves it held and due."""
        if state.phase == ENTERING:
            return replace(state, phase=FLAT, order_id=None)
        if state.phase == EXITING:
            return replace(state, phase=HOLDING, order_id=None)
        return state
