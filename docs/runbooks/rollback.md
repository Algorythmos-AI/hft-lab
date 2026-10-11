# Runbook: going back

For the owner, on the host. Going back puts the code that was in service before the last deploy in service
again. It is one command, and it asks the same gate a deploy asks
([deploy runbook](deploy.md), [ADR 0027](../adr/0027-deploy-gate-and-reports.md)).

**Nothing on this page has been followed on the host.**

```
hftdeploy() { ( cd /home/hft/hft-lab && sudo env PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m hftlab.ops.deploy "$@" ); }
```

## It may already have happened

A deploy checks the platform's status after it has restarted the units. If it finds a problem that was not
there before, or a unit that did not come up, it goes back by itself: you get one page, `hft:deploy`, the
command ends with exit code 1, and the old code is in service. There is nothing to do but read why.

## Going back by hand

Use this when a deploy passed its check and you want the old code back all the same.

1. Look.

   ```
   hftdeploy back --dry-run
   ```

   **You should see** `swap /home/hft/hft-lab back from <new> to <old>` and one `restart` for each unit
   that is running, each ending `(dry run: not done)`.
   **If it says `no deploy is on record here`,** or `there is nothing to go back to`, the code in service
   is not the one the last deploy put there. To reach an older commit, deploy it: step 3.

2. Go back.

   ```
   hftdeploy back
   sudo hft-status
   ```

   **You should see** `deploy: <old> is in service again`, and the recorder running with a run number one
   higher. The recording continues: the new run starts after the last record on disk, whichever code wrote
   it.

   **If it says `REFUSED`,** the gate is shut, for one of the reasons a deploy is refused for: the broker
   book is not flat, the trader is running with the market open, or the recorder would be restarted within
   30 minutes of 17:00 New York. Nothing was changed. The checks of the old commit are not asked again: it
   passed them when it was deployed.

   **If it says `the status check FAILED`,** the old code is in service and something is still wrong. The
   command does nothing more by itself: it will not swing back and forth. `sudo hft-status` says what.

3. Further back than one step. `back` goes back once. An older commit of `main` is deployed like any other:

   ```
   hftdeploy run --commit <older hash>
   ```

   Its environment is still on the host under `/home/hft/hft-lab.d/envs/`, so nothing is built, unless
   `prune` has removed it since ([deploy](deploy.md), "Removing old checkouts"); then it is built again,
   which takes minutes.

## The trader

Going back never restarts, starts or stops `hft-trader.service`. If it is running, which the gate allows
only with the market shut and every sleeve flat, it keeps running the code it was started on, and the
command says so. Restart it yourself ([trader](trader.md)).

## What going back cannot undo

- **A change of the recording format.** A reader refuses a format version it does not know, by design. If
  the commit you are leaving introduced a new version (its pull request says so), ask the maintainers before
  you go back: what it recorded has to stay readable.
- **What the newer code wrote into the journals.** They are only ever appended to. Older code reads the
  lines it knows.
- **Unit files.** If you installed new unit files for the deploy ([deploy](deploy.md), "When the unit files
  changed"), put the old ones back the same way, from the old commit's folder under `releases/`, before you
  go back: the gate compares them.

**If the platform does not start on either commit,** leave it stopped and say so. A stopped recorder costs
data; a recorder started in a state nobody understands costs more.

## Going back to a model

Nothing. Going back to the previous champion model is not a deploy: the weekly decision does it by itself
when a champion trips its loss latch, and the drill proves it on demand
([learning runbook](learning.md), section 6).
