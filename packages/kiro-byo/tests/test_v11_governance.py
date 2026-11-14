"""V11: public diagnostics and Provider slow-discovery transaction boundaries."""
from __future__ import annotations

import json
import threading
import time

from kiro_byo import provider_admin as admin
from kiro_byo.credential_store import MemoryCredentialStore
from kiro_byo.discovery import DiscoveredModel
from kiro_byo.durable_runs import DurableRunStore


def test_public_durable_run_is_allowlisted_and_does_not_mutate_ledger(tmp_path):
    store = DurableRunStore(tmp_path / 'runs')
    run = store.create_run('session', lease_owner='SECRET_LEASE', metadata={
        'model_id': 'model', 'api_key': 'SECRET_META', 'user_event_seq': 9,
    })
    step = store.start_step(run['run_id'], kind='tool', objective='SECRET_OBJECTIVE', owner_token='SECRET_LEASE')
    attempt = store.start_attempt(run['run_id'], step['step_id'], owner_token='SECRET_LEASE')
    raw = store.get(run['run_id'])
    public = store.public_record(raw)
    payload = json.dumps(public)
    assert all(secret not in payload for secret in ('SECRET_LEASE', 'SECRET_META', 'SECRET_OBJECTIVE'))
    assert not {'steps', 'checkpoints', 'tool_batches', 'owner_token'}.intersection(public)
    assert public['metadata'] == {'model_id': 'model'}
    assert public['run_id'] == run['run_id']
    assert public['lease']['active'] is True
    # Only output changes; persisted recovery record must retain its original data.
    assert store.get(run['run_id'])['metadata']['user_event_seq'] == 9


def test_configure_provider_discovery_does_not_hold_config_transaction(tmp_path, monkeypatch):
    target = tmp_path / 'providers.json'
    monkeypatch.setattr(admin, 'config_path', lambda: target)
    entered, release = threading.Event(), threading.Event()
    discovered = ('custom', 'Custom', [DiscoveredModel('test-model', 'Test')])
    errors = []

    def slow_discovery(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return discovered

    monkeypatch.setattr(admin, '_discover_for_preset', slow_discovery)
    def work():
        try:
            admin.configure_provider(base_url='https://api.example.test/v1', api_key='key',
                                     preset_id='custom', store=MemoryCredentialStore())
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert entered.wait(2)
        start = time.monotonic()
        admin.set_mode_selection('sky', 'tiga')
        assert time.monotonic() - start < 0.6, 'slow provider discovery blocked an unrelated mode change'
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive() and not errors, errors
    raw = json.loads(target.read_text('utf-8'))
    assert raw['mode']['type'] == 'sky'
    assert len(raw['providers']) == 1


def test_refresh_rejects_provider_modified_during_discovery(tmp_path, monkeypatch):
    target = tmp_path / 'providers.json'
    monkeypatch.setattr(admin, 'config_path', lambda: target)
    store = MemoryCredentialStore()
    first = ('custom', 'Custom', [DiscoveredModel('old', 'Old')])
    monkeypatch.setattr(admin, '_discover_for_preset', lambda *a, **k: first)
    provider = admin.configure_provider(base_url='https://old.example.test/v1', api_key='key',
                                        preset_id='custom', store=store)
    entered, release = threading.Event(), threading.Event()
    errors = []

    def slow_discovery(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return ('custom', 'Custom', [DiscoveredModel('new', 'New')])
    monkeypatch.setattr(admin, '_discover_for_preset', slow_discovery)
    def work():
        try:
            admin.refresh_provider_models(provider['id'], store=store)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert entered.wait(2)
        @admin._serialized_config_mutation
        def change_connection():
            raw = admin._read_raw()
            raw['providers'][0]['base_url'] = 'https://new.example.test/v1'
            admin._write_raw(raw)
        change_connection()
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], admin.ConfigError)
    raw = json.loads(target.read_text('utf-8'))
    assert raw['providers'][0]['base_url'] == 'https://new.example.test/v1'
    assert raw['providers'][0]['models'][0]['id'] == 'old'


def test_refresh_rejects_changed_credential_without_reference_change(tmp_path, monkeypatch):
    target = tmp_path / 'providers.json'
    monkeypatch.setattr(admin, 'config_path', lambda: target)
    store = MemoryCredentialStore()
    monkeypatch.setattr(admin, '_discover_for_preset',
                        lambda *a, **k: ('custom', 'Custom', [DiscoveredModel('old', 'Old')]))
    provider = admin.configure_provider(base_url='https://key.example.test/v1', api_key='old-key',
                                        preset_id='custom', store=store)
    entered, release = threading.Event(), threading.Event()
    errors = []

    def slow_discovery(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return ('custom', 'Custom', [DiscoveredModel('new', 'New')])

    monkeypatch.setattr(admin, '_discover_for_preset', slow_discovery)
    def worker():
        try:
            admin.refresh_provider_models(provider['id'], store=store)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(2)
        admin.update_provider_credential(provider['id'], 'new-key', store=store)
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], admin.ConfigError)
    assert 'old-key' not in str(errors[0]) and 'new-key' not in str(errors[0])
    raw = json.loads(target.read_text('utf-8'))
    assert raw['providers'][0]['models'][0]['id'] == 'old'
