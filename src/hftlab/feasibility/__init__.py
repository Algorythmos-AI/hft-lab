"""The feasibility map: where, by pair and holding time, the price moves by more than a round trip costs."""
from hftlab.feasibility.map import FeasibilityError, Settings, feasibility_map, load_settings, quantile, sample_points

__all__ = ["FeasibilityError", "Settings", "feasibility_map", "load_settings", "quantile", "sample_points"]
