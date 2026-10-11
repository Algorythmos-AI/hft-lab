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
send, which waits for the owner's signing key and for the site to accept this desk's key id and schema, and
then the parts of the snapshot that later phases make real. More pairs and a second venue are unchanged.

## Where things stand

**The gates of F1, F2, F3 and F4 are all open.** The code of the first four phases is written and passes its
tests against a deterministic fake broker and against synthetic streams and recordings the tests make
themselves. That is all it has been run against:

- Nothing has connected to a gateway. The capability probe has not been run with the paper login.
- **Nothing has traded.** No order has been placed on the paper account. The process that would is written,
  and as shipped it cannot: the risk limits are not in force, so it runs in shadow-only mode, and every
  sleeve's stage is `shadow` with no promotion rule that could be met.
- Nothing has been recorded from the real feed. There is no recorded trading day.
- No experiment has been run on real data. The research registry holds four hypotheses and four
  experiments, all registered and none run.
- Every number in the cost model, the recorder's quota and the feasibility settings is a placeholder.
- **F5 has not started as a phase, and F6 is open.** F6's weekly loop is written and tested on synthetic
  streams and against the fake broker only (see "Where F5 and F6 stand"). Its gate is not met and code
  cannot meet it. No model has been trained on real data, and nothing in this repository is evidence of an
  edge.

| Gate | State | What closes it |
|---|---|---|
| F1 | Open | The owner runs the capability probe against the real paper account ([runbook](runbooks/ibkr-probe.md)); the probe report and the two decision records follow |
| F2 | Open | Five recorded trading days with every gap accounted for. Code cannot close it: the days have to pass |
| F3 | Open | The registered control run (`HFT-EXP-0001`) on those five days, and the owner's decision on it. Code cannot close it either |
| F4 | Open | The capability probe of F1, risk limits put in force by a decision record, the owner's decision to let the control sleeve place minimum-size paper orders, then the registered run `HFT-EXP-0002`: five trading days on the real paper account with zero reconciliation breaks and the slippage report. Code cannot close it: the days have to pass |

| F6 | Open | Recorded weeks to train on, the owner's two times for the weekend jobs and their timers enabled, then two full weekly cycles on the host that needed no manual help ([`HFT-EXP-0003`](../research/experiments/HFT-EXP-0003-first-ridge-challenger-shadow-week/protocol.md) is the first, registered and not run) and one rollback drill there ([runbook](runbooks/learning.md)). A deep challenger is not built. Code cannot close it: the weeks have to pass |

## Where F5 and F6 stand

**F5: foundations and one baseline sleeve, in shadow. F6: the weekly loop is written, and its gate is open.**
What follows needs one dependency, which only training a gradient-boosted model installs. It has run on synthetic streams the tests make themselves, some with an
effect planted in them on purpose so that the answer is known, and against the fake broker. **There is no
real data, no model has been trained on real data, and nothing here is evidence of an edge.** In
`config/ops.yaml` F5 stays `not_started` and F6 is `open`: its code is written and its gate is not met.

