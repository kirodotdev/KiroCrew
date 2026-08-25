"""Tests: crash_guard crash-log path pinning.

asyncio reports unretrieved task exceptions at GC time, which can be long after
the owning process (or test) tore its environment down. If the crash-log path
were resolved lazily at write time, such a record could land in whichever
``KIROCREW_HOME`` happened to be in effect then — including a developer's live
data home while the suite runs. ``install_loop_handler`` therefore pins the path
to the config dir that was active when the handler was installed.
"""

from __future__ import annotations

import asyncio
import atexit
import faulthandler
import sys

import pytest

from kiro_crew import crash_guard


@pytest.fixture(autouse=True)
def _restore_crash_log():
    """Keep the module-level pinned path from leaking between tests."""
    saved = crash_guard._CRASH_LOG
    yield
    crash_guard._CRASH_LOG = saved


class TestInstallLoopHandler:
    def test_pins_crash_log_to_current_config_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home_a"))
        crash_guard._CRASH_LOG = None
        loop = asyncio.new_event_loop()
        try:
            crash_guard.install_loop_handler(loop)
            assert loop.get_exception_handler() is crash_guard._asyncio_exception_handler
        finally:
            loop.close()

        assert crash_guard._CRASH_LOG == tmp_path / "home_a" / "logs" / "crash.log"

    def test_write_uses_pinned_path_after_home_changes(self, tmp_path, monkeypatch):
        """A late (GC-time) write must not follow a since-changed home."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home_a"))
        crash_guard._CRASH_LOG = None
        loop = asyncio.new_event_loop()
        try:
            crash_guard.install_loop_handler(loop)
        finally:
            loop.close()

        # Simulate the environment being restored (monkeypatch teardown, home
        # switch) before the deferred write happens.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home_b"))
        crash_guard._write_crash("ASYNCIO UNHANDLED: late report")

        pinned = tmp_path / "home_a" / "logs" / "crash.log"
        assert "late report" in pinned.read_text()
        assert not (tmp_path / "home_b" / "logs" / "crash.log").exists()

    def test_path_resolution_failure_still_installs_handler(self, monkeypatch):
        """Path resolution is best-effort — it must never block the handler."""
        monkeypatch.setattr(
            crash_guard, "_crash_log_path", lambda: (_ for _ in ()).throw(OSError("nope"))
        )
        crash_guard._CRASH_LOG = None
        loop = asyncio.new_event_loop()
        try:
            crash_guard.install_loop_handler(loop)
            assert loop.get_exception_handler() is crash_guard._asyncio_exception_handler
        finally:
            loop.close()
        assert crash_guard._CRASH_LOG is None


class TestUnclosedConnectionDowngrade:
    """Unclosed-connection GC noise is downgraded to WARNING, not ERROR."""

    def test_unclosed_connection_logged_at_warning(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        crash_guard._CRASH_LOG = None
        loop = asyncio.new_event_loop()
        try:
            crash_guard.install_loop_handler(loop)
        finally:
            loop.close()

        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.crash_guard"):
            crash_guard._asyncio_exception_handler(
                loop, {"message": "Unclosed connection"}
            )

        assert any("noise" in r.message for r in caplog.records)
        assert all(r.levelno <= logging.WARNING for r in caplog.records)
        # Must NOT write to crash.log
        crash_log = tmp_path / "home" / "logs" / "crash.log"
        assert not crash_log.exists()

    def test_unclosed_client_session_also_downgraded(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        crash_guard._CRASH_LOG = None
        loop = asyncio.new_event_loop()
        try:
            crash_guard.install_loop_handler(loop)
        finally:
            loop.close()

        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.crash_guard"):
            crash_guard._asyncio_exception_handler(
                loop, {"message": "Unclosed client session"}
            )

        assert any("noise" in r.message for r in caplog.records)
        assert all(r.levelno <= logging.WARNING for r in caplog.records)

    def test_non_unclosed_message_still_errors(self, tmp_path, monkeypatch, caplog):
        """Other no-exception messages must still go to ERROR + crash.log."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        crash_guard._CRASH_LOG = None
        loop = asyncio.new_event_loop()
        try:
            crash_guard.install_loop_handler(loop)
        finally:
            loop.close()

        import logging

        with caplog.at_level(logging.ERROR, logger="kiro_crew.crash_guard"):
            crash_guard._asyncio_exception_handler(
                loop, {"message": "Some other problem"}
            )

        assert any(r.levelno == logging.ERROR for r in caplog.records)
        crash_log = tmp_path / "home" / "logs" / "crash.log"
        assert crash_log.exists()
        assert "Some other problem" in crash_log.read_text()


