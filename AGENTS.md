# AGENTS.md: rules for automated contributors in hft-lab

Read the org rules first (`Algorythmos-AI/.github-private` AGENTS.md and `catalog.yaml`). This file adds the rules
specific to this repository. Where the two conflict, the org file's "Never" sections win. The separation contract
between the trading platforms lives in that repository's `docs/trading-platforms.md`.

## Hard rules

1. **Paper only.** Real-money trading is never enabled from this repo.
   - Every venue is built on `hftlab.venues.guard.GuardedVenue`, which applies the paper-only lock
     (`hftlab.safety.lock`) before connecting, again on the account ids the broker reports, and again before
     every order. Never add a second path to the broker beside it. The lock refuses unless all of these hold:
     - `MODE` is exactly `paper`;
     - `LIVE_TRADING_ENABLED` is unset or an explicit false;
     - the gateway port is a paper port (IB Gateway 4002, TWS 7497);
     - the broker reported at least one managed account and every one is a paper id (`DU` and digits);
     - if a live username is declared, the configured username is known and differs from it.
   - `MODE`, `LIVE_TRADING_ENABLED` and every login belong to the owner alone. Never read them outside the lock,
     never name a live port outside the lock, never add a venue to `hftlab.safety.venues`
     (`tests/safety/test_isolation.py` fails on each).
   - Never log in to the broker, never type credentials, never place an order by hand.
2. **One platform per pull request, and no imports from other platforms.**
   - This repository is one platform. It has its own config, kill switch, state directory, journal, secrets
     file and runbooks, and it reads nobody else's.
   - No import, submodule or copied state from another trading platform's repository. A generic piece that is
     needed here is copied in and trimmed, with its origin commit noted.
   - Network libraries are imported only under `src/hftlab/venues/ibkr/` and `src/hftlab/record/`, and the
     broker's client library (`ib_async`) only under `src/hftlab/venues/ibkr/`.
3. **Pre-registration.** Every strategy or evaluation change needs a hypothesis, an experiment and a decision
   record in `research/`, written before any affected P&L is viewed.
   - Ids are `HFT-HYP-NNNN`, `HFT-EXP-NNNN` and `HFT-DEC-NNNN`, sequential and never reused.
   - Experiments are append-only: a re-run gets a new `HFT-EXP` id. A hold-out is run once per frozen config.
   - A reference to another repository's record is written `repo:ID`.
   - Every evaluated configuration is counted in `config/trial_registry.yaml`.
   - `python -m hftlab.registry lint` runs in CI and checks what a machine can: ids, folders, uniqueness,
     unchanged experiments, `repo:ID` references and trial rows. The rest is on the author and the reviewer.
4. **Never build** anything on [`docs/never-build.md`](docs/never-build.md): no live-money path, no layering,
   quote stuffing or spoofing, no order placed with intent to cancel.
5. **Commits and PRs.**
   - Branch, open a PR, and let the `test` and `leakcheck` checks pass, then squash-merge. Keep the branch up
     to date with `main` before merging, so the tree that lands is the tree that was attested.
   - No `Co-Authored-By` trailer and no AI-tool mention anywhere: not in commit messages, files, docs or
     comments (org decision D-016). The `commit-msg` hook refuses both.
   - Author: `skalaliya <skalaliya@gmail.com>`.
   - Run `make hooks` once per clone. Commit with `git add` then `git commit`; the pre-commit hook scans the
     staged tree and stages `.leakcheck-attest.json`. After a rebase, merge or cherry-pick run `make attest`.

## Data classes

The repository is **public**. Assume every byte committed is read by strangers and indexed.

| Class | Examples | May be committed |
|---|---|---|
| NEVER | Market data and tick history from any source, recordings, model files and weights, text from the private knowledge base, credentials and tokens, account ids, host names and addresses, the private term list and n-gram index of the leak check | Never |
| PRIVATE | Account balances, P&L of a real account, log lines, owner notes | Never here; they stay on the host or behind the owner's login |
| SAFE | Source code, tests on synthetic data, design records, research records written in our own words, counts, ids, statuses, dates and statistics | Yes |

- Research records are written in our own words. Quote nothing from course material, books or the private
  knowledge base; cite it by id instead, once citations exist.
- Tests use synthetic data only: `tmp_path`, fixtures built in the test, no network.
- A file of a denied type (pdf, office documents, notebooks, parquet, sqlite, archives, images, pickles, model
  files, tick files) is refused by the leak check whatever it is called. The allow-list
  (`tools/leakcheck-allow.txt`) takes an exact path and SHA-256 and needs a reason.

## Layout

```
src/hftlab/    core · safety · venues (port, guard, ibkr/, fake/) · record · registry
               (later: calendar · features · labels · models · strategy · risk · exec · book · learn · ops)
tools/         leakcheck.py and commitmsg.py, used by the hooks and CI
research/      hypotheses/ experiments/ decisions/
config/        risk limits, venue capability file, trial registry
docs/          architecture, ADRs, roadmap, requirements, runbooks, never-build list
contracts/     versioned data contracts this platform publishes (the status snapshot), each with an example
tests/         pytest and hypothesis (CI-safe: fixtures and tmp_path only, no network)
```

Git-ignored: `.venv/`, `var/` (runtime state), `data/`, `logs/`, `models/`, `*.parquet`, `.env`, `KILL`, `FLATTEN`.

## Everyday commands

```
make bootstrap     # one .venv from the hash-locked requirement files
make hooks         # use the tracked hooks in .githooks
make ci            # lint + typecheck + test + leakcheck; the same as the CI checks
```

The switches are files in the platform's state directory on the host. While `KILL` exists the trader makes no
new entries and keeps managing exits. `FLATTEN` closes every position.
