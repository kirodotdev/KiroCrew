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
