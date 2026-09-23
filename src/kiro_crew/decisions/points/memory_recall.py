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
Jev's narrowing removed. There is no shadow mode: the arm that is returned is
always Jev's.

The accounting, once
--------------------
Four different things shorten a recall, and the receipt names one of them. Each
number below means exactly this, everywhere -- in the row, in the publish, in the
strip's copy -- and nothing else:

``candidates`` -- OFFERED. The rows this point put in front of the oracle: the ones
the store's ``fit`` walk had already selected, capped at :data:`MAX_CANDIDATES` and
screened for a usable id. A row the screen dropped is still in ``baseline_keys``,
because the baseline is what the recall would have returned rather than what was
asked about.

``baseline_keys`` -- what the recall returns WITHOUT this point. The candidate list
itself.

``jev_keys`` -- KEPT BY JEV. The memories Jev's answer kept, in the search's order.
The DECISION's own output, before anything downstream touches it. It is not the set
that shipped, and it must not be quietly narrowed into one: a receipt whose kept
count silently absorbed a later removal is what makes the numbers stop adding up.

``bounded_omitted`` -- DROPPED BY BOUNDING. How many of ``jev_keys`` the response
budget then removed (``memory_recall.bound_recall_payload`` clips the largest text
and drops whole rows off the tail). The set the caller received is ``jev_keys``
minus these, so the two numbers together state what shipped and each stays
attributable to whoever did it. Absent rather than ``0`` when the budget removed
nothing.

``chars_saved`` -- characters of memory text JEV'S NARROWING removed, and nothing
else's. Both arms are measured by :func:`injected_chars` on the SAME rows at the
SAME moment -- before redaction, before bounding -- so the scrubber's substitutions
and the budget's clipping are absent from the difference by construction rather
than subtracted by hand. REMOVED BY REDACTION is therefore a quantity this receipt
deliberately does not carry: it is the same scrubbing applied to whatever ships, it
is no part of what Jev decided, and a figure that folded it in would credit the
scrubber's work to the judge.

One writer
----------
:func:`commit_receipt` is the ONLY thing that writes or publishes a receipt, and it
takes the completed payload as an argument. That is the structural half of the rule
above: a caller cannot reach the writer without the finished response in hand, and
the writer -- not the caller -- checks that the request is still live. Three
separate call sites have leaked a receipt for a recall the caller never got, each
time because the check sat beside the call instead of inside it.
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
from kiro_crew.decisions.points import MAX_KEY_CHARS, as_text
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

