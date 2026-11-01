"""Regression: AppStorage.get() must not fold a transient Windows sharing
violation into "key absent".

list_keys() reads digest files through read_bytes_with_retry (the sibling that
does it right, see test/test_apps_json_reads_windows_retry.py), but before the
fix get() read every value with a bare Path.read_text() and _read() turned the
PermissionError into None. A get() racing a concurrent set() of the SAME key
therefore reported a stored key as missing while list_keys() in the same instant
reported it present.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from windows_sim import read_text_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat
from kiro_crew.apps.app_storage import AppStorage


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


def test_get_rides_out_one_sharing_violation(windows, tmp_path: Path) -> None:
    storage = AppStorage("test-app", tmp_path)
    storage.set("settings", {"theme": "dark", "count": 7})

    # One sharing-violation window: a concurrent writer is replacing settings.json.
    with read_text_sharing_violation(match="settings.json", times=1):
        listed = storage.list_keys()
        value = storage.get("settings")

    assert "settings" in listed
    assert value == {
        "theme": "dark",
        "count": 7,
    }, f"get() reported a stored key as absent during a concurrent write: {value!r}"
