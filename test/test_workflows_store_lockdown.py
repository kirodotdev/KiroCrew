"""Workflow run files are owner-only on Windows too.

``WorkflowRunStore.save`` locks the temp file down while it is still empty, so
the published run record is never readable by other local users. Off the event
loop the Windows DACL applies on any volume; on the loop a non-local volume is
skipped (an SMB round-trip there would stall the gateway), matching the rule
``config.loader_document`` applies to ``config.json``.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew.workflows import store as store_mod
from kiro_crew.workflows.store import WorkflowRunStore


@pytest.fixture
def lockdowns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int]]:
    """Act as Windows and record each lockdown with the file's size at that moment."""
    calls: list[tuple[Path, int]] = []

    def _record(path) -> None:
        p = Path(path)
        calls.append((p, p.stat().st_size))

    monkeypatch.setattr(store_mod.platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(store_mod.platform_compat, "restrict_to_owner", _record)
    return calls


def _saved(store: WorkflowRunStore, run_id: str) -> dict:
    return json.loads((store.runs_dir / f"{run_id}.json").read_text("utf-8"))


def test_off_loop_save_locks_down_the_empty_temp_on_any_volume(
    lockdowns, monkeypatch, tmp_path
) -> None:
    def _no_volume_check(_path) -> bool:
        raise AssertionError("off the loop the volume must not decide")

    monkeypatch.setattr(store_mod.windows_acl, "volume_is_local", _no_volume_check)
    store = WorkflowRunStore(base_dir=tmp_path)

    store.save("wf_000101", {"run_id": "wf_000101", "status": "done"})

    assert lockdowns == [(store.runs_dir / "wf_000101.json.tmp", 0)]
    assert _saved(store, "wf_000101")["status"] == "done"


def test_on_loop_save_locks_down_a_local_volume(lockdowns, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(store_mod, "on_event_loop", lambda: True)
    monkeypatch.setattr(store_mod.windows_acl, "volume_is_local", lambda _p: True)
    store = WorkflowRunStore(base_dir=tmp_path)

    store.save("wf_000102", {"run_id": "wf_000102"})

    assert lockdowns == [(store.runs_dir / "wf_000102.json.tmp", 0)]


@pytest.mark.parametrize("volume", ["remote", "unknown"])
def test_on_loop_save_skips_a_non_local_volume_and_warns_once(
    lockdowns, monkeypatch, tmp_path, caplog, volume
) -> None:
    def _volume_is_local(_path) -> bool:
        if volume == "unknown":
            raise OSError("descriptor API unavailable")
        return False

    monkeypatch.setattr(store_mod, "on_event_loop", lambda: True)
    monkeypatch.setattr(store_mod.windows_acl, "volume_is_local", _volume_is_local)
    store = WorkflowRunStore(base_dir=tmp_path)

    with caplog.at_level(logging.WARNING, logger=store_mod.logger.name):
        store.save("wf_000103", {"run_id": "wf_000103"})
        store.save("wf_000104", {"run_id": "wf_000104"})

    assert lockdowns == []
    assert _saved(store, "wf_000104")["run_id"] == "wf_000104"
    skipped = [r for r in caplog.records if "owner-only DACL was skipped" in r.getMessage()]
    assert len(skipped) == 1


def test_a_failed_lockdown_still_persists_the_run(monkeypatch, tmp_path) -> None:
    def _refuse(_path) -> None:
        raise OSError("DACL write failed")

    monkeypatch.setattr(store_mod.platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(store_mod.platform_compat, "restrict_to_owner", _refuse)
    store = WorkflowRunStore(base_dir=tmp_path)

    store.save("wf_000105", {"run_id": "wf_000105"})

    assert _saved(store, "wf_000105")["run_id"] == "wf_000105"
    assert not (store.runs_dir / "wf_000105.json.tmp").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_posix_run_file_lands_owner_only(tmp_path) -> None:
    store = WorkflowRunStore(base_dir=tmp_path)

    store.save("wf_000106", {"run_id": "wf_000106"})

    mode = stat.S_IMODE(os.stat(store.runs_dir / "wf_000106.json").st_mode)
    assert mode == 0o600
