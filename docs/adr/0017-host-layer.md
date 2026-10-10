# ADR 0017: The host layer: units from code, a gate before the install, one status reading

- **Status:** accepted, 2026-10-11
- **Context:** F2, recorder; before the first install

## Context

[ADR 0007](0007-shared-host-isolation.md) decided how the platform lives on a host it shares with two other
desks: its own user and names, one capped slice, a pre-flight gate, a disk quota, and an owner who installs.
None of that existed as code. This record says how each of those decisions is made real, in a form that can be
reviewed and tested without the host.

Three things shape it.

- **Only the owner acts on the host.** Nothing in this repository logs in to the broker, holds a credential,
  or enables a unit. What is written here are files and scripts for the owner to run.
- **The other desks are in service.** A mistake in a unit file is a mistake on their machine. So the unit
  files are generated, reviewed as text, and held equal to the code by a test.
- **The repository is public.** No host name, address, account, bucket, topic or key may be in it. Every
  value of that kind comes from the environment on the host.

## Decision

1. **The names are constants in one module**, `hftlab.ops.host`: the user `hft`, the install at
   `/home/hft/hft-lab`, units prefixed `hft-` in `hft.slice`, the owner's secrets file `/etc/hft/secrets.env`,
   the state directory `var/` under the install with the `KILL` and `FLATTEN` switches in it, the alert-key
   prefix `hft:` and the backup tag `hft-lab`. The other desks' names appear in two places only: their unit
   prefix, so that a test can show nothing of ours collides with it, and their home directory, which every unit
   of ours keeps out of its own sight.

2. **The units are rendered from code** (`python -m hftlab.ops.units`), committed in `deploy/systemd/`, and a
   test fails when the two differ.

   | Unit | What it runs | May reach |
   |---|---|---|
   | `hft.slice` | Nothing: it holds the others. Memory capped at 1.2 GB, no swap, low CPU and disk weight | |
   | `hft-recorder.service` | `python -m hftlab.record`, always on | the gateway on this machine |
   | `hft-housekeep.timer` and `.service` | `python -m hftlab.record --housekeep`, hourly | nothing |
   | `hft-shadow.service` | `python -m hftlab.shadow --venue ibkr_paper` | the gateway on this machine |
   | `hft-publish.timer` and `.service` | the status check with alerts, then `python -m hftlab.publish --send`, every five minutes | the internet |
   | `hft-backup.timer` and `.service` | `python -m hftlab.ops.backup run`, nightly | nothing |
   | `hft-gateway.service` | A placeholder: see 3 | |

   Every service runs as `hft` in the slice with `MODE=paper`, a read-only file system but for the state
   directory, no capability, and a memory fuse of its own. No unit names the flag that would enable live
   trading. The two services that reach the gateway load no environment file, so nothing on disk can change
   the mode they are given, and they may talk to this machine only. Each service has an out-of-memory score
   above zero, the recorder the lowest: if the host runs out of memory, ours die before the other desks'
   units, and the recorder last among ours.

   The two daemons restart with a wait that grows from five seconds to five minutes, and are never given up
   on. Exit code 2, "refused or misconfigured", is not restarted: a refusal by the paper-only lock needs a
   person. The quota (3) and a write the system refused (4) are retried, slowly
   ([ADR 0012](0012-recording-format.md), addendum).

   The recorder is not started and stopped with the forex week. It runs always, records the weekend as
   closed, and comes back by itself after a reboot once the owner has enabled it.

3. **The gateway unit is described and cannot be enabled.** A standard third-party controller starts the
   broker's gateway and keeps it logged in. Its configuration holds the paper login, so it is the owner's
   file, outside this repository. The unit names where that file is, has no install section, and starts only
   when the owner has put the controller's configuration and a start script in place. Nothing here installs
   or starts it.

