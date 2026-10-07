"""Explicit engine-channel mappings; profiles do not transform logged values."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ChannelProfile:
    definition: dict
    filename: str
    sha256: str

    @property
    def required_channels(self) -> list[str]:
        return sorted({c["channel"] for c in self.definition["channels"].values() if c["required"]})

    def apply(self, manifest: dict) -> dict:
        available = {c["name"]: c for c in manifest["channels"]}
        mapped = {}
        for role, specification in self.definition["channels"].items():
            name = specification["channel"]
            channel = available.get(name)
            if channel is None or not channel["samples"]:
                status = "missing" if channel is None else "empty"
                if specification["required"]:
                    raise ValueError(f"Profile role {role!r}: channel {name!r} is {status}")
                mapped[role] = {"channel": name, "status": status}
                continue
            if channel["units"] != specification["units"]:
                raise ValueError(
                    f"Profile role {role!r}: channel {name!r} has units "
                    f"{channel['units']!r}, expected {specification['units']!r}"
                )
            mapped[role] = {
                "channel": name,
                "status": "present",
                "units": channel["units"],
                "issues": channel["issues"],
            }
        return {
            "definition": self.definition,
            "source": {"filename": self.filename, "sha256": self.sha256},
            "mapped_roles": mapped,
            "validation": "channel_presence_and_units_only",
        }


def load_profile(path: Path) -> ChannelProfile:
    raw = path.read_bytes()
    profile = json.loads(raw)
    if not isinstance(profile, dict) or profile.get("schema_version") != 1:
        raise ValueError("Engine profile must be an object with schema_version 1")
    for field in ("name", "ecu", "fueling_model"):
        if not isinstance(profile.get(field), str) or not profile[field].strip():
            raise ValueError(f"Engine profile requires a nonempty {field!r}")
    channels = profile.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise ValueError("Engine profile requires channel-role mappings")
    for role, channel in channels.items():
        if not role.strip() or not isinstance(channel, dict):
            raise ValueError("Engine profile channel roles must be named objects")
        if not isinstance(channel.get("channel"), str) or not channel["channel"].strip():
            raise ValueError(f"Engine profile role {role!r} requires an exact channel name")
        if not isinstance(channel.get("units"), str):
            raise ValueError(f"Engine profile role {role!r} requires explicit units")
        if not isinstance(channel.get("required"), bool):
            raise ValueError(f"Engine profile role {role!r} requires a boolean required flag")
    axes = profile.get("ve_axes")
    if (
        not isinstance(axes, list)
        or len(axes) != 2
        or any(not isinstance(axis, str) for axis in axes)
        or len(set(axes)) != 2
        or any(axis not in channels or not channels[axis]["required"] for axis in axes)
    ):
        raise ValueError("Engine profile ve_axes must reference two distinct required roles")
    if profile["fueling_model"] == "alpha_n" and set(axes) != {
        "engine_speed",
        "throttle_body_position",
    }:
        raise ValueError("Alpha-N VE axes must be engine_speed and throttle_body_position")
    return ChannelProfile(profile, path.name, hashlib.sha256(raw).hexdigest())
