"""The worker auto-approve seam: an app's bounded grant on its OWN worker slots.

``apps/worker_auto_approve.py`` is the seam for an app to have an unattended session's
tool calls approved without a worker-slot lease. These cases pin what it may never do as
firmly as what it does: an app that does not declare ``permissions.workerAutoApprove``
gets nothing, a declaring app reaches only its own slots and its own scope keys, the
approval-mode ceiling still refuses, an update that adds the declaration waits for the
owner, and disabling or uninstalling the app ends every grant it holds.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import safety_override as so_mod
from kiro_crew.apps import teardown
from kiro_crew.apps import worker_auto_approve as waa
from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    INSTALLED_META_FILENAME,
    _write_approve_declared,
    app_dir,
    approved_manifest_permissions,
    disable_app,
    enable_app,
    get_app,
    install_app,
    uninstall_app,
    update_app,
)
from kiro_crew.apps.manifest_sections import Permissions
from kiro_crew.apps.point_grants import grant_source, point_grant_families
from kiro_crew.dashboard import chat_runner
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy

APP = "test-app"
OTHER = "other-app"
TTL = 900


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch, sel_private_root):
    """A private app home, SEL root, ceiling and ``SafetyOverride`` per case."""
    monkeypatch = _floor_monkeypatch
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    ctx_mod.reset_context()
    so_mod.reset_yolo_policy_state()
    so_mod.reset_singleton()
    monkeypatch.setattr(waa, "_held", {})
    yield
    so_mod.reset_yolo_policy_state()
    so_mod.reset_singleton()
    gp.reset_store()
    ctx_mod.reset_context()


def _source(tmp_path: Path, sub: str, *, version: str = "1.0.0", declare: bool = True) -> Path:
    src = tmp_path / sub / APP
    src.mkdir(parents=True)
    permissions: dict = {"storage": True}
    if declare:
        permissions["workerAutoApprove"] = True
    manifest = {
        "name": APP,
        "version": version,
        "displayName": "Test App",
        "description": "worker auto-approve",
        "author": "tester",
        "permissions": permissions,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return src


def _installed(tmp_path: Path, *, declare: bool = True) -> None:
    result = install_app(_source(tmp_path, "v1", declare=declare))
    assert result.ok, result.error
    assert enable_app(APP).ok


def _hold(sdk: waa.WorkerAutoApprove, slot: SimpleNamespace, name: str) -> bool:
    """``hold`` with the one grant lifetime every case uses, which no case waits on."""
    return sdk.hold(slot, name, ttl_secs=TTL)  # flake-ok: a grant lifetime, never waited on


def _slot(app: str = APP, key: str = "worker-1", scope: str = "") -> SimpleNamespace:
    return SimpleNamespace(key=key, _app=app, _trust=False, _trust_scope=scope)


def _slot_is_trusted(slot: SimpleNamespace) -> bool:
    """The shared approval path's own answer, through the facade it is served from."""
    return bool(chat_runner._slot_is_trusted(slot))


def _active(scope: str) -> bool:
    return so_mod.safety_override().scope_remaining_secs(scope) > 0


def _deny_yolo() -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "approval_modes": {"mode": "deny", "deny": ["yolo"]},
        }
    )
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


# ── the declaration ─────────────────────────────────────────────────────────


def test_only_the_literal_true_declares_it():
    assert Permissions.from_dict({"workerAutoApprove": "true"}).workerAutoApprove is False
    assert Permissions.from_dict({"workerAutoApprove": 1}).workerAutoApprove is False
    declared = Permissions.from_dict({"workerAutoApprove": True})
    assert declared.to_dict() == {"workerAutoApprove": True}


def test_the_declaration_is_a_staged_grant_family():
    assert waa.GRANT_FAMILY in point_grant_families()
    source = grant_source({"permissions": {"workerAutoApprove": True}})
    assert source is not None and source[waa.GRANT_FAMILY] == [waa.GRANT_ENTRY]
    undeclared = grant_source({"permissions": {}})
    assert undeclared is not None and undeclared[waa.GRANT_FAMILY] == []


