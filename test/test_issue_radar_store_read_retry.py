"""Issue Radar store reads ride out a Windows sharing violation.

The store's cache and config readers read files the store itself replaces with
``atomic_write``. On Windows a read that lands inside that replace fails with
``PermissionError`` for a moment; a bare read let it escape to the dashboard
route as an HTTP 500, or worse, a read-modify-write of ``config.json`` could act
on a missing read. The reads go through ``read_bytes_with_retry``.

The contention is simulated: ``platform_compat.IS_WINDOWS`` is forced on and the
helper's single-read primitive fails with ``PermissionError`` a set number of
times, so every runner proves the Windows arm without a real lock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import atomic_write as atomic_write_module
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.issue_radar.backend import store

OWNER, REPO = "acme", "widgets"


@pytest.fixture
def contended(monkeypatch):
    """Make the next ``n`` reads of any path fail as a Windows sharing violation."""
    monkeypatch.setattr(atomic_write_module, "_REPLACE_BACKOFF_SECONDS", 0)
    real = atomic_write_module._read_bytes
    remaining = {"n": 0}

    def _read(target, max_bytes):
        if remaining["n"] > 0:
            remaining["n"] -= 1
            raise PermissionError(32, "The process cannot access the file", str(target))
        return real(target, max_bytes)

    monkeypatch.setattr(atomic_write_module, "_read_bytes", _read)

    class _Contention:
        def arm(self, n: int) -> None:
            # Set after the test's own writes, so only the read under test takes
            # the Windows arm.
            monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
            remaining["n"] = n

        def spent(self) -> bool:
            """Every armed failure was met: the read went through the helper."""
            return remaining["n"] == 0

    return _Contention()


def test_config_read_during_a_replace_returns_the_connected_repos(tmp_path: Path, contended):
    store.write_config({"repos": [{"owner": OWNER, "repo": REPO}]}, tmp_path)
    contended.arm(2)

    got = store.read_config(tmp_path)

    assert got["repos"] == [{"owner": OWNER, "repo": REPO}]
    assert contended.spent()


def test_labels_read_during_a_replace_returns_the_cache(tmp_path: Path, contended):
    store.write_labels_cache(OWNER, REPO, [{"name": "bug", "color": "d73a4a"}], root=tmp_path)
    contended.arm(2)

    got = store.read_labels_cache(OWNER, REPO, tmp_path)

    assert got is not None and [lab["name"] for lab in got] == ["bug"]
    assert contended.spent()


def test_members_read_during_a_replace_returns_the_roster(tmp_path: Path, contended):
    store.write_members_cache(
        OWNER, REPO, [{"login": "octocat"}], source="collaborators", root=tmp_path
    )
    contended.arm(2)

    got = store.read_members_cache(OWNER, REPO, tmp_path)

    assert got is not None and got["source"] == "collaborators"
    assert [m["login"] for m in got["members"]] == ["octocat"]
    assert contended.spent()


def test_watch_state_read_during_a_replace_keeps_the_high_water_mark(tmp_path: Path, contended):
    store.write_watch_state(OWNER, REPO, 42, tmp_path)
    contended.arm(2)

    assert store.read_watch_state(OWNER, REPO, tmp_path)["last_seen_number"] == 42
    assert contended.spent()


def test_issue_detail_read_during_a_replace_returns_the_cached_issue(tmp_path: Path, contended):
    store.write_issue_detail_cache(
        OWNER, REPO, 7, {"number": 7, "title": "Crash"}, [], root=tmp_path
    )
    contended.arm(2)

    got = store.read_issue_detail_cache(OWNER, REPO, 7, tmp_path)

    assert got is not None and got["detail"]["title"] == "Crash"
    assert contended.spent()


def test_investigation_read_during_a_replace_returns_the_record(tmp_path: Path, contended):
    store.write_investigation(OWNER, REPO, 7, {"status": "investigating"}, root=tmp_path)
    contended.arm(2)

    got = store.read_investigation(OWNER, REPO, 7, tmp_path)

    assert got is not None and got["status"] == "investigating"
    assert contended.spent()


def test_a_damaged_config_still_degrades_to_no_repos(tmp_path: Path):
    store.config_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    store.config_path(tmp_path).write_text("{not json", encoding="utf-8")

    assert store.read_config(tmp_path) == {"repos": []}
