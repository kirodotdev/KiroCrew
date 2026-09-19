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
plus one frontmatter read per skill) therefore happens on that worker thread,
never on the event loop that serves the gateway. Only the ``decide`` await is
submitted to the loop, and the caller waits on it for a bounded budget.

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
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
from pathlib import Path
from typing import Any, Sequence

from kiro_crew import decisions as core
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.points import MAX_KEY_CHARS
from kiro_crew.decisions.types import Answer, Choice, Question
from kiro_crew.trigger_match import trigger_score, words_of

logger = logging.getLogger(__name__)

POINT = "skills.select"

#: Cap on how many skills are described to the oracle. The menu has to stay a
#: small prompt, so the lowest-scoring tail is dropped rather than sent.
MAX_CANDIDATES = 100
MAX_MESSAGE_CHARS = 2000
MAX_DESCRIPTION_CHARS = 200

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


def selected_skills(
    skills_loader: Any,
    text: str,
    project_dir: str | Path | None = None,
    *,
    session_key: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
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
    4. discover candidates on THIS thread;
    5. submit the ``decide`` await to *loop* and wait for a bounded budget.

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
        candidates = candidates_from_loader(
            skills_loader, text, project_dir, session_key=session_key
        )
        if not candidates:
            return None
        coro = select_skills(text, candidates, session_key=session_key)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except BaseException:
            # A coroutine that never got scheduled has to be closed HERE.
            # Dropping it unscheduled emits "coroutine was never awaited" from
            # whichever unrelated test later triggers the GC.
            coro.close()
            raise
        try:
            picked = future.result(timeout=_wait_budget())
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            future.cancel()
            return None
        if picked is None:
            return None
        return list(picked)[:cap]
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
) -> list[str] | None:
    """Ask the oracle which skill *text* needs. Runs on the event loop.

    ONE question, deliberately: the answer is consumed, and a second question
    would be a second thing to reconcile with a cap of one pick.
    """
    state = build_state(text, candidates)
    keys = [candidate["key"] for candidate in state["candidates"]]
    if not keys:
        return None
    questions: list[Question] = [
        Choice(
            "pick",
            "Which skill should be loaded for this message? "
            f"Answer {NONE_OPTION} if none of them apply.",
            options=keys + [NONE_OPTION],
        )
    ]
    answers = await core.decide(POINT, state, questions, session_key=session_key)
    return read_answer(answers, keys)


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


def build_state(text: str, candidates: Sequence[dict[str, str]]) -> dict[str, Any]:
    """The state sent to the oracle: the message and the menu, nothing else.

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
    return {"message": (text or "")[:MAX_MESSAGE_CHARS], "candidates": rows}


def candidates_from_loader(
    skills_loader: Any,
    text: str,
    project_dir: str | Path | None = None,
    *,
    session_key: str | None = None,
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
    unreadable = 0
    for name, skill_file, within in visible:
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
        scored.append((score, key, str(meta.get("description", "") or "")))

    if not scored and unreadable:
        # Every entry the walk yielded failed to read: that is the reader, not
        # the tree, and it must not look like "nothing installed".
        logger.debug("skills.select: %d candidate(s) unreadable, none offered", unreadable)
        _record_menu_failure(session_key)

    # Score descending, then key, so the cap keeps the same menu on every run for
    # the same tree and message.
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [
        {"key": key, "description": description[:MAX_DESCRIPTION_CHARS]}
        for _score, key, description in scored[:MAX_CANDIDATES]
    ]


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
