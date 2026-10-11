# Runbook: the daily summary and the weekly scorecard

For the owner. `python -m hftlab.ops.report` reads the journals and writes two reports. It changes nothing
else: it holds no venue, sends no order and keeps no state of its own.

**Nothing on this page has been followed on the host.** The reports have been made from synthetic journals
only, and `hft-report.timer` has never been enabled anywhere.

The shorthand, once per session:

```
hftrun() { sudo -u hft sh -c 'cd /home/hft/hft-lab && PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 exec .venv/bin/python "$@"' sh "$@"; }
```

## What each one says

| Report | For | It holds |
|---|---|---|
| Daily summary | the trading day that closed last, from one 17:00 New York rollover to the next | the recorder's share of the market's open time and its unexplained gaps, by pair; each sleeve's stage, fills and result by currency; the desk's result in both books; the slippage report; reconciliation breaks; latches tripped and reset; stage changes; the disk: the recording against its quota, and the tapes against their soft limit, their hard limit and their quota |
| Weekly scorecard | the week that closed last, on a Friday at 17:00 New York | the same summed over the week's trading days; each sleeve less the control sleeve; the lineup's changes since the Friday before, with the training report each rests on; the counts so far towards the gates of F2, F4 and F6 |

How to read them:

- **A sleeve's result is the shadow book's.** That is the result of record. The broker book is beside it: its
  fills are counted for each sleeve, and its result is given for the desk.
- **Money is exact.** Millionths of a currency, written out, by currency. Two currencies are never added.
- **`ABSENT` is not zero.** A part the journals do not hold is shown as `ABSENT`, and a line at the end
  starting `absent` says why. A day whose last session did not say it stopped has its fills counted and no
  result: the report will not quote a figure nobody wrote down.
- **A count towards a gate is evidence, not the gate.** `F2 ... 2 of 3 recorded day(s), towards 5` counts
  trading days in which the recorder ran at all and, of those, the days with no unexplained gap. Whether
  the gate is met is in [the roadmap](../roadmap.md), and is yours to say. "Without manual help" is not
  something a journal can show, and the report does not claim it.
- **Nothing private is in it.** No account id, no host name, no path, no text from a venue or a log, no
  operator's name. A name that is not shaped like one of the platform's own is shown as `[text]`.

## By hand

```
hftrun -m hftlab.ops.report daily
hftrun -m hftlab.ops.report weekly
```

**You should see** the report on the screen and, on the last line, `report: written to
var/reports/daily-<date>.txt` (or `weekly-<year>-W<week>.txt`). Making the same report again replaces its
file. Add `--send` to send it as a page as well, through the address the alerts use
([publish](publish.md)). With no address configured the page goes to the log and nowhere else.

**If it exits with 1,** the report was written and the page was not delivered. **If it exits with 2,** a
settings file could not be read, and the line says which.

## Every trading day, by the timer

`hft-report.timer` starts `hft-report.service` on Monday to Friday at 17:10 New York, ten minutes after the
rollover. The service writes and sends the daily summary and, when the day that closed was a Friday's, the
weekly scorecard after it. The time is New York's, so it moves with New York's clock and not with yours.

The install script does not enable this timer, and refuses to if asked. A page every day is something you
ask for:

```
sudo systemctl enable --now hft-report.timer
systemctl list-timers 'hft*'
```

**You should see** `hft-report.timer` with its next run at the coming 17:10 New York.
**To stop the pages:** `sudo systemctl disable --now hft-report.timer`. The reports can still be made by
hand.

The reports are not in the backup. They are rebuilt: the same journals give the same report, byte for byte.
