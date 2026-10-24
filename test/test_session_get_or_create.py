"""SessionManager allocation: ``get_or_create`` and the checks an allocation passes.

Among them: cancellation during a start, the model an allocation resolves and reports (including
``_resolve_agent_model``'s guarded read of an agent spec), dead-provider cleanup and the
``is_provider_alive`` verdict, warm-pool claims, concurrent allocations, the session state an
allocation records, and the per-key ending fence that holds or refuses an allocation while that key
is being torn down.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from conftest import requires_symlinks
from kiro_crew.acp.types import AcpPromptStats
from kiro_crew.session import SessionEndingError, SessionManager
from kiro_crew.testing.wait import async_wait_until, await_until


class TestCancelRaceCondition:
    """Tests for process leak prevention when CancelledError fires during get_or_create."""

    @pytest.mark.asyncio
    async def test_cancel_during_start_kills_provider(self, cfg):
        """CancelledError during provider.start() dispatches the process kill.

        The kill goes through _dispatch_hard_kill (non-blocking submission to
        the subprocess executor) rather than an inline _sync_kill_provider:
        the inline form blocks the event loop (os.waitpid / taskkill), and
        resume prefetch makes this cancellation handler routine — a focus
        flip mid-session/load cancels the loading task. Submission is
        synchronous, so the kill is guaranteed dispatched before the
        re-raise.
        """
        mock_provider = AsyncMock()
        mock_provider.start = AsyncMock(side_effect=asyncio.CancelledError)
        mock_provider._client = AsyncMock()
        mock_provider._client._pid = 99999

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            return mock_provider

        mgr = SessionManager(cfg, provider_factory=factory)

        with patch.object(SessionManager, "_dispatch_hard_kill") as mock_kill:
            with pytest.raises(asyncio.CancelledError):
                await mgr.get_or_create("test-cancel")

            mock_kill.assert_called_once_with(mock_provider)

        # Session must NOT be registered
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_after_start_before_registration_kills_provider(self, cfg):
        """CancelledError after start() but before _sessions[key] dispatches the kill.

        Same contract as the during-start case: _dispatch_hard_kill, never an
        inline _sync_kill_provider (which blocks the event loop). This handler
        is also the landing site for SpeculativeResumeRefused, so resume
        prefetch exercises it on every failed speculative load.
        """
        mock_provider = AsyncMock()
        mock_provider.start = AsyncMock()  # succeeds
        mock_provider.context_usage_pct = lambda: 0.0
        mock_provider._client = AsyncMock()
        mock_provider._client._pid = 88888
        mock_provider.is_alive.return_value = True

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            return mock_provider

        mgr = SessionManager(cfg, provider_factory=factory)
        original_lock = mgr._lock

        class CancelOnThirdLock:
            """Reservation and fast-path locks pass; registration cancels."""

            def __init__(self):
                self._calls = 0

            async def __aenter__(self):
                self._calls += 1
                if self._calls == 3:
                    raise asyncio.CancelledError
                return await original_lock.__aenter__()

            async def __aexit__(self, *a):
                if self._calls != 3:
                    return await original_lock.__aexit__(*a)

        with patch.object(SessionManager, "_dispatch_hard_kill") as mock_kill:
            mgr._lock = CancelOnThirdLock()
            with pytest.raises(asyncio.CancelledError):
                await mgr.get_or_create("test-cancel-2")

            mock_kill.assert_called_once_with(mock_provider)

        mgr._lock = original_lock
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_normal_path_unaffected(self, cfg):
        """Normal get_or_create still works after the cancel-safety changes."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, is_new, _ = await mgr.get_or_create("normal-session")

        assert is_new is True
        assert mgr.count == 1
        provider.start.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_model_forwarded_to_factory(self, cfg):
        """model param is forwarded to factory as model_override."""
        captured = {}

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            captured.update(kwargs)
            m = AsyncMock()
            m.start = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("test-model", model="claude-sonnet")
        assert captured["model_override"] == "claude-sonnet"
        await mgr.close_all()


