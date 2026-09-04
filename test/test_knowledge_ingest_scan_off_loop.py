"""``_ingest_file`` must neither query per file nor await after the commit.

Two defects meet in this one function, and the fix for the first used to create
the second -- so both are ratcheted here together.

**The query.** The scan used to learn which items a file produced by reading the
source's entire item-id set BEFORE and AFTER every single file and diffing the two.
``idx_items_source_id`` keeps each read an index scan rather than a table scan, but
it still materializes one row per item in the SOURCE -- about 20k rows on a large
folder source, measured at over a second per call -- twice per file, synchronously
on the event loop. That trips the loop-stall watchdog, which exits the process; the
supervisor respawns it, the boot scan re-enters the same reads, and the gateway
crash-loops. Observed against a 2709-file source: four watchdog exits inside 17
minutes, with 7-9s of loop lag still reported between them.

**The orphan window.** Merely moving those reads to a worker thread introduces a
DIFFERENT bug, which is why this file also ratchets awaits. The pipeline commits
the new items inside its own uncancellable ``run_to_completion`` finalize hop, and
the caller (``_do_scan``) writes the ``folder_file_state`` row that NAMES them only
after ``_ingest_file`` returns. Any await between that commit and this function's
return is therefore a window where a gateway shutdown cancels the coroutine,
committed items end up named by no state row, and the next scan re-ingests the file
-- duplicating them while the first group stays untracked and undeletable.

Both are closed by the same change: the pipeline already reports its created ids
through ``on_committed``, invoked INSIDE the finalize hop that commits them. Taking
the ids from there means no read to offload and no post-commit await to cancel.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sqlite3

import pytest

from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.store import KnowledgeStore

# A nested def / lambda is a separate execution frame -- a sync helper or a thread
# target -- so a call inside one is not running on the loop.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

_WATCHER_SRC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "knowledge"
    / "folder_watcher.py"
)

# Names that only ever appear as a synchronous sqlite round-trip.
_DB_CALLS = {"execute", "executemany", "fetchall", "fetchone", "executescript"}


def _direct_calls(body: list[ast.stmt]) -> set[tuple[str, int]]:
    """Every name called directly in *body*, paired with its line number.

    Nested scopes are skipped: a call inside a nested ``def``/``lambda`` runs in
    that frame -- a sync helper, or a thread target -- not in the enclosing one.
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
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if called:
            out.add((called, node.lineno))
    return out


def _async_def(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"async def {name} no longer exists in folder_watcher.py -- this ratchet "
        "is guarding nothing and must be repointed at whatever replaced it"
    )


def _awaits(fn: ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    """``(lineno, callee)`` for every await in *fn*'s own frame, in source order."""
    out: list[tuple[int, str]] = []
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED_SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(node))
        if not isinstance(node, ast.Await):
            continue
        value = node.value
        callee = "<expr>"
        if isinstance(value, ast.Call):
            func = value.func
            callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "<expr>")
        out.append((node.lineno, callee))
    return sorted(out)


def test_ingest_file_issues_no_sqlite_call_on_the_event_loop():
    """Ratchet: the coroutine body runs no synchronous sqlite round-trip.

    Scoped to this one coroutine on purpose. A repo-wide ban on synchronous
    ``execute`` would light up hundreds of pre-existing sites and say nothing about
    this defect; this function is the one that ran a full per-source id read twice
    for every file in a scan, so it is the one that must stay clean.
    """
    tree = ast.parse(_WATCHER_SRC.read_text(errors="replace"))
    fn = _async_def(tree, "_ingest_file")
    offenders = sorted(
        f"folder_watcher.py:{lineno} calls {called}() directly"
        for called, lineno in _direct_calls(fn.body)
        if called in _DB_CALLS
    )
    assert offenders == [], (
        "_ingest_file runs sqlite on the event loop at:\n  "
        + "\n  ".join(offenders)
        + "\nIt does not need to: the pipeline reports the ids it created through "
        "the on_committed callback, from inside the finalize hop that commits "
        "them. Reading them back costs ~20k rows twice per file and, after the "
        "commit, opens the orphan window the next test guards."
    )


