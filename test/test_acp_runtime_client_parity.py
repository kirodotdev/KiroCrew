"""Contract parity between the shared ``AcpRuntime`` path and ``AcpClient``.

Session-config capture, command-output redaction, auth-failure detection and the
exceptions a dead runtime surfaces as, steer, cancel grace, ``set_model`` sync, the
permission event built from cached tool input, sweep protection of runtime PIDs,
the spawn environment scrub, and tool-purpose extraction.
"""

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _await_routed,
    _feed,
    _make_runtime,
    _register,
    _replying_send,
    _spawn_client_mod,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeDead, AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_KIRO, JsonRpcMessage
from kiro_crew.testing.wait import await_until

# ── Round-1 follow-up fixes: #5b currentModelId backfill + send_command redaction ──


def test_backfill_uses_resolved_model_id_from_session_config(monkeypatch):
    """#5b (parity): store_session_config captures currentModelId into
    _resolved_model_id, so context-window backfill works even when the user
    never called set_model — and _model stays empty (no pinning)."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh.model_registry, "has_known_window", lambda mid: True)
    monkeypatch.setattr(sh.model_registry, "model_window", lambda mid, **kw: 300000)
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.store_session_config(
        {"models": {"currentModelId": "resolved-model", "availableModels": []}}
    )
    assert handle._resolved_model_id == "resolved-model"
    assert handle._model == ""  # must NOT pollute the user-picked model field
    handle._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 40},
            }
        )
    )
    assert handle.last_prompt_stats.context_window_tokens == 300000
    assert handle.last_prompt_stats.context_used_tokens == 120000


@pytest.mark.asyncio
async def test_send_command_redacts_output(monkeypatch):
    """#send_command (parity): the command response text is redacted before
    return, matching AcpClient.send_command."""
    import kiro_crew.acp.session_handle as sh

    # send_command now applies the explicit two-pass redactors (parity with
    # AcpClient.send_command), not the redact_text helper.
    monkeypatch.setattr(sh, "redact_exfiltration_urls", lambda s: (s, []))
    monkeypatch.setattr(sh, "redact_credentials", lambda s: ("REDACTED", []))
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")

    async def _fake_send_request(method, params, **_kw):
        return 1

    rt.send_request = _fake_send_request  # type: ignore[method-assign]
    handle = AcpSessionHandle("sA", q["sA"], rt)

    async def _fake_wait(req_id, timeout=60.0):
        return JsonRpcMessage.from_dict({"id": 1, "result": {"text": "secret token xyz"}})

    handle._wait_for_response = _fake_wait  # type: ignore[assignment]
    out = await handle.send_command("/compact")
    assert out == "REDACTED"


# ── Round-2 parity fixes: auth detection, exception translation, steer ──


@pytest.mark.asyncio
async def test_saw_not_logged_in_detects_auth_failure():
    """#1: AcpRuntime.saw_not_logged_in reports kiro-cli's auth-failure signal on
    stderr so a death can be surfaced as AcpAuthRequired.

    Drives the real ``_drain_stderr`` rather than assigning ``_stderr_lines``
    directly. The observation is now latched as each line arrives, because the
    buffer is a 20-line ring and nothing asks about auth until a request has
    already timed out -- by which point a chatty startup can have evicted the
    line. ``_drain_stderr`` is the only production writer of that buffer, so
    driving it is strictly closer to the real path than the previous assignment
    was; the two assertions below are unchanged in intent.
    """

    class _Stderr:
        def __init__(self, lines):
            self._lines = [f"{ln}\n".encode() for ln in lines]

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    async def _drain(lines):
        rt, _, proc = _make_runtime()
        proc.stderr = _Stderr(lines)
        await rt._drain_stderr()
        return rt

    rt = await _drain(["startup noise", "error: You are not logged in, please log in"])
    assert rt.saw_not_logged_in() is True
    rt = await _drain(["ordinary stderr", "mcp server ready"])
    assert rt.saw_not_logged_in() is False


@pytest.mark.asyncio
async def test_stream_translates_runtime_dead_to_process_died():
    """#2: AcpSessionProvider.stream translates AcpRuntimeDead (an
    AcpRuntimeError, which chat_runner does NOT catch) into AcpProcessDied so
    the caller's AcpProcessDied handler fires (parity with AcpClient)."""
    from kiro_crew.acp.client import AcpProcessDied
    from kiro_crew.acp.session_provider import AcpSessionProvider

    rt = MagicMock()
    rt.saw_not_logged_in = MagicMock(return_value=False)
    handle = MagicMock()

    async def _boom(msg):
        raise AcpRuntimeDead("pipe broken")
        yield  # noqa: mark as async generator

    handle.prompt = _boom
    prov = AcpSessionProvider.__new__(AcpSessionProvider)
    prov._handle = handle
    prov._runtime = rt
    with pytest.raises(AcpProcessDied):
        async for _ in prov.stream("hi"):
            pass


