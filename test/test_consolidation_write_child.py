"""History consolidation's vector writes in a child process.

The end-to-end tests spawn the real child interpreter against a real
``memory.db`` and a real ``ConversationLog`` and check the rows the gateway-side
store reads back. The stub-child tests use a tiny script in place of the child to
drive the failure paths (hang, crash, error frames) deterministically.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import signal
import sqlite3
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew.consolidation_write_child as cwc
import kiro_crew.consolidation_write_offload as offload
from kiro_crew.consolidation_write_child import ChildUnavailable, WriteChild
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.history_consolidation_span import (
    _LeaseLostMidRun,
    _PersistenceDisabledMidRun,
    _RunCommitState,
)
from kiro_crew.history_derivation import TranscriptWithheld
from kiro_crew.testing.wait import wait_until
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and /proc")

_DIM = 4
_KEY = "dashboard:chat-cwc"


def _vector(text: str) -> list[float]:
    rng = random.Random(int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big"))
    return [rng.uniform(-1.0, 1.0) for _ in range(_DIM)]


class _Embedder:
    """The gateway-side embedder: counts calls and can run a hook on the first one."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.on_first = None

    def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        if len(self.calls) == 1 and self.on_first is not None:
            self.on_first()
        return _vector(text)


@pytest.fixture
def config_file():
    """Write ``config.json`` under the test home and remove it afterwards."""
    from kiro_crew.config.loader import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(data: dict) -> None:
        path.write_text(json.dumps(data))

    try:
        yield write
    finally:
        path.unlink(missing_ok=True)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """A fresh write child handle the offload module uses, with the switch on."""
    handle = WriteChild(job_timeout_secs=60.0, queue_wait_secs=30.0, ready_timeout_secs=60.0)
    monkeypatch.setattr(offload, "get_client", lambda: handle)
    monkeypatch.setattr(offload, "write_child_enabled", lambda: True)
    yield handle
    handle.close()


@pytest.fixture
def world(tmp_path: Path, client: WriteChild):
    """A real store (with a counting embedder), a real log, and a consolidator over both."""
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=_DIM)
    store.init()
    embedder = _Embedder()
    store.embed_fn = embedder
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.append(_KEY, "user", "hello")
    consolidator = HistoryConsolidator(
        log=log, memory=MagicMock(), vector_store=store, migrated=True
    )
    # Count the gateway-side writers: a job the child ran must leave these at zero.
    here = {"structured": 0, "lessons": 0}
    real_structured = consolidator._write_structured_memory
    real_lessons = consolidator._save_lessons

    def structured(*a, **k):
        here["structured"] += 1
        return real_structured(*a, **k)

    def lessons(*a, **k):
        here["lessons"] += 1
        return real_lessons(*a, **k)

    consolidator._write_structured_memory = structured
    consolidator._save_lessons = lessons
    try:
        yield store, consolidator, embedder, here
    finally:
        store.close()


def _episodic_rows(store: VectorMemoryStore) -> list[sqlite3.Row]:
    with store._db_lock:
        return store.db.execute(
            "SELECT text, embedding IS NOT NULL AS embedded FROM episodic_memories "
            "WHERE is_deleted = 0"
        ).fetchall()


# ── End to end, real child ──


def test_the_child_writes_semantic_and_episodic_rows_and_the_gateway_embeds(world, client) -> None:
    store, consolidator, embedder, here = world
    result = {
        "semantic": [
            {"key": "user.editor", "value": "neovim", "confidence": 0.9},
            {"key": "project.name", "value": "Kiro Crew", "confidence": 0.8},
        ],
        "episodic": [{"text": "Mingwei fixed the consolidation writes", "importance": 0.6}],
    }

    offload.write_structured_memory(
        consolidator, result, _KEY, store, visible_keys=frozenset(), commit_state=_RunCommitState()
    )

    assert here["structured"] == 0, "the job ran in the gateway"
    assert client.spawns == 1 and client.pid() not in (None, os.getpid())
    assert json.loads(store.get_semantic("user.editor")["value_json"]) == "neovim"
    assert json.loads(store.get_semantic("project.name")["value_json"]) == "Kiro Crew"
    assert store.get_semantic("user.editor")["source"] == f"consolidation:{_KEY}"
    episodes = _episodic_rows(store)
    assert [r["text"] for r in episodes] == ["Mingwei fixed the consolidation writes"]
    # Embeddings came from the gateway's embedder, not a second model in the child.
    assert embedder.calls, "the child never asked the gateway to embed"
    assert episodes[0]["embedded"] == 1


