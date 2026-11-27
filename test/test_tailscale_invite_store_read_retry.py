"""The Tailscale invite store's state read rides out a Windows sharing violation.

On Windows a read raises ``PermissionError`` (``WinError 32``) while another
thread's ``update_state`` replaces ``invites.json``. ``load_state`` folds an
unreadable file into the empty state, so without the retry a connected store
reads as ``device_id == ""`` and ``create_invites`` answers ``not_connected``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from windows_sim import read_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat
from kiro_crew.shared_crew import tailscale_invite_store as store


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


def _connected() -> None:
    store.update_state(lambda s: s.update(device_id="node-1", tailnet="example.com"))


def test_load_state_reads_the_device_through_a_sharing_violation(windows) -> None:
    _connected()
    with read_sharing_violation(match=store.STATE_FILE, times=2) as sim:
        state = store.load_state()
    assert state["device_id"] == "node-1"
    assert sim["n"] == 3


def test_update_state_keeps_the_stored_fields_through_a_sharing_violation(windows) -> None:
    _connected()
    with read_sharing_violation(match=store.STATE_FILE, times=2):
        state = store.update_state(lambda s: s.update(last_sync_at=1.0))
    assert state["device_id"] == "node-1"
    assert store.load_state()["tailnet"] == "example.com"


def test_a_damaged_state_file_still_reads_as_unreadable(windows) -> None:
    path = Path(store.ensure_root()) / store.STATE_FILE
    path.write_bytes(b"\xff\xfe not json")
    assert store.load_state()["device_id"] == ""
    with pytest.raises(store.StateUnreadableError):
        store.update_state(lambda s: None)
