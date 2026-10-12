# Roadmap

Eight phases. A phase is finished when its exit gate is met, not when its code is merged. The table is the
approved plan, unchanged.

| Phase | Deliverable | Exit gate |
|---|---|---|
| F0 Foundation | Repo, governance, leak mechanism, core types, paper lock, registry, ADRs, requirements from the knowledge base | CI green with `test` and `leakcheck` required |
| F1 Broker probe and adapter | IBKR adapter against the fake broker; a probe the owner runs once with the paper login | Probe report: tradable pairs and form (spot or CFD), update rate, depth, minimum size, commissions, order round trip, weekly-login behaviour; decision records for the access route and instrument form |
| F2 Recorder | Recording 24 hours on weekdays | five days; every gap accounted for; replay parity; memory and disk inside budget |
| F3 Shadow venue and control | Cost model, shadow book, control sleeve in shadow, feasibility map | a flat strategy earns 0; the random control loses about the modelled cost |
| F4 Paper trading | Order manager, reconciliation, control sleeve placing minimum-size paper orders | five days; zero reconciliation breaks; fault-injection suite green; shadow-versus-broker slippage reported |
| F5 Strategies | Directional Change and ML sleeves trading on paper automatically | pre-registered; staged rollout completed or stepped down by its own rules |
| F6 Learning | Weekend training, challengers, automatic promotion and rollback, DL challenger | two full weekly cycles without manual help; one forced rollback drill passes |
| F7 Visibility and scale | HFT section on the status site (after its refactor); more pairs; second venue for depth | — |

Recording starts as early as possible (F2), because data only accumulates with time. Real money,
co-location and anything on the [never-build list](never-build.md) are out of scope.

One line of the plan has been overtaken. F7 speaks of an HFT section on the status site "after its refactor".
The site already has a page for this desk, which says that nothing has been published yet, and the publisher
on this side is written ([ADR 0018](adr/0018-snapshot-publish.md)). What is left of that line is the first
send, which waits for the owner to give the site this desk's key, under a key id of its own and scoped to this
desk, and for the site to accept this desk's schema, and
then the parts of the snapshot that later phases make real. More pairs and a second venue are unchanged.

## Where things stand

**The gates of F1, F2, F3 and F4 are all open.** The code of the first four phases is written and passes its
tests against a deterministic fake broker and against synthetic streams and recordings the tests make
themselves. That is all it has been run against:

- The capability probe has run twice against the real paper gateway. Read-only on 2026-10-11, in the minutes
  after the weekly open (`HFT-EXP-0007`): EUR.USD, USD.JPY, AUD.USD and GBP.USD each resolve as spot and as a
  CFD, with five levels of market depth and historical ticks. With four one-unit test orders on 2026-10-12, in
  the Asian session, from the owner's laptop (`HFT-EXP-0008`): the top of the book changes about one to three
  times a second, not every few milliseconds, and **the broker rejected all four orders**. On this account
  spot currency cannot be bought without holding the currency that pays, cannot be shorted, and yen cannot be
  held at all. **Commissions, the SELL side, CFDs and the host's order timing are not measured**, and the
  spreads of neither run are inputs to the cost model. Those two runs are all that has touched anything
  real, and F1 is still open. Nothing else has connected to a gateway.
- **Nothing has traded.** No order has been accepted on the paper account: the probe's four were rejected. The process that would is written,
  and as shipped it cannot: the risk limits are not in force, so it runs in shadow-only mode, and every
  sleeve's stage is `shadow` with no promotion rule that could be met.
- Nothing has been recorded from the real feed. There is no recorded trading day.
- No history has been imported either. The import of tick files the owner exports himself is written
  ([ADR 0028](adr/0028-imported-history.md)) and has read hand-written rows in tests only. Imported days are
  for fitting a model before enough recordings exist: they are never this venue's results, and they count
  towards no promotion.
- No experiment on a strategy has been run on real data. The research registry holds 6 hypotheses and 8
  experiments, and 0 decision records: 6 of the experiments are registered and not run, and 2 are records
  of the capability probe's two measured runs (`HFT-EXP-0007`, `HFT-EXP-0008`), which test nothing and count as
  no trial.
- Every number in the cost model, the recorder's quota and the feasibility settings is a placeholder.
- **F5 has not started as a phase, and F6 is open.** F6's weekly loop is written and tested on synthetic
  streams and against the fake broker only (see "Where F5 and F6 stand"). Its gate is not met and code
  cannot meet it. No model has been trained on real data, and nothing in this repository is evidence of an
  edge.
