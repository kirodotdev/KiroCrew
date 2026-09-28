"""The judge's LLM lane: the strict parser, and which lane the gate picks.

Two things are pinned here, and they are the two that decide whether this feature
is safe to leave on.

The PARSER, because everything it lets through becomes a verdict about whether the
owner is told about their own work. A text model will hand back an extra key, a
missing id, a sentence, a ``NaN``; every one of those has to land on the gate's
existing failure path, which the caller turns into the behaviour it had before the
seam existed. The table below is that contract, one row per way a model can be
wrong.

The AUTHORITY, because the two lanes are authorized by different things and the
difference is the destination. The Jev lane needs the keystone in full -- consent for
the configured endpoint AND this point's ``nudge_evidence`` scope, because that scope
names a category of egress to a paid third party. The LLM lane needs neither: it
sends to the model provider the owner's sessions already send to, so
``decisions.nudge_wake.provider = llm`` is its authorization, and only the fleet
ceiling still binds it. A matrix rather than a few spot checks, because the
interesting cells are the ones where those two disagree.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.config.sections import DecisionsConfig, NudgeWakeConfig
from kiro_crew.decisions import gate, impl_llm
from kiro_crew.decisions.types import Choice

#: The judge's own questions. Every one is a ``Choice``, which is the only type the
#: shipped wire speaks (``impl_jev._to_wire`` refuses anything else), so the RFC's
#: three questions are a two-option one, a six-option one and an ordered level set.
WAKE = Choice(id="needs_owner", prompt="Does this need the owner now?", options=["wake", "quiet"])
OUTCOME = Choice(
    id="outcome",
    prompt="What state is the work in?",
    options=["nothing_new", "progress_only", "needs_action", "needs_human", "finished", "broken"],
)
URGENCY = Choice(id="urgency", prompt="How urgent?", options=["none", "next_tick_is_fine", "now"])

QUESTIONS = [WAKE, OUTCOME, URGENCY]

#: The keystone field worker D records the point's evidence scope in, and the reader
#: name derived from it. Spelled here because this suite must not depend on D's
#: branch: the tests register the scope the way the gate's own table does, so they
#: pin THIS PR's behaviour both when the scope exists and when it does not yet.
SCOPE_READER = "consented_nudge_evidence"
SCOPE_WORDS = "worker transcripts, review comments and ledger events"


def _config(nudge_wake: NudgeWakeConfig | None = None) -> object:
    """A config object shaped like the live snapshot, carrying only what the gate reads."""

    class _Cfg:
        def __init__(self) -> None:
            self.decisions = DecisionsConfig(nudge_wake=nudge_wake or NudgeWakeConfig())

    return _Cfg()


@pytest.fixture
def scope_registered(monkeypatch: pytest.MonkeyPatch):
    """Register the point's evidence scope the way worker D's PR will.

    Returns a setter for whether the scope is GRANTED, so a test can move the
    keystone answer without restaging the registration.
    """
    granted = {"value": False}
    monkeypatch.setitem(gate._POINT_SCOPES, gate.JUDGE_POINT, (SCOPE_READER, SCOPE_WORDS))
    monkeypatch.setattr(
        gate._consent, SCOPE_READER, lambda state=None: granted["value"], raising=False
    )
    monkeypatch.setattr(gate._consent, "load_state", lambda: {})

    def _grant(value: bool) -> None:
        granted["value"] = value

    return _grant


def _body(needs_owner: object = 0.8, outcome: object = None, urgency: object = None) -> str:
    """One well-formed response, with any field overridable by a test."""
    import json

    return json.dumps(
        {
            "needs_owner": needs_owner,
            "outcome": (
                outcome
                if outcome is not None
                else {"choice": "needs_action", "probabilities": {"needs_action": 0.77}}
            ),
            "urgency": (
                urgency if urgency is not None else {"choice": "now", "probabilities": {"now": 0.6}}
            ),
        }
    )


class TestParserAccepts:
    """The shapes a judge is allowed to answer in, and what they mean."""

    def test_full_object_form(self) -> None:
        answers = impl_llm.parse_answers(_body(), QUESTIONS)
        assert set(answers) == {"needs_owner", "outcome", "urgency"}
        assert answers["outcome"].value == "needs_action"
        assert answers["outcome"].p == pytest.approx(0.77)

    def test_bare_number_is_the_first_option(self) -> None:
        """A two-option question's bare number is P(first option), here P(wake)."""
        answers = impl_llm.parse_answers(_body(needs_owner=0.82), QUESTIONS)
        assert answers["needs_owner"].value == "wake"
        assert answers["needs_owner"].p == pytest.approx(0.82)

    def test_bare_number_below_half_flips_to_the_second(self) -> None:
        """Below 0.5 the answer is the OTHER option, and ``p`` is that option's."""
        answers = impl_llm.parse_answers(_body(needs_owner=0.08), QUESTIONS)
        assert answers["needs_owner"].value == "quiet"
        assert answers["needs_owner"].p == pytest.approx(0.92)

    def test_complete_distribution_summing_to_one(self) -> None:
        full = {
            "choice": "now",
            "probabilities": {"none": 0.1, "next_tick_is_fine": 0.2, "now": 0.7},
        }
        answers = impl_llm.parse_answers(_body(urgency=full), QUESTIONS)
        assert answers["urgency"].value == "now"

    def test_one_markdown_fence_is_stripped(self) -> None:
        """A fence is a formatting wrapper, not prose: what it wraps is still the object."""
        answers = impl_llm.parse_answers(f"```json\n{_body()}\n```", QUESTIONS)
        assert answers["needs_owner"].value == "wake"

    def test_optional_confidence_is_read(self) -> None:
        outcome = {"choice": "finished", "probabilities": {"finished": 0.9}, "confidence": 0.65}
        answers = impl_llm.parse_answers(_body(outcome=outcome), QUESTIONS)
        assert answers["outcome"].confidence == pytest.approx(0.65)

    @pytest.mark.parametrize("bad", ["high", 2.5, -0.1, float("nan"), "0.7"])
    def test_a_present_confidence_that_is_not_a_probability_is_refused(self, bad: object) -> None:
        """Optional means ABSENT is legal, not that a malformed value reads as absent.

        Every other field here refuses what it cannot read, and a model that answered
        this one wrongly answered wrongly -- accepting it as unstated would let the
        strict protocol be failed in the one field nothing downstream would notice. A
        JSON ``null`` is not in this list: it says "no confidence", which is what an
        omitted key says, and the two are the same object once parsed.
        """
        outcome = {"choice": "finished", "probabilities": {"finished": 0.9}, "confidence": bad}
        with pytest.raises(impl_llm.LlmProtocolError):
            impl_llm.parse_answers(_body(outcome=outcome), QUESTIONS)

    def test_the_gate_re_validates_what_the_parser_returns(self) -> None:
        """Whatever this parser accepts must also pass the gate's own domain check.

        The two are separate on purpose -- an implementation could always be wrong
        -- so this is the assertion that they agree, not a duplicate of either.
        """
        answers = impl_llm.parse_answers(_body(), QUESTIONS)
        assert gate._answers_are_valid(answers, QUESTIONS) is True


