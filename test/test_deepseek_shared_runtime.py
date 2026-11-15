"""The DeepSeek Harness on the shared runtime (``DeepseekHarness``).

On ``AcpClient`` a deepseek session owns its process: one ``--patch`` gate, one
read-back, one nonce, and a completed call Crew's gate never asked about stops that
process. On one process several sessions share, the gate is still the process's (the
plugin asks about every call of every session alike), but the tripwire's ledger has to
be one session's: a permission request names its session, so an ask in session A can
never vouch for a call in session B. A call that ran past the gate fails the runtime,
because the gate is gone for every session on it. The process restores a closed
session with ``session/resume``. No session is routed here yet; the harness is reached
through ``harness_for``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import agent_scratch
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp import session_handle_permissions as sh_permissions
from kiro_crew.acp.harness import deepseek as deepseek_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    METHOD_SESSION_CLOSE,
    METHOD_SESSION_RESUME,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_ACP_RUNTIME,
    can_restore_session,
)
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import await_until

_ARGV = ["/opt/bin/dsh", "--profile", "acp"]
_SEALED = "/opt/run/kirocrew_dsh_gate_1.mjs"
_PATCH = "/opt/run/kirocrew_dsh_gate_1.patch.yml"


def _completed(tool_call_id: str, status: str = "completed") -> dict[str, Any]:
    return {"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id, "status": status}


def _event(**kw: Any) -> AcpEvent:
    return AcpEvent(kind="permission_request", request_id="perm-1", **kw)


class _NoAudit:
    """An audit log that writes nothing: every call is a no-op."""

    def __getattr__(self, _name: str) -> Any:
        return lambda *_a, **_kw: None


@pytest.fixture(autouse=True)
def _no_real_audit_rows(_floor_monkeypatch):
    """A tripwire writes an audit row (``_refuse_moved_permission_state``); a real one
    goes through the background ``sel-writer`` thread, which can still be importing
    the redaction engine when the next test redacts on the main thread. A test that
    reads the rows installs its own stand-in over this one."""
    from kiro_crew import sel as sel_module

    _floor_monkeypatch.setattr(sel_module, "sel", lambda: _NoAudit())


# ── The harness's answers ──


def test_the_harness_is_registered_and_routes_nothing():
    assert ACP_BACKEND_DEEPSEEK not in ACP_BACKENDS_ACP_RUNTIME
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    assert isinstance(harness, deepseek_mod.DeepseekHarness)
    assert harness.per_session_state is True
    assert harness.permission_state_issue_ends_process is True
    assert harness.teardown.method == METHOD_SESSION_CLOSE
    assert harness.teardown.notification is False
    assert harness.host_answered_methods == ()
    assert harness.verifies_agent_activation is False


def test_only_the_deepseek_harness_ends_the_process_on_a_session_issue():
    assert harness_for(ACP_BACKEND_CODEX).permission_state_issue_ends_process is False


# ── Seam 1: the spawn is DeepseekLaunch's, against the runtime's own environment ──


@pytest.fixture
def launch_seams(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    async def _launch(backend):
        return _ARGV[0], list(_ARGV), "dsh --profile acp", "dsh"

    async def _preflight(preflight, backend, mode):
        return ("/opt/creds/.aws",)

    def _allocate(label):
        seen["probe_label"] = label
        probe = tmp_path / "probe"
        probe.mkdir()
        return probe

    async def _wrap(argv, **kwargs):
        return list(argv), None

    def _verify(session, backend, argv, extension_path, marker_path, nonce, **kwargs):
        seen["session"] = session
        seen["nonce"] = nonce
        seen["argv"] = list(argv)
        return seen.get("routing", ("", ""))

    def _no_key(*_a, **_kw):
        raise AssertionError("the spawn reached the provider key; only the child env does")

    monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _launch)
    monkeypatch.setattr(launch_mod, "_run_preflight_bounded", _preflight)
    monkeypatch.setattr(deepseek_mod, "_seal_deepseek_gate_extension", lambda: _SEALED)
    monkeypatch.setattr(deepseek_mod, "_write_deepseek_gate_patch", lambda _p: _PATCH)
    monkeypatch.setattr(deepseek_mod, "_deepseek_vault_env_names", lambda: ("DEEPSEEK_API_KEY",))
    monkeypatch.setattr(agent_scratch, "allocate_scratch", _allocate)
    monkeypatch.setattr(deepseek_mod, "wrap_argv_async", _wrap)
    monkeypatch.setattr(deepseek_mod, "_verify_deepseek_gate", _verify)
    monkeypatch.setattr(deepseek_mod, "_deepseek_vault_env", _no_key)
    return seen


def _ctx(tmp_path, **kw: Any) -> SpawnContext:
    return SpawnContext(
        agent="kirocrew",
        work_dir=tmp_path,
        model=None,
        environ={},
        home=tmp_path,
        sandbox_mode="standard",
        **kw,
    )


def test_the_spawn_composes_the_gate_and_proves_it_in_the_runtimes_environment(
    launch_seams, tmp_path
):
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    plan = asyncio.run(
        harness.resolve_spawn(
            _ctx(tmp_path, extra_env={"DSH_HOME": "/srv/dsh"}, agent_env={"HTTPS_PROXY": "p"})
        )
    )
    assert plan.argv == [*_ARGV, "--patch", _PATCH]
    assert plan.extra_hidden_dirs == ("/opt/creds/.aws",)
    probe = launch_seams["session"]
    # The read-back boots in what the child will get: the runtime's extra_env and
    # agent.env snapshot, in the runtime's own work directory.
    assert probe._extra_env == {"DSH_HOME": "/srv/dsh"}
    assert probe._agent_env_for_spawn() == {"HTTPS_PROXY": "p"}
    assert probe._spawn_work_dir == str(tmp_path)
    assert launch_seams["argv"] == [*_ARGV, "--patch", _PATCH]
    # One nonce for the process, the one the read-back was handed, carried to the
    # child with the pinned mode; the harness itself keeps nothing.
    assert launch_seams["nonce"]
    assert plan.child_env == {
        "DSH_PERMISSION_MODE": deepseek_mod.DEEPSEEK_PERMISSION_MODE,
        "KIROCREW_DSH_GATE_SESSION": launch_seams["nonce"],
    }
    assert vars(harness) == {}


def test_a_read_back_that_did_not_see_the_gate_refuses_the_spawn(launch_seams, tmp_path):
    launch_seams["routing"] = ("the gate's load marker was not written", "remedy")
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(harness.resolve_spawn(_ctx(tmp_path)))


def test_the_child_gets_the_vault_key_and_not_kiros_model_key(monkeypatch):
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    resolved = {"DEEPSEEK_API_KEY": "sk-test"}
    monkeypatch.setattr(
        deepseek_mod, "_deepseek_vault_env", lambda: (resolved, ("DEEPSEEK_API_KEY",))
    )
    env = {"KIRO_API_KEY": "kiro-key", "PATH": "/bin"}
    harness.apply_spawn_env(env)
    assert env["DEEPSEEK_API_KEY"] == "sk-test"
    assert "KIRO_API_KEY" not in env
    # The resolver's plaintext is emptied once copied onto the child's env.
    assert resolved == {}


def test_the_runtime_puts_the_spawns_child_env_on_the_child_before_the_host_answers():
    import inspect

    from kiro_crew.acp import runtime_spawn_launch

    source = inspect.getsource(runtime_spawn_launch)
    assert source.index("env.update(plan.child_env)") < source.index(
        "self._harness.apply_spawn_env("
    )


def test_a_provider_key_mapping_the_harness_would_not_honour_refuses_the_spawn(monkeypatch):
    def _refused():
        raise ValueError("agent.deepseek_env entry 'X' holds a literal value")

    monkeypatch.setattr(deepseek_mod, "_deepseek_vault_env", _refused)
    with pytest.raises(AcpToolGateUnroutable):
        harness_for(ACP_BACKEND_DEEPSEEK).apply_spawn_env({})


# ── Session restore: session/resume, advertised as sessionCapabilities.resume ──


def test_deepseek_restores_through_the_resume_capability_and_nothing_else():
    assert can_restore_session(ACP_BACKEND_DEEPSEEK, {"sessionCapabilities": {"resume": {}}})
    assert not can_restore_session(ACP_BACKEND_DEEPSEEK, {"loadSession": True})
    assert can_restore_session(ACP_BACKEND_CODEX, {"loadSession": True})
    assert not can_restore_session(ACP_BACKEND_CODEX, {"sessionCapabilities": {"resume": {}}})


def test_both_drivers_read_the_restore_capability_with_one_function():
    """``AcpClient`` and ``AcpRuntime`` cannot disagree on whether a session restores."""
    import inspect

    from kiro_crew.acp import client_start, runtime_spawn_launch

    for module in (client_start, runtime_spawn_launch):
        source = inspect.getsource(module)
        assert "can_restore_session(" in source, module.__name__
        assert '.get("resume")' not in source, module.__name__


# ── The tripwire, on each session's own ledger ──


async def _prepare(harness: Any) -> Any:
    return await harness.prepare_session(
        work_dir="/w",
        agent="kirocrew",
        model="",
        session_key="k",
        channel_id="dashboard",
        memory_mode="",
    )


def test_a_completed_call_the_gate_never_asked_about_is_an_issue():
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    state = asyncio.run(_prepare(harness))
    assert "without asking" in harness.session_update_issue(state, _completed("call_0"))


def test_an_approved_call_completes_once_and_its_ask_is_consumed():
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    state = asyncio.run(_prepare(harness))
    assert (
        harness.permission_refusal(state, _event(tool_call_id="call_0"), unclassified=False) is None
    )
    harness.note_permission_approved(state, "call_0")
    assert harness.session_update_issue(state, _completed("call_0")) == ""
    # A provider reuses ids across turns: the next call under the same id earns its
    # own ask or it is an unasked call.
    assert "without asking" in harness.session_update_issue(state, _completed("call_0"))


def test_an_asked_call_that_completes_without_crews_yes_is_an_issue():
    """Refused on any path (the floor, the batch guard, a person), cancelled, or not
    yet answered: no yes was written, so its completion ran past the gate."""
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    state = asyncio.run(_prepare(harness))
    harness.permission_refusal(state, _event(tool_call_id="call_1"), unclassified=False)
    assert "without Kiro Crew's gate approving" in harness.session_update_issue(
        state, _completed("call_1")
    )


def test_a_refused_call_that_fails_and_other_updates_are_not_issues():
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    state = asyncio.run(_prepare(harness))
    harness.permission_refusal(state, _event(tool_call_id="call_2"), unclassified=False)
    assert harness.session_update_issue(state, _completed("call_2", "failed")) == ""
    assert harness.session_update_issue(state, _completed("call_3", "in_progress")) == ""
    assert harness.session_update_issue(state, {"sessionUpdate": "agent_message_chunk"}) == ""


def test_one_sessions_ask_is_never_counted_for_another_sessions_call():
    """Two sessions on one process, the same provider id: A's ask is A's alone."""
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    a = asyncio.run(_prepare(harness))
    b = asyncio.run(_prepare(harness))
    harness.permission_refusal(a, _event(tool_call_id="call_0"), unclassified=False)
    harness.note_permission_approved(a, "call_0")
    assert "without asking" in harness.session_update_issue(b, _completed("call_0"))
    assert harness.session_update_issue(a, _completed("call_0")) == ""


# ── The session handle's side ──


class _Runtime:
    def __init__(self, states: dict[str, Any]) -> None:
        self.acp_backend = ACP_BACKEND_DEEPSEEK
        self._harness = harness_for(ACP_BACKEND_DEEPSEEK)
        self._session_harness_states = states
        self.terminated: list[str] = []
        self.dead: list[str] = []
        self.killed: list[tuple[str, bool]] = []

    async def terminate_session(self, session_id: str) -> None:
        self.terminated.append(session_id)

    def _mark_dead(self, reason: str, **_kw: Any) -> None:
        self.dead.append(reason)

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        # Recorded with whether the runtime was already dead: the ownership gate
        # lets a kill through a dead runtime's leases only.
        self.killed.append((reason, bool(self.dead)))


def _handle(runtime: _Runtime, session_id: str) -> Any:
    handle = SimpleNamespace(
        _runtime=runtime,
        _session_id=session_id,
        _session_key="",
        _permission_state_issue="",
        _tool_call_mcp_server={},
        _tool_call_is_shell={},
    )
    handle._note_permission_state = lambda msg: sh_permissions._note_permission_state(handle, msg)
    handle._tool_call_unclassified = lambda event: False
    return handle


def _update_frame(sid: str, update: dict[str, Any]) -> JsonRpcMessage:
    return JsonRpcMessage(method="session/update", params={"sessionId": sid, "update": update})


def test_a_permission_request_is_recorded_on_its_own_sessions_ledger_only():
    """Through the handle the runtime routes the frame to: A's ask never lands on B."""
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    states = {"a": asyncio.run(_prepare(harness)), "b": asyncio.run(_prepare(harness))}
    runtime = _Runtime(states)
    handle_a = _handle(runtime, "a")
    refused = asyncio.run(
        sh_permissions._refuse_by_harness(handle_a, _event(tool_call_id="call_0"))
    )
    assert refused is False
    assert states["a"].asked == {"call_0"}
    assert states["b"].asked == set()
    handle_b = _handle(runtime, "b")
    handle_b._note_permission_state(_update_frame("b", _completed("call_0")))
    assert "without asking" in handle_b._permission_state_issue
    harness.note_permission_approved(states["a"], "call_0")
    handle_a._note_permission_state(_update_frame("a", _completed("call_0")))
    assert handle_a._permission_state_issue == ""


