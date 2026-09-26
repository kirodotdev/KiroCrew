"""Tests for CLI logging setup — the detached-gateway ``gateway.log``
double-write fix.

``_spawn_detached_gateway`` redirects the child gateway's stdout/stderr INTO
``gateway.log``. Before the fix, ``main()``'s ``basicConfig`` console handler
(root → stderr) then wrote a second, console-formatted copy (no [PID]) of
every ``kiro_crew`` record into the same file the rotating file handler
writes, doubling log volume and halving the 2MB rotation window. The boot
rotation also renamed the inode fds 1/2 point at, sending raw stderr writes
into ``gateway.log.prev``.

Covers:
- ``_fd_targets_file``  — the dev/ino detection primitive
- ``_redirect_fds_to``  — the post-rotation fd re-point primitive
- ``_setup_cli_logging`` — handler topology in detached vs foreground mode
- the QueueHandler/QueueListener indirection — the file handler (rollover
  included) runs on the listener thread, never the calling thread, and
  ``_stop_log_queue_listener`` drains every queued record to disk
"""

import ast
import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew.cli as cli_mod
from kiro_crew.cli import (
    _CliLogQueueHandler,
    _fd_targets_file,
    _FdTrackingRotatingFileHandler,
    _redirect_fds_to,
    _setup_cli_logging,
    _stop_log_queue_listener,
    drain_log_queue_before_hard_exit,
)
from kiro_crew.config import config_dir


@pytest.fixture(autouse=True)
def _pristine_logging():
    """Snapshot, clear, and restore global logging state around each test.

    ``_setup_cli_logging`` mutates the root and ``kiro_crew`` loggers
    (handlers + levels), and these tests assert on absolute handler
    topology. Other tests in the same process may have leaked handlers
    onto either logger (they are process-global), so start each test from
    empty handler lists, then restore the originals afterwards — closing
    any handler the test added so the tmp gateway.log file descriptor is
    released promptly (required on Windows, where an open fd blocks the
    tmpdir cleanup). Also stops the module's QueueListener so its drain
    thread and the file handler's fd never leak across tests.
    """
    root = logging.getLogger()
    kc = logging.getLogger("kiro_crew")
    saved_root = (root.handlers[:], root.level)
    saved_kc = (kc.handlers[:], kc.level)
    root.handlers[:] = []
    kc.handlers[:] = []
    yield
    listener = cli_mod._LOG_QUEUE_LISTENER
    _stop_log_queue_listener()
    if listener is not None:
        # The drain returns before closing the file handler when a test left
        # the listener stopped (``QueueListener.stop`` raises on a listener
        # whose thread is gone). Close the listener's handlers here regardless:
        # ``Handler.close`` is idempotent, and the tmpdir cleanup on Windows
        # needs the gateway.log fd released whether or not the drain got there.
        for handler in listener.handlers:
            handler.close()
    for logger, (handlers, _) in ((root, saved_root), (kc, saved_kc)):
        for handler in logger.handlers[:]:
            if handler not in handlers:
                logger.removeHandler(handler)
                handler.close()
    root.handlers[:], root.level = saved_root
    kc.handlers[:], kc.level = saved_kc


class TestFdTargetsFile:
    def test_true_when_fd_open_on_path(self, tmp_path):
        target = tmp_path / "gateway.log"
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT)
        try:
            assert _fd_targets_file(fd, target) is True
        finally:
            os.close(fd)

    def test_false_for_different_file(self, tmp_path):
        target = tmp_path / "gateway.log"
        target.write_text("x")
        other = tmp_path / "other.log"
        fd = os.open(str(other), os.O_WRONLY | os.O_CREAT)
        try:
            assert _fd_targets_file(fd, target) is False
        finally:
            os.close(fd)

    def test_false_when_path_missing(self, tmp_path):
        other = tmp_path / "other.log"
        fd = os.open(str(other), os.O_WRONLY | os.O_CREAT)
        try:
            assert _fd_targets_file(fd, tmp_path / "gateway.log") is False
        finally:
            os.close(fd)

    def test_false_when_fd_invalid(self, tmp_path):
        target = tmp_path / "gateway.log"
        target.write_text("x")
        fd = os.open(str(target), os.O_RDONLY)
        os.close(fd)  # now guaranteed-invalid (recently closed)
        assert _fd_targets_file(fd, target) is False


