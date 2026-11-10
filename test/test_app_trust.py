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
from kiro_crew.dashboard.token_auth import validate_app_secret


def _secret(home, name):
    from kiro_crew.apps import protected_app_state

    return protected_app_state.read_app_secret(name)


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


def test_a_held_secrets_lock_refuses_a_registration_before_any_record(app_home, monkeypatch):
    """The registry install registers on the loop, where a held lock is tried once."""
    import contextlib

    from kiro_crew.apps import protected_app_state

    @contextlib.contextmanager
    def _held():
        raise OSError("lock held")
        yield

    monkeypatch.setattr(protected_app_state, "app_secrets_lock", _held)
    result = register_external_app("ext-app", "1.0.0", "Ext")
    assert not result.ok and "app secret" in (result.error or "")
    assert not (app_home / "apps" / "ext-app" / "installed.json").exists()


def test_a_legacy_external_app_recovers_by_registering_again(app_home):
    """An enable issues an external app no secret it could never be handed; the next
    registration issues one, returns it, and removes the old file."""
    from kiro_crew.apps import protected_app_state

    assert register_external_app("ext-app", "1.0.0", "Ext").ok
    protected_app_state.app_secret_path("ext-app").unlink()
    legacy = app_home / "apps" / "ext-app" / ".app_secret"
    legacy.write_text("old", encoding="utf-8")
    assert disable_app("ext-app").ok
    assert enable_app("ext-app").ok
    assert _secret(app_home, "ext-app") == ""
    result = register_external_app("ext-app", "1.0.0", "Ext")
    assert result.ok and result.secret not in ("", "old")
    assert validate_app_secret("ext-app", result.secret) is True
    assert not legacy.exists()


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
    # The refused registration delivered no secret, so none stays stored; after the
    # enable records the marker, registering again issues and returns one.
    assert _secret(app_home, "ext-app") == ""
    assert enable_app("ext-app").ok
    again = register_external_app("ext-app", "1.0.0", "Ext")
    assert again.ok and again.secret
    assert validate_app_secret("ext-app", again.secret) is True


def test_a_registration_that_fails_after_storing_its_secret_removes_it(app_home, monkeypatch):
    """Any failed exit after the secret is stored delivers none, so none is kept."""
    import pytest

    import kiro_crew.apps.manager as manager

    def _boom(_name):
        raise OSError("disk full")

    from kiro_crew.apps import routes as app_routes

    with monkeypatch.context() as m:
        m.setattr(manager, "app_data_dir", _boom)
        # As a proxy request during the failed call would have left it.
        m.setitem(app_routes._app_secret_cache, "ext-app", "provisional")
        with pytest.raises(OSError):
            register_external_app("ext-app", "1.0.0", "Ext")
        assert "ext-app" not in app_routes._app_secret_cache
    assert _secret(app_home, "ext-app") == ""
    result = register_external_app("ext-app", "1.0.0", "Ext")
    assert result.ok and result.secret
    # The failed first registration never recorded the owner's enable; once the
    # owner enables it, the secret the retry returned mints.
    assert enable_app("ext-app").ok
    assert validate_app_secret("ext-app", result.secret) is True


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
    assert _secret(app_home, "demo") == "s3cret"


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


def test_a_replaced_secret_file_mints_nothing(tmp_path, app_home):
    """A sandboxed shell can recreate apps/<name>/ with its own .app_secret: the
    exchange checks the secret the gateway stored in member-records/, not that file."""
    _install_and_enable(tmp_path)
    from kiro_crew import member_records
    from kiro_crew.apps import protected_app_state

    secret = _secret(app_home, "test-app")
    assert secret
    assert not (app_home / "apps" / "test-app" / ".app_secret").exists()
    # The whole directory is masked and created before every spawn, so a sandbox
    # started before this install cannot read the secret either.
    assert member_records.records_root() in protected_app_state.app_secret_path("test-app").parents
    (app_home / "apps" / "test-app" / ".app_secret").write_text("planted", encoding="utf-8")
    assert validate_app_secret("test-app", "planted") is False
    assert validate_app_secret("test-app", secret) is True