def test_a_call_past_the_gate_fails_and_kills_the_runtime():
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    runtime = _Runtime({"a": asyncio.run(_prepare(harness))})
    handle = _handle(runtime, "a")
    handle._note_permission_state(_update_frame("a", _completed("call_9")))
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(sh_permissions._refuse_moved_permission_state(handle))
    # No session/close: the process is ending, and dsh keeps the record either way.
    assert runtime.terminated == []
    assert len(runtime.dead) == 1 and "call_9" in runtime.dead[0]
    # Killed after it was marked dead, so the other chats' leases do not refuse it.
    assert len(runtime.killed) == 1 and runtime.killed[0][1] is True


def test_a_cancel_during_the_kill_still_leaves_the_runtime_dead_and_killed():
    """A Stop landing while the kill is in flight cannot keep the process serving."""
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    runtime = _Runtime({"a": asyncio.run(_prepare(harness))})
    handle = _handle(runtime, "a")
    handle._note_permission_state(_update_frame("a", _completed("call_9")))
    release = asyncio.Event()
    entered = asyncio.Event()
    killed_through = asyncio.Event()

    async def _slow_kill(*, expected: bool = False, reason: str = "") -> None:
        runtime.killed.append((reason, bool(runtime.dead)))
        entered.set()
        await release.wait()
        killed_through.set()

    runtime.kill = _slow_kill  # type: ignore[method-assign]

    async def _go() -> None:
        task = asyncio.ensure_future(sh_permissions._refuse_moved_permission_state(handle))
        reached = asyncio.ensure_future(entered.wait())
        done, _ = await asyncio.wait({task, reached}, timeout=5, return_when="FIRST_COMPLETED")
        if reached not in done:
            # The refusal ended (or stalled) without reaching the kill: fail, not hang.
            reached.cancel()
            task.cancel()
            pytest.fail(f"the tripwire did not reach the kill (finished={task in done})")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled refusal")
        release.set()
        # The shielded kill runs on after its caller was cancelled.
        await await_until(killed_through, what="the shielded kill")

    asyncio.run(_go())
    assert len(runtime.dead) == 1
    assert runtime.killed[0][1] is True
    assert killed_through.is_set()


