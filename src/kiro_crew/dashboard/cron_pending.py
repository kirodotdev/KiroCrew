"""The disk-backed store for cron runs held while their tab cannot be bound.

A run whose ``cron-{id}`` tab is still being built past the bounded wait
(``slot_create_transaction.wait_for_pending_create``) has nowhere to show. Its
rows go to the ``cron:{id}`` transcript by an append that can time out, and the
tab, once bound, hydrates from a transcript read that can predate that append.
So each such run is written here first, one file per run under
:meth:`ConversationLog.cron_pending_dir`, and stays until a bind of the tab has
merged it and the tab's save has confirmed. Memory keeps references only: a
run's rows are read back off the loop just before a bind (:func:`load`) and
dropped again once merged.

Both bounds are explicit and refuse rather than evict: a run that would take a
job past :data:`PENDING_RUNS_PER_JOB`, or whose entry is over
:data:`PENDING_RUN_MAX_BYTES`, is not held, an ERROR names the job and the run,
and the job's stored result is left as it is. A run whose append confirmed may
make room, because the transcript already holds it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.history import HistoryLockTimeout, append_rows_if_absent
from kiro_crew.owner_only_files import mkdirs_owner_only

if TYPE_CHECKING:
    from kiro_crew.cron import CronJob
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Most runs held for one job, written to its transcript or not. A run lands
#: here only while a create of the job's key outlasts the 30 s wait, so ten
#: carries a job firing every minute through ten minutes of a stuck create.
PENDING_RUNS_PER_JOB = 10
#: Most bytes one run's stored entry (its prompt and result rows as JSON) may
#: take. A full job's ten entries then stay within the 10 MB a transcript file
#: holds before it rotates (``history._SESSION_MAX_BYTES``).
PENDING_RUN_MAX_BYTES = 1024 * 1024
#: Tries at a held run's transcript append. A lock timeout is a wedged holder,
#: not contention (``HistoryLockTimeout``), so a few spaced tries ride out a
#: brief one; past them the stored entry is the copy the next bind shows.
KEEP_RUN_ATTEMPTS = 3
#: Seconds between those tries.
KEEP_RUN_RETRY_SECS = 1.0

_ENTRY_VERSION = 1

Row = tuple[str, str, str, "dict[str, Any] | None"]


@dataclass
class _Ref:
    """One held run: where its entry is, never its rows once they are on disk."""

    run_id: str
    path: Path
    admitted_at: float
    written: bool = False
    #: False when the entry could not be written: its rows then stay in memory,
    #: inside the same per-job cap, as the only copy.
    durable: bool = True
    #: The rows, loaded just before a bind, or kept because the entry is not durable.
    rows: list[Row] | None = None
    #: Merged into a tab whose save has not confirmed yet.
    merging: bool = False
    #: Gave way under the cap (the transcript holds it) but its entry could not
    #: be deleted. It is left out of the cap's count, and the delete is retried
    #: on the job's next admit and load.
    unremoved: bool = False


@dataclass
class _Job:
    refs: dict[str, _Ref] = field(default_factory=dict)
    scanned: bool = False
    #: Runs refused since this process started, named in each refusal's ERROR.
    refused: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


#: Per dashboard state, per job id: the held runs' references, oldest first.
_INDEX: weakref.WeakKeyDictionary[Any, dict[str, _Job]] = weakref.WeakKeyDictionary()
#: The writers and releases in flight, held strongly (the loop keeps only a
#: weak reference) and joined, bounded, at shutdown (:func:`join_writers`).
_TASKS: set[asyncio.Task[None]] = set()


def writers() -> list[asyncio.Task[None]]:
    """The store's tasks still in flight."""
    return [task for task in _TASKS if not task.done()]


async def join_writers(timeout: float) -> None:
    """Wait up to *timeout* seconds for the store's tasks, never cancelling one."""
    pending = writers()
    if pending:
        await asyncio.wait(pending, timeout=timeout)


def _job(state: DashboardState, job_id: str) -> _Job:
    return _INDEX.setdefault(state, {}).setdefault(job_id, _Job())


