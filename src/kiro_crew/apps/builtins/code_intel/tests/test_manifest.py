"""The shipped manifest: valid, discovered as a builtin, and in step with the routes."""

from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.builtins.code_intel.backend import routes
from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest

APP_ROOT = Path(__file__).resolve().parents[1]


def test_manifest_is_valid_and_discovered():
    manifest = AppManifest.from_json_file(APP_ROOT / "app.json")
    unchecked: list[str] = []
    assert manifest.validate(app_root=APP_ROOT, unchecked=unchecked) == []
    names = {a["name"] for a in discover_builtin_apps()}
    assert "code-intel" in names


def test_every_binding_names_an_operation_the_route_runs_and_posts_to_it():
    manifest = AppManifest.from_json_file(APP_ROOT / "app.json")
    bindings = manifest.contributes.editorBindings
    assert {b.id for b in bindings} == set(routes._BINDING_OPS)
    assert {b.endpoint for b in bindings} == {f"/api/apps/{routes.APP_NAME}/binding"}
    chords = {b.trigger.canonical() for b in bindings}
    assert chords == {"f12", "click:primary", "shift+f12", "alt+f12", "primary+shift+o"}
    # The host's own chords are not taken: save and quick search stay the host's.
    assert not chords & {"primary+s", "primary+k"}


def test_the_text_grant_is_declared_so_a_stale_answer_gets_the_buffer():
    manifest = AppManifest.from_json_file(APP_ROOT / "app.json")
    assert manifest.permissions.editor == ["content"]
    assert f"/api/apps/{routes.APP_NAME}/*" in manifest.permissions.api


def test_the_tab_entry_is_the_bundle_the_build_emits():
    manifest = AppManifest.from_json_file(APP_ROOT / "app.json")
    [tab] = manifest.contributes.panelTabs
    assert tab.id == "lsp" and tab.entry == "dist/panel.mjs"
    # `website/vite.app-bundles.config.ts` writes it there; that the build's list
    # names this app and this file is pinned on the website side
    # (`appBundles.test.ts`), where the build runs.


def test_catalogs_cover_every_sentence_the_routes_use():
    import json

    keys = {
        "no_server",
        "server_refused",
        "server_starting",
        "not_running",
        "unsupported",
        "timeout",
        "failed",
        "server_problem",
        "nothing_here",
        "truncated",
        "external_other",
        *routes._TITLE_KEYS.values(),
    }
    for path in sorted((APP_ROOT / "locales").glob("*.json")):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        missing = keys - set(catalog)
        assert not missing, f"{path.name} lacks {sorted(missing)}"
    # `external_one` exists where the language has a `one` form; the route falls
    # back to `_other` elsewhere rather than to English.
    from kiro_crew.apps.builtins.code_intel.i18n import plural

    assert plural("en", "external", 1).startswith("1 result ")
    assert "1" in plural("ja", "external", 1) and "result" not in plural("ja", "external", 1)
