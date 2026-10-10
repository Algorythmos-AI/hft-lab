"""The independent risk gate: limits, market guards, loss latches and switches, before every order (ADR 0015)."""
from hftlab.risk.config import (
    DeskLimits,
    LossLatch,
    MarketGuards,
    PairLimits,
    RiskConfig,
    RiskConfigError,
    RiskNotInForce,
    SleeveLimits,
    load_risk_config,
    parse_risk_config,
)
from hftlab.risk.gate import LatchError, RiskGate, Switches, latch_name
from hftlab.risk.switches import file_switches
from hftlab.risk.verdict import Outcome, Reason, Verdict

__all__ = [
    "DeskLimits",
    "LatchError",
    "LossLatch",
    "MarketGuards",
    "Outcome",
    "PairLimits",
    "Reason",
    "RiskConfig",
    "RiskConfigError",
    "RiskGate",
    "RiskNotInForce",
    "SleeveLimits",
    "Switches",
    "Verdict",
    "file_switches",
    "latch_name",
    "load_risk_config",
    "parse_risk_config",
]
