"""Content-based file/package discovery. Archive names never become disk paths."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import PurePosixPath

from .common import MAX_BYTES, InputError, decode_text, digest
from .logs import LZ4_MAGIC, metadata


def kind(data: bytes) -> str:
    if data.startswith(b"PK"):
        return "archive"
    if data.startswith(LZ4_MAGIC):
        return "log"
    try:
        text, _ = decode_text(data[:8192])
    except UnicodeError:
        return "unsupported"
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return "tune"
    if "LogRate=" in text or "CreatedTimestamp=" in text:
        return "metadata"
    if re.search(r"\[\d+\]", text.split("\n", 1)[0]) and ("\t" in text or "," in text):
        return "log"
    return "unsupported"


def safe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    return (
        bool(parts)
        and not normalized.startswith("/")
        and not any(p in ("..", ".") or ":" in p for p in parts)
    )


def unpack(data: bytes, name: str) -> dict:
    if len(data) > MAX_BYTES:
        raise InputError("Input exceeds 512 MiB limit.", "unsupported")
    detected = kind(data)
    if detected != "archive":
        return {
            "kind": detected,
            "members": [
                {"name": name, "data": data, "kind": detected, "hash": digest(data), "error": None}
            ],
            "warnings": [],
        }
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise InputError(f"Damaged ZIP archive: {error}") from error
    members = []
    with archive:
        infos = archive.infolist()
        if len(infos) > 4096 or sum(i.file_size for i in infos) > 2 * MAX_BYTES:
            raise InputError("Archive exceeds member/count limits.", "unsupported")
        if len({i.filename for i in infos}) != len(infos):
            raise InputError("Archive contains duplicate member names.")
        for info in infos:
            if not safe_member(info.filename):
                raise InputError(f"Unsafe archive member path: {info.filename}")
            if info.is_dir():
                continue
            if info.file_size > MAX_BYTES:
                raise InputError("Archive member exceeds 512 MiB limit.", "unsupported")
            try:
                if info.flag_bits & 1:
                    raise InputError("Encrypted archive member.", "protected")
                with archive.open(info) as stream:
                    content = stream.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise InputError("Archive member too large.", "unsupported")
                members.append(
                    {
                        "name": info.filename,
                        "data": content,
                        "kind": kind(content),
                        "hash": digest(content),
                        "error": None,
                    }
                )
            except (InputError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
                members.append(
                    {
                        "name": info.filename,
                        "data": None,
                        "kind": "unavailable",
                        "hash": None,
                        "error": str(error),
                        "status": getattr(error, "status", "corrupt"),
                    }
                )
    return {"kind": "archive", "members": members, "warnings": []}


def package_metadata(package: dict) -> tuple[dict, list[str]]:
    entries = [m for m in package["members"] if m["kind"] == "metadata"]
    if len(entries) == 1:
        return metadata(entries[0]["data"]), []
    return {}, ["Multiple metadata members; timing association is ambiguous."] if entries else []
