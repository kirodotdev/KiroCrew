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
    type_label_map,
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
    assert 'display: none !important' in patch.patched
    # The shared catalogue is model-only outside chat, but a chat-scoped Type
    # roster is still presented through the same Crew picker. The shell guard
    # must therefore both hide stale Type cache rows outside /chat and decorate
    # them on /chat without touching the content-hashed bundle.
    assert 'html:not(.kiro-byo-chat-surface)' in patch.patched
    assert '[data-model-id="auto"]' in patch.patched
    assert '[data-model-id="auto:jev"]' in patch.patched
    assert 'data-kiro-byo-type-label' in patch.patched
    assert 'data-kiro-byo-type-first' in patch.patched
    assert 'data-kiro-byo-model-first' in patch.patched
    assert 'replacementFor(node, wire)' in patch.patched
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


def test_type_group_caption_comes_from_pack_declared_default() -> None:
    labels = type_label_map()
    assert labels["kiro-byo-type:tiga:sky"]["group"] == "形态 · 默认复合型，可手动切换"
    assert labels["kiro-byo-type:tiga:glitter"]["group"] == "形态 · 默认复合型，可手动切换"


def test_dashboard_guard_scopes_type_rows_to_chat() -> None:
    from kiro_byo.crew_dashboard_patch import guard_block

    block = guard_block({})
    assert '[data-model-id^="kiro-byo-type:"]' in block
    assert 'html:not(.kiro-byo-chat-surface)' in block
    assert 'html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) div:has' in block
    assert 'display: none !important' in block
    assert 'data-kiro-byo-type-label' in block
    assert 'data-kiro-byo-type-active' in block
    assert 'data-kiro-byo-effort-block' in block
    assert 'input[type="range"]' in block
    assert "kiro_byo_surface" not in block
    assert "markChatModelsRequest" not in block
    assert "window.location.reload()" not in block
    assert "window.fetch =" not in block
    assert "data-kiro-byo-native-auto" in block
    # Settings -> Chat is not the conversation surface. Insider routes can
    # contain the literal /chat segment under /settings, so the guard must
    # explicitly exclude that rail while retaining the /chat/<name> route.
    assert 'parts.indexOf("chat")' in block
    assert 'parts.indexOf("settings") >= 0' in block


def test_dashboard_guard_hides_native_auto_only_on_chat_type_picker() -> None:
    from kiro_byo.crew_dashboard_patch import guard_block

    block = guard_block({})
    assert 'html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) div:has' in block
    assert '[data-model-id="auto"]' in block
    assert '[data-model-id="auto:jev"]' in block
    assert 'aria-selected' in block


def test_model_helper_returns_one_stable_catalogue_shape() -> None:
    from kiro_byo.crew_patch_common import _KIRO_BYO_MODEL_HELPER

    # KiroCrew 0.10 shares one React-Query model catalogue across pickers. BYO
    # must never change the payload by URL/referer or navigation surface.
    assert 'kiro_byo_surface' not in _KIRO_BYO_MODEL_HELPER
    assert 'request.query.get(' not in _KIRO_BYO_MODEL_HELPER
    assert 'request.headers.get("Referer", "")' not in _KIRO_BYO_MODEL_HELPER
    assert 'urlsplit' not in _KIRO_BYO_MODEL_HELPER
    assert 'return type_rows + model_rows' in _KIRO_BYO_MODEL_HELPER
    # The catalog remains policy-pack driven rather than hard-coding Tiga.
    assert '_kiro_byo_get_policy_pack(mode_pack)' in _KIRO_BYO_MODEL_HELPER
    assert '_kiro_byo_type_selection_id(_pack.id, _policy.id)' in _KIRO_BYO_MODEL_HELPER
    # A catalogue refresh also repairs a stale routing sentinel in an already
    # open conversation, but only through a capability-checked backend helper.
    assert '_kiro_byo_reconcile_slot_routing' in _KIRO_BYO_MODEL_HELPER


def test_dashboard_guard_hides_cached_plain_text_types_outside_chat() -> None:
    from kiro_byo.crew_dashboard_patch import guard_block

    block = guard_block({})
    assert 'data-kiro-byo-stale-type-row' in block
    assert 'markStaleTypeRow' in block
    assert 'if (!isChatSurface())' in block
    assert 'semanticRowFor' in block