class TestParserRefuses:
    """One row per way a model can be wrong. Every one of them is FALLBACK."""

    @pytest.mark.parametrize(
        "label,text",
        [
            ("extra key", _body()[:-1] + ', "volunteered": 0.5}'),
            ("missing id", '{"needs_owner": 0.8}'),
            ("prose around the object", "Here is the JSON:\n" + _body()),
            ("trailing commentary", _body() + "\n\nHope that helps!"),
            ("not json at all", "the loop looks quiet to me"),
            ("empty", "   "),
            ("a list, not an object", "[0.8, 0.2]"),
        ],
    )
    def test_shape(self, label: str, text: str) -> None:
        with pytest.raises(impl_llm.LlmProtocolError):
            impl_llm.parse_answers(text, QUESTIONS)

    @pytest.mark.parametrize(
        "label,value",
        [
            ("NaN", float("nan")),
            ("infinity", float("inf")),
            ("above one", 1.4),
            ("below zero", -0.2),
            ("a bool is not a probability", True),
            ("a string is not a probability", "0.8"),
        ],
    )
    def test_number(self, label: str, value: object) -> None:
        import json

        # ``json.dumps`` writes NaN/Infinity as bare tokens, which is exactly the
        # text a model emits, so the parser meets the real thing.
        text = json.dumps(
            {
                "needs_owner": value,
                "outcome": {"choice": "needs_action", "probabilities": {"needs_action": 0.8}},
                "urgency": {"choice": "now", "probabilities": {"now": 0.6}},
            }
        )
        with pytest.raises(impl_llm.LlmProtocolError):
            impl_llm.parse_answers(text, QUESTIONS)

    @pytest.mark.parametrize(
        "label,outcome",
        [
            ("choice outside the option set", {"choice": "nope", "probabilities": {"nope": 1.0}}),
            ("no choice at all", {"probabilities": {"needs_action": 0.9}}),
            ("no probabilities", {"choice": "needs_action"}),
            (
                "probabilities miss the chosen option",
                {"choice": "needs_action", "probabilities": {"finished": 0.9}},
            ),
            (
                "probabilities name a foreign option",
                {"choice": "needs_action", "probabilities": {"needs_action": 0.9, "zzz": 0.1}},
            ),
            (
                "a narrating key",
                {
                    "choice": "needs_action",
                    "probabilities": {"needs_action": 0.9},
                    "reasoning": "because",
                },
            ),
            (
                "a complete distribution that is not one",
                {
                    "choice": "finished",
                    "probabilities": {
                        "nothing_new": 0.9,
                        "progress_only": 0.9,
                        "needs_action": 0.9,
                        "needs_human": 0.9,
                        "finished": 0.9,
                        "broken": 0.9,
                    },
                },
            ),
        ],
    )
    def test_answer_object(self, label: str, outcome: object) -> None:
        with pytest.raises(impl_llm.LlmProtocolError):
            impl_llm.parse_answers(_body(outcome=outcome), QUESTIONS)

    def test_bare_number_is_refused_for_more_than_two_options(self) -> None:
        """A bare 0.9 against six options names no option, so it is not an answer."""
        with pytest.raises(impl_llm.LlmProtocolError):
            impl_llm.parse_answers(_body(outcome=0.9), QUESTIONS)

    def test_a_level_outside_the_declared_levels(self) -> None:
        bad = {"choice": "immediately", "probabilities": {"immediately": 1.0}}
        with pytest.raises(impl_llm.LlmProtocolError):
            impl_llm.parse_answers(_body(urgency=bad), QUESTIONS)

    def test_response_over_the_ceiling(self) -> None:
        with pytest.raises(impl_llm.LlmProtocolError):
            impl_llm.parse_answers("x" * (impl_llm._MAX_RESPONSE_CHARS + 1), QUESTIONS)


