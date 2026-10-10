"""Paging the owner: once when a problem starts, and once when it is over.

    alerts = Alerts(state / "alerts", sender, clock)
    alerts.fire("recorder_stale", "Recorder silent", "No status for 95 s while the market is open.")
    alerts.resolve("recorder_stale", "Recorder back", "Its status is 3 s old.")

    python -m hftlab.ops.alerts test       send one test page, to check the address the owner configured
    python -m hftlab.ops.alerts firing     list the keys that are firing

  * A key is this platform's own. Every key is kept and sent with the prefix `hft:`, so a page can never be
    taken for another desk's, and two desks' states can never be confused.
  * Once per key. The state is a file, so a job that runs every few minutes pages when a problem starts and
    not again while it lasts. A page that could not be delivered is tried again at the next `fire` of its key.
  * `resolve` says it is over, only if the owner was told it had started.
  * How a page travels is injected. `PushSender` posts plain text with a title and a priority to the
    push-notification address taken from the environment. With no address configured, `LogSender` writes the
    page to the log and does nothing else.
  * No secret reaches a log. The address is never printed, and anything shaped like an account id is masked
    before a page leaves.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from hftlab.core import Clock, WallClock
from hftlab.ops import host
from hftlab.ops.post import Poster, post

STATE_FILE: Final = "state.json"
TITLE_MAX, MESSAGE_MAX = 120, 1_000
_KEY: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,58}")
_ACCOUNT: Final = re.compile(r"\bD?U[0-9]{4,}\b")


class AlertError(ValueError):
    """A key that is not a key. Raised for a mistake in the calling code, never for a failed delivery."""


@dataclass(frozen=True, slots=True)
class Page:
    key: str  # with the platform's prefix
    title: str
    message: str
    priority: int  # 1 (for the record) to 5 (now)
    resolved: bool = False


class Sender(Protocol):
    def __call__(self, page: Page) -> bool:
        """Deliver one page. True when it is delivered, or when there is nowhere to deliver it to."""
        ...


def scrub(text: str) -> str:
    """The text with anything shaped like a broker account id masked, on one line."""
    return _ACCOUNT.sub("[account]", " ".join(text.split()))


class LogSender:
    """Nowhere to send a page: it is written to the log once, and that counts as dealt with."""

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log

    def __call__(self, page: Page) -> bool:
        what = "resolved" if page.resolved else "ALERT"
        self._log(f"alerts: no address is configured, so not sent. {what} [{page.key}] {page.title}: {page.message}")
        return True


class PushSender:
    """Posts a page to the owner's push-notification address: the text as the body, title and priority as headers."""

    def __init__(self, address: host.Secret, log: Callable[[str], None], poster: Poster = post) -> None:
        self._address, self._log, self._post = address, log, poster

    def __call__(self, page: Page) -> bool:
        headers = {
            "Title": f"[hft] {page.title}".encode("ascii", "replace").decode("ascii"),
            "Priority": str(page.priority),
            "Tags": "white_check_mark" if page.resolved else "warning",
        }
        outcome = self._post(self._address.reveal(), page.message.encode("utf-8"), headers)
        self._log(f"alerts: [{page.key}] {'sent' if outcome.ok else f'NOT sent ({outcome})'}")
        return outcome.ok


def sender_from(settings: Mapping[str, str], log: Callable[[str], None], poster: Poster = post) -> Sender:
    """The sender the settings ask for: the push address when there is one, the log when there is none."""
    address = host.Secret(settings.get("HFT_ALERT_URL", ""))
    return PushSender(address, log, poster) if address else LogSender(log)


class Alerts:
    """Fire and resolve by key, with the state of every key in `root`/state.json."""

    def __init__(self, root: Path, sender: Sender, clock: Clock, *, prefix: str = host.ALERT_PREFIX) -> None:
        self.root, self._deliver, self._clock, self._prefix = root, sender, clock, prefix

    def key(self, name: str) -> str:
        bare = name.removeprefix(self._prefix)
        if not _KEY.fullmatch(bare):
            raise AlertError(f"an alert key is lower-case letters, digits, '_', '.' and '-', not {name!r}")
        return self._prefix + bare

    def fire(self, name: str, title: str, message: str, priority: int = 4) -> bool:
        """Page that `name` has started, unless the owner already knows. True when a page went out now."""
        key, state = self.key(name), self._read()
        was = state.get(key, {})
        if was.get("firing") and was.get("delivered"):
            return False
        page = Page(key, scrub(title)[:TITLE_MAX], scrub(message)[:MESSAGE_MAX], max(1, min(5, priority)))
        delivered = self._deliver(page)
        since = was.get("since_ns") if was.get("firing") else None
        state[key] = {"firing": True, "delivered": delivered, "since_ns": since or self._clock.now_ns()}
        self._write(state)
        return delivered

    def resolve(self, name: str, title: str, message: str) -> bool:
        """Page that `name` is over, if the owner was told it had started. True when a page went out now."""
        key, state = self.key(name), self._read()
        was = state.get(key, {})
        if not was.get("firing"):
            return False
        delivered = True
        if was.get("delivered"):  # a start that never reached the owner needs no "it is over"
            delivered = self._deliver(Page(key, scrub(title)[:TITLE_MAX], scrub(message)[:MESSAGE_MAX], 2, True))
        if delivered:
            del state[key]
            self._write(state)
        return delivered and bool(was.get("delivered"))

    def firing(self) -> list[str]:
        return sorted(key for key, entry in self._read().items() if entry.get("firing"))

    def _read(self) -> dict[str, dict[str, object]]:
        """The state on disk. A file that cannot be read as one is no state: a page too many beats one too few."""
        try:
            loaded = json.loads((self.root / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {key: entry for key, entry in loaded.items() if isinstance(key, str) and isinstance(entry, dict)}

    def _write(self, state: Mapping[str, object]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        partial = self.root / (STATE_FILE + ".partial")
        partial.write_text(json.dumps(state, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        os.replace(partial, self.root / STATE_FILE)


def _log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.ops.alerts", description="Check the owner's paging.")
    parser.add_argument("command", choices=["test", "firing"])
    args = parser.parse_args(argv)
    settings = host.settings()
    state = host.state_dir(settings["HFT_STATE_DIR"]) / "alerts"
    if args.command == "firing":
        print("\n".join(Alerts(state, LogSender(_log), WallClock()).firing()) or "nothing is firing")
        return 0
    page = Page(host.ALERT_PREFIX + "test", "Test page", "A test page from HFT Lab. Nothing is wrong.", 2)
    return 0 if sender_from(settings, _log)(page) else 1


if __name__ == "__main__":
    sys.exit(main())