def test_dashboard_guard_batches_only_type_picker_mutations() -> None:
    from kiro_byo.crew_dashboard_patch import guard_block

    block = guard_block({})
    assert 'function touchesTypePicker' in block
    assert 'function scheduleDecorate' in block
    assert 'requestAnimationFrame' in block
    assert 'if (record.type === "attributes" && record.attributeName === "aria-selected" && touchesTypePicker(record.target))' in block
    # Regression: the old observer redecorated the entire model picker after
    # every unrelated added node anywhere in the dashboard, which made context
    # and capability refreshes visibly ripple through the open picker.
    assert 'if (record.addedNodes.length) needsDecorate = true' not in block
    assert 'scrollbar-gutter: stable' in block


def test_dashboard_guard_preserves_spa_navigation_without_page_dependent_fetches() -> None:
    from kiro_byo.crew_dashboard_patch import guard_block

    block = guard_block({})
    assert '_surfaceIsChat' in block
    assert 'syncSurface()' in block
    assert 'window.location.reload()' not in block
    assert 'kiro_byo_surface' not in block
    assert 'markChatModelsRequest' not in block
    assert 'window.fetch =' not in block
    assert '__kiroByoChatOverlayHook' not in block
    assert '__kiroByoComposeChatModels' not in block
    assert 'sanitizeModelsResponse' not in block
    assert 'Object.defineProperty(response, "json"' not in block


def test_crew10_discovery_does_not_require_compiled_jev_helper(tmp_path: Path) -> None:
    crew = tmp_path / "kiro_crew"
    js = (
        'const q="slot-selection-capabilities";'
        'const j="auto:jev";'
        'const row={"data-model-id":"m1"};'
        'const completelyDifferentBuildShape=()=>42;'
    )
    _write_dashboard(crew, js=js)
    asset = crew / "static" / "dist" / "assets" / "index-abc.js"
    before = asset.read_bytes()
    patches = discover_dashboard_patches(crew)
    assert len(patches) == 1
    assert patches[0].path.name == "index.html"
    assert patches[0].changed
    assert asset.read_bytes() == before


def test_crew10_discovery_is_chunk_filename_and_platform_agnostic(tmp_path: Path) -> None:
    crew = tmp_path / "kiro_crew"
    dist = crew / "static" / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><head></head><body></body></html>', encoding="utf-8"
    )
    (assets / "App-random-platform-hash.js").write_text(
        'const c="slot-selection-capabilities";const j="auto:jev";', encoding="utf-8"
    )
    (assets / "ui-random-hash.js").write_text(
        'const el={"data-model-id":"m1"};', encoding="utf-8"
    )
    patches = discover_dashboard_patches(crew)
    assert len(patches) == 1
    assert patches[0].path.name == "index.html"


def test_crew10_dashboard_patch_is_idempotent_without_bundle_rewrite(tmp_path: Path) -> None:
    crew = tmp_path / "kiro_crew"
    _write_dashboard(
        crew,
        js='const c="slot-selection-capabilities",j="auto:jev",el={"data-model-id":"m1"};',
    )
    first = discover_dashboard_patches(crew)
    assert len(first) == 1
    first[0].path.write_text(first[0].patched, encoding="utf-8")
    second = discover_dashboard_patches(crew)
    assert len(second) == 1
    assert not second[0].changed


