"""Fail closed if an installed BYO member guard has been partially replaced.

This protects the RC1 Mate/member backend boundary without touching any
Mode/Type or provider-routing semantics.
"""
from __future__ import annotations

import pytest

from kiro_byo import crew11_patch
from kiro_byo.crew_patch_common import PatchError
from test_crew11_no_official_member_fallback import member_source, MEMBERS, LOADER
from test_crew11_mate_byo_linkage import _patched_loader_source


def test_complete_installed_member_guard_is_idempotent():
    patched = crew11_patch.patch_text(MEMBERS, member_source())
    assert crew11_patch.patch_text(MEMBERS, patched) == patched
    assert crew11_patch.inspect(MEMBERS, patched).state == 'patched'


@pytest.mark.parametrize('old,new', [
    ('return "kiro-byo"', 'return "kas"'),
    ('!= "kiro-byo":', '!= "kas":'),
    ('_byo_resolve_member_backend("kiro-byo")', '_byo_resolve_member_backend("kas")'),
])
def test_partial_member_guard_fails_closed(old, new):
    patched = crew11_patch.patch_text(MEMBERS, member_source())
    assert old in patched
    damaged = patched.replace(old, new, 1)
    with pytest.raises(PatchError, match='member dispatch guard is incomplete'):
        crew11_patch.patch_text(MEMBERS, damaged)
    assert crew11_patch.inspect(MEMBERS, damaged).state == 'incompatible'


def test_duplicate_member_marker_fails_closed():
    patched = crew11_patch.patch_text(MEMBERS, member_source())
    marker = '# Kiro BYO: enforce BYO for new member sessions (no official fallback)'
    damaged = patched + '\n# '+marker+'\n'
    with pytest.raises(PatchError, match='member dispatch guard is incomplete'):
        crew11_patch.patch_text(MEMBERS, damaged)


def test_complete_installed_loader_guard_is_idempotent():
    patched = _patched_loader_source()
    assert crew11_patch.patch_text(LOADER, patched) == patched
    assert crew11_patch.inspect(LOADER, patched).state == 'patched'


@pytest.mark.parametrize('old,new', [
    ('value = ACP_BACKEND_KIRO_BYO', 'value = ACP_BACKEND_KAS'),
    ('agent_data.get("acp_backend")', 'agent_data.get("member_acp_backend")'),
])
def test_partial_loader_guard_fails_closed(old, new):
    patched = _patched_loader_source()
    assert old in patched
    damaged = patched.replace(old, new, 1)
    with pytest.raises(PatchError, match='member default guard is incomplete'):
        crew11_patch.patch_text(LOADER, damaged)
    assert crew11_patch.inspect(LOADER, damaged).state == 'incompatible'
