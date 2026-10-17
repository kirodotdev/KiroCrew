"""``model.route`` derived tiers: targets read off the live advertised list.

With ``decisions.model_route_derive`` on, a tier the owner left empty takes a
target derived from the advertised models and their credit multipliers. These
tests pin the four promises that make that safe to ship off by default:

- the switch OFF routes exactly as the owner map alone does;
- an owner pin always wins over a derived target;
- every derived id is one the account advertises, and a cold list derives nothing;
- a derived choice is marked ``derived`` on its record.

The model ids here are fakes; test files are outside the tree the model-id gate
reads, and production code names none.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kiro_crew import model_registry
from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT, DecisionsConfig
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions import outcomes
from kiro_crew.decisions.points import model_route as mr
from kiro_crew.decisions.types import Answer

#: A fake advertised list with prices: one cheap, two at the baseline, one dear.
RATES = {
    "fake-mini": 0.25,
    "fake-tiny": 0.5,
    "fake-standard": 1.0,
    "fake-also-standard": 1.0,
    "fake-frontier": 2.2,
}
ADVERTISED = ["auto", *RATES]


def _rate(model_id: str) -> float | None:
    return RATES.get(model_id)


EMPTY_MAP = {"simple": "", "medium": "", "complex": ""}


def _config(*, derive: bool, model_route: dict | None = None):
    return SimpleNamespace(
        decisions=DecisionsConfig(
            bucket=100,
            model_route=dict(EMPTY_MAP if model_route is None else model_route),
            model_route_derive=derive,
        )
    )


@pytest.fixture(autouse=True)
def consent(tmp_path, _floor_monkeypatch):
    path = tmp_path / "decisions_consent.json"
    _floor_monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT,
                "history_budget_chars": 0,
                # A spawn's task is a tool-call argument; the spawn tests need the
                # scope, and the test that withholds it rewrites this file.
                "tool_args": True,
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def clean_outcomes():
    outcomes.reset()
    yield
    outcomes.reset()


@pytest.fixture(autouse=True)
def log_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")


@pytest.fixture
def catalog_rates(monkeypatch):
    """The catalog's multipliers, as the one lookup derivation reads by default."""
    monkeypatch.setattr(model_registry, "_CATALOG_RATES", {"acp": dict(RATES)})


#: The lookup a Kiro session gets once the catalog above is warm.
def _kiro_lookup(model_id: str) -> float | None:
    return model_registry.rate_multiplier(model_id, "acp")


@pytest.fixture(autouse=True)
def clean_escalation():
    for store in (mr._LAST_TIER, mr._FLOOR, mr._UNSETTLED):
        store.clear()
    yield
    for store in (mr._LAST_TIER, mr._FLOOR, mr._UNSETTLED):
        store.clear()


class _Oracle:
    def __init__(self, value: str, p: float = 0.95) -> None:
        self.value = value
        self.p = p
        self.asked = 0

    async def ask(self, state, questions):
        self.asked += 1
        return {q.id: Answer(id=q.id, value=self.value, p=self.p) for q in questions}


@pytest.fixture
def answers(monkeypatch):
    """Pin the config snapshot to *cfg* and answer every question with *tier*."""
    import kiro_crew.decisions.gate as gate_mod
    import kiro_crew.decisions.impl_jev as impl_mod

    def _set(tier: str, cfg, p: float = 0.95) -> _Oracle:
        oracle = _Oracle(tier, p)
        monkeypatch.setattr(gate_mod, "_snapshot", lambda: cfg)
        monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: oracle)
        return oracle

    return _set


@pytest.fixture
def route(answers):
    """Answer *tier* under *cfg* and return what ``routed_model`` decided."""

    def _run(
        tier: str,
        cfg,
        *,
        current_model: str = "",
        advertised=ADVERTISED,
        retried: bool = False,
        session_key: str = "chat-derived",
    ):
        answers(tier, cfg)
        routed = asyncio.run(
            mr.routed_model(
                "do the thing",
                session_key=session_key,
                current_model=current_model,
                advertised=list(advertised),
                retried=retried,
                rate_of=_kiro_lookup,
            )
        )
        if routed is not None:
            # What the runner does once the turn runs on the answer.
            mr.note_applied(session_key, routed)
        return routed

    return _run


