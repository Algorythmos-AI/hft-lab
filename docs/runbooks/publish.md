# Runbook: publishing the status snapshot

For the owner. Every five minutes, `hft-publish` checks the recorder's status, pages you when a problem
starts or ends, and sends a small snapshot to the status site: stage, phases, health, and what the recorder
is doing. What may be in it is fixed by a contract ([contracts](../../contracts/README.md)): states, counts,
times and codes. No account, no host name, no quote, no log line, no free text.

**Nothing has been published yet, and two decisions are open. Both are yours:**

| Decision | State |
|---|---|
| The signing key for this desk | Not made. No key exists |
| Which key id the site accepts for this desk | Not made. The site has to be given the id and the key |

The site also has to accept this desk's schema, `hft-lab/snapshot`, before the first send. The site's own
repository is where that is done and checked. Until all three are true, the site's page for this desk goes on
saying that nothing has been published.

The shorthand, once per session:

```
hftrun() { sudo -u hft sh -c 'cd /home/hft/hft-lab && PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 exec .venv/bin/python "$@"' sh "$@"; }
```

## Never

| Never | Why |
|---|---|
| Put the key, the address or the alert address into this repository, a unit file or a chat | The repository is public. They live in `/etc/hft/secrets.env` on the host and in the site's own settings |
| Reuse another desk's key or key id | Each sender has its own, so that one desk can never be taken for another. The publisher refuses the id of a sender that names none |
| Reuse another desk's alert address | A page has to say whose it is |

## Look at the snapshot without sending it

```
hftrun -m hftlab.publish
```

**You should see** `publish: DRY RUN, nothing is sent`, then the snapshot. Check `"mode": "paper"`, the
`stage`, and that `health` says what `sudo hft-status` says. This needs no key and can be run at any time.

## Setting it up

1. **Make the key,** on your own machine, and keep it out of every file under version control.

   ```
   openssl rand -hex 32
   ```

2. **Give the site the key and its id,** by the site's own runbook, and confirm there that the site accepts
   the schema `hft-lab/snapshot`. Choose an id that names this desk.

3. **Write the secrets file on the host.** Only the publish job reads it. The recorder never does.

   ```
   sudo install -m 0640 -o root -g hft /dev/null /etc/hft/secrets.env
   sudoedit /etc/hft/secrets.env
   ```

   | Line | Value |
   |---|---|
   | `HFT_PUBLISH_URL=` | The site's ingest address. It must start with `https://` |
   | `HFT_PUBLISH_KEY_ID=` | The id from step 2 |
   | `HFT_PUBLISH_KEY=` | The key from step 1 |
   | `HFT_PUBLISH_BYPASS=` | Only if the site sits behind its hosting provider's deployment protection: that token |
   | `HFT_ALERT_URL=` | The push-notification address for this desk's pages. Its own, not another desk's |

   Run the first command only once: it would empty a file that is already there.

4. **Send one by hand.**

   ```
   sudo systemctl start hft-publish.service
   journalctl -u hft-publish -n 10
   ```

   **You should see** `publish: sent` with a run id and `(HTTP 200)`.

   | It says | Cause |
   |---|---|
   | `not sending: ... is not set` | The line named is missing from the secrets file, or empty |
   | `HFT_PUBLISH_KEY_ID must be this platform's own key id` | The id is empty, malformed, or the one the site keeps for senders that name none |
   | `NOT sent: HTTP 401` | The site does not know this key id, the key differs, or this host's clock is more than five minutes off: `timedatectl` |
   | `NOT sent: HTTP 422` | The site refused the body. It does not know this schema yet, or its copy of the contract is older than ours. Nothing was stored, and nothing of another desk's was touched |
   | `NOT sent: HTTP 403` | The site's deployment protection (set `HFT_PUBLISH_BYPASS`), or its ingest is switched off on that deployment |
   | `the site already holds a newer snapshot` | Not a fault. Two sends crossed |
   | `NOT sent: TimeoutError` or another error name | The network, or the site is down. The next run tries again |
   | `the snapshot does not conform to its contract` | A defect of ours. Nothing was sent. Copy the line to the maintainers |

5. **Enable it.**

   ```
   sudo systemctl enable --now hft-publish.timer
   ```

   **You should see** the desk's page on the status site change within five minutes.

## The pages

The same job pages you. A page goes out once when a problem starts and once when it is over.

| Page | Meaning | Where to go |
|---|---|---|
| `hft:recorder_stale` | The dead-man rule: the recorder has been silent for over a minute with the market open | [Recorder](recorder.md), "After a kill" |
| `hft:recorder_stopped` | It is stopped while the market is open | [Recorder](recorder.md), "Start and stop" |
| `hft:recorder_quota` | It stopped at its quota | [Recorder](recorder.md), "The quota" |
| `hft:recorder_io_error` | The disk refused a write | [Recorder](recorder.md), "The disk is full" |
| `hft:connection_down` | No connection to the gateway while the market is open | [Gateway down](gateway-down.md) |
| `hft:recorder_not_on_paper_venue` | The store was last written by a dry run | [Install](install.md), step 4 |
| `hft:recorder_status_unreadable` | The status file is missing or damaged beside recordings | `sudo systemctl restart hft-recorder.service` |
| `hft:shadow_journal_broken` | The shadow journal does not verify | [Shadow run](shadow-run.md) |
| `hft:backup_failed` | The nightly backup did not complete | [Backup](backup.md) |

To check the alert address, send a test page. The address is read from the secrets file for this one command:

```
sudo sh -c 'set -a; . /etc/hft/secrets.env; set +a; cd /home/hft/hft-lab && exec runuser -u hft -- env PYTHONPATH=src .venv/bin/python -m hftlab.ops.alerts test'
```

**You should see** `alerts: [hft:test] sent`, and the page on your phone.
**If it says `no address is configured`,** the `HFT_ALERT_URL` line is missing. With no address the platform
still runs: every page is written to the journal of `hft-publish` instead, and nowhere else.

## Changing the key

Make a new key, give it to the site under the same id or a new one, change the two lines in
`/etc/hft/secrets.env`, and start `hft-publish.service` once by hand. Remove the old key from the site when
the new one has sent. No restart of anything else is needed.

## If the site stops showing the desk

The site raises its own alarm when a snapshot is missing inside a trading week. That alarm and the pages
above are two halves: the pages come from the host and need the host to be up, the site's alarm needs only
the site. On the host, in this order:

```
systemctl list-timers 'hft*'
journalctl -u hft-publish -n 20
sudo hft-status
```
