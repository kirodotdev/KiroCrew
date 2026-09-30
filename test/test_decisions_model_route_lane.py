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
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.config.sections import (
    DecisionsConfig,
    ModelRouteJudgeConfig,
    NudgeWakeConfig,
)
from kiro_crew.decisions import gate, impl_llm
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions.points import model_route as mr
from kiro_crew.decisions.types import Answer, Choice

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
    # The default asking backend is kiro-cli's (``""``), the shipped install.
    kwargs.setdefault("backend", ACP_BACKEND_KIRO)
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

        def factory(model: str, _source: impl_llm.JudgeSource):
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
            gate.decide(
                gate.ROUTE_POINT,
                {"message": "hi"},
                mr.questions(),
                config=cfg,
                backend=ACP_BACKEND_KIRO,
            )
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
    def _llm_lane(self, _floor_monkeypatch, no_fleet_denial, log_home):
        # No endpoint consent: the lane's whole premise. The keystone's switch is on:
        # the owner's act the small-model side needs.
        _floor_monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        _floor_monkeypatch.setattr(gate, "_keystone_enabled", lambda: True)
        _floor_monkeypatch.setattr(gate, "_snapshot", lambda: _config(provider="llm"))
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
            # A runner that never answers inside the budget: the gate's timeout is
            # what ends this wait, so the test asserts the timeout row, not a race.
            await asyncio.Event().wait()
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

    def test_a_spawn_refused_for_a_smaller_window_writes_the_lane(self, monkeypatch) -> None:
        monkeypatch.setattr(mr, "derive_enabled", lambda config=None: True)
        monkeypatch.setattr(
            gate._consent,
            "load_state",
            lambda: {"enabled": True, gate._consent.STATE_KEY_TOOL_ARGS: True},
        )
        windows = {"model-c": 1_000_000, "model-a": 200_000}
        monkeypatch.setattr(mr, "known_window", windows.get)
        _install_llm(_tier_response("simple", 0.5))
        chosen, tier = asyncio.run(
            mr.routed_spawn_model(
                "redesign the scheduler",
                session_key="chat-parent",
                current_model="model-c",
                advertised=["model-a", "model-c"],
                backend=ACP_BACKEND_KIRO,
            )
        )
        assert (chosen, tier) == ("", "simple")
        refused = [row for row in self.rows() if row.get("error") == mr.ERROR_WINDOW_REFUSED]
        assert len(refused) == 1
        assert refused[0]["lane"] == gate.LANE_LLM

    def test_an_unusable_pin_row_carries_the_lane_too(self) -> None:
        _install_llm(_tier_response("complex"))
        assert _route(session_key="chat-1", advertised=["model-z"]) is None
        error_rows = [row for row in self.rows() if row.get("error") == mr.ERROR_UNKNOWN_MODEL]
        assert len(error_rows) == 1
        assert error_rows[0]["lane"] == gate.LANE_LLM


class _SharedRuntime:
    """A session manager whose background backend (kiro-cli) has a shared runtime."""

    def _bg_backend_supports_runtime(self) -> bool:
        return True

    def _configured_bg_backend(self) -> str:
        return ACP_BACKEND_KIRO


def _record_oneliner(monkeypatch: pytest.MonkeyPatch, text: str) -> list[dict]:
    """Replace ``run_bg_oneliner`` with a recorder answering *text*; return its calls."""
    calls: list[dict] = []

    async def _fake(sessions, prompt, **kwargs):
        calls.append(kwargs)
        return text

    monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", _fake)
    return calls


#: One tier question, enough for a call to reach the runner and parse its answer.
_TIER_QUESTION = Choice(
    id=mr.QUESTION_ID, prompt="Which tier?", options=["simple", "medium", "complex"]
)


def _register_session_factory() -> list[tuple[str, impl_llm.JudgeSource]]:
    """Register the gateway's factory shape; return the ``(model, source)`` it is asked for."""
    asked: list[tuple[str, impl_llm.JudgeSource]] = []

    def _factory(model: str, source: impl_llm.JudgeSource):
        asked.append((model, source))
        return impl_llm.build_session_runner(_SharedRuntime(), model=model, source=source)

    impl_llm.set_runner_factory(_factory)
    return asked


