"""Google Antigravity-specific checks for an ``AcpClient`` session.

The standalone Antigravity runtime advertises its per-tool permission flow in
``default`` mode from release 1.4.4 upward, while ``auto_edit`` and ``yolo``
suppress permission requests. Crew enforces the 1.4.4 minimum version floor at
the handshake and logs one warning per process when a release sits outside
``ANTIGRAVITY_TESTED_RANGE`` or reports an unparseable version.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from kiro_crew.acp.harness.antigravity import (
    ANTIGRAVITY_INSTALL_HINT,
    ANTIGRAVITY_MIN_VERSION,
    ANTIGRAVITY_TESTED_RANGE,
)
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.agent_sdk.backends import ACP_BACKEND_ANTIGRAVITY

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

logger = logging.getLogger("kiro_crew.acp.client")

_ANTIGRAVITY_VERSION_RE = re.compile(
    r"^[vV]?(\d{1,9})\.(\d{1,9})(?:\.(\d{1,9}))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\Z",
    re.ASCII,
)
_warned_versions: set[str] = set()


def _parse_antigravity_version(version: object) -> tuple[int, int, int, int] | None:
    """Parse an Antigravity version string as ``(major, minor, patch, is_stable)``."""
    if not isinstance(version, str):
        return None
    cleaned = version.strip()
    if not cleaned:
        return None
    match = _ANTIGRAVITY_VERSION_RE.match(cleaned)
    if match is None:
        return None
    major, minor, patch, prerelease = match.groups()
    # SemVer sorts pre-releases (0) before the matching final release (1).
    return (
        int(major),
        int(minor),
        int(patch) if patch is not None else 0,
        0 if prerelease else 1,
    )


def _log_version_warning_once(version: str, detail: str) -> None:
    """Log one warning per process for each distinct version string."""
    if version in _warned_versions:
        return
    _warned_versions.add(version)
    logger.warning(
        "Google Antigravity ACP version %r %s. The session continues under default mode checks.",
        version or "unknown",
        detail,
    )


def _check_antigravity_version(client: AcpClient) -> None:
    """Apply ``ANTIGRAVITY_MIN_VERSION`` and log once outside ``ANTIGRAVITY_TESTED_RANGE``."""
    raw = client.agent_version
    version = raw.strip() if isinstance(raw, str) else ""
    parsed = _parse_antigravity_version(version) if version else None
    if parsed is None:
        _log_version_warning_once(version, "is absent or unparseable")
        return
    if parsed < (*ANTIGRAVITY_MIN_VERSION, 1):
        min_str = ".".join(str(part) for part in ANTIGRAVITY_MIN_VERSION)
        raise AcpToolGateUnroutable(
            f"Google Antigravity ACP {version} is older than the minimum version "
            f"{min_str}, so Kiro Crew refused this chat. {ANTIGRAVITY_INSTALL_HINT} "
            "Then start a new chat.",
            transient=False,
            user_worded=True,
        )
    low, high = ANTIGRAVITY_TESTED_RANGE
    if parsed < (*low, 1) or parsed >= (*high, 0):
        _log_version_warning_once(version, "sits outside ANTIGRAVITY_TESTED_RANGE")


def _refuse_unverified_antigravity_version(self: AcpClient) -> None:
    """Refuse an Antigravity ACP runtime below ``ANTIGRAVITY_MIN_VERSION``."""
    if self.backend == ACP_BACKEND_ANTIGRAVITY:
        _check_antigravity_version(self)
