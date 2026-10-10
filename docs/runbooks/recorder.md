# Runbook: the recorder

For the owner, on the host, once the platform is [installed](install.md). The recorder is one service,
`hft-recorder`. It records every quote of the configured pairs and does nothing else: it holds no object that
could place an order. It runs all week and through the weekend, and comes back by itself after a restart of
the machine.

Commands that run the platform's own Python are written with this shorthand. Paste it once per session:

```
hftrun() { sudo -u hft sh -c 'cd /home/hft/hft-lab && PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 exec .venv/bin/python "$@"' sh "$@"; }
```

## Start and stop

| To | Run | You should see |
|---|---|---|
| Start it, now and at every boot | `sudo systemctl enable --now hft-recorder.service hft-housekeep.timer` | `sudo hft-status`: `recorder   running` within ten seconds |
| Stop it | `sudo systemctl stop hft-recorder.service` | `recorder   stopped`, `saying "stopped"`. It took its time to flush: the last block is on disk |
| Stop it for good | `sudo systemctl disable --now hft-recorder.service` | The same, and it no longer starts at boot |
| Start it again | `sudo systemctl start hft-recorder.service` | `recorder   running` |

Every stop and start is a hole in the recording. The journal explains it as `process_down`, so it is not an
unexplained gap, but the quotes of those seconds are gone. Stop the recorder when you must, and at the weekend
when you can choose.

**If it does not come up:** `systemctl status hft-recorder` and `journalctl -u hft-recorder -n 30`. The exit
code tells you which case this is:

| Code | It means | It is restarted |
|---|---|---|
| 0 | It was asked to stop | No |
| 2 | Refused or misconfigured. `refused by the paper-only lock` is in the journal when the gateway is not a paper gateway: **stop**, and go to [Weekly login](weekly-login.md) | No. It needs you |
| 3 | The quota: see below | Yes, more and more slowly, up to every five minutes |
| 4 | The disk refused a write: see below | Yes, the same way |
| anything else | A crash. The journal has the trace. Tell the maintainers | Yes, the same way |

## Reading the status

```
sudo hft-status
```

| Line | What it tells you |
|---|---|
| `market` | Whether the forex week is open by New York time, and the week it belongs to |
| `recorder` | `running` when its status file is under a minute old and says "recording". `last beat` is the age of that file: the recorder rewrites it every five seconds. `connection RECONCILING` is the good state |
| `records` | Today (the UTC day) from the files on disk; this run from the recorder itself; and the time of the last record |
| `gaps` | Silences in today's recording that nothing explains, and how many silences are open at this moment |
| `disk` | What the recording uses, against its quota |
| `loop` | How long the recorder is away from the feed between two polls: the longest of this run, and the longest of the last five seconds |
| `shadow` | [The shadow run](shadow-run.md), if there is one |
| `switches` | [KILL and FLATTEN](kill-and-flatten.md) |
| `health` | `OK`, `WARN` or `BAD`, and under it one line for each thing that is wrong |

It ends with a code you can use in a script: 0 nothing is wrong, 1 something is, 3 **the dead-man rule**:
the recorder last said "recording" and has been silent for over a minute while the market is open. That
means it is hung or was killed without a word. `sudo hft-status --json` prints the same reading for a
program.

## Checking a recorded day

```
hftrun -m hftlab.replay --day var/record/2026-10-14 --check
```

**You should see** `"ok": true` and `"unexplained_gaps": 0`. It re-reads every block, verifies every
checksum, and compares the gap journal with what the records imply.
**If `ok` is false:** do not touch the day folder. Copy the output to the maintainers. A damaged block is
never repaired and never read past.

## A gap

Every silence longer than five seconds has a reason in the journal: the market was closed, the recorder was
down, the connection was lost, or it is unexplained. Only the last kind matters.

```
sudo hft-status
hftrun -m hftlab.replay --day var/record/2026-10-14 --check
sudo -u hft grep '"reason": "unexplained"' /home/hft/hft-lab/var/record/2026-10-14/journal.jsonl
```

