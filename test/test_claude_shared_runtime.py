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
from kiro_crew.security.redaction_structured.checks import base62_crc32
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


async def _prepared_for_envelope(harness: Any, work_dir: Path) -> ClaudeSharedSession:
    """A prepared state with its spec adopted, as the runtime holds it at the envelope."""
    state = await _prepare(harness, work_dir)
    await harness.adopt_session_spec(state, None)
    return state


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
    state = asyncio.run(_prepared_for_envelope(harness, tmp_path))
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
    state = asyncio.run(_prepared_for_envelope(harness, tmp_path))
    with pytest.raises(AcpError):
        harness.session_meta(state, [{"name": "srv", "command": "x"}], zero_tools=True)
    asyncio.run(harness.end_session(state))


def test_no_envelope_is_built_before_the_spec_is_adopted(harness, tmp_path):
    """Fail closed: an envelope without the spec's allowlists would let the
    session's shell and write tools run unasked."""
    state = asyncio.run(_prepare(harness, tmp_path))
    with pytest.raises(AcpRuntimeError, match="allowlists were not read"):
        harness.session_meta(state, [])
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

    def __init__(self, new_response: dict[str, Any], max_refusals: int = 1) -> None:
        super().__init__(new_response)
        self.refusals = 0
        self.max_refusals = max_refusals

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        if method == "session/new" and self.refusals < self.max_refusals:
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
    on its requests, and on the shared runtime the session handle carries them. A
    projection that carries no spec (a withheld array) has them read once, as the
    per-chat client's MCP-array step falls back."""
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
    projected = rt._mirrored_session_mcp

    async def project(*args: Any, **kwargs: Any) -> Any:
        mirrored = await projected(*args, **kwargs)
        assert mirrored is not None
        return mirrored._replace(agent_spec=None)

    rt._mirrored_session_mcp = project  # type: ignore[method-assign]
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


@pytest.mark.parametrize("path", ["new", "load"])
def test_a_spec_tightened_after_prepare_is_the_one_the_session_enforces(
    harness, tmp_path, monkeypatch, path
):
    """The allowlists and the envelope's ask rules come from the spec the array was
    projected from: an operator who tightens the spec between the session's setup
    and its projection gets the tightened one held, so the old command is refused."""
    from kiro_crew.acp import claude_shared_session as css_mod
    from kiro_crew.acp.spec_tools_allowlists import CLAUDE_SHELL_ASK_RULES

    tightened = {
        "toolsSettings": {"shell": {"allowedCommands": ["git status"], "denyByDefault": True}}
    }
    reads: list[str] = []

    def read_spec(agent: str, work_dir: Any) -> Any:
        reads.append(agent)
        return None  # the spec as it stood before the operator tightened it

    monkeypatch.setattr(css_mod, "session_allowlist_spec", read_spec)
    plane: _Plane = (
        _LoadPlane()
        if path == "load"
        else _Plane({"sessionId": "s-19", "modes": {"currentModeId": "default"}})
    )
    rt = _claude_runtime(plane)
    projected = rt._mirrored_session_mcp

    async def project(*args: Any, **kwargs: Any) -> Any:
        mirrored = await projected(*args, **kwargs)
        assert mirrored is not None
        return mirrored._replace(agent_spec=tightened)

    rt._mirrored_session_mcp = project  # type: ignore[method-assign]
    if path == "load":
        handle = asyncio.run(_load(rt, tmp_path, "s-19"))
    else:
        handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert reads == []
    assert handle.spec_shell_allowlist == ("git status",)
    # The gate's shell tier refuses a command outside what the event carries.
    request = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 43,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s-19",
                "toolCall": {"toolCallId": "t-2", "title": "rm -rf notes", "kind": "execute"},
                "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"}],
            },
        }
    )
    event = handle._build_permission_event(request)
    assert event is not None and event.spec_shell_allowlist == ("git status",)
    sent = [p for m, p in plane.sent if m == f"session/{path}"][0]
    asks = sent["_meta"]["claudeCode"]["options"]["settings"]["permissions"]["ask"]
    assert set(CLAUDE_SHELL_ASK_RULES) <= set(asks)
    asyncio.run(rt.terminate_session("s-19"))


