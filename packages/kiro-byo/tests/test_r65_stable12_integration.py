"""Crew 0.12 compatibility floor and semantic model-discovery regressions.

Synthetic fixtures cannot establish real KiroCrew 0.12.0 Host compatibility.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kiro_byo.crew_patch_common import PatchError
from kiro_byo import crew_current_patch
from kiro_byo.integration_manager import _crew12_version_candidate
from _crew_semantic_fixtures import _minimal_agents_source_for_member_backend

PATH = 'src/kiro_crew/dashboard/handlers/agents.py'


def test_012_floor_accepts_rc_and_stable_candidates():
    assert _crew12_version_candidate('0.12.0')
    assert _crew12_version_candidate('0.12.1')
    assert _crew12_version_candidate('0.13.1-rc.1')
    assert all(_crew12_version_candidate(f'0.12.0-rc.{i}') for i in (1, 6, 10, 99))
    assert not _crew12_version_candidate('0.11.99')


def test_discovery_route_without_old_advertised_list_branch():
    original = _minimal_agents_source_for_member_backend()
    original = original.replace(
        'if capabilities_for(backend).resolves_model_from_advertised_list:',
        'if backend == "kas":',
    )
    patched = crew_current_patch.patch_text(PATH, original)
    assert patched.count('if backend == ACP_BACKEND_KIRO_BYO:') == 1
    assert patched == crew_current_patch.patch_text(PATH, patched)
    assert crew_current_patch.inspect(PATH, patched).state == 'patched'
    ast.parse(patched)


def test_discovery_fails_closed_when_cfg_is_not_bound_at_top_level():
    original = _minimal_agents_source_for_member_backend().replace(
        '    cfg = object()\n',
        '    if backend == "kas":\n        cfg = object()\n',
    )
    with pytest.raises(PatchError, match='backend/config bindings moved'):
        crew_current_patch.patch_text(PATH, original)


def test_stable_floor_declared_consistently():
    root = Path(__file__).resolve().parents[1]
    import json
    assert json.loads((root / 'app.json').read_text())['minKiroCrewVersion'] == '0.12.0'
    assert json.loads((root / 'integration/kiro_crew/v0.12-plus/compatibility.json').read_text())['min_version'] == '0.12.0'
    assert 'v0.12.0]' in (root / '.github/workflows/ci.yml').read_text()
