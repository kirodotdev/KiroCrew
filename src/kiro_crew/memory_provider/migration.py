"""Move memory between any two providers, and export or import it as JSONL.

A move is a job with phases::

    plan (dry run) -> run (pages, checkpoint) -> verify -> [switch, by the owner] -> [rollback]

- **Plan** lists the source page by page and counts what would happen per kind
  (written, refused by Crew's content screens, skipped because the target does not
  hold the kind, skipped because the item came from the target). It keeps up to 20
  sample items per kind with their planned outcome, and asks a target that offers
  ``dry_run_put`` for its dedup answer on those samples. It writes nothing.
- **Run** writes each page with ``put_batch`` (or ``put``), keyed
  ``crew-move:<job>:<origin id>``, so a resumed or repeated run writes nothing twice.
  After each page the checkpoint (source cursor and counts) is saved and one ledger
  line per item is appended. A provider error carrying ``retry_after_secs`` pauses
  that long (at most 300 s); three ``unavailable`` answers in a row pause the job
  (state ``paused``) for the owner to resume.
- **Verify** reads back 2% of the items the run inserted (at least 20, at most 200)
  and compares each text's hash.
- **Rollback** deletes from the target every id the ledger marks ``inserted`` by this
  job, and nothing else: an ``enriched``, ``deduped`` or ``unchanged`` item existed
  before. Crew's own memory is never a rollback target, and no job ever deletes
  from it.

Every item a move writes carries its ``origin`` (the first provider, store and id it
had), so a switch back can skip what came from the target. A move never switches
memory: the owner does that with ``PUT /api/memory/provider``.

State lives in :func:`kiro_crew.config.loader_locations.memory_provider_migrations_dir`:
``<job>.json`` and ``<job>.ledger.jsonl``, holding counts, ids and text hashes only,
never memory text. The last 20 jobs are kept. A job found ``running`` when the
gateway starts was interrupted, and reads as ``paused``.

The export format is JSONL ``kirocrew-memory/1``: a header line, one item per line
in the contract's wire form (``revisions`` added when asked for), and a trailer line
with the item count, the count per kind and the SHA-256 of the item lines, so an
import can tell a truncated file. A file without a trailer still imports, with a
warning.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import secrets
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.memory_provider.contract import (
    LESSON_KINDS,
    MemoryItem,
    MemoryProvider,
    NewItem,
    Origin,
    ProviderError,
    ProviderNotSupported,
    ProviderScope,
    ProviderUnavailable,
    item_from_wire,
    new_item_of,
)

logger = logging.getLogger("kiro_crew.memory_provider")

EXPORT_FORMAT = "kirocrew-memory/1"
#: A move or import walks at most this many items; past it the job says it stopped.
MAX_ITEMS = 100_000
MAX_IMPORT_BYTES = 32 * 1024 * 1024
JOBS_KEPT = 20
SAMPLES_PER_KIND = 20
RETRY_AFTER_CAP_SECS = 300.0
UNAVAILABLE_PAUSE_AFTER = 3
VERIFY_SHARE = 0.02
VERIFY_MIN = 20
VERIFY_MAX = 200
#: Revisions read per item on an export that asks for them.
EXPORT_REVISIONS_MAX = 50

#: What a job can be. ``planned`` ends a dry run; ``done`` ends a run.
STATES = frozenset(
    (
        "planning",
        "planned",
        "running",
        "paused",
        "verifying",
        "done",
        "failed",
        "cancelled",
        "rolling_back",
        "rolled_back",
    )
)
ACTIVE_STATES = frozenset(("planning", "running", "verifying", "rolling_back"))
RESUMABLE_STATES = frozenset(("paused", "failed"))
_JOB_ID = re.compile(r"^[0-9a-f]{16}$")

Sleep = Callable[[float], Awaitable[None]]
Notify = Callable[["MigrationJob"], None]


def _dir() -> Path:
    from kiro_crew.config.loader_locations import memory_provider_migrations_dir

    return memory_provider_migrations_dir()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class MigrationJob:
    id: str
    source: str
    target: str
    store: str
    kinds: list[str] | None
    include_retired: bool
    preserve_timestamps: bool
    dry_run: bool
    caller: str
    state: str = "planning"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cursor: str | None = None
    listed: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    skipped_kinds: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    verify: dict[str, int] = field(default_factory=dict)
    rolled_back: int = 0
    error: str = ""
    #: Planned samples per kind: shown in the plan, kept in this process only, never
    #: written to disk (they carry memory text).
    samples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    cancelled: bool = False

    @property
    def from_file(self) -> bool:
        return self.source.startswith("file:")

    def to_record(self) -> dict[str, Any]:
        """The persisted form: no samples, no memory text."""
        return {
            "id": self.id,
            "from": self.source,
            "to": self.target,
            "store": self.store,
            "kinds": self.kinds,
            "include_retired": self.include_retired,
            "preserve_timestamps": self.preserve_timestamps,
            "dry_run": self.dry_run,
            "caller": self.caller,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "checkpoint": {"cursor": self.cursor, "listed": self.listed},
            "by_kind": dict(self.by_kind),
            "outcomes": dict(self.outcomes),
            "skipped_kinds": dict(self.skipped_kinds),
            "notes": list(self.notes),
            "verify": dict(self.verify),
            "rolled_back": self.rolled_back,
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_record(), "samples": {k: list(v) for k, v in self.samples.items()}}

    @classmethod
    def from_record(cls, raw: object) -> MigrationJob | None:
        if not isinstance(raw, dict):
            return None
        try:
            checkpoint = raw.get("checkpoint") or {}
            kinds = raw.get("kinds")
            job = cls(
                id=str(raw["id"]),
                source=str(raw["from"]),
                target=str(raw["to"]),
                store=str(raw.get("store") or "default"),
                kinds=[str(k) for k in kinds] if isinstance(kinds, list) else None,
                include_retired=raw.get("include_retired") is True,
                preserve_timestamps=raw.get("preserve_timestamps") is not False,
                dry_run=raw.get("dry_run") is True,
                caller=str(raw.get("caller") or ""),
                state=str(raw.get("state") or "failed"),
                created_at=float(raw.get("created_at") or 0.0),
                updated_at=float(raw.get("updated_at") or 0.0),
                finished_at=raw.get("finished_at"),
                cursor=checkpoint.get("cursor") if isinstance(checkpoint, dict) else None,
                listed=int(checkpoint.get("listed") or 0) if isinstance(checkpoint, dict) else 0,
                by_kind=dict(raw.get("by_kind") or {}),
                outcomes=dict(raw.get("outcomes") or {}),
                skipped_kinds=dict(raw.get("skipped_kinds") or {}),
                notes=[str(n) for n in raw.get("notes") or []],
                verify=dict(raw.get("verify") or {}),
                rolled_back=int(raw.get("rolled_back") or 0),
                error=str(raw.get("error") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if job.state not in STATES or not _JOB_ID.match(job.id):
            return None
        return job


# ── Persistence ──────────────────────────────────────────────────────────────

_io_lock = threading.Lock()
_live_lock = threading.Lock()
#: Jobs this process holds, so a running job's samples and cancel flag are shared.
_live: dict[str, MigrationJob] = {}
_tasks: set[asyncio.Task] = set()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(job: MigrationJob) -> None:
    job.updated_at = time.time()
    with _io_lock:
        _atomic_write(_dir() / f"{job.id}.json", json.dumps(job.to_record()))


def _append_ledger(job_id: str, lines: Iterable[dict[str, Any]]) -> None:
    body = "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines)
    if not body:
        return
    path = _dir() / f"{job_id}.ledger.jsonl"
    with _io_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(body)


def read_ledger(job_id: str) -> list[dict[str, Any]]:
    if not _JOB_ID.match(job_id):
        return []
    try:
        raw = (_dir() / f"{job_id}.ledger.jsonl").read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _load(job_id: str) -> MigrationJob | None:
    try:
        raw = json.loads((_dir() / f"{job_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    job = MigrationJob.from_record(raw)
    if job is not None and job.state in ACTIVE_STATES:
        # Nothing in this process runs it, so a restart interrupted it.
        job.state = "paused"
        job.notes.append("interrupted by a restart; resume to continue")
    return job


def get_job(job_id: str) -> MigrationJob | None:
    if not _JOB_ID.match(job_id):
        return None
    with _live_lock:
        live = _live.get(job_id)
    return live if live is not None else _load(job_id)


def list_jobs() -> list[MigrationJob]:
    """Every kept job, newest first."""
    try:
        names = [p.stem for p in _dir().glob("*.json") if _JOB_ID.match(p.stem)]
    except OSError:
        return []
    jobs = [job for job in (get_job(name) for name in names) if job is not None]
    return sorted(jobs, key=lambda j: j.created_at, reverse=True)


def _prune() -> None:
    """Keep the newest :data:`JOBS_KEPT` jobs; older ones go with their ledgers."""
    jobs = list_jobs()
    for job in jobs[JOBS_KEPT:]:
        if job.state in ACTIVE_STATES:
            continue
        for suffix in (".json", ".ledger.jsonl"):
            try:
                (_dir() / f"{job.id}{suffix}").unlink()
            except OSError:
                pass


def active_job_for(target: str, store: str) -> MigrationJob | None:
    """A writing job already running into *target* for *store*, if any."""
    with _live_lock:
        live = list(_live.values())
    for job in live:
        if job.target == target and job.store == store and job.state in ACTIVE_STATES:
            if not job.dry_run:
                return job
    return None


def new_job(
    *,
    source: str,
    target: str,
    store: str,
    kinds: Sequence[str] | None,
    include_retired: bool,
    preserve_timestamps: bool,
    dry_run: bool,
    caller: str,
) -> MigrationJob:
    job = MigrationJob(
        id=secrets.token_hex(8),
        source=source,
        target=target,
        store=store or "default",
        kinds=sorted(set(kinds)) if kinds else None,
        include_retired=include_retired,
        preserve_timestamps=preserve_timestamps,
        dry_run=dry_run,
        caller=caller,
    )
    save(job)
    _prune()
    return job


def _hold(job: MigrationJob) -> None:
    with _live_lock:
        _live[job.id] = job


def _release(job: MigrationJob) -> None:
    with _live_lock:
        if _live.get(job.id) is job:
            del _live[job.id]


def start(coro: Awaitable[Any]) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def cancel(job_id: str) -> MigrationJob | None:
    """Stop a running job after its current page, or mark a kept one cancelled."""
    job = get_job(job_id)
    if job is None:
        return None
    if job.state in ACTIVE_STATES:
        job.cancelled = True
        return job
    if job.state in ("planned", "paused", "failed"):
        job.state = "cancelled"
        job.finished_at = time.time()
        save(job)
    return job


# ── Reading the source ───────────────────────────────────────────────────────


def _count(table: dict[str, int], key: str, n: int = 1) -> None:
    table[key] = table.get(key, 0) + n


def screen_refuses(item: NewItem) -> bool:
    """Whether Crew's content screens refuse *item*; they run before every ``put`` Crew makes.

    A lesson gets the lesson screens over its rule and its negative clause; any other
    item gets the injection screen Crew's own stores apply to facts and episodes.
    """
    from kiro_crew.learn import lesson_contains_injection
    from kiro_crew.lesson_validation import contains_volatile_lesson_fact
    from kiro_crew.vector_memory_constants import _contains_injection

    if item.kind not in LESSON_KINDS:
        return _contains_injection(item.text)
    negative = item.meta.get("negative")
    return contains_volatile_lesson_fact(item.text, negative) or lesson_contains_injection(
        item.text, negative
    )


async def iter_pages(
    provider: MemoryProvider,
    scope: ProviderScope,
    *,
    cursor: str | None = None,
    status: str | None = None,
    updated_after: float | None = None,
) -> AsyncIterator[tuple[list[MemoryItem], str | None]]:
    """``(items, cursor after this page)`` until the source ends."""
    while True:
        page = await provider.list(scope, cursor, None, status, updated_after)
        cursor = page.cursor
        yield list(page.items), cursor
        if not cursor:
            return


async def iter_items(
    provider: MemoryProvider, scope: ProviderScope, *, status: str | None = None
) -> AsyncIterator[MemoryItem]:
    """Every item *provider* lists for *scope*; past ``MAX_ITEMS`` it raises."""
    seen = 0
    async for items, _ in iter_pages(provider, scope, status=status):
        for item in items:
            if seen >= MAX_ITEMS:
                # More remain past the cap: a move or export never ends as if whole.
                raise ProviderError(f"stopped after {MAX_ITEMS} items; the rest were not read")
            yield item
            seen += 1


# ── Running a job ────────────────────────────────────────────────────────────


@dataclass
class _Planned:
    item: MemoryItem
    write: NewItem | None
    outcome: str
    origin_id: str


def _origin(job: MigrationJob, item: MemoryItem) -> Origin:
    """The item's first origin: kept from an earlier move, else where it is now."""
    if item.origin is not None:
        return item.origin
    return Origin(job.source, job.store, item.id)


