"""MCP elicitation vocabulary for application code.

The dashboard renders an MCP server's elicitation and answers it; the shape of
that ask and of its answer belongs to the ACP driver (:mod:`kiro_crew.acp.elicitation`).
Application code reads the names from here so it never imports ``kiro_crew.acp``
itself (``scripts/check_agent_sdk_boundary.py``). Every name is the object its home
module binds.

Module-scope imports on purpose, as in :mod:`kiro_crew.agent_sdk.drivers.acp_vocab`:
a constant has to exist when the importing module binds it, and an ``except`` clause
needs its class bound. The package ``__init__`` does not import this module.
"""

from __future__ import annotations

from kiro_crew.acp.elicitation import (
    ELICITATION_ACCEPT,
    ELICITATION_ACTIONS,
    ELICITATION_CANCEL,
    ELICITATION_DECLINE,
    ELICITATION_MODE_URL,
    PROPERTIES_MAX,
    STRING_VALUE_MAX_CHARS,
    ElicitationContentError,
    ElicitationRequest,
    validate_form_content,
)

__all__ = [
    "ELICITATION_ACCEPT",
    "ELICITATION_ACTIONS",
    "ELICITATION_CANCEL",
    "ELICITATION_DECLINE",
    "ELICITATION_MODE_URL",
    "PROPERTIES_MAX",
    "STRING_VALUE_MAX_CHARS",
    "ElicitationContentError",
    "ElicitationRequest",
    "validate_form_content",
]
