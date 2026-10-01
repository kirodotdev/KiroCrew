"""The in-app restart keeps the turn-in-flight marker of a turn it cuts.

``_restart_gateway`` (``POST /api/restart`` and update apply) drains sessions
with ``close_all()`` and re-execs the process image without ever setting
``shutdown_event``. The turn that drain ends must keep its marker, exactly as a
SIGTERM shutdown keeps it, so the next process shows the interruption row and
the Resume button. A turn that lands during the drain still clears it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_local_turn_restart_marker import _client_streaming, _state_for_run_chat

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history
from kiro_crew.dashboard.handlers import updates
from kiro_crew.session import SessionManager

_RESTART_KIND = "gateway_restart_interruption"


def _blocking_client(release: asyncio.Event, *, lands: bool):
    from kiro_crew.acp.types import STOP_REASON_CANCELLED, STOP_REASON_END_TURN
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    client = _client_streaming([])
    stop_reason = STOP_REASON_END_TURN if lands else STOP_REASON_CANCELLED

    async def _stream(msg):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the answer" if lands else "partial")
        await release.wait()
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason)

    client.stream = _stream
    client.stream_command = _stream
    return client


async def _restart_mid_turn(tmp_path, monkeypatch, key: str, *, lands: bool):
    """Run one turn, then drive the real in-app restart while it is in flight."""
    from kiro_crew.dashboard.chat import _run_chat

    state = _state_for_run_chat(tmp_path, monkeypatch)
    state._gateway_restart_in_progress = False
    state.push_update_progress = MagicMock()
    slot = state.get_or_create_slot(key)
    slot.append("user", "do something long", "msg msg-u")
    release = asyncio.Event()
    state.sessions.get_or_create = AsyncMock(
        return_value=(_blocking_client(release, lands=lands), True, False)
    )
    turn = asyncio.create_task(_run_chat(state, slot, "do something long"))
    while slot._turn_in_flight_generation == 0:
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)

    async def _close_all():
        # What the real drain does to this turn: closing state first, then the
        # provider is cancelled and the turn's own teardown runs.
        state.sessions.final_drain_started = True
        release.set()
        await turn

    state.sessions.close_all = _close_all
    execs: list[str] = []
    monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(updates.platform_compat, "execv_target_available", lambda _exe: True)
    monkeypatch.setattr(updates, "flush_breadcrumb_writes", lambda _t: None)
    monkeypatch.setattr(updates, "reexec_python_module", lambda *_a, **_k: execs.append("exec"))

    assert await updates._restart_gateway(state, resolver=lambda: "/python") is True
    assert execs == ["exec"]
    assert turn.done()
    return state, slot


@pytest.mark.asyncio
async def test_inapp_restart_keeps_the_marker_of_an_unfinished_turn(tmp_path, monkeypatch):
    state, slot = await _restart_mid_turn(tmp_path, monkeypatch, "cut", lands=False)

    assert slot._turn_in_flight_generation > 0
    meta = state.conversation_log.get_metadata("dashboard:cut")
    assert meta["turn_in_flight_generation"] == slot._turn_in_flight_generation


@pytest.mark.asyncio
async def test_after_inapp_restart_the_chat_reads_as_interrupted(tmp_path, monkeypatch):
    state, _slot = await _restart_mid_turn(tmp_path, monkeypatch, "resume-me", lands=False)

    # The successor process restores from disk.
    del state._slots["resume-me"]
    restored = _rehydrate_slot_from_history(state, "resume-me")
    assert restored is not None
    assert restored.to_dict()["interrupted"] is True
    kinds = [(m.get("meta") or {}).get("kind") for m in restored.messages]
    assert kinds.count(_RESTART_KIND) == 1


@pytest.mark.asyncio
async def test_a_turn_that_lands_during_inapp_restart_still_clears(tmp_path, monkeypatch):
    state, slot = await _restart_mid_turn(tmp_path, monkeypatch, "landed", lands=True)

    assert slot._turn_in_flight_generation == 0
    assert "turn_in_flight_generation" not in state.conversation_log.get_metadata(
        "dashboard:landed"
    )
    del state._slots["landed"]
    restored = _rehydrate_slot_from_history(state, "landed")
    assert restored is not None
    assert restored.to_dict()["interrupted"] is False


class _FakeProvider:
    pid = None

    async def stop(self):
        return None


@pytest.mark.asyncio
async def test_final_drain_started_is_set_by_close_all_only(tmp_path, monkeypatch):
    mgr = SessionManager(KiroCrewConfig(), provider_factory=lambda **k: _FakeProvider())
    assert mgr.final_drain_started is False

    # An update pause closes admission but can still resume: not final.
    assert await mgr.pause_turn_admission_for_update() is True
    assert mgr.final_drain_started is False
    await mgr.resume_turn_admission_after_update()
    assert mgr.final_drain_started is False

    await mgr.close_all()
    assert mgr.final_drain_started is True
