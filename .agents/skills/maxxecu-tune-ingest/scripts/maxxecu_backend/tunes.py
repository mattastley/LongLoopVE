"""Lossless XML structure plus convenient setting/table views.

Values are serialized values, not necessarily MTune display units. No blanket
application of firmware-memory scale definitions is valid for tune XML.
"""

from __future__ import annotations

import re

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from . import PARSER_VERSION, SCHEMA_VERSION
from .common import InputError, digest, xml_text


def xml_node(element) -> dict:
    return {
        "tag": element.tag,
        "attributes": dict(element.attrib),
        "text": element.text,
        "tail": element.tail,
        "children": [xml_node(child) for child in element],
    }


def values(text: str | None) -> list[str]:
    return [v.strip() for v in re.split(r"[,\r\n]+", text or "") if v.strip()]


def parse_tune(data: bytes) -> dict:
    text, encoding, warnings = xml_text(data)
    try:
        root = ET.fromstring(text, forbid_dtd=True)
    except (ET.ParseError, DefusedXmlException) as error:
        raise InputError(f"Invalid or unsafe tune XML: {error}") from error
    if root.tag != "MaxxECUSettingsFile":
        status = "protected" if re.search("encrypt|protected", root.tag, re.I) else "unsupported"
        raise InputError(f"Unrecognized tune root: {root.tag}", status)
    settings, tables = [], []
    protected = False
    for element in root:
        if re.search("encrypt|protected", element.tag, re.I):
            protected = True
            warnings.append(f"Opaque protected tune element retained: {element.tag}")
        if element.tag == "ECUSettingsItem":
            settings.append(
                {
                    "name": element.get("name"),
                    "source_value": element.text or "",
                    "attributes": dict(element.attrib),
                    "units": None,
                }
            )
        elif element.tag == "DynamicTable":
            axes = []
            for axis in element.findall("DynamicTableAxis"):
                entries = values(axis.text)
                axes.append(
                    {
                        "name": axis.get("name"),
                        "attributes": dict(axis.attrib),
                        "values": entries,
                        "source_text": axis.text,
                    }
                )
                try:
                    if len(entries) != int(axis.get("AxisCellCount", "0")):
                        warnings.append(
                            f"Axis count mismatch: {element.get('name')}/{axis.get('name')}"
                        )
                except ValueError:
                    warnings.append(f"Invalid axis dimension: {element.get('name')}")
            pages = []
            for page in element.findall("DynamicTableData"):
                entries = values(page.text)
                pages.append(
                    {"attributes": dict(page.attrib), "values": entries, "source_text": page.text}
                )
                try:
                    if len(entries) != int(page.get("XSize", "0")) * int(page.get("YSize", "0")):
                        warnings.append(f"Table dimensions mismatch: {element.get('name')}")
                except ValueError:
                    warnings.append(f"Invalid table dimension: {element.get('name')}")
            tables.append(
                {
                    "name": element.get("name"),
                    "attributes": dict(element.attrib),
                    "axes": axes,
                    "pages": pages,
                    "units": None,
                }
            )
    info_node = root.find("Fileinfo")
    info = {e.tag: e.text for e in info_node} if info_node is not None else {}
    by_name = {s["name"]: s["source_value"] for s in settings}
    vin = by_name.get("OBD VIN", "").strip().upper() or None
    # Preserve nonstandard VIN values as evidence, but don't use them as identifiers.
    valid_vin = (
        vin if vin and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) and len(set(vin)) > 1 else None
    )
    if vin and not valid_vin:
        warnings.append("VIN value is not a usable 17-character identifier.")
    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "id": digest(data),
        "encoding": encoding,
        "file_info": info,
        "status": "partial"
        if protected or any("dimension" in w or "count mismatch" in w for w in warnings)
        else "complete",
        "warnings": warnings,
        "settings": settings,
        "tables": tables,
        "identity_evidence": {
            "ecu_serial": info.get("ECUSerial"),
            "vin": valid_vin,
            "vin_source_value": vin,
        },
        "configuration_facts": {
            k: by_name[k]
            for k in ("Engine Type", "Engine Cylcount", "Engine Volume", "Tune Notes")
            if k in by_name
        },
        "xml": xml_node(root),
    }
