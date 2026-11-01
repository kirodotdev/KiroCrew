"""Workflow ``ctx`` session verbs: act as the launching session, never above it.

``ctx.open_session`` / ``ctx.send_to_session`` / ``ctx.set_session_mode`` reach the
session-control verbs through ``dashboard.workflow_session_port`` with the run's
launching session as the caller. Pinned here:

* a run with no launching session (system- or cron-started) is refused and opens
  nothing;
* a run whose captured memory mode is not ``persistent`` (an Incognito/Temporary
  run, or a rerun of one by a persistent session) cannot open or message a session;
* a rerun that replays earlier calls refuses ``open_session`` / ``send_to_session``
  instead of guessing what the prior run opened or delivered;
* ``set_session_mode`` can keep or lower a session's mode but never raise it above
  the launching session's own, and only on a session the launching session opened;
* every session a workflow opens carries the ``workflow-opened`` sidebar tag
  without using up the session's own first-message auto-tag pass;
* ``send_to_session`` reaches only sessions the launcher opened, even for an
  owner launcher;
* ``open_session`` and ``send_to_session`` spend the run's agent cap like
  ``ctx.agent()``;
* the happy path: a script opens a session and seeds it, and the seeded turn reads
  as sent by the launching session via ``workflow``.
"""

from __future__ import annotations

import asyncio
import functools

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.workflow_session_port import (
    WORKFLOW_OPENED_TAG,
    WorkflowSessionError,
    WorkflowSessionPort,
)
from kiro_crew.history import ConversationLog
from kiro_crew.workflow_memory import WorkflowScope
from kiro_crew.workflows import BudgetExceeded
from kiro_crew.workflows.runner import WorkflowRunner
from kiro_crew.workflows.service import WorkflowService

NOW = "2026-10-28T00:00:00Z"


@pytest.fixture
def setup(tmp_path):
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    launcher = _slot(state, "chat-1")
    return state, launcher, WorkflowSessionPort(state)


def _verb(port, verb, memory_mode="persistent"):
    """*verb* on *port*, bound to a run memory mode where the verb takes one."""
    fn = getattr(port, verb)
    if verb in ("open_session", "send_to_session"):
        return functools.partial(fn, memory_mode=memory_mode)
    return fn


def _tag_names(state, slot) -> set[str]:
    by_id = {t["id"]: t["name"] for t in state._tags}
    return {by_id.get(tag_id, "") for tag_id in slot.tags}


# ── no launching session ───────────────────────────────────────────────────


@pytest.mark.parametrize("caller", ["", "cron:job-1", "workflow-wf_7", "cron-job-1"])
def test_a_run_with_no_launching_session_is_refused_and_opens_nothing(setup, caller):
    state, _launcher, port = setup
    # A cron's own tab and a workflow result tab exist, so the refusal is about
    # their kind, not about a key that resolves to nothing.
    _slot(state, "workflow-wf_7")
    _slot(state, "cron-job-1")
    before = set(state._slots)
    for verb, kwargs in (
        ("open_session", {"name": "x"}),
        ("send_to_session", {"slot": "chat-1", "message": "hi"}),
        ("set_session_mode", {"slot": "chat-1", "mode": "normal"}),
    ):
        with pytest.raises(WorkflowSessionError) as exc:
            asyncio.run(_verb(port, verb)(run_id="wf_1", caller_session_key=caller, **kwargs))
        assert exc.value.code == "no_launching_session", verb
    assert set(state._slots) == before


# ── a mode can stay or drop, never rise ────────────────────────────────────


def _open_child(state, launcher, port) -> object:
    key = asyncio.run(
        _verb(port, "open_session")(run_id="wf_1", caller_session_key=_key(launcher), name="child")
    )
    return state.get_slot(key)


def test_a_workflow_cannot_raise_a_session_above_its_launching_session(setup):
    state, launcher, port = setup
    child = _open_child(state, launcher, port)
    assert child._trust is False and child._trust_reads is False
    for mode in ("trust_reads", "trust"):
        with pytest.raises(WorkflowSessionError) as exc:
            asyncio.run(
                port.set_session_mode(
                    run_id="wf_1", caller_session_key=_key(launcher), slot=child.key, mode=mode
                )
            )
        assert exc.value.code == "mode_raise_refused"
    assert child._trust is False and child._trust_reads is False


