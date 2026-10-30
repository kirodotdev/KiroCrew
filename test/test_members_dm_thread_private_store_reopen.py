"""Crew Members DM threads: opening and reopening a member thread on a private store.

``POST /api/members/{slug}/thread`` for a member with a private memory store names the
actual cause of a conflict and never interrupts or reassigns a running turn. A reopen
whose ownership changes while it reads (member deleted or recreated, store rebound,
binding or store identity altered) is refused; ``running`` is re-checked after the slot
lock and after the assignment await; and a failed pin leaves no ghost slot in the
registry or in a broadcast. Shared fakes live in ``members_dm_thread_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from members_dm_thread_helpers import CREW, OTHER, _make_members_app

from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.members import DM_SLOT_MODE, dm_binding_path, read_dm_binding
from kiro_crew.testing.wait import await_until


async def _finished(coro, what: str, *, timeout: float | None = None):
    """Run ``coro`` under the lost-run bound; fail by name if it hangs (D8)."""
    task = asyncio.ensure_future(coro)
    try:
        return await await_until(task, timeout=timeout, what=what)
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["linked", "store"])
async def test_private_thread_conflict_names_its_actual_cause(tmp_path, monkeypatch, conflict):

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import provision_member_memory

    pass  # Member routing does not depend on OS isolation.

    def configure():
        cfg = KiroCrewConfig.load()
        cfg.agents[CREW] = KiroCrewAgentConfig(kiro_agent=CREW)
        provision_member_memory(cfg, CREW)
        cfg.save()

    await asyncio.to_thread(configure)
    state = _make_state(tmp_path)
    finished = asyncio.Event()
    task = None
    try:
        async with TestClient(TestServer(_make_members_app(state))) as client:
            opened = await client.post(f"/api/members/{CREW}/thread")
            assert opened.status == 200, await opened.text()
            slot = state._slots[(await opened.json())["slot_key"]]
            if conflict == "linked":
                slot.linked_session_key = "slack:other-session"
            else:
                task = asyncio.create_task(finished.wait())
                slot._task = task
                slot.memory_store = ""
            response = await client.post(f"/api/members/{CREW}/thread")
            assert response.status == 409
            body = await response.json()
            assert body["code"] == "member_slot_conflict"
            expected = (
                "the member thread is linked to another session"
                if conflict == "linked"
                else "the member thread has a different member memory assignment"
            )
            assert body["error"] == expected
            assert "running" not in body["error"]
    finally:
        finished.set()
        if task is not None:
            await await_until(task, what="the running turn task")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "running, fault",
    [
        (False, ""),
        (True, ""),
        (True, "linked"),
        (True, "memory"),
        (True, "assignment_missing"),
        (True, "assignment_other"),
        (True, "binding_missing"),
        (True, "binding_other_slot"),
        (True, "race_replaced"),
        (True, "race_linked"),
        (True, "race_agent"),
        (True, "race_mode"),
        (True, "race_memory"),
    ],
)
async def test_reopen_bound_private_thread_preserves_active_turn(
    tmp_path, monkeypatch, running, fault
):
    """Opening the same pinned DM must not interrupt or reassign its active turn."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents[CREW] = KiroCrewAgentConfig(kiro_agent=CREW)
    await asyncio.to_thread(provision_member_memory, cfg, CREW)
    await asyncio.to_thread(cfg.save)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_members_app(state))) as client:
        first = await client.post(f"/api/members/{CREW}/thread")
        assert first.status == 200, await first.text()
        opened = await first.json()
        slot = state._slots[opened["slot_key"]]
        session_key = f"dashboard:{slot.key}"
        assigned = await asyncio.to_thread(read_private_session_store, session_key)
        assert assigned == cfg.agents[CREW].memory_store
        binding = await asyncio.to_thread(read_dm_binding, CREW)
        release = asyncio.Event()
        task = asyncio.create_task(release.wait()) if running else None
        slot.task = task
        if fault == "linked":
            slot.linked_session_key = "dashboard:other-session"
        elif fault == "memory":
            slot.memory_store = "member-other"
        loop = asyncio.get_running_loop()
        raced = threading.Event()

        def change_identity():
            if fault == "race_replaced":
                from kiro_crew.dashboard.state import _ChatSlot

                state._slots[slot.key] = _ChatSlot(slot.key, agent=CREW, mode=DM_SLOT_MODE)
            elif fault == "race_linked":
                slot.linked_session_key = "dashboard:other-session"
            elif fault == "race_agent":
                slot.agent = OTHER
            elif fault == "race_mode":
                slot.mode = ""
            elif fault == "race_memory":
                slot.memory_store = "member-other"
            raced.set()

        def read_assignment(key):
            if key == session_key:
                if fault == "assignment_missing":
                    return None
                if fault == "assignment_other":
                    return "member-other"
                if fault.startswith("race_"):
                    loop.call_soon_threadsafe(change_identity)
                    assert raced.wait(timeout=5)
            return read_private_session_store(key)

        monkeypatch.setattr(
            "kiro_crew.member_memory_auth.read_private_session_store", read_assignment
        )
        if fault in {"binding_missing", "binding_other_slot"}:
            reported_binding = (
                None if fault == "binding_missing" else dict(binding, slot_key="member-other")
            )
            monkeypatch.setattr("kiro_crew.members.read_dm_binding", lambda slug: reported_binding)
        if running:
            slot._memory_assignment_from_history = False
        assignment_flag = slot._memory_assignment_from_history
        pin = AsyncMock(side_effect=AssertionError("running thread was re-pinned"))
        if running:
            monkeypatch.setattr("kiro_crew.dashboard.handlers.members.pin_private_agent_store", pin)
        try:
            reopened = await client.post(f"/api/members/{CREW}/thread")
            body = await reopened.json()
            assert (state._slots[slot.key] is slot) is (fault != "race_replaced")
            assert slot.task is task
            assert slot.running is running
            expected_store = "member-other" if fault in {"memory", "race_memory"} else assigned
            assert slot.memory_store == expected_store
            assert await asyncio.to_thread(read_dm_binding, CREW) == binding
            assert await asyncio.to_thread(read_private_session_store, session_key) == assigned
            if running:
                pin.assert_not_awaited()
                assert slot._memory_assignment_from_history == assignment_flag
            if fault:
                assert reopened.status == 409, body
                assert body["code"] == "member_slot_conflict"
                if fault.startswith("race_"):
                    assert raced.is_set()
            else:
                assert reopened.status == 200, body
                assert body == opened
        finally:
            release.set()
            if task is not None:
                # flake-ok: awaits the stand-in turn release.set() just released; moved verbatim
                await task
            slot.task = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "member_deleted",
        "member_recreated",
        "config_lock_order",
        "store_rebound",
        "store_missing",
        "store_version",
        "store_owner",
        "store_shared",
        "binding_missing",
        "binding_member",
        "binding_malformed",
        "manifest_owner",
        "manifest_missing",
        "config_malformed",
    ],
)
async def test_running_private_thread_refuses_ownership_changed_during_read(
    tmp_path, monkeypatch, fault
):
    """A completed owner change must invalidate an in-flight, read-only reopen."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import agents
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.memory_stores import memory_stores_root, provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents[CREW] = KiroCrewAgentConfig(kiro_agent=CREW)
    store = await asyncio.to_thread(provision_member_memory, cfg, CREW)
    await asyncio.to_thread(cfg.save)
    state = _make_state(tmp_path)
    app = _make_members_app(state)
    app.router.add_delete("/api/agents/{name}", agents.api_kirocrew_agent_delete)
    monkeypatch.setattr(agents, "_refresh_session_defaults", AsyncMock())
    async with TestClient(TestServer(app)) as client:
        first = await client.post(f"/api/members/{CREW}/thread")
        assert first.status == 200, await first.text()
        slot = state._slots[(await first.json())["slot_key"]]
        session_key = f"dashboard:{slot.key}"
        entered = asyncio.Event()
        resume = threading.Event()
        release_turn = asyncio.Event()
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(release_turn.wait())
        slot.task = task
        slot._memory_assignment_from_history = False
        reopen = None
        config_lock = agents._get_config_lock()
        config_requested = asyncio.Event()

        def observed_config_lock():
            config_requested.set()
            assert not slot._lock.locked(), "reopen inverted config/slot lock order"
            return config_lock

        if fault == "config_lock_order":
            monkeypatch.setattr(agents, "_get_config_lock", observed_config_lock)

        def paused_read(key):
            assigned = read_private_session_store(key)
            if key == session_key:
                loop.call_soon_threadsafe(entered.set)
                assert resume.wait(timeout=5), "reopen read was not released"
            return assigned

        def change_ownership():
            current = KiroCrewConfig.load()
            if fault == "config_lock_order":
                del current.agents[CREW]
            elif fault == "store_rebound":
                current.agents[CREW].memory_store = "default"
            elif fault == "store_missing":
                del current.memory_stores[store]
            elif fault == "store_version":
                current.memory_stores[store].memory_version = 1
            elif fault == "store_owner":
                current.memory_stores[store].owner_member = OTHER
            elif fault == "store_shared":
                current.agents[OTHER] = KiroCrewAgentConfig(memory_store=store)
            elif fault == "binding_missing":
                dm_binding_path(CREW).unlink()
            elif fault == "binding_member":
                path = dm_binding_path(CREW)
                row = json.loads(path.read_text(encoding="utf-8"))
                row["member"] = OTHER
                row["member_id"] = OTHER
                path.write_text(json.dumps(row), encoding="utf-8")
            elif fault == "binding_malformed":
                dm_binding_path(CREW).write_text("{invalid", encoding="utf-8")
            elif fault == "manifest_owner":
                # The store's owner identity lives in the SQLite
                # ``member_database`` row, not a JSON manifest: flip the stored
                # member_id so the reopen's identity read mismatches its
                # declaration.
                from kiro_crew.memory_stores import MEMORY_DB_FILE
                from kiro_crew.vector_memory import sqlite3

                database = memory_stores_root() / store / MEMORY_DB_FILE
                connection = sqlite3.connect(database)
                try:
                    connection.execute(
                        "UPDATE member_database SET member_id=? WHERE singleton=1", (OTHER,)
                    )
                    connection.commit()
                finally:
                    connection.close()
            elif fault == "manifest_missing":
                from kiro_crew.memory_stores import MEMORY_DB_FILE

                (memory_stores_root() / store / MEMORY_DB_FILE).unlink()
            elif fault == "config_malformed":
                from kiro_crew.config.loader import config_path

                config_path().write_text("{invalid", encoding="utf-8")
                return
            current.save()

        monkeypatch.setattr("kiro_crew.member_memory_auth.read_private_session_store", paused_read)
        pin = AsyncMock(side_effect=AssertionError("running thread was re-pinned"))
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.pin_private_agent_store", pin)
        try:
            reopen = asyncio.create_task(client.post(f"/api/members/{CREW}/thread"))
            await await_until(entered, what="the handler entered the slot lock wait")
            if fault in {"member_deleted", "member_recreated"}:
                deleted = await _finished(
                    client.delete(f"/api/agents/{CREW}"), "the DELETE /agents response"
                )
                assert deleted.status == 200, await deleted.text()
                current = await asyncio.to_thread(KiroCrewConfig.load)
                assert CREW not in current.agents
                if fault == "member_recreated":
                    current.agents[CREW] = KiroCrewAgentConfig(kiro_agent=CREW)
                    replacement = await asyncio.to_thread(provision_member_memory, current, CREW)
                    assert replacement != store
                    await asyncio.to_thread(current.save)
            elif fault == "config_lock_order":
                async with config_lock:
                    resume.set()
                    await await_until(config_requested, what="the opener requesting config")
                    # A config writer must still be able to take the slot lock.
                    await _finished(slot._lock.acquire(), "the slot lock acquire")
                    try:
                        await asyncio.to_thread(change_ownership)
                    finally:
                        slot._lock.release()
            else:
                await asyncio.to_thread(change_ownership)
            binding_after_change = await asyncio.to_thread(read_dm_binding, CREW)
            resume.set()
            response = await await_until(reopen, what="the reopen response")
            body = await response.json()
            assert state._slots[slot.key] is slot
            assert slot.task is task and slot.running
            assert slot.agent == CREW and slot.mode == DM_SLOT_MODE
            assert slot.memory_store == store
            assert not slot._memory_assignment_from_history
            pin.assert_not_awaited()
            assert await asyncio.to_thread(read_private_session_store, session_key) == store
            assert await asyncio.to_thread(read_dm_binding, CREW) == binding_after_change
            assert response.status == 409, body
            assert body["code"] == "member_slot_conflict"
        finally:
            resume.set()
            release_turn.set()
            if reopen is not None:
                await await_until(
                    asyncio.gather(reopen, return_exceptions=True), what="the reopen task"
                )
            await await_until(task, what="the running turn task")
            slot.task = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_running, final_running, fault",
    [
        (False, True, ""),
        (False, True, "member_deleted"),
        (False, True, "member_recreated"),
        (False, True, "store_rebound"),
        (False, False, ""),
        (True, False, ""),
    ],
)
async def test_private_thread_reopen_rechecks_running_after_slot_lock(
    tmp_path, monkeypatch, initial_running, final_running, fault
):
    """A queued idle opener cannot reuse an unchecked running identity."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import agents, members
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents[CREW] = KiroCrewAgentConfig(kiro_agent=CREW)
    store = await asyncio.to_thread(provision_member_memory, cfg, CREW)
    await asyncio.to_thread(cfg.save)
    state = _make_state(tmp_path)
    app = _make_members_app(state)
    app.router.add_delete("/api/agents/{name}", agents.api_kirocrew_agent_delete)
    monkeypatch.setattr(agents, "_refresh_session_defaults", AsyncMock())
    async with TestClient(TestServer(app)) as client:
        first = await client.post(f"/api/members/{CREW}/thread")
        assert first.status == 200, await first.text()
        opened = await first.json()
        slot = state._slots[opened["slot_key"]]
        session_key = f"dashboard:{slot.key}"
        waiting = asyncio.Event()
        release_turn = asyncio.Event()

        class ObservedLock(asyncio.Lock):
            async def acquire(self):
                if self.locked():
                    waiting.set()
                return await super().acquire()

        monkeypatch.setattr(slot, "_lock", ObservedLock())
        task = asyncio.create_task(release_turn.wait()) if initial_running else None
        slot.task = task
        slot._memory_assignment_from_history = False
        pin = AsyncMock(wraps=members.pin_private_agent_store)
        monkeypatch.setattr(members, "pin_private_agent_store", pin)
        reopen = None
        try:
            async with slot._lock:
                assert slot.running is initial_running
                reopen = asyncio.create_task(client.post(f"/api/members/{CREW}/thread"))
                await await_until(waiting, what="the opener waiting on the slot lock")
                assert not reopen.done(), "opener did not wait for the held slot lock"
                if final_running:
                    task = asyncio.create_task(release_turn.wait())
                    slot.task = task
                elif task is not None:
                    release_turn.set()
                    await await_until(task, what="the turn task settling")
                assert slot.running is final_running
                if fault in {"member_deleted", "member_recreated"}:
                    deleted = await _finished(
                        client.delete(f"/api/agents/{CREW}"), "the DELETE /agents response"
                    )
                    assert deleted.status == 200, await deleted.text()
                    current = await asyncio.to_thread(KiroCrewConfig.load)
                    assert CREW not in current.agents
                    if fault == "member_recreated":
                        current.agents[CREW] = KiroCrewAgentConfig(kiro_agent=CREW)
                        replacement = await asyncio.to_thread(
                            provision_member_memory, current, CREW
                        )
                        assert replacement != store
                        await asyncio.to_thread(current.save)
                elif fault == "store_rebound":
                    current = await asyncio.to_thread(KiroCrewConfig.load)
                    current.agents[CREW].memory_store = "default"
                    await asyncio.to_thread(current.save)
                binding_after_change = await asyncio.to_thread(read_dm_binding, CREW)
            response = await await_until(reopen, what="the reopen response")
            body = await response.json()
            assert state._slots[slot.key] is slot
            assert slot.task is task and slot.running is final_running
            assert slot.agent == CREW and slot.mode == DM_SLOT_MODE
            assert slot.memory_store == store
            assert await asyncio.to_thread(read_private_session_store, session_key) == store
            assert await asyncio.to_thread(read_dm_binding, CREW) == binding_after_change
            if initial_running or final_running:
                pin.assert_not_awaited()
                assert not slot._memory_assignment_from_history
            else:
                pin.assert_awaited_once()
            if final_running and fault:
                assert response.status == 409, body
                assert body["code"] == "member_slot_conflict"
                assert not task.done() and not task.cancelled()
            elif final_running:
                # The turn started during the lock wait and ownership is
                # unchanged, so the re-dispatch into the running path validates
                # it and returns the live thread. A conflict here would be one
                # the caller could only clear by retrying the same request.
                assert response.status == 200, body
                assert body == opened
                assert not task.done() and not task.cancelled()
            else:
                assert response.status == 200, body
                assert body == opened
        finally:
            release_turn.set()
            if reopen is not None:
                await await_until(
                    asyncio.gather(reopen, return_exceptions=True), what="the reopen task"
                )
            if task is not None:
                await await_until(task, what="the turn task")
            slot.task = None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["", "store_rebound"])
