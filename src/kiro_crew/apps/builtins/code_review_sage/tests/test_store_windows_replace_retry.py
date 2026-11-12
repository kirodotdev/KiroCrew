"""The store's by-name publish survives a Windows sharing violation on its final rename.

On Windows ``os.replace`` raises ``PermissionError`` while another handle is open on
the destination (an AV scanner, the search indexer, a concurrent reader), so a bare
rename loses the write on the first fault. ``store.atomic_write_locked`` renames
through ``atomic_write.replace_with_retry``; this fakes two faults with
``IS_WINDOWS`` forced on and checks the write lands and no temp is left. It pins the
wiring only, not real OS behaviour. Kiro Crew core's sweep of every converted writer
is ``test/test_windows_replace_retry_sweep.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.code_review_sage.sage_lib import store


def _fail_replace(monkeypatch: pytest.MonkeyPatch, *, match: str, times: int) -> dict[str, int]:
    """Make ``os.replace`` onto a file named *match* raise a sharing violation *times* times."""
    real_replace = os.replace
    state = {"n": 0}

    def _replace(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        if os.path.basename(str(dst)) == match:
            state["n"] += 1
            if state["n"] <= times:
                raise PermissionError(f"[WinError 32] simulated sharing violation replacing {dst}")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _replace)
    return state


def test_sage_by_name_publish_survives_a_contended_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
    # The by-name branch is the one Windows takes: it has no directory pinning.
    monkeypatch.setattr(store, "_CAN_PIN_DIR", False)
    target = tmp_path / "record.json"
    sim = _fail_replace(monkeypatch, match="record.json", times=2)
    store.atomic_write_locked(target, b'{"ok": true}')

    assert sim["n"] == 3
    assert target.read_bytes() == b'{"ok": true}'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["record.json"]
