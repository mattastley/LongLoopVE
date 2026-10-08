"""Persistent calculator commands; prompts stay on stderr and stdout stays JSON."""

import json
import sys
from pathlib import Path

from longloopve.analysis import settings
from longloopve.ingest import IngestionError
from longloopve.library import Library, identity, read_json
from longloopve.tune import inspect_tune


def add_commands(commands):
    inspect = commands.add_parser(
        "tune-inspect", help="Inspect tune tables before defining a mapping"
    )
    inspect.add_argument("source", type=Path)
    create = commands.add_parser(
        "car-add", help="Register Name/Car, tune layout, and channel profile"
    )
    create.add_argument("--name", required=True)
    create.add_argument("--identifier", required=True)
    create.add_argument("--nickname")
    create.add_argument("--tune", type=Path, required=True)
    create.add_argument("--mapping", type=Path, required=True)
    create.add_argument("--profile", type=Path, required=True)
    commands.add_parser("cars", help="List cars, identities, and calibration history")
    update = commands.add_parser(
        "tune-add", help="Supply a latest base tune with explicit VE impact"
    )
    update.add_argument("source", type=Path)
    update.add_argument("--car", required=True)
    update.add_argument("--impact", choices=("preserve", "reset"), required=True)
    log = commands.add_parser("log-add", help="Import an XRK into a car calibration")
    log.add_argument("source", type=Path, nargs="+", help="One or more XRK/XRZ files")
    log.add_argument("--car")
    log.add_argument("--epoch")
    log.add_argument(
        "--recording-tune", help="Known tune hash, only when recording-time tune is known"
    )
    log.add_argument("--timeout", type=float, default=120)
    assign = commands.add_parser("log-assign", help="Explicitly move a log to a car/calibration")
    assign.add_argument("log", help="Log SHA256")
    assign.add_argument("--car", required=True)
    assign.add_argument("--epoch")
    assign.add_argument("--recording-tune")
    for command in ("calculate", "export"):
        sub = commands.add_parser(command)
        sub.add_argument("--car", required=True)
        sub.add_argument("--epoch")
        sub.add_argument("--settings", type=Path, help="JSON filter/freshness/threshold overrides")
        sub.add_argument("--min-observations", type=int)
        if command == "export":
            sub.add_argument("--output", type=Path, required=True, help="New artifact directory")
        else:
            sub.add_argument("--output", type=Path, help="Optional new calculation JSON file")


def _prompt(text):
    print(text, file=sys.stderr, end=" ", flush=True)
    try:
        return input().strip()
    except EOFError as exc:
        raise ValueError("No response; use explicit CLI options") from exc


def resolve_car(metadata, index):
    if not sys.stdin.isatty():
        raise ValueError("No Name/Car match; specify --car or register a car with car-add")
    print("No unique registered car matches this XRK.", file=sys.stderr)
    print(json.dumps(metadata, indent=2), file=sys.stderr)
    for car in index["cars"].values():
        print(f"{car['id']}: {car['nickname']} ({car['identities']})", file=sys.stderr)
    selected = _prompt("Enter a car ID to confirm its assignment, or 'new' to register a new car:")
    if selected != "new":
        if selected not in index["cars"]:
            raise ValueError("Unknown car ID")
        return selected
    profile = read_json(_prompt("Channel-profile JSON path:"))
    name, identifier = identity(metadata, profile)
    return {
        "name": name or _prompt("XRK Name:"),
        "identifier": identifier or _prompt("XRK Car identifier:"),
        "nickname": _prompt("Car nickname:"),
        "tune": Path(_prompt("Initial tune path:")),
        "mapping": read_json(_prompt("Tune-layout JSON path:")),
        "profile": profile,
    }


def dispatch(args):
    library = Library(args.library)
    if args.command == "tune-inspect":
        return inspect_tune(args.source)
    if args.command == "car-add":
        return library.create(
            args.name,
            args.identifier,
            args.tune,
            read_json(args.mapping),
            read_json(args.profile),
            args.nickname,
        )
    if args.command == "cars":
        return list(library.load()["cars"].values())
    if args.command == "tune-add":
        return library.add_tune(args.car, args.source, args.impact)
    if args.command == "log-add":

        def import_one(source):
            return library.import_log(
                source,
                args.car,
                args.epoch,
                args.recording_tune,
                resolver=resolve_car,
                timeout=args.timeout,
            )

        if len(args.source) == 1:
            return import_one(args.source[0])
        files, errors = [], []
        for source in args.source:
            try:
                files.append(import_one(source))
            except (IngestionError, OSError, ValueError, KeyError) as exc:
                errors.append({"source": str(source), "error": str(exc)})
        return {"status": "partial" if errors else "complete", "files": files, "errors": errors}
    if args.command == "log-assign":
        return library.assign_log(args.log, args.car, args.epoch, args.recording_tune)
    overrides = read_json(args.settings) if args.settings else {}
    if args.min_observations is not None:
        overrides["min_observations"] = args.min_observations
    overrides = settings(overrides)
    if args.command == "export":
        return library.export(args.car, args.output, overrides, args.epoch)
    result = library.calculate(args.car, overrides, args.epoch)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    return result
