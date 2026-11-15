"""For an app the owner ENABLED, its gateway secret and its approved API grants come
only from records the gateway writes, never from files under ``apps/<name>/``.

That folder is readable and writable from an agent sandbox, and can be renamed aside
and recreated. The secret lives in ``member-records/app-secrets/``, which every
sandbox masks; the approved set lives in ``member-records/app-grants/``.
"""

from __future__ import annotations

import json
import os

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

from conftest import requires_symlinks
from kiro_crew import member_records, sandbox_leaves, security
from kiro_crew.apps import protected_app_state
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    APPROVED_GRANTS_FILENAME,
    disable_app,
    enable_app,
    get_app,
    install_app,
    register_external_app,
    uninstall_app,
)
from kiro_crew.dashboard import token_auth_app_scope as scope
from kiro_crew.dashboard.token_auth import (
    read_app_secret,
    replace_legacy_app_secret,
    validate_app_secret,
)

APP = "test-app"
OLD_API, NEW_API = "/api/sessions", "/api/memory"


@pytest.fixture(autouse=True)
def _outside_a_sandbox(_floor_monkeypatch):
    # The gateway runs unsandboxed; a test host may itself be a sandbox.
    _floor_monkeypatch.delenv(member_records._IN_SANDBOX_MARKER, raising=False)


def _allowlist(name=APP):
    scope._app_perms_cache.clear()
    scope._app_perms_denied.discard(name)
    return scope._app_api_allowlist(name)


def _declare(home, api, name=APP):
    """Rewrite the installed manifest, as a sandboxed shell could."""
    path = home / "apps" / name / APP_MANIFEST_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("permissions", {})["api"] = list(api)
    path.write_text(json.dumps(data), encoding="utf-8")


def _install_and_enable(tmp_path, api=(OLD_API,)):
    src = _make_app_source(tmp_path, permissions={"api": list(api)})
    assert install_app(src).ok
    assert enable_app(APP).ok


# --- the secret ----------------------------------------------------------------


def test_an_enabled_app_keeps_no_secret_in_its_folder(tmp_path, app_home):
    _install_and_enable(tmp_path)
    secret = read_app_secret(APP)
    assert secret and validate_app_secret(APP, secret)
    assert not os.path.lexists(app_home / "apps" / APP / ".app_secret")
    path = protected_app_state.secret_path(APP)
    # The leaf every sandbox masks, and the file tools refuse.
    assert path.is_relative_to(member_records.records_root())
    assert member_records.MEMBER_RECORDS_LEAF in sandbox_leaves._CREW_HIDDEN_LEAVES
    assert security.is_sensitive_path(str(path))


def test_a_secret_planted_in_the_folder_of_an_enabled_app_mints_nothing(tmp_path, app_home):
    _install_and_enable(tmp_path)
    real = read_app_secret(APP)
    (app_home / "apps" / APP / ".app_secret").write_text("planted", encoding="utf-8")
    assert validate_app_secret(APP, "planted") is False
    assert validate_app_secret(APP, real) is True


def test_a_renamed_and_recreated_folder_mints_nothing(tmp_path, app_home):
    _install_and_enable(tmp_path)
    real = read_app_secret(APP)
    root = app_home / "apps" / APP
    root.rename(app_home / "apps" / "moved-aside")
    root.mkdir()
    (root / ".app_secret").write_text("planted", encoding="utf-8")
    assert validate_app_secret(APP, "planted") is False
    assert validate_app_secret(APP, real) is True


def test_an_upgraded_install_gets_a_new_secret_only_where_it_is_handed_over(tmp_path, app_home):
    _install_and_enable(tmp_path)
    # As an older version left it: the secret only in the app's folder.
    protected_app_state.secret_path(APP).unlink()
    legacy = app_home / "apps" / APP / ".app_secret"
    legacy.write_text("seen-by-a-sandbox", encoding="utf-8")

    # A read (token exchange, proxy) never accepts it and never replaces it.
    assert validate_app_secret(APP, "seen-by-a-sandbox") is False
    assert read_app_secret(APP) == ""
    assert legacy.is_file()
    # The backend spawn, which hands the value over, replaces it.
    fresh = replace_legacy_app_secret(APP)
    assert fresh and fresh != "seen-by-a-sandbox"
    assert not os.path.lexists(legacy)
    assert read_app_secret(APP) == fresh
    assert validate_app_secret(APP, fresh) is True