def run_id(job: CronJob) -> str:
    """The run's identity for logs: the job id and the run's own result timestamp."""
    return f"{job.id}@{getattr(job, 'last_result_ts', '') or '?'}"


def _run_key(job: CronJob, rows: list[Row]) -> str:
    """The entry's file stem: the run's result timestamp, so a retry lands on it again."""
    ts = str(getattr(job, "last_result_ts", "") or "")
    if ts and ts not in {"0", "0.0"}:
        return "".join(c if c.isalnum() or c in "-." else "_" for c in ts)
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()
    return f"r-{digest[:16]}"


def _payload(ref: _Ref, agent: str | None, rows: list[Row]) -> bytes:
    return json.dumps(
        {
            "v": _ENTRY_VERSION,
            "run_id": ref.run_id,
            "admitted_at": ref.admitted_at,
            "agent": agent,
            "written": ref.written,
            "rows": [list(row) for row in rows],
        }
    ).encode("utf-8")


def _write_entry(path: Path, payload: bytes) -> None:
    mkdirs_owner_only(path.parent)
    atomic_write(path, payload, restrict_to_owner=True)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _read_entry(path: Path) -> dict[str, Any] | None:
    """An entry as stored, or ``None`` when it is missing, oversized or malformed."""
    try:
        if path.stat().st_size > PENDING_RUN_MAX_BYTES:
            return None
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != _ENTRY_VERSION:
        return None
    rows = data.get("rows")
    if not isinstance(rows, list) or not all(
        isinstance(row, list)
        and len(row) == 4
        and all(isinstance(part, str) for part in row[:3])
        and (row[3] is None or isinstance(row[3], dict))
        for row in rows
    ):
        return None
    return data


def _scan(directory: Path) -> list[tuple[str, dict[str, Any]]]:
    """Every readable entry under *directory*, oldest admission first."""
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return []
    found = []
    for path in paths:
        data = _read_entry(path)
        if data is None:
            logger.error("cron pending entry %s is unreadable; it is left in place", path)
            continue
        found.append((path.stem, data))
    found.sort(key=lambda item: float(item[1].get("admitted_at") or 0.0))
    return found


def _adopt_scan(job: _Job, directory: Path, found: list[tuple[str, dict[str, Any]]]) -> None:
    for key, data in found:
        if key not in job.refs:
            job.refs[key] = _Ref(
                run_id=str(data.get("run_id") or key),
                path=directory / f"{key}.json",
                admitted_at=float(data.get("admitted_at") or 0.0),
                written=bool(data.get("written")),
            )
    job.refs = dict(sorted(job.refs.items(), key=lambda item: item[1].admitted_at))
    job.scanned = True


def _make_room(job: _Job, key: str) -> list[_Ref] | None:
    """The written entries to drop so *key* fits the cap, or ``None`` when it cannot."""
    if key in job.refs and not job.refs[key].unremoved:
        return []
    live = [ref for ref in job.refs.values() if not ref.unremoved]
    excess = len(live) + 1 - PENDING_RUNS_PER_JOB
    if excess <= 0:
        return []
    victims = [ref for ref in live if ref.written and not ref.merging][:excess]
    return victims if len(victims) == excess else None


def _kept_on_failed_delete(job: _Job, victim: _Ref, exc: OSError) -> None:
    """Keep *victim* listed when its entry could not be deleted, so a later try removes it.

    The run it holds is already in the transcript (or in a tab whose save
    confirmed), so it stops counting toward the cap. A new run that made it
    give way is still held and appended.
    """
    victim.unremoved = True
    victim.merging = False
    victim.rows = None
    job.refs[victim.path.stem] = victim
    job.refs = dict(sorted(job.refs.items(), key=lambda item: item[1].admitted_at))
    logger.warning(
        "cron run %s: its entry %s could not be deleted (%r); the delete is retried",
        victim.run_id,
        victim.path,
        exc,
    )


def _unremoved(job: _Job) -> list[_Ref]:
    return [ref for ref in job.refs.values() if ref.unremoved]


def _deleted(job: _Job, ref: _Ref) -> None:
    if job.refs.get(ref.path.stem) is ref:
        del job.refs[ref.path.stem]


