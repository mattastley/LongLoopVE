# LongLoopVE development

- Target Linux with Python 3.11–3.14. Install using `uv sync --locked`.
- Use `.agents/skills/aim-xrk-ingest/SKILL.md` for log-ingestion requests.
- Preserve decoder-returned channel timelines, values, and units. Keep alignment,
  engine mappings, and VE calculation separate from ingestion.
- Never claim PDM8/32 validation without representative logs and Race Studio
  comparisons. Record verification evidence and decoder/firmware versions.
- Keep raw logs, exported telemetry, and generated datasets out of Git.
- Before completing code changes, run `uv run --locked ruff check .`,
  `uv run --locked ruff format --check .`, and `uv run --locked pytest`.
- Use `work/` for local experiments. Output directories are generated artifacts.
