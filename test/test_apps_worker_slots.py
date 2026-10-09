"""Tests for the supported app worker-slot seam (kiro_crew.apps.worker_slots)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest

from kiro_crew.apps import worker_slots
from kiro_crew.apps.worker_slots import (
    DEFAULT_WORKER_SLOT_LIMIT,
    MAX_TRUST_TTL_SECS,
    WorkerSlotTimeout,
    acquire_worker_slot,
    set_worker_slot_limit,
    worker_trust_scope,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.testing.wait import async_wait_until


class _Slot:
    def __init__(self, key: str) -> None:
        self.key = key
        self._app = ""
        self.project = ""
        self._trust = False
        self._trust_scope = ""
        self._trusted_patterns: set[str] = set()


class _State:
    """Minimal stand-in for the dashboard state's slot registry."""

    def __init__(self) -> None:
        self.slots: dict[str, _Slot] = {}
        self.pushes = 0

    def get_or_create_slot(self, name, agent="", app="", model=""):
        slot = self.slots.get(name)
        if slot is None:
            slot = self.slots[name] = _Slot(name)
        return slot

    def get_slot(self, name):
        return self.slots.get(name)

    def push_slots_update(self) -> None:
        self.pushes += 1

    def recreate(self, name: str) -> _Slot:
        """Create the slot again the way a non-app create path does: no stamps."""
        slot = self.slots[name] = _Slot(name)
        return slot


class _FakeOverride:
    """Records scoped grants; ``is_scope_active`` reflects them."""

    def __init__(self, permit: bool = True) -> None:
        self.permit = permit
        self.active: dict[str, int] = {}
        self.calls: list[tuple] = []

    def activate_scoped(self, scope, source, ttl=None):
        self.calls.append(("activate", scope, source, ttl))
        if self.permit:
            self.active[scope] = ttl
        return SimpleNamespace(active=self.permit)

    def deactivate_scope(self, scope):
        self.calls.append(("deactivate", scope))
        self.active.pop(scope, None)

    def is_scope_active(self, scope):
        return scope in self.active


@pytest.fixture(autouse=True)
def _env():
    worker_slots._registry = worker_slots._Registry()
    worker_slots._limits.clear()
    override = _FakeOverride()
    fake_sel = mock.Mock()
    with (
        mock.patch.object(worker_slots, "safety_override", return_value=override),
        # The approval helpers run on chat_runner's globals (chat_turn.compose).
        mock.patch.object(chat_runner, "safety_override", return_value=override),
        mock.patch.object(worker_slots, "sel", return_value=fake_sel),
        mock.patch.object(worker_slots.AuditSDK, "record") as record,
    ):
        yield SimpleNamespace(override=override, sel=fake_sel, record=record)
    worker_slots._registry = worker_slots._Registry()
    worker_slots._limits.clear()


def test_default_limit_is_one():
    assert DEFAULT_WORKER_SLOT_LIMIT == 1
    assert worker_slots._limit("demo") == 1


@pytest.mark.asyncio
async def test_acquire_stamps_app_and_project():
    state = _State()
    lease = await acquire_worker_slot(state, "demo", "demo-1", project="/work/repo")
    slot = state.slots["demo-1"]
    assert lease.slot is slot
    assert slot._app == "demo"
    assert slot.project == "/work/repo"
    assert slot._trust is False
    assert slot._trust_scope == ""
    assert state.pushes == 1
    await lease.release()


@pytest.mark.asyncio
async def test_reacquire_restamps_a_slot_created_without_stamps():
    state = _State()
    lease = await acquire_worker_slot(state, "demo", "demo-1", project="/work/repo")
    await lease.release()
    state.recreate("demo-1")
    lease = await acquire_worker_slot(state, "demo", "demo-1", project="/work/repo")
    assert state.slots["demo-1"]._app == "demo"
    assert state.slots["demo-1"].project == "/work/repo"
    await lease.release()


@pytest.mark.asyncio
async def test_second_acquire_times_out_with_clear_message():
    state = _State()
    first = await acquire_worker_slot(state, "demo", "demo-1", project="/w")
    with pytest.raises(WorkerSlotTimeout) as info:
        await acquire_worker_slot(state, "demo", "demo-2", project="/w", timeout=0.05)
    text = str(info.value)
    assert "'demo'" in text
    assert "1 of its 1 worker slot" in text
    assert "waited 0.05s" in text
    assert "set_worker_slot_limit" in text
    assert "demo-2" not in state.slots, "a timed-out acquire must not create a slot"
    await first.release()


@pytest.mark.asyncio
async def test_same_key_is_leased_once_even_under_a_higher_limit():
    state = _State()
    set_worker_slot_limit("demo", 3)
    first = await acquire_worker_slot(state, "demo", "demo-1", project="/w")
    with pytest.raises(WorkerSlotTimeout) as info:
        await acquire_worker_slot(state, "demo", "demo-1", project="/w", timeout=0.05)
    assert "'demo-1' is already leased" in str(info.value)
    await first.release()


