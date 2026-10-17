from __future__ import annotations

from pathlib import Path

import pytest

from kiro_byo.crew_dashboard_patch import (
    AUTO_MARKER,
    HTML_GUARD_MARKER,
    JEV_MARKER,
    DashboardPatchError,
    discover_dashboard_patches,
    patch_bundle_text,
)


def _legacy_bundle() -> str:
    return (
        'function a(l,m){return[{...l,name:"auto",description:""},...m]}'
        'const J="auto:jev";'
        'function j(m,d){return[{name:J,description:d},...m]}'
    )


def _write_dashboard(crew: Path, *, js: str = 'x="data-model-id"') -> Path:
    dist = crew / "static" / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    index = dist / "index.html"
    index.write_text("<!doctype html><html><head><meta charset=\"utf-8\"></head><body></body></html>", encoding="utf-8")
    (assets / "index-abc.js").write_text(js, encoding="utf-8")
    return index


def test_legacy_bundle_helper_remains_idempotent_for_recovery() -> None:
    patched = patch_bundle_text(_legacy_bundle())
    assert AUTO_MARKER in patched
    assert JEV_MARKER in patched
    assert 'startsWith("kiro-byo-type:")' in patched
    assert patch_bundle_text(patched) == patched


def test_discovery_injects_stable_html_guard_instead_of_rewriting_bundle(tmp_path: Path) -> None:
    crew = tmp_path / "kiro_crew"
    index = _write_dashboard(crew)
    original_js = (crew / "static" / "dist" / "assets" / "index-abc.js").read_text(encoding="utf-8")

    patches = discover_dashboard_patches(crew)
    assert len(patches) == 1
    patch = patches[0]
    assert patch.path == index
    assert patch.changed
    assert HTML_GUARD_MARKER in patch.patched
    assert '[data-model-id^="kiro-byo-type:"]' in patch.patched
    assert '[data-model-id="auto"]' in patch.patched
    assert '[data-model-id="auto:jev"]' in patch.patched
    assert 'content: attr(data-kiro-byo-type-label)' in patch.patched
    assert 'data-kiro-byo-type-first' in patch.patched
    assert 'data-kiro-byo-model-first' in patch.patched
    assert 'margin-top: 24px' in patch.patched
    assert 'margin-top: 34px' in patch.patched
    assert 'position: absolute' in patch.patched
    assert 'replacementFor(node, wire)' in patch.patched
    assert 'isPinAction(node)' in patch.patched
    assert 'kiro-byo-type:([a-z0-9_-]+):([a-z0-9_-]+)' in patch.patched
    assert 'content: "Sky Type"' not in patch.patched
    assert 'content: "Glitter Type"' not in patch.patched
    # Discovery itself is pure and never mutates the content-hashed bundle.
    assert (crew / "static" / "dist" / "assets" / "index-abc.js").read_text(encoding="utf-8") == original_js


def test_discovery_is_idempotent_after_guard_is_installed(tmp_path: Path) -> None:
    crew = tmp_path / "kiro_crew"
    index = _write_dashboard(crew)
    first = discover_dashboard_patches(crew)[0]
    index.write_text(first.patched, encoding="utf-8")

    second = discover_dashboard_patches(crew)[0]
    assert not second.changed
    assert second.patched == first.patched


def test_discovery_accepts_changed_minified_helper_shape_when_dom_contract_survives(tmp_path: Path) -> None:
    crew = tmp_path / "kiro_crew"
    _write_dashboard(
        crew,
        js='const totallyDifferent=()=>React.createElement("span",{"data-model-id":model.name});',
    )
    patch = discover_dashboard_patches(crew)[0]
    assert patch.changed
    assert HTML_GUARD_MARKER in patch.patched


def test_discovery_fails_closed_when_picker_dom_contract_disappears(tmp_path: Path) -> None:
    crew = tmp_path / "kiro_crew"
    _write_dashboard(crew, js='console.log("different dashboard contract")')
    with pytest.raises(DashboardPatchError, match="data-model-id"):
        discover_dashboard_patches(crew)
