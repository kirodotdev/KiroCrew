"""The generic ``ui.show`` guide action: catalog, eligibility, stored steps, find_ui.

``ui.show`` points at one indexed UI location. Its plan (version 2: one step
list per placement, each step with an id) is generated with the find_ui index
(``website/scripts/lib/ui-index.mjs`` ``guidePlanFor``); the gateway accepts
only an id whose packaged plan exists, records the placement the claiming tab
walks, and find_ui hands out a ``ui.show`` guide_ref only for such an id.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import guide_catalog, ui_index
from kiro_crew.dashboard.guide_runs import GuideError, GuideStore

_REPO = Path(__file__).resolve().parents[1]
_PLANS_TS = _REPO / "website" / "src" / "uiLocations" / "guidePlans.gen.ts"

#: The generator's caution list (``GUIDE_CAUTION_IDS``): destructive or
#: data-replacing controls a guide may point at, with a caution on its last
#: step. Whether one gets a plan still depends on its prerequisites.
_CAUTION = (
    "agents.delete",
    "apps.detail.uninstall",
    "apps.library.tile-uninstall",
    "backup.import-file",
    "composer.automation.stop-monitor",
    "notifications.clear-all",
    "notifications.page-clear-all",
    "schedule.cancel-run",
    "schedule.delete",
    "sessions.list-menu.clean-up",
)
#: Caution controls whose prerequisites the guide cannot walk: never planned.
_CAUTION_UNPLANNED = (
    "apps.detail.uninstall",
    "notifications.clear-all",
    "notifications.page-clear-all",
    "schedule.cancel-run",
)
_ALLOWED_REQUIREMENTS = {"viewport", "shown_by", "preview_flag", "condition"}


def _index() -> dict[str, Any]:
    return json.loads(ui_index.INDEX_PATH.read_text(encoding="utf-8"))


def _by_id() -> dict[str, dict[str, Any]]:
    return {loc["id"]: loc for loc in _index()["locations"]}


@pytest.fixture(autouse=True)
def _fresh_caches(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    guide_catalog.ui_show_plans.cache_clear()
    ui_index._cache.update(key=None, index=None)
    _floor_monkeypatch.setattr(ui_index, "AUTO_INDEX_PATH", tmp_path / "no-auto-tier.json")
    yield
    guide_catalog.ui_show_plans.cache_clear()


# ── catalog ──


def test_ui_show_is_listed_as_a_pointer_whose_steps_depend_on_the_location() -> None:
    entry = next(a for a in guide_catalog.list_actions() if a["id"] == "ui.show")
    assert entry["mutates"] is False
    assert entry["step_count"] is None
    assert entry["params_schema"]["required"] == ["location_id"]


def test_ui_show_stores_every_placements_step_ids_and_no_step_count_until_claimed() -> None:
    (record,) = guide_catalog.validate_actions(
        [{"id": "ui.show", "params": {"location_id": "chat.older-sessions"}}]
    )
    assert record == {
        "id": "ui.show",
        "params": {"location_id": "chat.older-sessions"},
        "plan_version": 2,
        "placements": {
            "desktop": ["desktop:chat.sessions-sidebar-toggle", "desktop:chat.older-sessions"],
            "mobile": ["mobile:chat.mobile-sessions-toggle", "mobile:chat.older-sessions"],
        },
        # Only select and gate steps have an entry; these all only point.
        "step_meta": {},
        "step_count": None,
        # The packaged index's own digest, which the browser bundle carries too.
        "build_digest": guide_catalog.ui_build_manifest().build_digest,
    }
    assert record["build_digest"].startswith("sha256:")
    (menu_item,) = guide_catalog.validate_actions(
        [{"id": "ui.show", "params": {"location_id": "sessions.list-menu.view"}}]
    )
    claimed = guide_catalog.claim_placement(menu_item, "desktop")
    assert claimed["step_count"] == 3 and claimed["step_kinds"] == ["ui"] * 3
    assert claimed["step_ids"] == menu_item["placements"]["desktop"]
    with pytest.raises(guide_catalog.GuideCatalogError) as exc:
        guide_catalog.claim_placement(menu_item, "tablet")
    assert exc.value.code == "unknown_placement"


@pytest.mark.parametrize(
    "location_id",
    [
        *_CAUTION_UNPLANNED,
        # A runtime condition the guide cannot set up (a job must be open).
        "schedule.run-now",
        # Settings are settings.show's; a generated location has no plan.
        "setting:chat.link-previews",
        "page.chat",
        # Its own guide action covers it.
        "mcp.add-custom",
        "no.such-location",
    ],
)
def test_ui_show_refuses_a_location_without_a_packaged_plan(location_id: str) -> None:
    with pytest.raises(guide_catalog.GuideCatalogError) as exc:
        guide_catalog.validate_actions([{"id": "ui.show", "params": {"location_id": location_id}}])
    assert exc.value.code == "unknown_location"


@pytest.mark.parametrize(
    "params",
    [{}, {"location_id": ""}, {"location_id": 3}, {"location_id": "x" * 121}],
)
def test_ui_show_refuses_missing_or_malformed_ids(params: dict[str, Any]) -> None:
    with pytest.raises(guide_catalog.GuideCatalogError) as exc:
        guide_catalog.validate_actions([{"id": "ui.show", "params": params}])
    assert exc.value.code == "invalid_params"


def test_ui_show_refuses_extra_parameters() -> None:
    with pytest.raises(guide_catalog.GuideCatalogError) as exc:
        guide_catalog.validate_actions(
            [{"id": "ui.show", "params": {"location_id": "chat.older-sessions", "route": "/x"}}]
        )
    assert exc.value.code == "invalid_params"


def test_ui_show_carries_the_named_item_only_for_a_plan_that_has_a_choose_step() -> None:
    (record,) = guide_catalog.validate_actions(
        [{"id": "ui.show", "params": {"location_id": "agents.delete", "pick": " Helper "}}]
    )
    assert record["params"] == {"location_id": "agents.delete", "pick": "Helper"}
    # No choose step: there is nothing for the name to narrow.
    with pytest.raises(guide_catalog.GuideCatalogError) as exc:
        guide_catalog.validate_actions(
            [{"id": "ui.show", "params": {"location_id": "chat.older-sessions", "pick": "Helper"}}]
        )
    assert exc.value.code == "invalid_params"


def test_ui_show_refuses_a_pick_for_a_list_whose_rows_cannot_name_their_entity() -> None:
    plans = guide_catalog.ui_show_plans()
    session_plans = [
        lid
        for lid, plan in plans.items()
        if any(
            st.get("kind") == guide_catalog.STEP_KIND_SELECT
            and st.get("location") == "sessions.list"
            for p in plan["placements"]
            for st in p["steps"]
        )
    ]
    assert session_plans, "expected a plan that has the user choose a session"
    with pytest.raises(guide_catalog.GuideCatalogError) as exc:
        guide_catalog.validate_actions(
            [{"id": "ui.show", "params": {"location_id": session_plans[0], "pick": "Morning"}}]
        )
    assert exc.value.code == "invalid_params"
    # The mirror the tab enforces names the same lists.
    ts = (
        Path(__file__).resolve().parents[1] / "website" / "src" / "guide" / "guideActions.ts"
    ).read_text(encoding="utf-8")
    for picker in guide_catalog.UI_SHOW_PICKABLE_PICKERS:
        assert f"'{picker}'" in ts


def test_an_unreadable_index_leaves_ui_show_with_no_plans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(guide_catalog, "_UI_INDEX_PATH", tmp_path / "missing.json")
    guide_catalog.ui_show_plans.cache_clear()
    assert guide_catalog.ui_show_plans() == {}
    with pytest.raises(guide_catalog.GuideCatalogError):
        guide_catalog.validate_actions(
            [{"id": "ui.show", "params": {"location_id": "chat.older-sessions"}}]
        )


def _mangle(plan: dict[str, Any], how: str) -> None:
    if how == "version_1":
        plan["version"] = 1
    elif how == "no_version":
        del plan["version"]
    elif how == "duplicate_step_id":
        plan["placements"][0]["steps"][1]["id"] = plan["placements"][0]["steps"][0]["id"]
    elif how == "missing_step_id":
        del plan["placements"][0]["steps"][0]["id"]
    elif how == "duplicate_placement_id":
        plan["placements"][1]["id"] = plan["placements"][0]["id"]
    elif how == "too_many_steps":
        plan["placements"][0]["steps"] = [
            {**plan["placements"][0]["steps"][0], "id": f"desktop:s{i}"} for i in range(8)
        ]
    elif how == "bad_requires":
        plan["placements"][0]["steps"][0]["requires"] = "has_open_sessions"


@pytest.mark.parametrize(
    "how",
    [
        "version_1",
        "no_version",
        "duplicate_step_id",
        "missing_step_id",
        "duplicate_placement_id",
        "too_many_steps",
        "bad_requires",
    ],
)
def test_a_malformed_plan_is_left_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    real = _index()
    for loc in real["locations"]:
        if loc["id"] == "chat.older-sessions":
            _mangle(loc["guide_plan"], how)
    path = tmp_path / "ui-index.generated.json"
    path.write_text(json.dumps(real), encoding="utf-8")
    monkeypatch.setattr(guide_catalog, "_UI_INDEX_PATH", path)
    guide_catalog.ui_show_plans.cache_clear()
    plans = guide_catalog.ui_show_plans()
    assert "chat.older-sessions" not in plans and "sessions.list-menu" in plans


# ── eligibility, as generated ──


def test_a_caution_location_is_planned_with_its_last_step_marked() -> None:
    by_id = _by_id()
    planned = 0
    for lid in _CAUTION:
        assert lid in by_id, f"{lid} is no longer a location; update the caution list"
        loc = by_id[lid]
        assert loc["guide_policy"] == "caution", lid
        if "guide_plan" not in loc:
            continue
        planned += 1
        for pl in loc["guide_plan"]["placements"]:
            assert pl["steps"][-1].get("caution") is True, lid
            assert all("caution" not in st for st in pl["steps"][:-1]), lid
    assert planned >= 1
    # No other plan carries the mark, and nothing in the committed index is denied
    # unless it is one of the agent's own ceiling's controls (the approval mode).
    for lid, loc in by_id.items():
        if lid in _CAUTION or "guide_plan" not in loc:
            continue
        assert all(
            "caution" not in st for pl in loc["guide_plan"]["placements"] for st in pl["steps"]
        ), lid
    assert [lid for lid, loc in by_id.items() if loc.get("guide_policy") == "deny"] == [
        "composer.approval-mode"
    ]


def test_a_caution_plan_is_accepted_and_a_malformed_mark_is_not() -> None:
    (rec,) = guide_catalog.validate_actions(
        [{"id": "ui.show", "params": {"location_id": "sessions.list-menu.clean-up"}}]
    )
    assert rec["placements"]
    plan = _by_id()["sessions.list-menu.clean-up"]["guide_plan"]
    bad = json.loads(json.dumps(plan))
    bad["placements"][0]["steps"][-1]["caution"] = "yes"
    assert not guide_catalog._plan_is_well_formed(bad)
    gate = json.loads(json.dumps(plan))
    gate["placements"][0]["steps"].insert(
        0, {"id": "x:gate:g", "kind": "gate", "gate": "g", "caution": True}
    )
    assert not guide_catalog._plan_is_well_formed(gate)


def test_every_plan_is_a_version_2_plan_for_a_plain_curated_location() -> None:
    plans = {lid: loc for lid, loc in _by_id().items() if "guide_plan" in loc}
    assert len(plans) >= 20 and "chat.older-sessions" in plans
    raw = _index()
    handled = (
        set(raw["runtime_predicates"]) | set(raw["guide_selections"]) | set(raw["guide_gates"])
    )
    for lid, loc in plans.items():
        assert loc.get("tier") == "curated", lid
        assert "setting_id" not in loc and "guide_ref" not in loc, lid
        assert loc.get("label_kind") != "description", lid
        for p in loc["placements"]:
            assert {r["kind"] for r in p["requires"]} <= _ALLOWED_REQUIREMENTS, lid
            # A condition on a guided location is one the guide can check.
            assert {r["id"] for r in p["requires"] if r["kind"] == "condition"} <= handled, lid
        plan = loc["guide_plan"]
        assert plan["version"] == 2 and "step_count" not in plan, lid
        assert all(pl["steps"][-1]["location"] == lid for pl in plan["placements"]), lid
        ids = [st["id"] for pl in plan["placements"] for st in pl["steps"]]
        assert len(ids) == len(set(ids)), lid
        for pl in plan["placements"]:
            last = pl["steps"][-1]
            assert "scope" not in last and "kind" not in last, lid
            for st in pl["steps"][:-1]:
                if st.get("kind") == "gate":
                    # A gate points at nothing; it names its gate and setting.
                    assert "location" not in st and st["gate"] in raw["guide_gates"], lid
                    assert st.get("setting_id") == raw["guide_gates"][st["gate"]]["setting_id"], lid
                elif st.get("kind") == "select":
                    # A select step points at its scope's picker; it opens nothing.
                    sel = raw["guide_selections"][st["selection"]]
                    assert st["location"] == sel["picker"] and "scope" not in st, lid
                else:
                    # Every reveal step names the compiled scope it opens.
                    assert st["scope"] in raw["reveal_scopes"], lid
            # Outermost first: gates lead (a select step follows only its
            # picker's own reveal steps).
            gate_flags = [st.get("kind") == "gate" for st in pl["steps"]]
            assert gate_flags == sorted(gate_flags, reverse=True), lid


def test_the_older_sessions_plan_reveals_the_sidebar_first_on_each_viewport() -> None:
    plan = _by_id()["chat.older-sessions"]["guide_plan"]
    desktop, mobile = plan["placements"]
    assert desktop == {
        "id": "desktop",
        "route": "/chat",
        "viewport": "desktop",
        "steps": [
            {
                "id": "desktop:chat.sessions-sidebar-toggle",
                "location": "chat.sessions-sidebar-toggle",
                "label_key": "pages.chatPage.show_sessions_sidebar",
                "when": "sessions_sidebar_collapsed",
                # The sidebar's collapse owner reports this scope open.
                "scope": "chat.sessions-sidebar",
                # The toggle's own conditions, evaluated live in the tab.
                "requires": ["has_open_sessions", "full_dashboard"],
            },
            {
                "id": "desktop:chat.older-sessions",
                "location": "chat.older-sessions",
                "label_key": "pages.chatSidebar.older_sessions_2",
            },
        ],
    }
    assert mobile["viewport"] == "mobile"
    assert [s["location"] for s in mobile["steps"]] == [
        "chat.mobile-sessions-toggle",
        "chat.older-sessions",
    ]
    assert mobile["steps"][0]["scope"] == "chat.sessions-drawer"


def test_the_browser_module_carries_exactly_the_index_plans() -> None:
    text = _PLANS_TS.read_text(encoding="utf-8")
    marker = "export const GUIDE_PLANS: Readonly<Record<string, UiGuidePlan>> = "
    browser = json.loads(text[text.index(marker) + len(marker) :])
    index = {lid: loc["guide_plan"] for lid, loc in _by_id().items() if "guide_plan" in loc}
    assert browser == index


def _ts_const(text: str, name: str) -> Any:
    start = text.index(f"export const {name}")
    body = text[text.index("= ", start) + 2 :]
    return json.JSONDecoder().raw_decode(body)[0]


def test_the_browser_module_and_the_index_carry_one_build_digest() -> None:
    text = _PLANS_TS.read_text(encoding="utf-8")
    raw = _index()
    manifest = guide_catalog.ui_build_manifest()
    assert _ts_const(text, "GUIDE_BUILD_DIGEST") == raw["build_digest"] == manifest.build_digest
    assert set(_ts_const(text, "GUIDE_OBSERVABLE_IDS")) == manifest.observable
    assert set(_ts_const(text, "GUIDE_REVEAL_SCOPES")) == manifest.scopes
    assert manifest.plan_scopes["chat.older-sessions"] == (
        "chat.sessions-drawer",
        "chat.sessions-sidebar",
    )
    # Runtime predicates, selections and gates: every id a tab reports met/unmet.
    assert manifest.predicates == (
        frozenset(raw["runtime_predicates"])
        | frozenset(raw["guide_selections"])
        | frozenset(raw["guide_gates"])
    )
    assert manifest.selections == frozenset(raw["guide_selections"])
    assert manifest.gates["developer_mode"] == "developer.developer-mode"
    assert manifest.gates["terminal_enabled"] is None
    assert manifest.plan_predicates["chat.older-sessions"] == (
        "full_dashboard",
        "has_open_sessions",
    )


def test_an_index_without_a_well_formed_digest_has_an_empty_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _index()
    raw["build_digest"] = "not-a-digest"
    path = tmp_path / "ui-index.generated.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(guide_catalog, "_UI_INDEX_PATH", path)
    guide_catalog.ui_build_manifest.cache_clear()
    try:
        assert guide_catalog.ui_build_manifest().build_digest == ""
        assert guide_catalog.ui_build_manifest().observable == frozenset()
    finally:
        guide_catalog.ui_build_manifest.cache_clear()


# ── stored steps drive the guide run ──


def _store() -> GuideStore:
    return GuideStore(clock=lambda: 1000.0)


def _started(store: GuideStore, location_id: str, slot: str = "chat-x") -> dict[str, Any]:
    return store.start(
        slot_key=slot,
        session_key=f"dashboard:{slot}",
        actions=[{"id": "ui.show", "params": {"location_id": location_id}}],
    )


def _report(store: GuideStore, g: dict[str, Any], **over: Any) -> dict[str, Any]:
    ids = g["actions"][0].get("step_ids")
    kw: dict[str, Any] = {
        "guide_id": g["guide_id"],
        "tab_id": "tab-1",
        "revision": g["revision"],
        "action_index": 0,
        "step_index": g["step_index"],
        "outcome": "observed",
        "step_id": ids[g["step_index"]] if ids else None,
    }
    kw.update(over)
    return store.progress(**kw)


def test_a_ui_show_guide_walks_its_claimed_steps_and_completes_on_the_tabs_reports() -> None:
    store = _store()
    g = _started(store, "sessions.list-menu.view")
    g = store.claim(
        guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], placements=["desktop"]
    )
    record = g["actions"][0]
    assert record["placement"] == "desktop" and record["step_count"] == 3
    assert record["step_ids"] == record["placements"]["desktop"]
    for expected in (1, 2):
        g = _report(store, g)
        assert g["status"] == "active" and g["step_index"] == expected
    g = _report(store, g)
    assert g["status"] == "completed"


def test_a_placement_of_another_length_is_walked_by_its_own_count() -> None:
    # The equal-step-count contract is gone: each placement keeps its own list.
    store = _store()
    g = _started(store, "chat.older-sessions")
    g = store.claim(
        guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], placements=["mobile"]
    )
    assert g["actions"][0]["step_ids"] == [
        "mobile:chat.mobile-sessions-toggle",
        "mobile:chat.older-sessions",
    ]


def test_a_step_past_the_plan_is_not_the_current_step() -> None:
    store = _store()
    g = _started(store, "chat.older-sessions", "chat-y")
    g = store.claim(
        guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], placements=["desktop"]
    )
    with pytest.raises(GuideError) as exc:
        _report(store, g, step_index=1, step_id=g["actions"][0]["step_ids"][1])
    assert exc.value.code == "wrong_step"


# ── version-2 claim and step ids ──


def test_a_claim_without_a_placement_for_a_ui_show_action_is_refused() -> None:
    store = _store()
    g = _started(store, "chat.older-sessions")
    for placements, code in (
        (None, "placement_required"),
        ([None], "placement_required"),
        (["tablet"], "unknown_placement"),
        (["desktop", None], "invalid_placements"),
        ("desktop", "invalid_placements"),
    ):
        with pytest.raises(GuideError) as exc:
            store.claim(
                guide_id=g["guide_id"],
                tab_id="tab-1",
                revision=g["revision"],
                placements=placements,
            )
        assert exc.value.code == code, placements
    # Nothing moved: the guide is still offered at its first revision.
    assert store.status_for_caller(slot_key="chat-x")["status"] == "offered"


def test_another_actions_placement_must_be_none() -> None:
    store = _store()
    g = store.start(
        slot_key="chat-x",
        session_key="dashboard:chat-x",
        actions=[{"id": "mcp.open_add", "params": {}}],
    )
    with pytest.raises(GuideError) as exc:
        store.claim(
            guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], placements=["desktop"]
        )
    assert exc.value.code == "invalid_placements"


@pytest.mark.parametrize("bad", [None, "desktop:chat.older-sessions", "mobile:x", 0])
def test_a_report_naming_anything_but_the_recorded_step_id_is_refused(bad: object) -> None:
    store = _store()
    g = _started(store, "chat.older-sessions")
    g = store.claim(
        guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], placements=["desktop"]
    )
    with pytest.raises(GuideError) as exc:
        _report(store, g, step_id=bad)
    assert exc.value.code == "wrong_step"
    # The other placement's id for the same position is not this guide's step.
    with pytest.raises(GuideError):
        _report(store, g, step_id="mobile:chat.mobile-sessions-toggle")


def test_a_resume_must_name_the_earlier_steps_recorded_id() -> None:
    store = _store()
    g = _started(store, "sessions.list-menu.view")
    g = store.claim(
        guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], placements=["desktop"]
    )
    ids = g["actions"][0]["step_ids"]
    g = _report(store, g)
    g = _report(store, g, outcome="target_missing")
    with pytest.raises(GuideError) as exc:
        _report(store, g, outcome="target_found", resume_step_index=0, resume_step_id=ids[1])
    assert exc.value.code == "invalid_resume_step"
    g = _report(store, g, outcome="target_found", resume_step_index=0, resume_step_id=ids[0])
    assert g["status"] == "active" and g["step_index"] == 0


def test_a_fixed_actions_report_carries_no_step_id() -> None:
    store = _store()
    g = store.start(
        slot_key="chat-x",
        session_key="dashboard:chat-x",
        actions=[{"id": "mcp.open_add", "params": {}}],
    )
    g = store.claim(guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"])
    with pytest.raises(GuideError) as exc:
        _report(store, g, step_id="desktop:x")
    assert exc.value.code == "invalid_step_id"


def test_a_takeover_may_repick_only_an_action_not_yet_started() -> None:
    store = _store()
    g = _started(store, "sessions.list-menu.view")
    g = store.claim(
        guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], placements=["desktop"]
    )
    # At its first step a phone may take it over on its own placement.
    g = store.claim(
        guide_id=g["guide_id"],
        tab_id="tab-2",
        revision=g["revision"],
        take_over=True,
        placements=["mobile"],
    )
    assert g["actions"][0]["placement"] == "mobile"
    g = _report(store, g, tab_id="tab-2")
    with pytest.raises(GuideError) as exc:
        store.claim(
            guide_id=g["guide_id"],
            tab_id="tab-1",
            revision=g["revision"],
            take_over=True,
            placements=["desktop"],
        )
    assert exc.value.code == "placement_locked"
    # The same placement (or none) is fine.
    g = store.claim(
        guide_id=g["guide_id"],
        tab_id="tab-1",
        revision=g["revision"],
        take_over=True,
        placements=["mobile"],
    )
    assert g["owner_tab"] == "tab-1" and g["step_index"] == 1


def test_a_replay_forgets_the_claimed_placement() -> None:
    store = _store()
    g = _started(store, "chat.older-sessions")
    g = store.claim(
        guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], placements=["desktop"]
    )
    g = _report(store, g)
    g = _report(store, g)
    assert g["status"] == "completed"
    g = store.replay(guide_id=g["guide_id"], revision=g["revision"])
    record = g["actions"][0]
    assert "placement" not in record and "step_ids" not in record and record["step_count"] is None
    g = store.claim(
        guide_id=g["guide_id"], tab_id="tab-1", revision=g["revision"], placements=["mobile"]
    )
    assert g["actions"][0]["placement"] == "mobile"


# ── find_ui hands out the binding ──


def _top(query: str, path: Path | None = None) -> dict[str, Any]:
    d = ui_index.find_ui(query, "en", **({"path": path} if path else {}))
    assert d["status"] == "ok", d
    return d["results"][0]


def test_find_ui_offers_ui_show_for_an_eligible_location() -> None:
    top = _top("older sessions")
    assert top["id"] == "chat.older-sessions"
    assert top["guide_ref"] == {
        "action_id": "ui.show",
        "params": {"location_id": "chat.older-sessions"},
    }


def test_find_ui_offers_a_destructive_control_with_its_caution() -> None:
    d = ui_index.find_ui("clean up sessions", "en")
    hit = next((r for r in d.get("results", []) if r["id"] == "sessions.list-menu.clean-up"), None)
    assert hit is not None, d
    assert hit["caution"] is True
    assert hit["guide_ref"] == {
        "action_id": "ui.show",
        "params": {"location_id": "sessions.list-menu.clean-up"},
    }


def test_find_ui_quotes_the_pages_own_words_for_what_a_deletion_removes() -> None:
    # Delete crewmate only unbinds; Mate describes it from the page's own
    # notice, never from a guess that it wipes chats and notes for good.
    for locale, want in (
        ("en", "Deleting a crewmate only unbinds it from new sessions."),
        ("zh-CN", None),
    ):
        d = ui_index.find_ui("delete a crewmate", locale)
        hit = next((r for r in d.get("results", []) if r["id"] == "agents.delete"), None)
        assert hit is not None, d
        assert hit["caution"] is True
        text = hit["caution_text"]
        assert text and text != hit["label"]
        if want:
            assert text.startswith(want)
            assert "for good" not in text and "permanent" not in text
    # A caution location without the page's own words carries none.
    d = ui_index.find_ui("clean up sessions", "en")
    hit = next(r for r in d["results"] if r["id"] == "sessions.list-menu.clean-up")
    assert "caution_text" not in hit


def test_the_delete_crewmate_guide_step_carries_the_pages_caution_key() -> None:
    steps = _steps("agents.delete", "any")
    assert (
        steps[-1]["caution_key"]
        == "pages.kiroCrewAgentsPage.deleting_a_crew_unbinds_it_from_new_sessions_its"
    )
    assert all("caution_key" not in s for s in steps[:-1])


def test_a_caution_key_off_a_caution_step_is_a_malformed_plan() -> None:
    plan = json.loads(json.dumps(guide_catalog.ui_show_plans()["agents.delete"]))
    plan["placements"][0]["steps"][1]["caution_key"] = "pages.kiroCrewAgentsPage.danger_zone"
    assert not guide_catalog._plan_is_well_formed(plan)


def test_a_caution_text_key_on_a_non_caution_location_breaks_the_index(tmp_path: Path) -> None:
    real = _index()
    loc = next(x for x in real["locations"] if x["id"] == "chat.older-sessions")
    loc["caution_key"] = "pages.kiroCrewAgentsPage.deleting_a_crew_unbinds_it_from_new_sessions_its"
    path = tmp_path / "ui-index.generated.json"
    path.write_text(json.dumps(real), encoding="utf-8")
    out = ui_index.find_ui("older sessions", "en", path=path)
    assert out["status"] == "unavailable" and out["reason"] == "malformed caution text"


def test_find_ui_never_offers_ui_show_for_a_denied_location(tmp_path: Path) -> None:
    # Even an index that (wrongly) gave a denied control a plan is not handed
    # out: a denied policy is never guidable, whatever plan rides along.
    real = _index()
    clean_up = next(x for x in real["locations"] if x["id"] == "sessions.list-menu.clean-up")
    clean_up["guide_policy"] = "deny"
    path = tmp_path / "ui-index.generated.json"
    path.write_text(json.dumps(real), encoding="utf-8")
    d = ui_index.find_ui("clean up sessions", "en", path=path)
    hit = next((r for r in d.get("results", []) if r["id"] == "sessions.list-menu.clean-up"), None)
    assert hit is not None, d
    assert "guide_ref" not in hit and "caution" not in hit


def test_find_ui_keeps_a_locations_own_guide_action() -> None:
    d = ui_index.find_ui("add custom mcp server", "en")
    hit = next(r for r in d["results"] if r["id"] == "mcp.add-custom")
    assert hit["guide_ref"] == {"action_id": "mcp.open_add"}


# ── walks behind a pick, an editor section or a folded roster ──


def _steps(lid: str, placement: str) -> list[dict[str, Any]]:
    plan = guide_catalog.ui_show_plans()[lid]
    (pl,) = [p for p in plan["placements"] if p["id"] == placement]
    return list(pl["steps"])


def _pointing(lid: str, placement: str) -> list[dict[str, Any]]:
    """The steps after a Crewmates control's preview gate, which opens every one."""
    steps = _steps(lid, placement)
    assert steps[0] == {
        "id": f"{placement}:gate:preview_flag:mc-preview-crew",
        "kind": "gate",
        "gate": "preview_flag:mc-preview-crew",
        "setting_id": "developer.crewmates",
    }
    return steps[1:]


