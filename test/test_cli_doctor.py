"""Tests for the `kirocrew doctor` OS-aware fix hints.

Guards _os_fix_hint: it returns the macOS Homebrew command on Darwin and the
Linux/AL2023 guidance otherwise, so `kirocrew doctor` never prints a brew
command on Linux where there is no brew.

The sign-in and KAS sections, the install and launcher checks, the model and
channel sections and the cron and directory censuses live in
``test_cli_doctor_agent_auth.py``, ``test_cli_doctor_install.py``,
``test_cli_doctor_config_sections.py`` and ``test_cli_doctor_censuses.py``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import make_dir_link
from kiro_crew import cli_doctor


class TestManagedServicePolicyDoctor:
    def test_no_service_is_silent(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli_doctor.service_controller,
            "installed_service_has_managed_marker",
            lambda: None,
        )
        issues: list[str] = []
        cli_doctor._doctor_managed_service_policy(issues)
        assert capsys.readouterr().out == ""
        assert issues == []

    def test_stale_service_names_the_one_time_fix(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli_doctor.service_controller,
            "installed_service_has_managed_marker",
            lambda: False,
        )
        issues: list[str] = []
        cli_doctor._doctor_managed_service_policy(issues)
        output = capsys.readouterr().out
        assert "kirocrew service install" in output
        assert "managed-service defaults" in output
        assert issues == ["managed service definition is outdated"]

    def test_current_service_reports_managed_policy(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli_doctor.service_controller,
            "installed_service_has_managed_marker",
            lambda: True,
        )
        issues: list[str] = []
        cli_doctor._doctor_managed_service_policy(issues)
        assert "managed-service policy marker installed" in capsys.readouterr().out
        assert issues == []


class TestFixHint:
    """OS-aware `kirocrew doctor` fix hints."""

    def test_os_fix_hint_macos_returns_brew(self, monkeypatch) -> None:
        monkeypatch.setattr(cli_doctor._plat, "system", lambda: "Darwin")
        assert (
            cli_doctor._os_fix_hint("brew install ffmpeg", "static build") == "brew install ffmpeg"
        )

    def test_os_fix_hint_linux_returns_linux_guidance(self, monkeypatch) -> None:
        monkeypatch.setattr(cli_doctor._plat, "system", lambda: "Linux")
        assert cli_doctor._os_fix_hint("brew install ffmpeg", "static build") == "static build"

    def test_os_fix_hint_windows_returns_windows_arm(self, monkeypatch) -> None:
        monkeypatch.setattr(cli_doctor._plat, "system", lambda: "Windows")
        assert (
            cli_doctor._os_fix_hint("brew x", "linux x", windows="winget install Gyan.FFmpeg")
            == "winget install Gyan.FFmpeg"
        )

    def test_os_fix_hint_windows_falls_back_to_linux_without_arm(self, monkeypatch) -> None:
        # No Windows arm supplied → keep the Linux text rather than inventing one.
        monkeypatch.setattr(cli_doctor._plat, "system", lambda: "Windows")
        assert cli_doctor._os_fix_hint("brew x", "linux x") == "linux x"


class TestFfmpegLinuxHintResolvable:
    """The Linux missing-ffmpeg hint names only locations the resolver searches.

    An earlier hint told the user to drop a static build into ``~/.local/bin``,
    which ``transcribe._find_ffmpeg`` deliberately never searches (its candidate
    list documents removing that directory), so following the advice literally
    still ended at "not found". Hold the hint against the resolver's own candidate
    list rather than freezing its prose.
    """

    def test_hint_does_not_name_the_deliberately_excluded_dir(self) -> None:
        assert ".local/bin" not in cli_doctor._FFMPEG_LINUX_HINT

    def test_every_directory_named_is_actually_searched(self) -> None:
        from kiro_crew import transcribe

        dirs = re.findall(r"/[A-Za-z0-9._/-]+", cli_doctor._FFMPEG_LINUX_HINT)
        assert dirs, "the hint must name at least one concrete install directory"
        for directory in dirs:
            assert (
                directory in transcribe._FFMPEG_CANDIDATE_DIRS
            ), f"{directory} is in the doctor hint but _find_ffmpeg never searches it"

    def test_hint_offers_the_managed_store_download(self) -> None:
        # The store download is the remedy that needs no PATH reasoning and works
        # on distros with no packaged ffmpeg (the AL2023 case the old hint cited).
        # Anchored to the decoder table, not the prose alone: if the pinned Linux
        # artifacts were ever dropped, the hint would promise a download the
        # gateway refuses (409 decoder_unsupported_platform).
        from kiro_crew.stt import decoder

        assert "dashboard" in cli_doctor._FFMPEG_LINUX_HINT
        assert decoder.artifact_for("Linux", "x86_64") is not None
        assert decoder.artifact_for("Linux", "aarch64") is not None


class TestDataHome:
    """`kirocrew doctor` Data Home section — location + leftover legacy home."""

    def test_legacy_present_default_path_says_not_the_data_home(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # A leftover top-level ~/.kirocrew on the default path is not the data
        # home — the doctor notes it as safe to delete, never as active state.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)  # default-path case
        home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        home.mkdir(parents=True)
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()
        (legacy / "config.json").write_text("{}", encoding="utf-8")

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "not the data home" in out
        assert "ACTIVE" not in out

    def test_legacy_override_points_at_legacy_says_active_not_ignored(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # KIROCREW_HOME=~/.kirocrew makes the legacy dir the ACTIVE home, not
        # ignored debris — the doctor must not mislabel the home the process is
        # actually using (GPT 5.6 MEDIUM).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(legacy))
        # config_dir() resolves to the override (== legacy) when set
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: legacy.resolve())

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "ACTIVE data home" in out
        assert "IGNORED" not in out
        assert "will retry on next cold start" not in out

    def test_legacy_with_venv_is_never_advised_deletable(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # An older wheel install could nest its managed venv inside ~/.kirocrew,
        # so the leftover dir may hold the running interpreter. The doctor must
        # NOT tell the user it is safe to delete — that would remove their live
        # install.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: tmp_path / ".kiro" / "crew")
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        (legacy / "venv" / "bin").mkdir(parents=True)

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "Do NOT delete" in out
        assert "virtual environment" in out and "venv" in out
        assert "safe to delete" not in out

    def test_no_legacy_stays_quiet(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # Fresh install: only the location line, no leftover-legacy nag.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: tmp_path / ".kiro" / "crew")

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "Data Home" in out
        assert "legacy:" not in out
        assert "rm -rf" not in out

    def test_a_workspace_root_over_the_data_home_is_flagged(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        from kiro_crew.browser_cli import install as browser_install

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        home = tmp_path / ".kiro" / "crew"
        home.mkdir(parents=True)
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        monkeypatch.setattr(browser_install, "_agent_writable_roots", lambda: (tmp_path.resolve(),))

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert f"workspace:   ⚠️  {str(tmp_path.resolve())!r} contains the data home" in out
        assert "outside the data home" in out

    def test_a_workspace_root_beside_the_data_home_is_not_flagged(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        from kiro_crew.browser_cli import install as browser_install

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        home = tmp_path / ".kiro" / "crew"
        home.mkdir(parents=True)
        workspace = tmp_path / "workplace" / "kirocrew-workspace"
        workspace.mkdir(parents=True)
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        monkeypatch.setattr(
            browser_install, "_agent_writable_roots", lambda: (workspace.resolve(),)
        )

        cli_doctor._doctor_data_home()

        assert "workspace:" not in capsys.readouterr().out

    def test_location_prints_the_symlink_resolved_spelling(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # A data home reached through a ``current`` symlink to a versioned
        # directory is printed as its canonical target, so it cannot read as a
        # second install beside the (already canonical) PATH launcher row.
        versioned = tmp_path / "install" / "1.2.3"
        versioned.mkdir(parents=True)
        current = tmp_path / "install" / "current"
        make_dir_link(current, versioned)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: current)

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert f"location:    ✅ {os.path.realpath(versioned)}" in out
        assert str(current) not in out


class TestPodSessionBus:
    """`kirocrew doctor` Pods section — the systemd --user session bus.

    Pods are systemd --user units. A gateway started from a systemd SYSTEM unit
    inherits no login-session environment, and if the per-user instance is not
    running at all there is nothing to point at — every pod verb then fails with
    "Failed to connect to bus: No medium found". Doctor reports the three states,
    never gates its exit code on them (an absent bus means an optional dev
    feature is unavailable, not a broken install), and never changes the user's
    login-session lifetime itself.
    """

    @staticmethod
    def _linux(monkeypatch, tmp_path: Path, *, bus: bool, template: bool = True) -> Path:
        from kiro_crew.pod import runtime as rt

        monkeypatch.setattr(cli_doctor.sys, "platform", "linux")
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda n: f"/usr/bin/{n}")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setenv("USER", "tester")
        # Pin the per-user manager probe. Without this the outcome depends on
        # whether the RUNNER ships user@.service, which would make every test
        # below environment-dependent: a container without the template would
        # take the not-applicable branch and never reach the case under test.
        # Patched on the runtime module because the doctor imports the name
        # locally at call time, which keeps the pod package out of the CLI's
        # import graph for every other command.
        from kiro_crew.pod import runtime as _rt

        monkeypatch.setattr(
            _rt,
            "user_manager_unit",
            lambda uid=None: "/usr/lib/systemd/system/user@.service" if template else None,
        )
        sock = tmp_path / "bus"
        if bus:
            sock.touch()
        status = rt.USER_BUS_REACHABLE if bus else rt.USER_BUS_NO_SESSION
        detail = "degraded" if bus else "Failed to connect to bus: No medium found"
        monkeypatch.setattr(
            rt,
            "probe_user_bus",
            lambda: rt.UserBusProbe(status=status, socket=sock, detail=detail),
        )
        return sock

    def test_sandboxed_away_bus_names_outer_layer_and_host_shell(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        from kiro_crew.pod import runtime as rt

        sock = self._linux(monkeypatch, tmp_path, bus=True)
        monkeypatch.setattr(
            rt,
            "probe_user_bus",
            lambda: rt.UserBusProbe(
                status=rt.USER_BUS_SANDBOXED_AWAY,
                socket=sock,
                detail="Failed to connect to bus: Permission denied",
            ),
        )
        monkeypatch.setattr(
            cli_doctor,
            "_linger_enabled",
            lambda _user: pytest.fail("linger is irrelevant when the bus is unreachable"),
        )
        issues: list[str] = []

        cli_doctor._doctor_pod_session_bus(issues)

        out = capsys.readouterr().out
        assert "sandboxed away" in out
        assert "outer layer" in out
        assert "host shell" in out
        assert "Failed to connect to bus: Permission denied" in out
        assert issues == []

    def test_missing_bus_is_reported_but_never_blocks(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # A container / CI runner / headless server has no per-user systemd
        # instance. That is an unavailable optional feature, not a broken
        # install, so it must NOT gate doctor's exit code — otherwise every
        # such host is told its setup is broken (and `kirocrew doctor` starts
        # exiting 1 in CI).
        sock = self._linux(monkeypatch, tmp_path, bus=False)
        issues: list[str] = ["pre-existing"]

        cli_doctor._doctor_pod_session_bus(issues)

        out = capsys.readouterr().out
        assert "Pods" in out
        assert str(sock) in out
        assert "loginctl enable-linger tester" in out
        assert "Everything else works" in out
        assert issues == ["pre-existing"], "the missing bus must not add an issue"

    def test_present_bus_passes_and_stays_quiet_when_lingering(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        sock = self._linux(monkeypatch, tmp_path, bus=True)
        monkeypatch.setattr(cli_doctor, "_linger_enabled", lambda _u: True)
        issues: list[str] = []

        cli_doctor._doctor_pod_session_bus(issues)

        out = capsys.readouterr().out
        assert f"✅ {sock}" in out
        assert "linger" not in out
        assert issues == []

    def test_present_bus_without_linger_warns_but_does_not_block(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Pods work right now and die on logout — a warning, not an issue.
        self._linux(monkeypatch, tmp_path, bus=True)
        monkeypatch.setattr(cli_doctor, "_linger_enabled", lambda _u: False)
        issues: list[str] = []

        cli_doctor._doctor_pod_session_bus(issues)

        out = capsys.readouterr().out
        assert "linger:" in out and "⚠️" in out
        assert "loginctl enable-linger tester" in out
        assert issues == []

    def test_unknown_linger_stays_quiet(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # No loginctl / unparseable value → say nothing rather than guess.
        self._linux(monkeypatch, tmp_path, bus=True)
        monkeypatch.setattr(cli_doctor, "_linger_enabled", lambda _u: None)
        issues: list[str] = []

        cli_doctor._doctor_pod_session_bus(issues)

        assert "linger:" not in capsys.readouterr().out
        assert issues == []

    def test_non_linux_is_not_applicable(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli_doctor.sys, "platform", "darwin")
        issues: list[str] = []

        cli_doctor._doctor_pod_session_bus(issues)

        out = capsys.readouterr().out
        assert "not applicable" in out
        assert issues == []

    def test_no_systemctl_is_not_applicable(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli_doctor.sys, "platform", "linux")
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda _n: None)
        issues: list[str] = []

        cli_doctor._doctor_pod_session_bus(issues)

        out = capsys.readouterr().out
        assert "not applicable" in out and "systemctl" in out
        assert issues == []

    def test_no_per_user_manager_is_not_applicable_not_a_dead_end_fix(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """An EL7 host can never run a pod, so enable-linger must not be offered.

        systemd is present and the platform is Linux, so the two gates above both
        pass and the old code fell through to `❌ ... Fix: loginctl enable-linger`.
        On a host with no `user@.service` that fix cannot work: linger only tells
        logind to instantiate a template that is not installed, so the reader sets
        a flag, nothing starts, and the report still says the setup is broken.
        """
        self._linux(monkeypatch, tmp_path, bus=False, template=False)
        issues: list[str] = ["pre-existing"]

        cli_doctor._doctor_pod_session_bus(issues)

        out = capsys.readouterr().out
        assert "not applicable" in out
        assert "dev-backend.sh" in out
        # The dead-end remedy must NOT be recommended on this host.
        assert "Fix: loginctl enable-linger" not in out
        # Advisory like every other branch: it reports a limit, it never blocks.
        assert issues == ["pre-existing"]

    def test_a_live_socket_does_not_make_a_manager_less_host_look_supported(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The socket alone is a false positive, so the doctor outranks it.

        Observed on Amazon Linux 2: `has_session_bus()` returns True because a stray
        session `dbus-daemon` created `/run/user/<uid>/bus`, while no per-user manager
        exists at all. Testing the socket first would print `✅ session bus` on a host
        where `kirocrew pod` provably cannot work, which is the misdirection this
        change exists to remove.

        This is intentionally stricter than `require_systemd()`, which passes on a live
        socket. The doctor only prints, so a false ⏹ costs a sentence; refusing in the
        runtime gate on a probe that cannot enumerate every unit location would break a
        working host.
        """
        self._linux(monkeypatch, tmp_path, bus=True, template=False)

        cli_doctor._doctor_pod_session_bus([])

        out = capsys.readouterr().out
        assert "not applicable" in out
        assert "dev-backend.sh" in out
        assert "✅" not in out
        assert "Fix: loginctl enable-linger" not in out


