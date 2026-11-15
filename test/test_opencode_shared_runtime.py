"""opencode on the shared runtime (``OpencodeHarness``): one process's gates, each
session's own checks.

On ``AcpClient`` an opencode session owns its process, so the version gate, the
routing read-back and the deny rules are that session's. On one process several
sessions share, the gates run once at the spawn under the child's own environment,
and what is per session stays per session: the deny rules in force hold each
session's array, a withheld server opencode mounts natively refuses that session,
the hosted fall-back refusal closes that session alone, and a shell permission that
arrives ahead of its command waits for it. No session is routed here yet; the harness
is reached through ``harness_for``.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp import session_handle_permissions as sh_permissions
from kiro_crew.acp._dispatch import scoped_tool_cache_key
from kiro_crew.acp.harness import SpawnContext, harness_for
from kiro_crew.acp.harness import opencode as opencode_mod
from kiro_crew.acp.no_model_provider import AcpNoModelProvider
from kiro_crew.acp.session_handle_runtime_contract import host_defers_shell_permission
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.acp.types import (
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_OPENCODE,
    METHOD_SESSION_CLOSE,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_ACP_RUNTIME
from kiro_crew.testing.ids import UNALLOCATABLE_PID

_ARGV = ["/opt/bin/opencode", "acp"]


@pytest.fixture
def gates(monkeypatch):
    """The spawn's gates, recorded: what each was asked and with which session facts."""
    seen: dict[str, Any] = {"disabled": ()}

    async def _launch(backend: str) -> tuple[str, list[str], str, str]:
        return _ARGV[0], list(_ARGV), "opencode acp", "opencode"

    async def _mask(preflight: Any, backend: str, mode: str) -> tuple[str, ...]:
        return ("/home/u/.local/share/opencode",)

    async def _version(probe: Any, binary: str, **_kw: Any) -> tuple[str, str]:
        seen["version_env"] = {**probe._agent_env_for_spawn(), **probe._extra_env}
        seen["version_cwd"] = probe._spawn_work_dir
        return seen.get("version_issue", ("", ""))

    async def _wrap(argv: list[str], **_kw: Any) -> tuple[list[str], str]:
        return list(argv), ""

    def _verify(probe: Any, backend: str, argv: list[str], config_content: str) -> tuple[str, str]:
        seen["readback_argv"] = argv
        seen["readback_env"] = {**probe._agent_env_for_spawn(), **probe._extra_env}
        seen["readback_cwd"] = probe._spawn_work_dir
        seen["seed"] = json.loads(config_content)
        read_back = probe._harness_read_back()
        read_back.config_model = seen.get("config_model", "")
        read_back.native_mounts = frozenset(seen.get("native", ()))
        read_back.config_mcp_servers = tuple(seen.get("config_servers", ()))
        read_back.denies_unenforced = frozenset(seen.get("unenforced", ()))
        return seen.get("routing_issue", ("", ""))

    monkeypatch.setattr(opencode_mod.launch_mod, "resolve_self_served_launch", _launch)
    monkeypatch.setattr(opencode_mod.launch_mod, "_run_preflight_bounded", _mask)
    monkeypatch.setattr(opencode_mod, "_opencode_version_refusal", _version)
    monkeypatch.setattr(opencode_mod, "wrap_argv_async", _wrap)
    monkeypatch.setattr(opencode_mod, "_verify_opencode_routing", _verify)
    monkeypatch.setattr(
        opencode_mod, "_spec_disabled_tools", lambda agent, work_dir: tuple(seen["disabled"])
    )
    return seen


def _ctx(tmp_path: Any) -> SpawnContext:
    return SpawnContext(
        agent="kirocrew",
        work_dir=tmp_path,
        model=None,
        environ={},
        home=tmp_path,
        session=None,
        agent_env={"OPENCODE_CONFIG_DIR": "/agent/env"},
        extra_env={"XDG_CONFIG_HOME": "/chat/xdg"},
    )


def _spawned(gates: dict[str, Any], tmp_path: Any) -> Any:
    """A harness and the plan its spawn returned (``harness.plan``, for these tests only:
    the harness itself holds no state)."""
    harness = harness_for(ACP_BACKEND_OPENCODE)
    plan = asyncio.run(harness.resolve_spawn(_ctx(tmp_path)))
    assert vars(harness) == {}
    harness.plan = plan
    return harness


# ── The harness's answers ──


def test_the_harness_is_registered_and_routes_nothing():
    assert ACP_BACKEND_OPENCODE not in ACP_BACKENDS_ACP_RUNTIME
    harness = harness_for(ACP_BACKEND_OPENCODE)
    assert harness.per_session_state is True
    assert harness.defers_shell_permission is True
    assert harness.teardown.method == METHOD_SESSION_CLOSE
    assert harness.teardown.notification is False
    assert harness.host_answered_methods == ()
    assert harness.verifies_agent_activation is False


