"""The owner's ``dashboard_proxy`` grant: never pre-granted, registry-verified, per install.

The grant is a gateway-only record under ``member-records/`` (sandbox-masked, never under
the agent-writable ``apps/<app>/``). It names the install it was given for, so a reinstall
or another package under the same name holds none. It counts only while the owner-enable
marker stands. Secret-shaped values here are described in words.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps import dashboard_proxy
from kiro_crew.apps import dashboard_proxy_routes as routes

_APP = "tunnel-like"


@pytest.fixture
def records(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(
        "kiro_crew.member_records.records_dir", lambda kind: home / "member-records" / kind
    )
    return home


@pytest.fixture
def installed(monkeypatch):
    record = SimpleNamespace(
        installedAt="2026-11-22T10:00:00Z",
        version="0.9.4",
        origin="registry",
        sourceRegistry="crew-tunnels",
        sourceUrl="https://example.invalid/tunnel.git",
        sourceCommit="abc",
        sourceSigner="",
    )
    monkeypatch.setattr(dashboard_proxy, "_installed", lambda _name: record)
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (True, True))
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: True)
    return record


def test_no_grant_or_offer_on_a_backend_that_cannot_bind_a_stand_in(
    records, installed, monkeypatch
):
    """Seatbelt and every other non-namespace backend: refuse, and keep every mask."""
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: False)
    assert dashboard_proxy.grant(_APP) == (False, "platform-unsupported")
    assert dashboard_proxy.prompt_text(_APP) is None
    assert dashboard_proxy.granted(_APP) is False


def test_a_granted_app_stays_masked_when_the_backend_cannot_bind_a_stand_in(
    records, installed, monkeypatch
):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.grant(_APP) == (True, "")
    masks = (str(records / ".local_secret"),)
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: False)
    assert dashboard_proxy.backend_masks(_APP, masks) == (masks, ())


def test_an_installed_app_is_never_pre_granted(records, installed):
    assert dashboard_proxy.granted(_APP) is False


def test_the_grant_is_recorded_for_this_install_only(records, installed):
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.granted(_APP) is True
    stored = json.loads(
        (records / "member-records/app-dashboard-proxy" / f"{_APP}.json").read_text()
    )
    assert stored["app"] == _APP and "identity" in stored
    installed.installedAt = "2026-11-22T11:00:00Z"  # a reinstall
    assert dashboard_proxy.granted(_APP) is False
    installed.installedAt = "2026-11-22T10:00:00Z"
    installed.sourceUrl = "https://example.invalid/another-package.git"  # another package
    assert dashboard_proxy.granted(_APP) is False


def test_only_a_registry_install_can_be_offered_the_grant(records, installed):
    installed.origin = "local"
    assert dashboard_proxy.prompt_text(_APP) is None
    assert dashboard_proxy.grant(_APP) == (False, "not-registry-verified")
    assert dashboard_proxy.granted(_APP) is False


def test_the_offer_names_publisher_and_package_from_the_install_record(records, installed):
    offer = dashboard_proxy.prompt_text(_APP)
    assert offer is not None
    assert offer["registry"] == "crew-tunnels"
    assert offer["source"] == "https://example.invalid/tunnel.git"
    for words in (
        "tunnel-like",
        "0.9.4",
        "crew-tunnels",
        dashboard_proxy.DASHBOARD_ACCESS_SCOPE,
        "show the dashboard page with your theme and a list of your chats",
        "stop a running chat turn",
        "cannot read chat messages, send them, read your preferences, change settings, credentials, apps",
    ):
        assert words in offer["text"]
    assert "read your dashboard and chats" not in offer["text"]
    assert "run agents as you" not in offer["text"]
    assert "full dashboard control" not in offer["text"]


def test_the_grant_counts_only_while_the_enable_marker_stands(records, installed, monkeypatch):
    dashboard_proxy.grant(_APP)
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (False, True))
    assert dashboard_proxy.granted(_APP) is False
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (True, False))
    assert dashboard_proxy.granted(_APP) is False


def test_no_grant_without_the_enable_marker(records, installed, monkeypatch):
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (False, True))
    assert dashboard_proxy.grant(_APP) == (False, "not-enabled")


def test_revoke_removes_the_record_and_the_stand_in_source(records, installed):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    dashboard_proxy.grant(_APP)
    pairs = dashboard_proxy.stand_in_pairs(_APP, (str(records / ".local_secret"),))
    assert pairs
    dashboard_proxy.revoke(_APP)
    assert not list((records / "member-records/app-dashboard-proxy").iterdir())


@pytest.fixture
def forged_apps(records, tmp_path, monkeypatch):
    """An app tree whose ``installed.json`` claims a registry install the gateway never made."""
    apps = tmp_path / "apps"
    (apps / _APP).mkdir(parents=True)
    forged = {
        "name": _APP,
        "version": "9.9.9",
        "installedAt": "2026-11-22T09:00:00Z",
        "origin": "registry",
        "source": f"registry:{_APP}",
        "sourceUrl": "https://example.invalid/forged.git",
        "sourceRegistry": "crew-tunnels",
        "sourceSigner": "someone",
    }
    (apps / _APP / "installed.json").write_text(json.dumps(forged), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.apps.manager.apps_dir", lambda: apps)
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (True, True))
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: True)
    return apps


def test_a_forged_installed_json_does_not_make_an_app_grantable(forged_apps):
    assert dashboard_proxy.registry_verified(_APP) is False
    assert dashboard_proxy.prompt_text(_APP) is None
    assert dashboard_proxy.grant(_APP) == (False, "not-registry-verified")
    assert dashboard_proxy.granted(_APP) is False


def test_the_gateway_install_record_is_what_makes_an_app_grantable(forged_apps):
    dashboard_proxy.record_install(
        _APP,
        version="0.9.4",
        registry="crew-tunnels",
        url="https://example.invalid/tunnel.git",
        commit="abc",
        signer="",
    )
    assert dashboard_proxy.registry_verified(_APP) is True
    offer = dashboard_proxy.prompt_text(_APP)
    assert offer is not None and offer["source"] == "https://example.invalid/tunnel.git"
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.granted(_APP) is True


def test_a_forged_installed_json_cannot_change_a_recorded_install(forged_apps):
    dashboard_proxy.record_install(
        _APP, version="0.9.4", registry="r", url="https://example.invalid/a.git"
    )
    assert dashboard_proxy.grant(_APP) == (True, "")
    (forged_apps / _APP / "installed.json").write_text(
        json.dumps({"name": _APP, "version": "6.6.6", "origin": "registry"}), encoding="utf-8"
    )
    assert dashboard_proxy.granted(_APP) is True
    assert dashboard_proxy.prompt_text(_APP)["version"] == "0.9.4"  # type: ignore[index]


def test_a_new_install_record_ends_the_grant_and_forget_removes_the_record(forged_apps):
    dashboard_proxy.record_install(_APP, version="1", registry="r", url="https://x.invalid/a.git")
    assert dashboard_proxy.grant(_APP) == (True, "")
    dashboard_proxy.record_install(_APP, version="1", registry="r", url="https://x.invalid/a.git")
    assert dashboard_proxy.granted(_APP) is False, "a reinstall is another install"
    dashboard_proxy.forget_install(_APP)
    assert dashboard_proxy.registry_verified(_APP) is False


def test_each_grant_gets_its_own_id_and_revoke_ends_it(records, installed):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.grant(_APP) == (True, "")
    first = dashboard_proxy.grant_claim(_APP)
    assert first and dashboard_proxy.grant_current(_APP, first)
    dashboard_proxy.revoke(_APP)
    assert dashboard_proxy.grant_claim(_APP) == ""
    assert not dashboard_proxy.grant_current(_APP, first)
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.grant_claim(_APP) != first
    assert not dashboard_proxy.grant_current(_APP, first)
    assert not dashboard_proxy.grant_current(_APP, "")


def _unlink_refused(monkeypatch, error: type[OSError] = PermissionError) -> None:
    """Every ``Path.unlink`` fails, as on a read-only or locked records directory."""

    def _boom(self, *args, **kwargs):
        raise error("unlink refused")

    monkeypatch.setattr(Path, "unlink", _boom)


def _write_refused(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise OSError("write refused")

    monkeypatch.setattr(dashboard_proxy, "atomic_write", _boom)


def _tombstone_write_refused(monkeypatch) -> None:
    """Only a write into the tombstone leaf fails; a grant record still lands."""
    real = dashboard_proxy.atomic_write

    def _selective(path, *args, **kwargs):
        if Path(path).parent.name == dashboard_proxy._TOMBSTONE_LEAF:
            raise OSError("tombstone write refused")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(dashboard_proxy, "atomic_write", _selective)


def _nothing_lands(monkeypatch, error: type[OSError] = PermissionError) -> None:
    """The records directory and the tombstone leaf both refuse: neither a delete nor a write lands."""
    _unlink_refused(monkeypatch, error)
    _write_refused(monkeypatch)


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_a_revoke_that_cannot_remove_the_record_still_ends_the_grant(
    records, installed, monkeypatch, error
):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.grant(_APP) == (True, "")
    claim = dashboard_proxy.grant_claim(_APP)
    assert claim and dashboard_proxy.stand_in_value(_APP)
    _nothing_lands(monkeypatch, error)
    assert dashboard_proxy.revoke(_APP) is False, "the caller must hear nothing landed"
    assert dashboard_proxy.granted(_APP) is False
    assert dashboard_proxy.grant_claim(_APP) == ""
    assert not dashboard_proxy.grant_current(_APP, claim)
    assert dashboard_proxy.stand_in_value(_APP) is None
    assert dashboard_proxy.verify_leg_two(_APP, "any-value") == "not-granted"
    assert dashboard_proxy.backend_masks(_APP, ("/m",)) == (("/m",), ())


def test_a_new_grant_follows_a_revoke_that_could_not_remove_the_record(
    records, installed, monkeypatch
):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.grant(_APP) == (True, "")
    first = dashboard_proxy.grant_claim(_APP)
    with monkeypatch.context() as refused:
        _nothing_lands(refused)
        assert dashboard_proxy.revoke(_APP) is False
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.granted(_APP) is True
    assert dashboard_proxy.grant_claim(_APP) not in ("", first)
    assert not dashboard_proxy.grant_current(_APP, first)


@pytest.mark.asyncio
async def test_the_revoke_route_answers_an_error_when_the_record_stays(
    records, installed, monkeypatch
):
    async def _owner(_request, _operation):
        return None

    monkeypatch.setattr(routes, "_owner_denial", _owner)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app", lambda _name: object())
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.grant(_APP) == (True, "")
    _tombstone_write_refused(monkeypatch)  # K1: the tombstone cannot be written
    resp = await routes.handle_dashboard_proxy(_request("DELETE"))
    assert resp.status == 500
    body = json.loads(resp.text or "{}")
    assert body["code"] == "revoke_incomplete"
    assert dashboard_proxy.granted(_APP) is False, "access is ended in-process"
    # The text says what is true: over now; back after a restart only if the record stayed.
    text = body["error"]
    assert text == routes.REVOKE_INCOMPLETE_TEXT
    for claim in (
        "Access is ended now",
        "comes back after a gateway restart",
        "until a revoke succeeds",
        "Fix the disk error, then revoke again",
    ):
        assert claim in text
    assert "a stored record could not be removed" not in text, "the old text named only the record"


def _tombstone_dir(records: Path) -> Path:
    return records / "app-dashboard-revoked"


def test_a_revoke_that_cannot_delete_stays_revoked_after_a_restart(records, installed, monkeypatch):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.grant(_APP) == (True, "")
    claim = dashboard_proxy.grant_claim(_APP)
    stored_id = json.loads(
        (records / "member-records/app-dashboard-proxy" / f"{_APP}.json").read_text()
    )["grant_id"]
    _unlink_refused(monkeypatch)  # the delete fails; the tombstone write lands
    assert dashboard_proxy.revoke(_APP) is True, "the tombstone landed, so access is over for good"
    tomb = json.loads((_tombstone_dir(records) / f"{_APP}.grant").read_text())
    assert tomb["active"] is True and stored_id in tomb["ended_ids"], "it names the ended grant"
    dashboard_proxy.reset_marks()  # the gateway restarts: no in-memory mark survives
    assert (records / "member-records/app-dashboard-proxy" / f"{_APP}.json").exists()
    assert dashboard_proxy.granted(_APP) is False
    assert dashboard_proxy.grant_claim(_APP) == ""
    assert not dashboard_proxy.grant_current(_APP, claim)
    assert dashboard_proxy.stand_in_value(_APP) is None
    assert dashboard_proxy.verify_leg_two(_APP, "any-value") == "not-granted"


def test_a_revoke_whose_tombstone_cannot_be_written_refuses_the_grant_in_process(
    records, installed, monkeypatch
):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.grant(_APP) == (True, "")
    _tombstone_write_refused(monkeypatch)
    assert dashboard_proxy.revoke(_APP) is False, "K1: no tombstone, so the caller hears 500"
    assert not list(_tombstone_dir(records).glob("*"))
    assert dashboard_proxy.granted(_APP) is False, "the in-memory mark still holds"
    assert dashboard_proxy.grant_claim(_APP) == ""


def test_the_tombstone_and_its_directory_are_fsynced_before_any_record_is_unlinked(
    records, installed, monkeypatch
):
    assert dashboard_proxy.grant(_APP) == (True, "")
    events: list[str] = []
    real_unlink = Path.unlink
    real_write = dashboard_proxy.atomic_write

    def _write(path, *args, **kwargs):
        if Path(path).parent.name == dashboard_proxy._TOMBSTONE_LEAF:
            assert kwargs.get("fsync") is True
            events.append("tombstone-write")
        return real_write(path, *args, **kwargs)

    def _fsync_dir(path, *args, **kwargs):
        events.append(f"fsync-dir:{Path(path).name}")

    def _unlink(self, *args, **kwargs):
        events.append("unlink")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(dashboard_proxy, "atomic_write", _write)
    monkeypatch.setattr(dashboard_proxy, "fsync_dir", _fsync_dir)
    monkeypatch.setattr(Path, "unlink", _unlink)
    assert dashboard_proxy.revoke(_APP) is True
    first_unlink = events.index("unlink")
    assert events[:first_unlink] == [
        "tombstone-write",
        f"fsync-dir:{dashboard_proxy._TOMBSTONE_LEAF}",
        f"fsync-dir:{records.name}",
    ]


def test_a_directory_fsync_failure_counts_as_no_tombstone(records, installed, monkeypatch):
    assert dashboard_proxy.grant(_APP) == (True, "")

    def _boom(*args, **kwargs):
        raise OSError("fsync refused")

    monkeypatch.setattr(dashboard_proxy, "fsync_dir", _boom)
    assert dashboard_proxy.revoke(_APP) is False
    assert dashboard_proxy.granted(_APP) is False


@pytest.mark.parametrize(
    "content",
    ["", "{not json", "[]", '{"app": "someone-else", "active": false}', "\xff\xfe"],
    ids=["empty", "not-json", "not-an-object", "other-app", "not-utf8"],
)
def test_a_corrupt_tombstone_counts_as_ended_never_as_absent(records, installed, content):
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.granted(_APP) is True
    leaf = _tombstone_dir(records)
    leaf.mkdir()
    (leaf / f"{_APP}.grant").write_bytes(content.encode("latin-1"))
    assert dashboard_proxy.granted(_APP) is False
    dashboard_proxy.reset_marks()
    assert dashboard_proxy.granted(_APP) is False
    assert dashboard_proxy.verify_leg_two(_APP, "any-value") == "not-granted"


def test_an_unreadable_tombstone_counts_as_ended(records, installed):
    assert dashboard_proxy.grant(_APP) == (True, "")
    (_tombstone_dir(records) / f"{_APP}.grant").mkdir(parents=True)  # a directory: unreadable
    assert dashboard_proxy.granted(_APP) is False


def test_a_fresh_grant_clears_the_tombstone(records, installed, monkeypatch):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.revoke(_APP) is True
    tombstone = _tombstone_dir(records) / f"{_APP}.grant"
    assert json.loads(tombstone.read_text())["active"] is True
    dashboard_proxy.reset_marks()
    assert dashboard_proxy.granted(_APP) is False
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert json.loads(tombstone.read_text())["active"] is False, "cleared by the new grant"
    assert dashboard_proxy.granted(_APP) is True
    dashboard_proxy.reset_marks()
    assert dashboard_proxy.granted(_APP) is True, "nothing stands against the new grant"


def test_an_old_record_with_an_ended_grant_id_never_stands_again(records, installed, monkeypatch):
    record = records / "member-records/app-dashboard-proxy" / f"{_APP}.json"
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.grant(_APP) == (True, "")
    old_bytes = record.read_bytes()
    with monkeypatch.context() as refused:
        _unlink_refused(refused)  # the record stays on disk
        assert dashboard_proxy.revoke(_APP) is True
    assert dashboard_proxy.grant(_APP) == (True, "")  # a new grant id, tombstone cleared
    new_bytes = record.read_bytes()
    assert new_bytes != old_bytes
    record.write_bytes(old_bytes)  # the old record comes back (a restore, a copy)
    dashboard_proxy.reset_marks()
    assert dashboard_proxy.granted(_APP) is False, "its grant id was ended"
    assert dashboard_proxy.grant_claim(_APP) == ""
    record.write_bytes(new_bytes)
    assert dashboard_proxy.granted(_APP) is True


def test_a_grant_whose_tombstone_cannot_be_cleared_is_refused_and_stays_off(
    records, installed, monkeypatch
):
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.revoke(_APP) is True
    _tombstone_write_refused(monkeypatch)
    assert dashboard_proxy.grant(_APP) == (False, "write-failed")
    assert dashboard_proxy.granted(_APP) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits do not exist on Windows")
def test_the_tombstone_leaf_is_private_to_the_owner_user(records, installed):
    """A first revoke on a fresh home makes the leaf 0o700, as ``sandbox_precreate`` does."""
    import os
    import stat

    leaf = records.resolve() / dashboard_proxy._TOMBSTONE_LEAF
    old = os.umask(0o022)  # the gateway may run with a looser umask than a worker shell
    try:
        assert dashboard_proxy.revoke(_APP) is True
        assert stat.S_IMODE(leaf.stat().st_mode) == 0o700
        leaf.chmod(0o755)  # a leaf an older gateway made wider
        assert dashboard_proxy.revoke(_APP) is True
        assert stat.S_IMODE(leaf.stat().st_mode) == 0o700
    finally:
        os.umask(old)


def test_the_tombstone_is_in_a_leaf_the_sandbox_hides_and_no_app_can_write(records, installed):
    from kiro_crew import member_records, sandbox_leaves, sandbox_precreate
    from kiro_crew.security import paths_lists

    leaf = dashboard_proxy._TOMBSTONE_LEAF
    path = dashboard_proxy._tombstone_path(_APP, ".grant")
    home = records.resolve()
    assert path.parent == home / leaf and path.parent.parent == home
    # Not in member-records/, and not in the agent-writable app tree.
    assert member_records.records_root() not in path.parents
    assert home / "apps" not in path.parents
    # The leaf is masked in every sandbox mode, pre-created, and refused to the agent tools.
    assert leaf in sandbox_leaves._CREW_HIDDEN_LEAVES
    assert leaf in sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert leaf in paths_lists._CREW_SECRET_LEAVES


def test_an_uninstall_tombstone_outlives_a_restart_and_a_new_sealed_record_clears_it(
    forged_apps, monkeypatch
):
    dashboard_proxy.record_install(_APP, version="1", registry="r", url="https://x.invalid/a.git")
    with monkeypatch.context() as refused:
        _unlink_refused(refused)
        dashboard_proxy.forget_install(_APP)
    dashboard_proxy.reset_marks()
    assert dashboard_proxy.registry_verified(_APP) is False, "the install tombstone stands"
    assert dashboard_proxy.record_install(
        _APP, version="2", registry="r", url="https://x.invalid/a.git"
    )
    assert dashboard_proxy.registry_verified(_APP) is True


def test_an_uninstall_that_cannot_remove_the_install_record_leaves_no_grant_or_offer(
    forged_apps, monkeypatch
):
    dashboard_proxy.record_install(_APP, version="1", registry="r", url="https://x.invalid/a.git")
    assert dashboard_proxy.grant(_APP) == (True, "")
    _unlink_refused(monkeypatch)
    dashboard_proxy.forget_install(_APP)
    assert dashboard_proxy.granted(_APP) is False
    assert dashboard_proxy.registry_verified(_APP) is False
    assert dashboard_proxy.prompt_text(_APP) is None


def test_an_update_whose_install_seal_fails_leaves_no_grant_and_no_offer(forged_apps, monkeypatch):
    dashboard_proxy.record_install(_APP, version="1", registry="r", url="https://x.invalid/a.git")
    assert dashboard_proxy.grant(_APP) == (True, "")
    _write_refused(monkeypatch)
    sealed = dashboard_proxy.record_install(
        _APP, version="2", registry="r", url="https://x.invalid/a.git"
    )
    assert sealed is False
    assert dashboard_proxy.granted(_APP) is False, "the old install's grant must not stand"
    assert dashboard_proxy.registry_verified(_APP) is False
    assert dashboard_proxy.prompt_text(_APP) is None, "no offer without a sealed record"


def test_the_next_sealed_install_record_clears_the_failure(forged_apps, monkeypatch):
    with monkeypatch.context() as refused:
        _write_refused(refused)
        assert dashboard_proxy.record_install(_APP, version="2", registry="r", url="u") is False
    assert dashboard_proxy.record_install(_APP, version="2", registry="r", url="https://x/a.git")
    assert dashboard_proxy.registry_verified(_APP) is True
    assert dashboard_proxy.prompt_text(_APP) is not None


def test_set_app_provenance_reports_a_failed_install_seal(forged_apps, monkeypatch):
    """The App Store update path hears the failure; it is not logged and dropped."""
    from dataclasses import dataclass

    from kiro_crew.apps import manager as manager_mod

    @dataclass
    class _Meta:
        version: str = "2"
        source: str = ""
        sourceUrl: str = ""
        sourceRegistry: str = ""
        sourceCommit: str = ""
        sourceSigner: str = ""

    monkeypatch.setattr(manager_mod, "hold_app_record_lock", lambda _name: None)
    monkeypatch.setattr(manager_mod, "_read_installed", lambda _name: _Meta())
    monkeypatch.setattr(manager_mod, "_write_installed", lambda _name, _meta: None)
    _write_refused(monkeypatch)
    with pytest.raises(RuntimeError, match="install record"):
        manager_mod.set_app_provenance(_APP, source="registry:x", url="u", registry="r")


def test_process_state_leaks_in(monkeypatch):
    """Sets the module state the next test must not see (pairs with the one below)."""
    monkeypatch.setattr(dashboard_proxy, "granted", lambda _name: False)
    dashboard_proxy.set_gateway_secret("a-secret-an-earlier-test-left-behind")
    dashboard_proxy.note_refused_mint(_APP)
    assert dashboard_proxy._secret()


def test_process_state_is_reset_between_tests():
    assert dashboard_proxy._secret() == ""
    assert not dashboard_proxy._refused_mints


def _request(method: str, body: dict | None = None):
    async def _json():
        return body

    return SimpleNamespace(
        method=method,
        match_info={"name": _APP},
        get=lambda _k, default="": default,
        json=_json,
    )


@pytest.mark.asyncio
async def test_a_grant_needs_the_owner_and_the_confirm(records, installed, monkeypatch):
    async def _deny(_request, _operation):
        return routes.web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(routes, "_owner_denial", _deny)
    assert (await routes.handle_dashboard_proxy(_request("PUT", {"confirm": True}))).status == 403
    assert dashboard_proxy.granted(_APP) is False


@pytest.mark.asyncio
async def test_a_grant_without_confirm_is_refused(records, installed, monkeypatch):
    async def _owner(_request, _operation):
        return None

    monkeypatch.setattr(routes, "_owner_denial", _owner)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app", lambda _name: object())
    resp = await routes.handle_dashboard_proxy(_request("PUT", {}))
    assert resp.status == 400
    assert dashboard_proxy.granted(_APP) is False
    resp = await routes.handle_dashboard_proxy(_request("PUT", {"confirm": True}))
    assert resp.status == 200
    assert dashboard_proxy.granted(_APP) is True


@pytest.mark.asyncio
async def test_the_grant_route_grants_on_a_cold_probe_cache(records, installed, monkeypatch):
    """The route runs the grant off the loop, where the backend probe can finish.

    On the event loop a cold probe cache answers "none"; a refactor that moved ``grant`` onto
    the loop would refuse every first grant with ``platform-unsupported``.
    """
    import asyncio

    def _cold_cache(config_mode="auto"):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return "namespace"
        return "none"

    from kiro_crew import sandbox

    async def _owner(_request, _operation):
        return None

    monkeypatch.setattr(routes, "_owner_denial", _owner)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app", lambda _name: object())
    monkeypatch.setattr(sandbox, "detect_backend", _cold_cache)
    # The fixtures stand in for the platform check; here it asks the probe, as in production.
    monkeypatch.setattr(
        dashboard_proxy,
        "_namespace_backend",
        lambda: sandbox.detect_backend(config_mode="standard") == "namespace",
    )
    resp = await routes.handle_dashboard_proxy(_request("PUT", {"confirm": True}))
    assert resp.status == 200
    assert dashboard_proxy.granted(_APP) is True


def test_a_refused_mint_marks_the_app_until_it_holds_the_grant(records, installed):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.needs_access(_APP) is False
    dashboard_proxy.note_refused_mint(_APP)
    assert dashboard_proxy.needs_access(_APP) is True
    assert routes._view(_APP)["needs_access"] is True
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.needs_access(_APP) is False
    assert routes._view(_APP)["needs_access"] is False


def test_the_view_says_when_the_platform_cannot_bind_a_stand_in(records, installed, monkeypatch):
    assert routes._view(_APP)["platform_supported"] is True
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: False)
    view = routes._view(_APP)
    assert view["platform_supported"] is False
    assert view["offer"] is None


@pytest.mark.asyncio
async def test_the_restart_needs_the_owner_and_a_grant_then_restarts_the_backend(
    records, installed, monkeypatch
):
    calls: list[str] = []

    async def _owner(_request, _operation):
        return None

    def _stop(_name):
        calls.append("stop")
        return True

    def _start(_name):
        calls.append("start")
        return object()

    monkeypatch.setattr(routes, "_owner_denial", _owner)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app", lambda _name: object())
    monkeypatch.setattr("kiro_crew.apps.backend.stop_app_backend", _stop)
    monkeypatch.setattr("kiro_crew.apps.backend.start_app_backend", _start)
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    resp = await routes.handle_dashboard_proxy_restart(_request("POST", None))
    assert resp.status == 409
    assert calls == []
    assert dashboard_proxy.grant(_APP) == (True, "")
    resp = await routes.handle_dashboard_proxy_restart(_request("POST", None))
    assert resp.status == 200
    assert json.loads(resp.text or "{}")["restarted"] is True
    assert calls == ["stop", "start"]


def test_a_pre_upgrade_app_mints_nothing_and_gets_no_offer_until_one_owner_update(forged_apps):
    """An app installed before the sealed record existed keeps every mask and is offered nothing.

    ``installed.json`` (forged here) is app-writable, so it never backfills the record. The
    one way to a record is the install path the App Store update runs (``record_install``).
    """
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    view = routes._view(_APP)
    assert view["offer"] is None and view["granted"] is False
    assert view["update_to_enable"] is True, "the card must offer the owner the update"
    assert dashboard_proxy.grant(_APP) == (False, "not-registry-verified")
    assert dashboard_proxy.stand_in_value(_APP) is None
    assert dashboard_proxy.verify_leg_two(_APP, "any-value") == "not-granted"
    masks = ("/gateway/.local_secret",)
    assert dashboard_proxy.backend_masks(_APP, masks) == (masks, ())
    # One owner update: the same path the App Store runs seals the record.
    dashboard_proxy.record_install(
        _APP, version="0.9.5", registry="crew-tunnels", url="https://example.invalid/tunnel.git"
    )
    view = routes._view(_APP)
    assert view["offer"] is not None
    assert view["update_to_enable"] is False
    assert view["granted"] is False, "an update alone grants nothing"
    assert dashboard_proxy.stand_in_value(_APP) is None


def test_the_update_action_is_not_shown_where_no_grant_could_follow(forged_apps, monkeypatch):
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: False)
    assert routes._view(_APP)["update_to_enable"] is False


def _provenance(monkeypatch, **kwargs):
    """Run the gateway's own install bookkeeping (``set_app_provenance``) for the app."""
    from dataclasses import dataclass

    from kiro_crew.apps import manager as manager_mod

    @dataclass
    class _Meta:
        version: str = "0.9.4"
        source: str = ""
        sourceUrl: str = ""
        sourceRegistry: str = ""
        sourceCommit: str = ""
        sourceSigner: str = ""

    monkeypatch.setattr(manager_mod, "hold_app_record_lock", lambda _name: None)
    monkeypatch.setattr(manager_mod, "_read_installed", lambda _name: _Meta())
    monkeypatch.setattr(manager_mod, "_write_installed", lambda _name, _meta: None)
    return manager_mod.set_app_provenance(_APP, source=f"registry:{_APP}", **kwargs)


