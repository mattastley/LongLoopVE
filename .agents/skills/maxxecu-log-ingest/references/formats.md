# Supported formats and interpretation

## Content detection

Extensions guide directory discovery only. Explicit files and members are identified from content. `PK` means a ZIP candidate; an invalid directory is a corrupt archive, not text. LZ4 frames start with `04 22 4d 18`. Tune XML uses `MaxxECUSettingsFile`. Text headers carry bracketed channel IDs.

The parser accepts ZIP-packaged and standalone `.MaxxECU-log` / `.maxxlog`, MTune CSV, tab-delimited logs, and `.MaxxECU-save` XML. Members are read without extracting their names onto disk. Traversal/absolute paths, duplicate names, more than 4096 entries, individual payloads over 512 MiB, and total expanded members over 1 GiB are rejected, never silently truncated.

## Text logs

Channels occupy the first row. Tab or comma separation is detected there. A consistently empty final column caused by MTune's terminal delimiter is removed; populated extra values or missing columns are errors. UTF-8, UTF-8 BOM, UTF-16 BOM/recognizable XML byte patterns, and Windows-1252 are supported. Arbitrary third-party CSV layouts and locale-specific decimal/delimiter conventions are not implicitly guessed.

## Binary logs

Observed LZ4 payload layout, little-endian:

1. Signed 32-bit channel count and sample count.
2. For each channel: .NET 7-bit length-prefixed UTF-8 name, then float32 resolution.
3. Signed 16-bit samples, channel-major: all samples of channel one, then channel two, etc.

Counts, positive finite scales, string lengths, and exact payload length are checked. Float32 resolutions are recovered to seven significant decimal digits and multiplied by raw integers; CSV values use twelve significant digits. Original float32 scales remain separately available. In the paired supplied recording, all 7,784,100 resulting values agree exactly with MTune CSV. Unknown future layouts are rejected.

## Units and elapsed time

Do not apply firmware-memory scales to already scaled text or tune XML. General units remain null unless verified; familiar names and installed display preferences do not establish source units. Pre-1.146 imperial logs and localized CSV values are preserved without automatic conversion.

The installed realtime definition identifies `Log Timestamp [498]` in **milliseconds**. In supplied recordings these are per-sample intervals, not absolute time. Elapsed time accumulates those intervals, with sample zero defined as zero and its pre-record interval excluded. A missing/negative interval makes subsequent elapsed time unknown; no wrap correction or interpolation is applied. Without channel 498, positive `LogRate` supplies seconds/sample and is labeled derived. CSVs lacking both retain sample indexing unless an explicit interval is supplied.

`CreatedTimestamp=0` and `2` occur in real packages and are placeholders. Only plausible epoch timestamps become UTC dates. File modification times, tune edit dates, filename dates, and local timezone are not substituted.

## Tune XML

Some samples declare UTF-16 but contain UTF-8. Decode from BOM/bytes, retain originals, and record the mismatch. DTDs/entities are forbidden. Settings, unknown elements, axis source IDs, stepped axes, attributes, and all 4D pages are retained. Serialized scalars and tables may follow different conventions; source strings are not assumed engineering units.

Recognizable protected content is flagged; unknown proprietary payloads remain unsupported. No password bypass or decryption. A valid log may remain usable when its tune cannot be decoded.

## Associations

An archive associates its log and tune, but does not prove they describe identical recording-time ECU state. MaxxECU documents capture at log start for PC logs and after download for internal logs. Timing remains unknown unless supplied by the acquisition workflow. Multiple logs/tunes remain candidates and are flagged.

Sources: [MaxxECU file formats](https://www.maxxecu.com/webhelp/mtune-file_formats.html), [release history](https://www.maxxecu.com/mtune), installed `ecuRealtimeDataDefinitions.xml`, and supplied samples. Application release, serialized Softwareversion, and ECU firmware version are separate facts.
