"""Private decoder process. Invoked by the supervisor, never directly by a skill."""

import json
import math
import sys
from datetime import date, datetime
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from libxrk import aim_xrk

from longloopve import __version__
from longloopve.quality import summarize_channel


def json_safe(value):
    """Keep metadata JSON-compatible, explicitly representing binary data."""
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"encoding": "hex", "data": value.hex()}
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"Unsupported decoder metadata type: {type(value).__name__}")


def extract(source: Path, destination: Path, mode: str, required: list[str]) -> dict:
    log = aim_xrk(str(source))
    if not log.channels or not any(t.num_rows for t in log.channels.values()):
        raise ValueError("Decoder returned no channel samples; this is not a successful ingestion")
    missing = sorted(set(required) - set(log.channels))
    if missing:
        raise ValueError(f"Required channels missing: {', '.join(missing)}")
    empty = sorted(name for name in required if not log.channels[name].num_rows)
    if empty:
        raise ValueError(f"Required channels have no samples: {', '.join(empty)}")
    channels = []
    if mode == "ingest":
        (destination / "channels").mkdir()
    for index, name in enumerate(sorted(log.channels)):
        table = log.channels[name]
        summary = summarize_channel(name, table)
        if mode == "ingest":
            # Source names never become paths. Keep original Arrow field metadata.
            relative_path = f"channels/{index:05d}.parquet"
            pq.write_table(table, destination / relative_path, compression="zstd")
            summary["file"] = relative_path
        channels.append(summary)
    manifest = {
        "schema_version": 1,
        "longloopve_version": __version__,
        "decoder": {"name": "libxrk", "version": version("libxrk"), "backend": "cython"},
        "timecode_unit": "ms",
        "processing": {"resampled": False, "units_converted": False},
        "device_validation": "unverified_against_race_studio",
        "metadata": json_safe(log.metadata),
        "channels": channels,
        "required_channels": sorted(set(required)),
        "warnings": [
            "Decoded data has not been verified against Race Studio for this device/firmware.",
            "Channel completeness cannot be inferred from decoded channels alone.",
        ],
    }
    return manifest


def main():
    source, destination, mode, *required = sys.argv[1:]
    manifest = extract(Path(source), Path(destination), mode, required)
    (Path(destination) / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