def test_the_ceiling_is_the_launching_sessions_posture_now(setup):
    state, launcher, port = setup
    launcher._trust_reads = True
    child = _open_child(state, launcher, port)
    with pytest.raises(WorkflowSessionError) as exc:
        asyncio.run(
            port.set_session_mode(
                run_id="wf_1", caller_session_key=_key(launcher), slot=child.key, mode="trust"
            )
        )
    assert exc.value.code == "mode_raise_refused"
    assert child._trust is False


def test_a_card_scoped_launcher_trust_sets_no_ceiling_above_normal(setup):
    # A Crew Mode card trust covers the launcher's own tools and does not carry to
    # what it starts (``approval_posture.trust_carries``), so it lets a workflow
    # hold no session above ``normal``.
    state, launcher, port = setup
    launcher._trust = True
    launcher._trust_card_scoped = True
    child = _open_child(state, launcher, port)
    assert child._trust is False  # create_session does not pass it on either
    child._trust_reads = True  # a person's own choice on the child
    for mode in ("trust_reads", "trust"):
        with pytest.raises(WorkflowSessionError) as exc:
            asyncio.run(
                port.set_session_mode(
                    run_id="wf_1", caller_session_key=_key(launcher), slot=child.key, mode=mode
                )
            )
        assert exc.value.code == "mode_raise_refused", mode
    assert child._trust is False and child._trust_reads is True


def test_a_workflow_may_keep_or_drop_a_sessions_mode(setup):
    state, launcher, port = setup
    launcher._trust = True
    child = _open_child(state, launcher, port)
    assert child._trust is True  # inherited from the launcher by create_session
    out = asyncio.run(
        port.set_session_mode(
            run_id="wf_1", caller_session_key=_key(launcher), slot=child.key, mode="trust_reads"
        )
    )
    assert out == {"ok": True, "mode": "trust_reads", "slot": child.key}
    assert child._trust is False and child._trust_reads is True
    asyncio.run(
        port.set_session_mode(
            run_id="wf_1", caller_session_key=_key(launcher), slot=child.key, mode="normal"
        )
    )
    assert child._trust is False and child._trust_reads is False
    # The launcher's own grant is untouched by a change to its child.
    assert launcher._trust is True


@pytest.mark.parametrize("mode", ["yolo", "auto", 3])
def test_a_mode_outside_the_three_is_refused(setup, mode):
    state, launcher, port = setup
    launcher._trust = True
    child = _open_child(state, launcher, port)
    with pytest.raises(WorkflowSessionError) as exc:
        asyncio.run(
            port.set_session_mode(
                run_id="wf_1", caller_session_key=_key(launcher), slot=child.key, mode=mode
            )
        )
    assert exc.value.code == "mode_not_allowed"


def test_set_mode_reaches_only_sessions_the_launcher_opened(setup):
    state, launcher, port = setup
    launcher._trust = True
    peer = _slot(state, "chat-2")
    with pytest.raises(WorkflowSessionError) as exc:
        asyncio.run(
            port.set_session_mode(
                run_id="wf_1", caller_session_key=_key(launcher), slot=peer.key, mode="normal"
            )
        )
    assert exc.value.code == "not_creator"
    with pytest.raises(WorkflowSessionError) as exc:
        asyncio.run(
            port.set_session_mode(
                run_id="wf_1", caller_session_key=_key(launcher), slot=launcher.key, mode="normal"
            )
        )
    assert exc.value.code == "self_target"
    assert launcher._trust is True


# ── the launcher's mode dropping while the change is in flight ─────────────


def _set_mode(port, launcher, child, mode):
    return asyncio.run(
        port.set_session_mode(
            run_id="wf_1", caller_session_key=_key(launcher), slot=child.key, mode=mode
        )
    )


