# ADR 0023: The weekly learning loop as processes: training, the lineup journal, the decision, the scorer

- **Status:** accepted, 2026-10-11. Every section is built and tested on synthetic streams and against the
  fake broker only; section 5, the scorer in its own process, was built last and is off as shipped. Nothing
  has been trained on real data, no model has served, nothing has run on the host, and the gate of F6 is
  open.
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

### 5. The scorer in its own process

Built ([`hftlab.scoring`](../../src/hftlab/scoring/client.py)) and tested with real child processes on
synthetic rows. It has served no real model and has not run on the host. As shipped every place says
`scorer: in_process`; a place that says `scorer: process` is scored by a child of the trader.

- **The message format is fixed and versioned** ([`wire`](../../src/hftlab/scoring/wire.py)): one byte of
  version, four bytes of length, big-endian, and that many bytes of one line of canonical JSON, at most
  65,536. The lines are the request and the reply of [ADR 0022](0022-model-file-registry-and-scorer.md),
  unchanged, and two more: a question that asks the child to load a model by its hash, and a refusal. Whole
  numbers and text only; a line with a float in it is not a message. Nothing is pickled and nothing that
  arrives is run. A frame of another version or of an impossible length ends the conversation before a byte
  of it is read.
- **The child** (`python -m hftlab.scoring.child`) reads the model file under the hash it is asked for,
  refuses a file that is not what its name says, and answers with the hash of what it holds. It answers a row
  with the arithmetic of the in-process scorer, because it is the same code. A question it cannot answer is
  refused in a frame and the next one is read. It ends when its input ends. It is started with the place of
  the package and nothing of the trader's environment, and what it imports is the model file, the scorers and
  the wire format: no venue, no order path, no network.
- **The lineup's model or none.** At the start of a session the trader waits, for at most `start_ms`, for the
  child's answer to the load. Any answer but the lineup's hash is a refusal: the place runs idle and the owner
  is paged, as for a model file that cannot be read. A child that is merely slow is not refused; it is down.
- **A timeout.** No reply within `timeout_ms`, a closed pipe, a reply that is not one, a reply from another
  model, a refusal: the sleeve abstains on that row, the child is killed, and the loop goes on. A row is sent
  once and never asked again. The pipe is written without blocking, so a child that does not read cannot hold
  the loop either.
- **A restart with back-off.** A failed child is started again no sooner than a wait that doubles from
  `restart_first_ms` to `restart_last_ms`, and is the first wait again once a row was scored. While the child
  is down or starting, a row is abstained on at once: nothing in the loop waits for a start. A restarted
  child that answers the load with another model is refused for the rest of the session. The back-off and the
  time a start may take are measured on the session's clock, which is injected.
- **It never raises into the trader.** Whatever a child does ends in an abstention. A row that cannot be
  written as one message is abstained on too, and the child is not blamed for it.
- **A clean stop.** When a session ends the child's input is closed, which is how it is told; one that does
  not leave is killed. A child whose trader dies sees the same end of input and leaves.
- **The in-process scorer stays,** for tests and for replay. Both give the same decision on the same row.
- **Replay never needs the child.** A row the scorer process did not score is written, as it happens, into a
  hash-chained file beside the session's tape (`session-NNN.abstained.jsonl`,
  [`record`](../../src/hftlab/scoring/record.py)), and then into the decision journal as an `abstained`
  line with the reason. A replay scores in process, abstains on exactly those rows and writes the same
  lines, so a day with a failed scorer replays byte for byte. A record that does not verify is not replayed
  from. When a child came back is not journalled: it is not a function of the tape. The owner is paged when
  a scorer stops answering and when it answers again.
- **One thread decides.** Each child has a second thread that moves whole frames from the child's output
  into a queue and does nothing else. The one real wait in the loop is the wait for a reply, bounded by the
  timeout; a hung child costs one timeout and then nothing until its back-off is over.

