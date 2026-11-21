"""Stage 7: process-independent lease fencing and conservative recovery."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_byo.durable_runs import DurableRunStore


def test_active_lease_fences_all_mutations_and_keeps_revision(tmp_path: Path):
    store = DurableRunStore(tmp_path / 'runs')
    run = store.create_run('s', lease_owner='worker-a', lease_ttl=90)
    rid = run['run_id']
    assert run['lease']['generation'] == 1
    with pytest.raises(ValueError, match='lease'):
        store.start_step(rid, kind='turn')
    with pytest.raises(ValueError, match='lease'):
        store.checkpoint(rid, name='unsafe', data={'side_effect_risk': True})
    assert store.get(rid)['revision'] == 0
    step = store.start_step(rid, kind='turn', owner_token='worker-a')
    assert step
    attempt = store.start_attempt(rid, step['step_id'], owner_token='worker-a')
    assert attempt
    store.finish_attempt(rid, step['step_id'], attempt['attempt_id'], status='completed', owner_token='worker-a')
    store.finish_step(rid, step['step_id'], status='completed', owner_token='worker-a')
    store.finish_run(rid, status='completed', owner_token='worker-a')
    assert store.get(rid)['status'] == 'completed'


def test_active_other_process_not_reconciled_or_reopened(tmp_path: Path):
    store_a = DurableRunStore(tmp_path / 'runs')
    store_b = DurableRunStore(tmp_path / 'runs')
    run = store_a.create_run('s', lease_owner='a', lease_ttl=90, metadata={'user_event_seq': 1})
    rid = run['run_id']
    assert store_b.is_lease_active(store_b.get(rid))
    with pytest.raises(ValueError, match='lease'):
        store_b.mark_recovery(rid, 'pending_restart')
    with pytest.raises(ValueError, match='lease'):
        store_b.prepare_resume(rid, session_id='s', owner_token='b')
    assert store_b.get(rid)['revision'] == 0
    assert store_b.get(rid)['recovery_state'] == 'clean'


def test_expired_lease_new_generation_fences_old_owner(tmp_path: Path, monkeypatch):
    from kiro_byo import durable_runs as mod
    clock = [1000.0]
    monkeypatch.setattr(mod, '_now', lambda: clock[0])
    store = DurableRunStore(tmp_path / 'runs')
    run = store.create_run('s', lease_owner='old', lease_ttl=30, metadata={'user_event_seq': 2})
    rid = run['run_id']
    step = store.start_step(rid, kind='turn', owner_token='old')
    attempt = store.start_attempt(rid, step['step_id'], owner_token='old')
    clock[0] += 31
    with pytest.raises(ValueError, match='lease'):
        store.finish_attempt(rid, step['step_id'], attempt['attempt_id'], status='completed', owner_token='old')
    store.mark_recovery(rid, 'pending_restart')
    recovered = store.prepare_resume(rid, session_id='s', strategy='resume', owner_token='new', lease_ttl=30)
    assert recovered['lease']['generation'] == 2
    with pytest.raises(ValueError, match='lease'):
        store.checkpoint(rid, name='stale', owner_token='old')
    with pytest.raises(ValueError, match='lease'):
        store.finish_run(rid, status='completed', owner_token='old')
    assert store.get(rid)['steps'][0]['status'] == 'interrupted'
    assert store.get(rid)['steps'][0]['attempts'][0]['status'] == 'interrupted'
    new_step = store.start_step(rid, kind='resume', owner_token='new')
    assert new_step and new_step['step_id'] != step['step_id']
    refreshed = store.renew_lease(rid, owner_token='new', lease_ttl=30)
    assert refreshed['lease']['expires_at'] == clock[0] + 30


def test_side_effect_risk_blocks_takeover_even_after_expiry(tmp_path: Path, monkeypatch):
    from kiro_byo import durable_runs as mod
    clock = [50.0]
    monkeypatch.setattr(mod, '_now', lambda: clock[0])
    store = DurableRunStore(tmp_path / 'runs')
    run = store.create_run('s', lease_owner='old', lease_ttl=10, metadata={'user_event_seq': 1})
    store.checkpoint(run['run_id'], name='tool_start', owner_token='old', data={'side_effect_risk': True})
    clock[0] += 11
    store.mark_recovery(run['run_id'], 'unknown_side_effect')
    with pytest.raises(ValueError, match='side.effect'):
        store.prepare_resume(run['run_id'], session_id='s', owner_token='new')
    assert store.get(run['run_id'])['lease']['generation'] == 1


def test_cross_process_stale_token_rejected(tmp_path: Path):
    store = DurableRunStore(tmp_path / 'runs')
    run = store.create_run('s', lease_owner='first', lease_ttl=90)
    script = '''from pathlib import Path
from kiro_byo.durable_runs import DurableRunStore
import sys
s=DurableRunStore(Path(sys.argv[1]))
try:
 s.start_step(sys.argv[2],kind="turn",owner_token="second")
except ValueError as ex:
 print(type(ex).__name__)
 sys.exit(0)
sys.exit(4)
'''
    env = dict(os.environ)
    project_src = str(Path(__file__).resolve().parents[1] / 'src')
    env['PYTHONPATH'] = project_src + os.pathsep + env.get('PYTHONPATH', '')
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'runs'), run['run_id']], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert store.get(run['run_id'])['revision'] == 0


def test_legacy_unleased_runs_retain_existing_api(tmp_path: Path):
    store = DurableRunStore(tmp_path / 'runs')
    run = store.create_run('s')
    step = store.start_step(run['run_id'], kind='turn')
    assert step and not store.get(run['run_id']).get('lease')


def test_run_lease_heartbeat_and_stop(tmp_path: Path, monkeypatch):
    import time
    from types import SimpleNamespace
    from kiro_byo import turn_runner
    from kiro_byo.turn_runner import TurnRunnerMixin

    class Harness(TurnRunnerMixin):
        runtime_trace = None
        def trace(self, msg):
            pass

    monkeypatch.setattr(turn_runner, 'RUN_LEASE_HEARTBEAT_SECONDS', 0.015)
    h = Harness()
    h._kiro_durable_run_store = DurableRunStore(tmp_path / 'runs')
    run = h._kiro_durable_run_store.create_run('s', lease_owner='worker', lease_ttl=90)
    session = SimpleNamespace()
    h._start_run_lease_heartbeat(session, run['run_id'], 'worker')
    before = h._kiro_durable_run_store.get(run['run_id'])['revision']
    deadline = time.monotonic() + 2.0
    while h._kiro_durable_run_store.get(run['run_id'])['revision'] == before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert h._kiro_durable_run_store.get(run['run_id'])['revision'] > before
    h._stop_run_lease_heartbeat(session)
    assert session.active_run_lease_thread is None


def test_acp_diagnostics_redact_owner_token(tmp_path: Path):
    from io import StringIO
    from kiro_byo.acp_server import AcpServer
    server = AcpServer(stdin=StringIO(), stdout=StringIO())
    run = DurableRunStore(tmp_path / 'runs').create_run('s', lease_owner='TOP-SECRET', lease_ttl=120)
    payload = server._run_recovery_payload(run)
    assert 'TOP-SECRET' not in str(payload)
    assert payload['lease']['active'] is True
    server._executor.shutdown(wait=True)


def test_lease_token_never_in_public_record(tmp_path: Path):
    store = DurableRunStore(tmp_path / 'runs')
    run = store.create_run('s', lease_owner='private-owner-token')
    public = store.public_record(store.get(run['run_id']))
    assert 'private-owner-token' not in str(public)
    assert public['lease']['generation'] == 1
    assert public['lease']['active'] is True


def test_two_recovery_claims_admit_only_one(tmp_path: Path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from kiro_byo import durable_runs as mod
    clock = [200.0]
    monkeypatch.setattr(mod, '_now', lambda: clock[0])
    store = DurableRunStore(tmp_path / 'runs')
    run = store.create_run('s', lease_owner='original', lease_ttl=10, metadata={'user_event_seq': 0})
    clock[0] += 11
    store.mark_recovery(run['run_id'], 'pending_restart')
    def claim(token):
        try:
            return store.prepare_resume(run['run_id'], session_id='s', owner_token=token)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ['claim-a', 'claim-b']))
    assert len([r for r in results if r is not None]) == 1
    assert store.get(run['run_id'])['lease']['generation'] == 2

