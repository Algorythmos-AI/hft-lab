"""The message format: a line goes down a pipe whole or not at all, and nothing that is not a frame is read on."""
from __future__ import annotations

import io
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.features import FeatureRow
from hftlab.models.scorer import ask
from hftlab.scoring import wire
from hftlab.scoring.wire import HEAD, LIMIT, VERSION, WireError, frame, read_frame, said

LINES = st.text(st.characters(min_codepoint=32, max_codepoint=126), min_size=1, max_size=300)


class Counting:
    """A stream that keeps how many bytes were asked of it."""

    def __init__(self, data: bytes) -> None:
        self._stream, self.taken = io.BytesIO(data), 0

    def read(self, count: int) -> bytes:
        got = self._stream.read(count)
        self.taken += len(got)
        return got


@given(st.lists(LINES, max_size=5))
def test_lines_come_out_of_a_stream_as_they_went_in_and_the_end_between_two_frames_is_the_end(lines):
    stream = io.BytesIO(b"".join(frame(line) for line in lines))
    assert [read_frame(stream.read) for _ in lines] == lines
    assert read_frame(stream.read) is None and read_frame(stream.read) is None


def test_a_frame_is_one_byte_of_version_four_of_length_and_the_line():
    assert frame("{}") == bytes([1, 0, 0, 0, 2]) + b"{}" and (VERSION, HEAD, LIMIT) == (1, 5, 65_536)
    assert len(frame("x" * LIMIT)) == HEAD + LIMIT
    for line in ("", "x" * (LIMIT + 1), "café"):
        with pytest.raises(WireError):
            frame(line)


def test_a_stream_that_delivers_a_byte_at_a_time_gives_the_same_line():
    stream = io.BytesIO(frame("a line of some length"))
    assert read_frame(lambda count: stream.read(1)) == "a line of some length"


@pytest.mark.parametrize(
    ("head", "said_why"),
    [
        (bytes([2, 0, 0, 0, 2]), "version 2"),
        (bytes([0, 0, 0, 0, 2]), "version 0"),
        (bytes([VERSION, 0, 0, 0, 0]), "0 bytes"),
        (bytes([VERSION]) + (LIMIT + 1).to_bytes(4, "big"), "65537 bytes"),
        (bytes([VERSION, 255, 255, 255, 255]), "4294967295 bytes"),
    ],
)
def test_a_frame_of_another_version_or_an_impossible_length_is_refused_before_a_byte_of_it_is_read(head, said_why):
    stream = Counting(head + b"x" * 100)
    with pytest.raises(WireError, match=said_why):
        read_frame(stream.read)
    assert stream.taken == HEAD, "the length cannot be believed, so nothing after the head is taken"


@pytest.mark.parametrize("cut", [1, 4, 5, 6, 20])
def test_a_stream_that_ends_inside_a_frame_is_an_error_and_not_an_end(cut):
    whole = frame("a line of some length")
    with pytest.raises(WireError, match="ended inside"):
        read_frame(io.BytesIO(whole[:cut]).read)
    with pytest.raises(WireError, match="ASCII"):
        read_frame(io.BytesIO(bytes([VERSION, 0, 0, 0, 1, 200])).read)


def test_a_message_is_one_object_of_whole_numbers_and_text():
    row = FeatureRow("EUR.USD", 3, 4, (-(10**40), 0, 10**40))
    assert said(ask(row)) == {"pair": "EUR.USD", "n": 3, "known_ns": 4, "values": [-(10**40), 0, 10**40]}
    assert said(wire.load("a" * 64)) == {"load": "a" * 64} and said(wire.loaded("b" * 64)) == {"loaded": "b" * 64}
    for line in ("", "nothing", "[1]", "7", '"text"', '{"a":1.5}', '{"a":1e3}', '{"a":[NaN]}', '{"a":-Infinity}',
                 "[" * 20_000, '{"a":' + "1" * 5_000 + "}"):  # fmt: skip
        with pytest.raises(WireError):
            said(line)


def test_a_refusal_carries_its_reason_as_plain_text_that_fits_a_frame():
    reply = json.loads(wire.refused("no\nsuch \x00 thing é" + "x" * 1_000))
    assert set(reply) == {"refused"} and len(reply["refused"]) == 200
    assert reply["refused"].startswith("no?such ? thing ?")
    assert read_frame(io.BytesIO(frame(wire.refused("x" * 10**6))).read) is not None
