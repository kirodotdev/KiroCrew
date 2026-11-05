"""The cron store on disk: ``crons.json``'s format, its lock, and what a record may hold.

One owner for the three things every writer and reader of the store must agree
on: the byte format (:func:`encode_store` writes, :func:`decode_jobs` and
:func:`_job_from_record` read, :func:`store_digest` fingerprints), the
cross-process advisory lock (:func:`cron_store_lock`), and the failure types a
store operation raises (:class:`CronStoreBusy`, :class:`CronStoreUnreadable`,
:class:`CronPendingMismatch`).

The service keeps the store's live state -- the loaded job list, the
fingerprint of the bytes it last loaded, the unreadable-store latch -- and runs
every read-modify-write through these; the serviceless readers in
:mod:`kiro_crew.cron_service.readers` share :func:`_read_job_records`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.cron_service.model import (
    _ALERT_AFTER_DEFAULT,
    _AUTO_PAUSE_THRESHOLD,
    CronJob,
    CronSchedule,
)
from kiro_crew.cron_service.zone_default_migration import (
    LOCAL_DEFAULT_STORE_VERSION,
    predates_local_default,
)

logger = logging.getLogger("kiro_crew.cron")

_CRONS_FILE = "crons.json"


#: 3: an empty job zone under an empty config zone is the host's zone, not UTC
#: (``zone_default_migration`` keeps an older store's jobs on UTC).
_STORE_VERSION = LOCAL_DEFAULT_STORE_VERSION

#: The top-level keys of the store document this build writes itself. Any other
#: top-level key came from a newer build and is written back unchanged.
_STORE_DOCUMENT_KEYS = frozenset({"version", "jobs"})

# Bounded non-blocking acquire for the cron-store advisory lock (see
# CronService._file_lock). The spin never parks the event loop in an
# uninterruptible kernel wait; it fails fast after the timeout instead.
_FILE_LOCK_TIMEOUT_SECS = 10.0  # max wall-time to wait for the store lock
_FILE_LOCK_POLL_SECS = 0.02  # sleep between non-blocking acquire attempts


class CronStoreUnreadable(ValueError):
    """A mutation could not be persisted because the last load failed.

    Derives from ``ValueError`` so per-item handlers catch it: the onboarding
    importer's apply loop catches ``(OSError, ValueError, TypeError,
    sqlite3.Error)``, so one corrupt ``crons.json`` rejects one item, not the
    whole plan. Sites that catch both classes list this one BEFORE ``ValueError``.

    Raised by :meth:`CronService._save` when ``_load`` could not read
    ``crons.json``: the empty in-memory list would overwrite records still on
    disk, so the write is refused and the caller sees the failure. Background
    writers (reaper merge, result merge, deferred-removal drain) catch it so a
    corrupt store cannot take down the scheduler loop.

    Also raised by :func:`dispatched_agents_from_disk` when the store is PRESENT
    but nothing loads from it, so a reader that must fail CLOSED (the template
    delete guard) does not mistake an unreadable store for an empty one.
    """


class CronPendingMismatch(RuntimeError):
    """A compare-and-swap precondition on the job failed: the field the caller
    decided on changed after the caller read it.

    Raised inside the locked update when an ``expect_*`` precondition
    (``expect_secret_env_pending``, ``expect_secret_env``, or the app bridge's
    ``expect_app_manifest_digest``) does not match the freshly reloaded record
    — what keeps an approval or denial from acting on a request the decider
    never saw (the agent can replace a pending request at any moment), and a
    manifest re-sync from writing fields computed against a job another
    registrar has since moved on. Callers surface it as HTTP 409
    ``stale_request``, or recompute from the reloaded record.
    """


class CronStoreBusy(TimeoutError):
    """Raised when a cron-store mutator cannot acquire the store lock in time.

    The store mutators (:meth:`add_job`, :meth:`update_job`, :meth:`remove_job`,
    :meth:`enable_job`, :meth:`ack_job`, :meth:`unack_job` and their ``*_async``
    variants) raise this under lock contention instead of blocking forever.
    Subclasses :class:`TimeoutError` so ``except TimeoutError`` guards (reaper
    sweep, timer tick, read-path degrade) still catch it. Public boundaries turn
    it into a retryable error (HTTP 409, an MCP ``Error:`` string, a Slack "store
    busy" reply): contention is transient, so callers retry.
    """


@contextmanager
def cron_store_lock(
    store_dir: Path, *, timeout: float = _FILE_LOCK_TIMEOUT_SECS, poll: float = _FILE_LOCK_POLL_SECS
) -> Iterator[None]:
    """The cron store's cross-process advisory lock, for a caller with no service.

    :meth:`CronService._file_lock` delegates here. The Agent templates delete
    guard holds it across its :func:`dispatched_agents_from_disk` check and the
    file rename, so no schedule can be written between "nothing dispatches this
    template" and the template going.

    Off the event loop ONLY: the spin sleeps. Raises :class:`CronStoreBusy` after
    *timeout*. The lock file is opened without truncating so a contending opener
    on Windows does not crash at open() before the spin starts.
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    lock = store_dir / ".crons.lock"
    deadline = time.monotonic() + timeout
    with platform_compat.open_lock_file(lock) as lock_fd:
        while not platform_compat.try_acquire_lock(lock_fd, exclusive=True):
            if time.monotonic() >= deadline:
                raise CronStoreBusy(f"Could not acquire cron store lock within {timeout:g}s")
            time.sleep(poll)
        try:
            yield
        finally:
            platform_compat.release_lock(lock_fd)


def _is_representable_number(value: Any) -> bool:
    """True when *value* is a real number every consumer can hold.

    Rejects ``bool``, non-numbers, ``NaN``/``Infinity`` (``json.loads`` accepts
    them; they break ordering and emit invalid JSON), and ints too large for a
    float. No sign bound: load checks shape, not value. Raises nothing, so nothing
    escapes ``_load``'s ``(KeyError, TypeError)`` per-entry isolation.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _record_user_paused(j: dict[str, Any]) -> bool:
    """Single owner for the user-pause predicate of a serialized job.

    The ``!enabled`` fallback covers stores written before ``user_paused``
    existed. Every reader routes through here so the derivation has one owner.
    """
    return bool(j.get("user_paused", not j.get("enabled", True)))


def _record_is_enabled(j: dict[str, Any]) -> bool:
    """Single owner for the effective-enabled predicate of a serialized job.

    Enabled = neither user-paused nor auto-paused. ``_load`` and
    ``count_enabled_from_disk`` MUST both route through here so the two readers
    cannot drift.
    """
    return not _record_user_paused(j) and not j.get("auto_paused", False)


def _ack_ids_field(value: Any) -> list[str]:
    """``acked_item_ids`` as stored: a list of strings, else no ids at all."""
    if isinstance(value, list) and all(isinstance(i, str) for i in value):
        return value
    return []


def _job_from_record(j: dict[str, Any], *, warn_on_coercion: bool = True) -> CronJob:
    """Build one :class:`CronJob` from its serialized record.

    Raises ``KeyError``/``TypeError`` when the record is malformed (missing
    required keys, not shaped like a job object at all, or carrying a
    non-string where the field decides WHAT the job executes). The caller
    (:meth:`CronService._load`) isolates that failure to THIS entry — one bad
    record must never discard the rest of the store.

    Field-type handling splits by what a wrong type would cost:

    - **Required identity/payload** (``id``, ``name``, ``message``,
      ``schedule.kind``) and **execution selectors** (``script``, ``command``,
      ``agent_id``, the ``agent_sequence`` / ``skip_dates`` lists) raise
      ``TypeError``, skipping the record. Coercing a selector would fail OPEN:
      a ``script`` degraded to ``""`` turns the job into an LLM agent job.
    - **Everything else string-typed** coerces to its unset value (``""``, or
      ``None`` for ``Optional[str]``), and the coerced fields are named in one
      WARNING: ``_save`` rewrites ``jobs[]`` wholesale, so the log line is the
      only record of the lost value.

    No ``AttributeError`` is possible for ``json.loads`` output (every ``.get()``
    follows a ``[...]`` subscript, which only a ``dict`` survives), so one here
    is a code defect. ``_load`` lets it propagate: treating it as a malformed
    record would erase a valid job from disk on the next save.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    coerced: list[str] = []

    def _required_str(container: dict[str, Any], field: str) -> str:
        value = container[field]  # KeyError for a missing required key
        if not isinstance(value, str):
            raise TypeError(
                f"cron record field {field!r} must be a string, got {type(value).__name__}"
            )
        return value

    def _selector_str(field: str) -> str:
        # Absent = legacy record, "". A present non-string (null included) is
        # malformed: a selector has no safe fallback.
        if field not in j:
            return ""
        value = j[field]
        if not isinstance(value, str):
            raise TypeError(
                f"cron record execution selector {field!r} must be a string, got {type(value).__name__}"
            )
        return value

    def _str_list(field: str) -> list[str]:
        # Absent = legacy record, empty. Explicit null skips the record: read
        # as empty it would flip a multi-agent job onto the single-agent path.
        if field not in j:
            return []
        value = j[field]
        if not isinstance(value, list) or any(not isinstance(m, str) for m in value):
            raise TypeError(f"cron record field {field!r} must be a list of strings")
        return value

    def _guard_str(field: str) -> str:
        # Non-string (incl. null) degrades to "" -- the field's unset value --
        # so it can never reach a consumer that calls string methods on it
        # (the GET /api/crons redacting serializer would 500 the listing).
        value = j.get(field)
        if value is not None and not isinstance(value, str):
            coerced.append(field)
        return value if isinstance(value, str) else ""

    def _guard_opt_str(field: str) -> str | None:
        # None ("never set") differs from "" (last_status gates on `is None`),
        # so a non-string degrades to None.
        value = j.get(field)
        if value is not None and not isinstance(value, str):
            coerced.append(field)
        return value if isinstance(value, str) else None

    def _guard_num(field: str, default: Any) -> Any:
        # Telemetry numerics coerce to their default (never skip), joining the
        # string fields' WARNING. This keeps NaN/bignum out of the JSON output,
        # timer arithmetic and the secret-grant CAS (NaN != NaN blocks approval).
        # Absent = legacy, silent default. Null passes only where the default
        # is None (last_run_ts: "never ran"); elsewhere it is coerced and named.
        if field not in j:
            return default
        value = j[field]
        if value is None:
            if default is None:
                return default
            coerced.append(field)
            return default
        if not _is_representable_number(value):
            coerced.append(field)
            return default
        return value

    # Schedule sub-fields decide WHEN the job fires, so like the selectors a
    # mistyped value skips the record. The kind subscript runs first, so the
    # .get() calls below cannot raise AttributeError.
    sched = j["schedule"]
    sched_kind = _required_str(sched, "kind")
    for _numeric_field in ("every_secs", "at_ts"):
        _numeric_value = sched.get(_numeric_field)
        if _numeric_value is not None and not _is_representable_number(_numeric_value):
            # A NaN at_ts is due every tick and a bignum makes `at_ts - now`
            # raise OverflowError at gateway startup. _build_job and
            # _update_job_locked refuse both, so this skip drops no record a
            # writer can produce. No value bound: extreme finite values load.
            raise TypeError(
                f"cron record schedule field {_numeric_field!r} must be a representable finite number"
            )
    _cron_expr = sched.get("cron_expr")
    if _cron_expr is not None and not isinstance(_cron_expr, str):
        raise TypeError("cron record schedule field 'cron_expr' must be a string")

    job = CronJob(
        id=_required_str(j, "id"),
        name=_required_str(j, "name"),
        message=_required_str(j, "message"),
        schedule=CronSchedule(
            kind=sched_kind,
            every_secs=sched.get("every_secs"),
            at_ts=sched.get("at_ts"),
            cron_expr=_cron_expr,
        ),
        channel=_guard_opt_str("channel"),
        thread_ts=_guard_opt_str("thread_ts"),
        # Derived from user_paused/auto_paused, not the stored `enabled`, so
        # an auto-pause survives a restart (a recurring job's `enabled` is
        # not persisted on failure).
        enabled=seams._record_is_enabled(j),
        user_paused=_record_user_paused(j),
        auto_paused=j.get("auto_paused", False),
        last_run_ts=_guard_num("last_run_ts", None),
        last_status=_guard_opt_str("last_status"),
        last_error=_guard_opt_str("last_error"),
        created_ts=_guard_num("created_ts", 0.0),
        delete_after_run=j.get("delete_after_run", False),
        last_result=_guard_opt_str("last_result"),
        last_result_ts=_guard_num("last_result_ts", 0.0),
        last_result_stamp=_guard_str("last_result_stamp"),
        context_enabled=j.get("context_enabled", False),
        agent_id=_selector_str("agent_id"),
        # Memory-identity selectors (WHOSE memory the job runs with): like the
        # execution selectors, a non-string skips the record. Coercing to ""
        # would run the job unbound.
        member_id=_selector_str("member_id"),
        memory_store=_selector_str("memory_store"),
        execution_context=j.get("execution_context"),
        approval_mode=_guard_str("approval_mode"),
        acked_items=j.get("acked_items", []),
        # Anything but a list of strings (null from an imported archive, a hand
        # edit) loads as no ids, so the job still saves and no id names an entry.
        acked_item_ids=_ack_ids_field(j.get("acked_item_ids")),
        acked_item_ids_digest=_guard_str("acked_item_ids_digest"),
        created_by=_guard_str("created_by"),
        source_preset=_guard_str("source_preset"),
        source_template_prompt=_guard_str("source_template_prompt"),
        managed_by=_guard_str("managed_by"),
        app_manifest_digest=_guard_str("app_manifest_digest"),
        silent=j.get("silent", False),
        session_key=_guard_str("session_key"),
        last_posted_hash=_guard_str("last_posted_hash"),
        consecutive_dupes=_guard_num("consecutive_dupes", 0),
        last_posted_at=_guard_num("last_posted_at", 0.0),
        last_failure_hash=_guard_str("last_failure_hash"),
        last_failure_at=_guard_num("last_failure_at", 0.0),
        consecutive_failures=_guard_num("consecutive_failures", 0),
        skip_dates=_str_list("skip_dates"),
        timezone=_guard_str("timezone"),
        persistent_session=j.get("persistent_session", True),
        minimal_context=j.get("minimal_context", False),
        hide_in_chat=j.get("hide_in_chat", False),
        folder_id=_guard_str("folder_id"),
        chat_folder_id=_guard_str("chat_folder_id"),
        project_path=_guard_str("project_path"),
        model=_guard_str("model"),
        last_retry_count=_guard_num("last_retry_count", 0),
        last_retry_run_ts=_guard_num("last_retry_run_ts", 0.0),
        run_generation=_guard_num("run_generation", 0),
        user_pause_generation=_guard_num("user_pause_generation", 0),
        agent_sequence=_str_list("agent_sequence"),
        env=j.get("env", {}),
        timeout_secs=_guard_num("timeout_secs", seams._JOB_TIMEOUT_SECS),
        # Absent on every record written before the field existed: those jobs
        # keep the default limit. A malformed value is left for
        # CronJob.auto_pause_limit() to read back as the default.
        auto_pause_after_failures=_guard_num("auto_pause_after_failures", _AUTO_PAUSE_THRESHOLD),
        alert_after_failures=_guard_num("alert_after_failures", _ALERT_AFTER_DEFAULT),
        strict_schedule=j.get("strict_schedule", False),
        script=_selector_str("script"),
        command=_selector_str("command"),
        timeout=_guard_num("timeout", 0),
        secret_env=j.get("secret_env", {}),
        secret_env_pin=_guard_str("secret_env_pin"),
        secret_env_pending=j.get("secret_env_pending", {}),
        secret_env_pending_pin=_guard_str("secret_env_pending_pin"),
        secret_env_pending_ts=_guard_num("secret_env_pending_ts", 0.0),
    )
    if coerced and warn_on_coercion:
        logger.warning(
            "Coercing non-string value(s) in cron job entry (id=%r) field(s) %s to unset; "
            "the stored values will be replaced on the next write",
            job.id,
            ", ".join(coerced),
        )
    return job


def _is_loadable_record(j: dict[str, Any]) -> bool:
    """True when the SCHEDULER could build a job from *j*. NEVER raises.

    Asks :func:`_job_from_record` itself; ``isinstance(j, dict)`` is too weak
    (``{}`` is a dict the loader rejects).
    """
    try:
        # No WARNING: this probe runs on every status push and never precedes
        # a rewrite, so the coercion warning belongs to _load alone.
        _job_from_record(j, warn_on_coercion=False)
    except Exception:
        return False
    return True


def _read_job_records(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Read *path*, returning ``(records, loadable)``. NEVER raises.

    ``loadable`` is False only when the store is PRESENT but the scheduler can
    build nothing from it, so a diagnostic can tell "no crons" from "crons
    stopped loading". Runtime readers take ``[0]``.

    Shared read-parse-shape prologue for the readers that work with no running
    gateway (:func:`referenced_skill_names`, :func:`unhealthy_jobs_from_disk`,
    :meth:`CronService.count_enabled_from_disk`), so they survive the same
    corruption. Every failure mode collapses to "no records":

    * ``OSError`` — no file at all (every fresh install), permissions, or a
      directory where the file should be.
    * ``UnicodeError`` — invalid UTF-8 on disk. The explicit encoding matches
      :func:`~kiro_crew.atomic_write.atomic_write`, not the caller's locale.
    * ``ValueError`` / ``TypeError`` — unparseable JSON.
    * ``RecursionError`` — deeply nested JSON; it is a ``RuntimeError``, not a
      ``ValueError``, so a decode-error tuple would miss it.
    * Shape — not an object holding a ``jobs`` list (``[]``, a scalar,
      ``{"jobs": null}``).

    Non-dict entries are dropped here so no caller repeats the check.

    Not used by readers that owe a louder reaction: :meth:`CronService._load`
    (logs the corruption and resets the store fingerprint),
    :func:`~kiro_crew.portability._sanitize_imported_crons` and
    :func:`~kiro_crew.snapshot._merge_crons` (both report the file to the user).
    The latter two guard only ``(OSError, ValueError)``, so a deeply nested
    store still aborts an import or snapshot merge.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        # An ABSENT store is the fresh-install case: nothing to load is not a
        # fault. A path that is PRESENT but unusable — a directory, a broken
        # symlink, unreadable bytes — is the opposite, since the scheduler
        # loads nothing from it either and only that is worth reporting.
        # ``is_file()`` cannot draw this line: it is False for a directory and
        # for a broken symlink just as it is for a missing file. Use the
        # ``exists() or is_symlink()`` form ``cli_doctor`` already uses for the
        # same "present but not a usable file" distinction.
        try:
            present = path.exists() or path.is_symlink()
        except OSError:
            present = True
        return ([], not present)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return ([], False)
    records = data.get("jobs", []) if isinstance(data, dict) else None
    if not isinstance(records, list):
        return ([], False)
    kept = [j for j in records if isinstance(j, dict)]
    # Entries were present but NONE of them is loadable: the shape parsed, yet
    # nothing the scheduler can run came out of it. That is a read failure for
    # the diagnostic's purposes even though json.loads succeeded — distinct
    # from an honestly empty `{"jobs": []}`, which yields nothing because there
    # is nothing. Ask the LOADER, not `isinstance(dict)`: `{}` is a dict it
    # rejects, so `{"jobs":[{}]}` would otherwise report healthy while the
    # scheduler loads zero jobs. `kept` is returned UNCHANGED either way, so
    # partial salvage still reaches the runtime readers.
    return (kept, (not records) or any(_is_loadable_record(j) for j in kept))


def store_digest(raw: bytes) -> bytes:
    """The content fingerprint of the store bytes ``raw``: what ``_sync`` compares.

    Taken from the exact bytes parsed or written, so a reload decision compares
    like for like. A digest rather than ``(mtime_ns, size)`` because an external
    write can keep both while changing content (an equal-length rename).
    """
    return hashlib.blake2b(raw, digest_size=16).digest()


def job_record(j: CronJob) -> dict[str, Any]:
    """One job's ``crons.json`` entry. The key order IS the stored byte order."""
    record: dict[str, Any] = {
        "id": j.id,
        "name": j.name,
        "message": j.message,
        "schedule": asdict(j.schedule),
        "channel": j.channel,
        "thread_ts": j.thread_ts,
        "enabled": j.enabled,
        "user_paused": j.user_paused,
        "auto_paused": j.auto_paused,
        "last_run_ts": j.last_run_ts,
        "last_status": j.last_status,
        "last_error": j.last_error,
        "created_ts": j.created_ts,
        "delete_after_run": j.delete_after_run,
        "last_result": j.last_result,
        "last_result_ts": j.last_result_ts,
        "last_result_stamp": j.last_result_stamp,
        "context_enabled": j.context_enabled,
        "agent_id": j.agent_id,
        "member_id": j.member_id,
        "memory_store": j.memory_store,
        "execution_context": j.execution_context,
        "approval_mode": j.approval_mode,
        "acked_items": j.acked_items,
        "created_by": j.created_by,
        "source_preset": j.source_preset,
        "source_template_prompt": j.source_template_prompt,
        "silent": j.silent,
        "session_key": j.session_key,
        "last_posted_hash": j.last_posted_hash,
        "consecutive_dupes": j.consecutive_dupes,
        "last_posted_at": j.last_posted_at,
        "last_failure_hash": j.last_failure_hash,
        "last_failure_at": j.last_failure_at,
        "consecutive_failures": j.consecutive_failures,
        "skip_dates": j.skip_dates,
        "timezone": j.timezone,
        "persistent_session": j.persistent_session,
        "minimal_context": j.minimal_context,
        "hide_in_chat": j.hide_in_chat,
        "folder_id": j.folder_id,
        "chat_folder_id": j.chat_folder_id,
        "model": j.model,
        "last_retry_count": j.last_retry_count,
        "last_retry_run_ts": j.last_retry_run_ts,
        "run_generation": j.run_generation,
        "agent_sequence": j.agent_sequence,
        "env": j.env,
        "timeout_secs": j.timeout_secs,
        "auto_pause_after_failures": j.auto_pause_after_failures,
        "alert_after_failures": j.alert_after_failures,
        "strict_schedule": j.strict_schedule,
        "script": j.script,
        "command": j.command,
        "timeout": j.timeout,
        "secret_env": j.secret_env,
        "secret_env_pin": j.secret_env_pin,
        "secret_env_pending": j.secret_env_pending,
        "secret_env_pending_pin": j.secret_env_pending_pin,
        "secret_env_pending_ts": j.secret_env_pending_ts,
    }
    # Written only when it differs from the default, so a store holding no
    # custom limit saves to the same bytes it did before the field existed and
    # an older build reading it back sees nothing new.
    if j.auto_pause_after_failures == _AUTO_PAUSE_THRESHOLD:
        del record["auto_pause_after_failures"]
    if j.alert_after_failures == _ALERT_AFTER_DEFAULT:
        del record["alert_after_failures"]
    # Written only when set, and last: a store with no ordered user pause,
    # installer-managed job, or app-registered job stays byte-identical to one
    # an older build writes.
    if j.user_pause_generation:
        record["user_pause_generation"] = j.user_pause_generation
    # The optional ownership keys follow the same compatibility rule. An older
    # build reads a newer store's extra key and writes it back.
    if j.managed_by:
        record["managed_by"] = j.managed_by
    if j.app_manifest_digest:
        record["app_manifest_digest"] = j.app_manifest_digest
    # The per-entry ack ids follow the same rule: a job whose acks carry no id
    # (every ack before this key, and every Slack ack) writes nothing new.
    if any(j.acked_item_ids):
        record["acked_item_ids"] = j.acked_item_ids
        record["acked_item_ids_digest"] = j.acked_item_ids_digest
    # A job with no project directory saves to the bytes an older build wrote.
    if j.project_path:
        record["project_path"] = j.project_path
    return record


#: Every key :func:`job_record` can write, read off the encoder itself so the two
#: cannot drift: a job that sets each conditionally written key
#: (``user_pause_generation``, ``managed_by``, ``app_manifest_digest``,
#: ``acked_item_ids`` with ``acked_item_ids_digest``, ``project_path``, and a
#: non-default ``auto_pause_after_failures`` and ``alert_after_failures``, which
#: are omitted at the default). A record key outside this set came from a newer
#: build. A key inside it is this build's to write or to omit, so a value this
#: build cleared stays cleared.
_KNOWN_JOB_KEYS = frozenset(
    job_record(
        CronJob(
            id="",
            name="",
            message="",
            schedule=CronSchedule(kind="cron"),
            user_pause_generation=1,
            managed_by="-",
            app_manifest_digest="-",
            project_path="-",
            auto_pause_after_failures=0,
            alert_after_failures=2,
            acked_item_ids=["-"],
            acked_item_ids_digest="-",
        )
    )
)

#: The ``schedule`` keys this build writes (``asdict(job.schedule)``).
_KNOWN_SCHEDULE_KEYS = frozenset(f.name for f in fields(CronSchedule))

#: The most this build keeps of what a newer build added, per item: one job's unknown
#: keys together with its schedule's, and the document's unknown top-level keys. It is
#: measured as the JSON text they are written back as (``json.dumps`` of each non-empty
#: mapping; ASCII, so a character is a byte), where :func:`decode_store` keeps them. An
#: item over the cap keeps none of its unknown keys (logged), so the next save writes it
#: as this build knows it.
MAX_UNKNOWN_FIELDS_BYTES = 64 * 1024

#: The most the whole store keeps of them, measured the same way: the document's
#: top-level keys first, then each job in file order. The number of jobs does not bound
#: it (nothing limits how many jobs a store holds), so this does: a job whose keys would
#: take the total past it keeps none of them, and one warning per load counts those jobs.
MAX_UNKNOWN_FIELDS_STORE_BYTES = 1024 * 1024


def _kept_size(what: str, *parts: Mapping[str, Any]) -> int | None:
    """The JSON size of the unknown keys in *parts*, or None (logged) past the per-item cap."""
    try:
        size = sum(len(json.dumps(part)) for part in parts if part)
    except (TypeError, ValueError, RecursionError):
        size = MAX_UNKNOWN_FIELDS_BYTES + 1
    if size <= MAX_UNKNOWN_FIELDS_BYTES:
        return size
    logger.warning(
        "Cron store: %s has %d key(s) from a newer build totalling %d bytes, over the "
        "%d-byte cap; they are not kept, so the next save writes it without them",
        what,
        sum(len(part) for part in parts),
        size,
        MAX_UNKNOWN_FIELDS_BYTES,
    )
    return None


def encode_store(
    jobs: Iterable[CronJob],
    *,
    version: int = _STORE_VERSION,
    extra: Mapping[str, Any] | None = None,
    unknown_fields: Mapping[str, UnknownFields] | None = None,
) -> str:
    """The whole store document for ``jobs``, as ``_save`` writes it (two-space indent).

    ``version``, ``extra`` and ``unknown_fields`` carry what :func:`decode_store` read
    from a store a NEWER build wrote, so this build's save does not delete it. Each is
    written back fill-only, the rule ``KiroCrewConfig.to_dict`` applies to unknown
    config keys: a key this build writes itself is never overwritten, and only a job
    still in ``jobs`` gets its unknown keys back, so a job this build deleted is gone
    with them. ``version`` is written as given: the decoder's, never lower than the
    one read, or the older version a store whose upgrade had to wait keeps. A store with no
    unknown keys encodes exactly as it did before.
    """
    fields_by_job = unknown_fields or {}
    records = []
    for j in jobs:
        record = job_record(j)
        unknown = fields_by_job.get(j.id)
        if unknown is not None:
            for key, value in unknown.schedule.items():
                record["schedule"].setdefault(key, value)
            for key, value in unknown.job.items():
                record.setdefault(key, value)
        records.append(record)
    data: dict[str, Any] = {
        "version": version,
        "jobs": records,
    }
    for key, value in (extra or {}).items():
        data.setdefault(key, value)
    return json.dumps(data, indent=2)


@dataclass(frozen=True)
class UnknownFields:
    """The keys of one job record that this build does not know, kept for its save."""

    job: Mapping[str, Any]
    schedule: Mapping[str, Any]


@dataclass(frozen=True)
class StoreDocument:
    """What :func:`decode_store` read: the jobs, plus what a newer build added.

    ``version`` is the document's version when it is an integer above this build's,
    else this build's. ``extra`` holds the top-level keys other than ``version`` and
    ``jobs``; ``unknown_fields`` maps a loaded job's id to its unknown keys.
    """

    jobs: list[CronJob]
    version: int
    extra: Mapping[str, Any]
    unknown_fields: Mapping[str, UnknownFields]
    #: Written before an unset zone followed the host; see ``zone_default_migration``.
    predates_local_default: bool = False


def _unknown_fields_of(record: dict[str, Any]) -> tuple[UnknownFields, int] | None:
    """The keys of *record* (and of its ``schedule``) this build does not know, with
    their JSON size; None when there are none or they pass the per-job cap."""
    job = {key: value for key, value in record.items() if key not in _KNOWN_JOB_KEYS}
    schedule_record = record.get("schedule")
    schedule = (
        {key: value for key, value in schedule_record.items() if key not in _KNOWN_SCHEDULE_KEYS}
        if isinstance(schedule_record, dict)
        else {}
    )
    if not job and not schedule:
        return None
    size = _kept_size(f"job {record.get('id')!r}", job, schedule)
    if size is None:
        return None
    return UnknownFields(job=job, schedule=schedule), size


def decode_jobs(raw: bytes) -> list[CronJob] | None:
    """The jobs the store bytes ``raw`` hold; :func:`decode_store` without the rest."""
    document = decode_store(raw)
    return None if document is None else document.jobs


def decode_store(raw: bytes) -> StoreDocument | None:
    """The jobs the store bytes ``raw`` hold, each record isolated from the others.

    None when the document parses but is not an object holding a ``jobs`` list
    (a top-level ``[]``, a scalar, ``{"jobs": null}``): nothing can be salvaged
    from it. A decode error (``ValueError`` for bad JSON or invalid UTF-8,
    ``RecursionError`` for deep nesting) propagates to the caller, which owns
    what an unreadable store means (:meth:`CronService._load`).
    """
    data = json.loads(raw)
    records = data.get("jobs", []) if isinstance(data, dict) else None
    if not isinstance(records, list):
        return None
    read_version = data.get("version")
    version = (
        read_version
        if isinstance(read_version, int)
        and not isinstance(read_version, bool)
        and read_version > _STORE_VERSION
        else _STORE_VERSION
    )
    extra = {key: value for key, value in data.items() if key not in _STORE_DOCUMENT_KEYS}
    kept_total = 0
    if extra:
        extra_size = _kept_size("the store document", extra)
        if extra_size is None:
            extra = {}
        else:
            kept_total = extra_size
    unknown_fields: dict[str, UnknownFields] = {}
    past_total = 0
    # Per-entry isolation: a bad record is warned about and skipped, the
    # rest survive. Only KeyError/TypeError are bad-data signals; an
    # AttributeError is a code defect and propagates (see _job_from_record).
    jobs: list[CronJob] = []
    for j in records:
        try:
            job = _job_from_record(j)
        except (KeyError, TypeError) as entry_exc:
            entry_id = j.get("id", "<missing id>") if isinstance(j, dict) else "<not an object>"
            logger.warning(
                "Skipping malformed cron job entry (id=%r): %r; "
                "the entry will be dropped from the store on the next write",
                entry_id,
                entry_exc,
            )
            continue
        jobs.append(job)
        found = _unknown_fields_of(j)
        if found is None:
            continue
        unknown, size = found
        if kept_total + size > MAX_UNKNOWN_FIELDS_STORE_BYTES:
            past_total += 1
            continue
        kept_total += size
        unknown_fields[job.id] = unknown
    if past_total:
        logger.warning(
            "Cron store: %d job(s) carry keys from a newer build past the %d-byte total "
            "the store keeps; they are not kept, so the next save writes those jobs "
            "without them",
            past_total,
            MAX_UNKNOWN_FIELDS_STORE_BYTES,
        )
    return StoreDocument(
        jobs=jobs,
        version=version,
        extra=extra,
        unknown_fields=unknown_fields,
        predates_local_default=predates_local_default(read_version),
    )


#: Infix between the store's file name and the UTC stamp of a quarantine copy:
#: ``crons.json.corrupt-20261005T071953Z``.
_QUARANTINE_INFIX = ".corrupt-"


def quarantine_dir(store: Path) -> Path:
    """Where quarantine copies of *store* live: ``cron-history/quarantine``.

    The copy holds every job's session key and command, the same data the
    ``crons.json`` fence withholds. ``cron-history`` already sits behind the
    agent's file-tool fence and the sandbox mask, so a copy there stays as
    hidden as the store was, and an agent cannot plant a copy for doctor to
    tell the user to restore.
    """
    return store.parent / "cron-history" / "quarantine"


def quarantine_copies(store: Path) -> list[Path]:
    """Every quarantine copy of *store* on disk, oldest first. NEVER raises."""
    try:
        return sorted(quarantine_dir(store).glob(f"{store.name}{_QUARANTINE_INFIX}*"))
    except OSError:
        return []


def quarantine_unreadable_store(store: Path) -> Path | None:
    """Rename an unparseable *store* to a timestamped copy and return the copy.

    The caller holds the store lock. The file is renamed, never rewritten or
    deleted, so the copy holds the exact bytes that failed to parse.

    Returns None, moving nothing, when there is no file, when the bytes parse
    now, or when the read itself fails: an ``OSError`` such as a permission
    error says nothing about the bytes, so the file stays where it is.
    """
    try:
        raw = store.read_bytes()
    except OSError:
        return None
    try:
        if decode_jobs(raw) is not None:
            return None
    except (ValueError, TypeError, RecursionError):
        pass
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    folder = quarantine_dir(store)
    target = folder / f"{store.name}{_QUARANTINE_INFIX}{stamp}"
    suffix = 1
    while target.exists() or target.is_symlink():
        target = folder / f"{store.name}{_QUARANTINE_INFIX}{stamp}-{suffix}"
        suffix += 1
    try:
        folder.mkdir(parents=True, exist_ok=True)
        os.rename(store, target)
    except OSError:
        logger.warning("Could not move the unreadable cron store aside", exc_info=True)
        return None
    return target
