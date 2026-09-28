"""Behaviour of the counted pid shields and the kill attribution line.

Both rules under test are about a process having more than one interested party:
a shield must survive the first holder leaving, and a kill must say who ended it.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew import runtime_ownership as ro


class _FakeRuntime:
    """Something that carries a pid, which is all the attribution reads."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid


# -- the counted shield --


def test_a_pid_stays_shielded_until_its_last_holder_leaves() -> None:
    """As a set, the first holder to leave tore the shield off a process the
    second was still using, and the sweep then reaped a live runtime."""
    shield = ro.PidRefcount()
    shield.add(4242)
    shield.add(4242)
    shield.discard(4242)
    assert 4242 in shield, "one holder is gone, the other still needs the shield"
    shield.discard(4242)
    assert 4242 not in shield and not shield


def test_a_zero_count_pid_is_removed_rather_than_kept_at_zero() -> None:
    """Iteration and truthiness must never report a pid nothing holds."""
    shield = ro.PidRefcount()
    shield.add(7)
    shield.discard(7)
    assert list(shield) == [] and len(shield) == 0 and not shield


def test_the_refcount_reads_like_the_set_it_replaces() -> None:
    shield = ro.PidRefcount([11, 22, 22])
    assert set(shield) == {11, 22}
    assert 11 in shield and 33 not in shield
    assert sorted(shield | {33}) == [11, 22, 33]
    assert len(shield) == 2 and bool(shield) is True
    assert shield.count(22) == 2 and shield.count(11) == 1 and shield.count(99) == 0
    shield.clear()
    assert not shield


def test_the_refcount_rejects_what_is_not_a_pid() -> None:
    shield = ro.PidRefcount()
    for value in (0, -1, True, False):
        shield.add(value)  # type: ignore[arg-type]
    assert not shield
    shield.discard(12345)  # unheld: a no-op, not an error


def test_the_protected_pid_shield_is_reference_counted() -> None:
    """Two pools shielding one process. ``session_pid``'s registry is the one
    every app worker pool and the knowledge LLM pool register with, and each
    pairs its own register with its own unregister."""
    from kiro_crew import session_pid

    pid = 999_001
    session_pid.register_protected_pid(pid)
    session_pid.register_protected_pid(pid)
    try:
        session_pid.unregister_protected_pid(pid)
        assert pid in session_pid._protected_pids(), "the second holder still needs it"
        session_pid.unregister_protected_pid(pid)
        assert pid not in session_pid._protected_pids()
    finally:
        # Only THIS pid. The registry is process-global and other tests in the
        # same worker shield their own pids in it, so clearing the whole thing
        # would tear their shields off and fail them instead of this one.
        while pid in session_pid._protected_pids():
            session_pid.unregister_protected_pid(pid)


def test_the_starting_pid_shield_is_reference_counted() -> None:
    """Two concurrent starts of one session legitimately shield one pid: the
    allocator carries a race budget for starting the same session twice."""
    from kiro_crew.session_allocation import SessionRegistryState

    state = SessionRegistryState()
    state.starting_pids.add(4242)
    state.starting_pids.add(4242)
    state.starting_pids.discard(4242)
    assert 4242 in state.starting_pids
    state.starting_pids.discard(4242)
    assert 4242 not in state.starting_pids and not state.starting_pids


def test_the_starting_pid_shield_still_reads_as_a_set_for_the_sweep() -> None:
    """The orphan sweep unions it with other pid sources, so it has to keep
    answering ``in``, iteration and ``set(...)``."""
    from kiro_crew.session_allocation import SessionRegistryState

    state = SessionRegistryState()
    state.starting_pids.add(101)
    assert set(state.starting_pids) == {101}
    assert 101 in state.starting_pids
    assert bool(state.starting_pids) is True


# -- the attribution line --


def test_the_kill_line_names_pid_caller_and_reason(caplog) -> None:
    """The line the field could not get: a death log written where the process
    is reaped says a process died, never who decided it should."""
    with caplog.at_level(logging.WARNING, logger="kiro_crew.runtime_ownership"):
        ro.note_runtime_kill(909, reason="dashboard reset all", caller="reset handler")
    line = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "909" in line and "reset handler" in line and "dashboard reset all" in line


def test_the_kill_line_is_warning_not_info(caplog) -> None:
    """The gateway runs at WARNING, so INFO would omit the one line naming who
    fired from the log of every deployment that has the problem."""
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.runtime_ownership"):
        ro.note_runtime_kill(4242, reason="teardown", caller="test")
    levels = {r.levelno for r in caplog.records}
    assert levels == {logging.WARNING}, f"expected one WARNING record, got levels {levels}"


def test_the_kill_line_reads_a_pid_off_an_object(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="kiro_crew.runtime_ownership"):
        ro.note_runtime_kill(_FakeRuntime(pid=5150), reason="teardown", caller="test")
    assert "5150" in "\n".join(r.getMessage() for r in caplog.records)


def test_an_unsignalable_target_is_still_attributed_without_a_pid(caplog) -> None:
    """A stand-in coerces to 1 through ``__index__``, and pid<=1 selects the
    ``kill(0)`` / ``kill(-n)`` group semantics rather than one process -- so the
    pid is reported as absent rather than as 1, and the line is still written."""
    for target in (None, 0, 1, -1, True, "4242", object()):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.runtime_ownership"):
            ro.note_runtime_kill(target, reason="teardown", caller="test")
        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "pid=None" in line, f"{target!r} should report no pid, got: {line}"


