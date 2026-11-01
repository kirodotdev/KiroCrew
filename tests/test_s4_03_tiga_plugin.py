"""S4-03: Tiga built-in Mode plug-in, compatibility and fail-closed checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kiro_byo.modes import builtin_modes
from kiro_byo.mode_registry import load_mode_registry, MODE_PLUGIN_API_VERSION
from kiro_byo.paths import use_app_home
from kiro_byo.policy_packs import (
    PolicyPackError, default_pack, get_policy_pack, load_policy_packs,
    public_policy_packs, resolve_policy,
)
from kiro_byo.mode_router import route_model
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec


# Frozen SHA256 from S4-02: Tiga Type weighting and execution policy are not
# altered by the Mode Registry migration.
TIGA_S402_POLICY_SHA = '0c09ad73ea883b38ae177afb4b0bc88a8915cf4e95b85f1b88836ed0bb743969'
DYNA_S402_POLICY_SHA = '6a04ec5da69d631ff86c36835db7b1e5ad3feff1ce918529a4dd398a005773a0'


def test_tiga_is_explicit_built_in_mode_and_policy_byte_identical():
    definitions = builtin_modes()
    assert tuple(mode.id for mode in definitions) == ('tiga', 'dyna')
    tiga, dyna = definitions
    assert tiga.type_ids == ('sky', 'multi', 'power', 'glitter')
    assert tiga.default_type == 'multi' and tiga.enabled
    assert dyna.type_ids == ('miracle', 'flash', 'strong')
    assert dyna.default_type == 'flash' and dyna.enabled
    assert hashlib.sha256(tiga.policy_path.read_bytes()).hexdigest() == TIGA_S402_POLICY_SHA
    assert hashlib.sha256(dyna.policy_path.read_bytes()).hexdigest() == DYNA_S402_POLICY_SHA
    registry = load_mode_registry()
    assert registry.get('tiga').declaration == tiga
    assert registry.get('dyna').declaration == dyna
    assert all(mode.registry_api == MODE_PLUGIN_API_VERSION for mode in definitions)
    assert registry.default().id == 'tiga'
    assert registry.packs == load_policy_packs()


def test_existing_policy_apis_and_selection_identity_are_stable():
    tiga = get_policy_pack('tiga')
    assert tiga is not None
    assert tiga.source == 'builtin'
    assert tiga == default_pack()
    catalog = public_policy_packs()
    assert catalog['mode_plugin_kind'] == 'data-only-policy-pack'
    row = next(row for row in catalog['packs'] if row['id'] == 'tiga')
    assert tuple(p['selection_id'] for p in row['policies']) == (
        'kiro-byo-type:tiga:sky', 'kiro-byo-type:tiga:multi',
        'kiro-byo-type:tiga:power', 'kiro-byo-type:tiga:glitter',
    )
    for type_id in ('sky', 'multi', 'power', 'glitter'):
        assert resolve_policy('tiga', type_id)[1] == tiga.policy(type_id)
    for type_id in ('miracle', 'flash', 'strong'):
        assert resolve_policy('dyna', type_id)[1].id == type_id


def test_tiga_routes_all_four_types_without_a_decision_model():
    models = (
        ModelSpec(provider_id='demo', provider_name='Demo', model_id='fast', name='Fast',
                  routing_quality=0.45, routing_speed=0.95, routing_economy=0.9, tool_use=True),
        ModelSpec(provider_id='demo', provider_name='Demo', model_id='strong', name='Strong',
                  routing_quality=0.97, routing_speed=0.38, routing_economy=0.2, tool_use=True),
    )
    provider = ProviderSpec('demo', 'Demo', 'openai-compatible', 'key', '', models)
    for type_id in ('sky', 'multi', 'power', 'glitter'):
        config = KiroByoConfig((provider,), mode_pack='tiga', mode_type=type_id)
        decision = route_model(config, prompt='Summarize this design proposal', history=[],
                               has_image=False, tools_expected=False)
        assert decision is not None
        assert decision.pack == 'tiga'
        assert decision.policy == type_id
        assert decision.policy_definition == resolve_policy('tiga', type_id)[1].execution
        assert decision.model in models


def test_user_mode_hot_reload_remains_data_only_and_legacy_tiga_override_is_preserved(tmp_path: Path):
    with use_app_home(tmp_path):
        directory = tmp_path / 'policy_packs'
        directory.mkdir()
        (directory / 'evil.py').write_text('raise RuntimeError("must never import user Python")')
        raw = {
            'schema_version': 4, 'id': 'aurora', 'default_policy': 'light',
            'policies': [{'id': 'light', 'semantics': {}, 'execution': {'routing': {
                'quality': .2, 'speed': .6, 'economy': .2,
            }}}],
        }
        filename = directory / 'aurora.json'
        filename.write_text(json.dumps(raw))
        assert load_mode_registry().get('aurora').resolve_type('light').routing.speed == pytest.approx(.6)
        raw['policies'][0]['execution']['routing']['speed'] = .7
        filename.write_text(json.dumps(raw))
        assert load_mode_registry().get('aurora').resolve_type('light').routing.speed == pytest.approx(.7)
        assert load_mode_registry().get('tiga').declaration is not None
        # Legacy manually provided Tiga JSON remains loadable; S4-03 does not
        # silently break existing configurations that used that override.
        tiga = json.loads(builtin_modes()[0].policy_path.read_text('utf-8'))
        (directory / 'tiga.json').write_text(json.dumps(tiga))
        plugin = load_mode_registry().get('tiga')
        assert plugin.pack.source == 'custom'
        assert plugin.declaration is None
        assert plugin.resolve_type('sky').id == 'sky'
        # Dyna remains reserved regardless of user JSON contents.
        dyna = json.loads(builtin_modes()[1].policy_path.read_text('utf-8'))
        dyna['enabled'] = True
        (directory / 'dyna.json').write_text(json.dumps(dyna))
        registry = load_mode_registry()
        assert registry.get('dyna').enabled is True
        assert any('reserved' in row['error'] for row in registry.errors)


def test_corrupt_builtin_contract_is_not_silently_activated(monkeypatch, tmp_path: Path):
    import kiro_byo.policy_packs as policy_module
    import kiro_byo.modes as builtin_module
    from dataclasses import replace
    tiga, dyna = builtin_modes()
    mismatched = replace(tiga, type_ids=('not-sky', 'multi', 'power', 'glitter'))
    monkeypatch.setattr(builtin_module, 'builtin_modes', lambda: (mismatched, dyna))
    packs, errors = policy_module._discover_policy_packs(include_errors=True)
    assert not any(pack.id == 'tiga' and pack.source == 'builtin' for pack in packs)
    assert any("builtin Mode 'tiga' does not match" in e['error'] for e in errors)
