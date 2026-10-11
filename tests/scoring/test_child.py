"""The child: it answers every question it is asked, refuses what it cannot answer, and ends only on its input."""
from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from hftlab.models import scorer_for
from hftlab.models.scorer import answer, ask
from hftlab.scoring import wire
from hftlab.scoring.child import EXIT_OK, EXIT_WIRE
from hftlab.scoring.wire import LIMIT, VERSION, frame, read_frame
from tests.scoring.kit import kept, ridge, rows, trees
from tests.support import ROOT


def talk(models, data: bytes) -> tuple[int, list[dict], str]:
    """Start the real child, hand it these bytes and then the end of its input. Its exit code, replies and stderr."""
    command = [sys.executable, "-m", "hftlab.scoring.child", "--models", str(models)]
    environment = {"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run(command, input=data, capture_output=True, env=environment, timeout=60, check=False)
    stream, replies = io.BytesIO(done.stdout), []
    while (line := read_frame(stream.read)) is not None:
        replies.append(json.loads(line))
    return done.returncode, replies, done.stderr.decode()


@pytest.mark.parametrize("make", [ridge, trees])
def test_it_loads_a_model_by_its_hash_and_answers_each_row_as_the_scorer_in_process_does(tmp_path, make):
    model = make()
    shown = rows(200)
    asked = frame(wire.load(model.content_hash)) + b"".join(frame(ask(row)) for row in shown)
    code, replies, stderr = talk(kept(tmp_path, model), asked)
    assert code == EXIT_OK and stderr == "", "its input ended, and it left without a word"
    assert replies[0] == {"loaded": model.content_hash}
    scorer = scorer_for(model)
    assert replies[1:] == [json.loads(answer(scorer, ask(row))) for row in shown]
    assert {reply["side"] for reply in replies[1:]} == {"BUY", "SELL", None}, "both sides and an abstention"


def test_a_question_it_cannot_answer_is_refused_in_a_frame_and_the_next_one_is_answered(tmp_path):
    model = trees()
    other = ridge()
    (kept(tmp_path, model) / f"{other.content_hash}.json").write_bytes(model.data())  # named for another model
    row = rows(1)[0]
    good = ask(row)
    questions = [
        good,  # before any model is loaded
        wire.load("0" * 64),  # no such file
        wire.load(other.content_hash),  # a file that is not what its name says
        wire.load("../" + model.content_hash),  # not a hash at all
        json.dumps({"load": 7}),
        wire.load(model.content_hash),
        "not json",
        "[]",
        "{}",
        json.dumps({"pair": "EUR.USD", "n": 1, "known_ns": 1, "values": [1.5] * 10}),  # a float
        json.dumps({"pair": "EUR.USD", "n": 1, "known_ns": 1, "values": [True] * 10}),
        json.dumps({"pair": "EUR.USD", "n": 1, "known_ns": 1, "values": [1] * 9}),  # too few features
        json.dumps({"pair": "USD.JPY", "n": 1, "known_ns": 1, "values": [1] * 10}),  # another pair
        json.dumps({"pair": "EUR.USD", "n": 1, "known_ns": 1, "values": "0123456789"}),
        json.dumps({"load": model.content_hash, "also": 1}),
        "[" * 30_000,
        good,
    ]
    code, replies, stderr = talk(tmp_path, b"".join(frame(line) for line in questions))
    assert code == EXIT_OK and stderr == "" and len(replies) == len(questions), "one answer a question; none ended it"
    refused = [set(reply) == {"refused"} for reply in replies]
    assert refused == [True] * 5 + [False] + [True] * 10 + [False]
    assert "no model is loaded" in replies[0]["refused"] and "there is no model" in replies[1]["refused"]
    assert "not the one it is named for" in replies[2]["refused"] and "SHA-256" in replies[3]["refused"]
    assert replies[5] == {"loaded": model.content_hash}
    assert replies[-1] == json.loads(answer(scorer_for(model), good)), "and after all of it, the same answer"


@pytest.mark.parametrize(
    "bad",
    [
        bytes([9, 0, 0, 0, 2]) + b"{}",
        bytes([VERSION]) + (LIMIT + 1).to_bytes(4, "big") + b"x" * (LIMIT + 1),
        bytes([VERSION, 0, 0, 0, 0]),
        b"GET / HTTP/1.1\r\n\r\n",
    ],
)
def test_bytes_that_are_not_a_frame_end_the_conversation_with_a_reason_and_without_a_traceback(tmp_path, bad):
    model = trees()
    code, replies, stderr = talk(kept(tmp_path, model), frame(wire.load(model.content_hash)) + bad + frame("{}"))
    assert code == EXIT_WIRE and stderr == ""
    assert replies[0] == {"loaded": model.content_hash} and set(replies[1]) == {"refused"} and len(replies) == 2


def test_an_input_that_ends_inside_a_frame_is_not_a_clean_end(tmp_path):
    code, replies, stderr = talk(kept(tmp_path, trees()), frame("{}")[:-1])
    assert code == EXIT_WIRE and stderr == "" and "ended inside" in replies[0]["refused"]


def test_a_message_of_the_largest_size_is_taken_and_answered(tmp_path):
    model = trees()
    huge = json.dumps({"pair": "EUR.USD", "n": 1, "known_ns": 1, "values": [1] * 10, "pad": "x" * 60_000})
    code, replies, _ = talk(kept(tmp_path, model), frame(wire.load(model.content_hash)) + frame(huge))
    assert len(frame(huge)) > 60_000 and code == EXIT_OK, "below the limit: read whole, and answered"
    assert replies[1] == json.loads(answer(scorer_for(model), huge))


def test_started_without_its_folder_it_says_so_and_exits():
    done = subprocess.run(
        [sys.executable, "-m", "hftlab.scoring.child"], capture_output=True, timeout=60, check=False,
        env={"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )  # fmt: skip
    assert done.returncode == 2 and b"--models" in done.stderr and done.stdout == b""
