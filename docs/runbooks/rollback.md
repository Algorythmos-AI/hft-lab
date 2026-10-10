# Runbook: a deploy, and going back

For the owner, on the host. A deploy moves the installed code to another commit of `main`. Going back is the
same steps with the commit you came from.

The shorthand, once per session:

```
hftrun() { sudo -u hft sh -c 'cd /home/hft/hft-lab && PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 exec .venv/bin/python "$@"' sh "$@"; }
```

## Rules

| Rule | Why |
|---|---|
| Only a commit on `main` whose checks (`test`, `leakcheck`) are green | It is the only code that was reviewed. The install script refuses anything else |
| At the weekend when you can choose | The recorder is stopped for about a minute, and that minute is a hole in the recording |
| Only when flat | Today nothing trades. From paper trading on: no open position and no working order |
| One step at a time, reading each result | A deploy that is half done is still a working platform at every step below |

## Steps

1. **Write down where you are.** This is what you go back to.

   ```
   sudo -u hft git -C /home/hft/hft-lab rev-parse HEAD
   sudo hft-status
   ```

2. **Fetch, and choose the commit.**

   ```
   sudo -u hft git -C /home/hft/hft-lab fetch origin
   sudo -u hft git -C /home/hft/hft-lab log --oneline -5 origin/main
   ```

   **You should see** the commit you mean to deploy. Check on GitHub that its two checks are green.

3. **Stop what runs.**

   ```
   sudo systemctl stop hft-shadow.service hft-recorder.service
   ```

   **You should see**, with `sudo hft-status`, `recorder   stopped` and `saying "stopped"`.

4. **Move the code.**

   ```
   sudo -u hft git -C /home/hft/hft-lab checkout --detach <commit>
   ```

5. **Bring the environment and the units in line.** First look, then do. The environment is rebuilt only if
   the locked requirements changed, and only the unit files that differ are written again.

   ```
   sudo sh /home/hft/hft-lab/deploy/bin/hft-install --dry-run
   sudo sh /home/hft/hft-lab/deploy/bin/hft-install
   ```

   **You should see** `already at` and the commit in step 3 of its output, and `hft-install: done.`
   **If it says REFUSED,** nothing was changed. `is not on origin/main` means the commit of step 4 is not a
   commit of `main`.

6. **Start again.**

   ```
   sudo systemctl start hft-recorder.service
   sudo hft-status
   hftrun -m hftlab.ops.units diff
   ```

   **You should see** `recorder   running`, a run number one higher, and
   `units: the installed units match the code`. Start `hft-shadow.service` too if it was running.

7. **Look again after an hour,** and at the day's check the next morning
   ([the recorder runbook](recorder.md)).

## Going back

Do steps 3 to 6 with the commit you wrote down in step 1.

**You should see** the same results. The recording continues: the new run starts after the last record on
disk, whichever code wrote it.

**Do not go back across a change of the recording format.** A reader refuses a format version it does not
know, by design. If the commit you are leaving introduced a new version (its pull request says so), ask the
maintainers before you go back: what it recorded has to stay readable.

**If the platform does not start on either commit,** leave it stopped and say so. A stopped recorder costs
data; a recorder started in a state nobody understands costs more.

## Not here yet

Going back to the previous champion model is part of the learning loop (F6) and gets its own section then.