def _refuse(state: DashboardState, job_id: str, rid: str, why: str) -> None:
    job = _job(state, job_id)
    job.refused += 1
    logger.error(
        "cron %s run %s: not held for its tab (%s); the job's stored result is left as it is "
        "(%d run(s) refused since start)",
        job_id,
        rid,
        why,
        job.refused,
    )


def admit(state: DashboardState, job: CronJob, rows: list[Row]) -> None:
    """Hold *rows*, a run whose tab cannot be bound yet, and append them to ``cron:{id}``.

    Runs on the loop: the entry is written, and the append tried, by a tracked
    task. A run over :data:`PENDING_RUN_MAX_BYTES` is refused here; one past
    :data:`PENDING_RUNS_PER_JOB` is refused by the task, once it has counted the
    job's entries on disk. A refused run is still appended, so the transcript
    can still carry it.
    """
    log = state.conversation_log
    if log is None or not rows:
        return
    rid = run_id(job)
    key = _run_key(job, rows)
    ref = _Ref(rid, log.cron_pending_dir(job.id) / f"{key}.json", time.time())
    agent = job.agent_id or None
    payload = _payload(ref, agent, rows)
    hold = len(payload) <= PENDING_RUN_MAX_BYTES
    if not hold:
        _refuse(
            state, job.id, rid, f"{len(payload)} bytes is over the {PENDING_RUN_MAX_BYTES} byte cap"
        )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _hold_and_write_inline(state, job.id, key, ref, agent, rows, payload, hold)
        return
    task = loop.create_task(_hold_and_write(state, job.id, key, ref, agent, rows, payload, hold))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _place(
    state: DashboardState, job: _Job, job_id: str, key: str, ref: _Ref
) -> tuple[bool, list[_Ref]]:
    """Register *ref* under the cap: ``(held, written entries to drop)``."""
    victims = _make_room(job, key)
    if victims is None:
        _refuse(
            state,
            job_id,
            ref.run_id,
            f"the job holds its cap of {PENDING_RUNS_PER_JOB} runs and none can give way",
        )
        return False, []
    for victim in victims:
        job.refs.pop(victim.path.stem, None)
    previous = job.refs.get(key)
    if previous is not None:
        ref.admitted_at = previous.admitted_at
        ref.written = previous.written
    job.refs[key] = ref
    return True, victims


def _store_failed(ref: _Ref, rows: list[Row], exc: BaseException) -> None:
    ref.durable = False
    ref.rows = rows
    logger.error(
        "cron run %s: its pending entry could not be written (%r); it is held in memory only",
        ref.run_id,
        exc,
    )


def _append_failed(job_id: str, ref: _Ref, exc: BaseException) -> None:
    logger.error(
        "cron %s run %s: its transcript append failed after %d tries (%r); "
        "the run is held for its tab's next bind",
        job_id,
        ref.run_id,
        KEEP_RUN_ATTEMPTS,
        exc,
    )


def _append(state: DashboardState, job_id: str, agent: str | None, rows: list[Row]) -> None:
    log = state.conversation_log
    if log is None:
        return
    append_rows_if_absent(
        log,
        f"cron:{job_id}",
        [(role, content, cls, None) for role, content, cls, _meta in rows],
        agent=agent,
        row_meta=[meta for _role, _content, _cls, meta in rows],
    )


async def _append_with_retries(
    state: DashboardState, job_id: str, ref: _Ref, agent: str | None, rows: list[Row]
) -> BaseException | None:
    """Append *rows* off the loop, retrying a lock timeout; the last failure, or ``None``.

    Any other failure stops at once: an I/O error does not clear on a retry a
    second later.
    """
    from kiro_crew.dashboard.chat_utils import drained_to_thread

    failure: BaseException | None = None
    for attempt in range(KEEP_RUN_ATTEMPTS):
        if attempt:
            await asyncio.sleep(KEEP_RUN_RETRY_SECS)
        try:
            await drained_to_thread(_append, state, job_id, agent, rows)
        except HistoryLockTimeout as timeout:
            failure = timeout
            logger.warning(
                "cron %s run %s: transcript lock timed out (try %d of %d)",
                job_id,
                ref.run_id,
                attempt + 1,
                KEEP_RUN_ATTEMPTS,
            )
            continue
        except Exception as error:  # noqa: BLE001 - the stored entry is the copy
            return error
        return None
    return failure


