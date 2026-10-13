"""DeepSeek Harness side turns under READ_ONLY: what the gate approves, and what it refuses.

deepseek joins ``ACP_BACKENDS_SIDE_READONLY`` because every call it serves already
asks: Crew's gate plugin turns each harness ``allow`` into ``ask``, so the harness
has no allow rule a call could ride past the gate on. Each frame pair below is
replayed through the REAL deepseek transport (``AcpClient`` on the deepseek
backend: its tool_call cache, then its permission builder), the provider's event
conversion and the REAL ``HookManager``, so the verdict asserted is the one a
side turn on deepseek would get. The first group replays the live capture in
``test/fixtures/acp_frames/deepseek/side-read-only-live.jsonl`` verbatim.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness.deepseek import deepseek_gate_extension_path
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_DEEPSEEK, ACP_BACKENDS_SIDE_READONLY
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "acp_frames" / "deepseek" / "side-read-only-live.jsonl"
)

_OPTIONS = [
    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
]


def _live_pairs() -> dict[str, list[dict[str, Any]]]:
    """Each captured call's frames, keyed by the tool name its tool_call stated."""
    frames = [json.loads(line) for line in _FIXTURE.read_text().splitlines()[1:]]
    by_id: dict[str, list[dict[str, Any]]] = {}
    names: dict[str, str] = {}
    for frame in frames:
        if frame.get("method") == "session/update":
            update = frame["params"]["update"]
            if update["sessionUpdate"] == "tool_call":
                names[update["toolCallId"]] = update["title"]
                by_id.setdefault(update["toolCallId"], []).append(frame)
        else:
            by_id[frame["params"]["toolCall"]["toolCallId"]].append(frame)
    return {names[call_id]: pair for call_id, pair in by_id.items()}


_LIVE = _live_pairs()

#: The argument each captured read names its target under.
_TARGET_PARAM = {"read": "file_path", "grep": "path", "glob": "path"}


def _live_at(name: str, root: Path) -> list[dict[str, Any]]:
    """A captured read, its relative target anchored under *root*.

    The capture's targets are relative (``note.txt``, ``.``), which dsh resolves
    against the session's work dir and the gate cannot; anchored, the frames are
    otherwise the ones on the wire.
    """
    tool_call, permission = copy.deepcopy(_LIVE[name])
    raw_input = tool_call["params"]["update"]["rawInput"]
    param = _TARGET_PARAM[name]
    raw_input[param] = str(root / raw_input.get(param, "."))
    return [tool_call, permission]


def _tool_call(call_id: str, title: str, raw_input: dict[str, Any]) -> dict[str, Any]:
    """A tool_call frame in the shape dsh's ACP bridge writes for every tool."""
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "dsh-side-session-1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": title,
                "kind": "other",
                "status": "in_progress",
                "rawInput": raw_input,
            },
        },
    }