class TestNoModelTextEscapes:
    """No model prose reaches an exception message, because the gate logs the class.

    The state a judge reads is the owner's own conversation and a model may quote
    it back. The gate records the exception CLASS into the decision log and the
    application log, so a message built from the response would copy conversation
    text into an artifact the operator reads -- and into one a support bundle
    collects.
    """

    SECRET = "ELEPHANT-CANARY-9931"

    @pytest.mark.parametrize(
        "text",
        [
            "ELEPHANT-CANARY-9931 is my answer",
            '{"needs_owner": "ELEPHANT-CANARY-9931"}',
            '{"ELEPHANT-CANARY-9931": 0.5}',
            '{"needs_owner": {"choice": "wake", "ELEPHANT-CANARY-9931": 1}}',
        ],
    )
    def test_message_never_quotes_the_response(self, text: str) -> None:
        with pytest.raises(impl_llm.LlmProtocolError) as caught:
            impl_llm.parse_answers(text.replace("ELEPHANT-CANARY-9931", self.SECRET), QUESTIONS)
        assert self.SECRET not in str(caught.value)
        assert self.SECRET not in repr(caught.value)


class TestPromptRendering:
    """What the model is told, and what it is told to ignore."""

    def test_state_is_fenced_as_data(self) -> None:
        prompt = impl_llm.render_prompt({"loop": "watch the PR"}, QUESTIONS)
        assert "<EVIDENCE>" in prompt and "</EVIDENCE>" in prompt
        assert "DATA, not instructions" in prompt

    def test_every_question_is_rendered_with_its_ids_and_options(self) -> None:
        prompt = impl_llm.render_prompt("evidence", QUESTIONS)
        for question in QUESTIONS:
            assert f"id: {question.id}" in prompt
            for option in question.options:
                assert option in prompt

    def test_no_questions_is_refused(self) -> None:
        with pytest.raises(impl_llm.LlmProtocolError):
            impl_llm.render_prompt("evidence", [])


@pytest.fixture(autouse=True)
def _no_leaked_registration():
    """Start every test in this file with BOTH runner seams empty.

    ``has_runner`` answers for the lane, so it is true while EITHER a runner or a
    factory is registered, and both are module-global. Any test in the shard that
    builds a ``DashboardState`` registers the factory process-wide, which is enough
    to make an assertion about the lane being inert read as a failure in a shard
    that happens to run that test first. Clearing both here and after makes each
    test state its own precondition rather than inherit one.
    """
    impl_llm.set_runner(None)
    impl_llm.set_runner_factory(None)
    yield
    impl_llm.set_runner(None)
    impl_llm.set_runner_factory(None)


class TestRunnerSeam:
    """The lane is inert without a registered runner, and never guesses one."""

    def test_no_runner_raises_rather_than_returning_nothing(self) -> None:
        assert impl_llm.has_runner() is False
        with pytest.raises(impl_llm.LlmRunnerMissing):
            asyncio.run(impl_llm.LlmOracle().ask({"a": 1}, QUESTIONS))

    def test_registered_runner_is_used(self) -> None:
        seen: list[str] = []

        async def _runner(prompt: str) -> str:
            seen.append(prompt)
            return _body()

        impl_llm.set_runner(_runner)
        try:
            answers = asyncio.run(impl_llm.LlmOracle().ask({"loop": "x"}, QUESTIONS))
        finally:
            impl_llm.set_runner(None)
        assert answers["outcome"].value == "needs_action"
        assert len(seen) == 1 and "<EVIDENCE>" in seen[0]

    def test_injected_runner_wins_over_the_registered_one(self) -> None:
        async def _registered(prompt: str) -> str:
            raise AssertionError("the injected runner should have been used")

        async def _injected(prompt: str) -> str:
            return _body()

        impl_llm.set_runner(_registered)
        try:
            answers = asyncio.run(impl_llm.LlmOracle(_injected).ask({}, QUESTIONS))
        finally:
            impl_llm.set_runner(None)
        assert answers["needs_owner"].value == "wake"

    def test_session_runner_drops_a_model_id_that_is_not_one(self) -> None:
        """``llm_model`` is agent-writable, so an unbounded value never reaches the session."""
        asked: list[object] = []

        class _Sessions:
            async def get_or_create(self, key: str, agent: str = "", model: object = None):
                asked.append(model)
                raise RuntimeError("stop here; the model argument is what this asserts")

        runner = impl_llm.build_session_runner(_Sessions(), model="not a model id; rm -rf /")
        with pytest.raises(RuntimeError):
            asyncio.run(runner("prompt"))
        assert asked == [None]

    def test_the_inherit_word_is_not_sent_as_a_model(self) -> None:
        """``auto`` means keep the agent's own model, so no model is passed at all."""
        asked: list[object] = []

        class _Sessions:
            async def get_or_create(self, key: str, agent: str = "", model: object = None):
                asked.append(model)
                raise RuntimeError("stop here")

        runner = impl_llm.build_session_runner(_Sessions(), model=impl_llm.JUDGE_MODEL_DEFAULT)
        with pytest.raises(RuntimeError):
            asyncio.run(runner("prompt"))
        assert asked == [None]

    def test_the_response_ceiling_refuses_mid_stream(self, monkeypatch) -> None:
        """The ceiling bounds what is RETAINED, so it fires while chunks arrive.

        The parser's own check only sees a response that is already whole, so a
        runaway generation would sit in memory in full before anything refused it.
        `stream_and_collect` catches only its own transport error, so the raise from
        this callback leaves the stream rather than being swallowed.
        """
        fed: list[int] = []

        class _Provider:
            pass

        class _Sessions:
            async def get_or_create(self, key: str, agent: str = "", model: object = None):
                return _Provider(), True, False

            def release(self, key: str) -> None:
                return None

            async def destroy(self, key: str) -> None:
                return None

        chunk = "x" * (16 * 1024)

        async def _fake_collect(provider, prompt, *, approval_policy=None, on_chunk=None, **kw):
            # Ten chunks is well past the ceiling; the callback must stop it before
            # the tenth, which is what makes the bound a bound.
            for _ in range(10):
                fed.append(len(chunk))
                on_chunk(chunk)
            return chunk

        monkeypatch.setattr("kiro_crew.llm_helpers.stream_and_collect", _fake_collect)
        runner = impl_llm.build_session_runner(_Sessions(), model="")
        with pytest.raises(impl_llm.LlmProtocolError) as caught:
            asyncio.run(runner("prompt"))
        assert str(caught.value) == "response exceeded the response ceiling"
        assert sum(fed) <= impl_llm._MAX_RESPONSE_CHARS + len(chunk)

    def test_session_runner_passes_a_valid_model_id(self) -> None:
        asked: list[object] = []

        class _Sessions:
            async def get_or_create(self, key: str, agent: str = "", model: object = None):
                asked.append((agent, model))
                raise RuntimeError("stop here")

        runner = impl_llm.build_session_runner(_Sessions(), model=" claude-haiku-4.5 ")
        with pytest.raises(RuntimeError):
            asyncio.run(runner("prompt"))
        assert asked == [(impl_llm.JUDGE_AGENT_NAME, "claude-haiku-4.5")]


