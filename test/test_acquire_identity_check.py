"""Every session acquire runs the Kiro account check before it hands a session out.

``SessionManager.get_or_create``, ``open_task_session`` and ``try_acquire`` await the
check the dashboard installs (``identity_check_before_acquire``) before they allocate,
claim or take an idle session's lease, so a task step, a channel turn or ``/compact``,
a workflow agent or a cron run cannot reuse a session started under a previous Kiro
account, and an acquire path added later cannot leave the check out. These tests pin
that front door: the order, the two
ways an acquire skips the check (its caller ran it, or an outer acquire in the same
task did), that a task started inside an acquire still checks its own, that a
check which fails never fails the acquire, that only the final drain skips it (an
update pause does not), and that the check the dashboard installs is the gate's
unattended caller, installed by both dashboard start paths.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import gc
import logging
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from external_logout_detection_helpers import _FakeProvider, _FakeRuntime, _write_store
from test_cron_identity_gate import _kiro_provider, _Prerequisites

from kiro_crew import kiro_prerequisite as kp
from kiro_crew.channel import Channel, run_channel_agent
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_runner import (
    _PENDING_SWEEP_RETRY_SECS,
    install_acquire_identity_check,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.session import SessionManager, _Session
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until, await_until, until_parked
from kiro_crew.workflows.agent_pool import _WorkflowSessionWorker

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


@asynccontextmanager
async def _manager(provider_factory: Any = _kiro_provider) -> AsyncIterator[SessionManager]:
    """A real manager over fake kiro-backed providers, closed however the test ends."""
    manager = SessionManager(KiroCrewConfig(), provider_factory=provider_factory)
    try:
        yield manager
    finally:
        await manager.close_all()


class _Check:
    """An installed check that logs each call into the same list as the acquire's steps."""

    def __init__(self, order: list[str], error: BaseException | None = None) -> None:
        self._order = order
        self._error = error
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        self._order.append("check")
        if self._error is not None:
            raise self._error


class _Allocation:
    """The allocation service behind the facade, reduced to the two calls the front door makes.

    ``nested`` reproduces the acquires the real service makes on its owner from inside
    an acquire, in the same task: a won-race retry re-enters ``get_or_create``, and a
    task session that cannot share a runtime falls back to ``get_or_create``.
    ``spawn`` starts a separate task that acquires while this one is still acquiring.
    """

    def __init__(self, manager: SessionManager, order: list[str], *, nested: str = "") -> None:
        self._manager = manager
        self._order = order
        self._nested = nested
        self.provider = MagicMock()

    async def get_or_create(self, key: str, *, _won_race_retries: int = 0, **_kwargs: Any):
        self._order.append(f"allocate:{key}")
        if self._nested == "won-race" and _won_race_retries == 0:
            return await self._manager.get_or_create(key, _won_race_retries=1)
        if self._nested == "spawn" and key == "taskrunner:outer":
            spawned = asyncio.ensure_future(self._manager.get_or_create("taskrunner:spawned"))
            await await_until(spawned, what="the acquire the spawned task makes")
        return self.provider, True, False

    async def open_task_session(self, parent_session_key: str, session_key: str, **_kwargs: Any):
        self._order.append(f"open:{session_key}")
        if self._nested == "fallback":
            return await self._manager.get_or_create(session_key)
        return self.provider, True, False


@pytest.mark.asyncio
async def test_get_or_create_runs_the_check_before_it_starts_a_session() -> None:
    order: list[str] = []

    def factory(key: str, **kwargs: Any) -> Any:
        order.append(f"start:{key}")
        return _kiro_provider(key, **kwargs)

    async with _manager(factory) as manager:
        manager.identity_check_before_acquire = _Check(order)
        await manager.get_or_create("taskrunner:step")

    # The check ran first, so a sweep it triggers lands before anything is handed out.
    assert order == ["check", "start:taskrunner:step"]


@pytest.mark.asyncio
async def test_a_claim_of_a_live_session_runs_the_check_too() -> None:
    async with _manager() as manager:
        check = _Check([])
        manager.identity_check_before_acquire = check
        first, _is_new, _resumed = await manager.get_or_create("channel:thread")
        manager.release("channel:thread")

        again, is_new, _resumed = await manager.get_or_create("channel:thread")

    # Reusing a live session is exactly when a previous account's child would be
    # handed out, so the claim checks as well as the cold start.
    assert again is first and not is_new
    assert check.calls == 2


@pytest.mark.asyncio
async def test_open_task_session_runs_the_check_before_it_opens(monkeypatch) -> None:
    order: list[str] = []
    async with _manager() as manager:
        manager.identity_check_before_acquire = _Check(order)
        with monkeypatch.context() as patched:
            allocation = _Allocation(manager, order)
            patched.setattr(manager, "_allocation_boundary", lambda: allocation)
            await manager.open_task_session("taskrunner:run", "taskrunner:run:step")

    assert order == ["check", "open:taskrunner:run:step"]


@pytest.mark.asyncio
async def test_a_caller_that_ran_the_check_itself_does_not_run_it_again(monkeypatch) -> None:
    order: list[str] = []
    async with _manager() as manager:
        check = _Check(order)
        manager.identity_check_before_acquire = check
        with monkeypatch.context() as patched:
            allocation = _Allocation(manager, order)
            patched.setattr(manager, "_allocation_boundary", lambda: allocation)
            await manager.get_or_create("dashboard:chat", identity_checked=True)

    assert check.calls == 0
    assert order == ["allocate:dashboard:chat"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("nested", "acquire", "expected"),
    [
        (
            "won-race",
            "get_or_create",
            ["check", "allocate:taskrunner:step", "allocate:taskrunner:step"],
        ),
        (
            "fallback",
            "open_task_session",
            ["check", "open:taskrunner:step", "allocate:taskrunner:step"],
        ),
    ],
)
async def test_an_acquire_the_same_task_makes_inside_an_acquire_checks_once(
    monkeypatch, nested: str, acquire: str, expected: list[str]
) -> None:
    order: list[str] = []
    async with _manager() as manager:
        check = _Check(order)
        manager.identity_check_before_acquire = check
        with monkeypatch.context() as patched:
            allocation = _Allocation(manager, order, nested=nested)
            patched.setattr(manager, "_allocation_boundary", lambda: allocation)
            if acquire == "get_or_create":
                await manager.get_or_create("taskrunner:step")
            else:
                await manager.open_task_session("taskrunner:run", "taskrunner:step")

    # The inner acquire is part of the outer one, which already checked.
    assert order == expected
    assert check.calls == 1


