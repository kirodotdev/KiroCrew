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
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.cli import (
    _fd_targets_file,
    _FdTrackingRotatingFileHandler,
    _redirect_fds_to,
    _setup_cli_logging,
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
    tmpdir cleanup).
    """
    root = logging.getLogger()
    kc = logging.getLogger("kiro_crew")
    saved_root = (root.handlers[:], root.level)
    saved_kc = (kc.handlers[:], kc.level)
    root.handlers[:] = []
    kc.handlers[:] = []
    yield
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

    def test_file_handler_on_root_not_kiro_crew(self):
        _setup_cli_logging("gateway", 1)
        root_fhs = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
        assert len(root_fhs) == 1
        assert Path(root_fhs[0].baseFilename) == config_dir() / "gateway.log"
        kc_fhs = [
            h
            for h in logging.getLogger("kiro_crew").handlers
            if isinstance(h, RotatingFileHandler)
        ]
        assert kc_fhs == []

    def test_kiro_crew_record_written_exactly_once(self):
        _setup_cli_logging("gateway", 1)
        logging.getLogger("kiro_crew.test_doublewrite").warning("sentinel-record")
        for h in logging.getLogger().handlers:
            h.flush()
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert text.count("sentinel-record") == 1
        # And it is the PID-stamped file-handler copy, not the console format.
        assert "[PID" in text

    def test_third_party_warning_still_lands_in_file(self):
        # Before the fix these reached the file only via the accidental
        # stderr echo; the root-attached handler must keep them flowing.
        _setup_cli_logging("gateway", 1)
        logging.getLogger("somelib.test_doublewrite").warning("thirdparty-record")
        for h in logging.getLogger().handlers:
            h.flush()
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

    def test_handler_level_capped_at_warning_for_third_party(self, monkeypatch):
        """A stricter persisted kiro_crew level must not gag third-party
        WARNINGs on the shared root handler (kiro_crew records stay filtered
        at the kiro_crew logger itself)."""
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        cfg.agent.log_level = "ERROR"
        monkeypatch.setattr("kiro_crew.cli.KiroCrewConfig.load", staticmethod(lambda: cfg))
        _setup_cli_logging("gateway", 0)
        fh = next(h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler))
        assert fh.level == logging.WARNING
        assert logging.getLogger("kiro_crew").level == logging.ERROR


class TestSetupCliLoggingForeground:
    """The classic topology must be unchanged when stderr is a real console."""

    @pytest.fixture(autouse=True)
    def _foreground(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        self.redirect = MagicMock()
        monkeypatch.setattr("kiro_crew.cli._redirect_fds_to", self.redirect)

    def test_file_handler_on_kiro_crew_logger(self):
        _setup_cli_logging("gateway", 1)
        kc_fhs = [
            h
            for h in logging.getLogger("kiro_crew").handlers
            if isinstance(h, RotatingFileHandler)
        ]
        assert len(kc_fhs) == 1
        assert kc_fhs[0].level == logging.INFO
        root_fhs = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
        assert root_fhs == []

    def test_record_written_once_to_file(self):
        _setup_cli_logging("gateway", 1)
        logging.getLogger("kiro_crew.test_foreground").warning("fg-sentinel")
        for h in logging.getLogger("kiro_crew").handlers:
            h.flush()
        text = (config_dir() / "gateway.log").read_text(encoding="utf-8")
        assert text.count("fg-sentinel") == 1

    def test_std_fds_never_repointed(self):
        (config_dir() / "gateway.log").write_text("previous boot\n", encoding="utf-8")
        _setup_cli_logging("gateway", 1)
        # Rotation still happens (crash-line preservation) …
        assert (config_dir() / "gateway.log.prev").exists()
        # … but a foreground console must never be dup2'd into the log file.
        self.redirect.assert_not_called()
