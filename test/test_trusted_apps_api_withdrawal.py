"""Tests for withdrawing third-party app trust when part of the work fails.

A revoke, an uninstall or the blanket switch that cannot finish says so and leaves
the grant consistent: a failed delete puts the grant back, an overlay-owned grant
is reported rather than dropped, shutdown hooks run before the flag is persisted,
a grant write holds the lifecycle lock, the trust keys stay out of the generic config
PATCH, and disabling an app stands its in-process workers down in the request. The handlers run through the same in-test
``TestClient`` as ``test_trusted_apps_api.py``.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from trusted_apps_api_helpers import home_fixture  # noqa: F401
from trusted_apps_api_helpers import mock_sel_fixture  # noqa: F401
from trusted_apps_api_helpers import _APP, _client, _install, _stored

from kiro_crew.apps.manager import _read_installed


@pytest.mark.asyncio
async def test_failed_cron_cleanup_does_not_report_a_successful_revoke(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: ``on_app_disable`` reports cron cleanup as PROSE in one field —
    # "removed N job(s)" on success, "failed: cron store busy — jobs may still be
    # enabled" when the store is contended. Treating both as an informational
    # warning let a contended store leave the app's scheduled commands ARMED while
    # the endpoint returned 200 and said the app was switched off: third-party code
    # still executing on a timer, with trust reported as revoked.
    import kiro_crew.apps.teardown as appteardown

    async def _busy_store(name: str, record: dict, **_kw: object) -> dict:
        return {"cron_cleanup": "failed: cron store busy — jobs may still be enabled"}

    _install(tmp_path, _APP, enabled=True)
    with (
        patch.object(appteardown, "on_app_disable", _busy_store),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            assert (await client.post(f"/api/security/trusted-apps/{_APP}")).status == 200
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "teardown_incomplete"
            assert any("cron cleanup incomplete" in f for f in body["failures"])

    # RETRYABLE: the grant must still be there, otherwise a retry would skip the
    # teardown and the app would keep running with trust already gone. This is why
    # the teardown runs BEFORE the config write.
    assert _APP in _stored(home).get("apps_trusted", [])

    # And the retry succeeds once cleanup can complete.
    async def _clean(name: str, record: dict, **_kw: object) -> dict:
        return {"cron_cleanup": "removed 2 job(s)"}

    with (
        patch.object(appteardown, "on_app_disable", _clean),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["disabled"] is True
    assert _APP not in _stored(home).get("apps_trusted", [])


@pytest.mark.asyncio
async def test_live_detached_startup_hook_does_not_report_successful_revoke(
    home: Path, tmp_path: Path, mock_sel
):
    """Residual startup code is a hard, retryable teardown failure."""
    import kiro_crew.apps.teardown as appteardown

    async def _still_running(name: str, record: dict, **_kw: object) -> dict:
        return {
            "startup_cleanup": ("failed: detached startup hook is still running after cancellation")
        }

    _install(tmp_path, _APP, enabled=True)
    with (
        patch.object(appteardown, "on_app_disable", _still_running),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            assert (await client.post(f"/api/security/trusted-apps/{_APP}")).status == 200
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "teardown_incomplete"
            assert any("startup cleanup incomplete" in f for f in body["failures"])

    assert _APP in _stored(home).get("apps_trusted", [])


@pytest.mark.asyncio
async def test_blanket_off_reports_apps_it_could_not_stop(home: Path, tmp_path: Path, mock_sel):
    # REGRESSION: turning the blanket flag OFF sweeps every enabled third-party app
    # that holds no grant. A failed teardown must not `continue` silently, or the
    # response carried only `stopped` — the operator could not tell that code they
    # had just un-trusted was STILL RUNNING. Same shape as the metadata-only revoke
    # this feature already had to fix, one layer up.
    import kiro_crew.apps.teardown as appteardown

    async def _busy_store(name: str, record: dict, **_kw: object) -> dict:
        return {"cron_cleanup": "failed: cron store busy — jobs may still be enabled"}

    _install(tmp_path, _APP, enabled=True)
    with (
        patch.object(appteardown, "on_app_disable", _busy_store),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            on = await client.put("/api/security/trusted-apps/allow-all", json={"value": True})
            assert on.status == 200, await on.text()
            off = await client.put("/api/security/trusted-apps/allow-all", json={"value": False})
            # 409, not 200: the flag IS off (and the snapshot says so) but code the
            # flag was admitting survived, and the caller must be able to tell.
            assert off.status == 409
            body = await off.json()
            assert body["allowAll"] is False
            assert body["code"] == "blanket_trust_sweep_incomplete"
            # The app is NOT in `stopped`, and the response SAYS it is still running.
            assert _APP not in body["stopped"]
            assert _APP in body["stillRunning"]


def test_uninstall_reports_a_trust_grant_it_could_not_drop(home: Path, tmp_path: Path):
    # REGRESSION: uninstall drops the app's grant because a grant is keyed on NAME
    # alone — a leftover would admit a DIFFERENT app installed under that name with
    # no consent prompt. When the drop FAILED the old code logged and returned
    # ok=True, so the app vanished while its code-execution trust stayed on file and
    # reusable. The uninstall still succeeds (the files are gone either way) but it
    # must say so.
    from kiro_crew.apps import manager as appmanager

    _install(tmp_path, _APP, enabled=False)

    def _boom(name: str) -> None:
        raise RuntimeError("config.json is unreadable")

    with patch.object(appmanager, "_drop_trust_grant", _boom):
        result = appmanager.uninstall_app(_APP)

    # REFUSES rather than half-uninstalling. Deleting the files first and reporting
    # the surviving grant as a warning left a state the user could not recover from:
    # the app is gone, so nothing is left to uninstall and no retry clears the
    # grant, while the name stays armed for whatever is installed under it next.
    assert result.ok is False
    assert result.error_code == "trust_grant_not_removed"
    assert "not uninstalling" in result.error
    assert "code" in result.to_dict()
    # Nothing was destroyed, so the user can fix the cause and retry.
    assert appmanager.get_app(_APP) is not None


@pytest.mark.asyncio
async def test_mutations_refuse_when_config_local_owns_the_trust_setting(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: KiroCrewConfig deep-merges config.local.json OVER config.json and
    # save() STRIPS overlay-owned values from what it writes. So when the overlay
    # owns a trust setting, mutating config.json is doubly ineffective — and the old
    # code returned 200, telling the operator a grant was revoked while the overlay
    # re-admitted the app's code on the very next load. Third variant of
    # "revocation that revokes nothing". Refusing is the honest answer: the overlay
    # is user-owned and Kiro Crew never writes it.
    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": ["pinned-app"]}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)
    async with _client() as client:
        for call in (
            client.post(f"/api/security/trusted-apps/{_APP}"),
            client.delete(f"/api/security/trusted-apps/{_APP}"),
            client.put("/api/security/trusted-apps/allow-all", json={"value": True}),
        ):
            resp = await call
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "trust_setting_overlay_owned"
            assert body["overlaySettings"] == ["apps_trusted"]
            assert "config.local.json" in body["error"]


@pytest.mark.asyncio
async def test_refused_revoke_does_not_switch_the_app_off(home: Path, tmp_path: Path, mock_sel):
    # REGRESSION: revoke tore the app down FIRST and only then wrote the config, so
    # an overlay-owned trust setting produced the worst of both — a 409 saying "that
    # change did not take effect" over an app that had just been stopped and
    # disabled, with the grant it was supposed to lose still standing. The user
    # asked to withdraw permission and instead lost the working app and kept the
    # permission. Config mutability is now pre-flighted before anything is stopped.
    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP], "apps_trusted_local": [_APP]}}),
        encoding="utf-8",
    )
    _install(tmp_path, _APP, enabled=True)
    with patch("kiro_crew.dashboard.handlers.security.teardown_app_runtime") as teardown:
        async with _client() as client:
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 409
            assert (await resp.json())["code"] == "trust_setting_overlay_owned"

    # Nothing was stopped, and the app is still switched on.
    teardown.assert_not_called()
    meta = _read_installed(_APP)
    assert meta is not None and meta.enabled is True


def test_uninstall_holds_the_shared_config_lock_across_the_grant_drop(home: Path, tmp_path: Path):
    # REGRESSION: `_drop_trust_grant` is a read-modify-write of config.json, but the
    # uninstall route held only `app_lifecycle_lock(name)` — keyed on the APP, so it
    # serializes nothing against a settings/agent write, which takes the SHARED
    # config lock and rewrites the same file. The two interleaved into a lost
    # update: either the user's settings vanished, or the removed grant came back —
    # and a restored name-keyed grant is a consent bypass for whatever is installed
    # under that name next. Asserted structurally (the route acquires the shared
    # lock around the executor call) because reproducing the interleaving requires
    # winning a race.
    from kiro_crew.apps import routes_uninstall

    src = Path(routes_uninstall.__file__).read_text(encoding="utf-8")
    handler = src[src.index("async def _run_uninstall") :]
    handler = handler[: handler.index("\nasync def ", 1)]
    lock_at = handler.index("_get_config_lock()")
    call_at = handler.index("uninstall_app(name, keep_data=keep_data")
    assert lock_at < call_at, "the shared config lock must be held across uninstall_app"


def test_uninstall_reports_an_overlay_owned_grant_it_cannot_drop(home: Path, tmp_path: Path):
    # Same hazard on the uninstall path: the grant is name-keyed, so one left in the
    # overlay would admit a DIFFERENT app installed under this name later. The
    # uninstall still succeeds (files are gone) but must SAY the grant survived.
    from kiro_crew.apps import manager as appmanager

    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP], "apps_trusted_local": [_APP]}}),
        encoding="utf-8",
    )
    _install(tmp_path, _APP, enabled=False)
    result = appmanager.uninstall_app(_APP)

    assert result.ok is False
    assert result.error_code == "trust_grant_not_removed"
    assert "config.local.json" in result.error
    assert appmanager.get_app(_APP) is not None  # still installed, so retryable


def test_an_overlay_grant_for_another_app_does_not_block_this_uninstall(home: Path, tmp_path: Path):
    # The overlay refusal must not fire on the mere PRESENCE of `apps_trusted`,
    # regardless of which apps it named — so any operator who set it at all could
    # never uninstall ANY app. Scoped to a grant this app actually holds.
    from kiro_crew.apps import manager as appmanager

    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": ["some-other-app"]}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)

    assert appmanager.trust_grant_removal_blocked(_APP) is None
    result = appmanager.uninstall_app(_APP)
    assert result.ok is True, result.error
    assert appmanager.get_app(_APP) is None


def test_uninstall_drops_the_base_grant_even_when_an_overlay_replaces_the_list(
    home: Path, tmp_path: Path
):
    # The removal must not decide from the MERGED config. A list merge REPLACES,
    # so base ["<app>"] + overlay ["other"] merges to ["other"], the merged view
    # sees no grant for <app>, and nothing is removed — leaving the BASE entry
    # behind. It is inert only while that overlay key stands; edit or drop the
    # key and a different app installed under this name inherits the grant.
    from kiro_crew.apps import manager as appmanager

    # Install before authoring the trust fixture. Installation legitimately
    # persists config and applies overlay ownership; doing it afterwards would
    # remove the base key this test is specifically meant to exercise before
    # uninstall ever runs.
    _install(tmp_path, _APP, enabled=False)
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "apps_trusted": [_APP],
                    "apps_trusted_repositories": {_APP: "https://example.test/owner/trust-app"},
                    "model": "sonnet",
                }
            }
        ),
        encoding="utf-8",
    )
    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": ["some-other-app"]}}), encoding="utf-8"
    )

    # Not blocked: the OVERLAY does not grant this app, so the effective grant is
    # removable even though the overlay owns the setting's merged value.
    assert appmanager.trust_grant_removal_blocked(_APP) is None
    result = appmanager.uninstall_app(_APP)
    assert result.ok is True, result.error

    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == []
    assert base["agent"]["apps_trusted_repositories"] == {}
    # Everything else in the base file survives the targeted edit.
    assert base["agent"]["model"] == "sonnet"
    # And the overlay is never written by us.
    overlay = json.loads((home / "config.local.json").read_text(encoding="utf-8"))
    assert overlay["agent"]["apps_trusted"] == ["some-other-app"]


def test_uninstall_withdraws_a_grant_that_landed_during_the_delete(home: Path, tmp_path: Path):
    # The cross-process race GPT named: `kirocrew app uninstall` drops the (absent)
    # grant, and only THEN deletes the files. In that gap the dashboard's grant
    # handler sees the app on disk, writes the grant, and its own post-write
    # existence re-check ALSO still sees the app — so every guard on both sides
    # passes and the grant is left standing over a name no app occupies, ready to
    # admit a different app under that name with no consent prompt.
    #
    # Injected deterministically here: the concurrent write is performed from inside
    # `rmtree`, which is exactly the window (after the first withdrawal, before the
    # files are gone). No cross-process lock is needed to close it — only the second
    # withdrawal AFTER the delete, which by ordering must run after that write.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [], "model": "sonnet"}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)

    real_rmtree = appmanager.shutil.rmtree

    def _rmtree_racing_a_grant(path, *a, **kw):
        # Stand in for the other process's grant handler winning the gap.
        cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
        cfg["agent"]["apps_trusted"] = [_APP]
        (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return real_rmtree(path, *a, **kw)

    with patch.object(appmanager.shutil, "rmtree", _rmtree_racing_a_grant):
        result = appmanager.uninstall_app(_APP)

    assert result.ok is True, result.error
    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == [], (
        "a grant that landed mid-delete survived the uninstall — the name is armed "
        "with no app to own it"
    )
    assert base["agent"]["model"] == "sonnet"


def test_a_failed_delete_puts_the_trust_grant_back(home: Path, tmp_path: Path):
    # REGRESSION: the withdrawal runs BEFORE the delete (so a withdrawal failure is
    # retryable with nothing destroyed). But if the delete then failed, the app was
    # still installed with its grant already gone — an operation that did not even
    # succeed silently stripped a permission the operator had granted.
    #
    # Fixed by restoring rather than by deferring the withdrawal: deferring it until
    # after a successful delete re-opens the hole the current ordering exists to
    # close, where a withdrawal that fails leaves the app GONE with its name still
    # armed and no app left to uninstall, so no retry can ever clear it.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "apps_trusted": [_APP],
                    "apps_trusted_repositories": {_APP: "https://example.test/owner/trust-app"},
                    "model": "sonnet",
                }
            }
        ),
        encoding="utf-8",
    )
    _install(
        tmp_path,
        _APP,
        enabled=False,
        source_repository="https://example.test/owner/trust-app",
    )

    def _boom(*a, **kw):
        raise OSError("device busy")

    with patch.object(appmanager.shutil, "rmtree", _boom):
        result = appmanager.uninstall_app(_APP)

    assert result.ok is False
    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == [
        _APP
    ], "a failed uninstall left the still-installed app stripped of its grant"
    assert base["agent"]["apps_trusted_repositories"] == {
        _APP: "https://example.test/owner/trust-app"
    }, "a failed uninstall restored the name but lost its repository binding"
    assert base["agent"]["model"] == "sonnet"


def test_a_failed_delete_restores_explicit_local_grant_kind(home: Path, tmp_path: Path):
    from kiro_crew.apps import manager as appmanager

    _install(tmp_path, _APP, enabled=False)
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "apps_trusted": [_APP],
                    "apps_trusted_local": [_APP],
                    "model": "sonnet",
                }
            }
        ),
        encoding="utf-8",
    )

    def _boom(*args, **kwargs):
        raise OSError("device busy")

    with patch.object(appmanager.shutil, "rmtree", _boom):
        result = appmanager.uninstall_app(_APP)

    assert result.ok is False
    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == [_APP]
    assert base["agent"]["apps_trusted_local"] == [_APP]
    assert base["agent"]["model"] == "sonnet"


def test_partial_delete_never_restores_grant_to_a_missing_or_replacement_app(
    home: Path, tmp_path: Path
):
    """Old consent cannot survive loss of the durable installed occupant."""
    from kiro_crew.apps import manager as appmanager
    from kiro_crew.apps.execution import app_execution_denied
    from kiro_crew.config.loader import _invalidate_config_cache

    _install(tmp_path, _APP, enabled=False)
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "apps_trusted": [_APP],
                    "apps_trusted_repositories": {_APP: "https://example.test/owner/original"},
                    "apps_trusted_local": [_APP],
                }
            }
        ),
        encoding="utf-8",
    )
    _invalidate_config_cache()

    def _partial_rmtree(path, *args, **kwargs):
        (Path(path) / "installed.json").unlink()
        raise OSError("device failed after deleting installed metadata")

    with patch.object(appmanager.shutil, "rmtree", _partial_rmtree):
        result = appmanager.uninstall_app(_APP)

    assert result.ok is False
    assert "could not be safely restored" in result.error
    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == []
    assert base["agent"]["apps_trusted_repositories"] == {}
    assert base["agent"]["apps_trusted_local"] == []

    replacement = appmanager.register_external_app(
        _APP,
        "2.0.0",
        "Same-name replacement",
    )
    assert replacement.ok, replacement.error
    assert app_execution_denied(_APP, action="module_load") is not None


def test_restore_postcheck_withdraws_grant_if_installed_occupant_changes(
    home: Path, tmp_path: Path
):
    """A replacement between the precheck and config write inherits nothing."""
    from kiro_crew.apps import manager as appmanager
    from kiro_crew.config.loader import _invalidate_config_cache

    _install(tmp_path, _APP, enabled=False)
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "apps_trusted": [_APP],
                    "apps_trusted_local": [_APP],
                }
            }
        ),
        encoding="utf-8",
    )
    _invalidate_config_cache()

    def _delete_failure(*args, **kwargs):
        raise OSError("device busy")

    real_write = appmanager.update_config_locked
    writes = 0

    def _write_then_replace(path, **kwargs):
        nonlocal writes
        writes += 1
        result = real_write(path, **kwargs)
        if writes == 2:  # pre-delete drop is first; restore write is second
            changed = appmanager._read_installed(_APP)
            assert changed is not None
            changed.version = "9.9.9"
            appmanager._write_installed(_APP, changed)
        return result

    with (
        patch.object(appmanager.shutil, "rmtree", _delete_failure),
        patch.object(appmanager, "update_config_locked", _write_then_replace),
    ):
        result = appmanager.uninstall_app(_APP)

    assert result.ok is False
    assert "installed app changed" in result.error
    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == []
    assert base["agent"]["apps_trusted_local"] == []


def test_successful_uninstall_removes_explicit_local_grant_kind(home: Path, tmp_path: Path):
    from kiro_crew.apps import manager as appmanager

    _install(tmp_path, _APP, enabled=False)
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "apps_trusted": [_APP],
                    "apps_trusted_local": [_APP],
                }
            }
        ),
        encoding="utf-8",
    )

    result = appmanager.uninstall_app(_APP)

    assert result.ok, result.error
    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == []
    assert base["agent"]["apps_trusted_local"] == []


def test_a_failed_delete_does_not_grant_an_app_that_had_no_grant(home: Path, tmp_path: Path):
    # The other half, and the one that would be a real vulnerability: restoring must
    # never CREATE a grant. An untrusted app whose uninstall fails must stay
    # untrusted, or a failed uninstall becomes a way to gain execution permission.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(json.dumps({"agent": {"apps_trusted": []}}), encoding="utf-8")
    _install(tmp_path, _APP, enabled=False)

    def _boom(*a, **kw):
        raise OSError("device busy")

    with patch.object(appmanager.shutil, "rmtree", _boom):
        result = appmanager.uninstall_app(_APP)

    assert result.ok is False
    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == [], "a failed uninstall granted execution"


def test_the_cli_trust_withdrawal_is_audited(home: Path, tmp_path: Path):
    # A permission boundary that moves silently is what SEL exists to make visible.
    # The dashboard revoke endpoint audits its own withdrawal, but this path runs
    # from `kirocrew app uninstall`, so without this the grant could disappear with
    # nothing in the security event log to reconstruct the trust timeline from.
    #
    # Patched on `manager`, not via the `mock_sel` fixture: that one patches the
    # DASHBOARD handler's `_sel`, which this code path never touches.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP], "apps_trusted_local": [_APP]}}),
        encoding="utf-8",
    )
    _install(tmp_path, _APP, enabled=False)

    recorder = MagicMock()
    with patch.object(appmanager, "sel", lambda: recorder):
        result = appmanager.uninstall_app(_APP)
    assert result.ok is True, result.error

    ops = [c.kwargs.get("operation") for c in recorder.log_api_access.call_args_list]
    assert "app_trust_revoke" in ops, f"no SEL event for the withdrawal: {ops}"


def test_uninstall_writes_no_config_at_all_when_there_is_no_grant(home: Path, tmp_path: Path):
    # Bounds what the two withdrawals cost. `config.json` read-modify-write is
    # unlocked across processes REPO-WIDE (24 writers on the base branch, including
    # `kirocrew config set`, whose own reader documents the shape), so every writer
    # carries some lost-update exposure. This asserts the trust-grant cleanup adds
    # NONE of it in the ordinary case: with no grant to withdraw, both calls read,
    # find nothing, and return WITHOUT writing. A write happens only when there is
    # actually an orphaned grant to remove — the case the withdrawal exists for.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(json.dumps({"agent": {"model": "sonnet"}}), encoding="utf-8")
    _install(tmp_path, _APP, enabled=False)

    with patch.object(appmanager, "update_config_locked") as writes:
        result = appmanager.uninstall_app(_APP)

    assert result.ok is True, result.error
    assert (
        writes.call_count == 0
    ), f"uninstalling an ungranted app rewrote config.json {writes.call_count}x"


def test_uninstall_reports_a_grant_it_could_not_withdraw_after_the_delete(
    home: Path, tmp_path: Path
):
    # Same window, but the second withdrawal fails. Refusing the uninstall is not
    # available (the files are already gone) and claiming plain success would hide a
    # live grant over a name nothing occupies, invisible in the app list because
    # there is no app left to list. So it must succeed AND say so.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(json.dumps({"agent": {"apps_trusted": []}}), encoding="utf-8")
    _install(tmp_path, _APP, enabled=False)

    calls = {"n": 0}
    real_drop = appmanager._drop_trust_grant

    def _drop(name: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_drop(name)  # the pre-delete withdrawal succeeds
        raise RuntimeError("config.json went unreadable")

    with patch.object(appmanager, "_drop_trust_grant", _drop):
        result = appmanager.uninstall_app(_APP)

    assert calls["n"] == 2, "the post-delete withdrawal did not run"
    assert result.ok is True, result.error
    assert "still in" in result.message and _APP in result.message, result.message
    assert "Settings" in result.message, "no recovery step offered for a live grant"


@pytest.mark.asyncio
async def test_grant_preserves_base_settings_shadowed_by_the_overlay(
    home: Path, tmp_path: Path, mock_sel
):
    # The mutation must not run KiroCrewConfig.load() -> cfg.save(), because save()
    # strips every value config.local.json also defines so overlay settings do not
    # leak into the base file. Routing a trust write through it rewrote the WHOLE
    # base document minus all overlay-owned keys, so granting one app trust
    # permanently deleted the user's base `model` whenever an overlay also set it.
    # Nothing looked wrong at the time -- the merged config still read the overlay
    # value -- until the overlay was removed and the base setting was simply gone.
    # Install FIRST: `install_app` writes the whole config through the model, so
    # seeding before it would measure main's writer, not the trust endpoint.
    _install(tmp_path, _APP, enabled=False)
    # Migration-complete on purpose: `KiroCrewConfig.load()` write-BACKS a legacy
    # config (missing `agents`/`default_agent`), and that write-back is itself a
    # full model save. A minimal hand-written config would trip it and measure the
    # migration instead of the mutation under test.
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {"model": "sonnet", "max_subagents": 3},
                "agents": {
                    "default": {
                        "kiro_agent": "kirocrew",
                        "workspace": "default",
                        "memory_store": "default",
                    }
                },
                "default_agent": "default",
                "workspaces": {"default": {"dir": "workspace"}},
                "default_workspace": "default",
            }
        ),
        encoding="utf-8",
    )
    (home / "config.local.json").write_text(
        json.dumps({"agent": {"model": "opus"}}), encoding="utf-8"
    )

    async with _client() as client:
        resp = await client.post(f"/api/security/trusted-apps/{_APP}")
        assert resp.status == 200, await resp.text()

    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    # The grant landed...
    assert base["agent"]["apps_trusted"] == [_APP]
    # ...and the overlay-shadowed base setting survived, as did its neighbour.
    assert base["agent"]["model"] == "sonnet"
    assert base["agent"]["max_subagents"] == 3
    # The overlay itself is never written by us.
    overlay = json.loads((home / "config.local.json").read_text(encoding="utf-8"))
    assert overlay == {"agent": {"model": "opus"}}


@pytest.mark.asyncio
async def test_uninstall_route_refuses_before_running_anything_destructive(
    home: Path, tmp_path: Path, mock_sel
):
    # The abort must not live inside uninstall_app, which the handler reaches only
    # at Step 5 — AFTER cron deregistration, the app's non-idempotent onUninstall
    # script, the backend stop and dependency cleanup. The refusal therefore
    # stranded a half-removed app and re-ran onUninstall on every retry. It is now
    # a precondition: nothing destructive may have run when it fires.
    from kiro_crew.apps import routes as approutes

    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP], "apps_trusted_local": [_APP]}}),
        encoding="utf-8",
    )
    _install(tmp_path, _APP, enabled=False)

    uninstall_app_router = as_owner(web.Application())
    uninstall_app_router.router.add_delete("/api/apps/{name}", approutes.handle_uninstall_app)

    destructive: list[str] = []
    with (
        patch.object(
            approutes,
            "deregister_app",
            lambda *a, **k: destructive.append("deregister_app"),
        ),
        patch.object(
            approutes,
            "uninstall_app",
            lambda *a, **k: destructive.append("uninstall_app"),
        ),
    ):
        async with TestClient(TestServer(uninstall_app_router)) as client:
            resp = await client.delete(f"/api/apps/{_APP}")
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "trust_grant_not_removed"
            assert body["retryable"] is True
            assert "Nothing has been changed" in body["error"]

    assert destructive == []


@pytest.mark.asyncio
async def test_blanket_off_runs_shutdown_hooks_before_persisting_false(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: the flag is what authorises loading an app's Python at all —
    # `load_app_module` consults `app_execution_denied`. Persisting `false` FIRST
    # therefore denied the app's own `on_shutdown` hook, so it was stopped without
    # ever being told to flush state or release resources. The sweep has to run
    # while trust still stands. Pinned by observing the config as the hook sees it.
    import kiro_crew.apps.teardown as appteardown

    observed: list[bool] = []

    async def _on_app_disable(name: str, record: dict, **_kw: object) -> dict:
        # What a shutdown hook would see when the loader gates it.
        from kiro_crew.apps.execution import third_party_execution_allowed

        observed.append(third_party_execution_allowed())
        return {}

    _install(tmp_path, _APP, enabled=True)
    with (
        patch.object(appteardown, "on_app_disable", _on_app_disable),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            assert (
                await client.put("/api/security/trusted-apps/allow-all", json={"value": True})
            ).status == 200
            off = await client.put("/api/security/trusted-apps/allow-all", json={"value": False})
            assert off.status == 200, await off.text()
            assert (await off.json())["allowAll"] is False

    # The hook ran, and it ran while the app was still permitted to execute.
    assert observed == [True], (
        "the shutdown hook must run BEFORE the flag is persisted; observed "
        f"allow-all={observed} at hook time"
    )


@pytest.mark.asyncio
async def test_blanket_off_catches_an_app_enabled_during_the_sweep(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: the falling edge sweeps BEFORE persisting `false` so shutdown hooks
    # can still load. That opened a window — the sweep enumerates candidates and then
    # spends real time stopping backends, and an app enabled inside that window is
    # never in the enumeration, so it kept executing under trust the operator
    # believed they had withdrawn. Per-app locks cannot cover it: there is no name to
    # lock for an app that was not enumerated. A second sweep after the write closes
    # it. Simulated by enabling a second app from inside the first app's teardown.
    import kiro_crew.apps.teardown as appteardown
    from kiro_crew.apps.manager import enable_app

    late = "trust-late-app"
    _install(tmp_path, _APP, enabled=True)
    _install(tmp_path, late, enabled=False)

    async def _enable_the_other_app(name: str, record: dict, **_kw: object) -> dict:
        # The racing enable: lands after `late` was already passed over.
        if name == _APP:
            enable_app(late)
        return {}

    with (
        patch.object(appteardown, "on_app_disable", _enable_the_other_app),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            assert (
                await client.put("/api/security/trusted-apps/allow-all", json={"value": True})
            ).status == 200
            off = await client.put("/api/security/trusted-apps/allow-all", json={"value": False})
            body = await off.json()

    # The app that raced in must NOT be left enabled-and-running on withdrawn trust:
    # either it was stopped, or the response says it may still be executing.
    from kiro_crew.apps.manager import get_app

    record = get_app(late) or {}
    accounted = late in body.get("stopped", []) or late in body.get("stillRunning", [])
    assert accounted, (
        f"{late!r} was enabled during the sweep and is unaccounted for: "
        f"stopped={body.get('stopped')} stillRunning={body.get('stillRunning')}"
    )
    assert not (record.get("enabled") and not accounted)


def test_blanket_flag_is_not_editable_through_the_generic_config_patch():
    # REGRESSION: `agent.apps_allow_third_party` was PATCHable through
    # /api/config/kirocrew, and that path performs NO teardown — so flipping it off
    # there withdrew trust on paper while every app it had admitted kept executing,
    # crons included, until a gateway restart. A dashboard card shipped against that
    # endpoint, which is how the hole became reachable from the UI.
    #
    # The key is deliberately absent from the editable set so the ONLY writer is the
    # endpoint that runs the sweep, and the refusal names it rather than dead-ending
    # on "field not editable".
    from kiro_crew.dashboard.handlers.core import (
        _EDITABLE_CONFIG,
        _MOVED_CONFIG_FIELDS,
    )

    assert "agent.apps_allow_third_party" not in _EDITABLE_CONFIG
    hint = _MOVED_CONFIG_FIELDS["agent.apps_allow_third_party"]
    assert "/api/security/trusted-apps/allow-all" in hint
    # The sibling grant list is not editable there either, for the same reason.
    assert "agent.apps_trusted" not in _EDITABLE_CONFIG
    assert "agent.apps_trusted_local" not in _EDITABLE_CONFIG
    assert "agent.apps_trusted_repositories" not in _EDITABLE_CONFIG


@pytest.mark.asyncio
async def test_grant_holds_the_app_lifecycle_lock_across_validate_and_write(
    home: Path, tmp_path: Path, mock_sel
):
    # Validation and the write have to be one critical section. Unserialized, a
    # concurrent uninstall sees no grant to drop, tears the app down, and THIS
    # grant lands afterwards — leaving a grant on a name nothing owns, which is
    # exactly what lets a later app claiming that name run code unprompted.
    # Revoke already serializes this way; grant did not.
    from kiro_crew.apps import manager as appmanager
    from kiro_crew.dashboard.handlers import security as sec

    _install(tmp_path, _APP, enabled=False)
    lock = appmanager.app_lifecycle_lock(_APP)

    held_during_write: list[bool] = []
    real_mutate = sec._mutate_agent_config

    async def _spy(mutate):
        held_during_write.append(lock.locked())
        return await real_mutate(mutate)

    with patch.object(sec, "_mutate_agent_config", _spy):
        async with _client() as client:
            resp = await client.post(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 200, await resp.text()

    # The write ran while the per-app lifecycle lock was held, so an uninstall
    # cannot interleave between the existence check and the persisted grant.
    assert held_during_write == [True]
    # And the lock is released afterwards — a held lock would wedge every later
    # lifecycle op on this app.
    assert lock.locked() is False


class TestDisablingAnAppStandsItsWorkersDownInTheRequest:
    """The off-switch must stop in-process workers NOW, not on their next poll.

    An app whose workers hold something time-bounded — Issue Radar's crews hold an
    auto-approval grant — can otherwise act once more in the gap between the
    operator's click and the app's next sweep. At a 60s poll that is a whole
    fully-approved turn taken after permission was withdrawn.
    """

    @staticmethod
    def _teardown_patches(appteardown, ran: list[str], stopped: list[str]):
        async def _script(name, script, **kw):
            ran.append(f"script:{script}")
            return {"output": "", "failed": False}

        async def _hook(name, record, **kw):
            return {}

        return (
            patch.object(appteardown, "on_app_disable", _hook),
            patch.object(appteardown, "stop_app_backend", lambda n: stopped.append(n)),
            patch.object(appteardown, "recorded_backend_port", lambda n: None),
            patch.object(appteardown, "unstopped_backend_port", lambda n, **kw: None),
            patch.object(appteardown, "deregister_app", lambda n: None),
            patch.object(appteardown, "run_lifecycle_script", _script),
        )

    @pytest.mark.asyncio
    async def test_the_hook_runs_before_the_apps_own_shutdown_script(
        self, home: Path, tmp_path: Path
    ):
        # Ordering is the fix, not decoration: `onDisable` is third-party code and
        # stopping a backend waits on it, so a worker still holding authority while
        # those run keeps it for their whole duration.
        import kiro_crew.apps.teardown as appteardown

        ran: list[str] = []
        stopped: list[str] = []

        async def _revoke(app_name):
            ran.append(f"revoked:{app_name}")

        _install(tmp_path, _APP, enabled=True)
        record = {
            "enabled": True,
            "manifest": {"setup": {"onDisable": "stop-helper"}},
        }
        appteardown.register_app_disable_hook(_APP, _revoke)
        try:
            with contextlib.ExitStack() as stack:
                for p in self._teardown_patches(appteardown, ran, stopped):
                    stack.enter_context(p)
                result = await appteardown.teardown_app_runtime(_APP, record)
        finally:
            appteardown.unregister_app_disable_hook(_APP)

        assert result.ok, result.failures
        assert f"revoked:{_APP}" in ran, (
            "the app was never told it had been disabled, so its workers keep "
            "whatever authority they hold until their own next sweep"
        )
        assert (
            ran[0] == f"revoked:{_APP}"
        ), f"the workers were stood down after slower teardown steps: {ran}"

    @pytest.mark.asyncio
    async def test_withdrawing_trust_also_stands_the_workers_down(self, home: Path, tmp_path: Path):
        # Revoke and the falling-edge sweep are the SECURITY paths; they must not be
        # weaker than an ordinary off-switch.
        import kiro_crew.apps.teardown as appteardown

        ran: list[str] = []
        _install(tmp_path, _APP, enabled=True)
        appteardown.register_app_disable_hook(_APP, lambda app_name: _noop(ran))
        try:
            with contextlib.ExitStack() as stack:
                for p in self._teardown_patches(appteardown, ran, []):
                    stack.enter_context(p)
                result = await appteardown.teardown_app_runtime(
                    _APP, {"enabled": True, "manifest": {}}, withdrawing_trust=True
                )
        finally:
            appteardown.unregister_app_disable_hook(_APP)

        assert result.ok, result.failures
        assert "revoked" in ran, "trust withdrawal left the app's workers armed"

    @pytest.mark.asyncio
    async def test_a_failing_hook_does_not_abort_the_teardown(self, home: Path, tmp_path: Path):
        # Same contract as every other step here: push through and report.
        import kiro_crew.apps.teardown as appteardown

        ran: list[str] = []
        stopped: list[str] = []

        async def _boom(app_name):
            raise RuntimeError("worker registry unreachable")

        _install(tmp_path, _APP, enabled=True)
        appteardown.register_app_disable_hook(_APP, _boom)
        try:
            with contextlib.ExitStack() as stack:
                for p in self._teardown_patches(appteardown, ran, stopped):
                    stack.enter_context(p)
                result = await appteardown.teardown_app_runtime(
                    _APP, {"enabled": True, "manifest": {}}
                )
        finally:
            appteardown.unregister_app_disable_hook(_APP)

        assert result.ok, result.failures
        assert stopped == [_APP], "a throwing hook stopped the backend from stopping"

    @pytest.mark.asyncio
    async def test_an_app_with_no_hook_is_unaffected(self, home: Path, tmp_path: Path):
        import kiro_crew.apps.teardown as appteardown

        stopped: list[str] = []
        _install(tmp_path, _APP, enabled=True)
        assert _APP not in appteardown._APP_DISABLE_HOOKS
        with contextlib.ExitStack() as stack:
            for p in self._teardown_patches(appteardown, [], stopped):
                stack.enter_context(p)
            result = await appteardown.teardown_app_runtime(_APP, {"enabled": True, "manifest": {}})

        assert result.ok, result.failures
        assert stopped == [_APP]


async def _noop(ran: list[str]) -> None:
    ran.append("revoked")