def test_a_goose_mode_move_still_closes_only_its_own_session():
    from kiro_crew.acp.types import ACP_BACKEND_GOOSE

    runtime = _Runtime({"a": object()})
    runtime.acp_backend = ACP_BACKEND_GOOSE
    runtime._harness = harness_for(ACP_BACKEND_GOOSE)
    handle = _handle(runtime, "a")
    handle._note_permission_state(
        _update_frame("a", {"sessionUpdate": "current_mode_update", "currentModeId": "auto"})
    )
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(sh_permissions._refuse_moved_permission_state(handle))
    assert runtime.terminated == ["a"]
    assert runtime.dead == [] and runtime.killed == []


def test_only_a_written_yes_records_an_approval():
    """The approve path records the yes after the answer is written; no reject path
    records anything, so every refusal (the floor, the batch guard, a person) and a
    cancel leave the call unapproved."""
    import ast
    import inspect
    import textwrap

    def _calls(fn: Any) -> list[str]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        return [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]

    approve = inspect.getsource(sh_permissions.approve_tool)
    assert approve.index("sent = True") < approve.index("note_permission_approved(")
    assert "note_permission_approved" not in _calls(sh_permissions.reject_tool)


# ── The shared runtime's session start and restore, with the control plane recorded ──


class _Plane:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self._ids = iter(["s-1", "s-2", "s-3"])

    async def send_and_await(self, method: str, params: dict, timeout: Any = None) -> dict:
        self.sent.append((method, params))
        if method == "session/new":
            return {"sessionId": next(self._ids), "configOptions": []}
        if method == METHOD_SESSION_RESUME:
            # dsh answers a resume with its config options and no ``modes``.
            return {"configOptions": []}
        return {}


