"""The host as decided: who the platform runs as, where it lives, and what it calls its own (ADR 0007, ADR 0017).

One small Linux server is shared with two other trading desks, which are in service and must win under
pressure. Everything of this platform's has its own name there, so nothing can collide with theirs and nothing
here reads their state. These are the names. They are constants, not settings: another value is a reviewed
change to this file, to the units rendered from it and to the runbooks.

The few values that do differ from machine to machine, and every secret, come from the process environment,
each read by its literal name in `settings`. No value of any of them is in this repository.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Final

USER: Final = "hft"
GROUP: Final = "hft"
INSTALL_ROOT: Final = "/home/hft/hft-lab"
STATE_DIR: Final = "/home/hft/hft-lab/var"  # the recordings, the journals, the alert state and the switches
PYTHON: Final = "/home/hft/hft-lab/.venv/bin/python"
CONFIG_DIR: Final = "/etc/hft"
SECRETS_FILE: Final = "/etc/hft/secrets.env"  # written by the owner; this repository never holds its contents
GATEWAY_DIR: Final = "/etc/hft/gateway"  # the gateway controller's own configuration, the owner's too
BACKUP_DIR: Final = "/var/backups/hft"  # a directory the owner mounts or syncs somewhere else
UNIT_DIR: Final = "/etc/systemd/system"
SLICE: Final = "hft.slice"
UNIT_PREFIX: Final = "hft-"
ALERT_PREFIX: Final = "hft:"
BACKUP_TAG: Final = "hft-lab"

# The other desks' names, known here only so that a test can show nothing of ours collides with them and the
# units can keep their home out of sight. Nothing in this package opens a path under it.
OTHER_DESKS_UNIT_PREFIX: Final = "wt-"
OTHER_DESKS_HOME: Final = "/home/wt"

KILL: Final = "KILL"  # while it exists: no new entries, exits still managed
FLATTEN: Final = "FLATTEN"  # while it exists: every position is closed

SETTINGS: Final = (
    "HFT_STATE_DIR", "HFT_ALERT_URL", "HFT_PUBLISH_URL", "HFT_PUBLISH_KEY_ID", "HFT_PUBLISH_KEY",
    "HFT_PUBLISH_BYPASS", "HFT_BACKUP_DIR",
)  # fmt: skip


class Secret:
    """A value that must not reach a log. Printed, it says only whether it is set."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """The value itself, for the one place that sends it."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "<set>" if self._value else "<not set>"

    __str__ = __repr__


def settings() -> dict[str, str]:
    """What this layer takes from the process environment. An unset name is an empty string.

    Each name is read where it is spelled, so the scan in tests/safety/test_isolation.py can see that none of
    them is a variable of the paper-only lock.
    """
    return {
        "HFT_STATE_DIR": os.environ.get("HFT_STATE_DIR", ""),
        "HFT_ALERT_URL": os.environ.get("HFT_ALERT_URL", ""),
        "HFT_PUBLISH_URL": os.environ.get("HFT_PUBLISH_URL", ""),
        "HFT_PUBLISH_KEY_ID": os.environ.get("HFT_PUBLISH_KEY_ID", ""),
        "HFT_PUBLISH_KEY": os.environ.get("HFT_PUBLISH_KEY", ""),
        "HFT_PUBLISH_BYPASS": os.environ.get("HFT_PUBLISH_BYPASS", ""),
        "HFT_BACKUP_DIR": os.environ.get("HFT_BACKUP_DIR", ""),
    }


def state_dir(given: str = "") -> Path:
    """The platform's state directory: the one given, or `var` under the working directory."""
    return Path(given or "var")


def switches(state: Path) -> tuple[bool, bool]:
    """(KILL is set, FLATTEN is set). The switches are files in the state directory; this only looks."""
    return (state / KILL).exists(), (state / FLATTEN).exists()
