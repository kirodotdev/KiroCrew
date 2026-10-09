"""Store writers whose final rename survives a Windows sharing violation.

Each writer below finishes a tmp + rename publish. On Windows that rename
raises ``PermissionError`` while another handle is open on the destination (an
AV scanner, the search indexer, a concurrent reader), so a bare ``os.replace``
loses the write on the first fault. These writers rename through
``atomic_write.replace_with_retry``.

``windows_sim.replace_sharing_violation`` fakes two faults on the store's own
file with ``IS_WINDOWS`` forced on; each case checks the write still lands and
leaves no temp file behind. This pins the wiring only, not real OS behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from windows_sim import replace_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import cron_script, platform_compat
from kiro_crew.appearance_packs import store as ap
from kiro_crew.security import redaction_allow
from kiro_crew.workflows.library import WorkflowDefinitionLibrary
from kiro_crew.workflows.store import WorkflowRunStore


@pytest.fixture(autouse=True)
def _windows_without_backoff(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    _floor_monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


def _leftover_temps(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if ".tmp" in p.name)


def test_workflow_definition_survives_a_contended_rename(tmp_path: Path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)

    with replace_sharing_violation(match=".json.tmp", times=2) as sim:
        created = library.create(source="async def run(ctx):\n    return 1\n", name="demo")

    assert sim["n"] == 3
    on_disk = json.loads((library.library_dir / f"{created['id']}.json").read_text("utf-8"))
    assert on_disk["id"] == created["id"]
    assert _leftover_temps(library.library_dir) == []


def test_workflow_run_snapshot_survives_a_contended_rename(tmp_path: Path) -> None:
    store = WorkflowRunStore(tmp_path)

    with replace_sharing_violation(match=".json.tmp", times=2) as sim:
        store.save("run-1", {"run_id": "run-1", "status": "done"})

    assert sim["n"] == 3
    saved = json.loads((store.runs_dir / "run-1.json").read_text("utf-8"))
    assert saved["status"] == "done"
    assert _leftover_temps(store.runs_dir) == []


def test_appearance_colour_map_survives_a_contended_rename(tmp_path: Path) -> None:
    store = ap.AppearanceStore(tmp_path)
    store.load()

    with replace_sharing_violation(match="crew-companion-colours.json", times=2) as sim:
        assert store.set_colour_map("default", {"#000000": "#ffffff"}) is True

    assert sim["n"] == 3
    reloaded = ap.AppearanceStore(tmp_path)
    reloaded.load()
    assert reloaded.colour_map("default") == {"#000000": "#ffffff"}
    assert _leftover_temps(tmp_path) == []


def test_grant_epoch_bump_survives_a_contended_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cron_script, "config_dir", lambda: tmp_path)

    with replace_sharing_violation(match=".grant_epochs.json", times=2) as sim:
        assert cron_script.bump_grant_epoch("job-1") == 1

    # The rename is the only matching replace; the lock file is opened, never renamed.
    assert sim["n"] == 3
    epochs = tmp_path / ".vault" / ".grant_epochs.json"
    assert json.loads(epochs.read_text())["job-1"] == 1
    assert [
        p.name for p in epochs.parent.iterdir() if p.name.startswith(".grant_epochs.json.")
    ] == []


def test_redaction_allow_list_survives_a_contended_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "redaction-allow" / "hosts.json"
    monkeypatch.setattr(redaction_allow, "_path_override", path)
    monkeypatch.setattr(redaction_allow, "_snapshot", None)

    with replace_sharing_violation(match="hosts.json", times=2) as sim:
        assert redaction_allow.allow_host("default", "example.com") is True

    assert sim["n"] == 3
    assert json.loads(path.read_text("utf-8")) == {"default": ["example.com"]}
    assert _leftover_temps(path.parent) == []
