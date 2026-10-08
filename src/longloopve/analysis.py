"""Channel alignment, eligibility, and equal-sample sparse VE reconstruction."""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import lsqr

COMMON = (
    "engine_speed",
    "throttle_body_position",
    "acceleration_enrichment",
    "coolant_temperature",
    "intake_air_temperature",
    "decel_cut",
    "fuel_cut",
    "ignition_cut",
    "knock_retard",
)
BANK_ROLES = (
    "suggested_ve_bank_{bank}",
    "current_ve_bank_{bank}",
    "long_term_trim_bank_{bank}",
    "lambda_correction_bank_{bank}",
)
FILTERS = ("acceleration_enrichment", "decel_cut", "fuel_cut", "ignition_cut", "knock_retard")
DEFAULTS = {
    "clt_min_f": 165.0,
    "iat_max_f": 130.0,
    "freshness_ms": 100.0,
    "sanity_relative_tolerance": 0.05,
    "min_observations": 1,
    "color_scale": 45.0,
}


def settings(overrides=None):
    result = DEFAULTS | (overrides or {})
    for key, value in result.items():
        if key not in DEFAULTS or not isinstance(value, (int, float)) or not np.isfinite(value):
            raise ValueError(f"Unknown or non-finite setting: {key}")
    if result["freshness_ms"] <= 0 or result["color_scale"] <= 0:
        raise ValueError("Freshness and color scale must be positive")
    if result["sanity_relative_tolerance"] < 0:
        raise ValueError("Sanity tolerance must be nonnegative")
    minimum = result["min_observations"]
    if isinstance(minimum, bool) or int(minimum) != minimum or minimum < 1:
        raise ValueError("Minimum observations must be an integer >= 1")
    return result


def validate_profile(profile):
    roles = list(COMMON) + [r.format(bank=b) for b in ("a", "b") for r in BANK_ROLES]
    missing = set(roles) - set(profile.get("channels", {}))
    if missing:
        raise ValueError(f"Analysis profile missing roles: {sorted(missing)}")
    for role in roles:
        spec = profile["channels"][role]
        if not isinstance(spec.get("channel"), str) or not spec["channel"]:
            raise ValueError(f"Role {role} requires a channel name")
        if not isinstance(spec.get("units"), str):
            raise ValueError(f"Role {role} requires explicit logged units")
        if spec.get("alignment", "linear" if role in COMMON[:2] else "hold") not in {
            "linear",
            "hold",
        }:
            raise ValueError(f"Unknown alignment for {role}")
        if role in FILTERS and ("inactive" not in spec or not np.isfinite(spec["inactive"])):
            raise ValueError(f"Role {role} requires a finite inactive value")
    for role in ("coolant_temperature", "intake_air_temperature"):
        if profile["channels"][role]["units"] not in {"C", "F", "°C", "°F"}:
            raise ValueError(f"Unverified temperature unit for {role}")
    if profile["channels"]["engine_speed"]["units"] != "rpm":
        raise ValueError("Engine speed must be logged in rpm")
    for role in (
        "throttle_body_position",
        "suggested_ve_bank_a",
        "suggested_ve_bank_b",
        "current_ve_bank_a",
        "current_ve_bank_b",
    ):
        if profile["channels"][role]["units"] != "%":
            raise ValueError(f"Role {role} must be logged in percent")
    factors = profile.get("sanity", {}).get("factors", [])
    for factor in factors:
        if factor.get("convention") not in {"offset_percent", "multiplier_percent"}:
            raise ValueError("Sanity factors require an explicit percent convention")
        for bank in ("a", "b"):
            role = factor["role"].format(bank=bank)
            if role not in roles or profile["channels"][role]["units"] != "%":
                raise ValueError(f"Sanity factor {role} must map a percent channel")
    return roles


@dataclass
class Channel:
    times: np.ndarray
    values: np.ndarray

    def __post_init__(self):
        self.times = np.asarray(self.times, dtype=float)
        self.values = np.asarray(self.values, dtype=float)
        if self.times.ndim != 1 or self.values.shape != self.times.shape:
            raise ValueError("Channel timestamps and values must be equal-length vectors")
        self.valid_timeline = bool(
            np.all(np.isfinite(self.times)) and not np.any(np.diff(self.times) <= 0)
        )

    def align(self, times, freshness, mode="hold"):
        output = np.full(len(times), np.nan)
        if not len(self.times) or not self.valid_timeline:
            return output
        left = np.searchsorted(self.times, times, side="right") - 1
        valid = left >= 0
        indexes = np.clip(left, 0, len(self.times) - 1)
        age = times - self.times[indexes]
        valid &= (age >= 0) & (age <= freshness)
        if mode == "hold":
            output[valid] = self.values[indexes[valid]]
        else:
            exact = valid & (age == 0)
            output[exact] = self.values[indexes[exact]]
            right = np.minimum(indexes + 1, len(self.times) - 1)
            valid &= ~exact & (right > indexes) & (self.times[right] - times <= freshness)
            ratio = np.zeros(len(times))
            ratio[valid] = age[valid] / (self.times[right[valid]] - self.times[indexes[valid]])
            output[valid] = (
                self.values[indexes[valid]] * (1 - ratio[valid])
                + self.values[right[valid]] * ratio[valid]
            )
        return output


