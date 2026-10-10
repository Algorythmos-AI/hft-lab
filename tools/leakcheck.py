#!/usr/bin/env python3
"""Leak check: what must never reach a public repository (ADR 0006).

It scans the staged tree (a commit with --rev; every blob of every commit with --all-history) and fails on:

  extension  a denied file type, unless that exact path and content is listed in tools/leakcheck-allow.txt
  size       a file over 500 kB (the allow-list does not lift this)
  magic      the leading bytes of a denied format, whatever the file is called (same allow-list)
  terms      a term from a private term list: --terms, or the file $HFTLAB_LEAK_TERMS names
  corpus     a run of 8 words found in a private n-gram index: --corpus, or the file $HFTLAB_LEAK_CORPUS names

The term list and the index are never in the repository, so CI runs the first three rules only. The attestation
(.leakcheck-attest.json) says which rules ran on the owner's machine, for which tree:

  --write-attest   scan the staged tree and, on a pass, write the attestation (the pre-commit hook stages it)
  --verify-attest  check that the attestation in a commit (default HEAD) is present, well formed and current

The attested hash is the commit's tree with the attestation file itself removed. A file cannot contain the
hash of a tree that contains it, and this way the pre-commit hook and CI compute the same value.

Term list: one term per line, `#` starts a comment. A term matches as a substring of a path or of a file's
text, after both are NFKC-normalised, lower-cased and every run of white space is made one space.

Corpus index: text, one hash per line, `#` starts a comment. A hash is the first 16 to 64 hex characters (the
same number on every line) of the SHA-256 of 8 consecutive words joined by single spaces. Words are the runs of
letters and digits in the NFKC-normalised, lower-cased text, so punctuation, case and line breaks do not matter.
`--build-corpus OUT FILE...` writes such an index and is the reference for the format.

Exit codes: 0 pass, 1 a finding or a failed attestation, 2 the check could not run. Standard library only and
Python 3.9+, so the hooks work before `make bootstrap`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

ATTEST_PATH = ".leakcheck-attest.json"
ALLOW_PATH = "tools/leakcheck-allow.txt"
TERMS_ENV = "HFTLAB_LEAK_TERMS"
CORPUS_ENV = "HFTLAB_LEAK_CORPUS"
MAX_BYTES = 500_000
NGRAM = 8
INDEX_HEX = 16
SCHEMA = 1

DENIED_EXTENSIONS = frozenset(
    {
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "pages", "ipynb", "parquet", "sqlite", "db", "gz", "zip",
        "png", "jpg", "jpeg", "pkl", "pt", "onnx", "bi5",
    }
)  # fmt: skip
# Leading bytes of the denied formats. onnx is a bare protobuf with no signature: only its extension and its size
# can catch it. docx, xlsx, pptx, pages and most pt files are zip containers; doc, xls and ppt are OLE2.
MAGIC: tuple[tuple[str, bytes], ...] = (
    ("pdf", b"%PDF-"),
    ("zip container (zip, docx, xlsx, pptx, pages, pt)", b"PK\x03\x04"),
    ("zip container, empty", b"PK\x05\x06"),
    ("zip container, spanned", b"PK\x07\x08"),
    ("OLE2 container (doc, xls, ppt)", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    ("gzip", b"\x1f\x8b\x08"),
    ("png", b"\x89PNG\r\n\x1a\n"),
    ("jpeg", b"\xff\xd8\xff"),
    ("parquet", b"PAR1"),
    ("sqlite", b"SQLite format 3\x00"),
    ("LZMA stream (bi5)", b"\x5d\x00\x00"),
)
_PICKLE_PROTOCOLS = (b"\x02", b"\x03", b"\x04", b"\x05")
_ATTEST_KEYS = frozenset({"schema", "tree", "result", "files", "terms_checked", "corpus_checked"})
_HEX = re.compile(r"[0-9a-f]+")
_WORD = re.compile(r"[^\W_]+")


class LeakcheckError(Exception):
    """The check could not run (bad input, git failed). Exit code 2: never reported as a pass."""


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    detail: str
    blob: str = ""

    def __str__(self) -> str:
        where = f"{self.path} (blob {self.blob[:12]})" if self.blob else self.path
        return f"{self.rule}: {where}: {self.detail}"


@dataclass(frozen=True)
class Rules:
    """What to check. `terms` and `corpus` are None when their private file was not given."""

    allow: frozenset[tuple[str, str]] = frozenset()
    terms: tuple[tuple[int, str], ...] | None = None
    corpus: frozenset[str] | None = None
    corpus_hex: int = INDEX_HEX
    max_bytes: int = MAX_BYTES


@dataclass(frozen=True)
class BlobFacts:
    """Everything about a blob that does not depend on its path, so each blob is read and hashed once."""

    sha256: str
    size: int
    magic: str | None
    content: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Entry:
    path: str
    blob: str


# ---- text -----------------------------------------------------------------------------------------------------


def decode(data: bytes) -> str:
    """Bytes as text for the term and corpus rules. Never fails: an undecodable byte becomes U+FFFD."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8", errors="replace")


