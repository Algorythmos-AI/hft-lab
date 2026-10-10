# ADR 0007: Living on a shared host without hurting the other desks

- **Status:** accepted, 2026-10-10
- **Context:** F0 foundation; applies from the first install (F2)

## Context

The platform will run on a small host that already runs two other trading desks, by the owner's choice.
Those desks are in service. A new platform that records every quote, trains models at the weekend and
restarts itself is exactly the neighbour that takes a shared machine down.

The separation contract between the platforms says each one owns its config, kill switch, state directory,
journal, alert-key prefix, Unix user or unit prefix, secrets file and runbooks, and reads nobody else's.

## Decision

1. **Its own identity.** Unix user `hft`, its own install path, unit names prefixed `hft-`, its own helper
   names and its own secrets file. Nothing can collide with another desk's units or files, and nothing here
   reads another desk's state.
2. **Resource caps.** Every unit runs inside one `hft.slice` with a memory cap of about 1.2 GB and low CPU
   and disk priority. Under pressure the existing desks win. The platform's processes are built to be killed
   and restarted safely, which the recorder and outbox designs already require.
3. **A pre-flight gate before the first install.** One day of memory and load measurements on the host. If
   there is less than about 1.5 GB of headroom at the other desks' peak, the platform is **not** installed.
   The owner then chooses: a larger host, or the route without a gateway process if the capability probe
   supports it.
4. **A disk quota.** A rolling ten days of recordings locally, compressed and offloaded, under a hard quota
   that can never push the other desks below their free-space floor.
5. **Training stays out of the way.** It runs at the weekend with the trading processes stopped, capped at
   2 GB, and never overlaps another desk's training job. Deep models stay small enough for the host; moving
   training elsewhere is a later, costed decision.
6. **The owner installs and enables.** Automated contributors never log in to the broker, never type
   credentials, never enable or start units, and never place an order by hand. They hand the owner the
   command.

## Consequences

- The platform may be the one that gets killed. That is accepted and designed for.
- The caps are numbers in unit files, so a measured limit can be raised by a reviewed change instead of by
  hand on the host.
- Deploys follow the same discipline as the other desks: only a CI-green commit on `main`, smoke tests,
  automatic rollback, and only when flat.
- Resizing or splitting the host is a decision for when a measured limit is hit, not before.
