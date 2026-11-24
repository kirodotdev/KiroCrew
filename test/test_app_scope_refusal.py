"""An app token refused by the API scope check is told why, and what clears it.

An update that adds a ``permissions.api`` entry leaves it held back until the
owner approves it, so the app's call on that path answers 403. The body names the
entry, the app's page and the button that approves it
(``app_permission_not_approved``). A path no declared entry covers names the
manifest change instead (``app_permission_not_declared``), and a grant that could
not be read asks for a retry (``app_permission_unavailable``).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps import manager as manager_mod
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    disable_app,
    enable_app,
    held_back_app_grants,
    install_app,
    update_app,
)
from kiro_crew.dashboard import app_scope_refusal, token_auth

APP = "test-app"
OLD_API = "/api/sessions"
NEW_API = "/api/memory/*"


def _source(tmp_path, sub, version="1.0.0", api=(), display_name="Test App"):
    src = tmp_path / sub / APP
    src.mkdir(parents=True)
    manifest = {
        "name": APP,
        "version": version,
        "displayName": display_name,
        "description": "scope refusals",
        "author": "tester",
        "permissions": {"api": list(api), "events": []},
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return src


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_app_grant_facts", {})
    return home


def _installed_and_widened(tmp_path):
    assert install_app(_source(tmp_path, "v1", api=[OLD_API])).ok
    assert enable_app(APP).ok
    result = update_app(_source(tmp_path, "v2", "2.0.0", api=[OLD_API, NEW_API]))
    assert result.ok, result.error


def _refuse(path):
    """The scope check's answer for APP's token on *path*, read fresh."""
    token_auth._app_perms_cache.clear()
    return token_auth._enforce_app_scope(make_mocked_request("GET", path), APP, path)


def _body(response):
    assert response is not None
    assert response.status == 403
    return json.loads(response.body)


def test_a_held_back_entry_names_the_owner_approval(tmp_path, app_home):
    _installed_and_widened(tmp_path)

    body = _body(_refuse("/api/memory/notes"))

    assert body["code"] == "app_permission_not_approved"
    assert set(body) == {"error", "code", "pending_approval"}
    assert body["pending_approval"] == [NEW_API]
    assert '"Approve new permissions"' in body["error"]
    assert f"/apps/detail/{APP}" in body["error"]
    assert "within 30 seconds" in body["error"]
    # The prose a client shows on its own (the Python client's str(error)) carries
    # the cause, the entry and the remedy, not only the code.
    assert body["error"].startswith('App "Test App" cannot call /api/memory/notes yet.')
    assert f'"{NEW_API}"' in body["error"]


def test_the_approval_the_body_names_clears_the_refusal(tmp_path, app_home):
    _installed_and_widened(tmp_path)
    assert _refuse("/api/memory/notes") is not None

    assert enable_app(APP, grants_consent={"api": [NEW_API], "events": []}).ok

    assert _refuse("/api/memory/notes") is None


def test_a_path_no_entry_declares_names_the_manifest_change(tmp_path, app_home):
    _installed_and_widened(tmp_path)

    body = _body(_refuse("/api/crons"))

    assert body["code"] == "app_permission_not_declared"
    assert body["pending_approval"] == []
    assert '"/api/crons"' in body["error"]
    assert "permissions.api" in body["error"]
    assert f"kirocrew app update {APP}" in body["error"]
    assert "registers its new manifest again" in body["error"]
    assert '"Approve new permissions"' in body["error"]


def test_an_unread_grant_asks_for_a_retry(tmp_path, app_home, monkeypatch):
    """No facts from the read that built the allowlist: nothing is claimed about it."""
    _installed_and_widened(tmp_path)
    monkeypatch.setattr(manager_mod, "get_app_manifest", lambda name: None)

    body = _body(_refuse("/api/memory/notes"))

    assert body["code"] == "app_permission_unavailable"
    assert body["pending_approval"] == []
    assert f"kirocrew app info {APP}" in body["error"]


def _disabled_body(label):
    return {
        "error": (
            f'App "{label}" cannot call /api/memory/notes while it is turned off. To fix '
            "this, the dashboard owner opens the app's page (Apps, Library, then Details "
            f'on {label}, or /apps/detail/{APP}) and clicks "Enable".'
        ),
        "code": "app_disabled",
        "pending_approval": [],
    }


def _primed_refusal(path):
    """The scope check's answer after the middleware's prime, as a request runs them."""
    asyncio.run(token_auth._prime_app_api_allowlist(APP))
    return token_auth._enforce_app_scope(make_mocked_request("GET", path), APP, path)


def test_a_disabled_app_is_told_to_ask_for_the_enable(tmp_path, app_home):
    """The prime pins a turned-off app's entry empty; the refusal names that cause."""
    _installed_and_widened(tmp_path)
    assert _body(_refuse("/api/memory/notes"))["code"] == "app_permission_not_approved"
    assert disable_app(APP).ok

    assert _body(_primed_refusal("/api/memory/notes")) == _disabled_body("Test App")


