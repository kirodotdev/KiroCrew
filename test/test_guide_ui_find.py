"""The ``ui.find`` guide action: find a control by its on-screen name.

The agent names a page this build's index knows, the control's label in the
dashboard's language and optionally a role and a container hint; the tab
searches (and probes containers) itself and reports only found / ambiguous /
none, a count, a role and a registered id (``guide_catalog.clean_find_report``).
``find_ui`` hands out a ready ``find_ref`` for a result with no planned guide.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import guide_catalog, mcp_guide, ui_index
from kiro_crew.dashboard.guide_runs import REASON_NOT_FOUND, GuideError, GuideStore


@pytest.fixture(autouse=True)
def _no_auto_tier(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    _floor_monkeypatch.setattr(guide_catalog, "_UI_AUTO_INDEX_PATH", tmp_path / "no-auto.json")
    _floor_monkeypatch.setattr(ui_index, "AUTO_INDEX_PATH", tmp_path / "no-auto.json")
    guide_catalog.ui_find_routes.cache_clear()
    guide_catalog.guidable_settings.cache_clear()
    ui_index._cache.update(key=None, index=None)
    yield
    guide_catalog.ui_find_routes.cache_clear()
    ui_index._cache.update(key=None, index=None)


def _find(**params: Any) -> dict[str, Any]:
    (record,) = guide_catalog.validate_actions([{"id": "ui.find", "params": params}])
    return record


def _refused(code: str, **params: Any) -> None:
    with pytest.raises(guide_catalog.GuideCatalogError) as err:
        guide_catalog.validate_actions([{"id": "ui.find", "params": params}])
    assert err.value.code == code, err.value.message


# ── catalog ──


def test_ui_find_is_a_two_step_pointer() -> None:
    entry = next(a for a in guide_catalog.list_actions() if a["id"] == "ui.find")
    assert entry["mutates"] is False
    assert entry["step_count"] == 2
    assert entry["params_schema"]["required"] == ["label"]
    assert entry["params_schema"]["properties"]["role"]["enum"] == list(guide_catalog.FIND_ROLES)


def test_ui_find_keeps_a_known_route_label_role_and_container() -> None:
    record = _find(route="/schedule", label="Run Now", role="button", container="Jobs")
    assert record == {
        "id": "ui.find",
        "params": {"route": "/schedule", "label": "Run Now", "role": "button", "container": "Jobs"},
        "step_count": 2,
    }


def test_ui_find_without_a_route_stays_on_the_current_page() -> None:
    assert _find(label="Notifications")["params"] == {"label": "Notifications"}


def test_ui_find_accepts_a_bare_pathname_of_a_known_route() -> None:
    # '/capabilities?tab=mcp' is a known route, so '/capabilities' is too.
    assert _find(route="/capabilities", label="Add Custom")["params"]["route"] == "/capabilities"


@pytest.mark.parametrize(
    "route",
    ["/settings/security", "/settings/computer-use", "/settings/secrets/x", "/SETTINGS/Security/"],
)
def test_ui_find_never_opens_a_trust_root_page(route: str) -> None:
    _refused("sensitive_page", route=route, label="Profiles")


def test_ui_find_opens_the_instances_tab() -> None:
    assert (
        _find(route="/settings/instances", label="Remote Crew")["params"]["route"]
        == "/settings/instances"
    )


@pytest.mark.parametrize(
    "route", ["/no-such-page", "//evil.example", "https://x.example/a", "schedule", "/schedule\\x"]
)
def test_ui_find_refuses_a_route_the_index_does_not_know(route: str) -> None:
    with pytest.raises(guide_catalog.GuideCatalogError) as err:
        guide_catalog.validate_actions(
            [{"id": "ui.find", "params": {"route": route, "label": "X"}}]
        )
    assert err.value.code in {"unknown_route", "invalid_params"}


def test_ui_find_refuses_an_unknown_route_with_a_query() -> None:
    # A known pathname with a query the index never names is not accepted.
    _refused("unknown_route", route="/schedule?evil=1", label="Run Now")


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"label": ""},
        {"label": "   "},
        {"label": "x" * 81},
        {"label": "Run Now", "role": "slider"},
        {"label": "Run Now", "caution": "yes"},
        {"label": "Run Now", "selector": "#run"},
        {"label": "see https://x.example"},
        {"label": "<b>Run</b>"},
        {"label": "Run Now", "container": "[a](b)"},
        {"label": "Run\u202eNow"},
    ],
)
def test_ui_find_refuses_bad_params(params: dict[str, Any]) -> None:
    with pytest.raises(guide_catalog.GuideCatalogError):
        guide_catalog.validate_actions([{"id": "ui.find", "params": params}])


@pytest.mark.parametrize("label", ["Approve all", "Grant access", "Trust this app", "Allow"])
def test_ui_find_refuses_a_control_that_widens_the_agents_reach(label: str) -> None:
    _refused("sensitive_label", route="/schedule", label=label)


@pytest.mark.parametrize("label", ["Delete", "Remove server", "Uninstall", "Reset", "Clear all"])
def test_a_removal_label_always_carries_caution(label: str) -> None:
    assert _find(route="/schedule", label=label)["params"]["caution"] is True


@pytest.mark.parametrize(
    "label", ["Sign out", "Deny", "Deploy anyway", "Clear selection", "Run Now"]
)
def test_a_non_removal_label_is_a_plain_pointer(label: str) -> None:
    assert "caution" not in _find(route="/schedule", label=label)["params"]


def test_the_agent_may_mark_caution_itself() -> None:
    assert _find(route="/schedule", label="Löschen", caution=True)["params"]["caution"] is True


# ── what a tab may report ──


def test_a_find_report_carries_ids_and_counts_only() -> None:
    clean = guide_catalog.clean_find_report
    assert clean({"result": "found", "count": 1, "role": "button"}) == {
        "result": "found",
        "count": 1,
        "role": "button",
    }
    assert clean(
        {"result": "found", "count": 1, "location_id": "schedule.run-now", "label_key": "a.b_c"}
    ) == {"result": "found", "count": 1, "location_id": "schedule.run-now", "label_key": "a.b_c"}
    assert clean({"result": "ambiguous", "count": 3}) == {"result": "ambiguous", "count": 3}
    assert clean({"result": "none", "count": 0}) == {"result": "none", "count": 0}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"result": "found", "count": 1, "name": "Run Now"},
        {"result": "found", "count": 1, "contexts": ["Jobs"]},
        {"result": "maybe", "count": 1},
        {"result": "found", "count": 2},
        {"result": "none", "count": 1},
        {"result": "ambiguous", "count": 0},
        {"result": "ambiguous", "count": 51},
        {"result": "found", "count": True},
        {"result": "found", "count": 1, "role": "slider"},
        {"result": "found", "count": 1, "location_id": "has space"},
        {"result": "found", "count": 1, "label_key": "Run Now"},
    ],
)
def test_a_find_report_refuses_anything_else(raw: Any) -> None:
    with pytest.raises(guide_catalog.GuideCatalogError):
        guide_catalog.clean_find_report(raw)


# ── the guide run ──


def _active(store: GuideStore, **params: Any) -> dict[str, Any]:
    g = store.start(
        slot_key="s",
        session_key="dashboard:s",
        actions=[{"id": "ui.find", "params": {"route": "/schedule", "label": "Run Now", **params}}],
    )
    return store.claim(guide_id=g["guide_id"], tab_id="t", revision=g["revision"])


def _progress(store: GuideStore, g: dict[str, Any], outcome: str, **kw: Any) -> dict[str, Any]:
    return store.progress(
        guide_id=g["guide_id"],
        tab_id="t",
        revision=g["revision"],
        action_index=g["action_index"],
        step_index=g["step_index"],
        outcome=outcome,
        **kw,
    )


def test_a_found_control_walks_both_steps_and_keeps_the_report() -> None:
    store = GuideStore(clock=lambda: 1000.0)
    g = _active(store)
    g = _progress(store, g, "observed", find={"result": "found", "count": 1, "role": "button"})
    assert (g["status"], g["step_index"]) == ("active", 1)
    assert g["actions"][0]["find"] == {"result": "found", "count": 1, "role": "button"}
    g = _progress(store, g, "observed", find={"result": "found", "count": 1})
    assert g["status"] == "completed"


def test_nothing_found_is_target_missing_not_found() -> None:
    store = GuideStore(clock=lambda: 1000.0)
    g = _active(store)
    g = _progress(
        store, g, "target_missing", detail="not_found", find={"result": "none", "count": 0}
    )
    assert (g["status"], g["reason"]) == ("target_missing", REASON_NOT_FOUND)
    assert g["actions"][0]["find"] == {"result": "none", "count": 0}
    status = store.status_for_caller(slot_key="s")
    assert status["reason"] == "not_found" and status["actions"][0]["find"]["result"] == "none"


def test_several_matches_is_target_missing_ambiguous() -> None:
    store = GuideStore(clock=lambda: 1000.0)
    g = _active(store)
    g = _progress(
        store, g, "target_missing", detail="ambiguous", find={"result": "ambiguous", "count": 3}
    )
    assert (g["status"], g["reason"]) == ("target_missing", "ambiguous_target")
    assert g["actions"][0]["find"]["count"] == 3


def test_a_bad_report_changes_nothing() -> None:
    store = GuideStore(clock=lambda: 1000.0)
    g = _active(store)
    with pytest.raises(GuideError) as err:
        _progress(store, g, "observed", find={"result": "found", "count": 1, "name": "Run Now"})
    assert err.value.status == 400
    after = store.status_for_caller(slot_key="s")
    assert after["revision"] == g["revision"] and "find" not in after["actions"][0]


def test_a_bad_detail_after_a_valid_report_stores_nothing() -> None:
    store = GuideStore(clock=lambda: 1000.0)
    g = _active(store)
    with pytest.raises(GuideError):
        _progress(store, g, "observed", detail="not_found", find={"result": "none", "count": 0})
    assert "find" not in store.status_for_caller(slot_key="s")["actions"][0]


def test_only_a_ui_find_step_reports_a_search_or_not_found() -> None:
    store = GuideStore(clock=lambda: 1000.0)
    g = store.start(
        slot_key="s",
        session_key="dashboard:s",
        actions=[{"id": "settings.show", "params": {"setting_id": "chat.response-verbosity"}}],
    )
    g = store.claim(guide_id=g["guide_id"], tab_id="t", revision=g["revision"])
    with pytest.raises(GuideError) as err:
        _progress(store, g, "observed", find={"result": "found", "count": 1})
    assert err.value.code == "invalid_find"
    with pytest.raises(GuideError) as err:
        _progress(store, g, "target_missing", detail="not_found")
    assert err.value.code == "invalid_detail"


def test_a_replay_searches_again() -> None:
    store = GuideStore(clock=lambda: 1000.0)
    g = _active(store)
    g = _progress(store, g, "observed", find={"result": "found", "count": 1})
    g = _progress(store, g, "observed", find={"result": "found", "count": 1})
    again = store.replay(guide_id=g["guide_id"], revision=g["revision"])
    assert again["status"] == "offered" and "find" not in again["actions"][0]


# ── find_ui hands out find_ref ──


def _results(query: str) -> list[dict[str, Any]]:
    return ui_index.find_ui(query, "en")["results"]


def test_a_result_without_a_planned_guide_carries_a_find_ref() -> None:
    (run_now,) = [r for r in _results("run now") if r["id"] == "schedule.run-now"]
    assert "guide_ref" not in run_now
    assert run_now["find_ref"] == {
        "action_id": "ui.find",
        "params": {
            "label": "Run Now",
            "route": "/schedule",
            "role": "button",
            "location_id": "schedule.run-now",
        },
    }
    # As handed out, the gateway accepts it.
    guide_catalog.validate_actions(
        [{"id": run_now["find_ref"]["action_id"], "params": run_now["find_ref"]["params"]}]
    )


def test_a_find_ref_names_a_control_whose_label_depends_on_state_by_its_id() -> None:
    # The top bar's Search reads "Open command bar" while an app owns the
    # slot; the id is what the tab matches, whatever the label reads.
    (search,) = [r for r in _results("search my sessions") if r["id"] == "shell.search"]
    assert search["find_ref"]["params"]["location_id"] == "shell.search"
    (slack,) = [r for r in _results("slack") if r["id"] == "settings.sub.channels.slack"]
    assert slack["find_ref"]["params"]["location_id"] == "settings.sub.channels.slack"


def test_ui_find_keeps_a_known_location_id() -> None:
    assert _find(label="Slack", location_id="settings.sub.channels.slack")["params"] == {
        "label": "Slack",
        "location_id": "settings.sub.channels.slack",
    }


@pytest.mark.parametrize(
    "location_id",
    ["no.such.location", "settings.sub.security.denied", "settings.tab.secrets", "page.logs"],
)
def test_ui_find_refuses_a_location_it_may_not_name(location_id: str) -> None:
    _refused("unknown_location", label="X", location_id=location_id)


def test_a_guide_to_the_approval_mode_picker_is_never_started() -> None:
    """The approval mode is the agent's own ceiling: no guide walks the person to it."""
    assert "composer.approval-mode" not in guide_catalog.ui_find_location_ids()
    store = GuideStore(clock=lambda: 1000.0)
    with pytest.raises(GuideError) as err:
        store.start(
            slot_key="s",
            session_key="dashboard:s",
            actions=[
                {
                    "id": "ui.find",
                    "params": {
                        "label": "Approval mode",
                        "location_id": "composer.approval-mode",
                        "route": "/chat",
                    },
                }
            ],
        )
    assert (err.value.status, err.value.code) == (400, "unknown_location"), err.value.message