def load_channels(directory: Path, manifest: dict, profile: dict):
    roles = validate_profile(profile)
    by_name = {c["name"]: c for c in manifest["channels"]}
    required = {profile["channels"][role]["channel"] for role in roles}
    missing = required - set(by_name)
    if missing:
        raise ValueError(f"Required channels absent from log: {sorted(missing)}")
    result = {}
    for role in roles:
        spec = profile["channels"][role]
        entry = by_name[spec["channel"]]
        if entry["units"] != spec["units"]:
            raise ValueError(f"Unit mismatch for {role}: {entry['units']!r} != {spec['units']!r}")
        path = (directory / entry["file"]).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("Channel path escapes ingestion directory")
        table = pq.read_table(path)
        if table.column_names != ["timecodes", spec["channel"]]:
            raise ValueError(f"Unexpected channel schema for {role}")
        result[role] = Channel(table["timecodes"].to_numpy(), table[spec["channel"]].to_numpy())
    return result


def observations(channels, profile, config):
    result = {}
    for bank in ("A", "B"):
        suffix = bank.lower()
        anchor = channels[f"suggested_ve_bank_{suffix}"]
        aligned = {}
        roles = list(COMMON) + [r.format(bank=suffix) for r in BANK_ROLES]
        for role in roles:
            spec = profile["channels"][role]
            aligned[role] = channels[role].align(
                anchor.times,
                config["freshness_ms"],
                spec.get("alignment", "linear" if role in COMMON[:2] else "hold"),
            )
        corrected = anchor.values
        reasons = {}
        reasons["invalid_corrected_timeline"] = np.full(len(corrected), not anchor.valid_timeline)
        for role in COMMON:
            reasons[f"missing_or_stale:{role}"] = ~np.isfinite(aligned[role])
        reasons["invalid_corrected_ve"] = ~np.isfinite(corrected) | (corrected <= 0)
        reasons["invalid_axis_value"] = (
            (aligned["engine_speed"] < 0)
            | (aligned["throttle_body_position"] < 0)
            | (aligned["throttle_body_position"] > 100)
        )
        for role in FILTERS:
            spec = profile["channels"][role]
            reasons[role] = np.isfinite(aligned[role]) & (aligned[role] != spec["inactive"])
        clt = aligned["coolant_temperature"]
        iat = aligned["intake_air_temperature"]
        if profile["channels"]["coolant_temperature"]["units"] in {"C", "°C"}:
            clt = clt * 9 / 5 + 32
        if profile["channels"]["intake_air_temperature"]["units"] in {"C", "°C"}:
            iat = iat * 9 / 5 + 32
        reasons["coolant_temperature"] = clt <= config["clt_min_f"]
        reasons["intake_air_temperature"] = iat > config["iat_max_f"]
        accepted = ~np.logical_or.reduce(list(reasons.values()))
        warnings = []
        for role in roles:
            if not channels[role].valid_timeline:
                warnings.append(f"Unusable timeline for {role}; timestamps were not repaired")
        current = aligned[f"current_ve_bank_{suffix}"]
        if np.any(accepted & (~np.isfinite(current) | (current <= 0))):
            warnings.append(
                "Logged current VE missing, stale, or nonpositive; sanity check limited"
            )
        factors = profile.get("sanity", {}).get("factors", [])
        if factors:
            expected = current.copy()
            for factor in factors:
                values = aligned[factor["role"].format(bank=suffix)]
                expected *= (
                    (1 + values / 100) if factor["convention"] == "offset_percent" else values / 100
                )
            available = accepted & np.isfinite(expected) & (expected > 0)
            mismatch = available & (
                np.abs(corrected - expected) > config["sanity_relative_tolerance"] * expected
            )
            if np.any(mismatch):
                warnings.append(
                    f"Corrected VE formula discrepancy in {int(mismatch.sum())} samples"
                )
            if np.any(accepted & ~available):
                warnings.append("Some accepted samples lack usable sanity-check factors")
        else:
            warnings.append(
                "Correction math unverified: configure sanity factors from tune expressions"
            )
        extreme = (
            accepted
            & np.isfinite(current)
            & (current > 0)
            & ((corrected > 2 * current) | (corrected < 0.5 * current))
        )
        if np.any(extreme):
            warnings.append(
                f"Corrected/current VE ratio outside 0.5–2 in {int(extreme.sum())} samples"
            )
        result[bank] = {
            "times": anchor.times[accepted],
            "rpm": aligned["engine_speed"][accepted],
            "etps": aligned["throttle_body_position"][accepted],
            "values": corrected[accepted],
            "current_ve": current[accepted],
            "summary": {
                "total": len(corrected),
                "accepted": int(accepted.sum()),
                "rejected": int((~accepted).sum()),
                "reasons": {k: int(v.sum()) for k, v in reasons.items()},
                "warnings": warnings,
            },
        }
    return result


