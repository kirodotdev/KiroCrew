"""An app token is minted, and its API grants honoured, only for an app whose
owner-enable marker the gateway wrote on an owner-authenticated action.

The marker lives in the sealed top-level ``host-exec-grants/`` leaf. Files under
``apps/<name>/`` (secret, manifest, install record) are agent-writable and must never
be enough on their own.
"""

from __future__ import annotations

import asyncio
import json

from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

from conftest import requires_symlinks
from kiro_crew.apps.host_exec_grants import host_exec_granted
from kiro_crew.apps.manager import (
    disable_app,
    enable_app,
    install_app,
    register_external_app,
    uninstall_app,
)
from kiro_crew.dashboard import token_auth_app_scope as scope
from kiro_crew.dashboard.token_auth import read_app_secret, validate_app_secret


def _secret(home, name):
    del home  # the gateway's own record, never the app's folder
    return read_app_secret(name)


def _plant(home, name, secret="planted-secret", enabled=True):
    """What a sandboxed shell can write under apps/<name>/."""
    root = home / "apps" / name
    root.mkdir(parents=True, exist_ok=True)
    (root / ".app_secret").write_text(secret, encoding="utf-8")
    (root / "app.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "displayName": "Planted",
                "description": "x",
                "author": "x",
                "permissions": {"api": ["/api/sessions"]},
            }
        ),
        encoding="utf-8",
    )
    (root / "installed.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "enabled": enabled}), encoding="utf-8"
    )
    return secret


def _install_and_enable(tmp_path, name="test-app", **manifest):
    assert install_app(_make_app_source(tmp_path, name=name, **manifest)).ok
    assert enable_app(name).ok


def test_planted_app_cannot_mint(app_home):
    secret = _plant(app_home, "ghost")
    assert validate_app_secret("ghost", secret) is False
    scope._app_perms_cache.clear()
    assert scope._app_api_allowlist("ghost") == ()


def test_owner_installed_app_can_mint(tmp_path, app_home):
    _install_and_enable(tmp_path)
    assert validate_app_secret("test-app", _secret(app_home, "test-app")) is True
    assert validate_app_secret("test-app", "wrong") is False
    assert validate_app_secret("test-app", "") is False


def test_install_alone_does_not_mint(tmp_path, app_home):
    assert install_app(_make_app_source(tmp_path)).ok
    assert validate_app_secret("test-app", _secret(app_home, "test-app")) is False


def test_first_owner_registration_mints(app_home):
    assert register_external_app("ext-app", "1.0.0", "Ext").ok
    assert validate_app_secret("ext-app", _secret(app_home, "ext-app")) is True


@requires_symlinks
def test_an_unrecorded_owner_enable_is_reported_and_recoverable(tmp_path, app_home, monkeypatch):
    import kiro_crew.apps.manager as manager

    assert install_app(_make_app_source(tmp_path)).ok
    with monkeypatch.context() as m:
        m.setattr(manager, "grant_host_exec", lambda _n: False)
        assert not enable_app("test-app").ok
        assert not register_external_app("ext-app", "1.0.0", "Ext").ok
    # Enabling again (the record already reads enabled) records the marker.
    assert enable_app("test-app").ok
    assert validate_app_secret("test-app", _secret(app_home, "test-app")) is True
    assert enable_app("ext-app").ok
    assert validate_app_secret("ext-app", _secret(app_home, "ext-app")) is True


@requires_symlinks
def test_symlinked_app_dir_cannot_mint(tmp_path, app_home):
    elsewhere = tmp_path / "elsewhere"
    secret = _plant(elsewhere, "ghost")
    (app_home / "apps").mkdir(exist_ok=True)
    (app_home / "apps" / "ghost").symlink_to(elsewhere / "apps" / "ghost")
    assert validate_app_secret("ghost", secret) is False


def test_disable_and_uninstall_revoke(tmp_path, app_home):
    _install_and_enable(tmp_path)
    secret = _secret(app_home, "test-app")
    assert disable_app("test-app").ok
    assert validate_app_secret("test-app", secret) is False
    assert enable_app("test-app").ok
    assert validate_app_secret("test-app", secret) is True
    assert uninstall_app("test-app").ok
    assert validate_app_secret("test-app", secret) is False
    # A directory planted under the uninstalled name with the old secret mints nothing.
    _plant(app_home, "test-app", secret=secret)
    assert validate_app_secret("test-app", secret) is False


