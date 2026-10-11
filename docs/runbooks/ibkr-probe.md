# Runbook: the IBKR capability probe

For the owner. One sitting of about twenty minutes, on your own machine, while the forex market is open
(Sunday 17:15 to Friday 17:00 New York). It answers what the paper account can actually do, so that the
decisions on the access route and the instrument form rest on measurements.

**When to run it.** For spreads and update rates that mean something, run it once in liquid hours: the London
morning or the New York morning. The minutes after the weekly open on Sunday, and the hour after the 17:00
New York rollover on any day, are the worst time to sample: quotes are few and spreads are several times
their usual width. A run then still shows what resolves and what is on offer, and nothing about costs. The
first real run was made at the weekly open and is recorded as such
([`HFT-EXP-0007`](../../research/experiments/HFT-EXP-0007-capability-probe-weekly-open/report.md)).

Only you can do this: it needs the paper login, and nobody else ever types it.

## Never

| Never | Why |
|---|---|
| Log the gateway in with your live username | The probe refuses a live account, but the safest live session is the one that does not exist |
| Put a username, password or account number in this repository, or in any file under version control | The repository is public. The gateway keeps the login; the probe never asks for it |
| Use port 4001 or 7496 | Those are the live ports. The probe refuses them by name |
| Set `LIVE_TRADING_ENABLED` | Anything but unset or an explicit false refuses |
| Paste the report somewhere public without reading it | Account ids are masked, but the report describes your account |
| Run `--orders` before you have read a read-only report | The first run tells you whether the second is safe to make |

## 1. Install

1. **IB Gateway**, the stable version, from the broker's website. Not from anywhere else.
2. **A paper-only username.** In the broker's account management, create or look up the paper trading login.
   It is a separate username from the live one. Use only this one from here on.
3. **This repository's environment:** `make bootstrap`.

## 2. Start IB Gateway in paper mode

1. Start IB Gateway. On the login screen choose **Paper Trading** and **IB API**.
2. Log in with the **paper** username.
3. Open the API settings (Configure, Settings, API, Settings; names vary a little by version) and check:

   | Setting | Value |
   |---|---|
   | Socket port | `4002` |
   | Read-Only API | **on** for the first run; it must be **off** for `--orders` |
   | Allow connections from localhost only | on |

4. Leave the gateway running.
5. **Before every run, look at the gateway's own window.** It shows a row of status lights for its connection
   to the broker's servers and to each data service. All of them should be green. If one is red or the
   window says it is reconnecting, the gateway is cut off from the broker: it will accept the probe and
   answer nothing. Wait until the rows are green, or quit the gateway and log in again. A gateway often
   drops this way when a laptop has slept.

## 3. Environment

| Variable | Value | Needed |
|---|---|---|
| `MODE` | `paper` | Yes. Anything else refuses |
| `LIVE_TRADING_ENABLED` | leave unset | |
| `HFT_IBKR_USERNAME` | your paper username | Optional. It is compared, never sent |
| `HFT_IBKR_LIVE_USERNAME` | your live username | Optional and discouraged. Set it only if you want the lock to refuse that name too; it means writing the live name into your shell |

Set them in the shell for this session. Do not put them in a file inside the repository.

## 4. Dry run, without the gateway

```
PYTHONPATH=src .venv/bin/python -m hftlab.venues.ibkr.probe --fake --window-seconds 5
```

This runs the whole probe against the built-in fake broker and prints a report whose `venue` is `fake`. It
shows what the real report will look like and proves the install works.

## 5. The read-only probe

```
MODE=paper PYTHONPATH=src .venv/bin/python -m hftlab.venues.ibkr.probe \
    --pairs EUR.USD,USD.JPY --window-seconds 60
```

It takes a little over a minute per pair. It prints the report and writes it to
`var/probe/ibkr-<UTC date>T<UTC time>Z.json`, for example `ibkr-2026-10-11T213440Z.json`, which git ignores.
It places no order.

