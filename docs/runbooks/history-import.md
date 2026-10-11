# Runbook: importing history from files

For the owner ([ADR 0028](../adr/0028-imported-history.md)). The platform's own recordings come first. Before
enough of them exist, tick files you exported yourself can be read into a second store and used to fit a
model. **They are never this venue's results.**

**Nothing on this page has been followed with a real file.** The import has read hand-written rows in tests
only. Correct this page in the pull request that follows its first real use.

Three things only you can do, and the platform does none of them:

- **Export the files yourself**, with the data vendor's own export tool, under the terms you accept there.
  The platform downloads nothing and this repository does not say where history is to be had.
- **Read those terms.** Nobody else has. Every imported day and every report says `terms_verified: false`,
  and there is no switch for it (section 6).
- **State what the files are.** Nothing about a file is worked out from its contents or its name.

Every command below is run from the checkout with `PYTHONPATH=src` and the installed interpreter, written
`history ...` for `.venv/bin/python -m hftlab.history ...`.

## 1. Put the files in a folder of their own

One folder for one kind of export: the same layout, the same time zone, the same size unit. A second kind
goes in a second folder. The folder holds the files and nothing else but the description of section 2. It is
not inside the checkout's tracked paths: tick files are never committed.

## 2. Write `import.yaml` beside them

```yaml
version: 1
layout: generic              # generic | utc_ask_bid | local_bid_ask
time_zone: UTC               # UTC, an offset like -05:00, or a zone name like America/New_York
header: true                 # is the first line of every file a header line?
delimiter: ","
time_format: "%Y-%m-%d %H:%M:%S.%f"
columns: {time: Time, bid: Bid, ask: Ask, bid_size: BidSize, ask_size: AskSize}
size_unit: "1000000"         # base-currency units per 1 of a size column
instruments:
  - {pair: EUR.USD, kind: CASH, price_scale: 5, min_order_size: 20000, size_increment: 1000, price_increment: 5}
files:
  - {name: ticks-a.csv, pair: EUR.USD}
```

| Key | What to write |
|---|---|
| `layout` | `generic` for any delimited file: you state the rest. `utc_ask_bid` for five comma-separated columns in this order: time in UTC as `14.10.2026 03:00:00.000`, ask, bid, ask size, bid size. `local_bid_ask` for three or four in this order: wall-clock time as `20261013 230000000`, bid, ask, and a fourth column that is not read |
| `time_zone` | The zone the file's times are in. **There is no default.** For `utc_ask_bid` it must say `UTC`. For a wall clock that never changes for summer, write the offset (`-05:00`), not a zone name |
| `header` | `true` or `false`. A header line is skipped; it is never used to guess anything |
| `delimiter`, `time_format`, `columns` | Generic layout only. `columns` gives header names when `header` is true and positions counted from 1 when it is false. The format is made of `%Y %m %d %H %M %S %f` and plain text, or is one of `epoch_s`, `epoch_ms`, `epoch_us`, `epoch_ns` |
| `size_unit` | Only when the layout has sizes, and then always: how many base-currency units a `1` in a size column stands for, in quotes. Leave the two size columns out of `columns` if the file has none |
| `instruments` | Each pair, **exactly as a recorded day describes it**: copy the entry from the `instruments` line of a recorded day's `journal.jsonl`. A dataset refuses days that describe one pair in two ways |
| `files` | Every file in the folder, with the pair it holds. A file that is there and not listed refuses the import |

## 3. Look before you write

```
history import --from <folder> --label <label> --dry-run
```

The label is your name for the source, for example `vendor-a`: letters, digits, dots, dashes and underscores,
no space. It is written into every imported day and into every count made from one, so choose it once.

The dry run reads everything, writes nothing and prints the report of section 5. Read the `dropped` counts
before going on: thousands of `market_closed` rows mean the time zone you stated is wrong.

## 4. Import

```
history import --from <folder> --label <label>
```

