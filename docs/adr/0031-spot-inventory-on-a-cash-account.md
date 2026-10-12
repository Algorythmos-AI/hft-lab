# ADR 0031: Spot on a cash account: a float in every traded currency, and positions as deviations from it

- **Status:** accepted, 2026-10-12. Built and tested against the fake broker and synthetic data only. Nothing
  here has connected to a gateway and no order has been placed by it. Three things it relies on about the
  real gateway are marked UNVERIFIED below, each with what will show it.
- **Context:** F4. [ADR 0019](0019-trader-loop-and-staged-rollout.md) built the trader, and
  [ADR 0015](0015-risk-gate.md) and [ADR 0016](0016-order-manager-and-outbox.md) the gate and the order
  manager it sends through. The capability probe's run with test orders on 2026-10-12 showed that none of it
  could place a paper order as the account stands.

## Context

What the probe's four one-unit orders showed about spot on the paper account:

- **It lends nothing.** A buy of EUR.USD, AUD.USD or GBP.USD was refused for want of US dollars: the account
  holds none, and may not borrow them. Spot here is a conversion of cash the account already holds. A buy of
  a pair is paid from the quote currency, and by the same rule a sell is paid from the base currency. (The
  probe placed buys only. That a sell needs the base currency is the same rule read the other way, and is
  UNVERIFIED until a sell has been sent.)
- **It may not hold yen.** USD.JPY was refused whatever the cash: the account is not allowed a position in
  that currency at all.
- **Under 20,000 units (EUR or GBP base) or 25,000 (USD or AUD base) an order is an odd lot.** The venue
  takes it, says so in a warning, and prices it worse.

Every sleeve goes long and short from flat. On this account "short EUR.USD from flat" does not exist: there
are no euros to sell. So no sleeve could place one paper order, and the staged rollout had nothing to roll
out.

There are three ways out. Two need something of the real account that the owner would have to ask the broker
for: permission to trade forex on margin, or CFDs. The third needs nothing: hold cash in each traded currency
and trade around it. The owner asked for the platform to paper trade now, without a new permission.

## Decision

### 1. The account holds a float, and a sleeve's position is a deviation from it

The account is given an amount of every traded currency once: the **float**. From then on

```
sell 20,000 EUR.USD   20,000 of the float's euros become dollars     the sleeve is short EUR.USD
buy  20,000 EUR.USD   dollars of the float become 20,000 euros       the sleeve is long EUR.USD
the exit              the same amounts go back                       the sleeve is flat, the float is whole
```

A sleeve does not know. Its orders, its position view and its results are what they were. What changed is
underneath: an order is now only possible while the account holds what pays for it.

### 2. The float is a line in the broker book, not a second ledger

The conversions that fund the float are orders like any other: proposed to the order manager, ruled on by the
risk gate, written to the outbox before they are sent, filled by the venue. Their fills are booked in the
broker book under the name `float` (`hftlab.book.FLOAT`), which no sleeve may be called.

That one choice keeps everything else the same:

- the venue's positions still equal the book's desk positions, so reconciliation of positions is unchanged;
- a restart rebuilds the float from the outbox like every other line;
- "what is the float's and what is a sleeve's" is a matter of which lines are summed. Nothing is stored twice.

Cash is then a function of a book (`hftlab.book.cash`). A fill moves its quantity in the base currency, its
quote cash in the quote currency and its commission in the commission's currency, so

```
account cash      = anchor + effect(every line)
baseline          = anchor + effect(the float's line)
sleeve deviation  = effect(that sleeve's lines)
```

in exact millionths of each currency, with no price and no order of events in it. The **anchor** is where
the account's cash began as far as the book is concerned: the venue's cash at the moment of funding, less
everything the book's fills had moved by then. It is written once, in the float's own journal
(`inventory.jsonl`), by the funding an operator asked for.

No currency is converted into another here either. The baseline is an amount per currency.

### 3. Results stay sleeve-relative; the float's drift is said beside them

