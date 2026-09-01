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

import re
from pathlib import Path

import kiro_crew
from kiro_crew.apps.manifest import (
    CORE_APP_ROUTE_SEGMENTS,
    RESERVED_APP_PATH_SEGMENTS,
    AppManifest,
    app_endpoint_allowed,
)

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
        "/api/apps/doc-store/uninstall",  # a CORE route inside the app's own namespace
        "/api/apps/doc-store/uninstall/preview",  # ... and a child of one
        "/api/apps/doc-store/_jobs/active",  # the shared Job SDK surface
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


# --- core-owned segments inside the app's own namespace ---------------------


def test_core_owned_first_segment_is_refused_even_inside_the_app_namespace():
    """The app's own prefix is NOT sufficient: core mounts its lifecycle handlers there.

    Without this, an app declares `endpoint: /api/apps/<itself>/uninstall`, passes install
    validation, and a reader clicking that menu row POSTs to core's uninstall handler with
    their own session.
    """
    for segment in sorted(CORE_APP_ROUTE_SEGMENTS):
        assert app_endpoint_allowed("foo", f"/api/apps/foo/{segment}") is False, segment
        # A child path under a reserved segment is core's too (`uninstall/preview`).
        assert app_endpoint_allowed("foo", f"/api/apps/foo/{segment}/preview") is False


def test_a_query_string_cannot_smuggle_a_core_segment_past_the_allowlist():
    # The router matches on the PATH alone, so this still reaches core's handler.
    assert app_endpoint_allowed("foo", "/api/apps/foo/uninstall?x=1") is False
    assert app_endpoint_allowed("foo", "/api/apps/foo/uninstall#frag") is False
    # A reserved name is a whole segment, not a prefix: an app route may start with it.
    assert app_endpoint_allowed("foo", "/api/apps/foo/uninstall-helper") is True
    assert app_endpoint_allowed("foo", "/api/apps/foo/send?x=1") is True


def test_a_control_character_cannot_smuggle_a_core_segment_past_the_allowlist():
    """The browser strips TAB/LF/CR out of a URL while ``fetch`` builds the request.

    So ``"uninstall\\n"`` is not the reserved ``"uninstall"`` to a naive segment test, and
    the request still lands on core's uninstall handler with the reader's session. The
    validator uses a character ALLOWLIST, so these are refused without being enumerated.
    """
    for ch in ("\t", "\n", "\r", "\x00", "\x0b", "\x1f", "\x7f", " "):
        assert app_endpoint_allowed("foo", f"/api/apps/foo/uninstall{ch}") is False, repr(ch)
        # Also refused when the control char sits anywhere else in the path, and when it
        # arrives percent-encoded (the check runs AFTER unquote for that reason).
        assert app_endpoint_allowed("foo", f"/api/apps/foo/{ch}uninstall") is False, repr(ch)
        assert app_endpoint_allowed("foo", f"/api/apps/foo/send{ch}") is False, repr(ch)
    assert app_endpoint_allowed("foo", "/api/apps/foo/uninstall%0a") is False
    assert app_endpoint_allowed("foo", "/api/apps/foo/uninstall%09") is False
    # An ordinary app route with no control characters still passes.
    assert app_endpoint_allowed("foo", "/api/apps/foo/send") is True


def test_a_backslash_cannot_smuggle_a_core_segment_past_the_allowlist():
    """``\\`` is a PATH SEPARATOR to the URL parser for http(s), but not to posixpath.

    So ``/api/apps/foo/.\\uninstall`` is one opaque segment to a naive segment test --
    neither ``..`` nor a reserved word -- and the browser then normalizes it to
    ``/api/apps/foo/uninstall`` and POSTs to core's uninstall handler.
    """
    for endpoint in (
        "/api/apps/foo/.\\uninstall",
        "/api/apps/foo/.\\token",
        "/api/apps/foo\\uninstall",
        "/api/apps/foo/x\\..\\uninstall",
        "/api/apps/foo/%5cuninstall",  # percent-encoded, judged after unquote
        "/api/apps/foo/%2e%5cuninstall",
    ):
        assert app_endpoint_allowed("foo", endpoint) is False, endpoint


