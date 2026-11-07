"""Regression for the real 0.3.3 Crew Gateway import crash and member loader."""
from __future__ import annotations

import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from kiro_byo import crew12_patch
from kiro_byo.crew_import_closure import byo_import_closure_issues

SOURCE_ZIP = Path(os.environ.get('KIRO_CREW_SOURCE_ZIP', 'KiroCrew-0.12.0-BYO-source.zip'))
PREFIX = 'KiroCrew-0.12.0/'
REGISTRY = 'src/kiro_crew/providers/mirrors/registry.py'
LOADER = 'src/kiro_crew/config/loader_builders.py'


def _sources() -> dict[str, str]:
    if not SOURCE_ZIP.is_file():
        pytest.skip('real Crew 0.12.0 source ZIP not provided')
    with ZipFile(SOURCE_ZIP) as archive:
        return {rel: archive.read(PREFIX + rel).decode('utf-8-sig')
                for rel in crew12_patch.TARGET_FILES}


def _legacy_registry(patched: str) -> str:
    return (patched.replace('from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO\n', '')
            .replace('    ACP_BACKEND_KAS,\n', '    ACP_BACKEND_KAS,\n    ACP_BACKEND_KIRO_BYO,\n', 1))


def _legacy_loader(patched: str) -> str:
    return (patched.replace('    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO\n', '')
            .replace('        ACP_BACKEND_KIRO,\n        backend_from_stored,',
                     '        ACP_BACKEND_KIRO,\n        ACP_BACKEND_KIRO_BYO,\n        backend_from_stored,', 1))


def test_source_archive_entire_generated_set_imports_close() -> None:
    generated = {rel: crew12_patch.patch_text(rel, original)
                 for rel, original in _sources().items()}
    assert byo_import_closure_issues(generated) == []
    assert all(crew12_patch.inspect(rel, generated[rel]).state == 'patched'
               for rel in generated)


@pytest.mark.parametrize('rel,legacy', [(REGISTRY, _legacy_registry), (LOADER, _legacy_loader)])
def test_old_byo_owned_import_can_upgrade_without_facade(rel: str, legacy) -> None:
    before = _sources()[rel]
    old = legacy(crew12_patch.patch_text(rel, before))
    assert crew12_patch.inspect(rel, old).state == 'upgradeable'
    upgraded = crew12_patch.patch_text(rel, old)
    assert crew12_patch.patch_text(rel, upgraded) == upgraded
    assert crew12_patch.inspect(rel, upgraded).state == 'patched'
    assert 'from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO' in upgraded
    if rel == REGISTRY:
        assert '    ACP_BACKEND_KIRO_BYO,\n' not in upgraded.split('from kiro_crew.acp_backends import (', 1)[1].split(')', 1)[0]
    else:
        assert '        ACP_BACKEND_KIRO_BYO,\n' not in upgraded.split('from kiro_crew.acp_backends import (', 1)[1].split(')', 1)[0]


@pytest.mark.parametrize('rel,legacy', [(REGISTRY, _legacy_registry), (LOADER, _legacy_loader)])
def test_unrelated_host_code_is_preserved_by_identity_only_migration(rel: str, legacy) -> None:
    old = legacy(crew12_patch.patch_text(rel, _sources()[rel]))
    old += '\nCREW_FOREIGN_EDIT = True\n'
    patched = crew12_patch.patch_text(rel, old)
    assert patched.endswith('\nCREW_FOREIGN_EDIT = True\n')
    assert crew12_patch.patch_text(rel, patched) == patched


def test_preflight_rejects_original_gateway_breakage() -> None:
    generated = {rel: crew12_patch.patch_text(rel, original)
                 for rel, original in _sources().items()}
    assert byo_import_closure_issues(generated) == []
    generated[REGISTRY] = _legacy_registry(generated[REGISTRY])
    issues = byo_import_closure_issues(generated)
    assert any(REGISTRY in issue and 'kiro_crew.acp_backends' in issue for issue in issues)


