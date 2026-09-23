"""Does `memory.recall` fire when the agent calls the `memory_recall` tool?

Every other suite for this point drives it directly or through the store. None of them
answers the question that decides whether the feature exists: a recall goes
`memory_recall` tool -> `GET /api/memory/recall` -> `VectorMemoryStore.recall` ->
`_recall_once`, and a gate anywhere on that chain makes the point unreachable no matter
how correct it is in isolation.

So this drives the whole chain from the route inward, with the real gate -- a real
keystone, a real config snapshot, the real consent and scope reads -- and only the HTTP
transport to the oracle faked. The assertion is the one an operator would make: did a
`memory.recall` row land in the decision log, and did the tool return the kept subset.

It is deliberately the least mocked suite in this feature. A unit test that passes while
this fails describes a function nobody calls.
"""

from __future__ import annotations

import json
import math
import struct
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import session_surface
from kiro_crew.decisions import log as _log
from kiro_crew.vector_memory import VectorMemoryStore

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
SESSION = "chat-reachability"
QUERY = "where do we deploy the signer"

#: Stored vectors are unit vectors, so their dot product with this is their cosine.
_Q = [1.0, 0.0, 0.0, 0.0]

EPISODES = (
    ("mem-one", "the deploy target for the signer is us-west-2"),
    ("mem-two", "an unrelated aside about lunch that mentions the signer"),
    ("mem-three", "the signer rollback runbook lives in the ops repo"),
)


def _unit(cos: float) -> list[float]:
    return [cos, math.sqrt(max(0.0, 1.0 - cos * cos)), 0.0, 0.0]


def _seed(store: VectorMemoryStore) -> None:
    """Episodes close enough in wording to clear the relevance gate."""
    ts = datetime.now(timezone.utc).isoformat()
    for mem_id, text in EPISODES:
        vec = _unit(0.95)
        store.db.execute(
            "INSERT INTO episodic_memories (id, conversation_id, text, tags, embedding,"
            " importance, created_at, last_accessed_at, is_deleted)"
            " VALUES (?, '', ?, '[]', ?, 0.5, ?, ?, 0)",
            (mem_id, text, struct.pack(f"{len(vec)}f", *vec), ts, ts),
        )
    store.db.commit()


class _Oracle:
    """Answers `keep` unless the snippet names lunch. Only the TRANSPORT is faked.

    Consent, the scope, the bucket and the scrub are the real gate, because "is this
    reachable" is a question about those too.
    """

    asked: list = []

    def __init__(self, _provider):
        pass

    async def ask(self, state, questions):
        from kiro_crew.decisions.types import Answer

        _Oracle.asked.append(state)
        answers = {}
        for index, question in enumerate(questions):
            snippet = state["candidates"][index]["snippet"]
            answers[question.id] = Answer(
                id=question.id, value=("drop" if "lunch" in snippet else "keep"), p=0.9
            )
        return answers


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A consented keystone, a live config, a seeded store, and a surfaced session."""
    _Oracle.asked = []

    keystone = tmp_path / "decisions_consent.json"
    keystone.write_text(
        json.dumps(
            {
                "enabled": True,
                "endpoint": ENDPOINT,
                "history_budget_chars": 0,
                "tool_args": False,
                "memory_text": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: keystone)
    monkeypatch.setattr(_log, "log_dir", lambda: tmp_path / "decisions")

    provider = SimpleNamespace(endpoint=ENDPOINT, model="jev-1", timeout_ms=5000, api_key="")
    snapshot = SimpleNamespace(
        decisions=SimpleNamespace(bucket=100, provider=provider, history_budget_chars=0)
    )
    monkeypatch.setattr("kiro_crew.decisions.gate._snapshot", lambda: snapshot)
    monkeypatch.setattr("kiro_crew.decisions.impl_jev.JevOracle", _Oracle)
    monkeypatch.setattr(
        "kiro_crew.decisions.capability.is_decisions_denied", lambda *_a, **_kw: False
    )

    store = VectorMemoryStore(db_path=tmp_path / "mem.db")
    store.init()
    _seed(store)
    # The embedder the real path would use, pinned so the query vector is the one the
    # seeded rows were built against rather than a model download.
    store.embed_fn = lambda _text, *_a, **_kw: list(_Q)

    session_surface.set_dashboard_surfaced({SESSION})
    yield SimpleNamespace(store=store, log_dir=tmp_path / "decisions")
    session_surface.set_dashboard_surfaced(set())


def _rows(log_dir: Path) -> list[dict]:
    if not log_dir.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(log_dir.glob("decisions-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _request(session_key: str = SESSION):
    """A request shaped like the MCP tool's own internally-authenticated recall call."""
    request = MagicMock()
    request.path = "/api/memory/recall"
    store = {"internal_auth": True}
    request.get = lambda key, default=None: store.get(key, default)
    request.query = {"q": QUERY}
    request.headers = {"X-Session-Key": session_key}
    return request


