"""Dashboard guides, phase 3: selection and gate steps, and the mid-guide re-plan.

The generated ``ui.show`` plans carry ``select`` steps (point at a registered
picker until the page reports the selection) and ``gate`` steps (pause on a
blocker naming the setting that turns the gate on). The gateway learns only
plan ids from either: which entity anyone picked never reaches it. A tab whose
viewport class changes may re-plan its current action, revision-checked and
only at a step boundary both placements share.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import guide_catalog, mcp_guide
from kiro_crew.dashboard.guide_runs import GuideError, GuideStore

_INDEX = Path(guide_catalog.__file__).parent / "docs" / "ui-index.generated.json"


@pytest.fixture(autouse=True)
def _fresh_caches() -> Any:
    guide_catalog.ui_show_plans.cache_clear()
    guide_catalog.ui_build_manifest.cache_clear()
    yield
    guide_catalog.ui_show_plans.cache_clear()
    guide_catalog.ui_build_manifest.cache_clear()


def _index() -> dict[str, Any]:
    return json.loads(_INDEX.read_text(encoding="utf-8"))


def _plan(location_id: str) -> dict[str, Any]:
    plan = guide_catalog.ui_show_plans()[location_id]
    return {p["id"]: p["steps"] for p in plan["placements"]}


# ── the generated plans ──


def test_a_location_needing_a_selection_now_has_a_select_step_at_its_picker() -> None:
    steps = _plan("members.edit")["any"]
    # The Crewmates preview's gate comes first: the picker lives on that page.
    assert [st["id"] for st in steps] == [
        "any:gate:preview_flag:mc-preview-crew",
        "any:select:crewmate_selected",
        "any:members.edit",
    ]
    select = steps[1]
    assert select["kind"] == "select" and select["selection"] == "crewmate_selected"
    assert select["location"] == "members.roster-list" and select["entity"] == "crewmate"
    # A select step opens nothing: it waits for the page's fact.
    assert "scope" not in select


def test_a_select_step_carries_its_pickers_own_predicates() -> None:
    select, target = _plan("schedule.pause")["any"]
    assert select["location"] == "schedule.job-list"
    assert select["requires"] == ["schedule_list_view"]
    assert target["location"] == "schedule.pause"


def test_a_path_that_differs_by_viewport_is_planned_once_per_viewport() -> None:
    # The composer is drawn at every width, but its session picker is in the
    # sidebar on a desktop and in the drawer on a phone.
    plan = _plan("composer.expand")
    assert set(plan) == {"desktop", "mobile"}
    assert [st["id"].split(":", 1)[1] for st in plan["desktop"]] == [
        "chat.sessions-sidebar-toggle",
        "select:session_open",
        "composer.expand",
    ]
    assert plan["mobile"][0]["location"] == "chat.mobile-sessions-toggle"


def test_a_gate_step_leads_and_names_the_setting_that_turns_it_on() -> None:
    plan = _plan("shell.developer")
    gate = {
        "id": "desktop:gate:developer_mode",
        "kind": "gate",
        "gate": "developer_mode",
        "setting_id": "developer.developer-mode",
    }
    assert plan["desktop"][0] == gate
    assert plan["mobile"][0]["kind"] == "gate" and "location" not in plan["mobile"][0]
    # A gate no setting turns on carries no setting id.
    assert "setting_id" not in _plan("shell.terminal")["desktop"][0]


def test_the_index_declares_the_selection_and_gate_vocabularies() -> None:
    raw = _index()
    assert raw["guide_selections"]["crewmate_selected"] == {
        "picker": "members.roster-list",
        "entity": "crewmate",
    }
    assert raw["guide_gates"]["terminal_enabled"] == {"setting_id": None}
    # Every preview flag is a gate on the setting PREVIEW_FLAG_ENABLERS names.
    assert raw["guide_gates"]["preview_flag:mc-preview-artifact-deploy"] == {
        "setting_id": "developer.artifact-deploy"
    }


def test_a_validated_record_keeps_only_plan_ids_for_its_select_and_gate_steps() -> None:
    (record,) = guide_catalog.validate_actions(
        [{"id": "ui.show", "params": {"location_id": "shell.developer"}}]
    )
    assert record["step_meta"] == {
        "desktop:gate:developer_mode": {
            "kind": "gate",
            "gate": "developer_mode",
            "setting_id": "developer.developer-mode",
        },
        "mobile:gate:developer_mode": {
            "kind": "gate",
            "gate": "developer_mode",
            "setting_id": "developer.developer-mode",
        },
    }


@pytest.mark.parametrize(
    "step",
    [
        {"id": "a:gate:x", "kind": "gate", "gate": "x", "location": "shell.developer"},
        {"id": "a:select:x", "kind": "select", "location": "members.roster-list"},
        {"id": "a:x", "kind": "click", "location": "members.edit"},
        {"id": "a:gate:x", "kind": "gate", "gate": "x", "setting_id": 3},
    ],
)
def test_a_malformed_select_or_gate_step_leaves_the_plan_out(step: dict[str, Any]) -> None:
    plan = {
        "version": 2,
        "placements": [
            {"id": "any", "route": "/x", "steps": [step, {"id": "a:t", "location": "t"}]}
        ],
    }
    assert guide_catalog._plan_is_well_formed(plan) is False


def test_a_plan_ending_on_a_select_or_gate_step_is_left_out() -> None:
    plan = {
        "version": 2,
        "placements": [
            {"id": "any", "route": "/x", "steps": [{"id": "a:gate:x", "kind": "gate", "gate": "x"}]}
        ],
    }
    assert guide_catalog._plan_is_well_formed(plan) is False


# ── the gateway: gate pause / resume, an empty picker ──


def _store() -> GuideStore:
    return GuideStore(clock=lambda: 1000.0)


def _claimed(store: GuideStore, location_id: str, placement: str) -> dict[str, Any]:
    g = store.start(
        slot_key="chat-x",
        session_key="dashboard:chat-x",
        actions=[{"id": "ui.show", "params": {"location_id": location_id}}],
    )
    return store.claim(
        guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], placements=[placement]
    )


def _report(store: GuideStore, g: dict[str, Any], **over: Any) -> dict[str, Any]:
    ids = g["actions"][0]["step_ids"]
    kw: dict[str, Any] = {
        "guide_id": g["guide_id"],
        "tab_id": "tab-1",
        "revision": g["revision"],
        "action_index": 0,
        "step_index": g["step_index"],
        "outcome": "observed",
        "step_id": ids[g["step_index"]],
    }
    kw.update(over)
    return store.progress(**kw)


def test_a_gate_that_is_off_pauses_the_guide_naming_its_setting_and_resumes_in_place() -> None:
    store = _store()
    g = _claimed(store, "shell.developer", "desktop")
    assert g["blocker"] is None
    g = _report(store, g, outcome="target_missing", detail="gate_off")
    assert (g["status"], g["reason"], g["step_index"]) == ("target_missing", "gate_off", 0)
    assert g["blocker"] == {
        "kind": "gate_off",
        "gate": "developer_mode",
        "setting_id": "developer.developer-mode",
    }
    # The person turned it on: the same step comes back, then passes.
    g = _report(store, g, outcome="target_found")
    assert (g["status"], g["step_index"], g["blocker"]) == ("active", 0, None)
    g = _report(store, g)
    assert g["step_index"] == 1


def test_an_empty_picker_is_needs_selection_and_names_only_the_selection() -> None:
    store = _store()
    g = _claimed(store, "members.edit", "any")
    g = _report(store, g)  # the Crewmates preview is on: the select step is next
    g = _report(store, g, outcome="target_missing", detail="selection_empty")
    assert g["reason"] == "needs_selection"
    assert g["blocker"] == {"kind": "needs_selection", "selection": "crewmate_selected"}


@pytest.mark.parametrize(
    ("location_id", "placement", "detail"),
    [
        # A pointing step waits on no gate and no selection.
        ("chat.older-sessions", "desktop", "gate_off"),
        ("chat.older-sessions", "desktop", "selection_empty"),
        # A select step is not a gate, a gate step not a picker.
        ("schedule.pause", "any", "gate_off"),
        ("shell.developer", "desktop", "selection_empty"),
    ],
)
def test_a_blocker_is_reported_only_by_the_step_that_waits_on_it(
    location_id: str, placement: str, detail: str
) -> None:
    store = _store()
    g = _claimed(store, location_id, placement)
    with pytest.raises(GuideError) as exc:
        _report(store, g, outcome="target_missing", detail=detail)
    assert exc.value.code == "invalid_detail"


def test_a_guides_public_record_carries_no_entity_data() -> None:
    store = _store()
    g = _claimed(store, "members.edit", "any")
    g = _report(store, g)  # past the Crewmates preview's gate
    g = _report(store, g, outcome="target_missing", detail="selection_empty")
    record = g["actions"][0]
    # Only plan ids: the record's meta and the blocker name scopes, never a row.
    assert set(g["blocker"]) == {"kind", "selection"}
    assert record["step_meta"] == {
        "any:gate:preview_flag:mc-preview-crew": {
            "kind": "gate",
            "gate": "preview_flag:mc-preview-crew",
            "setting_id": "developer.crewmates",
        },
        "any:select:crewmate_selected": {"kind": "select", "selection": "crewmate_selected"},
    }


# ── the mid-guide re-plan ──


def _replan(store: GuideStore, g: dict[str, Any], placement: str, **over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "guide_id": g["guide_id"],
        "tab_id": "tab-1",
        "revision": g["revision"],
        "action_index": g["action_index"],
        "placement": placement,
    }
    kw.update(over)
    return store.replan(**kw)


def test_a_replan_at_a_shared_step_boundary_walks_the_new_placement_from_there() -> None:
    store = _store()
    g = _claimed(store, "shell.developer", "desktop")
    g = _report(store, g)  # the gate is on: step 1 is next
    g = _report(store, g, outcome="target_missing")  # the rail row left with the resize
    before = g["revision"]
    g = _replan(store, g, "mobile")
    record = g["actions"][0]
    assert record["placement"] == "mobile" and record["step_ids"] == record["placements"]["mobile"]
    assert (g["status"], g["step_index"], g["reason"]) == ("active", 1, "replanned")
    assert g["revision"] > before
    # The next report must name the new placement's step.
    g = _report(store, g)
    assert g["step_index"] == 2


def test_a_replan_where_the_walked_steps_differ_is_refused_and_moves_nothing() -> None:
    store = _store()
    g = _claimed(store, "chat.older-sessions", "desktop")
    g = _report(store, g)  # past the desktop sidebar toggle
    with pytest.raises(GuideError) as exc:
        _replan(store, g, "mobile")
    assert (exc.value.status, exc.value.code) == (409, "replan_not_at_boundary")
    held = store.status_for_caller(slot_key="chat-x", guide_id=g["guide_id"])
    assert held["actions"][0]["placement"] == "desktop" and held["revision"] == g["revision"]


def test_a_replan_at_the_first_step_is_always_a_boundary() -> None:
    store = _store()
    g = _claimed(store, "chat.older-sessions", "desktop")
    g = _replan(store, g, "mobile")
    assert g["actions"][0]["placement"] == "mobile" and g["step_index"] == 0


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"revision": 1}, "stale_revision"),
        ({"tab_id": "tab-2"}, "not_owner_tab"),
        ({"action_index": 1}, "wrong_step"),
        ({"placement": "tablet"}, "unknown_placement"),
    ],
)
def test_a_replan_is_revision_checked_and_owner_only(over: dict[str, Any], code: str) -> None:
    store = _store()
    g = _claimed(store, "chat.older-sessions", "desktop")
    target = over.pop("placement", "mobile")
    with pytest.raises(GuideError) as exc:
        _replan(store, g, target, **over)
    assert exc.value.code == code


def test_a_replan_of_a_fixed_action_is_refused() -> None:
    store = _store()
    g = store.start(
        slot_key="chat-x",
        session_key="dashboard:chat-x",
        actions=[{"id": "mcp.open_add", "params": {}}],
    )
    g = store.claim(guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"])
    with pytest.raises(GuideError) as exc:
        _replan(store, g, "mobile")
    assert exc.value.code == "replan_not_allowed"


# ── find_ui blockers ──


def test_find_ui_says_gate_off_before_any_other_blocker() -> None:
    gates = {"developer_mode": "developer.developer-mode"}
    blocker = mcp_guide._blocker_for(
        "unmounted",
        ("menu:shell.mobile-menu",),
        ("developer_mode", "not_on_sessions_page"),
        {"menu:shell.mobile-menu": "closed"},
        {"developer_mode": "unmet", "not_on_sessions_page": "unmet"},
        gates,
        frozenset(),
    )
    assert blocker == {
        "kind": "gate_off",
        "gate": "developer_mode",
        "setting_id": "developer.developer-mode",
    }


def test_find_ui_says_needs_selection_before_a_plain_predicate() -> None:
    blocker = mcp_guide._blocker_for(
        "unmounted",
        (),
        ("crewmate_selected", "has_open_sessions"),
        {},
        {"crewmate_selected": "unmet", "has_open_sessions": "unmet"},
        {},
        frozenset({"crewmate_selected"}),
    )
    assert blocker == {"kind": "needs_selection", "selection": "crewmate_selected"}


def test_find_ui_reports_no_selection_blocker_once_it_is_made_or_unknown() -> None:
    for state in ("met", "unknown"):
        assert (
            mcp_guide._blocker_for(
                "unmounted",
                (),
                ("crewmate_selected",),
                {},
                {"crewmate_selected": state},
                {},
                frozenset({"crewmate_selected"}),
            )
            is None
        )


def test_the_manifest_asks_a_tab_for_a_plans_selections_and_gates() -> None:
    manifest = guide_catalog.ui_build_manifest()
    assert "crewmate_selected" in manifest.plan_predicates["members.edit"]
    assert "developer_mode" in manifest.plan_predicates["shell.developer"]
    assert manifest.gates["developer_mode"] == "developer.developer-mode"
