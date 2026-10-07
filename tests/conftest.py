import os
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / ".agents"
        / "skills"
        / os.environ.get("MAXXECU_TEST_SKILL", "maxxecu-log-ingest")
        / "scripts"
    ),
)