def interpolation(rpm, etps, xs, ys):
    """Return four flattened cell indices/weights; exact nodes have zero neighbors."""
    x = np.clip(np.asarray(rpm), xs[0], xs[-1])
    y = np.clip(np.asarray(etps), ys[0], ys[-1])
    i = np.clip(np.searchsorted(xs, x, side="right") - 1, 0, len(xs) - 2)
    j = np.clip(np.searchsorted(ys, y, side="right") - 1, 0, len(ys) - 2)
    a = (x - xs[i]) / (xs[i + 1] - xs[i])
    b = (y - ys[j]) / (ys[j + 1] - ys[j])
    cells = np.column_stack(
        (j * len(xs) + i, j * len(xs) + i + 1, (j + 1) * len(xs) + i, (j + 1) * len(xs) + i + 1)
    )
    weights = np.column_stack(((1 - a) * (1 - b), a * (1 - b), (1 - a) * b, a * b))
    return cells, weights


def reconstruct(reference, latest, records, minimum=1):
    xs, ys = np.asarray(reference["rpm"]), np.asarray(reference["etps"])
    prior = np.asarray(reference["values"], dtype=float).ravel()
    base = np.asarray(latest["values"], dtype=float).ravel()
    n_cells = len(prior)
    cells_all, weights_all, targets = [], [], []
    log_counts = np.zeros(n_cells, dtype=int)
    filter_counts = Counter()
    warnings = []
    for record in records:
        cells, weights = interpolation(record["rpm"], record["etps"], xs, ys)
        cells_all.append(cells)
        weights_all.append(weights)
        targets.append(record["values"])
        log_counts[np.unique(cells[weights > 0])] += 1
        filter_counts.update(record["summary"]["reasons"])
        warnings.extend(record["summary"]["warnings"])
    cells = np.concatenate(cells_all) if cells_all else np.empty((0, 4), dtype=int)
    weights = np.concatenate(weights_all) if weights_all else np.empty((0, 4))
    target = np.concatenate(targets) if targets else np.empty(0)
    rows = np.broadcast_to(np.arange(len(target))[:, None], cells.shape)
    positive = weights > 0
    matrix = coo_matrix(
        (weights[positive], (rows[positive], cells[positive])), shape=(len(target), n_cells)
    ).tocsr()
    counts = np.bincount(cells[positive], minlength=n_cells)
    weight_sums = np.asarray(matrix.sum(axis=0)).ravel()
    fitted = prior.copy()
    condition = None
    rank = 0
    iterations = 0
    if len(target):
        # Starting at zero gives the minimum-norm delta among least-squares solutions.
        solution = lsqr(
            matrix,
            target - matrix @ prior,
            atol=1e-11,
            btol=1e-11,
            conlim=1e12,
            iter_lim=max(100, 10 * n_cells),
        )
        fitted += solution[0]
        iterations = solution[2]
        condition = float(solution[6])
        if solution[1] not in {0, 1, 2, 4, 5}:
            warnings.append(f"Solver stopped without convergence (status {solution[1]})")
        if condition > 1e8:
            warnings.append("Poorly conditioned reconstruction; review estimates carefully")
        supported = np.flatnonzero(counts)
        if len(supported) <= 512:
            eigen = np.linalg.eigvalsh((matrix[:, supported].T @ matrix[:, supported]).toarray())
            rank = int(np.count_nonzero(eigen > max(float(eigen[-1]) * 1e-12, 1e-14)))
            if rank < len(supported):
                warnings.append(
                    f"Underdetermined reconstruction: rank {rank}/{len(supported)} supported cells"
                )
        else:
            rank = None
            warnings.append("Rank not computed for more than 512 supported cells")
    else:
        warnings.append("No accepted observations; all exported cells retain latest base VE")
    eligible = counts >= minimum
    exported = np.where(eligible, fitted, base)
    if np.any(fitted <= 0):
        warnings.append("Reconstruction contains nonpositive VE values; human review required")
    change = np.full(n_cells, np.nan)
    np.divide(exported, base, out=change, where=base > 0)
    change = 100 * (change - 1)
    if np.any(base <= 0):
        warnings.append("Percentage change unavailable for zero/nonpositive base VE cells")
    shape = (len(ys), len(xs))

    def grid(values):
        return np.asarray(values).reshape(shape).tolist()

    return {
        "rpm": xs.tolist(),
        "etps": ys.tolist(),
        "estimate": grid(fitted),
        "exported": grid(exported),
        "base": grid(base),
        "change_percent": [
            [float(v) if np.isfinite(v) else None for v in row] for row in change.reshape(shape)
        ],
        "counts": grid(counts),
        "weight_sums": grid(weight_sums),
        "log_counts": grid(log_counts),
        "eligible": grid(eligible),
        "sample_count": len(target),
        "rank": rank,
        "condition_estimate": condition,
        "solver_iterations": iterations,
        "fit_rmse": float(np.sqrt(np.mean((matrix @ fitted - target) ** 2)))
        if len(target)
        else None,
        "export_rmse": float(np.sqrt(np.mean((matrix @ exported - target) ** 2)))
        if len(target)
        else None,
        "filter_rejections": dict(filter_counts),
        "warnings": sorted(set(warnings)),
    }
