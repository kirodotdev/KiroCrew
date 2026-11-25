"""A goose chat session's own goose home, and the spec allowlists it lets Crew enforce.

Every goose session runs on a goose home Crew builds for the spawn
(``build_session_goose_root``): the user's ``permission.yaml`` is not linked, so no
``user.always_allow`` entry runs a shell or write call unasked, and the ``summon``
(``delegate``) and ``extensionmanager`` platform extensions are switched off. Its
``data`` and ``state`` are the user's own, so goose's session store is unchanged.
With every shell and write call asking, goose joins
``ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS``: the gate's ``spec-shell-allowlist``
and ``spec-write-allowlist`` rows refuse what a spec does not cover, a scheduled
job's ``AUTO_APPROVE`` included.

The shell request is replayed from the committed live capture. The write and
``delegate`` frames are goose 1.50.1's measured shapes: ``kind: "other"``,
``rawInput`` with ``path`` (or ``instructions``), and the tool's own extension in
``_meta.goose.toolCall``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401

from kiro_crew import permission_floor
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import goose as goose_mod
from kiro_crew.acp.harness.goose import build_session_goose_root
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import AcpEvent, JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_GOOSE
from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS,
)

_posix = pytest.mark.skipif(os.name == "nt", reason="a goose home is built on POSIX hosts only")
_FRAMES = Path(__file__).parent / "fixtures" / "acp_frames" / "goose"
_AGENT = "probe"
_SESSION = "goose-session-1"
_LIVE_COMMAND = "echo goose-wire-probe"
_SHELL = {"shell": {"denyByDefault": True, "allowedCommands": ["git status"]}}
_WRITE = {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["docs/**"]}}
_OPTIONS = [
    {"optionId": kind, "name": kind, "kind": kind}
    for kind in ("allow_always", "allow_once", "reject_once", "reject_always")
]


# ── The home ─────────────────────────────────────────────────────────────────


def _user_home(tmp_path: Path) -> dict[str, str]:
    """A user's goose config that pre-approves shell and write and turns summon on."""
    config = tmp_path / "home" / ".config" / "goose"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text(
        "GOOSE_PROVIDER: openai\n"
        "extensions:\n"
        "  summon:\n    enabled: true\n    type: platform\n    name: summon\n"
        "  extensionmanager:\n    enabled: true\n    type: platform\n    name: x\n",
        encoding="utf-8",
    )
    (config / "permission.yaml").write_text(
        "user:\n  always_allow: [shell, write]\n", encoding="utf-8"
    )
    (config / "secrets.yaml").write_text("OPENAI_API_KEY: x\n", encoding="utf-8")
    return {"HOME": str(tmp_path / "home")}