def _plan_item(job: MigrationJob, item: MemoryItem, target_caps: Any) -> _Planned:
    origin = _origin(job, item)
    origin_id = f"{origin.provider}:{origin.store}:{origin.id}"
    if job.kinds and item.kind not in job.kinds:
        return _Planned(item, None, "skipped:not_selected", origin_id)
    if origin.provider == job.target and origin.store == job.store:
        # It came from the target: a switch back must not copy it into itself again.
        return _Planned(item, None, "skipped:from_target", origin_id)
    if item.kind not in target_caps.kinds:
        return _Planned(item, None, "skipped:kind", origin_id)
    if item.kind == "document" and not target_caps.holds_documents:
        return _Planned(item, None, "skipped:documents_stay", origin_id)
    write = new_item_of(item)
    write = NewItem(
        kind=write.kind,
        text=write.text,
        title=write.title,
        scopes=write.scopes,
        source=write.source,
        status=write.status,
        meta=write.meta,
        created_at=write.created_at if job.preserve_timestamps else None,
        updated_at=write.updated_at if job.preserve_timestamps else None,
        origin=origin,
    )
    if len(write.text) > target_caps.max_text:
        return _Planned(item, None, "refused:too_long", origin_id)
    if screen_refuses(write):
        return _Planned(item, None, "refused:content_screen", origin_id)
    return _Planned(item, write, "write", origin_id)


