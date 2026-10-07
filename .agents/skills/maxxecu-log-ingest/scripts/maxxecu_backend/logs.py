"""Text and LZ4 log decoders with explicit source and timing provenance."""

from __future__ import annotations

import array
import csv
import io
import math
import re
import struct
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import lz4.frame

from .common import MAX_BYTES, InputError, decode_text, number

LZ4_MAGIC = b"\x04\x22\x4d\x18"


@dataclass
class Log:
    channels: list[dict]
    rows: Iterable[list[str]]
    format: str
    warnings: list[str]
    expected_rows: int | None = None


def channel(name: str, ordinal: int, scale: float | None = None) -> dict:
    match = re.search(r"\[(\d+)\]\s*$", name)
    return {
        "key": f"c{ordinal}",
        "source_name": name,
        "channel_id": int(match[1]) if match else None,
        "label": name[: match.start()].strip() if match else name,
        "units": None,
        "units_source": "unknown",
        "binary_scale_float32": scale,
    }


def read_exact(stream, count: int) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise InputError("Truncated binary log.")
    return data


def binary_string(stream) -> str:
    length = 0
    for shift in range(0, 35, 7):
        value = read_exact(stream, 1)[0]
        length |= (value & 127) << shift
        if not value & 128:
            if length > 65536:
                raise InputError("Binary channel name exceeds size limit.")
            return read_exact(stream, length).decode("utf-8")
    raise InputError("Invalid 7-bit string length.")


def parse_binary(data: bytes) -> Log:
    try:
        with lz4.frame.open(io.BytesIO(data)) as frame:
            payload = frame.read(MAX_BYTES + 1)
    except (RuntimeError, EOFError, OSError) as error:
        raise InputError(f"Invalid LZ4 frame: {error}") from error
    if len(payload) > MAX_BYTES:
        raise InputError("Decompressed log exceeds 512 MiB limit.", "unsupported")
    stream = io.BytesIO(payload)
    columns, rows = struct.unpack("<ii", read_exact(stream, 8))
    if not 1 <= columns <= 16384 or rows < 0 or columns * rows * 2 > MAX_BYTES:
        raise InputError("Invalid binary dimensions.")
    channels = []
    for index in range(columns):
        name = binary_string(stream)
        scale = struct.unpack("<f", read_exact(stream, 4))[0]
        if not math.isfinite(scale) or scale <= 0:
            raise InputError("Invalid binary channel scale.")
        channels.append(channel(name, index, scale))
    sample_data = stream.read()
    if len(sample_data) != columns * rows * 2:
        raise InputError(
            f"Binary payload length mismatch: expected {columns * rows * 2}, "
            f"got {len(sample_data)}."
        )
    samples = array.array("h")
    samples.frombytes(sample_data)
    if sys.byteorder != "little":
        samples.byteswap()
    # Float32 scale encodings represent decimal resolutions (e.g. 0.1).
    # Recover at float32 precision and retain the original float32 separately.
    scales = [float(format(c["binary_scale_float32"], ".7g")) for c in channels]

    def iter_rows():
        for row in range(rows):
            yield [
                format(samples[col * rows + row] * scales[col], ".12g") for col in range(columns)
            ]

    return Log(channels, iter_rows(), "lz4-column-int16", [], rows)


def parse_text(data: bytes) -> Log:
    text, encoding = decode_text(data)
    if "\x00" in text:
        raise InputError("Unrecognized binary log.", "unsupported")
    first = text.split("\n", 1)[0]
    delimiter = "\t" if "\t" in first else "," if "," in first else None
    if delimiter is None:
        raise InputError("No recognizable delimited log header.", "unsupported")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    names = next(reader)
    if not names or not any(re.search(r"\[\d+\]", name) for name in names):
        raise InputError("No MaxxECU channel identifiers found in log header.", "unsupported")
    # MTune exports a terminal delimiter. Only remove consistently empty final fields.
    trailing_empty = bool(names and names[-1] == "")
    if trailing_empty:
        names.pop()
    if len(names) > 16384:
        raise InputError("Too many channels.", "unsupported")

    def iter_rows():
        for line, row in enumerate(reader, 2):
            if not row:
                continue
            if trailing_empty and len(row) == len(names) + 1 and row[-1] == "":
                row.pop()
            if len(row) != len(names):
                raise InputError(f"Row {line} has {len(row)} values; expected {len(names)}.")
            yield row

    return Log(
        [channel(name, i) for i, name in enumerate(names)],
        iter_rows(),
        f"{'tsv' if delimiter == chr(9) else 'csv'}:{encoding}",
        [],
    )