def test_the_context_carries_the_sdk_only_when_declared(tmp_path):
    declared = build_app_context(APP, tmp_path, permissions={"workerAutoApprove": True})
    assert isinstance(declared.auto_approve, waa.WorkerAutoApprove)
    assert declared.auto_approve.app == APP
    assert build_app_context(APP, tmp_path, permissions={}).auto_approve is None
    assert (
        build_app_context(APP, tmp_path, permissions={"workerAutoApprove": "yes"}).auto_approve
        is None
    )


# ── a grant on the app's own slot ───────────────────────────────────────────


def test_a_declaring_enabled_app_holds_a_grant_the_approval_path_honours(tmp_path):
    _installed(tmp_path)
    sdk = waa.WorkerAutoApprove(APP)
    slot = _slot()

    assert waa.app_holds_worker_auto_approve(APP) is True
    assert _hold(sdk, slot, "job-1") is True

    scope = sdk.scope("job-1")
    assert scope == "app:test-app:grant:job-1:autoapprove"
    assert slot._trust_scope == scope
    assert slot._trust is False, "the seam never writes the interactive trust flag"
    assert _slot_is_trusted(slot) is True
    assert 0 < so_mod.safety_override().scope_remaining_secs(scope) <= TTL


def test_a_second_hold_slides_the_grant_instead_of_reminting_it(tmp_path, monkeypatch):
    _installed(tmp_path)
    sdk = waa.WorkerAutoApprove(APP)
    slot = _slot()
    so = so_mod.safety_override()
    activations: list[str] = []
    real = type(so).activate_scoped

    def _count(self, scope, source, ttl=None):
        activations.append(source)
        return real(self, scope, source, ttl)

    monkeypatch.setattr(type(so), "activate_scoped", _count)
    for _ in range(3):
        assert _hold(sdk, slot, "job-1") is True
    assert activations == ["app:test-app"], "a live grant was re-minted rather than slid"


def test_release_ends_the_grant_and_clears_the_slot(tmp_path):
    _installed(tmp_path)
    sdk = waa.WorkerAutoApprove(APP)
    slot = _slot()
    assert _hold(sdk, slot, "job-1")

    assert sdk.release("job-1", slot) is True
    assert slot._trust_scope == ""
    assert not _active(sdk.scope("job-1"))
    assert _slot_is_trusted(slot) is False
    assert sdk.release("job-1", slot) is False, "a second release finds nothing"


# ── what an app cannot do with it (the negative half) ───────────────────────


def test_an_app_that_does_not_declare_it_gets_no_grant(tmp_path):
    _installed(tmp_path, declare=False)
    sdk = waa.WorkerAutoApprove(APP)
    slot = _slot()

    assert waa.app_holds_worker_auto_approve(APP) is False
    assert _hold(sdk, slot, "job-1") is False
    assert slot._trust_scope == ""
    assert not _active(sdk.scope("job-1"))
    assert _slot_is_trusted(slot) is False


def test_an_undeclaring_app_with_no_stored_approval_gets_no_grant(tmp_path):
    """A record from before the approved set was stored approves what is declared, so
    the declaration alone has to refuse here."""
    _installed(tmp_path, declare=False)
    # What the one-time import stores for such a record: approve what is declared.
    _write_approve_declared(APP)
    record = app_dir(APP) / INSTALLED_META_FILENAME
    data = json.loads(record.read_text(encoding="utf-8"))
    data.pop("approvedGrantsStored", None)
    record.write_text(json.dumps(data), encoding="utf-8")

    sdk = waa.WorkerAutoApprove(APP)
    assert waa.app_holds_worker_auto_approve(APP) is False
    assert _hold(sdk, _slot(), "job-1") is False


