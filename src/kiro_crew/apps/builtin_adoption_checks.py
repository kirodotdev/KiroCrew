"""The eligibility checks of built-in adoption, each answering with a reason code.

Tree checks (E7, E8, E10, :func:`pinned_tree_refusal`) depend only on the pinned
tree and the wheel, so a failure is never retried for that pin. Host, policy and
permission checks (E3, E4, E5, E6, E9) depend on this host's configuration and are
re-run at every boot without a network call. Nothing here names an app: the core
name tables E10 consults are read from their owning modules.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtin_adoption_pins import AdoptionPin
from kiro_crew.apps.builtin_adoption_state import GrantPlan

logger = logging.getLogger(__name__)

#: Reason codes a later attempt may cure without a new pin (re-checked every boot).
RECHECKED_CODES = frozenset(
    {
        "admission_denied",
        "trust_conflict",
        "trust_overlay_owned",
        "permission_widening",
        "platform_mismatch",
        "host_too_old",
        "host_sdk_too_old",
    }
)

#: Reason codes that end adoption for this pin commit.
NEVER_RETRIED_CODES = frozenset(
    {
        "commit_not_found",
        "archive_digest_mismatch",
        "tree_digest_mismatch",
        "manifest_identity_mismatch",
        "version_mismatch",
        "tree_requires_build",
        "tree_has_link",
        "tree_unportable_name",
        "tree_too_large",
        "pinned_feature_unsupported",
    }
)

#: Transient failures: retried with backoff.
TRANSIENT_CODES = frozenset(
    {"network_unreachable", "network_timeout", "http_status", "disk_full", "config_unreadable"}
)

ALL_REASON_CODES = RECHECKED_CODES | NEVER_RETRIED_CODES | TRANSIENT_CODES

#: Root files that would make an install run a build step. Adoption never builds.
_BUILD_INPUTS = ("package.json", "pyproject.toml", "setup.py", "requirements.txt", ".gitattributes")


def _version_newer(left: str, right: str) -> bool:
    from kiro_crew.apps.version import parse_version

    try:
        return parse_version(left) > parse_version(right)
    except ValueError:
        return False


def core_named_apps() -> frozenset[str]:
    """App names a core table keys a capability on (each needs its own parity seam).

    Read from the owning modules on every call, so a table that grows is honoured
    without editing this module, and this module names no app itself.
    """
    names: set[str] = set()
    try:
        from kiro_crew.mcp_tools.apps import TOOL_APPS

        names.update(TOOL_APPS.values())
        from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_DISPATCH_OPERATIONS

        names.update(
            TOOL_APPS[tool]
            for tool in CHANNEL_AGENT_BLOCKED_DISPATCH_OPERATIONS
            if tool in TOOL_APPS
        )
        from kiro_crew import webhook_sources

        names.update(
            value
            for key, value in vars(webhook_sources).items()
            if key.startswith("SOURCE_") and isinstance(value, str)
        )
        from kiro_crew.apps.routes import _BUILTIN_SERVICE_APPS

        names.update(_BUILTIN_SERVICE_APPS)
    except Exception:  # noqa: BLE001 - an unreadable table refuses every pin
        logger.error("core app-name tables unreadable; refusing adoption", exc_info=True)
        raise
    return frozenset(names)


def _raw_manifest(tree_root: Path) -> dict[str, Any]:
    try:
        raw = json.loads((tree_root / "app.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def pinned_tree_refusal(
    pin: AdoptionPin,
    manifest: Any,
    tree_root: Path,
    *,
    shipped_manifest: Any = None,
    record_version: str = "",
) -> str | None:
    """E7, E8 and E10 against a verified copy of the pinned tree, or None.

    *manifest* is the parsed ``AppManifest`` at *tree_root*; *shipped_manifest* the
    wheel built-in's (its version must equal the pin's, which keeps a one-release
    downgrade data-safe); *record_version* the installed record's.
    """
    if manifest is None or getattr(manifest, "name", None) != pin.name:
        return "manifest_identity_mismatch"
    if manifest.version != pin.version or _version_newer(record_version or "0", pin.version):
        return "version_mismatch"
    if shipped_manifest is not None and getattr(shipped_manifest, "version", "") != pin.version:
        return "version_mismatch"
    if any((tree_root / name).exists() for name in _BUILD_INPUTS):
        return "tree_requires_build"
    raw = _raw_manifest(tree_root)
    if manifest.mcpServers or manifest.ui.overlays or raw.get("defaultEnabled") is True:
        return "pinned_feature_unsupported"
    try:
        if pin.name in core_named_apps():
            return "pinned_feature_unsupported"
    except Exception:  # noqa: BLE001 - logged in core_named_apps
        return "pinned_feature_unsupported"
    return None


def host_refusal(pin: AdoptionPin, manifest: Any) -> str | None:
    """E3 and E5: this gateway can run the pinned version, or the reason it cannot."""
    from kiro_crew.apps.app_sdk_level import APP_SDK_LEVEL
    from kiro_crew.apps.version import check_min_version

    if check_min_version(manifest.minKiroCrewVersion or "") is not None:
        return "host_too_old"
    if pin.min_app_sdk_level > APP_SDK_LEVEL:
        return "host_sdk_too_old"
    if not manifest.platform.supports_platform(sys.platform):
        return "platform_mismatch"
    return None


def admission_refusal(name: str, manifest: Any) -> str | None:
    """E4: the host's admission policy, applied as to any update. No exemption."""
    from kiro_crew.apps.admission import app_admission_denied

    return (
        "admission_denied"
        if app_admission_denied(name, manifest=manifest, action="update")
        else None
    )


def permission_refusal(name: str, manifest: Any) -> str | None:
    """E9: what ``update_app`` would hold back or newly ask consent for, or None.

    The same computation ``update_app`` runs, against the record's live state, so
    an adopted app keeps every grant it held and its ``enabled`` flag.
    """
    from kiro_crew.apps.manager import (
        _approved_grants_after_manifest_change,
        _effective_approved_grants,
        _held_back_entries,
        _read_installed,
        get_app_manifest,
    )
    from kiro_crew.apps.point_grants import grant_source

    old_manifest = get_app_manifest(name)
    existing = _read_installed(name, manifest=old_manifest)
    if existing is None:
        return "permission_widening"
    if manifest.permissions.sessionApproval and not (
        old_manifest and old_manifest.permissions.sessionApproval
    ):
        return "permission_widening"
    approved = _approved_grants_after_manifest_change(
        existing_approved=_effective_approved_grants(name, existing),
        old_permissions=grant_source(old_manifest),
        new_permissions=grant_source(manifest),
    )
    if any(_held_back_entries(approved, grant_source(manifest)).values()):
        return "permission_widening"
    return None


def plan_grant(name: str, pin: AdoptionPin) -> GrantPlan | str:
    """E6: write a grant, reuse a same-repository one, or refuse (``trust_conflict``).

    A grant naming another repository is the operator's consent to that repository
    and is never overwritten.
    """
    from kiro_crew.apps.execution import trusted_app_repository
    from kiro_crew.apps.registry_pipeline.git_targets import _same_git_target

    current = trusted_app_repository(name)
    if not current:
        return GrantPlan(write=True)
    if _same_git_target(current, pin.repository):
        return GrantPlan(write=False)
    return "trust_conflict"
