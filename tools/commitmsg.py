#!/usr/bin/env python3
"""Commit-message check (org decision D-016): no co-author trailer, no assistant or tool name.

The refused names are kept as SHA-256 digests of the lower-cased word, so this file does not spell them and
passes its own rule. To refuse another name, add `printf '%s' name | shasum -a 256`.

Standard library only and Python 3.9+, like the leak check, so the hook works before `make bootstrap`.
"""
from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Sequence
from pathlib import Path

DENIED_SHA256 = frozenset(
    {
        "053ea4804ef1bb33d4a3d6fb024a614b6d257cebc2bc7cd915da9c9522f37ffc",
        "0ac0192d441f96da3a4a3174330c3a74e034a3a4704fded0e87515d1a97c19dc",
        "3ea125d0bff386e6754b3782b300016fc79a9cf8f8669c0a5c3db64467ddb681",
        "57de4cf40144bdf7d00010f2f5557a7d642c2b9705309bfade167dd313e2ca93",
        "5d0c0ab127fdea24d94e7e3326b6ceb965ab82ad897f9de1bd6b1a9a23f87578",
        "5d72436256ada53828b51895a94bb8489e9f1ac4fe937a8024ef1594e7045ff6",
        "60965168ce762e949600281ba6d01fee136e5b6e8257b1f216f9025ed324474c",
        "7d3194f79e645c42e4396dda38be04766810ec6a00d00aced3ffc2a0a1f1a9ef",
        "97f128115e33c7693db6156c493d1fcbe0ebf4a870b6b49cd4b6b865233f7725",
        "c70eca6b0f88f44d81a41311647e50fda1ac454ec04ffd442b0eb4743a993131",
        "c857d09db23e6822e3600bc06ad8d58f92ed62bc8efd81c753f77048662cb97d",
        "ebde709e306badca2f843f1ef91c7fb216a99aa4417b32fe911e15acfff8bedd",
    }
)
_TRAILER = re.compile(r"co-authored-by", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+")
_SCISSORS = re.compile(r"^# -+ >8 -+$")


def body(text: str) -> str:
    """The message git will keep: without comment lines and without anything below the scissors line."""
    kept = []
    for line in text.splitlines():
        if _SCISSORS.match(line):
            break
        if not line.startswith("#"):
            kept.append(line)
    return "\n".join(kept)


def denied_words(text: str, denied: frozenset[str] = DENIED_SHA256) -> list[str]:
    """The refused words in `text`, each once, in order of first appearance."""
    found: dict[str, None] = {}
    for word in _WORD.findall(text.lower()):
        if hashlib.sha256(word.encode("ascii")).hexdigest() in denied:
            found.setdefault(word)
    return list(found)


def violations(text: str, denied: frozenset[str] = DENIED_SHA256) -> list[str]:
    """Why this commit message is refused; empty when it is fine."""
    message = body(text)
    out = []
    if _TRAILER.search(message):
        out.append("it carries a Co-Authored-By trailer")
    out += [f"it names {word!r}" for word in denied_words(message, denied)]
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        print("usage: commitmsg.py <commit message file>", file=sys.stderr)
        return 2
    found = violations(Path(args[0]).read_text(encoding="utf-8", errors="replace"))
    for reason in found:
        print(f"commit-msg: refused, {reason} (AGENTS.md, commit rule)", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