| Part | State |
|---|---|
| Features | Written: `src/hftlab/features/` ([ADR 0020](adr/0020-features-and-labels-timing.md)). One step serves a recording and a live stream; every value is an integer; a crossed, unconfirmed or shut book gives no row. Slow for long windows on a fast feed, by a known limit of the implementation. **Never computed on a recorded day** |
| Labels | Written: `src/hftlab/labels/`. The outcome of each side priced with the cost model as the shadow venue prices, and triple-barrier outcomes, each with the time its window ends. They assume the cost model's largest latency. **Every one is priced on placeholder costs** until the cost model is measured |
| Datasets | Written: `src/hftlab/learn/dataset.py`. Built per pair and horizon from day folders of the store; the builder refuses a feature that reads ahead, by a check that looks at up to sixteen places of the stream and is a test, not a proof. **No dataset of real data exists**, and none is ever committed |
| Validation | Written: `src/hftlab/learn/validate.py` ([ADR 0021](adr/0021-validation-splits-trials-and-the-three-numbers.md)). Walk-forward by week with purge and embargo, a hold-out read once per frozen configuration, a trial ledger beside the signed registry, a day-block bootstrap, the deflated Sharpe ratio, and oracle, model and random on the same rows. **Nothing has been validated**: `config/trial_registry.yaml` is still empty |
| Models | Written: `src/hftlab/models/` ([ADR 0022](adr/0022-model-file-registry-and-scorer.md)). A model file named by its hash and never overwritten, ridge regression on standardised features solved exactly, the flat and the seeded random baseline, and a scorer that runs in the caller's process. **No model ships and none has been fitted to real data.** Neural networks are **not built**: they wait for recorded data and a decision on compute |
| Gradient boosting | Written: `src/hftlab/learn/boost.py`, `src/hftlab/models/trees.py` ([ADR 0025](adr/0025-gradient-boosting-optional-set-and-whole-number-trees.md)). A second kind of model, chosen by `model: gbm` in `config/learn.yaml`; ridge is the default. It is trained with LightGBM, pinned in an optional requirement set of its own (`make ml`, `hft-install --with-ml`; about 80 MB) that the recorder and the trader never install or import. The fitted trees are exported as whole numbers and scored by the platform's own arithmetic, so a decision does not depend on the library or on a float. Small, single-threaded, deterministic, fixed settings and no search. On synthetic streams it finds a planted band that ridge cannot and makes nothing of noise after costs. **Its settings are placeholders, it has been fitted to synthetic rows only, and the tests that fit ran on macOS only**: the `ml` job in CI is their first run on Linux |
| ML sleeve | A skeleton: `src/hftlab/strategy/ml.py`. It trades a cell the feasibility map marks viable when the model expects more than its margin, leaves at the label's horizon, and reports that it may not leave shadow while its model or its map rests on placeholder costs. In the trader, on a synthetic stream with a planted effect, it earns what the effect is worth and replays byte for byte; without the effect it pays the control sleeve's cost. **That checks the plumbing and says nothing about any market.** The shipped trader settings name two places for it, a champion's and one challenger's, and both run idle: no model is registered |
| Lineup | Written: `src/hftlab/lineup/` ([ADR 0023](adr/0023-weekly-learning-loop.md)). A hash-chained journal says which model stands in which place. The trader loads a model and its report by hash and by nothing else, journals what each session served, and steps a place down when its model changes. A journal that does not verify serves nothing. **No lineup journal exists on any host** |
| Weekend training | Written: `python -m hftlab.learn.train`. A walk-forward report as bytes, a candidate of the configured kind in the registry under its hash, a memory budget that fails the run. **It has trained on synthetic weeks only** |
| Directional Change sleeve | Written: `src/hftlab/strategy/dc/` ([ADR 0024](adr/0024-directional-change-sleeve.md)). A detector that books each event at the tick that crossed and keeps the provisional and the completed overshoot apart as two types; a rule-based baseline sleeve in two variants, trend and contrarian, that refuses a threshold small against the spread; and a random-walk null of matched volatility. Tested on synthetic streams: a zig-zag worked out by hand, driftless walks on which it pays the control sleeve's cost, a planted pattern against its null. **It has seen no real data and is not a claimed edge.** Configured as `directional_change` with every number a placeholder, at stage `shadow`. Its first shadow run is registered as `HFT-EXP-0005`, expected to show no advantage, **not run**. Not built: the command that runs the null on a recording, and carrying a sleeve's state across sessions |
| Promotion and rollback | Written: `python -m hftlab.learn.promote`. It reads the week's shadow round trips from the trader's journals, applies the rule through the pure function, and writes the lineup: promote, keep or roll back, with a page and the hash of the report it rests on. A promotion needs the rule in force, its decision record with the owner's approval, the risk limits in force and a week with no reconciliation break. A champion that trips its loss latch is rolled back without a rule; `--drill` proves that path on a copy. **As shipped it says `keep: rule not in force`: no rule exists.** The cycle has run with the trader on the fake broker only |
| Drift | Written: `python -m hftlab.learn.drift`. Feature shift, calibration and the spread regime against the summary kept at training; a breach asks the trader to take the sleeve down to shadow. **Every threshold is null**, so it reports and steps nothing down |
| Units, status, runbook | `hft-train` and `hft-promote` are rendered, at placeholder times that are the owner's to set, and the install script enables neither. The status screen and the snapshot show the lineup once something is registered. [Runbook](runbooks/learning.md). **Nothing is installed, and the page has not been followed on the host** |
| Sleeve state across a restart | Written: `src/hftlab/trader/resume.py` and each sleeve's `save` and `restore` ([ADR 0026](adr/0026-sleeve-state-across-a-restart.md)). A session that ends on purpose writes the control, the ML and the Directional Change sleeve's state into a hash-chained file, tied to where its journals stood and to how its tape ended. The next start restores a sleeve only when nothing moved since, the sleeve had no order on its way, and its stage and settings are the same; otherwise it starts cold, warm-up and all, and the reason is journalled and paged. A restored sleeve keeps what it holds; a cold one has it closed by the trader, as before. Tested by cutting synthetic sessions at many points against the uncut run, and with stale, broken and tampered files. **No state has been saved or restored on the host** |
| Scorer in its own process | Written: `src/hftlab/scoring/` ([ADR 0023](adr/0023-weekly-learning-loop.md), section 5). A place whose settings say `scorer: process` is scored by a child the trader talks to over a pipe, in a fixed, versioned message format of whole numbers and text. A child that hangs or dies costs its sleeve the rows until a new one answers, after a back-off; the loop waits at most one timeout and the other sleeves trade on. A child that holds another model than the lineup's is refused. The rows a child did not score are recorded beside the tape, so a day replays byte for byte without any child. Tested with real child processes, hung and killed, on synthetic rows and on the fake broker's market. **As shipped every place scores in the trader's process, and no scorer process has run on the host or served a real model** |
| DL challenger | **Not built.** It waits for recorded data and a decision on compute |
| Pre-registration | `HFT-HYP-0003` and `HFT-EXP-0003` register the first ridge challenger's week in shadow. The expected outcome is no advantage over the control sleeve after costs. `HFT-HYP-0004` and `HFT-EXP-0004` register the first gradient-boosted challenger's week, beside a ridge challenger; the expected outcome is no advantage over the control sleeve or the ridge challenger. **Not run. No decision record exists and no promotion rule is registered**; a rule that names no decision record passes nothing |
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

