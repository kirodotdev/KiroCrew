"""Whether a pod's gateway holds its port, is healthy, and can be trusted with a minted token.

``main_pid`` reads the unit's ``MainPID``, ``port_owner`` judges the derived port the pod's,
foreign or unproven, ``_wait_healthy`` stops early on a crashed unit and reports a foreign
holder ahead of the crash verdict, and ``mint_token`` refuses a port it cannot prove is the
pod's, sends the credential of the gateway it dials, names the gate that refused and checks
the peer on the pod's unix socket. ``TestRuntimeHelpers`` also covers the unit-state queries.
The shared fixtures come from ``pod_helpers.py``; ``TestPortOwner`` adds its own.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pod_helpers import (  # noqa: F401 -- the fixtures register by import
    _cp,
    _fake_node_toolchain,
    _isolate_pod_host_state,
    _systemd_backend,
    cfg_fixture,
)
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import platform_compat
from kiro_crew.instances import run_marker
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig


def _publish_pod_credential(cfg: PodConfig, name: str, port: int, secret: str = "s3cr3t") -> Path:
    """Publish a per-listener credential for pod *name* the way its gateway does.

    ``_wait_healthy`` only calls a pod ready once the credential the mint will read
    exists, so every test that drives it to a SUCCESS verdict has to stand one up.
    Writing the real per-port path rather than stubbing the predicate keeps these
    tests honest about which file the production reader looks at.
    """
    path = cfg.home_dir(name) / run_marker.RUN_DIR_NAME / run_marker.secret_file_name(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret)
    return path


class TestWaitHealthyFailsFast:
    def test_bails_on_failed_unit(self, cfg: PodConfig) -> None:
        with (
            patch.object(rt, "health", return_value=0),
            patch.object(rt, "unit_state", return_value=("failed", 0)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=45) == -1

    def test_bails_on_crash_loop(self, cfg: PodConfig) -> None:
        with (
            patch.object(rt, "health", return_value=0),
            patch.object(rt, "unit_state", return_value=("activating", 1)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=45) == -1

    def test_returns_code_when_healthy(self, cfg: PodConfig) -> None:
        _publish_pod_credential(cfg, "x", 7999)
        with (
            patch.object(rt, "health", return_value=403),
            patch.object(rt, "unit_state", return_value=("active", 0)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=3) == 403

    def test_a_foreign_port_holder_outranks_the_generic_crash_verdict(self, cfg: PodConfig) -> None:
        """A taken port is WHY the gateway crash-loops, so say that instead.

        Both signals are present in this scenario -- the port answers from
        somebody else AND the unit is restarting behind it -- and only one of them
        tells the operator what to do about it.
        """
        with (
            patch.object(rt, "health", return_value=rt.HEALTH_FOREIGN),
            patch.object(rt, "unit_state", return_value=("activating", 1)),
        ):
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=45) == rt.HEALTH_FOREIGN

    def test_a_foreign_holder_seen_mid_wait_is_remembered_at_the_deadline(
        self, cfg: PodConfig
    ) -> None:
        """The verdict survives a later poll that merely fails to connect."""
        codes = iter([rt.HEALTH_FOREIGN, 0, 0])

        def _health(*a: object, **k: object) -> int:
            return next(codes, 0)

        with (
            patch.object(rt, "health", _health),
            patch.object(rt, "unit_state", return_value=("active", 0)),
        ):
            # flake-ok: real 2 s poll budget; the first poll fixes the verdict -- moved verbatim
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=2) == rt.HEALTH_FOREIGN

    def test_a_pod_that_wins_its_port_back_still_reports_healthy(self, cfg: PodConfig) -> None:
        """Seeing a squatter first must not condemn a pod that then comes up.

        A predecessor releasing the port mid-wait is an ordinary sequence, so the
        foreign observation is attribution for a FAILURE, never a verdict of its
        own.
        """
        codes = iter([rt.HEALTH_FOREIGN, 200])

        def _health(*a: object, **k: object) -> int:
            return next(codes, 200)

        _publish_pod_credential(cfg, "x", 7999)
        with (
            patch.object(rt, "health", _health),
            patch.object(rt, "unit_state", return_value=("active", 0)),
        ):
            # flake-ok: one real 1 s poll interval inside a 5 s budget -- moved verbatim
            assert pod_cli._wait_healthy(cfg, "x", 7999, tries=5) == 200


class TestPortOwner:
    """Identity, not reachability: WHO holds the pod's derived port.

    A derived port maps 199 slots across every pod name and can be pinned by
    hand, so a collision with another pod or with the live gateway is ordinary.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse pid-record stub moved verbatim; no test in this class calls monkeypatch.undo()
    def _recorded_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stand in for a pid record that PROVED fresh.

        This deliberately does NOT pin ``rt.IS_POSIX``, because ``port_owner``
        returned ``OWNER_UNPROVEN`` on a non-POSIX host before consulting any
        helper — so on Windows the decision cases FAILED and the ones expecting
        ``OWNER_UNPROVEN`` passed VACUOUSLY. That platform gate is gone: both facts
        the proof needs now answer on win32, so every case below exercises the
        real branch on every platform and there is nothing left to pin.
        """
        # Every positive ownership verdict requires the gateway's pid sidecar to
        # agree with the service manager's MainPID. Patching the reader stands in
        # for a record that PROVED fresh (it answers ``None`` otherwise), which is
        # what lets these cases vary listener evidence independently; the
        # freshness proof itself is covered in ``test_pod_api.py``.
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: 4242)

    @staticmethod
    def _listener(pid: int, address: str = "127.0.0.1", family: str = "4"):
        return platform_compat.PortListener(pid=pid, address=address, family=family)

    def test_the_pods_own_main_pid_holding_the_port_is_proof_it_is_ours(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", lambda port: [self._listener(4242)])
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_POD

    def test_a_wildcard_bind_by_the_pod_still_counts_as_ours(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0.0.0.0 receives a 127.0.0.1 connect, so a wildcard pod owns the probe."""
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(
            rt, "find_port_listeners", lambda port: [self._listener(4242, "0.0.0.0")]
        )
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_POD

    def test_ownership_is_scoped_to_the_address_the_probe_reached(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pod existing elsewhere on the port must not vouch for a squatter.

        A port NUMBER can carry several LISTEN sockets on different local
        addresses. Here the pod holds one specific non-loopback address while a
        foreign process holds 127.0.0.1 -- the address the probe dialled -- so the
        foreign pid is the responder. Taking every pid on the port number instead
        would find the pod's own and trust the squatter BECAUSE the real pod
        exists.
        """
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(
            rt,
            "find_port_listeners",
            lambda port: [
                self._listener(4242, "10.0.0.5"),  # the pod, on another address
                self._listener(9999, "127.0.0.1"),  # the squatter, on the probed one
            ],
        )
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_FOREIGN

    def test_a_different_pid_holding_the_port_is_foreign(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", lambda port: [self._listener(9999)])
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_FOREIGN

    def test_a_pod_with_no_process_on_a_held_port_is_foreign_not_unproven(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported bug, at its root.

        A pod that lost the bind race has no process at all. If "no pid of ours"
        read as undecidable, the squatter's 200 would still be reported as this
        pod's health -- which is the whole defect. A held port plus a pod that
        authoritatively has no pid IS proof the responder is someone else.
        """
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", lambda port: [self._listener(9999)])
        monkeypatch.setattr(rt, "main_pid", lambda c, n: None)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_FOREIGN

    def test_missing_main_pid_is_unproven_without_listener_tool(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rt, "main_pid", lambda c, n: None)
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: False)
        monkeypatch.setattr(
            rt, "find_port_listeners", lambda port: pytest.fail("must not be reached")
        )
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_UNPROVEN

    def test_windows_can_prove_ownership_because_both_facts_now_answer(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blanket ``not IS_POSIX`` return would refuse Windows here outright.

        That was unsatisfiable rather than strict: ``pod up`` mints a token and
        ``mint_token`` requires positive proof, so every healthy Windows pod
        would have been refused its own credential forever. The proof needs
        exactly two facts and both answer on win32 now — ``pid_start_token`` reads
        the process creation FILETIME, and ``main_pid`` reads the pid the Task
        Scheduler wrapper's ``supervise_gateway`` records.
        """
        monkeypatch.setattr(rt, "IS_WINDOWS", True)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda c, n, p: 4242)
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: False)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_POD

    def test_a_windows_pod_with_no_agreeing_record_is_still_unproven(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Opening the platform gate must not weaken the proof itself."""
        monkeypatch.setattr(rt, "IS_WINDOWS", True)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda c, n, p: None)
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: False)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_UNPROVEN

    def test_an_unaskable_service_manager_is_unproven(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Could not ask" must never be rendered as "not ours"."""

        def _boom(c: object, n: object) -> int:
            raise rt.PodError("systemctl unavailable")

        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", lambda port: [self._listener(9999)])
        monkeypatch.setattr(rt, "main_pid", _boom)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_UNPROVEN

    def test_a_throwing_listener_lookup_keeps_a_fresh_pid_attestation(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken lookup says nothing about who holds the port.

        The autouse fixture supplies a record that PROVED fresh, so downgrading
        it here would punish the pod for the tool's failure. What is refused is
        an UNPROVABLE record -- see ``TestPodRecordFreshness`` in
        ``test_pod_api.py`` for that leg.
        """

        def _boom(port: int) -> list:
            raise OSError("lsof exploded")

        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", _boom)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_POD

    def test_an_unattributable_listener_keeps_a_fresh_pid_attestation(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An installed tool can still fail to attribute the reached socket.

        Exactly what an unprivileged caller sees when the socket belongs to a
        gateway its own process cannot inspect, which is how every pod runs.
        """
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", lambda port: [])
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_POD

    @pytest.mark.parametrize("tool_available, listeners", [(False, None), (True, [])])
    def test_an_unprovable_record_withholds_the_pod_secret(
        self,
        cfg: PodConfig,
        monkeypatch: pytest.MonkeyPatch,
        tool_available: bool,
        listeners: list[platform_compat.PortListener] | None,
    ) -> None:
        """No listener evidence AND no provable record: the secret stays put.

        ``_pod_recorded_pid`` answering ``None`` is what a crash leftover, a
        pre-binding record, and a host that will not report a start time all
        collapse to, so this covers the residual refusal on every one of them.
        """
        home = cfg.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("must-not-leave-this-process")
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: None)
        monkeypatch.setattr(rt, "main_pid", lambda c, n: 4242)
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7999)
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: tool_available)
        if listeners is not None:
            monkeypatch.setattr(rt, "find_port_listeners", lambda port: listeners)
        monkeypatch.setattr(
            rt,
            "loopback_urlopen",
            lambda *a, **k: pytest.fail("the secret must not be sent"),
        )

        assert rt.port_owner(cfg, "demo", 7999) == rt.OWNER_UNPROVEN
        with pytest.raises(rt.PodOwnershipUnproven):
            rt.mint_token(cfg, "demo", "1h")


