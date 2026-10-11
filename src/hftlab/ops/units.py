"""The platform's systemd units, rendered from code (ADR 0007, ADR 0017).

    python -m hftlab.ops.units render --out DIR       write the slice, the services and the timers into DIR
    python -m hftlab.ops.units diff --unit-dir DIR    which units installed in DIR differ from the code

deploy/systemd/ holds the rendered files and a test fails when the two differ, so what is reviewed is what gets
installed. Nothing here installs, enables or starts a unit: `deploy/bin/hft-install` copies the files when the
owner runs it, and enabling is a flag the owner passes or a command the owner types.

What every service has, and why:

  * `Slice=hft.slice`. The slice caps the whole platform at 1.2 GB of memory with no swap and gives it a low
    share of CPU and disk, so under pressure the other desks on the host win (ADR 0007). Each service also has
    a memory fuse of its own, so that one of ours cannot take the slice from the recorder.
  * `OOMScoreAdjust` above zero: if the host itself runs out of memory, ours are killed before the other
    desks' units, and the recorder last among ours.
  * `User=hft`, a read-only file system but for the state directory, and the other desks' home out of sight.
  * `Environment=MODE=paper`, and no unit names the flag that would enable live trading. The services that
    connect to the gateway load no environment file, so nothing on disk can change the mode they are given;
    and the paper-only lock refuses anything but paper whatever a unit says (ADR 0001).
  * The trader's unit, `hft-trader.service`, has no install section: it cannot be enabled, by the install
    script or by hand, and runs only while the owner has started it (docs/runbooks/trader.md). The reconciler
    the plan drew as a unit of its own runs inside the trader's loop, on the trader's schedule: it needs the
    order manager's book, and two processes on one outbox is what ADR 0016 rules out. There is no
    `hft-reconciler` unit.
  * No credential. The gateway keeps the broker login; the publisher's key and the alert address are in the
    owner's secrets file, which only the services that need them load.
  * The learning loop's two jobs (ADR 0023) start at times that are the owner's: `learning.train_at` and
    `learning.promote_at` of config/ops.yaml, a Saturday and a Sunday before the week opens, in New York
    time. Training must not overlap the other desks' weekend jobs, which only the owner knows. Their timers
    can be enabled, by the owner by hand; the install script refuses to. Training is capped at the plan's
    2 GB, and inside the slice the slice's own cap binds first.
  * The report's timer (`hft-report.timer`) is the owner's to enable by hand as well: a page a day is a thing
    to ask for, not to be given. Its time follows New York, as the rollover does.

The memory cap of the slice is the plan's number. The fuses, weights and times marked ENGINEERING DEFAULT are
first choices, to be replaced by what the first weeks on the host measure.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from hftlab.ops import host

SLICE_MEMORY_MAX: Final = "1200M"  # decided by the plan: about 1.2 GB for the whole platform (ADR 0007)
SLICE_MEMORY_HIGH: Final = "1000M"  # ENGINEERING DEFAULT: reclaim starts here, before the kill at the cap
SLICE_CPU_WEIGHT: Final = 20  # ENGINEERING DEFAULT: a fifth of a default unit's share when the CPU is contended
SLICE_IO_WEIGHT: Final = 20  # ENGINEERING DEFAULT: the same for the disk
SLICE_TASKS_MAX: Final = 512  # ENGINEERING DEFAULT: a runaway cannot fill the process table
RESTART_FIRST_S: Final = 5  # a failed service is started again after this long ...
RESTART_LAST_S: Final = 300  # ... and the wait grows in steps to this, so a crash loop cannot spin
RESTART_STEPS: Final = 6
REFUSED: Final = 2  # the exit code of "refused or misconfigured": a person is needed, a restart is not
CONFIG: Final = Path("config/ops.yaml")  # where the owner writes when the weekend's two jobs start
TRAIN_MEMORY_MAX: Final = "2G"  # decided by the plan: training is capped at 2 GB. The slice's 1.2 GB binds first
TRAIN_TIMEOUT_S: Final = 6 * 3_600  # ENGINEERING DEFAULT: no training run has been measured on the host yet
# enabled by the owner's own command, never by the script
BY_HAND: Final = ("hft-train.timer", "hft-promote.timer", "hft-report.timer")
# The market is shut from Friday 17:00 to Sunday 17:15 New York. Training has all of Saturday; the decision is
# made on Sunday before 17:00, so that a trader starting the week reads the lineup it wrote.
_SATURDAY: Final = re.compile(r"Sat \*-\*-\* ([01][0-9]|2[0-3]):[0-5][0-9]:00 America/New_York")
_SUNDAY: Final = re.compile(r"Sun \*-\*-\* (0[0-9]|1[0-6]):[0-5][0-9]:00 America/New_York")


class UnitsError(ValueError):
    """A time the owner set for a weekend job is not one a timer of this platform may have."""


@dataclass(frozen=True, slots=True)
class Timer:
    when: str  # OnCalendar=
    why: str
    persistent: bool = False  # run once after a boot that missed it


@dataclass(frozen=True, slots=True)
class Service:
    """One unit of the platform. `reach` is what it may talk to: the loopback gateway, the internet, or nothing."""

    name: str
    what: str
    command: tuple[str, ...]  # after the interpreter
    reach: str  # "gateway" | "out" | "none"
    memory_max: str  # ENGINEERING DEFAULT, every one
    oom_score: int
    daemon: bool = False  # runs until stopped and is restarted with back-off; otherwise a job a timer starts
    no_restart_on: tuple[int, ...] = (REFUSED,)
    secrets: bool = False
    first: tuple[str, ...] = ()  # a command run before the main one; its failure does not stop the main one
    then: tuple[str, ...] = ()  # a command run after the main one has succeeded; its failure does not fail the unit
    timer: Timer | None = None
    timeout_s: int = 300
    writes: tuple[str, ...] = (host.STATE_DIR,)
    only_if_directory: str = ""
    enable: bool = True  # False: the unit has no install section, so nothing can enable it; the owner starts it

    @property
    def unit(self) -> str:
        return f"{host.UNIT_PREFIX}{self.name}.service"


SERVICES: Final = (
    Service(
        "recorder",
        "every quote of the configured pairs, appended to the store. Market data only: it cannot place an order",
        ("-m", "hftlab.record"),
        reach="gateway", memory_max="300M", oom_score=100, daemon=True,
    ),
    Service(
        "housekeep",
        "compress the closed hours of the recording and hand old ones to the offload, then compress the closed "
        "parts of the trader's tapes",
        ("-m", "hftlab.record", "--housekeep"),
        reach="none", memory_max="200M", oom_score=500, timeout_s=1_800,
        then=("-m", "hftlab.trader", "--housekeep"),
        timer=Timer("*:07:00", "hourly, a few minutes after the hour has closed"),
    ),
    Service(
        "shadow",
        "the sleeves on quotes from the paper gateway, their orders on the shadow venue. No broker order",
        ("-m", "hftlab.shadow", "--venue", "ibkr_paper"),
        reach="gateway", memory_max="300M", oom_score=400, daemon=True, no_restart_on=(1, REFUSED),
    ),
    Service(
        "trader",
        "the sleeves, the risk gate, the order manager and the reconciler in one loop, on the paper account. "
        "Shadow-only while the risk limits are not in force. Started by the owner, never enabled",
        ("-m", "hftlab.trader"),
        reach="gateway", memory_max="300M", oom_score=300, daemon=True, enable=False,
    ),
    Service(
        "publish",
        "judge the recorder's status, page on a change, and send the status snapshot",
        ("-m", "hftlab.publish", "--send"),
        reach="out", memory_max="150M", oom_score=500, secrets=True,
        first=("-m", "hftlab.ops.status", "--alert", "--quiet"),
        timer=Timer("*:0/5:20", "every five minutes"),
    ),
    Service(
        "backup",
        "copy the journals and the run counter to the backup directory. It never deletes",
        ("-m", "hftlab.ops.backup", "run"),
        reach="none", memory_max="200M", oom_score=500, timeout_s=1_800,
        timer=Timer("*-*-* 22:30:00 America/New_York", "nightly, after the other desks' evening jobs", True),
        writes=(host.STATE_DIR, host.BACKUP_DIR), only_if_directory=host.BACKUP_DIR,
    ),
    Service(
        "report",
        "the daily summary after the rollover and, after the Friday close, the weekly scorecard: written to a file "
        "and sent as a page. It reads the journals and changes nothing else",
        ("-m", "hftlab.ops.report", "due", "--send"),
        reach="out", memory_max="200M", oom_score=500, secrets=True, timeout_s=600,
        timer=Timer(
            "Mon..Fri *-*-* 17:10:00 America/New_York", "each trading day, ten minutes after the 17:00 rollover", True
        ),
    ),
)  # fmt: skip


def weekend_times(config: Path = CONFIG) -> tuple[str, str]:
    """When training and the weekly decision start: the owner's two values, each checked to fall in the weekend."""
    try:
        loaded: Any = yaml.safe_load(config.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise UnitsError(f"cannot read {config}: {exc}") from exc
    learning = loaded.get("learning") if isinstance(loaded, dict) else None
    if not isinstance(learning, dict) or set(learning) != {"train_at", "promote_at"}:
        raise UnitsError(f"{config}: `learning` gives train_at and promote_at, and nothing else")
    train, promote = learning["train_at"], learning["promote_at"]
    if not isinstance(train, str) or not _SATURDAY.fullmatch(train):
        raise UnitsError(f"{config}: train_at is `Sat *-*-* HH:MM:00 America/New_York`, not {train!r}")
    if not isinstance(promote, str) or not _SUNDAY.fullmatch(promote):
        raise UnitsError(f"{config}: promote_at is `Sun *-*-* HH:MM:00 America/New_York` before 17:00, not {promote!r}")
    return train, promote


def weekend(train_at: str, promote_at: str) -> tuple[Service, Service]:
    """The learning loop's two jobs (ADR 0023). Neither holds a venue; neither is enabled by the install script."""
    return (
        Service(
            "train",
            "the weekend training run: a walk-forward report and a candidate in the registry. It serves nothing",
            ("-m", "hftlab.learn.train"),
            reach="none", memory_max=TRAIN_MEMORY_MAX, oom_score=600, timeout_s=TRAIN_TIMEOUT_S,
            timer=Timer(train_at, "Saturday, with the market shut; the time is the owner's (config/ops.yaml)"),
        ),
        Service(
            "promote",
            "the weekly decision on the lineup: promote, keep or roll back by the registered rule, and page",
            ("-m", "hftlab.learn.promote"),
            reach="out", memory_max="300M", oom_score=500, secrets=True, timeout_s=1_800,
            first=("-m", "hftlab.learn.drift"),
            timer=Timer(promote_at, "Sunday, before the week opens; the time is the owner's (config/ops.yaml)"),
        ),
    )  # fmt: skip


GATEWAY: Final = f"{host.UNIT_PREFIX}gateway.service"
GATEWAY_CONFIG: Final = f"{host.GATEWAY_DIR}/config.ini"  # the controller's own file: the paper login lives there
GATEWAY_START: Final = f"{host.GATEWAY_DIR}/start"  # the owner's few lines that start the controller in paper mode
GATEWAY_MEMORY_MAX: Final = "900M"  # ENGINEERING DEFAULT: what the gateway needs is not measured yet (ADR 0010)

_SANDBOX: Final = (
    "UMask=0027",
    "NoNewPrivileges=yes",
    "PrivateTmp=yes",
    "PrivateDevices=yes",
    "ProtectSystem=strict",
    "ProtectProc=invisible",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes",
    "ProtectClock=yes",
    "ProtectHostname=yes",
    "RestrictSUIDSGID=yes",
    "RestrictRealtime=yes",
    "RestrictNamespaces=yes",
    "LockPersonality=yes",
    "SystemCallArchitectures=native",
    "CapabilityBoundingSet=",
    "AmbientCapabilities=",
    "LimitCORE=0",
)
_REACH: Final = {
    # The recorder and the shadow runner talk to the gateway on this machine and to nothing else.
    "gateway": ("RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", "IPAddressDeny=any", "IPAddressAllow=localhost"),
    "out": ("RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK",),
    "none": ("PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX"),
}


def _identity(working_directory: str = host.INSTALL_ROOT) -> list[str]:
    return [
        f"User={host.USER}",
        f"Group={host.GROUP}",
        f"Slice={host.SLICE}",
        f"WorkingDirectory={working_directory}",
        "Environment=MODE=paper",
    ]


def slice_unit() -> str:
    return "\n".join([
        "[Unit]",
        "Description=HFT Lab: one capped slice for every unit of the platform, so the other desks win under pressure",
        "",
        "[Slice]",
        f"MemoryHigh={SLICE_MEMORY_HIGH}",
        f"MemoryMax={SLICE_MEMORY_MAX}",
        "MemorySwapMax=0",
        f"CPUWeight={SLICE_CPU_WEIGHT}",
        f"IOWeight={SLICE_IO_WEIGHT}",
        f"TasksMax={SLICE_TASKS_MAX}",
    ]) + "\n"  # fmt: skip


def service(spec: Service) -> str:
    python = host.PYTHON
    unit = [
        "[Unit]",
        f"Description=HFT Lab {spec.name}: {spec.what}",
        f"Documentation=file://{host.INSTALL_ROOT}/docs/runbooks/README.md",
    ]
    if spec.reach != "none":
        # time-sync.target: a receive time is only worth recording against a clock that has been set
        unit += ["Wants=network-online.target time-sync.target", "After=network-online.target time-sync.target"]
    if spec.reach == "gateway":
        unit.append(f"After={GATEWAY}")  # order only: with the gateway down these retry, they do not fail
    if spec.only_if_directory:
        unit.append(f"ConditionPathIsDirectory={spec.only_if_directory}")
    if spec.daemon:
        unit.append("StartLimitIntervalSec=0")  # the back-off below bounds a crash loop; no start is ever refused
    run = [
        "",
        "[Service]",
        "Type=exec" if spec.daemon else "Type=oneshot",
        *_identity(),
        "Environment=PYTHONPATH=src",
        "Environment=PYTHONUNBUFFERED=1",
        "Environment=PYTHONDONTWRITEBYTECODE=1",
        "Environment=TZ=UTC",
        f"Environment=HFT_STATE_DIR={host.STATE_DIR}",
    ]
    if host.BACKUP_DIR in spec.writes:
        run.append(f"Environment=HFT_BACKUP_DIR={host.BACKUP_DIR}")
    if spec.secrets:
        run.append(f"EnvironmentFile=-{host.SECRETS_FILE}")
    if spec.first:
        run.append(f"ExecStartPre=-{python} {' '.join(spec.first)}")
    run.append(f"ExecStart={python} {' '.join(spec.command)}")
    if spec.then:
        run.append(f"ExecStartPost=-{python} {' '.join(spec.then)}")
    if spec.daemon:
        run += [
            "Restart=on-failure",
            f"RestartSec={RESTART_FIRST_S}",
            f"RestartSteps={RESTART_STEPS}",
            f"RestartMaxDelaySec={RESTART_LAST_S}",
            f"RestartPreventExitStatus={' '.join(map(str, spec.no_restart_on))}",
            "KillSignal=SIGTERM",
            "KillMode=mixed",
            "TimeoutStopSec=30",
            "Nice=5",
        ]
    else:
        run += [f"TimeoutStartSec={spec.timeout_s}", "Nice=15", "IOSchedulingClass=idle"]
    run += [f"MemoryMax={spec.memory_max}", f"OOMScoreAdjust={spec.oom_score}"]
    run += [f"ReadWritePaths={' '.join(spec.writes)}", f"InaccessiblePaths=-{host.OTHER_DESKS_HOME}"]
    run += [*_SANDBOX, *_REACH[spec.reach]]
    install = ["", "[Install]", "WantedBy=multi-user.target"] if spec.daemon and spec.enable else []
    return "\n".join(unit + run + install) + "\n"


def timer(spec: Service) -> str:
    assert spec.timer is not None
    return "\n".join([
        "[Unit]",
        f"Description=Start HFT Lab {spec.name}: {spec.timer.why}",
        "",
        "[Timer]",
        f"OnCalendar={spec.timer.when}",
        "AccuracySec=10s",
        f"Persistent={'true' if spec.timer.persistent else 'false'}",
        f"Unit={spec.unit}",
        "",
        "[Install]",
        "WantedBy=timers.target",
    ]) + "\n"  # fmt: skip


def gateway() -> str:
    """The broker's gateway, described and not enabled: it has no install section, and nothing here installs it.

    A standard third-party controller starts the gateway, keeps it logged in and restarts it daily. Its
    configuration, which holds the paper login, is the owner's file outside this repository; the unit names
    where it is and nothing of what is in it. It starts only when the owner has put both files in place.
    """
    return "\n".join([
        "[Unit]",
        "Description=HFT Lab gateway (PLACEHOLDER, never enabled from the repository): the broker's gateway in "
        "paper mode, kept alive by a standard third-party controller",
        f"Documentation=file://{host.INSTALL_ROOT}/docs/runbooks/gateway-down.md",
        f"ConditionPathExists={GATEWAY_CONFIG}",
        f"ConditionFileIsExecutable={GATEWAY_START}",
        "Wants=network-online.target time-sync.target",
        "After=network-online.target time-sync.target",
        "",
        "[Service]",
        "Type=exec",
        *_identity(f"/home/{host.USER}"),
        f"Environment=HFT_GATEWAY_CONFIG={GATEWAY_CONFIG}",
        f"ExecStart={GATEWAY_START}",
        "Restart=on-failure",
        "RestartSec=30",
        f"RestartSteps={RESTART_STEPS}",
        f"RestartMaxDelaySec={RESTART_LAST_S}",
        "TimeoutStopSec=60",
        f"MemoryMax={GATEWAY_MEMORY_MAX}",
        "OOMScoreAdjust=300",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        f"InaccessiblePaths=-{host.OTHER_DESKS_HOME}",
        "LimitCORE=0",
    ]) + "\n"  # fmt: skip


def render_all(config: Path = CONFIG) -> dict[str, str]:
    """Every unit file by name. The same text for the same code and the same two times of config/ops.yaml:
    nothing of the machine or the day goes in."""
    out = {host.SLICE: slice_unit()}
    for spec in (*SERVICES, *weekend(*weekend_times(config))):
        out[spec.unit] = service(spec)
        if spec.timer is not None:
            out[spec.unit.removesuffix(".service") + ".timer"] = timer(spec)
    out[GATEWAY] = gateway()
    return out


def can_be_enabled(config: Path = CONFIG) -> list[str]:
    """The units with an install section: the recorder, the shadow runner and the timers.

    The slice, the gateway and the trader have none. The two timers of the learning loop and the report's
    (`BY_HAND`) have one, and are the owner's to enable by hand: the install script refuses them.
    """
    return sorted(name for name, text in render_all(config).items() if "\n[Install]\n" in text)


def diff(rendered: dict[str, str], unit_dir: Path) -> list[str]:
    out = []
    for name, text in sorted(rendered.items()):
        path = unit_dir / name
        if not path.is_file():
            out.append(f"{name}: not installed")
        elif path.read_text(encoding="utf-8") != text:
            out.append(f"{name}: differs from the code")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.ops.units", description="Render the systemd units.")
    parser.add_argument("--config", type=Path, default=CONFIG, help="where the weekend jobs' times are set")
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render", help="write every unit file into a directory")
    render.add_argument("--out", type=Path, required=True)
    compare = commands.add_parser("diff", help="say which installed units differ from the code; exit 1 if any do")
    compare.add_argument("--unit-dir", type=Path, default=Path(host.UNIT_DIR))
    commands.add_parser("list", help="the units the owner may enable, one a line")
    args = parser.parse_args(argv)
    try:
        units = render_all(args.config)
    except UnitsError as exc:
        print(f"units: {exc}", file=sys.stderr)
        return REFUSED
    if args.command == "list":
        print("\n".join(name for name, text in sorted(units.items()) if "\n[Install]\n" in text))
        return 0
    if args.command == "render":
        args.out.mkdir(parents=True, exist_ok=True)
        for name, text in units.items():
            (args.out / name).write_text(text, encoding="utf-8")
        print(f"units: rendered {len(units)} files into {args.out}")
        return 0
    changes = diff(units, args.unit_dir)
    print("\n".join(changes) if changes else "units: the installed units match the code")
    return 1 if changes else 0


if __name__ == "__main__":
    sys.exit(main())
