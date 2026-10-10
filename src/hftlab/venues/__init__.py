"""Venues: the port the platform trades through, the broker adapter behind it and a fake broker for tests.

Network libraries, and the broker's client library, may be imported only under `hftlab.venues.ibkr`
(tests/safety/test_isolation.py); the port, its guard and the fake broker are plain computation. The venues
that may exist are listed in `hftlab.safety.venues`, and every broker connection goes through the paper-only
lock before it may be used.
"""
