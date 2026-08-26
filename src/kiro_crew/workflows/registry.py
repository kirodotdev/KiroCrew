"""Background run registry for dynamic workflows.

Lets a workflow run **outlive a single request** and be addressed by ``run_id``
from anywhere — chat MCP tools, the Workflows dashboard tab, and result-to-chat
injection all share one registry. Without this, a run is just a synchronous
``WorkflowRunner.run()`` call that nobody can monitor, cancel, or fetch later.

A ``RunHandle`` holds the live state of one run: status, the event list as it
grows, the final result/error, the originating session (for result injection),
and the ``asyncio.Task`` driving it (for cancellation). The registry schedules
runs on the running event loop and tracks them in-memory (bounded LRU).

Thread-safety: the registry is loop-affine — all mutation happens on the gateway
event loop, so no locks are needed for the in-process store. Snapshots returned
to callers are plain dicts (JSON-serializable, never the live objects).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import WorkflowEvent

# Run lifecycle states (also the values surfaced to the UI / MCP status tool).
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
ACTIVE_STATUSES = frozenset({STATUS_RUNNING, STATUS_PAUSED})
_TERMINAL_EVENT_TYPES = frozenset({"run_finished", "run_failed", "run_cancelled"})

# Max concurrently-tracked runs kept in memory (oldest finished evicted first).
DEFAULT_MAX_RUNS = 200

# Callback fired (once) when a run reaches a terminal state, to inject the
# result back into the originating chat session.
#   on_done(run_id, snapshot: dict) -> None
OnDoneFn = Callable[[str, dict], None]

# Callback fired for each event as it is recorded (live WS push).
#   on_event(run_id, event_json: dict) -> None
OnEventFn = Callable[[str, dict], None]


def _int_key(k: Any) -> Any:
    """agent_results is keyed by call_index (int); JSON object keys are strings,
    so coerce back to int on load (keep non-numeric keys as-is, defensively)."""
    try:
        return int(k)
    except (TypeError, ValueError):
        return k


def _str_keyed(mapping: dict) -> dict:
    """JSON-safe view of a call_index-keyed map, ordered by key.

    Keys are stringified (JSON object keys must be strings) and sorted as strings
    rather than ints, so a restored handle whose key failed int-coercion (see
    ``_int_key``) can't raise on a mixed-type sort.
    """
    return {str(k): v for k, v in sorted(mapping.items(), key=lambda kv: str(kv[0]))}


@dataclass
class RunHandle:
    """Live state of one background workflow run."""

    run_id: str
    name: str
    status: str = STATUS_RUNNING
    events: list[WorkflowEvent] = field(default_factory=list)
    result: Any = None
    error: Optional[str] = None
    author: str = ""
    session_key: str = ""  # originating chat session (for result injection)
    task: Optional["asyncio.Task[Any]"] = None
    source: str = ""  # the script (so a resume/restart can re-run it)
    # False when the durable store had to redact source bytes (or provenance is
    # unknown). Such source remains usable for display/rerun, but not promotion.
    source_is_original: bool = True
    args: dict = field(default_factory=dict)
    agent_results: dict = field(default_factory=dict)  # call_index → result (resume cache)
    # call_index → bounded reason that call failed. Kept next to agent_results so a
    # missing payload always comes with an explanation instead of a bare ok=False.
    agent_errors: dict = field(default_factory=dict)
    source_format: str = "python"
    driver: str = "workflow"
    task_id: str = ""
    capabilities: tuple[str, ...] = ()
    completion_injection: bool = True
    workflow_id: str = ""
    workflow_slug: str = ""
    workflow_revision: int = 0
    derived_from_workflow_id: str = ""
    derived_from_revision: int = 0
    # Off-loop persistence coordination belongs to the run identity itself. It
    # is live-only state and therefore intentionally absent from snapshots.
    _persist_lock: Optional[asyncio.Lock] = field(
        default=None, init=False, repr=False, compare=False
    )
    _persist_generation: int = field(default=0, init=False, repr=False, compare=False)

    def snapshot(self, *, include_events: bool = True) -> dict:
        """JSON-serializable view of this run (never leaks the asyncio.Task)."""
        snap: dict[str, Any] = {
            "run_id": self.run_id,
            "name": self.name,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "author": self.author,
            "session_key": self.session_key,
            "source_format": self.source_format,
            "driver": self.driver,
            "task_id": self.task_id,
            "capabilities": list(self.capabilities),
            "completion_injection": self.completion_injection,
            "workflow_id": self.workflow_id,
            "workflow_slug": self.workflow_slug,
            "workflow_revision": self.workflow_revision,
            "derived_from": (
                {
                    "workflow_id": self.derived_from_workflow_id,
                    "revision": self.derived_from_revision,
                }
                if self.derived_from_workflow_id and self.derived_from_revision
                else None
            ),
            "event_count": len(self.events),
            # Live progress for the compact list view (sidebar + chat indicator):
            # the current phase title and most recent narrator line, derived from
            # the event stream so no extra plumbing is needed.
            "phase": self._current_phase(),
            "last_log": self._last_log(),
        }
        # Work that outlived a run which ENDED WITHOUT a usable return value
        # (ceiling / cancel / crash). Keyed on STATUS, not on ``result is None``:
        # a run can finish and legitimately return None (a script with no return,
        # or whose last statement is a failed agent call), and a still-running run
        # has no result yet — neither lost anything, so reporting partials for them
        # would both mislead the reader and re-send every payload on every poll.
        # The COUNTS ride in the compact view so a completion message can say the
        # work survived; the payloads themselves ride only in the detail view.
        ended_without_result = self.status not in ACTIVE_STATUSES | {STATUS_FINISHED}
        partials = self.agent_results if ended_without_result else {}
        if partials:
            snap["partial_result_count"] = len(partials)
        if self.agent_errors:
            snap["agent_error_count"] = len(self.agent_errors)
        if include_events:
            snap["events"] = [e.to_json() for e in self.events]
            # The authored/executed script — included only in the FULL snapshot
            # (detail view) so the UI can show "View source", edit, and rerun. Kept
            # out of the compact list to keep that payload small.
            snap["source"] = self.source
            if partials:
                snap["partial_results"] = _str_keyed(partials)
            if self.agent_errors:
                snap["agent_errors"] = _str_keyed(self.agent_errors)
        return snap

    def _latest(self, event_type: str, key: str) -> str:
        """``data[key]`` of the most recent event of ``event_type``, else ``""``."""
        for e in reversed(self.events):
            if e.type == event_type:
                return e.data.get(key, "")
        return ""

    def _current_phase(self) -> str:
        """Title of the most recent ``phase_started`` event (live progress)."""
        return self._latest("phase_started", "title")

    def _last_log(self) -> str:
        """Most recent narrator ``log`` message (live progress)."""
        return self._latest("log", "message")

    # --- durable persistence: full JSON form for the on-disk store ---
    # Distinct from ``snapshot`` (a UI view): this round-trips the COMPLETE run so a
    # restored handle supports list/result/rerun/restart-subtree across restarts.
    # JSON only (never pickle). The asyncio.Task is intentionally dropped.
    def to_store_json(self) -> dict:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "author": self.author,
            "session_key": self.session_key,
            "source_format": self.source_format,
            "driver": self.driver,
            "task_id": self.task_id,
            "capabilities": list(self.capabilities),
            "completion_injection": self.completion_injection,
            "workflow_id": self.workflow_id,
            "workflow_slug": self.workflow_slug,
            "workflow_revision": self.workflow_revision,
            "derived_from": (
                {
                    "workflow_id": self.derived_from_workflow_id,
                    "revision": self.derived_from_revision,
                }
                if self.derived_from_workflow_id and self.derived_from_revision
                else None
            ),
            "source": self.source,
            "source_is_original": self.source_is_original,
            "args": self.args,
            "agent_results": {str(k): v for k, v in self.agent_results.items()},
            "agent_errors": {str(k): v for k, v in self.agent_errors.items()},
            "events": [e.to_json() for e in self.events],
        }

    @classmethod
    def from_store_json(cls, obj: dict) -> "RunHandle":
        status = obj.get("status", STATUS_FINISHED)
        error = obj.get("error")
        derived_from = obj.get("derived_from") or {}
        # A run that was still "running" when the gateway died can never resume in
        # this process — mark it failed (interrupted) so the registry isn't stuck
        # with a zombie and eviction can reclaim it. Give it a clear error if the
        # stored record had none (it never reached a terminal transition).
        if status == STATUS_RUNNING:
            status = STATUS_FAILED
            error = error or "interrupted: gateway restarted while running"
        return cls(
            run_id=obj["run_id"],
            name=obj.get("name", ""),
            status=status,
            events=[WorkflowEvent.from_json(e) for e in obj.get("events", [])],
            result=obj.get("result"),
            error=error,
            author=obj.get("author", ""),
            session_key=obj.get("session_key", ""),
            source_format=obj.get("source_format", "python"),
            driver=obj.get("driver", "workflow"),
            task_id=obj.get("task_id", ""),
            capabilities=tuple(obj.get("capabilities") or ()),
            completion_injection=obj.get("completion_injection", True) is True,
            workflow_id=obj.get("workflow_id", ""),
            workflow_slug=obj.get("workflow_slug", ""),
            workflow_revision=int(obj.get("workflow_revision") or 0),
            derived_from_workflow_id=str(derived_from.get("workflow_id") or ""),
            derived_from_revision=int(derived_from.get("revision") or 0),
            source=obj.get("source", ""),
            # Legacy records lack provenance and therefore fail closed.
            source_is_original=obj.get("source_is_original") is True,
            args=obj.get("args") or {},
            agent_results={_int_key(k): v for k, v in (obj.get("agent_results") or {}).items()},
            agent_errors={_int_key(k): v for k, v in (obj.get("agent_errors") or {}).items()},
        )


class RunRegistry:
    """In-memory, loop-affine registry of background workflow runs.

    When a ``store`` is provided the registry mirrors each run to disk so
    runs survive a gateway restart: it saves on register, on terminal transition,
    and (throttled) as events accumulate; deletes on eviction; and ``load_persisted``
    rehydrates everything on startup. The in-memory ``OrderedDict`` stays the source
    of truth — the store is a best-effort durable mirror.
    """

    def __init__(self, *, max_runs: int = DEFAULT_MAX_RUNS, store: Any = None) -> None:
        self._runs: "OrderedDict[str, RunHandle]" = OrderedDict()
        self._max_runs = max_runs
        self._on_done: Optional[OnDoneFn] = None
        self._on_event: Optional[OnEventFn] = None
        self._store = store
        # Persist live runs at most every N events (terminal state always flushes),
        # so a long fan-out run doesn't write its file on every single event.
        self._save_every = 5

    # --- wiring (set by the gateway at startup) ---
    def set_on_done(self, cb: Optional[OnDoneFn]) -> None:
        self._on_done = cb

    def set_on_event(self, cb: Optional[OnEventFn]) -> None:
        self._on_event = cb

    def _persist(self, handle: RunHandle) -> None:
        if self._store is None:
            return
        self._persist_snapshot(handle.run_id, handle.to_store_json())

    def _persist_snapshot(self, run_id: str, payload: dict[str, Any]) -> None:
        """Write a caller-built snapshot without touching loop-affine state."""
        try:
            self._store.save(run_id, payload)
        except Exception:  # noqa: BLE001 - persistence must never break a run
            pass

    # --- lifecycle ---
    def register(self, handle: RunHandle, *, persist: bool = True) -> None:
        self._runs[handle.run_id] = handle
        self._runs.move_to_end(handle.run_id)
        self._evict()
        if persist:
            self._persist(handle)

    def _evict(self) -> None:
        # Drop oldest TERMINAL runs first; never evict a still-running run.
        while len(self._runs) > self._max_runs:
            for rid, h in list(self._runs.items()):
                if h.status not in ACTIVE_STATUSES:
                    del self._runs[rid]
                    if self._store is not None:
                        try:
                            self._store.delete(rid)
                        except Exception:  # noqa: BLE001
                            pass
                    break
            else:
                break  # all running — keep them

    def record_event(self, run_id: str, event: WorkflowEvent, *, persist: bool = True) -> None:
        h = self._runs.get(run_id)
        if h is None:
            return
        h.events.append(event)
        if self._on_event is not None:
            try:
                self._on_event(run_id, event.to_json())
            except Exception:  # noqa: BLE001 - a bad subscriber must not break the run
                pass
        # Throttled durable checkpoint while running, so a restart mid-run keeps
        # most of the event stream (and the authored source, recorded at start).
        if persist and self._store is not None and len(h.events) % self._save_every == 0:
            self._persist(h)

    async def record_event_async(self, run_id: str, event: WorkflowEvent) -> None:
        """Record an event and move any throttled checkpoint write off-loop."""
        self.record_event(run_id, event, persist=False)
        handle = self._runs.get(run_id)
        if (
            handle is not None
            and self._store is not None
            and len(handle.events) % self._save_every == 0
        ):
            await self.persist_async(run_id)

    def record_agent_result(
        self,
        run_id: str,
        call_index: int,
        *,
        result: Any,
        ok: bool = True,
        error: str = "",
    ) -> None:
        """Checkpoint ONE settled agent call onto the handle, as the call returns.

        Called by the runner after each ``ctx.agent()`` call. Landing each result on
        the handle as the call returns — rather than only in ``_drive`` after the
        whole run finishes — is what makes the wall-clock ceiling survivable: a run
        killed by the ceiling still holds every payload it has already paid for, the
        terminal paths read them back, and ``mark_terminal`` flushes the completed
        record to disk.

        Deliberately does NOT force a store write of its own. ``store.save``
        re-serializes and re-redacts the ENTIRE run record synchronously on the
        gateway event loop, so writing once per agent call would add an O(N) stall
        per call over a record that grows with every payload. Disk durability
        instead rides the write this module already performs — ``record_event``
        flushes every ``_save_every`` events, and each agent call emits two events —
        plus the guaranteed flush at ``mark_terminal``. The trade is explicit: a
        hard gateway kill can lose the newest payloads that no flush has covered
        yet, exactly as it can already lose the newest events.
        """
        h = self._runs.get(run_id)
        if h is None:
            return
        h.agent_results[call_index] = result
        if error or not ok:
            h.agent_errors[call_index] = error or "agent call failed (no reason recorded)"

    def _transition_terminal(
        self, run_id: str, status: str, *, result: Any = None, error: Optional[str] = None
    ) -> Optional[RunHandle]:
        h = self._runs.get(run_id)
        if h is None or h.status not in ACTIVE_STATUSES:
            return None  # idempotent: only the first terminal transition counts
        h.status = status
        h.result = result
        h.error = error
        return h

    def _notify_done(self, run_id: str, handle: RunHandle) -> None:
        if handle.completion_injection and self._on_done is not None:
            try:
                self._on_done(run_id, handle.snapshot(include_events=False))
            except Exception:  # noqa: BLE001
                pass

    def mark_terminal(
        self, run_id: str, status: str, *, result: Any = None, error: Optional[str] = None
    ) -> None:
        h = self._transition_terminal(run_id, status, result=result, error=error)
        if h is None:
            return
        # Durable flush on terminal state — the final result + full stream + the
        # authored script are now complete and reusable across restarts.
        self._persist(h)
        self._notify_done(run_id, h)

    async def mark_terminal_async(
        self, run_id: str, status: str, *, result: Any = None, error: Optional[str] = None
    ) -> None:
        """Mark terminal and await the durable flush without blocking the loop."""
        h = self._transition_terminal(run_id, status, result=result, error=error)
        if h is None:
            return
        await self.persist_async(run_id)
        self._notify_done(run_id, h)

    def persist(self, run_id: str) -> None:
        """Public hook: force-persist a run (e.g. after its source is set mid-run)."""
        h = self._runs.get(run_id)
        if h is not None:
            self._persist(h)

    async def persist_async(self, run_id: str) -> None:
        """Snapshot on-loop and serialize the durable mirror off-loop per run."""
        handle = self._runs.get(run_id)
        if handle is None or self._store is None:
            return
        payload = handle.to_store_json()
        handle._persist_generation += 1
        generation = handle._persist_generation
        if handle._persist_lock is None:
            handle._persist_lock = asyncio.Lock()
        lock = handle._persist_lock

        async def _write_latest() -> None:
            async with lock:
                if generation != handle._persist_generation or self._runs.get(run_id) is not handle:
                    return
                await asyncio.to_thread(self._persist_snapshot, run_id, payload)

        write_task = asyncio.create_task(_write_latest())
        try:
            await asyncio.shield(write_task)
        except BaseException:
            # A worker write cannot be stopped after dispatch. Keep the per-run
            # lock until it settles so cancellation cannot let another checkpoint
            # overlap the same temporary file or overtake the durable snapshot.
            await asyncio.shield(write_task)
            raise

    def delete(self, run_id: str) -> bool:
        """Forget one run and its durable record."""
        handle = self._runs.pop(run_id, None)
        if handle is None:
            return False
        if self._store is not None:
            try:
                self._store.delete(run_id)
            except Exception:  # noqa: BLE001 - in-memory deletion stays authoritative
                pass
        return True

    async def delete_async(self, run_id: str) -> bool:
        """Forget one run while keeping durable deletion off the event loop."""
        handle = self._runs.pop(run_id, None)
        if handle is None:
            return False
        if self._store is not None:
            handle._persist_generation += 1
            if handle._persist_lock is None:
                handle._persist_lock = asyncio.Lock()
            lock = handle._persist_lock

            async def _delete_after_writes() -> None:
                async with lock:
                    try:
                        await asyncio.to_thread(self._store.delete, run_id)
                    except Exception:  # noqa: BLE001 - memory deletion is authoritative
                        pass

            delete_task = asyncio.create_task(_delete_after_writes())
            try:
                await asyncio.shield(delete_task)
            except BaseException:
                await asyncio.shield(delete_task)
                raise
        return True

    def set_status(self, run_id: str, status: str, *, persist: bool = True) -> bool:
        """Move a host-driven run between non-terminal lifecycle states."""
        if status not in ACTIVE_STATUSES:
            raise ValueError(f"unsupported active workflow status: {status}")
        handle = self._runs.get(run_id)
        if handle is None or handle.status not in ACTIVE_STATUSES:
            return False
        handle.status = status
        if persist:
            self._persist(handle)
        return True

    def reopen_host_run(self, run_id: str, *, task_id: str = "", persist: bool = True) -> bool:
        """Reopen a persisted host run while preserving its stable identity.

        A TaskRunner project can survive a gateway restart or be retried after a
        terminal outcome. The host remains authoritative for that lifecycle, so
        its shared projection reconciles onto the existing run instead of
        minting a second history entry.
        """
        handle = self._runs.get(run_id)
        if handle is None or handle.driver == "workflow":
            return False
        if task_id and handle.task_id and handle.task_id != task_id:
            return False
        # A reopened host run continues one journal identity. Its prior terminal
        # marker described the interrupted attempt, but leaving it in place would
        # put a terminal event in the middle of the retried stream.
        if handle.events and handle.events[-1].type in _TERMINAL_EVENT_TYPES:
            handle.events.pop()
        handle.status = STATUS_RUNNING
        handle.result = None
        handle.error = None
        if task_id:
            handle.task_id = task_id
        if persist:
            self._persist(handle)
        return True

    def load_persisted(self) -> int:
        """Rehydrate runs from the store on startup. Returns the count loaded.

        Idempotent-ish: only fills run_ids not already in memory. Runs that were
        mid-flight when the gateway died are demoted to failed (see
        ``RunHandle.from_store_json``) — they can't resume in a new process.
        """
        if self._store is None:
            return 0
        loaded = 0
        try:
            for obj in self._store.load_all():
                rid = obj.get("run_id")
                if not rid or rid in self._runs:
                    continue
                try:
                    handle = RunHandle.from_store_json(obj)
                except Exception:  # noqa: BLE001 - skip a bad record
                    continue
                self._runs[rid] = handle
                loaded += 1
        except Exception:  # noqa: BLE001
            pass
        # Honor the bounded-LRU ceiling: a store with more records than max_runs
        # must not leave the in-memory registry over its documented bound.
        self._evict()
        return loaded

    # --- queries ---
    def get(self, run_id: str) -> Optional[RunHandle]:
        return self._runs.get(run_id)

    def status(self, run_id: str, *, include_events: bool = False) -> Optional[dict]:
        h = self._runs.get(run_id)
        return h.snapshot(include_events=include_events) if h else None

    def list(self) -> list[dict]:
        # Newest first; compact (no event bodies) for the list view.
        return [h.snapshot(include_events=False) for h in reversed(self._runs.values())]

    async def cancel(self, run_id: str) -> bool:
        h = self._runs.get(run_id)
        if h is None or h.status != STATUS_RUNNING or h.task is None:
            return False
        h.task.cancel()
        return True


async def start_background_run(
    registry: RunRegistry,
    run_coro_factory: Callable[[Callable[[WorkflowEvent], None]], Awaitable[Any]],
    *,
    run_id: str,
    name: str,
    author: str = "",
    session_key: str = "",
    source: str = "",
    source_is_original: bool = True,
    args: Optional[dict] = None,
    workflow_id: str = "",
    workflow_slug: str = "",
    workflow_revision: int = 0,
) -> str:
    """Schedule a workflow run on the loop, register a handle, return its run_id.

    ``run_coro_factory(record)`` returns the awaitable that drives the run; it is
    called with a ``record(event)`` sink so events land in the handle (and fan out
    to ``on_event``) as they happen. The driver should return ``(result, status,
    error, agent_results)`` — or raise, which is captured as a failed run.
    ``source``/``args`` are stored on the handle so a resume/restart-subtree can
    re-run the same script.
    """
    handle = RunHandle(
        run_id=run_id,
        name=name,
        author=author,
        session_key=session_key,
        source=source,
        source_is_original=source_is_original,
        args=args or {},
        workflow_id=workflow_id,
        workflow_slug=workflow_slug,
        workflow_revision=workflow_revision,
    )
    registry.register(handle)

    def record(event: WorkflowEvent) -> None:
        registry.record_event(run_id, event)

    async def _drive() -> None:
        try:
            result, status, error, agent_results = await run_coro_factory(record)
            # MERGE, never replace: per-call checkpoints already landed on the handle
            # while the run was executing, and a terminal path that hands back a
            # partial (or empty) map must not erase them.
            if agent_results:
                handle.agent_results.update(agent_results)
            registry.mark_terminal(run_id, status, result=result, error=error)
        except asyncio.CancelledError:
            registry.mark_terminal(run_id, STATUS_CANCELLED, error="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - capture, never crash the loop
            registry.mark_terminal(run_id, STATUS_FAILED, error=repr(exc))

    handle.task = asyncio.ensure_future(_drive())
    return run_id
