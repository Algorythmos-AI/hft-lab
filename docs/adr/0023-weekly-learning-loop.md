# ADR 0023: The weekly learning loop as processes: training, the lineup journal, the decision, the scorer

- **Status:** accepted, 2026-10-11. Sections 1 to 4 and 6 are built and tested on synthetic streams and
  against the fake broker only. Section 5, the scorer in its own process, is decided and **not built**.
  Nothing has been trained on real data, no model has served, and the gate of F6 is open.
- **Context:** F6, the learning loop

## Context

[ADR 0008](0008-learning-loop.md) decided that the platform retrains at the weekend, runs a challenger in
shadow for a week, promotes by a gate written down beforehand and rolls back by itself.
[ADR 0020](0020-features-and-labels-timing.md) to [ADR 0022](0022-model-file-registry-and-scorer.md) built the
pieces as functions. No process called them. Four things were open:

- what a training run writes, and how two runs over the same data are shown to be the same run;
- how the trader learns which model to serve, without reading a file that anybody could replace;
- who acts on the promotion decision, and what stops it from acting without a registered rule;
- what happens to the trader when the thing that scores a row stops answering.

## Decision

### 1. Training is one command, and its report is bytes

`python -m hftlab.learn.train` runs on Saturday with the market closed, and refuses otherwise unless it is a
dry run. For each configured pair and horizon it builds the dataset from the store's day folders, writes the
frozen configuration into the trial ledger, and only then evaluates it walk-forward by week: a ridge model
fitted to each fold's training rows, scored on the fold's test week, beside the oracle and a seeded coin.

- **The report** is one line of canonical JSON, integers and text only: the three numbers, the day-block
  interval, the deflated Sharpe ratio with the trials counted (each float as the text that reads back to it),
  the rows that were dropped and how many outcomes were priced late or at the worst case, the placeholder
  flags of the cost model, the feasibility settings and the learning settings, the training summary the drift
  monitor compares against, and what the trader needs to serve the model: the feature windows and the
  feasibility map of the same recording.
- **The candidate** is fitted to every development week and kept in the model registry under its hash. The
  report is kept under its own hash beside it.
- **Idempotent.** The same store, settings and ledger give the same model hash and the same report bytes.
  What differs between two runs, the time and the peak memory, is in a file of its own.
- **The hold-out.** Held-out weeks are in no fold, no fit, no summary and no feasibility map. They are read
  only by `HoldOut.read`, with `--hold-out`, which writes the read into the ledger first; a second read by
  the same configuration is refused.
- **Memory.** The run measures the most memory the process held and fails, registering nothing, above its
  budget. The dataset builder's memory is linear in its rows, and a test fails if it is not.

A candidate is registered in the lineup journal and serves nowhere.

### 2. The lineup journal

`lineup.jsonl`, in the trader's state directory, is a hash-chained journal
([`hftlab.lineup`](../../src/hftlab/lineup/journal.py)). Every line holds the whole lineup after a change: the
champion, the champions before it, the challengers, the candidates, and the action, the reasons, the decision
record and the report the change rests on. The lineup in force is the last line of a chain that verifies.

- A model is named by two hashes, of its file and of its evaluation report. The trader loads both by hash
  from the registry and refuses a file that is not what its name says. There is no name such as "latest".
- The trader's configuration names places, not models: one sleeve with `slot: champion` and any number with
  `slot: challenger-N`. A place the lineup leaves empty runs an idle sleeve.
- The trader reads the lineup when a session starts and writes what it serves into the session's first
  journal entry. The journal is one of the files a replay copies by its mark, so a recorded day replays with
  the models it ran with.
- A change of the lineup reaches a running trader as a request file, like an operator's: the session ends
  and the next one starts from the new lineup.
- **A place whose model changed starts again from the bottom of the paper ladder.** If its stage was
  `min_size` or `normal` it is stepped down, and the way up is the stage rule of
  [ADR 0019](0019-trader-loop-and-staged-rollout.md) again. A model that was only ever judged in shadow
  never inherits a stage that places orders.

### 3. The weekly decision

`python -m hftlab.learn.promote` runs on Sunday before the open. It reads the week's decision journals of the
trader, takes each sleeve's round trips in the shadow book by trading day, the reconciliation breaks and the
loss latches, and asks `hftlab.learn.promote.decide` once per challenger. Then it writes the new lineup.

