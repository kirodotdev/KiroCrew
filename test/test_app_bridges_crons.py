"""App bridge tests: app cron manifests and the CronService bridge."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source

from kiro_crew.apps.bridges import (
    _app_crons_path,
    _deregister_crons,
    _register_crons,
    load_app_cron_defs,
    register_app_crons_with_service,
)
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest


def _run(coro):
    """Drive an async bridge helper from a synchronous test.

    ``register_app_crons_with_service`` / ``deregister_app_crons_from_service``
    are async (they await the async CronSDK mutation API); these unit tests use
    mock services, so a one-shot event loop per call is sufficient.
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Cron registration
# ---------------------------------------------------------------------------


class TestCronRegistration:
    def test_register_crons(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )

        registered = _register_crons("test-app", manifest)
        assert len(registered) == 1
        assert "test-app/refresh" in registered

        defs = load_app_cron_defs("test-app")
        assert len(defs) == 1
        assert defs[0]["name"] == "test-app/refresh"
        assert defs[0]["every"] == 3600

    def test_register_crons_persists_enabled_flag(self, tmp_path, app_env):
        """A manifest cron shipped disabled keeps enabled:false in app-crons.json."""
        src = _make_app_source(
            tmp_path,
            crons=[
                {
                    "name": "nightly-run",
                    "cron_expr": "0 22 * * *",
                    "agent": "my-agent",
                    "enabled": False,
                }
            ],
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )

        _register_crons("test-app", manifest)
        defs = load_app_cron_defs("test-app")
        assert len(defs) == 1
        assert defs[0]["enabled"] is False

    @pytest.mark.parametrize("payload", ["{}", '{"name": "x"}', "42", '"str"', "null", "true"])
    def test_non_list_cron_manifest_is_ignored(self, payload, tmp_path, app_env):
        """Valid JSON that is not an array yields no definitions, not a crash.

        ``app-crons.json`` sits in the app's install directory, which is
        ordinary user-writable state. A non-array parses cleanly, so the
        ``JSONDecodeError`` guard never sees it and the value is returned
        under a ``list[dict]`` annotation that promised otherwise.
        """
        src = _make_app_source(tmp_path)
        install_app(src)
        _app_crons_path("test-app").write_text(payload, encoding="utf-8")

        assert load_app_cron_defs("test-app") == []

    def test_non_object_entries_are_skipped_and_the_rest_survive(self, tmp_path, app_env):
        """One malformed row must not cost the good rows beside it.

        Matches the disposition the registration loop already gives an entry
        whose ``add_job`` raises: skip that one, keep going.
        """
        src = _make_app_source(tmp_path)
        install_app(src)
        _app_crons_path("test-app").write_text(
            json.dumps([{"name": "test-app/good", "every": 60}, "junk", 7, None, []]),
            encoding="utf-8",
        )

        defs = load_app_cron_defs("test-app")
        assert defs == [{"name": "test-app/good", "every": 60}]

    def test_a_non_list_manifest_does_not_break_registration(self, tmp_path, app_env):
        """The end-to-end shape: enabling the app must not raise.

        ``register_app_crons_with_service`` reads ``d.get("name", "")`` OUTSIDE
        its per-job ``try``, so before the guard a JSON object here raised
        ``AttributeError`` (iterating an object yields its string keys) and a
        scalar raised ``TypeError`` -- out of the app-enable path entirely.
        """
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        src = _make_app_source(tmp_path)
        install_app(src)
        _app_crons_path("test-app").write_text(
            json.dumps({"name": "test-app/refresh", "every": 60}), encoding="utf-8"
        )

        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", MagicMock()))

        assert result == []
        mock_sdk.add_job_async.assert_not_called()

    def test_deregister_crons(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_crons("test-app", manifest)

        _deregister_crons("test-app")
        assert load_app_cron_defs("test-app") == []

    def test_no_crons(self, tmp_path, app_env):
        src = _make_app_source(tmp_path, crons=[])
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_crons("test-app", manifest)
        assert registered == []

    @pytest.mark.parametrize(
        "cut",
        [lambda whole: whole[: len(whole) // 2], lambda whole: b"\xff" + whole[1:]],
        ids=["truncated", "not-utf8"],
    )
    def test_an_unreadable_cron_manifest_is_reported_not_dropped_silently(
        self, cut, tmp_path, app_env, caplog
    ):
        """A torn manifest still loads as no crons, and the log says why."""
        import logging

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_crons("test-app", manifest)
        path = _app_crons_path("test-app")
        path.write_bytes(cut(path.read_bytes()))

        with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.bridges"):
            assert load_app_cron_defs("test-app") == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            str(path) in r.getMessage() for r in warnings
        ), "an unreadable cron manifest drops the app's crons without a word"

    def test_a_missing_cron_manifest_is_no_crons_without_a_warning(self, tmp_path, app_env, caplog):
        import logging

        src = _make_app_source(tmp_path)
        install_app(src)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.bridges"):
            assert load_app_cron_defs("test-app") == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_a_rewrite_that_fails_part_way_keeps_the_previous_manifest(
        self, tmp_path, app_env, monkeypatch
    ):
        """A disk that fills half way through a rewrite costs nothing already saved.

        The same fault is injected into whichever primitive puts the bytes down:
        half of them land, then the write fails with a full disk.
        """
        import errno

        from kiro_crew import atomic_write as atomic_mod

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_crons("test-app", manifest)
        before = load_app_cron_defs("test-app")
        assert [d["name"] for d in before] == ["test-app/refresh"]

        def _disk_full() -> OSError:
            return OSError(errno.ENOSPC, "No space left on device")

        real_write_text = Path.write_text
        real_write_all = atomic_mod._write_all

        def _half_write_text(self, data, *args, **kwargs):  # type: ignore[no-untyped-def]
            if self.name == "app-crons.json":
                real_write_text(self, data[: len(data) // 2], *args, **kwargs)
                raise _disk_full()
            return real_write_text(self, data, *args, **kwargs)

        def _half_write_all(fd, data, path):  # type: ignore[no-untyped-def]
            if Path(path).name == "app-crons.json":
                real_write_all(fd, data[: len(data) // 2], path)
                raise _disk_full()
            return real_write_all(fd, data, path)

        with monkeypatch.context() as patched:
            patched.setattr(Path, "write_text", _half_write_text)
            patched.setattr(atomic_mod, "_write_all", _half_write_all)
            with pytest.raises(OSError):
                _register_crons("test-app", manifest)

        assert load_app_cron_defs("test-app") == before

    @pytest.mark.asyncio
    async def test_register_with_running_service_arms_timer_on_loop(self, tmp_path, app_env):
        # register_app_crons_with_service is async: it awaits the async CronSDK
        # mutation API (add_job -> CronService.add_job_async), which offloads the
        # bounded store-lock spin to a worker thread and then arms the timer
        # IN-SERVICE on the loop. Driving it through a started CronService here
        # exercises that path end-to-end with NO caller-side drain step.
        from kiro_crew.cron import CronService

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_crons("test-app", manifest)  # persist the app-cron defs

        # Hermetic store under the isolated home (bare CronService() would bind
        # its crons.json at the process-default dir, leaking state across tests).
        svc = CronService(base_dir=app_env["home"] / "crons")
        await svc.start()
        try:
            registered = await register_app_crons_with_service("test-app", svc)
            assert "test-app/refresh" in registered
            # The job is fully added (owned) and the timer armed without error.
            assert any(j.name == "test-app/refresh" for j in svc.list_jobs())
            assert svc._timer_task is not None  # armed in-service, no drain call
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_register_offloads_lock_spin_and_arms_without_drain(self, tmp_path, app_env):
        # The async bridge awaits CronSDK.add_job -> CronService.add_job_async,
        # whose lock+persist runs in a worker thread (asyncio.to_thread) so the
        # bounded _file_lock spin never parks the gateway loop. The timer is
        # (re)armed by CronService ITSELF (in-service, via the bound loop) — so
        # no caller-side drain (the removed rearm_after_offload) is required.
        # This mirrors on_app_enable / on_gateway_startup.
        from kiro_crew.cron import CronService

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_crons("test-app", manifest)

        svc = CronService(base_dir=app_env["home"] / "crons")
        await svc.start()
        try:
            loop_thread = threading.get_ident()
            persist_threads: list[int] = []
            orig_persist = svc._persist_add_if_absent_locked

            def _track(predicate, job):  # type: ignore[no-untyped-def]
                persist_threads.append(threading.get_ident())
                return orig_persist(predicate, job)

            svc._persist_add_if_absent_locked = _track  # type: ignore[method-assign, assignment]

            registered = await register_app_crons_with_service("test-app", svc)
            assert "test-app/refresh" in registered
            # The store lock+persist ran OFF the event loop (offloaded).
            assert persist_threads and all(
                t != loop_thread for t in persist_threads
            ), "the store lock+persist must run in a worker thread, never on the loop"
            # Timer armed in-service, with no caller-side drain call.
            assert svc._timer_task is not None and not svc._timer_task.done()
            assert any(j.name == "test-app/refresh" for j in svc.list_jobs())
        finally:
            await svc.stop()


# ---------------------------------------------------------------------------
# Cron service bridge (register_app_crons_with_service)
# ---------------------------------------------------------------------------


class TestCronServiceBridge:
    """Tests for register_app_crons_with_service — promoting app crons to scheduler."""

    def _write_app_crons(self, tmp_path, app_name, cron_defs):
        """Write a fake app-crons.json for testing."""
        app_dir = tmp_path / "kirocrew-home" / "apps" / app_name
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "app-crons.json").write_text(json.dumps(cron_defs, indent=2))

    def test_boot_default_off_registers_no_third_party_crons(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        import kiro_crew.apps.execution as execution_mod

        self._write_app_crons(
            tmp_path,
            "test-app",
            [{"name": "test-app/refresh", "every": 60, "message": "go"}],
        )
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: False,
        )
        mock_sdk = MagicMock()

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", MagicMock()))

        assert result == []
        mock_sdk.add_job_if_absent_async.assert_not_called()

    def test_boot_explicit_allow_registers_third_party_crons(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        import kiro_crew.apps.execution as execution_mod

        self._write_app_crons(
            tmp_path,
            "test-app",
            [{"name": "test-app/refresh", "every": 60, "message": "go"}],
        )
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: True,
        )
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="job-id"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", MagicMock()))

        assert result == ["test-app/refresh"]
        mock_sdk.add_job_if_absent_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_boot_disarms_persisted_denied_app_crons_before_timer_start(
        self, tmp_path, app_env, monkeypatch
    ):
        from unittest.mock import MagicMock

        import kiro_crew.apps.bridges as bridges_mod
        import kiro_crew.apps.execution as execution_mod

        app_root = app_env["home"] / "apps" / "test-app"
        app_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            bridges_mod,
            "list_apps",
            lambda: [{"name": "test-app", "enabled": True}],
        )
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: False,
        )
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = []
        mock_service.remove_jobs_by_owner = AsyncMock(return_value=["job-id"])

        disarmed = await bridges_mod.reconcile_app_crons_for_execution(mock_service)

        assert disarmed == ["test-app"]
        mock_service.list_jobs.assert_called_once_with(include_disabled=True)
        mock_service.remove_jobs_by_owner.assert_awaited_once_with("app:test-app")

    @pytest.mark.asyncio
    async def test_boot_disarms_orphaned_app_cron_owner(self, app_env, monkeypatch):
        from unittest.mock import MagicMock

        import kiro_crew.apps.bridges as bridges_mod
        import kiro_crew.apps.execution as execution_mod

        events = []
        monkeypatch.setattr(bridges_mod, "list_apps", lambda: [])
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: True,
        )
        monkeypatch.setattr(
            bridges_mod,
            "sel",
            lambda: SimpleNamespace(log_api_access=lambda **kwargs: events.append(kwargs)),
        )
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = [SimpleNamespace(created_by="app:ghost-app")]
        mock_service.remove_jobs_by_owner = AsyncMock(return_value=["ghost-job"])

        disarmed = await bridges_mod.reconcile_app_crons_for_execution(mock_service)

        assert disarmed == ["ghost-app"]
        mock_service.list_jobs.assert_called_once_with(include_disabled=True)
        mock_service.remove_jobs_by_owner.assert_awaited_once_with("app:ghost-app")
        denial = [event for event in events if event.get("outcome") == "denied"]
        assert denial == [
            {
                "caller": "app_bridge",
                "operation": "app_execution_admission",
                "outcome": "denied",
                "resources": ("app=ghost-app action=cron_boot_restore provenance=unverified"),
                "error": "orphaned app cron owner has no installed app",
            }
        ]

    @pytest.mark.asyncio
    async def test_boot_keeps_shipped_builtin_app_cron_armed(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock

        import kiro_crew.apps.bridges as bridges_mod
        import kiro_crew.apps.execution as execution_mod

        shipped = tmp_path / "shipped-builtins"
        shipped_app = shipped / "builtin-app"
        shipped_app.mkdir(parents=True)
        (shipped_app / "app.json").write_text(
            json.dumps(
                {
                    "name": "builtin-app",
                    "version": "1.0.0",
                    "displayName": "Builtin App",
                    "description": "A test builtin app",
                    "author": "kirocrew",
                }
            )
        )
        monkeypatch.setattr(execution_mod, "_BUILTINS_DIR", shipped)
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: False,
        )
        monkeypatch.setattr(
            bridges_mod,
            "list_apps",
            lambda: [{"name": "builtin-app", "enabled": True}],
        )
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = [SimpleNamespace(created_by="app:builtin-app")]
        mock_service.remove_jobs_by_owner = AsyncMock(return_value=[])

        disarmed = await bridges_mod.reconcile_app_crons_for_execution(mock_service)

        assert disarmed == []
        mock_service.remove_jobs_by_owner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_boot_keeps_explicitly_admitted_third_party_cron_armed(
        self, app_env, monkeypatch
    ):
        from unittest.mock import MagicMock

        import kiro_crew.apps.bridges as bridges_mod
        import kiro_crew.apps.execution as execution_mod

        app_root = app_env["home"] / "apps" / "third-party-app"
        app_root.mkdir(parents=True)
        monkeypatch.setattr(
            bridges_mod,
            "list_apps",
            lambda: [{"name": "third-party-app", "enabled": True}],
        )
        monkeypatch.setattr(
            execution_mod,
            "third_party_execution_allowed",
            lambda: True,
        )
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = [SimpleNamespace(created_by="app:third-party-app")]
        mock_service.remove_jobs_by_owner = AsyncMock(return_value=[])

        disarmed = await bridges_mod.reconcile_app_crons_for_execution(mock_service)

        assert disarmed == []
        mock_service.remove_jobs_by_owner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execution_disarm_audit_failure_is_best_effort(self, monkeypatch):
        import kiro_crew.apps.bridges as bridges_mod

        def _audit_failure(**kwargs):
            raise OSError("audit unavailable")

        monkeypatch.setattr(
            bridges_mod,
            "sel",
            lambda: SimpleNamespace(log_api_access=_audit_failure),
        )
        mock_service = SimpleNamespace(remove_jobs_by_owner=AsyncMock(return_value=["job-id"]))

        removed = await bridges_mod.disarm_app_crons_for_execution(
            "test-app",
            mock_service,
        )

        assert removed == 1

    def test_registers_cron_with_all_fields(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/refresh",
                "every": 600,
                "cron_expr": "",
                "agent": "my-agent",
                "message": "do stuff",
                "app": "test-app",
                "agent_sequence": ["a1", "a2"],
                "env": {"FOO": "bar"},
                "persistent_session": False,
                "silent": True,
                "timezone": "America/New_York",
                "skip_dates": ["2026-12-25"],
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="abc123"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == ["test-app/refresh"]
        mock_sdk.add_job_if_absent_async.assert_called_once_with(
            name="test-app/refresh",
            message="do stuff",
            every_secs=600,
            cron_expr="",
            agent="my-agent",
            command="",
            script="",
            agent_sequence=["a1", "a2"],
            env={"FOO": "bar"},
            persistent_session=False,
            silent=True,
            enabled=True,
            timezone="America/New_York",
            skip_dates=["2026-12-25"],
            timeout=0,
            timeout_secs=0,
            strict_schedule=False,
            folder_id="",
            app_manifest_digest=ANY,
        )

    def test_cron_without_timezone_passes_the_empty_sentinel(self, tmp_path, app_env, monkeypatch):
        """A def that names no zone keeps today's config-then-UTC fallback."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        self._write_app_crons(
            tmp_path,
            "test-app",
            [{"name": "test-app/refresh", "every": 600, "message": "go"}],
        )

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="abc123"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            _run(register_app_crons_with_service("test-app", mock_cron_service))

        kwargs = mock_sdk.add_job_if_absent_async.call_args.kwargs
        assert kwargs["timezone"] == ""
        assert kwargs["skip_dates"] is None

    def test_disabled_cron_registers_paused(self, tmp_path, app_env, monkeypatch):
        """A manifest cron with enabled:false is passed through as enabled=False."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/nightly-run",
                "every": 0,
                "cron_expr": "0 22 * * *",
                "agent": "discovery",
                "message": "",
                "app": "test-app",
                "enabled": False,
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="abc123"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == ["test-app/nightly-run"]
        assert mock_sdk.add_job_if_absent_async.call_args.kwargs["enabled"] is False

    def test_legacy_defs_without_enabled_default_active(self, tmp_path, app_env, monkeypatch):
        """Pre-existing app-crons.json without the enabled key registers active."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/legacy",
                "every": 600,
                "agent": "a",
                "message": "m",
                "app": "test-app",
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="abc123"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert mock_sdk.add_job_if_absent_async.call_args.kwargs["enabled"] is True

    def test_startup_skips_existing_disabled_job(self, tmp_path, app_env, monkeypatch):
        """Gateway-startup re-registration must not re-add (and thus re-pause)
        a job that already exists in a disabled state.

        CronSDK.list_jobs() includes disabled jobs, so a paused job counts as
        existing — preserving a user's resume/pause state across restarts.
        """
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/nightly-run",
                "every": 0,
                "cron_expr": "0 22 * * *",
                "agent": "discovery",
                "message": "",
                "app": "test-app",
                "enabled": False,
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        existing = MagicMock()
        existing.name = "test-app/nightly-run"
        existing.enabled = False  # currently paused
        existing.user_paused = True

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = [existing]

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == []
        mock_sdk.add_job_if_absent_async.assert_not_called()
        # The existing job's state is untouched — no duplicate, no re-pause.
        assert existing.enabled is False
        assert existing.user_paused is True

    def test_registers_command_type_cron(self, tmp_path, app_env, monkeypatch):
        """Apps declaring command-type crons get them registered as command jobs."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/collect",
                "every": 60,
                "cron_expr": "",
                "agent": "",
                "message": "",
                "command": "python3 ~/.kirocrew/apps/test-app/scripts/collect.py",
                "script": "",
                "app": "test-app",
                "agent_sequence": [],
                "env": {},
                "persistent_session": False,
                "silent": True,
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="cmd123"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == ["test-app/collect"]
        mock_sdk.add_job_if_absent_async.assert_called_once_with(
            name="test-app/collect",
            message="",
            every_secs=60,
            cron_expr="",
            agent="",
            command="python3 ~/.kirocrew/apps/test-app/scripts/collect.py",
            script="",
            agent_sequence=None,
            env=None,
            persistent_session=False,
            silent=True,
            enabled=True,
            timezone="",
            skip_dates=None,
            timeout=0,
            timeout_secs=0,
            strict_schedule=False,
            folder_id="",
            app_manifest_digest=ANY,
        )

    def test_rejects_malicious_command(self, tmp_path, app_env, monkeypatch):
        """Commands blocked by _vet_shell_command are skipped with SEL audit."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/evil",
                "every": 60,
                "command": "cat ~/.aws/credentials",
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == []
        mock_sdk.add_job_if_absent_async.assert_not_called()

    def test_rejects_invalid_script_path(self, tmp_path, app_env, monkeypatch):
        """Scripts outside ~/.kirocrew/crons/ are rejected at registration."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {
                "name": "test-app/bad-script",
                "every": 60,
                "script": "/etc/passwd:run",
            }
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == []
        mock_sdk.add_job_if_absent_async.assert_not_called()

    def test_idempotent_skips_existing(self, tmp_path, app_env, monkeypatch):
        """A job stamped with the current manifest entry's digest is not touched."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import (
            app_cron_manifest_digest,
            register_app_crons_with_service,
        )

        cron_defs = [{"name": "test-app/refresh", "every": 600, "message": "go"}]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        existing_job = MagicMock()
        existing_job.name = "test-app/refresh"
        existing_job.app_manifest_digest = app_cron_manifest_digest(cron_defs[0])
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = [existing_job]

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == []
        mock_sdk.add_job_if_absent_async.assert_not_called()
        mock_sdk.update_job_async.assert_not_called()
        mock_sdk.remove_job_async.assert_not_called()

    def test_changed_manifest_updates_existing_in_place(self, tmp_path, app_env, monkeypatch):
        """A stamp from an older entry sends the current fields through update_job_async."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [{"name": "test-app/refresh", "every": 600, "message": "new words"}]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        existing_job = MagicMock(
            id="job-1", command="", script="", agent_sequence=[], env={}, enabled=True
        )
        existing_job.name = "test-app/refresh"
        existing_job.app_manifest_digest = "stale"
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = [existing_job]
        mock_sdk.update_job_async = AsyncMock(return_value=existing_job)

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", MagicMock()))

        assert result == [], "a re-applied job is not newly registered"
        mock_sdk.add_job_if_absent_async.assert_not_called()
        mock_sdk.remove_job_async.assert_not_called()
        kwargs = mock_sdk.update_job_async.call_args.kwargs
        assert mock_sdk.update_job_async.call_args.args == ("job-1",)
        assert kwargs["message"] == "new words"
        assert kwargs["every_secs"] == 600
        assert kwargs["app_manifest_digest"] not in ("", "stale")
        # The pause state is the person's: never sent by the manifest path.
        assert "enabled" not in kwargs and "user_paused" not in kwargs
        # Create-only fields never reach the update gate.
        assert not {"command", "script", "agent_sequence", "env"} & set(kwargs)

    def test_returns_empty_when_no_cron_service(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import register_app_crons_with_service

        result = _run(register_app_crons_with_service("test-app", None))
        assert result == []

    def test_returns_empty_when_no_app_crons_file(self, tmp_path, app_env):
        from unittest.mock import MagicMock

        from kiro_crew.apps.bridges import register_app_crons_with_service

        result = _run(register_app_crons_with_service("nonexistent-app", MagicMock()))
        assert result == []

    def test_handles_malformed_entry_gracefully(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {"name": "", "every": 600, "message": "bad"},  # empty name — skipped
            {"name": "test-app/good", "every": 300, "message": "ok"},
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="x"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        assert result == ["test-app/good"]

    def test_register_crons_serializes_all_fields(self, tmp_path, app_env):
        """Verify _register_crons writes all CronEntry fields to app-crons.json."""
        from kiro_crew.apps.bridges import _register_crons, load_app_cron_defs

        manifest = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="",
            author="t",
            crons=[],
        )
        # Manually construct a CronEntry with all fields set
        from kiro_crew.apps.manifest import CronEntry

        entry = CronEntry(
            name="refresh",
            every=600,
            agent="my-agent",
            message="go",
            agent_sequence=["a1"],
            env={"K": "V"},
            persistent_session=False,
            silent=True,
            timezone="America/New_York",
            skip_dates=["2026-12-25"],
        )
        manifest.crons = [entry]

        _register_crons("test-app", manifest)
        defs = load_app_cron_defs("test-app")

        assert len(defs) == 1
        d = defs[0]
        assert d["agent_sequence"] == ["a1"]
        assert d["env"] == {"K": "V"}
        assert d["persistent_session"] is False
        assert d["silent"] is True
        # Without these the declared zone is dropped between the manifest and
        # the scheduler, and the job fires in UTC.
        assert d["timezone"] == "America/New_York"
        assert d["skip_dates"] == ["2026-12-25"]

    def test_add_job_exception_logged_and_skipped(self, tmp_path, app_env):
        """Exception from CronSDK.add_job is caught, logged, and execution continues."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {"name": "test-app/bad", "every": 600, "message": "x"},
            {"name": "test-app/good", "every": 300, "message": "y"},
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        # First call raises, second succeeds
        mock_sdk.add_job_if_absent_async = AsyncMock(
            side_effect=[RuntimeError("boom"), MagicMock(id="ok")]
        )

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(register_app_crons_with_service("test-app", mock_cron_service))

        # Failed entry skipped, good entry registered
        assert result == ["test-app/good"]
        assert mock_sdk.add_job_if_absent_async.call_count == 2


class TestCronServiceDeregister:
    """Tests for deregister_app_crons_from_service — scheduler cleanup helper."""

    def test_returns_zero_when_no_cron_service(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import deregister_app_crons_from_service

        assert _run(deregister_app_crons_from_service("test-app", None)) == 0

    def test_calls_remove_all_and_returns_count(self, tmp_path, app_env):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import deregister_app_crons_from_service

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.remove_all_async = AsyncMock(return_value=3)

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(deregister_app_crons_from_service("test-app", mock_cron_service))

        assert result == 3
        mock_sdk.remove_all_async.assert_called_once()

    def test_returns_zero_on_exception(self, tmp_path, app_env):
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.bridges import deregister_app_crons_from_service

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.remove_all_async = AsyncMock(side_effect=RuntimeError("scheduler unavailable"))

        with patch("kiro_crew.apps.bridges.CronSDK", return_value=mock_sdk):
            result = _run(deregister_app_crons_from_service("test-app", mock_cron_service))

        assert result == 0  # exception swallowed, zero returned
