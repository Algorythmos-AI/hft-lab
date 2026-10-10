# ADR 0001: The paper-only lock

- **Status:** accepted, 2026-10-10
- **Context:** F0 foundation

## Context

The platform trades automatically, around the clock, through a broker that offers a live account and a paper
account behind almost the same API. The difference between them is a port number, a login and an account id.
A fully automatic system must not depend on anyone remembering which one it is pointed at.

A paper-only flag checked in one place is not enough. A flag can be set wrong, a gateway can be started in
the wrong mode, and a login can belong to the wrong account while every setting in this repository still
says "paper".

## Decision

1. **One pure function decides.** `hftlab.safety.lock.paper_violations(environ, facts)` returns every reason a
   connection is not provably paper. It reads nothing but its arguments. `assert_paper` raises
   `PaperOnlyError` when the list is not empty. It is checked when a venue adapter connects and again before
   every order.

2. **All of these must hold:**
   - `MODE` is exactly `paper`. Not `Paper`, not ` paper`.
   - `LIVE_TRADING_ENABLED` is unset, empty or an explicit false (`0`, `false`, `no`, `off`). Any other
     value refuses, including one the code does not recognise.
   - The gateway port is a paper port: IB Gateway 4002 or TWS 7497. The live ports 4001 and 7496 are refused
     by name; every other port is refused too.
   - The broker reported at least one managed account, and **every** one is a paper id: `DU` followed by
     digits. An empty list refuses. An id that is `U` followed by digits is reported as a live account id.
     These ids come from the broker after connecting, not from configuration.
   - If a live username is declared, the configured username must be known and must differ from it.

3. **The host holds a paper-only login.** A separate paper username is used, so no live credential exists on
   the host. Declaring a live username to the lock is therefore optional and discouraged: it only adds a
   refusal by name, and it means writing the live name down somewhere.

4. **Nothing else can turn it off.** `tests/safety/test_isolation.py` scans every module of the package and
   fails when anything but the lock names a live port, holds a `live` mode literal, or touches the process
   environment other than by reading a constant key that cannot select the trading mode. It also fails when
   a network library is imported outside `hftlab.venues` and `hftlab.record`.

5. **The venues are a closed list.** `hftlab.safety.venues.VENUES` holds exactly `ibkr_paper` and `sim`. The
   simulated venue is exempt from the lock because it never talks to a broker.

## Consequences

- A wrong port, a wrong login or a wrong flag stops the platform with a message that says which, instead of
  placing an order.
- The rules are strict enough to refuse some honest setups: a paper account whose id has another prefix, or
  a gateway on a non-standard port. Those need a change to the lock, with a decision record, not a way round.
- The scan reads syntax. A port built from a variable, or a network call made by shelling out, is not
  visible to it. The run-time lock and review are the other two layers, and the scan's docstring says so.
- F0 shipped the lock and its tests. F1 added the adapter and the way the lock is applied over the life of
  a connection, below.

## Addendum (F1): the lock in three stages

The account ids exist only after connecting, so the lock cannot run whole before a socket is opened.
`hftlab.safety.gate.PaperGate` applies it in stages, and the shared venue guard
([ADR 0009](0009-venue-port-guard-and-fake-broker.md)) is the only caller:

1. **Before connecting:** mode, live flag, gateway port and username. Nothing is dialled if this fails.
2. **After the handshake, before anything else is asked of the broker:** the full lock, with the account ids
   the broker reported. Passing it mints a permit for that one connection.
3. **Before every order and every cancel:** the full lock again, and the permit must be the current one.

A refusal at any stage is terminal for that venue object. A permit dies with its connection, so a reconnect
starts unchecked. The port that is dialled must be the port the lock was given. The usernames are read from
`HFT_IBKR_USERNAME` and `HFT_IBKR_LIVE_USERNAME`, by the lock module only; they are compared and never sent,
because the API connection carries no username: the account list is the evidence, the username a second
opinion.
