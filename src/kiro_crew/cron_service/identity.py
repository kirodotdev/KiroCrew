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
        # A result composed inside a project may carry private content. Once
        # unbound, seeding the next prompt would stamp derived output unbound,
        # so only a real False carries; malformed or unknown is withheld.
        if job.last_result and (job.project_path or job.last_result_project_bound is False):
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
    that repairs it. The scheduler records it as an ordinary failed run.
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
    record once; dispatch never runs an uncaptured one. Attribution is the one
    shared by a chat with the same shape
    (``execution_context.attribute_legacy_member``, which
    ``_backfill_legacy_member_record`` also calls): the store's declared
    owner must be exactly one configured member, the schedule's ``member_id``
    must name that member by alias or by id, and the member must still resolve
    to this store. Nothing is guessed. The result is the member's own execution;
    :func:`migrate_legacy_member_schedules` then keeps whatever ``agent_id`` the
    schedule named as its template and brings the session record the old build
    left under ``cron:<id>`` into agreement.

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
    from kiro_crew.execution_context import (
        LegacyOwnerNotNamed,
        LegacyStoreHasNoOwner,
        LegacyStoreOwnerDeleted,
        attribute_legacy_member,
    )

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
    try:
        return attribute_legacy_member(config, job.memory_store, job.member_id)
    except LegacyStoreHasNoOwner as exc:
        raise LegacyScheduleRefused(
            "its memory store has no attributed owner",
            f"to repair it, {LEGACY_MEMBER_STORE_REMEDY}, then restart",
        ) from exc
    except LegacyStoreOwnerDeleted as exc:
        # The store outlives a deleted member on purpose (its id stays
        # reserved), so there is no member left to recreate the schedule from.
        raise LegacyScheduleRefused("its store's member was deleted", _DELETE_REMEDY) from exc
    except LegacyOwnerNotNamed as exc:
        raise LegacyScheduleRefused(
            "cannot be attributed to its store's owner "
            "(the schedule does not name the store's owner)",
            _RECREATE_REMEDY,
        ) from exc
    except ValueError as exc:
        # LegacyOwnerBoundElsewhere, UnknownMemoryStore and MemberSlugError are
        # all ValueErrors.
        raise LegacyScheduleRefused(
            f"cannot be attributed to its store's owner ({exc})", _RECREATE_REMEDY
        ) from exc


def migrate_legacy_member_schedules(store_dir: Path) -> list[str]:
    """Capture the execution of every pre-identity member schedule once; never raises.

    Run after the start-of-process store upgrade, before the scheduler arms, so
    every reader of the record -- dispatch, the dashboard's session registry, the
    crewmate pages, which compare ``member_id`` with the member's permanent id --
    sees an ordinary captured schedule rather than re-deriving one on each fire.
    A compare-and-set keyed on shape: under the store's own lock, only a record
    that still has no ``execution_context`` is rewritten, and only its
    ``execution_context`` and ``member_id`` change. A named ``agent_id`` is kept
    unchanged and captured as the execution's template, as :func:`bind_cron_memory`
    does for a schedule that names an agent today. Before capturing the schedule,
    the session record the old build left under its stable key is brought into
    agreement (:func:`_reconcile_legacy_cron_session`), since dispatch publishes
    the capture under that key and refuses a record that disagrees. If that step
    raises, the schedule stays uncaptured for the next start to retry. A
    schedule that cannot be attributed is left as it was and logged with its
    repair; dispatch refuses it, and any schedule still uncaptured, through
    :func:`resolve_cron_memory`. Returns the ids of the schedules it captured.
    """
    from dataclasses import replace

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
                agent_id = record.get("agent_id")
                sequence = record.get("agent_sequence")
                job = CronJob(
                    id=str(record.get("id", "")),
                    name=str(record.get("name", "")),
                    message="",
                    agent_id=agent_id if isinstance(agent_id, str) else "",
                    member_id=record.get("member_id"),  # type: ignore[arg-type]
                    memory_store=record.get("memory_store"),  # type: ignore[arg-type]
                    # The two fields that decide which session key dispatch mints,
                    # read as the store reads them.
                    persistent_session=bool(record.get("persistent_session", True)),
                    agent_sequence=(
                        [item for item in sequence if isinstance(item, str)]
                        if isinstance(sequence, list)
                        else []
                    ),
                )
                try:
                    execution = legacy_member_cron_execution(job, config=config)
                except LegacyScheduleRefused as exc:
                    logger.warning("Cron '%s' was left uncaptured: %s", job.name, exc)
                    continue
                if execution is None:
                    continue
                if job.agent_id:
                    execution = replace(execution, template_id=job.agent_id)
                # Reconcile first: a failed session write must leave the schedule
                # uncaptured so the next start retries. An already-reconciled
                # session is safe to retry if the schedule write later fails.
                try:
                    _reconcile_legacy_cron_session(job, execution)
                except Exception:
                    logger.warning(
                        "Cron '%s' was left uncaptured: its session record was not reconciled",
                        job.name,
                        exc_info=True,
                    )
                    continue
                record["execution_context"] = execution.to_record()
                record["member_id"] = execution.member_id or ""
                migrated.append(job.id)
            if migrated:
                atomic_write(path, json.dumps(data, indent=2))
        return migrated
    except Exception:
        logger.warning("pre-identity member schedules were not upgraded", exc_info=True)
        return []


