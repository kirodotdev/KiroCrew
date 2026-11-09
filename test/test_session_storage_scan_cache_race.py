"""A cached store scan that a mutation overlapped is not kept as the cached pass.

A read path enumerates the stores with no mutation lock held, which is the point of the
cache. When a move or restore finishes and calls ``invalidate_scan_cache`` WHILE that
enumeration is still running, the pass in flight describes the stores from before the
mutation. Storing it after the invalidation would serve the pre-mutation stores for the
whole TTL: a reclaimed session still counted beside the trash it now sits in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import session_storage


def _unit(uid: str) -> session_storage._RawUnit:
    return session_storage._RawUnit(uid=uid, sid="", stems=(), bytes=1, mtime=0.0)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    # The floor's own stack, so a test's monkeypatch.undo() cannot drop the home redirect.
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    _floor_monkeypatch.setenv("KIRO_HOME", str(tmp_path / "crew" / "kiro"))
    session_storage.invalidate_scan_cache()
    yield
    session_storage.invalidate_scan_cache()


def test_a_scan_overlapped_by_an_invalidation_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passes = [[_unit("before-the-move")], [_unit("after-the-move")]]

    def scan(_sid_for_stem, *, strict=False):
        result = passes.pop(0)
        if result[0].uid == "before-the-move":
            # A mutation lands while this pass is still enumerating.
            session_storage.invalidate_scan_cache()
        return result

    monkeypatch.setattr(session_storage, "_scan_raw_uncached", scan)

    first = session_storage._scan_raw({}, cached=True)
    assert [u.uid for u in first] == ["before-the-move"]
    assert session_storage._scan_cache is None

    second = session_storage._scan_raw({}, cached=True)
    assert [u.uid for u in second] == ["after-the-move"]


def test_a_scan_with_no_overlap_is_still_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def scan(_sid_for_stem, *, strict=False):
        calls.append(1)
        return [_unit("only")]

    monkeypatch.setattr(session_storage, "_scan_raw_uncached", scan)

    session_storage._scan_raw({}, cached=True)
    again = session_storage._scan_raw({}, cached=True)
    assert [u.uid for u in again] == ["only"]
    assert len(calls) == 1
