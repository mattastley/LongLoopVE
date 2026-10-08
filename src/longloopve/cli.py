"""Command line interface; JSON reports go to stdout and errors to stderr."""

import argparse
import json
import math
import sys
from pathlib import Path

from longloopve import __version__
from longloopve.ingest import IngestionError, run


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return seconds


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and ingest AIM logs for LongLoopVE")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--library", type=Path, default=Path("data/longloopve"))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "ingest"):
        subparser = commands.add_parser(command)
        subparser.add_argument("source", type=Path)
        subparser.add_argument("--timeout", type=positive_seconds, default=120)
        subparser.add_argument("--require-channel", action="append", default=[])
        subparser.add_argument(
            "--profile", type=Path, help="Engine-channel profile JSON (exact names and units)"
        )
        if command == "ingest":
            subparser.add_argument("--output", type=Path, required=True)
    from longloopve.workflow_cli import add_commands, dispatch

    add_commands(commands)
    args = parser.parse_args()
    try:
        if args.command in {"inspect", "ingest"}:
            report = run(
                args.source,
                output=getattr(args, "output", None),
                required=args.require_channel,
                profile=args.profile,
                timeout=args.timeout,
            )
        else:
            report = dispatch(args)
    except (IngestionError, OSError, ValueError, KeyError) as exc:
        print(f"longloopve: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, allow_nan=False))
    return 2 if isinstance(report, dict) and report.get("status") == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())
