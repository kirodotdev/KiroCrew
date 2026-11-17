"""A preserved-thinking signature rejection, read from one ACP error frame.

The provider refuses a request whose signed thinking block does not match the
conversation it was minted in. ``transport_errors`` types that refusal
(``AcpConversationBindingMismatch``) and, when the stated cause is a changed
``tools`` list, remembers it for the Tool Search never-defer rule.

Split out of ``transport_errors.py`` to keep that module under the file-line
cap; ``transport_errors`` re-imports these names, so
``kiro_crew.acp.transport_errors`` and ``kiro_crew.acp.client`` stay the import
paths every caller already uses.
"""

from __future__ import annotations

import re

# A preserved-thinking signature rejection whose own provider text says the block
# is bound to another conversation is recoverable by replacing the NATIVE
# conversation. Both clauses must appear IN ONE FIELD: the shorter
# invalid-signature form also covers tampered or undecryptable signatures, and
# pairing a clause from ``data`` with unrelated prose from ``message`` would mint
# a recoverable verdict the provider never sent.
_RE_THINKING_SIGNATURE_INVALID = re.compile(
    r"Invalid `signature` in `thinking` block", re.IGNORECASE
)
_RE_THINKING_BOUND_TO_OTHER_CONVERSATION = re.compile(
    r"\bblock is bound to a different conversation\b", re.IGNORECASE
)
# The reason the provider adds when what changed under the signed block is the
# request's ``tools`` list: a deferred tool was loaded after the block was minted.
_RE_THINKING_TOOLS_LIST_CHANGED = re.compile(
    r"`?tools`?\s+list differs from the one this block was created with", re.IGNORECASE
)


def _thinking_binding_mismatch_text(error: object) -> str:
    """The one raw provider field carrying both binding-mismatch clauses, or ``""``."""
    if not isinstance(error, dict):
        return ""
    for key in ("data", "message"):
        value = error.get(key)
        if (
            isinstance(value, str)
            and _RE_THINKING_SIGNATURE_INVALID.search(value)
            and _RE_THINKING_BOUND_TO_OTHER_CONVERSATION.search(value)
        ):
            return value
    return ""


def _is_thinking_binding_mismatch(error: object) -> bool:
    """Whether one raw provider field carries both binding-mismatch clauses."""
    return bool(_thinking_binding_mismatch_text(error))


def _is_tools_list_refusal(error: object) -> bool:
    """Whether a binding mismatch names a changed ``tools`` list as its cause.

    Read from the same field as the mismatch itself, so the reason cannot be
    borrowed from unrelated prose in the other field.
    """
    return bool(_RE_THINKING_TOOLS_LIST_CHANGED.search(_thinking_binding_mismatch_text(error)))
