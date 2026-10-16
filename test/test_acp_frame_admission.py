"""A session-open frame is held to its admission until it is written.

The launch-bound check admits a ``session/new``, ``session/load`` or by-name
``session/set_mode`` against the unreadable-main-spec episode, and the frame then
waits for the stdin lock, which a co-tenant can hold for as long as kiro-cli is
not reading. Each test here holds that lock as a co-tenant would, lets the open
pass its check, opens an episode while the frame waits, and releases the lock:
the frame must not be written, the caller gets the shared refusal, and no
pending request is left behind. One test per transport (the shared runtime on
kiro-cli and on KAS, and the direct client), a by-name ``set_mode``, and the
healthy side: a reading that moves while the admission still holds writes the
frame once.

Every wait here is bounded (``kiro_crew.testing.wait``), and a send that ends
before its frame reaches the writer fails the test with its own error.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _make_runtime, _register

from kiro_crew.acp import frame_admission
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpRuntimeError, AcpSessionHandle
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW, METHOD_SET_MODE
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture
def episode(monkeypatch):
    """A healthy host whose episode the test opens; records every SEL denial."""
    import kiro_crew.agent as agent_mod

    audited: list[str] = []
    monkeypatch.setattr(agent_mod, "_main_spec_unreadable", False)
    monkeypatch.setattr(agent_mod, "_main_spec_episode_revision", 0)
    monkeypatch.setattr(agent_mod, "_main_spec_declared_name", None)
    monkeypatch.setattr(agent_mod, "_audit_main_spec_denial", audited.append)
    monkeypatch.setattr(agent_mod, "_schedule_main_spec_early_retry", lambda: None)
    # A refused view recycles an idle runtime; the runtime here is a fake process.
    monkeypatch.setattr(AcpRuntime, "kill", AsyncMock())

    def _open() -> None:
        agent_mod._main_spec_episode_revision += 1
        agent_mod._main_spec_unreadable = True

    return agent_mod, audited, _open


def _entered(owner, monkeypatch) -> asyncio.Event:
    """An event set when *owner*'s request writer is entered, after the open's check."""
    entered = asyncio.Event()
    write = owner._write_request_bounded

    async def _spy(*args, **kwargs):
        entered.set()
        return await write(*args, **kwargs)

    monkeypatch.setattr(owner, "_write_request_bounded", _spy)
    return entered


async def _settled(task: asyncio.Future, *, what: str) -> list:
    """*task*'s outcome as ``[result or exception]``, under the lost-run bound.

    A task still running at the bound is cancelled and drained before the
    failure propagates, so no send outlives its test.
    """
    try:
        return await await_until(asyncio.gather(task, return_exceptions=True), what=what)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _while_a_co_tenant_holds_stdin(lock: asyncio.Lock, entered, open_episode, send):
    """Run *send* while *lock* is held; open the episode once its check has passed.

    A send that ends before it reaches the writer fails the test with its own
    error: the race this sets up never ran.
    """
    await lock.acquire()
    task = asyncio.ensure_future(send())
    try:
        await async_wait_until(
            lambda: entered.is_set() or task.done(),
            describe=lambda: "waiting for the send to reach the request writer",
        )
        if not entered.is_set():
            error = None if task.cancelled() else task.exception()
            raise AssertionError(f"the send ended before its frame waited: {error!r}") from error
        if open_episode is not None:
            open_episode()
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        lock.release()
    return await _settled(task, what="the send under test")


async def _written(proc, task: asyncio.Future) -> bool:
    """Wait until the frame is written or the send ends; whether it was written."""
    await async_wait_until(
        lambda: proc.stdin.write.called or task.done(),
        describe=lambda: "waiting for the frame to be written or the send to end",
    )
    return bool(proc.stdin.write.called)


def _main_spec_view(rt: AcpRuntime, agent_mod) -> None:
    rt._agent = "kirocrew"
    rt._launch_unbound = False
    rt._launch_view_source = (agent_mod.kiro_agents_dir_path() / "kirocrew.json").as_posix()
    rt._launch_view_episode = agent_mod.main_spec_episode()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [METHOD_SESSION_NEW, METHOD_SESSION_LOAD])
@pytest.mark.parametrize("launch", ["main-spec-view", "unbound"])
async def test_an_episode_opened_during_the_lock_wait_refuses_the_shared_open(
    episode, monkeypatch, tmp_path, method, launch
):
    """kiro-cli on the shared runtime: the frame passes its check, then the episode opens.

    Mutation pin: dropping the under-lock comparison writes the frame.
    """
    agent_mod, audited, open_episode = episode
    rt, _, proc = _make_runtime()
    rt._work_dir = str(tmp_path)
    if launch == "unbound":
        rt._agent = "kirocrew"
        rt._launch_unbound = True
    else:
        _main_spec_view(rt, agent_mod)
    entered = _entered(rt, monkeypatch)
    (result,) = await _while_a_co_tenant_holds_stdin(
        rt._stdin_write_lock(),
        entered,
        open_episode,
        lambda: rt._send_session_open(method, {"cwd": str(tmp_path)}, timeout=5.0),
    )
    assert isinstance(result, AcpRuntimeError) and "may not start" in str(result)
    proc.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]
    # Cleanup: no orphan future, no registration left for the unwritten id.
    assert dict(rt._pending_requests) == {}
    assert not rt._stdin_write_lock().locked()


@pytest.mark.asyncio
async def test_an_episode_opened_during_the_lock_wait_refuses_the_kas_open(
    episode, monkeypatch, tmp_path
):
    """KAS: the open is held to the reading its wire payload was built on.

    KAS records no launch binding, so only the payload reading
    (``SessionExtras.main_spec_episode``) can refuse it. Mutation pin: an open
    that is not handed that reading writes the frame.
    """
    agent_mod, audited, open_episode = episode
    rt, _, proc = _make_runtime()
    rt._work_dir = str(tmp_path)
    rt._acp_backend = "kas"
    built_on = agent_mod.main_spec_episode()
    entered = _entered(rt, monkeypatch)
    (result,) = await _while_a_co_tenant_holds_stdin(
        rt._stdin_write_lock(),
        entered,
        open_episode,
        lambda: rt._send_session_open(
            METHOD_SESSION_NEW,
            {"cwd": str(tmp_path)},
            timeout=5.0,
            built_on=built_on,
            built_for="helper",
        ),
    )
    assert isinstance(result, AcpRuntimeError) and "may not start" in str(result)
    proc.stdin.write.assert_not_called()
    assert audited == ["helper"]
    assert dict(rt._pending_requests) == {}


@pytest.mark.asyncio
async def test_an_episode_opened_during_the_lock_wait_refuses_a_by_name_set_mode(
    episode, monkeypatch, tmp_path
):
    """A switch kiro-cli resolves by name is held the same way, and leaves no route."""
    agent_mod, audited, open_episode = episode
    monkeypatch.setattr(agent_mod, "require_main_spec_projected", lambda *a: None)
    rt, _, proc = _make_runtime()
    rt._work_dir = str(tmp_path)
    rt._native_skill_projection = None
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    entered = _entered(rt, monkeypatch)
    (result,) = await _while_a_co_tenant_holds_stdin(
        rt._stdin_write_lock(), entered, open_episode, lambda: handle.set_mode("helper")
    )
    assert isinstance(result, AcpRuntimeError) and "may not start" in str(result)
    proc.stdin.write.assert_not_called()
    assert audited == ["helper"]
    assert dict(rt._routed_requests) == {}


def _direct_client(tmp_path) -> MagicMock:
    from kiro_crew.acp.client import AcpClient

    client = object.__new__(AcpClient)
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._agent = "kirocrew"
    client._work_dir = str(tmp_path)
    client._launch_unbound = True
    client._next_id = 1
    client._native_skill_projection = None
    return client


@pytest.mark.asyncio
async def test_an_episode_opened_during_the_lock_wait_refuses_the_direct_client_open(
    episode, monkeypatch, tmp_path
):
    """The direct client: one process, one session, the same rule on its own lock.

    Mutation pin: checking only on the shared runtime writes the frame here.
    """
    _agent_mod, audited, open_episode = episode
    client = _direct_client(tmp_path)
    entered = _entered(client, monkeypatch)
    (result,) = await _while_a_co_tenant_holds_stdin(
        client._stdin_write_lock(),
        entered,
        open_episode,
        lambda: client._send_request(METHOD_SESSION_NEW, {"cwd": str(tmp_path)}),
    )
    assert isinstance(result, AcpError) and "may not start" in str(result)
    client._process.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]
    assert not client._stdin_write_lock().locked()


@pytest.mark.asyncio
async def test_a_reading_that_moves_while_the_admission_holds_writes_the_frame_once(
    episode, monkeypatch, tmp_path
):
    """The healthy side: a view of another file stays admitted, so the retry writes.

    The episode opens during the wait, the comparison refuses the first write,
    the check runs again off the lock and admits, and the same frame goes out
    exactly once. No denial is recorded.
    """
    _agent_mod, audited, open_episode = episode
    other = tmp_path / "helper.json"
    other.write_text("{}", encoding="utf-8")
    rt, _, proc = _make_runtime()
    rt._work_dir = str(tmp_path)
    rt._agent = "helper"
    rt._launch_unbound = False
    rt._launch_view_source = other.as_posix()
    rt._launch_view_episode = _agent_mod.main_spec_episode()
    entered = _entered(rt, monkeypatch)
    checks = []
    check = rt._check_session_open

    async def _counted(*args):
        checks.append(args)
        await check(*args)

    monkeypatch.setattr(rt, "_check_session_open", _counted)
    sent = asyncio.ensure_future(
        _while_a_co_tenant_holds_stdin(
            rt._stdin_write_lock(),
            entered,
            open_episode,
            lambda: rt._send_session_open(METHOD_SESSION_NEW, {"cwd": "/w"}, timeout=5.0),
        )
    )
    assert await _written(proc, sent), await _settled(sent, what="the refused send")
    (future,) = rt._pending_requests.values()
    future.set_result({"sessionId": "s1"})
    (result,) = await await_until(sent, what="the admitted open")
    assert result == {"sessionId": "s1"}
    assert proc.stdin.write.call_count == 1
    assert len(checks) == 2
    assert audited == []


@pytest.mark.asyncio
async def test_a_healthy_host_writes_the_open_with_one_check(episode, monkeypatch, tmp_path):
    """No episode: the comparison holds, so the frame is written after one check."""
    agent_mod, audited, _open = episode
    rt, _, proc = _make_runtime()
    rt._work_dir = str(tmp_path)
    _main_spec_view(rt, agent_mod)
    task = asyncio.ensure_future(
        rt._send_session_open(METHOD_SESSION_NEW, {"cwd": "/w"}, timeout=5.0)
    )
    assert await _written(proc, task), await _settled(task, what="the refused send")
    (future,) = rt._pending_requests.values()
    future.set_result({"sessionId": "s1"})
    assert await await_until(task, what="the healthy open") == {"sessionId": "s1"}
    assert proc.stdin.write.call_count == 1
    assert audited == []


@pytest.mark.asyncio
async def test_an_open_whose_reading_keeps_moving_fails_after_the_bounded_retries(
    episode, monkeypatch, tmp_path
):
    """A reading that moves on every attempt ends the open; nothing is written."""
    agent_mod, audited, _open = episode
    rt, _, proc = _make_runtime()
    readings = iter(range(100))
    monkeypatch.setattr(frame_admission, "_read_episode", lambda: (next(readings), False))
    with pytest.raises(AcpRuntimeError, match="kept changing"):
        await rt._send_session_open(METHOD_SESSION_NEW, {"cwd": "/w"}, timeout=5.0)
    proc.stdin.write.assert_not_called()
    assert dict(rt._pending_requests) == {}
    # The same SEL denial as any other launch-bound refusal.
    assert audited == [rt._agent]


_ALIAS = "kc-view-0123456789abcdef01234567"


def _bracket_runtime(agent_mod, monkeypatch, tmp_path, *, projected: bool):
    """A runtime whose start bracket activates ``kirocrew`` with fakes for its file reads.

    *projected*: the native skill projection decides the agent, through a view
    of the main spec built while the host was healthy; otherwise there is no
    projection and the frame names the bare agent, which kiro-cli resolves.
    """
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr("kiro_crew.agent.require_fresh_derived_spec", lambda *a: None)
    monkeypatch.setattr("kiro_crew.agent.require_unchanged_derived_spec", lambda *a: None)
    rt, _, proc = _make_runtime()
    rt._work_dir = str(tmp_path)
    rt._agent = "helper"
    rt.terminate_session = AsyncMock()  # type: ignore[method-assign]
    view = None
    if projected:
        from kiro_crew.acp.skill_projection import NativeSkillProjection

        view = NativeSkillProjection(
            aliases={"kirocrew": _ALIAS},
            sources={"kirocrew": (agents / "kirocrew.json").as_posix()},
            episodes={"kirocrew": agent_mod.main_spec_episode()},
        )
    rt._native_skill_projection = view
    return rt, proc, view


def _activate(rt, view):
    async def run():
        with patch(
            "kiro_crew.acp.skill_projection.prepare_native_skill_projection", return_value=view
        ):
            return await rt._activate_mode_bracketed(
                "s1", "kirocrew", budget=5.0, payload_snapshot=None, wire_registered=False
            )

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("projected", "episode_now"),
    [(True, "open"), (True, "opened-and-closed"), (False, "open")],
    ids=["projected-view-open", "projected-view-closed", "by-name-open"],
)
async def test_an_episode_during_the_lock_wait_refuses_the_start_bracket_set_mode(
    episode, monkeypatch, tmp_path, projected, episode_now
):
    """Every ``set_mode`` the start bracket writes on kiro-cli is held to its admission.

    A view of the main spec is bound like a session open on that view: one
    built before an episode is refused while it is open and after a rebuild
    closes it. A bare name is bound by name, and resolves the rewritten spec
    once the episode closes, so only its open case refuses. Mutation pins: the projected frame
    sent with no admission, or the bracket holding none.
    """
    agent_mod, audited, open_episode = episode
    rt, proc, view = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=projected)

    def _episode() -> None:
        open_episode()
        if episode_now == "opened-and-closed":
            agent_mod._main_spec_unreadable = False

    entered = _entered(rt, monkeypatch)
    (result,) = await _while_a_co_tenant_holds_stdin(
        rt._stdin_write_lock(), entered, _episode, _activate(rt, view)
    )
    assert isinstance(result, AcpRuntimeError) and "may not start" in str(result), result
    proc.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]
    assert dict(rt._pending_requests) == {}
    rt.terminate_session.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_a_healthy_host_writes_the_projected_bracket_set_mode(episode, monkeypatch, tmp_path):
    """No episode: the view's admission holds, so its alias is written once."""
    agent_mod, audited, _open = episode
    rt, proc, view = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=True)
    task = asyncio.ensure_future(_activate(rt, view)())
    assert await _written(proc, task), await _settled(task, what="the refused bracket")
    assert _ALIAS in proc.stdin.write.call_args.args[0].decode()
    (future,) = rt._pending_requests.values()
    future.set_result({})
    assert await await_until(task, what="the healthy bracket") == _ALIAS
    assert proc.stdin.write.call_count == 1
    assert audited == []


