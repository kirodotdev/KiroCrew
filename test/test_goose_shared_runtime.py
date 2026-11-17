"""goose on the shared runtime (``GooseHarness``): each session's own permission checks.

On ``AcpClient`` a goose session owns its process, so a mode read-back that fails, or a
mid-session move to ``auto``, kills the process. On one process several sessions share,
each check has to be one session's: the mode is read back off that session's own
answer, a refusal closes that session alone, and the identity-channel refusals read that
session's own placed servers. No session is routed here yet; the harness is reached
through ``harness_for``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp import session_handle_permissions as sh_permissions
from kiro_crew.acp.harness import goose as goose_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.acp.types import (
    ACP_BACKEND_GOOSE,
    METHOD_SESSION_CLOSE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_ACP_RUNTIME
from kiro_crew.testing.ids import UNALLOCATABLE_PID

_ARGV = ["/opt/bin/goose", "acp"]


def _modes(mode: str | None) -> dict[str, Any]:
    return {
        "currentModeId": mode,
        "availableModes": [{"id": "auto"}, {"id": "approve"}, {"id": "smart_approve"}],
    }


def _mode_update(sid: str, mode: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": sid,
            "update": {"sessionUpdate": "current_mode_update", "currentModeId": mode},
        },
    )


@pytest.fixture
def harness(monkeypatch):
    async def _launch(backend: str) -> tuple[str, list[str], str, str]:
        return _ARGV[0], list(_ARGV), "goose acp", "goose"

    asked: list[str] = []

    async def _mask(preflight: Any, backend: str, mode: str) -> tuple[str, ...]:
        asked.append(backend)
        return ("/home/u/.config/goose",)

    monkeypatch.setattr(goose_mod.launch_mod, "resolve_self_served_launch", _launch)
    monkeypatch.setattr(goose_mod.launch_mod, "_run_preflight_bounded", _mask)
    harness = harness_for(ACP_BACKEND_GOOSE)
    harness._masks_asked = asked  # type: ignore[attr-defined]
    return harness


# ── The harness's answers ──


def test_the_harness_is_registered_and_routes_nothing():
    assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_ACP_RUNTIME
    harness = harness_for(ACP_BACKEND_GOOSE)
    assert harness.per_session_state is True
    assert harness.teardown.method == METHOD_SESSION_CLOSE
    assert harness.teardown.notification is False
    assert harness.host_answered_methods == ()
    assert harness.verifies_agent_activation is False


def test_the_handshake_is_the_one_the_client_sends():
    from kiro_crew.acp import client_start
    from kiro_crew.acp.client_native_children import client_capabilities_for

    harness = harness_for(ACP_BACKEND_GOOSE)
    assert harness.protocol_version == client_start.PROTOCOL_VERSION_GOOSE
    assert harness.client_capabilities == client_capabilities_for(ACP_BACKEND_GOOSE)


def test_the_spawn_loads_the_developer_builtin_under_gooses_own_mask(
    harness, tmp_path, monkeypatch
):
    """The mask and the files handed back are goose's, never another harness's."""
    from kiro_crew import acp_tool_gate
    from kiro_crew.acp.harness import SpawnContext

    exposed: list[str] = []

    def _expose(backend: str, hidden: tuple[str, ...]) -> tuple[str, ...]:
        exposed.append(backend)
        return ()

    monkeypatch.setattr(acp_tool_gate, "adapter_expose_files", _expose)
    ctx = SpawnContext(
        agent="kirocrew", work_dir=tmp_path, model=None, environ={}, home=tmp_path, session=None
    )
    plan = asyncio.run(harness.resolve_spawn(ctx))
    assert plan.argv == [*_ARGV, "--with-builtin", "developer"]
    assert plan.extra_hidden_dirs == ("/home/u/.config/goose",)
    assert harness._masks_asked == [ACP_BACKEND_GOOSE]
    assert exposed == [ACP_BACKEND_GOOSE]


def test_the_process_seeds_the_required_mode_and_keeps_the_operators_home(harness):
    """The child reads the goose home the operator chose, the one the era reads too."""
    env = {"GOOSE_MODE": "auto", "GOOSE_PATH_ROOT": "/operator/goose", "PATH": "/bin"}
    harness.apply_spawn_env(env)
    assert env["GOOSE_MODE"] == "approve"
    assert env["GOOSE_PATH_ROOT"] == "/operator/goose"
    assert env["PATH"] == "/bin"


# ── The mode read-back, per session ──


async def _prepare(harness: Any) -> Any:
    return await harness.prepare_session(
        work_dir="/w",
        agent="kirocrew",
        model="",
        session_key="dashboard:a",
        channel_id="",
        memory_mode="persistent",
    )


def _start(harness: Any, resp: dict[str, Any]) -> list[str]:
    closed: list[str] = []

    async def _send(method: str, params: dict) -> dict:
        raise AssertionError("the read-back sends nothing")

    async def _close() -> None:
        closed.append("s-1")

    async def _run() -> None:
        state = await _prepare(harness)
        await harness.after_session_start(
            state, "s-1", resp, send_request=_send, close_session=_close
        )

    asyncio.run(_run())
    return closed


def test_the_required_mode_keeps_the_session(harness):
    assert _start(harness, {"sessionId": "s-1", "modes": _modes("approve")}) == []


@pytest.mark.parametrize(
    "resp",
    [
        {"sessionId": "s-1", "modes": _modes("auto")},
        {"sessionId": "s-1", "modes": _modes("smart_approve")},
        {"sessionId": "s-1"},
        {"sessionId": "s-1", "modes": _modes("")},
    ],
    ids=["auto", "smart_approve", "no-modes-block", "empty-mode"],
)
def test_any_other_mode_closes_only_this_session(harness, resp):
    closed: list[str] = []

    async def _close() -> None:
        closed.append("s-1")

    async def _send(method: str, params: dict) -> dict:
        return {}

    async def _run() -> None:
        state = await _prepare(harness)
        await harness.after_session_start(
            state, "s-1", resp, send_request=_send, close_session=_close
        )

    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(_run())
    assert closed == ["s-1"]


def test_a_mode_move_is_an_issue_and_the_required_mode_is_not(harness):
    state = asyncio.run(_prepare(harness))
    move = _mode_update("s-1", "auto").params["update"]
    stay = _mode_update("s-1", "approve").params["update"]
    assert harness.session_update_issue(state, move)
    assert harness.session_update_issue(state, stay) == ""
    assert harness.session_update_issue(state, {"sessionUpdate": "agent_message_chunk"}) == ""


def test_the_client_and_the_harness_read_the_mode_alike():
    """One read of goose's answer serves both drivers."""
    for mode in ("approve", "auto", "smart_approve", ""):
        resp = {"sessionId": "s", "modes": _modes(mode)}
        assert bool(goose_mod.session_mode_issue(resp)) is (mode != "approve")
        update = _mode_update("s", mode).params["update"]
        assert bool(goose_mod.mode_update_issue(update)[0]) is (mode != "approve")


