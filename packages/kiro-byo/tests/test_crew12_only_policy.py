"""One guarded semantic Crew 0.12+ adapter, no legacy adapters."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_byo.integration_manager import IntegrationError, resolve_target, _transformer_for, _adapter_matches

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "integration/kiro_crew/v0.12-plus"


def test_only_one_install_adapter_is_distributed():
    assert {p.name for p in (ROOT / "integration/kiro_crew").iterdir() if p.is_dir() and p.name.startswith('v0.')} == {'v0.12-plus'}
    assert json.loads((ROOT / 'app.json').read_text())['minKiroCrewVersion'] == '0.12.0'
    assert json.loads((PROFILE / 'manifest.json').read_text())['baseline'] == 'v0.12+'


@pytest.mark.parametrize('version', ['0.10.0','0.11.0','0.11.1','0.11.1-rc.2','unknown'])
def test_retired_and_unknown_versions_are_refused_before_any_read_of_crew_files(version, tmp_path):
    missing_package = tmp_path / 'nonexistent'
    with patch('kiro_byo.integration_manager.running_crew', return_value=(version,missing_package,tmp_path)):
        with pytest.raises(IntegrationError,match='not supported'):
            resolve_target(ROOT)
    assert not missing_package.exists()


def test_rc_version_reaches_source_preflight(tmp_path):
    missing = tmp_path / "missing"
    with patch("kiro_byo.integration_manager.running_crew",
               return_value=("0.12.0-rc.10", missing, tmp_path)):
        with pytest.raises(IntegrationError) as raised:
            resolve_target(ROOT)
    message = str(raised.value)
    assert "does not match the bundled" in message
    assert "missing overlay dependency" in message


@pytest.mark.parametrize('old', ['adaptive10','adaptive11','adaptive09',''])
def test_retired_transformer_names_refused(old):
    with pytest.raises(IntegrationError, match='unsupported'):
        _transformer_for({'transformer': old})


def test_012_plus_is_refused_when_upstream_sources_absent(tmp_path):
    crew = tmp_path/'kiro_crew'; crew.mkdir()
    for version in ('0.12.0', '0.12.1', '1.0.0'):
        ok,issues,_,_ = _adapter_matches(PROFILE,version,crew)
        assert not ok and any('missing overlay dependency' in s for s in issues)


def test_legacy_bootstrap_repair_is_inert():
    from kiro_byo import bootstrap_patch
    assert bootstrap_patch.repair() == 0
    content=(ROOT/'src/kiro_byo/bootstrap_patch.py').read_text()
    assert 'crew10_patch' not in content and 'adaptive10' not in content
    assert 'repair()\n    _record_activation()' not in content


def test_package_has_no_legacy_install_verifiers():
    assert not (ROOT/'scripts/verify_crew10_integration.py').exists()
    assert not (ROOT/'scripts/verify_crew11_integration.py').exists()
    assert (ROOT/'scripts/verify_crew12_integration.py').is_file()


def test_rc3_contract_inventory_does_not_self_refuse():
    from kiro_byo.crew_contract_checks import compatibility_contract_issues
    c = json.loads((PROFILE / 'compatibility.json').read_text(encoding='utf-8'))
    assert not (set(c['required_contract_symbols']) & set(c['required_contract_markers']))
    missing = compatibility_contract_issues(c, ROOT / 'missing_crew_package')
    assert missing and all('duplicate overlay contract metadata' not in item for item in missing)


def test_active_012_adapter_does_not_import_retired_crew_transformers():
    """The stable adapter uses current sources directly, never retired proxies."""
    import ast
    from kiro_byo import crew12_patch, crew_current_patch, crew_slot_patch

    assert crew12_patch.TARGET_FILES == crew_current_patch.TARGET_FILES
    for retired in ("crew10_patch", "crew11_patch"):
        assert not (ROOT / "src/kiro_byo" / f"{retired}.py").exists()
    for name in ("crew12_patch", "crew_current_patch", "crew_slot_patch"):
        source = (ROOT / "src/kiro_byo" / f"{name}.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                for entry in node.names:
                    assert entry.name.split(".")[-1] not in {"crew10_patch", "crew11_patch"}
    assert crew_current_patch.slot_patch._patch_slot_effort is crew_slot_patch._patch_slot_effort
    assert crew_current_patch.slot_patch._patch_chat_title is crew_slot_patch._patch_chat_title
