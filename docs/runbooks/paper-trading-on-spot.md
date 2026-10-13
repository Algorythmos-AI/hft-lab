# Runbook: paper trading on spot, from a laptop, on a float

How the owner runs the platform on the broker's **paper** account without the server: the recorder and the
trader on one machine, against a paper gateway on the same machine. Design:
[ADR 0031](../adr/0031-spot-inventory-on-a-cash-account.md). The values:
[HFT-DEC-0001](../../research/decisions/HFT-DEC-0001-paper-risk-limits-and-float.md) (limits and float),
[HFT-DEC-0002](../../research/decisions/HFT-DEC-0002-shadow-cost-model-from-the-probe.md) (cost model),
[HFT-DEC-0003](../../research/decisions/HFT-DEC-0003-control-sleeve-to-minimum-size.md) (the control sleeve's
first step).

Everything here is the owner's to run. Nothing in it has been run against a real gateway: it is tested on the
fake broker only, and section 8 lists what the first real run will show.

Note of 2026-10-13: sections 3 and 4 have now been run once against the real paper gateway, from a laptop
([HFT-EXP-0009](../../research/experiments/HFT-EXP-0009-first-float-funding-paper/report.md)). The float was
funded with seven fills, and the same session later ended in a reconciliation break. Section 8 says what
that run showed against each assumption. Sections 5 to 7 are as they were.

## 1. What will happen, in order

1. The trader starts with every sleeve in `shadow`. It sends nothing.
2. The owner funds the **float**: an amount of US dollars, euros and Australian dollars, bought once. The
   paper account lends nothing on spot, so a sleeve can only sell a currency the account holds.
3. After **one clean trading day** (the trader ran eight hours of it, with no unclean stop, no latch and no
   reconciliation break) the control sleeve goes to `min_size` at that day's close, 17:00 New York.
4. From the next session the control sleeve places paper orders of 20,000 EUR.USD and 25,000 AUD.USD, a few
   an hour. Every other sleeve stays in shadow.

EUR.USD and AUD.USD trade on paper. GBP.USD and USD.JPY are quoted, recorded and traded on the shadow venue
only: nothing of them is ever sent to the broker.

## 2. Before the first start

- A checkout with an environment: `make bootstrap`.
- IB Gateway running on this machine, logged in to the **paper** account by the owner, API port **4002**,
  Read-Only API **off**.
- No other session of the login the paper account takes its market data from. See section 7.
- No `MODE` in the shell, or `MODE=paper`. `LIVE_TRADING_ENABLED` unset.

## 3. Start

```
make paper-local
```

That is `deploy/bin/hft-paper-local`. It starts the recorder and the trader on `127.0.0.1:4002` with
`MODE=paper`, keeps all state under `var/` in the checkout, and writes `var/log/trader.log` and
`var/log/recorder.log`. It takes no host and no port. `deploy/bin/hft-paper-local --dry-run` prints what it
would run. `--no-recorder` starts the trader alone.

The trader's log should say, in its first lines:

```
PAPER: limits in force by HFT-DEC-0001. Orders go to the paper account of ibkr_paper.
ON THE FLOAT: the account opens these pairs only with cash it holds, so no sleeve goes above shadow ...
```

A line that begins `refused:` means it did not start; the line says why.

## 4. Fund the float

In a second terminal, in the checkout. Wait a minute after the start, so that the trader has reconciled once
and heard what the account holds.

```
PYTHONPATH=src .venv/bin/python -m hftlab.trader --fund-plan
```

It prints what the account holds, the float and the reserve of each currency, and the conversions that would
fund it. **It sends nothing.** It reads the running trader's status file; it makes no connection of its own.
Read it. If it says the account cannot cover the float, stop here.

```
PYTHONPATH=src .venv/bin/python -m hftlab.trader --fund --operator NAME
```

This leaves a request for the running trader, which places the conversions itself, one order at a time,
through the risk gate. A funding of an account that holds one currency takes a few minutes: the desk's limit
is twelve orders a minute, and the largest order is 40,000 or 50,000 units.

- Asking twice converts once: the second request is answered `ALREADY_FUNDED`.
- A trader that is stopped or killed in the middle goes on with what is left when it starts again.
- A conversion that cannot go on pages once (`float-stuck`) and waits. Look at the screen.

## 5. Watch

```
make paper-status
```

One screen. Under `trader` it shows each sleeve's stage and shadow result, and for the float:

```
float: funded; the account holds AUD ..., EUR ..., USD ...
float baseline ...; drift ..., in no result
paper positions: control/EUR.USD +20,000; paper fills this session: control 4, float 9
on the shadow venue only: GBP.USD, USD.JPY
```

- **drift** is what the float itself gained or lost as rates moved. It is in no sleeve's result and trips no
  latch. It is real currency risk of the account, on paper.
