"""SDK surface for the per-runtime resource sampler.

The sampler lives in ``kiro_crew.acp.resource_monitor`` because it walks
``AcpRuntime`` process trees and the live-runtime registry, which are ACP
internals. Application code (the dashboard's ``/api/system/chat-resources``
handler) reaches it through this module, the one import boundary the
agent-sdk gate allows, so a future backend that is not ACP can supply the same
``ResourceSampler`` contract without the dashboard changing its import.
"""

from __future__ import annotations

from kiro_crew.acp.resource_monitor import (
    DEFAULT_INTERVAL_S,
    EntrySample,
    ResourceSampler,
    ResourceSnapshot,
    SlotResolver,
    SubagentLookup,
    provider_identity_for_session,
)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "EntrySample",
    "ResourceSampler",
    "ResourceSnapshot",
    "SlotResolver",
    "SubagentLookup",
    "provider_identity_for_session",
]
