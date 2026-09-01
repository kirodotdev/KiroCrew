"""Tests for the declarative `contributes.fileMenuItems` contribution point.

Covers manifest parse / round-trip / validation, the caps and malformed-input
reporting `contributes.commands` established, and the ``/api/apps/<app>/`` endpoint
allowlist -- refused at install here rather than filtered later, because a row naming
a core route is what the dashboard would otherwise POST to with the reader's session.

`fileMenuItems` shares :class:`Contributes` with `commands`, so every test that
exercises one asserts the other still parses, serializes and validates: a second
field on a shared container is exactly where the first one silently disappears.
"""

from __future__ import annotations

from kiro_crew.apps.manifest import AppManifest, app_endpoint_allowed

_ITEM = {
    "id": "send-to-store",
    "label": "Send to store",
    "icon": "Package",
    "endpoint": "/api/apps/doc-store/send",
    "surfaces": ["file-overflow", "tree-context", "folder-row"],
    "when": {"extensions": [".md", "PY"], "kinds": ["file"]},
}

_COMMAND = {"id": "do-thing", "title": "Do thing", "prompt": "go"}


def _manifest(*, items: object = None, commands: object = None, name: str = "doc-store"):
    contributes: dict[str, object] = {}
    if items is not None:
        contributes["fileMenuItems"] = items
    if commands is not None:
        contributes["commands"] = commands
    return AppManifest.from_dict(
        {
            "name": name,
            "version": "1.0.0",
            "displayName": "Doc Store",
            "description": "x",
            "contributes": contributes,
        }
    )


# --- parse / round-trip -----------------------------------------------------


def test_file_menu_item_round_trip():
    m = _manifest(items=[_ITEM])
    (item,) = m.contributes.fileMenuItems
    assert item.id == "send-to-store"
    assert item.label == "Send to store"
    assert item.endpoint == "/api/apps/doc-store/send"
    assert item.surfaces == ["file-overflow", "tree-context", "folder-row"]
    # Extensions normalize: dot stripped, case folded, so the render side compares
    # literally against a lowercased suffix.
    assert item.when.extensions == ["md", "py"]
    assert item.when.kinds == ["file"]
    assert m.validate() == []
    assert m.to_dict()["contributes"]["fileMenuItems"][0]["id"] == "send-to-store"


def test_absent_contributes_yields_no_items_and_no_key():
    m = AppManifest.from_dict(
        {"name": "a", "version": "1.0.0", "displayName": "A", "description": "x"}
    )
    assert m.contributes.fileMenuItems == []
    assert "contributes" not in m.to_dict()


def test_commands_and_file_menu_items_coexist_on_one_contributes():
    """The regression this whole field is at risk of: two contributions, one container."""
    m = _manifest(items=[_ITEM], commands=[_COMMAND])
    assert [c.id for c in m.contributes.commands] == ["do-thing"]
    assert [i.id for i in m.contributes.fileMenuItems] == ["send-to-store"]
    serialized = m.to_dict()["contributes"]
    assert set(serialized) == {"commands", "fileMenuItems"}
    assert m.validate() == []


def test_commands_still_round_trip_with_no_file_menu_items():
    m = _manifest(commands=[_COMMAND])
    assert [c.id for c in m.contributes.commands] == ["do-thing"]
    assert m.to_dict()["contributes"] == {"commands": [_COMMAND]}


def test_file_menu_items_are_signature_covered():
    """`endpoint` decides where a reader's chosen path is sent, so tampering with it
    must invalidate the signature rather than verify unchanged."""
    payload = _manifest(items=[_ITEM]).signing_payload()
    assert b"fileMenuItems" in payload
    moved = dict(_ITEM, endpoint="/api/apps/doc-store/exfiltrate")
    assert _manifest(items=[moved]).signing_payload() != payload
    # An app contributing only commands keeps the bytes it produced before this field
    # existed, so signatures issued earlier still verify.
    assert b"fileMenuItems" not in _manifest(commands=[_COMMAND]).signing_payload()


# --- structural validation --------------------------------------------------


def _errors(**kwargs) -> list[str]:
    return _manifest(**kwargs).validate()


def test_missing_id_label_endpoint_are_reported():
    errs = _errors(items=[{"surfaces": ["file-overflow"]}])
    assert any("missing id" in e for e in errs)
    assert any("missing label" in e for e in errs)
    assert any("missing endpoint" in e for e in errs)