def test_the_run_committed_latch_follows_the_child(world, client) -> None:
    store, consolidator, _embedder, _here = world
    state = _RunCommitState()

    offload.write_structured_memory(
        consolidator,
        {"semantic": [{"key": "user.shell", "value": "zsh", "confidence": 0.9}]},
        _KEY,
        store,
        commit_state=state,
    )

    assert state.committed is True


def test_a_delete_the_model_never_saw_is_withheld_in_the_child(world, client) -> None:
    store, consolidator, _embedder, here = world
    assert store.set_semantic("user.seen", "a", 0.9, source="seed") is None
    assert store.set_semantic("user.unseen", "b", 0.9, source="seed") is None
    snapshot = {k: store.get_semantic(k) for k in ("user.seen", "user.unseen")}

    offload.write_structured_memory(
        consolidator,
        {
            "semantic": [
                {"key": "user.seen", "delete": True},
                {"key": "user.unseen", "delete": True},
            ]
        },
        _KEY,
        store,
        snapshot=snapshot,
        visible_keys=frozenset({"user.seen"}),
    )

    assert here["structured"] == 0
    assert store.get_semantic("user.seen") is None
    assert store.get_semantic("user.unseen") is not None


def test_only_what_the_items_name_crosses_the_pipe(world, client, monkeypatch) -> None:
    store, consolidator, _embedder, _here = world
    seen: list[dict] = []
    real = client.run_job

    def spy(job, **kwargs):
        seen.append(job)
        return real(job, **kwargs)

    monkeypatch.setattr(client, "run_job", spy)
    snapshot = {f"user.k{i}": {"value_json": json.dumps(i), "blob": "x" * 1000} for i in range(500)}

    offload.write_structured_memory(
        consolidator,
        {"semantic": [{"key": "user.k7", "value": "new", "confidence": 0.9}]},
        _KEY,
        store,
        snapshot=snapshot,
        visible_keys=frozenset(snapshot),
    )

    (job,) = seen
    assert job["args"]["snapshot"] == {"user.k7": {"value_json": json.dumps(7)}}
    assert job["args"]["visible_keys"] == ["user.k7"]


def test_lessons_are_written_by_the_child(world, client) -> None:
    store, consolidator, embedder, here = world

    offload.save_lessons(
        consolidator,
        [{"rule": "run the repo venv mypy before pushing", "category": "tool"}],
        store,
        None,
        key=_KEY,
        commit_state=_RunCommitState(),
    )

    assert here["lessons"] == 0
    rules = [json.loads(row["value_json"])["rule"] for row in store.get_lessons()]
    assert rules == ["run the repo venv mypy before pushing"]
    assert embedder.calls


def test_a_lost_lease_is_refused_in_the_child_and_raised_here(world, client) -> None:
    store, consolidator, _embedder, here = world
    # The metadata line names no lease token, so a run holding "mine" has lost it.
    state = _RunCommitState(lease_token="mine")

    with pytest.raises(_LeaseLostMidRun):
        offload.write_structured_memory(
            consolidator,
            {"semantic": [{"key": "user.x", "value": "y", "confidence": 0.9}]},
            _KEY,
            store,
            commit_state=state,
        )

    assert here["structured"] == 0
    assert store.get_semantic("user.x") is None


def test_writes_turned_off_before_the_first_commit_refuse_the_run(
    world, client, config_file
) -> None:
    store, consolidator, _embedder, _here = world
    config_file({"memory": {"writes_enabled": False}})

    with pytest.raises(_PersistenceDisabledMidRun):
        offload.save_lessons(
            consolidator, [{"rule": "never written", "category": "tool"}], store, None, key=_KEY
        )

    assert store.get_lessons() == []


