"""The loop's runtime budget is described as what it measures, wherever it is sized.

``runtime_elapsed`` advances on the monotonic clock while the gateway runs, so the
budget is awake time: a host that sleeps and wakes into the same process spends none
of it on Linux and macOS. After a restart the time since ``created_ts`` is read from
the wall clock. The surfaces an agent or a user sizes a budget from have to say that.
Calling it a "wall-clock budget" that "bounds elapsed time" sends a loop on a laptop
that sleeps past the time it was told it would stop.

Pinned here: the ``monitor_start`` and ``monitor_update`` schema descriptions, the
``monitor_start`` acknowledgement and ``docs/monitor-loops.md``. The expiry notice
and the ``monitor_update`` refusal for a budget-stopped loop are pinned beside their
other wording, in ``test_slack_gateway_more_coverage.py`` (``TestNotifyNudgeExpired``)
and ``test_autonudge_stop_auth.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import mcp_core
from kiro_crew.config import KiroCrewConfig
from kiro_crew.mcp_tools import control

MEANING = (
    "awake time while the gateway runs; a restart counts the time since "
    "created_ts on the wall clock"
)
DOC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "docs" / "monitor-loops.md"


@pytest.fixture
def operator_ceiling(monkeypatch):
    """Pin the operator ceiling the descriptions quote, so no config on disk is read."""
    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(monitoring=SimpleNamespace(max_runtime_secs=604_800)),
    )


@pytest.fixture
def bound_session(monkeypatch, operator_ceiling):
    """Make the tool believe it runs on a session that can arm a loop."""
    monkeypatch.setattr(mcp_core, "_autonudge_binding_key", lambda sk: "chat-1-123")
    monkeypatch.setattr(mcp_core, "_current_session_key", lambda: "dashboard:1", raising=False)


def _flat(text: str) -> str:
    """One line, no Markdown code ticks: a wrapped doc line compares as it reads."""
    return re.sub(r"\s+", " ", text.replace("`", ""))


@pytest.mark.parametrize(
    "tool, lead",
    [
        ("monitor_start", "runtime budget in seconds"),
        ("monitor_update", "new runtime budget in seconds"),
    ],
)
def test_the_schema_describes_the_budget_as_awake_time(operator_ceiling, tool, lead):
    tools = {entry["name"]: entry for entry in control.schemas()}
    description = tools[tool]["inputSchema"]["properties"]["max_runtime_secs"]["description"]
    assert description.lower().startswith(f"{lead} ({MEANING})"), description
    assert "wall-clock" not in description.lower(), "the budget no longer follows the wall clock"
    assert "elapsed time" not in description.lower(), "it bounds the time the loop runs"


def test_the_monitor_start_ack_describes_the_budget_as_awake_time(bound_session):
    ack = control.monitor_start(
        "monitor_start",
        {
            "message": "keep the branch rebased",
            "interval_secs": 300,
            "max_cycles": 5,
            "max_runtime_secs": 3600,
        },
    )
    assert f"runtime budget 3600s ({MEANING})" in ack, ack
    assert "wall-clock" not in ack


def test_the_user_doc_states_both_readings_of_the_budget():
    doc = _flat(DOC.read_text(encoding="utf-8"))
    assert f"runtime budget ({MEANING})" in doc
    assert "counts toward the budget on Windows and not on Linux or macOS" in doc
    assert "A restart reads the time since the loop was armed (created_ts)" in doc
    assert "wall-clock budget" not in doc
