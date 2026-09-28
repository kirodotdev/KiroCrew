"""``model.route``: the tier, the id it maps to, and the model the turn ends up on.

Four things carry the weight here, and each is a way the feature goes wrong without
them.

The APPLY path. A tier is only worth answering if the answer reaches the provider,
and nothing downstream of ``routed_model`` can tell "Jev said complex" from "the
session happened to be on opus-5 already". So the load-bearing test drives the real
runner hook against a recording client and asserts the id the PROVIDER was handed --
and the mutation check beside it breaks the mapping and the tier in turn, so a hook
that ignored either would fail rather than pass by coincidence.

The REFUSALS. Every one of them leaves the session on the model it was already on,
which is what an unconsented install does, so each is asserted as "the provider was
never asked to switch" rather than as an exception.

WHOSE TURN it is. The point runs for a normal dashboard chat turn of a slot whose
owner picked ``Auto (Jev)``, and for nothing else: a cron delivery, a sub-agent
turn, an app injection and an autonudge wake each already resolve their model
through their own tier, and none has an owner watching what the answer costs.

The ROWS. A tier that arrived and could not be applied still writes a row, because
the gate's own call row says the decision succeeded -- so without one the log would
report a healthy seam while every turn kept the old model.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kiro_crew.config.sections import (
    DECISION_MODEL_ROUTE_DEFAULT,
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DecisionsConfig,
)
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions import outcomes
from kiro_crew.decisions.points import model_route as mr
from kiro_crew.decisions.types import Answer

# ---------------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------------


#: A tier map for the tests. Nothing ships one (see
#: ``DECISION_MODEL_ROUTE_DEFAULT``), so every test that expects a turn to move
#: supplies this. Test files are outside the tree the model-id gate reads.
TIER_MAP = {
    "simple": "model-a",
    "medium": "model-b",
    "complex": "model-c",
}


def _config(*, bucket: int = 100, model_route: dict | None = None):
    """A config object shaped like the one the gate reads off the live snapshot.

    *model_route* defaults to :data:`TIER_MAP`; pass ``{}`` for the shipped state.
    """
    kwargs: dict = {"bucket": bucket}
    kwargs["model_route"] = dict(TIER_MAP) if model_route is None else model_route
    return SimpleNamespace(decisions=DecisionsConfig(**kwargs))


@pytest.fixture(autouse=True)
def consent(tmp_path, monkeypatch):
    """A real keystone under *tmp_path*, consented by default; returns a setter."""
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)

    def _set(value, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT, history=0):
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(
                json.dumps(
                    {"enabled": value, "endpoint": endpoint, "history_budget_chars": history}
                ),
                encoding="utf-8",
            )

    _set(True)
    return _set


@pytest.fixture(autouse=True)
def clean_registry():
    outcomes.reset()
    yield
    outcomes.reset()


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


class _Oracle:
    """Answers the tier question with a fixed value, recording every state sent."""

    def __init__(self, value: str = "complex", p: float = 0.91) -> None:
        self.value = value
        self.p = p
        self.states: list[object] = []

    async def ask(self, state, questions):
        self.states.append(state)
        return {q.id: Answer(id=q.id, value=self.value, p=self.p) for q in questions}


@pytest.fixture
def install_oracle(monkeypatch):
    """Replace the one implementation ``decide`` constructs."""
    import kiro_crew.decisions.impl_jev as impl_mod

    def _install(oracle):
        monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: oracle)
        return oracle

    return _install


@pytest.fixture
def snapshot(monkeypatch):
    """Pin the live config snapshot the gate and the point both read."""
    import kiro_crew.decisions.gate as gate_mod

    def _set(cfg):
        monkeypatch.setattr(gate_mod, "_snapshot", lambda: cfg)
        return cfg

    _set(_config())
    return _set


# ---------------------------------------------------------------------------
# The tier question
# ---------------------------------------------------------------------------


class TestTheQuestion:
    def test_the_three_tiers_are_the_offered_domain(self):
        """A CLOSED domain: the gate refuses an answer outside a question's options,
        so a provider inventing a fourth tier reads as an invalid result rather than
        as a mapping lookup that quietly misses."""
        assert mr.TIERS == ("simple", "medium", "complex")
        assert [q.options for q in mr.questions()] == [["simple", "medium", "complex"]]

    def test_every_tier_is_described_in_the_prompt(self):
        """Without the descriptions the three words are the oracle's own guess at
        what 'medium' means, which is not a question about this product."""
        prompt = mr.questions()[0].prompt
        for tier in mr.TIERS:
            assert f"{tier} = " in prompt
            assert mr.TIER_DESCRIPTIONS[tier] in prompt

    def test_no_description_names_a_model_or_a_price(self):
        """The question is about the WORK. Naming a model would ask the oracle to
        price the turn, and the tier-to-model map is the OWNER's."""
        prose = " ".join(mr.TIER_DESCRIPTIONS.values()).lower()
        for forbidden in ("claude", "haiku", "opus", "cheap", "expensive", "cost", "$"):
            assert forbidden not in prose

    def test_the_prompt_states_which_direction_an_error_costs_more(self):
        """Without it the oracle weighs the two mistakes equally, and a short message
        naming long work reads as the cheapest tier. It names no model and no price
        either: the asymmetry is a property of the WORK's outcome."""
        assert mr.TIER_ASYMMETRY in mr.questions()[0].prompt
        assert "however short it is" in mr.TIER_ASYMMETRY
        prose = mr.TIER_ASYMMETRY.lower()
        for forbidden in ("claude", "haiku", "opus", "sonnet", "gpt", "token", "$"):
            assert forbidden not in prose
        assert not any(ch.isdigit() for ch in prose)

    def test_the_message_excerpt_is_bounded_like_skills_select(self):
        """The same text answering a question about the same turn, so two different
        excerpt sizes would mean the consent text describes one of them."""
        from kiro_crew.decisions.points import skills_select as sel

        assert mr.MAX_MESSAGE_CHARS == sel.MAX_MESSAGE_CHARS == 2000
        state = mr.build_state("x" * 5000)
        assert len(state["message"]) == 2000

    def test_history_is_omitted_when_there_is_none(self):
        """The shape the request carried before prior turns existed."""
        assert mr.build_state("hi") == {"message": "hi"}
        assert "history" in mr.build_state("hi", [{"role": "user", "text": "earlier"}])


