"""Slot switch serialization across alias slots, the project set and the bulk model switch.

Alias slots on one live session serialize on a session-keyed lock while a switch
on another session runs unblocked, the project set waits behind the slot lock,
and the bulk model switch takes each slot's lock in turn and reports every slot
as switched, unchanged, skipped or failed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_slot_switch_atomicity_helpers import _MODEL_A, _MODEL_B, _make_app, _mock_state
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.chat_handlers import _slot_switch_session_lock
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.testing.wait import finished

_LINKED_KEY = "slack:8442.001"


def _alias_state(slot_a: _ChatSlot, slot_b: _ChatSlot) -> DashboardState:
    """A state holding two alias slots that resolve onto ONE session."""
    state = _mock_state(slot_a)
    state._slots = {slot_a.key: slot_a, slot_b.key: slot_b}
    return state


class TestAliasSlotSwitchSerialization:
    """Two alias slots on ONE session must serialize against each other.

    ``effective_session_key`` folds every alias onto the session a slot's
    turns actually run on, so two slot names can address one live session
    (a channel- or cron-born slot carries the real key in
    ``linked_session_key``). The switch handlers serialize on ``slot._lock``
    and ``slot._model_pick_lock``, both created per ``_ChatSlot`` — so two
    switches arriving through DIFFERENT aliases take different locks and
    neither waits for the other: both commit, both reset the shared session,
    and the two slots' committed settings can disagree with each other and
    with the live provider. ``_autocompact_txn_locks`` already solved this
    class for its own endpoint by keying the lock on the shared resource
    rather than on the slot; these tests pin the same shape for the switch
    handlers.
    """

    @pytest.mark.asyncio
    async def test_racing_alias_model_switches_serialize(self):
        slot_a = _ChatSlot("alias-a")
        slot_b = _ChatSlot("alias-b")
        for _s in (slot_a, slot_b):
            _s.model = ""
            _s.linked_session_key = _LINKED_KEY
        state = _alias_state(slot_a, slot_b)

        seen_at_reset: list[tuple[str, str]] = []
        first_reset_started = asyncio.Event()
        release_first_reset = asyncio.Event()

        async def _reset(*args, **kwargs):
            seen_at_reset.append((slot_a.model, slot_b.model))
            if len(seen_at_reset) == 1:
                first_reset_started.set()
                # flake-ok: pre-existing unbounded wait inside the stub until the test releases it, moved verbatim; tracked for a de-flake pass
                await release_first_reset.wait()
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = asyncio.create_task(
                client.post("/api/chat/slots/alias-a/model", json={"model": _MODEL_A})
            )
            # flake-ok: pre-existing unbounded wait for the stubbed reset to start, moved verbatim; tracked for a de-flake pass
            await first_reset_started.wait()
            second = asyncio.create_task(
                client.post("/api/chat/slots/alias-b/model", json={"model": _MODEL_B})
            )
            # Give the second request time to run as far as it can. Per-slot
            # locks are DISJOINT across aliases, so without a session-keyed
            # lock it runs to completion right here: it commits its model and
            # resets the very session the first request is still resetting.
            # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.05)
            assert slot_b.model == ""
            assert state.sessions.reset.await_count == 1
            release_first_reset.set()
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp1 = await first
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp2 = await second
            assert resp1.status == 200
            assert resp2.status == 200
            # Each reset observed exactly the state its own request committed.
            assert seen_at_reset == [(_MODEL_A, ""), (_MODEL_A, _MODEL_B)]
            assert slot_a.model == _MODEL_A
            assert slot_b.model == _MODEL_B
            assert {c.args[0] for c in state.sessions.reset.await_args_list} == {_LINKED_KEY}

    # One pin per handler the sweep changed: with the session lock held by
    # another alias's in-flight switch, each handler must neither mutate nor
    # reset. Holding the lock externally is the same shape the per-slot
    # tests above use for slot._lock.

    @pytest.mark.asyncio
    async def test_agent_switch_waits_for_the_session_lock(self, monkeypatch):
        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)
        slot = _ChatSlot("alias-a")
        slot.agent = "old-agent"
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                task = asyncio.create_task(
                    client.post("/api/chat/slots/alias-a/agent", json={"agent": "new-agent"})
                )
                # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                assert slot.agent == "old-agent"
                state.sessions.reset.assert_not_awaited()
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp = await task
            assert resp.status == 200
            assert slot.agent == "new-agent"

    @pytest.mark.asyncio
    async def test_model_switch_waits_for_the_session_lock(self):
        slot = _ChatSlot("alias-a")
        slot.model = _MODEL_A
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                task = asyncio.create_task(
                    client.post("/api/chat/slots/alias-a/model", json={"model": _MODEL_B})
                )
                # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                assert slot.model == _MODEL_A
                state.sessions.reset.assert_not_awaited()
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp = await task
            assert resp.status == 200
            assert slot.model == _MODEL_B

    @pytest.mark.asyncio
    async def test_effort_switch_waits_for_the_session_lock(self):
        slot = _ChatSlot("alias-a")
        slot.reasoning_effort = ""
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                task = asyncio.create_task(
                    client.post(
                        "/api/chat/slots/alias-a/reasoning-effort",
                        json={"reasoning_effort": "high"},
                    )
                )
                # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                assert slot.reasoning_effort == ""
                state.sessions.reset.assert_not_awaited()
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp = await task
            assert resp.status == 200
            assert slot.reasoning_effort == "high"

    @pytest.mark.asyncio
    async def test_workspace_switch_waits_for_the_session_lock(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda ws: f"/workspace/{ws}",
        )
        slot = _ChatSlot("alias-a")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                task = asyncio.create_task(
                    client.post("/api/chat/slots/alias-a/workspace", json={"workspace": "new-ws"})
                )
                # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                assert slot.workspace == "old-ws"
                state.sessions.reset.assert_not_awaited()
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp = await task
            assert resp.status == 200
            assert slot.workspace == "new-ws"

    @pytest.mark.asyncio
    async def test_bulk_model_switch_waits_for_the_session_lock(self):
        slot = _ChatSlot("alias-a")
        slot.model = _MODEL_A
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                task = asyncio.create_task(
                    client.post("/api/chat/slots/model", json={"model": _MODEL_B})
                )
                # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                assert slot.model == _MODEL_A
                state.sessions.reset.assert_not_awaited()
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp = await task
            data = await resp.json()
            assert resp.status == 200
            assert data["switched"] == ["alias-a"]
            assert slot.model == _MODEL_B

    @pytest.mark.asyncio
    async def test_a_rebind_while_queued_locks_the_new_session(self):
        # The window GPT 5.6 found on the first revision, closed structurally.
        # A rebind can land while a request queues on slot._lock, which is why
        # every handler resolves the key INSIDE that lock (pinned by
        # test_binding_that_lands_while_queued_on_the_lock_is_the_one_switched).
        # Had the session lock been keyed on any EARLIER read, the handler would
        # hold the lock for the OLD session while probing and resetting the new
        # one -- so an alias switching the new session would not be serialized
        # against it, and the post-await re-checks could not see it because they
        # compare against session_key, which is already the new key.
        # Entering the session lock AFTER the in-lock read makes the lock key
        # and the acted-on key the same value. This pins that: hold the NEW
        # session's lock, and the handler must wait for it.
        s2 = "slack:8442.999"
        s2_lock = _slot_switch_session_lock(s2)
        slot = _ChatSlot("alias-a")
        slot.model = _MODEL_A
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            # flake-ok: pre-existing acquire of a permit no other task holds, so it returns at once; moved verbatim
            await s2_lock.acquire()
            try:
                async with slot._lock:
                    task = asyncio.create_task(
                        client.post("/api/chat/slots/alias-a/model", json={"model": _MODEL_B})
                    )
                    # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
                    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                    await asyncio.sleep(0.05)
                    # Rebind while it is queued on slot._lock.
                    slot.linked_session_key = s2
                # slot._lock is free now, so the handler resolves s2 and must
                # queue on L(s2) -- which this test holds. Keyed on the stale
                # pre-lock read it would instead sail through holding L(S1).
                # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                assert not task.done()
                assert slot.model == _MODEL_A
                state.sessions.reset.assert_not_awaited()
            finally:
                s2_lock.release()
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp = await task
            assert resp.status == 200
            # The binding that landed is still the one switched.
            assert slot.model == _MODEL_B
            assert state.sessions.reset.await_args.args[0] == s2

    @pytest.mark.asyncio
    async def test_a_different_sessions_switch_is_not_blocked(self):
        # The complement, and the reason this is a session-keyed lock rather
        # than one global switch lock: a global lock would also stop the
        # collision above, by serializing every unrelated slot with it. Keyed
        # by session, a slot on a DIFFERENT session resolves to a different
        # lock and runs straight through while this one is held.
        other = _ChatSlot("other")
        other.model = _MODEL_A
        other.linked_session_key = "slack:9999.000"
        state = _mock_state(other, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                # Completes WITHOUT the held lock being released. Bounded so a
                # regression to ONE global switch lock reddens here promptly
                # instead of hanging: the correct path takes milliseconds.
                resp = await finished(
                    client.post("/api/chat/slots/other/model", json={"model": _MODEL_B}),
                    "the unrelated slot's model switch",
                    timeout=5,
                )
                assert resp.status == 200
                assert other.model == _MODEL_B
                state.sessions.reset.assert_awaited_once()


class TestSlotProjectSwitchAtomicity:
    @pytest.mark.asyncio
    async def test_project_set_waits_for_slot_lock(self, tmp_path):
        # api_chat_slot_project is the one remaining live mutator of
        # slot.project outside the switch handlers: unlocked, its write could
        # land during a locked workspace switch's reset await and then be
        # erased by that switch's rollback. Serialized on the same lock, the
        # write queues until the switch completes.
        import os

        from kiro_crew.dashboard.chat import api_chat_slot_project

        slot = _ChatSlot("test")
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot)
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        # A real directory on every OS: the endpoint realpaths and isdir-checks
        # the payload before the locked section this test pins.
        new_dir = os.path.realpath(str(tmp_path))
        async with TestClient(TestServer(app)) as client:
            async with slot._lock:
                task = asyncio.create_task(
                    client.post("/api/chat/slots/test/project", json={"project": new_dir})
                )
                # Let the request pass validation and block on the slot lock.
                # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                assert slot.project == "/workspace/old-ws"
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp = await task
            assert resp.status == 200
            assert slot.project == new_dir


class TestBulkModelSwitchAtomicity:
    @pytest.mark.asyncio
    async def test_bulk_switch_waits_for_slot_lock(self):
        # The bulk handler acquires each slot's lock per-iteration, same lock
        # as the single-slot switch handlers: while another actor holds slot
        # A's lock, the bulk switch must neither commit nor reset A.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with slot._lock:
                task = asyncio.create_task(
                    client.post("/api/chat/slots/model", json={"model": _MODEL_B})
                )
                # Let the request reach (and block on) the slot lock.
                # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                assert slot.model == _MODEL_A
                state.sessions.reset.assert_not_awaited()
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp = await task
            data = await resp.json()
            assert resp.status == 200
            assert data["switched"] == ["test"]
            assert slot.model == _MODEL_B
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_slot_that_started_running_while_queued_is_skipped(self):
        # The skip_running pre-check is re-run INSIDE the lock: a slot that
        # became running while the bulk request waited on its lock must land
        # in skipped_running — not be reset mid-turn (the defect this PR
        # exists to prevent, on the bulk path).
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with slot._lock:
                task = asyncio.create_task(
                    client.post("/api/chat/slots/model", json={"model": _MODEL_B})
                )
                # Let the request pass the unlocked pre-check and block on the
                # lock, then start a turn before releasing it.
                # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                running_task = MagicMock()
                running_task.done.return_value = False
                slot.task = running_task
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp = await task
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert data["switched"] == []
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_running_slot_already_on_target_is_unchanged_not_skipped(self):
        # Classification order inside the lock is equality FIRST: a running
        # slot that already uses the requested model is "unchanged", not
        # "skipped_running" — a running-check ahead of the equality check
        # would misreport it and imply work was left undone.
        slot = _ChatSlot("test")
        slot.model = _MODEL_B
        running_task = MagicMock()
        running_task.done.return_value = False
        slot.task = running_task
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["unchanged"] == ["test"]
            assert data["skipped_running"] == []
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retry_exception_is_isolated_per_slot(self):
        # The retry runs inside the per-slot failure-isolation try: a teardown
        # that raises on the retry classifies THAT slot as failed (model
        # untouched) and the remaining slots are still processed — never a
        # 500 aborting the whole bulk switch.
        from kiro_crew.providers.acp import AcpProvider

        slot_a = _ChatSlot("a")
        slot_a.model = _MODEL_A
        slot_b = _ChatSlot("b")
        slot_b.model = _MODEL_A
        idle = MagicMock(spec=AcpProvider)
        idle.has_active_turn.return_value = False
        state = _mock_state(slot_a, provider=idle)
        state._slots = {"a": slot_a, "b": slot_b}
        # Slot a: first reset declines, retry raises. Slot b: reset succeeds.
        state.sessions.reset = AsyncMock(side_effect=[False, RuntimeError("boom"), True])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["failed"] == ["a"]
            assert data["switched"] == ["b"]
            assert slot_a.model == _MODEL_A
            assert slot_b.model == _MODEL_B

    @pytest.mark.asyncio
    async def test_reset_declined_no_live_provider_switches(self):
        # A declined reset with NO live registered provider commits: nothing
        # to tear down, the next message cold-starts under the new model.
        # Exactly one reset attempt, slot lands in switched.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["switched"] == ["test"]
            assert slot.model == _MODEL_B
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_declined_cold_start_lands_in_skipped_running(self):
        # A first send can slip into the reset await and still be INSIDE its
        # provider.start() when the decline is read: slot.running is set (at
        # dispatch) but get_provider sees nothing yet. Bulk commits AFTER the
        # reset, so that cold-starting session captured the OLD model —
        # committing here would report success over it. The handler re-reads
        # slot.running before the provider ladder and classifies the slot as
        # skipped_running with its model untouched.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)

        async def _decline_and_start_turn(*_a, **_k):
            running_task = MagicMock()
            running_task.done.return_value = False
            slot.task = running_task
            return False

        state.sessions.reset = AsyncMock(side_effect=_decline_and_start_turn)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert data["switched"] == []
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_declined_idle_retry_switches(self):
        # An idle live session declined the first reset (its slipped-in turn
        # already finished). Bulk commits AFTER the reset, so that session is
        # on the old model: the handler retries once, and on success the slot
        # is switched — never left as a silent stale process.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        provider = MagicMock(spec=AcpProvider)
        provider.has_active_turn.return_value = False
        state = _mock_state(slot, provider=provider)
        state.sessions.reset = AsyncMock(side_effect=[False, True])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["switched"] == ["test"]
            assert slot.model == _MODEL_B
            assert state.sessions.reset.await_count == 2

    @pytest.mark.asyncio
    async def test_reset_declined_twice_lands_in_skipped_running(self):
        # A second decline means another turn is genuinely racing the retry:
        # the slot lands in skipped_running with its model untouched.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        provider = MagicMock(spec=AcpProvider)
        provider.has_active_turn.return_value = False
        state = _mock_state(slot, provider=provider)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert data["switched"] == []
            assert slot.model == _MODEL_A
            assert state.sessions.reset.await_count == 2

    @pytest.mark.asyncio
    async def test_reset_declined_busy_lands_in_skipped_running(self):
        # A turn can start even after the in-lock checks (message dispatch
        # does not take slot._lock), so the reset runs with
        # skip_if_busy=skip_running and its atomic decline is authoritative:
        # the slot lands in skipped_running with its model untouched, and the
        # in-flight turn survives.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        provider = MagicMock(spec=AcpProvider)
        # Idle at the in-lock pre-check, busy by the time the decline is read.
        provider.has_active_turn.side_effect = [False, True]
        state = _mock_state(slot, provider=provider)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert data["switched"] == []
            assert slot.model == _MODEL_A
            assert state.sessions.reset.await_args.kwargs == {"skip_if_busy": True}

    @pytest.mark.asyncio
    async def test_live_turn_on_effective_session_is_skipped_before_the_reset(self):
        # slot.running only sees turns dispatched through this slot's task; a
        # channel-linked slot's turn runs under its linked key without setting
        # it. The last-instant has_active_turn re-check on the effective
        # session catches it BEFORE the reset, so _reset_slot_session's
        # unblock half never runs against the live turn's pending cards.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot.linked_session_key = "slack:123.456"
        provider = MagicMock(spec=AcpProvider)
        provider.has_active_turn.return_value = True
        state = _mock_state(slot, provider=None)
        state.sessions.get_provider = MagicMock(
            side_effect=lambda key: provider if key == "slack:123.456" else None
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slot_with_attached_subagents_is_skipped_even_when_forced(self):
        # The reset tears down the runtime attached children run on, so a
        # parent with children is skipped — even with skip_running=false,
        # which speaks to the parent's own turn, not to its children.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)
        state.subagents = MagicMock()
        state.subagents.running_agents_for.return_value = ["child-1"]
        state.subagents._queued_depth.return_value = 0
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/model", json={"model": _MODEL_B, "skip_running": False}
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert data["switched"] == []
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()
