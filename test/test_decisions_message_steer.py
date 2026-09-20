"""``message.steer``: the two paths, the fallback, and what the question carries.

The load-bearing groups here are :class:`TestEveryRefusalKeepsTheShippedPath` --
the reason this point is safe on the path that accepts a message into a busy slot
-- and :class:`TestTheRunningTurnIsSpentOutOfTheConsentedCeiling`, which pins that
the one new kind of egress this point wants (what the agent is doing right now) is
capped by the same keystone budget prior turns are, so the shipped default of 0
sends the new message alone.

The handler wiring -- which path a ``steer: "auto"`` send actually takes -- is
``test_decisions_message_steer_apply.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew import credential_patterns as _cred
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions.points import message_steer as ms
from kiro_crew.decisions.types import Answer

#: An AWS key id assembled from the prefix list rather than written out, for the
#: reason ``test_decisions_gate`` gives: a contiguous key-shaped literal is refused
#: by the repo's own secret scanners, correctly, since neither they nor Semgrep can
#: tell a test vector from a leak.
_AWS_KEY = _cred.AWS_KEY_ID_PREFIXES.split("|")[0] + "A2B3C4D5E6F7G8H9"


@pytest.fixture
def answer(monkeypatch):
    """Install one ``decide`` answer and record what was asked. Returns a setter."""
    asked: list[dict] = []

    def _install(value, *, p=0.83):
        async def _decide(point, state, questions, **kwargs):
            asked.append({"point": point, "state": state, "questions": questions, "kwargs": kwargs})
            if value is None:
                return None
            return {ms.QUESTION_ID: Answer(id=ms.QUESTION_ID, value=value, p=p)}

        monkeypatch.setattr("kiro_crew.decisions.decide", _decide)
        return asked

    return _install


@pytest.fixture(autouse=True)
def no_turn_budget(monkeypatch):
    """The shipped ceiling: 0, so a test that wants turn text raises it itself."""
    monkeypatch.setattr("kiro_crew.decisions.history_budget_chars", lambda: 0)


@pytest.fixture
def written_rows(monkeypatch):
    """Capture every row ``record_outcome`` hands the writer; report it as written."""
    rows: list[dict] = []

    def _append(row):
        rows.append(row)
        return True

    monkeypatch.setattr(log_mod, "append", _append)
    return rows


def _rows(*pairs):
    """Transcript rows in the shape ``slot.messages`` holds them."""
    return [{"role": role, "content": content} for role, content in pairs]


class TestTheAnswerIsTheDecision:
    @pytest.mark.asyncio
    async def test_queue_comes_back_as_the_queue_choice(self, answer):
        asked = answer(ms.CHOICE_QUEUE)
        decided = await ms.steer_or_queue("and afterwards, bump the version")
        assert decided is not None
        assert decided["choice"] == ms.CHOICE_QUEUE
        assert decided["baseline"] == ms.CHOICE_STEER, "the arm the product would have taken"
        assert decided["p"] == pytest.approx(0.83)
        assert isinstance(decided["turn_id"], str) and decided["turn_id"]
        assert decided["latency_ms"] >= 0
        assert asked[0]["point"] == ms.POINT

    @pytest.mark.asyncio
    async def test_steer_comes_back_as_the_steer_choice(self, answer):
        answer(ms.CHOICE_STEER)
        decided = await ms.steer_or_queue("stop, wrong file")
        assert decided is not None and decided["choice"] == ms.CHOICE_STEER

    @pytest.mark.asyncio
    async def test_one_question_over_the_two_shipped_paths(self, answer):
        asked = answer(ms.CHOICE_STEER)
        await ms.steer_or_queue("hello")
        questions = asked[0]["questions"]
        assert len(questions) == 1, "the answer is consumed; a second question has nothing to do"
        assert questions[0].id == ms.QUESTION_ID
        assert questions[0].options == [ms.CHOICE_STEER, ms.CHOICE_QUEUE]

    @pytest.mark.asyncio
    async def test_the_turn_id_is_carried_on_the_call_row(self, answer):
        asked = answer(ms.CHOICE_QUEUE)
        decided = await ms.steer_or_queue("hello")
        assert (
            asked[0]["kwargs"]["extra"]["turn_id"] == decided["turn_id"]
        ), "the call row and the outcome row have to name the same turn"


class TestEveryRefusalKeepsTheShippedPath:
    @pytest.mark.asyncio
    async def test_a_refused_decision_is_none_not_a_choice_of_steer(self, answer):
        answer(None)
        assert (
            await ms.steer_or_queue("hello") is None
        ), "None and a steer answer are different facts: one has a receipt"

    @pytest.mark.asyncio
    async def test_an_answer_outside_the_two_options_is_none(self, monkeypatch):
        async def _decide(point, state, questions, **kwargs):
            return {ms.QUESTION_ID: Answer(id=ms.QUESTION_ID, value="interrupt", p=0.9)}

        monkeypatch.setattr("kiro_crew.decisions.decide", _decide)
        assert await ms.steer_or_queue("hello") is None

    @pytest.mark.asyncio
    async def test_a_decide_that_raises_is_none(self, monkeypatch):
        async def _decide(point, state, questions, **kwargs):
            raise RuntimeError("transport")

        monkeypatch.setattr("kiro_crew.decisions.decide", _decide)
        assert await ms.steer_or_queue("hello") is None

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, monkeypatch):
        async def _decide(point, state, questions, **kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr("kiro_crew.decisions.decide", _decide)
        with pytest.raises(asyncio.CancelledError):
            await ms.steer_or_queue("hello")

    @pytest.mark.asyncio
    async def test_an_unreadable_ceiling_still_asks_about_the_message(self, answer, monkeypatch):
        def _raise():
            raise OSError("keystone")

        monkeypatch.setattr("kiro_crew.decisions.history_budget_chars", _raise)
        asked = answer(ms.CHOICE_QUEUE)
        decided = await ms.steer_or_queue("stop")
        assert decided is not None and decided["choice"] == ms.CHOICE_QUEUE
        assert "running_turn" not in asked[0]["state"], "no turn text without a readable ceiling"


class TestTheRunningTurnIsSpentOutOfTheConsentedCeiling:
    @pytest.mark.asyncio
    async def test_at_the_shipped_ceiling_the_question_is_the_message_alone(self, answer):
        asked = answer(ms.CHOICE_STEER)
        await ms.steer_or_queue(
            "stop",
            rows=_rows(("user", "write the parser"), ("assistant", "editing parser.py")),
        )
        assert asked[0]["state"] == {
            "message": "stop"
        }, "the running turn is conversation, so 0 sends none of it"

    @pytest.mark.asyncio
    async def test_a_raised_ceiling_buys_the_request_and_the_activity(self, answer, monkeypatch):
        monkeypatch.setattr("kiro_crew.decisions.history_budget_chars", lambda: 2000)
        asked = answer(ms.CHOICE_QUEUE)
        await ms.steer_or_queue(
            "and bump the version",
            rows=_rows(
                ("user", "an older request"),
                ("assistant", "an older reply"),
                ("user", "write the parser"),
                ("assistant", "editing parser.py"),
                ("tool", "fs_write parser.py"),
            ),
        )
        state = asked[0]["state"]
        assert state["running_turn"]["message"] == "write the parser"
        assert state["running_turn"]["activity"] == "editing parser.py\nfs_write parser.py"
        assert "an older" not in str(state), "the walk stops at the running turn's own request"

    def test_the_walk_stops_at_the_running_turns_request(self):
        request, activity = ms.running_turn_excerpts(
            _rows(
                ("assistant", "from a turn that ended"),
                ("user", "the running request"),
                ("chunk", "half a sentence"),
            ),
            budget=2000,
        )
        assert request == "the running request"
        assert activity == "half a sentence"

    def test_private_reasoning_is_not_activity(self):
        _request, activity = ms.running_turn_excerpts(
            _rows(("user", "go"), ("thinking", "maybe the user meant"), ("chunk", "doing it")),
            budget=2000,
        )
        assert activity == "doing it"

    def test_a_zero_budget_yields_neither_half(self):
        assert ms.running_turn_excerpts(_rows(("user", "go"), ("assistant", "done")), budget=0) == (
            "",
            "",
        )

    def test_the_request_is_served_before_the_activity(self):
        request, activity = ms.running_turn_excerpts(
            _rows(("user", "R" * 40), ("assistant", "A" * 40)), budget=50
        )
        assert request == "R" * 40, "the shorter half, and the one the activity answers"
        assert len(activity) == 10, "the activity gets what is left of the shared budget"

    def test_the_activity_budget_spends_newest_first(self):
        _request, activity = ms.running_turn_excerpts(
            _rows(("user", ""), ("assistant", "old" * 10), ("chunk", "NEW")),
            budget=6,
        )
        assert activity.endswith("NEW"), (
            "the newest activity is what a sender is reacting to, so it is never the"
            " part a small budget drops"
        )
        assert "oldoldold" not in activity, "the oldest activity is what the budget drops"
        assert len(activity) <= 6

    def test_the_read_itself_is_bounded(self):
        many = _rows(*[("chunk", str(n)) for n in range(ms.MAX_ACTIVITY_ROWS + 20)])
        _request, activity = ms.running_turn_excerpts(many, budget=10_000)
        assert str(0) not in activity.split(
            "\n"
        ), "a long-running turn cannot turn one send into a whole-list scan"

    def test_an_unreadable_row_is_skipped_not_raised(self):
        request, activity = ms.running_turn_excerpts(
            [None, {"role": "chunk", "content": "kept"}, {"role": "chunk", "content": 7}],  # type: ignore[list-item]
            budget=100,
        )
        assert request == ""
        assert activity == "kept"

    def test_the_message_excerpt_is_capped(self, answer):
        asked = answer(ms.CHOICE_STEER)
        asyncio.run(ms.steer_or_queue("x" * (ms.MAX_MESSAGE_CHARS + 500)))
        assert len(asked[0]["state"]["message"]) == ms.MAX_MESSAGE_CHARS


class TestTheTurnTextIsRedacted:
    def test_a_credential_in_the_activity_does_not_reach_the_state(self):
        _request, activity = ms.running_turn_excerpts(
            _rows(("user", "go"), ("chunk", f"AWS_ACCESS_KEY_ID={_AWS_KEY}")), budget=2000
        )
        assert _AWS_KEY not in activity
        assert activity, "cleaned, not refused: a reply that quoted an env file still decides"

    def test_a_credential_in_the_running_request_does_not_reach_the_state(self):
        request, _activity = ms.running_turn_excerpts(
            _rows(("user", f"deploy with {_AWS_KEY}")), budget=2000
        )
        assert _AWS_KEY not in request

    def test_a_credential_across_the_clip_boundary_leaves_no_fragment(self):
        """Every clip is taken AFTER redaction, so no cut can halve a secret.

        A budget that falls inside the key is the case that matters: a clip taken
        first hands the redactor a fragment, the fragment matches no pattern, and
        the fragment is what goes on the wire. BOTH halves are covered because
        their clips run in opposite directions and so leak opposite ends of the
        key -- the request half keeps a tail, the activity rows kept a head. Driven
        through ``running_turn_excerpts`` rather than ``redacted`` alone, because
        the boundary is chosen by the caller's budget arithmetic.
        """
        # 7 + 20 + 4 characters, and a budget of 12 puts the cut eight characters
        # from the end of the key.
        budget = 12
        request, _activity = ms.running_turn_excerpts(
            _rows(("user", f"deploy {_AWS_KEY} now")), budget=budget
        )
        assert _AWS_KEY not in request
        tail_fragment = _AWS_KEY[-8:]
        assert tail_fragment not in request, (
            f"a {len(tail_fragment)}-character tail of the key survived the request "
            "clip: the cut ran before the redactor and left a fragment no pattern "
            "matches"
        )
        assert request, "cleaned, not refused: the decision stays available"
        assert len(request) <= budget

        # The same boundary on the ACTIVITY half, whose rows were clipped per row
        # before being joined: 4 + 20 + 4 characters against the same budget puts
        # the cut eight characters INTO the key.
        _empty, activity = ms.running_turn_excerpts(
            _rows(("user", ""), ("chunk", f"log {_AWS_KEY} end")), budget=budget
        )
        assert _AWS_KEY not in activity
        head_fragment = _AWS_KEY[:8]
        assert head_fragment not in activity, (
            f"a {len(head_fragment)}-character head of the key survived the activity "
            "clip: the row was cut before the redactor saw it"
        )
        assert activity, "cleaned, not refused, on this half too"
        assert len(activity) <= budget

    def test_a_redactor_that_fails_yields_nothing(self, monkeypatch):
        def _raise(_text):
            raise RuntimeError("scanner")

        monkeypatch.setattr("kiro_crew.security.redact_credentials", _raise)
        assert (
            ms.redacted("anything at all", 100) == ""
        ), "no cleaned text is the only safe answer for text about to leave the machine"


class TestTheOutcomeRow:
    def test_the_row_carries_the_choice_and_the_arm_it_replaced(self, written_rows):
        row = ms.record_outcome(
            "dashboard:chat-1",
            {"turn_id": "t1", "choice": ms.CHOICE_QUEUE, "p": 0.77, "latency_ms": 190},
        )
        assert row is not None
        assert written_rows == [row], "the row returned is the row written"
        assert row["point"] == ms.POINT
        assert row["turn_id"] == "t1"
        assert row["choice"] == ms.CHOICE_QUEUE
        assert row["baseline"] == ms.CHOICE_STEER
        assert row["p"] == pytest.approx(0.77)
        assert row["latency_ms"] == 190, "a core field, so it rides at the top level"
        assert "kind" not in row, "a decision row is told from a verdict by an absent kind"

    def test_a_refused_append_stamps_nothing(self, monkeypatch):
        monkeypatch.setattr(log_mod, "append", lambda row: False)
        assert (
            ms.record_outcome("s", {"turn_id": "t", "choice": ms.CHOICE_QUEUE, "latency_ms": 1})
            is None
        ), "the thumbs POST this turn id, so a receipt needs a row a verdict can join"

    def test_a_writer_that_raises_is_not_a_failed_send(self, monkeypatch):
        def _raise(row):
            raise OSError("full")

        monkeypatch.setattr(log_mod, "append", _raise)
        assert ms.record_outcome("s", {"turn_id": "t", "choice": "queue", "latency_ms": 1}) is None

    def test_the_point_is_one_this_build_ships(self):
        from kiro_crew.decisions.gate import DECISION_POINT_NAMES

        assert ms.POINT in DECISION_POINT_NAMES, "an absent name is refused by the gate"


class TestTheStateShape:
    def test_neither_half_known_omits_the_running_turn_key(self):
        assert ms.build_state("hello") == {"message": "hello"}

    def test_each_half_is_omitted_when_empty(self):
        assert ms.build_state("hello", activity="doing it") == {
            "message": "hello",
            "running_turn": {"activity": "doing it"},
        }

    def test_nothing_but_the_message_and_the_turn_is_sent(self):
        state = ms.build_state("hello", running_message="r", activity="a")
        assert set(state) == {"message", "running_turn"}
        assert set(state["running_turn"]) == {"message", "activity"}

    @pytest.mark.asyncio
    async def test_the_config_is_passed_through_to_the_gate(self, answer):
        asked = answer(ms.CHOICE_STEER)
        config = SimpleNamespace(decisions=None)
        await ms.steer_or_queue("hello", config=config)
        assert asked[0]["kwargs"]["config"] is config
