"""Who a cron run is: its session key, the principal behind it, the memory it runs with.

A run presents a ``cron:`` session key (:func:`build_cron_session_context` mints
it), and the job id inside that key is the principal jobs the run creates are
owned by -- parsed by the one key parser,
:func:`kiro_crew.cron.cron_job_id_from_session_key`, which the service's release
paths share. Whether a job's key is the same on every run
(:func:`cron_session_key_is_stable`) decides whether that principal outlives one
run. The member and memory store a
job executes as are captured once at creation (:func:`bind_cron_memory`) and read
back at dispatch (:func:`resolve_cron_memory`).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.cron_service.model import CronJob


def agent_sequence_dispatches(seq: list[str]) -> bool:
    """Whether a job's ``agent_sequence`` is what dispatch actually runs.

    A sequence of more than one agent takes precedence over ``agent_id``; a
    shorter one is dormant and dispatch falls through to ``agent_id``. This is
    the ONE spelling of that gate -- the Slack dispatch path, session-key
    stability, and the doctor's disk reader all call it, so a change to the
    dispatch semantics cannot silently leave a consumer reporting (or keying)
    against the old rule.
    """
    return len(seq) > 1


def build_cron_session_context(job: CronJob) -> tuple[str, str]:
    """Compute (session_key, prompt) for one cron run.

    When ``job.persistent_session`` is True (default, legacy behaviour):
      - session_key is stable across runs: ``cron:{job.id}``
      - prompt prepends ``job.last_result`` so the agent has recent context

    When ``job.persistent_session`` is False:
      - session_key is unique per call: ``cron:{job.id}:{uuid}``
        → each run opens a fresh agent session; no context accumulation
      - prompt is the bare ``job.message`` — no last_result injection
        (accumulated state is the other half of the bug)

    The key prefix ``cron:{job.id}`` is preserved in both modes so the
    reaper's existing session-matching logic continues to work.

    This is a pure function — all side effects (session creation, Slack
    delivery, acked_items handling) happen in the caller. Keep it that way
    so it stays trivially unit-testable.
    """
    if job.persistent_session:
        msg = job.message
        if job.last_result:
            last = job.last_result
            if job.minimal_context and len(last) > 2000:
                last = "[truncated]…" + last[-2000:]
            msg = (
                "[Previous run result — do NOT repeat the same content]\n"
                f"{last}\n"
                "[End of previous run result]\n\n"
                f"{msg}"
            )
        return f"cron:{job.id}", msg

    # Stateless: fresh key, bare message.
    run_id = uuid.uuid4().hex[:8]
    return f"cron:{job.id}:{run_id}", job.message


def cron_session_key_is_stable(job: CronJob) -> bool:
    """Whether every run of *job* presents the SAME session key.

    Lives beside :func:`build_cron_session_context` because it is the inverse of
    that function's branch, and a predicate that can silently disagree with the
    code that mints the key is worse than no predicate: it fails QUIET, as a
    warning that stops firing or one that fires on the wrong job.

    Two minting paths feed this, which is the whole reason callers must not infer
    the answer from the key's shape:

    * :func:`build_cron_session_context` -- ``cron:<job_id>`` when
      ``persistent_session``, else ``cron:<job_id>:<run_id>`` with a fresh
      ``uuid4`` per fire, so the three-segment form there is EPHEMERAL.
    * the sequential-agent path in the Slack gateway -- ``cron:<job_id>:<agent>``
      whenever ``agent_sequence`` holds more than one agent. It builds the key
      directly rather than calling the function above, and an agent NAME is
      stable, so the three-segment form there is DURABLE.

    So the two forms are indistinguishable by separator count, and only the job
    record separates them. The sequential path ignores ``persistent_session``
    entirely, which is why it is checked second rather than combined.
    """
    if agent_sequence_dispatches(job.agent_sequence):
        return True
    return job.persistent_session


def resolve_cron_memory(job: CronJob, *, validate_memory_files: bool = True) -> tuple[str, str]:
    """Dispatch the job's captured execution, never its current display alias."""
    from kiro_crew.execution_context import execution_from_record, validate_execution
    from kiro_crew.memory_stores import memory_store_version, require_memory_store

    if job.execution_context is not None:
        execution = execution_from_record({"execution_context": job.execution_context})
        if validate_memory_files:
            validate_execution(execution)
        return execution.store.legacy_name, execution.template_id
    if not isinstance(job.member_id, str) or not isinstance(job.memory_store, str):
        raise ValueError("memory_unavailable: malformed schedule identity")
    # A V2 schedule must carry the captured execution record: its member ID is
    # an immutable database identity and cannot be reconstructed from a name.
    # Older V1 schedules may still carry the historical member selector beside
    # their explicit legacy store; keep dispatching that store instead of
    # silently auto-pausing it after an upgrade.
    if memory_store_version(job.memory_store) == 2 or (job.member_id and not job.memory_store):
        raise ValueError("memory_unavailable: schedule has no canonical execution context")
    store = (
        require_memory_store(job.memory_store, require_directory=validate_memory_files)
        if job.memory_store
        else ""
    )
    return store, job.agent_id


def bind_cron_memory(job: CronJob) -> None:
    """Capture existing member or creator once inside the new job record."""
    from dataclasses import replace

    from kiro_crew.execution_context import (
        derive_execution,
        execution_for_store,
        read_session_execution,
    )

    if job.execution_context is not None:
        resolve_cron_memory(job, validate_memory_files=False)
        return
    creator = read_session_execution(job.session_key) if job.session_key else None
    execution = creator or execution_for_store(
        job.memory_store, template_id=job.agent_id or "kirocrew"
    )
    if job.member_id:
        execution = derive_execution(execution, target_member=job.member_id)
    if execution.memory_mode != "persistent":
        raise ValueError("Restricted sessions cannot create persistent schedules")
    if job.agent_id:
        execution = replace(execution, template_id=job.agent_id)
    job.execution_context = execution.to_record()
    job.member_id = execution.member_id or ""
    job.memory_store = execution.store.legacy_name