def test_a_restricted_transcript_is_withheld_in_the_child(world, client, tmp_path) -> None:
    store, consolidator, _embedder, _here = world
    consolidator._log.update_metadata(_KEY, {"memory_mode": "incognito"})

    with pytest.raises(TranscriptWithheld):
        offload.write_structured_memory(
            consolidator,
            {"semantic": [{"key": "user.x", "value": "y", "confidence": 0.9}]},
            _KEY,
            store,
        )

    assert store.get_semantic("user.x") is None


# ── The job stays in the gateway when it cannot go to the child ──


def test_switch_off_writes_in_the_gateway(world, client, monkeypatch) -> None:
    store, consolidator, _embedder, here = world
    monkeypatch.setattr(offload, "write_child_enabled", lambda: False)

    offload.write_structured_memory(
        consolidator,
        {"semantic": [{"key": "user.a", "value": "b", "confidence": 0.9}]},
        _KEY,
        store,
    )

    assert here["structured"] == 1 and client.spawns == 0
    assert store.get_semantic("user.a") is not None


def test_a_store_double_writes_in_the_gateway(client) -> None:
    consolidator = HistoryConsolidator(log=MagicMock(), memory=MagicMock(), migrated=True)
    double = MagicMock()
    double.algorithm_version = "v1"
    calls = []
    consolidator._write_structured_memory = lambda *a, **k: calls.append((a, k))

    offload.write_structured_memory(consolidator, {"semantic": []}, "k", double)

    assert len(calls) == 1 and client.spawns == 0


def test_the_switch_is_off_by_default() -> None:
    assert cwc.write_child_enabled() is False


def test_the_switch_reads_the_config(config_file) -> None:
    config_file({"memory": {"consolidation_write_child_process": True}})
    assert cwc.write_child_enabled() is True
    config_file({"memory": {"consolidation_write_child_process": "yes"}})
    assert cwc.write_child_enabled() is False


# ── Crash and hang: no write lost, the gateway not stuck ──


def test_a_child_killed_mid_job_leaves_the_write_to_the_gateway(world, client) -> None:
    store, consolidator, embedder, here = world
    embedder.on_first = lambda: os.kill(client.pid(), signal.SIGKILL)

    offload.write_structured_memory(
        consolidator,
        {
            "semantic": [{"key": "user.k", "value": "v", "confidence": 0.9}],
            "episodic": [{"text": "survived the crash", "importance": 0.5}],
        },
        _KEY,
        store,
    )

    assert here["structured"] == 1, "the gateway did not take the job over"
    assert json.loads(store.get_semantic("user.k")["value_json"]) == "v"
    assert [r["text"] for r in _episodic_rows(store)] == ["survived the crash"]
    # The failure starts the backoff: the next job goes straight to the gateway.
    spawns = client.spawns
    offload.write_structured_memory(
        consolidator,
        {"semantic": [{"key": "user.k2", "value": "v2", "confidence": 0.9}]},
        _KEY,
        store,
    )
    assert client.spawns == spawns and here["structured"] == 2
    assert store.get_semantic("user.k2") is not None


def test_stopping_the_handle_stops_the_child(world, client) -> None:
    store, consolidator, _embedder, _here = world
    offload.write_structured_memory(
        consolidator,
        {"semantic": [{"key": "user.a", "value": "b", "confidence": 0.9}]},
        _KEY,
        store,
    )
    pid = client.pid()
    assert pid is not None

    client.close()

    def gone() -> bool:
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists():
            return True
        # An exited child that is not reaped yet counts as gone.
        return stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"

    wait_until(gone, timeout=30, describe=lambda: f"child {pid} still running")


