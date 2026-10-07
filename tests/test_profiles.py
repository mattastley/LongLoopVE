import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from longloopve.ingest import IngestionError, run
from longloopve.profiles import load_profile

PROFILE_PATH = Path(__file__).parents[1] / "profiles/maxxecu-race-v1-alpha-n.json"


def write_profile(tmp_path):
    definition = {
        "schema_version": 1,
        "name": "test Alpha-N profile",
        "ecu": "test ECU",
        "fueling_model": "alpha_n",
        "ve_axes": ["engine_speed", "throttle_body_position"],
        "channels": {
            "engine_speed": {"channel": "RPM", "units": "rpm", "required": True},
            "throttle_body_position": {"channel": "ETPS", "units": "%", "required": True},
            "pedal_position": {"channel": "TPS", "units": "%", "required": False},
        },
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(definition))
    return path


def test_maxxecu_profile_distinguishes_axes_and_bank_ve_roles():
    profile = load_profile(PROFILE_PATH)
    channels = profile.definition["channels"]
    assert profile.definition["ve_axes"] == ["engine_speed", "throttle_body_position"]
    assert channels["throttle_body_position"]["channel"] == "ETPS_UC9"
    assert channels["pedal_position"]["channel"] == "TPS"
    assert channels["current_ve_bank_a"]["channel"] == "VE_Bank_A_UC5"
    assert channels["suggested_ve_bank_a"]["channel"] == "CorVE_BankA_UC1"


def test_alpha_n_rejects_pedal_axis(tmp_path):
    path = write_profile(tmp_path)
    definition = json.loads(path.read_text())
    definition["ve_axes"][1] = "pedal_position"
    definition["channels"]["pedal_position"]["required"] = True
    path.write_text(json.dumps(definition))
    with pytest.raises(ValueError, match="throttle_body_position"):
        load_profile(path)


def test_profile_reports_missing_optional_channels_without_changing_data(tmp_path):
    profile = load_profile(write_profile(tmp_path))
    manifest = {
        "channels": [
            {"name": "RPM", "units": "rpm", "samples": 2, "issues": []},
            {"name": "ETPS", "units": "%", "samples": 2, "issues": ["null_values"]},
        ]
    }
    before = json.dumps(manifest)
    result = profile.apply(manifest)
    assert result["mapped_roles"]["pedal_position"]["status"] == "missing"
    assert result["mapped_roles"]["throttle_body_position"]["issues"] == ["null_values"]
    assert result["validation"] == "channel_presence_and_units_only"
    assert result["source"]["sha256"] == profile.sha256
    assert json.dumps(manifest) == before


@pytest.mark.parametrize("problem", ["missing", "wrong_units"])
def test_profile_rejects_missing_or_misscaled_axis_channel(tmp_path, problem):
    profile = load_profile(write_profile(tmp_path))
    channels = [{"name": "RPM", "units": "rpm", "samples": 1, "issues": []}]
    if problem == "wrong_units":
        channels.append({"name": "ETPS", "units": "deg", "samples": 1, "issues": []})
    with pytest.raises(ValueError, match="throttle_body_position"):
        profile.apply({"channels": channels})


def test_profile_failure_prevents_publishing_outputs(tmp_path, monkeypatch):
    profile_path = write_profile(tmp_path)
    source = tmp_path / "test.xrk"
    source.write_bytes(b"test input")
    target = tmp_path / "output"

    def decoder(command, **kwargs):
        assert set(command[6:]) == {"RPM", "ETPS", "Extra"}
        stage = Path(command[4])
        (stage / "partial.parquet").write_bytes(b"incomplete")
        (stage / "manifest.json").write_text(
            json.dumps(
                {
                    "warnings": [],
                    "channels": [
                        {"name": "RPM", "units": "rpm", "samples": 1, "issues": []},
                        {"name": "ETPS", "units": "deg", "samples": 1, "issues": []},
                    ],
                }
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("longloopve.ingest.subprocess.run", decoder)
    with pytest.raises(IngestionError, match="expected '%'"):
        run(source, output=target, profile=profile_path, required=["Extra"])
    assert not target.exists()
    assert not list(tmp_path.glob(".longloopve-*"))
