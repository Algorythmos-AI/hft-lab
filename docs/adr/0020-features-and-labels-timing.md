# ADR 0020: Features and labels: what is known when

- **Status:** accepted, 2026-10-11. Built and tested on synthetic streams only. No recorded day exists, so no
  feature and no label has been computed on real data.
- **Context:** F5 and F6, foundations

## Context

[ADR 0008](0008-learning-loop.md) said that features are stamped strictly before the label starts and that a
planted future-peek feature must be caught. It did not say what "before" is measured in, who checks it, or
what a label is priced with. Those three decide whether a model's result means anything: a feature that saw
one event too many, or a label priced more kindly than the shadow venue prices, both produce a model that
looks good for a reason that will not be there when it trades.

Two more things were open. A feature computed one way from a recording and another way in the trader is two
features, and only one of them was validated. And a float in a feature is a decision that can differ between
two machines, which a replay ([ADR 0005](0005-deterministic-replay.md)) cannot allow.

## Decision

### 1. A feature is computed one event at a time, and says which event

`hftlab.features.FeatureBuilder.step(state, event, now_ns)` returns the new state and, when the book can be
trusted to give one, a row. There is no other implementation. A recording is that step folded over a list; a
sleeve calls it with the event it was just shown. A test takes generated streams both ways and compares the
rows.

- A row carries the pair, `n` (the number of its event in the stream) and `known_ns` (that event's receive
  time). It was made from the events up to and including `n`, because nothing after `n` had been shown.
- Every value is an exact integer: price units, counts, millionths, nanoseconds, seconds. A mid is kept
  doubled, as everywhere else ([ADR 0002](0002-integer-prices-and-nanosecond-timestamps.md)).
- A feature has a name, a version and, where it looks back, a window. Its key (`mid_change_x2:1000000000@v1`)
  is what a dataset and a model file keep. A change of meaning is a new version.
- The first set: the spread, the change of the mid over several look-backs, the quote rate, the imbalance of
  the displayed sizes, the range of the mid, the time since the price last moved, and the time of day, to the
  rollover and to the weekly close, taken from `hftlab.calendar`.

### 2. No row rather than a made-up value

There is no row when the book is crossed, when it shows no size on either side, while the market is shut, and
until the history reaches back as far as the longest window. What was known is dropped, and the history
starts again, after a book went unconfirmed for longer than a configured time, after any connection change or
recorder restart, and while the market is shut. A feature never spans a time nobody was watching.

### 3. A label is priced as the shadow venue prices, and has an end

`hftlab.labels` computes two labels for a decision on event `n` at time `t` and a horizon `H`.

- **The outcome of each side:** a fill-or-kill market order of the minimum size sent at `t`, and the opposite
  one sent at `t + H`, each priced with the `CostModel`'s own functions ([ADR 0013](0013-shadow-cost-model.md)):
  it arrives one latency later, meets the quote in force then, crosses the spread, pays the slippage and the
  commission, is widened or refused near the rollover, and fills only if the venue would have filled it. The
  result is integer millionths of the quote currency. A side the venue would not have filled at either end
  has no outcome.
- **Which barrier came first:** a profit barrier, a loss barrier, both in price increments from the entry
  price and judged at the price the position could be closed at, or the time limit.
- **The latency is the cost model's largest.** The venue draws one per order between its smallest and its
  largest, and a label cannot know the draw. A property test sends the same two orders to a real shadow venue
  with the two equal and compares the label with what the venue reports, to the unit.
- **`end_ns` is when the label's window ends:** the moment the exit arrives. A label is hindsight. It is
  never a price level, never an input to a decision, and never reported as a strategy's result.

### 4. The rule, and who checks it

> A row's features were all known strictly before its label window starts, and a label is usable for training
> only by a decision made after its window has ended.

"Before" is measured in events of the stream, not in clock time: two events can carry the same nanosecond. The
features of a decision on event `n` use events up to `n`; its label reads the stream after `n` and the book
those events left standing.

The dataset builder (`hftlab.learn.dataset`) checks the first half itself, on every dataset, whatever produced
the rows:

1. **The stamp.** A row for event `n` must say it was known at event `n`'s own time.
2. **The cut.** The stream is cut after an event and the features are computed twice more: with nothing after
   the cut, and with everything after the cut changed to other prices, other sizes and later times. Every row
   up to the cut must be exactly as it was. Cuts are taken at up to sixteen places spread over the stream, and
   at every place when the stream is that short.

A dataset that fails either is refused. There is no setting that lets one through. The tests plant a feature
that reads the next event's bid, one that reads when the next event comes, and one that is merely misaligned
by one event; each is refused, and a feature of the past passes.

The second half is carried by every row as `end_ns` and enforced by the splitter
([ADR 0021](0021-validation-splits-trials-and-the-three-numbers.md)). A decision whose label window has not
ended inside the recording has no row.

### 5. What a dataset records

A dataset is made per pair and horizon from day folders of the recorder's store, read as one stream. It keeps
the feature keys in column order, the label spec, the cost model's parameters with its placeholder flag, the
order size, the source days, the number of feature rows that became no dataset row and why, and a SHA-256 of
all of it and of every row. It holds integers and text. It is derived data: never committed
([ADR 0006](0006-public-repo-and-leak-mechanism.md)).

## Consequences

- The same code computes a feature for training and for trading, so there is no second implementation to keep
  equal to the first.
- The cut check is a test of behaviour, not a proof. It cannot catch code that carries its own copy of the
  future, and it looks at sixteen places, not at all of them. A feature that reads ahead at every row is
  caught at any one of them; one that reads ahead only on rare events can be missed. Review is the last layer.
- The feature state is a value, copied on every quote, with a history as long as the quotes of the longest
  window. That is simple and replayable and slow: windows of minutes on a feed of thousands of quotes a
  second need another structure. The definitions do not change when it comes.
- A label assumes the cost model's largest latency and an exit sent at exactly the horizon. A sleeve sends
  its exit on the first event at or after the horizon, so on a quiet book the two differ by the wait.
- Rows that follow one another have label windows that overlap. A row count is not a count of independent
  observations; the validation of ADR 0021 resamples whole days because of it.
- Every label priced on the placeholder cost model is a placeholder. The dataset says so, and so does every
  model fitted to it.

## Addendum, 2026-10-11: an order that would not fill is an outcome, not a missing row

Section 3 said that a side the venue would not have filled at either end has no outcome, and the dataset
builder dropped every decision with such a side. A review found the bias in that: whether an exit fills is
known only after the decision, and the trades that cannot be closed are the expensive ones. Leaving them out
made every evaluation look better than the trading it stood for.

The rule now:

- **An entry that would not have filled is no trade.** The side's outcome is nothing made and nothing lost,
  and the row is kept. A report counts it as unfilled; it is not a trade and does not enter a mean per trade.
- **An exit that would not have filled at the horizon is priced late.** It is sent again on each later
  confirmation of the pair's book, as a sleeve would, and priced at the first one the shadow venue would have
  filled, up to the label's `exit_wait_ns`.
- **Past that wait it is priced at a stated worst case:** `stuck_increments` price increments against the
  position, with both commissions.
- A row says how each side was priced, a dataset counts each kind, and so does every report.
- `end_ns` is when the later side was settled, so the purge of ADR 0021 still goes by what each label read.
- The only decision without a row is one whose label had not been settled when the recording stops.

The label spec is version 2 and the dataset format version 2. With no wait and no penalty configured a stuck
exit costs its two commissions only, which is no worst case: `config/learn.yaml` has no default for either.
The shadow venue still reproduces, to the unit, exactly the outcomes marked as filled at the horizon.
