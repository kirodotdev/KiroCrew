"""A new session's memory reads can start beside its startup and be taken by its build.

The invariant: a session-start context built from a prefetch is byte-for-byte the
context built without one, and the build reads for itself whenever a prefetch is
missing, not started, failed, or any input it read with has moved (the message, a
memory file, the caps). Each test builds the REAL session context on a real
``MemoryStore``; the read counter is the only seam, and it calls the real reads.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import threading
from unittest.mock import AsyncMock

import pytest

from kiro_crew import context_omissions
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import budget as _budgets
from kiro_crew.context_assembly import store_admission
from kiro_crew.dashboard import chat_runner
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.testing.clock import ManualClock

MESSAGE = "Summarize the deploy checklist."
_REAL_READS = store_admission.read_session_memory


def _key(query: str) -> tuple:
    return store_admission._prefetch_key(None, None, query)


def _held(query: str):
    return store_admission._memory_prefetches[_key(query)]


@pytest.fixture(autouse=True)
def _no_leftover_prefetch():
    store_admission._memory_prefetches.clear()
    yield
    store_admission._memory_prefetches.clear()


@pytest.fixture
def builder(tmp_path):
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    memory = builder.get_memory_for(None, None)
    memory.write_preferences("# Preferences\n- Answer in short sentences.\n")
    memory.write_projects("# Projects\n- Deploy checklist rework.\n")
    memory.append_history("Reviewed the deploy checklist with the team.")
    return builder


@pytest.fixture
def reads(monkeypatch) -> list[str]:
    """Count real memory reads, naming the thread that made each."""
    made: list[str] = []
    real = store_admission.read_session_memory

    def counted(*args, **kwargs):
        made.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(store_admission, "read_session_memory", counted)
    return made


#: The one line of a session-start build stamped with the wall clock, to the minute.
#: Two builds a test compares can straddle a minute, so it is blanked; the memory
#: blocks this file is about never carry it.
_CURRENT_DATE_LINE = re.compile(r"^\[CURRENT DATE\] .*$", re.MULTILINE)


def _build(builder, query: str = MESSAGE) -> tuple[str, dict]:
    with context_omissions.collect() as omitted:
        text = builder.build_session_context("dashboard:g3", query_text=query)
    assert len(_CURRENT_DATE_LINE.findall(text)) == 1
    return _CURRENT_DATE_LINE.sub("[CURRENT DATE] <now>", text), dict(omitted)


def _prefetch(builder, query: str = MESSAGE) -> None:
    store_admission.reserve_memory_prefetch(None, None, query)
    store_admission.prefetch_session_memory(builder, query, workspace=None, memory_store=None)


def test_a_build_from_a_prefetch_is_byte_for_byte_the_build_without_one(builder, reads):
    expected, expected_omitted = _build(builder)
    assert "Answer in short sentences." in expected  # the memory blocks are in it
    assert reads == [threading.current_thread().name]

    _prefetch(builder)
    reads.clear()
    got, got_omitted = _build(builder)

    assert got == expected
    assert got_omitted == expected_omitted
    assert reads == [], "the build read memory again instead of taking the prefetch"
    assert store_admission._memory_prefetches == {}, "a prefetch is taken once"


def test_the_omissions_the_early_reads_record_reach_the_build(builder, monkeypatch, reads):
    real = store_admission.read_session_memory

    def cutting(*args, **kwargs):
        result = real(*args, **kwargs)
        cut = {context_omissions.PREFERENCE_FACTS: {"reason": "cap", "omitted": 2}}
        return store_admission.SessionMemoryReads(
            result.memory_ctx, result.activity, result.activity_ctx, result.activity_ranked, cut
        )

    monkeypatch.setattr(store_admission, "read_session_memory", cutting)
    _prefetch(builder)
    _text, omitted = _build(builder)
    assert omitted[context_omissions.PREFERENCE_FACTS] == {"reason": "cap", "omitted": 2}


@pytest.mark.parametrize(
    "move",
    [
        pytest.param(
            lambda m: m.write_preferences("# Preferences\n- Answer in Spanish.\n"), id="prefs"
        ),
        pytest.param(lambda m: m.write_projects("# Projects\n- Another one.\n"), id="projects"),
        pytest.param(lambda m: m.append_history("A later note."), id="history"),
    ],
)
def test_a_memory_file_written_after_the_prefetch_is_read_again(builder, reads, move):
    _prefetch(builder)
    # A write whose stat would land in the same tick still differs in size.
    move(builder.get_memory_for(None, None))
    reads.clear()
    got, _ = _build(builder)
    store_admission._memory_prefetches.clear()
    expected, _ = _build(builder)
    assert got == expected
    assert len(reads) == 2, "the build used reads made before the file changed"


def test_a_preferences_write_during_the_reads_drops_the_prefetch(builder, reads, monkeypatch):
    """A Save that lands after preferences were read, but before the reads end."""

    def reads_then_save(memory, *args, **kwargs):
        result = _REAL_READS(memory, *args, **kwargs)
        memory.write_preferences("# Preferences\n- Answer in Spanish.\n")
        return result

    monkeypatch.setattr(store_admission, "read_session_memory", reads_then_save)
    _prefetch(builder)
    entry = _held(MESSAGE)
    assert entry.done.is_set() and entry.reads is None
    monkeypatch.setattr(store_admission, "read_session_memory", _counter(reads))
    got, _ = _build(builder)
    assert "Answer in Spanish." in got
    assert len(reads) == 1


class _FakeVectorStore:
    """Only the validity token the prefetch reads; no rows, so no reads use it."""

    def __init__(self) -> None:
        self._semantic_scoring_generation = 0
        self._episodic_scoring_generation = 0
        self._space_generation = 0
        self.data_version: int | None = 1

    def _sqlite_data_version(self) -> int | None:
        return self.data_version


@pytest.mark.parametrize(
    "move",
    [
        pytest.param(lambda v: setattr(v, "_semantic_scoring_generation", 1), id="semantic-write"),
        pytest.param(lambda v: setattr(v, "_episodic_scoring_generation", 1), id="episodic-write"),
        pytest.param(lambda v: setattr(v, "data_version", 2), id="other-process-write"),
        pytest.param(lambda v: setattr(v, "data_version", None), id="no-data-version"),
    ],
)
def test_a_vector_store_write_moves_the_token_the_prefetch_compares(move):
    vectors = _FakeVectorStore()
    before = store_admission._vector_token(vectors)
    assert store_admission._vector_token(vectors) == before, "the token moves at rest"
    move(vectors)
    assert store_admission._vector_token(vectors) != before


def test_a_store_that_cannot_see_other_writers_never_matches() -> None:
    """Without data_version another process's write is invisible, so nothing matches."""
    vectors = _FakeVectorStore()
    vectors.data_version = None
    assert store_admission._vector_token(vectors) != store_admission._vector_token(vectors)


