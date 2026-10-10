"""The trader: sleeves, the risk gate, the order manager and two books in one loop, on paper only (ADR 0019)."""
from hftlab.trader.session import Silent, Trader, TraderRefused, Why
from hftlab.trader.stages import STAGES, Change, Rollout, Rule, Stages
from hftlab.trader.tape import LiveEdge, ReplayEdge, TapeEnded, TapeError

__all__ = [
    "STAGES", "Change", "LiveEdge", "ReplayEdge", "Rollout", "Rule", "Silent", "Stages", "TapeEnded", "TapeError",
    "Trader", "TraderRefused", "Why",
]  # fmt: skip
