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

    def test_builtin_app_names_requires_builtin_owned_install(
        self, tmp_path, monkeypatch
    ) -> None:
        # A shipped manifest makes a name a CANDIDATE; it enters the first-party
        # set only when its active installed record is builtin-owned. Both a
        # core builtin and an edition/companion-contributed builtin count, so
        # long as the builtin actually occupies the slot.
        import kiro_crew.platform as platform_mod
        from kiro_crew.apps import execution
        from kiro_crew.apps.manager import InstalledApp, _write_installed

        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        source = tmp_path / "edition-builtins"
        shipped_root = source / "edition-app"
        shipped_root.mkdir(parents=True)
        (shipped_root / "app.json").write_text(json.dumps({"name": "edition-app"}), encoding="utf-8")
        context = SimpleNamespace(apps_loader=SimpleNamespace(manifest_sources=lambda: [source]))
        monkeypatch.setattr(platform_mod, "current_context", lambda: context)

        # Builtin registration won the slot for both the edition builtin and a
        # real core builtin → builtin-owned installed records.
        _write_installed(
            "edition-app", InstalledApp(name="edition-app", source="builtin", origin="builtin")
        )
        _write_installed(
            "file-explorer", InstalledApp(name="file-explorer", source="builtin", origin="builtin")
        )

        names = execution.builtin_app_names()
        assert isinstance(names, frozenset)
        assert "edition-app" in names  # edition/companion source, builtin-owned
        assert "file-explorer" in names  # core builtin, builtin-owned

    def test_builtin_app_names_excludes_shadowing_user_app(self, tmp_path, monkeypatch) -> None:
        # A user-installed app that shares a builtin's name makes registration
        # stand down and keeps its own (non-builtin) installed record. The
        # shipped manifest still exists, but the name must NOT be trusted as
        # first-party — otherwise the shadowing app's own-server MCP calls would
        # be auto-approved without a prompt.
        import kiro_crew.platform as platform_mod
        from kiro_crew.apps import execution
        from kiro_crew.apps.manager import InstalledApp, _write_installed

        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        source = tmp_path / "edition-builtins"
        shipped_root = source / "shadowed-app"
        shipped_root.mkdir(parents=True)
        (shipped_root / "app.json").write_text(
            json.dumps({"name": "shadowed-app"}), encoding="utf-8"
        )
        context = SimpleNamespace(apps_loader=SimpleNamespace(manifest_sources=lambda: [source]))
        monkeypatch.setattr(platform_mod, "current_context", lambda: context)

        # Pre-existing third-party install occupies the name (source/origin are
        # NOT builtin), exactly as register_builtin_apps() leaves it on stand-down.
        _write_installed(
            "shadowed-app",
            InstalledApp(name="shadowed-app", source="registry:evil", origin="registry"),
        )

        assert "shadowed-app" not in execution.builtin_app_names()

    def test_builtin_app_names_excludes_unregistered_manifest(self, tmp_path, monkeypatch) -> None:
        # A shipped manifest with NO installed record at all (registration has
        # not run, or the record is unreadable) fails closed: the name is not
        # trusted until a builtin-owned install proves it occupies the slot.
        import kiro_crew.platform as platform_mod
        from kiro_crew.apps import execution

        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        source = tmp_path / "edition-builtins"
        shipped_root = source / "unregistered-app"
        shipped_root.mkdir(parents=True)
        (shipped_root / "app.json").write_text(
            json.dumps({"name": "unregistered-app"}), encoding="utf-8"
        )
        context = SimpleNamespace(apps_loader=SimpleNamespace(manifest_sources=lambda: [source]))
        monkeypatch.setattr(platform_mod, "current_context", lambda: context)

        assert "unregistered-app" not in execution.builtin_app_names()

    def test_builtin_app_mcp_servers_declared_and_owned(self, tmp_path, monkeypatch) -> None:
        # builtin_app_mcp_servers emits <app>:<server> for every mcpServers key a
        # builtin-owned shipped manifest declares; a shadowing user app (install
        # not builtin-owned) contributes nothing.
        import kiro_crew.platform as platform_mod
        from kiro_crew.apps import execution
        from kiro_crew.apps.manager import InstalledApp, _write_installed

        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        source = tmp_path / "edition-builtins"
        # Builtin-owned app declaring two MCP servers.
        owned_root = source / "edition-app"
        owned_root.mkdir(parents=True)
        (owned_root / "app.json").write_text(
            json.dumps({"name": "edition-app", "mcpServers": {"srv": {}, "srv2": {}}}),
            encoding="utf-8",
        )
        # Shadowed app: shipped manifest declares a server, but the active
        # install is third-party → must NOT contribute.
        shadow_root = source / "shadowed-app"
        shadow_root.mkdir(parents=True)
        (shadow_root / "app.json").write_text(
            json.dumps({"name": "shadowed-app", "mcpServers": {"evil": {}}}),
            encoding="utf-8",
        )
        context = SimpleNamespace(apps_loader=SimpleNamespace(manifest_sources=lambda: [source]))
        monkeypatch.setattr(platform_mod, "current_context", lambda: context)

        _write_installed(
            "edition-app", InstalledApp(name="edition-app", source="builtin", origin="builtin")
        )
        _write_installed(
            "shadowed-app",
            InstalledApp(name="shadowed-app", source="registry:evil", origin="registry"),
        )

        servers = execution.builtin_app_mcp_servers()
        assert "edition-app:srv" in servers
        assert "edition-app:srv2" in servers
        assert "shadowed-app:evil" not in servers  # shadowing install excluded

    def test_builtin_app_agents_maps_declared_agents_to_owning_app(
        self, tmp_path, monkeypatch
    ) -> None:
        # builtin_app_agents maps every agent a builtin-owned shipped manifest
        # declares to its app, under both the bare and `app--agent` spellings. A
        # shadowing user app contributes nothing, and a name two apps both
        # declare is dropped entirely (ambiguous provenance must grant neither).
        import kiro_crew.platform as platform_mod
        from kiro_crew.apps import execution
        from kiro_crew.apps.manager import InstalledApp, _write_installed

        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        source = tmp_path / "edition-builtins"

        def _app(name: str, agents: dict[str, str]) -> None:
            root = source / name
            (root / "agents").mkdir(parents=True)
            (root / "app.json").write_text(
                json.dumps({"name": name, "agents": [f"agents/{f}" for f in agents]}),
                encoding="utf-8",
            )
            for filename, agent_name in agents.items():
                (root / "agents" / filename).write_text(
                    json.dumps({"name": agent_name}), encoding="utf-8"
                )

        _app("edition-app", {"main.json": "edition-main", "bg.json": "clash"})
        _app("other-app", {"main.json": "clash"})  # same agent name → ambiguous
        _app("shadowed-app", {"main.json": "shadow-main"})

        context = SimpleNamespace(apps_loader=SimpleNamespace(manifest_sources=lambda: [source]))
        monkeypatch.setattr(platform_mod, "current_context", lambda: context)

        for name in ("edition-app", "other-app"):
            _write_installed(name, InstalledApp(name=name, source="builtin", origin="builtin"))
        _write_installed(
            "shadowed-app",
            InstalledApp(name="shadowed-app", source="registry:evil", origin="registry"),
        )

        agents = execution.builtin_app_agents()
        assert agents["edition-main"] == "edition-app"
        assert agents["edition-app--edition-main"] == "edition-app"
        # Ambiguous across two builtins → neither app's identity is granted.
        assert "clash" not in agents
        # Per-app link spelling stays unambiguous, so it survives.
        assert agents["edition-app--clash"] == "edition-app"
        # Shadowing install contributes nothing.
        assert "shadow-main" not in agents

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
        assert "app='audit-app'" in events[0]["resources"]
        assert "action='open_command'" in events[0]["resources"]

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
                "resources": "app='audit-app' action='open_command' provenance=unverified",
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
        from kiro_crew.apps import lifecycle_scripts

        _install_test_app(tmp_path, monkeypatch)

        async def _unexpected_spawn(*args, **kwargs):
            pytest.fail("lifecycle script spawned while execution was disabled")

        # Patched where the runner now LIVES, not where it is re-exported. The
        # function moved to `lifecycle_scripts` (so `teardown.py` can run
        # `onDisable` without importing `routes` back), which means its globals
        # resolve there. Left on `routes` this guard still passes — but vacuously,
        # because a real spawn would reach the unpatched original instead of
        # failing the test. A guard that cannot fire is worse than no guard.
        monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _unexpected_spawn)
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

        # Patched on `lifecycle_scripts`, not `routes`: the runner moved there so
        # that `teardown.py` can run `onDisable` without importing `routes` back
        # (a cycle). `routes._run_lifecycle_script` is still the same function by
        # alias, but its module globals now resolve in its new home — patching
        # `routes.wrap_argv` here would silently miss and run the real sandbox.
        from kiro_crew.apps import execution, lifecycle_scripts

        _install_test_app(tmp_path, monkeypatch)
        monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: True)
        monkeypatch.setattr(
            lifecycle_scripts, "wrap_argv", lambda argv, **kwargs: (argv, None)
        )
        monkeypatch.setattr(lifecycle_scripts, "cgroup_scope_argv", lambda argv: argv)

        class _Process:
            pid = 55
            returncode = 0

            async def communicate(self):
                return b"admitted\n", None

        async def _spawn(*argv, **kwargs):
            return _Process()

        monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _spawn)
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


