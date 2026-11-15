"""Move memory between any two providers, and export or import it as JSONL.

A move is a job with phases::

    plan (dry run) -> run (pages, checkpoint) -> verify -> [switch, by the owner] -> [rollback]

- **Plan** lists the source page by page and counts what would happen per kind
  (written, refused by Crew's content screens, skipped because the target does not
  hold the kind, skipped because the item came from the target). It keeps up to 20
  sample items per kind with their planned outcome. It never sends memory to the
  target and writes nothing.
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
import io
import json
import logging
import os
import random
import re
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
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
    WriteResult,
    item_from_wire,
    new_item_of,
    text_cap,
    was_cut,
)

logger = logging.getLogger("kiro_crew.memory_provider")

EXPORT_FORMAT = "kirocrew-memory/1"
#: A move or import walks at most this many items; past it the job says it stopped.
MAX_ITEMS = 100_000
MAX_IMPORT_BYTES = 32 * 1024 * 1024
JOBS_KEPT = 20
SAMPLES_PER_KIND = 20
RETRY_AFTER_CAP_SECS = 300.0
#: Waits a provider may ask for on one batch before the job pauses for the owner.
RETRY_AFTER_MAX_WAITS = 5
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
        "rollback_failed",
    )
)
ACTIVE_STATES = frozenset(("planning", "running", "verifying", "rolling_back"))
#: A paused or failed MOVE resumes with ``run_job``. A failed or interrupted ROLLBACK
#: is ``rollback_failed`` and only ever continues with ``rollback``, so undoing a move
#: can never run the move again.
RESUMABLE_STATES = frozenset(("paused", "failed"))
#: The states a rollback may start (or continue) from.
ROLLBACK_STATES = frozenset(("done", "paused", "failed", "cancelled", "rollback_failed"))
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
    created_at: float = field(default_factory=lambda: time.time())
    updated_at: float = field(default_factory=lambda: time.time())
    finished_at: float | None = None
    cursor: str | None = None
    #: The whole source was read and written. Kept apart from ``cursor``, which is
    #: ``None`` both before the first page and after the last: a resume of a job
    #: whose read-back failed goes straight to the read-back, never re-copies.
    source_done: bool = False
    listed: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    skipped_kinds: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    verify: dict[str, int] = field(default_factory=dict)
    rolled_back: int = 0
    #: Source item ids of the batch in flight, sent but not yet ledgered (ids only,
    #: never text). Saved before the send: a reply lost for good leaves them here, and
    #: a rollback first replays their keys from the source to learn what the provider
    #: applied (a replay answers the first application's outcome and id).
    pending_ids: list[str] = field(default_factory=list)
    #: The counts at the committed cursor while a page is in flight. A restart mid-page
    #: restores them before the page is read again, so its items are counted once.
    page_start: dict[str, Any] | None = None
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
            "checkpoint": {
                "cursor": self.cursor,
                "listed": self.listed,
                "source_done": self.source_done,
                "pending_ids": list(self.pending_ids),
                "page_start": self.page_start,
            },
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
                source_done=isinstance(checkpoint, dict) and checkpoint.get("source_done") is True,
                pending_ids=(
                    [str(i) for i in checkpoint.get("pending_ids") or []]
                    if isinstance(checkpoint, dict)
                    else []
                ),
                page_start=_page_start(checkpoint),
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


def _page_start(checkpoint: object) -> dict[str, Any] | None:
    raw = checkpoint.get("page_start") if isinstance(checkpoint, dict) else None
    if not isinstance(raw, dict) or not isinstance(raw.get("listed"), int):
        return None
    if not all(isinstance(raw.get(k), dict) for k in ("by_kind", "outcomes", "skipped_kinds")):
        return None
    return raw


# ── Persistence ──────────────────────────────────────────────────────────────

_io_lock = threading.Lock()
_live_lock = threading.Lock()
#: Jobs this process holds, so a running job's samples and cancel flag are shared.
_live: dict[str, MigrationJob] = {}
#: Job ids a resume or rollback admitted and is still preparing, so a second request
#: for the same job is refused before either one starts a runner.
_reserved: set[str] = set()
#: Reserved job ids the owner cancelled before their runner started: the runner's
#: instance takes the cancel when it starts, so a resume never runs over it.
_cancel_requested: set[str] = set()


def reserve(job_id: str) -> bool:
    """Admit one runner for *job_id*; ``False`` when one is running or being prepared."""
    with _live_lock:
        if job_id in _live or job_id in _reserved:
            return False
        _reserved.add(job_id)
        return True


def unreserve(job_id: str) -> None:
    with _live_lock:
        _reserved.discard(job_id)
        _cancel_requested.discard(job_id)


async def reserved(job_id: str, coro: Awaitable[Any]) -> Any:
    """Run *coro*, then free *job_id* for the next resume or rollback."""
    try:
        return await coro
    finally:
        unreserve(job_id)


_tasks: set[asyncio.Task] = set()
#: Samples of finished plans, kept in this process only (they carry memory text and
#: are never written to disk), so a plan read after it finished still shows them.
_plan_samples: OrderedDict[str, dict[str, list[dict[str, Any]]]] = OrderedDict()
_PLAN_SAMPLES_KEPT = 20


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
    """The job's ledger. Only a missing file is empty: a damaged or unreadable one
    raises, so verify and undo never read "nothing written" from it."""
    if not _JOB_ID.match(job_id):
        return []
    try:
        raw = (_dir() / f"{job_id}.ledger.jsonl").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict):
            raise ValueError(f"move ledger {job_id} holds a line that is not an object")
        out.append(entry)
    return out


def _load(job_id: str) -> MigrationJob | None:
    try:
        raw = json.loads((_dir() / f"{job_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    job = MigrationJob.from_record(raw)
    if job is not None and job.state in ACTIVE_STATES:
        # Nothing in this process runs it, so a restart interrupted it. An undo
        # stays an undo: it continues with a rollback, never with a resume.
        if job.state == "rolling_back":
            job.state = "rollback_failed"
            job.notes.append("the undo was interrupted by a restart; undo again to finish it")
        else:
            job.state = "paused"
            job.notes.append("interrupted by a restart; resume to continue")
    return job


def get_job(job_id: str) -> MigrationJob | None:
    if not _JOB_ID.match(job_id):
        return None
    with _live_lock:
        live = _live.get(job_id)
        samples = _plan_samples.get(job_id)
    if live is not None:
        return live
    job = _load(job_id)
    if job is not None and samples:
        job.samples = samples
    return job


def list_jobs() -> list[MigrationJob]:
    """Every kept job, newest first."""
    try:
        names = [p.stem for p in _dir().glob("*.json") if _JOB_ID.match(p.stem)]
    except OSError:
        return []
    jobs = [job for job in (get_job(name) for name in names) if job is not None]
    return sorted(jobs, key=lambda j: j.created_at, reverse=True)


def _prune() -> None:
    """Keep the newest :data:`JOBS_KEPT` plans and, apart, the newest real moves.

    Plans and moves are counted separately: a plan (which an app may start) never
    evicts a move, so a move's ledger, the only record an undo can use, stays
    until newer moves push it out.
    """
    jobs = list_jobs()
    plans = [j for j in jobs if j.dry_run]
    moves = [j for j in jobs if not j.dry_run]
    for job in plans[JOBS_KEPT:] + moves[JOBS_KEPT:]:
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
        if job.id in _cancel_requested:
            _cancel_requested.discard(job.id)
            job.cancelled = True


def _release(job: MigrationJob) -> None:
    with _live_lock:
        if _live.get(job.id) is job:
            del _live[job.id]
        if job.dry_run and job.samples:
            _plan_samples[job.id] = job.samples
            _plan_samples.move_to_end(job.id)
            while len(_plan_samples) > _PLAN_SAMPLES_KEPT:
                _plan_samples.popitem(last=False)


def start(coro: Awaitable[Any]) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def cancel(job_id: str) -> MigrationJob | None:
    """Stop a running job after its current page, or mark a kept one cancelled."""
    job = get_job(job_id)
    if job is None:
        return None
    with _live_lock:
        preparing = job_id in _reserved and job_id not in _live
        if preparing:
            _cancel_requested.add(job_id)
    if preparing:
        job.cancelled = True
        return job
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


def redact_model_written(item: NewItem) -> NewItem:
    """*item* with credentials and exfiltration URLs redacted from every field a model wrote.

    For memory Crew writes on its own (a consolidation pass, an agent's or a task's
    lesson): its text, title, scopes and meta values come from a model, not from the
    owner, so a secret it repeats never leaves for a provider. An owner's own move or
    import is sent as written.
    """
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    return replace(
        item,
        text=_redact_memory_field(item.text),
        title=_redact_memory_field(item.title) if item.title else item.title,
        scopes=tuple(_redact_memory_field(s) for s in item.scopes),
        meta={k: _redact_memory_field(v) for k, v in item.meta.items()},
    )


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


async def _no_pages() -> AsyncIterator[tuple[list[MemoryItem], str | None]]:
    """No pages: a resume of a job that already read its whole source."""
    return
    yield  # pragma: no cover - makes this an async generator


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


def _content(item: MemoryItem) -> tuple[Any, ...]:
    """What a person can change about an item: never its id, times or provenance."""
    return (
        item.kind,
        item.text,
        tuple(sorted(item.scopes)),
        item.status or "active",
        item.title or "",
        tuple(
            item.meta.get(k) or ""
            for k in (
                "negative",
                "crew.category",
                "crew.value_json",
                "crew.tags",
                "crew.importance",
            )
        ),
    )


async def _returning(
    job: MigrationJob, target: MemoryProvider, scope: ProviderScope, item: MemoryItem, p: _Planned
) -> _Planned:
    """An item that first came from the target: skipped only if it came back unchanged.

    One edited while it lived elsewhere (a tier change, a new text) is reported as a
    conflict, never skipped as a copy and never written over the target's own item.
    """
    assert item.origin is not None
    # A failed read fails the job (it pauses or fails like any other provider error):
    # an unknown answer is never taken as "gone".
    home = await target.get(scope, item.origin.id)
    if home is None or _content(home) == _content(item):
        # Gone from home (removed there on purpose) or the same: nothing to bring back.
        return p
    return _Planned(item, None, "conflict:changed_since_copy", p.origin_id)


def _plan_item(job: MigrationJob, item: MemoryItem, target_caps: Any) -> _Planned:
    origin = _origin(job, item)
    origin_id = f"{origin.provider}:{origin.store}:{origin.id}"
    if job.kinds and item.kind not in job.kinds:
        return _Planned(item, None, "skipped:not_selected", origin_id)
    if origin.provider == job.target and origin.store == job.store and not job.from_file:
        # A file import is exempt: restoring a store from its own export must bring
        # back what was deleted; the target's dedup answers what still exists.
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
    if was_cut(write.text) or len(write.text) > min(target_caps.max_text, text_cap(write.kind)):
        # A source item read past its kind's cap arrives cut; a move never writes a
        # shortened copy as if it were the whole item.
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


def _key(job: MigrationJob, p: _Planned) -> str:
    # The source record's own id is part of the key: two records can share a kept
    # first origin (two imports of one fact), and each must be written, not replayed.
    ident = f"{p.origin_id}\0{p.item.id}"
    return f"crew-move:{job.id}:{hashlib.sha256(ident.encode()).hexdigest()[:32]}"


async def _with_retries(
    job: MigrationJob, call: Callable[[], Awaitable[Any]], sleep: Sleep, notify: Notify
) -> Any:
    """*call*, retried after ``retry_after_secs`` or a short wait.

    Three ``unavailable`` answers in a row, or :data:`RETRY_AFTER_MAX_WAITS` waits a
    provider asked for, pause the job. A cancel is honoured between attempts.
    """
    streak = 0
    waits = 0
    while True:
        if job.cancelled:
            raise _Cancelled()
        try:
            return await call()
        except ProviderError as exc:
            if exc.retry_after_secs is not None:
                waits += 1
                if waits > RETRY_AFTER_MAX_WAITS:
                    raise _Paused(f"the provider kept asking to wait ({exc.message})") from None
                note = "the provider asked to wait"
                job.notes = [n for n in job.notes if not n.startswith(note)]
                job.notes.append(f"{note} ({waits} times this batch)")
                await asyncio.to_thread(save, job)
                notify(job)
                await sleep(max(1.0, min(RETRY_AFTER_CAP_SECS, exc.retry_after_secs)))
                continue
            if not isinstance(exc, ProviderUnavailable):
                raise
            streak += 1
            if streak >= UNAVAILABLE_PAUSE_AFTER:
                raise _Paused(exc.message) from None
            await sleep(float(streak))


class _Cancelled(Exception):
    """The owner cancelled the job while it waited on the provider."""


class _Paused(Exception):
    """The provider stayed unavailable; the job waits for the owner to resume it."""


async def _write_page(
    job: MigrationJob,
    target: MemoryProvider,
    scope: ProviderScope,
    planned: list[_Planned],
    sleep: Sleep,
    notify: Notify,
    max_batch: int,
    target_batches: bool = True,
) -> None:
    """Write one page in the target's own batch size, ledgering each batch as it lands.

    A batch retried after a lost reply, or a page a resumed job sends again, replays
    keys the provider already applied. The contract has a replay answer the outcome of
    the key's first application, so an item this job inserted reads ``inserted``
    again and stays on the rollback list, and an item that already existed reads
    ``deduped`` and is never deleted.
    """
    writes = [p for p in planned if p.write is not None]
    for p in planned:
        if p.write is None:
            _count(job.outcomes, p.outcome)
    # Without ``put_batch`` the provider takes one ``put`` per entry: a batch of one,
    # so each item is ledgered the moment it lands and none is lost to a later failure.
    size = max(1, max_batch) if target_batches else 1
    for start in range(0, len(writes), size):
        chunk = writes[start : start + size]
        entries: list[tuple[NewItem, str]] = [
            (p.write, _key(job, p)) for p in chunk if p.write is not None
        ]

        def send(batch: list[tuple[NewItem, str]] = entries) -> Awaitable[list[WriteResult]]:
            return target.put_batch(scope, batch)

        job.pending_ids = [p.item.id for p in chunk if p.write is not None]
        await asyncio.to_thread(save, job)
        results = await _with_retries(job, send, sleep, notify)
        ledger = []
        for p, result in zip(chunk, results):
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
        await asyncio.to_thread(_append_ledger, job.id, ledger)
        job.pending_ids = []


async def _verify(
    job: MigrationJob, target: MemoryProvider, scope: ProviderScope, rng: random.Random
) -> None:
    caps = await asyncio.to_thread(target.capabilities)
    if not caps.has("get"):
        job.notes.append("the target cannot get items by id, so nothing was read back")
        return
    inserted = [
        e
        for e in (await asyncio.to_thread(read_ledger, job.id))
        if e.get("outcome") == "inserted" and e.get("target_id")
    ]
    if not inserted:
        return
    want = min(VERIFY_MAX, max(VERIFY_MIN, int(len(inserted) * VERIFY_SHARE)))
    chosen = rng.sample(inserted, min(want, len(inserted)))
    checked = matched = unread = 0
    for entry in chosen:
        try:
            item = await target.get(scope, str(entry["target_id"]))
        except ProviderError:
            unread += 1
            continue
        checked += 1
        if item is not None and text_sha256(item.text) == entry.get("text_sha256"):
            matched += 1
    job.verify = {
        "checked": checked,
        "matched": matched,
        "mismatched": checked - matched,
        "unread": unread,
    }


def _counters(job: MigrationJob) -> dict[str, Any]:
    """The counts a page checkpoint stands for, so an unfinished page can be undone."""
    return {
        "listed": job.listed,
        "by_kind": dict(job.by_kind),
        "outcomes": dict(job.outcomes),
        "skipped_kinds": dict(job.skipped_kinds),
    }


def _restore(job: MigrationJob, counters: dict[str, Any]) -> None:
    job.listed = counters["listed"]
    job.by_kind = dict(counters["by_kind"])
    job.outcomes = dict(counters["outcomes"])
    job.skipped_kinds = dict(counters["skipped_kinds"])


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
    if job.page_start is not None:
        # Stopped mid-page: that page is read and counted again from its first item.
        _restore(job, job.page_start)
        job.page_start = None
    checkpoint = _counters(job)
    try:
        await asyncio.to_thread(save, job)
    except BaseException:
        # Never started: release it, so a resume or undo is not refused as busy.
        _release(job)
        raise
    notify(job)
    try:
        if job.cancelled:
            raise _Cancelled()
        caps = await asyncio.to_thread(target.capabilities)
        # A plan applies the same bar, so it never counts writes the run would refuse.
        if caps.read_only or not caps.has("put"):
            raise ProviderError("the target provider does not take writes")
        if not caps.idempotent:
            # A retried or settled write resends its key: without a replay it would
            # store a second copy the ledger never names, so undo could not remove it.
            raise ProviderError("the target provider does not replay a repeated write")
        if not job.dry_run and job.include_retired and not caps.retired:
            job.notes.append("the target keeps no retired items; retired items are skipped")
        status = "all" if job.include_retired else None
        if job.pending_ids and not job.dry_run:
            # A batch whose reply was lost is ledgered before any page is sent again,
            # so a source item removed meanwhile cannot leave an unledgered copy.
            await _settle_pending(job, source if items is None else None, items, target, scope)
        pages: AsyncIterator[tuple[list[MemoryItem], str | None]]
        if job.source_done and not job.dry_run:
            pages = _no_pages()
        elif items is not None:
            pages = _file_pages(items, job.listed)
        else:
            assert source is not None
            pages = iter_pages(source, scope, cursor=job.cursor, status=status)
        async for page, cursor in pages:
            if job.cancelled:
                job.state = "cancelled"
                break
            # Counted again from here if this page does not finish: a page is
            # checkpointed whole, so a resume replays it from its first item.
            checkpoint = _counters(job)
            if not job.dry_run:
                job.page_start = checkpoint
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
                if p.outcome == "skipped:from_target":
                    p = await _returning(job, target, scope, item, p)
                if p.outcome.startswith("skipped:kind"):
                    _count(job.skipped_kinds, item.kind)
                if job.dry_run:
                    outcome = "would_write" if p.write is not None else p.outcome
                    _count(job.outcomes, outcome)
                    _sample(job, p, outcome)
                planned.append(p)
            # A plan stays local: it never sends memory to the target, so a plan an app
            # asked for (directly, or through the copy sheet) hands it nothing.
            if not job.dry_run:
                await _write_page(
                    job,
                    target,
                    scope,
                    planned,
                    sleep,
                    notify,
                    caps.max_batch,
                    caps.has("put_batch"),
                )
            job.cursor = cursor
            job.page_start = None
            checkpoint = _counters(job)
            await asyncio.to_thread(save, job)
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
                job.source_done = True
                await asyncio.to_thread(save, job)
                await _verify(job, target, scope, rng or random.Random())
                if job.verify.get("mismatched") or job.verify.get("unread"):
                    # Not done until what was written reads back as written: the owner
                    # can resume (keys replay, nothing is written twice) or undo.
                    job.state = "failed"
                    job.error = "the read-back check did not pass"
                else:
                    job.state = "done"
    except _Cancelled:
        _restore(job, checkpoint)
        job.state = "cancelled"
    except _Paused as exc:
        _restore(job, checkpoint)
        job.state = "paused"
        job.error = f"the provider stayed unavailable ({exc}); resume when it is back"
    except ProviderError as exc:
        _restore(job, checkpoint)
        job.state = "failed"
        job.error = exc.message
    except Exception:  # noqa: BLE001 - a job failure is reported, never raised into the loop
        logger.warning("memory move job %s failed", job.id, exc_info=True)
        _restore(job, checkpoint)
        job.state = "failed"
        job.error = "the move failed"
    if job.state not in ACTIVE_STATES:
        job.finished_at = time.time()
    try:
        await asyncio.to_thread(save, job)
        notify(job)
    finally:
        _release(job)
    return job


async def _file_pages(
    items: Sequence[MemoryItem], start: int, size: int = 100
) -> AsyncIterator[tuple[list[MemoryItem], str | None]]:
    for offset in range(start, len(items), size):
        end = offset + size
        yield list(items[offset:end]), (str(end) if end < len(items) else None)


async def _settle_pending(
    job: MigrationJob,
    source: MemoryProvider | None,
    file_items: Sequence[MemoryItem] | None,
    target: MemoryProvider,
    scope: ProviderScope,
) -> None:
    """Replay the keys of a batch whose reply was lost, and ledger what they answer.

    Each pending item is read again (from the source, or from the import's own file)
    and sent under its original key: a key the provider applied answers that first
    outcome and id, and one it never applied is written now, so either way the ledger
    then names every item this job put there.
    """
    by_id = {i.id: i for i in file_items or ()}
    items = []
    for item_id in job.pending_ids:
        item = by_id.get(item_id) if source is None else await source.get(scope, item_id)
        if item is None:
            # Gone from the source, so its key cannot be replayed: keep the obligation.
            raise ProviderError(
                f"{item_id} was sent but never acknowledged and is no longer in the "
                "source, so whether the target holds it is unknown"
            )
        items.append(item)
    caps = await asyncio.to_thread(target.capabilities)
    planned = [_plan_item(job, item, caps) for item in items]
    unsendable = [p.item.id for p in planned if p.write is None]
    if unsendable:
        # Its plan now sends nothing, so its key cannot be replayed: keep the obligation.
        raise ProviderError(
            f"{unsendable[0]} was sent but never acknowledged and would not be sent "
            "again, so whether the target holds it is unknown"
        )
    entries = [(p.write, _key(job, p)) for p in planned if p.write is not None]
    if entries:
        results = await target.put_batch(scope, entries)
        ledger = [
            {
                "origin_id": p.origin_id,
                "target_id": result.id,
                "outcome": result.outcome,
                "text_sha256": text_sha256(write.text),
            }
            for p, (write, _k), result in zip(planned, entries, results)
        ]
        await asyncio.to_thread(_append_ledger, job.id, ledger)
    job.pending_ids = []
    await asyncio.to_thread(save, job)


async def rollback(
    job: MigrationJob,
    target: MemoryProvider,
    scope: ProviderScope,
    *,
    source: MemoryProvider | None = None,
    file_items: Sequence[MemoryItem] | None = None,
    notify: Notify = lambda job: None,
) -> MigrationJob:
    """Delete from *target* exactly the items this job's ledger marks ``inserted``.

    A batch whose reply was lost is settled first, from *source* for a move or from
    *file_items* (the same file, read again) for an import; without them it refuses.
    """
    if job.pending_ids and (file_items is None if job.from_file else source is None):
        # The state stays as it was, so nothing is lost by refusing.
        job.error = (
            f"{len(job.pending_ids)} writes were sent but never acknowledged, so their "
            "ids are unknown until they are settled."
        )
        await asyncio.to_thread(save, job)
        notify(job)
        return job
    _hold(job)
    job.state = "rolling_back"
    try:
        await asyncio.to_thread(save, job)
    except BaseException:
        _release(job)
        raise
    notify(job)
    try:
        if job.pending_ids:
            await _settle_pending(job, None if job.from_file else source, file_items, target, scope)
        seen: set[str] = set()
        for entry in await asyncio.to_thread(read_ledger, job.id):
            target_id = str(entry.get("target_id") or "")
            if entry.get("outcome") != "inserted" or not target_id or target_id in seen:
                continue
            if job.cancelled:
                # The ledger is kept, so undoing again finishes the rest.
                raise ProviderError("the undo was cancelled; undo again to finish it")
            seen.add(target_id)
            result = await target.delete(scope, target_id)
            if result.outcome == "deleted":
                job.rolled_back += 1
            elif result.outcome != "unchanged":
                # Still there: the undo is not done, and can be tried again.
                raise ProviderError(
                    f"the target would not delete {target_id} ({result.reason or result.outcome})"
                )
        job.state = "rolled_back"
        job.error = ""
    except ProviderNotSupported:
        job.state = "rollback_failed"
        job.error = "the target cannot delete items, so the move cannot be rolled back"
    except ProviderError as exc:
        # Undo again to finish: deleting an id already gone answers ``unchanged``.
        job.state = "rollback_failed"
        job.error = f"rollback stopped: {exc.message}"
    except Exception:  # noqa: BLE001 - an undo failure is reported, never raised into the loop
        logger.warning("memory move job %s undo failed", job.id, exc_info=True)
        job.state = "rollback_failed"
        job.error = "the undo failed"
    job.finished_at = time.time()
    try:
        await asyncio.to_thread(save, job)
        notify(job)
    finally:
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
    cut = 0
    by_kind: dict[str, int] = {}
    async for item in iter_items(provider, scope, status="all" if include_retired else None):
        record = item.to_wire()
        if was_cut(item.text):
            # Read past its kind's cap, so this line is not the whole item: marked, and
            # an import refuses it (``refused:too_long``) instead of restoring it short.
            record["cut"] = True
            cut += 1
        if item.origin is None:
            # An item first written here names this provider and store as its origin,
            # so an import elsewhere keeps where it came from instead of the file.
            record["origin"] = Origin(provider_id, scope.store_id, item.id).to_wire()
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
    trailer = {
        "trailer": True,
        "items": count,
        "by_kind": by_kind,
        "cut": cut,
        "sha256": digest.hexdigest(),
    }
    yield json.dumps(trailer) + "\n"


def parse_import(raw: bytes) -> tuple[list[MemoryItem], list[str], list[str]]:
    """``(items, errors, warnings)`` from a ``kirocrew-memory/1`` document.

    A trailer that does not match the item lines is an error (the file was cut or
    changed); a file with no trailer imports with a warning.
    """
    if len(raw) > MAX_IMPORT_BYTES:
        return [], ["the import is larger than 32 MB; export it in pages"], []
    # Split on "\n" only: a record's text may hold U+2028 or U+0085, which
    # ``str.splitlines`` would also break on.
    lines = list(io.StringIO(raw.decode("utf-8", errors="replace"), newline="\n"))
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
            text = record.get("text") if isinstance(record, dict) else None
            kind = record.get("kind") if isinstance(record, dict) else None
            if isinstance(text, str) and isinstance(kind, str) and len(text) > text_cap(kind):
                # Never import a shortened copy: the line is refused by name.
                errors.append(f"line {number}: text is longer than {text_cap(kind)} characters")
                continue
            if isinstance(record, dict) and (
                record.get("cut") is True or (isinstance(text, str) and was_cut(text))
            ):
                errors.append(f"line {number}: the export holds only part of this item")
                continue
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