@pytest.mark.asyncio
async def test_a_view_built_before_an_episode_that_closed_before_its_admission_is_refused(
    episode, monkeypatch, tmp_path
):
    """A view is bound to the reading it was built on, not the reading at its admission.

    The view of the main spec is built while the host is healthy, then an
    episode opens and a rebuild closes it before the bracket builds the
    attempt's admission (the awaits between the projection and the admit). The
    view still carries the pre-episode grants, so its frame is refused although
    no episode is open when the admission is taken. Mutation pin: binding the
    view to the reading at admission time admits it.
    """
    agent_mod, audited, open_episode = episode
    rt, proc, view = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=True)

    def _prepared(*_a, **_k):
        open_episode()
        agent_mod._main_spec_unreadable = False
        return view

    with patch("kiro_crew.acp.skill_projection.prepare_native_skill_projection", _prepared):
        with pytest.raises(AcpRuntimeError, match="may not start"):
            await await_until(
                asyncio.ensure_future(
                    rt._activate_mode_bracketed(
                        "s1", "kirocrew", budget=5.0, payload_snapshot=None, wire_registered=False
                    )
                ),
                what="the refused bracket",
            )
    proc.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]
    assert dict(rt._pending_requests) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("episode_open", [False, True], ids=["healthy", "episode-open"])
