# ADR 0008: The learning loop: weekend training, automatic promotion, automatic rollback

- **Status:** accepted, 2026-10-10
- **Context:** F0 foundation; built in F6

## Context

The platform is meant to improve without a person in the loop: retrain on new data and put a better model
into service. Done naively that is a machine for fooling itself. With enough candidates one will look good
on the past by chance, a model that saw the future in its features will look brilliant, and a weekly swap
with no way back turns one bad model into a bad week.

## Decision

1. **Training happens at the weekend,** with the market closed and the trading processes stopped.
2. **Data.** The platform's own recordings first. Free tick history may be used for pre-training, tagged by
   source and never committed.
3. **Targets are cost-aware:** price change over several horizons after costs, and triple-barrier outcomes.
   Never price levels.
4. **Validation.** Walk-forward by week with purge and embargo; hold-out weeks that stay untouched; every
   trial counted in `config/trial_registry.yaml`; deflated Sharpe and a day-block bootstrap. Features are
   stamped strictly before the label starts, and a planted future-peek feature must be caught by the tests.
5. **A three-way report every time:** oracle (hindsight best), model, random.
6. **Promotion is automatic and gated.** A challenger runs in shadow for a full week. It replaces the
   champion only if it passes a gate written down beforehand: a minimum trade count, a confidence-interval
   lower bound above the champion after costs, a drawdown bound, and no reconciliation breaks. All of it is
   measured in the shadow book ([ADR 0003](0003-two-books.md)).
7. **Rollback is automatic.** A promoted model that breaches its loss latch is replaced by the previous
   champion without waiting for anyone. A forced rollback drill is part of the exit gate.
8. **Rollout is staged:** shadow only, then minimum-size paper orders, then normal paper size. A tripped
   latch steps a sleeve back down. Every step sends an alert.
9. **Drift is watched:** feature shift, calibration decay and a change of spread regime step a sleeve down
   to the control automatically.
10. **Model files are artefacts, not source.** They are stored outside git with a content hash, and the
    leak check refuses them.

## Consequences

- The promotion gate is a research record. Changing it is an evaluation change: a hypothesis, an experiment
  and a decision, written before the affected results are viewed.
- Most weeks the right outcome is "no promotion". The loop is judged by whether it refuses bad models, not
  by how often it swaps.
- A deep sleeve starts in shadow and must beat the simpler model sleeve before it may trade.
- None of this is a claim that a model will be profitable. The control sleeve and the feasibility map come
  first and may show that, after costs, nothing is.