**Every run keeps its own report.** The name carries the second the run started, so a second run on the same
day does not replace the first. If a file of that name is already there, the probe prints the report, leaves
the file alone, says so and exits with status 2. That can only happen to a dry run (section 4), whose clock
always starts at the same instant: delete the old dry-run file, or pass another `--out-dir`.

| Option | Default | |
|---|---|---|
| `--pairs` | `EUR.USD,USD.JPY` | Comma-separated |
| `--window-seconds` | `30` | How long quotes are sampled per pair |
| `--port` | `4002` | `7497` if you use TWS in paper mode instead of the gateway |
| `--client-id` | `78` | Any API client id nothing else is using |
| `--out-dir` | `var/probe` | |

## 6. Reading the report

| Field | What to look for |
|---|---|
| `accounts` | Every entry starts `DU`. If the probe got this far, the lock agreed |
| `server_time.offset_us` | The gateway's clock against yours. It answers in whole seconds, so anything under a second means nothing |
| `pairs.<pair>.forms.cash` / `.cfd` | `resolves` says whether the paper account can see the pair in that form. `price_increment`, `min_order_size` and `size_increment` are the broker's own numbers |
| `pairs.<pair>.sampled_as` | Which form the quotes were sampled in (spot if it resolves, else the CFD) |
| `pairs.<pair>.quotes.updates_per_second` | How often the top of the book changed |
| `pairs.<pair>.quotes.gap_us` | Time between changes, in microseconds: `median` is the usual rhythm, `p99` and `max` show the pauses |
| `pairs.<pair>.quotes.spread` | In price units of the pair (for EUR.USD one unit is 0.00001). `crossed_or_locked` should be 0 |
| `pairs.<pair>.depth` | Whether more than the top of the book is available, and how many levels |
| `pairs.<pair>.historical_ticks` | Whether the gateway serves past quotes |
| `pairs.<pair>.reason` | Present only when the pair could not be asked about at all. See "If the upstream link was down" |
| `notices` | Messages from the gateway, with its own codes, from the moment the connection is made. A well gateway sends a few as it connects, each saying a data service is connected: those are not errors. A market-data or subscription complaint shows up here too |
| `disconnects_during_probe` | Should be 0 |
| `upstream_link_down` | Should be `false`. `true` means the gateway was cut off from the broker and the report measures nothing |
| `not_measured` | What this probe cannot tell you |
| `proposed_config` | What the probe would write to `config/venues/ibkr.yaml` |

## If the upstream link was down

The probe exits with **status 3**, prints `the gateway is connected to us but not to the broker's servers`,
and the report has `"upstream_link_down": true`. Each pair carries that one sentence as its `reason`, with no
quotes, depth or history, and with `--orders` nothing is placed.

What happened: the probe reached the gateway, which is a program on your machine, and the gateway had lost its
own connection to the broker. It accepts a request in that state and never answers it. It says so only in
status messages, which are in `notices`; the ones that count are listed with their meaning in
`src/hftlab/venues/ibkr/link.py` (1100 and 2110 for the broker's servers; 2103, 2105 and 2157 for the quote,
history and contract-description services).

What to do:

1. Look at the gateway's status rows (section 2, step 5).
2. **Wait.** The gateway retries by itself, and a minute or two is often enough. Run the probe again when the
   rows are green.
3. If they stay red, **quit the gateway and log in again** with the paper username, then run the probe again.
4. If the laptop slept, expect this: do step 3 straight away.

Keep the report of the failed run. Nothing in it is a measurement, so nothing from it goes into the
capability file, and `--write-config` leaves that file alone on such a run.

A lookup that times out with no such message from the gateway is still reported as a bare timeout per form,
and the run exits with status 0: the probe does not guess at a cause the gateway did not state.

## 7. The order round trip (optional, second run)

Only after you have read a read-only report and it looks right. Turn **Read-Only API off** in the gateway,
then:

