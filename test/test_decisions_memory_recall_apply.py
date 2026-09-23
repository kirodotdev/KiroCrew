"""``memory.recall`` applied live: the kept set is what reaches the prompt.

The point's own suite drives the decision. This one drives the WIRING -- a real
``VectorMemoryStore`` with real rows, a real ``MemoryStore.get_context`` and a real
``ContextBuilder``, so the claim is about the block that actually gets injected
rather than about a list a helper returned.

Two directions, and both matter. With the switch on, the memories Jev kept are the
ones in the block and the dropped one is gone. With the switch off -- the shipped
default -- the block is byte-identical to the one the same tree produced before
this point existed, which is the property that makes the seam safe to place on the
prompt-assembly path.
"""

from __future__ import annotations

import asyncio
import math
import struct
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.decisions.points import memory_recall as mr
from kiro_crew.decisions.types import Answer
from kiro_crew.vector_memory import VectorMemoryStore

# Stored vectors are unit vectors, so their dot product with this is exactly their
# cosine similarity (the search path normalises the query).
_Q = [1.0, 0.0, 0.0, 0.0]

#: The question every recall in this suite asks.
QUERY = "where do we deploy the signer"


def _unit_at_cosine(cos: float) -> list[float]:
    return [cos, math.sqrt(max(0.0, 1.0 - cos * cos)), 0.0, 0.0]


def _store(tmp_path: Path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "mem.db")
    store.init()
    return store


def _insert(store: VectorMemoryStore, mem_id: str, text: str, cosine: float = 0.95) -> None:
    vec = _unit_at_cosine(cosine)
    ts = (datetime.now(timezone.utc) - timedelta(days=0)).isoformat()
    store.db.execute(
        "INSERT INTO episodic_memories "
        "(id, conversation_id, text, tags, embedding, importance, "
        "created_at, last_accessed_at, is_deleted) "
        "VALUES (?, '', ?, '[]', ?, 0.5, ?, ?, 0)",
        (mem_id, text, struct.pack(f"{len(vec)}f", *vec), ts, ts),
    )
    store.db.commit()


#: Marks "the caller passed no hook", which ``keep=None`` does not.
_RECALL_DEFAULT = object()


def _recalled(store, *, cap: int = 100_000, keep=_RECALL_DEFAULT) -> str:
    """The episode text one recall returns, as one string.

    The suites below were written against the prompt-assembly block and assert on the
    TEXT that reached the model. The recall path returns evidence rows instead, so this
    renders them the same way for the same assertions -- one place, so a change of shape
    does not have to be chased through every test.

    A sentinel default rather than ``None``: ``keep=None`` is a meaningful value on this
    seam (no hook), distinct from "the caller did not mention a hook".
    """
    kwargs = {} if keep is _RECALL_DEFAULT else {"keep": keep}
    result = store.recall(QUERY, cap=cap, **kwargs)
    rows = (result.get("retrieval") or {}).get("episodes") or []
    return "\n".join(str(row.get("text", "")) for row in rows)


@pytest.fixture
def seeded(tmp_path):
    """Three relevant episodes, each recognisable in the injected block."""
    store = _store(tmp_path)
    _insert(store, "keep-one", "KEEPONE the deploy target is us-west-2")
    _insert(store, "drop-two", "DROPTWO an unrelated aside")
    _insert(store, "keep-three", "KEEPTHREE the rollback runbook lives in ops")
    # `recall` embeds the question itself, unlike the block reader which took a vector.
    # Pinned to the vector the rows were seeded against, so the ranking under test is the
    # seeded cosine rather than whatever a downloaded model would produce.
    store.embed_fn = lambda _text, *_a, **_kw: _unit_at_cosine(1.0)
    return store


@pytest.fixture
def bg_loop():
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    loop.call_soon(ready.set)
    thread = threading.Thread(target=loop.run_forever, name="memory-apply-loop", daemon=True)
    thread.start()
    try:
        assert ready.wait(10), "background loop did not start"
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        assert not thread.is_alive(), "background loop did not stop"
        loop.close()


def _answering(monkeypatch, verdict_of):
    """Patch ``core.decide`` so each candidate is answered by *verdict_of(snippet)*."""

    async def _decide(_point, state, questions, **_kw):
        answers = {}
        for index, question in enumerate(questions):
            snippet = state["candidates"][index]["snippet"]
            answers[question.id] = Answer(id=question.id, value=verdict_of(snippet), p=0.9)
        return answers

    monkeypatch.setattr(mr.core, "decide", _decide)


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    from kiro_crew.decisions import log as _log

    monkeypatch.setattr(_log, "log_dir", lambda: tmp_path / "decisions")
    monkeypatch.setattr(mr.core, "is_enabled", lambda *a, **k: True)
    monkeypatch.setattr(mr.core, "timeout_secs", lambda *a, **k: 1.0)