def test_an_app_that_is_not_installed_gets_no_grant():
    sdk = waa.WorkerAutoApprove(APP)
    assert _hold(sdk, _slot(), "job-1") is False
    assert not _active(sdk.scope("job-1"))


def test_a_grant_never_lands_on_a_slot_the_app_does_not_own(tmp_path):
    _installed(tmp_path)
    sdk = waa.WorkerAutoApprove(APP)
    for foreign in (_slot(app=OTHER), _slot(app="")):
        assert _hold(sdk, foreign, "job-1") is False
        assert foreign._trust_scope == ""
        assert sdk.release_slot(foreign) is False
    assert not _active(sdk.scope("job-1"))


def test_a_grant_never_replaces_another_holders_scope(tmp_path):
    _installed(tmp_path)
    sdk = waa.WorkerAutoApprove(APP)
    slot = _slot(scope="taskrunner:t1:autoapprove")
    assert _hold(sdk, slot, "job-1") is False
    assert slot._trust_scope == "taskrunner:t1:autoapprove"
    assert not _active(sdk.scope("job-1"))


def test_an_app_can_name_only_scopes_in_its_own_namespace(tmp_path):
    _installed(tmp_path)
    sdk = waa.WorkerAutoApprove(APP)
    for name in ("", "x:y", "taskrunner:t1", "../x", "a" * 129, "a b"):
        with pytest.raises(ValueError):
            sdk.scope(name)
    assert sdk.scope("crew-c_1").startswith("app:test-app:grant:")
    assert waa.WorkerAutoApprove(OTHER).scope("crew-c_1") != sdk.scope("crew-c_1")
    with pytest.raises(ValueError):
        waa.WorkerAutoApprove("bad:app")


def test_release_slot_never_deactivates_another_holders_grant(tmp_path):
    """Its own slot's foreign scope is taken off the slot, never revoked globally."""
    _installed(tmp_path)
    so = so_mod.safety_override()
    assert so.activate_scoped("taskrunner:t1:autoapprove", "taskrunner", 600).active
    slot = _slot(scope="taskrunner:t1:autoapprove")

    assert waa.WorkerAutoApprove(APP).release_slot(slot) is True
    assert slot._trust_scope == ""
    assert _active("taskrunner:t1:autoapprove")


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), 24 * 60 * 60 + 1, True])
def test_a_ttl_outside_the_ceiling_is_refused(tmp_path, ttl):
    _installed(tmp_path)
    with pytest.raises(ValueError):
        waa.WorkerAutoApprove(APP).hold(_slot(), "job-1", ttl_secs=ttl)  # flake-ok: refused TTL


def test_the_approval_mode_ceiling_still_refuses(tmp_path):
    _installed(tmp_path)
    sdk = waa.WorkerAutoApprove(APP)
    slot = _slot()
    _deny_yolo()

    assert _hold(sdk, slot, "job-1") is False
    assert slot._trust_scope == ""
    assert _slot_is_trusted(slot) is False


def test_a_failed_audit_grants_nothing(tmp_path, monkeypatch):
    _installed(tmp_path)

    def _boom(**_kw):
        raise OSError("SEL unavailable")

    monkeypatch.setattr(type(so_mod.safety_override()), "_log_sel", lambda self, **kw: _boom())
    slot = _slot()
    assert _hold(waa.WorkerAutoApprove(APP), slot, "job-1") is False
    assert slot._trust_scope == ""


# ── the owner's approval and the app's lifecycle ────────────────────────────


def test_an_update_that_adds_the_declaration_waits_for_the_owner(tmp_path):
    _installed(tmp_path, declare=False)
    result = update_app(_source(tmp_path, "v2", version="2.0.0", declare=True))
    assert result.ok, result.error

    row = get_app(APP)
    assert row is not None and row["enabled"] is True
    assert row["workerAutoApproveGrant"] == {"approved": False, "staged": True}
    sdk = waa.WorkerAutoApprove(APP)
    assert _hold(sdk, _slot(), "job-1") is False

    approved = enable_app(APP, grants_consent={waa.GRANT_FAMILY: [waa.GRANT_ENTRY]})
    assert approved.ok, approved.error
    assert get_app(APP)["workerAutoApproveGrant"] == {"approved": True, "staged": False}
    assert _hold(sdk, _slot(), "job-1") is True


