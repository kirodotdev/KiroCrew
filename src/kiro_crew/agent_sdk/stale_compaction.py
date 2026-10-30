"""Agent SDK surface for marking a provider's next stale compaction status.

The idle compaction path in ``session_compaction`` gives up on a ``/compact`` that
reported nothing; this marks the provider so the late status, if it ever arrives,
is dropped instead of resetting the next turn's context reading. The mechanism
lives with the ACP clients in :mod:`kiro_crew.acp.stale_compaction`; application
code reaches it here, behind the agent SDK boundary.
"""

from __future__ import annotations

from kiro_crew.acp.stale_compaction import discard_provider_stale_compaction_status

__all__ = ["discard_provider_stale_compaction_status"]