def test_the_vector_token_is_part_of_the_inputs_compared(builder) -> None:
    from kiro_crew import context as ctx

    memory = builder.get_memory_for(None, None)
    vectors = _FakeVectorStore()
    memory.vector_store = vectors
    caps = ctx._resolve_caps(None)
    before = store_admission._memory_read_inputs(memory, caps, MESSAGE, True)
    vectors._semantic_scoring_generation += 1
    assert store_admission._memory_read_inputs(memory, caps, MESSAGE, True) != before


def test_a_real_store_token_holds_across_its_own_reads(tmp_path) -> None:
    """The reads' own access stamps must not invalidate every prefetch."""
    from kiro_crew.vector_memory import VectorMemoryStore

    vectors = VectorMemoryStore(db_path=tmp_path / "memory.db")
    vectors.init()
    memory = MemoryStore(workspace=tmp_path / "ws")
    memory.init()
    memory.vector_store = vectors
    from kiro_crew import context as ctx

    caps = ctx._resolve_caps(None)
    before = store_admission._memory_read_inputs(memory, caps, MESSAGE, True)
    _REAL_READS(memory, caps, MESSAGE, inject_activity=True)
    assert store_admission._memory_read_inputs(memory, caps, MESSAGE, True) == before


def test_the_default_store_keys_one_entry_however_it_is_spelled() -> None:
    """The turn reserves under its bindings' ``"default"``; the build takes under the
    execution record's ``""`` (or ``None``). One store, so one entry; a named store
    keys apart from it."""
    key = store_admission._prefetch_key
    assert key("ws", "default", MESSAGE) == key("ws", "", MESSAGE) == key("ws", None, MESSAGE)
    assert key("ws", "acme", MESSAGE) != key("ws", "", MESSAGE)
    store_admission.reserve_memory_prefetch("ws", "default", MESSAGE)
    store_admission.discard_memory_prefetch("ws", "", MESSAGE)
    assert store_admission._memory_prefetches == {}


