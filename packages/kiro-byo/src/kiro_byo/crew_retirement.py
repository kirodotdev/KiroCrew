"""Fail-closed retirement of previously owned Crew facade source seams.

Isolated from the main installer to preserve its size/complexity guard. These
checks only produce a transaction PLAN: the installer performs writes and rollback.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def plan_retired_generated_seams(target: Any, app_home: Path, safe_version: str) -> list[dict[str, Any]]:
    # Delay import to use the installer's exact path/backup/hash contracts after
    # the integration_manager module has finished loading.
    from . import integration_manager as im
    from . import crew_current_patch
    """Restore retired BYO facade changes only with a proven R71 receipt.

    A retired seam is no longer part of the active 0.12 mutation inventory. On
    upgrade, byte-for-byte ownership is established by the prior installed
    snapshot, and the restore source must independently inspect as pristine.
    An unknown file or absent receipt is refused; no 'best-effort' cleanup.
    """
    retired = im._compat_file_paths(target.compatibility, "retired_generated_files")
    if retired & (target.generated_files | target.created_files):
        raise im.IntegrationError("retired Crew seam overlaps active mutation inventory")
    previous = im._read_json(app_home / "data" / "integration.json")
    previously_owned = im._recorded_uninstall_paths(previous, "generated_files") if previous else []
    plans: list[dict[str, Any]] = []
    for rel in sorted(retired):
        if rel not in {
            "src/kiro_crew/dashboard/chat_handlers.py",
            "src/kiro_crew/acp_backends.py",
            "src/kiro_crew/agent_sdk/tool_gate.py",
            # member_sessions.py: BYO's only live injection here was a marker
            # comment; real member-backend selection lives in loader_builders.py
            # (_read_backend_choice).  Retired so Crew's own select_provider_backend
            # precedence (override > crewmate pin > member auto > default) stands
            # unpatched.  On upgrade from an older BYO its stale enforce-BYO guard
            # is cleared by pristine restore through this same transaction.
            "src/kiro_crew/members_parts/member_sessions.py",
        }:
            raise im.IntegrationError(f"unknown retired Crew seam: {rel}")
        dst = im._checked_uninstall_seam(target.crew_package, rel)
        if not dst.is_file():
            raise im.IntegrationError(f"missing retired Crew seam: {rel}")
        current_text = im._normalized_text(dst)
        inspection = crew_current_patch.inspect(rel, current_text)
        if inspection.state == "clean":
            continue
        if inspection.state not in {"patched", "upgradeable"}:
            raise im.IntegrationError(f"retired Crew seam has foreign/partial changes: {rel}")
        if (rel not in previously_owned
                or str(previous.get("detected_version") or "") != target.crew_version
                or Path(str(previous.get("crew_package") or "")).resolve() != target.crew_package.resolve()):
            raise im.IntegrationError(f"cannot prove previous BYO ownership of retired seam: {rel}")
        former_root = Path(str(previous.get("runtime_overlay_root") or ""))
        # Never accept a user-controlled arbitrary snapshot path as evidence.
        expected_root = app_home / "data" / "integration-runtime" / safe_version / "overlay"
        if former_root.resolve() != expected_root.resolve():
            raise im.IntegrationError(f"retired Crew seam snapshot belongs to a different runtime: {rel}")
        installed_snapshot = expected_root / rel
        pristine = im._backup_for(app_home, target.crew_version, rel)
        if not installed_snapshot.is_file() or not pristine.is_file():
            raise im.IntegrationError(f"retired Crew seam has no installed snapshot or pristine backup: {rel}")
        if im._normalized_sha(installed_snapshot) != im._sha_text(current_text):
            raise im.IntegrationError(f"retired Crew seam changed after installation: {rel}")
        pristine_text = im._normalized_text(pristine)
        if crew_current_patch.inspect(rel, pristine_text).state != "clean":
            raise im.IntegrationError(f"retired Crew seam backup is not pristine: {rel}")
        compile(pristine_text, rel, "exec")
        plans.append({
            "rel": rel,
            "dst": dst,
            "current_hash": im._sha_text(current_text),
            "output_text": pristine_text,
            "backup_text": None,
            "persistent_backup": pristine,
            "existed_before": True,
            "created_file": False,
            "retired_file": True,
        })
    return plans