class TestMainPid:
    def test_reads_systemd_main_pid(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(stdout="MainPID=4242\n"))
        assert rt.main_pid(cfg, "demo") == 4242

    def test_main_pid_zero_means_not_running(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """systemd prints MainPID=0 for a dead or unknown unit, and exits 0."""
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(stdout="MainPID=0\n"))
        assert rt.main_pid(cfg, "demo") is None

    def test_a_missing_main_pid_line_refuses_rather_than_guessing(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No MainPID key at all means the QUERY failed, not that the pod is dead."""
        monkeypatch.setattr(
            rt, "systemctl", lambda *a, **k: _cp(stdout="", returncode=1, stderr="boom")
        )
        with pytest.raises(rt.PodError):
            rt.main_pid(cfg, "demo")


class TestRuntimeHelpers:
    def test_is_active(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        assert rt.is_active(cfg, "x") is True
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=3))
        assert rt.is_active(cfg, "x") is False

    def test_active_names_parses_units(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = (
            "kirocrew-pod@alpha.service    loaded active running x\n"
            "kirocrew-pod@beta-two.service loaded active running y\n"
            "unrelated.service             loaded active running z\n"
        )
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(stdout=out))
        assert rt.active_names(cfg) == {"alpha", "beta-two"}

    def test_unit_state(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            rt, "systemctl", lambda *a, **k: _cp(stdout="ActiveState=failed\nNRestarts=2\n")
        )
        assert rt.unit_state(cfg, "x") == ("failed", 2)

    def test_unit_state_defaults(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(stdout="garbage\n"))
        assert rt.unit_state(cfg, "x") == ("unknown", 0)

    def test_health_codes(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        class _Resp:
            status = 200

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        # Ownership is proven separately (see TestPortOwner); here the pod owns
        # its port so health reports the raw code.
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_POD)
        monkeypatch.setattr(rt, "loopback_urlopen", lambda *a, **k: _Resp())
        assert rt.health(cfg, "demo", 7999) == 200

        def _raise_http(*a: object, **k: object) -> None:
            raise urllib.error.HTTPError("u", 403, "f", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(rt, "loopback_urlopen", _raise_http)
        assert rt.health(cfg, "demo", 7999) == 403

        def _raise_url(*a: object, **k: object) -> None:
            raise urllib.error.URLError("down")

        monkeypatch.setattr(rt, "loopback_urlopen", _raise_url)
        assert rt.health(cfg, "demo", 7999) == 0

    def test_health_treats_a_non_http_listener_as_unreachable(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A process holding the port that does not speak HTTP is not "serving".

        ``BadStatusLine`` is neither ``OSError`` nor ``URLError``, so without an
        explicit catch it escapes as a traceback out of ``pod status`` — and a
        foreign listener on a derived port is exactly when it happens.
        """
        import http.client

        def _raise_bad_status(*a: object, **k: object) -> None:
            raise http.client.BadStatusLine("garbage")

        monkeypatch.setattr(rt, "loopback_urlopen", _raise_bad_status)
        assert rt.health(cfg, "demo", 7999) == 0

    def test_health_reports_a_foreign_responder_as_not_up(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bug: a 200 from somebody else's gateway must not read as up."""

        class _Resp:
            status = 200

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        monkeypatch.setattr(rt, "loopback_urlopen", lambda *a, **k: _Resp())
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_FOREIGN)
        assert rt.health(cfg, "demo", 7999) == rt.HEALTH_FOREIGN

    def test_health_keeps_the_code_when_ownership_is_unprovable(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No listener-lookup tool must not turn every pod into an unhealthy one."""

        class _Resp:
            status = 200

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        monkeypatch.setattr(rt, "loopback_urlopen", lambda *a, **k: _Resp())
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_UNPROVEN)
        assert rt.health(cfg, "demo", 7999) == 200

    def test_health_skips_the_ownership_check_when_nothing_answers(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stopped pod costs one refused connection and no process lookup."""
        import urllib.error

        def _raise_url(*a: object, **k: object) -> None:
            raise urllib.error.URLError("down")

        calls: list[object] = []

        def _owner(*a: object, **k: object) -> str:
            calls.append(a)
            return rt.OWNER_FOREIGN

        monkeypatch.setattr(rt, "loopback_urlopen", _raise_url)
        monkeypatch.setattr(rt, "port_owner", _owner)
        assert rt.health(cfg, "demo", 7999) == 0
        assert calls == []

    def test_mint_token_reads_secret_and_posts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s3cret")

        class _Resp:
            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"token":"tok-xyz"}'

        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_POD)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: 4242)
        monkeypatch.setattr(rt, "unix_socket_urlopen", lambda *a, **k: _Resp())
        monkeypatch.setattr(rt, "loopback_urlopen", lambda *a, **k: _Resp())
        assert rt.mint_token(c, "demo", "1h") == "tok-xyz"

    def test_mint_token_uses_attested_loopback_on_windows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows mints through the ownership-attested loopback port only."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s3cret")

        class _Resp:
            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"token":"tok-tcp"}'

        tcp_calls: list[object] = []
        peer = {"pid": 4242}

        def _tcp(req: object, timeout: float, *, verify_peer: Any) -> "_Resp":
            # As the real opener does: the hook runs on the connected socket
            # before any byte, and a refusal sends nothing.
            verify_peer(object())
            tcp_calls.append(req)
            return _Resp()

        monkeypatch.setattr(rt, "IS_WINDOWS", True)
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_POD)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda *a, **k: 4242)
        monkeypatch.setattr("kiro_crew.cli_server._connected_tcp_peer_pid", lambda _s: peer["pid"])
        monkeypatch.setattr(rt, "tcp_verified_urlopen", _tcp)
        sel = MagicMock()
        monkeypatch.setattr("kiro_crew.cli_server.sel", sel)
        monkeypatch.setattr(
            rt, "loopback_urlopen", lambda *a, **k: pytest.fail("the mint must prove its peer")
        )
        monkeypatch.setattr(
            rt,
            "unix_socket_urlopen",
            lambda *a, **k: pytest.fail("Windows must not use the unix opener"),
        )
        monkeypatch.setattr(
            rt,
            "pod_socket_path",
            lambda *a, **k: pytest.fail("Windows must not build a unix socket path"),
        )
        monkeypatch.setattr(
            rt,
            "_attested_gateway_verifier",
            lambda *a, **k: pytest.fail("Windows must not build a unix peer verifier"),
        )

        assert rt.mint_token(c, "demo", "1h") == "tok-tcp"
        assert len(tcp_calls) == 1
        req = tcp_calls[0]
        assert req.get_header("X-local-secret") == "s3cret"  # type: ignore[attr-defined]
        assert sel.return_value.log_api_access.call_args.kwargs["outcome"] == "allowed"

        # A different process on the far end of the connection receives nothing.
        peer["pid"] = 4243
        with pytest.raises(rt.PodError, match="not the currently attested gateway"):
            rt.mint_token(c, "demo", "1h")
        assert sel.return_value.log_api_access.call_args.kwargs["outcome"] == "denied"
        # An unresolvable peer is refused the same way.
        peer["pid"] = None  # type: ignore[assignment]
        with pytest.raises(rt.PodError, match="unidentifiable process"):
            rt.mint_token(c, "demo", "1h")
        assert len(tcp_calls) == 1

    def test_mint_token_sends_the_secret_only_over_the_pod_socket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The X-Local-Secret request rides the pod's AF_UNIX socket, never TCP.

        A TCP transport would attest ownership and then open a
        SEPARATE loopback connection carrying the secret: a pod exiting inside
        that window frees the port for any local user, and loopback TCP has no
        peer-credential API to tell the squatter from the gateway. The socket
        lives inside the pod's owner-only home, so delivery there cannot reach
        another user -- and `unix_socket_urlopen` has no TCP handler, making
        "no fallback" structural. This test pins both properties: the TCP
        opener is never consulted, and the secret-bearing request lands on
        exactly the pod's own socket path.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s3cret")

        class _Resp:
            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"token":"tok-uds"}'

        tcp_calls: list[object] = []
        uds_calls: list[dict[str, object]] = []

        def _tcp(*a: object, **k: object) -> "_Resp":
            tcp_calls.append(a)
            return _Resp()

        def _uds(
            req: object, timeout: float, *, socket_path: object, verify_peer: object = None
        ) -> "_Resp":
            uds_calls.append({"req": req, "socket_path": socket_path, "verify_peer": verify_peer})
            return _Resp()

        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_POD)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: 4242)
        monkeypatch.setattr(rt, "loopback_urlopen", _tcp)
        monkeypatch.setattr(rt, "unix_socket_urlopen", _uds)

        assert rt.mint_token(c, "demo", "1h") == "tok-uds"
        assert tcp_calls == []  # the secret-bearing call has no TCP path at all
        port = rt.derive_port(c, "demo")
        assert uds_calls[0]["socket_path"] == rt.pod_socket_path(c, "demo", port)
        assert callable(uds_calls[0]["verify_peer"])  # peer check rides every send
        req = uds_calls[0]["req"]
        assert req.get_header("X-local-secret") == "s3cret"  # urllib-normalized key

    def test_mint_token_refuses_a_foreign_port_holder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never hand this pod's secret to whatever happens to answer.

        Sharper than the health misreport: mint sends the pod's own internal-API
        credential -- the per-listener ``run/gateway-<port>.secret`` when the
        gateway published one -- and returns a dashboard token for the responder,
        so against a squatter it prints a URL that drives the wrong instance.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s3cret")

        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_FOREIGN)
        monkeypatch.setattr(
            rt, "loopback_urlopen", lambda *a, **k: pytest.fail("must not dial a foreign listener")
        )
        with pytest.raises(rt.PodError, match="another process"):
            rt.mint_token(c, "demo", "1h")

    def test_mint_token_refuses_when_ownership_is_merely_unproven(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The credential path needs POSITIVE proof, unlike the health readout.

        The two ways of being wrong are not symmetrical. Refusing a live pod costs
        an error message; proceeding on an unproven port hands a different local
        user -- who can bind 127.0.0.1 but cannot read this 0600 secret -- a
        credential for this pod. ``_gateway_owns_port`` fails closed on the same
        path for the same reason, including when the listener lookup is missing.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / ".local_secret").write_text("s3cret")

        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_UNPROVEN)
        monkeypatch.setattr(
            rt,
            "loopback_urlopen",
            lambda *a, **k: pytest.fail("must not dial an unproven listener"),
        )
        with pytest.raises(rt.PodOwnershipUnproven, match="could not prove"):
            rt.mint_token(c, "demo", "1h")

    def test_the_unprovable_refusal_is_a_distinguishable_type(self) -> None:
        """`pod up` treats the two refusals differently, so they must be tellable.

        FOREIGN is positive knowledge that the port is somebody else's; UNPROVEN is
        the absence of knowledge. Both withhold the secret, but only the first
        means there is nothing truthful left for `pod up` to print.
        """
        assert issubclass(rt.PodOwnershipUnproven, rt.PodError)
        # A FOREIGN refusal must NOT be catchable as the unprovable one.
        assert not issubclass(rt.PodError, rt.PodOwnershipUnproven)

    def test_mint_token_no_secret_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        with pytest.raises(rt.PodError):
            rt.mint_token(PodConfig.load(), "ghost", "1h")