def test_a_prefetch_for_another_message_is_not_used(builder, reads):
    _prefetch(builder, "Something else entirely.")
    reads.clear()
    _build(builder)
    assert len(reads) == 1


class _RecordingEvent(threading.Event):
    """An event that records the timeout of every ``wait`` made on it."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return super().wait(timeout)


def _record_done_waits(query: str = MESSAGE) -> _RecordingEvent:
    done = _RecordingEvent()
    _held(query).done = done
    return done


def test_a_prefetch_that_never_started_is_not_waited_for(builder, reads, monkeypatch):
    monkeypatch.setattr(_budgets, "_startup_recall_timeout_secs", lambda: 30.0)
    store_admission.reserve_memory_prefetch(None, None, MESSAGE)
    done = _record_done_waits()
    _build(builder)
    assert done.waits == [], "the build waited on a prefetch that never started"
    assert len(reads) == 1


def test_a_failed_prefetch_leaves_the_build_to_read_as_today(builder, reads, monkeypatch):
    expected, _ = _build(builder)

    def broken(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(store_admission, "read_session_memory", broken)
    _prefetch(builder)
    monkeypatch.setattr(store_admission, "read_session_memory", _counter(reads))
    reads.clear()
    got, _ = _build(builder)
    assert got == expected
    assert len(reads) == 1


def _counter(made: list[str]):
    def counted(*args, **kwargs):
        made.append(threading.current_thread().name)
        return _REAL_READS(*args, **kwargs)

    return counted


def _start_slow_prefetch(builder, monkeypatch, release: threading.Event) -> threading.Thread:
    """A prefetch whose reads hold until *release*, already reading when this returns."""
    reading = threading.Event()

    def slow(*args, **kwargs):
        reading.set()
        release.wait(30)
        return _REAL_READS(*args, **kwargs)

    monkeypatch.setattr(store_admission, "read_session_memory", slow)
    store_admission.reserve_memory_prefetch(None, None, MESSAGE)
    worker = threading.Thread(
        target=store_admission.prefetch_session_memory,
        args=(builder, MESSAGE),
        kwargs={"workspace": None, "memory_store": None},
    )
    worker.start()
    assert reading.wait(30), "the prefetch never started reading"
    return worker


def test_a_prefetch_running_past_the_recall_bound_is_joined_not_restarted(
    builder, reads, monkeypatch
):
    """Reads slower than the embed recall bound: the build takes the prefetch.

    Restarting would queue the same reads behind the running ones and finish later
    than reading once without any prefetch.
    """
    from kiro_crew import context as ctx

    expected, _ = _build(builder)
    # Far shorter than the held reads, but above 0, which turns the prefetch off.
    monkeypatch.setattr(_budgets, "_startup_recall_timeout_secs", lambda: 0.001)
    release = threading.Event()
    worker = _start_slow_prefetch(builder, monkeypatch, release)
    done = _record_done_waits()
    monkeypatch.setattr(store_admission, "read_session_memory", _counter(reads))
    reads.clear()
    # Released from the build's own thread once it is waiting on the prefetch.
    real_wait = done.wait

    def wait_then_release(timeout=None):
        release.set()
        return real_wait(timeout)

    done.wait = wait_then_release  # type: ignore[method-assign]
    got, _ = _build(builder)
    worker.join(30)
    assert got == expected
    assert reads == [], "the build restarted reads a running prefetch was finishing"
    assert done.waits == [ctx._PROMPT_BUILD_EMBED_TIMEOUT_SECS]


def test_a_stuck_prefetch_is_given_up_on_at_the_build_embed_deadline(builder, reads, monkeypatch):
    from kiro_crew import context as ctx

    monkeypatch.setattr(ctx, "_PROMPT_BUILD_EMBED_TIMEOUT_SECS", 0.05)
    release = threading.Event()
    worker = _start_slow_prefetch(builder, monkeypatch, release)
    done = _record_done_waits()
    monkeypatch.setattr(store_admission, "read_session_memory", _counter(reads))
    _build(builder)
    release.set()
    worker.join(30)
    assert done.waits == [0.05], "the join is not bounded by the build's embed deadline"
    assert len(reads) == 1, "past the bound the build reads for itself"


def test_a_result_over_the_size_bound_is_not_held(builder, reads, monkeypatch):
    monkeypatch.setattr(store_admission, "_MEMORY_PREFETCH_MAX_CHARS", 10)
    _prefetch(builder)
    entry = _held(MESSAGE)
    assert entry.done.is_set() and entry.reads is None
    reads.clear()
    _build(builder)
    assert len(reads) == 1


def test_the_size_bound_holds_any_result_a_build_could_admit_whole() -> None:
    from kiro_crew import context as ctx

    assert store_admission._MEMORY_PREFETCH_MAX_CHARS >= ctx._resolve_caps(None).protected_context


def test_the_memory_caps_do_not_depend_on_the_model_window() -> None:
    """The prefetch resolves caps without a window; the build passes the client's."""
    from kiro_crew import context as ctx

    fields = ("prefs", "projects", "memory_history", "semantic", "episodic", "prefs_startup")
    unbounded = ctx._resolve_caps(None)
    for window in (32_000, 200_000, 1_000_000):
        caps = ctx._resolve_caps(window)
        assert {f: getattr(caps, f) for f in fields} == {f: getattr(unbounded, f) for f in fields}


