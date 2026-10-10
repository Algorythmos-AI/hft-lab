# Never build

Some things are out of scope for good. They are not backlog items waiting for a quiet week, and a pull
request that adds one is closed. Changing this list takes the owner's explicit decision, recorded.

## A path to real money

| Never | Why |
|---|---|
| A live-trading mode, flag, port, endpoint, venue or login | The platform is paper only by construction ([ADR 0001](adr/0001-paper-only-lock.md)). The venue registry holds `ibkr_paper` and `sim`, and a test asserts exactly that |
| A way round the paper-only lock: a bypass argument, a test-only switch, a second code path that connects without it | A lock with an override is a default |
| Reading `MODE` or `LIVE_TRADING_ENABLED` anywhere but the lock | One place decides |
| A live broker credential on the host or anywhere in the repository | With a paper-only login there is nothing for a bug to find |

## Orders that deceive

These are market manipulation. They are named here so that nobody builds one by accident under another name,
for instance as a "liquidity probe". Paper trading does not make them acceptable: the habit and the code
outlive the account they were written for.

| Never | What it is |
|---|---|
| Layering | Stacking orders at several prices to give a false picture of depth |
| Quote stuffing | Flooding a venue with orders and cancellations to slow or confuse others |
| Spoofing | Showing an order to move the price, meaning to cancel it before it trades |
| Any order placed with intent to cancel | The general rule the three above are cases of. Every order the platform sends is one it is willing to have filled |

The order-to-trade ratio limit and the orders-per-minute limit in the risk gate exist partly to make these
hard to do by mistake.

## Speed races

| Never | Why |
|---|---|
| Co-location | Out of scope. The platform holds positions for seconds to minutes on an ordinary connection; "high frequency" here does not mean microsecond races |

## Things an automated contributor never does

| Never | Instead |
|---|---|
| Log in to the broker, or type or store a credential | The owner installs the paper login on the host |
| Place, change or cancel an order by hand | Orders come only from the order manager, through the risk gate |
| Enable, start or stop the platform's units on the host | Hand the owner the command |
| Commit market data, recordings, model files, knowledge-base text or credentials | They stay out of git; the leak check refuses them ([ADR 0006](adr/0006-public-repo-and-leak-mechanism.md)) |
| Import code or read state from another trading platform | One platform, one repository; copy and trim a generic piece, with its origin noted |
| Look at an affected result before the hypothesis and experiment are written | Pre-register first ([research registry](../research/README.md)) |