@requires_symlinks
def test_an_upgrade_never_follows_a_link_at_the_old_secret(tmp_path, app_home):
    _install_and_enable(tmp_path)
    protected_app_state.secret_path(APP).unlink()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    link = app_home / "apps" / APP / ".app_secret"
    link.symlink_to(victim)

    assert replace_legacy_app_secret(APP)
    assert victim.read_text(encoding="utf-8") == "keep"
    assert not os.path.lexists(link)


def test_an_upgraded_self_managed_app_gets_its_new_secret_on_re_registration(app_home):
    name = "ext-app"
    assert register_external_app(name, "1.0.0", "Ext").ok
    protected_app_state.secret_path(name).unlink()
    (app_home / "apps" / name / ".app_secret").write_text("old", encoding="utf-8")
    # Its first token exchange after the upgrade is refused and changes nothing...
    assert validate_app_secret(name, "old") is False

    # ...so its next registration still answers with a new secret.
    result = register_external_app(name, "1.0.0", "Ext")
    assert result.ok and result.secret and result.secret != "old"
    assert validate_app_secret(name, result.secret) is True
    assert not os.path.lexists(app_home / "apps" / name / ".app_secret")


def test_a_re_registration_hands_back_the_stored_secret(app_home):
    # e.g. a registry install registered the app first and dropped the response.
    name = "ext-app"
    first = register_external_app(name, "1.0.0", "Ext")
    assert first.ok and first.secret
    again = register_external_app(name, "1.0.1", "Ext")
    assert again.ok and again.secret == first.secret
    assert validate_app_secret(name, again.secret) is True


def test_an_app_without_any_secret_gets_none_invented(app_home):
    assert read_app_secret("never-installed") == ""
    assert validate_app_secret("never-installed", "x") is False


def test_disable_and_uninstall_end_the_mint(tmp_path, app_home):
    _install_and_enable(tmp_path)
    secret = read_app_secret(APP)
    assert disable_app(APP).ok
    assert validate_app_secret(APP, secret) is False
    assert enable_app(APP).ok
    assert validate_app_secret(APP, secret) is True
    assert uninstall_app(APP).ok
    assert validate_app_secret(APP, secret) is False


# --- the approved grants -------------------------------------------------------


def test_a_widened_manifest_or_grant_file_grants_nothing_new(tmp_path, app_home):
    _install_and_enable(tmp_path)
    assert _allowlist() == (OLD_API,)
    _declare(app_home, [OLD_API, NEW_API])
    (app_home / "apps" / APP / APPROVED_GRANTS_FILENAME).write_text(
        json.dumps({"api": [OLD_API, NEW_API], "events": []}), encoding="utf-8"
    )
    assert _allowlist() == (OLD_API,)
    # The owner approving it is what grants it.
    assert enable_app(APP, grants_consent={"api": [NEW_API], "events": []}).ok
    assert _allowlist() == (OLD_API, NEW_API)


def test_a_pre_set_install_is_fixed_once_then_cannot_widen(tmp_path, app_home):
    from kiro_crew.apps import manager

    _install_and_enable(tmp_path)
    manager._write_approve_declared(APP)  # as the one-time import leaves an old install
    assert _allowlist() == (OLD_API,)  # an upgrade cuts nothing off
    _declare(app_home, [OLD_API, NEW_API])
    assert _allowlist() == (OLD_API,)
    assert get_app(APP)["approvedGrants"]["api"] == [OLD_API]


def test_a_registration_without_a_manifest_takes_none_from_the_folder(app_home):
    name = "ext-app"
    assert register_external_app(name, "1.0.0", "Ext").ok
    root = app_home / "apps" / name
    (root / APP_MANIFEST_FILENAME).write_text(
        json.dumps({"name": name, "version": "1.0.0", "permissions": {"api": [OLD_API]}}),
        encoding="utf-8",
    )
    assert _allowlist(name) == ()
    # The first manifest the owner's registration route sends is approved as declared.
    manifest = {"name": name, "version": "1.1.0", "permissions": {"api": [OLD_API]}}
    assert register_external_app(name, "1.1.0", "Ext", manifest_data=manifest).ok
    assert _allowlist(name) == (OLD_API,)


def test_a_builtin_grants_what_it_ships_not_its_folder_copy(app_home, monkeypatch):
    from kiro_crew.apps import manager

    builtin = {
        "name": "ship-app",
        "version": "1.0.0",
        "displayName": "Shipped",
        "description": "x",
        "author": "x",
        "permissions": {"api": [OLD_API]},
    }
    monkeypatch.setattr(manager, "_BUILTIN_APPS", [])
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda *a, **k: [dict(builtin)])
    monkeypatch.setattr(manager, "_edition_builtin_apps", lambda: [])
    manager.register_builtin_apps()
    assert enable_app("ship-app").ok
    assert _allowlist("ship-app") == (OLD_API,)

    _declare(app_home, [OLD_API, NEW_API], name="ship-app")
    assert _allowlist("ship-app") == (OLD_API,)

    # A new version that ships the entry grants it at the next start.
    builtin["permissions"] = {"api": [OLD_API, NEW_API]}
    manager.register_builtin_apps()
    assert _allowlist("ship-app") == (OLD_API, NEW_API)


