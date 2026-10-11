# ADR 0028: Imported history: a store of its own, provenance in the data, no download, and no promotion on it

- **Status:** accepted, 2026-10-11. Written and tested on a few hand-written rows in temporary folders. **No
  exported file has been read with it, and no model has been fitted to an imported day.**
- **Context:** the learning loop ([ADR 0008](0008-learning-loop.md), [ADR 0023](0023-weekly-learning-loop.md))
  and the recording it learns from ([ADR 0012](0012-recording-format.md)). The number 0026 was left for a
  record written in parallel.

## Context

A model is fitted to recorded days, and the recorder has not yet run on the real feed. Fitting anything worth
the name needs months of quotes; waiting for the recorder to collect them delays every later phase by that
long.

The owner decided how the gap may be bridged, and where the decision stops:

- **The platform's own recordings come first.** They are the only evidence of what this venue shows this
  account.
- **Before enough of them exist, a model may be fitted to history from elsewhere.** The owner exports tick
  files himself, from a data vendor's own export tool, under the terms he accepts there, and puts them in a
  folder.
- **The platform only reads such files from a disk.** It downloads nothing.

Two things can go wrong with that, and neither shows in a test of the arithmetic.

- **Imported quotes get taken for the platform's own.** They are in the same format, on the same disk, and
  read by the same code. A result on them then reads as a result on this venue, and it is not one: another
  venue's spread, another venue's size, another clock.
- **A missing size gets taken for liquidity.** Many exports hold prices and no sizes. The format has a size in
  every quote, and the shadow venue fills against the size on display
  ([ADR 0013](0013-shadow-cost-model.md)). Whatever number is written there, something downstream will trade
  against it.

## Decision

### 1. The platform downloads nothing

`python -m hftlab.history import` reads the files of one folder and writes day folders. That is all of it.

- The package imports no network library, holds no address of anybody's server and names no vendor. A test
  scans it for each.
- It has no clock and draws nothing. What it writes is a function of the files, the description, the label
  and the calendar.
- How a file came to be on the disk is the owner's act, under terms between him and the vendor. No command,
  runbook or record of this repository says where to get one or how.

### 2. Imported history has a store of its own

Imported days are written in the recording format, unchanged, so the replay, the day check, the features, the
labels, the datasets and the feasibility map read them with the code that reads a recording. They are written
under another root (`var/history` as shipped), never beside a recording.

- The import store's root holds a marker file, `import-store.json`.
- **The recorder refuses to open it**, and so does its housekeeping: a root with the marker, or with any day
  that opens with a provenance line, raises `WrongStore` before anything is written, and the command exits 2.
- **The importer refuses to write into the recorder's store**: the root the recorder's settings name, a root
  holding the recorder's run counter or status file, a root holding any recorded day, and any folder that is
  not empty and is not an import store.

One store with a flag on each day was the alternative. It was rejected because the recorder's store has
promises of its own: one index with no hole across restarts, a quota, a way out for old hours. An imported day
keeps none of them, and a housekeeping pass that compressed one would change bytes that are supposed never to
change (decision 5).

### 3. Provenance is part of the data

A day is imported if either of two things says so, and recorded only if neither does
(`hftlab.record.origin`).

- **Its journal opens with a provenance line**:

  | Field | Value |
  |---|---|
  | `source` | `imported` |
  | `label` | The owner's name for where the files came from: 1 to 64 letters, digits, dots, dashes and underscores, no space |
  | `terms_verified` | `false` |
  | `parser`, `parser_version`, `importer_version` | Which code read the files |
  | `time_zone` | The zone as stated |
  | `stated` | Everything else that was stated: the layout, the header, the size unit, the instruments, and for the generic layout the delimiter, the time format and the columns |
  | `inputs` | For every file that had a row in this day: its name, its pair, its SHA-256, rows read and rows kept |
  | `sizes`, `placeholder_size` | `stated`, or `placeholder` and the placeholder (decision 6) |

- **Every one of its segments carries run number 0** in its header, under the header's checksum. A recorder's
  runs count from 1. So a journal that is lost or edited does not turn imported quotes into recorded ones:
  such a day is `imported:unlabelled`.

A count by source is kept under `recorded` or `imported:<label>`.

### 4. `terms_verified` is false, and what would change it

Nobody working on this repository has read any vendor's terms. Whether an export may be used to fit a model,
kept, or mixed with other data is between the owner and the vendor. The tool cannot know, so it does not
pretend: `terms_verified: false` is a constant in the code, written into the marker, every imported day and
every report. There is no flag that sets it.

It changes only by a later record that names the source label, says who read which terms on which date and
what they allow, and adds the field to the description file with that record's id. Until then nothing reads
the field as permission, and no result that rests on an imported day is published anywhere.

### 5. Nothing about a file is guessed, and a day is written once

- **The description file states everything**: the layout, the time zone, whether there is a header, the
  instruments, and which file holds which pair. A file with no stated time zone is refused. A file in the
  folder that the description does not list is refused, not skipped.
