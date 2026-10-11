"""The scorer in a process of its own: the message format, the child, and the trader's side of the pipe (ADR 0023).

Nothing is imported here on purpose. The child is started as `python -m hftlab.scoring.child`, and what it
loads is what `child` and `wire` import: the model file, the scorers and nothing that could reach a venue.
"""
