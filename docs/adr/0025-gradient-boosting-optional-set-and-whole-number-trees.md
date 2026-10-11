# ADR 0025: Gradient boosting: one optional library to train with, whole-number trees to score with

- **Status:** accepted, 2026-10-11. Built and tested on synthetic rows and streams only. No model of this
  kind has been trained on real data, none ships, and none has served. Nothing here is evidence of an edge.
- **Context:** F6, the learning loop. The number 0024 was handed to a record written in parallel.

## Context

[ADR 0022](0022-model-file-registry-and-scorer.md) built one real model, a ridge regression solved exactly,
and said why it stopped there: a second kind needs a dependency, and with it floats in the model, a pinned
build and a decision on where training runs. The owner has now decided to add gradient boosting beside ridge,
and allowed one new dependency for it. Four things follow from the platform and had to be settled.

- **Where it runs.** Training runs at the weekend on a small shared server: two processors, a training unit
  capped at 2 GB and a slice that binds below that ([ADR 0007](0007-shared-host-isolation.md),
  [ADR 0023](0023-weekly-learning-loop.md)). Scoring runs inside the trader's loop.
- **What the recorder and the trader install.** They run all week. A numerical library is tens of megabytes
  of compiled code they have no use for, and one more thing that can fail to import.
- **Replay.** A recorded day must replay to the decisions it took, byte for byte, on another machine a year
  later ([ADR 0005](0005-deterministic-replay.md)). Model files hold no float so far. A library's
  floating-point prediction is not a thing that promise can rest on: it can differ between two builds.
- **Honesty.** Trees fit noise more readily than a straight line does. A second kind of model is also a
  second way to find something that is not there.

## Decision

### 1. One library: LightGBM, pinned

`lightgbm==4.7.0`. The other candidate was the histogram gradient boosting of scikit-learn. Both have wheels
for Linux x86-64 and macOS arm64 on Python 3.12, both are maintained, and both need numpy and scipy.

| | LightGBM | scikit-learn |
|---|---|---|
| What it adds beside numpy and scipy | The library itself, about 5 MB installed, and one small pure-Python package it depends on | The whole of scikit-learn, which is the larger package, and two helper packages. Not installed here, so not measured |
| Determinism | Documented settings for it: a `deterministic` mode, a fixed layout (`force_row_wise`), a thread count and a seed, all parameters of the fit | The same result for a fixed seed without subsampling or early stopping. The thread count is the environment's, not a parameter of the fit |
| Scoring without the library | `dump_model()` is a public, documented description of every tree: feature, threshold, the kind of comparison, how a missing value goes, every leaf | The fitted trees are reached through private attributes, and their thresholds through the model's own binning |

The third row decided it. What is scored must not need the library (section 3), so the trees have to be read
out of it, and reading them through a documented interface is the only version of that which survives an
upgrade.

**Measured on macOS arm64:** the set adds 80 MB to the environment (scipy 72 MB, lightgbm 5 MB, narwhals
2 MB). numpy, 24 MB, is in the base lock already, because the broker's client library depends on it. The
Linux x86-64 size has not been measured; expect somewhat more, since scipy's wheel is larger there.

**It needs the OpenMP runtime of the system,** which the wheel does not carry: `libgomp1` on Debian and
Ubuntu, `libomp` from Homebrew on macOS. Without it the import fails, and a gbm run is refused with the
message of section 2.

### 2. An optional requirement set of its own

`requirements-ml.in` names the one library. `requirements-ml.lock.txt` is compiled from it by `make lock`
with hashes, like the other two and constrained by them, so the three install into one environment.

- `make bootstrap` does not install it. `make ml` adds it.
- CI has a third job, `ml`, which installs it and runs the tests of the models, the learning loop and the
  lineup. The `test` job never installs it and is as light as it was. `test` and `leakcheck` remain the
  required checks; whether `ml` becomes one is the owner's branch setting.
- On the host, `hft-install --with-ml` adds it to the platform's environment, by hash. Without the flag
  nothing of it is installed.
- **One function imports it:** `hftlab.learn.boost.library`. Importing any module of the platform, that one
  included, needs none of the set. A scan holds that no other module names it, and a test imports the
  trader, the recorder and every module but the broker's adapter in a process where the set cannot be
  imported at all.