class TestTrustedGrantBounds:
    """``execution.trusted_app_names`` bounds — the gate's per-decision cost.

    The set is rebuilt on EVERY execution decision (each hook load, backend
    spawn, bridge registration and boot-reconcile iteration), so an unbounded
    name or an unbounded list turns a hand-edited ``config.json`` into a cost
    multiplier on the hot path. The caps are a DoS bound, not a naming rule:
    a too-long NAME is dropped (it can name no real app), while a too-long LIST
    is TRUNCATED rather than emptied, because ``apps_trusted`` is append-ordered
    — the operator's real grants sit at the front, and denying the whole list
    would revoke them all over someone else's junk.
    """

    @staticmethod
    def _seed(tmp_path, monkeypatch, entries: list) -> None:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        (home / "config.json").write_text(
            json.dumps({"agent": {"apps_trusted": entries}}), encoding="utf-8"
        )

    def test_name_at_the_cap_is_still_a_grant(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.execution import _MAX_GRANT_NAME_LEN, trusted_app_names

        at_cap = "a" * _MAX_GRANT_NAME_LEN
        self._seed(tmp_path, monkeypatch, [at_cap])
        # The cap is inclusive — bounding cost must not silently revoke a
        # legitimate (if absurdly named) grant one character early.
        assert at_cap in trusted_app_names()

    def test_over_long_name_is_not_a_grant(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.execution import (
            _MAX_GRANT_NAME_LEN,
            app_execution_denied,
            trusted_app_names,
        )

        over = "a" * (_MAX_GRANT_NAME_LEN + 1)
        self._seed(tmp_path, monkeypatch, [over])
        assert over not in trusted_app_names()
        # Postcondition, not just the set: the gate still DENIES that name.
        assert app_execution_denied(over, action="module_load") is not None

    def test_over_long_list_is_truncated_not_honoured_whole(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.apps.execution import _MAX_GRANT_ENTRIES, trusted_app_names

        entries = [f"app-{i:04d}" for i in range(_MAX_GRANT_ENTRIES + 50)]
        self._seed(tmp_path, monkeypatch, entries)
        effective = trusted_app_names()

        # Bounded: the work per execution decision cannot grow without limit.
        assert len(effective) == _MAX_GRANT_ENTRIES
        # Truncated from the TAIL — the operator's earliest (real) grants survive.
        assert entries[0] in effective
        assert entries[_MAX_GRANT_ENTRIES - 1] in effective
        assert entries[_MAX_GRANT_ENTRIES] not in effective
        assert entries[-1] not in effective

    def test_over_long_list_still_admits_a_grant_at_the_front(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.apps.execution import (
            _MAX_GRANT_ENTRIES,
            app_execution_denied,
        )

        real = "real-grant-app"
        padding = [f"junk-{i:04d}" for i in range(_MAX_GRANT_ENTRIES + 50)]
        self._seed(tmp_path, monkeypatch, [real, *padding])
        # Truncation is not a denial: the real grant is still enforced.
        assert app_execution_denied(real, action="module_load") is None
        assert app_execution_denied(padding[-1], action="module_load") is not None


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
