"""Focused regression probe for non-capability materialization drift.

An empty review reaches ``put``'s no-op branch exactly when the drift sits in a
top-level key the projection does not rebuild. The review renders nothing for
such a key, so Save must not restamp the file as reviewed unless every
pass-through key on it is vouched for: inherited unchanged from the parent,
Crew's own derivation, or inert. An edit-carrying save stamps the same file and
is held to the same rule once the file has drifted.
"""

import json

import pytest
from test_agent_capabilities import (  # noqa: F401 -- editor is a pytest fixture used by name
    editor,
    save,
    spec_for,
)

from kiro_crew import agent_state
from kiro_crew.agent_capabilities import (
    CapabilityError,
    _digest,
    prepare_member_capabilities,
)


def _enrolled(service, home, specs):
    save(service, enroll=True)
    spec = spec_for(home, specs)
    return spec, spec["name"], specs / (spec["name"] + ".json")


@pytest.mark.parametrize(
    "key, value",
    [
        # Permission-shaped: an approval shortcut the review never lists.
        ("toolsSettings", {"shell": {"autoAllowReadonly": True}}),
        ("autoAllowReadonly", True),
        ("permissions", {"rules": [{"capability": "fsWrite", "effect": "allow"}]}),
        # Not permission-shaped, still ungoverned: a denylist on name shape misses these.
        ("toolsSettings", {"read": {"setting": "custom"}}),
        ("managedToolPolicy", {"deny": ["kirocrew-computer"]}),
        ("excludedTools", ["execute_bash"]),
        ("includeMcpJson", True),
        # A key no current release knows: the allowlist refuses it unread.
        ("futureField", {"anything": 1}),
    ],
)
def test_empty_review_refuses_ungoverned_drift(
    editor,  # noqa: F811 -- pytest fixture by name
    key,
    value,
):
    service, home, specs, _ = editor
    spec, target, path = _enrolled(service, home, specs)
    original_intent = agent_state.get_capabilities(target)
    spec[key] = value
    path.write_text(json.dumps(spec), encoding="utf-8")
    on_disk = path.read_bytes()
    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")

    with pytest.raises(CapabilityError, match="unreviewable_drift") as refused:
        save(service)
    # The refusal names the file only: no key name or value leaves the service.
    assert refused.value.file == target + ".json"
    assert refused.value.status == 409

    # Refused before the intent or the file is touched: nothing was restamped.
    assert agent_state.get_capabilities(target) == original_intent
    assert path.read_bytes() == on_disk
    assert spec_for(home, specs) == spec
    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")


def test_edit_carrying_save_refuses_ungoverned_drift(
    editor,  # noqa: F811 -- pytest fixture by name
):
    """The publish branch stamps the projected file too; drift is held to the same rule."""
    service, home, specs, _ = editor
    spec, target, path = _enrolled(service, home, specs)
    original_intent = agent_state.get_capabilities(target)
    # Not permission-shaped, so ``_align_permissions`` has nothing to say about it:
    # only the drift rule stands between this key and a reviewed stamp.
    spec["managedToolPolicy"] = {"deny": ["kirocrew-computer"]}
    path.write_text(json.dumps(spec), encoding="utf-8")
    on_disk = path.read_bytes()

    with pytest.raises(CapabilityError, match="unreviewable_drift"):
        save(service, [{"section": "tools", "id": "write", "action": "remove"}])

    assert agent_state.get_capabilities(target) == original_intent
    assert path.read_bytes() == on_disk
    assert spec_for(home, specs) == spec


def test_edit_carrying_save_stamps_inert_drift(editor):  # noqa: F811 -- pytest fixture by name
    """Inherited pass-through keys (``includeMcpJson`` here) never block a real edit."""
    service, home, specs, parent = editor
    assert "includeMcpJson" in parent
    spec, target, path = _enrolled(service, home, specs)
    spec["$schema"] = "https://example.invalid/agent-v1.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")

    save(service, [{"section": "tools", "id": "write", "action": "remove"}])

    repaired = spec_for(home, specs)
    assert repaired["name"] != target
    assert repaired["includeMcpJson"] == parent["includeMcpJson"]
    assert prepare_member_capabilities("A")["status"] == "unverified"