def test_a_served_pin_is_named_on_the_retry_and_not_adopted_twice(harness, tmp_path, monkeypatch):
    """The refused model is the inherited default, not the session's pin: a pin the
    backend advertises is what the retry's seed names, and a second refusal of it
    is not retried."""
    import json

    from kiro_crew import model_registry

    pin = "global.anthropic.claude-opus-4-8[1m]"
    other = "global.anthropic.claude-sonnet-4-6[1m]"

    async def scenario() -> tuple[bool, bool]:
        state = await _prepared_for_envelope(harness, tmp_path)
        monkeypatch.setattr(
            model_registry,
            "_ADVERTISED_MODELS",
            {state._model_registry_namespace: (pin, other)},
        )
        state._model = pin
        first = await harness.adopt_substituted_model(state, other)
        assert state._model == pin
        assert json.loads(_seed(tmp_path).read_text()).get("model") == pin
        second = await harness.adopt_substituted_model(state, other)
        # The served pin was refused too: the second adoption falls back to the
        # offered model, and a third has nothing new to name.
        assert state._model == other
        assert json.loads(_seed(tmp_path).read_text()).get("model") == other
        return first, second

    assert asyncio.run(scenario()) == (True, True)


@pytest.mark.parametrize(
    ("refusals", "starts", "started"),
    [(2, 3, True), (99, 3, False)],
    ids=["the_offered_model_starts", "never_more_than_two_resends"],
)
def test_a_refused_pin_falls_back_once_to_the_offered_model(
    harness, tmp_path, monkeypatch, refusals, starts, started
):
    """A session on the offered model beats one that never starts: after the served
    pin is refused as well, the start is re-sent once on the offered model, and
    never a third time."""
    from kiro_crew.acp.client_claude import ClaudeAcpClient
    from kiro_crew.acp.session_handle import AcpRuntimeError

    monkeypatch.setattr(
        ClaudeAcpClient, "_substitution_retry_model", lambda self, substitute: "opus-pin"
    )
    plane = _SubstitutingPlane(
        {"sessionId": "s-20", "modes": {"currentModeId": "default"}}, max_refusals=refusals
    )
    rt = _claude_runtime(plane)
    if started:
        asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
        assert rt._session_harness_states["s-20"]._model == "sonnet-y"
        asyncio.run(rt.terminate_session("s-20"))
    else:
        with pytest.raises(AcpRuntimeError):
            asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert [m for m, _ in plane.sent].count("session/new") == starts


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


def test_an_abandoned_loads_late_close_leaves_a_retry_that_took_the_id(
    harness, tmp_path, monkeypatch
):
    """A retry that loaded the same session while the abandoned load was still in
    flight owns that id: the abandoned load registered nothing, so its late close
    must not end the retry's session."""
    from kiro_crew.acp import runtime_session_load

    settled = asyncio.Event()
    real = runtime_session_load._close_abandoned_load

    def close_then_signal(*args: Any) -> Any:
        close = real(*args)
        settled.set()
        return close

    monkeypatch.setattr(runtime_session_load, "_close_abandoned_load", close_then_signal)

    async def scenario() -> tuple[list[tuple[str, dict]], Any, Any]:
        plane = _HeldLoadPlane()
        rt = _claude_runtime(plane)
        start = asyncio.ensure_future(_load(rt, tmp_path, "s-9"))
        try:
            await _until(plane.load_started, start)
            start.cancel()
            await asyncio.wait({start}, timeout=_SIGNAL_DEADLINE_SECS)
            # The retry's session took the id on this runtime meanwhile.
            retry_queue: asyncio.Queue[Any] = asyncio.Queue()
            rt._session_queues["s-9"] = retry_queue
            plane.release.set()
            await asyncio.wait_for(settled.wait(), _SIGNAL_DEADLINE_SECS)
            await asyncio.wait_for(
                asyncio.gather(*runtime_session_load._ABANDONED_LOAD_CLOSES),
                _SIGNAL_DEADLINE_SECS,
            )
            return plane.sent, rt._session_queues.get("s-9"), retry_queue
        finally:
            plane.release.set()
            start.cancel()

    sent, queue, retry_queue = asyncio.run(scenario())
    assert ("session/close", {"sessionId": "s-9"}) not in sent
    assert queue is retry_queue


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
    state = asyncio.run(_prepared_for_envelope(harness, tmp_path))
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