- These are balances of the account. They are on this screen and in `var/trader/status.json`, and in nothing
  that is published.
- `PYTHONPATH=src .venv/bin/python -m hftlab.trader --stages` shows each sleeve's stage and its clean days.
- A promotion that is held back is in `var/trader/stages.jsonl` as `held`, with its cause.

## 6. Stop, kill, flatten, unfund

| What | How | What it does |
|---|---|---|
| Stop | Ctrl-C in the terminal of `make paper-local` | Both processes end their session and close their files. **Positions are not closed.** The float stays. Start again with `make paper-local` |
| No new entries | `touch var/KILL` | Exits are still managed. `rm var/KILL` ends it |
| Close every sleeve position | `touch var/FLATTEN` | Every sleeve is closed back to the float. The float itself is not touched. `rm var/FLATTEN` ends it |
| Take a sleeve down | `python -m hftlab.trader --demote control shadow --operator NAME` | The running trader does it |
| Take the float back | `python -m hftlab.trader --unfund --operator NAME` | Refused while any sleeve holds a position or has an order on its way: flatten first. Converts every currency back to the one the account began in. What cannot be bought back above the dollar reserve stays in dollars, and is written down |
| A latch tripped | `python -m hftlab.trader --reset LATCH --operator NAME` | After looking at why |

To end for good: `touch var/FLATTEN`, wait until the screen shows no paper position, `--unfund`, wait until
the screen says `NOT FUNDED`, `rm var/FLATTEN`, then Ctrl-C.

A reconciliation break on **cash** (the broker posted interest or a fee that no fill explains) stops entries.
Exits go on. Look at the account in the broker's own client. If the cause is innocent:
`python -m hftlab.trader --reanchor --operator NAME` takes the broker's cash as where the account stands.

Since the note of 2026-10-13 in [ADR 0031](../adr/0031-spot-inventory-on-a-cash-account.md), cash is the
only thing spot is reconciled on: the positions the broker lists are not compared. What the running trader
answers to `--reanchor`, in the day's decisions (`grep '"request"' var/trader/days/<date>/decisions.jsonl | tail -1`):

| `refused` | What it means | What to do |
|---|---|---|
| (empty), `done: 1` | The anchor was taken. `var/trader/inventory.jsonl` has an `anchor` line with your name, `absorbed` (what it explained away, by currency) and `restated` (the sleeve positions it took from the book). The break clears on the same turn | Read `absorbed`. It should be what you saw in the broker's client |
| `NOT_SETTLED` | An order is on its way, or a fill is fresher than `settle_seconds` | Ask again in a few seconds |
| `CONNECTION_NOT_SAFE` | There is no connection, or its working orders and executions are not settled yet | Wait for the connection. If it stays, it is not a break in cash: [trader.md](trader.md), section 5 |
| `NO_CASH` | The gateway did not say what the account holds | Ask again; look at the gateway |

It is taken with a sleeve holding a position, with a break standing and while a funding or an unfunding
cannot go on. A sleeve that the break stepped down stays `stepped_down`. `--unfund` in a break: reanchor
first, then flatten, then unfund.

## 7. Known limits