#: Scheduling slack, used for both ends of the same thing. Added to the provider
#: budget, because the coroutine is submitted to a loop that may be mid-task; and
#: withheld from the enclosing deadline in :func:`_remaining_budget`, because the
#: recall still has to come back, be bounded and be serialized after the judge
#: answers. One quantity, so there is no second number to keep in step.
WAIT_MARGIN_SECS = 0.5
MIN_WAIT_SECS = 0.25
#: The ceiling on the wait where NO enclosing deadline is in force, and a belt on a
#: hand-edited provider timeout. It is not what holds the judge inside the recall
#: route: that is :func:`_remaining_budget`, which reads the time actually left on
#: the request this decision runs inside.
#:
#: A constant cannot do that job, and this one could not. ``GET /api/memory/recall``
#: is bounded at ``executors.RECALL_TIMEOUT_SECS`` (9 s) and answers
#: ``504 memory_recall_timeout`` when it expires, so 7 s left 2 s for everything
#: else -- and the SEARCH the decision is attached to runs BEFORE the judge does. A
#: cold store, a rebuilt index or an embedding that took its own time spends that
#: 2 s, and then a full 7 s wait ends past the deadline: the tool answers with an
#: ERROR instead of the memories it had already found, which is exactly the one
#: direction this seam's promise must not fail in. Subtracting the elapsed time is
#: the only cap that holds, so the wait is derived rather than declared.
#:
#: Kept as the no-deadline ceiling because ``kept_memories`` is reachable without
#: one -- a caller outside the bounded route -- and the budget there is the turn's
#: critical path rather than a request deadline. ``skills_select`` is that shape
#: throughout, which is why its own ceiling is its own. The relationship to the
#: route's constant is still pinned by
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
    still_watched: Callable[[], bool] | None = None,
    pending: list[tuple[dict[str, Any], int]] | None = None,
    store: str | None = None,
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
    the caller's list and the caller commits it (:func:`commit_receipt`); passing
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
        # Re-read HERE, not where the hook was built. *owner_turn* was decided on the
        # event loop before the search began; this runs later, on the worker thread the
        # search is on, and the thing it asserts can stop being true in between -- the
        # owner closes the tab and the slot leaves the published set. That gap is the
        # difference between sending recalled memory and not sending it, which is the
        # one direction an egress gate must not get wrong. An absent predicate means the
        # caller has nothing further to assert, not that the answer is yes.
        if still_watched is not None and not still_watched():
            logger.debug("memory.recall: the surface went away before the question")
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
        # The enclosing request has no time left to lend, so there is nothing to ask
        # for: any answer would arrive after the route had already given up. Refused
        # on exactly the terms every other failure here takes -- return None, and the
        # caller injects the similarity result unnarrowed. It also appends nothing to
        # `pending`, so the turn leaves no receipt for a decision never made.
        if wait <= 0:
            logger.debug("memory.recall: no budget left on the enclosing request")
            return None
        turn_id = uuid.uuid4().hex[:16]
        trace: dict[str, Any] = {}
        started = time.monotonic()
        coro = keep_decision(
            text,
            screened,
            session_key=session_key,
            turn_id=turn_id,
            deadline=started + wait,
            still_watched=still_watched,
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
        # has actually come back -- see :func:`commit_receipt`. Writing here would record
        # a subset that was never returned whenever the store discards its own result:
        # ``_recall_once`` validates the embedding generation AFTER applying this hook,
        # and answers a moved generation by running the whole recall again.
        if pending is not None:
            pending.append(
                (
                    build_outcome(baseline=rows, injected=injected, trace=trace, store=store),
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
    still_watched: Callable[[], bool] | None = None,
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

    *still_watched* is the dashboard-surface test, read on the line before
    ``core.decide`` because that call IS the egress: it is the point at which
    snippets of the owner's own remembered notes leave the machine. Every earlier
    read of the same question -- in the route before the search is dispatched, and in
    :func:`kept_memories` before the menu is built -- is a cheap refusal that saves
    work. None of them is the gate, because the answer can change after any of them:
    the owner closes the tab while the search runs, the slot leaves the published
    set, and a request already assembled would still go out. A guard for an egress
    belongs at the egress. ``None`` means the caller asserts nothing further, not
    that the answer is yes.

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
    # The LAST line before the network call, deliberately: the state above is built
    # and redacted locally and costs nothing if this refuses, while everything after
    # this line has already left.
    if still_watched is not None and not still_watched():
        logger.debug("memory.recall: the surface went away before the call")
        return None
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
    raw = as_text(text)
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
    store: str | None = None,
) -> dict[str, Any]:
    """Both arms of one turn as the fields the row and the publish hook share.

    ``store`` is the memory store the recall ran against, carried onto the record
    so the strip's id popover looks each id up in the RIGHT store rather than
    always the default one. ``None`` -- an older producer or a caller that does not
    know it -- is left as ``None`` here and read as the default store on the wire.

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
    # Both arms, from the same rows at the same moment: before redaction and before
    # bounding. That is what makes the difference attributable to the DECISION -- see
    # the accounting section in this module's docstring.
    baseline_chars = injected_chars(baseline)
    jev_chars = injected_chars(injected)
    return {
        "turn_id": trace.get("turn_id"),
        "store": store,
        "baseline_keys": baseline_keys,
        "jev_keys": jev_keys,
        "agree": set(baseline_keys) == set(jev_keys),
        "p": trace.get("p"),
        # Non-negative by construction: the point only ever removes, so a negative
        # saving would be a bug made visible rather than a measurement.
        "chars_saved": max(0, baseline_chars - jev_chars),
        # Both sides kept, so a reader of the row can check the subtraction and a
        # later pass cannot re-derive the saving against a different basis.
        "baseline_chars": baseline_chars,
        "jev_chars": jev_chars,
        "candidates": trace.get("candidates"),
        "message_chars": trace.get("message_chars"),
    }


def _bounded_omitted(outcome: Mapping[str, Any], delivered: Sequence[Mapping[str, Any]]) -> int:
    """How many of ``jev_keys`` the response budget removed after the decision.

    ``bound_recall_payload`` runs after the point and holds the response to a
    transport budget: it clips the largest text and, once there is no text left to
    clip, drops whole rows off the tail. Multibyte content reaches that budget on an
    ordinary query, because the same text is counted in the evidence, again in the
    model-facing context and again through JSON escaping.

    Counted rather than folded in. ``jev_keys`` stays the decision's own output and
    this is a second number beside it, so "Jev kept 3" and "1 did not fit" together
    say what shipped and each part stays attributable to whoever removed it. A kept
    count quietly narrowed to the delivered set is what stops the arithmetic closing.
    """
    shipped = {key for key in (key_of(row) for row in delivered) if key}
    return sum(1 for key in (outcome.get("jev_keys") or []) if key not in shipped)


def _request_abandoned() -> bool:
    """Has the request this decision ran inside already given up?

    Read HERE rather than at the call site, which is the whole point of the funnel.
    ``memory_recall_deadline`` bounds the recall route with ``asyncio.wait_for``, and
    a cancellation is delivered only where the coroutine yields -- the ``to_thread``
    hand-off that reaches this module. The work is on an executor thread by then and a
    running thread does not cancel, so an unguarded write lands a receipt for a recall
    the caller received as ``504 memory_recall_timeout``.

    ``None`` means no deadline is in force -- a caller outside the bounded route --
    which is live rather than abandoned.
    """
    from kiro_crew.embeddings import embedding_work

    work = embedding_work.get()
    return work is not None and work.expired()


def _publish(session_key: str | None, outcome: Mapping[str, Any]) -> bool:
    """Hand *outcome* to :data:`OUTCOMES_MODULE` if this build has one. Never raises.

    Private, and called from exactly one place. A publish reachable on its own is a
    second writer, and the receipt has one: see this module's docstring.

    Resolved by name at CALL time rather than imported at module scope, because the
    module is optional and a top-level import would make this point unimportable on a
    build without it. The row is passed exactly as it was written, so what the
    dashboard shows and what the log holds cannot drift into two descriptions of one
    turn.
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


def commit_receipt(
    session_key: str | None,
    pending: Sequence[tuple[dict[str, Any], int]],
    *,
    payload: Mapping[str, Any],
) -> bool:
    """The ONE writer: record and publish the receipt for a COMPLETED recall.

    Never raises. Returns whether a row was written, for a test to assert on.

    *payload* is the response that is about to go out -- redacted, bounded, final.
    Taking it as an argument is the structural half of "no receipt for a recall
    nobody received": there is no way to reach this function without the finished
    response in hand. Every guard a receipt depends on lives HERE, in the one funnel,
    rather than at a call site -- a guard beside a call is a guard each new caller has
    to remember, and this one has three conditions to get right:

    * an empty *pending* is every refusal's shape -- the point holds nothing, so
      there is nothing to write;
    * a *payload* carrying no ``retrieval`` is one the bounding walk refused outright,
      so nothing was delivered for a receipt to describe;
    * :func:`_request_abandoned` is the deadline the route's own ``504`` is derived
      from, so the receipt exists exactly when the response carrying it does.

    Only the LAST held entry is committed, because that is the attempt whose result
    was returned. It is also the only one present -- :func:`kept_memories` empties the
    list on entry -- so reading the last rather than the only one keeps the two
    statements independent and neither has to be true for this to be right.

    The write and the publish are guarded together and separately from the decision:
    the kept set is already in the response by the time this runs, so neither a log
    failure nor a missing outcomes module may cost the recall its answer. The publish
    is CONDITIONAL on the write, for the reason ``skills_select`` states -- a strip
    whose durable row was refused describes a decision no verdict could be filed
    against.

    One STRIP per turn, and it describes the turn's MOST RECENT recall. An agent may
    call the tool more than once in a turn, and each call is a separate request with
    its own decision; :mod:`kiro_crew.decisions.outcomes` holds one entry per
    (session, point), so the second publish replaces the first. That is kept rather
    than changed -- accumulating would need a per-point LIST in a registry four points
    share, and a transcript component that draws N strips under one reply -- and the
    strip's own copy names the scope instead. The LOG keeps every call: each one
    appends its own row, so the measurement the strip samples is complete in the
    durable record.
    """
    if not pending:
        return False
    retrieval = payload.get("retrieval") if isinstance(payload, Mapping) else None
    if not isinstance(retrieval, Mapping):
        logger.debug("memory.recall: no delivered recall to describe; writing nothing")
        return False
    if _request_abandoned():
        logger.debug("memory.recall: the request was abandoned; writing nothing")
        return False
    outcome, latency_ms = pending[-1]
    delivered = list(retrieval.get("episodes") or [])
    row_fields = dict(outcome)
    omitted = _bounded_omitted(outcome, delivered)
    if omitted:
        row_fields["bounded_omitted"] = omitted
    try:
        row = _log.build_row(
            point=POINT,
            session_key=session_key,
            latency_ms=latency_ms,
            extra=row_fields,
        )
        written = _log.append(row)
    except Exception:
        logger.debug("memory.recall: could not record the outcome row", exc_info=True)
        return False
    if not written:
        logger.debug("memory.recall: outcome row was not written; not publishing it")
        return False
    _publish(session_key, row)
    return True


def keep_hook(
    text: str,
    *,
    session_key: str | None,
    loop: asyncio.AbstractEventLoop | None,
    owner_turn: bool,
    still_watched: Callable[[], bool] | None = None,
    pending: list[tuple[dict[str, Any], int]] | None = None,
    store: str | None = None,
) -> Callable[[list[dict]], list[dict] | None]:
    """A ``keep=`` callable for ``VectorMemoryStore.recall``.

    *store* is the name of the memory store this recall ran against. It is
    threaded onto the outcome so the strip's id popover resolves each id in the
    store it came from; ``None`` reads as the default store on the wire.

    The store owns the candidates and the response; this point owns the question. A
    callable is what keeps the two apart: the store hands over the rows it ranked and
    bounded, applies whatever comes back, and imports nothing from this package.

    *pending* is the caller's outcome list. The hook appends to it rather than recording,
    and the caller commits it with :func:`commit_receipt` once the recall has come back
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
            still_watched=still_watched,
            pending=pending,
            store=store,
        )

    return keep


def _remaining_budget() -> float:
    """Seconds left for the judge on the request this decision runs inside.

    ``math.inf`` when no deadline is in force, which is a caller outside the bounded
    recall route rather than a missing bound.

    The deadline lives on the ``EmbeddingWork`` that ``run_with_recall_deadline``
    installs for the request, and it is READABLE here because ``run_in_embed_pool``
    submits the search with ``copy_context().run`` -- so the worker thread this hook
    runs on carries the loop's context, deadline included.

    ``WAIT_MARGIN_SECS`` is withheld rather than spent: the judge answering is not
    the end of the request. The store still has to finish the recall, the route still
    has to bound the payload and serialize it, and a wait that ran to the deadline
    itself would hand all of that a budget of zero -- turning a judge that answered
    IN TIME into a ``504`` anyway.

    A cancelled work reads as no time left, not as unbounded: it is the same signal
    the route's own bound reads, and the answer would reach nobody.
    """
    from kiro_crew.embeddings import embedding_work

    work = embedding_work.get()
    if work is None:
        return math.inf
    if work.cancelled.is_set():
        return 0.0
    return work.deadline - time.monotonic() - WAIT_MARGIN_SECS


def _wait_budget() -> float:
    """How long the caller's thread may wait. ``<= 0`` means "do not ask at all".

    The provider's own budget, clamped into a sane window, then held under the time
    REMAINING on the enclosing request. The remaining budget wins over every other
    bound here, including ``MIN_WAIT_SECS``: a floor that outlasted the deadline
    would be the same overrun in a smaller size.
    """
    try:
        budget = float(core.timeout_secs()) + WAIT_MARGIN_SECS
    except Exception:
        logger.debug("memory.recall: provider budget unreadable")
        budget = MIN_WAIT_SECS
    if not math.isfinite(budget):
        budget = MIN_WAIT_SECS
    budget = min(max(budget, MIN_WAIT_SECS), MAX_WAIT_SECS)
    return min(budget, _remaining_budget())


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
