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
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from test_cron_identity_gate import _kiro_provider, _Prerequisites

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_runner import install_acquire_identity_check
from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import await_until

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
async def test_try_acquire_takes_a_healthy_idle_session_after_the_check() -> None:
    async with _manager() as manager:
        await manager.get_or_create("channel:thread")
        manager.release("channel:thread")
        check = _Check([])
        manager.identity_check_before_acquire = check

        assert await manager.try_acquire("channel:thread") is True
        manager.release("channel:thread")

    assert check.calls == 1


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
    assert skipping == [("dashboard/chat_runner.py", "_run_chat/_claim_session")]
    gates = [
        call
        for call in ast.walk(_function("dashboard/chat_runner.py", "_run_chat"))
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_retire_sessions_on_identity_change"
    ]
    # The turn's own check is the attended one: it is why the claim may skip.
    assert [ast.unparse(call) for call in gates] == ["_retire_sessions_on_identity_change(state)"]


@pytest.mark.parametrize("start", ["start_dashboard", "start_api_server"])
def test_both_dashboard_start_paths_install_the_check_after_the_seed(start: str) -> None:
    lines: dict[str, list[int]] = {"seed": [], "install": []}
    for node in ast.walk(_function("dashboard/server.py", start)):
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
    manager: SessionManager, prerequisites: _ReadablePrerequisites
) -> None:
    """Leave a sweep pending: a session held through the switch keeps it incomplete."""
    await manager.get_or_create("channel:member")
    install_acquire_identity_check(
        SimpleNamespace(kiro_prerequisite_service=prerequisites, sessions=manager)
    )
    await manager.get_or_create("taskrunner:first")
    manager.release("taskrunner:first")
    assert manager.pending_identity_sweep_fingerprint
    prerequisites._changed = False


@pytest.mark.asyncio
async def test_concurrent_acquires_with_a_pending_sweep_drain_the_start_permits_once(
    monkeypatch,
) -> None:
    async with _manager() as manager:
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites)
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
async def test_an_acquire_runs_its_own_check_when_the_account_changes_during_the_shared_one(
    monkeypatch,
) -> None:
    async with _manager() as manager:
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites)
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
    async with _manager() as manager:
        prerequisites = _ReadablePrerequisites(changed=True, live="fp-current")
        await _manager_with_a_pending_sweep(manager, prerequisites)
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
