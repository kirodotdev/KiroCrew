"""A database whose filename contains `?` must still be copied correctly.

Interpolating the path into a `file:` URI made everything after a `?` read as the URI's
query string, so the path was truncated and a DIFFERENT database was opened — then
stored under the name of the one that was asked for. Silent, and the bundle looks fine.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing

import pytest

from kiro_crew import snapshot


def _make_db(path, marker: str) -> None:
    with closing(sqlite3.connect(str(path))) as c:
        c.execute("CREATE TABLE t (who TEXT)")
        c.execute("INSERT INTO t VALUES (?)", (marker,))
        c.commit()


def _read(path) -> list[str]:
    with closing(sqlite3.connect(str(path))) as c:
        return [r[0] for r in c.execute("SELECT who FROM t")]


@pytest.mark.skipif(not hasattr(sqlite3.Connection, "backup"), reason="needs the SQLite backup API")
class TestAQuestionMarkInTheFilenameDoesNotRedirectTheCopy:
    def _stage(self, tmp_path, name: str):
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()
        # A truncated path would resolve to this decoy instead.
        _make_db(src_dir / "memory", "decoy")
        _make_db(src_dir / name, "wanted")
        # _restage_databases only rewrites a staged file that already exists.
        (dst_dir / name).write_bytes(b"")
        return src_dir, dst_dir

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows forbids `?` in a filename, so the case cannot be constructed",
    )
    def test_the_requested_database_is_the_one_copied(self, tmp_path):
        name = "memory?snapshot=1.db"
        src_dir, dst_dir = self._stage(tmp_path, name)
        snapshot._restage_databases(src_dir, dst_dir, bundle_root=dst_dir)
        assert _read(dst_dir / name) == [
            "wanted"
        ], "the copy opened a different database than the one requested"

    def test_a_hash_in_the_filename_is_also_safe(self, tmp_path):
        # `#` is legal on both platforms, so this case carries the invariant on
        # Windows where the `?` case above cannot exist.
        name = "memory#1.db"
        src_dir, dst_dir = self._stage(tmp_path, name)
        snapshot._restage_databases(src_dir, dst_dir, bundle_root=dst_dir)
        assert _read(dst_dir / name) == ["wanted"]

    def test_an_ordinary_name_still_works(self, tmp_path):
        name = "plain.db"
        src_dir, dst_dir = self._stage(tmp_path, name)
        snapshot._restage_databases(src_dir, dst_dir, bundle_root=dst_dir)
        assert _read(dst_dir / name) == ["wanted"]