def test_disable_revokes_when_record_already_reads_disabled(tmp_path, app_home):
    """A forged enabled:false must not leave the mint live past the owner's disable."""
    _install_and_enable(tmp_path)
    secret = _secret(app_home, "test-app")
    path = app_home / "apps" / "test-app" / "installed.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["enabled"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    assert disable_app("test-app").ok
    assert validate_app_secret("test-app", secret) is False


def test_forged_enabled_record_does_not_restore_the_mint(tmp_path, app_home):
    _install_and_enable(tmp_path)
    secret = _secret(app_home, "test-app")
    assert disable_app("test-app").ok
    path = app_home / "apps" / "test-app" / "installed.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["enabled"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    assert validate_app_secret("test-app", secret) is False
    assert host_exec_granted("test-app") is False


def test_upgrade_without_a_marker_recovers_with_one_enable(tmp_path, app_home):
    """An app enabled before the marker existed cannot mint until the owner
    re-enables it, which writes the marker."""
    _install_and_enable(tmp_path)
    (app_home / "host-exec-grants" / "test-app").unlink()  # pre-upgrade state
    secret = _secret(app_home, "test-app")
    assert validate_app_secret("test-app", secret) is False
    assert enable_app("test-app").ok  # the "already enabled" path writes it
    assert validate_app_secret("test-app", secret) is True


@requires_symlinks
def test_write_app_secret_never_writes_through_a_link(tmp_path, app_home):
    from kiro_crew.dashboard.token_auth import write_app_secret

    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    root = app_home / "apps" / "demo"
    root.mkdir(parents=True)
    (root / ".app_secret").symlink_to(victim)
    write_app_secret("demo", "s3cret")
    assert victim.read_text(encoding="utf-8") == "keep"
    assert not (root / ".app_secret").is_symlink()


def test_api_grants_need_the_owner_enable(tmp_path, app_home):
    assert install_app(_make_app_source(tmp_path, permissions={"api": ["/api/sessions"]})).ok
    scope._app_perms_cache.clear()
    assert scope._app_api_allowlist("test-app") == ()
    assert enable_app("test-app").ok
    scope._app_perms_cache.clear()
    assert scope._app_api_allowlist("test-app") == ("/api/sessions",)


def test_an_in_flight_refresh_cannot_overwrite_a_denial(tmp_path, app_home, monkeypatch):
    """A refresh that read the marker before a disable publishes after a later
    request recorded the denial: the denial must still win."""
    _install_and_enable(tmp_path, permissions={"api": ["/api/sessions"]})
    scope._app_perms_cache.clear()
    import kiro_crew.apps.manager as manager

    real = manager.staged_app_grants

    def _disable_mid_refresh(name, family, read_declared):
        granted = real(name, family, read_declared)  # read while still enabled
        assert disable_app(name).ok
        asyncio.run(scope._prime_app_api_allowlist(name))  # a later request's denial
        return granted

    monkeypatch.setattr(manager, "staged_app_grants", _disable_mid_refresh)
    assert scope._app_api_allowlist("test-app") == ()
    assert not scope.app_token_path_allowed("test-app", "/api/sessions")


def test_disable_ends_cached_grants_on_the_next_request(tmp_path, app_home):
    """The middleware primes before each scope check; a disable must not wait out
    the allowlist cache, and a re-enable restores the grants."""
    _install_and_enable(tmp_path, permissions={"api": ["/api/sessions"]})
    scope._app_perms_cache.clear()
    asyncio.run(scope._prime_app_api_allowlist("test-app"))
    assert scope.app_token_path_allowed("test-app", "/api/sessions")
    assert disable_app("test-app").ok
    asyncio.run(scope._prime_app_api_allowlist("test-app"))
    assert not scope.app_token_path_allowed("test-app", "/api/sessions")
    assert enable_app("test-app").ok
    asyncio.run(scope._prime_app_api_allowlist("test-app"))
    assert scope.app_token_path_allowed("test-app", "/api/sessions")