class TestDerivation:
    def test_the_three_targets_come_from_the_advertised_prices(self):
        derived = mr.derive_tier_models(ADVERTISED, "", _rate)
        assert derived == {"simple": "fake-mini", "medium": "", "complex": "fake-frontier"}

    def test_the_baseline_is_the_sessions_own_price(self):
        """A session already on a cheap model has nothing cheaper below it but the
        cheapest, and nothing it could call dear except what costs more than it."""
        assert mr.derive_tier_models(ADVERTISED, "fake-tiny", _rate) == {
            "simple": "fake-mini",
            "medium": "",
            "complex": "fake-frontier",
        }
        assert mr.derive_tier_models(ADVERTISED, "fake-frontier", _rate)["complex"] == ""
        assert mr.derive_tier_models(ADVERTISED, "fake-mini", _rate)["simple"] == ""

    def test_an_unpriced_baseline_reads_as_the_auto_rate(self):
        derived = mr.derive_tier_models(ADVERTISED, "fake-unlisted", _rate)
        assert derived["simple"] == "fake-mini"
        assert derived["complex"] == "fake-frontier"

    def test_a_cold_list_derives_nothing(self):
        assert mr.derive_tier_models([], "", _rate) == EMPTY_MAP
        assert mr.derive_tier_models(ADVERTISED, "", lambda _id: None) == EMPTY_MAP

    def test_only_advertised_ids_are_ever_derived(self):
        """A priced model the account is not offered is never a target."""
        derived = mr.derive_tier_models(["fake-standard", "fake-tiny"], "", _rate)
        assert derived == {"simple": "fake-tiny", "medium": "", "complex": ""}

    def test_the_auto_sentinel_is_never_a_target(self):
        derived = mr.derive_tier_models(["auto", "fake-standard"], "", lambda _id: 0.1)
        assert "auto" not in derived.values()

    def test_an_unbroken_price_tie_inherits(self):
        rates = {"fake-x": 3.0, "fake-y": 3.0}
        derived = mr.derive_tier_models(list(rates), "", rates.get)
        assert derived["complex"] == ""

    def test_a_price_tie_goes_to_the_larger_known_window(self, monkeypatch):
        rates = {"fake-x": 3.0, "fake-x-1m": 3.0}
        windows = {"fake-x": 200_000, "fake-x-1m": 1_000_000}
        monkeypatch.setattr(mr, "known_window", windows.get)
        derived = mr.derive_tier_models(list(rates), "", rates.get)
        assert derived["complex"] == "fake-x-1m"

    def test_no_lookup_derives_nothing(self):
        """A backend no catalog prices gets no lookup, and so no derived tier."""
        assert mr.derive_tier_models(ADVERTISED, "", None) == EMPTY_MAP


class TestTheLookupIsTheSessionsOwnBackend:
    def test_a_kiro_session_reads_the_catalogs_prices(self, catalog_rates):
        lookup = mr.rate_lookup(SimpleNamespace(backend=""))
        assert lookup is not None and lookup("fake-frontier") == 2.2
        assert mr.derive_tier_models(ADVERTISED, "", lookup)["complex"] == "fake-frontier"

    def test_another_harness_never_reads_them(self, catalog_rates):
        """Its ids may fold onto the catalog's; its account does not pay those prices."""
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE

        lookup = mr.rate_lookup(SimpleNamespace(backend=ACP_BACKEND_CLAUDE))
        assert lookup is not None and lookup("fake-frontier") is None
        assert mr.derive_tier_models(ADVERTISED, "", lookup) == EMPTY_MAP

    def test_an_unknown_backend_gets_no_lookup(self, catalog_rates):
        assert mr.rate_lookup(SimpleNamespace(backend="not-a-backend")) is None
        assert mr.rate_lookup(None) is None

    def test_the_wrapped_sessions_backend_is_read(self, catalog_rates):
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE

        wrapper = SimpleNamespace(client=SimpleNamespace(backend=ACP_BACKEND_CLAUDE))
        assert mr.rate_lookup(wrapper)("fake-frontier") is None