class TestAllocationRequestedModel:
    """The model an allocation selects is readable by its caller.

    A caller that pins nothing passes ``model=None``, and the allocation resolves
    an id from config itself. ``get_or_create`` reports the provider, ``is_new``
    and ``resumed``, so that id is otherwise invisible to the caller recording what
    the session was asked to run.
    """

    @staticmethod
    def _capturing_factory(captured: dict):
        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            captured.update(kwargs)
            m = AsyncMock()
            m.start = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_process_alive = lambda: True
            m.is_alive.return_value = True
            return m

        return factory

    @pytest.mark.asyncio
    async def test_the_internally_resolved_id_is_the_id_the_provider_got(self, cfg):
        """One value: the stamp is the same string the factory received."""
        cfg.agent.model = "claude-sonnet-5"
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=self._capturing_factory(captured))

        await mgr.get_or_create("alloc-resolved")

        assert captured["model_override"] == "claude-sonnet-5"
        assert mgr.allocation_requested_model("alloc-resolved") == "claude-sonnet-5"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_callers_explicit_model_is_reported_unchanged(self, cfg):
        """An explicit model is stamped too, so one read serves both cases."""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=self._capturing_factory(captured))

        await mgr.get_or_create("alloc-explicit", model="claude-haiku-5")

        assert mgr.allocation_requested_model("alloc-explicit") == "claude-haiku-5"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_allocation_that_resolves_nothing_reports_nothing(self, cfg):
        """No tier resolved an id, so there is no selection to report."""
        cfg.agent.model = ""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=self._capturing_factory(captured))

        await mgr.get_or_create("alloc-blank")

        assert captured["model_override"] is None
        assert mgr.allocation_requested_model("alloc-blank") == ""
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_unknown_key_reports_nothing(self, cfg):
        """No session, so nothing to report — never an error."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.allocation_requested_model("never-allocated") == ""
        await mgr.close_all()


class TestDeadProviderCleanup:
    """Tests for orphaned child process cleanup when a dead provider is detected."""

    @staticmethod
    def _make_provider(*, alive: bool = True):
        """Create a mock provider with sync is_alive."""
        from unittest.mock import MagicMock

        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = MagicMock(return_value=0.0)
        m.is_alive = MagicMock(return_value=alive)
        m.is_process_alive = MagicMock(return_value=alive)
        return m

    @pytest.mark.asyncio
    async def test_dead_provider_calls_shutdown(self, cfg):
        """When is_alive() returns False, shutdown() is called on the stale provider."""
        dead_provider = self._make_provider(alive=True)
        call_count = 0

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return dead_provider if call_count == 1 else self._make_provider()

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")

        dead_provider.is_alive.return_value = False
        dead_provider.is_process_alive.return_value = False
        _, is_new, _ = await mgr.get_or_create("sess1")
        assert is_new is True
        dead_provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_dead_provider_removal_unlinks_temp_files_from_its_queue(self, cfg, tmp_path):
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        dead_provider = self._make_provider(alive=True)
        call_count = 0

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return dead_provider if call_count == 1 else self._make_provider()

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("sess1")
        mgr.enqueue("sess1", "ts1", "queued", force=True, image_temp_paths=[str(img)])
        mgr.release("sess1")

        dead_provider.is_alive.return_value = False
        dead_provider.is_process_alive.return_value = False
        await mgr.get_or_create("sess1")

        assert not img.exists()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_dead_provider_shutdown_exception_does_not_propagate(self, cfg):
        """If shutdown() raises on a dead provider, get_or_create still succeeds."""
        dead_provider = self._make_provider(alive=True)
        dead_provider.shutdown = AsyncMock(side_effect=OSError("kill failed"))
        call_count = 0

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return dead_provider if call_count == 1 else self._make_provider()

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")

        dead_provider.is_alive.return_value = False
        dead_provider.is_process_alive.return_value = False
        _, is_new, _ = await mgr.get_or_create("sess1")
        assert is_new is True
        assert mgr.count == 1
        dead_provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_dead_provider_removed_from_sessions(self, cfg):
        """Dead provider session is removed and replaced by a fresh one."""
        dead_provider = self._make_provider(alive=True)
        fresh_provider = self._make_provider()
        call_count = 0

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return dead_provider if call_count == 1 else fresh_provider

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")

        dead_provider.is_alive.return_value = False
        dead_provider.is_process_alive.return_value = False
        provider, is_new, _ = await mgr.get_or_create("sess1")
        assert provider is fresh_provider
        assert is_new is True
        assert mgr.count == 1
        await mgr.close_all()


class TestIsProviderAlive:
    """Tests for is_provider_alive preferring is_process_alive over is_alive."""

    @pytest.mark.asyncio
    async def test_uses_is_process_alive_when_available(self, cfg):
        provider = TestDeadProviderCleanup._make_provider(alive=True)
        provider.is_process_alive.return_value = True
        mgr = SessionManager(cfg, provider_factory=lambda *a, **kw: provider)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")
        result = await mgr.is_provider_alive("sess1")
        assert result is True
        provider.is_process_alive.assert_called()
        await mgr.close_all()


class TestApprovalPolicy:
    """Tests for approval policy get/set on sessions."""

    @pytest.mark.asyncio
    async def test_set_and_get_approval_policy(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")

        mgr.set_approval_policy("thread1", "auto")
        assert mgr.get_approval_policy("thread1") == "auto"

        mgr.set_approval_policy("thread1", "")
        assert mgr.get_approval_policy("thread1") == ""
        await mgr.close_all()

    def test_get_approval_policy_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_approval_policy("nonexistent") == ""

    def test_set_approval_policy_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.set_approval_policy("nonexistent", "auto") is None
        assert not mgr.has_session("nonexistent")

    @pytest.mark.asyncio
    async def test_approval_policy_propagated_on_create(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1", approval_policy="auto")
        mgr.release("thread1")
        assert mgr.get_approval_policy("thread1") == "auto"
        await mgr.close_all()


class TestGetAgent:
    """Tests for get_agent() on SessionManager."""

    @pytest.mark.asyncio
    async def test_get_agent_returns_agent_name(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1", agent="my-agent")
        mgr.release("thread1")
        assert mgr.get_agent("thread1") == "my-agent"
        await mgr.close_all()

    def test_get_agent_missing_session_returns_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_agent("nonexistent") == ""

    @pytest.mark.asyncio
    async def test_get_agent_no_agent_returns_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")
        assert mgr.get_agent("thread1") == ""
        await mgr.close_all()


class TestTheEndingFenceAdmitsNothingUnderTheKey:
    """``ending_key``: the per-key sibling of the closing check, at every door of the registry.

    A holder that is ending a run -- the cron reaper, ``cancel()`` -- raises the
    fence around its kill passes. While it is up, a claim or a cold start under
    the key is HELD at the front door of ``get_or_create``: it waits for the fence
    to lift and then proceeds (the sub-agent completion injector's cold start
    under the parent key, racing the reap, is delivered after it -- never
    dropped). A cold start that was already inside ``provider.start()`` when the
    fence went up -- nothing published yet, so no pass could see it -- is refused
    at registration when its start returns, fence up or lifted, the provider it
    started is hard-killed by the closing manager's own path, and the same call
    then waits for the lift and allocates again. ``SessionEndingError`` reaches a
    caller only when the fence outlives the wait bound, or from
    ``open_task_session``, which is refused rather than held.
    """

    @staticmethod
    def _gated_start_factory():
        """A provider factory whose ``start()`` signals ``started`` and blocks until ``gate`` is set."""
        gate = asyncio.Event()
        started = asyncio.Event()
        inner = _mock_provider_factory()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            provider = inner(session_key, agent, channel_id, **kwargs)

            async def _gated_start(*_args, **_kwargs):
                started.set()
                # flake-ok: pre-existing unbounded Event.wait in the gated start double; each test using it sets the gate; moved verbatim; tracked for a de-flake pass
                await gate.wait()

            provider.start = AsyncMock(side_effect=_gated_start)
            return provider

        return factory, gate, started

    @staticmethod
    async def _until_inside_start(started: asyncio.Event) -> None:
        # The reservation is taken before ``provider.start()`` is reached (a
        # thread hop sits between them), so ``_has_allocation_reservation`` is not the
        # signal: the start itself says when it is in flight.
        await await_until(started, what="provider.start() in flight")

    @staticmethod
    async def _until(condition, what: str) -> None:
        await async_wait_until(condition, describe=lambda: what)

    @staticmethod
    async def _settle() -> None:
        """Give a task every chance to run: enough loop turns for a door that does not wait."""
        for _ in range(20):
            # flake-ok: pre-existing 20-yield budget so a held or waiting get_or_create task reaches its door before the asserts; moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    @staticmethod
    def _fenced(mgr: SessionManager, key: str) -> bool:
        """Whether *key*'s ending fence is up, read from the allocation state itself."""
        return key in mgr._allocation_boundary().state.ending_keys

    @pytest.mark.asyncio
    async def test_a_cold_start_under_a_fenced_key_is_held_at_the_door_and_lands_when_it_lifts(
        self, cfg
    ):
        """The completion racing the fence: its cold start waits, reserving nothing, and lands after the record."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert not self._fenced(mgr, "k1")

        with mgr.ending_key("k1"):
            assert self._fenced(mgr, "k1")
            held = asyncio.create_task(mgr.get_or_create("k1"))
            await self._settle()
            assert not held.done(), (
                "a cold start racing the fence was answered while the fence was up: "
                f"{held.exception() if held.done() and held.exception() else 'it landed'}"
            )
            assert not mgr.has_session("k1")
            assert not mgr._has_allocation_reservation("k1"), "a held caller reserved something"

        assert not self._fenced(mgr, "k1")
        # The fence lifted: the held call lands, a new life under the recorded key.
        provider, is_new, _ = await await_until(held, what="the held cold start landing")
        assert is_new and mgr.has_session("k1")
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_claim_of_a_live_session_under_a_fenced_key_is_held_and_claims_after_the_lift(
        self, cfg
    ):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        with mgr.ending_key("k1"):
            held = asyncio.create_task(mgr.get_or_create("k1"))
            await self._settle()
            assert not held.done(), "a claim racing the fence was answered while the fence was up"

        again, is_new, _ = await await_until(held, what="the held claim landing")
        assert again is provider and not is_new
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_cold_start_caught_inside_start_is_refused_at_registration_hard_killed_and_held(
        self, cfg
    ):
        """The case no pass can see: reservation taken, ``provider.start()`` in flight, nothing published.

        The start returns while the fence is still up: the registration is refused,
        the started provider hard-killed, and the call is then held like one that
        met the fence at the door -- it lands, with a fresh provider, once the
        fence lifts.
        """
        factory, gate, started = self._gated_start_factory()
        mgr = SessionManager(cfg, provider_factory=factory)
        cold_start = asyncio.create_task(mgr.get_or_create("k1"))
        await self._until_inside_start(started)
        assert not mgr.has_session("k1"), "nothing is published while start() runs"

        with patch.object(mgr, "_dispatch_hard_kill") as hard_kill:
            with mgr.ending_key("k1"):
                # The ending caller's post-pass read: the start is still in flight.
                assert mgr._has_allocation_reservation("k1")
                gate.set()
                await self._until(
                    lambda: hard_kill.call_count == 1,
                    "the start returned under the fence and its provider was not hard-killed",
                )
                await self._settle()
                assert not cold_start.done(), (
                    "the call whose start the fence caught was answered while the fence was up: "
                    f"{cold_start.exception() if cold_start.exception() else 'it landed'}"
                )
                assert not mgr.has_session("k1"), (
                    "a cold start caught inside provider.start() by the ending fence published "
                    "its session under the fenced key"
                )
                assert not mgr._has_allocation_reservation(
                    "k1"
                ), "the refused allocation kept its reservation while held"
            provider, is_new, _ = await await_until(cold_start, what="the cold start landing")

        hard_kill.assert_called_once()
        (killed,) = hard_kill.call_args.args
        killed.start.assert_awaited_once()
        assert is_new and provider is not killed and mgr.has_session("k1")
        assert not mgr._has_allocation_reservation("k1")
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_start_the_fence_caught_is_refused_at_registration_after_the_lift_and_allocates_again(
        self, cfg
    ):
        """Fence up during the start, lifted before it returns: still refused there, the provider killed, and the call lands at once."""
        factory, gate, started = self._gated_start_factory()
        mgr = SessionManager(cfg, provider_factory=factory)
        cold_start = asyncio.create_task(mgr.get_or_create("k1"))
        await self._until_inside_start(started)

        with patch.object(mgr, "_dispatch_hard_kill") as hard_kill:
            with mgr.ending_key("k1"):
                assert mgr._has_allocation_reservation("k1")
            # The fence has lifted by the time the start returns -- the ending
            # caller wrote its record and moved on -- and the registration is
            # refused all the same: the reservation was in flight when the fence
            # went up. The call allocates again without waiting.
            gate.set()
            provider, is_new, _ = await await_until(cold_start, what="the cold start landing")

        hard_kill.assert_called_once()
        (killed,) = hard_kill.call_args.args
        killed.start.assert_awaited_once()
        assert is_new and provider is not killed and mgr.has_session("k1")
        assert not mgr._has_allocation_reservation(
            "k1"
        ), "the refused call released its reservation"
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_an_allocation_fenced_before_it_started_anything_is_held_and_starts_after_the_lift(
        self, cfg
    ):
        """Reservation taken, no provider yet (the fence lands during the pre-claim read): refused at the first door, nothing to kill, held, then started."""
        first_read = threading.Event()
        reads = 0

        def _blocking_first_read(key):
            nonlocal reads
            reads += 1
            if reads == 1:
                assert first_read.wait(5), "the test never released the pre-claim read"
            return None

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        with (
            patch("kiro_crew.execution_context.read_session_execution", _blocking_first_read),
            patch.object(mgr, "_dispatch_hard_kill") as hard_kill,
        ):
            cold_start = asyncio.create_task(mgr.get_or_create("k1"))
            await self._until(
                lambda: mgr._has_allocation_reservation("k1"),
                "the cold start never took its reservation",
            )
            # A reservation ahead of the spawn door is not a start: the ending
            # caller's read must not name it.
            assert not mgr._spawn_in_flight(
                "k1"
            ), "an allocation blocked in its pre-claim read was read as a start in flight"
            with mgr.ending_key("k1"):
                first_read.set()
                await self._until(
                    lambda: not mgr._has_allocation_reservation("k1"),
                    "the fenced allocation was not refused at its first door",
                )
                await self._settle()
                assert not cold_start.done(), (
                    "an allocation the fence caught before it started anything was answered "
                    "while the fence was up"
                )
                assert not mgr.has_session("k1")
            provider, is_new, _ = await await_until(cold_start, what="the cold start landing")

        hard_kill.assert_not_called()  # nothing had been started when the fence caught it
        assert is_new and mgr.has_session("k1")
        provider.start.assert_awaited_once()
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_cold_start_is_a_spawn_in_flight_from_its_spawn_door_until_it_registers(
        self, cfg
    ):
        """``_spawn_in_flight``: true exactly while a start is past the pre-spawn check and unregistered."""
        factory, gate, started = self._gated_start_factory()
        mgr = SessionManager(cfg, provider_factory=factory)
        assert not mgr._spawn_in_flight("k1")

        cold_start = asyncio.create_task(mgr.get_or_create("k1"))
        await self._until_inside_start(started)
        assert mgr._has_allocation_reservation("k1")
        assert mgr._spawn_in_flight(
            "k1"
        ), "a cold start inside provider.start() was not read as a start in flight"
        assert not mgr.has_session("k1"), "nothing is published while start() runs"

        gate.set()
        provider, is_new, _ = await await_until(cold_start, what="the cold start landing")

        assert is_new and mgr.has_session("k1")
        assert not mgr._spawn_in_flight(
            "k1"
        ), "a registered session was still read as a start in flight"
        assert not mgr._has_allocation_reservation("k1")
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_start_refused_while_the_fence_is_up_leaves_a_receipt_until_the_lift(self, cfg):
        """The receipt: a start the fence caught, refused and hard-killed before the holder read the key is still named.

        Its reservation is removed with the refusal, so without the receipt the
        holder's post-pass read found nothing -- and a dispatched hard kill has no
        outcome this process reads back. The receipt names it until ``end_ending``,
        and only a refusal that happens while the fence is up leaves one.
        """
        factory, gate, started = self._gated_start_factory()
        mgr = SessionManager(cfg, provider_factory=factory)

        with patch.object(mgr, "_dispatch_hard_kill") as hard_kill:
            cold_start = asyncio.create_task(mgr.get_or_create("k1"))
            await self._until_inside_start(started)
            with mgr.ending_key("k1"):
                assert mgr._spawn_in_flight("k1") == (
                    "refused at registration, its provider hard-killed there"
                )
                gate.set()
                await self._until(
                    lambda: hard_kill.call_count == 1,
                    "the start that returned under the fence was not refused and hard-killed",
                )
                await self._until(
                    lambda: not mgr._has_allocation_reservation("k1"),
                    "the refused call kept its reservation",
                )
                assert mgr._spawn_in_flight("k1") == (
                    "1 refused at registration during the ending, the provider hard-killed there "
                    "by the allocation path -- an outcome this record does not confirm"
                ), "the refused start left no receipt for the ending caller's read"
            # The fence lifted: the receipt went with it, and the held call lands.
            assert mgr._spawn_in_flight("k1") is None
            provider, is_new, _ = await await_until(cold_start, what="the cold start landing")

        hard_kill.assert_called_once()
        assert is_new and mgr.has_session("k1")
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_start_refused_after_the_lift_leaves_no_receipt(self, cfg):
        """A late return after the holder's record is written has no reader left: no receipt lingers for the next ending."""
        factory, gate, started = self._gated_start_factory()
        mgr = SessionManager(cfg, provider_factory=factory)

        with patch.object(mgr, "_dispatch_hard_kill") as hard_kill:
            cold_start = asyncio.create_task(mgr.get_or_create("k1"))
            await self._until_inside_start(started)
            with mgr.ending_key("k1"):
                pass
            gate.set()
            provider, is_new, _ = await await_until(cold_start, what="the cold start landing")

        hard_kill.assert_called_once()
        assert is_new and mgr.has_session("k1")
        assert mgr._spawn_in_flight("k1") is None, "a refusal after the lift left a receipt behind"
        with mgr.ending_key("k1"):
            assert mgr._spawn_in_flight("k1") is None, "a stale receipt reached the next ending"
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_warm_pool_claim_refused_under_the_fence_leaves_the_same_receipt(self, cfg):
        """The pool path owns a LIVE process from the claim on; refused at registration under the fence, it is named like any start.

        The pooled provider never passes the cold start's pre-spawn door, so
        without its own mark the refusal's cleanup erased the claim before the
        ending caller's read: a hard kill dispatched at the process, nothing to
        say so, and a record that said ``reaped``.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        pooled = _mock_provider_factory()(None, None, None)
        claim_gate = asyncio.Event()
        claimed = asyncio.Event()
        real_drain = mgr._drain_and_claim

        async def _gated_claim(agent):
            claimed.set()
            # flake-ok: pre-existing unbounded Event.wait in the gated claim double; the test sets the gate once the claim enters; moved verbatim; tracked for a de-flake pass
            await claim_gate.wait()
            return await real_drain(agent)

        mgr._warm_pool.put_nowait((pooled, time.monotonic()))
        with (
            patch.object(mgr, "_drain_and_claim", side_effect=_gated_claim),
            patch.object(mgr, "_dispatch_hard_kill") as hard_kill,
        ):
            call = asyncio.create_task(mgr.get_or_create("k1"))
            await await_until(claimed, what="the gated claim entering")
            with mgr.ending_key("k1"):
                # The fence goes up while the claim is in flight; the claim then
                # returns a live pooled process that registration refuses.
                claim_gate.set()
                await self._until(
                    lambda: hard_kill.call_count == 1,
                    "the pooled provider refused at registration was not hard-killed",
                )
                await self._until(
                    lambda: not mgr._has_allocation_reservation("k1"),
                    "the refused claim kept its reservation",
                )
                assert mgr._spawn_in_flight("k1") == (
                    "1 refused at registration during the ending, the provider hard-killed there "
                    "by the allocation path -- an outcome this record does not confirm"
                ), "the refused warm-pool claim left no receipt for the ending caller's read"
            provider, is_new, _ = await await_until(call, what="the gated claim landing")

        hard_kill.assert_called_once_with(pooled)
        assert is_new and provider is not pooled and mgr.has_session("k1")
        assert mgr._spawn_in_flight("k1") is None
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_claim_waiting_on_the_busy_session_s_turn_is_not_a_spawn_in_flight(self, cfg):
        """A reservation is not a process: a claim parked on the live session's turn permit started nothing.

        The sub-agent completion's claim of its busy parent -- ``get_or_create``
        blocked on the turn semaphore -- holds a reservation for the whole wait.
        The ending caller's read must not name it: it is held or woken, never
        refused at a registration it never reaches, and no provider of its own
        is started. Its reservation reads as an allocation, not as a spawn.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")  # the turn: permit held

        waiter = asyncio.create_task(mgr.get_or_create("k1"))
        await self._settle()
        assert not waiter.done(), "the claim did not wait on the busy turn"
        assert mgr._has_allocation_reservation("k1"), "the waiting claim holds a reservation"
        assert not mgr._spawn_in_flight(
            "k1"
        ), "a claim waiting on the busy session's turn was read as a start in flight"
        with mgr.ending_key("k1"):
            # The ending caller's post-pass read: still nothing to name.
            assert not mgr._spawn_in_flight("k1")

        mgr.release("k1")
        claimed, is_new, _ = await await_until(waiter, what="the held waiter landing")
        assert claimed is provider and not is_new
        assert not mgr._has_allocation_reservation("k1")
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_waiter_the_busy_turn_releases_to_while_the_fence_is_up_is_held_not_handed_the_session(
        self, cfg
    ):
        """The turn's ordinary release can reach the waiter before the ending caller's reset pops the session.

        A claim blocked on the busy parent's turn permit passed the fence check
        at the claim door before its wait began; the wait can outlast a fence
        rising. If the busy turn then ends normally -- its permit released
        before the ending caller's reset has popped the session -- the waiter
        acquires the permit with the session still in the map. Without the
        recheck it is handed the session and runs a turn under a key being
        ended, which the reset then tears down under it. The fence is met
        again after the acquire, under the lock: the waiter releases the permit
        and is held at the front door until the lift, then lands.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")  # the turn: permit held

        waiter = asyncio.create_task(mgr.get_or_create("k1"))
        await self._settle()
        assert not waiter.done(), "the claim did not wait on the busy turn"

        with mgr.ending_key("k1"):
            # The fence is up; the ending caller has not popped the session yet.
            # The busy turn ends normally and hands the permit to the waiter.
            mgr.release("k1")
            await async_wait_until(
                lambda: not mgr._has_allocation_reservation("k1"),
                describe=lambda: "the held waiter never reached the front door (still reserved)",
            )
            assert not waiter.done(), (
                "the waiter the busy turn released to was handed the session while its key "
                "was being ended: the fence was bypassed after the semaphore wait"
            )
            assert not mgr._sessions[
                "k1"
            ].semaphore.locked(), "the refused waiter kept the permit it must release"

        claimed, is_new, _ = await await_until(waiter, what="the held waiter landing")
        assert claimed is provider and not is_new, "the held waiter did not land after the lift"
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_fence_held_past_the_wait_bound_refuses_the_held_caller(self, cfg, monkeypatch):
        """The one refusal a held caller can meet: a fence that outlives its bound is a stuck holder, surfaced."""
        from kiro_crew import session_allocation

        # flake-ok: cuts the held caller's fence wait to 50 ms; the test holds the fence across the await so the refusal is certain; moved verbatim
        monkeypatch.setattr(session_allocation, "ENDING_FENCE_WAIT_SECS", 0.05)
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with mgr.ending_key("k1"):
            with pytest.raises(SessionEndingError, match="still being ended after 0s"):
                await mgr.get_or_create("k1")
            assert not mgr.has_session("k1")
            assert not mgr._has_allocation_reservation("k1")

        provider, is_new, _ = await mgr.get_or_create("k1")
        assert is_new
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_reservation_taken_after_the_fence_lifted_is_not_invalidated(self, cfg):
        """Only the reservations in flight when the fence went up are invalidated -- a later one is a new life."""
        factory, gate, started = self._gated_start_factory()
        mgr = SessionManager(cfg, provider_factory=factory)

        with mgr.ending_key("k1"):
            pass
        cold_start = asyncio.create_task(mgr.get_or_create("k1"))
        await self._until_inside_start(started)
        gate.set()

        # flake-ok: pre-existing unbounded await of the cold start the test releases by setting its gate just above; moved verbatim; tracked for a de-flake pass
        provider, is_new, _ = await cold_start
        assert is_new and mgr.has_session("k1")
        provider.start.assert_awaited_once()
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_the_fence_lifts_however_the_block_ends(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with pytest.raises(RuntimeError, match="the holder's kill raised"):
            with mgr.ending_key("k1"):
                raise RuntimeError("the holder's kill raised")

        assert not self._fenced(mgr, "k1")
        provider, is_new, _ = await mgr.get_or_create("k1")
        assert is_new
        mgr.release("k1")
        await mgr.reset("k1")

    @pytest.mark.asyncio
    async def test_a_per_step_task_session_under_a_fenced_key_is_refused_before_it_is_created(
        self, cfg
    ):
        """The other publication door, gated at its entry: nothing is created for a fenced key."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        parent, _, _ = await mgr.get_or_create("parent")
        mgr.release("parent")

        with (
            patch.object(mgr, "_get_or_bootstrap_run_runtime", AsyncMock()) as bootstrap,
            mgr.ending_key("step1"),
        ):
            with pytest.raises(SessionEndingError, match="being ended"):
                await mgr.open_task_session("parent", "step1")

        bootstrap.assert_not_awaited()
        assert not mgr.has_session("step1")
        await mgr.reset("parent")


