# Security

## Reporting

Report a suspected secret leak or vulnerability privately to info@algorythmos.com.au. Never open a public issue.

## The repository is public

It is public for now, by the owner's decision, and is written to be safe to read by anyone.

| | |
|---|---|
| Never committed | Market data, recordings, model files, text from the private knowledge base, credentials, account ids, host names and addresses |
| How that is enforced | `tools/leakcheck.py` in a pre-commit hook and in CI, with an attestation tied to the exact tree ([ADR 0006](docs/adr/0006-public-repo-and-leak-mechanism.md)) |
| What CI holds | No secrets and no artifacts. The workflow has `contents: read` and nothing else (`tests/unit/test_workflows.py`) |
| If something leaks | Treat it as published. Rotate the credential first, then tell the owner; removing it from history comes after and is the owner's call |

## Secrets

- **Where they live.** Nowhere in this repository and nowhere in CI. The broker's paper login will live only on
  the host that runs the platform, in a file readable by the platform's own Unix user, installed by the owner.
- **Who handles them.** The owner. Automated contributors never log in to the broker, never type credentials
  and never place an order by hand.
- **What must not exist.** A live broker login on the host. The platform uses a separate paper-only username,
  so there is no live credential for a bug to find.
- **Blocking them.** `.env` files are git-ignored. The leak check can also be given a private list of terms
  that must never appear; that list is never in the repository.

## Trading safety

- **Paper only**, by construction. The paper-only lock (`hftlab.safety.lock`, [ADR 0001](docs/adr/0001-paper-only-lock.md))
  is applied by the guard every venue shares: before a connection is opened, again on the account ids the
  broker reports, and again before every order. A refusal is final for that process. All of these must hold:
  - `MODE` is exactly `paper`
  - `LIVE_TRADING_ENABLED` is unset or an explicit false
  - the gateway port is a paper port (IB Gateway 4002, TWS 7497)
  - the broker reported at least one managed account and every one is a paper id (`DU` and digits)
  - if a live username is declared, the configured username is known and is not that one
- **Nothing else can turn it off.** A test scans every module of the package: only the lock may name a live
  port or read those environment variables, network libraries may be imported only by the broker adapter, and the venue registry holds exactly `ibkr_paper` and `sim` (`tests/safety/test_isolation.py`).
- **Every order will pass an independent risk gate** that rebuilds positions from fills and owns the `KILL`
  and `FLATTEN` switches. The gate is built in a later phase; its requirements are already in
  [`docs/requirements/requirements.yaml`](docs/requirements/requirements.yaml).