# -- the lease and the gate it feeds --


class _FakeLeaseHolder:
    """A provider shape that can hold a lease, recording what it was asked."""

    def __init__(self, runtime: _FakeRuntime, *, owns: bool = True) -> None:
        self._runtime = runtime
        self._owns_runtime = owns
        self._runtime_lease: str | None = None
        self.acquired = 0
        self.released = 0

    async def acquire_runtime_lease(self) -> None:
        self.acquired += 1
        if not self._owns_runtime or self._runtime_lease is not None:
            return
        runtime = self._runtime

        async def _already_spawned() -> object:
            return runtime

        acquisition = await ro.RUNTIME_OWNERSHIP.acquire(
            runtime, "sess", _already_spawned, cap=ro.CHAT_RUNTIME_CAP
        )
        self._runtime_lease = acquisition.lease

    async def release_runtime_lease(self) -> None:
        self.released += 1
        lease = self._runtime_lease
        if lease is None:
            return
        self._runtime_lease = None
        await ro.RUNTIME_OWNERSHIP.release(lease)


class _Wrapper:
    """The outer provider: it holds the leasing one at ``_client``."""

    def __init__(self, inner: object) -> None:
        self._client = inner


@pytest.mark.asyncio
async def test_a_leased_runtime_refuses_a_kill_and_an_unleased_one_allows_it() -> None:
    """The whole point of the table: the gate's answer must change with it."""
    ro._reset_for_tests()
    runtime = _FakeRuntime(pid=5150)
    holder = _FakeLeaseHolder(runtime)
    assert ro.authorize_runtime_kill(runtime, reason="before", caller="t") is True
    await holder.acquire_runtime_lease()
    assert (
        ro.authorize_runtime_kill(runtime, reason="leased", caller="t") is False
    ), "a live tenant holds this process; a kill must be refused"
    await holder.release_runtime_lease()
    assert ro.authorize_runtime_kill(runtime, reason="after", caller="t") is True
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_the_helpers_find_the_holder_through_the_outer_provider() -> None:
    """Both shapes reach the kill paths, so both must be leasable."""
    ro._reset_for_tests()
    runtime = _FakeRuntime(pid=5151)
    inner = _FakeLeaseHolder(runtime)
    outer = _Wrapper(inner)
    await ro.acquire_session_lease(outer)
    assert inner.acquired == 1, "the lease lives on _client, not on the wrapper"
    assert ro.authorize_runtime_kill(runtime, reason="leased", caller="t") is False
    await ro.release_session_lease(outer)
    assert inner.released == 1
    assert ro.authorize_runtime_kill(runtime, reason="free", caller="t") is True
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_shape_with_no_lease_methods_is_skipped_not_an_error() -> None:
    """Non-ACP backends and the pre-startup placeholder client reach these calls."""
    ro._reset_for_tests()
    await ro.acquire_session_lease(object())
    await ro.release_session_lease(object())
    await ro.acquire_session_lease(_Wrapper(object()))
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_subagent_never_takes_a_lease() -> None:
    """It is handed a runtime it did not spawn and must not kill, so a lease of
    its own would refuse the owner's legitimate teardown."""
    ro._reset_for_tests()
    runtime = _FakeRuntime(pid=5152)
    await ro.acquire_session_lease(_FakeLeaseHolder(runtime, owns=False))
    assert ro.authorize_runtime_kill(runtime, reason="owner", caller="t") is True
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_releasing_twice_cannot_drop_a_later_tenants_lease() -> None:
    """A teardown racing the dashboard's reset calls release more than once."""
    ro._reset_for_tests()
    runtime = _FakeRuntime(pid=5153)
    first = _FakeLeaseHolder(runtime)
    await first.acquire_runtime_lease()
    await first.release_runtime_lease()
    second = _FakeLeaseHolder(runtime)
    await second.acquire_runtime_lease()
    await first.release_runtime_lease()
    assert (
        ro.authorize_runtime_kill(runtime, reason="second", caller="t") is False
    ), "the second tenant's lease must survive the first one's repeated release"
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_the_refusal_names_the_pid_the_caller_and_the_reason() -> None:
    """The refusal log is the only record that a kill was stopped."""
    ro._reset_for_tests()
    runtime = _FakeRuntime(pid=5154)
    holder = _FakeLeaseHolder(runtime)
    await holder.acquire_runtime_lease()
    logger = logging.getLogger("kiro_crew.runtime_ownership")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        assert ro.authorize_runtime_kill(runtime, reason="sweep", caller="reaper") is False
    finally:
        logger.removeHandler(handler)
    assert records and records[0].levelno == logging.WARNING
    text = records[0].getMessage()
    assert "5154" in text and "reaper" in text and "sweep" in text
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_mock_shaped_provider_is_not_mistaken_for_a_lease_holder() -> None:
    """Mocked providers flow through these kill paths all over the suite.

    A ``MagicMock`` answers every ``hasattr`` and returns a ``MagicMock`` from the
    call, which is not awaitable -- so recognising a holder by its methods turns
    every mocked provider on a release path into a TypeError at the await.
    """
    from unittest.mock import MagicMock

    await ro.release_session_lease(MagicMock())
    await ro.acquire_session_lease(MagicMock())