def parse_log(data: bytes) -> Log:
    return parse_binary(data) if data.startswith(LZ4_MAGIC) else parse_text(data)


def metadata(data: bytes) -> dict:
    text, _ = decode_text(data)
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def time_metadata(meta: dict) -> dict:
    stamp = number(meta.get("CreatedTimestamp"))
    created = None
    if stamp is not None and 946684800 <= stamp <= datetime.now(UTC).timestamp() + 86400:
        created = datetime.fromtimestamp(stamp, UTC).isoformat()
    interval = number(meta.get("LogRate"))
    return {
        "source_metadata": meta,
        "created_at": created,
        "created_at_status": "metadata" if created else "unknown",
        "sample_interval_seconds": interval if interval and 0 < interval <= 60 else None,
    }


def write_log(log: Log, destination, meta: dict) -> dict:
    """Stream canonical CSV and facts. Invalid rows fail the enclosing asset."""
    timing = time_metadata(meta)
    interval = timing["sample_interval_seconds"]
    # Channel 498 is an inter-sample interval in milliseconds, not an absolute
    # timestamp. Its source values are retained; elapsed time starts at sample 0.
    clock = next(
        (
            i
            for i, c in enumerate(log.channels)
            if c["channel_id"] == 498 and c["label"].casefold() == "log timestamp"
        ),
        None,
    )
    timing["elapsed_source"] = (
        "accumulated_logged_intervals_ms"
        if clock is not None
        else "derived_sample_interval"
        if interval
        else "unknown"
    )
    if clock is not None:
        log.channels[clock].update(units="ms", units_source="verified_channel_498_definition")
    stats = [
        {"finite_count": 0, "missing_or_nonnumeric_count": 0, "min": None, "max": None}
        for _ in log.channels
    ]
    warnings = list(log.warnings)
    if meta.get("CreatedTimestamp") and not timing["created_at"]:
        warnings.append(
            "Recording date unknown: source CreatedTimestamp is invalid or a placeholder."
        )
    accumulated_time = 0.0
    last_time = None
    clock_valid = True
    discontinuities = 0
    count = 0
    with open(destination, "w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["sample_index", "elapsed_seconds"] + [c["key"] for c in log.channels])
        for count, row in enumerate(log.rows, 1):
            elapsed = None
            if clock is not None:
                current = number(row[clock])
                if current is None or current < 0:
                    discontinuities += 1
                    clock_valid = False
                if count > 1 and clock_valid:
                    accumulated_time += current / 1000
                elapsed = accumulated_time if clock_valid else None
            elif interval:
                elapsed = (count - 1) * interval
            if elapsed is not None:
                last_time = elapsed
            writer.writerow([count - 1, "" if elapsed is None else format(elapsed, ".12g")] + row)
            for stat, value in zip(stats, row):
                numeric = number(value)
                if numeric is None:
                    stat["missing_or_nonnumeric_count"] += 1
                else:
                    stat["finite_count"] += 1
                    stat["min"] = numeric if stat["min"] is None else min(stat["min"], numeric)
                    stat["max"] = numeric if stat["max"] is None else max(stat["max"], numeric)
    timing["timestamp_discontinuities"] = discontinuities
    timing["duration_seconds"] = last_time if not discontinuities else None
    status = "complete"
    for field, actual in (("Rows", count), ("Columns", len(log.channels))):
        declared = number(meta.get(field))
        if declared and declared != actual:
            warnings.append(
                f"Metadata {field}={meta[field]} disagrees with decoded count {actual}."
            )
            status = "partial"
    if discontinuities:
        warnings.append(
            "Invalid logged sample interval; subsequent elapsed values and duration unknown."
        )
    if not count:
        warnings.append("Log contains no samples.")
    for c, stat in zip(log.channels, stats):
        c["statistics"] = stat
    return {
        "format": log.format,
        "row_count": count,
        "channels": log.channels,
        "timing": timing,
        "warnings": warnings,
        "status": status,
    }
