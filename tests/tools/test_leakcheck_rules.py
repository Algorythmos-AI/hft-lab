"""Each leak rule on its own: one case that must be caught and one that must pass. No git involved."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import lzma
import pickle
import sqlite3
import zipfile

import pytest

from tools import leakcheck as lc

PLAIN = b"An ordinary text file.\n"
NONE = lc.Rules()


def rules_of(path, data, rules=NONE):
    return [f.rule for f in lc.check(path, data, rules)]


def zip_bytes(**members) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def sqlite_bytes(tmp_path) -> bytes:
    path = tmp_path / "scratch"
    with sqlite3.connect(path) as db:
        db.execute("create table t (x integer)")
    return path.read_bytes()


# ---- extension ------------------------------------------------------------------------------------------------


def test_the_denied_extensions_are_the_agreed_list():
    assert {
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "pages", "ipynb", "parquet", "sqlite", "db", "gz", "zip",
        "png", "jpg", "jpeg", "pkl", "pt", "onnx", "bi5",
    } == lc.DENIED_EXTENSIONS  # fmt: skip


@pytest.mark.parametrize("ext", sorted(lc.DENIED_EXTENSIONS))
def test_every_denied_extension_is_refused(ext):
    assert rules_of(f"docs/report.{ext}", PLAIN) == ["extension"]


@pytest.mark.parametrize(
    "path", ["X.PDF", "a/b/Scan.Pdf", "report.pdf.", "report.pdf ", ".db", "ticks.csv.gz", "m.tar.GZ"]
)
def test_case_trailing_dots_and_double_extensions_do_not_hide_it(path):
    assert rules_of(path, PLAIN) == ["extension"]


@pytest.mark.parametrize(
    "path", ["README.md", "src/hftlab/core/money.py", "pdf", "docs/pdf/notes.md", "report.pdf.txt", "Makefile", "a.dbx"]
)
def test_ordinary_names_pass(path):
    assert rules_of(path, PLAIN) == []


def test_an_allow_listed_path_and_content_passes_the_extension_rule():
    allow = frozenset({("docs/logo.png", hashlib.sha256(PLAIN).hexdigest())})
    assert rules_of("docs/logo.png", PLAIN, lc.Rules(allow=allow)) == []


def test_the_allow_list_binds_both_the_path_and_the_content():
    allow = frozenset({("docs/logo.png", hashlib.sha256(PLAIN).hexdigest())})
    assert rules_of("docs/other.png", PLAIN, lc.Rules(allow=allow)) == ["extension"]
    assert rules_of("docs/logo.png", PLAIN + b"edited", lc.Rules(allow=allow)) == ["extension"]


# ---- size -----------------------------------------------------------------------------------------------------


def test_a_file_over_500_kb_is_refused():
    assert lc.MAX_BYTES == 500_000
    assert rules_of("notes.txt", b"a" * 500_001) == ["size"]


def test_a_file_of_exactly_500_kb_passes():
    assert rules_of("notes.txt", b"a" * 500_000) == []


def test_the_allow_list_does_not_lift_the_size_limit():
    data = b"\x89PNG\r\n\x1a\n" + b"a" * 500_000
    allow = frozenset({("docs/big.png", hashlib.sha256(data).hexdigest())})
    assert rules_of("docs/big.png", data, lc.Rules(allow=allow)) == ["size"]


# ---- magic bytes ----------------------------------------------------------------------------------------------


def samples(tmp_path) -> dict[str, bytes]:
    return {
        "pdf": b"%PDF-1.7\n1 0 obj\n",
        "zip": zip_bytes(member="text"),
        "empty zip": zip_bytes(),
        "docx": zip_bytes(**{"[Content_Types].xml": "<Types/>", "word/document.xml": "<w/>"}),
        "ole2": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(64),
        "gzip": gzip.compress(b"ticks"),
        "png": b"\x89PNG\r\n\x1a\n" + bytes(32),
        "jpeg": b"\xff\xd8\xff\xe0\x00\x10JFIF" + bytes(16),
        "parquet": b"PAR1" + bytes(32) + b"PAR1",
        "sqlite": sqlite_bytes(tmp_path),
        "bi5": lzma.compress(b"tick" * 50, format=lzma.FORMAT_ALONE),
        "pickle 2": pickle.dumps({"weights": [1, 2]}, protocol=2),
        "pickle 5": pickle.dumps({"weights": [1, 2]}, protocol=5),
        "notebook": json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}).encode(),
        "notebook with a byte-order mark": b"\xef\xbb\xbf\n" + json.dumps({"nbformat": 4, "cells": []}).encode(),
    }


def test_every_denied_format_is_recognised_by_content_under_an_innocent_name(tmp_path):
    for label, data in samples(tmp_path).items():
        assert rules_of("docs/notes.txt", data) == ["magic"], label


def test_a_renamed_file_is_caught_by_content_and_a_real_one_by_both(tmp_path):
    data = samples(tmp_path)["pdf"]
    assert rules_of("paper.txt", data) == ["magic"]
    assert rules_of("paper.pdf", data) == ["extension", "magic"]


@pytest.mark.parametrize(
    "data",
    [
        PLAIN,
        b"",
        b"The header of such a file is %PDF-1.7, and a zip starts with PK.\n",
        b'{"cells": "a table has cells", "note": "not a notebook"}\n',
        b'{"nbformat": 4}\n',
        b'The keys "nbformat" and "cells" in prose, not in a JSON object.\n',
        b"PARAMETERS = 1\n",
        b"]\n",
        "café € 1.08512\n".encode(),
    ],
)
def test_ordinary_text_is_not_mistaken_for_a_denied_format(data):
    assert lc.magic_of(data) is None
    assert rules_of("docs/notes.txt", data) == []


def test_an_allow_listed_binary_passes_the_content_rule_too(tmp_path):
    data = samples(tmp_path)["png"]
    allow = frozenset({("docs/logo.png", hashlib.sha256(data).hexdigest())})
    assert rules_of("docs/logo.png", data, lc.Rules(allow=allow)) == []
    assert rules_of("docs/logo.png", data) == ["extension", "magic"]


def test_formats_without_leading_bytes_rely_on_their_extension():
    """A known limit, pinned so it is not forgotten: onnx and text pickles have no signature to look for."""
    onnx_like = b"\x08\x07\x12\x07pytorch"
    assert lc.magic_of(onnx_like) is None and rules_of("model.onnx", onnx_like) == ["extension"]
    text_pickle = pickle.dumps({"a": 1}, protocol=0)
    assert lc.magic_of(text_pickle) is None and rules_of("model.pkl", text_pickle) == ["extension"]


# ---- terms ----------------------------------------------------------------------------------------------------

TERMS = lc.Rules(terms=((3, "project nightjar"), (7, "ridgeway")))


def test_a_private_term_in_the_text_is_refused():
    assert rules_of("docs/notes.md", b"Plan for Project Nightjar.\n", TERMS) == ["terms"]


def test_a_term_matches_across_case_spacing_line_breaks_and_width():
    wide_p = "\N{FULLWIDTH LATIN CAPITAL LETTER P}"
    for text in ["PROJECT   NIGHTJAR", "project\nnightjar", "project\t\r\n nightjar", f"{wide_p}roject nightjar"]:
        assert rules_of("docs/notes.md", text.encode(), TERMS) == ["terms"], text


def test_a_term_in_a_path_is_refused():
    assert rules_of("docs/Ridgeway/notes.md", PLAIN, TERMS) == ["terms"]


def test_text_without_the_terms_passes():
    assert rules_of("docs/notes.md", b"Projects and nightjars, a ridge and a way.\n", TERMS) == []


def test_without_a_term_list_the_rule_is_off():
    assert rules_of("docs/Ridgeway/notes.md", b"Project Nightjar\n") == []


def test_a_finding_names_the_term_by_number_and_never_prints_it():
    findings = lc.check("docs/Ridgeway/notes.md", b"line one\nProject Nightjar\n", TERMS)
    details = sorted(f.detail for f in findings)
    assert details == ["term #3 of the term list, line 2", "the path contains term #7 of the term list"]
    assert not any("nightjar" in str(f).lower().replace("docs/ridgeway", "") for f in findings)


def test_a_term_split_over_two_lines_is_reported_as_such():
    assert lc.check("a.md", b"project\nnightjar\n", TERMS)[0].detail == "term #3 of the term list, across lines"


def test_a_term_in_utf16_text_is_found():
    assert rules_of("notes.txt", "Project Nightjar".encode("utf-16"), TERMS) == ["terms"]


def test_load_terms_skips_comments_and_normalises(tmp_path):
    path = tmp_path / "terms.txt"
    path.write_text("# private\n\n  Project   Nightjar \nRidgeway\n")
    assert lc.load_terms(path) == ((3, "project nightjar"), (4, "ridgeway"))


@pytest.mark.parametrize("content", ["", "# only a comment\n\n   \n"])
def test_an_empty_term_list_is_refused_rather_than_attested(tmp_path, content):
    path = tmp_path / "terms.txt"
    path.write_text(content)
    with pytest.raises(lc.LeakcheckError):
        lc.load_terms(path)


# ---- corpus ---------------------------------------------------------------------------------------------------

PRIVATE = (
    "The spread widens sharply at the daily rollover, so a quote taken a minute before is no guide to the cost "
    "of a trade a minute after, and every holding period has to be costed on its own."
)


def corpus_rules(tmp_path, *texts) -> lc.Rules:
    sources = []
    for i, text in enumerate(texts or (PRIVATE,)):
        source = tmp_path / f"private{i}.txt"
        source.write_text(text)
        sources.append(source)
    index = tmp_path / "index.txt"
    lc.build_corpus(index, sources)
    hashes, width = lc.load_corpus(index)
    return lc.Rules(corpus=hashes, corpus_hex=width)


def test_eight_words_from_the_corpus_are_refused(tmp_path):
    copied = b"As noted: a quote taken a minute before is no guide, whatever else holds.\n"
    assert rules_of("docs/notes.md", copied, corpus_rules(tmp_path)) == ["corpus"]


def test_seven_words_from_the_corpus_pass(tmp_path):
    copied = b"As noted: a quote taken a minute before is something else entirely.\n"
    assert rules_of("docs/notes.md", copied, corpus_rules(tmp_path)) == []


def test_case_punctuation_and_line_breaks_do_not_hide_a_copied_run(tmp_path):
    copied = b"> A QUOTE, taken a\n> minute -- before -- is\n> no guide!\n"
    findings = lc.check("docs/notes.md", copied, corpus_rules(tmp_path))
    assert [f.rule for f in findings] == ["corpus"]
    assert findings[0].detail == "2 run(s) of 8 words are in the index, first at line 1"  # nine words: two runs


def test_the_finding_gives_the_line_the_copied_run_starts_on(tmp_path):
    copied = ("Our own intro line.\n" + PRIVATE.split(",")[0] + "\n").encode()  # exactly eight copied words
    detail = lc.check("docs/notes.md", copied, corpus_rules(tmp_path))[0].detail
    assert detail == "1 run(s) of 8 words are in the index, first at line 2"


def test_unrelated_text_passes_the_corpus_rule(tmp_path):
    text = b"Prices are integers at the quote precision of each pair and times are whole nanoseconds.\n"
    assert rules_of("docs/notes.md", text, corpus_rules(tmp_path)) == []


def test_without_an_index_the_rule_is_off():
    assert rules_of("docs/notes.md", PRIVATE.encode()) == []


def test_the_index_holds_one_truncated_sha256_per_run_of_eight_words(tmp_path):
    index = tmp_path / "index.txt"
    source = tmp_path / "source.txt"
    source.write_text("One two THREE four, five six seven eight nine.")
    assert lc.build_corpus(index, [source]) == 2
    expected = sorted(
        hashlib.sha256(run.encode()).hexdigest()[:16]
        for run in ("one two three four five six seven eight", "two three four five six seven eight nine")
    )
    lines = index.read_text().splitlines()
    assert lines[0].startswith("#") and lines[1:] == expected


def test_build_corpus_reads_directories_and_skips_binary_files(tmp_path):
    (tmp_path / "kb" / "deep").mkdir(parents=True)
    (tmp_path / "kb" / "deep" / "card.md").write_text(PRIVATE)
    (tmp_path / "kb" / "scan.bin").write_bytes(b"\x00\x01" * 100 + PRIVATE.encode())
    index = tmp_path / "index.txt"
    assert lc.build_corpus(index, [tmp_path / "kb"]) == len(PRIVATE.replace(",", "").replace(".", "").split()) - 7


def test_a_longer_hash_in_the_index_is_honoured(tmp_path):
    index = tmp_path / "index.txt"
    index.write_text(hashlib.sha256(b"a quote taken a minute before is no").hexdigest() + "\n")
    hashes, width = lc.load_corpus(index)
    assert width == 64
    rules = lc.Rules(corpus=hashes, corpus_hex=width)
    assert rules_of("n.md", b"a quote taken a minute before is no guide", rules) == ["corpus"]


@pytest.mark.parametrize(
    "content",
    [
        "",
        "# header only\n",
        "abcdef\n",
        "0123456789ABCDEF\n",
        "0123456789abcdef\n0123456789abcdef00\n",
        "not hex at all!!\n",
    ],
)
def test_a_bad_or_empty_index_is_refused_rather_than_attested(tmp_path, content):
    index = tmp_path / "index.txt"
    index.write_text(content)
    with pytest.raises(lc.LeakcheckError):
        lc.load_corpus(index)


def test_build_corpus_refuses_inputs_with_no_run_of_eight_words(tmp_path):
    source = tmp_path / "short.txt"
    source.write_text("only five words in here")
    with pytest.raises(lc.LeakcheckError):
        lc.build_corpus(tmp_path / "index.txt", [source])


# ---- allow-list file ------------------------------------------------------------------------------------------


def test_load_allow_reads_sha256sum_lines(tmp_path):
    digest = hashlib.sha256(PLAIN).hexdigest()
    path = tmp_path / "allow.txt"
    path.write_text(f"# the project logo\n\n{digest}  docs/logo.png\n{digest} docs/with space.png\n")
    assert lc.load_allow(path) == {("docs/logo.png", digest), ("docs/with space.png", digest)}
    assert lc.load_allow(tmp_path / "absent.txt") == frozenset()


@pytest.mark.parametrize("line", ["docs/logo.png", "abc123  docs/logo.png", "Z" * 64 + "  docs/logo.png", "a" * 64])
def test_load_allow_refuses_a_malformed_line(tmp_path, line):
    path = tmp_path / "allow.txt"
    path.write_text(line + "\n")
    with pytest.raises(lc.LeakcheckError):
        lc.load_allow(path)
