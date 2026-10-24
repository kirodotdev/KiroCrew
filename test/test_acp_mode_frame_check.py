"""Session opens and ``session/set_mode`` frames are checked once, right before the send.

kiro-cli reads an agent spec when its process starts and again only when the
file's content changes; a ``session/new`` or ``session/set_mode`` frame activates
an agent the process already holds. So the decision is made at the send: a view
of the main spec built before an unreadable-main-spec episode is refused, the
mid-session switch is checked on the ``modeId`` the send will carry, and a
refused switch writes one SEL denial.

Every wait here is bounded (``kiro_crew.testing.wait``).
"""

from __future__ import annotations

import ast
import asyncio
import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _make_runtime, _register

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpRuntimeError, AcpSessionHandle
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import METHOD_SESSION_NEW, METHOD_SET_MODE
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture
def episode(monkeypatch):
    """A healthy host whose episode the test opens; records every SEL denial.

    Once open, every bare name is refused: no file pins it.
    """
    import kiro_crew.agent as agent_mod

    audited: list[str] = []
    monkeypatch.setattr(agent_mod, "_main_spec_unreadable", False)
    monkeypatch.setattr(agent_mod, "_main_spec_episode_revision", 0)
    monkeypatch.setattr(agent_mod, "_main_spec_declared_name", None)
    monkeypatch.setattr(
        agent_mod, "_audit_main_spec_denial", lambda agent, **_: audited.append(agent)
    )
    monkeypatch.setattr(agent_mod, "_schedule_main_spec_early_retry", lambda: None)
    # A refused view recycles an idle runtime; the runtime here is a fake process.
    monkeypatch.setattr(AcpRuntime, "kill", AsyncMock())

    def _open() -> None:
        agent_mod._main_spec_episode_revision += 1
        agent_mod._main_spec_unreadable = True

    return agent_mod, audited, _open


async def _settled(task: asyncio.Future, *, what: str) -> list:
    """*task*'s outcome as ``[result or exception]``, under the lost-run bound."""
    try:
        return await await_until(asyncio.gather(task, return_exceptions=True), what=what)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


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
async def test_a_healthy_host_writes_the_open_once(episode, tmp_path):
    """No episode: the check at the send admits, and the frame is written once."""
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
@pytest.mark.parametrize("episode_now", ["open", "recovered"])
async def test_an_open_on_a_view_built_before_an_episode_is_refused_at_the_send(
    episode, tmp_path, episode_now
):
    """The session open's check refuses a pre-episode main-spec view; nothing is written."""
    agent_mod, audited, open_episode = episode
    rt, _, proc = _make_runtime()
    rt._work_dir = str(tmp_path)
    _main_spec_view(rt, agent_mod)
    open_episode()
    if episode_now == "recovered":
        agent_mod._main_spec_unreadable = False
    with pytest.raises(AcpRuntimeError, match="may not start"):
        await rt._send_session_open(METHOD_SESSION_NEW, {"cwd": "/w"}, timeout=5.0)
    proc.stdin.write.assert_not_called()
    assert dict(rt._pending_requests) == {}
    assert audited == ["kirocrew"]


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
async def test_the_start_bracket_set_mode_is_refused_at_its_send(
    episode, monkeypatch, tmp_path, projected, episode_now
):
    """Every ``set_mode`` the start bracket writes on kiro-cli is checked at its send.

    A view of the main spec is bound like a session open on that view: one
    built before an episode is refused while it is open and after a rebuild
    closes it. A bare name is bound by name, so only its open case refuses.
    Mutation pin: the projected frame sent with no check writes the alias.
    """
    agent_mod, audited, open_episode = episode
    rt, proc, view = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=projected)
    open_episode()
    if episode_now == "opened-and-closed":
        agent_mod._main_spec_unreadable = False
    task = asyncio.ensure_future(_activate(rt, view)())
    (result,) = await _settled(task, what="the refused bracket")
    assert isinstance(result, AcpRuntimeError) and "may not start" in str(result), result
    proc.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]
    assert dict(rt._pending_requests) == {}
    rt.terminate_session.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_a_healthy_host_writes_the_projected_bracket_set_mode(episode, monkeypatch, tmp_path):
    """No episode: the view's check admits, so its alias is written once."""
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
async def test_a_view_built_before_an_episode_that_closed_before_its_check_is_refused(
    episode, monkeypatch, tmp_path
):
    """A view is bound to the reading it was built on, not the reading at its check.

    The view of the main spec is built while the host is healthy, then an
    episode opens and a rebuild closes it before the bracket checks the
    attempt's frame. The view still carries the pre-episode grants, so its
    frame is refused although no episode is open at the check. Mutation pin:
    binding the view to the reading at check time admits it.
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
async def test_the_mode_check_runs_the_start_gate_for_a_view_off_the_main_spec(
    episode, monkeypatch, tmp_path, episode_open
):
    """The mode check folds the start gate in beside the view's launch bound.

    A view of the main agent's own name built from another file passes the
    launch bound, which binds a view to its file; the start gate still governs
    the main agent's names while the episode is open. Healthy, the same check
    admits. Mutation pin: dropping the gate from the check admits the episode case.
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
        await rt._check_mode_frame("kirocrew", view, _ALIAS)
        assert audited == []
        return
    open_episode()
    with pytest.raises(AcpRuntimeError, match="may not start"):
        await rt._check_mode_frame("kirocrew", view, _ALIAS)
    assert audited == ["kirocrew"]


