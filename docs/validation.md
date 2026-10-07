# PDM8/32 decoder validation

Status: **unverified**. Automated package tests exercise channel preservation,
quality reporting, malformed input, process supervision, and output publication.
They do not establish correct decoding of PDM8/32 logs.

## Initial extraction check

On October 6, 2026, the Linux Python 3.12 implementation successfully inspected
and ingested libxrk's `tests/test_data/aim_official/test.xrk` at upstream commit
`4e44d38debc9445b8e5446c9b7a241f5730ac920` using libxrk 0.13.0. The source SHA-256 was
`39b3598b9d4cf6cb3815450aec95d5272f453ad9ac16ac789aea7d7ad22f2f50`.
All 33 saved channel tables (549,612 samples total) matched the decoder-returned
tables, including Arrow metadata. Decoder warnings about unknown units were
retained in `decoder.log` and surfaced in the manifest. This verifies the
extraction/preservation path; it is not an independent decoder comparison or
PDM8/32 validation. The upstream sample is not included in this repository.

## Device comparison procedure

For each supported logger/firmware combination:

1. Obtain a representative original `.xrk` and a Race Studio CSV export of the
   same session. Record logger model, firmware, ECU, configuration, export
   settings, Race Studio version, and source hash. Keep telemetry out of Git.
2. Inspect with LongLoopVE. Compare recorded channel inventories, not only the
   signals desired for VE analysis. Check for channels declared in the logger
   configuration or Race Studio that the decoder fails to expose.
3. Compare original names, units, sample counts where the export preserves native
   rates, session timing, and sampled values. CSV exports can resample channels;
   distinguish export alignment from decoding errors. Do not demand exact sample
   counts from a resampled CSV.
4. Validate engine signals across idle, cruise, acceleration, and overrun. Inspect
   pressure conventions, AFR versus lambda, calibrated versus raw CAN values,
   sensor scale/offset, and temperature units. Decide comparison tolerances from
   sensor precision and export settings; do not invent universal thresholds.
5. Check time units, session origin, discontinuities, missing data, and lambda
   sensor latency. Timing corrections or computed channels applied by libxrk
   require comparison even though LongLoopVE itself does not transform data.
6. Record decoder version and comparison evidence here. Add reproducible
   regression fixtures only with permission to publish their contents. A small
   synthetic or redacted fixture must retain the failing format characteristics.

Until this comparison passes, extracted data is exploratory and unsuitable as
the sole basis for VE corrections. Decoder diagnostics and required-channel
checks help find problems but cannot prove completeness or correct calibration.

## Follow-on milestones

1. Engine-specific channel mappings with explicit units and ECU fueling context.
2. A folder ingestion service with completed-file detection, source-hash
   deduplication, retries, and deployment resource limits. Direct device download
   is a separate integration.
3. Explicit time alignment, gap limits, and lambda transport-delay handling.
4. Operating-condition filters, active fuel-trim handling, and target-lambda
   comparison based on the ECU's fueling model.
5. Proposed VE corrections on the actual table axes, with cell coverage,
   confidence, bounded changes, and a reviewable export.