def test_a_kept_store_follows_the_config_and_the_gateways_writes(
    tmp_path, config_file, monkeypatch
) -> None:
    import kiro_crew.vector_memory as vm

    db = tmp_path / "memory.db"
    seed = VectorMemoryStore(db_path=db, embedding_dim=_DIM)
    seed.init()
    seed.close()
    state = cwc._ChildState(MagicMock())
    try:
        first = state.store_for(str(db), _DIM)
        cap = first._episodic_max
        config_file({"memory": {"episodic_max_count": cap + 7}})
        rebuilt: list[int] = []
        monkeypatch.setattr(vm, "_HAS_FAISS", True)
        monkeypatch.setattr(vm, "_HAS_NUMPY", True)
        first._faiss_index = object()
        first._faiss_data_version = 1
        monkeypatch.setattr(first, "_sqlite_data_version", lambda: 2)
        monkeypatch.setattr(first, "build_faiss_index", lambda: rebuilt.append(1) or 0)

        second = state.store_for(str(db), _DIM)

        assert second is first
        assert second._episodic_max == cap + 7, "the kept store ignored a config change"
        assert rebuilt == [1], "the FAISS view was not rebuilt after another process wrote"
        # Unchanged since: no second rebuild.
        first._faiss_data_version = 2
        state.store_for(str(db), _DIM)
        assert rebuilt == [1]
    finally:
        for store in state.stores.values():
            store.close()


def test_a_kept_store_is_reopened_when_the_embedding_width_changes(tmp_path) -> None:
    db = tmp_path / "memory.db"
    seed = VectorMemoryStore(db_path=db, embedding_dim=_DIM)
    seed.init()
    seed.close()
    state = cwc._ChildState(MagicMock())
    try:
        first = state.store_for(str(db), _DIM)
        assert state.store_for(str(db), _DIM) is first

        second = state.store_for(str(db), _DIM * 2)

        assert second is not first and second._embedding_dim == _DIM * 2
        assert list(state.stores.values()) == [second]
    finally:
        for store in state.stores.values():
            store.close()


def test_a_private_member_store_never_goes_to_the_child(client, tmp_path) -> None:
    # The child's cut-down snapshot and missing `messages` are safe only because the
    # private-policy branch that reads them never runs there.
    store = MagicMock(spec=VectorMemoryStore)
    store.algorithm_version = "v2"
    store._db_path = tmp_path / "memory.db"
    store._db_path.write_bytes(b"")
    log = MagicMock()
    log._dir = tmp_path
    consolidator = HistoryConsolidator(log=log, memory=MagicMock(), migrated=True)

    assert offload._child_job_target(consolidator, store) is None


def test_a_long_run_of_failures_still_falls_back(tmp_path) -> None:
    handle = WriteChild(argv=[str(tmp_path / "no-such-python")])
    handle._failures = 5000  # 2 ** 5000 overflows a float
    try:
        with pytest.raises(ChildUnavailable):
            _run(handle)
        assert handle._down_until - time.monotonic() <= cwc._BACKOFF_CAP_SECS
    finally:
        handle.close()


# ── Stub child: the failure paths ──


def _stub(tmp_path: Path, body: str) -> list[str]:
    """argv of a stand-in child that reports ready, then runs *body* on the first job."""
    script = tmp_path / "stub_child.py"
    script.write_text(textwrap.dedent("""
            import json, os, sys, time
            out = os.fdopen(os.dup(1), "wb")
            def send(msg):
                out.write(json.dumps(msg).encode() + b"\\n"); out.flush()
            def recv():
                raw = sys.stdin.buffer.readline()
                return json.loads(raw) if raw else None
            send({"op": "ready", "pid": os.getpid()})
            job = recv()
            """) + textwrap.dedent(body))
    return [sys.executable, "-P", str(script)]


def _job() -> dict:
    return {"kind": "structured", "key": _KEY, "args": {}}


def _run(handle: WriteChild, **kwargs) -> dict:
    return handle.run_job(_job(), serve_embed=lambda f: {"vector": None}, on_committed=lambda: None)


def test_a_result_frame_is_returned(tmp_path) -> None:
    handle = WriteChild(argv=_stub(tmp_path, 'send({"op": "result", "id": job["id"], "ok": True})'))
    try:
        assert _run(handle)["ok"] is True
    finally:
        handle.close()


def test_a_hung_child_is_killed_at_the_deadline(tmp_path) -> None:
    handle = WriteChild(job_timeout_secs=1.0, argv=_stub(tmp_path, "time.sleep(600)"))
    try:
        with pytest.raises(ChildUnavailable, match="exceeded"):
            _run(handle)
        pid = handle.pid()
        assert pid is None, "a hung child must be dropped, not reused"
    finally:
        handle.close()


