"""What a task run tells the operator and remembers when a step ends.

Notifications routed back to the conversation the run was started from, each finished
step's line in the conversation history, and the lesson a failing step leaves behind
(gated by the memory switches, written to the run's own store). New run notices and new
step-outcome memory go here.

Instance fields written: none.

Instance fields read: ``_agent``, ``_consolidator``, ``_conversation_log``, ``_ctx``,
``_lesson_store``, ``_on_notify``, ``_run_session_keys``, ``_sessions``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.memory_sections import lesson_extraction_enabled
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.learn import Lesson
from kiro_crew.llm_helpers import stream_and_collect_json
from kiro_crew.session import BACKGROUND_KEY
from kiro_crew.task_models import Project, Task
from kiro_crew.task_reporter import notify
from kiro_crew.taskrunner_parts.run_scope import _SESSION_PREFIX

if TYPE_CHECKING:
    from kiro_crew.taskrunner import TaskRunner


# The facade's logger: records keep the name ``kiro_crew.taskrunner``.
logger = logging.getLogger("kiro_crew.taskrunner")


async def _notify(self: TaskRunner, title: str, body: str, run: Project | None = None) -> None:
    # The originating conversation is per-run, so it is resolved from the run
    # rather than passed at each of the ~20 call sites. A notification with no
    # run attached (a lesson learned, say) carries no conversation and falls
    # back to the sink's own default destination.
    session_key = self._run_session_keys.get(run.task_id, "") if run else ""
    await notify(title, body, run=run, callback=self._on_notify, session_key=session_key)


def _log_task(self: TaskRunner, history_key: str, run: Project, task: Task) -> None:
    if not self._conversation_log or (
        run.execution_context and run.execution_context.memory_mode != "persistent"
    ):
        return
    spec_name = Path(run.spec_path).name if run.spec_path else run.task_id
    user_msg = f"[Task: {spec_name}] Task {task.index}: {task.title}"
    result_summary = task.result[:2000] if task.result else "Task completed."
    log = self._conversation_log

    def _do() -> None:
        # Both appends run on ONE worker thread so they persist IN ORDER —
        # two independent append_off_loop dispatches could interleave on the
        # default executor and reorder the transcript — and take the patient
        # off-loop cross-process lock acquire path.
        log.append(history_key, "user", user_msg)
        log.append(history_key, "assistant", result_summary)

    # _log_task is invoked from async task_executor code running ON the
    # event loop. A direct append there hits _locked's fail-fast on-loop
    # path (raises HistoryLockTimeout under benign contention, silently
    # swallowed) and risks a synchronous disk write on the loop. Offload to
    # a worker thread so it takes the blocking off-loop acquire and can
    # neither stall the loop nor drop the write under contention.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _do()
        except Exception:
            logger.debug("Failed to log task to conversation history", exc_info=True)
        return

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.debug("Failed to log task to conversation history: %r", exc)

    loop.run_in_executor(None, _do).add_done_callback(_report)


async def _extract_lesson(self: TaskRunner, task: Task, run: Project | None = None) -> None:
    # Global persistence switch (memory.persistence_enabled), plus
    # memory.writes_enabled and memory.lesson_extraction_enabled:
    # skip BEFORE the LLM call, so a disabled system spends no turn
    # distilling a lesson it is not allowed to store. Placed ahead of store
    # resolution so member-private lesson stores are covered too.
    if not lesson_extraction_enabled(KiroCrewConfig.load().memory):
        return
    try:
        from kiro_crew.memory_stores import UnknownMemoryStore

        execution = run.execution_context if run else self._capture_execution()
        if execution is None:
            execution = self._capture_execution()
        if execution.memory_mode != "persistent":
            return
        runtime_key = f"{_SESSION_PREFIX}:{run.task_id}:runtime" if run else ""
        private_store = execution.store.legacy_name if execution.member_id else None
        lesson_store = self._lesson_store
        if not private_store and not lesson_store:
            return
        private_vectors = None
        if private_store:
            context = self._ctx
            if context is None:
                raise UnknownMemoryStore("The task's private lesson context is unavailable")
            if run is not None:
                await self._bind_run_execution(run, runtime_key)
            private_vectors = await context.ensure_store(private_store)
        prompt = (
            "A task failed after multiple attempts.\n\n"
            f'Task: "{task.title}"\n'
            f'Error: "{task.error[:500]}"\n\n'
            "Extract a concise lesson. Format as JSON:\n"
            '{"rule": "what to always do", '
            '"negative": "what to never do", '
            '"category": "tool"}\n\n'
            "Respond with ONLY valid JSON."
        )
        result = (
            await self._call_llm_for_lesson(prompt, runtime_key=runtime_key)
            if private_store
            else await self._call_llm_for_lesson(prompt)
        )
        if not result or "rule" not in result:
            return
        rule = result["rule"]
        category = result.get("category", "tool")
        negative = result.get("negative")
        if private_store:
            if private_vectors is None:
                raise UnknownMemoryStore("The task's private lesson store is unavailable")
            write_result = await run_in_embed_pool(
                private_vectors.write_lesson, rule, category, negative, "task_runner"
            )
            if write_result.outcome.value == "refused":
                return
        elif self._consolidator and self._consolidator._vector_store:
            # write_lesson embeds via blocking urllib (Ollama); offload to
            # keep the gateway event loop responsive (same pattern as
            # dashboard/handlers/cron_lessons.py api_lessons_create).
            write_result = await run_in_embed_pool(
                self._consolidator._vector_store.write_lesson,
                rule,
                category,
                negative,
                "task_runner",
            )
            if write_result.outcome.value == "refused":
                return
        else:
            if lesson_store is None:
                return
            # Offloaded for the same reason as write_lesson above, and now
            # necessarily so: LessonStore locks per PATH, so instances in
            # different components share one lock. A dashboard writer holding
            # it across its read-and-rewrite would stall this loop if save()
            # ran here. The lock is what makes the write atomic, so the fix is
            # to move the caller off the loop rather than to weaken it.
            outcome = await asyncio.to_thread(
                lesson_store.save,
                Lesson(
                    ts=datetime.now(tz=timezone.utc).isoformat(),
                    rule=rule,
                    category=category,
                    negative=negative,
                ),
            )
            if outcome == "refused":
                return
        logger.info("Lesson extracted from task %d: %s", task.index, rule)
        if run:
            run.lessons_learned.append(rule)
        await self._notify("\U0001f4dd Lesson learned", rule)
    except Exception:
        logger.debug("Lesson extraction failed", exc_info=True)


async def _call_llm_for_lesson(
    self: TaskRunner, prompt: str, *, runtime_key: str = ""
) -> dict | None:
    session_key = f"{runtime_key}:lesson" if runtime_key else BACKGROUND_KEY
    if runtime_key:
        from kiro_crew.context import inherit_session_memory

        await inherit_session_memory(self._ctx, runtime_key, session_key)
    try:
        if runtime_key:
            client, _is_new, _resumed = await self._sessions.open_task_session(
                runtime_key, session_key, agent=self._agent or None
            )
        else:
            client, _is_new, _resumed = await self._sessions.get_or_create(
                session_key,
                agent=self._agent or None,
            )
        return await stream_and_collect_json(client, prompt)
    except Exception:
        logger.debug("LLM lesson extraction call failed", exc_info=True)
        return None
    finally:
        self._sessions.release(session_key)
        if runtime_key:
            await self._sessions.reset(session_key)
        else:
            await self._sessions.recycle_background()
