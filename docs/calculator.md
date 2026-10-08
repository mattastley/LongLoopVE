# Long Loop VE calculator

The calculator runs locally through the `longloopve` CLI. It retains XRK evidence
and calibrations per physical car, reconstructs Bank A/B cell values from logged
Corrected VE using ECU interpolation equations, and produces an Excel workbook,
a PNG change report, and a separate updated MaxxECU tune for human review.
It never connects to or updates an ECU.

## Install and configure

```bash
uv sync --locked
uv run --locked longloopve inspect /path/to/session.xrk
uv run --locked longloopve tune-inspect /path/to/base.MaxxECU-save
```

Copy `profiles/ve-analysis.example.json`, `profiles/tune-layout.example.json`, and
(optionally) `profiles/calculation-settings.example.json` to your own configuration
location. These are **setup templates**, not verified mappings for every car.
Replace placeholder channels/table names and verify actual units and inactive
encodings. The original `maxxecu-race-v1-alpha-n.json` remains an ingestion-only
profile; its optional filter channels do not meet the calculator's requirements.

The analysis profile maps exact logged channel names and units to roles. Required
roles are engine speed, actual throttle-body position, coolant and intake-air
temperature, accel enrichment, decel/fuel/ignition cut, knock retard, and both banks'
Corrected VE, current VE, LTT and lambda correction. A required channel absent
from the log is an error. An empty channel is present but supplies no usable data.
Common missing/stale data rejects affected observations in both banks. An empty,
nonfinite or unusable Corrected VE timeline affects its bank independently.
Unusable supporting VE/trim data produces sanity warnings; finite Corrected VE
remains authoritative. Units must match the profile exactly; temperature conversion
happens only in analysis, preserving ingested samples.

ETPS is the throttle-body axis. AIM TPS/pedal position must never be mapped there.
The supplied decoder exposes AIM Name/Car as `Driver`/`Vehicle`; the template maps
those explicitly. `identity_fields` can map dotted metadata paths for other
configurations. Without explicit paths, the CLI recognizes Name/Driver and
Car/Vehicle aliases, rejecting conflicting aliases. Identity values remain
case-sensitive and are never inferred from filenames.

Tune mappings explicitly name each bank table and its RPM/ETPS axes. Choose
`rpm_fast` when serialized cells traverse RPM first within each ETPS row, or
`etps_fast` for the opposite order. Scales convert serialized numbers to rpm,
ETPS percent, and VE percent. Record the evidence for these choices in
`layout_evidence`; supplying a mapping is an explicit assertion of that layout,
not independent hardware validation. Only ascending, two-axis, single-page tables
on a shared grid and bilinear interpolation clamped to axis endpoints are supported.
Other strategies, protected tunes, and ambiguous tables fail explicitly. Confirm
ECU interpolation and source axis identifiers against MTune before using results.

Optionally configure correction sanity checks after confirming the tune math:

```json
"sanity": {
  "factors": [
    {"role": "long_term_trim_bank_{bank}", "convention": "offset_percent"},
    {"role": "lambda_correction_bank_{bank}", "convention": "multiplier_percent"}
  ]
}
```

Here `offset_percent` means `1 + value/100`; `multiplier_percent` means
`value/100`. This is an example, not an assertion of your ECU's expressions.
Factors can refer to required mapped percent channels. The check compares current
VE times these factors with Corrected VE. It warns without rejecting samples or
applying trims to Corrected VE again. Unknown math is reported as unverified.

## Car and log workflow

Use one library writer at a time. Pass `--library` **before** the subcommand. The
default is `data/longloopve` under the current directory; a portable library outside
the repository is recommended. Raw logs, tunes, catalogs and exports stay out of Git.

```bash
uv run --locked longloopve --library /path/to/library car-add \
  --name 'XRK Name' --identifier 'XRK Car identifier' --nickname 'S54 track car' \
  --tune /path/to/base.MaxxECU-save \
  --mapping /path/to/tune-layout.json --profile /path/to/ve-analysis.json

uv run --locked longloopve --library /path/to/library cars
uv run --locked longloopve --library /path/to/library log-add \
  /path/to/first.xrk /path/to/second.xrk
```

`car-add` returns a stable car ID and initial calibration ID. Logs matching a
registered Name/Car import automatically. Otherwise an interactive terminal prompts
for an existing car or new setup; automation must supply `--car CAR_ID` explicitly.
Explicit selection confirms an additional identity alias, but cannot silently take
an identity already belonging to another car.

Logs are deduplicated by source SHA-256. Reimport preserves assignment and does not
add observations. Use `log-assign` to change an existing assignment. Multi-file
imports continue after individual failures, report each error, and exit with status
2 for partial completion; command/single-file failures exit 1. Successful commands
exit 0. Stdout is JSON and prompts/errors use stderr.

```bash
uv run --locked longloopve --library /path/to/library calculate --car CAR_ID \
  --settings /path/to/settings.json --min-observations 20

uv run --locked longloopve --library /path/to/library export --car CAR_ID \
  --settings /path/to/settings.json --min-observations 20 \
  --output /path/to/new-review-directory
```