async def _recall(wired, session_key: str = SESSION) -> dict:
    """Drive the ROUTE, so the hook is armed by the code that arms it in production.

    Only the store resolution and the session recognition are stubbed -- those answer
    "which member's memory" and "is this session real", neither of which this suite is
    about. Everything from the hook builder inward is the real path, including
    `run_in_embed_pool`, which is the worker thread the point must be called on.
    """
    from kiro_crew.dashboard.handlers import memory_member as mm

    request = _request(session_key)
    with (pytest.MonkeyPatch.context() as patch,):
        patch.setattr(mm, "_recognize_session", AsyncMock(return_value=None))
        patch.setattr(mm, "_blocks_reads_session", lambda *_a, **_kw: False)
        patch.setattr(mm, "resolve_lesson_memory_store", AsyncMock(return_value=("", None)))
        patch.setattr(mm, "vector_memory_for_store", AsyncMock(return_value=wired.store))
        patch.setattr(mm, "markdown_memory_for_store", AsyncMock(return_value=None))
        patch.setattr(mm, "requesting_slot_project", lambda *_a, **_kw: None)
        response = await mm.api_memory_recall(request)
    assert response.status == 200, response.text
    return json.loads(response.text)


class TestTheHookIsReachedOnARealRecall:
    @pytest.mark.asyncio
    async def test_a_tool_recall_reaches_the_point(self, wired):
        """The claim this whole suite exists for.

        If the hook is not reached on this path, no question is asked, no row is written,
        and every other test in this feature describes a function production never runs.
        """
        await _recall(wired)
        rows = _rows(wired.log_dir)
        points = [row.get("point") for row in rows]
        assert "memory.recall" in points, (
            "no memory.recall row: the point was never reached on a real tool recall. "
            f"rows={rows}"
        )

    @pytest.mark.asyncio
    async def test_the_question_carried_the_recalled_candidates(self, wired):
        """Reached AND asked about the right thing, not merely reached."""
        await _recall(wired)
        assert _Oracle.asked, "the oracle was never asked"
        keys = {row["key"] for row in _Oracle.asked[0]["candidates"]}
        assert keys, "the question carried no candidates"
        assert keys <= {mem_id for mem_id, _text in EPISODES}

    @pytest.mark.asyncio
    async def test_the_tool_returns_the_kept_subset(self, wired):
        """And the answer reaches the RESPONSE, which is the point of the feature."""
        payload = await _recall(wired)
        episodes = payload["retrieval"]["episodes"]
        ids = {row["id"] for row in episodes}
        assert "mem-one" in ids, payload
        assert "mem-two" not in ids, "the dropped episode is still in the response"

    @pytest.mark.asyncio
    async def test_the_outcome_row_names_both_arms(self, wired):
        payload = await _recall(wired)
        outcome = next(r for r in _rows(wired.log_dir) if "baseline_keys" in r)
        assert outcome["point"] == "memory.recall"
        assert set(outcome["baseline_keys"]) == {mem_id for mem_id, _t in EPISODES}
        assert "mem-two" not in outcome["jev_keys"]
        assert outcome["agree"] is False
        assert outcome["chars_saved"] > 0
        # The row and the response describe ONE recall.
        assert set(outcome["jev_keys"]) == {row["id"] for row in payload["retrieval"]["episodes"]}

    @pytest.mark.asyncio
    async def test_the_outcome_is_published_for_the_strip(self, wired, monkeypatch):
        """The receipt rides the assistant reply through the same hand-off as skills.

        The tool runs mid-turn, after `chat_runner` discarded the previous turn's
        leftover, so the outcome published here is claimed by the reply that ends THIS
        turn.
        """
        from kiro_crew.decisions import outcomes

        seen: list = []
        monkeypatch.setattr(outcomes, "publish", lambda key, row: seen.append((key, row)))
        await _recall(wired)
        assert [key for key, _row in seen] == [SESSION]
        assert seen[0][1]["point"] == "memory.recall"


