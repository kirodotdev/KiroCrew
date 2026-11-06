"""Tests for CLI logging setup in a detached gateway.

The detached child's stdout/stderr already point INTO ``gateway.log``, so a console
handler would write every record twice, and boot rotation must re-point fds 1/2.

Covers:
- ``_fd_targets_file``  — the dev/ino detection primitive
- ``_redirect_fds_to``  — the post-rotation fd re-point primitive
- ``_setup_cli_logging`` — handler topology in detached vs foreground mode
- the QueueHandler/QueueListener indirection — the file handler (rollover
  included) runs on the listener thread, never the calling thread, and
  ``_stop_log_queue_listener`` drains every queued record to disk
"""

import ast
import errno
import io
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew.cli as cli_mod
from conftest import requires_symlinks
from kiro_crew.cli import (
    _CliLogQueueHandler,
    _fd_targets_file,
    _FdTrackingRotatingFileHandler,
    _redirect_fds_to,
    _setup_cli_logging,
    _stop_log_queue_listener,
    drain_log_queue_before_hard_exit,
)
from kiro_crew.cli_log_files import boot_log_dir, boot_log_paths, older_boot_logs
from kiro_crew.cli_logging import _fd_is_regular_file as _real_fd_is_regular_file
from kiro_crew.cli_logging import _rotate_boot_logs, _start_gateway_log_after_lock
from kiro_crew.config import config_dir


@pytest.fixture(autouse=True)
def _stderr_is_not_a_file(_floor_monkeypatch):
    """pytest's fd capture points fd 2 at a temp FILE, which a gateway reads as
    a service manager's error file. Tests model a console unless they opt in
    (TestSetupCliLoggingServiceErrorFile)."""
    _floor_monkeypatch.setattr("kiro_crew.cli._fd_is_regular_file", lambda fd: False)


