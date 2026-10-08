"""Each app stores its approved ``permissions.api`` / ``events`` set on disk.

The set lives in ``approved-grants.json`` beside ``installed.json``. Every
enforcement point (the app-token API allowlist, the WebSocket event scope, the
hook context's event bus) grants an entry only when the live manifest declares
it AND that file holds it. An update that adds entries keeps the app enabled
without them until the owner approves them through
``enable_app(grants_consent=<the entries shown>)``. An install without the file
approves what its manifest declares.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.apps import manager as manager_mod
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    INSTALLED_META_FILENAME,
    app_dir,
    approved_manifest_permissions,
    enable_app,
    get_app,
    install_app,
    register_external_app,
    staged_app_grants,
    update_app,
)
from kiro_crew.dashboard import token_auth, ws_event_scope

APP = "test-app"
OLD_API = "/api/sessions"
NEW_API = "/api/memory"


def _source(tmp_path, sub, version="1.0.0", api=(), events=()):
    src = tmp_path / sub / APP
    src.mkdir(parents=True)
    manifest = {
        "name": APP,
        "version": version,
        "displayName": "Test App",
        "description": "approved grants",
        "author": "tester",
        "permissions": {"api": list(api), "events": list(events)},
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
    return home


def _allowlist():
    token_auth._app_perms_cache.clear()
    return token_auth._app_api_allowlist(APP)


def _events():
    return ws_event_scope._read_declared_events(APP)


def _record_path(name=APP):
    return app_dir(name) / INSTALLED_META_FILENAME


def _edit_record(edit, name=APP):
    data = json.loads(_record_path(name).read_text(encoding="utf-8"))
    edit(data)
    _record_path(name).write_text(json.dumps(data), encoding="utf-8")


def _grants_path(name=APP):
    return app_dir(name) / manager_mod.APPROVED_GRANTS_FILENAME


def _make_legacy(name=APP):
    """Remove the approved set, as on an install from before it was stored."""
    _grants_path(name).unlink()


def _installed_v1(tmp_path):
    assert install_app(_source(tmp_path, "v1", api=[OLD_API], events=["slots:own"])).ok
    assert enable_app(APP).ok


def _widen(tmp_path, sub="v2", version="2.0.0"):
    return update_app(_source(tmp_path, sub, version, api=[OLD_API, NEW_API], events=["slots:own"]))


def test_install_stores_the_declared_set_as_approved(tmp_path, app_home):
    _installed_v1(tmp_path)
    assert get_app(APP)["approvedGrants"] == {"api": [OLD_API], "events": ["slots:own"]}
    assert _allowlist() == (OLD_API,)


def test_update_adding_api_and_event_entries_stays_enabled_on_old_grants(tmp_path, app_home):
    _installed_v1(tmp_path)

    result = update_app(
        _source(tmp_path, "v2", "2.0.0", api=[OLD_API, NEW_API], events=["slots:own", "log"])
    )

    assert result.ok, result.error
    assert "requests new permissions" in result.message
    info = get_app(APP)
    assert info["enabled"] is True
    assert info["version"] == "2.0.0"
    assert info["approvedGrants"] == {"api": [OLD_API], "events": ["slots:own"]}
    assert _allowlist() == (OLD_API,)
    assert _events() == (True, ws_event_scope.build_allowed_event_set(["slots:own"]))
    assert approved_manifest_permissions(info)["events"] == ["slots:own"]
    assert approved_manifest_permissions(info)["api"] == [OLD_API]


def test_grants_consent_approves_the_held_back_entries(tmp_path, app_home):
    _installed_v1(tmp_path)
    assert _widen(tmp_path).ok

    # A plain enable of an enabled app is not an approval.
    assert enable_app(APP).ok
    assert _allowlist() == (OLD_API,)

    result = enable_app(APP, grants_consent={"api": [NEW_API], "events": []})
    assert result.ok
    assert get_app(APP)["approvedGrants"] == {"api": [OLD_API, NEW_API], "events": ["slots:own"]}
    assert _allowlist() == (OLD_API, NEW_API)


def test_a_stale_record_write_cannot_grant_a_held_back_entry(tmp_path, app_home):
    # A writer (dev-mode toggle, a racing registration) read the record before
    # the update, then writes it back after. The set is not in the record, so
    # the write cannot touch it.
    _installed_v1(tmp_path)
    stale = manager_mod._read_installed(APP)
    assert _widen(tmp_path).ok

    manager_mod._write_installed(APP, stale)

    assert _allowlist() == (OLD_API,)


def test_a_stale_pre_set_record_does_not_write_the_approved_set_away(tmp_path, app_home):
    # The same race on an upgraded install: the writer's snapshot predates the
    # set, the update stored one, and the write-back must leave it.
    _installed_v1(tmp_path)
    _make_legacy()
    stale = manager_mod._read_installed(APP)
    assert _widen(tmp_path).ok

    manager_mod._write_installed(APP, stale)

    assert get_app(APP)["approvedGrants"] == {"api": [OLD_API], "events": ["slots:own"]}
    assert _allowlist() == (OLD_API,)


def test_a_stale_save_cannot_restore_an_entry_a_re_registration_dropped(app_home):
    # {A, B} approved -> a re-registration drops B -> the next one re-adds B,
    # which is held back -> a dev-mode save of a record read while {A, B} was
    # approved lands. With the set in installed.json that save carried {A, B}
    # back and granted B unapproved; in its own file the save cannot reach it.
    name = "ext-keypad"
    a, b = "/api/sessions", "/api/memory"

    def _register(version, api):
        manifest = {"name": name, "version": version, "permissions": {"api": api}}
        return register_external_app(name, version, "Keypad", manifest_data=manifest)

    def _allowed():
        token_auth._app_perms_cache.clear()
        return token_auth._app_api_allowlist(name)

    assert _register("1.0.0", [a, b]).ok
    assert _allowed() == (a, b)
    stale = manager_mod._read_installed(name)

    assert _register("1.1.0", [a]).ok
    assert _register("1.2.0", [a, b]).ok
    assert _allowed() == (a,)

    stale.dev = True
    manager_mod._write_installed(name, stale)

    assert _allowed() == (a,)
    assert get_app(name)["approvedGrants"] == {"api": [a], "events": []}


def test_an_app_source_cannot_ship_its_own_approved_set(tmp_path, app_home):
    # The set is the owner's record, never the app's: a file of that name in
    # the source tree is not copied on install or update.
    v1 = _source(tmp_path, "v1", api=[OLD_API])
    (v1 / manager_mod.APPROVED_GRANTS_FILENAME).write_text(
        json.dumps({"api": [OLD_API, NEW_API], "events": []}), encoding="utf-8"
    )
    assert install_app(v1).ok
    assert get_app(APP)["approvedGrants"] == {"api": [OLD_API], "events": []}

    v2 = _source(tmp_path, "v2", "2.0.0", api=[OLD_API, NEW_API])
    (v2 / manager_mod.APPROVED_GRANTS_FILENAME).write_text(
        json.dumps({"api": [OLD_API, NEW_API], "events": []}), encoding="utf-8"
    )
    assert update_app(v2).ok
    assert get_app(APP)["approvedGrants"] == {"api": [OLD_API], "events": []}
    assert _allowlist() == (OLD_API,)


def test_pre_field_record_approves_what_its_manifest_declares(tmp_path, app_home):
    # Backfill: an upgrade must not cut any app off.
    _installed_v1(tmp_path)
    _make_legacy()
    assert _allowlist() == (OLD_API,)
    assert _events() == (True, ws_event_scope.build_allowed_event_set(["slots:own"]))

    # Its first widening update holds back only what the update adds.
    result = _widen(tmp_path)
    assert "requests new permissions" in result.message
    assert get_app(APP)["approvedGrants"] == {"api": [OLD_API], "events": ["slots:own"]}
    assert _allowlist() == (OLD_API,)


def test_manifest_is_read_before_the_record(tmp_path, app_home):
    # A widening update landing between the two reads must meet the record it
    # wrote; read the other way round, a pre-field record would approve it.
    _installed_v1(tmp_path)
    _make_legacy()

    def _update_then_declare():
        assert _widen(tmp_path).ok
        return [OLD_API, NEW_API]

    assert staged_app_grants(APP, "api", _update_then_declare) == [OLD_API]


def test_entries_are_held_back_while_the_new_tree_lands(tmp_path, app_home, monkeypatch):
    # Between the old tree moving aside and the new record landing, the new
    # manifest is on disk with no record beside it: that grants nothing.
    _installed_v1(tmp_path)
    real_copy = manager_mod._copy_app_tree
    seen = []

    def _copy_then_probe(source, dest):
        real_copy(source, dest)
        seen.append(_allowlist())

    monkeypatch.setattr(manager_mod, "_copy_app_tree", _copy_then_probe)
    assert _widen(tmp_path).ok
    assert seen == [()]
    assert _allowlist() == (OLD_API,)


def test_failed_widening_update_restores_the_old_record(tmp_path, app_home, monkeypatch):
    _installed_v1(tmp_path)

    def _fail(source, dest):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(manager_mod, "_copy_app_tree", _fail)
    assert not _widen(tmp_path).ok
    assert get_app(APP)["approvedGrants"] == {"api": [OLD_API], "events": ["slots:own"]}
    assert _allowlist() == (OLD_API,)


def test_update_within_the_approved_set_holds_nothing_back(tmp_path, app_home):
    _installed_v1(tmp_path)
    result = update_app(_source(tmp_path, "v2", "2.0.0", api=[]))
    assert result.ok
    assert "requests new permissions" not in result.message
    assert get_app(APP)["approvedGrants"] == {"api": [], "events": []}


def test_a_dropped_entry_must_be_approved_again(tmp_path, app_home):
    _installed_v1(tmp_path)
    assert _widen(tmp_path).ok
    assert _widen(tmp_path, "v3", "3.0.0").ok
    assert get_app(APP)["approvedGrants"]["api"] == [OLD_API]

    # v4 drops /api/sessions; v5 brings it back, which is a new request.
    assert update_app(_source(tmp_path, "v4", "4.0.0", api=[])).ok
    result = update_app(_source(tmp_path, "v5", "5.0.0", api=[OLD_API]))
    assert "requests new permissions" in result.message
    assert _allowlist() == ()


def test_unreadable_old_manifest_on_a_pre_field_record_fails_closed(tmp_path, app_home):
    _installed_v1(tmp_path)
    _make_legacy()
    (app_dir(APP) / APP_MANIFEST_FILENAME).write_text("{ corrupt", encoding="utf-8")

    assert update_app(_source(tmp_path, "v2", "2.0.0", api=[OLD_API])).ok

    assert get_app(APP)["approvedGrants"] == {"api": [], "events": []}
    assert _allowlist() == ()


def test_unreadable_old_manifest_keeps_a_stored_approval(tmp_path, app_home):
    _installed_v1(tmp_path)
    (app_dir(APP) / APP_MANIFEST_FILENAME).write_text("{ corrupt", encoding="utf-8")

    assert update_app(_source(tmp_path, "v2", "2.0.0", api=[OLD_API])).ok

    assert _allowlist() == (OLD_API,)


@pytest.mark.parametrize("value", ["not-a-record", [], False, 0, "", None, {}])
def test_present_but_malformed_approved_set_fails_closed(tmp_path, app_home, value):
    _installed_v1(tmp_path)
    _grants_path().write_text(json.dumps(value), encoding="utf-8")

    assert _allowlist() == ()
    assert _events() == (True, frozenset())


def test_unreadable_install_record_grants_nothing(tmp_path, app_home):
    _installed_v1(tmp_path)
    _record_path().write_text("{ corrupt", encoding="utf-8")
    assert _allowlist() == ()


def test_self_registration_adding_events_is_held_back(app_home):
    name = "ext-keypad"

    def _manifest(events):
        return {"name": name, "version": "1.0.0", "permissions": {"events": events}}

    assert register_external_app(name, "1.0.0", "Keypad", manifest_data=_manifest(["log"])).ok
    assert get_app(name)["approvedGrants"] == {"api": [], "events": ["log"]}
    result = register_external_app(
        name, "1.1.0", "Keypad", manifest_data=_manifest(["log", "slots:all"])
    )

    assert result.ok, result.error
    info = get_app(name)
    assert info["enabled"] is True
    assert info["approvedGrants"] == {"api": [], "events": ["log"]}
    assert ws_event_scope._read_declared_events(name) == (
        True,
        ws_event_scope.build_allowed_event_set(["log"]),
    )
    assert enable_app(name, grants_consent={"api": [], "events": ["slots:all"]}).ok
    assert get_app(name)["approvedGrants"] == {"api": [], "events": ["log", "slots:all"]}


def test_detail_read_does_not_write_back_over_an_approval(tmp_path, app_home, monkeypatch):
    # get_app read the record, the owner approved, then get_app read the
    # manifest: writing its stale record back would drop the approval.
    _installed_v1(tmp_path)
    assert _widen(tmp_path).ok
    _edit_record(lambda d: d.__setitem__("version", "1.9.0"))
    _edit_record(lambda d: d.__setitem__("lifecycle", "app"))
    real_read = manager_mod._read_installed
    raced = []

    def _read_then_approve(app):
        meta = real_read(app)
        if not raced:
            raced.append(True)
            assert enable_app(APP, grants_consent={"api": [NEW_API], "events": []}).ok
        return meta

    monkeypatch.setattr(manager_mod, "_read_installed", _read_then_approve)
    row = get_app(APP)
    monkeypatch.setattr(manager_mod, "_read_installed", real_read)
    assert row["version"] == "2.0.0"
    assert _allowlist() == (OLD_API, NEW_API)


def test_approving_held_back_events_changes_the_hook_signature(tmp_path, app_home):
    # The hook context's EventBus is built once per load; the reconciler reloads
    # on a signature change, so approval must change it.
    from kiro_crew.apps.hooks_integration import hook_signature

    _installed_v1(tmp_path)
    assert update_app(
        _source(tmp_path, "v2", "2.0.0", api=[OLD_API], events=["slots:own", "log"])
    ).ok
    held = hook_signature(get_app(APP))
    assert enable_app(APP, grants_consent={"api": [], "events": ["log"]}).ok
    approved = hook_signature(get_app(APP))
    assert held != approved
    assert approved_manifest_permissions(get_app(APP))["events"] == ["slots:own", "log"]


def test_approval_covers_only_the_entries_the_owner_was_shown(tmp_path, app_home):
    # The owner saw /api/memory; an update then added /api/projects before the
    # click. Approving what was shown must leave the newer entry held back.
    later = "/api/projects"
    _installed_v1(tmp_path)
    assert _widen(tmp_path).ok
    assert update_app(_source(tmp_path, "v3", "3.0.0", api=[OLD_API, NEW_API, later])).ok

    result = enable_app(APP, grants_consent={"api": [NEW_API], "events": []})

    assert result.ok
    assert get_app(APP)["approvedGrants"]["api"] == [OLD_API, NEW_API]
    assert _allowlist() == (OLD_API, NEW_API)


def test_approval_of_an_entry_no_longer_declared_adds_nothing(tmp_path, app_home):
    # An entry shown then dropped from the manifest must not join the approved
    # set, or a later update re-adding it would be granted unseen.
    _installed_v1(tmp_path)
    assert update_app(_source(tmp_path, "v2", "2.0.0", api=[OLD_API, NEW_API, "/api/x"])).ok
    assert update_app(_source(tmp_path, "v3", "3.0.0", api=[OLD_API, "/api/x"])).ok

    assert enable_app(APP, grants_consent={"api": [NEW_API], "events": []}).ok

    assert get_app(APP)["approvedGrants"]["api"] == [OLD_API]


def test_approval_against_an_unreadable_manifest_is_refused(tmp_path, app_home):
    _installed_v1(tmp_path)
    assert _widen(tmp_path).ok
    manifest_path = app_dir(APP) / APP_MANIFEST_FILENAME
    good = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text("{ corrupt", encoding="utf-8")

    result = enable_app(APP, grants_consent={"api": [NEW_API], "events": []})

    assert not result.ok
    assert result.error_code == "grants_manifest_unreadable"
    manifest_path.write_text(good, encoding="utf-8")
    assert get_app(APP)["approvedGrants"]["api"] == [OLD_API]
    assert _allowlist() == (OLD_API,)
