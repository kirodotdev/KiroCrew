# SPDX-License-Identifier: Apache-2.0
"""A crewmate's stale worker: released by the idle sweep, resumed on the next send.

The idle sweep stops a worker's process and keeps its conversation; the next turn
cold-starts the same key and resumes it through ``session/load``, on the same
execution record and so with the same kit. ``session_status`` and the worker tab
say which: ``process: live | released``. A conductor messaging an archived worker
of its own revives it first; any other caller gets ``target_not_found``. After a
restart, the crew log's lineage still lets the worker report to its crewmate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import mate_worker
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard import mate_thread_wakes as wakes
from kiro_crew.dashboard.session_control_parts import session_revive
from kiro_crew.dashboard.session_control_parts.session_status import process_state
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.session import SessionManager

WORKER_KEY = "dashboard:chat-9-1790000000"
WORKER = ExecutionContext(
    "pixel", MemoryStoreRef("member-pixel", "pixel"), "member", "kirocrew"
).with_template(mate_worker.MATE_WORKER_AGENT, mate_worker.MATE_WORKER_AGENT)


def _provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        provider.has_active_turn = lambda: False
        return provider

    return factory


@pytest.mark.asyncio
async def test_an_idle_worker_is_released_then_resumed_in_the_same_conversation(
    monkeypatch, caplog
):
    manager = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    await manager.get_or_create(WORKER_KEY)
    manager.release(WORKER_KEY)
    # KAS decides a resume by `session/load` itself, so no transcript file is needed.
    manager._session_map.set(WORKER_KEY, "sid-worker-1", provider="kas")
    async with manager._lock:
        manager._sessions[WORKER_KEY].last_used = time.monotonic() - 10_000
    state = SimpleNamespace(sessions=manager)
    slot = SimpleNamespace(key="chat-9-1790000000", linked_session_key="")
    assert process_state(state, slot) == "live"

    await manager._expire_idle(timeout_secs=60)

    # Released: the process is gone, the conversation pointer is kept.
    assert WORKER_KEY not in manager._sessions
    assert process_state(state, slot) == "released"
    assert manager._session_map.get(WORKER_KEY) == "sid-worker-1"

    # The next send's turn cold-starts the same key and asks to resume that sid.
    with caplog.at_level(logging.INFO):
        _, is_new, _ = await manager.get_or_create(WORKER_KEY)
    assert is_new is True
    assert any("resume_sid=sid-worker-1" in r.getMessage() for r in caplog.records)
    assert process_state(state, slot) == "live"
    await manager.close_all()

    # Its kit is read from the same execution record, which the release left alone.
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key: WORKER if key == WORKER_KEY else None,
    )
    monkeypatch.setattr(mate_worker, "resolve_worker_spec", lambda base, member: {"m": member})
    assert mate_worker.worker_session_spec({}, WORKER_KEY) == {"m": "pixel"}


def _send_env(monkeypatch, caller_execution, revive_result=None, revive_error=None):
    calls: list[str] = []

    async def revive(state, *, caller_session_key, target, **_kw):
        calls.append(target)
        if revive_error is not None:
            raise revive_error
        return revive_result

    monkeypatch.setattr(session_revive, "revive_session", revive)
    monkeypatch.setattr(
        "kiro_crew.dashboard.session_control._resolve_slot",
        lambda state, target: None,
    )
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda key: caller_execution
    )
    return calls


CONDUCTOR = SimpleNamespace(selection_kind="member", template_id=mate_worker.MATE_CONDUCTOR_AGENT)


def test_a_conductor_messaging_its_archived_worker_revives_it_first(monkeypatch):
    calls = _send_env(monkeypatch, CONDUCTOR, revive_result={"target": "chat-9"})
    got = asyncio.run(
        session_revive.revive_own_archived_worker(object(), "dashboard:member-pixel", "chat-9")
    )
    assert calls == ["chat-9"] and got == "chat-9"


def test_a_refused_revive_and_any_other_caller_keep_target_not_found(monkeypatch):
    from kiro_crew.dashboard.session_control_parts.caller_access import SessionControlError

    calls = _send_env(
        monkeypatch, CONDUCTOR, revive_error=SessionControlError("no", code="not_creator")
    )
    assert asyncio.run(session_revive.revive_own_archived_worker(object(), "k", "t")) == "t"
    assert calls == ["t"]

    plain = SimpleNamespace(selection_kind="member", template_id="kirocrew")
    calls = _send_env(monkeypatch, plain, revive_result={"target": "x"})
    assert asyncio.run(session_revive.revive_own_archived_worker(object(), "k", "t")) == "t"
    assert calls == []


def _thread_and_worker():
    thread = SimpleNamespace(
        key="member-pixel", agent="pixel", mode="member", executor="", running=False
    )
    worker = SimpleNamespace(
        key="chat-9", _created_by="member-pixel", _lineage_minted=False, _reports_to_creator=False
    )
    state = SimpleNamespace(get_slot=lambda key: thread if key == thread.key else None)
    return state, thread, worker


def test_a_restored_worker_reports_when_the_lineage_names_its_crewmate(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.session_control._slot_tree_parent",
        lambda key: (True, "member-pixel", {}),
    )
    state, thread, worker = _thread_and_worker()
    assert wakes.reporting_creator(state, worker) is thread


def test_transcript_metadata_alone_still_wakes_nobody(monkeypatch):
    state, _thread, worker = _thread_and_worker()
    monkeypatch.setattr(
        "kiro_crew.dashboard.session_control._slot_tree_parent", lambda key: (False, "", {})
    )
    assert wakes.reporting_creator(state, worker) is None
    monkeypatch.setattr(
        "kiro_crew.dashboard.session_control._slot_tree_parent",
        lambda key: (True, "member-other", {}),
    )
    assert wakes.reporting_creator(state, worker) is None