async def test_the_mode_admission_runs_the_start_gate_for_a_view_off_the_main_spec(
    episode, monkeypatch, tmp_path, episode_open
):
    """The mode admission folds the start gate in beside the view's launch bound.

    A view of the main agent's own name built from another file passes the
    launch bound, which binds a view to its file; the start gate still governs
    the main agent's names while the episode is open. Healthy, the same
    admission holds. Mutation pin: dropping the gate from the admission admits
    the episode case.
    """
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    agent_mod, audited, open_episode = episode
    rt, _proc, _view = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=False)
    view = NativeSkillProjection(
        aliases={"kirocrew": _ALIAS},
        sources={"kirocrew": (tmp_path / "elsewhere.json").as_posix()},
        episodes={"kirocrew": agent_mod.main_spec_episode()},
    )
    if not episode_open:
        admission = await rt._admit_mode_frame("kirocrew", view, _ALIAS)
        assert admission.agent == "kirocrew"
        assert audited == []
        return
    open_episode()
    with pytest.raises(AcpRuntimeError, match="may not start"):
        await rt._admit_mode_frame("kirocrew", view, _ALIAS)
    assert audited == ["kirocrew"]


@pytest.mark.asyncio
async def test_an_episode_opened_during_the_check_itself_refuses_the_frame(
    episode, monkeypatch, tmp_path
):
    """The reading is taken before the check, so an episode during the check moves it.

    The check decides, then the episode opens before it returns (its reads run
    off the loop). Mutation pin: a reading taken after the check equals the one
    under the lock, and the frame is written.
    """
    agent_mod, audited, open_episode = episode
    rt, _, proc = _make_runtime()
    rt._work_dir = str(tmp_path)
    _main_spec_view(rt, agent_mod)
    check = rt._check_session_open
    calls = []

    async def _then_open(*args):
        calls.append(args)
        await check(*args)
        if len(calls) == 1:
            open_episode()

    monkeypatch.setattr(rt, "_check_session_open", _then_open)
    task = asyncio.ensure_future(
        rt._send_session_open(METHOD_SESSION_NEW, {"cwd": "/w"}, timeout=5.0)
    )
    if await _written(proc, task):
        (future,) = rt._pending_requests.values()
        future.set_result({"sessionId": "s1"})
    (result,) = await _settled(task, what="the open")
    assert isinstance(result, AcpRuntimeError) and "may not start" in str(result), result
    proc.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]
    assert dict(rt._pending_requests) == {}