def test_a_shared_claude_request_reads_its_sessions_placed_servers(harness, tmp_path):
    """A tool of a server Crew placed on this session's envelope keeps its trusted
    ``(server, tool)`` identity, as on the per-chat client."""
    from kiro_crew.acp._dispatch_parts.permission_requests import CLAUDE_CREW_PLACED_MCP_SOURCE

    plane = _Plane({"sessionId": "s-9", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    state = rt._session_harness_states["s-9"]
    state._session_mcp_cache = [{"name": "kirocrew-core", "command": "x", "args": [], "env": []}]
    tool = "mcp__kirocrew-core__list_sessions"
    from kiro_crew.acp._dispatch_parts.tool_call_provenance import scoped_tool_cache_key

    # What the preceding tool_call frame left: empty names, the fused harness name.
    key = scoped_tool_cache_key("s-9", "t-9")
    handle._tool_call_mcp_server[key] = ""
    handle._tool_call_tool_name[key] = ""
    handle._tool_call_harness_tool_name[key] = tool
    request = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 51,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s-9",
                "toolCall": {
                    "toolCallId": "t-9",
                    "title": "list_sessions",
                    "_meta": {
                        "claudeCode": {
                            "toolName": tool,
                            "mcpServer": {
                                "name": "kirocrew-core",
                                "source": CLAUDE_CREW_PLACED_MCP_SOURCE,
                            },
                        }
                    },
                },
                "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"}],
            },
        }
    )
    event = handle._build_permission_event(request)
    assert event is not None
    assert (event.mcp_server_name, event.tool_name) == ("kirocrew-core", "list_sessions")
    asyncio.run(rt.terminate_session("s-9"))


def test_a_shared_claude_request_marks_child_calls_so_the_parent_keeps_its_override(
    harness, tmp_path
):
    """As on the per-chat client, the shared handle tells a claude child's call apart
    (a foreign sessionId, or a ``parentToolUseId`` stamp on the parent's session), so
    the transport is trusted for deny overrides: the parent's own call may be offered
    one, a child's never."""
    plane = _Plane({"sessionId": "s-10", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))

    def request(rid: int, sid: str, stamp: str = "") -> JsonRpcMessage:
        tool_call: dict[str, Any] = {"toolCallId": f"t-{rid}", "title": "chmod 777 ./build"}
        if stamp:
            tool_call["_meta"] = {"claudeCode": {"parentToolUseId": stamp}}
        return JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "session/request_permission",
                "params": {
                    "sessionId": sid,
                    "toolCall": tool_call,
                    "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"}],
                },
            }
        )

    own = handle._build_permission_event(request(61, "s-10"))
    stamped = handle._build_permission_event(request(62, "s-10", stamp="toolu_agent"))
    foreign = handle._build_permission_event(request(63, "child-1"))
    assert own is not None and stamped is not None and foreign is not None
    assert own.child_calls_marked and not own.native_child_call
    assert stamped.child_calls_marked and stamped.native_child_call
    assert foreign.child_calls_marked and foreign.native_child_call
    asyncio.run(rt.terminate_session("s-10"))


def test_a_claude_chat_with_an_armed_native_fork_keeps_its_own_process(tmp_path, monkeypatch):
    """Edit & Resend arms a native fork that only the per-chat client forks and
    loads, so that start never asks for a shared placement."""
    from kiro_crew.providers import acp_lifecycle, chat_share

    async def _resolve(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a start with an armed fork asked for a shared placement")

    monkeypatch.setattr(chat_share, "resolve_chat_share", _resolve)
    monkeypatch.setattr(acp_lifecycle, "_bound_remotely", lambda key: False)
    provider = _claude_chat_provider(tmp_path, "dashboard:chat-4-1790000000")
    provider._client._fork_request = ("s-old", "msg-1")
    assert asyncio.run(acp_lifecycle._chat_takes_shared_runtime(provider)) is False


def test_the_shared_provider_lists_its_own_sessions_commands(harness, tmp_path):
    """The slash menu reads each chat's own advertised commands on the shared
    runtime, as on the per-chat client; another chat lists none of them."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    plane = _Plane({"sessionId": "s-1", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    chat_a = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    plane._new_response = {"sessionId": "s-2", "modes": {"currentModeId": "default"}}
    chat_b = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    update = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s-1",
                "update": {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": [{"name": "review", "description": "Review a PR"}],
                },
            },
        }
    )
    assert rt._harness.take_session_frame(rt._session_harness_states["s-1"], update) is False
    assert AcpSessionProvider(chat_a, rt).advertised_commands() == [
        {"name": "review", "description": "Review a PR"}
    ]
    assert AcpSessionProvider(chat_b, rt).advertised_commands() == []
    for sid in ("s-1", "s-2"):
        asyncio.run(rt.terminate_session(sid))


def test_a_shared_claude_steer_is_sent_as_the_per_chat_client_sends_it(harness, tmp_path):
    """A mid-turn steer on a shared claude chat goes out as claude's own
    ``_session/steering`` request, asking ``promptRequired`` for a turn that has
    settled; it is not sent while a tool call is open or below the adapter
    release that takes it, so the caller queues it as the next turn."""
    from kiro_crew.acp.liveness_tool_state import ToolCallState

    plane = _Plane({"sessionId": "s-11", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    rt._initialize_result = {"_meta": {"steering": {"supported": True}}}
    state = rt._session_harness_states["s-11"]
    sent: list[tuple[str, dict]] = []

    async def capture(method: str, params: dict, **_: Any) -> Any:
        sent.append((method, params))
        raise OSError("written nowhere")

    rt.send_request_for_answer = capture  # type: ignore[method-assign]
    handle._turn_done.clear()
    handle._prompt_written = True

    state._agent_version = "0.64.0"
    assert asyncio.run(handle.steer("look at the tests")) is False and sent == []
    state._agent_version = "0.94.0"
    handle._tool_tracker.dispatch("t-1", ToolCallState(title="pytest"))
    assert asyncio.run(handle.steer("look at the tests")) is False and sent == []
    handle._tool_tracker.clear()
    asyncio.run(handle.steer("look at the tests"))
    assert [m for m, _ in sent] == ["_session/steering"]
    assert sent[0][1]["_meta"] == {"steering": {"idleBehavior": "promptRequired"}}
    handle._turn_done.set()
    asyncio.run(rt.terminate_session("s-11"))


def test_a_command_list_read_before_the_session_registered_is_kept(harness, tmp_path):
    """The adapter's command list can arrive in the same burst as ``session/new``'s
    answer, before the session's state is registered: it is kept by session id and
    applied when the session starts, so the slash menu still lists it."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    plane = _Plane({"sessionId": "s-12", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    early = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s-12",
                "update": {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": [{"name": "deploy", "description": "Ship it"}],
                },
            },
        }
    )
    assert rt._harness.take_session_frame(None, early) is False
    handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert AcpSessionProvider(handle, rt).advertised_commands() == [
        {"name": "deploy", "description": "Ship it"}
    ]
    asyncio.run(rt.terminate_session("s-12"))


