"""The write child's serve loop, run in this process.

``test_consolidation_write_child.py`` spawns the real child, which coverage cannot
see. These tests drive the same ``_serve`` / ``_run_job`` code over in-memory
pipes, so each frame the child answers (and each way a job ends) is pinned here.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import kiro_crew.consolidation_write_child as cwc
import kiro_crew.consolidation_write_offload as offload
from kiro_crew.consolidation_write_child import WriteChild
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.history_consolidation_span import _RunCommitState
from kiro_crew.memory_schema import MemoryFacets
from kiro_crew.vector_memory import VectorMemoryStore

_DIM = 4
_KEY = "dashboard:chat-inproc"


def _vector(text: str) -> list[float]:
    rng = random.Random(int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big"))
    return [rng.uniform(-1.0, 1.0) for _ in range(_DIM)]


class _Gateway:
    """The gateway end of the pipes: scripted jobs in, the child's frames out.

    ``readline`` answers what the child is waiting for. While the child's last frame
    is an ``embed`` request it gets a vector back, once it has sent a ``result`` it
    gets the next scripted job (or ``exit`` when none is left).
    """

    def __init__(self, jobs: list[dict[str, Any]], *, embed_vectors: bool = True) -> None:
        self.jobs = [dict(job, op="job", id=n) for n, job in enumerate(jobs, start=1)]
        self.out = io.BytesIO()
        self.embed_vectors = embed_vectors
        self.started = False

    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def readline(self) -> bytes:
        frames = self.frames()
        last = frames[-1] if frames else {}
        if not self.started:
            self.started = True
            return self._line(self.jobs.pop(0))
        if last.get("op") == "embed":
            if not self.embed_vectors:
                return self._line({"op": "embed_result", "vector": None, "generation": 0})
            return self._line(
                {
                    "op": "embed_result",
                    "vector": _vector(last["text"]),
                    "token": [None, ""],
                    "managed": False,
                    "generation": 0,
                }
            )
        if self.jobs:
            return self._line(self.jobs.pop(0))
        return self._line({"op": "exit"})

    @staticmethod
    def _line(frame: dict[str, Any]) -> bytes:
        return json.dumps(frame).encode() + b"\n"


@dataclass
class _World:
    store: VectorMemoryStore
    db: Path
    sessions: Path
    log: ConversationLog

    def job(self, kind: str, args: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {
            "kind": kind,
            "key": _KEY,
            "sessions_dir": str(self.sessions),
            "db_path": str(self.db),
            "embedding_dim": _DIM,
            "embed_available": True,
            "space_generation": 0,
            "args": args,
            **extra,
        }


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
def world(tmp_path: Path):
    db = tmp_path / "memory.db"
    store = VectorMemoryStore(db_path=db, embedding_dim=_DIM)
    store.init()
    sessions = tmp_path / "sessions"
    log = ConversationLog(base_dir=sessions)
    log.append(_KEY, "user", "hello")
    yield _World(store, db, sessions, log)
    store.close()


def _serve(gateway: _Gateway) -> int:
    code = cwc._serve(gateway, gateway.out)  # type: ignore[arg-type]
    return code


def _results(gateway: _Gateway) -> list[dict[str, Any]]:
    return [f for f in gateway.frames() if f.get("op") == "result"]


# ── Serving ──


def test_a_structured_job_and_a_lesson_job_are_answered_in_turn(world) -> None:
    gateway = _Gateway(
        [
            world.job(
                "structured",
                {
                    "result": {
                        "semantic": [{"key": "user.editor", "value": "neovim", "confidence": 0.9}],
                        "episodic": [{"text": "wrote the serve tests", "importance": 0.5}],
                    },
                    "snapshot": {},
                    "visible_keys": [],
                },
                facets={"surface": "dashboard", "crew": "c", "session_key": _KEY},
            ),
            world.job("lessons", {"raw": [{"rule": "read the logs first", "category": "tool"}]}),
        ]
    )

    assert _serve(gateway) == 0

    frames = gateway.frames()
    assert frames[0]["op"] == "ready"
    first, second = _results(gateway)
    assert first["ok"] is True and first["committed"] is True and first["id"] == 1
    assert second["ok"] is True and second["id"] == 2
    assert {f["op"] for f in frames} >= {"ready", "embed", "committed", "result"}
    assert json.loads(world.store.get_semantic("user.editor")["value_json"]) == "neovim"
    assert [json.loads(r["value_json"])["rule"] for r in world.store.get_lessons()] == [
        "read the logs first"
    ]


def test_a_job_with_no_embedder_writes_without_asking_for_vectors(world) -> None:
    gateway = _Gateway(
        [
            world.job(
                "structured",
                {"result": {"semantic": [{"key": "user.a", "value": "b", "confidence": 0.9}]}},
                embed_available=False,
            )
        ]
    )

    _serve(gateway)

    assert [f for f in gateway.frames() if f["op"] == "embed"] == []
    assert _results(gateway)[0]["ok"] is True


def test_an_embed_the_gateway_could_not_answer_defers_the_embedding(world) -> None:
    gateway = _Gateway(
        [
            world.job(
                "structured",
                {"result": {"episodic": [{"text": "no vector today", "importance": 0.5}]}},
            )
        ],
        embed_vectors=False,
    )

    _serve(gateway)

    assert _results(gateway)[0]["ok"] is True
    with world.store._db_lock:
        row = world.store.db.execute(
            "SELECT embedding IS NOT NULL AS embedded FROM episodic_memories"
        ).fetchone()
    assert row["embedded"] == 0


def test_the_gateway_going_away_mid_job_ends_the_child(world) -> None:
    class _Gone(_Gateway):
        def readline(self) -> bytes:
            if not self.started:
                return super().readline()
            return b""  # stdin closed while the child waits for a vector

    gateway = _Gone(
        [
            world.job(
                "structured",
                {"result": {"episodic": [{"text": "gateway left", "importance": 0.5}]}},
            )
        ]
    )

    with pytest.raises(SystemExit):
        _serve(gateway)


def test_serve_ignores_other_ops_and_stops_on_eof_or_a_bad_frame() -> None:
    class _Script:
        def __init__(self, lines: list[bytes]) -> None:
            self.lines = lines

        def readline(self) -> bytes:
            return self.lines.pop(0) if self.lines else b""

    out = io.BytesIO()
    assert cwc._serve(_Script([b'{"op": "noop"}\n', b""]), out) == 0  # type: ignore[arg-type]
    assert json.loads(out.getvalue().splitlines()[0])["op"] == "ready"
    assert cwc._serve(_Script([b"not json\n"]), io.BytesIO()) == 1  # type: ignore[arg-type]
    assert cwc._serve(_Script([b'{"op": "exit"}\n']), io.BytesIO()) == 0  # type: ignore[arg-type]


def test_a_job_whose_store_cannot_open_is_an_error_frame(world, tmp_path) -> None:
    job = world.job("structured", {"result": {}})
    job["db_path"] = str(tmp_path / "missing" / "memory.db")
    # A directory in the way: the store cannot create its database file.
    (tmp_path / "missing").write_text("not a directory")
    gateway = _Gateway([job])

    assert _serve(gateway) == 0

    (result,) = _results(gateway)
    assert result["ok"] is False and result["kind"] == "error"


def test_an_unknown_job_kind_is_an_error_frame(world) -> None:
    gateway = _Gateway([world.job("nonsense", {})])

    _serve(gateway)

    (result,) = _results(gateway)
    assert result["ok"] is False and "unknown job kind" in result["message"]


# ── How a job ends ──


def test_the_publication_fences_come_back_as_named_outcomes(world, config_file) -> None:
    items = {"result": {"semantic": [{"key": "user.x", "value": "y", "confidence": 0.9}]}}
    # Lease: the metadata line names no token, so "mine" has been lost.
    lease = world.job("structured", items, lease_token="mine")
    gateway = _Gateway([lease])
    _serve(gateway)
    assert _results(gateway)[0]["kind"] == "lease_lost"

    # Restricted transcript.
    world.log.update_metadata(_KEY, {"memory_mode": "incognito"})
    gateway = _Gateway([world.job("structured", items)])
    _serve(gateway)
    assert _results(gateway)[0]["kind"] == "withheld"
    world.log.update_metadata(_KEY, {"memory_mode": "normal"})

    # Writes switched off before the run's first commit.
    config_file({"memory": {"writes_enabled": False}})
    gateway = _Gateway([world.job("structured", items)])
    _serve(gateway)
    assert _results(gateway)[0]["kind"] == "persistence_disabled"
    assert world.store.get_semantic("user.x") is None


def test_a_busy_transcript_lock_is_reported_as_busy(world, monkeypatch) -> None:
    from kiro_crew.history_derivation import TranscriptBusy

    def busy(self, key, **kwargs):
        raise TranscriptBusy("lock not taken in time")

    monkeypatch.setattr(ConversationLog, "publication_hold", busy)
    gateway = _Gateway(
        [world.job("structured", {"result": {"semantic": [{"key": "user.x", "value": "y"}]}})]
    )

    _serve(gateway)

    (result,) = _results(gateway)
    assert result["kind"] == "busy" and "lock" in result["message"]


def test_a_store_error_is_relayed_for_the_gateway_to_rerun(world, monkeypatch) -> None:
    def boom(self, *args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(HistoryConsolidator, "_save_lessons", boom)
    gateway = _Gateway([world.job("lessons", {"raw": [{"rule": "r", "category": "tool"}]})])

    _serve(gateway)

    (result,) = _results(gateway)
    assert result == {
        "op": "result",
        "id": 1,
        "ok": False,
        "kind": "error",
        "message": "ValueError: boom",
        "committed": False,
    }


def test_the_committed_latch_is_only_announced_once(world) -> None:
    gateway = _Gateway(
        [
            world.job(
                "structured",
                {
                    "result": {
                        "semantic": [
                            {"key": "user.a", "value": "1", "confidence": 0.9},
                            {"key": "user.b", "value": "2", "confidence": 0.9},
                        ]
                    }
                },
            ),
            world.job(
                "structured",
                {"result": {"semantic": [{"key": "user.c", "value": "3", "confidence": 0.9}]}},
                committed=True,
            ),
        ]
    )

    _serve(gateway)

    announced = [f for f in gateway.frames() if f["op"] == "committed"]
    assert len(announced) == 1, "a latch the gateway already holds is not announced again"


# ── Helpers ──


def test_facets_survive_the_trip_and_other_values_do_not() -> None:
    facets = MemoryFacets(surface="dashboard", crew="c", session_key=_KEY)
    assert MemoryFacets(**cwc.facets_to_json(facets)) == facets  # type: ignore[arg-type]
    assert cwc.facets_to_json(None) is None
    assert cwc.facets_to_json(MemoryFacets) is None
    assert cwc.facets_to_json("not facets") is None


def test_the_unreachable_embedder_marker_refuses_to_embed() -> None:
    with pytest.raises(RuntimeError):
        cwc._unreachable_embed("text")


def test_the_parent_watch_exits_when_the_gateway_is_gone(monkeypatch) -> None:
    exited: list[int] = []

    def fake_exit(code: int) -> None:
        exited.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(cwc, "_parent_gone", lambda parent: True)
    monkeypatch.setattr(cwc, "_PARENT_POLL_SECS", 0)
    monkeypatch.setattr(cwc.os, "_exit", fake_exit)

    with pytest.raises(SystemExit):
        cwc._watch_parent(1)

    assert exited == [0]


def test_a_kept_store_is_closed_when_the_cap_is_passed(tmp_path) -> None:
    state = cwc._ChildState(MagicMock())
    opened = []
    try:
        for n in range(cwc._CHILD_STORE_CAP + 2):
            db = tmp_path / f"m{n}.db"
            opened.append(state.store_for(str(db), _DIM))
        assert len(state.stores) == cwc._CHILD_STORE_CAP
        assert opened[0] not in state.stores.values()
    finally:
        for store in state.stores.values():
            store.close()


def test_a_transcript_log_is_kept_per_directory(tmp_path) -> None:
    state = cwc._ChildState(MagicMock())
    first = state.log_for(str(tmp_path / "a"))
    assert state.log_for(str(tmp_path / "a")) is first
    assert state.log_for(str(tmp_path / "b")) is not first


# ── The gateway's handle ──


def test_the_process_wide_handle_is_shared_and_can_be_stopped(monkeypatch) -> None:
    monkeypatch.setattr(cwc, "_client", None)
    monkeypatch.setattr(cwc.atexit, "register", lambda fn: None)

    first = cwc.get_client()
    assert cwc.get_client() is first
    cwc.shutdown_client()
    assert cwc._client is None
    cwc.shutdown_client()  # nothing to stop


def test_an_unreadable_config_keeps_the_switch_off(monkeypatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    def broken():
        raise RuntimeError("unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(broken))
    assert cwc.write_child_enabled() is False


def test_a_closed_handle_refuses_jobs() -> None:
    handle = WriteChild()
    handle.close()
    handle.close()
    with pytest.raises(cwc.ChildUnavailable, match="closed"):
        handle.run_job({}, serve_embed=lambda f: {}, on_committed=lambda: None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX child scripts")
def test_a_child_that_writes_garbage_is_unavailable(tmp_path, monkeypatch) -> None:
    script = tmp_path / "garbage.py"
    script.write_text(
        "import json, os, sys\n"
        "out = os.fdopen(os.dup(1), 'wb')\n"
        "out.write(json.dumps({'op': 'ready'}).encode() + b'\\n'); out.flush()\n"
        "sys.stdin.buffer.readline()\n"
        "out.write(b'this is not json\\n'); out.flush()\n"
        "sys.stdin.buffer.readline()\n"
    )
    handle = WriteChild(argv=[sys.executable, "-P", str(script)])
    try:
        with pytest.raises(cwc.ChildUnavailable):
            handle.run_job({"kind": "x"}, serve_embed=lambda f: {}, on_committed=lambda: None)
    finally:
        handle.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX child scripts")
def test_an_oversized_line_stops_the_child(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cwc, "_MAX_RESPONSE_BYTES", 64)
    script = tmp_path / "loud.py"
    script.write_text(
        "import json, os, sys\n"
        "out = os.fdopen(os.dup(1), 'wb')\n"
        "out.write(json.dumps({'op': 'ready'}).encode() + b'\\n'); out.flush()\n"
        "sys.stdin.buffer.readline()\n"
        "out.write(b'x' * 500 + b'\\n'); out.flush()\n"
        "sys.stdin.buffer.readline()\n"
    )
    handle = WriteChild(argv=[sys.executable, "-P", str(script)])
    try:
        with pytest.raises(cwc.ChildUnavailable):
            handle.run_job({"kind": "x"}, serve_embed=lambda f: {}, on_committed=lambda: None)
    finally:
        handle.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX child scripts")
def test_embed_requests_and_commits_are_served_while_a_job_runs(tmp_path) -> None:
    script = tmp_path / "chatty.py"
    script.write_text(
        "import json, os, sys\n"
        "out = os.fdopen(os.dup(1), 'wb')\n"
        "def send(m):\n"
        "    out.write(json.dumps(m).encode() + b'\\n'); out.flush()\n"
        "send({'op': 'ready'})\n"
        "job = json.loads(sys.stdin.buffer.readline())\n"
        "send({'op': 'committed'})\n"
        "send({'op': 'embed', 'text': 'hello', 'priority': 1})\n"
        "reply = json.loads(sys.stdin.buffer.readline())\n"
        "send({'op': 'result', 'id': job['id'], 'ok': True, 'seen': reply['vector']})\n"
        "sys.stdin.buffer.readline()\n"
    )
    handle = WriteChild(argv=[sys.executable, "-P", str(script)])
    commits: list[int] = []
    asked: list[str] = []

    def serve(frame: dict[str, Any]) -> dict[str, Any]:
        asked.append(frame["text"])
        return {"vector": [1.0, 2.0]}

    try:
        frame = handle.run_job(
            {"kind": "x"}, serve_embed=serve, on_committed=lambda: commits.append(1)
        )
    finally:
        handle.close()

    assert frame["seen"] == [1.0, 2.0] and asked == ["hello"] and commits == [1]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX child scripts")
def test_an_embed_that_raises_is_answered_with_no_vector(tmp_path) -> None:
    script = tmp_path / "ask.py"
    script.write_text(
        "import json, os, sys\n"
        "out = os.fdopen(os.dup(1), 'wb')\n"
        "def send(m):\n"
        "    out.write(json.dumps(m).encode() + b'\\n'); out.flush()\n"
        "send({'op': 'ready'})\n"
        "job = json.loads(sys.stdin.buffer.readline())\n"
        "send({'op': 'embed', 'text': 'x'})\n"
        "reply = json.loads(sys.stdin.buffer.readline())\n"
        "send({'op': 'result', 'id': job['id'], 'ok': True, 'vector': reply['vector']})\n"
        "sys.stdin.buffer.readline()\n"
    )
    handle = WriteChild(argv=[sys.executable, "-P", str(script)])

    def serve(frame: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("embedder down")

    try:
        frame = handle.run_job({"kind": "x"}, serve_embed=serve, on_committed=lambda: None)
    finally:
        handle.close()

    assert frame["vector"] is None


# ── The wrappers ──


@pytest.fixture
def client(monkeypatch):
    handle = WriteChild()
    monkeypatch.setattr(offload, "get_client", lambda: handle)
    monkeypatch.setattr(offload, "write_child_enabled", lambda: True)
    yield handle
    handle.close()


def _consolidator(world) -> HistoryConsolidator:
    return HistoryConsolidator(
        log=world.log, memory=MagicMock(), vector_store=world.store, migrated=True
    )


def test_a_job_that_is_not_plain_data_is_written_in_the_gateway(world, client) -> None:
    consolidator = _consolidator(world)
    ran: list[int] = []
    consolidator._write_structured_memory = lambda *a, **k: ran.append(1)

    offload.write_structured_memory(
        consolidator, {"semantic": [{"key": "user.a", "value": object()}]}, _KEY, world.store
    )

    assert ran == [1] and client.spawns == 0


def test_an_unsent_global_store_falls_back_to_the_consolidators_own(world, client) -> None:
    consolidator = _consolidator(world)
    ran: list[Any] = []
    consolidator._save_lessons = lambda raw, store, *a, **k: ran.append(store)
    facade = offload._facade()

    offload.save_lessons(consolidator, "not a list", facade._INHERIT_GLOBAL, None, key=_KEY)

    assert ran == [world.store]


def test_no_database_file_means_no_child(world, client, tmp_path) -> None:
    # A real store whose file is gone (an open file cannot be unlinked on Windows).
    gone = MagicMock(spec=VectorMemoryStore)
    gone.algorithm_version = "v1"
    gone._db_path = tmp_path / "gone.db"

    assert offload._child_job_target(_consolidator(world), gone) is None


def test_the_embed_service_returns_the_vector_with_its_space_token(world) -> None:
    world.store.embed_fn = _vector
    serve = offload._serve_embed(world.store)

    reply = serve({"text": "hello", "priority": 2})
    nothing = serve({"text": "hello", "priority": "bad"})

    assert reply["vector"] == pytest.approx(_vector("hello"), abs=1e-5)
    assert reply["managed"] is False and "token" in reply
    assert nothing["vector"] is not None
    world.store.embed_fn = None
    assert serve({"text": "x"})["vector"] is None


def test_a_busy_frame_is_raised_as_the_transcript_busy_error(world, client, monkeypatch) -> None:
    from kiro_crew.history_derivation import TranscriptBusy

    monkeypatch.setattr(
        client,
        "run_job",
        lambda job, **kw: {"op": "result", "ok": False, "kind": "busy", "message": "m"},
    )
    with pytest.raises(TranscriptBusy):
        offload.write_structured_memory(
            _consolidator(world),
            {"semantic": []},
            _KEY,
            world.store,
            commit_state=_RunCommitState(),
        )


def test_a_committed_flag_on_the_result_reaches_the_run_state(world, client, monkeypatch) -> None:
    monkeypatch.setattr(
        client, "run_job", lambda job, **kw: {"op": "result", "ok": True, "committed": True}
    )
    state = _RunCommitState()

    offload.save_lessons(
        _consolidator(world), [{"rule": "r"}], world.store, None, key=_KEY, commit_state=state
    )

    assert state.committed is True