| You should see | It means |
|---|---|
| Exit code 0, a JSON report, and `source: imported, terms_verified: false` on the last line | Done. The days are under `var/history/<YYYY-MM-DD>/` |
| Exit code 2 and `refused, nothing written: ...` | Nothing was written. Section 7 says what each refusal means |

Check one day the way a recorded day is checked. `"ok": true` is what you want; the `unexplained` gaps are the
other feed's silences, and nobody knows their reason:

```
.venv/bin/python -m hftlab.replay --day var/history/<YYYY-MM-DD> --check
```

Each kept row is 64 bytes on the disk. The import store has no quota of its own: look at the free space
first (`df -h var`).

Importing the same folder again changes nothing: every day says `unchanged`.

## 5. Reading the report

| Field | What it tells you |
|---|---|
| `files[].sha256` | The file's fingerprint. It is in every day the file gave a row to, so a day can be traced to its bytes |
| `files[].rows_read`, `rows_kept` | Rows in the file, and rows that became quotes |
| `files[].dropped` | Rows left out, by reason (below). A dropped row is never repaired |
| `files[].counted` | Rows kept and noted: `locked` (bid equals ask), `off_increment` (a price off this venue's order grid) |
| `days[].status` | `written`, `unchanged` (already there, byte for byte), `nothing_kept` (every row of that day was dropped; no folder), and on a dry run `would_write` or `exists` |
| `days[].gaps` | Silences longer than five seconds while the market was open, by reason |
| `days[].sizes` | `stated`, or `placeholder`: the files had no sizes (section 6) |

| Dropped for | It means |
|---|---|
| `market_closed` | The market was shut at that time. A few at the week's edges are normal. Many: the time zone is wrong |
| `time_not_one_instant` | A wall-clock time in the hour a clock change repeats or skips. State a fixed offset if the export never changes its clock |
| `time_goes_back` | Earlier than a row before it in the same file |
| `price_unfit` | More decimals than the pair's price scale, or not above zero. Many: `price_scale` is wrong for this file |
| `crossed` | The bid is above the ask |
| `size_unfit` | The size times `size_unit` is not a whole number of units. Many: `size_unit` is wrong |
| `duplicate` | The same quote as the row before it, at the same time |

## 6. What an imported day may be used for

- **Looking, for now.** The replay, the day check and the feasibility map read an imported day as they read a
  recorded one. Training on imported days, and the split by source in every report, come with the change that
  follows this one; until it is merged, do not point a training run at this store.
- **Not a result.** Results on another feed's quotes are not this venue's results.
- **A file without sizes shows prices only.** Each of its quotes carries a size of one unit, which no order
  fills against. If the export tool can write sizes, export them.
- **`terms_verified: false` stays.** It changes only by a design record that names the label, says who read
  which terms on which date and what they allow.

## 7. What each refusal means

| It says | Do this |
|---|---|
| `cannot read .../import.yaml`, `missing [...]`, `not understood [...]` | The description is absent or leaves something unstated. Section 2 |
| `no time zone is stated`, `` `time_zone`: ... `` | State the zone. It is never guessed |
| `` `size_unit` is stated when the layout has size columns and only then `` | Add it, or take it out |
| `the folder and files differ` | List every file, or move the stray one out of the folder |
| `<file>: line N: ...` | That line is not written as described: the wrong number of fields, a time that does not match the format, a value that is not a plain decimal number. Fix the description, or the file's export. The whole run is refused so that no part of a misdescribed file is kept |
| `is where the recorder writes`, `it is a recorder's store`, `holds recorded days` | You pointed `--store` at the recording. Imported history has its own store |
| `is not empty and is not an import store` | Choose a folder of its own for `--store` |
| `already imported from other files, under another label or described differently: <days>` | Those days are in the store from other inputs. Nothing is merged. To import them differently, remove their folders under `var/history/` yourself and run again |
| `a source label is ...` | Section 3 |
| From the recorder: `holds imported history, not this platform's recording` | The recorder or its housekeeping was pointed at the import store. Point it back at its own |
