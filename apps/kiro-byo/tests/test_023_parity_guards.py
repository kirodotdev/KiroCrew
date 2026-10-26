"""0.2.3 regression contracts: Crew backend parity and Dyna preview locks."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from kiro_byo import crew09_patch, crew10_patch, crew_patch_common
from kiro_byo.config import ConfigError, KiroByoConfig, load_catalog_config
from kiro_byo.mode_router import normalize_policy
from kiro_byo.paths import use_app_home
from kiro_byo.policy_packs import PolicyPackError, get_policy_pack, public_policy_packs, resolve_policy
from kiro_byo.provider_admin import set_mode_pack, set_mode_selection


MEMBERS = 'src/kiro_crew/members.py'
SLOT_EFFORT = 'src/kiro_crew/dashboard/chat_api/slot_effort.py'


def _native_members() -> str:
    return '''def is_member_session_key(key):
    return bool(key) and key.startswith("member-")

def select_provider_backend(session_key, member_backend, configured_default, backend_override=None):
    from kiro_crew.acp_backends import resolve_selected_backend
    if backend_override is not None:
        return resolve_selected_backend(backend_override)
    if is_member_session_key(session_key):
        return resolve_selected_backend(member_backend)
    return configured_default
'''


def test_member_explicit_backend_keeps_native_precedence_including_empty(monkeypatch):
    native = types.ModuleType('kiro_crew.acp_backends')
    native.ACP_BACKEND_KIRO_BYO = 'kiro-byo'
    native.resolve_selected_backend = lambda backend: backend if backend in {'', 'kiro-byo', 'kas'} else ''
    monkeypatch.setitem(sys.modules, 'kiro_crew.acp_backends', native)
    patched = crew09_patch.patch_text(MEMBERS, _native_members())
    ns = {}
    exec(compile(patched, MEMBERS, 'exec'), ns)  # nosemgrep
    select = ns['select_provider_backend']
    assert select('member-alice', 'kas', 'kiro-byo') == 'kiro-byo'
    assert select('member-alice', 'kas', 'kiro-byo', 'kas') == 'kas'
    assert select('member-alice', 'kas', 'kiro-byo', '') == ''
    assert select('member-alice', 'kas', 'kiro-byo', 'unknown') == ''
    assert select('chat', 'kas', 'kiro-byo', 'kas') == 'kas'


def test_member_022_owned_patch_is_upgraded_not_considered_current():
    current = crew09_patch.patch_text(MEMBERS, _native_members())
    previous = current.replace(
        ' and backend_override is None', ''
    )
    # The old guard must not accidentally be considered a current installation.
    assert previous != current
    assert crew09_patch.inspect(MEMBERS, previous).state == 'upgradeable'
    assert crew09_patch.patch_text(MEMBERS, previous) == current
    assert crew09_patch.patch_text(MEMBERS, current) == current


def test_live_session_owner_wins_over_new_global_config(monkeypatch, tmp_path):
    import kiro_byo.config as byo_cfg
    import kiro_byo.paths as byo_paths

    monkeypatch.setattr(byo_cfg, 'load_catalog_config', lambda _p: SimpleNamespace(
        models=[object()], mode_pack='tiga', mode_type='multi',
    ))
    monkeypatch.setattr(byo_paths, 'use_app_home', lambda _p: contextlib.nullcontext())
    crew = types.ModuleType('kiro_crew')
    apps = types.ModuleType('kiro_crew.apps')
    manager = types.ModuleType('kiro_crew.apps.manager')
    manager.app_data_dir = lambda _name: tmp_path
    monkeypatch.setitem(sys.modules, 'kiro_crew', crew)
    monkeypatch.setitem(sys.modules, 'kiro_crew.apps', apps)
    monkeypatch.setitem(sys.modules, 'kiro_crew.apps.manager', manager)

    class Slot:
        def __init__(self, model, jev_route, configured, live):
            self.model, self.jev_route = model, jev_route
            self.configured, self.live = configured, live
            self._dirty = False

    class State:
        def __init__(self, slots):
            self._slots = {str(i): slot for i, slot in enumerate(slots)}
            self.sessions = SimpleNamespace(get_provider=lambda key: key.live)
            self.pushes = 0

        def push_slots_update(self):
            self.pushes += 1

    async def configured(slot):
        return slot.configured

    # These sessions are active on their old harness, despite the opposite
    # global config. One of them uses Crew's native empty-string backend id.
    official_live = SimpleNamespace(capabilities=SimpleNamespace(backend=''))
    byo_live = SimpleNamespace(capabilities=SimpleNamespace(backend='kiro-byo'))
    native = Slot('auto', True, 'kiro-byo', official_live)
    byo = Slot('kiro-byo-type:tiga:sky', False, '', byo_live)
    cold_to_byo = Slot('auto', True, 'kiro-byo', None)
    cold_to_native = Slot('kiro-byo-type:tiga:multi', False, '', None)
    concrete = Slot('demo/m', False, 'kiro-byo', None)
    state = State([native, byo, cold_to_byo, cold_to_native, concrete])
    ns = {
        'DashboardState': State, '_ChatSlot': Slot, 'ACP_BACKEND_KIRO_BYO': 'kiro-byo',
        'asyncio': asyncio, 'logger': logging.getLogger('test'),
        '_configured_backend_for_slot': configured,
        'effective_session_key': lambda slot: slot,
    }
    exec(crew10_patch._KIRO_BYO_SLOT_RECONCILE_HELPER, ns)  # nosemgrep
    changed = asyncio.run(ns['_kiro_byo_reconcile_slot_routing'](state))
    assert changed and state.pushes == 1
    assert (native.model, native.jev_route, native._dirty) == ('auto', True, False)
    assert (byo.model, byo._dirty) == ('kiro-byo-type:tiga:sky', False)
    assert cold_to_byo.model == 'kiro-byo-type:tiga:multi'
    assert cold_to_native.model == ''
    assert (concrete.model, concrete._dirty) == ('demo/m', False)


def test_crew10_previous_reconcile_helper_is_upgradeable():
    from test_crew10_adapter import _minimal_slot_effort_source
    current = crew10_patch.patch_text(SLOT_EFFORT, _minimal_slot_effort_source())
    start = current.index('    for _slot in targets:\n')
    end = current.index('        _raw = str(getattr(_slot', start)
    old = current[:start] + '''    for _slot in targets:
        try:
            _backend = await _configured_backend_for_slot(_slot)
        except Exception:
            logger.debug("Kiro BYO slot backend probe failed during routing reconciliation", exc_info=True)
            continue

''' + current[end:]
    assert crew10_patch.inspect(SLOT_EFFORT, old).state == 'upgradeable'
    assert crew10_patch.patch_text(SLOT_EFFORT, old) == current


def test_dyna_three_forms_are_visible_but_not_executable(tmp_path):
    with use_app_home(tmp_path):
        dyna = get_policy_pack('dyna')
        assert dyna is not None and not dyna.enabled
        assert [p.id for p in dyna.policies] == ['flash', 'strong', 'miracle']
        packs = public_policy_packs()['packs']
        preview = next(pack for pack in packs if pack['id'] == 'dyna')
        assert preview['status'] == 'coming_soon'
        assert preview['enabled'] is False
        assert len(preview['policies']) == 3
        assert get_policy_pack('tiga').enabled
        for policy in ('flash', 'strong', 'miracle'):
            with pytest.raises(PolicyPackError, match='not available yet'):
                resolve_policy('dyna', policy)
            with pytest.raises(PolicyPackError, match='not available yet'):
                normalize_policy(policy, 'dyna')
            with pytest.raises(ConfigError, match='not available yet'):
                set_mode_selection(policy, 'dyna')
        with pytest.raises(ConfigError, match='not available yet'):
            set_mode_pack('dyna')
        assert not (tmp_path / 'config' / 'providers.json').exists()

        # The full loader must also reject a manually edited settings file.
        cfg = tmp_path / 'config' / 'providers.json'
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({'providers': [], 'mode': {'id': 'dyna', 'type': 'flash'}}))
        with pytest.raises(ConfigError, match='not available yet'):
            load_catalog_config(cfg)

        # Spoofing the reserved ID with a custom enabled JSON cannot unlock it.
        custom = tmp_path / 'policy_packs'
        custom.mkdir()
        (custom / 'dyna.json').write_text(json.dumps({
            'schema_version': 4, 'id': 'dyna', 'enabled': True,
            'default_policy': 'flash', 'policies': [
                {'id': 'flash', 'semantics': {}, 'execution': {'routing': {'quality': 1, 'speed': 0, 'economy': 0}}}
            ],
        }))
        assert get_policy_pack('dyna').enabled is False
        assert any('reserved' in issue['error'] for issue in public_policy_packs()['errors'])


def _model_helper(monkeypatch, tmp_path, cfg_loader, *, advertised=()):
    import kiro_byo.config as byo_cfg
    import kiro_byo.paths as byo_paths

    monkeypatch.setattr(byo_cfg, 'load_catalog_config', cfg_loader)
    monkeypatch.setattr(byo_paths, 'use_app_home', lambda _home: contextlib.nullcontext())
    manager = types.ModuleType('kiro_crew.apps.manager')
    manager.app_data_dir = lambda _name: tmp_path
    monkeypatch.setitem(sys.modules, 'kiro_crew.apps.manager', manager)
    registry = SimpleNamespace(
        advertised_models=lambda _namespace: list(advertised),
        model_window=lambda _key: 0,
        refresh_advertised_models=lambda _namespace, _ids: False,
    )
    ns = {
        'web': web, 'asyncio': asyncio, 'logger': logging.getLogger('test-models'),
        'ACP_BACKEND_KIRO_BYO': 'kiro-byo',
        'model_registry': registry,
        'model_registry_namespace': lambda backend: backend,
        '_advertised_cc_models': lambda _request, _namespace: [],
        '_normalize_model_key': lambda name: name.lower(),
    }
    exec(crew_patch_common._KIRO_BYO_MODEL_HELPER, ns)  # nosemgrep
    return ns['_kiro_byo_models']


def test_model_catalog_error_is_not_healthy_empty_list(monkeypatch, tmp_path):
    def broken(_path):
        raise ValueError('catalog not readable')

    helper = _model_helper(monkeypatch, tmp_path, broken)
    with pytest.raises(web.HTTPServiceUnavailable):
        asyncio.run(helper(SimpleNamespace(app={}), configured_default='auto'))


def test_clean_unconfigured_model_catalog_is_genuinely_empty(monkeypatch, tmp_path):
    helper = _model_helper(monkeypatch, tmp_path, lambda _path: SimpleNamespace(
        providers=(), public_models=(), mode_pack='tiga',
    ))
    assert asyncio.run(helper(SimpleNamespace(app={}), configured_default='auto')) == []


def test_configured_provider_without_models_is_error(monkeypatch, tmp_path):
    helper = _model_helper(monkeypatch, tmp_path, lambda _path: SimpleNamespace(
        providers=(object(),), public_models=(), mode_pack='tiga',
    ))
    with pytest.raises(web.HTTPServiceUnavailable):
        asyncio.run(helper(SimpleNamespace(app={}), configured_default='auto'))


def test_broken_local_catalog_can_use_real_advertised_model(monkeypatch, tmp_path):
    def broken(_path):
        raise ValueError('catalog down')

    helper = _model_helper(monkeypatch, tmp_path, broken, advertised=('demo/model',))
    rows = asyncio.run(helper(SimpleNamespace(app={}), configured_default='auto'))
    assert [row['model_name'] for row in rows] == ['demo/model']


def test_dyna_stays_disabled_and_tiga_mode_fits_one_page():
    ui = (Path(__file__).resolve().parents[1] / 'ui' / 'index.mjs').read_text()
    # One mode at a time: no second Dyna section beneath active Tiga content.
    assert "className:'ka-page ka-mode-page'" in ui
    assert '...unavailablePacks.map(preview' not in ui
    assert '.ka-mode-page{overflow:hidden}' in ui
    assert '.ka-mode-page .ka-mode-body{flex:1 1 auto;min-height:0;height:auto}' in ui
    # Pre-release Dyna is still visible only as a disabled menu item.
    assert 'disabled:p.enabled===false' in ui
    assert "a.id==='tiga'?-1:b.id==='tiga'?1:0" in ui
    assert 'options:orderedPacks.map' in ui
    assert 'onClick:()=>setViewingType(pol.id)' in ui
