"""The import: files on a disk become days in a store of their own, that say what they are and never change."""
from __future__ import annotations

import ast
import hashlib
import json
import shutil

import pytest

from hftlab.calendar import Calendar
from hftlab.core import Quote
from hftlab.history import __main__ as cli
from hftlab.history.importer import ImportRefused, check_root
from hftlab.record import __main__ as recorder_cli
from hftlab.record.origin import IMPORTED, OWN, Origin, origin_of
from hftlab.record.segment import header_of
from hftlab.record.store import Housekeeper, WrongStore, days, read_journal, segments
from hftlab.replay import check, events, instruments
from tests.history.kit import GENERIC, LABEL, SIZE_COLUMNS, SIZED, a_folder, run, tree
from tests.record.kit import ROOMY, SECOND, T0, a_store, quote
from tests.support import ROOT, called_name, imports, sources
from tests.venues.kit import EURUSD, USDJPY

DAY = "2026-10-14"
PACKAGE = "src/hftlab/history/"
WIRES = {"ib_async", "socket", "ssl", "asyncio", "http", "urllib", "subprocess", "ftplib", "requests", "httpx"}
THRESHOLD = 5 * SECOND
NOT_ITS_BUSINESS = ("hftlab.venues", "hftlab.safety", "hftlab.ops", "hftlab.trader", "hftlab.exec", "hftlab.strategy")


# ---- what is written -------------------------------------------------------------------------------------------------


def test_an_imported_day_is_read_back_by_the_replay_as_the_quotes_the_file_held(tmp_path):
    report = run(tmp_path / "store", a_folder(tmp_path / "in")).as_dict()
    day = tmp_path / "store" / DAY
    assert list(events(day)) == [
        Quote("EUR.USD", 108_500, 108_510, 1, 1, T0, 1),
        Quote("EUR.USD", 108_501, 108_511, 1, 1, T0 + 1_250_000_000, 2),
        Quote("EUR.USD", 108_502, 108_512, 1, 1, T0 + 2_500_000_000, 3),
    ]
    assert instruments(day) == {"EUR.USD": EURUSD}  # the day can be priced by itself
    assert report["days"] == [
        {"day": DAY, "rows": 3, "pairs": {"EUR.USD": 3}, "sizes": "placeholder", "gaps": {}, "dropped": {},
         "status": "written"}
    ]  # fmt: skip


def test_the_day_check_passes_an_imported_day_and_its_silences_are_journalled_as_the_recorder_journals_them(tmp_path):
    quiet = GENERIC + "2026-10-14 03:00:30.000,1.08503,1.08513\n2026-10-14 04:10:00.000,1.08504,1.08514\n"
    report = run(tmp_path / "store", a_folder(tmp_path / "in", {"ticks.csv": quiet})).as_dict()
    day = tmp_path / "store" / DAY
    found = check(day, Calendar(), THRESHOLD)
    assert found.ok and found.problems == [] and found.quotes == found.records == 5 and found.segments == 2
    assert found.gaps == {"unexplained": 2} == report["days"][0]["gaps"]  # nobody knows why another feed was silent
    assert found.runs == 0 and found.heartbeats == 0  # nothing pretends a recorder ran
    assert [path.name for path in segments(day)] == ["03.seg", "04.seg"]
    assert [line["type"] for line in read_journal(day)] == [
        "provenance", "carry", "instruments", "gap", "gap", "imported",
    ]  # fmt: skip


def test_provenance_is_the_first_line_of_every_imported_day(tmp_path):
    folder = a_folder(tmp_path / "in", {"a.csv": GENERIC, "b.csv": GENERIC.replace("2026-10-14", "2026-10-15")})
    run(tmp_path / "store", folder)
    names = ("a.csv", "b.csv")
    hashes = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in names}
    for day, name in ((DAY, "a.csv"), ("2026-10-15", "b.csv")):
        first = read_journal(tmp_path / "store" / day)[0]
        assert first["type"] == "provenance" and first["source"] == "imported" and first["label"] == LABEL
        assert first["terms_verified"] is False
        assert (first["parser"], first["parser_version"], first["time_zone"]) == ("generic", 1, "UTC")
        assert first["inputs"] == [
            {"name": name, "sha256": hashes[name], "pair": "EUR.USD", "rows_read": 3, "rows_kept": 3}
        ]  # only the file that had a row in this day
        assert (first["sizes"], first["placeholder_size"]) == ("placeholder", 1)
        assert first["stated"]["time_format"] == "%Y-%m-%d %H:%M:%S.%f" and first["stated"]["size_unit"] is None
    marker = json.loads((tmp_path / "store" / "import-store.json").read_text())
    assert marker["kind"] == "import-store" and marker["terms_verified"] is False


