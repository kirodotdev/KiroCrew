"""Build a local memory index in a child process.

A build reads every memory and tokenizes it in Python. Run on a thread of the
gateway it holds the interpreter lock most of the time: measured on a 23k-row
store, a recall that ran beside a build took 1.1 s instead of 0.5 s, and at
100k rows 5.9 s instead of 2.1 s. A child process has its own interpreter lock.

The child reads one snapshot of the store through a read-only connection,
writes a new generation directory with :func:`local.build_generation` and sends
back its name and the change-log seq it covers. It never repoints ``current``:
the parent, which owns the index, adopts the generation and swaps it in.

This module is the spawn target, so it imports nothing heavy at module level.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
from typing import Any

logger = logging.getLogger(__name__)

#: How long the parent waits. A 100k-row build takes about a minute alone.
CHILD_TIMEOUT_SECS = 1800.0
_TERMINATE_GRACE_SECS = 5.0
#: Threads the child's native libraries may use, so a build never takes the host.
_CHILD_THREADS = "2"


def run_child(
    db_path: str, index_root: str, signature: str, dim: int, conn: Any = None
) -> None:  # pragma: no cover - exercised through run_build_in_child
    """Spawn target: build a generation and send ``(name, build_seq)``."""
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[variable] = _CHILD_THREADS
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass
    from kiro_crew.memory_index.contract import EmbeddingSpace
    from kiro_crew.memory_index.local import build_generation
    from kiro_crew.memory_index.store_index import snapshot_docs

    docs, seq = snapshot_docs(db_path, dim)
    name = build_generation(
        index_root, EmbeddingSpace(signature=signature, dim=dim), iter(docs), seq
    )
    if conn is not None:
        conn.send((name, seq))


def _stop(proc: Any) -> None:
    proc.terminate()
    proc.join(_TERMINATE_GRACE_SECS)
    if proc.is_alive():
        proc.kill()
        proc.join()


def run_build_in_child(
    db_path: str, index_root: str, signature: str, dim: int, *, timeout: float = CHILD_TIMEOUT_SECS
) -> tuple[str, int] | None:
    """``(generation name, build_seq)`` from a spawned child, or ``None`` with one
    WARNING saying why; the caller then builds in process."""
    reason = ""
    result = None
    try:
        ctx = multiprocessing.get_context("spawn")
        receiver, sender = ctx.Pipe(duplex=False)
        proc = ctx.Process(
            target=run_child,
            args=(db_path, index_root, signature, dim, sender),
            name="kirocrew-memory-index-build",
            daemon=True,
        )
        proc.start()
        sender.close()
        try:
            ready = receiver.poll(timeout)
            result = receiver.recv() if ready else None
        except (EOFError, OSError):
            ready, result = True, None
        if not ready:
            reason = f"timed out after {timeout:.0f}s"
            _stop(proc)
        else:
            proc.join(_TERMINATE_GRACE_SECS)
            if proc.is_alive():
                _stop(proc)
            if proc.exitcode not in (0, None) or result is None:
                reason = f"exited with code {proc.exitcode} and no result"
                result = None
    except Exception as exc:  # noqa: BLE001 - no child is a degraded mode, not a crash
        reason = f"could not start ({type(exc).__name__}: {exc})"
    if reason:
        logger.warning("Memory index build child %s; building in process", reason)
        return None
    name, seq = result  # type: ignore[misc]
    return str(name), int(seq)