@pytest.mark.asyncio
async def test_stream_translates_auth_failure_to_auth_required():
    """#1: when stderr shows 'not logged in', a runtime death surfaces as
    AcpAuthRequired (non-retryable login prompt) rather than AcpProcessDied."""
    from kiro_crew.acp.client import AcpAuthRequired
    from kiro_crew.acp.session_provider import AcpSessionProvider

    rt = MagicMock()
    rt.saw_not_logged_in = MagicMock(return_value=True)
    handle = MagicMock()

    async def _boom(msg):
        raise AcpRuntimeDead("pipe broken")
        yield

    handle.prompt = _boom
    prov = AcpSessionProvider.__new__(AcpSessionProvider)
    prov._handle = handle
    prov._runtime = rt
    with pytest.raises(AcpAuthRequired):
        async for _ in prov.stream("hi"):
            pass


@pytest.mark.asyncio
async def test_handle_steer_sends_session_steer():
    """#3: outbound steer() wraps the message and sends _session/steer; empty
    message or no session returns False without sending."""
    sent = {}

    async def _send_request(method, params):
        sent["method"] = method
        sent["params"] = params
        return 1

    rt = MagicMock()
    rt.send_request = _send_request
    # A real backend id, not a MagicMock attribute: supports_steer is membership
    # in ACP_BACKENDS_STEER, so the host has to be named for it to answer.
    rt.acp_backend = ACP_BACKEND_KIRO
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt)
    assert handle.supports_steer is True
    assert handle.last_steer_monotonic == 0.0  # never steered
    ok = await handle.steer("please focus on X")
    assert ok is True
    assert sent["method"] == "_session/steer"
    assert "please focus on X" in sent["params"]["message"]
    assert await handle.steer("   ") is False


@pytest.mark.asyncio
async def test_handle_steer_stamps_write_time_and_provider_passes_it_through():
    """The stamp lives at the innermost write because that is the one point
    every steer funnels through — the dashboard steers the client directly
    while the IM transports steer the provider wrapper. The dashboard's
    keepalive route reads it to decide whether a sleeping `wait` should return
    early, so a refused steer must not move it.
    """
    rt = MagicMock()

    async def _send_request(method, params):
        return 1

    rt.send_request = _send_request
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt)
    from kiro_crew.acp.session_provider import AcpSessionProvider

    prov = AcpSessionProvider.__new__(AcpSessionProvider)
    prov._handle = handle
    prov._runtime = rt

    assert prov.last_steer_monotonic == 0.0
    before = time.monotonic()
    assert await handle.steer("focus on X") is True
    after = time.monotonic()
    stamped = handle.last_steer_monotonic
    assert before <= stamped <= after
    # The wrapper the IM transports hold must report the same fact.
    assert prov.last_steer_monotonic == stamped

    # A refused steer (empty text) never reached the wire, so it must not
    # look newer than the sleep it would otherwise cut short.
    assert await handle.steer("  ") is False
    assert handle.last_steer_monotonic == stamped


# ── Round-3 fixes: cancel_session grace + idempotent cancel ──


