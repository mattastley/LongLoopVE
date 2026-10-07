"""Deterministic automatic grouping with explicit, durable user corrections."""

from __future__ import annotations

import uuid

from .common import now

PROFILE_FIELDS = {"nickname", "vin", "year", "make", "model", "engine", "modification_notes"}


def create_vehicle(index: dict, nickname: str | None = None) -> str:
    identity = "vehicle-" + uuid.uuid4().hex[:12]
    index["vehicles"][identity] = {
        "id": identity,
        "created_at": now(),
        "provisional": True,
        "profile": {k: nickname if k == "nickname" else None for k in sorted(PROFILE_FIELDS)},
        "vin_evidence": [],
        "ecu_assignments": [],
        "history": [],
    }
    return identity


def vins(vehicle: dict) -> set[str]:
    return set(vehicle["vin_evidence"]) | (
        {vehicle["profile"]["vin"]} if vehicle["profile"].get("vin") else set()
    )


def serials(vehicle: dict) -> set[str]:
    return {e["ecu_serial"] for e in vehicle["ecu_assignments"] if e.get("ecu_serial")}


def automatic_assignment(index: dict, evidence: dict, forced: str | None = None) -> dict:
    serial, vin = evidence.get("ecu_serial"), evidence.get("vin")
    if forced:
        if forced not in index["vehicles"]:
            raise ValueError(f"Unknown vehicle: {forced}")
        return {
            "vehicle_id": forced,
            "reason": "explicit_user_assignment",
            "confidence": "confirmed",
            "review_required": False,
        }
    candidates = {k: v for k, v in index["vehicles"].items() if not v.get("merged_into")}
    vin_hits = [k for k, v in candidates.items() if vin and vin in vins(v)]
    serial_hits = [k for k, v in candidates.items() if serial and serial in serials(v)]
    if len(vin_hits) == 1 and (not serial_hits or serial_hits == vin_hits):
        return {
            "vehicle_id": vin_hits[0],
            "reason": "matching_vin",
            "confidence": "high",
            "review_required": False,
        }
    if not vin_hits and len(serial_hits) == 1:
        target = candidates[serial_hits[0]]
        if not vin or not vins(target) or vin in vins(target):
            return {
                "vehicle_id": serial_hits[0],
                "reason": "matching_ecu_serial",
                "confidence": "medium",
                "review_required": False,
            }
    conflict = bool(vin_hits or serial_hits)
    if conflict:
        return {
            "vehicle_id": None,
            "reason": "conflicting_identity_evidence",
            "confidence": "uncertain",
            "review_required": True,
            "candidates": sorted(set(vin_hits + serial_hits)),
        }
    if serial or vin:
        identity = create_vehicle(index)
        return {
            "vehicle_id": identity,
            "reason": "new_vin" if vin else "new_ecu_serial",
            "confidence": "high" if vin else "medium",
            "review_required": False,
        }
    return {
        "vehicle_id": None,
        "reason": "no_stable_identity",
        "confidence": "unknown",
        "review_required": False,
    }


def add_evidence(
    index: dict, assignment: dict, evidence: dict, record_id: str, date: str | None
) -> None:
    identity = assignment["vehicle_id"]
    if not identity:
        return
    target = index["vehicles"][identity]
    vin, serial = evidence.get("vin"), evidence.get("ecu_serial")
    if vin and vin not in target["vin_evidence"]:
        target["vin_evidence"].append(vin)
    if serial:
        existing = next(
            (
                a
                for a in target["ecu_assignments"]
                if a["ecu_serial"] == serial and a.get("source") == "recordings"
            ),
            None,
        )
        if not existing:
            existing = {
                "ecu_serial": serial,
                "source": "recordings",
                "observed_from": date,
                "observed_to": date,
                "recording_ids": [],
            }
            target["ecu_assignments"].append(existing)
        if record_id not in existing["recording_ids"]:
            existing["recording_ids"].append(record_id)
        if date:
            existing["observed_from"] = min(existing["observed_from"] or date, date)
            existing["observed_to"] = max(existing["observed_to"] or date, date)


def rebuild_evidence(index: dict) -> None:
    for vehicle in index["vehicles"].values():
        vehicle["vin_evidence"] = []
        vehicle["ecu_assignments"] = [
            a for a in vehicle["ecu_assignments"] if a.get("source") != "recordings"
        ]
    for record_id, record in index["records"].items():
        add_evidence(
            index,
            record["assignment"],
            record.get("identity_evidence", {}),
            record_id,
            record.get("created_at"),
        )


def assign_records(index: dict, record_ids: list[str], vehicle_id: str) -> None:
    if vehicle_id not in index["vehicles"] or index["vehicles"][vehicle_id].get("merged_into"):
        raise ValueError(f"Unknown or merged vehicle: {vehicle_id}")
    for record_id in record_ids:
        if record_id not in index["records"]:
            raise ValueError(f"Unknown recording: {record_id}")
    for record_id in record_ids:
        record = index["records"][record_id]
        record.setdefault("assignment_history", []).append(
            {"at": now(), "previous": record["assignment"], "to": vehicle_id}
        )
        record["assignment"] = {
            "vehicle_id": vehicle_id,
            "reason": "explicit_user_assignment",
            "confidence": "confirmed",
            "review_required": False,
        }
    rebuild_evidence(index)


def merge_vehicles(index: dict, source: str, target: str) -> None:
    if source == target or source not in index["vehicles"] or target not in index["vehicles"]:
        raise ValueError("Merge requires two distinct existing vehicle IDs.")
    if index["vehicles"][source].get("merged_into") or index["vehicles"][target].get("merged_into"):
        raise ValueError("Cannot merge already merged vehicles.")
    ids = [k for k, r in index["records"].items() if r["assignment"]["vehicle_id"] == source]
    for assignment in index["vehicles"][source]["ecu_assignments"]:
        if (
            assignment.get("source") == "user"
            and assignment not in index["vehicles"][target]["ecu_assignments"]
        ):
            index["vehicles"][target]["ecu_assignments"].append(dict(assignment))
    assign_records(index, ids, target)
    index["vehicles"][source]["merged_into"] = target
    index["vehicles"][target]["history"].append(
        {
            "at": now(),
            "action": "merge",
            "source": source,
            "source_profile": index["vehicles"][source]["profile"],
        }
    )
