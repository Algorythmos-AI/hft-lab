# Runbook: the KILL and FLATTEN switches

For the owner. The two switches are files in the platform's state directory on the host,
`/home/hft/hft-lab/var/`. A switch is set while its file exists.

| Switch | File | What it means |
|---|---|---|
| `KILL` | `/home/hft/hft-lab/var/KILL` | No new entries. Exits are still managed |
| `FLATTEN` | `/home/hft/hft-lab/var/FLATTEN` | Every position is closed |

**What works today, and what does not.** Nothing of this platform places a broker order yet, so the switches
have nothing to stop: the risk gate that obeys them arrives with paper trading (F4). What does work today is
that the status screen shows them and the status snapshot publishes them. Setting one now is harmless, and
it is how this page is drilled.

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

This section is written when the risk gate is: what each switch does to working orders, how long FLATTEN
takes, how to tell it has finished, and when it is safe to clear it. Until then, do not rely on either switch
to stop an order, because there is no order.
