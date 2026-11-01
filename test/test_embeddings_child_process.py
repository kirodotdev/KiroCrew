"""Embedding inference in a child process (``kiro_crew.embeddings_child``).

Every test here spawns a real child interpreter over real pipes. The child loads
``embed_child_fake.FakeLlama`` (this directory is put on its ``PYTHONPATH``)
instead of the vendored runtime, so no model is downloaded or run.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import embed_child_fake
import embeddings_helpers
import pytest
from embeddings_helpers import _write_model_file

import kiro_crew.embeddings as embeddings_mod
import kiro_crew.embeddings_child as child_mod
from kiro_crew.embeddings import (
    PRIORITY_BULK,
    PRIORITY_INTERACTIVE,
    EmbeddingWork,
    LlamaCppEmbedder,
    embedding_work,
)
from kiro_crew.embeddings_child import ChildEmbeddingError, ChildLlama
from kiro_crew.testing.wait import wait_until

_reset_embedding_singletons = embeddings_helpers._reset_embedding_singletons

_TEST_DIR = str(Path(__file__).resolve().parent)
_FACTORY = "embed_child_fake:load_class"
#: Enforced idle between bulk jobs in the pacing test.
_PACE_SECS = 2.0
#: A model call slower than any caller deadline used here.
_SLOW_CALL_SECS = 5.0
#: First respawn delay in the kill test.
_BACKOFF_SECS = 2.0
#: Length of one GIL-holding model call in the event-loop test.
_SPIN_SECS = 0.6
#: Loop iterations that must happen during the four spins (a free loop runs
#: hundreds of thousands).
_MIN_LOOP_TICKS = 1000

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and /proc")


def _alive(pid: int) -> bool:
    """True while *pid* runs; a zombie (exited, not yet reaped) counts as gone."""
    stat = Path(f"/proc/{pid}/stat")
    if stat.parent.parent.is_dir() and Path("/proc/self").exists():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def child_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the child import the fake, and route the embedder's loads to the child."""
    existing = os.environ.get("PYTHONPATH")
    monkeypatch.setenv("PYTHONPATH", _TEST_DIR + (os.pathsep + existing if existing else ""))
    monkeypatch.setattr(child_mod, "_CHILD_LLAMA_FACTORY", _FACTORY)


