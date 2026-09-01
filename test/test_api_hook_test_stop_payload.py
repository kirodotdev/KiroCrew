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
from kiro_crew.hooks import (
    HOOK_EVENT_SESSION_LANE_CHANGED,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHookStore,
)


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
async def test_non_stop_hook_test_uses_default_payload(tmp_path):
    # The endpoint's own session key rides along: without it run_script_hook resolves sk=""
    # and a profile that denies script_hooks is never applied to a test run.
    store = ScriptHookStore(tmp_path)
    hook = store.create(
        {
            "name": "ups-hook",
            "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
            "matcher": "",
            "command": "cat",
        }
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
            "hook_name": "ups-hook",
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
    assert hook_event is not None
    assert hook_event["parent_session_key"] == "dashboard:hook_test"
    assert hook_event["hook_event_name"] == HOOK_EVENT_USER_PROMPT_SUBMIT


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
async def test_lane_hook_test_supplies_the_stamped_payload_keys(tmp_path):
    """A SessionLaneChanged hook must be testable without KeyErroring on its payload.

    fire() stamps slot/added/removed before the caller's mapping, so the real path
    always carries them. This endpoint bypasses fire(), so it has to stamp them too.
    """
    store = ScriptHookStore(tmp_path)
    hook = store.create(
        {
            "name": "lane-hook",
            "event": HOOK_EVENT_SESSION_LANE_CHANGED,
            "matcher": "*",
            "command": "cat",
        }
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
            "hook_name": "lane-hook",
            "hook_id": hook.id,
            "event": HOOK_EVENT_SESSION_LANE_CHANGED,
        },
    )()
    with (
        patch(
            "kiro_crew.hooks.run_script_hook", new_callable=AsyncMock, return_value=fake_result
        ) as mock_run,
        patch("kiro_crew.dashboard.handlers.hooks._sel", return_value=MagicMock()),
    ):
        resp = await api_hook_test(_req_with_store(store, hook.id, "lane test"))

    assert resp.status == 200
    _args, _kwargs = mock_run.call_args
    hook_event = _args[2] if len(_args) > 2 else _kwargs.get("hook_event")
    assert hook_event is not None, (
        "the lane hook was tested with no payload, so a script reading slot/added/"
        "removed fails on the one path an author uses to try it"
    )
    for key, empty in (("slot", ""), ("added", []), ("removed", [])):
        assert key in hook_event, f"the test payload omits {key!r}"
        assert hook_event[key] == empty, f"{key!r} should default to {empty!r}"


@pytest.mark.asyncio
async def test_testing_a_hook_republishes_so_readers_see_the_run(tmp_path):
    """A Test click must reach /api/hooks, which is served from the snapshot.

    run_script_hook mutates the LIVE hook; list_all serves the committed snapshot. A
    direct call therefore updates status and run_count where no reader can see them.
    """
    store = ScriptHookStore(tmp_path)
    hook = store.create({"name": "pub", "event": HOOK_EVENT_STOP, "matcher": "", "command": "true"})
    before = [h for h in store.list_all() if h.id == hook.id][0]
    assert before.run_count == 0, "precondition: the snapshot starts at zero"

    with patch("kiro_crew.dashboard.handlers.hooks._sel", return_value=MagicMock()):
        resp = await api_hook_test(_req_with_store(store, hook.id, "hello"))

    assert resp.status == 200
    after = [h for h in store.list_all() if h.id == hook.id]
    assert after, "the hook vanished from the published snapshot"
    assert after[0].run_count == 1, (
        "the published snapshot still reports run_count="
        f"{after[0].run_count}, so /api/hooks serves a stale status after a test run"
    )
    assert after[0].last_status, "the snapshot carries no last_status after a test run"


def _req_as_app(store: ScriptHookStore, hook_id: str, app_name: str) -> make_mocked_request:
    """A request carrying a VERIFIED app identity, as the token-auth middleware leaves it."""
    req = _req_with_store(store, hook_id, "c")
    req["app"] = app_name
    return req


@pytest.mark.asyncio
async def test_an_app_token_caller_is_not_stamped_as_the_dashboard(tmp_path):
    """The endpoint must not label an app-token caller a dashboard caller.

    An app declaring /api/hooks in permissions.api reaches this route, so the surface is not
    evidence of who is calling. Stamping the dashboard key made governance resolve the
    dashboard profile, which is a ceiling the app was never granted.
    """
    store = ScriptHookStore(tmp_path)
    hook = store.create(
        {"name": "h", "event": HOOK_EVENT_USER_PROMPT_SUBMIT, "matcher": "", "command": "true"}
    )

    seen: dict = {}

    async def _capture(hook_obj, context, hook_event):
        seen.update(hook_event or {})
        return type(
            "R",
            (),
            {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "duration_ms": 1,
                "error": "",
                "hook_name": "h",
            },
        )()

    with patch.object(ScriptHookStore, "run_one_and_fold", new=AsyncMock(side_effect=_capture)):
        await api_hook_test(_req_as_app(store, hook.id, "rogue-app"))

    assert seen.get("parent_app") == "rogue-app", (
        "the endpoint dropped the verified app, so governance cannot resolve the app's own "
        "profile: %r" % (seen,)
    )
    assert seen.get("parent_session_key") != "dashboard:hook_test", (
        "an app-token caller is still stamped as the dashboard, which selects the dashboard "
        "profile instead of the app's: %r" % (seen,)
    )


@pytest.mark.asyncio
async def test_a_dashboard_caller_still_uses_the_dashboard_key(tmp_path):
    """No app bound: the dashboard key is correct and must be unchanged."""
    store = ScriptHookStore(tmp_path)
    hook = store.create(
        {"name": "h", "event": HOOK_EVENT_USER_PROMPT_SUBMIT, "matcher": "", "command": "true"}
    )

    seen: dict = {}

    async def _capture(hook_obj, context, hook_event):
        seen.update(hook_event or {})
        return type(
            "R",
            (),
            {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "duration_ms": 1,
                "error": "",
                "hook_name": "h",
            },
        )()

    with patch.object(ScriptHookStore, "run_one_and_fold", new=AsyncMock(side_effect=_capture)):
        await api_hook_test(_req_with_store(store, hook.id, "c"))

    assert seen.get("parent_session_key") == "dashboard:hook_test", seen
    assert seen.get("parent_app") == "", seen


@pytest.mark.asyncio
async def test_an_app_profile_denial_refuses_the_test_run():
    """The app's OWN denial must reach the gate and refuse the run."""
    import kiro_crew.hooks as H
    import kiro_crew.platform.governance_profiles as G

    seen: list[dict] = []

    def _permits(scope, item, *, session_key="", agent="", app="", **kw):
        seen.append({"session_key": session_key, "app": app})

        class _D:
            permitted = app != "rogue-app"
            reason = "script_hooks denied for this app"

        return _D()

    with patch.object(G, "governance_permits", _permits):
        denial = await H._script_hooks_capability_denied_async("app:rogue-app", "rogue-app")

    assert seen, "governance was never consulted"
    assert seen[-1]["app"] == "rogue-app", (
        "the verified app never reached governance_permits, so the app profile could not "
        "have been resolved: %r" % (seen[-1],)
    )
    assert denial, "the app's own script_hooks denial was not honoured"