class TestTheKeptSetIsWhatIsReturned:
    def test_a_dropped_memory_is_not_returned(self, seeded, bg_loop, enabled, monkeypatch):
        _answering(monkeypatch, lambda snippet: "drop" if "DROPTWO" in snippet else "keep")
        hook = mr.keep_hook("where do we deploy", session_key="s", loop=bg_loop, owner_turn=True)
        block = _recalled(seeded, keep=hook)
        assert "KEEPONE" in block
        assert "KEEPTHREE" in block
        assert "DROPTWO" not in block

    def test_keeping_nothing_returns_no_episodes(self, seeded, bg_loop, enabled, monkeypatch):
        """An empty kept set is an empty block, not a header with no rows under it."""
        _answering(monkeypatch, lambda _snippet: "drop")
        hook = mr.keep_hook("anything", session_key="s", loop=bg_loop, owner_turn=True)
        assert _recalled(seeded, keep=hook) == ""

    def test_the_response_keeps_the_rankers_order(self, seeded, bg_loop, enabled, monkeypatch):
        """Jev answers keep/drop, so the surviving rows stay in similarity order."""
        _answering(monkeypatch, lambda _snippet: "keep")
        hook = mr.keep_hook("anything", session_key="s", loop=bg_loop, owner_turn=True)
        decided = _recalled(seeded, keep=hook)
        baseline = _recalled(seeded)
        assert decided == baseline


class TestTheBudgetIsAppliedBeforeTheHook:
    """A hook may only narrow the response, so it must not see rows the budget excluded.

    The ordering IS the property. `fit` is the recall's char budget: it decides which
    ranked episodes this recall would return. With the hook shown the pre-budget list,
    dropping a high-ranked memory frees room a lower-ranked one then fits into -- so a
    memory the response would never have carried comes back because Jev removed a
    different one. That is the hook ADDING a memory, the one thing it must not do.
    """

    def _cap_for_two(self, store):
        """A recall cap that returns exactly two of the three seeded episodes."""
        for cap in range(200, 4000, 20):
            rows = self._episodes(store, cap)
            if len(rows) == 2:
                return cap
        raise AssertionError("no cap admits exactly two episodes")

    def _episodes(self, store, cap):
        result = store.recall(QUERY, cap=cap)
        return (result.get("retrieval") or {}).get("episodes") or []

    def test_a_budget_excluded_memory_cannot_enter_when_jev_drops_another(
        self, seeded, bg_loop, enabled, monkeypatch
    ):
        cap = self._cap_for_two(seeded)
        admitted = [row["id"] for row in self._episodes(seeded, cap)]
        assert len(admitted) == 2, admitted
        excluded = next(
            key for key in ("keep-one", "drop-two", "keep-three") if key not in admitted
        )

        # Jev drops the FIRST admitted row. The freed budget must not admit the third.
        first = admitted[0]
        texts = {row["id"]: row["text"] for row in self._episodes(seeded, cap)}
        _answering(monkeypatch, lambda snippet: "drop" if snippet in texts[first] else "keep")
        hook = mr.keep_hook("anything", session_key="s", loop=bg_loop, owner_turn=True)
        result = seeded.recall(QUERY, cap=cap, keep=hook)
        returned = {row["id"] for row in (result.get("retrieval") or {}).get("episodes") or []}
        assert first not in returned
        assert admitted[1] in returned
        assert excluded not in returned, "the budget excluded it, so no answer may admit it"

    def test_the_hook_is_only_asked_about_budget_admitted_rows(
        self, seeded, bg_loop, enabled, monkeypatch
    ):
        """The menu it is offered is the response, not the search."""
        cap = self._cap_for_two(seeded)
        seen: list[int] = []

        def _keep(candidates):
            seen.append(len(candidates))
            return None

        seeded.recall(QUERY, cap=cap, keep=_keep)
        assert seen == [2], "three rows were found; two fit the response"

    def test_a_generous_budget_offers_every_ranked_row(self, seeded):
        seen: list[int] = []

        def _keep(candidates):
            seen.append(len(candidates))
            return None

        seeded.recall(QUERY, cap=12000, keep=_keep)
        assert seen == [3]


class TestTheSwitchOffChangesNothing:
    def test_a_disabled_point_returns_the_similarity_result_unchanged(
        self, seeded, bg_loop, monkeypatch
    ):
        monkeypatch.setattr(mr.core, "is_enabled", lambda *a, **k: False)
        hook = mr.keep_hook("anything", session_key="s", loop=bg_loop, owner_turn=True)
        assert _recalled(seeded, keep=hook) == (_recalled(seeded))

    def test_no_hook_at_all_returns_the_similarity_result_unchanged(self, seeded):
        """The shipped default reaches this method with ``keep=None``."""
        assert _recalled(seeded, keep=None) == (_recalled(seeded))