# ---------------------------------------------------------------------------
# Reading the answer
# ---------------------------------------------------------------------------


class TestReadingTheAnswer:
    @pytest.mark.parametrize("tier", ["simple", "medium", "complex"])
    def test_an_offered_tier_is_read(self, tier):
        assert mr.read_tier({"tier": Answer(id="tier", value=tier, p=0.5)}) == tier

    @pytest.mark.parametrize(
        "answers",
        [
            None,
            {},
            "complex",
            {"tier": "complex"},
            {"tier": Answer(id="tier", value="Complex", p=0.5)},
            {"tier": Answer(id="tier", value="hard", p=0.5)},
            {"tier": Answer(id="tier", value=3, p=0.5)},
            {"other": Answer(id="other", value="complex", p=0.5)},
        ],
    )
    def test_anything_else_is_no_tier(self, answers):
        """Identity is exact: the value indexes a map the owner writes, where a
        near-miss spelling would read as an absent key and route the turn somewhere
        nobody chose."""
        assert mr.read_tier(answers) == ""


# ---------------------------------------------------------------------------
# The tier-to-model map
# ---------------------------------------------------------------------------


#: Every tier unpinned -- the shipped map.
UNPINNED_MAP = {"simple": "", "medium": "", "complex": ""}


class TestTheTierMap:
    def test_every_tier_ships_unpinned_and_no_model_id_is_named(self):
        """A hardcoded id fails at runtime for every account not entitled to it, so
        `model-selection.md` keeps code defaults at ""/"auto" and lets an operator
        pin ids in a map they write. All three keys are PRESENT and empty: "this
        tier is unpinned" is a state the log and the strip report, so it needs a
        spelling rather than being inferred from a missing key."""
        assert mr.DEFAULT_TIER_MODELS == DECISION_MODEL_ROUTE_DEFAULT == UNPINNED_MAP
        assert DecisionsConfig().model_route == UNPINNED_MAP
        assert DecisionsConfig.from_raw({}).model_route == UNPINNED_MAP

    def test_a_config_naming_one_tier_leaves_the_others_unpinned(self, snapshot):
        """Per TIER, not per map: the two tiers not named stay unpinned and keep the
        session's model, rather than inheriting an id nobody chose."""
        snapshot(_config(model_route={"complex": "model-d"}))
        assert mr.tier_models() == {"simple": "", "medium": "", "complex": "model-d"}

    def test_an_unreadable_section_leaves_every_tier_unpinned(self, monkeypatch):
        """Fail-closed: this value decides what a turn costs."""
        import kiro_crew.decisions.gate as gate_mod

        monkeypatch.setattr(gate_mod, "_snapshot", lambda: None)
        assert mr.tier_models() == UNPINNED_MAP

    def test_a_tier_outside_the_domain_is_ignored(self):
        """A tier the question never offers can never be answered, so a map that
        accepted one would read as configured while routing nothing."""
        parsed = DecisionsConfig.from_raw({"model_route": {"trivial": "x", "complex": "y"}})
        assert parsed.model_route == {"simple": "", "medium": "", "complex": "y"}

    @pytest.mark.parametrize("raw", ["   ", "auto", "", 7, None, ["model-a"]])
    def test_inherit_has_one_spelling(self, raw):
        """`normalize_agent_model`, the same normalizer `coerce_role_models` uses:
        "auto" and a non-string both collapse to "", so a tier set to "auto" keeps
        inheriting instead of hard-pinning the backend's own default."""
        assert (
            DecisionsConfig.from_raw({"model_route": {"complex": raw}}).model_route == UNPINNED_MAP
        )