class TestEachPointNamesItself:
    """A call is billed, audited and warned about under the point that asked it."""

    def test_the_oracle_carries_each_points_own_source(self) -> None:
        route = gate._oracle(gate.LANE_LLM, None, "", gate.ROUTE_POINT, backend=ACP_BACKEND_KIRO)
        judge = gate._oracle(gate.LANE_LLM, None, "", gate.JUDGE_POINT)
        assert route._source == impl_llm.JudgeSource(
            "model_route_judge", "decisions.model_route_judge.llm_model", backend=ACP_BACKEND_KIRO
        )
        with pytest.raises(impl_llm.LlmBackendMismatch):
            gate._oracle(gate.LANE_LLM, None, "", gate.ROUTE_POINT)
        assert judge._source == impl_llm.WAKE_JUDGE_SOURCE
        assert judge._source.sel_source == "wake_judge"

    def test_a_routed_turn_is_billed_as_the_routing_judge(
        self, _floor_monkeypatch, no_fleet_denial, log_home, monkeypatch
    ) -> None:
        _floor_monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        _floor_monkeypatch.setattr(gate, "_keystone_enabled", lambda: True)
        _floor_monkeypatch.setattr(gate, "_snapshot", lambda: _config(provider="llm"))
        calls = _record_oneliner(monkeypatch, _tier_response("complex"))
        asked = _register_session_factory()
        routed = _route(session_key="chat-1", current_model="model-b", advertised=["model-c"])
        assert routed is not None
        assert [source.sel_source for _model, source in asked] == ["model_route_judge"]
        assert [call["sel_source"] for call in calls] == ["model_route_judge"]

    def test_the_wake_judge_is_still_billed_as_wake_judge(self, monkeypatch) -> None:
        calls = _record_oneliner(monkeypatch, _tier_response("complex"))
        asked = _register_session_factory()
        oracle = gate._oracle(gate.LANE_LLM, None, "", gate.JUDGE_POINT)
        asyncio.run(oracle.ask({}, [_TIER_QUESTION]))
        assert asked == [("", impl_llm.WAKE_JUDGE_SOURCE)]
        assert [call["sel_source"] for call in calls] == ["wake_judge"]

    def test_a_runner_built_without_a_source_bills_the_wake_judge(self, monkeypatch) -> None:
        calls = _record_oneliner(monkeypatch, _tier_response("complex"))
        asyncio.run(impl_llm.build_session_runner(_SharedRuntime(), model="")("prompt"))
        assert [call["sel_source"] for call in calls] == ["wake_judge"]

    @pytest.mark.parametrize(
        ("point", "named", "not_named"),
        [
            (
                gate.ROUTE_POINT,
                "decisions.model_route_judge.llm_model",
                "decisions.nudge_wake.llm_model",
            ),
            (
                gate.JUDGE_POINT,
                "decisions.nudge_wake.llm_model",
                "decisions.model_route_judge.llm_model",
            ),
        ],
    )
    def test_an_unusable_model_warning_names_the_asking_points_key(
        self, monkeypatch, caplog, point: str, named: str, not_named: str
    ) -> None:
        _record_oneliner(monkeypatch, _tier_response("complex"))
        _register_session_factory()
        oracle = gate._oracle(
            gate.LANE_LLM, None, "not a model id!", point, backend=ACP_BACKEND_KIRO
        )
        with caplog.at_level("WARNING", logger=impl_llm.logger.name):
            asyncio.run(oracle.ask({}, [_TIER_QUESTION]))
        messages = [rec.getMessage() for rec in caplog.records if rec.name == impl_llm.logger.name]
        assert any(named in message for message in messages)
        assert not any(not_named in message for message in messages)


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


