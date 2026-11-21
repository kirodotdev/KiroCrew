"""The safe scope of a MiMo Code Side Chat turn, end to end through the gate.

MiMo Code's ``tool_call`` frames and permission requests -- replayed from the live
0.1.15 capture ``mimocode/side-read-only-live.jsonl``, or shaped as it sent them --
go through the real ACP parser, the provider's event conversion and
``stream_and_collect``. On a ``side_read_only`` session under READ_ONLY its own
``read``, ``grep`` and ``glob`` of an absolute target run, and every write, shell,
unasked, MCP and relative-path call is refused. The side seed denies every tool in
``MIMOCODE_SIDE_DENIED_TOOLS``, the read-back refuses a session that keeps one, the
array is empty and the tripwire allows no other unasked call. A normal session's
event names no built-in.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import mimocode as harness
from kiro_crew.acp.harness_tool_names import (
    HARNESS_UNASKED_TOOL_IDS,
    MIMOCODE_SIDE_DENIED_TOOLS,
    MIMOCODE_SIDE_UNASKED_TOOL_IDS,
)
from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, EVENT_TOOL_CALL_UPDATE, JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_MIMOCODE, ACP_BACKENDS_SIDE_READONLY
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_CAPTURE = (
    Path(__file__).parent / "fixtures" / "acp_frames" / "mimocode" / "side-read-only-live.jsonl"
)
#: The same Side Chat turn behind the LiteLLM proxy and on MiMo Code's own xiaomi provider.
_CAPTURES = (_CAPTURE, _CAPTURE.with_name("side-read-only-xiaomi-live.jsonl"))
_SESSION = "mimo-session-1"
#: Lost-run bound on every await the test unblocks.
_LOST_RUN_TIMEOUT = 30.0


def _caches() -> dict:
    return {
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "harness_tool_name_cache": {},
        "tool_input_cache": {},
    }


def _permission(msg_id, tool_call: dict, *, side_read_only: bool, caches: dict):
    msg = JsonRpcMessage(
        id=msg_id,
        method="session/request_permission",
        params={
            "toolCall": tool_call,
            "options": [
                {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        },
    )
    event, _ = _dispatch.build_permission_event(
        msg, **caches, harness_backend=ACP_BACKEND_MIMOCODE, side_read_only=side_read_only
    )
    assert event is not None
    return event


async def _captured_events(*, side_read_only: bool, capture: Path = _CAPTURE) -> list:
    """Every permission event of the live capture, through a real client's turn loop.

    The frames are served in captured order, so the side read deferral is what moves
    the ``grep`` and ``glob`` running updates ahead of their requests."""
    frames = deque(
        JsonRpcMessage.from_dict(json.loads(line))
        for line in capture.read_text(encoding="utf-8").splitlines()[3:]
    )
    events = await _turn_events(frames, side_read_only=side_read_only, req_id=3)
    return [e for e in events if e.kind == EVENT_PERMISSION_REQUEST]


async def _turn_events(frames: deque, *, side_read_only: bool, req_id: int) -> list:
    """Run one turn of a MiMo Code client over *frames*; return its events."""
    client = AcpClient(
        work_dir=Path("/work/mimo-proj"),
        acp_backend=ACP_BACKEND_MIMOCODE,
        side_read_only=side_read_only,
    )
    client._session_id = _SESSION
    read_cap = len(frames) + 12
    reads = [0]

    async def fake_read(timeout=20.0):
        reads[0] += 1
        if reads[0] > read_cap:
            raise AssertionError(f"prompt loop read {reads[0]} frames from {read_cap} queued")
        if client._buffer:
            return client._buffer.popleft()
        return frames.popleft() if frames else None

    client._read_message = fake_read  # type: ignore[method-assign]
    client._read_new_tool_results = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def collect() -> list:
        events = []
        async with aclosing(
            client._dispatch_events(req_id=req_id, timeout=FALLBACK_TIMEOUT_SECS)
        ) as stream:
            async for event in stream:
                events.append(event)
        return events

    return await asyncio.wait_for(collect(), timeout=_LOST_RUN_TIMEOUT)


def _shaped_event(tool: str, raw: dict, *, kind: str = "read", side_read_only: bool = True):
    """A first tool_call frame titled *tool* (rawInput empty, as MiMo Code sends it),
    then the arguments on an update, then the ask."""
    caches = _caches()
    _dispatch.parse_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c1",
            "title": tool,
            "kind": kind,
            "status": "pending",
            "rawInput": {},
        },
        **caches,
    )
    _dispatch.parse_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c1",
            "title": tool,
            "kind": kind,
            "status": "in_progress",
            "rawInput": raw,
        },
        **caches,
    )
    return _permission(
        "perm-1",
        {"toolCallId": "c1", "title": tool, "kind": kind, "status": "pending", "rawInput": raw},
        side_read_only=side_read_only,
        caches=caches,
    )


class _ScriptedProvider:
    def __init__(self, event: LLMEvent) -> None:
        self._events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="on it"),
            event,
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def stream(self, message: str):
        for event in self._events:
            yield event

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


async def _gate(acp_event) -> _ScriptedProvider:
    from kiro_crew.hooks import HookManager, HooksConfig

    provider = _ScriptedProvider(AcpProvider._to_llm_event(acp_event))
    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=HookManager(HooksConfig()),
        retry_transient=False,
    )
    return provider


def test_mimocode_is_a_side_readonly_member():
    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_SIDE_READONLY


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", _CAPTURES, ids=lambda p: p.stem)
async def test_the_captured_reads_are_proven_and_run(capture):
    """read, grep and glob, as 0.1.15 sent them live, each run under READ_ONLY: behind
    the LiteLLM proxy and on the harness's own xiaomi provider."""
    events = await _captured_events(side_read_only=True, capture=capture)
    assert [e.harness_builtin_tool for e in events] == ["fs_read", "grep", "glob"]
    assert all(e.tool_kind == "read" and e.raw_params_trusted for e in events)
    for event in events:
        provider = await _gate(event)
        assert provider.approved == [event.request_id]
        assert provider.rejected == []


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", _CAPTURES, ids=lambda p: p.stem)
async def test_a_normal_session_names_no_built_in(capture):
    for event in await _captured_events(side_read_only=False, capture=capture):
        assert event.harness_builtin_tool == ""