def test_an_untaken_prefetch_expires() -> None:
    store_admission.reserve_memory_prefetch(None, None, "old")
    _held("old").created -= store_admission._MEMORY_PREFETCH_TTL_SECS + 1
    store_admission.reserve_memory_prefetch(None, None, "new")
    assert list(store_admission._memory_prefetches) == [_key("new")]


@pytest.mark.asyncio
async def test_a_cold_start_reads_memory_while_the_session_starts(tmp_path, monkeypatch):
    """The runner starts the memory reads before ``get_or_create`` returns."""
    from test_chat_runner_startup_recall import _turn, _turn_state

    state, provider = _turn_state(tmp_path, monkeypatch, registered=False)
    loop = asyncio.get_running_loop()
    reading = asyncio.Event()
    seen: list[str] = []

    def fake_prefetch(builder, text, *, workspace, memory_store):
        seen.append(text)
        loop.call_soon_threadsafe(reading.set)

    monkeypatch.setattr(chat_runner, "prefetch_session_memory", fake_prefetch)

    async def starting_session(*args, **kwargs):
        await asyncio.wait_for(reading.wait(), 30)
        return provider, True, False

    state.sessions.get_or_create = AsyncMock(side_effect=starting_session)
    await _turn(state, "cold-start")
    assert seen == [MESSAGE]


@pytest.mark.asyncio
async def test_a_failed_session_start_drops_its_prefetch(tmp_path, monkeypatch):
    from test_chat_runner_startup_recall import _turn_state

    state, _provider = _turn_state(tmp_path, monkeypatch, registered=False)
    monkeypatch.setattr(chat_runner, "prefetch_session_memory", lambda *a, **k: None)
    state.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("spawn failed"))
    slot = state.get_or_create_slot("failed-start")
    slot.append("user", MESSAGE)
    await asyncio.wait_for(chat_runner._run_chat(state, slot, MESSAGE), 60)
    assert store_admission._memory_prefetches == {}