@pytest.fixture(autouse=True)
def _pristine_logging():
    """Snapshot, clear, and restore global logging state around each test.

    These tests assert absolute handler topology, and other tests may leak handlers
    onto these process-global loggers. Added handlers are closed so Windows can clean
    the tmpdir, and the QueueListener is stopped so its thread and fd never leak.
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

        The Windows CRT opens fds in text mode and writes ``\\r\\n``; these tests pin
        which file got which write, not newline conventions.
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


def _open_fds_on(path: Path) -> list[str]:
    """Descriptors of THIS process whose target is *path* (Linux ``/proc``)."""
    fd_dir = Path("/proc/self/fd")
    held = []
    for entry in fd_dir.iterdir():
        try:
            if os.readlink(entry) == str(path):
                held.append(entry.name)
        except OSError:
            continue  # the fd closed while we listed it
    return held


class TestAppendOnlyLogFileHandler:
    """A process that is not the gateway must not keep ``gateway.log`` open.

    Windows refuses to rename a file another process holds open, so an MCP
    server that kept its own handle made every gateway rollover fail and the
    gateway's file log dropped every later record until restart.
    """

    def test_no_handle_held_after_construction_or_emit(self, tmp_path):
        log = tmp_path / "gateway.log"
        handler = cli_mod._AppendOnlyLogFileHandler(log, encoding="utf-8")
        try:
            assert handler.stream is None
            record = logging.LogRecord("kiro_crew.t", logging.WARNING, "", 0, "one", None, None)
            handler.handle(record)
            handler.handle(record)
            assert handler.stream is None
            assert log.read_text(encoding="utf-8") == "one\none\n"
        finally:
            handler.close()

    def test_never_rotates(self, tmp_path):
        log = tmp_path / "gateway.log"
        log.write_text("x" * (3 * 1024 * 1024), encoding="utf-8")
        handler = cli_mod._AppendOnlyLogFileHandler(log, encoding="utf-8")
        try:
            handler.handle(
                logging.LogRecord("kiro_crew.t", logging.WARNING, "", 0, "tail", None, None)
            )
        finally:
            handler.close()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["gateway.log"]
        assert log.read_text(encoding="utf-8").endswith("tail\n")

    def test_unopenable_path_raises_at_construction(self, tmp_path):
        # A sandbox deny must surface at setup, where the caller falls back to
        # console-only logging, rather than as one traceback per record.
        with pytest.raises(OSError):
            cli_mod._AppendOnlyLogFileHandler(tmp_path / "missing-dir" / "gateway.log")

    def test_failed_open_on_emit_is_reported_not_raised(self, tmp_path, monkeypatch):
        log = tmp_path / "gateway.log"
        handler = cli_mod._AppendOnlyLogFileHandler(log, encoding="utf-8")
        reported = []
        monkeypatch.setattr(handler, "handleError", reported.append)

        def _deny():
            raise PermissionError(errno.EACCES, "denied")

        monkeypatch.setattr(handler, "_open", _deny)
        record = logging.LogRecord("kiro_crew.t", logging.WARNING, "", 0, "lost", None, None)
        handler.handle(record)  # must not raise into the caller
        assert reported == [record]
        handler.close()

    def test_recursion_error_propagates_and_releases_the_handle(self, tmp_path, monkeypatch):
        # Like the stdlib handlers, a RecursionError is not swallowed into
        # handleError: it reaches the code that logged.
        log = tmp_path / "gateway.log"
        handler = cli_mod._AppendOnlyLogFileHandler(log, encoding="utf-8")
        reported: list[logging.LogRecord] = []
        monkeypatch.setattr(handler, "handleError", reported.append)

        def _recurse(_record):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(handler, "format", _recurse)
        record = logging.LogRecord("kiro_crew.t", logging.WARNING, "", 0, "deep", None, None)
        with pytest.raises(RecursionError):
            handler.emit(record)
        assert reported == []
        assert handler.stream is None
        handler.close()

    def test_failed_close_after_emit_still_releases_the_handle(self, tmp_path, monkeypatch):
        # The record is already written when the close fails, so the error is
        # swallowed, and the handler must still drop the stream: holding it is
        # what blocks the gateway's rollover on Windows.
        log = tmp_path / "gateway.log"
        handler = cli_mod._AppendOnlyLogFileHandler(log, encoding="utf-8")
        reported: list[logging.LogRecord] = []
        monkeypatch.setattr(handler, "handleError", reported.append)
        real_open = handler._open
        closes: list[bool] = []

        def _open_with_failing_close():
            stream = real_open()
            real_close = stream.close

            def _close():
                closes.append(True)
                real_close()
                raise OSError(errno.EIO, "close failed")

            stream.close = _close
            return stream

        monkeypatch.setattr(handler, "_open", _open_with_failing_close)
        record = logging.LogRecord("kiro_crew.t", logging.WARNING, "", 0, "kept", None, None)
        handler.handle(record)  # must not raise into the caller
        assert closes == [True]
        assert handler.stream is None
        assert reported == []
        assert log.read_text(encoding="utf-8") == "kept\n"
        handler.close()

    @pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc fd listing")
    @pytest.mark.parametrize("command", ["mcp-core", "mcp-cron", "chat", "status"])
    def test_non_gateway_command_holds_no_handle_on_gateway_log(self, command, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        log_file = config_dir() / "gateway.log"
        _setup_cli_logging(command, 1)
        logging.getLogger("kiro_crew.holder").warning("from-%s", command)
        _stop_log_queue_listener()  # chat writes through the listener thread
        assert f"from-{command}" in log_file.read_text(encoding="utf-8")
        assert _open_fds_on(log_file) == []

    @pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc fd listing")
    def test_gateway_is_the_only_holder(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        _setup_cli_logging("gateway", 1)
        logging.getLogger("kiro_crew.holder").warning("gateway-record")
        listener = cli_mod._LOG_QUEUE_LISTENER
        assert listener is not None
        (fh,) = listener.handlers
        assert isinstance(fh, RotatingFileHandler)
        # Before the gateway lock is held the size rollover is off: a start the
        # lock refuses must not rename a running gateway's log.
        assert fh.maxBytes == 0
        _start_gateway_log_after_lock()
        assert fh.maxBytes == 2 * 1024 * 1024


def _boot() -> None:
    """A gateway start that gets the gateway lock: set up logging, then rotate."""
    _setup_cli_logging("gateway", 1)
    _start_gateway_log_after_lock()


class TestBootRotationFailureIsReported:
    def test_failed_boot_rotation_logs_a_warning(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        log_file = config_dir() / "gateway.log"
        log_file.write_text("previous run\n", encoding="utf-8")

        def _held(self, target):
            raise PermissionError(errno.EACCES, "held by another process")

        monkeypatch.setattr(Path, "rename", _held)
        _boot()
        _stop_log_queue_listener()
        text = log_file.read_text(encoding="utf-8")
        assert text.startswith("previous run\n")
        assert "could not move" in text and "held by another process" in text


def _boot_logs(log_file: Path) -> list[str]:
    """Text of gateway.log.prev (when present) then each kept older boot, newest first."""
    return [p.read_text(encoding="utf-8") for p in boot_log_paths(log_file) if p.exists()]


def _keep(log_file: Path, texts: list[str]) -> None:
    """Lay out kept boots, newest first: *texts[0]* is .prev, the rest are older."""
    archive = boot_log_dir(log_file)
    archive.mkdir(parents=True, exist_ok=True)
    for seq, text in enumerate(reversed(texts[1:]), start=1):
        (archive / f"gateway.log.{seq}").write_text(text, encoding="utf-8")
    log_file.with_name(log_file.name + ".prev").write_text(texts[0], encoding="utf-8")


def _rotate(log_file: Path) -> tuple[OSError | None, list[OSError]]:
    """Rotate as boot does: with the live log's size read first."""
    return _rotate_boot_logs(log_file, log_file.stat().st_size)