Each calculation reprocesses retained decoded data using the selected settings;
changing filters does not require decoding logs again. Thresholds are strict
CLT >165°F and inclusive IAT ≤130°F. Enrichment/cut/knock roles must equal their
configured inactive values. The default minimum observations is 1; no percentage
change limit is applied. `color_scale` affects only report colors.

Corrected VE timestamps define each bank's samples. RPM/ETPS default to linear
alignment between surrounding source samples; filters and diagnostic channels
default to holding the latest source sample. Each role may explicitly select
`alignment: "linear"` or `"hold"`. The freshness default is 100 ms. Linear alignment
requires both endpoints within that limit; holding never uses future values.
There is no extrapolation for linear alignment. Nonfinite samples, stale values,
and unusable timelines are reported, not repaired or filled with zeros. Negative
RPM and ETPS outside 0–100% are rejected. Valid operating points outside the table
breakpoints use endpoint clamping.

## Tunes and calibration boundaries

```bash
# Update reporting/export base while retaining observations and reconstruction reference.
uv run --locked longloopve --library /path/to/library tune-add /path/to/new.MaxxECU-save \
  --car CAR_ID --impact preserve

# For VANOS or other VE-impacting changes: archive history and start fresh.
uv run --locked longloopve --library /path/to/library tune-add /path/to/new.MaxxECU-save \
  --car CAR_ID --impact reset
```

The most recently **provided** tune is the comparison/export base, regardless of
its file date. A reset creates a new active calibration and retains the old one
for inspection with `calculate/export --epoch EPOCH_ID`. Logs are not reassigned
by guessed timestamps. To import or explicitly move an older log:

```bash
uv run --locked longloopve --library /path/to/library log-add /path/to/old.xrk \
  --car CAR_ID --epoch EPOCH_ID
uv run --locked longloopve --library /path/to/library log-assign LOG_SHA256 \
  --car CAR_ID --epoch EPOCH_ID
```

If the actual recording-time tune is known, supply its retained hash through
`--recording-tune TUNE_HASH` on `log-add` or `log-assign`. This adds a diagnostic
comparison between interpolated tune VE and logged current VE. Unknown recording
tunes are reported; the latest base is never assumed to have applied historically.
Changing RPM/ETPS breakpoints is unsupported, including on a reset.

## Reconstruction and review artifacts

Each accepted sample provides one equal-weight equation `sum(weight * cell VE) =
Corrected VE`. The sparse least-squares solver seeks the smallest residual across
all accepted samples. For an underdetermined system it chooses the smallest change
from the calibration's initial tune. That reference stays fixed across preserve
updates. There is no smoothing, recency weighting, duration weighting, or change cap.

An observation counts for a cell whenever its interpolation weight is positive,
however small. Exact breakpoint samples do not count for zero-weight neighbors.
Per-bank counts, weight sums and contributing-log counts are separate. Count alone
does not establish identifiability; rank/conditioning warnings and residuals identify
limitations. Rank is computed on supported cells up to 512 cells; larger supported
grids report rank as unknown rather than claiming full determination.

The new export directory contains:

- `ve-tables.xlsx`: reconstructed estimates, final export VE, latest base VE,
  changes in percent, counts, weight sums, contributing logs, eligibility, filter
  reasons, solver diagnostics, warnings and log provenance for each bank.
- `ve-change.png`: A/B percentage changes vs latest base; separate observation
  counts where banks differ. Gray is no data/unavailable change; hatching identifies
  observed cells below the selected minimum.
- `corrected.MaxxECU-save`: a copy of the latest supplied tune with eligible cells
  replaced. Insufficient cells retain latest base values. Only changed numeric cell
  text is edited; other text, comments, formatting and encoding are retained. The
  output is reparsed and compared against requested values and unrelated XML data.
- `calculation.json`: complete settings, mappings, hashes, per-log rejection
  summaries and calculation results for reproduction.

Zero base VE has no defined percentage change and is flagged as unavailable rather
than divided through. Nonfinite/negative base values and malformed grids are rejected.
Nonpositive reconstructed values and convergence/conditioning issues produce warnings
for human review. Reconstructed values, including negative values, are preserved
without clipping; such a tune may be unsuitable for ECU use and requires review.
Both reconstruction and final export interpolation RMSE are shown:
retaining insufficient cells can change the fit of the exported table.

Existing output directories/files are never overwritten. Artifact bundles publish
only after every output succeeds. Source snapshots remain immutable; atomic index
updates and a PID writer lock protect assignments. If `.writer.lock` remains after
a process crash, confirm its PID has stopped before removing that lock alone.
Unreferenced retained assets after a failed transaction are not assigned observations.

## Validation status

Automated tests use synthetic known-answer tables and mocked decoded channels to
verify the math and complete workflow. They cover filtering, independent banks,
duplicate imports, preserve/reset semantics, tune text edits in multiple encodings,
report generation and failures. They do **not** establish PDM8/32 decoding accuracy,
actual MaxxECU interpolation, source tune units, or universal MTune version support.

The existing ingest backend has historical observations for serialized MTune
1.156–1.161; tune **writing** currently has synthetic round-trip validation. Before
using a car's output, compare representative XRKs with Race Studio and open the
exported tune in the matching MTune version, checking axes, both VE tables, math
expressions and unrelated settings. Record evidence and versions as described in
`docs/validation.md`. No real XRKs or tunes were supplied for this implementation.
