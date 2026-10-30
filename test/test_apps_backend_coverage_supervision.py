"""Unit coverage for :mod:`kiro_crew.apps.backend`: supervising a running backend.

When the proxy port is published, the pidfile helpers' error paths,
``_proc_start_time``, the MCP registration gate and the health-check loop, and the
restart of a backend that exited: its admission and governance re-vets, the backoff
and steady state, the startup health gate and the attempt count it keeps or resets,
and the handoff when a start or a stop races the restart.

One topic of the ``test_apps_backend_coverage*.py`` family; the doubles and fixtures
the family shares come from ``apps_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import _fake_proc, _FakeResp, _manifest

import kiro_crew.apps.backend as bmod
from kiro_crew.apps.backend import AppProcess

# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def test_the_proxy_port_is_only_published_once_the_backend_is_healthy() -> None:
    """Publishing a pre-health port routes user traffic at a dead socket."""

    with bmod._lock:
        bmod._processes["p"] = AppProcess(app_name="p", port=9111, pid=1, healthy=False)
    assert bmod.get_app_backend_port("p") is None
    with bmod._lock:
        bmod._processes["p"].healthy = True
    assert bmod.get_app_backend_port("p") == 9111
    assert bmod.get_app_backend_port("absent") is None


# ---------------------------------------------------------------------------
# Pidfile helpers
# ---------------------------------------------------------------------------


class TestPidfileHelpers:
    def test_a_corrupt_pidfile_reads_as_empty_and_is_reported(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silently swallowing it would disable the very reap it exists for."""

        bmod._pidfile_path().write_text("{not json")
        with caplog.at_level(logging.WARNING):
            assert bmod._read_pidfile() == {}
        assert any("pidfile unreadable" in r.message for r in caplog.records)

    def test_a_non_mapping_pidfile_reads_as_empty(self, tmp_path: Any) -> None:
        bmod._pidfile_path().write_text("[1, 2, 3]")
        assert bmod._read_pidfile() == {}

    def test_an_unwritable_pidfile_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError("read-only filesystem")

        bmod._write_pidfile({"kept": {"pid": 7}})
        monkeypatch.setattr(bmod, "atomic_write", _boom)
        bmod._write_pidfile({"app": {"pid": 1}})
        assert bmod._read_pidfile() == {"kept": {"pid": 7}}

    def test_recording_a_pid_never_breaks_a_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_pid: int) -> str:
            raise RuntimeError("ps exploded")

        monkeypatch.setattr(bmod, "_proc_start_time", _boom)
        bmod._record_app_pid("app", 4321, 9100)  # must not raise
        assert bmod._read_pidfile() == {}

    def test_forgetting_a_pid_never_breaks_a_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> dict[str, dict[str, Any]]:
            raise RuntimeError("disk gone")

        bmod._write_pidfile({"app": {"pid": 7}})
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(bmod, "_read_pidfile", _boom)
            bmod._forget_app_pid("app")
        assert not bmod._pidfile_lock.locked()
        assert bmod._read_pidfile() == {"app": {"pid": 7}}


class TestProcStartTime:
    """The wrapper must not re-implement the per-platform probe.

    Every platform source (Linux /proc field 22, the Windows creation
    FILETIME, the BSD ``ps`` leg) and its fail-safe behaviour is pinned in
    test_platform_compat_process_identity::TestProcessStartTime. What matters here is that this
    module reads identity from that shim, because a /proc-or-ps probe answers
    None for every pid on Windows and a recorded None makes the stale-reap
    decline to confirm any backend at all.
    """

    def test_it_delegates_to_the_platform_shim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []

        def _probe(pid: int) -> str:
            seen.append(pid)
            return "ST-FROM-SHIM"

        monkeypatch.setattr(bmod.platform_compat, "process_start_time", _probe)
        assert bmod._proc_start_time(4242) == "ST-FROM-SHIM"
        assert seen == [4242]


