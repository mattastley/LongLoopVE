"""Bounded agent retrieval of channels and tables; originals remain untouched."""

from __future__ import annotations

import csv

from .common import number, read_json


def select_channels(channels: list[dict], selectors: list[str] | None) -> list[dict]:
    if not selectors:
        return channels
    selected = []
    for selector in selectors:
        hits = [
            c
            for c in channels
            if selector in (c["key"], c["source_name"], c["label"], str(c["channel_id"]))
        ]
        if len(hits) != 1:
            raise ValueError(
                f"Channel selector {selector!r} matches {len(hits)} channels; "
                "use the unique cN key."
            )
        if hits[0] not in selected:
            selected.append(hits[0])
    return selected


def log_rows(catalog, manifest: dict, channels: list[dict], start=None, end=None, limit=None):
    if "log" not in manifest:
        raise ValueError("This record has no decoded log.")
    if start is not None and end is not None and end < start:
        raise ValueError("End time must be at least start time.")
    if (start is not None or end is not None) and manifest["log"]["timing"][
        "elapsed_source"
    ] == "unknown":
        raise ValueError("Time filtering unavailable: sampling time is unknown.")
    emitted = 0
    keys = ["sample_index", "elapsed_seconds"] + [c["key"] for c in channels]
    with catalog.path(manifest["log"]["csv"]).open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            elapsed = number(row["elapsed_seconds"])
            if start is not None and (elapsed is None or elapsed < start):
                continue
            if end is not None and (elapsed is None or elapsed > end):
                continue
            if limit is not None and emitted >= limit:
                return
            yield {key: row[key] for key in keys}
            emitted += 1


def tune_tables(catalog, manifest: dict, name: str | None = None, tune_id: str | None = None):
    ids = manifest["tune_ids"]
    if tune_id:
        if tune_id not in ids:
            raise ValueError("Tune is not associated with this recording.")
        ids = [tune_id]
    result = []
    for identity in ids:
        tune = read_json(catalog.path(f"tunes/{identity}/tune.json"))
        for table in tune["tables"]:
            if name is None or table["name"] == name:
                result.append({"tune_id": identity, **table})
    if name is not None and not result:
        raise ValueError(f"No table named {name!r}.")
    return result