@pytest.mark.asyncio
async def test_a_holder_that_currently_leases_nothing_is_still_a_holder() -> None:
    """An empty slot means "no lease yet", not "cannot hold one" -- otherwise the
    first acquire on every session would be skipped."""
    ro._reset_for_tests()
    runtime = _FakeRuntime(pid=5155)
    holder = _FakeLeaseHolder(runtime)
    assert holder._runtime_lease is None
    await ro.acquire_session_lease(holder)
    assert holder.acquired == 1 and isinstance(holder._runtime_lease, str)
    await ro.release_session_lease(holder)
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_last_release_leaves_nothing_behind_in_the_registry() -> None:
    """A released entry must be unreachable, not merely lease-free.

    The sticky index maps a session key to the entry it last used, so an entry
    left in it holds that entry and its runtime for the gateway's life. At
    ``cap=1`` every release is a last release, which makes one stranded row per
    ordinary session teardown, unbounded over uptime.
    """
    ro._reset_for_tests()
    runtime = _FakeRuntime(pid=5160)
    holder = _FakeLeaseHolder(runtime)
    await holder.acquire_runtime_lease()
    reg = ro.RUNTIME_OWNERSHIP
    assert reg._entries and reg._by_lease
    await holder.release_runtime_lease()
    assert not reg._entries, "the entry is still registered after its last lease left"
    assert not reg._by_lease, "the lease index still points at a released entry"
    assert not reg._last_for_session, (
        "the session index still holds the released entry, pinning its runtime "
        "for the life of the gateway"
    )
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_many_sessions_releasing_leaves_the_registry_empty() -> None:
    """The leak is per session key, so one key cannot show it growing."""
    ro._reset_for_tests()
    reg = ro.RUNTIME_OWNERSHIP
    for n in range(6):
        holder = _FakeLeaseHolder(_FakeRuntime(pid=5200 + n))
        holder_key = f"slack:{n}"

        async def _spawned(rt: object = holder._runtime) -> object:
            return rt

        acq = await reg.acquire(holder._runtime, holder_key, _spawned, cap=ro.CHAT_RUNTIME_CAP)
        await reg.release(acq.lease)
    assert (
        len(reg._last_for_session) == 0
    ), f"{len(reg._last_for_session)} session row(s) stranded after 6 teardowns"
    assert not reg._entries and not reg._by_lease
    ro._reset_for_tests()


# -- tenancy: the claim of a party that uses a process it may not end --


class _FakeSharedRuntime:
    """A shared runtime that records the kills it is asked to perform."""

    def __init__(self, pid: int = 6000, *, alive: bool = True) -> None:
        self.pid = pid
        self._alive = alive
        self.kills: list[str] = []

    def is_alive(self) -> bool:
        return self._alive

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        self.kills.append(reason)
        self._alive = False


class _FakeMintClient:
    """The mint's child, which publishes its pid privately and its own liveness."""

    def __init__(self, pid: int = 6100, *, alive: bool = True) -> None:
        self._pid = pid
        self._alive = alive

    def is_process_alive(self) -> bool:
        return self._alive


def test_a_tenancy_refuses_a_kill_the_lease_table_alone_would_allow() -> None:
    """The gate's second question. Nothing leases this process, so the lease table
    says go ahead -- and a subagent is mid-turn on it."""
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6001)
    assert ro.authorize_runtime_kill(runtime, reason="before", caller="t") is True
    claim = ro.claim_runtime_tenancy(runtime, holder="subagent:sub-1")
    assert isinstance(claim, str)
    assert (
        ro.authorize_runtime_kill(runtime, reason="drain", caller="t") is False
    ), "a subagent is using this process; a kill must be refused"
    ro.release_runtime_tenancy(claim)
    assert ro.authorize_runtime_kill(runtime, reason="after", caller="t") is True
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_tenancy_survives_the_principals_last_lease_release() -> None:
    """The exact window the subagent was undefended in.

    A principal's last release forgets the entry and hands the runtime to its
    owner to kill. A claim stored on that entry would be destroyed by the event it
    exists to survive, so the subagent mid-turn would be signalled anyway.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6002)
    principal = _FakeLeaseHolder(runtime)  # type: ignore[arg-type]
    await principal.acquire_runtime_lease()
    claim = ro.claim_runtime_tenancy(runtime, holder="subagent:sub-1")
    await principal.release_runtime_lease()
    assert not ro.RUNTIME_OWNERSHIP._entries, "the principal's entry is gone, as designed"
    assert (
        ro.authorize_runtime_kill(runtime, reason="provider shutdown", caller="t") is False
    ), "the principal left but its subagent is still running on the process"
    ro.release_runtime_tenancy(claim)
    ro._reset_for_tests()


def test_the_first_subagent_to_finish_does_not_strip_the_seconds_defence() -> None:
    """Two subagents legitimately share one principal's runtime."""
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6003)
    first = ro.claim_runtime_tenancy(runtime, holder="subagent:a")
    second = ro.claim_runtime_tenancy(runtime, holder="subagent:b")
    ro.release_runtime_tenancy(first)
    assert (
        ro.authorize_runtime_kill(runtime, reason="drain", caller="t") is False
    ), "the second subagent is still mid-turn"
    ro.release_runtime_tenancy(second)
    assert ro.authorize_runtime_kill(runtime, reason="drain", caller="t") is True
    ro._reset_for_tests()


