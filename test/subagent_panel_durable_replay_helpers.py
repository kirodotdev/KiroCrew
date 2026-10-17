"""The run-folder writer and panel readers the durable-replay tests share.

``test_subagent_panel_durable_replay.py`` and its ``_records`` and ``_access`` siblings
import these by name: the fixture that points persistence at a registry below the test's
temp directory, the writer that leaves one run folder the way a real run does, the panel
and id readers, and the tombstone and truncation constants. ``agent_root`` is declared
``name="agent_root"`` on ``agent_root_fixture`` so no test parameter shadows the import.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from kiro_crew.subagent_persistence import _PANEL_TRUNC_MARKER, read_panel_records

DAY = 86_400.0


CORRUPT = "__corrupt__"
TRUNC = _PANEL_TRUNC_MARKER


@pytest.fixture(name="agent_root")
def agent_root_fixture(tmp_path, monkeypatch):
    """Point persistence at a registry below this test's temp directory."""
    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    return root


def write_record(
    root,
    agent_id: str,
    *,
    task: str = "summarise the changelog",
    agent: str = "kirocrew",
    parent_session: str = "dashboard:chat-1",
    started: float | None = None,
    memory_mode: str = "persistent",
    app: str = "",
    tombstone_cause: str | None = "delivered",
    outcome: str | None = None,
    detail: str | None = None,
    died: float | None = None,
    result: str | None = None,
    mtime: float | None = None,
) -> None:
    """Write one run folder the way a real run leaves it behind.

    ``tombstone_cause=None`` writes no tombstone; ``CORRUPT`` writes one that is
    present and unparseable. Those are different states and the reader tells them
    apart, so the helper has to be able to produce both.
    """
    moment = time.time()
    folder = root / agent_id
    folder.mkdir(parents=True, exist_ok=True)
    state = {
        "id": agent_id,
        "task": task,
        "agent": agent,
        "parent_session": parent_session,
        "started": moment - 120 if started is None else started,
        "status": "running",
        "turns": 2,
        "memory_mode": memory_mode,
        "execution_context": {"memory_mode": memory_mode},
        "app": app,
        "updated_at": moment - 60,
    }
    (folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
    if result is not None:
        (folder / "result.txt").write_text(result, encoding="utf-8")
    if tombstone_cause == CORRUPT:
        (folder / "tombstone.json").write_text("{not json", encoding="utf-8")
    elif tombstone_cause is not None:
        tombstone: dict = {
            "id": agent_id,
            "cause": tombstone_cause,
            "recovery_action": "delivered",
            "started": state["started"],
            "died": moment - 60 if died is None else died,
        }
        if outcome is not None:
            tombstone["outcome"] = outcome
        if detail is not None:
            tombstone["detail"] = detail
        (folder / "tombstone.json").write_text(json.dumps(tombstone), encoding="utf-8")
    if mtime is not None:
        os.utime(folder, (mtime, mtime))


def ids(records) -> list[str]:
    return [record["id"] for record in records]


def panel(**kwargs):
    """The records list alone, for cases that do not assert on the overflow."""
    return read_panel_records(**kwargs).records
