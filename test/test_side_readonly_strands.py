"""The safe scope of a Strands Side Chat turn, end to end through the gate.

A Strands permission frame -- the gate module's envelope inside the CLI's interrupt
-- goes through the real ACP parser, the provider's event conversion and
``stream_and_collect``. On a ``side_read_only`` session under READ_ONLY only the
harness's own ``read`` of an absolute path runs. The shell (a read-only command
included), writes, fetches, sub-agents, a project's own tools and MCP tools are
refused before the gate. On a normal session the frame is built as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_STRANDS, ACP_BACKENDS_SIDE_READONLY
from kiro_crew.dashboard.chat_threads import THREAD_BOUNDARY_PROMPT_FILE_READS, build_thread_message
from kiro_crew.dashboard.side_prompts import SIDE_BOUNDARY_PROMPT_FILE_READS, side_boundary_prompt
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

NONCE = "b" * 32
TOOL_USE_ID = "tooluse_side1"

_REFUSED = [
    ("shell", "builtin", {"command": "ls /tmp"}),
    ("shell", "builtin", {"command": "cat /etc/hostname"}),
    ("write", "builtin", {"path": "/tmp/x", "content": "x"}),
    ("edit", "builtin", {"path": "/tmp/x", "old": "a", "new": "b"}),
    ("web_fetch", "builtin", {"url": "https://example.com"}),
    ("web_search", "builtin", {"query": "x"}),
    ("subagent", "builtin", {"prompt": "x"}),
    ("programmatic_tool_caller", "builtin", {"code": "print(1)"}),
    ("read", "custom", {"path": "/tmp/x"}),
    ("mcp__fs__read", "mcp:fs", {"path": "/tmp/x"}),
    ("read", "builtin", {"path": "relative/x"}),
    ("read", "builtin", {"path": "~/x"}),
    ("read", "builtin", {}),
    ("read", None, {"path": "/tmp/x"}),
]


def _frame(tool: str, owner: str | None, raw: dict[str, Any], **extra: Any) -> JsonRpcMessage:
    body: dict[str, Any] = {
        "kiro-crew-gate": 1,
        "nonce": NONCE,
        "tool": tool,
        "toolCallId": TOOL_USE_ID,
        "input": raw,
        **extra,
    }
    if owner is not None:
        body["owner"] = owner
    return JsonRpcMessage(
        id="perm-1",
        method="session/request_permission",
        params={
            "sessionId": "s1",
            "toolCall": {
                "toolCallId": "interrupt-1",
                "title": tool,
                "status": "pending",
                "rawInput": {"method": "confirm", "title": tool, "message": json.dumps(body)},
            },
            "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
            ],
        },
    )


def _event(msg: JsonRpcMessage, *, side_read_only: bool, raw_params_cache=None):
    event, _ = _dispatch.build_permission_event(
        msg,
        tool_input_cache={},
        shell_cache={},
        raw_params_cache=raw_params_cache if raw_params_cache is not None else {},
        mcp_server_name_cache={TOOL_USE_ID: ""},
        tool_name_cache={TOOL_USE_ID: ""},
        harness_tool_name_cache={},
        gate_envelope_nonce=NONCE,
        harness_backend=ACP_BACKEND_STRANDS,
        side_read_only=side_read_only,
    )
    assert event is not None
    return event


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


async def _gate(acp_event, policy: ToolApprovalPolicy) -> _ScriptedProvider:
    from kiro_crew.hooks import HookManager, HooksConfig

    provider = _ScriptedProvider(AcpProvider._to_llm_event(acp_event))
    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=policy,
        hooks=HookManager(HooksConfig()),
        retry_transient=False,
    )
    return provider


def test_strands_is_a_side_readonly_member():
    assert ACP_BACKEND_STRANDS in ACP_BACKENDS_SIDE_READONLY


@pytest.mark.asyncio
async def test_a_strands_side_turn_runs_its_own_read_of_an_absolute_path(tmp_path):
    target = tmp_path / "notes.txt"
    event = _event(_frame("read", "builtin", {"path": str(target)}), side_read_only=True)
    assert event.harness_builtin_tool == "fs_read"
    assert event.side_read_only_refusal == ""
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == ["perm-1"]
    assert provider.rejected == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "owner", "raw"), _REFUSED)
async def test_every_other_strands_call_is_refused_on_a_side_turn(tool, owner, raw):
    event = _event(_frame(tool, owner, raw), side_read_only=True)
    assert event.harness_builtin_tool == ""
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == []
    assert provider.rejected == ["perm-1"]


@pytest.mark.asyncio
async def test_a_read_only_shell_command_is_refused_before_the_gate():
    """The gate's shell classifier would approve ``ls`` on its own; the refusal does not let it."""
    event = _event(_frame("shell", "builtin", {"command": "ls /tmp"}), side_read_only=True)
    assert event.is_shell is True
    assert event.side_read_only_refusal
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.rejected == ["perm-1"]


@pytest.mark.asyncio
async def test_a_truncated_envelope_proves_no_read():
    event = _event(
        _frame("read", "builtin", {"path": "/tmp/x"}, truncated=True), side_read_only=True
    )
    assert event.harness_builtin_tool == ""
    assert event.side_read_only_refusal
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.rejected == ["perm-1"]


