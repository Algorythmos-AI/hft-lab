# Runbook: the KILL and FLATTEN switches

For the owner. The two switches are files in the platform's state directory on the host,
`/home/hft/hft-lab/var/`. A switch is set while its file exists.

| Switch | File | What it means |
|---|---|---|
| `KILL` | `/home/hft/hft-lab/var/KILL` | No new entries. Exits are still managed |
| `FLATTEN` | `/home/hft/hft-lab/var/FLATTEN` | Every position is closed |

**What works today, and what does not.** The trader reads both switches once a second and obeys them, on
paper and in shadow ([trader runbook](trader.md), section 4). As shipped it runs in shadow-only mode, so
there is no broker order for a switch to stop yet: what a switch stops today is the shadow venue's entries.
The status screen shows the switches and the status snapshot publishes them. Setting one now is harmless,
and it is how this page is drilled.

## Set and clear

| To | Run | You should see |
|---|---|---|
| Set KILL | `sudo -u hft touch /home/hft/hft-lab/var/KILL` | `sudo hft-status`: `switches   KILL SET; FLATTEN off`. On the status site within five minutes |
| Clear KILL | `sudo -u hft rm /home/hft/hft-lab/var/KILL` | `KILL off` |
| Set FLATTEN | `sudo -u hft touch /home/hft/hft-lab/var/FLATTEN` | `FLATTEN SET` |
| Clear FLATTEN | `sudo -u hft rm /home/hft/hft-lab/var/FLATTEN` | `FLATTEN off` |

**If the status does not change,** you made the file somewhere else. It must be in
`/home/hft/hft-lab/var/` exactly, with that name in capitals.

A switch is your decision, not a fault: it is shown, and it raises no page.

## Stopping what does run today

| To stop | Run | Note |
|---|---|---|
| The shadow runner | `sudo systemctl stop hft-shadow.service` | It decides and sends nothing to a broker. Stopping it loses nothing |
| The recorder | `sudo systemctl stop hft-recorder.service` | Only if you must: every stopped second is a hole in the recording |
| Everything of ours, at once | `sudo systemctl stop hft.slice 'hft-*.timer'` | For when the other desks are suffering and ours has to go. Start again unit by unit |

**You should see**, with `systemctl list-units 'hft*'`, nothing active after the last one.

## From paper trading on (F4)

It is section 4 of the [trader runbook](trader.md): what each switch does with a trader running, how to tell
that `FLATTEN` has finished, and when it is safe to clear it. In short: `KILL` stops everything that adds to
a position and leaves exits alone; `FLATTEN` also closes every open line through the risk gate, trying a
refused close again on a fixed interval; neither cancels a working order, and stopping the process does not
make you flat. The trader closes everything by the `FLATTEN` path on its own, with no file set, when its
event tapes reach their hard limit (trader runbook, section 8). None of it has been drilled on the host.