class TestLingerProbe:
    """`loginctl show-user <u> -p Linger` → tri-state, via the canonical probe."""

    def _run(self, monkeypatch, *, stdout: str, returncode: int = 0):
        import subprocess

        from kiro_crew.service import linux as service_linux

        monkeypatch.setattr(service_linux, "trusted_system_bin", lambda _n: "/usr/bin/loginctl")
        monkeypatch.setattr(
            service_linux.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout=stdout, stderr=""
            ),
        )
        return cli_doctor._linger_enabled("tester")

    def test_yes_is_true(self, monkeypatch) -> None:
        assert self._run(monkeypatch, stdout="Linger=yes\n") is True

    def test_no_is_false(self, monkeypatch) -> None:
        assert self._run(monkeypatch, stdout="Linger=no\n") is False

    def test_unparseable_is_unknown(self, monkeypatch) -> None:
        assert self._run(monkeypatch, stdout="Linger=wat\n") is None

    def test_nonzero_exit_is_unknown(self, monkeypatch) -> None:
        assert self._run(monkeypatch, stdout="", returncode=1) is None

    def test_absent_loginctl_is_unknown(self, monkeypatch) -> None:
        from kiro_crew.service import linux as service_linux

        monkeypatch.setattr(service_linux, "trusted_system_bin", lambda _n: None)
        assert cli_doctor._linger_enabled("tester") is None


