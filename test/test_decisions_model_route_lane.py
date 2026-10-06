"""``model.route`` on the LLM lane -- who answers the tier question, and on whose authority.

The point had one oracle: ``decide`` set ``lane = LANE_JEV`` for every point and
branched to the small model only for ``nudge.wake``. This suite pins the second
two-lane point end to end: the lane resolver against the provider knob and the
consent, the authorization matrix (provider x consent x fleet ceiling), the row
carrying which oracle answered, the one arming predicate the chat runner reads
for both arms, the row the card draws, and the wake judge's behaviour left
byte-identical.

The consent here is PATCHED rather than staged on disk, the way
``test_decisions_judge_llm`` does it, so each case states its own authority instead
of depending on the developer's own home.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kiro_crew.config.sections import (
    DecisionsConfig,
    ModelRouteJudgeConfig,
    NudgeWakeConfig,
)
from kiro_crew.decisions import gate, impl_llm
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions.points import model_route as mr
from kiro_crew.decisions.types import Answer

#: A tier map for the tests. Nothing ships one, so every case expecting a turn to
#: move supplies this. Test files are outside the tree the model-id gate reads.
TIER_MAP = {"simple": "model-a", "medium": "model-b", "complex": "model-c"}


def _config(
    *,
    provider: str = "auto",
    llm_model: str = "",
    judge_provider: str = "auto",
    model_route: dict | None = None,
) -> object:
    """A config shaped like the live snapshot, carrying only what the gate reads."""
    return SimpleNamespace(
        decisions=DecisionsConfig(
            model_route=dict(TIER_MAP) if model_route is None else model_route,
            model_route_judge=ModelRouteJudgeConfig(provider=provider, llm_model=llm_model),
            nudge_wake=NudgeWakeConfig(provider=judge_provider),
        )
    )


@pytest.fixture(autouse=True)
def _no_leaked_registration():
    """Both runner seams empty before and after, so a case states its own runner."""
    impl_llm.set_runner(None)
    impl_llm.set_runner_factory(None)
    yield
    impl_llm.set_runner(None)
    impl_llm.set_runner_factory(None)


@pytest.fixture
def no_fleet_denial(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gate, "_capability_denied", lambda key: False)


@pytest.fixture
def log_home(tmp_path, monkeypatch):
    """Isolate disk writes and expose the rows actually built, in order."""
    monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")
    built: list[dict] = []
    real = log_mod.build_row

    def _record(**kwargs):
        row = real(**kwargs)
        built.append(row)
        return row

    monkeypatch.setattr(log_mod, "build_row", _record)
    return lambda: list(built)


def _tier_response(tier: str, p: float = 0.9) -> str:
    """One well-formed LLM-lane response to the tier question."""
    return json.dumps({mr.QUESTION_ID: {"choice": tier, "probabilities": {tier: p}}})


def _install_llm(text: str) -> list[str]:
    """Register a fixture runner answering *text*; returns the prompts it saw."""
    prompts: list[str] = []

    async def _run(prompt: str) -> str:
        prompts.append(prompt)
        return text

    impl_llm.set_runner(_run)
    return prompts


def _route(**kwargs):
    return asyncio.run(mr.routed_model("please redesign the scheduler", **kwargs))


class TestTheLaneTable:
    """Two points have a lane knob; every other point is Jev, as it always was."""

    def test_both_two_lane_points_are_registered_and_no_other(self) -> None:
        assert set(gate.LANE_POINTS) == {gate.JUDGE_POINT, gate.ROUTE_POINT}
        assert gate.ROUTE_POINT == mr.POINT

    def test_a_one_lane_point_resolves_to_jev_whatever_the_consent(self) -> None:
        for point in gate.DECISION_POINT_NAMES:
            if point in gate.LANE_POINTS:
                continue
            assert (
                gate.point_lane(point, _config(provider="llm"), jev_consented=False)
                == gate.LANE_JEV
            )
            assert (
                gate.point_lane(point, _config(provider="llm"), jev_consented=True) == gate.LANE_JEV
            )

    def test_the_route_point_reads_its_own_section_not_the_judges(self) -> None:
        """The two knobs are independent: pinning one lane on one point moves the other not at all."""
        cfg = _config(provider="llm", judge_provider="jev")
        assert gate.point_lane(gate.ROUTE_POINT, cfg, jev_consented=True) == gate.LANE_LLM
        assert gate.point_lane(gate.JUDGE_POINT, cfg, jev_consented=True) == gate.LANE_JEV
        cfg = _config(provider="jev", judge_provider="llm")
        assert gate.point_lane(gate.ROUTE_POINT, cfg, jev_consented=False) == gate.LANE_JEV
        assert gate.point_lane(gate.JUDGE_POINT, cfg, jev_consented=False) == gate.LANE_LLM

    def test_judge_lane_is_the_generic_resolver_on_the_judge(self) -> None:
        for provider in ("auto", "jev", "llm"):
            for consented in (True, False):
                cfg = _config(judge_provider=provider)
                assert gate.judge_lane(cfg, jev_consented=consented) == gate.point_lane(
                    gate.JUDGE_POINT, cfg, jev_consented=consented
                )


class TestLaneSelection:
    """``decisions.model_route_judge.provider`` against consent -- the matrix.

    ``auto`` stays on Jev whether or not Jev is consented: this point's small-model
    side needs the keystone as an act of ownership, so a slot whose owner said
    ``Auto (Jev)`` and never gave it gets the Jev side's refusal, not a model nobody
    named. That is the one line where the judge's table row and this one differ.
    """

    @pytest.mark.parametrize(
        "provider,consented,expected",
        [
            ("auto", True, gate.LANE_JEV),
            ("auto", False, gate.LANE_JEV),
            ("jev", True, gate.LANE_JEV),
            ("jev", False, gate.LANE_JEV),
            ("llm", True, gate.LANE_LLM),
            ("llm", False, gate.LANE_LLM),
        ],
    )
    def test_matrix(self, provider: str, consented: bool, expected: str) -> None:
        cfg = _config(provider=provider)
        assert gate.point_lane(gate.ROUTE_POINT, cfg, jev_consented=consented) == expected

    def test_a_config_without_the_section_reads_as_auto(self) -> None:
        """An older snapshot has no ``model_route_judge``; reading it must not raise."""
        assert gate.point_lane(gate.ROUTE_POINT, object(), jev_consented=False) == gate.LANE_JEV
        assert gate.point_lane(gate.ROUTE_POINT, object(), jev_consented=True) == gate.LANE_JEV

    def test_the_judge_still_falls_to_the_small_model_under_auto(self) -> None:
        """The table row, not the resolver, is what differs between the two points."""
        assert gate.LANE_POINTS[gate.ROUTE_POINT].llm_needs_keystone is True
        assert gate.LANE_POINTS[gate.JUDGE_POINT].llm_needs_keystone is False
        cfg = _config(judge_provider="auto")
        assert gate.point_lane(gate.JUDGE_POINT, cfg, jev_consented=False) == gate.LANE_LLM


class TestAuthority:
    """Who may answer: the keystone for both lanes, read for different things.

    The Jev side reads it as CONSENT to the configured endpoint. The small-model
    side reads its ``enabled`` bit as OWNERSHIP: the lane knob lives in
    ``config.json``, a settings file that is not owner-gated, so a pin there is not
    an owner's act on its own, and the keystone is the one owner-only,
    sandbox-readonly bit the seam has.
    ``model.route`` has no scope of its own -- the message excerpt is what the main
    switch was reviewed for -- so ``_jev_armed`` must NOT read the keystone for one.
    """

    @pytest.fixture
    def keystone_on(self, monkeypatch):
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: True)

    @pytest.fixture
    def keystone_off(self, monkeypatch):
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: False)

    def test_auto_consented_is_jev_authorized(self, no_fleet_denial, keystone_on) -> None:
        cfg = _config(provider="auto")
        assert gate._lane_authority(gate.ROUTE_POINT, cfg, "s", jev_consented=True) == (
            gate.LANE_JEV,
            True,
        )

    def test_auto_unconsented_is_the_jev_lane_refused(self, no_fleet_denial, keystone_off) -> None:
        """Not substituted: the pick was named ``Auto (Jev)``."""
        cfg = _config(provider="auto")
        assert gate._lane_authority(gate.ROUTE_POINT, cfg, "s", jev_consented=False) == (
            gate.LANE_JEV,
            False,
        )

    def test_llm_with_the_keystone_on_is_authorized_without_consent(
        self, no_fleet_denial, keystone_on
    ) -> None:
        """The lane's whole premise: a machine with no Jev key still routes."""
        cfg = _config(provider="llm")
        assert gate._lane_authority(gate.ROUTE_POINT, cfg, "s", jev_consented=False) == (
            gate.LANE_LLM,
            True,
        )
        assert gate._lane_authority(gate.ROUTE_POINT, cfg, "s", jev_consented=True) == (
            gate.LANE_LLM,
            True,
        )

    def test_llm_with_the_keystone_off_is_refused(self, no_fleet_denial, keystone_off) -> None:
        """A pin an agent could have written is no act; the keystone is the owner's."""
        cfg = _config(provider="llm")
        assert gate._lane_authority(gate.ROUTE_POINT, cfg, "s", jev_consented=False) == (
            gate.LANE_LLM,
            False,
        )

    def test_llm_under_the_fleet_ceiling_is_refused(self, monkeypatch, keystone_on) -> None:
        """A fleet that withdrew the seam withdrew every lane of it, not one endpoint."""
        monkeypatch.setattr(gate, "_capability_denied", lambda key: True)
        cfg = _config(provider="llm")
        assert gate._lane_authority(gate.ROUTE_POINT, cfg, "s", jev_consented=False) == (
            gate.LANE_LLM,
            False,
        )

    def test_jev_unconsented_is_refused_not_substituted(self, no_fleet_denial, keystone_on) -> None:
        """The owner named a lane; answering from the other would be a substitution."""
        cfg = _config(provider="jev")
        assert gate._lane_authority(gate.ROUTE_POINT, cfg, "s", jev_consented=False) == (
            gate.LANE_JEV,
            False,
        )
        assert gate._lane_authority(gate.ROUTE_POINT, cfg, "s", jev_consented=True) == (
            gate.LANE_JEV,
            True,
        )

    def test_the_jev_side_needs_no_scope_and_reads_none(self, no_fleet_denial, monkeypatch) -> None:
        """No per-point scope is invented: the keystone is not even read for it."""

        def _boom():
            raise AssertionError("model.route must not read the keystone for a scope")

        monkeypatch.setattr(gate._consent, "load_state", _boom)
        cfg = _config(provider="jev")
        assert gate._lane_authority(gate.ROUTE_POINT, cfg, "s", jev_consented=True) == (
            gate.LANE_JEV,
            True,
        )

    def test_the_keystone_is_read_for_its_enabled_bit_only(self, monkeypatch) -> None:
        """Ownership, not egress: an endpoint the keystone names for Jev is not what is asked."""
        monkeypatch.setattr(gate._consent, "load_state", lambda: {"enabled": True, "endpoint": ""})
        assert gate._keystone_enabled() is True
        monkeypatch.setattr(gate._consent, "load_state", lambda: {"enabled": "true"})
        assert gate._keystone_enabled() is False

    def test_is_enabled_agrees_with_the_authority(self, no_fleet_denial, monkeypatch) -> None:
        """A hook skipping expensive state on a False must read the answer ``decide`` gives."""
        monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: True)
        assert gate.is_enabled(gate.ROUTE_POINT, config=_config(provider="llm")) is True
        assert gate.is_enabled(gate.ROUTE_POINT, config=_config(provider="jev")) is False
        assert gate.is_enabled(gate.ROUTE_POINT, config=_config(provider="auto")) is False
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: False)
        assert gate.is_enabled(gate.ROUTE_POINT, config=_config(provider="llm")) is False


