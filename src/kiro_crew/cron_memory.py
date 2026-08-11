"""Bridge cron-session work into the agent memory system.

Agent-mode (message) cron runs historically wrote only to the cron
execution-history store (``cron-history/{job_id}.jsonl``), which has no
connection to :class:`~kiro_crew.history.HistoryConsolidator` or the memory
stores.  A cron could root-cause a defect and open a PR, yet leave no trace in
memory — a later interactive session would re-derive the same work from
scratch.

This module records each successful agent-mode run as a user/assistant
exchange in the canonical :class:`~kiro_crew.history.ConversationLog` under a
DEDICATED stable key (``cron-mem:{job_id}``) and immediately triggers
fire-and-forget consolidation, so the run flows through the same
history/semantic/episodic/lesson extraction as interactive sessions.

Why a dedicated key rather than the existing ``cron:{job_id}``: that key's
emptiness for hidden crons is a documented invariant (see the slot-creation
comment in ``slack/gateway.py``) — it exists solely to feed a dashboard
follow-up turn.  Overloading it would replay every recorded run into the
follow-up agent's context.  ``cron-mem:{job_id}`` is written for ALL
agent-mode jobs (silent, hidden, and ``persistent_session=False`` included)
and read only by the consolidator.

Noise control deliberately does NOT live here: recording is default-on, and
significance is judged at consolidation time (a trivial run distills to
nothing — see the empty-``history_entry`` contract in
``HistoryConsolidator._consolidate``), never configured per job.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Coroutine

    from kiro_crew.cron import CronJob
    from kiro_crew.history import ConversationLog

logger = logging.getLogger(__name__)

#: Dedicated ConversationLog key prefix for cron memory transcripts.
CRON_MEMORY_KEY_PREFIX = "cron-mem:"

# Strong references for detached recording tasks: asyncio keeps only weak
# refs to tasks, so a fire-and-forget task without an anchor can be
# garbage-collected mid-flight. Bounded by the number of concurrently
# finishing cron runs; entries remove themselves on completion.
_DETACHED_TASKS: set["asyncio.Task[Any]"] = set()


def _detach_cron_memory_task(coro: "Coroutine[Any, Any, bool]") -> None:
    """Run a recording coroutine as a detached task, outside any timeout.

    The cron executor calls this instead of awaiting so the transcript write
    never spends the job's execution deadline (a run finishing near its
    timeout would otherwise be marked timed out by its own memory
    bookkeeping). ``record_cron_run_to_memory`` already catches and logs its
    own failures; the callback guard here covers only cancellation and
    truly unexpected exits.
    """
    task = asyncio.get_running_loop().create_task(coro)
    _DETACHED_TASKS.add(task)

    def _done(t: "asyncio.Task[Any]") -> None:
        _DETACHED_TASKS.discard(t)
        if t.cancelled():
            logger.debug("Detached cron memory recording cancelled")
        elif t.exception() is not None:
            logger.warning(
                "Detached cron memory recording failed", exc_info=t.exception()
            )

    task.add_done_callback(_done)


#: Placeholder the executor substitutes for an empty model response — carries
#: no information worth remembering, so it is treated as an empty result.
_EMPTY_RESULT_PLACEHOLDER = "_No response._"


def cron_memory_key(job_id: str) -> str:
    """Return the dedicated memory-transcript key for *job_id*."""
    return f"{CRON_MEMORY_KEY_PREFIX}{job_id}"


async def record_cron_run_to_memory(
    conversation_log: "ConversationLog | None",
    job: "CronJob",
    result_text: str | None,
) -> bool:
    """Record a successful agent-mode cron run for later consolidation.

    Appends the run as a user turn (the job's message) and an assistant turn
    (the run's result) under :func:`cron_memory_key` — and nothing else. The
    transcript FILE is the durable enrollment record: the consolidator's
    heartbeat sweep (``HistoryConsolidator.sweep_cron_memory_keys``) discovers
    ``cron-mem:*`` transcripts from disk, so consolidation needs no
    in-process registration to survive gateway restarts or shutdowns, and a
    one-shot job's exchange is picked up even after its cron is deleted.

    Best-effort by contract: every failure is logged and swallowed — memory
    recording must never fail, delay, or otherwise alter the cron run that
    produced the result.  Returns ``True`` when the exchange was recorded,
    ``False`` on skip or failure (callers only use this for logging/tests).
    """
    try:
        if conversation_log is None:
            return False
        if not result_text or not result_text.strip():
            return False
        if result_text.strip() == _EMPTY_RESULT_PLACEHOLDER:
            return False
        key = cron_memory_key(job.id)
        # Same redaction precedent as the other cron result sinks (dashboard
        # inject, Slack delivery): job.message and the result are LLM/user
        # controllable, so scrub exfiltration URLs + credentials before they
        # become durable transcript rows.
        safe_msg, _ = redact_exfiltration_urls(job.message or "")
        safe_msg, _ = redact_credentials(safe_msg)
        safe_result, _ = redact_exfiltration_urls(result_text)
        safe_result, _ = redact_credentials(safe_result)
        log = conversation_log  # narrowed local: closure keeps the non-None type

        def _append_exchange() -> None:
            # Both rows in ONE worker-thread call under atomic_appends: a
            # single await point means cancellation lands before the thread
            # starts or after it completes (never an unmatched user turn),
            # and the group lock prevents two concurrent detached recordings
            # from interleaving into user/user/assistant/assistant.
            with log.atomic_appends(key):
                log.append(key, "user", safe_msg, agent=job.agent_id or None)
                log.append(key, "assistant", safe_result)

        await asyncio.to_thread(_append_exchange)
        # No trigger, no registration: the heartbeat's disk-backed sweep
        # (sweep_cron_memory_keys) discovers this transcript by its prefix.
        return True
    except Exception:
        logger.warning(
            "Cron memory recording failed for job %s (run unaffected)",
            getattr(job, "id", "?"),
            exc_info=True,
        )
        return False