_REFUSED = [
    ("write", {"file_path": "/work/x", "content": "x"}, "edit"),
    ("edit", {"file_path": "/work/x", "old_string": "a", "new_string": "b"}, "edit"),
    ("notebook_edit", {"notebook_path": "/work/x.ipynb"}, "edit"),
    ("memory", {"action": "write", "content": "x"}, "other"),
    ("task", {"action": "add"}, "other"),
    ("actor", {"action": "start", "prompt": "x"}, "other"),
    ("webfetch", {"url": "https://example.com"}, "fetch"),
    ("crew-probe_read", {"file_path": "/work/a"}, "read"),
    ("read", {"file_path": "work/a"}, "read"),
    ("read", {"file_path": "../a"}, "read"),
    ("read", {}, "read"),
    ("grep", {"pattern": "x"}, "search"),
    ("glob", {"pattern": "*.md", "path": "docs"}, "search"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "raw", "kind"), _REFUSED)
async def test_everything_else_is_refused(tool, raw, kind):
    """A writer, the shell, an unasked tool, a web fetch, an MCP tool whose fused name
    ends in ``read``, and a read with no absolute target: no built-in, refused."""
    event = _shaped_event(tool, raw, kind=kind)
    assert event.harness_builtin_tool == ""
    provider = await _gate(event)
    assert provider.approved == []
    assert provider.rejected == [event.request_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["edit", "execute", "delete"])
async def test_a_proven_read_with_a_stated_write_kind_is_refused(kind):
    event = _shaped_event("read", {"file_path": "/work/a"}, kind=kind)
    assert event.tool_kind == kind
    provider = await _gate(event)
    assert provider.approved == []


def test_a_shell_call_names_no_built_in():
    """The side seed hides ``bash``; a shell call that reached the gate anyway is judged
    as a shell call, never as a proven read built-in."""
    event = _shaped_event("bash", {"command": "ls /work"}, kind="execute")
    assert event.harness_builtin_tool == ""


@pytest.mark.asyncio
async def test_a_shaped_absolute_read_runs():
    event = _shaped_event("read", {"file_path": "/work/a"})
    assert event.harness_builtin_tool == "fs_read"
    provider = await _gate(event)
    assert provider.approved == ["perm-1"]


# ── the seed, the read-back and the tripwire ──


def _session(*, deny_rules: tuple = (), **kw) -> SimpleNamespace:
    return SimpleNamespace(_session_harness_deny_rules=deny_rules, **kw)