def test_a_launcher_lowered_before_the_change_is_applied_is_honoured(setup, monkeypatch):
    """The ceiling is judged after the call's awaits, not only at its start."""
    state, launcher, port = setup
    launcher._trust = True
    child = _open_child(state, launcher, port)
    real_prewarm = sc.prewarm_enabled_check

    async def _operator_picks_normal():
        launcher._trust = False  # the person drops their own session mid-call
        await real_prewarm()

    monkeypatch.setattr(sc, "prewarm_enabled_check", _operator_picks_normal)
    with pytest.raises(WorkflowSessionError) as exc:
        _set_mode(port, launcher, child, "trust_reads")
    assert exc.value.code == "mode_raise_refused"
    assert child._trust is True and child._trust_reads is False  # untouched


def test_a_launcher_lowered_while_the_change_is_written_puts_it_back(setup, monkeypatch):
    """A drop inside the mode write itself is caught after it and undone."""
    from kiro_crew.dashboard import chat_handlers

    state, launcher, port = setup
    launcher._trust = True
    child = _open_child(state, launcher, port)
    real_apply = chat_handlers.apply_approval_mode
    calls: list[str] = []

    async def _apply(state_, *, mode, **kw):
        if not calls:
            launcher._trust = False  # lands while the write runs
        calls.append(mode)
        return await real_apply(state_, mode=mode, **kw)

    monkeypatch.setattr(chat_handlers, "apply_approval_mode", _apply)
    with pytest.raises(WorkflowSessionError) as exc:
        _set_mode(port, launcher, child, "trust_reads")
    assert exc.value.code == "mode_raise_refused"
    assert calls == ["trust_reads", "normal"]
    assert child._trust_reads is False and child._trust is False


# ── a session the person lowered stays lowered ─────────────────────────────


def test_a_workflow_cannot_raise_a_session_the_person_lowered(setup):
    """Below the launcher is still not a licence: the target's own mode is a ceiling.

    The launcher is trusted, so its child is born trusted; the person drops the
    child to Normal to answer a pending tool call themselves. Raising it back to
    Trust would also approve that pending call through the mode write's sweep.
    """
    state, launcher, port = setup
    launcher._trust = True
    child = _open_child(state, launcher, port)
    child._trust = False  # the person's own pick on the child
    loop = asyncio.new_event_loop()
    pending = loop.create_future()
    child._approval_futures["req-1"] = pending
    try:
        for mode in ("trust", "trust_reads"):
            with pytest.raises(WorkflowSessionError) as exc:
                _set_mode(port, launcher, child, mode)
            assert exc.value.code == "mode_raise_refused"
        assert child._trust is False and child._trust_reads is False
        assert not pending.done(), "the person's pending approval must stay theirs"
    finally:
        child._approval_futures.pop("req-1", None)
        loop.close()


