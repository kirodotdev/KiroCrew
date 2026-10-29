"""SessionManager ending verbs and what each one clears.

Among them: provider drains, ``reset`` (the torn-down entry it retains, the PID capture and
force-kill), ``destroy`` (including its conditional, generation-guarded form), ``discard_conversation``
and the replay suppression that makes it stick, ``remove``, ``close_all`` and its session-map
persistence, ``_safe_cleanup``, and the compaction cooldown each lifecycle event prunes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    _raw_sid,
    cfg_fixture,
)

from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import async_wait_until, await_until


class TestDrainProviders:
    """Tests for drain_all_providers and drain_warm_pool."""

    @pytest.mark.asyncio
    async def test_drain_all_providers(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.get_or_create("k2")
        mgr.release("k2")
        providers = await mgr.drain_all_providers()
        assert len(providers) == 2
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_all_providers_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        providers = await mgr.drain_all_providers()
        assert providers == []

    @pytest.mark.asyncio
    async def test_drain_all_providers_unlinks_temp_files_from_every_queue(self, cfg, tmp_path):
        img1 = tmp_path / "img1.png"
        img2 = tmp_path / "img2.png"
        img1.write_bytes(b"fake")
        img2.write_bytes(b"fake")
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts2", "second", force=True, image_temp_paths=[str(img1)])
        await mgr.get_or_create("k2")
        mgr.enqueue("k2", "ts3", "third", force=True, image_temp_paths=[str(img2)])

        await mgr.drain_all_providers()

        assert not img1.exists()
        assert not img2.exists()

    @pytest.mark.asyncio
    async def test_drain_warm_pool(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Manually put items in the warm pool
        mock_p = AsyncMock()
        mgr._warm_pool.put_nowait((mock_p, "agent1"))
        drained = await mgr.drain_warm_pool()
        assert len(drained) == 1
        assert drained[0] is mock_p
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_warm_pool_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        drained = await mgr.drain_warm_pool()
        assert drained == []


class TestResetRetainsTheTornDownSession:
    """``reset`` keeps the popped session readable for exactly the life of its teardown.

    The pop happens under the registry lock before the awaits that can hang, so
    from then on the live map does not name the process the teardown holds. A
    reader that must still reach it -- the cron reaper, after a run's own finally
    reset popped the session and hung -- reads ``tearing_down(key)``. The entry is
    recorded at the pop and released when the reset ends, however it ends.
    """

    @staticmethod
    def _hang_shutdown(provider):
        gate = asyncio.Event()

        async def _hung():
            # flake-ok: hung shutdown double: the gate is never set and every test cancels the reset task parked here so the wait ends by that cancel; moved verbatim
            await gate.wait()

        provider.shutdown = AsyncMock(side_effect=_hung)
        return gate

    @staticmethod
    async def _until_the_shutdown_hangs(mgr, key, provider):
        """Wait until the teardown has popped the session and entered the hung shutdown.

        The cancel below has to land IN the shutdown: a cancellation that lands one
        await earlier, at ``record_session_ended``'s crumb hop, is absorbed by
        design (the pop is the point of no return, so the teardown runs on) -- and a
        teardown that runs on into a hung shutdown keeps its entry, correctly.
        """
        await async_wait_until(
            lambda: not mgr.has_session(key) and provider.shutdown.await_count,
            describe=lambda: f"the teardown of {key} never reached the provider shutdown",
        )

    @pytest.mark.asyncio
    async def test_the_popped_session_is_readable_while_its_teardown_runs_and_gone_after(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        assert mgr.tearing_down("k1") == [], "no teardown is in flight yet"
        self._hang_shutdown(provider)

        teardown = asyncio.create_task(mgr.reset("k1"))
        await self._until_the_shutdown_hangs(mgr, "k1", provider)

        torn = mgr.tearing_down("k1")
        assert [entry.session.provider for entry in torn] == [provider]
        assert not mgr.has_session("k1")

        # The teardown is cancelled out from under its hung shutdown (the reaper's
        # cancel of a run task lands exactly here): the entry goes with it.
        teardown.cancel()
        # flake-ok: gathers the reset cancelled in its hung shutdown; it re-raises after local steps; no timeout; moved verbatim; tracked for a de-flake pass
        await asyncio.gather(teardown, return_exceptions=True)
        assert mgr.tearing_down("k1") == []

    @pytest.mark.asyncio
    async def test_the_entry_keeps_the_process_handle_read_at_the_pop_after_the_teardown_clears_the_pid(
        self, cfg
    ):
        """The retained handle is the pop-time identity, not a re-read of the session.

        The ACP client's reset clears its recorded pid after a kill it could not
        confirm and can then hang on the transport -- so a reader that re-read the
        popped session found no pid and named no process while the process stood.
        The table captures the handle in the pop's own lock hold and never reads
        the session again.
        """
        from kiro_crew.process_identity import process_handle_of

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        client = MagicMock()
        client._pid = 2**22 + 4141
        client._child_pids = {}
        client._start_time = "start-4141"
        provider._client = client
        self._hang_shutdown(provider)

        teardown = asyncio.create_task(mgr.reset("k1"))
        await self._until_the_shutdown_hangs(mgr, "k1", provider)
        # What the client's reset does after a failed kill, with the process still up.
        client._pid = None

        (entry,) = mgr.tearing_down("k1")
        assert process_handle_of(entry.session).pid is None, "the re-read still names the pid"
        assert (entry.handle.pid, entry.handle.start_id) == (2**22 + 4141, "start-4141"), (
            "the torn-down entry lost the process identity its pop captured: " f"{entry.handle}"
        )

        teardown.cancel()
        # flake-ok: gathers the reset cancelled in its hung shutdown; it re-raises after an unpatched pid probe; no timeout; moved verbatim; tracked for a de-flake pass
        await asyncio.gather(teardown, return_exceptions=True)
        assert mgr.tearing_down("k1") == []

    @pytest.mark.asyncio
    async def test_a_completed_reset_leaves_no_entry(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        assert await mgr.reset("k1") is True

        provider.shutdown.assert_awaited_once()
        assert mgr.tearing_down("k1") == []

    @pytest.mark.asyncio
    async def test_a_reset_whose_shutdown_raises_still_releases_the_entry(self, cfg):
        """``reset`` defers a shutdown error to its end and re-raises it; the entry is gone by then."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.shutdown = AsyncMock(side_effect=RuntimeError("shutdown failed"))

        with pytest.raises(RuntimeError, match="shutdown failed"):
            await mgr.reset("k1")

        assert mgr.tearing_down("k1") == []

    @pytest.mark.asyncio
    async def test_a_successor_torn_down_during_the_teardown_is_retained_beside_the_first(
        self, cfg
    ):
        """Every teardown in flight under the key is readable: the first popper's AND a hung successor's.

        A cold start can register a successor under the key while the first
        teardown awaits, and that successor's own reset can pop it and hang as
        well. A reader ending the key must reach both processes, so both are
        retained, in pop order, and each entry leaves exactly when its own
        teardown ends -- the first's cancellation does not release the second's.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        first, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        self._hang_shutdown(first)
        teardown = asyncio.create_task(mgr.reset("k1"))
        await self._until_the_shutdown_hangs(mgr, "k1", first)

        successor, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        assert successor is not first
        self._hang_shutdown(successor)
        second_teardown = asyncio.create_task(mgr.reset("k1"))
        await self._until_the_shutdown_hangs(mgr, "k1", successor)

        assert [entry.session.provider for entry in mgr.tearing_down("k1")] == [
            first,
            successor,
        ], "a successor whose own reset popped it and hung was not retained beside the first"

        teardown.cancel()
        # flake-ok: gathers the first reset cancelled in its hung shutdown; it re-raises after local steps; no timeout; moved verbatim; tracked for a de-flake pass
        await asyncio.gather(teardown, return_exceptions=True)
        assert [entry.session.provider for entry in mgr.tearing_down("k1")] == [
            successor
        ], "ending the first teardown released the successor's entry"

        second_teardown.cancel()
        # flake-ok: gathers the successor reset cancelled in its hung shutdown; it re-raises after local steps; no timeout; moved verbatim; tracked for a de-flake pass
        await asyncio.gather(second_teardown, return_exceptions=True)
        assert mgr.tearing_down("k1") == []

    @pytest.mark.asyncio
    async def test_a_successor_s_completed_reset_leaves_the_first_entry_alone(self, cfg):
        """A successor reset that completes releases only its own entry; the hung first teardown's stays."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        first, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        self._hang_shutdown(first)
        teardown = asyncio.create_task(mgr.reset("k1"))
        await self._until_the_shutdown_hangs(mgr, "k1", first)

        successor, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        assert await mgr.reset("k1") is True

        successor.shutdown.assert_awaited_once()
        assert [entry.session.provider for entry in mgr.tearing_down("k1")] == [
            first
        ], "the successor's completed reset touched the first teardown's entry"

        teardown.cancel()
        # flake-ok: gathers the first reset cancelled in its hung shutdown after the successor reset is done; no timeout; moved verbatim; tracked for a de-flake pass
        await asyncio.gather(teardown, return_exceptions=True)
        assert mgr.tearing_down("k1") == []

    @pytest.mark.asyncio
    async def test_a_caller_s_scope_names_the_exact_session_its_reset_popped(self, cfg):
        """The ``on_pop`` hook: what THIS reset popped, read atomically with the pop, before the session leaves the map."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        seen: list[tuple[object, bool]] = []
        scope = mgr.teardown_scope(
            on_pop=lambda session: seen.append((session, mgr.has_session("k1")))
        )

        assert await mgr.reset("k1", scope=scope) is True

        # The hook ran once, in the pop's own lock hold, before the session left
        # the map, and was handed the exact session this reset popped.
        assert len(seen) == 1 and seen[0][0].provider is provider and seen[0][1] is True
        # The table entry is released with the teardown.
        assert mgr.tearing_down("k1") == []

    @pytest.mark.asyncio
    async def test_a_scope_whose_reset_popped_nothing_runs_no_hook(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        seen: list[object] = []
        scope = mgr.teardown_scope(on_pop=seen.append)

        assert await mgr.reset("absent", scope=scope) is False

        assert seen == []


class TestResetWithPid:
    """Tests for reset() PID capture and force-kill logic."""

    @pytest.mark.asyncio
    async def test_reset_no_pid_just_shuts_down(self, cfg):
        """reset() with no PID attribute just calls shutdown."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.reset("k1")
        provider.shutdown.assert_awaited_once()
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_reset_unlinks_temp_files_from_the_dropped_queue(self, cfg, tmp_path):
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts2", "second", force=True, image_temp_paths=[str(img)])

        # Only an ending reset drops the queue; a recycle parks it for a successor.
        await mgr.reset("k1", ends_conversation=True)

        assert not img.exists()

    @pytest.mark.asyncio
    async def test_reset_with_acp_pid_dead_after_shutdown(self, cfg):
        """reset() with ACP PID that dies after shutdown — no force kill."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        mock_client = AsyncMock()
        mock_client._pid = 12345
        mock_client._child_pids = {}
        provider._client = mock_client

        with patch("os.kill", side_effect=ProcessLookupError):
            await mgr.reset("k1")

        provider.shutdown.assert_awaited_once()
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_reset_with_pid_survives_shutdown_force_kills(self, cfg):
        """reset() force-kills when PID survives shutdown."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        mock_client = AsyncMock()
        mock_client._pid = 12345
        mock_client._child_pids = {}
        provider._client = mock_client

        # Await points allow unrelated liveness probes in this process. Keep
        # every mocked probe successful instead of consuming a finite list.
        with (
            patch("os.kill", return_value=None),
            patch("os.killpg") as mock_killpg,
            patch("os.getpgid", return_value=12345),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
        ):
            await mgr.reset("k1")
            mock_killpg.assert_called_once()
            assert mock_killpg.call_args.args[0] == 12345
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_reset_with_cc_provider_proc(self, cfg):
        """reset() picks up PID from ClaudeCode _proc attribute."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # No _client, but has _proc (CC provider style)
        provider._client = None
        mock_proc = AsyncMock()
        # flake-ok: patched os.kill makes POSIX read pid 99999 as gone so no kill runs; the Windows probe is unpatched; moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing fabricated pid moved verbatim; patched os.kill keeps the POSIX kill path unreached; tracked for a de-flake pass
        mock_proc.pid = 99999
        mock_proc.returncode = None
        provider._proc = mock_proc

        with (
            patch("os.kill", side_effect=ProcessLookupError),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False) as probe,
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
        ):
            await mgr.reset("k1")

        provider.shutdown.assert_awaited_once()
        assert not mgr.has_session("k1")
        assert 99999 in [c.args[0] for c in probe.call_args_list], "the _proc pid is probed"

    @pytest.mark.asyncio
    async def test_reset_child_sweep(self, cfg):
        """reset() sweeps escaped children after root is dead."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        mock_client = AsyncMock()
        mock_client._pid = 12345
        mock_client._child_pids = {111: 1000, 222: 2000}
        provider._client = mock_client

        with (
            patch("os.kill", side_effect=ProcessLookupError),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[333]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=3000),
            patch("kiro_crew.acp.client._read_basename", return_value=b"node"),
            patch("kiro_crew.acp.client._kill_escaped_children") as mock_sweep,
        ):
            await mgr.reset("k1")
            mock_sweep.assert_called_once()
            call_arg = mock_sweep.call_args[0][0]
            assert 111 in call_arg
            assert 222 in call_arg
            assert 333 in call_arg

    @pytest.mark.asyncio
    async def test_reset_nonexistent_session(self, cfg):
        """reset() on missing key is a no-op."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert await mgr.reset("nonexistent") is False
        assert not mgr.has_session("nonexistent")


class TestDestroy:
    """Tests for destroy() — permanent session removal."""

    @pytest.mark.asyncio
    async def test_destroy_shuts_down_and_deletes_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.destroy("k1")
        provider.shutdown.assert_awaited_once()
        # The reason is part of the call: a destroyed session takes any inbound
        # resume binding with it, and the map audits the removal under this name.
        mock_delete.assert_called_once_with("k1", reason="session_destroyed")
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_conditional_destroy_refusal_leaves_session_and_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        generation = mgr.session_generation("k1")
        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if("k1", generation, lambda: False)

        assert destroyed is False
        provider.shutdown.assert_not_awaited()
        mock_delete.assert_not_called()
        assert mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_conditional_destroy_guard_runs_after_lock_acquisition(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        generation = mgr.session_generation("k1")
        allowed = True
        await mgr._lock.acquire()
        try:
            destroy_task = asyncio.create_task(mgr.destroy_if("k1", generation, lambda: allowed))
            # flake-ok: one yield parks destroy_if on the held registry lock before the guard flips; nothing suspends before that lock so one step is exact; moved verbatim
            await asyncio.sleep(0)
            allowed = False
        finally:
            mgr._lock.release()

        destroyed = await await_until(destroy_task, what="the conditional destroy returning")

        assert destroyed is False
        provider.shutdown.assert_not_awaited()
        assert mgr.has_session("k1")
        await mgr.destroy("k1")

    @pytest.mark.asyncio
    async def test_conditional_destroy_refuses_a_successor_generation(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        original, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        original_generation = mgr.session_generation("k1")
        await mgr.destroy("k1")
        successor, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if("k1", original_generation, lambda: True)

        assert destroyed is False
        original.shutdown.assert_awaited_once()
        successor.shutdown.assert_not_awaited()
        mock_delete.assert_not_called()
        assert mgr.has_session("k1")
        await mgr.destroy("k1")

    @pytest.mark.asyncio
    async def test_conditional_destroy_expected_absence_refuses_new_alias(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        thread_ts = "1785370133.085469"
        canonical = f"slack:{thread_ts}"
        generation = mgr.session_generation(canonical)
        assert generation == 0
        successor, _, _ = await mgr.get_or_create(thread_ts)
        mgr.release(thread_ts)

        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if(canonical, generation, lambda: True)

        assert destroyed is False
        successor.shutdown.assert_not_awaited()
        mock_delete.assert_not_called()
        assert mgr.has_session(thread_ts)
        await mgr.destroy(thread_ts)

    @pytest.mark.asyncio
    async def test_conditional_destroy_refuses_absent_successor_absent_aba(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        thread_ts = "1785370133.085469"
        canonical = f"slack:{thread_ts}"
        stale_absence = mgr.session_generation(canonical)

        await mgr.get_or_create(thread_ts)
        mgr.release(thread_ts)
        await mgr.remove(thread_ts)
        assert not mgr.has_session(thread_ts)
        assert mgr.session_generation(canonical) > stale_absence

        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if(canonical, stale_absence, lambda: True)

        assert destroyed is False
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_provider_factory_advances_removed_generation(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        stale_generation = mgr.session_generation("k1")

        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.reload_provider_factory()
            destroyed = await mgr.destroy_if("k1", stale_generation, lambda: True)

        assert mgr.session_generation("k1") > stale_generation
        assert destroyed is False
        provider.shutdown.assert_awaited_once()
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_all_advances_removed_generation(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        stale_generation = mgr.session_generation("k1")

        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.close_all()
            destroyed = await mgr.destroy_if("k1", stale_generation, lambda: True)

        assert mgr.session_generation("k1") > stale_generation
        assert destroyed is False
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_conditional_destroy_can_preserve_autocompact_override(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr.set_autocompact_pct("k1", 55.0)
        generation = mgr.session_generation("k1")

        destroyed = await mgr.destroy_if(
            "k1",
            generation,
            lambda: True,
            preserve_autocompact_override=True,
        )

        assert destroyed is True
        folded = mgr._fold_key("k1")
        assert mgr._compaction.state.pct_overrides[folded] == 55.0

        # The public unconditional path retains its historical clear behavior.
        await mgr.destroy("k1")
        assert folded not in mgr._compaction.state.pct_overrides

    @pytest.mark.asyncio
    async def test_conditional_destroy_refuses_an_inflight_alias_allocation(self, cfg):
        start_entered = asyncio.Event()
        release_start = asyncio.Event()
        provider = _mock_provider_factory()(session_key="reserved")

        async def blocked_start():
            start_entered.set()
            # flake-ok: start double parks until the test sets release_start after the destroy_if check; an await_until bounds the released allocation; moved verbatim
            await release_start.wait()

        provider.start = AsyncMock(side_effect=blocked_start)
        mgr = SessionManager(cfg, provider_factory=lambda *_args, **_kwargs: provider)
        thread_ts = "1785370133.085469"
        canonical = f"slack:{thread_ts}"
        expected_absence = mgr.session_generation(canonical)
        assert expected_absence == 0

        allocation = asyncio.create_task(mgr.get_or_create(thread_ts))
        await await_until(start_entered, what="provider.start() entered")

        assert mgr.session_generation(canonical) > expected_absence
        assert thread_ts in mgr.session_keys()
        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if(canonical, expected_absence, lambda: True)

        assert destroyed is False
        mock_delete.assert_not_called()
        release_start.set()
        allocated_provider, _, _ = await await_until(allocation, what="the allocation landing")
        assert allocated_provider is provider
        mgr.release(thread_ts)
        await mgr.destroy(thread_ts)

    @pytest.mark.asyncio
    async def test_failed_allocation_releases_ownership_reservation(self, cfg):
        provider = _mock_provider_factory()(session_key="reserved")
        provider.start = AsyncMock(side_effect=RuntimeError("start failed"))
        mgr = SessionManager(cfg, provider_factory=lambda *_args, **_kwargs: provider)
        before = mgr.session_generation("failed-key")

        with pytest.raises(RuntimeError, match="start failed"):
            await mgr.get_or_create("failed-key")

        assert mgr.session_generation("failed-key") > before
        assert "failed-key" not in mgr.session_keys()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancelled_allocation_releases_ownership_reservation(self, cfg):
        start_entered = asyncio.Event()
        provider = _mock_provider_factory()(session_key="reserved")

        async def blocked_start():
            start_entered.set()
            # flake-ok: start double parks on an event nothing sets; the test cancels the allocation once start is entered so the wait ends by that cancel; moved verbatim
            await asyncio.Event().wait()

        provider.start = AsyncMock(side_effect=blocked_start)
        mgr = SessionManager(cfg, provider_factory=lambda *_args, **_kwargs: provider)
        before = mgr.session_generation("cancelled-key")
        allocation = asyncio.create_task(mgr.get_or_create("cancelled-key"))
        await await_until(start_entered, what="provider.start() entered")
        assert "cancelled-key" in mgr.session_keys()

        allocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            # flake-ok: awaits the allocation cancelled in its parked start; ends when the cancel re-raises with no timeout bound; moved verbatim; tracked for a de-flake pass
            await allocation

        assert mgr.session_generation("cancelled-key") > before
        assert "cancelled-key" not in mgr.session_keys()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_successful_reservation_finalizer_has_no_cancellable_await(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        session = mgr._sessions["k1"]
        impl_entered = asyncio.Event()
        release_impl = asyncio.Event()

        async def completed_impl(*_args, **_kwargs):
            await session.semaphore.acquire()
            impl_entered.set()
            # flake-ok: patched impl parks until the test sets release_impl on the next line after await_until sees it entered; moved verbatim
            await release_impl.wait()
            return provider, False, False

        with patch.object(mgr._allocation_boundary(), "_get_or_create_impl", completed_impl):
            claim = asyncio.create_task(mgr.get_or_create("k1"))
            await await_until(impl_entered, what="the impl entered")
            release_impl.set()
            # flake-ok: one yield lets the woken claim finish before the test resumes; FIFO ready order makes one step exact and that is the property pinned; moved verbatim
            await asyncio.sleep(0)

            assert claim.done()
            assert claim.cancel() is False
            claimed_provider, _, _ = claim.result()

        assert claimed_provider is provider
        assert session.semaphore.locked()
        assert mgr._allocation_boundary()._allocation_reservations == {}
        mgr.release("k1")
        await mgr.destroy("k1")

    @pytest.mark.asyncio
    async def test_conditional_destroy_refuses_a_busy_current_generation(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        generation = mgr.session_generation("k1")

        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if("k1", generation, lambda: True)

        assert destroyed is False
        provider.shutdown.assert_not_awaited()
        mock_delete.assert_not_called()
        assert mgr.has_session("k1")
        mgr.release("k1")
        await mgr.destroy("k1")

    @pytest.mark.asyncio
    async def test_destroy_deletes_map_before_provider_shutdown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        map_deleted = False

        def delete(*_args, **_kwargs):
            nonlocal map_deleted
            map_deleted = True

        async def shutdown():
            assert map_deleted is True

        provider.shutdown = AsyncMock(side_effect=shutdown)
        with patch.object(mgr._session_map, "delete", side_effect=delete):
            await mgr.destroy("k1")

        provider.shutdown.assert_awaited_once()
        assert map_deleted is True
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_destroy_deletes_map_before_end_metric_can_yield(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        metric_entered = asyncio.Event()
        release_metric = asyncio.Event()

        async def delayed_metric(*_args, **_kwargs):
            metric_entered.set()
            # flake-ok: end-metric double parks until the test sets release_metric after the delete check; await_until bounds the destroy it releases; moved verbatim
            await release_metric.wait()

        with (
            patch(
                "kiro_crew.session_lifecycle.record_session_ended",
                new=AsyncMock(side_effect=delayed_metric),
            ),
            patch.object(mgr._session_map, "delete") as mock_delete,
        ):
            destroy = asyncio.create_task(mgr.destroy("k1"))
            await await_until(metric_entered, what="the end metric entered")
            mock_delete.assert_called_once_with("k1", reason="session_destroyed")
            release_metric.set()
            await await_until(destroy, what="the destroy finishing")
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_destroy_unlinks_temp_files_from_the_session_queue(self, cfg, tmp_path):
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts2", "second", force=True, image_temp_paths=[str(img)])

        await mgr.destroy("k1")

        assert not img.exists()

    @pytest.mark.asyncio
    async def test_destroy_nonexistent_still_deletes_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.set("nonexistent", "stale-sid")
        with patch.object(mgr._session_map, "delete", wraps=mgr._session_map.delete) as mock_delete:
            await mgr.destroy("nonexistent")
        mock_delete.assert_called_once_with("nonexistent", reason="session_destroyed")
        assert mgr._session_map.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_unconditional_destroy_is_not_refused_by_a_reservation(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._allocation_boundary()._allocation_reservations["k1"] = {object()}

        mgr._session_map.set("k1", "reserved-sid")
        with patch.object(mgr._session_map, "delete", wraps=mgr._session_map.delete) as mock_delete:
            await mgr.destroy("k1")

        mock_delete.assert_called_once_with("k1", reason="session_destroyed")
        assert mgr._session_map.get("k1") is None

    @pytest.mark.asyncio
    async def test_destroy_shutdown_exception_still_deletes_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(mgr._session_map, "delete") as mock_delete:
            with pytest.raises(RuntimeError, match="boom"):
                await mgr.destroy("k1")
        # finally block still runs
        mock_delete.assert_called_once_with("k1", reason="session_destroyed")


class TestReplaySuppression:
    """``replay=False`` is what makes discarding a conversation actually stick.

    Clearing the sid stops the provider resuming its own conversation — and "the
    provider has no history" is exactly the condition that makes the next cold
    start rebuild one from ``conversation_log``. So the two mechanisms work
    against each other, and the caller who wanted a fresh conversation is handed
    a reconstruction of the old one. Measured on one app-owned session, that
    replay was 80,359 characters, 76% of the first turn's injected context.
    """

    @pytest.mark.asyncio
    async def test_default_does_not_suppress(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.discard_conversation("k1")
        assert (
            mgr.consume_replay_suppression("k1") is False
        ), "the default must leave every existing caller's behaviour alone"

    @pytest.mark.asyncio
    async def test_replay_false_suppresses_exactly_once(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.discard_conversation("k1", replay=False)

        assert mgr.consume_replay_suppression("k1") is True
        assert mgr.consume_replay_suppression("k1") is False, (
            "one-shot: a later cold start (idle expiry, gateway restart) must "
            "re-anchor rather than stay silently amnesiac"
        )

    @pytest.mark.asyncio
    async def test_a_later_replay_true_reset_clears_a_pending_suppression(self, cfg):
        """Two resets in a row must not leave the first one's intent standing."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.discard_conversation("k1", replay=False)
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.discard_conversation("k1")

        assert mgr.consume_replay_suppression("k1") is False

    @pytest.mark.asyncio
    async def test_teardown_does_not_leave_a_suppression_for_a_reused_key(self, cfg):
        """A slot key outlives the slot that held it, and keys ARE reused.

        A leaked flag would starve the NEXT holder of that key of its re-anchor —
        so the teardown paths clear it alongside the compaction cooldown they
        already clear, rather than leaving it to age out.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.discard_conversation("k1", replay=False)

        await mgr.remove("k1")

        assert mgr.consume_replay_suppression("k1") is False


class TestDiscardConversation:
    """Tests for discard_conversation() — the poisoned-conversation escape.

    Unlike destroy(), the session-map ENTRY must survive: it carries the
    Slack thread/channel linkage (and feeds the reverse thread→session sync
    index), so deleting it would silently unlink a mirrored session. Only
    the resume sid is cleared, forcing the next turn to cold-start a fresh
    native conversation."""

    @pytest.mark.asyncio
    async def test_discard_shuts_down_and_clears_only_sid(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        with (
            patch.object(mgr._session_map, "clear_sid") as mock_clear,
            patch.object(mgr._session_map, "delete") as mock_delete,
        ):
            await mgr.discard_conversation("k1")
        provider.shutdown.assert_awaited_once()
        mock_clear.assert_called_once_with("k1")
        mock_delete.assert_not_called()
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_skip_if_busy_refuses_while_a_turn_holds_the_semaphore(self, cfg):
        """The guard reads the SEMAPHORE, which is why it has to live here.

        ``get_or_create`` leaves the semaphore held until ``release``, and the
        provider reports ``has_active_turn() is False`` throughout — a turn that
        holds the semaphore without a prompt in flight yet. So a CALLER probing
        the provider and then calling this would see "idle", tear the session
        down, and take the provider away from a turn that had already been
        admitted. Refusing here, under the lock that pops the session, is what
        closes that window.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        # The blind spot, made explicit: the provider says idle while busy.
        assert provider.has_active_turn() is False

        discarded = await mgr.discard_conversation("k1", replay=False, skip_if_busy=True)

        assert discarded is False
        provider.shutdown.assert_not_awaited()
        assert mgr.has_session("k1"), "the refusal must leave the session intact"

    @pytest.mark.asyncio
    async def test_a_refusal_changes_nothing_at_all(self, cfg):
        """Not a partial teardown: the replay flag must not move either, or the
        caller's retry would find suppression already consumed."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")

        assert await mgr.discard_conversation("k1", replay=False, skip_if_busy=True) is False

        assert mgr.consume_replay_suppression("k1") is False

    @pytest.mark.asyncio
    async def test_skip_if_busy_proceeds_once_the_turn_releases(self, cfg):
        """The refusal is a wait, not a cancellation."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        assert await mgr.discard_conversation("k1", replay=False, skip_if_busy=True) is False

        mgr.release("k1")
        discarded = await mgr.discard_conversation("k1", replay=False, skip_if_busy=True)

        assert discarded is True
        provider.shutdown.assert_awaited_once()
        assert mgr.consume_replay_suppression("k1") is True

    @pytest.mark.asyncio
    async def test_the_default_still_tears_down_a_busy_session(self, cfg):
        """``skip_if_busy`` defaults False, so every pre-existing caller — the
        poisoned-conversation escalation among them — keeps its behaviour."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")

        discarded = await mgr.discard_conversation("k1")

        assert discarded is True
        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_successor_mapped_during_shutdown_keeps_its_sid(self, cfg):
        """The sid clear must not outlive the pop.

        Ordered deterministically rather than by timing: the successor is mapped
        from inside ``provider.shutdown``, which is precisely the await the
        teardown suspends on. That is the whole window the bug needs — pop, then
        a concurrent channel turn creates and maps a new session under the same
        key, then a clear deferred past the shutdown wipes the NEW session's
        pointer. Clearing in the same tick as the pop closes it.

        Observed on the RAW entry, not through ``SessionMap.get``: that getter
        additionally requires ``<sid>.json`` to exist on disk, so for a synthetic
        sid it answers None whether or not the clear ran — which would make this
        assertion pass with the bug present.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._session_map.set("k1", "original-sid")

        async def _map_a_successor_while_shutting_down():
            mgr._session_map.set("k1", "successor-sid")

        provider.shutdown = AsyncMock(side_effect=_map_a_successor_while_shutting_down)

        await mgr.discard_conversation("k1", replay=False)

        provider.shutdown.assert_awaited_once()
        assert _raw_sid(mgr, "k1") == "successor-sid", (
            "the successor session's sid was erased by a clear deferred past the " "shutdown await"
        )

    @pytest.mark.asyncio
    async def test_the_sid_is_still_cleared_with_no_successor(self, cfg):
        """Scope pin: the clear still happens — it just happens earlier."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._session_map.set("k1", "original-sid")

        await mgr.discard_conversation("k1", replay=False)

        assert _raw_sid(mgr, "k1") == ""

    @pytest.mark.asyncio
    async def test_a_refusal_does_not_clear_the_sid(self, cfg):
        """``skip_if_busy`` refusing must leave the mapping alone too — the clear
        sits after the early return, not before it."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr._session_map.set("k1", "original-sid")

        assert await mgr.discard_conversation("k1", replay=False, skip_if_busy=True) is False

        assert _raw_sid(mgr, "k1") == "original-sid"

    @pytest.mark.asyncio
    async def test_discard_conversation_unlinks_temp_files_from_the_session_queue(
        self, cfg, tmp_path
    ):
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts2", "second", force=True, image_temp_paths=[str(img)])

        await mgr.discard_conversation("k1")

        assert not img.exists()

    @pytest.mark.asyncio
    async def test_discard_preserves_slack_linkage(self, cfg):
        """The poisoned-conversation discard keeps Slack linkage: a Slack-linked
        session that discards its rejected conversation must keep its thread
        binding, or the recovered answer is not mirrored and later inbound
        replies fork a new conversation."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._session_map.set("k1", "sid-poisoned")
        mgr._session_map.set_slack_link("k1", "1234.5678", "C0FFEE")
        await mgr.discard_conversation("k1")
        # sid gone → next turn cold-starts instead of session/load-ing the poison
        assert not mgr._session_map.get("k1")
        # ...but the Slack linkage survives.
        assert mgr.get_slack_link("k1") == ("1234.5678", "C0FFEE")
        # ...and the dropped sid is stashed, so a false-positive discard is
        # diagnosable and manually reversible (the native conversation still
        # exists on disk; only the pointer was cleared).
        assert mgr._session_map.get_discarded_sid("k1") == "sid-poisoned"

    @pytest.mark.asyncio
    async def test_discard_shutdown_exception_still_clears_sid(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(mgr._session_map, "clear_sid") as mock_clear:
            with pytest.raises(RuntimeError, match="boom"):
                await mgr.discard_conversation("k1")
        # finally block still runs
        mock_clear.assert_called_once_with("k1")


class TestCooldownPruning:
    """`_compact_cooldown_until` entries must be cleared on session lifecycle
    events so a fresh session reusing a key never inherits a stale cooldown."""

    @pytest.mark.asyncio
    async def test_remove_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0
        mgr._compact_pending_verdict["k1"] = 92.0

        await mgr.remove("k1")

        assert "k1" not in mgr._compact_cooldown_until
        assert "k1" not in mgr._compact_pending_verdict
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_destroy_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0
        mgr._compact_pending_verdict["k1"] = 92.0

        await mgr.destroy("k1")

        assert "k1" not in mgr._compact_cooldown_until
        assert "k1" not in mgr._compact_pending_verdict
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reset_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0
        mgr._compact_pending_verdict["k1"] = 92.0

        await mgr.reset("k1")

        assert "k1" not in mgr._compact_cooldown_until
        assert "k1" not in mgr._compact_pending_verdict
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_close_all_clears_cooldowns(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0
        mgr._compact_cooldown_until["k2"] = time.monotonic() + 60.0
        mgr._compact_pending_verdict["k1"] = 92.0

        await mgr.close_all()

        assert mgr._compact_cooldown_until == {}
        assert mgr._compact_pending_verdict == {}


class TestCloseAllPersistence:
    """Tests for close_all session_map persistence."""

    @pytest.mark.asyncio
    async def test_close_all_persists_acp_session_ids(self, cfg):
        from unittest.mock import MagicMock

        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.session import _Session

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_provider = MagicMock(spec=AcpProvider)
        mock_provider.shutdown = AsyncMock()
        mock_provider.context_usage_pct = MagicMock(return_value=0.0)
        # session_map persistence reads the public provider.cwd accessor
        # (AcpProvider exposes the work dir via _client._work_dir, not a bare
        # _work_dir attribute).
        mock_provider.cwd = "/tmp/test"
        mock_provider.client = MagicMock()
        mock_provider.client._session_id = "sid-persist-test"
        mock_provider.client.backend = ""  # kiro-cli backend

        mgr._sessions["dashboard:slot0"] = _Session(provider=mock_provider)
        await mgr.close_all()
        # provider= is persisted so the next-startup detect_provider_switch
        # does not see a missing label and falsely fire an acp/cc switch.
        stored = mgr._session_map
        assert stored.mapped_sid("dashboard:slot0") == "sid-persist-test"
        assert stored.get_provider("dashboard:slot0") == "acp"
        assert stored.get_cwd("dashboard:slot0") == "/tmp/test"


class TestRemove:
    """Tests for remove() — shutdown but preserve session_map."""

    @pytest.mark.asyncio
    async def test_remove_shuts_down_preserves_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.remove("k1")
        provider.shutdown.assert_awaited_once()
        mock_delete.assert_not_called()  # remove preserves map
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_remove_unlinks_temp_files_from_the_session_queue(self, cfg, tmp_path):
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts2", "second", force=True, image_temp_paths=[str(img)])

        await mgr.remove("k1")

        assert not img.exists()


class TestSafeCleanup:
    """Tests for _safe_cleanup best-effort session file removal."""

    @pytest.mark.asyncio
    async def test_cleanup_calls_provider(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.cleanup_session = AsyncMock()
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.session"):
            await mgr._safe_cleanup(mock_p, "sid-123")
        mock_p.cleanup_session.assert_awaited_once_with("sid-123")
        assert "Cleaned up session files for sid-123" in caplog.messages

    @pytest.mark.asyncio
    async def test_cleanup_swallows_exception(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.cleanup_session = AsyncMock(side_effect=OSError("disk full"))
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            await mgr._safe_cleanup(mock_p, "sid-456")
        assert "Failed to clean up session files for sid-456" in caplog.messages


class TestCloseSession:
    @pytest.mark.asyncio
    async def test_close_nonexistent_is_noop(self, cfg):
        """An unmapped, unheld key: nothing to delete, and nothing is created."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.destroy("nonexistent")
        assert mgr._session_map.get("nonexistent") is None
        assert not mgr.has_session("nonexistent")

    @pytest.mark.asyncio
    async def test_close_calls_shutdown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p, _, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        await mgr.destroy("thread1")
        p.shutdown.assert_awaited_once()
        assert not mgr.has_session("thread1")


class TestCloseAll:
    @pytest.mark.asyncio
    async def test_close_all_shuts_down_all(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p1, _, _ = await mgr.get_or_create("a")
        p2, _, _ = await mgr.get_or_create("b")
        mgr.release("a")
        mgr.release("b")
        await mgr.close_all()
        p1.shutdown.assert_awaited_once()
        p2.shutdown.assert_awaited_once()
        assert mgr.count == 0
