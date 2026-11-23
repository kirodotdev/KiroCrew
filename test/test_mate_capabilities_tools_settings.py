# SPDX-License-Identifier: Apache-2.0
"""A person takes a Bot's carried tool settings off in its Abilities settings.

Adding an agent's abilities copies its ``toolsSettings`` into the Bot's file. When
a ``deniedCommands`` entry of it is one nobody can check, the worker refuses every
terminal command that is not read-only. The Abilities section lists each
tool's settings the file adds beyond the worker base with a Remove, the
capabilities ``remove`` op takes that one tool's entry out of the file, and a
capabilities card, which an agent raises, cannot name them.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import change_card_catalog as catalog_mod
from kiro_crew import mate_capabilities, mate_spec_policy, mate_worker
from kiro_crew.agent_capabilities import CapabilityError
from kiro_crew.config import paths

UNJUDGED = "(touch|mkdir).*"
SETTINGS = {"shell": {"deniedCommands": [UNJUDGED]}, "read": {"deniedPaths": ["/secret"]}}
FILE = {"name": "pixel", "tools": [], "mcpServers": {}, "toolsSettings": SETTINGS}


@pytest.fixture
def mate(tmp_path, monkeypatch):
    mates = tmp_path / "mates" / "agents"
    mates.mkdir(parents=True)
    (mates / "pixel.json").write_text(json.dumps(FILE), encoding="utf-8")
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "worker_summary", lambda member, catalog: None)
    return mates / "pixel.json"


def _body(action: str = "remove") -> dict:
    op = {"section": "toolsSettings", "id": "shell", "action": action}
    if action == "set":
        op["value"] = {}
    return {"revision": mate_capabilities._revision(FILE), "operations": [op]}


def test_removing_the_shell_settings_takes_out_only_that_entry(mate):
    assert mate_spec_policy.unjudged_denies(FILE) == [UNJUDGED]
    body = _body()

    preview = mate_capabilities.preview("pixel", body, {})
    mate_capabilities.put("pixel", {**body, "preview_token": preview["preview_token"]}, {})

    assert [(i["section"], i["id"], i["change"]) for i in preview["impact"]] == [
        ("toolsSettings", "shell", "removed")
    ]
    spec = json.loads(mate.read_text(encoding="utf-8"))
    assert spec["toolsSettings"] == {"read": {"deniedPaths": ["/secret"]}}
    assert mate_spec_policy.unjudged_denies(spec) == []


def test_tool_settings_take_only_a_remove(mate):
    with pytest.raises(CapabilityError) as refused:
        mate_capabilities.preview("pixel", _body("set"), {})

    assert refused.value.code == "invalid_operation"
    assert json.loads(mate.read_text(encoding="utf-8")) == FILE


def test_the_summary_offers_remove_for_each_tool_the_file_adds(monkeypatch):
    from kiro_crew.agent_sdk import agent_specs

    # The worker base holds the same ``read`` entry, so only ``shell`` is the file's.
    base = {"name": "kirocrew-mate-worker", "toolsSettings": {"read": SETTINGS["read"]}}
    monkeypatch.setattr(agent_specs, "load_agent_spec", lambda *a, **k: dict(base))
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: dict(FILE))
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, dict(FILE)))

    rows = mate_capabilities.worker_summary("pixel", {})["rows"]

    assert [(r["id"], r["state"], r["value"], r["remove"]) for r in rows] == [
        ("shell", "local", SETTINGS["shell"], {"section": "toolsSettings", "id": "shell"})
    ]


def test_a_capabilities_card_cannot_name_tool_settings():
    params = {"member": "pixel", "draft": {"operations": _body()["operations"]}}

    with pytest.raises(catalog_mod.CardCatalogError) as refused:
        catalog_mod.validate_params(catalog_mod.KIND_CREWMATE_CAPABILITIES, params)

    assert (refused.value.code, refused.value.message) == (
        "invalid_param",
        "a Bot's tool settings are not changed by a card",
    )


def test_a_file_whose_tool_settings_are_not_an_object_still_previews(mate):
    spec = {**FILE, "toolsSettings": True}
    mate.write_text(json.dumps(spec), encoding="utf-8")
    body = {**_body(), "revision": mate_capabilities._revision(spec)}

    assert mate_capabilities.preview("pixel", body, {})["impact"] == []
