"""A failed ``gateway.log`` rollover keeps every later record.

On Windows a rename fails with ``WinError 32`` while another process holds the
file open. CPython's ``doRollover`` closes the stream before renaming, so without
handling that failure the record is lost and every later record retries the same
rename and is lost too. The rename failure is injected through ``os.rename``, the
seam ``RotatingFileHandler.rotate`` uses, so this runs on every platform.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from kiro_crew import cli_log_files
from kiro_crew.cli_log_files import _OwnerOnlyRotatingFileHandler
from kiro_crew.cli_logging import _FdTrackingRotatingFileHandler


def _sharing_violation(src, dst):
    raise PermissionError(13, "The process cannot access the file", str(src))


def _logger(handler: logging.Handler, name: str) -> logging.Logger:
    log = logging.getLogger(name)
    log.propagate = False
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    return log


def _kept(directory: Path) -> int:
    return sum(
        1
        for path in directory.iterdir()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("record ")
    )


@pytest.mark.parametrize("handler_cls", [_OwnerOnlyRotatingFileHandler])
def test_no_record_is_dropped_while_the_rename_keeps_failing(
    tmp_path: Path, monkeypatch, capsys, handler_cls
) -> None:
    handler = handler_cls(tmp_path / "gateway.log", maxBytes=512, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = _logger(handler, "test.rollover.failing")
    monkeypatch.setattr(os, "rename", _sharing_violation)
    try:
        for i in range(60):
            log.warning("record %03d %s", i, "x" * 40)
    finally:
        handler.close()

    assert _kept(tmp_path) == 60
    assert "--- Logging error ---" not in capsys.readouterr().err
    text = (tmp_path / "gateway.log").read_text(encoding="utf-8")
    assert text.count("log rollover of") == 1, "the failure is reported once, not per record"


def test_a_failed_rollover_waits_before_trying_again(tmp_path: Path, monkeypatch) -> None:
    handler = _OwnerOnlyRotatingFileHandler(
        tmp_path / "gateway.log", maxBytes=256, backupCount=2, encoding="utf-8"
    )
    log = _logger(handler, "test.rollover.backoff")
    attempts: list[str] = []

    def _counting(src, dst):
        attempts.append(str(src))
        _sharing_violation(src, dst)

    monkeypatch.setattr(os, "rename", _counting)
    try:
        for i in range(40):
            log.warning("record %03d %s", i, "y" * 40)
    finally:
        handler.close()

    # One failed pass (the backup renames are skipped while no backup exists; the
    # live file's rename is the one attempt), then no more inside the wait window.
    assert len(attempts) == 1


def test_the_rollover_resumes_once_the_rename_works(tmp_path: Path, monkeypatch) -> None:
    # Enough backups that ordinary retention keeps every record this test writes.
    handler = _OwnerOnlyRotatingFileHandler(
        tmp_path / "gateway.log", maxBytes=256, backupCount=20, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = _logger(handler, "test.rollover.resume")
    real_rename = os.rename
    monkeypatch.setattr(os, "rename", _sharing_violation)
    try:
        for i in range(10):
            log.warning("record %03d %s", i, "z" * 40)
        monkeypatch.setattr(os, "rename", real_rename)
        monkeypatch.setattr(cli_log_files, "_ROLLOVER_RETRY_SECS", 0.0)
        handler._rollover_retry_at = 0.0
        for i in range(10, 20):
            log.warning("record %03d %s", i, "z" * 40)
    finally:
        handler.close()

    assert (tmp_path / "gateway.log.1").exists()
    assert _kept(tmp_path) == 20
    assert handler._rollover_failure_reported is False


def test_the_detached_handler_keeps_records_too(tmp_path: Path, monkeypatch) -> None:
    """The detached gateway's fd-tracking subclass inherits the same rollover."""
    monkeypatch.setattr("kiro_crew.cli_logging._redirect_fds_to", lambda path, fds=(1, 2): None)
    handler = _FdTrackingRotatingFileHandler(
        tmp_path / "gateway.log", maxBytes=512, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = _logger(handler, "test.rollover.detached")
    monkeypatch.setattr(os, "rename", _sharing_violation)
    try:
        for i in range(60):
            log.warning("record %03d %s", i, "x" * 40)
    finally:
        handler.close()

    assert _kept(tmp_path) == 60
    assert handler._stopped is False