def test_a_re_registration_after_the_folder_was_replaced_keeps_the_sealed_set(app_home):
    import shutil

    name = "ext-keypad"
    manifest = {"name": name, "version": "1.0.0", "permissions": {"api": [OLD_API]}}
    assert register_external_app(name, "1.0.0", "Keypad", manifest_data=manifest).ok
    assert _allowlist(name) == (OLD_API,)
    # From the sandbox: the folder moved aside and recreated with a wider manifest.
    root = app_home / "apps" / name
    shutil.move(str(root), str(root.parent / f"{name}-aside"))
    root.mkdir()
    (root / APP_MANIFEST_FILENAME).write_text(
        json.dumps({"name": name, "version": "1.0.0", "permissions": {"api": [OLD_API, NEW_API]}}),
        encoding="utf-8",
    )
    # The app re-registers itself at boot, without a manifest.
    assert register_external_app(name, "1.0.1", "Keypad").ok
    assert _allowlist(name) == (OLD_API,)


def test_a_reconstructed_registration_still_awaits_its_first_manifest(app_home):
    import shutil

    name = "ext-later"
    assert register_external_app(name, "1.0.0", "Later").ok
    root = app_home / "apps" / name
    shutil.move(str(root), str(root.parent / f"{name}-aside"))
    root.mkdir()
    assert register_external_app(name, "1.0.0", "Later").ok
    assert _allowlist(name) == ()
    manifest = {"name": name, "version": "1.1.0", "permissions": {"api": [OLD_API]}}
    assert register_external_app(name, "1.1.0", "Later", manifest_data=manifest).ok
    assert _allowlist(name) == (OLD_API,)


def test_every_write_of_a_set_holds_the_app_lock(tmp_path, app_home, monkeypatch):
    # The one-time fix compares and writes under this lock, so an owner's write or an
    # uninstall's removal in another process cannot land between the two.
    from kiro_crew.platform_lock_compat import release_lock, try_acquire_lock

    _install_and_enable(tmp_path)
    lock_path = protected_app_state.grants_path(APP).with_suffix(".lock")
    seen: list[bool] = []
    real = protected_app_state.write_grants_text_locked

    def _probe(name, text):
        fd = os.open(lock_path, os.O_RDWR)
        try:
            free = try_acquire_lock(fd, exclusive=True)
            if free:
                release_lock(fd)
            seen.append(free)
        finally:
            os.close(fd)
        real(name, text)

    monkeypatch.setattr(protected_app_state, "write_grants_text_locked", _probe)
    assert enable_app(APP, grants_consent={"api": [], "events": []}).ok
    assert uninstall_app(APP).ok
    assert seen and not any(seen)


def test_a_write_of_one_apps_set_never_waits_on_another_apps(app_home):
    # An install on the event loop takes the lock once, so a write held by another
    # app's approval must not make it fail.
    with protected_app_state.grants_write_lock("other-app"):
        protected_app_state.write_grants_text(APP, '{"api": [], "events": []}\n')
    assert protected_app_state.read_grants_text(APP) == '{"api": [], "events": []}\n'


def test_a_read_spanning_an_invalidation_does_not_cache_the_old_secret(monkeypatch):
    # The proxy reads the secret in a worker thread; an uninstall or reinstall that
    # invalidates the cache during that read must not be undone by it.
    from kiro_crew.apps import routes_proxy
    from kiro_crew.dashboard import token_auth

    def _read_then_reinstall(name):
        routes_proxy.invalidate_app_secret_cache(name)
        return "old-secret"

    monkeypatch.setattr(token_auth, "read_app_secret", _read_then_reinstall)
    routes_proxy.invalidate_app_secret_cache(APP)
    assert routes_proxy._get_app_secret(APP) == "old-secret"
    assert APP not in routes_proxy._app_secret_cache
    monkeypatch.setattr(token_auth, "read_app_secret", lambda name: "new-secret")
    assert routes_proxy._get_app_secret(APP) == "new-secret"
    assert routes_proxy._app_secret_cache[APP] == "new-secret"
    routes_proxy.invalidate_app_secret_cache(APP)
