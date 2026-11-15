"""claude-agent-acp on the shared runtime (``ClaudeHarness``): each session's own setup.

On ``AcpClient`` a claude session owns its process, so its settings seed is written at
spawn and a failed mode pin ends the process. On one process several sessions share,
both have to be one session's. These pin the harness's answers: the seed is written per
session in that session's folder, a refused pin closes only that session, the seed goes
with the session, the teardown keeps the record, and the RSS ceiling counts the adapter
alone. No session is routed here yet; the harness is reached through ``harness_for``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.acp.claude_shared_session import ClaudeSharedSession
from kiro_crew.acp.harness import claude as claude_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.runtime_session_registry import terminate_session
from kiro_crew.acp.session_handle_runtime_contract import AcpRequestTimeout, AcpRuntimeError
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    METHOD_SESSION_CLOSE,
    METHOD_SET_MODE,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_ACP_RUNTIME
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.testing.ids import UNALLOCATABLE_PID

_ARGV = ["/opt/bin/claude-agent-acp"]


@pytest.fixture
def harness(monkeypatch):
    async def _argv() -> list[str]:
        return list(_ARGV)

    monkeypatch.setattr(claude_mod, "_resolve_claude_argv", _argv)
    monkeypatch.setattr(claude_mod, "_claude_adapter_installed_version", lambda _argv: "0.94.0")
    return harness_for(ACP_BACKEND_CLAUDE)


async def _prepare(harness: Any, work_dir: Path) -> ClaudeSharedSession:
    return await harness.prepare_session(
        work_dir=work_dir,
        agent="kirocrew",
        model="",
        session_key=f"dashboard:{work_dir.name}",
        channel_id="",
        memory_mode="persistent",
    )


def _seed(work_dir: Path) -> Path:
    return work_dir / ".claude" / "settings.local.json"


def test_the_harness_is_registered_and_routes_nothing():
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_ACP_RUNTIME
    harness = harness_for(ACP_BACKEND_CLAUDE)
    assert harness.per_session_state is True
    assert harness.teardown.method == METHOD_SESSION_CLOSE
    assert harness.teardown.notification is False
    assert harness.release(None) is None
    assert harness.verifies_agent_activation is False
    assert harness.host_answered_methods == ()


def test_the_protocol_version_is_the_one_the_client_sends():
    from kiro_crew.acp import client_start

    assert harness_for(ACP_BACKEND_CLAUDE).protocol_version == client_start.PROTOCOL_VERSION_CLAUDE


def test_the_rss_ceiling_counts_the_adapter_alone():
    """A whole-subtree ceiling would recycle every chat once a few were open."""
    harness = harness_for(ACP_BACKEND_CLAUDE)
    policy = harness.reclaim_policy(max_age_secs=60.0, max_rss_mb=500.0)
    assert policy.max_rss_mb == harness.CORE_RSS_CEILING_MB == 512.0
    assert harness.CORE_RSS_DEPTH == 0


def test_the_spawn_seeds_nothing_and_is_measured_adapter_only(harness, tmp_path, monkeypatch):
    from kiro_crew.acp.harness import SpawnContext
    from kiro_crew.acp.harness import codex as codex_mod

    monkeypatch.setattr(codex_mod, "_sandbox_wrapper_generations", lambda _mode: 1)
    ctx = SpawnContext(
        agent="kirocrew", work_dir=tmp_path, model=None, environ={}, home=tmp_path, session=None
    )
    plan = asyncio.run(harness.resolve_spawn(ctx))
    assert plan.argv == _ARGV
    # One resident sandbox launcher above the adapter, and nothing below it.
    assert plan.rss_depth == 1
    assert not _seed(tmp_path).exists()


def test_each_session_writes_its_own_seed_in_its_own_folder(harness, tmp_path):
    """A seed written once per process would leave a later session unseeded."""
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()

    async def _run() -> tuple[Any, Any]:
        return await _prepare(harness, first), await _prepare(harness, second)

    one, two = asyncio.run(_run())
    assert _seed(first).exists() and _seed(second).exists()
    assert harness.permission_surface_owned(one) is True
    assert harness.permission_surface_owned(two) is True
    assert harness.permission_surface_owned(None) is False
    asyncio.run(harness.end_session(one))
    asyncio.run(harness.end_session(two))


def test_ending_a_session_removes_its_seed_once(harness, tmp_path):
    state = asyncio.run(_prepare(harness, tmp_path))
    assert _seed(tmp_path).exists()
    asyncio.run(harness.end_session(state))
    assert not _seed(tmp_path).exists()
    # A refused start and the teardown it triggers can both reach here.
    asyncio.run(harness.end_session(state))
    assert not _seed(tmp_path).exists()


def test_the_envelope_is_the_sessions_own(harness, tmp_path):
    state = asyncio.run(_prepare(harness, tmp_path))
    meta = harness.session_meta(state, [], zero_tools=False, stub_token="tok-1")
    env = meta["claudeCode"]["options"]["env"]
    # This session's identity rides its own envelope, not the shared process env.
    assert env["KIROCREW_SESSION_KEY"] == f"dashboard:{tmp_path.name}"
    assert env[STUB_SESSION_TOKEN_ENV] == "tok-1"
    bare = state._claude_session_meta()
    assert {k: v for k, v in meta["claudeCode"].items() if k != "options"} == {
        k: v for k, v in bare["claudeCode"].items() if k != "options"
    }
    assert harness.session_meta(None, []) is None
    asyncio.run(harness.end_session(state))


def test_a_zero_tool_session_that_mounts_a_server_is_refused(harness, tmp_path):
    state = asyncio.run(_prepare(harness, tmp_path))
    with pytest.raises(AcpError):
        harness.session_meta(state, [{"name": "srv", "command": "x"}], zero_tools=True)
    asyncio.run(harness.end_session(state))


def _pinned_scenario(monkeypatch, state: ClaudeSharedSession) -> None:
    """A session the pin applies to: the operator asked for inherited modes to be
    pinned, and the session carries Crew's tools."""
    from kiro_crew.acp import client_claude_session

    monkeypatch.setattr(client_claude_session, "resolve_cc_pin_inherited_mode", lambda: True)
    state._mcp_mount_servers = ["kirocrew-core"]


