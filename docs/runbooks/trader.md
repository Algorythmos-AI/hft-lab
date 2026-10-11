# Runbook: the trader

For the owner. The trader is `hft-trader.service`: the sleeves, the risk gate, the order manager and the
reconciler in one process ([ADR 0019](../adr/0019-trader-loop-and-staged-rollout.md)). It trades on the
broker's **paper** account only, and with the configuration as shipped it trades nothing there at all.

**Nothing on this page has been followed on the host.** The trader has run against the fake broker only.

Where things are, on the host:

| What | Where |
|---|---|
| Its journals, tapes and status | `/home/hft/hft-lab/var/trader/` |
| A day's decisions and tapes | `/home/hft/hft-lab/var/trader/days/<New York date>/` |
| The switches | `/home/hft/hft-lab/var/KILL`, `/home/hft/hft-lab/var/FLATTEN` |
| Its settings | `config/trader.yaml`, `config/risk.yaml`, `config/cost_model.yaml` |

Every command below is run from `/home/hft/hft-lab` as the user `hft`, with `PYTHONPATH=src` and the
installed interpreter: `sudo -u hft env PYTHONPATH=src MODE=paper .venv/bin/python -m hftlab.trader ...`,
written `trader ...` from here on.

## 1. Start it, in shadow

The unit has no install section. Nothing can enable it, the install script included; it runs while you have
started it.

1. Check that the gateway is up and logged in to the paper account ([weekly login](weekly-login.md)).
2. `sudo systemctl start hft-trader.service`
3. `sudo journalctl -u hft-trader.service -n 20`

**You should see** these lines, in this order:

```
trader: SHADOW-ONLY: the risk limits are not in force. No order can reach a broker from this run.
trader: session 1 of 2026-10-14; stages {"control": "shadow", "idle": "shadow"}
```

`sudo hft-status` then shows a `trader` line: `running; shadow-only`, and each sleeve with its stage.

**If it says `refused`,** it stopped on purpose and will not be restarted. The line says why:

| It says | It means |
|---|---|
| `refused by the paper-only lock` | The gateway is not a paper gateway, or the mode is not paper. Do not work round it |
| `... may place paper orders by the stage journal, and the risk limits are not in force` | A sleeve's stage is above `shadow` and `config/risk.yaml` is not in force. One of the two is wrong |
| `another trader holds ...` | One is already running |
| `a journal or a tape cannot be used` | A journal does not verify. It is never repaired: keep the file, and read section 6 |

**If it says `PAPER: limits in force by HFT-DEC-NNNN`** and you did not expect that, stop it (section 4).
Paper orders are possible only after a decision record set the limits and another approved the sleeve's
step.

## 2. Read the stages

`trader --stages`

```
control: shadow; 0 clean trading day(s) in a row in this stage
idle: shadow; 0 clean trading day(s) in a row in this stage
```

| Stage | What the sleeve's orders do |
|---|---|
| `off` | The sleeve is not run |
| `shadow` | Shadow venue only |
| `min_size` | Paper orders; everything that adds to a position at the pair's minimum size |
| `normal` | Paper orders at the size the sleeve asks for |
| `stepped_down` | Paper orders that reduce a position only |

A stage goes up only at a day's close, by the rule in `config/trader.yaml`: the number of clean trading days
in a row, the decision record that approved the step, and the risk limits in force. It never goes up by a
command. The stage journal, `var/trader/stages.jsonl`, has a `day` line for every trading day and sleeve,
with `clean` and, when it was not, `causes`:

| Cause | What happened that day |
|---|---|
| `LATCH`, `BREAK` | A latch tripped, or a reconciliation break was flagged |
| `UNCLEAN_STOP` | The trader was killed, not stopped |
| `NOT_RUNNING` | Nobody saw the day close |
| `TOO_SHORT` | It ran for less than `clean_day_minutes`, or that is not set yet |
| `STAGE_CHANGED` | The sleeve's stage changed during the day |

A line of type `held` says a rule was met and the step was refused, and why: `LIMITS_NOT_IN_FORCE`,
`STILL_LATCHED` or `STILL_BROKEN`.

## 3. Take a sleeve down

`trader --demote control shadow --operator <your name>` (or `off`).

The running trader acts on it within a second and writes a `request` line into the day's decisions. It is
done only when the sleeve is flat and has no order on its way. If the line says `"refused": "NOT_FLAT"`, set
`FLATTEN`, wait for it to finish (section 4), clear it, and ask again.

There is no command that takes a sleeve up.

## 4. Kill and flatten