def test_a_read_whose_judged_path_differs_from_the_envelope_proves_nothing():
    """The deny tiers judge the cached params; a read naming another path is not proven."""
    event = _event(
        _frame("read", "builtin", {"path": "/tmp/x"}),
        side_read_only=True,
        raw_params_cache={TOOL_USE_ID: {"path": "/tmp/other"}},
    )
    assert event.harness_builtin_tool == ""
    assert event.side_read_only_refusal


@pytest.mark.asyncio
async def test_a_read_of_a_sensitive_path_is_refused_on_a_side_turn():
    """The proven read still meets the sensitive-path keystone on its argument."""
    target = str(Path.home() / ".ssh" / "id_rsa")
    event = _event(_frame("read", "builtin", {"path": target}), side_read_only=True)
    assert event.harness_builtin_tool == "fs_read"
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == []
    assert provider.rejected == ["perm-1"]


@pytest.mark.parametrize(("tool", "owner", "raw"), [*_REFUSED, ("read", "builtin", {"path": "/x"})])
def test_a_normal_turn_event_carries_no_side_mapping(tool, owner, raw):
    event = _event(_frame(tool, owner, raw), side_read_only=False)
    assert event.harness_builtin_tool == ""
    assert event.side_read_only_refusal == ""


def test_the_side_and_thread_prompts_name_file_reads_only():
    assert (
        side_boundary_prompt(shell_available=False, searches_available=False)
        == SIDE_BOUNDARY_PROMPT_FILE_READS
    )
    message = build_thread_message(
        {"role": "assistant", "content": "x"},
        [],
        [],
        "q",
        tools_available=True,
        shell_available=False,
        searches_available=False,
    )
    assert THREAD_BOUNDARY_PROMPT_FILE_READS in message


class _LaunchSession:
    """The two session members :func:`launch_mcp_servers` reads, recorded."""

    def __init__(self, *, side_read_only: bool) -> None:
        self._side_read_only = side_read_only
        self.prepared = False

    async def _prepare_session_mcp(self) -> None:
        self.prepared = True

    def _session_mcp_servers(self) -> list[dict[str, Any]]:
        return [{"name": "probe", "command": "true", "args": [], "env": []}]


@pytest.mark.asyncio
async def test_a_side_session_records_the_spec_but_launches_with_no_mcp_server(monkeypatch):
    from kiro_crew.acp.harness import strands as strands_mod

    monkeypatch.setattr(strands_mod, "prune_unstartable", lambda servers: (list(servers), []))
    side = _LaunchSession(side_read_only=True)
    assert await strands_mod.launch_mcp_servers(side) == []  # type: ignore[arg-type]
    # Still prepared: the projection records the spec's own restrictions (a
    # ``"tools": []`` ban) that the permission path enforces on the side session.
    assert side.prepared is True
    normal = _LaunchSession(side_read_only=False)
    assert [s["name"] for s in await strands_mod.launch_mcp_servers(normal)] == [  # type: ignore[arg-type]
        "probe"
    ]
    assert normal.prepared is True


_LIVE = Path(__file__).parent / "fixtures" / "acp_frames" / "strands" / "side-read-only-live.jsonl"


_LIVE_ROOT = "/work/strands-probe"


def _live_permission_events(root: Path, *, side_read_only: bool) -> list:
    """The capture's frames through the real parser, with one client's caches.

    The capture's POSIX root is swapped for ``root``, a native absolute path, in
    every frame alike: on Windows a driveless path is not absolute, so the read
    would be refused there for the path's spelling rather than for its tool.
    """
    native = json.dumps(str(root))[1:-1]
    lines = _LIVE.read_text(encoding="utf-8").replace(_LIVE_ROOT, native).splitlines()
    frames = [json.loads(line) for line in lines[1:] if line.strip()]
    caches: dict = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "harness_tool_name_cache": {},
    }
    events = []
    for frame in frames:
        if frame.get("method") == "session/update":
            update = frame["params"]["update"]
            if update.get("sessionUpdate") == "tool_call":
                _dispatch.parse_session_update(update, **caches)
        elif frame.get("method") == "session/request_permission":
            msg = JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
            event, _ = _dispatch.build_permission_event(
                msg,
                **caches,
                gate_envelope_nonce="strands-nonce-1",
                harness_backend=ACP_BACKEND_STRANDS,
                side_read_only=side_read_only,
            )
            assert event is not None
            events.append(event)
    return events


def test_the_live_capture_shows_the_read_ran_and_the_shell_and_write_did_not():
    """What the CLI answered: the read's text came back, the other two were DENIED."""
    text = _LIVE.read_text(encoding="utf-8")
    assert "kumquat-6" in text
    assert "DENIED: Kiro Crew did not approve 'shell'" in text
    assert "DENIED: Kiro Crew did not approve 'write'" in text


@pytest.mark.asyncio
async def test_the_live_side_session_proves_its_read_and_refuses_the_rest(tmp_path):
    read, shell, write = _live_permission_events(tmp_path, side_read_only=True)
    assert read.harness_builtin_tool == "fs_read" and read.side_read_only_refusal == ""
    assert shell.is_shell and shell.side_read_only_refusal
    assert write.side_read_only_refusal
    for event, approved in ((read, True), (shell, False), (write, False)):
        provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
        assert (provider.approved == [event.request_id]) is approved
        assert (provider.rejected == [event.request_id]) is not approved


def test_the_live_frames_on_a_normal_session_carry_no_side_mapping(tmp_path):
    for event in _live_permission_events(tmp_path, side_read_only=False):
        assert event.harness_builtin_tool == ""
        assert event.side_read_only_refusal == ""
