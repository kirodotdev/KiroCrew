"""A codex session whose agent spec declares ``"tools": []`` refuses every permission request.

codex is served by ``AcpRuntime`` / ``AcpSessionHandle``, not ``AcpClient``, and its
mirror turns ``tools`` into an MCP allowlist only: codex keeps its own shell and file
tools. The runtime copies the mirror's ``zero_tools`` onto the handle as
``spec_zero_tools`` on ``session/new`` and ``session/load``, and the handle's dispatch
loop answers every ``session/request_permission`` with the adapter's reject option
before any consumer sees it.

The ban is partial: codex-acp 2.3.0 asks nothing before a passive read, a hosted web
search or a command its sandbox allows, and codex runs a self-approved tool of an MCP
server in the operator's own ``config.toml``. So codex stays out of
``ACP_BACKENDS_HONOR_ZERO_TOOL_BAN``, and a new session says so in one notice row.

The permission frames are shaped as codex-acp 2.3.0 builds them: the command request is
the one in ``test/fixtures/acp_frames/codex/session-live.jsonl``, the file change is
``FileChangeReporter.permission`` with ``fileChangeDecisionOptions``, and the MCP approval
is ``buildMcpPermissionRequest`` after its ``tool_call`` frame.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import codex_session_mcp_helpers
import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import (
    _await_routed,
    _feed,
    _make_runtime,
    _register,
    _start_reader,
    _stop_reader,
)
from codex_session_mcp_helpers import _CODEX_1_11_CAPS, _CORE, _write_spec

from kiro_crew.acp import runtime as acp_runtime
from kiro_crew.acp import session_handle_permissions
from kiro_crew.acp.runtime import AcpRuntime, _MirroredSessionMcp
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    EVENT_PERMISSION_REQUEST,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_HONOR_ZERO_TOOL_BAN
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until

# The shared fixture, bound here by name so pytest registers it for this module.
agents_dir = codex_session_mcp_helpers.agents_dir

_CODEX_OPTIONS = [
    {"optionId": "allow_once", "name": "Yes, proceed", "kind": "allow_once"},
    {
        "optionId": "allow_for_session",
        "name": "Yes, and don't ask again for these files",
        "kind": "allow_always",
    },
    {
        "optionId": "cancel",
        "name": "No, and tell Codex what to do differently",
        "kind": "reject_once",
    },
]
_REJECTED = {"outcome": "selected", "optionId": "cancel"}


def _execute_request(request_id: int, session_id: str = "sA") -> dict:
    """codex-acp's command approval (``handleCommandExecution``), as captured live."""
    return {
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": session_id,
            "toolCall": {
                "toolCallId": f"call_{request_id}",
                "title": "Run command",
                "rawInput": {"command": "touch /w/escape.txt", "cwd": "/w"},
                "kind": "execute",
                "status": "pending",
            },
            "options": [
                {"optionId": "allow_once", "name": "Yes, proceed", "kind": "allow_once"},
                {
                    "optionId": "accept_execpolicy_amendment",
                    "name": "Yes, and don't ask again for commands that start with `touch`",
                    "kind": "allow_always",
                },
                {
                    "optionId": "cancel",
                    "name": "No, and tell Codex what to do differently",
                    "kind": "reject_once",
                },
            ],
        },
    }


def _file_change_request(request_id: int) -> dict:
    """codex-acp's file change approval (``handleFileChange``)."""
    return {
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "sA",
            "toolCall": {
                "toolCallId": f"item_{request_id}",
                "title": "Editing files",
                "kind": "edit",
                "status": "pending",
                "locations": [{"path": "/w/notes.txt"}],
            },
            "options": _CODEX_OPTIONS,
        },
    }


def _mcp_tool_call(call_id: str) -> dict:
    """The ``tool_call`` frame codex-acp emits before an MCP approval."""
    return {
        "method": "session/update",
        "params": {
            "sessionId": "sA",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "kind": "execute",
                "title": "mcp.kirocrew-core.send_message",
                "status": "pending",
                "rawInput": {"server": "kirocrew-core", "tool": "send_message", "arguments": {}},
                "_meta": {"is_mcp_tool_call": True},
            },
        },
    }


