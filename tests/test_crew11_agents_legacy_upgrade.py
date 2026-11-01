"""Regression for upgrading a prior 0.2.5 BYO agents.py in Crew 0.11 RC1.

The source in these tests is a synthetic Crew semantic fixture.  Actual Windows
Crew disk state is not present; unknown foreign changes stay fail-closed.
"""
from __future__ import annotations

import ast

from kiro_byo import crew11_patch, crew_patch_common as common
from kiro_byo.integration_manager import _converge_semantic_transform
from test_crew_shared_transform import _minimal_agents_source_for_member_backend

PATH = 'src/kiro_crew/dashboard/handlers/agents.py'


def _old_release_patch():
    fresh = crew11_patch.patch_text(PATH, _minimal_agents_source_for_member_backend())
    old = fresh.replace(
        common._KIRO_BYO_EFFECTIVE_MEMBER_BACKEND_HELPER.rstrip('\n'),
        crew11_patch._PREVIOUS_AGENTS_MEMBER_BACKEND_HELPER.rstrip('\n'),
        1,
    )
    assert old != fresh
    return fresh, old


def test_prior_release_agents_is_upgradeable_without_pristine_backup() -> None:
    fresh, old = _old_release_patch()
    assert crew11_patch.inspect(PATH, old).state == 'upgradeable'
    upgraded = _converge_semantic_transform(
        PATH, old, inspect_fn=crew11_patch.inspect,
        transform_fn=crew11_patch.patch_text,
        transform_error=crew11_patch.PatchError,
    )
    assert upgraded == fresh
    assert crew11_patch.inspect(PATH, upgraded).state == 'patched'
    assert crew11_patch.patch_text(PATH, upgraded) == upgraded
    compile(upgraded, PATH, 'exec')


def test_current_and_clean_agents_states_preserved() -> None:
    fresh, _ = _old_release_patch()
    assert crew11_patch.inspect(PATH, fresh).state == 'patched'
    clean = _minimal_agents_source_for_member_backend()
    assert crew11_patch.inspect(PATH, clean).state == 'clean'


def test_partial_agents_only_import_or_helper_rejected() -> None:
    fresh, old = _old_release_patch()
    import_only = _minimal_agents_source_for_member_backend().replace(
        '    ACP_BACKEND_KIRO,\n', '    ACP_BACKEND_KIRO,\n    ACP_BACKEND_KIRO_BYO,\n', 1
    )
    assert crew11_patch.inspect(PATH, import_only).state == 'incompatible'
    no_catalogue = old.replace('async def _kiro_byo_models(', 'async def other_models(', 1)
    assert crew11_patch.inspect(PATH, no_catalogue).state == 'incompatible'


def test_unknown_modified_owned_agents_helper_rejected() -> None:
    _, old = _old_release_patch()
    tampered = old.replace('return getattr(cfg.agent, "member_acp_backend", "")',
                           'return "kas"', 1)
    assert tampered != old
    assert crew11_patch.inspect(PATH, tampered).state == 'incompatible'


def test_old_agents_without_resolved_model_helper_upgrades() -> None:
    _, old = _old_release_patch()
    tree = ast.parse(old)
    resolver = next(node for node in tree.body
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == '_kiro_byo_resolved_model')
    lines = old.splitlines(keepends=True)
    legacy = ''.join(lines[:resolver.lineno-1] + lines[resolver.end_lineno:])
    legacy = legacy.replace(common._KIRO_BYO_RESOLVED_MODEL_REBIND, '', 1)
    assert crew11_patch.inspect(PATH, legacy).state == 'upgradeable'
    upgraded = _converge_semantic_transform(
        PATH, legacy, inspect_fn=crew11_patch.inspect,
        transform_fn=crew11_patch.patch_text,
        transform_error=crew11_patch.PatchError,
    )
    assert crew11_patch.inspect(PATH, upgraded).state == 'patched'
    assert crew11_patch.patch_text(PATH, upgraded) == upgraded