def _deepseek_runtime(plane: _Plane, tmp_path) -> Any:
    from unittest.mock import MagicMock

    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(
        work_dir=str(tmp_path), acp_backend=ACP_BACKEND_DEEPSEEK, expect_mcp_reports=False
    )
    proc = MagicMock()
    proc.stdout = None
    proc.stdin = MagicMock()
    proc.returncode = None
    # Above every pid_max, so no cleanup path can signal a real process.
    proc.pid = UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = UNALLOCATABLE_PID
    rt._initialized = True
    rt._can_load_session = True
    rt._agent_version = "0.0.1"
    rt._send_and_await = plane.send_and_await  # type: ignore[method-assign]
    return rt


def test_a_session_starts_and_closes_with_its_own_ledger(tmp_path):
    plane = _Plane()
    rt = _deepseek_runtime(plane, tmp_path)
    handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert handle.session_id == "s-1"
    assert isinstance(rt._session_harness_states["s-1"], deepseek_mod._DeepseekSession)
    asyncio.run(rt.terminate_session("s-1"))
    assert plane.sent[-1][0] == METHOD_SESSION_CLOSE
    assert rt._session_harness_states == {}


def test_a_closed_session_is_restored_with_session_resume(tmp_path):
    plane = _Plane()
    rt = _deepseek_runtime(plane, tmp_path)
    handle = asyncio.run(rt.load_session("", "s-7", cwd=str(tmp_path), agent="kirocrew"))
    assert handle.session_id == "s-7"
    methods = [method for method, _params in plane.sent]
    assert METHOD_SESSION_RESUME in methods
    assert "session/load" not in methods
    params = dict(plane.sent[methods.index(METHOD_SESSION_RESUME)][1])
    assert params["sessionId"] == "s-7"
    assert isinstance(rt._session_harness_states["s-7"], deepseek_mod._DeepseekSession)