def test_the_pin_talks_to_this_session_on_the_shared_connection(harness, tmp_path, monkeypatch):
    state = asyncio.run(_prepare(harness, tmp_path))
    _pinned_scenario(monkeypatch, state)
    sent: list[tuple[str, dict]] = []
    closed: list[str] = []

    async def _send(method: str, params: dict) -> dict:
        sent.append((method, params))
        return {}

    async def _close() -> None:
        closed.append("s-1")

    resp = {"sessionId": "s-1", "modes": {"currentModeId": "bypassPermissions"}}
    asyncio.run(
        harness.after_session_start(
            state, "s-1", resp, send_request=_send, close_session=_close, agent_version="0.94.0"
        )
    )
    assert sent == [(METHOD_SET_MODE, {"sessionId": "s-1", "modeId": "default"})]
    assert closed == []
    asyncio.run(harness.end_session(state))


#: What the shared connection raises for a ``session/set_mode`` it gets no good answer
#: to: an error answer, or none within the budget.
_SHARED_PIN_FAILURES = [
    pytest.param(AcpRuntimeError("set_mode refused"), id="error-answer"),
    pytest.param(AcpRequestTimeout("set_mode timed out"), id="timeout"),
]


@pytest.mark.parametrize("failure", _SHARED_PIN_FAILURES)
def test_a_failed_pin_closes_only_this_session(harness, tmp_path, monkeypatch, failure):
    """The process serves other sessions, so a refused one is closed, never killed."""
    state = asyncio.run(_prepare(harness, tmp_path))
    _pinned_scenario(monkeypatch, state)
    closed: list[str] = []

    async def _send(method: str, params: dict) -> dict:
        raise failure

    async def _close() -> None:
        closed.append("s-1")

    resp = {"sessionId": "s-1", "modes": {"currentModeId": "bypassPermissions"}}
    with pytest.raises(AcpError, match="pin the asking permission mode"):
        asyncio.run(
            harness.after_session_start(
                state,
                "s-1",
                resp,
                send_request=_send,
                close_session=_close,
                agent_version="0.94.0",
            )
        )
    assert closed == ["s-1"]
    asyncio.run(harness.end_session(state))