def _sample(job: MigrationJob, planned: _Planned, outcome: str) -> None:
    bucket = job.samples.setdefault(planned.item.kind, [])
    if len(bucket) >= SAMPLES_PER_KIND:
        return
    bucket.append(
        {
            "id": planned.item.id,
            "kind": planned.item.kind,
            "text": planned.item.text[:300],
            "planned": outcome,
            "created_at": planned.item.created_at,
        }
    )


def _key(job: MigrationJob, origin_id: str) -> str:
    return f"crew-move:{job.id}:{hashlib.sha256(origin_id.encode()).hexdigest()[:32]}"


async def _with_retries(
    job: MigrationJob, call: Callable[[], Awaitable[Any]], sleep: Sleep, notify: Notify
) -> Any:
    """*call*, retried after ``retry_after_secs``; three ``unavailable`` in a row pause."""
    streak = 0
    while True:
        try:
            return await call()
        except ProviderError as exc:
            if exc.retry_after_secs is not None:
                job.notes.append(f"provider asked to wait {exc.retry_after_secs:g}s")
                save(job)
                notify(job)
                await sleep(min(RETRY_AFTER_CAP_SECS, exc.retry_after_secs))
                continue
            if not isinstance(exc, ProviderUnavailable):
                raise
            streak += 1
            if streak >= UNAVAILABLE_PAUSE_AFTER:
                raise _Paused(exc.message) from None
            await sleep(float(streak))