def test_a_side_seed_denies_every_writer_shell_and_unasked_tool():
    seed = json.loads(
        harness._mimocode_routing_config(
            ACP_BACKEND_MIMOCODE, harness._mimocode_seed_denies(_session(_side_read_only=True))
        )
    )
    permission = seed["permission"]
    assert next(iter(permission)) == "*" and permission["*"] == "ask"
    assert {t for t, action in permission.items() if action == "deny"} == set(
        MIMOCODE_SIDE_DENIED_TOOLS
    )
    for tool in ("read", "grep", "glob"):
        assert tool not in permission
    # Every unasked tool but the two that touch nothing is denied.
    unasked = HARNESS_UNASKED_TOOL_IDS[ACP_BACKEND_MIMOCODE]
    assert unasked - set(MIMOCODE_SIDE_DENIED_TOOLS) <= MIMOCODE_SIDE_UNASKED_TOOL_IDS


def test_a_normal_seed_is_unchanged():
    session = _session(_side_read_only=False)
    assert harness._mimocode_seed_denies(session) == ()
    session = _session(_side_read_only=False, deny_rules=("srv_tool",))
    assert harness._mimocode_seed_denies(session) == ("srv_tool",)


def test_a_side_seed_keeps_the_projections_own_denies():
    session = _session(_side_read_only=True, deny_rules=("srv_tool",))
    denies = harness._mimocode_seed_denies(session)
    assert denies[0] == "srv_tool"
    assert set(MIMOCODE_SIDE_DENIED_TOOLS) <= set(denies)


def test_the_side_session_array_is_empty():
    from kiro_crew.acp.client_mcp_array import _mimocode_session_mcp_servers

    client = SimpleNamespace(_side_read_only=True, _session_mcp_servers=lambda: [{"name": "x"}])
    assert _mimocode_session_mcp_servers(client) == []
    client = SimpleNamespace(_side_read_only=False, _session_mcp_servers=lambda: [{"name": "x"}])
    assert _mimocode_session_mcp_servers(client) == [{"name": "x"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "side", "expected"),
    [
        ("memory", True, False),
        ("history", True, False),
        ("task", True, False),
        ("skill_search", True, False),
        ("invalid", True, True),
        ("mcp_tool_search", True, True),
        ("memory", False, True),
    ],
)
async def test_the_side_tripwire_allows_only_what_the_seed_leaves(name, side, expected):
    from kiro_crew.acp.client_permissions import _unasked_call_is_expected

    client = SimpleNamespace(
        backend=ACP_BACKEND_MIMOCODE,
        _side_read_only=side,
        _tool_call_harness_tool_name={"c1": name},
        _tool_call_params={},
    )
    assert await _unasked_call_is_expected(client, {}, "c1") is expected


# ── the side read deferral ──


def _frame(update: dict) -> JsonRpcMessage:
    return JsonRpcMessage(method="session/update", params={"sessionId": _SESSION, "update": update})


def _grep_turn(*, update_call: str = "c1", update_input: dict | None = None) -> deque:
    """A grep call whose request comes before its running update, as 0.1.15 sent it."""
    target = {"pattern": "LYNX", "path": "/work/mimo-proj"}
    return deque(
        [
            _frame(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "c1",
                    "title": "grep",
                    "kind": "search",
                    "status": "pending",
                    "rawInput": {},
                }
            ),
            JsonRpcMessage(
                id=7,
                method="session/request_permission",
                params={
                    "sessionId": _SESSION,
                    "toolCall": {
                        "toolCallId": "c1",
                        "title": "grep",
                        "kind": "search",
                        "status": "pending",
                        "rawInput": dict(target),
                    },
                    "options": [{"optionId": "reject", "kind": "reject_once"}],
                },
            ),
            _frame(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": update_call,
                    "title": "grep",
                    "kind": "search",
                    "status": "in_progress",
                    "rawInput": target if update_input is None else update_input,
                }
            ),
            JsonRpcMessage(id=1, result={"stopReason": "end_turn"}),
        ]
    )


