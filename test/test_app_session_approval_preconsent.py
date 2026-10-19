"""Re-consent for installs that predate the ``sessionApproval`` consent marker.

A host upgrade changes the parser, not the manifest. An app installed while
``permissions.sessionApproval`` was ignored can already declare it, and its
``installed.json`` has no ``sessionApprovalConsentPending`` key. These tests
write that pre-feature record by hand, then cross the boundary.
"""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

from kiro_crew import agent
from kiro_crew.apps import manager as manager_mod
from kiro_crew.apps import session_consent
from kiro_crew.apps.manager import (
    INSTALLED_META_FILENAME,
    app_dir,
    enable_app,
    get_app,
    install_app,
    mark_session_approval_consent_pending,
)
from kiro_crew.apps.permissions import app_can_manage_session_approvals
from kiro_crew.apps.teardown import TeardownResult
from kiro_crew.testing.wait import async_wait_until, await_until

_KEY = "sessionApprovalConsentPending"


def _write_pre_feature_record(name: str, *, enabled: bool = True) -> None:
    """Rewrite *name*'s record as an older host would have: no consent key."""
    path = app_dir(name) / INSTALLED_META_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop(_KEY, None)
    data["enabled"] = enabled
    path.write_text(json.dumps(data), encoding="utf-8")


def _raw_record(name: str) -> dict:
    return json.loads((app_dir(name) / INSTALLED_META_FILENAME).read_text(encoding="utf-8"))


def _install(tmp_path, **permissions) -> None:
    result = install_app(_make_app_source(tmp_path, permissions=permissions))
    assert result.ok, result.error


@pytest.fixture(name="teardowns")
def teardowns_fixture(monkeypatch):
    """Record each teardown and the record's ``enabled`` on disk at that moment."""
    calls: list[tuple[str, bool]] = []

    async def _fake(name, record, **_kw):
        calls.append((name, _raw_record(name)["enabled"]))
        return TeardownResult(warnings=[], failures=[])

    monkeypatch.setattr("kiro_crew.apps.teardown.teardown_app_runtime", _fake)
    monkeypatch.setattr(agent, "rebuild_agent_config", lambda **_kw: None)
    return calls


def _boot() -> list[str]:
    """Run the gateway's post-readiness reconcile step."""
    return asyncio.run(session_consent.reconcile_after_ready())


def _first_run_setup(monkeypatch, tmp_path) -> None:
    """Run the pre-readiness first-run step with the unrelated steps stubbed out."""
    marker = tmp_path / "stale-mcp-marker"
    marker.write_text("done\n", encoding="utf-8")
    monkeypatch.setattr(agent, "ensure_kirocrew_on_path", lambda: None)
    monkeypatch.setattr(agent, "_stale_mcp_purge_marker", lambda: marker)
    monkeypatch.setattr("kiro_crew.platform.admission.seed_default_policy", lambda: False)
    monkeypatch.setattr(manager_mod, "backfill_default_on_builtins", lambda: [])
    monkeypatch.setattr("kiro_crew.skills.remove_retired_conductor_skill", lambda: False)
    agent.run_first_run_setup()


