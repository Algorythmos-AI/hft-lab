# Runbook: a deploy

For the owner, on the host. A deploy puts another commit of `main` in service. It is one command,
`python -m hftlab.ops.deploy`, and the code on the host changes through it and through nothing else
([ADR 0027](../adr/0027-deploy-gate-and-reports.md)). Going back is [the rollback runbook](rollback.md).

**Nothing on this page has been followed on the host.** The command has run on temporary folders with a
stand-in for the service manager. The first `adopt` and the first deploy are drills: do them at a weekend,
with everything stopped, and correct this page from what you see.

The command runs as root, because it restarts units. Everything it starts that touches the code (git, uv,
the smoke tests) runs as the user `hft`. The shorthand, once per session:

```
hftdeploy() { ( cd /home/hft/hft-lab && sudo env PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m hftlab.ops.deploy "$@" ); }
```

## What it will and will not do

| It refuses unless | Why |
|---|---|
| the commit is on `main` | Only reviewed code is deployed. Name the commit by its hash, never by a branch |
| the checks `test` and `leakcheck` are green on it | It asks GitHub through the `gh` program when the host has it. When it cannot ask, you look yourself and say so with `--checks-verified-by-owner`. A check it looked up and found red is refused whatever you say |
| every sleeve is flat in the broker book, and no order is on its way | Read from the order outbox. An outbox that does not verify is refused: "not known" is not "flat" |
| the trader is stopped, or the market is shut | |
| the recorder is not restarted within 30 minutes of 17:00 New York | A hole in the recording costs most at the rollover |
| the commit's unit files are the ones installed | See "When the unit files changed" |

| It restarts | It never |
|---|---|
| `hft-recorder.service` and `hft-shadow.service`, each only if it is running. Neither can place an order | restarts, starts or stops `hft-trader.service`. A trader that is running keeps running the code it was started on, and the command tells you so. Restarting it is yours |
| nothing that is stopped | starts a unit that was not running |

Every step is printed before it is done. If the status check after the restart finds a problem that was not
there before, it puts the old code back in service and restarts the same units, by itself, and you get one
page, `hft:deploy`.

## 0. Once: adopt the layout

The install script leaves a plain folder at `/home/hft/hft-lab`. The deploy needs that path to be a link it
can swap. `adopt` makes it one, and moves the state directory out to `/home/hft/hft-lab.d/var`, where every
checkout is linked to it.

1. Stop everything of the platform's, and check.

   ```
   sudo systemctl stop 'hft-*.timer' 'hft-*.service'
   systemctl list-units 'hft-*' --state=active
   ```

   **You should see** no unit listed.

2. Look, then do.

   ```
   hftdeploy adopt --dry-run
   hftdeploy adopt
   ls -l /home/hft/hft-lab /home/hft/hft-lab/var
   ```

   **You should see** five steps, then `deploy: adopted.`, and both paths shown as links: the first into
   `hft-lab.d/releases/<commit>`, the second to `../../var`.
   **If it stops part way,** run `hftdeploy adopt` again: every step can be done twice. Until it has
   finished, start nothing.

3. Check that the units still find everything, before anything is started.

   ```
   sudo systemd-analyze verify /etc/systemd/system/hft-recorder.service
   sudo hft-status
   ```

   **You should see** the status screen as it was before, with the recordings counted.
   **If the status says nothing was ever recorded,** the state link is wrong. Start nothing and say so.

4. Start the recorder and the timers again ([install](install.md), step 5), and watch the first hour as you
   did after the install.

From here on, never run `hft-install` to move the code, and never `git checkout` in `/home/hft/hft-lab`:
that would change a checkout in place, which is what this layout exists to prevent.

## 1. Look

```
hftdeploy run --commit <hash> --dry-run
```

**You should see** `deploy: the gate is open: <old> to <new>; to restart: ...` and nine steps, each ending
`(dry run: not done)`.
**If it says `REFUSED`,** each line under it is one reason. `is not a commit of main` in a dry run can mean
only that `main` has not been fetched yet: a dry run fetches nothing. The real run fetches first.

## 2. Deploy

Pick a weekend when you can. On a weekday, stay outside the half hour either side of 17:00 New York.

```
hftdeploy run --commit <hash>
```

**You should see**, in this order: `fetch main`, `the gate is open`, `check out`, `build its environment`
(the first deploy after a change of the locked requirements takes minutes; otherwise it is reused), `run
the smoke tests`, `look at the platform's status`, `write down ... what to go back to`, `swap`, one `restart`
for each unit that was running, and `deploy: deployed: <new> is in service`. Exit code 0.