def normalise(text: str) -> str:
    """NFKC, lower case, every run of white space one space: how terms and scanned text are compared."""
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def grams(text: str) -> Iterator[tuple[int, str]]:
    """(line number, SHA-256 hex) of every run of NGRAM words in `text`."""
    flat = unicodedata.normalize("NFKC", text).lower()
    words = [(m.start(), m.group()) for m in _WORD.finditer(flat)]
    line, seen = 1, 0
    for i in range(len(words) - NGRAM + 1):
        start = words[i][0]
        line += flat.count("\n", seen, start)
        seen = start
        run = " ".join(word for _, word in words[i : i + NGRAM])
        yield line, hashlib.sha256(run.encode("utf-8")).hexdigest()


def term_hits(text: str, terms: Sequence[tuple[int, str]]) -> list[tuple[int, int]]:
    """(the term's line in the term list, the line it was found on, 0 if it spans lines) for each term found."""
    flat = normalise(text)
    lines: list[str] | None = None
    hits = []
    for number, term in terms:
        if term not in flat:
            continue
        if lines is None:
            lines = [normalise(line) for line in text.splitlines()]
        hits.append((number, next((i for i, line in enumerate(lines, 1) if term in line), 0)))
    return hits


def magic_of(data: bytes) -> str | None:
    """The denied format `data` starts like, or None."""
    for label, prefix in MAGIC:
        if data.startswith(prefix):
            return label
    if data[:1] == b"\x80" and data[1:2] in _PICKLE_PROTOCOLS:
        return "pickle (pkl, pt)"
    # A notebook is JSON, so it has no leading bytes of its own; its two mandatory keys are the signature.
    if data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")[:1] == b"{" and b'"nbformat"' in data and b'"cells"' in data:
        return "notebook (ipynb)"
    return None


def extension(path: str) -> str:
    """The lower-cased last extension of the file name; trailing dots and spaces do not hide it."""
    name = path.rsplit("/", 1)[-1].rstrip(". ")
    return name.rpartition(".")[2].lower() if "." in name else ""


# ---- private inputs --------------------------------------------------------------------------------------------


def load_allow(path: Path) -> frozenset[tuple[str, str]]:
    """(path, sha256) pairs from `<sha256>  <path>` lines; a missing file is an empty list."""
    if not path.exists():
        return frozenset()
    out = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition(" ")
        if len(digest) != 64 or not _HEX.fullmatch(digest) or not name.strip():
            raise LeakcheckError(f"{path}:{number}: expected `<sha256>  <path>`")
        out.add((name.strip(), digest))
    return frozenset(out)


def load_terms(path: Path) -> tuple[tuple[int, str], ...]:
    """(line number, normalised term) for each term. An empty list is refused: it would attest a check of nothing."""
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise LeakcheckError(f"cannot read the term list {path}: {exc}") from exc
    terms = tuple(
        (number, normalise(raw))
        for number, raw in enumerate(raw_lines, 1)
        if normalise(raw) and not raw.lstrip().startswith("#")
    )
    if not terms:
        raise LeakcheckError(f"the term list {path} has no terms")
    return terms


def load_corpus(path: Path) -> tuple[frozenset[str], int]:
    """(hashes, hex characters per hash) from an index file. An empty or ragged index is refused."""
    try:
        raw_lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise LeakcheckError(f"cannot read the corpus index {path}: {exc}") from exc
    hashes: set[str] = set()
    width = 0
    for number, raw in enumerate(raw_lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _HEX.fullmatch(line) or not INDEX_HEX <= len(line) <= 64:
            raise LeakcheckError(f"{path}:{number}: not a lower-case hex hash of {INDEX_HEX} to 64 characters")
        if width and len(line) != width:
            raise LeakcheckError(f"{path}:{number}: hash length {len(line)} differs from the {width} above")
        width = len(line)
        hashes.add(line)
    if not hashes:
        raise LeakcheckError(f"the corpus index {path} has no hashes")
    return frozenset(hashes), width


def build_corpus(out: Path, inputs: Sequence[Path]) -> int:
    """Write the n-gram index of `inputs` (files, or directories read recursively) to `out`; returns its size."""
    files: list[Path] = []
    for item in inputs:
        files += sorted(p for p in item.rglob("*") if p.is_file()) if item.is_dir() else [item]
    hashes: set[str] = set()
    for file in files:
        try:
            data = file.read_bytes()
        except OSError as exc:
            raise LeakcheckError(f"cannot read {file}: {exc}") from exc
        if b"\x00" in data[:8192] and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
            continue  # a binary file has no words worth indexing
        hashes.update(digest[:INDEX_HEX] for _, digest in grams(decode(data)))
    if not hashes:
        raise LeakcheckError(f"no run of {NGRAM} words in the inputs: nothing to index")
    header = f"# hftlab leak corpus v1: sha256 of {NGRAM} lower-cased words joined by spaces, first {INDEX_HEX} hex\n"
    out.write_text(header + "".join(f"{digest}\n" for digest in sorted(hashes)), encoding="ascii")
    return len(hashes)


# ---- rules ----------------------------------------------------------------------------------------------------


def inspect(data: bytes, rules: Rules) -> BlobFacts:
    content: list[tuple[str, str]] = []
    if rules.terms is not None or rules.corpus is not None:
        text = decode(data)
        if rules.terms is not None:
            for number, line in term_hits(text, rules.terms):
                where = f"line {line}" if line else "across lines"
                content.append(("terms", f"term #{number} of the term list, {where}"))
        if rules.corpus is not None:
            lines = [line for line, digest in grams(text) if digest[: rules.corpus_hex] in rules.corpus]
            if lines:
                detail = f"{len(lines)} run(s) of {NGRAM} words are in the index, first at line {lines[0]}"
                content.append(("corpus", detail))
    return BlobFacts(hashlib.sha256(data).hexdigest(), len(data), magic_of(data), tuple(content))


def judge(path: str, facts: BlobFacts, rules: Rules, blob: str = "") -> list[Finding]:
    """The findings for one path holding one blob."""
    out = []
    allowed = (path, facts.sha256) in rules.allow
    ext = extension(path)
    if ext in DENIED_EXTENSIONS and not allowed:
        out.append(Finding("extension", path, f".{ext} files are denied", blob))
    if facts.size > rules.max_bytes:
        out.append(Finding("size", path, f"{facts.size} bytes, over the {rules.max_bytes} byte limit", blob))
    if facts.magic and not allowed:
        out.append(Finding("magic", path, f"content is {facts.magic}", blob))
    if rules.terms is not None:
        flat = normalise(path)
        hits = [number for number, term in rules.terms if term in flat]
        out += [Finding("terms", path, f"the path contains term #{number} of the term list", blob) for number in hits]
    out += [Finding(rule, path, detail, blob) for rule, detail in facts.content]
    return out


def check(path: str, data: bytes, rules: Rules) -> list[Finding]:
    """The findings for one file: `judge` over `inspect`."""
    return judge(path, inspect(data, rules), rules)


# ---- git ------------------------------------------------------------------------------------------------------


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, *args: str, env: dict[str, str] | None = None) -> bytes:
        try:
            proc = subprocess.run(["git", *args], cwd=self.root, env=env, capture_output=True, check=False)
        except OSError as exc:
            raise LeakcheckError(f"cannot run git: {exc}") from exc
        if proc.returncode:
            raise LeakcheckError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout

    def commit(self, rev: str) -> str:
        """The commit id `rev` names; an unknown revision is an error, not an empty scan."""
        return self.git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").decode().strip()

    def staged(self) -> list[Entry]:
        out = []
        for record in self.git("ls-files", "-s", "-z").split(b"\x00"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, blob, stage = meta.decode().split()
            if stage != "0":
                raise LeakcheckError(f"{path.decode(errors='replace')} is unmerged: resolve the merge first")
            if mode != "160000":  # a submodule link has no blob here
                out.append(Entry(path.decode("utf-8", errors="replace"), blob))
        return out

    def tree(self, treeish: str) -> list[Entry]:
        out = []
        for record in self.git("ls-tree", "-r", "-z", "--full-tree", treeish).split(b"\x00"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            _, kind, blob = meta.decode().split()
            if kind == "blob":
                out.append(Entry(path.decode("utf-8", errors="replace"), blob))
        return out

    def history(self) -> list[Entry]:
        """Every (path, blob) in every commit reachable from any ref."""
        trees = sorted(set(self.git("log", "--all", "--format=%T").decode().split()))
        pairs = {(e.path, e.blob) for tree in trees for e in self.tree(tree)}
        return [Entry(path, blob) for path, blob in sorted(pairs)]

    def blobs(self, ids: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        """(id, content) for each blob, through one `git cat-file --batch` process."""
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            for blob in ids:
                proc.stdin.write(blob.encode() + b"\n")
                proc.stdin.flush()
                header = proc.stdout.readline().split()
                if len(header) != 3 or header[1] != b"blob":
                    raise LeakcheckError(f"git cat-file: {blob} is not a readable blob")
                data = proc.stdout.read(int(header[2]))
                proc.stdout.read(1)
                yield blob, data
        finally:
            proc.stdin.close()
            proc.stdout.close()
            proc.wait()

    def attested_tree(self, treeish: str) -> str:
        """The hash of `treeish` with the attestation file removed, built in a throwaway index."""
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self.git("read-tree", treeish, env=env)
            self.git("update-index", "--force-remove", "--", ATTEST_PATH, env=env)
            return self.git("write-tree", env=env).decode().strip()

    def attestation(self, rev: str) -> bytes | None:
        """The attestation file's bytes in `rev`, or None when it is not there as a regular file."""
        listing = self.git("ls-tree", "-z", "--full-tree", self.commit(rev), "--", ATTEST_PATH).split(b"\x00")[0]
        if not listing:
            return None
        mode, kind, blob = listing.partition(b"\t")[0].decode().split()
        if kind != "blob" or mode not in ("100644", "100755"):
            return None
        return self.git("cat-file", "blob", blob)


def scan(repo: Repo, entries: Iterable[Entry], rules: Rules, *, history: bool = False) -> list[Finding]:
    paths: dict[str, set[str]] = {}
    for entry in entries:
        paths.setdefault(entry.blob, set()).add(entry.path)
    findings: list[Finding] = []
    for blob, data in repo.blobs(sorted(paths)):
        facts = inspect(data, rules)
        for path in sorted(paths[blob]):
            findings += judge(path, facts, rules, blob if history else "")
    return sorted(findings, key=lambda f: (f.path, f.rule, f.detail, f.blob))


# ---- attestation ----------------------------------------------------------------------------------------------


def render_attestation(tree: str, files: int, rules: Rules) -> bytes:
    """The attestation, in the one form `verify` accepts: sorted keys, two-space indent, a final newline."""
    doc = {
        "schema": SCHEMA,
        "tree": tree,
        "result": "pass",
        "files": files,
        "terms_checked": rules.terms is not None,
        "corpus_checked": rules.corpus is not None,
    }
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("ascii")


def verify(repo: Repo, rev: str) -> tuple[bool, str]:
    """(ok, message): is the attestation in `rev` present, well formed and for exactly that commit's tree?"""
    raw = repo.attestation(rev)
    if raw is None:
        return False, f"{ATTEST_PATH} is missing from {rev}: run `make hooks`, then `make attest` and commit"
    try:
        doc = json.loads(raw.decode("ascii"))
    except ValueError:
        return False, f"{ATTEST_PATH} in {rev} is not valid JSON"
    well_formed = (
        isinstance(doc, dict)
        and set(doc) == _ATTEST_KEYS
        and doc["schema"] == SCHEMA
        and doc["result"] == "pass"
        and isinstance(doc["tree"], str)
        and type(doc["files"]) is int
        and type(doc["terms_checked"]) is bool
        and type(doc["corpus_checked"]) is bool
    )
    # Byte-for-byte canonical, so the one file left out of the attested tree cannot carry anything else.
    if not well_formed or (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("ascii") != raw:
        return False, f"{ATTEST_PATH} in {rev} is malformed: regenerate it with `make attest`"
    tree = repo.attested_tree(f"{repo.commit(rev)}^{{tree}}")
    if doc["tree"] != tree:
        return False, (
            f"{ATTEST_PATH} in {rev} is stale: it attests tree {doc['tree'][:12]}, the commit's tree without it "
            f"is {tree[:12]}. Run `make attest` and commit."
        )
    flags = ", ".join(f"{key}={json.dumps(doc[key])}" for key in ("files", "terms_checked", "corpus_checked"))
    return True, f"attestation is current for tree {tree[:12]} ({flags})"


# ---- command line ---------------------------------------------------------------------------------------------


def _private_file(flag: str | None, env_name: str, what: str) -> Path | None:
    value = flag if flag is not None else os.environ.get(env_name) or None
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_file():
        source = "the command line" if flag is not None else f"${env_name}"
        raise LeakcheckError(f"the {what} {path} does not exist (from {source})")
    return path


def _inside(path: Path, repo: Repo) -> bool:
    try:
        top = Path(repo.git("rev-parse", "--show-toplevel").decode().strip()).resolve()
    except LeakcheckError:
        return False
    return path.resolve().is_relative_to(top)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leakcheck", description="Fail on what must not reach a public repository.")
    p.add_argument("--repo", default=".", help="the repository to check (default: the current directory)")
    p.add_argument("--rev", help="scan or verify this commit instead of the staged tree (verify default: HEAD)")
    p.add_argument("--all-history", action="store_true", help="also scan every blob of every commit on every ref")
    p.add_argument("--terms", help=f"private term list (default: ${TERMS_ENV})")
    p.add_argument("--corpus", help=f"private n-gram index (default: ${CORPUS_ENV})")
    p.add_argument("--allow", help=f"allow-list of `<sha256>  <path>` lines (default: {ALLOW_PATH})")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--write-attest", action="store_true", help="on a pass, attest the staged tree")
    mode.add_argument("--verify-attest", action="store_true", help="check the attestation in a commit; no scan")
    mode.add_argument("--build-corpus", metavar="OUT", help="write the n-gram index of the given files to OUT")
    p.add_argument("inputs", nargs="*", help="files or directories for --build-corpus")
    return p


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = Repo(Path(args.repo))

    if args.build_corpus:
        out = Path(args.build_corpus)
        if not args.inputs:
            raise LeakcheckError("--build-corpus needs at least one input file or directory")
        if _inside(out, repo):
            raise LeakcheckError("the corpus index must be written outside the repository")
        print(f"leakcheck: wrote {build_corpus(out, [Path(i) for i in args.inputs])} hashes to {out}")
        return 0
    if args.inputs:
        raise LeakcheckError("file arguments are only for --build-corpus")

    if args.verify_attest:
        ok, message = verify(repo, args.rev or "HEAD")
        print(f"leakcheck: {message}", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if args.write_attest and (args.rev or args.all_history):
        raise LeakcheckError("--write-attest attests the staged tree: it takes neither --rev nor --all-history")
    terms_file = _private_file(args.terms, TERMS_ENV, "term list")
    corpus_file = _private_file(args.corpus, CORPUS_ENV, "corpus index")
    corpus, width = load_corpus(corpus_file) if corpus_file else (None, INDEX_HEX)
    rules = Rules(
        allow=load_allow(Path(args.allow) if args.allow else repo.root / ALLOW_PATH),
        terms=load_terms(terms_file) if terms_file else None,
        corpus=corpus,
        corpus_hex=width,
    )

    entries = repo.tree(repo.commit(args.rev)) if args.rev else repo.staged()
    findings = scan(repo, entries, rules)
    past: list[Entry] = []
    if args.all_history:
        current = {(e.path, e.blob) for e in entries}
        past = [e for e in repo.history() if (e.path, e.blob) not in current]
        findings += scan(repo, past, rules, history=True)

    for finding in findings:
        print(f"leakcheck: {finding}", file=sys.stderr)
    if findings:
        print(f"leakcheck: FAILED, {len(findings)} finding(s). Nothing was attested.", file=sys.stderr)
        return 1

    files = sum(1 for e in entries if e.path != ATTEST_PATH)
    on = {True: "on", False: "off"}
    summary = f"{files} files, terms {on[rules.terms is not None]}, corpus {on[rules.corpus is not None]}"
    if args.all_history:
        summary += f", {len(past)} more path/blob pairs in history"
    print(f"leakcheck: ok ({summary})")
    if args.write_attest:
        tree = repo.attested_tree(repo.git("write-tree").decode().strip())
        (repo.root / ATTEST_PATH).write_bytes(render_attestation(tree, files, rules))
        print(f"leakcheck: attested tree {tree[:12]} in {ATTEST_PATH}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(argv)
    except LeakcheckError as exc:
        print(f"leakcheck: cannot run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
