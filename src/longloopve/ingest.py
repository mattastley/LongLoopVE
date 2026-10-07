"""Supervise decoding and publish complete artifacts without overwriting outputs."""

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from longloopve.profiles import load_profile


class IngestionError(Exception):
    """An actionable input, decoder, or output failure."""


def validate_source(source: Path) -> Path:
    source = source.resolve()
    if source.suffix.lower() not in {".xrk", ".xrz"}:
        raise IngestionError("Expected an AIM .xrk or .xrz file")
    if not source.is_file():
        raise IngestionError(f"Log file does not exist: {source}")
    if source.stat().st_size == 0:
        raise IngestionError("Log file is empty")
    return source


def run(
    source: Path,
    *,
    output: Path | None = None,
    required: list[str] | None = None,
    profile: Path | None = None,
    timeout: float = 120,
) -> dict:
    source = validate_source(source)
    if not math.isfinite(timeout) or timeout <= 0:
        raise IngestionError("Timeout must be a finite positive number")
    mode = "ingest" if output is not None else "inspect"
    target = output.absolute() if output is not None else None
    reserved = False
    published = False
    try:
        engine_profile = load_profile(profile) if profile is not None else None
        required_channels = sorted(
            set(required or []) | set(engine_profile.required_channels if engine_profile else [])
        )
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Reserve exclusively, including when two processes choose the same target.
            target.mkdir(exist_ok=False)
            reserved = True
        with tempfile.TemporaryDirectory(
            prefix=".longloopve-", dir=target.parent if target else None
        ) as temporary:
            stage = Path(temporary) / "result"
            stage.mkdir()
            snapshot = Path(temporary) / f"input{source.suffix.lower()}"
            before = source.stat()
            shutil.copyfile(source, snapshot)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise IngestionError(
                    "Source changed during copying; retry after download completes"
                )
            with snapshot.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            diagnostic_path = stage / "decoder.log"
            environment = os.environ.copy()
            environment["LIBXRK_BACKEND"] = "cython"
            try:
                with diagnostic_path.open("wb") as diagnostics:
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "longloopve.worker",
                            str(snapshot),
                            str(stage),
                            mode,
                            *required_channels,
                        ],
                        stdout=diagnostics,
                        stderr=subprocess.STDOUT,
                        env=environment,
                        timeout=timeout,
                        check=False,
                    )
            except subprocess.TimeoutExpired as exc:
                raise IngestionError(f"Decoder exceeded {timeout:g} seconds") from exc
            with diagnostic_path.open("rb") as diagnostics:
                diagnostics.seek(max(0, diagnostic_path.stat().st_size - 2000))
                tail = diagnostics.read().decode("utf-8", errors="replace").strip()
            if result.returncode:
                raise IngestionError(
                    f"Decoder failed (exit {result.returncode}). "
                    f"{tail or 'No diagnostics emitted.'}"
                )
            manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
            if engine_profile is not None:
                manifest["engine_profile"] = engine_profile.apply(manifest)
            manifest["source"] = {
                "filename": source.name,
                "sha256": digest,
                "size_bytes": snapshot.stat().st_size,
            }
            if diagnostic_path.stat().st_size:
                manifest["warnings"].append(
                    "Decoder emitted diagnostics; review decoder.log before analysis."
                    if target
                    else "Decoder emitted diagnostics; ingest to retain decoder.log for review."
                )
            if target:
                manifest["diagnostics_file"] = "decoder.log"
            (stage / "manifest.json").write_text(
                json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8"
            )
            if target:
                # Replace only our own reserved empty directory, on the same filesystem.
                os.replace(stage, target)
                published = True
            return manifest
    except (OSError, ValueError) as exc:
        raise IngestionError(str(exc)) from exc
    finally:
        if reserved and not published:
            target.rmdir()
