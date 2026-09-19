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
import builtins
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

    Without these tests, the ``if _INSTALLED: return`` arc in ``install()`` runs
    only when two tests calling ``install()`` land in the same pytest-xdist worker,
    so its line coverage is a scheduling coin flip that flips the per-file
    coverage floor on unrelated PRs. These tests exercise that arc
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


class TestNonAsciiCrashRecord:
    """crash.log is written as utf-8, so a non-ASCII crash still lands whole.

    The writer is the last-resort diagnostic sink and its body sits inside
    ``except Exception: pass``. With a locale-encoded ``open()`` a cp1252 host
    raises ``UnicodeEncodeError`` on the first non-ASCII byte -- from the
    exception message, or from a source line echoed by
    ``traceback.print_exception`` -- and the swallow turns that into a missing
    or half-written record, as reported from a native Windows install. These
    tests force an ASCII default so the locale-encoded variant reproduces that
    loss here, on any host.
    """

    # ``open()`` parameters after ``mode``, in positional order — the fixture
    # normalizes positionals into keywords so it still sees an ``encoding``
    # passed as ``open(path, "a", -1, "utf-8")``. Without that, a caller using
    # positional form would silently escape the forced ASCII default and these
    # tests would pass without exercising the guard at all.
    _OPEN_PARAMS = ("buffering", "encoding", "errors", "newline", "closefd", "opener")

    @pytest.fixture
    def ascii_default_open(self, monkeypatch):
        """Make an ``open()`` that names no ``encoding=`` behave like cp1252/ascii.

        This is the host condition the ticket reports, expressed without
        depending on the test machine's locale or on ``PYTHONUTF8``.
        """
        real_open = builtins.open
        params = self._OPEN_PARAMS

        def _open(file, mode="r", *args, **kwargs):
            kwargs.update(zip(params, args))
            if "b" not in mode and kwargs.get("encoding") is None:
                kwargs["encoding"] = "ascii"
            return real_open(file, mode, **kwargs)

        monkeypatch.setattr(builtins, "open", _open)

    @pytest.mark.parametrize("positional", [False, True])
    def test_fixture_really_forces_an_ascii_default(
        self, tmp_path, ascii_default_open, positional
    ):
        """Pin the fixture itself: unless a call names utf-8, ASCII is enforced.

        The two tests below only prove anything while this holds, and it is the
        half a later refactor can silently break — by passing ``encoding``
        positionally, which would leave the default untouched.
        """
        target = tmp_path / "probe.txt"
        with pytest.raises(UnicodeEncodeError):
            with open(target, "w") as f:
                f.write("café")

        # A call that DOES name utf-8, keyword or positional, is left alone.
        if positional:
            handle = open(target, "w", -1, "utf-8")
        else:
            handle = open(target, "w", encoding="utf-8")
        with handle as f:
            f.write("café")
        assert target.read_text(encoding="utf-8") == "café"

    @staticmethod
    def _raise_non_ascii() -> tuple:
        """Raise with a non-ASCII message from a non-ASCII source line."""
        try:
            raise RuntimeError("🐾 gateway startup failed — café")  # noqa: RUF001
        except RuntimeError:
            return sys.exc_info()

    def test_full_record_survives_an_ascii_default_encoding(self, tmp_path, ascii_default_open):
        crash_log = tmp_path / "crash.log"
        crash_guard._CRASH_LOG = crash_log
        exc_info = self._raise_non_ascii()

        crash_guard._write_crash(
            f"UNHANDLED EXCEPTION: {exc_info[0].__name__}: {exc_info[1]}", exc_info
        )

        text = crash_log.read_text(encoding="utf-8")
        # The header glyph and the accented word both reach the file...
        assert "🐾" in text
        assert "café" in text
        # ...and the record is whole: traceback, the echoed source line that
        # carries the non-ASCII literal, and the closing separator.
        assert "Traceback (most recent call last)" in text
        assert "_raise_non_ascii" in text
        assert 'raise RuntimeError("🐾 gateway startup failed — café")' in text
        assert text.rstrip().endswith("=" * 72)

    def test_unencodable_surrogate_does_not_lose_the_record(self, tmp_path, ascii_default_open):
        """``errors="backslashreplace"`` keeps even a lone surrogate writable.

        A surrogate reaches the writer from any string built out of undecodable
        OS bytes (``surrogateescape``), and utf-8 alone cannot encode it -- so
        without the error handler the record is swallowed like before.
        """
        crash_log = tmp_path / "crash.log"
        crash_guard._CRASH_LOG = crash_log

        crash_guard._write_crash("UNHANDLED EXCEPTION: OSError: bad path \ud800 tail")

        text = crash_log.read_text(encoding="utf-8")
        assert "bad path" in text
        assert "tail" in text
        assert text.rstrip().endswith("=" * 72)