class TestRequestScopesBindTheLlmLane:
    """A request scope names content, not a destination, so it binds the LLM lane too."""

    @pytest.fixture(autouse=True)
    def _llm_lane(self, _floor_monkeypatch, no_fleet_denial, log_home):
        # No endpoint consent, keystone switch on: the pinned small model is armed
        # for a chat turn. Only the request's own scope is in question.
        _floor_monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        _floor_monkeypatch.setattr(gate, "_keystone_enabled", lambda: True)
        self.monkeypatch = _floor_monkeypatch
        self.rows = log_home

    def _keystone(self, *, tool_args: bool) -> None:
        state = {"enabled": True, gate._consent.STATE_KEY_TOOL_ARGS: tool_args}
        self.monkeypatch.setattr(gate._consent, "load_state", lambda: dict(state))

    def _decide(self, *, scopes: tuple[str, ...]) -> tuple[object, dict]:
        receipt: dict = {}
        answers = asyncio.run(
            gate.decide(
                gate.ROUTE_POINT,
                {"message": "spawn: redesign the scheduler"},
                mr.questions(),
                session_key="chat-1",
                config=_config(provider="llm"),
                receipt=receipt,
                scopes=scopes,
                backend=ACP_BACKEND_KIRO,
            )
        )
        return answers, receipt

    def test_a_spawn_without_the_tool_args_scope_sends_nothing(self) -> None:
        self._keystone(tool_args=False)
        prompts = _install_llm(_tier_response("complex"))
        answers, receipt = self._decide(scopes=mr.SPAWN_SCOPES)
        assert answers is None
        assert prompts == []
        # The refusal names the lane it refused on, and writes no row.
        assert receipt["lane"] == gate.LANE_LLM
        assert receipt["row_written"] is False
        assert self.rows() == []

    def test_a_spawn_with_the_tool_args_scope_is_answered_on_the_llm_lane(self) -> None:
        self._keystone(tool_args=True)
        prompts = _install_llm(_tier_response("complex"))
        answers, receipt = self._decide(scopes=mr.SPAWN_SCOPES)
        assert answers is not None
        assert answers[mr.QUESTION_ID].value == "complex"
        assert receipt["lane"] == gate.LANE_LLM
        assert len(prompts) == 1
        assert "redesign the scheduler" in prompts[0]

    def test_a_chat_turn_carries_no_scope_and_reads_no_further_keystone_state(self) -> None:
        def _boom() -> dict:
            raise AssertionError("a request with no scopes reads no request scope")

        self.monkeypatch.setattr(gate._consent, "load_state", _boom)
        prompts = _install_llm(_tier_response("medium"))
        answers, receipt = self._decide(scopes=())
        assert answers is not None
        assert answers[mr.QUESTION_ID].value == "medium"
        assert receipt["lane"] == gate.LANE_LLM
        assert len(prompts) == 1


class _BackendRuntime(_SharedRuntime):
    def __init__(self, backend: str = ACP_BACKEND_KIRO) -> None:
        self.backend = backend

    def _configured_bg_backend(self) -> str:
        return self.backend


class _BgHandle:
    """A background handle whose backend is *acp_backend*; records what reached it."""

    def __init__(self, acp_backend: str | None) -> None:
        if acp_backend is not None:
            self.acp_backend = acp_backend
        self.prompts: list[str] = []
        self.models: list[str] = []
        self.destroyed = 0

    async def set_model(self, model: str) -> None:
        self.models.append(model)

    async def prompt(self, text: str, *, allow_image: bool = True):
        self.prompts.append(text)
        yield SimpleNamespace(kind=EVENT_COMPLETE, text="")

    async def destroy(self) -> None:
        self.destroyed += 1


class _FlippingRuntime(_SharedRuntime):
    """Configured on *before*; its acquisition hot-applies *after* and hands a handle on it."""

    def __init__(self, before: str, after: str) -> None:
        self.backend = before
        self.after = after
        self.handle = _BgHandle(after)

    def _configured_bg_backend(self) -> str:
        return self.backend

    async def get_bg_session(self, start_priority: object = None) -> _BgHandle:
        self.backend = self.after
        return self.handle


class _OwnSessionRuntime:
    """No shared runtime: each call opens a session of its own on *provider*."""

    def __init__(self, backend: str, provider: object) -> None:
        self.backend = backend
        self.provider = provider
        self.released: list[str] = []
        self.destroyed: list[str] = []

    def _bg_backend_supports_runtime(self) -> bool:
        return False

    def _configured_bg_backend(self) -> str:
        return self.backend

    async def get_or_create(self, key: str, agent: str = "", model: object = None):
        return self.provider, True, False

    def release(self, key: str) -> None:
        self.released.append(key)

    async def destroy(self, key: str) -> None:
        self.destroyed.append(key)


def _decide_on_llm(backend: str | None, sessions: object, floor) -> object:
    """Run ``model.route`` on the ``llm`` lane for *backend* over the runner on *sessions*."""
    floor.setattr(gate, "_consented_for", lambda *a, **k: False)
    floor.setattr(gate, "_keystone_enabled", lambda: True)

    def _factory(model: str, source: impl_llm.JudgeSource):
        return impl_llm.build_session_runner(sessions, model=model, source=source)

    impl_llm.set_runner_factory(_factory)
    return asyncio.run(
        gate.decide(
            gate.ROUTE_POINT,
            {"message": "hi"},
            mr.questions(),
            session_key="chat-1",
            config=_config(provider="llm"),
            backend=backend,
        )
    )


def _one_provider_error(rows: list[dict]) -> None:
    assert [row["lane"] for row in rows] == [gate.LANE_LLM]
    assert [row["error"] for row in rows] == [gate.ERROR_PROVIDER]


