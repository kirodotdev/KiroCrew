"""What the operating-contract renderer reads of ``agent.subagents``.

The agent-SDK boundary in front of ``acp.kas_engine_subagents``: the switch's
contract reading and the one sentence it swaps. Only ``context_assembly.agent_spec``
imports it, so the switch keeps exactly two readers (the KAS projection and the
contract).
"""

from __future__ import annotations

from kiro_crew.acp.kas_engine_subagents import (
    DEFAULT_AGENT,
    SPAWN_SUB_AGENTS_SENTENCE,
    contract_names_engine_subagents,
    with_engine_sentence,
)

__all__ = [
    "DEFAULT_AGENT",
    "SPAWN_SUB_AGENTS_SENTENCE",
    "contract_names_engine_subagents",
    "with_engine_sentence",
]