class TestGateMcpRegistration:
    def test_a_healthy_backend_is_registered_with_its_live_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, int]] = []
        monkeypatch.setattr(
            "kiro_crew.apps.bridges.reregister_app_mcp_servers",
            lambda name, live_port, io_failures=None: seen.append((name, live_port)),
        )
        bmod._gate_mcp_registration("app", 9133, healthy=True)
        assert seen == [("app", 9133)]

    def test_an_unhealthy_backend_has_its_entries_scrubbed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A dead MCP url breaks every kiro-cli session, not just this app."""

        monkeypatch.setattr("kiro_crew.apps.bridges._deregister_mcp_servers", lambda _n: 2)
        with caplog.at_level(logging.WARNING):
            bmod._gate_mcp_registration("app", 9133, healthy=False)
        assert any("Scrubbed 2 MCP server(s)" in r.getMessage() for r in caplog.records)

    def test_a_reconcile_failure_never_crashes_the_health_loop(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("mcp.json locked")

        monkeypatch.setattr("kiro_crew.apps.bridges.reregister_app_mcp_servers", _boom)
        with caplog.at_level(logging.WARNING):
            bmod._gate_mcp_registration("app", 9133, healthy=True)
        assert any("Health-gated MCP registration failed" in r.message for r in caplog.records)


class TestHealthCheckLoop:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the class's zero health-check interval, moved verbatim; tracked for a de-flake pass
    def _instant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 2)

    def test_an_error_status_is_retried_and_then_scrubbed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: gate.append((name, port, healthy)),
        )
        attempts = {"n": 0}

        def _urlopen(*_a: Any, **_k: Any) -> Any:
            attempts["n"] += 1
            return _FakeResp(500)

        monkeypatch.setattr(bmod, "loopback_urlopen", _urlopen)
        with bmod._lock:
            bmod._processes["sick"] = AppProcess(app_name="sick", port=9134)
        bmod._health_check_loop(
            bmod._processes.get("sick") or bmod.AppProcess(app_name="sick", port=9134), "/health"
        )
        assert attempts["n"] == 2
        assert gate == [("sick", 9134, False)]

    def test_an_app_stopped_between_the_probe_and_the_commit_is_not_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registering here would write the dead-url entry the gate exists to avoid."""

        gate: list[Any] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: gate.append((name, port, healthy)),
        )

        def _urlopen(*_a: Any, **_k: Any) -> Any:
            # The disable lands after the top-of-loop guard, before the commit.
            with bmod._lock:
                bmod._processes.pop("racy", None)
            return _FakeResp(200)

        monkeypatch.setattr(bmod, "loopback_urlopen", _urlopen)
        with bmod._lock:
            bmod._processes["racy"] = AppProcess(app_name="racy", port=9135)
        bmod._health_check_loop(
            bmod._processes.get("racy") or bmod.AppProcess(app_name="racy", port=9135), "/health"
        )
        assert gate == []


