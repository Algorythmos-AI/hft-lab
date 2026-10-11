# Runbook: installing on the host

For the owner, on the shared host, over your usual route in. Three sittings: ten minutes to start the
measurement, a day of waiting, half an hour to install. It covers the pre-flight, the install and the first
start of the recorder. Nobody else does any of it, and nothing here needs a secret.

**Nothing has been installed anywhere yet.** The units and the scripts have been tested off the host only, in
dry runs against a temporary directory. This run is their first on a real machine. Where the text says "tell
the maintainers", the step is one of the things this run is there to find out.

## Never

| Never | Why |
|---|---|
| Install without a pre-flight that passed | The host runs two other desks. Under 1.5 GB of headroom, the platform is not installed ([ADR 0007](../adr/0007-shared-host-isolation.md)) |
| Run the scripts from a branch, or from a checkout with edits | Only a reviewed commit on `main` is installed. The script refuses anything else |
| Stop, restart, edit or enable a unit that does not start with `hft` | The other desks' units are not ours to touch |
| Put an address, a key, a login or an account number into this repository | It is public. The secrets file is `/etc/hft/secrets.env`, and only you write it |
| Record a dry run into `var/record` | A dry run of the fake broker belongs in a throwaway directory (step 4) |

## Before you start

| You need | Check | If it is missing |
|---|---|---|
| Python 3.12 or newer | `python3 --version` | Install it from the distribution. The gate runs on it |
| git | `git --version` | Install it from the distribution |
| uv 0.8 or newer, for every user | `command -v uv` shows a path that is not under your home; `uv --version` | Install uv system-wide yourself, from its own instructions. The script never downloads it |
| systemd 254 or newer | `systemctl --version`, first line | With an older one the growing restart wait is ignored and a flat 5 s is used. Tell the maintainers before you go on |

## 1. Get the code

```
git clone https://github.com/Algorythmos-AI/hft-lab.git ~/hft-lab-src
cd ~/hft-lab-src
git log -1 --format='%H %s'
```

**You should see** the commit that is at the head of `main` on GitHub, with both of its checks green (`test`
and `leakcheck`).

**If not:** `git fetch origin && git checkout --detach origin/main`, and look again. Do not go on from a
commit whose checks are not green.

## 2. The pre-flight: one day of measurements

1. Try the sampler. It writes nothing in this mode.

   ```
   sh deploy/bin/hft-preflight-sample --dry-run
   ```

   **You should see** four lines of what it would do, then one line that starts with the time and ends in
   `disk_free_kb=` and a number.
   **If not:** `this is for a Linux host` means you are on the wrong machine.

2. Start it. Pick a normal trading day for the other desks, so that their heaviest jobs fall inside the 24
   hours.

   ```
   nohup sh deploy/bin/hft-preflight-sample --out "$HOME/hft-preflight.log" >/dev/null 2>&1 &
   ```

   **You should see**, with `tail -2 ~/hft-preflight.log`, a line starting `# hft-preflight-sample` and one
   sample, and a new sample every minute. It stops by itself after 1440 samples.
   **If not:** `pgrep -f hft-preflight-sample` shows whether it runs. It reads `/proc` and the free space on
   `/`, and writes only that one file.

3. A day later, judge it.

   ```
   PYTHONPATH=src python3 -m hftlab.ops.preflight --check "$HOME/hft-preflight.log"
   ```

   **You should see** `pre-flight: PASS: the platform may be installed` and six lines marked `[ok]`.
   Read the `memory headroom` line: it names the tightest minute of the day. Then read the last line and
   **check that the other desks' heaviest jobs really ran inside the sampled day.** The tool cannot know that.

   **If it says FAIL, the platform is not installed.** What each `[NO]` means:

   | Line | What to do |
   |---|---|
   | `memory headroom` | Stop. This is the gate doing its job. The choice is yours: a larger host, or the route without a gateway process ([ADR 0010](../adr/0010-access-route.md)) |
   | `disk floor` | Free disk on the host, or have the recording quota lowered by a pull request, then sample again |
   | `samples`, `a full day`, `longest hole` | The measurement is not a whole day. Start the sampler again and wait a full day |
   | `how old` | The file is over a week old, or the clock is wrong. Sample again |