def test_an_imported_day_says_so_twice_and_a_lost_journal_does_not_make_it_a_recording(tmp_path):
    run(tmp_path / "store", a_folder(tmp_path / "in"))
    day = tmp_path / "store" / DAY
    assert origin_of(day) == Origin(IMPORTED, LABEL, placeholder_sizes=True)
    assert origin_of(day).key == "imported:vendor-a"
    assert {header_of(path).run for path in segments(day)} == {0}  # a recorder's runs count from 1
    (day / "journal.jsonl").unlink()
    assert origin_of(day).key == "imported:unlabelled" and origin_of(day).placeholder_sizes
    quote(a_store(tmp_path / "record"), T0)
    assert all(origin_of(day) == OWN and OWN.key == "recorded" for day in days(tmp_path / "record"))


def test_stated_sizes_are_written_in_base_units_and_the_day_says_they_were_stated(tmp_path):
    folder = a_folder(tmp_path / "in", {"ticks.csv": SIZED}, columns=SIZE_COLUMNS, size_unit="1000000")
    run(tmp_path / "store", folder)
    day = tmp_path / "store" / DAY
    assert [(q.bid_size, q.ask_size) for q in events(day)] == [(1_500_000, 2_000_000), (750_000, 1_000_000)]
    first = read_journal(day)[0]
    assert (first["sizes"], first["placeholder_size"], first["stated"]["size_unit"]) == ("stated", None, "1000000")
    assert origin_of(day) == Origin(IMPORTED, LABEL, placeholder_sizes=False)


def test_two_files_of_one_day_become_one_stream_in_time_order_and_two_layouts_read_the_same_quotes(tmp_path):
    yen = "Time,Bid,Ask\n2026-10-14 03:00:00.500,151.230,151.240\n2026-10-14 03:00:02.000,151.231,151.241\n"
    run(tmp_path / "store", a_folder(tmp_path / "in", {"e.csv": GENERIC, "y.csv": yen}, pairs={"y.csv": "USD.JPY"}))
    day = tmp_path / "store" / DAY
    assert [(q.pair, q.bid, q.seq) for q in events(day)] == [
        ("EUR.USD", 108_500, 1), ("USD.JPY", 151_230, 2), ("EUR.USD", 108_501, 3), ("USD.JPY", 151_231, 4),
        ("EUR.USD", 108_502, 5),
    ]  # fmt: skip
    assert instruments(day) == {"EUR.USD": EURUSD, "USD.JPY": USDJPY} and check(day, Calendar(), THRESHOLD).ok
    # the same three quotes, written the two other ways: UTC with the ask first, and New York time with the bid first
    utc = "t,a,b,av,bv\n14.10.2026 03:00:00.000,1.08510,1.08500,1,1\n14.10.2026 03:00:01.250,1.08511,1.08501,1,1\n"
    utc += "14.10.2026 03:00:02.500,1.08512,1.08502,1,1\n"
    local = "20261013 230000000,1.08500,1.08510,0\n20261013 230001250,1.08501,1.08511,0\n"
    local += "20261013 230002500,1.08502,1.08512,0\n"
    fixed = {"delimiter": None, "time_format": None, "columns": None}
    run(tmp_path / "a", a_folder(tmp_path / "in-a", {"t.csv": utc}, layout="utc_ask_bid", size_unit="1", **fixed))
    zone = {"time_zone": "America/New_York", "header": False}
    run(tmp_path / "b", a_folder(tmp_path / "in-b", {"t.csv": local}, layout="local_bid_ask", **zone, **fixed))
    plain = list(events(run_plain(tmp_path)))
    assert list(events(tmp_path / "a" / DAY)) == plain == list(events(tmp_path / "b" / DAY))
    assert origin_of(tmp_path / "a" / DAY).placeholder_sizes is False
    assert origin_of(tmp_path / "b" / DAY).placeholder_sizes


def run_plain(tmp_path):
    run(tmp_path / "plain", a_folder(tmp_path / "in-plain"))
    return tmp_path / "plain" / DAY


# ---- the same inputs, the same bytes; a day is written once ----------------------------------------------------------