class TestRestartExitedBackend:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the class's recorded restart backoff, moved verbatim; tracked for a de-flake pass
    def _fast_backoff(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        delays: list[float] = []
        monkeypatch.setattr(
            bmod,
            "shutdown_event",
            SimpleNamespace(
                is_set=lambda: False,
                wait=lambda delay: delays.append(delay) or False,
            ),
        )
        return delays

    @staticmethod
    def _track(name: str = "app") -> AppProcess:
        ap = AppProcess(
            app_name=name,
            port=9136,
            pid=617,
            proc=_fake_proc(pid=617, returncode=-15),
            healthy=False,
            mcp_healthy=False,
        )
        with bmod._lock:
            bmod._processes[name] = ap
            bmod._allocated_ports[name] = ap.port
        return ap

    def test_an_exited_tracked_backend_restarts_through_start_path(
        self, monkeypatch: pytest.MonkeyPatch, _fast_backoff: list[float]
    ) -> None:
        ap = self._track()
        started: list[str] = []
        replacement = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            bmod,
            "_demote",
            lambda record, reason: setattr(record, "mcp_healthy", False),
        )

        def _start(name: str) -> AppProcess:
            started.append(name)
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        bmod._watch_backend_health_sweeps(ap, "/health")
        assert started == ["app"]
        assert _fast_backoff[-1:] == [0.0]
        assert bmod._processes["app"] is replacement

    @pytest.mark.parametrize("enabled", [False, None])
    def test_disabled_or_unknown_app_is_not_restarted(
        self, monkeypatch: pytest.MonkeyPatch, enabled: bool | None
    ) -> None:
        ap = self._track()
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: enabled)
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not restart without confirmed enablement"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        assert bmod._processes["app"] is ap

    def test_restart_revet_reads_origin_without_syncing_installed_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps import manager

        stored = manager.InstalledApp(
            name="app",
            version="1.0.0",
            origin="registry",
            lifecycle="app",
        )
        manifest = SimpleNamespace(version="2.0.0", backend=SimpleNamespace(entryPoint="server.py"))
        monkeypatch.setattr(bmod, "_app_activation_denied", lambda _name, **_kwargs: None)
        monkeypatch.setattr(bmod, "_read_installed", lambda _name: stored)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: manifest)
        monkeypatch.setattr(bmod, "app_admission_denied", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            manager,
            "_write_installed",
            lambda *_args, **_kwargs: pytest.fail("restart re-vet wrote installed metadata"),
        )

        assert bmod._activation_denied("app", "restart") == bmod.ActivationVerdict()
        assert stored.version == "1.0.0"

    def test_restart_stops_when_nonbuiltin_activation_is_denied(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ap = self._track()
        audit = MagicMock()
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "sel", lambda: audit)
        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied", lambda _name, **_kwargs: None
        )
        monkeypatch.setattr(
            "kiro_crew.apps.backend._read_installed",
            lambda _name: SimpleNamespace(origin="registry"),
        )
        monkeypatch.setattr(
            bmod,
            "app_admission_denied",
            lambda _name, **_kwargs: "signature required",
        )
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("denied restart reached the spawn seam"),
        )

        with caplog.at_level(logging.WARNING):
            assert bmod._restart_exited_backend(ap, -15) is False

        assert (
            sum(
                "backend not restarted: blocked by activation policy" in record.message
                for record in caplog.records
            )
            == 1
        )
        audit.log_api_access.assert_called_once_with(
            caller="gateway",
            operation="app_backend_restart",
            outcome="denied",
            resources="app",
            error="signature required",
        )

    def test_admission_revet_error_stops_restart_and_audits_denial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        audit = MagicMock()
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "sel", lambda: audit)
        monkeypatch.setattr(bmod, "_app_activation_denied", lambda _name, **_kwargs: None)
        monkeypatch.setattr(
            bmod, "_read_installed", lambda _name: SimpleNamespace(origin="registry")
        )

        def _raise_admission(_name: str, **_kwargs: Any) -> str:
            raise RuntimeError("signature store unreadable")

        monkeypatch.setattr(bmod, "app_admission_denied", _raise_admission)
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("admission re-vet error reached the spawn seam"),
        )

        assert bmod._restart_exited_backend(ap, -15) is False
        assert "app" not in bmod._processes
        audit.log_api_access.assert_called_once_with(
            caller="gateway",
            operation="app_backend_restart",
            outcome="denied",
            resources="app",
            error="admission re-vet error: signature store unreadable",
        )

    def test_platform_composition_error_stops_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        audit = MagicMock()
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "sel", lambda: audit)

        def _raise_composition(_name: str, **_kwargs: Any) -> str:
            raise bmod.PlatformCompositionError("governance composition failed")

        monkeypatch.setattr(bmod, "_app_activation_denied", _raise_composition)
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("composition error reached the spawn seam"),
        )

        assert bmod._restart_exited_backend(ap, -15) is False
        assert "app" not in bmod._processes
        audit.log_api_access.assert_called_once_with(
            caller="gateway",
            operation="app_backend_restart",
            outcome="denied",
            resources="app",
            error="platform composition error: governance composition failed",
        )

    def test_governance_evaluation_error_refuses_one_attempt_then_retries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        _fast_backoff: list[float],
    ) -> None:
        ap = self._track()
        audit = MagicMock()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        strict_calls: list[bool] = []

        def _evaluate(*_args: Any, **kwargs: Any) -> Any:
            strict_calls.append(kwargs.get("fail_closed") is True)
            if len(strict_calls) == 1:
                return SimpleNamespace(
                    permitted=False,
                    reason=f"{bmod.GOVERNANCE_ERROR_REASON}; denied (fail-closed)",
                    rule="default",
                    layer="profile",
                )
            return SimpleNamespace(permitted=True)

        def _start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            _evaluate,
        )
        monkeypatch.setattr(
            bmod, "_read_installed", lambda _name: SimpleNamespace(origin="builtin")
        )
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "sel", lambda: audit)

        with caplog.at_level(logging.WARNING):
            assert bmod._restart_exited_backend(ap, -15) is True

        assert strict_calls == [True, True]
        assert _fast_backoff == [0.0, 1.0]
        assert bmod._restart_attempts["app"] == 2
        assert bmod._processes["app"] is replacement
        assert (
            sum(
                "restart activation evaluation failed" in record.message
                for record in caplog.records
            )
            == 1
        )
        assert audit.log_api_access.call_args_list == [
            call(
                caller="gateway",
                operation="app_backend_restart",
                outcome="error",
                resources="app",
                error=f"{bmod.GOVERNANCE_ERROR_REASON}; denied (fail-closed)",
            ),
            call(
                caller="gateway",
                operation="app_backend_restart",
                outcome="allowed",
                resources="app attempt=2",
            ),
        ]

    def test_builtin_restart_ignores_signature_admission_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied", lambda _name, **_kwargs: None
        )
        monkeypatch.setattr(
            "kiro_crew.apps.backend._read_installed",
            lambda _name: SimpleNamespace(origin="builtin"),
        )
        monkeypatch.setattr(
            bmod,
            "app_admission_denied",
            lambda _name, **_kwargs: pytest.fail("builtin restart reached signature admission"),
        )

        def _start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._processes["app"] is replacement

    @pytest.mark.parametrize("audit_sink_fails", [False, True])
    def test_permitted_restart_is_audited_before_the_spawn(
        self, monkeypatch: pytest.MonkeyPatch, audit_sink_fails: bool
    ) -> None:
        ap = self._track()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        events: list[str] = []
        audit = MagicMock()
        if audit_sink_fails:
            audit.log_api_access.side_effect = OSError("sel sink unavailable")
        else:
            audit.log_api_access.side_effect = lambda **_kw: events.append("audit")
        monkeypatch.setattr(bmod, "sel", lambda: audit)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "_app_activation_denied", lambda _name, **_kwargs: None)
        monkeypatch.setattr(
            bmod, "_read_installed", lambda _name: SimpleNamespace(origin="registry")
        )
        monkeypatch.setattr(bmod, "app_admission_denied", lambda _name, **_kwargs: None)

        def _start(name: str) -> AppProcess:
            events.append("spawn")
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._processes["app"] is replacement
        audit.log_api_access.assert_called_once_with(
            caller="gateway",
            operation="app_backend_restart",
            outcome="allowed",
            resources="app attempt=1",
        )
        assert events == ["spawn"] if audit_sink_fails else ["audit", "spawn"]

    def test_governance_denial_blocks_builtin_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            "kiro_crew.apps.backend._app_activation_denied",
            lambda _name, **_kwargs: "not in apps allowlist",
        )
        monkeypatch.setattr(
            "kiro_crew.apps.backend._read_installed",
            lambda _name: SimpleNamespace(origin="builtin"),
        )
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("governance-denied builtin restarted"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False

    def test_an_intentionally_dropped_record_is_not_restarted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        with bmod._health_reconcile_lock:
            with bmod._lock:
                bmod._advance_lifecycle_locked("app", bmod._LIFECYCLE_STOP)
                bmod._processes.pop("app")
                bmod._allocated_ports.pop("app")
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not restart a stopped record"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False

    def test_gateway_shutdown_prevents_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ap = self._track()
        monkeypatch.setattr(bmod, "shutdown_event", SimpleNamespace(is_set=lambda: True))
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not restart during gateway shutdown"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False

    def test_ramp_exhaustion_enters_steady_state_and_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ap = self._track()
        started: list[str] = []
        enabled = True
        monkeypatch.setattr(bmod, "_RESTART_ON_EXIT_FAST_ATTEMPTS", 2)
        monkeypatch.setattr(bmod, "_RESTART_STEADY_INTERVAL", 0)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: enabled)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _fail_until_disabled(name: str) -> None:
            nonlocal enabled
            started.append(name)
            if len(started) == 5:
                enabled = False
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _fail_until_disabled)
        with caplog.at_level(logging.WARNING):
            assert bmod._restart_exited_backend(ap, -15) is False
        assert started == ["app"] * 5
        assert bmod._restart_attempts["app"] == 5
        assert (
            sum(
                "still failing after 2 fast restart attempts" in record.message
                for record in caplog.records
            )
            == 1
        )

    def test_shutdown_during_steady_wait_prevents_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        waits: list[float] = []
        monkeypatch.setattr(bmod, "_RESTART_ON_EXIT_FAST_ATTEMPTS", 2)
        monkeypatch.setattr(bmod, "_RESTART_STEADY_INTERVAL", 7)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(
            bmod,
            "shutdown_event",
            SimpleNamespace(
                is_set=lambda: False,
                wait=lambda delay: waits.append(delay) or True,
            ),
        )
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("shutdown must prevent the steady-state spawn"),
        )
        with bmod._lock:
            bmod._restart_attempts["app"] = 2

        assert bmod._restart_exited_backend(ap, -15) is False
        assert waits == [7]

    def test_successor_pid_record_survives_restart_handoff_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        ap.pid_start_time = "old-start"
        bmod._write_pidfile(
            {"app": {"pid": ap.pid, "start_time": ap.pid_start_time, "port": ap.port}}
        )
        successor = AppProcess(
            app_name="app",
            port=9137,
            pid=618,
            pid_start_time="new-start",
            proc=_fake_proc(pid=618),
        )
        real_forget = bmod._forget_app_pid_if

        def _race_successor_record(name: str, pid: int, start_time: str | None) -> None:
            bmod._write_pidfile(
                {name: {"pid": successor.pid, "start_time": successor.pid_start_time, "port": 9137}}
            )
            real_forget(name, pid, start_time)

        def _start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = successor
            return successor

        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", _race_successor_record)
        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._read_pidfile()["app"] == {
            "pid": 618,
            "start_time": "new-start",
            "port": 9137,
        }

    def test_a_stop_during_a_failed_spawn_prevents_restore_and_further_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        started: list[str] = []
        monkeypatch.setattr(bmod, "_RESTART_ON_EXIT_FAST_ATTEMPTS", 2)
        monkeypatch.setattr(bmod, "_RESTART_STEADY_INTERVAL", 0)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        with bmod._lock:
            bmod._restart_attempts["app"] = 2

        def _fail_after_stop(name: str) -> None:
            started.append(name)
            assert bmod.stop_app_backend(name) is False
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _fail_after_stop)
        assert bmod._restart_exited_backend(ap, -15) is False
        assert started == ["app"]
        assert bmod._processes == {}
        assert bmod._lifecycle_generation["app"][1] == bmod._LIFECYCLE_STOP

    def test_a_stop_during_restart_publication_stops_the_replacement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        stopped: list[tuple[int, int]] = []
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda pid, signal: stopped.append((pid, signal)),
        )

        def _start(name: str) -> AppProcess:
            # STOP lands after the old record was removed but before publication.
            assert bmod.stop_app_backend(name) is False
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is False
        assert bmod._processes == {}
        assert stopped == [(618, bmod.platform_compat.SIGTERM)]

    def test_disable_then_quick_reenable_during_spawn_keeps_the_replacement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        stopped: list[tuple[int, int]] = []
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda pid, signal: stopped.append((pid, signal)),
        )

        def _enabled_start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        def _restart_start(name: str) -> AppProcess:
            # Disable records STOP while the restart owns the spawn. The immediate
            # public start records a later START and reuses/publishes the replacement.
            assert bmod.stop_app_backend(name) is False
            monkeypatch.setattr(bmod, "_start_app_backend", _enabled_start)
            assert bmod.start_app_backend(name) is replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _restart_start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._processes["app"] is replacement
        assert bmod._lifecycle_generation["app"][1] == bmod._LIFECYCLE_START
        assert stopped == []

    def test_superseding_start_failure_keeps_restart_supervision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        successor = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        starts: list[str] = []
        awaits: list[str] = []
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _start(name: str) -> AppProcess | None:
            starts.append(name)
            if len(starts) == 1:
                # A public START records intent, but its spawn fails and clears its
                # placeholder before the restart supervisor compares generations.
                with bmod._health_reconcile_lock:
                    with bmod._lock:
                        bmod._advance_lifecycle_locked(name, bmod._LIFECYCLE_START)
                        bmod._processes.pop(name, None)
                return None
            with bmod._lock:
                bmod._processes[name] = successor
            return successor

        def _await(name: str) -> None:
            assert bmod._lock.acquire(blocking=False)
            bmod._lock.release()
            awaits.append(name)
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert awaits == ["app"]
        assert starts == ["app", "app"]
        assert bmod._processes["app"] is successor
        assert "app" not in bmod._allocated_ports

    def test_superseding_start_warns_once_after_unresolved_awaits(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ap = self._track()
        placeholder = AppProcess(app_name="app", starting=True)
        successor = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        with bmod._lock:
            bmod._processes["app"] = placeholder
        monkeypatch.setattr(bmod, "_SETTLE_UNRESOLVED_WARN_AFTER", 2)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        awaits = 0

        def _await(name: str) -> None:
            nonlocal awaits
            awaits += 1
            if awaits == 4:
                with bmod._lock:
                    bmod._processes[name] = successor

        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        with caplog.at_level(logging.WARNING):
            assert bmod._settle_superseding_start("app", ap, None) == "return_true"

        assert awaits == 4
        assert (
            sum("superseding start unresolved after" in record.message for record in caplog.records)
            == 1
        )

    def test_superseding_start_success_ends_restart_supervision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        successor = AppProcess(
            app_name="app",
            port=9137,
            proc=_fake_proc(pid=618),
        )
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _start(name: str) -> None:
            with bmod._health_reconcile_lock:
                with bmod._lock:
                    bmod._advance_lifecycle_locked(name, bmod._LIFECYCLE_START)
                    bmod._processes[name] = successor
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "_await_inflight_spawn", lambda _name: successor)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._processes["app"] is successor

    def test_superseding_start_then_stop_cancels_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        stopped: list[tuple[int, int]] = []
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(
            bmod.platform_compat,
            "kill_process_tree",
            lambda pid, signal: stopped.append((pid, signal)),
        )

        def _start(name: str) -> AppProcess:
            with bmod._health_reconcile_lock:
                with bmod._lock:
                    bmod._advance_lifecycle_locked(name, bmod._LIFECYCLE_START)
                    bmod._processes[name] = replacement
            return replacement

        def _await(name: str) -> None:
            assert bmod.stop_app_backend(name) is True
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        assert bmod._restart_exited_backend(ap, -15) is False
        assert "app" not in bmod._processes
        assert bmod._lifecycle_generation["app"][1] == bmod._LIFECYCLE_STOP
        assert stopped == [(618, bmod.platform_compat.SIGTERM)]

    def test_start_failure_during_backoff_restores_restart_supervision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        placeholder = AppProcess(app_name="app", starting=True)
        successor = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        waits: list[float] = []
        starts: list[str] = []
        awaits: list[str] = []
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        with bmod._lock:
            bmod._restart_attempts["app"] = 1

        def _wait(delay: float) -> bool:
            waits.append(delay)
            if len(waits) == 1:
                with bmod._health_reconcile_lock:
                    with bmod._lock:
                        bmod._advance_lifecycle_locked("app", bmod._LIFECYCLE_START)
                        bmod._processes["app"] = placeholder
            return False

        def _await(name: str) -> None:
            awaits.append(name)
            with bmod._lock:
                assert bmod._processes.get(name) is placeholder
                bmod._processes.pop(name)
            return None

        def _start(name: str) -> AppProcess:
            starts.append(name)
            with bmod._lock:
                bmod._processes[name] = successor
            return successor

        monkeypatch.setattr(
            bmod,
            "shutdown_event",
            SimpleNamespace(is_set=lambda: False, wait=_wait),
        )
        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert awaits == ["app"]
        assert starts == ["app"]
        assert waits == [1.0, 1.0]
        assert bmod._processes["app"] is successor

    def test_start_failure_at_loop_top_restores_restart_supervision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        placeholder = AppProcess(app_name="app", starting=True)
        successor = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        awaits: list[str] = []
        starts: list[str] = []
        with bmod._health_reconcile_lock:
            with bmod._lock:
                bmod._advance_lifecycle_locked("app", bmod._LIFECYCLE_START)
                bmod._processes["app"] = placeholder
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _await(name: str) -> None:
            awaits.append(name)
            with bmod._lock:
                assert bmod._processes.get(name) is placeholder
                bmod._processes.pop(name)
            return None

        def _start(name: str) -> AppProcess:
            starts.append(name)
            with bmod._lock:
                bmod._processes[name] = successor
            return successor

        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert awaits == ["app"]
        assert starts == ["app"]
        assert bmod._processes["app"] is successor

    def test_start_failure_in_failed_spawn_handoff_restores_supervision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        placeholder = AppProcess(app_name="app", starting=True)
        successor = AppProcess(app_name="app", port=9137, proc=_fake_proc(pid=618))
        starts: list[str] = []
        awaits: list[str] = []
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _start(name: str) -> AppProcess | None:
            starts.append(name)
            with bmod._lock:
                if len(starts) == 1:
                    bmod._processes[name] = placeholder
                    return None
                bmod._processes[name] = successor
            return successor

        def _await(name: str) -> None:
            awaits.append(name)
            with bmod._lock:
                assert bmod._processes.get(name) is placeholder
                bmod._processes.pop(name)
            return None

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "_await_inflight_spawn", _await)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert starts == ["app", "app"]
        assert awaits == ["app"]
        assert bmod._processes["app"] is successor
        assert "app" not in bmod._allocated_ports

    def test_startup_health_gate_exit_continues_the_restart_sequence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        started: list[str] = []
        replacement = AppProcess(
            app_name="app",
            port=9137,
            pid=618,
            proc=_fake_proc(pid=618),
        )
        with bmod._lock:
            bmod._restart_attempts["app"] = 1
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "_health_check_loop", lambda *_args: None)

        def _start(name: str) -> AppProcess:
            started.append(name)
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        bmod._supervise_backend_health(ap, "/health")
        assert started == ["app"]
        assert bmod._restart_attempts["app"] == 2
        assert bmod._processes["app"] is replacement

    def test_startup_health_gate_tracks_live_process_until_later_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _fake_proc(pid=617)
        ap = AppProcess(
            app_name="app",
            port=9136,
            pid=617,
            proc=proc,
            healthy=False,
            mcp_healthy=False,
        )
        with bmod._lock:
            bmod._processes["app"] = ap
        restart_calls: list[int | None] = []

        def _startup_gate(record: AppProcess, _health_path: str) -> None:
            assert record.proc is not None
            assert record.proc.poll() is None
            return None

        def _exit_after_startup(_delay: float) -> None:
            proc.returncode = -15

        def _restart(record: AppProcess, returncode: int | None) -> bool:
            assert record is ap
            restart_calls.append(returncode)
            return False

        monkeypatch.setattr(bmod, "_health_check_loop", _startup_gate)
        # flake-ok: a pre-existing in-test stand-in for the backend module's time.sleep, undone by monkeypatch at teardown; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- a pre-existing in-test stand-in for the backend module's time.sleep moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(bmod.time, "sleep", _exit_after_startup)
        monkeypatch.setattr(bmod, "_restart_exited_backend", _restart)

        bmod._supervise_backend_health(ap, "/health")

        assert restart_calls == [-15]

    def test_startup_exit_retries_an_unlanded_scrub_before_restarting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track()
        ap.mcp_healthy = None
        restarted: list[str] = []
        replacement = AppProcess(app_name="app", port=9137, pid=618, proc=_fake_proc(pid=618))
        gate_calls = 0
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_health_check_loop", lambda *_args: None)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _gate(*_args: Any, **_kwargs: Any) -> bool:
            nonlocal gate_calls
            gate_calls += 1
            return gate_calls == 2

        def _start(name: str) -> AppProcess:
            restarted.append(name)
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_gate_mcp_registration", _gate)
        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        bmod._supervise_backend_health(ap, "/health")
        assert gate_calls == 2
        assert restarted == ["app"]
        assert bmod._processes["app"] is replacement

    def test_startup_health_gate_exhaustion_warns_once_across_generations(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ap = self._track()
        replacement = AppProcess(
            app_name="app",
            port=9137,
            pid=618,
            proc=_fake_proc(pid=618, returncode=-15),
            mcp_healthy=False,
        )
        starts: list[str] = []
        monkeypatch.setattr(bmod, "_RESTART_ON_EXIT_FAST_ATTEMPTS", 1)
        monkeypatch.setattr(bmod, "_RESTART_STEADY_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
        monkeypatch.setattr(bmod, "_health_check_loop", lambda *_args: None)

        def _start(name: str) -> AppProcess:
            starts.append(name)
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        with caplog.at_level(logging.WARNING):
            assert bmod._restart_exited_backend(ap, -15) is True
            bmod._supervise_backend_health(replacement, "/health")
        assert starts == ["app", "app"]
        assert (
            sum(
                "still failing after 1 fast restart attempts" in record.message
                for record in caplog.records
            )
            == 1
        )

    def test_health_gate_keeps_the_restart_attempt_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = AppProcess(app_name="app", port=9138, proc=_fake_proc(pid=619), healthy=False)
        with bmod._lock:
            bmod._processes["app"] = ap
            bmod._restart_attempts["app"] = 2
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_gate_mcp_registration", lambda *_a, **_kw: True)
        assert bmod._set_backend_health(ap, healthy=True) is True
        assert bmod._restart_attempts["app"] == 2

    def test_sustained_health_resets_the_restart_attempt_count(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ap = AppProcess(
            app_name="app",
            port=9138,
            proc=_fake_proc(pid=619),
            healthy=True,
            mcp_healthy=True,
        )
        sweeps = 0
        with bmod._lock:
            bmod._processes["app"] = ap
            bmod._restart_attempts["app"] = 2
        monkeypatch.setattr(
            bmod, "_health_probe", lambda *_args: bmod.HealthProbeOutcome.answered(200)
        )

        def _sleep(_delay: float) -> None:
            nonlocal sweeps
            sweeps += 1
            if sweeps > bmod._RESTART_STABLE_SWEEPS:
                with bmod._lock:
                    bmod._processes.pop("app", None)

        # flake-ok: a pre-existing in-test stand-in for the backend module's time.sleep, undone by monkeypatch at teardown; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- a pre-existing in-test stand-in for the backend module's time.sleep moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(bmod.time, "sleep", _sleep)
        with caplog.at_level(logging.INFO):
            bmod._watch_backend_health_sweeps(ap, "/health")
        assert sweeps == bmod._RESTART_STABLE_SWEEPS + 1
        assert "app" not in bmod._restart_attempts
        assert sum("backend recovered after restart" in r.message for r in caplog.records) == 1

    def test_steady_state_success_publishes_replacement(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ap = self._track()
        replacement = AppProcess(
            app_name="app",
            port=9137,
            pid=618,
            proc=_fake_proc(pid=618),
        )
        monkeypatch.setattr(bmod, "_RESTART_ON_EXIT_FAST_ATTEMPTS", 2)
        monkeypatch.setattr(bmod, "_RESTART_STEADY_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)

        def _start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        with bmod._lock:
            bmod._restart_attempts["app"] = 2
        with caplog.at_level(logging.WARNING):
            assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._processes["app"] is replacement
        assert bmod._restart_attempts["app"] == 3
        assert (
            sum(
                "still failing after 2 fast restart attempts" in record.message
                for record in caplog.records
            )
            == 1
        )