@pytest.mark.asyncio
async def test_a_task_started_inside_an_acquire_runs_the_check_for_its_own(monkeypatch) -> None:
    order: list[str] = []
    async with _manager() as manager:
        check = _Check(order)
        manager.identity_check_before_acquire = check
        with monkeypatch.context() as patched:
            allocation = _Allocation(manager, order, nested="spawn")
            patched.setattr(manager, "_allocation_boundary", lambda: allocation)
            await manager.get_or_create("taskrunner:outer")

    # The spawned task inherits the outer acquire's context but is another task, so
    # it checks: otherwise a long-lived task started inside one acquire would skip
    # the check on every acquire it ever made.
    assert order == ["check", "allocate:taskrunner:outer", "check", "allocate:taskrunner:spawned"]
    assert check.calls == 2


@pytest.mark.asyncio
async def test_a_task_started_inside_an_acquire_does_not_keep_the_acquiring_task_alive() -> None:
    async with _manager() as manager:
        release = asyncio.Event()
        started: list[asyncio.Task[bool]] = []

        async def _acquire() -> None:
            async with manager._acquire_after_identity_check(identity_checked=False):
                # A provider's stderr reader starts like this and runs for the provider's life.
                started.append(asyncio.ensure_future(release.wait()))

        acquiring = asyncio.ensure_future(_acquire())
        await acquiring
        finished = weakref.ref(acquiring)
        del acquiring
        # The wakeup that resumed this test still holds the finished task until it returns.
        await asyncio.sleep(0)
        try:
            gc.collect()
            assert not started[0].done()
            assert finished() is None
        finally:
            release.set()
            await await_until(started[0], what="the task started inside the acquire")


@pytest.mark.asyncio
async def test_a_check_that_fails_does_not_fail_the_acquire(monkeypatch, caplog) -> None:
    order: list[str] = []
    async with _manager() as manager:
        manager.identity_check_before_acquire = _Check(order, RuntimeError("store unreadable"))
        with monkeypatch.context() as patched:
            allocation = _Allocation(manager, order)
            patched.setattr(manager, "_allocation_boundary", lambda: allocation)
            with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
                provider, _is_new, _resumed = await manager.get_or_create("taskrunner:step")

    assert provider is allocation.provider
    assert order == ["check", "allocate:taskrunner:step"]
    assert "The Kiro account check before an acquire failed" in caplog.text


@pytest.mark.asyncio
async def test_a_cancelled_check_cancels_the_acquire(monkeypatch) -> None:
    order: list[str] = []
    async with _manager() as manager:
        manager.identity_check_before_acquire = _Check(order, asyncio.CancelledError())
        with monkeypatch.context() as patched:
            allocation = _Allocation(manager, order)
            patched.setattr(manager, "_allocation_boundary", lambda: allocation)
            with pytest.raises(asyncio.CancelledError):
                await manager.get_or_create("taskrunner:step")

    # A cancellation is not a failed check to step over: nothing is allocated.
    assert order == ["check"]


@pytest.mark.asyncio
async def test_no_check_runs_while_the_manager_closes(monkeypatch) -> None:
    order: list[str] = []
    async with _manager() as manager:
        check = _Check(order)
        manager.identity_check_before_acquire = check
        with monkeypatch.context() as patched:
            allocation = _Allocation(manager, order)
            patched.setattr(manager, "_allocation_boundary", lambda: allocation)
            patched.setattr(manager, "_closing", True)
            await manager.get_or_create("taskrunner:step")

    # The real allocation refuses an acquire on a closing manager; a sweep then
    # would only race the shutdown.
    assert check.calls == 0


@pytest.mark.asyncio
async def test_try_acquire_runs_the_check_during_an_update_pause() -> None:
    async with _manager() as manager:
        await manager.get_or_create("channel:thread")
        manager.release("channel:thread")
        check = _Check([])
        manager.identity_check_before_acquire = check
        assert await manager.pause_turn_admission_for_update() is True
        try:
            taken = await manager.try_acquire("channel:thread")
            if taken:
                manager.release("channel:thread")
        finally:
            await manager.resume_turn_admission_after_update()

    # An update pause closes admission but can resume, and it leaves try_acquire
    # open: a channel /compact or model pick takes the lease of an idle session
    # that may still hold the previous account. Only the final drain, when no
    # session outlives the process, skips the check.
    assert check.calls == 1


@pytest.mark.asyncio
async def test_the_installed_check_retires_a_session_left_on_the_previous_account() -> None:
    async with _manager() as manager:
        # Started before the account changed, and idle now.
        stale, _is_new, _resumed = await manager.get_or_create("channel:thread")
        manager.release("channel:thread")
        prerequisites = _Prerequisites(changed=True, live="fp-current")
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
        )

        await manager.get_or_create("taskrunner:step")

        stale.shutdown.assert_awaited()
        assert manager.get_provider("channel:thread") is None
        # A complete sweep reconciles, so the next acquire does not sweep again.
        assert prerequisites.reconciled == ["fp-current"]


@pytest.mark.asyncio
async def test_the_installed_check_is_the_unattended_caller() -> None:
    async with _manager() as manager:
        kept, _is_new, _resumed = await manager.get_or_create("channel:thread")
        manager.release("channel:thread")
        # Empty without proving a sign-out: a social login the reader cannot identify.
        prerequisites = _Prerequisites(changed=True, live="")
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
        )

        await manager.get_or_create("taskrunner:step")

        # An attended caller would retire every idle session on every acquire here;
        # the unattended one cannot tell a stale session from a healthy one, so it
        # touches none.
        kept.shutdown.assert_not_awaited()
        assert manager.get_provider("channel:thread") is kept
        assert prerequisites.reconciled == []


@pytest.mark.asyncio
async def test_try_acquire_retires_a_session_left_on_the_previous_account_instead_of_taking_it() -> (
    None
):
    async with _manager() as manager:
        stale, _is_new, _resumed = await manager.get_or_create("channel:thread")
        manager.release("channel:thread")
        prerequisites = _Prerequisites(changed=True, live="fp-current")
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
        )

        taken = await manager.try_acquire("channel:thread")

        # A channel /compact takes this lease and then prompts the session. The check
        # runs before the lease, while the session is still idle, so the sweep retires
        # it and the command finds nothing to compact; run after the lease, the sweep
        # would only flag it and the compaction would run on the previous account.
        assert taken is False
        stale.shutdown.assert_awaited()
        assert manager.get_provider("channel:thread") is None