- **Three layouts, each with its own small parser, described by its columns only**: a generic one whose
  delimiter, columns and time format are stated; one with a UTC time and the ask before the bid, with sizes;
  one with a wall-clock time in a stated zone and the bid before the ask, without sizes.
- **Prices are exact.** A price is read as a decimal to the instrument's price scale, by the function the
  venue adapter uses. Nothing is rounded.
- **Every fault of a row has one stated answer** (`hftlab.history.rows`):

  | What | Answer | Why |
  |---|---|---|
  | The wrong number of fields, a time that does not match the format, a price or a size that is not a plain decimal number | The file is refused | The description is wrong for this file. Guessing which lines it fits is not the tool's to do |
  | A wall-clock time in the hour a clock change repeats or skips | Dropped, counted | It names two instants or none, and the file does not say which |
  | A time earlier than a row before it in the same file | Dropped, counted | Where it belongs is not known; a replay would deliver it at a time it does not carry |
  | A row while the market is shut by the calendar | Dropped, counted | Nothing trades on it; many of them mean the stated zone is wrong |
  | A price with more decimals than the scale, or not above zero | Dropped, counted | Rounding it would invent a price |
  | A crossed quote | Dropped, counted | The recorder keeps one, as what its venue sent. Here it would let a label buy below the bid of another feed |
  | A size that is not a whole number of units in the stated unit | Dropped, counted | The same as a price |
  | A row that repeats the row kept before it, at the same time | Dropped, counted | It says nothing new |
  | A silence longer than the threshold | Kept as a silence, journalled | By the recorder's own gap tracker. The reason is `market_closed` or `unexplained`: nobody knows why another feed was silent |

  The counts are in the day's journal by file and reason, and in the report the command prints.
- **The same files give the same bytes**, in every segment and every journal.
- **A day already in the store is never merged and never written over.** A run that would write the same
  bytes touches nothing. Any other run for that day is refused: a file that changed, another label, a pair
  more or less. To import a day differently the owner removes its folder first.
- **A refusal refuses the run.** Days are built in a staging folder and moved into place only when all of
  them can be. A run that is refused or killed leaves the days in the store as they were.

### 6. A size that was not in the file is one unit, and says so

Where a file has no sizes, every quote gets a size of **one base-currency unit** on both sides.

- Zero was the first candidate. A book showing nothing on either side gives no feature row
  ([ADR 0020](0020-features-and-labels-timing.md)), so a day of zeros could not even be looked at.
- Any number near an order's size was the wrong candidate: the shadow venue would fill against it, and every
  label and every map cell on that day would rest on liquidity nobody displayed.

One unit is the smallest size that says "there is a book". The shadow venue's rule needs no exception for it:
it fills no more than the size on display, an order of the minimum size is far larger than one unit, so the
order does not fill, the label is "no entry", and a window of the feasibility map is "unfillable". **The
displayed-size rule cannot be flattered by a placeholder, because the placeholder is a size the rule already
refuses.**

The cost is stated as plainly: a day without sizes can show what prices did, and cannot say what an order
would have made. A file that holds sizes is read in a unit the owner states, and those sizes are still another
venue's.

A size imbalance computed on two placeholders is exactly zero on every row. That is a made-up constant, and
it is why such a day is flagged rather than passed off: the day's provenance says `sizes: placeholder`.

### 7. Everything that counts rows says where they came from

- **A dataset** keeps, for every day of its stream in order, the day, its source and how many events it gave,
  and all of it is in the dataset's hash. The same quotes recorded and imported are two datasets with two
  hashes. `split` says of any set of its rows how many rows, and how many days, came from each source. A row
  belongs to the day the event of its decision came from, counted by the event's number, not by a clock.
- **A stream nobody vouched for is `unstated`**, never `recorded`. Only a day folder that says it is the
  platform's own is counted as that.
- **The evaluation report** has a `sources` block with one entry for each of its results, by name: the
  three-way comparison, the interval, the deflated Sharpe ratio, the hold-out and the fitted model. Each is
  the rows and days by source of exactly the rows that result was computed on. Training prints the same on a
  line per candidate, and the weekly scorecard says how many of the fitted rows were not recorded.
- **A model file** names its dataset by that hash, so two models fitted to different mixes of sources are two
  models, and the report that carries the split is pinned beside the model, hash by hash, in the lineup. The
  model file was not given a field of its own for the split in this record; it has one since (the addendum
  at the end).
- **The feasibility map** says the days and the events by source, and names the days whose sizes are
  placeholders.
- **A run that read a day with placeholder sizes says it is not a result**, in the way a run on placeholder
  costs does: `placeholders.sizes`, `not_a_result`. The weekly decision already refuses a challenger whose
  report says that.
- **A shadow run over imported days** names them `history:<day>=imported:<label>` in the first line of its
  journal. It used to call any day folder a recording.
- `python -m hftlab.learn.train --imports <store>` and the feasibility command with the same option read
  an import store beside the recordings. A date both hold is read from the recording, and the imported day
  is left out and named: where the platform has its own evidence, somebody else's is not mixed into it.

### 8. An imported day counts towards no promotion

