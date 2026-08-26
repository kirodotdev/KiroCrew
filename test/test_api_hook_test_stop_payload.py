"""Tests for api_hook_test (POST /api/hooks/{hook_id}/test) Stop-event payload.

A Stop hook reads the full final assistant segment from the stdin
``assistant_text`` key (the KIROCREW_HOOK_CONTEXT env var is capped at 500).
The test endpoint must build the same payload so a tail-reading Stop hook is
actually testable through the dashboard, not silently starved of its context.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers.hooks import api_hook_test
from kiro_crew.hooks import HOOK_EVENT_STOP, HOOK_EVENT_USER_PROMPT_SUBMIT, ScriptHookStore


def _req_with_store(store: ScriptHookStore, hook_id: str, context: str) -> make_mocked_request:
    state = MagicMock()
    state._hook_store = store
    req = make_mocked_request(
        "POST",
        f"/api/hooks/{hook_id}/test",
        match_info={"hook_id": hook_id},
    )
    req.app["state"] = state
    req.json = AsyncMock(return_value={"context": context})
    return req


@pytest.mark.asyncio
async def test_stop_hook_test_supplies_full_assistant_text(tmp_path):
    store = ScriptHookStore(tmp_path)
    hook = store.create({"name": "stop-hook", "event": HOOK_EVENT_STOP, "matcher": "", "command": "cat"})
    full = ("x" * 900) + "\n[OPTIONS: A | B | C]"

    fake_result = type("R", (), {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1, "error": "", "hook_name": "stop-hook"})()
    with (
        patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock, return_value=fake_result) as mock_run,
        patch("kiro_crew.dashboard.handlers.hooks._sel", return_value=MagicMock()),
    ):
        resp = await api_hook_test(_req_with_store(store, hook.id, full))

    assert resp.status == 200
    # The endpoint must pass a hook_event carrying the FULL segment on stdin.
    _args, _kwargs = mock_run.call_args
    hook_event = _args[2] if len(_args) > 2 else _kwargs.get("hook_event")
    assert hook_event is not None
    assert hook_event["assistant_text"] == full
    assert "[OPTIONS:" in hook_event["assistant_text"]


@pytest.mark.asyncio
async def test_non_stop_hook_test_uses_default_payload(tmp_path):
    # A non-Stop hook keeps the default (None) payload — run_script_hook builds it.
    store = ScriptHookStore(tmp_path)
    hook = store.create({"name": "ups-hook", "event": HOOK_EVENT_USER_PROMPT_SUBMIT, "matcher": "", "command": "cat"})

    fake_result = type("R", (), {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1, "error": "", "hook_name": "ups-hook"})()
    with (
        patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock, return_value=fake_result) as mock_run,
        patch("kiro_crew.dashboard.handlers.hooks._sel", return_value=MagicMock()),
    ):
        resp = await api_hook_test(_req_with_store(store, hook.id, "hello"))

    assert resp.status == 200
    _args, _kwargs = mock_run.call_args
    hook_event = _args[2] if len(_args) > 2 else _kwargs.get("hook_event")
    assert hook_event is None
