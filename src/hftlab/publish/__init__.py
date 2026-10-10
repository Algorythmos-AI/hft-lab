"""Publishing the status snapshot: built from the status reading, checked against its contract, signed and sent.

`contract` checks a snapshot against contracts/hft-snapshot.v1.schema.json; `snapshot` builds one from what
hftlab.ops.status read; `send` signs it and posts it. `python -m hftlab.publish` prints the snapshot unless it
is told to send it (ADR 0018, docs/runbooks/publish.md).
"""