| To | Run | What happens |
|---|---|---|
| Stop new entries | `sudo -u hft touch /home/hft/hft-lab/var/KILL` | Within a second: no order that adds to a position, on paper or in shadow. Exits go on |
| Close everything | `sudo -u hft touch /home/hft/hft-lab/var/FLATTEN` | No entries, and one market order for each open line, through the risk gate. A close that is refused is tried again after `flatten_retry_seconds` |
| Stop the process | `sudo systemctl stop hft-trader.service` | It takes no new turn, cancels nothing, closes its journals and exits. Positions and working orders stay where they are |

**FLATTEN has finished when** `sudo hft-status` shows `0 open position(s)` on the trader line. Then, and not
before, remove the file.

**If positions stay open under FLATTEN,** the closes are being refused. The day's decisions say with which
code: `grep '"verdict"' var/trader/days/<date>/decisions.jsonl | tail`. `STALE_QUOTE` and `MARKET_SHUT` mean
there is no market to close into; `PRICE_COLLAR` means the book is beyond even the exit collar. They are
tried again by themselves. `NOT_RECONCILED` means the connection is not safe yet: see section 5.

**Stopping the process does not make you flat.** Set `FLATTEN` first if flat is what you want. What a stop
leaves is taken up by the next start: working orders are cancelled and the books are reconciled before
anything is sent, and a position left from before is closed by the trader itself.

## 5. A reconciliation break

A break means the broker's positions are not the ones the trader's book of the broker's fills adds up to.

**What it looks like.** A page titled `Reconciliation break`. `sudo hft-status` shows
`1 reconciliation break(s) today` and the sleeves that were placing paper orders at `stepped_down`. The
day's decisions hold a line `"type":"break","broken":1`, and `var/trader/reconcile.jsonl` holds
`break_opened` with the pair and the two quantities, ours first.

**What the trader has already done.** Stopped every entry, stepped the paper sleeves down, and left exits
alone. If the line says `"blind":1`, the difference was found while making a connection safe, and nothing
at all is sent until it is gone.

**What to do.**

1. Do not restart the trader to make it go away. A restart finds the same difference.
2. Read the difference: `grep break_opened var/trader/reconcile.jsonl | tail -1`, or, for a blind one,
   `grep positions_differ var/trader/outbox.jsonl | tail -1`.
3. Look at the paper account in the broker's own screen. An order placed by hand, a position carried from
   before the trader ever ran, and a corporate adjustment by the broker are the usual explanations.
4. If the broker holds a position the trader does not know, close it by hand in the broker's screen. That is
   yours to do; the trader will not trade a position it cannot explain.
5. When the two agree again the trader writes `break_cleared` and pages `Reconciliation break over`. The
   sleeves stay `stepped_down`: the way back to `min_size` is the rule, counted from the step down.
6. Write down what it was. A break with no explanation is not closed by going away.

## 6. A latch

A latch is the risk gate's stop: a loss limit, a streak of rejects, or something the order manager saw that
cannot happen (`ORDER_FAULT`). It stops entries until **you** reset it. It survives a restart, the end of
the day and the price coming back.

**What it looks like.** A page titled `Latch tripped: <name>`. `sudo hft-status` shows
`latched: <name>`. `var/trader/risk.jsonl` has the `trip` line with the numbers.

**Before you reset it,** find out why it tripped. For `ORDER_FAULT`, the reason is the last `fault` line of
`var/trader/outbox.jsonl`. For a loss latch, it is the loss.

**To reset it:** `trader --reset <name> --operator <your name>`, with the name exactly as the status shows
it, for example `DAILY_LOSS_SLEEVE/control/USD`.

The running trader does the reset, because only it knows the positions and the quotes the latch starts
again from. Within a second the day's decisions hold a `request` line with `"done":1`, `risk.jsonl` holds
a `reset` line with your name, and you get a page that it is over. `"done":0` means no such latch was
tripped, or a position it measures has no quote yet.

Only the owner resets a latch. An automated contributor never does.

## 7. Replay a day

`trader --replay var/trader/days/2026-10-14`

It runs each session of that day again from its tape, in a scratch folder, and compares the decisions with
the ones the day left. Nothing is sent and nobody is paged.

**You should see** every session with `"identical": true`, and exit code 0. A session that was killed says
`"ended": "killed"` and is still identical as far as it got.

**If one is not identical,** that is a defect to report with the day's folder: something the trader decided
depended on something it did not record. `refused: ... recorded under other settings` means the
configuration changed since that day; check out the commit the day ran on and replay there.