class TestResolvingTheModel:
    def test_an_advertised_id_is_returned(self):
        assert mr.resolve_model("complex", {"complex": "opus-5"}, ["opus-5", "haiku"]) == "opus-5"

    def test_an_id_the_account_cannot_run_keeps_the_current_model(self):
        """The advertised list is the one the picker is built from, so 'the owner
        could have picked this by hand' and 'the tier may route to it' are one test."""
        assert mr.resolve_model("complex", {"complex": "opus-5"}, ["haiku"]) == ""

    def test_an_unmapped_or_blank_tier_keeps_the_current_model(self):
        assert mr.resolve_model("complex", {}, ["opus-5"]) == ""
        assert mr.resolve_model("complex", {"complex": "  "}, ["opus-5"]) == ""

    def test_an_unknown_advertised_list_reads_as_not_known_and_permits(self):
        """The list comes from a live provider read that can be cold, and refusing
        on a cold read would make the feature silently inert after a restart."""
        assert mr.resolve_model("complex", {"complex": "opus-5"}, []) == "opus-5"

    @pytest.mark.parametrize("advertised", ["MODEL-B", " model-b "])
    def test_a_spelling_variant_of_the_same_model_matches(self, advertised):
        """The picker and the config can spell one model two ways; refusing on the
        spelling would look exactly like refusing on entitlement."""
        assert mr.resolve_model("medium", {"medium": "model-b"}, [advertised]) != ""

    def test_two_ids_one_character_apart_are_not_folded_together(self):
        """An owner pins `model-c.1` while the same account also advertises
        `model-c`, one character away. Neither is in the canonical registry, so both
        take the lossless string fold -- and that fold must keep the dot: a fold
        that lost it would send the turn to the OTHER model while the strip row and
        the log both named the one that was pinned. This is the shape of the real
        pair an owner is most likely to write (a point release beside its base)."""
        assert mr._same_model("model-c.1", "model-c") is False
        assert (
            mr.resolve_model("complex", {"complex": "model-c.1"}, ["model-c"]) == ""
        ), "an account offered only the sibling keeps its current model"
        assert mr.resolve_model("complex", {"complex": "model-c.1"}, ["model-c.1"]) == "model-c.1"

    def test_two_registry_entries_that_differ_only_in_punctuation_stay_distinct(self):
        """The canonical registry deliberately keeps a 1M entry and its 200K
        sibling apart even though they differ by one character, so the comparison
        must ask the registry rather than fold punctuation away: routing to the
        200K model because the pin spelled the 1M one is a silently smaller
        context window, not a near-miss.

        These two literals are REGISTRY FIXTURES -- the pair whose distinctness is
        under test -- not a default or a pin, so they are the one place a real id
        belongs. A fake id has no registry entry and would exercise the string fold
        instead, leaving the registry half of the comparison untested."""
        dotted, dashed = "claude-opus-4.8", "claude-opus-4-8"
        assert mr._same_model(dotted, dashed) is False
        assert mr.resolve_model("medium", {"medium": dotted}, [dashed]) == ""
        assert mr.resolve_model("medium", {"medium": dotted}, [dotted]) == dotted

    def test_an_id_the_registry_does_not_list_folds_dots_to_dashes(self):
        """The fallback for a GPT/Qwen/operator-typed id, matching the frontend's
        own `normalizeModelKey` fallback so the picker and the map agree."""
        assert mr.resolve_model("simple", {"simple": "model-e.1"}, ["model-e-1"]) != ""


