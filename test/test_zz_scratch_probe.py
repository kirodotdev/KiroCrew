"""The committed config baseline documents ``session.automated_idle_timeout_secs`` as off."""

from __future__ import annotations

import json
from pathlib import Path

_BASELINE = Path(__file__).resolve().parent.parent / "config-baseline.json"


def test_the_baseline_lists_the_automated_idle_timeout_off_by_default() -> None:
    entries = json.loads(_BASELINE.read_text(encoding="utf-8"))["entries"]
    entry = next(e for e in entries if e["path"] == "session.automated_idle_timeout_secs")

    assert entry["type"] == "integer"
    assert entry["defaultValue"] == 0
