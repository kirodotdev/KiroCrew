"""``memory.recall``: what it keeps, what it sends, and everything it refuses.

The point narrows a list, so most properties here are about the two directions it
must never move in: it may not admit a memory the search did not rank, and every
failure must leave the similarity top-k exactly as it is. The positive claims are
that a keep/drop answer is applied live, that the record names both arms, and that
each snippet is redacted BEFORE it is clipped.

``decide`` is patched at the module the point calls it through, so these drive the
point's own logic rather than the gate's -- the gate has its own suite. The tests
that need the REAL gate (the point name being admitted, the scrub over a snippet)
say so and use it.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.decisions import consent, gate
from kiro_crew.decisions import log as _log
from kiro_crew.decisions import outcomes as _outcomes
from kiro_crew.decisions.points import memory_recall as mr
from kiro_crew.decisions.types import Answer
from kiro_crew.vector_memory import EPISODIC_BLOCK_TEXT_CHARS, _kept_episodes


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private ``config_dir`` so the log writes under the test's own tree."""
    monkeypatch.setattr(_log, "log_dir", lambda: tmp_path / "decisions")
    return tmp_path


def _rows(home) -> list[dict[str, Any]]:
    directory = home / "decisions"
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("decisions-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _episode(key: str, text: str = "an episode") -> dict:
    """One row in the shape ``search_episodic`` yields."""
    return {"id": key, "text": text, "score": 0.5, "cosine_sim": 0.5}


def _answers(*verdicts: tuple[str, float]) -> dict[str, Answer]:
    """One answer per candidate, in candidate order."""
    return {
        mr.question_id(index): Answer(id=mr.question_id(index), value=value, p=p)
        for index, (value, p) in enumerate(verdicts)
    }


@pytest.fixture
def bg_loop():
    """Run the target loop off the test thread, with a bounded startup wait."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    loop.call_soon(ready.set)
    thread = threading.Thread(target=loop.run_forever, name="memory-recall-test-loop", daemon=True)
    thread.start()
    try:
        assert ready.wait(10), "background loop did not start"
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        assert not thread.is_alive(), "background loop did not stop"
        loop.close()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(mr.core, "is_enabled", lambda *a, **k: True)
    monkeypatch.setattr(mr.core, "timeout_secs", lambda *a, **k: 0.0)
    monkeypatch.setattr(mr, "WAIT_MARGIN_SECS", 0.0)
    monkeypatch.setattr(mr, "MIN_WAIT_SECS", 0.5)


def _kept(candidates, *, loop, monkeypatch, answers, session_key="s", owner_turn=True):
    """Decide, then COMMIT, which is what the recall route does in that order.

    The point holds its outcome rather than writing it, so a caller that never commits
    leaves no row -- see
    ``test_decisions_memory_recall_reachable.py::TestOnlyACommittedRecallLeavesAReceipt``
    for why. Committing here keeps every row assertion below meaning what it says.
    """
    monkeypatch.setattr(mr.core, "decide", AsyncMock(return_value=answers))
    pending: list = []
    kept = mr.kept_memories(
        candidates,
        "what did we decide",
        session_key=session_key,
        loop=loop,
        owner_turn=owner_turn,
        pending=pending,
    )
    _commit(session_key, pending, kept if kept is not None else candidates)
    return kept


def _payload(rows) -> dict:
    """The response shape :func:`mr.commit_receipt` is handed: bounded and final."""
    return {"store": "", "retrieval": {"episodes": [dict(row) for row in rows]}}


def _commit(session_key, pending, delivered) -> bool:
    """Drive the ONE writer the way the route drives it."""
    return mr.commit_receipt(session_key, pending, payload=_payload(delivered))


# ── the decision, applied ─────────────────────────────────────────────────────


class TestTheDecision:
    def test_a_keep_drop_answer_narrows_the_injected_set(self, home, bg_loop, enabled, monkeypatch):
        rows = [_episode("a"), _episode("b"), _episode("c")]
        kept = _kept(
            rows,
            loop=bg_loop,
            monkeypatch=monkeypatch,
            answers=_answers(("keep", 0.9), ("drop", 0.8), ("keep", 0.7)),
        )
        assert [row["id"] for row in kept] == ["a", "c"]

    def test_keeping_nothing_is_a_real_answer_and_not_a_refusal(
        self, home, bg_loop, enabled, monkeypatch
    ):
        """An empty LIST, never ``None``: "none of these help" is a decision."""
        kept = _kept(
            [_episode("a")],
            loop=bg_loop,
            monkeypatch=monkeypatch,
            answers=_answers(("drop", 0.9)),
        )
        assert kept == []

    def test_a_confident_drop_is_not_read_as_a_confident_keep(
        self, home, bg_loop, enabled, monkeypatch
    ):
        """The provider reports the CHOSEN option's probability.

        Without the complement, ``drop`` at 0.95 clears a 0.5 threshold on the raw
        number and every candidate is kept -- the point becomes a no-op that still
        pays the latency and still sends the snippets.
        """
        assert mr.keep_probability(Answer(id="m0", value="drop", p=0.95)) == pytest.approx(0.05)
        assert mr.keep_probability(Answer(id="m0", value="keep", p=0.95)) == pytest.approx(0.95)

    def test_the_threshold_is_inclusive_at_its_own_value(self):
        """A keep probability exactly at the constant keeps, so the bound is stated."""
        assert mr.KEEP_THRESHOLD == 0.5
        answer = Answer(id="m0", value="keep", p=mr.KEEP_THRESHOLD)
        assert mr.read_answer({"m0": answer}, [{"key": "a"}]) == ["a"]

    def test_order_is_the_rankers_and_not_the_answers(self, home, bg_loop, enabled, monkeypatch):
        """Kept rows come back in CANDIDATE order.

        A keep/drop answer says nothing about rank, so reordering on it would
        discard the ranker's judgement for one that was never made.
        """
        rows = [_episode("a"), _episode("b"), _episode("c")]
        kept = _kept(
            rows,
            loop=bg_loop,
            monkeypatch=monkeypatch,
            answers=_answers(("keep", 0.9), ("keep", 0.9), ("keep", 0.9)),
        )
        assert [row["id"] for row in kept] == ["a", "b", "c"]


# ── every refusal is the shipped recall ───────────────────────────────────────


class TestRefusals:
    def test_a_turn_that_is_not_an_owner_dashboard_one_asks_nothing(
        self, home, bg_loop, enabled, monkeypatch
    ):
        spy = AsyncMock(return_value=_answers(("keep", 0.9)))
        monkeypatch.setattr(mr.core, "decide", spy)
        assert (
            mr.kept_memories([_episode("a")], "hi", session_key="s", loop=bg_loop, owner_turn=False)
            is None
        )
        spy.assert_not_awaited()

    def test_a_disabled_point_redacts_nothing_and_asks_nothing(self, home, bg_loop, monkeypatch):
        monkeypatch.setattr(mr.core, "is_enabled", lambda *a, **k: False)
        spy = AsyncMock()
        monkeypatch.setattr(mr.core, "decide", spy)
        scrubs: list[str] = []
        monkeypatch.setattr(mr, "scrubbed", lambda text, limit: scrubs.append(str(text)) or "")
        assert (
            mr.kept_memories([_episode("a")], "hi", session_key="s", loop=bg_loop, owner_turn=True)
            is None
        )
        spy.assert_not_awaited()
        assert scrubs == []

    def test_no_candidates_asks_nothing(self, home, bg_loop, enabled, monkeypatch):
        spy = AsyncMock()
        monkeypatch.setattr(mr.core, "decide", spy)
        assert mr.kept_memories([], "hi", session_key="s", loop=bg_loop, owner_turn=True) is None
        spy.assert_not_awaited()

    def test_a_refusing_gate_keeps_the_baseline(self, home, bg_loop, enabled, monkeypatch):
        monkeypatch.setattr(mr.core, "decide", AsyncMock(return_value=None))
        assert (
            mr.kept_memories([_episode("a")], "hi", session_key="s", loop=bg_loop, owner_turn=True)
            is None
        )

    def test_a_partial_answer_keeps_the_baseline(self, home, bg_loop, enabled, monkeypatch):
        """A missing answer is indistinguishable from "drop it".

        Applying the rest would silently drop a memory nobody decided about, so a
        partial reading is a refusal rather than a partial application.
        """
        kept = _kept(
            [_episode("a"), _episode("b")],
            loop=bg_loop,
            monkeypatch=monkeypatch,
            answers=_answers(("keep", 0.9)),
        )
        assert kept is None

    def test_an_out_of_domain_answer_keeps_the_baseline(self, home, bg_loop, enabled, monkeypatch):
        answers = {"m0": Answer(id="m0", value="maybe", p=0.9)}
        assert (
            _kept([_episode("a")], loop=bg_loop, monkeypatch=monkeypatch, answers=answers) is None
        )

    def test_a_raising_transport_keeps_the_baseline(self, home, bg_loop, enabled, monkeypatch):
        monkeypatch.setattr(mr.core, "decide", AsyncMock(side_effect=RuntimeError("boom")))
        assert (
            mr.kept_memories([_episode("a")], "hi", session_key="s", loop=bg_loop, owner_turn=True)
            is None
        )

    def test_no_loop_means_no_decision(self, home, enabled, monkeypatch):
        spy = AsyncMock()
        monkeypatch.setattr(mr.core, "decide", spy)
        assert (
            mr.kept_memories([_episode("a")], "hi", session_key="s", loop=None, owner_turn=True)
            is None
        )
        spy.assert_not_awaited()

    def test_a_loop_that_is_not_running_means_no_decision(self, home, enabled, monkeypatch):
        loop = asyncio.new_event_loop()
        try:
            assert (
                mr.kept_memories([_episode("a")], "hi", session_key="s", loop=loop, owner_turn=True)
                is None
            )
        finally:
            loop.close()

    @pytest.mark.asyncio
    async def test_a_call_past_the_deadline_is_not_started(self, monkeypatch):
        spy = AsyncMock(return_value=_answers(("keep", 0.9)))
        monkeypatch.setattr(mr.core, "decide", spy)
        rows = [{"key": "a", "snippet": "s"}]
        assert await mr.keep_decision("hi", rows, session_key="s", deadline=0.0) is None
        spy.assert_not_awaited()

    def test_an_expired_budget_keeps_the_baseline(self, home, bg_loop, enabled, monkeypatch):
        started = threading.Event()

        async def _slow(*_a, **_k):
            started.set()
            await asyncio.sleep(5)
            return _answers(("keep", 0.9))

        monkeypatch.setattr(mr.core, "decide", _slow)
        monkeypatch.setattr(mr, "MIN_WAIT_SECS", 0.05)
        monkeypatch.setattr(mr, "MAX_WAIT_SECS", 0.05)
        assert (
            mr.kept_memories([_episode("a")], "hi", session_key="s", loop=bg_loop, owner_turn=True)
            is None
        )
        assert started.wait(10)

    def test_a_scheduling_failure_closes_the_coroutine(self, home, bg_loop, enabled, monkeypatch):
        """A coroutine that never got scheduled is closed here.

        Dropping it unscheduled emits "coroutine was never awaited" from whichever
        unrelated test later triggers the GC.
        """
        monkeypatch.setattr(mr.core, "decide", AsyncMock(return_value=_answers(("keep", 0.9))))

        def _refuse(coro, _loop):
            coro.close()
            raise RuntimeError("no")

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _refuse)
        assert (
            mr.kept_memories([_episode("a")], "hi", session_key="s", loop=bg_loop, owner_turn=True)
            is None
        )


# ── what leaves the machine ───────────────────────────────────────────────────


class TestEgress:
    def test_a_snippet_is_redacted_before_it_is_clipped(self):
        """Clipping first can halve a secret into a fragment neither pattern matches.

        The secret sits past the clip bound, so a clip-then-redact implementation
        would either send its tail or drop it silently; redact-then-clip replaces
        it whole and the placeholder is what the bound is applied to.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        raw = "x" * (mr.MAX_SNIPPET_CHARS - 4) + secret
        out = mr.scrubbed(raw, mr.MAX_SNIPPET_CHARS)
        assert secret not in out
        assert len(out) <= mr.MAX_SNIPPET_CHARS

    def test_a_snippet_is_clipped_to_the_bound(self):
        assert len(mr.scrubbed("y" * 5000, mr.MAX_SNIPPET_CHARS)) == mr.MAX_SNIPPET_CHARS

    def test_a_failing_redactor_drops_the_snippet_rather_than_sending_it(self, monkeypatch):
        """A scan that did not complete cannot clear text for the wire."""
        import kiro_crew.security as security

        monkeypatch.setattr(
            security, "redact_credentials", lambda _t: (_ for _ in ()).throw(RuntimeError("x"))
        )
        assert mr.scrubbed("anything at all", mr.MAX_SNIPPET_CHARS) == ""

    def test_the_state_carries_the_message_excerpt_and_the_candidates_only(self):
        state = mr.build_state("hello", [{"key": "a", "snippet": "s"}])
        assert set(state) == {"message", "candidates"}
        assert state["candidates"] == [{"key": "a", "snippet": "s"}]

    def test_there_is_no_history_key(self):
        """The candidates ARE the prior conversation, already ranked for this message.

        A second unranked copy would make the question worse and the egress larger,
        so the consented history ceiling is not spent here at all.
        """
        assert "history" not in mr.build_state("hello", [{"key": "a", "snippet": "s"}])

    @pytest.mark.parametrize(
        "text, expected",
        [("", 0), ("abc", 3), ("z" * (mr.MAX_MESSAGE_CHARS + 10), mr.MAX_MESSAGE_CHARS)],
    )
    def test_the_message_is_capped(self, text, expected):
        assert mr.message_chars(text) == expected

    def test_the_excerpt_the_request_carries_is_the_one_message_chars_measures(self):
        text = "y" * (mr.MAX_MESSAGE_CHARS + 500)
        state = mr.build_state(text, [{"key": "a", "snippet": "s"}])
        assert len(state["message"]) == mr.message_chars(text) == mr.MAX_MESSAGE_CHARS

    def test_a_question_id_is_an_ordinal_and_never_the_memory_id(self):
        """Question ids are dictionary keys in the request body.

        A memory id there would put a store handle on the wire for no gain: the
        caller holds the list and the POSITION is what maps an answer back.
        """
        questions = mr.build_questions([{"key": "mem-abc", "snippet": "s"}])
        assert [q.id for q in questions] == ["m0"]
        assert "mem-abc" not in json.dumps([q.prompt for q in questions])

    def test_the_menu_is_capped(self):
        rows = mr.screen_candidates([_episode(f"k{i}") for i in range(mr.MAX_CANDIDATES + 40)])
        assert len(rows) == mr.MAX_CANDIDATES

    def test_a_row_without_a_usable_key_is_not_offered(self):
        """An over-long or missing id cannot map an answer back, so it is dropped."""
        rows = mr.screen_candidates(
            [
                _episode(""),
                {"text": "no id"},
                _episode("x" * (mr.MAX_KEY_CHARS + 1)),
                _episode("ok"),
            ]
        )
        assert [row["key"] for row in rows] == ["ok"]

    def test_a_duplicate_key_is_offered_once(self):
        rows = mr.screen_candidates([_episode("a"), _episode("a")])
        assert [row["key"] for row in rows] == ["a"]

    def test_an_unoffered_row_stays_in_the_block(self, home, bg_loop, enabled, monkeypatch):
        """A row the screen dropped was never offered, so nobody may drop it.

        The earlier version of this assertion pinned the opposite and was WRONG: it
        filtered the injected set down to the offered rows, so a candidate past the
        cap vanished from the prompt because it was absent from the answer -- which is
        indistinguishable from "Jev said no" in a plain filter and is not what
        happened. Only an offered row may be removed, and then only by an answer
        naming it.

        The id-less row here is the screen's own refusal case: it cannot be named on
        the wire, so it is not offered, so it survives untouched.
        """
        rows = [_episode(""), _episode("a")]
        kept = _kept(rows, loop=bg_loop, monkeypatch=monkeypatch, answers=_answers(("keep", 0.9)))
        assert [row["id"] for row in kept] == ["", "a"]

    def test_a_candidate_past_the_cap_is_never_dropped(self, home, bg_loop, enabled, monkeypatch):
        """The realistic shape of the same defect: more rows than the menu holds.

        Every offered row is answered ``drop``, so the pick is empty -- and the rows
        past :data:`MAX_CANDIDATES` must still be in the block, because the question
        never mentioned them.
        """
        rows = [_episode(f"k{i}") for i in range(mr.MAX_CANDIDATES + 3)]
        answers = _answers(*[("drop", 0.9)] * mr.MAX_CANDIDATES)
        kept = _kept(rows, loop=bg_loop, monkeypatch=monkeypatch, answers=answers)
        assert [row["id"] for row in kept] == [
            f"k{i}" for i in range(mr.MAX_CANDIDATES, mr.MAX_CANDIDATES + 3)
        ]

    def test_the_baseline_arm_is_the_whole_block_not_the_menu(
        self, home, bg_loop, enabled, monkeypatch
    ):
        """The record has to be able to show what the block would have carried.

        A baseline narrowed to the offered rows understates it by exactly the rows the
        screen dropped, and then the strip's "similarity: N" is a count of the menu
        rather than of the recall.
        """
        rows = [_episode("a"), _episode("b"), _episode("c")]
        # Only two are offered: `c` carries an over-long id the screen refuses.
        rows[2]["id"] = "x" * (mr.MAX_KEY_CHARS + 1)
        _kept(
            rows,
            loop=bg_loop,
            monkeypatch=monkeypatch,
            answers=_answers(("keep", 0.9), ("drop", 0.9)),
        )
        outcome = _rows(home)[-1]
        assert outcome["baseline_keys"] == ["a", "b", "x" * (mr.MAX_KEY_CHARS + 1)]
        assert outcome["jev_keys"] == ["a", "x" * (mr.MAX_KEY_CHARS + 1)]
        assert outcome["candidates"] == 2, "the MENU size, which is what was asked"

    def test_an_id_less_row_is_named_in_neither_list(self, home, bg_loop, enabled, monkeypatch):
        """Naming it as ``""`` would cost the whole receipt.

        The strip's reader refuses a key list carrying an empty name, so one unnamed
        row would make the record fail validation and draw nothing. It stays in both
        ARMS -- `chars_saved` measures rows, not keys -- and an episode without its
        own primary key is a store defect rather than a state this seam produces.
        """
        rows = [_episode(""), _episode("a")]
        _kept(rows, loop=bg_loop, monkeypatch=monkeypatch, answers=_answers(("keep", 0.9)))
        outcome = _rows(home)[-1]
        assert outcome["baseline_keys"] == ["a"]
        assert outcome["jev_keys"] == ["a"]
        assert "" not in outcome["baseline_keys"]


# ── the gate still owns the refusals ──────────────────────────────────────────


class TestTheGateOwnsRefusals:
    def test_the_point_name_is_one_the_gate_admits(self):
        """A name the gate does not know is refused on its first line.

        Asserted against the REAL tuple rather than the point's own constant: the
        two live in different modules and only the gate's copy is consulted.
        """
        assert mr.POINT in gate.DECISION_POINT_NAMES

    def test_the_point_needs_the_recalled_memory_scope_and_not_the_tool_one(self):
        """Its scope is ``memory_text``, and it is the memory reader that answers.

        Held against the gate's own table rather than the point's constants: the two
        live in different modules and only the gate's copy is consulted. An entry
        naming the TOOL reader would let a tool-argument consent stand for memory
        text, which is the exact widening the scope exists to prevent.
        """
        reader_name, category = gate._POINT_SCOPES[mr.POINT]
        assert reader_name == "consented_memory_text"
        assert getattr(consent, reader_name) is consent.consented_memory_text
        assert reader_name != "consented_tool_args"
        assert "recalled memories" in category
        assert mr.POINT in gate.POINTS_NEEDING_MEMORY_TEXT

    @pytest.mark.asyncio
    async def test_a_credential_in_a_snippet_refuses_the_whole_request(self, home, monkeypatch):
        """The gate's own scrub, over the state this point builds.

        A snippet is redacted first, so this is the second line of defence: a
        spelling ``scrubbed`` misses refuses the request rather than sending it.
        """
        state = mr.build_state("hi", [{"key": "a", "snippet": "AKIAIOSFODNN7EXAMPLE"}])
        assert gate.scrub_reason(state, mr.build_questions([{"key": "a"}])) == (
            gate.ERROR_SCRUBBED_CREDENTIAL
        )


# ── both arms on the record ───────────────────────────────────────────────────


class TestTheRecord:
    def test_the_outcome_row_names_both_arms(self, home, bg_loop, enabled, monkeypatch):
        rows = [_episode("a", "aaa"), _episode("b", "bbbbb")]
        _kept(
            rows,
            loop=bg_loop,
            monkeypatch=monkeypatch,
            answers=_answers(("keep", 0.9), ("drop", 0.8)),
        )
        outcome = _rows(home)[-1]
        assert outcome["point"] == mr.POINT
        assert outcome["baseline_keys"] == ["a", "b"]
        assert outcome["jev_keys"] == ["a"]
        assert outcome["agree"] is False
        assert outcome["candidates"] == 2
        # JEV'S removal, measured on the same rows before redaction and before
        # bounding -- see the accounting section in the point's module docstring.
        assert outcome["chars_saved"] == len("bbbbb")
        assert outcome["baseline_chars"] == len("aaa") + len("bbbbb")
        assert outcome["jev_chars"] == len("aaa")
        assert outcome["baseline_chars"] - outcome["jev_chars"] == outcome["chars_saved"]
        assert outcome["p"] == pytest.approx((0.9 + 0.2) / 2)

    def test_agree_is_set_equality(self):
        """Both arms are selections; an order difference is not a disagreement."""
        rows = [_episode("a"), _episode("b")]
        outcome = mr.build_outcome(
            baseline=rows, injected=list(reversed(rows)), trace={"turn_id": "t"}
        )
        assert outcome["agree"] is True

    def test_the_store_lands_on_the_outcome(self):
        """The recall's store travels onto the record so the popover resolves ids in it.

        A member (V2) recall's ids are meaningless in the default store, so the store
        the recall ran against is what the strip's id popover has to look them up in.
        """
        outcome = mr.build_outcome(
            baseline=[], injected=[], trace={"turn_id": "t"}, store="member-bob"
        )
        assert outcome["store"] == "member-bob"
        # An older producer, or a caller that does not know it, leaves it None -- which
        # the strip's reader reads as the default store.
        assert mr.build_outcome(baseline=[], injected=[], trace={"turn_id": "t"})["store"] is None

    def test_the_lists_are_named_for_what_they_hold(self):
        """``baseline_keys``/``jev_keys``, never ``baseline``/``jev``.

        Those two names are what the SKILL strip's reader requires, so a memory
        record spelling them would render as a skill selection holding memory ids.
        """
        outcome = mr.build_outcome(baseline=[], injected=[], trace={"turn_id": "t"})
        assert "baseline" not in outcome and "jev" not in outcome
        assert {"baseline_keys", "jev_keys"} <= set(outcome)

    def test_chars_saved_never_goes_negative(self):
        """The point only ever removes, so a negative saving would be a bug shown."""
        outcome = mr.build_outcome(
            baseline=[_episode("a", "x")], injected=[_episode("a", "x" * 50)], trace={}
        )
        assert outcome["chars_saved"] == 0

    def test_the_saving_is_measured_against_the_blocks_own_clip(self):
        """One number in two places would make the saving about a block nobody built."""
        long_text = "x" * (EPISODIC_BLOCK_TEXT_CHARS + 500)
        assert mr.injected_chars([_episode("a", long_text)]) == EPISODIC_BLOCK_TEXT_CHARS

    def test_a_refused_turn_writes_no_outcome_row(self, home, bg_loop, enabled, monkeypatch):
        """``agree`` against an answer that never arrived compares one arm with nothing."""
        monkeypatch.setattr(mr.core, "decide", AsyncMock(return_value=None))
        mr.kept_memories([_episode("a")], "hi", session_key="s", loop=bg_loop, owner_turn=True)
        assert [row for row in _rows(home) if "baseline_keys" in row] == []

    def test_the_outcome_is_published_for_the_strip(self, home, bg_loop, enabled, monkeypatch):
        seen: list[tuple[str, dict]] = []
        monkeypatch.setattr(_outcomes, "publish", lambda key, row: seen.append((key, row)))
        _kept(
            [_episode("a")], loop=bg_loop, monkeypatch=monkeypatch, answers=_answers(("keep", 0.9))
        )
        assert [key for key, _ in seen] == ["s"]
        assert seen[0][1]["jev_keys"] == ["a"]

    def test_an_unwritten_row_is_not_published(self, home, monkeypatch):
        """A strip whose durable row was refused describes a decision no verdict fits."""
        seen: list[Any] = []
        monkeypatch.setattr(_outcomes, "publish", lambda key, row: seen.append(row))
        monkeypatch.setattr(mr._log, "append", lambda _row: False)
        assert _commit("s", [({"turn_id": "t"}, 1)], []) is False
        assert seen == []

    def test_an_empty_pending_list_commits_nothing(self, home, monkeypatch):
        """The shape every refusal leaves behind: nothing held, so nothing written."""
        seen: list[Any] = []
        monkeypatch.setattr(_outcomes, "publish", lambda key, row: seen.append(row))
        assert _commit("s", [], []) is False
        assert seen == []
        assert _rows(home) == []

    def test_only_the_last_held_outcome_is_committed(self, home):
        """A store that discarded an attempt must not leave its row behind.

        ``_recall_once`` can run twice for one tool call -- it validates the embedding
        generation after applying the hook, and ``recall`` retries a moved generation --
        so the list can hold two entries and only the one whose result was returned may
        be written.
        """
        first = {"turn_id": "discarded", "baseline_keys": ["a"], "jev_keys": ["a"]}
        second = {"turn_id": "returned", "baseline_keys": ["a"], "jev_keys": []}
        assert _commit("s", [(first, 1), (second, 2)], []) is True
        written = [row for row in _rows(home) if "baseline_keys" in row]
        assert [row["turn_id"] for row in written] == ["returned"]

    def test_a_missing_outcomes_module_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(mr, "OUTCOMES_MODULE", "kiro_crew.this_module_does_not_exist")
        assert mr._publish("s", {"turn_id": "t"}) is False

    def test_a_raising_publisher_cannot_cost_the_turn(self, monkeypatch):
        monkeypatch.setattr(
            _outcomes, "publish", lambda *_a: (_ for _ in ()).throw(RuntimeError("x"))
        )
        assert mr._publish("s", {"turn_id": "t"}) is False

    @pytest.mark.asyncio
    async def test_a_surface_gone_by_the_call_sends_nothing(self):
        """The guard sits AT the egress, so the last word belongs to the last moment.

        `core.decide` is where snippets of the owner's own remembered notes leave the
        machine. Every earlier read of the surface question -- the route's, before the
        search is dispatched, and `kept_memories`', before the menu is built -- happens
        while the answer can still change: the owner closes the tab, the slot leaves the
        published set, and a request already assembled would still go out.

        So this drives `keep_decision` directly with a predicate that is false by the
        time it is read, and asserts the call never happens. Nothing is stubbed about
        the decision itself -- `core.decide` is left real and would raise if reached,
        which is the point: a guard that let it through could not pass this quietly.
        """
        sent: list[Any] = []

        async def _decide(*args: Any, **kwargs: Any):
            sent.append((args, kwargs))
            raise AssertionError("the surface was gone; nothing may be sent")

        trace: dict[str, Any] = {}
        with patch.object(mr.core, "decide", _decide):
            keys = await mr.keep_decision(
                "where do we deploy the signer",
                [{"key": "a", "snippet": "the signer deploys to us-west-2"}],
                session_key="s",
                turn_id="t",
                still_watched=lambda: False,
                trace=trace,
            )

        assert keys is None, "a refusal returns the baseline"
        assert sent == [], "recalled memory left the machine for a surface that was gone"

    @pytest.mark.asyncio
    async def test_a_live_surface_still_reaches_the_call(self):
        """The other direction, or the guard above proves only that nothing works."""
        asked: list[Any] = []

        async def _decide(_point, _state, questions, **_kwargs):
            asked.append(questions)
            return {mr.question_id(0): Answer(id=mr.question_id(0), value="keep", p=0.9)}

        with patch.object(mr.core, "decide", _decide):
            keys = await mr.keep_decision(
                "where do we deploy the signer",
                [{"key": "a", "snippet": "the signer deploys to us-west-2"}],
                session_key="s",
                turn_id="t",
                still_watched=lambda: True,
                trace={},
            )

        assert asked, "the call never happened, so the guard above proves nothing"
        assert keys == ["a"]

    def test_the_funnel_is_the_only_writer(self):
        """The accounting holds because there is one way in, not because callers agree.

        Three call sites leaked a receipt for a recall nobody received, each time
        because the guard sat beside the call rather than inside it. A second reachable
        writer would put that mistake back within reach, so the old public names are
        gone and the one that remains cannot be called without the finished response.
        """
        import inspect

        for gone in ("commit_outcome", "delivered_outcome", "publish_outcome"):
            assert not hasattr(mr, gone), f"{gone} is a second way to write a receipt"
        payload = inspect.signature(mr.commit_receipt).parameters["payload"]
        assert payload.default is inspect.Parameter.empty, "the payload must be required"
        assert payload.kind is inspect.Parameter.KEYWORD_ONLY

    def test_an_unfinished_payload_writes_nothing(self, home):
        """No `retrieval` means the bounding walk refused it: nothing was delivered."""
        held = [({"turn_id": "t", "baseline_keys": ["a"], "jev_keys": ["a"]}, 1)]
        refused = {"error": "Recall metadata exceeds the response budget; narrow the query."}
        assert mr.commit_receipt("s", held, payload=refused) is False
        assert [row for row in _rows(home) if "baseline_keys" in row] == []

    def test_an_abandoned_request_writes_nothing(self, home):
        """The funnel reads the deadline itself, so no call site can forget to."""
        import time as _time

        from kiro_crew.embeddings import EmbeddingWork, embedding_work

        held = [({"turn_id": "t", "baseline_keys": ["a"], "jev_keys": ["a"]}, 1)]
        previous = embedding_work.get()
        embedding_work.set(EmbeddingWork(_time.monotonic() - 1.0))
        try:
            assert _commit("s", held, [_episode("a")]) is False
        finally:
            embedding_work.set(previous)
        assert [row for row in _rows(home) if "baseline_keys" in row] == []

    def test_the_budget_drop_is_counted_beside_the_kept_list_not_folded_into_it(self, home):
        """ "Jev kept 2" and "1 did not fit" have to add up to what shipped.

        `jev_keys` is the DECISION's output. Narrowing it to the delivered set made the
        header read "Jev kept 1" beside a row saying "2 that Jev kept did not fit",
        which is arithmetic a reader cannot close.
        """
        held = [
            (
                mr.build_outcome(
                    baseline=[_episode("a", "aaa"), _episode("b", "bbb")],
                    injected=[_episode("a", "aaa"), _episode("b", "bbb")],
                    trace={"turn_id": "t"},
                ),
                1,
            )
        ]
        # Only one of the two Jev kept survives the response budget.
        assert _commit("s", held, [_episode("a", "aaa")]) is True
        row = [r for r in _rows(home) if "baseline_keys" in r][-1]
        assert row["jev_keys"] == ["a", "b"], "the kept list is the decision's own"
        assert row["bounded_omitted"] == 1
        assert len(row["jev_keys"]) - row["bounded_omitted"] == 1, "the arithmetic closes"

    def test_nothing_bounded_carries_no_bounded_field(self, home):
        """A row about a thing that did not happen is noise on every ordinary receipt."""
        held = [
            (
                mr.build_outcome(
                    baseline=[_episode("a", "aaa")],
                    injected=[_episode("a", "aaa")],
                    trace={"turn_id": "t"},
                ),
                1,
            )
        ]
        assert _commit("s", held, [_episode("a", "aaa")]) is True
        row = [r for r in _rows(home) if "baseline_keys" in r][-1]
        assert "bounded_omitted" not in row

    def test_redaction_is_not_credited_to_jev(self, home):
        """The scrubber's removals are absent from the saving by construction.

        Both arms are measured on the same rows at the same moment, before redaction,
        so a delivered row whose text the scrubber shortened cannot move the figure.
        """
        held = [
            (
                mr.build_outcome(
                    baseline=[_episode("a", "aaa"), _episode("b", "bbb")],
                    injected=[_episode("a", "aaa")],
                    trace={"turn_id": "t"},
                ),
                1,
            )
        ]
        saving = held[0][0]["chars_saved"]
        assert saving == len("bbb")
        # The delivered row is the same memory with most of its text scrubbed away.
        assert _commit("s", held, [_episode("a", "[REDACTED]")]) is True
        row = [r for r in _rows(home) if "baseline_keys" in r][-1]
        assert row["chars_saved"] == saving, "redaction moved a figure that is Jev's"


# ── the store's own hook, and its fallbacks ───────────────────────────────────


class TestTheKeepHook:
    def test_no_hook_leaves_the_similarity_result(self):
        rows = [_episode("a"), _episode("b")]
        assert _kept_episodes(rows, None) == rows

    def test_a_none_answer_leaves_the_similarity_result(self):
        rows = [_episode("a")]
        assert _kept_episodes(rows, lambda _c: None) == rows

    def test_a_raising_hook_leaves_the_similarity_result(self):
        rows = [_episode("a")]
        assert _kept_episodes(rows, lambda _c: (_ for _ in ()).throw(RuntimeError("x"))) == rows

    def test_a_non_list_answer_leaves_the_similarity_result(self):
        rows = [_episode("a")]
        assert _kept_episodes(rows, lambda _c: "nope") == rows  # type: ignore[arg-type]

    def test_a_hook_cannot_admit_a_row_the_search_did_not_rank(self):
        """The hook may REMOVE and nothing else.

        An injected block must never hold a memory this search did not rank, so an
        answer naming an unknown row is unusable rather than applied.
        """
        rows = [_episode("a")]
        assert _kept_episodes(rows, lambda _c: [_episode("planted")]) == rows

    def test_a_hook_cannot_reorder(self):
        """Ranked order is the store's; a keep/drop answer says nothing about rank."""
        rows = [_episode("a"), _episode("b")]
        assert _kept_episodes(rows, lambda c: [c[1], c[0]]) == rows

    def test_an_equal_but_distinct_row_is_not_membership(self):
        """Identity, not equality: two episodes can hold equal dicts."""
        rows = [_episode("a"), _episode("a")]
        assert _kept_episodes(rows, lambda _c: [_episode("a")]) == rows

    def test_the_hook_narrows_when_the_answer_is_a_subset(self):
        rows = [_episode("a"), _episode("b"), _episode("c")]
        assert _kept_episodes(rows, lambda c: [c[0], c[2]]) == [rows[0], rows[2]]

    def test_keep_hook_passes_the_turns_own_arguments_through(self, monkeypatch):
        seen: list[dict] = []

        def _kept_memories(
            candidates, text, *, session_key, loop, owner_turn, still_watched, pending, store
        ):
            seen.append(
                {
                    "candidates": candidates,
                    "text": text,
                    "session_key": session_key,
                    "loop": loop,
                    "owner_turn": owner_turn,
                    "still_watched": still_watched,
                    "pending": pending,
                    "store": store,
                }
            )
            return None

        monkeypatch.setattr(mr, "kept_memories", _kept_memories)
        held: list = []
        watching = object()
        hook = mr.keep_hook(
            "the message",
            session_key="s",
            loop=None,
            owner_turn=True,
            still_watched=watching,
            pending=held,
            store="member-bob",
        )
        assert hook([_episode("a")]) is None
        assert seen[0]["text"] == "the message"
        # Carried through rather than resolved here: the point re-reads it on the
        # worker thread, which is where the egress actually happens.
        assert seen[0]["still_watched"] is watching
        assert seen[0]["session_key"] == "s"
        assert seen[0]["owner_turn"] is True
        # The caller's list travels through, so the hook records nothing itself.
        assert seen[0]["pending"] is held
        # The store threads through so the outcome names the store the recall ran in.
        assert seen[0]["store"] == "member-bob"


class TestTheHookAndTheStoreAgreeOnIdentity:
    """The one coupling between the point and the store that fails SILENTLY.

    The store decides membership with ``id()``, so the point must hand back the
    same row objects it was given. A copy looks to the store like a row its own
    search never ranked, which is the store's correct fallback -- so every decision
    would be discarded and the block would stay at the similarity top-k with no
    error anywhere. Pinned from both sides.
    """

    def test_the_kept_rows_are_the_same_objects_the_caller_passed(
        self, home, bg_loop, enabled, monkeypatch
    ):
        rows = [_episode("a"), _episode("b")]
        kept = _kept(
            rows,
            loop=bg_loop,
            monkeypatch=monkeypatch,
            answers=_answers(("keep", 0.9), ("drop", 0.9)),
        )
        assert [id(row) for row in kept] == [id(rows[0])]

    def test_the_store_applies_what_the_point_hands_back(self, home, bg_loop, enabled, monkeypatch):
        """End to end through the real applier, which is where a copy would be lost."""
        rows = [_episode("a"), _episode("b")]
        monkeypatch.setattr(
            mr.core, "decide", AsyncMock(return_value=_answers(("keep", 0.9), ("drop", 0.9)))
        )
        hook = mr.keep_hook("hi", session_key="s", loop=bg_loop, owner_turn=True)
        assert [row["id"] for row in _kept_episodes(rows, hook)] == ["a"]

    def test_a_copying_point_would_lose_every_decision(self, home, bg_loop, enabled, monkeypatch):
        """Revert-verify the coupling: with rows copied, the store keeps all of them."""
        rows = [_episode("a"), _episode("b")]
        monkeypatch.setattr(
            mr.core, "decide", AsyncMock(return_value=_answers(("keep", 0.9), ("drop", 0.9)))
        )
        original = mr.surviving_rows
        with patch.object(
            mr, "surviving_rows", lambda c, s, k: [dict(row) for row in original(c, s, k)]
        ):
            hook = mr.keep_hook("hi", session_key="s", loop=bg_loop, owner_turn=True)
            assert [row["id"] for row in _kept_episodes(rows, hook)] == ["a", "b"]


# ── the mutation this point's application must not survive ────────────────────


class TestApplyIsLoadBearing:
    def test_dropping_the_filter_would_inject_every_memory(
        self, home, bg_loop, enabled, monkeypatch
    ):
        """Revert-verify, in-process: with the pick ignored, the assertion above fails.

        The mutation is the one a refactor would plausibly make -- return the
        screened rows instead of the answered subset -- and it is what the narrowing
        assertion in ``TestTheDecision`` exists to catch.
        """
        rows = [_episode("a"), _episode("b")]
        answers = _answers(("keep", 0.9), ("drop", 0.9))

        # Unmutated: the drop is applied.
        kept = _kept(rows, loop=bg_loop, monkeypatch=monkeypatch, answers=answers)
        assert [row["id"] for row in kept] == ["a"]

        # Mutated: `read_answer` hands back every offered key, which is what an
        # "apply nothing" regression looks like from the caller's side.
        with patch.object(mr, "read_answer", lambda _a, rows: [str(r["key"]) for r in rows]):
            leaked = _kept(rows, loop=bg_loop, monkeypatch=monkeypatch, answers=answers)
        assert [row["id"] for row in leaked] == ["a", "b"]


class TestTheWaitFitsInsideTheRoute:
    """The hook's ceiling has to sit under the deadline the whole request runs on.

    The point is reached through ``GET /api/memory/recall``, which is wrapped in
    ``memory_recall_deadline`` and answers ``504 memory_recall_timeout`` when
    ``executors.RECALL_TIMEOUT_SECS`` expires. The seam's promise is that a refusal costs
    an observation and never the recall, so a wait that can outlast the request breaks it
    the one way that costs the caller something: the tool answers with an error instead of
    the memories the search had already found. This is a RELATIONSHIP between two
    constants in different modules, which is exactly the kind nothing else checks.
    """

    def test_the_ceiling_is_under_the_route_deadline(self):
        from kiro_crew.executors import RECALL_TIMEOUT_SECS

        assert mr.MAX_WAIT_SECS < RECALL_TIMEOUT_SECS, (
            f"the hook may wait {mr.MAX_WAIT_SECS}s inside a request bounded at "
            f"{RECALL_TIMEOUT_SECS}s, so a slow judge answers 504 rather than the "
            "similarity result"
        )

    def test_it_leaves_room_for_the_search_itself(self):
        """Under is not enough: the recall has its own work to do inside that budget.

        The hook runs AFTER `fit`, so the embedding, the ranking and the budgeting are
        already paid for by the time it waits. A ceiling a hair under the deadline would
        be arithmetically fine and still turn every slow judge into a 504.
        """
        from kiro_crew.executors import RECALL_TIMEOUT_SECS

        headroom = RECALL_TIMEOUT_SECS - mr.MAX_WAIT_SECS
        assert headroom >= 2.0, (
            f"only {headroom}s left for the search the decision is attached to; the "
            "ceiling needs to drop, not the deadline to rise"
        )

    def test_the_clamp_applies_it(self):
        """A hand-edited provider timeout cannot raise the wait past the ceiling."""
        with patch.object(mr.core, "timeout_secs", lambda: 3600.0):
            assert mr._wait_budget() == mr.MAX_WAIT_SECS
