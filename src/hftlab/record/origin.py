"""Where a day folder's quotes came from: this platform's recorder, or files somebody else's tool wrote (ADR 0028).

The two are kept in the same format and are not the same evidence. A recorded day is what this platform's
venue showed this platform's process. An imported day is what another venue showed somebody else, exported by
the owner and read from disk. So every reader that counts, fits or decides asks here first, and the answer is
one of two:

  recorded          nothing in the folder says otherwise
  imported:<label>  the journal opens with a provenance line, which carries the owner's label for the source;
                    or a segment's header carries run number 0, which no recorder writes (its runs count from 1)

The second test needs no journal: an imported segment says what it is under its own checksum, so a journal
that was lost or edited does not turn imported quotes into recorded ones. Such a day is `imported:unlabelled`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from hftlab.record.segment import header_of
from hftlab.record.store import PROVENANCE, journal_head, segments

RECORDED: Final = "recorded"
IMPORTED: Final = "imported"
UNLABELLED: Final = "unlabelled"
IMPORT_RUN: Final = 0  # the run number of every imported segment
STATED: Final = "stated"  # a provenance line's `sizes`: the files held sizes, in a unit the owner stated
PLACEHOLDER: Final = "placeholder"  # the files held none, and every size in the day is the placeholder
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def source_label(value: object) -> str:
    """The owner's name for where a set of files came from: letters, digits, dot, dash and underscore. No space."""
    if not isinstance(value, str) or not _LABEL.fullmatch(value) or value == UNLABELLED:
        raise ValueError(
            f"a source label is 1 to 64 letters, digits, dots, dashes or underscores, starting with a letter or a "
            f"digit, and is not {UNLABELLED!r}: not {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class Origin:
    source: str  # RECORDED or IMPORTED
    label: str = ""  # the owner's label of an imported day
    placeholder_sizes: bool = False  # its sizes are a stated placeholder, not anything a venue displayed

    @property
    def imported(self) -> bool:
        return self.source == IMPORTED

    @property
    def key(self) -> str:
        """What a count by source is kept under: `recorded`, or `imported:` and the label."""
        return f"{IMPORTED}:{self.label or UNLABELLED}" if self.imported else RECORDED


OWN: Final = Origin(RECORDED)


def origin_of(day: Path) -> Origin:
    """What a day folder is. A provenance line or an imported segment makes it imported; nothing makes it recorded.

    A segment whose header cannot be read raises `CorruptSegment`: a damaged day is not given an origin.
    """
    head = journal_head(day)
    if head.get("type") == PROVENANCE:
        label = head.get("label")
        named = label if isinstance(label, str) and _LABEL.fullmatch(label) else ""
        return Origin(IMPORTED, named, head.get("sizes") != STATED)
    if any(header_of(path).run == IMPORT_RUN for path in segments(day)):
        return Origin(IMPORTED, "", True)  # no journal to say its sizes were stated: they are taken as not
    return OWN
