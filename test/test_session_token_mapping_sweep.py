"""The periodic cleanup tick bounds the ``session_token_*.sig`` mappings.

Every started session publishes one signed identity mapping in the data home
(``session_token_sig``). The age-based prune that removes them is also reached
from ``cleanup_orphaned_sessions`` at startup and shutdown; the tick is what
bounds the directory on a gateway that keeps running. These tests pin the real
wiring from ``SessionManager`` and the step's fail-open contract; the tick call
site is pinned in ``test_session_config_hot_reload.py`` beside its siblings.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from kiro_crew.session_pid_mappings import _SESSION_TOKEN_TTL_SECS


def test_the_real_wiring_prunes_an_aged_mapping_and_keeps_a_fresh_one() -> None:
    """``SessionManager._cleanup_deps`` forwards to the shipped prune and TTL.

    Drives the callable the real deps carry against the isolated
    ``KIROCREW_HOME`` the suite pins, so the directory, the name pattern and the
    TTL are the shipped ones end to end.
    """
    from kiro_crew.config.paths import config_dir
    from kiro_crew.session import SessionManager

    home = config_dir()
    aged = home / ("session_token_" + "a" * 64 + ".sig")
    fresh = home / ("session_token_" + "b" * 64 + ".sig")
    aged.write_text("aged")
    fresh.write_text("fresh")
    past = time.time() - _SESSION_TOKEN_TTL_SECS - 60
    os.utime(aged, (past, past))
    try:
        deps = SessionManager._cleanup_deps(cast(Any, SimpleNamespace()))
        assert deps.prune_session_token_mappings() >= 1
        assert not aged.exists(), "a mapping past the TTL must be reclaimed by the tick's prune"
        assert fresh.read_text() == "fresh", "a mapping inside the TTL must be kept"
    finally:
        aged.unlink(missing_ok=True)
        fresh.unlink(missing_ok=True)


def test_a_failing_prune_does_not_break_the_tick(caplog: pytest.LogCaptureFixture) -> None:
    """The step is fail-open like its siblings: a raise is logged, not propagated."""
    from kiro_crew.session_cleanup import SessionCleanup

    def boom() -> int:
        raise OSError("disk gone")

    pool = ThreadPoolExecutor(max_workers=1)
    deps = SimpleNamespace(
        get_maintenance_executor=lambda: pool,
        prune_session_token_mappings=boom,
        logger=logging.getLogger("test.session_token_sweep"),
    )
    try:
        with caplog.at_level(logging.DEBUG, logger="test.session_token_sweep"):
            asyncio.run(
                SessionCleanup._sweep_session_token_mappings(cast(Any, SimpleNamespace(_deps=deps)))
            )
    finally:
        pool.shutdown(wait=True)
    own = [r.getMessage() for r in caplog.records if r.name == "test.session_token_sweep"]
    assert own == ["session token mapping sweep failed: OSError"]


def test_the_prune_reads_age_and_unlinks_under_the_publication_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A republish while the prune waits on the lock is seen as fresh and kept.

    The lock is held here while the prune starts, as a publication would hold it
    around its write. The aged file is republished before the lock is released,
    so a prune that read the age outside the lock would delete the new mapping.
    """
    from kiro_crew import session_pid, session_token_sig
    from kiro_crew.testing.wait import wait_until

    real = threading.Lock()
    reached = threading.Event()

    class _Gate:
        """The publication lock, reporting when the prune starts waiting on it."""

        def __enter__(self) -> None:
            if threading.current_thread() is not threading.main_thread():
                reached.set()
            real.acquire()

        def __exit__(self, *exc: object) -> None:
            real.release()

    gate = _Gate()
    monkeypatch.setattr(session_token_sig, "MAPPING_WRITE_LOCK", gate)
    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    path = tmp_path / ("session_token_" + "c" * 64 + ".sig")
    path.write_text("aged")
    past = time.time() - _SESSION_TOKEN_TTL_SECS - 60
    os.utime(path, (past, past))
    removed: list[int] = []

    with gate:
        worker = threading.Thread(
            target=lambda: removed.append(session_pid._prune_stale_session_token_files())
        )
        worker.start()
        wait_until(reached.is_set, describe=lambda: "the prune never took the publication lock")
        assert path.exists(), "the prune removed a mapping while publication held the lock"
        path.write_text("republished")
        os.utime(path, None)
    worker.join(timeout=10)

    assert removed == [0]
    assert path.read_text() == "republished"


def test_publication_writes_under_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import session_token_sig

    held: list[bool] = []

    def write(path: Any, content: str, **_kw: Any) -> None:
        held.append(session_token_sig.MAPPING_WRITE_LOCK.locked())

    monkeypatch.setattr(session_token_sig, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(session_token_sig, "_load_hmac_key", lambda: b"k" * 32)
    monkeypatch.setattr(session_token_sig, "atomic_write", write)

    session_token_sig.publish_session_token("tok", "dashboard:abc")

    assert held == [True]
