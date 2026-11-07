"""KiroCrew rc.6 compatibility: refuse lost worker restrictions before launch."""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from kiro_byo.crew_contract_checks import rc6_contract_issues
from kiro_byo.integration_manager import _adapter_matches

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'integration/kiro_crew/v0.12-plus'
OVERLAY = PROFILE / 'overlay/src/kiro_crew/acp/harness/kiro_byo.py'


def _project_helper(monkeypatch, worker_impl):
    """Execute the shipped helper, with a fake host API (no real Crew needed)."""
    source = OVERLAY.read_text(encoding='utf-8')
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == '_project_worker_for_byo')
    package = ModuleType('kiro_crew')
    package.__path__ = []
    module = ModuleType('kiro_crew.mate_worker')
    module.worker_session_spec = worker_impl
    monkeypatch.setitem(sys.modules, 'kiro_crew', package)
    monkeypatch.setitem(sys.modules, 'kiro_crew.mate_worker', module)
    calls = []

    def resolve_prompt(spec, *, agent_id, agents_dir):
        calls.append(('prompt', agent_id, agents_dir))
        return 'resolved prompt'

    def legacy(spec, prompt_dir, session_key):
        calls.append(('legacy', spec, prompt_dir, session_key))
        return {'from': 'legacy'}

    env = {
        'Path': Path,
        'kas_agents': SimpleNamespace(resolve_prompt=resolve_prompt),
        'MATE_WORKER_AGENT': 'kirocrew-mate-worker',
        'mate_worker_projection': legacy,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(OVERLAY), 'exec'), env)
    return env['_project_worker_for_byo'], calls


def test_rc6_worker_projection_requests_host_refusal_in_same_spec_read(monkeypatch, tmp_path):
    calls = []
    def worker(spec, session_key, *, refuse_spec_policy=False):
        calls.append((spec, session_key, refuse_spec_policy))
        return {'from': 'rc6', **spec}
    helper, projected = _project_helper(monkeypatch, worker)
    base = {'prompt': 'file://worker.md', 'allowedTools': ['@safe/tool']}
    result = helper(base, tmp_path, 'worker-session')
    assert result['prompt'] == 'resolved prompt'
    assert base['prompt'] == 'file://worker.md'  # host source not mutated
    assert calls == [({'prompt': 'resolved prompt', 'allowedTools': ['@safe/tool']},
                      'worker-session', True)]
    assert projected == [('prompt', 'kirocrew-mate-worker', tmp_path)]


def test_rc6_policy_failure_is_not_downgraded_to_legacy_projection(monkeypatch, tmp_path):
    class WorkerKitUnreadable(Exception):
        pass
    def worker(spec, session_key, *, refuse_spec_policy=False):
        if refuse_spec_policy:
            raise WorkerKitUnreadable('toolsSettings cannot be applied on BYO')
        return {'unexpected': True}
    helper, calls = _project_helper(monkeypatch, worker)
    with pytest.raises(WorkerKitUnreadable, match='cannot be applied'):
        helper({'prompt': 'base'}, tmp_path, 'session')
    assert len(calls) == 1  # never fall back after explicit refusal


def test_stable_worker_missing_refusal_contract_fails_closed(monkeypatch, tmp_path):
    def obsolete_worker(spec, session_key):
        raise AssertionError('older worker API must not be invoked')
    helper, calls = _project_helper(monkeypatch, obsolete_worker)
    with pytest.raises(RuntimeError, match='policy-refusal contract unavailable'):
        helper({'prompt': 'old'}, tmp_path, 's')
    assert calls == []