@_posix
def test_the_session_home_drops_pre_approvals_and_delegate(tmp_path: Path) -> None:
    env = _user_home(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = build_session_goose_root(env, scratch)
    config = root / "config"
    assert not (config / "permission.yaml").exists()
    assert (config / "secrets.yaml").is_symlink()
    data = yaml.safe_load((config / "config.yaml").read_text(encoding="utf-8"))
    assert data == {
        "GOOSE_PROVIDER": "openai",
        "extensions": {"summon": {"enabled": False}, "extensionmanager": {"enabled": False}},
    }


@_posix
def test_the_session_home_keeps_the_users_session_store(tmp_path: Path) -> None:
    """goose's data and state are the user's, so session/load finds earlier sessions."""
    env = _user_home(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = build_session_goose_root(env, scratch)
    home = tmp_path / "home"
    assert (root / "data").resolve() == (home / ".local" / "share" / "goose").resolve()
    assert (root / "state").resolve() == (home / ".local" / "state" / "goose").resolve()
    xdg = {**env, "XDG_DATA_HOME": str(tmp_path / "d"), "XDG_STATE_HOME": str(tmp_path / "s")}
    root = build_session_goose_root(xdg, scratch)
    assert (root / "data").resolve() == (tmp_path / "d" / "goose").resolve()
    assert (root / "state").resolve() == (tmp_path / "s" / "goose").resolve()


@_posix
def test_rebuilding_the_home_leaves_the_users_store_alone(tmp_path: Path) -> None:
    """A grant goose wrote last spawn is gone; the linked session store is not touched."""
    env = _user_home(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = build_session_goose_root(env, scratch)
    (root / "config" / "permission.yaml").write_text("user:\n  always_allow: [shell]\n")
    (root / "data" / "sessions.db").write_text("kept", encoding="utf-8")
    again = build_session_goose_root(env, scratch)
    assert not (again / "config" / "permission.yaml").exists()
    assert (again / "data" / "sessions.db").read_text(encoding="utf-8") == "kept"


@_posix
def test_a_home_that_cannot_be_built_refuses_the_session(tmp_path, monkeypatch) -> None:
    env = _user_home(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    def _no_links(*args, **kwargs):
        raise OSError("no links here")

    monkeypatch.setattr(goose_mod.os, "symlink", _no_links)
    with pytest.raises(AcpError, match="goose session is refused"):
        build_session_goose_root(env, scratch)


class _Session:
    """The few members ``GooseLaunch.resolve_spawn`` reads off its session."""

    def __init__(self, scratch: Path | None) -> None:
        self._side_read_only = False
        self._scratch_dir: Path | None = scratch
        self._extra_env: dict[str, str] = {}

    async def _prepare_session_mcp(self) -> None:
        return None


async def _spawn_env(monkeypatch, tmp_path: Path, scratch: Path | None) -> dict[str, str]:
    from kiro_crew.acp.harness.base import SpawnContext

    async def _resolved(backend):
        return "goose", ["goose", "acp"], "goose acp", "goose"

    async def _no_mask(preflight, backend, mode):
        return ()

    monkeypatch.setattr(goose_mod.launch_mod, "resolve_self_served_launch", _resolved)
    monkeypatch.setattr(goose_mod.launch_mod, "_run_preflight_bounded", _no_mask)
    adapter = goose_mod.GooseLaunch()
    ctx = SpawnContext.__new__(SpawnContext)
    object.__setattr__(ctx, "session", _Session(scratch))
    object.__setattr__(ctx, "sandbox_mode", "off")
    await adapter.resolve_spawn(ctx)

    async def _to_thread(fn, *args):
        return fn(*args)

    env = _user_home(tmp_path)
    await adapter.prepare_spawn_env(env, to_thread=_to_thread)
    return env


@_posix
def test_a_chat_session_is_spawned_on_its_own_home(monkeypatch, tmp_path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = asyncio.run(_spawn_env(monkeypatch, tmp_path, scratch))
    root = Path(env["GOOSE_PATH_ROOT"])
    assert root.parent == scratch
    assert not (root / "config" / "permission.yaml").exists()


@_posix
def test_a_chat_session_with_no_scratch_window_is_refused(monkeypatch, tmp_path) -> None:
    with pytest.raises(AcpError, match="scratch directory"):
        asyncio.run(_spawn_env(monkeypatch, tmp_path, None))


def test_a_host_that_builds_no_home_refuses_the_session(monkeypatch, tmp_path) -> None:
    """Windows: never started on the user's own home."""
    monkeypatch.setattr(goose_mod, "side_home_supported", lambda: False)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(AcpError, match="Windows"):
        asyncio.run(_spawn_env(monkeypatch, tmp_path, scratch))
    assert list(scratch.iterdir()) == []


def test_the_shared_goose_process_is_not_routed_to_yet() -> None:
    """``GooseHarness`` runs on the operator's goose home: no session may reach it
    while goose is enforced on the strength of the per-chat home."""
    assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_ACP_RUNTIME


# ── The allowlists Crew now enforces ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def _ungoverned(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    """No governance profile on the host decides any of these calls."""
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance_profiles as gp

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _write_spec(agents_dir: Path, tools_settings: dict[str, Any] | None) -> None:
    spec: dict[str, Any] = {"name": _AGENT, "prompt": "p", "tools": ["*"], "mcpServers": {}}
    if tools_settings is not None:
        spec["toolsSettings"] = tools_settings
    (agents_dir / f"{_AGENT}.json").write_text(json.dumps(spec), encoding="utf-8")


def _client(work_dir: Path) -> AcpClient:
    """A goose client whose array resolution ran, as the spawn path runs it."""
    client = AcpClient(
        work_dir=work_dir, agent=_AGENT, acp_backend=ACP_BACKEND_GOOSE, session_key="cron:probe"
    )
    client._session_mcp_servers()
    client._session_id = _SESSION
    return client


def _feed_update(client: AcpClient, params: dict[str, Any]) -> None:
    """One ``session/update`` through both parsers the turn loop runs on it."""
    msg = JsonRpcMessage(method="session/update", params=params)
    client._extract_tool_event(msg)
    client._extract_tool_call_refinement(msg)


def _live_shell_request(client: AcpClient) -> AcpEvent:
    """The live capture's tool_call frame, then its permission request."""
    text = (_FRAMES / "turn-live.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines()[1:]:
        frame = json.loads(line)
        method = frame.get("method")
        if method == "session/update":
            _feed_update(client, frame["params"])
        elif method == "session/request_permission":
            msg = JsonRpcMessage(id=frame["id"], method=method, params=frame["params"])
            event = client._build_permission_event(msg)
            assert event is not None
            return event
    raise AssertionError("no permission request in the capture")


def _measured_request(
    client: AcpClient, tool: str, extension: str, raw: dict[str, Any], title: str
) -> JsonRpcMessage:
    """goose 1.50.1's tool_call frame for *tool*, then its permission request."""
    call_id = f"call-{tool}"
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": title,
        "status": "pending",
        "rawInput": raw,
        "_meta": {"goose": {"toolCall": {"toolName": tool, "extensionName": extension}}},
    }
    _feed_update(client, {"sessionId": _SESSION, "update": update})
    tool_call = {"toolCallId": call_id, "kind": "other", "status": "pending", "title": title}
    return JsonRpcMessage(
        id=f"req-{call_id}",
        method="session/request_permission",
        params={
            "sessionId": _SESSION,
            "toolCall": {**tool_call, "rawInput": raw},
            "options": _OPTIONS,
        },
    )


def _write_request(client: AcpClient, tool: str, target: Path) -> AcpEvent:
    raw: dict[str, Any] = {"path": str(target), "content": "x\n"}
    if tool == "edit":
        raw = {"path": str(target), "before": "a", "after": "b"}
    msg = _measured_request(client, tool, "developer", raw, f"{tool} · {target}")
    event = client._build_permission_event(msg)
    assert event is not None
    return event


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_a_built_in_write_is_classified_and_not_a_foreign_server(
    tmp_path, agents_dir, tool
) -> None:
    """Reported with no server, goose's own write still reads as classified, so
    neither identity refusal fires before the approval is asked for."""
    _write_spec(agents_dir, None)
    client = _client(tmp_path)
    event = _write_request(client, tool, tmp_path / "docs" / "a.md")
    assert event.mcp_server_name == "" and event.tool_kind == "edit"
    assert client._unclassified_tool_call(event) is False
    assert client._foreign_mcp_identity(event) is False


def _auto_refusal(event: AcpEvent) -> str | None:
    """What an ``AUTO_APPROVE`` job's gate answers: a reason refuses, ``None`` runs it."""
    return permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)


def test_goose_is_a_member() -> None:
    assert ACP_BACKEND_GOOSE in ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS


def test_an_auto_mode_job_refuses_a_command_the_spec_never_allowed(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, _SHELL)
    reason = _auto_refusal(_live_shell_request(_client(tmp_path)))
    assert reason is not None and "denyByDefault" in reason


def test_an_auto_mode_job_still_runs_a_listed_command(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, {"shell": {"denyByDefault": True, "allowedCommands": [_LIVE_COMMAND]}})
    assert _auto_refusal(_live_shell_request(_client(tmp_path))) is None


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_an_auto_mode_job_refuses_a_write_outside_the_allowed_paths(
    tmp_path, agents_dir, tool
) -> None:
    _write_spec(agents_dir, _WRITE)
    event = _write_request(_client(tmp_path), tool, tmp_path / "src" / "a.py")
    reason = _auto_refusal(event)
    assert reason is not None and "fallbackAction" in reason


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_an_auto_mode_job_still_writes_inside_the_allowed_paths(tmp_path, agents_dir, tool) -> None:
    _write_spec(agents_dir, _WRITE)
    event = _write_request(_client(tmp_path), tool, tmp_path / "docs" / "a.md")
    assert _auto_refusal(event) is None


def test_an_always_answer_is_sent_as_once(tmp_path, agents_dir, monkeypatch) -> None:
    """The live request offers ``allow_always``; the client answers ``allow_once``."""
    _write_spec(agents_dir, None)
    client = _client(tmp_path)
    event = _live_shell_request(client)
    monkeypatch.setattr(permission_floor, "refusal_for", lambda *_a, **_k: None)
    sent: list[str] = []

    async def _send(request_id, result, **kwargs):
        sent.append(result["outcome"]["optionId"])

    monkeypatch.setattr(client, "_send_response", _send)
    assert asyncio.run(client.approve_tool(event.request_id, always=True)) is True
    assert sent == ["allow_once"]


@pytest.mark.parametrize(
    ("tool", "extension", "raw"),
    [
        ("delegate", "summon", {"instructions": "Run echo probe and write a file."}),
        ("extensionmanager__manage_extensions", "extensionmanager", {"action": "enable"}),
    ],
)
def test_a_restored_session_cannot_start_a_delegate(tmp_path, agents_dir, tool, extension, raw):
    """A session created before the home existed keeps ``summon`` in goose's own
    record. Its ``delegate`` still asks first, naming the ``summon`` extension, and the
    client refuses that as a server it never placed, on every approval policy."""
    _write_spec(agents_dir, None)
    client = _client(tmp_path)
    msg = _measured_request(client, tool, extension, raw, tool)
    rejected: list[str] = []

    async def _reject(request_id: str) -> None:
        rejected.append(request_id)

    async def _never(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("a delegate must not be approved")

    client.reject_tool = _reject  # type: ignore[method-assign]
    client.approve_tool = _never  # type: ignore[method-assign]
    asyncio.run(client._handle_permission(msg))
    assert rejected == [msg.id]