@pytest.mark.asyncio
async def test_an_admitted_frame_is_not_written_into_a_runtime_that_died_during_the_wait(
    episode, monkeypatch, tmp_path
):
    """The transport's own under-lock check runs before the admission's.

    A sibling's stall marks the runtime dead while the open waits for the lock.
    Mutation pin: an admitted frame that skips the dead check is written.
    """
    from kiro_crew.acp.runtime import AcpRuntimeDead

    agent_mod, audited, _open = episode
    rt, _, proc = _make_runtime()
    rt._work_dir = str(tmp_path)
    _main_spec_view(rt, agent_mod)
    entered = _entered(rt, monkeypatch)

    def _die() -> None:
        rt._dead = True

    (result,) = await _while_a_co_tenant_holds_stdin(
        rt._stdin_write_lock(),
        entered,
        _die,
        lambda: rt._send_session_open(METHOD_SESSION_NEW, {"cwd": "/w"}, timeout=5.0),
    )
    assert isinstance(result, AcpRuntimeDead), result
    proc.stdin.write.assert_not_called()
    assert dict(rt._pending_requests) == {}
    assert audited == []


@pytest.mark.asyncio
async def test_a_cancellation_during_the_recheck_leaves_no_pending_request(
    episode, monkeypatch, tmp_path
):
    """A start cancelled while its check runs again off the lock leaves no orphan future."""
    agent_mod, _audited, open_episode = episode
    other = tmp_path / "helper.json"
    other.write_text("{}", encoding="utf-8")
    rt, _, proc = _make_runtime()
    rt._work_dir = str(tmp_path)
    rt._agent = "helper"
    rt._launch_unbound = False
    rt._launch_view_source = other.as_posix()
    rt._launch_view_episode = agent_mod.main_spec_episode()
    entered = _entered(rt, monkeypatch)
    rechecking = asyncio.Event()
    never = asyncio.Event()
    check = rt._check_session_open

    async def _parked_recheck(*args):
        if entered.is_set():
            rechecking.set()
            await never.wait()
        await check(*args)

    monkeypatch.setattr(rt, "_check_session_open", _parked_recheck)
    lock = rt._stdin_write_lock()
    await lock.acquire()
    task = asyncio.ensure_future(
        rt._send_session_open(METHOD_SESSION_NEW, {"cwd": "/w"}, timeout=5.0)
    )
    try:
        await await_until(entered, what="the open reaching the writer")
        open_episode()
    finally:
        lock.release()
    await await_until(rechecking, what="the admission's re-check")
    task.cancel()
    (result,) = await _settled(task, what="the cancelled open")
    assert isinstance(result, asyncio.CancelledError)
    proc.stdin.write.assert_not_called()
    assert not lock.locked()
    assert dict(rt._pending_requests) == {}


