# ADR 0010: Access route: the TWS API through IB Gateway first

- **Status:** accepted as the engineering default, 2026-10-10. The research decision on the access route is
  an `HFT-DEC` record the owner writes after the capability probe has run; it may confirm or replace this.
- **Context:** F1, broker probe and adapter

## Context

There are two programmatic ways into Interactive Brokers.

| | TWS API through IB Gateway | Web API |
|---|---|---|
| What runs on the host | The broker's gateway application, logged in, listening on a local socket | Nothing of the broker's, if the keyed (OAuth) form is available to the account |
| Protocol | The broker's own socket protocol; a client library is needed | HTTPS requests and a WebSocket |
| Session | A login that lasts about a week, and a daily restart of the gateway | Tokens |
| Paper or live | A separate paper login, a separate default port (4002 against 4001), and account ids that start `DU` | To be established |
| Known unknowns for us | Memory use of the gateway on a small shared host | Quote update rate for forex, order round-trip time, what an individual account is allowed to use, and what the paper-only lock would check |

The lock ([ADR 0001](0001-paper-only-lock.md)) was designed around what the gateway gives: a port that says
paper, and an account list in the handshake that says paper again before anything else is asked.

## Decision

1. **F1 builds the TWS API route:** IB Gateway in paper mode on this machine, reached with the `ib_async`
   library, the maintained successor of `ib_insync`. The library is pinned by hash and may be imported only
   under `src/hftlab/venues/ibkr/`.
2. **The adapter is kept thin so the route can change.** Everything the platform depends on is the venue
   port ([ADR 0009](0009-venue-port-guard-and-fake-broker.md)). A second adapter for the Web API would
   implement the same port, pass the same contract suite and reuse the same guard.
3. **The Web API with OAuth stays an open alternative.** Its attraction is real: no gateway process to keep
   alive and log in every week, and less memory on a host shared with two other desks
   ([ADR 0007](0007-shared-host-isolation.md)). It is not chosen now because what it can do for forex on
   this account is not known, and the probe as built measures the gateway route.
4. **The probe decides whether to reopen this.** If it shows the gateway route is too slow, too fragile over
   the weekly login, or too heavy for the host, the alternative is examined with a probe of its own before
   anything is rebuilt.

## Consequences

- The owner has to install the gateway, hold a paper-only login and log in about once a week. The runbook
  says how ([ibkr-probe](../runbooks/ibkr-probe.md)); a weekly-login runbook follows when the host install
  does.
- The gateway's daily restart and a loss of its own link to the broker are treated as lost connections: the
  supervisor reconnects on its backoff schedule, and every reconnect passes the lock again.
- Two facts this ADR leans on are not verified offline: that a paper login reports only `DU` account ids,
  and that the paper gateway listens on 4002. Both are checked by the lock itself on every connection, so a
  wrong assumption refuses rather than trades.
- A change of route later costs one adapter and its translation tests, not the platform.