def test_the_last_tenant_out_is_handed_a_runtime_whose_kill_was_refused() -> None:
    """Without the hand-back the refusal becomes a permanent leak.

    The principal's teardown was refused and returned without signalling, so no
    session owns this process, the sweep skips it because the runtime still holds
    its own shield, and no later caller has a reason to visit it.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6004)
    first = ro.claim_runtime_tenancy(runtime, holder="subagent:a")
    second = ro.claim_runtime_tenancy(runtime, holder="subagent:b")
    # The principal tries to end it and is refused -- the evidence a kill is owed.
    assert ro.authorize_runtime_kill(runtime, reason="provider shutdown", caller="t") is False
    assert ro.release_runtime_tenancy(first) is None, "a sibling tenant is still on it"
    assert (
        ro.release_runtime_tenancy(second) is runtime
    ), "the last tenant off a runtime whose kill was refused must be handed it"
    ro._reset_for_tests()


def test_refusal_debt_does_not_migrate_to_a_later_process_reusing_the_pid() -> None:
    """A pid is a number the OS hands out again; the debt must not follow it.

    The refused teardown is settled by the tenant that leaves last, and once it
    is settled nothing is owed. A second runtime that later holds the same pid is
    a different process with its own tenants, and a turn finishing on it must not
    be read as the closing act of somebody else's refused kill.
    """
    ro._reset_for_tests()
    doomed = _FakeSharedRuntime(pid=6011)
    claim = ro.claim_runtime_tenancy(doomed, holder="subagent:a")
    assert ro.authorize_runtime_kill(doomed, reason="provider shutdown", caller="t") is False
    assert ro.release_runtime_tenancy(claim) is doomed, "the debt is settled here"

    reborn = _FakeSharedRuntime(pid=6011)
    again = ro.claim_runtime_tenancy(reborn, holder="subagent:b")
    assert (
        ro.release_runtime_tenancy(again) is None
    ), "a healthy runtime that merely inherited the pid must survive its turn"
    ro._reset_for_tests()


def test_a_dead_claims_debt_is_not_inherited_by_a_process_reusing_the_pid() -> None:
    """The debt is inheritable only while the process that earned it is alive.

    The sibling case above settles the debt before the pid is reused. Here the
    refused process exits while its claim is still held -- the unpaired claim the
    module documents as a live failure mode -- so the mark stays resident on a
    record that now matches nothing but a recycled number. Inheriting it would
    arm an unrelated runtime's tenant to hand its own healthy process back for a
    teardown that was never about it, and the dead claim's own teardown has
    nothing left to perform.
    """
    ro._reset_for_tests()
    doomed = _FakeSharedRuntime(pid=6021)
    leaked = ro.claim_runtime_tenancy(doomed, holder="subagent:a")
    assert ro.authorize_runtime_kill(doomed, reason="provider shutdown", caller="t") is False
    doomed._alive = False  # the process exits; its claim is never released

    reborn = _FakeSharedRuntime(pid=6021)
    fresh = ro.claim_runtime_tenancy(reborn, holder="subagent:b")
    assert (
        ro.release_runtime_tenancy(fresh) is None
    ), "a live runtime must not be handed back for a dead claim's refused kill"
    assert reborn.kills == [], "the unrelated process must survive"
    assert leaked is not None
    ro._reset_for_tests()


def test_a_refusal_is_not_recorded_for_a_process_that_has_no_tenant() -> None:
    """The count and the debt are one act, so an allowed kill leaves no debt.

    Read separately, the count and the record straddle the last tenant's exit: the
    gate counts one tenant, that tenant leaves, and the debt is written for a
    process nobody is defending -- to be spent by whoever claims the pid next.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6012)
    assert ro.authorize_runtime_kill(runtime, reason="t", caller="t") is True, "no tenant, no bar"
    claim = ro.claim_runtime_tenancy(runtime, holder="subagent:a")
    assert (
        ro.release_runtime_tenancy(claim) is None
    ), "an allowed kill owes nothing, so a later turn must not be handed the runtime"
    ro._reset_for_tests()


def test_a_turn_starting_after_the_refusal_still_settles_it() -> None:
    """The debt belongs to the process, so it passes to whoever leaves last.

    The refused teardown does not return, so if the debt left with only the claims
    held at refusal time, a turn that began afterwards would be the last light off
    a runtime nobody owns -- unowned, self-shielded from the sweep, never visited.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6013)
    early = ro.claim_runtime_tenancy(runtime, holder="subagent:a")
    assert ro.authorize_runtime_kill(runtime, reason="provider shutdown", caller="t") is False
    late = ro.claim_runtime_tenancy(runtime, holder="subagent:b")
    assert ro.release_runtime_tenancy(early) is None, "a sibling tenant is still on it"
    assert (
        ro.release_runtime_tenancy(late) is runtime
    ), "the turn that starts after the refusal is still the one that must settle it"
    ro._reset_for_tests()


def test_a_refusal_on_one_runtime_does_not_hand_back_an_unrelated_one() -> None:
    """The debt is scoped to the process the gate was asked about."""
    ro._reset_for_tests()
    refused = _FakeSharedRuntime(pid=6014)
    bystander = _FakeSharedRuntime(pid=6015)
    on_refused = ro.claim_runtime_tenancy(refused, holder="subagent:a")
    on_bystander = ro.claim_runtime_tenancy(bystander, holder="subagent:b")
    assert ro.authorize_runtime_kill(refused, reason="provider shutdown", caller="t") is False
    assert (
        ro.release_runtime_tenancy(on_bystander) is None
    ), "nobody asked for the bystander to die, so its tenant leaving must not end it"
    assert ro.release_runtime_tenancy(on_refused) is refused
    ro._reset_for_tests()


def test_a_tenant_leaving_a_runtime_nobody_tried_to_kill_is_handed_nothing() -> None:
    """The defect this guards is severe and not hypothetical: NO shared runtime
    holds a lease. The companion runtime is spawned bare, the task-run path forces
    ``_owns_runtime = False``, and ``acquire_session_lease`` is reached only from
    chat registration. So inferring abandonment from lease-absence would hand back
    every healthy shared runtime and kill a task run's process at the end of its
    first step, with the run-scoped map still pointing at it.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6009)
    assert ro.outstanding_leases(runtime) == 0, "a shared runtime is unleased, as in production"
    claim = ro.claim_runtime_tenancy(runtime, holder="subagent:a")
    assert (
        ro.release_runtime_tenancy(claim) is None
    ), "nobody asked for this runtime to die, so finishing a turn must not end it"
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_tenant_leaving_a_still_leased_runtime_is_handed_nothing() -> None:
    """The ordinary case: the principal is alive and keeps its process."""
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6005)
    principal = _FakeLeaseHolder(runtime)  # type: ignore[arg-type]
    await principal.acquire_runtime_lease()
    claim = ro.claim_runtime_tenancy(runtime, holder="subagent:a")
    assert (
        ro.release_runtime_tenancy(claim) is None
    ), "a subagent finishing must never be handed its principal's live runtime"
    assert ro.authorize_runtime_kill(runtime, reason="t", caller="t") is False
    await principal.release_runtime_lease()
    ro._reset_for_tests()


