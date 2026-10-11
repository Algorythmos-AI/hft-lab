"""The description file states everything about a folder. What it leaves unstated is refused, never worked out."""
from __future__ import annotations

import pytest

from hftlab.history.describe import DescriptionError, load
from hftlab.record.origin import source_label
from tests.history.kit import SIZE_COLUMNS, SIZED, a_folder


def test_a_whole_description_is_read_as_stated(tmp_path):
    said = load(a_folder(tmp_path, {"b.csv": SIZED, "a.csv": SIZED}, columns=SIZE_COLUMNS, size_unit="1000000"))
    assert (said.layout, said.time_zone, said.header, said.delimiter) == ("generic", "UTC", True, ",")
    assert [(one.name, one.pair) for one in said.files] == [("a.csv", "EUR.USD"), ("b.csv", "EUR.USD")]
    assert said.has_sizes and said.size_unit == "1000000" and said.size_unit_scaled == 10**15
    assert said.instruments["EUR.USD"].price_scale == 5


def test_a_file_with_no_stated_time_zone_is_refused(tmp_path):
    with pytest.raises(DescriptionError, match=r"missing.*time_zone"):
        load(a_folder(tmp_path / "a", time_zone=None))
    with pytest.raises(DescriptionError, match="never guessed"):
        load(a_folder(tmp_path / "b", time_zone=""))
    with pytest.raises(DescriptionError, match="time_zone"):
        load(a_folder(tmp_path / "c", time_zone="EASTERN"))


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"version": 2}, "version 1"),
        ({"layout": "anything"}, "layout"),
        ({"header": "yes"}, "true or false"),
        ({"time_format": None}, "time_format"),
        ({"time_format": "%Y-%m-%d"}, "time_format"),
        ({"columns": {"time": "Time", "bid": "Bid"}}, "columns"),
        ({"columns": {"time": "Time", "bid": "Bid", "ask": "Ask", "bid_size": "B"}}, "both size columns"),
        ({"columns": {"time": 1, "bid": 2, "ask": 3}}, "header name"),
        ({"columns": {"time": "T", "bid": "T", "ask": "A"}}, "twice"),
        ({"delimiter": "ab"}, "delimiter"),
        ({"size_unit": "1"}, "size_unit"),  # stated, and the layout has no sizes
        ({"columns": SIZE_COLUMNS}, "size_unit"),  # sizes, and no unit stated
        ({"columns": SIZE_COLUMNS, "size_unit": 1000000}, "quoted"),
        ({"columns": SIZE_COLUMNS, "size_unit": "0"}, "above zero"),
        ({"instruments": []}, "at least one pair"),
        ({"guess_zone": True}, "not understood"),
        ({"layout": "utc_ask_bid", "delimiter": None, "time_format": None, "columns": None, "size_unit": "1",
          "time_zone": "+02:00"}, "must say UTC"),
        ({"layout": "local_bid_ask", "delimiter": None, "time_format": None, "columns": None, "size_unit": "1"},
         "not understood"),
    ],
)  # fmt: skip
def test_what_is_unstated_unknown_or_contradictory_is_refused(tmp_path, changes, why):
    with pytest.raises(DescriptionError, match=why):
        load(a_folder(tmp_path, **changes))


def test_every_file_in_the_folder_is_listed_and_every_listed_file_is_there(tmp_path):
    folder = a_folder(tmp_path / "extra")
    (folder / "nobody-described-this.csv").write_text("x\n")
    with pytest.raises(DescriptionError, match=r"unlisted.*nobody-described-this"):
        load(folder)
    folder = a_folder(tmp_path / "absent")
    (folder / "ticks.csv").unlink()
    with pytest.raises(DescriptionError, match=r"absent.*ticks\.csv"):
        load(folder)
    with pytest.raises(DescriptionError, match="cannot read"):
        load(tmp_path / "nothing-here")
    with pytest.raises(DescriptionError, match="does not describe"):
        load(a_folder(tmp_path / "pair", pairs={"ticks.csv": "GBP.USD"}))


@pytest.mark.parametrize("wrong", ["", "two words", "a/b", "-a", "unlabelled", "x" * 65, None, 7, "caf\xe9"])
def test_a_source_label_has_no_space_and_nothing_that_is_not_safe_in_a_key(wrong):
    with pytest.raises(ValueError, match="source label"):
        source_label(wrong)
    assert source_label("vendor-a_2026.v1") == "vendor-a_2026.v1"