class _HeldClosePlane(_HeldLoadPlane):
    """A held load, then a ``session/close`` the test also holds."""

    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        if method == "session/close":
            self.close_started.set()
            await asyncio.wait_for(self.close_release.wait(), _SIGNAL_DEADLINE_SECS)
            answer = await super().send_and_await(method, params, timeout)
            self.sent.append(("close finished", {}))
            return answer
        return await super().send_and_await(method, params, timeout)


def test_a_retry_waits_for_an_abandoned_loads_close_of_the_same_id(harness, tmp_path):
    """The abandoned load's close of an id and a retry's load of that id are
    serialized: the retry's load goes out only after the close finished, so the
    close cannot end the retry's session."""

    async def scenario() -> tuple[list[str], Any]:
        plane = _HeldClosePlane()
        rt = _claude_runtime(plane)
        first = asyncio.ensure_future(_load(rt, tmp_path, "s-13"))
        await _until(plane.load_started, first)
        first.cancel()
        await asyncio.wait({first}, timeout=_SIGNAL_DEADLINE_SECS)
        plane.release.set()
        await asyncio.wait_for(plane.close_started.wait(), _SIGNAL_DEADLINE_SECS)
        # Stand a watched future in for the close the retry must wait on, so the
        # test sees the retry park on it before letting the close finish.
        from kiro_crew.acp import runtime_session_load

        awaited = asyncio.Event()
        key = (id(rt), "s-13")
        close = runtime_session_load._PENDING_ABANDONED_CLOSES[key]

        class _Watched(asyncio.Future):  # type: ignore[type-arg]
            def add_done_callback(self, fn: Any, **kw: Any) -> None:
                awaited.set()
                super().add_done_callback(fn, **kw)

        watched = _Watched()
        close.add_done_callback(lambda _t: watched.done() or watched.set_result(None))
        runtime_session_load._PENDING_ABANDONED_CLOSES[key] = watched
        retry = asyncio.ensure_future(_load(rt, tmp_path, "s-13"))
        await _until(awaited, retry)
        loads_before_close = [m for m, _ in plane.sent].count("session/load")
        plane.close_release.set()
        handle = await asyncio.wait_for(retry, _SIGNAL_DEADLINE_SECS)
        assert "s-13" in rt._session_queues and "s-13" in rt._session_harness_states
        order = [m for m, _ in plane.sent if m in ("session/load", "close finished")]
        await rt.terminate_session(handle.session_id)
        return order + [str(loads_before_close)], handle

    order, handle = asyncio.run(scenario())
    assert order == ["session/load", "close finished", "session/load", "1"]
    assert handle.session_id == "s-13"


