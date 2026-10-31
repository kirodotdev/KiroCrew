"""Turn-context recall answered by child processes (``memory.recall_pool_processes``).

The end-to-end tests spawn the real child interpreter against a real ``memory.db``
and compare what it renders with what the same store renders in this process. The
stub-child tests use a tiny script in place of the child to drive the failure paths
(crash, hang, error frame) deterministically.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import sys
import textwrap
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew.recall_pool as rp
from kiro_crew import platform_compat
from kiro_crew.consolidation_write_child import ChildUnavailable
from kiro_crew.recall_pool import RecallPool
from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_runtime import episodic_search as episodic_mod
from kiro_crew.vector_memory_runtime import lessons as lessons_mod
from kiro_crew.vector_memory_runtime import semantic as semantic_mod

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and /proc")

_DIM = 8
#: A fixed free-memory reading, so a child starts whatever the host has free.
_ROOMY = lambda: 1e9  # noqa: E731


def _vector(text: str) -> list[float]:
    rng = random.Random(int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big"))
    return [rng.uniform(-1.0, 1.0) for _ in range(_DIM)]


class _Embedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        return _vector(text)


@pytest.fixture(autouse=True)
def _fresh_settings(_floor_monkeypatch: pytest.MonkeyPatch):
    """Each test reads the config afresh, not the previous test's cached answer."""
    _floor_monkeypatch.setattr(rp, "_settings_cache", None)


FACTS = {
    "project.deploy.target": "kubernetes cluster rollout for the payments service",
    "project.build.tool": "gradle build with a remote cache",
    "user.editor": "prefers vim keybindings",
}
EPISODE = "we discussed rolling back the kubernetes rollout after the payments outage"
LESSON = "always run the integration suite before a rollout"
QUERY = "kubernetes rollout payments"


@pytest.fixture
def store(tmp_path: Path):
    s = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=_DIM)
    s.init()
    s.embed_fn = _Embedder()
    for key, value in FACTS.items():
        s.set_semantic(key, value, 0.9, "test")
    s.write_episodic(EPISODE, _vector(EPISODE), "conv-1")
    s.write_lesson(LESSON, "knowledge", source="user_explicit")
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch):
    """A real pool the offload hook uses, with the switch on."""
    handle = RecallPool(2, 3072, job_timeout_secs=60.0, queue_wait_secs=30.0, free_memory_mb=_ROOMY)
    monkeypatch.setattr(rp, "settings", lambda: (2, 3072))
    monkeypatch.setattr(rp, "get_pool", lambda size, max_mb: handle)
    yield handle
    handle.close()


def _lessons_kwargs(**over):
    kw = dict(
        query_text=QUERY,
        cap=7000,
        background=True,
        hard_cap=40000,
        directive_budget=30000,
        experience_budget=1500,
    )
    kw.update(over)
    return kw


