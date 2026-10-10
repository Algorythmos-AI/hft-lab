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

## Where things stand

**The gates of F1, F2, F3 and F4 are all open.** The code of the first three phases, and of the first half of
F4, is written and passes its tests against a deterministic fake broker and against synthetic streams and
recordings the tests make themselves. That is all it has been run against:

- Nothing has connected to a gateway. The capability probe has not been run with the paper login.
- Nothing has placed an order on the paper account, and nothing can yet: the process that would is not built,
  and the risk limits are not in force.
- Nothing has been recorded from the real feed. There is no recorded trading day.
- No experiment has been run on real data. The research registry holds one hypothesis and one experiment,
  both registered and neither run.
- Every number in the cost model, the recorder's quota and the feasibility settings is a placeholder.

| Gate | State | What closes it |
|---|---|---|
| F1 | Open | The owner runs the capability probe against the real paper account ([runbook](runbooks/ibkr-probe.md)); the probe report and the two decision records follow |
| F2 | Open | Five recorded trading days with every gap accounted for. Code cannot close it: the days have to pass |
| F3 | Open | The registered control run (`HFT-EXP-0001`) on those five days, and the owner's decision on it. Code cannot close it either |
| F4 | Open | The trader process, risk limits set by a decision record, then five trading days on the real paper account with zero reconciliation breaks and the slippage report. Code cannot close it: the days have to pass |

## Where F4 stands

The first half of F4 is written: everything between a sleeve's intent and the broker, except the process
that wires it together. It has run against the fake broker and the shadow venue, and against nothing else.

| Part of the deliverable | State |
|---|---|
| Risk gate | Written: `src/hftlab/risk/` ([ADR 0015](adr/0015-risk-gate.md)). Every check has a code from a closed list and its own tests. **Every limit in `config/risk.yaml` is a null placeholder and the file is not in force**: no gate, and so no order manager, can be built from it until a decision record sets the values. Gross exposure and leverage are not enforced: they need a currency conversion, and nothing does one |
| Order manager | Written: `src/hftlab/exec/` ([ADR 0016](adr/0016-order-manager-and-outbox.md)). Idempotent ids, an explicit state machine, a write-ahead outbox, recovery by client order id, cancel and reconcile on every reconnect. It is the only caller of `place` and `cancel` on a venue the platform trades on, and a scan holds that |
| Broker book and reconciliation | Written: the same `Book` class, fed by the venue's fills; `src/hftlab/book/reconcile.py` compares the venue's positions and executions with it, books what was missed once, and flags a break that clears only when the books agree again |
| Fault-injection suite | Green against the fake broker (`tests/faults/`): a kill at every step of the outbox, a disconnect mid-order, duplicated and reordered reports, partial fills, rejects, a gateway restart, a clock jump, stale quotes, the weekend gap, a rollover blow-out, an outbox that cannot be written, and generated sessions that hold the accounting invariants. What only a real gateway does is not in it |
| Trader process | **Not built.** The loop that polls the venue, runs the sleeves, passes their intents to the order manager, steps the reconciliation and schedules the reconciler is the next round. Until it exists nothing reads the real `KILL` and `FLATTEN` files and nothing can place an order |
| Control sleeve placing minimum-size paper orders | **Not started.** It needs the trader process, limits in force by a decision record, and the probe of F1 |
| Shadow-versus-broker slippage report | **Not built** |
| Alerts for a fault and for a reconciliation break | **Not built.** Both stop entries and are journalled; nobody is told. Alerts belong to the host layer |
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
experiment needs it), and the runbook for a shadow run.

## Where F2 stands

| Part of the deliverable | State |
|---|---|
| Calendar | Written: the forex week by New York time, the rollover, dated holidays and macro events, `src/hftlab/calendar/`. **Both dated lists are empty**: no holiday and no event has been entered. A calendar directory or file that is missing is an error, not an empty calendar |
| Recording format and store | Written: `src/hftlab/record/` ([ADR 0012](adr/0012-recording-format.md)). Tested with a kill at every byte of a block, restarts, a full quota and an offload that does not confirm |
| Recorder process | Written: `python -m hftlab.record`, on a market-data view that has no order methods. Run so far only against the fake broker (`--dry-run-seconds`) |
| Replay and the day check | Written: `python -m hftlab.replay --day DIR --check`. Replay parity is tested on recordings of the fake broker |
| Recording 24 hours on weekdays | **Not started.** It needs the probe to have been run, the host's pre-flight measurement ([ADR 0007](adr/0007-shared-host-isolation.md)), the install, and a runbook for the recorder. None of those has happened yet |
| Exit gate: five days, every gap accounted for, replay parity, memory and disk inside budget | Open. The quota in `config/recorder.yaml` is a placeholder until the host's disk budget is measured |

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
| Registry | Done: lint and trial count. The first two records came with F3 |
| ADRs | Done: 0001 to 0008. Later phases added 0009 to 0016 |
| Requirements | Seeded by engineering judgement in [`requirements/requirements.yaml`](requirements/requirements.yaml), and taken from the private knowledge base, cited by id, in [`requirements/kb-requirements.yaml`](requirements/kb-requirements.yaml). Seventeen of the first file's twenty-five are verified by tests they name; the other eight and all 68 of the second are open |
| Exit gate | `make ci` is green locally. Making `test` and `leakcheck` required checks is done on GitHub by the owner |
