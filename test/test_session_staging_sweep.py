"""The sessions directory sheds staging temps a killed writer left behind.

A writer stages a transcript as ``tmp<8>.tmp`` or ``.kc-incoming-<8>`` next to its
target and renames it into place. A process killed between the two leaves the
staging file, and nothing in the writer can remove it afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import session_staging_sweep as sweep

_NOW = 2_000_000_000.0
_OLD = _NOW - sweep.STAGING_TEMP_MIN_AGE_SECS - 1
_FRESH = _NOW - 5


def _file(directory: Path, name: str, mtime: float, data: str = "x") -> Path:
    path = directory / name
    path.write_text(data)
    os.utime(path, (mtime, mtime))
    return path


def test_an_old_default_prefix_temp_is_removed(tmp_path: Path) -> None:
    orphan = _file(tmp_path, "tmpab12_x9z.tmp", _OLD)

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 1
    assert not orphan.exists()


def test_an_old_incoming_temp_is_removed(tmp_path: Path) -> None:
    orphan = _file(tmp_path, ".kc-incoming-q7w3e9r1", _OLD)

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 1
    assert not orphan.exists()


def test_the_names_match_what_mkstemp_produces(tmp_path: Path) -> None:
    """The shapes are mkstemp's own output, not a guess about it."""
    for kwargs in ({"suffix": ".tmp"}, {"prefix": ".kc-incoming-"}):
        fd, name = tempfile.mkstemp(dir=tmp_path, **kwargs)
        os.close(fd)
        assert sweep.is_staging_temp_name(os.path.basename(name)), name


def test_a_fresh_temp_is_kept(tmp_path: Path) -> None:
    """A temp younger than the age gate may belong to a write in flight."""
    live = _file(tmp_path, "tmpab12_x9z.tmp", _FRESH)
    incoming = _file(tmp_path, ".kc-incoming-q7w3e9r1", _FRESH)

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert live.exists() and incoming.exists()


@pytest.mark.parametrize(
    "name",
    [
        "dashboard_chat-abc.jsonl",
        "dashboard_chat-abc.jsonl.lock",
        "subagent_1.jsonl.lock",
        "tmpab12_x9z.jsonl",
        "tmpab12.tmp",
        "tmpab12_x9z0.tmp",
        "mytmpab12_x9z.tmp",
        ".kc-incoming-",
        ".kc-incoming-q7w3e9r1.jsonl",
        "notes.tmp",
    ],
)
def test_other_names_are_never_touched(tmp_path: Path, name: str) -> None:
    kept = _file(tmp_path, name, _OLD)

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert kept.exists()


def test_a_directory_with_a_staging_name_is_kept(tmp_path: Path) -> None:
    folder = tmp_path / "tmpab12_x9z.tmp"
    folder.mkdir()
    os.utime(folder, (_OLD, _OLD))

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert folder.is_dir()


@requires_symlinks
def test_a_link_with_a_staging_name_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = _file(outside, "keep.txt", _OLD)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "tmpab12_x9z.tmp").symlink_to(target)

    assert sweep.sweep_session_staging_temps(sessions, now=_NOW) == 0
    assert target.exists()
    assert os.path.lexists(sessions / "tmpab12_x9z.tmp")


def test_only_direct_children_are_examined(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    nested = _file(archive, "tmpab12_x9z.tmp", _OLD)

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert nested.exists()


def test_a_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert sweep.sweep_session_staging_temps(tmp_path / "absent", now=_NOW) == 0


def test_the_default_directory_is_the_data_home_sessions_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "data_home", lambda: tmp_path)
    sessions = tmp_path / sweep.SESSIONS_DIR_NAME
    sessions.mkdir()
    orphan = _file(sessions, "tmpab12_x9z.tmp", time.time() - sweep.STAGING_TEMP_MIN_AGE_SECS - 60)

    assert sweep.sweep_session_staging_temps() == 1
    assert not orphan.exists()


def test_the_cleanup_tick_runs_the_sweep_off_the_event_loop() -> None:
    """The janitor's pass calls the dependency on its maintenance executor."""
    from types import SimpleNamespace

    from kiro_crew.session_cleanup import SessionCleanup

    calls: list[str] = []
    loop_thread: list[int] = []

    def fake_sweep() -> int:
        import threading

        calls.append("swept")
        loop_thread.append(threading.get_ident())
        return 2

    pool = ThreadPoolExecutor(max_workers=1)
    deps = SimpleNamespace(
        get_maintenance_executor=lambda: pool,
        sweep_session_staging_temps=fake_sweep,
        logger=logging.getLogger("test.staging_sweep"),
    )
    service = SimpleNamespace(_deps=deps)

    async def run() -> int:
        import threading

        await SessionCleanup._sweep_session_staging_temps(service)  # type: ignore[arg-type]
        return threading.get_ident()

    try:
        main_thread = asyncio.run(run())
    finally:
        pool.shutdown(wait=True)

    assert calls == ["swept"]
    assert loop_thread[0] != main_thread


def test_a_failing_sweep_does_not_break_the_tick() -> None:
    from types import SimpleNamespace

    from kiro_crew.session_cleanup import SessionCleanup

    def boom() -> int:
        raise OSError("disk gone")

    pool = ThreadPoolExecutor(max_workers=1)
    deps = SimpleNamespace(
        get_maintenance_executor=lambda: pool,
        sweep_session_staging_temps=boom,
        logger=logging.getLogger("test.staging_sweep"),
    )
    try:
        asyncio.run(
            SessionCleanup._sweep_session_staging_temps(SimpleNamespace(_deps=deps))  # type: ignore[arg-type]
        )
    finally:
        pool.shutdown(wait=True)