class TestMintRefusalNamesTheGateThatRefused:
    """A 403 from ``/api/token/local`` must report the gate that actually refused.

    The endpoint refuses at three independent gates and says which one in the body it
    already sends. Listing candidates instead sends the operator to a remedy for a gate
    that did not refuse, and the remedy those candidates want is a recreate, whose
    ``down`` half reclaims the pod's HOME. No gate this endpoint names is repaired by
    one: transport admission and owner provenance both judge who connected, and a
    credential mismatch can come from a live second gateway in the same pod home
    rather than from the pod's state. Any message that still carries a recreate must
    state what it costs.
    """

    def _refused(self, body: bytes | None) -> object:
        import io
        import urllib.error

        fp = io.BytesIO(body) if body is not None else None

        def _raise(*a: object, **k: object) -> None:
            raise urllib.error.HTTPError(
                "http://127.0.0.1/api/token/local",
                403,
                "Forbidden",
                {},  # type: ignore[arg-type]
                fp,  # type: ignore[arg-type]
            )

        return _raise

    def _mint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        body: bytes | None,
    ) -> str:
        """Drive the real ``mint_token`` unix-socket path to its 403 branch.

        Idempotent in the pod home so one test can drive several causes in turn.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        home = c.home_dir("demo")
        home.mkdir(parents=True, exist_ok=True)
        (home / ".local_secret").write_text("s3cret")
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_POD)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: 4242)
        monkeypatch.setattr(rt, "unix_socket_urlopen", self._refused(body))
        with pytest.raises(rt.PodError) as exc:
            rt.mint_token(c, "demo", "1h")
        return str(exc.value)

    @staticmethod
    def _recreate_cost_is_stated(message: str) -> bool:
        """A message may name ``pod down`` only while stating what it deletes."""
        return "pod down" not in message or "DELETES the pod's HOME" in message

    def test_owner_refusal_is_named_and_not_blamed_on_transport_or_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate that refuses a CALLER must not be reported as the other two.

        This is the refusal a healthy, serving pod produces: the transport was
        admitted and the secret accepted, so naming those two as the candidates
        describes a pod whose state is fine and sends the operator to a recreate
        that cannot change the verdict.
        """
        body = json.dumps(
            {
                "error": "The gateway could not verify this process as the local owner.",
                "code": "member_owner_token_refused",
            }
        ).encode()
        message = self._mint(tmp_path, monkeypatch, body)

        assert "member_owner_token_refused" in message
        assert "unverified-owner-process" in message  # the pod's own audit record
        # The credential is named by ROLE, not as the shared file: the mint reads
        # the per-listener secret first, so that file may not be what was accepted.
        assert ".local_secret" not in message
        # The two causes that did NOT refuse are not offered as candidates.
        assert "predates unix-socket admission" not in message
        assert "stale" not in message
        # And a recreate is not presented as the fix for a caller-provenance verdict.
        assert "does NOT change this verdict" in message

    def test_the_owner_refusal_never_recommends_deleting_the_pod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``pod down`` reclaims the pod's HOME and cannot fix a caller verdict.

        Pinned separately from the wording above because this is the destructive
        half: a remedy that deletes a pod's data must never be printed for a
        refusal it cannot repair, and if it is named at all its cost is stated.
        """
        body = json.dumps({"error": "no", "code": "member_owner_token_refused"}).encode()
        message = self._mint(tmp_path, monkeypatch, body)

        assert "kirocrew pod down demo && kirocrew pod up demo" not in message
        assert self._recreate_cost_is_stated(message)

    def test_transport_refusal_names_the_peer_verdict_not_a_stale_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A code-bearing transport refusal cannot be an outdated gateway.

        The codes ship with the same handler that admits this socket, so a gateway able
        to say `loopback_only` is not one that predates that admission. What it means is
        that the kernel did not positively confirm the connecting peer as the gateway's
        own principal. Blaming a stale worktree there would re-offer the HOME-deleting
        recreate for a refusal it cannot repair, which is this issue's defect moved one
        gate over.
        """
        body = json.dumps({"error": "loopback only", "code": "loopback_only"}).encode()
        message = self._mint(tmp_path, monkeypatch, body)

        assert "loopback_only" in message and "TRANSPORT" in message
        assert "non-loopback" in message  # the pod's own audit record
        assert "peer" in message  # the verdict that actually fired
        # The impossible cause is not offered, and neither is its remedy.
        assert "predates unix-socket admission" not in message
        assert "Update the pod's worktree" not in message
        assert "kirocrew pod down demo && kirocrew pod up demo" not in message
        assert self._recreate_cost_is_stated(message)

    def test_secret_refusal_names_both_causes_and_offers_no_recreate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A credential mismatch is two causes, and neither is repaired by a recreate.

        A gateway that starts while a sibling serves another port in the same pod home
        publishes its credential only to the per-listener file, so the shared slot
        keeps pointing at the sibling. A caller reading that slot is refused here with
        the pod's state perfectly healthy, which makes a recreate both useless and
        destructive.
        """
        body = json.dumps({"error": "invalid secret", "code": "invalid_secret"}).encode()
        message = self._mint(tmp_path, monkeypatch, body)

        assert "invalid_secret" in message and ".local_secret" in message
        assert "TRANSPORT" not in message
        assert "earlier run" in message
        assert "second gateway" in message
        assert "kirocrew pod down" not in message
        assert self._recreate_cost_is_stated(message)

    def test_only_a_codeless_reply_may_blame_an_outdated_gateway(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stale-worktree cause lives in exactly one branch: the code-less one.

        A reply with no code is the only one that can have come from a gateway old
        enough for the missing-admission cause to be real, so that is the only branch
        allowed to name it, and the only transport-flavoured branch allowed to suggest
        updating the worktree before a recreate.
        """
        codeless = self._mint(tmp_path, monkeypatch, json.dumps({"error": "nope"}).encode())
        assert "predates unix-socket admission" in codeless
        assert "updating the pod's worktree" in codeless

        for code in ("loopback_only", "invalid_secret", "member_owner_token_refused"):
            body = json.dumps({"error": "x", "code": code}).encode()
            message = self._mint(tmp_path, monkeypatch, body)
            assert "predates unix-socket admission" not in message, code
            assert "worktree" not in message, code

    def test_a_codeless_body_sends_the_operator_to_the_audit_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a code the honest answer is where the pod wrote the answer.

        A gateway that answers 403 without one leaves three possible gates, so the
        message names the record that distinguishes them instead of picking one.
        """
        message = self._mint(tmp_path, monkeypatch, json.dumps({"error": "nope"}).encode())

        assert "no machine-readable cause" in message
        assert "security_events.jsonl" in message
        assert "non-loopback" in message and "unverified-owner-process" in message
        assert "nope" in message  # the pod's own words are still relayed
        assert self._recreate_cost_is_stated(message)

    def test_the_relayed_words_cannot_drive_the_operators_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pod chooses this text, and `pod` prints it to a terminal unchanged.

        A pod's gateway runs that pod's own worktree checkout, so the ``error``
        string is chosen by the code under test, and it reaches the operator through
        a plain write to stderr. Control bytes would let it repaint the screen, and
        a newline would let it forge further lines of this very message.
        """
        hostile = "\x1b[2Jwiped\x07 \x1b]0;retitled\x07\r\nfake: pod is fine"
        message = self._mint(tmp_path, monkeypatch, json.dumps({"error": hostile}).encode())

        for forbidden in ("\x1b", "\x07", "\r"):
            assert forbidden not in message
        # The escape BODIES survive as inert text; only the control bytes are gone.
        assert "wiped" in message
        # And the forged line is folded into the one line the relay occupies.
        relayed = [ln for ln in message.splitlines() if "It said:" in ln]
        assert len(relayed) == 1 and "fake: pod is fine" in relayed[0]

    def test_an_overlong_relayed_error_is_capped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 64 KiB body is readable, so the quoted slice needs its own bound.

        The body cap admits far more text than a terminal line holds, so without a
        second bound one reply could bury the remedy under its own length.
        """
        message = self._mint(tmp_path, monkeypatch, json.dumps({"error": "A" * 5000}).encode())

        assert "A" * rt._MAX_ECHOED_DETAIL_LEN in message
        assert "A" * (rt._MAX_ECHOED_DETAIL_LEN + 1) not in message
        # The remedy is still reachable after the quote.
        assert "security_events.jsonl" in message

    @pytest.mark.parametrize("body", [None, b"", b"<html>403</html>", b"[]"])
    def test_an_unreadable_body_still_produces_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes | None
    ) -> None:
        """Reading the body must not be able to replace the 403 with a crash.

        A missing file object, an empty body, HTML and valid JSON that is not an
        object all reach the same reader, and a raise there would lose the refusal.
        """
        message = self._mint(tmp_path, monkeypatch, body)

        assert "HTTP 403" in message
        assert "no machine-readable cause" in message
        assert self._recreate_cost_is_stated(message)

    def test_the_no_fallback_guarantee_survives_the_rewrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal still states that the secret was not retried over TCP.

        That sentence is the security property of this path, not decoration: it
        tells the operator the secret was never offered to whatever holds the port.
        """
        body = json.dumps({"error": "x", "code": "member_owner_token_refused"}).encode()
        message = self._mint(tmp_path, monkeypatch, body)

        assert "Not retried on 127.0.0.1:" in message

    def test_a_body_over_the_cap_is_read_no_further_and_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An over-long reply is not parsed for a cause, however well it is formed.

        The reader is what a refused mint hands an untrusted length, so it takes at
        most the cap plus the one byte that proves the cap was passed. A reply longer
        than every 403 this route sends is not one of them, so its ``code`` is not
        honoured: it takes the same path as a body that could not be read at all.
        """
        padding = " " * (rt._MINT_403_BODY_CAP + 1)
        body = (json.dumps({"error": "x", "code": "invalid_secret"}) + padding).encode()
        message = self._mint(tmp_path, monkeypatch, body)

        assert "no machine-readable cause" in message
        assert "invalid_secret" not in message
        assert self._recreate_cost_is_stated(message)

    def test_no_code_bearing_remedy_blames_a_rebound_socket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rebound socket cannot produce any of these replies, so none may name it.

        ``_attested_gateway_verifier`` runs as the connection's peer check and raises
        before the request line is written, so a rebound path never answers with HTTP
        at all. Offering it as a cause here repeats this refusal's original defect:
        sending the operator after something the path itself has already excluded.
        """
        for code in ("member_owner_token_refused", "loopback_only", "invalid_secret"):
            body = json.dumps({"error": "x", "code": code}).encode()
            message = self._mint(tmp_path, monkeypatch, body)

            assert "rebound" not in message and "rebind" not in message, code

    def test_the_owner_remedy_states_the_condition_of_the_running_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner gate is not one condition, so the remedy may not name only one.

        ``local_owner_bootstrap_allowed`` accepts an ordinary host process by the
        running platform's own measure: matching user and mount namespaces on Linux,
        and the absence of a sandbox profile on macOS. A remedy naming namespaces
        alone tells a sandboxed macOS caller to fix something that is not the gate it
        failed.
        """
        body = json.dumps({"error": "x", "code": "member_owner_token_refused"}).encode()
        message = self._mint(tmp_path, monkeypatch, body)

        assert "namespace" in message
        assert "sandbox" in message
        assert "Linux" in message and "macOS" in message


class TestMintSendsTheCredentialOfTheGatewayItDials:
    """The mint must authenticate as the generation that owns the port it dials.

    The shared ``.local_secret`` holds one slot per data home. A gateway starting
    while a sibling still serves another port in that home publishes its credential
    only to the per-listener file, deliberately leaving the shared slot pointing at
    the sibling. A mint reading the slot then presents one generation's credential to
    another and is refused with nothing wrong with the pod, and the refusal is
    indistinguishable at the wire from a credential left behind by an earlier run.
    """

    @staticmethod
    def _capturing_urlopen(seen: dict) -> object:
        """A stub gateway that records the credential the mint presented."""

        class _Resp:
            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"token":"tok-xyz"}'

        def _open(req: object, *a: object, **k: object) -> "_Resp":
            seen["secret"] = req.get_header("X-local-secret")  # type: ignore[attr-defined]
            return _Resp()

        return _open

    def _pod(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[object, int]:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        c.home_dir("demo").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_POD)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: 4242)
        return c, rt.derive_port(c, "demo")

    def test_the_per_listener_credential_wins_over_the_shared_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With both files present the mint sends the one paired with the port.

        This is the live-sibling shape: the shared slot names the sibling, and the
        per-listener file names the generation actually answering this port.
        """
        c, port = self._pod(tmp_path, monkeypatch)
        (c.home_dir("demo") / ".local_secret").write_text("sibling-generation")
        per_port = rt._pod_secret_path(c, "demo", port)
        per_port.parent.mkdir(parents=True, exist_ok=True)
        per_port.write_text("owns-this-port")

        seen: dict[str, str] = {}
        monkeypatch.setattr(rt, "unix_socket_urlopen", self._capturing_urlopen(seen))

        assert rt.mint_token(c, "demo", "1h") == "tok-xyz"
        assert seen["secret"] == "owns-this-port"

    def test_the_shared_slot_is_used_when_no_per_listener_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gateway predating the per-listener file is still mintable."""
        c, _ = self._pod(tmp_path, monkeypatch)
        (c.home_dir("demo") / ".local_secret").write_text("only-shared")

        seen: dict[str, str] = {}
        monkeypatch.setattr(rt, "unix_socket_urlopen", self._capturing_urlopen(seen))

        assert rt.mint_token(c, "demo", "1h") == "tok-xyz"
        assert seen["secret"] == "only-shared"

    def test_neither_credential_present_names_both_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal must say where it looked, in the order it looked."""
        c, port = self._pod(tmp_path, monkeypatch)

        with pytest.raises(rt.PodError) as exc:
            rt.mint_token(c, "demo", "1h")

        message = str(exc.value)
        assert str(rt._pod_secret_path(c, "demo", port)) in message
        assert ".local_secret" in message


