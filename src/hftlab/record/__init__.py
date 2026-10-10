"""The recorder: every quote update, appended to a store, in its own process (ADR 0004, ADR 0012).

`format` and `segment` are the bytes on disk; `store` is the directory, its quota and its way out; `gaps`
explains every silence; `recorder` is the loop. Nothing here imports a network library: quotes arrive through
the venue port, as a view that has no order methods. The recorder never places an order and keeps running if
the trader stops.
"""