def _reconcile_legacy_cron_session(job: CronJob, execution: ExecutionContext) -> None:
    """Make the session record the old build left under *job*'s key carry *execution*.

    0.7.0-insider.1 to .5 wrote a member schedule's session as
    ``{memory_store: <store>, agent: <member_id selector>}`` under ``cron:<id>``
    on its first fire, whatever ``agent_id`` the schedule named. The single-agent
    fire publishes the captured execution under that same key with
    ``bind_session_execution(key, execution)`` -- no ``replace_existing`` -- which
    refuses any record that decodes to something else. The first read of that
    record backfills it into the member's OWN template
    (``execution_context._backfill_legacy_member_record``), so a schedule that
    named an agent would be refused on every fire until it auto-paused; a record
    whose ``agent`` is not the member is never backfilled and is refused as
    identity-less instead. Either way the record is the schedule's own: the key
    is minted from the job id, and its store is the one the schedule was just
    attributed through. So the capture is written into it here, once.

    Two shapes, one identity. A record still in the legacy shape is rewritten
    by a compare-and-set against exactly that shape: no carrier, the schedule's
    store, persistent, no app, and an ``agent`` naming what the schedule itself
    names -- its ``member_id`` selector as the old build wrote it, the member's
    id, or the ``agent_id`` it kept. A record some earlier read already
    backfilled is rebound through :func:`bind_session_execution` with
    ``replace_existing`` and the decoded value as ``expected``, and only when it
    differs from the capture in its template alone -- same member, store, mode
    and app -- so the rewrite changes nothing else. Nothing here guesses a
    member: the identity written is the one
    :func:`legacy_member_cron_execution` attributed, and a record that names
    anyone else, a restricted mode or an app is left as it is and logged, since
    dispatch will refuse it with its own reason. Not vouched: a cron key is never
    the caller slot of an own-store admission.

    Only the stable single-agent key is reconciled. A per-run key is fresh on
    every fire, and the sequential path reads the record and replaces it itself.
    """
    from kiro_crew.execution_context import EXECUTION_CONTEXT_KEY
    from kiro_crew.history import ConversationLog

    if agent_sequence_dispatches(job.agent_sequence) or not job.persistent_session:
        return
    session_key, _ = build_cron_session_context(job)
    log = ConversationLog()
    metadata, readable = log.get_metadata_status(session_key)
    if not readable:
        raise OSError(f"cron session record {session_key} is unreadable")
    if not metadata:
        # The old build never fired it; the first fire writes the record afresh.
        return
    accepted_agents = {job.member_id, execution.member_id, job.agent_id} - {"", None}
    fields = {
        EXECUTION_CONTEXT_KEY: execution.to_record(),
        "memory_store": execution.store.legacy_name,
        "memory_mode": execution.memory_mode,
    }
    if log.update_metadata_if(
        session_key,
        fields,
        lambda meta: EXECUTION_CONTEXT_KEY not in meta
        and meta.get("memory_store") == job.memory_store
        and meta.get("agent") in accepted_agents
        and meta.get("memory_mode", "persistent") == "persistent"
        and not meta.get("app"),
        require_existing=True,
    ):
        logger.info("Cron '%s': its pre-identity session record now carries its capture", job.name)
        return
    rebind_cron_session_template(session_key, execution, job.name)


def rebind_cron_session_template(
    session_key: str, execution: ExecutionContext, job_name: str
) -> None:
    """Make the schedule's own session record name the template it now captures.

    Dispatch publishes the capture under the job's stable key with a plain
    ``bind_session_execution``, which refuses a record that differs. A record
    that differs from the capture in its template alone -- same member, store,
    mode, app and selection -- is this schedule's own, left by a run under the
    template the capture named before (a startup upgrade, or an agent change
    through ``cron update``). It is rewritten under a compare-and-set against
    the value just read. Any other record is left as it is and logged, and
    dispatch refuses it with its own reason. Not vouched: a cron key is never
    the caller slot of an own-store admission.
    """
    from dataclasses import replace

    from kiro_crew.execution_context import bind_session_execution, read_session_execution
    from kiro_crew.memory_stores import MissingExecutionIdentity

    try:
        current = read_session_execution(session_key)
    except MissingExecutionIdentity:
        logger.warning(
            "Cron '%s': its session record cannot be attributed to the schedule and was "
            "left as it is; archive that session or recreate the schedule",
            job_name,
        )
        return
    if current is None or current == execution:
        return
    if replace(current, template_id=execution.template_id) != execution:
        logger.warning(
            "Cron '%s': its session record belongs to another execution and was left as it "
            "is; archive that session or recreate the schedule",
            job_name,
        )
        return
    bind_session_execution(
        session_key, execution, replace_existing=True, expected=current, vouch=False
    )
    logger.info("Cron '%s': its session record now carries the agent it names", job_name)