What changed from the design as first written: the load is a message and not an argument; a start has its
own time, `start_ms`, beside the three settings the design named; and the file beside the tape is a
hash-chained journal with the reasons in it, mirrored in the decision journal.

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
- A scorer process gives the in-process decision on every one of thousands of rows, for a ridge model and
  for a model of boosted trees written by hand (`tests/scoring/test_client.py`), and a day scored in a
  process is byte for byte the day scored in the trader (`tests/scoring/test_session.py`).
- A hung child and a killed child each make their sleeve abstain while the trader takes its turns and the
  other sleeves trade; a new child answers after the back-off; and the day replays byte for byte with no
  child (`tests/scoring/test_session.py`).
- A child that holds another model is refused; a frame or a line that is not one is refused without either
  side failing (`tests/scoring/test_child.py`, `tests/scoring/test_wire.py`).
- The child can reach no venue, no order path and no network, by a scan of everything it imports and by
  starting one and looking (`tests/scoring/test_structure.py`).

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
- **As shipped the scorer shares the trader's process.** Every ML place says `scorer: in_process`, where a
  scorer that hangs, hangs the trader's turn. The two kinds of model there are score with exact sums, so this
  is a limit for models that are not built yet; `scorer: process` is there for them.
- **A scorer process costs a timeout when it fails.** The loop waits up to `timeout_ms` once for a child that
  hangs, and again for each restarted child that hangs on its first row, with the back-off between them.
  Whether 20 ms is the right wait is a setting nobody has measured on the host.
- **A child that is slow and right is treated as wrong.** A reply after the timeout is thrown away with the
  child. Under load on a shared host that can happen, and each time it costs the rows of a back-off.
- **Rows are lost while a child starts.** A start takes tens of milliseconds, and every row of that time is
  an abstention. They are in the record, so the day still replays.
- **An outage writes a line a row,** into the record and into the decision journal, for as long as it lasts.
  A place that is refused for the rest of a session writes one for every row its sleeve would have asked
  about.
- **None of it has run on the host,** and on real data the likeliest outcome of the first challenger's week
  is no advantage over the control sleeve after costs (`HFT-HYP-0003`).

## Addendum, 2026-10-11: a second kind of model

The training run fits the kind of model the settings name: `ridge`, as before and by default, or `gbm`
([ADR 0025](0025-gradient-boosting-optional-set-and-whole-number-trees.md)). One run fits one kind. Nothing
else of this record changes: the report, the registry, the lineup journal, the weekly decision and the drift
look do not ask what kind a model is. `last-run.json` now also says which kind the run fitted.

## Addendum, 2026-10-12: three of the consequences above no longer hold

- **"Gradient boosting, neural networks and a deep challenger are not built."** Gradient-boosted trees are a
  second kind of model ([ADR 0025](0025-gradient-boosting-optional-set-and-whole-number-trees.md)) and a small
  sequence network, the plan's deep challenger, a third
  ([ADR 0029](0029-sequence-network-windows-and-fixed-point-scoring.md)). The loop fits whichever one the
  settings name, and nothing else of it asks what kind a model is. All three have been fitted to synthetic
  rows only.
- **"The backup does not hold the lineup journal, the models or the reports yet."** It does: the backup
  takes the trader's folder and the learning folder whole, so the lineup journal, the model files and the
  evaluation reports are in it ([ADR 0027](0027-deploy-gate-and-reports.md), section 1). No backup has been
  made on a host and no restore of a lineup has been drilled.
- **"The two kinds of model there are score with exact sums, so this is a limit for models that are not built
  yet."** There are three kinds now, and all three are scored with whole numbers: exact fractions, sums of
  leaves, integer sums and shifts. As shipped every place still scores in the trader's process, and
  `scorer: process` is still off.

The shipped trader settings hold a champion's place and three challengers', as many as the registered shadow
weeks name. Nothing here has run on a host.