def _mcp_request(request_id: int, call_id: str) -> dict:
    """codex-acp's MCP approval (``buildMcpPermissionRequest``)."""
    return {
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "sA",
            "toolCall": {"toolCallId": call_id, "kind": "execute", "status": "pending"},
            "_meta": {"is_mcp_tool_approval": True},
            "options": [
                {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                {
                    "optionId": "allow_session",
                    "name": "Allow for this session",
                    "kind": "allow_always",
                },
                {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
            ],
        },
    }


def _subagent_spawned(parent: str, child: str) -> dict:
    """The parent-session announcement that routes *child*'s frames to *parent*."""
    return {
        "method": "session/update",
        "params": {
            "sessionId": parent,
            "update": {
                "sessionUpdate": "subagent_spawned",
                "subagentSessionId": child,
                "name": "Probe worker",
                "task": "Delegated task",
                "capabilities": {},
            },
        },
    }


@pytest.fixture
def audited(monkeypatch) -> list[dict]:
    """The SEL records the handle writes, in place of the real SEL."""
    records: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            records.append(kw)

    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    return records


async def _turn_with(
    frames: list[dict], *, zero_tools: bool, child_fidelity_aware: bool = False
) -> tuple[list, Any, AcpSessionHandle]:
    """Drive one real dispatch-loop turn of a codex handle over *frames*."""
    rt, reader, proc = _make_runtime()
    rt._acp_backend = ACP_BACKEND_CODEX
    queues = _register(rt, "sA")
    handle = AcpSessionHandle("sA", queues["sA"], rt)
    # What AcpRuntime sets from the mirror's projection.
    handle.spec_zero_tools = zero_tools
    # The dashboard opts in, so a child's request would otherwise reach its gate.
    handle.child_fidelity_aware = child_fidelity_aware
    task = await _start_reader(rt)
    events: list = []
    try:

        async def drive():
            async for ev in handle.prompt("hi", timeout=FALLBACK_TIMEOUT_SECS):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        prompt_id = (await _await_routed(rt, "sA"))["sA"]
        for frame in frames:
            _feed(reader, frame)
        _feed(reader, {"id": prompt_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        if handle._audit_tasks:
            await asyncio.gather(*list(handle._audit_tasks))
    finally:
        await _stop_reader(task)
    return events, proc, handle


def _answers(proc: Any, request_id: int) -> list[dict]:
    """Every response the handle wrote for *request_id*."""
    out = []
    for call in proc.stdin.write.call_args_list:
        frame = json.loads(call.args[0].decode())
        if frame.get("id") == request_id and "method" not in frame:
            out.append(frame)
    return out


# ── The refusal, through the real dispatch loop ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frames", "request_id", "audited_title"),
    [
        pytest.param([_execute_request(7101)], 7101, "Run command", id="execute"),
        pytest.param([_file_change_request(7102)], 7102, "Editing files", id="file_change"),
        pytest.param(
            [_mcp_tool_call("mcp-1"), _mcp_request(7103, "mcp-1")],
            7103,
            "mcp__kirocrew-core__send_message",
            id="mcp",
        ),
    ],
)
async def test_a_zero_tool_codex_session_refuses_every_permission_request(
    audited, frames, request_id, audited_title
):
    events, proc, _handle = await _turn_with(frames, zero_tools=True)

    # Nothing is yielded, so no hook, trust grant or auto-approve can answer it.
    assert [e for e in events if e.kind == EVENT_PERMISSION_REQUEST] == []
    answers = _answers(proc, request_id)
    assert answers, "the request must be answered, never dropped: a dropped one hangs"
    # The adapter's own reject option, as the spec-disabled refusal answers.
    assert [a["result"]["outcome"] for a in answers] == [_REJECTED]
    assert [(r["error"], r["outcome"], r["tool_name"]) for r in audited] == [
        ("spec_zero_tools", "denied", audited_title)
    ]


@pytest.mark.asyncio
async def test_a_native_childs_request_on_the_same_handle_is_refused(audited):
    """A codex sub-agent's request is routed to its parent's handle under the child's
    sessionId. The parent's spec governs it, so it is refused too, also for a consumer
    that takes child requests to its own gate."""
    events, proc, _handle = await _turn_with(
        [_subagent_spawned("sA", "child-1"), _execute_request(7104, session_id="child-1")],
        zero_tools=True,
        child_fidelity_aware=True,
    )

    assert [e for e in events if e.kind == EVENT_PERMISSION_REQUEST] == []
    assert [a["result"]["outcome"] for a in _answers(proc, 7104)] == [_REJECTED]
    assert [(r["error"], r["session_key"]) for r in audited] == [
        ("spec_zero_tools", "acp:sA:child-1")
    ]


@pytest.mark.asyncio
async def test_a_codex_session_whose_spec_declares_tools_is_not_affected(audited):
    events, proc, _handle = await _turn_with([_execute_request(7105)], zero_tools=False)

    perms = [e for e in events if e.kind == EVENT_PERMISSION_REQUEST]
    assert [p.request_id for p in perms] == [7105]
    assert _answers(proc, 7105) == []
    assert audited == []


# ── The flag, carried from the mirror onto the handle ──


class _Plane:
    """The control plane one ``create_session`` or ``load_session`` puts on the wire."""

    def __init__(self, session_id: str) -> None:
        self._response = {
            "sessionId": session_id,
            "modes": {"currentModeId": "agent"},
            "configOptions": [
                {"id": "mode", "options": [{"value": "read-only"}, {"value": "agent"}]}
            ],
        }
        self.methods: list[str] = []

    def install(self, rt: AcpRuntime, stack: ExitStack) -> None:
        async def _send_and_await(method, params, timeout=None):
            self.methods.append(method)
            return self._response if method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD) else {}

        async def _send_request(method, params, **_kw):
            self.methods.append(method)
            return 999

        async def _send_notification(method, params):
            return None

        async def _wait_for_response(_self, req_id, timeout=None):
            return {}

        stack.enter_context(patch.object(rt, "_send_and_await", _send_and_await))
        stack.enter_context(patch.object(rt, "send_request", _send_request))
        stack.enter_context(patch.object(rt, "send_notification", _send_notification))
        stack.enter_context(
            patch.object(AcpSessionHandle, "_wait_for_response", _wait_for_response)
        )


def _codex_runtime() -> AcpRuntime:
    """An initialized codex runtime with a fake process and no pipe."""
    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_CODEX, expect_mcp_reports=False)
    proc = MagicMock()
    proc.stdout = None
    proc.stdin = MagicMock()
    proc.returncode = None
    # nosemgrep: kirocrew.mock-pid-allocatable -- the fake process is never signalled: every wire seam is patched
    proc.pid = 4246
    rt._process = proc
    rt._pid = 4246
    rt._initialized = True
    return rt


