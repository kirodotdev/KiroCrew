"""Tests for the age-based agents-directory janitor.

Covers the hard safety contract in :mod:`kiro_crew.agents_janitor`:

* an aged orphaned atomic-write temp is removed (foreign shape and this
  project's own ``mkstemp`` residue);
* a fresh temp (younger than the threshold) is kept;
* a live ``*.json`` spec is never touched;
* an aged foreign file with an unrecognized name is never touched;
* a symlink whose name matches a recognized shape is skipped (never followed);
* an unreadable / vanished entry is tolerated (fail-open);
* backups are aged on their own, longer retention window and are opt-in (Kiro
  Crew authors none of them, so they belong to foreign writers).

All filesystem work is under ``tmp_path`` — the sweep never runs against a real
agents directory here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew.agents_janitor import (
    DEFAULT_BACKUP_MAX_AGE_SECONDS,
    DEFAULT_TEMP_MAX_AGE_SECONDS,
    SweepResult,
    _classify_junk_name,
    sweep_agents_dir,
)

# A fixed reference clock keeps every age assertion deterministic.
_NOW = 1_000_000_000.0
# Older than the temp threshold but younger than the backup threshold — proves
# the two classes are aged independently.
_OLD_TEMP = _NOW - (DEFAULT_TEMP_MAX_AGE_SECONDS + 3600)
_OLD_BACKUP = _NOW - (DEFAULT_BACKUP_MAX_AGE_SECONDS + 3600)
_FRESH = _NOW - 60  # one minute old — an in-flight temp


def _touch(path: Path, *, mtime: float, content: str = "{}") -> Path:
    path.write_text(content, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


# ── name-shape classification (pure, no filesystem) ──


@pytest.mark.parametrize(
    "name,kind",
    [
        ("build-agent.json.12345.tmp", "temp"),
        ("kiro.json.1.tmp", "temp"),
        ("tmpA1b2C3d4.tmp", "temp"),  # this project's own mkstemp residue
        ("tmp0.tmp", "temp"),
        ("spec.bak-1700000000", "backup"),
        ("spec.json.bak.42", "backup"),
    ],
)
def test_classifies_junk_shapes(name: str, kind: str) -> None:
    assert _classify_junk_name(name) == kind


@pytest.mark.parametrize(
    "name",
    [
        "build-agent.json",  # live spec — the core safety case
        "kiro.json",
        "notes.txt",
        "agent.json.tmp",  # no digits between .json. and .tmp
        "agent.tmp",  # not a recognized temp shape
        "tmp.tmp",  # mkstemp always has a non-empty random middle
        "agent.bak-",  # no digits
        "agent.json.bak.",  # no digits
        "agent.json.bak",  # missing the trailing .<digits>
    ],
)
def test_rejects_non_junk_shapes(name: str) -> None:
    assert _classify_junk_name(name) is None


# ── sweep behaviour ──


def test_removes_aged_orphan_temp(tmp_path: Path) -> None:
    orphan = _touch(tmp_path / "build-agent.json.98765.tmp", mtime=_OLD_TEMP)

    result = sweep_agents_dir(tmp_path, now=_NOW)

    assert not orphan.exists()
    assert result.removed == 1
    assert orphan.name in result.removed_names


def test_sweep_survives_zeroed_dirent_identity(tmp_path: Path, monkeypatch) -> None:
    """Windows regression guard, runnable on any platform.

    On Windows, ``os.scandir``'s cached stat reports ``st_ino == st_dev == 0``
    while ``os.lstat`` returns the real identifiers. The delete-time recheck
    (``_unlink_if_still_stale``) compares by inode identity, so a scan that
    takes its snapshot from ``entry.stat()`` can never match the recheck there
    and the sweep silently refuses every removal. The scan must therefore
    snapshot via ``os.lstat`` — this shim makes ``entry.stat()`` return zeroed
    identity everywhere, so a regression back to ``entry.stat()`` fails here,
    not only on the Windows runner.

    The shim is installed on the MODULE UNDER TEST's ``os`` binding only
    (``agents_janitor.os``), never on the global ``os`` module: pytest's own
    tmp-path/residue machinery walks directories with ``with os.scandir(...)``
    during teardown, and a globally patched scandir handed it a fake that broke
    on Windows while passing on Linux.
    """
    import kiro_crew.agents_janitor as aj

    orphan = _touch(tmp_path / "build-agent.json.98765.tmp", mtime=_OLD_TEMP)

    class _ZeroIdentityEntry:
        def __init__(self, entry: "os.DirEntry[str]") -> None:
            self.name = entry.name
            self.path = entry.path
            self._entry = entry

        def stat(self, follow_symlinks: bool = True) -> os.stat_result:
            info = self._entry.stat(follow_symlinks=follow_symlinks)
            values = list(info)
            values[1] = 0  # st_ino, as on Windows scandir
            values[2] = 0  # st_dev, as on Windows scandir
            return os.stat_result(tuple(values))

    class _OsShim:
        """Delegates everything to the real ``os`` except ``scandir``, whose
        entries carry zeroed cached-stat identity — the exact Windows
        contract. ``os.lstat`` stays real, so the fixed scan (snapshotting via
        ``os.lstat``) sees true identity and removal proceeds; a regression to
        ``entry.stat()`` reads zeros and refuses."""

        def __getattr__(self, name: str):
            return getattr(os, name)

        def scandir(self, path):
            with os.scandir(path) as it:
                return [_ZeroIdentityEntry(e) for e in it]

    monkeypatch.setattr(aj, "os", _OsShim())

    result = sweep_agents_dir(tmp_path, now=_NOW)

    assert not orphan.exists()
    assert result.removed == 1


def test_removes_aged_own_mkstemp_residue(tmp_path: Path) -> None:
    # Kiro Crew's own _atomic_json_write leaves tmp<random>.tmp on a crash.
    residue = _touch(tmp_path / "tmpA1b2C3d4.tmp", mtime=_OLD_TEMP)

    result = sweep_agents_dir(tmp_path, now=_NOW)

    assert not residue.exists()
    assert result.removed == 1


def test_removes_aged_backups_when_opted_in(tmp_path: Path) -> None:
    # Backups are only swept when explicitly enabled.
    bak_epoch = _touch(tmp_path / "spec.bak-1700000000", mtime=_OLD_BACKUP)
    bak_json = _touch(tmp_path / "spec.json.bak.7", mtime=_OLD_BACKUP)

    result = sweep_agents_dir(tmp_path, now=_NOW, sweep_backups=True)

    assert not bak_epoch.exists()
    assert not bak_json.exists()
    assert result.removed == 2


def test_backups_left_alone_when_gated_off(tmp_path: Path) -> None:
    # sweep_backups=False (what the wired-in callers pass by default from
    # agent.sweep_agents_backups): foreign backups are never touched, even when
    # well past the backup window, because their retention is not Kiro Crew's to
    # decide. This is the ownership-boundary guarantee.
    bak_epoch = _touch(tmp_path / "spec.bak-1700000000", mtime=_OLD_BACKUP)
    bak_json = _touch(tmp_path / "spec.json.bak.7", mtime=_OLD_BACKUP)
    # A temp is still swept in the same call — only the backup class is gated.
    temp = _touch(tmp_path / "build-agent.json.5.tmp", mtime=_OLD_TEMP)

    result = sweep_agents_dir(tmp_path, now=_NOW, sweep_backups=False)

    assert bak_epoch.exists()
    assert bak_json.exists()
    assert not temp.exists()
    assert result.removed == 1
    assert result.removed_names == [temp.name]


def test_keeps_fresh_temp(tmp_path: Path) -> None:
    fresh = _touch(tmp_path / "build-agent.json.11111.tmp", mtime=_FRESH)

    result = sweep_agents_dir(tmp_path, now=_NOW)

    assert fresh.exists()  # an in-flight atomic replace must survive
    assert result.removed == 0


def test_keeps_backup_within_retention_window(tmp_path: Path) -> None:
    # A backup older than the TEMP threshold but younger than the BACKUP
    # threshold is a recovery artifact still inside its window — keep it even
    # when backups are opted in. This is the case Design Review flagged: a backup
    # must not be swept at 24h.
    recent_backup = _touch(tmp_path / "spec.json.bak.9", mtime=_OLD_TEMP)

    result = sweep_agents_dir(tmp_path, now=_NOW, sweep_backups=True)

    assert recent_backup.exists()
    assert result.removed == 0


def test_leaves_live_json_spec_untouched(tmp_path: Path) -> None:
    # Even an OLD live spec is not junk: it does not match any recognized shape.
    spec = _touch(tmp_path / "build-agent.json", mtime=_OLD_BACKUP)

    result = sweep_agents_dir(tmp_path, now=_NOW, sweep_backups=True)

    assert spec.exists()
    assert result.removed == 0


def test_leaves_aged_foreign_unrecognized_file(tmp_path: Path) -> None:
    # An old file whose name is not a recognized temp/backup shape is left be,
    # no matter how old it is.
    foreign = _touch(tmp_path / "some-other-tool.leftover", mtime=_OLD_BACKUP)

    result = sweep_agents_dir(tmp_path, now=_NOW, sweep_backups=True)

    assert foreign.exists()
    assert result.removed == 0


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation is privileged on Windows")
def test_skips_symlink_with_matching_name(tmp_path: Path) -> None:
    # A symlink whose NAME matches a recognized shape must be skipped entirely,
    # and its target must never be followed or removed.
    target = _touch(tmp_path / "real-target.txt", mtime=_OLD_BACKUP)
    link = tmp_path / "build-agent.json.55555.tmp"
    link.symlink_to(target)
    # Age the link itself past the threshold (utime the link, not the target).
    os.utime(link, (_OLD_TEMP, _OLD_TEMP), follow_symlinks=False)

    result = sweep_agents_dir(tmp_path, now=_NOW)

    assert link.is_symlink()  # link untouched
    assert target.exists()  # target never followed / removed
    assert result.removed == 0


def test_tolerates_unreadable_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A stat/unlink failure on one entry must not abort the sweep: the good
    # entry is still removed, the bad one is tolerated (fail-open).
    good = _touch(tmp_path / "good.json.222.tmp", mtime=_OLD_TEMP)
    bad = _touch(tmp_path / "bad.json.333.tmp", mtime=_OLD_TEMP)

    real_unlink = os.unlink

    def flaky_unlink(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if os.path.basename(os.fspath(path)) == bad.name:
            raise PermissionError("simulated unreadable/locked entry")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", flaky_unlink)

    result = sweep_agents_dir(tmp_path, now=_NOW)

    assert not good.exists()  # sweep continued past the failure
    assert bad.exists()  # the failing entry was tolerated, left in place
    assert result.removed == 1


def test_missing_directory_is_fail_open(tmp_path: Path) -> None:
    result = sweep_agents_dir(tmp_path / "does-not-exist", now=_NOW)
    assert isinstance(result, SweepResult)
    assert result.removed == 0


def test_never_touches_directories(tmp_path: Path) -> None:
    # A directory whose name matches a recognized shape is never removed.
    d = tmp_path / "weird.json.999.tmp"
    d.mkdir()
    os.utime(d, (_OLD_TEMP, _OLD_TEMP))

    result = sweep_agents_dir(tmp_path, now=_NOW)

    assert d.is_dir()
    assert result.removed == 0


def test_temp_age_boundary_is_exact(tmp_path: Path) -> None:
    # Exactly at the threshold is eligible (age >= max_age); just under is not.
    at = _touch(tmp_path / "at.json.1.tmp", mtime=_NOW - DEFAULT_TEMP_MAX_AGE_SECONDS)
    under = _touch(tmp_path / "under.json.2.tmp", mtime=_NOW - DEFAULT_TEMP_MAX_AGE_SECONDS + 1)

    result = sweep_agents_dir(tmp_path, now=_NOW)

    assert not at.exists()
    assert under.exists()
    assert result.removed == 1


def test_recreated_pathname_race_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The recreate-same-pathname race: an aged temp is scanned, then a writer
    # replaces that exact pathname with a FRESH file before the unlink. The
    # janitor must detect the change (inode/mtime) and refuse to delete the new
    # occupant, so a live in-flight spec write is never lost.
    orphan = _touch(tmp_path / "build-agent.json.7.tmp", mtime=_OLD_TEMP)

    real_lstat = os.lstat
    swapped = {"done": False}

    def racing_lstat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        # On the re-stat immediately before unlink, simulate a writer having
        # replaced the file with a fresh one (new inode + current mtime).
        info = real_lstat(path, *args, **kwargs)
        if not swapped["done"] and os.path.basename(os.fspath(path)) == orphan.name:
            swapped["done"] = True
            orphan.unlink()
            _touch(orphan, mtime=_NOW, content="{FRESH}")
            return real_lstat(path, *args, **kwargs)
        return info

    monkeypatch.setattr(os, "lstat", racing_lstat)

    result = sweep_agents_dir(tmp_path, now=_NOW)

    assert orphan.exists()  # the freshly-recreated file survives
    assert orphan.read_text(encoding="utf-8") == "{FRESH}"
    assert result.removed == 0


def test_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    # dry_run identifies eligible files and populates the result exactly as a
    # real sweep would, but unlinks nothing — this is the doctor report path.
    orphan = _touch(tmp_path / "build-agent.json.42.tmp", mtime=_OLD_TEMP)
    backup = _touch(tmp_path / "spec.json.bak.3", mtime=_OLD_BACKUP)

    result = sweep_agents_dir(tmp_path, now=_NOW, dry_run=True, sweep_backups=True)

    assert orphan.exists()  # nothing deleted
    assert backup.exists()
    assert result.removed == 2  # but both are reported as reclaimable
    assert set(result.removed_names) == {orphan.name, backup.name}
    assert result.freed_bytes > 0