def _permission(call_id: str, request_id: int = 0) -> dict[str, Any]:
    """dsh's permission frame: the toolCallId and nothing else about the call."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "dsh-side-session-1",
            "toolCall": {"toolCallId": call_id},
            "options": _OPTIONS,
        },
    }


def _client(tmp_path: Path) -> AcpClient:
    return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_DEEPSEEK)


def _replay(client: AcpClient, *frames: dict[str, Any]) -> LLMEvent:
    """The permission event the deepseek transport hands the gate for these frames."""
    *updates, permission = frames
    for update in updates:
        client._extract_tool_event(JsonRpcMessage.from_dict(update))
    event = client._build_permission_event(JsonRpcMessage.from_dict(permission))
    assert event is not None
    return AcpProvider._to_llm_event(event)


class _ScriptedProvider:
    def __init__(self, event: LLMEvent) -> None:
        self._events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="on it"),
            event,
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
        self.approved: list[Any] = []
        self.rejected: list[Any] = []

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        for event in self._events:
            yield event

    async def approve_tool(self, request_id: Any) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: Any) -> None:
        self.rejected.append(request_id)


def _hooks(**config: Any):
    from kiro_crew.hooks import HookManager, HooksConfig

    return HookManager(HooksConfig(**config))


async def _side_turn(tmp_path: Path, *frames: dict[str, Any], **config: Any) -> _ScriptedProvider:
    provider = _ScriptedProvider(_replay(_client(tmp_path), *frames))
    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.READ_ONLY,
        hooks=_hooks(**config),
        retry_transient=False,
    )
    return provider


async def _normal_turn(tmp_path: Path, *frames: dict[str, Any]) -> tuple[_ScriptedProvider, int]:
    """A main-chat turn (HOOK_BASED) whose approval card is declined."""
    asked: list[LLMEvent] = []

    async def _decline(event: LLMEvent) -> bool:
        asked.append(event)
        return False

    provider = _ScriptedProvider(_replay(_client(tmp_path), *frames))
    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        approval_policy=ToolApprovalPolicy.HOOK_BASED,
        hooks=_hooks(),
        on_tool_approval=_decline,
        retry_transient=False,
    )
    return provider, len(asked)


def test_deepseek_is_a_side_readonly_member():
    assert ACP_BACKEND_DEEPSEEK in ACP_BACKENDS_SIDE_READONLY


def test_the_live_capture_asked_for_every_call():
    """The membership premise: with the gate plugin composed, every call asked."""
    assert set(_LIVE) == {"read", "grep", "glob", "write", "bash", "mcp__probe__read"}
    for name, (tool_call, permission) in _LIVE.items():
        assert tool_call["params"]["update"]["kind"] == "other", name
        assert "_meta" not in tool_call["params"]["update"], name
        assert permission["method"] == "session/request_permission", name


# ── (a) a read succeeds on a side turn ─────────────────────────────────────────


@pytest.mark.parametrize("name", ["read", "grep", "glob"])
@pytest.mark.asyncio
async def test_side_turn_approves_the_live_reads(tmp_path, name):
    tool_call, permission = _live_at(name, tmp_path)
    provider = await _side_turn(tmp_path, tool_call, permission)

    assert provider.approved == [permission["id"]]
    assert provider.rejected == []


@pytest.mark.parametrize(
    "frames",
    [
        # The live capture's own glob names no root.
        _LIVE["glob"],
        (_tool_call("call_n", "grep", {"pattern": "x"}), _permission("call_n")),
        (_tool_call("call_e", "glob", {"pattern": "*", "path": ""}), _permission("call_e")),
        (_tool_call("call_f", "read", {"path": "note.txt"}), _permission("call_f")),
        # Relative targets, which the gate would judge against its own cwd.
        _LIVE["read"],
        _LIVE["grep"],
        (_tool_call("call_u", "read", {"file_path": "../x/.env"}), _permission("call_u")),
        (_tool_call("call_v", "grep", {"pattern": "k", "path": "$HOME"}), _permission("call_v")),
    ],
    ids=[
        "live_glob",
        "grep_no_path",
        "glob_empty_path",
        "read_no_file_path",
        "live_read_relative",
        "live_grep_dot",
        "read_parent_relative",
        "grep_unexpanded_var",
    ],
)
@pytest.mark.asyncio
async def test_side_turn_refuses_a_read_that_names_no_target(tmp_path, frames):
    """dsh resolves a missing or relative target against the session's work dir,
    which the sensitive-path and ``filesystem.read`` checks never see."""
    tool_call, permission = frames
    provider = await _side_turn(tmp_path, tool_call, permission)

    assert provider.approved == []
    assert provider.rejected == [permission["id"]]


# ── (b) write and exec are refused on a side turn and asked on a normal one ────


@pytest.mark.parametrize("name", ["write", "bash"])
@pytest.mark.asyncio
async def test_side_turn_refuses_the_live_write_and_exec(tmp_path, name):
    tool_call, permission = _LIVE[name]
    provider = await _side_turn(tmp_path, tool_call, permission)

    assert provider.approved == []
    assert provider.rejected == [permission["id"]]


@pytest.mark.asyncio
async def test_side_turn_refuses_a_read_only_bash_command(tmp_path):
    """dsh's bash streams as ``kind: other`` with no recoverable shell command."""
    provider = await _side_turn(
        tmp_path, _tool_call("call_ls", "bash", {"command": "ls"}), _permission("call_ls")
    )

    assert provider.approved == []
    assert provider.rejected == [0]