def test_the_endpoint_character_allowlist_admits_a_real_route_and_nothing_exotic():
    """An allowlist, so the refusals above hold for characters nobody enumerated."""
    for ok in (
        "/api/apps/foo/send",
        "/api/apps/foo/send-item",
        "/api/apps/foo/send_item",
        "/api/apps/foo/v1.2/send",
        "/api/apps/foo/send?path=/a/b&kind=file",
        "/api/apps/foo/send#frag",
    ):
        assert app_endpoint_allowed("foo", ok) is True, ok
    for bad in (
        '/api/apps/foo/send"x',
        "/api/apps/foo/send<x",
        "/api/apps/foo/send>x",
        "/api/apps/foo/send|x",
        "/api/apps/foo/send{x}",
        "/api/apps/foo/send[x]",
        "/api/apps/foo/send^x",
        "/api/apps/foo/send`x",
        "/api/apps/foo/send\u00a0x",  # NBSP
        "/api/apps/foo/send\u2028x",  # LINE SEPARATOR
    ):
        assert app_endpoint_allowed("foo", bad) is False, bad


def test_reserved_app_names_never_own_their_api_apps_path():
    """A reserved segment is a SHARED core route, not an app's namespace.

    ``/api/apps/registries/refresh`` and ``/api/apps/registry/install`` are literal routes
    mounted before the ``/api/apps/{name}`` catch-all, and the segment below the prefix
    (``refresh``, ``install``) is not a per-app lifecycle name, so
    :data:`CORE_APP_ROUTE_SEGMENTS` does not cover them. Reserving the NAME is documented
    as forward-looking only — it leaves an app already published under one — and the
    token_auth carve-out that constrains such an app governs an APP's own token, not a row
    a reader clicks with their own session.
    """
    for name in sorted(RESERVED_APP_PATH_SEGMENTS):
        assert app_endpoint_allowed(name, f"/api/apps/{name}/refresh") is False, name
        assert app_endpoint_allowed(name, f"/api/apps/{name}/install") is False, name
        assert app_endpoint_allowed(name, f"/api/apps/{name}/anything") is False, name
        # The proxy namespace is refused for these names too, in either flag position.
        assert (
            app_endpoint_allowed(name, f"/apps/{name}/api/x", allow_proxy_namespace=True) is False
        ), name
    # The two named in the route table are the sharp ones: both would otherwise pass,
    # because neither `refresh` nor `install` is a per-app lifecycle segment.
    assert "refresh" not in CORE_APP_ROUTE_SEGMENTS
    assert "install" not in CORE_APP_ROUTE_SEGMENTS
    # An ordinary app is unaffected, including one whose name merely CONTAINS a
    # reserved word.
    assert app_endpoint_allowed("registry-helper", "/api/apps/registry-helper/x") is True
    assert app_endpoint_allowed("my-registry", "/api/apps/my-registry/x") is True


def test_reserved_segment_mirrors_agree_across_python_and_the_frontend():
    """Two copies of one control drift apart invisibly, so pin them to each other."""
    root = Path(kiro_crew.__file__).resolve().parent.parent.parent
    src = (root / "website" / "src" / "apps" / "coreAppRoutes.ts").read_text(encoding="utf-8")
    block = src[src.index("RESERVED_APP_PATH_SEGMENTS = new Set([") :]
    block = block[: block.index("])")]
    mirrored = set(re.findall(r"'([^']+)'", block))
    assert mirrored == set(
        RESERVED_APP_PATH_SEGMENTS
    ), f"frontend mirror {sorted(mirrored)} != python {sorted(RESERVED_APP_PATH_SEGMENTS)}"