- **Roll back** when the champion's own loss latch tripped in the week. The champion before it is restored
  and nothing is promoted on that decision. This needs no rule: a rule that is not in force passes nothing
  and still rolls back.
- **Promote** a challenger only when `decide` says so, and also: the rule names a decision record that
  exists in `research/decisions/` with the line `approved_by: owner`; the risk limits are in force; the week
  had no reconciliation break; the challenger's report is not marked as resting on placeholders; and the
  challenger served the same model in every session of the week. Each is looked up by the command as well
  as by the rule; none is assumed from another. With several passing, the one with the largest lower bound
  of advantage.
- **Keep** otherwise. The week's challengers leave and the candidates take their places.
- While there is no champion, a challenger is compared with the configured baseline sleeve, the control.
- **One pair, one currency.** A result is in the quote currency of its pair. The two sleeves are compared in
  the pair the challenger traded, and in nothing else: the control sleeve trades several pairs, and what it
  made in yen is never added to what it made in dollars. A challenger that traded more than one pair is
  not promoted.
- **In force means a named decision record.** A rule with a null number is no rule. A rule with every number
  written and `decision` null weighs the week with those numbers, for the report, and can pass nothing: that
  is how a registered experiment measures a week before any rule exists (`HFT-EXP-0003`).
- **A latch is the champion's own** when the risk gate named it for the champion's sleeve
  (`DAILY_LOSS_SLEEVE/<sleeve>/<currency>` or `DRAWDOWN_SLEEVE/...`) in a session that served that model.

With the shipped settings every part of the rule is null. The command says `keep: rule not in force`, and
with nothing registered it changes nothing.

Every change of the lineup is one line of the journal, with the hash of the decision report it rests on, and
one page through `hftlab.ops.alerts`.

`python -m hftlab.learn.promote --drill` proves the rollback on demand. On a copy of the journal it promotes
a challenger, forces the champion's latch through the same decision function, rolls back with the same code,
and checks that the restored champion is the one from before and can be loaded. The real journal gets a line
that says the drill ran and what it found; the lineup in force is unchanged.

### 4. Drift

`python -m hftlab.learn.drift` compares, for each ML sleeve with a model: the week's feature rows against the
bins stored at training, what the model expected of its trades against what they made, and the median spread
against the training median ([`hftlab.learn.drift`](../../src/hftlab/learn/drift.py)). Each statistic is a
fraction of whole numbers. A breach asks the trader, by a request file, to take the sleeve down to shadow, and
pages. Every threshold is null as shipped; the four are in force together or not at all, and with one null
every monitor is computed, reported and breaches nothing.

- **The rows are the trader's own week:** the quotes of the week's tapes, folded through the feature builder
  with the windows of the model's report. The heartbeats the trader makes for a quiet book are not on a
  tape, so a quiet stretch counts fewer rows than the sleeve saw.
- **The spread regime is measured against the training weeks,** whose spreads the model's outcomes were
  priced at, and not against a number of the cost model: the cost model takes the spread from the book.
- **It runs before the weekly decision,** as the first step of `hft-promote.service`. A failed look does
  not stop the decision.
- A sleeve already in `shadow` stays there; the trader journals that the request changed nothing.

So that calibration can be read from the journals, an intent may carry what the model expected of it, and the
trader journals it beside the decision. Nothing routes on it.

### 5. The scorer in its own process (decided, not built)

**Nothing of this section exists in the code.** Every ML place scores in the trader's process, and
`scorer: process` in the trader's settings is refused, so that it cannot be believed to be isolated when it
is not. The settings it will read (`scorer:` in `config/learn.yaml`) are parsed and unused. What follows is
the design it will be built to.

The trader may be configured to ask a scorer child over a pipe (`scorer: process` on an ML sleeve).

- **The message format is fixed and versioned:** one byte of version, four bytes of length, big-endian, and
  that many bytes of the request or reply line of [ADR 0022](0022-model-file-registry-and-scorer.md). A
  frame of another version or longer than the limit ends the conversation.
- **A timeout.** No reply within the configured time, a closed pipe, a malformed reply or a reply from
  another model: the sleeve abstains on that row, the child is killed, and the trader's loop goes on. The
  child is started again no sooner than a back-off that doubles to a limit. The scorer never raises into the
  trader.
- **The in-process scorer stays,** for tests and for replay. Both give the same decision on the same row,
  because the child runs the same exact arithmetic.
