"""The test-only interleaving seam, and the reload-vs-switch race it reaches.

``chat_handlers._test_interleave`` is awaited at four named points across the
session-teardown paths. These tests pin its contract -- unset by default, called
with the point names in the order the code reaches them -- and then use it for the
thing it exists for: driving a reload's teardown into the middle of a switch
handler's commit-then-reset span, deterministically, with no sleep.

That interleaving is what reload joining the same two locks the switch
handlers hold now closes: the tests named for it below assert the serialized
outcome -- the second racer blocks on the session lock while the first holds
it. See their docstrings.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat import api_chat_slot_model, api_chat_slot_reload
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# Registry aliases the model guard accepts, matching the ids the sibling
# switch-atomicity tests use.
_MODEL_OLD = "claude-opus-4.8"
_MODEL_NEW = "gpt-5.6-sol"
_SLOT = "s1"
_SESSION_KEY = f"dashboard:{_SLOT}"

# Loop turns a bounded yield will spend before giving up. Turns, not seconds:
# the cap exists only to bound a coroutine that can never make progress, and is
# far above what any interleaving here needs.
_MAX_TURNS = 500


async def _yield_until(predicate: Callable[[], bool]) -> bool:
    """Hand the loop back until *predicate* holds, then report whether it did.

    Scheduler turns, not wall clock. ``asyncio.sleep(0)`` reschedules this
    coroutine behind whatever is already runnable, so how many turns a given
    interleaving needs is a property of the code under test, identical on a busy
    host and an idle one -- unlike a sleep, whose duration decides the outcome.

    Returning a bool rather than asserting is what keeps a caller readable in
    both worlds: a racer that becomes blocked by a future fix reports False here
    and the caller fails on its own assertion, instead of hanging until the
    suite-wide timeout kills it with no explanation.
    """
    for _ in range(_MAX_TURNS):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


def _make_app(state: DashboardState) -> web.Application:
    # Mirror production: the token_auth middleware sets request["app"] on every
    # authenticated path ("" = dashboard user), and the app-isolation guards on
    # both routes fail closed without it.
    @web.middleware
    async def dashboard_auth_marker(request, handler):
        if "app" not in request:
            request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[dashboard_auth_marker])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
    app.router.add_post("/api/chat/slots/{slot}/reload", api_chat_slot_reload)
    return app


def _idle_provider() -> MagicMock:
    provider = MagicMock()
    provider.has_active_turn = MagicMock(return_value=False)
    return provider


def _mock_state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock(return_value=True)
    state.sessions.get_provider = MagicMock(return_value=_idle_provider())
    return state


@pytest.fixture
def slot() -> _ChatSlot:
    s = _ChatSlot(_SLOT)
    s.model = _MODEL_OLD
    return s


@pytest.fixture
def state(slot: _ChatSlot) -> DashboardState:
    return _mock_state(slot)


@pytest.fixture(autouse=True)
def _no_eager_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reload re-arms the resume spawn; there is no runtime here to spawn onto."""
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", MagicMock(return_value=None))