**You should see** nothing from the last command on a good day.
**If there are lines:** each has the pair and the two times, in nanoseconds since 1970. Look in
`journalctl -u hft-recorder` and `journalctl -u hft-gateway` at those times, and in the status `loop` line
for a long pause. Write down what you find and give it to the maintainers: an unexplained gap is counted
against the F2 gate, and its cause is worth more than its repair. Nothing can fill it afterwards.

## The quota

The recorder stops by itself, loudly, rather than write past its quota (`quota_bytes` in
`config/recorder.yaml`). You get a page `hft:recorder_quota`, the status says `saying "quota"`, and the unit
tries again every few minutes.

1. See what takes the room.

   ```
   sudo hft-status
   sudo -u hft du -sh /home/hft/hft-lab/var/record/*
   ```

2. Let the housekeeping compress what is closed. It deletes nothing unless an offload has confirmed its
   copy.

   ```
   sudo systemctl start hft-housekeep.service
   journalctl -u hft-housekeep -n 3
   ```

   **You should see** `record: compressed` and a number. The recorder then starts by itself within five
   minutes, or at once with `sudo systemctl restart hft-recorder.service`.

3. **If that was not enough,** the quota or the offload has to change, and both are a pull request: a larger
   `quota_bytes` only as far as the pre-flight's disk budget allows, or an `offload_dir` on another disk so
   that old hours can leave. Then [deploy](rollback.md). Never delete a segment by hand to make room: the
   recordings cannot be bought back.

## The disk is full, or a write failed

This is not the quota. The operating system refused a write: no space left, or an I/O error. You get a page
`hft:recorder_io_error`, the status says `saying "io-error"`, and the journal has
`RECORDER STOPPED: I/O ERROR` with the system's own name for it (`ENOSPC` is a full disk, `EIO` a failing
one). The unit tries again, more and more slowly, up to every five minutes.

1. Whose disk is full?

   ```
   df -h /
   sudo -u hft du -sh /home/hft/hft-lab/var
   ```

   **If the disk is full and it is not ours,** the other desks are in trouble too. That comes first: free
   their space by their own runbooks. Ours waits, and starts again by itself when there is room.
   **If it is ours,** treat it as the quota above. And tell the maintainers: the quota exists so that this
   cannot happen, so the quota is wrong.

2. `EIO` means the disk or its driver, not space. Look at `journalctl -k --since -1h`. The recorder cannot
   fix that, and neither can a restart.

3. When there is room again:

   ```
   sudo systemctl restart hft-recorder.service
   sudo hft-status
   ```

   **You should see** `recorder   running`. The run number is one higher. Nothing was duplicated: the new
   run continues after the last block that is whole on disk, and what the disk had cut short was taken back.

## After a kill, a crash or a power cut

Nothing is needed. The unit starts the recorder again within seconds, and at boot. A kill loses at most the
last quarter of a second, or one block. On starting, the recorder cuts off a block the kill left half
written, and journals the hole as `process_down`.

```
sudo hft-status
journalctl -u hft-recorder -n 20
```

**You should see** `recorder   running`, a run number one higher, and in the journal the line
`record: recording` followed by the pairs.
**If you get the dead-man page** (`hft:recorder_stale`) and the status still shows it stopped, the unit is
not restarting it. `systemctl status hft-recorder` says why. `sudo systemctl restart hft-recorder.service`
is always safe.

## Housekeeping

`hft-housekeep.timer` runs every hour. It compresses the hours that are closed and, when an offload is
configured, hands over the hours older than the retention window and removes them here only after the offload
has confirmed an identical copy. With no offload configured, it never deletes anything.

```
systemctl list-timers 'hft*'
journalctl -u hft-housekeep -n 5
```

**You should see** a next run within the hour and `record: compressed N, handed over N, kept N`.
**`kept` above zero** means the offload took a file and could not confirm it. The original is still here.
Look at the offload directory before anything else.