class TestRedirectFdsTo:
    @staticmethod
    def _read(path):
        """Read file bytes with newlines normalized to ``\\n``.

        On Windows the CRT opens fds in text mode by default (both the test's
        own ``os.open`` and the redirect target), translating ``\\n`` to
        ``\\r\\n`` on write. These tests assert fd *redirection* semantics —
        which file received which write — not platform newline conventions,
        so comparisons are newline-agnostic.
        """
        return path.read_bytes().replace(b"\r\n", b"\n")

    def test_repoints_fd_to_target(self, tmp_path):
        """Writes through the fd land in the new target after redirect."""
        old = tmp_path / "gateway.log.prev"
        target = tmp_path / "gateway.log"
        fd = os.open(str(old), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            os.write(fd, b"before\n")
            _redirect_fds_to(target, fds=(fd,))
            os.write(fd, b"after\n")
        finally:
            os.close(fd)
        assert self._read(old) == b"before\n"
        assert self._read(target) == b"after\n"

    def test_appends_to_existing_target(self, tmp_path):
        """O_APPEND: existing live-log content must not be truncated."""
        old = tmp_path / "old.log"
        target = tmp_path / "gateway.log"
        target.write_bytes(b"existing\n")
        fd = os.open(str(old), os.O_WRONLY | os.O_CREAT)
        try:
            _redirect_fds_to(target, fds=(fd,))
            os.write(fd, b"new\n")
        finally:
            os.close(fd)
        assert self._read(target) == b"existing\nnew\n"

    def test_unopenable_target_is_best_effort_noop(self, tmp_path):
        """A target that cannot be opened leaves the fds untouched."""
        old = tmp_path / "old.log"
        fd = os.open(str(old), os.O_WRONLY | os.O_CREAT)
        try:
            _redirect_fds_to(tmp_path / "no-such-dir" / "gateway.log", fds=(fd,))
            os.write(fd, b"still-old\n")
        finally:
            os.close(fd)
        assert self._read(old) == b"still-old\n"


class TestFdTrackingRotatingFileHandler:
    @pytest.mark.skipif(
        os.name == "nt",
        reason=(
            "Windows cannot rename a file while a raw fd without "
            "FILE_SHARE_DELETE holds it open, so the follow-the-renamed-inode "
            "hazard this test exercises is POSIX-specific; the doRollover "
            "hook test below covers the subclass on all platforms."
        ),
    )
    def test_rollover_repoints_fd_to_new_base_file(self, tmp_path):
        """GPT finding: after a size rollover renames gateway.log, redirected
        raw fds must follow the NEW base file — not the renamed inode through
        .1 → .2 → .3 → unlink, where raw stderr would vanish from all
        retained logs."""
        log = tmp_path / "gateway.log"
        # A scratch fd stands in for raw stderr (patching fd 2 itself would
        # eat pytest's own output); point the handler's rollover at it.
        scratch = os.open(str(tmp_path / "scratch"), os.O_WRONLY | os.O_CREAT)
        try:
            handler = _FdTrackingRotatingFileHandler(
                log, maxBytes=64, backupCount=2, encoding="utf-8"
            )
            _redirect_fds_to(log, fds=(scratch,))
            os.write(scratch, b"raw-before\n")
            # Force a rollover: two emits (an empty file never rolls —
            # gh-116263). The first fills past maxBytes, the second rolls
            # gateway.log -> gateway.log.1 and creates a new gateway.log.
            for _ in range(2):
                handler.emit(
                    logging.LogRecord("t", logging.WARNING, __file__, 1, "x" * 100, None, None)
                )
            assert (tmp_path / "gateway.log.1").exists()
            # doRollover re-points fds (1, 2) at the new base file; emulate the
            # same re-point for the scratch fd to assert the mechanism, then
            # verify the raw write lands in the NEW file, not the renamed one.
            _redirect_fds_to(Path(handler.baseFilename), fds=(scratch,))
            os.write(scratch, b"raw-after\n")
            after = log.read_bytes().replace(b"\r\n", b"\n")
            assert b"raw-after\n" in after
            rotated = (tmp_path / "gateway.log.1").read_bytes().replace(b"\r\n", b"\n")
            assert b"raw-after" not in rotated
            handler.close()
        finally:
            os.close(scratch)

    def test_do_rollover_calls_fd_redirect(self, tmp_path, monkeypatch):
        """The subclass must re-point fds 1/2 at the new base file on every
        rollover — pin the doRollover hook itself."""
        import kiro_crew.cli as cli_mod

        calls: list[Path] = []
        monkeypatch.setattr(
            cli_mod, "_redirect_fds_to", lambda path, fds=(1, 2): calls.append(path)
        )
        log = tmp_path / "gateway.log"
        handler = _FdTrackingRotatingFileHandler(log, maxBytes=64, backupCount=2, encoding="utf-8")
        for _ in range(2):  # ≥1 rollover on all versions (3.12+ never rolls an empty file)
            handler.emit(
                logging.LogRecord("t", logging.WARNING, __file__, 1, "x" * 100, None, None)
            )
        handler.close()
        # Exact rollover count is version-dependent (3.10 also rolls the empty
        # file); the invariant is: every rollover re-pointed at the base file.
        assert calls
        assert all(c == Path(handler.baseFilename) for c in calls)


class TestSetupCliLoggingDetached:
    """Handler topology when stderr IS gateway.log (detach-spawned)."""

    @pytest.fixture(autouse=True)
    def _detached(self, monkeypatch):
        # Force detached detection instead of dup2-ing over the REAL fd 2,
        # which would fight pytest's capture machinery. The detection
        # primitive itself is covered by TestFdTargetsFile.
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: True)
        self.redirect = MagicMock()
        monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", self.redirect)

    def test_no_console_handler_installed(self):
        _setup_cli_logging("gateway", 1)
        stream_handlers = [
            h for h in logging.getLogger().handlers if type(h) is logging.StreamHandler
        ]
        assert stream_handlers == []

    def test_queue_handler_on_root_not_kiro_crew(self):
        _setup_cli_logging("gateway", 1)
        root_qhs = [h for h in logging.getLogger().handlers if isinstance(h, _CliLogQueueHandler)]
        assert len(root_qhs) == 1
        kc_qhs = [
            h
            for h in logging.getLogger("kiro_crew").handlers
            if isinstance(h, _CliLogQueueHandler)
        ]
        assert kc_qhs == []
        # The file handler must never sit on a logger directly — inline emit
        # on the event-loop thread is the loop-stall bug this fix removes.
        for logger in (logging.getLogger(), logging.getLogger("kiro_crew")):
            assert not any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
        listener = cli_mod._LOG_QUEUE_LISTENER
        assert listener is not None
        (fh,) = listener.handlers
        # Detached mode needs the fd-tracking subclass so the WHOLE rollover
        # (rename chain + dup2 fd re-point) rides the listener thread.
        assert isinstance(fh, _FdTrackingRotatingFileHandler)
        assert Path(fh.baseFilename) == config_dir() / "gateway.log"

    def test_kiro_crew_record_written_exactly_once(self):
        _setup_cli_logging("gateway", 1)
        logging.getLogger("kiro_crew.test_doublewrite").warning("sentinel-record")
        _stop_log_queue_listener()  # deterministic drain to disk
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert text.count("sentinel-record") == 1
        # And it is the PID-stamped file-handler copy, not the console format.
        assert "[PID" in text

    def test_third_party_warning_still_lands_in_file(self):
        # Before the fix these reached the file only via the accidental
        # stderr echo; the root-attached handler must keep them flowing.
        _setup_cli_logging("gateway", 1)
        logging.getLogger("somelib.test_doublewrite").warning("thirdparty-record")
        _stop_log_queue_listener()  # deterministic drain to disk
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert text.count("thirdparty-record") == 1

    def test_rotation_repoints_std_fds(self):
        log_file = config_dir() / "gateway.log"
        log_file.write_text("previous boot\n", encoding="utf-8")
        _setup_cli_logging("gateway", 1)
        assert (config_dir() / "gateway.log.prev").read_text(encoding="utf-8") == "previous boot\n"
        self.redirect.assert_called_once_with(log_file)

    def test_no_rotation_no_repoint_for_non_gateway_command(self):
        (config_dir() / "gateway.log").write_text("live\n", encoding="utf-8")
        _setup_cli_logging("status", 1)
        assert not (config_dir() / "gateway.log.prev").exists()
        self.redirect.assert_not_called()

    def test_handlers_carry_no_level_third_party_warning_still_lands(self, monkeypatch):
        """The ``kiro_crew`` logger is the SINGLE level gate. Neither the file
        handler nor the queue handler carries a level of its own: a stricter
        persisted kiro_crew level gates kiro_crew records at the kiro_crew
        logger, third-party records are gated at the root logger's WARNING,
        and nothing is re-judged on the way to the file. A level on either
        handler would be a boot-time copy that a runtime ``agent.log_level``
        change does not reach."""
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent.log_level = "ERROR"
        monkeypatch.setattr("kiro_crew.cli.KiroCrewConfig.load", staticmethod(lambda: cfg))
        _setup_cli_logging("gateway", 0)
        (fh,) = cli_mod._LOG_QUEUE_LISTENER.handlers
        (qh,) = [h for h in logging.getLogger().handlers if isinstance(h, _CliLogQueueHandler)]
        assert fh.level == logging.NOTSET
        assert qh.level == logging.NOTSET
        assert cli_mod._LOG_QUEUE_LISTENER.respect_handler_level is False
        assert logging.getLogger("kiro_crew").level == logging.ERROR
        logging.getLogger("somelib.test_single_gate").warning("thirdparty-still-flows")
        logging.getLogger("kiro_crew.test_single_gate").warning("kiro-crew-gated")
        _stop_log_queue_listener()  # deterministic drain to disk
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert "thirdparty-still-flows" in text
        assert "kiro-crew-gated" not in text


