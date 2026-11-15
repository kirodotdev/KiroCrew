"""An object entry in ``resources`` (a kiro-cli knowledge base) is a capability row.

kiro-cli's ``resources`` list holds ``file://`` and ``skill://`` strings and, for a
knowledge base, an object with no string spelling. A Parent carrying one must
still load in the Capabilities editor, carry the object into the member's copy
unchanged, and route a change to it through the same review as any other
``resources`` change.
"""

from __future__ import annotations

import json

import pytest
from agent_capabilities_helpers import _accepting_kiro_cli  # noqa: F401
from agent_capabilities_helpers import editor_fixture, save, spec_for  # noqa: F401

from kiro_crew import agent_state
from kiro_crew.agent_capabilities import CapabilityError, _rows
from kiro_crew.config import loader

KB = {
    "type": "knowledgeBase",
    "name": "OncallKB",
    "source": "file://./context/knowledge",
    "indexType": "fast",
    "autoUpdate": True,
    "description": "Runbooks",
}
KB_ID = "knowledgeBase:OncallKB"


def _with_kb(specs, parent, kb=KB):
    parent["resources"] = ["file://guide.md", kb, "skill://manual/*"]
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")


def _resource_rows(view):
    return {row["id"]: row for row in view["rows"] if row["section"] == "resources"}


def test_a_parent_with_a_knowledge_base_loads_and_lists_it(editor):
    service, _, specs, parent = editor
    _with_kb(specs, parent)

    view = service.get("A")

    assert view["template"]["available"] is True
    assert _resource_rows(view)[KB_ID]["value"] == KB


def test_enrolling_carries_the_knowledge_base_unchanged_and_in_place(editor):
    service, home, specs, parent = editor
    _with_kb(specs, parent)

    save(service, [{"section": "tools", "id": "read", "action": "remove"}], enroll=True)

    assert spec_for(home, specs)["resources"] == ["file://guide.md", KB, "skill://manual/*"]


def test_removing_the_knowledge_base_drops_it_and_inherit_restores_it(editor):
    service, home, specs, parent = editor
    _with_kb(specs, parent)

    save(service, [{"section": "resources", "id": KB_ID, "action": "remove"}], enroll=True)
    assert spec_for(home, specs)["resources"] == ["file://guide.md", "skill://manual/*"]

    save(service, [{"section": "resources", "id": KB_ID, "action": "inherit"}])
    assert KB in spec_for(home, specs)["resources"]


def test_a_changed_parent_knowledge_base_waits_for_approval(editor):
    service, home, specs, parent = editor
    _with_kb(specs, parent)
    save(service, enroll=True)
    moved = {**KB, "source": "file://~/elsewhere"}
    _with_kb(specs, parent, moved)

    change = next(c for c in service.get("A")["parent_changes"] if c["id"] == KB_ID)
    assert change["kind"] == "changed"
    assert change["requires_approval"] is True
    save(service)
    assert KB in spec_for(home, specs)["resources"]

    save(service, accept_parent=[{"section": "resources", "id": KB_ID}])
    resources = spec_for(home, specs)["resources"]
    assert moved in resources and KB not in resources


def test_setting_a_knowledge_base_row_is_refused(editor):
    service, _, specs, parent = editor
    _with_kb(specs, parent)

    with pytest.raises(CapabilityError, match="invalid_resource"):
        save(
            service,
            [{"section": "resources", "id": KB_ID, "action": "set", "value": True}],
            enroll=True,
        )


def test_an_object_row_survives_the_saved_state_reader():
    rows = {section: {} for section in agent_state.CAPABILITY_SECTIONS}
    rows["resources"][KB_ID] = KB
    state = {"accepted": rows, "overrides": {s: {} for s in agent_state.CAPABILITY_SECTIONS}}
    agent_state._validate_capability_rows(state)

    rows["resources"]["knowledgeBase:Other"] = KB
    with pytest.raises(ValueError, match="capability_state_invalid"):
        agent_state._validate_capability_rows(state)


def test_an_unnamed_object_is_keyed_by_its_source_and_a_duplicate_is_one_row():
    unnamed = {"type": "knowledgeBase", "source": "file://./docs"}

    rows = _rows({"resources": [unnamed, unnamed]}, {})["resources"]

    assert rows == {"knowledgeBase:file://./docs": unnamed}


@pytest.mark.parametrize(
    "resources",
    [
        [KB, {**KB, "source": "file://./other"}],
        [{**KB, "source": "file://./other"}, KB],
        [KB, KB_ID],
    ],
    ids=["two-objects", "two-objects-reversed", "string-and-object"],
)
def test_two_different_entries_sharing_a_key_are_invalid(resources):
    with pytest.raises(CapabilityError, match="invalid_spec"):
        _rows({"resources": resources}, {})


def test_an_object_sharing_a_skill_uri_key_is_invalid():
    skill = "skill://guide/SKILL.md"
    spec = {"resources": [skill, {"type": "skill", "name": "//guide/SKILL.md"}]}

    with pytest.raises(CapabilityError, match="invalid_spec"):
        _rows(spec, {"guide": skill})


@pytest.mark.parametrize("entry", [3, None, ["file://x"]])
def test_a_resource_with_no_row_shape_is_still_invalid(entry):
    with pytest.raises(CapabilityError, match="invalid_spec"):
        _rows({"resources": ["file://guide.md", entry]}, {})


def test_a_severed_copy_refuses_to_set_its_knowledge_base_row():
    from kiro_crew.mate_capabilities import _apply_operations

    rows = _rows({"resources": [KB]}, {})
    with pytest.raises(CapabilityError, match="invalid_resource"):
        _apply_operations(
            rows, [{"section": "resources", "id": KB_ID, "action": "set", "value": True}], {}
        )
    assert rows["resources"][KB_ID] == KB


def test_a_legacy_copy_refuses_to_set_its_own_knowledge_base_row(editor):
    service, home, specs, parent = editor
    private = {**parent, "name": "private", "resources": ["file://guide.md", KB]}
    (specs / "private.json").write_text(json.dumps(private))
    agent_state.set_fork_info("private", "parent", "A")
    config = json.loads((home / "config.json").read_text())
    config["agents"]["A"]["kiro_agent"] = "private"
    (home / "config.json").write_text(json.dumps(config))
    loader._invalidate_config_cache()
    assert service.get("A")["mode"] == "legacy_snapshot"
    save(service, enroll=True)
    assert KB in spec_for(home, specs)["resources"]

    with pytest.raises(CapabilityError, match="invalid_resource"):
        save(service, [{"section": "resources", "id": KB_ID, "action": "set", "value": True}])
    assert KB in spec_for(home, specs)["resources"]