def _held(monkeypatch, *names: str) -> None:
    """Model a Windows reader holding each named file: renaming it, or onto it, fails."""
    real_rename = Path.rename

    def _rename(self, target):
        if self.name in names or Path(target).name in names:
            raise PermissionError(errno.EACCES, "held by a reader")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _rename)


class TestBootLogGenerations:
    """Each gateway boot keeps the runs before it: .prev, then up to three older boots."""

    def test_two_restarts_keep_the_run_before_the_last(self, monkeypatch):
        # The reported failure: run A fails, the user restarts twice, and A's log was gone.
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", MagicMock())
        log_file = config_dir() / "gateway.log"
        log_file.write_text("run A failed\n", encoding="utf-8")
        _boot()  # boot B
        logging.getLogger("kiro_crew.test_boot_gen").warning("run B record")
        _stop_log_queue_listener()
        _boot()  # boot C
        _stop_log_queue_listener()
        kept = _boot_logs(log_file)
        assert len(kept) == 2
        assert "run B record" in kept[0]
        assert kept[1] == "run A failed\n"

    def test_five_boots_keep_four_and_drop_the_oldest(self, tmp_path):
        log_file = tmp_path / "gateway.log"
        for run in range(1, 6):
            log_file.write_text(f"run {run}\n", encoding="utf-8")
            # A clock that goes backwards every boot: order must not depend on it.
            os.utime(log_file, ns=((10 - run) * 10**9, (10 - run) * 10**9))
            assert _rotate(log_file) == (None, [])
        assert not log_file.exists()
        assert _boot_logs(log_file) == ["run 5\n", "run 4\n", "run 3\n", "run 2\n"]
        assert sorted(p.name for p in boot_log_dir(log_file).iterdir()) == [
            "gateway.log.2",
            "gateway.log.3",
            "gateway.log.4",
        ]

    def test_size_budget_drops_older_boots_but_never_prev(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli.BOOT_LOG_GENERATIONS_MAX_BYTES", 10)
        log_file = tmp_path / "gateway.log"
        _keep(log_file, ["111\n", "2222\n"])
        log_file.write_text("0" * 20 + "\n", encoding="utf-8")  # alone over budget
        assert _rotate(log_file) == (None, [])
        assert _boot_logs(log_file) == ["0" * 20 + "\n"]

    def test_size_budget_keeps_boots_that_fit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli.BOOT_LOG_GENERATIONS_MAX_BYTES", 14)
        log_file = tmp_path / "gateway.log"
        _keep(log_file, ["bbb\n", "cccc\n"])
        log_file.write_text("aaaaaaa\n", encoding="utf-8")
        assert _rotate(log_file) == (None, [])
        # 8 + 4 fit in 14; adding the next boot's 5 does not, so it goes.
        assert _boot_logs(log_file) == ["aaaaaaa\n", "bbb\n"]

    def test_held_prev_moves_nothing(self, tmp_path, monkeypatch):
        log_file = tmp_path / "gateway.log"
        kept = ["older run\n", "boot 2 back\n", "boot 3 back\n", "boot 4 back\n"]
        _keep(log_file, kept)
        log_file.write_text("last run\n", encoding="utf-8")
        _held(monkeypatch, "gateway.log.prev")
        for _ in range(4):
            rotation_error, errors = _rotate(log_file)
            assert str(rotation_error) == "[Errno 13] held by a reader"
            assert errors == []
        assert log_file.read_text(encoding="utf-8") == "last run\n"
        assert _boot_logs(log_file) == kept

    def test_held_live_log_loses_no_boot(self, tmp_path, monkeypatch):
        # The live log cannot move after the old .prev already moved: the old .prev is
        # kept under its stamp, and repeated failed boots delete nothing.
        log_file = tmp_path / "gateway.log"
        kept = ["older run\n", "boot 2 back\n", "boot 3 back\n", "boot 4 back\n"]
        _keep(log_file, kept)
        log_file.write_text("held run\n", encoding="utf-8")
        _held(monkeypatch, "gateway.log")
        for _ in range(4):
            rotation_error, _errors = _rotate(log_file)
            assert str(rotation_error) == "[Errno 13] held by a reader"
        assert log_file.read_text(encoding="utf-8") == "held run\n"
        assert _boot_logs(log_file) == kept

    def test_recreated_live_log_is_never_written_over(self, tmp_path, monkeypatch):
        # Another CLI recreates gateway.log while boot rotates and the move fails:
        # nothing is put back over it, so its records stay.
        log_file = tmp_path / "gateway.log"
        _keep(log_file, ["older run\n"])
        log_file.write_text("last run\n", encoding="utf-8")
        real_rename = Path.rename

        def _rename(self, target):
            if self.name == "gateway.log":
                log_file.write_text("other process record\n", encoding="utf-8")
                raise PermissionError(errno.EACCES, "held by another process")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", _rename)
        rotation_error, _errors = _rotate(log_file)
        assert isinstance(rotation_error, PermissionError)
        assert log_file.read_text(encoding="utf-8") == "other process record\n"
        assert _boot_logs(log_file) == ["older run\n"]

    def test_refused_start_leaves_the_running_log_untouched(self, monkeypatch):
        # A second gateway start the gateway lock refuses has already set up
        # logging: the running gateway's log stays the same file, in place, every
        # kept boot stays as it was, and the refused start's lines land in it.
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", MagicMock())
        log_file = config_dir() / "gateway.log"
        kept = ["older run\n", "boot 2 back\n", "boot 3 back\n", "boot 4 back\n"]
        _keep(log_file, kept)
        log_file.write_text("running gateway\n", encoding="utf-8")
        before = log_file.stat()
        _setup_cli_logging("gateway", 1)  # the lock refuses: no rotation follows
        logging.getLogger("kiro_crew.test_boot_gen").warning("refused start's record")
        _stop_log_queue_listener()
        after = log_file.stat()
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        text = log_file.read_text(encoding="utf-8")
        assert text.startswith("running gateway\n") and "refused start's record" in text
        assert _boot_logs(log_file) == kept

    def test_five_refused_starts_never_move_or_delete_the_live_log(self, monkeypatch):
        # The running gateway keeps one descriptor open on gateway.log, which is
        # already past the 2 MiB size rollover. Five stray starts the lock refuses
        # neither rename it (boot or size rotation) nor delete it, and nothing
        # kept is pruned.
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", MagicMock())
        log_file = config_dir() / "gateway.log"
        kept = ["older run\n", "boot 2 back\n", "boot 3 back\n", "boot 4 back\n"]
        _keep(log_file, kept)
        log_file.write_bytes(b"x" * (2 * 1024 * 1024) + b"\n")
        before = log_file.stat()
        writer = os.open(str(log_file), os.O_WRONLY | os.O_APPEND)
        try:
            for start in range(5):
                _setup_cli_logging("gateway", 1)
                logging.getLogger("kiro_crew.test_boot_gen").warning("stray start %d", start)
                _stop_log_queue_listener()
            os.write(writer, b"running gateway still writing\n")
        finally:
            os.close(writer)
        after = log_file.stat()
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        text = log_file.read_text(encoding="utf-8")
        assert text.endswith("running gateway still writing\n")
        assert all(f"stray start {n}" in text for n in range(5))
        assert _boot_logs(log_file) == kept
        assert not log_file.with_name("gateway.log.1").exists()

    def test_rotation_waits_for_the_gateway_lock(self, monkeypatch):
        # The start that holds the lock rotates: the lines it wrote before the
        # lock stay at the end of .prev, and what it writes after starts the
        # fresh gateway.log.
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", MagicMock())
        log_file = config_dir() / "gateway.log"
        log_file.write_text("last run\n", encoding="utf-8")
        _setup_cli_logging("gateway", 1)
        (fh,) = cli_mod._LOG_QUEUE_LISTENER.handlers
        logging.getLogger("kiro_crew.test_boot_gen").warning("before the lock")
        _stop_log_queue_listener()  # drain, so the record is on disk first
        assert not log_file.with_name("gateway.log.prev").exists()
        _start_gateway_log_after_lock()
        fh.handle(logging.LogRecord("kiro_crew.t", logging.WARNING, "", 0, "after", None, None))
        fh.close()
        prev = log_file.with_name("gateway.log.prev").read_text(encoding="utf-8")
        assert prev.startswith("last run\n") and "before the lock" in prev
        assert "after" not in prev.replace("before the lock", "")
        assert log_file.read_text(encoding="utf-8").endswith("after\n")
        # Called once per boot: a second call moves nothing.
        _start_gateway_log_after_lock()
        assert _boot_logs(log_file) == [prev]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
    def test_unlistable_archive_dir_moves_nothing(self, tmp_path):
        # A listing error other than "missing" must not read as "no boots": that
        # would reuse number 1 and replace the boot already kept there.
        log_file = tmp_path / "gateway.log"
        _keep(log_file, ["older run\n", "boot 2 back\n"])
        log_file.write_text("last run\n", encoding="utf-8")
        archive = boot_log_dir(log_file)
        archive.chmod(0o300)  # search and write, but no listing
        try:
            if os.access(archive, os.R_OK):
                pytest.skip("running as a user that ignores directory modes")
            rotation_error, errors = _rotate(log_file)
        finally:
            archive.chmod(0o700)
        assert isinstance(rotation_error, PermissionError)
        assert errors == []
        assert log_file.read_text(encoding="utf-8") == "last run\n"
        assert _boot_logs(log_file) == ["older run\n", "boot 2 back\n"]
        assert older_boot_logs(log_file) == [(1, archive / "gateway.log.1")]

    def test_filesystem_without_hard_links_rotates(self, tmp_path, monkeypatch):
        def _no_links(*args, **kwargs):
            raise PermissionError(errno.EPERM, "hard links not supported")

        monkeypatch.setattr(os, "link", _no_links)
        log_file = tmp_path / "gateway.log"
        _keep(log_file, ["older run\n", "boot 2 back\n"])
        log_file.write_text("last run\n", encoding="utf-8")
        assert _rotate(log_file) == (None, [])
        assert _boot_logs(log_file) == ["last run\n", "older run\n", "boot 2 back\n"]

    def test_archive_number_follows_the_largest_kept(self, tmp_path):
        log_file = tmp_path / "gateway.log"
        archive = boot_log_dir(log_file)
        archive.mkdir(parents=True)
        (archive / "gateway.log.7").write_text("boot 2 back\n", encoding="utf-8")
        log_file.with_name("gateway.log.prev").write_text("older run\n", encoding="utf-8")
        log_file.write_text("last run\n", encoding="utf-8")
        assert _rotate(log_file) == (None, [])
        assert (archive / "gateway.log.8").read_text(encoding="utf-8") == "older run\n"
        assert _boot_logs(log_file) == ["last run\n", "older run\n", "boot 2 back\n"]

    def test_boot_lists_only_the_archive_dir(self, tmp_path):
        # Other names, and the data home's own files, are never boots.
        log_file = tmp_path / "gateway.log"
        (tmp_path / "gateway.log.1").write_text("size backup\n", encoding="utf-8")
        (tmp_path / "gateway.log.prev.5").write_text("x\n", encoding="utf-8")
        archive = boot_log_dir(log_file)
        archive.mkdir(parents=True)
        for name in ("gateway.log.", "gateway.log.new", "gateway.log.٣", "other.log.4"):
            (archive / name).write_text("x\n", encoding="utf-8")
        assert older_boot_logs(log_file) == []

    @requires_symlinks
    @pytest.mark.parametrize("linked", ["logs", "logs/gateway-boots"])
    def test_linked_archive_dir_is_never_followed(self, tmp_path, linked):
        home = tmp_path / "home"
        home.mkdir()
        outside = tmp_path / "outside"
        (outside / "gateway-boots").mkdir(parents=True)
        (outside / "gateway.log.1").write_text("not ours\n", encoding="utf-8")
        (outside / "gateway-boots" / "gateway.log.1").write_text("not ours\n", encoding="utf-8")
        link = home / linked
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside if linked == "logs" else outside / "gateway-boots")
        log_file = home / "gateway.log"
        log_file.with_name("gateway.log.prev").write_text("older run\n", encoding="utf-8")
        log_file.write_text("last run\n", encoding="utf-8")
        assert older_boot_logs(log_file) == []
        rotation_error, errors = _rotate(log_file)
        assert rotation_error is None
        assert [str(e).endswith("is a link; older boots not kept") for e in errors] == [True]
        # One previous boot kept, as before; nothing moved into the link target.
        assert _boot_logs(log_file) == ["last run\n"]
        assert sorted(p.name for p in outside.rglob("*")) == [
            "gateway-boots",
            "gateway.log.1",
            "gateway.log.1",
        ]

    def test_empty_log_shifts_nothing(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", MagicMock())
        log_file = config_dir() / "gateway.log"
        log_file.with_name("gateway.log.prev").write_text("kept run\n", encoding="utf-8")
        log_file.write_bytes(b"")
        _boot()
        _stop_log_queue_listener()
        assert _boot_logs(log_file) == ["kept run\n"]


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


def _big_record(msg: str = "x" * 100) -> logging.LogRecord:
    return logging.LogRecord("t", logging.WARNING, __file__, 1, msg, None, None)


class TestRolloverFailureStopsFileLogging:
    """When the reopen inside a rollover fails, fds 1/2 stay on the RENAMED
    file. A traceback per record there escapes the 2 MB cap and fills the
    disk, so an OSError streak prints one traceback and, for a non-retryable
    errno or a long streak, file logging stops."""

    @pytest.fixture
    def redirects(self, monkeypatch):
        # Record fd re-points instead of performing them: a real dup2 onto
        # fd 2 would swallow pytest's own output.
        calls: list[Path] = []
        monkeypatch.setattr(
            cli_mod, "_redirect_fds_to", lambda path, fds=(1, 2): calls.append(Path(path))
        )
        return calls

    @staticmethod
    def _deny_reopen(handler, monkeypatch, err: int) -> None:
        def _open():
            raise OSError(err, os.strerror(err), handler.baseFilename)

        monkeypatch.setattr(handler, "_open", _open)

    def test_eacces_on_reopen_stops_file_logging(self, tmp_path, monkeypatch, redirects):
        err = io.StringIO()
        monkeypatch.setattr(sys, "stderr", err)
        handler = _FdTrackingRotatingFileHandler(
            tmp_path / "gateway.log", maxBytes=64, backupCount=2, encoding="utf-8"
        )
        handler.emit(_big_record())  # fills past maxBytes: the next emit rolls
        # Some CPython 3.12 patch releases (3.12.4 here) also roll an EMPTY file
        # over on that first emit, re-pointing the fds once. Only the re-points
        # after the denied reopen are under test.
        redirects.clear()
        self._deny_reopen(handler, monkeypatch, errno.EACCES)
        for _ in range(20):
            handler.emit(_big_record())
        handler.close()
        out = err.getvalue()
        assert out.count("--- Logging error ---") == 1
        assert len([ln for ln in out.splitlines() if "file logging to" in ln]) == 1
        assert redirects == [Path(os.devnull)]

    def test_retryable_errno_stops_at_streak_limit(self, tmp_path, monkeypatch, redirects):
        err = io.StringIO()
        monkeypatch.setattr(sys, "stderr", err)
        handler = _FdTrackingRotatingFileHandler(
            tmp_path / "gateway.log", maxBytes=64, backupCount=2, encoding="utf-8"
        )
        handler.emit(_big_record())
        redirects.clear()  # see test_eacces_on_reopen_stops_file_logging
        self._deny_reopen(handler, monkeypatch, errno.ENOSPC)
        for _ in range(20):
            handler.emit(_big_record())
        handler.close()
        out = err.getvalue()
        assert out.count("--- Logging error ---") == 1
        assert len([ln for ln in out.splitlines() if "file logging to" in ln]) == 1
        assert redirects == [Path(os.devnull)]

    def test_transient_error_below_limit_keeps_logging(self, tmp_path, monkeypatch, redirects):
        err = io.StringIO()
        monkeypatch.setattr(sys, "stderr", err)
        log = tmp_path / "gateway.log"
        handler = _FdTrackingRotatingFileHandler(log, maxBytes=0, backupCount=2, encoding="utf-8")
        failing = {"on": False}
        real = handler.shouldRollover

        def _maybe_fail(record):
            if failing["on"]:
                raise OSError(errno.ENOENT, "transient", handler.baseFilename)
            return real(record)

        monkeypatch.setattr(handler, "shouldRollover", _maybe_fail)
        # Two bursts of two failures each: four in all, past the limit of 3
        # unless the success between the bursts resets the streak.
        for burst in range(2):
            failing["on"] = True
            for _ in range(2):
                handler.emit(_big_record("fail"))
            failing["on"] = False
            handler.emit(_big_record(f"ok-{burst}"))
        handler.close()
        text = log.read_text(encoding="utf-8")
        assert "ok-0" in text and "ok-1" in text
        assert err.getvalue().count("--- Logging error ---") == 2  # one per streak
        assert redirects == []

    def test_write_error_on_open_file_never_stops(self, tmp_path, monkeypatch, redirects):
        # A disk-full write to the live, still-rotated file may clear by
        # itself: suppress the repeat tracebacks but keep the handler alive.
        err = io.StringIO()
        monkeypatch.setattr(sys, "stderr", err)
        log = tmp_path / "gateway.log"
        handler = _FdTrackingRotatingFileHandler(log, maxBytes=0, backupCount=2, encoding="utf-8")
        real = handler.stream
        failing = {"on": True}

        class _FullDisk:
            def write(self, text):
                if failing["on"]:
                    raise OSError(errno.ENOSPC, "full", handler.baseFilename)
                return real.write(text)

            def __getattr__(self, name):
                return getattr(real, name)

        handler.stream = _FullDisk()
        for _ in range(10):
            handler.emit(_big_record("fail"))
        failing["on"] = False
        handler.emit(_big_record("back"))
        handler.close()
        assert "back" in log.read_text(encoding="utf-8")
        assert err.getvalue().count("--- Logging error ---") == 1
        assert redirects == []

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows cannot rename a file another handle holds open, so "
        "stderr cannot follow the renamed inode there.",
    )
    def test_renamed_backup_does_not_grow_after_failure(self, tmp_path, monkeypatch, redirects):
        log = tmp_path / "gateway.log"
        handler = _FdTrackingRotatingFileHandler(log, maxBytes=64, backupCount=2, encoding="utf-8")
        # Stands in for fd 2 after the detached redirect: it follows the inode
        # through the rename, exactly as raw stderr does.
        stderr_file = open(log, "a", encoding="utf-8")
        try:
            monkeypatch.setattr(sys, "stderr", stderr_file)
            handler.emit(_big_record())
            self._deny_reopen(handler, monkeypatch, errno.EACCES)
            handler.emit(_big_record())  # renames gateway.log -> .1, reopen fails
            stderr_file.flush()
            backup = tmp_path / "gateway.log.1"
            size_after_failure = backup.stat().st_size
            for _ in range(50):
                handler.emit(_big_record())
            stderr_file.flush()
            assert backup.stat().st_size == size_after_failure
        finally:
            monkeypatch.undo()
            stderr_file.close()
            handler.close()


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
            h for h in logging.getLogger("kiro_crew").handlers if isinstance(h, _CliLogQueueHandler)
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
        # Nothing moves before the gateway lock is held, so the fds stay put.
        assert not (config_dir() / "gateway.log.prev").exists()
        self.redirect.assert_not_called()
        _start_gateway_log_after_lock()
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

    def test_unwritable_gateway_log_fails_loudly(self, monkeypatch):
        """A DETACHED process must not soft-fail an unopenable ``gateway.log``.

        Detached setup has no console handler, so swallowing the OSError would leave the
        gateway with no log and nowhere to warn. The foreground path soft-fails instead.
        """

        def _deny(*_a, **_k):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr("kiro_crew.cli._OwnerOnlyRotatingFileHandler", _deny)
        monkeypatch.setattr("kiro_crew.cli._FdTrackingRotatingFileHandler", _deny)
        with pytest.raises(PermissionError):
            _setup_cli_logging("gateway", 1)