class TestSetupCliLoggingForeground:
    """The classic topology must be unchanged when stderr is a real console."""

    @pytest.fixture(autouse=True)
    def _foreground(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        self.redirect = MagicMock()
        monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", self.redirect)

    def test_queue_handler_on_kiro_crew_logger(self):
        _setup_cli_logging("gateway", 1)
        kc_qhs = [
            h
            for h in logging.getLogger("kiro_crew").handlers
            if isinstance(h, _CliLogQueueHandler)
        ]
        assert len(kc_qhs) == 1
        # The level lives on the kiro_crew logger alone; the queue handler and
        # the file handler carry none (a copy here is one a runtime change
        # does not reach).
        assert kc_qhs[0].level == logging.NOTSET
        assert logging.getLogger("kiro_crew").level == logging.INFO
        assert not any(
            isinstance(h, _CliLogQueueHandler) for h in logging.getLogger().handlers
        )
        # No inline file handler on either logger.
        for logger in (logging.getLogger(), logging.getLogger("kiro_crew")):
            assert not any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
        (fh,) = cli_mod._LOG_QUEUE_LISTENER.handlers
        # Foreground keeps the plain handler: no fds were redirected, so
        # there is nothing to re-point on rollover.
        assert type(fh) is RotatingFileHandler
        assert fh.level == logging.NOTSET

    def test_record_written_once_to_file(self):
        _setup_cli_logging("gateway", 1)
        logging.getLogger("kiro_crew.test_foreground").warning("fg-sentinel")
        _stop_log_queue_listener()  # deterministic drain to disk
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert text.count("fg-sentinel") == 1

    def test_std_fds_never_repointed(self):
        (config_dir() / "gateway.log").write_text("previous boot\n", encoding="utf-8")
        _setup_cli_logging("gateway", 1)
        # Rotation still happens (crash-line preservation) …
        assert (config_dir() / "gateway.log.prev").exists()
        # … but a foreground console must never be dup2'd into the log file.
        self.redirect.assert_not_called()


class TestRuntimeLevelReachesFileLog:
    """A runtime ``agent.log_level`` change reaches ``gateway.log`` without a restart.

    ``apply_log_level`` -- the Logs-page toggle and the ``agent.log_level``
    config applier -- moves the ``kiro_crew`` logger, and that logger is the
    single level gate: neither the file handler nor the producer-side queue
    handler pins a boot-time level of its own. A gateway booted at WARNING and
    raised to INFO writes its INFO records to the file ``kirocrew logs`` reads,
    not only to the live Logs stream (its own handler on the logger). These
    tests fail if either handler carries a level.
    """

    @staticmethod
    def _boot(monkeypatch, *, detached: bool, persisted: str = "WARNING") -> None:
        """Build the chain the way an installed gateway boots it: persisted
        level from config (no ``--verbose``), foreground or detach-spawned.
        The config handed to boot is a fresh default object, never the one a
        real ``load()`` returned, so no cached document is mutated."""
        from kiro_crew.config import KiroCrewConfig

        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: detached)
        monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", MagicMock())
        cfg = KiroCrewConfig()
        cfg.agent.log_level = persisted
        monkeypatch.setattr("kiro_crew.cli.KiroCrewConfig.load", staticmethod(lambda: cfg))
        _setup_cli_logging("gateway", 0)

    @staticmethod
    def _sinks(detached: bool):
        producer = logging.getLogger() if detached else logging.getLogger("kiro_crew")
        (qh,) = [h for h in producer.handlers if isinstance(h, _CliLogQueueHandler)]
        (fh,) = cli_mod._LOG_QUEUE_LISTENER.handlers
        return fh, qh

    def test_raised_info_record_reaches_gateway_log(self, monkeypatch):
        from kiro_crew.dashboard.handlers.updates import apply_log_level

        self._boot(monkeypatch, detached=False)
        fh, qh = self._sinks(detached=False)
        assert logging.getLogger("kiro_crew").level == logging.WARNING

        assert apply_log_level("INFO", source="dashboard") is True

        assert logging.getLogger("kiro_crew").level == logging.INFO
        logging.getLogger("kiro_crew.test_runtime_level").info("raised-info-record")
        _stop_log_queue_listener()  # deterministic drain to disk
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert "raised-info-record" in text, "raised INFO record never reached gateway.log"
        # The applier's own confirmation is the first raised record; the
        # reporter saw it on the desktop's stdout capture and never in the file.
        assert "Log level changed to INFO via dashboard" in text
        # The handlers never held a level to move: the logger was the whole gate.
        assert fh.level == qh.level == logging.NOTSET

    def test_detached_tightening_keeps_third_party_warnings_flowing(self, monkeypatch):
        """A stricter kiro_crew level gates kiro_crew records at the kiro_crew
        logger; third-party records on the shared root handler stay gated at
        the root logger's WARNING. Neither handler holds a level to do it."""
        from kiro_crew.dashboard.handlers.updates import apply_log_level

        self._boot(monkeypatch, detached=True)
        fh, qh = self._sinks(detached=True)

        assert apply_log_level("ERROR", source="config") is True

        assert logging.getLogger("kiro_crew").level == logging.ERROR
        assert fh.level == qh.level == logging.NOTSET
        logging.getLogger("somelib.test_runtime_level").warning("thirdparty-still-flows")
        logging.getLogger("kiro_crew.test_runtime_level").warning("kiro-crew-gagged")
        _stop_log_queue_listener()
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert "thirdparty-still-flows" in text
        assert "kiro-crew-gagged" not in text

    def test_detached_raise_does_not_open_the_root_to_third_party_info(self, monkeypatch):
        """Raising kiro_crew to INFO admits kiro_crew INFO; third-party INFO
        stays gated at the root logger's WARNING, exactly as a boot at INFO."""
        from kiro_crew.dashboard.handlers.updates import apply_log_level

        self._boot(monkeypatch, detached=True)

        assert apply_log_level("INFO", source="dashboard") is True

        logging.getLogger("kiro_crew.test_runtime_level").info("kiro-crew-info")
        logging.getLogger("somelib.test_runtime_level").info("thirdparty-info")
        _stop_log_queue_listener()
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert "kiro-crew-info" in text
        assert "thirdparty-info" not in text

    def test_tightening_keeps_records_already_queued(self, monkeypatch):
        """A record logged while INFO was in force must still reach the file
        after a runtime tightening: with no level on the handlers and no
        handler-level re-check at dequeue, nothing judges a queued record by a
        level set after it was logged -- a queue backed up behind a rollover
        on a slow disk holds hundreds of them."""
        from kiro_crew.dashboard.handlers.updates import apply_log_level

        self._boot(monkeypatch, detached=False, persisted="INFO")
        listener = cli_mod._LOG_QUEUE_LISTENER
        assert listener is not None
        listener.stop()  # park the consumer: records now sit in the queue
        try:
            logging.getLogger("kiro_crew.test_runtime_level").info("queued-before-tightening")

            assert apply_log_level("ERROR", source="dashboard") is True
        finally:
            # Teardown drains through ``listener.stop()``, which raises on a
            # listener whose thread is already gone and then skips the file
            # handler's close -- a failed assertion inside this window must
            # not hand it a stopped listener (an open gateway.log fd blocks
            # the tmpdir cleanup on Windows).
            listener.start()  # the consumer resumes with the new level in force
        _stop_log_queue_listener()
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert "queued-before-tightening" in text, "tightening dropped a record already queued"