def test_the_shared_state_never_ends_the_process(tmp_path):
    closed: list[str] = []

    async def _close() -> None:
        closed.append("closed")

    async def _send(method: str, params: dict) -> dict:
        return {}

    state = ClaudeSharedSession(
        work_dir=tmp_path, model="", agent="kirocrew", session_key="", channel_id=""
    )
    state.bind_started("s-1", "0.94.0", send_request=_send, close_session=_close)
    asyncio.run(state._kill_process(force=True))
    assert closed == ["closed"]


class _RuntimeStub:
    """The members ``terminate_session`` reads, with a harness that records its calls."""

    def __init__(self, harness: Any, state: Any) -> None:
        self._harness = harness
        self._session_harness_states = {"s-1": state}
        self._record_keeping_sessions: set[str] = set()
        self._unclaimed_new_sessions: set[str] = set()
        self._unconfirmed_teardowns: set[str] = set()
        self._dead = False
        self._process = object()
        self.sent: list[tuple[str, dict]] = []
        self.unregistered: list[str] = []

    async def _send_and_await(self, method: str, params: dict, **_kw: Any) -> dict:
        self.sent.append((method, params))
        return {}

    def unregister_session(self, session_id: str) -> None:
        self.unregistered.append(session_id)


def test_the_session_teardown_closes_it_and_ends_its_state(harness, tmp_path):
    state = asyncio.run(_prepare(harness, tmp_path))
    runtime = _RuntimeStub(harness, state)
    asyncio.run(terminate_session(runtime, "s-1"))  # type: ignore[arg-type]
    assert runtime.sent == [(METHOD_SESSION_CLOSE, {"sessionId": "s-1"})]
    assert runtime.unregistered == ["s-1"]
    assert runtime._session_harness_states == {}
    assert not _seed(tmp_path).exists()


def test_a_host_without_session_state_skips_every_seam():
    for backend in ("", "kas", "codex"):
        assert harness_for(backend).per_session_state is False


# ── The shared runtime's session start, with the control plane recorded ──


class _Plane:
    """The requests one claude session start puts on the shared connection."""

    def __init__(
        self,
        new_response: dict[str, Any],
        *,
        refuse: str = "",
        refusal: BaseException | None = None,
    ) -> None:
        self._new_response = new_response
        self._refuse = refuse
        self._refusal = refusal
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        self.sent.append((method, params))
        if method == self._refuse:
            raise self._refusal or AcpRuntimeError(f"{method} refused")
        if method == "session/new":
            return self._new_response
        return {}

    def methods(self) -> list[str]:
        return [m for m, _ in self.sent]


def _claude_runtime(plane: _Plane) -> Any:
    from unittest.mock import MagicMock

    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_CLAUDE, expect_mcp_reports=False)
    proc = MagicMock()
    proc.stdout = None
    proc.stdin = MagicMock()
    proc.returncode = None
    # Above every pid_max, so no cleanup path can signal a real process.
    proc.pid = UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = UNALLOCATABLE_PID
    rt._initialized = True
    rt._agent_version = "0.94.0"
    rt._send_and_await = plane.send_and_await  # type: ignore[method-assign]
    return rt