@pytest.mark.asyncio
async def test_the_direct_client_launch_agent_set_mode_is_bound_like_its_open(
    episode, monkeypatch, tmp_path
):
    """On an unbound launch kiro-cli resolves ``set_mode``'s bare name as it does an open's.

    The shared runtime refuses that frame during an episode; the direct client
    refuses it the same way, and holds it to its admission under the lock.
    """
    _agent_mod, audited, open_episode = episode
    client = _direct_client(tmp_path)
    open_episode()
    with pytest.raises(AcpError, match="may not start"):
        await client._send_request(METHOD_SET_MODE, {"sessionId": "s1", "modeId": "kirocrew"})
    client._process.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]


@pytest.mark.asyncio
async def test_the_direct_client_set_mode_waiting_for_the_lock_is_refused(
    episode, monkeypatch, tmp_path
):
    """The direct client's ``set_mode`` passes its check, then the episode opens."""
    _agent_mod, audited, open_episode = episode
    client = _direct_client(tmp_path)
    entered = _entered(client, monkeypatch)
    (result,) = await _while_a_co_tenant_holds_stdin(
        client._stdin_write_lock(),
        entered,
        open_episode,
        lambda: client._send_request(METHOD_SET_MODE, {"sessionId": "s1", "modeId": "kirocrew"}),
    )
    assert isinstance(result, AcpError) and "may not start" in str(result), result
    client._process.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]


#: The runtime and session-handle methods that put a request frame on the wire.
_SEND_ATTRS = {
    "send_request",
    "send_notification",
    "send_request_for_answer",
    "_send_and_await",
    "_send_awaited",
}
_ADMITTED_NAMES = {"METHOD_SET_MODE", "METHOD_SESSION_NEW", "METHOD_SESSION_LOAD"}
#: The same methods as literal strings, held here so a narrowed production set
#: cannot also blind the ratchet.
_ADMITTED_LITERALS = {METHOD_SET_MODE, METHOD_SESSION_NEW, METHOD_SESSION_LOAD}


def _admitted_method(call: ast.Call) -> str | None:
    """The admitted method a frame-writer call sends, as written, or ``None``.

    The method is the first argument or the ``method=`` keyword: an admitted
    method's constant, its literal string, or the chokepoint's own ``method``
    passed to ``_send_and_await``.
    """
    assert isinstance(call.func, ast.Attribute)
    arg = call.args[0] if call.args else None
    if arg is None:
        arg = next((kw.value for kw in call.keywords if kw.arg == "method"), None)
    if isinstance(arg, ast.Name) and (
        arg.id in _ADMITTED_NAMES or (arg.id == "method" and call.func.attr == "_send_and_await")
    ):
        return arg.id
    if isinstance(arg, ast.Constant) and arg.value in _ADMITTED_LITERALS:
        return str(arg.value)
    return None


def _admitted_sends(tree: ast.AST) -> list[tuple[str, bool, bool]]:
    """Each send of an admitted method: ``(method as written, inside holding, wire branch)``.

    A send counts when its call names a frame writer (:data:`_SEND_ATTRS`) and
    sends an admitted method (:func:`_admitted_method`). *wire branch* is a
    send inside the body of ``if wire_registered:``.
    """
    found: list[tuple[str, bool, bool]] = []

    def visit(node: ast.AST, held: bool, wire: bool) -> None:
        if isinstance(node, (ast.With, ast.AsyncWith)):
            held = held or any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "holding"
                for item in node.items
            )
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "wire_registered"
        ):
            visit(node.test, held, wire)
            for child in node.body:
                visit(child, held, True)
            for child in node.orelse:
                visit(child, held, wire)
            return
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SEND_ATTRS
        ):
            method = _admitted_method(node)
            if method is not None:
                found.append((method, held, wire))
        for child in ast.iter_child_nodes(node):
            visit(child, held, wire)

    visit(tree, False, False)
    return found


def _holding_arguments(tree: ast.AST) -> list[tuple[ast.AST, list[ast.AST]]]:
    """Each ``holding(...)`` argument, with every value its name is bound to in its function."""
    out: list[tuple[ast.AST, list[ast.AST]]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(fn):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "holding"
            ):
                continue
            (arg,) = call.args
            bound: list[ast.AST] = []
            if isinstance(arg, ast.Name):
                for node in ast.walk(fn):
                    targets = (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else [node.target] if isinstance(node, ast.AnnAssign) else []
                    )
                    if any(isinstance(t, ast.Name) and t.id == arg.id for t in targets):
                        bound.append(node)
            out.append((arg, bound))
    return out


def _modules():
    from kiro_crew.acp import runtime, session_handle

    return {
        m.__name__: ast.parse(Path(m.__file__).read_text(encoding="utf-8"))
        for m in (runtime, session_handle)
    }