- Tests that fit a model are skipped, with the reason, where the library is absent.
- A run configured for `gbm` on an installation without the set is **refused** (exit 2) before a dataset is
  built or a trial is counted, and says `make ml`.

### 3. The boundary: floats to fit, whole numbers to keep and to score

Training uses the library's floats. They stop at `hftlab.learn.boost.keep`:

| The library has | The model file keeps | Why nothing is lost, or how much |
|---|---|---|
| A split: feature `j`, `x[j] <= t` goes left, `t` a float | The whole number `floor(t)` | A feature is a whole number ([ADR 0020](0020-features-and-labels-timing.md)), and for a whole `x`, `x <= t` and `x <= floor(t)` are the same question. The row goes the same way: exact |
| A leaf value `v`, a float, in millionths of the quote currency | The whole number nearest to `v x scale`; `scale` is in the file | The one rounding. At most half of `1 / scale` per tree |
| A prediction: the float sum of one leaf per tree | The exact sum of the kept leaves, over `scale` | Within `trees / (2 x scale)` of the library's: 0.025 millionths with the shipped 50 trees and a scale of 1000 |

- **The fit is refused** unless every feature and target is a whole number below 2^53, which a float holds
  exactly; unless every split is a plain `<=` on a row with every feature (the library is told
  `use_missing: false`, and a split of any other kind is not written down); and unless a sample of the
  training rows ends in the same leaf of every tree in the library's model and in the exported one.
- **The model file** is the format of [ADR 0022](0022-model-file-registry-and-scorer.md) with the kind
  `gbm`: one canonical line of integers and text, named by its hash. Its parameters are the scale, the row
  count, the feature count, the settings, the library's name and version as text, and for each side a list
  of trees, each five lists of whole numbers. The library's name is there to say where the trees came from.
  Nothing reads it.
- **The scorer** is `hftlab.models.trees` and `TreeScorer`: for each side, the sum of one leaf per tree;
  the larger side wins; the expectation is that sum divided by the scale, rounded down, in whole millionths.
  Comparisons and sums of whole numbers. It imports nothing but the standard library, refuses a row that is
  not whole numbers, and refuses a file whose splits do not lead every row to exactly one leaf.
- `hftlab.models` cannot import `hftlab.learn`, and a scan holds that. What scores cannot reach what fits.

### 4. Small, single-threaded and deterministic

The settings are `boosting:` in `config/learn.yaml`, read when `model: gbm`.

| Setting | Shipped placeholder | The loader refuses |
|---|---|---|
| `trees` per side | 50 | more than 500 |
| `max_depth` | 3 | more than 6 |
| `leaves` per tree | 8 | more than 2 to the power of the depth |
| `bins` | 63 | more than 255 |
| `learning_rate_ppm`, `min_leaf_rows`, `l2_ppm`, `leaf_scale`, `seed` | 50000, 200, 1000000, 1000, 1 | a number that is not whole |

The library is told: one thread; its deterministic mode; the row-wise layout, because the other is chosen
by a timing; the seed; and every row and every feature in every tree. Nothing is subsampled and no threshold
is drawn, so the seed has nothing to draw. It is fixed all the same.

**What that gives, and what it does not.** The same rows, settings and seed give the same model file on the
same build of the library on the same machine: a test fits twice in one process and once in another and
compares the hashes. Across builds or processors the library may round a sum differently and grow another
tree. That would change which model a training run registers there. It cannot change what a registered
model decides: a model is served by the hash of its file and scored without floats.

**Memory.** A run's peak is the process's own, from the operating system, written to `last-run.json` beside
the report, and a run above its budget fails and registers nothing (ADR 0023). It is not in the evaluation
report, which must be the same bytes on every run. A gbm run of a synthetic week of about 10,000 rows, in a
process of its own, peaked at about 95 MB on macOS arm64, the library and its arrays included. The peak of a
real week does not exist yet.

### 5. It plugs into what exists

`model: ridge | gbm` in the settings; ridge is the default and the shipped value. The kind chooses the
function that fits. Everything after that is shared and asks nothing about the kind:

- the frozen configuration that a trial is counted by holds the kind and that kind's numbers, so the two
  kinds are two trials and a number a run does not read makes no new one;