def test_an_unsigned_official_catalog_install_is_offered_and_grantable(forged_apps, monkeypatch):
    """The bundled catalog seals an empty registry and no signer; that is still a registry."""
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert routes._view(_APP)["update_to_enable"] is True
    assert _provenance(monkeypatch, url="https://example.invalid/official.git", registry="")
    view = routes._view(_APP)
    assert view["update_to_enable"] is False, "the update hint clears once the record is sealed"
    assert view["offer"] is not None
    offer = view["offer"]
    assert offer["registry"] == ""  # type: ignore[index]
    assert "from the bundled catalog" in offer["text"]  # type: ignore[index]
    assert 'registry "the bundled catalog"' not in offer["text"]  # type: ignore[index]
    assert "registry " not in offer["text"]  # type: ignore[index]
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.granted(_APP) is True


def test_the_official_catalog_marker_is_sealed_only_by_the_install_path(forged_apps, monkeypatch):
    """A marker from the manifest, ``installed.json`` or a bare record is never read."""
    forged = json.loads((forged_apps / _APP / "installed.json").read_text(encoding="utf-8"))
    forged.update(sourceRegistry="", sourceSigner="", catalog="official-catalog")
    (forged_apps / _APP / "installed.json").write_text(json.dumps(forged), encoding="utf-8")
    (forged_apps / _APP / "manifest.json").write_text(
        json.dumps({"name": _APP, "catalog": "official-catalog", "officialCatalog": True}),
        encoding="utf-8",
    )
    assert dashboard_proxy.registry_verified(_APP) is False
    assert routes._view(_APP)["update_to_enable"] is True
    assert dashboard_proxy.grant(_APP) == (False, "not-registry-verified")
    # A sealed record made without the install path's marker stays unverified too.
    dashboard_proxy.record_install(_APP, version="1", registry="", url="https://x.invalid/a.git")
    assert dashboard_proxy.registry_verified(_APP) is False
    assert dashboard_proxy.prompt_text(_APP) is None


