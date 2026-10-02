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

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.cron_service.model import CronJob
    from kiro_crew.execution_context import ExecutionContext

logger = logging.getLogger("kiro_crew.cron")


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
    if legacy_member_cron_execution(job) is not None:
        # Attributable, but only the start-of-process capture may bind it: a
        # run derived here would carry no record the dashboard's session
        # registry, or the next fire after a template change, could agree on.
        raise LegacyScheduleRefused(
            "its member execution has not been captured yet",
            "restart the gateway, which captures it",
        )
    # Any other V2 schedule must carry the captured execution record: its member
    # ID is an immutable database identity and cannot be reconstructed from a
    # name. Older V1 schedules may still carry the historical member selector
    # beside their explicit legacy store; keep dispatching that store instead of
    # silently auto-pausing it after an upgrade.
    if memory_store_version(job.memory_store) == 2 or (job.member_id and not job.memory_store):
        raise ValueError("memory_unavailable: schedule has no canonical execution context")
    store = (
        require_memory_store(job.memory_store, require_directory=validate_memory_files)
        if job.memory_store
        else ""
    )
    return store, job.agent_id


class LegacyScheduleRefused(ValueError):
    """A pre-identity member schedule that dispatch must not run as it stands.

    Raised by :func:`legacy_member_cron_execution` when the schedule cannot be
    attributed, and by :func:`resolve_cron_memory` when it could be but was never
    captured; always before anything is dispatched, and always carrying the step
    that repairs it. The scheduler treats it as a run this state prevented, not
    as a failed run.
    """

    def __init__(self, reason: str, remedy: str) -> None:
        super().__init__(
            f"memory_unavailable: this schedule predates member identities and {reason}; {remedy}"
        )


_RECREATE_REMEDY = "recreate it from the member's chat"
_DELETE_REMEDY = "delete this schedule"


def _is_legacy_member_schedule(job: CronJob) -> bool:
    """The shape 0.7.0-insider.1 to .5 stored: a member alias and a store, nothing captured."""
    return (
        job.execution_context is None
        and isinstance(job.member_id, str)
        and isinstance(job.memory_store, str)
        and bool(job.member_id)
        and bool(job.memory_store)
    )


def legacy_member_cron_execution(job: CronJob, *, config: Any = None) -> ExecutionContext | None:
    """The member a V2 schedule with no captured execution runs as, or a refusal.

    0.7.0-insider.1 to .5 stored a member schedule as ``{member_id: <alias>,
    memory_store}`` with no ``execution_context``. The start-of-process store
    upgrade gives that store its ``owner_member_id``, and
    :func:`migrate_legacy_member_schedules` then captures this result into the
    record once; dispatch never runs an uncaptured one. Attribution mirrors how
    a chat with the same shape is backfilled
    (``execution_context._backfill_legacy_member_record``): the store's declared
    owner must be exactly one configured member, the schedule's ``member_id``
    must name that member by alias or by id, and the member must still resolve
    to this store. Nothing is guessed. The result is the member's own execution,
    whatever ``agent_id`` the schedule named: the session record the old build
    left under ``cron:<id>`` already carries the member's own template.

    ``None`` for any other shape, including a member schedule on a V1 store,
    which keeps its V1 dispatch. Every refusal is a
    :class:`LegacyScheduleRefused` naming its repair.
    """
    from kiro_crew.memory_stores import (
        DEFAULT_MEMORY_STORE,
        LEGACY_MEMBER_STORE_REMEDY,
        validate_memory_store_name,
    )

    # The default store is V1 by definition, as memory_store_version answers.
    if not _is_legacy_member_schedule(job) or job.memory_store == DEFAULT_MEMORY_STORE:
        return None
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import member_config_for_id, resolve_member_execution

    config = config if config is not None else KiroCrewConfig.load()
    try:
        record = config.memory_stores.get(validate_memory_store_name(job.memory_store))
    except ValueError as exc:
        raise LegacyScheduleRefused(f"its memory store is invalid ({exc})", _DELETE_REMEDY) from exc
    version = getattr(record, "memory_version", None)
    if record is None or type(version) is not int or version not in (1, 2):
        raise LegacyScheduleRefused(
            "its memory store declaration is unavailable",
            # Capture runs only at start, so the restored entry needs a restart
            # before this schedule can run.
            f"restore the store's entry in config.json, then restart the gateway, "
            f"or {_DELETE_REMEDY}",
        )
    if version == 1:
        return None
    owner_id = getattr(record, "owner_member_id", "")
    if not isinstance(owner_id, str) or not owner_id:
        raise LegacyScheduleRefused(
            "its memory store has no attributed owner",
            f"to repair it, {LEGACY_MEMBER_STORE_REMEDY}, then restart",
        )
    if not any(getattr(member, "member_id", "") == owner_id for member in config.agents.values()):
        # The store outlives a deleted member on purpose (its id stays
        # reserved), so there is no member left to recreate the schedule from.
        raise LegacyScheduleRefused("its store's member was deleted", _DELETE_REMEDY)
    try:
        alias, _ = member_config_for_id(config, owner_id)
        if job.member_id not in (alias, owner_id):
            raise ValueError("the schedule does not name the store's owner")
        execution = resolve_member_execution(config, alias)
        if execution.store.store_id != job.memory_store:
            raise ValueError("the member is bound to another store")
    except ValueError as exc:
        # UnknownMemoryStore and MemberSlugError are both ValueErrors.
        raise LegacyScheduleRefused(
            f"cannot be attributed to its store's owner ({exc})", _RECREATE_REMEDY
        ) from exc
    return execution


