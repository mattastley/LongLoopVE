import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from longloopve import ingest, worker
from longloopve.quality import summarize_channel


def channel(name, times, values, units="rpm"):
    schema = pa.schema(
        [
            pa.field("timecodes", pa.int64()),
            pa.field(name, pa.float64(), metadata={b"units": units.encode()}),
        ]
    )
    return pa.Table.from_arrays([pa.array(times), pa.array(values)], schema=schema)


def test_quality_distinguishes_nulls_nonfinite_duplicates_and_gaps():
    table = channel("RPM", [0, 10, 10, 20, 100, 90, None], [1, None, float("inf"), 2, 3, 4, 5])
    result = summarize_channel("RPM", table)
    assert result["null_values"] == 1
    assert result["non_finite_values"] == 1
    assert result["null_timestamps"] == 1
    assert result["duplicate_timestamps"] == 1
    assert result["out_of_order_intervals"] == 1
    assert result["large_gaps"] == 1
    assert result["estimated_rate_hz"] == 100
    assert table["timecodes"].to_pylist() == [0, 10, 10, 20, 100, 90, None]


def test_extract_preserves_channel_timelines_and_arrow_metadata(tmp_path, monkeypatch):
    tables = {
        "RPM": channel("RPM", [0, 10, 20], [800, 900, 1000]),
        "../../Lambda": channel("../../Lambda", [0, 25], [1.01, 0.99], "lambda"),
    }
    monkeypatch.setattr(
        worker, "aim_xrk", lambda _: SimpleNamespace(channels=tables, metadata={"Vehicle": "test"})
    )
    result = worker.extract(Path("fake.xrk"), tmp_path, "ingest", ["RPM"])
    for summary in result["channels"]:
        saved = pq.read_table(tmp_path / summary["file"])
        assert saved.equals(tables[summary["name"]], check_metadata=True)
        assert Path(summary["file"]).parent == Path("channels")
    assert result["processing"] == {"resampled": False, "units_converted": False}


@pytest.mark.parametrize("case", ["missing", "empty", "all_empty"])
def test_required_channels_and_empty_decode_fail(tmp_path, monkeypatch, case):
    tables = {"RPM": channel("RPM", [0], [1000])}
    if case in {"empty", "all_empty"}:
        tables["Lambda"] = channel("Lambda", [], [], "lambda")
    if case == "all_empty":
        del tables["RPM"]
    monkeypatch.setattr(worker, "aim_xrk", lambda _: SimpleNamespace(channels=tables, metadata={}))
    with pytest.raises(ValueError):
        worker.extract(Path("fake.xrk"), tmp_path, "ingest", ["Lambda"])
    assert not (tmp_path / "channels").exists()


def test_supervisor_hashes_snapshot_and_never_overwrites(tmp_path, monkeypatch):
    source = tmp_path / "test.xrk"
    source.write_bytes(b"test input")
    target = tmp_path / "result"

    def fake_decoder(command, **kwargs):
        snapshot = Path(command[3])
        stage = Path(command[4])
        assert snapshot.read_bytes() == b"test input"
        (stage / "manifest.json").write_text(json.dumps({"warnings": []}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ingest.subprocess, "run", fake_decoder)
    result = ingest.run(source, output=target)
    assert result["source"]["sha256"] == hashlib.sha256(b"test input").hexdigest()
    assert json.loads((target / "manifest.json").read_text()) == result
    before = (target / "manifest.json").read_bytes()
    with pytest.raises(ingest.IngestionError):
        ingest.run(source, output=target)
    assert (target / "manifest.json").read_bytes() == before
    assert not list(tmp_path.glob(".longloopve-*"))


@pytest.mark.parametrize("failure", ["timeout", "crash"])
def test_failed_worker_cleans_partial_output(tmp_path, monkeypatch, failure):
    source = tmp_path / "test.xrk"
    source.write_bytes(b"test input")
    target = tmp_path / "result"

    def broken_decoder(command, **kwargs):
        (Path(command[4]) / "partial.parquet").write_bytes(b"incomplete")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        return SimpleNamespace(returncode=-11)

    monkeypatch.setattr(ingest.subprocess, "run", broken_decoder)
    with pytest.raises(ingest.IngestionError, match="exceeded|failed"):
        ingest.run(source, output=target)
    assert not target.exists()
    assert not list(tmp_path.glob(".longloopve-*"))


def test_malformed_log_fails_through_real_cli_without_output(tmp_path):
    source = tmp_path / "bad.xrk"
    source.write_bytes(b"not an AIM log\n" * 20)
    target = tmp_path / "result"
    result = subprocess.run(
        [sys.executable, "-m", "longloopve", "ingest", str(source), "--output", str(target)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert "Decoder failed" in result.stderr
    assert not result.stdout
    assert not target.exists()


@pytest.mark.parametrize("filename,content", [("empty.xrk", b""), ("log.csv", b"a,b")])
def test_invalid_input_rejected_before_decoder(tmp_path, filename, content):
    source = tmp_path / filename
    source.write_bytes(content)
    with pytest.raises(ingest.IngestionError):
        ingest.run(source)


def test_metadata_is_strict_json_compatible():
    result = worker.json_safe({"bad": float("nan"), "raw": b"\xff", "rows": (1, 2)})
    assert result == {"bad": None, "raw": {"encoding": "hex", "data": "ff"}, "rows": [1, 2]}
    json.dumps(result, allow_nan=False)


def test_source_modified_during_copy_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "changing.xrk"
    source.write_bytes(b"initial")
    copyfile = ingest.shutil.copyfile

    def changing_copy(src, dest):
        copyfile(src, dest)
        source.write_bytes(b"changed file with more bytes")

    monkeypatch.setattr(ingest.shutil, "copyfile", changing_copy)
    with pytest.raises(ingest.IngestionError, match="Source changed"):
        ingest.run(source, output=tmp_path / "result")
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_rejected(tmp_path, timeout):
    source = tmp_path / "test.xrk"
    source.write_bytes(b"test input")
    with pytest.raises(ingest.IngestionError, match="finite positive"):
        ingest.run(source, timeout=timeout)
