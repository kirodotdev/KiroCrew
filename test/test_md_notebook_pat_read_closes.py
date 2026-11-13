"""md_notebook's token read closes the file it opens.

``_read_pat_sync`` once read the token with ``open(...).read()`` and never
closed the handle. CPython frees it when the last reference goes, so it is
quiet on Linux, but it warns ``ResourceWarning: unclosed file``, and on
Windows an open handle can block the rename or delete of the token file.

The warning is raised inside the file object's finalizer, where an
``error`` filter cannot turn it into an exception (it would only be reported
as "Exception ignored"). So the test records ResourceWarnings around the read
and forces collection. Garbage other tests left behind (an unclosed event
loop, a neighbour's file) is collected before recording starts, and only a
warning that names this test's own file counts.
"""

from __future__ import annotations

import gc
import warnings
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.md_notebook import server_store as store


def _read_recording_unclosed(path: Path) -> tuple[object, list[str]]:
    """Read the token and return it with the ResourceWarnings that name *path*."""
    gc.collect()  # flush what earlier tests left, so it is not counted here
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        result = store._read_pat_sync()
        gc.collect()
    messages = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
    return result, [m for m in messages if str(path) in m]


def test_reading_the_token_leaves_no_file_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "pat"
    token.write_text("  ghp_example  \n", encoding="utf-8")
    monkeypatch.setattr(store, "_pat_file", lambda: token)
    monkeypatch.setattr(store, "restrict_to_owner", lambda _path: None)

    result, unclosed = _read_recording_unclosed(token)

    assert result == "ghp_example"
    assert unclosed == []


def test_a_missing_token_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "_pat_file", lambda: tmp_path / "absent")

    assert store._read_pat_sync() is None
