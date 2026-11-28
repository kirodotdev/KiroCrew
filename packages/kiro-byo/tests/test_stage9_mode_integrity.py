"""Stage 9: verify data-only Mode upgrades without claiming executable HMR."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_byo.mode_lifecycle import (
    ModeLifecycleError, install_or_update_mode, mode_lifecycle_status,
    rollback_custom_mode,
)
from kiro_byo.mode_registry import load_mode_registry
from kiro_byo.paths import use_app_home
from kiro_byo.policy_packs import public_policy_packs, resolve_policy


def _pack(version: str = '1.0.0', *, quality: float = 0.2):
    return {
        'id': 'aurora', 'schema_version': 4, 'registry_api': 1,
        'plugin_version': version, 'display_name': 'Aurora',
        'enabled': True, 'default_policy': 'flash',
        'policies': [{
            'id': 'flash', 'semantics': {'display_name': 'Flash'},
            'execution': {'routing': {'quality': quality, 'speed': .7, 'economy': .1}},
        }],
    }


@pytest.mark.parametrize('tamper', ['missing', 'modified'])
def test_status_does_not_claim_rollback_when_previous_snapshot_lost(tmp_path, tamper):
    with use_app_home(tmp_path):
        install_or_update_mode(_pack())
        install_or_update_mode(_pack('1.1.0', quality=.25))
        previous = tmp_path / 'policy_packs' / '.history' / 'aurora' / 'previous.json'
        if tamper == 'missing':
            previous.unlink()
        else:
            previous.write_text('corrupted', encoding='utf-8')
        status = mode_lifecycle_status('aurora')
        assert not status['rollback_available']
        assert status['rollback_conflicted']
        with pytest.raises(ModeLifecycleError, match='missing or corrupted'):
            rollback_custom_mode('aurora')


def test_builtin_mode_declarations_and_type_selections_work(tmp_path):
    with use_app_home(tmp_path):
        registry = load_mode_registry()
        tiga = registry.get('tiga')
        assert tiga is not None and tiga.enabled
        assert tiga.declaration is not None
        assert tiga.type_ids == ('sky', 'multi', 'power', 'glitter')
        assert tiga.pack.default_policy == 'multi'
        for type_id in tiga.type_ids:
            pack, policy = resolve_policy('tiga', type_id)
            assert pack.id == 'tiga' and policy.id == type_id
        dyna = registry.get('dyna')
        assert dyna is not None and dyna.enabled
        assert dyna.type_ids == ('miracle', 'flash', 'strong')
        assert resolve_policy('dyna', 'flash')[1].id == 'flash' 


def test_public_mode_capability_flags_do_not_claim_executable_hotswap(tmp_path):
    with use_app_home(tmp_path):
        packs = public_policy_packs()
        assert packs['mode_plugin_kind'] == 'data-only-policy-pack'
        assert packs['hot_reload_scope'] == 'custom-json-policy-only'
        assert packs['executable_hot_swap'] is False
        by_id = {pack['id']: pack for pack in packs['packs']}
        assert by_id['tiga']['code_hot_swap'] is False
        assert by_id['tiga']['managed_upgrades'] is False
        assert by_id['dyna']['status'] == 'available'
        install_or_update_mode(_pack())
        changed = {pack['id']: pack for pack in public_policy_packs()['packs']}
        assert changed['aurora']['managed_upgrades'] is True
        assert changed['aurora']['policy_hot_reload'] is True
        assert changed['aurora']['code_hot_swap'] is False


def test_read_only_audit_script_reports_mode_contracts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    import os
    result = subprocess.run(
        [sys.executable, str(root / 'scripts' / 'audit_mode_plugins.py')],
        cwd=root, text=True, capture_output=True, timeout=20,
        env={**os.environ, 'KIRO_BYO_HOME': str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['result'] == 'pass'
    assert payload['executable_hot_swap'] is False
    by_id = {row['id']: row for row in payload['modes']}
    assert by_id['tiga']['type_ids'] == ['sky', 'multi', 'power', 'glitter']
    assert by_id['tiga']['contract_valid'] is True
    assert by_id['dyna']['enabled'] is True
    assert by_id['dyna']['contract_valid'] is True


def test_completed_tool_receipt_is_a_critical_durable_boundary(tmp_path):
    """A missing Run cannot quietly record success after tools already executed."""
    from types import SimpleNamespace
    from kiro_byo.durable_runs import DurableRunStore
    from kiro_byo.turn_runner import DurableCheckpointError, TurnRunnerMixin

    class MinimalHarness(TurnRunnerMixin):
        def _durable_run_store(self):
            return DurableRunStore(tmp_path / 'runs')

    session = SimpleNamespace(
        active_turn_id='turn:1', active_run_id='missing-run',
        active_run_step_id='step-1', active_run_attempt_id='attempt-1',
        active_run_lease_token='owner', active_run_lease_lost=False, events=[],
    )
    harness = MinimalHarness()
    with pytest.raises(DurableCheckpointError):
        harness._checkpoint_durable_run(session, 'tool_batch_completed',
                                        data={'batch_id': 'batch-a'}, critical=True)
