"""Fixtures and builders shared by ``test_session_storage.py`` and its seven topic files.

``stores`` points both stores at temp dirs (registered under that name; the function is
``stores_fixture``), and the autouse ``_fresh_scan_cache`` starts every test with no
cached filesystem pass. ``_cli_half``, ``_transcript`` and ``_archive_segment`` write
session files aged against the fixed ``_NOW``, ``_index`` builds a ``SessionIndex`` from
a {sid: stem} map, and ``_pin_a_fake_default_home`` puts the process in the default-home
posture. Each test file imports the fixtures it uses by name, which is what registers
them there.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import session_storage
from kiro_crew.config import paths
from kiro_crew.session_storage import SessionIndex

_NOW = 1_700_000_000.0
_DAY = 86400.0


@pytest.fixture(autouse=True)
def _fresh_scan_cache() -> None:
    """Start every test with no cached filesystem pass.

    The cache is real process state, and its key covers the store paths and the
    pairing — not the CONTENTS of the stores. So a test that writes more files and
    re-reads within the TTL would be answered from its own earlier pass. Clearing
    here makes that isolation explicit instead of depending on each test happening
    to read only once.
    """
    session_storage.invalidate_scan_cache()


@pytest.fixture(name="stores")
def stores_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point both stores at temp dirs; return (crew data home, kiro home).

    The kiro home is nested INSIDE the data home because that is what
    :func:`reclaim_block_reason` requires of an isolated instance: a store outside
    the data home may be shared with an instance whose map this one cannot read, so
    reclaiming from a sibling layout is refused by design.
    """
    crew_home = tmp_path / "crew"
    kiro_home = crew_home / "kiro"
    (crew_home / "sessions" / "archive").mkdir(parents=True)
    (kiro_home / "sessions" / "cli").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
    monkeypatch.setenv("KIRO_HOME", str(kiro_home))
    return crew_home, kiro_home


def _cli_half(kiro_home: Path, sid: str, *, log_bytes: int, age_days: float) -> int:
    root = kiro_home / "sessions" / "cli"
    mtime = _NOW - age_days * _DAY
    total = 0
    for suffix, payload in ((".json", b"{}"), (".jsonl", b"c" * log_bytes)):
        path = root / f"{sid}{suffix}"
        path.write_bytes(payload)
        os.utime(path, (mtime, mtime))
        total += len(payload)
    return total


def _transcript(crew_home: Path, stem: str, *, size: int, age_days: float) -> int:
    path = crew_home / "sessions" / f"{stem}.jsonl"
    path.write_bytes(b"t" * size)
    mtime = _NOW - age_days * _DAY
    os.utime(path, (mtime, mtime))
    return size


def _archive_segment(crew_home: Path, stem: str, stamp: str, *, size: int, age_days: float) -> int:
    path = crew_home / "sessions" / "archive" / f"{stem}__{stamp}.jsonl"
    path.write_bytes(b"a" * size)
    mtime = _NOW - age_days * _DAY
    os.utime(path, (mtime, mtime))
    return size


def _index(pairs: dict[str, str] | None = None, active: set[str] | None = None) -> SessionIndex:
    """Build an index from a readable {sid: stem} mapping.

    The production shape is stem -> sid (one session can own several stems), but
    tests read better keyed on the session, so this inverts for them.
    """
    stem_to_sid = {stem: sid for sid, stem in (pairs or {}).items()}
    return SessionIndex(stem_to_sid=stem_to_sid, active_sids=frozenset(active or set()))


def _pin_a_fake_default_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, legacy: bool = False
) -> None:
    """Put the process in the DEFAULT-home posture, against a fake host home.

    These tests need ``data_home()`` to BE the default (or the legacy default), so
    ``reclaim_block_reason`` takes its shared-store branch. Pinning ``_resolved_home``
    to ``paths._default_home()`` under the operator's real ``HOME`` achieved that by
    pointing the process at the operator's real ``~/.kiro/crew``: the next
    ``config_dir()`` then ``mkdir``s it and drops the recovery breadcrumb beside it,
    and the rootdir floor now fails a test that leaves the real default resolved. So
    the host home is faked first: every resolver here reads ``Path.home()``
    (``session_storage`` for both defaults and the pod root, ``paths`` for the data
    home), so the posture holds exactly as before, one directory over.
    """
    host_home = tmp_path / "host-home"
    host_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: host_home))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.setattr(
        paths, "_resolved_home", paths.legacy_home() if legacy else paths._default_home()
    )
    monkeypatch.setattr(paths, "_config_dir_memo", None)
