"""Resolution for the pi ACP adapter.

The registry identity is ``pi-acp``; Kiro Crew persists ``pi``. The adapter is
the npm package ``pi-acp``, which wraps the ``pi`` coding agent. There is no
``pi acp`` subcommand on the agent itself — launching that would not start an
ACP server.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

PI_ACP_BIN = "pi-acp"
PI_ACP_NPM_PKG = "pi-acp"

_UNRESOLVED: object = object()
_argv_cache: object = _UNRESOLVED


def resolve_argv() -> list[str] | None:
    """Find the ``pi-acp`` entry and return the argv that starts the adapter."""
    from kiro_crew.acp.client import _mise_which, _normalize_exe_casing, _ordered_path_matches
    from kiro_crew.env import augmented_path

    candidates: list[str] = []

    override = os.environ.get("PI_ACP_BIN")
    if override and Path(override).is_file():
        candidates.append(override)

    mise_resolved = _mise_which(PI_ACP_BIN)
    if mise_resolved:
        candidates.append(mise_resolved)

    candidates.extend(_ordered_path_matches(PI_ACP_BIN, augmented_path()))

    for candidate in candidates:
        resolved = _normalize_exe_casing(candidate)
        if not resolved:
            continue
        if not platform_compat.is_executable_file(resolved):
            continue
        return [resolved]

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
    """What to tell an operator whose host has no pi-acp adapter."""
    return (
        "pi-acp not found. Install it with "
        f"`npm install -g {PI_ACP_NPM_PKG}` and install the pi agent "
        "(`npm install -g @earendil-works/pi-coding-agent`), or set "
        "PI_ACP_BIN to the adapter entry. Then configure pi's providers."
    )


def signin_hint() -> str:
    """pi owns its provider credentials; Kiro Crew never reads them."""
    return "Configure a provider in pi."


__all__ = [
    "PI_ACP_BIN",
    "PI_ACP_NPM_PKG",
    "missing_adapter_message",
    "resolve_argv",
    "resolve_argv_cached",
    "signin_hint",
]
