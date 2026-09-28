"""Typed Jev questions and answers for ``skills.select``.

The gate checks answer identifiers and domains before a response is consumed.
Probability metadata is not a guarantee of correctness or a selection threshold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The one shape a provider model id may take on the wire. ``provider.model``
#: lives in the agent-writable ``config.json`` and ``_to_wire`` sends it verbatim,
#: so without a bound it is a channel for anything an agent can write into that
#: file -- a credential it read, a message it wants to exfiltrate. A model id is a
#: short word of letters, digits, dots, dashes and underscores; nothing else is
#: sent, and the gate scrubs the word like the rest of the request besides.
MODEL_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def is_model_id(value: object) -> bool:
    """Whether *value* is a string :data:`MODEL_ID_RE` accepts."""
    return isinstance(value, str) and MODEL_ID_RE.fullmatch(value) is not None


@dataclass
class Choice:
    """Pick one member of the declared option domain."""

    id: str
    prompt: str
    options: list[str] = field(default_factory=list)


Question = Choice


@dataclass
class Answer:
    """A chosen option, its probability and optional provider confidence."""

    id: str
    value: object
    p: float
    confidence: float | None = None


Answers = dict[str, Answer]