@pytest.mark.asyncio
async def test_waiting_acquire_gets_the_slot_once_released():
    state = _State()
    first = await acquire_worker_slot(state, "demo", "demo-1", project="/w")
    waiter = asyncio.ensure_future(
        acquire_worker_slot(state, "demo", "demo-2", project="/w", timeout=5)
    )
    await asyncio.sleep(0)
    assert not waiter.done()
    await first.release()
    second = await asyncio.wait_for(waiter, 5)
    assert second.slot.key == "demo-2"
    await second.release()


@pytest.mark.asyncio
async def test_limit_is_per_app_and_configurable():
    state = _State()
    a = await acquire_worker_slot(state, "a", "a-1", project="/w")
    b = await acquire_worker_slot(state, "b", "b-1", project="/w", timeout=0.05)
    set_worker_slot_limit("a", 2)
    a2 = await acquire_worker_slot(state, "a", "a-2", project="/w", timeout=0.05)
    with pytest.raises(WorkerSlotTimeout):
        await acquire_worker_slot(state, "a", "a-3", project="/w", timeout=0.05)
    for lease in (a, b, a2):
        await lease.release()


@pytest.mark.asyncio
async def test_raising_the_limit_wakes_a_waiter():
    state = _State()
    first = await acquire_worker_slot(state, "demo", "demo-1", project="/w")
    waiter = asyncio.ensure_future(
        acquire_worker_slot(state, "demo", "demo-2", project="/w", timeout=5)
    )
    await asyncio.sleep(0)
    set_worker_slot_limit("demo", 2)
    second = await asyncio.wait_for(waiter, 5)
    await second.release()
    await first.release()


@pytest.mark.asyncio
async def test_release_is_idempotent_and_context_manager_releases():
    state = _State()
    async with await acquire_worker_slot(state, "demo", "demo-1", project="/w") as lease:
        assert not lease.released
    assert lease.released
    await lease.release()
    again = await acquire_worker_slot(state, "demo", "demo-2", project="/w", timeout=0.05)
    await again.release()
    assert worker_slots._registry.leases == {}


@pytest.mark.asyncio
async def test_failed_slot_creation_frees_the_lease():
    state = _State()
    with mock.patch.object(state, "get_or_create_slot", side_effect=ValueError("busy")):
        with pytest.raises(ValueError):
            await acquire_worker_slot(state, "demo", "demo-1", project="/w")
    lease = await acquire_worker_slot(state, "demo", "demo-1", project="/w", timeout=0.05)
    await lease.release()


@pytest.mark.asyncio
async def test_blanket_trust_is_a_scoped_grant_never_the_session_flag(_env):
    state = _State()
    lease = await acquire_worker_slot(
        state, "demo", "demo-1", project="/w", trust=True, trust_ttl_secs=60
    )
    slot = state.slots["demo-1"]
    scope = worker_trust_scope("demo", "demo-1")
    assert lease.trust_granted
    assert slot._trust is False, "the never-expiring interactive flag must not be written"
    assert slot._trust_scope == scope
    assert _env.override.calls[0] == ("activate", scope, "app:demo", 60)
    assert chat_runner._slot_is_trusted(slot) is True
    # Not cached as a session policy, so subagents spawned by the worker
    # cannot keep auto-approving after the grant ends.
    assert chat_runner._persistable_session_policy(slot, yolo_active=False) == ""
    await lease.release()
    assert ("deactivate", scope) in _env.override.calls
    assert slot._trust_scope == ""
    assert chat_runner._slot_is_trusted(slot) is False


@pytest.mark.asyncio
async def test_refused_scoped_grant_leaves_the_worker_untrusted(_env):
    _env.override.permit = False
    state = _State()
    lease = await acquire_worker_slot(state, "demo", "demo-1", project="/w", trust=True)
    slot = state.slots["demo-1"]
    assert not lease.trust_granted
    assert slot._trust_scope == ""
    assert chat_runner._slot_is_trusted(slot) is False
    await lease.release()


@pytest.mark.asyncio
async def test_patterns_are_audited_before_grant_and_withdrawn_on_release(_env):
    state = _State()
    lease = await acquire_worker_slot(
        state, "demo", "demo-1", project="/w", trusted_patterns=["npm test"]
    )
    slot = state.slots["demo-1"]
    assert slot._trusted_patterns == {"npm test"}
    assert _env.sel.log_api_access.call_args.kwargs["critical"] is True
    await lease.release()
    assert slot._trusted_patterns == set()
    assert [c.args[1] for c in _env.record.call_args_list] == ["revoked"]


@pytest.mark.asyncio
async def test_pattern_grant_is_refused_when_its_audit_fails(_env):
    _env.sel.log_api_access.side_effect = OSError("disk full")
    state = _State()
    lease = await acquire_worker_slot(
        state, "demo", "demo-1", project="/w", trusted_patterns=["npm test"]
    )
    assert state.slots["demo-1"]._trusted_patterns == set()
    await lease.release()


