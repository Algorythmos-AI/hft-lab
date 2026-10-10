# ADR 0006: A public repository and the leak mechanism

- **Status:** accepted, 2026-10-10
- **Context:** F0 foundation

## Context

This repository is public for now. The work around it is not: there is a private knowledge base of source
material and notes, there will be market data and recordings under licence terms, trained model files, and a
broker login. Any of them committed here is published, indexed and effectively permanent, and history
keeps a file after it is deleted.

"Be careful" does not survive a tired evening. The rule has to be mechanical, run before the commit exists,
and be checked again where the developer cannot skip it.

## Decision

### 1. One tool, five rules

`tools/leakcheck.py` (standard library only) scans the staged tree, a commit, or with `--all-history` every
path and blob of every commit on every ref. It fails on:

| Rule | Fails on | Lifted by the allow-list |
|---|---|---|
| `extension` | pdf, doc, docx, xls, xlsx, ppt, pptx, pages, ipynb, parquet, sqlite, db, gz, zip, png, jpg, jpeg, pkl, pt, onnx, bi5 | yes |
| `size` | a file over 500 kB (500,000 bytes) | no |
| `magic` | the leading bytes of those formats, whatever the file is called | yes |
| `terms` | a term from a private term list | no |
| `corpus` | a run of 8 words found in a private n-gram index | no |

- The allow-list, `tools/leakcheck-allow.txt`, holds `<sha256>  <path>` lines. An entry admits that exact
  path with that exact content and needs a reason. It is empty.
- `onnx` and text-protocol pickles have no leading bytes to recognise; they are caught by extension and size
  only. A test pins that limit so it is not forgotten.

### 2. The two private inputs never enter the repository

- **Term list.** One term per line, `#` for comments. A term matches as a substring of a path or of a
  file's text after both are NFKC-normalised, lower-cased and every run of white space is made one space.
  Given with `--terms` or the environment variable `HFTLAB_LEAK_TERMS`. A finding names the term by its
  line number and never prints it.
- **Corpus index.** Text, one hash per line, `#` for comments. A hash is the first 16 to 64 hex characters
  (the same number on every line) of the SHA-256 of 8 consecutive words joined by single spaces. Words are
  the runs of letters and digits in the NFKC-normalised, lower-cased text, so punctuation, case and line
  breaks do not matter. Given with `--corpus` or `HFTLAB_LEAK_CORPUS`.
  `tools/leakcheck.py --build-corpus OUT FILE...` writes such an index and is the reference for the format;
  it refuses to write inside the repository.
- An empty term list or index is an error, not a pass: it would attest a check of nothing.
- CI has neither file. It runs the first three rules.

### 3. An attestation ties the local check to the exact tree

The term and corpus rules can only run on the owner's machine, so that run leaves a record that CI can
check: `.leakcheck-attest.json`, committed with the tree it describes.

```json
{
  "corpus_checked": true,
  "files": 107,
  "result": "pass",
  "schema": 1,
  "terms_checked": true,
  "tree": "<40 hex>"
}
```

- **The attested hash excludes the attestation file itself.** A file cannot contain the hash of a tree that
  contains it. So `tree` is the hash of the tree with `.leakcheck-attest.json` removed, computed in a
  throwaway index (`git read-tree`, `git update-index --force-remove`, `git write-tree`). The hook and CI
  compute the same value, and committing the attestation does not change it.
- **Written in `pre-commit`.** The hook scans the staged tree, writes the attestation on a pass and stages
  it, so it is part of the commit it describes. A failed scan blocks the commit and attests nothing.
- **Verified in `pre-push`.** That hook only verifies: each commit about to be pushed must carry a current
  attestation. It never rewrites.
- **Verified again in CI.** The `leakcheck` job scans the tree and all history with the public rules,
  recomputes the excluded-tree hash and fails when the attestation is missing, malformed or stale.
- **`terms_checked` and `corpus_checked` say which private rules ran.** `corpus_checked` is true only when an
  environment variable or flag pointed at a local index. CI prints both flags and does not fail on them: a
  commit made without the private files is visible, not blocked.
- **The file must be byte-for-byte canonical** (sorted keys, two-space indent, one final newline, exactly
  these six keys). It is the one file left out of the attested tree, so it is not allowed to carry anything
  else.

### 4. Commit messages

`.githooks/commit-msg` refuses a `Co-Authored-By` trailer and any assistant or tool name (org decision
D-016). The refused names are stored as SHA-256 digests, so the tool does not spell them and passes its own
rule. A test applies the same list to every tracked file.

`make hooks` points `core.hooksPath` at `.githooks`.

## Consequences

- **The attestation is not a signature.** Anyone can compute the hash and write the file by hand. It shows
  that the tool ran on this tree on a machine that may hold the private files; it stops accidents, not a
  determined author. CI's own scan of the public rules does not depend on it.
- **The tree that lands must be the tree that was attested.** For a pull request, CI checks out the head of
  the branch, not GitHub's merge commit. A squash merge produces the same tree only when the branch is up to
  date with `main`, so the branch rule should require that; otherwise the push run on `main` reports a stale
  attestation.
- **Commits made outside the hook are stale** until re-attested: a rebase, a merge, a cherry-pick, a commit
  made on GitHub, a Dependabot pull request. `make attest` and a commit fix it. `git commit <path>` commits
  from a temporary index and can leave the attestation out; use `git add` then `git commit`.
- **Not covered:** archive and image formats outside the list (xz, bz2, 7z, zstd, tar, gif, webp), commit
  messages as a place for leaked text, and paraphrase, which no n-gram check can see. Review still matters.
- A leak that reaches a public remote is treated as published: rotate first, clean history second.