def test_a_guide_to_a_crewmates_permission_picker_is_never_started() -> None:
    """A crewmate's permission is its own approval ceiling: answered in words, never guided."""
    assert "members.permissions" not in guide_catalog.ui_find_location_ids()
    store = GuideStore(clock=lambda: 1000.0)
    with pytest.raises(GuideError) as err:
        store.start(
            slot_key="s",
            session_key="dashboard:s",
            actions=[
                {
                    "id": "ui.find",
                    "params": {
                        "label": "Permissions",
                        "location_id": "members.permissions",
                        "route": "/members",
                    },
                }
            ],
        )
    assert (err.value.status, err.value.code) == (400, "unknown_location"), err.value.message


@pytest.mark.parametrize("location_id", ["", "Shell.Search", "a" * 161, 3, "x y"])
def test_ui_find_refuses_a_malformed_location_id(location_id: Any) -> None:
    _refused("invalid_params", label="X", location_id=location_id)


def test_a_planned_guide_wins_and_no_find_ref_is_added() -> None:
    for r in _results("older sessions"):
        assert not ("guide_ref" in r and "find_ref" in r), r["id"]


def test_a_setting_never_gets_a_find_ref() -> None:
    for r in _results("response verbosity") + _results("security policy"):
        if r.get("setting_id"):
            assert "find_ref" not in r, r["id"]