def test_the_handshake_is_the_one_the_client_sends():
    from kiro_crew.acp import client_start
    from kiro_crew.acp.client_native_children import client_capabilities_for

    harness = harness_for(ACP_BACKEND_OPENCODE)
    assert harness.protocol_version == client_start.PROTOCOL_VERSION_OPENCODE
    assert harness.client_capabilities == client_capabilities_for(ACP_BACKEND_OPENCODE)


# ── The spawn's gates, once per process ──


def test_the_gates_run_under_the_childs_own_environment_and_folder(gates, tmp_path):
    """opencode finds its config files from its environment, so the read-back reads
    the ones the child will: agent.env and the chat's overlay, in the child's folder."""
    _spawned(gates, tmp_path)
    expected = {"OPENCODE_CONFIG_DIR": "/agent/env", "XDG_CONFIG_HOME": "/chat/xdg"}
    assert gates["version_env"] == expected
    assert gates["readback_env"] == expected
    assert gates["readback_cwd"] == str(tmp_path)
    assert gates["readback_argv"] == [_ARGV[0], "debug", "config"]


def test_the_seed_carries_the_spawning_agents_deny_rules_and_reaches_the_child(gates, tmp_path):
    gates["disabled"] = (("github", "delete_repo"),)
    harness = _spawned(gates, tmp_path)
    assert gates["seed"]["permission"] == {"*": "ask", "github_delete_repo": {"*": "deny"}}
    assert harness.plan.spawn_env == {"OPENCODE_CONFIG_CONTENT": json.dumps(gates["seed"])}
    env: dict[str, str] = {"KIRO_API_KEY": "k", "PATH": "/bin"}
    harness.apply_spawn_env(env)
    assert "KIRO_API_KEY" not in env
    assert env["PATH"] == "/bin"


def test_a_failed_read_back_refuses_the_spawn(gates, tmp_path):
    gates["routing_issue"] = ("the resolved configuration could not be parsed", "fix it")
    with pytest.raises(AcpToolGateUnroutable, match="could not be parsed"):
        _spawned(gates, tmp_path)


def test_a_binary_outside_the_tested_major_refuses_the_spawn(gates, tmp_path):
    gates["version_issue"] = ("the resolved binary reports major 2", "pin 1.x")
    with pytest.raises(AcpToolGateUnroutable, match="major 2"):
        _spawned(gates, tmp_path)


# ── Each session's own checks ──


async def _prepare(harness: Any, *, model: str = "", **extra: Any) -> Any:
    host = {"host_state": harness.plan.host_state} if hasattr(harness, "plan") else {}
    return await harness.prepare_session(
        work_dir="/w",
        agent="kirocrew",
        model=model,
        session_key="dashboard:a",
        channel_id="",
        memory_mode="persistent",
        **{**host, **extra},
    )


def test_a_process_with_no_read_back_serves_no_session():
    with pytest.raises(AcpToolGateUnroutable, match="never read back"):
        asyncio.run(_prepare(harness_for(ACP_BACKEND_OPENCODE)))


def test_each_projection_is_held_to_the_rules_in_force(gates, tmp_path):
    """A rule the read-back found outranked is not in force, so its server is withheld."""
    gates["disabled"] = (("github", "delete_repo"), ("jira", "drop"))
    gates["unenforced"] = ("jira_drop",)
    harness = _spawned(gates, tmp_path)
    state = asyncio.run(_prepare(harness))
    assert harness.projection_extras(state) == {
        "harness_denies_in_force": frozenset({"github_delete_repo"})
    }


def test_a_withheld_server_opencode_mounts_itself_refuses_only_that_session(gates, tmp_path):
    gates["disabled"] = (("github", "delete_repo"),)
    harness = _spawned(gates, tmp_path)
    harness.plan.host_state.native_mounts = frozenset({"github"})
    # In force: the server is narrowed, not withheld, so its native mount is fine.
    asyncio.run(_prepare(harness))
    harness.plan.host_state.denies_in_force = frozenset()
    with pytest.raises(AcpToolGateUnroutable, match="mounts that server from its own config"):
        asyncio.run(_prepare(harness))


def test_the_permission_parser_gets_the_placed_and_the_configured_servers(gates, tmp_path):
    gates["config_servers"] = ("my-own",)
    harness = _spawned(gates, tmp_path)
    state = asyncio.run(_prepare(harness))
    harness.session_meta(state, [{"name": "kirocrew-core"}, {"name": "my-own"}, {"x": 1}])
    assert harness.harness_mcp_server_names(state) == ("kirocrew-core", "my-own")