- The shadow book never sees a conversion. It is the result of record, and it is unchanged.
- In the broker book the desk is its sleeves. The float's line gains and loses as rates move, because it is a
  holding of several currencies: that is the float's **drift**. It is reported on its own (the status
  screen, per quote currency) and is in no sleeve's result and not in the desk's.
- The risk gate judges losses the same way: the float's drift trips no latch. A latch exists to stop a sleeve
  that loses, and the float is not a sleeve's doing.

The drift is given per quote currency, like every result here, and not as one figure in the account's own
currency: that would need a conversion, and nothing in the platform makes one
([ADR 0015](0015-risk-gate.md)).

### 4. The venue is asked what cash it holds, and that is reconciled

The venue port has a fourth question, `cash()`: the account's cash per currency, read-only, in millionths.

- The fake broker models a cash account (`FakeConfig.cash_only`): every execution moves cash, the commission
  is taken in its own currency, and an order the account cannot pay for is refused in the two classes the
  real one showed, or, for a pair with a currency the account may not hold, whatever the cash.
- The broker adapter reads the account's values and keeps the cash balances. UNVERIFIED: that the gateway
  reports a balance per currency under the tag the adapter reads, and sends a changed balance promptly after
  a fill. The first reconciliation after the trader starts shows the first; the first paper fill the second.
- The reconciler compares the venue's cash with anchor plus fills, per currency, within a tolerance. A
  currency further off is a difference like a position's: the same grace time, the same break, the same
  consequences (entries stop, sleeves on paper are stepped down, the owner is paged).
- A break of that kind that has an innocent cause (the broker posted interest or a fee no fill explains) is
  ended by an operator, who looks and then asks for a new anchor (`--reanchor --operator NAME`). It is
  journalled with the operator's name, like a latch reset.

UNVERIFIED, and the one that matters most: that the gateway lists a spot conversion as a position in its
pair, as it lists any forex trade. Position reconciliation assumes it. If it does not, the first funding
conversion opens a reconciliation break within the grace time, nothing else is sent, and the float is taken
back with `--unfund`; reconciliation would then have to be done on cash alone, which is a change to
[ADR 0016](0016-order-manager-and-outbox.md)'s fourth step and is not built.

### 5. The risk gate keeps a reserve of every currency

With `inventory` in the limits the gate keeps its own figure of the account's cash: the anchor it is shown
plus what the fills in its own book moved. Before an order may go:

| Check | Refusal | What it means |
|---|---|---|
| The pair can be held | `PAIR_NOT_TRADABLE` | USD.JPY: the account may not hold yen |
| A float stands | `NOT_FUNDED` (entries stop) | there is nothing to deviate from yet |
| An entry is at least the pair's minimum | `ODD_LOT` | under it the venue prices worse |
| The reserve holds | `CASH_RESERVE` | see below |

The reserve rule: the currency the order pays in must stay at or above its configured reserve with this
order filled in full at the worst price its collar allows, **and** every order still on its way filled the
same way. What those orders would bring in is not counted. It is the position limit's pessimism, applied to
cash.

- A sleeve that cannot enter because the float is used up does not enter. That is an ordinary verdict with its
  own code, journalled, and nothing is wrong.
- An order that can only reduce a sleeve's position is not held to the reserve: it takes back what the entry
  paid out. The reserve is what covers an exit that costs more than the entry brought.
- The conversions of the float pass every guard (the switches, the spread, the collar, the order size, the
  rates) and the reserve, and may go only while a funding an operator asked for is open
  (`FUNDING_NOT_OPEN` otherwise).
- The float is not a sleeve: it has no position cap, FLATTEN does not close it (FLATTEN closes every sleeve
  back to the float), and the desk's position and open pairs are counted without it.

### 6. Funding is asked for by an operator and done by the running trader

```
python -m hftlab.trader --fund-plan                  print the conversions; send nothing
python -m hftlab.trader --fund --operator NAME       ask the running trader to make them
python -m hftlab.trader --unfund --operator NAME     ask it to convert the float back
python -m hftlab.trader --reanchor --operator NAME   ask it to take the venue's cash as where the account stands
```