class _Paused(Exception):
    """The provider stayed unavailable; the job waits for the owner to resume it."""


async def _write_page(
    job: MigrationJob,
    target: MemoryProvider,
    scope: ProviderScope,
    planned: list[_Planned],
    sleep: Sleep,
    notify: Notify,
) -> None:
    writes = [p for p in planned if p.write is not None]
    for p in planned:
        if p.write is None:
            _count(job.outcomes, p.outcome)
    if not writes:
        return
    entries = [(p.write, _key(job, p.origin_id)) for p in writes if p.write is not None]
    results = await _with_retries(job, lambda: target.put_batch(scope, entries), sleep, notify)
    ledger = []
    for p, result in zip(writes, results):
        _count(job.outcomes, result.outcome)
        assert p.write is not None
        ledger.append(
            {
                "origin_id": p.origin_id,
                "target_id": result.id,
                "outcome": result.outcome,
                "text_sha256": text_sha256(p.write.text),
            }
        )
    _append_ledger(job.id, ledger)


async def _dry_run_samples(
    job: MigrationJob, target: MemoryProvider, scope: ProviderScope, planned: list[_Planned]
) -> None:
    """Ask a target with ``dry_run_put`` what it would do with the sampled items."""
    for p in planned:
        if p.write is None:
            continue
        bucket = job.samples.get(p.item.kind) or []
        sample = next((s for s in bucket if s["id"] == p.item.id), None)
        if sample is None:
            continue
        try:
            result = await target.put(scope, p.write, _key(job, p.origin_id), dry_run=True)
        except ProviderError:
            continue
        sample["planned"] = result.outcome