class TestTheCatalogRates:
    def test_a_snapshot_carries_each_rows_price(self, monkeypatch):
        monkeypatch.setattr(model_registry, "_CATALOG_RATES", {})
        monkeypatch.setattr(model_registry, "refresh_advertised_models", lambda *_a: False)
        monkeypatch.setattr(model_registry, "_replace_kiro_windows", lambda _w: False)
        model_registry.refresh_kiro_catalog(
            [
                {"model_id": "fake-a", "model_name": "fake-a", "rate_multiplier": 0.4},
                {"model_id": "fake-b", "model_name": "fake-b", "rate_multiplier": True},
                {"model_id": "fake-c", "model_name": "fake-c", "rate_multiplier": -1},
                {"model_id": "fake-d", "model_name": "fake-d"},
            ],
            "acp",
        )
        assert model_registry.rate_multiplier("fake-a", "acp") == 0.4
        assert model_registry.rate_multiplier("FAKE-A", "acp") == 0.4
        assert model_registry.rate_multiplier("fake-a", "claude_code") is None
        for unpriced in ("fake-b", "fake-c", "fake-d", ""):
            assert model_registry.rate_multiplier(unpriced, "acp") is None

    def test_a_spelling_fold_finds_the_price(self, monkeypatch):
        monkeypatch.setattr(model_registry, "_CATALOG_RATES", {"acp": {"fake-4.5": 1.3}})
        assert model_registry.rate_multiplier("fake-4-5", "acp") == 1.3


class TestTheSwitch:
    def test_it_ships_off(self):
        assert DecisionsConfig().model_route_derive is False
        assert DecisionsConfig.from_raw({}).model_route_derive is False

    def test_only_a_real_bool_turns_it_on(self):
        assert DecisionsConfig.from_raw({"model_route_derive": True}).model_route_derive is True
        for value in ("true", 1, "yes", None):
            assert (
                DecisionsConfig.from_raw({"model_route_derive": value}).model_route_derive is False
            )

    def test_off_applies_the_owner_map_alone(self):
        mapping, derived = mr.effective_tier_models(
            EMPTY_MAP, advertised=ADVERTISED, derive=False, rate_of=_rate
        )
        assert mapping == EMPTY_MAP and derived == frozenset()


class TestRoutedWithTheSwitch:
    @pytest.mark.parametrize("tier", list(mr.TIERS))
    def test_off_routes_exactly_as_the_owner_map_does(self, route, catalog_rates, tier):
        """The shipped default. An empty owner map stays unpinned on every tier,
        whatever the advertised prices would derive."""
        routed = route(tier, _config(derive=False))
        assert routed is not None
        assert routed["model_chosen"] == mr.UNPINNED
        assert "source" not in routed

    def test_on_routes_an_empty_tier_to_its_derived_target(self, route, catalog_rates):
        routed = route("complex", _config(derive=True))
        assert routed["model_chosen"] == "fake-frontier"
        assert routed["source"] == mr.SOURCE_DERIVED

    def test_on_leaves_the_baseline_tier_on_the_sessions_model(self, route, catalog_rates):
        routed = route("medium", _config(derive=True), current_model="fake-standard")
        assert routed["model_chosen"] == mr.UNPINNED
        assert "source" not in routed

    def test_an_owner_pin_wins_over_the_derived_target(self, route, catalog_rates):
        cfg = _config(derive=True, model_route={"simple": "fake-tiny", "medium": "", "complex": ""})
        routed = route("simple", cfg)
        assert routed["model_chosen"] == "fake-tiny"
        assert "source" not in routed

    def test_a_cold_list_on_routes_nothing_new(self, route, catalog_rates):
        routed = route("complex", _config(derive=True), advertised=[])
        assert routed["model_chosen"] == mr.UNPINNED

    def test_a_cold_catalog_says_why_nothing_was_derived(self, route, monkeypatch):
        """No price for any advertised model: the record shows the inert state."""
        monkeypatch.setattr(model_registry, "_CATALOG_RATES", {})
        routed = route("complex", _config(derive=True))
        assert routed["model_chosen"] == mr.UNPINNED
        assert mr.build_outcome(routed)["derive"] == mr.DERIVE_UNPRICED

    def test_a_priced_list_and_the_switch_off_carry_no_marker(self, route, catalog_rates):
        assert "derive" not in mr.build_outcome(route("complex", _config(derive=True)))
        assert "derive" not in mr.build_outcome(route("complex", _config(derive=False)))

    def test_the_record_says_derived(self, route, catalog_rates):
        routed = route("simple", _config(derive=True))
        row = mr.build_outcome(routed)
        assert row["source"] == mr.SOURCE_DERIVED
        assert row["model_chosen"] == "fake-mini"
        assert "source" not in mr.build_outcome({**routed, "source": None})