async def test_private_thread_reopen_rechecks_running_after_assignment(
    tmp_path, monkeypatch, fault
):
    """A turn starting DURING the pin must not be answered by the assignment path.

    ``slot._lock`` does not exclude turn dispatch -- ``api_chat`` reads
    ``slot.running`` and publishes ``slot.task`` without taking it -- so the
    window this covers is the ``await`` on the namespaced assignment, not the
    wait for the lock that
    :func:`test_private_thread_reopen_rechecks_running_after_slot_lock` covers.
    The route must notice the turn afterwards and answer through the read-only
    reopen path, which revalidates CURRENT ownership: with the member's store
    repointed in that same window, the assignment path's identity-only snapshot
    would publish and return 200 instead.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import agents, members
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents[CREW] = KiroCrewAgentConfig(kiro_agent=CREW)
    store = await asyncio.to_thread(provision_member_memory, cfg, CREW)
    await asyncio.to_thread(cfg.save)
    state = _make_state(tmp_path)
    app = _make_members_app(state)
    monkeypatch.setattr(agents, "_refresh_session_defaults", AsyncMock())
    async with TestClient(TestServer(app)) as client:
        first = await client.post(f"/api/members/{CREW}/thread")
        assert first.status == 200, await first.text()
        opened = await first.json()
        slot = state._slots[opened["slot_key"]]
        session_key = f"dashboard:{slot.key}"
        pinned = asyncio.Event()
        release_pin = asyncio.Event()
        real_pin = members.pin_private_agent_store

        async def gated_pin(*args, **kwargs):
            # Publish first, THEN hold: the window under test opens after the
            # assignment has been made and before the route acts on it.
            result = await real_pin(*args, **kwargs)
            pinned.set()
            # flake-ok: parks until the test sets release_pin, in its body and in finally; moved verbatim
            await release_pin.wait()
            return result

        monkeypatch.setattr(members, "pin_private_agent_store", gated_pin)
        slot.task = None
        slot.memory_store = store
        turn = None
        reopen = None
        try:
            assert not slot.running
            reopen = asyncio.create_task(client.post(f"/api/members/{CREW}/thread"))
            await await_until(pinned, what="the opener reaching the gated assignment")
            assert not reopen.done(), "opener did not reach the gated assignment"
            # The turn dispatch this route cannot lock out.
            turn = asyncio.create_task(release_pin.wait())
            slot.task = turn
            assert slot.running
            if fault == "store_rebound":
                current = await asyncio.to_thread(KiroCrewConfig.load)
                current.agents[CREW].memory_store = "default"
                await asyncio.to_thread(current.save)
            binding_after_change = await asyncio.to_thread(read_dm_binding, CREW)
            release_pin.set()
            response = await await_until(reopen, what="the reopen response")
            body = await response.json()
            assert state._slots[slot.key] is slot
            assert slot.task is turn
            assert slot.agent == CREW and slot.mode == DM_SLOT_MODE
            assert slot.memory_store == store
            assert await asyncio.to_thread(read_private_session_store, session_key) == store
            assert await asyncio.to_thread(read_dm_binding, CREW) == binding_after_change
            if fault:
                assert response.status == 409, body
                assert body["code"] == "member_slot_conflict"
            else:
                assert response.status == 200, body
                assert body == opened
        finally:
            release_pin.set()
            if reopen is not None:
                await await_until(
                    asyncio.gather(reopen, return_exceptions=True), what="the reopen task"
                )
            if turn is not None:
                await await_until(turn, what="the turn task")
            slot.task = None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["pin_raises", "pin_mismatch"])
async def test_fresh_private_thread_pin_failure_leaves_no_ghost_slot(tmp_path, monkeypatch, fault):
    """A pin failure on a fresh private-store open leaves no ghost slot.

    The v2 collision guard and ``pin_private_agent_store`` run BEFORE
    ``get_or_create_slot`` (pin-before-mint).  A refused pin returns without
    ever creating a slot, so no member-prefixed key enters ``state._slots``
    and no such key reaches any broadcast.

    ``pin_raises``: ``pin_private_agent_store`` raises → route answers 503.
    ``pin_mismatch``: two members share the same slug and the canonical key
    has no protected assignment → route answers 409 (member_pin_mismatch).
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.sections import MemoryStoreConfig
    from kiro_crew.dashboard.handlers import members as members_mod
    from kiro_crew.memory_stores import UnknownMemoryStore, provision_member_memory

    # Provision a v2 private store for CREW so the pin path is reached.
    def configure():
        cfg = KiroCrewConfig.load()
        cfg.agents[CREW] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        provision_member_memory(cfg, CREW)

        if fault == "pin_mismatch":
            # Add a second member whose name produces the SAME slug as CREW,
            # so len(slug_owners) == 2 and the collision guard fires.
            # Build a minimal v2 record; the store directory need not exist
            # because the route refuses before touching it.
            twin = CREW + "-twin"
            cfg.agents[twin] = KiroCrewAgentConfig(kiro_agent="kirocrew")
            twin_store = f"member-{twin}-store"
            cfg.memory_stores[twin_store] = MemoryStoreConfig(
                owner_member=twin, owner_member_id="twin-id", memory_version=2
            )
            cfg.agents[twin].memory_store = twin_store
            # Patch slug resolution to report twin as a slug_owner for CREW's
            # slug: the real slug_for_name would give a different key, so we
            # override _member_names_for_slug instead.
            real_names = members_mod._member_names_for_slug

            def _patched_names(cfg, slug):
                owners = real_names(cfg, slug)
                if CREW in owners and twin not in owners:
                    owners = owners + [twin]
                return owners

            monkeypatch.setattr(members_mod, "_member_names_for_slug", _patched_names)

        cfg.save()

    await asyncio.to_thread(configure)
    state = _make_state(tmp_path)

    broadcast_snapshots: list[list[str]] = []

    _real_push = state.push_slots_update

    def _recording_push():
        broadcast_snapshots.append(sorted(state._slots.keys()))
        _real_push()

    monkeypatch.setattr(state, "push_slots_update", _recording_push)

    if fault == "pin_raises":
        monkeypatch.setattr(
            members_mod,
            "pin_private_agent_store",
            AsyncMock(side_effect=UnknownMemoryStore("disk full")),
        )

    async with TestClient(TestServer(_make_members_app(state))) as client:
        resp = await client.post(f"/api/members/{CREW}/thread")
        body = await resp.json()

    if fault == "pin_raises":
        assert resp.status == 503, body
        assert body.get("code") == "store_unavailable"
    else:
        # Two owners → the collision guard fires → 409 (member_pin_mismatch).
        assert resp.status == 409, body
        assert body.get("code") == "member_pin_mismatch"

    # No ghost slot left in the registry (V1 or V2 key): the pre-flight
    # pin runs before get_or_create_slot, so no slot is created on a refusal.
    lingering = [k for k in state._slots if k.startswith("member-")]
    assert not lingering, f"ghost slot(s) in _slots after refused open: {lingering}"
    ghost_broadcasts = [
        snap for snap in broadcast_snapshots if any(k.startswith("member-") for k in snap)
    ]
    assert not ghost_broadcasts, f"ghost slot broadcast after refused open: {ghost_broadcasts}"
