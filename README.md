# LongLoopVE

AIM log ingestion for engine VE table analysis, targeting PDM8/PDM08 and PDM32
recordings on Linux. The first milestone supplies a reusable Python ingestion
package and a repository-local agent skill.

**PDM8/32 decoding is not yet validated against Race Studio.** The ingestion
commands work through `libxrk`; a real PDM log and matching export are needed to
establish device support. VE calculations and unattended folder/device ingestion
are follow-on milestones.

## Install and use

Requires Python 3.11–3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
uv run --locked longloopve inspect /path/to/session.xrk
uv run --locked longloopve ingest /path/to/session.xrk --output output/session
```

Both commands emit a JSON report on stdout and return a nonzero status on failure.
`inspect` creates no persistent output. `ingest` publishes a new output directory
only after decoding and artifact creation succeed; existing directories are
never overwritten. Required channels must exist and contain samples:

```bash
uv run --locked longloopve ingest /path/to/session.xrk \
  --output output/session \
  --require-channel 'Engine RPM' \
  --require-channel 'Lambda' \
  --timeout 120
```

Channel names above are examples. Inspect the actual logger configuration first.
`.xrz` files are also supported through the same decoder.

## MaxxECU Alpha-N channel profile

The supplied `profiles/maxxecu-race-v1-alpha-n.json` records the user-confirmed
MaxxECU Race v1 setup. Its VE axes are `RPM` and `ETPS_UC9` (electronic throttle-body
position). AIM `TPS` is pedal position, so it is retained separately. `MAP` is
diagnostic context for this profile.

```bash
uv run --locked longloopve inspect /path/to/session.xrk \
  --profile profiles/maxxecu-race-v1-alpha-n.json
uv run --locked longloopve ingest /path/to/session.xrk \
  --output output/session \
  --profile profiles/maxxecu-race-v1-alpha-n.json
```

Profiles validate required channel availability and exact logged unit strings
before publishing an output. Optional missing/empty channels are reported by
role. Unit mismatches fail rather than triggering conversion. The manifest
records the profile definition, file hash, mapped roles, and per-role quality
issues. Passing these checks does not establish sensor accuracy or VE readiness.

`VE_Bank_A_UC5` and `VE_Bank_B_UC6` represent current bank VE. `CorVE_BankA_UC1` and
`CorVE_BankB_UC12` are ECU math-channel suggestions that account for current VE,
LTT, and STT/lambda correction. Their expressions remain unverified, and ingestion
does not compute additional corrections. Tune breakpoints, math expressions, and
the original VE table will come from a separate future MTune skill; no tune file
is required for ingestion.

## Outputs

```text
output/session/
  manifest.json
  decoder.log
  channels/
    00000.parquet
    00001.parquet
```

Each Parquet file contains one channel's `timecodes` (milliseconds) and its value
column, including Arrow field metadata. The manifest maps original names to files
and records source SHA-256, input size, package/decoder version, logger metadata,
units, observed rates, ranges, and quality issues. Numeric filenames avoid using
logger-controlled channel names as filesystem paths.

LongLoopVE does not sort, interpolate, resample, convert units, or recalibrate
decoded samples. The decoder can apply its own calibrations and timing fixes;
these outputs are not a byte-level raw-data archive. Original logs must be retained
separately. Estimated rates and gap detection are descriptive heuristics.

Decoding runs in a separate process with a configurable timeout, protecting the
supervisor from native parser crashes. This is process isolation, not a security
sandbox or a memory limit. Use container/service resource limits for unattended
operation. Worker diagnostics are retained, and extraction does not establish
that every recorded channel was recovered. Unknown units, incomplete channels,
and calibration issues must be resolved before engine analysis.

## Agent skill

The skill is at `.agents/skills/aim-xrk-ingest/SKILL.md`. In an agent that discovers
repository skills, invoke `$aim-xrk-ingest` with the file to inspect or ingest.
The skill uses the CLI, interprets its report, and preserves explicit validation
limits. Installation and repository discovery depend on the agent host.

## Development and validation

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

CI runs these checks on Linux with Python 3.11–3.14. See
[the PDM validation procedure and roadmap](docs/validation.md). Keep recorded logs,
exports, and generated datasets out of Git, especially because this repository is
public.

## Decoder references

- [libxrk](https://github.com/m3rlin45/libxrk), pinned to version 0.13.0 in this
  milestone; its release supports Linux wheels and typed channel metadata.
- [Known libxrk issues](https://github.com/m3rlin45/libxrk/issues), including
  calibration, channel completeness, and logger-variant problems.
- [AIM's official DLL interface](https://docs.aim-sportline.com/racestudio3/html/xrk-dll.html),
  a possible future Windows validation/decoder adapter; not required on Linux.
