import csv
import io
import json
import shutil
import struct
import zipfile
from pathlib import Path

import lz4.frame
import pytest
from maxxecu_backend import common
from maxxecu_backend.catalog import Catalog
from maxxecu_backend.common import InputError, atomic_json
from maxxecu_backend.inputs import unpack
from maxxecu_backend.logs import parse_binary, parse_log, time_metadata, write_log
from maxxecu_backend.query import log_rows, select_channels
from maxxecu_backend.tunes import parse_tune
from maxxecu_backend.vehicles import assign_records, create_vehicle, merge_vehicles


def tune(serial="ABC", vin="", extra=""):
    return (
        f'<?xml version="1.0" encoding="utf-16"?><MaxxECUSettingsFile><Fileinfo>'
        f"<Softwareversion>1.159</Softwareversion><ECUSerial>{serial}</ECUSerial></Fileinfo>"
        f'<ECUSettingsItem name="OBD VIN">{vin}</ECUSettingsItem>'
        '<ECUSettingsItem name="Unknown">001.20</ECUSettingsItem>'
        f"{extra}</MaxxECUSettingsFile>"
    ).encode()


def archive(path, serial="ABC", vin="", rows="1000\t20\n1100\t30\n", extra=""):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("tune.MaxxECU-save", tune(serial, vin, extra))
        z.writestr("data.maxxlog", "RPM [61]\tLog Timestamp [498]\n" + rows)
        z.writestr("fileinfo01.LogMetaData", "LogRate=0.02\nCreatedTimestamp=2\n")
    return path


def binary(names, scales, columns):
    data = struct.pack("<ii", len(names), len(columns[0]))
    for name, scale in zip(names, scales):
        encoded = name.encode()
        n = len(encoded)
        while n >= 128:
            data += bytes([(n & 127) | 128])
            n >>= 7
        data += bytes([n]) + encoded + struct.pack("<f", scale)
    for col in columns:
        data += struct.pack("<" + "h" * len(col), *col)
    return lz4.frame.compress(data)


def test_binary_layout_scaling_and_long_name():
    data = binary(["x" * 150 + " [9999]", "RPM [61]"], [0.1, 1], [[-32768, 32767], [0, 1000]])
    log = parse_binary(data)
    assert list(log.rows) == [["-3276.8", "0"], ["3276.7", "1000"]]
    assert log.channels[0]["channel_id"] == 9999
    assert log.channels[0]["units"] is None
    assert log.expected_rows == 2


@pytest.mark.parametrize(
    "data",
    [
        b"\x04\x22\x4d\x18broken",
        lz4.frame.compress(struct.pack("<ii", -1, 2)),
        lz4.frame.compress(struct.pack("<ii", 1, 1) + b"\x01x" + struct.pack("<f", 1)),
    ],
)
def test_binary_truncation_rejected(data):
    with pytest.raises(InputError):
        parse_binary(data)


def test_text_bom_empty_column_and_source_values():
    log = parse_log("RPM [61],Custom [9999],\r\n001000,NaN,\r\n".encode("utf-16"))
    assert len(log.channels) == 2
    assert list(log.rows) == [["001000", "NaN"]]


def test_bad_text_row_does_not_silently_truncate():
    log = parse_log(b"RPM [61],Lambda [5]\n1000\n")
    with pytest.raises(InputError, match="Row 2"):
        list(log.rows)


def test_tune_encoding_unknown_elements_and_multipage():
    extra = (
        '<FutureThing name="abc"><Nested>untouched</Nested></FutureThing>'
        '<DynamicTable name="Fuel"><DynamicTableAxis name="X" '
        'AxisCellCount="2">1000,2000</DynamicTableAxis>'
        '<DynamicTableAxis name="Z" AxisCellCount="2">0,1</DynamicTableAxis>'
        '<DynamicTableData XSize="2" YSize="1">1,2</DynamicTableData>'
        '<DynamicTableData XSize="2" YSize="1">3,4</DynamicTableData></DynamicTable>'
    )
    result = parse_tune(tune(extra=extra))
    assert result["encoding"] == "utf-8"
    assert result["warnings"]
    assert result["settings"][1]["source_value"] == "001.20"
    assert result["tables"][0]["pages"][1]["values"] == ["3", "4"]
    assert result["xml"]["children"][3]["children"][0]["text"] == "untouched"


def test_protected_and_xml_entities():
    result = parse_tune(tune(extra="<EncryptedSettings>abc</EncryptedSettings>"))
    assert result["status"] == "partial"
    with pytest.raises(InputError):
        parse_tune(
            b'<!DOCTYPE x [<!ENTITY y "abc">]><MaxxECUSettingsFile>&y;</MaxxECUSettingsFile>'
        )


@pytest.mark.parametrize(
    "name", ["../escape.maxxlog", "/root.maxxlog", "C:\\file.maxxlog", "sub\\..\\escape.maxxlog"]
)
def test_zip_paths_rejected(name):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(name, b"RPM [61],Lambda [5]\n1000,1\n")
    with pytest.raises(InputError, match="Unsafe"):
        unpack(buffer.getvalue(), "input.zip")


