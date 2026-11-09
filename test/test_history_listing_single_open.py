"""A cold read of an untitled transcript opens it once (scale-F10).

``listing_row`` reads the metadata line and, for an untitled session, the head of the
file. On a cold read both come from one open handle. These tests pin that the row is
the one the old two-open read built (head scanned by path), for every head shape, and
that a cold listing opens each transcript once.
"""

from __future__ import annotations

import builtins
import json
import os
from collections import Counter
from pathlib import Path

import pytest

from kiro_crew import history_listing_memo, history_search
from kiro_crew.history import ConversationLog
from kiro_crew.history_search import SessionCatalogProjection, listing_row

_MTIME_NS = 1_700_000_000 * 10**9
_META = {"_type": "metadata", "created_at": "2026-01-01T00:00:00"}


def _lines(*rows: object) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in rows)


def _assistant(n: int) -> list[dict]:
    return [{"role": "assistant", "content": f"reply {i}"} for i in range(n)]


#: Head shapes by name: the bytes of a transcript.
_SHAPES: dict[str, bytes] = {
    "titled": _lines({**_META, "title": "From metadata"}, {"role": "user", "content": "hi"}),
    "user_on_line_2": _lines(_META, {"role": "user", "content": "second line"}),
    "user_on_line_15": _lines(_META, *_assistant(13), {"role": "user", "content": "line 15"}),
    "metadata_only_short": _lines(_META),
    "metadata_only_long": _lines(*([_META] * 25)),
    "first_line_not_json": b"not json\n" + _lines({"role": "user", "content": "after junk"}),
    "first_line_json_list": _lines([1, 2], {"role": "user", "content": "after list"}),
    "first_line_other_object": _lines({"role": "user", "content": "no metadata line"}),
    "empty": b"",
    "non_utf8_first_line": b'{"_type": "metadata", "x": "\xff"}\n'
    + _lines({"role": "user", "content": "u"}),
    "non_utf8_later_line": _lines(_META, *_assistant(2)) + b'{"role": "user", "content": "\xff"}\n',
    "non_utf8_past_first_chunk": _lines(_META, {"role": "assistant", "content": "x" * 9000})
    + b'{"role": "user", "content": "\xff"}\n',
    "user_after_line_21": _lines(_META, *_assistant(22), {"role": "user", "content": "too late"}),
    "metadata_then_user_after_21": _lines(*([_META] * 22), {"role": "user", "content": "too late"}),
}


@pytest.fixture(autouse=True)
def _no_listing_memo(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(history_listing_memo, "TTL_SECS", 0.0)


@pytest.fixture
def opens(monkeypatch: pytest.MonkeyPatch) -> Counter:
    """Count every open of a transcript by the listing readers, by path."""
    seen: Counter = Counter()
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen[os.fspath(file)] += 1
        return real_open(file, *args, **kwargs)

    # The facade mirror creates the name on every owner: the metadata read and both head scans.
    monkeypatch.setattr(history_search, "open", counting_open, raising=False)
    return seen


def _seed(directory: Path, name: str, data: bytes) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    path.write_bytes(data)
    os.utime(path, ns=(_MTIME_NS, _MTIME_NS))
    return os.fspath(path)


def _two_open_row(log: ConversationLog, path: str, key: str):  # type: ignore[no-untyped-def]
    """The row as the old read built it: the head scanned by path, in a second open."""
    by_path = SessionCatalogProjection._scan_head_title
    return listing_row(log, path, key, None, by_path, lambda f: by_path(f.name))


def _caches(log: ConversationLog, key: str) -> tuple[object, object]:
    meta = log._meta_cache.get(key)
    scanned = log._title_scan_cache.get(key)
    return (meta and meta[2], scanned and scanned[2])


@pytest.mark.parametrize("shape", ["titled", "user_on_line_2", "first_line_other_object"])
def test_the_metadata_fill_publishes_after_the_handle_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """A writer that replaces the file during the publish must find it closed (Windows)."""
    path = _seed(tmp_path, shape, _SHAPES[shape])
    log = ConversationLog(base_dir=tmp_path)
    handles: list = []
    real_open = builtins.open

    def tracking_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = real_open(file, *args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(history_search, "open", tracking_open, raising=False)
    closed_at_store: list[list[bool]] = []

    class _Store:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __setitem__(self, key: str, value: object) -> None:
            closed_at_store.append([h.closed for h in handles])
            self._inner[key] = value  # type: ignore[index]

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    log._meta_cache = _Store(log._meta_cache)  # type: ignore[assignment]
    listing_row(
        log,
        path,
        shape,
        None,
        SessionCatalogProjection._scan_head_title,
        SessionCatalogProjection._scan_head_lines,
    )
    assert closed_at_store, "the metadata fill was never published"
    assert all(all(state) for state in closed_at_store), closed_at_store


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_single_open_row_matches_the_two_open_row(tmp_path: Path, opens: Counter, shape: str):
    path = _seed(tmp_path / "one", shape, _SHAPES[shape])
    ref_path = _seed(tmp_path / "two", shape, _SHAPES[shape])
    log = ConversationLog(base_dir=tmp_path / "one")
    ref_log = ConversationLog(base_dir=tmp_path / "two")

    row = log._catalog_projection._listing_row(path, shape, None)
    assert opens[path] == 1, f"{shape}: the cold read opened the transcript {opens[path]} times"
    ref = _two_open_row(ref_log, ref_path, shape)

    assert row._replace(stat=None) == ref._replace(stat=None)
    assert _caches(log, shape) == _caches(ref_log, shape)


def test_an_open_that_raises_marks_the_row_volatile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = _seed(tmp_path, "locked", _SHAPES["user_on_line_2"])
    log = ConversationLog(base_dir=tmp_path)
    real_open = builtins.open

    def refuse(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        if os.fspath(file) == path:
            raise PermissionError(13, "denied", path)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(history_search, "open", refuse, raising=False)
    row = log._catalog_projection._listing_row(path, "locked", None)
    assert (row.status, row.volatile, row.reusable, row.has_messages) == ("row", True, False, None)
    assert row.row is not None and row.row["title"] == "locked"
    assert row._replace(stat=None) == _two_open_row(log, path, "locked")._replace(stat=None)
    assert log._title_scan_cache.get("locked") is None


@pytest.mark.parametrize("listing", ["list_sessions_indexed", "list_sessions"])
def test_a_cold_listing_opens_each_transcript_once(tmp_path: Path, opens: Counter, listing: str):
    paths = [_seed(tmp_path, name, data) for name, data in sorted(_SHAPES.items())]
    log = ConversationLog(base_dir=tmp_path)

    rows = getattr(log, listing)()

    assert {r["key"] for r in rows} == set(_SHAPES)
    assert {p: opens[p] for p in paths} == {p: 1 for p in paths}