def test_guard_navigation_never_reloads_or_replaces_fetch(tmp_path: Path) -> None:
    import json
    import re
    import subprocess

    from kiro_byo.crew_dashboard_patch import guard_block

    block = guard_block({
        "kiro-byo-type:tiga:sky": {"full": "Tiga — Sky", "type": "Sky Type", "group": "形态"},
        "kiro-byo-type:tiga:multi": {"full": "Tiga — Multi", "type": "Multi Type", "group": "形态"},
    })
    match = re.search(r'<script id="kiro-byo-model-label">\s*(.*?)\s*</script>', block, flags=re.S)
    assert match, "guard script missing"
    script = match.group(1)

    program = f'''
      globalThis.window = globalThis;
      const locationState = {{ pathname: "/chat/default", href: "http://crew.local/chat/default", origin: "http://crew.local", reloads: 0 }};
      locationState.reload = function(){{ this.reloads += 1; }};
      window.location = locationState;
      window.addEventListener = function(){{}};
      globalThis.history = {{
        pushState:function(_s,_t,url){{ if(url) window.location.pathname = String(url); }},
        replaceState:function(_s,_t,url){{ if(url) window.location.pathname = String(url); }},
      }};
      const classes = new Set();
      globalThis.document = {{
        documentElement: {{ classList: {{ contains:x=>classes.has(x), toggle:(x,on)=>{{ if(on) classes.add(x); else classes.delete(x); }} }} }},
        body: null,
        addEventListener:function(){{}},
      }};
      const originalFetch = async function(input) {{ return String(input); }};
      globalThis.fetch = originalFetch;
      {script}
      history.pushState({{}}, "", "/chat/other");
      const chatClass = classes.has("kiro-byo-chat-surface");
      history.pushState({{}}, "", "/settings/models");
      const settingsClass = classes.has("kiro-byo-chat-surface");
      history.pushState({{}}, "", "/agents");
      history.pushState({{}}, "", "/chat/default");
      const chatAgainClass = classes.has("kiro-byo-chat-surface");
      console.log(JSON.stringify({{
        reloads:window.location.reloads,
        fetchPreserved:globalThis.fetch === originalFetch,
        chatClass, settingsClass, chatAgainClass
      }}));
    '''
    out = subprocess.check_output(["node", "-e", program], text=True)
    data = json.loads(out.strip())
    assert data["reloads"] == 0
    assert data["fetchPreserved"] is True
    assert data["chatClass"] is True
    assert data["settingsClass"] is False
    assert data["chatAgainClass"] is True


def test_dashboard_guard_excludes_task_editors_portaled_over_chat() -> None:
    from kiro_byo.crew_dashboard_patch import guard_block

    block = guard_block({})
    assert 'function isScheduleTaskEditorOpen()' in block
    assert 'function syncTaskEditorSurface()' in block
    assert 'html.kiro-byo-task-editor-open [data-model-id^="kiro-byo-type:"]' in block
    assert 'html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open)' in block
    assert 'Cron\\s*(?:表达式|expression)' in block
    assert 'task\\s+(?:run\\s+)?frequency' in block
    assert 'dialogsMayHaveChanged && syncTaskEditorSurface()' in block
    assert '!document.documentElement.classList.contains(TASK_EDITOR_CLASS)' in block


def test_schedule_type_guard_covers_unmarked_portal_and_plain_text_options() -> None:
    from kiro_byo.crew_dashboard_patch import guard_block

    block = guard_block({
        "kiro-byo-type:tiga:sky": {"full": "Tiga — Sky", "type": "Sky Type", "group": "形态"},
        "kiro-byo-type:tiga:multi": {"full": "Tiga — Multi", "type": "Multi Type", "group": "形态"},
    })
    # Insider builds can use plain div portals, without role=dialog.
    assert "function hasScheduledFormControls(root)" in block
    assert "Fallback for the KiroCrew scheduled-task editor without accessible" in block
    assert "root.querySelectorAll('input,textarea,select,[role=\"combobox\"]')" in block
    assert "function markPlainTaskTypeRows()" in block
    assert 'data-kiro-byo-task-type-row="1"' in block
    assert 'html:not(.kiro-byo-chat-surface) [data-kiro-byo-task-type-row="1"]' in block
    assert "var active = !isChatSurface()" in block
    # Tag/presentation changes never modify model IDs or the catalogue.
    assert "if (row.querySelector('[data-model-id]')) continue;" in block
    assert "removeAttribute('data-kiro-byo-task-type-row')" in block
    assert 'function touchesLateTaskOptions(node)' in block
    assert 'touchesTypePicker(added) || touchesLateTaskOptions(added)' in block
    assert 'window.location.reload()' not in block
    assert 'window.fetch =' not in block


def test_dashboard_guard_blocks_hidden_type_rows_without_mutating_catalogue() -> None:
    from kiro_byo.crew_dashboard_patch import guard_block

    block = guard_block({})
    assert 'data-kiro-byo-type-blocked' in block
    assert "typeRow.setAttribute('inert', '')" in block
    assert "typeRow.removeAttribute('inert')" in block
    assert "if (!chatPicker) return;" in block
    assert 'window.fetch =' not in block
    assert 'window.location.reload()' not in block