@pytest.mark.asyncio
async def test_try_acquire_refuses_a_session_the_stamp_check_flags_without_evicting_it() -> None:
    async with _manager() as manager:
        # Spawned during an A->B->A round trip no read observed: the baseline and the
        # live account are both A, but this child's own spawn stamp says B.
        interim, _is_new, _resumed = await manager.get_or_create("channel:thread")
        manager.release("channel:thread")
        interim.spawn_identity = "cli-b"
        prerequisites = _Prerequisites(changed=False, live="cli-a")
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
        )

        taken = await manager.try_acquire("channel:thread")

        # Nothing changed for the baseline, so the check runs no sweep and only flags
        # the session. try_acquire refuses it, so a /compact never runs on B's
        # credential, but leaves the provider up: the same flag marks an idle parent
        # the sweep spared for its running sub-agents.
        assert taken is False
        assert manager._sessions["channel:thread"].retire_on_identity_change is True
        interim.shutdown.assert_not_awaited()
        assert prerequisites.reconciled == []

        # The next real turn's claim retires it.
        successor, is_new, _resumed = await manager.get_or_create("channel:thread")
        manager.release("channel:thread")
        assert is_new is True
        assert successor is not interim
        interim.shutdown.assert_awaited()


@pytest.mark.asyncio
async def test_is_retiring_answers_for_the_exact_key_try_acquire_refuses() -> None:
    async with _manager() as manager:
        session_key = "slack:1700000000.000100"
        await manager.get_or_create(session_key)
        manager.release(session_key)
        # Registered and unflagged: a refusal there is a running reply, not a restart.
        assert manager.is_retiring(session_key) is False
        # Absent: nothing to restart.
        assert manager.is_retiring("channel:absent") is False

        manager._sessions[session_key].retire_on_identity_change = True

        assert manager.is_retiring(session_key) is True
        assert await manager.try_acquire(session_key) is False
        assert manager.has_session(session_key) is True
        # The same exact-key lookup as try_acquire: the bare legacy Slack alias folds
        # onto this session for has_session, but try_acquire never takes it through
        # the alias, so is_retiring does not answer for it either.
        assert manager.has_session("1700000000.000100") is True
        assert manager.is_retiring("1700000000.000100") is False


@pytest.mark.asyncio
async def test_try_acquire_takes_a_healthy_idle_session_after_the_check() -> None:
    async with _manager() as manager:
        await manager.get_or_create("channel:thread")
        manager.release("channel:thread")
        check = _Check([])
        manager.identity_check_before_acquire = check

        assert await manager.try_acquire("channel:thread") is True
        manager.release("channel:thread")

    assert check.calls == 1


def _stamped_provider_factory(stamp: dict[str, str]) -> Any:
    """Kiro-backed providers stamped with the account ``stamp["account"]`` names at spawn.

    Each has a cheap ``new_conversation``, so only the account check decides whether a
    warm workflow worker reuses its process or takes the hard reset.
    """

    def _factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = _kiro_provider(
            session_key=session_key, agent=agent, channel_id=channel_id, **kwargs
        )
        provider.spawn_identity = stamp["account"]
        provider.new_conversation = AsyncMock()
        return provider

    return _factory


@pytest.mark.asyncio
async def test_a_warm_workflow_worker_reused_after_an_account_switch_starts_on_the_current_account() -> (
    None
):
    stamp = {"account": "fp-previous"}
    async with _manager(_stamped_provider_factory(stamp)) as manager:
        worker = _WorkflowSessionWorker(
            manager, key="wf-pool:run:0", agent=None, model=None, cwd=None
        )
        # Its first task. The worker keeps this lease until its last task, so its
        # later tasks never acquire again.
        await worker.start()
        stale = worker._provider
        # The account switches while the worker sits warm between two tasks.
        stamp["account"] = "fp-current"
        prerequisites = _Prerequisites(changed=True, live="fp-current")
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
        )
        try:
            await worker.reset()

            # The sweep only flags a session its worker holds. The reuse sees the flag and
            # takes the hard reset, not a new conversation on the previous account's process.
            stale.new_conversation.assert_not_awaited()
            stale.shutdown.assert_awaited()
            assert worker._provider is not stale
            assert worker._provider.spawn_identity == "fp-current"
            assert manager.get_provider("wf-pool:run:0") is worker._provider
        finally:
            await worker.shutdown()


@pytest.mark.asyncio
async def test_a_warm_workflow_worker_reused_on_an_unchanged_account_keeps_its_process() -> None:
    stamp = {"account": "fp-current"}
    async with _manager(_stamped_provider_factory(stamp)) as manager:
        worker = _WorkflowSessionWorker(
            manager, key="wf-pool:run:0", agent=None, model=None, cwd=None
        )
        await worker.start()
        warm = worker._provider
        prerequisites = _Prerequisites(changed=False, live="fp-current")
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
        )
        try:
            await worker.reset()

            warm.new_conversation.assert_awaited_once()
            warm.shutdown.assert_not_awaited()
            assert worker._provider is warm
        finally:
            await worker.shutdown()


def _answering_provider_factory(stamp: dict[str, str], answered: list[str]) -> Any:
    """Kiro-backed providers stamped at spawn that log the account each answer runs on."""

    def _factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = _kiro_provider(
            session_key=session_key, agent=agent, channel_id=channel_id, **kwargs
        )
        provider.spawn_identity = account = stamp["account"]

        async def _stream(message):
            answered.append(account)
            yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=f"answered on {account}")
            yield SimpleNamespace(kind=EVENT_COMPLETE)

        provider.stream = _stream
        return provider

    return _factory


async def _member_answers_one_message(
    manager: SessionManager, answered: list[str], prerequisites: _Prerequisites, switch: Any
) -> tuple[Any, Any]:
    """Spawn a channel member, run *switch* while it listens, then send it one message.

    Returns the provider the member answered on and the one it took at spawn.
    """
    channel = Channel(id="c1", topic="review")
    member = channel.add_agent(role="dev", agent_name="dev")
    assert member is not None
    task = asyncio.create_task(run_channel_agent(member, channel, manager))
    try:
        await async_wait_until(lambda: member.state == "listening", describe=lambda: member.state)
        spawned = manager.get_provider(member.session_key)
        switch()
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
        )
        await channel.post("human", "review this")
        await async_wait_until(
            lambda: answered and member.state == "listening", describe=lambda: member.state
        )
        return manager.get_provider(member.session_key), spawned
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_a_channel_member_after_an_account_switch_answers_on_the_current_account() -> None:
    stamp = {"account": "fp-previous"}
    answered: list[str] = []
    async with _manager(_answering_provider_factory(stamp, answered)) as manager:
        # The member keeps its spawn lease until it dies, so it never acquires again; the
        # account switches while it sits listening.
        current, spawned = await _member_answers_one_message(
            manager,
            answered,
            _Prerequisites(changed=True, live="fp-current"),
            lambda: stamp.update(account="fp-current"),
        )

    assert answered == ["fp-current"], f"the message ran on the previous account: {answered}"
    assert current is not spawned
    spawned.shutdown.assert_awaited()