def test_a_session_start_seeds_its_folder_and_sends_its_envelope(harness, tmp_path):
    plane = _Plane({"sessionId": "s-1", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)

    async def _run() -> Any:
        return await rt.create_session(cwd=str(tmp_path), agent="kirocrew")

    handle = asyncio.run(_run())
    assert handle.session_id == "s-1"
    new_params = [p for m, p in plane.sent if m == "session/new"]
    assert len(new_params) == 1
    assert "claudeCode" in new_params[0].get("_meta", {})
    assert _seed(tmp_path).exists()
    assert set(rt._session_harness_states) == {"s-1"}
    # Ending the session closes it on the host and takes its seed with it.
    asyncio.run(rt.terminate_session("s-1"))
    assert plane.methods()[-1] == METHOD_SESSION_CLOSE
    assert rt._session_harness_states == {}
    assert not _seed(tmp_path).exists()


def test_a_start_the_host_refuses_leaves_no_seed(harness, tmp_path):
    plane = _Plane({}, refuse="session/new")
    rt = _claude_runtime(plane)
    with pytest.raises(Exception):
        asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert not _seed(tmp_path).exists()
    assert rt._session_harness_states == {}


@pytest.mark.parametrize("failure", _SHARED_PIN_FAILURES)
def test_a_failed_pin_closes_that_session_and_keeps_the_process(
    harness, tmp_path, monkeypatch, failure
):
    from kiro_crew.acp import client_claude_session

    monkeypatch.setattr(client_claude_session, "resolve_cc_pin_inherited_mode", lambda: True)
    plane = _Plane(
        {"sessionId": "s-2", "modes": {"currentModeId": "bypassPermissions"}},
        refuse=METHOD_SET_MODE,
        refusal=failure,
    )
    rt = _claude_runtime(plane)
    # A session carrying one of Crew's servers, which is what the pin applies to.
    crew_server = {"name": "kirocrew-core", "command": "kirocrew", "args": [], "env": []}
    with pytest.raises(AcpError, match="pin the asking permission mode"):
        asyncio.run(
            rt.create_session(cwd=str(tmp_path), agent="kirocrew", mcp_servers=[crew_server])
        )
    assert ("session/close", {"sessionId": "s-2"}) in plane.sent
    assert rt._process is not None and not rt._dead
    proc_kill = getattr(rt._process, "kill")
    assert not proc_kill.called
    assert not _seed(tmp_path).exists()
    assert rt._session_harness_states == {}


def test_a_start_cancelled_during_its_pin_closes_that_session(harness, tmp_path, monkeypatch):
    """A cancel the pin does not catch still reaches the start's shielded close."""
    from kiro_crew.acp import client_claude_session

    monkeypatch.setattr(client_claude_session, "resolve_cc_pin_inherited_mode", lambda: True)
    plane = _Plane(
        {"sessionId": "s-3", "modes": {"currentModeId": "bypassPermissions"}},
        refuse=METHOD_SET_MODE,
        refusal=asyncio.CancelledError(),
    )
    rt = _claude_runtime(plane)
    crew_server = {"name": "kirocrew-core", "command": "kirocrew", "args": [], "env": []}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            rt.create_session(cwd=str(tmp_path), agent="kirocrew", mcp_servers=[crew_server])
        )
    assert ("session/close", {"sessionId": "s-3"}) in plane.sent
    assert rt._session_harness_states == {}


class _LoadPlane(_Plane):
    """A host that resumes the session it is asked to load."""

    def __init__(self) -> None:
        super().__init__({"sessionId": "unused"})

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        resp = await super().send_and_await(method, params, timeout)
        return {"modes": {"currentModeId": "default"}} if method == "session/load" else resp


def test_a_resumed_session_sends_its_own_envelope(harness, tmp_path):
    """session/load carries the per-session envelope, as session/new does."""
    plane = _LoadPlane()
    rt = _claude_runtime(plane)
    rt._can_load_session = True
    handle = asyncio.run(rt.load_session("", "s-5", cwd=str(tmp_path), agent="kirocrew"))
    assert handle.session_id == "s-5"
    loads = [p for m, p in plane.sent if m == "session/load"]
    assert len(loads) == 1
    assert "claudeCode" in loads[0].get("_meta", {})
    assert _seed(tmp_path).exists()
    asyncio.run(rt.terminate_session("s-5"))
    assert not _seed(tmp_path).exists()


def test_the_kiro_start_never_reaches_the_per_session_seams(monkeypatch):
    """harness-parity H13: kiro-cli reads the flag and goes on."""
    from kiro_crew.acp.runtime import AcpRuntime

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the kiro start reached the per-session seams")

    monkeypatch.setattr(AcpRuntime, "_create_session_with_harness_state", _boom)
    monkeypatch.setattr(AcpRuntime, "_load_session_with_harness_state", _boom)
    rt = AcpRuntime(work_dir="/tmp")
    assert rt._harness.per_session_state is False


