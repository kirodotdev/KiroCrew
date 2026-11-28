"""A stale Chromium ``SingletonLock`` is cleared before a POSIX launch.

Chromium writes the lock as a symlink whose target is ``<host>-<pid>``. When the
host it names is not this one, Chromium refuses the profile (exit code 21) without
asking whether that pid is alive, so a machine whose host name changes on restart
keeps its browser down until a person removes the lock. The supervisor clears a
lock from another host, or one whose pid is gone on this host, and leaves a lock a
live local process holds.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.browser_harness import earlier_chromium, supervisor
from kiro_crew.testing.wait import finished

_HOST = "this-host"
_SINGLETONS = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _profile_with_lock(tmp_path: Path, target: str) -> Path:
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to(target)
    (profile / "SingletonSocket").symlink_to(tmp_path / "gone" / "SingletonSocket")
    (profile / "SingletonCookie").symlink_to("1234567890")
    return profile


def _singletons_left(profile: Path) -> list[str]:
    return [name for name in _SINGLETONS if (profile / name).is_symlink()]


@pytest.fixture
def this_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: _HOST)


def _pids_alive(monkeypatch: pytest.MonkeyPatch, *alive: int) -> None:
    monkeypatch.setattr(earlier_chromium.platform_compat, "pid_exists", lambda pid: pid in alive)


@requires_symlinks
def test_a_lock_from_another_host_is_removed_with_its_socket_and_cookie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    this_host: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _pids_alive(monkeypatch, 12345)  # a reused pid on this host does not keep the lock
    profile = _profile_with_lock(tmp_path, "other-host-12345")
    with caplog.at_level(logging.INFO, logger=earlier_chromium.logger.name):
        earlier_chromium.clear_stale_singleton_lock(profile)
    assert _singletons_left(profile) == []
    assert any("other-host" in record.getMessage() for record in caplog.records)


@requires_symlinks
def test_a_lock_a_live_local_process_holds_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, this_host: None
) -> None:
    _pids_alive(monkeypatch, 12345)
    profile = _profile_with_lock(tmp_path, f"{_HOST}-12345")
    earlier_chromium.clear_stale_singleton_lock(profile)
    assert _singletons_left(profile) == list(_SINGLETONS)


@requires_symlinks
def test_a_lock_whose_local_pid_is_gone_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, this_host: None
) -> None:
    _pids_alive(monkeypatch)
    profile = _profile_with_lock(tmp_path, f"{_HOST}-12345")
    earlier_chromium.clear_stale_singleton_lock(profile)
    assert _singletons_left(profile) == []


def test_a_profile_without_a_lock_is_left_alone(tmp_path: Path, this_host: None) -> None:
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "Local State").write_text("{}", encoding="utf-8")
    earlier_chromium.clear_stale_singleton_lock(profile)
    assert sorted(p.name for p in profile.iterdir()) == ["Local State"]


def test_a_lock_that_is_not_a_symlink_is_kept(tmp_path: Path, this_host: None) -> None:
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "SingletonLock").write_text("other-host-12345", encoding="utf-8")
    earlier_chromium.clear_stale_singleton_lock(profile)
    assert (profile / "SingletonLock").is_file()


@requires_symlinks
@pytest.mark.parametrize("target", ["nodelimiter", f"{_HOST}-notapid"])
def test_a_lock_chromium_cannot_read_is_left_for_chromium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, this_host: None, target: str
) -> None:
    _pids_alive(monkeypatch)
    profile = _profile_with_lock(tmp_path, target)
    earlier_chromium.clear_stale_singleton_lock(profile)
    assert _singletons_left(profile) == list(_SINGLETONS)


@requires_symlinks
@pytest.mark.asyncio
async def test_posix_profile_preparation_clears_a_stale_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, this_host: None
) -> None:
    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", False)
    _pids_alive(monkeypatch, 12345)
    profile = _profile_with_lock(tmp_path, "other-host-12345")
    await finished(earlier_chromium.prepare_profile(profile), "the preparation")
    assert _singletons_left(profile) == []


@requires_symlinks
@pytest.mark.asyncio
async def test_windows_profile_preparation_never_touches_the_posix_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, this_host: None
) -> None:
    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", True)
    _pids_alive(monkeypatch, 12345)
    profile = _profile_with_lock(tmp_path, "other-host-12345")
    await finished(earlier_chromium.prepare_profile(profile), "the preparation")
    assert _singletons_left(profile) == list(_SINGLETONS)