def test_a_retry_before_the_abandoned_load_settles_waits_for_its_close(harness, tmp_path):
    """The id is held from the moment the load is abandoned: a retry issued while
    the abandoned load is still in flight waits for it to settle and for its close,
    so that close cannot end the retry's session."""

    async def scenario() -> tuple[list[str], Any]:
        from kiro_crew.acp import runtime_session_load

        plane = _HeldClosePlane()
        rt = _claude_runtime(plane)
        first = asyncio.ensure_future(_load(rt, tmp_path, "s-14"))
        await _until(plane.load_started, first)
        first.cancel()
        await asyncio.wait({first}, timeout=_SIGNAL_DEADLINE_SECS)
        # The barrier exists from the cancellation on; a watched stand-in shows
        # the retry parking on it.
        key = (id(rt), "s-14")
        barrier = runtime_session_load._PENDING_ABANDONED_CLOSES[key]
        awaited = asyncio.Event()

        class _Watched(asyncio.Future):  # type: ignore[type-arg]
            def add_done_callback(self, fn: Any, **kw: Any) -> None:
                awaited.set()
                super().add_done_callback(fn, **kw)

        watched = _Watched()
        barrier.add_done_callback(lambda _b: watched.done() or watched.set_result(None))
        runtime_session_load._PENDING_ABANDONED_CLOSES[key] = watched
        # The retry starts before the abandoned load has settled.
        retry = asyncio.ensure_future(_load(rt, tmp_path, "s-14"))
        await _until(awaited, retry)
        plane.release.set()
        await asyncio.wait_for(plane.close_started.wait(), _SIGNAL_DEADLINE_SECS)
        plane.close_release.set()
        handle = await asyncio.wait_for(retry, _SIGNAL_DEADLINE_SECS)
        assert "s-14" in rt._session_queues and "s-14" in rt._session_harness_states
        order = [m for m, _ in plane.sent if m in ("session/load", "close finished")]
        await rt.terminate_session(handle.session_id)
        return order, handle

    order, handle = asyncio.run(scenario())
    assert order == ["session/load", "close finished", "session/load"]
    assert handle.session_id == "s-14"


def _command_list(sid: str, commands: list[dict[str, Any]]) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": commands,
                },
            },
        }
    )


def _kept_commands(rt: Any, sid: str, registered: bool) -> list[dict[str, str]]:
    """The command list *rt* kept for *sid*: the early copy, or the session's own."""
    if not registered:
        return rt._harness._early_command_lists()[sid].params["update"]["availableCommands"]
    return list(rt._session_harness_states[sid]._advertised_commands[1])


@pytest.mark.parametrize("registered", [False, True], ids=["early", "registered"])
def test_a_command_list_is_kept_bounded(harness, tmp_path, caplog, registered):
    """A command list is kept to only what the menu reads, with every field
    bounded, before the session registers and after: the number of commands, each
    name and description, and the session id. Commands past the bound are counted
    and said once."""
    from kiro_crew.acp import client_transport as ct

    rt = _claude_runtime(_Plane({"sessionId": "s-15", "modes": {"currentModeId": "default"}}))
    if registered:
        asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    state = rt._session_harness_states.get("s-15") if registered else None
    commands = [
        {"name": f"c{i}", "description": "d" * 5000, "input": {"x": 1}}
        for i in range(ct.ADVERTISED_COMMANDS_MAX + 5)
    ]
    with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
        rt._harness.take_session_frame(state, _command_list("s-15", commands))
    assert caplog.text.count("advertised 5 command(s) past the") == 1
    kept = _kept_commands(rt, "s-15", registered)
    assert len(kept) == ct.ADVERTISED_COMMANDS_MAX
    assert all(set(c) == {"name", "description"} for c in kept)
    assert len(kept[0]["description"]) == ct.ADVERTISED_COMMAND_DESCRIPTION_MAX_CHARS
    long_id = "x" * (ct.ADVERTISED_COMMANDS_SESSION_ID_MAX_CHARS + 1)
    rt._harness.take_session_frame(state, _command_list(long_id, [{"name": "c"}]))
    if registered:
        assert state._advertised_commands[0] == "s-15"
        asyncio.run(rt.terminate_session("s-15"))
    else:
        assert list(rt._harness._early_command_lists()) == ["s-15"]