- **Replay.** A row the process scorer abstained on by failure is written, as it happens, into a file beside
  the session's tape. A replay scores in process and abstains on exactly those rows, so a day with a failed
  scorer still replays byte for byte.

### 6. Units

`hft-train.timer` (Saturday) and `hft-promote.timer` (Sunday before the open), rendered like the others and
enabled by nobody but the owner. Their times are values of `config/ops.yaml`: the training window must not
overlap the other desks' weekend training ([ADR 0007](0007-shared-host-isolation.md)), which only the owner
knows.

- A time is accepted only as a Saturday, or a Sunday before 17:00, in New York time. Anything else renders
  no unit.
- The install script refuses to enable either timer. The owner enables them by hand
  ([runbook](../runbooks/learning.md)).
- Training has no network and is capped at the plan's 2 GB.
- **The weekly decision may reach the internet,** to page, and loads the owner's secrets file for the alert
  address. It is the second unit that can, after the publisher. It holds no venue and cannot reach the
  gateway. The alternative, leaving a word for the publisher's job to page as the backup does, was not
  taken: a lineup change should page from the process that made it.

### 7. Status

The status screen and the snapshot read the lineup journal: the champion as the first twelve characters of
its hash, the number of challengers, and the times of the last registration, promotion and rollback. The
part is absent until training has registered something. A journal that does not verify is a bad problem
that pages.

## Consequences

**What holds, each with a test**

- The same inputs give the same model hash and report bytes (`tests/learn/test_train.py`).
- A held-out week changes nothing but the dataset's hash unless it is read through the ledger.
- No lineup is written by a rule that is not in force, except a rollback (`tests/lineup/test_decide.py`).
- A promoted champion that trips its latch is replaced by the one before it at the next decision, and the
  drill proves the same path without a loss. The cycle is run with the trader itself, on the fake broker's
  paper account (`tests/lineup/test_cycle.py`).
- A planted shift of a feature, of the spread or of what a model's expectations come to is found to the
  count, and takes the sleeve down to shadow through the trader (`tests/learn/test_drift.py`,
  `tests/lineup/test_watch.py`).

**What it costs, and what it does not cover**

- **The memory cap.** The plan caps training at 2 GB. Inside `hft.slice` the slice's own 1.2 GB binds first,
  so the unit's 2 GB is not what stops a run today. Whether training gets a slice of its own is the owner's
  decision with the host's measurements; the run's own budget is set below both.
- **The whole recording is held in memory** for a run. On a real feed that is the first limit a run will
  meet, and the measured peak of a real week does not exist yet.
- **Several challengers are several comparisons.** Each is judged alone against the champion, and the best
  passing one is promoted. The rule's confidence level is not adjusted for their number; a rule that allows
  more than one challenger should say so in its decision record.
- **The baseline.** The first challenger must beat the control sleeve, which has no edge and loses its
  costs. Beating it by the rule is a low bar, and it is why the rule also asks for a result above zero.
- **A stuck exit's worst case is a setting,** and so is the wait before it. Both are placeholders.
- **The feature shift is not the published stability index,** which needs a logarithm. It is the share of
  rows in other bins, exact, and its threshold has to be chosen for that statistic.
- **A lineup change waits for the trader to take its request.** A trader that is not running takes it at
  its next start, which reads the new lineup anyway.
- **Requests are files** ([ADR 0019](0019-trader-loop-and-staged-rollout.md)). The drift monitor and the
  weekly decision write them as the operator's command does.
- **Gradient boosting, neural networks and a deep challenger are not built.** The loop runs with the ridge
  model, which is all that exists.
- **The backup does not hold the lineup journal, the models or the reports yet.** A lost disk loses the
  lineup's history; the models are trained again from the recordings.
- **The scorer shares the trader's process.** A scorer that hangs, hangs the trader's turn. The ridge
  scorer is one exact sum, so this is a limit for the models that are not built yet.
- **None of it has run on the host,** and on real data the likeliest outcome of the first challenger's week
  is no advantage over the control sleeve after costs (`HFT-HYP-0003`).

## Addendum, 2026-10-11: a second kind of model

The training run fits the kind of model the settings name: `ridge`, as before and by default, or `gbm`
([ADR 0025](0025-gradient-boosting-optional-set-and-whole-number-trees.md)). One run fits one kind. Nothing
else of this record changes: the report, the registry, the lineup journal, the weekly decision and the drift
look do not ask what kind a model is. `last-run.json` now also says which kind the run fitted.
