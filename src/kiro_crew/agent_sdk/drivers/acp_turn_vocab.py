"""The ACP driver's turn vocabulary: what running one prompt to its answer is built from.

The event a turn's stream yields and the kinds a collector branches on, the provider
interface it streams from, the errors a prompt raises (the busy one included), the usage
a turn reports and the resolver that finds its billing, the model ids a live session
advertises, and the label of the default backend. The owner modules of
:mod:`kiro_crew.llm_helpers` (``kiro_crew.llm_helpers_parts``) read these by value, so
they reach application code from here and a consumer never names ``kiro_crew.acp`` or
``kiro_crew.providers`` itself (``scripts/check_agent_sdk_boundary.py``). This follows
:mod:`kiro_crew.agent_sdk.drivers.acp_vocab`, which carries the names application code
reads by value for the rest of the tree.

As there, the imports are at MODULE scope on purpose: a constant has to exist when the
importing module binds it, and an ``except`` clause needs its class bound. The package
``__init__`` does not import this module, so the boot path stays ACP-free. Its only
importers are the ``llm_helpers_parts`` owners, and :mod:`kiro_crew.llm_helpers` drives
ACP turns, so whatever imports it has the ACP package loaded either way. Every name here
is the object its home module binds; ``test/test_llm_helpers_facade.py`` pins that, and
that no module outside ``kiro_crew.llm_helpers_parts`` imports this one.
"""

from __future__ import annotations

from kiro_crew.acp.client import AcpError, AcpPromptBusy, advertised_model_ids
from kiro_crew.acp.types import EVENT_STEER_CONSUMED, PROVIDER_LABEL_DEFAULT, TurnUsage
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    LLMEvent,
    LLMProvider,
    resolve_billing_stats,
)

__all__ = [
    "EVENT_COMPLETE",
    "EVENT_PERMISSION_REQUEST",
    "EVENT_STEER_CONSUMED",
    "EVENT_TEXT_CHUNK",
    "EVENT_TOOL_CALL",
    "EVENT_TOOL_RESULT",
    "PROVIDER_LABEL_DEFAULT",
    "AcpError",
    "AcpPromptBusy",
    "LLMEvent",
    "LLMProvider",
    "TurnUsage",
    "advertised_model_ids",
    "resolve_billing_stats",
]
