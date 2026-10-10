# Runbook: a shadow run

For the owner. A shadow run lets the sleeves decide on real quotes and sends their orders to the shadow venue,
our own simulation with its latency, spread and commission model. **No mode of it sends an order to a
broker**: the runner builds the shadow venue itself and can be handed no other.

Until the capability probe has replaced the cost model's numbers, every result here is on placeholder costs,
and the tools say so on every run. A result that is to be compared with anything is a registered experiment
([research](../../research/README.md)), run by its own protocol, not from this page.

The shorthand, once per session:

```
hftrun() { sudo -u hft sh -c 'cd /home/hft/hft-lab && PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 exec .venv/bin/python "$@"' sh "$@"; }
```

## Over a recorded day

1. Check the day first. A shadow run on a damaged day is worth nothing.

   ```
   hftrun -m hftlab.replay --day var/record/2026-10-14 --check
   ```

   **You should see** `"ok": true`.

2. Run it.

   ```
   hftrun -m hftlab.shadow --day var/record/2026-10-14
   ```

   **You should see** `shadow: session 1 over recording:2026-10-14` and then the result as JSON: for each
   sleeve its decisions, orders, fills and, per pair, the net result in millionths of the quote currency.
   The `idle` sleeve must show zero everywhere. The journal is `var/shadow/2026-10-14.jsonl`.
   `the cost model is a PLACEHOLDER` on the first line means what it says.

   **If it says** `already holds a journal`: a recorded day is run into a new file each time. Add
   `--journal var/shadow/2026-10-14.second.jsonl`. Several days as one session: name them in order after
   `--day`.

3. Verify the journal. Every line is chained to the one before by a hash.

   ```
   hftrun -m hftlab.shadow --verify var/shadow/2026-10-14.jsonl
   ```

   **You should see** `"ok": true` and the number of entries.
   **If it exits with 1,** the journal has been changed or damaged. It is never repaired. Keep the file as
   it is, run the day again into a new file, and tell the maintainers.

## The feasibility map

```
hftrun -m hftlab.feasibility --store var/record --out var/feasibility.json
```

**You should see** the file written, and `PLACEHOLDER costs or rule` for as long as that is true. The map
says, for each pair and holding time, whether the market moved by more than a round trip costs. On
placeholder costs no cell of it is a result.

## On live quotes

`hft-shadow.service` runs the same sleeves on the gateway's quotes. It is a second reader of the feed beside
the recorder, and it is not needed for recording. Start it when the recorder has run cleanly for some days.

```
sudo systemctl enable --now hft-shadow.service
sudo hft-status
```

**You should see** a `shadow` line with one session, and under it a line for each sleeve with its counts.
Every start is a new session that begins flat; the journal `var/shadow/live.jsonl` continues.

| To | Run |
|---|---|
| Stop it | `sudo systemctl stop hft-shadow.service` |
| See what it does | `journalctl -u hft-shadow -n 20` |
| Verify its journal | `hftrun -m hftlab.shadow --verify var/shadow/live.jsonl` |

**If the status says `shadow_journal_broken`,** the service has stopped with exit code 1 and is not started
again: it will not write onto a journal that does not verify. Move the journal aside, do not delete it, and
start again. A new journal begins.

```
sudo -u hft mv /home/hft/hft-lab/var/shadow/live.jsonl /home/hft/hft-lab/var/shadow/live.broken-20261014.jsonl
sudo systemctl start hft-shadow.service
```

Then tell the maintainers, with the broken file: a chain does not break by itself.

**If it stops with exit code 2,** it was refused or is misconfigured, like the recorder
([the recorder runbook](recorder.md), "Start and stop").