# ---------------------------------------------------------------------------
# One-step escalation on a failed or retried turn
# ---------------------------------------------------------------------------

KEY = "chat-derived"


class TestEscalation:
    def test_one_step_up_and_complex_is_the_top(self):
        assert mr.step_up("simple") == "medium"
        assert mr.step_up("medium") == "complex"
        assert mr.step_up("complex") == "complex"
        assert mr.step_up("") == ""

    def test_a_turn_too_hard_for_its_tier_moves_the_next_one_exactly_one_step(
        self, route, catalog_rates
    ):
        cfg = _config(derive=True)
        assert route("simple", cfg)["tier"] == "simple"
        mr.settle(KEY, False)
        routed = route("simple", cfg)
        assert routed["tier"] == "medium", "one step, not two"
        assert routed["escalated_from"] == "simple"
        assert mr.build_outcome(routed)["escalated_from"] == "simple"

    def test_the_floor_holds_until_a_turn_succeeds(self, route, catalog_rates):
        cfg = _config(derive=True)
        route("simple", cfg)
        mr.settle(KEY, False)
        route("simple", cfg)
        assert route("simple", cfg)["tier"] == "medium", "stays up for the unit of work"
        mr.settle(KEY, True)
        routed = route("simple", cfg)
        assert routed["tier"] == "simple"
        assert "escalated_from" not in routed

    def test_a_retried_turn_runs_one_step_above_the_turn_it_repeats(self, route, catalog_rates):
        cfg = _config(derive=True)
        route("medium", cfg)
        mr.settle(KEY, True)
        routed = route("medium", cfg, retried=True)
        assert routed["tier"] == "complex"
        assert routed["model_chosen"] == "fake-frontier"

    def test_escalation_never_lowers_an_answer(self, route, catalog_rates):
        cfg = _config(derive=True)
        route("simple", cfg)
        mr.settle(KEY, False)
        assert route("complex", cfg)["tier"] == "complex"

    def test_a_stop_press_leaves_no_floor(self, route, catalog_rates):
        cfg = _config(derive=True)
        route("simple", cfg)
        mr.settle(KEY, None)
        assert route("simple", cfg)["tier"] == "simple"

    def test_with_the_switch_off_nothing_escalates_or_is_remembered(self, route, catalog_rates):
        cfg = _config(derive=False)
        route("simple", cfg)
        assert mr._UNSETTLED == {} and mr._LAST_TIER == {}
        mr.settle(KEY, False)
        assert mr._FLOOR == {}
        routed = route("simple", cfg, retried=True)
        assert routed["tier"] == "simple"
        assert "escalated_from" not in routed

    def test_an_answer_the_turn_did_not_run_on_is_not_followed(self, answers, catalog_rates):
        """The runner throws some answers away (a window refusal, a moved pick);
        only one it applied may leave a floor or be stepped up from."""
        answers("simple", _config(derive=True))
        routed = asyncio.run(mr.routed_model("x", session_key=KEY, advertised=ADVERTISED))
        assert routed["tier"] == "simple" and mr._UNSETTLED == {}
        mr.settle(KEY, False)
        assert mr._FLOOR == {}
        assert mr.TRACKED not in mr.build_outcome(routed)

    def test_a_switch_that_did_not_take_is_not_followed(self, answers, catalog_rates):
        answers("simple", _config(derive=True))
        routed = asyncio.run(mr.routed_model("x", session_key=KEY, advertised=ADVERTISED))
        mr.note_applied(KEY, {**routed, "applied": False})
        assert mr._UNSETTLED == {} and mr._LAST_TIER == {}
        mr.note_applied(KEY, {**routed, "applied": True})
        assert mr._UNSETTLED == {KEY: "simple"}

    def test_the_store_is_bounded(self):
        for n in range(mr.ESCALATION_MAX_KEYS + 5):
            mr.note_routed(f"chat-{n}", "simple")
        assert len(mr._LAST_TIER) == mr.ESCALATION_MAX_KEYS
        assert "chat-0" not in mr._LAST_TIER