@pytest.mark.parametrize("tag", ["crew-tunnels", "owner-designated-index", "https://idx.invalid/r"])
def test_an_entry_with_a_registry_tag_never_gets_the_official_marker(forged_apps, monkeypatch, tag):
    """Any external index (``_registry``), the owner-designated one included, seals no marker."""
    assert _provenance(monkeypatch, url="https://example.invalid/t.git", registry=tag)
    sealed = json.loads(dashboard_proxy._path(_APP, ".install.json").read_text(encoding="utf-8"))
    assert sealed["sourceRegistry"] == tag
    assert sealed["catalog"] == ""
    assert dashboard_proxy.registry_verified(_APP) is True  # admitted by the registry id alone


def test_a_third_party_registry_row_is_unchanged_by_the_official_marker(forged_apps, monkeypatch):
    assert _provenance(monkeypatch, url="https://example.invalid/t.git", registry="crew-tunnels")
    assert dashboard_proxy.registry_verified(_APP) is True
    assert dashboard_proxy.prompt_text(_APP)["registry"] == "crew-tunnels"  # type: ignore[index]
    assert getattr(dashboard_proxy._installed(_APP), "catalog", "") == ""
    # No source url: not a registry install at all, marker or not.
    assert _provenance(monkeypatch, url="", registry="") is True
    assert dashboard_proxy.registry_verified(_APP) is False