def _forbid_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the in-process readers fail loudly: the answer must come from a child."""

    def boom(*_a, **_k):
        raise AssertionError("read ran in this process")

    monkeypatch.setattr(semantic_mod, "get_semantic_context", boom)
    monkeypatch.setattr(episodic_mod, "get_episodic_context", boom)
    monkeypatch.setattr(lessons_mod, "get_lessons_context", boom)


class TestSwitch:
    def test_off_by_default(self) -> None:
        assert rp.settings() == (0, 0)
        assert rp.pool_enabled() is False

    def test_off_never_builds_a_pool(self, store, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a):
            raise AssertionError("pool built while the switch is off")

        monkeypatch.setattr(rp, "get_pool", boom)
        assert "kubernetes" in store.get_semantic_context(QUERY, 1500)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (3, (3, 3072)),
            (99, (8, 3072)),
            (0, (0, 0)),
            (-2, (0, 0)),
            (True, (0, 0)),
            ("4", (0, 0)),
        ],
    )
    def test_process_count_is_clamped(self, raw, expected, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = MagicMock()
        cfg.memory.recall_pool_processes = raw
        cfg.memory.recall_pool_worker_max_mb = 3072
        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: cfg)
        assert rp.settings() == expected

    def test_a_reading_is_reused_briefly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loads = []
        cfg = MagicMock()
        cfg.memory.recall_pool_processes = 2
        cfg.memory.recall_pool_worker_max_mb = 4096
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", lambda: loads.append(1) or cfg
        )
        clock = [50.0]
        monkeypatch.setattr(rp, "_now", lambda: clock[0])
        assert rp.settings() == (2, 4096)
        clock[0] += rp._SETTINGS_TTL_SECS / 2
        assert rp.settings() == (2, 4096)
        assert len(loads) == 1
        clock[0] += rp._SETTINGS_TTL_SECS
        rp.settings()
        assert len(loads) == 2

    def test_config_defaults_and_bounds(self, tmp_path: Path) -> None:
        from kiro_crew.config.section_builders import _build_memory_config

        default = _build_memory_config({})
        assert default.recall_pool_processes == 0
        assert default.recall_pool_worker_max_mb == 3072
        clamped = _build_memory_config(
            {"recall_pool_processes": 50, "recall_pool_worker_max_mb": 1}
        )
        assert clamped.recall_pool_processes == 8
        assert clamped.recall_pool_worker_max_mb == 256


class TestEndToEnd:
    def test_all_three_reads_match_the_gateway(
        self, store, pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected_sem = semantic_mod.get_semantic_context(store, QUERY, 1500, facts_only=True)
        expected_epi = episodic_mod.get_episodic_context(store, _vector(EPISODE), EPISODE, 3000)
        expected_les = lessons_mod.get_lessons_context(store, **_lessons_kwargs())
        assert "kubernetes" in expected_sem
        assert EPISODE[:20] in expected_epi
        assert LESSON in expected_les

        _forbid_in_process(monkeypatch)
        assert store.get_semantic_context(QUERY, 1500, facts_only=True) == expected_sem
        assert store.get_episodic_context(_vector(EPISODE), EPISODE, 3000) == expected_epi
        assert store.get_lessons_context(**_lessons_kwargs()) == expected_les
        assert pool.spawns >= 1
        assert pool.pids()

    def test_a_committed_write_is_seen_by_the_next_read(
        self, store, pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _forbid_in_process(monkeypatch)
        before = store.get_semantic_context("terraform drift", 1500, facts_only=True)
        assert "terraform" not in before
        store.set_semantic("project.infra.iac", "terraform drift detection nightly", 0.9, "test")
        after = store.get_semantic_context("terraform drift", 1500, facts_only=True)
        assert "terraform drift detection nightly" in after

    def test_the_query_is_embedded_by_the_gateway(
        self, store, pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _forbid_in_process(monkeypatch)
        embedder = store.embed_fn
        before = len(embedder.calls)
        store.get_semantic_context(QUERY, 1500, facts_only=True)
        assert QUERY in embedder.calls[before:]

    def test_the_gateway_records_the_access_later(
        self, store, pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rp, "_pending_touches", {})
        monkeypatch.setattr(rp, "_touch_timer", None)
        _forbid_in_process(monkeypatch)
        try:
            row = store._fetch_one_locked("SELECT id, last_accessed_at FROM episodic_memories")
            assert row["last_accessed_at"] is None
            assert EPISODE[:20] in store.get_episodic_context(_vector(EPISODE), EPISODE, 3000)
            # Not committed per read: that would move data_version for every child.
            row = store._fetch_one_locked("SELECT id, last_accessed_at FROM episodic_memories")
            assert row["last_accessed_at"] is None
            rp.flush_touches()
            row = store._fetch_one_locked("SELECT id, last_accessed_at FROM episodic_memories")
            assert row["last_accessed_at"] is not None
            assert row["id"] in store._last_accessed_touch
        finally:
            rp.flush_touches()

    @staticmethod
    def _collected(read):
        """What a turn's two collectors hold after *read*, from a seeded omission record."""
        from kiro_crew import context_omissions, injection_receipts

        with context_omissions.collect() as omissions, injection_receipts.collect() as receipts:
            # A record an earlier render of the same tier left behind in this build.
            context_omissions.record(
                context_omissions.LESSON_RULES, "budget", omitted=9, total=9, budget_chars=1
            )
            text = read()
        return text, dict(omissions), dict(receipts)

    @pytest.mark.parametrize(
        "budgets",
        [
            dict(hard_cap=700, directive_budget=300, experience_budget=100),
            dict(hard_cap=40000, directive_budget=30000, experience_budget=1500),
        ],
        ids=["tiers cut for budget", "everything kept"],
    )
    def test_lesson_records_reach_the_builds_collectors(
        self, store, pool, monkeypatch: pytest.MonkeyPatch, budgets
    ) -> None:
        for i in range(25):
            store.write_lesson(f"rule number {i} about rollout " + "word " * 30, "knowledge")
        kw = _lessons_kwargs(**budgets)
        expected = self._collected(lambda: lessons_mod.get_lessons_context(store, **kw))
        _forbid_in_process(monkeypatch)
        got = self._collected(lambda: store.get_lessons_context(**kw))
        assert got == expected
        # The receipt names the rows shown, and the seeded omission is dropped or replaced
        # exactly as the in-process render does.
        assert expected[2]
        if budgets["hard_cap"] < 1000:
            assert expected[1]["lesson_rules"]["reason"] == "budget"
        else:
            assert "lesson_rules" not in expected[1]

    def test_a_read_outside_a_collection_records_nothing(
        self, store, pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _forbid_in_process(monkeypatch)
        assert LESSON in store.get_lessons_context(**_lessons_kwargs())

    def test_a_stale_query_vector_ranks_by_keyword_like_the_gateway(
        self, store, pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.vector_memory_runtime.embedding import _RecallQuery

        stale = _RecallQuery(_vector(QUERY), store.space_generation + 7, "other-space")
        expected = lessons_mod.get_lessons_context(store, **_lessons_kwargs(recall_query=stale))
        _forbid_in_process(monkeypatch)
        assert store.get_lessons_context(**_lessons_kwargs(recall_query=stale)) == expected

    def test_concurrent_callers_share_the_children(
        self, store, pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = semantic_mod.get_semantic_context(store, QUERY, 1500, facts_only=True)
        _forbid_in_process(monkeypatch)
        out: list[str] = []
        errors: list[BaseException] = []

        def one() -> None:
            try:
                out.append(store.get_semantic_context(QUERY, 1500, facts_only=True))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=one) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not errors
        assert out == [expected] * 6
        assert len(pool.pids()) <= 2

    def test_a_child_does_not_start_children_of_its_own(
        self, store, pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``python -m`` runs the module as ``__main__``: the child must still know it is one.

        The child reads the same config as the gateway, so with the switch on in the
        file it would offload its own reads and start grandchildren, level after level.
        """
        from kiro_crew.config.loader import config_path

        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"memory": {"recall_pool_processes": 2}}))
        try:
            _forbid_in_process(monkeypatch)
            assert "kubernetes" in store.get_semantic_context(QUERY, 1500, facts_only=True)
            assert "kubernetes" in store.get_semantic_context(QUERY, 1500, facts_only=True)
            assert pool.pids()
            for pid in pool.pids():
                assert platform_compat.process_descendants(pid) == []
        finally:
            path.unlink(missing_ok=True)

    def test_a_child_over_its_memory_limit_is_replaced(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handle = RecallPool(1, 1, job_timeout_secs=60.0, free_memory_mb=lambda: 0.0)
        monkeypatch.setattr(rp, "settings", lambda: (1, 1))
        monkeypatch.setattr(rp, "get_pool", lambda size, max_mb: handle)
        try:
            expected = semantic_mod.get_semantic_context(store, QUERY, 1500, facts_only=True)
            _forbid_in_process(monkeypatch)
            for _ in range(2):
                assert store.get_semantic_context(QUERY, 1500, facts_only=True) == expected
            # Every read passed the 1 MB limit, so each one ran in a fresh child, and
            # leaving one is not a failure: no backoff.
            assert handle.spawns == 2
            assert handle._down_until == 0.0
        finally:
            handle.close()


class TestFallback:
    def _stub(self, tmp_path: Path, body: str) -> list[str]:
        script = tmp_path / "stub_child.py"
        script.write_text(textwrap.dedent(body))
        return [sys.executable, str(script)]

    def test_a_child_that_cannot_start_leaves_the_read_here(
        self, store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handle = RecallPool(
            2,
            3072,
            ready_timeout_secs=5.0,
            argv=self._stub(tmp_path, "import sys; sys.exit(3)"),
            free_memory_mb=_ROOMY,
        )
        monkeypatch.setattr(rp, "settings", lambda: (2, 3072))
        monkeypatch.setattr(rp, "get_pool", lambda size, max_mb: handle)
        clock = [1000.0]
        monkeypatch.setattr(rp, "_now", lambda: clock[0])
        try:
            assert "kubernetes" in store.get_semantic_context(QUERY, 1500, facts_only=True)
            assert handle.spawns == 0
            assert handle._down_until == clock[0] + 1.0
            # Backing off: the next read goes straight to the gateway without a spawn.
            assert "kubernetes" in store.get_semantic_context(QUERY, 1500, facts_only=True)
            assert handle._failures == 1
            # Past the backoff the pool tries again, and a second failure doubles it.
            clock[0] += 1.5
            assert "kubernetes" in store.get_semantic_context(QUERY, 1500, facts_only=True)
            assert handle._failures == 2
            assert handle._down_until == clock[0] + 2.0
        finally:
            handle.close()

    def test_a_hung_child_is_killed_and_the_read_runs_here(
        self, store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = self._stub(
            tmp_path,
            """
            import json, sys
            sys.stdout.write(json.dumps({"op": "ready"}) + "\\n"); sys.stdout.flush()
            sys.stdin.readline()
            import time; time.sleep(60)
            """,
        )
        handle = RecallPool(1, 3072, job_timeout_secs=1.0, argv=argv, free_memory_mb=_ROOMY)
        monkeypatch.setattr(rp, "settings", lambda: (1, 3072))
        monkeypatch.setattr(rp, "get_pool", lambda size, max_mb: handle)
        try:
            assert "kubernetes" in store.get_semantic_context(QUERY, 1500, facts_only=True)
            assert handle.pids() == []
            assert handle._failures == 1
        finally:
            handle.close()

    def test_a_child_that_dies_mid_read_leaves_the_read_here(
        self, store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = self._stub(
            tmp_path,
            """
            import json, sys
            sys.stdout.write(json.dumps({"op": "ready"}) + "\\n"); sys.stdout.flush()
            sys.stdin.readline()
            sys.exit(9)
            """,
        )
        handle = RecallPool(1, 3072, job_timeout_secs=20.0, argv=argv, free_memory_mb=_ROOMY)
        monkeypatch.setattr(rp, "settings", lambda: (1, 3072))
        monkeypatch.setattr(rp, "get_pool", lambda size, max_mb: handle)
        try:
            assert "kubernetes" in store.get_semantic_context(QUERY, 1500, facts_only=True)
            assert handle._failures == 1
        finally:
            handle.close()

    def test_an_error_frame_leaves_the_read_here(
        self, store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = self._stub(
            tmp_path,
            """
            import json, sys
            sys.stdout.write(json.dumps({"op": "ready"}) + "\\n"); sys.stdout.flush()
            for line in sys.stdin:
                job = json.loads(line)
                sys.stdout.write(json.dumps({"op": "result", "id": job["id"], "ok": False,
                                             "message": "boom"}) + "\\n")
                sys.stdout.flush()
            """,
        )
        handle = RecallPool(1, 3072, job_timeout_secs=20.0, argv=argv, free_memory_mb=_ROOMY)
        monkeypatch.setattr(rp, "settings", lambda: (1, 3072))
        monkeypatch.setattr(rp, "get_pool", lambda size, max_mb: handle)
        try:
            assert "kubernetes" in store.get_semantic_context(QUERY, 1500, facts_only=True)
            # An error the child reported is not a crash: no backoff.
            assert handle._failures == 0
        finally:
            handle.close()

    def test_too_little_free_memory_keeps_the_read_here(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handle = RecallPool(2, 3072, free_memory_mb=lambda: 100.0)
        monkeypatch.setattr(rp, "settings", lambda: (2, 3072))
        monkeypatch.setattr(rp, "get_pool", lambda size, max_mb: handle)
        try:
            assert "kubernetes" in store.get_semantic_context(QUERY, 1500, facts_only=True)
            assert handle.spawns == 0
            with pytest.raises(ChildUnavailable):
                handle.run_job({"kind": "semantic"}, serve_embed=lambda f: {})
        finally:
            handle.close()

    def test_a_store_a_child_cannot_open_is_read_here(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rp, "settings", lambda: (2, 3072))
        assert rp.offload(MagicMock(), "semantic", {"query_text": "x"}) is None

    def test_a_member_store_is_read_here(self, store, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rp, "settings", lambda: (2, 3072))
        monkeypatch.setattr(VectorMemoryStore, "algorithm_version", property(lambda self: "v2"))
        assert rp.offload(store, "semantic", {"query_text": QUERY}) is None

    def test_a_child_never_offloads_again(self, store, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rp, "settings", lambda: (2, 3072))
        monkeypatch.setattr(rp, "_IN_CHILD", True)
        assert rp.pool_enabled() is False
        assert rp.offload(store, "semantic", {"query_text": QUERY}) is None

    def test_the_environment_marks_a_child_too(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rp, "settings", lambda: (2, 3072))
        monkeypatch.setenv(rp._CHILD_ENV, "1")
        assert rp.in_child() is True
        assert rp.pool_enabled() is False
        assert rp.offload(store, "semantic", {"query_text": QUERY}) is None

    def test_a_closed_pool_refuses_work(self) -> None:
        handle = RecallPool(1, 3072)
        handle.close()
        with pytest.raises(ChildUnavailable):
            handle.run_job({"kind": "semantic"}, serve_embed=lambda f: {})


class TestReceiptReplay:
    def test_replay_records_known_kinds_inside_a_collection(self) -> None:
        from kiro_crew import injection_receipts

        with injection_receipts.collect() as sink:
            injection_receipts.replay(
                {
                    "lessons": {"ids": ["lesson.a", 3], "omitted": 2},
                    "findings": {"ids": "bad", "omitted": -4},
                    "nonsense": {"ids": ["x"], "omitted": 1},
                    "turn_lessons": "bad",
                }
            )
        assert sink == {
            "lessons": {"ids": ["lesson.a"], "omitted": 2},
            "findings": {"ids": [], "omitted": 0},
        }

    def test_replay_outside_a_collection_is_a_no_op(self) -> None:
        from kiro_crew import injection_receipts

        injection_receipts.replay({"lessons": {"ids": ["lesson.a"], "omitted": 0}})

    def test_malformed_records_from_a_child_never_fail_a_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rp._replay_records({"omissions": {"cleared": 7, "set": [1]}, "receipts": "x"})
        rp._replay_records(
            {"omissions": {"cleared": ["nonsense"], "set": {"x": {}, "lesson_rules": 1}}}
        )


def test_the_pool_is_rebuilt_when_the_setting_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp, "_pool", None)
    first = rp.get_pool(2, 3072)
    try:
        assert rp.get_pool(2, 3072) is first
        second = rp.get_pool(3, 3072)
        assert second is not first and second.size == 3
        assert first._closed
    finally:
        rp.shutdown_pool()


def test_job_frames_are_plain_json(store, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    class _Spy:
        def run_job(self, job, *, serve_embed):
            seen.append(job)
            json.dumps(job)
            raise ChildUnavailable("spy")

    monkeypatch.setattr(rp, "settings", lambda: (1, 3072))
    monkeypatch.setattr(rp, "get_pool", lambda size, max_mb: _Spy())
    store.get_lessons_context(**_lessons_kwargs(project_dir=Path("/tmp/proj"), recall_query=None))
    store.get_episodic_context(_vector(EPISODE), EPISODE, 3000)
    assert [j["kind"] for j in seen] == ["lessons", "episodic"]
    assert seen[0]["args"]["project_dir"] == "/tmp/proj"


class TestServeLoopInProcess:
    """The child's serve loop over in-memory pipes, so coverage sees it."""

    @staticmethod
    def _lines(*frames: dict) -> io.BytesIO:
        return io.BytesIO(b"".join(json.dumps(f).encode() + b"\n" for f in frames))

    @staticmethod
    def _frames(out: io.BytesIO) -> list[dict]:
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def _job(self, store, kind: str, args: dict, job_id: int = 1) -> dict:
        return {
            "op": "job",
            "id": job_id,
            "kind": kind,
            "db_path": str(store._db_path),
            "embedding_dim": _DIM,
            "embed_available": True,
            "space_generation": store.space_generation,
            "args": args,
        }

    def test_every_kind_answers_and_unknown_kinds_are_refused(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KIROCREW_RECALL_POOL_MAX_RSS_MB", raising=False)
        embed_reply = {"op": "embed_result", "vector": _vector(QUERY), "generation": 0}
        stdin = self._lines(
            self._job(store, "semantic", {"query_text": QUERY, "cap": 1500, "facts_only": True}, 1),
            embed_reply,
            self._job(
                store,
                "episodic",
                {"query_embedding": _vector(EPISODE), "query_text": EPISODE, "cap": 3000},
                2,
            ),
            self._job(
                store,
                "lessons",
                {
                    "query_text": QUERY,
                    "cap": 7000,
                    "recall_query": None,
                    "background": True,
                    "hard_cap": 40000,
                    "directive_budget": 30000,
                    "experience_budget": 1500,
                },
                3,
            ),
            embed_reply,
            self._job(store, "nonsense", {}, 4),
            {"op": "ignored"},
            {"op": "exit"},
        )
        out = io.BytesIO()
        assert rp._serve(stdin, out) == 0
        frames = self._frames(out)
        # The reads that embed the query ask the gateway to; nothing embeds in the child.
        embeds = [f["text"] for f in frames if f["op"] == "embed"]
        assert embeds and set(embeds) == {QUERY}
        ready, sem, epi, les, bad = [f for f in frames if f["op"] != "embed"]
        assert ready["op"] == "ready"
        assert sem["ok"] and "kubernetes" in sem["text"]
        assert epi["ok"] and EPISODE[:20] in epi["text"] and epi["touched"]
        assert les["ok"] and LESSON in les["text"]
        assert bad["ok"] is False and "nonsense" in bad["message"]
        assert not any(f.get("recycle") for f in (sem, epi, les, bad))

    def test_a_job_that_cannot_open_its_store_reports_an_error(
        self, store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KIROCREW_RECALL_POOL_MAX_RSS_MB", raising=False)
        job = self._job(store, "semantic", {"query_text": QUERY})
        job["db_path"] = "/proc/no-such-dir/memory.db"
        out = io.BytesIO()
        assert rp._serve(self._lines(job), out) == 0
        ready, reply = self._frames(out)
        assert reply["ok"] is False and reply["message"]

    def test_a_read_that_raises_is_relayed_not_fatal(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KIROCREW_RECALL_POOL_MAX_RSS_MB", raising=False)

        def boom(*_a, **_k):
            raise RuntimeError("scoring blew up")

        monkeypatch.setattr(semantic_mod, "get_semantic_context", boom)
        out = io.BytesIO()
        stdin = self._lines(self._job(store, "semantic", {"query_text": QUERY}), {"op": "exit"})
        assert rp._serve(stdin, out) == 0
        _, reply = self._frames(out)
        assert reply["ok"] is False and "scoring blew up" in reply["message"]

    def test_a_child_past_its_limit_says_so_and_stops(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_RECALL_POOL_MAX_RSS_MB", "1")
        stdin = self._lines(
            self._job(store, "lessons", {"query_text": QUERY, "background": True}, 1),
            {"op": "embed_result", "vector": _vector(QUERY), "generation": 0},
            self._job(store, "lessons", {"query_text": QUERY, "background": True}, 2),
        )
        out = io.BytesIO()
        assert rp._serve(stdin, out) == 0
        frames = self._frames(out)
        assert [f["op"] for f in frames] == ["ready", "result"]
        assert frames[1]["recycle"] is True

    def test_a_malformed_line_ends_the_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_RECALL_POOL_MAX_RSS_MB", raising=False)
        out = io.BytesIO()
        assert rp._serve(io.BytesIO(b"{not json\n"), out) == 1

    def test_the_resident_size_is_readable_here(self) -> None:
        assert rp._rss_mb() > 0
        assert rp._max_rss_mb() >= 0.0

    def test_an_unreadable_resident_size_never_recycles(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_RECALL_POOL_MAX_RSS_MB", "1")
        monkeypatch.setattr(rp, "_rss_mb", lambda: None)
        stdin = self._lines(
            self._job(store, "nonsense", {}, 1), self._job(store, "nonsense", {}, 2)
        )
        out = io.BytesIO()
        assert rp._serve(stdin, out) == 0
        results = [f for f in self._frames(out) if f["op"] == "result"]
        assert len(results) == 2 and not any(f.get("recycle") for f in results)


class TestPoolLifecycle:
    def test_a_child_spawned_while_the_pool_closes_is_not_leaked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handle = RecallPool(1, 3072, free_memory_mb=_ROOMY)
        closed: list[bool] = []

        class _Conn:
            proc = MagicMock()

            def close(self) -> None:
                closed.append(True)

        def spawn_then_close() -> _Conn:
            handle.close()  # the setting changed while this child was starting
            return _Conn()

        monkeypatch.setattr(handle, "_spawn", spawn_then_close)
        with pytest.raises(ChildUnavailable):
            handle.run_job({"kind": "semantic"}, serve_embed=lambda f: {})
        assert closed == [True]
        assert handle.pids() == []

    def test_turning_the_setting_off_stops_the_children(
        self, store, pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rp, "_pool", pool)
        assert "kubernetes" in store.get_semantic_context(QUERY, 1500, facts_only=True)
        assert pool.pids()
        monkeypatch.setattr(rp, "settings", lambda: (0, 0))
        assert rp.pool_enabled() is False
        assert pool._closed and pool.pids() == []
        assert rp._pool is None

    def test_flushing_writes_each_stores_buffered_accesses_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rp, "_pending_touches", {})
        monkeypatch.setattr(rp, "_touch_timer", None)
        first, second = MagicMock(), MagicMock()
        second._touch_last_accessed.side_effect = RuntimeError("store closed")
        try:
            rp._queue_touches(first, ["b", "a"])
            rp._queue_touches(first, ["a", "c"])
            rp._queue_touches(second, ["z"])
            assert rp._touch_timer is not None
            rp.flush_touches()
            first._touch_last_accessed.assert_called_once_with(["a", "b", "c"])
            second._touch_last_accessed.assert_called_once_with(["z"])
            assert rp._touch_timer is None and not rp._pending_touches
            rp.flush_touches()
            first._touch_last_accessed.assert_called_once()
        finally:
            rp.flush_touches()


class TestKeptStoreConfig:
    def test_an_unchanged_config_leaves_the_resident_sets_alone(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KIROCREW_RECALL_POOL_MAX_RSS_MB", raising=False)
        calls: list[int] = []
        real = VectorMemoryStore.reconfigure

        def counting(self, cfg):
            calls.append(1)
            return real(self, cfg)

        monkeypatch.setattr(VectorMemoryStore, "reconfigure", counting)
        job = TestServeLoopInProcess()._job
        lessons = {"query_text": QUERY, "background": True}
        out = io.BytesIO()
        stdin = TestServeLoopInProcess._lines(
            job(store, "lessons", lessons, 1),
            job(store, "lessons", lessons, 2),
            job(store, "lessons", lessons, 3),
        )
        assert rp._serve(stdin, out) == 0
        # Opening the store applies the config once; the two kept-store jobs apply nothing.
        assert len(calls) == 1
