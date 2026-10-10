"""`python -m hftlab.registry lint [--root DIR] [--base REV]`: lint the research registry.

With --base, experiments are also checked against that commit: none that exists there may differ now.
Exit codes: 0 clean, 1 findings, 2 the lint could not run.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

from hftlab.registry import records
from hftlab.registry.trials import trial_history


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.registry", description="Research registry tools.")
    commands = parser.add_subparsers(dest="command", required=True)
    lint = commands.add_parser("lint", help="check ids, locations, uniqueness, references and append-only experiments")
    lint.add_argument("--root", type=Path, default=Path("."), help="repository root (default: current directory)")
    lint.add_argument("--base", help="a commit to compare experiments against (for example origin/main)")
    args = parser.parse_args(argv)

    root: Path = args.root
    try:
        head = records.worktree_state(root)
        base = records.commit_state(root, args.base) if args.base else None
        registry = root / "config" / "trial_registry.yaml"
        trials = trial_history(registry) if registry.exists() else []
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"registry lint: cannot run: {exc}", file=sys.stderr)
        return 2
    findings = records.lint(head, base=base, texts=records.record_texts(root, head), trials=trials)
    for finding in findings:
        print(f"registry lint: {finding}", file=sys.stderr)
    if findings:
        print(f"registry lint: FAILED, {len(findings)} finding(s)", file=sys.stderr)
        return 1
    count = len(records.records(head))
    print(f"registry lint: ok ({count} records{', experiments compared with ' + args.base if args.base else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
