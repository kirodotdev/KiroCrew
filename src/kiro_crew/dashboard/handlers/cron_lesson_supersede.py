"""Retiring the lessons a newly written rule contradicts.

After a lesson is stored, a background sweep asks the model whether each existing
candidate contradicts the new rule, and supersedes the ones that do.

Served through :mod:`kiro_crew.dashboard.handlers.cron`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.dashboard.handlers.cron_shared import _sel
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.llm_helpers import run_bg_oneliner

logger = logging.getLogger("kiro_crew.dashboard.handlers.cron")


_CONTRADICTION_PROMPT = (
    "Given an OLD rule and a NEW rule, determine if they contradict each other "
    "(following both simultaneously is impossible or produces conflicting behavior).\n\n"
    "OLD: {old_rule}\n\nNEW: {new_rule}\n\n"
    "Respond with exactly one word: CONTRADICTORY, COMPLEMENTARY, or UNRELATED."
)

_CONTRADICTION_MODEL = "auto"  # inherit the governed default; a hardcoded id 400s where unavailable
# Per-candidate cap on the background contradiction verdict. The sweep runs
# fire-and-forget after the lesson is already persisted, so this bounds a hung
# model call rather than gating the write path.
_CONTRADICTION_TIMEOUT = 60.0


async def _classify_contradiction(state: DashboardState, prompt: str) -> str:
    """Run one contradiction classification on the shared ``_bg`` runtime.

    Mirrors the lightweight background path used by title/suggestion generation
    (``chat_title`` / ``suggestions``): acquire an ephemeral ``_bg`` session
    handle via ``get_bg_session``, best-effort pin it to the cheap model, stream
    to completion while rejecting any tool call (the classification is
    tool-free), then always ``destroy()`` the handle. A fresh handle per call
    keeps each verdict a clean binary classification — no cross-candidate turn
    history — and avoids the unbounded context growth of a single long-lived
    session reused across every ``learn_add``. Bounded by
    ``_CONTRADICTION_TIMEOUT``. Returns the first upper-cased token of the
    model's reply (e.g. ``"CONTRADICTORY"``), or ``""`` on empty output.
    """
    text = await run_bg_oneliner(
        state.sessions,
        prompt,
        model=_CONTRADICTION_MODEL,
        sel_source="contradiction_check",
        timeout=_CONTRADICTION_TIMEOUT,
    )
    stripped = text.strip()
    return stripped.upper().split()[0] if stripped else ""


async def _resolve_contradictions(
    state: DashboardState, new_rule: str, candidates: list[dict]
) -> list[tuple[str, str | None]]:
    """Use an LLM to identify which candidate lessons contradict the new rule.

    Each candidate is classified independently on a fresh ``_bg`` runtime
    session (see ``_classify_contradiction``). A per-candidate failure/timeout
    is swallowed so one bad verdict never aborts the sweep — the lesson is
    already persisted, and a missed verdict self-heals on the next ``learn_add``
    touching the topic.

    Each entry is ``(key, value_json)``: the key alone is not enough to delete
    safely, because the body under it can be replaced while this loop waits on a
    verdict. The caller hands the body back as ``expect_value_json``.
    """
    to_delete: list[tuple[str, str | None]] = []
    for candidate in candidates:
        prompt = _CONTRADICTION_PROMPT.format(old_rule=candidate["rule"], new_rule=new_rule)
        try:
            verdict = await _classify_contradiction(state, prompt)
        except Exception:
            logger.debug("Contradiction check failed for %r", candidate["key"], exc_info=True)
            continue
        if verdict == "CONTRADICTORY":
            logger.info(
                "Contradiction: new %r supersedes %r (sim=%.2f)",
                new_rule[:60],
                candidate["rule"][:60],
                candidate["similarity"],
            )
            # ``None`` when the candidate carried no body: an unguarded delete is
            # the pre-existing behaviour, so a caller shaping its own candidates
            # keeps working rather than silently never deleting.
            body = candidate.get("value_json")
            to_delete.append((candidate["key"], body if isinstance(body, str) else None))
    return to_delete


async def _resolve_and_supersede(
    state: DashboardState, sk: str, rule: str, candidates: list[dict], vs: Any
) -> None:
    """Resolve V1 contradictions and delete superseded lessons in background.

    Split out of ``api_lessons_create`` so the slow per-candidate LLM verdict
    does not block the HTTP response. Deletes are emitted with the same SEL
    audit event as the inline path. Exceptions are swallowed (logged) —
    a failed background sweep must never crash the event loop, and the lesson
    itself is already persisted.
    """
    # V2 keeps distinct rules for explicit owner review. A model's guessed
    # contradiction is not authority to remove an existing private memory.
    if getattr(vs, "algorithm_version", "v1") == "v2":
        return
    try:
        contradicted = await _resolve_contradictions(state, rule, candidates)
    except Exception:
        # Outer guard: this runs as a fire-and-forget background task, so an
        # unhandled raise would surface only as a noisy "Task exception was never
        # retrieved" — and the lesson is already persisted (not data loss). warning,
        # not debug: a persistent sweep failure means contradicted lessons accumulate
        # uncleaned, and running in the background means no request timeout surfaces
        # the failure — operators need the visibility.
        logger.warning("Background contradiction sweep failed", exc_info=True)
        return
    for key, expect_body in contradicted:
        try:
            # Audit the supersede DECISION *before* the destructive delete: a
            # lesson must never be deleted without a SEL record, so if the audit
            # call itself raises (audit-service blip) we skip the delete for this
            # key rather than deleting unaudited.
            _sel().log_api_access(
                caller=sk,
                operation="lesson.contradiction_superseded",
                outcome="allowed",
                source="dashboard",
                resources=key,
            )
            # COMPARE-AND-DELETE on the body read at write time. The candidates are
            # a write-time snapshot and this runs after a per-candidate LLM verdict,
            # so the row under this key can have been replaced in between --
            # ``_lesson_key`` keys on rule text plus scope alone, and re-tiering a
            # rule is a delete plus a re-add under that same key. An unconditional
            # delete here tombstones the replacement, which defeats the tier filter
            # in ``api_lessons_create``: the replacement can be the `always` row the
            # filter refuses to let a finding retire. Same guard the inline dedup
            # pass applies to its own deferred supersedes, for the same reason.
            #
            # delete_semantic is a sync FAISS op; off-load so this background
            # sweep doesn't block concurrent dashboard/Slack requests on the loop.
            deleted = await asyncio.to_thread(
                vs.delete_semantic,
                key,
                "contradiction_superseded",
                expect_value_json=expect_body,
            )
            if not deleted:
                # Not an error: the row changed or went while the verdict was
                # pending, so what this decided to retire is already gone.
                # A contradiction against whatever replaced it is re-nominated by
                # the next write touching the topic.
                logger.info("Contradicted lesson %s changed while its verdict ran; kept", key)
                continue
            logger.info("Deleted contradicted lesson: %s", key)
        except Exception:
            # per-key so one bad/already-deleted key doesn't abort the batch (a
            # concurrent sweep may have deleted it — candidates are a write-time snapshot).
            logger.warning("Failed to supersede contradicted lesson %s", key, exc_info=True)
            continue