def test_delete_crewmate_walks_through_the_editor_and_never_finishes_on_its_own() -> None:
    steps = _steps("agents.delete", "any")
    assert [s.get("kind") for s in steps] == ["select", None, None]
    assert steps[0]["selection"] == "crewmate_editor_open"
    assert steps[0]["location"] == "agents.crew-list"
    # The Danger zone section is a step of its own, pointed at while it is
    # closed; the destructive step keeps its caution and needs no blocker.
    assert steps[1]["location"] == "agents.section-danger"
    assert "caution" not in steps[1]
    assert "requires" not in steps[-1]
    assert steps[-1]["caution"] is True


def test_crewmate_memory_and_model_walk_through_the_editor() -> None:
    memory = _steps("agents.manage-memory", "any")
    assert memory[0]["selection"] == "crewmate_editor_open"
    assert memory[1]["location"] == "agents.section-place"
    assert memory[-1]["requires"] == ["crewmate_memory_manageable"]
    model = _steps("agents.model", "any")
    assert [s["location"] for s in model] == [
        "agents.crew-list",
        "agents.section-model",
        "agents.model",
    ]
    assert "requires" not in model[-1]


def test_new_crewmate_reopens_a_folded_roster_through_the_switcher_first() -> None:
    desktop = [s["location"] for s in _pointing("members.add-menu.new", "desktop")]
    assert desktop == [
        "members.switcher",
        "members.switcher.show-roster",
        "members.add-menu",
        "members.add-menu.new",
    ]
    # The folded roster implies an open crewmate: no "choose the crewmate" step.
    assert all(s.get("kind") != "select" for s in _pointing("members.add-menu.new", "desktop"))
    phone = [s["location"] for s in _pointing("members.add-menu.new", "mobile")]
    assert phone == ["members.back", "members.add-menu", "members.add-menu.new"]
    add_menu = _pointing("members.add-menu.new", "mobile")[1]
    assert add_menu["requires"] == ["has_crewmates"]


def test_add_menu_itself_honours_its_reveals() -> None:
    desktop = [s["location"] for s in _pointing("members.add-menu", "desktop")]
    assert desktop[:2] == ["members.switcher", "members.switcher.show-roster"]
    assert desktop[-1] == "members.add-menu"
