"""What the capability file holds against the account, and what a funded float answers (ADR 0031).

The probe measured that the paper account refuses a spot order that opens a position from flat: for want of
cash in three pairs, and whatever the cash in the pair with a currency it may not hold. The measured file is
not changed by any of this. A refusal for want of cash is answered by cash, so with a float in the limits a
pair held for that alone is held only until the float is funded; every other hold stands.
"""
from __future__ import annotations

import pytest
import yaml

from hftlab.core import InstrumentKind
from hftlab.trader import TraderRefused
from hftlab.trader import __main__ as cli
from hftlab.trader.config import load
from hftlab.venues import capability
from hftlab.venues.ibkr import refusals
from tests.inventory.kit import NEVER, PAIRS, Bureau, loaded_limits, put_stage, settings
from tests.support import ROOT

CASH = InstrumentKind.CASH
FOR_CASH = {"buy": "refused", "buy_refusal": capability.REFUSED_FOR_CASH, "sell": None, "sell_refusal": None}
YEN = {"buy": "refused", "buy_refusal": "currency_not_permitted", "sell": None, "sell_refusal": None}
UNKNOWN = {"buy": "refused", "buy_refusal": None, "sell": None, "sell_refusal": None}
OPEN = {"buy": "accepted", "sell": "accepted"}


def measured(**opening) -> dict:
    pairs = [{"pair": pair.replace("_", "."), "spot_position_opening": said} for pair, said in opening.items()]
    return {"probe_record": "HFT-EXP-0008", "pairs": pairs}


AS_PROBED = measured(EUR_USD=FOR_CASH, AUD_USD=FOR_CASH, USD_JPY=YEN)
EARNED = {"clean_days": 1, "decision": "HFT-DEC-9998"}


def test_the_class_a_float_answers_is_the_probes_own_word_for_a_refusal_for_want_of_cash():
    assert capability.REFUSED_FOR_CASH == refusals.INSUFFICIENT_CASH


def test_a_refusal_for_want_of_cash_is_held_until_funded_and_every_other_hold_stands():
    pairs = [("EUR.USD", CASH), ("AUD.USD", CASH), ("GBP.USD", CASH), ("USD.JPY", CASH), ("NZD.USD", CASH)]
    file = measured(EUR_USD=FOR_CASH, AUD_USD=OPEN, GBP_USD=UNKNOWN, USD_JPY=YEN)
    blocked, unfunded = capability.split(file, pairs)
    assert set(unfunded) == {"EUR.USD"} and "insufficient_cash" in unfunded["EUR.USD"]
    assert set(blocked) == {"GBP.USD", "USD.JPY", "NZD.USD"}, "reason unknown, currency barred, not described"
    assert capability.split(file, pairs, ["USD.JPY", "GBP.USD", "NZD.USD"]) == ({}, unfunded), "never sent: not asked"
    assert capability.split(file, [("EUR.USD", InstrumentKind.CFD)])[0], "nothing is measured about another form"
    both = dict(FOR_CASH, sell="refused", sell_refusal="other")
    assert set(capability.split(measured(EUR_USD=both), pairs[:1])[0]) == {"EUR.USD"}, "one side a float cannot answer"


def test_the_shipped_file_as_measured_holds_the_paper_pairs_only_for_want_of_cash():
    shipped = capability.load(ROOT / capability.path_for("ibkr_paper"))
    pairs = [(entry["pair"], CASH) for entry in shipped["pairs"]]
    blocked, unfunded = capability.split(shipped, pairs)
    assert set(unfunded) == {"EUR.USD", "AUD.USD"}
    assert set(blocked) == {"USD.JPY", "GBP.USD"}, "yen may not be held; why GBP.USD was refused was not captured"
    assert len(capability.cannot_open(shipped, pairs)) == 4, "and by itself the file still shows no pair as open"