async def _retry_deletes(job: _Job) -> None:
    """Retry the delete of each entry that gave way but could not be removed."""
    from kiro_crew.dashboard.chat_utils import drained_to_thread

    for stale in _unremoved(job):
        try:
            await drained_to_thread(_unlink, stale.path)
        except OSError:
            continue
        _deleted(job, stale)


async def _hold_and_write(
    state: DashboardState,
    job_id: str,
    key: str,
    ref: _Ref,
    agent: str | None,
    rows: list[Row],
    payload: bytes,
    hold: bool,
) -> None:
    from kiro_crew.dashboard.chat_utils import drained_to_thread

    held = False
    if hold:
        job = _job(state, job_id)
        async with job.lock:
            if not job.scanned:
                directory = ref.path.parent
                _adopt_scan(job, directory, await drained_to_thread(_scan, directory))
            await _retry_deletes(job)
            held, victims = _place(state, job, job_id, key, ref)
            for victim in victims:
                try:
                    await drained_to_thread(_unlink, victim.path)
                except OSError as exc:
                    _kept_on_failed_delete(job, victim, exc)
            if held:
                try:
                    await drained_to_thread(_write_entry, ref.path, _payload(ref, agent, rows))
                except Exception as exc:  # noqa: BLE001 - kept in memory instead
                    _store_failed(ref, rows, exc)
    failure = await _append_with_retries(state, job_id, ref, agent, rows)
    if failure is not None:
        _append_failed(job_id, ref, failure)
        return
    if held:
        ref.written = True
        current = _INDEX.get(state, {}).get(job_id)
        if ref.durable and current is not None and current.refs.get(key) is ref and not ref.merging:
            try:
                await drained_to_thread(_write_entry, ref.path, _payload(ref, agent, rows))
            except Exception:  # noqa: BLE001 - the entry still holds the run
                logger.debug("cron run %s: written mark not stored", ref.run_id, exc_info=True)


def _hold_and_write_inline(
    state: DashboardState,
    job_id: str,
    key: str,
    ref: _Ref,
    agent: str | None,
    rows: list[Row],
    payload: bytes,
    hold: bool,
) -> None:
    """:func:`_hold_and_write` with no running loop: the same steps, back to back."""
    held = False
    if hold:
        job = _job(state, job_id)
        if not job.scanned:
            _adopt_scan(job, ref.path.parent, _scan(ref.path.parent))
        for stale in _unremoved(job):
            try:
                _unlink(stale.path)
            except OSError:
                continue
            _deleted(job, stale)
        held, victims = _place(state, job, job_id, key, ref)
        for victim in victims:
            try:
                _unlink(victim.path)
            except OSError as exc:
                _kept_on_failed_delete(job, victim, exc)
        if held:
            try:
                _write_entry(ref.path, _payload(ref, agent, rows))
            except Exception as exc:  # noqa: BLE001 - kept in memory instead
                _store_failed(ref, rows, exc)
    failure: BaseException | None = None
    for _attempt in range(KEEP_RUN_ATTEMPTS):
        try:
            _append(state, job_id, agent, rows)
        except HistoryLockTimeout as timeout:
            failure = timeout
            continue
        except Exception as error:  # noqa: BLE001 - the stored entry is the copy
            failure = error
            break
        failure = None
        break
    if failure is not None:
        _append_failed(job_id, ref, failure)
    elif held:
        ref.written = True


