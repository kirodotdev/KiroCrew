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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.cron_service.model import CronJob, CronSchedule

logger = logging.getLogger("kiro_crew.cron")

_CRONS_FILE = "crons.json"


_STORE_VERSION = 2

# Bounded non-blocking acquire for the cron-store advisory lock (see
# CronService._file_lock). The spin never parks the event loop in an
# uninterruptible kernel wait; it fails fast after the timeout instead.
_FILE_LOCK_TIMEOUT_SECS = 10.0  # max wall-time to wait for the store lock
_FILE_LOCK_POLL_SECS = 0.02  # sleep between non-blocking acquire attempts


class CronStoreUnreadable(ValueError):
    """A mutation could not be persisted because the last load failed.

    Derives from ``ValueError`` rather than ``RuntimeError`` so a refusal lands in
    the per-item handlers callers already have. The onboarding importer's apply
    loop catches ``(OSError, ValueError, TypeError, sqlite3.Error)`` per item; a
    ``RuntimeError`` escaped that tuple, so ONE corrupt ``crons.json`` failed the
    whole apply request with a 500 and lost every later item in the plan, instead
    of rejecting the single schedule it actually blocks. The three sites that
    catch both classes list ``CronStoreUnreadable`` BEFORE ``ValueError``, so they
    keep binding their own arm and their messages do not change.

    Raised by :meth:`CronService._save` when ``_load`` could not read
    ``crons.json``. The in-memory job list is empty for that reason rather than
    because the store is empty, so writing it would overwrite records that are
    still on disk. Persisting is refused AND the refusal is raised, so a
    user-initiated mutation reports failure instead of returning success for a
    write that never happened. Background writers (the reaper merge, the job
    result merge, the deferred-removal drain) catch it and degrade: a corrupt
    store must not take down the scheduler loop.

    Also raised by :func:`dispatched_agents_from_disk` when the store is PRESENT
    but nothing loads from it, so a reader that must fail CLOSED (the template
    delete guard) does not mistake an unreadable store for an empty one.
    """


class CronPendingMismatch(RuntimeError):
    """The job's pending secret request changed after the caller read it.

    Raised inside the locked update when an ``expect_secret_env_pending``
    precondition does not match the freshly reloaded record — the
    compare-and-swap that keeps an approval or denial from acting on a request
    the decider never saw (the agent can replace a pending request at any
    moment). Callers surface it as HTTP 409 ``stale_request``.
    """


class CronStoreBusy(TimeoutError):
    """Raised when a cron-store mutator cannot acquire the store lock in time.

    This is the DEFINED failure contract of the store mutators (:meth:`add_job`,
    :meth:`update_job`, :meth:`remove_job`, :meth:`enable_job`, :meth:`ack_job`,
    :meth:`unack_job` and their ``*_async`` variants): under sustained lock
    contention they raise this instead of blocking forever. It subclasses
    :class:`TimeoutError` so the existing ``except TimeoutError`` guards (the
    reaper sweep, the timer tick, the read-path degrade) keep catching it, while
    giving the public scheduling boundaries a named, greppable type to translate
    into a clean *retryable* error — HTTP 409 at the dashboard handlers, a
    structured ``Error:`` string at the MCP tools, a "store busy, try again"
    reply at the Slack surfaces — rather than surfacing an opaque 500 / tool
    crash. Contention is transient (a large atomic save on network storage, the
    CLI process, or the off-loop batch-remove worker holding the lock), so the
    correct caller response is to retry, not to fail permanently.
    """