def _projection(*, zero_tools: bool):
    async def _mirrored(agent, *, work_dir, session_key, channel_id):
        return _MirroredSessionMcp(
            servers=[],
            denied_tools=frozenset(),
            stub_token="tok-zero",
            derived_spec_snapshot=None,
            zero_tools=zero_tools,
        )

    return _mirrored


@pytest.mark.asyncio
@pytest.mark.parametrize("zero_tools", [True, False])
async def test_the_create_path_carries_the_flag_onto_the_handle(zero_tools):
    rt = _codex_runtime()
    plane = _Plane("sid-new")
    with ExitStack() as stack:
        plane.install(rt, stack)
        stack.enter_context(
            patch.object(rt, "_mirrored_session_mcp", _projection(zero_tools=zero_tools))
        )
        handle = await rt.create_session(cwd="/w", agent="kirocrew")

    assert METHOD_SESSION_NEW in plane.methods
    assert handle.spec_zero_tools is zero_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("zero_tools", [True, False])
async def test_the_load_path_carries_the_flag_onto_the_handle(zero_tools):
    """``session/load`` re-declares the array, so a resumed session is governed by the
    same parse as a new one."""
    rt = _codex_runtime()
    # codex-acp advertises ``loadSession`` in its ``initialize`` answer.
    rt._can_load_session = True
    plane = _Plane("sid-resumed")
    with ExitStack() as stack:
        plane.install(rt, stack)
        stack.enter_context(
            patch.object(rt, "_mirrored_session_mcp", _projection(zero_tools=zero_tools))
        )
        handle = await rt.load_session("", "sid-resumed", cwd="/w", agent="kirocrew")

    assert METHOD_SESSION_LOAD in plane.methods
    assert METHOD_SESSION_NEW not in plane.methods
    assert handle.spec_zero_tools is zero_tools


