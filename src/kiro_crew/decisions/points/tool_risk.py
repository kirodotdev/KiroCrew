"""``tool.risk`` -- how risky is the tool call this session is about to run?

An ANNOTATION, and nothing else. The permission DECISION is not consulted and not
altered: the answer becomes a badge on the tool card and a row in the decision
log, and a call the policy allows is still allowed with the tier ``risky`` on it,
while a call the policy refuses is still refused with ``safe`` on it. That is the
whole contract, and it is the reason this point may run at all -- an oracle answer
is an observation, and an observation must never become a gate.

Two things the contract does NOT claim, because neither is true. It is not free of
TIMING: the caller awaits this point in the ``tool_call`` branch, which the harness
sends BEFORE it asks for permission, so a sampled call's approval is answered up to
the wait budget later than it otherwise would be. And a badge is not a verdict on
whether the call ran -- a host gate, a ``PreToolUse`` hook or a batch cascade can
refuse a call that already carries one, and the SEL tool-invocation audit is where
that outcome is recorded. What is unchanged is the decision itself: same inputs,
same branches, same answer.

Why it only runs where nothing asks the human
---------------------------------------------
The caller gates on the session AUTO-APPROVING its tool calls (``trust`` or
YOLO). In a session that prompts, the human reads the call and is the
annotation; adding a second opinion to a card they are already judging would be
noise beside a control. In a trusting session nothing stops to describe what is
about to run, so a badge is the only signal there is, and the only place a reader
learns that the seam looked at all.

What ``policy`` on the row means, and what it deliberately does not
------------------------------------------------------------------
``policy`` is the grant that answers this call -- ``yolo``, ``trust`` or
``trust_scope``, the caller's own descriptive spelling. It is NOT a per-call
verdict, and it cannot be: the harness sends the ``tool_call`` notification
BEFORE it asks for permission (``acp/_dispatch.permission_event``, "the preceding
``tool_call`` notification"), so the card this record rides exists before any
verdict about it does. A later refusal -- a host gate, a ``PreToolUse`` hook, a
batch cascade -- is recorded by the SEL tool-invocation audit, which is where a
per-call outcome already lives. Putting a guess at it here would give the log two
descriptions of one call and make the weaker one look authoritative.

A SECOND consent, not a wider reading of the first
-------------------------------------------------
This point sends a category ``skills.select`` never did -- the tool's name and its
arguments -- so consent to send is not consent to send this. The keystone records
the two separately (``consent.consented_tool_args``), absent reads as NOT
consented, and ``gate._scope_consented`` refuses the point without it. An install
that consented before the scope existed is therefore INERT here rather than
retroactively signed up, which is the same argument that gave
``history_budget_chars`` a keystone ceiling instead of a config value.

Everything is a refusal back to "no badge"
------------------------------------------
:func:`risk_record` returns ``None`` for: the seam off, the session unsampled, the
tool-argument scope not consented to, the turn cap reached, a scrubbed or failed
call, an unusable answer, a ``safe`` verdict, and a ``risky`` verdict the provider
was not confident about. So an ordinary tool card is byte-identical to the one this
build appends today, and a caller needs no try/except and no feature check.

``safe`` is a refusal to BADGE, not a refusal to record: the row is written with
``tier="safe"``, because "the seam looked and thought it was fine" is the answer
that makes the other two readable, and a badge on every card would cost the
annotation its meaning.

An unconvinced ``risky`` is refused the same way and recorded the same way. The
tier is not the flag on its own: ``p`` is the probability the provider assigned to
the option it CHOSE, so a ``risky`` at 0.5 is a coin flip about the most alarming
word in the domain, and :data:`RISKY_CONFIDENCE_THRESHOLD` is where this build
stops printing one. The row still carries ``tier="risky"``, so the suppressed
answers stay countable -- which is how the threshold was measured and the only way
the next one can be.

Two bounds, because a turn can call many tools
---------------------------------------------
One oracle call per tool call, and at most :data:`MAX_CALLS_PER_TURN` per turn.
The wait is the same shape every point uses (``timeout_secs`` plus
:data:`WAIT_MARGIN_SECS`, clamped into :data:`MIN_WAIT_SECS` ..
:data:`MAX_WAIT_SECS`); the per-turn cap is what keeps a tool-heavy turn from
spending that budget twenty, or two hundred, times over. The call that CROSSES
the cap writes one row saying so, so a reader can tell "this turn stopped being
annotated" from "this turn was never sampled"; the calls after it write nothing.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from typing import Any

from kiro_crew import decisions as core
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.types import Answer, Choice, Question

logger = logging.getLogger(__name__)

POINT = "tool.risk"

#: The one question's identifier, and the domain it may be answered inside.
QUESTION_ID = "risk"

TIER_SAFE = "safe"
TIER_CAUTION = "caution"
TIER_RISKY = "risky"

#: Every admissible answer, in increasing severity. The ORDER is part of the
#: contract: the frontend reads the tier as a name, but a reader comparing two
#: rows needs to know which way severity runs without consulting prose.
TIERS = (TIER_SAFE, TIER_CAUTION, TIER_RISKY)

#: The tiers that may earn a badge. ``safe`` is absent deliberately -- see the
#: module docstring. Membership is NECESSARY and not sufficient: ``risky`` must
#: also clear :data:`RISKY_CONFIDENCE_THRESHOLD`, and :func:`earns_badge` is the
#: one function that answers the whole question.
FLAGGED_TIERS = (TIER_CAUTION, TIER_RISKY)

#: Confidence a ``risky`` answer needs before it reaches the card.
#:
#: 0.80, and measured rather than picked. Over this build's own day-files -- 40
#: answered ``risky`` calls, median 0.795, so the tier does NOT arrive confident --
#: every answer at or above 0.80 named a push, a force-push, an upload or an
#: opened pull request, which is what the rubric's own sentence asks for. The
#: answers below 0.62 were a queued monitor edit, a message to another session and
#: a comment posted on a pull request: none of those destroy data, touch
#: credentials or leave the workspace, and a badge that fires on them is the
#: failure mode a badge has. 0.90 was measured too and REJECTED -- it drops eleven
#: of the true ones, force-pushes and a credential fix among them, and a flag that
#: misses a force-push is worse than no flag. The clean part of the reading is the
#: gap: nothing was answered between 0.62 and 0.76.
#:
#: n was 40, so this is a floor with evidence rather than a tuned optimum, and the
#: suppressed rows are what a later reading re-derives it from.
#:
#: A CONSTANT, not a setting, for the reason ``memory_recall.KEEP_THRESHOLD`` is
#: one: it is the meaning of the answer rather than a knob, and a configurable
#: threshold would be a second, undocumented way to make the point a no-op
#: (``1.0`` badges nothing) without turning the seam off.
RISKY_CONFIDENCE_THRESHOLD = 0.80

#: The rubric, sent as the question's prompt. One sentence per tier, because the
#: tiers are the answer domain and a domain nobody defined is a domain every
#: provider reads differently.
PROMPT = (
    "How risky is this tool call for the person whose machine it runs on? "
    f"Answer {TIER_SAFE} when it only reads or reports. "
    f"Answer {TIER_CAUTION} when it changes something inside the workspace "
    "that is easy to put back. "
    f"Answer {TIER_RISKY} when it destroys data, touches credentials, sends "
    "local data out over the network, or acts outside the workspace."
)

#: Longest tool name sent or recorded. A name is an identifier the harness chose,
#: so this is a bound rather than a truncation anyone should see.
MAX_TOOL_CHARS = 120

#: Longest rendered arguments sent. Tool arguments are the largest and least
#: predictable part of this state -- a file body, a diff, a whole shell script --
#: so they are clipped before the request rather than bounded only by the wire.
MAX_ARGUMENT_CHARS = 2000

#: Longest excerpt of the message that led to this call.
MAX_MESSAGE_CHARS = 2000

#: Oracle calls one turn may make at this point. A turn that calls twenty tools
#: has already spent twenty wait budgets; past that the annotation is not worth
#: the turn it is annotating.
MAX_CALLS_PER_TURN = 20

#: Scheduling slack on top of the provider budget, and the floor and ceiling the
#: wait is clamped into. The ceiling is the real protection: this budget is spent
#: on the turn's critical path, so a hand-edited ``timeout_ms`` must not hold a
#: tool card there. Held equal to the values ``skills.select`` waits by
#: (``test_decisions_tool_risk.py``) so every point in this package waits in one
#: shape rather than each inventing its own.
WAIT_MARGIN_SECS = 0.5
MIN_WAIT_SECS = 0.25
MAX_WAIT_SECS = 10.0

#: How long the outcome row's write may hold the caller, on top of the provider
#: budget. The write is one ``O_APPEND`` of a few hundred bytes, so this exists
#: only so a stalled filesystem cannot make an observation cost the turn. Named
#: and valued as ``gate._LOG_BUDGET_SECS`` is, because it is the same write on the
#: same loop -- this module runs on the event loop, unlike ``skills.select``, which
#: appends from the executor thread that assembles the prompt.
LOG_BUDGET_SECS = 0.05

#: Row ``error`` when the turn cap stopped the annotation. Written once per turn,
#: by the call that crossed the cap, so "this turn stopped being annotated" is
#: visible instead of indistinguishable from an unsampled one.
ERROR_TURN_CAP = "turn-cap"


def earns_badge(tier: str, p: float) -> bool:
    """Whether this answer reaches the tool card. The ONE place that is decided.

    ``caution`` badges on its tier alone: it is the mild word -- "inside the
    workspace, easy to put back" -- so a hesitant one costs a reader almost
    nothing. ``risky`` must also clear :data:`RISKY_CONFIDENCE_THRESHOLD`, because
    it is the alarming word and an alarm nobody believes is what costs every other
    badge its meaning.

    The consequence is deliberate and is not an ordering slip: an unconvinced
    ``risky`` draws NOTHING while a ``caution`` at the same confidence draws a
    badge. The two words are not two points on one scale here. They name different
    claims -- ``caution`` is about the workspace, ``risky`` is about data,
    credentials and this machine's edge -- and only the second one is expensive to
    be wrong about. Reading an unconvinced ``risky`` DOWN to ``caution`` was the
    alternative and is not what this does: it would put "easy to put back" on a
    call the provider was describing as a force-push, which is a claim nobody
    made.
    """
    if tier == TIER_RISKY:
        return p >= RISKY_CONFIDENCE_THRESHOLD
    return tier in FLAGGED_TIERS


def wait_budget() -> float:
    """How long one call may take, clamped into a sane window. Never raises."""
    try:
        budget = float(core.timeout_secs()) + WAIT_MARGIN_SECS
    except Exception:
        logger.debug("tool.risk: provider budget unreadable", exc_info=True)
        return MIN_WAIT_SECS
    if not math.isfinite(budget):
        return MIN_WAIT_SECS
    return min(max(budget, MIN_WAIT_SECS), MAX_WAIT_SECS)


def scrubbed(text: object, limit: int) -> str:
    """*text* as at most *limit* characters with credentials and exfiltration URLs replaced.

    The canonical redactors, not the gate's scanner, and that difference is the
    design. The gate REFUSES a request carrying a credential, which is right for a
    message excerpt: a secret there is a finding. Tool arguments are different --
    a credential in an argument is ORDINARY (an ``aws`` command, a curl header),
    so refusing on it would mean the point never fires on exactly the calls most
    worth annotating. Replacing it with the shipped placeholder keeps the request
    describable and leaves nothing to leak; the gate then scans the placeholder
    and passes.

    Clipped AFTER redaction, never before: clipping first can cut a secret in
    half, and a half is a fragment neither redactor matches.
    """
    raw = text if isinstance(text, str) else ("" if text is None else str(text))
    if not raw:
        return ""
    try:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        cleaned, _ = redact_exfiltration_urls(raw)
        cleaned, _ = redact_credentials(cleaned)
    except Exception:
        # A scan that did not complete cannot clear text for the wire. Sending
        # nothing is a worse question, not a worse outcome: the gate would refuse
        # the request anyway, and this keeps the refusal free of a provider call.
        logger.debug("tool.risk: redaction failed; dropping the field", exc_info=True)
        return ""
    return cleaned[:limit]


def build_state(tool: str, arguments: str, message: str = "") -> dict[str, str]:
    """The state one question is asked about: the tool, its arguments, the message.

    Every field is bounded and redacted by :func:`scrubbed` before it gets here or
    inside it. ``message`` is OMITTED when empty rather than sent as ``""``, for
    the reason ``skills.select`` omits an empty ``history``: a field that is
    always present but sometimes meaningless makes the wire shape say less than
    its absence would.
    """
    state: dict[str, str] = {
        "tool": scrubbed(tool, MAX_TOOL_CHARS),
        "arguments": scrubbed(arguments, MAX_ARGUMENT_CHARS),
    }
    excerpt = scrubbed(message, MAX_MESSAGE_CHARS)
    if excerpt:
        state["message"] = excerpt
    return state


def questions() -> list[Question]:
    """The one question, with the three tiers as its whole domain."""
    return [Choice(QUESTION_ID, PROMPT, options=list(TIERS))]


def read_answer(answers: Any) -> tuple[str, float] | None:
    """``(tier, p)`` for an answer inside :data:`TIERS`, else ``None``.

    The gate already checked the value against the declared options, so this is
    the second reading rather than the only one -- and it is exact, because the
    tier decides what a reader is told about a call that is already running.
    """
    if not isinstance(answers, dict):
        return None
    answer = answers.get(QUESTION_ID)
    if not isinstance(answer, Answer):
        return None
    value = answer.value
    if not isinstance(value, str) or value not in TIERS:
        return None
    if not isinstance(answer.p, (int, float)) or isinstance(answer.p, bool):
        return None
    return value, float(answer.p)


async def risk_record(
    *,
    tool: str,
    arguments: str,
    message: str = "",
    policy: str,
    session_key: str | None = None,
    calls_this_turn: int = 1,
) -> dict[str, Any] | None:
    """The badge record for one tool call, or ``None`` to leave the card alone.

    ``None`` is the only failure signal and it is never exceptional: the seam
    off, an unsampled session, the turn cap, a scrub, a timeout, a provider
    failure, an unusable answer and a ``safe`` verdict all return it. A caller
    therefore writes ``record = await risk_record(...)`` and stamps it only when
    it is truthy.

    *calls_this_turn* is this call's 1-based position in the turn. The caller
    already counts tool calls per turn, so the cap is enforced against the
    caller's own counter rather than against per-session state this module would
    have to bound and expire.

    Returns the ROW that was written, so the badge on the card and the line in
    the log cannot become two descriptions of one call -- the same rule
    ``skills.select`` publishes its outcome by.

    Never raises. This runs on the turn path of a call that is already approved,
    so it may cost an observation and must never cost the call.
    """
    try:
        if calls_this_turn > MAX_CALLS_PER_TURN:
            # Only the call that CROSSES the cap says so, and only when the seam
            # is on for this session: a row is a finding, and an unsampled turn
            # must keep writing nothing at all.
            if calls_this_turn == MAX_CALLS_PER_TURN + 1:
                await _record_turn_cap(session_key=session_key, calls=calls_this_turn)
            return None
        turn_id = uuid.uuid4().hex[:16]
        state = build_state(tool, arguments, message)
        asked = questions()
        # Bound to a local str, not read back out of `extra`: the mapping's values
        # are a mix of strings and counts, so a value taken from it is typed as the
        # union and the outcome row's own `policy` is a string.
        bounded_policy = str(policy)[:MAX_TOOL_CHARS]
        extra: dict[str, Any] = {
            "turn_id": turn_id,
            "tool": state["tool"],
            "arg_chars": len(state["arguments"]),
            "policy": bounded_policy,
            "call_index": calls_this_turn,
        }
        started = time.monotonic()
        answers = await _ask(state, asked, session_key=session_key, extra=extra)
        if answers is None:
            return None
        read = read_answer(answers)
        if read is None:
            return None
        tier, p = read
        return await _record_outcome(
            session_key=session_key,
            latency_ms=int((time.monotonic() - started) * 1000),
            turn_id=turn_id,
            tool=state["tool"],
            tier=tier,
            p=p,
            policy=bounded_policy,
        )
    except Exception:
        logger.debug("tool.risk: leaving the tool card unannotated", exc_info=True)
        return None


async def _ask(
    state: dict[str, str],
    asked: list[Question],
    *,
    session_key: str | None,
    extra: dict[str, Any],
) -> Any:
    """One bounded ``decide``, or ``None``.

    The gate enforces ``timeout_secs`` on the provider itself; this outer wait is
    the caller-side budget the module docstring names, so a stalled log write or a
    slow keystone read cannot hold the tool card past :data:`MAX_WAIT_SECS`
    either.
    """
    try:
        return await asyncio.wait_for(
            core.decide(POINT, state, asked, session_key=session_key, extra=extra),
            timeout=wait_budget(),
        )
    except asyncio.TimeoutError:
        # The gate writes its own row for a provider timeout; this arm is the
        # caller-side budget expiring around it, which means the gate is still
        # inside its own and will record whatever it finds.
        logger.debug("tool.risk: the call outlived the caller's budget")
        return None


async def _record_outcome(
    *,
    session_key: str | None,
    latency_ms: int,
    turn_id: str,
    tool: str,
    tier: str,
    p: float,
    policy: str,
) -> dict[str, Any] | None:
    """Write the outcome row; return it only when the tier earns a badge.

    The row is written for EVERY answered call, ``safe`` included, because the
    log is the observation and a log that only records the alarming answers
    cannot say how often the seam is wrong. The RETURN is the badge, so the
    transcript is quiet about a call the oracle thought was fine.

    ``flagged`` on the row is what a reader was SHOWN, so it is
    :func:`earns_badge` and not the tier: a row saying ``flagged`` beside a card
    that drew nothing would be the two-descriptions-of-one-call failure the
    ``policy`` field's own paragraph refuses. The tier stays on the row either
    way, so a suppressed ``risky`` is still countable.

    A row that was not written returns ``None`` whatever the tier: a badge whose
    durable row was refused carries a ``turn_id`` no verdict could be filed
    against, which is the rule ``skills.select`` publishes its strip by. A write
    that did not finish inside :data:`LOG_BUDGET_SECS` is treated the same way,
    for the same reason: this caller cannot tell a slow write from a refused one,
    and the badge must not outlive the row a verdict would be filed against.

    OFF THE LOOP, and that is not optional here. ``skills.select`` appends
    synchronously because it runs on the executor thread that assembles the
    prompt; this point is an ``async def`` awaited by ``_run_chat``, so a
    synchronous append would put a lock wait and a filesystem write on the
    gateway's event loop -- the failure ``no-blocking-call-on-event-loop`` exists
    for. The bound on top is the shape ``gate._write`` already uses for the same
    write on the same loop.
    """
    badge = earns_badge(tier, p)
    row = _log.build_row(
        point=POINT,
        session_key=session_key,
        latency_ms=latency_ms,
        extra={
            "turn_id": turn_id,
            "tool": tool,
            "tier": tier,
            "p": p,
            "policy": policy,
            "flagged": badge,
        },
    )
    try:
        written = await asyncio.wait_for(asyncio.to_thread(_log.append, row), LOG_BUDGET_SECS)
    except asyncio.TimeoutError:
        # The worker thread is NOT cancellable, so the append may still land
        # afterwards -- acceptable for a write that cannot corrupt a line, and the
        # row is the durable record either way. What this arm refuses is the
        # BADGE, because the caller stopped waiting for its row.
        logger.debug("tool.risk: outcome row outlived its write budget; leaving the card alone")
        return None
    if not written:
        logger.debug("tool.risk: outcome row was not written; leaving the card alone")
        return None
    return row if badge else None


async def _record_turn_cap(*, session_key: str | None, calls: int) -> None:
    """One ``ERROR_TURN_CAP`` row, and only for a session the seam is on for.

    The enable check is what keeps an unsampled turn writing nothing: this is the
    one row in this module that is not produced by a ``decide`` call, so it does
    not inherit the gate's own refusals and has to ask for them. Read off the
    event loop, because the keystone is a file.
    """
    try:
        enabled = await asyncio.to_thread(core.is_enabled, POINT, session_key=session_key)
        if not enabled:
            return
        await asyncio.to_thread(
            _log.append,
            _log.build_row(
                point=POINT,
                session_key=session_key,
                latency_ms=0,
                error=ERROR_TURN_CAP,
                extra={"calls": calls},
            ),
        )
    except Exception:
        logger.debug("tool.risk: could not record the turn cap", exc_info=True)