# ---------------------------------------------------------------------------
# routed_model: the answer, and every refusal
# ---------------------------------------------------------------------------


def _route(**kwargs):
    return asyncio.run(mr.routed_model("please redesign the scheduler", **kwargs))


class TestRoutedModel:
    def test_a_tier_becomes_the_model_the_map_names(self, install_oracle, snapshot, log_home):
        install_oracle(_Oracle("complex", p=0.91))
        routed = _route(session_key="chat-1", current_model="model-b", advertised=["model-c"])
        assert routed is not None
        assert routed["tier"] == "complex"
        assert routed["model_chosen"] == "model-c"
        assert routed["baseline_model"] == "model-b"
        assert routed["p"] == 0.91
        assert routed["latency_ms"] >= 0
        assert routed["turn_id"]

    def test_the_message_is_what_leaves_the_machine(self, install_oracle, snapshot, log_home):
        oracle = install_oracle(_Oracle())
        _route(session_key="chat-1", advertised=["model-c"])
        assert oracle.states == [{"message": "please redesign the scheduler"}]

    def test_the_shipped_map_answers_and_applies_nothing(self, install_oracle, snapshot, log_home):
        """The state every install starts in. The tier is STILL asked and recorded --
        that answer is the owner's evidence for what to pin -- and `model_chosen` is
        empty, so the caller switches nothing."""
        snapshot(_config(model_route=dict(UNPINNED_MAP)))
        oracle = install_oracle(_Oracle("complex", p=0.91))
        routed = _route(session_key="chat-1", current_model="model-b", advertised=["model-c"])
        assert routed is not None, "an unpinned tier is reported, not refused"
        assert routed["tier"] == "complex"
        assert routed["p"] == 0.91
        assert routed["model_chosen"] == mr.UNPINNED == ""
        assert routed["baseline_model"] == "model-b"
        assert oracle.states, "the question is asked even with nothing pinned"
        # Not an error: this is the documented state, so no error row is written.
        assert [row["error"] for row in log_home() if row["error"]] == []

    def test_an_unpinned_tier_is_told_apart_from_a_pin_that_cannot_run(
        self, install_oracle, snapshot, log_home
    ):
        """Both keep the model, and only one is a finding. The answered tier is
        pinned here and the pin is not advertised, so it records an error and
        reports nothing -- unlike the unpinned case above."""
        snapshot(_config(model_route={"simple": "", "medium": "", "complex": "model-c"}))
        install_oracle(_Oracle("complex"))
        assert _route(session_key="chat-1", advertised=["model-a"]) is None
        errors = [row["error"] for row in log_home() if row["error"]]
        assert mr.ERROR_UNKNOWN_MODEL in errors

    def test_only_the_answered_tier_decides_whether_anything_applies(
        self, install_oracle, snapshot, log_home
    ):
        """A map that pins `simple` says nothing about a turn answered `complex`."""
        snapshot(_config(model_route={"simple": "model-a", "medium": "", "complex": ""}))
        install_oracle(_Oracle("complex"))
        routed = _route(session_key="chat-1", advertised=["model-a"])
        assert routed is not None and routed["model_chosen"] == ""

    def test_no_consent_asks_nothing_and_routes_nothing(
        self, consent, install_oracle, snapshot, log_home
    ):
        consent(None)
        oracle = install_oracle(_Oracle())
        assert _route(session_key="chat-1", advertised=["model-c"]) is None
        assert oracle.states == []
        assert log_home() == []

    def test_an_unsampled_session_asks_nothing_and_routes_nothing(
        self, install_oracle, snapshot, log_home
    ):
        snapshot(_config(bucket=0))
        oracle = install_oracle(_Oracle())
        assert _route(session_key="chat-1", advertised=["model-c"]) is None
        assert oracle.states == []
        assert log_home() == []

    def test_a_provider_failure_routes_nothing(self, install_oracle, snapshot, log_home):
        class _Boom:
            async def ask(self, state, questions):
                raise RuntimeError("transport")

        install_oracle(_Boom())
        assert _route(session_key="chat-1", advertised=["model-c"]) is None

    def test_an_out_of_domain_answer_routes_nothing(self, install_oracle, snapshot, log_home):
        """The gate refuses it before this point sees it, and the point refuses it
        again -- the value is about to index the owner's own map."""
        install_oracle(_Oracle("trivial"))
        assert _route(session_key="chat-1", advertised=["model-c"]) is None

    def test_an_id_the_account_cannot_run_routes_nothing_and_says_so(
        self, install_oracle, snapshot, log_home
    ):
        install_oracle(_Oracle("complex"))
        assert _route(session_key="chat-1", advertised=["model-a"]) is None
        errors = [row["error"] for row in log_home() if row["error"]]
        assert mr.ERROR_UNKNOWN_MODEL in errors

    def test_the_unmapped_row_carries_the_tier_that_could_not_be_applied(
        self, install_oracle, snapshot, log_home
    ):
        """Without the tier the row says a decision failed and not which one."""
        install_oracle(_Oracle("complex"))
        _route(session_key="chat-1", advertised=["model-a"])
        row = next(r for r in log_home() if r["error"] == mr.ERROR_UNKNOWN_MODEL)
        assert row["tier"] == "complex"
        assert row["point"] == mr.POINT
        assert row["turn_id"]


