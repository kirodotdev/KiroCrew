"""Background run registry for dynamic workflows (M6.1).

Lets a workflow run **outlive a single request** and be addressed by ``run_id``
from anywhere — chat MCP tools, the Workflows dashboard tab, and result-to-chat
injection all share one registry. Without this, a run is just a synchronous
``WorkflowRunner.run()`` call that nobody can monitor, cancel, or fetch later.

A ``RunHandle`` holds the live state of one run: status, the event list as it
grows, the final result/error, the originating session (for result injection),
and the ``asyncio.Task`` driving it (for cancellation). The registry schedules
runs on the running event loop and tracks them in-memory (bounded LRU) — the
durable journal for resume is M6.6.

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
STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# Max concurrently-tracked runs kept in memory (oldest finished evicted first).
DEFAULT_MAX_RUNS = 200

# Callback fired (once) when a run reaches a terminal state. Used by M6.4 to
# inject the result back into the originating chat session.
#   on_done(run_id, snapshot: dict) -> None
OnDoneFn = Callable[[str, dict], None]

# Callback fired for each event as it is recorded (M6.5 live WS push).
#   on_event(run_id, event_json: dict) -> None
OnEventFn = Callable[[str, dict], None]


def _int_key(k: Any) -> Any:
    """agent_results is keyed by call_index (int); JSON object keys are strings,
    so coerce back to int on load (keep non-numeric keys as-is, defensively)."""
    try:
        return int(k)
    except (TypeError, ValueError):
        return k


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
    args: dict = field(default_factory=dict)
    agent_results: dict = field(default_factory=dict)  # call_index → result (resume cache)

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
            "event_count": len(self.events),
            # Live progress for the compact list view (sidebar + chat indicator):
            # the current phase title and most recent narrator line, derived from
            # the event stream so no extra plumbing is needed.
            "phase": self._current_phase(),
            "last_log": self._last_log(),
        }
        if include_events:
            snap["events"] = [e.to_json() for e in self.events]
            # The authored/executed script — included only in the FULL snapshot
            # (detail view) so the UI can show "View source", edit, and rerun. Kept
            # out of the compact list to keep that payload small.
            snap["source"] = self.source
        return snap

    def _current_phase(self) -> str:
        """Title of the most recent ``phase_started`` event (live progress)."""
        for e in reversed(self.events):
            if e.type == "phase_started":
                return e.data.get("title", "")
        return ""

    def _last_log(self) -> str:
        """Most recent narrator ``log`` message (live progress)."""
        for e in reversed(self.events):
            if e.type == "log":
                return e.data.get("message", "")
        return ""

    # --- durable persistence (FIX-21): full JSON form for the on-disk store ---
    # Distinct from ``snapshot`` (a UI view): this round-trips the COMPLETE run so a
    # restored handle supports list/result/rerun/restart-subtree across restarts.
    # JSON only (BSC12 — never pickle). The asyncio.Task is intentionally dropped.
    def to_store_json(self) -> dict:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "author": self.author,
            "session_key": self.session_key,
            "source": self.source,
            "args": self.args,
            "agent_results": {str(k): v for k, v in self.agent_results.items()},
            "events": [e.to_json() for e in self.events],
        }

    @classmethod
    def from_store_json(cls, obj: dict) -> "RunHandle":
        status = obj.get("status", STATUS_FINISHED)
        error = obj.get("error")
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
            source=obj.get("source", ""),
            args=obj.get("args") or {},
            agent_results={_int_key(k): v for k, v in (obj.get("agent_results") or {}).items()},
        )


class RunRegistry:
    """In-memory, loop-affine registry of background workflow runs.

    When a ``store`` is provided (FIX-21) the registry mirrors each run to disk so
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
        try:
            self._store.save(handle.run_id, handle.to_store_json())
        except Exception:  # noqa: BLE001 - persistence must never break a run
            pass

    # --- lifecycle ---
    def register(self, handle: RunHandle) -> None:
        self._runs[handle.run_id] = handle
        self._runs.move_to_end(handle.run_id)
        self._evict()
        self._persist(handle)

    def _evict(self) -> None:
        # Drop oldest TERMINAL runs first; never evict a still-running run.
        while len(self._runs) > self._max_runs:
            for rid, h in list(self._runs.items()):
                if h.status != STATUS_RUNNING:
                    del self._runs[rid]
                    if self._store is not None:
                        try:
                            self._store.delete(rid)
                        except Exception:  # noqa: BLE001
                            pass
                    break
            else:
                break  # all running — keep them

    def record_event(self, run_id: str, event: WorkflowEvent) -> None:
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
        if self._store is not None and len(h.events) % self._save_every == 0:
            self._persist(h)

    def mark_terminal(
        self, run_id: str, status: str, *, result: Any = None, error: Optional[str] = None
    ) -> None:
        h = self._runs.get(run_id)
        if h is None or h.status != STATUS_RUNNING:
            return  # idempotent: only the first terminal transition counts
        h.status = status
        h.result = result
        h.error = error
        # Durable flush on terminal state — the final result + full stream + the
        # authored script are now complete and reusable across restarts.
        self._persist(h)
        if self._on_done is not None:
            try:
                self._on_done(run_id, h.snapshot(include_events=False))
            except Exception:  # noqa: BLE001
                pass

    def persist(self, run_id: str) -> None:
        """Public hook: force-persist a run (e.g. after its source is set mid-run)."""
        h = self._runs.get(run_id)
        if h is not None:
            self._persist(h)

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
    args: Optional[dict] = None,
) -> str:
    """Schedule a workflow run on the loop, register a handle, return its run_id.

    ``run_coro_factory(record)`` returns the awaitable that drives the run; it is
    called with a ``record(event)`` sink so events land in the handle (and fan out
    to ``on_event``) as they happen. The driver should return ``(result, status,
    error, agent_results)`` — or raise, which is captured as a failed run.
    ``source``/``args`` are stored on the handle so a resume/restart-subtree can
    re-run the same script (M6.6).
    """
    handle = RunHandle(
        run_id=run_id,
        name=name,
        author=author,
        session_key=session_key,
        source=source,
        args=args or {},
    )
    registry.register(handle)

    def record(event: WorkflowEvent) -> None:
        registry.record_event(run_id, event)

    async def _drive() -> None:
        try:
            result, status, error, agent_results = await run_coro_factory(record)
            handle.agent_results = agent_results or {}
            registry.mark_terminal(run_id, status, result=result, error=error)
        except asyncio.CancelledError:
            registry.mark_terminal(run_id, STATUS_CANCELLED, error="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - capture, never crash the loop
            registry.mark_terminal(run_id, STATUS_FAILED, error=repr(exc))

    handle.task = asyncio.ensure_future(_drive())
    return run_id