@pytest.fixture
def child_embedder(child_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A LlamaCppEmbedder whose model lives in a child process running the fake."""
    monkeypatch.setattr(embeddings_mod, "_load_llama_class", lambda: embed_child_fake.FakeLlama)
    monkeypatch.setattr(embeddings_mod, "is_vendored_llama_class", lambda cls: True)
    monkeypatch.setattr(embeddings_mod, "embedding_child_process_enabled", lambda: True)
    emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"), dim=16)
    assert emb.wait_ready(timeout=60)
    assert isinstance(emb._llm, ChildLlama)
    yield emb
    emb.retire()


def _child(emb: LlamaCppEmbedder) -> ChildLlama:
    llm = emb._llm
    assert isinstance(llm, ChildLlama)
    return llm


class TestOffByDefault:
    """With the switch unset, the vendored model runs in-process exactly as before."""

    @pytest.mark.parametrize(
        ("memory", "expected"),
        [
            ({}, False),
            ({"embedding_child_process": False}, False),
            ({"embedding_child_process": "true"}, False),
            ({"embedding_child_process": 1}, False),
            ({"embedding_child_process": True}, True),
        ],
    )
    def test_only_a_literal_true_turns_the_child_on(self, monkeypatch, memory, expected):
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: memory)
        assert embeddings_mod.embedding_child_process_enabled() is expected

    def test_default_loads_the_vendored_class_in_process_and_spawns_nothing(
        self, tmp_path: Path, monkeypatch
    ):
        fake_cls = embeddings_helpers._make_fake_llama_class(dim=16)
        monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: {})
        monkeypatch.setattr(embeddings_mod, "_load_llama_class", lambda: fake_cls)
        monkeypatch.setattr(embeddings_mod, "is_vendored_llama_class", lambda cls: True)

        def no_spawn(self):
            raise AssertionError("the default path must not start an embedding child")

        monkeypatch.setattr(child_mod._ChildState, "spawn", no_spawn)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"), dim=16)
        try:
            assert emb.embed_batch(["hello", "world"]) == [[0.1] * 16, [0.1] * 16]
            assert emb.embed("bulk row", priority=PRIORITY_BULK) == [0.1] * 16
            (llm,) = fake_cls.instances
            assert emb._llm is llm
            assert llm.embed_calls == [["hello", "world"], ["bulk row"]]
            assert set(llm.kwargs) == {
                "model_path",
                "embedding",
                "pooling_type",
                "n_ctx",
                "n_batch",
                "n_ubatch",
                "n_threads",
                "n_threads_batch",
                "verbose",
            }
            assert llm.kwargs["model_path"] == str(tmp_path / "model.gguf")
        finally:
            emb.retire()
        assert not [t for t in threading.enumerate() if t.name.startswith("kc-embed-child")]


class TestSameVectors:
    def test_child_returns_the_in_process_vectors_bit_for_bit(self, child_env):
        texts = ["recall the deploy plan", "knowledge chunk", "x" * 500]
        local = embed_child_fake.FakeLlama().create_embedding(texts)
        llm = ChildLlama({"model_path": "unused"}, stamp=None, factory=_FACTORY)
        try:
            assert llm.n_embd() == embed_child_fake.DIM
            assert llm.create_embedding(texts) == local
        finally:
            llm.close()

    def test_embedder_vectors_match_with_the_switch_on_and_off(
        self, child_embedder, tmp_path: Path, monkeypatch
    ):
        texts = ["memory recall", "consolidation block"]
        via_child = child_embedder.embed_batch(texts, priority=PRIORITY_INTERACTIVE)
        monkeypatch.setattr(embeddings_mod, "embedding_child_process_enabled", lambda: False)
        local = LlamaCppEmbedder(model_path=tmp_path / "model.gguf", dim=16)
        try:
            assert local.wait_ready(timeout=10)
            assert isinstance(local._llm, embed_child_fake.FakeLlama)  # off switch: in-process
            assert local.embed_batch(texts) == via_child
        finally:
            local.retire()

    def test_native_stdout_noise_does_not_corrupt_the_protocol(self, child_embedder):
        vectors = child_embedder.embed_batch(["__print", "after"])
        assert vectors is not None
        assert vectors[1] == embed_child_fake.vector_for("after")

    def test_thread_class_is_forwarded_to_the_child(self, child_embedder, monkeypatch):
        monkeypatch.setattr(embeddings_mod, "bulk_embed_threads", lambda: 1)
        monkeypatch.setattr(embeddings_mod, "_embed_threads", lambda: 3)
        monkeypatch.setattr(embeddings_mod, "bulk_pace_delay", lambda elapsed: 0.0)
        assert child_embedder.embed_batch(["__threads"])[0][:2] == [3.0, 3.0]
        bulk = child_embedder.embed_batch(["__threads"], priority=PRIORITY_BULK)
        assert bulk[0][:2] == [1.0, 1.0]

    def test_thread_class_set_while_a_child_loads_reaches_it(self, child_embedder, monkeypatch):
        """A count recorded after the load request was built is sent once the child is up."""
        monkeypatch.setattr(child_mod, "_CHILD_RESPAWN_BASE_SECS", 0.2)
        llm = _child(child_embedder)
        state = llm._state
        state.set_threads(4, 4)
        real_send = child_mod._Conn.send

        def send(conn, msg):
            real_send(conn, msg)
            if msg.get("op") == "load":
                state.set_threads(1, 1)  # lands while this child is loading

        monkeypatch.setattr(child_mod._Conn, "send", send)
        old = llm.pid
        assert old is not None
        os.kill(old, signal.SIGKILL)
        wait_until(lambda: llm.pid not in (None, old), timeout=30)
        assert llm.create_embedding(["__threads"])["data"][0]["embedding"][:2] == [1.0, 1.0]


class TestPriority:
    @pytest.mark.skipif(not hasattr(os, "getpriority"), reason="needs os.getpriority")
    def test_child_runs_at_lower_cpu_priority(self, child_embedder):
        pid = _child(child_embedder).pid
        assert pid is not None
        assert os.getpriority(os.PRIO_PROCESS, pid) >= os.getpriority(os.PRIO_PROCESS, 0) + min(
            child_mod._CHILD_NICE, 19 - os.getpriority(os.PRIO_PROCESS, 0)
        )

    def test_bulk_rows_are_paced_through_the_child(self, child_embedder, monkeypatch):
        monkeypatch.setattr(embeddings_mod, "bulk_pace_delay", lambda elapsed: _PACE_SECS)
        start = time.monotonic()
        for i in range(3):
            assert child_embedder.embed_batch([f"row {i}"], priority=PRIORITY_BULK) is not None
        # Three bulk rows with an enforced idle between them: at least two pauses.
        assert time.monotonic() - start >= 2 * _PACE_SECS * 0.9

    def test_interactive_embed_jumps_a_bulk_pause(self, child_embedder, monkeypatch):
        """The bulk pause is effectively endless; only queue order lets the query through."""
        monkeypatch.setattr(embeddings_mod, "bulk_pace_delay", lambda elapsed: 3600.0)
        done: list[str] = []

        def bulk(i: int) -> None:
            assert child_embedder.embed_batch([f"row {i}"], priority=PRIORITY_BULK) is not None
            done.append(f"bulk{i}")

        first = threading.Thread(target=bulk, args=(0,))
        first.start()
        first.join(30)
        assert done == ["bulk0"]
        second = threading.Thread(target=bulk, args=(1,))
        second.start()
        wait_until(lambda: child_embedder._jobs.qsize() == 1, timeout=10)
        assert child_embedder.embed_batch(["urgent"], priority=PRIORITY_INTERACTIVE) == [
            embed_child_fake.vector_for("urgent")
        ]
        assert done == ["bulk0"]  # the queued bulk row is still waiting out its pause
        # Release the pause so the bulk row finishes.
        child_embedder._bulk_ready_at = 0.0
        child_embedder._jobs_changed.set()
        second.join(30)
        assert done == ["bulk0", "bulk1"]


class TestDegradeAndRespawn:
    def test_caller_deadline_degrades_to_none_without_killing_the_child(self, child_embedder):
        pid = _child(child_embedder).pid
        previous = embedding_work.get()
        embedding_work.set(EmbeddingWork(time.monotonic() + 0.5, priority=PRIORITY_INTERACTIVE))
        try:
            slow = f"__sleep:{_SLOW_CALL_SECS}"
            assert child_embedder.embed_batch([slow], priority=PRIORITY_INTERACTIVE) is None
            # Degraded at the caller's deadline: the child has not answered yet.
            conn = _child(child_embedder)._state.conn
            assert conn is not None and len(conn.outstanding) == 1
        finally:
            embedding_work.set(previous)
        assert _child(child_embedder).pid == pid
        assert child_embedder.embed_batch(["later"]) == [embed_child_fake.vector_for("later")]

    def test_abandoned_request_is_not_queued_behind_in_the_child(self, child_embedder):
        """A caller that cannot wait out an abandoned request degrades; nothing is sent."""
        state = _child(child_embedder)._state
        previous = embedding_work.get()
        try:
            for text in (f"__sleep:{_SLOW_CALL_SECS}", "second", "third"):
                embedding_work.set(
                    EmbeddingWork(time.monotonic() + 0.3, priority=PRIORITY_INTERACTIVE)
                )
                assert child_embedder.embed_batch([text], priority=PRIORITY_INTERACTIVE) is None
                conn = state.conn
                assert conn is not None and len(conn.outstanding) == 1  # only the first
        finally:
            embedding_work.set(previous)
        # Once the child answers the abandoned request, the next caller is served.
        assert child_embedder.embed_batch(["later"]) == [embed_child_fake.vector_for("later")]

    def test_unanswered_call_kills_and_respawns_the_child(self, child_embedder, monkeypatch):
        monkeypatch.setattr(child_mod, "_CHILD_CALL_BASE_SECS", 0.5)
        monkeypatch.setattr(child_mod, "_CHILD_CALL_SECS_PER_TEXT", 0.0)
        monkeypatch.setattr(child_mod, "_CHILD_RESPAWN_BASE_SECS", 0.2)
        llm = _child(child_embedder)
        old = llm.pid
        assert child_embedder.embed_batch(["__sleep:30"]) is None
        wait_until(lambda: llm.pid not in (None, old), timeout=30)
        assert old is not None and not _alive(old)
        assert child_embedder.embed_batch(["back"]) == [embed_child_fake.vector_for("back")]

    def test_killed_child_degrades_then_respawns_with_backoff(self, child_embedder, monkeypatch):
        monkeypatch.setattr(child_mod, "_CHILD_RESPAWN_BASE_SECS", _BACKOFF_SECS)
        monkeypatch.setattr(child_mod, "_CHILD_STABLE_SECS", 0.0)
        llm = _child(child_embedder)
        old = llm.pid
        assert old is not None
        os.kill(old, signal.SIGKILL)
        wait_until(lambda: llm.pid != old, timeout=10)
        killed_at = time.monotonic()
        # While it respawns, embeds degrade instead of waiting for the new child.
        assert child_embedder.embed_batch(["during"]) is None
        wait_until(lambda: llm.pid is not None, timeout=30)
        assert time.monotonic() - killed_at >= _BACKOFF_SECS * 0.8  # waited out the backoff
        assert child_embedder.embed_batch(["after"]) == [embed_child_fake.vector_for("after")]
        assert llm._state.failures == 0  # a served embed resets the backoff

    def test_crash_loop_backs_off_exponentially_to_the_cap(self, child_env, monkeypatch):
        monkeypatch.setattr(child_mod, "_CHILD_RESPAWN_BASE_SECS", 1.0)
        monkeypatch.setattr(child_mod, "_CHILD_RESPAWN_CAP_SECS", 8.0)
        state = child_mod._ChildState({}, _FACTORY)
        delays = []
        for failures in range(1, 8):
            state.failures = failures
            delays.append(state.respawn_delay())
        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0]

    def test_wedged_child_is_restarted_by_interactive_callers_alone(
        self, child_embedder, monkeypatch
    ):
        """Every caller gives up at its own deadline; the wedge is still detected."""
        monkeypatch.setattr(child_mod, "_CHILD_CALL_BASE_SECS", _BACKOFF_SECS)
        monkeypatch.setattr(child_mod, "_CHILD_CALL_SECS_PER_TEXT", 0.0)
        monkeypatch.setattr(child_mod, "_CHILD_RESPAWN_BASE_SECS", 0.2)
        llm = _child(child_embedder)
        old = llm.pid
        previous = embedding_work.get()

        def interactive(text: str) -> object:
            embedding_work.set(
                EmbeddingWork(time.monotonic() + _BACKOFF_SECS / 4, priority=PRIORITY_INTERACTIVE)
            )
            try:
                return child_embedder.embed_batch([text], priority=PRIORITY_INTERACTIVE)
            finally:
                embedding_work.set(previous)

        assert interactive(f"__sleep:{_SLOW_CALL_SECS * 6}") is None

        def restarted() -> bool:
            interactive("probe")
            return llm.pid not in (None, old)

        wait_until(restarted, timeout=60)
        assert old is not None and not _alive(old)

    def test_close_does_not_block_on_a_child_that_stopped_reading(self, child_embedder):
        """A request bigger than the pipe buffer, sent to a busy child, cannot wedge close()."""
        pid = _child(child_embedder).pid
        assert pid is not None
        previous = embedding_work.get()
        embedding_work.set(EmbeddingWork(time.monotonic() + 0.3, priority=PRIORITY_INTERACTIVE))
        try:
            assert child_embedder.embed_batch([f"__sleep:{_SLOW_CALL_SECS * 6}"]) is None
            embedding_work.set(EmbeddingWork(time.monotonic() + 0.3, priority=PRIORITY_INTERACTIVE))
            # json escapes non-ASCII, so this is several times the 64 KiB pipe buffer.
            assert child_embedder.embed_batch(["\u8bb0\u5fc6" * 3000] * 8) is None
        finally:
            embedding_work.set(previous)
        closer = threading.Thread(target=child_embedder.close)
        closer.start()
        closer.join(timeout=60)
        assert not closer.is_alive(), "close() blocked on the child's pipe"
        wait_until(lambda: not _alive(pid), timeout=30)

    def test_backoff_holds_while_a_respawned_child_keeps_crashing(
        self, child_embedder, monkeypatch
    ):
        """A crash right after an answered embed does not reset the delay to its base."""
        monkeypatch.setattr(child_mod, "_CHILD_RESPAWN_BASE_SECS", 0.2)
        llm = _child(child_embedder)
        state = llm._state
        assert child_embedder.embed_batch(["__crash"]) is None
        wait_until(lambda: llm.pid is not None, timeout=30)
        assert child_embedder.embed_batch(["fine"]) is not None  # served, but not yet stable
        assert state.failures == 1
        assert child_embedder.embed_batch(["__crash"]) is None
        wait_until(lambda: state.failures >= 2, timeout=30)

    def test_crash_during_embed_fails_the_waiter(self, child_embedder):
        assert child_embedder.embed_batch(["__crash"]) is None

    def test_failed_load_is_reported_as_a_failed_load(self, child_env):
        with pytest.raises(ChildEmbeddingError):
            ChildLlama({}, stamp=None, factory="embed_child_fake:no_such_factory")


class TestLifecycle:
    def test_close_stops_the_child(self, child_embedder):
        pid = _child(child_embedder).pid
        assert pid is not None
        child_embedder.close()
        wait_until(lambda: not _alive(pid), timeout=10)

    def test_child_exits_when_its_parent_is_killed(self, child_env, tmp_path: Path):
        script = tmp_path / "parent.py"
        script.write_text(textwrap.dedent(f"""
                import sys, time
                from kiro_crew.embeddings_child import ChildLlama
                llm = ChildLlama({{}}, stamp=None, factory={_FACTORY!r})
                print(llm.pid, flush=True)
                time.sleep(120)
                """))
        parent = subprocess.Popen(
            [sys.executable, str(script)], stdout=subprocess.PIPE, text=True, encoding="utf-8"
        )
        try:
            assert parent.stdout is not None
            child_pid = int(parent.stdout.readline().strip())
            assert _alive(child_pid)
            parent.kill()
            parent.wait(10)
            wait_until(lambda: not _alive(child_pid), timeout=15)
        finally:
            if parent.poll() is None:
                parent.kill()


class TestEventLoop:
    def test_loop_keeps_running_while_the_child_computes(self, child_embedder):
        """GIL-holding model calls in the child leave the parent's loop free to run.

        Four calls of ``_SPIN_SECS`` each run back to back in the child. A loop
        task that only yields counts how often it ran meanwhile; a loop starved
        for the whole inference would count close to zero.
        """

        async def scenario() -> tuple[int, list]:
            loop = asyncio.get_running_loop()
            ticks = 0
            stop = asyncio.Event()

            async def ticker() -> None:
                nonlocal ticks
                while not stop.is_set():
                    ticks += 1
                    await asyncio.sleep(0)

            tick = asyncio.create_task(ticker())
            results = await asyncio.gather(
                *(
                    loop.run_in_executor(None, child_embedder.embed, f"__spin:{_SPIN_SECS}")
                    for _ in range(4)
                )
            )
            stop.set()
            await tick
            return ticks, results

        ticks, results = asyncio.run(scenario())
        assert all(r is not None for r in results)
        assert ticks >= _MIN_LOOP_TICKS, f"the loop ran only {ticks} times during inference"


class TestChildSide:
    """The child's request loop and loader, run in this process over byte streams."""

    @staticmethod
    def _lines(*msgs: object) -> io.BytesIO:
        return io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in msgs))

    @staticmethod
    def _replies(out: io.BytesIO) -> list[dict]:
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def _load_request(self, **extra: object) -> dict:
        request: dict = {"op": "load", "kwargs": {}, "stamp": None, "factory": _FACTORY}
        request["nice"] = 0
        request.update(extra)
        return request

    def test_serve_loads_answers_and_exits(self, child_env):
        stdin = self._lines(
            self._load_request(threads=[3, 3]),
            {"id": 1, "op": "embed", "texts": ["__threads"]},
            {"op": "threads", "n": 2, "nb": 2},
            {"id": 2, "op": "embed", "texts": ["__threads", "a"]},
            {"op": "unknown"},
            {"id": 3, "op": "embed", "texts": ["__sleep:not-a-number"]},
            {"op": "exit"},
            {"id": 4, "op": "embed", "texts": ["never read"]},
        )
        out = io.BytesIO()
        assert child_mod._serve(stdin, out) == 0
        ready, first, second, failed = self._replies(out)
        assert ready["op"] == "ready" and ready["n_embd"] == embed_child_fake.DIM
        assert first == {"id": 1, "ok": True, "vectors": [[3.0, 3.0] + [0.0] * 14]}
        assert second["vectors"] == [[2.0, 2.0] + [0.0] * 14, embed_child_fake.vector_for("a")]
        assert failed["id"] == 3 and failed["ok"] is False and "ValueError" in failed["error"]

    def test_serve_reports_a_failed_load(self, child_env):
        stdin = self._lines(self._load_request(factory="embed_child_fake:missing"))
        out = io.BytesIO()
        assert child_mod._serve(stdin, out) == 1
        (reply,) = self._replies(out)
        assert reply["op"] == "error" and "AttributeError" in reply["error"]

    def test_serve_stops_on_eof_and_on_a_malformed_line(self, child_env):
        assert child_mod._serve(io.BytesIO(b""), io.BytesIO()) == 0
        stdin = io.BytesIO(json.dumps(self._load_request()).encode() + b"\nnot json\n")
        assert child_mod._serve(stdin, io.BytesIO()) == 1

    def test_load_refuses_a_file_changed_since_the_parent_checked_it(
        self, child_env, tmp_path: Path
    ):
        model = _write_model_file(tmp_path / "model.gguf")
        stamp = list(embeddings_mod._model_file_stamp_unaliased(model))
        request = self._load_request(kwargs={"model_path": str(model)}, stamp=stamp)
        llm, n_embd = child_mod._load(request)
        assert n_embd == embed_child_fake.DIM and isinstance(llm, embed_child_fake.FakeLlama)
        request["stamp"] = [stamp[0] + 1] + stamp[1:]
        with pytest.raises(ChildEmbeddingError, match="changed since"):
            child_mod._load(request)

    def test_load_refuses_a_protected_path_and_a_missing_runtime(self, child_env, monkeypatch):
        monkeypatch.setattr(child_mod, "_resolve_factory", lambda spec: None)
        with pytest.raises(ChildEmbeddingError, match="unavailable"):
            child_mod._load(self._load_request())
        monkeypatch.setattr(embeddings_mod, "_is_sensitive_model_path", lambda path: True)
        with pytest.raises(ChildEmbeddingError, match="protected"):
            child_mod._load(self._load_request())

    def test_set_threads_tolerates_a_runtime_without_the_dial(self):
        child_mod._set_threads(object(), 2, 2)  # no _ctx: keeps the loaded count

        class _Broken:
            class _ctx:  # noqa: N801 - mirrors the runtime attribute name
                @staticmethod
                def set_n_threads(a: int, b: int) -> None:
                    raise RuntimeError("no dial")

        child_mod._set_threads(_Broken(), 2, 2)

    def test_vendored_class_detection(self):
        assert not child_mod.is_vendored_llama_class(embed_child_fake.FakeLlama)
        vendored = type("Llama", (), {"__module__": "llama_cpp.llama"})
        assert child_mod.is_vendored_llama_class(vendored)

    def test_parent_gone_reads_the_parent_pid_on_posix(self, monkeypatch):
        monkeypatch.setattr(child_mod, "_IS_WINDOWS", False)
        assert child_mod._parent_gone(os.getppid()) is False
        assert child_mod._parent_gone(os.getppid() + 1) is True

    def test_parent_gone_asks_liveness_on_windows(self, monkeypatch):
        import kiro_crew.platform_compat as platform_compat

        monkeypatch.setattr(child_mod, "_IS_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: pid == 42)
        assert child_mod._parent_gone(42) is False
        assert child_mod._parent_gone(43) is True

    def test_windows_child_gets_no_console_and_below_normal_priority(self, monkeypatch):
        monkeypatch.setattr(child_mod, "_SUBPROCESS_NO_WINDOW", 0x08000000)
        monkeypatch.setattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, raising=False)
        assert child_mod._child_creationflags() == 0x08000000 | 0x4000