async def _verify(
    job: MigrationJob, target: MemoryProvider, scope: ProviderScope, rng: random.Random
) -> None:
    caps = await asyncio.to_thread(target.capabilities)
    if not caps.has("get"):
        job.notes.append("the target cannot get items by id, so nothing was read back")
        return
    inserted = [
        e for e in read_ledger(job.id) if e.get("outcome") == "inserted" and e.get("target_id")
    ]
    if not inserted:
        return
    want = min(VERIFY_MAX, max(VERIFY_MIN, int(len(inserted) * VERIFY_SHARE)))
    chosen = rng.sample(inserted, min(want, len(inserted)))
    checked = matched = 0
    for entry in chosen:
        try:
            item = await target.get(scope, str(entry["target_id"]))
        except ProviderError:
            continue
        checked += 1
        if item is not None and text_sha256(item.text) == entry.get("text_sha256"):
            matched += 1
    job.verify = {"checked": checked, "matched": matched, "mismatched": checked - matched}


async def run_job(
    job: MigrationJob,
    source: MemoryProvider | None,
    target: MemoryProvider,
    scope: ProviderScope,
    *,
    items: Sequence[MemoryItem] | None = None,
    sleep: Sleep = asyncio.sleep,
    notify: Notify = lambda job: None,
    rng: random.Random | None = None,
) -> MigrationJob:
    """Plan (a dry run) or run *job* from its checkpoint. Fills and saves *job* as it goes.

    *items* stands in for *source* when the job imports a file.
    """
    _hold(job)
    job.state = "planning" if job.dry_run else "running"
    job.error = ""
    save(job)
    notify(job)
    try:
        caps = await asyncio.to_thread(target.capabilities)
        if not job.dry_run and (caps.read_only or not caps.has("put")):
            raise ProviderError("the target provider does not take writes")
        if not job.dry_run and job.include_retired and not caps.retired:
            job.notes.append("the target keeps no retired items; retired items are skipped")
        status = "all" if job.include_retired else None
        pages: AsyncIterator[tuple[list[MemoryItem], str | None]]
        if items is not None:
            pages = _file_pages(items, job.listed)
        else:
            assert source is not None
            pages = iter_pages(source, scope, cursor=job.cursor, status=status)
        async for page, cursor in pages:
            if job.cancelled:
                job.state = "cancelled"
                break
            planned = []
            for item in page:
                if job.listed >= MAX_ITEMS:
                    raise ProviderError(f"stopped after {MAX_ITEMS} items; the rest were not read")
                if item.status == "retired" and not (job.include_retired and caps.retired):
                    job.listed += 1
                    _count(job.outcomes, "skipped:retired")
                    continue
                job.listed += 1
                _count(job.by_kind, item.kind)
                p = _plan_item(job, item, caps)
                if p.outcome.startswith("skipped:kind"):
                    _count(job.skipped_kinds, item.kind)
                if job.dry_run:
                    outcome = "would_write" if p.write is not None else p.outcome
                    _count(job.outcomes, outcome)
                    _sample(job, p, outcome)
                planned.append(p)
            if job.dry_run:
                if caps.dry_run_put:
                    await _dry_run_samples(job, target, scope, planned)
            else:
                await _write_page(job, target, scope, planned, sleep, notify)
            job.cursor = cursor
            save(job)
            notify(job)
        else:
            if job.dry_run:
                if not caps.holds_documents and job.by_kind.get("document"):
                    job.notes.append(
                        "the target holds no Markdown documents; preferences.md and "
                        "projects.md stay in Crew's memory and Crew keeps reading them"
                    )
                job.state = "planned"
            else:
                job.state = "verifying"
                save(job)
                await _verify(job, target, scope, rng or random.Random())
                job.state = "done"
    except _Paused as exc:
        job.state = "paused"
        job.error = f"the provider stayed unavailable ({exc}); resume when it is back"
    except ProviderError as exc:
        job.state = "failed"
        job.error = exc.message
    except Exception:  # noqa: BLE001 - a job failure is reported, never raised into the loop
        logger.warning("memory move job %s failed", job.id, exc_info=True)
        job.state = "failed"
        job.error = "the move failed"
    if job.state not in ACTIVE_STATES:
        job.finished_at = time.time()
    save(job)
    notify(job)
    _release(job)
    return job


async def _file_pages(
    items: Sequence[MemoryItem], start: int, size: int = 100
) -> AsyncIterator[tuple[list[MemoryItem], str | None]]:
    for offset in range(start, len(items), size):
        end = offset + size
        yield list(items[offset:end]), (str(end) if end < len(items) else None)


