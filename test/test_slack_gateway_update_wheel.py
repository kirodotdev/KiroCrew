"""The unattended managed-venv (wheel) apply.

Covers the preflight refusals in the order they run (the release grammar, the
source pin on the feed base and then the artifact base, the CDN base, the sandbox
profile) and the apply through the shadow engine beside the live venv: admission,
restart, retries, cancellation, a held lock, an incompatible release, a failed
apply and the shutdown wait.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _make_orchestrator,
    _mock_dashboard_state,
)
from wheel_update_test_helpers import ARTIFACT_BASE, FEED_BASE, wire_wheel_apply

from kiro_crew.slack import gateway as gw


class TestWheelAutoApplyRefusals:
    """The unattended managed-venv apply runs the one preflight, in its one order.

    The policy source pin first (feed base, then artifact base), then the shape
    of the operator-set KIROCREW_CDN_BASE. Either refusal stops the apply before
    anything is fetched or built, and so does a promotion that would detach the
    sandbox's AppArmor profile.
    """

    @staticmethod
    def _wire(monkeypatch, *, safe: bool = True, blocked_base: str = "", reattach: bool = False):
        seen: list[str] = []

        def _safe() -> bool:
            seen.append("shape")
            return safe

        def _blocked(base: str) -> str | None:
            seen.append(f"pin:{base}")
            return "pinned elsewhere" if base == blocked_base else None

        engine = wire_wheel_apply(monkeypatch, safe=_safe, blocked=_blocked, reattach=reattach)
        return seen, engine

    @pytest.mark.asyncio
    async def test_the_source_pin_refuses_first(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        seen, engine = self._wire(monkeypatch, blocked_base=FEED_BASE)

        await orch._auto_apply_wheel_update("stable", "9.9.9")

        assert seen == [f"pin:{FEED_BASE}"]
        engine.assert_not_called()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_a_version_outside_the_release_grammar_refuses_before_the_pin(self, monkeypatch):
        """The feed check admits a wider grammar than a tree name; nothing that
        names one (the AppArmor probe, the engine) runs for such a version."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        seen, engine = self._wire(monkeypatch, reattach=True)

        await orch._auto_apply_wheel_update("stable", "1.0-rc1")

        assert seen == []
        engine.assert_not_called()
        ds.notify.assert_not_called()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_the_artifact_base_is_pinned_too(self, monkeypatch):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        seen, engine = self._wire(monkeypatch, blocked_base=ARTIFACT_BASE)

        await orch._auto_apply_wheel_update("stable", "9.9.9")

        assert seen == [f"pin:{FEED_BASE}", f"pin:{ARTIFACT_BASE}"]
        engine.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unsafe_cdn_base_refuses_after_the_pin(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        seen, engine = self._wire(monkeypatch, safe=False)

        await orch._auto_apply_wheel_update("stable", "9.9.9")

        assert seen == [f"pin:{FEED_BASE}", f"pin:{ARTIFACT_BASE}", "shape"]
        engine.assert_not_called()
        ds.push_refresh.assert_called_with("update_available")

    @pytest.mark.asyncio
    async def test_a_promotion_that_would_detach_the_sandbox_profile_waits_for_the_operator(
        self, monkeypatch
    ):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        _seen, engine = self._wire(monkeypatch, reattach=True)

        await orch._auto_apply_wheel_update("stable", "9.9.9")
        await orch._auto_apply_wheel_update("stable", "9.9.9")

        engine.assert_not_called()
        ds.notify.assert_called_once()
        assert "kirocrew service install" in ds.notify.call_args.args[2]
        ds.push_update_progress.assert_not_called()


class TestWheelAutoApplyUsesTheShadowEngine:
    """The unattended apply builds beside the live venv; it never re-runs cli.sh.

    cli.sh moves the LIVE venv aside and rebuilds it in place, which a stop in
    that window strands. The shadow engine the CLI and the approve route use
    builds a sibling tree and flips the stable link, so the served tree is never
    touched, and admission stays open until the restart into the new tree.
    """

    @pytest.mark.asyncio
    async def test_applies_through_the_engine_with_admission_open_then_restarts(self, monkeypatch):
        from kiro_crew.platform.wheel_engine import ApplyCancel, respawn_executable

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        sessions = MagicMock()
        sessions.pause_turn_admission_for_update = AsyncMock(return_value=True)
        orch.sessions = sessions
        paused_during_build: list[bool] = []

        def engine(**kwargs):
            paused_during_build.append(sessions.pause_turn_admission_for_update.await_count > 0)
            return Path("/x/crew-venv-9.9.9")

        engine_mock = MagicMock(side_effect=engine)
        wire_wheel_apply(monkeypatch, apply=engine_mock)
        spawn = AsyncMock()
        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
        restart = AsyncMock()
        monkeypatch.setattr(orch, "_restart_after_update", restart)
        monkeypatch.setattr(orch, "_finish_auto_update_apply", AsyncMock())
        monkeypatch.setattr(orch, "_in_flight_work_counts", lambda: (0, 0))

        await orch._auto_apply_wheel_update("stable", "9.9.9")

        spawn.assert_not_awaited()
        kwargs = engine_mock.call_args.kwargs
        assert (kwargs["channel"], kwargs["expected_version"]) == ("stable", "9.9.9")
        assert (kwargs["feed_base"], kwargs["artifact_base"]) == (FEED_BASE, ARTIFACT_BASE)
        assert isinstance(kwargs["cancel"], ApplyCancel)
        assert paused_during_build == [False], "turns must keep running while the tree builds"
        sessions.pause_turn_admission_for_update.assert_awaited_once()
        restart.assert_awaited_once_with(respawn_executable)

    @pytest.mark.asyncio
    async def test_a_promotion_the_restart_cannot_reach_never_restarts(self, monkeypatch):
        """A restart that would exec the running version again (the stable link
        leads elsewhere) is never scheduled: its successor would find the same
        update, promote and restart again, for ever, closing every session."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        wire_wheel_apply(monkeypatch, reaches=False)
        restart = AsyncMock()
        monkeypatch.setattr(orch, "_restart_after_update", restart)

        await orch._auto_apply_wheel_update("stable", "9.9.9")
        await orch._auto_apply_wheel_update("stable", "9.9.9")

        restart.assert_not_awaited()
        assert orch._pending_update_respawn is None, "no restart is left pending either"
        ds.notify.assert_called_once()
        assert "Re-run the installer" in ds.notify.call_args.args[2]

    @pytest.mark.asyncio
    async def test_a_mandatory_apply_keeps_its_grace_on_every_restart_retry(self, monkeypatch):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        wire_wheel_apply(monkeypatch)
        prepare = AsyncMock(return_value=False)  # busy: the restart is retried later
        monkeypatch.setattr(orch, "_prepare_auto_update_apply", prepare)

        await orch._auto_apply_wheel_update("stable", "9.9.9", mandatory=True, mandatory_key="k")
        await orch._retry_pending_update_restart()  # the coordinator loop's bare retry

        assert prepare.await_args_list == [
            call(mandatory=True, mandatory_key="k"),
            call(mandatory=True, mandatory_key="k"),
        ]

    @pytest.mark.asyncio
    async def test_the_mandatory_grace_warning_fires_across_real_retries(self, monkeypatch, caplog):
        """Two retries of a deferred mandatory restart, 601 s apart: the grace
        warning fires, because the deferral clock is not restarted by a retry."""
        orch = _make_orchestrator()
        sessions = MagicMock()
        sessions.pause_turn_admission_for_update = AsyncMock(return_value=True)
        sessions.resume_turn_admission_after_update = AsyncMock()
        orch.sessions = sessions
        orch._schedule_inbound_replay = MagicMock()
        monkeypatch.setattr(orch, "_in_flight_work_counts", lambda: (1, 0))
        orch._pending_update_respawn = lambda: "/x/python3"
        orch._pending_update_mandatory = True
        orch._pending_update_mandatory_key = "floor:9.9.9"

        with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.gateway"):
            await orch._retry_pending_update_restart()
            # Age the recorded deferral rather than freezing ``loop.time``: the
            # loop clock is the one every timer on this loop waits on, so a
            # frozen one strands any bounded await the retry path makes.
            assert orch._mandatory_update_deferred_at is not None
            orch._mandatory_update_deferred_at -= orch._MANDATORY_UPDATE_MAX_DEFER_SECS + 1
            await orch._retry_pending_update_restart()

        assert "remains deferred after its grace period" in caplog.text

    @pytest.mark.asyncio
    async def test_a_held_lock_defers_quietly(self, monkeypatch):
        from kiro_crew.platform.wheel_engine import WheelUpdateBusy

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        wire_wheel_apply(
            monkeypatch, apply=MagicMock(side_effect=WheelUpdateBusy("already in progress"))
        )
        restart = AsyncMock()
        monkeypatch.setattr(orch, "_restart_after_update", restart)

        await orch._auto_apply_wheel_update("stable", "9.9.9")

        assert orch._update_apply_deferred is True, "retried on the short cadence"
        ds.push_update_progress.assert_not_called()
        restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_cancelled_apply_is_retried_on_the_short_cadence(self, monkeypatch):
        from kiro_crew.platform.wheel_engine import WheelUpdateCancelled

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        wire_wheel_apply(
            monkeypatch,
            apply=MagicMock(side_effect=WheelUpdateCancelled("stopped", reason="exec")),
        )

        await orch._auto_apply_wheel_update("stable", "9.9.9")

        assert orch._update_apply_deferred is True
        ds.push_update_progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_incompatible_release_is_noticed_once_and_decided_again_each_cycle(
        self, monkeypatch
    ):
        from kiro_crew.platform.wheel_engine import WheelUpdateIncompatible

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        engine = wire_wheel_apply(
            monkeypatch,
            apply=MagicMock(
                side_effect=WheelUpdateIncompatible(
                    "kirocrew 9.9.9 requires Python >= 3.99", version="9.9.9", sha256="a" * 64
                )
            ),
        )

        await orch._auto_apply_wheel_update("stable", "9.9.9")
        await orch._auto_apply_wheel_update("stable", "9.9.9")

        ds.notify.assert_called_once()
        body = ds.notify.call_args.args[2]
        assert "requires Python >= 3.99" in body
        assert "Re-run the installer" in body and "cli.sh" in body, body
        assert engine.call_count == 2, "the signed metadata is checked again every cycle"
        ds.push_update_progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_apply_reports_redacted_and_does_not_restart(self, monkeypatch):
        from kiro_crew.platform.wheel_engine import WheelUpdateError

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        secret = "https://user:hunter2@bytes.example/cli"
        wire_wheel_apply(
            monkeypatch, apply=MagicMock(side_effect=WheelUpdateError(f"could not fetch {secret}"))
        )
        restart = AsyncMock()
        monkeypatch.setattr(orch, "_restart_after_update", restart)

        await orch._auto_apply_wheel_update("stable", "9.9.9")

        restart.assert_not_awaited()
        step, detail = ds.push_update_progress.call_args.args
        assert step == "failed"
        assert "hunter2" not in detail
        assert "kirocrew update" in detail

    @pytest.mark.asyncio
    async def test_a_mandatory_floor_that_waits_for_the_operator_retries_soon(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        engine = wire_wheel_apply(monkeypatch, reattach=True)

        await orch._auto_apply_wheel_update("stable", "9.9.9", mandatory=True, mandatory_key="k")

        engine.assert_not_called()
        assert orch._update_apply_deferred is True, "a floor is never left for 12 h"
        ds.notify.assert_called_once()

    def test_a_stop_cancels_the_apply_in_flight_synchronously(self, monkeypatch):
        """The signal handler's path: the cancel is set synchronously, nothing is awaited."""
        from kiro_crew import platform_compat
        from kiro_crew.platform import wheel_apply
        from kiro_crew.platform.wheel_engine import ApplyCancel

        cancel = ApplyCancel()
        running = wheel_apply._Running(cancel, MagicMock())
        monkeypatch.setattr(wheel_apply, "_IN_FLIGHT", {running})

        platform_compat.cancel_wheel_applies_in_flight("shutdown")

        assert cancel.is_set() and cancel.reason == "shutdown"

    @pytest.mark.asyncio
    async def test_a_pending_restart_waits_for_an_apply_in_flight_without_cancelling_it(
        self, monkeypatch
    ):
        """A deferred retry at an idle tick must not kill an approved apply of a newer
        release and exec the older promotion over it; it defers until that apply ends."""
        from kiro_crew.platform import wheel_apply
        from kiro_crew.platform.wheel_engine import ApplyCancel

        orch = _make_orchestrator()
        sessions = MagicMock()
        sessions.pause_turn_admission_for_update = AsyncMock(return_value=True)
        sessions.resume_turn_admission_after_update = AsyncMock()
        orch.sessions = sessions
        orch._schedule_inbound_replay = MagicMock()
        cancel = ApplyCancel()
        monkeypatch.setattr(wheel_apply, "_IN_FLIGHT", {wheel_apply._Running(cancel, MagicMock())})
        orch._pending_update_respawn = lambda: "/x/python3"
        restart = AsyncMock()
        monkeypatch.setattr(orch, "_restart_after_update", restart)

        await orch._retry_pending_update_restart()

        restart.assert_not_awaited()
        assert orch._update_apply_deferred is True
        assert not cancel.is_set(), "the approved apply keeps building"

    @pytest.mark.asyncio
    async def test_the_shutdown_wait_never_imports_the_apply_module(self, monkeypatch):
        """With the install tree gone the module cannot be imported; the shutdown
        must not try, and still waits for the update coordinator."""

        class _RefuseApplyModule:
            def find_spec(self, name, path=None, target=None):
                if name == "kiro_crew.platform.wheel_apply":
                    raise ImportError("the install tree is gone")
                return None

        # flake-ok: pre-existing sys.modules eviction that monkeypatch restores, moved verbatim; tracked for a de-flake pass
        monkeypatch.delitem(sys.modules, "kiro_crew.platform.wheel_apply", raising=False)
        monkeypatch.setattr(sys, "meta_path", [_RefuseApplyModule(), *sys.meta_path])
        orch = _make_orchestrator()
        # flake-ok: pre-existing parked stand-in task the test cancels, moved verbatim; tracked for a de-flake pass
        task = asyncio.create_task(asyncio.sleep(10))
        task.cancel()

        await orch._settle_update_work(task)

        assert task.done()

    @pytest.mark.asyncio
    async def test_an_update_restart_waits_while_another_restart_owns_the_exec(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds._gateway_restart_in_progress = True
        orch.dashboard_state = ds
        monkeypatch.setattr(gw, "resolve_restart_launcher", lambda: None)
        monkeypatch.setattr("kiro_crew.platform_compat.execv_target_available", lambda _path: True)
        claimed = AsyncMock()
        monkeypatch.setattr(orch, "_restart_after_update_claimed", claimed)

        await orch._restart_after_update(lambda: "/x/python3")

        claimed.assert_not_awaited()
        assert orch._update_apply_deferred is True
        assert ds._gateway_restart_in_progress is True, "the other restart's claim is kept"