@pytest.mark.asyncio
async def test_a_prewarmed_session_starts_no_memory_prefetch(tmp_path, monkeypatch):
    """Registered already: the build runs at once, so nothing is read early."""
    from test_chat_runner_startup_recall import _turn, _turn_state

    state, _provider = _turn_state(tmp_path, monkeypatch, registered=True)
    seen: list[str] = []
    monkeypatch.setattr(chat_runner, "prefetch_session_memory", lambda b, t, **k: seen.append(t))
    await _turn(state, "prewarmed")
    state.context_builder.build_message.assert_called_once()
    assert seen == []


@pytest.mark.asyncio
async def test_a_cold_start_build_takes_its_prefetch_and_sends_the_same_context(
    tmp_path, monkeypatch
):
    """A dashboard cold start through the real runner and the real ``build_message``.

    The turn reserves its prefetch under its bindings' store name (``"default"``) and
    the build takes it under the execution record's (``""``). Run once without the
    early reads (the build reads for itself) and once with them: the build takes the
    prefetch, reading nothing itself, and the context sent is byte-for-byte the same.
    """
    from test_chat_runner_startup_recall import _turn, _turn_state

    sent: dict[str, tuple[str, list[str]]] = {}
    for run, prefetch in (("without", False), ("with", True)):
        state, provider = _turn_state(tmp_path, monkeypatch, registered=False)
        builder = state.context_builder
        del builder.build_message  # the real method again, not the mock
        if run == "without":  # one memory directory, written once, read by both runs
            memory = builder.get_memory_for(None, None)
            memory.write_preferences("# Preferences\n- Answer in short sentences.\n")
            memory.write_projects("# Projects\n- Deploy checklist rework.\n")
            memory.append_history("Reviewed the deploy checklist with the team.")
        real_build = builder.build_message
        build_reads: list[str] = []
        all_reads: list[str] = []

        def counted(*args, _all=all_reads, **kwargs):
            _all.append(threading.current_thread().name)
            return _REAL_READS(*args, **kwargs)

        def build(*args, _real=real_build, _reads=build_reads, _all=all_reads, **kwargs):
            before = len(_all)
            out = _real(*args, **kwargs)
            _reads.extend(_all[before:])
            return out

        monkeypatch.setattr(store_admission, "read_session_memory", counted)
        loop = asyncio.get_running_loop()
        prefetched = asyncio.Event()

        def real_prefetch(*args, _done=prefetched, **kwargs):
            try:
                store_admission.prefetch_session_memory(*args, **kwargs)
            finally:
                loop.call_soon_threadsafe(_done.set)

        monkeypatch.setattr(
            chat_runner,
            "prefetch_session_memory",
            real_prefetch if prefetch else (lambda *a, **k: None),
        )
        if prefetch:
            # The session finishes starting only once the early reads have run, as a
            # real cold start outlasts them; without this the build could come first.
            async def starting_session(*args, _done=prefetched, _p=provider, **kwargs):
                await asyncio.wait_for(_done.wait(), 30)  # bound names a hang only
                return _p, True, False

            state.sessions.get_or_create = AsyncMock(side_effect=starting_session)
        monkeypatch.setattr(builder, "build_message", build)
        captured: list[str] = []
        real_stream = provider.stream

        async def stream(prompt, *args, _cap=captured, _real=real_stream, **kwargs):
            _cap.append(prompt)
            async for event in _real(prompt, *args, **kwargs):
                yield event

        provider.stream = stream
        await _turn(state, "cold-start")
        assert len(captured) == 1
        sent[run] = (_CURRENT_DATE_LINE.sub("[CURRENT DATE] <now>", captured[0]), build_reads)

    assert "Answer in short sentences." in sent["without"][0]
    assert sent["with"][0] == sent["without"][0]
    assert sent["without"][1], "without the prefetch the build reads memory itself"
    assert sent["with"][1] == [], "the build read memory again instead of taking the prefetch"


