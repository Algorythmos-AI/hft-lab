# ADR 0029: A small sequence network: fitted with arrays and no new library, read through windows, scored in fixed point

- **Status:** accepted, 2026-10-11. Built and tested on synthetic rows and streams only. No model of this
  kind has been trained on real data, none ships, and none has served. Nothing here is evidence of an edge.
- **Context:** F6, the learning loop. The "DL challenger" of the roadmap.

## Context

[ADR 0025](0025-gradient-boosting-optional-set-and-whole-number-trees.md) added a second kind of model and
said what was not built: no neural network, until there was a decision on compute. The owner has now decided
to add one, small, as a challenger: it starts in shadow and has to beat the ML sleeve under the promotion rule
that is already written before it can trade. Five things follow from the platform and had to be settled.

- **Where it runs.** The same small shared server: two processors, a training unit capped at 2 GB and a slice
  that binds below that ([ADR 0007](0007-shared-host-isolation.md), [ADR 0023](0023-weekly-learning-loop.md)).
  No accelerator, and none wanted.
- **What it costs to install.** The recorder and the trader run all week and must install and import without
  whatever fits a network.
- **Replay.** A recorded day must replay to the decisions it took, byte for byte
  ([ADR 0005](0005-deterministic-replay.md)). Model files hold no float, and a scorer adds whole numbers.
- **Time.** A model of this kind reads more than one row. Every rule the platform has about what was known
  when ([ADR 0020](0020-features-and-labels-timing.md), [ADR 0021](0021-validation-splits-trials-and-the-three-numbers.md))
  was written for a single row.
- **Honesty.** A network fits noise more readily than trees do, and it is a third look at the same data.

## Decision

### 1. No new dependency: the network is written out, on the arrays the base lock already holds

The two routes were the CPU-only build of one mainstream library in an optional set of its own, as ADR 0025
did for LightGBM, or a network, its gradients and its optimiser written by hand on numpy, which is in the
base lock because the broker's client library depends on it.