def _app_launch_copy(rt, agent_mod) -> str:
    """Make *rt* an app agent's runtime launched on its launch copy; return the copy's name.

    The app's registered spec declares the bare name, as ``apps.agent_launch``
    leaves it, so the start gate resolves that name to the app's own file.
    """
    (agent_mod.KIRO_AGENTS_DIR / "myapp--reviewer.json").write_text(
        '{"name": "reviewer"}', encoding="utf-8"
    )
    rt._agent = "reviewer"
    rt._launch_agent = "kirocrew-app-launch-0000aaaa-1111bbbb"
    return rt._launch_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("episode_open", [False, True], ids=["healthy", "episode"])
async def test_a_launch_copy_frame_is_checked_by_name(episode, monkeypatch, tmp_path, episode_open):
    """The bracket's ``set_mode`` for an app agent names its launch copy, a file Crew wrote.

    It is checked as a bare name is, whatever the projection publishes: admitted
    on a healthy host and refused while an episode is open, as every start is.
    Another agent on the same runtime goes out by its bare name, and so does the
    same name on a runtime with no launch copy: both are refused in the episode.
    Mutation pin: admitting the copy's frame on its file admits it in the episode.
    """
    agent_mod, audited, open_episode = episode
    rt, _proc, _view = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=False)
    copy = _app_launch_copy(rt, agent_mod)
    if not episode_open:
        await rt._check_mode_frame("reviewer", None, None)
        await rt._check_mode_frame("reviewer", None, copy)
        assert audited == []
        return
    open_episode()
    for alias in (None, copy):
        with pytest.raises(AcpRuntimeError, match="may not start"):
            await rt._check_mode_frame("reviewer", None, alias)
    assert audited == ["reviewer", "reviewer"]
    if episode_open:
        (agent_mod.KIRO_AGENTS_DIR / "other.json").write_text('{"name": "other"}', encoding="utf-8")
        with pytest.raises(AcpRuntimeError, match="may not start"):
            await rt._check_mode_frame("other", None, None)
        assert audited == ["reviewer", "reviewer", "other"]
        rt._launch_agent = ""
        with pytest.raises(AcpRuntimeError, match="may not start"):
            await rt._check_mode_frame("reviewer", None, None)
        assert audited == ["reviewer", "reviewer", "other", "reviewer"]