class TestHowATurnEnded:
    @pytest.fixture
    def turn_ok(self):
        from kiro_crew.dashboard import chat_runner

        return chat_runner._jev_route_turn_ok

    def test_a_clean_ending_is_ok(self, turn_ok):
        assert turn_ok({"stop_reason": "end_turn"}) is True
        assert turn_ok({"stop_reason": None}) is True

    @pytest.mark.parametrize("reason", ["max_tokens", "max_turn_requests"])
    def test_only_the_models_own_limits_read_as_too_hard(self, turn_ok, reason):
        assert turn_ok({"stop_reason": reason}) is False

    @pytest.mark.parametrize(
        "reason",
        ["error: backend unavailable", "error: quota", "timeout", "refusal", "cancelled"],
    )
    def test_a_provider_or_transport_ending_is_not_evidence(self, turn_ok, reason):
        """An outage must not walk a session up to the dearest model."""
        assert turn_ok({"stop_reason": reason}) is None

    def test_a_turn_with_no_terminal_event_is_not_evidence(self, turn_ok):
        """A cancel, a dead process and a transport error all end this way."""
        assert turn_ok(None) is None


# ---------------------------------------------------------------------------
# Unpinned sub-agent starts
# ---------------------------------------------------------------------------


class _Provider:
    """The parent's provider: what it advertises and what it serves."""

    def __init__(self, served: str = "fake-standard") -> None:
        self.served_model = served
        self._session_id = "sid-1"
        self._explicit_pick_epoch = 0

    def available_models(self):
        return [{"modelId": name} for name in ADVERTISED]


class _Sessions:
    def __init__(self, provider) -> None:
        self.provider = provider

    def get_provider(self, key):
        return self.provider


def _info(**overrides):
    from kiro_crew.subagent import SubagentInfo

    fields = {"id": "run-1", "task": "rename the variable", "parent_session_key": "chat-parent"}
    fields.update(overrides)
    return SubagentInfo(**fields)


@pytest.fixture
def no_role_pin(monkeypatch):
    import kiro_crew.subagent as subagent_mod

    monkeypatch.setattr(subagent_mod, "_subagent_default_model", lambda cfg=None: "")


def _start(info, kind: str = "template", provider=None):
    from kiro_crew.subagent import _route_unpinned_start

    parent = _Provider() if provider is None else provider
    asyncio.run(_route_unpinned_start(info, kind, _Sessions(parent)))
    return info