def test_both_documented_app_namespaces_are_admitted():
    """Which namespace an app owns is not the app's choice, so both must validate.

    An app declaring ``backend.entryPoint`` runs its own process and is reverse-proxied at
    ``/apps/<app>/api/``; one declaring only ``backend.hooks.routes`` is registered
    in-gateway under ``/api/apps/<app>/``. Admitting only the second refused every
    process-backed app's contributed row at install, so those apps could contribute
    nothing at all.
    """
    allow = {"allow_proxy_namespace": True}
    assert app_endpoint_allowed("foo", "/api/apps/foo/send", **allow) is True
    assert app_endpoint_allowed("foo", "/apps/foo/api/send", **allow) is True
    assert app_endpoint_allowed("foo", "/apps/foo/api/v1/send?x=1", **allow) is True
    # Still another app's namespace, in either form.
    assert app_endpoint_allowed("foo", "/apps/bar/api/send", **allow) is False
    assert app_endpoint_allowed("foo", "/apps/foobar/api/send", **allow) is False
    # The bare proxy root IS a route: the proxy is registered as
    # `/apps/{name}/api/{path:.*}` and that tail may be empty, which matches how the
    # in-gateway namespace already admits its own bare root.
    assert app_endpoint_allowed("foo", "/apps/foo/api", **allow) is True
    # But the app root without the `api` segment is not in either namespace.
    assert app_endpoint_allowed("foo", "/apps/foo/send", **allow) is False
    # The character allowlist and traversal guards apply to both namespaces.
    assert app_endpoint_allowed("foo", "/apps/foo/api/send\n", **allow) is False
    assert app_endpoint_allowed("foo", "/apps/foo/api/.\\send", **allow) is False
    assert app_endpoint_allowed("foo", "/apps/foo/api/../../shutdown", **allow) is False


def test_proxy_namespace_is_opt_in_per_caller():
    """This is the ONE shared endpoint check, so widening it for everyone is not free.

    The publish-provider registry in ``routes.py`` states ``/api/apps/<app>/`` in its own
    refusal message and must keep that narrower shape; the default therefore refuses the
    proxy prefix, and a caller that needs it says so.
    """
    assert app_endpoint_allowed("foo", "/apps/foo/api/send") is False
    assert app_endpoint_allowed("foo", "/apps/foo/api/send", allow_proxy_namespace=False) is False
    # The in-gateway namespace is unaffected by the flag in either position.
    for flag in (True, False):
        assert app_endpoint_allowed("foo", "/api/apps/foo/send", allow_proxy_namespace=flag) is True
        assert (
            app_endpoint_allowed("foo", "/api/apps/foo/uninstall", allow_proxy_namespace=flag)
            is False
        )


def test_publish_provider_registry_does_not_admit_the_proxy_namespace():
    """Pinned at the CALL, because the default is what protects that surface."""
    src = (Path(kiro_crew.__file__).parent / "apps" / "routes.py").read_text(encoding="utf-8")
    call = src[src.index("if not app_endpoint_allowed(app_name, endpoint)") :][:120]
    assert "allow_proxy_namespace" not in call


def test_core_reserved_segments_apply_only_to_the_in_gateway_namespace():
    """The proxy namespace forwards wholesale into the app's process.

    Core serves nothing under ``/apps/<app>/api/``, so a name that is reserved in the
    in-gateway namespace is just one of the app's own routes there.
    """
    for segment in sorted(CORE_APP_ROUTE_SEGMENTS):
        assert app_endpoint_allowed("foo", f"/api/apps/foo/{segment}") is False, segment
        assert (
            app_endpoint_allowed("foo", f"/apps/foo/api/{segment}", allow_proxy_namespace=True)
            is True
        ), segment


def test_core_route_segments_covers_every_core_route_in_the_app_namespace():
    """Drift guard: the reserved list is derived from the code, not from memory.

    A new core route mounted under ``/api/apps/{name}/`` is exactly the change that
    silently re-opens the hole above, and nothing else in the tree would notice. The scan
    is deliberately whole-tree rather than a fixed file list, so a core route added in a
    NEW module is covered too; ``scaffold.py`` is the one exclusion, because its
    ``/api/apps/{name}/status`` is a template line emitted into a GENERATED app's own
    backend rather than a route core serves.
    """
    root = Path(kiro_crew.__file__).resolve().parent
    pattern = re.compile(r"/api/apps/\{(?:name|app|app_name)\}/([a-z_][a-z0-9_-]*)")
    registered: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        if path.name == "scaffold.py":
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            registered.setdefault(match.group(1), set()).add(path.name)
    assert registered, "the scan matched nothing -- the pattern or the tree layout moved"
    assert set(registered) == set(CORE_APP_ROUTE_SEGMENTS), (
        "CORE_APP_ROUTE_SEGMENTS is out of date with the routes core mounts under "
        f"/api/apps/{{name}}/ -- scanned {sorted(registered)}. Add or remove the segment "
        "in BOTH manifest.py and website/src/apps/fileMenuContributions.tsx."
    )
