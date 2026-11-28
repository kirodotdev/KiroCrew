"""The KAS session facts an ``AcpSessionProvider`` reads off its session handle.

KAS reports these per session and the handle records them: the capabilities it
auto-approves, the agent its registered batch was built for, and the session recap it
replayed during ``session/load``. Each reader answers an empty value of its type when
the handle holds none, which is every other backend's answer.

Each function here backs an ``AcpSessionProvider`` member: the class binds
``take_session_recap`` by name and wraps the other two in typed properties, so each
reads the provider's own state on the instance.

Instance fields read: ``_handle``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.acp.session_provider import AcpSessionProvider


def kas_auto_approved_capabilities(self: AcpSessionProvider) -> frozenset[str] | None:
    """See ``AcpSessionHandle.kas_auto_approved``."""
    return getattr(self._handle, "kas_auto_approved", None)


def kas_projected_agent(self: AcpSessionProvider) -> str:
    """See ``AcpSessionHandle.kas_projected_agent``."""
    value = getattr(self._handle, "kas_projected_agent", "")
    return value if isinstance(value, str) else ""


def take_session_recap(self: AcpSessionProvider) -> str | None:
    """See ``AcpSessionHandle.take_session_recap``."""
    recap = self._handle.take_session_recap()
    return recap if isinstance(recap, str) and recap else None
