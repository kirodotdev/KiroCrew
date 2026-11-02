"""The crash backfill keeps a crumb it cannot read instead of reaping it as malformed.

A present crumb whose read fails (a Windows sharing violation, an ACL) has an
owner nobody can judge, so the fail-closed rule on ``_owner_still_running``
keeps it for a later boot.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.metrics import sessions as sess


@pytest.fixture(autouse=True)
def clean_live_registry() -> Iterator[None]:
    with sess._live_lock:
        sess._live_starts.clear()
    yield
    with sess._live_lock:
        sess._live_starts.clear()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    return tmp_path


def _crumbs(home: Path) -> list[Path]:
    d = home / "metrics" / "open-sessions"
    return sorted(d.glob("*.json")) if d.is_dir() else []


def _live_sibling_crumb(home: Path) -> Path:
    """A crumb from the production writer, owned by a running process (this one)."""
    key, started_at = "dashboard:chat-1", time.time() - 7200
    with sess._live_lock:
        sess._live_starts[key] = started_at
    sess._write_crumb(key, started_at)
    with sess._live_lock:
        sess._live_starts.pop(key, None)
    crumb = _crumbs(home)[0]
    payload = json.loads(crumb.read_text(encoding="utf-8"))
    payload["pid"] = os.getpid()
    payload.pop("start_id", None)
    crumb.write_text(json.dumps(payload), encoding="utf-8")
    return crumb


@contextmanager
def _unreadable(path: Path) -> Iterator[None]:
    """Reads of *path* raise the PermissionError a Windows sharing violation does."""
    real_text, real_bytes = Path.read_text, Path.read_bytes

    def _refuse(target: Path) -> None:
        if target == path:
            raise PermissionError(13, "being used by another process", str(target))

    def text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        _refuse(self)
        return real_text(self, *args, **kwargs)

    def raw(self: Path):  # type: ignore[no-untyped-def]
        _refuse(self)
        return real_bytes(self)

    with patch.object(Path, "read_text", text), patch.object(Path, "read_bytes", raw):
        yield


def test_an_unreadable_live_crumb_is_kept(home: Path, caplog: pytest.LogCaptureFixture) -> None:
    crumb = _live_sibling_crumb(home)
    with (
        _unreadable(crumb),
        caplog.at_level(logging.DEBUG, logger="kiro_crew.metrics.sessions"),
    ):
        assert sess.backfill_crashed_sessions() == 0
    assert crumb.exists(), "an owner nobody can judge counts as running"
    assert any("PermissionError" in r.getMessage() for r in caplog.records)


def test_a_malformed_crumb_is_still_reaped(home: Path) -> None:
    crumb = _live_sibling_crumb(home)
    crumb.write_text("not json at all", encoding="utf-8")
    assert sess.backfill_crashed_sessions() == 0
    assert not crumb.exists()
