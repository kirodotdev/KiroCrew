"""History consolidation's vector-memory writes, as their own process.

Why this is not a thread
------------------------
A consolidation pass writes the facts, episodes and lessons the model extracted:
it checks each item against the rows already stored (dedupe, supersede, the
lesson tier fence) and then writes it. That is pure-Python CPU plus SQLite, and it
ran on ``mc-membg`` threads inside the gateway. Every session's turn-context build
(semantic, episodic and lessons recall) runs in the same interpreter, so the two
compete for one GIL: a thread pool cannot fix that, because the contended resource
is the interpreter lock and not a core. Only a separate interpreter can.

``memory.consolidation_write_child_process`` (default off) moves exactly those
writes into a child. The gateway keeps the read path, the model call, the lease,
the history marker and everything else of the pass.

What the child does
-------------------
The child runs the SAME functions the gateway would have run
(``HistoryConsolidator._write_structured_memory`` and the vector branch of
``_save_lessons``), against its own ``VectorMemoryStore`` on the same
``memory.db`` and its own ``ConversationLog`` on the same sessions directory.
Nothing is re-implemented, so the two paths cannot drift. SQLite (WAL) already
serves several processes on one file, and the CLI and the MCP server write the same
store while the gateway runs.

The publication hold stays what it was. The child takes the same transcript lock
(an ``flock`` on a sidecar file, so it serializes across processes), re-reads the
lease token under it and reads ``memory.persistence_enabled`` /
``memory.writes_enabled`` before the run's first commit, per write. The gateway
sends the token and the run's committed latch with the job, and the child streams
a ``committed`` frame when the latch flips so the gateway's copy follows.

Embedding stays in the gateway. The child asks over the pipe and the gateway runs
``store._try_embed`` on its own embedder (the in-process model or the embedding
child), then returns the vector with the embedding-space token that write checks.
No second copy of the model loads.

No write is lost
----------------
A child that cannot start, crashes, hangs past its deadline or reports an error
that is not a publication fence makes the caller run the job in the gateway, as it
always did. A hung or crashed child is killed first so two writers never overlap,
and SQLite rolls back its open transaction. A rerun after a partial job is safe for
the keyed writes (semantic rows, lessons): the same key and value is the row
already there. An episodic row written before the crash is skipped on the rerun by
the store's similarity dedupe; with no embedder available the dedupe cannot run, so
that rare case can leave one duplicate episode, which is preferred to losing it.
A child that keeps failing is not respawned for ``1..300`` s (exponential), and
callers go straight to the gateway path meanwhile.

Spawn, never fork
-----------------
The gateway carries 100+ threads. This is a ``subprocess`` launch of a fresh
interpreter (``python -P -m kiro_crew.consolidation_write_child``), like
:mod:`kiro_crew.embeddings_child`: only paths, numbers and JSON cross the pipe.
The child runs at a lower CPU priority, exits on stdin EOF or when its parent
changes, and is stopped after sitting idle.

``python -m kiro_crew.consolidation_write_child`` is the child's entry point;
nothing else runs it.
"""

from __future__ import annotations

import atexit
import dataclasses
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any, Callable

from kiro_crew.embeddings_child import (
    _MAX_RESPONSE_BYTES,
    _PARENT_POLL_SECS,
    _child_creationflags,
    _kill,
    _parent_gone,
    _write_msg,
)

logger = logging.getLogger("kiro_crew.history")

