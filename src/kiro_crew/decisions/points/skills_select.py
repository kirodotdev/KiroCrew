"""``skills.select`` — which skill should this message load?

The shipped selector is word-overlap trigger matching
(``SkillsLoader.get_triggered_skills``, scored by ``trigger_match.trigger_score``
against ``MIN_TRIGGER_OVERLAP``). This point asks the oracle the same question
over a WIDER menu and, when an answer arrives, that answer is what
``build_message`` injects.

Two halves, deliberately split by thread
----------------------------------------
:func:`selected_skills` is synchronous and runs on the caller's thread —
``ContextBuilder.build_message`` is sync and production reaches it only through
``run_in_embed_pool``, a thread executor. Candidate discovery (a skill-tree walk
plus one frontmatter read per skill), the prior-turn read, the body measurement
and the outcome row therefore all happen on that worker thread, never on the
event loop that serves the gateway. Only the ``decide`` awaits are submitted to
the loop, and the caller waits for a bounded budget.

Everything is a REFUSAL back to the baseline
--------------------------------------------
:func:`selected_skills` returns ``None`` for "keep exactly what trigger matching
chose" — the point is off, this session is not sampled, the cap is zero, the menu
is empty, the answer is unusable, the transport failed, the budget expired, or
there is no usable loop. It returns ``[]`` only for a real answer of "no skill
applies", and ``[key]`` for a real pick. A caller needs no try/except of its own.

What the menu is, and is not
----------------------------
Candidates are every skill this message COULD load, not the skills word overlap
already won on: offering only the winners would let the oracle re-rank a
selection and never widen it. The existing RESTRICTIONS all survive, because each
one is a rule about what may reach a prompt rather than a ranking:

* ``always: true`` skills are injected unconditionally and are never selected;
* a skill with no ``triggers`` is not selectable by the baseline either;
* ``repo_scope`` is enforced through the loader's own gate;
* a NEGATIVE trigger that matches this message excludes the skill outright;
* enumeration goes through ``_iter_visible``, so an untrusted project's skills
  are absent and a confined project skill is read through the confined reader;
* the answer must name an offered key EXACTLY, so nothing resolves by prefix;
* the result is capped by the live ``skills.max_triggered``, and a cap of zero
  means no selection and no call at all.

Both arms, every sampled turn
-----------------------------
The word-overlap result is computed from the SAME tree walk that builds the menu
(:func:`candidates_from_loader`, ``baseline_out``), so knowing what the baseline
would have injected costs no second walk and no second frontmatter read. Jev's
answer is what gets injected; the baseline is recorded beside it, with
``agree``, the probability and an estimate of the body characters the difference
saves. That is what makes the log answerable about whether the seam is worth its
latency, and it is the only reason both arms exist here — there is no shadow
mode: the arm that is injected is always Jev's.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import logging
import math
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from kiro_crew import decisions as core
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.points import MAX_KEY_CHARS
from kiro_crew.decisions.types import Answer, Choice, Question
from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP, trigger_score, words_of

logger = logging.getLogger(__name__)

POINT = "skills.select"

#: Cap on how many skills are described to the oracle. The menu has to stay a
#: small prompt, so the lowest-scoring tail is dropped rather than sent.
MAX_CANDIDATES = 100
MAX_MESSAGE_CHARS = 2000
MAX_DESCRIPTION_CHARS = 200

#: How many prior transcript rows the point will even look at, before the CHAR
#: budget is applied. The budget is what bounds egress; this bounds the READ, so
#: a long conversation cannot turn one selection into a full-file scan. It is the
#: value the caller passes to ``conversation_log.recent``, which serves a slice
#: this small from a tail read rather than a whole-file parse.
MAX_HISTORY_MESSAGES = 20

#: The two roles a prior turn may carry. Tool output is not conversation and is
#: not sent: it is the largest and least selective text in a transcript, and it
#: routinely quotes files the message itself never mentioned.
HISTORY_ROLES = frozenset({"user", "assistant"})

#: Characters per token for the ``tokens_saved`` estimate. An estimate by
#: construction -- a real tokenizer is a model-specific dependency this seam has
#: no business importing on a hot path -- and named so the row's units are
#: readable rather than folded into a magic 4.
CHARS_PER_TOKEN = 4

#: The "nothing applies" option. An explicit choice rather than an empty answer,
#: so a refusal stays distinguishable from a transport failure.
#:
#: Outside the skill-key namespace BY CONSTRUCTION, not by convention: a key's
#: first component is a directory name, so no key can start with ``/``, and the
#: loader's ``_safe_name`` rejects a rooted name besides. A skill can be called
#: ``none`` or ``(no skill applies)``; picking it must stay distinguishable from
#: declining to pick one.
NONE_OPTION = "/no skill applies"

#: Scheduling slack added to the provider budget. The coroutine is submitted to a
#: loop that may be mid-task, so a wait of exactly ``timeout_ms`` would expire on
#: a call the gate itself would have allowed to finish.
WAIT_MARGIN_SECS = 0.5

#: Floor and ceiling on the wait, whatever the config says. The ceiling is the
#: real protection: this budget is spent on the turn's critical path, so a
#: hand-edited ``timeout_ms`` of an hour must not hold a message there.
MIN_WAIT_SECS = 0.25
MAX_WAIT_SECS = 10.0

#: Row ``error`` when the menu could not be built at all. Written so a loader
#: change that breaks enumeration shows up as rows saying so, not as a log that
#: stays as empty as an unsampled session's would.
ERROR_CANDIDATES = "candidates-failed"

#: The module H2 owns and this one only ever READS: an outcome published here
#: reaches the dashboard through it. Resolved by name at call time inside a
#: ``try``/``except ImportError`` so a build without it is a no-op rather than an
#: import error on a hot path.
OUTCOMES_MODULE = "kiro_crew.decisions.outcomes"
PUBLISH_ATTR = "publish"


def selected_skills(
    skills_loader: Any,
    text: str,
    project_dir: str | Path | None = None,
    *,
    session_key: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    history_source: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
) -> list[str] | None:
    """The oracle's selection for *text*, or ``None`` to keep the baseline.

    Runs on the CALLER's thread, which in production is an executor worker. The
    order below is the contract:

    1. no usable loop, or this thread is running one — refuse. Waiting on a
       future from the loop's own thread would deadlock the loop, and no
       selection is worth that;
    2. the point is not enabled for this session — refuse, before any walk;
    3. ``skills.max_triggered`` is 0 — refuse. The baseline selects nothing at
       that cap, so there is nothing for one pick to fit inside;
    4. discover candidates and the baseline arm on THIS thread, in one walk;
    5. read the prior turns on THIS thread through *history_source*, and only
       when the history budget is above 0;
    6. submit the rounds to *loop* and wait ONCE for the whole turn's budget;
    7. record both arms and publish the outcome, still on THIS thread.

    *history_source* is a CALLABLE, not a list, so a transcript read costs
    nothing on the turns this point refuses: it is invoked only after gates 1-3
    have passed AND only when the history budget is above 0, which at the shipped
    default of 0 means never. It returns newest-LAST rows carrying ``role`` and
    ``content`` (``ConversationLog.recent``'s shape); a raise reads as no history.

    A budget expiry cancels the future and returns ``None``. ``cancel()`` cannot
    stop a coroutine that already started, so the guarantee is the stronger one
    available: the result is never read again, so a late answer cannot alter the
    message that was assembled without it.
    """
    try:
        # Closed, or not running: such a loop will never run the coroutine, so
        # waiting on that future would spend the whole budget on a certain
        # refusal.
        if loop is None or loop.is_closed() or not loop.is_running():
            return None
        if _this_thread_runs_a_loop():
            return None
        if not core.is_enabled(POINT, session_key=session_key):
            return None
        cap = _max_triggered(skills_loader)
        if cap <= 0:
            return None
        baseline: list[str] = []
        candidates = candidates_from_loader(
            skills_loader, text, project_dir, session_key=session_key, baseline_out=baseline
        )
        if not candidates:
            return None
        rows = screen_candidates(candidates)
        if not rows:
            return None
        # The budget FIRST: at its shipped default of 0 there is nothing for a
        # transcript read to contribute, and reading 20 messages to discard all of
        # them is a cost every sampled turn would otherwise pay for nothing.
        history_budget = _history_budget()
        history = _prior_turns(history_source) if history_budget > 0 else []
        wait = _wait_budget()
        turn_id = uuid.uuid4().hex[:16]
        trace: dict[str, Any] = {}
        started = time.monotonic()
        coro = select_skills(
            text,
            rows,
            session_key=session_key,
            history=history,
            history_budget_chars=history_budget,
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
            picked = future.result(timeout=wait)
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            future.cancel()
            return None
        if picked is None:
            return None
        injected = list(picked)[:cap]
        _record_outcome(
            skills_loader,
            project_dir,
            session_key=session_key,
            baseline=baseline,
            injected=injected,
            trace=trace,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return injected
    except Exception:
        # Every failure keeps the shipped selection. This sits on the path that
        # assembles every message, so the seam may cost an observation and must
        # never cost a turn.
        logger.debug("skills.select: keeping the trigger-matched selection", exc_info=True)
        return None


async def select_skills(
    text: str,
    candidates: Sequence[dict[str, str]],
    *,
    session_key: str | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
    history_budget_chars: int | None = None,
    turn_id: str | None = None,
    deadline: float | None = None,
    trace: dict[str, Any] | None = None,
) -> list[str] | None:
    """Ask the oracle which skill *text* needs. Runs on the event loop.

    ONE question, deliberately: the answer is consumed, and a second question
    would be a second thing to reconcile with a cap of one pick. The menu is
    bounded by :data:`MAX_CANDIDATES` and :data:`MAX_DESCRIPTION_CHARS`, so the
    request has a ceiling without a budget of its own.

    *deadline* is a ``time.monotonic()`` reading the call must start inside. It
    is checked BEFORE the call rather than raced against: a call started past the
    deadline is one whose answer the caller has already stopped waiting for.

    *trace* is filled with what the caller needs for the outcome row (the turn
    id, the menu size, the history cost, the probability), so the caller need not
    reconstruct any of it from the answer.
    """
    rows = screen_candidates(candidates)
    if not rows:
        return None
    turn = turn_id or uuid.uuid4().hex[:16]
    state_trace: dict[str, Any] = {}
    history_rows = build_history(
        history, text, history_budget_chars=history_budget_chars, trace=state_trace
    )
    extra: dict[str, Any] = {
        "turn_id": turn,
        "candidates": len(rows),
        "message_chars": message_chars(text),
        "history_chars": state_trace["history_chars"],
        "truncated": state_trace["truncated"],
    }
    if trace is not None:
        trace.update(extra)
        trace["p"] = None
    picked, p = await _ask_one(
        text, rows, history_rows, session_key=session_key, extra=extra, deadline=deadline
    )
    if picked is None:
        return None
    if trace is not None:
        trace["p"] = p
    return list(picked)


async def _ask_one(
    text: str,
    batch: Sequence[dict[str, str]],
    history_rows: Sequence[dict[str, str]],
    *,
    session_key: str | None,
    extra: dict[str, Any],
    deadline: float | None,
) -> tuple[list[str] | None, float | None]:
    """The pick: ``([key] | [], p)``, or ``(None, None)`` for keep-the-baseline."""
    if deadline is not None and time.monotonic() >= deadline:
        logger.debug("skills.select: the call would start past the deadline")
        return None, None
    keys = [row["key"] for row in batch]
    if not keys:
        return None, None
    state = build_state_rows(text, batch, history_rows)
    questions: list[Question] = [
        Choice(
            "pick",
            "Which skill should be loaded for this message? "
            f"Answer {NONE_OPTION} if none of them apply.",
            options=keys + [NONE_OPTION],
        )
    ]
    answers = await core.decide(POINT, state, questions, session_key=session_key, extra=extra)
    picked = read_answer(answers, keys)
    return picked, (_probability_of(answers) if picked is not None else None)


def _probability_of(answers: Any) -> float | None:
    """The ``pick`` answer's probability, or ``None``. Only read after :func:`read_answer`."""
    if not isinstance(answers, dict):
        return None
    answer = answers.get("pick")
    return answer.p if isinstance(answer, Answer) else None


def read_answer(answers: Any, keys: Sequence[str]) -> list[str] | None:
    """The admissible reading of *answers*: ``[key]``, ``[]``, or ``None``.

    Identity is exact. A value that is not one of the keys just offered — a
    near-miss spelling, a prefix, a skill that was capped out of the menu — is
    ``None`` (keep the baseline) rather than a best-effort resolution, because
    the string is about to be handed to a loader that resolves skills by name.
    """
    if not isinstance(answers, dict):
        return None
    answer = answers.get("pick")
    if not isinstance(answer, Answer):
        return None
    value = answer.value
    if not isinstance(value, str):
        return None
    if value == NONE_OPTION:
        return []
    return [value] if value in keys else None


def _record_menu_failure(session_key: str | None) -> None:
    """One ``ERROR_CANDIDATES`` row, written on this (executor) thread. Never raises."""
    try:
        _log.append(
            _log.build_row(
                point=POINT, session_key=session_key, latency_ms=0, error=ERROR_CANDIDATES
            )
        )
    except Exception:
        logger.debug("skills.select: could not record the menu failure", exc_info=True)


def screen_candidates(candidates: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """The menu rows that may be sent: capped, key-screened, description-clipped.

    The message and each description are truncated because they are prose. Keys
    are not, for the reason :data:`~kiro_crew.decisions.points.MAX_KEY_CHARS`
    exists: an over-long key is dropped by the enumerator instead.
    """
    rows: list[dict[str, str]] = []
    for candidate in list(candidates)[:MAX_CANDIDATES]:
        key = str(candidate.get("key", ""))
        if not key or len(key) > MAX_KEY_CHARS:
            continue
        rows.append(
            {
                "key": key,
                "description": str(candidate.get("description", ""))[:MAX_DESCRIPTION_CHARS],
            }
        )
    return rows


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
    ``truncated`` (how many entries were clipped to fit — at most one, since the
    budget stops the walk). Both are filled even when nothing is admitted, which
    is what makes "no history was reachable" a row that says ``history_chars=0``
    rather than a row missing a field.
    """
    budget = (
        _history_budget() if history_budget_chars is None else max(0, int(history_budget_chars))
    )
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


def build_state(
    text: str,
    candidates: Sequence[dict[str, str]],
    history: Sequence[Mapping[str, Any]] | None = None,
    *,
    history_budget_chars: int | None = None,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The state sent to the oracle: this message, the prior turns, and the menu.

    Nothing else, and ``history`` only when there are prior turns to send -- see
    :func:`build_state_rows` for why absence is the right default shape.
    """
    return build_state_rows(
        text,
        screen_candidates(candidates),
        build_history(history, text, history_budget_chars=history_budget_chars, trace=trace),
    )


def message_excerpt(text: str) -> str:
    """The part of *text* that actually leaves the machine, after the cap.

    One function so the count on the outcome record and the string in the request
    cannot disagree: :func:`build_state_rows` sends this, and
    :func:`message_chars` measures the same call.
    """
    return (text or "")[:MAX_MESSAGE_CHARS]


def message_chars(text: str) -> int:
    """Characters of *text* that were sent, which is the excerpt's own length.

    The number the strip prints beside ``history_chars``: together they are the
    whole egress of one question, so a reader can see what left rather than infer
    it from the message they typed.
    """
    return len(message_excerpt(text))


def build_state_rows(
    text: str,
    rows: Sequence[dict[str, str]],
    history_rows: Sequence[dict[str, str]],
) -> dict[str, Any]:
    """The state sent to the oracle, over already-screened rows and history.

    ``history`` is OMITTED when there is none, so the request at the shipped
    default carries exactly the shape that shipped before prior turns existed.
    An always-present empty list would change the wire for every consented owner
    who never opts in, against a provider no local test can speak for -- and the
    "was history reachable or merely not sent" question it was there to answer is
    already answered locally by ``history_chars`` on the call row.
    """
    state: dict[str, Any] = {
        "message": message_excerpt(text),
        "candidates": [dict(row) for row in rows],
    }
    if history_rows:
        state["history"] = [dict(entry) for entry in history_rows]
    return state


def candidates_from_loader(
    skills_loader: Any,
    text: str,
    project_dir: str | Path | None = None,
    *,
    session_key: str | None = None,
    baseline_out: list[str] | None = None,
) -> list[dict[str, str]]:
    """Every skill *text* could load, best-scoring first, capped and screened.

    Walks the same ``(name, file, within)`` triples and the same frontmatter
    reader ``get_triggered_skills`` uses, so eligibility cannot drift from the
    baseline's own notion of it — and so a confined project skill is read through
    the descriptor-pinned reader rather than by path.

    Scoring is only an ORDER here: a skill below ``MIN_TRIGGER_OVERLAP`` is still
    offered, which is the whole point of asking. A NEGATIVE trigger that matches
    the message excludes the skill unconditionally, which is stricter than the
    baseline (which only records a negation as a veto when the positive score
    would otherwise have won) and cannot drop anything the baseline selected: a
    negated skill never reaches the baseline's own result either.

    *baseline_out*, when given, is filled with what word overlap WOULD have
    injected: the same score threshold, the same score-descending order with ties
    left in walk order, and the same ``skills.max_triggered`` cut that
    ``get_triggered_skills`` applies. It is computed from this walk, so the
    comparison arm costs no second enumeration and no second frontmatter read. An
    out-parameter rather than a second return value: every existing caller reads
    the menu and a tuple would break them all to serve one.

    Failure is not silent. This reaches into the loader's internals, so a loader
    refactor can break it without breaking anything else; when the walk raises,
    or every entry it yields is unreadable, one row with ``ERROR_CANDIDATES`` is
    written so the operator sees a broken menu instead of an empty log.
    """
    try:
        visible = list(skills_loader._iter_visible(project_dir))
    except Exception:
        logger.debug("skills.select: candidate listing failed", exc_info=True)
        _record_menu_failure(session_key)
        return []

    text_words = words_of(text or "")
    scored: list[tuple[float, str, str]] = []
    matched: list[tuple[float, int, str]] = []
    unreadable = 0
    for position, (name, skill_file, within) in enumerate(visible):
        key = str(name or "")
        # An over-long key is dropped, never shortened: the answer is resolved by
        # name downstream.
        if not key or len(key) > MAX_KEY_CHARS:
            continue
        try:
            meta = skills_loader._cached_frontmatter(skill_file, within=within)
        except Exception:
            # A skill whose metadata cannot be read is one the baseline cannot
            # select either, so dropping it keeps the two menus comparable.
            unreadable += 1
            continue
        if str(meta.get("always", "")).strip().lower() == "true":
            continue
        triggers = str(meta.get("triggers", "") or "")
        if not triggers.strip():
            continue
        scope = str(meta.get("repo_scope", "") or "").strip()
        if scope and not _repo_scope_ok(skills_loader, scope, project_dir):
            continue
        score, negated = trigger_score(triggers, text_words)
        if negated:
            continue
        # The baseline arm: the same threshold the matcher applies, recorded with
        # the walk POSITION so ties keep the order a stable sort on score alone
        # would have left them in -- which is the order the matcher's own cut
        # sees.
        if score >= MIN_TRIGGER_OVERLAP:
            matched.append((score, position, key))
        scored.append((score, key, str(meta.get("description", "") or "")))

    if not scored and unreadable:
        # Every entry the walk yielded failed to read: that is the reader, not
        # the tree, and it must not look like "nothing installed".
        logger.debug("skills.select: %d candidate(s) unreadable, none offered", unreadable)
        _record_menu_failure(session_key)

    if baseline_out is not None:
        matched.sort(key=lambda row: (-row[0], row[1]))
        baseline_out[:] = [
            key for _score, _position, key in matched[: _max_triggered(skills_loader)]
        ]

    # Score descending, then key, so the cap keeps the same menu on every run for
    # the same tree and message.
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [
        {"key": key, "description": description[:MAX_DESCRIPTION_CHARS]}
        for _score, key, description in scored[:MAX_CANDIDATES]
    ]


def injected_chars(skills_loader: Any, key: str, project_dir: str | Path | None) -> int:
    """Characters *key*'s body contributes to a prompt, or 0 when it cannot be read.

    The body without its frontmatter, which is what ``build_message`` appends.
    A pointer-only skill actually contributes one line instead, so counting its
    body overstates it; the estimate deliberately stays the cheap one, because
    the split is a per-skill loader call and bodies are the block the comparison
    is about.
    """
    try:
        content = skills_loader.load_skill(key, project_dir)
        if not content:
            return 0
        return len(skills_loader.strip_frontmatter(content))
    except Exception:
        logger.debug("skills.select: could not size skill %r", key, exc_info=True)
        return 0


def tokens_saved(
    skills_loader: Any,
    baseline: Sequence[str],
    injected: Sequence[str],
    project_dir: str | Path | None = None,
) -> int:
    """Estimated tokens the pick saves over the baseline. Negative when it costs.

    Only the SYMMETRIC DIFFERENCE is measured: a skill both arms chose
    contributes the same characters to both sides and cancels, so measuring it
    would be a file read that cannot change the answer. That is what keeps the
    common "they agree" case free of body reads entirely.
    """
    chosen = set(injected)
    kept = set(baseline)
    only_baseline = sum(
        injected_chars(skills_loader, key, project_dir) for key in baseline if key not in chosen
    )
    only_injected = sum(
        injected_chars(skills_loader, key, project_dir) for key in injected if key not in kept
    )
    return int((only_baseline - only_injected) / CHARS_PER_TOKEN)


def build_outcome(
    skills_loader: Any,
    project_dir: str | Path | None,
    *,
    baseline: Sequence[str],
    injected: Sequence[str],
    trace: Mapping[str, Any],
) -> dict[str, Any]:
    """Both arms of one turn as the fields the row and the publish hook share.

    ``jev`` is the list actually injected, cap applied — not the raw answer — so
    a reader comparing the arms is comparing what reached the prompt. ``agree``
    is SET equality: the two arms are selections, and an order difference between
    two identical sets is not a disagreement about which skills apply.

    ``message_chars`` and ``history_chars`` are the whole egress of the question,
    each measured on the string that was actually sent, so a reader of the strip
    can see what left this machine rather than infer it. A clip count is NOT on
    the record: it is a fact about the history READ, which the call row carries,
    and a reader comparing two arms cannot act on it.
    """
    return {
        "turn_id": trace.get("turn_id"),
        "baseline": list(baseline),
        "jev": list(injected),
        "agree": set(baseline) == set(injected),
        "p": trace.get("p"),
        "tokens_saved": tokens_saved(skills_loader, baseline, injected, project_dir),
        "candidates": trace.get("candidates"),
        "message_chars": trace.get("message_chars"),
        "history_chars": trace.get("history_chars"),
    }


def _record_outcome(
    skills_loader: Any,
    project_dir: str | Path | None,
    *,
    session_key: str | None,
    baseline: Sequence[str],
    injected: Sequence[str],
    trace: Mapping[str, Any],
    latency_ms: int,
) -> None:
    """One outcome row for the turn, then the publish hook. Never raises.

    Guarded as a whole and separately from the selection: the pick is already
    decided by the time this runs, so neither a log failure nor a missing
    outcomes module may cost the turn its answer.

    Written ONCE per turn, beside the call row the gate writes: that row says what
    was asked, this one says what both arms chose. The publish is CONDITIONAL on
    the write: a strip whose durable row was refused describes a decision a verdict
    could not be filed against. Rows exist only for a
    real answer — a refused turn already has the gate's own row carrying the
    error category, and an ``agree`` computed against an answer that never
    arrived would be a comparison of one arm with nothing.
    """
    try:
        outcome = build_outcome(
            skills_loader,
            project_dir,
            baseline=baseline,
            injected=injected,
            trace=trace,
        )
        row = _log.build_row(
            point=POINT,
            session_key=session_key,
            latency_ms=latency_ms,
            extra=outcome,
        )
        written = _log.append(row)
    except Exception:
        logger.debug("skills.select: could not record the outcome row", exc_info=True)
        return
    if not written:
        # The strip describes a decision whose durable row was refused, so there is
        # nothing for a verdict against its turn id to be about. The log's own
        # WARNING already says why the write went.
        logger.debug("skills.select: outcome row was not written; not publishing it")
        return
    publish_outcome(session_key, row)


def publish_outcome(session_key: str | None, outcome: dict[str, Any]) -> bool:
    """Hand *outcome* to :data:`OUTCOMES_MODULE` if this build has one. Never raises.

    Returns whether a publisher ran, for a test to assert on. Resolved by name at
    CALL time rather than imported at module scope: the module is optional, and a
    top-level import would make this point unimportable on a build without it —
    turning an observation feature into a broken hot path.

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
        logger.debug("skills.select: could not publish the outcome", exc_info=True)
        return False


def _prior_turns(
    history_source: Callable[[], Sequence[Mapping[str, Any]]] | None,
) -> list[Mapping[str, Any]]:
    """The caller's prior turns, or an empty list. Never raises.

    Called only after the gates, so a transcript read is paid on the sampled
    turns and nowhere else. A source that raises reads as no history rather than
    as a failed selection: prior turns make the question better, they are not
    what makes it answerable.
    """
    if history_source is None:
        return []
    try:
        return list(history_source() or [])
    except Exception:
        logger.debug("skills.select: prior turns unreadable", exc_info=True)
        return []


def _repo_scope_ok(skills_loader: Any, scope: str, project_dir: str | Path | None) -> bool:
    """The loader's own repo-scope gate. A gate that fails reads as NOT satisfied."""
    try:
        return bool(skills_loader._repo_scope_satisfied(scope, project_dir))
    except Exception:
        logger.debug("skills.select: repo scope gate failed", exc_info=True)
        return False


def _max_triggered(skills_loader: Any) -> int:
    """The live per-message cap, or 0 when it cannot be read.

    0 is the fail-closed answer: it means no selection and no call, which is
    exactly what the shipped default (``skills.max_triggered: 0``) already does.
    """
    try:
        return int(skills_loader._max_triggered_now())
    except Exception:
        logger.debug("skills.select: trigger cap unreadable", exc_info=True)
        return 0


def _history_budget() -> int:
    """``decisions.history_budget_chars``, or 0 when it cannot be read.

    0 is the fail-closed direction for this one: it sends the message alone,
    which is exactly what the seam did before prior turns were part of the state.
    """
    try:
        return max(0, int(core.history_budget_chars()))
    except Exception:
        logger.debug("skills.select: history budget unreadable", exc_info=True)
        return 0


def _wait_budget() -> float:
    """How long the caller's thread may wait, clamped into a sane window."""
    try:
        budget = float(core.timeout_secs()) + WAIT_MARGIN_SECS
    except Exception:
        logger.debug("skills.select: provider budget unreadable", exc_info=True)
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