def test_placeholder_date_and_elapsed(tmp_path):
    meta = {"LogRate": "0.01", "CreatedTimestamp": "2"}
    assert time_metadata(meta)["created_at"] is None
    assert time_metadata({})["sample_interval_seconds"] is None
    output = tmp_path / "out.csv"
    result = write_log(
        parse_log(b"RPM [61]\tLog Timestamp [498]\n1000\t200\n1100\t20\n1200\t30\n"), output, meta
    )
    rows = list(csv.DictReader(output.open()))
    assert [r["elapsed_seconds"] for r in rows] == ["0", "0.02", "0.05"]
    assert result["timing"]["duration_seconds"] == pytest.approx(0.05)
    result = write_log(parse_log(b"RPM [61],x [999]\n1000,1\n1100,2\n"), output, meta)
    assert result["timing"]["duration_seconds"] == 0.01


def test_catalog_dedup_correction_relocation(tmp_path):
    source = archive(tmp_path / "a.zip")
    catalog = Catalog(tmp_path / "library")
    record = catalog.ingest(source)["records"][0]
    before = catalog.load()
    assert len(before["vehicles"]) == 1
    assert catalog.ingest(source)["status"] == "already_ingested"
    renamed = tmp_path / "renamed.zip"
    shutil.copyfile(source, renamed)
    catalog.ingest(renamed)
    assert len(next(iter(catalog.load()["sources"].values()))["provenance"]) == 2
    with catalog.writer() as index:
        second = create_vehicle(index, "Corrected car")
        assign_records(index, [record], second)
    catalog.ingest(source)
    assert catalog.manifest(record)["assignment"]["vehicle_id"] == second
    relocated = tmp_path / "moved"
    shutil.copytree(catalog.root, relocated)
    moved = Catalog(relocated)
    manifest = moved.manifest(record)
    channels = select_channels(manifest["log"]["channels"], ["61"])
    assert list(log_rows(moved, manifest, channels, 0, 0.1))[0]["c0"] == "1000"
    with moved.writer() as index:
        old = next(k for k in index["vehicles"] if k != second)
        merge_vehicles(index, old, second)
    assert moved.load()["vehicles"][old]["merged_into"] == second


def test_conflicting_vins_require_review(tmp_path):
    c = Catalog(tmp_path / "library")
    a = c.ingest(archive(tmp_path / "a.zip", vin="WBA12345678901234"))["records"][0]
    b = c.ingest(archive(tmp_path / "b.zip", vin="WBA98765432109876"))["records"][0]
    assert c.manifest(a)["assignment"]["vehicle_id"]
    assert c.manifest(b)["assignment"]["review_required"]
    assert c.manifest(b)["assignment"]["vehicle_id"] is None


def test_tune_dedup_and_same_name_distinct_logs(tmp_path):
    c = Catalog(tmp_path / "library")
    c.ingest(archive(tmp_path / "a.zip"))
    c.ingest(archive(tmp_path / "b.zip", rows="1002\t30\n"))
    assert len(c.load()["tunes"]) == 1
    assert len(c.load()["records"]) == 2


def test_corrupt_source_retained_and_next_ingest_works(tmp_path):
    c = Catalog(tmp_path / "library")
    broken = tmp_path / "bad.zip"
    broken.write_bytes(b"PK\x03\x04truncated")
    result = c.ingest(broken)
    assert c.manifest(result["records"][0])["status"] == "corrupt"
    source = c.load()["sources"][result["source_id"]]
    assert c.path(source["original"]).read_bytes() == broken.read_bytes()
    assert c.ingest(archive(tmp_path / "good.zip"))["status"] == "complete"


