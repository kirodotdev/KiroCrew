"""BYO mode member-dispatch guard: no silent official-credit fallback.

Source-shaped route tests cannot certify a complete Windows Mate 2.0 E2E path;
They do check the source transforms that Crew 0.11 will install and their
legacy-upgrade behavior. All changes affect future sessions only.
"""
from __future__ import annotations
import ast
import sys
import types
from pathlib import Path

import pytest
from kiro_byo import crew_shared_patch, crew_current_patch

ROOT = Path(__file__).resolve().parents[1]
MEMBERS = 'src/kiro_crew/members_parts/member_sessions.py'
LOADER = 'src/kiro_crew/config/loader_builders.py'


def member_source(with_override=True):
    override = 'backend_override=None' if with_override else ''
    comma = ', ' if with_override else ''
    conditional = '''    if backend_override is not None:
        return resolve_selected_backend(backend_override)
''' if with_override else ''
    return f'''def is_member_session_key(value):
    return bool(value) and value.startswith("member-")

def select_provider_backend(session_key, member_backend, configured_default{comma}{override}):
    from kiro_crew.acp_backends import resolve_selected_backend
{conditional}    if is_member_session_key(session_key):
        return resolve_selected_backend(member_backend)
    return configured_default
'''


def _execute_route(source, monkeypatch, *, allowed=True):
    acp = types.ModuleType('kiro_crew.acp_backends')
    acp.resolve_selected_backend = (lambda raw: raw) if allowed else (lambda raw: 'kas')
    monkeypatch.setitem(sys.modules, 'kiro_crew', types.ModuleType('kiro_crew'))
    monkeypatch.setitem(sys.modules, 'kiro_crew.acp_backends', acp)
    ns = {}
    exec(compile(source, '<member-route>', 'exec'), ns)
    return ns['select_provider_backend']


def test_official_member_selector_precedence_without_byo_patch(monkeypatch):
    # member_sessions.py is retired from BYO's patch inventory (R-slim): BYO no
    # longer injects anything here.  Crew's own select_provider_backend must
    # already give the right precedence unpatched -- override > member key auto
    # > configured default -- which is exactly why the marker-only patch was
    # safe to drop.  This guards that assumption against future edits.
    route = _execute_route(member_source(), monkeypatch)
    assert route('member-1', 'kiro-byo', 'kiro-byo') == 'kiro-byo'
    assert route('member-1', 'kas', 'kiro') == 'kas'
    assert route('other-session', 'kas', 'kiro-byo') == 'kiro-byo'
    assert route('member-1', 'kiro-byo', 'kiro') == 'kiro-byo'
    assert route('member-1', 'kiro-byo', 'kiro-byo', 'kas') == 'kas'
    assert route('member-1', 'kas', 'kiro', 'kiro-byo') == 'kiro-byo'


def test_member_sessions_is_retired_from_patch_inventory():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    adapter = root / 'integration/kiro_crew/v0.12-plus'
    manifest = json.loads((adapter / 'manifest.json').read_text(encoding='utf-8'))
    compat = json.loads((adapter / 'compatibility.json').read_text(encoding='utf-8'))
    assert MEMBERS not in manifest['files']
    assert MEMBERS not in compat['generated_files']
    assert MEMBERS in compat['retired_generated_files']
    # BYO's current transform entry no longer handles this file at all.
    with pytest.raises(crew_current_patch.PatchError):
        crew_current_patch.patch_text(MEMBERS, member_source())


def test_member_loader_follows_global_both_directions():
    from test_crew11_mate_byo_linkage import _load_read_backend_choice, _Section
    import sys
    read, old = _load_read_backend_choice()
    try:
        section = _Section()
        choices = [
            ({'acp_backend': 'kiro-byo', 'member_acp_backend': 'kas'}, 'kiro-byo'),
            ({'acp_backend': 'kiro-byo', 'member_acp_backend': 'kiro-byo'}, 'kiro-byo'),
            ({'acp_backend': 'kiro', 'member_acp_backend': 'kiro-byo'}, 'kas'),
            ({'acp_backend': 'kas', 'member_acp_backend': 'kiro-byo'}, 'kas'),
            ({'acp_backend': 'kiro', 'member_acp_backend': 'kas'}, 'kas'),
            ({'member_acp_backend': 'kiro-byo'}, 'kas'),
        ]
        for conf, expected in choices:
            assert read(section, conf, 'member_acp_backend') == expected
    finally:
        for name, previous in old.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def test_loader_upgrade_from_037_exact_guard():
    from test_crew11_mate_byo_linkage import _patched_loader_source
    latest = _patched_loader_source()
    current = '''    value = backend_from_stored(agent_data.get(key))
    # Kiro BYO: effective member default follows main official/BYO harness.
    if key == _MEMBER_HARNESS_KEY:
        _main = backend_from_stored(agent_data.get("acp_backend"))
        if _main == ACP_BACKEND_KIRO_BYO:
            value = ACP_BACKEND_KIRO_BYO
        elif _main in (None, ACP_BACKEND_KIRO, ACP_BACKEND_KAS):
            value = ACP_BACKEND_KAS
    if value is None:
        default = section.default(key)
        value = default if default in selectable_backends() else None
'''
    historical = '''    value = backend_from_stored(agent_data.get(key))
    # Kiro BYO: enforce BYO member backend while main BYO is active.
    if key == _MEMBER_HARNESS_KEY and backend_from_stored(agent_data.get("acp_backend")) == ACP_BACKEND_KIRO_BYO:
        value = ACP_BACKEND_KIRO_BYO
    if value is None:
        default = section.default(key)
        value = default if default in selectable_backends() else None
'''
    assert current in latest
    previous = latest.replace(current, historical, 1)
    assert crew_current_patch.inspect(LOADER, previous).state == 'upgradeable'
    assert crew_current_patch.patch_text(LOADER, previous) == latest
    assert crew_current_patch.inspect(LOADER, latest).state == 'patched'
    damaged = previous.replace('value = ACP_BACKEND_KIRO_BYO', 'value = ACP_BACKEND_KAS', 1)
    assert crew_current_patch.inspect(LOADER, damaged).state == 'incompatible'


def test_no_user_data_writes_in_patch_sources():
    for owner in (crew_shared_patch._patch_members, crew_current_patch._loader_builders):
        import inspect
        source = inspect.getsource(owner)
        assert 'update_config_locked' not in source
        assert 'unlink(' not in source