class TestTheRunnerIsWiredAtBoot:
    """Without this the lane is inert however well it parses.

    ``decisions/`` imports nothing above itself, so the only thing that can hand it
    a model call is the layer that owns the session manager. Pinned behaviourally
    rather than by reading the docstring's claim, because the claim and the wiring
    are in different files and only one of them is executable.
    """

    def test_constructing_the_dashboard_state_registers_a_runner(self) -> None:
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.state import DashboardState

        impl_llm.set_runner(None)
        try:
            assert not impl_llm.has_runner()
            DashboardState(
                sessions=MagicMock(),
                crons=MagicMock(
                    list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
                ),
                lessons=MagicMock(load_all=MagicMock(return_value=[])),
                start_time=0.0,
            )
            assert impl_llm.has_runner()
        finally:
            impl_llm.set_runner(None)


def handlers_mod():
    """The decisions route module, imported at call time.

    Function-local so this suite's import graph stays the decisions package: the
    dashboard handler pulls in the whole route layer, and only the row-projection
    tests below need it.
    """
    from kiro_crew.dashboard.handlers import decisions as handlers

    return handlers


class TestTheCardsRowForTheJudge:
    """The row the owner reads, which cannot be derived from the keystone alone.

    A row reporting ``off`` while the small-model lane is in fact answering is the one
    error a reader has no way to check for themselves, so the projection reads the
    provider for this point. Every other point keeps the keystone rule, which the last
    case here pins by leaving the judge's own provider out of it.
    """

    def _rows(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        provider: str,
        permits: bool,
        oracle: bool = True,
    ) -> dict:
        from kiro_crew.dashboard.handlers import decisions as handlers

        monkeypatch.setattr(handlers, "_sampling_admits_anybody", lambda: True)
        # Patch the READER, not the rule: it delegates to the real ``gate.judge_lane``
        # against a config carrying this provider, so the row under test still resolves
        # ``auto`` the way the gate does instead of against a lane this helper picked.
        monkeypatch.setattr(
            handlers,
            "_judge_lane",
            lambda armed, p=provider: gate.judge_lane(
                _config(NudgeWakeConfig(provider=p)), jev_consented=armed
            ),
        )
        monkeypatch.setattr(handlers, "_llm_lane_available", lambda: oracle)
        rows = handlers._points({}, permits=permits)
        return {row["id"]: row["status"] for row in rows}

    def test_the_llm_provider_is_active_with_no_consent_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = self._rows(monkeypatch, provider="llm", permits=False)
        assert rows[gate.JUDGE_POINT] == handlers_mod()._POINT_ACTIVE

    def test_auto_is_active_with_no_consent_when_the_small_model_can_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``auto`` resolves to the small model when Jev is not armed, so a judge answers.

        The row has to say that. This is the same defect as an ``llm`` row reading
        ``off``, reached through the provider the config ships by default.
        """
        rows = self._rows(monkeypatch, provider="auto", permits=False, oracle=True)
        assert rows[gate.JUDGE_POINT] == handlers_mod()._POINT_ACTIVE

    def test_auto_is_off_when_neither_lane_can_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No keystone and no registered runner: nothing would answer, so ``off``."""
        rows = self._rows(monkeypatch, provider="auto", permits=False, oracle=False)
        assert rows[gate.JUDGE_POINT] == handlers_mod()._POINT_OFF

    def test_the_row_carries_the_lane_and_consent_alone_does_not_arm_jev(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The card cannot derive this, which is why the row carries it.

        An owner with the switch on and the address in force, on the default ``auto``,
        is sent to the SMALL MODEL while this point has no registered scope -- the gate
        resolves ``auto`` against Jev being armed, and an unregistered scope is not
        armed. A card deriving the lane from the switch would name Jev here and print
        the generic word over a small-model judge. The lane is on the judge row only:
        it is the one row that has two.
        """
        handlers = handlers_mod()
        monkeypatch.setattr(handlers, "_sampling_admits_anybody", lambda: True)
        monkeypatch.setattr(handlers, "_llm_lane_available", lambda: True)
        monkeypatch.setattr(
            handlers,
            "_judge_lane",
            lambda armed: gate.judge_lane(
                _config(NudgeWakeConfig(provider="auto")), jev_consented=armed
            ),
        )
        rows = {row["id"]: row for row in handlers._points({}, permits=True)}
        assert rows[gate.JUDGE_POINT]["lane"] == gate.LANE_LLM
        assert rows[gate.JUDGE_POINT]["status"] == handlers._POINT_ACTIVE
        assert all("lane" not in row for name, row in rows.items() if name != gate.JUDGE_POINT)

    def test_a_pinned_jev_fail_closes_on_an_unregistered_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner named a lane whose grant this build has no registration for.

        ``gate._point_scope_granted`` answers ``False`` for a point with no registered
        scope -- the absence means nothing authorized THIS point's egress, not that
        none is needed -- so ``decide`` refuses. A row calling that ``active`` would
        report a judge the gate will not run, and the small model being available says
        nothing about the lane the owner pinned.
        """
        handlers = handlers_mod()
        rows = self._rows(monkeypatch, provider="jev", permits=False, oracle=True)
        assert rows[gate.JUDGE_POINT] == handlers._POINT_OFF
        rows = self._rows(monkeypatch, provider="jev", permits=True, oracle=False)
        assert rows[gate.JUDGE_POINT] == handlers._POINT_NEEDS_SCOPE

    def test_a_pinned_jev_is_active_once_its_scope_is_registered_and_granted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same rule in the build that registers the judge's scope.

        The registration lands with the point's own change, so this pins the row
        against the table rather than against this branch's absence of an entry: with
        the scope registered and recorded, the pinned lane is armed and the row says so.
        """
        from kiro_crew.decisions import consent

        handlers = handlers_mod()
        monkeypatch.setitem(gate.POINT_SCOPE_KEYS, gate.JUDGE_POINT, "nudge_evidence")
        monkeypatch.setattr(consent, "consented_nudge_evidence", lambda _state: True, raising=False)
        rows = self._rows(monkeypatch, provider="jev", permits=True, oracle=False)
        assert rows[gate.JUDGE_POINT] == handlers._POINT_ACTIVE

    def test_the_sampled_share_still_binds_every_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """At a share of zero no session is asked, whatever the provider says."""
        handlers = handlers_mod()
        monkeypatch.setattr(handlers, "_sampling_admits_anybody", lambda: False)
        monkeypatch.setattr(handlers, "_llm_lane_available", lambda: True)
        for provider in ("llm", "auto", "jev"):
            monkeypatch.setattr(
                handlers,
                "_judge_lane",
                lambda armed, p=provider: gate.judge_lane(
                    _config(NudgeWakeConfig(provider=p)), jev_consented=armed
                ),
            )
            rows = {row["id"]: row["status"] for row in handlers._points({}, permits=True)}
            assert rows[gate.JUDGE_POINT] == handlers._POINT_OFF

    def test_a_fleet_denial_turns_the_judge_row_off_on_every_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A managed install that withdrew the seam withdrew both lanes of it.

        The gate refuses every lane under that pin, so a row calling the judge active
        there would describe a judge that cannot answer. The denial reaches this
        projection separately from ``permits`` because this lane needs the denial
        WITHOUT the consent.
        """
        from kiro_crew.dashboard.handlers import decisions as handlers

        monkeypatch.setattr(handlers, "_sampling_admits_anybody", lambda: True)
        monkeypatch.setattr(handlers, "_llm_lane_available", lambda: True)
        for provider in ("llm", "auto", "jev"):
            monkeypatch.setattr(
                handlers,
                "_judge_lane",
                lambda armed, p=provider: gate.judge_lane(
                    _config(NudgeWakeConfig(provider=p)), jev_consented=armed
                ),
            )
            rows = {
                row["id"]: row["status"] for row in handlers._points({}, permits=True, denied=True)
            }
            assert rows[gate.JUDGE_POINT] == handlers._POINT_OFF

    def test_the_configured_model_reaches_the_call_and_inherit_sends_none(self) -> None:
        """The picker is only real if the id it holds arrives at the runner.

        A runner built once at boot captures whatever the model was then, so this
        asserts the FACTORY is asked for the configured id at decision time. The
        inherit sentinel must arrive as empty: it is this build's word for "the
        agent's own model", not an id any provider would answer to.
        """
        asked: list[str] = []

        def factory(model: str):
            asked.append(model)

            async def _run(_prompt: str) -> str:
                return _body()

            return _run

        impl_llm.set_runner(None)
        impl_llm.set_runner_factory(factory)
        try:
            assert impl_llm.has_runner() is True
            asyncio.run(impl_llm.LlmOracle(model="claude-haiku-4.5").ask({}, QUESTIONS))
            asyncio.run(impl_llm.LlmOracle(model=impl_llm.JUDGE_MODEL_DEFAULT).ask({}, QUESTIONS))
        finally:
            impl_llm.set_runner_factory(None)
        assert asked == ["claude-haiku-4.5", impl_llm.JUDGE_MODEL_DEFAULT]

    def test_a_test_registered_runner_wins_over_a_leaked_factory(self) -> None:
        """Order matters because the factory outlives the test that caused it.

        ``DashboardState`` registers the factory process-wide, so any test that
        builds one leaves it behind. A fake registered here must still be the thing
        that answers, or those tests would silently start making real calls.
        """

        async def _fake(_prompt: str) -> str:
            return _body()

        def _factory(_model: str):
            raise AssertionError("the factory must not be consulted")

        impl_llm.set_runner(_fake)
        impl_llm.set_runner_factory(_factory)
        try:
            answers = asyncio.run(impl_llm.LlmOracle(model="x").ask({}, QUESTIONS))
        finally:
            impl_llm.set_runner(None)
            impl_llm.set_runner_factory(None)
        assert set(answers) == {q.id for q in QUESTIONS}

    def test_the_judge_provider_does_not_reach_another_point(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``llm`` must not turn on a row that has nothing to do with the judge."""
        rows = self._rows(monkeypatch, provider="llm", permits=False)
        others = {name: status for name, status in rows.items() if name != gate.JUDGE_POINT}
        assert others, "the projection listed no other point, so this asserts nothing"
        assert set(others.values()) == {handlers_mod()._POINT_OFF}


class TestLaneSelection:
    """``decisions.nudge_wake.provider`` against consent -- the matrix, not spot checks."""

    @pytest.mark.parametrize(
        "provider,consented,expected",
        [
            ("auto", True, gate.LANE_JEV),
            ("auto", False, gate.LANE_LLM),
            ("jev", True, gate.LANE_JEV),
            ("jev", False, gate.LANE_JEV),
            ("llm", True, gate.LANE_LLM),
            ("llm", False, gate.LANE_LLM),
        ],
    )
    def test_matrix(self, provider: str, consented: bool, expected: str) -> None:
        cfg = _config(NudgeWakeConfig(provider=provider))
        assert gate.judge_lane(cfg, jev_consented=consented) == expected

    def test_an_unknown_provider_reads_as_auto(self) -> None:
        """A typo must not become a third lane, and must not stop the gateway."""
        cfg = _config(NudgeWakeConfig(provider="jevv"))
        assert gate.judge_lane(cfg, jev_consented=False) == gate.LANE_LLM
        assert gate.judge_lane(cfg, jev_consented=True) == gate.LANE_JEV

    def test_a_config_without_the_section_reads_as_auto(self) -> None:
        """An older snapshot has no ``nudge_wake``; reading it must not raise."""
        assert gate.judge_lane(object(), jev_consented=False) == gate.LANE_LLM

    def test_the_config_normalizes_an_unknown_provider(self) -> None:
        assert NudgeWakeConfig.from_raw({"provider": "JEV "}).provider == "jev"
        assert NudgeWakeConfig.from_raw({"provider": "nope"}).provider == "auto"
        assert NudgeWakeConfig.from_raw(None).provider == "auto"


class TestJudgeAuthority:
    """Who may answer: the provider key for the LLM lane, the whole keystone for Jev."""

    def test_llm_lane_needs_no_consent_at_all(
        self, monkeypatch: pytest.MonkeyPatch, scope_registered
    ) -> None:
        """The lane's whole premise: a machine with no Jev key still has a judge.

        ``nudge_evidence`` is a category of JEV egress, so it does not govern a lane
        that never reaches Jev. ``provider = llm`` plus the loop's own ``judge`` spec
        is the authorization.
        """
        monkeypatch.setattr(gate, "_capability_denied", lambda key: False)
        cfg = _config(NudgeWakeConfig(provider="llm"))
        scope_registered(False)
        assert gate._judge_authority(cfg, "s", jev_consented=False) == (gate.LANE_LLM, True)
        scope_registered(True)
        assert gate._judge_authority(cfg, "s", jev_consented=False) == (gate.LANE_LLM, True)

    def test_an_unregistered_scope_does_not_close_the_llm_lane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A build before worker D registers the scope still runs the small model."""
        monkeypatch.setattr(gate, "_capability_denied", lambda key: False)
        monkeypatch.setattr(gate._consent, "load_state", lambda: {})
        monkeypatch.delitem(gate._POINT_SCOPES, gate.JUDGE_POINT, raising=False)
        assert gate._judge_authority(
            _config(NudgeWakeConfig(provider="llm")), "s", jev_consented=False
        ) == (gate.LANE_LLM, True)

    def test_jev_lane_needs_the_endpoint_consent_and_the_scope(
        self, monkeypatch: pytest.MonkeyPatch, scope_registered
    ) -> None:
        """Both halves, and the scope half is read fail-closed here."""
        monkeypatch.setattr(gate, "_capability_denied", lambda key: False)
        cfg = _config(NudgeWakeConfig(provider="jev"))
        scope_registered(False)
        assert gate._judge_authority(cfg, "s", jev_consented=True) == (gate.LANE_JEV, False)
        assert gate._judge_authority(cfg, "s", jev_consented=False) == (gate.LANE_JEV, False)
        scope_registered(True)
        assert gate._judge_authority(cfg, "s", jev_consented=True) == (gate.LANE_JEV, True)

    def test_an_unregistered_scope_closes_the_jev_lane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_scope_consented`` would answer True here; the judge must not.

        That permissiveness is right for a point which sends nothing beyond what the
        main switch records. Reusing it would let Jev receive worker transcripts with
        nothing having granted that, so the judge reads the scope through
        ``_point_scope_granted``, where an absent registration is a refusal.
        """
        monkeypatch.setattr(gate, "_capability_denied", lambda key: False)
        monkeypatch.setattr(gate._consent, "load_state", lambda: {})
        monkeypatch.delitem(gate._POINT_SCOPES, gate.JUDGE_POINT, raising=False)
        assert gate._judge_authority(
            _config(NudgeWakeConfig(provider="jev")), "s", jev_consented=True
        ) == (gate.LANE_JEV, False)

    def test_auto_falls_back_to_the_llm_lane_when_the_scope_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, scope_registered
    ) -> None:
        """Consented to the endpoint, never granted this point: the small model answers.

        ``auto`` resolves against the Jev side ARMED rather than merely consented, so
        this owner gets a judge instead of a refusal.
        """
        monkeypatch.setattr(gate, "_capability_denied", lambda key: False)
        cfg = _config(NudgeWakeConfig(provider="auto"))
        scope_registered(False)
        assert gate._judge_authority(cfg, "s", jev_consented=True) == (gate.LANE_LLM, True)
        scope_registered(True)
        assert gate._judge_authority(cfg, "s", jev_consented=True) == (gate.LANE_JEV, True)

    def test_the_fleet_ceiling_binds_the_llm_lane_too(
        self, monkeypatch: pytest.MonkeyPatch, scope_registered
    ) -> None:
        """A fleet that withdrew the seam withdrew every lane of it, not one endpoint."""
        monkeypatch.setattr(gate, "_capability_denied", lambda key: True)
        scope_registered(True)
        assert gate._judge_authority(
            _config(NudgeWakeConfig(provider="llm")), "s", jev_consented=False
        ) == (gate.LANE_LLM, False)


class TestBudget:
    """The LLM lane cannot be held to a budget sized for a ~100 ms System One call."""

    def test_jev_keeps_the_configured_number(self) -> None:
        assert gate.timeout_secs(_config(), lane=gate.LANE_JEV) == pytest.approx(1.0)

    def test_llm_is_clamped_up_to_the_floor(self) -> None:
        assert gate.timeout_secs(_config(), lane=gate.LANE_LLM) == pytest.approx(
            gate._LLM_TIMEOUT_MIN_SECS
        )

    def test_llm_is_clamped_down_to_the_ceiling(self) -> None:
        cfg = _config()
        cfg.decisions.provider.timeout_ms = 900_000  # type: ignore[attr-defined]
        assert gate.timeout_secs(cfg, lane=gate.LANE_LLM) == pytest.approx(
            gate._LLM_TIMEOUT_MAX_SECS
        )

    def test_the_default_lane_is_unchanged_for_every_other_point(self) -> None:
        """Keyword-only with a jev default, so no existing caller moved."""
        assert gate.timeout_secs(_config()) == gate.timeout_secs(_config(), lane=gate.LANE_JEV)


class TestLaneModel:
    """Both lanes' model ids go through the one ``scrubbed:provider-model`` bound."""

    def test_llm_lane_uses_the_configured_id(self) -> None:
        cfg = _config(NudgeWakeConfig(llm_model="my-judge-1"))
        assert gate.lane_model(cfg, lane=gate.LANE_LLM) == "my-judge-1"

    def test_an_empty_model_resolves_to_the_inherit_word(self) -> None:
        assert gate.lane_model(_config(), lane=gate.LANE_LLM) == impl_llm.JUDGE_MODEL_DEFAULT

    def test_the_inherit_word_passes_the_scrub(self) -> None:
        """What the log records must itself be a legal model id, or every tick scrubs."""
        model = gate.lane_model(_config(), lane=gate.LANE_LLM)
        assert gate.scrub_reason({"a": 1}, QUESTIONS, model=model) is None

    def test_jev_lane_is_unchanged(self) -> None:
        cfg = _config()
        assert gate.lane_model(cfg, lane=gate.LANE_JEV) == cfg.decisions.provider.model

    def test_a_model_id_that_is_not_one_is_scrubbed_rather_than_sent(self) -> None:
        cfg = _config(NudgeWakeConfig(llm_model="not an id\nAuthorization: Bearer x"))
        model = gate.lane_model(cfg, lane=gate.LANE_LLM)
        assert gate.scrub_reason({"a": 1}, QUESTIONS, model=model) == gate.ERROR_SCRUBBED_MODEL


class TestEditableConfig:
    """Both keys are writable through the config route, and nothing else is added."""

    def test_provider_is_a_closed_enum(self) -> None:
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        spec = _EDITABLE_CONFIG["decisions.nudge_wake.provider"]
        assert spec["type"] == "enum"
        assert set(spec["values"]) == {"auto", "jev", "llm"}

    def test_llm_model_takes_the_role_model_grammar(self) -> None:
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        spec = _EDITABLE_CONFIG["decisions.nudge_wake.llm_model"]
        assert spec["type"] == "str"
        assert spec.get("validate_fn") is not None

    def test_the_endpoint_and_key_stay_uneditable(self) -> None:
        """This PR must not make the egress destination writable by the config route."""
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        assert "decisions.provider.endpoint" not in _EDITABLE_CONFIG
        assert "decisions.provider.api_key" not in _EDITABLE_CONFIG


class TestDecideThroughTheLlmLane:
    """End to end: the point name, the lane, the parse, and every failure as ``None``."""

    @pytest.fixture(autouse=True)
    def _no_endpoint_consent_no_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, scope_registered
    ) -> None:
        # No endpoint consent and the scope NOT granted -- the lane's whole premise,
        # and after the ruling neither of them is its authority. No fleet denial.
        # Patched rather than staged on disk so the test says which authority it
        # exercises instead of depending on the developer's own home.
        monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        monkeypatch.setattr(gate, "_capability_denied", lambda key: False)
        # The row write touches the log directory; the answers are what this asserts.
        monkeypatch.setattr(gate._log, "append", lambda row: True)
        scope_registered(False)
        self._grant = scope_registered

    def _decide(self, cfg: object, runner: object) -> object:
        impl_llm.set_runner(runner)  # type: ignore[arg-type]
        try:
            return asyncio.run(
                gate.decide(
                    gate.JUDGE_POINT,
                    {"loop": "watch it", "since_last_tick": []},
                    QUESTIONS,
                    session_key="chat-1",
                    config=cfg,
                )
            )
        finally:
            impl_llm.set_runner(None)

    def test_the_judge_point_is_a_known_name(self) -> None:
        """Without this the gate refuses on its own list and no lane is ever reached."""
        assert gate.JUDGE_POINT in gate.DECISION_POINT_NAMES

    def test_a_well_formed_answer_comes_back(self) -> None:
        async def _runner(prompt: str) -> str:
            return _body()

        answers = self._decide(_config(NudgeWakeConfig(provider="llm")), _runner)
        assert answers is not None
        assert answers["outcome"].value == "needs_action"

    def test_prose_returns_none_rather_than_raising(self) -> None:
        async def _runner(prompt: str) -> str:
            return "I think the loop is quiet."

        assert self._decide(_config(NudgeWakeConfig(provider="llm")), _runner) is None

    def test_a_runner_that_raises_returns_none(self) -> None:
        async def _runner(prompt: str) -> str:
            raise RuntimeError("backend down")

        assert self._decide(_config(NudgeWakeConfig(provider="llm")), _runner) is None

    def test_no_runner_returns_none(self) -> None:
        assert self._decide(_config(NudgeWakeConfig(provider="llm")), None) is None

    def test_no_consent_and_no_scope_still_answers(self) -> None:
        """Named explicitly, because this is the case the lane exists for."""

        async def _runner(prompt: str) -> str:
            return _body()

        self._grant(False)
        assert self._decide(_config(NudgeWakeConfig(provider="llm")), _runner) is not None

    def test_the_fleet_ceiling_returns_none_without_calling_a_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one authority that still stops this lane: the seam withdrawn fleet-wide."""
        called: list[int] = []

        async def _runner(prompt: str) -> str:
            called.append(1)
            return _body()

        monkeypatch.setattr(gate, "_capability_denied", lambda key: True)
        assert self._decide(_config(NudgeWakeConfig(provider="llm")), _runner) is None
        assert called == []

    def test_a_pinned_jev_lane_without_consent_calls_no_model(self) -> None:
        """The owner named a lane; answering from a different one would be a substitution."""
        called: list[int] = []

        async def _runner(prompt: str) -> str:
            called.append(1)
            return _body()

        assert self._decide(_config(NudgeWakeConfig(provider="jev")), _runner) is None
        assert called == []

    def test_every_other_point_still_needs_the_keystone(self) -> None:
        """The judge's provider key must not become a second way to arm ``skills.select``."""
        called: list[int] = []

        async def _runner(prompt: str) -> str:
            called.append(1)
            return _body()

        impl_llm.set_runner(_runner)
        try:
            answers = asyncio.run(
                gate.decide(
                    "skills.select",
                    {"message": "x"},
                    QUESTIONS,
                    session_key="chat-1",
                    config=_config(NudgeWakeConfig(provider="llm")),
                )
            )
        finally:
            impl_llm.set_runner(None)
        assert answers is None
        assert called == []

    def test_a_credential_in_the_state_is_refused_before_any_model_call(self) -> None:
        """The scrub sits above both lanes, so no transport can exist below it."""
        called: list[int] = []

        async def _runner(prompt: str) -> str:
            called.append(1)
            return _body()

        impl_llm.set_runner(_runner)
        try:
            answers = asyncio.run(
                gate.decide(
                    gate.JUDGE_POINT,
                    {"since_last_tick": [{"text": "AKIAIOSFODNN7EXAMPLE"}]},
                    QUESTIONS,
                    session_key="chat-1",
                    config=_config(NudgeWakeConfig(provider="llm")),
                )
            )
        finally:
            impl_llm.set_runner(None)
        assert answers is None
        assert called == []

    def test_is_enabled_agrees_with_decide(self) -> None:
        """The cheap hook and the real call must never disagree about authority."""

        async def _runner(prompt: str) -> str:
            return _body()

        llm = _config(NudgeWakeConfig(provider="llm"))
        self._grant(False)
        assert gate.is_enabled(gate.JUDGE_POINT, session_key="chat-1", config=llm) is True
        assert self._decide(llm, _runner) is not None

        jev = _config(NudgeWakeConfig(provider="jev"))
        assert gate.is_enabled(gate.JUDGE_POINT, session_key="chat-1", config=jev) is False
        assert self._decide(jev, _runner) is None
