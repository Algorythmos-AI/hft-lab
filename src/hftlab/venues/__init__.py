"""Venue adapters: the paper broker connection and the simulated venue. Empty until F1.

Network libraries may be imported only here and under `hftlab.record` (tests/safety/test_isolation.py). The
venues that may exist are listed in `hftlab.safety.venues`, and every broker connection made here goes through
the paper-only lock.
"""