def test_a_tripwire_in_one_chat_ends_the_others_turn_and_they_resume_on_a_new_process(
    tmp_path,
):
    """The trade-off made concrete: B is not lost when A's call ran past the gate.

    A's tripwire fails the shared runtime: B's queue gets the death sentinel, so B's
    running turn ends with the runtime's death instead of waiting on a process that
    is being killed. B's record is the harness's own (dsh keeps it on disk; measured:
    a SIGKILLed process's sessions resume on a new one with their history), so the
    chat restores B on a fresh process with ``session/resume``.
    """
    from unittest.mock import AsyncMock

    plane = _Plane()
    rt = _deepseek_runtime(plane, tmp_path)
    rt.kill = AsyncMock()  # type: ignore[method-assign]
    a = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    b = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert (a.session_id, b.session_id) == ("s-1", "s-2")
    a._note_permission_state(_update_frame("s-1", _completed("call_0")))
    assert b._permission_state_issue == ""
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(a._refuse_moved_permission_state())
    assert rt._dead and not rt.is_alive()
    rt.kill.assert_awaited_once()
    assert (METHOD_SESSION_CLOSE, {"sessionId": "s-1"}) not in plane.sent
    # B's turn is woken by the death sentinel, not left waiting.
    assert rt._session_queues["s-2"].get_nowait() is None

    fresh = _Plane()
    rt2 = _deepseek_runtime(fresh, tmp_path)
    restored = asyncio.run(rt2.load_session("", "s-2", cwd=str(tmp_path), agent="kirocrew"))
    assert restored.session_id == "s-2"
    assert [m for m, _p in fresh.sent if m == METHOD_SESSION_RESUME] == [METHOD_SESSION_RESUME]