class TestPriorTurns:
    def test_the_shipped_budget_sends_no_prior_turns_and_reads_none(
        self, install_oracle, snapshot, log_home
    ):
        """The budget is read BEFORE the transcript, so at 0 the source is never
        called and a sampled turn pays nothing for a read it would discard."""
        oracle = install_oracle(_Oracle())
        calls: list[int] = []

        def _source():
            calls.append(1)
            return [{"role": "user", "content": "earlier"}]

        _route(session_key="chat-1", advertised=["model-c"], history_source=_source)
        assert calls == []
        assert oracle.states == [{"message": "please redesign the scheduler"}]

    def test_a_consented_budget_carries_the_prior_turns(
        self, consent, install_oracle, snapshot, log_home
    ):
        consent(True, history=500)
        snapshot(_config())
        import kiro_crew.decisions.gate as gate_mod

        # The config half of the ceiling; the keystone half is the consent above.
        monkeyed = _config()
        monkeyed.decisions = DecisionsConfig(history_budget_chars=500, model_route=dict(TIER_MAP))
        gate_mod._snapshot = lambda: monkeyed  # type: ignore[assignment]
        oracle = install_oracle(_Oracle())
        _route(
            session_key="chat-1",
            advertised=["model-c"],
            history_source=lambda: [{"role": "user", "content": "earlier"}],
        )
        assert oracle.states[0]["history"] == [{"role": "user", "text": "earlier"}]

    def test_a_source_that_raises_reads_as_no_history(
        self, consent, install_oracle, snapshot, log_home
    ):
        consent(True, history=500)
        monkeyed = SimpleNamespace(
            decisions=DecisionsConfig(history_budget_chars=500, model_route=dict(TIER_MAP))
        )
        import kiro_crew.decisions.gate as gate_mod

        gate_mod._snapshot = lambda: monkeyed  # type: ignore[assignment]

        def _boom():
            raise OSError("transcript gone")

        oracle = install_oracle(_Oracle())
        routed = _route(session_key="chat-1", advertised=["model-c"], history_source=_boom)
        assert routed is not None, "prior turns make the question better, not answerable"
        assert "history" not in oracle.states[0]


