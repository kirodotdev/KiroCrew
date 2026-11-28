"""Fail closed if an installed BYO member guard has been partially replaced.

This protects the RC1 Mate/member backend boundary without touching any
Mode/Type or provider-routing semantics.
"""
from __future__ import annotations

import pytest

from kiro_byo import crew_current_patch
from kiro_byo.crew_patch_common import PatchError
from test_crew11_no_official_member_fallback import member_source, MEMBERS, LOADER
from test_crew11_mate_byo_linkage import _patched_loader_source


# NOTE: member_sessions.py 的补丁已在 R-slim 退役（见
# test_crew11_no_official_member_fallback.test_member_sessions_is_retired...）。
# 原先验证该补丁 marker 幂等 / 重复 marker 失败的两个测试随之移除；
# 成员后端选择的真实逻辑现由下面的 LOADER（loader_builders）测试守护。


def test_complete_installed_loader_rule_is_idempotent():
    patched = _patched_loader_source()
    assert crew_current_patch.patch_text(LOADER, patched) == patched
    assert crew_current_patch.inspect(LOADER, patched).state == 'patched'


@pytest.mark.parametrize('old,new', [
    ('value = ACP_BACKEND_KIRO_BYO', 'value = ACP_BACKEND_KAS'),
    ('agent_data.get("acp_backend")', 'agent_data.get("member_acp_backend")'),
])
def test_partial_loader_guard_fails_closed(old, new):
    patched = _patched_loader_source()
    assert old in patched
    damaged = patched.replace(old, new, 1)
    with pytest.raises(PatchError, match='global-follow loader rule incomplete'):
        crew_current_patch.patch_text(LOADER, damaged)
    assert crew_current_patch.inspect(LOADER, damaged).state == 'incompatible'
