"""Business adapters for the Jev decision seam.

``skills.select`` picks the skill a message loads: an exact offered key selects
one, an explicit no-skill answer selects none, and a refusal keeps trigger
matching. ``message.steer`` decides whether a message sent into a RUNNING turn
steers it or queues for the next one, and a refusal takes the steer path the
composer has always defaulted to. ``model.route`` answers how hard a chat turn is
and maps that tier to a model id, and a refusal keeps the model the session was
already on. ``memory.recall`` decides which of the memories vector similarity
recalled are worth their place in the prompt, and a refusal injects the similarity
top-k unchanged. ``tool.risk`` decides nothing and only annotates. Each adapter's
refusal is the shipped behaviour, never a third outcome.

The core package owns transport, sampling and diagnostic logging.

What lives HERE rather than in one adapter is the prior-conversation read, because
``skills.select`` and ``model.route`` send the same thing under the same ceiling:
the budget comes from ``gate.history_budget_chars`` (the smaller of the config and
the keystone), the rows come from a caller-supplied callable so an unsampled turn
pays for no transcript read, and only ``user`` / ``assistant`` text is ever
projected. A second copy of that walk in the second adapter would be a second
place for the budget to be spent differently, which is the one property the
keystone ceiling exists to make checkable.

:func:`as_text` is here for the same reason at a smaller scale: every function in
this package that coerces an UNTYPED payload before a redactor can scan it --
today ``compaction_keep.redacted``, ``memory_recall.scrubbed`` and
``tool_risk.scrubbed`` -- calls it, and a second spelling of that coercion would
be a second answer to what ``None`` sends.

Nothing here imports an adapter: the shared helpers are below the points, so a
broken adapter cannot make another one unimportable.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Sequence

from kiro_crew import decisions as core

logger = logging.getLogger(__name__)

# Skill keys and memory ids must remain exact when passed back to the loader or
# matched against a search result. Drop a key exceeding this bound rather than
# truncating it into a different identifier.
MAX_KEY_CHARS = 120

#: How many prior transcript rows a point will even look at, before the CHAR
#: budget is applied. The budget is what bounds egress; this bounds the READ, so
#: a long conversation cannot turn one decision into a full-file scan. It is the
#: value the caller passes to ``conversation_log.recent``, which serves a slice
#: this small from a tail read rather than a whole-file parse.
MAX_HISTORY_MESSAGES = 20

#: The two roles a prior turn may carry. Tool output is not conversation and is
#: not sent: it is the largest and least selective text in a transcript, and it
#: routinely quotes files the message itself never mentioned.
HISTORY_ROLES = frozenset({"user", "assistant"})


def as_text(text: object) -> str:
    """*text* as a string, with ``None`` reading as empty rather than ``"None"``.

    Every function in this package that hands an UNTYPED payload to a redactor
    calls this -- today ``compaction_keep.redacted``, ``memory_recall.scrubbed``
    and ``tool_risk.scrubbed``, each annotating its text ``object`` because a
    provider payload or a tool argument is whatever the caller had. A bare
    ``str()`` would turn an absent field into the four characters ``None`` and put
    them on the wire.

    ``message_steer.redacted`` cleans text the same way and is deliberately NOT a
    caller: it annotates ``str``, because its own walk drops a row whose content
    is not a string before any of it reaches a redactor.

    A ``str`` is returned unchanged rather than re-stringified, so a ``str``
    subclass survives the call as itself.
    """
    if isinstance(text, str):
        return text
    if text is None:
        return ""
    return str(text)


def history_budget() -> int:
    """``decisions.history_budget_chars`` under the keystone ceiling, or 0.

    0 is the fail-closed direction: it sends the message alone, which is exactly
    what the seam did before prior turns were part of any state.
    """
    try:
        return max(0, int(core.history_budget_chars()))
    except Exception:
        logger.debug("decisions: history budget unreadable", exc_info=True)
        return 0


def prior_turns(
    history_source: Callable[[], Sequence[Mapping[str, Any]]] | None,
) -> list[Mapping[str, Any]]:
    """The caller's prior turns, or an empty list. Never raises.

    Called only after the gates, so a transcript read is paid on the sampled
    turns and nowhere else. A source that raises reads as no history rather than
    as a failed decision: prior turns make the question better, they are not
    what makes it answerable.
    """
    if history_source is None:
        return []
    try:
        return list(history_source() or [])
    except Exception:
        logger.debug("decisions: prior turns unreadable", exc_info=True)
        return []


def build_history(
    history: Sequence[Mapping[str, Any]] | None,
    text: str = "",
    *,
    history_budget_chars: int | None = None,
    trace: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Prior turns as ``[{role, text}]``, newest FIRST, inside the char budget.

    Newest first because that is the order the budget spends in: the turn just
    before this message is the one worth a request, and the oldest reachable turn
    is the one a small budget should drop. Walking from the newest end is also
    what makes the truncation land on the LAST entry admitted rather than on the
    most useful one.

    Only ``user`` and ``assistant`` rows (:data:`HISTORY_ROLES`) are read, so no
    tool output leaves the machine. A row whose text equals *text* is skipped: the
    current turn may already be flushed to the transcript, and the caller drops it
    with ``exclude_last_n``, but a caller that does not must not send the message
    twice.

    *trace* receives ``history_chars`` (the characters actually admitted) and
    ``truncated`` (how many entries were clipped to fit -- at most one, since the
    budget stops the walk). Both are filled even when nothing is admitted, which
    is what makes "no history was reachable" a row that says ``history_chars=0``
    rather than a row missing a field.
    """
    budget = history_budget() if history_budget_chars is None else max(0, int(history_budget_chars))
    rows: list[dict[str, str]] = []
    spent = 0
    truncated = 0
    for entry in reversed(list(history or [])):
        if spent >= budget:
            break
        if not isinstance(entry, Mapping):
            continue
        role = str(entry.get("role", "") or "")
        if role not in HISTORY_ROLES:
            continue
        content = entry.get("content", "")
        if not isinstance(content, str) or not content:
            continue
        if content == text:
            continue
        room = budget - spent
        if len(content) > room:
            content = content[:room]
            truncated += 1
        rows.append({"role": role, "text": content})
        spent += len(content)
    if trace is not None:
        trace["history_chars"] = spent
        trace["truncated"] = truncated
    return rows
