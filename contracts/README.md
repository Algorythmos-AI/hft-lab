# Contracts

What this platform publishes about itself, as versioned data contracts. A consumer never imports this
repository's code: it keeps a copy of the contract file, pinned by the file's SHA-256 and the commit it was
taken from, and checks what it receives against that copy.

| File | Schema id | What it is |
|---|---|---|
| `hft-snapshot.v1.schema.json` | `hft-lab/snapshot`, version 1 | The status snapshot: stage, build phases, health, and, once they exist, the recorder, pairs, sleeves, books and learning loop |
| `examples/hft-snapshot.v1.json` | | A snapshot that conforms, for consumers' tests. Its numbers are made up |

## Rules of the snapshot contract

1. **No free text.** Every string is one of a fixed list or a short id, time or code matching an anchored
   pattern. Reasons are codes. A consumer can show a snapshot without reading it for anything private.
2. **Closed.** Every object refuses fields the contract does not name, and every list is bounded.
3. **Paper only.** `desk.mode` has one allowed value, `paper`. There is no way to describe a live desk.
4. **Numbers and states only.** Account ids, host names, order ids, quotes and log lines are not in it and
   may not be added.
5. **Optional parts are absent or null until they are real.** Before the recorder runs, a snapshot holds
   identity, windows, stage, phases and health, and nothing else. A missing part is never sent as zeros.
6. **`expected_windows` lists the trading week that is open or the next one to open**, in UTC, derived
   from New York time (the currency week runs from Sunday evening to Friday 17:00 there). A consumer treats silence inside
   a window as a fault and silence outside one as normal.

`tests/unit/test_snapshot_contract.py` holds the file to rules 1 to 3 and checks the example. The publisher
checks every snapshot it builds with the same code, before the snapshot is printed or sent.

## Changing a contract

- A change that a consumer's pinned copy would refuse (a new field, a new allowed value) ships in two
  steps, in this order: the consumer takes the new file and hash, then the publisher starts sending it.
- A change of meaning, a removed field or a narrowed value is a new version: a new file
  (`hft-snapshot.v2.schema.json`), with `schema_version` 2. The old file stays until no publisher sends it.
- A published contract file is never edited in a way that keeps its name and changes what conforms
  without the two steps above.

The publisher is `python -m hftlab.publish` ([ADR 0018](../docs/adr/0018-snapshot-publish.md)). It prints
the snapshot unless it is told to send it. **Nothing has been sent yet**: the signing key, and the key id the
site accepts for this desk, are the owner's decisions and are still open
([runbook](../docs/runbooks/publish.md)). This round it sends identity, windows, stage, phases and health,
and the recorder part once something has been recorded; pairs, sleeves, books and learning stay absent.
