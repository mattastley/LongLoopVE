"""Transactional car/calibration history over immutable ingested source evidence."""

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from longloopve._maxxecu.common import atomic_bytes, atomic_json, digest, now
from longloopve.analysis import (
    interpolation,
    load_channels,
    observations,
    reconstruct,
    settings,
    validate_profile,
)
from longloopve.ingest import run
from longloopve.tune import read_tables


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def identity(metadata, profile):
    """Only explicit metadata fields establish identity; filenames never do."""
    result = []
    for role in ("name", "car"):
        path = profile.get("identity_fields", {}).get(role)
        if path is None:
            aliases = {"name", "driver"} if role == "name" else {"car", "vehicle"}
            candidates = {
                v.strip()
                for k, v in metadata.items()
                if k.casefold() in aliases and isinstance(v, str) and v.strip()
            }
            result.append(next(iter(candidates)) if len(candidates) == 1 else None)
            continue
        value = metadata
        for key in path.split("."):
            if not isinstance(value, dict):
                value = None
                break
            matches = [v for k, v in value.items() if k.casefold() == key.casefold()]
            value = matches[0] if len(matches) == 1 else None
        result.append(value.strip() if isinstance(value, str) and value.strip() else None)
    return result


class Library:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.index_path = self.root / "library.json"

    def load(self):
        if not self.index_path.exists():
            return {"schema_version": 1, "cars": {}, "logs": {}}
        result = read_json(self.index_path)
        if result.get("schema_version") != 1:
            raise ValueError("Unsupported library schema")
        return result

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Asset path escapes library")
        return path

    @contextmanager
    def writer(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".writer.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ValueError(
                f"Library writer lock exists: {lock}; check its PID before recovery"
            ) from exc
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(str(os.getpid()))
            index = self.load()
            yield index
            atomic_json(self.index_path, index)
        finally:
            lock.unlink()

    def _retain_tune(self, source, mapping):
        data = Path(source).read_bytes()
        tables = read_tables(data, mapping)
        relative = f"tunes/{tables['sha256']}/original.bin"
        target = self.path(relative)
        if target.exists():
            if target.read_bytes() != data:
                raise ValueError("Retained tune does not match source hash")
        else:
            atomic_bytes(target, data)
        return {
            "id": tables["sha256"],
            "original": relative,
            "source_name": Path(source).name,
            "tables": tables,
            "provided_at": now(),
        }

    def _create(self, index, name, identifier, tune, mapping, profile, nickname=None):
        if not name or not identifier:
            raise ValueError("Car setup requires both XRK Name and Car identifier")
        if any([name, identifier] in c["identities"] for c in index["cars"].values()):
            raise ValueError("Name/Car identity already belongs to a registered car")
        validate_profile(profile)
        retained = self._retain_tune(tune, mapping)
        car_id, epoch_id = uuid.uuid4().hex, uuid.uuid4().hex
        epoch = {
            "id": epoch_id,
            "created_at": now(),
            "reference_tune": retained["id"],
            "latest_tune": retained["id"],
            "tunes": {retained["id"]: retained},
            "archived": False,
            "logs": [],
        }
        car = {
            "id": car_id,
            "nickname": nickname or identifier,
            "identities": [[name, identifier]],
            "mapping": mapping,
            "profile": profile,
            "active_epoch": epoch_id,
            "epochs": {epoch_id: epoch},
            "history": [{"action": "created", "at": now()}],
        }
        index["cars"][car_id] = car
        return car

    def create(self, name, identifier, tune, mapping, profile, nickname=None):
        with self.writer() as index:
            return self._create(index, name, identifier, tune, mapping, profile, nickname)

    def add_tune(self, car_id, source, impact):
        if impact not in {"preserve", "reset"}:
            raise ValueError("Tune impact must explicitly be preserve or reset")
        with self.writer() as index:
            car = index["cars"][car_id]
            old = car["epochs"][car["active_epoch"]]
            retained = self._retain_tune(source, car["mapping"])
            reference = old["tunes"][old["reference_tune"]]["tables"]
            for bank in ("A", "B"):
                for axis in ("rpm", "etps"):
                    if retained["tables"][bank][axis] != reference[bank][axis]:
                        raise ValueError("Changing RPM/ETPS breakpoints is not supported")
            if impact == "reset":
                old["archived"] = True
                epoch_id = uuid.uuid4().hex
                old = {
                    "id": epoch_id,
                    "created_at": now(),
                    "reference_tune": retained["id"],
                    "latest_tune": retained["id"],
                    "tunes": {},
                    "archived": False,
                    "logs": [],
                }
                car["epochs"][epoch_id] = old
                car["active_epoch"] = epoch_id
            old["tunes"][retained["id"]] = retained
            old["latest_tune"] = retained["id"]
            car["history"].append(
                {
                    "action": "tune",
                    "impact": impact,
                    "tune": retained["id"],
                    "epoch": old["id"],
                    "at": now(),
                }
            )
            return {"car": car_id, "epoch": old["id"], "tune": retained["id"], "impact": impact}

    def import_log(
        self, source, car_id=None, epoch_id=None, recording_tune=None, resolver=None, timeout=120
    ):
        source = Path(source)
        data = source.read_bytes()
        source_id = digest(data)
        with self.writer() as index:
            if source_id in index["logs"]:
                existing = index["logs"][source_id]
                if car_id and existing["car"] != car_id:
                    raise ValueError("Log already assigned to another car; use log-assign")
                if epoch_id and existing["epoch"] != epoch_id:
                    raise ValueError("Log already assigned to another calibration; use log-assign")
                return {"status": "already_imported", "log": source_id, **existing}
            relative = f"logs/{source_id}"
            folder = self.path(relative)
            # Decode a retained copy of the exact hashed bytes, not a changing source file.
            if not folder.exists():
                with tempfile.TemporaryDirectory(prefix=".import-", dir=self.root) as temporary:
                    snapshot = Path(temporary) / source.name
                    snapshot.write_bytes(data)
                    result = Path(temporary) / "decoded"
                    manifest = run(snapshot, output=result, timeout=timeout)
                    (result / ("original" + source.suffix.lower())).write_bytes(data)
                    folder.parent.mkdir(exist_ok=True)
                    os.replace(result, folder)
            manifest = read_json(folder / "manifest.json")
            if manifest["source"]["sha256"] != source_id:
                raise ValueError("Ingestion hash does not match original")
            if car_id:
                car = index["cars"][car_id]
                log_identity = identity(manifest["metadata"], car["profile"])
            else:
                matches = []
                for candidate in index["cars"].values():
                    ident = identity(manifest["metadata"], candidate["profile"])
                    if None not in ident and ident in candidate["identities"]:
                        matches.append(candidate["id"])
                if len(matches) == 1:
                    car = index["cars"][matches[0]]
                else:
                    if resolver is None:
                        raise ValueError(
                            "Unknown/ambiguous XRK Name/Car; specify --car "
                            "or use an interactive terminal"
                        )
                    selected = resolver(manifest["metadata"], index)
                    if isinstance(selected, dict):
                        car = self._create(index, **selected)
                    else:
                        car = index["cars"][selected]
                log_identity = identity(manifest["metadata"], car["profile"])
            channels = load_channels(folder, manifest, car["profile"])
            epoch = car["epochs"][epoch_id or car["active_epoch"]]
            if recording_tune and recording_tune not in epoch["tunes"]:
                raise ValueError("Recording tune must be a known tune in the selected calibration")
            # Explicit car selection/resolution confirms an additional metadata identity.
            if None not in log_identity and log_identity not in car["identities"]:
                if any(log_identity in c["identities"] for c in index["cars"].values() if c != car):
                    raise ValueError(
                        "XRK identity belongs to another car; resolve the identity conflict"
                    )
                car["identities"].append(log_identity)
            initial = observations(channels, car["profile"], settings())
            record = {
                "car": car["id"],
                "epoch": epoch["id"],
                "directory": relative,
                "source_name": source.name,
                "imported_at": now(),
                "identity": log_identity,
                "recording_tune": recording_tune,
                "initial_summary": {b: initial[b]["summary"] for b in ("A", "B")},
            }
            index["logs"][source_id] = record
            epoch["logs"].append(source_id)
            return {"status": "imported", "log": source_id, **record}

    def assign_log(self, source_id, car_id, epoch_id=None, recording_tune=None):
        with self.writer() as index:
            record = index["logs"][source_id]
            car = index["cars"][car_id]
            epoch = car["epochs"][epoch_id or car["active_epoch"]]
            if recording_tune and recording_tune not in epoch["tunes"]:
                raise ValueError("Recording tune is not in selected calibration")
            folder = self.path(record["directory"])
            load_channels(folder, read_json(folder / "manifest.json"), car["profile"])
            old = index["cars"][record["car"]]["epochs"][record["epoch"]]
            old["logs"].remove(source_id)
            epoch["logs"].append(source_id)
            car["history"].append(
                {
                    "action": "log_assigned",
                    "log": source_id,
                    "from_car": record["car"],
                    "from_epoch": record["epoch"],
                    "epoch": epoch["id"],
                    "at": now(),
                }
            )
            record.update(car=car_id, epoch=epoch["id"], recording_tune=recording_tune)
            return record

    def calculate(self, car_id, overrides=None, epoch_id=None):
        config = settings(overrides)
        index = self.load()
        car = index["cars"][car_id]
        epoch = car["epochs"][epoch_id or car["active_epoch"]]
        reference = epoch["tunes"][epoch["reference_tune"]]["tables"]
        latest = epoch["tunes"][epoch["latest_tune"]]["tables"]
        records = {"A": [], "B": []}
        provenance = []
        for source_id in epoch["logs"]:
            record = index["logs"][source_id]
            folder = self.path(record["directory"])
            manifest = read_json(folder / "manifest.json")
            aligned = observations(
                load_channels(folder, manifest, car["profile"]), car["profile"], config
            )
            for bank in ("A", "B"):
                if record["recording_tune"]:
                    tune = epoch["tunes"][record["recording_tune"]]["tables"][bank]
                    obs = aligned[bank]
                    cells, weights = interpolation(
                        obs["rpm"], obs["etps"], np.asarray(tune["rpm"]), np.asarray(tune["etps"])
                    )
                    # This is context only; logged corrected values remain the solver targets.
                    prediction = (np.asarray(tune["values"]).ravel()[cells] * weights).sum(axis=1)
                    if len(prediction):
                        aligned[bank]["summary"]["recording_base_ve_range"] = [
                            float(prediction.min()),
                            float(prediction.max()),
                        ]
                        valid = np.isfinite(obs["current_ve"])
                        mismatch = valid & (
                            np.abs(obs["current_ve"] - prediction)
                            > config["sanity_relative_tolerance"] * prediction
                        )
                        if np.any(mismatch):
                            aligned[bank]["summary"]["warnings"].append(
                                "Logged current VE differs from recording tune interpolation "
                                f"in {int(mismatch.sum())} samples"
                            )
                else:
                    aligned[bank]["summary"]["warnings"].append(
                        "Recording-time tune unknown; latest base not assumed historical"
                    )
                records[bank].append(aligned[bank])
            provenance.append(
                {
                    "log": source_id,
                    "source_name": record["source_name"],
                    "identity": record["identity"],
                    "decoder": manifest["decoder"],
                    "device_validation": manifest["device_validation"],
                    "warnings": manifest["warnings"],
                    "recording_tune": record["recording_tune"],
                    "summary": {b: aligned[b]["summary"] for b in ("A", "B")},
                }
            )
        return {
            "schema_version": 1,
            "created_at": now(),
            "car": car_id,
            "nickname": car["nickname"],
            "epoch": epoch["id"],
            "archived": epoch["archived"],
            "reference_tune": epoch["reference_tune"],
            "latest_tune": epoch["latest_tune"],
            "settings": config,
            "mapping": car["mapping"],
            "profile": car["profile"],
            "latest_tune_original": str(
                self.path(epoch["tunes"][epoch["latest_tune"]]["original"])
            ),
            "logs": provenance,
            "banks": {
                b: reconstruct(reference[b], latest[b], records[b], config["min_observations"])
                for b in ("A", "B")
            },
        }

    def export(self, car_id, output, overrides=None, epoch_id=None):
        from longloopve.reports import export

        return export(self.calculate(car_id, overrides, epoch_id), Path(output))