def test_the_same_files_give_the_same_bytes_wherever_and_whenever_they_are_imported(tmp_path):
    minutes = [(m, s) for m in range(1, 9) for s in range(60)]
    many = GENERIC + "".join(f"2026-10-14 03:{m:02d}:{s:02d}.000,1.085{m:02d},1.086{s:02d}\n" for m, s in minutes)
    many += "2026-10-15 09:00:00.000,1.08500,1.08510\n"
    run(tmp_path / "one", a_folder(tmp_path / "in-1", {"ticks.csv": many}))
    run(tmp_path / "two" / "deeper", a_folder(tmp_path / "in-2", {"ticks.csv": many}))
    first = tree(tmp_path / "one")
    assert first == tree(tmp_path / "two" / "deeper")
    assert len(first) == 1 + 2 * 2  # the marker; a segment and a journal a day
    assert check(tmp_path / "one" / DAY, Calendar(), THRESHOLD).records == 3 + 8 * 60  # more than one block


def test_importing_a_day_twice_changes_nothing(tmp_path):
    folder = a_folder(tmp_path / "in")
    run(tmp_path / "store", folder)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted((tmp_path / "store").rglob("*"))
              if path.is_file()}  # fmt: skip
    again = run(tmp_path / "store", folder).as_dict()
    assert again["days_by_status"] == {"unchanged": 1}
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted((tmp_path / "store").rglob("*"))
             if path.is_file()}  # fmt: skip
    assert after == before and not (tmp_path / "store" / ".staging").exists()


def test_a_changed_file_another_label_or_another_file_for_a_day_already_imported_is_refused_not_merged(tmp_path):
    folder = a_folder(tmp_path / "in")
    run(tmp_path / "store", folder)
    before = tree(tmp_path / "store")
    changed = a_folder(tmp_path / "changed", {"ticks.csv": GENERIC.replace("1.08502", "1.08503")})
    more = a_folder(tmp_path / "more", {"ticks.csv": GENERIC, "late.csv": GENERIC.replace("03:00:0", "05:00:0")})
    new_day = GENERIC.replace("2026-10-14", "2026-10-15")
    both = a_folder(tmp_path / "both", {"ticks.csv": GENERIC.replace("1.08502", "1.08503"), "next.csv": new_day})
    for other, label in ((changed, LABEL), (folder, "vendor-b"), (more, LABEL), (both, LABEL)):
        with pytest.raises(ImportRefused, match=f"already imported.*{DAY}.*never merged"):
            run(tmp_path / "store", other, label)
        assert tree(tmp_path / "store") == before  # and the new day of a refused run is not written either


def test_a_dry_run_reads_and_counts_everything_and_writes_nothing(tmp_path):
    report = run(tmp_path / "store", a_folder(tmp_path / "in"), dry_run=True).as_dict()
    assert report["dry_run"] is True and report["rows"] == {"read": 3, "kept": 3}
    assert report["days_by_status"] == {"would_write": 1} and not (tmp_path / "store").exists()


# ---- two stores that cannot be taken for one another -----------------------------------------------------------------


def test_the_recorder_and_its_housekeeping_refuse_to_open_a_store_of_imported_history(tmp_path):
    run(tmp_path / "store", a_folder(tmp_path / "in"))
    before = tree(tmp_path / "store")
    with pytest.raises(WrongStore, match="imported history"):
        a_store(tmp_path / "store")
    with pytest.raises(WrongStore):
        Housekeeper(tmp_path / "store", ROOMY).run(T0 + 10**15)
    (tmp_path / "store" / "import-store.json").unlink()  # the marker is one of two things that say so
    with pytest.raises(WrongStore):
        a_store(tmp_path / "store")
    assert tree(tmp_path / "store") == {k: v for k, v in before.items() if k != "import-store.json"}
    assert not (tmp_path / "store" / "state.json").exists()


def test_the_record_command_exits_2_on_an_import_store_and_writes_nothing(tmp_path, capsys):
    run(tmp_path / "store", a_folder(tmp_path / "in"))
    before = tree(tmp_path / "store")
    config = str(ROOT / "config" / "recorder.yaml")
    for more in (["--dry-run-seconds", "1"], ["--housekeep"]):
        assert recorder_cli.main(["--config", config, "--root", str(tmp_path / "store"), *more]) == 2
        assert "imported history" in capsys.readouterr().err
    assert tree(tmp_path / "store") == before


