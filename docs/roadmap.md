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
| Registry | Done: lint and trial count; no records yet |
| ADRs | Done: 0001 to 0008 |
| Requirements | Seeded by engineering judgement in [`requirements/requirements.yaml`](requirements/requirements.yaml). The citations to the private knowledge base are added later, so this part of the deliverable is not complete |
| Exit gate | `make ci` is green locally. Making `test` and `leakcheck` required checks is done on GitHub by the owner |