Not built yet in F3: the tool that compares the control run trip by trip with the recording (the registered
experiment needs it). The [runbook for a shadow run](runbooks/shadow-run.md) is written and has not been
followed on the host.

## Where F2 stands

| Part of the deliverable | State |
|---|---|
| Calendar | Written: the forex week by New York time, the rollover, dated holidays and macro events, `src/hftlab/calendar/`. **Both dated lists are empty**: no holiday and no event has been entered. A calendar directory or file that is missing is an error, not an empty calendar |
| Recording format and store | Written: `src/hftlab/record/` ([ADR 0012](adr/0012-recording-format.md)). Tested with a kill at every byte of a block, restarts, a full quota and an offload that does not confirm |
| Recorder process | Written: `python -m hftlab.record`, on a market-data view that has no order methods. Run so far only against the fake broker (`--dry-run-seconds`) |
| Replay and the day check | Written: `python -m hftlab.replay --day DIR --check`. Replay parity is tested on recordings of the fake broker |
| Recording 24 hours on weekdays | **Not started.** It needs three things the owner does, in this order: the capability probe, the host's pre-flight day of measurements ([ADR 0007](adr/0007-shared-host-isolation.md)), and the install. None of the three has been run. What they need from this repository is written: see "Where the host layer stands" |
| Exit gate: five days, every gap accounted for, replay parity, memory and disk inside budget | Open. The quota in `config/recorder.yaml` is a placeholder until the host's disk budget is measured |

## Where the host layer stands

What F2 needs in order to run on the shared host ([ADR 0017](adr/0017-host-layer.md)). **All of it is written
and tested off the host. None of it is installed, and none of it has run on any host.**