def test_an_authorized_kill_is_abandoned_when_a_tenant_claims_after_the_verdict() -> None:
    """The gate's verdict is computed on an executor thread and consumed after a
    start-id read, a group resolution and an unbounded descendant walk, while a
    shared turn claims on the event loop. A claim landing in that window is
    invisible to the verdict, and the turn it belongs to dies.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6010)
    assert ro.authorize_runtime_kill(runtime, reason="drain", caller="t") is True
    token = ro.tenancy_epoch(runtime)
    claim = ro.claim_runtime_tenancy(runtime, holder="subagent:late")
    assert (
        ro.commit_runtime_teardown(runtime, token) is False
    ), "a turn started after the verdict; the signal must be abandoned"
    ro.release_runtime_tenancy(claim)
    assert (
        ro.commit_runtime_teardown(runtime, token) is False
    ), "a turn that came and went still means the verdict describes a stale state"
    ro._reset_for_tests()


def test_a_committed_teardown_refuses_a_tenancy_instead_of_granting_one() -> None:
    """The first signal cannot be recalled, so narrowing the window is not closing
    it. Once the killer commits, a claim on that pid must be REFUSED -- granting one
    would hand its holder a defence against a signal already delivered, and the turn
    would die on a process it believed was protected.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6410)
    assert ro.authorize_runtime_kill(runtime, reason="drain", caller="t") is True
    token = ro.tenancy_epoch(runtime)
    assert ro.commit_runtime_teardown(runtime, token) is True, "no tenant stands in the way"
    with pytest.raises(ro.RuntimeTeardownCommitted):
        ro.claim_runtime_tenancy(runtime, holder="subagent:during-the-grace")
    assert ro.RUNTIME_TENANCY.claims_on(runtime) == 0, "the refused claim took no slot"
    ro._reset_for_tests()


def test_the_teardown_barrier_is_dropped_so_a_later_process_can_be_claimed() -> None:
    """A barrier left standing refuses that pid's tenancies for the life of the
    gateway, which is exactly the leak that made a reservation released at each of
    the killer's early exits the wrong shape. One release covers every exit.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6411)
    token = ro.tenancy_epoch(runtime)
    assert ro.commit_runtime_teardown(runtime, token) is True
    ro.release_runtime_teardown(runtime)
    handle = ro.claim_runtime_tenancy(runtime, holder="subagent:after")
    assert handle is not None, "the pid is claimable again once the teardown is over"
    assert ro.release_runtime_teardown(runtime) is None, "dropping a barrier twice is silent"
    ro.release_runtime_tenancy(handle)
    ro._reset_for_tests()


def test_a_live_tenant_stops_the_teardown_from_committing() -> None:
    """The barrier is not a way around the refusal: a pid with a live tenant must
    not be closed to new ones and then signalled anyway.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6412)
    token = ro.tenancy_epoch(runtime)
    holding = ro.claim_runtime_tenancy(runtime, holder="subagent:mid-turn")
    assert ro.commit_runtime_teardown(runtime, token) is False
    ro.release_runtime_tenancy(holding)
    sibling = ro.claim_runtime_tenancy(runtime, holder="subagent:next")
    assert sibling is not None, "a refused commit leaves no barrier behind"
    ro.release_runtime_tenancy(sibling)
    ro._reset_for_tests()