class TestTrustRoot:
    """`kirocrew doctor` reports whether session identities can be signed.

    Publication reports the same failure, but only once a session is actually
    claimed; doctor answers without waiting for one. It must not, however, cry
    wolf on a fresh install whose key has legitimately never been created.
    """

    def test_healthy_trust_root_prints_the_resolved_path(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        key = tmp_path / "trust" / "sel_hmac.key"
        key.parent.mkdir(parents=True)
        key.write_bytes(b"\x01" * 32)
        monkeypatch.setattr(cli_doctor, "signing_health", lambda: (True, key))
        cli_doctor._doctor_trust_root()
        out = capsys.readouterr().out
        assert "trust root:  ✅" in out
        assert str(key) in out

    def test_broken_trust_root_names_what_stops_working(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        key = tmp_path / "trust" / "sel_hmac.key"
        key.parent.mkdir(parents=True)  # dir exists, key gone → genuinely broken
        monkeypatch.setattr(cli_doctor, "signing_health", lambda: (False, key))
        cli_doctor._doctor_trust_root()
        out = capsys.readouterr().out
        assert "⚠ trust root" in out
        assert "sub-agent" in out and "memory" in out

    def test_fresh_home_is_informational_not_a_warning(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Trust dir and key are created together, so neither present means no
        instance has ever run here — not a broken install."""
        key = tmp_path / "trust" / "sel_hmac.key"
        monkeypatch.setattr(cli_doctor, "signing_health", lambda: (False, key))
        cli_doctor._doctor_trust_root()
        out = capsys.readouterr().out
        assert "not created yet" in out
        assert "⚠" not in out


class TestUnresolvedMcpRefs:
    """`kirocrew doctor` answers "why does my agent have no tools here?" statically.

    The runtime detector in ``acp/mcp_ref_guard`` reports the same thing from inside
    a session; this row reports it before one, per selectable harness. Advisory by
    design -- a harness with no projection yet is a known state of the tree, not a
    broken install, so it must never move doctor's exit code.
    """

    def _arrange(self, monkeypatch, rows, *, spec_found=True):
        """Fixture the SDK delegation the row asks.

        Patched at ``agent_sdk.drivers.acp``, its defining module, because the row
        imports it inside the function (doctor keeps its import graph lazy). That
        the row asks ONE boundary-clean question rather than assembling the answer
        from the spec, the backend registry and the mirror seam is the reason this
        fixture is a single return value -- and is what keeps `doctor_checks/mcp.py`, the
        row's home, off the agent-sdk-boundary baseline.
        """
        from kiro_crew.agent_sdk.drivers import acp as acp_driver

        monkeypatch.setattr(acp_driver, "agent_spec_mcp_refs", lambda _agent: (spec_found, rows))

    def test_a_backend_with_no_projection_names_the_unprojected_refs(self, monkeypatch, capsys):
        self._arrange(monkeypatch, [("codex", ["@kirocrew-core"], False)])
        cli_doctor._doctor_unresolved_mcp_refs()
        out = capsys.readouterr().out
        assert "codex has no mirror" in out
        assert "@kirocrew-core" in out

    def test_the_no_mirror_verdict_claims_only_what_the_wire_proves(self, monkeypatch, capsys):
        """The static half hedges exactly as the runtime line does, on every backend.

        The row is the same detector before a session exists, so it may not assert
        what the runtime line stopped asserting: the harness may mount a same-named
        server from its own configuration, which neither half reads, so the row
        says a listed ref may still be served and never that the tools are absent.
        One sentence, no backend condition; the broker-stub caveat and the registry
        pointer stay. Called at the row's own home in ``doctor_checks.mcp`` (the
        ``cli_doctor`` name is the facade's re-export of the same function).
        """
        from kiro_crew.doctor_checks import mcp as doctor_mcp

        for backend in ("goose", ""):
            self._arrange(monkeypatch, [(backend, ["@ghost"], False)])
            doctor_mcp._doctor_unresolved_mcp_refs()
            # ``_print_wrapped`` folds the paragraph, so compare on collapsed whitespace.
            out = " ".join(capsys.readouterr().out.split())
            assert "may mount a same-named server from its own configuration" in out
            assert "this row cannot tell which" in out
            assert "absent from its sessions" not in out
            assert "nothing to say so" not in out
            assert "broker stub, which this row does not model" in out
            assert "providers/mirrors/registry.py" in out

    def test_a_healthy_projection_prints_a_clean_row(self, monkeypatch, capsys):
        self._arrange(monkeypatch, [("claude", [], True)])
        cli_doctor._doctor_unresolved_mcp_refs()
        out = capsys.readouterr().out
        assert out.strip() == "mcp tool refs: \u2705 claude \u2014 every @server ref resolves"
        # The whole row, so a clean backend cannot also print a remedy paragraph.
        assert "no mirror" not in out and "still misses" not in out

    def test_kiro_is_labelled_by_its_policy_id_not_the_empty_string(self, monkeypatch, capsys):
        """The kiro backend is spelled ``""``, which would print as a blank row.

        Same translation ``_doctor_agent_auth`` applies: the policy id is the
        readable name for the one backend whose identifier is empty.
        """
        self._arrange(monkeypatch, [("", [], True)])
        cli_doctor._doctor_unresolved_mcp_refs()
        assert "\u2705 kiro" in capsys.readouterr().out

    def test_a_mirrored_backend_that_still_drops_a_ref_is_the_louder_row(self, monkeypatch, capsys):
        # A mirror exists and its projection lost the server anyway, which is a
        # different problem from having no projection at all.
        self._arrange(monkeypatch, [("claude", ["@marked"], True)])
        cli_doctor._doctor_unresolved_mcp_refs()
        out = capsys.readouterr().out
        assert "\u26a0 claude" in out
        assert "@marked" in out
        assert "has no mirror" not in out

    def test_no_spec_on_disk_is_informational(self, monkeypatch, capsys):
        self._arrange(monkeypatch, [], spec_found=False)
        cli_doctor._doctor_unresolved_mcp_refs()
        assert "no default agent spec" in capsys.readouterr().out

    def test_a_ref_carrying_terminal_controls_is_rendered_inert(self, monkeypatch, capsys):
        """Spec-derived text printed to a terminal goes through ``_safe_display``.

        A cloned repository ships its own ``<project>/.kiro/agents/*.json`` and an
        installed app registers a user-level spec, so a ref can carry OSC/ANSI
        sequences that spoof the diagnostic lines around it.
        """
        self._arrange(monkeypatch, [("codex", ["@srv\x1b]0;pwned\x07"], False)])
        cli_doctor._doctor_unresolved_mcp_refs()
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "srv" in out

    def test_the_row_never_moves_doctors_exit_code(self):
        """It takes no ``issues`` list, so it structurally cannot append one.

        Same rule as ``_doctor_strict_identity``: making every stock host red for a
        backend nobody selected is how a useful note becomes one people disable.
        """
        import inspect

        assert list(inspect.signature(cli_doctor._doctor_unresolved_mcp_refs).parameters) == []

    def test_the_row_is_reached_from_the_report_itself(self):
        """The check runs, rather than merely existing for its own tests to call.

        Every other test here invokes it directly, so all of them stay green on a
        build where nothing in ``_doctor`` calls it at all -- which is the same
        shape of omission the detector exists to catch, one layer up.
        """
        import inspect

        assert "_doctor_unresolved_mcp_refs()" in inspect.getsource(cli_doctor._doctor)

    def test_the_sdk_probe_models_an_owned_permission_surface(self, monkeypatch):
        """The delegation must pass ``permission_surface_owned=True``.

        The claude mirror withholds its WHOLE array when Crew did not author the
        session's native permission file — a per-session fact no static check can
        know. Passing False would make doctor report every ref as unresolved on the
        one backend whose projection actually works, which is the false positive
        that would get this row disabled.
        """
        from kiro_crew import acp_backends, providers
        from kiro_crew.acp import session_mcp
        from kiro_crew.agent_sdk.drivers import acp as acp_driver

        seen: dict = {}

        class _Mirror:
            def session_params(self, _agent, **kw):
                seen.update(kw)
                return {"mcpServers": [{"name": "kirocrew-core"}]}

        monkeypatch.setattr(
            session_mcp,
            "agent_spec_snapshot",
            lambda _a: {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": {}}},
        )
        monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: ["claude"])
        # The PACKAGE module: the driver imports both names from ``providers.mirrors``.
        monkeypatch.setattr(providers.mirrors, "mirror_for", lambda _b: _Mirror())

        found, rows = acp_driver.agent_spec_mcp_refs("kirocrew")

        assert found is True
        assert seen.get("permission_surface_owned") is True
        assert rows == [("claude", [], True)]

    def test_an_unreadable_registry_does_not_break_triage(self, monkeypatch, capsys):
        from kiro_crew.agent_sdk.drivers import acp as acp_driver

        def _boom(*_a, **_k):
            raise RuntimeError("spec unreadable")

        monkeypatch.setattr(acp_driver, "agent_spec_mcp_refs", _boom)
        cli_doctor._doctor_unresolved_mcp_refs()  # must not raise
        assert capsys.readouterr().out == ""


class TestBackendAbilityCardRows:
    """The MCP ability of the harness IN USE, and which others cost a whole server.

    The class above answers for the configured harness only when something about it is
    wrong. This is the other question -- what is my harness doing to my agent file, and
    what would switching cost me -- and it is in a terminal report because the users most
    likely to meet a projection gap are the ones already diagnosing one.

    **Two lines on a stock run.** The full per-harness comparison belongs to the
    dashboard, which has the room and the labels in thirteen languages; a row apiece for
    six harnesses in a terminal report is a section readers learn to skip. What this
    section carries is the in-use harness's own card and the one cross-harness fact a
    chooser cannot act without: where a tool-off can withhold Crew's own servers.

    It also carries what the CARD does not. The projection kind is a route Crew takes,
    which costs a reader choosing a harness nothing, so the dashboard dropped it and this
    report is where it is stated.

    Every assertion reads the SHIPPED declarations. A row rendered from a stub would
    prove the formatting and not the wiring, and a declaration nothing renders is the
    state the issue reported.
    """

    def _cfg(self, backend: str):
        return SimpleNamespace(agent=SimpleNamespace(acp_backend=backend))

    def test_the_section_reports_the_install_and_the_cross_harness_cost(self, capsys):
        """What the section claims to answer, and nothing wider."""
        from kiro_crew.providers.mirrors import PROJECTIONS, PerToolDeny

        cli_doctor._doctor_backend_ability_cards(self._cfg("claude"))
        out = capsys.readouterr().out
        assert "mcp ability:" in out
        assert cli_doctor._backend_policy_label("claude") in out
        for backend, declared in PROJECTIONS.items():
            if declared.per_tool_deny is PerToolDeny.WHOLE_SERVER:
                assert cli_doctor._backend_policy_label(backend) in out, backend

    def test_only_the_harness_in_use_gets_a_row(self, capsys):
        """One card, for the install being diagnosed, and none for the rest."""
        from kiro_crew.acp_backends import selectable_backend_values

        cli_doctor._doctor_backend_ability_cards(self._cfg("claude"))
        out = capsys.readouterr().out
        assert f"    {cli_doctor._backend_policy_label('claude')} (in use): " in out
        for backend in selectable_backend_values():
            if backend == "claude":
                continue
            label = cli_doctor._backend_policy_label(backend)
            assert f"    {label} (in use): " not in out, label
            assert f"    {label}: " not in out, label

    def test_the_route_the_card_dropped_is_stated_here(self, capsys):
        """The projection kind lives in this report, because the card does not carry it.

        ``native`` / ``mirror`` / ``external`` costs a reader choosing a harness nothing:
        no feature changes, no risk appears, no setting of theirs stops working. It is
        exactly what a reader DIAGNOSING a session wants, so it prints here, as the
        declaration's own value.
        """
        from kiro_crew.providers.mirrors import PROJECTIONS

        for backend, declared in PROJECTIONS.items():
            if backend not in set(
                __import__("kiro_crew.acp_backends", fromlist=["x"]).selectable_backend_values()
            ):
                continue
            cli_doctor._doctor_backend_ability_cards(self._cfg(backend))
            out = capsys.readouterr().out
            assert f"projection: '{declared.kind.value}'" in out, backend

    def test_the_in_use_row_states_the_declared_reach(self, capsys):
        """The line the maintainer's ruling turned into a row."""
        from kiro_crew.providers.mirrors import PROJECTIONS, PerToolDeny

        declared = sorted(
            backend
            for backend, projection in PROJECTIONS.items()
            if projection.per_tool_deny is PerToolDeny.WHOLE_SERVER
        )
        assert declared, "no harness declares the whole-server reach any more"
        for backend in declared:
            cli_doctor._doctor_backend_ability_cards(self._cfg(backend))
            out = capsys.readouterr().out
            label = cli_doctor._backend_policy_label(backend)
            row = next(
                line for line in out.splitlines() if line.strip().startswith(f"{label} (in use):")
            )
            assert "per-tool deny: 'whole-server'" in row, backend

    def test_the_whole_server_reach_says_what_it_costs_once(self, capsys):
        """The consequence a reader cannot recover from the declared value alone."""
        import re as _re

        from kiro_crew.providers.mirrors import PROJECTIONS, PerToolDeny

        declared = sorted(
            b for b, p in PROJECTIONS.items() if p.per_tool_deny is PerToolDeny.WHOLE_SERVER
        )
        assert declared, "no harness declares the whole-server reach any more"
        cli_doctor._doctor_backend_ability_cards(self._cfg("claude"))
        out = capsys.readouterr().out
        flat = " ".join(out.split())
        assert "where that server is kirocrew-core" in flat
        assert flat.count("kirocrew-core") == 1, "one sentence, not one per harness"
        named = _re.search(r"On (.+?), switching a single MCP tool off", flat)
        assert named, flat
        for backend in declared:
            assert cli_doctor._backend_policy_label(backend) in named.group(1), backend

    def test_a_harness_whose_tool_off_costs_one_tool_is_not_named_in_the_caveat(self, capsys):
        """The sentence holds for the reach it describes, and for no other."""
        import re as _re

        from kiro_crew.providers.mirrors import PROJECTIONS, PerToolDeny

        spared = sorted(
            b
            for b, p in PROJECTIONS.items()
            if p.per_tool_deny in (PerToolDeny.SETTINGS_FILE, PerToolDeny.PER_CALL)
        )
        assert spared, "no harness keeps a tool-off per tool any more"
        cli_doctor._doctor_backend_ability_cards(self._cfg("claude"))
        out = capsys.readouterr().out
        named = _re.search(r"On (.+?), switching a single MCP tool off", " ".join(out.split()))
        assert named, out
        for backend in spared:
            assert cli_doctor._backend_policy_label(backend) not in named.group(1), backend

    def test_a_withhold_is_named_by_the_key_the_spec_spells(self, capsys):
        """The reader is holding the agent file, so the row names its own keys."""
        cli_doctor._doctor_backend_ability_cards(self._cfg("opencode"))
        out = capsys.readouterr().out
        assert "not sent from your agent file:" in out
        assert "permissions.defaultMode" in out
        # Crew's turn loop runs opencode's spec hooks, so hooks is no gap here.
        assert "no channel yet: hooks" not in out

    def test_a_backend_that_still_drops_hooks_names_the_gap(self, capsys):
        cli_doctor._doctor_backend_ability_cards(self._cfg("claude"))
        assert "no channel yet: hooks" in capsys.readouterr().out

    def test_a_harness_in_use_that_loses_nothing_still_gets_its_row(self, capsys):
        """Silence is wrong for the harness in USE, however good its answer is."""
        cli_doctor._doctor_backend_ability_cards(self._cfg(""))
        out = capsys.readouterr().out
        assert "(in use): projection: 'native'" in out

    def test_the_row_never_moves_doctors_exit_code(self):
        """It takes no ``issues`` list, so it structurally cannot append one."""
        import inspect

        params = list(inspect.signature(cli_doctor._doctor_backend_ability_cards).parameters)
        assert params == ["cfg"]

    def test_the_rows_are_reached_from_the_report_itself(self):
        """The section runs, rather than existing for its own tests to call."""
        import inspect

        assert "_doctor_backend_ability_cards(cfg)" in inspect.getsource(cli_doctor._doctor)

    def test_an_unreadable_config_does_not_break_triage(self, capsys):
        """No harness in use, no report: the section is advisory either way."""

        class _Boom:
            @property
            def agent(self):
                raise RuntimeError("config unreadable")

        cli_doctor._doctor_backend_ability_cards(_Boom())  # must not raise
        assert capsys.readouterr().out == ""

    def test_an_unreadable_registry_does_not_break_triage(self, monkeypatch, capsys):
        """Advisory rows, so a broken tree is reported by something else."""
        from kiro_crew.agent_sdk import backend_mcp_ability

        def _boom(*_a, **_k):
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(backend_mcp_ability, "ability_for", _boom)
        cli_doctor._doctor_backend_ability_cards(self._cfg("claude"))  # must not raise
        assert capsys.readouterr().out == ""

    def test_a_kind_this_build_has_no_phrase_for_prints_the_raw_value(self, monkeypatch, capsys):
        """Honest rather than silent, and scrubbed on the way out."""
        from kiro_crew.agent_sdk import backend_mcp_ability
        from kiro_crew.agent_sdk.backend_mcp_ability import McpAbility

        monkeypatch.setattr(
            backend_mcp_ability,
            "ability_for",
            lambda _b: McpAbility(
                projection="teleported\x1b]0;pwned\x07",
                per_tool_deny="",
                withheld=(),
                no_channel=(),
            ),
        )
        cli_doctor._doctor_backend_ability_cards(self._cfg("claude"))
        out = capsys.readouterr().out
        assert "teleported" in out
        assert "\x1b" not in out


class TestSelectedBackendProjectionRow:
    """The row for a harness whose transport carries none of Crew's own tools.

    Distinct from the unresolved-refs row above, and the difference is what the
    row is for: refs are read off the default spec, so a spec that references no
    server produces no row at all -- while a ``no-channel`` harness has no
    transport for Crew's servers whatever any spec says. That is a property of the
    harness, and the operator who selected it gets told once.

    The per-tool deny reach is NOT this row's subject:
    ``TestBackendAbilityCardRows`` owns it -- in the in-use harness's own row, and in
    one sentence naming every harness the costly reach holds for. One declaration with
    two readings in one report is how the two drift apart, so
    ``test_the_deny_reach_is_stated_once_in_the_report`` holds that boundary.
    """

    def _cfg(self, backend: str):
        return SimpleNamespace(agent=SimpleNamespace(acp_backend=backend))

    def test_a_no_channel_backend_gets_a_row_with_its_channel(self, monkeypatch, capsys):
        """Driven through a stubbed declaration, because no backend ships one now.

        This read the SHIPPED opencode entry, which was the one ``no-channel``
        declaration -- on the reading that its ``initialize`` advertising
        ``mcpCapabilities`` of http and sse and no stdio meant the array could not
        carry Crew's stdio servers. Measured against a real ``opencode acp``, it
        carries them, so opencode became a mirror and the shipped tables hold no
        ``no-channel`` entry at all. The ROW still has to render for the next
        harness that legitimately has no channel, so the declaration is supplied
        here rather than the test being deleted with the backend it happened to be
        demonstrated on.
        """
        from kiro_crew.agent_sdk import backend_mcp_ability
        from kiro_crew.agent_sdk.backend_mcp_ability import McpAbility

        monkeypatch.setattr(
            backend_mcp_ability,
            "ability_for",
            lambda _b: McpAbility(
                projection="no-channel",
                # A no-channel backend has no mirror, so it declares no reach.
                per_tool_deny="",
                withheld=(),
                no_channel=(),
                channel="an http or sse MCP endpoint the shared gateway serves",
                tracking="docs/request-for-change/rfc-agent-config-mirror.md#5-migration",
            ),
        )
        cli_doctor._doctor_selected_backend_projection(self._cfg("some-harness"))
        out = capsys.readouterr().out
        assert "some-harness" in out
        assert "none of Kiro Crew's own tools" in out
        assert "Would need:" in out
        assert "Tracked at:" in out

    def test_the_deny_reach_is_stated_once_in_the_report(self, capsys):
        """The consequence of a whole-server reach is this report's, not this ROW's.

        It was stated twice: once per selectable harness by
        ``_doctor_backend_ability_cards`` and again, in different words, for the
        selected harness alone. Two readings of one declared field is how prose and
        record drift, so the selected-harness row states the kind's own gap and
        nothing about the reach.

        Driven on the shipped goose declaration, which carries `whole-server`, so
        the assertion is about the report rather than about a stub.
        """
        cli_doctor._doctor_selected_backend_projection(self._cfg("goose"))
        assert capsys.readouterr().out == ""

        cli_doctor._doctor_backend_ability_cards(self._cfg("goose"))
        out = capsys.readouterr().out
        assert out.count("per-tool deny: 'whole-server'") == 1, out

    def test_the_shipped_tables_hold_exactly_the_declared_no_channel_backends(self):
        """The row's own subject, read off the SHIPPED tables rather than a stub.

        A ``no-channel`` entry here is not a failure of the row -- it must simply be
        addressable, which ``McpProjection`` enforces and ``test_provider_mirrors``
        asserts -- but it IS the state an operator gets told about, so the set is
        pinned by name and a new one is a deliberate change to this assertion.

        No backend ships as one: pi, whose adapter drops the ``session/new`` array,
        receives Crew's servers through its gate extension instead and is declared
        ``broker-only`` like deepseek. The case that follows pins that pi's row is
        therefore silent.
        """
        from kiro_crew.providers.mirrors import PROJECTIONS, ProjectionKind

        gaps = {
            backend
            for backend, declared in PROJECTIONS.items()
            if declared.kind is ProjectionKind.NO_CHANNEL
        }
        assert gaps == set(), f"no-channel backends shipping: {sorted(gaps)}"

    def test_the_real_pi_declaration_prints_no_channel_row(self, capsys):
        """Read off the SHIPPED declaration: pi's servers arrive, so nothing is said."""
        cli_doctor._doctor_selected_backend_projection(self._cfg("pi"))
        assert "carries none of Kiro Crew's own tools" not in capsys.readouterr().out

    def test_a_backend_that_does_receive_its_servers_prints_nothing(self, capsys):
        """Silence is the whole point on a stock install.

        kiro-cli reads the spec itself, so a row there would be a permanent note
        about a state that is correct -- and a report that talks on every install
        is one people stop reading.
        """
        cli_doctor._doctor_selected_backend_projection(self._cfg(""))
        assert capsys.readouterr().out == ""

    def test_an_undeclared_backend_is_silent_rather_than_wrong(self, capsys):
        """The parity test refuses this state; the report must not guess at it."""
        cli_doctor._doctor_selected_backend_projection(self._cfg("a-backend-nobody-declared"))
        assert capsys.readouterr().out == ""

    def test_a_declaration_carrying_terminal_controls_is_rendered_inert(self, monkeypatch, capsys):
        """An edition plugin authors its own declaration, so the text is scrubbed.

        Same rule as the refs row: anything a party other than this file wrote
        goes through ``_safe_display`` before reaching a terminal.
        """
        from kiro_crew.agent_sdk import backend_mcp_ability
        from kiro_crew.agent_sdk.backend_mcp_ability import McpAbility

        monkeypatch.setattr(
            backend_mcp_ability,
            "ability_for",
            lambda _b: McpAbility(
                projection="no-channel",
                per_tool_deny="",
                withheld=(),
                no_channel=(),
                channel="http\x1b]0;pwned\x07",
                tracking="tracked",
            ),
        )
        cli_doctor._doctor_selected_backend_projection(self._cfg("some-harness"))
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "http" in out

    def test_the_row_never_moves_doctors_exit_code(self):
        """It takes no ``issues`` list, so it structurally cannot append one.

        Choosing a harness whose transport cannot carry Crew's tools is a
        supported configuration with a declared reason, so it must not make
        ``kirocrew doctor`` exit 1.
        """
        import inspect

        params = list(inspect.signature(cli_doctor._doctor_selected_backend_projection).parameters)
        assert params == ["cfg"]

    def test_the_row_is_reached_from_the_report_itself(self):
        """The check runs, rather than merely existing for its own tests to call."""
        import inspect

        assert "_doctor_selected_backend_projection(cfg)" in inspect.getsource(cli_doctor._doctor)

    def test_an_unreadable_config_does_not_break_triage(self, capsys):
        class _Boom:
            @property
            def agent(self):
                raise RuntimeError("config unreadable")

        cli_doctor._doctor_selected_backend_projection(_Boom())  # must not raise
        assert capsys.readouterr().out == ""


class TestSwapTotalProbe:
    """``SwapTotal`` parsed from /proc/meminfo → KiB, or None when unreadable."""

    def _meminfo(self, monkeypatch, tmp_path: Path, content: str) -> None:
        path = tmp_path / "meminfo"
        path.write_text(content, encoding="ascii")
        monkeypatch.setattr(cli_doctor, "_PROC_MEMINFO", path)

    def test_swap_present(self, monkeypatch, tmp_path: Path) -> None:
        self._meminfo(
            monkeypatch, tmp_path, "MemTotal:       63901234 kB\nSwapTotal:       8388604 kB\n"
        )
        assert cli_doctor._swap_total_kib() == 8388604

    def test_swap_zero(self, monkeypatch, tmp_path: Path) -> None:
        self._meminfo(
            monkeypatch, tmp_path, "MemTotal:       63901234 kB\nSwapTotal:             0 kB\n"
        )
        assert cli_doctor._swap_total_kib() == 0

    def test_missing_file_is_none(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(cli_doctor, "_PROC_MEMINFO", tmp_path / "absent")
        assert cli_doctor._swap_total_kib() is None

    def test_missing_line_is_none(self, monkeypatch, tmp_path: Path) -> None:
        self._meminfo(monkeypatch, tmp_path, "MemTotal:       63901234 kB\n")
        assert cli_doctor._swap_total_kib() is None

    def test_malformed_value_is_none(self, monkeypatch, tmp_path: Path) -> None:
        self._meminfo(monkeypatch, tmp_path, "SwapTotal: banana kB\n")
        assert cli_doctor._swap_total_kib() is None


class TestOomKillerProbe:
    """``systemctl is-active <unit>`` → unit name / False / None (unknown)."""

    def _probe(self, monkeypatch, active: set[str] | None, *, raises: bool = False):
        import subprocess

        monkeypatch.setattr(
            cli_doctor.platform_compat,
            "trusted_system_bin",
            lambda _n: "/usr/bin/systemctl",
        )

        def fake_run(cmd, **_k):
            if raises:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)
            unit = cmd[-1]
            if active is not None and unit in active:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="active\n")
            return subprocess.CompletedProcess(args=cmd, returncode=3, stdout="inactive\n")

        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
        return cli_doctor._detect_userspace_oom_killer()

    def test_systemd_oomd_active(self, monkeypatch) -> None:
        assert self._probe(monkeypatch, {"systemd-oomd"}) == "systemd-oomd"

    def test_earlyoom_active(self, monkeypatch) -> None:
        assert self._probe(monkeypatch, {"earlyoom"}) == "earlyoom"

    def test_none_active_is_false(self, monkeypatch) -> None:
        assert self._probe(monkeypatch, set()) is False

    def test_probe_timeout_is_unknown(self, monkeypatch) -> None:
        # A hung/failed probe must degrade to "unknown", never propagate.
        assert self._probe(monkeypatch, None, raises=True) is None

    def test_absent_systemctl_is_unknown(self, monkeypatch) -> None:
        # Resolution goes through the trusted-bin pin (fixed system dirs), so a
        # PATH-planted shim can never be executed; a miss degrades to unknown.
        monkeypatch.setattr(cli_doctor.platform_compat, "trusted_system_bin", lambda _n: None)
        assert cli_doctor._detect_userspace_oom_killer() is None


class TestMemoryPressure:
    """`kirocrew doctor` Memory Pressure section — freeze-preparedness verdict.

    A Linux host with zero swap AND no userspace OOM killer livelocks under
    sustained memory pressure (file-backed page thrashing) before the kernel
    OOM killer fires. Doctor warns on exactly that quadrant, passes when either
    protection exists, reports "unknown" when detection is inconclusive, and
    never gates its exit code on any of it (host config is the user's call).
    """

    def _arrange(
        self, monkeypatch, *, swap_kib: int | None, killer: str | bool | None
    ) -> list[str]:
        monkeypatch.setattr(cli_doctor.sys, "platform", "linux")
        monkeypatch.setattr(cli_doctor, "_swap_total_kib", lambda: swap_kib)
        monkeypatch.setattr(cli_doctor, "_detect_userspace_oom_killer", lambda: killer)
        return ["pre-existing"]

    def test_no_swap_no_killer_warns_but_never_blocks(self, monkeypatch, capsys) -> None:
        # The dangerous quadrant: warn with the remediation, but stay advisory —
        # swap sizing and killer policy are host configuration the user owns.
        issues = self._arrange(monkeypatch, swap_kib=0, killer=False)

        cli_doctor._doctor_memory_pressure(issues)

        out = capsys.readouterr().out
        assert "freeze" in out and "⚠️" in out
        assert "add swap" in out and "systemd-oomd" in out and "earlyoom" in out
        assert issues == ["pre-existing"], "the warning must not add an issue"

    def test_swap_present_no_killer_passes(self, monkeypatch, capsys) -> None:
        issues = self._arrange(monkeypatch, swap_kib=8388604, killer=False)

        cli_doctor._doctor_memory_pressure(issues)

        out = capsys.readouterr().out
        assert "swap:        ✅" in out
        assert "⚠️" not in out
        assert issues == ["pre-existing"]

    def test_no_swap_killer_active_passes(self, monkeypatch, capsys) -> None:
        issues = self._arrange(monkeypatch, swap_kib=0, killer="earlyoom")

        cli_doctor._doctor_memory_pressure(issues)

        out = capsys.readouterr().out
        assert "oom killer:  ✅ earlyoom" in out
        assert "⚠️" not in out
        assert issues == ["pre-existing"]

    def test_both_protections_pass(self, monkeypatch, capsys) -> None:
        issues = self._arrange(monkeypatch, swap_kib=8388604, killer="systemd-oomd")

        cli_doctor._doctor_memory_pressure(issues)

        out = capsys.readouterr().out
        assert "swap:        ✅" in out and "oom killer:  ✅ systemd-oomd" in out
        assert "⚠️" not in out
        assert issues == ["pre-existing"]

    def test_no_swap_unknown_killer_is_informational_not_warning(self, monkeypatch, capsys) -> None:
        # Inconclusive detection (no systemctl / probe failure) must not warn —
        # a container or non-systemd host may run a killer doctor cannot see.
        issues = self._arrange(monkeypatch, swap_kib=0, killer=None)

        cli_doctor._doctor_memory_pressure(issues)

        out = capsys.readouterr().out
        assert "unknown" in out and "inconclusive" in out
        assert "⚠️" not in out
        assert issues == ["pre-existing"]

    def test_unreadable_meminfo_skips_quietly(self, monkeypatch, capsys) -> None:
        issues = self._arrange(monkeypatch, swap_kib=None, killer=False)

        cli_doctor._doctor_memory_pressure(issues)

        out = capsys.readouterr().out
        assert "check skipped" in out
        assert "freeze risk: ⚠️" not in out
        assert issues == ["pre-existing"]

    def test_non_linux_is_not_applicable(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli_doctor.sys, "platform", "darwin")
        issues: list[str] = []

        cli_doctor._doctor_memory_pressure(issues)

        out = capsys.readouterr().out
        assert "not applicable" in out
        assert issues == []

    def test_ceiling_and_rss_print_on_every_platform(self, monkeypatch, capsys) -> None:
        """The Linux-only freeze check must not hide the two readings a Windows
        or macOS operator asking "what bounds a runaway tree?" needs."""
        monkeypatch.setattr(cli_doctor.sys, "platform", "win32")
        monkeypatch.setattr(
            cli_doctor,
            "_gateway_memory_lines",
            lambda: ["  session ceiling: ✅ 1536 MiB", "  gateway rss:     412 MiB (pid 4242)"],
        )
        issues: list[str] = []

        cli_doctor._doctor_memory_pressure(issues)

        out = capsys.readouterr().out
        assert out.index("session ceiling") < out.index("gateway rss") < out.index("not applicable")
        assert issues == []


class TestRuntimeTmpfs:
    """`kirocrew doctor` Runtime tmpfs section — early warning before the
    sandbox's mount-source roots run out of space or inodes and every tool
    spawn degrades to a bare ``rc=1``."""

    @staticmethod
    def _arrange(monkeypatch, usage_by_root: dict) -> list[str]:
        monkeypatch.setattr(cli_doctor.sys, "platform", "linux")
        monkeypatch.setattr(cli_doctor, "_runtime_tmpfs_roots", lambda: list(usage_by_root))
        monkeypatch.setattr(cli_doctor, "_tmpfs_usage", lambda root: usage_by_root[root])
        return ["pre-existing"]

    def test_healthy_roots_pass(self, monkeypatch, capsys) -> None:
        issues = self._arrange(
            monkeypatch,
            {"/run/user/1000": (80.0, 95.0, 50000, 3), "/dev/shm": (99.0, 99.0, 900000, 0)},
        )
        cli_doctor._doctor_runtime_tmpfs(issues)
        out = capsys.readouterr().out
        assert "Runtime tmpfs" in out
        assert "/run/user/1000: ✅" in out and "3 kirocrew_sb_* entries" in out
        assert "⚠️" not in out
        assert issues == ["pre-existing"]

    def test_low_inodes_warns_with_entry_count_and_fails_doctor(self, monkeypatch, capsys) -> None:
        # The observed incident: plenty of bytes, no inodes, thousands of leaked dirs.
        issues = self._arrange(monkeypatch, {"/run/user/1000": (97.0, 2.0, 400, 18231)})
        cli_doctor._doctor_runtime_tmpfs(issues)
        out = capsys.readouterr().out
        assert "/run/user/1000: ⚠️  low on inodes" in out
        assert "18231 kirocrew_sb_* entries" in out and "rc=1" in out
        assert len(issues) == 2 and "low on inodes" in issues[1]

    def test_inode_floor_warns_even_above_ten_percent(self, monkeypatch, capsys) -> None:
        # A tiny tmpfs at 12% free inodes can be a few hundred dirs from failure.
        issues = self._arrange(monkeypatch, {"/run/user/1000": (90.0, 12.0, 600, 5)})
        cli_doctor._doctor_runtime_tmpfs(issues)
        assert "low on inodes" in capsys.readouterr().out
        assert len(issues) == 2

    def test_low_space_warns(self, monkeypatch, capsys) -> None:
        issues = self._arrange(monkeypatch, {"/dev/shm": (4.0, 90.0, 90000, 0)})
        cli_doctor._doctor_runtime_tmpfs(issues)
        assert "low on space" in capsys.readouterr().out
        assert len(issues) == 2

    def test_both_low_names_both(self, monkeypatch, capsys) -> None:
        issues = self._arrange(monkeypatch, {"/dev/shm": (1.0, 1.0, 10, 0)})
        cli_doctor._doctor_runtime_tmpfs(issues)
        assert "low on space and inodes" in capsys.readouterr().out
        assert len(issues) == 2

    def test_missing_root_is_skipped(self, monkeypatch, capsys) -> None:
        issues = self._arrange(monkeypatch, {"/run/user/1000": None})
        cli_doctor._doctor_runtime_tmpfs(issues)
        assert "not present or unreadable" in capsys.readouterr().out
        assert issues == ["pre-existing"]

    def test_non_linux_is_a_silent_noop(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli_doctor.sys, "platform", "win32")
        called: list[str] = []
        monkeypatch.setattr(cli_doctor, "_runtime_tmpfs_roots", lambda: called.append("x") or [])
        issues: list[str] = []
        cli_doctor._doctor_runtime_tmpfs(issues)
        assert capsys.readouterr().out == ""
        assert called == [] and issues == []

    def test_roots_come_from_the_sandbox_chooser(self, monkeypatch) -> None:
        from kiro_crew import sandbox

        monkeypatch.setattr(
            sandbox, "_mount_source_candidate_roots", lambda: ["/run/user/7", "/dev/shm", "/tmp"]
        )
        assert cli_doctor._runtime_tmpfs_roots() == ["/run/user/7", "/dev/shm", "/tmp"]

    def test_usage_reads_statvfs_and_counts_sandbox_entries(self, monkeypatch, tmp_path) -> None:
        # Only the sandbox launcher's own mount-source dirs count; a foreign
        # ``tmp*`` entry is somebody else's and must not inflate the advice.
        for name in ("kirocrew_sb_41_a", "kirocrew_sb_42_b", "tmpabc", "other"):
            (tmp_path / name).mkdir()
        # A plain namespace rather than ``os.statvfs_result``: neither that type
        # nor ``os.statvfs`` exists on Windows, and the production code only
        # reads the four fields below.
        fake = SimpleNamespace(f_blocks=1000, f_bavail=50, f_files=2000, f_favail=200)
        monkeypatch.setattr(cli_doctor.os, "statvfs", lambda root: fake, raising=False)
        free_space_pct, free_inode_pct, free_inodes, tmp_entries = cli_doctor._tmpfs_usage(
            str(tmp_path)
        )
        assert free_space_pct == 5.0  # f_bavail / f_blocks
        assert free_inode_pct == 10.0  # f_favail / f_files
        assert free_inodes == 200
        assert tmp_entries == 2

    def test_usage_without_inode_accounting_is_not_low(self, monkeypatch, tmp_path) -> None:
        # btrfs-style statvfs: f_files == 0 means the filesystem does not
        # count inodes, so it must read as unconstrained on both axes and never
        # trip the absolute floor.
        fake = SimpleNamespace(f_blocks=1000, f_bavail=900, f_files=0, f_favail=0)
        monkeypatch.setattr(cli_doctor.os, "statvfs", lambda root: fake, raising=False)
        usage = cli_doctor._tmpfs_usage(str(tmp_path))
        assert usage is not None
        _, free_inode_pct, free_inodes, _ = usage
        assert free_inode_pct == 100.0
        assert free_inodes >= cli_doctor._TMPFS_FREE_INODES_FLOOR

    def test_usage_unreadable_root_is_none(self, monkeypatch) -> None:
        def _boom(root):
            raise FileNotFoundError(root)

        monkeypatch.setattr(cli_doctor.os, "statvfs", _boom, raising=False)
        assert cli_doctor._tmpfs_usage("/nope") is None

    def test_usage_is_none_without_statvfs(self, monkeypatch, tmp_path) -> None:
        # The Windows shape: ``os`` has no ``statvfs`` at all.
        monkeypatch.delattr(cli_doctor.os, "statvfs", raising=False)
        assert cli_doctor._tmpfs_usage(str(tmp_path)) is None


class TestGatewayMemoryLines:
    """`_gateway_memory_lines`: the configured ceiling plus the live gateway's RSS."""

    @staticmethod
    def _cfg(monkeypatch, ceiling: int) -> None:
        cfg = MagicMock()
        cfg.session.watchdog_rss_max_mb = ceiling
        monkeypatch.setattr(cli_doctor.KiroCrewConfig, "load", lambda: cfg)

    def test_reports_ceiling_and_live_rss(self, monkeypatch) -> None:
        self._cfg(monkeypatch, 1536)
        monkeypatch.setattr(cli_doctor, "_read_gateway_pid", lambda: 4242)
        monkeypatch.setattr(
            cli_doctor.platform_compat,
            "proc_rss_bytes_for_pid",
            lambda pid: 412 * 1024 * 1024 if pid == 4242 else None,
        )
        ceiling, rss = cli_doctor._gateway_memory_lines()
        assert "1536 MiB" in ceiling and "watchdog_rss_max_mb" in ceiling
        assert "412 MiB" in rss and "4242" in rss

    def test_disabled_ceiling_is_called_out(self, monkeypatch) -> None:
        from kiro_crew.doctor_checks import resources

        self._cfg(monkeypatch, 0)
        monkeypatch.setattr(cli_doctor, "_read_gateway_pid", lambda: None)
        monkeypatch.setattr(resources, "_gateway_lock_indeterminate", lambda: False)
        ceiling, rss = cli_doctor._gateway_memory_lines()
        assert "disabled" in ceiling and "nothing bounds" in ceiling
        assert "not running" in rss

    def test_indeterminate_lock_probe_is_not_reported_as_not_running(self, monkeypatch) -> None:
        """A serving Windows gateway's lock reads as indeterminate, not absent.

        The same run's Connectivity row says the gateway is running; this row
        must say the probe could not locate it rather than contradict that.
        """
        from kiro_crew import gateway_lock

        self._cfg(monkeypatch, 1536)
        monkeypatch.setattr(cli_doctor, "_read_gateway_pid", lambda: None)

        def _indeterminate(home):
            raise gateway_lock.LockProbeError(home / "gateway.lock", OSError("locked"))

        monkeypatch.setattr(gateway_lock, "lock_holder", _indeterminate)
        _ceiling, rss = cli_doctor._gateway_memory_lines()
        assert "not running" not in rss
        assert "could not locate the gateway process" in rss
        assert "does not mean it is stopped" in rss

    def test_free_lock_still_reads_as_not_running(self, monkeypatch) -> None:
        from kiro_crew import gateway_lock

        self._cfg(monkeypatch, 1536)
        monkeypatch.setattr(cli_doctor, "_read_gateway_pid", lambda: None)
        monkeypatch.setattr(
            gateway_lock,
            "lock_holder",
            lambda home: gateway_lock.LockHolder(pid=None, alive=False, source="none"),
        )
        _ceiling, rss = cli_doctor._gateway_memory_lines()
        assert "⏹ not running" in rss

    def test_unreadable_rss_and_config_never_raise(self, monkeypatch) -> None:
        monkeypatch.setattr(cli_doctor.KiroCrewConfig, "load", MagicMock(side_effect=OSError))
        monkeypatch.setattr(cli_doctor, "_read_gateway_pid", lambda: 7)
        monkeypatch.setattr(cli_doctor.platform_compat, "proc_rss_bytes_for_pid", lambda pid: None)
        monkeypatch.setattr(cli_doctor.platform_compat, "trusted_system_bin", lambda name: None)
        ceiling, rss = cli_doctor._gateway_memory_lines()
        assert "could not read" in ceiling
        assert "unreadable" in rss and "7" in rss

    def test_falls_back_to_trusted_ps_when_the_shim_has_no_route(self, monkeypatch) -> None:
        """macOS: the shim answers None, so the doctor reads ``ps -o rss=`` (KiB)."""
        self._cfg(monkeypatch, 1536)
        monkeypatch.setattr(cli_doctor, "_read_gateway_pid", lambda: 4242)
        monkeypatch.setattr(cli_doctor.platform_compat, "proc_rss_bytes_for_pid", lambda pid: None)
        monkeypatch.setattr(cli_doctor.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda name: "/bin/ps"
        )
        calls: list[list[str]] = []

        def _ps(argv, timeout):
            calls.append(argv)
            return b" 421888\n"

        monkeypatch.setattr(cli_doctor.subprocess, "check_output", _ps)
        _ceiling, rss = cli_doctor._gateway_memory_lines()
        assert "412 MiB" in rss and "4242" in rss
        assert calls == [["/bin/ps", "-o", "rss=", "-p", "4242"]]