def test_the_importer_refuses_to_write_into_the_recorder_s_store(tmp_path):
    folder = a_folder(tmp_path / "in")
    recorded = a_store(tmp_path / "record")
    quote(recorded, T0)
    recorded.close()
    before = tree(tmp_path / "record")
    with pytest.raises(ImportRefused, match="recorder's store"):
        run(tmp_path / "record", folder)
    (tmp_path / "record" / "state.json").unlink()
    with pytest.raises(ImportRefused, match="recorded days"):
        run(tmp_path / "record", folder)
    assert tree(tmp_path / "record") == {k: v for k, v in before.items() if k != "state.json"}
    with pytest.raises(ImportRefused, match="where the recorder writes"):  # by its configured place, even when empty
        run(tmp_path / "empty", folder, recorder_root=tmp_path / "other" / ".." / "empty")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "anything.txt").write_text("x")
    with pytest.raises(ImportRefused, match="not empty"):
        run(tmp_path / "notes", folder)
    check_root(tmp_path / "not-there-yet", tmp_path / "record")


def test_a_run_that_is_refused_or_was_killed_leaves_the_days_in_the_store_as_they_were(tmp_path):
    run(tmp_path / "store", a_folder(tmp_path / "in"))
    before = tree(tmp_path / "store")
    left = tmp_path / "store" / ".staging" / "2026-10-20"
    left.mkdir(parents=True)  # what a killed run leaves
    (left / "03.seg").write_bytes(b"half a segment")
    files = {"ok.csv": GENERIC.replace("2026-10-14", "2026-10-15"), "torn.csv": GENERIC + "x,y\n"}
    bad = a_folder(tmp_path / "bad", files)
    with pytest.raises(ImportRefused, match=r"torn\.csv: line 5"):
        run(tmp_path / "store", bad)
    assert tree(tmp_path / "store") == before  # the good file of the refused run is not imported either
    assert [day.name for day in days(tmp_path / "store")] == [DAY]


# ---- the command -----------------------------------------------------------------------------------------------------


def arguments(tmp_path, folder, *more):
    config = tmp_path / "recorder.yaml"
    config.write_text(f"root: {tmp_path / 'record'}\n")
    return ["import", "--from", str(folder), "--label", LABEL, "--store", str(tmp_path / "store"),
            "--recorder-config", str(config), "--calendar", str(ROOT / "config" / "calendar"), *more]  # fmt: skip


def test_the_command_prints_a_report_and_exits_0_and_2_when_it_refuses(tmp_path, capsys):
    folder = a_folder(tmp_path / "in")
    assert cli.main(arguments(tmp_path, folder)) == 0
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert report["kind"] == "import-report" and report["label"] == LABEL and report["terms_verified"] is False
    assert report["files"][0]["rows_kept"] == 3 and len(report["files"][0]["sha256"]) == 64
    assert "terms_verified: false" in err and "not this venue's quotes" in err
    assert cli.main(arguments(tmp_path, folder, "--store", str(tmp_path / "record"))) == 2  # the recorder's own place
    assert "where the recorder writes" in capsys.readouterr().err
    assert cli.main([*arguments(tmp_path, folder)[:4], "two words", *arguments(tmp_path, folder)[5:]]) == 2
    assert "source label" in capsys.readouterr().err
    shutil.rmtree(tmp_path / "in")
    assert cli.main(arguments(tmp_path, tmp_path / "in")) == 2 and "refused, nothing written" in capsys.readouterr().err


# ---- it reads files, and nothing else --------------------------------------------------------------------------------


def history_sources():
    found = [(rel, tree_) for rel, tree_ in sources() if rel.startswith(PACKAGE)]
    assert [rel.removeprefix(PACKAGE) for rel, _ in found] == [
        "__init__.py", "__main__.py", "describe.py", "importer.py", "parsers.py", "rows.py", "times.py",
    ]  # fmt: skip
    return found


def test_the_package_imports_no_network_library_holds_no_address_and_reaches_no_venue():
    for rel, module in history_sources():
        for how, name in imports(module, rel):
            assert how in ("import", "from"), f"{rel}: an import by a computed or dynamic name"
            assert name.split(".")[0] not in WIRES, (rel, name)
            assert not name.startswith(NOT_ITS_BUSINESS), (rel, name)
        for node in ast.walk(module):
            if isinstance(node, ast.Call):
                assert called_name(node) not in {"place", "cancel", "connect", "urlopen", "getattr", "__import__"}, rel
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.casefold()
                assert not any(part in text for part in ("://", "www.", "http", "ftp")), f"{rel} line {node.lineno}"


def test_the_package_reads_no_clock_and_draws_nothing():
    """What it writes is a function of the files, the description, the label and the calendar."""
    for rel, module in history_sources():
        tops = {name.split(".")[0] for _, name in imports(module, rel)}
        assert not tops & {"time", "random", "secrets", "uuid"}, rel
        for node in ast.walk(module):
            if isinstance(node, ast.Call):
                assert called_name(node) not in {"now", "utcnow", "today", "time_ns"}, f"{rel} line {node.lineno}"