@pytest.mark.parametrize("registered", [False, True], ids=["early", "registered"])
def test_a_command_with_an_overlong_name_is_dropped_not_cut(harness, tmp_path, caplog, registered):
    """A name over its bound is dropped whole and said once, before the session
    registers and after: a cut one would send a name the backend never advertised,
    and two sharing the kept prefix would merge."""
    from kiro_crew.acp import client_transport as ct

    rt = _claude_runtime(_Plane({"sessionId": "s-16", "modes": {"currentModeId": "default"}}))
    if registered:
        asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    state = rt._session_harness_states.get("s-16") if registered else None
    prefix = "n" * ct.ADVERTISED_COMMAND_NAME_MAX_CHARS
    names = [prefix + "a", prefix + "b", prefix, "short"]
    msg = _command_list("s-16", [{"name": n, "description": ""} for n in names])
    with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
        rt._harness.take_session_frame(state, msg)
    assert [c["name"] for c in _kept_commands(rt, "s-16", registered)] == [prefix, "short"]
    assert caplog.text.count("advertised 2 command(s) with a name over") == 1
    if registered:
        asyncio.run(rt.terminate_session("s-16"))


@pytest.mark.parametrize("registered", [False, True], ids=["early", "registered"])
def test_a_credential_across_the_description_bound_is_scrubbed_whole(harness, tmp_path, registered):
    """A description is scrubbed whole before it is cut: a credential the cut would
    split reaches the slash menu as neither half."""
    from kiro_crew.acp import client_transport as ct

    rt = _claude_runtime(_Plane({"sessionId": "s-20", "modes": {"currentModeId": "default"}}))
    if registered:
        asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    state = rt._session_harness_states.get("s-20") if registered else None
    # The rule table reads a GitHub token by its CRC32 suffix, so the fixture carries one.
    body = "a" * 30
    credential = "ghp_" + body + base62_crc32(body)
    start = ct.ADVERTISED_COMMAND_DESCRIPTION_MAX_CHARS - 20
    description = "d" * start + credential + " tail"
    msg = _command_list("s-20", [{"name": "review", "description": description}])
    rt._harness.take_session_frame(state, msg)
    if not registered:
        # Applied when the session starts, as the menu reads it then.
        asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    kept = _kept_commands(rt, "s-20", True)
    assert "ghp_" not in kept[0]["description"]
    assert len(kept[0]["description"]) <= ct.ADVERTISED_COMMAND_DESCRIPTION_MAX_CHARS
    asyncio.run(rt.terminate_session("s-20"))


@pytest.mark.parametrize("path", ["new", "load"])
def test_an_inheriting_session_is_sent_the_model_its_adapter_reports(harness, tmp_path, path):
    """claude-agent-acp reports the settings model without passing it on, so a
    resumed session can run Claude Code's built-in default: the reported id goes
    back over ``session/set_config_option``, as the per-chat client sends it, and
    the session still inherits rather than pins it."""
    models = {
        "availableModels": [
            {"modelId": "default", "name": "Default"},
            {"modelId": "opus", "name": "Opus"},
        ],
        "currentModelId": "opus",
    }
    options = [
        {
            "id": "model",
            "name": "Model",
            "type": "select",
            "currentValue": "opus",
            "options": [{"value": "default", "name": "Default"}, {"value": "opus", "name": "Opus"}],
        }
    ]
    answer = {
        "sessionId": "s-21",
        "modes": {"currentModeId": "default"},
        "models": models,
        "configOptions": options,
    }

    class _ModelPlane(_Plane):
        async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
            resp = await super().send_and_await(method, params, timeout)
            if method == "session/load":
                return {k: v for k, v in answer.items() if k != "sessionId"}
            return resp

    plane = _ModelPlane(answer)
    rt = _claude_runtime(plane)
    ids = iter(range(900, 1000))

    async def send_request(method: str, params: dict, *, on_reserved: Any = None, **_: Any) -> int:
        # A request whose answer the reader puts on the session's own queue.
        req_id = next(ids)
        if on_reserved is not None:
            on_reserved(req_id)
        plane.sent.append((method, params))
        rt._session_queues[params["sessionId"]].put_nowait(
            JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": req_id, "result": {}})
        )
        return req_id

    rt.send_request = send_request  # type: ignore[method-assign]
    if path == "load":
        handle = asyncio.run(_load(rt, tmp_path, "s-21"))
    else:
        handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    writes = [p for m, p in plane.sent if m == "session/set_config_option"]
    assert {"sessionId": "s-21", "configId": "model", "value": "opus"} in writes
    assert handle._model == "" and handle.inherited_model == "opus"
    asyncio.run(rt.terminate_session("s-21"))


class _HeldNewPlane(_Plane):
    """A ``session/new`` the test releases, so the caller can leave mid-request."""

    def __init__(self, new_response: dict[str, Any]) -> None:
        super().__init__(new_response)
        self.new_started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        if method == "session/new":
            self.new_started.set()
            await asyncio.wait_for(self.release.wait(), _SIGNAL_DEADLINE_SECS)
        if method == "session/close":
            self.closed.set()
        return await super().send_and_await(method, params, timeout)