def test_non_slug_id_is_reported():
    bad = dict(_ITEM, id="Send_To_Store")
    assert any("kebab slug" in e for e in _errors(items=[bad]))


def test_duplicate_id_is_reported():
    assert any("duplicate id" in e for e in _errors(items=[_ITEM, dict(_ITEM)]))


def test_unknown_surface_is_reported():
    bad = dict(_ITEM, surfaces=["sidebar"])
    assert any("unknown surface" in e for e in _errors(items=[bad]))


def test_no_surface_is_reported():
    bad = dict(_ITEM, surfaces=[])
    assert any("at least one surface" in e for e in _errors(items=[bad]))


def test_unknown_when_kind_is_reported():
    bad = dict(_ITEM, when={"kinds": ["symlink"]})
    assert any("unknown kind" in e for e in _errors(items=[bad]))


def test_over_long_label_is_reported():
    bad = dict(_ITEM, label="x" * 121)
    assert any("label exceeds" in e for e in _errors(items=[bad]))


def test_label_cap_counts_utf16_units_like_the_renderer():
    """Mirrors `_mirrored_len`: an astral character is one code point here and two
    `.length` units in the menu, so a label the manifest accepted would otherwise be
    dropped by the renderer with no error."""
    bad = dict(_ITEM, label="\U0001f600" * 61)  # 61 code points, 122 UTF-16 units
    assert any("label exceeds" in e for e in _errors(items=[bad]))


def test_item_cap_is_enforced():
    items = [dict(_ITEM, id=f"row-{n}") for n in range(11)]
    assert any("exceeds the limit" in e for e in _errors(items=items))


# --- malformed input is REPORTED, never silently coerced --------------------


def test_non_list_file_menu_items_is_reported_not_erased():
    m = _manifest(items="nope")
    assert m.contributes.bad_file_menu_items is True
    assert any("must be an array" in e for e in m.validate())


def test_non_object_entry_is_counted_and_reported():
    m = _manifest(items=[_ITEM, "junk", 3])
    assert m.contributes.dropped_file_menu_items == 2
    assert any("must be an object" in e for e in m.validate())


def test_non_list_surfaces_is_reported_not_erased():
    m = _manifest(items=[dict(_ITEM, surfaces="file-overflow")])
    assert any("surfaces must be an array" in e for e in m.validate())


def test_non_list_when_field_is_reported_not_erased():
    """A `when` that coerces to empty does not narrow anything -- it shows the row
    everywhere the author meant to restrict it."""
    m = _manifest(items=[dict(_ITEM, when={"extensions": "md"})])
    assert any("must be arrays" in e for e in m.validate())


def test_non_object_contributes_block_is_reported():
    m = AppManifest.from_dict(
        {
            "name": "a",
            "version": "1.0.0",
            "displayName": "A",
            "description": "x",
            "contributes": "nope",
        }
    )
    assert m.contributes.bad_block is True
    assert any("must be an object" in e for e in m.validate())


# --- endpoint allowlist (§9.3) ---------------------------------------------


def test_endpoint_outside_own_namespace_is_refused_at_install():
    for endpoint in (
        "/api/shutdown",  # a core route
        "/api/apps/other-app/send",  # another app's namespace
        "/api/apps/doc-store-evil/send",  # sibling-prefix collision
        "/api/apps/doc-store/../../shutdown",  # dot-segment traversal
        "/api/apps/doc-store/%2e%2e/%2e%2e/shutdown",  # percent-encoded traversal
    ):
        errs = _manifest(items=[dict(_ITEM, endpoint=endpoint)]).validate()
        assert any("must route under" in e for e in errs), endpoint


def test_endpoint_inside_own_namespace_is_accepted():
    assert _manifest(items=[dict(_ITEM, endpoint="/api/apps/doc-store/deep/send")]).validate() == []


def test_app_endpoint_allowed_is_the_one_shared_check():
    """`publishProvider` and `fileMenuItems` both call this, so its edges are pinned
    here rather than in each caller."""
    assert app_endpoint_allowed("foo", "/api/apps/foo/x") is True
    assert app_endpoint_allowed("foo", "/api/apps/foo/") is True
    assert app_endpoint_allowed("foo", "/api/apps/foobar/x") is False
    assert app_endpoint_allowed("foo", "/api/apps/foo/../../shutdown") is False
    assert app_endpoint_allowed("foo", "/api/core") is False
    assert app_endpoint_allowed("", "/api/apps/foo/x") is False
    assert app_endpoint_allowed("foo", "") is False
