"""``memory.recall`` — which recalled memories are worth returning?

The agent recalls memory on demand, through the ``memory_recall`` tool: the tool
calls ``GET /api/memory/recall``, the route calls ``VectorMemoryStore.recall``, and
that searches episodes by vector similarity, drops what falls under the
length-aware cosine gate, and bounds the survivors to a char budget. Similarity is
the only judgement made — a memory that is ABOUT the same words as the question is
returned whether or not it answers it, and every returned episode is paid for out
of the tool's response budget and then out of the model's window.

This point asks the oracle the second question, per candidate: keep this one, or
drop it. Jev's kept set is what the tool returns.

Two halves, deliberately split by thread
----------------------------------------
:func:`kept_memories` is synchronous and runs on the caller's thread — the recall
route reaches ``VectorMemoryStore.recall`` through ``run_in_embed_pool``, a thread
executor. The redaction of each snippet, the consent read and the outcome row all
happen on that worker thread, never on the event loop that serves the gateway.
Only the ``decide`` await is submitted to the loop, and the caller waits for a
bounded budget. Same shape, same reason, as ``points/skills_select.py``.

A SECOND consent, because this is a new category
------------------------------------------------
A recalled memory is not the text the main switch describes. That consent is
recorded against a message excerpt — text the owner just typed — and skill
descriptions, which this build shipped. A recalled memory is text the AGENT wrote
down turns or days ago, about work the owner was not reviewing when they flipped
the switch. So the keystone records a scope of its own, ``memory_text``
(``consent.consented_memory_text``), default FALSE, and
``gate.POINT_EGRESS_SCOPES`` refuses this point without it — which means an
install consented before the scope existed is INERT here rather than
retroactively signed up. The refusal arrives as ``core.is_enabled`` answering
False, so it costs no redaction and writes no row, exactly like an unsampled
call: the three cheap refusals touch no disk by design.

Everything is a REFUSAL back to the baseline
--------------------------------------------
:func:`kept_memories` returns ``None`` for "return exactly what the recall found"
— the point is off, this session is not sampled, this is not an owner dashboard
call, the candidate list is empty, the answer is unusable, the transport failed,
the budget expired, or there is no usable loop. It returns a LIST only for a real
answer, and that list may legitimately be empty: "none of these answer the
question" is an answer, not a failure.

Only a SUBSET, never a re-ranking and never a widening
------------------------------------------------------
The candidates are exactly the rows the recall already chose, in the order it
chose them, and the answer can only remove some of them. Two reasons. The order
is the ranker's, and a keep/drop answer says nothing about order, so reordering
on it would discard a judgement for one that was never made. And widening would
mean offering rows the relevance gate or the char budget already refused, which
is a different question — is this relevant at all, does it fit — asked of a model
holding neither the embeddings nor the budget. The store enforces both bounds
itself: the hook is handed the rows its ``fit`` walk selected, and
``_kept_episodes`` discards an answer naming anything else.

Both arms, every sampled call
-----------------------------
The baseline arm is the candidate list itself, so knowing what the recall would
have returned costs nothing. Jev's kept set is what the tool returns; the baseline
is recorded beside it with ``agree``, the mean keep probability and the characters
the narrower response saves. There is no shadow mode: the arm that is returned is
always Jev's.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import logging
import math
import time
import uuid
from typing import Any, Callable, Mapping, Sequence, TypeVar

from kiro_crew import decisions as core
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.points import MAX_KEY_CHARS
from kiro_crew.decisions.types import Answer, Choice, Question

logger = logging.getLogger(__name__)

POINT = "memory.recall"

#: One recalled-memory row, as whatever mapping type the caller holds.
#:
#: A TypeVar rather than a plain ``Mapping[str, Any]`` because this point returns a
#: SUBSET OF THE SAME OBJECTS it was handed -- the identity contract
#: ``vector_memory._kept_episodes`` judges membership on. Saying ``Mapping`` would
#: throw that away and hand a ``list[dict]`` caller back something it could not put
#: where its own rows go, which is exactly the seam the store's ``keep=`` parameter
#: is. So the bound travels with the value: a store passing ``list[dict]`` gets
#: ``list[dict]``, and the type now states what the docstrings promise.
RowT = TypeVar("RowT", bound=Mapping[str, Any])

#: Cap on how many recalled memories are described to the oracle. One question per
#: candidate, so this bounds the request's question count as well as its size. The
#: shipped ``memory.episodic_max_results`` is well under it; a configuration that
#: raised it far higher would have the tail dropped rather than sent.
MAX_CANDIDATES = 20

#: The message excerpt that leaves the machine, same bound ``skills.select`` and
#: ``message.steer`` apply, so one owner reviewed one number.
MAX_MESSAGE_CHARS = 2000

#: How much of each memory is described. A snippet, not the memory: the question
#: is whether this row is worth its place in the block, and the opening of a
#: fragment is what answers it. Applied AFTER redaction — see :func:`scrubbed`.
MAX_SNIPPET_CHARS = 200

#: The two answers one candidate may carry. A Choice rather than a score because
#: ``Choice`` is the only question type ``impl_jev`` speaks; the probability the
#: threshold is applied to is the one the provider reports for the chosen option.
KEEP_OPTION = "keep"
DROP_OPTION = "drop"
KEEP_OPTIONS = [KEEP_OPTION, DROP_OPTION]

#: Keep probability at or above which a memory reaches the prompt. A CONSTANT, not
#: a setting: it is the meaning of the answer rather than a knob, and a
#: configurable threshold would be a second, undocumented way to turn the point
#: into a no-op (1.0 drops everything) without turning the seam off.
KEEP_THRESHOLD = 0.5

#: Scheduling slack added to the provider budget, and the floor and ceiling on the
#: wait whatever the config says. The ceiling is the real protection: this budget
#: is spent on the turn's critical path.
WAIT_MARGIN_SECS = 0.5
MIN_WAIT_SECS = 0.25
#: BELOW the recall route's own deadline (``executors.RECALL_TIMEOUT_SECS``, which
#: bounds the whole request and answers ``504 memory_recall_timeout`` when it
#: expires), with room left for the search this decision is attached to. That
#: ordering is the point of the number rather than a tuning choice: this seam's
#: promise is that every refusal keeps the similarity result, and a wait that could
#: outlast the request would break it in the one direction that costs the caller
#: something -- the tool would answer with an ERROR instead of the memories it had
#: already found, so a slow judge would take the recall down with it. ``skills_select``
#: sits on the prompt path, which has no such enclosing deadline, so its ceiling is
#: its own. Pinned against the route's constant by
#: ``test_decisions_memory_recall.py::TestTheWaitFitsInsideTheRoute``.
MAX_WAIT_SECS = 7.0

#: The module H2 owns and this one only ever READS: an outcome published here
#: reaches the dashboard through it. Resolved by name at call time inside a
#: ``try``/``except ImportError`` so a build without it is a no-op rather than an
#: import error on a hot path.
OUTCOMES_MODULE = "kiro_crew.decisions.outcomes"
PUBLISH_ATTR = "publish"


def kept_memories(
    candidates: Sequence[RowT],
    text: str,
    *,
    session_key: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    owner_turn: bool = False,
    pending: list[tuple[dict[str, Any], int]] | None = None,
) -> list[RowT] | None:
    """The memories the oracle would keep, or ``None`` to return all of *candidates*.

    Runs on the CALLER's thread, which in production is an executor worker. The
    order below is the contract:

    1. this is not an owner dashboard call — refuse, before anything else. The
       CALLER decides this (``handlers/memory_member._memory_recall_keep``, gated
       on membership of ``session_surface.dashboard_surfaced_keys()``): the receipt
       rides a reply
       someone is looking at, and the egress is memory text, so a recall with
       nobody watching is never asked about — a cron, a sub-agent, an integration,
       and any session whose tab is closed. A CHANNEL-born session with its
       dashboard tab open DOES qualify, because the publisher contributes each open
       slot's own key, channel keys included, and that is the intended reading
       rather than an accident;
    2. no usable loop, or this thread is running one — refuse. Waiting on a
       future from the loop's own thread would deadlock the loop;
    3. there are no candidates — refuse. An empty block is what both arms
       produce, so there is nothing to decide;
    4. the point is not enabled for this session — refuse, before any redaction.
       That covers the ``memory_text`` consent scope as well as the switch and the
       sampling bucket, because the gate funnels all three through one keystone
       read;
    5. screen and redact the candidate rows on THIS thread;
    6. submit one round to *loop* and wait ONCE for the whole turn's budget;
    7. record both arms and publish the outcome, still on THIS thread.

    The returned list holds the CANDIDATE ROWS THEMSELVES -- the same objects, not
    copies -- in candidate order, so the caller returns the rows it already had rather
    than re-resolving keys, and so the store's identity-based membership check accepts
    them (see :func:`surviving_rows`).

    A budget expiry cancels the future and returns ``None``. ``cancel()`` cannot
    stop a coroutine that already started, so the guarantee is the stronger one
    available: the result is never read again, so a late answer cannot alter the
    response that was assembled without it -- and it leaves no outcome row either,
    because nothing is appended to *pending* on that path.

    *pending* receives ``(outcome, latency_ms)`` for a decision that was made. It is
    the caller's list and the caller commits it (:func:`commit_outcome`); passing
    ``None`` makes this function decide and record NOTHING, which is what a caller
    that only wants the subset should pass.

    *pending* is EMPTIED first, so it describes THIS call and nothing earlier. One
    recall may call this more than once: ``recall`` answers a moved embedding space by
    discarding its result and running the whole search again keyword-only, and the
    hook is applied on each run. Every refusal above returns without appending, so a
    list left as it was would still hold the DISCARDED run's outcome, and the caller's
    commit would write that row -- a receipt naming a subset the store threw away,
    reported for a recall that in the worst case returned no memories at all. Clearing
    on entry makes the list carry one outcome or none: the one this call reached, or
    nothing because this call refused.
    """
    try:
        if pending is not None:
            # Before the first refusal, so EVERY path below inherits the invariant.
            pending.clear()
        if not owner_turn:
            return None
        # Closed, or not running: such a loop will never run the coroutine, so
        # waiting on that future would spend the whole budget on a certain
        # refusal.
        if loop is None or loop.is_closed() or not loop.is_running():
            return None
        if _this_thread_runs_a_loop():
            return None
        rows = list(candidates or [])
        if not rows:
            return None
        if not core.is_enabled(POINT, session_key=session_key):
            return None
        screened = screen_candidates(rows)
        if not screened:
            return None
        wait = _wait_budget()
        turn_id = uuid.uuid4().hex[:16]
        trace: dict[str, Any] = {}
        started = time.monotonic()
        coro = keep_decision(
            text,
            screened,
            session_key=session_key,
            turn_id=turn_id,
            deadline=started + wait,
            trace=trace,
        )
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except BaseException:
            # A coroutine that never got scheduled has to be closed HERE.
            # Dropping it unscheduled emits "coroutine was never awaited" from
            # whichever unrelated test later triggers the GC.
            coro.close()
            raise
        try:
            keys = future.result(timeout=wait)
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            future.cancel()
            return None
        if keys is None:
            return None
        injected = surviving_rows(rows, screened, keys)
        # HELD, not written. The caller commits it once the recall this decision shaped
        # has actually come back -- see :func:`commit_outcome`. Writing here would record
        # a subset that was never returned whenever the store discards its own result:
        # ``_recall_once`` validates the embedding generation AFTER applying this hook,
        # and answers a moved generation by running the whole recall again.
        if pending is not None:
            pending.append(
                (
                    build_outcome(baseline=rows, injected=injected, trace=trace),
                    int((time.monotonic() - started) * 1000),
                )
            )
        return injected
    except Exception:
        # Every failure keeps the shipped recall. This sits on the path that
        # assembles a session's prompt, so the seam may cost an observation and
        # must never cost a turn.
        logger.debug("memory.recall: keeping the similarity top-k", exc_info=True)
        return None


async def keep_decision(
    text: str,
    candidates: Sequence[dict[str, str]],
    *,
    session_key: str | None = None,
    turn_id: str | None = None,
    deadline: float | None = None,
    trace: dict[str, Any] | None = None,
) -> list[str] | None:
    """The keys the oracle keeps, or ``None`` to keep the baseline. Runs on the loop.

    ONE request carrying one question PER candidate: ``decide`` puts every
    question in a single POST, and a keep/drop answer about one memory says
    nothing about another, so a single question over the whole list would have to
    encode a subset as a string the gate could not check against a domain.

    *deadline* is a ``time.monotonic()`` reading the call must start inside. It is
    checked BEFORE the call rather than raced against: a call started past the
    deadline is one whose answer the caller has already stopped waiting for.

    *candidates* are WIRE rows -- ``{key, snippet}`` as :func:`screen_candidates`
    produces them -- not store rows, and they arrive already capped, key-screened
    and redacted. The two shapes use different names for the identifier (``id`` in
    the store, ``key`` on the wire), which is what keeps a store row from reaching
    the request by looking close enough: it would carry no ``key`` and be sent as a
    blank one. Nothing is re-screened here; :func:`screen_candidates` is the one
    bound, on the one path that reaches this.

    *trace* is filled with what the caller needs for the outcome row (the turn id,
    the menu size, the excerpt cost, the mean keep probability).
    """
    rows = list(candidates)
    if not rows:
        return None
    turn = turn_id or uuid.uuid4().hex[:16]
    extra: dict[str, Any] = {
        "turn_id": turn,
        "candidates": len(rows),
        "message_chars": message_chars(text),
    }
    if trace is not None:
        trace.update(extra)
        trace["p"] = None
    if deadline is not None and time.monotonic() >= deadline:
        logger.debug("memory.recall: the call would start past the deadline")
        return None
    state = build_state(text, rows)
    questions = build_questions(rows)
    answers = await core.decide(POINT, state, questions, session_key=session_key, extra=extra)
    keys = read_answer(answers, rows)
    if keys is None:
        return None
    if trace is not None:
        trace["p"] = mean_keep_probability(answers, rows)
    return keys


def question_id(index: int) -> str:
    """The wire id of the question about candidate *index*.

    An ORDINAL, never the memory's own id. Question ids are dictionary keys in the
    request body, so a memory id would put a store identifier on the wire for no
    gain — the caller already holds the list, and the position is what maps an
    answer back to a row.
    """
    return f"m{index}"


def build_questions(rows: Sequence[Mapping[str, str]]) -> list[Question]:
    """One keep/drop question per screened candidate, in candidate order."""
    return [
        Choice(
            question_id(index),
            f"Memory {index + 1} is offered to this turn's prompt. "
            f"Answer {KEEP_OPTION} if it helps with this message, "
            f"{DROP_OPTION} if it is not worth its place in the prompt.",
            options=list(KEEP_OPTIONS),
        )
        for index, _row in enumerate(rows)
    ]


def scrubbed(text: object, limit: int) -> str:
    """*text* as at most *limit* characters with credentials and exfiltration URLs replaced.

    The canonical redactors, not the gate's scanner, and that difference is the
    design. The gate REFUSES a request carrying a credential, which is right for a
    message the owner just typed. A recalled memory is different: it is text the
    agent wrote down turns or days ago, and a single episode that happens to quote
    an env file would mean the point never fires again on that store. Replacing
    the match keeps the question answerable and leaves nothing to leak; the gate
    then scans the placeholder and passes.

    Clipped AFTER redaction, never before, for the reason ``tool_risk.scrubbed``
    and ``message_steer.redacted`` both state: clipping first can cut a secret in
    half, and a half is a fragment neither redactor matches. The clip is a HEAD
    rather than a tail, unlike the running-turn excerpt: a memory's opening is
    what says what it is about, which is the question being asked.

    Never raises: a redactor that fails yields nothing rather than unredacted
    text, which is the only safe direction for text about to leave the machine.
    """
    raw = text if isinstance(text, str) else ("" if text is None else str(text))
    if not raw or limit <= 0:
        return ""
    try:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        cleaned, _ = redact_credentials(raw)
        cleaned, _ = redact_exfiltration_urls(cleaned)
    except Exception:
        # A scan that did not complete cannot clear text for the wire. Dropping
        # the snippet is a worse question, not a worse outcome: the gate would
        # refuse the request anyway, and this keeps the refusal free of a call.
        logger.debug("memory.recall: redaction failed; dropping the snippet", exc_info=True)
        return ""
    return cleaned[:limit]


def key_of(row: Mapping[str, Any]) -> str:
    """A candidate row's identity, or ``""`` when it has none.

    The store's own episode id. Never synthesised from the text: the key is what
    the answer is matched back on, and two rows sharing a derived key would make
    one answer apply to both.
    """
    key = row.get("id", "")
    return key if isinstance(key, str) else ""


def screen_candidates(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The rows that may be sent: capped, key-screened, redacted and clipped.

    A row with no id, an over-long id or a duplicate id is DROPPED rather than
    repaired: the id is what maps an answer back to a memory, so a row that cannot
    carry one cannot be decided about, and it stays in the baseline block exactly
    as it is today.
    """
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in list(candidates)[:MAX_CANDIDATES]:
        if not isinstance(candidate, Mapping):
            continue
        key = key_of(candidate)
        if not key or len(key) > MAX_KEY_CHARS or key in seen:
            continue
        seen.add(key)
        rows.append({"key": key, "snippet": scrubbed(candidate.get("text", ""), MAX_SNIPPET_CHARS)})
    return rows