def recapture_cron_template(job: CronJob, agent_id: str) -> dict[str, Any] | None:
    """*job*'s captured execution re-pointed at a newly chosen *agent_id*.

    Dispatch runs the captured ``execution_context.template_id``
    (:func:`resolve_cron_memory`), not ``agent_id``, so an update that changes
    the agent must move the capture with it or the job keeps running the agent
    it was created with. The template is chosen as :func:`bind_cron_memory`
    chooses it at creation: the named agent, else the member's own template for
    a member schedule, else the ``kirocrew`` floor that dispatch maps to the
    configured default. Store, member, mode and app are left as captured.

    Returns ``None`` when nothing moves: no capture yet (an uncaptured record
    dispatches from ``agent_id`` already), an ``agent_id`` equal to the stored
    one (the job form resubmits the field on every edit, and a schedule made
    from a template chat names its template only in the capture), or a template
    the capture already names.
    """
    from dataclasses import replace

    from kiro_crew.execution_context import derive_execution, execution_from_record

    if job.execution_context is None or agent_id == job.agent_id:
        return None
    execution = execution_from_record({"execution_context": job.execution_context})
    if agent_id:
        template_id = agent_id
    elif execution.member_id is not None or execution.selection_kind == "member":
        member = job.member_id or execution.member_id or execution.selection_name
        template_id = derive_execution(execution, target_member=member).template_id
    else:
        template_id = "kirocrew"
    if template_id == execution.template_id:
        return None
    return replace(execution, template_id=template_id).to_record()


def _is_own_cron_execution(record: ExecutionContext, job_execution: ExecutionContext) -> bool:
    """Whether *record* is an identity this job's own dispatch could have published.

    A project-bound single-agent fire publishes one of two shapes under its
    stable key: the job's captured execution (possibly under another template),
    or the project-override projection of it -- an unowned template on Global
    memory (:func:`kiro_crew.execution_context.execution_for_project_override`).
    The template half is exactly what a binding change moves, so it is not
    compared. The member, store and app are: a record naming anyone else's is
    not this job's to overwrite, and stays refused by the plain bind.
    """
    if record.app != job_execution.app:
        return False
    if record.member_id == job_execution.member_id and record.store == job_execution.store:
        return True
    return record.member_id is None and record.store.store_id == "default"


def rebind_own_cron_execution(
    session_key: str,
    job_execution: ExecutionContext,
    dispatch_execution: ExecutionContext,
) -> bool:
    """Replace a stale durable identity this job itself published, before dispatch.

    The single-agent fire binds its dispatch identity with a plain
    :func:`bind_session_execution`, which refuses any record that differs. A
    project-bound job's dispatch identity is not constant: when the checkout
    starts (or stops) declaring the dispatched template, the store moves between
    the captured one and Global, and a binding change can move the template.
    ``_cron_session_binding_requires_reset`` resets the live session for that,
    but nothing rewrote the durable record, so every later fire raised "session
    already belongs to another execution" before acquisition and the job failed
    on every tick until it auto-paused. The sequence path already replaces under
    a compare-and-set against a fresh read; this is the same replace for the
    single-agent key, restricted to records :func:`_is_own_cron_execution`
    recognises. Not conditioned on "the binding changed on THIS fire": a fire
    that reset and then failed to acquire leaves the binding retained and the
    record stale, and the next fire must still repair it.

    Returns whether a replace happened. A record this job did not publish, an
    absent record, and one that already matches are all left alone.
    """
    from dataclasses import replace

    from kiro_crew.execution_context import bind_session_execution, read_session_execution
    from kiro_crew.memory_stores import MissingExecutionIdentity

    try:
        current = read_session_execution(session_key)
    except MissingExecutionIdentity:
        return False
    if current is None:
        return False
    if replace(current, memory_mode=dispatch_execution.memory_mode) == dispatch_execution:
        return False
    if not _is_own_cron_execution(current, job_execution):
        return False
    bind_session_execution(
        session_key, dispatch_execution, replace_existing=True, expected=current, vouch=False
    )
    return True


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