def test_a_start_abandoned_mid_request_closes_the_session_it_made(harness, tmp_path):
    """A ``session/new`` its caller left is kept to its end on the shared process,
    and the session it answers with is closed: no session stays resident unowned."""

    async def scenario() -> list[tuple[str, dict]]:
        plane = _HeldNewPlane({"sessionId": "s-16", "modes": {"currentModeId": "default"}})
        rt = _claude_runtime(plane)
        start = asyncio.ensure_future(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
        await _until(plane.new_started, start)
        start.cancel()
        await asyncio.wait({start}, timeout=_SIGNAL_DEADLINE_SECS)
        plane.release.set()
        await asyncio.wait_for(plane.closed.wait(), _SIGNAL_DEADLINE_SECS)
        assert "s-16" not in rt._session_queues
        return plane.sent

    assert ("session/close", {"sessionId": "s-16"}) in asyncio.run(scenario())


class _LateNewPlane(_HeldNewPlane):
    """A held ``session/new`` that times out, its answer arriving after on the
    future ``_send_and_await`` hands on (``adopted_future``)."""

    def __init__(self, new_response: dict[str, Any]) -> None:
        super().__init__(new_response)
        self.late: asyncio.Future[dict[str, Any]] | None = None
        self.timed_out = asyncio.Event()

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        if method != "session/new":
            return await super().send_and_await(method, params, timeout)
        self.sent.append((method, params))
        self.new_started.set()
        await asyncio.wait_for(self.release.wait(), _SIGNAL_DEADLINE_SECS)
        self.late = asyncio.get_running_loop().create_future()
        self.timed_out.set()
        exc = AcpRequestTimeout("Request session/new timed out after 1s")
        setattr(exc, "req_id", 7)
        setattr(exc, "adopted_future", self.late)
        raise exc


def test_a_start_abandoned_then_timed_out_closes_its_late_session(harness, tmp_path):
    """A ``session/new`` its caller left that then times out still has its late
    answer closed when it lands: no collector owns it, so nothing else would."""

    async def scenario() -> list[tuple[str, dict]]:
        plane = _LateNewPlane({"sessionId": "s-18", "modes": {"currentModeId": "default"}})
        rt = _claude_runtime(plane)
        start = asyncio.ensure_future(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
        await _until(plane.new_started, start)
        start.cancel()
        await asyncio.wait({start}, timeout=_SIGNAL_DEADLINE_SECS)
        plane.release.set()
        await asyncio.wait_for(plane.timed_out.wait(), _SIGNAL_DEADLINE_SECS)
        assert plane.late is not None
        plane.late.set_result({"sessionId": "s-18"})
        await asyncio.wait_for(plane.closed.wait(), _SIGNAL_DEADLINE_SECS)
        return plane.sent

    assert ("session/close", {"sessionId": "s-18"}) in asyncio.run(scenario())


def test_a_killed_runtime_releases_every_sessions_prepared_state(harness, tmp_path):
    """The process is gone, so are its sessions: whatever a session prepared (its
    settings seed) is released even when no teardown reached it."""
    from kiro_crew.acp.runtime_tree_kill import _end_remaining_session_states

    plane = _Plane({"sessionId": "s-17", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert _seed(tmp_path).exists() and rt._session_harness_states
    asyncio.run(_end_remaining_session_states(rt))
    assert rt._session_harness_states == {}
    assert not _seed(tmp_path).exists()


# --- The held limits of a moved Bot reach the shared session's envelope ---------------


@pytest.fixture
def held_ledger(monkeypatch, tmp_path):
    """A ``migrated.json`` under a test home, and the session's Bot settable per test."""
    import json

    import kiro_crew.config.paths as paths_mod
    from kiro_crew import execution_context
    from kiro_crew.connections import crew_oauth_proxy

    mates = tmp_path / "home" / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: tmp_path / "home")
    monkeypatch.setattr(crew_oauth_proxy, "is_connection_server", lambda key, name: False)
    who = {"member": "m1"}
    monkeypatch.setattr(
        execution_context,
        "read_session_execution",
        lambda key: type("E", (), {"member_id": who["member"]})(),
    )
    path = mates.parent / "migrated.json"

    def hold(capability: str | None, member: str = "m1") -> None:
        who["member"] = member
        if capability is None:
            if path.exists():
                path.unlink()
            return
        rules = [{"text": "a held limit", "capability": capability}]
        path.write_text(json.dumps({"held_rules": {"zxc": {"member_id": "m1", "rules": rules}}}))

    return hold


def _shared_options(harness: Any, work_dir: Path, servers: list[dict]) -> dict:
    state = asyncio.run(_prepared_for_envelope(harness, work_dir))
    try:
        return harness.session_meta(state, servers)["claudeCode"]["options"]
    finally:
        asyncio.run(harness.end_session(state))


def _asks(options: dict) -> list[str]:
    return list(((options.get("settings") or {}).get("permissions") or {}).get("ask") or [])


_SERVERS = [{"name": "gh", "command": "x"}, {"name": "dot.probe", "command": "y"}]


def test_a_shared_session_of_a_bot_with_a_held_read_limit_asks_for_reads(
    harness, tmp_path, held_ledger
):
    """Claude's ``default`` mode runs in-project reads unasked: the shared session of a
    Bot that holds a read limit asks for each, as the per-session client does."""
    held_ledger("fs_read")
    held = _shared_options(harness, tmp_path, _SERVERS)
    assert _asks(held) == ["Read", "Grep", "Glob"]
    assert held["allowDangerouslySkipPermissions"] is False
    assert "strictMcpConfig" not in held

    # Controls: another member, and no ledger at all, get nothing.
    held_ledger("fs_read", member="other")
    assert _asks(_shared_options(harness, tmp_path, _SERVERS)) == []
    held_ledger(None)
    assert _asks(_shared_options(harness, tmp_path, _SERVERS)) == []


def test_a_shared_session_under_a_held_mcp_limit_asks_on_each_server_it_sends(
    harness, tmp_path, held_ledger
):
    held_ledger("mcp")
    held = _shared_options(harness, tmp_path, _SERVERS)
    # Claude spells a server's tools with `.` as `_`; the ask must match them.
    assert _asks(held) == ["mcp__gh", "mcp__dot_probe"]
    assert held["strictMcpConfig"] is True

    held_ledger("mcp", member="other")
    other = _shared_options(harness, tmp_path, _SERVERS)
    assert _asks(other) == []
    assert "strictMcpConfig" not in other


def test_the_shared_envelope_asks_what_the_per_session_client_asks(
    harness, tmp_path, held_ledger, monkeypatch
):
    """Pinned to the per-session result, so the two paths cannot drift. The array
    carries no pooled stubs: with them the shared list may name more servers."""
    from types import SimpleNamespace

    from kiro_crew.acp.client_claude_session import _claude_session_meta, _prepare_claude_held_asks

    held_ledger("*")
    client = SimpleNamespace(
        backend=ACP_BACKEND_CLAUDE,
        memory_mode="persistent",
        _session_key=f"dashboard:{tmp_path.name}",
        _session_mcp_cache=list(_SERVERS),
    )
    asyncio.run(_prepare_claude_held_asks(client))
    per_session = _claude_session_meta(client)["claudeCode"]["options"]
    shared = _shared_options(harness, tmp_path, _SERVERS)
    assert _asks(per_session)
    assert _asks(shared) == _asks(per_session)
    assert shared["strictMcpConfig"] is per_session["strictMcpConfig"] is True


def test_a_second_envelope_for_the_same_session_does_not_double_its_asks(
    harness, tmp_path, held_ledger
):
    """The substitution retry builds the envelope again for the same state."""
    held_ledger("*")
    state = asyncio.run(_prepared_for_envelope(harness, tmp_path))
    first = _asks(harness.session_meta(state, _SERVERS)["claudeCode"]["options"])
    second = _asks(harness.session_meta(state, _SERVERS)["claudeCode"]["options"])
    assert first == second
    assert len(second) == len(set(second))
    # A start with a smaller array asks only on the servers it sends.
    third = _asks(harness.session_meta(state, _SERVERS[:1])["claudeCode"]["options"])
    assert "mcp__gh" in third and "mcp__dot_probe" not in third
    asyncio.run(harness.end_session(state))


def test_the_shared_prepare_reads_held_asks_after_the_side_denies(harness, tmp_path, monkeypatch):
    steps: list[str] = []
    real_side = ClaudeSharedSession._prepare_claude_side_read_denies

    async def _side(self: Any) -> None:
        steps.append("side")
        await real_side(self)

    async def _held(self: Any) -> None:
        steps.append("held")
        self._claude_held_asks = ("Read",)
        self._claude_held_mcp = False

    monkeypatch.setattr(ClaudeSharedSession, "_prepare_claude_side_read_denies", _side)
    monkeypatch.setattr(ClaudeSharedSession, "_prepare_claude_held_asks", _held)
    state = asyncio.run(_prepare(harness, tmp_path))
    assert steps == ["side", "held"]
    assert state._claude_held_builtin_asks == ("Read",)
    asyncio.run(harness.end_session(state))
