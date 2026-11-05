"""Agent SDK surface for unresolved compaction-status ownership.

An idle compaction can stop without a terminal status. The ACP status reader
keeps that ownership unresolved so a late status cannot mutate a normal turn.
The session coordinator examines this state before it starts another
compaction. It can also record an unresolved terminal without importing the
ACP implementation directly.
"""

from __future__ import annotations

from kiro_crew.acp.stale_compaction import (
    discard_provider_stale_compaction_status,
    provider_has_unresolved_compaction_status,
    take_provider_context_reinjection,
    take_provider_unresolved_warning,
)

__all__ = [
    "discard_provider_stale_compaction_status",
    "provider_has_unresolved_compaction_status",
    "take_provider_context_reinjection",
    "take_provider_unresolved_warning",
]
