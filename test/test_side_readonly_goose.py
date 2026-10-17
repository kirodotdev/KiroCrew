"""The safe scope of a goose Side Chat turn, end to end through the gate.

A goose tool_call frame and its permission request -- shaped as goose 1.50.1 sent
them live -- go through the real ACP parser, the provider's event conversion and
``stream_and_collect``. On a ``side_read_only`` session under READ_ONLY goose's own
``tree`` and ``read_image`` run, and every write, shell, platform-extension and
MCP call is refused, as is a tool another extension states under an allowed name.
On a normal session the frame names no built-in, so the normal-turn gate sees the
input it saw before. The goose home a side session runs on is built without the
user's ``permission.yaml`` and without their ``extensions`` block.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from kiro_crew.acp import _dispatch
from kiro_crew.acp.harness import goose as goose_mod
from kiro_crew.acp.harness.goose import build_side_goose_root, goose_config_dir
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_GOOSE, ACP_BACKENDS_SIDE_READONLY
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

#: The side home is POSIX-only (``goose.side_home_supported``); these tests build one.
_posix_home = pytest.mark.skipif(
    os.name == "nt", reason="a goose side home is built on POSIX hosts only"
)

# (extension, toolName as goose states it, rawInput)
_READS = [
    ("developer", "tree", {"path": "/work"}),
    ("developer", "read_image", {"path": "/work/a.png"}),
]
_WRITES_AND_EXEC = [
    ("developer", "shell", {"command": "touch /work/x"}),
    ("developer", "write", {"path": "/work/x", "content": "x"}),
    ("developer", "edit", {"path": "/work/x", "before": "a", "after": "b"}),
    ("summon", "delegate", {"instructions": "hi"}),
    ("summon", "load", {"source": "x"}),
    ("todo", "todo__todo_write", {"content": "x"}),
    ("apps", "apps__create_app", {"name": "x"}),
    ("extensionmanager", "extensionmanager__manage_extensions", {"action": "enable"}),
    ("analyze", "analyze", {"path": "/work"}),
]
# A tool another extension serves under an allowed name. goose prefixes it with the
# extension (captured live: a Crew server named ``developer`` serving ``tree`` is
# stated ``developer__tree``), and an extension other than the builtin one never
# serves the bare name -- held here in case a release does.
_SHADOWS = [
    ("developer", "developer__tree", {"path": "/work"}),
    ("developer", "developer__read_image", {"path": "/work/a.png"}),
    ("shadowx", "shadowx__tree", {"path": "/work"}),
    ("shadowx", "tree", {"path": "/work"}),
    ("analyze", "read_image", {"path": "/work/a.png"}),
]


def _goose_event(
    extension, tool, raw_input, *, side_read_only: bool, meta: bool = True, kind: str = "other"
):
    """One goose frame pair through the real parser, with the client's caches."""
    caches: dict = {
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "harness_tool_name_cache": {},
    }
    title = f"{tool} · {raw_input.get('path', '')}"
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_1",
        "title": title,
        "rawInput": raw_input,
    }
    if meta:
        update["_meta"] = {"goose": {"toolCall": {"toolName": tool, "extensionName": extension}}}
    _dispatch.parse_session_update(update, **caches)
    msg = JsonRpcMessage(
        id="perm-1",
        method="session/request_permission",
        params={
            "toolCall": {
                "toolCallId": "call_1",
                "kind": kind,
                "status": "pending",
                "title": title,
                "rawInput": raw_input,
            },
            "options": [
                {"optionId": "allow_always", "name": "allow_always", "kind": "allow_always"},
                {"optionId": "allow_once", "name": "allow_once", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "reject_once", "kind": "reject_once"},
            ],
        },
    )
    event, _ = _dispatch.build_permission_event(
        msg,
        **caches,
        harness_backend=ACP_BACKEND_GOOSE,
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


def test_goose_is_a_side_readonly_member():
    assert ACP_BACKEND_GOOSE in ACP_BACKENDS_SIDE_READONLY


@pytest.mark.asyncio
@pytest.mark.parametrize(("extension", "tool", "raw"), _READS)
async def test_a_goose_side_turn_runs_its_read_tools(extension, tool, raw):
    event = _goose_event(extension, tool, raw, side_read_only=True)
    assert event.harness_builtin_tool == "fs_read"
    assert event.mcp_server_name == ""
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == ["perm-1"]
    assert provider.rejected == []


@pytest.mark.asyncio
@pytest.mark.parametrize("side_read_only", [True, False], ids=["side", "normal"])
@pytest.mark.parametrize(("extension", "tool", "raw"), _WRITES_AND_EXEC)
async def test_writes_shell_and_other_extensions_are_never_proven_read_only(
    extension, tool, raw, side_read_only
):
    """No write or exec call gains a read built-in, on a side or a normal turn,
    so READ_ONLY refuses it either way."""
    event = _goose_event(extension, tool, raw, side_read_only=side_read_only)
    assert event.harness_builtin_tool == ""
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == []
    assert provider.rejected == ["perm-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("extension", "tool", "raw"), _SHADOWS)
async def test_a_tool_stated_under_an_allowed_name_by_another_extension_is_refused(
    extension, tool, raw
):
    event = _goose_event(extension, tool, raw, side_read_only=True)
    assert event.harness_builtin_tool == ""
    assert event.mcp_server_name == extension
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["edit", "execute", "delete"])
async def test_a_proven_read_with_a_stated_write_kind_is_refused(kind):
    """The pair proves the tool; a non-read kind the frame does state still narrows."""
    event = _goose_event("developer", "tree", {"path": "/work"}, side_read_only=True, kind=kind)
    assert event.tool_kind == kind
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == []


@pytest.mark.asyncio
async def test_a_frame_with_no_goose_identity_is_not_named_by_its_title():
    """Only goose's own ``_meta`` stamp names the tool; a title saying ``tree`` is prose."""
    event = _goose_event("developer", "tree", {"path": "/work"}, side_read_only=True, meta=False)
    assert event.harness_builtin_tool == ""
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["tree", "read_image"])
async def test_a_read_of_a_sensitive_path_is_refused_on_a_side_turn(tool, tmp_path):
    """The proven read still meets the sensitive-path keystone on its argument."""
    target = str(Path.home() / ".ssh" / "id_rsa")
    event = _goose_event("developer", tool, {"path": target}, side_read_only=True)
    assert event.harness_builtin_tool == "fs_read"
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == []
    assert provider.rejected == ["perm-1"]


