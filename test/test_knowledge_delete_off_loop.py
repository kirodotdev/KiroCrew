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
import asyncio
import json
import threading
from unittest.mock import MagicMock

import pytest
from source_corpus import parsed_candidates, src_root

from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.store import KnowledgeStore

# A nested def / lambda is a separate execution frame -- a sync helper or a thread
# target -- so a call inside one is not running on the loop. Mirrors the scoping the
# repo's other on-loop guards use.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

_SRC = src_root()


def _called_names(body: list[ast.stmt]) -> set[tuple[str, int]]:
    """Every name called directly in *body*, paired with its line number.

    Nested scopes are skipped: a call inside a nested ``def``/``lambda`` runs in
    that frame (a sync helper, or a thread target), not in the enclosing one.
    """
    out: set[tuple[str, int]] = set()
    stack = list(body)
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
        if called:
            out.add((called, node.lineno))
    return out


def _sync_helpers_reaching(tree: ast.Module, name: str) -> set[str]:
    """Sync ``def`` names in this module that reach *name* without a thread hop.

    Computed as a fixpoint, so a chain of sync helpers is followed to any depth.
    Calling one of these from a coroutine body blocks the loop exactly as much
    as calling *name* itself -- the indirection is invisible to a lexical scan,
    which is how ``_skip_as_duplicate`` kept a synchronous
    ``delete_items_batch`` alive after every direct call site was offloaded.

    Scope is deliberately one module: resolving a call to another module's
    method needs import and receiver-type resolution, which an AST scan cannot
    do honestly. Same-file indirection is what the real defect used.
    """
    sync_defs = {
        n.name: n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    reaching: set[str] = set()
    while True:
        grew = False
        for fname, fn in sync_defs.items():
            if fname in reaching:
                continue
            targets = {called for called, _ in _called_names(fn.body)}
            if name in targets or targets & reaching:
                reaching.add(fname)
                grew = True
        if not grew:
            return reaching


def _on_loop_call_sites(name: str) -> list[str]:
    """Every call reaching *name* from an ``async def`` body without a hop.

    Covers both the direct call and a call to a same-module sync helper that
    reaches it (see ``_sync_helpers_reaching``).
    """
    found: list[str] = []
    # Only files whose TEXT holds ``name`` can call it (directly, or through a
    # same-module sync helper that does), so the shared corpus parses just those
    # instead of re-walking all ~1250 modules for this gate. ``src_root()`` is the
    # same tree the old ``_SRC`` named, so the relative paths below are unchanged.
    for path, _text, tree in parsed_candidates(require_all=(name,)):
        indirect = _sync_helpers_reaching(tree, name)
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
            for called, lineno in sorted(_called_names(fn.body), key=lambda c: c[1]):
                if called == name:
                    found.append(
                        f"{path.relative_to(_SRC)}:{lineno} in async {fn.name}")
                elif called in indirect:
                    found.append(
                        f"{path.relative_to(_SRC)}:{lineno} in async {fn.name} "
                        f"(via sync {called})")
    return found


def test_deduped_state_writes_are_never_called_on_the_event_loop():
    """Ratchet: every terminal 'deduped' write reaches a worker thread.

    All three doc-state tables record that state from inside the pre-ingest gate's
    own ``BEGIN IMMEDIATE``, reached through ``run_to_completion``. A new call site
    added straight to a coroutine body would both hold the write lock on the loop
    thread and sit outside the gate's transaction, so it must fail here rather than
    in production.

    One assertion for three modules: ``folder_watcher``, ``artifact_ingest`` and
    ``agent_source`` each name their finalizer ``_record_deduped_state``.
    """
    offenders = _on_loop_call_sites("_record_deduped_state")
    assert offenders == [], (
        "_record_deduped_state is reached from the event loop at:\n  "
        + "\n  ".join(offenders)
        + "\nIt is the pre-ingest gate's `on_duplicate` finalizer: pass it down to "
          "ingest_file/ingest_text instead of calling it, so it runs inside the "
          "gate's transaction on the gate's worker thread. Calling it directly both "
          "takes the write lock on the loop and separates the record from the "
          "delete and location claim it must be atomic with."
    )


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


def test_resolve_old_item_ids_is_never_called_on_the_event_loop():
    """Ratchet: the full-source _old_item_ids read stays on a worker thread.

    ``_resolve_old_item_ids`` materializes one row per item in the source --
    tens of thousands on a large library, over a second of blocking SQLite --
    so the ingest paths resolve it inside the off-loop duplicate-gate hop. A
    call added straight to a coroutine body reintroduces the per-ingest loop
    stall this helper exists to prevent, so it must fail here rather than in
    production. Nested closures handed to ``run_to_completion`` (the gate
    hops) are not coroutine bodies and correctly do not trip this.
    """
    offenders = _on_loop_call_sites("_resolve_old_item_ids")
    assert offenders == [], (
        "_resolve_old_item_ids is reached from the event loop at:\n  "
        + "\n  ".join(offenders)
        + "\nResolve the group inside the off-loop duplicate-gate hop (the "
          "run_to_completion(_gate) closure) as ingest_file/ingest_text do, or "
          "wrap the call in asyncio.to_thread. The read is a full-source scan "
          "and must never run on the loop thread."
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
async def test_duplicate_skip_runs_the_delete_off_the_loop_thread(tmp_path):
    """The duplicate gate's delete executes on some other thread.

    The gate is reached through a plain ``def`` helper, so the lexical ratchet
    above cannot see the hop; only asserting the THREAD proves it is real.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.knowledge.ingestion import IngestionPipeline

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(
            return_value=[{"category": "document", "summary": "s", "entities": []}])
        chunker = MagicMock()
        chunker.chunk.side_effect = lambda text, **kw: [
            {"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]
        pipeline = IngestionPipeline(
            store=store, extractor=extractor, chunker=chunker,
            reader=MagicMock(), embedder=None)

        # Holder: another source already owns this exact text. Equal source_type
        # means neither outranks the other, which is the branch that refuses the
        # write -- and therefore the branch that deletes the superseded items.
        holder = store.add_source(name="holder", source_type="artifact",
                                  uri="artifact://holder")
        await pipeline.ingest_text("shared body", title="H", source_id=holder,
                                   old_item_ids=[])

        target = store.add_source(name="target", source_type="artifact",
                                  uri="artifact://target")
        await pipeline.ingest_text("its own body", title="T", source_id=target,
                                   old_item_ids=[])
        superseded = [r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (target,)).fetchall()]
        assert superseded, "no items to supersede -- setup would prove nothing"

        seen_threads: list[int] = []
        real = store.delete_items_batch_in_txn

        def recording(*args, **kwargs):
            seen_threads.append(threading.get_ident())
            return real(*args, **kwargs)

        store.delete_items_batch_in_txn = recording  # type: ignore[method-assign]

        job = await pipeline.ingest_text("shared body", title="T", source_id=target,
                                         old_item_ids=superseded)

        assert job, "the duplicate gate did not return its terminal job id"
        assert seen_threads, (
            "delete_items_batch_in_txn was never called -- the duplicate gate was "
            "not reached and this test would pass vacuously")
        assert threading.get_ident() not in seen_threads, (
            "the duplicate gate's delete ran on the event-loop thread; the whole "
            "gate must travel through run_to_completion")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_run_to_completion_forwards_the_return_value():
    """The hop is usable for work that reports a result, not just side effects.

    Without this the duplicate gate could only be offloaded by splitting its
    delete from the job id it returns, across an await -- the exact shape that
    strands committed data.
    """
    from kiro_crew.knowledge.ingestion import run_to_completion

    assert await run_to_completion(lambda: "job-1234") == "job-1234"
    assert await run_to_completion(lambda: None) is None


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


@pytest.mark.asyncio
async def test_duplicate_gate_reingests_when_the_holder_vanishes_before_the_lock(tmp_path):
    """A holder deleted between the probe and the write lock must not dedupe.

    The gate reads a holder and then makes the target DEPEND on it, so the two
    steps have to be one atomic unit. Moving the gate off the event loop let a
    concurrent source deletion land in the middle: the target got a terminal
    'skipped_duplicate' row while the copy it was attaching to was cascaded
    away, leaving the file on disk with its content unrecoverable.

    Simulate the interleaving deterministically by making the authoritative
    in-transaction lookup miss after the cheap probe has already hit. The gate
    must decline to dedupe and let a normal ingest proceed.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.knowledge.ingestion import IngestionPipeline

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(
            return_value=[{"category": "document", "summary": "s", "entities": []}])
        chunker = MagicMock()
        chunker.chunk.side_effect = lambda text, **kw: [
            {"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]
        pipeline = IngestionPipeline(
            store=store, extractor=extractor, chunker=chunker,
            reader=MagicMock(), embedder=None)

        holder = store.add_source(name="holder", source_type="artifact",
                                  uri="artifact://holder")
        await pipeline.ingest_text("shared body", title="H", source_id=holder,
                                   old_item_ids=[])
        target = store.add_source(name="target", source_type="artifact",
                                  uri="artifact://target")

        real_find = store.find_doc_by_content_hash
        calls: list[int] = []

        def vanishing(*args, **kwargs):
            calls.append(1)
            # First call is the unlocked probe (hit); the second is the
            # authoritative read under BEGIN IMMEDIATE -- by then the holder is
            # "deleted", so it must miss.
            return real_find(*args, **kwargs) if len(calls) == 1 else None

        store.find_doc_by_content_hash = vanishing  # type: ignore[method-assign]

        job = await pipeline.ingest_text("shared body", title="T",
                                         source_id=target, old_item_ids=[])

        assert len(calls) >= 2, (
            "the gate consulted the holder only once, so the holder is still "
            "read outside the write lock and this test proves nothing")
        rows = store.db.execute(
            "SELECT status FROM ingestion_jobs WHERE source_id = ?",
            (target,)).fetchall()
        assert rows, "the ingest recorded no job at all"
        assert all(r["status"] != "skipped_duplicate" for r in rows), (
            "target was marked a duplicate of a holder that no longer exists -- "
            f"its content is now unrecoverable (job={job}, rows={[dict(r) for r in rows]})")
        assert store.db.execute(
            "SELECT COUNT(*) FROM items WHERE source_id = ?",
            (target,)).fetchone()[0] > 0, (
            "target kept no items of its own after the holder vanished")
    finally:
        store.db.close()


@pytest.mark.asyncio
async def test_deduped_state_write_is_off_loop_and_keeps_a_late_adoption(tmp_path):
    """The terminal 'deduped' write runs off the loop AND keeps a cascade's adoption.

    Both properties belong to that one write. It takes the write lock, so running
    it on the loop thread makes ``BEGIN IMMEDIATE`` wait there on any concurrent
    writer -- the same stall this change exists to remove, reintroduced one line
    further along. And the group it records cannot be predicted:

    The pre-ingest gate commits its own transaction, so a user deleting the HOLDER
    lands in the window between that commit and this write. That cascade is benign
    by design -- it sees this folder's location row, reassigns the surviving item
    to this source, and adopts it into this very ``folder_file_state`` row. Writing
    ``item_ids='[]'`` afterwards -- which is what "the gate refused, so this file
    owns nothing" predicts -- erases the adoption and leaves the only remaining
    copy owned by this source but named by no state row: unreachable by the
    deleted-file path and undeletable, which is exactly the strand the ownership
    model exists to stop.

    The interleaving is produced deterministically by cascading the holder away the
    instant the gate returns its terminal job, with no threads involved. The scan
    must end with the row naming the item it now owns.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.knowledge.ingestion import IngestionPipeline

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        folder = tmp_path / "folder"
        folder.mkdir()
        (folder / "doc.md").write_text("shared body", encoding="utf-8")

        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(
            return_value=[{"category": "document", "summary": "s", "entities": []}])
        chunker = MagicMock()
        chunker.chunk.side_effect = lambda text, **kw: [
            {"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]
        reader = MagicMock()
        reader.read.return_value = ("shared body", {})
        pipeline = IngestionPipeline(
            store=store, extractor=extractor, chunker=chunker,
            reader=reader, embedder=None)

        # The holder owns this exact text first. Equal rank (both persistent
        # source types) is the branch that REFUSES the incoming copy -- a
        # transient holder would be outranked and the folder would just ingest.
        holder = store.add_source(name="holder", source_type="local_folder",
                                  uri=str(tmp_path / "other"))
        await pipeline.ingest_text("shared body", title="H", source_id=holder,
                                   old_item_ids=[])
        held = [r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (holder,)).fetchall()]
        assert held, "holder owns no items -- the gate would never refuse anything"

        source_id = store.add_source(name="folder", source_type="local_folder",
                                     uri=str(folder))

        watcher = FolderWatcher(store, pipeline)
        real_ingest = pipeline.ingest_file
        refused: list[str] = []

        async def cascade_between(*args, **kwargs):
            job = await real_ingest(*args, **kwargs)
            status = (pipeline.get_job_status(job) or {}).get("status") if job else None
            if status == "skipped_duplicate":
                refused.append(str(job))
                # The user deletes the holder HERE: after the gate committed its
                # attachment, before the scan records the file's terminal state.
                store.delete_source_cascade(holder)
            return job

        pipeline.ingest_file = cascade_between  # type: ignore[method-assign]

        seen_threads: list[int] = []
        real_record = watcher._record_deduped_state

        def recording(*args, **kwargs):
            seen_threads.append(threading.get_ident())
            return real_record(*args, **kwargs)

        watcher._record_deduped_state = recording  # type: ignore[method-assign]

        await watcher.scan_source(
            {"id": source_id, "uri": str(folder), "source_type": "local_folder",
             "properties": "{}"})

        assert refused, (
            "the pre-ingest gate never refused the file, so no cascade was "
            "interleaved and this test would pass vacuously")
        assert seen_threads, (
            "_record_deduped_state was never called -- the deduped branch was not "
            "reached and both assertions below would prove nothing")
        assert threading.get_ident() not in seen_threads, (
            "the terminal 'deduped' write ran on the event-loop thread; it takes "
            "the write lock, so it must travel through run_to_completion")

        owned = [r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]
        assert owned, (
            "the cascade left this source owning nothing, so there is no "
            "adoption to preserve and the assertion below proves nothing")

        row = store.db.execute(
            "SELECT item_ids, status FROM folder_file_state "
            "WHERE source_id = ?", (source_id,)).fetchone()
        assert row, "the scan recorded no state row for the file at all"
        named = json.loads(row["item_ids"] or "[]")
        assert sorted(named) == sorted(owned), (
            "the terminal 'deduped' write overwrote the adoption: this source "
            f"owns {owned} but its state row names {named}, so the last copy of "
            "the document is unreachable and undeletable "
            f"(status={row['status']!r})")
    finally:
        store.close()


def test_deduped_state_write_never_adopts_a_sibling_files_items(tmp_path):
    """The refused row must not name items belonging to another file's row.

    Two distinct files in one folder may legitimately hold identical text, so
    ``(source_id, content_hash)`` does not identify a document. Resolving the
    group by hash hands the refused row the SIBLING's items; both rows then name
    the same physical item, and deleting either file destroys the other's content
    while its file still sits on disk. ``_adopt_reassigned_item`` refuses an
    ambiguous hash for exactly this reason, and this write must not reintroduce
    the ambiguity it avoids.

    Seeded directly rather than driven through a scan: the point under test is
    which items the terminal write is willing to claim, and the sibling-hash
    collision has to be present for the question to mean anything.
    """
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        source_id = store.add_source("folder", "local_folder", str(tmp_path))
        # a.md owns this content, and its row names its own item.
        sibling_item = store.add_item("A", "shared body", "document",
                                      source_id=source_id)
        content_hash = "a" * 64
        store.db.execute("UPDATE items SET content_hash = ? WHERE id = ?",
                         (content_hash, sibling_item))
        assert store.db.execute(
            "SELECT 1 FROM items WHERE source_id = ? AND content_hash = ?",
            (source_id, content_hash)).fetchone(), (
            "the sibling item is not reachable by (source_id, content_hash), so a "
            "hash-based lookup could not have claimed it and this proves nothing")
        now = "2026-01-01T00:00:00"
        store.db.execute(
            "INSERT OR REPLACE INTO folder_file_state (source_id, file_path, "
            "content_hash, text_hash, mtime, item_ids, last_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'done')",
            (source_id, "a.md", content_hash, content_hash, 1.0,
             json.dumps([sibling_item]), now))
        # b.md holds the same text and was just refused by the pre-ingest gate.
        # It owns nothing: its row has never named an item.
        store.db.execute(
            "INSERT OR REPLACE INTO folder_file_state (source_id, file_path, "
            "content_hash, text_hash, mtime, item_ids, last_seen, status) "
            "VALUES (?, ?, ?, NULL, ?, '[]', ?, 'scanning')",
            (source_id, "b.md", content_hash, 2.0, now))

        watcher = FolderWatcher(store, MagicMock())
        watcher._record_deduped_state(source_id, "b.md", content_hash, 2.0, now)

        b_row = store.db.execute(
            "SELECT item_ids, status FROM folder_file_state "
            "WHERE source_id = ? AND file_path = 'b.md'", (source_id,)).fetchone()
        assert json.loads(b_row["item_ids"] or "[]") == [], (
            "the refused row claimed a sibling file's items "
            f"({b_row['item_ids']}); deleting b.md would now delete a.md's only "
            "item while a.md is still on disk")
        assert b_row["status"] == "deduped", (
            f"a row that owns nothing must stay 'deduped', got {b_row['status']!r}")
        a_row = store.db.execute(
            "SELECT item_ids FROM folder_file_state "
            "WHERE source_id = ? AND file_path = 'a.md'", (source_id,)).fetchone()
        assert json.loads(a_row["item_ids"]) == [sibling_item], (
            "a.md's own group was disturbed by a write about b.md")
    finally:
        store.close()


def test_deduped_state_write_drops_the_group_the_gate_just_deleted(tmp_path):
    """A stale pre-ingest group must not survive into the terminal row.

    The scan writes a ``scanning`` marker naming the items this file is about to
    REPLACE, and the gate then deletes exactly those items. Preserving the row's
    ``item_ids`` verbatim would leave the terminal row naming deleted items --
    a group that cannot be re-deleted and reports content the Library no longer
    has. Only ids that still exist under this source survive.
    """
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        source_id = store.add_source("folder", "local_folder", str(tmp_path))
        gone = "11111111-1111-1111-1111-111111111111"
        now = "2026-01-01T00:00:00"
        store.db.execute(
            "INSERT OR REPLACE INTO folder_file_state (source_id, file_path, "
            "content_hash, text_hash, mtime, item_ids, last_seen, status) "
            "VALUES (?, ?, 'h', NULL, ?, ?, ?, 'scanning')",
            (source_id, "c.md", 3.0, json.dumps([gone]), now))
        assert not store.db.execute(
            "SELECT 1 FROM items WHERE id = ?", (gone,)).fetchone(), (
            "the supposedly-deleted item exists, so the filter under test would "
            "have nothing to drop")

        watcher = FolderWatcher(store, MagicMock())
        watcher._record_deduped_state(source_id, "c.md", "h", 3.0, now)

        row = store.db.execute(
            "SELECT item_ids, status FROM folder_file_state "
            "WHERE source_id = ? AND file_path = 'c.md'", (source_id,)).fetchone()
        assert json.loads(row["item_ids"] or "[]") == [], (
            f"the terminal row kept a deleted item ({row['item_ids']})")
        assert row["status"] == "deduped", (
            f"expected 'deduped' for a row owning nothing, got {row['status']!r}")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_deduped_state_write_recovers_a_transformed_files_reassigned_item(tmp_path):
    """A transformed file must not be stranded when adoption silently matched nothing.

    ``folder_file_state.content_hash`` is over the file's RAW BYTES;
    ``items.content_hash`` is over the EXTRACTED TEXT. For .md they coincide, so a
    plaintext test cannot see this: for anything the reader transforms -- PDF,
    DOCX, HTML -- they differ. ``_adopt_reassigned_item`` matches a folder row on
    ``COALESCE(text_hash, content_hash)``, and a row the gate refused has no
    ``text_hash`` of its own, so the match falls back to the bytes hash, finds
    nothing, and adopts nothing WITHOUT logging. The reassignment ahead of it
    still happened.

    So after a ``delete_source_cascade`` on the holder, this source OWNS the
    surviving item while no state row names it: unreachable by the deleted-file
    path and undeletable. This test exists to pin that gap with a live repro
    rather than a comment, and to fail loudly once the text hash is plumbed
    through.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.knowledge.ingestion import IngestionPipeline

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        folder = tmp_path / "folder"
        folder.mkdir()
        # Bytes the reader transforms: the file's own bytes hash can never equal
        # the hash of the text extracted from it.
        (folder / "doc.pdf").write_bytes(b"%PDF-1.4 not the extracted text")
        extracted = "shared body"

        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(
            return_value=[{"category": "document", "summary": "s", "entities": []}])
        chunker = MagicMock()
        chunker.chunk.side_effect = lambda text, **kw: [
            {"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]
        reader = MagicMock()
        reader.read.return_value = (extracted, {})
        pipeline = IngestionPipeline(
            store=store, extractor=extractor, chunker=chunker,
            reader=reader, embedder=None)

        holder = store.add_source(name="holder", source_type="local_folder",
                                  uri=str(tmp_path / "other"))
        await pipeline.ingest_text(extracted, title="H", source_id=holder,
                                   old_item_ids=[])
        assert store.db.execute(
            "SELECT COUNT(*) FROM items WHERE source_id = ?",
            (holder,)).fetchone()[0], "holder owns nothing; the gate would not refuse"

        source_id = store.add_source(name="folder", source_type="local_folder",
                                     uri=str(folder))
        watcher = FolderWatcher(store, pipeline)
        real_ingest = pipeline.ingest_file
        refused: list[str] = []

        async def cascade_between(*args, **kwargs):
            job = await real_ingest(*args, **kwargs)
            status = (pipeline.get_job_status(job) or {}).get("status") if job else None
            if status == "skipped_duplicate":
                refused.append(str(job))
                store.delete_source_cascade(holder)
            return job

        pipeline.ingest_file = cascade_between  # type: ignore[method-assign]

        await watcher.scan_source(
            {"id": source_id, "uri": str(folder), "source_type": "local_folder",
             "properties": "{}"})

        assert refused, (
            "the gate never refused the file, so no cascade was interleaved and "
            "this test would pass vacuously")
        row = store.db.execute(
            "SELECT content_hash, text_hash, item_ids, status FROM folder_file_state "
            "WHERE source_id = ?", (source_id,)).fetchone()
        assert row, "the scan recorded no state row at all"
        owned = [r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]
        assert owned, (
            "the cascade left this source owning nothing, so there is no strand "
            "to recover and the assertion below proves nothing")
        text_hash = store.db.execute(
            "SELECT content_hash FROM items WHERE id = ?", (owned[0],)).fetchone()[0]
        assert text_hash != row["content_hash"], (
            "the byte hash and the text hash are equal, so this test is exercising "
            "the plaintext path and not the transformed-file one it exists for")

        named = json.loads(row["item_ids"] or "[]")
        assert sorted(named) == sorted(owned), (
            "the transformed file was stranded: this source owns "
            f"{owned} but its row names {named}, so the only copy of the document "
            "is undeletable and unreachable by the deleted-file path "
            f"(status={row['status']!r})")
    finally:
        store.close()


def test_deduped_state_write_propagates_an_unreadable_group(tmp_path):
    """An unreadable ``item_ids`` must not be silently rewritten as 'owns nothing'.

    The caller writes whatever the derivation returns as this row's terminal
    state, so mapping a corrupt value to ``[]`` would overwrite the only record of
    the group and orphan every item it named. Propagating leaves the row on its
    ``scanning`` marker, which the next sweep retries.
    """
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        source_id = store.add_source("folder", "local_folder", str(tmp_path))
        now = "2026-01-01T00:00:00"
        store.db.execute(
            "INSERT OR REPLACE INTO folder_file_state (source_id, file_path, "
            "content_hash, text_hash, mtime, item_ids, last_seen, status) "
            "VALUES (?, 'd.md', 'h', NULL, 4.0, 'not json at all', ?, 'scanning')",
            (source_id, now))

        watcher = FolderWatcher(store, MagicMock())
        with pytest.raises(RuntimeError, match="item_ids unreadable"):
            watcher._record_deduped_state(source_id, "d.md", "h", 4.0, now)

        row = store.db.execute(
            "SELECT item_ids, status FROM folder_file_state "
            "WHERE source_id = ? AND file_path = 'd.md'", (source_id,)).fetchone()
        assert row["item_ids"] == "not json at all", (
            "the corrupt group was overwritten instead of preserved for retry")
        assert row["status"] == "scanning", (
            f"expected the retryable marker to survive, got {row['status']!r}")
    finally:
        store.close()


def test_artifact_deduped_state_write_keeps_a_late_adoption(tmp_path):
    """The aggregate Artifacts path must not clobber a cascade's adoption either.

    Same window as the folder path, in a different table: the gate commits, a
    ``delete_source_cascade`` on the holder reassigns the surviving item to this
    source and adopts it into this ``artifact_item_state`` row, and the caller then
    writes its terminal state. Predicting an empty group there erases the adoption
    and leaves the last copy of the content owned but named by no row.

    Reachable more easily here than for folders: ``_OWNERSHIP_HASH_COL`` maps the
    aggregate tables to ``content_hash`` directly, which is already the text-hash
    domain, so ``_adopt_reassigned_item`` matches and adopts rather than silently
    missing the way it does for a transformed file.
    """
    from kiro_crew.knowledge.artifact_ingest import _record_deduped_state

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        source_id = store.add_source("artifacts", "artifact", "artifact://all")
        item = store.add_item("A", "shared body", "document", source_id=source_id)
        text_hash = "b" * 64
        store.db.execute("UPDATE items SET content_hash = ? WHERE id = ?",
                         (text_hash, item))
        # The cascade already adopted the reassigned item into this row.
        store.db.execute(
            "INSERT OR REPLACE INTO artifact_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
            "VALUES (?, 'doc', ?, ?, '2026-01-01T00:00:00', 'Doc', 'active')",
            (source_id, text_hash, json.dumps([item])))

        _record_deduped_state(store, source_id, "doc", text_hash, "Doc", "markdown")

        row = store.db.execute(
            "SELECT item_ids, status FROM artifact_item_state "
            "WHERE source_id = ? AND slug = 'doc'", (source_id,)).fetchone()
        assert json.loads(row["item_ids"] or "[]") == [item], (
            "the terminal write erased the adoption: the row names "
            f"{row['item_ids']} while this source owns {item}, so the only copy is "
            "unreachable by the delete path")
        assert row["status"] == "active", (
            "a row that owns items must be 'active' -- find_document_by_hash only "
            f"matches 'active', so {row['status']!r} would let the same text in "
            "again under a second slug")
    finally:
        store.close()


def test_agent_deduped_state_write_keeps_a_late_adoption(tmp_path):
    """Same for the agent aggregate: an adopted group survives the terminal write."""
    from kiro_crew.knowledge.agent_source import _record_deduped_state

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        source_id = store.add_source("agent", "agent", "agent://all")
        item = store.add_item("A", "shared body", "document", source_id=source_id)
        text_hash = "c" * 64
        store.db.execute("UPDATE items SET content_hash = ? WHERE id = ?",
                         (text_hash, item))
        store.db.execute(
            "INSERT OR REPLACE INTO agent_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
            "VALUES (?, 'doc', ?, ?, '2026-01-01T00:00:00', 'Doc', 'active')",
            (source_id, text_hash, json.dumps([item])))

        _record_deduped_state(store, source_id, "doc", text_hash, "Doc")

        row = store.db.execute(
            "SELECT item_ids, status FROM agent_item_state "
            "WHERE source_id = ? AND slug = 'doc'", (source_id,)).fetchone()
        assert json.loads(row["item_ids"] or "[]") == [item], (
            f"the terminal write erased the adoption: row names {row['item_ids']}, "
            f"source owns {item}")
        assert row["status"] == "active", (
            f"a row owning items must be 'active', got {row['status']!r}")
    finally:
        store.close()


def test_aggregate_deduped_state_write_records_an_empty_group_when_nothing_adopted(
        tmp_path):
    """With no adoption to preserve, the row still records 'deduped' and no group.

    The derivation must not invent ownership: the common case is that the gate
    refused and nothing was reassigned, and that row has to stay ``deduped`` so
    the owning sync does not re-attempt a write the gate will refuse again.
    """
    from kiro_crew.knowledge.agent_source import _record_deduped_state

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        source_id = store.add_source("agent", "agent", "agent://all")
        gone = "22222222-2222-2222-2222-222222222222"
        store.db.execute(
            "INSERT OR REPLACE INTO agent_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
            "VALUES (?, 'doc', 'h', ?, '2026-01-01T00:00:00', 'Doc', 'active')",
            (source_id, json.dumps([gone])))

        _record_deduped_state(store, source_id, "doc", "h", "Doc")

        row = store.db.execute(
            "SELECT item_ids, status FROM agent_item_state "
            "WHERE source_id = ? AND slug = 'doc'", (source_id,)).fetchone()
        assert json.loads(row["item_ids"] or "[]") == [], (
            f"the row kept a group the gate deleted ({row['item_ids']})")
        assert row["status"] == "deduped", (
            f"expected 'deduped' for a row owning nothing, got {row['status']!r}")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_duplicate_gate_records_terminal_state_even_when_cancelled(tmp_path):
    """A cancellation must not split the gate's commit from the caller's record.

    ``run_to_completion`` guarantees the gate FINISHES and then re-raises the
    cancellation, so anything the caller was going to do afterwards is skipped by
    construction -- not merely at risk. The gate commits the delete of the previous
    group, the location claim on the holder's items and the terminal job row, so a
    shutdown landing there used to leave all three durable with no state row naming
    them: the claim cannot be detached (a ``scanning`` row has no ``text_hash``, so
    the detach short-circuits) and the content is orphaned.

    Passing the caller's write in as ``on_duplicate`` puts it inside the same hop.
    Here the awaiting task is cancelled while the gate is in flight; the finalizer
    must still have run.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.knowledge.ingestion import IngestionPipeline

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(
            return_value=[{"category": "document", "summary": "s", "entities": []}])
        chunker = MagicMock()
        chunker.chunk.side_effect = lambda text, **kw: [
            {"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]
        pipeline = IngestionPipeline(
            store=store, extractor=extractor, chunker=chunker,
            reader=MagicMock(), embedder=None)

        holder = store.add_source(name="holder", source_type="local_folder",
                                  uri=str(tmp_path / "other"))
        await pipeline.ingest_text("shared body", title="H", source_id=holder,
                                   old_item_ids=[])
        target = store.add_source(name="target", source_type="local_folder",
                                  uri=str(tmp_path / "target"))

        recorded: list[str] = []
        started = asyncio.Event()
        release = threading.Event()

        def finalizer(_text_hash: str) -> None:
            recorded.append("terminal state written")

        real_gate = pipeline._skip_as_duplicate
        loop = asyncio.get_running_loop()

        def slow_gate(*args, **kwargs):
            # Hold the gate on its worker thread until the test has cancelled the
            # awaiting task. No sleep: an arbitrary wall-clock delay would both race
            # on a slow runner and block a shared xdist worker, which perturbs other
            # cancellation-timing tests. The handshake makes the shutdown ordering
            # exact -- gate in flight, cancellation delivered, gate then commits.
            loop.call_soon_threadsafe(started.set)
            release.wait(timeout=10)
            return real_gate(*args, **kwargs)

        pipeline._skip_as_duplicate = slow_gate  # type: ignore[method-assign]

        task = asyncio.ensure_future(pipeline.ingest_text(
            "shared body", title="T", source_id=target, old_item_ids=[],
            on_duplicate=finalizer))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert recorded == ["terminal state written"], (
            "the gate committed its delete, location claim and terminal job row, "
            "then the cancellation skipped the caller's state write -- the claim is "
            "now undetachable and the content orphaned. The write must run inside "
            "the gate's own run_to_completion hop, as on_duplicate.")
        rows = store.db.execute(
            "SELECT status FROM ingestion_jobs WHERE source_id = ?",
            (target,)).fetchall()
        assert any(r["status"] == "skipped_duplicate" for r in rows), (
            "the gate did not actually refuse the write, so this test would pass "
            "vacuously")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_duplicate_gate_and_terminal_state_are_one_transaction(tmp_path):
    """The claim and the row that names it land together, or neither lands.

    The finalizer runs inside the gate's own transaction, not after its commit. That
    ordering is what a first-time aggregate document depends on: after the commit its
    state row does not exist yet, so a ``delete_source_cascade`` landing in the gap
    reassigns the surviving item to this source and then has nothing to adopt it
    into -- ``_adopt_reassigned_item`` matches on ``(source_id, hash)``, finds no
    row, and returns without logging. The record that follows reports an empty group
    while the source owns the item.

    Atomicity is asserted the direct way: make the finalizer raise, and require that
    the gate's deletion, its location claim and its terminal job row are all absent
    afterwards. If the finalizer ran after ``COMMIT`` they would survive.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.knowledge.ingestion import IngestionPipeline

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(
            return_value=[{"category": "document", "summary": "s", "entities": []}])
        chunker = MagicMock()
        chunker.chunk.side_effect = lambda text, **kw: [
            {"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]
        pipeline = IngestionPipeline(
            store=store, extractor=extractor, chunker=chunker,
            reader=MagicMock(), embedder=None)

        holder = store.add_source(name="holder", source_type="local_folder",
                                  uri=str(tmp_path / "other"))
        await pipeline.ingest_text("shared body", title="H", source_id=holder,
                                   old_item_ids=[])
        held = [r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (holder,)).fetchall()]
        assert held, "holder owns nothing; the gate would not refuse"

        target = store.add_source(name="target", source_type="local_folder",
                                  uri=str(tmp_path / "target"))
        await pipeline.ingest_text("its own body", title="T", source_id=target,
                                   old_item_ids=[])
        superseded = [r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (target,)).fetchall()]
        assert superseded, "nothing to supersede -- the delete under test is a no-op"

        def exploding_finalizer(_text_hash: str) -> None:
            raise RuntimeError("terminal state write failed")

        with pytest.raises(RuntimeError, match="terminal state write failed"):
            await pipeline.ingest_text(
                "shared body", title="T", source_id=target,
                old_item_ids=superseded, on_duplicate=exploding_finalizer)

        still_there = [r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE id IN (%s)"  # noqa: S608
            % ",".join("?" for _ in superseded), superseded).fetchall()]
        assert sorted(still_there) == sorted(superseded), (
            "the gate's delete of the superseded group survived a failed terminal "
            "state write, so the two are not one transaction: the items are gone "
            "and nothing records why")
        claims = store.db.execute(
            "SELECT COUNT(*) FROM source_locations "
            "WHERE source_id = ? AND item_id IN (%s)"  # noqa: S608
            % ",".join("?" for _ in held), (target, *held)).fetchone()[0]
        assert claims == 0, (
            f"{claims} claim(s) on the HOLDER's items survived a failed terminal "
            "state write; an unnamed claim cannot be detached and orphans them")
        dupe_jobs = store.db.execute(
            "SELECT COUNT(*) FROM ingestion_jobs "
            "WHERE source_id = ? AND status = 'skipped_duplicate'",
            (target,)).fetchone()[0]
        assert dupe_jobs == 0, (
            "a terminal skipped_duplicate job survived a failed state write, so a "
            "caller would read 'already present' for a refusal that was rolled back")
    finally:
        store.close()