```
MODE=paper PYTHONPATH=src .venv/bin/python -m hftlab.venues.ibkr.probe \
    --pairs EUR.USD,USD.JPY --window-seconds 30 --orders
```

For each pair it places **one** buy limit order of the minimum size, five per cent below the bid so that it
cannot trade, waits for the acknowledgement, cancels it, and times both. Whatever happens in between, it
cancels on the way out and then asks the gateway what is still working.

| Field | What to look for |
|---|---|
| `orders.placed` | `false` with a reason if the account already had working orders: the probe places nothing beside them |
| `orders.pairs.<pair>.ack_us` | Microseconds from sending to the acknowledgement |
| `orders.pairs.<pair>.cancel_us` | Microseconds from the cancel to its confirmation |
| `orders.left_working` | Must be `[]` |
| `orders.positions_changed` | Must be `false` |

Exit statuses: 0 done; 1 ATTENTION (below); 2 refused, could not connect, or the report's file already
exists; 3 the upstream link was down.

**If the command exits with status 1**, it says ATTENTION: an order is still working or a position changed.
Open the gateway's order and position panels now, cancel what is working, and note what you see before doing
anything else.

Turn Read-Only API back on afterwards.

## 8. Afterwards

1. Keep every `var/probe/ibkr-<date>T<time>Z.json`, the runs that failed included: together they are the
   record of what the gateway did. The reports of runs that measured something are what the F1 exit gate
   asks for.
2. Record each run that measured something as a research experiment with its results: a new `HFT-EXP` id
   every time, naming the earlier runs, in our own words, with no account id and nothing about the machine.
   Say when in the trading week it ran.
3. Record the two decisions the runs feed as research decisions (`HFT-DEC-NNNN`): the access route
   ([ADR 0010](../adr/0010-access-route.md)) and the instrument form, spot or CFD. **Not before both of these
   exist:** a read-only run in liquid hours, and a run with `--orders`. A run at the weekly open, or one
   that placed no order, is not evidence for either.
4. When the numbers of a liquid-hours run look right, run the probe once more, also in liquid hours, with
   `--write-config` and open a pull request with the diff. This is a fresh run: it writes what that run
   measures, not what an earlier report said.

   ```
   MODE=paper PYTHONPATH=src .venv/bin/python -m hftlab.venues.ibkr.probe \
       --pairs EUR.USD,USD.JPY,AUD.USD,GBP.USD --window-seconds 60 --write-config
   ```

   Without `--write-config` the probe never touches `config/venues/ibkr.yaml`. With it, the whole file is
   replaced, comments and hand-entered values included. Read the diff before you commit it:

   - `status` is written as `partly_measured`. The probe never writes `probed`: it sees no commission and
     cannot know that it sampled liquid hours. `probed` is set by hand, with the decision records.
   - `resolves` lists the forms the gateway describes. It is not a list of what the account may trade.
   - `update_interval_ms` is that run's window. Keep it only from liquid hours; otherwise set it back to null.
   - `probe_record` is written as null. Put in the id of the experiment that records that run.

## If it refuses or cannot connect

| Message | Cause |
|---|---|
| `refused by the paper-only lock: MODE is ...` | `MODE=paper` is not set in this shell |
| `... port 4001 is a live trading port` | Wrong port, or the gateway is in live mode. Stop and check the gateway's title bar |
| `... is not a paper account id` or `... is a live account id` | The gateway is logged in with a login that is not paper-only. Log out. Do not retry until it is the paper login |
| `the venue could not be probed: ... ConnectionRefusedError` | The gateway is not running, the API is not enabled, or the port is different |
| `... TimeoutError` | The gateway accepted the socket and did not answer. Another program may hold the same client id; try `--client-id 79` |
| Exit status 3, `the gateway is connected to us but not to the broker's servers` | The gateway has lost its own connection to the broker. See "If the upstream link was down" |
| Exit status 2, `... already exists and is left as it is` | A report of that name is there and is never replaced. Only a dry run can meet this; see section 5 |