def test_find_ref_never_names_a_setting_a_list_or_a_denied_control() -> None:
    idx = ui_index.load_index()
    placements = [
        {"route": "/schedule", "surface_id": "schedule", "parent_ids": [], "requires": []}
    ]
    control = {"id": "x", "kind": "button", "label_key": "k"}
    assert ui_index._find_ref(idx, control, placements, "Run Now") is not None
    for loc in (
        {**control, "setting_id": "chat.response-verbosity"},
        {**control, "kind": "setting"},
        {**control, "kind": "list"},
        {**control, "guide_policy": "deny"},
        {**control, "label_kind": "description"},
    ):
        assert ui_index._find_ref(idx, loc, placements, "Run Now") is None, loc


def test_a_page_is_named_by_the_control_that_opens_it() -> None:
    idx = ui_index.load_index()
    page = {"id": "x", "kind": "page", "label_key": "k"}
    rail = [{"route": "/schedule", "surface_id": "schedule", "entry_kind": "rail", "requires": []}]
    other = [
        {
            "route": "/schedule",
            "surface_id": "schedule",
            "entry_kind": "direct-link",
            "requires": [],
        }
    ]
    assert ui_index._find_ref(idx, page, rail, "Schedule") == {
        "action_id": "ui.find",
        "params": {"label": "Schedule"},
    }
    # No rail entry: nothing to point at, and the guide never opens the page.
    assert ui_index._find_ref(idx, page, other, "Schedule") is None


def test_find_ref_labels_follow_the_dashboard_language() -> None:
    (run_now,) = [
        r
        for r in ui_index.find_ui("run now", "en", label_lang="zh-CN")["results"]
        if r["id"] == "schedule.run-now"
    ]
    assert run_now["find_ref"]["params"]["label"] == run_now["label"] != "Run Now"


# ── fallback ordering in the tool result ──


def test_the_tool_result_offers_guide_ref_first_then_find_ref() -> None:
    both = {
        "results": [
            {"id": "a", "find_ref": {"action_id": "ui.find", "params": {"label": "A"}}},
            {"id": "b", "guide_ref": {"action_id": "ui.show", "params": {"location_id": "b"}}},
        ]
    }
    hint = mcp_guide._find_ui_next(both) or ""
    assert hint.startswith("b has a guide_ref")
    only_find = {"results": [both["results"][0]]}
    hint = mcp_guide._find_ui_next(only_find) or ""
    assert hint.startswith("a has a find_ref") and "guide_start" in hint
    # Nothing to guide to: answered in words, never with a promised card.
    assert mcp_guide._find_ui_next({"results": [{"id": "c"}]}) == mcp_guide._NO_GUIDE_NOTE
    assert (
        mcp_guide._find_ui_next({**only_find, "ambiguous": True})
        == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE
    )


