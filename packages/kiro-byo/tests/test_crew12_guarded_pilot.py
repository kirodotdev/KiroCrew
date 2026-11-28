"""0.12 pilot must be separate from the working 0.11 adapter.

The source excerpts here are contract fixtures, not a substitute for running
against the full, installed upstream 0.12.0-rc.3 package.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_byo import crew12_patch
from kiro_byo.integration_manager import (
    IntegrationError, _crew12_version_candidate, _adapter_matches, _transformer_for,
    resolve_target,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'integration/kiro_crew/v0.12-plus'
LOAD = 'src/kiro_crew/acp/runtime_session_load.py'

# Extracted relevant import/control shape from official 0.12 rc.3; this is a
# deliberate minimal regression fixture and not the upstream source archive.
OFFICIAL_RC3_LOAD_SHAPE = '''from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    METHOD_SESSION_LOAD,
    JsonRpcMessage,
)

async def load_session(self, session_file, resume_sid):
    kas_agents = None
    if self._acp_backend == ACP_BACKEND_KAS:
        kas_extras = await self._kas_custom_agents(active_agent)
        kas_agents = kas_extras.custom_agents
    self._harness.record_session_projection(handle, kas_agents, active_agent)
    try:
        await handle.apply_session_permission_routing()
    except Exception:
        await self.terminate_session(resume_sid)
        raise
    self._start_mount_check(handle, wire_servers, resume_sid, session_key, request_sent_at)
    return kas_agents
'''


@pytest.mark.parametrize('version', [
    '0.12.0', '0.12.0+local', '0.12.0-rc.1', '0.12.0-rc.6', '0.12.0-rc.10', '0.12.0rc10', '0.12.0-beta.1', '0.12.1', '0.12.1-rc.1',
    '0.13.0-rc.1', '0.13.0', '0.13.1-rc.1', '1.0.0',
])
def test_012_plus_version_is_candidate(version: str) -> None:
    assert _crew12_version_candidate(version)


@pytest.mark.parametrize('version', [
    '0.10.0', '0.11.0', '0.11.1-rc.2', '0.11.9', 'unknown',
    '0.12.0-dev', '', '0.12',
    '0.12.0garbage', '0.12.0-!', '0.12.0xrc.4',
    '0.12.1-feature', '0.13.0-beta',
])
def test_legacy_and_invalid_versions_are_rejected(version: str) -> None:
    assert not _crew12_version_candidate(version)


def test_012_source_guard_is_not_version_pinned() -> None:
    c = json.loads((PROFILE / 'compatibility.json').read_text())
    assert c['transformer'] == 'adaptive12'
    assert 'allowed_crew_versions' not in c
    assert not c.get('max_version_exclusive')
    assert c['min_version'] == '0.12.0'
    assert c['verified_versions'] == [] and not c['verified_through']
    assert c['semantic_unpinned_files'] == c['generated_files']
    assert set(c['generated_files']) == set(json.loads((PROFILE / 'manifest.json').read_text())['files']) - set(c['created_files'])


def test_012_new_load_import_is_patched_then_idempotent() -> None:
    initial = crew12_patch.inspect(LOAD, OFFICIAL_RC3_LOAD_SHAPE)
    assert initial.state == 'clean', initial
    patched = crew12_patch.patch_text(LOAD, OFFICIAL_RC3_LOAD_SHAPE)
    assert 'self._acp_backend in {ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO}' in patched
    assert 'from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO' in patched
    assert patched == crew12_patch.patch_text(LOAD, patched)
    assert crew12_patch.inspect(LOAD, patched).state == 'patched'


def test_012_incompatible_load_never_treated_as_clean() -> None:
    raw = OFFICIAL_RC3_LOAD_SHAPE.replace('self._acp_backend == ACP_BACKEND_KAS', 'self._acp_backend in available_harnesses')
    assert crew12_patch.inspect(LOAD, raw).state == 'incompatible'
    raw = OFFICIAL_RC3_LOAD_SHAPE.replace('    if self._acp_backend == ACP_BACKEND_KAS:', '    # kiro_byo partial\n    if self._acp_backend == ACP_BACKEND_KAS:')
    assert crew12_patch.inspect(LOAD, raw).state == 'incompatible'


def test_012_pilot_does_not_use_011_transformer() -> None:
    assert _transformer_for({'transformer':'adaptive12'})[2] is crew12_patch.inspect
    with pytest.raises(IntegrationError, match="unsupported"):
        _transformer_for({'transformer':'adaptive11'})


def test_012_missing_core_never_installs(tmp_path: Path) -> None:
    crew = tmp_path / 'kiro_crew'
    crew.mkdir()
    ok, reasons, _, _ = _adapter_matches(PROFILE, '0.12.0', crew)
    assert not ok and reasons
    assert any('missing overlay dependency' in reason for reason in reasons)
    for v in ['0.12.1', '0.13.0']:
        ok, reasons, _, _ = _adapter_matches(PROFILE, v, crew)
        assert not ok and any('missing overlay dependency' in issue for issue in reasons)
    ok, reasons, _, _ = _adapter_matches(PROFILE, '0.11.1', crew)
    assert not ok and any('floor' in issue for issue in reasons)


def test_rc_preflight_inspects_source_and_refuses_missing_host(tmp_path: Path) -> None:
    crew = tmp_path / 'kiro_crew'
    crew.mkdir()
    for version in ('0.12.0-rc.1', '0.12.0-rc.6', '0.12.0-rc.10'):
        ok, reasons, _, _ = _adapter_matches(PROFILE, version, crew)
        assert not ok
        assert any('missing overlay dependency' in item for item in reasons)


def test_012_source_guarded_discovery_checks_real_installed_tree(tmp_path: Path) -> None:
    crew = tmp_path / 'kiro_crew'
    crew.mkdir()
    with patch('kiro_byo.integration_manager.running_crew', return_value=('0.12.0', crew, tmp_path)):
        with pytest.raises(IntegrationError, match='does not match the bundled'):
            resolve_target(ROOT)


def test_011_known_versions_are_rejected(tmp_path: Path) -> None:
    crew = tmp_path / 'kiro_crew'
    crew.mkdir()
    for version in ['0.11.0', '0.11.1']:
        with patch('kiro_byo.integration_manager.running_crew', return_value=(version, crew, tmp_path)):
            with pytest.raises(IntegrationError) as error:
                resolve_target(ROOT)
            assert "not supported" in str(error.value)


def test_rc3_session_load_refuses_missing_permission_or_resume_mcp_edges():
    # These edges are independently present in the official rc.3 load_session.
    # Protecting only the old KAS custom-agent branch would silently accept a
    # changed resume or permission path after an upstream refactor.
    for required in (
        "await handle.apply_session_permission_routing()",
        "self._harness.record_session_projection(handle, kas_agents, active_agent)",
        "await self.terminate_session(resume_sid)",
        "self._start_mount_check(handle, wire_servers, resume_sid, session_key, request_sent_at)",
    ):
        altered = OFFICIAL_RC3_LOAD_SHAPE.replace(required, "pass")
        result = crew12_patch.inspect(LOAD, altered)
        assert result.state == "incompatible", (required, result)