def surviving_rows(
    candidates: Sequence[RowT],
    screened: Sequence[Mapping[str, str]],
    kept_keys: Sequence[str],
) -> list[RowT]:
    """The candidate rows that reach the prompt, in candidate order.

    Two classes survive, and the second one is the fix this function exists for:

    * a row Jev KEPT -- its key is in *kept_keys*;
    * a row Jev was never OFFERED -- absent from *screened*, because
      :func:`screen_candidates` capped it out at :data:`MAX_CANDIDATES` or refused its
      id. Nobody decided about it, so nobody may drop it.

    Only an offered row can be removed, and then only by an answer naming it. An
    earlier version filtered down to the offered rows and dropped the rest, which made
    the point delete memories it had never asked about -- a row past the cap vanished
    from the prompt because it was absent from the answer, which is indistinguishable
    from "Jev said no" in a plain filter and is not what happened.

    The rows come back BY IDENTITY, never copied. The store that hands them over
    decides membership with ``id()`` (``vector_memory._kept_episodes``), because two
    distinct episodes can hold equal dicts and a membership test by value would let
    one answer admit the other. A copy here would therefore look to the store like a
    row its own search never ranked, and every decision would be discarded as
    unusable -- silently, since discarding one is the correct fallback.
    """
    offered = {str(row.get("key", "")) for row in screened}
    kept = set(kept_keys)
    return [row for row in candidates if key_of(row) not in offered or key_of(row) in kept]