def _next_for(monkeypatch: pytest.MonkeyPatch, query: str) -> dict:
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("", "Error: no identity"))
    return json.loads(mcp_guide._call_tool_inner("find_ui", {"query": query, "lang": "en"}))


def test_a_removal_the_user_asked_for_still_gets_its_guide_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _next_for(monkeypatch, "delete schedule")
    assert out["results"][0]["caution"] is True
    hint = out["next"]
    assert hint.startswith("schedule.delete has a guide_ref") and "guide_start" in hint
    # Its consequences come from the page's own words, never a stock "cannot be undone".
    assert "caution_text" in hint and "cannot be undone" not in hint


def test_a_create_question_is_guided_even_though_mate_could_create_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _next_for(monkeypatch, "add a schedule")
    assert out["next"].startswith("schedule.add-job has a find_ref")
    assert "never instead of it" in out["next"]


def test_one_label_in_several_places_is_guided_to_the_features_own_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "New crewmate" is drawn on Customize and on the Crewmates page. The
    # Crewmates page is still behind its preview, which this tab does not show
    # as on, so the tie is not settled on it: the user is asked which one,
    # never sent to the first-ranked Customize button either.
    out = _next_for(monkeypatch, "Where do I create a new crewmate?")
    assert out["ambiguous"] is True and out["tied"] >= 2
    assert out["results"][0]["id"] == "agents.add"
    assert out["next"] == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE


def test_ties_are_one_guide_only_for_the_same_location_or_the_same_target() -> None:
    ui_show = lambda lid: {"action_id": "ui.show", "params": {"location_id": lid}}  # noqa: E731
    # One label, two controls, two different guides: asked, never the first.
    rows = [
        {"id": "a", "label": "Add Job", "guide_ref": ui_show("a")},
        {"id": "b", "label": "add job", "guide_ref": ui_show("b")},
    ]
    assert (
        mcp_guide._find_ui_next({"results": rows, "ambiguous": True})
        == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE
    )
    # Different labels, different controls: asked too.
    rows = [
        {
            "id": "a",
            "label": "Add Job",
            "find_ref": {"action_id": "ui.find", "params": {"label": "Add Job"}},
        },
        {
            "id": "b",
            "label": "Create your first job",
            "find_ref": {"action_id": "ui.find", "params": {"label": "Create"}},
        },
    ]
    assert (
        mcp_guide._find_ui_next({"results": rows, "ambiguous": True})
        == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE
    )
    # Two results whose guides lead to the same place: the first is guided.
    same_target = [
        {"id": "setting:x", "label": "Theme", "guide_ref": ui_show("display.theme")},
        {"id": "display.theme", "label": "Colour theme", "guide_ref": ui_show("display.theme")},
    ]
    hint = mcp_guide._find_ui_next({"results": same_target, "ambiguous": True}) or ""
    assert hint.startswith("setting:x has a guide_ref")
    # The same location reached two ways: the first is guided.
    same_id = [
        {"id": "a", "label": "Add Job", "guide_ref": ui_show("a")},
        {
            "id": "a",
            "label": "Add Job",
            "find_ref": {"action_id": "ui.find", "params": {"label": "Add Job"}},
        },
    ]
    assert (mcp_guide._find_ui_next({"results": same_id, "ambiguous": True}) or "").startswith("a ")
    # A later row on another target never stands in for the tied one.
    lone = [
        same_target[0],
        same_target[1],
        {"id": "c", "label": "Theme", "guide_ref": ui_show("c")},
    ]
    assert (mcp_guide._find_ui_next({"results": lone, "ambiguous": True}) or "").startswith(
        "setting:x "
    )


def _placed(lid: str, page: str, **extra: object) -> dict:
    return {
        "id": lid,
        "label": "New thing",
        "guide_ref": {"action_id": "ui.show", "params": {"location_id": lid}},
        "placements": [{"path": [{"id": page}, {"id": lid}], "route": "/" + page[5:]}],
        **extra,
    }


def test_a_tie_is_settled_by_the_users_screen_then_the_features_own_page() -> None:
    nxt = mcp_guide._find_ui_next
    other, home = _placed("agents.add", "page.capabilities"), _placed("members.new", "page.members")
    # The feature's own page wins over a copy on another page, whatever the rank.
    assert (nxt({"results": [other, home], "ambiguous": True}) or "").startswith("members.new ")
    # The control on the user's screen now wins over the feature's own page.
    shown = {**other, "live": {"status": "pointable"}}
    assert (nxt({"results": [shown, home], "ambiguous": True}) or "").startswith("agents.add ")
    # Two controls on the feature's own page: the first-ranked one.
    menu = _placed("members.menu-new", "page.members")
    hint = nxt({"results": [other, menu, home], "ambiguous": True, "tied": 3}) or ""
    assert hint.startswith("members.menu-new ")
    # The page itself outranks a tab of the same name on another page.
    page = {"id": "page.hooks", "kind": "page", "label": "Hooks", "find_ref": {"params": {}}}
    tab = _placed("settings.tab.hooks", "page.settings")
    assert (nxt({"results": [tab, page], "ambiguous": True}) or "").startswith("page.hooks ")
    # Homes on two different pages, or no home at all: asked, never a claimed guide.
    pages2 = [home, _placed("schedule.new", "page.schedule")]
    assert nxt({"results": pages2, "ambiguous": True}) == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE
    assert (
        nxt({"results": [other, _placed("x.new", "page.chat")], "ambiguous": True})
        == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE
    )
    # A row below the tie never settles it.
    assert (
        nxt({"results": [other, _placed("y.new", "page.chat"), home], "ambiguous": True})
        == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE
    )
    assert (
        nxt({"results": [other, _placed("y.new", "page.chat"), home], "ambiguous": True, "tied": 3})
        or ""
    ).startswith("members.new ")