def desk(folder, *, stage=None, **more) -> Bureau:
    if stage:
        put_stage(folder, "control", stage)
    made = Bureau(folder, start=False, **more)
    made.cannot_open, made.needs_float = capability.split(AS_PROBED, PAIRS, made.risk.inventory.shadow_only)
    assert not made.cannot_open and set(made.needs_float) == {"EUR.USD", "AUD.USD"}
    made.start()
    return made


def a_trading_day(made: Bureau) -> None:
    """Run to the close of the trading day in hand, so that the promotion rule is asked."""
    for _ in range(40):
        if made.run(3_600):
            return
    raise AssertionError("the day did not close")


def earned() -> dict:
    loaded = settings()
    loaded["rollout"]["control"]["promote"]["min_size"] = dict(EARNED)
    return loaded


def test_without_a_float_the_hold_stands_and_the_journal_says_what_is_missing(tmp_path):
    made = desk(tmp_path, settings=earned())
    assert made.trader.stages.stage("control") == "shadow"
    assert capability.NO_FLOAT in made.trader._held_back() and "EUR.USD" in made.trader._held_back()
    made.run(30)
    assert not [e for e in made.outbox() if e["type"] == "order"], "nothing is sent"


def test_with_a_funded_float_nothing_holds_the_sleeve_and_the_yen_pair_stays_on_the_shadow_venue(tmp_path):
    made = desk(tmp_path, settings=earned())
    made.fund()
    assert made.trader._held_back() == ""
    made.restart()
    assert made.trader._held_back() == "", "the ledger is read again at a start"
    made.run(15)
    made.ask("unfund", "float")
    made.run(40)
    assert made.trader.float_status()["phase"] == "none"
    assert capability.NO_FLOAT in made.trader._held_back(), "taken back: the hold stands again"


def test_a_sleeve_above_shadow_starts_on_a_funded_float_and_trades_it(tmp_path):
    made = desk(tmp_path, stage="min_size")
    made.fund()
    made.run(90)
    pairs = {f["client_order_id"].split("-")[1] for f in made.of("broker_fill")}
    assert made.of("broker_fill") and not [o for o in made.outbox() if o.get("pair") == "USD.JPY"]
    assert len(pairs) >= 1


def test_a_sleeve_above_shadow_with_no_float_starts_and_enters_nothing(tmp_path):
    """Refusing the start would leave no running trader to fund the float: the gate stops every entry instead."""
    made = desk(tmp_path, stage="min_size")
    made.run(60)
    assert not made.of("broker_fill") and made.positions() == {}
    assert {v["code"] for v in made.of("verdict")} <= {"NOT_FUNDED", "VENUE_NOT_READY", "NOT_RECONCILED"}


def test_a_hold_a_float_cannot_answer_still_refuses_the_start_above_shadow(tmp_path):
    put_stage(tmp_path, "control", "min_size")
    made = Bureau(tmp_path, start=False)
    made.cannot_open, made.needs_float = capability.split(measured(EUR_USD=FOR_CASH, AUD_USD=UNKNOWN), PAIRS[:2])
    with pytest.raises(TraderRefused, match=r"AUD\.USD: buy refused"):
        made.start()


def test_the_command_sorts_the_holds_by_whether_the_limits_keep_a_float(tmp_path):
    import argparse

    file = tmp_path / "ibkr.yaml"
    file.write_text(yaml.safe_dump(AS_PROBED))
    args = argparse.Namespace(capability=file)
    where = load(ROOT / "config" / "trader.yaml")
    where = type(where)(**{**{f: getattr(where, f) for f in where.__dataclass_fields__}, "pairs": PAIRS})
    with_float = Bureau(tmp_path / "a", start=False).risk
    assert cli.account_cannot_open(args, where, None)[1] == {}
    assert set(cli.account_cannot_open(args, where, None)[0]) == {"EUR.USD", "AUD.USD", "USD.JPY"}
    blocked, unfunded = cli.account_cannot_open(args, where, with_float)
    assert blocked == {} and set(unfunded) == {"EUR.USD", "AUD.USD"}
    assert loaded_limits()["inventory"]["shadow_only"] == ["USD.JPY"] and NEVER
