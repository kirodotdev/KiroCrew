"""Turn-context recall, answered by a small pool of child processes.

Why this is not a thread
------------------------
A turn's context build asks the V1 vector store three questions: the semantic
facts that match the message, the episodic fragments, and the lessons. Each ranks
tens of thousands of rows in pure Python plus numpy, so each holds the
interpreter lock for most of its run. Every session in the gateway builds its
context in the same interpreter, so ``N`` concurrent builds take ``N`` times one
build, however many cores the host has: on a store with 54k facts, 27k episodes
and 12k lessons, one recall set is about 1.2 s alone and the 100th of 100
concurrent builds waits minutes. Raising the thread count shares the same lock.
Only separate interpreters add cores.

``memory.recall_pool_processes`` (default ``0``, off) is the number of child
processes that answer these three reads. Each child opens its own
``VectorMemoryStore`` on the same ``memory.db`` and keeps its own resident
scoring sets, so a warm recall costs the child, not the gateway.

What moves, what stays
----------------------
The child runs the SAME store methods the gateway would have run
(``get_semantic_context``, ``get_episodic_context``, ``get_lessons_context``);
nothing is re-implemented. It gets plain data (a query string, an optional query
vector, the caps) and returns the rendered text. These stay in the gateway:

* the query embedding: the child asks over its pipe and the gateway answers from
  its own embedder, so no second copy of the model loads;
* the ``last_accessed_at`` touch an episodic hit makes: the child reports the row
  ids and the gateway writes them, through the same debounce, at most every few
  minutes (``_TOUCH_FLUSH_SECS``). A read in a child never commits, and the
  gateway's commit for the accesses moves SQLite's ``data_version`` for every child
  once per flush instead of once per read;
* the writes: consolidation, the dashboard, the CLI.

Staying current
---------------
A child's resident sets are valid for one in-process generation and one SQLite
``data_version``. A commit by any other connection (the gateway's writer, the
consolidation write child, the CLI) moves ``data_version`` for every child, and the
next recall in that child rebuilds its sets, exactly as the gateway's own would
after a CLI write. Reads therefore see every committed write, as before. The cost
of a write is one rebuild per child, which a later change can shrink by patching
the resident set instead of discarding it.

No recall is lost
-----------------
A child that cannot start, is short of memory, crashes, hangs past its deadline,
or reports an error makes the caller run the same read in the gateway, as it
always did. A hung or crashed child is killed first. A child that keeps failing is
not respawned for ``1..300`` s (exponential), and callers go straight to the
gateway path meanwhile. Reads are idempotent, so the rerun is safe.

Memory
------
Each child holds its own resident sets: roughly the store's embeddings plus the
episodic matrix plus the lesson rows. ``memory.recall_pool_worker_max_mb`` bounds
one child: no child starts while less than that is free on the host, and a child
whose resident size passes it after a job exits and is replaced by the next job.
The existing ``_semantic_scoring_max_bytes`` clamp (1/16 of free memory, 1 GiB at
most) still applies inside each child.

Spawn, never fork
-----------------
This is a ``subprocess`` launch of a fresh interpreter
(``python -P -m kiro_crew.recall_pool``), like
:mod:`kiro_crew.consolidation_write_child` and :mod:`kiro_crew.embeddings_child`:
only paths, numbers and JSON cross the pipe. The child exits on stdin EOF or when
its parent changes, and is stopped after sitting idle.

``python -m kiro_crew.recall_pool`` is the child's entry point; nothing else runs it.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from kiro_crew.consolidation_write_child import (
    ChildUnavailable,
    _ChildState,
    _Conn,
    _Rpc,
    _watch_parent,
)
from kiro_crew.embeddings_child import _child_creationflags

logger = logging.getLogger("kiro_crew.vector_memory")

_CHILD_MODULE = "kiro_crew.recall_pool"
#: Longest one recall may run in a child, embedding round trips included. A warm
#: recall set takes about a second; a cold child builds three resident sets first.
JOB_TIMEOUT_SECS = 120.0
#: Longest a caller waits for a free child. Callers are already admitted by the
#: embed pool (8 at a time), so this only bounds a stall; past it the caller reads here.
QUEUE_WAIT_SECS = 60.0
#: Longest a child may take to import and report ready.
_READY_TIMEOUT_SECS = 60.0
#: A pool whose children have all run no job for this long is stopped.
IDLE_STOP_SECS = 900.0
_BACKOFF_BASE_SECS = 1.0
_BACKOFF_CAP_SECS = 300.0
#: Upper bound on ``memory.recall_pool_processes``.
MAX_POOL_PROCESSES = 8

#: The pool's clock for backoff, idleness and the settings reuse. A name of its own so a test
#: can move it without rebinding ``time.monotonic`` for everything else. The per-job deadline
#: uses the real clock, since it is raced against a real pipe.
_now = time.monotonic

_IN_CHILD = False
#: Set in the environment of every child. ``python -m`` runs this file as ``__main__``, so a
#: flag set by ``main()`` would live on that copy and not on the ``kiro_crew.recall_pool``
#: the store methods import; the environment reaches both.
_CHILD_ENV = "KIROCREW_RECALL_POOL_CHILD"

#: The reads a child answers, keyed by the ``kind`` of the job.
KINDS = ("semantic", "episodic", "lessons")


def in_child() -> bool:
    """Whether this process is a pool child (which must never offload again)."""
    return _IN_CHILD or os.environ.get(_CHILD_ENV) == "1"


# ── gateway side ──


class _Slot:
    """One child process and the bookkeeping the pool keeps for it."""

    def __init__(self) -> None:
        self.conn: _Conn | None = None
        self.last_job_end = 0.0


class RecallPool:
    """The gateway's handle on the recall children.

    ``size`` children at most, each answering one job at a time; callers wait on a
    free-slot queue. A child is spawned on first use, stopped after
    :data:`IDLE_STOP_SECS` idle, and replaced when it recycles itself.
    """

    def __init__(
        self,
        size: int,
        max_rss_mb: int,
        *,
        job_timeout_secs: float = JOB_TIMEOUT_SECS,
        queue_wait_secs: float = QUEUE_WAIT_SECS,
        ready_timeout_secs: float = _READY_TIMEOUT_SECS,
        idle_stop_secs: float = IDLE_STOP_SECS,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        free_memory_mb: Callable[[], float] | None = None,
    ) -> None:
        self.size = max(1, min(int(size), MAX_POOL_PROCESSES))
        self.max_rss_mb = int(max_rss_mb)
        self._job_timeout = job_timeout_secs
        self._queue_wait = queue_wait_secs
        self._ready_timeout = ready_timeout_secs
        self._idle_stop = idle_stop_secs
        self._argv = argv
        self._env = env
        self._free_memory_mb = free_memory_mb or _free_memory_mb
        self._slots = [_Slot() for _ in range(self.size)]
        self._free: "queue.Queue[_Slot]" = queue.Queue()
        for slot in self._slots:
            self._free.put(slot)
        self._state_lock = threading.Lock()
        self._failures = 0
        self._down_until = 0.0
        self._next_id = 0
        self._closed = False
        self._idle_timer: threading.Timer | None = None
        self.spawns = 0

    # lifecycle

    def _spawn(self) -> _Conn:
        from kiro_crew import platform_compat

        free_mb = self._free_memory_mb()
        if 0 < free_mb < self.max_rss_mb:
            raise ChildUnavailable(
                f"only {free_mb:.0f} MB free, a recall child may need {self.max_rss_mb} MB"
            )
        # -P: never put the working directory on the child's sys.path, so a
        # kiro_crew/ directory in the gateway's cwd cannot shadow the package.
        argv = self._argv or platform_compat.isolated_python_argv("-P", "-m", _CHILD_MODULE)
        env = {
            **(os.environ if self._env is None else self._env),
            _CHILD_ENV: "1",
            "KIROCREW_RECALL_POOL_MAX_RSS_MB": str(self.max_rss_mb),
        }
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, this interpreter
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            close_fds=True,
            env=env,
            creationflags=_child_creationflags(),
        )
        conn = _Conn(proc)
        try:
            ready = conn.inbox.get(timeout=self._ready_timeout)
        except queue.Empty:
            ready = None
        if not ready or ready.get("op") != "ready":
            conn.close()
            raise ChildUnavailable("recall child did not report ready")
        self.spawns += 1
        return conn

    def _fail(self, slot: _Slot, why: str) -> ChildUnavailable:
        """Kill the slot's child, start the backoff and return the error to raise."""
        conn, slot.conn = slot.conn, None
        if conn is not None:
            conn.close()
        with self._state_lock:
            self._failures += 1
            # Clamp the exponent: the counter only resets on a completed job.
            delay = min(_BACKOFF_CAP_SECS, _BACKOFF_BASE_SECS * 2 ** min(self._failures - 1, 16))
            self._down_until = _now() + delay
        logger.warning(
            "Recall pool child failed (%s); the gateway reads instead, and the pool is "
            "not retried for %.0f s",
            why,
            delay,
        )
        return ChildUnavailable(why)

    def pids(self) -> list[int]:
        return [s.conn.proc.pid for s in self._slots if s.conn is not None]

    def close(self) -> None:
        """Stop every child. Idempotent; never blocks on a pipe."""
        with self._state_lock:
            self._closed = True
            timer, self._idle_timer = self._idle_timer, None
            conns = []
            for slot in self._slots:
                conn, slot.conn = slot.conn, None
                if conn is not None:
                    conns.append(conn)
        if timer is not None:
            timer.cancel()
        for conn in conns:
            conn.close()

    def _arm_idle_stop(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            timer = threading.Timer(self._idle_stop, self._stop_idle)
            timer.daemon = True
            self._idle_timer = timer
        timer.start()

    def _stop_idle(self) -> None:
        # Only a slot nobody is using can be stopped: take it off the free queue.
        taken: list[_Slot] = []
        try:
            while True:
                taken.append(self._free.get_nowait())
        except queue.Empty:
            pass
        try:
            now = _now()
            for slot in taken:
                if slot.conn is not None and now - slot.last_job_end >= self._idle_stop:
                    conn, slot.conn = slot.conn, None
                    conn.close()
        finally:
            for slot in taken:
                self._free.put(slot)

    # jobs

    def run_job(
        self, job: dict[str, Any], *, serve_embed: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        """Run *job* in a child and return its result frame.

        The frame is ``{"ok": True, "text": ...}`` or ``{"ok": False, ...}``.
        Raises :class:`ChildUnavailable` when no child could finish the job.
        """
        if self._closed:
            raise ChildUnavailable("recall pool is closed")
        if _now() < self._down_until:
            raise ChildUnavailable("recall pool is backing off")
        try:
            slot = self._free.get(timeout=self._queue_wait)
        except queue.Empty:
            raise ChildUnavailable("recall pool stayed busy") from None
        try:
            return self._run_on(slot, job, serve_embed)
        finally:
            slot.last_job_end = _now()
            self._free.put(slot)
            self._arm_idle_stop()

    def _run_on(
        self,
        slot: _Slot,
        job: dict[str, Any],
        serve_embed: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        if _now() < self._down_until:
            raise ChildUnavailable("recall pool is backing off")
        conn = slot.conn
        if conn is None or conn.proc.poll() is not None:
            if conn is not None:
                conn.close()
                slot.conn = None
            try:
                conn = self._spawn()
            except ChildUnavailable as exc:
                raise self._fail(slot, str(exc)) from exc
            except OSError as exc:
                raise self._fail(slot, f"could not start: {exc}") from exc
            with self._state_lock:
                closed = self._closed
                if not closed:
                    slot.conn = conn  # published under the lock close() takes
            if closed:
                conn.close()
                raise ChildUnavailable("recall pool is closed")
        with self._state_lock:
            self._next_id += 1
            job_id = self._next_id
        conn.send({**job, "op": "job", "id": job_id})
        deadline = time.monotonic() + self._job_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._fail(slot, f"job exceeded {self._job_timeout:.0f} s")
            try:
                frame = conn.inbox.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                if conn.proc.poll() is not None:
                    raise self._fail(slot, f"exited with code {conn.proc.returncode}")
                continue
            if frame is None:
                raise self._fail(slot, f"exited with code {conn.proc.poll()}")
            op = frame.get("op")
            if op == "embed":
                try:
                    reply = serve_embed(frame)
                except Exception:  # noqa: BLE001 - an embed that raised is an embed that failed
                    logger.debug("Embedding for the recall child failed", exc_info=True)
                    reply = {"vector": None}
                conn.send({"op": "embed_result", **reply})
            elif op == "result" and frame.get("id") == job_id:
                with self._state_lock:
                    self._failures = 0
                    self._down_until = 0.0
                if frame.get("recycle"):
                    # The child passed its memory limit and is exiting on its own:
                    # not a failure, the next job starts a fresh one.
                    slot.conn = None
                    conn.close()
                return frame


def _free_memory_mb() -> float:
    """Host memory available to a new child, in MB; 0 when it cannot be read."""
    try:
        from kiro_crew.subagent import _available_memory_gb

        return float(_available_memory_gb()) * 1024.0
    except Exception:  # noqa: BLE001 - an unreadable probe never blocks a child
        return 0.0


_pool: RecallPool | None = None
_pool_lock = threading.Lock()


#: How long a read of ``memory.*`` is reused. Loading the config costs about 2 ms and three
#: reads of one turn's context would each pay it, so a switch change reaches reads within
#: this long.
_SETTINGS_TTL_SECS = 1.0
_settings_cache: tuple[float, tuple[int, int]] | None = None


def settings() -> tuple[int, int]:
    """``(processes, worker_max_mb)`` from ``memory.*``; ``(0, 0)`` when off."""
    global _settings_cache
    cached = _settings_cache
    now = _now()
    if cached is not None and now - cached[0] < _SETTINGS_TTL_SECS:
        return cached[1]
    value = _read_settings()
    _settings_cache = (now, value)
    return value


def _read_settings() -> tuple[int, int]:
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        memory = KiroCrewConfig.load().memory
        processes = getattr(memory, "recall_pool_processes", 0)
        max_mb = getattr(memory, "recall_pool_worker_max_mb", 3072)
        if not isinstance(processes, int) or isinstance(processes, bool) or processes < 1:
            return 0, 0
        if not isinstance(max_mb, int) or isinstance(max_mb, bool) or max_mb < 1:
            max_mb = 3072
        return min(processes, MAX_POOL_PROCESSES), max_mb
    except Exception:  # noqa: BLE001 - an unreadable config keeps the default (off)
        logger.debug("Could not read memory.recall_pool_processes", exc_info=True)
        return 0, 0


def pool_enabled() -> bool:
    """Whether recall reads may go to the children (not in a child, switch on)."""
    if in_child():
        return False
    if settings()[0] < 1:
        _retire_if_off()
        return False
    return True


def get_pool(size: int, max_rss_mb: int) -> RecallPool:
    """The process-wide pool, rebuilt when the configured size or limit changed."""
    global _pool
    with _pool_lock:
        old = _pool
        if old is not None and (old.size, old.max_rss_mb) != (
            min(size, MAX_POOL_PROCESSES),
            max_rss_mb,
        ):
            old.close()
            old = None
        if old is None:
            old = _pool = RecallPool(size, max_rss_mb)
            atexit.register(shutdown_pool)
        return old


def shutdown_pool() -> None:
    """Stop the children, if any were started, and write the buffered accesses."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.close()
    flush_touches()


#: How long accesses reported by children wait before the gateway writes them. Each write is
#: a commit, which moves SQLite's ``data_version`` for every child and makes each rebuild its
#: resident sets (several seconds on a large store); ``last_accessed_at`` only feeds recency
#: reporting, so a few minutes of delay costs nothing and a rebuild per read would cost a lot.
_TOUCH_FLUSH_SECS = 300.0
_touch_lock = threading.Lock()
_pending_touches: dict[int, tuple[Any, set[str]]] = {}
_touch_timer: threading.Timer | None = None


def _queue_touches(store: Any, ids: list[str]) -> None:
    """Hold *ids* (episodic rows a child's read returned) until the next flush."""
    global _touch_timer
    with _touch_lock:
        _pending_touches.setdefault(id(store), (store, set()))[1].update(ids)
        if _touch_timer is None:
            _touch_timer = threading.Timer(_TOUCH_FLUSH_SECS, flush_touches)
            _touch_timer.daemon = True
            _touch_timer.start()


def flush_touches() -> None:
    """Write the buffered accesses through each store's own debounce, in this process."""
    global _touch_timer
    with _touch_lock:
        pending = list(_pending_touches.values())
        _pending_touches.clear()
        timer, _touch_timer = _touch_timer, None
    if timer is not None:
        timer.cancel()
    for store, ids in pending:
        try:
            store._touch_last_accessed(sorted(ids))
        except Exception:  # noqa: BLE001 - recency reporting never fails a recall
            logger.debug("Could not record episodic access", exc_info=True)


def _retire_if_off() -> None:
    """Stop running children once the setting is back to 0, instead of at their idle stop."""
    if _pool is not None:
        shutdown_pool()


def _eligible(store: Any) -> tuple[str, int | None] | None:
    """``(db_path, embedding_dim)`` a child should open, or ``None`` to read here."""
    from kiro_crew.vector_memory import VectorMemoryStore

    if type(store) is not VectorMemoryStore:
        return None
    if getattr(store, "algorithm_version", "v1") == "v2":
        return None
    db_path = getattr(store, "_db_path", None)
    if not isinstance(db_path, Path) or not db_path.exists():
        return None
    dim = getattr(store, "_embedding_dim", None)
    return str(db_path), dim if isinstance(dim, int) else None


def _serve_embed(store: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    from kiro_crew.consolidation_write_offload import _serve_embed as serve

    return serve(store)


def offload(store: Any, kind: str, args: dict[str, Any]) -> str | None:
    """The text a child rendered for *kind*, or ``None`` when the caller must read here.

    ``None`` is the answer whenever the pool is off, the store is not one a child can
    open, or the child could not finish: the caller then runs the same method itself.
    """
    if in_child() or kind not in KINDS:
        return None
    processes, max_mb = settings()
    if processes < 1:
        _retire_if_off()
        return None
    target = _eligible(store)
    if target is None:
        return None
    db_path, dim = target
    job = {
        "kind": kind,
        "db_path": db_path,
        "embedding_dim": dim,
        "embed_available": store.embed_fn is not None,
        "space_generation": store.space_generation,
        "args": args,
    }
    try:
        json.dumps(job)
    except (TypeError, ValueError):
        logger.warning("Recall job for %s is not plain data; reading here", kind)
        return None
    try:
        frame = get_pool(processes, max_mb).run_job(job, serve_embed=_serve_embed(store))
    except ChildUnavailable:
        return None
    if not frame.get("ok"):
        logger.warning(
            "Recall pool child reported an error for %s (%s); reading here",
            kind,
            frame.get("message"),
        )
        return None
    _replay_records(frame)
    touched = frame.get("touched")
    if isinstance(touched, list) and touched:
        _queue_touches(store, [str(t) for t in touched])
    text = frame.get("text")
    return text if isinstance(text, str) else None


def _replay_records(frame: dict[str, Any]) -> None:
    """Hand the per-turn collectors of this build what the child's read recorded.

    The lesson renderers write an omission record and an injection receipt into
    ContextVar collectors that the turn opened; those do not cross a process, so the
    child gathers them and this puts them where the in-process read would have.
    """
    from kiro_crew import context_omissions, injection_receipts

    try:
        omissions = frame.get("omissions")
        if isinstance(omissions, dict):
            for kind in omissions.get("cleared") or ():
                if kind in context_omissions.KINDS:
                    context_omissions.clear(kind)
            recorded = omissions.get("set")
            if isinstance(recorded, dict):
                context_omissions.replay(
                    {
                        k: v
                        for k, v in recorded.items()
                        if k in context_omissions.KINDS and isinstance(v, dict)
                    }
                )
        receipts = frame.get("receipts")
        if isinstance(receipts, dict):
            injection_receipts.replay(receipts)
    except Exception:  # noqa: BLE001 - telemetry never fails a recall
        logger.debug("Could not replay a recall child's records", exc_info=True)


def lessons_args(
    store: Any,
    query_text: str,
    cap: int,
    project_dir: str | Path | None,
    recall_query: Any,
    background: bool,
    hard_cap: int,
    directive_budget: int,
    experience_budget: int,
) -> dict[str, Any]:
    """Job arguments for a lessons read.

    A query vector from a space the store has left is dropped here, in the gateway,
    where the embedder is known, with the same outcome the in-process read gives: a
    background read ranks by keyword, an explicit read raises.
    """
    query: dict[str, Any] | None = None
    if recall_query is not None:
        from kiro_crew.vector_memory_runtime.embedding import _RecallQuery, _RecallSpaceChanged

        try:
            with store._db_lock:
                store._check_recall_query(recall_query)
        except _RecallSpaceChanged:
            if not background:
                raise
            recall_query = _RecallQuery(None, None, None)
        query = {
            "vector": None if recall_query.vector is None else list(recall_query.vector),
            "generation": recall_query.generation,
            "signature": recall_query.signature,
        }
    return {
        "query_text": query_text,
        "cap": cap,
        "project_dir": None if project_dir is None else str(project_dir),
        "recall_query": query,
        "background": background,
        "hard_cap": hard_cap,
        "directive_budget": directive_budget,
        "experience_budget": experience_budget,
    }


# ── child side ──


def _rss_mb() -> float | None:
    """This process's current resident set in MB, or ``None`` where it cannot be read."""
    from kiro_crew import platform_compat

    rss = platform_compat.proc_rss_bytes()
    return rss / (1024.0 * 1024.0) if rss > 0 else None


class _RecallState(_ChildState):
    """The write child's store cache, minus its FAISS rebuild.

    A kept store follows ``config.json`` before each job (the gateway's watcher does
    that there). The FAISS view is not rebuilt: a recall child answers episodic reads
    from the numpy scoring tier, which needs no index.
    """

    @staticmethod
    def _refresh(store: Any) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        # ``reconfigure`` drops the resident episodic set whether or not anything changed,
        # so a kept store re-applies the settings only when ``memory.*`` is not what it
        # applied last.
        fingerprint = repr(cfg.memory)
        if getattr(store, "_recall_pool_config", None) != fingerprint:
            store.reconfigure(cfg)
            store._recall_pool_config = fingerprint


def _prepare(state: _ChildState, job: dict[str, Any]) -> tuple[Any, list[str]]:
    """The child's store for *job*, bound to the gateway's embedder, and a touch sink."""
    store = state.store_for(job["db_path"], job.get("embedding_dim"))
    if not hasattr(store, "_recall_pool_config"):
        from kiro_crew.config.loader import KiroCrewConfig

        store._recall_pool_config = repr(KiroCrewConfig.load().memory)
    state.bind(store, job)
    touched: list[str] = []
    # The gateway records the access, through its own debounce, on its own connection.
    store._touch_last_accessed = lambda ids: touched.extend(ids)
    return store, touched


#: Marks a kind the read has not touched: ``record`` and ``clear`` both replace or drop
#: the entry, so an entry still ``is`` its seed was left alone and one that is gone was cleared.
_OMISSION_SEED: dict[str, Any] = {}
_RECEIPT_SEED: dict[str, Any] = {}


def _run_job(state: _ChildState, job: dict[str, Any]) -> dict[str, Any]:
    """Run one read under the two per-turn collectors and return its text and their records."""
    from kiro_crew import context_omissions, injection_receipts

    with context_omissions.collect() as omissions, injection_receipts.collect() as receipts:
        omissions.update({k: _OMISSION_SEED for k in context_omissions.KINDS})
        receipts.update({k: _RECEIPT_SEED for k in injection_receipts.KINDS})
        reply = _read(state, job)
    if reply.get("ok"):
        reply["omissions"] = {
            "set": {k: v for k, v in omissions.items() if v is not _OMISSION_SEED},
            "cleared": [k for k in context_omissions.KINDS if k not in omissions],
        }
        reply["receipts"] = {k: v for k, v in receipts.items() if v is not _RECEIPT_SEED}
    return reply


def _read(state: _ChildState, job: dict[str, Any]) -> dict[str, Any]:
    from kiro_crew.vector_memory_runtime.embedding import _RecallQuery

    store, touched = _prepare(state, job)
    args = job.get("args") or {}
    kind = job.get("kind")
    try:
        if kind == "semantic":
            text = store.get_semantic_context(
                args.get("query_text", ""),
                args.get("cap", 1500),
                facts_only=bool(args.get("facts_only")),
            )
        elif kind == "episodic":
            text = store.get_episodic_context(
                args.get("query_embedding"), args.get("query_text", ""), args.get("cap", 3000)
            )
        elif kind == "lessons":
            raw = args.get("recall_query")
            query = (
                None
                if raw is None
                else _RecallQuery(raw.get("vector"), raw.get("generation"), raw.get("signature"))
            )
            text = store.get_lessons_context(
                args.get("query_text", ""),
                args.get("cap", 0),
                args.get("project_dir"),
                recall_query=query,
                background=bool(args.get("background")),
                hard_cap=args.get("hard_cap", 0),
                directive_budget=args.get("directive_budget", 0),
                experience_budget=args.get("experience_budget", 0),
            )
        else:
            return {"ok": False, "message": f"unknown job kind {kind!r}"}
    except Exception as exc:  # noqa: BLE001 - relayed; the gateway reruns the read itself
        logger.warning("Recall job failed", exc_info=True)
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "text": text, "touched": touched}


def _max_rss_mb() -> float:
    try:
        return float(os.environ.get("KIROCREW_RECALL_POOL_MAX_RSS_MB", "0"))
    except ValueError:
        return 0.0


def main() -> int:
    """Child entry: answer recall jobs from stdin until EOF or the gateway goes away."""
    global _IN_CHILD
    _IN_CHILD = True
    os.environ[_CHILD_ENV] = "1"
    # The protocol owns the original stdout; anything else that prints goes to stderr.
    out = os.fdopen(os.dup(1), "wb")
    try:
        os.dup2(2, 1)
        sys.stdout = sys.stderr
    except OSError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="kirocrew recall child: %(message)s"
    )
    threading.Thread(
        target=_watch_parent, args=(os.getppid(),), name="kc-recall-parent-watch", daemon=True
    ).start()
    return _serve(sys.stdin.buffer, out)


def _serve(stdin: Any, out: Any) -> int:
    rpc = _Rpc(stdin, out)
    state = _RecallState(rpc)
    limit = _max_rss_mb()
    rpc.send({"op": "ready", "pid": os.getpid()})
    while True:
        try:
            msg = rpc.next()
        except ValueError:
            return 1
        if msg is None or msg.get("op") == "exit":
            break
        if msg.get("op") != "job":
            continue
        try:
            reply = _run_job(state, msg)
        except Exception as exc:  # noqa: BLE001 - setup failed (store open)
            logger.warning("Recall job could not start", exc_info=True)
            reply = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        rss = _rss_mb()
        recycle = bool(limit and rss is not None and rss > limit)
        if recycle:
            reply["recycle"] = True
        rpc.send({"op": "result", "id": msg.get("id"), **reply})
        if recycle:
            logger.info("resident set %.0f MB passed the %.0f MB limit; exiting", rss, limit)
            break
    for store in state.stores.values():
        try:
            store.close()
        except Exception:  # noqa: BLE001 - exiting anyway
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
