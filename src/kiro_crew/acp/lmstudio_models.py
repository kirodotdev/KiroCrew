"""LM Studio inventory limits.

LM Studio inventories every GGUF artifact it can see as an ``llm`` entry,
including speculative-decoding draft heads; which of those rows may become a
session model is decided by :func:`kiro_crew.model_registry.is_interactive_chat_model`,
and the generic identifier/label bounds live in ``kiro_crew.acp.direct_toolkit``.
Only the LM-Studio-specific ceilings are here.
"""

from __future__ import annotations

#: Ceiling on catalogue rows retained from one ``/v1/models`` response.
MAX_MODEL_CATALOG_ENTRIES = 512

#: Ceiling on models treated as simultaneously resident. LM Studio loads on
#: demand, so an unbounded count would let one prompt wave load more than the
#: machine can hold.
MAX_LOADED_MODEL_INSTANCES = 16
