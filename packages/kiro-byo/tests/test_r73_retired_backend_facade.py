"""R73: the legacy acp_backends re-export is no longer a Crew mutation owner."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_byo import crew_current_patch, crew12_patch
from kiro_byo.integration_manager import IntegrationError, _plan_retired_generated_seams, _prepare_seam_install_plans

ROOT = Path(__file__).resolve().parents[1]
REL = 'src/kiro_crew/acp_backends.py'


def test_facade_is_retired_and_active_owners_import_directly():
    compat = json.loads((ROOT/'integration/kiro_crew/v0.12-plus/compatibility.json').read_text())
    assert REL in compat['retired_generated_files']
    assert REL not in crew12_patch.TARGET_FILES
    assert REL not in compat['generated_files']
    source_root = Path(os.environ.get('KIROCREW_012_SOURCE', ''))
    if not os.environ.get('KIROCREW_012_SOURCE'):
        pytest.skip('real Crew source provided only to host integration test')
    for owner in ('src/kiro_crew/acp/types.py', 'src/kiro_crew/dashboard/handlers/agents.py'):
        source = (source_root/owner).read_text()
        patched = crew12_patch.patch_text(owner, source)
        assert 'ACP_BACKEND_KIRO_BYO' in patched
        assert 'from kiro_crew.agent_sdk.backends import' in patched
        assert 'from kiro_crew.acp_backends import ACP_BACKEND_KIRO_BYO' not in patched


def _fixture(tmp_path):
    pristine_source = 'from kiro_crew.agent_sdk.backends import (\n    ACP_BACKEND_KAS,\n)\n__all__ = ["ACP_BACKEND_KAS"]\n'
    patched = crew_current_patch.patch_text(REL, pristine_source)
    assert patched != pristine_source
    crew = tmp_path/'crew'/'kiro_crew'
    dst = crew/'acp_backends.py'
    dst.parent.mkdir(parents=True)
    dst.write_text(patched)
    home = tmp_path/'home'
    backup = home/'data'/'backups'/'packaged-0.12.0'/'acp_backends.py'
    backup.parent.mkdir(parents=True)
    backup.write_text(pristine_source)
    root = home/'data'/'integration-runtime'/'0.12.0'/'overlay'
    snap = root/REL
    snap.parent.mkdir(parents=True)
    snap.write_text(patched)
    state = home/'data'/'integration.json'
    state.write_text(json.dumps({
        'detected_version':'0.12.0','crew_package':str(crew),
        'runtime_overlay_root':str(root),'generated_files':[REL]
    }))
    target = SimpleNamespace(crew_package=crew,crew_version='0.12.0',
        generated_files=frozenset(),created_files=frozenset(),
        compatibility={'retired_generated_files':[REL]},manifest={'files':[]})
    return target, home, dst, backup, snap, state


def test_retired_reexport_restored_only_with_verified_receipt(tmp_path):
    target, home, dst, backup, snap, state = _fixture(tmp_path)
    plans, *_ = _prepare_seam_install_plans(target, home, '0.12.0')
    assert len(plans) == 1 and plans[0]['retired_file']
    assert dst.read_text() != backup.read_text()  # preflight does not write
    dst.write_text(plans[0]['output_text'])
    assert dst.read_text() == backup.read_text()
    assert _plan_retired_generated_seams(target, home, '0.12.0') == []


@pytest.mark.parametrize('failure', ['no-receipt','changed-live','changed-snapshot','changed-backup','other-host'])
def test_retired_reexport_refuses_unproven_bytes(tmp_path, failure):
    target, home, dst, backup, snap, state = _fixture(tmp_path)
    if failure == 'no-receipt': state.unlink()
    elif failure == 'changed-live': dst.write_text(dst.read_text()+'\n# foreign\n')
    elif failure == 'changed-snapshot': snap.write_text('bad')
    elif failure == 'changed-backup': backup.write_text('bad')
    else:
        record = json.loads(state.read_text()); record['crew_package'] = '/foreign/kiro_crew'
        state.write_text(json.dumps(record))
    with pytest.raises(IntegrationError):
        _plan_retired_generated_seams(target,home,'0.12.0')


@pytest.mark.parametrize('relative', [
    'src/kiro_crew/acp/types.py',
    'src/kiro_crew/dashboard/handlers/agents.py',
])
def test_r72_installed_owners_upgrade_only_known_imports(relative):
    """Exercise exact R72-installed source when the audit fixture is available."""
    installed = os.environ.get('KIROCREW_012_R72_PATCHED')
    if not installed:
        pytest.skip('no isolated R72 patched-host fixture configured')
    source = Path(installed) / relative.removeprefix('src/kiro_crew/')
    before = source.read_text(encoding='utf8')
    report = crew12_patch.inspect(relative, before)
    assert report.state == 'upgradeable', report.detail
    after = crew12_patch.patch_text(relative, before)
    assert after != before
    assert crew12_patch.patch_text(relative, after) == after
    assert crew12_patch.inspect(relative, after).state == 'patched'
    if relative.endswith('/acp/types.py'):
        assert 'from kiro_crew.agent_sdk.backends import' in after
        # Foreign/partial BYO exports must never be treated as owned migrations.
        damaged = before.replace('PROVIDER_LABEL_KIRO_BYO = "kiro_byo"',
                                 'PROVIDER_LABEL_KIRO_BYO = "hijacked"', 1)
        assert damaged != before
        assert crew12_patch.inspect(relative, damaged).state == 'incompatible'