- **The pieces written after the first loop are in the same state as the rest:** the scorer in a process of
  its own ([ADR 0023](adr/0023-weekly-learning-loop.md), section 5), sleeve state kept across a restart, and the
  two commands the registered experiments name ([ADR 0026](adr/0026-sleeve-state-across-a-restart.md)). Each
  is written and tested on synthetic data. None has run on a host, and none has seen real data.

| Gate | State | What closes it |
|---|---|---|
| F1 | Open | **Measured now**, by two runs of the capability probe against the real paper account ([`HFT-EXP-0007`](../research/experiments/HFT-EXP-0007-capability-probe-weekly-open/report.md), read-only, at the weekly open; [`HFT-EXP-0008`](../research/experiments/HFT-EXP-0008-capability-probe-orders-asian-session/report.md), with four test orders, in the Asian session, from the owner's laptop): the four pairs resolve as spot and as a CFD; five levels of depth and historical ticks; the update rate of an ordinary session, about one to three changes a second per pair; median spreads of 0.2 to 0.4 pip in one thirty-second sample; the size below which the venue routes an order as an odd lot (20,000 for EUR and GBP base, 25,000 for USD and AUD base); and what the account does with a spot BUY from flat, which is to refuse it: no paying currency and no borrowing, and no yen at all. **What still blocks the gate, all of it the owner's** ([runbook](runbooks/ibkr-probe.md)): (1) the owner's choice of how a long-and-short sleeve could trade here at all: cash balances held in each currency, or CFDs, which need a permission that belongs to the owner's real account (`HFT-EXP-0008`, finding b). Nothing is built for either; (2) the decision record on the instrument form and (3) the decision record on the access route, both of which now depend on that choice; (4) a probe run with `--orders` **on the host**, with an order the broker accepts, for the order round trip: the one timing taken is from a laptop in Sydney, of rejected orders; (5) a commission, from a fill, which needs an order the broker accepts; (6) each recorded as a new experiment, and the capability file filled in from them. Not part of this gate: the weekly login, which a week of watching shows; the SELL side and the busiest hours are worth a run and block nothing by themselves |
| F2 | Open | Five recorded trading days with every gap accounted for. Code cannot close it: the days have to pass |
| F3 | Open | The registered control run (`HFT-EXP-0001`) on those five days, and the owner's decision on it. Code cannot close it either |
| F4 | Open | The capability probe of F1, risk limits put in force by a decision record, the owner's decision to let the control sleeve place minimum-size paper orders, then the registered run `HFT-EXP-0002`: five trading days on the real paper account with zero reconciliation breaks and the slippage report. Code cannot close it: the days have to pass |

| F6 | Open | Recorded weeks to train on, the owner's two times for the weekend jobs and their timers enabled, then two full weekly cycles on the host that needed no manual help ([`HFT-EXP-0003`](../research/experiments/HFT-EXP-0003-first-ridge-challenger-shadow-week/protocol.md) is the first, registered and not run) and one rollback drill there ([runbook](runbooks/learning.md)). The deep challenger of the plan's line is written, as a small sequence network, and has been fitted to synthetic rows only. Code cannot close it: the weeks have to pass |

## Where F5 and F6 stand

**F5: foundations and one baseline sleeve, in shadow. F6: the weekly loop is written, and its gate is open.**
What follows needs one dependency, which only training a gradient-boosted model installs; the sequence network needs none. It has run on synthetic streams the tests make themselves, some with an
effect planted in them on purpose so that the answer is known, and against the fake broker. **There is no
real data, no model has been trained on real data, and nothing here is evidence of an edge.** In
`config/ops.yaml` F5 stays `not_started` and F6 is `open`: its code is written and its gate is not met.

| Part | State |
|---|---|
| Features | Written: `src/hftlab/features/` ([ADR 0020](adr/0020-features-and-labels-timing.md)). One step serves a recording and a live stream; every value is an integer; a crossed, unconfirmed or shut book gives no row. Slow for long windows on a fast feed, by a known limit of the implementation. **Never computed on a recorded day** |
| Labels | Written: `src/hftlab/labels/`. The outcome of each side priced with the cost model as the shadow venue prices, and triple-barrier outcomes, each with the time its window ends. They assume the cost model's largest latency. **Every one is priced on placeholder costs** until the cost model is measured |
| Datasets | Written: `src/hftlab/learn/dataset.py`. Built per pair and horizon from day folders of the store; the builder refuses a feature that reads ahead, by a check that looks at up to sixteen places of the stream and is a test, not a proof. **No dataset of real data exists**, and none is ever committed |
| Validation | Written: `src/hftlab/learn/validate.py` ([ADR 0021](adr/0021-validation-splits-trials-and-the-three-numbers.md)). Walk-forward by week with purge and embargo, a hold-out read once per frozen configuration, a trial ledger beside the signed registry, a day-block bootstrap, the deflated Sharpe ratio, and oracle, model and random on the same rows. **Nothing has been validated**: `config/trial_registry.yaml` is still empty |
| Models | Written: `src/hftlab/models/` ([ADR 0022](adr/0022-model-file-registry-and-scorer.md)). A model file named by its hash and never overwritten, ridge regression on standardised features solved exactly, the flat and the seeded random baseline, and a scorer that runs in the caller's process. **No model ships and none has been fitted to real data.** A small sequence network is a third kind (see "Sequence network" below) |
| Gradient boosting | Written: `src/hftlab/learn/boost.py`, `src/hftlab/models/trees.py` ([ADR 0025](adr/0025-gradient-boosting-optional-set-and-whole-number-trees.md)). A second kind of model, chosen by `model: gbm` in `config/learn.yaml`; ridge is the default. It is trained with LightGBM, pinned in an optional requirement set of its own (`make ml`, `hft-install --with-ml`; about 80 MB) that the recorder and the trader never install or import. The fitted trees are exported as whole numbers and scored by the platform's own arithmetic, so a decision does not depend on the library or on a float. Small, single-threaded, deterministic, fixed settings and no search. On synthetic streams it finds a planted band that ridge cannot and makes nothing of noise after costs. **Its settings are placeholders, it has been fitted to synthetic rows only, and the tests that fit ran on macOS only**: the `ml` job in CI is their first run on Linux. A test there compares one tiny fit's file with the hash it has on macOS (`tests/learn/test_portable.py`); the record does not promise they agree, and no Linux result is written down yet |
| ML sleeve | A skeleton: `src/hftlab/strategy/ml.py`. It trades a cell the feasibility map marks viable when the model expects more than its margin, leaves at the label's horizon, and reports that it may not leave shadow while its model or its map rests on placeholder costs. In the trader, on a synthetic stream with a planted effect, it earns what the effect is worth and replays byte for byte; without the effect it pays the control sleeve's cost. **That checks the plumbing and says nothing about any market.** The shipped trader settings name four places for it, a champion's and three challengers' (as many as the registered shadow weeks name), and all four run idle: no model is registered |
| Lineup | Written: `src/hftlab/lineup/` ([ADR 0023](adr/0023-weekly-learning-loop.md)). A hash-chained journal says which model stands in which place. The trader loads a model and its report by hash and by nothing else, journals what each session served, and steps a place down when its model changes. A journal that does not verify serves nothing. **No lineup journal exists on any host** |
| Weekend training | Written: `python -m hftlab.learn.train`. A walk-forward report as bytes, a candidate of the configured kind in the registry under its hash, a memory budget that fails the run. **It has trained on synthetic weeks only** |
| Directional Change sleeve | Written: `src/hftlab/strategy/dc/` ([ADR 0024](adr/0024-directional-change-sleeve.md)). A detector that books each event at the tick that crossed and keeps the provisional and the completed overshoot apart as two types; a rule-based baseline sleeve in two variants, trend and contrarian, that refuses a threshold small against the spread; and a random-walk null of matched volatility. Tested on synthetic streams: a zig-zag worked out by hand, driftless walks on which it pays the control sleeve's cost, a planted pattern against its null. **It has seen no real data and is not a claimed edge.** Configured as `directional_change` with every number a placeholder, at stage `shadow`. Its first shadow run is registered as `HFT-EXP-0005`, expected to show no advantage, **not run**. The command that runs the null on a recorded day is written (`python -m hftlab.review.dc_null`, [ADR 0026](adr/0026-sleeve-state-across-a-restart.md)), and so is carrying a sleeve's state across sessions; both are tested on synthetic days only. The protocol of `HFT-EXP-0005` still says the command is not built: a registered experiment is frozen |
| Imported history | Written: `python -m hftlab.history import` ([ADR 0028](adr/0028-imported-history.md), [runbook](runbooks/history-import.md)). It reads tick files the owner exported himself from a folder he describes, and writes day folders in the recording format into a store of its own; it downloads nothing. Every imported day says `source: imported`, the owner's label, the SHA-256 of its input files and `terms_verified: false`; the recorder refuses that store and the importer refuses the recorder's. Datasets, evaluation reports, the feasibility map and the weekly scorecard say how many rows and days were recorded and how many imported, by label, and the weekly rule counts no imported day: a model fitted to imports alone is kept with `too_few_own_days`. A file without sizes gets a size of one unit, which no order fills against, and a run that read one is not a result. **It has read hand-written rows in tests only: no exported file has been imported, nobody has verified any vendor's terms, and no model has been fitted to an imported day.** The model file says the split itself, under its own hash (version 2 of the file), and the weekly decision counts a day only where the file and the report agree; a file of the older version still loads and says its split is unknown |
| Promotion and rollback | Written: `python -m hftlab.learn.promote`. It reads the week's shadow round trips from the trader's journals, applies the rule through the pure function, and writes the lineup: promote, keep or roll back, with a page and the hash of the report it rests on. A promotion needs the rule in force, its decision record with the owner's approval, the risk limits in force and a week with no reconciliation break. A champion that trips its loss latch is rolled back without a rule; `--drill` proves that path on a copy. **As shipped it says `keep: rule not in force`: no rule exists.** The cycle has run with the trader on the fake broker only |
| Drift | Written: `python -m hftlab.learn.drift`. Feature shift, calibration and the spread regime against the summary kept at training; a breach asks the trader to take the sleeve down to shadow. **Every threshold is null**, so it reports and steps nothing down |
| Units, status, runbook | `hft-train` and `hft-promote` are rendered, at placeholder times that are the owner's to set, and the install script enables neither. The status screen and the snapshot show the lineup once something is registered. [Runbook](runbooks/learning.md). **Nothing is installed, and the page has not been followed on the host** |
| Sleeve state across a restart | Written: `src/hftlab/trader/resume.py` and each sleeve's `save` and `restore` ([ADR 0026](adr/0026-sleeve-state-across-a-restart.md)). A session that ends on purpose writes the control, the ML and the Directional Change sleeve's state into a hash-chained file, tied to where its journals stood and to how its tape ended. The next start restores a sleeve only when nothing moved since, the sleeve had no order on its way, and its stage and settings are the same; otherwise it starts cold, warm-up and all, and the reason is journalled and paged. A restored sleeve keeps what it holds; a cold one has it closed by the trader, as before. Tested by cutting synthetic sessions at many points against the uncut run, and with stale, broken and tampered files. **No state has been saved or restored on the host** |
| Reading a run back | Written: `src/hftlab/review/` ([ADR 0026](adr/0026-sleeve-state-across-a-restart.md)). Round trips out of a shadow journal; each round trip's result against the cost model's figure for the quotes at its two ends, with the count, the mean and the variance exact and the registered verdict decided without a root (`HFT-EXP-0001`); and a Directional Change sleeve's day beside N seeded walks of matched per-tick variance, with the share of the null at or above it as an exact fraction (`HFT-EXP-0005`). Tested on synthetic recordings with answers worked out by hand. **Neither has read a recording of the real feed, and the running time of the null on a real day is not measured** |
| Scorer in its own process | Written: `src/hftlab/scoring/` ([ADR 0023](adr/0023-weekly-learning-loop.md), section 5). A place whose settings say `scorer: process` is scored by a child the trader talks to over a pipe, in a fixed, versioned message format of whole numbers and text. A child that hangs or dies costs its sleeve the rows until a new one answers, after a back-off; the loop waits at most one timeout and the other sleeves trade on. A child that holds another model than the lineup's is refused. The rows a child did not score are recorded beside the tape, so a day replays byte for byte without any child. Tested with real child processes, hung and killed, on synthetic rows and on the fake broker's market. **As shipped every place scores in the trader's process, and no scorer process has run on the host or served a real model** |
| Sequence network (the DL challenger) | Written: `src/hftlab/learn/sequence.py`, `src/hftlab/models/net.py`, `src/hftlab/features/window.py` ([ADR 0029](adr/0029-sequence-network-windows-and-fixed-point-scoring.md)). A third kind of model, chosen by `model: seq` in `config/learn.yaml`; ridge is the default. One small 1-D convolution and one hidden layer over a window of the last rows of a pair: about a thousand weights at the shipped placeholders. It is fitted on the array library the base requirements already hold, with the network, its gradients and its optimiser written out and checked against differences: **nothing is installed for it**. The fitted weights are exported as whole numbers at a fixed scale and scored by integer sums and shifts; the scorer works out from the file how far that can be from the fitted network and abstains inside that bound. A window never spans a gap, a closed market, a reconnect or a day's rollover, the dataset builder refuses a window that is not exactly the rows it claims, and the purge goes by a window's earliest row. It trades through the ML sleeve every model has, starts as a challenger in shadow, and is promoted by the same rule or not at all. On a synthetic stream it finds a planted order of rows that no model of one row can, and makes nothing of noise. **Its settings are placeholders, it has been fitted to synthetic rows only, every test that fits ran on macOS only, and that two machines write the same file is argued in the record and not yet shown.** A test now asks: one tiny fit of each kind against the hashes it has on macOS (`tests/learn/test_portable.py`), which CI runs on Linux. No result of it on Linux is written down yet |
| Pre-registration | `HFT-HYP-0003` and `HFT-EXP-0003` register the first ridge challenger's week in shadow. The expected outcome is no advantage over the control sleeve after costs. `HFT-HYP-0004` and `HFT-EXP-0004` register the first gradient-boosted challenger's week, beside a ridge challenger; the expected outcome is no advantage over the control sleeve or the ridge challenger. `HFT-HYP-0006` and `HFT-EXP-0006` register the first sequence-network challenger's week, beside a ridge and a tree challenger; the expected outcome is no advantage over any of the three. **Not run. No decision record exists and no promotion rule is registered**; a rule that names no decision record passes nothing |
| Exit gates of F5 and F6 | F5: not started. It needs sleeves trading on paper under a pre-registered rollout. F6: open. It needs two weekly cycles without manual help and a rollback drill, on the host. Both need recorded data first, so both wait behind F2 |

## Where F4 stands

F4's code is complete against the fake broker: everything between a sleeve's intent and the broker, and the
process that wires it together. It has run against the fake broker and the shadow venue, and against nothing
else. Its gate is open and needs five days on the real paper account.

| Part of the deliverable | State |
|---|---|
| Risk gate | Written: `src/hftlab/risk/` ([ADR 0015](adr/0015-risk-gate.md)). Every check has a code from a closed list and its own tests. An order that only reduces a position has its own, wider collar, and a restart no longer empties the rate windows or the count of rejects. **Every limit in `config/risk.yaml` is a null placeholder and the file is not in force**: no gate, and so no order manager, can be built from it until a decision record sets the values. Gross exposure and leverage are not enforced: they need a currency conversion, and nothing does one |
| Order manager | Written: `src/hftlab/exec/` ([ADR 0016](adr/0016-order-manager-and-outbox.md)). Idempotent ids, an explicit state machine, a write-ahead outbox, recovery by client order id, cancel and reconcile on every reconnect. It is the only caller of `place` and `cancel` on a venue the platform trades on, and a scan holds that |
| Broker book and reconciliation | Written: the same `Book` class, fed by the venue's fills; `src/hftlab/book/reconcile.py` compares the venue's positions and executions with it, books what was missed once, and flags a break that clears only when the books agree again |
| Fault-injection suite | Green against the fake broker (`tests/faults/`): a kill at every step of the outbox, and of the whole trader (its tape, both books' journals, every send), a disconnect mid-order, duplicated and reordered reports, partial fills, rejects, a gateway restart, a clock jump, stale quotes, the weekend gap, a rollover blow-out, an outbox that cannot be written, and generated sessions that hold the accounting invariants. What only a real gateway does is not in it |
| Trader process | Written: `python -m hftlab.trader` ([ADR 0019](adr/0019-trader-loop-and-staged-rollout.md)). One loop from the venue's events through the sleeves, the gate and the order manager to two books; it reads the real `KILL` and `FLATTEN` files, records what it saw on a tape, and a recorded day replays to the same decision journal byte for byte. **With the shipped configuration it runs in shadow-only mode and holds nothing that could send an order.** Run so far against the fake broker only |
| Staged rollout | Written: `shadow`, `min_size`, `normal`, `stepped_down`, `off`, in a hash-chained journal. A sleeve goes up only by its rule (clean trading days, the decision record that approved the step, limits in force) and comes down at once on a latch or a reconciliation break. **Every rule in `config/trader.yaml` is null**, and a null rule is never met |
| Control sleeve placing minimum-size paper orders | **Not started on the paper account.** Against the fake broker it does. On the real one it needs the probe of F1, limits in force by a decision record, and the owner's decision record for the step to `min_size`. The run is pre-registered as `HFT-EXP-0002`, **not run** |
| Shadow-versus-broker slippage report | Written: per sleeve and pair, in exact price increments, at the end of every session (`src/hftlab/trader/slippage.py`). **No report of real fills exists** |
| Alerts for a latch, a reconciliation break and a change of stage | Written, through the host layer's pager. **No page has been sent** |
| Unit, status and runbook | `hft-trader.service` is rendered and committed; it has no install section and cannot be enabled. The status screen and the snapshot show sleeves and books once a trader has run. [Runbook](runbooks/trader.md), not yet followed on the host |
| Exit gate: five days, zero reconciliation breaks, fault-injection suite green, slippage reported | Open. Only the third part can be met by code, and it is met against the fake broker only. Nothing here has seen the paper account |

## Where F3 stands

| Part of the deliverable | State |
|---|---|
| Cost model | Written: `src/hftlab/venues/sim/cost.py`, `config/cost_model.yaml` ([ADR 0013](adr/0013-shadow-cost-model.md)). **Every number is a placeholder** until the probe has measured latency, minimum size and commission |
| Shadow venue | Written: `src/hftlab/venues/sim/`. Fills against quotes it is fed, by the rules of ADR 0013. Tested case by case and against the parts of the venue contract suite that apply to it |
| Shadow book | Written: `src/hftlab/book/`. Positions, cash and profit in exact integers, from fills only. It converts no currency: a fee in a currency other than the pair's quote currency is reported beside the net result, not in it |
| Sleeves | Written: the interface, the idle sleeve and the seeded control sleeve, `src/hftlab/strategy/` ([ADR 0014](adr/0014-sleeves-as-pure-functions.md)) |
| Control sleeve in shadow | Written: `python -m hftlab.shadow`, with a hash-chained journal. It can be handed no venue but the shadow venue it builds. Run so far only on synthetic streams and on recordings of the fake broker |
| Feasibility map | Written: `python -m hftlab.feasibility` over a store of recorded days. Tested on streams worked out by hand. **No map of real data exists**, and a map on placeholder costs says in itself that it is not a result |
| Pre-registration | Done: `HFT-HYP-0001` and `HFT-EXP-0001` in [`research/`](../research/README.md), with seed, entry rate and times fixed. **Not run** |
| Exit gate: a flat strategy earns 0 | Holds exactly on synthetic streams (`tests/shadow/test_gate.py`). Open on recorded data |
| Exit gate: the random control loses about the modelled cost | On synthetic streams: exact on a still book, within 3% in the mean over 30 seeds on a walk. Open on recorded data |

The tool that compares the control run trip by trip with the recording, which the registered experiment
names, is written: `python -m hftlab.review.trip_costs`
([ADR 0026](adr/0026-sleeve-state-across-a-restart.md)). It is tested on books worked out by hand and has
read no real recording. The protocol of `HFT-EXP-0001` still says the tool is not built: a registered
experiment is frozen, so the note stays as it was registered. The
[runbook for a shadow run](runbooks/shadow-run.md) is written and has not been followed on the host.

## Where F2 stands

| Part of the deliverable | State |
|---|---|
| Calendar | Written: the forex week by New York time, the rollover, dated holidays and macro events, `src/hftlab/calendar/`. **Both dated lists are empty**: no holiday and no event has been entered. A calendar directory or file that is missing is an error, not an empty calendar |
| Recording format and store | Written: `src/hftlab/record/` ([ADR 0012](adr/0012-recording-format.md)). Tested with a kill at every byte of a block, restarts, a full quota and an offload that does not confirm |
| Recorder process | Written: `python -m hftlab.record`, on a market-data view that has no order methods. Run so far only against the fake broker (`--dry-run-seconds`) |
| Replay and the day check | Written: `python -m hftlab.replay --day DIR --check`. Replay parity is tested on recordings of the fake broker |
| Recording 24 hours on weekdays | **Not started.** It needs three things the owner does, in this order: the capability probe, the host's pre-flight day of measurements ([ADR 0007](adr/0007-shared-host-isolation.md)), and the install. The probe has run once, read-only, at the weekly open, which does not close F1; the other two have not been run. What they need from this repository is written: see "Where the host layer stands" |
| Exit gate: five days, every gap accounted for, replay parity, memory and disk inside budget | Open. The quota in `config/recorder.yaml` is a placeholder until the host's disk budget is measured |

## Where the host layer stands

What F2 needs in order to run on the shared host ([ADR 0017](adr/0017-host-layer.md)). **All of it is written
and tested off the host. None of it is installed, and none of it has run on any host.**

| Part | State |
|---|---|
| Units | Rendered from code (`python -m hftlab.ops.units`) and committed in `deploy/systemd/`: the slice with its 1.2 GB cap, the recorder, housekeeping (the recording's, then the trader's tapes), the shadow runner, the trader (no install section: started by the owner, never enabled), the publisher, the backup, the report with its timer (the owner's to enable; the install script refuses), and a placeholder for the gateway that cannot be enabled. Tested as text. **Never loaded by a service manager** |
| Pre-flight gate | Written: a shell sampler for the host and `python -m hftlab.ops.preflight --check`, with thresholds in `config/preflight.json`. Tested on made-up samples. **No day has been measured on the host** |
| Install | Written: `deploy/bin/hft-install`, which refuses without a passing pre-flight and enables nothing unasked. Tested in dry runs against a temporary directory. **Never run for real** |
| Status and the dead-man rule | Written: `python -m hftlab.ops.status`, tested against the files a real recorder writes over the fake broker |
| Alerts | Written: once per problem, keys prefixed `hft:`. **No page has been sent**: the address is the owner's to configure |
| Status snapshot | Written: `python -m hftlab.publish` ([ADR 0018](adr/0018-snapshot-publish.md)). **Nothing has been sent.** How it signs is settled: a key and a key id of its own, which the site holds scoped to this desk, the site configured before the publisher ([runbook](runbooks/publish.md)). Setting that up is the owner's, and no send has been made |
| Backup | Written: `python -m hftlab.ops.backup`, into a directory or to a tool; it never deletes. It takes the trader's folder and the learning folder whole (the order outbox, both books' journals, the stage journal, the lineup journal, the model registry) and names what it leaves out, each with its reason; the tapes are left out although they cannot be rebuilt, and say so. Tested against a real trader and a real training run on synthetic data. **No backup directory exists, and no restore has been drilled** |
| The trader's tape | Written ([ADR 0027](adr/0027-deploy-gate-and-reports.md)): cut into parts by UTC day and by size, closed parts compressed by the housekeeping unit, all tapes held under a quota, the part being written synced on every beat. The quota no longer stops the trader with a position open (the record's addendum): at a soft limit it opens nothing, at a hard limit it closes everything by the `FLATTEN` path into a reserve sized from the risk limits, and it stays up, paging, while anything is still open. A day replays byte for byte across a cut, and a kill at the cut loses and duplicates nothing, against the fake broker. **The quota is a placeholder: the host's disk has not been measured with a trader running** |
| Reports | Written: `python -m hftlab.ops.report`, a daily summary after the 17:00 New York rollover and a weekly scorecard after the Friday close, from the journals alone, in exact integers, with every missing part said to be absent. Tested on synthetic journals with numbers worked out by hand. **No report has been made from a real journal, none has been sent, and the timer is enabled nowhere** |
| Deploy gate | Written: `python -m hftlab.ops.deploy`. It refuses unless the commit is on `main` with green checks, every sleeve is flat in the broker book, the trader is stopped or the market shut, and the recorder is not restarted at the rollover; it swaps a link between whole checkouts, restarts only the recorder and the shadow runner, never the trader, and goes back by itself when the status check fails. `prune` removes old checkouts and the environments nothing links to, never the one in service, the one a step back goes to, or one a running unit still runs in. Tested on temporary folders with a stand-in service manager, a kill at every step included. **No deploy has been made with it. The layout it needs has been adopted on no host, and the look-up of the checks, the environment build and the status check have not been run for real** |
| Recorder hardening | Done: a write the system refuses stops the recorder by name instead of crashing it, and the loop's time away from the feed is in the status file ([ADR 0012](adr/0012-recording-format.md), addendum) |
| Runbooks | Written for the owner: [install](runbooks/install.md), [recorder](runbooks/recorder.md), [shadow run](runbooks/shadow-run.md), [weekly login](runbooks/weekly-login.md), [gateway down](runbooks/gateway-down.md), [switches](runbooks/kill-and-flatten.md), [trader](runbooks/trader.md), [backup](runbooks/backup.md), [deploy](runbooks/deploy.md), [rollback](runbooks/rollback.md), [reports](runbooks/reports.md), [publish](runbooks/publish.md). **None has been followed on the host** |

Every number in the units that is not the plan's (the per-service memory fuses, the weights, the restart
waits, the timer times) is a first choice, marked as one where it is written.

