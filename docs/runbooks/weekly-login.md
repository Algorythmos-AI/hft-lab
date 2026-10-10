# Runbook: the gateway and its weekly login

For the owner, and only for the owner: this is the one place where the broker's paper login is used, and
nobody else ever types it. The recorder and the shadow runner connect to the broker's gateway on this machine.
The gateway holds the session. A session lasts about a week, and the gateway restarts itself once a day.

**This runbook has not been exercised.** No gateway has been installed on the host. The unit
`hft-gateway.service` in this repository is a placeholder that describes the arrangement; it cannot be
enabled, and nothing here starts it. Your first set-up and your first weekly login are the drill. Correct this
page in the same pull request as your notes.

## Never

| Never | Why |
|---|---|
| Log the gateway in with the live username | The lock refuses a live account, but the safest live session is the one that does not exist. The host holds a paper-only login and nothing else |
| Put the login into this repository, a unit file, or `/etc/hft/secrets.env` | It lives in the controller's own configuration and nowhere else |
| Use port 4001 or 7496 | Those are the live ports. The lock refuses them by name |
| "Just try" a different login when the recorder is refused | A refusal by the lock is a statement about where the gateway is pointed. Find out why first |

## What has to be on the host

| Thing | Where | Whose |
|---|---|---|
| The broker's gateway, the stable version, from the broker's own site | Your choice of directory, readable by `hft` | Yours to install |
| A standard third-party controller that starts the gateway, logs it in and handles its daily restart | Your choice | Yours to install, from its own project |
| The controller's configuration, which holds the **paper** username and password | `/etc/hft/gateway/config.ini`, owner `root`, group `hft`, mode `0640` | Yours to write. The unit names this path and nothing of its contents |
| A start script of a few lines that runs the controller in paper mode with that configuration | `/etc/hft/gateway/start`, mode `0750`, group `hft` | Yours to write |

In the gateway's API settings: socket port `4002`, connections from this machine only. Read-only API may stay
**on** for as long as only the recorder and the shadow runner connect: neither sends an order.

The unit starts only when both files exist. It runs as `hft`, inside `hft.slice`, with a memory fuse of 900 MB.
What the gateway really needs is not measured yet ([ADR 0010](../adr/0010-access-route.md)); the first days
will show whether that fuse and the slice's 1.2 GB hold.

## The first start

```
sudo systemctl start hft-gateway.service
systemctl status hft-gateway
ss -ltn | grep 4002
```

**You should see** the unit `active (running)` and a listener on `127.0.0.1:4002`.
**If `systemctl status` says the condition failed,** one of the two files in `/etc/hft/gateway/` is missing
or the script is not executable.

Then, with the recorder running:

```
sudo hft-status
journalctl -u hft-recorder -n 20
```

**You should see** `connection RECONCILING`.
**If the journal says `refused by the paper-only lock`,** stop the gateway now:
`sudo systemctl stop hft-gateway.service`. The message says which rule failed. `is not a paper account id` or
`is a live account id` means the controller is configured with a login that is not paper-only. Fix that
before anything is started again. The recorder is not restarted after a refusal:
`sudo systemctl start hft-recorder.service` when the cause is gone.

## Every week

The broker ends the session about once a week, at the weekend. Do this on Saturday or on Sunday before the
market opens at 17:15 New York.

1. Restart the gateway, so that the controller logs in afresh. Approve the second-factor prompt on your
   phone if the paper login has one.

   ```
   sudo systemctl restart hft-gateway.service
   ```

2. Two minutes later:

   ```
   sudo hft-status
   ```

   **You should see** `connection RECONCILING`. The recorder found the gateway again by itself; it retries on
   a growing wait and needs no restart.
   **If it stays `DISCONNECTED`,** go to [Gateway down](gateway-down.md).

3. After the Sunday open, look once more. The `records` line should be growing and `gaps` should show no
   unexplained gap.

What the weekly reset really looks like on this account, and whether the session can be kept longer, is
something the capability probe and the first weeks will show. Write it into `config/venues/ibkr.yaml`
(`weekly_login`) by pull request when you know.