## 3. Install

1. Look at what it would do. Nothing is changed.

   ```
   sudo sh deploy/bin/hft-install --preflight "$HOME/hft-preflight.log" --dry-run
   ```

   **You should see** seven numbered headings, a `+` in front of every command it would run, and a last line
   `DRY RUN finished. Nothing was changed.` Read the commands. Every path is under `/home/hft`, `/etc/hft`,
   `/etc/systemd/system/hft*` or `/usr/local/bin/hft-status`.

   **If it says REFUSED:**

   | It says | Cause |
   |---|---|
   | `no pre-flight samples` or `cannot read` | The `--preflight` file is missing or mistyped |
   | `the pre-flight gate did not pass` | Step 2.3 failed. The platform is not installed |
   | `is not on origin/main` | You are on a branch or an unpushed commit. Step 1 |
   | `has uncommitted changes` | The checkout was edited. `git status`, then `git checkout -- .` or clone again |
   | `uv was not found` | See "Before you start" |
   | `is missing or older than 3.12` | Set `HFT_PYTHON3` to a newer Python, for instance `sudo HFT_PYTHON3=/usr/bin/python3.12 sh ...` |

2. Install. It enables nothing and starts nothing.

   ```
   sudo sh deploy/bin/hft-install --preflight "$HOME/hft-preflight.log"
   ```

   **You should see** the same steps, now carried out, and `hft-install: done.` The download of the packages
   in step 4 takes a few minutes.
   **If a step fails,** the script stops there. Nothing is half-enabled, because nothing is ever enabled.
   Fix the cause and run the same command again: what is done is skipped.

   **The optional ML set.** Leave it out unless this host is to train a gradient-boosted model
   (`model: gbm` in `config/learn.yaml`; the shipped value is `ridge`). Only `hft-train.service` uses it.
   The recorder, the trader and the weekly decision never import it, and a gbm model that is already
   trained is scored without it. To add it, give `--with-ml`, at the first install or at any later time:

   ```
   sudo sh deploy/bin/hft-install --with-ml
   ```

   **You should see** under step 4 one more `uv pip install` line, with `requirements-ml.lock.txt`, the
   line that stamps it, and nothing else done again. It installs LightGBM and what it needs, every package checked against its hash.
   It takes about 80 MB of disk as measured on a Mac; on this host expect somewhat more, and check with
   `du -sh /home/hft/hft-lab/.venv` before and after. No service has to be stopped for it.
   It needs the system's OpenMP runtime: `dpkg -s libgomp1` should say `install ok installed`; if not,
   `sudo apt install libgomp1`.
   **Give `--with-ml` on every later run of the script too.** An environment that is rebuilt because the
   locked requirements changed comes back without the set, and the next gbm training run is then refused
   with a message that says so.

3. Look at the result.

   ```
   sudo hft-status
   systemctl list-unit-files 'hft*'
   sudo systemd-analyze verify /etc/systemd/system/hft-recorder.service
   ```

   **You should see** `recorder   never started` and `health     OK`; ten unit files, each `disabled` or
   `static`; and nothing from the last command.
   **If the last command prints a warning** about a key it does not know, copy it to the maintainers. The
   unit still loads, without that one setting.

4. Run the dry run once more, without the samples.

   ```
   sudo sh deploy/bin/hft-install --dry-run
   ```

   **You should see** no line with a `+`: there is nothing left to do. The gate is not asked again, because
   its report is kept in `/etc/hft/preflight-passed.json`.

## 4. Before the first start

| This must be true | Where |
|---|---|
| The capability probe has been run and read | [IBKR capability probe](ibkr-probe.md). It closes the F1 gate |
| The gateway runs on this host in paper mode, on port 4002 | [Weekly login](weekly-login.md) |
| `config/recorder.yaml` lists the pairs you want recorded | A pull request, then [a deploy](rollback.md) |

Then prove the recorder runs here at all, against the built-in fake broker, in a throwaway directory:

```
sudo -u hft sh -c 'cd /home/hft/hft-lab && PYTHONPATH=src .venv/bin/python -m hftlab.record --dry-run-seconds 30 --root /tmp/hft-dry-run'
```

