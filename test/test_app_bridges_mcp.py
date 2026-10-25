"""App bridge tests: app MCP server registration and the lifecycle of its mcp.json entries.

Namespacing properties, boot and health reconcile, scrub and reregister, the legacy-file
lock and the health-lock serialization.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from conftest import host_abs
from kiro_crew.apps.bridges import _deregister_mcp_servers, _register_mcp_servers, register_app
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------


class TestMCPRegistration:
    def test_register_mcp_servers(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Backend live → HTTP url server registers (the dead-port skip only fires when
        # no backend is up; see test_http_mcp_server_skipped_when_backend_not_yet_up).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9000)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "my-mcp": {"url": "http://localhost:9000/mcp"},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_mcp_servers("test-app", manifest)
        assert registered == ["test-app:my-mcp"]

        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "test-app:my-mcp" in data["mcpServers"]

    def test_stdio_declared_env_path_is_expanded(self, tmp_path, app_env, monkeypatch):
        """This file is consumed by kiro-cli (per-key env), so an app manifest
        naming a PATH fragment must be emitted complete. See env.emit_env."""
        import os

        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Absolute for THIS host (see conftest.host_abs): the manifest's entries go
        # through the ``os.path.isabs`` filter in env._spec_path_entries, and from
        # Python 3.13 a bare ``/opt/shims`` is not absolute under ntpath (no
        # drive), so the POSIX literal was silently dropped on Windows.
        shims = host_abs("opt", "shims")
        usr_bin = host_abs("usr", "bin")
        monkeypatch.setenv("PATH", usr_bin)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "my-mcp": {
                    "command": "/opt/bin/tool",
                    "env": {"PATH": shims, "TOKEN": "t"},
                },
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_mcp_servers("test-app", manifest)
        assert registered == ["test-app:my-mcp"]

        written = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]["test-app:my-mcp"][
            "env"
        ]
        entries = written["PATH"].split(os.pathsep)
        assert entries[0] == shims, "manifest-authored entries stay first"
        assert usr_bin in entries, "inherited PATH must survive the override"
        assert written["TOKEN"] == "t"

    def test_http_mcp_url_port_rewritten_to_live_backend_port(self, tmp_path, app_env, monkeypatch):
        # An app with backend.port:"auto" gets a free port at spawn time (9100, else
        # 9101, …). The manifest's mcpServers url carries an illustrative fixed port.
        # Registration MUST rewrite it to the live allocated port, else agents call the
        # wrong port and every app tool call silently fails.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Pretend the backend actually came up on 9101 (not the manifest's 9100).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9101)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "my-mcp": {"url": "http://localhost:9100/mcp"},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_mcp_servers("test-app", manifest)
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        # Port rewritten 9100 -> 9101; scheme/host/path preserved.
        assert data["mcpServers"]["test-app:my-mcp"]["url"] == "http://localhost:9101/mcp"

    def test_http_mcp_server_skipped_when_backend_not_yet_up(self, tmp_path, app_env, monkeypatch):
        # REGRESSION (revert): a GATEWAY-MANAGED backend's HTTP server
        # (backend.entryPoint set) with the backend not running (port unknown) must NOT be
        # registered at all — registering
        # the manifest's illustrative dead port (:9100) into global ~/.kiro/settings/mcp.json
        # makes kiro-cli try to connect on EVERY session → "backend hiccup" → 3 retries →
        # hard error, breaking all requests. The enable/boot flow re-registers with the
        # live port once the backend is up.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)

        src = _make_app_source(
            tmp_path,
            backend={"entryPoint": "backend/app.py"},
            mcpServers={
                "my-mcp": {"url": "http://localhost:9100/mcp"},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_mcp_servers("test-app", manifest)
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        # No dead-port entry written — nothing for kiro to fail to connect to.
        assert "test-app:my-mcp" not in data.get("mcpServers", {})

    def test_http_mcp_dead_entry_scrubbed_on_reregister_without_backend(
        self, tmp_path, app_env, monkeypatch
    ):
        # A GATEWAY-MANAGED backend's stale dead-port entry from a prior (now-down)
        # registration must be SCRUBBED
        # when we re-register and the backend still isn't up — so it can't keep poisoning
        # every kiro session across reboots/disable.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)

        src = _make_app_source(
            tmp_path,
            backend={"entryPoint": "backend/app.py"},
            mcpServers={
                "my-mcp": {"url": "http://localhost:9100/mcp"},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        # Backend up → entry written with live port.
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9101)
        _register_mcp_servers("test-app", manifest)
        assert "test-app:my-mcp" in json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        # Backend now DOWN → a re-register must remove the now-dead entry.
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)
        _register_mcp_servers("test-app", manifest)
        assert "test-app:my-mcp" not in json.loads(mcp_path.read_text(encoding="utf-8")).get(
            "mcpServers", {}
        )

    def test_stdio_mcp_server_always_registered_no_backend(self, tmp_path, app_env, monkeypatch):
        # A command/stdio MCP server (no url) has no port to be dead — it must always be
        # registered regardless of backend liveness (only HTTP url servers are gated).
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "my-stdio": {"command": "my-server", "args": ["--stdio"]},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_mcp_servers("test-app", manifest)
        assert registered == ["test-app:my-stdio"]
        assert "test-app:my-stdio" in json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]

    def test_self_managed_http_url_registered_without_backend(self, tmp_path, app_env, monkeypatch):
        # A SELF-MANAGED app (no backend.entryPoint) runs no gateway-launched backend, so
        # its mcpServers url is an AUTHORITATIVE fixed endpoint — not an illustrative port
        # awaiting a live allocation. It never gets a live registration, so the registrar
        # must PRESERVE it (not scrub it as a dead default port). This mirrors
        # _collect_app_mcp_servers (test_app_mcp_scoping::test_self_managed_http_url_is_preserved),
        # the other writer of this config: the two must agree or one scrubs what the other
        # writes straight back.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # No backend the gateway launches → no live port, ever.
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "companion": {"url": "http://127.0.0.1:7778/mcp"},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        assert manifest.backend.entryPoint == ""  # self-managed
        registered = _register_mcp_servers("test-app", manifest)
        assert registered == ["test-app:companion"]
        written = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        # The authoritative manifest url is preserved verbatim (no live port to rewrite to).
        assert written["test-app:companion"]["url"] == "http://127.0.0.1:7778/mcp"

    def test_backend_app_portless_url_still_scrubbed(self, tmp_path, app_env, monkeypatch):
        # The complement of the self-managed case: an app that DOES declare a backend
        # (backend.entryPoint set) still has its portless url scrubbed when no live port
        # is known — the scrub is now conditional on backend.entryPoint, not removed.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)

        src = _make_app_source(
            tmp_path,
            backend={"entryPoint": "backend/app.py"},
            mcpServers={
                "my-mcp": {"url": "http://localhost:9100/mcp"},
            },
        )
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        assert manifest.backend.entryPoint == "backend/app.py"
        registered = _register_mcp_servers("test-app", manifest)
        assert registered == []
        assert "test-app:my-mcp" not in json.loads(mcp_path.read_text(encoding="utf-8")).get(
            "mcpServers", {}
        )

    def test_reregister_app_mcp_servers_overwrites_with_live_port(
        self, tmp_path, app_env, monkeypatch
    ):
        # reregister_app_mcp_servers (called after the backend starts) overwrites the
        # earlier manifest-default entry with the live-port url.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod
        from kiro_crew.apps.bridges import reregister_app_mcp_servers

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)

        src = _make_app_source(
            tmp_path,
            backend={"entryPoint": "backend/app.py"},
            mcpServers={
                "my-mcp": {"url": "http://localhost:9100/mcp"},
            },
        )
        install_app(src)
        # First registration BEFORE the backend is up: HTTP server is skipped (no dead
        # entry written — the fail-safe that keeps kiro from dialing a dead port).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_mcp_servers("test-app", manifest)
        assert "test-app:my-mcp" not in json.loads(mcp_path.read_text(encoding="utf-8")).get(
            "mcpServers", {}
        )
        # Backend now up on 9101 → re-register writes it with the live port.
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9101)
        reregister_app_mcp_servers("test-app")
        assert (
            json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]["test-app:my-mcp"]["url"]
            == "http://localhost:9101/mcp"
        )

    def test_explicit_live_port_rewrites_even_when_backend_unhealthy(
        self, tmp_path, app_env, monkeypatch
    ):
        # The boot/enable path passes the just-allocated port explicitly because the
        # backend isn't marked *healthy* yet (get_app_backend_port would return None at
        # that instant). An explicit live_port must still rewrite the url — this is the
        # exact bug that left the registered url at :9100 while the backend was on :9101.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod
        from kiro_crew.apps.bridges import reregister_app_mcp_servers

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Health-gated lookup returns None (backend up but not yet confirmed healthy).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)

        src = _make_app_source(
            tmp_path, mcpServers={"my-mcp": {"url": "http://localhost:9100/mcp"}}
        )
        install_app(src)
        # Explicit live_port=9101 (from the spawn result) must win over the None lookup.
        reregister_app_mcp_servers("test-app", live_port=9101)
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert data["mcpServers"]["test-app:my-mcp"]["url"] == "http://localhost:9101/mcp"

    def test_deregister_mcp_servers(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)

        # Pre-populate with entries from two apps
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "app-a:srv1": {"url": "http://localhost:1"},
                        "app-a:srv2": {"url": "http://localhost:2"},
                        "app-b:srv1": {"url": "http://localhost:3"},
                    }
                }
            )
        )

        removed = _deregister_mcp_servers("app-a")
        assert removed == 2

        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "app-a:srv1" not in data["mcpServers"]
        assert "app-a:srv2" not in data["mcpServers"]
        assert "app-b:srv1" in data["mcpServers"]

    def test_deregister_does_not_run_legacy_scrub_on_loop(self, tmp_path, app_env, monkeypatch):
        # The legacy shared-file scrub takes a cross-process flock contended by
        # external processes (Kiro IDE, other agents). deregister_app runs
        # synchronously on the gateway event loop, so the scrub must NOT run
        # here or a held lock would stall all chat/heartbeat. Boot reconcile
        # performs it off-loop instead.
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(json.dumps({"mcpServers": {"app-a:srv1": {"url": "http://x"}}}))

        called: list[str] = []
        monkeypatch.setattr(bmod, "_scrub_legacy_shared_mcp", lambda name: called.append(name) or 0)
        _deregister_mcp_servers("app-a")
        assert called == [], "deregister must not run the blocking legacy scrub on the event loop"

    def test_deregister_no_servers(self, tmp_path, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        assert _deregister_mcp_servers("nonexistent") == 0

    def test_register_no_mcp_servers(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)

        manifest = AppManifest(name="test", mcpServers={})
        assert _register_mcp_servers("test", manifest) == []

    def test_register_app_includes_mcp(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Backend live so the HTTP url server is registered (not dead-port-skipped).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 8080)

        src = _make_app_source(
            tmp_path,
            mcpServers={
                "backend": {"url": "http://localhost:8080/mcp"},
            },
        )
        install_app(src)
        result = register_app("test-app")
        assert len(result.mcp_servers) == 1
        assert "test-app:backend" in result.mcp_servers


# ---------------------------------------------------------------------------
# MCP property tests
# ---------------------------------------------------------------------------


_app_name_st = st.from_regex(r"[a-z][a-z0-9\-]{0,15}", fullmatch=True)


_server_name_st = st.from_regex(r"[a-z][a-z0-9\-]{0,15}", fullmatch=True)


class TestMCPProperties:
    # Feature: app-classification-redesign, Property 10: MCP server registration is namespaced per app
    @given(
        app_name=_app_name_st,
        servers=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:9000")}),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_register_namespace(self, app_name, servers, tmp_path, monkeypatch):
        """**Validates: Requirements 8.1, 8.2**"""
        import uuid

        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / f"mcp-{uuid.uuid4().hex[:8]}.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Backend live → HTTP url servers register (dead-port skip only with no backend).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9000)

        manifest = AppManifest(name=app_name, mcpServers=servers)
        registered = _register_mcp_servers(app_name, manifest)

        for server_name in servers:
            expected = f"{app_name}:{server_name}"
            assert expected in registered

        data = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.is_file() else {}
        for name in registered:
            assert name in data.get("mcpServers", {})

    # Feature: app-classification-redesign, Property 11: MCP server deregistration is isolated to one app
    @given(
        app_a=_app_name_st,
        app_b=_app_name_st.filter(lambda s: len(s) > 1),
        servers_a=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:1")}),
            min_size=1,
            max_size=3,
        ),
        servers_b=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:2")}),
            min_size=1,
            max_size=3,
        ),
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_deregister_isolation(self, app_a, app_b, servers_a, servers_b, tmp_path, monkeypatch):
        """**Validates: Requirements 8.3**"""
        assume(app_a != app_b)
        import uuid

        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / f"mcp-iso-{uuid.uuid4().hex[:8]}.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Backend live → HTTP url servers register (dead-port skip only with no backend).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: 9000)

        _register_mcp_servers(app_a, AppManifest(name=app_a, mcpServers=servers_a))
        _register_mcp_servers(app_b, AppManifest(name=app_b, mcpServers=servers_b))

        _deregister_mcp_servers(app_a)

        data = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.is_file() else {}
        remaining = data.get("mcpServers", {})

        for name in servers_a:
            assert f"{app_a}:{name}" not in remaining
        for name in servers_b:
            assert f"{app_b}:{name}" in remaining


class TestBootReconcile:
    """Boot-time scrub of stale MCP entries for disabled apps."""

    def test_boot_scrubs_stale_mcp_entry_for_disabled_app(self, tmp_path, monkeypatch):
        # A disabled app that left a (now-dead-port) MCP entry in global mcp.json must
        # have it scrubbed at gateway boot — else kiro-cli dials the dead port on every
        # session. start_enabled_app_backends() reconciles disabled apps before starting
        # any backend.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Seed a stale entry as if a prior enable had registered it.
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ai-app:backend": {"url": "http://localhost:9100/mcp"},
                        "other:keep": {"command": "x"},
                    }
                }
            )
            + "\n"
        )

        # One installed-but-DISABLED app that declares an MCP server. list_apps is imported
        # inside start_enabled_app_backends from the manager module, so patch it there.
        monkeypatch.setattr(
            backend_mod,
            "list_apps",
            lambda: [
                {
                    "name": "ai-app",
                    "enabled": False,
                    "manifest": {
                        "mcpServers": {"backend": {"url": "http://localhost:9100/mcp"}},
                        "backend": {"entryPoint": "x"},
                    },
                },
            ],
        )
        # No backend should be started for a disabled app.
        monkeypatch.setattr(
            backend_mod,
            "start_app_backend",
            lambda *_a, **_k: pytest.fail("must not start disabled app"),
        )

        backend_mod.start_enabled_app_backends()

        remaining = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        assert "ai-app:backend" not in remaining  # stale dead entry scrubbed
        assert "other:keep" in remaining  # unrelated entry untouched

    def test_enabled_app_never_healthy_mcp_entry_scrubbed(self, tmp_path, app_env, monkeypatch):
        # Review scenario: an ENABLED port:"auto" app registered with an optimistic
        # pre-health port whose backend never passes /health must NOT leave a dead HTTP MCP
        # url behind — that's the exact shape that broke every kiro-cli session. The
        # health-gated path calls _gate_mcp_registration(healthy=False) on health failure,
        # which scrubs the entry. (Closes the disabled-only asymmetry the reviewer flagged.)
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Seed an optimistic entry as if the pre-health register had written it.
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "test-app:backend": {"url": "http://localhost:9100/mcp"},
                        "other:keep": {"command": "x"},
                    }
                }
            )
            + "\n"
        )

        backend_mod._gate_mcp_registration("test-app", 9100, healthy=False)

        remaining = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        assert "test-app:backend" not in remaining  # dead enabled-app entry scrubbed
        assert "other:keep" in remaining  # unrelated entry untouched

    def test_enabled_app_healthy_registers_with_live_port(self, tmp_path, app_env, monkeypatch):
        # The complement: once /health passes, _gate_mcp_registration(healthy=True) writes the
        # HTTP MCP url with the confirmed live port (rewriting the manifest's illustrative one).
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Health-gated lookup returns None (port resolved from the explicit live_port instead).
        monkeypatch.setattr(backend_mod, "get_app_backend_port", lambda _n: None)
        src = _make_app_source(
            tmp_path, mcpServers={"my-mcp": {"url": "http://localhost:9100/mcp"}}
        )
        install_app(src)

        backend_mod._gate_mcp_registration("test-app", 9101, healthy=True)

        servers = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        assert "test-app:my-mcp" in servers
        assert servers["test-app:my-mcp"]["url"] == "http://localhost:9101/mcp"  # live port

    def test_boot_does_not_register_enabled_app_before_health(self, tmp_path, monkeypatch):
        # Review scenario: the boot loop must NOT register MCP servers for a freshly
        # spawned (healthy=False) enabled app — registration is deferred to the health-check
        # loop. Registering here is what could leave a dead url for a never-healthy app.
        import kiro_crew.apps.backend as backend_mod
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        mcp_path.write_text(json.dumps({"mcpServers": {}}) + "\n")

        monkeypatch.setattr(
            backend_mod,
            "list_apps",
            lambda: [
                {
                    "name": "ai-app",
                    "enabled": True,
                    "manifest": {
                        "mcpServers": {"backend": {"url": "http://localhost:9100/mcp"}},
                        "backend": {"entryPoint": "x"},
                    },
                },
            ],
        )
        # Spawn returns a not-yet-healthy process (the real pre-health state).
        fake_ap = SimpleNamespace(port=9101, healthy=False)
        monkeypatch.setattr(backend_mod, "start_app_backend", lambda *_a, **_k: fake_ap)
        # If the boot loop tries to register before health, fail loudly.
        monkeypatch.setattr(
            backend_mod,
            "_gate_mcp_registration",
            lambda *_a, **_k: pytest.fail("must not register before health"),
        )

        backend_mod.start_enabled_app_backends()

        # Nothing registered synchronously; the health loop owns it.
        assert json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"] == {}


class TestRebuildPreservesTheLiveMcpSpec:
    """A rebuild must keep the health-registered live-port spec, not re-copy the
    manifest's illustrative port.

    An auto-port app's manifest carries a fixed illustrative port; the reachable
    one is written to the app mcp.json only after the backend starts. Reading the
    manifest on every rebuild would stamp the dead port back over the live one,
    and kiro-cli dials every configured server — so the app's tools would break
    until the next reregister.
    """

    def test_live_registered_spec_wins_over_the_manifest(self, monkeypatch):
        from kiro_crew import agent

        class _M:
            mcpServers = {"srv": {"url": "http://127.0.0.1:9100/mcp"}}  # illustrative

        monkeypatch.setattr(agent, "_ceiling_filtered_spec", lambda ref, spec, *, audit=True: spec)
        import kiro_crew.apps.bridges as bridges
        import kiro_crew.apps.manager as manager

        monkeypatch.setattr(manager, "list_apps", lambda: [{"name": "someapp"}])
        monkeypatch.setattr(manager, "is_app_enabled", lambda n: True)
        monkeypatch.setattr(manager, "get_app_manifest", lambda n: _M())
        monkeypatch.setattr(
            bridges,
            "registered_app_mcp_servers",
            lambda: {"someapp:srv": {"url": "http://127.0.0.1:54321/mcp"}},  # live
        )
        out = agent._collect_app_mcp_servers()
        assert out["someapp:srv"]["url"] == "http://127.0.0.1:54321/mcp"

    def test_http_server_with_no_live_entry_is_skipped(self, monkeypatch):
        from kiro_crew import agent

        class _M:
            mcpServers = {"srv": {"url": "http://127.0.0.1:9100/mcp"}}
            backend = SimpleNamespace(entryPoint="backend.py")

        monkeypatch.setattr(agent, "_ceiling_filtered_spec", lambda ref, spec, *, audit=True: spec)
        import kiro_crew.apps.bridges as bridges
        import kiro_crew.apps.manager as manager

        monkeypatch.setattr(manager, "list_apps", lambda: [{"name": "someapp"}])
        monkeypatch.setattr(manager, "is_app_enabled", lambda n: True)
        monkeypatch.setattr(manager, "get_app_manifest", lambda n: _M())
        monkeypatch.setattr(bridges, "registered_app_mcp_servers", lambda: {})
        out = agent._collect_app_mcp_servers()
        assert "someapp:srv" not in out  # dead-port URL never written

    def test_stdio_server_falls_back_to_the_manifest(self, monkeypatch):
        from kiro_crew import agent

        class _M:
            mcpServers = {"srv": {"command": "run", "args": ["x"]}}

        monkeypatch.setattr(agent, "_ceiling_filtered_spec", lambda ref, spec, *, audit=True: spec)
        import kiro_crew.apps.bridges as bridges
        import kiro_crew.apps.manager as manager

        monkeypatch.setattr(manager, "list_apps", lambda: [{"name": "someapp"}])
        monkeypatch.setattr(manager, "is_app_enabled", lambda n: True)
        monkeypatch.setattr(manager, "get_app_manifest", lambda n: _M())
        monkeypatch.setattr(bridges, "registered_app_mcp_servers", lambda: {})
        out = agent._collect_app_mcp_servers()
        assert out["someapp:srv"]["command"] == "run"  # no port to resolve


class TestLegacyScrubIsLocked:
    """The pre-fix shared ~/.kiro/settings/mcp.json can be written by other
    processes (Kiro IDE, another agent). Scrubbing an app's entries there must
    hold that file's own lock across read+remove+write, or a concurrent writer's
    new server is lost to a stale read-modify-write.
    """

    def test_scrub_holds_the_legacy_file_lock(self) -> None:
        import inspect

        from kiro_crew.apps import bridges as bridges_mod

        src = inspect.getsource(bridges_mod._scrub_legacy_shared_mcp)
        assert "with _mcp_lock(target=_LEGACY_SHARED_MCP_PATH):" in src

    def test_scrub_removes_only_the_apps_entries(self, tmp_path, monkeypatch) -> None:
        import json

        from kiro_crew.apps import bridges as bridges_mod

        legacy = tmp_path / "mcp.json"
        legacy.write_text(
            json.dumps(
                {"mcpServers": {"myapp:srv": {"command": "x"}, "other:srv": {"command": "y"}}}
            )
        )
        monkeypatch.setattr(bridges_mod, "_LEGACY_SHARED_MCP_PATH", legacy)
        removed = bridges_mod._scrub_legacy_shared_mcp("myapp")
        assert removed == 1
        data = json.loads(legacy.read_text())
        assert "myapp:srv" not in data["mcpServers"]
        assert "other:srv" in data["mcpServers"], "another app's entry must survive"


class TestReregisterRefreshesAgents:
    """After an auto-port backend becomes healthy, reregister writes the live
    server to the global map — but the app's AGENTS copy that spec into their own
    config, so they must be refreshed too or the app agent can't reach its tools.
    """

    def test_reregister_refreshes_agents_after_registering(self, monkeypatch) -> None:
        from kiro_crew.apps import bridges as bridges_mod

        calls: list[str] = []
        monkeypatch.setattr(bridges_mod, "_registration_source", lambda n: (object(), "/app/root"))
        monkeypatch.setattr(bridges_mod, "_registration_denied", lambda *a, **k: "")
        # manifest with mcpServers truthy
        monkeypatch.setattr(
            bridges_mod, "_register_mcp_servers", lambda n, m, live_port=None: ["srv"]
        )
        monkeypatch.setattr(
            bridges_mod,
            "_register_agents",
            lambda n, m, r, io_failures=None: calls.append(f"agents:{n}"),
        )
        # give _registration_source a manifest with mcpServers

        class _M:
            mcpServers = {"srv": {"command": "x"}}

        monkeypatch.setattr(bridges_mod, "_registration_source", lambda n: (_M(), "/app/root"))
        out = bridges_mod.reregister_app_mcp_servers("myapp", live_port=54321)
        assert out == ["srv"]
        assert calls == ["agents:myapp"], "agents must be refreshed after live registration"


class TestMcpEnableHandlersOffloadTheSync:
    """`_sync_mcp_to_agent*` now walks the profile directory (profile-aware
    may_skip_gate_now), so the async MCP handlers must offload it or the gateway
    loop freezes on slow storage."""

    def test_handlers_offload_sync_to_agent(self) -> None:
        import importlib
        import re

        from kiro_crew.dashboard.handlers import mcp

        # The handler facade and its owners, read as one text.
        src = "\n".join(
            Path(importlib.import_module(m).__file__).read_text(encoding="utf-8")
            for m in (mcp.__name__, *mcp._OWNERS)
        )
        # No bare synchronous call to a PUBLIC sync-to-agent function inside an
        # async handler: every such invocation is wrapped in asyncio.to_thread.
        # The `_unlocked` variants are the sync locking-wrapper's own delegation
        # (it holds _mcp_lock then calls the body), which is intentional and not a
        # handler call — exclude them.
        bare = [
            m
            for m in re.findall(r"^\s+_sync_mcp_to_agent\w*\(", src, re.M)
            if "_unlocked(" not in m
        ]
        assert bare == [], f"un-offloaded sync-to-agent call(s): {bare}"
        assert "asyncio.to_thread(_sync_mcp_to_agent" in src


class TestMalformedConfigIsNotClobbered:
    """A read-modify-write of an EXISTING-but-unreadable kirocrew.json must
    ABORT, not treat it as empty and overwrite it — that would drop the agent's
    whole configuration."""

    def test_strict_read_raises_on_malformed_existing(self, tmp_path, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "kirocrew.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        # Missing file -> empty map, both modes.
        assert bmod._read_mcp_json_unlocked() == {}
        assert bmod._read_mcp_json_unlocked(strict=True) == {}
        # Present but malformed: lenient degrades to {}, strict PROPAGATES.
        mcp_path.write_text("{ not valid json", encoding="utf-8")
        assert bmod._read_mcp_json_unlocked() == {}
        with pytest.raises((json.JSONDecodeError, OSError)):
            bmod._read_mcp_json_unlocked(strict=True)

    def test_register_does_not_overwrite_a_malformed_config(self, tmp_path, app_env, monkeypatch):
        import kiro_crew.apps.bridges as bmod

        mcp_path = tmp_path / "kirocrew.json"
        monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
        original = "{ this is not json and must survive"
        mcp_path.write_text(original, encoding="utf-8")

        manifest = AppManifest(name="app-a", mcpServers={"srv": {"command": "x"}})
        # register_app wraps _register_mcp_servers in try/except, so the strict
        # read's raise aborts the write; called directly it propagates.
        with pytest.raises((json.JSONDecodeError, OSError)):
            _register_mcp_servers("app-a", manifest)

        # The malformed-but-present config is UNTOUCHED (not clobbered with {}).
        assert mcp_path.read_text(encoding="utf-8") == original


class TestDemotionKeepsBackendIndependentServers:
    """A dead HTTP backend must not take an app's stdio tools with it.

    stdio/command servers are launched by kiro-cli itself and have no port to be dead.
    Blanket-deregistering every `<app>:` entry on demotion removed working tools for a
    reason that had nothing to do with them.
    """

    def test_the_unhealthy_reconcile_scrubs_http_and_keeps_stdio(self, monkeypatch, tmp_path):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        calls: dict[str, object] = {}

        # Fabricated app: not in installed.json, so the demotion path's enablement gate
        # would otherwise divert into the disabled-app cleanup. The gate is pinned by
        # TestDemotionRefreshIsGatedOnEnablement.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        def _scrub(app_name, unreconciled=None):
            calls["app"] = app_name
            return ["app:stdio-tool"]  # the stdio entry survives

        monkeypatch.setattr(brmod, "scrub_backend_mcp_url", _scrub)
        monkeypatch.setattr(brmod, "refresh_app_agents", lambda name, io_failures=None: [])

        def _blanket(name):
            raise AssertionError("must not blanket-deregister on a health demotion")

        monkeypatch.setattr(brmod, "_deregister_mcp_servers", _blanket)

        assert bmod._gate_mcp_registration("app", 9280, healthy=False) is True
        assert calls == {"app": "app"}

    def test_the_registration_path_pops_a_stale_http_entry(self):
        # Pins the property the fix leans on, in the code that owns it: for a
        # gateway-managed backend with no live port, an HTTP server is removed rather than
        # merely left unwritten — otherwise the dead url would survive the demotion. The
        # scrub is conditional on backend.entryPoint (a self-managed app's url is kept),
        # mirroring _collect_app_mcp_servers.
        import inspect

        import kiro_crew.apps.bridges as brmod

        src = inspect.getsource(brmod._register_mcp_servers)
        assert "servers.pop(namespaced, None)" in src
        assert "if is_http and not resolved_port and manifest.backend.entryPoint:" in src


class TestScrubFallsBackWhenTheManifestCannotSay:
    """No manifest → remove everything, rather than keep a dead url out of ignorance."""

    def test_an_unresolvable_manifest_scrubs_every_entry(self, monkeypatch):
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(brmod, "_registration_source", lambda n: (None, brmod.Path(".")))
        removed: list[str] = []
        monkeypatch.setattr(
            brmod,
            "_deregister_mcp_servers",
            lambda n: (removed.append(n), 2)[1],
        )
        monkeypatch.setattr(
            brmod,
            "reregister_app_mcp_servers",
            lambda n, live_port=None: pytest.fail("cannot register without a manifest"),
        )

        assert brmod.scrub_backend_mcp_url("gone") == []
        assert removed == ["gone"]

    def test_a_manifest_declaring_no_servers_also_scrubs_everything(self, monkeypatch):
        # A stale `<app>:` entry with nothing declaring it is exactly the shape that
        # should not survive; there is no independent server to protect.
        import kiro_crew.apps.bridges as brmod

        empty = SimpleNamespace(mcpServers={})
        monkeypatch.setattr(brmod, "_registration_source", lambda n: (empty, brmod.Path(".")))
        removed: list[str] = []
        monkeypatch.setattr(
            brmod,
            "_deregister_mcp_servers",
            lambda n: (removed.append(n), 1)[1],
        )

        assert brmod.scrub_backend_mcp_url("bare") == []
        assert removed == ["bare"]


class TestLifecycleWritersShareTheHealthSerialization:
    """Both families of writer hold one lock.

    An app's mcp.json entries and its materialized agents are written by the lifecycle
    paths here AND by the backend's health watch. Unserialized, the two can interleave
    their decisions — each doing a correct read-modify-write, with the STALE one landing
    last. Closing that per call site is what this review kept re-finding; the writers now
    share the guard instead.
    """

    def _lock_held(self):
        # RLock has no public `locked()`; a non-blocking acquire from ANOTHER thread is
        # the honest probe — it fails only while someone actually holds it.
        import threading

        import kiro_crew.apps.backend as bmod

        result: list[bool] = []

        def _probe():
            got = bmod._health_reconcile_lock.acquire(blocking=False)
            result.append(not got)
            if got:
                bmod._health_reconcile_lock.release()

        t = threading.Thread(target=_probe)
        t.start()
        t.join()
        return result[0]

    def test_mcp_registration_runs_under_the_guard(self, monkeypatch):
        import kiro_crew.apps.bridges as brmod

        held: list[bool] = []
        monkeypatch.setattr(
            brmod,
            "_read_mcp_json_unlocked",
            lambda strict=False: (held.append(self._lock_held()), {"mcpServers": {}})[1],
        )
        monkeypatch.setattr(brmod, "_write_mcp_json_unlocked", lambda data: None)

        # A non-empty manifest: the function returns before the lock when there is
        # nothing to register, so an empty one would pass this test vacuously.
        monkeypatch.setattr(brmod, "strip_ungoverned_auto_approve", lambda m, **kw: m)
        brmod._register_mcp_servers(
            "app",
            SimpleNamespace(
                mcpServers={"srv": {"command": "x"}},
                backend=SimpleNamespace(entryPoint="server.py"),
            ),
        )
        assert held == [True]

    def test_mcp_deregistration_runs_under_the_guard(self, monkeypatch):
        import kiro_crew.apps.bridges as brmod

        held: list[bool] = []
        monkeypatch.setattr(
            brmod,
            "_read_mcp_json_unlocked",
            lambda strict=False: (held.append(self._lock_held()), {"mcpServers": {}})[1],
        )
        monkeypatch.setattr(brmod, "_write_mcp_json_unlocked", lambda data: None)

        brmod._deregister_mcp_servers("app")
        assert held == [True]

    def test_agent_materialization_runs_under_the_guard(self, monkeypatch, tmp_path):
        # The READ is inside too, not just the write: an agent copies the ambient spec,
        # so a read before a scrub and a write after it is the interleave that matters.
        import kiro_crew.apps.bridges as brmod

        held: list[bool] = []
        monkeypatch.setattr(brmod, "_kiro_agents_dir", lambda: tmp_path)
        monkeypatch.setattr(
            brmod,
            "_agent_mcp_policy",
            lambda name: (held.append(self._lock_held()), {})[1],
        )

        brmod._register_agents("app", SimpleNamespace(agents=[]), tmp_path)
        assert held == [True]

    def test_the_health_path_can_re_enter_it(self):
        # The health transition holds the lock and then calls straight into these
        # writers. A non-re-entrant lock would deadlock the watch thread here, which is
        # why it is an RLock — pinned so nobody "simplifies" it back to Lock.
        import kiro_crew.apps.backend as bmod

        with bmod._health_reconcile_lock:
            assert bmod._health_reconcile_lock.acquire(blocking=False)
            bmod._health_reconcile_lock.release()


class TestScrubNeverDeletesMaterializedAgents:
    """The scrub fallback must not delete agent files.

    Deleting them is unrecoverable — it takes the user-owned fields
    `_preserve_user_agent_edits` carries across every refresh — while what it would
    prevent, an agent naming a removed server, costs failed tool calls until the next
    refresh rewrites it. An unreadable manifest is also frequently TRANSIENT, so
    destroying data over it trades a temporary fault for a permanent one. Only
    `deregister_app` owns deleting these files.
    """

    def test_an_unreadable_manifest_keeps_the_agents(self, monkeypatch):
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(brmod, "_registration_source", lambda n: (None, brmod.Path(".")))
        monkeypatch.setattr(brmod, "_deregister_mcp_servers", lambda n: 1)
        monkeypatch.setattr(
            brmod,
            "_deregister_agents",
            lambda n: pytest.fail("user-edited agent configs must survive a scrub"),
        )

        assert brmod.scrub_backend_mcp_url("unreadable") == []

    def test_a_manifest_declaring_no_servers_keeps_them_too(self, monkeypatch):
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod,
            "_registration_source",
            lambda n: (SimpleNamespace(mcpServers={}), brmod.Path(".")),
        )
        monkeypatch.setattr(brmod, "_deregister_mcp_servers", lambda n: 1)
        monkeypatch.setattr(
            brmod,
            "_deregister_agents",
            lambda n: pytest.fail("nothing declared means nothing stale to remove"),
        )

        assert brmod.scrub_backend_mcp_url("bare") == []

    def test_the_mcp_entry_is_still_scrubbed(self, monkeypatch):
        # Keeping the agents must not turn into keeping the dead url as well.
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(brmod, "_registration_source", lambda n: (None, brmod.Path(".")))
        removed: list[str] = []
        monkeypatch.setattr(brmod, "_deregister_mcp_servers", lambda n: (removed.append(n), 1)[1])
        monkeypatch.setattr(brmod, "_deregister_agents", lambda n: 0)

        brmod.scrub_backend_mcp_url("gone")
        assert removed == ["gone"]


class TestUnreadableManifestIsNotASilentRegistration:
    """An unreadable manifest registered nothing, so it must not report success.

    `reregister_app_mcp_servers` returns an empty list for several reasons. One of them —
    the manifest could not be read — means nothing was written, and recording that as a
    completed registration leaves a healthy backend with no MCP entry and nothing to
    retry it.
    """

    def test_an_unreadable_manifest_is_collected(self, monkeypatch):
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(brmod, "_registration_source", lambda n: (None, brmod.Path(".")))
        monkeypatch.setattr(brmod, "_registration_denied", lambda name, action, app_root: None)

        collected: list[str] = []
        assert brmod.reregister_app_mcp_servers("app", io_failures=collected) == []
        assert collected == ["app: manifest unreadable"]

    def test_a_manifest_declaring_no_servers_is_not_collected(self, monkeypatch):
        # Readable and simply empty: nothing to register, and retrying never changes it.
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod,
            "_registration_source",
            lambda n: (SimpleNamespace(mcpServers={}, agents=[]), brmod.Path(".")),
        )
        monkeypatch.setattr(brmod, "_registration_denied", lambda name, action, app_root: None)

        collected: list[str] = []
        assert brmod.reregister_app_mcp_servers("app", io_failures=collected) == []
        assert collected == []


class TestScrubDoesNotRematerializeAgents:
    """The scrub must not write agents.

    `reregister_app_mcp_servers` calls `_register_agents` internally, so routing the
    scrub through it re-materialized this app's agent configs BEFORE the caller's
    enablement check ran — making a disabled app's agents dispatchable in the gap. The
    scrub needs only the mcp.json half; the agent refresh belongs to the caller, which
    gates it.
    """

    def test_the_scrub_touches_mcp_only(self, monkeypatch):
        import kiro_crew.apps.bridges as brmod

        manifest = SimpleNamespace(mcpServers={"srv": {"command": "x"}}, agents=["a.json"])
        monkeypatch.setattr(brmod, "_registration_source", lambda n: (manifest, brmod.Path(".")))
        monkeypatch.setattr(brmod, "_registration_denied", lambda name, action, app_root: None)
        monkeypatch.setattr(
            brmod,
            "_register_mcp_servers",
            lambda name, m, live_port=None: ["app:srv"],
        )
        monkeypatch.setattr(
            brmod,
            "_register_agents",
            lambda *a, **k: pytest.fail("the scrub must not re-materialize agents"),
        )

        assert brmod.scrub_backend_mcp_url("app") == ["app:srv"]

    def test_a_denied_app_still_gets_a_full_removal(self, monkeypatch):
        # The admission gate `reregister_app_mcp_servers` applied has to survive the
        # switch: nothing of a denied app stays reachable, not even its stdio servers.
        import kiro_crew.apps.bridges as brmod

        manifest = SimpleNamespace(mcpServers={"srv": {"command": "x"}}, agents=[])
        monkeypatch.setattr(brmod, "_registration_source", lambda n: (manifest, brmod.Path(".")))
        monkeypatch.setattr(brmod, "_registration_denied", lambda name, action, app_root: "revoked")
        removed: list[str] = []
        monkeypatch.setattr(brmod, "_deregister_mcp_servers", lambda n: (removed.append(n), 1)[1])
        monkeypatch.setattr(
            brmod,
            "_register_mcp_servers",
            lambda *a, **k: pytest.fail("a denied app must not keep any server"),
        )

        assert brmod.scrub_backend_mcp_url("app") == []
        assert removed == ["app"]
