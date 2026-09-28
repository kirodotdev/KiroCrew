"""A workflow ``ctx.agent()`` step answers the same two spawn gates every other
spawn entrypoint answers to.

A ``ctx.agent()`` step allocates a child agent session, so it is a spawn. The
subagent admission gate refuses a spawn whose ``capabilities.spawn`` ceiling says
no, and refuses a ``cwd`` outside ``agent.subagent_cwd_allowed_roots``; both
workflow ``agent_fn`` builders run the same two helpers through
``agent_exec.vet_step_spawn``.

Read the controls first. Tests 1-4 are positive: they pin that an allowed agent
in an allowed directory under a permissive ceiling still spawns, with the same
``get_or_create`` keyword arguments and the same warm-reuse behaviour as before
the gates existed. Only then do tests 5-7 assert the refusals, so an empty
allocation record there means REFUSED rather than a broken harness.

No provider is constructed and no process is spawned: ``stream_and_collect`` is
replaced by a local echo and ``SessionManager`` is a recording double. The only
path written is pytest's ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from types import SimpleNamespace

import pytest
from overload_fakes import settle_store_writes

import kiro_crew.platform.governance_profiles as governance_profiles
import kiro_crew.workflows.agent_exec as agent_exec
import kiro_crew.workflows.agent_pool as agent_pool
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.taskq.dependency import classify_exception
from kiro_crew.workflows.agent_exec import WorkflowSpawnRefused, build_agent_fn
from kiro_crew.workflows.agent_pool import build_pooled_agent_fn

pytestmark = pytest.mark.asyncio

#: The run's originating surface — the parent whose ceiling the gate resolves.
PARENT_SESSION_KEY = "dashboard:chat-wf-gate"
TARGET_AGENT = "agent-sde"
OTHER_AGENT = "agent-reviewer"


class _FakeProvider:
    def __init__(self, key: str) -> None:
        self.key = key

    async def new_conversation(self) -> None:
        return None

    def is_process_alive(self) -> bool:
        return True


class _RecordingSessions:
    """SessionManager double recording every child-session allocation request."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.created: list[dict[str, object]] = []

    async def get_or_create(self, key, *, agent=None, model=None, cwd=None, extra_env=None, **kw):
        self.created.append(
            {
                "via": self.label,
                "key": key,
                "agent": agent,
                "model": model,
                "cwd": cwd,
                "extra_env": extra_env,
            }
        )
        return _FakeProvider(key), True, False

    def release(self, key, *, cleanup=False):
        return None

    async def destroy(self, key):
        return None

    async def reset(self, key, **kw):
        return None

    async def end_children_for(self, key):
        return None


class _Decision:
    def __init__(self, permitted: bool, reason: str = "") -> None:
        self.permitted = permitted
        self.reason = reason


@pytest.fixture(autouse=True)
def _no_model_call(monkeypatch):
    """Replace the model call on both step paths; the spawn decision is the subject."""

    async def _echo(provider, message, **kw):
        return f"reply[{provider.key}]"

    monkeypatch.setattr(agent_exec, "stream_and_collect", _echo)
    monkeypatch.setattr(agent_pool, "stream_and_collect", _echo)


def _permit_all(monkeypatch) -> None:
    monkeypatch.setattr(
        governance_profiles,
        "governance_permits",
        lambda scope, item="", **kw: _Decision(True),
    )


def _deny_spawn(monkeypatch) -> None:
    def _permits(scope, item="", **kw):
        if scope == "capabilities.spawn":
            return _Decision(False, "spawn capability disabled by policy")
        return _Decision(True)

    monkeypatch.setattr(governance_profiles, "governance_permits", _permits)


def _deny_one_agent(monkeypatch, agent: str) -> None:
    """Spawning is enabled, but the ``agents`` scope excludes ``agent``."""

    def _permits(scope, item="", **kw):
        if scope == "capabilities.spawn" and item == f"agents:{agent}":
            return _Decision(False, "agent not in spawn scope")
        return _Decision(True)

    monkeypatch.setattr(governance_profiles, "governance_permits", _permits)


def _deny_one_app(monkeypatch, app: str) -> list[str]:
    """Spawning is enabled except under ``app``'s own profile; records every app asked.

    ``_vet_spawn_governance`` forwards ``app`` to ``governance_permits``, which is
    where ``resolve_active_scope`` binds the app profile (precedence #1). Recording
    the value is how the test tells "threaded" from "defaulted to empty".
    """
    seen: list[str] = []

    def _permits(scope, item="", **kw):
        asked = str(kw.get("app", ""))
        seen.append(asked)
        if scope == "capabilities.spawn" and asked == app:
            return _Decision(False, f"app {app!r} profile denies spawning")
        return _Decision(True)

    monkeypatch.setattr(governance_profiles, "governance_permits", _permits)
    return seen


