"""Knowledge ingest paths stat the disk off the event loop.

A knowledge file can sit on a slow or network disk, where one stat can hold for
seconds. Each test records the thread every stat of the file ran on and fails
if any ran on the event-loop thread. Each also checks the stat happened at all,
so a test cannot pass by never reaching the code under test.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.artifacts import ArtifactStore
from kiro_crew.knowledge.artifact_ingest import ensure_artifact_source, ingest_artifact
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.knowledge.watcher import KnowledgeWatcher


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s.close()


def _one_chunk(text, **kw):
    return [
        {"content": text, "chunk_index": 0, "section_title": None, "line_start": 0, "line_end": 0}
    ]


@pytest.fixture()
def pipeline(store):
    """A real pipeline over *store*, with the chunker and extractor stubbed."""
    extractor = MagicMock()
    extractor._pool = None
    extractor.extract_batch = AsyncMock(
        return_value=[{"category": "document", "summary": "s", "entities": []}]
    )
    chunker = MagicMock()
    for name in ("chunk", "chunk_markdown", "chunk_code", "chunk_slides"):
        getattr(chunker, name).side_effect = _one_chunk
    return IngestionPipeline(
        store=store, extractor=extractor, chunker=chunker, reader=FileReader(), embedder=None
    )


def _record(monkeypatch, owner, attr: str, as_path) -> list[tuple[str, int]]:
    """Record ``(path, thread)`` for every ``owner.attr`` call."""
    calls: list[tuple[str, int]] = []
    original = getattr(owner, attr)

    def recording(*args, **kwargs):
        calls.append((as_path(args), threading.get_ident()))
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, attr, recording)
    return calls


def _threads_for(calls, path) -> list[int]:
    threads = [t for p, t in calls if p == str(path)]
    assert threads, f"{path} was never stat'ed; the test is vacuous"
    return threads


def _watcher(store) -> KnowledgeWatcher:
    pipeline = MagicMock()
    pipeline.embedder = None
    watcher = KnowledgeWatcher(store=store, pipeline=pipeline)
    watcher._maybe_reembed_stale = AsyncMock()  # type: ignore[method-assign]
    return watcher


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [True, False])
async def test_the_file_sweep_stats_each_file_off_the_event_loop(
    store, tmp_path, monkeypatch, present
):
    """The sweep's existence check and mtime read both run in a worker thread."""
    doc = tmp_path / "doc.md"
    doc.write_text("# doc")
    # A stored mtime far ahead keeps a present file out of the re-ingest.
    sid = store.add_source("doc.md", "local_file", str(doc), properties={"mtime": 1 << 40})
    if not present:
        doc.unlink()
    exists_calls = _record(monkeypatch, Path, "exists", lambda a: str(a[0]))
    stat_calls = _record(monkeypatch, os, "stat", lambda a: str(a[0]))

    await _watcher(store)._scan()

    loop_thread = threading.get_ident()
    assert loop_thread not in _threads_for(exists_calls, doc), "exists() ran on the loop"
    status = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()[
        "sync_status"
    ]
    if present:
        assert loop_thread not in _threads_for(stat_calls, doc), "os.stat() ran on the loop"
        assert status != "missing"
    else:
        assert status == "missing"


@pytest.mark.asyncio
async def test_artifact_source_path_is_resolved_off_the_event_loop(
    pipeline, store, tmp_path, monkeypatch
):
    """The sensitive-path check resolves a file-backed artifact's source in a thread."""
    sid, _ = ensure_artifact_source(store)
    src = tmp_path / "backed.md"
    src.write_text("# backed\nbody", encoding="utf-8")
    art_store = ArtifactStore(root=tmp_path / "artifacts")
    art = art_store.create(
        name="Backed", content="# backed\nbody", kind="markdown", source_path=str(src)
    )
    calls = _record(monkeypatch, Path, "resolve", lambda a: str(a[0]))

    job = await ingest_artifact(pipeline, art_store, art.slug, sid, {"markdown"})

    assert job is not None
    assert threading.get_ident() not in _threads_for(calls, src), "resolve() ran on the loop"


@pytest.mark.asyncio
async def test_local_file_ingest_never_resolves_on_the_event_loop(
    pipeline, store, tmp_path, monkeypatch
):
    """The local-file branch keys its source by URI without a second on-loop resolve."""
    doc = tmp_path / "note.md"
    doc.write_text("# note\nbody", encoding="utf-8")
    calls = _record(monkeypatch, Path, "resolve", lambda a: str(a[0]))

    job = await pipeline.ingest_file(str(doc))

    assert job is not None
    assert threading.get_ident() not in _threads_for(calls, doc), "resolve() ran on the loop"
    assert store.get_source_by_uri(str(doc.resolve()))["source_type"] == "local_file"