class TestTheArm:
    """Both arms of ``_route_model_for_turn`` go through ONE predicate.

    The shipped install is consent off with every slot on ``auto``, and a rule that
    sent all of them to the small model would put a model call in front of every
    turn of every default install. ``route_lane`` is the narrower question, and
    ``route_armed`` holds the keystone, the fleet ceiling and the sampled share
    against it -- for the picker-armed slot too, so a withdrawn consent stops it.
    """

    @pytest.mark.parametrize(
        "provider,consented,expected",
        [
            ("auto", True, gate.LANE_JEV),
            ("auto", False, None),
            ("jev", True, gate.LANE_JEV),
            ("jev", False, None),
            ("llm", True, gate.LANE_LLM),
            ("llm", False, gate.LANE_LLM),
        ],
    )
    def test_matrix(self, provider: str, consented: bool, expected: str | None) -> None:
        assert gate.route_lane(_config(provider=provider), jev_consented=consented) == expected

    def test_the_default_install_routes_nothing(self, no_fleet_denial, monkeypatch) -> None:
        """Consent off, provider ``auto``: the case every install ships in."""
        monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: False)
        assert gate.route_armed(config=_config(provider="auto")) is False

    def test_a_pinned_small_model_needs_the_keystone_on(self, no_fleet_denial, monkeypatch) -> None:
        """The D1 case: the pin alone is not an owner's act and arms nothing."""
        monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: False)
        assert gate.route_armed(config=_config(provider="llm")) is False
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: True)
        assert gate.route_armed(config=_config(provider="llm")) is True

    def test_the_fleet_ceiling_binds_the_pinned_small_model(self, monkeypatch) -> None:
        monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: True)
        monkeypatch.setattr(gate, "_capability_denied", lambda key: True)
        assert gate.route_armed(config=_config(provider="llm")) is False

    def test_consent_still_arms_jev_as_it_always_did(self, no_fleet_denial, monkeypatch) -> None:
        monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: True)
        assert gate.route_armed(config=_config(provider="auto")) is True
        assert gate.route_armed(config=_config(provider="jev")) is True

    def test_the_sampled_share_still_binds(self, no_fleet_denial, monkeypatch) -> None:
        monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: True)
        cfg = _config(provider="llm")
        cfg.decisions.bucket = 0  # type: ignore[attr-defined]
        assert gate.route_armed(config=cfg) is False

    def test_the_chat_runner_reads_this_helper_with_the_session_key(self, monkeypatch) -> None:
        """The predicate the runner asks is the one this suite pins, not ``is_enabled``."""
        from kiro_crew.dashboard import chat_runner

        seen: list[str | None] = []
        sentinel = object()

        def _armed(*, session_key=None, config=None):
            seen.append(session_key)
            return sentinel

        monkeypatch.setattr(gate, "route_armed", _armed)
        assert chat_runner._route_armed("chat-x") is sentinel
        assert seen == ["chat-x"]

    def test_the_base_preview_probe_stays_bound_and_answers_is_enabled(self, monkeypatch) -> None:
        """``_jev_preview_on`` is a name the runner bound before the chat_turn split
        (``test_chat_runner_composition_contract`` pins every such name). The turn
        gate no longer reads it, but it stays on the runner and still answers the
        narrower preview question through ``gate.is_enabled``."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.decisions.points import model_route

        seen: list[tuple[str, str | None]] = []

        def _enabled(point, *, session_key=None, **_):
            seen.append((point, session_key))
            return True

        monkeypatch.setattr(gate, "is_enabled", _enabled)
        assert chat_runner._jev_preview_on("chat-x") is True
        assert seen == [(model_route.POINT, "chat-x")]

    @pytest.mark.parametrize("explicit", [True, False])
    def test_a_refused_arm_asks_no_oracle_on_either_arm(self, monkeypatch, explicit: bool) -> None:
        """The D2 case: a picker-armed slot stops when the predicate says no."""
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(chat_runner, "_route_armed", lambda key: False)

        def _no_baseline(*a, **k):
            raise AssertionError("a refused arm must not read the baseline or route")

        monkeypatch.setattr(chat_runner, "_jev_route_baseline", _no_baseline)
        slot = SimpleNamespace(jev_route=explicit)
        asyncio.run(
            chat_runner._route_model_for_turn(
                SimpleNamespace(), slot, object(), "hello", "chat-x", prompt="hello"
            )
        )


class TestLaneModel:
    """The LLM lane's model id comes from the POINT's own section."""

    def test_the_route_point_reads_its_own_llm_model(self) -> None:
        cfg = _config(llm_model="router-1")
        cfg.decisions.nudge_wake.llm_model = "judge-1"  # type: ignore[attr-defined]
        assert gate.lane_model(cfg, lane=gate.LANE_LLM, point=gate.ROUTE_POINT) == "router-1"
        assert gate.lane_model(cfg, lane=gate.LANE_LLM, point=gate.JUDGE_POINT) == "judge-1"

    def test_the_default_point_is_still_the_judge(self) -> None:
        """Callers written before the second lane point named no point; they meant the judge."""
        cfg = _config(llm_model="router-1")
        cfg.decisions.nudge_wake.llm_model = "judge-1"  # type: ignore[attr-defined]
        assert gate.lane_model(cfg, lane=gate.LANE_LLM) == "judge-1"

    def test_an_empty_model_inherits(self) -> None:
        assert (
            gate.lane_model(_config(), lane=gate.LANE_LLM, point=gate.ROUTE_POINT)
            == impl_llm.JUDGE_MODEL_DEFAULT
        )

    def test_the_jev_lane_is_the_provider_model_for_both_points(self) -> None:
        cfg = _config()
        assert (
            gate.lane_model(cfg, lane=gate.LANE_JEV, point=gate.ROUTE_POINT)
            == cfg.decisions.provider.model  # type: ignore[attr-defined]
        )

    def test_the_configured_model_reaches_the_factory(self, no_fleet_denial, monkeypatch) -> None:
        """The picker is only real if the id it holds arrives at the runner."""
        asked: list[str] = []

        def factory(model: str):
            asked.append(model)

            async def _run(prompt: str) -> str:
                return _tier_response("complex")

            return _run

        impl_llm.set_runner_factory(factory)
        monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: True)
        monkeypatch.setattr(gate._log, "append", lambda row: True)
        cfg = _config(provider="llm", llm_model="router-1")
        answers = asyncio.run(
            gate.decide(gate.ROUTE_POINT, {"message": "hi"}, mr.questions(), config=cfg)
        )
        assert answers is not None
        assert asked == ["router-1"]


class TestThePrompt:
    """The LLM lane builds its prompt from the SAME question ``model.route`` sends."""

    def test_the_prompt_carries_the_tier_question_and_the_asymmetry(self) -> None:
        prompt = impl_llm.render_prompt({"message": "fix the typo"}, mr.questions())
        assert f"id: {mr.QUESTION_ID}" in prompt
        assert "type: choice" in prompt
        assert f"options: {', '.join(mr.TIERS)}" in prompt
        for tier in mr.TIERS:
            assert mr.TIER_DESCRIPTIONS[tier] in prompt
        assert mr.TIER_ASYMMETRY in prompt

    @pytest.mark.parametrize("tier", list(mr.TIERS))
    def test_a_valid_tier_is_accepted_by_the_parser_and_the_gate(self, tier: str) -> None:
        answers = impl_llm.parse_answers(_tier_response(tier, 0.83), mr.questions())
        assert answers[mr.QUESTION_ID].value == tier
        assert answers[mr.QUESTION_ID].p == pytest.approx(0.83)
        assert gate._answers_are_valid(answers, mr.questions()) is True
        assert mr.read_tier(answers) == tier

    def test_an_invalid_tier_is_refused_by_the_parser(self) -> None:
        with pytest.raises(impl_llm.LlmProtocolError):
            impl_llm.parse_answers(_tier_response("trivial"), mr.questions())

    def test_a_bare_number_is_refused_for_a_three_option_question(self) -> None:
        """The two-option shorthand names no tier out of three."""
        with pytest.raises(impl_llm.LlmProtocolError):
            impl_llm.parse_answers(json.dumps({mr.QUESTION_ID: 0.7}), mr.questions())


class TestEndToEnd:
    """``routed_model`` through the real gate on the LLM lane, with a fixture runner."""

    @pytest.fixture(autouse=True)
    def _llm_lane(self, monkeypatch, no_fleet_denial, log_home):
        # No endpoint consent: the lane's whole premise. The keystone's switch is on:
        # the owner's act the small-model side needs.
        monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: True)
        monkeypatch.setattr(gate, "_snapshot", lambda: _config(provider="llm"))
        self.rows = log_home

    def test_a_complex_answer_routes_to_the_pinned_complex_model(self) -> None:
        prompts = _install_llm(_tier_response("complex", 0.91))
        routed = _route(session_key="chat-1", current_model="model-b", advertised=["model-c"])
        assert routed is not None
        assert routed["tier"] == "complex"
        assert routed["model_chosen"] == "model-c"
        assert routed["lane"] == gate.LANE_LLM
        assert routed["p"] == pytest.approx(0.91)
        assert len(prompts) == 1
        # The message left the machine to the small model, fenced as data.
        assert "please redesign the scheduler" in prompts[0]

    def test_an_invalid_tier_keeps_the_model(self) -> None:
        _install_llm(_tier_response("trivial"))
        assert _route(session_key="chat-1", advertised=["model-c"]) is None
        rows = self.rows()
        assert [row["error"] for row in rows] == [gate.ERROR_PROVIDER]
        # The gate wrote this refusal, and it says which oracle refused.
        assert [row["lane"] for row in rows] == [gate.LANE_LLM]

    def test_prose_around_the_object_keeps_the_model(self) -> None:
        _install_llm("Sure! " + _tier_response("complex"))
        assert _route(session_key="chat-1", advertised=["model-c"]) is None

    def test_no_runner_keeps_the_model(self) -> None:
        assert _route(session_key="chat-1", advertised=["model-c"]) is None
        rows = self.rows()
        assert [row["error"] for row in rows] == [gate.ERROR_PROVIDER]
        assert [row["lane"] for row in rows] == [gate.LANE_LLM]

    def test_the_gates_answered_row_carries_the_lane(self) -> None:
        """Not only the point's outcome row: the gate's own call row names the oracle."""
        _install_llm(_tier_response("complex"))
        assert _route(session_key="chat-1", current_model="model-b", advertised=["model-c"])
        call_rows = [
            row for row in self.rows() if row["point"] == mr.POINT and not row.get("error")
        ]
        assert call_rows and all(row["lane"] == gate.LANE_LLM for row in call_rows)

    def test_a_timeout_row_carries_the_lane(self, monkeypatch) -> None:
        monkeypatch.setattr(gate, "timeout_secs", lambda *a, **k: 0.01)

        async def _slow(prompt: str) -> str:
            await asyncio.sleep(1)
            return _tier_response("complex")

        impl_llm.set_runner(_slow)
        assert _route(session_key="chat-1", advertised=["model-c"]) is None
        rows = self.rows()
        assert [row["error"] for row in rows] == [gate.ERROR_TIMEOUT]
        assert [row["lane"] for row in rows] == [gate.LANE_LLM]

    def test_a_pinned_small_model_with_the_keystone_off_asks_nothing(self, monkeypatch) -> None:
        """The D1 case end to end: the pin alone is no owner's act; no keystone, no oracle call."""
        monkeypatch.setattr(gate, "_keystone_enabled", lambda: False)
        prompts = _install_llm(_tier_response("complex"))
        assert _route(session_key="chat-1", current_model="model-b", advertised=["model-c"]) is None
        assert prompts == []
        assert self.rows() == []

    def test_the_outcome_row_and_the_strip_carry_the_lane(self, monkeypatch) -> None:
        _install_llm(_tier_response("complex"))
        routed = _route(session_key="chat-1", current_model="model-b", advertised=["model-c"])
        assert routed is not None
        published: list[dict] = []
        monkeypatch.setattr(mr, "publish_outcome", lambda key, row: published.append(row) or True)
        assert mr.record_outcome("chat-1", routed) is True
        outcome = self.rows()[-1]
        assert outcome["point"] == mr.POINT
        assert outcome["lane"] == gate.LANE_LLM
        assert published == [outcome]

    def test_an_unusable_pin_row_carries_the_lane_too(self) -> None:
        _install_llm(_tier_response("complex"))
        assert _route(session_key="chat-1", advertised=["model-z"]) is None
        error_rows = [row for row in self.rows() if row.get("error") == mr.ERROR_UNKNOWN_MODEL]
        assert len(error_rows) == 1
        assert error_rows[0]["lane"] == gate.LANE_LLM