def test_writer_lock_and_interrupted_commit(tmp_path, monkeypatch):
    c = Catalog(tmp_path / "library")
    with c.writer() as index:
        create_vehicle(index, "before")
        with pytest.raises(ValueError, match="lock"):
            with c.writer():
                pass
    original = c.index_path.read_bytes()
    with pytest.raises(RuntimeError):
        with c.writer() as index:
            create_vehicle(index, "uncommitted")
            raise RuntimeError("interrupted")
    assert c.index_path.read_bytes() == original

    def fail_replace(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(common.os, "replace", fail_replace)
    with pytest.raises(OSError):
        atomic_json(c.index_path, {"broken": True})
    assert c.index_path.read_bytes() == original
    assert not list(c.root.glob(".pending-*"))


def test_catalog_paths_cannot_escape(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        Catalog(tmp_path).path("../private")


def test_unknown_channel_selector_is_error():
    channels = parse_log(b"RPM [61],Duplicate [61]\n1,2\n").channels
    with pytest.raises(ValueError, match="matches 2"):
        select_channels(channels, ["61"])
    assert select_channels(channels, ["c1"])[0]["label"] == "Duplicate"


def test_missing_timing_rejects_time_query(tmp_path):
    p = tmp_path / "log.csv"
    p.write_text("RPM [61],x [999]\n1000,1\n")
    c = Catalog(tmp_path / "library")
    r = c.ingest(p)["records"][0]
    m = c.manifest(r)
    with pytest.raises(ValueError, match="unknown"):
        list(log_rows(c, m, m["log"]["channels"], start=0))


def test_nonzero_metadata_dimension_mismatch(tmp_path):
    result = write_log(parse_log(b"RPM [61],x [999]\n1000,1\n"), tmp_path / "x.csv", {"Rows": "5"})
    assert result["status"] == "partial"
    assert any("Rows=5" in message for message in result["warnings"])


def test_invalid_logged_interval_does_not_invent_time(tmp_path):
    output = tmp_path / "x.csv"
    result = write_log(
        parse_log(b"RPM [61]\tLog Timestamp [498]\n1000\t20\n1001\t-1\n1002\t20\n"), output, {}
    )
    rows = list(csv.DictReader(output.open()))
    assert rows[1]["elapsed_seconds"] == rows[2]["elapsed_seconds"] == ""
    assert result["timing"]["duration_seconds"] is None


def test_unsupported_and_separate_tune(tmp_path):
    c = Catalog(tmp_path / "library")
    unknown = tmp_path / "weird.bin"
    unknown.write_bytes(b"not a log")
    rid = c.ingest(unknown)["records"][0]
    assert c.manifest(rid)["status"] == "unsupported"
    p = tmp_path / "data.csv"
    p.write_text("RPM [61],x [999]\n1000,1\n")
    t = tmp_path / "tune.xml"
    t.write_bytes(tune())
    rid = c.ingest(p, external_tune=t, capture_timing="download")["records"][0]
    m = c.manifest(rid)
    assert m["tune_association"] == {
        "basis": "user_supplied",
        "capture_timing": "download",
        "timing_basis": "user_supplied",
    }
    assert m["assignment"]["vehicle_id"]


def test_cli_agent_workflow_and_merge_evidence(tmp_path):
    from maxxecu import parser, run

    def cli(*args):
        output, status = run(parser().parse_args(["--catalog", str(tmp_path / "library"), *args]))
        assert status == 0
        return output

    extra = (
        '<DynamicTable name="Fuel"><DynamicTableAxis name="X" '
        'AxisCellCount="2">1000,2000</DynamicTableAxis>'
        '<DynamicTableData XSize="2" YSize="1">1,2</DynamicTableData></DynamicTable>'
    )
    source = archive(tmp_path / "source.zip", extra=extra)
    rid = cli("ingest", str(source))["files"][0]["records"][0]
    car = cli("list", "--vehicles")[0]["id"]
    assert cli("list", "--vehicle", car)[0]["id"] == rid
    assert cli("show", rid)["tune_association"]["capture_timing"] == "unknown"
    assert (
        cli("show", rid, "--channels", "61", "--start", "0.01", "--end", "0.1")["rows"][0]["c0"]
        == "1100"
    )
    assert cli("show", rid, "--tables")[0]["name"] == "Fuel"
    assert cli("show", rid, "--table", "Fuel")[0]["pages"][0]["values"] == ["1", "2"]
    output = tmp_path / "selection.csv"
    cli("export", rid, "--channels", "61", "--output", str(output))
    assert len(list(csv.DictReader(output.open()))) == 2
    assert (
        json.loads(output.with_suffix(".csv.metadata.json").read_text())["channels"][0][
            "channel_id"
        ]
        == 61
    )
    cli("vehicle", "edit", car, "--ecu-serial", "SPARE", "--from-date", "2020-01-01")
    new_car = cli("vehicle", "assign", rid, "--new", "Corrected car")["id"]
    cli("vehicle", "merge", car, new_car)
    assert any(a["ecu_serial"] == "SPARE" for a in cli("list", "--vehicles")[1]["ecu_assignments"])
    assert cli("ingest", str(source))["files"][0]["status"] == "already_ingested"
    assert cli("show", rid)["assignment"]["vehicle_id"] == new_car
    with pytest.raises(ValueError, match="merged"):
        Catalog(tmp_path / "library").ingest(source, forced_vehicle=car)


def test_export_does_not_overwrite_metadata(tmp_path):
    from maxxecu import parser, run

    source = archive(tmp_path / "a.zip")
    c = Catalog(tmp_path / "library")
    rid = c.ingest(source)["records"][0]
    output = tmp_path / "selection.csv"
    metadata = output.with_suffix(".csv.metadata.json")
    metadata.write_text("keep this")
    with pytest.raises(ValueError, match="metadata destination"):
        run(parser().parse_args(["--catalog", str(c.root), "export", rid, "--output", str(output)]))
    assert metadata.read_text() == "keep this"
    assert not output.exists()


def test_independent_skill_backends_stay_in_sync():
    skills = Path(__file__).resolve().parents[1] / ".agents" / "skills"
    log = skills / "maxxecu-log-ingest"
    tune = skills / "maxxecu-tune-ingest"
    files = {p.relative_to(log) for p in (log / "scripts").rglob("*.py")}
    assert files == {p.relative_to(tune) for p in (tune / "scripts").rglob("*.py")}
    for relative in files | {Path("requirements.txt")}:
        assert (log / relative).read_bytes() == (tune / relative).read_bytes()
