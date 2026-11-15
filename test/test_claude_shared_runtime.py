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
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, METHOD_SESSION_CLOSE, METHOD_SET_MODE
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_ACP_RUNTIME
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
    meta = harness.session_meta(state, [], zero_tools=False)
    assert meta == state._claude_session_meta()
    assert "claudeCode" in meta
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
