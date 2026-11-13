"""Copy memory between providers, and export or import it as JSONL.

A copy reads the source with ``list`` and writes each item to the target with
``put``, keyed ``crew-copy:<from>:<item id>``, so a rerun writes nothing twice. It
deletes nothing on either side, and it reports counts per outcome and per skipped
kind. A dry run lists and counts without writing.

The export format is JSONL ``kirocrew-memory/1``: a header line
``{"format": "kirocrew-memory/1", "provider": ..., "store": ..., "exported_at": ...}``
then one item per line in the contract's wire form. An import reads the same lines
and writes them through ``put`` with key ``crew-import:<sha256 of the line>``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.memory_provider.contract import (
    LESSON_KINDS,
    MemoryItem,
    MemoryProvider,
    NewItem,
    ProviderError,
    ProviderScope,
    item_from_wire,
)

logger = logging.getLogger("kiro_crew.memory_provider")

EXPORT_FORMAT = "kirocrew-memory/1"
#: A copy or import walks at most this many items; past it the job says it stopped.
MAX_ITEMS = 100_000
MAX_IMPORT_BYTES = 32 * 1024 * 1024
_JOBS_MAX = 32


@dataclass
class CopyJob:
    id: str
    source: str
    target: str
    store: str
    dry_run: bool
    state: str = "running"
    listed: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    skipped_kinds: dict[str, int] = field(default_factory=dict)
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from": self.source,
            "to": self.target,
            "store": self.store,
            "dry_run": self.dry_run,
            "state": self.state,
            "listed": self.listed,
            "outcomes": dict(self.outcomes),
            "skipped_kinds": dict(self.skipped_kinds),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


_jobs_lock = threading.Lock()
_jobs: dict[str, CopyJob] = {}


def _remember(job: CopyJob) -> None:
    with _jobs_lock:
        _jobs[job.id] = job
        while len(_jobs) > _JOBS_MAX:
            del _jobs[next(iter(_jobs))]


def get_job(job_id: str) -> CopyJob | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def cancel_job(job_id: str) -> bool:
    job = get_job(job_id)
    if job is None or job.state != "running":
        return False
    job.cancelled = True
    return True


def new_item(item: MemoryItem) -> NewItem:
    return NewItem(
        kind=item.kind,
        text=item.text,
        title=item.title,
        scopes=item.scopes,
        source=item.source,
        status=item.status,
        meta=dict(item.meta),
    )


async def iter_items(provider: MemoryProvider, scope: ProviderScope) -> AsyncIterator[MemoryItem]:
    """Every item *provider* lists for *scope*, page by page; past ``MAX_ITEMS`` it raises."""
    cursor: str | None = None
    seen = 0
    while True:
        page = await provider.list(scope, cursor, None, None)
        for item in page.items:
            if seen >= MAX_ITEMS:
                # More remain past the cap: a copy or export never ends as if whole.
                raise ProviderError(f"stopped after {MAX_ITEMS} items; the rest were not read")
            yield item
            seen += 1
        cursor = page.cursor
        if not cursor:
            return


def _count(table: dict[str, int], key: str) -> None:
    table[key] = table.get(key, 0) + 1


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


async def run_copy(
    job: CopyJob,
    source: MemoryProvider,
    target: MemoryProvider,
    scope: ProviderScope,
) -> CopyJob:
    """Copy *source* into *target* for *scope*. Never deletes. Fills *job* as it goes."""
    try:
        caps = await asyncio.to_thread(target.describe)
        kinds, read_only = caps.kinds, caps.read_only
        if read_only and not job.dry_run:
            raise ProviderError("the target provider only reads")
        async for item in iter_items(source, scope):
            if job.cancelled:
                job.state = "cancelled"
                break
            job.listed += 1
            if item.kind not in kinds:
                _count(job.skipped_kinds, item.kind)
                continue
            if job.dry_run:
                _count(job.outcomes, "would_copy")
                continue
            new = new_item(item)
            if screen_refuses(new):
                _count(job.outcomes, "refused:content_screen")
                continue
            key = f"crew-copy:{job.source}:{item.id}"
            try:
                result = await target.put(scope, new, key)
            except ProviderError as exc:
                _count(job.outcomes, f"error:{exc.code}")
                continue
            _count(job.outcomes, result.outcome)
        else:
            job.state = "done"
    except ProviderError as exc:
        job.state = "failed"
        job.error = exc.message
    except Exception:  # noqa: BLE001 - a job failure is reported, never raised into the loop
        logger.warning("memory copy job %s failed", job.id, exc_info=True)
        job.state = "failed"
        job.error = "the copy failed"
    job.finished_at = time.time()
    return job


def start_copy(
    source_id: str,
    target_id: str,
    source: MemoryProvider,
    target: MemoryProvider,
    scope: ProviderScope,
    *,
    dry_run: bool,
) -> CopyJob:
    job = CopyJob(
        id=secrets.token_hex(8),
        source=source_id,
        target=target_id,
        store=scope.store_id,
        dry_run=dry_run,
    )
    _remember(job)
    task = asyncio.get_running_loop().create_task(run_copy(job, source, target, scope))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


_tasks: set[asyncio.Task] = set()


async def export_lines(
    provider: MemoryProvider,
    provider_id: str,
    scope: ProviderScope,
    scrub: Callable[[dict[str, Any]], dict[str, Any]] = lambda record: record,
) -> AsyncIterator[str]:
    """The export, one JSON record per line; *scrub* runs on each record before encoding."""
    header = {
        "format": EXPORT_FORMAT,
        "provider": provider_id,
        "store": scope.store_id,
        "exported_at": time.time(),
    }
    yield json.dumps(scrub(header), ensure_ascii=False) + "\n"
    async for item in iter_items(provider, scope):
        yield json.dumps(scrub(item.to_wire()), ensure_ascii=False) + "\n"


def parse_import(raw: bytes) -> tuple[list[tuple[str, NewItem]], list[str]]:
    """``(items, errors)`` from a ``kirocrew-memory/1`` document, each with its line key."""
    if len(raw) > MAX_IMPORT_BYTES:
        return [], ["the import is larger than 32 MB"]
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if not lines:
        return [], ["the import is empty"]
    try:
        header = json.loads(lines[0])
    except ValueError:
        header = None
    if not isinstance(header, dict) or header.get("format") != EXPORT_FORMAT:
        return [], [f"the first line must name the {EXPORT_FORMAT} format"]
    items: list[tuple[str, NewItem]] = []
    errors: list[str] = []
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        if len(items) >= MAX_ITEMS:
            errors.append(f"stopped after {MAX_ITEMS} items")
            break
        try:
            item = item_from_wire(json.loads(line))
        except (ValueError, ProviderError) as exc:
            errors.append(f"line {number}: {getattr(exc, 'message', str(exc))}"[:200])
            continue
        key = "crew-import:" + hashlib.sha256(line.encode("utf-8")).hexdigest()[:32]
        items.append((key, new_item(item)))
    return items, errors


async def run_import(
    target: MemoryProvider,
    scope: ProviderScope,
    items: list[tuple[str, NewItem]],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    outcomes: dict[str, int] = {}
    skipped: dict[str, int] = {}
    kinds = (await asyncio.to_thread(target.describe)).kinds
    for key, item in items:
        if item.kind not in kinds:
            _count(skipped, item.kind)
            continue
        if screen_refuses(item):
            _count(outcomes, "refused:content_screen")
            continue
        if dry_run:
            _count(outcomes, "would_import")
            continue
        try:
            result = await target.put(scope, item, key)
        except ProviderError as exc:
            _count(outcomes, f"error:{exc.code}")
            continue
        _count(outcomes, result.outcome)
    return {"outcomes": outcomes, "skipped_kinds": skipped, "dry_run": dry_run}
