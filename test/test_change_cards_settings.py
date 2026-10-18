"""Change cards for settings: string-list settings and the settings diagnosis.

A string-list setting changes one item at a time, and the diagnosis reports what the
settings say and what changed recently without carrying a credential value.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import change_cards_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from change_cards_helpers import (
    AGENT,
    OWNER,
    REPO,
    SECRET_VALUE,
    SK,
    SLOT,
    _app,
    _card_headers,
    _run,
)

from kiro_crew import change_card_catalog as catalog
from kiro_crew import mcp_guide
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes

# The shared fixtures, bound here by name so pytest registers them for this module.
world = change_cards_helpers.world


# ── string-list settings (one item at a time) ──

_MODELS = "chat.selectable-models"
_LIST_SNAP = {
    "store": "dashboard",
    "key": "model_picker_hidden_models",
    "label": "Selectable Models",
}


def _list_params(op: str, item: str = "fable-1") -> dict[str, Any]:
    return catalog.validate_params(
        "setting.change", {"setting_id": _MODELS, "op": op, "item": item}
    )


def test_a_string_list_setting_resolves_to_the_settings_pages_delta_keys():
    target = cards.resolve_setting({"setting_id": _MODELS})
    assert target == {**_LIST_SNAP, "list": True}
    assert cards.resolve_setting({"path": "dashboard.model_picker_hidden_models"})["list"] is True
    # The keys a card sends are exactly the delta keys the route accepts.
    route = (REPO / "src/kiro_crew/dashboard/file_api/dashboard_config.py").read_text(
        encoding="utf-8"
    )
    accepted = re.search(r"_allowed = \{([^}]*)\}", route).group(1)
    ids = {e["id"] for e in cards.settings_registry()}
    for sid, (_key, add_key, remove_key) in catalog.DASHBOARD_LIST_SETTINGS.items():
        assert sid in ids
        assert f'"{add_key}"' in accepted and f'"{remove_key}"' in accepted


def test_list_add_title_hides_and_an_unworded_list_reads_add_to_remove_from(monkeypatch):
    before = {**_LIST_SNAP, "value": ["other-2"]}
    added = catalog.build_preview("setting.change", _list_params("add"), before, {})
    assert added["title"] == "Hide “fable-1” from the model picker"
    assert added["changes"][0]["label"] == "Hidden models"
    monkeypatch.setattr(catalog, "LIST_SETTING_WORDING", {})
    added = catalog.build_preview("setting.change", _list_params("add"), before, {})
    assert added["title"] == "Add “fable-1” to Selectable Models"
    removed = catalog.build_preview("setting.change", _list_params("remove", "other-2"), before, {})
    assert removed["title"] == "Remove “other-2” from Selectable Models"


def test_list_remove_previews_before_and_after_and_undo_adds_it_back():
    p = _list_params("remove")
    assert p == {"setting_id": _MODELS, "op": "remove", "item": "fable-1"}
    before = {**_LIST_SNAP, "value": ["other-2", "fable-1"]}
    preview = catalog.build_preview("setting.change", p, before, {})
    assert preview["apply"] == [
        {
            "method": "PUT",
            "path": "/api/dashboard/config",
            "body": {"model_picker_hidden_models_remove": ["fable-1"]},
        }
    ]
    # The card names the list it edits and what the person will see: removing
    # from the HIDDEN list shows the model, it does not make it unselectable.
    assert preview["title"] == "Show “fable-1” in the model picker"
    assert preview["changes"] == [
        {
            "label": "Hidden models",
            "before": ["other-2", "fable-1"],
            "after": ["other-2"],
            "add": [],
            "remove": ["fable-1"],
        }
    ]
    assert preview["risk"] == "normal"
    undo, reason = catalog.build_undo("setting.change", p, before, [{}], 1)
    assert reason is None
    assert undo == [
        {
            "method": "PUT",
            "path": "/api/dashboard/config",
            "body": {"model_picker_hidden_models_add": ["fable-1"]},
        }
    ]


def test_list_add_previews_and_undo_removes_it():
    p = _list_params("add", "new-3")
    before = {**_LIST_SNAP, "value": ["other-2"]}
    preview = catalog.build_preview("setting.change", p, before, {})
    assert preview["changes"][0]["after"] == ["other-2", "new-3"]
    assert preview["apply"][0]["body"] == {"model_picker_hidden_models_add": ["new-3"]}
    assert preview["risk"] == "normal"
    undo, _ = catalog.build_undo("setting.change", p, before, [{}], 1)
    assert undo[0]["body"] == {"model_picker_hidden_models_remove": ["new-3"]}


def test_a_list_op_that_changes_nothing_is_refused():
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(
            "setting.change", _list_params("remove"), {**_LIST_SNAP, "value": ["x"]}, {}
        )
    assert exc.value.code == "no_change"
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(
            "setting.change", _list_params("add"), {**_LIST_SNAP, "value": ["fable-1"]}, {}
        )
    assert exc.value.code == "no_change"


@pytest.mark.parametrize(
    "params, code",
    [
        ({"setting_id": _MODELS, "op": "remove", "item": 7}, "invalid_param"),
        ({"setting_id": _MODELS, "op": "remove", "item": ["a"]}, "invalid_param"),
        ({"setting_id": _MODELS, "op": "remove"}, "invalid_param"),
        ({"setting_id": _MODELS, "op": "toggle", "item": "a"}, "invalid_param"),
        ({"setting_id": _MODELS, "op": "add", "item": "a", "value": "b"}, "invalid_param"),
        ({"setting_id": _MODELS, "op": "add", "item": ""}, "invalid_param"),
    ],
)
def test_list_op_params_are_validated(params, code):
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params("setting.change", params)
    assert exc.value.code == code


def test_op_on_a_non_list_setting_and_value_on_a_list_setting_are_refused():
    scalar = cards.resolve_setting({"setting_id": "chat.response-verbosity"})
    with pytest.raises(catalog.CardCatalogError) as exc:
        cards.check_target_params(scalar, {"op": "add", "item": "x"})
    assert exc.value.code == "not_a_list"
    listed = cards.resolve_setting({"setting_id": _MODELS})
    with pytest.raises(catalog.CardCatalogError) as exc:
        cards.check_target_params(listed, {"value": ["x"]})
    assert exc.value.code == "list_setting"
    cards.check_target_params(listed, {"op": "remove", "item": "x"})
    # The catalog refuses both on its own too, without the gateway's pre-check.
    p = catalog.validate_params(
        "setting.change", {"setting_id": "chat.response-verbosity", "op": "add", "item": "x"}
    )
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(
            "setting.change", p, {"store": "dashboard", "key": "verbosity", "value": "default"}, {}
        )
    assert exc.value.code == "not_a_list"
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(
            "setting.change", {"setting_id": _MODELS, "value": "x"}, {**_LIST_SNAP, "value": []}, {}
        )
    assert exc.value.code == "list_setting"


# ── skills.extra_paths: an editable list that no card can carry ──
#
# The path is classified (``_CANNOT_WIDEN_SETTINGS``) only so every editable path sits in
# exactly one class. A card carries no config-list value, so none can propose it. The
# refusal pin below is a tripwire: extending cards to lists makes it fail, which is the
# signal to reclassify the path as widening with a type-guarded list comparator. The
# ``str_list`` value check has its own test, ahead of the pin.

_EXTRA_PATHS = "skills.extra_paths"
_EXTRA_PATHS_SPEC = {"type": "str_list", "max_items": 64, "max_item_len": 4096}


def test_check_setting_value_takes_only_a_list_of_strings_for_str_list():
    spec = _EXTRA_PATHS_SPEC
    cards.check_setting_value(spec, [])
    cards.check_setting_value(spec, ["/b", "/a", "/b"])
    for bad in ("/a", "", None, 3, True, {"0": "/a"}, ["/a", 1], ["/a", None], [["/a"]], [False]):
        with pytest.raises(catalog.CardCatalogError) as exc:
            cards.check_setting_value(spec, bad)
        assert exc.value.code == "invalid_value", bad


def test_check_setting_value_holds_a_str_list_to_its_bounds():
    spec = {"type": "str_list", "max_items": 2, "max_item_len": 3}
    cards.check_setting_value(spec, ["abc", "de"])
    for bad, reason in (
        (["a", "b", "c"], "at most 2 items"),
        (["abcd"], "at most 3 characters"),
        (["a", "abcd"], "at most 3 characters"),
    ):
        with pytest.raises(catalog.CardCatalogError) as exc:
            cards.check_setting_value(spec, bad)
        assert exc.value.code == "invalid_value", bad
        assert reason in str(exc.value), bad


def test_a_list_value_stays_refused_on_a_card_for_the_extra_paths():
    """Pin: a card's value is scalar, so the list setting stays out of a card's reach.

    The refusal does not depend on the ``str_list`` value check, which
    ``test_check_setting_value_takes_only_a_list_of_strings_for_str_list`` covers.
    """
    for value in ([], ["/a"]):
        with pytest.raises(catalog.CardCatalogError) as exc:
            catalog.validate_params("setting.change", {"path": _EXTRA_PATHS, "value": value})
        assert exc.value.code == "invalid_param"


def test_a_scalar_or_a_list_op_is_refused_for_the_extra_paths():
    target = cards.resolve_setting({"path": _EXTRA_PATHS})
    assert target["spec"] == _EXTRA_PATHS_SPEC
    with pytest.raises(catalog.CardCatalogError) as exc:
        cards.check_target_params(target, {"value": "/a"})
    assert exc.value.code == "invalid_value"
    with pytest.raises(catalog.CardCatalogError) as exc:
        cards.check_target_params(target, {"op": "add", "item": "/a"})
    assert exc.value.code == "not_a_list"
    # The catalog refuses the op on its own too, without the gateway's pre-check.
    p = catalog.validate_params("setting.change", {"path": _EXTRA_PATHS, "op": "add", "item": "/a"})
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(
            "setting.change", p, {"store": "kirocrew", "key": _EXTRA_PATHS, "value": []}, {}
        )
    assert exc.value.code == "not_a_list"


def test_find_setting_reports_a_string_list_with_its_items(monkeypatch):
    monkeypatch.setattr(cards, "read_setting_value", lambda target: ["fable-1", 3])
    rows = cards.find_settings(_MODELS)
    row = rows[0]
    assert row["setting_id"] == _MODELS and row["writable"] is True
    assert row["value_type"] == "string_list" and row["ops"] == ["add", "remove"]
    assert row["current_value"] == ["fable-1"]


def _list_world(world, monkeypatch, hidden):
    world.hidden = list(hidden)
    real_read_state = cards.read_state

    async def read_state(kind, params, evidence, *, state, app):
        if kind == "setting.change" and params.get("setting_id") == _MODELS:
            return {**_LIST_SNAP, "value": list(world.hidden)}
        return await real_read_state(kind, params, evidence, state=state, app=app)

    monkeypatch.setattr(cards, "read_state", read_state)
    monkeypatch.setattr(routes, "_config_before", lambda route, body: None)

    async def put_dash(request):
        # The real route's merge: remove first, then append adds not yet present.
        body = await request.json()
        world.calls.append(("PUT", "/api/dashboard/config"))
        drop = set(body.get("model_picker_hidden_models_remove") or [])
        merged = [m for m in world.hidden if m not in drop]
        merged += [m for m in body.get("model_picker_hidden_models_add") or [] if m not in merged]
        world.hidden = merged
        return web.json_response({"ok": True})

    return put_dash


def _run_list(world, store, put_dash, fn):
    async def main():
        app = _app(world, store)
        app.router.add_put("/api/dashboard/config", put_dash)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            out = await fn(client)
            await asyncio.gather(*list(routes._BACKGROUND))
            return out
        finally:
            await client.close()

    return asyncio.run(main())


async def _propose_unhide(client, item="fable-1"):
    r = await client.post(
        "/api/cards/agent/propose",
        json={
            "kind": "setting.change",
            "params": {"setting_id": _MODELS, "op": "remove", "item": item},
        },
        headers=AGENT,
    )
    assert r.status == 200, await r.text()
    return await r.json()


def test_list_card_applies_once_through_the_route_and_undo_restores(world, tmp_path, monkeypatch):
    put_dash = _list_world(world, monkeypatch, ["other-2", "fable-1"])
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        card = await _propose_unhide(c)
        step = card["plan"]["apply"][0]
        first = await c.put(step["path"], json=step["body"], headers=_card_headers(card))
        again = await c.put(step["path"], json=step["body"], headers=_card_headers(card))
        after_apply = list(world.hidden)
        applied = store.public(store.get(card["id"]))
        undo = applied["plan"]["undo"][0]
        undone = await c.put(
            undo["path"], json=undo["body"], headers=_card_headers(applied, op="undo")
        )
        return card, first.status, await again.json(), after_apply, applied, undone.status

    card, first, again, after_apply, applied, undone = _run_list(world, store, put_dash, go)
    assert card["plan"]["apply"][0]["body"] == {"model_picker_hidden_models_remove": ["fable-1"]}
    assert first == 200 and again["card_replay"] is True
    assert after_apply == ["other-2"]
    assert world.calls == [("PUT", "/api/dashboard/config")] * 2  # apply once, undo once
    assert applied["plan"]["undo"][0]["body"] == {"model_picker_hidden_models_add": ["fable-1"]}
    assert undone == 200 and store.get(card["id"])["status"] == "undone"
    assert sorted(world.hidden) == ["fable-1", "other-2"]
    assert any("model_picker_hidden_models_remove" in m and "fable-1" in m for m in world.memory)


def test_list_card_refuses_a_list_changed_since_preview_or_a_different_item(
    world, tmp_path, monkeypatch
):
    put_dash = _list_world(world, monkeypatch, ["fable-1"])
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        card = await _propose_unhide(c)
        wrong = await c.put(
            "/api/dashboard/config",
            json={"model_picker_hidden_models_remove": ["other-2"]},
            headers=_card_headers(card),
        )
        world.hidden.append("late-9")  # someone edited the list meanwhile
        changed = await c.put(
            "/api/dashboard/config",
            json=card["plan"]["apply"][0]["body"],
            headers=_card_headers(card),
        )
        return (wrong.status, (await wrong.json())["code"]), (
            changed.status,
            (await changed.json())["code"],
        )

    wrong, changed = _run_list(world, store, put_dash, go)
    assert wrong == (409, "plan_mismatch")
    assert changed == (409, "changed_since_preview")
    assert world.calls == [] and world.hidden == ["fable-1", "late-9"]


def test_shim_routes_find_setting(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: (SK, ""))
    monkeypatch.setattr(
        mcp_guide, "_get", lambda path, session_key: sent.append(path) or {"settings": [{"a": 1}]}
    )
    out = json.loads(mcp_guide._call_tool_inner("find_setting", {"query": "shorter replies"}))
    assert sent == ["/api/cards/agent/settings?q=shorter+replies"]
    assert out == {"settings": [{"a": 1}]}


# ── settings diagnosis ──


@pytest.fixture
def diagnosed(monkeypatch):
    """A loaded config differing from the defaults in two known places, plus history."""
    from kiro_crew import context
    from kiro_crew.config.loader import KiroCrewConfig

    loaded = KiroCrewConfig()
    loaded.dashboard.model_picker_hidden_models = ["fable-1"]
    loaded.telegram.bot_token = SECRET_VALUE
    loaded.teams.app_password = SECRET_VALUE
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: loaded))
    days: list[dict[str, Any]] = []

    class _Mem:
        def read_history_entries(self, since=None):
            return list(days)

    monkeypatch.setattr(
        context.ContextBuilder, "get_memory_for", staticmethod(lambda *a, **k: _Mem())
    )
    return days


def _history_day(day: str, lines: list[tuple[str, str]]) -> dict[str, Any]:
    body = f"# {day}\n" + "".join(f"\n#### {t}\n{text}\n" for t, text in lines)
    return {"date": day, "content": body}


def test_diagnose_names_a_hidden_model_with_its_settings_label(diagnosed):
    rows = {r["key"]: r for r in cards.diagnose_settings()["non_default"]}
    row = rows["dashboard.model_picker_hidden_models"]
    assert row == {
        "key": "dashboard.model_picker_hidden_models",
        "store": "dashboard",
        "setting_id": "chat.selectable-models",
        "label": "Selectable Models",
        "current": ["fable-1"],
        "default": [],
    }
    # Nothing else in a default config differs from its default.
    assert set(rows) == {
        "dashboard.model_picker_hidden_models",
        "telegram.bot_token",
        "teams.app_password",
    }


def test_diagnose_never_returns_a_credential_value(diagnosed):
    result = cards.diagnose_settings()
    assert SECRET_VALUE not in json.dumps(result)
    rows = {r["key"]: r for r in result["non_default"]}
    for key in ("telegram.bot_token", "teams.app_password"):
        assert rows[key]["store"] == "kirocrew"
        assert rows[key]["current"] == {"set": True}
        assert rows[key]["default"] == {"set": False}


def test_diagnose_scrubs_credential_keys_nested_in_a_reported_value():
    out = cards._scrub({"name": "a", "env": {"API_TOKEN": SECRET_VALUE, "REGION": "x"}})
    assert out == {"name": "a", "env": {"API_TOKEN": {"set": True}, "REGION": "x"}}


def test_diagnose_recent_changes_are_newest_first_and_capped(diagnosed):
    diagnosed.append(_history_day("2026-09-01", [("09:00 PDT", "Dashboard: old (manual)")]))
    many = [(f"10:{i:02d} PDT", f"Dashboard: change {i} (card)") for i in range(60)]
    many.insert(3, ("10:59 PDT", "an unrelated memory line"))
    diagnosed.append(_history_day("2026-10-01", many))
    changes = cards.diagnose_settings()["recent_changes"]
    assert len(changes) == cards.DIAGNOSE_RECENT_MAX == 50
    assert changes[0] == {
        "date": "2026-10-01",
        "time": "10:59 PDT",
        "line": "Dashboard: change 59 (card)",
    }
    assert [c["line"] for c in changes[:2]] == [
        "Dashboard: change 59 (card)",
        "Dashboard: change 58 (card)",
    ]
    assert all(c["line"].startswith("Dashboard: ") for c in changes)
    assert "Dashboard: old (manual)" not in [c["line"] for c in changes]
    diagnosed.pop()
    assert [c["line"] for c in cards.diagnose_settings()["recent_changes"]] == [
        "Dashboard: old (manual)"
    ]


def test_diagnose_topic_filters_both_lists(diagnosed):
    diagnosed.append(
        _history_day(
            "2026-10-01",
            [
                ("09:00 PDT", "Dashboard: hid model fable-1 in Selectable Models (manual)"),
                ("09:05 PDT", "Dashboard: set Response verbosity to concise (card)"),
            ],
        )
    )
    result = cards.diagnose_settings("Selectable")
    assert [r["key"] for r in result["non_default"]] == ["dashboard.model_picker_hidden_models"]
    assert [c["time"] for c in result["recent_changes"]] == ["09:00 PDT"]
    by_key = cards.diagnose_settings("TELEGRAM")
    assert [r["key"] for r in by_key["non_default"]] == ["telegram.bot_token"]
    assert by_key["recent_changes"] == []


def test_diagnose_multi_word_topic_matches_by_any_meaningful_word(diagnosed, monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig

    KiroCrewConfig.load().dashboard.verbosity = "concise"
    diagnosed.append(
        _history_day(
            "2026-10-01",
            [("09:05 PDT", "Dashboard: set Response verbosity to concise (card)")],
        )
    )
    verbose = cards.diagnose_settings("response verbosity reply length")
    assert verbose["topic_matched"] is True
    assert verbose["non_default"][0]["setting_id"] == "chat.response-verbosity"
    assert [c["time"] for c in verbose["recent_changes"]] == ["09:05 PDT"]
    hidden = cards.diagnose_settings("model picker hidden models")
    assert hidden["topic_matched"] is True
    assert hidden["non_default"][0]["key"] == "dashboard.model_picker_hidden_models"


def test_diagnose_topic_tokens_drop_stopwords_and_short_words():
    assert cards._topic_tokens("Why did my model picker change?") == ["model", "pick"]
    assert cards._topic_tokens("a an to of") == []


def test_diagnose_unmatched_topic_falls_back_to_the_unfiltered_summary(diagnosed):
    diagnosed.append(_history_day("2026-10-01", [("09:00 PDT", "Dashboard: hid model x")]))
    full = cards.diagnose_settings()
    result = cards.diagnose_settings("zzqx quokka")
    assert result["topic_matched"] is False
    assert [r["key"] for r in result["non_default"]] == [r["key"] for r in full["non_default"]]
    assert result["recent_changes"] == full["recent_changes"]
    assert "topic_matched" not in full


def test_diagnose_output_is_size_capped(diagnosed, monkeypatch):
    monkeypatch.setattr(cards, "DIAGNOSE_OUTPUT_MAX", 400)
    result = cards.diagnose_settings()
    assert result["truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False)) <= 400


def test_diagnose_route_is_agent_only(world, tmp_path, monkeypatch):
    monkeypatch.setattr(
        cards,
        "diagnose_settings",
        lambda topic="", app=None, include_history=True: {"non_default": [], "topic": topic},
    )
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        ok = await c.get("/api/cards/agent/diagnose?topic=model", headers=AGENT)
        owner = await c.get("/api/cards/agent/diagnose", headers=OWNER)
        app = await c.get("/api/cards/agent/diagnose", headers={"X-Test-Auth": "app"})
        sub = await c.get(
            "/api/cards/agent/diagnose",
            headers={"X-Test-Auth": "internal", "X-Session-Key": "subagent:abc"},
        )
        long = await c.get("/api/cards/agent/diagnose?topic=" + "x" * 201, headers=AGENT)
        return await ok.json(), owner.status, app.status, sub.status, long.status

    body, owner, app, sub, long = _run(world, store, go)
    assert body == {"non_default": [], "topic": "model"}
    assert owner == 403 and app == 403 and sub == 403
    assert long == 400


def test_diagnose_reads_no_memory_for_a_temporary_session(world, tmp_path, monkeypatch):
    seen: list[bool] = []

    def diagnose(topic="", app=None, include_history=True):
        seen.append(include_history)
        changes = [{"line": "Dashboard: x"}] if include_history else []
        return {"non_default": [{"key": "k"}], "recent_changes": changes}

    monkeypatch.setattr(cards, "diagnose_settings", diagnose)
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        persistent = await (await c.get("/api/cards/agent/diagnose", headers=AGENT)).json()
        c.server.app["state"]._slots[SLOT].memory_mode = "temporary"
        temporary = await (await c.get("/api/cards/agent/diagnose", headers=AGENT)).json()
        return persistent, temporary

    persistent, temporary = _run(world, store, go)
    assert seen == [True, False]
    assert persistent["recent_changes"] and "recent_changes_withheld" not in persistent
    assert temporary["recent_changes"] == []
    assert temporary["recent_changes_withheld"] == "memory_reads_disabled"
    assert temporary["non_default"] == [{"key": "k"}]  # the rest is not memory


def test_diagnose_without_history_never_reads_memory(diagnosed):
    diagnosed.append(_history_day("2026-10-01", [("09:00 PDT", "Dashboard: hid model x")]))
    assert cards.diagnose_settings()["recent_changes"]
    assert cards.diagnose_settings(include_history=False)["recent_changes"] == []


def test_shim_routes_diagnose_settings(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: (SK, ""))
    monkeypatch.setattr(
        mcp_guide, "_get", lambda path, session_key: sent.append(path) or {"non_default": []}
    )
    mcp_guide._call_tool_inner("diagnose_settings", {})
    mcp_guide._call_tool_inner("diagnose_settings", {"topic": "hidden models"})
    assert sent == [
        "/api/cards/agent/diagnose",
        "/api/cards/agent/diagnose?topic=hidden+models",
    ]