def _claude_chat_provider(tmp_path: Path, session_key: str) -> Any:
    from types import SimpleNamespace

    client = SimpleNamespace(
        backend=ACP_BACKEND_CLAUDE,
        _session_key=session_key,
        _work_dir=str(tmp_path),
        _agent="kirocrew",
        _sandbox_mode="off",
        _extra_env={},
        _mcp_gateway_overlay=None,
        _mcp_gateway_socket=None,
        _model="",
        _permission_mode="acceptEdits",
        _side_read_only=True,
        _include_memory=False,
    )
    return SimpleNamespace(
        _client=client, is_kiro_backend=False, app_launch_agent="", _prejoined_chat_share=None
    )


def test_a_claude_chat_takes_the_runtime_arm_only_with_a_placement(tmp_path, monkeypatch):
    """The arm choice asks the placement first and keeps what it got for the start;
    a refused placement leaves the chat on its own per-chat process."""
    from kiro_crew.providers import acp_lifecycle, chat_share
    from kiro_crew.providers.acp_runtime_start import _harness_session_options

    placement = object()
    answers: list[Any] = [placement, None]

    async def _resolve(provider: Any, **kwargs: Any) -> Any:
        assert kwargs["remote_workspace"] is False
        return answers.pop(0)

    monkeypatch.setattr(chat_share, "resolve_chat_share", _resolve)
    monkeypatch.setattr(acp_lifecycle, "_bound_remotely", lambda key: False)
    shared = _claude_chat_provider(tmp_path, "dashboard:chat-1-1790000000")
    assert asyncio.run(acp_lifecycle._chat_takes_shared_runtime(shared)) is True
    assert shared._prejoined_chat_share is placement
    refused = _claude_chat_provider(tmp_path, "dashboard:chat-2-1790000000")
    assert asyncio.run(acp_lifecycle._chat_takes_shared_runtime(refused)) is False
    assert refused._prejoined_chat_share is None
    # The chat's own choices travel with its session start.
    assert _harness_session_options(shared) == {
        "permission_mode": "acceptEdits",
        "side_read_only": True,
        "include_memory": False,
    }