def test_a_tie_is_never_settled_on_a_removal_or_the_agents_ceiling() -> None:
    nxt = mcp_guide._find_ui_next
    other = _placed("agents.add", "page.capabilities")
    removal = _placed("members.delete", "page.members", caution=True)
    assert (
        nxt({"results": [other, removal], "ambiguous": True}) == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE
    )
    shown = {**removal, "live": {"status": "pointable"}}
    assert nxt({"results": [other, shown], "ambiguous": True}) == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE
    ceiling = _placed("settings.tab.security", "page.settings")
    assert (
        nxt({"results": [other, ceiling], "ambiguous": True}) == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE
    )
    secret = {**_placed("setting:secrets.vault", "page.setting"), "kind": "setting"}
    assert (
        nxt({"results": [other, {**secret, "live": {"status": "pointable"}}], "ambiguous": True})
        == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE
    )
    on_root = _placed("security.deny", "page.security")
    on_root["placements"][0]["route"] = "/settings/security"
    assert (
        nxt({"results": [other, {**on_root, "live": {"status": "pointable"}}], "ambiguous": True})
        == mcp_guide._AMBIGUOUS_NO_GUIDE_NOTE
    )


def test_a_missed_sentence_is_searched_again_by_its_control_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _next_for(monkeypatch, "How do I add a schedule that runs every morning?")
    assert out["status"] == "no_match"
    assert "find_ui again" in out["next"] and "does not exist" in out["next"]


def test_the_agents_own_ceiling_is_never_offered_as_a_guide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for query in ("denied commands", "computer use", "secrets"):
        out = _next_for(monkeypatch, query)
        assert out["status"] == "ok", query
        assert out.get("next", mcp_guide._NO_GUIDE_NOTE) == mcp_guide._NO_GUIDE_NOTE, query


def test_a_preview_feature_is_answered_in_words_not_by_flipping_its_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _next_for(monkeypatch, "webhooks")
    ids = [r["id"] for r in out["results"]]
    assert "setting:developer.webhooks" in ids  # the switch is found, never offered
    assert out["next"].startswith("This is a preview feature")
    # Once the user's tab shows the page reachable, the preview is on: guide
    # normally, never to the switch. The Webhooks page has no entry to point
    # at, so the tie goes to the Settings tab of the same name that opens it.
    rows = [{**r, "live": {"status": "pointable"}} for r in out["results"]]
    assert out["ambiguous"] is True
    hint = mcp_guide._find_ui_next({**out, "results": rows}) or ""
    assert hint.startswith("settings.tab.webhooks has a find_ref")
    # The page alone, preview on: no guide, its path is given in words.
    page = [r for r in rows if r["id"] == "page.webhooks"]
    assert (
        mcp_guide._find_ui_next({**out, "results": page, "ambiguous": False})
        == mcp_guide._NO_GUIDE_NOTE
    )


def test_find_ui_tells_the_agent_to_start_the_find_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("", "Error: no identity"))
    out = json.loads(mcp_guide._call_tool_inner("find_ui", {"query": "run now", "lang": "en"}))
    assert out["results"][0]["id"] == "schedule.run-now"
    assert out["next"].startswith("schedule.run-now has a find_ref")


def test_guide_start_lists_ui_find() -> None:
    tools = {t["name"]: t for t in mcp_guide._tool_definitions()}
    item = tools["guide_start"]["inputSchema"]["properties"]["actions"]["items"]
    assert "ui.find" in item["properties"]["id"]["enum"]


# ── Instances is guidable; the trust root is not ──


def test_instances_settings_are_guidable_and_the_trust_root_is_not() -> None:
    ids = guide_catalog.guidable_settings()
    assert "instances.enable-remote-crew-management" in ids
    assert "instances.auto-connect-crews" in ids
    assert "developer.remote-crew-sessions" not in ids
    assert not any(e["tab"] in {"security", "secrets", "computer-use"} for e in ids.values())


# ── the numbered pick and the tab's own refusals, as Mate is told them ──


def test_ui_find_tells_the_agent_the_user_picks_a_numbered_match() -> None:
    entry = next(a for a in guide_catalog.list_actions() if a["id"] == "ui.find")
    assert "'#N'" not in entry["description"]
    assert "never a number" in entry["description"]
    assert "never a number" in entry["params_schema"]["properties"]["container"]["description"]
    # Only the dashboard's shared containers are opened; the rest the user opens.
    assert "collapsed sections" not in entry["description"]
    assert "asked to open" in entry["description"]
    status = next(t for t in mcp_guide._tool_definitions() if t["name"] == "guide_status")
    assert "'#N'" not in status["description"]
    assert "picks one there" in status["description"]
    assert "asked to open" in status["description"]


def test_a_sensitive_match_is_reported_as_nothing_found() -> None:
    # The tab refuses a ceiling match on the element itself and says only 'none'.
    assert guide_catalog.clean_find_report({"result": "none", "count": 0}) == {
        "result": "none",
        "count": 0,
    }
    with pytest.raises(guide_catalog.GuideCatalogError):
        guide_catalog.clean_find_report({"result": "sensitive", "count": 1})


# ── a control inside a menu names the control that opens it ──


def test_a_menu_items_find_ref_names_its_opener() -> None:
    # Mark all as read sits in the notifications menu: the guide points at the
    # bell first, a static fact of the index, never a runtime probe.
    (mark,) = [r for r in _results("mark all as read") if r["id"] == "notifications.mark-all-read"]
    assert mark["find_ref"]["params"]["opener"] == "shell.notifications"
    (shot,) = [
        r for r in _results("take a screenshot") if r["id"] == "composer.add-menu.screenshot"
    ]
    assert shot["find_ref"]["params"]["opener"] == "composer.add-menu"
    # Reference a file has a planned walk through the same menu instead.
    (ref,) = [
        r for r in _results("mention a file") if r["id"] == "composer.add-menu.reference-file"
    ]
    assert isinstance(ref.get("guide_ref"), dict)
    # A control drawn on the page itself has no opener.
    (job,) = [r for r in _results("add job") if r["id"] == "schedule.add-job"]
    assert "opener" not in job["find_ref"]["params"]