# ---------------------------------------------------------------------------
# The outcome row and the strip
# ---------------------------------------------------------------------------


class TestTheOutcome:
    def _routed(self) -> dict:
        return {
            "turn_id": "t-9",
            "tier": "complex",
            "p": 0.91,
            "model_chosen": "model-c",
            "baseline_model": "model-b",
            "latency_ms": 180,
        }

    def test_the_row_carries_both_models_and_the_tier(self, log_home):
        assert mr.record_outcome("chat-1", self._routed()) is True
        row = next(r for r in log_home() if r.get("tier"))
        assert row["point"] == mr.POINT
        assert row["tier"] == "complex"
        assert row["model_chosen"] == "model-c"
        assert row["baseline_model"] == "model-b"
        assert row["p"] == 0.91
        # latency_ms is a CORE row field, so it is at top level and an `extra`
        # naming it would be dropped.
        assert row["latency_ms"] == 180
        assert "latency_ms" not in mr.build_outcome(self._routed())

    def test_the_published_outcome_is_the_row_that_was_logged(self, log_home):
        mr.record_outcome("chat-1", self._routed())
        assert outcomes.consume("chat-1") == [next(r for r in log_home() if r.get("tier"))]

    def test_a_refused_write_publishes_nothing(self, log_home, monkeypatch):
        """A strip whose durable row was refused describes a decision no verdict
        could be filed against."""
        monkeypatch.setattr(log_mod, "append", lambda row: False)
        assert mr.record_outcome("chat-1", self._routed()) is False
        assert outcomes.consume("chat-1") == []

    def test_an_error_row_is_never_published(self, log_home):
        """The turn ran on the model it was already on, so there is no routing for
        a receipt to describe and no pair of models to rate."""
        mr.record_error(
            "chat-1", turn_id="t-9", tier="complex", latency_ms=5, error=mr.ERROR_SWITCH_FAILED
        )
        assert outcomes.consume("chat-1") == []

    def test_the_row_carries_no_message_text(self, log_home):
        mr.record_outcome("chat-1", self._routed())
        serialized = json.dumps(log_home()).lower()
        for forbidden in ("message", 'history"', "scheduler"):
            assert forbidden not in serialized


# ---------------------------------------------------------------------------
# The two window rules
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_windows(monkeypatch):
    """Pin what the registry KNOWS, as a ``{model id: window}`` map.

    An id absent from the map is one the registry has not met: ``has_known_window``
    is False for it while ``model_window`` still answers its guessed reference,
    which is the pair :func:`known_window` exists to tell apart.
    """
    from kiro_crew import model_registry

    def _set(windows: dict[str, int]) -> None:
        monkeypatch.setattr(model_registry, "has_known_window", lambda name: name in windows)
        monkeypatch.setattr(
            model_registry, "model_window", lambda name, **_kw: windows.get(name, 1_000_000)
        )

    return _set