_CHILD_MODULE = "kiro_crew.consolidation_write_child"
#: Longest one job may run from the moment the child starts it. The pass's own
#: ceiling on inline embedding is 60 s; the rest is local SQLite work.
JOB_TIMEOUT_SECS = 600.0
#: Longest a caller waits for the child to finish a job queued ahead of it. The
#: gateway keeps renewing the consolidation lease while the caller waits, and a
#: caller that gives up writes in the gateway, so this only bounds the wait.
QUEUE_WAIT_SECS = 120.0
#: Longest the child may take to import and report ready.
_READY_TIMEOUT_SECS = 60.0
#: A child that has run no job for this long is stopped; the next job respawns it.
IDLE_STOP_SECS = 900.0
#: Respawn backoff after a failure: 1 s doubling to 300 s, reset by a job that completes.
_BACKOFF_BASE_SECS = 1.0
_BACKOFF_CAP_SECS = 300.0
#: ``os.nice`` increment in the child. Windows uses BELOW_NORMAL_PRIORITY_CLASS instead.
_CHILD_NICE = 10
#: Open stores the child keeps, oldest closed first.
_CHILD_STORE_CAP = 8


class ChildUnavailable(RuntimeError):
    """The child could not complete the job: not running, crashed, timed out or busy.

    The caller runs the job in the gateway.
    """


# ── gateway side ──