def test_a_dead_targets_claim_stops_defending_its_pid() -> None:
    """Refusing to signal an exited process suppresses the reap of its zombie and
    the sweep of the descendants that escaped its group."""
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6006)
    ro.claim_runtime_tenancy(runtime, holder="subagent:a")
    assert ro.authorize_runtime_kill(runtime, reason="t", caller="t") is False
    runtime._alive = False
    assert (
        ro.authorize_runtime_kill(runtime, reason="t", caller="t") is True
    ), "a claim on a dead process must not defend it"
    assert ro.RUNTIME_TENANCY.claims_on(6006) == 0
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_tenancy_neither_consumes_the_cap_nor_moves_a_session() -> None:
    """1:1 behaviour is unchanged: the lease table never consults tenancy, so a
    runtime serving one session behaves exactly as it did before this table."""
    ro._reset_for_tests()
    reg = ro.RUNTIME_OWNERSHIP
    runtime = _FakeSharedRuntime(pid=6007)
    ro.claim_runtime_tenancy(runtime, holder="subagent:a")
    ro.claim_runtime_tenancy(runtime, holder="subagent:b")

    async def _spawned() -> object:
        return runtime

    acq = await reg.acquire(runtime, "chat:1", _spawned, cap=ro.CHAT_RUNTIME_CAP)
    assert acq.joined is False and acq.leases_on_runtime == 1, (
        "two tenancies must not count against the cap: the principal still founds "
        "its own entry with exactly one lease"
    )
    again = await reg.acquire(runtime, "chat:1", _spawned, cap=ro.CHAT_RUNTIME_CAP)
    assert again.joined is False, "placement is unchanged: at cap=1 a second acquire spawns"
    assert await reg.release(acq.lease) is runtime
    assert await reg.release(again.lease) is runtime
    ro._reset_for_tests()


def test_the_tenancy_refusal_names_its_holders(caplog) -> None:
    """A refusal is only actionable if it says who is keeping the process alive."""
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6008)
    ro.claim_runtime_tenancy(runtime, holder="subagent:chat-7")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.runtime_ownership"):
        assert ro.authorize_runtime_kill(runtime, reason="reset all", caller="dash") is False
    line = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "subagent:chat-7" in line and "6008" in line and "dash" in line
    ro._reset_for_tests()


def test_an_unsignalable_pid_cannot_be_claimed_and_releasing_nothing_is_safe() -> None:
    """A pid of 1 or below selects the kill-group semantics rather than one
    process, so there is nothing here to defend."""
    ro._reset_for_tests()
    for target in (0, 1, -1, True):
        assert ro.claim_runtime_tenancy(target, holder="t") is None
    assert ro.release_runtime_tenancy(None) is None
    assert ro.release_runtime_tenancy("not-a-live-handle") is None
    ro._reset_for_tests()


def test_a_target_with_no_pid_cannot_be_claimed() -> None:
    """Mocked providers flow through the turn paths all over the suite, and a
    ``MagicMock`` answers every attribute -- so a claim that accepted a target
    without a resolvable pid would defend every mocked runtime and then hand the
    "orphan" back to be killed, turning an inert stand-in into an unexpected kill
    call. A tenant is mid-flight on a running process; no pid, nothing to defend.
    """
    from unittest.mock import MagicMock

    ro._reset_for_tests()
    assert ro.claim_runtime_tenancy(MagicMock(), holder="subagent:mocked") is None
    assert ro.claim_runtime_tenancy(object(), holder="t") is None
    mock = MagicMock()
    assert ro.RUNTIME_TENANCY.claims_on(mock) == 0
    assert ro.authorize_runtime_kill(mock, reason="t", caller="t") is True
    ro._reset_for_tests()


def test_a_claim_on_a_client_shaped_target_is_answerable_by_bare_pid() -> None:
    """``_sync_kill_provider`` asks the gate with a BARE PID, and the ACP client
    publishes its pid only privately -- so a claim that failed to record one would
    be invisible to the very killer this table exists to refuse."""
    ro._reset_for_tests()
    client = _FakeMintClient(pid=6109)
    claim = ro.claim_runtime_tenancy(client, holder="connections.mint")
    assert isinstance(claim, str)
    assert (
        ro.authorize_runtime_kill(6109, reason="leaked provider teardown", caller="t") is False
    ), "the drain asks by pid; a claim on the client object must still answer"
    ro.release_runtime_tenancy(claim)
    assert ro.authorize_runtime_kill(6109, reason="t", caller="t") is True
    ro._reset_for_tests()


# -- the two callers: a subagent's turn and the OAuth mint child --


def _shared_provider(runtime: object, *, owns: bool) -> object:
    from kiro_crew.acp.session_provider import AcpSessionProvider

    return AcpSessionProvider(
        object(),  # type: ignore[arg-type]
        runtime,  # type: ignore[arg-type]
        owns_runtime=owns,
        session_key="chat-7:sub-1",
    )


