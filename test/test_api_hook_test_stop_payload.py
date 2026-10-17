"""Tests for api_hook_test (POST /api/hooks/{hook_id}/test) Stop-event payload.

A Stop hook reads the full final assistant segment from the stdin
``assistant_text`` key (the KIROCREW_HOOK_CONTEXT env var is capped at 500).
The test endpoint must build the same payload so a tail-reading Stop hook is
actually testable through the dashboard, not silently starved of its context.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from body_stream_helpers import BodyStreamPayload

from kiro_crew.dashboard.handlers.hooks import api_hook_test
from kiro_crew.hooks import HOOK_EVENT_STOP, HOOK_EVENT_USER_PROMPT_SUBMIT, ScriptHookStore


def _req_with_store(
    store: ScriptHookStore,
    hook_id: str,
    context: str,
    content_type: str = "application/json",
) -> make_mocked_request:
    state = MagicMock()
    state._hook_store = store
    # The route is owner-gated
    # (``handlers._shared.require_owner_dashboard_request``): the predicate reads
    # the claims the token-auth middleware publishes plus ``state.owner_id``, and
    # ``owner_id == ""`` with the signed local bootstrap subject ``local-app`` is
    # the standalone-local owner shape.
    state.owner_id = ""
    raw = json.dumps({"context": context}).encode()
    req = make_mocked_request(
        "POST",
        f"/api/hooks/{hook_id}/test",
        match_info={"hook_id": hook_id},
        headers={"Content-Type": content_type, "Content-Length": str(len(raw))},
        payload=BodyStreamPayload(raw),
    )
    req.app["state"] = state
    req["user"] = "local-app"
    req["app"] = ""
    return req


@pytest.mark.asyncio
async def test_stop_hook_test_supplies_full_assistant_text(tmp_path):
    store = ScriptHookStore(tmp_path)
    hook = store.create(
        {"name": "stop-hook", "event": HOOK_EVENT_STOP, "matcher": "", "command": "cat"}
    )
    full = ("x" * 900) + "\n[OPTIONS: A | B | C]"

    fake_result = type(
        "R",
        (),
        {
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
            "error": "",
            "hook_name": "stop-hook",
        },
    )()
    with (
        patch(
            "kiro_crew.hooks.run_script_hook", new_callable=AsyncMock, return_value=fake_result
        ) as mock_run,
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
async def test_user_prompt_submit_hook_test_supplies_full_prompt(tmp_path):
    # A UserPromptSubmit hook reads its context from the stdin ``prompt`` key
    # (fire() sets it there); the env var is capped at 500 for every event, so
    # the test endpoint must carry the FULL prompt on stdin or a >500-char test
    # context is truncated on both paths and the hook receives incomplete input.
    store = ScriptHookStore(tmp_path)
    hook = store.create(
        {
            "name": "ups-hook",
            "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
            "matcher": "",
            "command": "cat",
        }
    )
    full = "p" * 900

    fake_result = type(
        "R",
        (),
        {
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
            "error": "",
            "hook_name": "ups-hook",
        },
    )()
    with (
        patch(
            "kiro_crew.hooks.run_script_hook", new_callable=AsyncMock, return_value=fake_result
        ) as mock_run,
        patch("kiro_crew.dashboard.handlers.hooks._sel", return_value=MagicMock()),
    ):
        resp = await api_hook_test(_req_with_store(store, hook.id, full))

    assert resp.status == 200
    _args, _kwargs = mock_run.call_args
    hook_event = _args[2] if len(_args) > 2 else _kwargs.get("hook_event")
    assert hook_event is not None
    assert hook_event["hook_event_name"] == HOOK_EVENT_USER_PROMPT_SUBMIT
    assert hook_event["prompt"] == full


@pytest.mark.asyncio
async def test_other_event_hook_test_uses_default_payload(tmp_path):
    # An event whose context fire() sends only via the env var (not a dedicated
    # stdin key) keeps the default (None) payload — run_script_hook builds it,
    # matching production, so the diagnostic path does not diverge.
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE

    store = ScriptHookStore(tmp_path)
    hook = store.create(
        {"name": "ptu-hook", "event": HOOK_EVENT_PRE_TOOL_USE, "matcher": "", "command": "cat"}
    )

    fake_result = type(
        "R",
        (),
        {
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
            "error": "",
            "hook_name": "ptu-hook",
        },
    )()
    with (
        patch(
            "kiro_crew.hooks.run_script_hook", new_callable=AsyncMock, return_value=fake_result
        ) as mock_run,
        patch("kiro_crew.dashboard.handlers.hooks._sel", return_value=MagicMock()),
    ):
        resp = await api_hook_test(_req_with_store(store, hook.id, "hello"))

    assert resp.status == 200
    _args, _kwargs = mock_run.call_args
    hook_event = _args[2] if len(_args) > 2 else _kwargs.get("hook_event")
    assert hook_event is None


@pytest.mark.asyncio
async def test_hook_test_redacts_context_in_sel_metadata(tmp_path):
    store = ScriptHookStore(tmp_path)
    hook = store.create(
        {
            "name": "audit-hook",
            "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
            "matcher": "",
            "command": "cat",
        }
    )
    secret = "AKIAIOSFODNN7EXAMPLE"
    context = f"deploy with {secret}"
    fake_result = type(
        "R",
        (),
        {
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
            "error": "",
            "hook_name": "audit-hook",
        },
    )()
    audit = MagicMock()

    with (
        patch(
            "kiro_crew.hooks.run_script_hook",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as mock_run,
        patch("kiro_crew.dashboard.handlers.hooks._sel", return_value=audit),
    ):
        resp = await api_hook_test(_req_with_store(store, hook.id, context))

    assert resp.status == 200
    assert mock_run.await_args.args[1] == context
    metadata = audit.log_tool_invocation.call_args.kwargs["metadata"]
    assert secret not in metadata["context"]
    assert "redacted" in metadata["context"].lower()


@pytest.mark.asyncio
async def test_a_non_json_body_is_refused_415_not_defaulted(tmp_path):
    """This ``default_empty`` route defaults an absent body, never a refused one."""
    store = ScriptHookStore(tmp_path)
    hook = store.create(
        {"name": "stop-hook", "event": HOOK_EVENT_STOP, "matcher": "", "command": "cat"}
    )
    with (
        patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run,
        patch("kiro_crew.dashboard.handlers.hooks._sel", return_value=MagicMock()),
    ):
        resp = await api_hook_test(
            _req_with_store(store, hook.id, "ctx", content_type="text/plain")
        )

    assert resp.status == 415
    assert json.loads(resp.body)["code"] == "unsupported_media_type"
    mock_run.assert_not_called()