def _allow_cwd_roots(monkeypatch, roots: list[str]) -> None:
    """Point the cwd allowlist at ``roots`` (empty list = overrides disabled)."""
    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(
                agent=SimpleNamespace(subagent_cwd_allowed_roots=list(roots))
            )
        ),
    )


async def _drive_both_step_paths(tag: str, opts: dict, *, app: str = "") -> list[dict[str, object]]:
    """Run one ``ctx.agent()`` step through BOTH production builders.

    The pooled builder is driven through its ``session=`` leg, which allocates
    with ``get_or_create`` directly and starts no warm worker thread (the warm
    leg has its own test below). Only ``WorkflowSpawnRefused`` is swallowed: a
    refusal is the behaviour under test, while any other exception is a broken
    harness and must surface.
    """
    cold = _RecordingSessions("build_agent_fn")
    cold_fn = build_agent_fn(cold, run_id=f"wf-{tag}-cold", session_key=PARENT_SESSION_KEY, app=app)
    with contextlib.suppress(WorkflowSpawnRefused):
        await cold_fn("do work", dict(opts))

    pooled = _RecordingSessions("build_pooled_agent_fn")
    pooled_fn, pool = build_pooled_agent_fn(
        pooled, run_id=f"wf-{tag}-pooled", session_key=PARENT_SESSION_KEY, app=app
    )
    try:
        with contextlib.suppress(WorkflowSpawnRefused):
            await pooled_fn("do work", {**opts, "session": f"named-{tag}"})
    finally:
        await pool.shutdown()
    return cold.created + pooled.created


# --------------------------------------------------------------------------- #
# 1-4: positive controls — an allowed step still spawns, unchanged.
# --------------------------------------------------------------------------- #


async def test_control_a_permitted_step_spawns_with_unchanged_kwargs(monkeypatch) -> None:
    """No overrides: both builders allocate, with today's get_or_create kwargs."""
    _permit_all(monkeypatch)
    spawned = await _drive_both_step_paths("control", {})

    assert sorted(str(row["via"]) for row in spawned) == [
        "build_agent_fn",
        "build_pooled_agent_fn",
    ], f"a permitted step did not allocate on both paths: {spawned!r}"
    for row in spawned:
        assert row["agent"] is None, row
        assert row["model"] is None, row
        assert row["cwd"] is None, row
        assert row["extra_env"] is None, row


async def test_control_a_permitted_named_agent_threads_through(monkeypatch) -> None:
    """``ctx.agent(agent=…)`` still reaches the child-session request."""
    _permit_all(monkeypatch)
    spawned = await _drive_both_step_paths("named", {"agent": TARGET_AGENT})

    assert len(spawned) == 2, spawned
    assert [row["agent"] for row in spawned] == [TARGET_AGENT, TARGET_AGENT], spawned


async def test_control_a_cwd_inside_an_allowed_root_is_resolved_and_used(
    monkeypatch, tmp_path
) -> None:
    """An allowlisted cwd still spawns, and the REALPATH is what launches.

    The path is spelled with a trailing ``.`` segment so the assertion also pins
    that the resolved form (what the admission gate stores) is what reaches the
    session, not the caller's spelling.
    """
    _permit_all(monkeypatch)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _allow_cwd_roots(monkeypatch, [os.path.realpath(str(tmp_path))])

    spelled = os.path.join(str(work_dir), ".")
    spawned = await _drive_both_step_paths("cwd-ok", {"cwd": spelled})

    assert len(spawned) == 2, spawned
    expected = os.path.realpath(str(work_dir))
    assert [row["cwd"] for row in spawned] == [expected, expected], spawned


async def test_control_the_warm_pool_still_reuses_one_worker_per_identity(
    monkeypatch, tmp_path
) -> None:
    """The pooled (warm) leg keeps its cold-start count, keyed on the resolved cwd."""
    _permit_all(monkeypatch)
    work_dir = tmp_path / "warm"
    work_dir.mkdir()
    _allow_cwd_roots(monkeypatch, [os.path.realpath(str(tmp_path))])

    sessions = _RecordingSessions("warm")
    agent_fn, pool = build_pooled_agent_fn(
        sessions, run_id="wf-warm", session_key=PARENT_SESSION_KEY, max_workers=2
    )
    try:
        first = await agent_fn("one", {"cwd": str(work_dir)})
        second = await agent_fn("two", {"cwd": str(work_dir)})
    finally:
        await pool.shutdown()

    assert first and second
    # Sequential calls on one identity: ONE cold start, reused warm afterwards.
    assert len(sessions.created) == 1, sessions.created
    assert sessions.created[0]["cwd"] == os.path.realpath(str(work_dir)), sessions.created