def test_an_install_approves_what_it_declares(tmp_path):
    _installed(tmp_path)
    assert get_app(APP)["workerAutoApproveGrant"] == {"approved": True, "staged": False}


def test_an_undeclaring_app_row_carries_no_grant_key(tmp_path):
    _installed(tmp_path, declare=False)
    assert "workerAutoApproveGrant" not in get_app(APP)


def test_the_hook_context_of_a_declaring_app_carries_the_sdk(tmp_path):
    """The flag reaches the hook context as the flag, not as a list of grant entries.

    ``approved_manifest_permissions`` is what ``on_app_enable`` and the lifecycle hooks
    build the context from; it filters the entry-list families by the approved set.
    """
    _installed(tmp_path)
    permissions = approved_manifest_permissions(get_app(APP))
    assert permissions["workerAutoApprove"] is True
    ctx = build_app_context(APP, tmp_path, permissions=permissions)
    assert isinstance(ctx.auto_approve, waa.WorkerAutoApprove)


def test_the_teardown_ends_every_grant_before_the_apps_own_hook(tmp_path):
    _installed(tmp_path)
    sdk = waa.WorkerAutoApprove(APP)
    a, b = _slot(key="w1"), _slot(key="w2")
    assert _hold(sdk, a, "job-1") and _hold(sdk, b, "job-2")
    seen: list[bool] = []

    async def _hook(_app):
        seen.append(_active(sdk.scope("job-1")) or _active(sdk.scope("job-2")))

    teardown.register_app_disable_hook(APP, _hook)
    try:
        asyncio.run(teardown.notify_app_disabled(APP))
    finally:
        teardown.unregister_app_disable_hook(APP)

    assert seen == [False], "the app's hook ran while a grant was still live"
    assert _slot_is_trusted(a) is False and _slot_is_trusted(b) is False


def test_a_disable_ends_the_grant_and_refuses_the_next(tmp_path):
    _installed(tmp_path)
    sdk = waa.WorkerAutoApprove(APP)
    slot = _slot()
    assert _hold(sdk, slot, "job-1")

    assert disable_app(APP).ok
    assert not _active(sdk.scope("job-1"))
    assert _hold(sdk, slot, "job-1") is False
    assert slot._trust_scope == ""


def test_a_grant_minted_while_a_disable_lands_is_released(tmp_path, monkeypatch):
    """The record can turn disabled between the permission check and the mint."""
    _installed(tmp_path)
    sdk = waa.WorkerAutoApprove(APP)
    slot = _slot()
    answers = iter([True, False])
    monkeypatch.setattr(waa, "app_holds_worker_auto_approve", lambda _app: next(answers))

    assert _hold(sdk, slot, "job-1") is False
    assert slot._trust_scope == ""
    assert not _active(sdk.scope("job-1"))


def test_an_uninstall_ends_the_grant(tmp_path):
    _installed(tmp_path)
    sdk = waa.WorkerAutoApprove(APP)
    assert _hold(sdk, _slot(), "job-1")

    assert uninstall_app(APP).ok
    assert not _active(sdk.scope("job-1"))
    assert _hold(sdk, _slot(), "job-1") is False


def test_revoking_an_app_leaves_other_apps_grants_alone(tmp_path):
    _installed(tmp_path)
    so = so_mod.safety_override()
    assert so.activate_scoped("app:other-app:grant:job-1:autoapprove", "app:other-app", 600).active
    sdk = waa.WorkerAutoApprove(APP)
    assert _hold(sdk, _slot(), "job-1")

    assert waa.revoke_app(APP) == 1
    assert not _active(sdk.scope("job-1"))
    assert _active("app:other-app:grant:job-1:autoapprove")