@pytest.mark.asyncio
async def test_a_switch_back_to_the_launch_copy_is_refused_while_an_episode_is_open(
    episode, monkeypatch, tmp_path
):
    """A mid-session switch to the runtime's own app agent goes out as its launch copy.

    ``send_request`` names the copy, which is checked by name: written on a
    healthy host, refused with no frame written while an episode is open.
    Mutation pin: admitting the switch on the copy's file writes the frame.
    """
    agent_mod, audited, open_episode = episode
    rt, proc, _view = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=False)
    copy = _app_launch_copy(rt, agent_mod)
    handle = _switch_handle(rt)
    await await_until(asyncio.ensure_future(handle.set_mode("reviewer")), what="the switch")
    assert proc.stdin.write.call_count == 1
    assert copy in proc.stdin.write.call_args.args[0].decode()
    assert audited == []
    open_episode()
    with pytest.raises(AcpRuntimeError, match="may not start"):
        await await_until(
            asyncio.ensure_future(handle.set_mode("reviewer")), what="the refused switch"
        )
    assert proc.stdin.write.call_count == 1
    assert audited == ["reviewer"]


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
async def test_the_direct_client_launch_agent_set_mode_is_bound_like_its_open(episode, tmp_path):
    """On an unbound launch kiro-cli resolves ``set_mode``'s bare name as it does an open's.

    The shared runtime refuses that frame during an episode; the direct client
    refuses it the same way, at its send.
    """
    _agent_mod, audited, open_episode = episode
    client = _direct_client(tmp_path)
    open_episode()
    with pytest.raises(AcpError, match="may not start"):
        await client._send_request(METHOD_SET_MODE, {"sessionId": "s1", "modeId": "kirocrew"})
    client._process.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]


def _modules():
    """The runtime's and the session handle's source: each facade with its owner modules."""
    from kiro_crew.acp import runtime, session_handle

    def _parsed(facade, owners) -> ast.Module:
        body = []
        for name in (facade.__name__, *owners):
            path = Path(importlib.import_module(name).__file__)
            body.extend(ast.parse(path.read_text(encoding="utf-8")).body)
        return ast.Module(body=body, type_ignores=[])

    return {
        runtime.__name__: _parsed(runtime, runtime._RUNTIME_OWNERS),
        session_handle.__name__: _parsed(session_handle, session_handle._SESSION_HANDLE_OWNERS),
    }


def _function(tree: ast.AST, name: str) -> ast.AST:
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name]
    return fn


def _awaited(fn: ast.AST) -> list[str]:
    return [
        ast.unparse(n.value.func)
        for n in ast.walk(fn)
        if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
    ]


def test_every_kiro_mode_frame_is_checked_by_the_one_check():
    """Ratchet: on kiro-cli a ``set_mode`` frame is checked by ``check_mode_frame``.

    The start bracket's check delegates to it, and the mid-session switch runs
    it, so a switch the projection decides cannot go out on the main-spec check
    alone (the retained-view case). The one check binds a view by its source
    and the episode reading it was built against.
    """
    trees = _modules()
    rt_tree, sh_tree = trees["kiro_crew.acp.runtime"], trees["kiro_crew.acp.session_handle"]
    assert _awaited(_function(rt_tree, "_check_mode_frame")) == ["check_mode_frame"]
    assert "check_mode_frame" in _awaited(_function(sh_tree, "set_mode"))
    check = _function(sh_tree, "check_mode_frame")
    bounds = [
        n
        for n in ast.walk(check)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "require_main_spec_launch_bound"
    ]
    # The bare-name bound is bound by name; the view bound carries its source, the
    # episode reading recorded with it, and the switch's refusal kind.
    assert sorted(kw.arg for n in bounds for kw in n.keywords) == [
        "before",
        "superseded",
        "view_source",
    ]
    (kind,) = [kw.value for n in bounds for kw in n.keywords if kw.arg == "superseded"]
    # A switch recycles nothing, so a view refused after recovery names a new session.
    assert ast.literal_eval(kind) == "session"


