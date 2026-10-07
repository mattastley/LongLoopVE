"""Small shared helpers for explicit errors, encoding, and atomic persistence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 512 * 1024 * 1024


class InputError(ValueError):
    def __init__(self, message: str, status: str = "corrupt"):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_text(data: bytes) -> tuple[str, str]:
    # Some MTune files declare UTF-16 but contain UTF-8. Trust the BOM/bytes.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data[:4] in (b"<\x00?\x00", b"<\x00M\x00"):
        return data.decode("utf-16-le"), "utf-16-le"
    if data[:4] in (b"\x00<\x00?", b"\x00<\x00M"):
        return data.decode("utf-16-be"), "utf-16-be"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "cp1252"


def number(value: str | float | int | None) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: object) -> None:
    atomic_bytes(
        path,
        (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"),
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def xml_text(data: bytes) -> tuple[str, str, list[str]]:
    text, encoding = decode_text(data)
    declaration = re.match(r"\s*<\?xml[^?]*\?>", text)
    warnings = []
    if declaration:
        declared = re.search(r'encoding=[\'"]([^\'"]+)', declaration[0])
        if declared and declared[1].lower() != encoding.removesuffix("-sig"):
            warnings.append(f"XML declares {declared[1]}; actual encoding is {encoding}.")
        text = text[declaration.end() :]
    return text, encoding, warnings
