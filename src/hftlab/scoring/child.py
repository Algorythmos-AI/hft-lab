"""`python -m hftlab.scoring.child --models DIR`: a scorer in a process of its own (ADR 0023, section 5).

Started by the trader and by nobody else. It reads frames from its standard input and answers each on its
standard output (`hftlab.scoring.wire`):

  * asked to load a model, it reads `DIR/<hash>.json`, refuses a file that is not what its name says, and
    answers with the hash of what it now holds, worked out from the bytes it read;
  * asked about a row, it answers with the scorer's reply line: a side and whole millionths, or an abstention.
    The arithmetic is the in-process scorer's, because it is the same code (`hftlab.models.scorer`);
  * a question it cannot answer (a line that is not a message, a row before any model, a row that is not the
    model's) is refused in a frame, and it goes on.

It ends when its input ends, which is how the trader stops it and what happens when the trader dies. It also
ends, after saying why, on bytes that are not a frame: the length of such a message cannot be believed.

It holds no venue and opens nothing but the one model file. What it imports is the model file, the scorers
and the wire format, and a test starts it and looks (tests/scoring/test_structure.py).

Exit codes: 0 the input ended, 2 it was started wrongly, 3 the input was not frames.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from hftlab.models.file import ModelError, parse
from hftlab.models.scorer import ScoreError, Scorer, answer, scorer_for
from hftlab.scoring import wire

EXIT_OK, EXIT_USAGE, EXIT_WIRE = 0, 2, 3
_HASH = re.compile(r"[0-9a-f]{64}")


def held(models: Path, digest: object) -> Scorer:
    """The scorer of the model kept under this hash. Refused unless the file is that model, whole."""
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ModelError("a model is asked for by its SHA-256")
    path = models / f"{digest}.json"
    try:
        model = parse(path.read_bytes())
    except OSError as exc:
        raise ModelError(f"there is no model {digest[:12]} here: {type(exc).__name__}") from exc
    if model.content_hash != digest:
        raise ModelError(f"{path.name} holds model {model.content_hash[:12]}, not the one it is named for")
    return scorer_for(model)


def reply(models: Path, scorer: Scorer | None, line: str) -> tuple[Scorer | None, str]:
    """The scorer in hand after this question, and the answer to it."""
    asked = wire.said(line)
    if set(asked) == {"load"}:
        found = held(models, asked["load"])
        return found, wire.loaded(found.model.content_hash)
    if scorer is None:
        raise ScoreError("no model is loaded")
    return scorer, answer(scorer, line)


def serve(models: Path, source: IO[bytes], sink: IO[bytes]) -> int:
    """Answer frames until the input ends. No question ends it; bytes that are not a frame do."""
    scorer: Scorer | None = None
    while True:
        try:
            line = wire.read_frame(source.read)
        except wire.WireError as exc:
            _say(sink, wire.refused(str(exc)))
            return EXIT_WIRE
        if line is None:
            return EXIT_OK
        try:
            scorer, said = reply(models, scorer, line)
            sent = wire.frame(said)
        except (ValueError, ArithmeticError, LookupError, TypeError, RecursionError) as exc:
            # Whatever one question does, it is answered and the next one is read: a refusal, never an exit.
            sent = wire.frame(wire.refused(f"{type(exc).__name__}: {exc}"))
        sink.write(sent)
        sink.flush()


def _say(sink: IO[bytes], line: str) -> None:
    sink.write(wire.frame(line))
    sink.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.scoring.child", description="Score rows for the trader.")
    parser.add_argument("--models", type=Path, required=True, help="the folder of model files, each named by its hash")
    args = parser.parse_args(argv)
    try:
        return serve(args.models, sys.stdin.buffer, sys.stdout.buffer)
    except BrokenPipeError:
        return EXIT_OK  # the trader is gone: there is nobody to answer


if __name__ == "__main__":
    sys.exit(main())