def test_asking_for_the_mode_a_session_already_has_changes_nothing(setup, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    state, launcher, port = setup
    launcher._trust = True
    child = _open_child(state, launcher, port)
    called: list[str] = []

    async def _apply(*_a, mode, **_kw):
        called.append(mode)

    monkeypatch.setattr(chat_handlers, "apply_approval_mode", _apply)
    out = _set_mode(port, launcher, child, "trust")
    assert out == {"ok": True, "mode": "trust", "slot": child.key, "unchanged": True}
    assert called == [], "a no-op must not run the trust write and its approval sweep"


def test_a_session_holding_a_scoped_grant_is_left_alone(setup):
    state, launcher, port = setup
    launcher._trust = True
    child = _open_child(state, launcher, port)
    child._trust_scope = "scope-1"
    with pytest.raises(WorkflowSessionError) as exc:
        _set_mode(port, launcher, child, "normal")
    assert exc.value.code == "trust_scope_active"
    assert child._trust is True and child._trust_scope == "scope-1"


# ── the session-control scope gate ─────────────────────────────────────────


class _Execution:
    def __init__(self, store: str) -> None:
        self.store = type("Store", (), {"legacy_name": store})()


def test_a_scoped_launcher_the_surface_refuses_is_refused(setup, monkeypatch):
    """Same answer ``/api/session-control/*`` gives that caller: member_scope_denied."""
    state, launcher, port = setup
    monkeypatch.setattr(sc, "read_session_execution", lambda _key: _Execution("retired-store"))
    monkeypatch.setattr(sc, "member_admitted_to_scoped_surface", lambda _key, _store: False)
    _slot(state, "chat-2")
    before = set(state._slots)
    for verb, kwargs in (
        ("open_session", {"name": "x"}),
        ("send_to_session", {"slot": "chat-2", "message": "hi"}),
        ("set_session_mode", {"slot": "chat-2", "mode": "normal"}),
    ):
        with pytest.raises(WorkflowSessionError) as exc:
            asyncio.run(
                _verb(port, verb)(run_id="wf_1", caller_session_key=_key(launcher), **kwargs)
            )
        assert exc.value.code == "member_scope_denied", verb
    assert set(state._slots) == before


def test_an_admitted_member_launcher_stays_creator_fenced(setup, monkeypatch):
    """Admitted as a member, the launcher reaches only what it created."""
    state, launcher, port = setup
    monkeypatch.setattr(sc, "read_session_execution", lambda _key: _Execution("member-store"))
    monkeypatch.setattr(sc, "member_admitted_to_scoped_surface", lambda _key, _store: True)
    _slot(state, "chat-2")
    with pytest.raises(WorkflowSessionError) as exc:
        asyncio.run(
            _verb(port, "send_to_session")(
                run_id="wf_1", caller_session_key=_key(launcher), slot="chat-2", message="hi"
            )
        )
    assert exc.value.code == "not_creator"


def test_an_unreadable_launcher_identity_is_refused(setup, monkeypatch):
    state, launcher, port = setup

    def _unreadable(_key):
        raise OSError("record unreadable")

    monkeypatch.setattr(sc, "read_session_execution", _unreadable)
    with pytest.raises(WorkflowSessionError) as exc:
        asyncio.run(_verb(port, "open_session")(run_id="wf_1", caller_session_key=_key(launcher)))
    assert exc.value.code == "member_identity_unavailable"


# ── the sidebar label ──────────────────────────────────────────────────────


def test_an_opened_session_carries_the_workflow_opened_tag(setup):
    state, launcher, port = setup
    child = _open_child(state, launcher, port)
    assert WORKFLOW_OPENED_TAG in _tag_names(state, child)
    # The label does not stand in for the session's own first-message pass.
    assert getattr(child, "_auto_tagged", False) is False
    # A second opened session reuses the one tag definition.
    second = _open_child(state, launcher, port)
    assert WORKFLOW_OPENED_TAG in _tag_names(state, second)
    assert [t["name"] for t in state._tags].count(WORKFLOW_OPENED_TAG) == 1
    # The launcher's own row is not labelled.
    assert WORKFLOW_OPENED_TAG not in _tag_names(state, launcher)


# ── the agent cap ──────────────────────────────────────────────────────────


async def _echo(prompt: str, opts: dict):
    return f"echo:{prompt}"


def _open_script(n: int) -> str:
    return (
        'META = {"name": "o"}\n'
        "async def workflow(ctx):\n"
        "    keys = []\n"
        f"    for i in range({n}):\n"
        "        keys.append(await ctx.open_session('w' + str(i)))\n"
        "    return keys\n"
    )


def _stub_ports(opened: list):
    async def open_session(*, caller_session_key, name, folder_id, agent, model):
        opened.append((caller_session_key, name))
        return f"slot-{name}"

    async def unused(**_kw):
        return {}

    return {"open_session": open_session, "send_to_session": unused, "set_session_mode": unused}


def test_open_session_counts_against_the_runs_agent_cap():
    opened: list = []
    runner = WorkflowRunner(
        agent_fn=_echo,
        ports=_stub_ports(opened),
        audit=lambda *a, **k: None,
        max_agents_per_run=2,
    )
    res = asyncio.run(runner.run(_open_script(3), run_id="wf_c", now=NOW, session_key="chat-1"))
    assert not res.ok
    assert "agent-count cap" in (res.error or "")
    # The third open was refused before the port ran.
    assert opened == [("chat-1", "w0"), ("chat-1", "w1")]


def test_open_session_stops_at_the_budget_ceiling():
    from kiro_crew.workflows.context import AgentCounter, Budget
    from kiro_crew.workflows.events import EventStream
    from kiro_crew.workflows.runner import _RunContext

    opened: list = []
    budget = Budget(total=10)
    budget._spent = 10
    ctx = _RunContext(
        run_id="wf_b",
        args={},
        now=NOW,
        owner_dm="",
        stream=EventStream("wf_b"),
        budget=budget,
        counter=AgentCounter(5),
        agent_fn=_echo,
        concurrency=None,
        session_key="chat-1",
        ports=_stub_ports(opened),
    )
    with pytest.raises(BudgetExceeded):
        asyncio.run(ctx.open_session("w"))
    assert opened == []


def test_the_session_verbs_are_rejected_before_exec_when_unwired():
    runner = WorkflowRunner(agent_fn=_echo, ports={}, audit=lambda *a, **k: None)
    res = asyncio.run(runner.run(_open_script(1), run_id="wf_u", now=NOW, session_key="chat-1"))
    assert not res.ok
    assert "open_session" in (res.error or "")


# ── the run's captured memory mode ─────────────────────────────────────────


def _service_ports(port, memory_mode):
    """The session ports ``WorkflowService`` wires for a run captured in *memory_mode*."""
    service = WorkflowService(sessions=None, persist=False, session_port=port)
    scope = (
        None
        if memory_mode is None
        else WorkflowScope("wf_r", "memory", "", memory_mode=memory_mode)
    )
    return service._runner("wf_r", memory_scope=scope)._ports


@pytest.mark.parametrize("memory_mode", ["incognito", "temporary", "", None])
def test_a_restricted_run_cannot_open_or_message_a_session(setup, memory_mode):
    # A rerun of an Incognito/Temporary run keeps that run's mode but takes the
    # rerunning (persistent) session as its caller; the run's mode, not the
    # caller's, decides. An unknown or missing mode is refused too.
    state, launcher, port = setup
    child = _open_child(state, launcher, port)
    ports = _service_ports(port, memory_mode)
    before = set(state._slots)
    with pytest.raises(WorkflowSessionError) as exc:
        asyncio.run(ports["open_session"](caller_session_key=_key(launcher), name="x"))
    assert exc.value.code == "restricted_memory_mode"
    with pytest.raises(WorkflowSessionError) as exc:
        asyncio.run(
            ports["send_to_session"](
                caller_session_key=_key(launcher), slot=child.key, message="secret args"
            )
        )
    assert exc.value.code == "restricted_memory_mode"
    assert set(state._slots) == before


def test_a_persistent_run_is_wired_to_open_sessions(setup):
    state, launcher, port = setup
    ports = _service_ports(port, "persistent")
    key = asyncio.run(ports["open_session"](caller_session_key=_key(launcher), name="x"))
    assert state.get_slot(key) is not None


# ── a rerun's replayed prefix ──────────────────────────────────────────────

_OPEN_SEND_AGENTS = (
    'META = {"name": "r"}\n'
    "async def workflow(ctx):\n"
    "    slot = await ctx.open_session('item')\n"
    "    receipt = await ctx.send_to_session(slot, 'start on item')\n"
    "    a = await ctx.agent('first')\n"
    "    b = await ctx.agent('second')\n"
    "    return {'slot': slot, 'receipt': receipt, 'a': a, 'b': b}\n"
)


def _counting_ports():
    calls = {"open": 0, "send": []}

    async def open_session(*, caller_session_key, name, folder_id, agent, model):
        calls["open"] += 1
        return f"chat-{calls['open'] + 10}"

    async def send_to_session(*, caller_session_key, slot, message):
        calls["send"].append((slot, message))
        return {"ok": True, "slot": slot}

    async def unused(**_kw):
        raise AssertionError("not called")

    ports = {
        "open_session": open_session,
        "send_to_session": send_to_session,
        "set_session_mode": unused,
    }
    return ports, calls


def _run(ports, script=_OPEN_SEND_AGENTS, *, expect_ok=True, **kw):
    sink_fps: dict[int, str] = {}

    def sink(call_index, *, result, ok, error, fingerprint=""):
        if fingerprint:
            sink_fps[call_index] = fingerprint

    runner = WorkflowRunner(agent_fn=_echo, ports=ports, audit=lambda *a, **k: None)
    res = asyncio.run(runner.run(script, now=NOW, session_key="chat-1", on_agent_result=sink, **kw))
    if expect_ok:
        assert res.ok, res.error
    return res, sink_fps


def _rerun(ports, first, fingerprints, from_index, script=_OPEN_SEND_AGENTS, **kw):
    return _run(
        ports,
        script,
        run_id="wf_2",
        replay_results=first.agent_results,
        replay_before=from_index,
        replay_fingerprints=fingerprints,
        **kw,
    )[0]


_REFUSED = "refused on a rerun that replays earlier calls"


@pytest.mark.parametrize("from_index", [1, 2])
def test_a_prefix_rerun_refuses_to_open_or_send(from_index):
    ports, calls = _counting_ports()
    first, fingerprints = _run(ports, run_id="wf_1")
    assert calls["open"] == 1 and len(calls["send"]) == 1
    second = _rerun(ports, first, fingerprints, from_index, expect_ok=False)
    # Refused, not guessed: no second session and no second seed message.
    assert not second.ok
    assert _REFUSED in (second.error or "")
    assert calls["open"] == 1
    assert len(calls["send"]) == 1


_PARALLEL_SEND = (
    'META = {"name": "p"}\n'
    "async def a_side(ctx):\n"
    "    x = await ctx.agent('a-first')\n"
    "    y = await ctx.agent('a-second')\n"
    "    return [x, y]\n"
    "async def b_side(ctx):\n"
    "    z = await ctx.agent('b-first')\n"
    "    r = await ctx.send_to_session('chat-5', 'b is done')\n"
    "    return [z, r]\n"
    "async def workflow(ctx):\n"
    "    return await ctx.parallel([lambda: a_side(ctx), lambda: b_side(ctx)])\n"
)


def test_a_live_sibling_branch_does_not_make_a_replayed_branch_resend():
    ports, calls = _counting_ports()
    first, fingerprints = _run(ports, _PARALLEL_SEND, run_id="wf_1")
    assert len(calls["send"]) == 1
    # Calls 0 and 1 replay; the A branch's second call runs live.
    _rerun(ports, first, fingerprints, 2, _PARALLEL_SEND, expect_ok=False)
    assert len(calls["send"]) == 1


def test_a_send_the_prior_run_never_delivered_is_not_reported_as_sent():
    ports, calls = _counting_ports()
    delivered = ports["send_to_session"]

    async def refusing_send(**_kw):
        raise RuntimeError("send_to_session() failed: target_busy")

    ports["send_to_session"] = refusing_send
    first, fingerprints = _run(ports, run_id="wf_1", expect_ok=False)
    assert not first.ok and calls["open"] == 1
    ports["send_to_session"] = delivered
    second = _rerun(ports, first, fingerprints, 1, expect_ok=False)
    # The rerun says it cannot do this instead of claiming the seed went out.
    assert not second.ok
    assert _REFUSED in (second.error or "")
    assert calls["send"] == []


def test_a_full_rerun_opens_and_sends_live():
    ports, calls = _counting_ports()
    first, fingerprints = _run(ports, run_id="wf_1")
    _rerun(ports, first, fingerprints, 0)
    assert calls["open"] == 2
    assert len(calls["send"]) == 2


# ── the happy path, end to end through the runner ──────────────────────────


def test_a_workflow_opens_a_session_and_seeds_it_as_the_launcher(setup, monkeypatch):
    state, launcher, port = setup
    ran: dict[str, str] = {}

    async def _fake_run_chat(_state, slot, prompt):
        ran[slot.key] = prompt

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

    ports = {
        verb: (lambda fn: lambda **kw: fn(run_id="wf_h", **kw))(_verb(port, verb))
        for verb in ("open_session", "send_to_session", "set_session_mode")
    }
    runner = WorkflowRunner(agent_fn=_echo, ports=ports, audit=lambda *a, **k: None)
    script = (
        'META = {"name": "h"}\n'
        "async def workflow(ctx):\n"
        "    slot = await ctx.open_session('item 1')\n"
        "    receipt = await ctx.send_to_session(slot, 'start on item 1')\n"
        "    return {'slot': slot, 'ok': receipt.get('ok')}\n"
    )

    async def _drive():
        res = await runner.run(script, run_id="wf_h", now=NOW, session_key=_key(launcher))
        # flake-ok: two yields let the started turn task run, as the send tests do
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return res

    res = asyncio.run(_drive())
    assert res.ok, res.error
    child_key = res.result["slot"]
    assert res.result["ok"] is True
    child = state.get_slot(child_key)
    assert child is not None
    # Opened as the launcher: it is the creator, so the launcher can address it.
    assert child._created_by == launcher.key
    assert child.workspace == launcher.workspace
    assert WORKFLOW_OPENED_TAG in _tag_names(state, child)
    assert ran[child_key].startswith(f"[sent by session {launcher.key} via workflow]")
    assert "start on item 1" in ran[child_key]


def test_send_to_session_reaches_only_sessions_the_launcher_opened(setup, monkeypatch):
    """An owner launcher's run cannot start a turn in the person's own tab."""
    state, launcher, port = setup
    ran: dict[str, str] = {}

    async def _fake_run_chat(_state, slot, prompt):
        ran[slot.key] = prompt

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)
    peer = _slot(state, "chat-2")
    peer._trust = True
    with pytest.raises(WorkflowSessionError) as exc:
        asyncio.run(
            _verb(port, "send_to_session")(
                run_id="wf_1", caller_session_key=_key(launcher), slot=peer.key, message="x"
            )
        )
    assert exc.value.code == "not_creator"
    child = _open_child(state, launcher, port)

    async def _send_child():
        receipt = await _verb(port, "send_to_session")(
            run_id="wf_1", caller_session_key=_key(launcher), slot=child.key, message="go"
        )
        # flake-ok: two yields let the started turn task run, as the send tests do
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return receipt

    assert asyncio.run(_send_child())["ok"] is True
    assert set(ran) == {child.key}