class TestOnlyACommittedRecallLeavesAReceipt:
    """A row must describe a subset the caller actually received.

    `_recall_once` validates the embedding generation AFTER applying the hook, and
    `recall` answers a moved generation by running the whole recall again keyword-only.
    So an outcome written the moment the answer arrived could describe a subset the store
    then discarded -- and a retried recall would leave two rows for one tool call. The
    point holds its outcome and the route commits it once the recall has come back.
    """

    def _outcomes(self, wired) -> list[dict]:
        return [row for row in _rows(wired.log_dir) if "baseline_keys" in row]

    @pytest.mark.asyncio
    async def test_a_late_answer_leaves_no_outcome_row(self, wired, monkeypatch):
        """The provider answers past the caller's wait, so nothing was applied.

        The gate's own call row may still land -- it records that a call timed out, which
        is a fact an operator acts on. What must not exist is an OUTCOME row, because no
        subset was chosen and none was returned.
        """
        import asyncio as _asyncio

        from kiro_crew.decisions.points import memory_recall as mr

        class _Slow:
            def __init__(self, _provider):
                pass

            async def ask(self, _state, _questions):
                await _asyncio.sleep(5)
                raise AssertionError("the caller must have stopped waiting by now")

        monkeypatch.setattr("kiro_crew.decisions.impl_jev.JevOracle", _Slow)
        monkeypatch.setattr(mr, "MIN_WAIT_SECS", 0.05)
        monkeypatch.setattr(mr, "MAX_WAIT_SECS", 0.05)

        payload = await _recall(wired)
        assert self._outcomes(wired) == [], "a late answer must leave no outcome row"
        # And the recall still answered, whole.
        ids = {row["id"] for row in payload["retrieval"]["episodes"]}
        assert ids == {mem_id for mem_id, _t in EPISODES}

    @pytest.mark.asyncio
    async def test_a_discarded_generation_leaves_no_row_for_the_attempt_it_threw_away(
        self, wired, monkeypatch
    ):
        """One tool call, one row -- the retry's, not the discarded attempt's.

        The store validates the embedding generation after the hook has run. Forcing that
        check to fail once makes `recall` retry, so the hook runs twice and exactly one of
        those attempts produced the result the caller got.
        """
        from kiro_crew.vector_memory import _RecallSpaceChanged

        # Discard the first attempt AFTER it has run, which is the production sequence:
        # `_recall_once` applies the hook and only then validates the generation, so the
        # hook has already decided by the time the result is thrown away. Patching the
        # validator itself would raise BEFORE the hook on its first call, which is a
        # different (and harmless) path.
        calls: list[int] = []
        real_once = type(wired.store)._recall_once

        def _discard_first(self, *args, **kwargs):
            result = real_once(self, *args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                raise _RecallSpaceChanged("the space moved under this query")
            return result

        monkeypatch.setattr(type(wired.store), "_recall_once", _discard_first)

        # The two attempts must answer DIFFERENTLY, or "commit the last one" is
        # indistinguishable from "commit the first" and this test proves nothing about
        # WHICH row survives. The discarded attempt keeps everything; the retry drops one.
        asks: list[int] = []

        class _VaryingOracle(_Oracle):
            async def ask(self, state, questions):
                from kiro_crew.decisions.types import Answer

                asks.append(1)
                _Oracle.asked.append(state)
                first = len(asks) == 1
                return {
                    q.id: Answer(
                        id=q.id,
                        value=(
                            "keep"
                            if first or "lunch" not in state["candidates"][i]["snippet"]
                            else "drop"
                        ),
                        p=0.9,
                    )
                    for i, q in enumerate(questions)
                }

        monkeypatch.setattr("kiro_crew.decisions.impl_jev.JevOracle", _VaryingOracle)

        payload = await _recall(wired)
        assert len(calls) == 2, "the store did not retry, so this proves nothing"
        assert len(_Oracle.asked) == 2, "the hook decided twice, which is the hazard"
        outcomes = self._outcomes(wired)
        assert len(outcomes) == 1, f"one recall must leave one outcome row, got {outcomes}"
        # And the row that survived is the one whose result came back -- the retry's
        # narrower set, never the discarded attempt's wider one.
        returned = {row["id"] for row in payload["retrieval"]["episodes"]}
        assert set(outcomes[0]["jev_keys"]) == returned
        assert len(returned) == 2, "the retry dropped one, which is what makes the two differ"

    @pytest.mark.asyncio
    async def test_a_retry_that_finds_nothing_commits_no_row_for_the_first_attempt(
        self, wired, monkeypatch
    ):
        """The retry REFUSES, so the discarded attempt's outcome must not be what commits.

        Every refusal in the point returns without appending. The first attempt decided
        and its outcome is on the list; the keyword retry is handed an empty candidate
        list, refuses on it, and the recall comes back carrying no memories at all. A
        list that kept what the first attempt left would make the caller write a row
        describing three kept memories for a response that returned none.
        """
        from kiro_crew.vector_memory import _RecallSpaceChanged

        calls: list[int] = []
        real_once = type(wired.store)._recall_once

        def _discard_first(self, *args, **kwargs):
            result = real_once(self, *args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                raise _RecallSpaceChanged("the space moved under this query")
            return result

        monkeypatch.setattr(type(wired.store), "_recall_once", _discard_first)

        # The RETRY finds nothing. Keyed on the ATTEMPT rather than on a call count,
        # because `search_episodic` re-enters itself once to hold the identity check and
        # the index read under one lock, so each attempt reaches this twice. `calls` is
        # appended after an attempt returns, so it is empty for the whole first one.
        real_search = wired.store.search_episodic

        def _empty_on_the_retry(*args, **kwargs):
            if calls:
                return []
            return real_search(*args, **kwargs)

        monkeypatch.setattr(wired.store, "search_episodic", _empty_on_the_retry)

        payload = await _recall(wired)

        assert len(calls) == 2, "the store did not retry, so this proves nothing"
        assert len(_Oracle.asked) == 1, "the first attempt decided, which is the hazard"
        assert payload["retrieval"]["episodes"] == [], "the returned recall carried none"
        assert self._outcomes(wired) == [], (
            "a refused retry must leave NO row -- the only outcome ever made describes "
            "a subset of a result the store threw away"
        )

    @pytest.mark.asyncio
    async def test_a_failing_route_commits_nothing(self, wired, monkeypatch):
        """A decision made for a response the caller never gets leaves no receipt."""
        from kiro_crew.dashboard.handlers import memory_member as mm

        real_recall = wired.store.recall

        def _then_fail(*args, **kwargs):
            real_recall(*args, **kwargs)
            raise OSError("the store went away after answering")

        monkeypatch.setattr(wired.store, "recall", _then_fail)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mm, "_recognize_session", AsyncMock(return_value=None))
            patch.setattr(mm, "_blocks_reads_session", lambda *_a, **_kw: False)
            patch.setattr(mm, "resolve_lesson_memory_store", AsyncMock(return_value=("", None)))
            patch.setattr(mm, "vector_memory_for_store", AsyncMock(return_value=wired.store))
            patch.setattr(mm, "markdown_memory_for_store", AsyncMock(return_value=None))
            patch.setattr(mm, "requesting_slot_project", lambda *_a, **_kw: None)
            response = await mm.api_memory_recall(_request())
        assert response.status != 200
        assert _Oracle.asked, "the decision was made, which is what makes this a test"
        assert self._outcomes(wired) == [], "a refused response must leave no receipt"

    @pytest.mark.asyncio
    async def test_a_timed_out_request_records_nothing(self, wired):
        """The caller received 504, so a receipt would describe memories nobody got.

        `memory_recall_deadline` bounds the route with `asyncio.wait_for`, and a
        cancellation is delivered only where the coroutine yields -- which is the
        `to_thread` that commits. The work reaches the executor before that cancellation
        arrives and a running thread does not cancel, so the row lands while the response
        goes out as `504 memory_recall_timeout`.

        Held in two halves, because the route and the handler answer different questions.
        The bounded route is asked what the CALLER gets on a spent budget: 504. The handler
        body is then run on that same spent budget, which is the state it is in whenever
        that cancellation is due -- it answers the recall in full, and the receipt is the
        one thing that must not survive it.
        """
        import time

        from kiro_crew.dashboard.handlers import memory_member as mm
        from kiro_crew.embeddings import EmbeddingWork, embedding_work

        def _spent():
            work = EmbeddingWork(time.monotonic() - 1.0)
            assert work.expired(), "the budget must be spent, or this proves nothing"
            return work

        # Value-based restore, not a token. A `Token` is bound to the Context it was
        # taken in, and both blocks below cross an `await`; a worker sharing one
        # main-thread Context across tests would then raise ValueError out of `reset`.
        # The var defaults to `None` and the predicate reads absence as live, so
        # restoring the previous VALUE is exact here rather than merely close.
        previous = embedding_work.get()
        embedding_work.set(_spent())
        try:
            timed_out = await mm.api_memory_recall(_request())
        finally:
            embedding_work.set(previous)
        assert timed_out.status == 504, f"the caller must get a timeout, got {timed_out.text}"

        # The budget is HEALTHY when the judge is asked and spent by the time the
        # commit runs, which is the state the race actually produces: the store burns
        # the rest of it after answering, so the decision is real and the deadline has
        # passed before the receipt would be written. An already-spent budget cannot
        # show this any more -- the point refuses to ask on one at all.
        real_recall = wired.store.recall

        def _answer_then_outlast_the_deadline(*args, **kwargs):
            result = real_recall(*args, **kwargs)
            time.sleep(1.4)
            return result

        # Wide enough that the judge is funded when it is asked -- the whole point of
        # this half -- and then outlived by the sleep above, so the deadline has passed
        # by the time the commit would run.
        embedding_work.set(EmbeddingWork(time.monotonic() + 1.2))
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(wired.store, "recall", _answer_then_outlast_the_deadline)
                patch.setattr(mm, "_recognize_session", AsyncMock(return_value=None))
                patch.setattr(mm, "_blocks_reads_session", lambda *_a, **_kw: False)
                patch.setattr(mm, "resolve_lesson_memory_store", AsyncMock(return_value=("", None)))
                patch.setattr(mm, "vector_memory_for_store", AsyncMock(return_value=wired.store))
                patch.setattr(mm, "markdown_memory_for_store", AsyncMock(return_value=None))
                patch.setattr(mm, "requesting_slot_project", lambda *_a, **_kw: None)
                # The handler itself, so the body actually reaches the commit rather than
                # being refused by the bound above it.
                response = await mm.api_memory_recall.__wrapped__(_request())
        finally:
            embedding_work.set(previous)

        assert response.status == 200, response.text
        assert _Oracle.asked, "the decision was made, which is what makes this a test"
        payload = json.loads(response.text)
        ids = {row["id"] for row in payload["retrieval"]["episodes"]}
        assert "mem-one" in ids, f"the recall must still answer in full, got {payload}"
        assert self._outcomes(wired) == [], (
            "a request whose deadline had passed left a receipt: the row claims a subset "
            "reached a caller who received a timeout instead"
        )

    @pytest.mark.asyncio
    async def test_a_bounded_payload_keeps_the_arithmetic_closed(self, wired, monkeypatch):
        """The decision is not the last thing that shortens the recall.

        `bound_recall_payload` runs after it and holds the response to a transport
        budget, dropping whole rows off the tail once there is no text left to clip --
        and multibyte content reaches that budget on an ordinary query, so this is the
        common case rather than an exotic one.

        The receipt names the two removals SEPARATELY: what Jev kept, and how many of
        those the budget then dropped. Folding the second into the first made the
        header read "Jev kept 1" beside a row saying "2 that Jev kept did not fit",
        which is arithmetic a reader cannot close.

        The cap is lowered to reach the same state a large payload reaches on its own.
        Jev keeps two of three here and the budget drops one of those two.
        """
        from kiro_crew.dashboard.handlers import memory_member as mm

        monkeypatch.setattr(mm, "_RECALL_CONTEXT_CAP", 160)

        payload = await _recall(wired)
        retrieval = payload["retrieval"]
        delivered = [row["id"] for row in retrieval["episodes"]]

        # Non-vacuous: bounding must actually have removed something, or this test
        # passes on a payload the decision alone shaped.
        assert retrieval.get("omitted_for_payload_budget") == 1, retrieval
        assert delivered == ["mem-one"], delivered

        outcome = self._outcomes(wired)[0]
        # `jev_keys` is the DECISION's output and stays so. The budget's removal is a
        # second number beside it, so the two ADD UP to what shipped and each part
        # stays attributable to whoever removed it.
        assert outcome["jev_keys"] == ["mem-one", "mem-three"], outcome
        assert outcome["bounded_omitted"] == 1, outcome
        assert len(outcome["jev_keys"]) - outcome["bounded_omitted"] == len(delivered), (
            "the arithmetic does not close: "
            f"kept={outcome['jev_keys']} omitted={outcome['bounded_omitted']} "
            f"delivered={delivered}"
        )

        # And the saving is JEV's own removal, untouched by the budget: both arms were
        # measured on the same rows before either redaction or bounding ran.
        assert outcome["chars_saved"] == outcome["baseline_chars"] - outcome["jev_chars"]
        assert outcome["chars_saved"] > 0

    @pytest.mark.asyncio
    async def test_a_slow_search_shortens_the_judge_wait(self, wired, monkeypatch):
        """The judge's wait is the time LEFT, not a constant.

        The search runs BEFORE the judge does, inside the same bounded request, so a
        cold store, a rebuilt index or a slow embedding spends budget the judge would
        then assume it still had. Capped by its own constant the judge could overrun
        the route's deadline and the tool would answer `504` instead of the memories
        the search had already found -- the one direction this seam must not fail in.

        The search is made to burn a third of the budget, and the wait the judge is
        actually given has to reflect that.
        """
        import time

        from kiro_crew.decisions.points import memory_recall as mr
        from kiro_crew.embeddings import EmbeddingWork, embedding_work

        budget, burn = 3.0, 1.0

        waits: list[float] = []
        real_budget = mr._wait_budget

        def _record_wait() -> float:
            value = real_budget()
            waits.append(value)
            return value

        monkeypatch.setattr(mr, "_wait_budget", _record_wait)

        # Burned ONCE: `search_episodic` re-enters itself to hold the identity check
        # and the index read under one lock, so a bare sleep would be paid twice.
        burned: list[int] = []
        real_search = wired.store.search_episodic

        def _slow_search(*args, **kwargs):
            if not burned:
                burned.append(1)
                time.sleep(burn)
            return real_search(*args, **kwargs)

        monkeypatch.setattr(wired.store, "search_episodic", _slow_search)

        previous = embedding_work.get()
        embedding_work.set(EmbeddingWork(time.monotonic() + budget))
        started = time.monotonic()
        try:
            payload = await _recall(wired)
        finally:
            embedding_work.set(previous)
        elapsed = time.monotonic() - started

        assert burned, "the search did not burn any budget, so this proves nothing"
        assert waits, "the wait was never computed"
        # Shortened: the provider's own budget here is 5.5 s and the no-deadline
        # ceiling is 7 s, so anything near either would mean the deadline was ignored.
        assert 0 < waits[0] < 2.0, (
            f"the judge was given {waits[0]}s of a {budget}s request that had already "
            f"spent {burn}s, rather than the time remaining"
        )
        assert waits[0] < mr.MAX_WAIT_SECS
        # And the recall still answered inside the deadline, narrowed.
        assert elapsed < budget, f"the recall took {elapsed}s of a {budget}s budget"
        ids = {row["id"] for row in payload["retrieval"]["episodes"]}
        assert ids == {"mem-one", "mem-three"}, payload

    def test_no_time_left_keeps_the_unnarrowed_result_and_asks_nothing(self, wired):
        """Out of budget is a Jev failure, and every one of those keeps the search.

        Asking would spend a wait the request cannot fund, and the answer would reach
        a caller the route had already given up on. So the point declines: the rows go
        back exactly as the search ranked them, nothing is sent, and no receipt is
        held -- which is what every other refusal on this path does.
        """
        import asyncio as _asyncio
        import threading
        import time

        from kiro_crew.decisions.points import memory_recall as mr
        from kiro_crew.embeddings import EmbeddingWork, embedding_work

        rows = [{"id": mem_id, "text": text} for mem_id, text in EPISODES]
        pending: list = []

        loop = _asyncio.new_event_loop()
        ready = threading.Event()
        loop.call_soon(ready.set)
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        previous = embedding_work.get()
        # Live enough that `expired()` is False, but with less left than the slack the
        # rest of the request needs -- which is the state a slow search produces.
        embedding_work.set(EmbeddingWork(time.monotonic() + mr.WAIT_MARGIN_SECS / 2))
        try:
            assert ready.wait(10)
            assert mr._remaining_budget() < 0, "the budget must be out, or this proves nothing"
            kept = mr.kept_memories(
                rows,
                QUERY,
                session_key=SESSION,
                loop=loop,
                owner_turn=True,
                pending=pending,
            )
        finally:
            embedding_work.set(previous)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)
            loop.close()

        assert kept is None, "the unnarrowed result is what a refusal returns"
        assert _Oracle.asked == [], "nothing may be sent on a request with no time left"
        assert pending == [], "a decision never made leaves no receipt to commit"

    @pytest.mark.asyncio
    async def test_a_tab_closed_during_the_search_sends_nothing(self, wired, monkeypatch):
        """The egress gate is read where the egress happens, not before the search.

        `_memory_recall_keep` runs on the event loop before `recall` is dispatched, so
        its membership test is a cheap early refusal. The question it answers can stop
        being true while the search runs: the owner closes the tab and the slot leaves
        the published set, and a decision built on the earlier answer would still send
        snippets of their remembered notes. This closes the tab mid-search.
        """
        from kiro_crew.dashboard.handlers import memory_member as mm

        closed: list[int] = []
        real_search = wired.store.search_episodic

        def _close_the_tab(*args, **kwargs):
            if not closed:
                closed.append(1)
                session_surface.set_dashboard_surfaced(set())
            return real_search(*args, **kwargs)

        monkeypatch.setattr(wired.store, "search_episodic", _close_the_tab)

        request = _request()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mm, "_recognize_session", AsyncMock(return_value=None))
            patch.setattr(mm, "_blocks_reads_session", lambda *_a, **_kw: False)
            patch.setattr(mm, "resolve_lesson_memory_store", AsyncMock(return_value=("", None)))
            patch.setattr(mm, "vector_memory_for_store", AsyncMock(return_value=wired.store))
            patch.setattr(mm, "markdown_memory_for_store", AsyncMock(return_value=None))
            patch.setattr(mm, "requesting_slot_project", lambda *_a, **_kw: None)
            response = await mm.api_memory_recall(request)

        assert closed, "the tab never closed, so this proves nothing"
        assert response.status == 200, response.text
        assert _Oracle.asked == [], "recalled memory left the machine for a closed tab"
        payload = json.loads(response.text)
        ids = {row["id"] for row in payload["retrieval"]["episodes"]}
        assert ids == {mem_id for mem_id, _text in EPISODES}, "the recall must answer in full"
        assert self._outcomes(wired) == [], "a decision never made leaves no receipt"

    @pytest.mark.asyncio
    async def test_the_committed_row_matches_what_came_back(self, wired):
        """The positive half: one row, and it describes the response."""
        payload = await _recall(wired)
        outcomes = self._outcomes(wired)
        assert len(outcomes) == 1
        assert set(outcomes[0]["jev_keys"]) == {
            row["id"] for row in payload["retrieval"]["episodes"]
        }

    def test_the_point_writes_nothing_without_a_pending_list(self, wired, monkeypatch):
        """`pending=None` decides and records nothing, which is the contract.

        Held directly, because it is what makes the deferral a property of the POINT
        rather than a habit of its one caller.
        """
        import asyncio as _asyncio
        import threading

        from kiro_crew.decisions.points import memory_recall as mr

        loop = _asyncio.new_event_loop()
        ready = threading.Event()
        loop.call_soon(ready.set)
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        try:
            assert ready.wait(10)
            kept = mr.kept_memories(
                [{"id": "mem-one", "text": "a memory"}],
                QUERY,
                session_key=SESSION,
                loop=loop,
                owner_turn=True,
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)
            loop.close()
        assert kept is not None, "the decision still happened"
        assert [row for row in _rows(wired.log_dir) if "baseline_keys" in row] == []


