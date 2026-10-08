"""Explicit tune layouts and text-only table edits using the existing ingest parser."""

import copy
import re
from pathlib import Path

import numpy as np

from longloopve._maxxecu.common import decode_text, digest
from longloopve._maxxecu.tunes import parse_tune


def inspect_tune(path: Path) -> dict:
    parsed = parse_tune(path.read_bytes())
    return {key: parsed[key] for key in ("id", "file_info", "status", "warnings", "tables")}


def _axis(table, name, scale):
    matches = [axis for axis in table["axes"] if axis["name"] == name]
    if len(matches) != 1:
        raise ValueError(f"Expected one axis {name!r} in {table['name']!r}")
    axis = matches[0]
    values = np.asarray(axis["values"], dtype=float) * scale
    if (
        len(values) < 2
        or not np.all(np.isfinite(values))
        or not np.all(np.diff(values) > 0)
        or int(axis["attributes"].get("AxisCellCount", 0)) != len(values)
    ):
        raise ValueError(f"Axis {name!r} must have at least two finite ascending breakpoints")
    return values


def read_tables(data: bytes, mapping: dict, *, allow_negative=False) -> dict:
    parsed = parse_tune(data)
    if parsed["status"] != "complete":
        raise ValueError(f"Tune is {parsed['status']}: {parsed['warnings']}")
    if mapping.get("interpolation") != "bilinear_clamped":
        raise ValueError("Only explicitly mapped bilinear_clamped interpolation is supported")
    if not mapping.get("layout_evidence"):
        raise ValueError("Mapping requires layout_evidence describing units/order/interpolation")
    if set(mapping.get("banks", {})) != {"A", "B"}:
        raise ValueError("Tune mapping must define banks A and B")
    result = {
        "sha256": digest(data),
        "file_info": parsed["file_info"],
        "warnings": parsed["warnings"],
    }
    names = []
    for bank, spec in mapping["banks"].items():
        names.append(spec["table"])
        tables = [t for t in parsed["tables"] if t["name"] == spec["table"]]
        if len(tables) != 1:
            raise ValueError(f"Expected one tune table named {spec['table']!r}")
        table = tables[0]
        if spec["rpm_axis"] == spec["etps_axis"]:
            raise ValueError("RPM and ETPS must map to distinct axes")
        scales = [float(spec[key]) for key in ("rpm_scale", "etps_scale", "value_scale")]
        if not all(np.isfinite(s) and s > 0 for s in scales):
            raise ValueError("Tune scales must be finite and positive")
        rpm = _axis(table, spec["rpm_axis"], scales[0])
        etps = _axis(table, spec["etps_axis"], scales[1])
        if len(table["axes"]) != 2 or len(table["pages"]) != 1:
            raise ValueError("Only two-axis, single-page VE tables are supported")
        page = table["pages"][0]
        order = spec["order"]
        if order not in {"rpm_fast", "etps_fast"}:
            raise ValueError("Table order must explicitly be rpm_fast or etps_fast")
        shape = (len(etps), len(rpm)) if order == "rpm_fast" else (len(rpm), len(etps))
        if (int(page["attributes"]["YSize"]), int(page["attributes"]["XSize"])) != shape:
            raise ValueError("Mapped table order does not match serialized XSize/YSize")
        values = np.asarray(page["values"], dtype=float).reshape(shape) * scales[2]
        if order == "etps_fast":
            values = values.T
        if not np.all(np.isfinite(values)) or (not allow_negative and np.any(values < 0)):
            raise ValueError("Base VE must be finite and nonnegative")
        result[bank] = {"rpm": rpm.tolist(), "etps": etps.tolist(), "values": values.tolist()}
    if len(set(names)) != 2:
        raise ValueError("Banks must map to two distinct VE tables")
    if result["A"]["rpm"] != result["B"]["rpm"] or result["A"]["etps"] != result["B"]["etps"]:
        raise ValueError("Banks must share RPM/ETPS breakpoints")
    return result


def write_tables(data: bytes, mapping: dict, updates: dict) -> bytes:
    """Replace only changed cell text; retain all unrelated source text and encoding."""
    original = read_tables(data, mapping)
    before = parse_tune(data)
    text, encoding = decode_text(data)
    table_spans = list(re.finditer(r"<DynamicTable\b[^>]*>.*?</DynamicTable\s*>", text, re.S))
    if len(table_spans) != len(before["tables"]):
        raise ValueError("Unsupported XML table nesting; cannot safely edit tune")
    edits = []
    for bank, spec in mapping["banks"].items():
        index = next(i for i, t in enumerate(before["tables"]) if t["name"] == spec["table"])
        span = table_spans[index]
        pages = list(
            re.finditer(r"<DynamicTableData\b[^>]*>([^<]*)</DynamicTableData\s*>", span[0])
        )
        if len(pages) != 1:
            raise ValueError("Unsupported XML data representation; cannot safely edit tune")
        page = pages[0]
        values = np.asarray(updates[bank], dtype=float)
        base = np.asarray(original[bank]["values"])
        if values.shape != base.shape or not np.all(np.isfinite(values)):
            raise ValueError("Export VE dimensions/values are invalid")
        if spec["order"] == "etps_fast":
            values, base = values.T, base.T
        tokens = list(re.finditer(r"[^,\r\n]+", page[1]))
        if len(tokens) != values.size:
            raise ValueError("Unexpected cell delimiters in tune")
        offset = span.start() + page.start(1)
        for token, new, old in zip(tokens, values.ravel(), base.ravel(), strict=True):
            if new != old:
                replacement = re.sub(
                    r"\S.*\S|\S", format(new / float(spec["value_scale"]), ".15g"), token[0]
                )
                edits.append((offset + token.start(), offset + token.end(), replacement))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    output = text.encode(encoding)
    # A reconstructed negative value is preserved for review, not silently capped.
    # Input base tunes still require nonnegative values; the solver reports a warning.
    reread = read_tables(output, mapping, allow_negative=True)
    for bank in ("A", "B"):
        if not np.allclose(reread[bank]["values"], updates[bank], rtol=1e-12, atol=1e-12):
            raise ValueError("Tune readback did not reproduce requested VE values")
    # Check the complete parser-returned XML tree, including unknown settings/attributes.
    after = copy.deepcopy(parse_tune(output)["xml"])
    original_tree = before["xml"]
    for new, old in zip(after["children"], original_tree["children"], strict=True):
        if old["tag"] == "DynamicTable" and old["attributes"].get("name") in {
            s["table"] for s in mapping["banks"].values()
        }:
            for new_child, old_child in zip(new["children"], old["children"], strict=True):
                if old_child["tag"] == "DynamicTableData":
                    new_child["text"] = old_child["text"]
    if after != original_tree:
        raise ValueError("Tune export changed unrelated XML content")
    return output