@pytest.mark.asyncio
async def test_provider_cancel_session_accepts_and_forwards_grace():
    """Blocker #1: AcpSessionProvider.cancel_session must accept grace_secs
    (AcpProvider.cancel calls it with grace_secs=) and forward it to the
    handle — otherwise a kiro-path cancel raises TypeError."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    rt, _, _ = _make_runtime()
    rt.send_notification = AsyncMock()  # type: ignore[method-assign]
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    prov = AcpSessionProvider.__new__(AcpSessionProvider)
    prov._handle = handle
    prov._runtime = rt
    await prov.cancel_session(grace_secs=25.0)  # must NOT raise TypeError
    assert handle._cancel_grace_secs == 25.0


def test_is_turn_active_factors_cancelled():
    """is_turn_active is False once cancel() has fired (parity with
    AcpClient.has_active_turn) so a repeat cancel is a no-op early-return."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._turn_done.clear()
    handle._cancelled = False
    assert handle.is_turn_active is True
    handle._cancelled = True
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_dispatch_mcp_oauth_guard_and_dedup():
    """Shared-path mcp_oauth_request mirrors AcpClient (R5 fix): unsafe-scheme
    URLs and empty serverName are dropped; duplicates deduped; a matching
    server_initialized discards the dedupe entry so a later retry re-emits."""
    from kiro_crew.acp.types import (
        EVENT_MCP_OAUTH_REQUEST,
        METHOD_MCP_OAUTH_REQUEST,
        METHOD_MCP_SERVER_INITIALIZED,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        base = {"sessionId": "sA"}
        # unsafe scheme -> dropped
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {**base, "serverName": "evil", "oauthUrl": "javascript:alert(1)"},
            },
        )
        # empty serverName -> dropped
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {**base, "serverName": "", "oauthUrl": "https://ok.example.com"},
            },
        )
        # safe -> emitted
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {**base, "serverName": "gh", "oauthUrl": "https://auth.example.com"},
            },
        )
        # duplicate same server -> deduped
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {**base, "serverName": "gh", "oauthUrl": "https://auth.example.com"},
            },
        )
        # server_initialized -> discard dedupe entry
        _feed(
            reader,
            {"method": METHOD_MCP_SERVER_INITIALIZED, "params": {**base, "serverName": "gh"}},
        )
        # safe again after discard -> re-emitted
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {**base, "serverName": "gh", "oauthUrl": "https://auth.example.com"},
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")

        oauth = [e for e in events if e.kind == EVENT_MCP_OAUTH_REQUEST]
        # evil (unsafe) + empty-name dropped; gh emitted, deduped, then re-emitted = 2
        assert [e.server_name for e in oauth] == ["gh", "gh"], [e.server_name for e in oauth]
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_is_turn_active_requires_alive_runtime():
    """Contract parity: a turn on a DEAD runtime reads inactive (mirrors
    AcpClient.has_active_turn's process-alive condition)."""
    rt = MagicMock()
    rt.is_alive.return_value = True
    h = AcpSessionHandle("sA", asyncio.Queue(), rt)
    h._turn_done.clear()
    h._cancelled = False
    assert h.is_turn_active is True
    rt.is_alive.return_value = False
    assert h.is_turn_active is False


@pytest.mark.asyncio
async def test_set_model_syncs_resolved_model_id():
    """Contract parity: set_model updates BOTH _model and _resolved_model_id
    (else context-window backfill uses the stale session/new model)."""
    rt = MagicMock()
    rt.is_alive.return_value = True
    queue: asyncio.Queue = asyncio.Queue()
    rt.send_request = _replying_send(lambda _p: queue)
    h = AcpSessionHandle("sA", queue, rt)
    h._turn_done.set()
    await h.set_model("new-model")
    assert h._model == "new-model"
    assert h._resolved_model_id == "new-model"


@pytest.mark.asyncio
async def test_set_model_rebases_context_stats(monkeypatch):
    """Contract parity with AcpClient.set_model: a mid-session switch re-anchors
    last_prompt_stats to the new model's window and clears the authoritative
    usage flag, so the next metadata pct backfills against the NEW model
    instead of being gated forever by the old model's usage_update."""
    from kiro_crew import model_registry

    monkeypatch.setattr(model_registry, "has_known_window", lambda mid: True)
    monkeypatch.setattr(model_registry, "model_window", lambda mid, **kw: 272_000)
    rt = MagicMock()
    rt.is_alive.return_value = True
    queue: asyncio.Queue = asyncio.Queue()
    rt.send_request = _replying_send(lambda _p: queue)
    h = AcpSessionHandle("sA", queue, rt)
    h._turn_done.set()
    h.last_prompt_stats.context_used_tokens = 100_000
    h.last_prompt_stats.context_window_tokens = 1_000_000
    h.last_prompt_stats.context_pct = 10.0
    h.last_prompt_stats.context_tokens_from_usage = True

    await h.set_model("new-model")

    stats = h.last_prompt_stats
    assert stats.context_window_tokens == 272_000
    assert stats.context_used_tokens == 100_000
    assert stats.context_pct == round(100_000 / 272_000 * 100, 1)
    assert stats.context_tokens_from_usage is False


def test_normalize_models_shape():
    """Contract parity: available_models normalized to {modelId,name,description}
    with guaranteed keys (mirrors AcpClient._capture_available_models)."""
    out = AcpSessionHandle._normalize_models(
        [
            {"modelId": "m1", "name": "Model One", "description": "d"},
            {"value": "m2"},  # value fallback; name defaults to id
            {"name": "no-id"},  # dropped: no id
            "garbage",  # dropped: not a dict
        ]
    )
    assert out == [
        {"modelId": "m1", "name": "Model One", "description": "d"},
        {"modelId": "m2", "name": "m2", "description": ""},
    ]