def test_the_table_holds_no_copy_of_the_message(builder) -> None:
    """A huge paste must not be retained by the prefetch table, as key or input."""
    paste = "x" * 200_000
    _prefetch(builder, paste)
    held = repr(store_admission._memory_prefetches)
    assert paste[:1000] not in held
    assert len(held) < 10_000


def test_the_build_cuts_the_prefetch_embed_deadline_to_its_own(builder) -> None:
    """A slow embed then ends where the build's own read would have stopped waiting."""
    from kiro_crew import context as ctx
    from kiro_crew.embeddings import EmbeddingWork, embedding_work

    store_admission.reserve_memory_prefetch(None, None, MESSAGE)
    entry = _held(MESSAGE)
    entry.work = EmbeddingWork(deadline=10_000_000.0)
    entry.started.set()
    entry.done.set()
    previous = embedding_work.get()
    embedding_work.set(EmbeddingWork(deadline=123.0))
    try:
        memory = builder.get_memory_for(None, None)
        store_admission._take_memory_prefetch(
            memory,
            ctx._resolve_caps(None),
            workspace=None,
            memory_store=None,
            query=MESSAGE,
            inject_activity=True,
        )
    finally:
        embedding_work.set(previous)
    assert entry.work.deadline == 123.0


def test_the_prefetch_records_the_embed_budget_it_runs_under(builder) -> None:
    from kiro_crew.embeddings import EmbeddingWork, embedding_work

    work = EmbeddingWork(deadline=10_000_000.0)
    store_admission.reserve_memory_prefetch(None, None, MESSAGE)
    previous = embedding_work.get()
    embedding_work.set(work)
    try:
        store_admission.prefetch_session_memory(builder, MESSAGE, workspace=None, memory_store=None)
    finally:
        embedding_work.set(previous)
    assert _held(MESSAGE).work is work


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
@pytest.mark.parametrize("leaf", ["preferences", "projects", "history"])
def test_a_linked_memory_source_turns_the_prefetch_off(builder, tmp_path, leaf) -> None:
    """Metadata is read only behind the reader's admission; a link is never followed."""
    from kiro_crew import context as ctx

    memory = builder.get_memory_for(None, None)
    path = {
        "preferences": memory._preferences_file,
        "projects": memory._projects_file,
        "history": memory._history_dir,
    }[leaf]
    target = tmp_path / f"elsewhere-{leaf}"
    if path.is_dir():
        shutil.rmtree(path)
        target.mkdir()
    else:
        path.unlink(missing_ok=True)
        target.write_text("planted\n")
    try:
        os.symlink(target, path, target_is_directory=target.is_dir())
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")
    assert (
        store_admission._memory_read_inputs(memory, ctx._resolve_caps(None), MESSAGE, True) is None
    )
    store_admission.reserve_memory_prefetch(None, None, MESSAGE)
    store_admission.prefetch_session_memory(builder, MESSAGE, workspace=None, memory_store=None)
    assert _held(MESSAGE).reads is None


def test_a_store_on_another_file_provider_turns_the_prefetch_off(builder, monkeypatch) -> None:
    """Local metadata says nothing about content served by another provider."""
    from kiro_crew import context as ctx

    memory = builder.get_memory_for(None, None)
    monkeypatch.setattr(memory, "_files_cache", object())
    assert (
        store_admission._memory_read_inputs(memory, ctx._resolve_caps(None), MESSAGE, True) is None
    )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_link_inside_the_history_directory_is_never_followed(builder, tmp_path) -> None:
    """The root guard passes; the entry stat itself must not follow either link."""
    from kiro_crew import context as ctx

    memory = builder.get_memory_for(None, None)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"x" * 1_000_000)
    try:
        os.symlink(outside, memory._history_dir / "2099-01-01.md")
        os.symlink(tmp_path / "missing.md", memory._history_dir / "2099-01-02.md")
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")
    inputs = store_admission._memory_read_inputs(memory, ctx._resolve_caps(None), MESSAGE, True)
    assert inputs is not None, "a dangling link must not make the metadata unreadable"
    history = store_admission._admitted_file_stats(memory)[2]
    assert history is not None
    assert history[2] < 1_000_000, "the outside file's size was counted"