def test_ingest_file_never_awaits_after_the_pipeline_commits():
    """Ratchet: the only awaits are the pipeline call and drained state writes.

    This is the invariant that makes the orphan window unreachable rather than
    merely unlikely. The caller writes the ``folder_file_state`` row naming the new
    items only after this function RETURNS, so an await that a cancellation can
    SKIP PAST -- a bare ``asyncio.to_thread`` hop added in good faith to get a
    query off the loop -- lets a shutdown cancel the coroutine after the pipeline
    has already committed. The items survive, nothing names them, and the next
    scan re-ingests the file alongside them.

    ``_persist_state`` is admitted because it is the opposite shape: it is
    ``run_to_completion``-backed, so the state write it carries runs to completion
    even when the coroutine is cancelled mid-await -- the same guarantee the
    pipeline's own finalize hop relies on. It is how the failure branches record
    their terminal ``failed`` row without a synchronous sqlite call on the loop
    (the sqlite ratchet above), and every such branch is one where the pipeline
    never committed, so there is no committed group for the await to orphan.
    """
    tree = ast.parse(_WATCHER_SRC.read_text(errors="replace"))
    fn = _async_def(tree, "_ingest_file")
    awaits = _awaits(fn)
    rendered = ", ".join(f"line {ln}: await {name}(...)" for ln, name in awaits)
    names = [name for _, name in awaits]
    assert names.count("ingest_file") == 1 and set(names) <= {"ingest_file", "_persist_state"}, (
        f"_ingest_file has awaits other than the pipeline call ({rendered}).\n"
        "Every await after the pipeline's commit that a cancellation can skip is an "
        "orphan window: a shutdown cancelling there leaves committed items that no "
        "folder_file_state row names, so the next scan duplicates them and the "
        "first group is undeletable. If you need data the pipeline holds, take it "
        "through a callback that runs inside its finalize hop -- do not read it "
        "back. A state WRITE belongs in `_persist_state`, which drains under "
        "cancellation."
    )


def test_ingest_file_takes_its_item_ids_from_the_commit_callback():
    """The ids come from ``on_committed``, not from a read-back.

    Guards the two ratchets above against passing because the bookkeeping was
    simply deleted: a scan that reports no item ids silently stops attributing
    items to files, which breaks deletion and re-ingest detection.
    """
    source = _WATCHER_SRC.read_text(errors="replace")
    assert "on_committed=" in source, (
        "folder_watcher no longer passes on_committed to the pipeline, so it has "
        "no way to learn which items a file produced"
    )
    tree = ast.parse(source)
    fn = _async_def(tree, "_ingest_file")
    body = ast.get_source_segment(source, fn) or ""
    assert (
        "SELECT id FROM items" not in body
    ), "_ingest_file still reads the source's item-id set back"
    assert "on_committed=_record_committed" in body, (
        "_ingest_file does not hand its own recorder to the pipeline, so the "
        "committed ids never reach it"
    )


