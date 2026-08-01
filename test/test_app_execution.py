"""Security tests for the central third-party App Kit execution boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    _write_installed,
    install_app,
)


def _install_test_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "execution-test-app",
    enabled: bool = False,
    origin: str = "registry",
    manifest_extra: dict[str, Any] | None = None,
) -> Any:
    home = tmp_path / "kirocrew-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    source = tmp_path / "source" / name
    source.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Execution Test App",
        "description": "Exercises the app execution boundary",
        "author": "tester",
        **(manifest_extra or {}),
    }
    (source / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    assert install_app(source).ok
    meta = _read_installed(name)
    assert meta is not None
    meta.enabled = enabled
    meta.origin = origin
    _write_installed(name, meta)
    return home


def _route_app() -> web.Application:
    from kiro_crew.apps.routes import register_app_routes

    app = web.Application()
    register_app_routes(app)
    return app


class TestExecutionDecision:
    def test_absent_config_defaults_to_denied(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.execution import third_party_execution_allowed

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        assert third_party_execution_allowed() is False

    def test_explicit_boolean_true_admits(self, monkeypatch) -> None:
        from kiro_crew.apps import execution
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setattr(
            KiroCrewConfig,
            "load",
            classmethod(
                lambda cls: SimpleNamespace(agent=SimpleNamespace(apps_allow_third_party=True))
            ),
        )
        assert execution.third_party_execution_allowed() is True

    @pytest.mark.parametrize("value", ["true", "1", 1, object()])
    def test_truthy_non_boolean_values_do_not_admit(self, monkeypatch, value) -> None:
        from kiro_crew.apps import execution
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setattr(
            KiroCrewConfig,
            "load",
            classmethod(
                lambda cls: SimpleNamespace(agent=SimpleNamespace(apps_allow_third_party=value))
            ),
        )
        assert execution.third_party_execution_allowed() is False

    def test_environment_variable_cannot_override_absent_policy(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.apps.execution import third_party_execution_allowed

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setenv("KIROCREW_APPS_ALLOW_THIRD_PARTY", "true")
        assert third_party_execution_allowed() is False

    def test_config_load_failure_fails_closed(self, monkeypatch) -> None:
        from kiro_crew.apps import execution
        from kiro_crew.config.loader import KiroCrewConfig

        def _raise(cls):
            raise OSError("unreadable config")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_raise))
        assert execution.third_party_execution_allowed() is False
        assert execution.app_execution_denied(
            "untrusted-app", action="module_load"
        )

    def test_shipped_builtin_name_and_path_are_both_required(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.apps import execution

        monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: False)
        shipped_root = execution.shipped_builtin_app_root("file-explorer")
        assert shipped_root is not None
        assert execution.is_builtin_app(app_root=shipped_root)
        assert (
            execution.app_execution_denied(
                "file-explorer",
                action="backend_spawn",
                app_root=shipped_root,
            )
            is None
        )
        assert execution.app_execution_denied(
            "forged-builtin",
            action="backend_spawn",
            app_root=shipped_root,
        )

        mutable_root = tmp_path / "file-explorer"
        mutable_root.mkdir()
        assert execution.app_execution_denied(
            "file-explorer",
            action="backend_spawn",
            app_root=mutable_root,
        )

    def test_edition_manifest_source_builtin_is_admitted_with_containment(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.platform as platform_mod
        from kiro_crew.apps import execution

        source = tmp_path / "edition-builtins"
        shipped_root = source / "edition-app"
        shipped_root.mkdir(parents=True)
        (shipped_root / "app.json").write_text(
            json.dumps({"name": "edition-app"}),
            encoding="utf-8",
        )
        context = SimpleNamespace(
            apps_loader=SimpleNamespace(manifest_sources=lambda: [source])
        )
        monkeypatch.setattr(platform_mod, "current_context", lambda: context)
        monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: False)

        assert execution.shipped_builtin_app_root("edition-app") == shipped_root
        assert execution.app_execution_denied(
            "edition-app",
            action="resource_register",
            app_root=shipped_root,
        ) is None

        outside_root = tmp_path / "outside" / "edition-app"
        outside_root.mkdir(parents=True)
        assert execution.app_execution_denied(
            "edition-app",
            action="resource_register",
            app_root=outside_root,
        )

    def test_denial_is_audited_with_action_and_app(self, monkeypatch) -> None:
        from kiro_crew.apps import execution

        events: list[dict[str, Any]] = []
        fake_sel = SimpleNamespace(log_api_access=lambda **kwargs: events.append(kwargs))
        monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: False)
        monkeypatch.setattr(execution, "sel", lambda: fake_sel)

        reason = execution.app_execution_denied(
            "audit-app", action="open_command", caller="dashboard"
        )

        assert reason
        assert len(events) == 1
        assert events[0]["operation"] == "app_execution_admission"
        assert events[0]["outcome"] == "denied"
        assert "app=audit-app" in events[0]["resources"]
        assert "action=open_command" in events[0]["resources"]

    def test_allowed_with_working_audit_emits_event(self, monkeypatch) -> None:
        from kiro_crew.apps import execution

        events: list[dict[str, Any]] = []
        fake_sel = SimpleNamespace(log_api_access=lambda **kwargs: events.append(kwargs))
        monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: True)
        monkeypatch.setattr(execution, "sel", lambda: fake_sel)

        reason = execution.app_execution_denied(
            "audit-app", action="open_command", caller="dashboard"
        )

        assert reason is None
        assert events == [
            {
                "caller": "dashboard",
                "operation": "app_execution_admission",
                "outcome": "allowed",
                "resources": "app=audit-app action=open_command provenance=unverified",
            }
        ]

    def test_allowed_with_broken_audit_still_executes(self, monkeypatch) -> None:
        from kiro_crew.apps import execution

        def _audit_failure(**kwargs) -> None:
            raise OSError("audit unavailable")

        fake_sel = SimpleNamespace(log_api_access=_audit_failure)
        monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: True)
        monkeypatch.setattr(execution, "sel", lambda: fake_sel)

        reason = execution.app_execution_denied(
            "audit-app", action="module_load"
        )

        assert reason is None

    def test_denial_with_broken_audit_stays_denied(self, monkeypatch) -> None:
        from kiro_crew.apps import execution

        def _audit_failure(**kwargs) -> None:
            raise OSError("audit unavailable")

        fake_sel = SimpleNamespace(log_api_access=_audit_failure)
        monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: False)
        monkeypatch.setattr(execution, "sel", lambda: fake_sel)

        reason = execution.app_execution_denied(
            "audit-app", action="module_load"
        )

        assert reason
        assert "third-party app execution is disabled" in reason


class TestLaunchAndLifecycleBoundary:
    @pytest.mark.asyncio
    async def test_disabled_app_cannot_open_even_when_execution_is_admitted(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.apps.routes as routes
        from kiro_crew.apps import execution

        _install_test_app(
            tmp_path,
            monkeypatch,
            manifest_extra={"openCommand": "echo should-not-run"},
        )
        monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: True)

        async def _unexpected_spawn(*args, **kwargs):
            pytest.fail("disabled app open attempted to spawn")

        monkeypatch.setattr(routes, "create_subprocess_limited", _unexpected_spawn)
        async with TestClient(TestServer(_route_app())) as client:
            response = await client.post("/api/apps/execution-test-app/open")
            assert response.status == 409
            body = await response.json()
            assert body["code"] == "app_disabled"
            assert "disabled" in body["error"]

    @pytest.mark.asyncio
    async def test_default_off_refuses_open_before_spawn(self, tmp_path, monkeypatch) -> None:
        import kiro_crew.apps.routes as routes

        _install_test_app(
            tmp_path,
            monkeypatch,
            enabled=True,
            manifest_extra={"openCommand": "echo should-not-run"},
        )
        monkeypatch.setenv("DISPLAY", ":99")

        async def _unexpected_spawn(*args, **kwargs):
            pytest.fail("openCommand spawned while execution was disabled")

        monkeypatch.setattr(routes, "create_subprocess_limited", _unexpected_spawn)
        async with TestClient(TestServer(_route_app())) as client:
            response = await client.post("/api/apps/execution-test-app/open")
            assert response.status == 403
            body = await response.json()
            assert body["code"] == "app_execution_denied"
            assert "execution is disabled" in body["error"]

    @pytest.mark.asyncio
    async def test_explicit_admission_allows_open(self, tmp_path, monkeypatch) -> None:
        import kiro_crew.apps.routes as routes
        from kiro_crew.apps import execution

        _install_test_app(
            tmp_path,
            monkeypatch,
            enabled=True,
            manifest_extra={"openCommand": "echo admitted"},
        )
        monkeypatch.setenv("DISPLAY", ":99")
        monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: True)
        monkeypatch.setattr(routes, "wrap_argv", lambda argv, **kwargs: (argv, None))
        monkeypatch.setattr(routes, "cgroup_scope_argv", lambda argv: argv)
        calls: list[list[str]] = []

        async def _spawn(*argv, **kwargs):
            calls.append(list(argv))
            return SimpleNamespace(pid=1234)

        monkeypatch.setattr(routes, "create_subprocess_limited", _spawn)
        async with TestClient(TestServer(_route_app())) as client:
            response = await client.post("/api/apps/execution-test-app/open")
            assert response.status == 200
            assert (await response.json())["pid"] == 1234
        assert calls

    @pytest.mark.parametrize("name", ["forged-builtin", "file-explorer"])
    @pytest.mark.asyncio
    async def test_forged_builtin_origin_does_not_exempt_mutable_open_command(
        self, tmp_path, monkeypatch, name
    ) -> None:
        import kiro_crew.apps.routes as routes

        _install_test_app(
            tmp_path,
            monkeypatch,
            name=name,
            enabled=True,
            origin="builtin",
            manifest_extra={"openCommand": "echo should-not-run"},
        )

        async def _unexpected_spawn(*args, **kwargs):
            pytest.fail("forged builtin provenance spawned an openCommand")

        monkeypatch.setattr(routes, "create_subprocess_limited", _unexpected_spawn)
        async with TestClient(TestServer(_route_app())) as client:
            response = await client.post(f"/api/apps/{name}/open")
            assert response.status == 403
            body = await response.json()
            assert body["code"] == "app_execution_denied"

    @pytest.mark.asyncio
    async def test_lifecycle_script_default_off_has_no_process_side_effect(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.apps.routes as routes

        _install_test_app(tmp_path, monkeypatch)

        async def _unexpected_spawn(*args, **kwargs):
            pytest.fail("lifecycle script spawned while execution was disabled")

        monkeypatch.setattr(routes, "create_subprocess_limited", _unexpected_spawn)
        result = await routes._run_lifecycle_script(
            "execution-test-app", "echo should-not-run", action="on_enable"
        )
        assert result["failed"] is True
        assert result["denied"] is True

    @pytest.mark.asyncio
    async def test_lifecycle_script_runs_after_explicit_admission(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.apps.routes as routes
        from kiro_crew.apps import execution

        _install_test_app(tmp_path, monkeypatch)
        monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: True)
        monkeypatch.setattr(routes, "wrap_argv", lambda argv, **kwargs: (argv, None))
        monkeypatch.setattr(routes, "cgroup_scope_argv", lambda argv: argv)

        class _Process:
            pid = 55
            returncode = 0

            async def communicate(self):
                return b"admitted\n", None

        async def _spawn(*argv, **kwargs):
            return _Process()

        monkeypatch.setattr(routes, "create_subprocess_limited", _spawn)
        result = await routes._run_lifecycle_script(
            "execution-test-app", "echo admitted", action="on_enable"
        )
        assert result == {"output": "admitted", "failed": False}

    @pytest.mark.asyncio
    async def test_enable_denial_rolls_back_before_any_side_effect(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.apps.routes as routes

        _install_test_app(
            tmp_path,
            monkeypatch,
            manifest_extra={
                "backend": {"entryPoint": "server.py", "port": "auto"},
                "setup": {"onEnable": "echo should-not-run"},
                "dependencies": {"commands": ["missing-command"]},
            },
        )

        def _unexpected(*args, **kwargs):
            pytest.fail("enable performed a side effect after execution denial")

        async def _unexpected_async(*args, **kwargs):
            pytest.fail("enable performed an async side effect after execution denial")

        monkeypatch.setattr(routes, "register_app", _unexpected)
        monkeypatch.setattr(routes, "start_app_backend", _unexpected)
        monkeypatch.setattr(routes, "_resolve_deps", _unexpected_async)
        monkeypatch.setattr(routes, "_run_lifecycle_script", _unexpected_async)
        monkeypatch.setattr(routes, "on_app_enable", _unexpected_async)

        async with TestClient(TestServer(_route_app())) as client:
            response = await client.post("/api/apps/execution-test-app/enable")
            assert response.status == 400
            body = await response.json()
            assert "execution policy" in body["error"]

        meta = _read_installed("execution-test-app")
        assert meta is not None
        assert meta.enabled is False


class TestRegistryAndProvenanceBoundary:
    @pytest.mark.asyncio
    async def test_registry_install_script_is_denied_before_clone_or_build(
        self, tmp_path, monkeypatch
    ) -> None:
        import kiro_crew.apps.registry as registry

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        entry = {
            "name": "registry-app",
            "repo": "https://github.com/example/registry-app.git",
            "gitUrl": "https://github.com/example/registry-app.git",
            "branch": "main",
        }
        manifest = {
            "name": "registry-app",
            "version": "1.0.0",
            "displayName": "Registry App",
            "description": "registry app",
            "setup": {"onInstall": "echo should-not-run"},
        }
        monkeypatch.setattr(registry, "get_registry_app", lambda name: entry)

        async def _fetch(*args, **kwargs):
            return manifest

        async def _unexpected_build(*args, **kwargs):
            pytest.fail("registry cloned/built while execution was disabled")

        monkeypatch.setattr(registry, "_fetch_app_manifest", _fetch)
        monkeypatch.setattr(registry, "app_admission_denied", lambda *args, **kwargs: None)
        monkeypatch.setattr(registry, "_clone_build_app", _unexpected_build)

        result = await registry.install_from_registry("registry-app")
        assert result["ok"] is False
        assert "execution policy" in result["error"]

    @pytest.mark.asyncio
    async def test_registry_detect_script_default_off_has_no_process_side_effect(
        self, monkeypatch
    ) -> None:
        from kiro_crew.apps import execution, registry

        entry = {
            "name": "registry-detect-app",
            "repo": "https://example.com/registry-detect-app.git",
            "detectInstalled": "echo should-not-run",
        }
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [entry])

        async def _no_external_registries():
            return []

        async def _identity_manifest(item):
            return item

        monkeypatch.setattr(registry, "_load_external_registries", _no_external_registries)
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
        monkeypatch.setattr(registry, "_resolve_manifest", _identity_manifest)
        monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: False)

        async def _unexpected_spawn(*args, **kwargs):
            pytest.fail("registry detectInstalled spawned while execution was disabled")

        monkeypatch.setattr(registry, "create_subprocess_limited", _unexpected_spawn)
        apps = await registry.list_registry()
        assert [app["name"] for app in apps] == ["registry-detect-app"]

    def test_external_registration_cannot_claim_builtin_provenance(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.apps.manager import register_external_app

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        result = register_external_app(
            "spoofed-builtin",
            "1.0.0",
            "Spoofed Builtin",
            origin="builtin",
        )
        assert result.ok is False
        assert "reserved" in result.error
        assert _read_installed("spoofed-builtin") is None
