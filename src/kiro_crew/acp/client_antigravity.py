"""Google Antigravity-specific checks for an ``AcpClient`` session.

The standalone Antigravity runtime advertises its per-tool permission flow in
``default`` mode from release 1.4.4 upward, while ``auto_edit`` and ``yolo``
suppress permission requests. Crew refuses a runtime below the 1.4.4 floor, and
one that reports no readable version, at the handshake, so a runtime nobody
probed cannot silently change the wire shape it checks. A release outside
``ANTIGRAVITY_TESTED_RANGE`` still runs and gets the shared tested-range notice
(``untested_version_issue``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.acp.client_claude_session import _adapter_version_at_least
from kiro_crew.acp.harness.antigravity import (
    ANTIGRAVITY_INSTALL_HINT,
    ANTIGRAVITY_MIN_VERSION,
)
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.agent_sdk.backends import ACP_BACKEND_ANTIGRAVITY

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient


def _refuse_unverified_antigravity_version(self: AcpClient) -> None:
    """Refuse an Antigravity ACP runtime below ``ANTIGRAVITY_MIN_VERSION`` or unreadable."""
    if self.backend != ACP_BACKEND_ANTIGRAVITY:
        return
    raw = self.agent_version
    version = raw.strip() if isinstance(raw, str) else ""
    if _adapter_version_at_least(version, ANTIGRAVITY_MIN_VERSION):
        return
    floor = ".".join(str(part) for part in ANTIGRAVITY_MIN_VERSION)
    what = (
        f"Google Antigravity ACP {version} is older than the minimum version {floor}, "
        "so Kiro Crew refused this chat."
        if _adapter_version_at_least(version, (0, 0, 0))
        else "Google Antigravity ACP did not report a version Kiro Crew can read, so "
        f"Kiro Crew could not confirm it is {floor} or newer and refused this chat."
    )
    raise AcpToolGateUnroutable(
        f"{what} {ANTIGRAVITY_INSTALL_HINT} Then start a new chat.",
        transient=False,
        user_worded=True,
    )