async def rollback(
    job: MigrationJob,
    target: MemoryProvider,
    scope: ProviderScope,
    *,
    notify: Notify = lambda job: None,
) -> MigrationJob:
    """Delete from *target* exactly the items this job's ledger marks ``inserted``."""
    _hold(job)
    job.state = "rolling_back"
    save(job)
    notify(job)
    try:
        for entry in read_ledger(job.id):
            if entry.get("outcome") != "inserted" or not entry.get("target_id"):
                continue
            result = await target.delete(scope, str(entry["target_id"]))
            if result.outcome == "deleted":
                job.rolled_back += 1
        job.state = "rolled_back"
    except ProviderNotSupported:
        job.state = "failed"
        job.error = "the target cannot delete items, so the move cannot be rolled back"
    except ProviderError as exc:
        job.state = "failed"
        job.error = f"rollback stopped: {exc.message}"
    job.finished_at = time.time()
    save(job)
    notify(job)
    _release(job)
    return job


# ── Export and import ────────────────────────────────────────────────────────


async def export_lines(
    provider: MemoryProvider,
    provider_id: str,
    scope: ProviderScope,
    *,
    include_retired: bool = False,
    include_revisions: bool = False,
    crew_version: str = "",
    scrub: Callable[[dict[str, Any]], dict[str, Any]] = lambda record: record,
) -> AsyncIterator[str]:
    """The export, one JSON record per line; *scrub* runs on each record before encoding."""
    include = [
        name for name, on in (("retired", include_retired), ("revisions", include_revisions)) if on
    ]
    header = {
        "format": EXPORT_FORMAT,
        "provider": provider_id,
        "store": scope.store_id,
        "member_id": scope.member_id,
        "exported_at": time.time(),
        "crew_version": crew_version,
        "include": include,
    }
    yield json.dumps(scrub(header), ensure_ascii=False) + "\n"
    revisions_on = include_revisions and (await asyncio.to_thread(provider.capabilities)).has(
        "revisions"
    )
    digest = hashlib.sha256()
    count = 0
    by_kind: dict[str, int] = {}
    async for item in iter_items(provider, scope, status="all" if include_retired else None):
        record = item.to_wire()
        if revisions_on:
            try:
                history = await provider.revisions(scope, item.id)
            except ProviderError:
                history = []
            if history:
                record["revisions"] = [r.to_wire() for r in history[:EXPORT_REVISIONS_MAX]]
        line = json.dumps(scrub(record), ensure_ascii=False) + "\n"
        digest.update(line.encode("utf-8"))
        count += 1
        _count(by_kind, item.kind)
        yield line
    trailer = {"trailer": True, "items": count, "by_kind": by_kind, "sha256": digest.hexdigest()}
    yield json.dumps(trailer) + "\n"


def parse_import(raw: bytes) -> tuple[list[MemoryItem], list[str], list[str]]:
    """``(items, errors, warnings)`` from a ``kirocrew-memory/1`` document.

    A trailer that does not match the item lines is an error (the file was cut or
    changed); a file with no trailer imports with a warning.
    """
    if len(raw) > MAX_IMPORT_BYTES:
        return [], ["the import is larger than 32 MB; export it in pages"], []
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    if not lines:
        return [], ["the import is empty"], []
    try:
        header = json.loads(lines[0])
    except ValueError:
        header = None
    if not isinstance(header, dict) or header.get("format") != EXPORT_FORMAT:
        return [], [f"the first line must name the {EXPORT_FORMAT} format"], []
    items: list[MemoryItem] = []
    errors: list[str] = []
    warnings: list[str] = []
    digest = hashlib.sha256()
    item_lines = 0
    trailer: dict[str, Any] | None = None
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            errors.append(f"line {number}: not JSON")
            continue
        if isinstance(record, dict) and record.get("trailer") is True:
            trailer = record
            break
        digest.update((line if line.endswith("\n") else line + "\n").encode("utf-8"))
        item_lines += 1
        if len(items) >= MAX_ITEMS:
            errors.append(f"stopped after {MAX_ITEMS} items")
            break
        if isinstance(record, dict):
            record.pop("revisions", None)
        try:
            items.append(item_from_wire(record))
        except ProviderError as exc:
            errors.append(f"line {number}: {exc.message}"[:200])
    if trailer is None:
        warnings.append("the file has no trailer, so a cut-off export cannot be detected")
    elif trailer.get("sha256") != digest.hexdigest() or trailer.get("items") != item_lines:
        return [], ["the file does not match its trailer: it was cut off or changed"], warnings
    return items, errors, warnings


def file_source_id(raw: bytes) -> str:
    return "file:" + hashlib.sha256(raw).hexdigest()[:16]