def test_the_join_ends_at_the_build_embed_deadline(builder, monkeypatch) -> None:
    """An embed still in flight gives the build the short bound; the join keeps to it."""
    from kiro_crew import context as ctx
    from kiro_crew.embeddings import EmbeddingWork, embedding_work

    # Pin store_admission.time to a manual clock so the deadline computation is
    # exact: (monotonic_start + 0.05) - monotonic_start == 0.05 by construction.
    # Without this, a coarse Windows monotonic clock (~15.6 ms ticks) makes two
    # real readings return the same float, and IEEE rounding of large_float + 0.05
    # yields a value strictly above 0.05, breaking the equality check.
    clock = ManualClock(monotonic_start=0.0)
    clock.install(monkeypatch, store_admission)

    store_admission.reserve_memory_prefetch(None, None, MESSAGE)
    entry = _held(MESSAGE)
    entry.started.set()  # running, never done
    done = _record_done_waits()
    previous = embedding_work.get()
    embedding_work.set(EmbeddingWork(deadline=store_admission.time.monotonic() + 0.05))
    try:
        got = store_admission._take_memory_prefetch(
            builder.get_memory_for(None, None),
            ctx._resolve_caps(None),
            workspace=None,
            memory_store=None,
            query=MESSAGE,
            inject_activity=True,
        )
    finally:
        embedding_work.set(previous)
    assert got is None
    assert len(done.waits) == 1 and done.waits[0] == 0.05


def test_a_prefetch_whose_embed_wait_is_cut_is_joined_past_the_build_deadline(
    builder, monkeypatch
) -> None:
    """Cut to the build's deadline, the prefetch's reads after it are the keyword
    reads the build would make itself: the join keeps the long bound rather than end
    at that deadline and run them a second time beside the prefetch's."""
    from kiro_crew import context as ctx
    from kiro_crew.embeddings import EmbeddingWork, embedding_work

    clock = ManualClock(monotonic_start=0.0)
    clock.install(monkeypatch, store_admission)
    store_admission.reserve_memory_prefetch(None, None, MESSAGE)
    entry = _held(MESSAGE)
    entry.work = EmbeddingWork(deadline=30.0)
    entry.started.set()
    done = _record_done_waits()
    done.set()  # so the join returns at once; the bound it was given is what is checked
    previous = embedding_work.get()
    embedding_work.set(EmbeddingWork(deadline=0.05))
    try:
        store_admission._take_memory_prefetch(
            builder.get_memory_for(None, None),
            ctx._resolve_caps(None),
            workspace=None,
            memory_store=None,
            query=MESSAGE,
            inject_activity=True,
        )
    finally:
        embedding_work.set(previous)
    assert entry.work.deadline == 0.05
    assert done.waits == [ctx._PROMPT_BUILD_EMBED_TIMEOUT_SECS]


def test_a_prefetch_older_than_its_ttl_is_not_taken(builder, reads) -> None:
    _prefetch(builder)
    _held(MESSAGE).created -= store_admission._MEMORY_PREFETCH_TTL_SECS + 1
    reads.clear()
    _build(builder)
    assert len(reads) == 1


def test_turning_the_early_embed_off_turns_the_early_reads_off(builder, reads, monkeypatch):
    """``startup_recall_timeout_secs: 0`` must also stop the reads, which embed the message."""
    monkeypatch.setattr(_budgets, "_startup_recall_timeout_secs", lambda: 0.0)
    _prefetch(builder)
    entry = _held(MESSAGE)
    assert not entry.started.is_set() and entry.reads is None
    assert reads == [], "the prefetch read (and embedded) memory with the early embed off"
    _build(builder)
    assert len(reads) == 1
