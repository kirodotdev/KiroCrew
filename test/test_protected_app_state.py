"""The owner's approvals and disable live outside the app's own tree.

An app's code can write ``apps/<name>/``, so neither the approved set of staged
permission entries nor the owner's disable may be read from there. Both live under
``member-records/`` (``apps.protected_app_state``), which the sandbox masks. These
tests drive the real install, enable, disable and uninstall paths and pin that:

* a file the app writes in its own tree, or a rewrite of its ``installed.json``,
  approves nothing and does not turn a disabled app back on;
* the one-time import carries each app's existing set over, keeps a legacy record's
  "approve what is declared", and never reads the app tree again;
* the owner's enable ends the disable, and an uninstall removes both records.
"""

from __future__ import annotations

import concurrent.futures
import json
import os

import pytest

from kiro_crew import member_records, sandbox_leaves
from kiro_crew.apps import manager as manager_mod
from kiro_crew.apps import protected_app_state
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    APPROVED_GRANTS_FILENAME,
    INSTALLED_META_FILENAME,
    app_dir,
    app_enabled_state,
    disable_app,
    enable_app,
    install_app,
    is_app_enabled,
    staged_app_grants,
    uninstall_app,
    update_app,
)

APP = "guarded-app"
OLD_API = "/api/sessions"
NEW_API = "/api/memory"


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    return home


