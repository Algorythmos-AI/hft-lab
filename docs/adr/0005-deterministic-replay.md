# ADR 0005: Deterministic replay as the main test strategy

- **Status:** accepted, 2026-10-10
- **Context:** F0 foundation; replay parity is an exit gate from F2 on

## Context

An automatic trading system fails in ways unit tests do not reach: a quote that arrives between two steps of
a decision, an execution report delivered twice, a restart in the middle of an order. Those failures depend
on the exact order of events, and they cannot be reproduced from a log line and a guess.

Testing against the broker is not an answer either. The paper account is slow, stateful, needs a login and
gives different quotes every time.

## Decision

1. **Strategy, risk and shadow-book code is a pure function of the event stream and the clock.** It does no
   I/O, reads no system time and holds no hidden state. Given the same events in the same order with the same
   clock, it makes the same decisions.
2. **The foundations make that possible** ([ADR 0002](0002-integer-prices-and-nanosecond-timestamps.md)):
   integer prices and quantities, an injected clock, a receive timestamp and a sequence number on every
   event, and client order ids that are a pure function of the decision.
3. **Replay parity is the main test.** The decisions taken while running equal the decisions of an offline
   replay of the same recording. It runs in CI on synthetic streams and nightly on real recorded days.
4. **A fake broker stands in for the real one** at the protocol level, so CI needs no network and no account.
5. **Faults are injected into the stream, not hoped for:** disconnect mid-order, duplicate and out-of-order
   execution reports, partial fills, rejects, gateway restart, clock jump, stale quotes, weekend gap,
   rollover blow-out, disk full, process killed mid-write.
6. **Crash recovery is tested by killing at every step** of the order outbox; state must come back
   consistent.

## Consequences

- Any incident on a recorded day can be replayed on a laptop and stepped through.
- Purity is a constraint on every module outside `venues/` and `record/`. Two tests hold the line today:
  only the wall clock may read system time, and only those two packages may import a network library.
- Anything that cannot be made deterministic (network timing, the broker's own behaviour) is pushed to the
  edge and recorded, so that what happened becomes an input instead of a hidden variable.
- Unit and property tests still matter for the pieces: the accounting invariants (cash conserved, position
  equals the sum of fills, booking exactly once) are checked under generated inputs.