def test_every_handle_construction_on_the_create_path_passes_the_flag():
    """A ``session/new`` that timed out and is adopted late builds its handle through
    the collector's own ``_finish_create_session`` call. Each call, and the hand-off to
    the collector, must pass ``zero_tools``: the parameter defaults to False, so a call
    that drops it starts a zero-tool session that refuses nothing."""
    import ast
    import inspect

    from kiro_crew.acp import runtime_session_create

    tree = ast.parse(inspect.getsource(runtime_session_create))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("_finish_create_session", "_collect_late_start")
    ]
    assert sorted(c.func.attr for c in calls) == [
        "_collect_late_start",
        "_finish_create_session",
        "_finish_create_session",
    ], "a handle construction site was added or removed"
    for call in calls:
        assert "zero_tools" in {kw.arg for kw in call.keywords}, call.func.attr


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        pytest.param([], True, id="an_explicit_empty_list"),
        pytest.param(["@kirocrew-core"], False, id="a_list_that_names_a_tool"),
        pytest.param(None, False, id="no_tools_key"),
    ],
)
def test_the_mirrored_array_reports_zero_tools_from_its_own_parse(
    tmp_path, agents_dir, tools, expected
):
    """The real codex mirror, not a double: ``zero_tools`` comes back with the array."""
    _write_spec(agents_dir, servers={"kirocrew-core": dict(_CORE)}, tools=tools)
    rt = AcpRuntime(work_dir=str(tmp_path), acp_backend=ACP_BACKEND_CODEX)
    rt._initialized = True
    rt._mcp_gateway_overlay = str(tmp_path)
    rt._agent_capabilities = {"mcpCapabilities": dict(_CODEX_1_11_CAPS)}
    with (
        patch.object(acp_runtime, "pooled_session_servers", lambda *a, **k: []),
        patch.object(acp_runtime, "injection_server_names", lambda *a, **k: frozenset()),
    ):
        out = asyncio.run(
            rt._mirrored_session_mcp(
                "kirocrew", work_dir=str(tmp_path), session_key="s-zero", channel_id="c-zero"
            )
        )
    assert out is not None
    assert out.zero_tools is expected


# ── The notice ──


def _handle(*, backend: str = ACP_BACKEND_CODEX, zero_tools: bool) -> AcpSessionHandle:
    runtime = MagicMock()
    runtime.acp_backend = backend
    handle = AcpSessionHandle("s-1", asyncio.Queue(), runtime)
    handle.spec_zero_tools = zero_tools
    return handle


def _provider_notice(handle: AcpSessionHandle) -> str:
    """The notice as the dashboard reads it: ``AcpProvider`` over ``AcpSessionProvider``."""
    session = AcpSessionProvider(handle, MagicMock())
    return AcpProvider.zero_tool_ban_notice.fget(SimpleNamespace(_client=session))


def test_a_new_zero_tool_codex_session_gets_one_notice_row(monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    notice = _provider_notice(_handle(zero_tools=True))

    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(zero_tool_ban_notice=notice)
    )

    assert len(rows) == 1
    assert rows[0][2] == "notice"
    text = rows[0][3]
    assert '"tools": []' in text
    assert "refuses every tool call that Codex asks permission for" in text
    assert "read files" in text and "without asking" in text
    # codex starts the operator's own config.toml MCP servers and runs a tool it
    # approves itself without asking, so the notice must name that residual too.
    assert "MCP tools from your Codex config.toml" in text
    # A hosted web search runs on the provider and never asks either.
    assert "search the web" in text


@pytest.mark.parametrize(
    ("backend", "zero_tools"),
    [
        pytest.param(ACP_BACKEND_CODEX, False, id="a_codex_spec_that_declares_tools"),
        pytest.param(ACP_BACKEND_KAS, True, id="a_backend_that_honours_the_ban_itself"),
    ],
)
def test_no_notice_where_the_ban_is_not_partial(monkeypatch, backend, zero_tools):
    rows: list[tuple] = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    notice = _provider_notice(_handle(backend=backend, zero_tools=zero_tools))

    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(zero_tool_ban_notice=notice)
    )

    assert notice == ""
    assert rows == []


def test_codex_still_does_not_claim_the_total_ban():
    """The refusal cannot see what codex runs without asking, so the capability stays
    False, and a backend with a partial-ban notice is never one that claims the total ban."""
    notices = session_handle_permissions.PARTIAL_ZERO_TOOL_BAN_NOTICES
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_HONOR_ZERO_TOOL_BAN
    assert capabilities_for(ACP_BACKEND_CODEX).honors_zero_tool_ban is False
    assert ACP_BACKEND_CODEX in notices
    assert set(notices).isdisjoint(ACP_BACKENDS_HONOR_ZERO_TOOL_BAN)
