"""Every card-editable config path says whether a change to it can widen.

A path a card can write but no class names would read ``normal`` on its card,
so a setting that widens what an agent may do would ask for no acknowledgement.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

_CLASSES = {
    "_SETTING_ORDER": set(catalog._SETTING_ORDER),
    "_CODE_EXEC_SETTINGS": set(catalog._CODE_EXEC_SETTINGS),
    "_CANNOT_WIDEN_SETTINGS": set(catalog._CANNOT_WIDEN_SETTINGS),
}


def test_every_editable_path_is_classified():
    classified = set().union(*_CLASSES.values())
    missing = sorted(set(_EDITABLE_CONFIG) - classified)
    assert not missing, (
        "classify each new editable config path in change_card_catalog: "
        "_SETTING_ORDER (with its tightest-to-widest order), _CODE_EXEC_SETTINGS, "
        f"or _CANNOT_WIDEN_SETTINGS: {missing}"
    )


def test_each_path_has_exactly_one_class():
    names = list(_CLASSES)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            assert not (_CLASSES[a] & _CLASSES[b]), (a, b, sorted(_CLASSES[a] & _CLASSES[b]))


def test_the_cannot_widen_list_names_only_editable_paths():
    assert not (_CLASSES["_CANNOT_WIDEN_SETTINGS"] - set(_EDITABLE_CONFIG))


def test_ordered_values_are_values_the_route_accepts():
    for path, order in catalog._SETTING_ORDER.items():
        spec = _EDITABLE_CONFIG.get(path)
        if spec is None:
            continue
        if spec.get("type") == "bool":
            assert set(order) == {True, False}, path
        elif "values" in spec:
            assert set(order) <= set(spec["values"]), path


@pytest.mark.parametrize(
    "path,before,after,risk",
    [
        ("dashboard.tailscale.pin_scope", "node", "login", "widen"),
        ("dashboard.tailscale.pin_scope", "login", "node", "tighten"),
        ("knowledge.auto_add_documents", False, True, "widen"),
        ("knowledge.auto_add_documents", True, False, "tighten"),
        ("agent.acp_backend", "", "claude", "code_exec"),
        ("session.pool_agent", "", "builder", "code_exec"),
        # The default can bind every new chat to a Kiro Cloud profile.
        ("session_environments.default_profile", "", "cloud", "code_exec"),
        ("telemetry.enabled", False, True, "normal"),
    ],
)
def test_newly_classified_paths_read_their_class_on_the_card(path, before, after, risk):
    p = {"path": path, "value": after}
    assert catalog.build_preview("setting.change", p, {"value": before}, {})["risk"] == risk


@pytest.mark.parametrize(
    "before,after,risk",
    [
        ("node", "LOGIN", "widen"),
        ("node", " login ", "widen"),
        ("Login", "node", "tighten"),
        ("", "login", "widen"),
        ("bogus", "login", "widen"),
        ("login", "bogus", "tighten"),
        ("node", "NODE", "normal"),
    ],
)
def test_pin_scope_ranks_the_value_the_loader_applies(before, after, risk):
    p = {"path": "dashboard.tailscale.pin_scope", "value": after}
    assert catalog.build_preview("setting.change", p, {"value": before}, {})["risk"] == risk


def test_a_card_saved_under_an_older_risk_still_loads_with_the_new_one(tmp_path):
    path = tmp_path / "change_cards.json"
    store = CardStore(path)
    params = {"path": "dashboard.tailscale.pin_scope", "value": "login"}
    before = {"value": "node"}
    rec = store.propose(
        slot_key="chat-1",
        session_key="sk-1",
        kind="setting.change",
        params=params,
        reason="roam between devices",
        preview=catalog.build_preview("setting.change", params, before, {}),
        before=before,
        context={},
    )
    asyncio.run(store.flush())
    data = json.loads(path.read_text(encoding="utf-8"))
    # What a build that had not classified pin_scope wrote, signed by this store.
    old = {k: v for k, v in data["cards"][0].items() if k != "mac"}
    old["risk"] = "normal"
    data["cards"][0] = {**old, "mac": CardStore._mac(store._store_key(), old)}
    path.write_text(json.dumps(data), encoding="utf-8")

    again = CardStore(path)
    asyncio.run(again.warm())
    assert again.get(rec["id"])["risk"] == "widen"