def test_both_kas_opens_hand_the_open_their_payload_reading():
    """Ratchet: ``create_session`` and ``load_session`` pass KAS's payload reading itself.

    A keyword count alone passes ``built_on=None``, which skips the KAS
    payload's check: the value is checked.
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
@pytest.mark.parametrize("episode_now", ["healthy", "open"])
async def test_a_switch_to_a_refused_main_spec_view_writes_one_denial(
    episode, monkeypatch, tmp_path, episode_now
):
    """A switch the projection refuses (its view would be the main spec) is audited once.

    The projection built during an episode refuses ``kirocrew``. Healthy, the
    switch's checks admit the name and the send's own translation refuses it:
    one denial. With the episode open, the start gate refuses before the send:
    one denial. The switch reads the ``modeId`` with the projection's pure
    lookup, so no read audits. Mutation pin: reading it through the strict
    resolver writes a denial per read.
    """
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    agent_mod, audited, open_episode = episode
    rt, proc, _view = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=False)
    rt._native_skill_projection = NativeSkillProjection(
        aliases={},
        errors={"kirocrew": "read during an unreadable-main-spec episode"},
        main_spec_refused={"kirocrew"},
    )
    if episode_now == "open":
        open_episode()
    handle = _switch_handle(rt)
    task = asyncio.ensure_future(handle.set_mode("kirocrew"))
    (result,) = await _settled(task, what="the refused switch")
    assert isinstance(result, (AcpRuntimeError, ValueError)), result
    assert "may not start" in str(result), result
    proc.stdin.write.assert_not_called()
    assert audited == ["kirocrew"]
    assert dict(rt._routed_requests) == {}


def test_the_projections_mode_lookup_audits_nothing(episode):
    """``mode_id`` raises where ``request`` refuses, with no SEL denial; ``request`` writes one."""
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    _agent_mod, audited, _open = episode
    projection = NativeSkillProjection(
        aliases={},
        errors={"kirocrew": "read during an unreadable-main-spec episode"},
        main_spec_refused={"kirocrew"},
    )
    with pytest.raises(ValueError, match="unreadable-main-spec episode"):
        projection.mode_id("kirocrew")
    assert audited == []
    with pytest.raises(ValueError, match="may not start"):
        projection.request(METHOD_SET_MODE, {"sessionId": "s1", "modeId": "kirocrew"})
    assert audited == ["kirocrew"]


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["healthy", "rebuilt-after-recovery"])
async def test_a_switch_to_a_current_main_spec_view_writes_its_alias(
    episode, monkeypatch, tmp_path, history
):
    """A view built on the current reading switches normally, once.

    Healthy, or a fresh view the rebuild after recovery published: its alias
    goes out after one check and no denial.
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
async def test_a_switch_checks_the_view_adopted_during_its_check(episode, monkeypatch, tmp_path):
    """A newer projection adopted while the check runs gets a check of its own.

    The first check names the retained pre-episode view; a rebuild after
    recovery adopts a fresh view during it. The switch checks the fresh view
    and writes its alias, never the one the first check named. Mutation pin:
    keeping the first check writes after judging the wrong view.
    """
    from kiro_crew.acp import session_handle as handle_mod
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    agent_mod, audited, open_episode = episode
    rt, proc, _old = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=True)
    fresh_alias = "kc-view-fedcba9876543210fedcba98"
    seen: list[str | None] = []
    real = handle_mod.check_mode_frame

    async def _adopting(
        mode_agent,
        work_dir,
        projection,
        alias,
        *,
        bound_to=None,
    ):
        seen.append(alias)
        if len(seen) == 1:
            _recover(agent_mod, open_episode)
            rt._native_skill_projection = NativeSkillProjection(
                aliases={"kirocrew": fresh_alias},
                sources={"kirocrew": (agent_mod.KIRO_AGENTS_DIR / "kirocrew.json").as_posix()},
                episodes={"kirocrew": agent_mod.main_spec_episode()},
            )
            return await real(
                mode_agent, work_dir, rt._native_skill_projection, fresh_alias, bound_to=bound_to
            )
        return await real(mode_agent, work_dir, projection, alias, bound_to=bound_to)

    monkeypatch.setattr(handle_mod, "check_mode_frame", _adopting)
    handle = _switch_handle(rt)
    await await_until(asyncio.ensure_future(handle.set_mode("kirocrew")), what="the switch")
    assert seen == [_ALIAS, fresh_alias]
    assert proc.stdin.write.call_count == 1
    assert fresh_alias in proc.stdin.write.call_args.args[0].decode()
    assert audited == []


