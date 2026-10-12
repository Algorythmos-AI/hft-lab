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
Look at the account in the broker's own client. If the cause is innocent:
`python -m hftlab.trader --reanchor --operator NAME` takes the broker's cash as where the account stands.

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

None of this has been seen on the real gateway.

| What is assumed | What shows it | If it is not so |
|---|---|---|
| The gateway reports a cash balance per currency | `--fund-plan` prints what the account holds | It says the status file does not say what the account holds: stop, nothing was sent |
| A spot conversion is listed as a position in its pair | No reconciliation break within four minutes of the first conversion | A break opens, nothing more is sent. `--unfund`, stop, and report it: reconciliation on cash alone is not built |
| A changed balance arrives soon after a fill | The same | The same |
| A sell is paid from the base currency the account holds | The first conversion that sells, and the first paper sell | The broker refuses it. Three refusals in a row stop entries |
| The commission is charged in the account's own currency | The first paper fill | A commission in a currency with no loss latch stops entries (`UNLATCHED_CURRENCY`): add the latch by a decision record |