class TestWindowsProactorShutdownDowngrade:
    """A reset repeated by Proactor's close callback is disconnect noise."""

    def test_connection_lost_callback_reset_is_warning_only(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        crash_guard._CRASH_LOG = None
        loop = asyncio.new_event_loop()
        try:
            crash_guard.install_loop_handler(loop)
        finally:
            loop.close()

        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.crash_guard"):
            crash_guard._asyncio_exception_handler(
                loop,
                {
                    "message": (
                        "Exception in callback "
                        "_ProactorBasePipeTransport._call_connection_lost(None)"
                    ),
                    "exception": ConnectionResetError(10054, "peer reset"),
                },
            )

        assert any("noise" in record.message for record in caplog.records)
        assert all(record.levelno <= logging.WARNING for record in caplog.records)
        assert not (tmp_path / "home" / "logs" / "crash.log").exists()

    def test_other_connection_reset_stays_an_error(self, tmp_path, monkeypatch, caplog):
        """A task-level reset may be a real defect and must retain crash evidence."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        crash_guard._CRASH_LOG = None
        loop = asyncio.new_event_loop()
        try:
            crash_guard.install_loop_handler(loop)
        finally:
            loop.close()

        import logging

        with caplog.at_level(logging.ERROR, logger="kiro_crew.crash_guard"):
            crash_guard._asyncio_exception_handler(
                loop,
                {
                    "message": "Task exception was never retrieved",
                    "exception": ConnectionResetError(10054, "peer reset"),
                },
            )

        assert any(record.levelno == logging.ERROR for record in caplog.records)
        crash_log = tmp_path / "home" / "logs" / "crash.log"
        assert "Task exception was never retrieved" in crash_log.read_text()


class TestInstallIdempotent:
    """``install()`` is documented "Idempotent." — pin the early-return contract.

    The ``if _INSTALLED: return`` arc in ``install()`` previously executed only
    when two tests calling ``install()`` landed in the same pytest-xdist worker,
    so its line coverage was a scheduling coin flip that flipped the per-file
    coverage floor on unrelated PRs (#5019). These tests exercise that arc
    unconditionally and deterministically: one sets the flag explicitly, the
    other forces the flag off so the first call is the real installation and
    the second call takes the early return.

    Both tests monkeypatch ``atexit.register`` (to a recorder) and
    ``faulthandler.enable`` (to a no-op) BEFORE calling ``install()``, so no
    real registration and no faulthandler re-targeting ever happens: pytest's
    own faulthandler plugin binds the dump fd to the real stderr, and a bare
    ``faulthandler.enable()`` inside a test would silently re-point fatal-signal
    dumps at the captured ``sys.stderr`` for the rest of the worker process.
    """

    @pytest.fixture(autouse=True)
    def _restore_install_state(self, tmp_path, monkeypatch):
        """Save/restore the module globals the tests mutate.

        ``_CRASH_LOG`` is restored by the module-level autouse fixture. The
        atexit and faulthandler side effects need no rollback because both are
        monkeypatched away before any ``install()`` call in this class.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        saved_installed = crash_guard._INSTALLED
        saved_hook = sys.excepthook
        yield
        crash_guard._INSTALLED = saved_installed
        sys.excepthook = saved_hook

    @staticmethod
    def _neutralize_globals(monkeypatch) -> list[object]:
        """Spy ``atexit.register`` and no-op ``faulthandler.enable``."""
        registrations: list[object] = []
        monkeypatch.setattr(
            atexit, "register", lambda fn, *a, **kw: registrations.append(fn)
        )
        monkeypatch.setattr(faulthandler, "enable", lambda *a, **kw: None)
        return registrations

    def test_early_return_when_flag_already_set(self, monkeypatch):
        """The early-return arc, exercised regardless of prior worker state."""
        crash_guard._INSTALLED = True
        registrations = self._neutralize_globals(monkeypatch)
        hook_before = sys.excepthook
        crash_log_before = crash_guard._CRASH_LOG

        crash_guard.install()

        assert sys.excepthook is hook_before
        assert registrations == []
        assert crash_guard._CRASH_LOG is crash_log_before

    def test_second_install_is_a_noop(self, monkeypatch):
        """A real install followed by a second call that must change nothing."""
        crash_guard._INSTALLED = False  # deterministic: first call really installs
        registrations = self._neutralize_globals(monkeypatch)

        crash_guard.install()

        assert crash_guard._INSTALLED is True
        assert registrations == [crash_guard._atexit_handler]
        assert sys.excepthook is crash_guard._excepthook
        crash_log_after_first = crash_guard._CRASH_LOG
        assert crash_log_after_first is not None

        crash_guard.install()

        assert sys.excepthook is crash_guard._excepthook
        assert registrations == [crash_guard._atexit_handler]
        assert crash_guard._CRASH_LOG is crash_log_after_first