class TestQueueOffLoop:
    """The point of the queue: file-handler I/O never runs on the calling
    thread (in the gateway that thread runs the asyncio event loop, and an
    inline emit or rollover blocking >25s trips the loop-stall watchdog,
    which hard-exits the process and orphans every in-flight subagent)."""

    @pytest.fixture(autouse=True)
    def _foreground(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", MagicMock())

    def test_file_emit_runs_on_listener_thread(self):
        _setup_cli_logging("gateway", 1)
        (fh,) = cli_mod._LOG_QUEUE_LISTENER.handlers
        emit_threads: list[threading.Thread] = []
        orig_emit = fh.emit

        def recording_emit(record):
            emit_threads.append(threading.current_thread())
            orig_emit(record)

        fh.emit = recording_emit  # type: ignore[method-assign]
        logging.getLogger("kiro_crew.offloop").warning("off-loop-sentinel")
        _stop_log_queue_listener()  # drains the queue through recording_emit
        assert emit_threads, "record never reached the file handler"
        caller = threading.current_thread()
        assert all(t is not caller for t in emit_threads)

    def test_stop_drains_pending_records(self):
        _setup_cli_logging("gateway", 1)
        log = logging.getLogger("kiro_crew.drain")
        for i in range(50):
            log.warning("drain-record-%d", i)
        _stop_log_queue_listener()
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert text.count("drain-record-") == 50

    def test_reentrant_setup_leaves_single_queue_handler(self):
        _setup_cli_logging("gateway", 1)
        _setup_cli_logging("gateway", 1)
        kc_qhs = [
            h
            for h in logging.getLogger("kiro_crew").handlers
            if isinstance(h, _CliLogQueueHandler)
        ]
        assert len(kc_qhs) == 1
        assert cli_mod._LOG_QUEUE_LISTENER is not None

    def test_short_lived_command_keeps_sync_handler(self):
        """Short-lived verbs get NO queue: several replace the process via
        os.exec* (``logs -f``, ``config edit``), which skips atexit — a queued
        tail there would be lost, while a sync handler has already written it.
        They also run no event loop, so there is nothing for the queue to
        protect."""
        _setup_cli_logging("status", 1)
        assert cli_mod._LOG_QUEUE_LISTENER is None
        kc = logging.getLogger("kiro_crew")
        assert not any(isinstance(h, _CliLogQueueHandler) for h in kc.handlers)
        sync_fhs = [h for h in kc.handlers if isinstance(h, RotatingFileHandler)]
        assert len(sync_fhs) == 1
        logging.getLogger("kiro_crew.short").warning("short-lived-record")
        for h in kc.handlers:
            h.flush()
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert "short-lived-record" in text

    def test_stop_is_idempotent(self):
        _setup_cli_logging("gateway", 1)
        _stop_log_queue_listener()
        _stop_log_queue_listener()  # second stop must be a silent no-op
        assert cli_mod._LOG_QUEUE_LISTENER is None

    def test_bounded_stop_still_drains_when_healthy(self):
        """The force-exit path (timeout=) must still flush a healthy queue."""
        _setup_cli_logging("gateway", 1)
        logging.getLogger("kiro_crew.bounded").warning("bounded-record")
        _stop_log_queue_listener(timeout=5.0)
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert "bounded-record" in text
        assert cli_mod._LOG_QUEUE_LISTENER is None

    def test_bounded_stop_gives_up_on_wedged_handler(self):
        """A wedged disk must not hang the force exit: the bounded stop
        returns within the deadline and skips flush/close."""
        import time

        _setup_cli_logging("gateway", 1)
        (fh,) = cli_mod._LOG_QUEUE_LISTENER.handlers
        release = threading.Event()
        orig_emit = fh.emit

        def wedged_emit(record):
            release.wait(5.0)  # simulate blocking handler I/O
            orig_emit(record)

        fh.emit = wedged_emit  # type: ignore[method-assign]
        logging.getLogger("kiro_crew.wedged").warning("wedged-record")
        t0 = time.monotonic()
        _stop_log_queue_listener(timeout=0.2)
        elapsed = time.monotonic() - t0
        release.set()  # unblock the daemon thread so teardown closes cleanly
        assert elapsed < 2.0
        assert cli_mod._LOG_QUEUE_LISTENER is None


class TestDrainBeforeHardExit:
    """``os._exit`` runs no ``atexit`` handler, so every ``async`` path that
    hard-exits the gateway must drain the log queue itself - bounded, and off
    the event loop."""

    @pytest.fixture(autouse=True)
    def _foreground(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", MagicMock())

    @pytest.mark.asyncio
    async def test_queued_tail_reaches_disk(self):
        """The records logged immediately before a hard exit - the ones a
        post-mortem actually needs - are on disk when the drain returns."""
        _setup_cli_logging("gateway", 1)
        logging.getLogger("kiro_crew.shutdown").warning("shutdown-tail-record")
        await drain_log_queue_before_hard_exit()
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert "shutdown-tail-record" in text
        assert cli_mod._LOG_QUEUE_LISTENER is None

    @pytest.mark.asyncio
    async def test_join_runs_off_the_event_loop_thread(self):
        """The synchronous listener join happens on an executor thread: doing
        it inline would park the loop for up to the drain deadline."""
        _setup_cli_logging("gateway", 1)
        join_threads: list[threading.Thread] = []
        real_stop = cli_mod._stop_log_queue_listener

        def recording_stop(timeout=None):
            join_threads.append(threading.current_thread())
            real_stop(timeout)

        cli_mod._stop_log_queue_listener = recording_stop  # type: ignore[assignment]
        try:
            await drain_log_queue_before_hard_exit()
        finally:
            cli_mod._stop_log_queue_listener = real_stop  # type: ignore[assignment]
        assert join_threads, "drain never reached the listener stop"
        assert all(t is not threading.current_thread() for t in join_threads)

    @pytest.mark.asyncio
    async def test_wedged_handler_does_not_delay_the_exit(self):
        """A wedged disk must not hold the loop past the deadline: the drain
        returns bounded, and the exit proceeds without it."""
        import time

        _setup_cli_logging("gateway", 1)
        (fh,) = cli_mod._LOG_QUEUE_LISTENER.handlers
        release = threading.Event()
        orig_emit = fh.emit

        def wedged_emit(record):
            release.wait(10.0)  # simulate blocking handler I/O
            orig_emit(record)

        fh.emit = wedged_emit  # type: ignore[method-assign]
        logging.getLogger("kiro_crew.wedged").warning("wedged-shutdown-record")
        t0 = time.monotonic()
        await drain_log_queue_before_hard_exit(timeout=0.2)
        elapsed = time.monotonic() - t0
        release.set()  # unblock the daemon thread so teardown closes cleanly
        assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_no_listener_is_a_silent_no_op(self):
        """Short-lived verbs never start a listener; the drain must still be
        safe to await, and must never raise into a hard-exit path."""
        _setup_cli_logging("status", 1)
        assert cli_mod._LOG_QUEUE_LISTENER is None
        await drain_log_queue_before_hard_exit()

    @pytest.mark.asyncio
    async def test_a_failing_drain_never_blocks_the_exit(self):
        """Logging must not be able to abort a shutdown: a stop that raises is
        swallowed, not propagated to the caller about to ``os._exit``."""
        _setup_cli_logging("gateway", 1)
        real_stop = cli_mod._stop_log_queue_listener

        def exploding_stop(timeout=None):
            raise RuntimeError("interpreter teardown race")

        cli_mod._stop_log_queue_listener = exploding_stop  # type: ignore[assignment]
        try:
            await drain_log_queue_before_hard_exit()
        finally:
            cli_mod._stop_log_queue_listener = real_stop  # type: ignore[assignment]


class TestEveryGatewayHardExitDrainsTheQueue:
    """Ratchet: the queue only helps if EVERY hard exit in the gateway process
    drains it. ``src/kiro_crew/slack/gateway.py`` and ``.../events.py`` are the
    two modules that call ``os._exit`` from inside the long-lived gateway
    process - the other ``os._exit`` sites in the tree (``sandbox.py``,
    ``_process_group_supervisor.py``) run in forked/pre-exec children that
    never install the CLI's queue listener.

    The check is per-function and does NOT look inside nested functions, so a
    drain in a sibling closure cannot vouch for its parent."""

    _MODULES = (
        Path(__file__).resolve().parents[1] / "src/kiro_crew/slack/gateway.py",
        Path(__file__).resolve().parents[1] / "src/kiro_crew/slack/events.py",
    )
    _DRAINS = {"_stop_log_queue_listener", "drain_log_queue_before_hard_exit"}

    @staticmethod
    def _own_body_names(fn):
        """Every Name/Attribute identifier in ``fn``'s own body, skipping the
        bodies of functions nested inside it."""
        names: set[str] = set()

        class _Walk(ast.NodeVisitor):
            def visit_FunctionDef(self, node):  # nested def: not fn's own body
                if node is fn:
                    self.generic_visit(node)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Name(self, node):
                names.add(node.id)

            def visit_Attribute(self, node):
                names.add(node.attr)
                self.generic_visit(node)

        _Walk().visit(fn)
        return names

    def _hard_exit_functions(self, tree):
        """(function node, line) for each ``os._exit(...)`` call, attributed to
        the nearest enclosing function."""
        parents: "dict[ast.AST, ast.AST]" = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        found = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_exit"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                continue
            cur = parents.get(node)
            while cur is not None and not isinstance(
                cur, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                cur = parents.get(cur)
            if cur is not None:
                found.append((cur, node.lineno))
        return found

    def test_the_ratchet_actually_finds_the_hard_exits(self):
        """A scan that matches nothing would pass vacuously."""
        total = 0
        for path in self._MODULES:
            total += len(
                self._hard_exit_functions(ast.parse(path.read_text(encoding="utf-8")))
            )
        assert total >= 3, f"expected the known os._exit sites, found {total}"

    def test_no_hard_exit_strands_the_queued_log_tail(self):
        violations = []
        for path in self._MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn, lineno in self._hard_exit_functions(tree):
                if not (self._own_body_names(fn) & self._DRAINS):
                    violations.append(f"{path.name}:{lineno} in {fn.name}()")
        assert not violations, (
            "os._exit skips atexit, so these hard exits drop the queued "
            "gateway.log tail; await drain_log_queue_before_hard_exit() (or "
            "call _stop_log_queue_listener(timeout=...) from a sync handler) "
            "first: " + ", ".join(violations)
        )
