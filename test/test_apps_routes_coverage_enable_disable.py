"""Coverage tests for the app enable and disable routes.

Pins who may enable an app and grant its consent, the warning and rollback branches of
enable and disable, repeated and concurrent toggles, the ``on_disable`` hook lookup and
name normalization for hyphenated builtins, and that enable does not re-register MCP
after the backend starts.
``test_apps_routes_coverage.py`` holds install, registration and the app views, and
names the other route modules; the shared helpers live in
``apps_routes_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import platform as platform_mod
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import (
    APP,
    _install,
    _make_app,
    _make_app_source,
    _setup_env,
)

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import AppResult, disable_app, enable_app, register_external_app
from kiro_crew.testing.wait import await_until

# ---------------------------------------------------------------------------
# Enable / disable warning + rollback branches
# ---------------------------------------------------------------------------


class TestEnableRefusesAppTokens:
    """Enabling is the consent moment for ``permissions.sessionApproval``.

    ``disable_app`` leaves the app's token valid and ``_app_owns_path`` grants
    the token its own ``/api/apps/{name}/**`` namespace, so without a refusal
    a disabled app -- including one an update left disabled because it newly
    asked for session control -- could POST its own enable route and restore
    the grant with no user moment.
    """

    @pytest.mark.asyncio
    async def test_own_app_token_cannot_enable_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, permissions={"sessionApproval": True})
        assert disable_app(APP).ok
        async with TestClient(TestServer(_make_app(app_identity=APP))) as client:
            resp = await client.post(f"/api/apps/{APP}/enable")
            assert resp.status == 403
            assert (await resp.json())["code"] == "app_token_forbidden"
        assert routes_mod.get_app(APP)["enabled"] is False

    @pytest.mark.asyncio
    async def test_dashboard_caller_still_enables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, permissions={"sessionApproval": True})
        assert disable_app(APP).ok
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            # A fresh install that declares the grant is consent-pending, so the
            # owner enables it through the disclosure surface.
            resp = await client.post(
                f"/api/apps/{APP}/enable", json={"sessionApprovalConsent": True}
            )
            assert resp.status == 200
        assert routes_mod.get_app(APP)["enabled"] is True

    @pytest.mark.asyncio
    async def test_grants_consent_must_name_the_entries_shown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bare flag cannot say what the owner saw, so it approves nothing;
        # the object form approves exactly the entries it names.
        from kiro_crew.apps.manager import enable_app, update_app

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, permissions={"api": ["/api/sessions"]})
        assert enable_app(APP).ok
        v2 = tmp_path / "v2"
        v2.mkdir()
        assert update_app(
            _make_app_source(
                v2, version="2.0.0", permissions={"api": ["/api/sessions", "/api/memory"]}
            )
        ).ok
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/enable", json={"grantsConsent": True})
            assert resp.status == 200
            assert routes_mod.get_app(APP)["approvedGrants"]["api"] == ["/api/sessions"]
            resp = await client.post(
                f"/api/apps/{APP}/enable",
                json={"grantsConsent": {"api": ["/api/memory"], "events": []}},
            )
            assert resp.status == 200
        assert routes_mod.get_app(APP)["approvedGrants"]["api"] == ["/api/sessions", "/api/memory"]

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_user_cannot_consent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Consent hands an app control of the OWNER's sessions, so a signed-in
        # non-owner dashboard user is refused exactly like the other
        # machine-global mutations (403 owner_only), and nothing is enabled.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, permissions={"sessionApproval": True})
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="guest"))) as client:
            resp = await client.post(
                f"/api/apps/{APP}/enable", json={"sessionApprovalConsent": True}
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "owner_only"
        assert routes_mod.get_app(APP)["enabled"] is False
        assert routes_mod.get_app(APP)["sessionApprovalConsentPending"] is True

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_user_cannot_enable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Enabling registers the app's agents, skills, MCP servers and crons and
        # starts its backend, so a signed-in non-owner dashboard user is refused
        # with 403 owner_only even without a consent body, and nothing runs.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, setup={"onEnable": "echo should-not-run"})
        reached: list[str] = []

        async def _script(*args: Any, **kwargs: Any) -> dict[str, Any]:
            reached.append("on_enable_script")
            return {"output": "", "failed": False}

        monkeypatch.setattr(routes_mod, "_run_lifecycle_script", _script)
        monkeypatch.setattr(
            routes_mod, "start_app_backend", lambda n: reached.append("start_app_backend")
        )
        async with TestClient(TestServer(_make_app(dashboard_user="guest"))) as client:
            resp = await client.post(f"/api/apps/{APP}/enable")
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "owner_only"
        assert reached == []
        assert routes_mod.get_app(APP)["enabled"] is False

    @pytest.mark.asyncio
    async def test_pending_consent_requires_disclosure_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        result = register_external_app(
            APP,
            "1.0.0",
            "Consent App",
            manifest_data={
                "name": APP,
                "version": "1.0.0",
                "permissions": {"sessionApproval": True},
            },
        )
        assert result.notice == "session_approval_reconsent"
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            denied = await client.post(f"/api/apps/{APP}/enable")
            assert denied.status == 400
            assert (await denied.json())["code"] == "session_approval_consent_required"
            accepted = await client.post(
                f"/api/apps/{APP}/enable",
                json={"sessionApprovalConsent": True},
            )
            assert accepted.status == 200
        assert routes_mod.get_app(APP)["enabled"] is True
        assert routes_mod.get_app(APP)["sessionApprovalConsentPending"] is False


class TestEnableBranches:
    #: An OS that is never the host, so the app under test is always
    #: "unsupported here". Hardcoding "windows" made this pass on Linux and fail
    #: on Windows (there the platform IS supported, so onEnable runs and trips
    #: the guard below with a 500). Derive it instead.
    _FOREIGN_OS = "linux" if platform_mod.system() == "Windows" else "windows"

    @pytest.mark.asyncio
    async def test_client_app_script_skipped_on_unsupported_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A client-install app declares a desktop payload for another OS; its
        # onEnable can only fail here, so it must be skipped, not executed.
        _setup_env(tmp_path, monkeypatch)
        _install(
            tmp_path,
            setup={"onEnable": "open /Applications/Nope.app"},
            platform={"os": [self._FOREIGN_OS], "installMode": "client"},
        )

        async def _must_not_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("onEnable must not run on an unsupported platform")

        monkeypatch.setattr(routes_mod, "_run_lifecycle_script", _must_not_run)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/enable")
            assert resp.status == 200
            body = await resp.json()
        assert body["onEnable"]["skipped"] == "unsupported_platform"

    @pytest.mark.asyncio
    async def test_failed_on_enable_rolls_back_to_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, setup={"onEnable": "setup.sh"})

        async def _failed(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"output": "boom", "failed": True}

        monkeypatch.setattr(routes_mod, "_run_lifecycle_script", _failed)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/enable")
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "on_enable_failed"
            # The rollback really left the app disabled.
            resp = await client.get(f"/api/apps/{APP}")
            assert (await resp.json())["enabled"] is False

    @pytest.mark.asyncio
    async def test_hook_failure_becomes_a_warning_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)

        async def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("hook exploded")

        monkeypatch.setattr(routes_mod, "on_app_enable", _boom)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/enable")
            assert resp.status == 200
            body = await resp.json()
        assert any("hooks failed" in w for w in body["warnings"])

    @pytest.mark.asyncio
    async def test_health_status_issues_are_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)

        async def _hooks(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "health_status": {
                    "issues": ["cannot reach token=ghp_0123456789abcdefghijABCDEFGHIJ0123456789"]
                }
            }

        monkeypatch.setattr(routes_mod, "on_app_enable", _hooks)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/enable")
            assert resp.status == 200
            issues = (await resp.json())["hooks"]["health_status"]["issues"]
        assert "ghp_0123456789abcdefghijABCDEFGHIJ0123456789" not in issues[0]

    @pytest.mark.asyncio
    async def test_enable_of_unknown_app_is_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/ghost/enable")
            assert resp.status == 404


class TestDisableBranches:
    @pytest.mark.asyncio
    async def test_live_detached_startup_hook_refuses_disable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[tuple[str, bool]] = []

        async def _stop_retained(app_name: str, *, bounded: bool) -> bool:
            calls.append((app_name, bounded))
            return False

        monkeypatch.setattr(routes_mod, "stop_retained_startup_hooks", _stop_retained)

        async def _must_not_teardown(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("disable must not wait on retained startup work")

        def _must_not_disable(*args: Any, **kwargs: Any) -> AppResult:
            raise AssertionError("disable metadata must remain unchanged on refusal")

        monkeypatch.setattr(routes_mod, "teardown_app_runtime", _must_not_teardown)
        monkeypatch.setattr(routes_mod, "disable_app", _must_not_disable)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/disable")
            assert resp.status == 409
            body = await resp.json()
            app_resp = await client.get(f"/api/apps/{APP}")
            assert (await app_resp.json())["enabled"] is True
        assert body["code"] == "startup_hook_still_running"
        assert body["retryable"] is True
        assert calls == [(APP, True)]

    @pytest.mark.asyncio
    async def test_not_installed_is_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/ghost/disable")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_failed_on_disable_warns_but_still_disables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, setup={"onDisable": "teardown.sh"})
        enable_app(APP)

        async def _failed(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "output": "failed with token=ghp_0123456789abcdefghijABCDEFGHIJ0123456789",
                "failed": True,
            }

        # Patched on the SHARED teardown, not on `routes`: this PR routes the
        # disable path's hook/backend/onDisable work through
        # `apps/teardown.py::teardown_app_runtime`, the one implementation the
        # trust-revocation path also calls, so `routes` does not hold these
        # symbols. The behaviour these tests pin is unchanged — the warnings still
        # surface on the disable response — only the module that owns the step moved.
        from kiro_crew.apps import teardown as teardown_mod

        # Patched on `teardown`, NOT on `lifecycle_scripts`: teardown does
        # `from ... import run_lifecycle_script`, so it holds its own binding and a
        # patch on the defining module never reaches it. Getting this wrong passed
        # locally for the wrong reason — the real runner executed, the missing
        # script failed, and the expected warning appeared anyway — while on a CI
        # host with no unprivileged userns the sandbox raised instead, producing
        # "could not be run" rather than "script failed".
        monkeypatch.setattr(teardown_mod, "run_lifecycle_script", _failed)
        monkeypatch.setattr(teardown_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/disable")
            assert resp.status == 200
            body = await resp.json()
            warnings = body["warnings"]
            assert any("onDisable script failed" in w for w in warnings)
            assert not any("ghp_0123456789abcdefghijABCDEFGHIJ0123456789" in w for w in warnings)
            resp = await client.get(f"/api/apps/{APP}")
            assert (await resp.json())["enabled"] is False

    @pytest.mark.asyncio
    async def test_hook_failure_and_cron_cleanup_surface_as_warnings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)

        async def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("disable hook exploded")

        # Patched on the SHARED teardown, not on `routes`: this PR routes the
        # disable path's hook/backend/onDisable work through
        # `apps/teardown.py::teardown_app_runtime`, the one implementation the
        # trust-revocation path also calls, so `routes` does not hold these
        # symbols. The behaviour these tests pin is unchanged — the warnings still
        # surface on the disable response — only the module that owns the step moved.
        from kiro_crew.apps import teardown as teardown_mod

        monkeypatch.setattr(teardown_mod, "on_app_disable", _boom)
        monkeypatch.setattr(teardown_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/disable")
            assert resp.status == 200
            assert any("hooks disable failed" in w for w in (await resp.json())["warnings"])

    @pytest.mark.asyncio
    async def test_cron_cleanup_message_is_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)

        async def _hooks(*args: Any, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["bounded_startup_cleanup"] is False
            return {"cron_cleanup": "2 job(s) left enabled", "other": 1}

        # Patched on the SHARED teardown, not on `routes`: this PR routes the
        # disable path's hook/backend/onDisable work through
        # `apps/teardown.py::teardown_app_runtime`, the one implementation the
        # trust-revocation path also calls, so `routes` does not hold these
        # symbols. The behaviour these tests pin is unchanged — the warnings still
        # surface on the disable response — only the module that owns the step moved.
        from kiro_crew.apps import teardown as teardown_mod

        monkeypatch.setattr(teardown_mod, "on_app_disable", _hooks)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/disable")
            assert (await resp.json())["warnings"] == ["2 job(s) left enabled"]

    @pytest.mark.asyncio
    async def test_disable_failure_is_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        monkeypatch.setattr(
            routes_mod,
            "disable_app",
            lambda name: AppResult(ok=False, name=name, error="metadata locked"),
        )
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/disable")
            assert resp.status == 400
            assert (await resp.json())["error"] == "metadata locked"


class TestRepeatedToggleIsIdempotent:
    """A repeated enable skips the Python hooks; overlapping disables run app code once."""

    @pytest.mark.asyncio
    async def test_repeated_enable_runs_hooks_once_and_repeats_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, setup={"onEnable": "setup.sh"})
        calls: list[str] = []

        async def _script(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append("onEnable")
            return {"output": "", "failed": False}

        async def _hooks(*args: Any, **kwargs: Any) -> None:
            calls.append("hooks")

        monkeypatch.setattr(routes_mod, "_run_lifecycle_script", _script)
        monkeypatch.setattr(routes_mod, "on_app_enable", _hooks)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("backend"))
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            first = await client.post(f"/api/apps/{APP}/enable")
            assert first.status == 200
            assert calls == ["backend", "onEnable", "hooks"]
            second = await client.post(f"/api/apps/{APP}/enable")
            assert second.status == 200
            assert (await second.json())["message"] == f"{APP} is already enabled"
        # The flag is not evidence onEnable ran, so the script and backend start repeat.
        assert calls == ["backend", "onEnable", "hooks", "backend", "onEnable"]

    @pytest.mark.asyncio
    async def test_concurrent_disables_run_on_disable_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, setup={"onDisable": "teardown.sh"})
        enable_app(APP)
        from kiro_crew.apps import teardown as teardown_mod

        both_read = asyncio.Event()
        loop = asyncio.get_running_loop()
        real_get_app = routes_mod.get_app
        reads: list[str] = []

        def _get_app(name: str) -> Any:
            # The route reads the record on a worker thread: append is atomic, and
            # the event is set from the loop's own thread.
            reads.append(name)
            if len(reads) >= 2:
                loop.call_soon_threadsafe(both_read.set)
            return real_get_app(name)

        scripts: list[str] = []

        async def _script(*args: Any, **kwargs: Any) -> dict[str, Any]:
            scripts.append("onDisable")
            # Hold the lock until the second request has read its pre-lock metadata.
            await await_until(both_read, what="the second request's pre-lock read")
            return {"output": "", "failed": False}

        monkeypatch.setattr(routes_mod, "get_app", _get_app)
        monkeypatch.setattr(teardown_mod, "run_lifecycle_script", _script)
        monkeypatch.setattr(teardown_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            first, second = await asyncio.gather(
                client.post(f"/api/apps/{APP}/disable"),
                client.post(f"/api/apps/{APP}/disable"),
            )
            assert (first.status, second.status) == (200, 200)
            messages = {(await r.json())["message"] for r in (first, second)}
        assert messages == {f"disabled {APP}", f"{APP} is already disabled"}
        assert scripts == ["onDisable"]


def test_on_disable_hook_resolves_hyphenated_builtin_names():
    """`on_disable` must be reachable for multi-word builtins.

    The disable route receives the MANIFEST name (`code-review-sage`), while
    `BUILTIN_NAMES` and the package dirs use underscores. Without normalizing,
    the membership test fails and the documented hook silently never fires — for
    every builtin whose name has more than one word, which is nearly all of them.
    """
    import importlib

    from kiro_crew.apps.builtins import BUILTIN_NAMES

    hyphenated = [n for n in BUILTIN_NAMES if "_" in n]
    assert hyphenated, "expected multi-word builtins to exist"
    for module_name in hyphenated:
        manifest_name = module_name.replace("_", "-")
        # What the route computes from the manifest name must land on the package.
        assert manifest_name.replace("-", "_") == module_name
        importlib.import_module(f"kiro_crew.apps.builtins.{module_name}")


def test_disable_route_normalizes_the_name_before_the_builtin_lookup():
    """Pins the normalization in the route itself, not just the name algebra."""
    import inspect

    from kiro_crew.apps import routes

    src = inspect.getsource(routes.handle_disable_app)
    assert 'name.replace("-", "_")' in src, (
        "the disable handler must normalize the manifest name before testing "
        "membership in BUILTIN_NAMES / importing the package"
    )


@pytest.mark.asyncio
async def test_enable_does_not_re_register_after_the_backend_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enable must not re-register MCP after start_app_backend returns.

    A call made here is queued behind the handler, so the adopted backend's watch can
    demote and scrub in between — and the queued write would then restore the dead url,
    without updating `mcp_healthy`, so the watch would never notice the disagreement.
    The adoption path registers through the serialized transition instead, before its
    watch is armed.
    """
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    called: list[str] = []
    import kiro_crew.apps.bridges as bridges_mod

    monkeypatch.setattr(
        bridges_mod,
        "reregister_app_mcp_servers",
        lambda name, live_port=None, io_failures=None: called.append(name) or [],
    )
    monkeypatch.setattr(
        routes_mod,
        "start_app_backend",
        lambda name: SimpleNamespace(healthy=True, port=7999, to_dict=lambda: {"port": 7999}),
    )

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/enable", json={})
        assert resp.status == 200

    assert called == [], "enable re-registered after start; the adoption path owns that"


class TestRecordWritersRunOffTheLoop:
    """The enable and disable routes write the record from a worker thread.

    The record lock is single-shot on the event-loop thread, so a writer called
    there would fail on ordinary contention with another writer of the app.
    """

    @pytest.mark.asyncio
    async def test_enable_and_disable_write_off_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        on_loop: list[tuple[str, bool]] = []
        real_enable, real_disable = routes_mod.enable_app, routes_mod.disable_app

        def _running_on_loop() -> bool:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return False
            return True

        def _enable(*args: Any, **kwargs: Any) -> AppResult:
            on_loop.append(("enable", _running_on_loop()))
            return real_enable(*args, **kwargs)

        def _disable(*args: Any, **kwargs: Any) -> AppResult:
            on_loop.append(("disable", _running_on_loop()))
            return real_disable(*args, **kwargs)

        monkeypatch.setattr(routes_mod, "enable_app", _enable)
        monkeypatch.setattr(routes_mod, "disable_app", _disable)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            assert (await client.post(f"/api/apps/{APP}/enable")).status == 200
            assert (await client.post(f"/api/apps/{APP}/disable")).status == 200
        assert on_loop == [("enable", False), ("disable", False)]


class TestRecordReadsRunOffTheLoop:
    """The enable, disable and open routes read the app record from a worker thread.

    ``get_app`` reads installed.json and parses app.json, and ``app_enabled_state``
    stats and reads installed.json. Called on the event loop, either read stalls
    every gateway task until the disk answers.
    """

    @pytest.mark.asyncio
    async def test_enable_disable_and_open_read_the_record_off_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        reads: list[tuple[str, bool]] = []

        def _running_on_loop() -> bool:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return False
            return True

        def _spy(attr: str) -> Any:
            real = getattr(routes_mod, attr)

            def _read(name: str) -> Any:
                reads.append((attr, _running_on_loop()))
                return real(name)

            return _read

        for attr in ("get_app", "app_enabled_state"):
            monkeypatch.setattr(routes_mod, attr, _spy(attr))
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            assert (await client.post(f"/api/apps/{APP}/enable")).status == 200
            enable_reads, reads[:] = list(reads), []
            # No openCommand: the route answers 400 after its record read.
            assert (await client.post(f"/api/apps/{APP}/open")).status == 400
            open_reads, reads[:] = list(reads), []
            assert (await client.post(f"/api/apps/{APP}/disable")).status == 200
            disable_reads = list(reads)
        assert enable_reads == [("get_app", False), ("app_enabled_state", False)]
        assert open_reads == [("get_app", False)]
        assert disable_reads == [("get_app", False), ("app_enabled_state", False)]