- **A competing login cuts the quotes.** The paper account takes its market data from the login it belongs
  to. While that login is active anywhere else (the broker's phone app, a browser, another gateway), the paper
  session gets no quotes: the trader's quotes go stale, entries stop, and exits wait for a quote. Log the
  other session out.
- **A sleeping Mac cuts the link.** `make paper-local` holds `caffeinate` so the machine does not sleep from
  idleness. Closing the lid, or the battery running out, still sleeps it. The trader reconnects when it
  wakes, and reconciles before it sends anything. A day with a long sleep in it is not a clean day.
- **The gateway logs out once a day** and needs the owner's login again at the weekly reset
  ([weekly-login.md](weekly-login.md)).
- **USD.JPY is shadow only**: the account may not hold yen. **GBP.USD is shadow only** until a probe run
  captures why its test order was refused (HFT-DEC-0001).
- **Direction is not free.** A sleeve can be refused an entry one way (`CASH_RESERVE`) and allowed the other.
- **The cost model is decided, not measured.** No result of a paper day measures cost until commissions and
  slippage come from fills.
- A laptop is not the host. Nothing here is a measurement of the platform's timing.

## 8. What the first real run shows, and what to do if it goes the other way

None of this has been seen on the real gateway. (Written before the first run. The note under the table says
what it showed.)

| What is assumed | What shows it | If it is not so |
|---|---|---|
| The gateway reports a cash balance per currency | `--fund-plan` prints what the account holds | It says the status file does not say what the account holds: stop, nothing was sent |
| A spot conversion is listed as a position in its pair | No reconciliation break within four minutes of the first conversion | A break opens, nothing more is sent. `--unfund`, stop, and report it: reconciliation on cash alone is not built |
| A changed balance arrives soon after a fill | The same | The same |
| A sell is paid from the base currency the account holds | The first conversion that sells, and the first paper sell | The broker refuses it. Three refusals in a row stop entries |
| The commission is charged in the account's own currency | The first paper fill | A commission in a currency with no loss latch stops entries (`UNLATCHED_CURRENCY`): add the latch by a decision record |

### Note of 2026-10-13: what the first run showed

The float was funded on 2026-10-12 at 22:43 UTC: five sells of AUD.USD and two buys of EUR.USD, all filled, in
28 seconds, not the few minutes section 4 expects. The record is
[HFT-EXP-0009](../../research/experiments/HFT-EXP-0009-first-float-funding-paper/report.md). Row by row:

| What was assumed | What the run showed |
|---|---|
| The gateway reports a cash balance per currency | It does, under another tag than the adapter first read. The first start read the account as empty and `--fund-plan` refused; the adapter was corrected, and the plan then printed |
| A spot conversion is listed as a position in its pair | **Yes while the connection lasted, no after it.** No difference in 211 checks over 3 h 31 min. Then the gateway lost its connection to the broker for ten minutes, and from its return it listed no position at all. The trader opened a break and has sent nothing since |
| A changed balance arrives soon after a fill | Yes: all three balances were right at the first check, 31 s after the last fill, and stayed equal to the trader's own figures, commissions included |
| A sell is paid from the base currency the account holds | Yes for AUD.USD: five sells accepted. A sleeve's sell, and a sell of EUR.USD, have not been sent |
| The commission is charged in the account's own currency | Yes: Australian dollars, 2.874 AUD an order, which is the published minimum of USD 2.00. The limits hold a loss latch in that currency, so no entry was stopped by it |

What that changes in this runbook, until it is settled by a design record:

- **A reconnection can end in a break that nothing here ends.** The table's answer for the second row is
  `--unfund`. In the state that run ended in, the connection is not made safe, and the trader answers
  `--unfund`, `--fund` and `--reanchor` with `NOT_SETTLED`. Whether the gateway lists the positions again after
  it or the trader is started afresh has not been tried. Stop (Ctrl-C) is safe: it sends nothing and keeps
  the journals. Do not edit a journal to get past it.
- **In that state the tape fills.** The trader asks the venue on every beat and the tape keeps each answer:
  about 85 MB an hour in that run, against a quota of 1 GB. [trader.md](trader.md), section 8, says what
  happens at the soft and at the hard limit, and how closed parts are compressed (`trader --housekeep`). A
  trader left in a break overnight gets to the limits.
- **A competing login did it.** The ten minutes without the broker ended with the gateway's message that
  there is no market data during a competing session: the first limit of section 7. It costs more than
  quotes.
- A day with a break in it is not a clean day, so the control sleeve's step up waits for a later one.

Still not seen on the real gateway: a sleeve's order, an exit, `--unfund`, `--reanchor`, a refusal by the
broker, the daily logout and the weekly reset.

### Note of 2026-10-13, later: what was changed, and how that run's break ends

The first two points above are settled by the last note in
[ADR 0031](../adr/0031-spot-inventory-on-a-cash-account.md).

- **A reconnection no longer ends in that break.** Spot is reconciled on cash. A position list that is empty
  after a reconnection or a restart is not a difference, and neither is one that is full.
- **The trader that was stopped in that break** starts as any other (`make paper-local`). It makes the
  connection safe, compares the cash, and if the cash is the book's it writes `break_cleared` in
  `var/trader/reconcile.jsonl` and pages `Reconciliation break over`, by itself. If the cash is not the
  book's, a break in cash stands after four minutes: section 6 says what to do. Do not edit a journal.
- **The tape no longer fills.** A connection that cannot be made safe is asked after 250 ms, then after
  twice as long each time, up to once in 30 s; an answer that repeats the last one is counted on the tape,
  not written. An hour in that state is an hour of beats.
- **Error 322** (`Maximum number of account summary requests exceeded`) in the trader's and the recorder's
  log came from the broker library, which asked for the account summary again at each reconnection. It no
  longer does. Seen again, it is news: report it.
- A day with a break in it is still not a clean day. A link the gateway lost and found again, with nothing
  differing afterwards, does not by itself make a day unclean, and never did.

What the next run on the real gateway should show, and what to do if it does not:

| What is expected | What shows it | If it is not so |
|---|---|---|
| After a reconnection: no break, and the cash compared within a second or two | The status screen: no break, and the float's cash with a fresh time | A break in cash after four minutes: look at the account, then section 6 |
| A sleeve's exit is accepted with no position listed | The first paper exit after a reconnection | The broker refuses it; three refusals in a row stop entries. Stop, and report it |
| No error 322 after three reconnections | `var/log/trader.log`, `var/log/recorder.log` | Report it with the lines before it |