class TestTheConfidenceRule:
    def test_a_window_that_does_not_shrink_is_permitted_at_any_probability(self):
        """The harm is one-directional: room that grows or holds changes nothing
        about what fits, so the probability is not asked to carry the move."""
        assert mr.permits_smaller_window(0.0, current=200_000, target=1_000_000) is True
        assert mr.permits_smaller_window(None, current=200_000, target=200_000) is True

    def test_a_smaller_window_needs_the_floor_and_the_bound_is_inclusive(self):
        """The floor is asserted as the value it is, so weakening it is a failing test
        rather than a silent widening -- a range around it clears a weaker floor."""
        assert mr.MIN_DOWNGRADE_P == 0.80
        floor = mr.MIN_DOWNGRADE_P
        assert mr.permits_smaller_window(floor, current=1_000_000, target=200_000) is True
        assert mr.permits_smaller_window(floor - 1e-9, current=1_000_000, target=200_000) is False
        # A row with no number carries nothing, and ``True`` is an int in Python:
        # without the type check a producer bug would read as maximum confidence.
        assert mr.permits_smaller_window(None, current=1_000_000, target=200_000) is False
        assert mr.permits_smaller_window(True, current=1_000_000, target=200_000) is False

    def test_an_unknown_window_on_either_side_refuses_nothing(self, registry_windows):
        """``model_window`` answers a guessed reference for an id it has never met.
        Vetoing on a guess would pin routing to whatever model a session happens to
        be on for every model nothing is known about."""
        registry_windows({"small": 200_000})
        assert mr.is_smaller_window(current=1_000_000, target=200_000) is True
        assert mr.is_smaller_window(current=None, target=200_000) is False
        assert mr.is_smaller_window(current=1_000_000, target=None) is False
        assert mr.known_window("small") == 200_000
        assert mr.known_window("a-model-nobody-listed") is None
        assert mr.known_window("") is None
        assert mr.permits_smaller_window(0.1, current=None, target=200_000) is True
        assert mr.permits_smaller_window(0.1, current=1_000_000, target=None) is True

    def test_the_floor_is_a_constant_and_not_a_config_knob(self):
        """It is the meaning of the answer rather than a setting: a configurable
        floor is a second, undocumented way to turn the rule into a no-op."""
        names = [n for n in dir(DecisionsConfig()) if not n.startswith("__")]
        assert not [n for n in names if "threshold" in n or "downgrade" in n]


class TestTheWindowLookupSpelling:
    def test_a_pin_the_owner_cased_differently_is_still_sized(self):
        """The registry's own lookup is spelling-sensitive while a pin is accepted on a
        lossless fold, so an id the owner wrote differently must not read as a window
        nothing knows -- that answer refuses nothing, and the pin would carry the
        shrink. The registry PAIR is used rather than a placeholder, because a fake id
        has no entry and would exercise nothing."""
        plain = mr.known_window("claude-haiku-4.5")
        assert plain, "the registry pair under test must be listed"
        assert mr.known_window("Claude-Haiku-4.5") == plain
        assert mr.known_window("  CLAUDE-HAIKU-4.5  ") == plain
        # The dotted and dashed entries stay DISTINCT: two entries, two windows, so
        # folding them together here would size a pin against the wrong one.
        assert mr.known_window("claude-haiku-4-5") != plain


class TestTheMessageInTheFitReading:
    def test_the_estimate_is_the_shared_divisor_and_an_empty_message_adds_nothing(self):
        """The same rough divisor the sibling points estimate with, so a reader does
        not have to learn a second one. Over-estimating costs one downgrade."""
        from kiro_crew.decisions.points import skills_select as sel

        assert mr.CHARS_PER_TOKEN == sel.CHARS_PER_TOKEN == 4
        assert mr.prompt_tokens("x" * 400) == 100
        assert mr.prompt_tokens("") == 0

    def test_token_dense_text_is_not_read_as_a_quarter_of_its_size(self):
        """The divisor answers for Latin text. A CJK prompt tokenizes several times
        denser, so dividing every character reads it as a quarter of what it is and
        clears a shrink on ordinary input. Each non-ASCII character counts as a token,
        which can only refuse more."""
        assert mr.prompt_tokens("\u4ea4" * 400) == 400
        # Mixed input is counted per character, not by whichever kind leads.
        assert mr.prompt_tokens("x" * 400 + "\u4ea4" * 100) == 200
        # An over-estimate is the safe direction, so it is never below the divisor.
        for text in ("\u4ea4" * 400, "x" * 400, "x\u4ea4" * 200, ""):
            assert mr.prompt_tokens(text) >= len(text) // mr.CHARS_PER_TOKEN
