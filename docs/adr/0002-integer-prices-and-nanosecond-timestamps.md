# ADR 0002: Integer prices and nanosecond receive timestamps

- **Status:** accepted, 2026-10-10
- **Context:** F0 foundation

## Context

The platform's results are sums of many small differences: a few price units of spread, thousands of times
a week. Two things would quietly spoil them.

- **Binary floats.** `0.1 + 0.2` is not `0.3`. A price held as a float can differ from the price the venue
  quoted in the last bit, which changes a comparison against a limit, and a book that adds floats in a
  different order on replay no longer matches the live run.
- **Loose time.** A wall-clock float in seconds cannot order two quotes that arrive five milliseconds apart
  with any margin, and a clock read wherever it is convenient makes a replay impossible.

## Decision

1. **A price is an integer** at the pair's quote precision: EUR.USD 1.08512 at scale 5 is `108512`, USD.JPY
   151.234 at scale 3 is `151234`. Each `Instrument` carries its `price_scale`.
2. **A quantity is a whole number** of base-currency units.
3. **A cash amount is an integer** in millionths of its currency (`CASH_SCALE = 6`).
4. **Conversion happens once, at the edge, exactly.** `hftlab.core.money` turns the venue's decimal text into
   integers and back. It accepts decimal strings, `Decimal` and `int`. It refuses `float` and `bool`, and it
   raises rather than round a value the scale cannot hold.
5. **A position is two exact sums**, quantity and quote-currency cash flow, not a quantity and an average
   price. An average has to round; two sums do not, and booking is then independent of the order of fills.
6. **Time is integer UTC nanoseconds since the Unix epoch.** Every market event carries the time this process
   *received* it and a sequence number from the recorder.
7. **Time is injected.** Code is handed a `Clock`. `WallClock` is the only code allowed to read system time,
   and a test scans the package to keep it that way. A replay uses `SimClock`, which only moves forward.

## Consequences

- Equal inputs give bit-identical results on every machine, which deterministic replay depends on
  ([ADR 0005](0005-deterministic-replay.md)).
- Every boundary needs the pair's scale: a price without its instrument is meaningless. That is deliberate.
- The mid price can fall between two price units, so `Quote` exposes `mid_x2` (bid plus ask) and never a mid.
- Receive time is not event time. Latency between the venue and this process is measured, not hidden.
- Statistics and model features may use floats. They are derived values and never flow back into a price, a
  quantity or a book.