class TestResolveAgentModelResolution:
    """Tests for _resolve_agent_model()."""

    def test_resolve_agent_model_cache_miss_returns_auto(self, cfg):
        # Clear cache if exists
        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        result = SessionManager._resolve_agent_model("nonexistent-agent-xyz")
        assert result == "auto"

    def test_resolve_agent_model_from_file(self, cfg, tmp_path):
        """Reads model from agent JSON file."""
        import json

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        agent_file = tmp_path / "test-agent.json"
        agent_file.write_text(json.dumps({"name": "test-agent", "model": "opus-5"}))

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
            result = SessionManager._resolve_agent_model("test-agent")
        assert result == "opus-5"

    def test_resolve_agent_model_coerces_non_string_spec(self, cfg, tmp_path):
        """A foreign spec's structured ``model`` must not escape as a dict.

        ``~/.kiro/agents`` is shared with other tools; an ACP-style
        ``{"id": ...}`` here would be CACHED and then handed to
        the pooled-model comparison in ``claim_pooled``. This method is
        annotated ``-> str`` and must honour that.
        """
        import json

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        agent_file = tmp_path / "foreign.json"
        agent_file.write_text(
            json.dumps({"name": "foreign", "model": {"id": "anthropic:claude-opus-4-8"}})
        )

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
            result = SessionManager._resolve_agent_model("foreign")
        assert result == "auto"
        assert isinstance(result, str)

    def test_resolve_agent_model_refuses_an_oversized_spec(self, tmp_path, monkeypatch):
        """The scan reads through the hardened, size-capped reader.

        ``~/.kiro/agents`` is user-writable and shared with kiro-cli, so an
        oversized "agent config" there must be refused rather than slurped into
        memory — and this resolution is CACHED and reused on every later
        lookup, so it is not a rare corner.

        Exercised with a LOWERED cap rather than a real 50 MB fixture; the
        property is that the cap is consulted, not its value. Paired with the
        A-side below so the refusal cannot pass by breaking every read.
        """
        import json

        from kiro_crew import hooks

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 256)
        (tmp_path / "big.json").write_text(
            json.dumps({"name": "big", "model": "pinned-by-oversized", "pad": "x" * 1024})
        )

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
            assert SessionManager._resolve_agent_model("big") == "auto"

    def test_resolve_agent_model_still_reads_a_spec_under_the_same_cap(self, tmp_path, monkeypatch):
        """A-side of the cap test above: a normal spec still resolves."""
        import json

        from kiro_crew import hooks

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 256)
        (tmp_path / "small.json").write_text(
            json.dumps({"name": "small", "model": "pinned-by-small"})
        )

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
            assert SessionManager._resolve_agent_model("small") == "pinned-by-small"

    @requires_symlinks
    def test_resolve_agent_model_refuses_a_link_to_a_sensitive_target(self, tmp_path, monkeypatch):
        """A spec that is a symlink resolving onto a sensitive target is refused,
        so the model is not resolved out of whatever the link names."""
        import json

        from kiro_crew import agent_discovery

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        target = tmp_path / "protected.json"
        target.write_text(json.dumps({"model": "leaked-value"}))
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "linked.json").symlink_to(target)
        monkeypatch.setattr(
            agent_discovery, "is_sensitive_canonical_path", lambda p: str(target) in str(p)
        )

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents):
            assert SessionManager._resolve_agent_model("linked") == "auto"