**You should see** `record: stopped after` and a number in the thousands.
**Never leave out `--root` here.** A dry run into `var/record` would put made-up quotes beside real ones.
The status would then refuse to call the store the market's (`recorder_not_on_paper_venue`).

## 5. The first start of the recorder

1. Enable and start the recorder and its hourly housekeeping. Nothing else.

   ```
   sudo systemctl enable --now hft-recorder.service hft-housekeep.timer
   ```

2. Within ten seconds:

   ```
   sudo hft-status
   ```

   **You should see** `recorder   running; last beat` and a few seconds, `saying "recording"`,
   `venue ibkr_paper`, `connection RECONCILING`, and a `records` line that grows when you run it again.
   `RECONCILING` is right and stays: the recorder holds a view that can never be made ready for orders.

   **If not:**

   | You see | Cause, and what to do |
   |---|---|
   | `recorder   never started`, and `systemctl status hft-recorder` shows `status=2` | It was refused or is misconfigured, and it is not restarted. `journalctl -u hft-recorder -n 20` says which. `refused by the paper-only lock` means the gateway is not a paper gateway: **stop**, and go to [Weekly login](weekly-login.md) |
   | `status=226/NAMESPACE` | The state directory is missing. Run the install again |
   | `connection DISCONNECTED` or `CONNECTING` | The recorder runs and the gateway does not answer. [Gateway down](gateway-down.md) |
   | `Unknown key name` in `journalctl -u hft-recorder` | An older systemd. The recorder runs. Tell the maintainers |

3. In the first hour, look three times:

   ```
   sudo hft-status
   journalctl -k --since -1h | grep -i -c 'out of memory'
   ```

   **You should see** the `loop` line of the status, which says how long the recorder is away from the feed
   between two polls. Write the `longest` value down: it is the first measurement of what syncing to disk
   costs. The second command should print `0`, and the other desks' own status should be as it was.
   **If the other desks suffer:** `sudo systemctl stop hft.slice` stops every unit of ours at once. Then tell
   the maintainers what you saw.

4. After the first full hour, the housekeeping has run:

   ```
   systemctl list-timers 'hft*'
   journalctl -u hft-housekeep -n 3
   ```

   **You should see** `record: compressed 1, handed over 0, kept 0`.

From here on, [the recorder runbook](recorder.md).

## After the install

Three things are written and tested off the host, and are **not** done by the install script. Nothing below
has been done on any host.

| What | It is | Where |
|---|---|---|
| The deploy gate | From the first deploy on, the code moves through `python -m hftlab.ops.deploy` and no longer through this script. It needs the install root turned into a link once, with everything stopped (`adopt`). Until you have done that, a deploy is refused and says so | [deploy](deploy.md), [rollback](rollback.md) |
| The reports | `hft-report.timer` is installed with the other units and is enabled by nobody. This script refuses `--enable hft-report.timer`: a page every trading day is yours to ask for | [reports](reports.md) |
| The tapes | The hourly housekeeping also compresses the closed parts of the trader's tapes, once a trader has run. The tape quota in `config/trader.yaml` is a placeholder until this host's disk has been measured with a trader running | [trader](trader.md), section 8 |

The nightly backup takes the trader's folder and the learning folder as well, once they exist
([backup](backup.md)).

## What this run confirms

The host layer was written without the host. Each line is something it assumes. Tell the maintainers which,
if any, did not hold.

| Assumption | How the run shows it |
|---|---|
| The host's systemd knows every setting in the units | `systemd-analyze verify` prints nothing, and the journal has no `Unknown key name` |
| The sandbox does not stop Python or the gateway connection | The recorder reaches `connection RECONCILING` |
| The two gateway-reaching services need only this machine's address | The same |
| 300 MB is enough for the recorder | `systemctl show hft-recorder -p MemoryCurrent` stays well under it over a day |
| The slice's cap and weights keep the other desks unharmed | Their own status over the first day |
| The other desks' heaviest jobs ran inside the sampled day | Your own check in step 2.3 |
| 22:30 New York is a quiet time for the nightly backup | Your knowledge of the other desks' evening jobs. Say so if it is not |
| The paper gateway is reachable on this machine on port 4002 | `connection RECONCILING` |
| `uv` can fetch Python 3.12 and the locked packages as the user `hft` | Step 3.2 completes |