def test_empty_review_restamps_inert_drift(editor):  # noqa: F811 -- pytest fixture by name
    service, home, specs, parent = editor
    # The fixture parent carries ``includeMcpJson``; the fork inherits it unchanged,
    # which is exactly the pass-through shape the restamp must vouch for.
    assert "includeMcpJson" in parent
    spec, target, path = _enrolled(service, home, specs)
    original_intent = agent_state.get_capabilities(target)
    spec["$schema"] = "https://example.invalid/agent-v1.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    on_disk = path.read_bytes()
    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")

    save(service)

    repaired_intent = agent_state.get_capabilities(target)
    assert path.read_bytes() == on_disk
    assert repaired_intent["revision"] != original_intent["revision"]
    assert repaired_intent["materialized"] == _digest(spec)
    assert repaired_intent["materialized"] != original_intent["materialized"]
    assert prepare_member_capabilities("A")["status"] == "unverified"


def test_empty_review_writes_a_new_spec_for_reviewed_section_drift(
    editor,  # noqa: F811 -- pytest fixture by name
):
    service, home, specs, _ = editor
    spec, target, path = _enrolled(service, home, specs)
    spec["tools"] = ["read", "write"]
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")

    save(service)

    repaired = spec_for(home, specs)
    assert repaired["name"] != target
    assert repaired["tools"] == ["read"]
    assert prepare_member_capabilities("A")["status"] == "unverified"


def _drop_parent_key(service, home, specs, parent, key):
    parent_value = parent[key]
    spec, target, path = _enrolled(service, home, specs)
    assert spec.get(key) == parent_value
    del spec[key]
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")
    return spec, target, path


def test_empty_review_refuses_a_deleted_parent_key(editor):  # noqa: F811 -- pytest fixture
    """Deleting a key is drift: an absent ``includeMcpJson`` reads as true in kiro-cli."""
    service, home, specs, parent = editor
    spec, target, path = _drop_parent_key(service, home, specs, parent, "includeMcpJson")
    original_intent = agent_state.get_capabilities(target)
    on_disk = path.read_bytes()

    with pytest.raises(CapabilityError, match="unreviewable_drift"):
        save(service)
    assert agent_state.get_capabilities(target) == original_intent
    assert path.read_bytes() == on_disk


def test_edit_carrying_save_refuses_a_deleted_parent_key(editor):  # noqa: F811 -- fixture
    service, home, specs, parent = editor
    # A deny list the fork inherits; deleting it from the fork lifts the denial.
    parent["managedToolPolicy"] = {"deny": ["kirocrew-computer"]}
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    spec, target, path = _drop_parent_key(service, home, specs, parent, "managedToolPolicy")
    original_intent = agent_state.get_capabilities(target)

    with pytest.raises(CapabilityError, match="unreviewable_drift"):
        save(service, [{"section": "tools", "id": "write", "action": "remove"}])
    assert agent_state.get_capabilities(target) == original_intent
    assert spec_for(home, specs) == spec


def test_permissions_removed_for_an_old_cli_is_vouched(editor, monkeypatch):  # noqa: F811
    """On a release that refuses ``permissions``, Crew itself drops the block."""
    from kiro_crew import kiro_cli
    from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions

    service, home, specs, parent = editor
    parent["permissions"] = derived_agent_permissions(parent["allowedTools"], "parent")
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    monkeypatch.setattr(kiro_cli, "spec_permissions_supported", lambda _version: False)
    save(service, enroll=True)
    # A tools edit re-aligns permissions, which this release removes.
    save(service, [{"section": "tools", "id": "read", "action": "remove"}])
    spec = spec_for(home, specs)
    target, path = spec["name"], specs / (spec["name"] + ".json")
    assert "permissions" not in spec
    spec["$schema"] = "https://example.invalid/agent-v1.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")

    save(service)

    assert agent_state.get_capabilities(target)["materialized"] == _digest(spec)
