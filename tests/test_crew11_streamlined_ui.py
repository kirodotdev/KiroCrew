"""BYO 0.2.5 Crew 0.11: no duplicate tool-permissions UI; core management views preserved."""
from __future__ import annotations
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'ui' / 'index.mjs'


def test_frontend_has_no_duplicate_tool_center_or_permission_controls():
    ui = UI.read_text(encoding='utf-8')
    for forbidden in ("ToolCenterPage", "loadTools", "toolsData", "toolcenter.",
                      "api('/tool-center')", "['tools',tr", "tab==='tools'"):
        assert forbidden not in ui
    assert "['records',tr(" in ui
    assert "['tasks',tr(" in ui
    assert "if(tab==='overview')" in ui
    assert "else if(tab==='settings')" in ui


def test_existing_management_views_and_safe_lazy_loading_remain():
    ui = UI.read_text(encoding='utf-8')
    for view in ('OverviewPage', 'ProvidersPage', 'ModelsPage', 'ModesPage',
                 'AccountsPage', 'TasksPage', 'RecordsPage', 'SettingsPage'):
        assert f'function {view}(' in ui
        assert f'body=h({view}' in ui
    assert 'Promise.allSettled([loadStatus(),loadPresets(),loadOps()])' in ui
    assert "if(tab==='records')Promise.allSettled([loadOps(),loadTrace(),loadRecords()])" in ui
    # Backend diagnostics remain possible without an unsafe second grant mechanism.
    routes = (ROOT/'backend/routes.py').read_text(encoding='utf-8')
    assert '/tool-center' not in routes  # never existed in Crew App routes
    assert '/tool-center' in (ROOT/'src/kiro_byo/web_ui.py').read_text(encoding='utf-8')
    assert 'session/request_permission' in (ROOT/'src/kiro_byo/acp_server.py').read_text(encoding='utf-8') or 'session/request_permission' in (ROOT/'src/kiro_byo/turn_runner.py').read_text(encoding='utf-8')


def test_locales_are_complete_without_unused_tool_center_page():
    ui = UI.read_text(encoding='utf-8')
    referenced = set(re.findall(r'ui\.[0-9a-f]{12}', ui))
    keys = None
    for locale in ('zh-CN','zh-TW','en','ja','ko'):
        data=json.loads((ROOT/f'src/kiro_byo/locales/{locale}.json').read_text(encoding='utf-8'))
        assert not any(key.startswith('toolcenter.') for key in data)
        assert not (referenced-set(data)), f'{locale}: missing translations {referenced-set(data)}'
        if keys is None: keys = set(data)
        assert set(data) == keys


def test_model_and_setting_bridge_still_separate_from_bottom_status():
    from kiro_byo.crew11_model_labels import display_labels, block
    names=display_labels()
    for type_id in ('sky','multi','power','glitter'):
        model=f'kiro-byo-type:tiga:{type_id}'
        assert names[model]['short']==f'{type_id.capitalize()} Type'
        assert names[model]['full']==f'Tiga · {type_id.capitalize()} Type'
    js=block()
    assert 'data-multi-select-option' in js
    assert 'data-setting-label' in js
    assert 'onSelect' in js


def test_current_crew_rc1_source_frontend_seams_when_reference_archive_present():
    # Checked only when the actual user-supplied Crew archive is available.
    archive=Path('/mnt/data/KiroCrew-0.11.0-rc.1.zip')
    if not archive.is_file(): return
    with zipfile.ZipFile(archive) as z:
        base='KiroCrew-0.11.0-rc.1/website/src/'
        targets={
            'components/ModelDropdownList.tsx':'data-model-id',
            'components/MultiSelect.tsx':'data-multi-select-option',
            'components/SimpleSelect.tsx':'SettingsSelect',
            'pages/chat/ChatSettings.tsx':'model',
        }
        for name,marker in targets.items():
            text=z.read(base+name).decode('utf-8')
            assert marker in text, (name,marker)


def test_remaining_app_frontend_api_urls_match_registered_routes():
    ui = UI.read_text(encoding='utf-8')
    routes = (ROOT/'backend/routes.py').read_text(encoding='utf-8')
    patterns = re.findall(r'AppRoute\("(?:GET|POST|PUT|DELETE|PATCH)",\s*"([^\"]+)"',routes)
    assert patterns, 'No Crew App routes discovered'
    # UI templates interpolate IDs; API patterns express these as {id} segments.
    def route_regex(template: str) -> str:
        parts = re.split(r'(\{[^}]+\})',template)
        return '^' + ''.join('[^/]+' if p.startswith('{') else re.escape(p) for p in parts) + '$'
    discovered=re.findall(r'''api\(\s*["'`](/[^"'`]+)''',ui)
    for url in discovered:
        # Percent-encoded values may contain arbitrary text; sentinel is one
        # segment and includes every template interpolation, not its original name.
        url=re.sub(r'\$\{[^}]+\}','segment',url).split('?',1)[0]
        assert any(re.fullmatch(route_regex(p),url) for p in patterns), (url,patterns)
