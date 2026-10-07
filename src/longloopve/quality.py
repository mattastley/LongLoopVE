"""Describe decoded channels without repairing, sorting, or resampling them."""

import numpy as np
import pyarrow as pa


def summarize_channel(name: str, table: pa.Table) -> dict:
    if table.column_names != ["timecodes", name]:
        raise ValueError(f"Unexpected channel schema for {name!r}: {table.column_names}")
    times_column = table["timecodes"]
    if not pa.types.is_integer(times_column.type):
        raise ValueError(f"Expected integer millisecond timecodes for {name!r}")
    times = times_column.to_numpy(zero_copy_only=False).astype(np.float64)
    finite_times = times[np.isfinite(times)]
    deltas = np.diff(times)
    positive_deltas = deltas[np.isfinite(deltas) & (deltas > 0)]
    period = float(np.median(positive_deltas)) if len(positive_deltas) else None
    values = table[name]
    numeric = pa.types.is_integer(values.type) or pa.types.is_floating(values.type)
    non_finite = 0
    value_range = None
    if numeric:
        data = values.to_numpy(zero_copy_only=False)
        valid = np.isfinite(data)
        non_finite = int(np.count_nonzero(~valid)) - values.null_count
        if np.any(valid):
            value_range = [float(np.min(data[valid])), float(np.max(data[valid]))]
    metadata = table.schema.field(name).metadata or {}
    units = metadata.get(b"units", b"").decode("utf-8", errors="replace")
    issues = []
    if not table.num_rows:
        issues.append("empty_channel")
    if not units:
        issues.append("units_absent_or_dimensionless")
    if times_column.null_count:
        issues.append("null_timestamps")
    reversed_count = int(np.count_nonzero(deltas < 0))
    duplicate_count = len(finite_times) - len(np.unique(finite_times))
    if reversed_count:
        issues.append("timestamps_out_of_order")
    if duplicate_count:
        issues.append("duplicate_timestamps")
    gap_count = int(np.count_nonzero(deltas > 5 * period)) if period else 0
    if gap_count:
        issues.append("gaps_over_five_median_intervals")
    if values.null_count:
        issues.append("null_values")
    if non_finite:
        issues.append("non_finite_values")
    return {
        "name": name,
        "samples": table.num_rows,
        "value_type": str(values.type),
        "units": units,
        "time_range_ms": (
            [float(np.min(finite_times)), float(np.max(finite_times))]
            if len(finite_times)
            else None
        ),
        "median_interval_ms": period,
        "estimated_rate_hz": 1000 / period if period else None,
        "null_timestamps": times_column.null_count,
        "out_of_order_intervals": reversed_count,
        "duplicate_timestamps": duplicate_count,
        "large_gaps": gap_count,
        "null_values": values.null_count,
        "non_finite_values": non_finite,
        "value_range": value_range,
        "issues": issues,
    }
