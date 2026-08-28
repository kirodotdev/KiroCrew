"""Resolution and permission seed for the OpenCode ACP adapter.

OpenCode serves ACP from its own binary via ``opencode acp``, the same shape as
goose: no separate npm adapter and no Node floor. The registry lists it as a
binary distribution. Privileged tools on that path fire
``session/request_permission`` only when project ``permission`` is ``ask``.
OpenCode's own default is permissive (most tools ``allow``), so an unseeded
work_dir would never ask.

``ensure_routed_settings`` writes ``permission: "ask"`` into the session
``work_dir`` (``opencode.json`` or an existing ``.opencode/opencode.json``)
when nothing is configured. It never overwrites an explicit operator choice
(including ``allow`` / ``--auto``-equivalent bypass) and never writes
``~/.config/opencode``. The routing probe always reads the file back rather
than trusting that a write happened.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.settings_io import read_text as read_settings_text
from kiro_crew.acp.settings_io import write_text as write_settings_text

logger = logging.getLogger(__name__)

OPENCODE_BIN = "opencode"
OPENCODE_ACP_SUBCOMMAND = "acp"

# Project-local files OpenCode reads. Root ``opencode.json`` is the documented
# project config; ``.opencode/opencode.json`` is the nested alternate. Never
# ``~/.config/opencode`` — that is the operator's global machine config.
_PROJECT_CONFIG_RELATIVE = "opencode.json"
_NESTED_CONFIG_RELATIVE = Path(".opencode") / "opencode.json"

PERMISSION_ASK = "ask"
PERMISSION_ALLOW = "allow"

# Modes under which OpenCode approves tool calls itself, so no
# session/request_permission is emitted and the gate never runs.
PERMISSION_BYPASS_VALUES = frozenset({PERMISSION_ALLOW})
# The one value that routes every privileged decision back to Kiro Crew.
PERMISSION_ROUTED_VALUES = frozenset({PERMISSION_ASK})

_UNRESOLVED: object = object()
_argv_cache: object = _UNRESOLVED


def resolve_argv() -> list[str] | None:
    """Find the OpenCode binary and return the argv that starts its ACP server."""
    from kiro_crew.acp.client import _mise_which, _normalize_exe_casing, _ordered_path_matches
    from kiro_crew.env import augmented_path

    candidates: list[str] = []

    override = os.environ.get("OPENCODE_BIN")
    if override and Path(override).is_file():
        candidates.append(override)

    mise_resolved = _mise_which(OPENCODE_BIN)
    if mise_resolved:
        candidates.append(mise_resolved)

    candidates.extend(_ordered_path_matches(OPENCODE_BIN, augmented_path()))

    for candidate in candidates:
        resolved = _normalize_exe_casing(candidate)
        if not resolved:
            continue
        if not platform_compat.is_executable_file(resolved):
            continue
        return [resolved, OPENCODE_ACP_SUBCOMMAND]

    return None


def resolve_argv_cached() -> list[str] | None:
    """``resolve_argv`` memoised for the process. Failures are not cached."""
    global _argv_cache  # noqa: PLW0603
    if _argv_cache is _UNRESOLVED:
        resolved = resolve_argv()
        if resolved is None:
            return None
        _argv_cache = resolved
    return _argv_cache if isinstance(_argv_cache, list) else None


def missing_adapter_message() -> str:
    """What to tell an operator whose host has no OpenCode binary."""
    return (
        "opencode not found. Install OpenCode (see https://opencode.ai), or set "
        "OPENCODE_BIN to the binary's path. OpenCode serves ACP from its own "
        "binary via `opencode acp`. Then sign in with `opencode auth login`."
    )


def signin_hint() -> str:
    """OpenCode owns its provider credentials; Kiro Crew never reads them."""
    return "Sign in with `opencode auth login`."


def _project_config_candidates(work_dir: Path | str) -> tuple[Path, Path]:
    root = Path(work_dir)
    return (root / _PROJECT_CONFIG_RELATIVE, root / _NESTED_CONFIG_RELATIVE)


def project_config_path(work_dir: Path | str) -> Path:
    """The project file we read and write. Never the operator's global config.

    Prefers an existing ``opencode.json``, then an existing
    ``.opencode/opencode.json``, else the documented project-root path so a
    seed does not invent a nested directory the operator did not use.
    """
    root_path, nested_path = _project_config_candidates(work_dir)
    if root_path.is_file():
        return root_path
    if nested_path.is_file():
        return nested_path
    return root_path


def _read_project_config(work_dir: Path | str) -> tuple[Path, dict | None, bool]:
    """Load the project file and report whether an unusable file exists.

    The boolean distinguishes a missing file (safe to create) from an unreadable,
    malformed, or non-object operator file (must be preserved). Callers that need
    the raw ``permission`` value still check for the key themselves — a present
    ``null`` is configured, not absent.
    """
    path = project_config_path(work_dir)
    try:
        raw = read_settings_text(path)
    except FileNotFoundError:
        return path, None, False
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return path, None, True
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("Could not parse %s: %s", path, exc)
        return path, None, True
    if not isinstance(data, dict):
        logger.warning("Could not use non-object project config %s", path)
        return path, None, True
    return path, data, True


def _effective_permission_token(value: object) -> str:
    """Collapse OpenCode's string / object permission shapes to one token.

    ``""`` means nothing configured (or unreadable). ``allow`` / ``ask`` are
    the documented shorthands. An object with ``"*"`` uses that wildcard. Any
    other configured shape is returned as ``"configured"`` so the seed leaves
    it alone and the probe does not invent a verdict.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        wildcard = value.get("*")
        if isinstance(wildcard, str) and wildcard.strip():
            return wildcard.strip()
        if value:
            return "configured"
        return ""
    return "configured"