| It ends with | It means | Do |
|---|---|---|
| `deployed: ... is in service` | Done | `sudo hft-status`, then look again after an hour |
| `hft-trader.service is running and was NOT restarted` | The trader still runs the old code. Only possible with the market shut | Restart it yourself when you choose ([trader](trader.md)) |
| `REFUSED. What is in service was not changed.` | The gate, or the smoke tests, stopped it before the swap | Read the reasons. Nothing needs undoing |
| `the status check FAILED after the deploy ... Going back` and exit code 1 | The new code was in service for under a minute and the old code is back | Read the page and `sudo hft-status`. Report it with the output. Do not deploy that commit again |
| `a unit could NOT be restarted` | The old code is back, and a unit did not come up on it either | `systemctl status <unit>`, and [gateway down](gateway-down.md) if it is the connection |

To restart only some units, name them: `--restart hft-recorder.service`. To restart none: the units keep
running the old code until their next start, which is rarely what you want.

## When the unit files changed

The deploy refuses with `unit files of <commit> are not the ones installed: <names>`. It does not install
units: that needs the service manager told. The refusal comes after the commit has been checked out, so the
new files are on the host, in the folder the message names.

```
sudo install -m 0644 /home/hft/hft-lab.d/releases/<commit>/deploy/systemd/<name> /etc/systemd/system/<name>
sudo systemctl daemon-reload
hftdeploy run --commit <hash>
```

Do the three one after the other. Between the second and the third, a unit that restarts runs the old code
under the new unit file. Read the pull request that changed the unit before you do this on a weekday.
A new timer is not enabled by any of this. Enabling one is a step of its own, in the runbook that names it.

## What a deploy leaves behind

| Where | What |
|---|---|
| `/home/hft/hft-lab.d/releases/` | One folder per commit ever deployed. Never changed. `run` and `back` remove none; `prune` removes the old ones (below). Never remove one by hand |
| `/home/hft/hft-lab.d/envs/` | One environment per set of locked requirements. `prune` removes one when no checkout that is kept links to it |
| `/home/hft/hft-lab.d/removing/` | Empty, except while `prune` works or after it was killed: a folder is moved here in one rename before it is removed, and the next `prune` finishes the job |
| `/home/hft/hft-lab.d/last.json` | The commit before and the commit after the last swap: what `back` reads |
| `/home/hft/hft-lab/var/deploy/deploys.jsonl` | Every deploy and every step back, in a hash-chained journal. It is in the backup |

## Removing old checkouts

Every deploy leaves a whole checkout behind, and a change of the locked requirements leaves an environment.
Neither is removed by a deploy. `prune` removes the old ones. Look first:

```
hftdeploy prune --dry-run
```

**You should see** one line for every checkout, `keep <commit>: <why>` or `REMOVE <commit>`, the same for
every environment, then each step ending `(dry run: not done)`, and a count. Then, when the list is what you
expect:

```
hftdeploy prune
```

| It always keeps | Why |
|---|---|
| The checkout in service | The install root links to it |
| The checkout before the last swap, and the one after it | `back` goes to the first. After a `back`, the second is the one you came from |
| Every checkout a running process has its working directory in | A unit that was not restarted runs the checkout it was started in. The trader is never restarted by a deploy, so it can be running a checkout several deploys old |
| The newest of the rest: as many as `deploy.keep_more_releases` in `config/ops.yaml` (2 as shipped, an engineering default) | Something to compare with, and to deploy again without a build |
| Every environment a kept checkout links to | |

| It says | It means |
|---|---|
| `REFUSED ... where the running units have their working directories could not be looked up` | It could not ask the service manager, or could not read a running unit's folder. It removes nothing rather than guess. Run it as you run a deploy, as root |
| `REFUSED ... the deploy journal cannot be read or does not verify` | Which checkout is newer is not known. Nothing was removed. Report it: that journal is never repaired by hand |
| `REFUSED ... another deploy is running` | Wait for it |
| `finish removing what an earlier run left in ...` | An earlier `prune` was killed. Nothing was half removed where checkouts are: this run clears what it left |

The setting is read from the checkout in service. A checkout that was removed can be deployed again: the
mirror still holds its commit, and `run` checks it out afresh.

**Nothing on this page has been followed on the host,** this section included. `prune` has run on temporary
folders, with a stand-in for where the units run.
