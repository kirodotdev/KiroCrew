"""Tests for the declarative `contributes.editorBindings` contribution point.

A binding is a key chord or a modifier-click an app binds in the side panel's code
editor; the host matches the event and POSTs the file's path and the position to the
app's endpoint. Covers parse / round-trip / validation, the trigger grammar, the caps
and malformed-input reporting `contributes.fileMenuItems` established, and the
endpoint allowlist, refused at install for the same reason: a chord the reader
presses POSTs with their session to whatever the declaration names.
"""

from __future__ import annotations

from kiro_crew.apps.manifest import (
    _MAX_EDITOR_BINDINGS_PER_APP,
    RESERVED_EDITOR_CHORDS,
    AppManifest,
    EditorBindingTrigger,
)

_BINDING = {
    "id": "goto-definition",
    "label": "Go to definition",
    "trigger": {"key": "F12"},
    "endpoint": "/api/apps/code-nav/definition",
    "when": {"extensions": [".java", "KT"], "kinds": ["file"]},
}
_CLICK = {
    "id": "peek",
    "label": "Peek",
    "trigger": {"click": True, "modifiers": ["alt"]},
    "endpoint": "/api/apps/code-nav/peek",
}
_ITEM = {
    "id": "send-to-store",
    "label": "Send to store",
    "endpoint": "/api/apps/code-nav/send",
    "surfaces": ["file-overflow"],
}


def _manifest(*, bindings: object = None, items: object = None, name: str = "code-nav"):
    contributes: dict[str, object] = {}
    if bindings is not None:
        contributes["editorBindings"] = bindings
    if items is not None:
        contributes["fileMenuItems"] = items
    return AppManifest.from_dict(
        {
            "name": name,
            "version": "1.0.0",
            "displayName": "Code Nav",
            "description": "x",
            "contributes": contributes,
        }
    )


def _errors(**kwargs) -> list[str]:
    return _manifest(**kwargs).validate()


# --- parse / round-trip -----------------------------------------------------


def test_binding_round_trip():
    m = _manifest(bindings=[_BINDING, _CLICK])
    key, click = m.contributes.editorBindings
    assert key.id == "goto-definition" and key.trigger.key == "f12"
    assert key.trigger.canonical() == "f12"
    # Extensions are dot-stripped and case-folded, as the file-menu `when` is.
    assert key.when.extensions == ["java", "kt"]
    assert click.trigger.click and click.trigger.modifiers == ["alt"]
    assert click.trigger.canonical() == "click:alt"
    assert m.validate() == []
    out = m.to_dict()["contributes"]["editorBindings"]
    assert out[0] == {
        "id": "goto-definition",
        "label": "Go to definition",
        "trigger": {"key": "f12"},
        "endpoint": "/api/apps/code-nav/definition",
        "when": {"extensions": ["java", "kt"], "kinds": ["file"]},
    }
    assert out[1]["trigger"] == {"click": True, "modifiers": ["alt"]}
    assert AppManifest.from_dict(m.to_dict()).to_dict() == m.to_dict()


def test_modifiers_are_sorted_so_two_spellings_are_one_chord():
    a = EditorBindingTrigger.from_dict({"key": "F12", "modifiers": ["Shift", "primary"]})
    b = EditorBindingTrigger.from_dict({"key": "f12", "modifiers": ["primary", "shift"]})
    assert a.canonical() == b.canonical() == "primary+shift+f12"


def test_absent_bindings_yield_no_key_and_coexist_with_file_menu_items():
    assert "editorBindings" not in _manifest(items=[_ITEM]).to_dict()["contributes"]
    m = _manifest(bindings=[_BINDING], items=[_ITEM])
    assert m.validate() == []
    assert len(m.contributes.fileMenuItems) == 1 and len(m.contributes.editorBindings) == 1


def test_bindings_are_signature_covered():
    """`endpoint` decides where a reader's keystroke is sent: tampering must break the signature."""
    payload = _manifest(bindings=[_BINDING]).signing_payload()
    assert b"editorBindings" in payload
    moved = dict(_BINDING, endpoint="/api/apps/code-nav/exfiltrate")
    assert _manifest(bindings=[moved]).signing_payload() != payload
    assert b"editorBindings" not in _manifest(items=[_ITEM]).signing_payload()


def test_editor_text_is_a_permission_not_a_binding_field():
    """Whether an app may be sent the buffer after `stale` is `permissions.editor`,
    shown and approved on the app page like a route; a binding declares nothing."""
    plain = _manifest(bindings=[_BINDING])
    assert plain.permissions.editor == []
    assert "editor" not in plain.to_dict().get("permissions", {})
    m = AppManifest.from_dict({**plain.to_dict(), "permissions": {"editor": ["content"]}})
    assert m.validate() == []
    assert m.permissions.editor == ["content"]
    assert m.to_dict()["permissions"]["editor"] == ["content"]
    # Signature-covered: the grant decides whether the reader's text can be requested.
    assert m.signing_payload() != plain.signing_payload()
    # The binding itself carries no content field; a stray one is dropped, not kept.
    stray = _manifest(bindings=[dict(_BINDING, content="on-mismatch")])
    assert "content" not in stray.to_dict()["contributes"]["editorBindings"][0]


def test_unknown_editor_permission_entry_is_reported_not_read_as_nothing():
    m = AppManifest.from_dict(
        {**_manifest(bindings=[_BINDING]).to_dict(), "permissions": {"editor": ["always"]}}
    )
    assert any(
        "permissions.editor: unknown entry 'always' (allowed: content)" in e for e in m.validate()
    )