@contextmanager
def cron_store_lock(
    store_dir: Path, *, timeout: float = _FILE_LOCK_TIMEOUT_SECS, poll: float = _FILE_LOCK_POLL_SECS
) -> Iterator[None]:
    """The cron store's cross-process advisory lock, for a caller with no service.

    ONE implementation of the store lock: :meth:`CronService._file_lock` (every
    store mutator, loop-safety guard included) delegates here, and the Agent
    templates delete guard takes it directly around its reference check and the
    file rename, so a schedule cannot be written between "nothing dispatches this
    template" and the template going -- the two writers exclude each other on
    the same ``.crons.lock`` file the mutators use. The reader side mirrors
    :func:`dispatched_agents_from_disk`: a walk over the store file, so holding
    the store lock across walk + rename is exactly what makes the pair atomic.

    Off the event loop ONLY (the guard runs on a worker thread; the mutators go
    through their ``*_async`` variants): the spin sleeps. Bounded -- raises
    :class:`CronStoreBusy` after *timeout* rather than parking the caller on a
    slow holder. Non-truncating create-or-open (GH-9248): a contending opener on
    Windows must not crash at open() before the spin starts.
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

    The numeric half of the load-time type-shape contract: ``bool`` is
    rejected (a stored ``true`` is not a count), non-``(int, float)`` is
    rejected, a non-finite float is rejected (``json.loads`` parses
    ``NaN``/``Infinity`` by default; NaN breaks comparison ordering and the
    pair emits invalid JSON tokens), and an int too large to convert to a
    float is rejected via ``try/except OverflowError`` -- the same predicate
    shape as ``monitoring.models.is_finite_non_negative_number``, minus its
    sign bound (a value bound has no place at load; see ``_job_from_record``).
    Raises nothing, so nothing can leak outside ``_load``'s
    ``(KeyError, TypeError)`` per-entry isolation.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _record_user_paused(j: dict[str, Any]) -> bool:
    """Single owner for the user-pause predicate of a serialized job.

    The legacy ``!enabled`` fallback covers stores written before ``user_paused``
    existed, where the only record of a pause was the ``enabled`` flag. Every
    reader routes through here for the same reason :func:`_record_is_enabled`
    exists: a future pause-state change must not land in one spelling of this
    derivation and miss another.
    """
    return bool(j.get("user_paused", not j.get("enabled", True)))


def _record_is_enabled(j: dict[str, Any]) -> bool:
    """Single owner for the effective-enabled predicate of a serialized job.

    A job is enabled when it is neither user-paused nor auto-paused, with the
    legacy ``!enabled`` fallback for stores written before those fields existed.
    Both ``_load`` (the scheduler deserialization path) and
    ``count_enabled_from_disk`` (the off-thread dashboard count) MUST route
    through here so the semantics have exactly one implementation and cannot
    drift when a future pause-state change lands in only one reader.
    """
    return not _record_user_paused(j) and not j.get("auto_paused", False)


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
      ``agent_id``, plus the ``agent_sequence`` / ``skip_dates`` lists) raise
      ``TypeError`` on a non-string value, so the record is skipped whole.
      Coercing a selector instead would fail OPEN: a script job whose
      ``script`` degraded to ``""`` silently becomes an LLM agent job
      (``_cron_callback`` picks the mode from which selector is non-empty),
      executing the job's message through the default agent — worse than not
      loading the record at all.
    - **Everything else string-typed** coerces to the field's own unset value
      (``""``, or ``None`` for ``Optional[str]`` fields), and every field
      whose stored value was destroyed by that coercion is named in one
      WARNING, because ``_save`` rewrites ``jobs[]`` wholesale and the coerced
      value replaces the operator's stored one on the next write — the log
      line is the recovery window, exactly like the skip warning in ``_load``.

    It does NOT raise ``AttributeError`` for any record ``json.loads`` can
    produce: every ``.get()`` below is dominated by a ``[...]`` subscript on the
    same object, and only a ``dict`` survives a string subscript. An
    ``AttributeError`` from this function therefore signals a defect in this
    code, not bad data, so :meth:`CronService._load` deliberately lets it
    propagate rather than catching it: catching it there would reclassify a
    valid job as malformed, and because ``_save`` rewrites ``jobs[]`` from
    ``self._jobs`` the next write would erase that job from disk permanently —
    turning a code defect into silent, unrecoverable data loss. Letting it
    propagate trades a loud failure at load for that silent loss.
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
        # Absent key = legacy record, defaults to "". A PRESENT non-string
        # (null included) is malformed: an execution selector has no safe
        # fallback value — see the docstring.
        if field not in j:
            return ""
        value = j[field]
        if not isinstance(value, str):
            raise TypeError(
                f"cron record execution selector {field!r} must be a string, got {type(value).__name__}"
            )
        return value

    def _str_list(field: str) -> list[str]:
        # Absent field: a legacy record predating the field -- default empty.
        # Explicit null is NOT the same thing: no writer produces it, and
        # treating it as empty flips a multi-agent job onto the single-agent
        # fallback -- an execution-path change, so the record skips whole.
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
        # Optional[str] sibling: None means "never set" and is semantically
        # distinct from "" (e.g. last_status gates rendering on `is None`),
        # so a non-string degrades to None, never to an invented "".
        value = j.get(field)
        if value is not None and not isinstance(value, str):
            coerced.append(field)
        return value if isinstance(value, str) else None

    def _guard_num(field: str, default: Any) -> Any:
        # The numeric half of split-by-cost: telemetry numerics COERCE to
        # their declared unset value (never skip -- coercion drops no record,
        # satisfying the data-loss rule unconditionally), joining the same
        # coerced list and single WARNING as the string fields. This is what
        # keeps a NaN/bignum in a telemetry field out of every consumer at
        # once: the JSON envelope (bare NaN/Infinity tokens), timer and
        # due-decision arithmetic (OverflowError / due-every-tick), delivery
        # dedup arithmetic, subprocess budgets, and the secret-grant CAS
        # (NaN != NaN is always true, permanently blocking approval).
        # Absent field: a legacy record -- the declared default, silently.
        # Explicit null is a present value: where the declared unset IS None
        # (last_run_ts) it is the writer's own serialization of "never ran"
        # and passes through; everywhere else it is malformed like any other
        # non-number and joins the coerced list so the load names it.
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

    # Schedule sub-fields decide WHEN the job fires and feed format_schedule
    # on the listing path unguarded, so like the execution selectors they fail
    # CLOSED: a mistyped value skips the record whole. The kind subscript runs
    # first — only a dict survives a string subscript, so the .get() calls
    # below can never raise AttributeError (see the docstring).
    sched = j["schedule"]
    sched_kind = _required_str(sched, "kind")
    for _numeric_field in ("every_secs", "at_ts"):
        _numeric_value = sched.get(_numeric_field)
        if _numeric_value is not None and not _is_representable_number(_numeric_value):
            # Representability is TYPE SHAPE for a schedule numeric: NaN and
            # Infinity break comparison ordering (a NaN at_ts is due every
            # tick) and emit invalid JSON tokens, and a bignum int crashes
            # int-float arithmetic on the timer-arming path (`at_ts - now` ->
            # OverflowError, aborting gateway startup). No write path can
            # persist any of these -- _build_job and _update_job_locked refuse
            # them at the persistence chokepoints, so this reader-side skip
            # provably drops no writer-producible record. Deliberately no
            # FINITE value bound: extreme representable values load intact
            # and are tolerated at their consumers.
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
        # Effective enabled is derived from the two "reasons a job is
        # off": an explicit user pause and an execution auto-pause
        # (repeated failures). Deriving it — rather than trusting the
        # stored `enabled` — is what makes an auto-pause survive a
        # restart: the failing run sets auto_paused=True, and a
        # recurring job's `enabled` is otherwise never persisted, so a
        # naive `enabled` read would resurrect the job on reload.
        # The predicate (incl. the legacy !enabled fallback) has one
        # owner, `_record_is_enabled`, shared with
        # count_enabled_from_disk so the two readers cannot drift.
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
        # member_id / memory_store are memory-identity selectors: they decide
        # WHOSE memory and protected runtime identity the job runs with, and
        # resolve_cron_memory raises on a malformed binding rather than
        # falling back -- so a present non-string skips the record whole,
        # exactly like the execution selectors. Coercing to "" would silently
        # strip a member binding and run the job unbound.
        member_id=_selector_str("member_id"),
        memory_store=_selector_str("memory_store"),
        execution_context=j.get("execution_context"),
        approval_mode=_guard_str("approval_mode"),
        acked_items=j.get("acked_items", []),
        created_by=_guard_str("created_by"),
        source_preset=_guard_str("source_preset"),
        source_template_prompt=_guard_str("source_template_prompt"),
        managed_by=_guard_str("managed_by"),
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
        model=_guard_str("model"),
        last_retry_count=_guard_num("last_retry_count", 0),
        last_retry_run_ts=_guard_num("last_retry_run_ts", 0.0),
        run_generation=_guard_num("run_generation", 0),
        agent_sequence=_str_list("agent_sequence"),
        env=j.get("env", {}),
        timeout_secs=_guard_num("timeout_secs", seams._JOB_TIMEOUT_SECS),
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

    :func:`_job_from_record` is the authority on that — it raises
    ``KeyError``/``TypeError``/``AttributeError`` on a record that is not shaped
    like a job — so asking it is the only honest test. ``isinstance(j, dict)``
    is a weaker stand-in: ``{}`` is a dict the loader rejects. Wrapped here so
    :func:`_read_job_records` keeps its non-raising contract.
    """
    try:
        # warn_on_coercion=False: this probe runs at WS status-pusher cadence
        # (count_enabled_from_disk on every push cycle, per connected
        # dashboard) and never precedes a store rewrite, so the coercion
        # recovery-window WARNING belongs to _load alone — emitting it here
        # would repeat it indefinitely for one bad record.
        _job_from_record(j, warn_on_coercion=False)
    except Exception:
        return False
    return True


