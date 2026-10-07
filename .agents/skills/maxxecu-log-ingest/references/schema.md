# Catalog schema version 1

JSON is UTF-8. Library asset links are relative to the catalog root. Provenance includes original absolute input locations, but retrieval never depends on them. CLI structured output is JSON; progress goes to stderr. Exit 0 means success/deduplicated; 2 means batch completion with incomplete inputs; 1 means command-level failure. Shell wrappers may map a nonzero native exit to their own failure code.

## Storage and authority

```text
catalog.json                         Authoritative index, vehicles and assignments
sources/<sha256>/original.bin         Byte-identical original input
records/<record-id>/manifest.json     Immutable extraction facts
records/<record-id>/summary.md        Compact factual summary
records/<record-id>/data.csv          Complete decoded log
tunes/<sha256>/original.bin           Byte-identical tune member
tunes/<sha256>/tune.json              XML structure and setting/table views
.writer.lock                         Present only during a write
```

`catalog.json` has `schema_version`, `parser_version`, and dictionaries `sources`, `records`, `tunes`, `vehicles`. Read it once for a consistent assignment snapshot. Atomic replacement commits changes. A failed write may leave unreferenced assets; they are not committed records. Do not treat directory enumeration as the index.

Source IDs are SHA-256 of original bytes. Record IDs hash the source ID plus NUL plus member name. Tune IDs hash tune bytes. Duplicate imports add provenance locations without duplicating recordings or resetting corrections. Equal filenames, dates, or similar settings do not imply equal recordings.

## Recording manifests

Fields include `id`, `type`, `source_id`, `source_name`, `member_sha256`, `source_original`, `schema_version`, `parser_version`, `status`, `warnings`, `software_versions`, `tune_ids`, `tune_association`, `identity_evidence`, `configuration_facts`, and optional `log`.

- `status`: `complete`, `partial`, `corrupt`, `protected`, or `unsupported`. Complete means successful extraction of recognized data; it does not assert known units, date, or capture timing. Check warnings and explicit unknown fields.
- `type`: `log`, `tune`, or `unavailable`.
- `tune_association.basis`: `embedded_package`, `user_supplied`, `standalone`, or `ambiguous_package`. Multiple candidate tunes/logs retain links but flag ambiguity.
- `tune_association.capture_timing`: `recording`, `download`, or `unknown`; explicit classifications carry `timing_basis=user_supplied`.
- `log`: `format`, `row_count`, `channels`, `timing`, `csv`, `status`, and `warnings`.
- `show` combines facts with `assignment` and `assignment_history` from the current index. On-disk manifests omit potentially stale current assignments.

Each channel has `key` (`c0`, `c1`, ... in source order), nullable `channel_id`, `source_name`, `label`, nullable `units`, `units_source`, nullable `binary_scale_float32`, and `statistics`. Duplicate IDs/names remain separate columns. Statistics contain finite count, missing/nonnumeric count, minimum and maximum. Sensor sentinel values are preserved, not diagnosed or filtered.

CSV starts with zero-based `sample_index`, `elapsed_seconds` (blank if unavailable), then `cN` columns. Text-log strings retain their numerical spelling and NaN tokens. Binary values use verified scale; original integers remain in the retained source. Missing values are not substituted with zero.

Timing includes source metadata, `created_at` (UTC or null), its status, `sample_interval_seconds`, `elapsed_source`, `timestamp_discontinuities`, and `duration_seconds`. A null timestamp does not mean 1970. `elapsed_source` is `accumulated_logged_intervals_ms`, `derived_sample_interval`, or `unknown`. See formats.md for the interval convention.

## Tunes

`file_info` retains software/firmware/hardware fields as strings. `settings` preserves duplicate names, attributes and exact `source_value`. `tables` preserves attributes, axes and all data pages; page values are flat source-order strings with dimensions. No assumed transposition or blanket scaling. `xml` retains element structure, attributes, text and tails, including unknown elements. Original bytes also retain comments and formatting. `configuration_facts` is a convenience view of serialized engine fields and notes, not verified physical-build specifications.

## Vehicles

Vehicle IDs are UUID-derived, not serial-derived. Profiles contain nickname, VIN, year, make, model, engine notes, and modification notes. `provisional` identifies inferred cars. `vin_evidence`, dated `ecu_assignments`, and `history` retain evidence and edits.

Record assignment fields are nullable `vehicle_id`, `reason`, `confidence` (`confirmed`, `high`, `medium`, `uncertain`, `unknown`) and `review_required`. Conflicting identifiers include candidates and stay unassigned. Serial-only grouping is medium confidence. Unidentified standalone CSVs remain unassigned. Explicit reassignment and merges preserve previous assignments. Tune-derived facts stay with recordings.

## Schema changes

Version 1 readers may ignore added fields. The writer refuses unsupported schema versions. Breaking migrations must produce a new library or an explicit backed-up migration; do not reinterpret existing fields in place.
