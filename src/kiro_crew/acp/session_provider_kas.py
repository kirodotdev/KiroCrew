"""The KAS session facts an ``AcpSessionProvider`` reads off its session handle.

KAS reports these per session and the handle records them: the capabilities it
auto-approves, the agent its registered batch was built for, that batch, the mode it
last reported, its notices, and the session recap it replayed during ``session/load``.
Each reader answers an empty value of its type when the handle holds none, which is
every other backend's answer.

Each function here is an ``AcpSessionProvider`` member: the class binds it by name (a
property through ``property``), so it reads the provider's own state on the instance.

Instance fields read: ``_handle``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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


def kas_registered_agents(self: AcpSessionProvider) -> list[dict[str, Any]]:
    """A copy of ``AcpSessionHandle.kas_registered_agents``: the batch KAS holds."""
    value = getattr(self._handle, "kas_registered_agents", None)
    return [dict(a) for a in value if isinstance(a, dict)] if isinstance(value, list) else []


def kas_active_mode_id(self: AcpSessionProvider) -> str:
    """The last mode id KAS reported for this session, or ``""`` before any switch."""
    value = getattr(self._handle, "_last_kas_mode_id", "")
    return value if isinstance(value, str) else ""


def kas_agent_prompt_notice(self: AcpSessionProvider) -> str:
    """See ``AcpSessionHandle.kas_agent_prompt_notice``."""
    value = getattr(self._handle, "kas_agent_prompt_notice", "")
    return value if isinstance(value, str) else ""


def kas_bundle_engine_version(self: AcpSessionProvider) -> str:
    """See ``AcpSessionHandle.kas_bundle_engine_version``."""
    value = getattr(self._handle, "kas_bundle_engine_version", "")
    return value if isinstance(value, str) else ""


def kas_telemetry_notice(self: AcpSessionProvider) -> str:
    """See ``AcpSessionHandle.kas_telemetry_notice``."""
    value = getattr(self._handle, "kas_telemetry_notice", "")
    return value if isinstance(value, str) else ""


def kas_withheld_credentials_notice(self: AcpSessionProvider) -> str:
    """See ``AcpSessionHandle.kas_withheld_credentials_notice``."""
    value = getattr(self._handle, "kas_withheld_credentials_notice", "")
    return value if isinstance(value, str) else ""