class TestTheGatesStillHold:
    @pytest.mark.asyncio
    async def test_an_unsurfaced_session_is_not_decided_for(self, wired):
        """A recall nobody is watching asks nothing, and still answers."""
        session_surface.set_dashboard_surfaced(set())
        payload = await _recall(wired)
        assert _Oracle.asked == []
        assert [r for r in _rows(wired.log_dir) if r.get("point") == "memory.recall"] == []
        ids = {row["id"] for row in payload["retrieval"]["episodes"]}
        assert "mem-two" in ids, "the similarity result must come back whole"

    @pytest.mark.asyncio
    async def test_a_retained_dashboard_slot_is_not_decided_for(self, wired):
        """A closed tab whose KEY still looks dashboard-born is not a live audience.

        A retained or archived slot keeps its `dashboard:` key and leaves the published
        set, so the two readings of "surfaced" disagree here and the gate has to take the
        published one: nobody is watching this conversation, so nothing may be sent about
        it and no receipt may be stamped on the reply.
        """
        archived = "dashboard:closed-tab"
        assert session_surface.has_dashboard_surface(archived), (
            "the prefix reading says yes for this key, which is what makes it the case "
            "that pins the gate rather than a restatement of the unsurfaced one"
        )
        assert archived not in session_surface.dashboard_surfaced_keys()

        payload = await _recall(wired, session_key=archived)

        assert _Oracle.asked == []
        assert [r for r in _rows(wired.log_dir) if r.get("point") == "memory.recall"] == []
        ids = {row["id"] for row in payload["retrieval"]["episodes"]}
        assert "mem-two" in ids, "the similarity result must come back whole"

    @pytest.mark.asyncio
    async def test_no_consent_scope_asks_nothing_and_returns_everything(self, wired, tmp_path):
        """The `memory_text` scope, held on the live path rather than in isolation."""
        (tmp_path / "decisions_consent.json").write_text(
            json.dumps({"enabled": True, "endpoint": ENDPOINT}), encoding="utf-8"
        )
        payload = await _recall(wired)
        assert _Oracle.asked == []
        ids = {row["id"] for row in payload["retrieval"]["episodes"]}
        assert "mem-two" in ids

    @pytest.mark.asyncio
    async def test_a_failing_oracle_returns_everything(self, wired, monkeypatch):
        """Every refusal is the shipped recall, on the live path."""

        class _Broken:
            def __init__(self, _provider):
                pass

            async def ask(self, _state, _questions):
                raise RuntimeError("boom")

        monkeypatch.setattr("kiro_crew.decisions.impl_jev.JevOracle", _Broken)
        payload = await _recall(wired)
        ids = {row["id"] for row in payload["retrieval"]["episodes"]}
        assert ids == {mem_id for mem_id, _t in EPISODES}


class TestTheHookIsNotOnThePromptPath:
    """There is no seam on the prompt-assembly path, and that absence is deliberate.

    Prompt assembly asks `MemoryStore.get_context` for preferences with
    `include_activity=False`, and that flag also gates episodic retrieval, so the block
    is not built. A `keep=` parameter there is a second door nothing opens, which the
    next reader would take for a live path -- so these three assertions pin that it
    stays absent.
    """

    def test_get_episodic_context_takes_no_hook(self, wired):
        import inspect

        assert "keep" not in inspect.signature(wired.store.get_episodic_context).parameters

    def test_get_context_carries_no_hook(self):
        import inspect

        from kiro_crew.memory import MemoryStore

        assert "episodic_keep" not in inspect.signature(MemoryStore.get_context).parameters

    def test_the_context_builder_has_no_hook_builder(self):
        from kiro_crew.context import ContextBuilder

        assert not hasattr(ContextBuilder, "_episodic_keep_hook")

    def test_recall_is_the_seam(self, wired):
        import inspect

        assert "keep" in inspect.signature(wired.store.recall).parameters