- The **plan** is a pure function of what the account holds, the quotes and the configured float
  (`hftlab.trader.funding.plan`). Every currency of the float but the hub is the base of a pair against the
  hub, so a plan has two kinds of step: sell a currency the account has more of than its float, for the hub;
  buy a currency it has less of, with the hub. Each is priced at the worst price its collar allows and a
  little more of the hub is raised than that comes to. An account that cannot cover the float is refused,
  with the reason.
- `--fund-plan` reads what the running trader last heard the account holds from its status file. It makes no
  connection of its own: a second login would be a second path to the broker.
- `--fund` leaves a request for the running trader, like a latch reset. The trader takes the anchor, opens
  the funding in the float's journal with the position each pair is to reach, and proposes one order a beat
  to its own order manager until the book's float line is there. There is no other way an order of this
  platform reaches the broker.
- It is **idempotent** twice over: the plan is worked out from what the account holds, so a float that is
  there needs no conversion; and the journal knows a float stands, so asking again is answered
  `ALREADY_FUNDED` and nothing is sent.
- A funding that is interrupted goes on. What is left of a pair is its target less what the book holds, so a
  restart, a partial fill or a reject changes nothing: the next beat proposes what is still left. A
  conversion the account cannot pay for waits, and the owner is told once.
- `--unfund` takes every pair's float line back to zero, what brings the hub in first. It is refused while
  any sleeve holds a position or has an order on its way. While it runs the reserve of every currency but
  the hub is no longer kept, so all of a currency goes back. What cannot be bought back above the hub's
  reserve, or is less than the venue takes as an order, is written down and left.

### 7. A pair the account cannot hold is traded on the shadow venue only

USD.JPY is listed under `inventory.shadow_only`. It is quoted, on the tape and in every recording, and every
sleeve trades it on the shadow venue whatever its stage: its orders in that pair are never proposed to the
order manager, its position in that pair is read from the shadow book, and it hears of its fills from the
shadow venue. The gate refuses the pair as well, so the rule does not rest on the trader alone.

### 8. The paper minimum size is the venue's minimum for good pricing

20,000 units where the base currency is EUR or GBP, 25,000 where it is USD or AUD: the sizes the venue's own
odd-lot warning names. They are the cost model's minimum per base currency (so `min_size` means that), and
the gate's minimum entry. A fill under them would be priced as an odd lot, and a paper fill priced like no
normal fill would say nothing about normal fills.

## Consequences

- Capital sits idle. The float the limits propose is worth roughly 270,000 US dollars, to carry positions of
  at most 40,000 or 50,000 units a pair. On a paper account that is free. It is the reason this is not how a
  real account would be run, and the owner's choice between this and a permission stays open.
- The account carries currency risk the sleeves did not take. It is reported as the float's drift and judged
  by nothing.
- Direction is no longer free: a sleeve can be refused an entry one way and allowed the other, depending on
  what the others hold. The code for it is in the journal, and a comparison of a paper day with its shadow
  day has to count those refusals.
- A commission is charged in the account's own currency, and a fee is a loss in that currency. The limits
  need a loss latch in it, or entries stop with `UNLATCHED_CURRENCY` after the first fill. UNVERIFIED which
  currency that is; the first paper fill says.
- The tape holds one more kind of answer (`cash`), and the reserve kept on it is sized for it.

## What was not done

- **No conversion to one currency.** No total value of the float, no exposure limit in the account's
  currency. `desk.max_gross_exposure` and `desk.max_leverage` stay null and unenforced.
- **No reconciliation on cash alone.** See the UNVERIFIED point under 4.
- **No float per sleeve.** The float is the desk's. Two sleeves holding opposite ways use no float between
  them at the broker, and the reserve rule sees that, because it works on the account's cash.
- **No automatic top-up.** A float worn down by losses and fees is not refilled. `--unfund` and `--fund` do
  it, when an operator says so.