class TestSpawnRouting:
    def test_the_spawn_scope_is_one_the_gate_checks(self):
        from kiro_crew.decisions import gate

        assert set(mr.SPAWN_SCOPES) <= set(gate.REQUEST_SCOPES)

    def test_the_gate_checks_a_request_scope_against_its_own_keystone_read(self, consent, answers):
        """One keystone read authorizes both the endpoint and the scope, so a
        consent change between a caller's check and the send cannot slip a wider
        category through."""
        from kiro_crew import decisions as core
        from kiro_crew.decisions import gate

        oracle = answers("complex", _config(derive=True))
        asked = asyncio.run(
            core.decide(
                mr.POINT, {"message": "x"}, mr.questions(), session_key="k", scopes=("tool_args",)
            )
        )
        assert asked is not None and oracle.asked == 1
        consent.write_text(
            json.dumps({"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT}),
            encoding="utf-8",
        )
        refused = asyncio.run(
            core.decide(
                mr.POINT, {"message": "x"}, mr.questions(), session_key="k", scopes=("tool_args",)
            )
        )
        assert refused is None and oracle.asked == 1
        unscoped = asyncio.run(
            core.decide(mr.POINT, {"message": "x"}, mr.questions(), session_key="k")
        )
        assert unscoped is not None, "a chat turn needs no extra scope"
        assert gate._request_scopes_consented(("not-a-scope",), {"tool_args": True}) is False

    def test_without_the_tool_argument_scope_a_start_sends_nothing(
        self, consent, answers, catalog_rates, no_role_pin
    ):
        """The task is an agent-written tool argument, not owner-typed text."""
        consent.write_text(
            json.dumps({"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT}),
            encoding="utf-8",
        )
        oracle = answers("complex", _config(derive=True))
        info = _start(_info())
        assert (info.routed_model, info.routed_tier) == ("", "")
        assert oracle.asked == 0

    def test_the_switch_off_asks_nothing(self, answers, catalog_rates):
        oracle = answers("complex", _config(derive=False))
        result = asyncio.run(
            mr.routed_spawn_model("x", session_key="chat-parent", advertised=ADVERTISED)
        )
        assert result == ("", "")
        assert oracle.asked == 0

    def test_an_unpinned_start_takes_the_derived_target(
        self, answers, catalog_rates, no_role_pin, tmp_path
    ):
        answers("complex", _config(derive=True))
        info = _start(_info())
        assert info.routed_model == "fake-frontier"
        assert info.routed_tier == "complex"
        assert info.model == "", "the caller's own pin field is untouched"

    def test_the_start_is_receipted_on_the_parents_log(
        self, answers, catalog_rates, monkeypatch, tmp_path
    ):
        rows: list[dict] = []
        real = log_mod.build_row
        monkeypatch.setattr(log_mod, "build_row", lambda **kw: rows.append(real(**kw)) or rows[-1])
        answers("simple", _config(derive=True))
        chosen, tier = asyncio.run(
            mr.routed_spawn_model(
                "x",
                session_key="chat-parent",
                current_model="fake-standard",
                advertised=ADVERTISED,
                run_id="run-1",
                rate_of=_kiro_lookup,
            )
        )
        assert (chosen, tier) == ("fake-mini", "simple")
        row = next(r for r in rows if r.get("subject") == mr.SUBJECT_SUBAGENT)
        assert row["run_id"] == "run-1"
        assert row["source"] == mr.SOURCE_DERIVED
        assert row["session"] == log_mod.session_digest("chat-parent")

    def test_the_baseline_tier_keeps_the_inherited_model(self, answers, catalog_rates, no_role_pin):
        answers("medium", _config(derive=True))
        info = _start(_info())
        assert (info.routed_model, info.routed_tier) == ("", "medium")

    def test_a_spawn_does_not_touch_the_parents_escalation(
        self, answers, catalog_rates, no_role_pin
    ):
        mr.note_routed("chat-parent", "simple")
        mr.settle("chat-parent", False)
        answers("simple", _config(derive=True))
        info = _start(_info())
        assert info.routed_tier == "simple"
        assert mr._UNSETTLED == {}

    @pytest.mark.parametrize(
        "overrides",
        [
            {"model": "fake-tiny"},
            {"agent": "some-template"},
            {"crew": "some-crew"},
            {"backend": ""},
            {"conversation_key": "subagent:earlier"},
            {"parent_session_key": ""},
        ],
    )
    def test_a_start_that_chose_its_model_is_never_routed(
        self, answers, catalog_rates, no_role_pin, overrides
    ):
        oracle = answers("complex", _config(derive=True))
        info = _start(_info(**overrides))
        assert (info.routed_model, info.routed_tier) == ("", "")
        assert oracle.asked == 0

    def test_a_role_pin_is_never_routed(self, answers, catalog_rates, monkeypatch):
        import kiro_crew.subagent as subagent_mod

        monkeypatch.setattr(subagent_mod, "_subagent_default_model", lambda cfg=None: "fake-tiny")
        oracle = answers("complex", _config(derive=True))
        info = _start(_info())
        assert info.routed_model == ""
        assert oracle.asked == 0

    def test_a_member_start_is_never_routed(self, answers, catalog_rates, no_role_pin):
        oracle = answers("complex", _config(derive=True))
        info = _start(_info(), kind="member")
        assert info.routed_model == ""
        assert oracle.asked == 0

    def test_the_switch_off_leaves_every_start_alone(self, answers, catalog_rates, no_role_pin):
        oracle = answers("complex", _config(derive=False))
        info = _start(_info())
        assert (info.routed_model, info.routed_tier) == ("", "")
        assert oracle.asked == 0

    def test_a_respawned_start_keeps_its_routing_without_asking(
        self, answers, catalog_rates, no_role_pin
    ):
        """A dead process says nothing about whether the tier was right."""
        oracle = answers("complex", _config(derive=True))
        info = _start(_info(routed_tier="simple", routed_model="fake-mini"))
        assert (info.routed_model, info.routed_tier) == ("fake-mini", "simple")
        assert oracle.asked == 0

    def test_a_smaller_window_needs_the_confidence_floor(
        self, answers, catalog_rates, monkeypatch, tmp_path
    ):
        windows = {"fake-standard": 1_000_000, "fake-mini": 200_000}
        monkeypatch.setattr(mr, "known_window", windows.get)
        answers("simple", _config(derive=True), p=0.5)
        chosen, tier = asyncio.run(
            mr.routed_spawn_model(
                "x",
                session_key="chat-parent",
                current_model="fake-standard",
                advertised=ADVERTISED,
                rate_of=_kiro_lookup,
            )
        )
        assert (chosen, tier) == ("", "simple")


class TestTheSpawnBaseline:
    def test_the_runner_and_the_start_read_one_latch(self):
        """One spelling of the attribute, and the runner's latch is what a start reads."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.llm_helpers import ROUTE_BASELINE_ATTR, latch_route_baseline

        assert chat_runner._JEV_ROUTE_BASELINE_ATTR is ROUTE_BASELINE_ATTR
        parent = _Provider(served="fake-mini")
        latch_route_baseline(parent, "fake-standard")
        assert mr.inherited_baseline(parent) == "fake-standard"

    def test_a_start_after_a_simple_parent_turn_is_priced_against_the_parents_own_model(
        self, answers, catalog_rates, no_role_pin
    ):
        """The parent's last routed turn moved it to the cheap model; the latch
        still names the model it runs with routing off."""
        from kiro_crew.llm_helpers import latch_route_baseline

        parent = _Provider(served="fake-mini")
        latch_route_baseline(parent, "fake-standard")
        answers("simple", _config(derive=True))
        info = _start(_info(), provider=parent)
        assert info.routed_model == "fake-mini"
        answers("medium", _config(derive=True))
        info = _start(_info(id="run-2"), provider=parent)
        assert (info.routed_model, info.routed_tier) == ("", "medium")

    def test_a_latch_for_another_session_or_pick_is_not_read(self):
        from kiro_crew.llm_helpers import ROUTE_BASELINE_ATTR

        parent = _Provider(served="fake-tiny")
        setattr(parent, ROUTE_BASELINE_ATTR, ("sid-old", "fake-standard", 0))
        assert mr.inherited_baseline(parent) == "fake-tiny"
        setattr(parent, ROUTE_BASELINE_ATTR, ("sid-1", "fake-standard", 3))
        assert mr.inherited_baseline(parent) == "fake-tiny"

    def test_a_substitute_is_never_the_baseline(self, answers, catalog_rates, no_role_pin):
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        parent = _Provider(served="fake-tiny")
        setattr(parent, TURN_FALLBACK_ATTR, ("fake-standard", "fake-tiny"))
        assert mr.inherited_baseline(parent) == "fake-standard"
        setattr(parent, TURN_FALLBACK_ATTR, ("", "fake-tiny"))
        assert mr.inherited_baseline(parent) is None
        oracle = answers("complex", _config(derive=True))
        info = _start(_info(), provider=parent)
        assert (info.routed_model, info.routed_tier) == ("", "")
        assert oracle.asked == 0

    def test_a_parent_with_no_live_provider_routes_nothing(
        self, answers, catalog_rates, no_role_pin
    ):
        from kiro_crew.subagent import _route_unpinned_start

        oracle = answers("complex", _config(derive=True))
        info = _info()
        asyncio.run(_route_unpinned_start(info, "template", _Sessions(None)))
        assert (info.routed_model, info.routed_tier) == ("", "")
        assert oracle.asked == 0


class TestTheUsageRowOfARoutedStart:
    @pytest.mark.asyncio
    async def test_the_row_names_no_routed_id_the_child_may_have_withheld(self):
        """A routed id is a request, not proof of what served: the usage row keeps
        the caller's own pin (none here) and lets the served model be resolved."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from test_subagent import _mock_ctx_builder_auto_spawn, _mock_sessions

        from kiro_crew.acp.types import AcpEvent, TurnUsage
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.providers.base import EVENT_COMPLETE
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        async def _complete_stream(*_a, **_k):
            yield AcpEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=0.5))

        sessions = _mock_sessions()
        provider = AsyncMock()
        provider.context_usage_pct = lambda: 12.0
        provider.stream = MagicMock(side_effect=lambda *a, **kw: _complete_stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        manager = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder_auto_spawn())
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="routed01",
            task="rename the variable",
            agent="researcher",
            parent_session_key="slack:C123:T456",
            routed_model="fake-mini",
            routed_tier="simple",
        )
        manager._log_spawned(info)
        usage_rows = AsyncMock()
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.dashboard.handlers.usage.persist_token_record_async",
                usage_rows,
            ),
            patch(
                "kiro_crew.dashboard.handlers.usage.read_context_tokens",
                MagicMock(return_value=(1, 200000)),
                create=True,
            ),
        ):
            await manager._run_inner(info, "subagent:routed01")

        usage_rows.assert_awaited_once()
        assert usage_rows.await_args.args[1] == ""
