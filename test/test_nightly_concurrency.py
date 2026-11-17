"""The nightly's concurrency: a manual dispatch must not cancel the scheduled run.

``nightly.yml`` keeps one run per ``nightly-build`` group. Only the scheduled run
may cancel the run in progress; a manual dispatch waits behind it. These tests
read the workflow file and evaluate the ``cancel-in-progress`` expression for
each trigger the workflow declares.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

NIGHTLY = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "nightly.yml"

# The one expression shape the workflow uses: `${{ github.event_name == '<event>' }}`.
_EVENT_EQUALS = re.compile(r"^\$\{\{\s*github\.event_name\s*==\s*'([a-z_]+)'\s*\}\}$")


def _workflow() -> dict[Any, Any]:
    doc = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _cancels_in_progress(value: object, event_name: str) -> bool:
    """Evaluate ``cancel-in-progress`` for a run started by *event_name*."""
    if isinstance(value, bool):
        return value
    match = _EVENT_EQUALS.match(str(value).strip())
    assert match, f"unrecognised cancel-in-progress expression: {value!r}"
    return match.group(1) == event_name


def test_nightly_keeps_one_run_per_group() -> None:
    concurrency = _workflow()["concurrency"]
    assert concurrency["group"] == "nightly-build"


def test_only_the_scheduled_run_cancels_the_run_in_progress() -> None:
    doc = _workflow()
    # PyYAML reads the bare `on:` key as boolean True.
    triggers = doc.get("on", doc.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    cancel = doc["concurrency"]["cancel-in-progress"]
    assert _cancels_in_progress(cancel, "schedule") is True
    assert _cancels_in_progress(cancel, "workflow_dispatch") is False