class TestIsProviderAliveProcessVerdict:
    """Test is_provider_alive reads the provider's process-level verdict.

    The is_alive fallback for a provider that does not override
    ``is_process_alive`` lives in the LLMProvider ABC default, not here —
    it is pinned by the ABC contract tests in
    ``test_session_provider_liveness.py``.
    """

    @pytest.mark.asyncio
    async def test_returns_the_process_liveness_verdict(self, cfg):
        from unittest.mock import MagicMock

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.is_process_alive = MagicMock(return_value=True)
        assert await mgr.is_provider_alive("k1") is True
        provider.is_process_alive = MagicMock(return_value=False)
        assert await mgr.is_provider_alive("k1") is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_session_returns_none(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        result = await mgr.is_provider_alive("nonexistent")
        assert result is None


class TestGetOrCreatePoolClaim:
    """Test get_or_create claiming from warm pool."""

    @pytest.mark.asyncio
    async def test_claims_from_pool_on_new_session(self, cfg):
        from kiro_crew.providers.acp import AcpProvider

        cfg.session.pool_size = 1
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 1
        mgr._pool_agent = "kirocrew"

        # Pre-fill pool with a mock provider that looks like AcpProvider
        mock_pooled = AsyncMock(spec=AcpProvider)
        mock_pooled.start = AsyncMock()
        mock_pooled.shutdown = AsyncMock()
        mock_pooled.context_usage_pct = lambda: 0.0
        mock_pooled.is_process_alive = lambda: True
        mock_pooled.client = AsyncMock()
        mock_pooled.client._model = "claude-opus-4"
        mock_pooled.client._agent = "kirocrew"
        mock_pooled.client._session_id = None
        mock_pooled.client.rekey = lambda *a, **kw: None
        mock_pooled.client.resumed = False

        mgr._warm_pool.put_nowait((mock_pooled, time.monotonic()))

        provider, is_new, _ = await mgr.get_or_create("dashboard:slot1", agent="kirocrew")
        mgr.release("dashboard:slot1")
        assert provider is mock_pooled
        assert is_new is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_pool_claim_resets_stale_context_and_skips_compaction(self, cfg):
        """End-to-end: a pooled provider carrying a previous session's
        context stats must not hand them to the claiming session. The claim
        path calls client.rekey(), whose reset makes the first turn-end
        check_context_usage read 0%/unknown instead of firing compaction on
        an empty conversation."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.providers.acp import AcpProvider

        cfg.session.pool_size = 1
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 1
        mgr._pool_agent = "kirocrew"

        # Real (unstarted) AcpClient seeded with the PREVIOUS session's stats —
        # the exact leak shape from the issue: high confirmed pct, real counts.
        real_client = AcpClient()
        real_client.last_prompt_stats = AcpPromptStats(
            context_pct=95.0,
            context_used_tokens=190_000,
            context_window_tokens=200_000,
            context_tokens_from_usage=True,
        )

        mock_pooled = AsyncMock(spec=AcpProvider)
        mock_pooled.start = AsyncMock()
        mock_pooled.shutdown = AsyncMock()
        mock_pooled.is_process_alive = lambda: True
        mock_pooled.client = real_client
        # Route the provider probes through the real client stats (mirrors
        # AcpProvider.context_usage_pct / context_usage_unknown).
        mock_pooled.context_usage_pct = lambda: real_client.last_prompt_stats.context_pct
        mock_pooled.context_usage_unknown = (
            lambda: real_client.last_prompt_stats.context_pct_unknown
        )

        mgr._warm_pool.put_nowait((mock_pooled, time.monotonic()))

        provider, is_new, _ = await mgr.get_or_create("dashboard:slot1", agent="kirocrew")
        mgr.release("dashboard:slot1")
        assert provider is mock_pooled

        # The handoff dropped the stale session-scoped state (back to plain
        # defaults — NOT flagged unknown, which would collide with the
        # compacted-in-place recycle predicate)...
        stats = real_client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 0
        assert stats.context_pct_unknown is False

        # ...so the first turn-end check does not compact the empty session.
        with patch.object(mgr, "_compact_session", new_callable=AsyncMock) as compact:
            pct = mgr.check_context_usage("dashboard:slot1", provider)
        compact.assert_not_called()
        assert "dashboard:slot1" not in mgr._compacting
        assert pct == 0.0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cold_start_with_resume_sid(self, cfg):
        """get_or_create with a stored session_map entry attempts resume."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        with (
            patch.object(mgr._session_map, "get", return_value="sid-resume-test"),
            patch.object(mgr._session_map, "get_cwd", return_value=None),
            patch.object(mgr._session_map, "get_provider", return_value="acp"),
        ):
            provider, is_new, _ = await mgr.get_or_create("dashboard:slot2")
            mgr.release("dashboard:slot2")
        assert is_new is True
        assert mgr.has_session("dashboard:slot2")
        await mgr.close_all()


