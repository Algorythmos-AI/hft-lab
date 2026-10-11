# Runbook: the backup and the restore check

For the owner. The backup hands away what cannot be made again and is small. It never deletes anything, here
or there.

| In the backup | What it is |
|---|---|
| `record/state.json`, `record/<day>/journal.jsonl` | the store's run counter and each recorded day's journal |
| `shadow/*.jsonl` | the shadow runner's journals |
| `alerts/state.json` | which pages are firing |
| `trader/outbox.jsonl` | the order outbox, which is also the broker book's journal: every order and every fill it booked |
| `trader/shadow.jsonl` | the shadow book's journal: the result of record |
| `trader/risk.jsonl`, `trader/reconcile.jsonl` | the latches, and what the reconciler found |
| `trader/stages.jsonl` | each sleeve's stage and its clean days |
| `trader/lineup.jsonl` | the lineup journal: which model is champion, and every change of it |
| `trader/days/<day>/decisions.jsonl` | each trading day's decision journal |
| `learn/models/`, `learn/reports/` | the model registry and the evaluation reports, each file named by its hash |
| `learn/trials.jsonl`, `learn/last-run.json` | the trial ledger, and the word the last training run left |

The trader's folder and the learning folder are taken whole: a file nobody has listed is backed up, not
dropped. Where they are comes from `state_dir` and `registry` in `config/trader.yaml` and `state_dir` and
`lineup` in `config/learn.yaml`.

| Left out | Why |
|---|---|
| `trader/days/<day>/session-*` (the event tapes) | **Not rebuilt.** A tape cannot be made again. It is large, as the recordings are, and is evidence for one check only: the replay of its day. It stays on the host under the tape quota ([the trader runbook](trader.md)). Losing it loses that check for those days, and no record of what was decided or booked |
| `trader/status.json` | rebuilt: the running trader replaces it on every beat |
| `trader/trader.lock` | rebuilt: an empty file that marks that a trader holds the folder |
| `trader/requests/*` | rebuilt: a request not taken yet; one that was lost is asked again |
| any `*.partial` | rebuilt: a file half written, which its writer renames or writes again |

The reports under `var/reports/` are not in the backup either. They are rebuilt: the same journals give the
same report ([reports](reports.md)).

That list is `LEFT_OUT` in `hftlab.ops.backup`, and a test holds a real trader and a real training run to it:
every file they write is in the backup or is left out by a rule that says why.

The recordings themselves are **not** in it. They are large, and they leave through the recorder's own
offload (`offload_dir` in `config/recorder.yaml`), which removes an hour here only after it has confirmed an
identical copy. With no offload configured, the recordings stay on this disk and nowhere else: decide where
they go before the disk budget decides for you.

The shorthand, once per session:

```
hftrun() { sudo -u hft sh -c 'cd /home/hft/hft-lab && PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 exec .venv/bin/python "$@"' sh "$@"; }
```

## Setting it up

1. Make the backup directory. It has to end up somewhere that is not this disk: a mounted volume, or a
   directory you sync away. A backup on the disk it protects is not one.

   ```
   sudo install -d -o hft -g hft -m 0750 /var/backups/hft
   ```

   The job is skipped, without an error, for as long as this directory does not exist.

2. See what would go.

   ```
   hftrun -m hftlab.ops.backup list
   ```

   **You should see** one line a file: `record/state.json`, a `record/<day>/journal.jsonl` for each recorded
   day, `shadow/...` and `alerts/state.json` when they exist, and, once a trader has run or training has
   registered, the `trader/...` and `learn/...` files of the table above. No `.seg` file and no tape.

3. Run it once by hand.

   ```
   sudo systemctl start hft-backup.service
   journalctl -u hft-backup -n 5
   ```

   **You should see** `backup: N file(s) handed to /var/backups/hft/hft-lab/<time>, tagged hft-lab`.
   **If it says FAILED,** the reason is on that line, nothing was deleted, and you get one page
   `hft:backup_failed`, which resolves at the next good run.

4. Enable the nightly run, at 22:30 New York.

   ```
   sudo systemctl enable --now hft-backup.timer
   systemctl list-timers 'hft*'
   ```

   **If 22:30 New York collides with a heavy job of the other desks,** say so: the time is in
   `hftlab.ops.units` and changes by pull request.

Each run writes a new folder named by its time under `/var/backups/hft/hft-lab/`. A folder is a whole backup
when it holds a `MANIFEST.json`, which is written last. Nothing ever removes an old folder: thinning them out
is yours to do, by hand, on the far side.

## The restore check

Do it after the first backup and once a month. A backup that has never been read back is a hope.

1. Check the newest folder against its own manifest: every file there, every size and SHA-256 right.

   ```
   ls /var/backups/hft/hft-lab/
   hftrun -m hftlab.ops.backup verify --folder /var/backups/hft/hft-lab/20261014T023000Z
   ```

   **You should see** `backup: every file in ... matches its manifest`.
   **If it lists a file,** that backup is damaged. Run a new one now, and find out what touched the folder.

2. Check that a restored journal still verifies. Read the copy, not the original.

   ```
   hftrun -m hftlab.shadow --verify /var/backups/hft/hft-lab/20261014T023000Z/shadow/live.jsonl
   ```

   **You should see** `"ok": true`. The copy may be a little shorter than the journal on the host: it was
   taken while the journal was being written, and it is whole up to there.

3. Do the same from the far side: fetch a folder back from wherever the directory is mounted or synced to,
   onto another machine, and run the two checks there. Only that shows the way out works end to end.

## Restoring for real

After a lost disk, the platform is installed again from the repository ([install](install.md)). Then, with
the recorder stopped, the files of the newest whole backup go back to where they came from:
`record/...` under `/home/hft/hft-lab/var/record/`, `shadow/...` under `var/shadow/`, `trader/...` under
`var/trader/` and `learn/...` under `var/learn/`, owned by `hft`. The recordings come back from the offload,
hour by hour, into their day folders. Check every restored day ([the recorder runbook](recorder.md),
"Checking a recorded day") before the recorder is started.

Before the trader is started on restored journals:

1. `hftrun -m hftlab.trader --stages` reads the stage journal: every sleeve is where it was.
2. `hftrun -m hftlab.ops.status` reads the lineup journal: the champion's id is the one you expect, and the
   screen shows no `lineup_journal_broken`. A model the lineup names must be in `var/learn/models/` under its
   hash, or its place runs idle and you are paged.
3. The copies are as old as the backup. Orders and fills after it are not in the restored outbox: the trader
   starts blind, asks the broker what is open and what was executed, and reconciles before it sends anything
   ([the trader runbook](trader.md)). A position the restored book does not know is flagged, not adopted.

The tapes are not restored, because they were not backed up. The days before the loss cannot be replayed.

This has not been drilled, because nothing has been recorded yet. The first drill is done on a spare machine
after the first recorded week, and this section is corrected from it.

## Another tool instead of a directory

`backup.tool` in `config/ops.yaml` names a command that is handed the tag `hft-lab` and a file listing what
to back up. It changes by pull request. A command that would forget, prune or delete is refused when the job
starts. Such a tool needs the network and its own credentials, which the backup unit does not have today: it
is cut off from the network on purpose. Changing that is a reviewed change to the unit, not a setting.
