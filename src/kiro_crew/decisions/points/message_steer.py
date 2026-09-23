"""``message.steer`` -- does this mid-turn message change the work, or follow it?

A message typed while a turn is RUNNING can go two ways, and the shipped product
makes the sender choose: the composer's split send button defaults to STEER
(inject into the running turn, ``chat_delivery.steer_into_running_turn``) and the
sender may pick QUEUE instead (hold it for the next turn,
``chat_delivery.queue_for_next_turn``). The choice is a guess about text the
sender has not finished reading -- the agent is mid-reply -- and the wrong guess
costs real work: a steer that was meant as "and afterwards, also do X" cuts a
reply in half, and a queue that was meant as "stop, you are editing the wrong
file" arrives after the damage.

This point asks the oracle that one question and applies the answer. Both arms
already exist and neither is changed: the point only decides WHICH of the two
shipped paths a send takes.

Never behind the sender's back
-----------------------------
It runs only when the sender SELECTED it. The split send button carries a third
mode, ``Auto (Jev)``, drawn only while the seam is consented and the governance
ceiling permits it, and choosing it sends ``steer: "auto"`` instead of
``steer: true``. A manual Steer or Queue never reaches this module: that pick IS
the answer to the question this point asks.

The other conditions are the call site's (``chat_handlers.api_chat``): a turn is
running on the slot, and the send came from the dashboard's own authenticated
human rather than an app token, an integration or a cron. Consent and the
sampling bucket are ``decide``'s own gates.

Everything is a refusal back to STEER
-------------------------------------
:func:`steer_or_queue` returns ``None`` for: the seam is off, the session is not
sampled, the answer is outside the two options, the transport failed, or the
budget expired. ``None`` means "take the path a manual Steer would have taken",
which is what the composer's own default has always done, so the fallback is the
shipped behaviour rather than a third outcome. The caller needs no try/except.

The running turn's text is prior conversation, and is capped as such
-------------------------------------------------------------------
The one thing this question wants that ``skills.select`` never sends is what the
agent is doing RIGHT NOW: the running turn's own request, and the newest assistant
text and tool activity. That is conversation the sender did not type into this
send, so it is spent out of the SAME consented ceiling prior turns are
(``gate.history_budget_chars``: the smaller of what ``config.json`` asks for and
what the keystone recorded the owner reviewing). At the shipped default of 0 the
request therefore carries the new message alone and the decision is made on it --
which is still the question, because a mid-turn message usually says which of the
two it is -- and an owner who raises the ceiling buys the running turn's text,
newest first, request before activity.

There is no ``history`` key at all: prior turns have ENDED, the turn in progress
is what the question is about, and one ceiling spent on both would make the
context worse the further back it reached.

Runs on the event loop
----------------------
The caller is the HTTP handler's coroutine, so the ``decide`` await needs no
cross-thread hand-off: ``gate.decide`` bounds the provider call by
``timeout_secs`` and returns ``None`` on expiry. The ceiling read, the redaction
of the excerpts and the outcome row's append are pushed to threads, because this
coroutine runs on the loop that serves every other session.

The receipt rides the USER row
------------------------------
``skills.select`` publishes through :mod:`kiro_crew.decisions.outcomes`, which
hands ONE outcome per session to whatever finalizes the next ASSISTANT message.
This point deliberately does not: the decision is about the message being sent, so
its receipt belongs on that message's own row, and ``outcomes.consume`` POPS --
claiming that slot here would take the strip a ``skills.select`` turn had already
published and attach it to the wrong reply. So :func:`record_outcome` returns the
row it wrote and the caller stamps it onto the row it is about to append
(``meta.decisions_strip``), which is the same field and the same reader.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Mapping, Sequence

from kiro_crew import decisions as core
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.types import Answer, Choice, Question

logger = logging.getLogger(__name__)

POINT = "message.steer"

#: The question's id, and therefore the key the answer arrives under.
QUESTION_ID = "handling"

#: The two shipped paths, which are the question's CLOSED option domain: the gate
#: refuses an answer outside a question's declared options, so a provider
#: inventing a third reads as an invalid result rather than as a branch nobody
#: wrote.
CHOICE_STEER = "steer"
CHOICE_QUEUE = "queue"
CHOICES: tuple[str, ...] = (CHOICE_STEER, CHOICE_QUEUE)

#: What a refusal keeps, and what the outcome row records as the arm the product
#: would have taken without the seam. Named rather than spelled inline at three
#: sites: it is the composer's shipped default, and the row's own comparison arm.
BASELINE = CHOICE_STEER

#: Characters of the new message sent with the question -- the SAME bound
#: ``skills.select`` applies, because it is the same kind of text answering a
#: question about the same turn, and two different excerpt sizes would mean the
#: consent text describes one of them.
MAX_MESSAGE_CHARS = 2000

#: Ceilings on the two halves of the running turn, applied ON TOP of the consented
#: budget they share. The budget is the authorization; these are the shape, so a
#: generous ceiling still cannot spend everything on one half.
MAX_RUNNING_MESSAGE_CHARS = 1000
MAX_ACTIVITY_CHARS = 1500

#: How many transcript rows the activity walk will even look at. The char budget
#: is what bounds egress; this bounds the READ, so a long-running turn that has
#: printed thousands of rows cannot turn one send into a whole-list scan.
MAX_ACTIVITY_ROWS = 40

#: Rows the activity excerpt is built from. ``chunk`` is the live, unflushed text
#: of the reply in progress, which is exactly the part a sender is reacting to.
#:
#: ``thinking`` is NOT here: private reasoning is the largest and least reviewed
#: text a turn produces, and the question is answerable without it.
ACTIVITY_ROLES = frozenset({"assistant", "chunk", "tool"})

#: The row that ends the walk backwards. Everything after it belongs to the
#: running turn; the row itself is that turn's own request.
REQUEST_ROLE = "user"


def questions() -> list[Question]:
    """The one question, with both paths described in the prompt.

    ONE ``Choice``, because the answer is consumed: the send takes exactly one of
    the two paths, and a second question would be a second thing to reconcile
    with a single dispatch.

    The descriptions name the EFFECT on the work, never the button: the oracle is
    judging whether the new text corrects what is happening now or asks for
    something after it, and naming a UI control would invite it to guess at what
    the sender clicked instead.
    """
    return [
        Choice(
            QUESTION_ID,
            "A new message arrived while the assistant is still working on the "
            "previous one. Answer "
            f"{CHOICE_STEER} if it changes or corrects what the assistant is "
            "doing right now, so the work in progress should be interrupted with "
            f"it; answer {CHOICE_QUEUE} if it is a separate later request, so the "
            "work in progress should finish first and the new message run after "
            "it.",
            options=list(CHOICES),
        )
    ]


def turn_budget() -> int:
    """Characters of the RUNNING turn this decision may carry, or 0.

    The consented history ceiling (``gate.history_budget_chars``: the smaller of
    the config's ask and what the keystone recorded the owner reviewing), spent on
    the turn in progress instead of on turns that have ended. 0 whenever either
    side is unreadable, which is the shipped default and sends the new message
    alone.

    Filesystem IO (the keystone read), so a caller on the event loop hands it to a
    thread.
    """
    try:
        return max(0, int(core.history_budget_chars()))
    except Exception:
        logger.debug("message.steer: turn budget unreadable; sending no turn text")
        return 0


def redacted(text: str, limit: int) -> str:
    """*text* with credentials and exfiltration URLs removed, clipped to *limit*.

    The canonical redactors, not the gate's scrub: the two answer different
    questions. The gate REFUSES a whole request that carries a credential, which
    is right for text a caller chose to send; this excerpt is whatever the agent
    printed into a running turn, so refusing on it would turn "the reply happened
    to quote an env file" into "the seam silently stops deciding". Cleaning
    instead keeps the decision available and still sends no secret -- and the
    gate's own scan still runs over the cleaned result, so a spelling this pass
    misses refuses the request rather than sending it.

    Redaction runs over the WHOLE text and the tail is taken AFTER it, never
    before: a cut placed inside a secret leaves a fragment neither redactor
    matches, and that fragment is what would go on the wire. The clip is a tail
    rather than a head because the newest text is the part the decision is about.
    Never raises: a redactor that fails yields nothing rather than unredacted
    text, which is the only safe direction for text about to leave the machine.
    """
    if not text or limit <= 0:
        return ""
    try:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        cleaned, _warnings = redact_credentials(str(text))
        cleaned, _warnings = redact_exfiltration_urls(cleaned)
    except Exception:
        logger.debug("message.steer: redaction failed; sending no turn text", exc_info=True)
        return ""
    return cleaned[-limit:]


def running_turn_excerpts(rows: Sequence[Mapping[str, Any]], budget: int) -> tuple[str, str]:
    """The running turn's ``(request, activity)`` excerpts, inside *budget*.

    A pure walk over the transcript tail -- newest first -- so it is testable
    without a slot and cannot alter one. It stops at the first
    :data:`REQUEST_ROLE` row, whose content is the running turn's own request:
    everything after that row was produced BY this turn, and everything before it
    belongs to a turn that has already ended.

    *budget* is the consented ceiling (:func:`turn_budget`) and covers BOTH halves
    together, because both are conversation the sender did not type into this send.
    It is spent newest-first within the activity and on the request before the
    activity: the request is what the activity is an answer to, so a budget too
    small for both buys the more useful half. At 0 both halves are empty.

    Both halves come back REDACTED and THEN clipped, in that order, so a caller can
    forget neither and no cut can halve a secret into a fragment the scanners miss.
    Never raises: an unreadable row is skipped, and an unreadable list yields two
    empty strings, which is a question about the new message alone.
    """
    if budget <= 0:
        return "", ""
    request = ""
    pieces: list[str] = []
    spent = 0
    activity_room = max(0, min(MAX_ACTIVITY_CHARS, budget))
    try:
        tail = list(rows or [])[-MAX_ACTIVITY_ROWS:]
    except Exception:
        logger.debug("message.steer: transcript tail unreadable", exc_info=True)
        return "", ""
    for entry in reversed(tail):
        if not isinstance(entry, Mapping):
            continue
        role = str(entry.get("role", "") or "")
        content = entry.get("content", "")
        if not isinstance(content, str) or not content:
            # A role-only row (a turn boundary, an empty flush) still ENDS the
            # walk when it is the request row: the boundary is what the role
            # means, not what the row carries.
            if role == REQUEST_ROLE:
                break
            continue
        if role == REQUEST_ROLE:
            request = content
            break
        if role not in ACTIVITY_ROLES or spent >= activity_room:
            continue
        # The row is taken WHOLE. Clipping it here would cut before the redactors
        # run, and a cut inside a secret leaves a fragment neither pattern matches
        # -- the fragment, not the secret, is then what goes on the wire. What
        # bounds the send is still the budget, applied by `redacted` AFTER it has
        # scanned; what this loop bounds is how much text reaches the scanners,
        # which is the budget plus the row that crosses it.
        pieces.append(content)
        spent += len(content)
    pieces.reverse()
    # The REQUEST is served first out of the shared budget, then whatever is left
    # pays for the activity: the request is the shorter half and the one the
    # activity only makes sense against.
    request_text = redacted(request, min(MAX_RUNNING_MESSAGE_CHARS, budget))
    activity_text = redacted("\n".join(pieces), max(0, budget - len(request_text)))
    return request_text, activity_text


def build_state(
    text: str,
    *,
    running_message: str = "",
    activity: str = "",
) -> dict[str, Any]:
    """The state sent to the oracle: the new message, and what is happening now.

    ``running_turn`` is OMITTED when neither of its halves is known, and each half
    is omitted when it is empty, the same shape rule ``skills.select`` follows for
    ``history``: a key whose value is always present but sometimes empty tells a
    reader nothing the local row does not already say, and it changes the wire for
    every consented owner. At the shipped ceiling of 0 the request is therefore the
    new message alone.
    """
    state: dict[str, Any] = {"message": (text or "")[:MAX_MESSAGE_CHARS]}
    running: dict[str, Any] = {}
    if running_message:
        running["message"] = running_message
    if activity:
        running["activity"] = activity
    if running:
        state["running_turn"] = running
    return state


def read_choice(answers: Any) -> str:
    """The path the answer names, or ``""``. Identity is exact.

    The gate has already held the value against the declared options, so this is
    the second check rather than the only one -- and it is here because the caller
    branches on it: a near-miss spelling read as a steer would inject text the
    oracle asked to be queued.
    """
    if not isinstance(answers, dict):
        return ""
    answer = answers.get(QUESTION_ID)
    if not isinstance(answer, Answer):
        return ""
    value = answer.value
    return value if isinstance(value, str) and value in CHOICES else ""


def probability_of(answers: Any) -> float | None:
    """The answer's probability, or ``None``. Only read after :func:`read_choice`."""
    if not isinstance(answers, dict):
        return None
    answer = answers.get(QUESTION_ID)
    return answer.p if isinstance(answer, Answer) else None


async def steer_or_queue(
    text: str,
    *,
    session_key: str | None = None,
    rows: Sequence[Mapping[str, Any]] = (),
    config: Any | None = None,
) -> dict[str, Any] | None:
    """Which path this send should take, or ``None`` to take the shipped default.

    Returns the outcome the caller applies and the strip renders:
    ``{turn_id, choice, p, latency_ms, baseline}``. ``choice`` is one of
    :data:`CHOICES`; there is no third value, and no answer at all is ``None``
    rather than a choice of :data:`BASELINE`, so a caller can tell "the oracle
    said steer" from "nothing was asked or nothing came back" -- the first is a
    decision with a receipt, the second is the product behaving as it always has.

    *rows* is the slot's transcript list. It is SLICED and read off the event loop
    (:func:`running_turn_excerpts`), and nothing is written back, so a caller hands
    over the live list rather than copying it.

    Never raises except :class:`asyncio.CancelledError`, which ``decide``
    propagates: cancellation is the request going away, not a decision failure.
    """
    turn_id = uuid.uuid4().hex[:16]
    # Bound before the try so the latency is always measurable, including when the
    # excerpt work itself was what took the time.
    started = time.monotonic()
    try:
        # The ceiling FIRST, off the loop: it reads the keystone as well as the
        # config, and at the shipped default of 0 there is nothing for the walk
        # and the redaction to contribute.
        budget = await asyncio.to_thread(turn_budget)
        if budget > 0:
            running_message, activity = await asyncio.to_thread(running_turn_excerpts, rows, budget)
        else:
            running_message, activity = "", ""
        state = build_state(text, running_message=running_message, activity=activity)
        extra: dict[str, Any] = {
            "turn_id": turn_id,
            "turn_chars": len(running_message) + len(activity),
        }
        answers = await core.decide(
            POINT, state, questions(), session_key=session_key, config=config, extra=extra
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # This sits on the path that accepts a message into a busy slot, so the
        # seam may cost an observation and must never cost a send.
        logger.debug("message.steer: taking the shipped default", exc_info=True)
        return None
    latency_ms = int((time.monotonic() - started) * 1000)
    choice = read_choice(answers)
    if not choice:
        return None
    return {
        "turn_id": turn_id,
        "choice": choice,
        "p": probability_of(answers),
        "latency_ms": latency_ms,
        "baseline": BASELINE,
    }


def build_outcome(decided: Mapping[str, Any]) -> dict[str, Any]:
    """The fields the outcome row and the strip share, off :func:`steer_or_queue`.

    ``latency_ms`` is deliberately NOT here: it is a core row field
    (:func:`~kiro_crew.decisions.log.build_row`), so the row carries it at top
    level and an ``extra`` naming it would be dropped.
    """
    return {
        "turn_id": decided.get("turn_id"),
        "choice": decided.get("choice"),
        "baseline": str(decided.get("baseline") or BASELINE),
        "p": decided.get("p"),
    }


def record_outcome(session_key: str | None, decided: Mapping[str, Any]) -> dict[str, Any] | None:
    """Write one outcome row for the decided send and RETURN it. Never raises.

    ``None`` means no row was written -- the build raised, or ``append`` refused
    it (the day-file ceiling, a sealed directory). The caller stamps nothing in
    that case, which is the same rule ``skills.select`` follows for its publish: a
    strip whose durable row was refused describes a decision no verdict could be
    filed against, and the thumbs on this line POST that turn id.

    Returned rather than published through :mod:`kiro_crew.decisions.outcomes` for
    the reason the module docstring gives: that registry's one slot per session
    belongs to the next assistant reply, and this receipt belongs to the user row
    the caller is about to append.

    Blocking (the append is filesystem IO), so a caller on the event loop hands it
    to a thread.
    """
    try:
        row = _log.build_row(
            point=POINT,
            session_key=session_key,
            latency_ms=int(decided.get("latency_ms") or 0),
            extra=build_outcome(decided),
        )
        written = _log.append(row)
    except Exception:
        logger.debug("message.steer: could not record the outcome row", exc_info=True)
        return None
    if not written:
        logger.debug("message.steer: outcome row was not written; stamping nothing")
        return None
    return row