class TestInterleaveSeamContract:
    """What the seam promises when nothing sets it, and what it reports when set."""

    def test_hook_defaults_to_none(self):
        # Production cost is this attribute being None: each point reads the
        # global, compares, and creates no coroutine.
        assert chat_handlers._test_interleave is None

    @pytest.mark.asyncio
    async def test_unset_hook_leaves_the_teardown_untouched(self, state, slot):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")
        assert resp.status == 200
        state.sessions.reset.assert_awaited_once_with(_SESSION_KEY, skip_if_busy=True)

    @pytest.mark.asyncio
    async def test_reload_reaches_its_points_in_order(self, state, slot, monkeypatch):
        seen: list[str] = []

        async def _record(point: str) -> None:
            seen.append(point)

        monkeypatch.setattr(chat_handlers, "_test_interleave", _record)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")

        assert resp.status == 200
        # Reload owns the outer point and reaches the shared chokepoint's two
        # through _reset_slot_session. No switch point: reload commits nothing.
        assert seen == ["reload:pre_reset", "reset:pre_pop", "reset:post_pop"]

    @pytest.mark.asyncio
    async def test_model_switch_reaches_its_points_in_order(self, state, slot, monkeypatch):
        seen: list[str] = []

        async def _record(point: str) -> None:
            seen.append(point)

        monkeypatch.setattr(chat_handlers, "_test_interleave", _record)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/model", json={"model": _MODEL_NEW})

        assert resp.status == 200
        assert slot.model == _MODEL_NEW
        # switch:post_commit precedes the chokepoint's pair, which is the whole
        # point of where it sits: the new value is already committed and both
        # locks are held while the old session is still alive.
        assert seen == ["switch:post_commit", "reset:pre_pop", "reset:post_pop"]

    @pytest.mark.asyncio
    async def test_a_raising_hook_is_not_swallowed(self, state, slot, monkeypatch):
        """A broken hook must fail its test, not silently skip the interleaving.

        Pins the ABSENCE of a suppress around the points: were one added, the
        teardown would proceed and every seam test would still pass while
        interleaving nothing.
        """

        async def _boom(point: str) -> None:
            raise RuntimeError(f"hook failed at {point}")

        monkeypatch.setattr(chat_handlers, "_test_interleave", _boom)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")

        assert resp.status == 500
        state.sessions.reset.assert_not_awaited()


