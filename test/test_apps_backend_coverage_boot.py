"""Unit coverage for :mod:`kiro_crew.apps.backend`: boot, and the remaining defensive branches.

``start_enabled_app_backends``' boot-time MCP and executable-resource reconcile, its
spawn gating and fixed-port preclaim, and the defensive branches that keep a start, a
stop, a boot or the stale-pidfile reap going when the audit sink or a signal fails, an
entry cannot be sniffed, npm is missing or a pidfile entry is malformed.

One topic of the ``test_apps_backend_coverage*.py`` family; the doubles and fixtures
the family shares come from ``apps_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import spawn_root_fixture  # noqa: F401
from apps_backend_coverage_helpers import (
    _capture_popen,
    _fake_proc,
    _FakeResp,
    _install_fake_socket,
    _manifest,
    _record_runs,
    _StopSpawn,
)

import kiro_crew.apps.backend as bmod
from kiro_crew.apps.backend import AppProcess

# ---------------------------------------------------------------------------
# Boot reconcile
# ---------------------------------------------------------------------------


def _app(
    name: str, *, enabled: bool = True, origin: str = "builtin", manifest: Any = None
) -> dict[str, Any]:
    return {
        "name": name,
        "enabled": enabled,
        "origin": origin,
        "manifest": {"backend": {"entryPoint": "server.py"}} if manifest is None else manifest,
    }


@pytest.fixture()
def boot_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Stub every collaborator ``start_enabled_app_backends`` reaches out to."""

    calls: dict[str, list[Any]] = {
        "started": [],
        "dereg_mcp": [],
        "dereg_agents": [],
        "dereg_skills": [],
        "register": [],
        "reconcile_skills": [],
    }

    def _start(name: str) -> AppProcess | None:
        calls["started"].append(name)
        return None

    monkeypatch.setattr(bmod, "_reap_stale_app_backends", lambda: 0)
    monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: None)
    monkeypatch.setattr(bmod, "shipped_builtin_app_root", lambda _name: None)
    monkeypatch.setattr(bmod, "app_admission_denied", lambda _name, **_kw: None)
    monkeypatch.setattr(bmod, "app_execution_denied", lambda _name, **_kw: None)
    monkeypatch.setattr(bmod, "start_app_backend", _start)
    monkeypatch.setattr(
        "kiro_crew.apps.backend._app_activation_denied", lambda _name, **_kwargs: None
    )

    def _dereg_mcp(name: str) -> int:
        calls["dereg_mcp"].append(name)
        return 1

    def _dereg_agents(name: str) -> int:
        calls["dereg_agents"].append(name)
        return 1

    def _dereg_skills(name: str) -> int:
        calls["dereg_skills"].append(name)
        return 1

    def _register(name: str) -> Any:
        calls["register"].append(name)
        return SimpleNamespace(errors=[])

    def _reconcile(name: str) -> None:
        calls["reconcile_skills"].append(name)

    monkeypatch.setattr("kiro_crew.apps.bridges._deregister_mcp_servers", _dereg_mcp)
    monkeypatch.setattr("kiro_crew.apps.bridges._deregister_agents", _dereg_agents)
    monkeypatch.setattr("kiro_crew.apps.bridges._deregister_skills", _dereg_skills)
    monkeypatch.setattr("kiro_crew.apps.bridges.register_app", _register)
    monkeypatch.setattr("kiro_crew.apps.bridges.reconcile_app_skills", _reconcile)
    return calls