@pytest.mark.asyncio
async def test_a_side_grep_asked_before_its_update_is_proven():
    events = await _turn_events(_grep_turn(), side_read_only=True, req_id=1)
    permission = next(e for e in events if e.kind == EVENT_PERMISSION_REQUEST)
    assert permission.harness_builtin_tool == "grep"
    assert permission.raw_params_trusted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "update_call", "update_input"),
    [
        (False, "c1", None),
        (True, "c2", None),
        (True, "c1", {"pattern": "LYNX"}),
    ],
)
async def test_the_request_inline_target_never_proves_a_read(side, update_call, update_input):
    """A normal session, another call's update, or an update with no path: the
    request's own ``rawInput`` path is never the proof, and the frame order stays."""
    events = await _turn_events(
        _grep_turn(update_call=update_call, update_input=update_input),
        side_read_only=side,
        req_id=1,
    )
    permission = next(e for e in events if e.kind == EVENT_PERMISSION_REQUEST)
    assert permission.harness_builtin_tool == ""
    assert events.index(permission) < max(
        i for i, e in enumerate(events) if e.kind == EVENT_TOOL_CALL_UPDATE
    )


# ── the read-back of a side session ──

_ARGV = ["/opt/bin/mimo", "debug", "config"]


def _side_read_back(monkeypatch, tmp_path, document: dict, *, side_read_only: bool = True):
    """The launch read-back of a session seeded as its launch seeds it, answered with
    *document* as ``mimo debug config`` would print it."""
    for name in [n for n in __import__("os").environ if n.startswith(("MIMOCODE_", "MIMO_"))]:
        monkeypatch.delenv(name, raising=False)
    document = {"memory": {"disable_write": True}, **document}
    completed = SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr="")
    monkeypatch.setattr(harness.subprocess_mod, "run", lambda argv, **kw: completed)
    client = AcpClient(
        work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE, side_read_only=side_read_only
    )
    seed = harness._mimocode_routing_config(
        ACP_BACKEND_MIMOCODE, harness._mimocode_seed_denies(client)
    )
    return harness._verify_mimocode_routing(client, ACP_BACKEND_MIMOCODE, list(_ARGV), seed), seed


def test_a_side_session_whose_denies_hold_starts(monkeypatch, tmp_path):
    seed = harness._mimocode_routing_config(ACP_BACKEND_MIMOCODE, tuple(MIMOCODE_SIDE_DENIED_TOOLS))
    verdict, _seed = _side_read_back(monkeypatch, tmp_path, json.loads(seed))
    assert verdict == ("", "")


@pytest.mark.parametrize("tool", ["bash", "edit", "memory", "task"])
def test_a_side_session_that_lost_a_deny_is_refused(monkeypatch, tmp_path, tool):
    document = json.loads(
        harness._mimocode_routing_config(ACP_BACKEND_MIMOCODE, tuple(MIMOCODE_SIDE_DENIED_TOOLS))
    )
    del document["permission"][tool]
    verdict, _seed = _side_read_back(monkeypatch, tmp_path, document)
    assert verdict is not None
    issue, remedy = verdict
    assert issue.startswith("MiMo Code's own config keeps tools a Side Chat must not offer")
    assert tool in issue and "reopen the side chat" in remedy


def test_a_side_session_with_a_native_mcp_mount_is_refused(monkeypatch, tmp_path):
    document = json.loads(
        harness._mimocode_routing_config(ACP_BACKEND_MIMOCODE, tuple(MIMOCODE_SIDE_DENIED_TOOLS))
    )
    document["mcp"] = {"docs": {"type": "local", "command": ["docs-mcp"]}}
    verdict, _seed = _side_read_back(monkeypatch, tmp_path, document)
    assert verdict is not None
    assert "a Side Chat runs with no MCP server" in verdict[0]


def test_the_side_denies_cover_every_named_tool_that_is_not_a_read():
    """Every MiMo Code tool Crew names for a kiro tool, apart from the three reads and
    the two web tools the gate judges itself, is denied in a side seed."""
    from kiro_crew.acp.harness_tool_names import (
        MIMOCODE_READ_ONLY_BUILTINS,
        MIMOCODE_TOOL_IDS_BY_KIRO_TOOL,
    )

    named = {tool for tools in MIMOCODE_TOOL_IDS_BY_KIRO_TOOL.values() for tool in tools}
    expected = named - set(MIMOCODE_READ_ONLY_BUILTINS) - {"webfetch", "websearch"}
    assert expected <= set(MIMOCODE_SIDE_DENIED_TOOLS)
    assert {"bash", "edit", "write", "apply_patch", "actor"} <= expected