def _hosted(current: str) -> dict[str, Any]:
    return {
        "sessionId": "s-1",
        "models": {
            "currentModelId": current,
            "availableModels": [{"modelId": "opencode/big"}, {"modelId": "opencode/small"}],
        },
    }


def _start(harness: Any, resp: dict[str, Any], *, model: str = "") -> list[str]:
    closed: list[str] = []

    async def _send(method: str, params: dict) -> dict:
        raise AssertionError("the check sends nothing")

    async def _close() -> None:
        closed.append("s-1")

    async def _run() -> None:
        state = await _prepare(harness, model=model)
        await harness.after_session_start(
            state, "s-1", resp, send_request=_send, close_session=_close
        )

    asyncio.run(_run())
    return closed


def test_an_inherited_hosted_model_closes_only_this_session(gates, tmp_path):
    harness = _spawned(gates, tmp_path)
    calls: list[str] = []

    async def _close() -> None:
        calls.append("closed")

    async def _run() -> None:
        state = await _prepare(harness)
        await harness.after_session_start(
            state, "s-1", _hosted("opencode/big"), send_request=None, close_session=_close
        )

    with pytest.raises(AcpNoModelProvider):
        asyncio.run(_run())
    assert calls == ["closed"]


def test_a_pinned_or_configured_model_is_never_refused(gates, tmp_path):
    harness = _spawned(gates, tmp_path)
    assert _start(harness, _hosted("opencode/big"), model="opencode/big") == []
    gates["config_model"] = "opencode/big"
    harness = _spawned(gates, tmp_path)
    assert _start(harness, _hosted("opencode/big")) == []
    # A model of the operator's own among the advertised ones is no fall-back either.
    resp = _hosted("opencode/big")
    resp["models"]["availableModels"].append({"modelId": "anthropic/claude"})
    gates["config_model"] = ""
    harness = _spawned(gates, tmp_path)
    assert _start(harness, resp) == []


# ── A shell permission ahead of its command, on the session handle ──


def _permission(sid: str = "s-1", call: str = "t1") -> JsonRpcMessage:
    return JsonRpcMessage(
        id=7,
        method="session/request_permission",
        params={"sessionId": sid, "toolCall": {"toolCallId": call}},
    )


def _command_update(
    sid: str = "s-1", call: str = "t1", command: str = "ls", **extra: Any
) -> JsonRpcMessage:
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call,
        "status": "in_progress",
        "rawInput": {"command": command},
        **extra,
    }
    return JsonRpcMessage(method="session/update", params={"sessionId": sid, "update": update})


def _shell_handle(*queued: JsonRpcMessage | None) -> Any:
    queue: asyncio.Queue = asyncio.Queue()
    for frame in queued:
        queue.put_nowait(frame)
    key = scoped_tool_cache_key("s-1", "t1")
    return SimpleNamespace(
        _session_id="s-1",
        _queue=queue,
        _tool_call_is_shell={key: True},
        _tool_call_raw_params={key: {}},
    )


def _defer(handle: Any, msg: JsonRpcMessage) -> tuple[bool, list[Any]]:
    front: deque = deque()
    deferred = asyncio.run(sh_permissions.defer_shell_permission(handle, msg, 30.0, front))
    return deferred, list(front)


def test_the_command_update_is_handled_before_its_permission():
    update = _command_update()
    msg = _permission()
    deferred, front = _defer(_shell_handle(update), msg)
    assert deferred is True
    assert front == [update, msg]


@pytest.mark.parametrize(
    "following",
    [
        _command_update(call="t2"),
        _command_update(sid="s-2"),
        _command_update(command=""),
        _command_update(rawOutput="already ran"),
        None,
    ],
)
def test_any_other_next_frame_goes_back_and_the_permission_is_judged_now(following):
    deferred, front = _defer(_shell_handle(following), _permission())
    assert deferred is False
    assert front == [following]


def test_a_call_with_its_command_cached_or_not_a_shell_reads_nothing():
    handle = _shell_handle(_command_update())
    key = scoped_tool_cache_key("s-1", "t1")
    handle._tool_call_raw_params[key] = {"command": "ls"}
    assert _defer(handle, _permission()) == (False, [])
    handle._tool_call_raw_params[key] = {}
    handle._tool_call_is_shell[key] = False
    assert _defer(handle, _permission()) == (False, [])
    assert handle._queue.qsize() == 1


def test_only_a_host_that_defers_waits():
    assert host_defers_shell_permission(SimpleNamespace(_harness=harness_for(ACP_BACKEND_OPENCODE)))
    assert not host_defers_shell_permission(
        SimpleNamespace(_harness=harness_for(ACP_BACKEND_GOOSE))
    )
    assert not host_defers_shell_permission(SimpleNamespace())