class TestTheAskingBackend:
    def test_the_kiro_backend_is_accepted(
        self, _floor_monkeypatch, no_fleet_denial, log_home, monkeypatch
    ) -> None:
        calls = _record_oneliner(monkeypatch, _tier_response("complex"))
        answers = _decide_on_llm(
            ACP_BACKEND_KIRO, _BackendRuntime(ACP_BACKEND_KIRO), _floor_monkeypatch
        )
        assert answers is not None
        assert len(calls) == 1

    def test_a_route_with_no_asking_backend_refuses(
        self, _floor_monkeypatch, no_fleet_denial, log_home, monkeypatch
    ) -> None:
        calls = _record_oneliner(monkeypatch, _tier_response("complex"))
        answers = _decide_on_llm(None, _BackendRuntime(), _floor_monkeypatch)
        assert answers is None
        assert calls == []
        _one_provider_error(log_home())

    def test_a_hot_apply_during_acquisition_refuses(
        self, _floor_monkeypatch, no_fleet_denial, log_home
    ) -> None:
        sessions = _FlippingRuntime("claude", "codex")
        answers = _decide_on_llm("claude", sessions, _floor_monkeypatch)
        assert answers is None
        assert sessions.handle.prompts == []
        assert sessions.handle.models == []
        assert sessions.handle.destroyed == 1
        _one_provider_error(log_home())

    def test_a_provider_bg_session_on_another_backend_refuses(
        self, _floor_monkeypatch, no_fleet_denial, log_home
    ) -> None:
        sessions = _FlippingRuntime("claude", "claude")
        sessions.handle = _BgHandle(None)
        sessions.handle.provider = SimpleNamespace(_client=SimpleNamespace(backend="codex"))
        answers = _decide_on_llm("claude", sessions, _floor_monkeypatch)
        assert answers is None
        assert sessions.handle.prompts == []
        assert sessions.handle.destroyed == 1
        _one_provider_error(log_home())

    def test_an_own_session_on_another_backend_refuses(
        self, _floor_monkeypatch, no_fleet_denial, log_home, monkeypatch
    ) -> None:
        streamed: list[str] = []

        async def _stream(provider, prompt, **kwargs):
            streamed.append(prompt)
            return _tier_response("complex")

        monkeypatch.setattr("kiro_crew.llm_helpers.stream_and_collect", _stream)
        provider = SimpleNamespace(_client=SimpleNamespace(backend="codex"))
        sessions = _OwnSessionRuntime("claude", provider)
        answers = _decide_on_llm("claude", sessions, _floor_monkeypatch)
        assert answers is None
        assert streamed == []
        assert len(sessions.released) == 1
        assert sessions.destroyed == sessions.released
        _one_provider_error(log_home())

    def test_a_matching_acquired_backend_sends_once(
        self, _floor_monkeypatch, no_fleet_denial, log_home, monkeypatch
    ) -> None:
        streamed: list[str] = []

        async def _stream(provider, prompt, **kwargs):
            streamed.append(prompt)
            return _tier_response("complex")

        monkeypatch.setattr("kiro_crew.llm_helpers.stream_and_collect", _stream)
        own = _OwnSessionRuntime(ACP_BACKEND_KIRO, SimpleNamespace(backend=ACP_BACKEND_KIRO))
        assert _decide_on_llm(ACP_BACKEND_KIRO, own, _floor_monkeypatch) is not None
        assert len(streamed) == 1

        shared = _FlippingRuntime(ACP_BACKEND_KIRO, ACP_BACKEND_KIRO)
        runtime = MagicMock()
        runtime.acp_backend = ACP_BACKEND_KIRO
        runtime.is_alive.return_value = True
        runtime.send_request = AsyncMock(return_value=1)
        runtime.send_notification = AsyncMock()
        handle = AcpSessionHandle(
            session_id="sess-route",
            queue=asyncio.Queue(),
            runtime=runtime,
            watchdog=WatchdogSettings(),
        )
        prompts: list[str] = []

        async def _answer(text: str, *, allow_image: bool = True):
            prompts.append(text)
            yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=_tier_response("complex"))
            yield SimpleNamespace(kind=EVENT_COMPLETE, text="")

        destroy = AsyncMock()
        setattr(handle, "prompt", _answer)
        setattr(handle, "destroy", destroy)
        setattr(shared, "handle", handle)
        assert _decide_on_llm(ACP_BACKEND_KIRO, shared, _floor_monkeypatch) is not None
        assert len(prompts) == 1
        destroy.assert_awaited_once()

    @pytest.mark.parametrize("backend", ["claude"])
    def test_a_backend_mismatch_refuses_before_acquiring_a_session(
        self, backend, _floor_monkeypatch, no_fleet_denial, log_home, monkeypatch
    ) -> None:
        _floor_monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
        _floor_monkeypatch.setattr(gate, "_keystone_enabled", lambda: True)
        calls: list[str] = []

        async def _own(*_args, **_kwargs):
            calls.append("own")
            return _tier_response("complex")

        async def _shared(*_args, **_kwargs):
            calls.append("shared")
            return _tier_response("complex")

        def _factory(model: str, source: impl_llm.JudgeSource):
            assert source.backend == backend
            return impl_llm.build_session_runner(_BackendRuntime(), model=model, source=source)

        monkeypatch.setattr(impl_llm, "_call_on_own_session", _own)
        monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", _shared)
        impl_llm.set_runner_factory(_factory)
        answers = asyncio.run(
            gate.decide(
                gate.ROUTE_POINT,
                {"message": "hi"},
                mr.questions(),
                session_key="chat-1",
                config=_config(provider="llm"),
                backend=backend,
            )
        )
        assert answers is None
        assert calls == []
        rows = log_home()
        assert [row["lane"] for row in rows] == [gate.LANE_LLM]
        assert [row["error"] for row in rows] == [gate.ERROR_PROVIDER]

    def test_the_chat_turn_passes_its_backend_to_the_judge(self, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_runner

        seen: list[str | None] = []

        async def _route(*_args, **kwargs):
            seen.append(kwargs.get("backend"))
            return None

        monkeypatch.setattr(chat_runner, "_route_armed", lambda _key: True)
        monkeypatch.setattr(
            chat_runner,
            "_jev_route_baseline",
            lambda _slot, _client: ("model-b", "model-b", 0),
        )
        monkeypatch.setattr(mr, "rate_lookup", lambda _provider: None)
        monkeypatch.setattr(mr, "routed_model", _route)
        asyncio.run(
            chat_runner._route_model_for_turn(
                SimpleNamespace(conversation_log=None),
                SimpleNamespace(jev_route=True),
                SimpleNamespace(
                    backend="claude",
                    available_models=lambda: [],
                ),
                "hello",
                "chat-x",
                prompt="hello",
            )
        )
        assert seen == ["claude"]

    @pytest.mark.parametrize(
        ("client", "expected"),
        [
            (SimpleNamespace(backend=ACP_BACKEND_KIRO, available_models=lambda: []), ""),
            (SimpleNamespace(available_models=lambda: []), None),
        ],
    )
    def test_the_chat_turn_tells_kiro_from_an_unreadable_backend(
        self, monkeypatch, client, expected
    ) -> None:
        from kiro_crew.dashboard import chat_runner

        seen: list[str | None] = []

        async def _route(*_args, **kwargs):
            seen.append(kwargs.get("backend"))
            return None

        monkeypatch.setattr(chat_runner, "_route_armed", lambda _key: True)
        monkeypatch.setattr(
            chat_runner,
            "_jev_route_baseline",
            lambda _slot, _client: ("model-b", "model-b", 0),
        )
        monkeypatch.setattr(mr, "rate_lookup", lambda _provider: None)
        monkeypatch.setattr(mr, "routed_model", _route)
        asyncio.run(
            chat_runner._route_model_for_turn(
                SimpleNamespace(conversation_log=None),
                SimpleNamespace(jev_route=True),
                client,
                "hello",
                "chat-x",
                prompt="hello",
            )
        )
        assert seen == [expected]

    def test_a_spawn_passes_its_parent_backend_to_the_judge(self, monkeypatch) -> None:
        from kiro_crew import subagent as subagent_mod

        seen: list[str | None] = []

        async def _route(*_args, **kwargs):
            seen.append(kwargs.get("backend"))
            return "", "medium"

        provider = SimpleNamespace(backend="claude")
        sessions = SimpleNamespace(get_provider=lambda _key: provider)
        info = SimpleNamespace(
            routed_tier="",
            routed_model="",
            model="",
            agent="",
            crew="",
            backend=None,
            conversation_key="",
            parent_session_key="chat-parent",
            task="fix the bug",
            id="run-1",
        )
        monkeypatch.setattr(mr, "derive_enabled", lambda: True)
        monkeypatch.setattr(subagent_mod, "_subagent_default_model", lambda: "")
        monkeypatch.setattr(mr, "inherited_baseline", lambda _provider: "model-b")
        monkeypatch.setattr("kiro_crew.llm_helpers.provider_advertised_ids", lambda _p: [])
        monkeypatch.setattr(mr, "rate_lookup", lambda _provider: None)
        monkeypatch.setattr(mr, "routed_spawn_model", _route)
        asyncio.run(subagent_mod._route_unpinned_start(info, "template", sessions))
        assert seen == ["claude"]
