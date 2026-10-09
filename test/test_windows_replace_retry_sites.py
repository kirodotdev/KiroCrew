"""Store writes that finish a tmp + rename through the Windows retry helper.

On Windows the final ``os.replace`` of an atomic write raises ``PermissionError``
(WinError 32) while another handle is open on the destination -- an indexer, an
AV scanner, a concurrent reader. ``atomic_write.replace_with_retry`` retries that
window. Each case below faults the first two renames of one store's write and
pins that the write still lands, so a store that renames with a bare
``os.replace`` fails here.

``windows_sim.replace_sharing_violation`` reproduces the fault on any OS; it
proves the retry is wired at each site, not the real OS behaviour end to end.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from windows_sim import replace_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import gateway_identity, platform_compat
from kiro_crew.artifacts import ArtifactFolderStore
from kiro_crew.session_map import SESSION_MAP_FILENAME, SessionMap


@pytest.fixture(autouse=True)
def _windows_without_backoff(_floor_monkeypatch):
    """Take the Windows branch of the helper and keep its retry loop instant."""
    _floor_monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    _floor_monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


def test_artifact_folder_save_survives_a_sharing_violation(tmp_path):
    path = tmp_path / "artifact_folders.json"
    folders = ArtifactFolderStore(path=path)

    with replace_sharing_violation(match=path.name, times=2) as sim:
        created = folders.create("Reports")

    assert sim["n"] == 3
    assert [f["id"] for f in json.loads(path.read_text(encoding="utf-8"))] == [created["id"]]
    assert list(tmp_path.glob("*.tmp")) == []


def test_gateway_id_is_persisted_through_a_sharing_violation(tmp_path, monkeypatch):
    gateway_identity._CACHED_IDS.clear()
    monkeypatch.setattr(gateway_identity, "config_dir", lambda: tmp_path)
    path = tmp_path / gateway_identity.GATEWAY_ID_FILE

    try:
        with replace_sharing_violation(match=path.name, times=2) as sim:
            minted = gateway_identity.gateway_id()
    finally:
        gateway_identity._CACHED_IDS.clear()

    assert sim["n"] == 3
    # A raise inside the install falls back to the process-local id, which is
    # never written: a persisted id on disk is what proves the retry ran.
    assert minted != gateway_identity._IN_MEMORY_ID
    assert path.read_text(encoding="utf-8").strip() == minted


def test_session_map_write_survives_a_sharing_violation(tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        session_map = SessionMap()
        with replace_sharing_violation(match=SESSION_MAP_FILENAME, times=2) as sim:
            session_map.set("dashboard:durable", "sid-durable")

    assert sim["n"] == 3
    on_disk = json.loads((tmp_path / SESSION_MAP_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["dashboard:durable"]["sid"] == "sid-durable"
    assert list(tmp_path.glob("*.tmp")) == []