Results on somebody else's quotes are not this venue's results. A model fitted to them has learned another
venue's spread, another venue's sizes and another clock. It may still be worth a week in shadow, which costs
nothing; it is not to become the champion on that evidence.

So the weekly rule ([ADR 0021](0021-validation-splits-trials-and-the-three-numbers.md),
[ADR 0023](0023-weekly-learning-loop.md)) has a data requirement, in the pure decision and again in the
command:

- A challenger is promoted only if its model was fitted to at least the rule's `min_days` days of **this
  platform's own recordings**. The weekly decision reads that number from the challenger's evaluation report
  (`sources.model.days.recorded`), which is pinned by hash beside the model.
- **Days of imported history are not counted, however many there are.** A report that does not say where its
  days came from has no recorded day. The reason code is `too_few_own_days`.
- A rule asks for at least one day, so a model fitted to imports alone passes no rule. A test trains the
  same model on the same quotes twice, recorded and imported, gives both the same good week, and sees one
  promoted and the other kept.

The rule was given no new number. Its `min_days` is asked twice: of the week's shadow evidence, as before,
and of the recorded days behind the model. A number of its own would have been cleaner, and was not added
because the registered experiments (`HFT-EXP-0003`, `HFT-EXP-0004`) freeze the rule's numbers value for
value, and a settings file with a key they do not have could not be the file they describe. A decision
record that sets the rule can introduce one.

This is a change to how a challenger is evaluated. It makes the rule stricter and can turn no "keep" into a
"promote". The rule is not in force, no result exists that it could have affected, and it was asked for by
the owner; the record of it is this one.

## Consequences

- The owner can put months of history beside the recordings with one command, and can prove afterwards which
  bytes of which file every imported day came from.
- A day folder says what it is to any reader, with or without its journal, and no reader has to be told.
- An import of a file with one malformed line imports nothing. That is deliberate and will be inconvenient at
  least once.
- Adding a pair to a day already imported means removing the day and importing it again with both files.
- Imported days have no heartbeat, no connection record and no start record. A book is confirmed only by a
  quote, so on a quiet feed the features start over more often and a label finds a stale book more often than
  on a recording. Both err against the result.
- A silence that runs over midnight UTC is not counted: each day is checked by itself, so that a day's bytes
  do not depend on which other days were imported with it.
- The format's sizes are whole base-currency units. An export whose sizes are in another unit is read only
  with that unit stated; one stated wrongly gives sizes that are wrong by that factor, and nothing here can
  notice.
- A model fitted to recordings alone is judged exactly as before, as long as it was fitted to as many recorded
  days as the rule asks of the shadow week. A fold needs a week to train on and a week to test, so a model
  that was evaluated at all has more than a week of days behind it.
- A dataset's hash changed for every dataset (its version is 3): where the days came from is now in it.
  Models and reports made before this change name datasets by the old hashes, and say nothing of sources, so
  the weekly decision counts no recorded day for them. None exists outside a test.
- Nothing here has read a real export. The two fixed layouts are written from their column order; the first
  real file of each may show a detail they do not cover, and the generic layout is the way round it until a
  parser version is added.

## What is not here

- **A field for the split in the model file.** The model file's format belonged to work that was under way in
  parallel, and was not touched. The model was bound to the split through its dataset's hash and through the
  report pinned beside it; reading the split needed the report. **Added since: see the addendum below.**
- **A number of its own for the data requirement**, for the reason in decision 8.
- **A mark on the week's shadow evidence.** The weekly decision counts shadow days from the trader's
  journals, and the trader reads no day folder of any store: it sees the venue's quotes, and its replay
  re-reads its own session's tape. So an imported day cannot become a day of shadow evidence today, and
  nothing checks that it never will. Anything that later lets the trader run over a store has to carry the
  day's origin into its journal first.
- **A way to set `terms_verified`.** Decision 4.

## Addendum, 2026-10-11: the model file says where its rows came from

The first point of "What is not here" no longer holds. A model file is of version 2 and has a field
`sources`: the rows the model was fitted to, and the days they are from, by source
(`{"rows": {"recorded": ..., "imported:<label>": ...}, "days": {...}}`).

- **It is written at fit time, from the dataset**, for all three kinds (ridge, trees, sequence network):
  `Dataset.split` of exactly the rows handed to the fit. A day that gave no fitted row is not counted.
- **It is under the file's hash**, like every other field: the same weights fitted to another mix of sources
  are another model.
- **A file of version 2 that lacks it is refused on load.** A file of version 1 never had it and still
  loads, byte for byte and under the hash it always had. Its split is **unknown**, and is said so
  (`unknown`), never as no rows or no days.
- **The weekly decision reads it.** Where the model file says its days they must be the days its report
  says, or no day is counted and the challenger is kept with `too_few_own_days`. A file of version 1 is
  judged by its report alone, as before. This can turn a "promote" into a "keep" and never the other way.
  The decision report shows both: `fitted_days_by_source` and `model_file_days_by_source`.
- **Training says it** on a line of its own for each candidate, read from the file it has just written.

No model file exists outside a test, so nothing was converted. The baselines (`flat`, `random`) are fitted to
nothing; their `sources` are those of the dataset they are for.