@pytest.mark.asyncio
async def test_a_subagent_turn_defends_the_shared_runtime() -> None:
    """A session-sharing subagent takes no lease, so once the principal releases
    its own, nothing in the gate's registry says the process is still in use and a
    drain is free to SIGTERM a live turn."""
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6200)
    provider = _shared_provider(runtime, owns=False)
    claim = provider._claim_shared_turn()  # type: ignore[attr-defined]
    assert isinstance(claim, str)
    assert ro.authorize_runtime_kill(runtime, reason="reset all", caller="dash") is False
    await provider._end_shared_turn(claim)  # type: ignore[attr-defined]
    assert ro.authorize_runtime_kill(runtime, reason="reset all", caller="dash") is True
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_an_owning_provider_takes_no_turn_tenancy() -> None:
    """It already holds a lease for its whole session, so a turn claim would
    defend a defended process and refuse its owner's teardown until the turn
    ended."""
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6201)
    provider = _shared_provider(runtime, owns=True)
    assert provider._claim_shared_turn() is None  # type: ignore[attr-defined]
    assert ro.RUNTIME_TENANCY.claims_on(runtime) == 0
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_subagent_finishing_after_a_refused_kill_ends_the_runtime() -> None:
    """The principal's teardown was refused on this turn's behalf, so the tenant
    that just finished is the only party that will ever visit the process again."""
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6202)
    provider = _shared_provider(runtime, owns=False)
    claim = provider._claim_shared_turn()  # type: ignore[attr-defined]
    assert ro.authorize_runtime_kill(runtime, reason="reset all", caller="dash") is False
    await provider._end_shared_turn(claim)  # type: ignore[attr-defined]
    assert runtime.kills, "a runtime whose refused kill nobody else can finish must be ended"
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_subagent_turn_on_an_unleased_runtime_does_not_end_it() -> None:
    """The ordinary shared-runtime turn, and the severe case to get right.

    A task run's runtime and a companion subagent runtime hold NO lease, so a
    hand-back keyed on lease-absence would kill the process at the end of every
    single turn -- the first step of a task run would take down the runtime its own
    run-scoped map still points at, and a sibling subagent between turns would lose
    its process mid-run.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6205)
    assert ro.outstanding_leases(runtime) == 0
    provider = _shared_provider(runtime, owns=False)
    for _ in range(3):
        claim = provider._claim_shared_turn()  # type: ignore[attr-defined]
        await provider._end_shared_turn(claim)  # type: ignore[attr-defined]
    assert not runtime.kills, "three ordinary turns must leave the shared runtime running"
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_turn_on_a_committed_teardown_is_told_the_process_is_gone() -> None:
    """The one tenancy refusal a turn must NOT shrug off.

    A committed teardown means the signal has left and no claim can defend against
    it. Returning None here -- the answer for "nothing to defend" -- would run the
    turn on a corpse and surface as a mid-stream death with no cause attached, so
    it is translated into the same error a dead runtime raises, which callers
    already handle by getting another runtime.
    """
    from kiro_crew.acp.client import AcpProcessDied

    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6206)
    provider = _shared_provider(runtime, owns=False)
    token = ro.tenancy_epoch(runtime)
    assert ro.commit_runtime_teardown(runtime, token) is True
    with pytest.raises(AcpProcessDied):
        provider._claim_shared_turn()  # type: ignore[attr-defined]
    assert ro.RUNTIME_TENANCY.claims_on(runtime) == 0, "the refused turn took no claim"
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_subagent_leaves_a_live_principals_runtime_running() -> None:
    """The ordinary end of a turn must not kill the process the parent is on."""
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=6203)
    principal = _FakeLeaseHolder(runtime)  # type: ignore[arg-type]
    await principal.acquire_runtime_lease()
    provider = _shared_provider(runtime, owns=False)
    claim = provider._claim_shared_turn()  # type: ignore[attr-defined]
    await provider._end_shared_turn(claim)  # type: ignore[attr-defined]
    assert not runtime.kills, "the principal still leases this runtime"
    await principal.release_runtime_lease()
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_provider_assembled_without_init_is_inert_in_the_turn_bookkeeping() -> None:
    """A turn must never fail because bookkeeping could not be done.

    The unit tests of this class's exception translation build a provider with
    ``__new__`` and set only ``_handle`` and ``_runtime``, then drive ``stream`` --
    so it carries no ownership flag, no session key and no lease slot at all.
    Reading that state unguarded raises ``AttributeError`` out of the claim and
    into the caller's turn, which is a crash where the worst honest outcome is
    simply not claiming.
    """
    from kiro_crew.acp.session_provider import AcpSessionProvider

    ro._reset_for_tests()
    partial = AcpSessionProvider.__new__(AcpSessionProvider)
    partial._runtime = _FakeSharedRuntime(pid=6204)  # type: ignore[assignment]
    assert partial._claim_shared_turn() is None
    await partial._end_shared_turn(None)
    await partial._end_shared_turn("not-a-live-handle")
    assert ro.RUNTIME_TENANCY.claims_on(6204) == 0
    ro._reset_for_tests()


def test_the_mint_child_is_defended_from_the_provider_drain() -> None:
    """A Connect click routes through the session reset, whose provider drain
    reaches ``_sync_kill_provider`` and can SIGTERM the mint child that same click
    spawned -- ~3s in, before the token exchange persists the grant, after which
    Connect waits out its whole TTL for a grant file that never appears."""
    from kiro_crew.connections.mint import _claim_mint_pid

    ro._reset_for_tests()
    holdings: dict[str, object] = {}
    client = _FakeMintClient(pid=6300)
    from kiro_crew import session_pid

    try:
        assert _claim_mint_pid(client, holdings) is True  # type: ignore[arg-type]
        assert holdings["pid"] == 6300
        assert isinstance(holdings.get("tenancy"), str) and holdings["tenancy"]
        assert (
            ro.authorize_runtime_kill(6300, reason="leaked provider teardown", caller="drain")
            is False
        ), "the mint child is mid-exchange; the drain must be refused"
        ro.release_runtime_tenancy(holdings["tenancy"])  # type: ignore[arg-type]
        assert (
            ro.authorize_runtime_kill(6300, reason="mint teardown", caller="mint") is True
        ), "the mint's own teardown releases first and must then be authorized"
    finally:
        while 6300 in session_pid._protected_pids():
            session_pid.unregister_protected_pid(6300)
        ro._reset_for_tests()


def test_the_mint_claim_is_idempotent_across_poll_rounds() -> None:
    """``_claim_mint_pid_when_spawned`` polls until a pid appears, so a second
    round must not take a second claim that the single release cannot pay off."""
    from kiro_crew.connections.mint import _claim_mint_pid

    ro._reset_for_tests()
    from kiro_crew import session_pid

    holdings: dict[str, object] = {}
    client = _FakeMintClient(pid=6301)
    try:
        _claim_mint_pid(client, holdings)  # type: ignore[arg-type]
        first = holdings["tenancy"]
        _claim_mint_pid(client, holdings)  # type: ignore[arg-type]
        assert holdings["tenancy"] == first
        assert ro.RUNTIME_TENANCY.claims_on(6301) == 1
        ro.release_runtime_tenancy(first)  # type: ignore[arg-type]
        assert ro.authorize_runtime_kill(6301, reason="t", caller="t") is True
    finally:
        while 6301 in session_pid._protected_pids():
            session_pid.unregister_protected_pid(6301)
        ro._reset_for_tests()


def test_a_respawned_mint_child_is_reshielded_and_the_dead_pid_released() -> None:
    """``ensure_ready`` retries a transient spawn failure in its own two-attempt
    loop, so the client can be on its SECOND child by the time readiness returns.
    A write-once claim would hold the dead first number and leave the replacement
    naked for the whole mint TTL, reachable by the orphan sweep and by the drain's
    kill gate alike, with no re-claim anywhere in the flow to correct it."""
    from kiro_crew.connections.mint import _claim_mint_pid

    ro._reset_for_tests()
    from kiro_crew import session_pid

    holdings: dict[str, object] = {}
    client = _FakeMintClient(pid=6302)
    try:
        _claim_mint_pid(client, holdings)  # type: ignore[arg-type]
        stale = holdings["tenancy"]

        client._pid = 6303  # the respawn inside ensure_ready

        assert _claim_mint_pid(client, holdings) is True  # type: ignore[arg-type]
        assert holdings["pid"] == 6303, "the holding must follow the live child"
        assert holdings["tenancy"] != stale

        assert (
            ro.authorize_runtime_kill(6303, reason="leaked provider teardown", caller="drain")
            is False
        ), "the REPLACEMENT child is mid-exchange; the drain must be refused"
        assert 6303 in session_pid._protected_pids(), "the sweep shield must move too"

        assert ro.RUNTIME_TENANCY.claims_on(6302) == 0, "the dead number must not stay claimed"
        assert 6302 not in session_pid._protected_pids()
        assert (
            ro.authorize_runtime_kill(6302, reason="t", caller="t") is True
        ), "a recycled pid must not inherit the dead child's shield"

        ro.release_runtime_tenancy(holdings["tenancy"])  # type: ignore[arg-type]
        assert ro.authorize_runtime_kill(6303, reason="mint teardown", caller="mint") is True
    finally:
        for pid in (6302, 6303):
            while pid in session_pid._protected_pids():
                session_pid.unregister_protected_pid(pid)
        ro._reset_for_tests()


def test_a_mint_client_that_has_not_spawned_keeps_an_existing_holding() -> None:
    """The reconcile reads the client FIRST, so a pid cleared by a teardown must
    not be read as "nothing is held" -- the recorded holding is what
    ``_dispose_mint`` releases, and dropping it would leak both shields."""
    from kiro_crew.connections.mint import _claim_mint_pid

    ro._reset_for_tests()
    from kiro_crew import session_pid

    holdings: dict[str, object] = {}
    client = _FakeMintClient(pid=6304)
    try:
        _claim_mint_pid(client, holdings)  # type: ignore[arg-type]
        held = holdings["tenancy"]
        client._pid = 0
        assert _claim_mint_pid(client, holdings) is True  # type: ignore[arg-type]
        assert holdings["pid"] == 6304 and holdings["tenancy"] == held
        assert ro.RUNTIME_TENANCY.claims_on(6304) == 1, "exactly one claim, still payable"
    finally:
        while 6304 in session_pid._protected_pids():
            session_pid.unregister_protected_pid(6304)
        ro._reset_for_tests()


def test_a_mint_claim_on_a_committed_teardown_records_no_tenancy_and_says_so() -> None:
    """Both mint paths call the reconcile from a ``finally``, so this refusal must
    not be raised: it would replace whatever exception the exchange was already
    failing with. It records no tenancy, keeps the sweep shield -- a different
    reaper, which the teardown in progress does not consult -- and names the reason.
    """
    from kiro_crew.connections.mint import _claim_mint_pid

    ro._reset_for_tests()
    from kiro_crew import session_pid

    holdings: dict[str, object] = {}
    client = _FakeMintClient(pid=6305)
    try:
        token = ro.tenancy_epoch(6305)
        assert ro.commit_runtime_teardown(6305, token) is True
        assert _claim_mint_pid(client, holdings) is True  # type: ignore[arg-type]
        assert holdings["tenancy"] == "", "a refused claim must not be recorded as one"
        assert ro.RUNTIME_TENANCY.claims_on(6305) == 0
        assert 6305 in session_pid._protected_pids(), "the sweep shield is a separate defence"
    finally:
        while 6305 in session_pid._protected_pids():
            session_pid.unregister_protected_pid(6305)
        ro._reset_for_tests()


def test_an_unspawned_mint_client_claims_nothing_and_keeps_polling() -> None:
    """The poller's exit condition. A client with no pid yet must answer False so
    ``_claim_mint_pid_when_spawned`` keeps polling rather than returning early."""
    from kiro_crew.connections.mint import _claim_mint_pid

    ro._reset_for_tests()
    holdings: dict[str, object] = {}
    assert _claim_mint_pid(_FakeMintClient(pid=0), holdings) is False  # type: ignore[arg-type]
    assert not holdings
    ro._reset_for_tests()
