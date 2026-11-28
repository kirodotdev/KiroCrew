"""R72 retires the facade shim without losing upgrade/rollback safety."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_byo import crew_current_patch
from kiro_byo.integration_manager import (
    IntegrationError,
    _plan_retired_generated_seams,
    _prepare_seam_install_plans,
    _sha_text,
)

REL = "src/kiro_crew/dashboard/chat_handlers.py"
PRISTINE = '''from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
from kiro_crew.dashboard.chat_api.slot_effort import (
    _configured_backend_for_slot,
    api_chat_slot_reasoning_effort,
    api_chat_slot_selection_capabilities,
)
from kiro_crew._facade_compose import compose as _facade_compose
from kiro_crew.dashboard.chat_api import slot_effort as _owner_slot_effort
_facade_compose(_owner_slot_effort)
'''
PATCHED = crew_current_patch.patch_text(REL, PRISTINE)


def _setup(tmp_path: Path, *, receipt: bool = True):
    crew = tmp_path / "site-packages" / "kiro_crew"
    live = crew / "dashboard" / "chat_handlers.py"
    live.parent.mkdir(parents=True)
    live.write_text(PATCHED, encoding="utf-8")
    home = tmp_path / "app-home"
    pristine = home / "data" / "backups" / "packaged-0.12.0" / "dashboard" / "chat_handlers.py"
    pristine.parent.mkdir(parents=True)
    pristine.write_text(PRISTINE, encoding="utf-8")
    snap_root = home / "data" / "integration-runtime" / "0.12.0" / "overlay"
    installed = snap_root / REL
    installed.parent.mkdir(parents=True)
    installed.write_text(PATCHED, encoding="utf-8")
    if receipt:
        state = {
            "detected_version": "0.12.0",
            "crew_package": str(crew),
            "runtime_overlay_root": str(snap_root),
            "generated_files": [REL],
        }
        state_path = home / "data" / "integration.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
    target = SimpleNamespace(
        crew_package=crew,
        crew_version="0.12.0",
        generated_files=frozenset(),
        created_files=frozenset(),
        compatibility={"retired_generated_files": [REL]},
        manifest={"files": []},
    )
    return target, home, live, pristine, installed


def test_r71_owned_facade_is_transactionally_restored_on_r72_upgrade(tmp_path):
    target, home, live, pristine, installed = _setup(tmp_path)
    plans, outputs, hashes, recovered, missing = _prepare_seam_install_plans(target, home, "0.12.0")
    assert len(plans) == 1
    assert plans[0]["retired_file"] is True
    assert plans[0]["current_hash"] == _sha_text(PATCHED)
    assert plans[0]["output_text"] == PRISTINE
    assert REL not in outputs  # must NOT be present in the new runtime snapshot
    assert not hashes and not recovered and not missing
    assert live.read_text() == PATCHED  # no writes during preflight
    live.write_text(plans[0]["output_text"])
    assert crew_current_patch.inspect(REL, live.read_text()).state == "clean"
    assert _plan_retired_generated_seams(target, home, "0.12.0") == []  # fixed point


@pytest.mark.parametrize("change", [
    "no-receipt", "modified-live", "modified-snapshot", "modified-backup",
    "wrong-version", "wrong-host", "external-snapshot",
])
def test_r72_retirement_rejects_unproven_or_foreign_bytes(tmp_path, change):
    target, home, live, pristine, installed = _setup(tmp_path, receipt=change != "no-receipt")
    if change == "modified-live":
        live.write_text(PATCHED + "\n# third party\n")
    elif change == "modified-snapshot":
        installed.write_text("different")
    elif change == "modified-backup":
        pristine.write_text(PATCHED)
    elif change in {"wrong-version", "wrong-host", "external-snapshot"}:
        state_file = home / "data" / "integration.json"
        state = json.loads(state_file.read_text())
        if change == "wrong-version":
            state["detected_version"] = "0.12.1"
        elif change == "wrong-host":
            state["crew_package"] = str(tmp_path / "other-crew")
        else:
            state["runtime_overlay_root"] = str(tmp_path / "foreign-snapshot")
        state_file.write_text(json.dumps(state))
    with pytest.raises(IntegrationError, match="retired Crew seam|previous BYO ownership"):
        _plan_retired_generated_seams(target, home, "0.12.0")
    assert live.read_text() != PRISTINE  # never accidentally erase an unknown modification


def test_pristine_crew_never_requires_old_byos_install_receipt(tmp_path):
    target, home, live, pristine, installed = _setup(tmp_path, receipt=False)
    live.write_text(PRISTINE)
    assert _plan_retired_generated_seams(target, home, "0.12.0") == []


def test_retirement_manifest_cannot_overlap_active_or_create_only(tmp_path):
    target, home, live, pristine, installed = _setup(tmp_path)
    target.generated_files = frozenset({REL})
    with pytest.raises(IntegrationError, match="overlaps active"):
        _plan_retired_generated_seams(target, home, "0.12.0")
