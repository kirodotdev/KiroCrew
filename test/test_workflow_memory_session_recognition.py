"""Workflow memory recognition follows the real manager's registration lifetime."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.handlers.cron import _recognize_session
from kiro_crew.dashboard.handlers.memory import _memory_write_gate
from kiro_crew.history import is_incognito_transcript
from kiro_crew.session import SessionManager


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["wf", "wf-pool", "wf-unpooled", "wf-worker", "wf-author"])
async def test_workflow_memory_recognition_tracks_registration(prefix):
    provider = AsyncMock()
    provider.is_process_alive = lambda: True
    provider.has_active_turn = lambda: False
    provider.runtime_info = lambda: (None, None)
    sessions = SessionManager(KiroCrewConfig(), provider_factory=lambda *args, **kwargs: provider)
    state = SimpleNamespace(sessions=sessions, _slots={}, _restricted_keys=set())
    key = f"{prefix}:wf_1:0"

    async def recognize(candidate):
        return await _recognize_session(
            state, candidate, "learn_add", blocks_persisted_mode=is_incognito_transcript
        )

    assert (await recognize(key)).status == 400
    from kiro_crew.workflow_memory import publish_binding

    await asyncio.to_thread(publish_binding, "wf_1", "", "", memory_mode="persistent")
    try:
        await sessions.get_or_create(key)
        assert sessions.has_session(key)
        assert await recognize(key) is None
        assert (await recognize(f"{prefix}:wf_2:0")).status == 400
        request = make_mocked_request("POST", "/api/memory/recall", headers={"X-Session-Key": key})
        state._restricted_keys.add(key)
        refusal = await _memory_write_gate(state, request, "memory_recall")
        assert refusal.status == 403
        assert json.loads(refusal.text)["code"] == "restricted_session"
        state._restricted_keys.clear()
    finally:
        sessions.release(key)
        await sessions.destroy(key)
    assert not sessions.has_session(key)
    assert (await recognize(key)).status == 400