| Part | State |
|---|---|
| Units | Rendered from code (`python -m hftlab.ops.units`) and committed in `deploy/systemd/`: the slice with its 1.2 GB cap, the recorder, housekeeping, the shadow runner, the trader (no install section: started by the owner, never enabled), the publisher, the backup, and a placeholder for the gateway that cannot be enabled. Tested as text. **Never loaded by a service manager** |
| Pre-flight gate | Written: a shell sampler for the host and `python -m hftlab.ops.preflight --check`, with thresholds in `config/preflight.json`. Tested on made-up samples. **No day has been measured on the host** |
| Install | Written: `deploy/bin/hft-install`, which refuses without a passing pre-flight and enables nothing unasked. Tested in dry runs against a temporary directory. **Never run for real** |
| Status and the dead-man rule | Written: `python -m hftlab.ops.status`, tested against the files a real recorder writes over the fake broker |
| Alerts | Written: once per problem, keys prefixed `hft:`. **No page has been sent**: the address is the owner's to configure |
| Status snapshot | Written: `python -m hftlab.publish` ([ADR 0018](adr/0018-snapshot-publish.md)). **Nothing has been sent.** The signing key and the key id the site accepts are the owner's open decisions |
| Backup | Written: `python -m hftlab.ops.backup`, into a directory or to a tool; it never deletes. **No backup directory exists** |
| Recorder hardening | Done: a write the system refuses stops the recorder by name instead of crashing it, and the loop's time away from the feed is in the status file ([ADR 0012](adr/0012-recording-format.md), addendum) |
| Runbooks | Written for the owner: [install](runbooks/install.md), [recorder](runbooks/recorder.md), [shadow run](runbooks/shadow-run.md), [weekly login](runbooks/weekly-login.md), [gateway down](runbooks/gateway-down.md), [switches](runbooks/kill-and-flatten.md), [trader](runbooks/trader.md), [backup](runbooks/backup.md), [rollback](runbooks/rollback.md), [publish](runbooks/publish.md). **None has been followed on the host** |

Every number in the units that is not the plan's (the per-service memory fuses, the weights, the restart
waits, the timer times) is a first choice, marked as one where it is written.

## Where F1 stands

| Part of the deliverable | State |
|---|---|
| Venue port | Done: `src/hftlab/venues/port.py`, with the shared guard ([ADR 0009](adr/0009-venue-port-guard-and-fake-broker.md), [ADR 0011](adr/0011-synchronous-pumped-port.md)) |
| Paper lock in stages | Done: before connecting, after the handshake, before every order; a reconnect starts unchecked |
| IBKR adapter against the fake broker | Done: `src/hftlab/venues/ibkr/` over `ib_async`, `src/hftlab/venues/fake/`, and a contract suite both pass. Written and tested without a gateway |
| The probe | Written and tested against the fake broker. **Not yet run with the paper login** ([runbook](runbooks/ibkr-probe.md)) |
| Exit gate: the probe report | Open. It needs the owner's run |
| Exit gate: decision records for the access route and the instrument form | Open. They follow the report; [ADR 0010](adr/0010-access-route.md) is the engineering default until then |

F1 is not finished until the last two rows are. Everything the adapter assumes about the gateway and could
not check offline is listed at the end of the runbook.

## Where F0 stands

| Part of the deliverable | State |
|---|---|
| Repo, governance | Done |
| Leak mechanism | Done: `tools/leakcheck.py`, hooks, attestation ([ADR 0006](adr/0006-public-repo-and-leak-mechanism.md)) |
| Core types | Done: `src/hftlab/core/` |
| Paper lock | Done as a checked function with its isolation tests. The adapter that calls it is F1 |
| Registry | Done: lint and trial count. The first two records came with F3, the next two with F4 |
| ADRs | Done: 0001 to 0008. Later phases added 0009 to 0016 and 0019, the host layer 0017 and 0018, the foundations of F5 and F6 0020 to 0022, the weekly loop 0023, and the Directional Change sleeve 0024 |
| Requirements | Seeded by engineering judgement in [`requirements/requirements.yaml`](requirements/requirements.yaml), and taken from the private knowledge base, cited by id, in [`requirements/kb-requirements.yaml`](requirements/kb-requirements.yaml). Twenty-three of the first file's 31 are verified by tests they name and the other 8 are open. Six of the second file's 68 are verified, all of them by tests on synthetic streams; the other 62 are open |
| Exit gate | `make ci` is green locally. Making `test` and `leakcheck` required checks is done on GitHub by the owner |
