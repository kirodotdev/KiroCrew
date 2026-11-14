"""Windows sharing-violation regressions for Mode/provider config writes.

Mocked WinErrors are deterministic fault injection, not Windows E2E.
"""
from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from kiro_byo import provider_admin
from kiro_byo.paths import use_app_home


def _sharing_violation() -> PermissionError:
    exc = PermissionError(errno.EACCES, "simulated Windows sharing violation")
    exc.winerror = 5
    return exc


def test_windows_temporarily_locked_destination_retries_without_losing_data(tmp_path, monkeypatch):
    path = tmp_path / 'providers.json'
    path.write_text('{"mode": {"id": "tiga", "type": "multi"}}', encoding='utf-8')
    original = provider_admin.os.replace
    before = path.read_bytes()
    attempts, sleeps = [], []

    def intermittent(src, dst):
        attempts.append((Path(src), Path(dst)))
        if len(attempts) <= 2:
            assert path.read_bytes() == before
            raise _sharing_violation()
        return original(src, dst)

    monkeypatch.setattr(provider_admin, '_IS_WINDOWS', True)
    monkeypatch.setattr(provider_admin.os, 'replace', intermittent)
    monkeypatch.setattr(provider_admin.time, 'sleep', sleeps.append)
    provider_admin._write_raw({'mode': {'id': 'dyna', 'type': 'flash'}}, path)
    assert len(attempts) == 3
    assert sleeps == list(provider_admin._WINDOWS_REPLACE_BACKOFF[:2])
    assert json.loads(path.read_text(encoding='utf-8'))['mode']['id'] == 'dyna'
    assert not list(tmp_path.glob('.providers.json.*.tmp'))


def test_windows_permanent_denial_fails_closed_without_damaging_original(tmp_path, monkeypatch):
    path = tmp_path / 'providers.json'
    before = b'{"mode":{"id":"tiga","type":"multi"}}'
    path.write_bytes(before)
    attempts = []

    def blocked(src, dst):
        attempts.append(src)
        raise _sharing_violation()

    monkeypatch.setattr(provider_admin, '_IS_WINDOWS', True)
    monkeypatch.setattr(provider_admin.os, 'replace', blocked)
    monkeypatch.setattr(provider_admin.time, 'sleep', lambda _: None)
    with pytest.raises(PermissionError, match='simulated Windows'):
        provider_admin._write_raw({'mode': {'id': 'dyna', 'type': 'flash'}}, path)
    assert len(attempts) == len(provider_admin._WINDOWS_REPLACE_BACKOFF) + 1
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('.providers.json.*.tmp'))


def test_non_windows_and_non_sharing_errors_are_never_retried(tmp_path, monkeypatch):
    path = tmp_path / 'providers.json'
    def fatal(src, dst):
        raise OSError(errno.ENOSPC, 'disk full')
    monkeypatch.setattr(provider_admin, '_IS_WINDOWS', True)
    monkeypatch.setattr(provider_admin.os, 'replace', fatal)
    monkeypatch.setattr(provider_admin.time, 'sleep', lambda _: pytest.fail('unexpected retry'))
    with pytest.raises(OSError, match='disk full'):
        provider_admin._write_raw({'providers': []}, path)
    assert not path.exists()
    assert not list(tmp_path.glob('.providers.json.*.tmp'))


def test_reselecting_same_mode_does_not_replace_json(tmp_path, monkeypatch):
    with use_app_home(tmp_path):
        path = tmp_path / 'config/providers.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'mode': {'id': 'dyna', 'type': 'flash'}, 'providers': []}), encoding='utf-8')
        before = path.read_bytes()
        monkeypatch.setattr(provider_admin, '_write_raw', lambda *args, **kwargs: pytest.fail('unnecessary replace'))
        selected = provider_admin.set_mode_selection('flash', 'dyna')
        assert selected['pack'] == 'dyna'
        assert selected['policy'] == 'flash'
        assert path.read_bytes() == before