def test_store_session_config_syncs_effort_levels(monkeypatch):
    """Contract parity: store_session_config pushes effort levels to the global
    validation set (mirrors AcpClient._sync_effort_levels)."""
    import sys
    import types

    calls = []
    fake = types.ModuleType("kiro_crew.dashboard.chat_persistence")
    fake.update_reasoning_effort_values = lambda levels: calls.append(levels)
    monkeypatch.setitem(sys.modules, "kiro_crew.dashboard.chat_persistence", fake)
    rt = MagicMock()
    rt.is_alive.return_value = True
    h = AcpSessionHandle("sA", asyncio.Queue(), rt)
    h.store_session_config(
        {"configOptions": [{"id": "effort", "options": [{"value": "low"}, {"value": "high"}]}]}
    )
    assert calls == [["low", "high"]]


@pytest.mark.asyncio
async def test_stale_turn_probes_then_signals_recovery():
    """A stale turn probed via session/cancel that never acks within the grace
    window is a confirmed wedge → the shared-runtime handle yields
    EVENT_COMPLETE(STOP_REASON_STALE_RECOVER) so the dashboard auto-recovers
    (reset+resume+continue-nudge). Replaces the former stale->end_turn behavior,
    which orphaned the wedged turn until the user's next message collided with
    'prompt already in progress'. (Stale DETECTION → probe is covered by
    test_acp_stale_recovery.py::test_genuine_stale_probes_via_cancel.)"""
    from kiro_crew.acp.types import EVENT_COMPLETE, STOP_REASON_STALE_RECOVER

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._turn_done.clear()  # a turn is in flight (cleared by prompt() in prod)
    # A genuine stale turn was probed via session/cancel; the grace window has
    # elapsed with no ack (confirmed wedge). The unresponsive-cancel branch runs
    # at the loop top, before any queue read, so this is deterministic.
    handle._stale_probe = True
    handle._cancelled = True
    handle._cancel_ts = time.monotonic() - 1.0
    handle._cancel_grace_secs = 0.05

    events = [ev async for ev in handle._dispatch_events(req_id=1, timeout=5.0)]

    assert events and events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == STOP_REASON_STALE_RECOVER
    assert handle._turn_done.is_set()


@pytest.mark.asyncio
async def test_mark_dead_clears_routed_requests():
    """R7 fix: _mark_dead clears _routed_requests (not just _pending_requests) so
    a routed-request correlation can't linger past runtime death."""
    rt, _, _ = _make_runtime()
    rt._routed_requests[42] = "sA"
    rt._pending_requests[7] = asyncio.get_event_loop().create_future()
    rt._mark_dead("test")
    assert rt._routed_requests == {}
    assert rt._pending_requests == {}


def test_build_permission_event_sets_raw_tool_params():
    """Regression (PR #21 HIGH): the shared build_permission_event must carry
    raw_tool_params (the structured dict cached from the preceding tool_call) so
    the governance keystone (hooks.on_tool_call sensitive-path / write-protected
    checks) enforces on the shared-runtime path even when the display title
    hides the path."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    raw_cache = {"tc-1": {"path": "/home/u/.ssh/id_rsa", "content": "x"}}
    msg = JsonRpcMessage.from_dict(
        {
            "id": 5,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-1", "title": "Editing"},
                "options": [],
            },
        }
    )
    event, _recorded = build_permission_event(msg, raw_params_cache=raw_cache)
    assert event.raw_tool_params == {"path": "/home/u/.ssh/id_rsa", "content": "x"}
    # RETAINED on use (.get, matching the sibling caches): a second permission
    # frame for the same toolCallId (re-ask after reject_once, re-prompt after
    # a mode change) must still find the params — the per-turn dispatch
    # .clear() handles cleanup.
    assert "tc-1" in raw_cache
    event2, _ = build_permission_event(msg, raw_params_cache=raw_cache)
    assert event2.raw_params_trusted is True


def test_build_permission_event_raw_params_none_without_cache():
    """No cache entry + no inline dict → raw_tool_params stays None (no crash)."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    msg = JsonRpcMessage.from_dict(
        {
            "id": 6,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {"toolCall": {"toolCallId": "tc-x", "title": "Editing"}, "options": []},
        }
    )
    event, _ = build_permission_event(msg, raw_params_cache={})
    assert event.raw_tool_params is None


