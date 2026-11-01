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
from kiro_byo import crew_shared_patch, crew11_patch

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


def test_byo_forces_new_member_but_not_regular_chat(monkeypatch):
    patched = crew11_patch.patch_text(MEMBERS, member_source())
    route = _execute_route(patched, monkeypatch)
    assert route('member-1', 'kas', 'kiro-byo') == 'kiro-byo'
    assert route('member-1', 'kiro', 'kiro-byo') == 'kiro-byo'
    assert route('other-session', 'kas', 'kiro-byo') == 'kiro-byo'
    assert route('member-1', 'kas', 'kiro') == 'kas'
    assert route('member-1', 'kiro-byo', 'kiro') == 'kiro-byo'


@pytest.mark.parametrize('explicit', ['kas', 'kiro', '', 'unknown'])
def test_official_member_session_override_blocked_when_byo_active(monkeypatch, explicit):
    route = _execute_route(crew11_patch.patch_text(MEMBERS, member_source()), monkeypatch)
    with pytest.raises(RuntimeError, match='official backend override'):
        route('member-1', 'kas', 'kiro-byo', explicit)
    # Switching to official must restore Crew's native precedence.
    assert route('member-1', 'kas', 'kiro', explicit) == explicit


def test_missing_byo_registry_fails_closed(monkeypatch):
    route = _execute_route(crew11_patch.patch_text(MEMBERS, member_source()), monkeypatch, allowed=False)
    with pytest.raises(RuntimeError, match='official fallback refused'):
        route('member-1', 'kas', 'kiro-byo')


def test_member_old_marker_upgrade_and_idempotency():
    fresh = member_source(with_override=False)
    old = crew_shared_patch._patch_members(fresh)
    # Simulate the already-installed previous marker, whose semantics were
    # identity-only and allowed Crew's native official member route.
    new_guard = '# Kiro BYO: enforce BYO for new member sessions (no official fallback)'
    assert new_guard in old
    lines = old.splitlines(keepends=True)
    guard_at = next(i for i,x in enumerate(lines) if new_guard in x)
    end = next(i for i,x in enumerate(lines[guard_at:],guard_at) if 'return "kiro-byo"' in x)
    del lines[guard_at:end+1]
    prior = ''.join(lines)
    assert crew11_patch.inspect(MEMBERS, prior).state == 'upgradeable'
    updated = crew11_patch.patch_text(MEMBERS, prior)
    assert updated == old
    assert crew11_patch.inspect(MEMBERS, updated).state == 'patched'
    assert crew11_patch.patch_text(MEMBERS, updated) == updated


def test_existing_loader_member_patch_upgrade_and_revert_on_official(monkeypatch):
    from test_crew11_mate_byo_linkage import _patched_loader_source
    patched = _patched_loader_source()
    assert '# Kiro BYO: enforce BYO member backend while main BYO is active.' in patched
    assert crew11_patch.patch_text(LOADER, patched) == patched
    old = '''    value = backend_from_stored(agent_data.get(key))
    if value is None:
        # Kiro BYO: implicit member harness follows the BYO main harness.
        # Explicit member choices (including KAS) still win. Keeping this
        # # at load time means switching the main harness back restores
'''
    # Derive a fixture of precisely the *old* BYO-owned block.
    start = patched.index('    value = backend_from_stored(agent_data.get(key))')
    end = patched.index('    elif key == _MEMBER_HARNESS_KEY', start)
    previous = '''    value = backend_from_stored(agent_data.get(key))
    if value is None:
        # Kiro BYO: implicit member harness follows the BYO main harness.
        # Explicit member choices (including KAS) still win. Keeping this
        # at load time means switching the main harness back restores
        # Crew's normal member default without leaving a hidden BYO pin.
        main_value = backend_from_stored(agent_data.get("acp_backend"))
        if key == _MEMBER_HARNESS_KEY and main_value == ACP_BACKEND_KIRO_BYO:
            value = ACP_BACKEND_KIRO_BYO
        else:
            default = section.default(key)
            value = default if default in selectable_backends() else None
'''
    old = patched[:start] + previous + patched[end:]
    assert crew11_patch.inspect(LOADER, old).state == 'upgradeable'
    assert crew11_patch.patch_text(LOADER, old) == patched
    assert crew11_patch.inspect(LOADER, patched).state == 'patched'


def test_no_user_data_writes_in_patch_sources():
    for owner in (crew_shared_patch._patch_members, crew11_patch._loader_builders):
        import inspect
        source = inspect.getsource(owner)
        assert 'update_config_locked' not in source
        assert 'unlink(' not in source