def test_every_admitted_send_is_held_to_its_admission():
    """Ratchet: each session open and ``set_mode`` send on the runtime is held.

    The shared runtime's session opens go through ``_send_session_open``, and
    its ``set_mode`` sends live in the mode bracket and the session handle.
    Every send of those methods, through any frame writer, runs inside
    ``with holding(...)``. The one exception is the bracket's KAS branch
    (``if wire_registered:``), which activates the payload the held open
    registered. Both runtime request writers read the held admission back
    (``_write_request_admitted``), so a new send site that skips the hold, or a
    writer that skips the read, is caught here.
    """
    trees = _modules()
    rt_tree = trees["kiro_crew.acp.runtime"]
    sh_tree = trees["kiro_crew.acp.session_handle"]
    rt_sends, sh_sends = _admitted_sends(rt_tree), _admitted_sends(sh_tree)
    assert [s for s in rt_sends + sh_sends if not s[1] and not s[2]] == []
    # The chokepoint's send, the bracket's held send and its KAS send.
    assert sorted(rt_sends) == [
        ("METHOD_SET_MODE", False, True),
        ("METHOD_SET_MODE", True, False),
        ("method", True, False),
    ]
    assert sh_sends == [("METHOD_SET_MODE", True, False)]

    writers = {
        fn.name: {
            n.func.attr
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        for fn in ast.walk(rt_tree)
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name in {"send_request", "_send_and_await"}
    }
    for name, calls in writers.items():
        assert "_write_request_admitted" in calls, name
        assert "_write_request_bounded" not in calls, name
    assert frame_admission.ADMITTED_METHODS >= {METHOD_SESSION_NEW, METHOD_SESSION_LOAD}


@pytest.mark.parametrize(
    "send",
    [
        'await rt._send_and_await("session/set_mode", {})',
        'await rt.send_request(method="session/set_mode", params={})',
        "await rt._send_and_await(method=METHOD_SET_MODE, params={})",
        'await h._runtime.send_request("session/new", {})',
    ],
    ids=["literal", "literal-keyword", "constant-keyword", "literal-open"],
)
def test_the_send_ratchet_sees_every_spelling_of_an_admitted_method(send):
    """The ratchet's detector finds an admitted send however its method is written.

    A literal method string or the ``method=`` keyword would otherwise add an
    unheld ``set_mode`` or open that the ratchet never sees.
    """
    tree = ast.parse(f"async def f(rt, h):\n    {send}\n")
    ((_method, held, wire),) = _admitted_sends(tree)
    assert (held, wire) == (False, False)


def test_holding_refuses_anything_but_an_admission_at_run_time():
    """``holding`` raises before its scope opens when handed no admission.

    The type hint and the AST ratchet keep ``None`` out of the code that
    exists; this keeps it out at run time, so no frame is sent inside a hold
    that holds nothing.
    """
    sent: list[str] = []
    for value in (None, object()):
        with pytest.raises(TypeError, match="FrameAdmission"):
            with frame_admission.holding(value):  # type: ignore[arg-type]
                sent.append("inside")
        assert frame_admission.held_admission(METHOD_SET_MODE) is None
    assert sent == []


@pytest.mark.asyncio
async def test_send_request_for_answer_refuses_a_held_set_mode(episode, tmp_path):
    """``send_request_for_answer`` reads no held admission, so it refuses ``set_mode``.

    Held to an admission whose reading then moved, a ``set_mode`` sent through
    it would be written unchecked; it is refused before any frame or pending
    request exists. Its one caller sends ``_session/steering``.
    """
    _agent_mod, audited, open_episode = episode
    rt, _, proc = _make_runtime()
    rt._work_dir = str(tmp_path)

    async def _admits() -> None:
        return None

    admission = await frame_admission.admit(_admits, agent="kirocrew")
    open_episode()
    with frame_admission.holding(admission):
        with pytest.raises(ValueError, match="holding"):
            await rt.send_request_for_answer(
                METHOD_SET_MODE, {"sessionId": "s1", "modeId": "kirocrew"}
            )
    proc.stdin.write.assert_not_called()
    assert dict(rt._pending_requests) == {}
    assert audited == []


def test_no_hold_is_ever_given_no_admission():
    """Ratchet: ``holding`` is only ever handed an admission a check just returned.

    Its argument is a name bound only to ``await admit(...)``,
    ``await admit_mode_frame(...)`` or ``await self._admit_mode_frame(...)``,
    never to ``None``, a conditional or
    anything computed, so a hold that holds nothing is caught here and not only
    by the race tests.
    """
    import inspect

    assert inspect.signature(frame_admission.holding).parameters["admission"].annotation in {
        "FrameAdmission",
        frame_admission.FrameAdmission,
    }
    seen = 0
    for name, tree in _modules().items():
        for arg, bound in _holding_arguments(tree):
            seen += 1
            assert isinstance(arg, ast.Name), (name, ast.unparse(arg))
            assert bound, (name, arg.id)
            for node in bound:
                assert isinstance(node, ast.Assign), (name, ast.unparse(node))
                value = node.value
                assert (
                    isinstance(value, ast.Await)
                    and isinstance(value.value, ast.Call)
                    and ast.unparse(value.value.func)
                    in {"admit", "admit_mode_frame", "self._admit_mode_frame"}
                ), (name, ast.unparse(node))
    assert seen == 3


def test_every_kiro_mode_frame_takes_its_admission_from_the_one_builder():
    """Ratchet: on kiro-cli a ``set_mode``'s hold is built by ``admit_mode_frame``.

    The start bracket's builder delegates to it, and the mid-session switch
    binds its held admission to it, so a switch the projection decides cannot
    go out on the main-spec check alone (the retained-view case). Only the
    switch's off-kiro branch and a decided agent with no view to name take the
    plain ``admit``: neither puts a view alias on the wire.
    """
    trees = _modules()
    rt_tree, sh_tree = trees["kiro_crew.acp.runtime"], trees["kiro_crew.acp.session_handle"]

    def _function(tree: ast.AST, name: str) -> ast.AST:
        (fn,) = [
            n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name
        ]
        return fn

    def _awaited(fn: ast.AST) -> list[str]:
        return [
            ast.unparse(n.value.func)
            for n in ast.walk(fn)
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
        ]

    assert "admit_mode_frame" in _awaited(_function(rt_tree, "_admit_mode_frame"))
    assert "admit" not in _awaited(_function(rt_tree, "_admit_mode_frame"))
    ((arg, bound),) = _holding_arguments(_function(sh_tree, "set_mode"))
    builders = sorted(ast.unparse(node.value.value.func) for node in bound)
    assert builders == ["admit", "admit", "admit_mode_frame"], builders
    builder = _function(sh_tree, "admit_mode_frame")
    bounds = [
        n
        for n in ast.walk(builder)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "require_main_spec_launch_bound"
    ]
    assert sorted(kw.arg for n in bounds for kw in n.keywords) == ["before", "view_source"]


def test_both_kas_opens_hand_the_open_their_payload_reading():
    """Ratchet: ``create_session`` and ``load_session`` pass KAS's payload reading itself.

    A keyword count alone passes ``built_on=None``, which reopens the KAS
    window with every race test green: the value is checked.
    """
    rt_tree = _modules()["kiro_crew.acp.runtime"]
    values = sorted(
        ast.unparse(kw.value)
        for n in ast.walk(rt_tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_send_session_open"
        for kw in n.keywords
        if kw.arg == "built_on"
    )
    assert values == ["kas_built_on", "kas_extras.main_spec_episode"]


def _switch_handle(rt) -> AcpSessionHandle:
    q = _register(rt, "sA")
    return AcpSessionHandle("sA", q["sA"], rt)


def _recover(agent_mod, open_episode) -> None:
    """An unreadable-spec episode opens and a later rebuild reads the spec again."""
    open_episode()
    agent_mod._main_spec_unreadable = False


@pytest.mark.asyncio
@pytest.mark.parametrize("episode_now", ["open", "recovered"])
async def test_a_switch_to_a_retained_main_spec_view_built_before_an_episode_is_refused(
    episode, monkeypatch, tmp_path, episode_now
):
    """A mid-session switch the projection decides is bound to the view it names.

    The projection still publishes a view of the main spec built while the
    host was healthy; an episode then opens (and, in the recovered case, a
    rebuild closes it). ``set_mode("kirocrew")`` would translate to that
    retained alias and reactivate grants no rebuild re-projected, so it is
    refused and nothing is written, exactly like the start bracket's frame.
    Mutation pin: skipping the launch bound for a decided agent writes the alias.
    """
    agent_mod, audited, open_episode = episode
    rt, proc, view = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=True)
    handle = _switch_handle(rt)
    if episode_now == "open":
        open_episode()
    else:
        _recover(agent_mod, open_episode)
    task = asyncio.ensure_future(handle.set_mode("kirocrew"))
    (result,) = await _settled(task, what="the refused switch")
    assert isinstance(result, AcpRuntimeError) and "may not start" in str(result), result
    proc.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]
    assert dict(rt._routed_requests) == {}