def message_excerpt(text: str) -> str:
    """The part of *text* that actually leaves the machine, after the cap.

    One function so the count on the outcome record and the string in the request
    cannot disagree: :func:`build_state` sends this and :func:`message_chars`
    measures the same call.
    """
    return (text or "")[:MAX_MESSAGE_CHARS]


def message_chars(text: str) -> int:
    """Characters of *text* that were sent, which is the excerpt's own length."""
    return len(message_excerpt(text))


def build_state(text: str, rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """The state sent to the oracle: this message and the recalled candidates.

    Nothing else. There is no ``history`` key: the candidates ARE prior
    conversation, already chosen by relevance to this message, so spending the
    consented history ceiling on a second, unranked copy of the same transcript
    would make the question worse and the egress larger.
    """
    return {
        "message": message_excerpt(text),
        "candidates": [
            {"key": str(row.get("key", "")), "snippet": str(row.get("snippet", ""))} for row in rows
        ],
    }


def keep_probability(answer: object) -> float | None:
    """The probability this answer assigns to KEEPING, or ``None``.

    The provider reports the probability of the option it CHOSE, so a ``drop``
    answer's probability is read as its complement. Without that, a confident drop
    (``drop`` at 0.95) and a confident keep (``keep`` at 0.95) would both clear a
    threshold on the raw number, and every candidate would be kept.
    """
    if not isinstance(answer, Answer):
        return None
    if not isinstance(answer.p, (int, float)) or isinstance(answer.p, bool):
        return None
    p = float(answer.p)
    if not math.isfinite(p) or not 0.0 <= p <= 1.0:
        return None
    if answer.value == KEEP_OPTION:
        return p
    if answer.value == DROP_OPTION:
        return 1.0 - p
    return None


def read_answer(answers: Any, rows: Sequence[Mapping[str, str]]) -> list[str] | None:
    """The keys to keep, or ``None`` to keep the baseline.

    EVERY offered candidate must carry a readable answer. A partial reading is a
    refusal, not a partial application: a missing answer is indistinguishable from
    "drop it", so applying the rest would silently drop a memory nobody decided
    about. An empty list is a real answer — "none of these are worth the prompt".
    """
    if not isinstance(answers, dict):
        return None
    kept: list[str] = []
    for index, row in enumerate(rows):
        p = keep_probability(answers.get(question_id(index)))
        if p is None:
            return None
        if p >= KEEP_THRESHOLD:
            kept.append(str(row.get("key", "")))
    return kept


def mean_keep_probability(answers: Any, rows: Sequence[Mapping[str, str]]) -> float | None:
    """The mean keep probability over the offered candidates, or ``None``.

    A summary, and named as one: there is no single confidence for a request that
    asked twenty questions, and the strip has one number to print. Read only
    after :func:`read_answer` has returned a list, so every answer is known
    readable.
    """
    values = [keep_probability(answers.get(question_id(index))) for index, _row in enumerate(rows)]
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def injected_chars(rows: Sequence[Mapping[str, Any]]) -> int:
    """Characters *rows* contribute to the recall response.

    The memory texts, clipped the way the store clips them
    (``vector_memory.EPISODIC_BLOCK_TEXT_CHARS``, the bound its ``fit`` walk applies),
    which is the part of the response that scales with the decision. The ``[memory:id]``
    framing beside each line is not counted: it is a handful of characters per row and
    counting it would make this estimate depend on the formatter's punctuation.

    The import is function-local, and that is required rather than tidy:
    ``vector_memory`` imports this package's gate through the hook it is handed, so a
    module-scope import here closes the cycle. It is also the reason the rest of this
    module imports nothing from the store.
    """
    from kiro_crew.vector_memory import EPISODIC_BLOCK_TEXT_CHARS

    total = 0
    for row in rows:
        text = row.get("text", "")
        if isinstance(text, str):
            total += len(text[:EPISODIC_BLOCK_TEXT_CHARS])
    return total


def build_outcome(
    *,
    baseline: Sequence[Mapping[str, Any]],
    injected: Sequence[Mapping[str, Any]],
    trace: Mapping[str, Any],
) -> dict[str, Any]:
    """Both arms of one turn as the fields the row and the publish hook share.

    The two lists are spelled ``baseline_keys`` and ``jev_keys`` rather than
    ``baseline`` and ``jev``, which is what keeps this record out of the skill
    strip's reader: that reader requires both of those names to hold lists of
    skill keys, so a record using them would render as a skill selection with
    memory ids in it. Naming the lists for what they hold makes the two records
    tell each other apart on shape as well as on ``point``.

    ``agree`` is SET equality: both arms are selections, and an order difference
    between two identical sets is not a disagreement about which memories help.

    A row with no readable id is left out of BOTH key lists rather than named as
    ``""``: the strip's reader refuses a list carrying an empty name, so one such row
    would cost the whole receipt. It is still in both ARMS -- ``chars_saved`` measures
    the rows, not the keys, so the saving stays exact -- and an episode without its
    own primary key is a store defect rather than a state this seam produces.
    """
    baseline_keys = [key for key in (key_of(row) for row in baseline) if key]
    jev_keys = [key for key in (key_of(row) for row in injected) if key]
    return {
        "turn_id": trace.get("turn_id"),
        "baseline_keys": baseline_keys,
        "jev_keys": jev_keys,
        "agree": set(baseline_keys) == set(jev_keys),
        "p": trace.get("p"),
        # Non-negative by construction: the point only ever removes, so a negative
        # saving would be a bug made visible rather than a measurement.
        "chars_saved": max(0, injected_chars(baseline) - injected_chars(injected)),
        "candidates": trace.get("candidates"),
        "message_chars": trace.get("message_chars"),
    }


def commit_outcome(session_key: str | None, pending: Sequence[tuple[dict[str, Any], int]]) -> bool:
    """Record and publish the decision the returned recall actually used. Never raises.

    Called by the caller AFTER the recall came back, which is the whole point of the
    two-step: ``_recall_once`` validates the embedding generation after applying the
    hook, and ``recall`` answers a moved generation by running the recall again. An
    outcome written when the answer arrived would therefore describe a subset the store
    then discarded, and a retried recall would leave two rows for one tool call.

    Only the LAST entry is committed, because that is the attempt whose result was
    returned. It is also the only one present: :func:`kept_memories` empties the list on
    entry, so a retried recall cannot leave the discarded attempt's outcome behind for
    this to write -- a row for an attempt nobody saw is a receipt for nothing, which is
    worse than no row. Reading the last entry rather than the only one keeps the two
    statements independent, so neither has to be true for this to be right.

    A caller that never reaches this -- the request failed, the route refused, the
    provider answered late -- commits nothing, so a failure leaves NO receipt at all.
    Returns whether a row was written, for a test to assert on.

    Guarded as a whole and separately from the decision: the kept set is already in the
    response by the time this runs, so neither a log failure nor a missing outcomes
    module may cost the recall its answer. The publish is CONDITIONAL on the write, for
    the reason ``skills_select`` states: a strip whose durable row was refused describes
    a decision no verdict could be filed against.
    """
    if not pending:
        return False
    outcome, latency_ms = pending[-1]
    try:
        row = _log.build_row(
            point=POINT,
            session_key=session_key,
            latency_ms=latency_ms,
            extra=outcome,
        )
        written = _log.append(row)
    except Exception:
        logger.debug("memory.recall: could not record the outcome row", exc_info=True)
        return False
    if not written:
        logger.debug("memory.recall: outcome row was not written; not publishing it")
        return False
    publish_outcome(session_key, row)
    return True


def publish_outcome(session_key: str | None, outcome: dict[str, Any]) -> bool:
    """Hand *outcome* to :data:`OUTCOMES_MODULE` if this build has one. Never raises.

    Returns whether a publisher ran, for a test to assert on. Resolved by name at
    CALL time rather than imported at module scope: the module is optional, and a
    top-level import would make this point unimportable on a build without it.

    The row is passed exactly as it was written, so what the dashboard shows and
    what the log holds cannot drift into two descriptions of one turn.
    """
    try:
        try:
            module = importlib.import_module(OUTCOMES_MODULE)
        except ImportError:
            return False
        publish = getattr(module, PUBLISH_ATTR, None)
        if publish is None:
            return False
        publish(session_key, outcome)
        return True
    except Exception:
        logger.debug("memory.recall: could not publish the outcome", exc_info=True)
        return False


def keep_hook(
    text: str,
    *,
    session_key: str | None,
    loop: asyncio.AbstractEventLoop | None,
    owner_turn: bool,
    pending: list[tuple[dict[str, Any], int]] | None = None,
) -> Callable[[list[dict]], list[dict] | None]:
    """A ``keep=`` callable for ``VectorMemoryStore.recall``.

    The store owns the candidates and the response; this point owns the question. A
    callable is what keeps the two apart: the store hands over the rows it ranked and
    bounded, applies whatever comes back, and imports nothing from this package.

    *pending* is the caller's outcome list. The hook appends to it rather than recording,
    and the caller commits it with :func:`commit_outcome` once the recall has come back
    -- so a recall the store discards, or one the route never returns, leaves no row and
    no receipt.
    """

    def keep(candidates: list[dict]) -> list[dict] | None:
        return kept_memories(
            candidates,
            text,
            session_key=session_key,
            loop=loop,
            owner_turn=owner_turn,
            pending=pending,
        )

    return keep


def _wait_budget() -> float:
    """How long the caller's thread may wait, clamped into a sane window."""
    try:
        budget = float(core.timeout_secs()) + WAIT_MARGIN_SECS
    except Exception:
        logger.debug("memory.recall: provider budget unreadable")
        return MIN_WAIT_SECS
    if not math.isfinite(budget):
        return MIN_WAIT_SECS
    return min(max(budget, MIN_WAIT_SECS), MAX_WAIT_SECS)


def _this_thread_runs_a_loop() -> bool:
    """Whether the calling thread is itself running an event loop.

    Positive identity, not a probe of the target loop: blocking this thread on a
    cross-thread future is only safe when this thread has no loop of its own to
    starve — and that holds for the executor worker production actually uses.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