def test_a_disabled_app_with_no_earlier_read_is_named_by_its_id(tmp_path, app_home):
    _installed_and_widened(tmp_path)
    assert disable_app(APP).ok

    assert _body(_primed_refusal("/api/memory/notes")) == _disabled_body(APP)


def test_a_read_that_finds_the_marker_gone_words_the_disable(tmp_path, app_home):
    """With no prime first, the allowlist read itself finds the app turned off."""
    _installed_and_widened(tmp_path)
    assert disable_app(APP).ok

    assert _body(_refuse("/api/memory/notes")) == _disabled_body(APP)


def test_a_denial_recorded_during_a_read_words_the_disable(tmp_path, app_home, monkeypatch):
    """A read that saw the marker before a disable publishes neither its grant nor its cause."""
    from kiro_crew.dashboard import token_auth_app_scope as scope

    _installed_and_widened(tmp_path)
    real = scope._read_grant_facts

    def _disabled_meanwhile(app_name, manifest):
        facts = real(app_name, manifest)
        scope._app_perms_denied.add(app_name)
        return facts

    monkeypatch.setattr(scope, "_read_grant_facts", _disabled_meanwhile)

    assert _body(_refuse("/api/memory/notes")) == _disabled_body(APP)


def test_the_enable_the_body_names_clears_the_disable(tmp_path, app_home):
    _installed_and_widened(tmp_path)
    assert disable_app(APP).ok
    assert _body(_primed_refusal(OLD_API))["code"] == "app_disabled"

    assert enable_app(APP).ok

    assert _primed_refusal(OLD_API) is None


def test_facts_from_another_read_are_not_used(tmp_path, app_home):
    """An allowlist entry stored by a later read refuses with no stale entry named."""
    _installed_and_widened(tmp_path)
    token_auth._app_perms_cache.clear()
    token_auth._app_api_allowlist(APP)
    stamp, allow = token_auth._app_perms_cache[APP]
    token_auth._app_perms_cache[APP] = (stamp + 1.0, allow)

    request = make_mocked_request("GET", "/api/memory/notes")
    body = _body(token_auth._enforce_app_scope(request, APP, "/api/memory/notes"))

    assert body["code"] == "app_permission_unavailable"


def test_a_failed_held_back_read_leaves_the_allowlist_alone(tmp_path, app_home, monkeypatch):
    _installed_and_widened(tmp_path)

    def _boom(*_args):
        raise OSError("unreadable")

    monkeypatch.setattr(manager_mod, "held_back_app_grants", _boom)
    token_auth._app_perms_cache.clear()

    assert token_auth._app_api_allowlist(APP) == (OLD_API,)
    request = make_mocked_request("GET", "/api/memory/notes")
    assert _body(token_auth._enforce_app_scope(request, APP, "/api/memory/notes"))["code"] == (
        "app_permission_unavailable"
    )


