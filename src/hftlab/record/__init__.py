"""The recorder: every quote update, appended to a store, in its own process. Empty until F2.

Network libraries may be imported only here and under `hftlab.venues` (tests/safety/test_isolation.py). The
recorder never places an order and keeps running if the trader stops (ADR 0004).
"""