4. **A gate decides whether the platform is installed at all.**
   - `deploy/bin/hft-preflight-sample` is a POSIX shell script the owner starts on the host. Once a minute for
     a day it appends one line: the time, the memory available, the memory in total, the swap free, the load
     averages and the free disk. It writes nowhere else.
   - `python -m hftlab.ops.preflight --check FILE` judges the file and prints a pass or a fail with the
     numbers. Under 1.5 GB available at the tightest minute, or a disk that would leave the other desks under
     their 3 GB floor after our share, and the platform is **not** installed.
   - The thresholds are in `config/preflight.json`, each with who decided it and where. The three that are
     the plan's are marked so; the others are engineering choices.
   - The checker needs the standard library only, because it runs on the host's own Python before our
     environment exists. That is why this one settings file is JSON.
   - The gate is passed once. The report is kept in `/etc/hft`, and later runs of the install find it there:
     a second measurement would count our own memory against us.

5. **`deploy/bin/hft-install` is the install, and the owner runs it.** It refuses without samples that pass,
   refuses a commit that is not on `origin/main` or a checkout with uncommitted changes, prints every step
   before doing it, skips what is already done, and enables only the units the owner names with `--enable`.
   `--dry-run` prints the whole plan and changes nothing.

6. **One reading, one judgement.** `python -m hftlab.ops.status` reads the recorder's status file, which the
   recorder replaces every five seconds and which is its heartbeat, the store's journals and the shadow
   journal, and prints one screen. The status snapshot is built from the same reading
   ([ADR 0018](0018-snapshot-publish.md)).
   - **The dead-man rule:** a recorder that last said "recording" and has been silent for more than a
     minute while the market is open is dead, whatever the service manager believes. Exit code 3.
   - A host where nothing was ever recorded is not a failure. A recorder that stopped on purpose is a
     failure only while the market is open.

7. **Alerts page once.** `hftlab.ops.alerts` keeps the state of every key in a file: a page when a problem
   starts, none while it lasts, one when it is over. Every key carries the prefix `hft:`. How a page travels
   is injected. The real sender posts to the push-notification address in the environment; with none
   configured the page is written to the log and nothing else happens. The address never reaches a log.

8. **One module may make an HTTPS request.** Paging the owner and sending the snapshot need one, and until
   now only the broker adapter could open a connection. The rule is widened by one file,
   `src/hftlab/ops/post.py`, which may import five names of the standard library's HTTP client and nothing
   else that opens a socket; `tests/safety/test_isolation.py` holds it to that list. It sends to `https`
   addresses only, never to this machine, so it cannot be pointed at the gateway, and it follows no redirect.
   Shelling out to a download tool would have left the scan untouched, and that is the reason it was not
   done: the scan says in its own words that it cannot see such a call.

9. **The backup hands the journals away and never deletes.** `python -m hftlab.ops.backup` lists what cannot
   be made again (the recording's journals, the run counter, the shadow journals, the alert state; model
   files later), and copies it into a new folder of a directory the owner mounts, with a manifest of sizes
   and SHA-256, or hands the list to a configured tool. The recordings themselves leave through the store's
   own offload.

## Consequences

- **Nothing here has run on the host.** The units have been rendered and read, not loaded by a service
  manager. The scripts have run in `--dry-run` against a temporary directory. The first install is also the
  first test of both, and its runbook says what to look at ([install](../runbooks/install.md)).
- **The numbers that are not the plan's are first choices**, each marked ENGINEERING DEFAULT where it is
  written: the per-service memory fuses, the slice's reclaim point and weights, no swap for the slice, the
  restart waits, the timer times, the dead-man age of 60 seconds, and four of the gate's seven thresholds.
  They change by a reviewed edit when the host has been measured.
- **What the owner has to confirm** is listed at the end of the install runbook: the service manager's
  version (the growing restart wait needs 254 or newer), that the other desks' heaviest jobs ran inside the
  sampled day, the time of the nightly backup against their evening jobs, and how the gateway's controller
  is started.
- **The sandbox is deliberately not the tightest possible.** A system-call filter and a ban on writable
  executable memory are left out until they have been tried on the host: an obscure start failure at an
  inconvenient hour costs more than they buy before then.
- **The switches are files in the state directory**, `var/KILL` and `var/FLATTEN`. The status reads them and
  publishes them. Nothing acts on them yet: the risk gate that does is F4, and it has to look in the same
  place.
- **Units for the trader, the reconciler and the weekend jobs** arrive with their phases. They are added to
  the same table, and the same tests then apply to them without being asked.