class TestGetOrCreateDeadProvider:
    """Test get_or_create when existing session has a dead provider."""

    @pytest.mark.asyncio
    async def test_dead_provider_gets_replaced(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # Mark provider as dead
        provider.is_alive = lambda: False
        provider.is_process_alive = lambda: False
        # Next get_or_create should detect dead provider and create new one
        new_provider, is_new, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        assert new_provider is not provider
        await mgr.close_all()


class TestConcurrentAccess:
    @pytest.mark.asyncio
    async def test_concurrent_get_or_create_same_key(self, cfg):
        """Second get_or_create on same key reuses existing session."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p1, new1, _ = await mgr.get_or_create("shared")
        mgr.release("shared")
        p2, new2, _ = await mgr.get_or_create("shared")
        mgr.release("shared")
        assert p1 is p2
        assert new1 is True
        assert new2 is False
        assert mgr.count == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_concurrent_different_keys(self, cfg):
        """Different keys should create independent sessions."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # flake-ok: pre-existing unbounded gather of three cold starts on mock providers that complete on their own; moved verbatim
        results = await asyncio.gather(  # noqa: F841
            mgr.get_or_create("a"),
            mgr.get_or_create("b"),
            mgr.get_or_create("c"),
        )
        assert mgr.count == 3
        for key in ("a", "b", "c"):
            mgr.release(key)
        await mgr.close_all()


class TestSessionState:
    @pytest.mark.asyncio
    async def test_is_new_flag(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _, is_new1, _ = await mgr.get_or_create("t1")
        mgr.release("t1")
        _, is_new2, _ = await mgr.get_or_create("t1")
        mgr.release("t1")
        assert is_new1 is True
        assert is_new2 is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_release_updates_last_used(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("t1")
        mgr.release("t1")
        async with mgr._lock:
            sess = mgr._sessions["t1"]
        # last_used should be recent (within last second)
        # flake-ok: pre-existing real-clock check that release stamped last_used within 1 s; a worker stall over 1 s reds it; moved verbatim; tracked for a de-flake pass
        assert time.monotonic() - sess.last_used < 1.0
        await mgr.close_all()