| | A mainstream library (PyTorch 2.14.1, read from the package index's own listing; **not installed**) | By hand on numpy 2.5.3 |
|---|---|---|
| Install | The wheel for macOS arm64 is 127 MB compressed. The wheel the public index serves for Linux x86-64 is 555 MB compressed and requires the vendor's GPU runtime packages beside it. The CPU-only Linux build is not on the public index: it is served from the project's own index, so one hash-locked file compiled for both platforms from the public index cannot name it | Nothing is added. numpy is 24 MB in the environment and was there already |
| The same fit on both platforms | Documented as not promised across platforms or releases, even with its deterministic mode on and one thread | Follows from what is used (section 4), on any machine with IEEE 754 doubles. Shown on macOS arm64; the first run on Linux x86-64 is the check |
| Fit time, for a model this small | Not measured: nothing was installed | Measured (below). Seconds for a synthetic week |
| What else it brings | A compiled runtime of several hundred megabytes in the environment the trader shares, for a network of about a thousand weights | About 200 lines, and a test of the gradients |

The first row decided it, and the second would have on its own. Disk on the development machine was checked
first (8.1 GB free of the 4 GB the owner asked for), and then nothing was downloaded: the sizes above are the
index's listing. **Installed size added: zero.**

**Measured on macOS arm64**, one process, the test settings of 8 filters and 16 hidden units on windows of 8
rows of 10 values. A whole training run of three synthetic weeks (6,372 windows: the dataset with its checks,
two walk-forward fits and the candidate's at 20 passes each, and every test window scored by the whole-number
scorer) took 3.2 s by the clock and peaked at 103 MB, the array library and the dataset included. A larger
one (11,232 windows, 30 passes) took 18 s on a machine busy with other work, and peaked at 153 MB. A recorded
week has not been fitted: none exists.

- **One module fits a network:** `hftlab.learn.sequence`. It imports the array library inside its functions,
  as `hftlab.learn.boost` does, so importing any module of the platform needs neither. A scan holds that no
  other module names the array library, and a test imports the trader, the recorder, the scorer's child and
  every module but the broker's adapter in a process where it cannot be imported at all.
- Tests that fit a network are skipped, with the reason, where the array library is absent. It is in the
  base requirements, so in practice they run in the required `test` job.
- A run configured for `seq` in an environment without the array library is **refused** (exit 2) before a
  dataset is built or a trial is counted.
- There is no new requirement set, Makefile target, install flag or CI job, because there is nothing to
  install.

### 2. The network

One 1-D convolution over the rows of a window, one hidden layer, one output per side, and between them the
one activation `min(max(x, 0), cap)`. The settings are `sequence:` in `config/learn.yaml`, read when
`model: seq`.

| Setting | Shipped placeholder | The loader refuses |
|---|---|---|
| `window` rows | 8 | fewer than 2, more than 64 |
| `kernel` rows | 3 | more than the window |
| `filters` | 8 | more than 32 |
| `hidden` units | 16 | more than 64 |
| `epochs`, `batch_rows` | 30, 256 | more than 500 passes |
| `learning_rate_ppm`, `l2_ppm` | 2000, 100 | a rate of 0 or above 1,000,000 |
| `clip`, `cap` | 8, 8 | 0, or more than 16 |
| `seed` | 1 | a number that is not whole |

With the shipped eleven features that is 1,090 weights. A model file refuses more than 20,000.

It is fitted by Adam on the squared error of each side's outcome, over batches taken in a fixed order, for a
fixed number of passes, with a step that shrinks evenly to nothing. Each feature is centred and scaled by the
whole-number mean and standard deviation of the training rows, cut off at `clip` standard deviations; the
targets are divided by one whole number, the root of their mean square. Nothing is stopped early, nothing is
dropped out and nothing is drawn.

### 3. A window, and the rule about time

The model reads the last `window` feature rows of one pair. `hftlab.features.window.WindowBuilder` makes
them, one event at a time, by the same `step` offline and in the trader, from the rows `FeatureBuilder` makes
and from nothing else: a window's values are those rows' values end to end, oldest first. Its keys say so
(`spread@v1[-2]` is the spread two rows back; the row in hand keeps the keys it always had).

**Every row of a window was known strictly before the label window starts.** A window ends in the row made
on the event in hand, and a row is made only from events up to its own.

**A window never spans a time nobody could vouch for.** The rows in hand are thrown away:

| | When |
|---|---|
| gap | An event of the pair gives no row. That is every case in which the builder could not trust the book or forgot it: a book unconfirmed for longer than `stale_after_ns`, a market that is shut, a crossed book, a book with no size, a history shorter than its windows |
| connection | A connection change, or a recorder restart |
| session | The row in hand belongs to another trading day than the rows held. A window does not reach across the 17:00 New York rollover, and so not across a weekend |

Such a window produces no row. The single rows that ended no window are counted by reason in the dataset
and in every report made from it (`rows.dropped`: `window_gap`, `window_connection`, `window_session`).

**The dataset builder trusts none of that.** A source of windows has to show the single rows it was made of.
The builder makes those rows itself and refuses the dataset (`LeakError`) unless every window is exactly the
consecutive single rows ending on its own event, says truly which event its earliest row was made on and
when, has between its first and last event no event of the pair that gave no row and no connection change or
restart, and lies within one trading day. The existing look-ahead check runs on the windows as it runs on
single rows: the stream is cut and disturbed, and no window up to the cut may change.

**The purge and the embargo go by the window's earliest row as well as its latest.** A dataset row of a
window keeps when its earliest row was known (`first_ns`). A fold's test week begins at the earliest
`first_ns` of its rows, and a training label must have ended a whole embargo before that. Its latest row was
always covered: a label is priced after the decision.

A dataset of single rows is written exactly as before, so its hash and every configuration counted on it are
unchanged.

### 4. Deterministic by what is used, not by a setting

- **Nothing is drawn.** The starting weights are a hash of the seed and of each weight's place, like every
  other random number of the platform.
- **Every float operation has one correctly rounded answer:** additions, subtractions, multiplications,
  divisions, square roots and comparisons of doubles, element by element. No exponential, logarithm or power.
- **No sum is left to a library to order.** A sum over a batch or over a unit's inputs is taken pairwise by a
  function of this repository. No matrix product and no routine of a linear-algebra library is called, so
  there is no thread count to set and no build of such a library to differ: the one thread is the
  interpreter's. A scan of the module holds this.
- The features are scaled with whole-number arithmetic, and the fit reads the very integers the scorer reads.

**What that gives.** The same rows, settings and seed give the same model file: a test fits twice in one
process and once in another that is told to use four threads, and compares the hashes. Across processors it
follows from the list above and is **not yet shown**: every test that fits ran on macOS arm64.

### 5. The boundary: floats to fit, whole numbers to keep and to score

Training uses floats. They stop at `hftlab.learn.sequence.keep`. The model file is the format of
[ADR 0022](0022-model-file-registry-and-scorer.md) with the kind `seq`: integers and text, named by its hash.

| | Kept as | Scale |
|---|---|---|
| An activation `a` | the integer `q` nearest `a x 2^16` | `BITS = 16` |
| A weight `w` | the integer nearest `w x 2^24` | `WEIGHT_BITS = 24` |
| A bias | the integer nearest `b x 2^40` | the product of the two |
| An output | `z`, kept whole | `z / 2^40`, times `target_scale`, is millionths |

Scoring (`hftlab.models.net`, standard library only):

```
input     t = min(max(x - centre, -clip x spread), clip x spread);   q = (t x 2^16) // spread
a layer   z = bias + sum(weight x q);   q' = min(max((z + 2^23) >> 24, 0), cap x 2^16)
out       z = bias + sum(weight x q)
```

Integer additions, multiplications, comparisons and shifts. The float network takes the same input,
`q / 2^16`, which a double holds exactly, so the input adds no difference at all.

**How far the whole numbers are from the fitted network.** Write `e` for how far a unit's integer can be
from `2^16` times its real activation, and `m` for the most its integer can be. A kept weight and a kept bias
are each within a half of the real one at their scale, so before the shift a unit is off by at most

```
D = sum(|weight| x e) + (sum(m + e) + 1) / 2
```

and after it by `e' = D / 2^24 + 1/2`, since the shift rounds to the nearest and the activation moves two
numbers no further apart. For an input `e = 0` and `m = clip x 2^16`. The bound of an output is its `D`,
rounded up. It is computed from the model file alone, in exact arithmetic, and holds for every row whatever
its values. For the networks of the tests it is between one and six ten-thousandths of an output of one:
from about a hundred to a few thousand millionths of the quote currency, against outcomes of dollars.

**The scorer abstains inside the bound.** It takes a side only when the two outputs are further apart than
both bounds together and the better one is further above zero than its own. So where it decides, the fitted
network decides the same; where it could not tell, it does not trade.

**Nothing overflows.** A model file is refused if, with its weights, any intermediate value could reach
`2^62` for any row at all; a row holding a value at or beyond `2^62` is refused before anything is computed;
a spread is below `2^40`. No feature comes near: the largest are nanoseconds within a trading week, below
`2^50`. The fit keeps every weight within 4, and refuses to keep a network it cannot write down.

The fit is also refused unless a sample of the training rows gives, through both networks, outputs within the
bound.

### 6. It plugs into what exists

`model: ridge | gbm | seq`; ridge is the default and the shipped value.

- **No second sleeve.** The ML sleeve asks its feature builder for a row and its scorer for a decision. A
  window is a row, wider. The sleeve, its saved state across a restart ([ADR 0026](0026-sleeve-state-across-a-restart.md)),
  the request and reply lines and the scorer's own process carry it unchanged.
- **How a model is served is its report's to say.** `serving.features` holds `window_rows` when the model
  reads windows, and the lineup builds the builder that asks for. It does not read the model's kind. Neither
  do the weekly decision, the drift look, or the split by source of [ADR 0028](0028-imported-history.md):
  the scan that holds this for `gbm` now names `seq` too.
- The frozen configuration a trial is counted by holds the kind, the network's numbers and the window, so
  `seq` is its own trial and a number a run does not read makes no new one.
- The drift look summarises every value of a window, so a network is watched on `window` times the columns.

### 7. Memory and time

- **A fit that will not fit is not started.** Before a trial is counted, the run estimates what its largest
  fit holds (`hftlab.learn.sequence.needs`: the inputs and targets as doubles, a few copies of one batch) and
  is refused (exit 2) if that, on top of what the process already holds, is over `memory_budget_mb`.
- The peak a run reached, how long it took by the clock and how much processor time it used are measured and
  written to `last-run.json`, for every kind. They are not in the evaluation report, which must be the same
  bytes on every run.

### 8. No search, and every fit is a trial

The settings are fixed in the file and no run searches them. Another number, the seed among them, is another
configuration, written into the ledger before it is scored.

### 9. What is not built

- No recurrent cell, no attention, no second layer of convolution, no pooling, no normalisation layer.
- No early stopping, no dropout, no learning-rate search, no ensemble of seeds.
- No accelerator and no second library.
- **Windows are not shared between rows of a dataset.** Each row holds its whole window, so a dataset of
  windows is `window` times as wide in memory as one of single rows.

## Consequences

**What holds, each with a test**

- The gradient written by hand agrees with a central difference in every weight and bias
  (`tests/learn/test_sequence.py`).
- On 5,000 windows the fit never saw, every output of the whole-number network is within its own bound of
  the float network's. On 4,000 more, and on over a hundred moved onto the margin on purpose, wherever the
  scorer decides the float network takes the same side; the windows it abstains on that the floats would
  trade are counted, and all are within the bound.
- For networks and rows drawn up to the most a file and a row may hold, no product and no running sum of a
  score leaves a signed 64-bit integer (`tests/models/test_net.py`, a property test).
- Two fits of the same inputs are the same file, in one process and in another.
- A model file of this kind is loaded and scored, in process and by the scorer's child, and the trader and the
  recorder are imported, where the array library cannot be imported (`tests/models/test_boundary.py`).
- **A window that peeks is refused.** One slid a row forward, one that reads the next event in its newest
  place, one that says it begins later than it does, one whose rows are out of order, one that spans a
  restart, a crossed book or a rollover: each is planted and each is a `LeakError`
  (`tests/learn/test_windows.py`).
- **Where something is, it finds it.** On a synthetic stream where the book takes one of three states and
  then another, and the mid then moves by the ORDER of the two, a row shows one state and the move after it
  is up, down and none equally often: ridge takes no trade. The network, walk-forward, makes within a
  twentieth of the oracle.
- **Where nothing is, it shows nothing.** On the same book with a move that follows a draw of its own, over
  three seeds chosen before any was run, the lower bound of the interval of its mean result per trade is not
  above zero, neither is that mean less the seeded coin's, and its total is not a gain. Counted once over
  the seeds 1 to 24 with the test's settings: neither bound was above zero for any, and the total was a loss
  for all twenty-four. A smaller stream with fewer passes, tried to make the test quicker and not kept, put
  the second bound above zero for one seed of three, with a loss in total: an interval of ten days is wrong
  about that often, and the network trades the rows before a move, where one result is large either way.
- A `seq` candidate becomes a challenger, trades in shadow, replays byte for byte, is scored identically by
  the scorer's own process, is promoted by a rule in force and by nothing else, and is watched for drift, by
  the code that does it for ridge (`tests/lineup/test_seq.py`).

**What it costs, and what it does not cover**

- **The trader's process holds a third kind of arithmetic.** About 2,400 integer multiplications a row at
  the shipped size, in the interpreter: some tenths of a millisecond. It is in the trader's turn unless the
  place says `scorer: process`.
- **The same file across processors is argued, not shown.** If the first Linux run disagrees, a model
  registered there is still served by its hash and scored without floats: what would be wrong is this record.
- **The fit is slow per row.** Scaling the inputs is a loop of the interpreter, and the dataset is held as
  tuples of integers. A week of thousands of quotes a second would not fit in the budget as windows; the run
  would be refused, loudly, before a trial is counted.
- **The estimate is an estimate.** It leaves out what the interpreter itself holds. The measured peak is what
  a run is judged by afterwards.
- **A network extrapolates.** Three of the features are clocks. A week whose clocks sit where no training
  week's did is outside what the network saw; the cut-off at `clip` bounds how far, and nothing bounds what
  the network says there. The drift look is what would show it.
- **Fitting is sensitive to the seed** at this size: on the synthetic order some seeds find all of it and
  some nine tenths. A seed is a trial, and choosing among seeds is a search that is not built.
- **The placeholders are placeholders,** chosen small and tuned on nothing.
- **On real data the likeliest outcome** of the first sequence challenger's week is no advantage after costs
  over the control sleeve, the ridge challenger or the tree challenger (`HFT-HYP-0006`, `HFT-EXP-0006`:
  registered, not run).