@pytest.mark.parametrize("name", ["write", "bash", "read"])
@pytest.mark.asyncio
async def test_normal_turn_still_asks_and_auto_approves_nothing(tmp_path, name):
    """The main chat is untouched: the read proof is a READ_ONLY verdict only."""
    tool_call, permission = _LIVE[name]
    provider, asked = await _normal_turn(tmp_path, tool_call, permission)

    assert asked == 1
    assert provider.approved == []
    assert provider.rejected == [permission["id"]]


# ── (c) a spoofed read name is refused ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_side_turn_refuses_an_mcp_tool_named_after_read(tmp_path):
    tool_call, permission = _LIVE["mcp__probe__read"]
    provider = await _side_turn(tmp_path, tool_call, permission)

    assert provider.approved == []
    assert provider.rejected == [permission["id"]]


@pytest.mark.parametrize(
    "first,second",
    [("write", "read"), ("read", "write"), ("bash", "grep")],
)
@pytest.mark.asyncio
async def test_two_calls_under_one_id_are_not_read_as_the_read(tmp_path, first, second):
    """dsh forwards the provider's id, and its permission frame names only that id."""
    provider = await _side_turn(
        tmp_path,
        _tool_call("call_0", first, {"file_path": "note.txt", "content": "x"}),
        _tool_call("call_0", second, {"file_path": "note.txt"}),
        _permission("call_0"),
    )

    assert provider.approved == []
    assert provider.rejected == [0]


@pytest.mark.asyncio
async def test_a_reused_id_in_a_later_turn_is_read_afresh(tmp_path):
    """The per-turn reset clears the name, so ``call_0`` is not poisoned for good."""
    client = _client(tmp_path)
    client._extract_tool_event(
        JsonRpcMessage.from_dict(_tool_call("call_0", "write", {"file_path": "a"}))
    )
    client._tool_call_harness_tool_name.clear()
    event = _replay(
        client, _tool_call("call_0", "read", {"file_path": "note.txt"}), _permission("call_0")
    )

    assert event.deepseek_tool_name == "read"


@pytest.mark.parametrize(
    "frames",
    [
        # No tool_call frame preceded the request.
        (_permission("call_orphan"),),
        # A title that is prose, not a tool name.
        (
            _tool_call("call_prose", "read the file", {"file_path": "note.txt"}),
            _permission("call_prose"),
        ),
        # The request names a different id than the read.
        (_tool_call("call_r", "read", {"file_path": "note.txt"}), _permission("call_other")),
    ],
    ids=["no_tool_call", "prose_title", "other_id"],
)
@pytest.mark.asyncio
async def test_side_turn_refuses_a_read_it_cannot_tie_to_its_frame(tmp_path, frames):
    provider = await _side_turn(tmp_path, *frames)

    assert provider.approved == []
    assert provider.rejected == [0]


def test_the_mapped_name_rides_deepseek_alone(tmp_path):
    """Another harness stating ``read`` as its title gets no deepseek mapping."""
    for backend, expected in (("opencode", ""), (ACP_BACKEND_DEEPSEEK, "read")):
        client = AcpClient(work_dir=tmp_path, acp_backend=backend)
        event = _replay(
            client, _tool_call("call_r", "read", {"file_path": "note.txt"}), _permission("call_r")
        )
        assert event.deepseek_tool_name == expected, backend


# ── (d) a sensitive path is refused, whatever the harness would allow ─────────


@pytest.mark.parametrize(
    "title,raw_input",
    [
        ("read", {"file_path": "~/.aws/credentials"}),
        ("grep", {"pattern": "PRIVATE", "path": "~/.ssh"}),
        ("glob", {"pattern": "*", "path": "~/.aws"}),
    ],
    ids=["read_credentials", "grep_ssh", "glob_aws"],
)
@pytest.mark.asyncio
async def test_side_turn_refuses_a_sensitive_path(tmp_path, title, raw_input):
    provider = await _side_turn(
        tmp_path, _tool_call("call_s", title, raw_input), _permission("call_s")
    )

    assert provider.approved == []
    assert provider.rejected == [0]