# --------------------------------------------------------------------------- #
# 5-7: the refusals (F34 governance ceiling + agent scope, F35 cwd allowlist).
# --------------------------------------------------------------------------- #


async def test_a_denied_spawn_ceiling_refuses_both_workflow_paths(monkeypatch) -> None:
    """F34: ``capabilities.spawn`` denied → no child session on either path."""
    _deny_spawn(monkeypatch)
    spawned = await _drive_both_step_paths("denied", {"agent": TARGET_AGENT})

    assert not spawned, (
        f"the workflow agent step allocated {spawned!r} while the "
        "capabilities.spawn ceiling refused the same (parent, agent) pair"
    )


async def test_the_refusal_is_raised_so_the_step_fails(monkeypatch) -> None:
    """The refusal surfaces as an exception the runner records per call."""
    _deny_spawn(monkeypatch)
    sessions = _RecordingSessions("raise")
    fn = build_agent_fn(sessions, run_id="wf-raise", session_key=PARENT_SESSION_KEY)

    with pytest.raises(WorkflowSpawnRefused) as caught:
        await fn("do work", {"agent": TARGET_AGENT})

    assert "spawn refused by governance" in str(caught.value)
    assert not sessions.created


async def test_the_agent_scope_refuses_only_the_agent_outside_it(monkeypatch) -> None:
    """F34, second half: an ``agents`` scope bounds WHICH agent a surface spawns."""
    _deny_one_agent(monkeypatch, TARGET_AGENT)

    refused = await _drive_both_step_paths("scoped-out", {"agent": TARGET_AGENT})
    assert not refused, refused

    permitted = await _drive_both_step_paths("scoped-in", {"agent": OTHER_AGENT})
    assert len(permitted) == 2, permitted
    assert [row["agent"] for row in permitted] == [OTHER_AGENT, OTHER_AGENT], permitted


async def test_an_app_bound_profile_refuses_the_app_s_own_workflow_spawn(monkeypatch) -> None:
    """F1 parity: the app identity reaches the ceiling, so an app profile binds.

    ``resolve_active_scope`` binds an app's profile on the ``app`` argument alone.
    A workflow that did not thread it was judged only by the policy ceiling and the
    surface profile, so an app whose own profile forbids spawning could still start
    a child agent from a workflow step.
    """
    seen = _deny_one_app(monkeypatch, "auto_research")

    refused = await _drive_both_step_paths("app-denied", {}, app="auto_research")
    assert not refused, (
        "the workflow agent step allocated a child session although the calling "
        f"app's own profile denies spawning: {refused!r}"
    )
    assert "auto_research" in seen, (
        "the app identity never reached governance_permits, so the refusal above "
        f"came from something other than the app profile: {seen!r}"
    )


async def test_a_run_with_no_app_identity_asks_with_an_empty_app(monkeypatch) -> None:
    """The no-app case is unchanged: the same empty string a non-app spawn passes."""
    seen = _deny_one_app(monkeypatch, "auto_research")

    spawned = await _drive_both_step_paths("app-absent", {})
    assert len(spawned) == 2, spawned
    assert seen and set(seen) == {""}, (
        f"a run with no app identity asked about {seen!r}, not the empty app a "
        "non-app spawn passes"
    )


async def test_the_service_hands_the_builders_the_run_s_app_identity(monkeypatch) -> None:
    """The identity is derived where it lives: the run scope's execution context.

    The two tests above pass ``app`` to a builder directly, which pins the gate.
    This one pins the WIRING -- without it the gate would be correct and always
    asked about an empty app, because nothing would populate the argument.
    """
    from kiro_crew.workflows import service as service_mod

    captured: dict[str, object] = {}

    def _spy(sessions, **kw):
        captured.update(kw)

        async def _never_called(prompt, opts):  # pragma: no cover - not run here
            raise AssertionError("the runner was not started by this test")

        return _never_called

    monkeypatch.setattr(service_mod, "build_agent_fn", _spy)
    svc = service_mod.WorkflowService(
        sessions=_RecordingSessions("service"), persist=False, pool_agents=False
    )
    scope = SimpleNamespace(
        execution_context=SimpleNamespace(app="auto_research"),
        memory_mode="persistent",
        validate=None,
        origin=PARENT_SESSION_KEY,
    )
    svc._runner("run-app", memory_scope=scope, session_key=PARENT_SESSION_KEY)
    assert captured.get("app") == "auto_research", (
        "the service did not hand the run's app identity to the agent_fn builder, "
        f"so the app-bound half of the spawn ceiling can never bind: {captured!r}"
    )

    captured.clear()
    plain = SimpleNamespace(
        execution_context=None, memory_mode="persistent", validate=None, origin=""
    )
    svc._runner("run-plain", memory_scope=plain, session_key=PARENT_SESSION_KEY)
    assert captured.get("app") == "", captured