def configured_permission(work_dir: Path | str) -> object:
    """Return the raw ``permission`` value as written, or ``None``.

    ``None`` covers a missing file, unreadable file, malformed JSON, and a
    missing key — every case where the value cannot be established.
    """
    _path, data, _exists = _read_project_config(work_dir)
    if data is None or "permission" not in data:
        return None
    return data.get("permission")


def routing_verdict(work_dir: Path | str) -> tuple[Verdict, str]:
    """Decide whether OpenCode tool calls will reach the PreToolUse gate.

    Reads the file back rather than trusting that a write happened: the seed is
    best-effort, its failure is only logged, and a permission assumed-but-not-
    present is precisely the case that silently disables the gate. OpenCode's
    own default is permissive, so a missing key is INDETERMINATE, not ROUTED.
    """
    path = project_config_path(work_dir)
    token = _effective_permission_token(configured_permission(work_dir))

    if not token:
        return (
            Verdict.INDETERMINATE,
            f"no permission in {path}, so OpenCode uses its own permissive default",
        )
    if token in PERMISSION_BYPASS_VALUES:
        return (
            Verdict.BYPASSED,
            f"permission={token} lets OpenCode approve tool calls without asking",
        )
    if token in PERMISSION_ROUTED_VALUES:
        return (Verdict.ROUTED, f"permission={token}")
    return (
        Verdict.INDETERMINATE,
        f"unrecognized permission={token!r} in {path}",
    )


def ensure_routed_settings(work_dir: Path | str) -> bool:
    """Write ``permission = "ask"`` when nothing is configured.

    This is what makes the OpenCode backend ROUTED by construction rather than
    by assumption: without it the adapter falls back to its own permissive
    default, and the routing verdict is correctly INDETERMINATE.

    Two deliberate restraints:

    - **Writes only when no permission is configured.** An explicit ``allow``,
      ``ask``, object map, or any other operator choice stays. A configured
      bypass therefore remains, the verdict stays BYPASSED, and the session
      refuses unless the named opt-out is on.
    - **Merges rather than replaces.** Other keys in the project file are
      preserved. Writes only under ``work_dir``, never ``~/.config/opencode``.

    Returns True when it wrote. Failures are reported, never swallowed: the
    caller's routing probe reads the file back, so a failed write surfaces as
    INDETERMINATE and refuses the session rather than passing silently.
    """
    path, data, exists = _read_project_config(work_dir)
    if data is not None and "permission" in data:
        existing = data.get("permission")
        logger.debug(
            "OpenCode permission already configured as %r in %s; leaving it",
            existing,
            path,
        )
        return False

    if data is None and exists:
        return False

    # Only a missing file starts from an empty document. A valid dict without
    # ``permission`` is merged so other keys stay; an invalid existing file was
    # refused above so operator content is never destroyed to establish routing.
    payload: dict = {} if data is None else data
    payload["permission"] = PERMISSION_ASK

    try:
        write_settings_text(path, json.dumps(payload, indent=2) + "\n")
    except OSError as exc:
        logger.warning("Could not seed %s: %s", path, exc)
        return False
    logger.info("Seeded %s with permission=%s", path, PERMISSION_ASK)
    return True


def remediation_hint(work_dir: Path | str) -> str:
    """What an operator must do to make the gate reachable."""
    path = project_config_path(work_dir)
    return (
        f'Write {{"permission": "{PERMISSION_ASK}"}} to {path} so OpenCode '
        "asks Kiro Crew before each privileged tool call."
    )


__all__ = [
    "OPENCODE_ACP_SUBCOMMAND",
    "OPENCODE_BIN",
    "PERMISSION_ALLOW",
    "PERMISSION_ASK",
    "PERMISSION_BYPASS_VALUES",
    "PERMISSION_ROUTED_VALUES",
    "configured_permission",
    "ensure_routed_settings",
    "missing_adapter_message",
    "project_config_path",
    "remediation_hint",
    "resolve_argv",
    "resolve_argv_cached",
    "routing_verdict",
    "signin_hint",
]
