"""Unit coverage for :mod:`kiro_crew.apps.backend`: ``stop_app_backend``.

SIGTERM-then-SIGKILL escalation for a spawned child, the identity revalidation every
recorded PID of an adopted backend passes before it is signalled (and the SIGKILL of
an adopted survivor), and the bounded wait for those PIDs to exit.

One topic of the ``test_apps_backend_coverage*.py`` family; the doubles and fixtures
the family shares come from ``apps_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import _fake_proc

import kiro_crew.apps.backend as bmod
from kiro_crew.apps.backend import AppProcess

# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


class TestStopSpawnedBackend:
    @pytest.fixture()
    def kills(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
        recorded: list[tuple[int, int]] = []
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda pid, sig: recorded.append((pid, sig)),
        )
        return recorded

    def test_a_live_child_gets_sigterm(self, kills: list[tuple[int, int]]) -> None:
        proc = _fake_proc(pid=555)
        with bmod._lock:
            bmod._processes["spawned"] = AppProcess(
                app_name="spawned", port=9100, pid=555, proc=proc
            )
        assert bmod.stop_app_backend("spawned") is True
        assert kills == [(555, bmod.platform_compat.SIGTERM)]
        assert "spawned" not in bmod._processes

    def test_a_child_that_ignores_sigterm_is_escalated_to_sigkill(
        self, kills: list[tuple[int, int]]
    ) -> None:
        proc = _fake_proc(pid=556)
        proc.wait_raises = True
        with bmod._lock:
            bmod._processes["stubborn"] = AppProcess(
                app_name="stubborn", port=9100, pid=556, proc=proc
            )
        assert bmod.stop_app_backend("stubborn") is True
        assert kills == [
            (556, bmod.platform_compat.SIGTERM),
            (556, bmod.platform_compat.SIGKILL),
        ]

    def test_a_vanished_child_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _gone(_pid: int, _sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree", _gone)
        with bmod._lock:
            bmod._processes["gone"] = AppProcess(
                app_name="gone", port=9100, pid=557, proc=_fake_proc(pid=557)
            )
        assert bmod.stop_app_backend("gone") is True

    def test_a_failing_log_handle_close_is_swallowed(self, kills: list[tuple[int, int]]) -> None:
        handle = MagicMock()
        handle.close.side_effect = OSError("already closed")
        with bmod._lock:
            bmod._processes["loggy"] = AppProcess(
                app_name="loggy", port=9100, pid=558, proc=_fake_proc(pid=558), log_fh=handle
            )
        assert bmod.stop_app_backend("loggy") is True
        handle.close.assert_called_once()


class TestStopAdoptedBackend:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the class's instant PID wait, moved verbatim; tracked for a de-flake pass
    def _fast_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "_wait_for_pids", lambda _pids, timeout=2.0: None)

    @pytest.fixture()
    def kills(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
        recorded: list[tuple[int, int]] = []

        def _pinned_kill(pid: int, _start_time: str, sig: int) -> bool:
            recorded.append((pid, sig))
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _pinned_kill)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        return recorded

    def _track(self, **kwargs: Any) -> AppProcess:
        ap = AppProcess(app_name="ext", port=bmod._MIN_PORT + 12, pid=0, proc=None, **kwargs)
        with bmod._lock:
            bmod._processes["ext"] = ap
            bmod._allocated_ports["ext"] = ap.port
        return ap

    def test_an_adopted_backend_with_no_recorded_pids_is_left_alone(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Killing unknown PIDs on a port could take out an unrelated service."""

        ap = self._track(healthy=True)
        with caplog.at_level(logging.WARNING):
            assert bmod.stop_app_backend("ext") is False
        assert any("refusing to kill unknown processes" in r.message for r in caplog.records)
        # Tracking is restored so a retry after re-adoption is possible.
        assert bmod._processes["ext"] is ap
        assert bmod._allocated_ports["ext"] == ap.port

    def test_only_identity_verified_pids_are_signalled(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Revalidation is process identity, not a port heuristic: a PID whose
        live start time does not match the adoption record was recycled and
        must not be signalled, whatever it listens on now."""

        self._track(
            adopted_pids=[111, 222],
            adopted_start_times={111: "st-111", 222: "st-222"},
            healthy=True,
        )
        monkeypatch.setattr(
            bmod, "_proc_start_time", lambda pid: {111: "st-111", 222: "st-999"}.get(pid)
        )
        # 222 stays alive (it is the recycled process we must not touch);
        # 111 dies on SIGTERM so no SIGKILL escalation muddies the record.
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda pid: pid == 222)
        assert bmod.stop_app_backend("ext") is True
        assert kills == [(111, bmod.platform_compat.SIGTERM)]

    def test_a_recycled_pid_is_not_signalled_even_when_it_listens(
        self,
        kills: list[tuple[int, int]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The exact residual the identity guard closes: the adopted backend
        exits, the OS recycles its PID onto ANOTHER listener of the same port
        (any local address, including a v6-only wildcard) — the start-time
        mismatch keeps it from being signalled."""

        self._track(adopted_pids=[111], adopted_start_times={111: "st-old"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-recycled")
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        with caplog.at_level(logging.WARNING):
            assert bmod.stop_app_backend("ext") is True
        assert kills == []
        assert any("identity does not match" in r.message for r in caplog.records)

    def test_an_exited_backend_signals_nothing(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead PID reads back no start time: nothing to signal, no warning
        spam for a process that simply finished."""

        self._track(adopted_pids=[111], adopted_start_times={111: "st-111"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        assert bmod.stop_app_backend("ext") is True
        assert kills == []

    def test_a_pid_recycled_during_the_graceful_wait_is_not_sigkilled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escalation re-reads identity: a PID whose start time changed
        during the 2s graceful wait was recycled and must not receive the
        destructive SIGKILL, on any platform."""

        recorded: list[tuple[int, int]] = []

        def _pinned_kill(pid: int, _start_time: str, sig: int) -> bool:
            recorded.append((pid, sig))
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _pinned_kill)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        self._track(adopted_pids=[111], adopted_start_times={111: "st"}, healthy=True)
        # Identity matches for the SIGTERM selection, then flips before the
        # escalation re-read (the PID was recycled during the graceful wait).
        reads = iter(["st", "st-recycled"])
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: next(reads))
        assert bmod.stop_app_backend("ext") is True
        assert recorded == [(111, bmod.platform_compat.SIGTERM)]

    def test_a_pin_refusal_signals_nothing_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """kill_pid_pinned returning False means the process exited between the
        identity check and the pin — there is nothing left to stop, and no
        signal may be sent to whatever holds the PID now."""

        self._track(adopted_pids=[111], adopted_start_times={111: "st"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st")
        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", lambda _pid, _st, _sig: False)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        with caplog.at_level(logging.INFO):
            assert bmod.stop_app_backend("ext") is True
        assert any("pinned SIGTERM" in r.message for r in caplog.records)

    def test_an_unreadable_identity_is_never_signalled(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No token was recorded at adoption (identity unreadable): the PID
        cannot be positively named, so it is skipped - fail toward not
        killing, per the process_start_time contract."""

        self._track(adopted_pids=[111], adopted_start_times={}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-live")
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        assert bmod.stop_app_backend("ext") is True
        assert kills == []

    def test_nonpositive_recorded_pids_are_never_signalled(
        self, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._track(
            adopted_pids=[0, -1],
            adopted_start_times={0: "st-0", -1: "st-1"},
            healthy=True,
        )
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: {0: "st-0", -1: "st-1"}.get(pid))
        assert bmod.stop_app_backend("ext") is True
        assert kills == []

    def test_a_survivor_is_escalated_to_sigkill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple[int, int]] = []

        def _pinned_kill(pid: int, _start_time: str, sig: int) -> bool:
            recorded.append((pid, sig))
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _pinned_kill)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        self._track(adopted_pids=[111], adopted_start_times={111: "st"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st")
        assert bmod.stop_app_backend("ext") is True
        assert recorded == [
            (111, bmod.platform_compat.SIGTERM),
            (111, bmod.platform_compat.SIGKILL),
        ]

    def test_an_unsignalable_pid_is_skipped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _denied(_pid: int, _start_time: str, _sig: int) -> bool:
            raise ProcessLookupError

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _denied)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        self._track(adopted_pids=[111], adopted_start_times={111: "st"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st")
        assert bmod.stop_app_backend("ext") is True

    def test_an_unexpected_stop_failure_restores_tracking(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Losing the record would orphan the backend with no way to retry."""

        def _bad(_pid: int, _start_time: str, _sig: int) -> bool:
            raise ValueError("bad signal")

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _bad)
        ap = self._track(adopted_pids=[111], adopted_start_times={111: "st"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st")
        with caplog.at_level(logging.WARNING):
            assert bmod.stop_app_backend("ext") is False
        assert any("Failed to stop adopted backend" in r.message for r in caplog.records)
        assert bmod._processes["ext"] is ap
        assert bmod._allocated_ports["ext"] == ap.port


class TestWaitForPids:
    def test_polling_stops_as_soon_as_every_pid_is_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        states = [bmod.platform_compat.PID_ALIVE, bmod.platform_compat.PID_DEAD]

        def _liveness(_pid: int) -> str:
            return states.pop(0) if states else bmod.platform_compat.PID_DEAD

        monkeypatch.setattr(bmod.platform_compat, "pid_liveness", _liveness)
        # flake-ok: a pre-existing in-test stand-in for the backend module's time.sleep, undone by monkeypatch at teardown; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- a pre-existing in-test stand-in for the backend module's time.sleep moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(bmod.time, "sleep", lambda _s: None)
        bmod._wait_for_pids([111], timeout=5.0)
        assert states == []

    def test_an_unsignalable_pid_counts_as_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EPERM means 'not ours' — waiting the full deadline on it is pure latency."""

        monkeypatch.setattr(
            bmod.platform_compat,
            "pid_liveness",
            lambda _pid: bmod.platform_compat.PID_UNSIGNALABLE,
        )
        # flake-ok: a pre-existing in-test stand-in for the backend module's time.sleep, undone by monkeypatch at teardown; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- a pre-existing in-test stand-in for the backend module's time.sleep moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(
            bmod.time, "sleep", lambda _s: pytest.fail("slept on an unsignalable pid")
        )
        bmod._wait_for_pids([111], timeout=5.0)

    def test_an_already_elapsed_deadline_polls_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            bmod.platform_compat, "pid_liveness", lambda _pid: pytest.fail("polled")
        )
        bmod._wait_for_pids([111], timeout=0.0)