@pytest.mark.parametrize("rule", ["fs_read", "read"])
@pytest.mark.asyncio
async def test_a_deny_rule_in_either_spelling_refuses_the_deepseek_read(tmp_path, rule):
    tool_call, permission = _live_at("read", tmp_path)
    provider = await _side_turn(tmp_path, tool_call, permission, auto_deny_tools=[rule])

    assert provider.approved == []
    assert provider.rejected == [permission["id"]]


@pytest.fixture
def _governance():
    """Install a governance ceiling for one test, and reset the context after."""
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.governance import parse_policy

    def install(policy: dict[str, Any]) -> None:
        base = build_default_context(KiroCrewConfig.load())
        body = {"version": 1, "boot": {"fail_closed": True}, **policy}
        ctx_mod.set_context(dataclasses.replace(base, governance=parse_policy(body)))

    yield install
    ctx_mod.reset_context()


@pytest.mark.parametrize(
    "denied,name",
    [("read", "read"), ("fs_read", "read"), ("grep", "grep")],
    ids=["tools_deny_read", "tools_deny_fs_read", "tools_deny_grep"],
)
@pytest.mark.asyncio
async def test_a_governance_ceiling_refuses_the_deepseek_read(tmp_path, _governance, denied, name):
    _governance({"tools": {"mode": "deny", "deny": [denied]}})
    frames = _live_at(name, tmp_path)
    provider = await _side_turn(tmp_path, *frames)

    assert provider.approved == []
    assert provider.rejected == [frames[1]["id"]]


@pytest.mark.asyncio
async def test_a_filesystem_read_ceiling_refuses_the_deepseek_read(tmp_path, _governance):
    # The matcher normalizes the item to native separators and reads the pattern
    # verbatim, so the pattern is built with them too.
    denied = os.path.join(str(tmp_path), "secret", "**")
    _governance({"filesystem": {"read": {"mode": "deny", "deny": [denied]}}})
    provider = await _side_turn(
        tmp_path,
        _tool_call("call_p", "read", {"file_path": str(tmp_path / "secret" / "plan.txt")}),
        _permission("call_p"),
    )

    assert provider.approved == []
    assert provider.rejected == [0]


@pytest.mark.asyncio
async def test_an_allow_mode_ceiling_naming_fs_read_admits_the_deepseek_read(tmp_path, _governance):
    """``read`` and ``fs_read`` are one identity: an allow list written in
    kiro-cli's spelling does not also demand dsh's."""
    _governance({"tools": {"mode": "allow", "allow": ["fs_read", "unknown"]}})
    tool_call, permission = _live_at("read", tmp_path)
    provider = await _side_turn(tmp_path, tool_call, permission)

    assert provider.approved == [permission["id"]]


_PLUGIN_PROBE = """
const gate = await import(process.argv[2]);
const verdicts = {};
const ctx = { on(event, listener) { this.listener = listener; } };
gate.apply(ctx);
for (const downstream of [{ kind: "allow" }, undefined, { kind: "deny", reason: "dsh" }]) {
  const out = await ctx.listener({ name: "read" }, async () => downstream);
  verdicts[downstream ? downstream.kind : "none"] = out.kind;
}
process.stdout.write(JSON.stringify(verdicts));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to load the gate plugin")
def test_the_gate_plugin_turns_a_harness_allow_into_an_ask(tmp_path):
    """No harness pre-approval reaches a call: an ``allow`` from dsh's own policy
    (or from a plugin answering before the bridge) becomes ``ask``, so the call
    still raises the permission request the sensitive-path check runs on, and a
    harness ``deny`` stays a deny."""
    script = tmp_path / "probe.mjs"
    script.write_text(_PLUGIN_PROBE)
    plugin_url = Path(deepseek_gate_extension_path()).as_uri()
    out = subprocess.run(
        ["node", str(script), plugin_url],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert json.loads(out.stdout) == {"allow": "ask", "none": "ask", "deny": "deny"}