def test_a_manifest_without_a_display_name_keeps_its_grant(tmp_path, app_home, monkeypatch):
    """The refusal's label is read beside the grant and must never cost the app it."""
    from types import SimpleNamespace

    _installed_and_widened(tmp_path)
    bare = SimpleNamespace(
        permissions=SimpleNamespace(
            api=[OLD_API, NEW_API], sessionApproval=False, memberThreads=False
        )
    )
    monkeypatch.setattr(manager_mod, "get_app_manifest", lambda name: bare)

    assert _refuse(OLD_API) is None
    assert _body(_refuse("/api/memory/notes"))["error"].startswith(f'App "{APP}" cannot call')


def test_a_path_outside_api_keeps_the_fixed_reason(tmp_path, app_home):
    """The shared sign-in page quotes its reason unescaped, so it gets no app text."""
    _installed_and_widened(tmp_path)

    response = _refuse("/apps/other-app/api/x")

    assert response is not None
    assert response.status == 403
    assert response.content_type == "text/html"
    assert "app token not permitted for this endpoint" in response.text
    assert "Test App" not in response.text


def test_held_back_entries_are_the_declared_ones_outside_the_approved_set(tmp_path, app_home):
    _installed_and_widened(tmp_path)

    assert held_back_app_grants(APP, "api", [OLD_API, NEW_API]) == [NEW_API]
    assert held_back_app_grants("not-installed", "api", [NEW_API]) is None


def test_a_manifest_that_moved_since_the_allowlist_read_names_no_entry(tmp_path, app_home):
    """The live manifest declares other entries than the allowlist read saw."""
    _installed_and_widened(tmp_path)

    assert held_back_app_grants(APP, "api", [OLD_API]) is None


def test_an_update_between_the_two_reads_is_worded_as_unavailable(tmp_path, app_home, monkeypatch):
    """Old declarations are never paired with the approvals of a newer version."""
    _installed_and_widened(tmp_path)
    real = manager_mod.get_app_manifest
    calls = []

    def _older_then_live(name):
        calls.append(name)
        manifest = real(name)
        if len(calls) == 1:
            manifest.permissions.api = [OLD_API]
        return manifest

    monkeypatch.setattr(manager_mod, "get_app_manifest", _older_then_live)

    body = _body(_refuse("/api/memory/notes"))

    assert body["code"] == "app_permission_unavailable"
    assert body["pending_approval"] == []


def test_an_install_from_before_the_set_holds_nothing_back(tmp_path, app_home):
    # As the one-time import leaves such an install: the store approves what the
    # manifest declares, and the record never stored a set.
    assert install_app(_source(tmp_path, "v1", api=[OLD_API, NEW_API])).ok
    manager_mod._write_approve_declared(APP)
    record = manager_mod.app_dir(APP) / manager_mod.INSTALLED_META_FILENAME
    data = json.loads(record.read_text(encoding="utf-8"))
    data.pop("approvedGrantsStored", None)
    record.write_text(json.dumps(data), encoding="utf-8")

    assert held_back_app_grants(APP, "api", [OLD_API, NEW_API]) == []


def test_two_held_back_entries_are_both_named():
    refusal = app_scope_refusal.scope_refusal(
        APP,
        "Test App",
        "/api/memory",
        pending_approval=["/api/memory", "/api/memory/*"],
        declared=True,
        enabled=True,
        approval_delay_secs=30,
    )

    assert refusal.code == app_scope_refusal.NOT_APPROVED
    assert 'new API permissions "/api/memory", "/api/memory/*"' in refusal.message


@pytest.mark.parametrize(
    ("display_name", "label"),
    [
        ("Test App", "Test App"),
        ("  Test \n\t App  ", "Test App"),
        ("Test\u202eppA", "TestppA"),
        ("Test \u202eApp\u200b\n", "Test App"),
        ("Test\u180e App\U000e0001\u2028", "Test App"),
        ("\u200b\u200d", APP),
        ("", APP),
        (None, APP),
        ("x" * 200, "x" * 79 + "…"),
    ],
)
def test_the_label_is_one_visible_line(display_name, label):
    assert app_scope_refusal.app_label(APP, display_name) == label
