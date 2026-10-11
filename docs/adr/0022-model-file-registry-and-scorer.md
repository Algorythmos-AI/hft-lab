# ADR 0022: The model file, the model registry, the exact ridge model, and the scorer

- **Status:** accepted, 2026-10-11. No model has been trained on real data and none ships. The models in the
  tests are fitted to synthetic streams with a planted effect.
- **Context:** F5 and F6, foundations

## Context

[ADR 0008](0008-learning-loop.md) made model files artefacts with a content hash, kept outside git. Three
things were left to decide before the first one is written.

- **What a model is tied to.** A model fitted to one definition of a feature and handed rows built to another
  gives numbers, and they are wrong. The same goes for a label with another horizon, or costs that have since
  been measured.
- **How a model's arithmetic replays.** A sleeve that asks a model for a score has to take the same decision
  from a recording a year later, on another machine ([ADR 0005](0005-deterministic-replay.md)).
- **What can be built with what is installed.** This round adds no dependency.

## Decision

### 1. The model file

One line of canonical JSON: sorted keys, no spaces, integers and text only. It holds the kind, the pair, the
feature keys in the order the model reads them, the label spec, the cost model the label was priced with (and
so its placeholder flag), the order size, the SHA-256 of the dataset it was fitted to, the parameters, and a
SHA-256 of the line without that hash. A number that is not whole is kept as text (`"3/4"`), never as a float.

### 2. The registry is a directory, and a model is never overwritten

`ModelRegistry` keeps each file under its hash. A file is written whole under another name and linked into
place, which fails if the name is taken; putting the same model twice changes nothing; a name that already
holds something else is an error and is left as it is.

`load(hash, pair=..., features=..., label=...)` refuses:

- a file that does not match its own hash or is not in canonical form;
- a file that is whole and is another model than its name says;
- a model whose pair, feature keys or label are not the ones the caller expects.

### 3. One real model: ridge regression on standardised features, solved exactly

It predicts the cost-aware outcome of each side from the features. The method is the normal equations in
exact rational arithmetic.

1. One pass over the rows accumulates sums and products as integers of any size.
2. Standardising and then penalising the weights is the same as penalising each unscaled weight by its
   feature's variance, so the standard deviation never has to be taken. The system
   `(C + (penalty / n) diag(C)) v = c` is solved by Gaussian elimination on fractions.
3. A prediction is a fraction, exact for a row of whole numbers.

Nothing is iterated, nothing is rounded and there is no tolerance. The means and variances are those of the
training rows only; they are kept in the model file and applied unchanged to whatever is scored later.

Its limits:

- It is linear. An exact solution of the wrong model is exactly wrong.
- A feature that never varies in training gets a weight of zero.
- With a penalty of zero, features that repeat one another have no single solution, and the fit is refused.
- The cost is rows times features squared, then features cubed operations on fractions that grow with the
  number of features. It is for tens of features.

### 4. Two baselines, in the same format

The model that never trades and the model that takes a side by a seeded coin are model files like any other,
so a report loads its baselines the way it loads its model.

### 5. The scorer

```
score(feature_row) -> Decision(side, expected_micro, model) | None
```

A decision is a side and the outcome the model expects of it after costs, in whole millionths, rounded down.
None is an abstention: nothing above zero is expected, or the sides cannot be told apart. Whether an
expectation is enough to trade on is the asking sleeve's business, which has a margin of its own.

- Scoring a ridge model is one integer sum and one division per side, and the sides are compared as
  fractions. No float is involved, so the decision is the same on any machine.
- A request and a reply can each be written as one line of canonical JSON. The scorer can be moved into a
  process of its own later without the caller changing. In this round it runs in the caller's process.

### 6. The ML sleeve is a skeleton that asks a scorer

`hftlab.strategy.ml.MlSleeve` is a sleeve like any other ([ADR 0014](0014-sleeves-as-pure-functions.md)). It
is handed a scorer, a feature builder and a feasibility map.

- It refuses a feature builder whose keys are not its model's, in order.
- It enters only on a cell the map marks viable, and only when the expected outcome is above its margin.
- It leaves at the label's horizon and waits between attempts as the control sleeve does.
- It reports why its results may not leave shadow: a model fitted to placeholder costs, a map computed on
  placeholder costs or a placeholder rule, a cell that is not viable. Stages are the trader's
  ([ADR 0019](0019-trader-loop-and-staged-rollout.md)); the sleeve cannot set one.

## Consequences

- A model cannot part from the features, the label and the costs it was fitted under: they are in its hash.
  Measuring the cost model makes every existing model a model of other costs, which is correct.
- Replay parity extends through a model: the tests fit a model, run the sleeve in the shadow runner twice and
  compare the journals byte for byte.
- On a synthetic stream with a planted effect the sleeve earns exactly what the effect is worth, and on a
  stream without it the sleeve pays the control sleeve's cost per round trip. That checks the plumbing. It
  says nothing about any market.
- **Gradient boosting and neural networks are not built.** Each needs a new dependency, and with it floats in
  the model, a pinned build and a decision on where training runs. None of those decisions has been made.
  When one is, its float arithmetic stays inside the scorer and the decision that leaves it is still a side
  and whole millionths.
- **The Directional Change sleeve is not built.** It waits for its published method to be read into the
  knowledge base, so that it is built from the source and not from memory.
- The scorer's own process, the weekend training run, the weekly promotion and the drift watch of ADR 0008
  are not built.

## Addendum, 2026-10-11: gradient boosting is built

The consequence above that gradient boosting is not built no longer holds.
[ADR 0025](0025-gradient-boosting-optional-set-and-whole-number-trees.md) adds it as a second kind of model
file, `gbm`. What this record expected of it changed in one respect: the float arithmetic does not stay inside
the scorer, it stays out of it. The library's floats end where the trees are exported, and the scorer adds
whole numbers. Neural networks are still not built.

## Addendum, 2026-10-11: the scorer's own process is built

The scorer can now run in a process of its own ([ADR 0023](0023-weekly-learning-loop.md), section 5). The
request and the reply lines of section 5 above are what crosses the pipe, unchanged, and the caller did not
change: a sleeve is handed a scorer and cannot tell which kind. In process is still the default and is what a
replay uses.