def test_editor_grant_is_staged_like_a_route():
    """An update that adds `editor: ["content"]` must wait for the owner, as a new
    api entry does: the family is in the staged set the grants file records."""
    from kiro_crew.apps.manager_records import (
        STAGED_GRANT_FAMILIES,
        _approved_grants_after_manifest_change,
        _held_back_entries,
    )

    assert "editor" in STAGED_GRANT_FAMILIES
    old = {"api": ["/api/apps/code-nav/x"]}
    new = {"api": ["/api/apps/code-nav/x"], "editor": ["content"]}
    approved = _approved_grants_after_manifest_change(
        existing_approved=None, old_permissions=old, new_permissions=new
    )
    assert approved["editor"] == []
    assert _held_back_entries(approved, new)["editor"] == ["content"]


# --- structural validation --------------------------------------------------


def test_missing_id_label_endpoint_are_reported():
    errs = _errors(bindings=[{"trigger": {"key": "F12"}}])
    assert any("missing id" in e for e in errs)
    assert any("missing label" in e for e in errs)
    assert any("missing endpoint" in e for e in errs)


def test_non_slug_and_duplicate_ids_are_reported():
    assert any("kebab slug" in e for e in _errors(bindings=[dict(_BINDING, id="Goto Def")]))
    two = [_BINDING, dict(_BINDING, trigger={"key": "F11"})]
    assert any("duplicate id" in e for e in _errors(bindings=two))


def test_two_bindings_on_one_chord_are_reported():
    errs = _errors(bindings=[_BINDING, dict(_BINDING, id="other", trigger={"key": "f12"})])
    assert any("both bind f12" in e for e in errs)


def test_trigger_grammar():
    def trig(t: object) -> list[str]:
        return _errors(bindings=[dict(_BINDING, trigger=t)])

    assert trig({"key": "F12"}) == []
    assert trig({"key": "F12", "modifiers": ["shift"]}) == []
    assert trig({"key": "o", "modifiers": ["primary", "shift"]}) == []
    assert trig({"click": True, "modifiers": ["primary"]}) == []
    # Neither or both of key / click.
    assert any("exactly one of key or click" in e for e in trig({}))
    assert any("exactly one of key or click" in e for e in trig({"key": "F12", "click": True}))
    # Keys outside the grammar are the editor's.
    for key in ("Enter", "Escape", "ArrowDown", "Tab", "F13", "Backspace", "ü"):
        assert any("must be F1-F12" in e for e in trig({"key": key})), key
    # A bare letter is typing; a bare click places the caret.
    assert any("needs at least one modifier" in e for e in trig({"key": "g"}))
    assert any("needs at least one modifier" in e for e in trig({"click": True}))
    assert any("unknown modifier" in e for e in trig({"key": "F12", "modifiers": ["meta"]}))
    # The host's own chords.
    for chord in ("primary+s", "primary+k", "alt+k", "primary+n", "alt+shift+a"):
        assert chord in RESERVED_EDITOR_CHORDS
        mods, key = chord.rsplit("+", 1)
        errs = trig({"key": key, "modifiers": mods.split("+")})
        assert any("host's own chord" in e for e in errs), chord


def test_trigger_of_the_wrong_type_is_reported_not_erased():
    assert any(
        "trigger must be an object" in e for e in _errors(bindings=[dict(_BINDING, trigger="F12")])
    )
    errs = _errors(bindings=[dict(_BINDING, trigger={"key": "F12", "modifiers": "shift"})])
    assert any("modifiers must be an array" in e for e in errs)
    errs = _errors(bindings=[dict(_BINDING, trigger={"click": "yes", "modifiers": ["alt"]})])
    assert any("click a boolean" in e for e in errs)


def test_when_kinds_may_only_name_file():
    errs = _errors(bindings=[dict(_BINDING, when={"kinds": ["dir"]})])
    assert any("may only name 'file'" in e for e in errs)
    assert any(
        "must be arrays" in e
        for e in _errors(bindings=[dict(_BINDING, when={"extensions": "java"})])
    )


def test_over_long_label_is_reported():
    assert any("label exceeds" in e for e in _errors(bindings=[dict(_BINDING, label="x" * 121)]))


def test_cap_is_enforced():
    many = [
        dict(
            _BINDING,
            id=f"g{i}",
            trigger={"key": f"f{i % 12 + 1}", "modifiers": ["alt"] if i >= 12 else []},
        )
        for i in range(_MAX_EDITOR_BINDINGS_PER_APP + 1)
    ]
    assert any("exceeds the limit" in e for e in _errors(bindings=many))
    assert not any("exceeds the limit" in e for e in _errors(bindings=many[:-1]))


def test_non_list_and_non_object_entries_are_reported_not_erased():
    assert any("must be an array" in e for e in _errors(bindings={"id": "x"}))
    errs = _errors(bindings=[_BINDING, "nope", 3])
    assert any("2 entries must be an object" in e for e in errs)


# --- endpoint allowlist -----------------------------------------------------


def test_endpoint_outside_own_namespace_is_refused_at_install():
    for endpoint in (
        "/api/shutdown",
        "/api/apps/other-app/x",
        "/api/apps/code-nav/../../shutdown",
        "/api/apps/code-nav/uninstall",
    ):
        errs = _errors(bindings=[dict(_BINDING, endpoint=endpoint)])
        assert any("editorBindings" in e and "must route under" in e for e in errs), endpoint


def test_both_app_namespaces_are_accepted():
    assert _errors(bindings=[dict(_BINDING, endpoint="/api/apps/code-nav/deep/x")]) == []
    assert _errors(bindings=[dict(_BINDING, endpoint="/apps/code-nav/api/x")]) == []