@pytest.mark.asyncio
async def test_a_switch_re_checks_a_view_swapped_in_place_during_its_check(
    episode, monkeypatch, tmp_path
):
    """The re-check compares the alias, not only the projection object.

    The first check names a fresh view built after recovery. During it the
    SAME projection object is changed in place to name the retained view built
    before the episode. The switch checks that view and refuses it.
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
    real = handle_mod.check_mode_frame

    async def _swapping(
        mode_agent,
        work_dir,
        projection,
        alias,
        *,
        bound_to=None,
    ):
        seen.append(alias)
        await real(mode_agent, work_dir, projection, alias, bound_to=bound_to)
        if len(seen) == 1:
            current.aliases["kirocrew"] = _ALIAS
            current.episodes["kirocrew"] = stale_reading

    monkeypatch.setattr(handle_mod, "check_mode_frame", _swapping)
    handle = _switch_handle(rt)
    task = asyncio.ensure_future(handle.set_mode("kirocrew"))
    (result,) = await _settled(task, what="the re-checked switch")
    assert seen == [fresh_alias, _ALIAS]
    assert isinstance(result, AcpRuntimeError) and "may not start" in str(result), result
    proc.stdin.write.assert_not_called()
    assert dict(rt._routed_requests) == {}


@pytest.mark.asyncio
async def test_a_switch_whose_projection_keeps_changing_writes_nothing(
    episode, monkeypatch, tmp_path
):
    """A projection replaced after every check ends the switch with no frame.

    Each pass adopts a new projection object, and every other one names the
    same alias as the pass before it. The loop gives up after its bounded
    attempts with "kept changing". That is projection churn, not a governance
    answer, so no SEL denial is recorded.
    Mutation pins: falling through on exhaustion writes the last check's frame;
    comparing only the alias accepts a different projection that names it.
    """
    from kiro_crew.acp import session_handle as handle_mod
    from kiro_crew.acp import session_handle_model_selection as model_selection
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    agent_mod, audited, _open = episode
    rt, proc, _old = _bracket_runtime(agent_mod, monkeypatch, tmp_path, projected=True)
    other_alias = "kc-view-aaaaaaaaaaaaaaaaaaaaaaaa"
    real = handle_mod.check_mode_frame
    passes = [0]

    def _view(alias: str) -> NativeSkillProjection:
        return NativeSkillProjection(
            aliases={"kirocrew": alias},
            sources={"kirocrew": (agent_mod.KIRO_AGENTS_DIR / "kirocrew.json").as_posix()},
            episodes={"kirocrew": agent_mod.main_spec_episode()},
        )

    async def _churning(
        mode_agent,
        work_dir,
        projection,
        alias,
        *,
        bound_to=None,
    ):
        passes[0] += 1
        await real(mode_agent, work_dir, projection, alias, bound_to=bound_to)
        rt._native_skill_projection = _view(_ALIAS if passes[0] % 2 else other_alias)

    monkeypatch.setattr(handle_mod, "check_mode_frame", _churning)
    handle = _switch_handle(rt)
    task = asyncio.ensure_future(handle.set_mode("kirocrew"))
    (result,) = await _settled(task, what="the churning switch")
    assert isinstance(result, AcpRuntimeError) and "kept changing" in str(result), result
    assert passes[0] == model_selection._SWITCH_CHECK_ATTEMPTS
    proc.stdin.write.assert_not_called()
    assert audited == []
    assert dict(rt._routed_requests) == {}
