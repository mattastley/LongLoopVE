#!/usr/bin/env python3
"""CLI entry point. Run --help or a subcommand's --help for usage."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

from maxxecu_backend.catalog import DEFAULT_CATALOG, Catalog
from maxxecu_backend.common import InputError, atomic_json, digest, now, read_json
from maxxecu_backend.inputs import unpack
from maxxecu_backend.query import log_rows, select_channels, tune_tables
from maxxecu_backend.vehicles import PROFILE_FIELDS, assign_records, create_vehicle, merge_vehicles


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--catalog",
        type=Path,
        default=Path(os.environ.get("MAXXECU_CATALOG", str(DEFAULT_CATALOG))),
    )
    cli.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON output (default for structured responses)",
    )
    commands = cli.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser(
        "inspect", help="Read file/package signatures without modifying a catalog"
    )
    inspect.add_argument("path", type=Path)
    ingest = commands.add_parser(
        "ingest", help="Ingest files or directories, continuing after per-file errors"
    )
    ingest.add_argument("paths", nargs="+", type=Path)
    ingest.add_argument("--vehicle")
    ingest.add_argument("--tune", type=Path, help="Explicit associated standalone tune")
    ingest.add_argument(
        "--sample-interval", type=float, help="Explicit seconds/sample for exports missing timing"
    )
    ingest.add_argument(
        "--capture-timing", choices=("recording", "download", "unknown"), default="unknown"
    )
    listing = commands.add_parser("list", help="Compact catalog records or vehicles")
    listing.add_argument("--vehicles", action="store_true")
    listing.add_argument("--vehicle")
    listing.add_argument("--recording")
    listing.add_argument("--status")
    listing.add_argument("--name", help="Case-insensitive source-name substring")
    show = commands.add_parser("show", help="Show manifest, bounded rows, tune settings, or tables")
    show.add_argument("recording")
    show.add_argument("--channels", nargs="+")
    show.add_argument("--start", type=float)
    show.add_argument("--end", type=float)
    show.add_argument("--limit", type=int, default=20)
    show.add_argument("--rows", action="store_true")
    show.add_argument("--table")
    show.add_argument("--tables", action="store_true", help="List table names without values")
    show.add_argument("--settings", action="store_true")
    show.add_argument("--tune-id")
    export = commands.add_parser("export", help="Export selected rows to CSV or a table to JSON")
    export.add_argument("recording")
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--channels", nargs="+")
    export.add_argument("--start", type=float)
    export.add_argument("--end", type=float)
    export.add_argument("--table")
    export.add_argument("--tune-id")
    vehicle = commands.add_parser(
        "vehicle", help="Create/edit/merge vehicles or correct recording assignments"
    )
    actions = vehicle.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create")
    create.add_argument("--nickname")
    edit = actions.add_parser("edit")
    edit.add_argument("vehicle_id")
    for field in sorted(PROFILE_FIELDS):
        edit.add_argument("--" + field.replace("_", "-"))
    edit.add_argument("--ecu-serial")
    edit.add_argument("--from-date")
    edit.add_argument("--to-date")
    assign = actions.add_parser("assign")
    assign.add_argument("recordings", nargs="+")
    target = assign.add_mutually_exclusive_group(required=True)
    target.add_argument("--vehicle")
    target.add_argument(
        "--new", metavar="NICKNAME", help="Create a vehicle and move selected recordings to it"
    )
    merge = actions.add_parser("merge")
    merge.add_argument("source")
    merge.add_argument("target")
    return cli


def run(args):
    catalog = Catalog(args.catalog)
    if args.command == "inspect":
        data = args.path.read_bytes()
        package = unpack(data, args.path.name)
        return {
            "sha256": digest(data),
            "format": package["kind"],
            "members": [{k: v for k, v in m.items() if k != "data"} for m in package["members"]],
        }, 0
    if args.command == "ingest":
        if args.sample_interval is not None and not 0 < args.sample_interval <= 60:
            raise ValueError("Sample interval must be greater than zero and at most 60 seconds.")
        files = []
        for path in args.paths:
            if path.is_dir():
                files.extend(
                    p
                    for p in sorted(path.rglob("*"))
                    if p.is_file()
                    and p.suffix.lower()
                    in (
                        ".maxxecu-zip-log",
                        ".maxxecu-log",
                        ".maxxlog",
                        ".maxxecu-save",
                        ".csv",
                        ".tsv",
                        ".zip",
                        ".xml",
                    )
                    and not p.resolve().is_relative_to(catalog.root)
                )
            else:
                files.append(path)
        if not files:
            raise ValueError("No candidate files found.")
        if args.tune and len(files) != 1:
            raise ValueError("--tune requires a single input file.")
        output = []
        for i, path in enumerate(dict.fromkeys(files), 1):
            print(f"[{i}/{len(files)}] {path.name}", file=sys.stderr, flush=True)
            try:
                result = catalog.ingest(
                    path,
                    forced_vehicle=args.vehicle,
                    external_tune=args.tune,
                    sample_interval=args.sample_interval,
                    capture_timing=args.capture_timing,
                )
                output.append({"file": str(path), **result})
            except (OSError, InputError, ValueError) as error:
                output.append({"file": str(path), "status": "error", "error": str(error)})
        return {"files": output}, 2 if any(
            r["status"] in ("error", "partial") for r in output
        ) else 0
    index = catalog.load()
    if args.command == "list":
        if args.vehicles:
            return list(index["vehicles"].values()), 0
        return [
            r
            for r in index["records"].values()
            if (not args.vehicle or r["assignment"]["vehicle_id"] == args.vehicle)
            and (not args.recording or r["id"] == args.recording)
            and (not args.status or r["status"] == args.status)
            and (not args.name or args.name.casefold() in r["source_name"].casefold())
        ], 0
    if args.command in ("show", "export"):
        manifest = catalog.manifest(args.recording, index)
        if args.command == "show":
            if args.limit < 0:
                raise ValueError("Limit cannot be negative.")
            if args.table or args.tables:
                tables = tune_tables(catalog, manifest, args.table, args.tune_id)
                return [
                    {"name": t["name"], "tune_id": t["tune_id"]} for t in tables
                ] if args.tables else tables, 0
            if args.settings:
                ids = [args.tune_id] if args.tune_id else manifest["tune_ids"]
                if any(i not in manifest["tune_ids"] for i in ids):
                    raise ValueError("Tune not associated with recording.")
                return [
                    {
                        "tune_id": i,
                        "settings": read_json(catalog.path(index["tunes"][i]["path"]))["settings"],
                    }
                    for i in ids
                ], 0
            if args.rows or args.channels or args.start is not None or args.end is not None:
                selected = select_channels(
                    manifest.get("log", {}).get("channels", []), args.channels
                )
                return {
                    "channels": selected,
                    "timing": manifest.get("log", {}).get("timing"),
                    "rows": list(
                        log_rows(catalog, manifest, selected, args.start, args.end, args.limit)
                    ),
                }, 0
            return manifest, 0
        destination = args.output.resolve()
        if destination.is_relative_to(catalog.root):
            raise ValueError("Export outside the catalog to avoid overwriting source assets.")
        if destination.exists():
            raise ValueError("Export destination already exists; choose a new output path.")
        if (
            not args.table
            and destination.with_suffix(destination.suffix + ".metadata.json").exists()
        ):
            raise ValueError(
                "Export metadata destination already exists; choose a new output path."
            )
        if args.table:
            atomic_json(destination, tune_tables(catalog, manifest, args.table, args.tune_id))
        else:
            selected = select_channels(manifest.get("log", {}).get("channels", []), args.channels)
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=destination.parent, prefix=".pending-")
            os.close(fd)
            try:
                with open(temp, "w", newline="", encoding="utf-8") as stream:
                    keys = ["sample_index", "elapsed_seconds"] + [c["key"] for c in selected]
                    writer = csv.DictWriter(stream, fieldnames=keys)
                    writer.writeheader()
                    writer.writerows(log_rows(catalog, manifest, selected, args.start, args.end))
                os.replace(temp, destination)
            finally:
                Path(temp).unlink(missing_ok=True)
            atomic_json(
                destination.with_suffix(destination.suffix + ".metadata.json"),
                {
                    "recording_id": args.recording,
                    "channels": selected,
                    "timing": manifest["log"]["timing"],
                },
            )
        return {"output": str(destination)}, 0
    if args.command == "vehicle":
        with catalog.writer() as index:
            if args.action == "create":
                identity = create_vehicle(index, args.nickname)
            elif args.action == "edit":
                identity = args.vehicle_id
                vehicle = index["vehicles"][identity]
                changes = {
                    key: getattr(args, key)
                    for key in PROFILE_FIELDS
                    if getattr(args, key) is not None
                }
                if changes.get("vin"):
                    changes["vin"] = changes["vin"].strip().upper()
                    if (
                        not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", changes["vin"])
                        or len(set(changes["vin"])) == 1
                    ):
                        raise ValueError(
                            "VIN must be a usable 17-character VIN, or empty to clear it."
                        )
                for stamp in (args.from_date, args.to_date):
                    if stamp:
                        date.fromisoformat(stamp)
                if args.from_date and args.to_date and args.from_date > args.to_date:
                    raise ValueError("ECU assignment end date precedes start date.")
                if (args.from_date or args.to_date) and not args.ecu_serial:
                    raise ValueError("Assignment dates require --ecu-serial.")
                vehicle["history"].append(
                    {
                        "at": now(),
                        "action": "profile_edit",
                        "previous": dict(vehicle["profile"]),
                        "changes": changes,
                    }
                )
                vehicle["profile"].update(changes)
                vehicle["provisional"] = False
                if args.ecu_serial:
                    vehicle["ecu_assignments"].append(
                        {
                            "ecu_serial": args.ecu_serial,
                            "source": "user",
                            "observed_from": args.from_date,
                            "observed_to": args.to_date,
                        }
                    )
            elif args.action == "assign":
                identity = create_vehicle(index, args.new) if args.new else args.vehicle
                assign_records(index, args.recordings, identity)
            else:
                merge_vehicles(index, args.source, args.target)
                identity = args.target
            return index["vehicles"][identity], 0
    raise ValueError("Unknown command")


def main():
    try:
        output, code = run(parser().parse_args())
    except (OSError, InputError, ValueError, KeyError) as error:
        output, code = {"error": str(error), "status": getattr(error, "status", "error")}, 1
    print(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
