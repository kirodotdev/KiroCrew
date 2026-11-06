"""What a consolidation pass keeps of the semantic table while its model call runs.

The block bounds the PROMPT's copy of the table. The writers read the table after the
call: on the private path a correction is verified against a row's value_json,
updated_at and record_revision, all as read before the call, and the V1 writer retires
against the value_json read then. So the pass keeps those fields of every row and
nothing else across the call: no stored row, no embedding, no prompt copy.

The pass reads the table in keyset pages; the store stand-in serves them as the real
``get_all_semantic`` does, and the snapshot and liveness tests read it in several pages,
so the snapshot is shown to gather every page and no page's rows to outlive the block.

The liveness test watches what the store hands the pass through weak references. The
pass offloads its reads through ``asyncio.to_thread`` and
``run_in_memory_background_pool``, and a pool worker keeps a call's arguments until it
next runs (CPython drops its work item after delivering the result), which can be after
the pass has resumed: that reference is the pool's, not the pass's. So that test runs
both offloads on a thread of its own and resumes the pass only once that thread has
ended (:func:`_joined`), and it asserts that every stand-in handed the rows ran on such
a thread. Every thread handed the rows has then been joined before the model call
starts, so what is alive in the call is what the pass holds; an offload through any
other executor is joined the same way.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import gc
import json
import re
import threading
import weakref
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from consolidation_lease_helpers import lease_aware_log

from kiro_crew import executors, history_consolidation
from kiro_crew import memory_record_metadata as record_meta
from kiro_crew import memory_stores
from kiro_crew.history import HistoryConsolidator
from kiro_crew.vector_memory import (
    VectorMemoryStore,
    create_member_database,
    open_member_database,
)

_HEADING = "\n\n## Current Semantic Memory\n"
# The notice is the block's last line; what follows the block differs by path.
_NOTICE = re.compile(r"\n\[Context budget: omitted \d+ of \d+ semantic rows\b[^\]]*\]")

#: The lost-run bound of an offloaded call (D1, D8): it only names a hang.
_LOST_RUN_SECS = 60.0
#: The name of the thread :func:`_joined` runs a call on.
_JOINED = "joined-offload"
#: Rows per page of the watched read: the 120 stored rows come in three pages.
_PAGE = 50

_QUOTE = "Please replace old@example.com with new@example.com."
_SPAN = [{"role": "user", "content": _QUOTE, "ts": "2026-10-25T10:00:00+00:00"}]
_CORRECTION = {
    "key": "user.work_email",
    "value": "new@example.com",
    "confidence": 1,
    "correction_quote": _QUOTE,
}


class _Watchable(dict):
    """A mapping a weak reference can watch; a plain dict cannot be watched."""


class _Number(float):
    """A row's confidence, which only the row and the prompt's copy hold."""


class _Watch:
    """Weak references to what the store handed the pass, by kind, the thread each
    stand-in handed the rows ran on, and the filters and row count of every table read."""

    KINDS = ("row", "confidence", "metadata")

    def __init__(self) -> None:
        self.refs: dict[str, list[weakref.ref]] = {kind: [] for kind in self.KINDS}
        self.threads: dict[str, str] = {}
        self.reads: list[dict] = []

    def add(self, kind: str, obj):
        self.refs[kind].append(weakref.ref(obj))
        return obj

    def ran(self, step: str) -> None:
        self.threads[step] = threading.current_thread().name

    def alive(self) -> dict[str, int]:
        gc.collect()
        return {kind: sum(ref() is not None for ref in refs) for kind, refs in self.refs.items()}


async def _joined(func, /, *args, **kwargs):
    """Run ``func(*args, **kwargs)`` on a thread of its own; return once it has ended."""
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()
    outcome: list = []
    context = contextvars.copy_context()

    def run() -> None:
        try:
            outcome.append((True, context.run(func, *args, **kwargs)))
        except BaseException as exc:  # handed back to the awaiting pass
            outcome.append((False, exc))
        finally:
            loop.call_soon_threadsafe(finished.set)

    worker = threading.Thread(target=run, name=_JOINED, daemon=True)
    worker.start()
    await asyncio.wait_for(finished.wait(), _LOST_RUN_SECS)
    worker.join(_LOST_RUN_SECS)
    assert not worker.is_alive(), "an offloaded call outlived its own result"
    ok, value = outcome.pop()
    if not ok:
        raise value
    return value


def _offload_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pass imports the background pool's runner from ``executors`` each time it
    # runs, so it reads this patch; no facade mirrors that module.
    monkeypatch.setattr(executors, "run_in_memory_background_pool", _joined)
    monkeypatch.setattr(asyncio, "to_thread", _joined)


def _stored_rows() -> list[dict]:
    """120 rows of about 1,000 characters, twice the cap. Three are lessons, whose
    prompt value is their display text, never the stored value_json the writers read."""
    rows = []
    for index in range(120):
        lesson = index % 40 == 0
        key = f"lesson.rule_{index:03d}" if lesson else f"project.row_{(index * 7919) % 10_000:05d}"
        value: object = (
            {"rule": f"rule {index} " + "y" * 900, "category": "knowledge"}
            if lesson
            else f"v{index}:" + "x" * 950
        )
        stamp = f"2026-01-{1 + index % 28:02d}T00:00:00"
        rows.append(
            {
                "key": key,
                "value_json": json.dumps(value),
                "confidence": 0.9,
                "source": "consolidation:test",
                "created_at": stamp,
                "updated_at": stamp,
                "is_deleted": 0,
                "embedding": bytes(4096),
            }
        )
    # ``get_all_semantic`` reads ``ORDER BY key``; the stand-in keeps that contract.
    return sorted(rows, key=lambda r: r["key"])


def _watched_store(watch: _Watch, *, private: bool) -> MagicMock:
    store = MagicMock()
    store.algorithm_version = "v2" if private else "v1"
    store.space_generation = 1
    store.consolidation_rotation_cursor.return_value = None
    store.advance_consolidation_rotation.return_value = True
    store.consolidation_receipt.return_value = None
    store.apply_consolidation.return_value = {"semantic": 0, "episodic": 0, "lessons": 0}

    # Plain functions, not mock methods: a mock records every call's arguments, and
    # that record would keep the rows alive itself.
    def get_all_semantic(
        limit: int | None = None,
        offset: int = 0,
        *,
        q: str = "",
        after_key: str | None = None,
        exclude_lessons: bool = False,
    ) -> list[dict]:
        # The real read's page: the active rows in key order after ``after_key``,
        # lessons left out when asked, then ``offset`` and ``limit``. A text filter
        # is recorded, not applied; the snapshot test asserts the pass asks for none.
        watch.ran("read")
        rows = [
            row
            for row in _stored_rows()
            if (after_key is None or row["key"] > after_key)
            and not (exclude_lessons and row["key"].startswith("lesson."))
        ][offset : None if limit is None else offset + limit]
        watch.reads.append({"q": q, "exclude_lessons": exclude_lessons, "served": len(rows)})
        return [
            watch.add(
                "row",
                _Watchable(row, confidence=watch.add("confidence", _Number(row["confidence"]))),
            )
            for row in rows
        ]

    def with_record_metadata(rows: list[dict]) -> list[dict]:
        watch.ran("revisions")
        return [
            watch.add(
                "row",
                _Watchable(
                    row,
                    record_revision=1,
                    record_metadata=watch.add(
                        "metadata",
                        _Watchable(category="project", subject="user", predicate=f"p{i}"),
                    ),
                ),
            )
            for i, row in enumerate(rows)
        ]

    def score_consolidation_rows(rows, texts, query_text, *, deadline) -> list[float]:
        # No default for ``deadline``: an offload that drops a keyword argument fails
        # the call before it is recorded, and the pass takes that as no signal.
        watch.ran("scorer")
        return [0.0] * len(rows)

    store.get_all_semantic = get_all_semantic
    store.with_record_metadata = with_record_metadata
    store.score_consolidation_rows = score_consolidation_rows
    return store


def _consolidator(vector_store, *, span: list[dict] | None = None) -> HistoryConsolidator:
    log = lease_aware_log()
    log.snapshot_for_consolidation.return_value = (
        span or [{"role": "user", "content": "hi"}],
        1,
        0,
        0,
    )
    log.consolidation_retry_state.return_value = (0, 0.0)
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    return HistoryConsolidator(
        log=log, memory=memory, sessions=None, vector_store=vector_store, migrated=True
    )


def _as_member(monkeypatch: pytest.MonkeyPatch, consolidator: HistoryConsolidator, store) -> None:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    execution = ExecutionContext(
        "alice", MemoryStoreRef("member-alice", "alice"), "member", "kirocrew", "persistent"
    )
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda _key: execution
    )
    monkeypatch.setattr("kiro_crew.memory_stores.memory_store_version", lambda _store: 2)
    monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=store))
    monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda **_kwargs: consolidator._memory)


def _shown_keys(prompt: str) -> set[str]:
    start = prompt.index(_HEADING) + len(_HEADING)
    notice = _NOTICE.search(prompt, start)
    assert notice, "the table must render over the cap, notice included"
    return {entry["key"] for entry in json.loads(prompt[start : notice.start()])}


async def _drive(path: str, monkeypatch: pytest.MonkeyPatch, watch: _Watch, *, joined: bool):
    """One pass over the watched store; what the packer, the model and the writer saw."""
    store = _watched_store(watch, private=path != "v1-writer")
    c = _consolidator(store)
    if path == "member":
        _as_member(monkeypatch, c, store)
    if joined:
        _offload_joined(monkeypatch)
    # The pass's own pager, in pages short enough that the stored rows take several.
    # Through the facade: the family shares the name, and the mirror writes every holder.
    monkeypatch.setattr(
        history_consolidation,
        "_semantic_snapshot_in_pages",
        functools.partial(history_consolidation._semantic_snapshot_in_pages, page=_PAGE),
    )
    seen: dict = {}
    pack = history_consolidation._bounded_semantic_table

    def watched_pack(*args, **kwargs):
        watch.ran("packer")
        seen["alive at the packer"] = watch.alive()
        return pack(*args, **kwargs)

    monkeypatch.setattr(history_consolidation, "_bounded_semantic_table", watched_pack)

    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        seen["shown"] = _shown_keys(prompt)
        seen["alive in the call"] = watch.alive()
        seen["threads at the call"] = dict(watch.threads)
        return {"semantic": []}

    def fake_write(result, key, vector_store=None, **kwargs) -> None:
        seen["snapshot"] = kwargs["snapshot"]

    with (
        patch.object(c, "_call_llm", side_effect=fake_llm),
        patch.object(c, "_write_structured_memory", side_effect=fake_write),
    ):
        await c._consolidate("k", include_history=False)
    if path == "member":
        store.apply_consolidation.assert_called_once()
        seen["snapshot"] = store.apply_consolidation.call_args.kwargs["snapshot"]
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1-writer", "v2-writer", "member"])
async def test_the_writers_snapshot_is_every_key_with_only_the_fields_they_read(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    watch = _Watch()
    seen = await _drive(path, monkeypatch, watch, joined=False)

    rows = _stored_rows()
    # Read in pages, no one of them the whole table, and with no filter: the keys below
    # are gathered from every page.
    served = [read["served"] for read in watch.reads]
    assert len(served) > 1 and max(served) < len(rows)
    assert not any(read["q"] or read["exclude_lessons"] for read in watch.reads)
    snapshot = seen["snapshot"]
    # Every key, not only the shown ones: a correction can name a row the cap cut.
    assert set(snapshot) == {row["key"] for row in rows}
    assert set(snapshot) - seen["shown"], "the block must leave a key out"
    # One shape on both paths; a V1 row carries no revision, so its value has none.
    revision = {} if path == "v1-writer" else {"record_revision": 1}
    assert snapshot == {
        row["key"]: {"value_json": row["value_json"], "updated_at": row["updated_at"], **revision}
        for row in rows
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1-writer", "v2-writer", "member"])
async def test_nothing_the_store_returned_is_alive_while_the_model_call_is_in_flight(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    watch = _Watch()
    seen = await _drive(path, monkeypatch, watch, joined=True)

    # The watch sees them while the packer holds them (it is handed both copies), so
    # their absence in the call below is a release, not a watch that sees nothing.
    assert seen["alive at the packer"] == {
        "row": 120,
        "confidence": 120,
        "metadata": 0 if path == "v1-writer" else 120,
    }
    assert "alive in the call" in seen, "the model call must have been reached"
    # A row is held by the rows; a confidence and a record's metadata by the rows and
    # the prompt's copy. Every entry of that copy holds its row's own confidence, which
    # the writers' snapshot does not keep: once the rows are gone, a live confidence is
    # a live prompt entry.
    assert seen["alive in the call"] == dict.fromkeys(_Watch.KINDS, 0)
    # Before the call, every stand-in handed the rows ran on a joined thread: the
    # read, the revisions on the private path, the packer, and the scorer, whose
    # offload passes ``deadline`` by keyword.
    steps = ["read", "scorer", "packer"] + ([] if path == "v1-writer" else ["revisions"])
    assert seen["threads at the call"] == dict.fromkeys(steps, _JOINED)


def _embed(text: str) -> list[float]:
    vector = [0.0] * 16
    vector[len(text) % 16] = 1.0
    return vector


def _member_database(tmp_path, opened, monkeypatch, *, embed=_embed) -> VectorMemoryStore:
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "stores")
    # Every write at one instant before the span's, so a correction's evidence is newer
    # than the row it replaces.
    monkeypatch.setattr("kiro_crew.vector_memory._now_iso", lambda: "2026-01-01T00:00:00+00:00")
    path = tmp_path / "member" / "memory.db"
    path.parent.mkdir()
    create_member_database(path, member_id="alice", store_id="member-alice")
    store = opened(
        open_member_database(path, member_id="alice", store_id="member-alice", embedding_dim=16)
    )
    store.embed_fn_factory = None
    store.embed_fn = embed
    return store


def _fact(store: VectorMemoryStore, key: str) -> tuple[object, int, str]:
    with store._db_lock:
        row = store.db.execute(
            "SELECT value_json FROM semantic_memory WHERE key = ? AND is_deleted = 0", (key,)
        ).fetchone()
        meta = record_meta.get_record_metadata(store.db, f"key:{key}")
    return json.loads(row[0]), meta.get("revision", 0), meta.get("source_ref", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["control", "changed-and-restored"])
@pytest.mark.parametrize("path", ["member", "v2-writer"])
async def test_a_correction_read_before_the_call_is_refused_after_a_write_lands_during_it(
    tmp_path, opened, monkeypatch: pytest.MonkeyPatch, path: str, race: str
) -> None:
    store = _member_database(tmp_path, opened, monkeypatch)
    assert store.set_semantic("user.work_email", "old@example.com", 1.0, "user_explicit") is None
    c = _consolidator(store, span=_SPAN)
    if path == "member":
        _as_member(monkeypatch, c, store)

    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        if race == "changed-and-restored":
            # Another writer lands while the model runs and puts the value back: the
            # value the correction names is current again, its revision is not. It
            # writes as the seed did (user_explicit, confidence 1.0), so the revision
            # is the one thing the correction meets that the control's does not.
            for value in ("interim@example.com", "old@example.com"):
                assert store.set_semantic("user.work_email", value, 1.0, "user_explicit") is None
        return {"semantic": [_CORRECTION]}

    with patch.object(c, "_call_llm", side_effect=fake_llm):
        if path == "member" and race != "control":
            with pytest.raises(ValueError, match="Memory changed since extraction"):
                await c._consolidate("k", include_history=False)
        else:
            await c._consolidate("k", include_history=False)

    value, revision, _ = _fact(store, "user.work_email")
    if race == "control":
        assert (value, revision) == ("new@example.com", 2)
    else:
        assert (value, revision) == ("old@example.com", 3)
    if path == "member":
        with store._db_lock:
            receipts = store.db.execute("SELECT COUNT(*) FROM memory_consolidations").fetchone()[0]
        assert receipts == (1 if race == "control" else 0)


@pytest.mark.asyncio
async def test_a_v1_update_retires_against_the_value_read_before_the_call(
    tmp_path, opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = opened(VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=16))
    store.init()
    store.embed_fn = _embed
    assert store.set_semantic("project.plan", "the old plan", 0.8, "consolidation:other") is None
    write, retire = store.set_semantic, store.embed_semantic_retirement
    writes: list[dict] = []
    retired: list[tuple] = []

    def recorded_write(*args, **kwargs):
        writes.append(kwargs)
        return write(*args, **kwargs)

    def recorded_retire(key: str, value_json: str):
        retired.append((key, value_json))
        return retire(key, value_json)

    monkeypatch.setattr(store, "set_semantic", recorded_write)
    monkeypatch.setattr(store, "embed_semantic_retirement", recorded_retire)
    c = _consolidator(store)

    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        # A write that lands while the model runs: every read from here on sees it. It
        # comes from the seed's source and confidence (consolidation:other, 0.8), which
        # the V1 store lets the pass's own update (consolidation:k, 0.9) overwrite; a
        # user_explicit row would refuse that update, and the test would then pin the
        # refusal instead of the value the update retires.
        assert write("project.plan", "a concurrent plan", 0.8, "consolidation:other") is None
        assert store.get_semantic("project.plan")["value_json"] == json.dumps("a concurrent plan")
        return {"semantic": [{"key": "project.plan", "value": "the new plan", "confidence": 0.9}]}

    with patch.object(c, "_call_llm", side_effect=fake_llm):
        await c._consolidate("k", include_history=False)

    [call] = [kwargs for kwargs in writes if kwargs.get("key") == "project.plan"]
    assert call["retirement_value_json"] == json.dumps("the old plan")
    assert retired == [("project.plan", json.dumps("the old plan"))]
    assert store.get_semantic("project.plan")["value_json"] == json.dumps("the new plan")


@pytest.mark.asyncio
async def test_an_identity_resolved_correction_finds_its_row_the_block_did_not_show(
    tmp_path, opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No embedder: no row has a vector and the span has none, so relevance is the
    # keyword overlap alone, and the hundred wide rows hold every word of the span. A
    # word more than a tenth of the rows hold counts for none of them, so no row scores
    # above zero and the block is the rotating slice from the first key, then the
    # newest rows, then backfill. Every row is written at one pinned instant, so each
    # pool scans in key order: the wide rows, then the twenty narrow ones, then the
    # resolved row, the LAST key. The slice stops at the first wide row past its
    # allowance; the other two pools take every row that still fits, and the wide rows
    # alone overflow both the recency allowance and the block, so the narrow rows meet
    # what is left of each first; each costs less than the resolved row, so less than
    # its cost is left when it is reached.
    store = _member_database(tmp_path, opened, monkeypatch, embed=None)
    identity = {"category": "contact", "subject": "user", "predicate": "work_email"}
    assert (
        store.set_semantic(
            "user.contact_primary", "old@example.com", 1.0, "user_explicit", metadata=identity
        )
        is None
    )
    for index in range(100):
        value = f"{index} please replace old example com with new example com " + "x" * 850
        assert (
            store.set_semantic(f"project.filler_{index:03d}", value, 0.9, "user_explicit") is None
        )
    for index in range(20):
        assert (
            store.set_semantic(f"user.a_{index:02d}", f"note {index:02d}", 0.9, "user_explicit")
            is None
        )
    # The span is one user row, the quote itself, later than the pinned instant every
    # row was written at.
    span = [{"role": "user", "content": _QUOTE, "ts": "2026-10-25T10:00:00+00:00"}]
    c = _consolidator(store, span=span)
    _as_member(monkeypatch, c, store)
    seen: dict = {}
    # The answer names a new key with the held row's identity, which the store resolves
    # to that row: the user's own words as its correction_quote, at confidence 1.
    answer = {
        "key": "user.work_email",
        "value": "new@example.com",
        "confidence": 1,
        "correction_quote": _QUOTE,
        "metadata": identity,
    }

    async def fake_llm(prompt: str, *, memory_store: str = "", session_key: str = "") -> dict:
        seen["shown"] = _shown_keys(prompt)
        return {"semantic": [answer]}

    with patch.object(c, "_call_llm", side_effect=fake_llm):
        await c._consolidate("k", include_history=False)

    assert seen["shown"] and "user.contact_primary" not in seen["shown"]
    value, revision, source_ref = _fact(store, "user.contact_primary")
    assert (value, revision) == ("new@example.com", 2)
    assert source_ref == f"k#message:0: {_QUOTE}"
    assert store.get_semantic("user.work_email") is None
