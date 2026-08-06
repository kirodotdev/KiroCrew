"""Tests for ``GET /api/sessions/memory``.

Follows the handler-test convention in ``test_dashboard_sessions_clear.py``: call
the handler directly with a faked request/state rather than standing up aiohttp.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers.sessions import api_sessions_memory


class _FakeSlot:
    def __init__(self, title: str) -> None:
        self.display_title = title


def _make_request(
    rows: list[dict[str, object]],
    *,
    slots: dict[str, _FakeSlot] | None = None,
    tasks: list[dict[str, object]] | None = None,
    with_subagents: bool = True,
) -> web.Request:
    slots = slots or {}
    sessions = MagicMock()
    sessions.runtime_pids.return_value = rows
    state = MagicMock()
    state.sessions = sessions
    state.get_slot.side_effect = lambda name: slots.get(name)
    if with_subagents:
        subagents = MagicMock()
        subagents.task_memory_rows.return_value = tasks or []
        state.subagents = subagents
    else:
        state.subagents = None
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    return request


def _row(key: str, pid: int | None) -> dict[str, object]:
    return {
        "key": key,
        "agent": "kirocrew",
        "pid": pid,
        "owns_runtime": True,
        "created_at": 1000.0,
        "prompts": 1,
    }


@pytest.fixture(autouse=True)
def stub_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the handler off real /proc — the sampler is unit-tested separately."""
    from kiro_crew.dashboard import session_memory as sm

    monkeypatch.setattr(sm.sys, "platform", "linux")
    monkeypatch.setattr(sm, "_get_rss_tree_mb", lambda pid: 1525.0)
    monkeypatch.setattr(sm, "_iter_descendant_pids", lambda pid: [pid, pid + 1])
    monkeypatch.setattr(sm, "_read_cmdline", lambda pid: "")
    monkeypatch.setattr(sm, "_subtree_cpu_jiffies", lambda pid: 0)
    monkeypatch.setattr(sm, "_get_static_system_info", lambda: {"mem_total_gb": 48.0})


async def _call(request: web.Request) -> tuple[int, dict]:
    resp = await api_sessions_memory(request)
    return resp.status, json.loads(resp.body)


@pytest.mark.asyncio
async def test_returns_titles_resolved_through_the_slot() -> None:
    request = _make_request(
        [_row("dashboard:chat-69", 7)],
        slots={"chat-69": _FakeSlot("GitHub PR review explanation request")},
    )
    status, body = await _call(request)

    assert status == 200
    assert body["sessions"][0]["title"] == "GitHub PR review explanation request"
    assert body["sessions"][0]["slot_key"] == "chat-69"


@pytest.mark.asyncio
async def test_payload_carries_tasks_totals_and_history() -> None:
    tasks = [{"id": "t1", "task": "aspect-review", "rss_mb": 900.0, "sampled": True}]
    status, body = await _call(_make_request([_row("dashboard:a", 7)], tasks=tasks))

    assert status == 200
    assert body["tasks"] == tasks
    assert body["totals"]["rss_mb"] == 1525.0
    assert body["totals"]["host_mb"] == pytest.approx(49152.0)
    assert len(body["history"]) >= 1


@pytest.mark.asyncio
async def test_works_before_the_subagent_manager_exists() -> None:
    """The dashboard serves requests during startup, when state.subagents is None;
    a task-manager view must degrade to sessions-only rather than 500."""
    status, body = await _call(_make_request([_row("dashboard:a", 7)], with_subagents=False))

    assert status == 200
    assert body["tasks"] == []