class TestBootMcpReconcile:
    def test_a_disabled_app_with_mcp_servers_is_scrubbed(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its backend is not running, so its HTTP MCP url points at a dead port."""

        monkeypatch.setattr(
            bmod,
            "list_apps",
            lambda: [_app("off", enabled=False, manifest={"mcpServers": {"x": {}}})],
        )
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["dereg_mcp"] == ["off"]

    def test_a_disabled_app_without_mcp_servers_is_left_alone(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("off", enabled=False, manifest={})])
        bmod.start_enabled_app_backends()
        assert boot_env["dereg_mcp"] == []

    def test_a_failing_scrub_does_not_crash_boot(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(_name: str) -> int:
            raise RuntimeError("mcp.json locked")

        monkeypatch.setattr("kiro_crew.apps.bridges._deregister_mcp_servers", _boom)
        monkeypatch.setattr(
            bmod,
            "list_apps",
            lambda: [_app("off", enabled=False, manifest={"mcpServers": {"x": {}}})],
        )
        with caplog.at_level(logging.WARNING):
            bmod.start_enabled_app_backends()
        assert any("Boot MCP reconcile failed" in r.message for r in caplog.records)


class TestBootResourceReconcile:
    def test_an_admitted_app_is_re_registered_and_its_skills_reconciled(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("good")])
        bmod.start_enabled_app_backends()
        assert boot_env["register"] == ["good"]
        assert boot_env["reconcile_skills"] == ["good"]
        assert boot_env["started"] == ["good"]

    def test_registration_errors_are_surfaced_not_swallowed(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.apps.bridges.register_app",
            lambda _n: SimpleNamespace(errors=["skill clash"]),
        )
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("noisy")])
        with caplog.at_level(logging.WARNING):
            bmod.start_enabled_app_backends()
        assert any("completed with errors" in r.message for r in caplog.records)

    def test_a_failing_reconcile_does_not_crash_boot(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(_name: str) -> Any:
            raise RuntimeError("registry corrupt")

        monkeypatch.setattr("kiro_crew.apps.bridges.register_app", _boom)
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("broken")])
        with caplog.at_level(logging.WARNING):
            bmod.start_enabled_app_backends()
        assert any("Boot resource reconcile failed" in r.message for r in caplog.records)
        # The backend spawn loop is independent of the reconcile outcome.
        assert boot_env["started"] == ["broken"]

    def test_a_denied_app_has_its_derivative_resources_revoked(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A policy tightened after enable must revoke, not merely decline to start."""

        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied",
            lambda _n, **_kwargs: "not in allowlist",
        )
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("banned")])
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["dereg_agents"] == ["banned"]
        assert boot_env["dereg_skills"] == ["banned"]
        assert boot_env["dereg_mcp"] == ["banned"]
        assert boot_env["register"] == []
        assert boot_env["started"] == []

    def test_a_failed_revocation_is_logged_as_an_error(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(_name: str) -> int:
            raise RuntimeError("agents dir read-only")

        monkeypatch.setattr("kiro_crew.apps.bridges._deregister_agents", _boom)
        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied", lambda _n, **_kwargs: "banned"
        )
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("banned")])
        with caplog.at_level(logging.ERROR):
            bmod.start_enabled_app_backends()
        assert any("FAILED to revoke resources" in r.message for r in caplog.records)

    def test_a_vetting_error_is_treated_as_a_denial(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-closed: an unverifiable app must not keep its resources."""

        def _boom(_name: str, **_kw: Any) -> str:
            raise RuntimeError("provenance store unreadable")

        monkeypatch.setattr(bmod, "app_execution_denied", _boom)
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("unknowable")])
        bmod.start_enabled_app_backends()
        assert boot_env["dereg_agents"] == ["unknowable"]
        assert boot_env["register"] == []

    def test_an_unavailable_bridges_module_aborts_only_the_reconcile(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Boot must still start admitted backends when the reconcile cannot load."""

        monkeypatch.setitem(sys.modules, "kiro_crew.apps.bridges", SimpleNamespace())
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("a"), _app("b")])
        with caplog.at_level(logging.WARNING):
            bmod.start_enabled_app_backends()
        assert any("Boot resource reconcile unavailable" in r.message for r in caplog.records)
        assert sorted(boot_env["started"]) == ["a", "b"]