@pytest.mark.asyncio
async def test_a_switch_to_a_retained_view_is_refused_when_the_episode_opens_during_the_wait(
    episode, monkeypatch, tmp_path
):
    """The switch's admission holds under the stdin lock like every other mode frame."""
    agent_mod, audited, open_episode = episode
    rt, proc, view = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=True)
    handle = _switch_handle(rt)
    entered = _entered(rt, monkeypatch)
    (result,) = await _while_a_co_tenant_holds_stdin(
        rt._stdin_write_lock(),
        entered,
        lambda: _recover(agent_mod, open_episode),
        lambda: handle.set_mode("kirocrew"),
    )
    assert isinstance(result, AcpRuntimeError) and "may not start" in str(result), result
    proc.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]
    assert dict(rt._routed_requests) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["healthy", "rebuilt-after-recovery"])
async def test_a_switch_to_a_current_main_spec_view_writes_its_alias(
    episode, monkeypatch, tmp_path, history
):
    """A view built on the current reading switches normally, once.

    Healthy, or a fresh view the rebuild after recovery published: its alias
    goes out with one admission and no denial.
    """
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    agent_mod, audited, open_episode = episode
    rt, proc, _old = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=True)
    if history == "rebuilt-after-recovery":
        _recover(agent_mod, open_episode)
        rt._native_skill_projection = NativeSkillProjection(
            aliases={"kirocrew": _ALIAS},
            sources={"kirocrew": (agent_mod.KIRO_AGENTS_DIR / "kirocrew.json").as_posix()},
            episodes={"kirocrew": agent_mod.main_spec_episode()},
        )
    handle = _switch_handle(rt)
    await await_until(asyncio.ensure_future(handle.set_mode("kirocrew")), what="the switch")
    assert proc.stdin.write.call_count == 1
    assert _ALIAS in proc.stdin.write.call_args.args[0].decode()
    assert audited == []