# ── An MCP server whose command cannot start, on the shared runtime ──


def _unstartable_server(tmp_path: Path) -> dict[str, Any]:
    """An absolute path that does not exist, on every platform."""
    missing = tmp_path / "missing" / "kirocrew-probe-mcp"
    return {"name": "broken-probe", "command": str(missing), "args": []}


def test_the_shared_prune_drops_a_server_whose_command_cannot_start(tmp_path):
    from kiro_crew.acp.mcp_prune import prune_session_servers

    good = {"name": "ok", "command": sys.executable, "args": []}
    result = asyncio.run(
        prune_session_servers(ACP_BACKEND_DEEPSEEK, [good, _unstartable_server(tmp_path)])
    )
    assert result.kept == [good]
    assert len(result.pruned) == 1  # its name is sanitized on the way in
    assert result.omitted == 0


def _seed_array(monkeypatch, rt: Any, servers: list[dict[str, Any]]) -> None:
    """Every array build on *rt* starts from *servers*, as a spec naming them would."""
    from kiro_crew.acp import runtime_session_create, runtime_session_load

    real = runtime_session_create.prune_session_servers

    async def _prune(backend: str, built: list[Any]) -> Any:
        return await real(backend, [*built, *servers])

    monkeypatch.setattr(runtime_session_create, "prune_session_servers", _prune)
    monkeypatch.setattr(runtime_session_load, "prune_session_servers", _prune)


def test_a_new_shared_session_is_sent_without_the_server_and_reports_it_failed(
    monkeypatch, tmp_path
):
    plane = _Plane()
    rt = _deepseek_runtime(plane, tmp_path)
    _seed_array(monkeypatch, rt, [_unstartable_server(tmp_path)])
    asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    sent = next(params for method, params in plane.sent if method == "session/new")
    assert "broken-probe" not in [s.get("name") for s in sent["mcpServers"]]
    # One failed entry: the left-out server (its name is sanitized on the way in).
    assert len(rt._mcp_reports()["s-1"]._failed) == 1


def test_a_resumed_shared_session_is_sent_without_the_server_and_reports_it_failed(
    monkeypatch, tmp_path
):
    plane = _Plane()
    rt = _deepseek_runtime(plane, tmp_path)
    _seed_array(monkeypatch, rt, [_unstartable_server(tmp_path)])
    asyncio.run(rt.load_session("", "s-7", cwd=str(tmp_path), agent="kirocrew"))
    sent = next(params for method, params in plane.sent if method == METHOD_SESSION_RESUME)
    assert "broken-probe" not in [s.get("name") for s in sent["mcpServers"]]
    assert len(rt._mcp_reports()["s-7"]._failed) == 1


# ── The audit row a trip writes ──


def test_a_trip_is_audited_as_the_call_that_ran_not_as_a_mode_change(monkeypatch):
    from kiro_crew import sel as sel_module

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sel_module, "sel", lambda: SimpleNamespace(log_tool_invocation=lambda **kw: rows.append(kw))
    )
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    state = asyncio.run(_prepare(harness))
    harness.permission_refusal(state, _event(tool_call_id="call_5"), unclassified=False)
    runtime = _Runtime({"a": state})
    handle = _handle(runtime, "a")
    handle._note_permission_state(_update_frame("a", _completed("call_5")))
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(sh_permissions._refuse_moved_permission_state(handle))
    assert rows and rows[0]["tool_name"] == "call_5"
    assert rows[0]["outcome"] == "ran_without_approval"
    assert rows[0]["metadata"]["reason"] == "pi_gate_tripwire"


def test_a_side_chat_session_is_refused_a_shared_process():
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(
            harness.prepare_session(
                work_dir="/w",
                agent="kirocrew",
                model="",
                session_key="k",
                channel_id="dashboard",
                memory_mode="",
                options={"side_read_only": True},
            )
        )