def _host_source_fixture(root: Path) -> Path:
    sample = {
        'mate_worker.py': '''\
def resolve_worker_spec(base, member_id, *, refuse_spec_policy=False):
    if refuse_spec_policy:
        raise uncarried(['toolsSettings'])
    return base

def worker_session_spec(base, session_key, *, refuse_spec_policy=False):
    return resolve_worker_spec(base, session_key, refuse_spec_policy=refuse_spec_policy)
''',
        'mate_spec_policy.py': '''\
def applies_spec_policy(backend):
    return backend == ACP_BACKEND_KAS

def layer_policy(target, policy, *, worker=False):
    return None

def policy_of(spec):
    return spec
''',
        'mcp_gateway/backend_handshake.py': '''\
_INITIALIZE_TIMEOUT_COLD_START_FLOOR_SECS = 30.0

def _effective_initialize_timeout_secs(configured):
    return max(float(configured), _INITIALIZE_TIMEOUT_COLD_START_FLOOR_SECS)
''',
        'config/validation.py': '''\
_FAIL_CLOSED_PATHS = frozenset({'agent', 'publish'})

def _apply_field_default(data, dot_path):
    if dot_path in _FAIL_CLOSED_PATHS:
        return False
    return True
''',
    }
    for rel, code in sample.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding='utf-8')
    return root


def test_rc6_contract_passes_known_upstream_shapes(tmp_path):
    assert rc6_contract_issues(_host_source_fixture(tmp_path)) == []


def test_rc6_contract_refuses_missing_or_changed_safety_seams(tmp_path):
    root = _host_source_fixture(tmp_path)
    (root / 'mate_worker.py').write_text(
        (root / 'mate_worker.py').read_text().replace('if refuse_spec_policy:', 'if False:'),
        encoding='utf-8')
    (root / 'mcp_gateway/backend_handshake.py').write_text(
        (root / 'mcp_gateway/backend_handshake.py').read_text().replace('30.0', '10.0'),
        encoding='utf-8')
    (root / 'config/validation.py').write_text(
        (root / 'config/validation.py').read_text().replace("'agent', ", ''),
        encoding='utf-8')
    problems = rc6_contract_issues(root)
    assert any('worker policy is not refused' in issue for issue in problems)
    assert any('30s cold-start floor' in issue for issue in problems)
    assert any('malformed agent' in issue for issue in problems)
    root = _host_source_fixture(tmp_path)
    worker = root / 'mate_worker.py'
    worker.write_text(worker.read_text().replace('refuse_spec_policy=refuse_spec_policy',
                                                 'refuse_spec_policy=False'))
    handshake = root / 'mcp_gateway/backend_handshake.py'
    handshake.write_text(handshake.read_text().replace(
        'return max(float(configured), _INITIALIZE_TIMEOUT_COLD_START_FLOOR_SECS)',
        'return float(configured)'))
    problems = rc6_contract_issues(root)
    assert any('session refusal is not forwarded' in issue for issue in problems)
    assert any('floor not applied' in issue for issue in problems)



def test_rc6_contract_rejects_refusal_only_in_else_branch(tmp_path):
    """A refusal in the opposite branch must not satisfy the safety gate."""
    root = _host_source_fixture(tmp_path)
    worker = root / "mate_worker.py"
    worker.write_text(worker.read_text().replace(
        "if refuse_spec_policy:\n        raise uncarried(['toolsSettings'])",
        "if refuse_spec_policy:\n        pass\n    else:\n        raise uncarried(['toolsSettings'])",
    ), encoding="utf-8")
    issues = rc6_contract_issues(root)
    assert any("worker policy is not refused" in issue for issue in issues)


def test_stable_source_preflight_always_checks_worker_seams(monkeypatch, tmp_path):
    import kiro_byo.integration_manager as manager
    calls = []
    def check(_crew):
        calls.append('worker_policy')
        return ['worker policy not checked']
    monkeypatch.setattr(manager, 'rc6_contract_issues', check)
    crew = tmp_path / 'kiro_crew'
    crew.mkdir()
    _, old_issues, _, _ = _adapter_matches(PROFILE, '0.12.0-rc.10', crew)
    _, stable_issues, _, _ = _adapter_matches(PROFILE, '0.12.0', crew)
    _, next_issues, _, _ = _adapter_matches(PROFILE, '0.12.1', crew)
    assert 'worker policy not checked' in old_issues
    assert 'worker policy not checked' in stable_issues
    assert 'worker policy not checked' in next_issues
    assert calls == ['worker_policy', 'worker_policy', 'worker_policy']