async def test_a_cwd_outside_every_allowed_root_is_refused(monkeypatch, tmp_path) -> None:
    """F35: a caller-named launch directory outside the allowlist never launches."""
    _permit_all(monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    _allow_cwd_roots(monkeypatch, [os.path.realpath(str(allowed))])

    spawned = await _drive_both_step_paths("cwd-outside", {"cwd": str(outside)})
    assert not spawned, (
        f"the workflow agent step launched in {str(outside)!r}, which is under no "
        f"allowed root: {spawned!r}"
    )


async def test_an_empty_allowlist_disables_cwd_overrides(monkeypatch, tmp_path) -> None:
    """F35: an operator who empties the allowlist disables the override entirely."""
    _permit_all(monkeypatch)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _allow_cwd_roots(monkeypatch, [])

    spawned = await _drive_both_step_paths("cwd-disabled", {"cwd": str(work_dir)})
    assert not spawned, spawned

    # A step that names NO cwd is untouched by the allowlist and still spawns.
    still_works = await _drive_both_step_paths("cwd-none", {})
    assert len(still_works) == 2, still_works


# --------------------------------------------------------------------------- #
# 8: the refusal is terminal, never a retryable dependency.
# --------------------------------------------------------------------------- #


async def test_a_spawn_refusal_is_not_a_retryable_dependency() -> None:
    """``admitted_agent_fn`` must fail the row, not park it for a re-run.

    ``admitted_agent_fn`` asks ``classify_exception`` whether a step's exception
    is a dependency outage worth waiting on. A policy refusal is not: if an
    adapter recognised it, a refused step would be re-run up to the dependency
    attempt ceiling and the row would sit in ``waiting_dependency`` forever.
    """
    for message in (
        "spawn refused by governance: spawn capability disabled by policy",
        "spawn refused: cwd override is disabled (subagent_cwd_allowed_roots is empty)",
    ):
        assert classify_exception(WorkflowSpawnRefused(message)) is None, message


async def test_a_refusal_whose_text_looks_like_an_outage_still_fails_the_row(tmp_path) -> None:
    """The refusal is settled TERMINALLY without asking the dependency adapters.

    ``validate_cwd``'s reason names the configured roots, so the refusal text
    carries operator-supplied strings. The generic HTTP adapter reads a status
    token out of any exception's text, so a root spelled ``/srv/status 503`` makes
    a refusal look like a retryable outage: the row would park in
    ``waiting_dependency`` and rejected work would be re-run instead of ending.
    """
    import kiro_crew.taskq.model as m
    from kiro_crew.taskq.adapters.runner import (
        RunnerAdmission,
        RunnerLane,
        workflow_task_id,
    )
    from kiro_crew.taskq.store import TaskStore
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    # Precondition: this text IS classified as retryable, so the guard below is
    # what keeps the row terminal -- not the absence of a matching adapter.
    poisoned = "spawn refused: cwd is not under any allowed root: ['/srv/status 503/roots']"
    signal = classify_exception(WorkflowSpawnRefused(poisoned))
    assert signal is not None and signal.retryable, (
        "precondition unmet: the adapters no longer read this text as a dependency "
        f"signal, so the test cannot show the refusal bypassing them ({signal!r})"
    )

    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    try:

        async def _refuse(prompt, opts):
            raise WorkflowSpawnRefused(poisoned)

        admission = RunnerAdmission(store, lane=RunnerLane(2, mode="aimd"))
        fn = admitted_agent_fn(_refuse, admission, run_id="refused", session_key=PARENT_SESSION_KEY)

        # Bounded, and the bound is part of the assertion: an unguarded refusal is
        # PARKED awaiting a dependency wake that never arrives here, so the call
        # would simply never return.
        with pytest.raises(WorkflowSpawnRefused):
            await asyncio.wait_for(fn("do work", {"cwd": "/nowhere"}), timeout=10.0)

        await settle_store_writes(store)
        # The row id comes from the production helper, so a change to the id
        # shape cannot silently make this assertion read a row that never existed.
        state = await asyncio.to_thread(store.state_of, workflow_task_id("refused", 1))
        assert state == m.FAILED, f"a refused step settled as {state!r}, not terminally"
        assert admission.lane.running == 0
    finally:
        store.close()
