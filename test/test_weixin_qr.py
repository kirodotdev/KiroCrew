"""Tests for the Weixin QR-login persistence helpers.

Covers the security-sensitive bits of the sign-in write path: a credential must
never touch disk world-readable, a corrupt ``config.json`` must not be silently
replaced by the weixin section alone, and a failed confirmation must not clobber
a working credential.

Permission assertions are POSIX-only: NTFS reports synthetic mode bits, so the
cross-platform guarantee comes from ``platform_compat.restrict_to_owner`` rather
than from raw ``st_mode`` inspection.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from kiro_crew.dashboard.handlers import weixin_qr as qr

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")


def test_atomic_write_secret_round_trips(tmp_path):
    target = tmp_path / ".env"
    qr._atomic_write(target, "WEIXIN=abc\n", secret=True)
    assert target.read_text() == "WEIXIN=abc\n"


@posix_only
def test_atomic_write_secret_is_owner_only(tmp_path):
    target = tmp_path / ".env"
    qr._atomic_write(target, "WEIXIN=abc\n", secret=True)
    assert stat.S_IMODE(target.stat().st_mode) & 0o077 == 0


def test_atomic_write_secret_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / ".env"
    qr._atomic_write(target, "a=1\n", secret=True)
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_env_value_round_trips_and_delete_preserves_other_keys(tmp_path, monkeypatch):
    ep = tmp_path / ".env"
    ep.write_text("OTHER=keepme\nWEIXIN_TOKEN=old\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    assert qr._read_env_value("WEIXIN_TOKEN") == "old"
    assert qr._read_env_value("ABSENT") is None
    qr._delete_env_key("WEIXIN_TOKEN")
    assert qr._read_env_value("WEIXIN_TOKEN") is None
    assert qr._read_env_value("OTHER") == "keepme"


def _fail_config_writes(monkeypatch, env_file):
    """Let the .env write through, but make the config.json write fail."""
    real = qr._atomic_write

    def selective(path, text, **kw):
        if path == env_file:
            return real(path, text, **kw)
        raise OSError("ENOSPC")

    monkeypatch.setattr(qr, "_atomic_write", selective)


def test_failed_config_commit_restores_the_previous_credential(tmp_path, monkeypatch):
    """A half-applied sign-in must not destroy a working credential."""
    ep = tmp_path / ".env"
    ep.write_text("OTHER=keepme\nWEIXIN_TOKEN=previous\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    _fail_config_writes(monkeypatch, ep)

    with pytest.raises(OSError):
        qr._commit_credential_and_config(tmp_path / "config.json", "{}", "brand-new")

    assert qr._read_env_value("WEIXIN_TOKEN") == "previous"
    assert qr._read_env_value("OTHER") == "keepme"


def test_failed_config_commit_removes_a_credential_that_did_not_exist(tmp_path, monkeypatch):
    """First-time sign-in: rollback must leave no orphan credential behind."""
    ep = tmp_path / ".env"
    ep.write_text("OTHER=x\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    _fail_config_writes(monkeypatch, ep)

    with pytest.raises(OSError):
        qr._commit_credential_and_config(tmp_path / "config.json", "{}", "brand-new")

    assert qr._read_env_value("WEIXIN_TOKEN") is None
    assert qr._read_env_value("OTHER") == "x"


def test_successful_commit_writes_both_files(tmp_path, monkeypatch):
    ep = tmp_path / ".env"
    cp = tmp_path / "config.json"
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    qr._commit_credential_and_config(cp, '{"weixin": {}}', "brand-new")
    assert qr._read_env_value("WEIXIN_TOKEN") == "brand-new"
    assert json.loads(cp.read_text()) == {"weixin": {}}


def test_atomic_write_plain_config_preserves_existing_mode(tmp_path):
    """A non-secret replace must never widen permissions.

    config.json can hold inline fallback credentials, so if the operator (or an
    earlier save) locked it to 0600, the atomic replace must not hand it back at
    the umask default and expose it to other local users.
    """
    target = tmp_path / "config.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    os.chmod(target, 0o600)
    qr._atomic_write(target, '{"a": 2}')
    assert json.loads(target.read_text()) == {"a": 2}
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_write_plain_config(tmp_path):
    target = tmp_path / "config.json"
    qr._atomic_write(target, '{"a": 1}')
    assert json.loads(target.read_text()) == {"a": 1}


def test_write_env_secret_upserts_and_preserves_other_lines(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OTHER=keep\nWEIXIN_TOKEN=old\nTRAILING=yes\n")
    monkeypatch.setattr(qr, "env_path", lambda: env)
    qr._write_env_secret("WEIXIN_TOKEN", "new")
    body = env.read_text()
    assert "OTHER=keep" in body
    assert "TRAILING=yes" in body
    assert "WEIXIN_TOKEN=new" in body
    assert "old" not in body


def test_stage_config_preserves_unrelated_sections(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"slack": {"command": "kirocrew"}, "agent": {"model": "auto"}}))
    monkeypatch.setattr(qr, "config_path", lambda: cfg)
    path, serialized = qr._stage_weixin_config(account_id="acct1@im.bot", base_url="https://x")
    assert path == cfg
    data = json.loads(serialized)
    assert data["slack"] == {"command": "kirocrew"}  # untouched
    assert data["agent"] == {"model": "auto"}
    assert data["weixin"]["enabled"] is True
    assert data["weixin"]["account_id"] == "acct1@im.bot"
    assert data["weixin"]["base_url"] == "https://x"


def test_stage_config_does_not_write_anything(tmp_path, monkeypatch):
    """Staging is pure: the credential write must be able to run after it."""
    cfg = tmp_path / "config.json"
    original = json.dumps({"agent": {"model": "auto"}})
    cfg.write_text(original)
    monkeypatch.setattr(qr, "config_path", lambda: cfg)
    qr._stage_weixin_config(account_id="acct1", base_url="https://x")
    assert cfg.read_text() == original


def test_stage_config_raises_on_a_corrupt_file(tmp_path, monkeypatch):
    """A malformed config must fail BEFORE the credential is overwritten."""
    cfg = tmp_path / "config.json"
    cfg.write_text("{not valid json")
    monkeypatch.setattr(qr, "config_path", lambda: cfg)
    with pytest.raises(Exception):
        qr._stage_weixin_config(account_id="acct1", base_url="https://x")
    assert cfg.read_text() == "{not valid json"  # original preserved for repair


def test_stage_config_rejects_a_non_object_document(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("[1, 2, 3]")
    monkeypatch.setattr(qr, "config_path", lambda: cfg)
    with pytest.raises(ValueError):
        qr._stage_weixin_config(account_id="acct1", base_url="https://x")


def test_corrupt_config_does_not_clobber_an_existing_credential(tmp_path, monkeypatch):
    """Regression: stage-before-write keeps a working token when config is bad."""
    env = tmp_path / ".env"
    env.write_text("WEIXIN_TOKEN=working\n")
    cfg = tmp_path / "config.json"
    cfg.write_text("{corrupt")
    monkeypatch.setattr(qr, "env_path", lambda: env)
    monkeypatch.setattr(qr, "config_path", lambda: cfg)

    # The handler stages first, so this raises before _write_env_secret runs.
    with pytest.raises(Exception):
        _path, _serialized = qr._stage_weixin_config(account_id="new", base_url="https://x")
        qr._write_env_secret("WEIXIN_TOKEN", "replacement")

    assert "working" in env.read_text()
    assert "replacement" not in env.read_text()
