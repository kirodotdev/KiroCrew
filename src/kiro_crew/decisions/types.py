"""Typed Jev questions and answers.

Three question types, matching the provider's three: ``Choice`` picks one of the
declared options, ``Noul`` asks a yes/no question and gets back the probability
of yes, and ``Score`` rates the state along an ordered list of levels and gets
back a probability-weighted level. The gate checks every answer's identifier and
its type's own domain before a response is consumed. Probability metadata is not
a guarantee of correctness or a selection threshold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union

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
    """Pick one member of the declared option domain.

    ``criteria`` is the optional per-option rubric, sent as the provider's
    ``criteria`` map so the model is told what separates one option from another
    rather than inferring it from the option name alone. It maps an option to its
    rubric text; an option left out, or the whole field left unset, sends that
    option with no rubric -- today's wire shape -- so a point gains nothing until
    it authors text and nothing changes for one that does not. Any text supplied
    travels in the request, so it is scanned by the same scrub as the rest of the
    payload (see :func:`question_texts`).
    """

    id: str
    prompt: str
    options: list[str] = field(default_factory=list)
    criteria: dict[str, str] | None = None


#: The fewest and most levels one ``Score`` may declare. The provider refuses
#: anything outside this range, so a question outside it is refused here before
#: a request is spent on it.
SCORE_MIN_LEVELS = 2
SCORE_MAX_LEVELS = 10


@dataclass
class Noul:
    """A yes/no question. The answer's ``value`` is the probability of yes.

    ``true_means`` and ``false_means`` are the optional rubric for each side,
    sent as the provider's ``criteria``; either may be left unset.
    """

    id: str
    prompt: str
    true_means: str | None = None
    false_means: str | None = None


@dataclass
class Score:
    """Rate the state along ``levels``, lowest first.

    The answer's ``value`` is the probability-weighted level index: a number
    from 0 to ``len(levels) - 1`` that can land between two levels.
    """

    id: str
    prompt: str
    levels: list[str] = field(default_factory=list)


Question = Union[Choice, Noul, Score]


def question_texts(question: object) -> list[str]:
    """Every string *question* puts on the wire besides its id.

    The scrub scans these beside the state, because the rubric leaves the
    machine in the same request. One function for all three types, so a new
    field on one of them cannot be sent without also being scanned.
    """
    texts = [str(getattr(question, "prompt", "") or "")]
    if isinstance(question, Choice):
        texts.extend(str(option) for option in question.options)
        if question.criteria:
            texts.extend(str(text) for text in question.criteria.values() if text)
    elif isinstance(question, Noul):
        texts.extend(str(t) for t in (question.true_means, question.false_means) if t)
    elif isinstance(question, Score):
        texts.extend(str(level) for level in question.levels)
    return texts


@dataclass
class Answer:
    """One answer, its probability and optional provider confidence.

    ``value`` depends on the question's type: the chosen option for a
    ``Choice``, the probability of yes for a ``Noul``, and the weighted level
    index for a ``Score``. ``p`` is always a probability in 0..1: the chosen
    option's for a ``Choice``, the more likely side's (``max(value, 1 - value)``)
    for a ``Noul``, and the most likely level's for a ``Score``.
    """

    id: str
    value: object
    p: float
    confidence: float | None = None


Answers = dict[str, Answer]