class TestMintPeerVerification:
    """Connect-time peer verification on the pod's unix socket.

    ``port_owner`` proves the pid RECORD is fresh; these tests pin the other
    half: the process ANSWERING the socket file must be that recorded pid, as
    read from the kernel's peer credentials on the connected socket, before a
    single HTTP byte (and the ``X-Local-Secret`` header behind it) is sent.
    """

    def _grant_record_freshness(self, monkeypatch: pytest.MonkeyPatch, pid: int) -> None:
        monkeypatch.setattr(rt, "port_owner", lambda *a, **k: rt.OWNER_POD)
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: pid)

    def test_verifier_refuses_a_mismatched_peer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        self._grant_record_freshness(monkeypatch, 4242)
        monkeypatch.setattr(rt, "get_peer_pid", lambda sock: 9999)
        verify = rt._attested_gateway_verifier(c, "demo", 7999, Path("/x/sock"))
        with pytest.raises(rt.PodError, match="refusing to send"):
            verify(object())

    def test_verifier_refuses_when_attestation_expires_before_connect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The connected peer needs a fresh record, not only a matching pid."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        attestations = iter((4242, None))
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: next(attestations))
        monkeypatch.setattr(rt, "get_peer_pid", lambda sock: 4242)
        verify = rt._attested_gateway_verifier(c, "demo", 7999, Path("/x/sock"))
        with pytest.raises(rt.PodError, match="not the currently attested gateway"):
            verify(object())

    def test_verifier_refuses_an_unreadable_peer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``None`` from the kernel is a refusal, never a pass (deny-by-default)."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        self._grant_record_freshness(monkeypatch, 4242)
        monkeypatch.setattr(rt, "get_peer_pid", lambda sock: None)
        verify = rt._attested_gateway_verifier(c, "demo", 7999, Path("/x/sock"))
        with pytest.raises(rt.PodError, match="unidentifiable process"):
            verify(object())

    def test_verifier_refuses_when_no_pid_is_attested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No provable record at send time = no verifier at all, fail closed."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        monkeypatch.setattr(rt, "_pod_recorded_pid", lambda cfg, name, port: None)
        with pytest.raises(rt.PodOwnershipUnproven):
            rt._attested_gateway_verifier(c, "demo", 7999, Path("/x/sock"))

    def test_verifier_accepts_the_attested_peer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path))
        c = PodConfig.load()
        self._grant_record_freshness(monkeypatch, 4242)
        monkeypatch.setattr(rt, "get_peer_pid", lambda sock: 4242)
        verify = rt._attested_gateway_verifier(c, "demo", 7999, Path("/x/sock"))
        assert verify(object()) is None  # no raise = the send may proceed

    @staticmethod
    def _serve_once(server_sock: socket.socket, received: list[bytes]) -> threading.Thread:
        """Accept one connection and record whatever arrives (empty = refused)."""

        def _run() -> None:
            try:
                conn, _ = server_sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                data = b""
                try:
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    pass
                received.append(data)
                if data:
                    body = b'{"token":"tok-live"}'
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
                    )

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return thread

    def _mint_against_live_listener(
        self, monkeypatch: pytest.MonkeyPatch, attested_pid: int
    ) -> tuple[list[bytes], "rt.PodError | None"]:
        """Drive the REAL kernel peer check: a live in-process unix listener
        (peer pid = ``os.getpid()``) answers the pod's derived socket path,
        while the attested record names *attested_pid*. Only record freshness
        is granted; ``get_peer_pid`` runs unpatched against the real socket.
        """
        if platform_compat.IS_WINDOWS:
            pytest.skip("AF_UNIX transport is POSIX-only")
        root = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "podpeer-", dir=short_tmp_base()))
        try:
            monkeypatch.setenv("KIROCREW_POD_ROOT", str(root))
            c = PodConfig.load()
            home = c.home_dir("demo")
            home.mkdir(parents=True)
            (home / ".local_secret").write_text("s3cret")
            port = rt.derive_port(c, "demo")
            socket_path = rt.pod_socket_path(c, "demo", port)
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(socket_path))
                server.listen(1)
                received: list[bytes] = []
                thread = self._serve_once(server, received)
                self._grant_record_freshness(monkeypatch, attested_pid)
                error: rt.PodError | None = None
                try:
                    assert rt.mint_token(c, "demo", "1h") == "tok-live"
                except rt.PodError as exc:
                    error = exc
                server.close()
                # flake-ok: the mint has returned before this join -- moved verbatim
                thread.join(timeout=5)
                return received, error
            finally:
                server.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_mint_refuses_a_rebound_socket_before_any_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rebind window: a same-UID process rebinds the socket path.

        The listener is real and answering, the pid record is fresh, but the
        kernel says the peer is this test process, not the attested gateway
        (pid 1). The mint must refuse AND the listener must observe zero bytes
        -- the request line, let alone the secret header, never went out.
        """
        received, error = self._mint_against_live_listener(monkeypatch, attested_pid=1)
        assert error is not None and "refusing to send" in str(error)
        # flake-ok: both values mean no HTTP byte reached the listener -- moved verbatim
        assert received in ([], [b""])  # connection at most; never a byte of HTTP

    def test_mint_sends_when_the_kernel_names_the_attested_peer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Benign path, no new denials: attest THIS process, the real
        ``SO_PEERCRED``/``LOCAL_PEERPID`` read agrees, the mint completes."""
        received, error = self._mint_against_live_listener(monkeypatch, attested_pid=os.getpid())
        assert error is None
        assert len(received) == 1 and b"X-Local-Secret: s3cret" in received[0]