def test_a_remote_bound_claude_chat_never_asks_for_a_placement(tmp_path, monkeypatch):
    from kiro_crew.providers import acp_lifecycle, chat_share

    async def _resolve(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a remote-bound chat asked for a shared placement")

    monkeypatch.setattr(chat_share, "resolve_chat_share", _resolve)
    monkeypatch.setattr(acp_lifecycle, "_bound_remotely", lambda key: True)
    provider = _claude_chat_provider(tmp_path, "dashboard:chat-3-1790000000")
    assert asyncio.run(acp_lifecycle._chat_takes_shared_runtime(provider)) is False


class _SubstitutingPlane(_Plane):
    """A ``session/new`` the organization policy refuses once, naming a substitute."""

    def __init__(self, new_response: dict[str, Any]) -> None:
        super().__init__(new_response)
        self.refusals = 0

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        if method == "session/new" and not self.refusals:
            self.sent.append((method, params))
            self.refusals += 1
            from kiro_crew.acp.runtime_reader_loop import _runtime_rpc_exception

            raise _runtime_rpc_exception(
                {
                    "code": -32603,
                    "message": "Internal error",
                    "data": {
                        "details": 'Model "opus-x" is restricted by your organization\'s '
                        "settings. Using sonnet-y instead."
                    },
                }
            )
        return await super().send_and_await(method, params, timeout)


def test_a_substituted_model_is_adopted_and_the_start_retried_once(harness, tmp_path):
    plane = _SubstitutingPlane({"sessionId": "s-3", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert handle.session_id == "s-3"
    starts = [p for m, p in plane.sent if m == "session/new"]
    assert len(starts) == 2
    # The retry carries this session's envelope again, built on the substitute.
    assert "claudeCode" in starts[1]["_meta"]
    state = rt._session_harness_states["s-3"]
    assert state._model == "sonnet-y"
    asyncio.run(rt.terminate_session("s-3"))


def test_an_ordinary_session_new_error_is_not_retried(harness, tmp_path):
    plane = _Plane({"sessionId": "s-4"}, refuse="session/new")
    rt = _claude_runtime(plane)
    with pytest.raises(AcpRuntimeError):
        asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert [m for m, _ in plane.sent].count("session/new") == 1
    assert not _seed(tmp_path).exists()


def _load(rt: Any, tmp_path: Path, sid: str) -> Any:
    rt._can_load_session = True
    return rt.load_session(
        "",
        sid,
        cwd=str(tmp_path),
        agent="kirocrew",
        session_key=f"dashboard:{tmp_path.name}",
        channel_id="",
    )


def test_a_resumed_session_carries_its_own_session_key(harness, tmp_path):
    """session/load's envelope names this session, as session/new's does."""
    plane = _LoadPlane()
    rt = _claude_runtime(plane)
    handle = asyncio.run(_load(rt, tmp_path, "s-5"))
    assert handle.session_id == "s-5"
    loads = [p for m, p in plane.sent if m == "session/load"]
    assert len(loads) == 1
    env = loads[0]["_meta"]["claudeCode"]["options"]["env"]
    assert env["KIROCREW_SESSION_KEY"] == f"dashboard:{tmp_path.name}"
    asyncio.run(rt.terminate_session("s-5"))


def test_a_resume_cancelled_after_it_registered_closes_its_session(harness, tmp_path, monkeypatch):
    from kiro_crew.acp import runtime as runtime_facade

    def _cancelled(*_a: Any) -> Any:
        raise asyncio.CancelledError

    # Through the facade: it rebinds every owner that holds the name, and undoes it.
    monkeypatch.setattr(runtime_facade, "_load_watchdog_settings", _cancelled)
    plane = _LoadPlane()
    rt = _claude_runtime(plane)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_load(rt, tmp_path, "s-6"))
    assert ("session/close", {"sessionId": "s-6"}) in plane.sent
    assert rt._session_harness_states == {}
    assert not _seed(tmp_path).exists()


def test_the_specs_shell_and_write_allowlists_reach_the_shared_sessions_requests(
    harness, tmp_path, monkeypatch
):
    """Claude never reads ``toolsSettings``; the per-chat client stamps the allowlists
    on its requests, and on the shared runtime the session handle carries them."""
    from kiro_crew.acp import claude_shared_session as css_mod
    from kiro_crew.acp.spec_tools_allowlists import CLAUDE_SHELL_ASK_RULES

    spec = {
        "toolsSettings": {
            "shell": {"allowedCommands": ["git status"], "denyByDefault": True},
            "write": {"allowedPaths": ["notes/**"], "fallbackAction": "deny"},
        }
    }
    monkeypatch.setattr(css_mod, "session_allowlist_spec", lambda agent, work_dir: spec)
    plane = _Plane({"sessionId": "s-7", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert handle.spec_shell_allowlist == ("git status",)
    assert handle.spec_write_allowlist and handle.spec_write_cwd == str(tmp_path)
    # The envelope asks for those tools, so a project allow rule cannot pre-approve them.
    new = [p for m, p in plane.sent if m == "session/new"][0]
    asks = new["_meta"]["claudeCode"]["options"]["settings"]["permissions"]["ask"]
    assert set(CLAUDE_SHELL_ASK_RULES) <= set(asks)
    request = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 41,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s-7",
                "toolCall": {"toolCallId": "t-1", "title": "rm -rf notes", "kind": "execute"},
                "options": [
                    {"optionId": "allow", "kind": "allow_once", "name": "Allow"},
                    {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
                ],
            },
        }
    )
    event = handle._build_permission_event(request)
    assert event is not None and event.spec_shell_allowlist == ("git status",)
    assert event.spec_write_allowlist == handle.spec_write_allowlist
    # A native sub-agent's request, under the child's own sessionId, runs inside the
    # parent's spec and carries the same allowlists.
    child = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "session/request_permission",
            "params": {**request.params, "sessionId": "child-1"},
        }
    )
    child_event = handle._build_permission_event(child)
    assert child_event is not None and child_event.sub_session_id == "child-1"
    assert child_event.spec_shell_allowlist == ("git status",)
    assert child_event.spec_write_allowlist == handle.spec_write_allowlist
    asyncio.run(rt.terminate_session("s-7"))