class TestReloadRacesSwitchCommitResetSpan:
    """The reload-vs-switch race, driven through the seam, serialized.

    Without a shared lock, ``api_chat_slot_reload`` tears down the slot's
    effective session while holding neither ``slot._lock`` nor the
    session-keyed switch lock, while the four commit-before-reset switch
    handlers hold both across their commit-then-reset span -- nothing orders
    the two. Reload joins the SAME two locks in the SAME order, so its
    probe-then-teardown is serialized against a switch's span on the same
    session. These tests drive the interleaving from both directions and
    prove that the second racer BLOCKS on the session lock while the first
    holds it, then completes only in the serialized order once the first
    releases.
    """

    @pytest.mark.asyncio
    async def test_switch_blocks_until_reloads_serialized_teardown_completes(
        self, state, slot, monkeypatch
    ):
        """A model switch launched while reload holds the locks must wait its turn.

        Reload is suspended at ``reload:pre_reset`` holding both ``slot._lock``
        and the session-keyed switch lock. A model switch on the same session
        is launched from there: it BLOCKS on the session
        lock, so ``switch.done()`` stays False for as long as reload is
        suspended. Once reload resumes, finishes its teardown and releases the
        locks, the switch proceeds -- its whole transaction lands strictly
        AFTER reload's teardown pair.
        """
        order: list[str] = []
        switch: asyncio.Task[object] | None = None

        async with TestClient(TestServer(_make_app(state))) as client:

            async def _interleave(point: str) -> None:
                order.append(point)
                if point != "reload:pre_reset":
                    return
                # Suspended inside reload's teardown while it holds both locks.
                # Start the switch here and hand the loop back a bounded number
                # of turns: a serialized switch cannot make progress, so this
                # proves it blocks rather than interleaves.
                nonlocal switch
                switch = asyncio.create_task(
                    client.post(f"/api/chat/slots/{_SLOT}/model", json={"model": _MODEL_NEW})
                )
                blocked = not await _yield_until(lambda: switch is not None and switch.done())
                # The switch is blocked on the session lock reload holds: it has
                # made no progress past the lock, so none of its seam points
                # have fired yet.
                assert blocked, "switch completed while reload held the session lock"
                assert "switch:post_commit" not in order

            monkeypatch.setattr(chat_handlers, "_test_interleave", _interleave)
            reload_resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")

            assert switch is not None
            try:
                # Reload has released its locks by now, so the switch that was
                # blocked runs to completion in the serialized order.
                assert await _yield_until(
                    lambda: switch.done()
                ), "switch never completed after reload released the locks"
                switch_resp = switch.result()
            finally:
                switch.cancel()
                await asyncio.gather(switch, return_exceptions=True)

            assert switch_resp.status == 200
            assert reload_resp.status == 200

        # Reload's own teardown pair completes BEFORE any switch point: the
        # switch was blocked on the session lock until reload released it, so
        # its commit and teardown land strictly after reload's pair.
        assert order == [
            "reload:pre_reset",
            "reset:pre_pop",
            "reset:post_pop",
            "switch:post_commit",
            "reset:pre_pop",
            "reset:post_pop",
        ]
        assert slot.model == _MODEL_NEW
        # Reload's single teardown, then the switch's -- serialized on the one
        # session key, serialized rather than two unserialized teardowns racing.
        assert state.sessions.reset.await_count == 2
        assert {c.args[0] for c in state.sessions.reset.await_args_list} == {_SESSION_KEY}

    @pytest.mark.asyncio
    async def test_reload_refuses_a_slot_recreated_under_the_same_name_while_it_queued(
        self, state, slot, monkeypatch
    ):
        """A stale slot reference must not authorize tearing down its replacement.

        Reload reads ``state._slots.get(name)`` before ever awaiting, then
        queues on ``slot._lock``. If the name is deleted and recreated (a
        different app's slot, or the same app reconnecting) while that request
        sits on the lock, the locks it eventually acquires belong to the OLD
        object -- but ``session_key`` and the app-isolation check downstream
        would resolve against the NEW slot if nothing re-checked identity. This
        drives that exact window: hold ``slot._lock`` itself (so reload queues
        on it, never reaching any interleave seam), swap in a same-named
        replacement, then release. Reload must see the mismatch and refuse
        with 404 -- not tear down the replacement's session.
        """
        replacement = _ChatSlot(_SLOT)
        replacement.model = _MODEL_OLD

        async with TestClient(TestServer(_make_app(state))) as client:
            async with slot._lock:
                reload_task = asyncio.create_task(client.post(f"/api/chat/slots/{_SLOT}/reload"))
                # Give the request a chance to read the (still-current) slot and
                # queue on the lock this block already holds.
                stuck = not await _yield_until(lambda: reload_task.done())
                assert stuck, "reload completed without ever contending for slot._lock"

                # The name is now a different slot object -- same shape a
                # delete-then-recreate under the same key produces.
                state._slots[_SLOT] = replacement

            try:
                resp = await _yield_until(lambda: reload_task.done())
                assert resp, "reload never completed after slot._lock was released"
                reload_resp = reload_task.result()
                reload_status = reload_resp.status
                reload_body = await reload_resp.json()
            finally:
                reload_task.cancel()
                await asyncio.gather(reload_task, return_exceptions=True)

        assert reload_status == 404
        assert reload_body["code"] == "slot_not_found"
        # The replacement's session was never touched by the stale request.
        state.sessions.reset.assert_not_awaited()
        assert state._slots[_SLOT] is replacement

    @pytest.mark.asyncio
    async def test_reload_refuses_a_slot_recreated_while_queued_on_the_session_lock(
        self, state, slot, monkeypatch
    ):
        """The same stale-authorization window, reopened by the SECOND await.

        Reload re-checks slot identity right after ``slot._lock`` -- the test
        above pins that. But ``_slot_switch_session_lock(session_key)`` is a
        SECOND suspension point (a concurrent switch on the same session holds
        it), and nothing re-checks identity between resuming from it and the
        app-isolation call that follows. This drives that exact window: hold
        the session lock externally (via the same registry function reload
        uses) so reload passes ``slot._lock`` and its first re-check, then
        queues on the session lock; swap in a same-named replacement; release.
        Reload must refuse with 404 rather than authorize against the
        replacement.
        """
        replacement = _ChatSlot(_SLOT)
        replacement.model = _MODEL_OLD
        session_lock = chat_handlers._slot_switch_session_lock(_SESSION_KEY)

        async with TestClient(TestServer(_make_app(state))) as client:
            async with session_lock:
                reload_task = asyncio.create_task(client.post(f"/api/chat/slots/{_SLOT}/reload"))
                # Give the request a chance to pass slot._lock, pass its first
                # identity re-check (the slot is still current at this point),
                # resolve session_key, and queue on the session lock this
                # block already holds.
                stuck = not await _yield_until(lambda: reload_task.done())
                assert stuck, "reload completed without ever contending for the session lock"

                # The name is now a different slot object -- reload is queued
                # past its first re-check with the OLD slot captured, so only
                # a second re-check after the session lock catches this.
                state._slots[_SLOT] = replacement

            try:
                resp = await _yield_until(lambda: reload_task.done())
                assert resp, "reload never completed after the session lock was released"
                reload_resp = reload_task.result()
                reload_status = reload_resp.status
                reload_body = await reload_resp.json()
            finally:
                reload_task.cancel()
                await asyncio.gather(reload_task, return_exceptions=True)

        assert reload_status == 404
        assert reload_body["code"] == "slot_not_found"
        # The replacement's session was never touched by the stale request.
        state.sessions.reset.assert_not_awaited()
        assert state._slots[_SLOT] is replacement

    @pytest.mark.asyncio
    async def test_reload_refuses_a_rebind_that_lands_while_queued_on_the_session_lock(
        self, state, slot, monkeypatch
    ):
        """A same-slot rebind must not let a stale session_key authorize a teardown.

        Unlike the two tests above, the slot object here is NEVER swapped --
        ``state._slots[_SLOT] is slot`` holds throughout, so both identity
        re-checks pass. What changes is ``slot.linked_session_key`` itself: a
        cron/channel rebind mutates that attribute on the SAME slot object
        while reload is queued on the session lock it already resolved
        ``session_key`` from. ``effective_session_key(slot)`` now names a
        DIFFERENT session than the one the lock guards, so reload must refuse
        with 409/session_rebound rather than tear the new session down under
        an authorization computed for the old one.
        """
        NEW_SESSION_KEY = "slack:1234.5678"
        new_lock = chat_handlers._slot_switch_session_lock(NEW_SESSION_KEY)
        session_lock = chat_handlers._slot_switch_session_lock(_SESSION_KEY)

        async with TestClient(TestServer(_make_app(state))) as client:
            async with session_lock:
                reload_task = asyncio.create_task(client.post(f"/api/chat/slots/{_SLOT}/reload"))
                # Give the request a chance to pass slot._lock, its first
                # identity re-check, resolve session_key = _SESSION_KEY, and
                # queue on the session lock this block already holds.
                stuck = not await _yield_until(lambda: reload_task.done())
                assert stuck, "reload completed without ever contending for the session lock"

                # The rebind: same slot object, new linked session. Reload's
                # captured session_key (_SESSION_KEY) is now stale.
                slot.linked_session_key = NEW_SESSION_KEY

            try:
                resp = await _yield_until(lambda: reload_task.done())
                assert resp, "reload never completed after the session lock was released"
                reload_resp = reload_task.result()
                reload_status = reload_resp.status
                reload_body = await reload_resp.json()
            finally:
                reload_task.cancel()
                await asyncio.gather(reload_task, return_exceptions=True)

        assert reload_status == 409
        assert reload_body["code"] == "session_rebound"
        # Neither the stale session nor the new one was torn down by this
        # request: the guard fires before any teardown call.
        state.sessions.reset.assert_not_awaited()
        assert state._slots[_SLOT] is slot
        assert not new_lock.locked()

    @pytest.mark.asyncio
    async def test_reload_refuses_a_rebind_that_lands_during_the_reset_await(
        self, state, slot, monkeypatch
    ):
        """The same rebind race, one await later: during the teardown itself.

        The two earlier guards re-check identity/binding before
        ``_reset_slot_session`` is awaited. ``_bind_cron_slot`` takes NO lock
        of its own, so a rebind is just as free to land WHILE that await is
        in flight -- one await later than the second guard's window, and past
        it. This drives that exact placement: hook ``reset:pre_pop`` (a seam
        point INSIDE ``_reset_slot_session``, reached only after both earlier
        guards already passed) to mutate ``slot.linked_session_key`` there,
        then let the reset and the rest of reload's flow proceed. Reload must
        still refuse with 409/session_rebound rather than report success for
        a session that was never touched.
        """
        NEW_SESSION_KEY = "slack:9999.1111"

        async def _interleave(point: str) -> None:
            if point == "reset:pre_pop":
                slot.linked_session_key = NEW_SESSION_KEY

        monkeypatch.setattr(chat_handlers, "_test_interleave", _interleave)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")
            status = resp.status
            body = await resp.json()

        assert status == 409
        assert body["code"] == "session_rebound"
        # The reset itself still ran against the ORIGINAL session_key (the
        # rebind lands mid-reset, not before it) -- this guard's job is to
        # stop the response, not to have prevented the one reset already in
        # flight when the rebind arrived.
        state.sessions.reset.assert_awaited_once_with(_SESSION_KEY, skip_if_busy=True)
        assert slot.linked_session_key == NEW_SESSION_KEY

    @pytest.mark.asyncio
    async def test_reload_blocks_until_a_suspended_switch_releases_its_locks(
        self, state, slot, monkeypatch
    ):
        """The same race from the other side: switch holds the locks, reload waits.

        The switch is suspended at ``switch:post_commit`` after committing its
        new model and before tearing the old session down, holding both of its
        locks. A reload on the same session is launched from there: it BLOCKS on the session lock, so ``reload_task.done()`` stays
        False for as long as the switch is suspended. Only after the switch
        resumes, finishes its teardown and releases the locks does reload run
        its own teardown -- strictly AFTER the switch's pair.
        """
        order: list[str] = []
        reload_task: asyncio.Task[object] | None = None
        # Guards against a vacuous pass: proves the hook body ran to its end
        # rather than the yield loop exiting on an early predicate.
        hook_completed = asyncio.Event()

        async with TestClient(TestServer(_make_app(state))) as client:

            async def _interleave(point: str) -> None:
                order.append(point)
                if point != "switch:post_commit":
                    return
                # Suspended inside the switch's span while it holds both locks.
                # Launch reload and hand the loop back a bounded number of
                # turns: a serialized reload cannot make progress, so this
                # proves it blocks rather than interleaves.
                nonlocal reload_task
                reload_task = asyncio.create_task(client.post(f"/api/chat/slots/{_SLOT}/reload"))
                blocked = not await _yield_until(
                    lambda: reload_task is not None and reload_task.done()
                )
                # Reload is blocked on the session lock the switch holds: it has
                # not reached its own pre-reset seam point.
                assert blocked, "reload completed while the switch held the session lock"
                assert "reload:pre_reset" not in order
                hook_completed.set()

            monkeypatch.setattr(chat_handlers, "_test_interleave", _interleave)
            switch_resp = await client.post(
                f"/api/chat/slots/{_SLOT}/model", json={"model": _MODEL_NEW}
            )

            assert reload_task is not None
            try:
                # The switch has released its locks by now, so the reload that
                # was blocked runs to completion in the serialized order.
                assert await _yield_until(
                    lambda: reload_task.done()
                ), "reload never completed after the switch released the locks"
                reload_resp = reload_task.result()
            finally:
                reload_task.cancel()
                await asyncio.gather(reload_task, return_exceptions=True)

            assert reload_resp.status == 200
            assert switch_resp.status == 200

        assert hook_completed.is_set()
        # The switch's whole commit-then-reset pair completes BEFORE reload's
        # pre-reset point: reload was blocked on the session lock until the
        # switch released it, so its teardown lands strictly after.
        assert order == [
            "switch:post_commit",
            "reset:pre_pop",
            "reset:post_pop",
            "reload:pre_reset",
            "reset:pre_pop",
            "reset:post_pop",
        ]
        assert state.sessions.reset.await_count == 2