def test_ui_find_keeps_a_known_opener() -> None:
    record = _find(
        label="Mark all as read",
        location_id="notifications.mark-all-read",
        opener="shell.notifications",
    )
    assert record["params"]["opener"] == "shell.notifications"


@pytest.mark.parametrize("opener", ["no.such.location", "settings.tab.security", "page.logs"])
def test_ui_find_refuses_an_opener_it_may_not_name(opener: str) -> None:
    _refused("unknown_location", label="X", opener=opener)


@pytest.mark.parametrize("opener", ["", "Shell.Bell", 3, "x y"])
def test_ui_find_refuses_a_malformed_opener(opener: Any) -> None:
    _refused("invalid_params", label="X", opener=opener)


# The exact repro: Clear all (a destructive control in the same menu) passed
# off as the control that opens Mark all as read's menu.
_CLEAR_ALL_AS_OPENER = {
    "label": "Mark all as read",
    "location_id": "notifications.mark-all-read",
    "opener": "notifications.clear-all",
}


def test_ui_find_refuses_an_opener_that_is_not_the_targets_own() -> None:
    _refused("unknown_location", **_CLEAR_ALL_AS_OPENER)
    # Another registered, harmless control is refused too: only the index's.
    _refused(
        "unknown_location",
        label="Mark all as read",
        location_id="notifications.mark-all-read",
        opener="members.add-menu",
    )


def test_the_agent_never_chooses_the_opener() -> None:
    # The agent's opener is replaced by the index's before the catalog sees it.
    out = ui_index.authoritative_find(dict(_CLEAR_ALL_AS_OPENER))
    assert out["opener"] == "shell.notifications"
    assert _find(**out)["params"]["opener"] == "shell.notifications"
    # Without a location_id there is no target to derive one from: dropped.
    assert "opener" not in ui_index.authoritative_find(
        {"label": "Add Job", "route": "/schedule", "opener": "notifications.clear-all"}
    )


def test_the_catalog_derives_the_opener_when_it_is_left_out() -> None:
    record = _find(label="Mark all as read", location_id="notifications.mark-all-read")
    assert record["params"]["opener"] == "shell.notifications"


def test_an_opener_needs_the_controls_location_id() -> None:
    _refused("unknown_location", label="Mark all as read", opener="shell.notifications")


def test_a_destructive_or_trust_root_control_is_never_an_opener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An index whose menu parent removes something / is a trust-root tab:
    # the target keeps no opener rather than pointing at either.
    index = {
        "locations": [
            {"id": "x.wipe", "kind": "button", "label_key": "k.wipe", "placements": []},
            {
                "id": "x.item",
                "kind": "button",
                "label_key": "k.item",
                "placements": [{"entry_kind": "menu", "parent_ids": ["x.wipe"]}],
            },
            {
                "id": "x.sec-item",
                "kind": "button",
                "label_key": "k.item",
                "placements": [{"entry_kind": "menu", "parent_ids": ["settings.tab.security"]}],
            },
            {"id": "settings.tab.security", "kind": "tab", "label_key": "k.s", "placements": []},
            {"id": "x.safe", "kind": "button", "label_key": "k.safe", "placements": []},
            {
                "id": "x.ok",
                "kind": "button",
                "label_key": "k.item",
                "placements": [{"entry_kind": "menu", "parent_ids": ["x.safe"]}],
            },
        ],
        "labels": {"en": {"k.wipe": "Delete everything", "k.item": "Item", "k.safe": "More"}},
    }
    path = tmp_path / "ui-index.generated.json"
    path.write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setattr(guide_catalog, "_UI_INDEX_PATH", path)
    guide_catalog._location_ids_cache.clear()
    try:
        assert guide_catalog.ui_find_opener("x.item") is None
        assert guide_catalog.ui_find_opener("x.sec-item") is None
        assert guide_catalog.ui_find_opener("x.ok") == "x.safe"
        _refused("unknown_location", label="Item", location_id="x.item", opener="x.wipe")
    finally:
        guide_catalog._location_ids_cache.clear()


def test_an_opener_is_never_the_control_itself() -> None:
    _refused(
        "unknown_location",
        label="X",
        location_id="shell.notifications",
        opener="shell.notifications",
    )


# ── the label the guide names is the index's, never the agent's own ──


def test_a_label_no_control_carries_becomes_the_index_match() -> None:
    out = ui_index.authoritative_find({"label": "New schedule", "route": "/schedule"})
    assert out["label"] == "Add Job" and out["location_id"] == "schedule.add-job"


def test_a_label_no_control_carries_is_replaced_without_a_route_too() -> None:
    out = ui_index.authoritative_find({"label": "New schedule"})
    assert out["label"] == "Add Job" and out["location_id"] == "schedule.add-job"
    assert out["route"] == "/schedule"


def test_an_index_match_on_another_page_leaves_the_agents_params_alone() -> None:
    # The agent named a page; the best index match is on a different one, so
    # it is another control: nothing is swapped, the catalog judges as asked.
    asked = {"label": "New schedule", "route": "/members", "container": "Roster"}
    assert ui_index.authoritative_find(dict(asked)) == asked


def test_a_registered_controls_label_is_its_own() -> None:
    bell = {"opener": "shell.notifications"}
    kept = {"label": "Mark all as read", "location_id": "notifications.mark-all-read"}
    assert ui_index.authoritative_find(kept) == {**kept, **bell}
    # Its own label in another shipped language is kept too.
    zh = {"label": "全部标为已读", "location_id": "notifications.mark-all-read"}
    assert ui_index.authoritative_find(zh) == {**zh, **bell}
    # Anything else becomes the label in the dashboard's language.
    out = ui_index.authoritative_find(
        {"label": "Read everything", "location_id": "notifications.mark-all-read"},
        label_lang="zh-CN",
    )
    assert out["label"] == "全部标为已读"


def test_a_known_label_is_bound_to_its_control_and_an_unplaceable_one_left_alone() -> None:
    assert ui_index.authoritative_find({"label": "Add Job"}) == {
        "label": "Add Job",
        "location_id": "schedule.add-job",
    }
    assert ui_index.authoritative_find({"label": "zzqx"}) == {"label": "zzqx"}