def _read_job_records(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Read *path*, returning ``(records, loadable)``. NEVER raises.

    ``loadable`` is False only when the store is PRESENT but the scheduler can
    build nothing from it. It exists for the DIAGNOSTIC caller, which must tell
    "you have no crons" (fine) from "your crons stopped loading" (a fault);
    runtime readers take ``[0]`` and keep degrading quietly, so the records
    half is unchanged by it.

    Single owner of the read-parse-shape prologue for the three readers that
    deliberately bypass the scheduler so they work with no running gateway
    (:func:`referenced_skill_names`, :func:`unhealthy_jobs_from_disk`,
    :meth:`CronService.count_enabled_from_disk`). Each had grown its own
    spelling of this prologue and they had drifted in WHICH corruption they
    survived, so a store that one reader shrugged off crashed another. This is
    NOT every reader of the file — see the exclusions below.

    Every failure mode collapses to "no records", because all three callers
    degrade quietly by contract rather than propagate:

    * ``OSError`` — no file at all (every fresh install), permissions, or a
      directory where the file should be.
    * ``UnicodeError`` — the store is bytes on disk and can hold invalid
      UTF-8. Reading with an explicit encoding also pins the decode to the
      one :func:`~kiro_crew.atomic_write.atomic_write` writes, rather than to
      the caller's locale.
    * ``ValueError`` / ``TypeError`` — unparseable JSON.
    * ``RecursionError`` — deeply nested JSON. It subclasses ``RuntimeError``,
      NOT ``ValueError``, so it escapes a decode-error tuple and would abort
      the caller from inside the read it expected to be safe.
    * Shape — a document that parses but is not an object holding a ``jobs``
      list (a top-level ``[]``, a scalar, ``{"jobs": null}``).

    Non-dict entries are dropped here so that no caller repeats the check.
    All three already discarded them — two by an explicit ``isinstance``
    guard, one by letting :func:`_job_from_record` reject them — so
    filtering centrally preserves each caller's behaviour exactly.

    Three readers are deliberately NOT served, because each owes the user or
    the scheduler a louder reaction than a quiet degrade:

    * :meth:`CronService._load` reads bytes handed over by ``_sync``, logs a
      warning naming the corruption, and resets the store fingerprint — those
      are scheduler-state side effects folding in here would silence.
    * :func:`~kiro_crew.portability._sanitize_imported_crons` rewrites an
      unreadable import to an empty store and reports it to the caller.
    * :func:`~kiro_crew.snapshot._merge_crons` prints which path it could not
      read, skips the merge, and answers ``False`` so its caller can report
      the refusal instead of a success.

    The latter two still guard on ``(OSError, ValueError)`` only, so a deeply
    nested store aborts an import or a snapshot merge there. That is a real
    remaining gap, left for separate work: both owe the user a message naming
    the file, which this quiet loader cannot give them.
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
    # Written only when set, and last: a store with no installer-managed job
    # stays byte-identical to the one an older build writes (pinned by
    # test_cron_refactor_contract's store-bytes fixtures), and an older build
    # reading a newer store simply ignores the extra key.
    if j.managed_by:
        record["managed_by"] = j.managed_by
    return record


def encode_store(jobs: Iterable[CronJob]) -> str:
    """The whole store document for ``jobs``, as ``_save`` writes it (two-space indent)."""
    data = {
        "version": _STORE_VERSION,
        "jobs": [job_record(j) for j in jobs],
    }
    return json.dumps(data, indent=2)


def decode_jobs(raw: bytes) -> list[CronJob] | None:
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
    # Per-entry isolation: one malformed or legacy record must not
    # discard the whole registry. Each record is built in its own
    # try block; a bad one is warned about and skipped, and every
    # well-formed job survives. The whole-store reset in CronService._load
    # is reserved for a file that yields nothing parseable at all, where
    # there is nothing to salvage.
    #
    # The caught tuple is deliberately NARROWER than the exceptions
    # _job_from_record can raise: KeyError and TypeError are its two
    # bad-data signals, and AttributeError is not reachable from JSON.
    # See _job_from_record's docstring for why, and for what catching it
    # would cost.
    jobs: list[CronJob] = []
    for j in records:
        try:
            jobs.append(_job_from_record(j))
        except (KeyError, TypeError) as entry_exc:
            entry_id = j.get("id", "<missing id>") if isinstance(j, dict) else "<not an object>"
            logger.warning(
                "Skipping malformed cron job entry (id=%r): %r; "
                "the entry will be dropped from the store on the next write",
                entry_id,
                entry_exc,
            )
    return jobs


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