def test_preflight_rejects_member_loader_stale_facade() -> None:
    generated = {rel: crew12_patch.patch_text(rel, original)
                 for rel, original in _sources().items()}
    generated[LOADER] = _legacy_loader(generated[LOADER])
    issues = byo_import_closure_issues(generated)
    assert any(LOADER in issue and 'kiro_crew.acp_backends' in issue for issue in issues)


def test_preflight_rejects_missing_canonical_declaration() -> None:
    generated = {rel: crew12_patch.patch_text(rel, original)
                 for rel, original in _sources().items()}
    generated['src/kiro_crew/agent_sdk/backends.py'] = generated[
        'src/kiro_crew/agent_sdk/backends.py'].replace(
        'ACP_BACKEND_KIRO_BYO = "kiro-byo"', 'ACP_BACKEND_MISSING = "kiro-byo"', 1)
    assert any('declaration missing' in issue for issue in byo_import_closure_issues(generated))

@pytest.mark.parametrize('rel,legacy', [(REGISTRY, _legacy_registry), (LOADER, _legacy_loader)])
def test_real_python_import_resolution_would_fail_before_fix(rel: str, legacy) -> None:
    """Execute real import opcodes with a facade intentionally lacking BYO.

    Not an end-to-end Gateway run; this directly reproduces the observed
    ImportError at the Python import boundary with the production module names.
    """
    import ast
    import types

    current = crew12_patch.patch_text(rel, _sources()[rel])
    before = legacy(current)

    def exec_imports(source: str) -> None:
        tree = ast.parse(source)
        if rel == REGISTRY:
            nodes = tree.body
        else:
            owner = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                         and n.name == '_read_backend_choice')
            nodes = owner.body
        relevant = [n for n in nodes if isinstance(n, ast.ImportFrom)
                    and n.module in ('kiro_crew.acp_backends', 'kiro_crew.agent_sdk.backends')]
        assert relevant
        facade = types.ModuleType('kiro_crew.acp_backends')
        owner_module = types.ModuleType('kiro_crew.agent_sdk.backends')
        owner_module.ACP_BACKEND_KIRO_BYO = 'kiro-byo'
        for node in relevant:
            for name in node.names:
                if name.name != 'ACP_BACKEND_KIRO_BYO':
                    setattr(facade, name.name, name.name)
        mapping = {'kiro_crew.acp_backends': facade,
                   'kiro_crew.agent_sdk.backends': owner_module}

        def importer(module_name: str, *_args, **_kwargs):
            return mapping[module_name]

        compiled = compile(ast.fix_missing_locations(ast.Module(
            body=relevant, type_ignores=[])), '<import-closure>', 'exec')
        exec(compiled, {'__builtins__': {'__import__': importer}}, {})

    with pytest.raises(ImportError, match='ACP_BACKEND_KIRO_BYO'):
        exec_imports(before)
    exec_imports(current)


def test_preflight_rejects_composed_endpoint_global_resolution_regression() -> None:
    generated = {rel: crew12_patch.patch_text(rel, original)
                 for rel, original in _sources().items()}
    path = "src/kiro_crew/dashboard/chat_api/slot_effort.py"
    text = generated[path]
    line = "    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO\n"
    assert line in text
    generated[path] = text.replace(line, "", 1)
    issues = byo_import_closure_issues(generated)
    assert any(path in issue and "facade rebinding" in issue for issue in issues)


def test_preflight_rejects_composed_helper_missing_local_import() -> None:
    generated = {rel: crew12_patch.patch_text(rel, original)
                 for rel, original in _sources().items()}
    path = "src/kiro_crew/dashboard/chat_api/slot_effort.py"
    source = generated[path]
    helper = "_kiro_byo_cold_selection_capabilities"
    marker = f"async def {helper}("
    before, after = source.split(marker, 1)
    needle = "    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO\n"
    assert needle in after
    generated[path] = before + marker + after.replace(needle, "", 1)
    issues = byo_import_closure_issues(generated)
    assert any(helper in issue and "facade rebinding" in issue for issue in issues)
