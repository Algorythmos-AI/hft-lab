# ADR 0004: The recorder is separate from the trader

- **Status:** accepted, 2026-10-10
- **Context:** F0 foundation; built in F2

## Context

The platform's own recordings are its most valuable asset: they are the training data, the replay input and
the evidence behind every result. Data only accumulates with time, and a gap can never be filled afterwards,
because there is no public tape to buy it back from.

The trader is the part most likely to fail. It holds the models, the strategies and the order path, and it
is the part that changes every week.

## Decision

1. **Recording is its own process** (`hft-recorder`) whose sole job is to write every quote update to an
   append-only store. It imports no strategy, model or order code and never places an order.
2. **It keeps running when the trader stops**, for a crash, a deploy, a tripped latch or the `KILL` switch.
3. **It starts as early as possible** (F2), well before anything trades, because data only accumulates with time.
4. **Every gap is accounted for.** A gap has a recorded cause (market closed, disconnect, restart) or it is a
   defect. The target is the recorder up for at least 99.5% of market hours.
5. **Recordings never enter git.** They are kept locally for a rolling window, compressed and offloaded.
   `data/` and `var/` are git-ignored, and the leak check refuses the file types.

## Consequences

- Two processes read the same quote stream. Whether that is one broker connection shared or two is decided
  by what the capability probe measures.
- The trader can be restarted or rolled back freely without losing data.
- The recorder must be boring: small, rarely changed, and tested against disconnects, weekend gaps, disk
  full and a kill mid-write.
- Replay reads the recorder's output, so the recorder's schema is a contract with a version.
