"""Google Antigravity-specific checks for an ``AcpClient`` session.

The standalone Antigravity runtime advertises its per-tool permission flow in
``default`` mode on release 1.4.4, while ``auto_edit`` and ``yolo`` suppress
those requests. Crew pins the verified release at the handshake so a later
runtime cannot silently change the wire shape it checks. An authenticated turn
proving every native and MCP call asks has not been captured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.acp.harness.antigravity import (
    ANTIGRAVITY_INSTALL_HINT,
    ANTIGRAVITY_VERIFIED_VERSION,
)
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.agent_sdk.backends import ACP_BACKEND_ANTIGRAVITY

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient


def _refuse_unverified_antigravity_version(self: AcpClient) -> None:
    """Refuse an Antigravity ACP runtime other than the verified release."""
    if self.backend == ACP_BACKEND_ANTIGRAVITY:
        version = self.agent_version
        if version == ANTIGRAVITY_VERIFIED_VERSION:
            return
        what = (
            f"Google Antigravity ACP {version} is not the version Kiro Crew verified "
            f"({ANTIGRAVITY_VERIFIED_VERSION}), so this chat was refused."
            if version
            else "Google Antigravity ACP did not report its version, so Kiro Crew could "
            f"not confirm it is the verified release ({ANTIGRAVITY_VERIFIED_VERSION}) "
            "and refused this chat."
        )
        raise AcpToolGateUnroutable(
            f"{what} {ANTIGRAVITY_INSTALL_HINT} Then start a new chat.",
            transient=False,
            user_worded=True,
        )