@pytest.mark.asyncio
async def test_ingest_file_reports_the_committed_ids_without_touching_sqlite(tmp_path):
    """Behavioural: the right ids come back, and the loop's connection stays idle.

    Asserted by tracing the LOOP THREAD's own sqlite connection rather than by
    patching a method. ``store.db`` is per-thread, so that connection sees a
    statement only if it was executed on the loop thread -- the exact property
    under test, and one a lexical check cannot fake.
    """
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        source_id = store.add_source("folder", "local_folder", str(tmp_path))
        created: list[str] = []

        class _Pipeline:
            """Stand-in that really inserts, then reports through on_committed."""

            async def ingest_file(
                self,
                path,
                *,
                source_id,
                namespace,
                original_name,
                old_item_ids,
                on_committed=None,
                on_duplicate=None,
            ):
                def _insert_and_report() -> None:
                    # Mirrors the real finalize hop: insert, then hand the ids to
                    # the callback inside the same uncancellable unit.
                    created.append(store.add_item("T", "body", "document", source_id=source_id))
                    if on_committed is not None:
                        on_committed(list(created))

                await asyncio.to_thread(_insert_and_report)
                return "job-1"

            def get_job_status(self, job_id):
                return {"status": "completed"}

        watcher = FolderWatcher(store, _Pipeline())

        # Touch `db` on the loop thread first so the traced connection is the one
        # the loop would reuse, then record every statement it executes.
        loop_conn = store.db
        traced: list[str] = []
        loop_conn.set_trace_callback(traced.append)
        try:
            item_ids, outcome = await watcher._ingest_file(str(doc), source_id, "default", {}, [])
        finally:
            loop_conn.set_trace_callback(None)

        assert outcome == "done", (
            f"the ingest did not take the success path (outcome={outcome!r}), so "
            "this test would prove nothing about it"
        )
        assert created, "the stand-in pipeline inserted nothing"
        assert item_ids == created, (
            f"reported {item_ids} but the pipeline created {created} -- the scan "
            "no longer attributes items to files"
        )

        on_loop = [s for s in traced if "FROM items" in s or "sync_status" in s]
        assert on_loop == [], (
            "these ingest queries ran on the event-loop thread's connection:\n  "
            + "\n  ".join(on_loop)
            + "\nOn a real source each is ~20k rows, per file; on the loop they "
            "trip the stall watchdog and crash-loop the gateway."
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ingest_file_reports_failure_when_the_pipeline_never_commits(tmp_path):
    """A silent rollback is still detected, without reading ``sync_status``.

    The pipeline invokes ``on_committed`` only on the branch that actually commits
    a group, so an unset callback IS the rollback signal. Losing this would make a
    partial failure look like a successful ingest that produced no items: the file
    would be recorded ``done`` and never retried.
    """
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        source_id = store.add_source("folder", "local_folder", str(tmp_path))

        class _RollingBackPipeline:
            async def ingest_file(
                self,
                path,
                *,
                source_id,
                namespace,
                original_name,
                old_item_ids,
                on_committed=None,
                on_duplicate=None,
            ):
                # Rolls back internally and does NOT raise -- exactly the shape
                # the old sync_status read existed to catch.
                return "job-1"

            def get_job_status(self, job_id):
                return {"status": "error"}

        watcher = FolderWatcher(store, _RollingBackPipeline())
        item_ids, outcome = await watcher._ingest_file(str(doc), source_id, "default", {}, [])

        assert outcome == "failed", (
            f"a silent rollback was reported as {outcome!r}; the file would be "
            "recorded terminal and never retried"
        )
        assert item_ids is None, f"a failed ingest reported item ids: {item_ids!r}"
        row = store.db.execute(
            "SELECT status, error_message FROM folder_file_state "
            "WHERE source_id = ? AND file_path = ?",
            (source_id, str(doc)),
        ).fetchone()
        assert (
            row is not None and row["status"] == "failed"
        ), f"no 'failed' state row was written for the rollback (row={row and dict(row)})"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ingest_file_still_reports_a_refused_duplicate_as_deduped(tmp_path):
    """The duplicate branch is unaffected by sourcing ids from the callback.

    The pre-ingest gate refuses the write and never commits a group, so
    ``on_committed`` does not fire there either -- the same condition as a
    rollback. The refusal is reported the same way the commit is: through
    ``on_duplicate``, invoked inside the gate's own transaction (the only place
    ``DUPLICATE_JOB_STATUS`` is ever written). The duplicate check must therefore
    keep winning, or a legitimately refused file would be recorded ``failed`` and
    retried on every scan.

    The fake follows the real gate's contract and fires the callback. It also
    exposes ``get_job_status`` and asserts it is never consulted: reading the
    status back after the pipeline returned was a synchronous sqlite round-trip
    on the event loop for every ingested file.
    """
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        source_id = store.add_source("folder", "local_folder", str(tmp_path))

        class _RefusingPipeline:
            status_reads = 0

            async def ingest_file(
                self,
                path,
                *,
                source_id,
                namespace,
                original_name,
                old_item_ids,
                on_committed=None,
                on_duplicate=None,
            ):
                assert on_duplicate is not None, "the watcher no longer listens for a refusal"
                on_duplicate("text-hash-of-body")
                return "job-dupe"

            def get_job_status(self, job_id):
                self.status_reads += 1
                return {"status": "skipped_duplicate"}

        pipeline = _RefusingPipeline()
        watcher = FolderWatcher(store, pipeline)
        item_ids, outcome = await watcher._ingest_file(str(doc), source_id, "default", {}, [])

        assert outcome == "deduped", (
            f"a refused duplicate was reported as {outcome!r} -- 'failed' would "
            "make every scan retry a write the gate will refuse again"
        )
        assert item_ids == [], f"a deduped file claimed ownership of {item_ids!r}"
        assert pipeline.status_reads == 0, (
            "the watcher read the job status back after the pipeline returned -- a "
            "synchronous sqlite query on the event loop; the refusal arrives through "
            "on_duplicate"
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ingest_file_forwards_the_refusal_to_the_callers_on_duplicate(tmp_path):
    """Latching the refusal must not swallow the caller's own ``on_duplicate``.

    ``_do_scan`` passes one that writes the 'deduped' state row inside the gate's
    transaction; a wrapper that only set its flag would leave that row unwritten.
    """
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        source_id = store.add_source("folder", "local_folder", str(tmp_path))

        class _RefusingPipeline:
            async def ingest_file(self, path, *, on_duplicate=None, **kw):
                on_duplicate("text-hash-of-body")
                return "job-dupe"

        seen: list[str] = []
        watcher = FolderWatcher(store, _RefusingPipeline())
        _, outcome = await watcher._ingest_file(
            str(doc), source_id, "default", {}, [], on_duplicate=seen.append
        )
        assert outcome == "deduped"
        assert seen == ["text-hash-of-body"], (
            f"the caller's on_duplicate saw {seen!r}; the text hash the gate matched "
            "must reach it unchanged"
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ingest_file_lets_cancellation_through_without_a_failed_state_row(tmp_path):
    """A cancelled ingest must not be recorded as an ingest failure.

    ``CancelledError`` is a ``BaseException``, so the function's ``except
    Exception`` does not catch it and the file keeps its retryable ``scanning``
    marker. Widening that handler would write a terminal ``failed`` row on every
    gateway shutdown, and the next scan would skip a file that was never broken.
    """
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        source_id = store.add_source("folder", "local_folder", str(tmp_path))
        started = asyncio.Event()

        class _HangingPipeline:
            async def ingest_file(self, path, **kwargs):
                started.set()
                await asyncio.Event().wait()  # never completes

            def get_job_status(self, job_id):  # pragma: no cover - not reached
                return {}

        watcher = FolderWatcher(store, _HangingPipeline())
        task = asyncio.ensure_future(watcher._ingest_file(str(doc), source_id, "default", {}, []))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        row = store.db.execute(
            "SELECT status FROM folder_file_state WHERE source_id = ? " "AND file_path = ?",
            (source_id, str(doc)),
        ).fetchone()
        assert row is None or row["status"] != "failed", (
            "a cancellation was recorded as a terminal 'failed' state row; a "
            "shutdown mid-scan would make the next scan skip a healthy file"
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_commit_callback_persists_the_state_row_before_returning(tmp_path):
    """The committed group reaches the state row INSIDE the callback.

    Capturing the ids in memory is not enough: the pipeline awaits again after
    its finalize hop (``generate_source_summary``), so a shutdown cancelling
    there strands a committed group the caller never gets to record -- the
    'scanning' marker survives and the next sweep re-ingests the file alongside
    the untracked first group. This asserts the row is already terminal, and
    names the group, at the moment ``on_committed`` returns -- observed from
    inside the stand-in pipeline, before ``_ingest_file`` has a chance to run
    any code after its await.
    """
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        source_id = store.add_source("folder", "local_folder", str(tmp_path))
        seen_inside: dict = {}

        class _Pipeline:
            """Really inserts, fires the callback, then reads the row back --
            all inside the same worker hop, mirroring the real finalize."""

            async def ingest_file(
                self,
                path,
                *,
                source_id,
                namespace,
                original_name,
                old_item_ids,
                on_committed=None,
                on_duplicate=None,
            ):
                def _insert_report_and_observe() -> None:
                    item_id = store.add_item("T", "body", "document", source_id=source_id)
                    if on_committed is not None:
                        on_committed([item_id])
                    row = store.db.execute(
                        "SELECT item_ids, status FROM folder_file_state "
                        "WHERE source_id = ? AND file_path = ?",
                        (source_id, str(doc)),
                    ).fetchone()
                    seen_inside["row"] = dict(row) if row else None
                    seen_inside["item_id"] = item_id

                await asyncio.to_thread(_insert_report_and_observe)
                return "job-1"

            def get_job_status(self, job_id):
                return {"status": "completed"}

        watcher = FolderWatcher(store, _Pipeline())
        # The scan always writes a 'scanning' marker before invoking
        # _ingest_file; the callback's targeted UPDATE lands on that row.
        watcher._update_state(
            source_id, str(doc), "hash", 1.0, "[]", "2026-01-01T00:00:00", "scanning", attempts=1
        )

        item_ids, outcome = await watcher._ingest_file(str(doc), source_id, "default", {}, [])

        assert outcome == "done" and item_ids == [seen_inside["item_id"]]
        row = seen_inside["row"]
        assert row is not None, "no state row existed when the commit callback returned"
        assert row["status"] == "done", (
            f"the callback left status={row['status']!r}; a cancellation landing "
            "after the finalize hop would leave the 'scanning' marker in place "
            "and the next sweep would re-ingest the file alongside the "
            "committed group"
        )
        assert json.loads(row["item_ids"] or "[]") == [seen_inside["item_id"]], (
            f"the callback persisted {row['item_ids']} instead of the committed "
            "group -- the items would be unreachable by the deleted-file path"
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_failed_callback_persistence_does_not_poison_the_ingest(tmp_path):
    """The callback's state write is a durability upgrade, never a precondition.

    It runs inside the pipeline's finalize hop, AFTER the group has committed
    and the superseded items are deleted. If a writer-lock timeout (a large
    concurrent import_bundle) made it raise, the exception would poison that
    hop: the pipeline reports the whole ingest failed, the caller writes a
    terminal 'failed' row, and the next sweep re-ingests alongside the
    committed group -- exactly the duplication the write exists to prevent. So
    a failed write must be swallowed: the ids still travel through memory and
    the caller's own 'done' write persists them on the uncancelled path.
    """

    class _LockedConn:
        """Real connection, except folder_file_state writes hit a locked DB."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args):
            if sql.lstrip().startswith("UPDATE folder_file_state"):
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._real, name)

    class _Store:
        def __init__(self, real):
            self._real = real

        @property
        def db(self):
            return _LockedConn(self._real.db)

        def __getattr__(self, name):
            return getattr(self._real, name)

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        source_id = store.add_source("folder", "local_folder", str(tmp_path))
        created: list[str] = []

        class _Pipeline:
            async def ingest_file(
                self,
                path,
                *,
                source_id,
                namespace,
                original_name,
                old_item_ids,
                on_committed=None,
                on_duplicate=None,
            ):
                def _insert_and_report() -> None:
                    created.append(store.add_item("T", "body", "document", source_id=source_id))
                    if on_committed is not None:
                        # Must not raise: a raise here poisons the finalize
                        # hop after the commit (see docstring).
                        on_committed(list(created))

                await asyncio.to_thread(_insert_and_report)
                return "job-1"

            def get_job_status(self, job_id):
                return {"status": "completed"}

        watcher = FolderWatcher(_Store(store), _Pipeline())
        item_ids, outcome = await watcher._ingest_file(str(doc), source_id, "default", {}, [])

        assert outcome == "done", (
            f"a failed best-effort persistence turned the ingest into "
            f"{outcome!r}; the committed group would be rolled up as a partial "
            "failure and re-ingested (duplicated) by the next sweep"
        )
        assert item_ids == created, (
            "the in-memory path must still deliver the committed ids when the "
            "durability write fails"
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_scan_records_the_committed_group_on_the_state_row(tmp_path):
    """End to end: the ids the pipeline committed are what the state row names.

    The unit tests above assert the value ``_ingest_file`` returns; this asserts
    the value the SCAN persists, which is what the deleted-file path and re-ingest
    detection actually read. A callback wired up but dropped on the way out would
    pass every test above and still strand every ingested file.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.knowledge.ingestion import IngestionPipeline

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        folder = tmp_path / "folder"
        folder.mkdir()
        (folder / "doc.md").write_text("some body", encoding="utf-8")

        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(
            return_value=[{"category": "document", "summary": "s", "entities": []}]
        )
        chunker = MagicMock()
        # A .md file dispatches to chunk_markdown (see _run_chunker); a bare
        # MagicMock would answer it with len()==0 and the scan would commit an
        # empty group, passing this test vacuously in the other direction.
        _one_chunk = lambda text, **kw: [  # noqa: E731
            {
                "content": text,
                "chunk_index": 0,
                "section_title": None,
                "line_start": 0,
                "line_end": 0,
            }
        ]
        chunker.chunk.side_effect = _one_chunk
        chunker.chunk_markdown.side_effect = _one_chunk
        reader = MagicMock()
        reader.read.return_value = ("some body", {})
        pipeline = IngestionPipeline(
            store=store, extractor=extractor, chunker=chunker, reader=reader, embedder=None
        )

        source_id = store.add_source("folder", "local_folder", str(folder))
        watcher = FolderWatcher(store, pipeline)
        await watcher.scan_source(
            {"id": source_id, "uri": str(folder), "source_type": "local_folder", "properties": "{}"}
        )

        owned = sorted(
            r["id"]
            for r in store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,)
            ).fetchall()
        )
        assert owned, (
            "the scan ingested nothing, so there is no group to name and this "
            "test would pass vacuously"
        )
        row = store.db.execute(
            "SELECT item_ids, status FROM folder_file_state WHERE source_id = ?", (source_id,)
        ).fetchone()
        assert row is not None, "the scan recorded no state row for the file"
        assert sorted(json.loads(row["item_ids"] or "[]")) == owned, (
            f"the state row names {row['item_ids']} but the source owns {owned} -- "
            "the committed group was not carried out of the callback, so those "
            "items are unreachable by the deleted-file path "
            f"(status={row['status']!r})"
        )
    finally:
        store.close()


def test_await_ratchet_would_catch_a_reintroduced_post_commit_hop():
    """Negative control: the await detector fires on the shape just removed.

    Without this, a detector bug that reports nothing would make the ratchet pass
    forever. Feeds it the exact code the GPT reviewer flagged.
    """
    pre_fix = ast.parse(
        "class W:\n"
        "    async def _ingest_file(self):\n"
        "        job = await self.pipeline.ingest_file(p)\n"
        "        if await asyncio.to_thread(_sync_status) == 'error':\n"
        "            return None, 'failed'\n"
        "        after = await asyncio.to_thread(_owned_item_ids)\n"
        "        return after, 'done'\n"
    )
    names = [name for _, name in _awaits(_async_def(pre_fix, "_ingest_file"))]
    assert names.count("to_thread") == 2, f"the detector missed the post-commit hops (saw {names})"


def test_await_ratchet_ignores_awaits_inside_nested_helpers():
    """Negative control: an await in a nested frame is not this frame's await.

    A detector that flagged those would make the correct shape unlandable and push
    the next author back onto the loop.
    """
    post_fix = ast.parse(
        "class W:\n"
        "    async def _ingest_file(self):\n"
        "        async def _later():\n"
        "            return await asyncio.sleep(0)\n"
        "        return await self.pipeline.ingest_file(p)\n"
    )
    names = [name for _, name in _awaits(_async_def(post_fix, "_ingest_file"))]
    assert names == [
        "ingest_file"
    ], f"the detector counted a nested frame's await as this frame's: {names}"


def test_db_ratchet_would_catch_a_reintroduced_on_loop_query():
    """Negative control for the sqlite detector, on the original pre-fix shape."""
    pre_fix = ast.parse(
        "class W:\n"
        "    async def _ingest_file(self):\n"
        "        before = {r['id'] for r in self.store.db.execute(\n"
        "            'SELECT id FROM items WHERE source_id = ?', (1,)).fetchall()}\n"
        "        return before\n"
    )
    fn = _async_def(pre_fix, "_ingest_file")
    hits = {called for called, _ in _direct_calls(fn.body) if called in _DB_CALLS}
    assert {
        "execute",
        "fetchall",
    } <= hits, f"the detector missed part of the pre-fix shape (found {sorted(hits)})"


def test_db_ratchet_ignores_queries_inside_nested_helpers():
    """Negative control: a query in a nested sync helper is a thread target."""
    nested = ast.parse(
        "class W:\n"
        "    async def _ingest_file(self):\n"
        "        def _ids():\n"
        "            return self.store.db.execute('SELECT 1').fetchall()\n"
        "        return _ids\n"
    )
    fn = _async_def(nested, "_ingest_file")
    hits = {called for called, _ in _direct_calls(fn.body) if called in _DB_CALLS}
    assert (
        hits == set()
    ), f"the detector flagged a query that lives in a nested helper: {sorted(hits)}"