def test_a_legacy_secret_is_replaced_on_the_owner_enable(tmp_path, app_home):
    from kiro_crew.apps import protected_app_state

    _install_and_enable(tmp_path)
    old = _secret(app_home, "test-app")
    # As an older build left it: only the app directory's file.
    protected_app_state.app_secret_path("test-app").unlink()
    (app_home / "apps" / "test-app" / ".app_secret").write_text(old, encoding="utf-8")
    assert validate_app_secret("test-app", old) is False
    assert disable_app("test-app").ok
    assert enable_app("test-app").ok
    new = _secret(app_home, "test-app")
    assert new != old
    assert validate_app_secret("test-app", new) is True


def test_the_reissue_removes_the_legacy_secret_file(tmp_path, app_home):
    """Once the store holds the new secret, the dead file is gone from the app tree."""
    from kiro_crew.apps import protected_app_state

    _install_and_enable(tmp_path)
    protected_app_state.app_secret_path("test-app").unlink()
    legacy = app_home / "apps" / "test-app" / ".app_secret"
    legacy.write_text("old", encoding="utf-8")
    assert enable_app("test-app").ok
    assert _secret(app_home, "test-app") not in ("", "old")
    assert not legacy.exists() and not legacy.is_symlink()


@requires_symlinks
def test_a_refused_uninstall_keeps_the_secret(tmp_path, app_home):
    """The secret goes only once the tree's removal committed: a linked data/ refuses
    the uninstall, and the app keeps the secret its backend holds."""
    import shutil

    _install_and_enable(tmp_path)
    secret = _secret(app_home, "test-app")
    data = app_home / "apps" / "test-app" / "data"
    shutil.rmtree(data)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    data.symlink_to(elsewhere, target_is_directory=True)
    assert not uninstall_app("test-app").ok
    assert _secret(app_home, "test-app") == secret
    data.unlink()
    assert uninstall_app("test-app").ok
    assert _secret(app_home, "test-app") == ""


def test_an_unreadable_store_mints_nothing(tmp_path, app_home, monkeypatch):
    from kiro_crew.apps import protected_app_state

    _install_and_enable(tmp_path)
    secret = _secret(app_home, "test-app")

    def _unreadable(_name):
        raise PermissionError("store unreadable")

    monkeypatch.setattr(protected_app_state, "read_app_secret", _unreadable)
    assert validate_app_secret("test-app", secret) is False


def test_an_app_enabled_on_an_older_build_recovers_with_one_enable(tmp_path, app_home):
    """The owner's enable of an app that already reads enabled issues the stored secret."""
    from kiro_crew.apps import protected_app_state

    _install_and_enable(tmp_path)
    old = _secret(app_home, "test-app")
    protected_app_state.app_secret_path("test-app").unlink()
    (app_home / "apps" / "test-app" / ".app_secret").write_text(old, encoding="utf-8")
    assert enable_app("test-app").ok  # the "already enabled" path
    new = _secret(app_home, "test-app")
    assert new and new != old
    assert validate_app_secret("test-app", new) is True
    assert validate_app_secret("test-app", old) is False


def test_every_concurrent_issuer_gets_the_same_secret(app_home):
    """Backend spawn, enable and registration can race on an app with no stored secret:
    one secret is issued, and every caller gets it."""
    import threading

    from kiro_crew.apps import protected_app_state

    legacy = app_home / "apps" / "demo" / ".app_secret"
    assert protected_app_state.issue_app_secret_if_absent("demo", legacy) == ""
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old", encoding="utf-8")
    got: list[str] = []
    threads = [
        threading.Thread(
            target=lambda: got.append(
                protected_app_state.issue_app_secret_if_absent("demo", legacy)
            )
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(got) == 8 and len(set(got)) == 1
    assert got[0] not in ("", "old")
    assert protected_app_state.read_app_secret("demo") == got[0]
    assert protected_app_state.issue_app_secret_if_absent("demo") == got[0]