class TestBootSpawnGating:
    def test_a_governance_denied_app_is_not_spawned(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied", lambda _n, **_kwargs: "not allowed"
        )
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("blocked")])
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["started"] == []

    def test_an_enabled_app_without_a_backend_is_skipped(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("uionly", manifest={})])
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["started"] == []

    def test_an_admission_revet_error_fails_closed(
        self,
        boot_env: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An app whose admission cannot be confirmed must not boot unchecked."""

        def _boom(_name: str, **_kw: Any) -> str:
            raise RuntimeError("signature store unreadable")

        monkeypatch.setattr(bmod, "app_admission_denied", _boom)
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("shady", origin="registry")])
        with caplog.at_level(logging.ERROR):
            assert bmod.start_enabled_app_backends() == []
        assert any("treating as denied" in r.message for r in caplog.records)
        assert boot_env["started"] == []

    def test_a_boot_spawn_error_is_isolated_even_if_the_audit_sink_is_down(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _start(name: str) -> AppProcess | None:
            if name == "bad":
                raise RuntimeError("sandbox unavailable")
            boot_env["started"].append(name)
            return None

        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "start_app_backend", _start)
        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("bad"), _app("ok")])
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["started"] == ["ok"]


class TestPreclaimFixedPorts:
    def test_two_apps_declaring_the_same_fixed_port_is_reported_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        port = bmod._MIN_PORT + 13
        manifests = {
            "first": _manifest("s.py", port=str(port)),
            "second": _manifest("s.py", port=str(port)),
        }
        monkeypatch.setattr(bmod, "get_app_manifest", lambda n: manifests.get(n))
        with caplog.at_level(logging.WARNING):
            bmod._preclaim_fixed_ports(["first", "second"])
        assert bmod._allocated_ports == {"first": port}
        assert any("fixed-port pre-claim" in r.message for r in caplog.records)

    def test_an_empty_boot_set_short_circuits(self) -> None:
        assert bmod._start_backends_concurrently([]) == []


# ---------------------------------------------------------------------------
# Remaining defensive branches
# ---------------------------------------------------------------------------


class TestDefensiveBranches:
    def test_an_unreadable_extensionless_entry_is_not_a_shell_launcher(self, tmp_path: Any) -> None:
        """A directory passes the executable check but cannot be sniffed."""

        candidate = tmp_path / "launcher"
        candidate.mkdir()
        assert bmod._is_shell_entry(candidate) is False

    def test_refusing_an_unhealthy_occupant_survives_an_audit_sink_failure(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        _install_fake_socket(monkeypatch, connect_exc=None)

        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(500))
        monkeypatch.setattr(
            bmod, "popen_limited", lambda *_a, **_k: pytest.fail("spawned onto a taken port")
        )
        result = bmod._start_app_backend_body(
            "occupied", _manifest("server.py", port=str(bmod._MIN_PORT + 14))
        )
        assert result is None

    def test_npm_install_proceeds_when_the_audit_sink_is_down(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))

        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(bmod, "_find_npm_binary", lambda: "/usr/bin/npm")
        runs = _record_runs(monkeypatch)
        _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nodeaudit", _manifest("server.js"))
        assert runs and runs[0][0] == "/usr/bin/npm"

    def test_a_missing_npm_binary_does_not_block_the_node_spawn(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dependencies may already be vendored; a missing npm is not fatal."""

        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(bmod, "_find_npm_binary", lambda: None)
        runs = _record_runs(monkeypatch)
        seen = _capture_popen(monkeypatch)
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nonpm", _manifest("server.js"))
        assert runs == []
        assert seen["argv"][0] == "/usr/bin/node"

    def test_stopping_a_spawned_backend_survives_audit_and_signal_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither a broken audit sink nor a pid that vanished may fail the stop."""

        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        signals: list[int] = []

        def _kill(_pid: int, sig: int) -> None:
            signals.append(sig)
            if sig == bmod.platform_compat.SIGKILL:
                raise ProcessLookupError

        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree", _kill)
        proc = _fake_proc(pid=600)
        proc.wait_raises = True
        with bmod._lock:
            bmod._processes["audit"] = AppProcess(app_name="audit", port=9100, pid=600, proc=proc)
        assert bmod.stop_app_backend("audit") is True
        assert signals == [
            bmod.platform_compat.SIGTERM,
            bmod.platform_compat.SIGKILL,
        ]

    def test_refusing_an_adopted_stop_survives_an_audit_sink_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _no_sel)
        with bmod._lock:
            bmod._processes["ext"] = AppProcess(
                app_name="ext", port=9100, pid=0, proc=None, healthy=True
            )
        assert bmod.stop_app_backend("ext") is False
        assert "ext" in bmod._processes

    def test_an_adopted_stop_kill_path_survives_an_audit_sink_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod, "_wait_for_pids", lambda _pids, timeout=2.0: None)

        def _pinned_kill(pid: int, _start_time: str, sig: int) -> bool:
            killed.append((pid, sig))
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", _pinned_kill)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st")
        with bmod._lock:
            bmod._processes["ext"] = AppProcess(
                app_name="ext",
                port=9100,
                pid=0,
                proc=None,
                adopted_pids=[111],
                adopted_start_times={111: "st"},
                healthy=True,
            )
        assert bmod.stop_app_backend("ext") is True
        assert killed == [
            (111, bmod.platform_compat.SIGTERM),
            (111, bmod.platform_compat.SIGKILL),
        ]

    def test_a_boot_admission_denial_survives_an_audit_sink_failure(
        self, boot_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(bmod, "app_admission_denied", lambda _n, **_kw: "unsigned")
        monkeypatch.setattr(bmod, "list_apps", lambda: [_app("shady", origin="registry")])
        assert bmod.start_enabled_app_backends() == []
        assert boot_env["started"] == []


class TestReapDefensiveBranches:
    @pytest.fixture()
    def matched_orphan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A recorded pid that is alive and positively identified."""

        bmod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-1", "port": 9100}})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "ST-1")
        monkeypatch.setattr(
            bmod.platform_compat, "pid_liveness", lambda _pid: bmod.platform_compat.PID_ALIVE
        )
        monkeypatch.setattr(bmod, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(bmod, "_REAP_SIGTERM_GRACE", 0.0)
        monkeypatch.setattr(bmod, "_REAP_POLL_INTERVAL", 0.0)

    def test_malformed_and_nonpositive_entries_are_dropped_without_signalling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bmod._write_pidfile(
            {
                "bad-type": {"pid": "not-an-int", "start_time": "ST", "port": 9100},
                "zero": {"pid": 0, "start_time": "ST", "port": 9101},
            }
        )
        for _name in ("kill_process_tree", "kill_process_tree_pinned"):
            monkeypatch.setattr(
                bmod.platform_compat,
                _name,
                lambda *_a: pytest.fail("signalled an unusable pidfile entry"),
            )
        assert bmod._reap_stale_app_backends() == 0
        assert bmod._read_pidfile() == {}

    def test_a_pid_that_exits_before_the_signal_is_dropped(
        self, matched_orphan: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        def _gone(_pid: int, _expected: str, _sig: int, **kwargs: Any) -> bool:
            raise ProcessLookupError

        # Patched at the PINNED entry point, which is what the reap calls now.
        # Patching the inner ``kill_process_tree`` would leave the real handle
        # work in front of it and make the case host-dependent.
        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree_pinned", _gone)
        assert bmod._reap_stale_app_backends() == 0
        if bmod.platform_compat.IS_WINDOWS:
            assert bmod._read_pidfile() == {
                "app": {"pid": 4321, "start_time": "ST-1", "port": 9100}
            }, "a Windows drain exception does not prove the descendant tree absent"
        else:
            assert bmod._read_pidfile() == {}

    def test_the_reap_survives_an_audit_sink_failure_on_both_signals(
        self, matched_orphan: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_sel() -> Any:
            raise RuntimeError("sel unavailable")

        signals: list[int] = []
        monkeypatch.setattr(bmod, "sel", _no_sel)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree_pinned",
            lambda _pid, _expected, sig, **kwargs: bool(signals.append(sig)) or True,
        )
        assert bmod._reap_stale_app_backends() == 1
        expected = [bmod.platform_compat.SIGTERM]
        if not bmod.platform_compat.IS_WINDOWS:
            expected.append(bmod.platform_compat.SIGKILL)
        assert signals == expected

    def test_a_pid_that_exits_before_the_escalation_is_not_an_error(
        self, matched_orphan: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        def _kill(_pid: int, _expected: str, sig: int, **kwargs: Any) -> bool:
            if sig == bmod.platform_compat.SIGKILL:
                raise ProcessLookupError
            return True

        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree_pinned", _kill)
        assert bmod._reap_stale_app_backends() == 1
        assert bmod._read_pidfile() == {}