def test_another_name_of_a_control_becomes_the_label_the_page_shows() -> None:
    # The tooltip of New artifact is not its accessible name: searched as
    # written it would miss; bound by id it reads as the page does.
    out = ui_index.authoritative_find(
        {"label": "Start a new blank document in the library", "route": "/artifacts"},
        label_lang="en",
    )
    assert out == {"label": "New artifact", "route": "/artifacts", "location_id": "artifacts.new"}
    # A flipping label keeps the state the agent named.
    out = ui_index.authoritative_find({"label": "Collapse sidebar"}, label_lang="en")
    assert out == {"label": "Collapse sidebar", "location_id": "shell.nav-toggle"}


def test_a_find_for_a_planned_control_becomes_its_walk() -> None:
    assert ui_index.planned_find(
        {
            "label": "Delete crewmate",
            "route": "/capabilities?tab=crews",
            "location_id": "agents.delete",
        }
    ) == {"id": "ui.show", "params": {"location_id": "agents.delete"}}
    # Several planned controls share the label: the page named decides, and
    # the one needing no prior pick is the general door.
    assert ui_index.planned_find({"label": "新建队友", "route": "/members"}) == {
        "id": "ui.show",
        "params": {"location_id": "members.add-menu.new"},
    }
    assert ui_index.planned_find({"label": "New crewmate"}) is None
    # A page the plan does not walk is not this control.
    assert ui_index.planned_find({"label": "Delete crewmate", "route": "/members"}) is None
    # A control with no plan stays a find.
    assert ui_index.planned_find({"label": "Add Custom"}) is None


def test_route_compatibility_compares_the_tab_the_route_names() -> None:
    ok = guide_catalog._route_compatible
    assert ok("/capabilities?tab=crews", "/capabilities?tab=crews")
    assert ok("/capabilities", "/capabilities?tab=crews")
    assert ok("/apps/", "/apps")
    assert ok("/settings/voice", "/settings/voice?highlight=voice.engine")
    assert not ok("/capabilities?tab=mcp", "/capabilities?tab=crews")
    assert not ok("/capabilities?tab=mcp", "/capabilities")
    assert not ok("/apps", "/apps/library")


def test_a_control_on_another_tab_is_never_bound_or_walked() -> None:
    for label, lid in (
        ("Manage memory", "agents.manage-memory"),
        ("Delete crewmate", "agents.delete"),
    ):
        asked = {"label": label, "route": "/capabilities?tab=mcp"}
        assert ui_index.authoritative_find(dict(asked)) == asked
        assert ui_index.planned_find(dict(asked)) is None
        assert ui_index.planned_find({**asked, "location_id": lid}) is None
        on_tab = {"label": label, "route": "/capabilities?tab=crews"}
        bound = ui_index.authoritative_find(dict(on_tab))
        assert bound["location_id"] == lid
        assert ui_index.planned_find(bound) == {"id": "ui.show", "params": {"location_id": lid}}


def test_an_index_match_on_another_tab_leaves_the_agents_params_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = {
        "action_id": "ui.find",
        "params": {"label": "Add Server", "route": "/capabilities?tab=mcp"},
    }
    monkeypatch.setattr(ui_index, "find_ui", lambda *a, **k: {"results": [{"find_ref": ref}]})
    asked = {"label": "zzqx server", "route": "/capabilities?tab=crews"}
    assert ui_index.authoritative_find(dict(asked)) == asked
    out = ui_index.authoritative_find({"label": "zzqx server", "route": "/capabilities?tab=mcp"})
    assert out["label"] == "Add Server" and out["route"] == "/capabilities?tab=mcp"


def test_guide_start_turns_a_find_for_a_planned_control_into_its_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_guide, "_dashboard_ui_lang", lambda: "en")
    out = mcp_guide._authoritative_actions(
        [
            {
                "id": "ui.find",
                "params": {"label": "Manage memory", "route": "/capabilities?tab=crews"},
                "note": "n",
            }
        ]
    )
    assert out == [
        {"id": "ui.show", "params": {"location_id": "agents.manage-memory"}, "note": "n"}
    ]


def test_a_page_is_reached_by_its_planned_opener_or_its_rail_entry_else_in_words() -> None:
    (dev,) = [r for r in _results("developer page") if r["id"] == "page.developer"]
    assert dev["guide_ref"] == {
        "action_id": "ui.show",
        "params": {"location_id": "shell.developer"},
    }
    # Logs has no entry anywhere to point at, and a guide never opens a page:
    # no find is handed out, and one the agent writes itself is refused.
    (logs,) = [r for r in _results("logs page") if r["id"] == "page.logs"]
    assert "find_ref" not in logs and "guide_ref" not in logs
    with pytest.raises(guide_catalog.GuideCatalogError) as err:
        guide_catalog.validate_actions(
            [{"id": "ui.find", "params": {"label": "Logs", "route": "/logs"}}]
        )
    assert err.value.code == "no_entry"
    # Naming a role or a container for it does not make an entry exist.
    for extra in ({"role": "link"}, {"role": "tab"}, {"container": "Navigation"}):
        with pytest.raises(guide_catalog.GuideCatalogError) as err:
            guide_catalog.validate_actions(
                [{"id": "ui.find", "params": {"label": "Logs", "route": "/logs", **extra}}]
            )
        assert err.value.code == "no_entry", extra
    # The pages the rail draws (an app's own row, Discover and Library drawn
    # by hand) are named by that entry, with no route: the guide stays put.
    for query, pid, label in (
        ("app library", "page.apps-library", "Library"),
        ("discover", "page.apps", "Discover"),
        ("task runner", "page.projects", "Task Runner"),
    ):
        (rec,) = [r for r in _results(query) if r["id"] == pid]
        assert rec["find_ref"] == {"action_id": "ui.find", "params": {"label": label}}
    # A caller's own page flag is never taken: a control at that route is a control.
    (job,) = guide_catalog.validate_actions(
        [{"id": "ui.find", "params": {"label": "Add Job", "route": "/schedule", "page": True}}]
    )
    assert "page" not in job["params"]