@pytest.mark.parametrize("redaction_cache", [None, {}])
def test_cached_input_without_redaction_provenance_fails_closed(redaction_cache):
    """Unknown cached-input provenance may display, but cannot grant trust."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    msg = JsonRpcMessage.from_dict(
        {
            "id": 61,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-legacy", "title": "Legacy cached tool"},
                "options": [],
            },
        }
    )
    cached = '{"command": "echo [REDACTED: credential]"}'

    event, _ = build_permission_event(
        msg,
        tool_input_cache={"tc-legacy": cached},
        tool_input_redacted_cache=redaction_cache,
    )

    assert event.tool_input == cached
    assert event.tool_input_redacted is True


def test_build_permission_event_recovers_mcp_server_name_from_cache():
    """Regression: build_permission_event must carry mcp_server_name recovered
    from the preceding tool_call (the permission payload has no _meta), so
    hooks.on_tool_call's app-own-server auto-approve can fire on the dashboard
    permission path. Without this the event's mcp_server_name is always "" and
    the feature is inert."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    mcp_cache = {"tc-1": "mochi:mochi"}
    msg = JsonRpcMessage.from_dict(
        {
            "id": 7,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-1", "title": "perform_pet_action"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(msg, mcp_server_name_cache=mcp_cache)
    assert event.mcp_server_name == "mochi:mochi"
    # .get() (not .pop()): a later tool_call_update for the same id re-reads it.
    assert mcp_cache.get("tc-1") == "mochi:mochi"


def test_build_permission_event_mcp_server_name_empty_without_cache():
    """No cache / no entry → mcp_server_name stays "" (fail-closed: the app-own
    auto-approve never matches on a forged title with no trusted server name)."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    msg = JsonRpcMessage.from_dict(
        {
            "id": 8,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {"toolCall": {"toolCallId": "tc-y", "title": "x"}, "options": []},
        }
    )
    event, _ = build_permission_event(msg, mcp_server_name_cache={})
    assert event.mcp_server_name == ""


def test_build_permission_event_recovers_tool_name_from_cache():
    """Mirror of the mcp_server_name recovery: the permission payload carries no
    _meta, so build_permission_event recovers the trusted tool name from the
    preceding tool_call via tool_name_cache. This is what lets the
    app-own-server auto-approve rebuild the canonical mcp__<server>__<tool> and
    govern the real tool on the permission path."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    name_cache = {"tc-1": "perform_pet_action"}
    msg = JsonRpcMessage.from_dict(
        {
            "id": 9,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-1", "title": "perform_pet_action"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(msg, tool_name_cache=name_cache)
    assert event.tool_name == "perform_pet_action"
    # Replay provenance belongs to EVENT_TOOL_CALL, the only event shape the
    # dashboard recovery collector reads. Permission events keep their separate
    # pair-provenance contract and must not mint this unused flag.
    assert event.tool_identity_trusted is False
    # .get() (not .pop()): a later tool_call_update for the same id re-reads it.
    assert name_cache.get("tc-1") == "perform_pet_action"


def test_build_permission_event_tool_name_empty_without_cache():
    """No cache / no entry → tool_name stays "" (fail-closed: the app-own-server
    auto-approve cannot identify the tool to govern it, so it never fires)."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    msg = JsonRpcMessage.from_dict(
        {
            "id": 10,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {"toolCall": {"toolCallId": "tc-z", "title": "x"}, "options": []},
        }
    )
    event, _ = build_permission_event(msg, tool_name_cache={})
    assert event.tool_name == ""


def test_shared_handle_permission_inherits_origin_bound_tool_identity():
    """Shared-runtime transport carries identity through its real cache path."""
    rt, _, _ = _make_runtime()
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt)
    tool_events = handle._handle_update(
        JsonRpcMessage.from_dict(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-shared",
                        "title": "Shared model-authored title",
                        "kind": "other",
                        "rawInput": {},
                        "_meta": {
                            "kiro": {
                                "toolName": "delete_record",
                                "mcpServerName": "records:primary",
                            }
                        },
                    },
                },
            }
        )
    )
    assert tool_events and tool_events[0].tool_name == "delete_record"

    permission = handle._build_permission_event(
        JsonRpcMessage.from_dict(
            {
                "id": 11,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "sA",
                    "toolCall": {
                        "toolCallId": "tc-shared",
                        "title": "Shared model-authored title",
                    },
                    "options": [],
                },
            }
        )
    )

    assert permission.tool_name == "delete_record"
    assert permission.mcp_server_name == "records:primary"


def test_shared_handle_structured_non_shell_reprompt_keeps_argument_provenance():
    """Shared transport retains display and raw params across a re-prompt."""
    from kiro_crew.trust_patterns import approval_command

    rt, _, _ = _make_runtime()
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt)
    handle._handle_update(
        JsonRpcMessage.from_dict(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-shared-args",
                        "title": "Looking up the record",
                        "kind": "other",
                        "rawInput": {"record_id": "sensitive-record"},
                        "_meta": {
                            "kiro": {
                                "toolName": "read_record",
                                "mcpServerName": "records:primary",
                            }
                        },
                    },
                },
            }
        )
    )
    request = JsonRpcMessage.from_dict(
        {
            "id": 12,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sA",
                "toolCall": {
                    "toolCallId": "tc-shared-args",
                    "title": "Looking up the record",
                },
                "options": [],
            },
        }
    )

    first = handle._build_permission_event(request)
    repeated = handle._build_permission_event(request)

    assert first.tool_input
    assert repeated.tool_input == first.tool_input
    assert repeated.raw_tool_params == {"record_id": "sensitive-record"}
    assert (
        approval_command(
            repeated.tool_input,
            is_shell=repeated.is_shell,
            tool_name=repeated.tool_name,
            mcp_server_name=repeated.mcp_server_name,
            raw_tool_params=repeated.raw_tool_params,
        )
        == ""
    )


@pytest.mark.parametrize("raw_input", ["/etc/secret", ["/etc/secret"]])
def test_shared_non_dict_non_shell_reprompt_cannot_become_durable_tool_trust(raw_input):
    """String/list rawInput remains visible to the repeat trust gate."""
    from kiro_crew.trust_patterns import approval_command

    rt, _, _ = _make_runtime()
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt)
    handle._handle_update(
        JsonRpcMessage.from_dict(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-shared-nondict",
                        "title": "Reading a path",
                        "kind": "read",
                        "rawInput": raw_input,
                        "_meta": {"kiro": {"toolName": "read_path", "mcpServerName": "files"}},
                    },
                },
            }
        )
    )
    request = JsonRpcMessage.from_dict(
        {
            "id": 13,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sA",
                "toolCall": {
                    "toolCallId": "tc-shared-nondict",
                    "title": "Reading a path",
                },
                "options": [],
            },
        }
    )

    first = handle._build_permission_event(request)
    repeated = handle._build_permission_event(request)

    assert repeated.tool_input == first.tool_input
    assert repeated.tool_input
    assert (
        approval_command(
            repeated.tool_input,
            is_shell=repeated.is_shell,
            tool_name=repeated.tool_name,
            mcp_server_name=repeated.mcp_server_name,
            raw_tool_params=repeated.raw_tool_params,
        )
        == ""
    )


def test_build_permission_event_non_string_option_entries_skipped():
    """The shared parser feeds AcpSessionHandle's prompt event generator; a
    truthy non-string id (e.g. {"id": 42}) crashed opt_id.lower() in the
    legacy-kind synthesis, tearing down the turn on the shared-runtime
    transport — the same class of crash AcpClient's copy guards against.
    Non-dict entries and non-string label/kind must be skipped/coerced while
    valid entries still parse."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    msg = JsonRpcMessage.from_dict(
        {
            "id": 7,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-y", "title": "shell"},
                "options": [
                    "allow",  # non-dict
                    None,  # non-dict
                    {"id": 42, "label": "int id"},  # non-string id → skipped
                    {"id": "allow_once", "label": 7, "kind": ["x"]},  # coerced
                    {"id": "allow_always", "label": "Always"},
                ],
            },
        }
    )
    event, recorded = build_permission_event(msg, raw_params_cache={})  # must not raise
    assert event.options == [
        {"id": "allow_once", "label": ""},
        {"id": "allow_always", "label": "Always"},
    ]
    assert recorded is not None


def test_mark_dead_unregisters_protected_pid():
    """Regression (PR #21 follow-up): _mark_dead must release the sweep-protection
    shield on ANY death path (not just kill()), else the dead PID lingers in
    _PROTECTED_PIDS forever and could shield a recycled-orphan from the sweep."""
    from kiro_crew.session_pid import _protected_pids, register_protected_pid

    rt, _, _ = _make_runtime()
    rt._pid = 515151
    register_protected_pid(rt._pid)
    assert rt._pid in _protected_pids()
    rt._mark_dead("simulated EOF")
    assert rt._pid not in _protected_pids()


def test_protected_runtime_pid_lands_in_sweep_active_set():
    """Companion (``_subagent_runtimes``) and background (``_bg_runtime``)
    AcpRuntimes live only in SessionManager instance attributes, NOT in
    ``self._sessions``, so ``_collect_active_pids`` cannot see them via a session
    provider. They stay protected only because ``AcpRuntime.spawn()`` shields
    their PID via ``register_protected_pid``, and ``_collect_active_pids`` seeds
    from ``_protected_pids()``. This asserts both a companion and a bg runtime
    PID land in the sweep's active set (so phase-2 never confirms them orphans) —
    the Kiro Crew analog of the upstream project's end-to-end guard.
    """
    from kiro_crew.session_pid import (
        _collect_active_pids,
        register_protected_pid,
        unregister_protected_pid,
    )

    companion_pid, bg_pid = 717171, 727272
    register_protected_pid(companion_pid)
    register_protected_pid(bg_pid)
    try:
        # Empty session map == neither runtime is a registered session; they are
        # shielded ONLY via the register_protected_pid path that spawn() uses.
        active, ok = _collect_active_pids({})
        assert ok
        assert companion_pid in active
        assert bg_pid in active
    finally:
        unregister_protected_pid(companion_pid)
        unregister_protected_pid(bg_pid)

    # Once unregistered (runtime died), they are not shielded.
    active_after, _ = _collect_active_pids({})
    assert companion_pid not in active_after
    assert bg_pid not in active_after


def test_periodic_sweep_skips_protected_runtime_pid():
    """Reproduce the exact orphan-sweep path for a live companion/bg runtime: its
    kiro-cli PID is tagged in ``kiro_session_pids.txt`` and is NOT a registered
    session, so it would be confirmed an orphan and SIGKILLed — except the sweep
    wires ``is_managed = (pid in active_pids)`` and ``active_pids`` includes
    ``_protected_pids()``. With the runtime's PID registered (as ``spawn`` does),
    ``_sweep_pid_entries`` skips it (0 killed, entry retained).
    """
    from unittest.mock import patch

    from kiro_crew.session_pid import (
        _collect_active_pids,
        _sweep_pid_entries,
        register_protected_pid,
        unregister_protected_pid,
    )

    runtime_pid = 969696
    register_protected_pid(runtime_pid)
    try:
        active, ok = _collect_active_pids({})
        assert ok and runtime_pid in active
        with patch("os.kill", side_effect=lambda pid, sig: None):  # all alive
            killed, dead, _ = _sweep_pid_entries(
                [f"1:{runtime_pid}"],
                should_skip_tagged=lambda gw, p: False,
                should_skip_bare=lambda p: False,
                is_managed=lambda p: p in active,  # mirrors the real periodic sweep
            )
        assert killed == 0
        assert f"1:{runtime_pid}" not in dead
    finally:
        unregister_protected_pid(runtime_pid)


@pytest.mark.asyncio
async def test_runtime_spawn_scrubs_sensitive_env_on_default_auto(monkeypatch):
    """AcpRuntime.spawn applies the full ACP child scrub on the default tier.

    This parent-side enforcement is what protects raw Windows Kiro delegation;
    POSIX launchers apply the same sensitive/Python scrub inline.
    """
    import kiro_crew.acp.runtime as runtime_mod

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0000:FAKE-telegram")
    monkeypatch.setenv("WECOM_BOT_ID", "FAKE-wecom-bot")
    monkeypatch.setenv("WECOM_SECRET", "FAKE-wecom-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-FAKE")
    monkeypatch.setenv("KIROCREW_OWNER_ID", "U_FAKE_OWNER")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FAKE-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    monkeypatch.setenv("PYTHONPATH", "/gateway/pythonpath")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/gateway/pycache")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FAKE-akid")
    monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")

    captured: dict[str, object] = {}

    class _StopSpawn(Exception):
        pass

    async def _fake_exec(*_args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise _StopSpawn()

    async def resolve_kiro_bin(*, environ=None, home=None):
        return "/fake/kiro"

    client_mod = _spawn_client_mod()
    monkeypatch.setattr(
        client_mod,
        "_resolve_kiro_bin_for_spawn",
        resolve_kiro_bin,
    )
    monkeypatch.setattr(
        runtime_mod,
        "wrap_argv",
        lambda argv, mode, strip_python_env=False, is_kiro_cli=None, **_kw: (argv, None),
    )
    monkeypatch.setattr(runtime_mod, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(runtime_mod, "augmented_path", lambda p: p)
    monkeypatch.setattr(runtime_mod, "resolve_krb5_ccname", lambda env: None)
    monkeypatch.setattr(runtime_mod, "create_subprocess_limited", _fake_exec)

    rt = AcpRuntime(sandbox_mode="auto")  # default tier
    with pytest.raises(_StopSpawn):
        await rt.spawn()

    env = captured["env"]
    assert isinstance(env, dict)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "WECOM_BOT_ID",
        "WECOM_SECRET",
        "SLACK_BOT_TOKEN",
        "KIROCREW_OWNER_ID",
        "AWS_SECRET_ACCESS_KEY",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
        "PYTHONDONTWRITEBYTECODE",
    ):
        assert key not in env, f"{key} leaked into runtime child env"
    # The default tier leaves ~/.ssh readable, so the agent socket is kept:
    # git-over-SSH works while every other credential above stays scrubbed.
    if sys.platform == "win32":
        # Windows has no SSH_AUTH_SOCK (a named-pipe agent): never forwarded.
        assert "SSH_AUTH_SOCK" not in env, "SSH_AUTH_SOCK leaked into runtime child env"
    else:
        assert env.get("SSH_AUTH_SOCK") == "/tmp/fake-agent.sock"
    assert env.get("KIROCREW_UNRELATED_KEEPME") == "keep-this-value"
    assert env.get("AWS_ACCESS_KEY_ID") == "FAKE-akid"
    assert env.get("KIROCREW_RUNTIME_PYTHON") == runtime_mod.sys.executable


@pytest.mark.asyncio
async def test_runtime_spawn_names_its_own_browser_session(monkeypatch):
    """A subagent gets its own playwright-cli browser, not the parent's.

    AcpRuntime builds its child environment independently of AcpClient, so this
    is the drift guard: without it a subagent's ``goto`` lands in whatever page
    the parent was reading, and its ``close`` takes the parent's browser down.
    """
    import kiro_crew.acp.runtime as runtime_mod

    monkeypatch.delenv("PLAYWRIGHT_CLI_SESSION", raising=False)
    captured: dict[str, object] = {}

    class _StopSpawn(Exception):
        pass

    async def _fake_exec(*_args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise _StopSpawn()

    async def resolve_kiro_bin(*, environ=None, home=None):
        return "/fake/kiro"

    monkeypatch.setattr(_spawn_client_mod(), "_resolve_kiro_bin_for_spawn", resolve_kiro_bin)
    monkeypatch.setattr(
        runtime_mod,
        "wrap_argv",
        lambda argv, mode, strip_python_env=False, is_kiro_cli=None, **_kw: (argv, None),
    )
    monkeypatch.setattr(runtime_mod, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(runtime_mod, "augmented_path", lambda p: p)
    monkeypatch.setattr(runtime_mod, "resolve_krb5_ccname", lambda env: None)
    monkeypatch.setattr(runtime_mod, "create_subprocess_limited", _fake_exec)

    names = []
    for _ in range(2):
        rt = AcpRuntime(sandbox_mode="auto")
        with pytest.raises(_StopSpawn):
            await rt.spawn()
        env = captured["env"]
        assert isinstance(env, dict)
        names.append(env["PLAYWRIGHT_CLI_SESSION"])

    assert all(name.startswith("kc-") for name in names)
    assert names[0] != names[1]


class TestToolPurposeExtraction:
    """The reserved purpose arg is what the dashboard's concise tool pill shows
    instead of the literal invocation. kiro-cli echoes it back under EITHER
    spelling, so the shared runtime path must accept both — matching only the
    snake_case key silently degraded half the pills to raw command text."""

    def _update(self, raw_input: dict) -> dict:
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-purpose",
            "kind": "execute",
            "title": "Running: node kc-shot.mjs",
            "rawInput": raw_input,
        }

    def test_snake_case_key(self):
        from kiro_crew.acp._dispatch import _build_tool_call_event

        event = _build_tool_call_event(
            self._update({"command": "node kc-shot.mjs", "__tool_use_purpose": "check harness"}),
            None,
        )
        assert event.tool_purpose == "check harness"

    def test_camel_case_key(self):
        from kiro_crew.acp._dispatch import _build_tool_call_event

        event = _build_tool_call_event(
            self._update({"command": "node kc-shot.mjs", "__toolUsePurpose": "check harness"}),
            None,
        )
        assert event.tool_purpose == "check harness"

    def test_no_purpose_key_yields_empty(self):
        from kiro_crew.acp._dispatch import _build_tool_call_event

        event = _build_tool_call_event(self._update({"command": "node kc-shot.mjs"}), None)
        assert event.tool_purpose == ""

    def test_blank_and_non_string_values_ignored(self):
        from kiro_crew.acp._dispatch import extract_tool_purpose

        assert extract_tool_purpose({"__tool_use_purpose": "   "}) == ""
        assert extract_tool_purpose({"__toolUsePurpose": 123}) == ""
        assert extract_tool_purpose("not a dict") == ""
        # A blank snake_case value must not shadow a real camelCase one.
        assert (
            extract_tool_purpose({"__tool_use_purpose": "", "__toolUsePurpose": "real"}) == "real"
        )
