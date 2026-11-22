"""A member database's full identity check runs once per file state, not per call.

Every named-store resolution reads the member database's identity, and the full
check (``PRAGMA quick_check``) reads every page. The answer is kept while the file
and its WAL keep the same stat identity, and a file that changed is checked again.
"""

from __future__ import annotations

import os
import shutil

import pytest

from kiro_crew import vector_memory_member as member_db
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.vector_memory import (
    create_member_database,
    read_member_database_identity,
)


@pytest.fixture
def checks(monkeypatch):
    """How many full identity checks ran since the fixture started."""
    seen = {"checks": 0}
    real = member_db._member_identity

    def counting(db):
        seen["checks"] += 1
        return real(db)

    monkeypatch.setattr(member_db, "_member_identity", counting)
    if hasattr(member_db, "_IDENTITY_CACHE"):
        monkeypatch.setattr(member_db, "_IDENTITY_CACHE", {})
    return seen


def _database(tmp_path, name="memory.db", *, member_id="m-1", store_id="member-a"):
    path = tmp_path / name
    create_member_database(path, member_id=member_id, store_id=store_id)
    return path


def _bump(path):
    """Move *path*'s mtime, as a write within the same clock tick would not.

    Two seconds clears even a filesystem with two-second timestamps.
    """
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def test_repeated_reads_check_the_file_once(tmp_path, checks):
    path = _database(tmp_path)

    for _ in range(20):
        assert read_member_database_identity(path) == ("m-1", "member-a")

    assert checks["checks"] == 1, "every read re-ran the full identity check"


def test_oversized_identity_is_returned_uncached(tmp_path, checks):
    member_id = "m" * (member_db._IDENTITY_CACHE_MAX_CHARS + 1)
    path = _database(tmp_path, member_id=member_id)

    for _ in range(2):
        assert read_member_database_identity(path) == (member_id, "member-a")

    assert checks["checks"] == 2


def test_a_write_to_the_database_checks_it_again(tmp_path, checks):
    path = _database(tmp_path)
    read_member_database_identity(path)

    db = sqlite3.connect(path)
    try:
        db.execute("INSERT INTO memory_meta (key,value,updated_at) VALUES ('probe','1','now')")
        db.commit()
    finally:
        db.close()
    _bump(path)

    assert read_member_database_identity(path) == ("m-1", "member-a")
    assert checks["checks"] == 2


def test_a_replaced_file_reports_the_new_identity(tmp_path, checks):
    path = _database(tmp_path)
    other = _database(tmp_path, "other.db", member_id="m-2", store_id="member-b")
    assert read_member_database_identity(path) == ("m-1", "member-a")

    os.replace(other, path)

    assert read_member_database_identity(path) == ("m-2", "member-b")


def test_a_damaged_file_fails_again_after_a_good_read(tmp_path, checks):
    path = _database(tmp_path)
    read_member_database_identity(path)

    # Overwrite the last page with junk, keeping the size: only the full check sees it.
    size = path.stat().st_size
    page = 4096
    with open(path, "r+b") as handle:
        handle.seek(size - page)
        handle.write(b"\xff" * page)
    _bump(path)

    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        read_member_database_identity(path)
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        read_member_database_identity(path)


def test_a_missing_file_still_raises_after_a_good_read(tmp_path, checks):
    path = _database(tmp_path)
    read_member_database_identity(path)
    moved = tmp_path / "moved"
    moved.mkdir()
    shutil.move(str(path), moved / path.name)

    with pytest.raises(sqlite3.Error):
        read_member_database_identity(path)