async def load(state: DashboardState, job_id: str) -> None:
    """Read *job_id*'s held runs off the loop, so the next bind can merge them.

    The first call in a process lists the job's entries on disk, which is how a
    run held before a restart reaches its tab.
    """
    log = state.conversation_log
    if log is None:
        return
    from kiro_crew.dashboard.chat_utils import drained_to_thread

    job = _job(state, job_id)
    async with job.lock:
        if not job.scanned:
            directory = log.cron_pending_dir(job_id)
            _adopt_scan(job, directory, await drained_to_thread(_scan, directory))
        await _retry_deletes(job)
        for ref in list(job.refs.values()):
            if ref.rows is not None or ref.merging or not ref.durable or ref.unremoved:
                continue
            data = await drained_to_thread(_read_entry, ref.path)
            if data is None:
                continue
            ref.rows = [
                (str(role), str(content), str(cls), meta)
                for role, content, cls, meta in data["rows"]
            ]


def merge_into(state: DashboardState, job_id: str, slot: Any) -> None:
    """Show *job_id*'s loaded held runs in its bound tab, each row once.

    A row already in the tab (the hydration read carried it) is skipped. The
    entries are deleted only once the tab's save confirms (:func:`_release`),
    so a save that fails leaves them for the next bind.
    """
    job = _INDEX.get(state, {}).get(job_id)
    if job is None:
        return
    merged = [
        ref
        for ref in job.refs.values()
        if ref.rows is not None and not ref.merging and not ref.unremoved
    ]
    if not merged:
        return
    for ref in merged:
        for role, content, cls, meta in ref.rows or ():
            if any(msg.get("content") == content for msg in slot.messages):
                continue
            slot.append(role, content, cls, meta=meta)
        ref.merging = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _release_inline(state, job_id, slot, merged)
        return
    task = loop.create_task(_release(state, job_id, slot, merged))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _released(state: DashboardState, job_id: str, merged: list[_Ref], saved: bool) -> list[_Ref]:
    """Settle *merged* after the tab's save: the entries to delete, if it confirmed."""
    job = _INDEX.get(state, {}).get(job_id)
    if not saved:
        logger.warning(
            "cron %s: the tab's save did not confirm; %d held run(s) stay for its next bind",
            job_id,
            len(merged),
        )
        for ref in merged:
            ref.merging = False
            if ref.durable:
                ref.rows = None
        return []
    if job is not None:
        for ref in merged:
            if job.refs.get(ref.path.stem) is ref:
                del job.refs[ref.path.stem]
    return [ref for ref in merged if ref.durable]


def _release_failed(state: DashboardState, job_id: str, ref: _Ref, exc: OSError) -> None:
    """Keep a merged entry whose delete failed listed, so a later admit or load removes it."""
    job = _INDEX.get(state, {}).get(job_id)
    if job is None:
        logger.warning(
            "cron run %s: its entry %s could not be deleted (%r)", ref.run_id, ref.path, exc
        )
        return
    _kept_on_failed_delete(job, ref, exc)


async def _release(state: DashboardState, job_id: str, slot: Any, merged: list[_Ref]) -> None:
    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.chat_utils import drained_to_thread

    try:
        saved = await chat_persistence.save_slot_off_loop(
            state,
            slot,
            force=True,
            best_effort=False,
            expected_history_key=f"cron:{job_id}",
            expected_slot_name=slot.key,
        )
    except Exception:  # noqa: BLE001 - the entries stay for the next bind
        logger.warning("cron %s: the tab's save raised", job_id, exc_info=True)
        saved = False
    for ref in _released(state, job_id, merged, bool(saved)):
        try:
            await drained_to_thread(_unlink, ref.path)
        except OSError as exc:
            _release_failed(state, job_id, ref, exc)


def _release_inline(state: DashboardState, job_id: str, slot: Any, merged: list[_Ref]) -> None:
    from kiro_crew.dashboard import chat_persistence

    try:
        saved = chat_persistence._save_slot_to_history(
            state,
            slot,
            force=True,
            expected_history_key=f"cron:{job_id}",
            expected_slot_name=slot.key,
        )
    except Exception:  # noqa: BLE001 - the entries stay for the next bind
        logger.warning("cron %s: the tab's save raised", job_id, exc_info=True)
        saved = False
    for ref in _released(state, job_id, merged, bool(saved)):
        try:
            _unlink(ref.path)
        except OSError as exc:
            _release_failed(state, job_id, ref, exc)