def test_the_side_and_thread_prompts_name_reads_and_searches_without_web():
    from kiro_crew.dashboard.chat_threads import (
        THREAD_BOUNDARY_PROMPT_NO_WEB,
        build_thread_message,
    )
    from kiro_crew.dashboard.side_prompts import (
        SIDE_BOUNDARY_PROMPT_NO_SHELL,
        SIDE_BOUNDARY_PROMPT_NO_WEB,
        side_boundary_prompt,
    )

    assert side_boundary_prompt(shell_available=False, web_available=False) == (
        SIDE_BOUNDARY_PROMPT_NO_WEB
    )
    assert side_boundary_prompt(shell_available=False) == SIDE_BOUNDARY_PROMPT_NO_SHELL
    message = build_thread_message(
        {"role": "assistant", "content": "x"},
        [],
        [],
        "q",
        tools_available=True,
        shell_available=False,
        web_available=False,
    )
    assert THREAD_BOUNDARY_PROMPT_NO_WEB in message


def test_an_inline_input_on_the_request_never_proves_a_read():
    """With no running update cached, an absolute target the permission frame carries
    inline (``input`` or ``params``) is untrusted, so the call names no built-in."""
    for key in ("input", "params"):
        caches = _caches()
        _dispatch.parse_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "c1",
                "title": "read",
                "kind": "read",
                "status": "pending",
                "rawInput": {},
            },
            **caches,
        )
        event = _permission(
            "perm-1",
            {
                "toolCallId": "c1",
                "title": "read",
                "kind": "read",
                "status": "pending",
                key: {"file_path": "/work/a"},
            },
            side_read_only=True,
            caches=caches,
        )
        assert event.harness_builtin_tool == ""


def _held_side_document(**extra) -> dict:
    document = json.loads(
        harness._mimocode_routing_config(ACP_BACKEND_MIMOCODE, tuple(MIMOCODE_SIDE_DENIED_TOOLS))
    )
    document.update(extra)
    return document


def test_a_side_session_that_loads_a_plugin_is_refused(monkeypatch, tmp_path):
    """A plugin can register `read` and replace the built-in (0.1.15, measured: a
    project `.mimocode/plugin/` file's `read` ran instead), so a call titled `read`
    proves nothing while one is loaded. The resolved `plugin` list names it."""
    plugin = "file:///work/proj/.mimocode/plugin/p.js"
    verdict, _seed = _side_read_back(monkeypatch, tmp_path, _held_side_document(plugin=[plugin]))
    issue, remedy = verdict
    assert issue.startswith("MiMo Code loads the plugins ")
    assert plugin in issue and "reopen the side chat" in remedy


def test_an_empty_plugin_list_starts_a_side_session(monkeypatch, tmp_path):
    verdict, _seed = _side_read_back(monkeypatch, tmp_path, _held_side_document(plugin=[]))
    assert verdict == ("", "")


def test_a_main_session_with_a_plugin_is_not_refused_for_it(monkeypatch, tmp_path):
    document = json.loads(harness._mimocode_routing_config(ACP_BACKEND_MIMOCODE))
    document["plugin"] = ["file:///work/proj/.mimocode/plugin/p.js"]
    verdict, _seed = _side_read_back(monkeypatch, tmp_path, document, side_read_only=False)
    assert "plugin" not in verdict[0]


# ── a search walks its whole root ──


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["grep", "glob"])
async def test_a_search_over_a_root_holding_a_credential_store_is_refused(tool, tmp_path):
    """grep and glob walk hidden folders, so a root with a sensitive location below it
    (here the home folder, which holds `~/.ssh`) proves no read."""
    from pathlib import Path as _Path

    home = str(_Path.home())
    raw = {"pattern": "**/*" if tool == "glob" else "BEGIN", "path": home}
    event = _shaped_event(tool, raw, kind="search")
    assert event.harness_builtin_tool == ""
    provider = await _gate(event)
    assert provider.approved == []


@pytest.mark.parametrize("pattern", ["/etc/*", "~/.ssh/*", "../*", "docs/../../*", "..\\x"])
def test_a_glob_pattern_that_leaves_its_root_names_no_built_in(pattern):
    event = _shaped_event("glob", {"pattern": pattern, "path": "/work/mimo-proj"}, kind="search")
    assert event.harness_builtin_tool == ""


def test_a_glob_inside_its_root_and_a_file_read_stay_proven():
    glob = _shaped_event("glob", {"pattern": "**/*.md", "path": "/work/mimo-proj"}, kind="search")
    assert glob.harness_builtin_tool == "glob"
    read = _shaped_event("read", {"file_path": "/work/mimo-proj/docs/notes.md"})
    assert read.harness_builtin_tool == "fs_read"