## Where F1 stands

| Part of the deliverable | State |
|---|---|
| Venue port | Done: `src/hftlab/venues/port.py`, with the shared guard ([ADR 0009](adr/0009-venue-port-guard-and-fake-broker.md), [ADR 0011](adr/0011-synchronous-pumped-port.md)) |
| Paper lock in stages | Done: before connecting, after the handshake, before every order; a reconnect starts unchecked |
| IBKR adapter against the fake broker | Done: `src/hftlab/venues/ibkr/` over `ib_async`, `src/hftlab/venues/fake/`, and a contract suite both pass. Written and tested without a gateway |
| The probe | Written and tested against the fake broker, and run twice against the real paper gateway: read-only at the weekly open ([`HFT-EXP-0007`](../research/experiments/HFT-EXP-0007-capability-probe-weekly-open/report.md)), and with orders in the Asian session ([`HFT-EXP-0008`](../research/experiments/HFT-EXP-0008-capability-probe-orders-asian-session/report.md)). It says when the gateway is cut off from the broker and when the other login's session has taken the market data, each with an exit status of its own, and never overwrites a report. Since the second run it waits for the broker's own verdict on an order and reports a rejection by number and class, and says per side which pairs the account may open a position in. **It has not been run on the host, in the busiest hours, or with an order the broker accepted** ([runbook](runbooks/ibkr-probe.md)) |
| Exit gate: the probe report | Open. Two reports exist. Measured: what resolves, depth, history, the update rate and spreads of one ordinary session, the venue's odd-lot size, and that the account refuses a spot BUY from flat. Not measured: commissions, the SELL side, a position funded with cash in the right currency, CFDs, and any order timing from the host |
| Exit gate: decision records for the access route and the instrument form | Open, and **not written on purpose**: both now depend on the owner's choice between cash balances held in each currency and CFDs, which is the owner's alone. [ADR 0010](adr/0010-access-route.md) is the engineering default until then |