def _source(tmp_path, sub, api, *, version="1.0.0", **permissions):
    src = tmp_path / sub / APP
    src.mkdir(parents=True)
    manifest = {
        "name": APP,
        "version": version,
        "displayName": "Guarded",
        "description": "protected app state",
        "author": "tester",
        "permissions": {"api": list(api), **permissions},
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return src


def _install(tmp_path, api=(OLD_API,)):
    assert install_app(_source(tmp_path, "v1", api)).ok
    assert enable_app(APP).ok


def _declare(api):
    """Rewrite the installed manifest, as the app's own code could."""
    path = app_dir(APP) / APP_MANIFEST_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["permissions"]["api"] = list(api)
    path.write_text(json.dumps(data), encoding="utf-8")


def _edit_record(edit):
    path = app_dir(APP) / INSTALLED_META_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def _granted():
    return staged_app_grants(APP, "api", lambda: [OLD_API, NEW_API])


def test_both_records_live_in_the_masked_store(tmp_path, app_home):
    _install(tmp_path)
    assert disable_app(APP).ok
    root = member_records.records_root()
    for path in (
        protected_app_state.grants_path(APP),
        protected_app_state._disabled_path(APP),
    ):
        assert path.is_file()
        assert path.is_relative_to(root)
        assert not path.is_relative_to(app_dir(APP))
    # The directory the sandbox masks in every mode.
    assert member_records.MEMBER_RECORDS_LEAF in sandbox_leaves._CREW_HIDDEN_LEAVES


def test_a_set_the_app_writes_in_its_own_tree_approves_nothing(tmp_path, app_home):
    _install(tmp_path)
    _declare([OLD_API, NEW_API])
    (app_dir(APP) / APPROVED_GRANTS_FILENAME).write_text(
        json.dumps({"api": [OLD_API, NEW_API], "events": []}), encoding="utf-8"
    )

    assert _granted() == [OLD_API]


def test_clearing_the_stored_flag_in_installed_json_approves_nothing(tmp_path, app_home):
    _install(tmp_path)
    _declare([OLD_API, NEW_API])
    _edit_record(lambda data: data.update(approvedGrantsStored=False))
    (app_dir(APP) / APPROVED_GRANTS_FILENAME).unlink(missing_ok=True)

    assert _granted() == [OLD_API]


def test_a_missing_store_record_approves_nothing(tmp_path, app_home):
    _install(tmp_path)
    protected_app_state.grants_path(APP).unlink()

    assert _granted() == []


def _legacy_home(tmp_path, *, stored, legacy):
    """An app as an older build left it: no store record, the set (if any) in its tree."""
    _install(tmp_path)
    protected_app_state.grants_path(APP).unlink()
    marker = protected_app_state.GRANTS_IMPORT_MARKER
    (member_records.records_dir(protected_app_state.APP_GRANTS_KIND) / marker).unlink(
        missing_ok=True
    )
    _edit_record(lambda data: data.update(approvedGrantsStored=stored))
    if legacy is not None:
        (app_dir(APP) / APPROVED_GRANTS_FILENAME).write_text(json.dumps(legacy), encoding="utf-8")


def test_the_import_carries_an_existing_set_over_once(tmp_path, app_home):
    _legacy_home(tmp_path, stored=True, legacy={"api": [OLD_API], "events": []})
    _declare([OLD_API, NEW_API])

    assert _granted() == [OLD_API]
    assert protected_app_state.grants_imported()
    # Written in the tree after the import: never read.
    (app_dir(APP) / APPROVED_GRANTS_FILENAME).write_text(
        json.dumps({"api": [OLD_API, NEW_API], "events": []}), encoding="utf-8"
    )
    assert _granted() == [OLD_API]


def test_the_import_keeps_a_legacy_record_approving_what_it_declares(tmp_path, app_home):
    _legacy_home(tmp_path, stored=False, legacy=None)
    _declare([OLD_API, NEW_API])

    assert _granted() == [OLD_API, NEW_API]
    # Fixed at that first read: a later write to the app's own manifest widens nothing.
    _declare([OLD_API, NEW_API, "/api/config"])
    assert staged_app_grants(APP, "api", lambda: [OLD_API, NEW_API, "/api/config"]) == [
        OLD_API,
        NEW_API,
    ]


def test_the_import_reads_a_lost_set_as_nothing_approved(tmp_path, app_home):
    _legacy_home(tmp_path, stored=True, legacy=None)

    assert _granted() == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a POSIX FIFO")
def test_a_fifo_planted_as_the_legacy_set_neither_blocks_nor_approves(tmp_path, app_home):
    _legacy_home(tmp_path, stored=True, legacy=None)
    os.mkfifo(app_dir(APP) / APPROVED_GRANTS_FILENAME)

    # A blocking open would never return; the bound turns that into a failure.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        granted = pool.submit(_granted).result(timeout=30)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    assert granted == []


def test_a_rewritten_record_does_not_turn_a_disabled_app_back_on(tmp_path, app_home):
    _install(tmp_path)
    assert disable_app(APP).ok

    _edit_record(lambda data: data.update(enabled=True))

    assert is_app_enabled(APP) is False
    assert app_enabled_state(APP) is False
    assert manager_mod._read_installed(APP).enabled is False


def test_the_owners_enable_ends_the_disable(tmp_path, app_home):
    _install(tmp_path)
    assert disable_app(APP).ok
    assert enable_app(APP).ok

    assert is_app_enabled(APP) is True
    assert not protected_app_state.owner_disabled(APP)


def test_a_disable_already_on_disk_is_still_recorded(tmp_path, app_home):
    _install(tmp_path)
    _edit_record(lambda data: data.update(enabled=False))
    assert disable_app(APP).ok

    _edit_record(lambda data: data.update(enabled=True))

    assert is_app_enabled(APP) is False


def test_uninstall_removes_both_records(tmp_path, app_home):
    _install(tmp_path)
    assert disable_app(APP).ok
    assert uninstall_app(APP, keep_data=False).ok

    assert not protected_app_state.grants_path(APP).exists()
    assert not protected_app_state.owner_disabled(APP)


def test_an_update_held_off_for_consent_records_the_disable(tmp_path, app_home):
    _install(tmp_path)
    # The update swaps the tree, so there is no record on disk to compare against.
    assert update_app(_source(tmp_path, "v2", [OLD_API], version="2.0.0", sessionApproval=True)).ok
    assert is_app_enabled(APP) is False

    _edit_record(lambda data: data.update(enabled=True, sessionApprovalConsentPending=False))

    assert is_app_enabled(APP) is False
    assert protected_app_state.owner_disabled(APP)


def test_a_builtin_registered_after_the_import_approves_what_it_declares(app_home, monkeypatch):
    protected_app_state.mark_grants_imported()
    builtin = {
        "name": "late-builtin",
        "version": "1.0.0",
        "displayName": "Late",
        "description": "registered after the one-time import",
        "author": "tester",
        "permissions": {"api": [OLD_API]},
    }
    monkeypatch.setattr(manager_mod, "_BUILTIN_APPS", [])
    monkeypatch.setattr(manager_mod, "discover_builtin_apps", lambda *a, **k: [builtin])
    monkeypatch.setattr(manager_mod, "_edition_builtin_apps", lambda: [])
    manager_mod.register_builtin_apps()

    assert staged_app_grants("late-builtin", "api", lambda: [OLD_API]) == [OLD_API]


def test_a_failed_uninstall_keeps_the_approvals_and_the_disable(tmp_path, app_home):
    _install(tmp_path)
    assert disable_app(APP).ok
    before = protected_app_state.grants_path(APP).read_text(encoding="utf-8")
    data = app_dir(APP) / "data"
    if data.exists():
        data.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    data.symlink_to(elsewhere, target_is_directory=True)

    assert not uninstall_app(APP, keep_data=True).ok

    assert protected_app_state.grants_path(APP).read_text(encoding="utf-8") == before
    assert protected_app_state.owner_disabled(APP)


def test_the_import_holds_its_lock_until_the_marker_is_written(tmp_path, app_home, monkeypatch):
    from kiro_crew.platform_lock_compat import release_lock, try_acquire_lock

    _legacy_home(tmp_path, stored=True, legacy={"api": [OLD_API], "events": []})
    lock_path = member_records.records_dir(protected_app_state.APP_GRANTS_KIND) / ".importing"
    app_lock = protected_app_state.grants_path(APP).with_suffix(".lock")
    seen: list[tuple[bool, bool]] = []
    real_write = protected_app_state.write_grants_text_locked

    def _free(path):
        if not path.exists():
            return True
        fd = os.open(path, os.O_RDWR)
        try:
            got = try_acquire_lock(fd, exclusive=True)
            if got:
                release_lock(fd)
            return got
        finally:
            os.close(fd)

    def _write_while_probing(name, text):
        # Another importer, thread or process, cannot take the lock mid-pass.
        seen.append((_free(lock_path), _free(app_lock)))
        real_write(name, text)

    monkeypatch.setattr(protected_app_state, "write_grants_text_locked", _write_while_probing)
    assert _granted() == [OLD_API]
    # The import's write holds the import lock; the one after it, which fixes the
    # families the carried-over set was written before, holds the app's lock.
    assert len(seen) == 2
    assert seen[0][0] is False
    assert seen[1][1] is False
    assert protected_app_state.grants_imported()


@pytest.mark.parametrize("legacy_text", [None, "not json", "null", "[]"])
def test_a_lost_or_damaged_legacy_set_imports_as_nothing_in_every_family(
    tmp_path, app_home, legacy_text
):
    _legacy_home(tmp_path, stored=True, legacy=None)
    if legacy_text is not None:
        (app_dir(APP) / APPROVED_GRANTS_FILENAME).write_text(legacy_text, encoding="utf-8")

    assert _granted() == []
    stored = json.loads(protected_app_state.grants_path(APP).read_text(encoding="utf-8"))
    # A family left out would approve what the manifest declares.
    assert stored == {family: [] for family in manager_mod.STAGED_GRANT_FAMILIES}


def test_a_removal_that_stops_halfway_puts_the_records_back(tmp_path, app_home, monkeypatch):
    _install(tmp_path)
    assert disable_app(APP).ok
    before = protected_app_state.grants_path(APP).read_text(encoding="utf-8")

    def _forget_grants_then_fail(name):
        protected_app_state.grants_path(name).unlink()
        raise PermissionError("the removal stopped halfway")

    monkeypatch.setattr(protected_app_state, "forget_grants", _forget_grants_then_fail)
    assert not uninstall_app(APP, keep_data=False).ok

    assert protected_app_state.grants_path(APP).read_text(encoding="utf-8") == before
    assert protected_app_state.owner_disabled(APP)


def test_a_disable_during_a_failed_uninstall_keeps_its_record(tmp_path, app_home, monkeypatch):
    import threading

    from kiro_crew.apps import manager_records
    from kiro_crew.platform_lock_compat import release_lock, try_acquire_lock

    _install(tmp_path)
    data = app_dir(APP) / "data"
    if data.exists():
        data.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    data.symlink_to(elsewhere, target_is_directory=True)

    reached = threading.Event()
    real_hold = manager_records.hold_app_record_lock
    verdicts: list[bool] = []
    disabler = threading.Thread(target=lambda: verdicts.append(disable_app(APP).ok))

    def _hold(name):
        if threading.current_thread() is disabler:
            reached.set()
        real_hold(name)

    monkeypatch.setattr(manager_mod, "hold_app_record_lock", _hold)
    lock_was_held: list[bool] = []
    real_forget = protected_app_state.forget_grants

    def _forget_then_disable(name):
        real_forget(name)
        disabler.start()
        assert reached.wait(30)
        # The disable now waits on the record lock the uninstall holds.
        fd = os.open(manager_records._record_lock_path(APP), os.O_RDWR)
        try:
            got = try_acquire_lock(fd, exclusive=True)
            if got:
                release_lock(fd)
        finally:
            os.close(fd)
        lock_was_held.append(not got)

    monkeypatch.setattr(protected_app_state, "forget_grants", _forget_then_disable)
    assert not uninstall_app(APP, keep_data=True).ok
    disabler.join(30)

    assert lock_was_held == [True]
    assert verdicts == [True]
    assert protected_app_state.owner_disabled(APP)
    _edit_record(lambda data: data.update(enabled=True))
    assert is_app_enabled(APP) is False


def test_the_import_keeps_a_disable_made_before_the_upgrade(tmp_path, app_home):
    _legacy_home(tmp_path, stored=True, legacy={"api": [OLD_API], "events": []})
    # An older build kept the disable in installed.json alone.
    _edit_record(lambda data: data.update(enabled=False))
    protected_app_state.set_owner_disabled(APP, False)

    _granted()  # the first grant read runs the one-time import
    _edit_record(lambda data: data.update(enabled=True))

    assert protected_app_state.owner_disabled(APP)
    assert is_app_enabled(APP) is False


def test_the_import_leaves_an_enabled_app_on(tmp_path, app_home):
    _legacy_home(tmp_path, stored=True, legacy={"api": [OLD_API], "events": []})

    _granted()

    assert not protected_app_state.owner_disabled(APP)
    assert is_app_enabled(APP) is True


@pytest.mark.parametrize(
    ("name", "record", "expected"),
    [
        ("command-bar", {"enabled": False, "source": "builtin"}, False),
        ("command-bar", {"enabled": False, "source": "builtin", "defaultOnBackfilled": True}, True),
        ("command-bar", {"enabled": False, "source": "registry:x"}, True),
        (APP, "not json", True),
    ],
)
def test_which_records_the_import_marks_disabled(tmp_path, name, record, expected):
    path = tmp_path / INSTALLED_META_FILENAME
    path.write_text(record if isinstance(record, str) else json.dumps(record), encoding="utf-8")

    assert manager_mod._disabled_before_upgrade(name, path) is expected


def test_the_disable_stands_until_the_uninstall_commits(tmp_path, app_home, monkeypatch):
    _install(tmp_path)
    assert disable_app(APP).ok
    seen: list[bool] = []
    real_forget = protected_app_state.forget_grants

    def _forget_then_look(name):
        real_forget(name)
        # The tree is still on disk: a rewritten record must not turn it on.
        _edit_record(lambda data: data.update(enabled=True))
        seen.append(is_app_enabled(APP))

    monkeypatch.setattr(protected_app_state, "forget_grants", _forget_then_look)
    assert uninstall_app(APP, keep_data=False).ok

    assert seen == [False]
    assert not protected_app_state.owner_disabled(APP)


def test_an_enable_finishes_the_import_before_it_clears_the_disable(tmp_path, app_home):
    _legacy_home(tmp_path, stored=True, legacy={"api": [OLD_API], "events": []})
    _edit_record(lambda data: data.update(enabled=False))
    assert not protected_app_state.grants_imported()

    assert enable_app(APP).ok
    # The enable ran the import itself, so no importer that read the old record
    # is left to put the disable back after it.
    assert protected_app_state.grants_imported()
    _granted()
    assert not protected_app_state.owner_disabled(APP)
    assert is_app_enabled(APP) is True


def test_an_update_with_an_unreadable_set_fails_cleanly(tmp_path, app_home):
    _install(tmp_path)
    protected_app_state.grants_path(APP).write_bytes(b"\xff\xfe not utf-8")

    result = update_app(_source(tmp_path, "v2", [OLD_API], version="2.0.0"))

    assert not result.ok and "approved permissions" in result.error
    # Nothing changed and no update marker is left behind.
    assert json.loads((app_dir(APP) / APP_MANIFEST_FILENAME).read_text())["version"] == "1.0.0"
    assert not manager_mod._update_in_progress(APP)


def test_a_reregistration_with_an_unreadable_set_fails_cleanly(app_home):
    from kiro_crew.apps.manager import register_external_app

    assert register_external_app(APP, version="1.0.0", display_name="Guarded").ok
    protected_app_state.grants_path(APP).write_bytes(b"\xff\xfe not utf-8")

    result = register_external_app(APP, version="2.0.0", display_name="Guarded")

    assert not result.ok and "approved permissions" in result.error


def test_an_uninstall_with_an_unreadable_set_keeps_every_grant(tmp_path, app_home, monkeypatch):
    from kiro_crew.apps import manager_uninstall

    _install(tmp_path)
    protected_app_state.grants_path(APP).write_bytes(b"\xff\xfe not utf-8")
    dropped: list[str] = []
    monkeypatch.setattr(manager_uninstall, "_drop_trust_grant", dropped.append)

    result = uninstall_app(APP, keep_data=False)

    assert not result.ok and "approved permissions" in result.error
    assert dropped == []  # the execution grant was never withdrawn
    assert (app_dir(APP) / INSTALLED_META_FILENAME).is_file()