class TestFdIsRegularFile:
    def test_true_for_a_regular_file(self, tmp_path):
        with open(tmp_path / "gateway.err", "w") as fh:
            assert _real_fd_is_regular_file(fh.fileno()) is True

    def test_false_for_a_pipe(self):
        r, w = os.pipe()
        try:
            assert _real_fd_is_regular_file(w) is False
        finally:
            os.close(r)
            os.close(w)

    def test_false_for_a_closed_fd(self):
        r, w = os.pipe()
        os.close(r)
        os.close(w)
        assert _real_fd_is_regular_file(w) is False


class TestSetupCliLoggingServiceErrorFile:
    """A gateway whose stderr is a file nothing rotates (launchd's
    ``gateway.err``, the Desktop launch log) echoes only warnings and errors
    there; ``gateway.log`` keeps every record at the configured level."""

    @pytest.fixture(autouse=True)
    def _service_stderr(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        _floor_monkeypatch.setattr("kiro_crew.cli._fd_is_regular_file", lambda fd: True)
        _floor_monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", MagicMock())
        # pytest's own capture handler on root turns the real basicConfig into
        # a no-op, so stand in for it with the console handler it would add.
        self.console = io.StringIO()

        def _basic_config(**_kwargs):
            logging.getLogger().addHandler(logging.StreamHandler(self.console))

        _floor_monkeypatch.setattr(logging, "basicConfig", _basic_config)

    def test_gateway_echoes_only_warnings_to_the_stderr_file(self):
        _setup_cli_logging("gateway", 1)
        logging.getLogger("kiro_crew.test_service").info("kc-info-record")
        logging.getLogger("kiro_crew.test_service").warning("kc-warn-record")
        _stop_log_queue_listener()
        assert "kc-info-record" not in self.console.getvalue()
        assert "kc-warn-record" in self.console.getvalue()
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert text.count("kc-info-record") == 1
        assert text.count("kc-warn-record") == 1

    def test_other_commands_keep_echoing_every_level(self):
        _setup_cli_logging("status", 1)
        logging.getLogger("kiro_crew.test_service").info("kc-info-record")
        assert "kc-info-record" in self.console.getvalue()


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
            h for h in logging.getLogger("kiro_crew").handlers if isinstance(h, _CliLogQueueHandler)
        ]
        assert len(kc_qhs) == 1
        # The level lives on the kiro_crew logger alone; the queue handler and
        # the file handler carry none (a copy here is one a runtime change
        # does not reach).
        assert kc_qhs[0].level == logging.NOTSET
        assert logging.getLogger("kiro_crew").level == logging.INFO
        assert not any(isinstance(h, _CliLogQueueHandler) for h in logging.getLogger().handlers)
        # No inline file handler on either logger.
        for logger in (logging.getLogger(), logging.getLogger("kiro_crew")):
            assert not any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
        (fh,) = cli_mod._LOG_QUEUE_LISTENER.handlers
        # Foreground keeps the plain (owner-only) handler: no fds were redirected,
        # so there is nothing to re-point on rollover.
        assert type(fh) is cli_mod._OwnerOnlyRotatingFileHandler
        assert fh.level == logging.NOTSET

    def test_record_written_once_to_file(self):
        _setup_cli_logging("gateway", 1)
        logging.getLogger("kiro_crew.test_foreground").warning("fg-sentinel")
        _stop_log_queue_listener()  # deterministic drain to disk
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert text.count("fg-sentinel") == 1

    def test_std_fds_never_repointed(self):
        (config_dir() / "gateway.log").write_text("previous boot\n", encoding="utf-8")
        _boot()
        # Rotation still happens (crash-line preservation) …
        assert (config_dir() / "gateway.log.prev").exists()
        # … but a foreground console must never be dup2'd into the log file.
        self.redirect.assert_not_called()

    def test_unwritable_gateway_log_soft_fails(self, monkeypatch):
        """Seatbelt may deny ``gateway.log``; a CONSOLE-backed setup must not raise.

        Sandboxed MCP children (e.g. ``kirocrew mcp-core``) cannot open the log path;
        the console handler takes the warning and the MCP handshake must still start.
        """

        def _deny(*_a, **_k):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr("kiro_crew.cli._AppendOnlyLogFileHandler", _deny)
        _setup_cli_logging("mcp-core", 0)  # must not raise
        assert cli_mod._LOG_QUEUE_LISTENER is None
        assert not any(
            isinstance(h, logging.FileHandler) for h in logging.getLogger("kiro_crew").handlers
        )

    def test_unwritable_gateway_log_still_redacts_for_long_lived(self, monkeypatch):
        """OSError on gateway.log must not skip Bearer/JWT console redaction."""

        def _deny(*_a, **_k):
            raise PermissionError(1, "Operation not permitted")

        redaction = MagicMock()
        monkeypatch.setattr("kiro_crew.cli._OwnerOnlyRotatingFileHandler", _deny)
        monkeypatch.setattr("kiro_crew.cli._FdTrackingRotatingFileHandler", _deny)
        monkeypatch.setattr("kiro_crew.cli.install_log_redaction", redaction)
        _setup_cli_logging("gateway", 1)  # must not raise
        redaction.assert_called_once_with([])
        assert cli_mod._LOG_QUEUE_LISTENER is None
        assert not any(
            isinstance(h, logging.FileHandler) for h in logging.getLogger("kiro_crew").handlers
        )