def _send_script(n: int) -> str:
    return (
        'META = {"name": "s"}\n'
        "async def workflow(ctx):\n"
        f"    for i in range({n}):\n"
        "        await ctx.send_to_session('chat-5', 'turn ' + str(i))\n"
        "    return 'done'\n"
    )


def test_send_to_session_counts_against_the_runs_agent_cap():
    ports, calls = _counting_ports()
    runner = WorkflowRunner(
        agent_fn=_echo, ports=ports, audit=lambda *a, **k: None, max_agents_per_run=2
    )
    res = asyncio.run(runner.run(_send_script(3), run_id="wf_s", now=NOW, session_key="chat-1"))
    assert not res.ok
    assert "agent-count cap" in (res.error or "")
    # The third send was refused before the port ran: each send starts a turn.
    assert calls["send"] == [("chat-5", "turn 0"), ("chat-5", "turn 1")]


def test_send_to_session_stops_at_the_budget_ceiling():
    from kiro_crew.workflows.context import AgentCounter, Budget
    from kiro_crew.workflows.events import EventStream
    from kiro_crew.workflows.runner import _RunContext

    ports, calls = _counting_ports()
    budget = Budget(total=10)
    budget._spent = 10
    ctx = _RunContext(
        run_id="wf_b",
        args={},
        now=NOW,
        owner_dm="",
        stream=EventStream("wf_b"),
        budget=budget,
        counter=AgentCounter(5),
        agent_fn=_echo,
        concurrency=None,
        session_key="chat-1",
        ports=ports,
    )
    with pytest.raises(BudgetExceeded):
        asyncio.run(ctx.send_to_session("chat-5", "x"))
    assert calls["send"] == []


def test_send_to_session_keeps_the_session_control_fence(setup):
    """A workflow reaches no session its launcher could not reach with session_send."""
    state, launcher, port = setup
    # A workflow result tab is never a session-control target, for any caller.
    _slot(state, "workflow-wf_9")
    with pytest.raises(WorkflowSessionError) as exc:
        asyncio.run(
            _verb(port, "send_to_session")(
                run_id="wf_1", caller_session_key=_key(launcher), slot="workflow-wf_9", message="x"
            )
        )
    assert exc.value.code == "unattended_target"