def test_a_reported_mode_goes_through_the_shared_refusal_scrub(monkeypatch):
    """The mode id goose reports reaches a refusal and a log line, so it goes through
    ``client_permissions._scrub_observed``, the one scrub every harness-sourced
    refusal string gets, on the session read and the update read alike. Patched
    through the ``acp.client`` facade, which carries it to every owner that binds it."""
    from kiro_crew.acp import client as acp_client

    monkeypatch.setattr(acp_client, "_scrub_observed", lambda v: "SCRUBBED")
    _issue, observed = goose_mod.mode_update_issue(_mode_update("s", "auto").params["update"])
    assert observed == "SCRUBBED"
    assert "SCRUBBED" in goose_mod.session_mode_issue({"sessionId": "s", "modes": _modes("auto")})


# ── The identity-channel refusals, per session ──


def _event(**kw: Any) -> AcpEvent:
    return AcpEvent(kind="permission_request", request_id="perm-1", **kw)


def test_a_call_no_channel_classified_is_refused(harness):
    state = asyncio.run(_prepare(harness))
    refusal = harness.permission_refusal(state, _event(tool_call_id="t1"), unclassified=True)
    assert refusal == ("spec_disabled_tool_unclassified_call", "tool__unclassified")
    assert harness.permission_refusal(state, _event(tool_call_id="t1"), unclassified=False) is None


