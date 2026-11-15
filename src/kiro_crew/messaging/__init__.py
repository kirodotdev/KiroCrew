"""Messaging transport abstraction (`kiro_crew.messaging`).

Channel-neutral contracts shared by Slack and future messaging channels.
This package depends on nothing in ``kiro_crew.slack`` / ``kiro_crew.dashboard``;
both depend on ``kiro_crew.messaging``, never the reverse.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.messaging.approval import (
        APPROVAL_TIMEOUT_S,
        PendingApprovals,
        SessionApprovalDecider,
    )
    from kiro_crew.messaging.driver import (
        APPROVAL_AUTO,
        APPROVAL_INTERACTIVE,
        APPROVAL_TRUST,
        APPROVAL_TRUST_READS,
        TurnDriver,
    )
    from kiro_crew.messaging.link import (
        SLACK_NAMESPACE,
        ChannelLink,
        canonical_key,
        is_legacy_slack_key,
        legacy_key,
        session_key,
    )
    from kiro_crew.messaging.renderer import (
        COMPACTION,
        DONE,
        OUTPUT_KINDS,
        PROMPT_CHOICE,
        STEER_CONSUMED,
        TEXT_CHUNK,
        THINKING,
        TOOL_CALL,
        OutputEvent,
        Renderer,
        chunk_text,
    )
    from kiro_crew.messaging.transport import (
        ConfiguredChannelTarget,
        InboundMessage,
        MessagingTransport,
        TransportCapabilities,
    )

#: Public name -> the submodule that defines it. Resolved on first read
#: (:func:`__getattr__`): importing a leaf of this package runs this file first, and
#: a process that only needs a leaf (``messaging.link`` names a session's transcript)
#: must not load the turn driver, and the agent client behind it, to get it.
_EXPORTS: dict[str, str] = {
    "APPROVAL_TIMEOUT_S": "kiro_crew.messaging.approval",
    "PendingApprovals": "kiro_crew.messaging.approval",
    "SessionApprovalDecider": "kiro_crew.messaging.approval",
    "APPROVAL_AUTO": "kiro_crew.messaging.driver",
    "APPROVAL_INTERACTIVE": "kiro_crew.messaging.driver",
    "APPROVAL_TRUST": "kiro_crew.messaging.driver",
    "APPROVAL_TRUST_READS": "kiro_crew.messaging.driver",
    "TurnDriver": "kiro_crew.messaging.driver",
    "SLACK_NAMESPACE": "kiro_crew.messaging.link",
    "ChannelLink": "kiro_crew.messaging.link",
    "canonical_key": "kiro_crew.messaging.link",
    "is_legacy_slack_key": "kiro_crew.messaging.link",
    "legacy_key": "kiro_crew.messaging.link",
    "session_key": "kiro_crew.messaging.link",
    "COMPACTION": "kiro_crew.messaging.renderer",
    "DONE": "kiro_crew.messaging.renderer",
    "OUTPUT_KINDS": "kiro_crew.messaging.renderer",
    "PROMPT_CHOICE": "kiro_crew.messaging.renderer",
    "STEER_CONSUMED": "kiro_crew.messaging.renderer",
    "TEXT_CHUNK": "kiro_crew.messaging.renderer",
    "THINKING": "kiro_crew.messaging.renderer",
    "TOOL_CALL": "kiro_crew.messaging.renderer",
    "OutputEvent": "kiro_crew.messaging.renderer",
    "Renderer": "kiro_crew.messaging.renderer",
    "chunk_text": "kiro_crew.messaging.renderer",
    "ConfiguredChannelTarget": "kiro_crew.messaging.transport",
    "InboundMessage": "kiro_crew.messaging.transport",
    "MessagingTransport": "kiro_crew.messaging.transport",
    "TransportCapabilities": "kiro_crew.messaging.transport",
}

__all__ = [
    # Layer 1
    "MessagingTransport",
    "TransportCapabilities",
    "InboundMessage",
    "ConfiguredChannelTarget",
    # Layer 2
    "Renderer",
    "OutputEvent",
    "chunk_text",
    "OUTPUT_KINDS",
    "TEXT_CHUNK",
    "THINKING",
    "TOOL_CALL",
    "PROMPT_CHOICE",
    "COMPACTION",
    "DONE",
    "STEER_CONSUMED",
    # Layer 2 driver
    "TurnDriver",
    "APPROVAL_AUTO",
    "APPROVAL_TRUST",
    "APPROVAL_TRUST_READS",
    "APPROVAL_INTERACTIVE",
    # Layer 2 approvals
    "PendingApprovals",
    "SessionApprovalDecider",
    "APPROVAL_TIMEOUT_S",
    # Layer 3
    "ChannelLink",
    "session_key",
    "canonical_key",
    "legacy_key",
    "is_legacy_slack_key",
    "SLACK_NAMESPACE",
]


def __getattr__(name: str) -> Any:
    owner = _EXPORTS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(owner), name)
    # Bound once, so later reads skip this function and a patch of the name
    # replaces a real attribute.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
