# HFT-DEC-0004: the instrument form is spot, on a cash account with a float

- **Date:** 2026-10-14
- **Status:** decided by the owner on 2026-10-12. This record writes down a choice already made and acted on;
  it does not propose one.
- **approved_by: owner**, who made the choice on 2026-10-12 and funded the float that carries it out. Changing
  it needs a new record (see "What would change it").
- **Rests on:** [HFT-EXP-0008](../experiments/HFT-EXP-0008-capability-probe-orders-asian-session/report.md),
  finding b, which put the choice to the owner, and
  [HFT-EXP-0009](../experiments/HFT-EXP-0009-first-float-funding-paper/report.md), the first real fills on
  the form chosen. Built by [ADR 0031](../../docs/adr/0031-spot-inventory-on-a-cash-account.md); put in force
  by HFT-DEC-0001, HFT-DEC-0002 and HFT-DEC-0003.
- **Trials:** none. No configuration was evaluated and no result of a strategy was looked at to make this
  choice, so `config/trial_registry.yaml` gets no row.
- **Sets:** `instrument_form` in `config/venues/ibkr.yaml`, to `cash`. Nothing in the trader reads that key:
  it records the decision and changes no behaviour.

## What was open

HFT-EXP-0008 showed that on this paper account spot currency is an unleveraged cash conversion. A BUY from flat
was refused for want of the quote currency, a short cannot be held, and yen cannot be held at all. So a sleeve
that goes long and short could trade here in one of two forms:

- **spot (IDEALPRO), with cash held in each traded currency**: a float, against which a position is a
  deviation; or
- **forex CFDs**, which need a trading permission on the owner's real account. Asking for it is the owner's
  decision alone.

## What is decided

| | Value | Reasoning |
|---|---|---|
| Instrument form | **`cash`**: spot on IDEALPRO, traded against a float of each traded currency | The owner's choice, 2026-10-12: tradable now, with no new permission |
| CFDs | **not used** | They need a permission on the owner's real account that has not been asked for |
| Pairs on paper | EUR.USD and AUD.USD | As HFT-DEC-0001 sets; not changed here |
| USD.JPY and GBP.USD | **shadow venue only** | As HFT-DEC-0001 sets. Yen cannot be held on this account |

## What has happened since the choice

- ADR 0031 built the float: positions as deviations from it, and the risk gate's cash rules.
- HFT-DEC-0001, HFT-DEC-0002 and HFT-DEC-0003 put the limits, the cost model and the control sleeve's first
  step in force for it.
- The owner funded the float. HFT-EXP-0009 records the first real fills, all accepted, and that cash reconciled
  exactly while the connection lasted.
- Reconciliation of a spot position now rests on cash, not on the gateway's list of positions.

## What this does not decide

- **The access route.** Its record needs an order timing taken on the server, which comes with the server
  install. [ADR 0010](../../docs/adr/0010-access-route.md) stays the engineering default until then.
- No limit, float size, stage rule or cost value is changed by this record.

## What would change it

A move to CFDs needs, first, the owner's own request for the permission on their real account, and then a new
decision record that names this one. Nothing in this repository asks for that permission.