F1 is not finished until the last two rows are. Everything the adapter assumes about the gateway and could
not check offline is listed at the end of the runbook.

## Where F0 stands

| Part of the deliverable | State |
|---|---|
| Repo, governance | Done |
| Leak mechanism | Done: `tools/leakcheck.py`, hooks, attestation ([ADR 0006](adr/0006-public-repo-and-leak-mechanism.md)) |
| Core types | Done: `src/hftlab/core/` |
| Paper lock | Done as a checked function with its isolation tests. The adapter that calls it is F1 |
| Registry | Done: lint and trial count. The first two records came with F3, the next two with F4, and the learning loop, the Directional Change sleeve and the two later kinds of model each registered their first run. It holds 14 records: 6 hypotheses, 8 experiments and 0 decisions. The seventh and eighth experiments are not registrations: they are the records of the capability probe's two measured runs |
| ADRs | Done: 0001 to 0008, and 29 records now. Later phases added 0009 to 0016 and 0019, the host layer 0017 and 0018, the foundations of F5 and F6 0020 to 0022, the weekly loop 0023, the Directional Change sleeve 0024, gradient boosting 0025, sleeve state 0026, the deploy gate 0027, imported history 0028 and the sequence network 0029 |
| Requirements | Seeded by engineering judgement in [`requirements/requirements.yaml`](requirements/requirements.yaml), and taken from the private knowledge base, cited by id, in [`requirements/kb-requirements.yaml`](requirements/kb-requirements.yaml). 23 of the first file's 31 requirements are verified by tests they name and the other 8 are open. 12 of the second file's 80 requirements are verified, all of them by tests on synthetic streams; the other 68 are open. Its last 12, on directional change, come from a second release of the knowledge base |
| Exit gate | `make ci` is green locally. Making `test` and `leakcheck` required checks is done on GitHub by the owner |