class TestRuntimeLevelReachesFileLog:
    """A runtime ``agent.log_level`` change reaches ``gateway.log`` without a restart.

    ``apply_log_level`` moves the ``kiro_crew`` logger, the single level gate, so
    neither the file handler nor the queue handler may carry a boot-time level.
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
            h for h in logging.getLogger("kiro_crew").handlers if isinstance(h, _CliLogQueueHandler)
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
        sync_fhs = [h for h in kc.handlers if isinstance(h, logging.FileHandler)]
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

        reached: list[bool] = []

        def exploding_stop(timeout=None):
            reached.append(True)
            raise RuntimeError("interpreter teardown race")

        cli_mod._stop_log_queue_listener = exploding_stop  # type: ignore[assignment]
        try:
            await drain_log_queue_before_hard_exit()
        finally:
            cli_mod._stop_log_queue_listener = real_stop  # type: ignore[assignment]
        assert reached == [True], "the drain never reached the raising stop"


class TestEveryGatewayHardExitDrainsTheQueue:
    """Ratchet: every ``os._exit`` in the gateway process drains the log queue.

    Only ``slack/gateway_runtime/exit_path.py`` and ``slack/events_slash.py`` qualify;
    other sites run in forked children with no queue listener. The check is
    per-function, so a drain in a sibling closure cannot vouch for its parent."""

    _MODULES = (
        Path(__file__).resolve().parents[1] / "src/kiro_crew/slack/gateway.py",
        *sorted(
            (Path(__file__).resolve().parents[1] / "src/kiro_crew/slack/gateway_runtime").glob(
                "*.py"
            )
        ),
        *sorted((Path(__file__).resolve().parents[1] / "src/kiro_crew/slack").glob("events*.py")),
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
        """(function node, line) for each ``os._exit(...)`` or
        ``platform_compat.hard_exit(...)`` call (the spelling that cancels an
        update apply in flight first, then ``os._exit``), attributed to the nearest
        enclosing function."""
        parents: "dict[ast.AST, ast.AST]" = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        found = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and (
                    (node.func.attr == "_exit" and node.func.value.id == "os")
                    or (node.func.attr == "hard_exit" and node.func.value.id == "platform_compat")
                )
            ):
                continue
            cur = parents.get(node)
            while cur is not None and not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cur = parents.get(cur)
            if cur is not None:
                found.append((cur, node.lineno))
        return found

    def test_the_ratchet_actually_finds_the_hard_exits(self):
        """A scan that matches nothing would pass vacuously."""
        total = 0
        for path in self._MODULES:
            total += len(self._hard_exit_functions(ast.parse(path.read_text(encoding="utf-8"))))
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
