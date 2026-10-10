# Runbook: the gateway is down

For the owner. You are here because of a page `hft:connection_down`, or because the status shows
`connection DISCONNECTED` or `CONNECTING` while the market is open.

What is already happening without you: the recorder keeps running and keeps trying, on a growing wait. The
silence is written to the journal as `connection_lost`, so it is an explained gap. Nothing of ours trades, so
nothing is at risk but the recording.

**This runbook has not been exercised:** no gateway has run on the host yet
([Weekly login](weekly-login.md)). Correct it after the first real outage.

## Never

| Never | Why |
|---|---|
| Point anything at port 4001 or 7496 "to see if that works" | Those are the live ports |
| Start the gateway in live mode to test the connection | The host holds a paper-only login. Keep it so |
| Restart the recorder over and over | It reconnects by itself. A restart only adds a second hole |

## Steps

1. **Is it the gateway's own daily restart, or the weekend?** Both end by themselves.

   ```
   sudo hft-status
   ```

   **You should see** `market     closed` at the weekend: then this is not a fault, and the page clears
   itself. For the daily restart, wait five minutes and look again.

2. **Is the gateway running?**

   ```
   systemctl status hft-gateway
   journalctl -u hft-gateway -n 50
   ```

   | You see | What to do |
   |---|---|
   | `active (running)` and a login prompt or a session-expired message in the journal | [Weekly login](weekly-login.md), "Every week" |
   | `inactive` or `failed` | `sudo systemctl start hft-gateway.service`, and read the journal if it fails again |
   | The condition failed | A file in `/etc/hft/gateway/` is missing: [Weekly login](weekly-login.md), "What has to be on the host" |
   | Killed for memory (`oom-kill` in `journalctl -k`) | The fuse of 900 MB, or the slice's 1.2 GB, is too small for the gateway. Do not raise it by hand: tell the maintainers, with the numbers. It is the question ADR 0010 left open |

3. **Is it listening?**

   ```
   ss -ltn | grep 4002
   ```

   **You should see** a listener on `127.0.0.1:4002`.
   **If not,** the gateway is up and its API is not: its settings (socket port, API enabled) were lost or
   changed.

4. **Is it the broker, or the network?** The gateway's own journal says when it lost its link to the broker.
   Then there is nothing to do on the host but wait. The recorder records the connection as it changes.

5. **When it is back:**

   ```
   sudo hft-status
   ```

   **You should see** `connection RECONCILING` within a few minutes, and the page `hft:connection_down`
   resolves at the next check. If the recorder was refused in the meantime (exit code 2), it is not started
   again by itself: read `journalctl -u hft-recorder -n 20` before you start it.

6. **Afterwards,** the gap is in the day's journal with the reason `connection_lost`. The day still checks
   out ([the recorder runbook](recorder.md), "Checking a recorded day").