@pytest.mark.asyncio
async def test_a_switch_admits_the_view_adopted_during_its_admission(
    episode, monkeypatch, tmp_path
):
    """A newer projection adopted while the check runs gets an admission of its own.

    The first admission names the retained pre-episode view; a rebuild after
    recovery adopts a fresh view during it. The switch re-admits on the fresh
    view and writes its alias, never the one the first admission named.
    """
    from kiro_crew.acp import session_handle as handle_mod
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    agent_mod, audited, open_episode = episode
    rt, proc, _old = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=True)
    fresh_alias = "kc-view-fedcba9876543210fedcba98"
    seen: list[str | None] = []
    real = handle_mod.admit_mode_frame

    async def _adopting(mode_agent, work_dir, projection, alias):
        seen.append(alias)
        if len(seen) == 1:
            _recover(agent_mod, open_episode)
            rt._native_skill_projection = NativeSkillProjection(
                aliases={"kirocrew": fresh_alias},
                sources={"kirocrew": (agent_mod.KIRO_AGENTS_DIR / "kirocrew.json").as_posix()},
                episodes={"kirocrew": agent_mod.main_spec_episode()},
            )
            return await real(mode_agent, work_dir, rt._native_skill_projection, fresh_alias)
        return await real(mode_agent, work_dir, projection, alias)

    monkeypatch.setattr(handle_mod, "admit_mode_frame", _adopting)
    handle = _switch_handle(rt)
    await await_until(asyncio.ensure_future(handle.set_mode("kirocrew")), what="the switch")
    assert seen == [_ALIAS, fresh_alias]
    assert proc.stdin.write.call_count == 1
    assert fresh_alias in proc.stdin.write.call_args.args[0].decode()
    assert audited == []


@pytest.mark.asyncio
async def test_a_switch_re_admits_a_view_swapped_in_place_during_its_admission(
    episode, monkeypatch, tmp_path
):
    """The re-check compares the alias, not only the projection object.

    The first admission names a fresh view built after recovery. During it the
    SAME projection object is changed in place to name the retained view built
    before the episode. The switch re-admits on that view and refuses it.
    Mutation pin: comparing only the projection's identity writes the retained alias.
    """
    from kiro_crew.acp import session_handle as handle_mod
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    agent_mod, audited, open_episode = episode
    rt, proc, _old = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=True)
    stale_reading = agent_mod.main_spec_episode()
    _recover(agent_mod, open_episode)
    fresh_alias = "kc-view-fedcba9876543210fedcba98"
    current = NativeSkillProjection(
        aliases={"kirocrew": fresh_alias},
        sources={"kirocrew": (agent_mod.KIRO_AGENTS_DIR / "kirocrew.json").as_posix()},
        episodes={"kirocrew": agent_mod.main_spec_episode()},
    )
    rt._native_skill_projection = current
    seen: list[str | None] = []
    real = handle_mod.admit_mode_frame

    async def _swapping(mode_agent, work_dir, projection, alias):
        seen.append(alias)
        admitted = await real(mode_agent, work_dir, projection, alias)
        if len(seen) == 1:
            current.aliases["kirocrew"] = _ALIAS
            current.episodes["kirocrew"] = stale_reading
        return admitted

    monkeypatch.setattr(handle_mod, "admit_mode_frame", _swapping)
    handle = _switch_handle(rt)
    task = asyncio.ensure_future(handle.set_mode("kirocrew"))
    (result,) = await _settled(task, what="the re-admitted switch")
    assert seen == [fresh_alias, _ALIAS]
    assert isinstance(result, AcpRuntimeError) and "may not start" in str(result), result
    proc.stdin.write.assert_not_called()
    assert dict(rt._routed_requests) == {}


@pytest.mark.asyncio
async def test_a_switch_whose_projection_keeps_changing_writes_nothing(
    episode, monkeypatch, tmp_path
):
    """A projection replaced after every admission ends the switch with no frame.

    Each pass adopts a new projection object, and every other one names the
    same alias as the pass before it. The loop gives up after its bounded
    attempts with "kept changing". That is projection churn, not a governance
    answer, so no SEL denial is recorded.
    Mutation pins: falling through on exhaustion writes the last admission's
    frame; comparing only the alias accepts a different projection that names it.
    """
    from kiro_crew.acp import session_handle as handle_mod
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    agent_mod, audited, _open = episode
    rt, proc, _old = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=True)
    other_alias = "kc-view-aaaaaaaaaaaaaaaaaaaaaaaa"
    real = handle_mod.admit_mode_frame
    passes = [0]

    def _view(alias: str) -> NativeSkillProjection:
        return NativeSkillProjection(
            aliases={"kirocrew": alias},
            sources={"kirocrew": (agent_mod.KIRO_AGENTS_DIR / "kirocrew.json").as_posix()},
            episodes={"kirocrew": agent_mod.main_spec_episode()},
        )

    async def _churning(mode_agent, work_dir, projection, alias):
        passes[0] += 1
        admitted = await real(mode_agent, work_dir, projection, alias)
        rt._native_skill_projection = _view(_ALIAS if passes[0] % 2 else other_alias)
        return admitted

    monkeypatch.setattr(handle_mod, "admit_mode_frame", _churning)
    handle = _switch_handle(rt)
    task = asyncio.ensure_future(handle.set_mode("kirocrew"))
    (result,) = await _settled(task, what="the churning switch")
    assert isinstance(result, AcpRuntimeError) and "kept changing" in str(result), result
    assert passes[0] == frame_admission.ADMISSION_ATTEMPTS
    proc.stdin.write.assert_not_called()
    assert audited == []
    assert dict(rt._routed_requests) == {}