@pytest.mark.asyncio
async def test_a_channel_member_on_an_unchanged_account_keeps_its_process() -> None:
    stamp = {"account": "fp-current"}
    answered: list[str] = []
    async with _manager(_answering_provider_factory(stamp, answered)) as manager:
        current, spawned = await _member_answers_one_message(
            manager, answered, _Prerequisites(changed=False, live=""), lambda: None
        )
        spawned.shutdown.assert_not_awaited()

    assert answered == ["fp-current"]
    assert current is spawned


class _EnclosingFunctions(ast.NodeVisitor):
    """The enclosing function path of every call that passes ``identity_checked=True``."""

    def __init__(self) -> None:
        self._stack: list[str] = []
        self.found: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if (
                keyword.arg == "identity_checked"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                self.found.append("/".join(self._stack))
        self.generic_visit(node)


def _function(path: str, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse((_SRC / path).read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def test_only_the_dashboard_turn_skips_the_check_and_it_runs_its_own() -> None:
    skipping: list[tuple[str, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "identity_checked" not in source:
            continue
        visitor = _EnclosingFunctions()
        visitor.visit(ast.parse(source))
        skipping += [(path.relative_to(_SRC).as_posix(), where) for where in visitor.found]

    # Any other caller passing it would acquire with no check at all.
    assert skipping == [("dashboard/chat_turn/turn_admission.py", "_turn_admit/_claim_session")]
    gates = [
        call
        for call in ast.walk(_function("dashboard/chat_turn/turn_admission.py", "_turn_admit"))
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_retire_sessions_on_identity_change"
    ]
    # The turn's own check is the attended one: it is why the claim may skip.
    assert [ast.unparse(call) for call in gates] == ["_retire_sessions_on_identity_change(state)"]


@pytest.mark.parametrize(
    ("path", "start"),
    [
        ("dashboard/server.py", "start_dashboard"),
        ("dashboard/server_runtime/api_entrypoint.py", "start_api_server"),
    ],
)
def test_both_dashboard_start_paths_install_the_check_after_the_seed(path: str, start: str) -> None:
    lines: dict[str, list[int]] = {"seed": [], "install": []}
    for node in ast.walk(_function(path, start)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "seed_sessions_baseline":
            lines["seed"].append(node.lineno)
        if isinstance(node.func, ast.Name) and node.func.id == "install_acquire_identity_check":
            lines["install"].append(node.lineno)

    # After the seed: a check installed before it would compare against an unset
    # baseline and sweep on the first acquire.
    assert len(lines["seed"]) == 1 and len(lines["install"]) == 1
    assert lines["install"][0] > lines["seed"][0]


class _ReadablePrerequisites(_Prerequisites):
    """Adds what an acquire reads before it takes a shared check's result.

    The fresh store read, and the record of reads since that check's own:
    ``reads`` lists the (generation, fingerprint) pairs the service returned.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.reads: list[tuple[int, str]] = []
        self.identity_read_generation = 0

    async def current_identity_fingerprint(self, *, allow_cached: bool = True) -> str:
        return self._live

    def identity_read_other_than(self, fingerprint: str, *, since: int) -> bool:
        return any(gen > since and seen != fingerprint for gen, seen in self.reads)


class _CountedDrain:
    """Counts the sweeps that drain the start permits, holding the first until released."""

    def __init__(self, manager: SessionManager) -> None:
        self.count = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._drain = manager._start_sem.drain

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[None]:
        self.count += 1
        self.entered.set()
        await await_until(self.release, what="the identity sweep's release barrier")
        async with self._drain():
            yield


async def _manager_with_a_pending_sweep(
    manager: SessionManager, prerequisites: _ReadablePrerequisites, clock: ManualClock
) -> None:
    """Leave a sweep pending: a session held through the switch keeps it incomplete.

    The installed check reads *clock*, so the test decides whether its next acquire
    falls inside the retry interval of the check that left the sweep pending.
    """
    held, _is_new, _resumed = await manager.get_or_create("channel:member")
    held.spawn_identity = "fp-previous"
    install_acquire_identity_check(
        SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager),
        clock=clock.monotonic,
    )
    current, _is_new, _resumed = await manager.get_or_create("taskrunner:first")
    current.spawn_identity = prerequisites._live
    manager.release("taskrunner:first")
    assert manager.pending_identity_sweep_fingerprint
    prerequisites._changed = False


@pytest.mark.asyncio
async def test_concurrent_acquires_with_a_pending_sweep_drain_the_start_permits_once(
    monkeypatch,
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        # Past the retry interval, so the first of the acquires re-runs the pending sweep.
        clock.advance(_PENDING_SWEEP_RETRY_SECS)
        drains = _CountedDrain(manager)
        monkeypatch.setattr(manager._start_sem, "drain", drains)

        acquires = [
            asyncio.ensure_future(manager.get_or_create(f"workflow:agent-{n}")) for n in range(5)
        ]
        await await_until(drains.entered, what="the first acquire's sweep")
        # Let every other acquire reach the check before the sweep finishes.
        for _ in range(20):
            await asyncio.sleep(0)
        drains.release.set()
        try:
            await await_until(asyncio.gather(*acquires), what="the five acquires")
        finally:
            for acquire in acquires:
                acquire.cancel()

        # The holdout still keeps the sweep pending, so each acquire alone would have
        # re-run it; together they share the one in flight.
        assert drains.count == 1
        assert manager.pending_identity_sweep_fingerprint


@pytest.mark.asyncio
async def test_an_acquire_joining_a_check_runs_its_own_while_a_process_of_unknown_account_is_claimable(
    monkeypatch,
) -> None:
    clock = ManualClock()
    live = kp._combine_identity_fingerprints("store", "vault", "new-key")
    stamp = kp._combine_identity_fingerprints("store", "vault", "old-key")
    async with _manager() as manager:
        prerequisites = _HeldReadsPrerequisites(changed=True, live=live)
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        clock.advance(_PENDING_SWEEP_RETRY_SECS)
        runtime = _identity_runtime(stamp, active=True)
        manager._subagent_runtimes["taskrunner:run"] = runtime  # type: ignore[assignment]
        drains = _CountedDrain(manager)
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        prerequisites.reads_released = asyncio.Event()

        first = asyncio.ensure_future(manager.get_or_create("workflow:agent-0"))
        joining: asyncio.Future[Any] | None = None
        try:
            await await_until(drains.entered, what="the first acquire's sweep")
            joining = asyncio.ensure_future(manager.get_or_create("workflow:agent-1"))
            for _ in range(20):
                await asyncio.sleep(0)
            drains.release.set()
            await until_parked(prerequisites.reads_released)
            assert manager._subagent_runtimes["taskrunner:run"] is runtime
            assert runtime.killed == 0
            runtime._active = False
            prerequisites.reads_released.set()
            await await_until(asyncio.gather(first, joining), what="both acquires")
        finally:
            drains.release.set()
            prerequisites.reads_released.set()
            first.cancel()
            if joining is not None:
                joining.cancel()

        assert drains.count == 2
        assert runtime.killed == 1
        assert "taskrunner:run" not in manager._subagent_runtimes


@pytest.mark.asyncio
async def test_an_acquire_runs_its_own_check_when_the_account_changes_during_the_shared_one(
    monkeypatch,
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        # Past the retry interval, so the first acquire re-runs the pending sweep.
        clock.advance(_PENDING_SWEEP_RETRY_SECS)
        drains = _CountedDrain(manager)
        monkeypatch.setattr(manager._start_sem, "drain", drains)

        first = asyncio.ensure_future(manager.get_or_create("workflow:agent-0"))
        await await_until(drains.entered, what="the first acquire's sweep")
        joining = asyncio.ensure_future(manager.get_or_create("workflow:agent-1"))
        for _ in range(20):
            await asyncio.sleep(0)
        # The account switches while the first sweep runs: sessions it spares are on
        # the account just left, so the joining acquire cannot take its result.
        prerequisites._live = "fp-next"
        prerequisites._changed = True
        drains.release.set()
        try:
            await await_until(asyncio.gather(first, joining), what="both acquires")
        finally:
            first.cancel()
            joining.cancel()

        assert drains.count == 2


@pytest.mark.asyncio
async def test_cancelling_the_acquire_that_started_the_shared_check_does_not_cancel_it(
    monkeypatch,
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        # Past the retry interval, so the starter re-runs the pending sweep.
        clock.advance(_PENDING_SWEEP_RETRY_SECS)
        drains = _CountedDrain(manager)
        monkeypatch.setattr(manager._start_sem, "drain", drains)

        starter = asyncio.ensure_future(manager.get_or_create("workflow:agent-0"))
        await await_until(drains.entered, what="the first acquire's sweep")
        joining = asyncio.ensure_future(manager.get_or_create("workflow:agent-1"))
        for _ in range(20):
            await asyncio.sleep(0)
        starter.cancel()
        drains.release.set()

        try:
            with pytest.raises(asyncio.CancelledError):
                await await_until(starter, what="the cancelled starter")
            await await_until(joining, what="the acquire that joined")
        finally:
            joining.cancel()
        assert drains.count == 1


class _GatedPrerequisites(_ReadablePrerequisites):
    """Holds the first account read open, so a second acquire arrives mid-check.

    The store is signed out after that first read: the next fresh read proves it, as
    the real service's read does when it finds no account on disk.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.reading = asyncio.Event()
        self.release = asyncio.Event()
        self._held = False

    async def current_identity_fingerprint(self, *, allow_cached: bool = True) -> str:
        self.identity_absence_is_definitive = True
        return await super().current_identity_fingerprint(allow_cached=allow_cached)

    async def identity_changed_since_sessions(self) -> tuple[bool, str]:
        if not self._held:
            self._held = True
            self.reading.set()
            await await_until(self.release, what="the held account read")
        else:
            self.identity_absence_is_definitive = True
        return await super().identity_changed_since_sessions()


@pytest.mark.asyncio
async def test_an_acquire_does_not_take_an_empty_shared_result_after_a_sign_out(
    monkeypatch,
) -> None:
    async with _manager() as manager:
        stale, _is_new, _resumed = await manager.get_or_create("channel:thread")
        manager.release("channel:thread")
        # A social login: the store reads empty and proves no sign-out.
        prerequisites = _GatedPrerequisites(changed=True, live="")
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
        )

        first = asyncio.ensure_future(manager.get_or_create("workflow:agent-0"))
        joining: asyncio.Future[Any] | None = None
        try:
            await await_until(prerequisites.reading, what="the first acquire's read")
            joining = asyncio.ensure_future(manager.get_or_create("workflow:agent-1"))
            for _ in range(20):
                await asyncio.sleep(0)
            # The first check reads an account it cannot identify and leaves every
            # session alone; the user then signs out. Both read empty, so the joining
            # acquire must not take the first check's result.
            prerequisites.release.set()
            await await_until(asyncio.gather(first, joining), what="both acquires")
        finally:
            prerequisites.release.set()
            first.cancel()
            if joining is not None:
                joining.cancel()

        stale.shutdown.assert_awaited()
        assert manager.get_provider("channel:thread") is None


@pytest.mark.asyncio
async def test_a_joiner_does_not_take_an_empty_shared_result_without_unknown_holders() -> None:
    async with _manager() as manager:
        prerequisites = _GatedPrerequisites(changed=True, live="")
        identity_checks = AsyncMock(side_effect=prerequisites.identity_changed_since_sessions)
        prerequisites.identity_changed_since_sessions = identity_checks  # type: ignore[method-assign]
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
        )

        first = asyncio.ensure_future(manager.identity_check_before_acquire())
        joining: asyncio.Future[Any] | None = None
        try:
            await await_until(prerequisites.reading, what="the first acquire's read")
            joining = asyncio.ensure_future(manager.identity_check_before_acquire())
            for _ in range(20):
                await asyncio.sleep(0)
            prerequisites.release.set()
            await await_until(asyncio.gather(first, joining), what="both account checks")
        finally:
            prerequisites.release.set()
            first.cancel()
            if joining is not None:
                joining.cancel()

        assert identity_checks.await_count == 2


class _StampingPrerequisites(_ReadablePrerequisites):
    """Restamps one idle session during the joining acquire's fresh read.

    That models a cold start which began under an interim account and registered
    after the shared sweep scanned the registry: it comes up stamped with the
    interim account while the store reads the live one again.
    """

    def __init__(self, interim: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._interim = interim

    async def current_identity_fingerprint(self, *, allow_cached: bool = True) -> str:
        self._interim.spawn_identity = "cli-b"
        return await super().current_identity_fingerprint(allow_cached=allow_cached)


@pytest.mark.asyncio
async def test_an_acquire_that_takes_a_shared_result_still_runs_the_stamp_check(
    monkeypatch,
) -> None:
    async with _manager() as manager:
        interim, _is_new, _resumed = await manager.get_or_create("channel:thread")
        manager.release("channel:thread")
        interim.spawn_identity = "cli-a"
        prerequisites = _StampingPrerequisites(interim, changed=True, live="cli-a")
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
        )
        drains = _CountedDrain(manager)
        monkeypatch.setattr(manager._start_sem, "drain", drains)

        first = asyncio.ensure_future(manager.get_or_create("workflow:agent-0"))
        compact: asyncio.Future[Any] | None = None
        try:
            await await_until(drains.entered, what="the first acquire's sweep")
            # A channel /compact arrives while the shared check runs.
            compact = asyncio.ensure_future(manager.try_acquire("channel:thread"))
            for _ in range(20):
                await asyncio.sleep(0)
            drains.release.set()
            await await_until(asyncio.gather(first, compact), what="both acquires")
        finally:
            drains.release.set()
            first.cancel()
            if compact is not None:
                compact.cancel()

        # The shared sweep spared the session (its stamp matched the live account
        # then), so only the joining acquire's own stamp check can catch it.
        assert drains.count == 1
        assert compact.result() is False
        assert manager._sessions["channel:thread"].retire_on_identity_change is True
        interim.shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_acquire_runs_its_own_check_after_another_account_was_observed(
    monkeypatch,
) -> None:
    async with _manager() as manager:
        prerequisites = _ReadablePrerequisites(changed=True, live="cli-a")
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
        )
        drains = _CountedDrain(manager)
        monkeypatch.setattr(manager._start_sem, "drain", drains)

        first = asyncio.ensure_future(manager.get_or_create("workflow:agent-0"))
        joining: asyncio.Future[Any] | None = None
        try:
            await await_until(drains.entered, what="the first acquire's sweep")
            joining = asyncio.ensure_future(manager.get_or_create("workflow:agent-1"))
            for _ in range(20):
                await asyncio.sleep(0)
            # A status poll read account B while the sweep ran, and the store is back
            # on A: a session that registered on B late may carry no stamp at all,
            # so only a check of the joining acquire's own can cover it.
            prerequisites.identity_read_generation += 1
            prerequisites.reads.append((prerequisites.identity_read_generation, "cli-b"))
            drains.release.set()
            await await_until(asyncio.gather(first, joining), what="both acquires")
        finally:
            drains.release.set()
            first.cancel()
            if joining is not None:
                joining.cancel()

        assert drains.count == 2


@pytest.mark.asyncio
async def test_sequential_acquires_re_run_a_pending_sweep_at_most_once_a_minute(
    monkeypatch,
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        late, _is_new, _resumed = await manager.get_or_create("channel:thread")
        manager.release("channel:thread")
        # Stamped with the live account, so the sweep below spares it.
        late.spawn_identity = "fp-current"
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        assert drains.count == 1
        # A cold start that began under another account registered after that sweep
        # scanned the registry: its stamp is the only evidence.
        late.spawn_identity = "fp-other"

        clock.advance(_PENDING_SWEEP_RETRY_SECS - 1)
        await manager.get_or_create("workflow:agent-0")

        # Inside the minute the acquire takes the last check's result, so it drains
        # nothing, and it still runs the stamp check.
        assert drains.count == 1
        assert manager._sessions["channel:thread"].retire_on_identity_change is True
        late.shutdown.assert_not_awaited()

        clock.advance(1)
        await manager.get_or_create("workflow:agent-1")

        # The holdout still keeps the sweep pending, and the minute is over.
        assert drains.count == 2


def _unstamped_kiro_provider(session_key: Any = None, **kwargs: Any) -> Any:
    """A fake kiro-backed provider with no spawn stamp yet, so its cold start stamps it."""
    provider = _kiro_provider(session_key, **kwargs)
    provider.spawn_identity = ""
    return provider


@pytest.mark.asyncio
@pytest.mark.usefixtures("sel_private_root")
async def test_thirty_cold_starts_inside_the_minute_do_not_re_run_a_pending_sweep(
    monkeypatch, tmp_path: Path
) -> None:
    clock = ManualClock()
    store = kp.kiro_identity_store_path("linux", tmp_path, {})
    _write_store(store)
    # The real service, so its own read record decides whether a check is shared.
    service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
    assert await service.seed_sessions_baseline() is True
    baseline = await service.current_identity_fingerprint(allow_cached=False)
    async with _manager(_unstamped_kiro_provider) as manager:
        # Held through the switch, so the sweep cannot finish. Its non-empty stamp
        # lets acquires inside the retry interval use the recorded check.
        held, _is_new, _resumed = await manager.get_or_create("channel:member")
        held.spawn_identity = baseline
        # From here each cold start reads the store before and after its spawn.
        manager.spawn_identity_reader = service.read_spawn_identity
        _write_store(
            store,
            start_url="https://personal.awsapps.com/start",
            profile="arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
        )
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=service, sessions=manager),
            clock=clock.monotonic,
        )
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        await manager.get_or_create("taskrunner:first")
        manager.release("taskrunner:first")
        assert manager.pending_identity_sweep_fingerprint
        assert drains.count == 1
        reads_before = service.identity_read_generation

        for n in range(30):
            await manager.get_or_create(f"workflow:agent-{n}")
            manager.release(f"workflow:agent-{n}")

        # Every acquire took the result of the check that left the sweep pending.
        assert drains.count == 1
        # That held across one read for each acquire's check and two for each cold start.
        assert service.identity_read_generation - reads_before >= 90


@pytest.mark.asyncio
@pytest.mark.parametrize("another_account", ["switched", "observed"])
async def test_an_acquire_inside_the_retry_interval_checks_itself_after_another_account(
    monkeypatch, another_account: str
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        if another_account == "switched":
            # The sessions the pending sweep spared are on the account just left.
            prerequisites._live = "fp-next"
            prerequisites._changed = True
        else:
            # A status poll read another account and the store is back: a session that
            # registered on it may carry no stamp, so only a sweep covers it.
            prerequisites.identity_read_generation += 1
            prerequisites.reads.append((prerequisites.identity_read_generation, "fp-next"))

        await manager.get_or_create("workflow:agent-0")

        assert drains.count == 1


@pytest.mark.asyncio
async def test_an_acquire_inside_the_retry_interval_after_a_sign_out_runs_its_own_check(
    monkeypatch,
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        # Held through the sign-out, so the sweep cannot finish.
        await manager.get_or_create("channel:member")
        prerequisites = _ReadablePrerequisites(changed=True, live="", absence_definitive=True)
        install_acquire_identity_check(
            SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager),
            clock=clock.monotonic,
        )
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        await manager.get_or_create("workflow:agent-0")
        assert drains.count == 1

        await manager.get_or_create("workflow:agent-1")

        # An empty reading is never shared: a sign-out and an account the reader cannot
        # identify both read empty, and only an acquire's own check tells them apart.
        assert drains.count == 2


@pytest.mark.asyncio
async def test_an_acquire_inside_the_retry_interval_checks_itself_once_another_sweep_ran(
    monkeypatch,
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        # The Crew sign-out sweep reads no account and carries no fingerprint, so the
        # last check does not speak for what the registry still holds.
        await manager.retire_kiro_identity_sessions()
        assert manager.pending_identity_sweep_fingerprint == ""
        # No sweep has completed under fp-current, so it still reads as a change.
        prerequisites._changed = True
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)

        await manager.get_or_create("workflow:agent-0")

        assert drains.count == 1


class _HeldReadsPrerequisites(_ReadablePrerequisites):
    """Holds every fresh store read open while ``reads_released`` is set and clear."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.reads_released: asyncio.Event | None = None

    async def current_identity_fingerprint(self, *, allow_cached: bool = True) -> str:
        if self.reads_released is not None:
            await await_until(self.reads_released, what="the held store reads")
        return await super().current_identity_fingerprint(allow_cached=allow_cached)


@pytest.mark.asyncio
async def test_acquires_that_read_the_store_together_inside_the_retry_interval_share_a_check(
    monkeypatch,
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        prerequisites = _HeldReadsPrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        # The account switches, and a fan-out arrives inside the minute: each acquire
        # reads the store before any of them has started a check.
        prerequisites._live = "fp-next"
        prerequisites._changed = True
        prerequisites.reads_released = asyncio.Event()
        acquires = [
            asyncio.ensure_future(manager.get_or_create(f"workflow:agent-{n}")) for n in range(3)
        ]
        try:
            await until_parked(prerequisites.reads_released, count=3)
            prerequisites.reads_released.set()
            await await_until(asyncio.gather(*acquires), what="the three acquires")
        finally:
            prerequisites.reads_released.set()
            for acquire in acquires:
                acquire.cancel()

        # The first to find the last check stale starts one; the others share it.
        assert drains.count == 1


def _identity_runtime(stamp: str = "", *, active: bool = False) -> _FakeRuntime:
    runtime = _FakeRuntime("", active=active)
    runtime.spawn_identity = stamp
    runtime.is_alive = lambda: runtime.killed == 0  # type: ignore[attr-defined]
    return runtime


def _identity_provider(stamp: str = "", *, backend: str = "") -> _FakeProvider:
    provider = _FakeProvider(backend)
    provider.spawn_identity = stamp
    return provider


def test_holds_kiro_process_of_unknown_account_finds_a_registered_session() -> None:
    manager = SessionManager(KiroCrewConfig())
    manager._sessions["session"] = _Session(provider=_identity_provider())

    assert manager.holds_kiro_process_of_unknown_account("fp-current") is True


def test_holds_kiro_process_of_unknown_account_finds_a_companion_runtime() -> None:
    manager = SessionManager(KiroCrewConfig())
    manager._subagent_runtimes["parent"] = _identity_runtime()  # type: ignore[assignment]

    assert manager.holds_kiro_process_of_unknown_account("fp-current") is True


def test_holds_kiro_process_of_unknown_account_finds_a_background_runtime() -> None:
    manager = SessionManager(KiroCrewConfig())
    manager._bg_runtime = _identity_runtime()  # type: ignore[assignment]

    assert manager.holds_kiro_process_of_unknown_account("fp-current") is True


def test_holds_kiro_process_of_unknown_account_finds_a_warm_provider_without_consuming_it() -> None:
    manager = SessionManager(KiroCrewConfig())
    provider = _identity_provider()
    manager._warm_pool.put_nowait((provider, 0.0))

    assert manager.holds_kiro_process_of_unknown_account("fp-current") is True
    assert manager.warm_providers() == [provider]


def test_holds_kiro_process_of_unknown_account_ignores_stamped_non_kiro_and_unclaimable_holders() -> (
    None
):
    manager = SessionManager(KiroCrewConfig())
    manager._sessions["spawned-under-live"] = _Session(provider=_identity_provider("fp-current"))
    manager._sessions["mismatched"] = _Session(provider=_identity_provider("fp-other"))
    manager._sessions["non-kiro"] = _Session(provider=_identity_provider(backend="claude"))
    retiring = _Session(provider=_identity_provider())
    retiring.retire_on_identity_change = True
    manager._sessions["retiring"] = retiring
    manager._subagent_runtimes["parent"] = _identity_runtime(
        "fp-current"
    )  # type: ignore[assignment]
    manager._bg_runtime = _identity_runtime("fp-current")  # type: ignore[assignment]
    pooled = _identity_provider("fp-current")
    pooled_non_kiro = _identity_provider(backend="claude")
    manager._warm_pool.put_nowait((pooled, 0.0))
    manager._warm_pool.put_nowait((pooled_non_kiro, 0.0))
    manager._draining_subagent_runtimes.append(_identity_runtime())  # type: ignore[arg-type]
    manager._draining_bg_runtimes.append(_identity_runtime())  # type: ignore[arg-type]

    assert manager.holds_kiro_process_of_unknown_account("fp-current") is False
    assert manager.warm_providers() == [pooled, pooled_non_kiro]


def test_holds_kiro_process_of_unknown_account_counts_a_stamp_whose_store_went_empty() -> None:
    stamp = kp._combine_identity_fingerprints("store", "vault")
    # An external logout empties the store while the Crew vault stays populated.
    live = kp._combine_identity_fingerprints("", "vault")
    manager = SessionManager(KiroCrewConfig())
    manager._subagent_runtimes["parent"] = _identity_runtime(stamp)  # type: ignore[assignment]

    assert manager.holds_kiro_process_of_unknown_account(live) is True


def test_holds_kiro_process_of_unknown_account_counts_a_changed_key_the_holder_receives() -> None:
    stamp = kp._combine_identity_fingerprints("store", "vault", "old-key")
    live = kp._combine_identity_fingerprints("store", "vault", "new-key")
    given_the_key = SessionManager(KiroCrewConfig())
    given_the_key._sessions["session"] = _Session(provider=_identity_provider(stamp))
    keyless = SessionManager(KiroCrewConfig())
    relay = _identity_provider(stamp, backend="kas")
    relay._kas_host_auth = True
    keyless._sessions["session"] = _Session(provider=relay)

    assert given_the_key.holds_kiro_process_of_unknown_account(live) is True
    # A relay never handed the key loaded the store and vault, and those still match.
    assert keyless.holds_kiro_process_of_unknown_account(live) is False


@pytest.mark.asyncio
async def test_an_unstamped_companion_runtime_forces_a_sweep_inside_the_retry_interval(
    monkeypatch,
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        runtime = _identity_runtime(active=True)
        manager._subagent_runtimes["taskrunner:run"] = runtime  # type: ignore[assignment]
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        assert drains.count == 1
        store_reads = AsyncMock(side_effect=prerequisites.current_identity_fingerprint)
        prerequisites.current_identity_fingerprint = store_reads  # type: ignore[method-assign]

        runtime._active = False
        await manager.get_or_create("workflow:agent-0")
        manager.release("workflow:agent-0")

        assert drains.count == 2
        # The acquire breaks before the store read that taking the result would need.
        store_reads.assert_not_awaited()
        assert runtime.killed == 1
        assert runtime.kill_reasons == ["subagent runtime released"]
        assert "taskrunner:run" not in manager._subagent_runtimes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stamp", "live"),
    [
        pytest.param(
            kp._combine_identity_fingerprints("store", "vault"),
            kp._combine_identity_fingerprints("", "vault"),
            id="store_dropped",
        ),
        pytest.param(
            kp._combine_identity_fingerprints("store", "vault", "old-key"),
            kp._combine_identity_fingerprints("store", "vault", "new-key"),
            id="api_key_changed",
        ),
    ],
)
async def test_a_companion_runtime_of_unknown_account_forces_a_sweep_inside_the_retry_interval(
    monkeypatch, stamp: str, live: str
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        runtime = _identity_runtime(stamp, active=True)
        manager._subagent_runtimes["taskrunner:run"] = runtime  # type: ignore[assignment]
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        prerequisites = _ReadablePrerequisites(changed=True, live=live)
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        assert drains.count == 1
        # Neither the busy sweep nor the stamp check moved it: its stamp proves no mismatch.
        assert manager._subagent_runtimes["taskrunner:run"] is runtime

        runtime._active = False
        await manager.get_or_create("workflow:agent-0")
        manager.release("workflow:agent-0")

        assert drains.count == 2
        assert runtime.killed == 1
        assert runtime.kill_reasons == ["subagent runtime released"]
        assert "taskrunner:run" not in manager._subagent_runtimes


@pytest.mark.asyncio
async def test_a_stamped_wrong_account_runtime_is_displaced_without_a_sweep_inside_the_interval(
    monkeypatch,
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        assert drains.count == 1
        runtime = _identity_runtime("fp-other", active=True)
        manager._subagent_runtimes["taskrunner:run"] = runtime  # type: ignore[assignment]

        await manager.get_or_create("workflow:agent-0")
        manager.release("workflow:agent-0")

        assert drains.count == 1
        assert runtime.killed == 0
        assert "taskrunner:run" not in manager._subagent_runtimes
        assert runtime in manager._draining_subagent_runtimes


@pytest.mark.asyncio
async def test_an_acquire_rechecks_pending_after_the_recorded_covers_read(
    monkeypatch,
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        prerequisites = _HeldReadsPrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        for session in manager._sessions.values():
            session.provider.spawn_identity = "fp-current"
        identity_checks = AsyncMock(side_effect=prerequisites.identity_changed_since_sessions)
        prerequisites.identity_changed_since_sessions = identity_checks  # type: ignore[method-assign]
        prerequisites.reads_released = asyncio.Event()

        acquire = asyncio.ensure_future(manager.get_or_create("workflow:agent-0"))
        try:
            await until_parked(prerequisites.reads_released)
            _retired, complete = await manager.retire_kiro_identity_sessions(
                fingerprint="fp-current"
            )
            assert complete is True
            assert manager.pending_identity_sweep_fingerprint == ""
            prerequisites.reads_released.set()
            await await_until(acquire, what="the acquire after the sweep completed")
            manager.release("workflow:agent-0")
        finally:
            prerequisites.reads_released.set()
            acquire.cancel()

        assert identity_checks.await_count == 1
        assert drains.count == 2


@pytest.mark.asyncio
async def test_a_process_of_unknown_account_that_registers_during_the_store_read_forces_a_sweep(
    monkeypatch,
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        prerequisites = _HeldReadsPrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        assert manager.holds_kiro_process_of_unknown_account("fp-current") is False
        identity_checks = AsyncMock(side_effect=prerequisites.identity_changed_since_sessions)
        prerequisites.identity_changed_since_sessions = identity_checks  # type: ignore[method-assign]
        prerequisites.reads_released = asyncio.Event()
        late = _identity_runtime()

        acquire = asyncio.ensure_future(manager.get_or_create("workflow:agent-0"))
        try:
            await until_parked(prerequisites.reads_released)
            manager._subagent_runtimes["taskrunner:late"] = late  # type: ignore[assignment]
            prerequisites.reads_released.set()
            await await_until(acquire, what="the acquire after a process registered")
            manager.release("workflow:agent-0")
        finally:
            prerequisites.reads_released.set()
            acquire.cancel()

        assert identity_checks.await_count == 1
        assert drains.count == 2
        assert late.killed == 1
        assert "taskrunner:late" not in manager._subagent_runtimes


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["missing", "raising"])
async def test_an_acquire_inside_the_retry_interval_checks_itself_without_a_working_holder_query(
    monkeypatch, broken: str
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        # Every claimable process is accounted for, so a working query would share the result.
        assert manager.holds_kiro_process_of_unknown_account("fp-current") is False
        if broken == "missing":
            monkeypatch.delattr(SessionManager, "holds_kiro_process_of_unknown_account")
        else:
            monkeypatch.setattr(
                manager,
                "holds_kiro_process_of_unknown_account",
                MagicMock(side_effect=RuntimeError("registry unreadable")),
            )

        await manager.get_or_create("workflow:agent-0")

        assert drains.count == 2


class _RecordlessPrerequisites(_ReadablePrerequisites):
    """A service that keeps no record of the reads since a check."""

    identity_read_other_than = None


class _UnreadableRecordPrerequisites(_ReadablePrerequisites):
    """A service whose record of the reads since a check cannot be read."""

    def identity_read_other_than(self, fingerprint: str, *, since: int) -> bool:
        raise RuntimeError("read record unreadable")


class _UncountedReadsPrerequisites(_ReadablePrerequisites):
    """A service that keeps no read count, so a check records no read generation.

    Its record answers "no other read" whatever it is asked, so only the missing
    generation can keep an acquire from taking the recorded result.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.identity_read_generation = None

    def identity_read_other_than(self, fingerprint: str, *, since: Any) -> bool:
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prerequisites_class", "drains_after_acquire"),
    [
        (_ReadablePrerequisites, 1),
        (_RecordlessPrerequisites, 2),
        (_UnreadableRecordPrerequisites, 2),
        (_UncountedReadsPrerequisites, 2),
    ],
    ids=["working_record", "no_read_record", "read_record_raises", "no_read_generation"],
)
async def test_an_acquire_inside_the_retry_interval_checks_itself_unless_the_read_record_answers(
    monkeypatch, prerequisites_class: type[_ReadablePrerequisites], drains_after_acquire: int
) -> None:
    clock = ManualClock()
    async with _manager() as manager:
        drains = _CountedDrain(manager)
        drains.release.set()
        monkeypatch.setattr(manager._start_sem, "drain", drains)
        prerequisites = prerequisites_class(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites, clock)
        # Every claimable process is accounted for and the store keeps reading the
        # recorded account, so the read record alone decides whether the next
        # acquire may take the recorded result: working_record shows it would.
        assert manager.holds_kiro_process_of_unknown_account("fp-current") is False
        assert drains.count == 1

        await manager.get_or_create("workflow:agent-0")

        assert drains.count == drains_after_acquire