def test_a_child_that_exits_mid_job_is_unavailable(tmp_path) -> None:
    handle = WriteChild(argv=_stub(tmp_path, "os._exit(3)"))
    try:
        with pytest.raises(ChildUnavailable, match="exited"):
            _run(handle)
    finally:
        handle.close()


def test_a_failed_child_is_not_retried_during_the_backoff(tmp_path) -> None:
    handle = WriteChild(argv=_stub(tmp_path, "os._exit(3)"))
    try:
        with pytest.raises(ChildUnavailable):
            _run(handle)
        spawns = handle.spawns
        with pytest.raises(ChildUnavailable, match="backing off"):
            _run(handle)
        assert handle.spawns == spawns
    finally:
        handle.close()


def test_a_child_that_never_reports_ready_is_unavailable(tmp_path) -> None:
    script = tmp_path / "mute.py"
    script.write_text("import time\ntime.sleep(600)\n")
    handle = WriteChild(ready_timeout_secs=1.0, argv=[sys.executable, "-P", str(script)])
    try:
        with pytest.raises(ChildUnavailable, match="ready"):
            _run(handle)
    finally:
        handle.close()


def test_a_missing_interpreter_is_unavailable(tmp_path) -> None:
    handle = WriteChild(argv=[str(tmp_path / "no-such-python")])
    try:
        with pytest.raises(ChildUnavailable):
            _run(handle)
    finally:
        handle.close()


def test_a_busy_child_makes_the_next_caller_give_up(tmp_path) -> None:
    handle = WriteChild(queue_wait_secs=0.2, argv=_stub(tmp_path, "time.sleep(600)"))
    try:
        assert handle._job_lock.acquire(timeout=1)
        try:
            with pytest.raises(ChildUnavailable, match="busy"):
                _run(handle)
        finally:
            handle._job_lock.release()
    finally:
        handle.close()


def test_an_idle_child_is_stopped(tmp_path) -> None:
    handle = WriteChild(
        idle_stop_secs=0.2,
        argv=_stub(
            tmp_path, 'send({"op": "result", "id": job["id"], "ok": True})\ntime.sleep(600)'
        ),
    )
    try:
        assert _run(handle)["ok"] is True
        wait_until(lambda: handle.pid() is None, timeout=30, describe=lambda: "idle child kept")
    finally:
        handle.close()


def test_fence_and_error_frames_map_to_the_in_process_outcomes(tmp_path, monkeypatch) -> None:
    store = MagicMock(spec=VectorMemoryStore)
    store.algorithm_version = "v1"
    store._db_path = tmp_path / "memory.db"
    store._db_path.write_bytes(b"")
    store.embed_fn = None
    store.space_generation = 0
    store._embedding_dim = _DIM
    log = MagicMock()
    log._dir = tmp_path
    consolidator = HistoryConsolidator(log=log, memory=MagicMock(), migrated=True)
    monkeypatch.setattr(offload, "write_child_enabled", lambda: True)

    def reply(frame: str):
        handle = WriteChild(
            argv=_stub(tmp_path, f'send({{"op": "result", "id": job["id"], {frame}}})')
        )
        monkeypatch.setattr(offload, "get_client", lambda: handle)
        return handle

    gateway_runs = []
    consolidator._write_structured_memory = lambda *a, **k: gateway_runs.append(1)
    outcomes = {
        '"ok": False, "kind": "lease_lost"': _LeaseLostMidRun,
        '"ok": False, "kind": "persistence_disabled"': _PersistenceDisabledMidRun,
        '"ok": False, "kind": "withheld", "message": "m"': TranscriptWithheld,
    }
    for frame, exc in outcomes.items():
        handle = reply(frame)
        try:
            with pytest.raises(exc):
                offload.write_structured_memory(consolidator, {"semantic": []}, _KEY, store)
        finally:
            handle.close()
    assert gateway_runs == []

    # An error the child cannot classify is rerun in the gateway.
    handle = reply('"ok": False, "kind": "error", "message": "ValueError: boom"')
    try:
        offload.write_structured_memory(consolidator, {"semantic": []}, _KEY, store)
    finally:
        handle.close()
    assert gateway_runs == [1]