def test_a_trusted_identity_names_only_a_placed_server_or_gooses_own(harness):
    state = asyncio.run(_prepare(harness))
    harness.session_meta(state, [{"name": "kirocrew-core", "command": "x"}])

    def _named(server: str) -> AcpEvent:
        return _event(
            tool_call_id="t1", mcp_server_name=server, mcp_identity_trusted=True, tool_name="t"
        )

    assert harness.permission_refusal(state, _named("kirocrew-core"), unclassified=False) is None
    assert harness.permission_refusal(state, _named("developer"), unclassified=False) is None
    assert harness.permission_refusal(state, _named("drifted"), unclassified=False) == (
        "spec_disabled_tool_foreign_server",
        "mcp__drifted__t",
    )


# ── The session handle's side of both refusals ──


class _Runtime:
    def __init__(self, backend: str = ACP_BACKEND_GOOSE) -> None:
        self.acp_backend = backend
        self._harness = harness_for(backend)
        self._session_harness_states: dict[str, Any] = {"s-1": object()}
        self.terminated: list[str] = []

    async def terminate_session(self, session_id: str) -> None:
        self.terminated.append(session_id)


def _handle(runtime: _Runtime) -> Any:
    handle = SimpleNamespace(
        _runtime=runtime,
        _session_id="s-1",
        _session_key="",
        _permission_state_issue="",
        _tool_call_mcp_server={},
        _tool_call_is_shell={},
    )
    handle._note_permission_state = lambda msg: sh_permissions._note_permission_state(handle, msg)
    return handle


def test_a_mode_move_closes_the_session_and_fails_the_turn():
    runtime = _Runtime()
    handle = _handle(runtime)
    handle._note_permission_state(_mode_update("s-1", "approve"))
    asyncio.run(sh_permissions._refuse_moved_permission_state(handle))
    assert runtime.terminated == []
    handle._note_permission_state(_mode_update("s-1", "auto"))
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(sh_permissions._refuse_moved_permission_state(handle))
    assert runtime.terminated == ["s-1"]


def test_another_sessions_mode_move_is_not_this_sessions():
    """A child session's frame routed here does not close the parent."""
    handle = _handle(_Runtime())
    handle._note_permission_state(_mode_update("child-1", "auto"))
    assert handle._permission_state_issue == ""


def test_a_host_without_session_state_reads_no_update():
    runtime = _Runtime("")
    handle = _handle(runtime)
    handle._note_permission_state(_mode_update("s-1", "auto"))
    assert handle._permission_state_issue == ""


def test_the_unclassified_read_needs_the_tool_call_frame():
    from kiro_crew.acp._dispatch import scoped_tool_cache_key

    handle = _handle(_Runtime())
    event = _event(tool_call_id="t1")
    # Never saw the frame: no evidence, no refusal.
    assert sh_permissions._tool_call_unclassified(handle, event) is False
    key = scoped_tool_cache_key("s-1", "t1")
    handle._tool_call_mcp_server[key] = ""
    assert sh_permissions._tool_call_unclassified(handle, event) is True
    handle._tool_call_is_shell[key] = True
    assert sh_permissions._tool_call_unclassified(handle, event) is False
    del handle._tool_call_is_shell[key]
    handle._tool_call_mcp_server[key] = "kirocrew-core"
    assert sh_permissions._tool_call_unclassified(handle, event) is False


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


def _goose_runtime(plane: _Plane) -> Any:
    from unittest.mock import MagicMock

    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_GOOSE, expect_mcp_reports=False)
    proc = MagicMock()
    proc.stdout = None
    proc.stdin = MagicMock()
    proc.returncode = None
    # Above every pid_max, so no cleanup path can signal a real process.
    proc.pid = UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = UNALLOCATABLE_PID
    rt._initialized = True
    rt._agent_version = "1.57.0"
    rt._send_and_await = plane.send_and_await  # type: ignore[method-assign]
    return rt