@pytest.mark.asyncio
async def test_trust_expiry_is_scheduled_at_the_ttl_and_withdraws(_env):
    state = _State()
    loop = asyncio.get_running_loop()
    with mock.patch.object(loop, "call_later", wraps=loop.call_later) as spy:
        lease = await acquire_worker_slot(
            state,
            "demo",
            "demo-1",
            project="/w",
            trust=True,
            trusted_patterns=["ls"],
            trust_ttl_secs=120,
        )
    assert spy.call_args.args[:2] == (120, lease._on_trust_expired)
    slot = state.slots["demo-1"]
    # Fire the scheduled callback directly instead of waiting out the TTL.
    lease._on_trust_expired()
    assert slot._trust_scope == ""
    assert slot._trusted_patterns == set()
    assert not lease.trust_granted
    await lease.release()


@pytest.mark.asyncio
async def test_short_ttl_really_fires_on_the_loop():
    state = _State()
    lease = await acquire_worker_slot(
        state, "demo", "demo-1", project="/w", trust=True, trust_ttl_secs=0.01
    )
    slot = state.slots["demo-1"]
    await async_wait_until(lambda: slot._trust_scope == "", timeout=5)
    await lease.release()


@pytest.mark.asyncio
async def test_release_keeps_grants_the_lease_did_not_make(_env):
    state = _State()
    slot = state.get_or_create_slot("demo-1")
    slot._trust = True
    slot._trusted_patterns = {"git status"}
    lease = await acquire_worker_slot(
        state, "demo", "demo-1", project="/w", trust=True, trusted_patterns=["git status", "ls"]
    )
    assert not lease.trust_granted, "a human's session trust needs no extra grant"
    await lease.release()
    assert slot._trust is True
    assert slot._trusted_patterns == {"git status"}


@pytest.mark.asyncio
async def test_another_scope_on_the_slot_is_left_alone(_env):
    state = _State()
    slot = state.get_or_create_slot("demo-1")
    slot._trust_scope = "crew:x:autoapprove"
    lease = await acquire_worker_slot(state, "demo", "demo-1", project="/w", trust=True)
    assert not lease.trust_granted
    await lease.release()
    assert slot._trust_scope == "crew:x:autoapprove"
    assert _env.override.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [0, -1, float("inf"), MAX_TRUST_TTL_SECS + 1])
async def test_unbounded_trust_ttl_is_refused(ttl):
    state = _State()
    with pytest.raises(ValueError):
        await acquire_worker_slot(
            state, "demo", "demo-1", project="/w", trust=True, trust_ttl_secs=ttl
        )
    assert not worker_slots._registry.leases and not worker_slots._registry.pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"app": "", "key": "k", "project": "/w"},
        {"app": "demo", "key": "", "project": "/w"},
        {"app": "demo", "key": "k", "project": ""},
        {"app": "demo", "key": "k", "project": "/w", "timeout": 0},
    ],
)
async def test_bad_arguments_are_refused(kwargs):
    with pytest.raises(ValueError):
        await acquire_worker_slot(_State(), **kwargs)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_bad_limit_is_refused(limit):
    with pytest.raises(ValueError):
        set_worker_slot_limit("demo", limit)


@pytest.mark.asyncio
async def test_another_app_cannot_lease_a_key_already_leased(_env):
    state = _State()
    a = await acquire_worker_slot(state, "a", "worker", project="/a", trust=True)
    with pytest.raises(WorkerSlotTimeout):
        await acquire_worker_slot(state, "b", "worker", project="/b", timeout=0.05)
    slot = state.slots["worker"]
    assert slot._app == "a"
    assert slot.project == "/a"
    await a.release()


@pytest.mark.asyncio
async def test_a_slot_owned_by_another_app_is_refused(_env):
    state = _State()
    a = await acquire_worker_slot(state, "a", "worker", project="/a")
    await a.release()
    with pytest.raises(ValueError, match="belongs to app 'a'"):
        await acquire_worker_slot(state, "b", "worker", project="/b", trust=True)
    assert state.slots["worker"]._app == "a"
    assert _env.override.calls == []
    again = await acquire_worker_slot(state, "b", "b-1", project="/b", timeout=0.05)
    await again.release()


@pytest.mark.asyncio
async def test_a_folded_key_cannot_alias_a_held_lease():
    state = _State()
    folded = _Slot("demo-1")
    first = await acquire_worker_slot(state, "demo", "demo-1", project="/w")
    set_worker_slot_limit("demo", 2)
    with mock.patch.object(state, "get_or_create_slot", return_value=folded):
        with pytest.raises(ValueError, match="already leased"):
            await acquire_worker_slot(state, "demo", "Demo 1", project="/w")
    await first.release()


@pytest.mark.asyncio
async def test_a_deleted_slot_frees_its_lease_for_the_next_acquire(_env):
    state = _State()
    lost = await acquire_worker_slot(state, "demo", "demo-1", project="/w", trust=True)
    del state.slots["demo-1"]
    lease = await acquire_worker_slot(state, "demo", "demo-2", project="/w", timeout=0.05)
    assert lost.released
    assert ("deactivate", worker_trust_scope("demo", "demo-1")) in _env.override.calls
    await lease.release()