class _Conn:
    """One running child: its process, a writer queue and a reader queue.

    Every write goes through the writer thread, so no caller ever blocks on a full
    pipe. The reader thread parses frames into ``inbox`` and puts ``None`` when the
    child's stdout closes.
    """

    def __init__(self, proc: "subprocess.Popen[bytes]") -> None:
        self.proc = proc
        self.outbox: "queue.SimpleQueue[bytes | None]" = queue.SimpleQueue()
        self.inbox: "queue.SimpleQueue[dict[str, Any] | None]" = queue.SimpleQueue()
        self.writer = threading.Thread(target=self._write_loop, name="kc-cwc-write", daemon=True)
        self.reader = threading.Thread(target=self._read_loop, name="kc-cwc-read", daemon=True)
        self.writer.start()
        self.reader.start()

    def send(self, msg: dict[str, Any]) -> None:
        self.outbox.put(json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n")

    def _write_loop(self) -> None:
        stdin = self.proc.stdin
        try:
            while True:
                line = self.outbox.get()
                if line is None or stdin is None:
                    break
                stdin.write(line)
                stdin.flush()
        except (OSError, ValueError):
            _kill(self.proc)  # the reader sees EOF
        finally:
            try:
                if stdin is not None:
                    stdin.close()
            except (OSError, ValueError):
                pass

    def _read_loop(self) -> None:
        stream = self.proc.stdout
        assert stream is not None
        try:
            while True:
                raw = stream.readline(_MAX_RESPONSE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > _MAX_RESPONSE_BYTES:
                    logger.warning("Consolidation write child wrote an oversized line; stopping it")
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    logger.warning("Consolidation write child wrote a malformed line; stopping it")
                    break
                if isinstance(msg, dict):
                    self.inbox.put(msg)
        except (OSError, ValueError):
            pass
        finally:
            self.inbox.put(None)

    def close(self) -> None:
        self.outbox.put(None)
        _kill(self.proc)


class WriteChild:
    """The gateway's handle on the consolidation write child.

    One child, one job at a time (callers queue on ``_job_lock``). The child is
    spawned on the first job and stopped after :data:`IDLE_STOP_SECS` idle.
    """

    def __init__(
        self,
        *,
        job_timeout_secs: float = JOB_TIMEOUT_SECS,
        queue_wait_secs: float = QUEUE_WAIT_SECS,
        ready_timeout_secs: float = _READY_TIMEOUT_SECS,
        idle_stop_secs: float = IDLE_STOP_SECS,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._job_timeout = job_timeout_secs
        self._queue_wait = queue_wait_secs
        self._ready_timeout = ready_timeout_secs
        self._idle_stop = idle_stop_secs
        self._argv = argv
        self._env = env
        self._job_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._conn: _Conn | None = None
        self._failures = 0
        self._down_until = 0.0
        self._last_job_end = 0.0
        self._next_id = 0
        self._closed = False
        self._idle_timer: threading.Timer | None = None
        self.spawns = 0

    # lifecycle

    def _spawn(self) -> _Conn:
        from kiro_crew import platform_compat

        # -P: never put the working directory on the child's sys.path, so a
        # kiro_crew/ directory in the gateway's cwd cannot shadow the package.
        argv = self._argv or platform_compat.isolated_python_argv("-P", "-m", _CHILD_MODULE)
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, this interpreter
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            close_fds=True,
            env=self._env,
            creationflags=_child_creationflags(),
        )
        conn = _Conn(proc)
        try:
            ready = conn.inbox.get(timeout=self._ready_timeout)
        except queue.Empty:
            ready = None
        if not ready or ready.get("op") != "ready":
            conn.close()
            raise ChildUnavailable("consolidation write child did not report ready")
        self.spawns += 1
        return conn

    def _fail(self, conn: "_Conn | None", why: str) -> ChildUnavailable:
        """Kill *conn*, start the backoff and return the error for the caller to raise."""
        if conn is not None:
            conn.close()
        with self._state_lock:
            if self._conn is conn:
                self._conn = None
            self._failures += 1
            # Clamp the exponent: the counter only resets on a completed job, so it
            # can grow without bound while the child keeps failing to start.
            delay = min(_BACKOFF_CAP_SECS, _BACKOFF_BASE_SECS * 2 ** min(self._failures - 1, 16))
            self._down_until = time.monotonic() + delay
        logger.warning(
            "Consolidation write child failed (%s); the gateway writes instead, "
            "and the child is not retried for %.0f s",
            why,
            delay,
        )
        return ChildUnavailable(why)

    def pid(self) -> int | None:
        conn = self._conn
        return conn.proc.pid if conn is not None else None

    def close(self) -> None:
        """Stop the child. Idempotent; never blocks on a pipe."""
        with self._state_lock:
            self._closed = True
            conn, self._conn = self._conn, None
            timer, self._idle_timer = self._idle_timer, None
        if timer is not None:
            timer.cancel()
        if conn is not None:
            conn.close()

    def _arm_idle_stop(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            timer = threading.Timer(self._idle_stop, self._stop_if_idle)
            timer.daemon = True
            self._idle_timer = timer
        timer.start()

    def _stop_if_idle(self) -> None:
        # A running job holds _job_lock; a busy child is never stopped from here.
        if not self._job_lock.acquire(blocking=False):
            return
        try:
            if time.monotonic() - self._last_job_end < self._idle_stop:
                return
            with self._state_lock:
                conn, self._conn = self._conn, None
            if conn is not None:
                conn.close()
        finally:
            self._job_lock.release()

    # jobs

    def run_job(
        self,
        job: dict[str, Any],
        *,
        serve_embed: Callable[[dict[str, Any]], dict[str, Any]],
        on_committed: Callable[[], None],
    ) -> dict[str, Any]:
        """Run *job* in the child and return its result frame.

        The result frame is ``{"ok": True, ...}`` or ``{"ok": False, "kind": ...}``
        for an outcome the child decided (a publication fence or a store error).
        Raises :class:`ChildUnavailable` when the child could not finish the job.
        """
        if self._closed:
            raise ChildUnavailable("consolidation write child is closed")
        if time.monotonic() < self._down_until:
            raise ChildUnavailable("consolidation write child is backing off")
        if not self._job_lock.acquire(timeout=self._queue_wait):
            raise ChildUnavailable("consolidation write child stayed busy")
        try:
            return self._run_locked(job, serve_embed, on_committed)
        finally:
            self._last_job_end = time.monotonic()
            self._job_lock.release()
            self._arm_idle_stop()

    def _run_locked(
        self,
        job: dict[str, Any],
        serve_embed: Callable[[dict[str, Any]], dict[str, Any]],
        on_committed: Callable[[], None],
    ) -> dict[str, Any]:
        if time.monotonic() < self._down_until:
            raise ChildUnavailable("consolidation write child is backing off")
        conn = self._conn
        if conn is None or conn.proc.poll() is not None:
            if conn is not None:
                conn.close()
            try:
                conn = self._spawn()
            except ChildUnavailable as exc:
                raise self._fail(None, str(exc)) from exc
            except OSError as exc:
                raise self._fail(None, f"could not start: {exc}") from exc
            with self._state_lock:
                if self._closed:
                    conn.close()
                    raise ChildUnavailable("consolidation write child is closed")
                self._conn = conn
        self._next_id += 1
        job_id = self._next_id
        conn.send({**job, "op": "job", "id": job_id})
        deadline = time.monotonic() + self._job_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._fail(conn, f"job exceeded {self._job_timeout:.0f} s")
            try:
                frame = conn.inbox.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                if conn.proc.poll() is not None:
                    raise self._fail(conn, f"exited with code {conn.proc.returncode}")
                continue
            if frame is None:
                raise self._fail(conn, f"exited with code {conn.proc.poll()}")
            op = frame.get("op")
            if op == "committed":
                on_committed()
            elif op == "embed":
                try:
                    reply = serve_embed(frame)
                except Exception:  # noqa: BLE001 - an embed that raised is an embed that failed
                    logger.debug("Embedding for the write child failed", exc_info=True)
                    reply = {"vector": None}
                conn.send({"op": "embed_result", **reply})
            elif op == "result" and frame.get("id") == job_id:
                with self._state_lock:
                    self._failures = 0
                    self._down_until = 0.0
                return frame


_client: WriteChild | None = None
_client_lock = threading.Lock()


def get_client() -> WriteChild:
    """The process-wide child handle, created on first use."""
    global _client
    with _client_lock:
        if _client is None:
            _client = WriteChild()
            atexit.register(shutdown_client)
        return _client


def shutdown_client() -> None:
    """Stop the child, if one was started."""
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


def write_child_enabled() -> bool:
    """Whether consolidation writes run in a child (``memory``), default off.

    Anything but a literal ``true`` keeps the in-process writes. Read through the
    fingerprint-cached config, so the switch takes effect at the next pass.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return getattr(KiroCrewConfig.load().memory, "consolidation_write_child_process") is True
    except Exception:  # noqa: BLE001 - an unreadable config keeps the default
        logger.debug("Could not read memory.consolidation_write_child_process", exc_info=True)
        return False


# ── child side ──


class _Rpc:
    """The child's synchronous channel to the gateway (one outstanding request)."""

    def __init__(self, stdin: IO[bytes], out: IO[bytes]) -> None:
        self.stdin = stdin
        self.out = out

    def send(self, msg: dict[str, Any]) -> None:
        _write_msg(self.out, msg)

    def next(self) -> dict[str, Any] | None:
        raw = self.stdin.readline()
        if not raw:
            return None
        msg = json.loads(raw)
        return msg if isinstance(msg, dict) else {}


class _ChildState:
    """What the child keeps between jobs: the opened stores and the transcript log."""

    def __init__(self, rpc: _Rpc) -> None:
        self.rpc = rpc
        self.stores: dict[str, Any] = {}
        self.logs: dict[str, Any] = {}

    def log_for(self, sessions_dir: str) -> Any:
        log = self.logs.get(sessions_dir)
        if log is None:
            from kiro_crew.history import ConversationLog

            log = self.logs[sessions_dir] = ConversationLog(Path(sessions_dir))
        return log

    def store_for(self, db_path: str, embedding_dim: int | None) -> Any:
        store = self.stores.pop(db_path, None)
        if (
            store is not None
            and isinstance(embedding_dim, int)
            and getattr(store, "_embedding_dim", embedding_dim) != embedding_dim
        ):
            # The gateway moved to an embedding model of another width. A kept store
            # would build its FAISS view and score at the old one, so reopen it.
            try:
                store.close()
            except Exception:  # noqa: BLE001 - replaced either way
                logger.debug("Could not close a store of the previous width", exc_info=True)
            store = None
        if store is None:
            from kiro_crew.config.loader import KiroCrewConfig
            from kiro_crew.memory_stores import named_store_of_db
            from kiro_crew.vector_memory import VectorMemoryStore, declared_store

            cfg = KiroCrewConfig.load()
            path = Path(db_path)
            name = named_store_of_db(path)
            # The gateway store's own width, so both sides score one vector space.
            dim = embedding_dim if isinstance(embedding_dim, int) else cfg.memory.embedding_dim
            if name:
                store = declared_store(path, store_id=name, config=cfg, embedding_dim=dim)
            else:
                store = VectorMemoryStore(path, config=cfg, embedding_dim=dim)
            store.init()
            self._route_embeds(store)
        else:
            self._refresh(store)
        self.stores[db_path] = store  # most recently used last
        while len(self.stores) > _CHILD_STORE_CAP:
            oldest = next(iter(self.stores))
            try:
                self.stores.pop(oldest).close()
            except Exception:  # noqa: BLE001 - closing a store we are done with
                logger.debug("Could not close an idle store", exc_info=True)
        return store

    @staticmethod
    def _refresh(store: Any) -> None:
        """Bring a store kept from an earlier job up to date with the gateway.

        The gateway's store follows ``config.json`` through a watcher and re-reads
        its FAISS view when another process has written. This child has neither, so
        both happen here before each job: the ``memory.*`` thresholds (the episodic
        cap and dedupe threshold decide what a write tombstones) and the FAISS
        index, which the episodic dedupe searches. SQLite's ``data_version`` moves
        only for writes made by OTHER connections, so a rebuild happens only after
        the gateway or another process wrote, never for this child's own writes.
        """
        from kiro_crew import vector_memory as vm
        from kiro_crew.config.loader import KiroCrewConfig

        store.reconfigure(KiroCrewConfig.load())
        if not (vm._HAS_FAISS and vm._HAS_NUMPY):
            return
        if store._faiss_index is None or store._faiss_data_version != store._sqlite_data_version():
            store.build_faiss_index()

    def _route_embeds(self, store: Any) -> None:
        """Send every embedding the store asks for to the gateway's embedder."""
        from kiro_crew.embeddings_settings import PRIORITY_NORMAL
        from kiro_crew.vector_memory_runtime.embedding import _EmbeddingVector

        rpc = self.rpc

        def try_embed(text: str, priority: int = PRIORITY_NORMAL) -> Any:
            rpc.send({"op": "embed", "text": text, "priority": priority})
            reply = rpc.next()
            if reply is None:
                raise SystemExit(0)  # the gateway is gone
            generation = reply.get("generation")
            if isinstance(generation, int):
                store._space_generation = generation
            vector = reply.get("vector")
            if not vector:
                return None
            token = reply.get("token") or [None, ""]
            return _EmbeddingVector(
                vector, (token[0], token[1]), managed=bool(reply.get("managed"))
            )

        store._try_embed = try_embed

    def bind(self, store: Any, job: dict[str, Any]) -> None:
        """Mirror the gateway store's embedder availability and vector space."""
        generation = job.get("space_generation")
        if isinstance(generation, int):
            store._space_generation = generation
        # embed_semantic / embed_episodic / embed_lesson check ``embed_fn is None``
        # before they call _try_embed; the real embedding happens in the gateway.
        store.embed_fn = _unreachable_embed if job.get("embed_available") else None
        store.embed_fn_factory = None


def _unreachable_embed(text: str) -> list[float] | None:
    raise RuntimeError("embeddings are served by the gateway")


def _host(log: Any) -> Any:
    """A ``HistoryConsolidator`` with only what the two write functions read.

    ``__new__`` skips ``__init__`` (config watchers, the session manager, the
    skills loader), none of which the write path touches. The write functions are
    methods of that class, so running them on this instance runs the same code.
    """
    from kiro_crew.history_consolidation import HistoryConsolidator

    host = object.__new__(HistoryConsolidator)
    host._log = log
    host._vector_store = None
    host._lesson_store = None
    return host


def _run_job(state: _ChildState, job: dict[str, Any]) -> dict[str, Any]:
    from kiro_crew.history_consolidation import (
        _LeaseLostMidRun,
        _PersistenceDisabledMidRun,
        _RunCommitState,
    )
    from kiro_crew.history_derivation import TranscriptBusy, TranscriptWithheld
    from kiro_crew.memory_schema import MemoryFacets

    rpc = state.rpc

    class _NotifyingState(_RunCommitState):
        def mark_committed(self) -> None:
            first = not self.committed
            super().mark_committed()
            if first:
                rpc.send({"op": "committed"})

    host = _host(state.log_for(job["sessions_dir"]))
    store = state.store_for(job["db_path"], job.get("embedding_dim"))
    state.bind(store, job)
    commit_state = _NotifyingState(job.get("lease_token"))
    commit_state.committed = bool(job.get("committed"))
    facets_raw = job.get("facets")
    facets = MemoryFacets(**facets_raw) if isinstance(facets_raw, dict) else None
    args = job.get("args") or {}
    try:
        if job["kind"] == "structured":
            visible = args.get("visible_keys")
            host._write_structured_memory(
                args["result"],
                job["key"],
                store,
                facets=facets,
                snapshot=args.get("snapshot"),
                visible_keys=None if visible is None else frozenset(visible),
                messages=None,
                commit_state=commit_state,
            )
        elif job["kind"] == "lessons":
            host._save_lessons(
                args["raw"], store, None, facets=facets, key=job["key"], commit_state=commit_state
            )
        else:
            return {"ok": False, "kind": "error", "message": f"unknown job kind {job['kind']!r}"}
    except _LeaseLostMidRun:
        return {"ok": False, "kind": "lease_lost", "committed": commit_state.committed}
    except _PersistenceDisabledMidRun:
        return {"ok": False, "kind": "persistence_disabled", "committed": commit_state.committed}
    except TranscriptBusy as exc:  # before its base class
        return {
            "ok": False,
            "kind": "busy",
            "message": str(exc),
            "committed": commit_state.committed,
        }
    except TranscriptWithheld as exc:
        return {
            "ok": False,
            "kind": "withheld",
            "message": str(exc),
            "committed": commit_state.committed,
        }
    except Exception as exc:  # noqa: BLE001 - relayed; the gateway reruns the job itself
        logger.warning("Consolidation write job failed", exc_info=True)
        return {
            "ok": False,
            "kind": "error",
            "message": f"{type(exc).__name__}: {exc}",
            "committed": commit_state.committed,
        }
    return {"ok": True, "committed": commit_state.committed}


def facets_to_json(facets: Any) -> dict[str, Any] | None:
    """A ``MemoryFacets`` as plain data for a job frame."""
    if facets is None or not dataclasses.is_dataclass(facets) or isinstance(facets, type):
        return None
    return dataclasses.asdict(facets)


def _watch_parent(parent: int) -> None:
    while True:
        time.sleep(_PARENT_POLL_SECS)
        if _parent_gone(parent):
            os._exit(0)


def main() -> int:
    """Child entry: answer jobs from stdin until EOF or the gateway goes away."""
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
        level=logging.INFO,
        stream=sys.stderr,
        format="kirocrew consolidation write child: %(message)s",
    )
    if hasattr(os, "nice"):
        try:
            os.nice(_CHILD_NICE)
        except OSError:
            pass
    threading.Thread(
        target=_watch_parent, args=(os.getppid(),), name="kc-cwc-parent-watch", daemon=True
    ).start()
    return _serve(sys.stdin.buffer, out)


def _serve(stdin: IO[bytes], out: IO[bytes]) -> int:
    rpc = _Rpc(stdin, out)
    state = _ChildState(rpc)
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
        except Exception as exc:  # noqa: BLE001 - setup failed (store open, log open)
            logger.warning("Consolidation write job could not start", exc_info=True)
            reply = {"ok": False, "kind": "error", "message": f"{type(exc).__name__}: {exc}"}
        rpc.send({"op": "result", "id": msg.get("id"), **reply})
    for store in state.stores.values():
        try:
            store.close()
        except Exception:  # noqa: BLE001 - exiting anyway
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