def test_a_session_in_the_required_mode_starts_and_closes_with_its_state(tmp_path):
    plane = _Plane({"sessionId": "s-1", "modes": _modes("approve")})
    rt = _goose_runtime(plane)
    handle = asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert handle.session_id == "s-1"
    assert set(rt._session_harness_states) == {"s-1"}
    asyncio.run(rt.terminate_session("s-1"))
    assert plane.sent[-1][0] == METHOD_SESSION_CLOSE
    assert rt._session_harness_states == {}


def test_a_session_in_auto_is_closed_and_the_process_kept(tmp_path):
    plane = _Plane({"sessionId": "s-2", "modes": _modes("auto")})
    rt = _goose_runtime(plane)
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(rt.create_session(cwd=str(tmp_path), agent="kirocrew"))
    assert (METHOD_SESSION_CLOSE, {"sessionId": "s-2"}) in plane.sent
    assert rt._process is not None and not rt._dead
    assert not rt._process.kill.called
    assert rt._session_harness_states == {}


# ── Where the handle runs both refusals ──


def _calls(fn: Any) -> list[str]:
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]


def test_the_turn_reads_every_update_and_every_permission_request_through_the_host():
    """Each refusal on the path a turn's frames take, and the drained ones too."""
    from kiro_crew.acp.session_handle_dispatch import _dispatch_events
    from kiro_crew.acp.session_handle_turn import _run_turn

    dispatch = _calls(_dispatch_events)
    assert "_refuse_by_harness" in dispatch
    assert "_note_permission_state" in dispatch
    assert "_refuse_moved_permission_state" in dispatch
    turn = _calls(_run_turn)
    assert "_note_permission_state" in turn
    assert "_refuse_moved_permission_state" in turn


def test_both_drivers_decide_the_identity_refusals_with_one_function():
    """A renamed goose builtin cannot be refused on one driver and passed on the other."""
    import ast
    import inspect
    import textwrap

    from kiro_crew.acp import client_permissions

    def _called(fn: Any) -> set[str]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        return {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

    names = {"foreign_server_refused", "unclassified_call_refused"}
    assert names <= _called(client_permissions._foreign_mcp_identity) | _called(
        client_permissions._unclassified_tool_call
    )
    assert names <= _called(goose_mod.GooseHarness.permission_refusal)


# ── Routing: only a dashboard chat whose switch is on takes the shared runtime ──


def test_a_side_session_is_never_prepared_on_the_shared_process(harness):
    """Its goose home is the child's GOOSE_PATH_ROOT, which a shared process cannot
    give one session; the placement refuses it first (``side:`` is not a dashboard
    origin), and the harness refuses it again here."""

    async def _prepare_side() -> Any:
        return await harness.prepare_session(
            work_dir="/w",
            agent="kirocrew",
            model="",
            session_key="side:dashboard:a:1",
            channel_id="",
            memory_mode="persistent",
            options={"side_read_only": True},
        )

    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(_prepare_side())
    normal = asyncio.run(_prepare(harness))
    assert normal is not None


def test_a_goose_placement_keys_on_gooses_own_provider(monkeypatch):
    """goose never reads Kiro's sign-in, so its era is its provider identity."""
    from kiro_crew.providers import chat_share

    seen: list[object] = []
    monkeypatch.setattr(
        chat_share,
        "goose_spawn_identity",
        lambda environ=None, env_overrides=None: seen.append(env_overrides) or "goose:x",
    )
    assert chat_share._self_signed_era(ACP_BACKEND_GOOSE, {"A": "1"}) == "goose:x"
    # The start's extra_env reaches the reader.
    assert seen == [{"A": "1"}]
    monkeypatch.setattr(
        chat_share, "goose_spawn_identity", lambda environ=None, env_overrides=None: ""
    )
    assert chat_share._self_signed_era(ACP_BACKEND_GOOSE) == ""
