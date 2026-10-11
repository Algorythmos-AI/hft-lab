"""The whole cycle with the trader itself: a promoted champion loses on paper, is stepped down, and is rolled back."""
from __future__ import annotations

from hftlab import lineup
from hftlab.learn.config import load
from hftlab.lineup import Lineup, decide
from hftlab.trader.config import parse
from tests.learn.kit import COST, learn_settings, planted
from tests.lineup.kit import CHALLENGER, DAY, ML, desk, put, register, settings
from tests.trader.kit import Pages, put_stage
from tests.venues.kit import MS, T0

TIGHT = {"per_sleeve/ml/loss_latches/USD/daily_loss": "15.00"}  # two losing round trips of 6.00 are within it


def no_effect(made, seed: int, blocks: int) -> int:
    """The planted stream without its jump: a model that enters on a one-sided book pays its costs and gets nothing."""
    for quote in planted(seed, blocks, effect=False, start_ns=made.quoted + MS):
        made.market.script_quote(quote.recv_ts_ns, "EUR.USD", quote.bid, quote.ask, quote.bid_size, quote.ask_size)
    made.market.script_quote(made.quoted + MS, "USD.JPY", 151_230, 151_240)
    made.quoted += blocks * 3_000 * MS
    return blocks * 3


def test_a_promoted_champion_that_loses_on_paper_is_stepped_down_at_once_and_rolled_back_at_the_weekend(tmp_path):
    registry, state = tmp_path / "learn", tmp_path / "trader"
    a, b = register(registry, 1, not_a_result=0), register(registry, 3, not_a_result=0)
    before = put(state, Lineup(champion=a, challengers=(b,)))
    put(state, before.promoted(b), lineup.PROMOTE, at_ns=2)  # last weekend's decision: B is the champion, A is kept

    first = desk(state, registry, paper=True, changes=TIGHT)  # B's first session: its place starts in shadow
    first.run(2)
    first.stop()
    assert first.of("start")[0]["serving"] == {ML: b.model, CHALLENGER: None}
    put_stage(state, ML, "min_size", at_ns=first.clock.now_ns())  # as its stage rule would have, days later

    made = desk(state, registry, paper=True, changes=TIGHT, at_ns=first.clock.now_ns())
    assert made.trader.stages.stage(ML) == "min_size"
    made.run(no_effect(made, 7, 20), quotes=False)
    made.stop()
    (tripped,) = [entry for entry in made.of("latch") if entry["tripped"] == 1]
    assert tripped["latch"] == "DAILY_LOSS_SLEEVE/ml/USD", "the champion's own loss latch, as the risk gate names it"
    down = [e for e in made.of("stage") if e["sleeve"] == ML]
    assert [(e["before"], e["to"], e["cause"]) for e in down] == [("min_size", "stepped_down", "LATCH")]
    assert made.trader.broker_book.position(ML, "EUR.USD").quantity == 0, "and it was let out of what it held"
    assert COST * 2 < 15_000_000 < COST * 3, "the limit was passed on the third losing round trip"

    pages = Pages()
    learn = load(learn_settings(tmp_path))  # the shipped shape: no promotion rule in force
    weekly = decide.Desk(learn, parse(settings(registry, state_dir=str(state))), False, tmp_path / "none", pages)
    assert learn.rule_in_force is False
    _, outcome = decide.run(weekly, T0 + 3 * DAY)
    assert outcome.action == "roll_back", "without a rule, a record or limits: a rollback waits for nobody"
    after = lineup.read(state / lineup.FILE)
    assert after.champion == a and after.previous == () and b not in (*after.challengers, *after.candidates)
    assert pages.fired == ["lineup.roll_back.2025-10-12"]
    line = lineup.history(state / lineup.FILE)[-1]
    assert (learn.reports / f"{line['report']}.json").is_file(), "journalled with the report it rests on"

    again = desk(state, registry, paper=True, changes=TIGHT, at_ns=made.clock.now_ns())
    assert again.of("start")[-1]["serving"] == {ML: a.model, CHALLENGER: None}, "the champion from before serves again"
    assert again.trader.stages.stage(ML) == "stepped_down", "and its place earns its way up again"
