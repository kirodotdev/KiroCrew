"""Regression: RC1 older BYO member capabilities upgrade to Mate 2.0 safely."""
from __future__ import annotations

from kiro_byo import crew11_patch
from kiro_byo.integration_manager import _converge_semantic_transform


PATH = 'src/kiro_crew/agent_sdk/backends_members.py'
OLDER = '''from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO_BYO,
)
ACP_BACKENDS_MEMBER_DISPATCH = frozenset({ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO})
ACP_BACKENDS_MEMBER_PANEL = frozenset({ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO})
ACP_BACKENDS_MATE_WORKER_KIT = frozenset({ACP_BACKEND_KAS})
'''


def test_older_byo_member_capability_is_upgradeable_and_converges() -> None:
    assert crew11_patch.inspect(PATH, OLDER).state == 'upgradeable'
    upgraded = _converge_semantic_transform(
        PATH, OLDER, inspect_fn=crew11_patch.inspect,
        transform_fn=crew11_patch.patch_text,
        transform_error=crew11_patch.PatchError,
    )
    assert upgraded.count('ACP_BACKEND_KIRO_BYO') == OLDER.count('ACP_BACKEND_KIRO_BYO') + 1
    assert 'ACP_BACKENDS_MATE_WORKER_KIT = frozenset({ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO})' in upgraded
    assert crew11_patch.inspect(PATH, upgraded).state == 'patched'
    assert crew11_patch.patch_text(PATH, upgraded) == upgraded
    compile(upgraded, PATH, 'exec')


def test_partial_member_capability_with_missing_owner_marker_is_not_upgradeable() -> None:
    partial = OLDER.replace('ACP_BACKENDS_MEMBER_PANEL = frozenset({ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO})',
                            'ACP_BACKENDS_MEMBER_PANEL = frozenset({ACP_BACKEND_KAS})')
    assert crew11_patch.inspect(PATH, partial).state == 'incompatible'


def test_fake_owned_marker_in_string_is_not_upgradeable() -> None:
    fake = OLDER.replace('    ACP_BACKEND_KIRO_BYO,\n', '')
    fake = 'NOTE = "ACP_BACKEND_KIRO_BYO"\n' + fake
    assert crew11_patch.inspect(PATH, fake).state == 'incompatible'


def test_unknown_member_additions_are_not_assumed_owned() -> None:
    # If a file contains only a partly written BYO member integration, the
    # installer must not upgrade it without known ownership proof.
    partial = OLDER.replace('ACP_BACKENDS_MEMBER_DISPATCH = frozenset({ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO})',
                            'ACP_BACKENDS_MEMBER_DISPATCH = frozenset({ACP_BACKEND_KAS})')
    assert crew11_patch.inspect(PATH, partial).state == 'incompatible'


def test_clean_member_owner_source_is_still_clean() -> None:
    clean = OLDER.replace('    ACP_BACKEND_KIRO_BYO,\n', '').replace(', ACP_BACKEND_KIRO_BYO', '')
    assert crew11_patch.inspect(PATH, clean).state == 'clean'