def test_a_substitution_retry_is_refused_once_the_settings_file_was_replaced(
    harness, tmp_path, monkeypatch
):
    """The array was authorized under Crew's own settings file: a re-seed that finds
    it replaced sends no second start with that array (as the per-chat client)."""
    plane = _SubstitutingPlane({"sessionId": "s-8", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    adopted: list[bool] = []
    real_adopt = rt._harness.adopt_substituted_model

    async def adopt(state: Any, substitute: str) -> bool:
        adopted.append(await real_adopt(state, substitute))
        return adopted[-1]

    # The re-seed found the settings file replaced: Crew does not govern it.
    monkeypatch.setattr(rt._harness, "adopt_substituted_model", adopt)
    monkeypatch.setattr(rt._harness, "permission_surface_owned", lambda state: not adopted)
    from kiro_crew.acp.session_handle import AcpRuntimeError

    with pytest.raises(AcpRuntimeError, match="settings file was replaced"):
        asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert [m for m, _ in plane.sent].count("session/new") == 1
    assert rt._session_harness_states == {}


#: The longest a test waits for a signal it arranged; a wait past it fails the test.
_SIGNAL_DEADLINE_SECS = 30.0


async def _until(event: asyncio.Event, start: asyncio.Future[Any]) -> None:
    """Wait for *event*, failing at once if *start* ends first, or at the deadline."""
    signalled = asyncio.ensure_future(event.wait())
    try:
        done, _ = await asyncio.wait(
            {signalled, start},
            timeout=_SIGNAL_DEADLINE_SECS,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        signalled.cancel()
    if signalled not in done:
        if start in done:
            start.result()  # the start's own failure, reported as the test's
        pytest.fail("the signal the test waits for never came")


class _HeldLoadPlane(_LoadPlane):
    """A ``session/load`` the test releases, so the caller can leave mid-request."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.load_started = asyncio.Event()
        self.closed = asyncio.Event()

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        if method == "session/load":
            self.load_started.set()
            await asyncio.wait_for(self.release.wait(), _SIGNAL_DEADLINE_SECS)
        if method == "session/close":
            self.closed.set()
        return await super().send_and_await(method, params, timeout)


def test_a_load_abandoned_mid_request_is_closed_when_it_settles(harness, tmp_path):
    from kiro_crew.acp import runtime_session_load

    async def scenario() -> list[tuple[str, dict]]:
        plane = _HeldLoadPlane()
        rt = _claude_runtime(plane)
        start = asyncio.ensure_future(_load(rt, tmp_path, "s-9"))
        try:
            await _until(plane.load_started, start)
            start.cancel()
            done, _ = await asyncio.wait({start}, timeout=_SIGNAL_DEADLINE_SECS)
            assert start in done and start.cancelled()
            assert ("session/close", {"sessionId": "s-9"}) not in plane.sent
            # The adapter finishes the load nobody waits for any more.
            plane.release.set()
            await asyncio.wait_for(plane.closed.wait(), _SIGNAL_DEADLINE_SECS)
            await asyncio.wait_for(
                asyncio.gather(*runtime_session_load._ABANDONED_LOAD_CLOSES),
                _SIGNAL_DEADLINE_SECS,
            )
            assert rt._session_harness_states == {}
            return plane.sent
        finally:
            plane.release.set()
            start.cancel()

    sent = asyncio.run(scenario())
    assert ("session/close", {"sessionId": "s-9"}) in sent
    assert not _seed(tmp_path).exists()


class _HeldPinPlane(_Plane):
    """A starting-mode pin the test holds, so the start can be cancelled inside it."""

    def __init__(self, new_response: dict[str, Any]) -> None:
        super().__init__(new_response)
        self.pin_started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        if method == METHOD_SET_MODE:
            self.pin_started.set()
            await asyncio.wait_for(self.release.wait(), _SIGNAL_DEADLINE_SECS)
        return await super().send_and_await(method, params, timeout)


_CREW_SERVER = {"name": "kirocrew-core", "command": "kirocrew", "args": [], "env": []}


def test_a_start_cancelled_inside_the_mode_pin_closes_its_session(harness, tmp_path, monkeypatch):
    """The session exists on the host once session/new answered: a cancel during the
    pin closes it, so it is not left on a process other chats keep alive."""
    from kiro_crew.acp import client_claude_session

    monkeypatch.setattr(client_claude_session, "resolve_cc_pin_inherited_mode", lambda: True)

    async def scenario() -> tuple[Any, list]:
        plane = _HeldPinPlane(
            {"sessionId": "s-10", "modes": {"currentModeId": "bypassPermissions"}}
        )
        rt = _claude_runtime(plane)
        start = asyncio.ensure_future(
            rt.create_session(cwd=str(tmp_path), agent="kirocrew", mcp_servers=[_CREW_SERVER])
        )
        try:
            await _until(plane.pin_started, start)
            start.cancel()
            done, _ = await asyncio.wait({start}, timeout=_SIGNAL_DEADLINE_SECS)
            assert start in done and start.cancelled()
            return rt, plane.sent
        finally:
            plane.release.set()
            start.cancel()

    rt, sent = asyncio.run(scenario())
    assert ("session/close", {"sessionId": "s-10"}) in sent
    assert rt._session_harness_states == {}
    assert not _seed(tmp_path).exists()


def test_a_pin_the_shared_connection_refuses_stops_only_this_session(
    harness, tmp_path, monkeypatch
):
    """A JSON-RPC error on the shared connection reads as a failed pin, as on the
    per-chat client: the session is closed and the start refused."""
    from kiro_crew.acp import client_claude_session
    from kiro_crew.acp.runtime_reader_loop import _runtime_rpc_exception

    monkeypatch.setattr(client_claude_session, "resolve_cc_pin_inherited_mode", lambda: True)

    class _RpcRefusingPlane(_Plane):
        async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
            if method == METHOD_SET_MODE:
                self.sent.append((method, params))
                raise _runtime_rpc_exception({"code": -32602, "message": "Invalid params"})
            return await super().send_and_await(method, params, timeout)

    plane = _RpcRefusingPlane(
        {"sessionId": "s-11", "modes": {"currentModeId": "bypassPermissions"}}
    )
    rt = _claude_runtime(plane)
    with pytest.raises(AcpError):
        asyncio.run(
            rt.create_session(cwd=str(tmp_path), agent="kirocrew", mcp_servers=[_CREW_SERVER])
        )
    assert ("session/close", {"sessionId": "s-11"}) in plane.sent
    assert rt._session_harness_states == {}


def test_the_envelope_reads_this_sessions_array_for_connection_asks(harness, tmp_path):
    state = asyncio.run(_prepare(harness, tmp_path))
    servers = [{"name": "conn-x", "command": "x", "args": [], "env": []}]
    harness.session_meta(state, servers)
    assert state._session_mcp_cache == servers
    asyncio.run(harness.end_session(state))


def test_the_shared_provider_reads_its_own_sessions_withheld_tools_cause(harness, tmp_path):
    """The first-turn notice that Crew's tools are withheld reads the session's own
    harness state, as the per-chat client answers it; another chat reads ``""``."""
    from kiro_crew.acp.client_seed_ownership import SETTINGS_WITHHELD_BY_PROJECT
    from kiro_crew.acp.session_provider import AcpSessionProvider

    plane = _Plane({"sessionId": "s-1", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    chat_a = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    plane._new_response = {"sessionId": "s-2", "modes": {"currentModeId": "default"}}
    chat_b = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    rt._session_harness_states["s-1"].settings_withheld_cause = lambda: SETTINGS_WITHHELD_BY_PROJECT
    assert AcpSessionProvider(chat_a, rt).settings_withheld_cause() == SETTINGS_WITHHELD_BY_PROJECT
    assert AcpSessionProvider(chat_b, rt).settings_withheld_cause() == ""
    for sid in ("s-1", "s-2"):
        asyncio.run(rt.terminate_session(sid))
