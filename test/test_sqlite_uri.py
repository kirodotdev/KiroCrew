"""Tests for kiro_crew.sqlite_uri: the read-only SQLite URI built from a path.

The helper is imported inside each test, so a run without the helper reports each
test failing instead of the whole module erroring at collection.
"""

from __future__ import annotations

import sys
from pathlib import PureWindowsPath

import pytest

from kiro_crew._sqlite_compat import sqlite3


def test_unc_uri_gets_the_empty_authority_spelling():
    from kiro_crew.sqlite_uri import with_empty_authority

    # Path.as_uri() gives file://fileserver/share/..., which SQLite rejects with
    # "invalid uri authority". The share must sit behind an empty authority.
    uri = PureWindowsPath(r"\\fileserver\share\crew\k.db").as_uri()
    assert uri == "file://fileserver/share/crew/k.db"
    assert with_empty_authority(uri) == "file:////fileserver/share/crew/k.db"


def test_drive_letter_and_posix_uris_are_unchanged():
    from kiro_crew.sqlite_uri import with_empty_authority

    drive = PureWindowsPath(r"C:\Users\a#b\k.db").as_uri()
    assert with_empty_authority(drive) == "file:///C:/Users/a%23b/k.db"
    assert with_empty_authority("file:///tmp/k.db") == "file:///tmp/k.db"


@pytest.mark.skipif(sys.platform == "win32", reason="probes the POSIX collapse of a leading //")
def test_empty_authority_spelling_opens_the_file(tmp_path):
    # The rewritten spelling must be one SQLite accepts: the doubled slash after
    # the empty authority is a path whose leading "//" the OS collapses on POSIX.
    db = tmp_path / "k.db"
    sqlite3.connect(str(db)).close()
    uri = "file:////" + db.absolute().as_uri()[len("file:///") :] + "?mode=ro"
    sqlite3.connect(uri, uri=True).close()


def test_uri_characters_in_the_path_open_that_exact_file_read_only(tmp_path):
    from kiro_crew.sqlite_uri import read_only_uri

    for name in ("has#hash", "has%41pct"):
        db = tmp_path / name / "k.db"
        db.parent.mkdir()
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE t (x)")
        con.commit()
        con.close()
        ro = sqlite3.connect(read_only_uri(db), uri=True)
        try:
            assert ro.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)
            try:
                ro.execute("CREATE TABLE u (x)")
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("the connection accepted a write")
        finally:
            ro.close()