- the walk-forward, the oracle and the seeded coin on the same rows, the day-block interval, the deflated
  Sharpe ratio, the report, the registry, the candidate in the lineup journal;
- the trader loads it by hash and gets a scorer from `scorer_for`; the weekly decision promotes it by the
  same rule and the same gates; the drift look reads its week the same way. A scan holds that none of the
  three reads a model's kind.

### 6. No search

The hyperparameters are fixed in the file and no run searches them. A different number is a different
configuration, written into the ledger as another trial. If a search is ever wanted it is an evaluation
change: declared in configuration beforehand, small, and every member counted.

### 7. What is not built

- **No neural network.** It waits for recorded data and a decision on compute: the shared server has no
  capacity for it, and nothing recorded yet says a larger model has anything to learn.
- No hyperparameter search, no early stopping, no subsampling, no categorical features, no missing values.
- No second library, and no use of the library's own model file: a model exists only as the whole-number
  file.
- The scorer in its own process (ADR 0023, section 5) is still not built. A gbm model is scored in the
  trader's process like a ridge one.

## Consequences

**What holds, each with a test**

- On 8,000 rows the fit never saw, values on and beside the thresholds among them, the exported trees end
  every row in the leaf the library ends it in, for every tree, and the two predictions differ by no more
  than the stated rounding (`tests/learn/test_boost.py`).
- Two fits of the same inputs are the same file, in one process and in another.
- A gbm model file is loaded and scored, and the trader and the recorder are imported, in a process where the
  set cannot be imported (`tests/models/test_boundary.py`). These run in the base job too.
- **Where something is, it finds it.** On a synthetic stream where the mid follows the heavier side of a
  mildly one-sided book and goes against it on a strongly one-sided one, arranged so that the imbalance and
  the move are exactly uncorrelated, ridge takes no trade and the trees take every paying row, on the right
  side, walk-forward.
- **Where nothing is, it shows nothing.** On the same book with a move that follows a coin of its own, the
  trees do trade: fitted to noise, they expect a profit of some rows. Walk-forward they lose their costs. The
  lower bound of the interval of their mean result per trade is not above zero, and neither is the lower
  bound of that mean less the seeded coin's. Counted once over forty seeds: the second bound was above zero
  for three, the first for none, and the total was a loss for all forty. A 95% interval of ten days is wrong
  about that often.
- A gbm candidate becomes a challenger, is promoted by a rule in force and by nothing else, and is watched
  for drift, by the code that does it for ridge (`tests/lineup/test_gbm.py`).

**What it costs, and what it does not cover**

- **The trader's process holds a second kind of arithmetic.** It is a few hundred comparisons of whole
  numbers per row at the shipped size, and it cannot hang on a library. It is still in the trader's turn.
- **The host's environment is one environment.** With `--with-ml` the library is installed where the trader
  could import it. Nothing does, and a test holds that; a separate environment for the training unit would
  make it impossible instead of untested-for, at the cost of a second copy of everything.
- **An environment that is rebuilt loses the set** unless `--with-ml` is given again. The next gbm run is
  then refused, loudly, and registers nothing.
- **The tests that fit ran on macOS arm64.** The `ml` job's first run is the first on Linux x86-64.
- **The same hash across machines is not promised** (section 4). Two hosts that train on the same recordings
  may register two models. This platform has one host.
- **The agreement check at fit time is a sample** of at most 512 training rows. The test of the boundary is
  the thorough one.
- **The placeholders are placeholders.** Fifty trees of depth three with leaves of 200 rows were chosen to be
  small and were tuned on nothing. Choosing others on evidence is an experiment with its trials counted.
- **On real data the likeliest outcome** of the first gbm challenger's week is no advantage over the control
  sleeve or over the ridge challenger after costs (`HFT-HYP-0004`, `HFT-EXP-0004`: registered, not run).

## Addendum, 2026-10-11: a small network is built

Section 7 said no neural network is built. [ADR 0029](0029-sequence-network-windows-and-fixed-point-scoring.md)
adds a small one as a third kind of model file, `seq`. It took another route than this record's for its
dependency: nothing is installed for it, and the optional set here is still LightGBM alone. It kept this
record's boundary: floats to fit, whole numbers to keep and to score.