def migrate_legacy_member_schedules(store_dir: Path) -> list[str]:
    """Capture the execution of every pre-identity member schedule once; never raises.

    Run after the start-of-process store upgrade, before the scheduler arms, so
    every reader of the record -- dispatch, the dashboard's session registry, the
    crewmate pages, which compare ``member_id`` with the member's permanent id --
    sees an ordinary captured schedule rather than re-deriving one on each fire.
    A compare-and-set keyed on shape: under the store's own lock, only a record
    that still has no ``execution_context`` is rewritten, and only its
    ``execution_context``, ``member_id`` and a named ``agent_id`` change: the
    agent becomes the captured template, as it is for a schedule a member names
    an agent in today, so the readers that list ``agent_id`` name the template the
    job runs. A schedule that cannot be
    attributed is left as it was and logged with its repair; dispatch refuses
    it, and any schedule still uncaptured, through :func:`resolve_cron_memory`.
    Returns the ids of the schedules it captured.
    """
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.cron_service.model import CronJob
    from kiro_crew.cron_service.store import _CRONS_FILE, cron_store_lock

    path = store_dir / _CRONS_FILE
    try:
        if not path.is_file():
            return []
        config = KiroCrewConfig.load()
        with cron_store_lock(store_dir):
            data = json.loads(path.read_bytes())
            records = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(records, list):
                return []
            migrated: list[str] = []
            for record in records:
                # The old build wrote no execution_context key at all.
                if not isinstance(record, dict) or record.get("execution_context") is not None:
                    continue
                job = CronJob(
                    id=str(record.get("id", "")),
                    name=str(record.get("name", "")),
                    message="",
                    member_id=record.get("member_id"),  # type: ignore[arg-type]
                    memory_store=record.get("memory_store"),  # type: ignore[arg-type]
                )
                try:
                    execution = legacy_member_cron_execution(job, config=config)
                except LegacyScheduleRefused as exc:
                    logger.warning("Cron '%s' was left uncaptured: %s", job.name, exc)
                    continue
                if execution is None:
                    continue
                agent_id = record.get("agent_id")
                if isinstance(agent_id, str) and agent_id:
                    if agent_id != execution.template_id:
                        logger.warning(
                            "Cron '%s' named agent %r; it runs as its member's own template %r",
                            job.name,
                            agent_id,
                            execution.template_id,
                        )
                    record["agent_id"] = execution.template_id
                record["execution_context"] = execution.to_record()
                record["member_id"] = execution.member_id or ""
                migrated.append(job.id)
            if migrated:
                atomic_write(path, json.dumps(data, indent=2))
        return migrated
    except Exception:
        logger.warning("pre-identity member schedules were not upgraded", exc_info=True)
        return []


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