def test_guide_start_quotes_the_guides_label(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def post(path: str, body: dict[str, Any], session_key: str) -> dict[str, Any]:
        sent.update(body)
        return {"guide_id": "g", "actions": body["actions"], "delivered_clients": 1}

    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("sk", None))
    monkeypatch.setattr(mcp_guide, "_dashboard_ui_lang", lambda: None)
    monkeypatch.setattr(mcp_guide, "_post", post)
    out = json.loads(
        mcp_guide._call_tool_inner(
            "guide_start",
            {
                "actions": [
                    {"id": "ui.find", "params": {"label": "New schedule", "route": "/schedule"}}
                ]
            },
        )
    )
    assert sent["actions"][0]["params"]["label"] == "Add Job"
    assert "“Add Job”" in out["next"]


# ── a number never names one of several matches ──


@pytest.mark.parametrize("container", ["#2", "2", " # 3 ", "#10"])
def test_ui_find_refuses_a_numbered_container(container: str) -> None:
    # The number names a match only in the list the person saw; the page can
    # reorder before the new guide runs, so the person picks in the panel.
    _refused("invalid_params", label="Run Now", route="/schedule", container=container)


def test_a_container_that_only_contains_a_number_is_a_name() -> None:
    assert _find(label="Run Now", route="/schedule", container="Job 2")["params"]["container"] == (
        "Job 2"
    )


def test_uninstalling_an_app_is_guided_through_its_library_card_named_by_pick() -> None:
    (rec,) = [r for r in _results("uninstall an app") if r["id"] == "apps.library.tile-uninstall"]
    assert rec["guide_ref"] == {
        "action_id": "ui.show",
        "params": {"location_id": "apps.library.tile-uninstall"},
    }
    assert rec["caution"] is True
    (act,) = guide_catalog.validate_actions(
        [
            {
                "id": "ui.show",
                "params": {"location_id": "apps.library.tile-uninstall", "pick": "Command Bar"},
            }
        ]
    )
    assert act["placements"]["any"] == [
        "any:select:app_tile_menu_open",
        "any:apps.library.tile-uninstall",
    ]


@pytest.mark.parametrize(
    ("query", "lang", "want"),
    [
        ("Where can I report a problem?", "en", "shell.report-problem"),
        ("report an issue", "en", "shell.report-problem"),
        ("connect a device", "en", "shell.connect-phone"),
        ("scan a qr code to connect", "en", "shell.connect-phone"),
        ("二维码连接设备", "zh-CN", "shell.connect-phone"),
        ("stop auto nudge", "en", "composer.automation.pause"),
        ("关闭自动 nudge", "zh-CN", "composer.automation.pause"),
        ("move an artifact to a folder", "en", "artifacts.detail.move-to-folder"),
        ("把产物移到文件夹", "zh-CN", "artifacts.detail.move-to-folder"),
    ],
)
def test_controls_the_index_used_to_miss_are_found_and_guidable(
    query: str, lang: str, want: str
) -> None:
    out = ui_index.find_ui(query, lang)
    assert out["results"][0]["id"] == want, [r["id"] for r in out["results"]]
    assert (mcp_guide._find_ui_next(out) or "").startswith(f"{want} has a ")


def test_a_described_control_is_found_by_its_id_and_named_by_its_alias() -> None:
    # The model chip's text is the model's name: the find names it "Model" and
    # matches the chip by its registered id.
    (chip,) = [
        r
        for r in ui_index.find_ui("switch model", "en")["results"]
        if r["id"] == "chat.model-picker"
    ]
    assert chip["find_ref"]["params"] == {
        "label": "Model",
        "route": "/chat",
        "role": "button",
        "location_id": "chat.model-picker",
    }
    assert "label" not in chip  # its description is never quoted as a label
    # The approval-mode picker is described and aliased too, but it is the
    # agent's own ceiling: never a find.
    (mode,) = [
        r
        for r in ui_index.find_ui("approval mode", "en")["results"]
        if r["id"] == "composer.approval-mode"
    ]
    assert "find_ref" not in mode and "guide_ref" not in mode


def test_a_planned_guide_never_outranks_a_better_or_equal_find() -> None:
    find = {
        "id": "a",
        "match": "search_term",
        "find_ref": {"action_id": "ui.find", "params": {"label": "A"}},
    }
    weaker = {
        "id": "b",
        "match": "tokens",
        "guide_ref": {"action_id": "ui.show", "params": {"location_id": "b"}},
    }
    stronger = {**weaker, "match": "exact_label"}
    assert (mcp_guide._find_ui_next({"results": [find, weaker]}) or "").startswith(
        "a has a find_ref"
    )
    assert (mcp_guide._find_ui_next({"results": [find, stronger]}) or "").startswith(
        "b has a guide_ref"
    )
    # A guide ranked first is offered as before.
    assert (mcp_guide._find_ui_next({"results": [weaker, find]}) or "").startswith(
        "b has a guide_ref"
    )


def test_a_warm_catalog_validates_ui_find_without_reading_the_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routes warm the catalog in a worker thread; ui.find must then be a cache hit."""
    guide_catalog._location_ids_cache.clear()
    guide_catalog.warm_catalogs()
    reads: list[str] = []
    real = Path.read_text

    def counting(self: Path, *a: Any, **k: Any) -> str:
        if self.suffix == ".json":  # the packaged indexes, not unrelated metadata
            reads.append(str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    _find(route="/schedule", label="Run Now", role="button", container="Jobs")
    assert reads == []


@pytest.mark.parametrize("detail", [[], {}, 7])
def test_a_detail_that_is_not_a_string_is_refused_not_a_crash(detail: Any) -> None:
    store = GuideStore(clock=lambda: 1000.0)
    g = _active(store)
    with pytest.raises(GuideError) as err:
        _progress(store, g, "target_missing", detail=detail)
    assert (err.value.status, err.value.code) == (400, "invalid_detail")