@_posix_home
@pytest.mark.asyncio
async def test_the_linked_secrets_file_in_the_side_home_is_refused(tmp_path, monkeypatch):
    """The side home links goose's secrets.yaml; a read through that link is the
    credential leaf and is refused, whatever goose's own config pre-approves."""
    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / ".config" / "goose"
    source.mkdir(parents=True)
    (source / "secrets.yaml").write_text("OPENAI_API_KEY: x\n", encoding="utf-8")
    (source / "permission.yaml").write_text(
        "user:\n  always_allow: [read_image, tree]\n", encoding="utf-8"
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = build_side_goose_root(source, scratch)
    linked = root / "config" / "secrets.yaml"
    assert linked.is_symlink()
    event = _goose_event("developer", "read_image", {"path": str(linked)}, side_read_only=True)
    provider = await _gate(event, ToolApprovalPolicy.READ_ONLY)
    assert provider.approved == []


@pytest.mark.parametrize(("extension", "tool", "raw"), _READS + _WRITES_AND_EXEC)
def test_a_normal_turn_event_is_unchanged_by_the_side_mapping(extension, tool, raw):
    """Off a side session nothing is mapped: the event equals the one main built."""
    event = _goose_event(extension, tool, raw, side_read_only=False)
    assert event.harness_builtin_tool == ""
    assert event.mcp_server_name == ("" if tool == "shell" else extension)
    side = _goose_event(extension, tool, raw, side_read_only=True)
    if side.harness_builtin_tool:
        # A proven side read differs from the normal event in exactly these three.
        assert (side.mcp_server_name, side.tool_kind) == ("", "read")
        assert event.tool_kind == "other"
        ignore = {"harness_builtin_tool", "mcp_server_name", "tool_kind"}
        normal = {k: v for k, v in dataclasses.asdict(event).items() if k not in ignore}
        as_side = {k: v for k, v in dataclasses.asdict(side).items() if k not in ignore}
        assert normal == as_side


# ── The side home ───────────────────────────────────────────────────────────


def _user_config(tmp_path: Path) -> Path:
    source = tmp_path / "user-config"
    source.mkdir()
    (source / "config.yaml").write_text(
        "GOOSE_PROVIDER: openai\n"
        "GOOSE_MODEL: stub\n"
        "extensions:\n"
        "  shadowx:\n"
        "    enabled: true\n"
        "    type: stdio\n"
        "    cmd: python3\n",
        encoding="utf-8",
    )
    (source / "permission.yaml").write_text(
        "user:\n  always_allow: [shell, tree]\n  ask_before: []\n  never_allow: []\n",
        encoding="utf-8",
    )
    (source / "permission.yaml.lock").write_text("", encoding="utf-8")
    (source / "secrets.yaml").write_text("OPENAI_API_KEY: x\n", encoding="utf-8")
    (source / "custom_providers").mkdir()
    return source


@_posix_home
def test_the_side_home_leaves_out_permission_yaml_and_extensions(tmp_path):
    source = _user_config(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = build_side_goose_root(source, scratch)
    config = root / "config"
    assert not (config / "permission.yaml").exists()
    assert not (config / "permission.yaml.lock").exists()
    assert (config / "secrets.yaml").resolve() == (source / "secrets.yaml").resolve()
    assert (config / "custom_providers").is_symlink()
    copied = config / "config.yaml"
    assert not copied.is_symlink()
    assert yaml.safe_load(copied.read_text(encoding="utf-8")) == {
        "GOOSE_PROVIDER": "openai",
        "GOOSE_MODEL": "stub",
    }
    assert not (root / "data").exists()


@_posix_home
def test_the_side_home_is_rebuilt_on_every_spawn(tmp_path):
    """A permission.yaml goose wrote into an earlier side home does not survive."""
    source = _user_config(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = build_side_goose_root(source, scratch)
    (root / "config" / "permission.yaml").write_text(
        "user:\n  always_allow: [tree]\n", encoding="utf-8"
    )
    again = build_side_goose_root(source, scratch)
    assert again == root
    assert not (again / "config" / "permission.yaml").exists()


@_posix_home
def test_the_side_home_needs_no_user_config(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = build_side_goose_root(tmp_path / "absent", scratch)
    assert list((root / "config").iterdir()) == []


@_posix_home
@pytest.mark.parametrize("name", ["Permission.yaml", "PERMISSION.YAML", "permission.YAML.lock"])
def test_a_case_variant_of_permission_yaml_is_not_linked(tmp_path, name):
    """On case-insensitive storage goose opens ``Permission.yaml`` as its own store."""
    source = tmp_path / "user-config"
    source.mkdir()
    (source / name).write_text("user:\n  always_allow: [shell]\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = build_side_goose_root(source, scratch)
    assert list((root / "config").iterdir()) == []


@_posix_home
def test_a_home_that_cannot_be_built_refuses_the_side_session(tmp_path, monkeypatch):
    source = _user_config(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    def _no_links(*args, **kwargs):
        raise OSError("symlink privilege not held")

    monkeypatch.setattr(goose_mod.os, "symlink", _no_links)
    with pytest.raises(AcpError):
        build_side_goose_root(source, scratch)


@pytest.mark.asyncio
async def test_a_host_without_a_side_home_keeps_goose_side_turns_tool_free(monkeypatch):
    """Where no separate home can be built, the side turn and the footer both
    answer no tools, and a launch asked for one refuses."""
    from kiro_crew.dashboard.handlers import side as side_mod

    monkeypatch.setattr(side_mod, "goose_side_home_supported", lambda: False)
    assert await side_mod._side_read_only_tools(ACP_BACKEND_GOOSE) is False
    monkeypatch.setattr(side_mod, "goose_side_home_supported", lambda: True)
    assert await side_mod._side_read_only_tools(ACP_BACKEND_GOOSE) is True


@_posix_home
@pytest.mark.parametrize("body", ["- a list\n", "key: [unclosed\n"])
def test_an_unreadable_config_refuses_the_side_session(tmp_path, body):
    source = tmp_path / "user-config"
    source.mkdir()
    (source / "config.yaml").write_text(body, encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(AcpError):
        build_side_goose_root(source, scratch)


@pytest.mark.skipif(os.name == "nt", reason="the POSIX ladder of goose's config directory")
def test_goose_config_dir_follows_goose_resolution(tmp_path):
    assert goose_config_dir({"GOOSE_PATH_ROOT": "/r", "XDG_CONFIG_HOME": "/x"}) == Path("/r/config")
    assert goose_config_dir({"GOOSE_PATH_ROOT": "rel", "XDG_CONFIG_HOME": "/x"}) == Path("/x/goose")
    assert goose_config_dir({"XDG_CONFIG_HOME": "rel", "HOME": "/h"}) == Path("/h/.config/goose")
    assert goose_config_dir({"HOME": "/h"}) == Path("/h/.config/goose")


# ── The launch: which session gets the side home, and its array ──────────────


class _Session:
    """The few members ``GooseLaunch.resolve_spawn`` reads off its session."""

    def __init__(self, *, side_read_only: bool, scratch: Path | None) -> None:
        self._side_read_only = side_read_only
        self._scratch_dir: Path | None = None
        self._planned_scratch = scratch
        self._extra_env: dict[str, str] = {}

    async def _prepare_session_mcp(self) -> None:
        return None


async def _launch_env(monkeypatch, tmp_path, session: _Session) -> dict[str, str]:
    from kiro_crew.acp.harness import goose as goose_mod
    from kiro_crew.acp.harness.base import SpawnContext

    async def _resolved(backend):
        return "goose", ["goose", "acp"], "goose acp", "goose"

    async def _no_mask(preflight, backend, mode):
        return ()

    monkeypatch.setattr(goose_mod.launch_mod, "resolve_self_served_launch", _resolved)
    monkeypatch.setattr(goose_mod.launch_mod, "_run_preflight_bounded", _no_mask)
    adapter = goose_mod.GooseLaunch()
    ctx = SpawnContext.__new__(SpawnContext)
    object.__setattr__(ctx, "session", session)
    object.__setattr__(ctx, "sandbox_mode", "off")
    await adapter.resolve_spawn(ctx)
    # The launch tail allocates the scratch window after the plan.
    session._scratch_dir = session._planned_scratch

    async def _to_thread(fn, *args):
        return fn(*args)

    env = {"HOME": str(tmp_path / "home")}
    await adapter.prepare_spawn_env(env, to_thread=_to_thread)
    return env


@_posix_home
@pytest.mark.asyncio
async def test_a_side_session_is_spawned_on_its_own_goose_home(monkeypatch, tmp_path):
    user = tmp_path / "home" / ".config" / "goose"
    user.mkdir(parents=True)
    (user / "permission.yaml").write_text("user:\n  always_allow: [shell]\n", encoding="utf-8")
    (user / "secrets.yaml").write_text("K: v\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    session = _Session(side_read_only=True, scratch=scratch)
    env = await _launch_env(monkeypatch, tmp_path, session)
    root = Path(env["GOOSE_PATH_ROOT"])
    assert root.parent == scratch
    assert (root / "config" / "secrets.yaml").is_symlink()
    assert not (root / "config" / "permission.yaml").exists()
    assert session._extra_env["GOOSE_MODE"] == "approve"


@pytest.mark.asyncio
async def test_a_normal_session_keeps_the_users_goose_home(monkeypatch, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = await _launch_env(monkeypatch, tmp_path, _Session(side_read_only=False, scratch=scratch))
    assert "GOOSE_PATH_ROOT" not in env
    assert list(scratch.iterdir()) == []


@_posix_home
@pytest.mark.asyncio
async def test_a_side_session_with_no_scratch_window_is_refused(monkeypatch, tmp_path):
    with pytest.raises(AcpError):
        await _launch_env(monkeypatch, tmp_path, _Session(side_read_only=True, scratch=None))


@pytest.mark.parametrize("side_read_only", [True, False], ids=["side", "normal"])
def test_a_side_session_mounts_no_mcp_server(side_read_only):
    from kiro_crew.acp.client import AcpClient

    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_GOOSE
    client._session_id = "goose-1"
    client._side_read_only = side_read_only
    placed = [{"name": "kirocrew-core", "command": "x", "args": [], "env": []}]
    client._session_mcp_servers = lambda: placed  # type: ignore[method-assign]
    assert client._goose_session_mcp_servers() == ([] if side_read_only else placed)