A refusal by the lock is final for that run by design. Fix the cause and start the command again.

## What this run confirms

The adapter was written without a gateway. Each line below is something it assumes; the report or the
gateway's own panels show whether it holds. Tell the maintainers which, if any, did not.

| Assumption | How the run shows it |
|---|---|
| A paper login reports only account ids that start `DU`, with at most one more letter before the digits | `accounts`; and the probe would have refused otherwise |
| The paper gateway listens on 4002 (TWS paper on 7497) | The probe connects |
| The handshake alone returns the managed accounts, before anything else is requested | The probe gets past the lock |
| Forex spot is `CASH` on `IDEALPRO`; a forex CFD is `CFD` on `SMART` with the base currency as symbol | `forms.cash.resolves` and `forms.cfd.resolves` |
| A contract's `minTick` is its price increment, and `minSize` and `sizeIncrement` are whole base-currency units | `price_increment`, `min_order_size`, `size_increment` look right against the broker's website; a missing or fractional value shows as `resolves: false` with a reason |
| Streaming top-of-book for forex needs no market-data subscription on paper and updates every few milliseconds | `quotes.updates` is not 0; `gap_us.median` |
| Quote prices never have more decimals than the price increment allows | No `translation` notice |
| The gateway's "no price" is -1 or not-a-number, and "no size" is 0 | No `translation` notice; `first` and `last` look like real quotes |
| Market depth and historical ticks are requested the way the adapter does | `depth` and `historical_ticks`, and any `notices` beside them |
| The gateway answers a clock request in UTC | `server_time.server` matches the wall clock |
| An order's reference field carries our id back unchanged | With `--orders`: the round trip completes |
| A new order is acknowledged as `PreSubmitted` or `Submitted`, and a cancel ends as `Cancelled` | `state_after_placing: WORKING`, `state_after_cancelling: CANCELLED` |
| A limit price sent as decimal text and a whole-number quantity are accepted | The order is acknowledged, not rejected |
| A minimum-size order far from the market is accepted and does not trade | `positions_changed: false` |
| Working orders can be listed for the whole account, with their reference | `left_working: []` after the cancel |

## What the first real run showed

One read-only run, at the weekly open (`HFT-EXP-0007`). Against the table above:

| | |
|---|---|
| Held | The paper id's form, the port, the handshake before anything else, spot and CFD both resolving for EUR.USD, USD.JPY, AUD.USD and GBP.USD, streaming quotes with no market-data subscription, five levels of depth, historical ticks, the clock answered in UTC, no `translation` notice |
| Held, with a correction already made | The gateway reports a minimum size below one unit for these pairs; the adapter takes it as one whole unit. It is the smallest order the gateway takes, not the size from which its pricing is good |
| Not shown either way | That quotes update every few milliseconds: at the weekly open they came seconds apart, which says nothing about normal hours |
| Seen and not explained | Spot quotes were finer than the spot contract's price increment for three of the four pairs. An order priced straight from a quote could be off the increment and refused. The order round trip will show which grid orders have to be on |
| Not touched | Every line about orders: Read-Only API was on |

Two earlier attempts failed because the gateway was cut off from the broker, and their reports gave no hint.
The probe now says so (see "If the upstream link was down").

Not confirmed by any run so far, and still assumed by the adapter until paper trading starts (F4): how a fill, a
partial fill and a commission are reported; that an execution's time carries a time zone; that the liquidity
flag is 1 for added and 2 for removed; that the unfilled remainder of an immediate order arrives as
`Cancelled`; which message codes announce a lost and a restored link to the broker (1100, 1101, 1102) and a
cancel that came too late (10147, 10148); what the gateway's daily restart looks like from the socket; and
that a forex position is reported as a whole number of base-currency units.
