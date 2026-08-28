"""Claude-backend paths and the tool-gate routing seed.

The claude-agent-acp adapter decides per tool call whether to ask the client for
permission, and it takes that from ``permissions.defaultMode`` in a per-session
``<work_dir>/.claude/settings.local.json``. ``default`` routes every decision
back over ACP as ``session/request_permission``, which is the only path that
reaches Kiro Crew's PreToolUse gate. ``auto`` is the SDK's auto-accept mode: the
adapter approves on its own and the gate is never consulted.

``ensure_routed_settings`` writes that file when no mode is configured, which
is what makes the backend ROUTED by construction. It never overwrites an
explicit mode: a configured bypass stays, the verdict stays BYPASSED, and the
session refuses unless the named opt-out is on. The routing probe always reads
the file back rather than trusting that a write happened.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.settings_io import read_text as read_settings_text
from kiro_crew.acp.settings_io import write_text as write_settings_text
from kiro_crew.acp.types import CC_PERMISSION_MODE_AUTO, CC_PERMISSION_MODE_DEFAULT

logger = logging.getLogger(__name__)

# Modes under which the adapter approves tool calls itself, so no
# session/request_permission is emitted and the gate never runs.
PERMISSION_BYPASS_MODES = frozenset({CC_PERMISSION_MODE_AUTO, "bypassPermissions"})

# The one mode that routes every decision back to Kiro Crew.
PERMISSION_ROUTED_MODES = frozenset({CC_PERMISSION_MODE_DEFAULT})


def local_settings_path(work_dir: Path | str) -> Path:
    """The per-session settings file the adapter reads its mode from."""
    return Path(work_dir) / ".claude" / "settings.local.json"


def configured_permission_mode(work_dir: Path | str) -> str:
    """Return ``permissions.defaultMode`` as written, or ``""``.

    ``""`` covers a missing file, unreadable file, malformed JSON, and a mode of a
    non-string type — every case where the value cannot be established, which the
    caller must not conflate with a safe default.
    """
    path = local_settings_path(work_dir)
    try:
        raw = read_settings_text(path)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return ""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("Could not parse %s: %s", path, exc)
        return ""
    if not isinstance(data, dict):
        return ""
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return ""
    mode = permissions.get("defaultMode")
    return mode if isinstance(mode, str) and mode else ""


def routing_verdict(work_dir: Path | str) -> tuple[Verdict, str]:
    """Decide whether claude tool calls will reach the PreToolUse gate.

    Reads the file back rather than trusting that a write happened: the seed is
    best-effort and companion-attached, its failure is only logged, and a mode
    assumed-but-not-present is precisely the case that silently disables the gate.
    """
    path = local_settings_path(work_dir)
    mode = configured_permission_mode(work_dir)

    if not mode:
        return (
            Verdict.INDETERMINATE,
            f"no permissions.defaultMode in {path}, so the adapter uses its own "
            "default mode, which Kiro Crew does not set",
        )
    if mode in PERMISSION_BYPASS_MODES:
        return (
            Verdict.BYPASSED,
            f"permissions.defaultMode={mode} lets the adapter approve tool calls " "without asking",
        )
    if mode in PERMISSION_ROUTED_MODES:
        return (Verdict.ROUTED, f"permissions.defaultMode={mode}")
    # An unrecognized mode is not assumed safe: the adapter may add one that
    # auto-approves, and defaulting to ROUTED would silently adopt it.
    return (
        Verdict.INDETERMINATE,
        f"unrecognized permissions.defaultMode={mode} in {path}",
    )


def ensure_routed_settings(work_dir: Path | str) -> bool:
    """Write ``permissions.defaultMode = "default"`` when nothing is configured.

    This is what makes the claude backend ROUTED by construction rather than by
    assumption: without it the adapter falls back to its own default mode, which
    Kiro Crew does not set, and the routing verdict is correctly INDETERMINATE.

    Two deliberate restraints:

    - **Writes only when no mode is configured.** An explicitly configured mode is
      somebody's decision — an operator's, or a companion edition's — and
      silently rewriting it would be Kiro Crew overruling a choice it can see. A
      configured bypass mode therefore stays, the verdict stays BYPASSED, and the
      session refuses with the named opt-out available. Strengthening the gate
      behind the operator's back is still going behind their back.
    - **Merges rather than replaces.** Other keys in the file (a companion's
      model allowlist, for instance) are preserved, because this function's
      concern is one key.

    Returns True when it wrote. Failures are reported, never swallowed: the
    caller's routing probe reads the file back, so a failed write surfaces as
    INDETERMINATE and refuses the session rather than passing silently.
    """
    path = local_settings_path(work_dir)
    try:
        raw = read_settings_text(path)
    except FileNotFoundError:
        data: dict = {}
    except OSError as exc:
        logger.warning("Could not read %s before seeding: %s", path, exc)
        return False
    else:
        try:
            loaded = json.loads(raw)
        except ValueError as exc:
            logger.warning("Could not parse %s before seeding: %s", path, exc)
            return False
        if not isinstance(loaded, dict):
            logger.warning("Could not seed non-object settings file %s", path)
            return False
        data = loaded

    permissions = data.get("permissions")
    if permissions is None and "permissions" not in data:
        permissions = {}
    elif not isinstance(permissions, dict):
        logger.warning("Could not seed non-object permissions in %s", path)
        return False
    elif "defaultMode" in permissions:
        logger.debug(
            "claude permission mode already configured as %r in %s; leaving it",
            permissions.get("defaultMode"),
            path,
        )
        return False
    permissions["defaultMode"] = CC_PERMISSION_MODE_DEFAULT
    data["permissions"] = permissions

    try:
        write_settings_text(path, json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        logger.warning("Could not seed %s: %s", path, exc)
        return False
    logger.info("Seeded %s with permissions.defaultMode=%s", path, CC_PERMISSION_MODE_DEFAULT)
    return True


def remediation_hint(work_dir: Path | str) -> str:
    """What an operator must do to make the gate reachable."""
    path = local_settings_path(work_dir)
    return (
        f'Write {{"permissions": {{"defaultMode": "{CC_PERMISSION_MODE_DEFAULT}"}}}} '
        f"to {path} so the adapter asks Kiro Crew before each tool call."
    )


def signin_hint() -> str:
    return (
        "Sign in with the Claude CLI on this host; Kiro Crew stores no credential "
        "for this backend and reads none."
    )


__all__ = [
    "PERMISSION_BYPASS_MODES",
    "PERMISSION_ROUTED_MODES",
    "configured_permission_mode",
    "ensure_routed_settings",
    "local_settings_path",
    "remediation_hint",
    "routing_verdict",
    "signin_hint",
]