class TestPreFeatureSessionApproval:
    def test_grant_is_refused_before_boot_reconciles(self, tmp_path, app_home):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)

        assert app_can_manage_session_approvals("test-app") is False
        assert get_app("test-app")["sessionApprovalConsentPending"] is True

    def test_a_later_manifest_read_cannot_clear_pending(self, tmp_path, app_home, monkeypatch):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)
        real = manager_mod.get_app_manifest("test-app")
        reads = iter([real, None, real, real])
        monkeypatch.setattr(manager_mod, "get_app_manifest", lambda _name: next(reads))
        monkeypatch.setattr(
            "kiro_crew.apps.permissions.get_app_manifest", manager_mod.get_app_manifest
        )

        assert app_can_manage_session_approvals("test-app") is False

    def test_first_run_setup_leaves_the_scan_to_after_readiness(
        self, tmp_path, app_home, monkeypatch
    ):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)

        _first_run_setup(monkeypatch, tmp_path)

        assert _KEY not in _raw_record("test-app")
        assert app_can_manage_session_approvals("test-app") is False

    def test_boot_disables_and_marks_pending_until_consent(
        self, tmp_path, app_home, monkeypatch, teardowns
    ):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)
        audits = []
        monkeypatch.setattr(
            manager_mod,
            "sel",
            lambda: type("S", (), {"log_api_access": lambda _s, **kw: audits.append(kw)})(),
        )

        assert _boot() == ["test-app"]

        raw = _raw_record("test-app")
        assert raw["enabled"] is False
        assert raw[_KEY] is True
        assert app_can_manage_session_approvals("test-app") is False
        assert [(a["caller"], a["operation"]) for a in audits] == [
            ("app_boot", "session_approval_widened")
        ]
        blocked = enable_app("test-app")
        assert not blocked.ok
        assert blocked.error_code == "session_approval_consent_required"
        assert app_can_manage_session_approvals("test-app") is False

        assert enable_app("test-app", session_approval_consent=True).ok
        assert app_can_manage_session_approvals("test-app") is True
        # Handled once: the next start leaves the consented app alone.
        assert _boot() == []
        assert _raw_record("test-app")["enabled"] is True

    def test_boot_stops_a_running_app_before_marking_it(self, tmp_path, app_home, teardowns):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)

        _boot()

        # The disable route's teardown ran while the record still read enabled.
        assert teardowns == [("test-app", True)]

    def test_boot_skips_teardown_for_an_app_already_off(self, tmp_path, app_home, teardowns):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=False)

        assert _boot() == ["test-app"]
        assert teardowns == []
        assert enable_app("test-app").error_code == "session_approval_consent_required"

    def test_boot_rebuilds_the_agent_spec_only_when_it_disabled_an_app(
        self, tmp_path, app_home, monkeypatch, teardowns
    ):
        rebuilds = []
        monkeypatch.setattr(agent, "rebuild_agent_config", lambda **kw: rebuilds.append(kw))
        _install(tmp_path, sessionApproval=True)
        assert enable_app("test-app", session_approval_consent=True).ok

        _boot()
        assert rebuilds == []

        _write_pre_feature_record("test-app", enabled=True)
        _boot()
        assert rebuilds == [{"refresh_forks": "defer"}]

    def test_a_failed_spec_rebuild_keeps_the_app_disabled(
        self, tmp_path, app_home, monkeypatch, teardowns
    ):
        def _fail(**_kw):
            raise OSError("spec write refused")

        monkeypatch.setattr(agent, "rebuild_agent_config", _fail)
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)

        assert _boot() == ["test-app"]
        assert _raw_record("test-app")["enabled"] is False

    def test_an_uninstall_holding_the_lock_is_not_undone(self, tmp_path, app_home, teardowns):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)

        async def _race() -> list[str]:
            lock = manager_mod.app_lifecycle_lock("test-app")
            async with lock:
                task = asyncio.create_task(session_consent.reconcile_after_ready())
                await async_wait_until(
                    lambda: task.done() or bool(lock._locks[asyncio.get_running_loop()]._waiters),
                    describe=lambda: "reconcile never queued on the app lifecycle lock",
                )
                assert not task.done()
                shutil.rmtree(app_dir("test-app"))
            return await await_until(
                task, what="reconcile did not finish after the lock was released"
            )

        assert asyncio.run(_race()) == []
        assert not (app_dir("test-app") / INSTALLED_META_FILENAME).exists()
        assert teardowns == []

    def test_an_app_consented_while_waiting_is_not_torn_down(self, tmp_path, app_home, teardowns):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)

        async def _race() -> list[str]:
            lock = manager_mod.app_lifecycle_lock("test-app")
            async with lock:
                task = asyncio.create_task(session_consent.reconcile_after_ready())
                await async_wait_until(
                    lambda: task.done() or bool(lock._locks[asyncio.get_running_loop()]._waiters),
                    describe=lambda: "reconcile never queued on the app lifecycle lock",
                )
                assert not task.done()
                assert enable_app("test-app", session_approval_consent=True).ok
            return await await_until(
                task, what="reconcile did not finish after the lock was released"
            )

        assert asyncio.run(_race()) == []
        assert teardowns == []
        assert _raw_record("test-app")["enabled"] is True

    def test_consent_on_an_enabled_pre_feature_record_is_written(self, tmp_path, app_home):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)

        assert enable_app("test-app").error_code == "session_approval_consent_required"
        assert enable_app("test-app", session_approval_consent=True).ok
        assert _raw_record("test-app")[_KEY] is False
        assert app_can_manage_session_approvals("test-app") is True

    def test_a_stuck_startup_hook_defers_the_app(self, tmp_path, app_home, monkeypatch, teardowns):
        async def _still_running(_name, *, bounded):
            assert bounded is True
            return False

        monkeypatch.setattr(
            "kiro_crew.apps.hooks_integration.stop_retained_startup_hooks", _still_running
        )
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)

        assert _boot() == []
        assert teardowns == []
        assert _KEY not in _raw_record("test-app")
        assert app_can_manage_session_approvals("test-app") is False

    def test_a_pending_record_saved_before_boot_is_still_disabled(
        self, tmp_path, app_home, teardowns
    ):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)
        # Another writer reads the key-less record (pending on read) and saves it
        # before the gateway's first start.
        meta = manager_mod._read_installed("test-app")
        manager_mod._write_installed("test-app", meta)
        raw = _raw_record("test-app")
        assert raw["enabled"] is True and raw[_KEY] is True

        assert _boot() == ["test-app"]
        assert teardowns == [("test-app", True)]
        assert _raw_record("test-app")["enabled"] is False
        assert enable_app("test-app").error_code == "session_approval_consent_required"

    def test_a_save_with_a_broken_manifest_keeps_consent_needed(self, tmp_path, app_home):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)
        manifest_path = app_dir("test-app") / manager_mod.APP_MANIFEST_FILENAME
        good = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text("{ not json", encoding="utf-8")

        assert manager_mod.disable_app("test-app").ok
        assert _raw_record("test-app")[_KEY] is True

        manifest_path.write_text(good, encoding="utf-8")
        assert enable_app("test-app").error_code == "session_approval_consent_required"
        assert app_can_manage_session_approvals("test-app") is False
        assert enable_app("test-app", session_approval_consent=True).ok
        assert app_can_manage_session_approvals("test-app") is True

    def test_an_update_after_a_failed_manifest_read_keeps_consent_needed(
        self, tmp_path, app_home, monkeypatch
    ):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)
        real = manager_mod.get_app_manifest
        calls = {"n": 0}

        def _first_read_fails(name):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(name)

        monkeypatch.setattr(manager_mod, "get_app_manifest", _first_read_fails)
        v2 = _make_app_source(
            tmp_path / "v2", version="2.0.0", permissions={"sessionApproval": True}
        )
        assert manager_mod.update_app(v2).ok
        monkeypatch.setattr(manager_mod, "get_app_manifest", real)

        assert _raw_record("test-app")[_KEY] is True
        assert app_can_manage_session_approvals("test-app") is False
        assert enable_app("test-app").error_code == "session_approval_consent_required"

    def test_a_failed_reregistration_restores_the_key_less_record(
        self, tmp_path, app_home, monkeypatch
    ):
        manifest = {"name": "ext-app", "version": "1.0.0", "permissions": {"sessionApproval": True}}
        assert manager_mod.register_external_app(
            "ext-app", "1.0.0", "Ext", manifest_data=manifest
        ).ok
        _write_pre_feature_record("ext-app", enabled=True)
        before = (app_dir("ext-app") / INSTALLED_META_FILENAME).read_text(encoding="utf-8")
        real_read, real_write = manager_mod.get_app_manifest, manager_mod.atomic_write
        calls = {"n": 0}

        def _first_read_fails(name):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_read(name)

        def _manifest_write_fails(path, *args, **kwargs):
            if path.name == manager_mod.APP_MANIFEST_FILENAME and "api" in str(args[0]):
                raise OSError("manifest write failed")
            return real_write(path, *args, **kwargs)

        monkeypatch.setattr(manager_mod, "get_app_manifest", _first_read_fails)
        monkeypatch.setattr(manager_mod, "atomic_write", _manifest_write_fails)
        widened = {
            **manifest,
            "version": "1.1.0",
            "permissions": {"sessionApproval": True, "api": ["GET /x"]},
        }
        assert not manager_mod.register_external_app(
            "ext-app", "1.1.0", "Ext", manifest_data=widened
        ).ok
        monkeypatch.setattr(manager_mod, "get_app_manifest", real_read)
        monkeypatch.setattr(manager_mod, "atomic_write", real_write)

        assert (app_dir("ext-app") / INSTALLED_META_FILENAME).read_text(encoding="utf-8") == before
        assert app_can_manage_session_approvals("ext-app") is False
        assert enable_app("ext-app").error_code == "session_approval_consent_required"

    def test_a_stale_copy_written_back_cannot_clear_pending_on_disk(self, tmp_path, app_home):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)
        stale = manager_mod._read_installed("test-app")
        stale.sessionApprovalConsentPending = False  # a copy read before pending was known
        manager_mod._write_installed("test-app", stale)  # another writer saves first...
        assert _raw_record("test-app")[_KEY] is True
        manager_mod._write_installed("test-app", stale)  # ...then the stale copy again
        assert _raw_record("test-app")[_KEY] is True
        assert app_can_manage_session_approvals("test-app") is False

        manager_mod._write_installed("test-app", stale, session_approval_consent=True)
        assert _raw_record("test-app")[_KEY] is False

    def test_an_app_with_no_manifest_is_not_made_to_ask(self, tmp_path, app_home, teardowns):
        assert manager_mod.register_external_app("ext-plain", "1.0.0", "Plain").ok
        assert not (app_dir("ext-plain") / manager_mod.APP_MANIFEST_FILENAME).exists()
        _write_pre_feature_record("ext-plain", enabled=True)

        assert manager_mod.register_external_app("ext-plain", "1.0.0", "Plain").ok
        assert _raw_record("ext-plain").get(_KEY) is not True
        assert _boot() == []
        assert teardowns == []
        assert _raw_record("ext-plain")["enabled"] is True

    def test_a_save_while_the_manifest_is_gone_records_no_consent(self, tmp_path, app_home):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)
        manifest_path = app_dir("test-app") / manager_mod.APP_MANIFEST_FILENAME
        good = manifest_path.read_text(encoding="utf-8")
        manifest_path.unlink()

        manager_mod._write_installed("test-app", manager_mod._read_installed("test-app"))
        assert _KEY not in _raw_record("test-app")

        manifest_path.write_text(good, encoding="utf-8")
        assert app_can_manage_session_approvals("test-app") is False
        assert enable_app("test-app").error_code == "session_approval_consent_required"

    def test_a_manifest_returning_during_a_save_records_no_consent(
        self, tmp_path, app_home, monkeypatch
    ):
        from kiro_crew.apps import manager_records

        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)
        manifest_path = app_dir("test-app") / manager_mod.APP_MANIFEST_FILENAME
        good = manifest_path.read_text(encoding="utf-8")
        manifest_path.unlink()
        meta = manager_mod._read_installed("test-app")
        real_lexists = manager_records.os.path.lexists

        def _manifest_returns(path):
            seen = real_lexists(path)
            if str(path) == str(manifest_path) and not seen:
                manifest_path.write_text(good, encoding="utf-8")
            return seen

        monkeypatch.setattr(manager_records.os.path, "lexists", _manifest_returns)
        manager_mod._write_installed("test-app", meta)
        monkeypatch.setattr(manager_records.os.path, "lexists", real_lexists)

        assert manifest_path.exists()
        assert _raw_record("test-app").get(_KEY) is not False
        assert app_can_manage_session_approvals("test-app") is False
        assert enable_app("test-app").error_code == "session_approval_consent_required"

    def test_a_save_of_a_no_grant_record_records_no_consent_step(self, tmp_path, app_home):
        _install(tmp_path)
        _write_pre_feature_record("test-app", enabled=True)

        assert manager_mod.disable_app("test-app").ok
        assert _raw_record("test-app")[_KEY] is False
        assert enable_app("test-app").ok

    def test_marking_an_uninstalled_app_recreates_nothing(self, tmp_path, app_home):
        _install(tmp_path, sessionApproval=True)
        _write_pre_feature_record("test-app", enabled=True)
        shutil.rmtree(app_dir("test-app"))

        assert mark_session_approval_consent_pending("test-app") is False
        assert not (app_dir("test-app") / INSTALLED_META_FILENAME).exists()

    def test_marking_a_consented_app_leaves_it_alone(self, tmp_path, app_home):
        _install(tmp_path, sessionApproval=True)
        assert enable_app("test-app", session_approval_consent=True).ok

        assert mark_session_approval_consent_pending("test-app") is False
        assert _raw_record("test-app")["enabled"] is True

    def test_pre_feature_app_without_the_grant_is_untouched(self, tmp_path, app_home, teardowns):
        _install(tmp_path)
        _write_pre_feature_record("test-app", enabled=True)

        assert _boot() == []
        raw = _raw_record("test-app")
        assert raw["enabled"] is True
        assert _KEY not in raw
        assert get_app("test-app")["sessionApprovalConsentPending"] is False

    def test_record_with_the_key_is_trusted_as_written(self, tmp_path, app_home, teardowns):
        _install(tmp_path, sessionApproval=True)
        assert enable_app("test-app", session_approval_consent=True).ok

        assert _boot() == []
        assert app_can_manage_session_approvals("test-app") is True


def test_gateway_schedules_the_reconcile_after_readiness():
    import inspect

    from kiro_crew.slack.gateway_runtime import service_boot

    source = inspect.getsource(service_boot.run)
    ready = source.index('print(f"KIROCREW_READY:')
    scheduled = source.index("asyncio.create_task(reconcile_after_ready())")
    assert ready < scheduled
    assert "reconcile_after_ready" not in inspect.getsource(service_boot._init_services)
