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
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "ingest"):
        subparser = commands.add_parser(command)
        subparser.add_argument("source", type=Path)
        subparser.add_argument("--timeout", type=positive_seconds, default=120)
        subparser.add_argument("--require-channel", action="append", default=[])
        if command == "ingest":
            subparser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(
            args.source,
            output=getattr(args, "output", None),
            required=args.require_channel,
            timeout=args.timeout,
        )
    except IngestionError as exc:
        print(f"longloopve: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
