"""``delete_items_batch`` must never run on the event loop.

It rebuilds the entire entity graph (``store._load_graph``: clear + two full table
scans) inside its own ``BEGIN``/``COMMIT``, so on a large library it blocks the loop
past the stall watchdog. The watchdog then exits, the supervisor respawns, and the
fresh process runs the same scan -- a crash loop, observed repeatedly against a
2900-document folder source with the frozen stack pinning
``store.delete_items_batch <- ingestion._ingest_file_body <- folder_watcher._do_scan``.

Two tests, deliberately different in kind:

* the AST ratchet pins every call site at once and fails if any future edit calls it
  straight from a coroutine body, including sites this PR has not seen;
* the behavioural test proves the offload is real rather than lexical -- it asserts
  the THREAD the call lands on, so a refactor that keeps ``asyncio.to_thread`` in the
  source but hands it something already-invoked still fails.
"""

from __future__ import annotations

import ast
import json
import pathlib
import threading
from unittest.mock import MagicMock

import pytest

from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.store import KnowledgeStore

# A nested def / lambda is a separate execution frame -- a sync helper or a thread
# target -- so a call inside one is not running on the loop. Mirrors the scoping the
# repo's other on-loop guards use.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def _on_loop_call_sites(name: str) -> list[str]:
    """Every lexical call to *name* directly inside an ``async def`` body."""
    found: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except SyntaxError:  # pragma: no cover - syntax is enforced elsewhere
            continue
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
            stack = list(fn.body)
            while stack:
                node = stack.pop()
                if isinstance(node, _NESTED_SCOPES):
                    continue
                stack.extend(ast.iter_child_nodes(node))
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = (
                    func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None)
                )
                if called == name:
                    found.append(
                        f"{path.relative_to(_SRC)}:{node.lineno} in async {fn.name}")
    return found


def test_delete_items_batch_is_never_called_on_the_event_loop():
    """Ratchet: hand it to a worker, never call it from a coroutine body."""
    offenders = _on_loop_call_sites("delete_items_batch")
    assert offenders == [], (
        "delete_items_batch runs on the event loop at:\n  "
        + "\n  ".join(offenders)
        + "\nOffload it: await asyncio.to_thread(store.delete_items_batch, ids, ...). "
          "The connection is autocommit (isolation_level=None), so no enclosing "
          "transaction spans the call and the worker's thread-local connection may "
          "take the write lock on its own."
    )


@pytest.mark.asyncio
async def test_handle_deleted_runs_the_delete_off_the_loop_thread(tmp_path, monkeypatch):
    """The offload is real: the delete executes on some other thread."""
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        source_id = store.add_source("src", "local_folder", str(tmp_path))
        item_id = store.add_item("title", "body", "doc", source_id=source_id)

        seen_threads: list[int] = []
        real = store.delete_items_batch

        def recording(*args, **kwargs):
            seen_threads.append(threading.get_ident())
            return real(*args, **kwargs)

        monkeypatch.setattr(store, "delete_items_batch", recording)

        watcher = FolderWatcher(store, MagicMock())
        await watcher._handle_deleted(
            source_id, "gone.md", {"item_ids": json.dumps([item_id])})

        assert seen_threads, (
            "delete_items_batch was never called -- this test no longer exercises "
            "the deleted-file path and would pass vacuously")
        assert threading.get_ident() not in seen_threads, (
            "delete_items_batch ran on the event-loop thread; it must be handed to "
            "asyncio.to_thread")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_finalizer_runs_even_when_cancelled_while_queued():
    """A cancellation landing while the finalizer is still QUEUED in the
    executor must not skip it (GPT round-2 finding on #2336): a bare
    ``await asyncio.to_thread(fn)`` cancels the queued future before ``fn``
    starts, stranding the committed new items with no state finalization.

    Deterministic setup: a 1-worker executor whose only worker is held by a
    blocker, so the finalizer is provably queued when the cancel arrives.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from kiro_crew.knowledge.ingestion import run_to_completion

    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    try:
        gate = threading.Event()
        ran = threading.Event()

        # Occupy the single worker so the finalizer sits in the queue.
        blocker = loop.run_in_executor(None, gate.wait)
        await asyncio.sleep(0)  # let the blocker claim the worker

        task = asyncio.ensure_future(run_to_completion(ran.set))
        await asyncio.sleep(0)  # finalizer submitted, still queued
        task.cancel()
        gate.set()  # release the worker AFTER the cancel landed

        with pytest.raises(asyncio.CancelledError):
            await task
        assert ran.is_set(), (
            "finalizer was skipped by a cancellation that arrived while it "
            "was queued; run_to_completion must drain it before re-raising")
        await blocker
    finally:
        pool.shutdown(wait=True)
