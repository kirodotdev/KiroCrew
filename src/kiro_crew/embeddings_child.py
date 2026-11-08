"""Run the embedding model in a child process, behind the in-process ``Llama`` surface.

:class:`ChildLlama` stands in for the vendored ``Llama`` object that
:class:`~kiro_crew.embeddings_llama.LlamaCppEmbedder` publishes. Everything
around it -- lazy load, the serving gate, the priority queue, bulk pacing, idle
eviction -- keeps running in the parent as before. Only the model and its
``create_embedding`` calls move to a child interpreter, so native inference and
the Python around it run outside the parent's GIL and at a lower CPU priority.

Transport is the child's own stdin and stdout pipes, newline-delimited JSON.
There is no socket and no port: only the parent holds the pipe ends, and the
pipes are not inherited by other children (Python creates them close-on-exec).

The child never outlives the parent: it exits on EOF from its stdin, which the
kernel delivers when the parent dies, and a watchdog thread also exits it when
its parent is gone (the pipe can outlive the parent if a grandchild somehow
holds it): a changed parent pid on POSIX, a parent pid that is not alive on
Windows.

When the child dies or stops answering, waiting callers fail (``embed_batch``
returns ``None``, the keyword-search path) and the child is respawned in the
background with exponential backoff (see ``_CHILD_RESPAWN_*``).

``python -m kiro_crew.embeddings_child`` is the child's entry point; nothing
else runs it.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import itertools
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from typing import IO, Any

from kiro_crew.embeddings_settings import (
    _CHILD_CALL_BASE_SECS,
    _CHILD_CALL_SECS_PER_TEXT,
    _CHILD_LOAD_TIMEOUT_SECS,
    _CHILD_NICE,
    _CHILD_RESPAWN_BASE_SECS,
    _CHILD_RESPAWN_CAP_SECS,
    _CHILD_STABLE_SECS,
    EmbeddingWork,
)
from kiro_crew.platform_compat_native import _SUBPROCESS_NO_WINDOW

logger = logging.getLogger("kiro_crew.embeddings")

#: ``module:callable`` the child calls to get the Llama class. Production uses
#: the vendored loader; tests point it at a fake importable in the child.
_CHILD_LLAMA_FACTORY = "kiro_crew.embeddings_native:_load_llama_class"
_CHILD_MODULE = "kiro_crew.embeddings_child"
_IS_WINDOWS = sys.platform == "win32"
# How often the child checks that its parent is still the process that started it.
_PARENT_POLL_SECS = 1.0
# Longest line the parent accepts from the child.
_MAX_RESPONSE_BYTES = 16 << 20
# How long close() waits for the child to exit after asking it to.
_CHILD_STOP_WAIT_SECS = 5.0


class ChildEmbeddingError(RuntimeError):
    """The child could not answer: not running, crashed, or reported an error."""


def _child_creationflags() -> int:
    """Windows process flags for the child; 0 on other platforms.

    No console window (a detached gateway would otherwise open a visible one),
    and below-normal priority, which is Windows' counterpart of the child's nice
    level since it has no ``os.nice``.
    """
    return _SUBPROCESS_NO_WINDOW | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


def is_vendored_llama_class(cls: object) -> bool:
    """True for the vendored runtime's ``Llama`` class, which the child can rebuild.

    A class injected in-process (a test fake, a future backend) cannot be
    re-imported in another interpreter, so it keeps running in-process.
    """
    return getattr(cls, "__module__", "") == "llama_cpp.llama" and (
        getattr(cls, "__name__", "") == "Llama"
    )


class _ChildCtx:
    """The ``_ctx`` surface ``_apply_thread_class`` programs, forwarded to the child."""

    def __init__(self, state: "_ChildState") -> None:
        self._state = state

    def set_n_threads(self, n_threads: int, n_threads_batch: int) -> None:
        self._state.set_threads(int(n_threads), int(n_threads_batch))


class _Conn:
    """One running child: its process, its writer queue, and its outstanding requests.

    Every write goes through the writer thread, so no caller (and no shutdown)
    ever blocks on a full pipe. ``outstanding`` keeps the send time of every
    request the child has not answered, including ones whose caller gave up, so
    a wedged child is detected whoever is waiting.
    """

    def __init__(self, proc: "subprocess.Popen[bytes]") -> None:
        self.proc = proc
        self.outbox: "queue.SimpleQueue[bytes | None]" = queue.SimpleQueue()
        # request id -> text count, in send order (the child answers in order)
        self.outstanding: dict[int, int] = {}
        # Set while ``outstanding`` is empty.
        self.idle = threading.Event()
        self.idle.set()
        self.started = time.monotonic()
        # Last time the child answered anything (or was sent work while idle).
        self.progress_at = self.started
        self.writer = threading.Thread(
            target=self._write_loop, name="kc-embed-child-write", daemon=True
        )
        self.writer.start()

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
            _kill(self.proc)  # the reader sees EOF and handles the exit
        finally:
            try:
                if stdin is not None:
                    stdin.close()
            except (OSError, ValueError):
                pass

    def wedged(self, now: float) -> bool:
        """Whether the request the child is working on is past its budget.

        The child answers in order, so the oldest outstanding request is the one
        running; its clock starts at the child's last answer (or when it was sent
        to an idle child), and its budget grows with its text count.
        """
        head = next(iter(self.outstanding.values()), None)
        if head is None:
            return False
        budget = _CHILD_CALL_BASE_SECS + _CHILD_CALL_SECS_PER_TEXT * max(1, head)
        return now - self.progress_at >= budget


class _ChildState:
    """Process, pipes and pending requests of one ChildLlama. Threads hold this, not it.

    Keeping the worker threads off the ChildLlama object is what lets the last
    reference to a retired model stop its child: ``weakref.finalize`` on the
    ChildLlama calls :meth:`shutdown` here.
    """

    def __init__(self, load: dict[str, Any], factory: str) -> None:
        self.load = load
        self.factory = factory
        self.lock = threading.Lock()
        self.conn: _Conn | None = None
        # A child between Popen and ready; shutdown kills it too.
        self.starting: "subprocess.Popen[bytes] | None" = None
        self.pending: dict[int, concurrent.futures.Future[dict[str, Any]]] = {}
        self.ids = itertools.count(1)
        self.closed = threading.Event()
        self.failures = 0
        self.respawn_thread: threading.Thread | None = None
        self.threads: tuple[int, int] | None = None
        self.n_embd: int | None = None
        self.spawns = 0

    @property
    def proc(self) -> "subprocess.Popen[bytes] | None":
        conn = self.conn
        return conn.proc if conn is not None else None

    # ── lifecycle ──

    def spawn(self) -> None:
        """Start a child, send the load request, and wait until it is ready."""
        from kiro_crew import platform_compat

        # -P: never put the working directory on the child's sys.path, so a
        # kiro_crew/ directory in the gateway's cwd cannot shadow the package;
        # isolated_python_argv adds the bundle's user-site policy on top.
        argv = platform_compat.isolated_python_argv("-P", "-m", _CHILD_MODULE)
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, this interpreter
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            close_fds=True,
            creationflags=_child_creationflags(),
        )
        with self.lock:
            if self.closed.is_set():
                _kill(proc)
                raise ChildEmbeddingError("embedding child closed while starting")
            self.starting = proc
        conn = _Conn(proc)
        ready: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
        threading.Thread(
            target=self._read_loop, args=(conn, ready), name="kc-embed-child-read", daemon=True
        ).start()
        request = dict(self.load)
        request["op"] = "load"
        request["factory"] = self.factory
        request["nice"] = _CHILD_NICE
        sent_threads = self.threads
        request["threads"] = list(sent_threads) if sent_threads else None
        try:
            conn.send(request)
            msg = ready.result(_CHILD_LOAD_TIMEOUT_SECS)
            if msg.get("op") != "ready":
                raise ChildEmbeddingError(str(msg.get("error") or "embedding child failed to load"))
            n_embd = msg.get("n_embd")
            n_embd = n_embd if isinstance(n_embd, int) and n_embd > 0 else None
            if self.n_embd is not None and n_embd != self.n_embd:
                # A respawn must serve the vector space the first child published.
                raise ChildEmbeddingError(
                    f"respawned embedding child reports width {n_embd}, expected {self.n_embd}"
                )
        except BaseException:
            with self.lock:
                if self.starting is proc:
                    self.starting = None
            conn.outbox.put(None)
            _kill(proc)
            raise
        with self.lock:
            self.starting = None
            if self.closed.is_set():
                published = False
            else:
                published = True
                self.conn = conn
                self.n_embd = n_embd
                self.spawns += 1
                if self.threads != sent_threads and self.threads is not None:
                    # set_threads ran while this child was loading; under the
                    # same lock, so it either saw this conn or is covered here.
                    conn.send({"op": "threads", "n": self.threads[0], "nb": self.threads[1]})
                if self.respawn_thread is threading.current_thread():
                    # Cleared in the same step that publishes, so an exit landing
                    # from now on always starts a fresh respawn thread.
                    self.respawn_thread = None
        if not published:
            conn.outbox.put(None)
            _kill(proc)
            raise ChildEmbeddingError("embedding child closed while loading")
        if proc.poll() is not None:
            # Died between ready and publishing: the reader's exit was ignored
            # because this conn was not current yet, so handle it here.
            self._on_exit(conn)

    def _read_loop(self, conn: _Conn, ready: "concurrent.futures.Future[dict[str, Any]]") -> None:
        stream = conn.proc.stdout
        assert stream is not None
        try:
            while True:
                # Bounded: one answer is a few hundred KiB of vectors, so a line
                # past the cap is a broken child, never a real response.
                raw = stream.readline(_MAX_RESPONSE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > _MAX_RESPONSE_BYTES:
                    logger.warning("Embedding child wrote an oversized line; stopping it")
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    logger.warning("Embedding child wrote a malformed line; stopping it")
                    break
                if not isinstance(msg, dict):
                    continue
                if "id" not in msg:
                    if not ready.done():
                        ready.set_result(msg)
                    continue
                with self.lock:
                    conn.outstanding.pop(msg["id"], None)
                    conn.progress_at = time.monotonic()
                    if not conn.outstanding:
                        conn.idle.set()
                    fut = self.pending.pop(msg["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except (OSError, ValueError):
            pass
        finally:
            if not ready.done():
                ready.set_result({"op": "error", "error": "embedding child exited during load"})
            conn.outbox.put(None)
            _kill(conn.proc)
            self._on_exit(conn)

    def _on_exit(self, conn: _Conn) -> None:
        """The child behind *conn* is gone: fail its waiters and schedule a respawn.

        Idempotent per conn: only the call that finds it current acts.
        """
        with self.lock:
            if self.conn is not conn:
                return  # never published (a failed load) or already handled
            self.conn = None
            pending, self.pending = self.pending, {}
            start = False
            if not self.closed.is_set():
                self.failures += 1
                if self.respawn_thread is None or not self.respawn_thread.is_alive():
                    self.respawn_thread = threading.Thread(
                        target=self._respawn_loop, name="kc-embed-respawn", daemon=True
                    )
                    start = True
            thread = self.respawn_thread
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(ChildEmbeddingError("embedding child exited"))
        if not self.closed.is_set():
            logger.warning(
                "Embedding child exited (code %s); respawning it in %.0f s",
                conn.proc.returncode,
                self.respawn_delay(),
            )
        if start and thread is not None:
            thread.start()

    def respawn_delay(self) -> float:
        exponent = max(0, self.failures - 1)
        return min(_CHILD_RESPAWN_CAP_SECS, _CHILD_RESPAWN_BASE_SECS * (2.0 ** min(exponent, 20)))

    def _respawn_loop(self) -> None:
        while not self.closed.wait(self.respawn_delay()):
            try:
                self.spawn()
                return
            except Exception:  # noqa: BLE001 - keep retrying on the backoff
                with self.lock:
                    self.failures += 1
                logger.warning(
                    "Embedding child respawn failed; next attempt in %.0f s",
                    self.respawn_delay(),
                    exc_info=True,
                )

    def shutdown(self) -> None:
        """Stop the child and every future respawn. Idempotent; never blocks on a pipe."""
        self.closed.set()
        with self.lock:
            conn, self.conn = self.conn, None
            starting, self.starting = self.starting, None
            pending, self.pending = self.pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(ChildEmbeddingError("embedding child closed"))
        if starting is not None:
            _kill(starting)
        if conn is None:
            return
        conn.send({"op": "exit"})
        conn.outbox.put(None)
        try:
            conn.proc.wait(_CHILD_STOP_WAIT_SECS)
        except Exception:  # noqa: BLE001 - fall through to kill
            pass
        _kill(conn.proc)

    # ── requests ──

    def set_threads(self, n_threads: int, n_threads_batch: int) -> None:
        """Record the thread class and forward it; a respawn reapplies it at load."""
        with self.lock:
            self.threads = (n_threads, n_threads_batch)
            conn = self.conn
        if conn is not None:
            conn.send({"op": "threads", "n": n_threads, "nb": n_threads_batch})

    def _wait_until_idle(self, conn: _Conn, work: "EmbeddingWork | None") -> None:
        """Wait until *conn* has no unanswered request, within the caller's deadline.

        The child serves one request at a time, as the in-process model does. A
        request whose caller gave up is still computed there, so sending more
        behind it would build a queue of abandoned work that every later caller
        waits out. A caller that cannot wait for it degrades instead, and nothing
        is sent for it.
        """
        while True:
            with self.lock:
                if self.conn is not conn:
                    raise ChildEmbeddingError("embedding child exited")
                if not conn.outstanding:
                    return
                wedged = conn.wedged(time.monotonic())
            if wedged:
                logger.warning("Embedding child stopped answering; restarting it")
                _kill(conn.proc)
                raise TimeoutError("embedding child did not answer in time")
            if work is not None and work.expired():
                raise TimeoutError("embedding work expired waiting for the child")
            conn.idle.wait(0.05)

    def embed(self, texts: "list[str]", work: "EmbeddingWork | None") -> dict[str, Any]:
        with self.lock:
            if self.closed.is_set():
                raise ChildEmbeddingError("embedding child closed")
            conn = self.conn
            if conn is None:
                raise ChildEmbeddingError("embedding child is restarting")
        while True:
            self._wait_until_idle(conn, work)
            with self.lock:
                if self.conn is not conn:
                    raise ChildEmbeddingError("embedding child exited")
                if conn.outstanding:
                    continue  # another sender got there first
                req_id = next(self.ids)
                fut: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
                self.pending[req_id] = fut
                conn.progress_at = time.monotonic()  # idle child: the clock starts now
                conn.outstanding[req_id] = len(texts)
                conn.idle.clear()
                break
        conn.send({"id": req_id, "op": "embed", "texts": list(texts)})
        while True:
            try:
                msg = fut.result(0.05)
                break
            except concurrent.futures.TimeoutError:
                pass
            with self.lock:
                wedged = conn.wedged(time.monotonic())
            if wedged:
                # The child has left SOME request unanswered too long, whether or
                # not its caller still waits: treat the runtime as wedged.
                with self.lock:
                    self.pending.pop(req_id, None)
                logger.warning("Embedding child stopped answering; restarting it")
                _kill(conn.proc)
                raise TimeoutError("embedding child did not answer in time")
            if work is not None and work.expired():
                # The caller stopped waiting. The child finishes this request and
                # its answer is dropped; it stays outstanding until then.
                with self.lock:
                    self.pending.pop(req_id, None)
                raise TimeoutError("embedding work expired during child inference")
        if not msg.get("ok"):
            raise ChildEmbeddingError(str(msg.get("error") or "embedding child error"))
        if time.monotonic() - conn.started >= _CHILD_STABLE_SECS:
            # Only a child that has stayed up resets the backoff, so text that
            # crashes the runtime cannot pin the respawn delay at its minimum.
            with self.lock:
                self.failures = 0
        vectors = msg.get("vectors")
        if not isinstance(vectors, list):
            raise ChildEmbeddingError("embedding child returned no vectors")
        return {"data": [{"embedding": v} for v in vectors]}


def _kill(proc: "subprocess.Popen[bytes]") -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(_CHILD_STOP_WAIT_SECS)
    except Exception:  # noqa: BLE001 - nothing more can be done
        pass


class ChildLlama:
    """A loaded model living in a child process, with the ``Llama`` calls the embedder uses.

    Construction spawns the child and blocks until it has loaded the model, so
    it runs on the embedder's loader thread like the in-process constructor.
    Raises :class:`ChildEmbeddingError` when the child cannot load; the loader
    treats that like any failed load.
    """

    def __init__(
        self,
        kwargs: dict[str, Any],
        *,
        stamp: "tuple[int, ...] | None",
        factory: str | None = None,
    ) -> None:
        load = {"kwargs": dict(kwargs), "stamp": list(stamp) if stamp is not None else None}
        self._state = _ChildState(load, factory or _CHILD_LLAMA_FACTORY)
        self._ctx = _ChildCtx(self._state)
        self._finalizer = weakref.finalize(self, self._state.shutdown)
        self._state.spawn()

    def n_embd(self) -> int:
        n = self._state.n_embd
        if n is None:
            raise ChildEmbeddingError("embedding child reported no width")
        return n

    def create_embedding(self, texts: "list[str]") -> dict[str, Any]:
        return self._state.embed(texts, None)

    def create_embedding_for(
        self, texts: "list[str]", work: "EmbeddingWork | None"
    ) -> dict[str, Any]:
        """``create_embedding`` that stops waiting when *work* expires."""
        return self._state.embed(texts, work)

    @property
    def pid(self) -> int | None:
        proc = self._state.proc
        return proc.pid if proc is not None else None

    def close(self) -> None:
        self._finalizer()


# ── Child side ──


def _write_msg(out: IO[bytes], msg: dict[str, Any]) -> None:
    out.write(json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n")
    out.flush()


def _parent_gone(parent: int) -> bool:
    """Whether the process that started this child has exited.

    POSIX reparents an orphan, so its parent pid changes. Windows keeps the
    original parent pid after that parent exits, so liveness is asked instead.
    """
    if _IS_WINDOWS:
        from kiro_crew import platform_compat

        return not platform_compat.pid_exists(parent)
    return os.getppid() != parent


def _watch_parent(parent: int) -> None:
    while True:
        time.sleep(_PARENT_POLL_SECS)
        if _parent_gone(parent):
            os._exit(0)


def _resolve_factory(spec: str) -> Any:
    module_name, _, attr = spec.partition(":")
    factory = getattr(importlib.import_module(module_name), attr)
    return factory()


def _load(request: dict[str, Any]) -> tuple[Any, int | None]:
    from pathlib import Path

    from kiro_crew.embeddings_llama import (
        LlamaCppEmbedder,
        _install_bounded_decode,
        _install_metadata_kv_clear,
    )
    from kiro_crew.embeddings_model import (
        _is_sensitive_model_path,
        _model_file_stamp,
        _model_file_stamp_unaliased,
    )

    nice = request.get("nice")
    if isinstance(nice, int) and nice > 0 and hasattr(os, "nice"):
        try:
            os.nice(nice)
        except OSError:
            pass
    kwargs = dict(request.get("kwargs") or {})
    path = Path(str(kwargs.get("model_path", "")))
    if _is_sensitive_model_path(path):
        raise ChildEmbeddingError("refusing to load an embedding model from a protected location")
    expected = request.get("stamp")
    if expected is not None:
        stamp = _model_file_stamp_unaliased(path)
        if stamp is None or list(stamp) != list(expected):
            raise ChildEmbeddingError("embedding model file changed since the parent checked it")
    llama_cls = _resolve_factory(str(request.get("factory") or _CHILD_LLAMA_FACTORY))
    if llama_cls is None:
        raise ChildEmbeddingError("llama.cpp runtime unavailable in the embedding child")
    llm = llama_cls(**kwargs)
    _install_metadata_kv_clear(llm)
    _install_bounded_decode(llm)
    if expected is not None:
        try:
            unchanged = list(_model_file_stamp(path) or ()) == list(expected)
        except OSError:
            unchanged = False
        if not unchanged:
            raise ChildEmbeddingError("embedding model file changed while the child loaded it")
    threads = request.get("threads")
    if isinstance(threads, list) and len(threads) == 2:
        _set_threads(llm, int(threads[0]), int(threads[1]))
    return llm, LlamaCppEmbedder._probe_dim(llm)


def _set_threads(llm: Any, n_threads: int, n_threads_batch: int) -> None:
    setter = getattr(getattr(llm, "_ctx", None), "set_n_threads", None)
    if callable(setter):
        try:
            setter(n_threads, n_threads_batch)
        except Exception:  # noqa: BLE001 - keep the loaded count
            logger.debug("Could not set the embedding thread count", exc_info=True)


def main() -> int:
    """Child entry: load once, then answer requests from stdin until EOF or exit."""
    try:
        from kiro_crew._llama_lib_path import drop_inherited_lib_path

        drop_inherited_lib_path()
    except Exception:  # noqa: BLE001 - the parent's prelude already ran
        pass
    # The protocol owns the original stdout; anything native code prints goes to
    # stderr instead, so a stray printf can never corrupt a response line.
    out = os.fdopen(os.dup(1), "wb")
    try:
        os.dup2(2, 1)
        sys.stdout = sys.stderr
    except OSError:  # no stderr to point at: discard native output instead
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stderr, format="kirocrew embedding child: %(message)s"
    )
    threading.Thread(
        target=_watch_parent, args=(os.getppid(),), name="kc-embed-parent-watch", daemon=True
    ).start()
    return _serve(sys.stdin.buffer, out)


def _serve(stdin: IO[bytes], out: IO[bytes]) -> int:
    """Load from the first request, then answer requests until EOF or ``exit``."""
    first = stdin.readline()
    if not first:
        return 0
    try:
        request = json.loads(first)
        llm, n_embd = _load(request)
    except Exception as exc:  # noqa: BLE001 - reported to the parent
        _write_msg(out, {"op": "error", "error": f"{type(exc).__name__}: {exc}"})
        return 1
    _write_msg(out, {"op": "ready", "n_embd": n_embd, "pid": os.getpid()})
    for raw in stdin:
        try:
            msg = json.loads(raw)
        except ValueError:
            return 1
        op = msg.get("op")
        if op == "exit":
            break
        if op == "threads":
            _set_threads(llm, int(msg.get("n", 1)), int(msg.get("nb", 1)))
            continue
        if op != "embed":
            continue
        try:
            result = llm.create_embedding(list(msg.get("texts") or []))
            vectors = [item["embedding"] for item in result["data"]]
            reply: dict[str, Any] = {"id": msg.get("id"), "ok": True, "vectors": vectors}
        except Exception as exc:  # noqa: BLE001 - relayed to the waiting caller
            reply = {"id": msg.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _write_msg(out, reply)
    closer = getattr(llm, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001 - exiting anyway
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