def test_the_turn_defers_before_it_builds_the_permission_event():
    import ast
    import inspect
    import textwrap

    from kiro_crew.acp.session_handle_dispatch import _dispatch_events

    source = textwrap.dedent(inspect.getsource(_dispatch_events))
    names = [
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    ]
    assert "defer_shell_permission" in names
    assert source.index("defer_shell_permission(") < source.index("_build_permission_event(msg)")


def test_the_handle_hands_the_parser_this_sessions_server_names():
    harness = harness_for(ACP_BACKEND_OPENCODE)
    state = opencode_mod._OpencodeSession(
        "dashboard:a", inherits_model=True, read_back=opencode_mod.HarnessReadBack()
    )
    state.placed = ("kirocrew-core",)
    runtime = SimpleNamespace(_harness=harness, _session_harness_states={"s-1": state})
    handle = SimpleNamespace(_runtime=runtime, _session_id="s-1")
    assert sh_permissions._harness_mcp_server_names(handle) == ("kirocrew-core",)
    goose = SimpleNamespace(_harness=harness_for(ACP_BACKEND_GOOSE), _session_harness_states={})
    assert (
        sh_permissions._harness_mcp_server_names(SimpleNamespace(_runtime=goose, _session_id="s-1"))
        == ()
    )
    assert (
        sh_permissions._harness_mcp_server_names(
            SimpleNamespace(_runtime=object(), _session_id="s-1")
        )
        == ()
    )


# ── The shared runtime's session start, with the control plane recorded ──


class _Plane:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        self.sent.append((method, params))
        if method in ("session/new", "session/load"):
            return self._response
        return {}


def _opencode_runtime(plane: _Plane, harness: Any) -> Any:
    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_OPENCODE, expect_mcp_reports=False)
    rt._harness_resolved = harness
    # What the spawn left for its sessions, as ``spawn`` records it.
    rt._spawn_host_state = harness.plan.host_state
    proc = MagicMock()
    proc.stdout = None
    proc.stdin = MagicMock()
    proc.returncode = None
    # Above every pid_max, so no cleanup path can signal a real process.
    proc.pid = UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = UNALLOCATABLE_PID
    rt._initialized = True
    rt._agent_version = "1.19.1"
    rt._send_and_await = plane.send_and_await  # type: ignore[method-assign]
    return rt


def test_the_runtime_hands_the_projection_the_rules_in_force(gates, tmp_path, monkeypatch):
    from kiro_crew.providers.mirrors import opencode as mirror_mod

    seen: list[Any] = []
    real = mirror_mod.opencode_projection

    def _projection(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("denies_in_force"))
        return real(*args, **kwargs)

    monkeypatch.setattr(mirror_mod, "opencode_projection", _projection)
    gates["disabled"] = (("github", "delete_repo"),)
    harness = _spawned(gates, tmp_path)
    plane = _Plane({"sessionId": "s-1"})
    rt = _opencode_runtime(plane, harness)
    handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert handle.session_id == "s-1"
    assert seen == [frozenset({"github_delete_repo"})]
    asyncio.run(rt.terminate_session("s-1"))
    assert plane.sent[-1] == (METHOD_SESSION_CLOSE, {"sessionId": "s-1"})
    assert rt._session_harness_states == {}


def test_a_hosted_fall_back_closes_the_session_and_keeps_the_process(gates, tmp_path):
    harness = _spawned(gates, tmp_path)
    plane = _Plane(_hosted("opencode/big"))
    rt = _opencode_runtime(plane, harness)
    with pytest.raises(AcpNoModelProvider):
        asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert (METHOD_SESSION_CLOSE, {"sessionId": "s-1"}) in plane.sent
    assert rt._process is not None and not rt._dead
    assert not rt._process.kill.called
    assert rt._session_harness_states == {}


def test_the_spawn_hands_its_plan_to_the_child_and_to_every_session():
    """The seed reaches the child after the harness's own env step, and the read-back
    reaches each session start; a plan without one asks ``prepare_session`` as before."""
    import inspect

    from kiro_crew.acp import runtime_session_create, runtime_session_load, runtime_spawn_launch
    from kiro_crew.acp.runtime_session_setup import host_state_kwargs

    spawn = inspect.getsource(runtime_spawn_launch)
    assert spawn.index("self._harness.apply_spawn_env(") < spawn.index("env.update(plan.spawn_env)")
    assert "self._spawn_host_state = plan.host_state" in spawn
    for module in (runtime_session_create, runtime_session_load):
        assert "**host_state_kwargs(self)" in inspect.getsource(module)
    assert host_state_kwargs(SimpleNamespace()) == {}
    assert host_state_kwargs(SimpleNamespace(_spawn_host_state=None)) == {}
    marker = object()
    assert host_state_kwargs(SimpleNamespace(_spawn_host_state=marker)) == {"host_state": marker}