class TestTheJevRowStillCarriesTheLane:
    """A Jev answer says so on its row, so an operator tells the two apart."""

    def test_a_jev_answer_is_labelled_jev(self, no_fleet_denial, monkeypatch, log_home) -> None:
        import kiro_crew.decisions.impl_jev as impl_mod

        class _Oracle:
            async def ask(self, state, questions):
                return {q.id: Answer(id=q.id, value="complex", p=0.9) for q in questions}

        monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: _Oracle())
        monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: True)
        monkeypatch.setattr(gate, "_snapshot", lambda: _config(provider="auto"))
        routed = _route(session_key="chat-1", advertised=["model-c"])
        assert routed is not None
        assert routed["lane"] == gate.LANE_JEV
        assert mr.build_outcome(routed)["lane"] == gate.LANE_JEV

    def test_a_row_with_no_lane_reported_carries_none(self) -> None:
        """A build whose gate predates the receipt field writes no ``lane`` key."""
        assert "lane" not in mr.build_outcome({"turn_id": "t", "tier": "simple"})


class TestTheCardsRow:
    """The ``model.route`` row names the lane that RUNS, and agrees with the oracle.

    The keystone *state* handed to ``_points`` is the same dict the gate reads:
    ``permits`` is its consent read against the endpoint, ``enabled`` its owner bit.
    A small-model row is ``active`` only with that bit on, as the gate refuses
    without it, so the word on the card and the oracle that answers are one read.
    """

    def _rows(
        self,
        monkeypatch,
        *,
        provider: str,
        permits: bool,
        runner: bool = True,
        denied=False,
        enabled: bool | None = None,
    ):
        from kiro_crew.dashboard.handlers import decisions as handlers

        monkeypatch.setattr(handlers, "_sampling_admits_anybody", lambda: True)
        monkeypatch.setattr(
            handlers,
            "_route_lane",
            lambda armed, p=provider: gate.route_lane(_config(provider=p), jev_consented=armed),
        )
        monkeypatch.setattr(handlers, "_llm_lane_available", lambda: runner)
        # A consenting keystone is an enabled one; an owner may also switch it on
        # without a Jev endpoint, which is the small-model lane's own case.
        state = {"enabled": permits if enabled is None else enabled}
        return {row["id"]: row for row in handlers._points(state, permits=permits, denied=denied)}

    def test_a_pinned_small_model_is_active_with_no_consent(self, monkeypatch) -> None:
        """No Jev consent, keystone switched on: the lane's whole premise, on the card."""
        from kiro_crew.dashboard.handlers import decisions as handlers

        row = self._rows(monkeypatch, provider="llm", permits=False, enabled=True)[gate.ROUTE_POINT]
        assert row["status"] == handlers._POINT_ACTIVE
        assert row["lane"] == gate.LANE_LLM

    def test_a_pinned_small_model_with_the_keystone_off_is_off(self, monkeypatch) -> None:
        """The D1 case on the card: the pin alone reads ``off``, naming the lane it would run."""
        from kiro_crew.dashboard.handlers import decisions as handlers

        row = self._rows(monkeypatch, provider="llm", permits=False, enabled=False)[
            gate.ROUTE_POINT
        ]
        assert row["status"] == handlers._POINT_OFF
        assert row["lane"] == gate.LANE_LLM

    def test_a_pinned_small_model_never_reads_needs_your_ok(self, monkeypatch) -> None:
        """The chip must not send a reader to a consent the lane does not use."""
        from kiro_crew.dashboard.handlers import decisions as handlers

        for permits in (True, False):
            row = self._rows(monkeypatch, provider="llm", permits=permits, enabled=True)[
                gate.ROUTE_POINT
            ]
            assert row["status"] != handlers._POINT_NEEDS_SCOPE

    def test_a_pinned_small_model_with_no_runner_is_off(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import decisions as handlers

        row = self._rows(monkeypatch, provider="llm", permits=False, runner=False, enabled=True)[
            gate.ROUTE_POINT
        ]
        assert row["status"] == handlers._POINT_OFF

    def test_auto_with_no_consent_is_off_and_names_no_lane(self, monkeypatch) -> None:
        """The default install: nothing routes for a slot nobody armed, so no lane runs."""
        from kiro_crew.dashboard.handlers import decisions as handlers

        row = self._rows(monkeypatch, provider="auto", permits=False)[gate.ROUTE_POINT]
        assert row["status"] == handlers._POINT_OFF
        assert "lane" not in row

    def test_auto_with_consent_is_jev_and_active(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import decisions as handlers

        row = self._rows(monkeypatch, provider="auto", permits=True)[gate.ROUTE_POINT]
        assert row["status"] == handlers._POINT_ACTIVE
        assert row["lane"] == gate.LANE_JEV

    def test_a_fleet_denial_turns_the_row_off_on_every_provider(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import decisions as handlers

        for provider in ("llm", "auto", "jev"):
            row = self._rows(monkeypatch, provider=provider, permits=True, denied=True)
            assert row[gate.ROUTE_POINT]["status"] == handlers._POINT_OFF

    def test_the_route_provider_does_not_reach_another_point(self, monkeypatch) -> None:
        """``llm`` on this point must not turn on a row that has nothing to do with it."""
        from kiro_crew.dashboard.handlers import decisions as handlers

        rows = self._rows(monkeypatch, provider="llm", permits=False, enabled=True)
        others = {name: row["status"] for name, row in rows.items() if name not in gate.LANE_POINTS}
        assert others, "the projection listed no other point, so this asserts nothing"
        assert set(others.values()) == {handlers._POINT_OFF}


class TestTheConfigSection:
    """Coercion mirrors ``NudgeWakeConfig``: normalize, never reject."""

    def test_an_unknown_provider_falls_back_to_auto(self) -> None:
        assert ModelRouteJudgeConfig.from_raw({"provider": "jevv"}).provider == "auto"
        assert ModelRouteJudgeConfig.from_raw({"provider": " LLM "}).provider == "llm"
        assert ModelRouteJudgeConfig.from_raw({"provider": 3}).provider == "auto"
        assert ModelRouteJudgeConfig.from_raw(None).provider == "auto"
        assert ModelRouteJudgeConfig.from_raw("llm").provider == "auto"

    def test_the_model_is_kept_verbatim_and_stripped(self) -> None:
        assert ModelRouteJudgeConfig.from_raw({"llm_model": " router-1 "}).llm_model == "router-1"
        assert ModelRouteJudgeConfig.from_raw({"llm_model": 7}).llm_model == ""

    def test_the_section_rides_the_decisions_parse(self) -> None:
        cfg = DecisionsConfig.from_raw({"model_route_judge": {"provider": "llm"}})
        assert cfg.model_route_judge.provider == "llm"
        assert DecisionsConfig.from_raw({}).model_route_judge == ModelRouteJudgeConfig()

    def test_the_tier_map_is_untouched_by_the_sibling(self) -> None:
        """The map keeps its type and path: hand-edited configs and the PATCH carry it."""
        cfg = DecisionsConfig.from_raw(
            {"model_route": {"complex": "model-c"}, "model_route_judge": {"provider": "llm"}}
        )
        assert cfg.model_route == {"simple": "", "medium": "", "complex": "model-c"}

    def test_both_keys_are_editable_through_the_config_route(self) -> None:
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        spec = _EDITABLE_CONFIG["decisions.model_route_judge.provider"]
        assert spec["type"] == "enum"
        assert set(spec["values"]) == {"auto", "jev", "llm"}
        model = _EDITABLE_CONFIG["decisions.model_route_judge.llm_model"]
        assert model == _EDITABLE_CONFIG["decisions.nudge_wake.llm_model"]


class TestTheJudgeIsUnchanged:
    """``nudge.wake`` reads the same answers it did before the second lane point."""

    def test_the_judge_authority_is_the_generic_one_on_its_own_point(
        self, no_fleet_denial, monkeypatch
    ) -> None:
        monkeypatch.setattr(gate._consent, "load_state", lambda: {})
        for provider in ("auto", "jev", "llm"):
            for consented in (True, False):
                cfg = _config(judge_provider=provider, provider="llm")
                assert gate._judge_authority(
                    cfg, "s", jev_consented=consented
                ) == gate._lane_authority(gate.JUDGE_POINT, cfg, "s", jev_consented=consented)

    def test_the_route_knob_does_not_move_the_judge(self, no_fleet_denial, monkeypatch) -> None:
        """Pinning the small model for routing leaves a consented judge on Jev."""
        monkeypatch.setitem(
            gate._POINT_SCOPES, gate.JUDGE_POINT, ("consented_nudge_evidence", "evidence")
        )
        monkeypatch.setattr(gate._consent, "consented_nudge_evidence", lambda s=None: True)
        monkeypatch.setattr(gate._consent, "load_state", lambda: {})
        cfg = _config(provider="llm", judge_provider="auto")
        assert gate._judge_authority(cfg, "s", jev_consented=True) == (gate.LANE_JEV, True)
