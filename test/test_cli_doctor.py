"""Tests for the `kirocrew doctor` OS-aware fix hints.

Guards _os_fix_hint: it returns the macOS Homebrew command on Darwin and the
Linux/AL2023 guidance otherwise, so `kirocrew doctor` never prints a brew
command on Linux where there is no brew.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import cli_doctor, cron


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
    def _linux(monkeypatch, tmp_path: Path, *, bus: bool) -> Path:
        monkeypatch.setattr(cli_doctor.sys, "platform", "linux")
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda n: f"/usr/bin/{n}")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setenv("USER", "tester")
        sock = tmp_path / "bus"
        if bus:
            sock.touch()
        return sock

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


class TestLingerProbe:
    """`loginctl show-user <u> -p Linger --value` → tri-state."""

    def _run(self, monkeypatch, *, stdout: str, returncode: int = 0):
        import subprocess

        monkeypatch.setattr(cli_doctor.shutil, "which", lambda _n: "/usr/bin/loginctl")
        monkeypatch.setattr(
            cli_doctor.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout=stdout, stderr=""
            ),
        )
        return cli_doctor._linger_enabled("tester")

    def test_yes_is_true(self, monkeypatch) -> None:
        assert self._run(monkeypatch, stdout="yes\n") is True

    def test_no_is_false(self, monkeypatch) -> None:
        assert self._run(monkeypatch, stdout="no\n") is False

    def test_unparseable_is_unknown(self, monkeypatch) -> None:
        assert self._run(monkeypatch, stdout="wat\n") is None

    def test_nonzero_exit_is_unknown(self, monkeypatch) -> None:
        assert self._run(monkeypatch, stdout="", returncode=1) is None

    def test_absent_loginctl_is_unknown(self, monkeypatch) -> None:
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda _n: None)
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
        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: None
        )
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

    def test_no_swap_unknown_killer_is_informational_not_warning(
        self, monkeypatch, capsys
    ) -> None:
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


class TestDoctorKas:
    """`kirocrew doctor` KAS backend section — gated on acp_backend == kas.

    KAS is served by kiro-cli's ACP relay, so the section reports the relay
    invocation and whether this kiro-cli can select the KAS engine. It probes no
    credential: the relay resolves tokens from kiro-cli's own store, which the
    sign-in check already covers.
    """

    class _Cfg:
        def __init__(self, backend: str) -> None:
            self.agent = type("A", (), {"acp_backend": backend})()

    def _patch_cfg(self, monkeypatch, backend: str) -> None:
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", classmethod(lambda cls: self._Cfg(backend))
        )

    def test_silent_when_backend_not_kas(self, monkeypatch, capsys) -> None:
        self._patch_cfg(monkeypatch, "")
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        assert "KAS backend" not in capsys.readouterr().out
        assert issues == []

    def test_selected_but_no_kiro_cli_appends_issue(self, monkeypatch, capsys) -> None:
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: None)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "KAS backend" in out
        assert "KAS backend selected but kiro-cli is not installed" in issues
        # No engine probe is attempted when there is no binary to probe.
        assert "engine:" not in out

    def test_engine_supported_prints_the_relay_argv(self, monkeypatch, capsys) -> None:
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        monkeypatch.setattr(
            cli_doctor,
            "_kas_relay_help",
            lambda _binary: "--agent-engine <ENGINE>  v1, v2 (default), or v3",
        )
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        # The exact invocation, so a reader can reproduce it by hand.
        assert "acp --agent-engine v3 --auth-method cli" in out
        assert "✅ v3 supported" in out
        assert issues == []

    def test_engine_missing_appends_issue(self, monkeypatch, capsys) -> None:
        """A kiro-cli that offers engines but not ours cannot serve KAS."""
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        monkeypatch.setattr(
            cli_doctor,
            "_kas_relay_help",
            lambda _binary: "--agent-engine <ENGINE>  v1, v2 (default)",
        )
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "does not offer engine v3" in out
        assert any("does not support the KAS engine" in i for i in issues)

    def test_help_without_the_flag_is_a_failure_not_unknown(
        self, monkeypatch, capsys
    ) -> None:
        """A kiro-cli predating engine selection must FAIL the check.

        Reporting it as "unknown" would let a configuration that cannot work
        pass readiness and fail later at session-create time instead.
        """
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        monkeypatch.setattr(
            cli_doctor,
            "_kas_relay_help",
            lambda _binary: "Usage: kiro-cli acp [OPTIONS]\n  -a, --trust-all-tools",
        )
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "no --agent-engine flag" in out
        assert "engine support unknown" not in out
        assert any("too old to select the KAS engine" in i for i in issues)

    def test_unreadable_help_is_reported_unknown_not_failed(
        self, monkeypatch, capsys
    ) -> None:
        """Only a FAILED probe is unknown; a diagnostic must not invent a verdict.

        ``None`` now means the subprocess did not run, which is the one case
        where nothing is established either way.
        """
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        monkeypatch.setattr(cli_doctor, "_kas_relay_help", lambda _binary: None)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "engine support unknown" in out
        assert issues == []

    def test_probe_returns_help_text_even_without_the_flag(
        self, monkeypatch
    ) -> None:
        """The probe must not swallow ran-but-lacks-the-flag into None.

        Pins the split directly: the previous implementation returned None for
        both a failed spawn and help text missing the selector, which is what
        let an unsupported kiro-cli pass.
        """

        class _Proc:
            stdout = "Usage: kiro-cli acp [OPTIONS]"
            stderr = ""

        monkeypatch.setattr(cli_doctor.subprocess, "run", lambda *a, **k: _Proc())
        got = cli_doctor._kas_relay_help("/x/kiro-cli")
        assert got is not None
        assert "--agent-engine" not in got

    def test_probe_returns_none_when_the_spawn_fails(self, monkeypatch) -> None:
        def _boom(*_a, **_k):
            raise OSError("no such binary")

        monkeypatch.setattr(cli_doctor.subprocess, "run", _boom)
        assert cli_doctor._kas_relay_help("/x/kiro-cli") is None

    def test_no_credential_probe_is_performed(self, monkeypatch, capsys) -> None:
        """The relay owns auth, so the doctor must not reach for a token.

        Pinned as an assertion because the previous implementation DID shell out
        for one, and re-adding that would put Crew back in the credential path.
        """
        self._patch_cfg(monkeypatch, "kas")
        monkeypatch.setattr(cli_doctor, "resolve_kiro_cli", lambda: "/x/kiro-cli")
        monkeypatch.setattr(cli_doctor, "_kas_relay_help", lambda _binary: "v3")
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "owned by kiro-cli" in out
        assert not hasattr(cli_doctor, "_kas_version_label")


class TestPathLauncherOwnership:
    """`kirocrew doctor` names which install owns the `kirocrew` command.

    A gateway deliberately never takes the name from another install's working
    launcher, so the two can diverge silently: the documented Linux pairing puts
    a cli.sh wheel and a deb/rpm desktop install on one machine, and the desktop
    app has no terminal to show the decline. This is where that is visible.
    """

    def test_matching_launcher_is_reported_clean(self, monkeypatch, tmp_path, capsys) -> None:
        exe = tmp_path / "opt" / "bin" / "kirocrew"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda c, **kw: str(exe))
        monkeypatch.setattr("kiro_crew.agent._resolve_kirocrew_bin", lambda: str(exe))

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "kirocrew CLI: ✅" in out
        assert "different install" not in out

    def test_divergent_launcher_names_both_paths(self, monkeypatch, tmp_path, capsys) -> None:
        wheel = tmp_path / "crew-venv" / "bin" / "kirocrew"
        wheel.parent.mkdir(parents=True)
        wheel.write_text("")
        package = tmp_path / "opt" / "KiroCrew" / "kirocrew"  # brand-ok: real /opt path
        package.parent.mkdir(parents=True)
        package.write_text("")
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda c, **kw: str(wheel))
        monkeypatch.setattr("kiro_crew.agent._resolve_kirocrew_bin", lambda: str(package))

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "⚠ kirocrew CLI on PATH belongs to a different install" in out
        # Both sides must be named, or the user cannot tell which is which.
        # Compare like with like: the check prints realpath, and on Windows a
        # realpath can differ in form (short vs long name, case) from str(path).
        assert os.path.realpath(wheel) in out and os.path.realpath(package) in out
        assert "kirocrew setup" in out

    def test_no_launcher_on_path_is_informational(self, monkeypatch, capsys) -> None:
        """The desktop app runs its bundled backend directly, so an absent
        terminal command is a state, not a fault."""
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda c, **kw: None)

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "⏹ not on PATH" in out
        assert "⚠" not in out

    def test_unresolvable_install_does_not_cry_wolf(self, monkeypatch, tmp_path, capsys) -> None:
        """A bare "kirocrew" sentinel is not a path, so there is nothing to
        compare and no divergence to claim."""
        found = tmp_path / "bin" / "kirocrew"
        found.parent.mkdir(parents=True)
        found.write_text("")
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda c, **kw: str(found))
        monkeypatch.setattr("kiro_crew.agent._resolve_kirocrew_bin", lambda: "kirocrew")

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "kirocrew CLI: ✅" in out


class TestSourceCheckout:
    """`kirocrew doctor` Source Checkout section — stale/off-branch source tree.

    Guards _doctor_source_checkout: an editable install parked on a stale
    feature branch runs old code (merged security fixes included) while every
    other doctor section reports healthy. These tests drive the probe through
    the _git_line seam — no real repository needed.
    """

    @staticmethod
    def _fake_git(answers: dict[tuple[str, ...], str | None]):
        def fake(repo, *args):
            return answers.get(tuple(args))

        return fake

    def _repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        return tmp_path

    def test_on_default_up_to_date_passes(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "main",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): "0",
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "✅ main (up to date" in out
        assert "⚠️" not in out

    def test_on_default_behind_warns_with_count(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "main",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): "42",
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "⚠️" in out
        assert "42 commit(s) behind" in out
        assert "update + restart" in out

    def test_feature_branch_behind_warns_with_fix(self, monkeypatch, tmp_path, capsys) -> None:
        # The incident shape: gateway source parked on a feature branch for
        # days, hundreds of commits behind — doctor must name the branch, the
        # distance, and the recovery path.
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "fix/some-feature",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): "798",
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "⚠️" in out
        assert "fix/some-feature" in out
        assert "798 commit(s) behind origin/main" in out
        assert "NOT active" in out
        assert "check out the default branch" in out

    def test_remediation_never_renders_ref_inside_a_command(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        """A hostile ref name must not become a pasteable command payload.

        Branch names come from the repository — agent-writable on this threat
        model — so a ref like ``$(touch${IFS}/tmp/pwn)`` rendered into a
        suggested ``git checkout ...`` line would execute when the operator
        pastes it. Remediation must stay prose: no line may combine a command
        word with the interpolated ref.
        """
        evil = "$(touch${IFS}/tmp/pwn)"
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): evil,
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): "3",
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        # The state is still reported (prose may name the ref) ...
        assert evil in out
        # ... but never on a line shaped like a runnable git command.
        for line in out.splitlines():
            if "git -C" in line or "git checkout" in line:
                raise AssertionError(f"pasteable command rendered: {line!r}")

    def test_on_default_failed_count_reports_could_not_check(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        # rev-list failing on the default branch must NOT masquerade as a
        # verified-fresh checkout — "up to date" is a claim the probe could
        # not establish.
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "main",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): None,
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "✅" not in out
        assert "could not count commits behind" in out

    def test_feature_branch_unknown_distance_still_warns(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        # rev-list failing (e.g. origin/main ref pruned) must not hide the
        # off-branch state itself.
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "fix/some-feature",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): None,
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "⚠️" in out
        assert "on 'fix/some-feature' — not the default branch" in out
        assert "behind" not in out.split("not the default branch")[1].splitlines()[0]

    def test_missing_origin_head_reports_branch_without_guessing(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        # No origin/HEAD → report what we know, never assume the default is
        # "main" (could mislabel a repo whose default genuinely differs).
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "develop",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): None,
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "develop" in out
        assert "could not determine default branch" in out
        assert "main" not in out

    def test_not_a_git_checkout_is_not_applicable(self, monkeypatch, tmp_path, capsys) -> None:
        # Tarball installs (cloud/EC2) have no .git — mirror the update
        # handler's guard and stay quiet rather than warning.
        cli_doctor._doctor_source_checkout(tmp_path)
        out = capsys.readouterr().out
        assert "⏹ not a git checkout" in out
        assert "⚠️" not in out

    def test_git_failure_reports_could_not_check(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr(cli_doctor, "_git_line", self._fake_git({}))
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "could not check" in out

    def test_git_line_returns_none_on_nonzero_exit(self, monkeypatch, tmp_path) -> None:
        import subprocess as _sp

        def fake_run(*a, **k):
            return _sp.CompletedProcess(a, 128, stdout="", stderr="fatal: not a repo")

        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: "/usr/bin/git"
        )
        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") is None

    def test_git_line_returns_first_line_stripped(self, monkeypatch, tmp_path) -> None:
        import subprocess as _sp

        def fake_run(*a, **k):
            return _sp.CompletedProcess(a, 0, stdout="  main  \nextra\n", stderr="")

        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: "/usr/bin/git"
        )
        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    def test_git_line_returns_none_on_oserror(self, monkeypatch, tmp_path) -> None:
        def fake_run(*a, **k):
            raise OSError("git not found")

        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: "/usr/bin/git"
        )
        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") is None

    def test_git_line_survives_non_utf8_output(self, monkeypatch, tmp_path) -> None:
        """A non-UTF-8 ref name must not crash doctor.

        ``text=True`` decodes strictly unless an ``errors=`` policy is given:
        a branch named with latin-1 bytes would raise ``UnicodeDecodeError``
        inside ``_git_line`` — which the OSError/SubprocessError handler does
        not catch — terminating the whole doctor run. The call passes
        ``errors="replace"`` so undecodable bytes degrade to U+FFFD instead.
        The fake below decodes with whatever policy the call supplies, so
        removing ``errors="replace"`` makes this test crash exactly as the
        real doctor would.
        """
        import subprocess as _sp

        raw = b"exp\xe9rimental\n"  # latin-1 e-acute: invalid as UTF-8

        def fake_run(argv, *a, **k):
            errors = k.get("errors")
            stdout = (
                raw.decode("utf-8", errors=errors)
                if errors
                else raw.decode("utf-8")
            )
            return _sp.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: "/usr/bin/git"
        )
        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
        line = cli_doctor._git_line(tmp_path, "rev-parse", "--abbrev-ref", "HEAD")
        assert line == "exp\ufffdrimental"

    def test_git_line_pins_git_and_returns_none_when_untrusted(
        self, monkeypatch, tmp_path
    ) -> None:
        """git resolves via trusted_git_bin; a miss means no subprocess at all.

        Doctor runs with operator privileges, so a ``git`` shim planted in an
        agent-writable PATH directory must never execute: when the trusted
        resolver declines, _git_line collapses to None without spawning. When it
        resolves, the pinned absolute path -- not the bare name -- reaches argv[0].

        The resolver itself (system dirs plus the Windows install-root fallback)
        is tested in `test_platform_compat`; this asserts what the doctor does
        with each OUTCOME, which is why it patches the resolver rather than the
        directories behind it.
        """
        import subprocess as _sp

        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _sp.CompletedProcess(argv, 0, stdout="main\n", stderr="")

        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)

        # Miss: no trusted git -> None, and no process spawned.
        monkeypatch.setattr(cli_doctor.platform_compat, "trusted_git_bin", lambda: None)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") is None
        assert calls == []

        # Hit: the resolved absolute path is argv[0], never the bare "git".
        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_git_bin", lambda: "/usr/bin/git"
        )
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") == "main"
        assert calls and calls[0][0] == "/usr/bin/git"


class TestCliInstallerResidue:
    """Detection of leftover kiro-cli auto-update installers in the temp dir.

    kiro-cli checks for updates on every process start, and Crew spawns a fresh
    kiro-cli per session. On Windows the running binary cannot be replaced, so
    each check leaves an installer behind that is never cleaned up (upstream
    kirodotdev/Kiro#10970). These guard the doctor surface that makes the
    resulting disk usage visible.
    """

    def _installer(self, directory: Path, name: str, size: int = 1024) -> Path:
        path = directory / name
        path.write_bytes(b"\0" * size)
        return path

    def test_scan_counts_matching_files_and_sums_bytes(self, tmp_path: Path) -> None:
        self._installer(tmp_path, "kiro-installer-2.14.0.msi", size=2048)
        self._installer(tmp_path, "kiro-installer-2.15.0.msi", size=1024)
        assert cli_doctor._scan_cli_installer_residue(tmp_path) == (2, 3072)

    def test_scan_ignores_unrelated_files(self, tmp_path: Path) -> None:
        # Must not sweep in every temp file that happens to mention kiro.
        self._installer(tmp_path, "kiro-installer-2.14.0.msi")
        self._installer(tmp_path, "kiro-log.txt")
        self._installer(tmp_path, "some-other-installer.msi")
        count, _ = cli_doctor._scan_cli_installer_residue(tmp_path)
        assert count == 1

    def test_scan_ignores_directories(self, tmp_path: Path) -> None:
        # A directory whose name matches must not be counted as a reclaimable
        # file, nor make stat() sizes meaningless.
        (tmp_path / "kiro-installer-dir").mkdir()
        assert cli_doctor._scan_cli_installer_residue(tmp_path) == (0, 0)

    def test_scan_is_non_recursive(self, tmp_path: Path) -> None:
        # The installer lands at the top level; descending would make the scan
        # unbounded over a shared temp dir.
        nested = tmp_path / "nested"
        nested.mkdir()
        self._installer(nested, "kiro-installer-2.14.0.msi")
        assert cli_doctor._scan_cli_installer_residue(tmp_path) == (0, 0)

    def test_scan_returns_zero_for_missing_dir(self, tmp_path: Path) -> None:
        # Note: glob() on a missing directory yields nothing rather than
        # raising, so this pins the missing-dir OUTCOME, not the OSError
        # handler — that branch is covered by the unreadable-dir test below.
        assert cli_doctor._scan_cli_installer_residue(tmp_path / "gone") == (0, 0)

    def test_scan_returns_zero_for_unreadable_dir(self, tmp_path: Path, monkeypatch) -> None:
        # A temp dir the process cannot list (permissions, or a racing rmtree)
        # must degrade to "nothing found" rather than crashing the doctor run.
        def boom(self: Path, _pattern: str):  # type: ignore[no-untyped-def]
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "glob", boom)
        assert cli_doctor._scan_cli_installer_residue(tmp_path) == (0, 0)

    def test_scan_skips_entry_that_races_a_delete(self, tmp_path: Path, monkeypatch) -> None:
        # The updater (or a cleanup script) can remove a file mid-scan; one
        # unreadable entry must not abort the diagnostic.
        self._installer(tmp_path, "kiro-installer-a.msi", size=512)
        self._installer(tmp_path, "kiro-installer-b.msi", size=512)
        real_stat = Path.stat

        def flaky_stat(self: Path, *a, **kw):  # type: ignore[no-untyped-def]
            if self.name == "kiro-installer-a.msi":
                raise OSError("vanished")
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", flaky_stat)
        assert cli_doctor._scan_cli_installer_residue(tmp_path) == (1, 512)

    def test_scan_stops_at_cap(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(cli_doctor, "_CLI_INSTALLER_SCAN_CAP", 3)
        for i in range(6):
            self._installer(tmp_path, f"kiro-installer-{i}.msi", size=10)
        count, _ = cli_doctor._scan_cli_installer_residue(tmp_path)
        assert count == 3

    def test_single_file_is_silent(self, tmp_path: Path, monkeypatch, capsys) -> None:
        # One file can be a download still in flight — not residue.
        self._installer(tmp_path, "kiro-installer-2.14.0.msi")
        monkeypatch.setattr(cli_doctor.tempfile, "gettempdir", lambda: str(tmp_path))
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        assert issues == []
        assert capsys.readouterr().out == ""

    def test_clean_host_is_silent(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli_doctor.tempfile, "gettempdir", lambda: str(tmp_path))
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        assert issues == []
        assert capsys.readouterr().out == ""

    def test_residue_is_reported_and_recorded(self, tmp_path: Path, monkeypatch, capsys) -> None:
        self._installer(tmp_path, "kiro-installer-2.14.0.msi", size=1048576)
        self._installer(tmp_path, "kiro-installer-2.15.0.msi", size=1048576)
        monkeypatch.setattr(cli_doctor.tempfile, "gettempdir", lambda: str(tmp_path))
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        out = capsys.readouterr().out
        assert "kiro-cli installer residue" in out
        assert "2 in" in out
        assert "2.0 MiB" in out
        # The remedy must name the setting AND its cost, so a user is not talked
        # into silently disabling their own security updates.
        assert "app.disableAutoupdates true" in out
        assert "per-user" in out
        assert issues == ["kiro-cli installer residue in temp"]

    def test_unusable_temp_volume_does_not_crash_doctor(self, monkeypatch, capsys) -> None:
        # gettempdir() raises when no candidate temp dir is usable. A diagnostic
        # must degrade to silence rather than abort the whole doctor run with a
        # traceback on exactly the host that most needs the rest of it.
        def boom() -> str:
            raise FileNotFoundError("No usable temporary directory found")

        monkeypatch.setattr(cli_doctor.tempfile, "gettempdir", boom)
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        assert issues == []
        assert capsys.readouterr().out == ""

    def test_large_total_renders_gib(self, monkeypatch, capsys) -> None:
        # Formatting only: writing gigabytes to disk in a test is not acceptable.
        monkeypatch.setattr(
            cli_doctor, "_scan_cli_installer_residue", lambda _d: (700, 80 * 1073741824)
        )
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        out = capsys.readouterr().out
        assert "80.00 GiB" in out
        # 700 is past the cap, so BOTH the count and the size are floors: the scan
        # stopped summing at the cap, so an exact-looking size would contradict
        # the "700+" beside it.
        assert "700+" in out
        assert "≥ 80.00 GiB" in out

    def test_uncapped_size_is_not_marked_as_a_floor(self, monkeypatch, capsys) -> None:
        # Below the cap the scan saw everything, so the figure is exact and must
        # NOT be hedged -- otherwise every host reads as approximate.
        monkeypatch.setattr(
            cli_doctor, "_scan_cli_installer_residue", lambda _d: (4, 4 * 1048576)
        )
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        out = capsys.readouterr().out
        assert "4.0 MiB" in out
        assert "≥" not in out
        assert "4+" not in out


class TestEffectiveModelSection:
    """`kirocrew doctor`'s Model section (#2559).

    The four-tier model precedence is not visible from any single file, so a
    stale spec pin that outlived the setting which created it is otherwise only
    diagnosable by hand-reading config.json, two agent-spec directories and the
    sidecar. This section names the winning tier and, when a pin is deciding,
    the exact command that clears it.

    ISOLATION: the section reads the directory the RESOLVER reads, and that
    resolver is ``kiro_home()``, which the suite's autouse fixtures deliberately
    do NOT pin (see the note in the rootdir conftest) -- it resolves the real
    machine-wide ``~/.kiro``. So every test here sets ``KIRO_HOME`` itself, and
    ``_agents_dir`` asserts the resolved path really is under tmp before writing
    a byte. Without that guard these tests overwrite the operator's live agent
    spec.
    """

    @pytest.fixture(autouse=True)
    def _isolate_kiro_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro-home"))
        self._tmp = tmp_path

    def _agents_dir(self) -> Path:
        from kiro_crew.config.paths import kiro_agents_dir

        agents_dir = kiro_agents_dir()
        # Fail loudly rather than write into a real home if the override lapses.
        assert self._tmp in agents_dir.parents or agents_dir.is_relative_to(self._tmp), (
            f"KIRO_HOME isolation failed: {agents_dir} is outside {self._tmp}"
        )
        agents_dir.mkdir(parents=True, exist_ok=True)
        return agents_dir

    def _cfg(self, global_model: str):
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent.model = global_model
        return cfg

    def _install_spec(self, model: str | None) -> Path:
        from kiro_crew.agent import AGENT_FILENAME

        body: dict = {"name": "kirocrew"}
        if model is not None:
            body["model"] = model
        spec = self._agents_dir() / AGENT_FILENAME
        spec.write_text(json.dumps(body), encoding="utf-8")
        return spec

    def test_spec_pin_decides_when_the_global_defers(self, capsys) -> None:
        """The reported symptom: the global says auto, so the spec pin decides
        and the report says so instead of leaving the user to work it out."""
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)

        out = capsys.readouterr().out
        assert "effective:   'claude-opus-4.8'" in out
        assert "decided by:  default spec pin" in out
        assert "kirocrew agent reset-model" in out
        # Advisory, not a setup failure: the state is legal and may be wanted.
        assert issues == []

    def test_explicit_global_outranks_the_spec_pin(self, capsys) -> None:
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("claude-haiku-4.5"), "", issues)

        out = capsys.readouterr().out
        assert "effective:   'claude-haiku-4.5'" in out
        assert "decided by:  global agent.model" in out
        # No pin is deciding, so no repair is offered.
        assert "reset-model" not in out
        assert issues == []

    def test_report_and_resolver_agreement_is_asserted(self, capsys) -> None:
        """The self-check must stay silent while the two agree -- if this line
        ever fires it means the tier list drifted from the resolver."""
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)
        assert "out of date" not in capsys.readouterr().out
        assert issues == []

    def test_tracking_state_is_reported(self, capsys) -> None:
        from kiro_crew import agent_state

        agent_state.set_model_managed("kirocrew", False)
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)
        assert "tracking:    frozen (explicit pick)" in capsys.readouterr().out

    def test_unrecorded_tracking_is_named(self, capsys) -> None:
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)
        assert "tracking:    not recorded" in capsys.readouterr().out

    def test_unreadable_spec_is_reported_not_swallowed(self, capsys) -> None:
        from kiro_crew.agent import AGENT_FILENAME

        (self._agents_dir() / AGENT_FILENAME).write_text("{ not json", encoding="utf-8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)
        assert "unreadable" in capsys.readouterr().out
        assert issues == ["agent spec unreadable"]

    def test_project_local_spec_is_flagged_as_shadowing(self, capsys) -> None:
        """kiro-cli resolves <project>/.kiro/agents FIRST and Kiro Crew's own
        resolver never reads it, so that file can decide what actually runs while
        every Kiro Crew surface reports something else."""
        from kiro_crew.agent import AGENT_FILENAME

        self._install_spec(None)
        project = self._tmp / "proj"
        (project / ".kiro" / "agents").mkdir(parents=True)
        (project / ".kiro" / "agents" / AGENT_FILENAME).write_text(
            json.dumps({"name": "kirocrew", "model": "claude-opus-4.8"}), encoding="utf-8"
        )
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), str(project), issues)

        out = capsys.readouterr().out
        assert "project spec" in out
        assert "claude-opus-4.8" in out
        assert "kiro-cli loads this one first" in out
        assert issues == ["project-local agent spec shadows the user-level one"]

    def test_no_project_dir_prints_no_project_line(self, capsys) -> None:
        self._install_spec(None)
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)
        assert "project spec" not in capsys.readouterr().out
        assert issues == []

    def _bind_custom_agent(self, cfg, name: str):
        """Point the default alias at a non-built-in kiro agent."""
        from kiro_crew.config.loader import KiroCrewAgentConfig

        cfg.default_agent = "default"
        cfg.agents["default"] = KiroCrewAgentConfig(kiro_agent=name)
        return cfg

    def test_a_bound_custom_agent_is_attributed_to_its_own_spec(self, capsys) -> None:
        """The default alias may bind a kiro agent other than the built-in one,
        and the resolver consults THAT spec's pin above the global (tier 2).
        Reading kirocrew.json in both cases attributed the pin to the wrong file
        and printed a reset command for the wrong agent (#4911 review)."""
        self._install_spec(None)
        agents_dir = self._agents_dir()
        (agents_dir / "custom-agent.json").write_text(
            json.dumps({"name": "custom-agent", "model": "claude-opus-4.8"}), encoding="utf-8"
        )
        cfg = self._bind_custom_agent(self._cfg("auto"), "custom-agent")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, "", issues)

        out = capsys.readouterr().out
        assert "effective:   'claude-opus-4.8'" in out
        assert "decided by:  bound agent pin ('custom-agent')" in out
        # The repair must name the agent that actually holds the pin.
        assert "kirocrew agent reset-model --agent 'custom-agent'" in out
        # And the tier the resolver skipped for the built-in agent is shown here.
        assert "bound agent pin ('custom-agent'):" in out
        assert "out of date" not in out, "report must agree with the resolver"
        assert issues == []

    def test_the_builtin_agent_shows_no_bound_tier(self, capsys) -> None:
        """Tier 2 is skipped for the built-in agent, so the list must not show
        a tier the resolver never consulted."""
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)

        out = capsys.readouterr().out
        assert "bound agent pin" not in out
        assert "decided by:  default spec pin" in out
        assert "kirocrew agent reset-model" in out
        assert "--agent" not in out, "the built-in agent needs no --agent flag"

    def test_tracking_names_the_agent_it_describes(self, capsys) -> None:
        from kiro_crew import agent_state

        self._install_spec(None)
        agents_dir = self._agents_dir()
        (agents_dir / "custom-agent.json").write_text(
            json.dumps({"name": "custom-agent", "model": "claude-opus-4.8"}), encoding="utf-8"
        )
        agent_state.set_model_managed("custom-agent", False)
        cfg = self._bind_custom_agent(self._cfg("auto"), "custom-agent")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, "", issues)
        assert "tracking:    frozen (explicit pick) ('custom-agent')" in capsys.readouterr().out

    def test_project_spec_check_follows_the_bound_agent(self, capsys) -> None:
        """kiro-cli dispatches the BOUND agent, so that is the filename whose
        project-local copy can shadow the user-level spec."""
        self._install_spec(None)
        agents_dir = self._agents_dir()
        (agents_dir / "custom-agent.json").write_text(
            json.dumps({"name": "custom-agent"}), encoding="utf-8"
        )
        project = self._tmp / "proj"
        (project / ".kiro" / "agents").mkdir(parents=True)
        (project / ".kiro" / "agents" / "custom-agent.json").write_text(
            json.dumps({"name": "custom-agent", "model": "claude-haiku-4.5"}), encoding="utf-8"
        )
        cfg = self._bind_custom_agent(self._cfg("auto"), "custom-agent")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, str(project), issues)

        out = capsys.readouterr().out
        assert "custom-agent.json' -> 'claude-haiku-4.5'" in out
        assert issues == ["project-local agent spec shadows the user-level one"]

    def test_control_sequences_in_a_spec_model_are_escaped(self, capsys) -> None:
        """An agent spec is not always trusted input -- an installed app writes
        one and a cloned repository can ship a project-local one -- so an
        OSC/ANSI sequence in `model` must reach the terminal inert rather than
        executing controls or spoofing the surrounding diagnostic lines."""
        hostile = "claude-opus-4.8\x1b]0;pwned\x07\x1b[2K"
        self._install_spec(hostile)
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)

        out = capsys.readouterr().out
        assert "\x1b" not in out, "raw escape reached the terminal"
        assert "\x07" not in out
        assert "\\x1b" in out, "the value is still shown, just escaped"

    def test_control_sequences_in_a_project_spec_are_escaped(self, capsys) -> None:
        from kiro_crew.agent import AGENT_FILENAME

        self._install_spec(None)
        project = self._tmp / "proj"
        (project / ".kiro" / "agents").mkdir(parents=True)
        (project / ".kiro" / "agents" / AGENT_FILENAME).write_text(
            json.dumps({"name": "kirocrew", "model": "x\x1b[31mred"}), encoding="utf-8"
        )
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), str(project), issues)

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\\x1b" in out

    def test_control_sequences_in_the_global_are_escaped(self, capsys) -> None:
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto\x1b[2J"), "", issues)

        assert "\x1b" not in capsys.readouterr().out

    @requires_symlinks
    def test_a_symlink_to_a_sensitive_target_is_refused(self, monkeypatch, capsys) -> None:
        """The doctor read goes through agent_discovery's hardened reader, which
        refuses a symlink whose RESOLVED target is sensitive (the documented
        `evil.json -> ~/.aws/credentials` case) and caps the read size. Routing
        through that one reader instead of hand-rolling the checks is the point
        (#4911 review); a benign link is followed exactly as the resolver follows
        it, so the report cannot disagree with what will actually run."""
        from kiro_crew import agent_discovery
        from kiro_crew.agent import AGENT_FILENAME

        agents_dir = self._agents_dir()
        target = self._tmp / "protected.json"
        target.write_text(json.dumps({"model": "leaked-value"}), encoding="utf-8")
        (agents_dir / AGENT_FILENAME).symlink_to(target)
        monkeypatch.setattr(agent_discovery, "is_sensitive_path", lambda p: str(target) in str(p))
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)

        out = capsys.readouterr().out
        # The report refuses to ATTRIBUTE the refused spec ...
        assert "unreadable" in out
        assert issues == ["agent spec unreadable"]
        assert "(defers)" in out.split("default spec pin:", 1)[1].splitlines()[0]
        # ... and nothing else acts on it either: the resolver reads through
        # the same hardened reader, so it refuses too -- `effective` carries no
        # value from the refused spec, and there is no resolver-vs-report gap
        # to explain.
        assert "leaked-value" not in out
        assert "refused to follow" not in out
        assert "out of date" not in out

    def test_an_absent_spec_is_not_reported_as_a_fault(self, capsys) -> None:
        """A clean install has no spec and the resolver just falls through, so
        absence must not raise an issue."""
        self._agents_dir()  # exists, but empty
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)

        assert "unreadable" not in capsys.readouterr().out
        assert issues == []

    def test_an_absolute_kiro_agent_binding_cannot_escape_the_agent_dir(self, capsys) -> None:
        """`kiro_agent` is free text in config.json and reaches a path join, and
        pathlib DISCARDS the left side when the right is absolute -- so an
        unvalidated binding would turn a spec lookup into an arbitrary read
        (#4911 review)."""
        self._install_spec(None)
        secret = self._tmp / "protected.json"
        secret.write_text(json.dumps({"model": "leaked-value"}), encoding="utf-8")
        cfg = self._bind_custom_agent(self._cfg("auto"), str(secret)[:-5])
        project = self._tmp / "proj"
        (project / ".kiro" / "agents").mkdir(parents=True)
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, str(project), issues)

        out = capsys.readouterr().out
        assert "leaked-value" not in out
        assert "not a valid agent name" in out
        assert "configured kiro_agent is not a valid agent name" in issues

    def test_a_control_bearing_binding_is_escaped_and_refused(self, capsys) -> None:
        self._install_spec(None)
        cfg = self._bind_custom_agent(self._cfg("auto"), "evil\x1b[2Jname")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, "", issues)

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "not a valid agent name" in out

    def test_a_control_bearing_project_filename_is_escaped(self, monkeypatch, capsys) -> None:
        """A cloned repository can TRACK a filename containing control bytes, so
        the path itself is untrusted input on this line (#4911 review).

        The hostile path is INJECTED rather than created: control bytes are
        illegal in a Windows filename, so building it on disk would make this
        assertion Windows-only-skipped, and what is under test is that the
        printer escapes what it is handed.
        """
        hostile = Path("/tmp/proj/.kiro/agents/kirocrew\x1b[2J.json")
        self._install_spec(None)
        real_reader = cli_doctor._read_agent_spec
        monkeypatch.setattr(cli_doctor, "project_agent_files", lambda d: [hostile])
        monkeypatch.setattr(cli_doctor, "project_agent_name", lambda p: "kirocrew")
        # Only the injected path is faked; the user-level spec still goes through
        # the real reader so the report's own self-check is not disturbed. The
        # stub forwards **kw because the reader takes keyword-only SEL
        # attribution labels (#6722) that this test does not care about.
        monkeypatch.setattr(
            cli_doctor,
            "_read_agent_spec",
            lambda p, **kw: {"model": "m"} if p == hostile else real_reader(p, **kw),
        )
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "/tmp/proj", issues)

        out = capsys.readouterr().out
        # Not vacuous: the shadow line must actually be reached.
        assert "project spec" in out
        assert issues == ["project-local agent spec shadows the user-level one"]
        assert "\x1b" not in out, "raw escape from a tracked filename reached the terminal"
        assert "\\x1b" in out, "the path is still shown, just escaped"

    def test_a_non_string_kiro_agent_does_not_crash_the_report(self, capsys) -> None:
        """The config loader deliberately KEEPS a type-mismatched value ("validated
        by its consumer"), so a hand-edited non-string reaches this section intact
        and a bare `re.match` would raise TypeError -- aborting the one command a
        user runs BECAUSE their config is broken (#4911 review)."""
        self._install_spec("claude-opus-4.8")
        cfg = self._bind_custom_agent(self._cfg("auto"), "placeholder")
        cfg.agents["default"].kiro_agent = 12345  # type: ignore[assignment]
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, "", issues)

        out = capsys.readouterr().out
        assert "not a valid agent name" in out
        assert "configured kiro_agent is not a valid agent name" in issues
        # It degrades to the built-in agent and still produces the report.
        assert "effective:" in out
        assert "tracking:" in out


class TestWhatsAppSection:
    """`kirocrew doctor`'s WhatsApp Integration section.

    WhatsApp is the only channel whose whole runtime hangs off an OPTIONAL wheel
    plus a locally stored credential, and neither absence produces an error the
    operator sees: a message simply never arrives. So the section has to answer
    both, and it has to answer them WITHOUT loading the Go core: a preflight that
    initializes the subsystem it is inspecting is both slow and a side effect.
    """

    def _cfg(self, *, enabled: bool = True, groups: list | None = None):
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.whatsapp.enabled = enabled
        cfg.whatsapp.groups = groups if groups is not None else []
        return cfg

    @pytest.fixture()
    def home(self, tmp_path: Path, monkeypatch) -> Path:
        """Pin the data home the section reports on, so no real store is read."""
        target = tmp_path / "home"
        target.mkdir()
        monkeypatch.setattr(cli_doctor, "data_home", lambda: target)
        return target

    @staticmethod
    def _extra(monkeypatch, present: bool) -> None:
        monkeypatch.setattr(
            "kiro_crew.whatsapp.client.neonize_available", lambda: present
        )

    @staticmethod
    def _pair(home: Path) -> Path:
        from kiro_crew.whatsapp.client import default_db_path

        store = default_db_path(home)
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_bytes(b"sqlite")
        return store

    def test_a_disabled_channel_names_the_two_ways_to_enable_it(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        """The channel must be VISIBLE in the preflight even when off, because that
        is the
        surface an operator checks before wondering why nothing arrives."""
        self._extra(monkeypatch, True)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(enabled=False), issues)

        out = capsys.readouterr().out
        assert "WhatsApp Integration" in out
        assert "not enabled" in out
        assert "setup --whatsapp" in out
        assert issues == []

    def test_a_missing_extra_on_an_enabled_channel_is_a_reported_issue(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        """Config says the channel is on and the wheel it needs is absent: the
        channel cannot start at all, and the fix is one offline pip install."""
        self._extra(monkeypatch, False)
        self._pair(home)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(), issues)

        out = capsys.readouterr().out
        # neonize by name -- the extras form is not installable from an index.
        assert "neonize" in out
        assert "kirocrew[" not in out
        assert "whatsapp extra missing" in issues

    def test_an_installed_extra_and_a_paired_store_report_clean(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        self._extra(monkeypatch, True)
        store = self._pair(home)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(), issues)

        out = capsys.readouterr().out
        assert "extra:       ✅" in out
        assert f"session:     ✅ paired session store at {store}" in out
        assert issues == []

    def test_an_unpaired_store_warns_but_never_fails_doctor(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        """Load-bearing split. Pairing is a QR scan served BY the running gateway,
        so a freshly enabled channel legitimately has no store yet. Counting that
        as an issue would exit 1 and break the documented
        `kirocrew doctor && kirocrew gateway` chain at the one moment the operator
        has to start the gateway to make progress.
        """
        self._extra(monkeypatch, True)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(), issues)

        out = capsys.readouterr().out
        assert "not paired yet" in out
        assert "Settings → Channels" in out
        assert issues == [], "an unpaired channel must not fail the preflight"

    def test_the_reported_store_is_the_path_the_gateway_opens(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        """Doctor and the channel must resolve ONE path, or the report describes a
        store the gateway never touches."""
        from kiro_crew.whatsapp.client import default_db_path

        self._extra(monkeypatch, True)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(), issues)

        assert str(default_db_path(home)) in capsys.readouterr().out

    def test_the_check_never_imports_neonize(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        """The whole point of the ``find_spec`` probe: importing neonize loads a
        ~19 MB ctypes CDLL plus protobuf descriptors, and a health check must not
        pay that (or construct a client as a side effect of asking a question).
        """
        import builtins

        real_import = builtins.__import__

        def _guard(name, *args, **kwargs):
            if name.split(".")[0] == "neonize":
                raise AssertionError(
                    f"doctor imported {name!r}: the preflight must stay a find_spec check"
                )
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _guard)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(), issues)

        assert "WhatsApp Integration" in capsys.readouterr().out

    def test_configured_groups_are_counted_and_junk_entries_are_not(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        """A hand-edited config reaches this section intact, so a non-dict or a
        blank JID must neither be counted nor crash the one command a user runs
        BECAUSE their config is broken."""
        self._extra(monkeypatch, True)
        issues: list[str] = []
        groups = [{"jid": "123@g.us"}, {"jid": "  "}, "not-a-dict", {"jid": "456@g.us"}]

        cli_doctor._doctor_whatsapp(self._cfg(groups=groups), issues)

        assert "groups:      ✅ 2 configured" in capsys.readouterr().out

    def test_no_configured_groups_says_group_messages_are_ignored(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        self._extra(monkeypatch, True)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(groups=[]), issues)

        assert "none configured" in capsys.readouterr().out

    def test_the_section_is_wired_into_the_doctor_run(self) -> None:
        """Guards the call site itself. Every other test here drives the helper
        directly, so a deleted call would leave them all green and the operator
        with no WhatsApp line, the exact gap this section was added to close.
        ``_doctor()`` spawns subprocesses, probes the network and calls
        ``sys.exit``, so its source is read rather than run.
        """
        import inspect

        source = inspect.getsource(cli_doctor._doctor)
        assert "_doctor_whatsapp(cfg, issues)" in source


class TestVenvDepsProbe:
    """The deps probe answers for the VENV, never the doctor's own process.

    ``python -c`` puts the child's CWD at ``sys.path[0]`` and inherits
    ``PYTHONPATH``, so an unisolated probe imports whatever decoy package
    sits on either route -- making the doctor's verdict describe the
    caller's environment instead of the venv under test (the false-healthy
    the isolated ``dep_sync._probe_interpreter`` closes). The decoys here
    raise on import: a probe that can still see them fails against an
    interpreter that genuinely serves the real modules, so each test proves
    the route is closed in a way that does not depend on which direction the
    decoy lies in. The probe children run a fixed read-only import with the
    cwd the code under test pins (the interpreter's own bin dir) -- nothing
    is written, so the tmp-cwd rule for file-creating children does not
    apply, and pointing them at ``tmp_path`` would test nothing.
    """

    _DEP_NAMES = ("websockets", "slack_sdk", "aiohttp")

    def _plant_raising_decoys(self, root: Path) -> Path:
        decoy = root / "decoy-path"
        for name in self._DEP_NAMES:
            pkg = decoy / name
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text(
                "raise ImportError('decoy package imported')", encoding="utf-8"
            )
        return decoy

    def test_decoy_on_pythonpath_is_invisible_to_the_probe(self, tmp_path, monkeypatch) -> None:
        """PYTHONPATH entries rank ahead of site-packages, so an unisolated
        probe imports the raising decoys and misreports this healthy
        interpreter as missing its deps."""
        decoy = self._plant_raising_decoys(tmp_path)
        monkeypatch.setenv("PYTHONPATH", str(decoy))

        assert cli_doctor._venv_deps_ok(Path(sys.executable)) is True

    def test_decoy_in_the_callers_cwd_is_invisible_to_the_probe(
        self, tmp_path, monkeypatch
    ) -> None:
        """The second route: the caller's CWD lands at ``sys.path[0]`` for an
        unisolated ``python -c``, ranking the decoys above site-packages."""
        decoy = self._plant_raising_decoys(tmp_path)
        monkeypatch.chdir(decoy)

        assert cli_doctor._venv_deps_ok(Path(sys.executable)) is True

    def test_missing_modules_still_report_missing(self, monkeypatch) -> None:
        """Isolation must not soften the verdict: a probe exiting nonzero is
        exactly the missing-deps answer the doctor section exists to show."""
        monkeypatch.setattr(
            cli_doctor.dep_sync,
            "_probe_interpreter",
            lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=1),
        )

        assert cli_doctor._venv_deps_ok(Path(sys.executable)) is False

    def test_a_wedged_interpreter_reports_missing(self, monkeypatch) -> None:
        """A hung venv python must surface as a deps failure, not hang the
        operator's doctor run or escape as a traceback."""

        def _hang(*a, **k):
            raise subprocess.TimeoutExpired(cmd="python", timeout=5)

        monkeypatch.setattr(cli_doctor.dep_sync, "_probe_interpreter", _hang)

        assert cli_doctor._venv_deps_ok(Path(sys.executable)) is False

    def test_an_unspawnable_interpreter_reports_missing(self, tmp_path) -> None:
        assert cli_doctor._venv_deps_ok(tmp_path / "no-such-venv" / "python") is False

    def test_the_probe_asks_the_venv_for_all_three_core_deps(self, monkeypatch) -> None:
        """Pins the probe's question itself: the decoy tests above pass any
        probe that ignores PYTHONPATH, including one that stopped importing a
        module the gateway needs."""
        seen: dict = {}

        def record(target_py, code, timeout=None):
            seen.update(target=target_py, code=code, timeout=timeout)
            return subprocess.CompletedProcess(args=[], returncode=0)

        monkeypatch.setattr(cli_doctor.dep_sync, "_probe_interpreter", record)

        assert cli_doctor._venv_deps_ok(Path("/v/bin/python")) is True
        assert seen["code"] == "import websockets, slack_sdk, aiohttp"
        assert seen["target"] == Path("/v/bin/python")
        assert seen["timeout"] == 15

    def test_the_probe_is_wired_into_the_doctor_run(self) -> None:
        """Guards the call site: every other test drives the helper directly,
        so a deleted call would leave them green while the doctor silently
        skipped the check. ``_doctor()`` spawns subprocesses and calls
        ``sys.exit``, so its source is read rather than run."""
        import inspect

        source = inspect.getsource(cli_doctor._doctor)
        assert "_venv_deps_ok(venv_py)" in source


class TestCronHealth:
    """`kirocrew doctor` Cron Jobs section — auto-paused / errored jobs.

    Read-only by contract: the check reports and hints, it never resumes or
    triggers anything. The negative half of this suite (healthy store,
    user-paused job, missing file) is what stops the check crying wolf.
    """

    @staticmethod
    def _job(job_id: str, name: str, **over: object) -> dict:
        job = {
            "id": job_id,
            "name": name,
            "message": "do a thing",
            "schedule": {"kind": "every", "every_secs": 3600},
            "enabled": True,
            "user_paused": False,
            "auto_paused": False,
            "last_status": "ok",
        }
        job.update(over)
        return job

    def _write(self, tmp_path: Path, *jobs: dict) -> Path:
        path = tmp_path / "crons.json"
        path.write_text(json.dumps({"version": 2, "jobs": list(jobs)}), encoding="utf-8")
        return path

    def _run(self, monkeypatch, tmp_path: Path) -> list[str]:
        # The scan lives in cron.py (single owner of the pause predicates), so
        # the data home is patched THERE; doctor is only the presentation half.
        monkeypatch.setattr(cron, "config_dir", lambda: tmp_path)
        issues: list[str] = []
        cli_doctor._doctor_cron_health(issues)
        return issues

    # ── positive: the signals ARE reported ──

    def test_auto_paused_job_is_reported_with_resume_hint(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        self._write(
            tmp_path,
            self._job("j1", "nightly-sync", auto_paused=True, enabled=False, last_status="error"),
        )

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "Cron Jobs" in out
        assert "auto-paused" in out
        assert "'nightly-sync' ('j1')" in out
        assert "kirocrew cron resume <id>" in out
        assert issues == ["1 cron job(s) auto-paused"]

    def test_errored_job_is_reported_with_trigger_hint(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        self._write(tmp_path, self._job("j2", "pr-watch", last_status="error"))

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "errored:" in out
        assert "'pr-watch' ('j2')" in out
        assert "kirocrew cron trigger <id>" in out
        assert issues == ["1 cron job(s) last ran with an error"]

    def test_a_user_paused_at_job_with_a_stale_error_is_not_reported(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """An explicitly paused ``at`` job must stay silent even carrying an error.

        The user pause is the later, more specific instruction, so it wins -- the
        contract this function's own docstring states. A stale ``last_status``
        from a run before the pause is not a reason to hand back a hint for a job
        the user switched off.

        Paired with the sibling below, which keeps a NON-paused errored at-job
        reported: neither test alone pins the distinction, because one mutation
        can only move one of the two outcomes.
        """
        self._write(
            tmp_path,
            self._job(
                "j-at",
                "one-off-import",
                schedule={"kind": "at", "at_ts": 1.0},
                enabled=False,
                user_paused=True,
                last_status="error",
            ),
        )

        issues = self._run(monkeypatch, tmp_path)

        assert issues == []
        out = capsys.readouterr().out
        assert "errored:" not in out
        assert "'one-off-import' ('j-at')" not in out

    def test_a_non_paused_at_job_with_an_error_is_still_reported(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The other half: silencing paused jobs must not silence live failures.

        Same ``at`` schedule and the same errored status as the sibling above --
        only the pause flag differs, so this is the assertion that catches a fix
        that simply stopped reporting at-jobs.
        """
        self._write(
            tmp_path,
            self._job(
                "j-at-live",
                "live-import",
                schedule={"kind": "at", "at_ts": 1.0},
                enabled=True,
                user_paused=False,
                last_status="error",
            ),
        )

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "errored:" in out
        assert "'live-import' ('j-at-live')" in out
        assert issues == ["1 cron job(s) last ran with an error"]

    def test_auto_paused_job_is_not_also_counted_as_errored(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # A job only auto-pauses by failing repeatedly, so it carries
        # last_status="error" too. Reporting both would print contradictory
        # advice (resume vs. re-trigger) for one job.
        self._write(
            tmp_path,
            self._job("j3", "flaky", auto_paused=True, enabled=False, last_status="error"),
        )

        issues = self._run(monkeypatch, tmp_path)

        assert issues == ["1 cron job(s) auto-paused"]
        assert "errored:" not in capsys.readouterr().out

    def test_job_list_is_capped_with_a_plus_n_more_tail(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # A user with dozens of crons must not get a wall of text.
        jobs = [self._job(f"j{n}", f"job-{n}", auto_paused=True, enabled=False) for n in range(8)]
        self._write(tmp_path, *jobs)

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "+3 more" in out
        assert "'job-0' ('j0')" in out
        assert "'job-7' ('j7')" not in out, "beyond the cap must be summarised, not listed"
        assert issues == ["8 cron job(s) auto-paused"]

    # ── negative: healthy / deliberate state is NOT reported ──

    def test_healthy_store_is_silent(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._write(tmp_path, self._job("j4", "fine"), self._job("j5", "also-fine"))

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    def test_user_paused_job_is_not_reported(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # user_paused is deliberately distinct from auto_paused: a job the user
        # paused on purpose is not a health signal, and neither is a stale
        # last_status left over from before they paused it.
        self._write(
            tmp_path,
            self._job("j6", "on-purpose", user_paused=True, enabled=False, last_status="error"),
        )

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    def test_legacy_record_without_user_paused_key_is_not_reported(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Records written before `user_paused` existed carry the reason only in
        # `enabled`; the deserializer derives user_paused from it, and so must this.
        job = self._job("j7", "legacy", enabled=False, last_status="error")
        del job["user_paused"]
        self._write(tmp_path, job)

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    # ── degradation: a broken or absent store must not fail doctor ──

    def test_missing_crons_file_is_silent(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # Every fresh install: no crons yet.
        assert not (tmp_path / "crons.json").exists()

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    @pytest.mark.parametrize(
        "body",
        ["not json at all", "", "[]", '{"jobs": "not-a-list"}', '{"jobs": [null, 3]}'],
        ids=["garbage", "empty", "top-level-list", "jobs-not-a-list", "jobs-of-scalars"],
    )
    def test_corrupt_crons_file_is_reported_not_silent(
        self, monkeypatch, tmp_path: Path, capsys, body: str
    ) -> None:
        # The run on a host with a corrupt crons.json is exactly the run that
        # most needs doctor's other checks — it must not get a traceback. But it
        # must not be SILENT either: the scheduler can load no jobs from an
        # unreadable store, so every job has stopped, and reporting a clean bill
        # of health there is the silence this check exists to break.
        (tmp_path / "crons.json").write_text(body, encoding="utf-8")

        issues = self._run(monkeypatch, tmp_path)

        assert "could not be read" in capsys.readouterr().out
        assert issues == ["cron store unreadable"]

    def test_one_malformed_record_does_not_discard_the_rest(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        path = tmp_path / "crons.json"
        good = self._job("j8", "real-job", auto_paused=True, enabled=False)
        path.write_text(json.dumps({"jobs": ["junk", good]}), encoding="utf-8")

        issues = self._run(monkeypatch, tmp_path)

        assert "'real-job' ('j8')" in capsys.readouterr().out
        assert issues == ["1 cron job(s) auto-paused"]

    def test_record_with_blank_id_and_name_still_renders(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # The `(unnamed)` / `no-id` label fallback, on a record the scheduler
        # CAN load: `_job_from_record` needs the keys present, not non-empty, so
        # blank strings still build a job and must still be nameable. A record
        # MISSING those keys is a different case -- unloadable, so it is skipped
        # and reported as a broken store instead (see the unloadable-record
        # test above); asserting a hint for it would encode that defect.
        self._write(tmp_path, self._job("", "", auto_paused=True, enabled=False))

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "(unnamed)" in out and "no-id" in out
        assert issues == ["1 cron job(s) auto-paused"]

    def test_an_auto_paused_job_the_user_also_paused_is_not_reported(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Both flags can be set at once, and the user pause wins.

        `_enable_job_locked` clears `auto_paused` only when ENABLING, so pausing
        an already-auto-paused job leaves `auto_paused` true and adds
        `user_paused`. Telling the user to resume a job they deliberately
        switched off would contradict the more specific instruction.
        """
        self._write(
            tmp_path,
            self._job(
                "j10",
                "off-on-purpose",
                auto_paused=True,
                user_paused=True,
                enabled=False,
                last_status="error",
            ),
        )

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    def test_invalid_utf8_in_the_store_is_reported_not_silent(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # The store is bytes on disk and can hold invalid UTF-8. A
        # UnicodeDecodeError here would abort the whole doctor run; swallowing
        # it silently would instead hide that no job can load at all.
        (tmp_path / "crons.json").write_bytes(b'{"jobs": [{"id": "a", "name": "\xff\xfe"}]}')

        issues = self._run(monkeypatch, tmp_path)

        assert "could not be read" in capsys.readouterr().out
        assert issues == ["cron store unreadable"]

    def test_deeply_nested_json_is_reported_not_silent(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # json.loads raises RecursionError on deeply nested input, and
        # RecursionError is a RuntimeError -- NOT a ValueError -- so it escapes
        # the decode-error tuple and would abort the whole doctor run. Caught, it
        # is still an unreadable store and must be reported rather than hidden.
        depth = 100_000
        (tmp_path / "crons.json").write_text("[" * depth + "]" * depth, encoding="utf-8")

        issues = self._run(monkeypatch, tmp_path)

        assert "could not be read" in capsys.readouterr().out
        assert issues == ["cron store unreadable"]

    def test_a_store_of_non_job_dicts_is_reported_not_silent(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # `{}` is a dict, so an isinstance-only shape check calls this store
        # readable -- but `_job_from_record` rejects it (KeyError: 'id'), so the
        # scheduler loads ZERO jobs from it. Entries were present and none is
        # loadable: that is the "parsed but nothing came out" fault this check
        # exists to surface, not an honestly empty store.
        (tmp_path / "crons.json").write_text('{"jobs": [{}]}', encoding="utf-8")

        issues = self._run(monkeypatch, tmp_path)

        assert "could not be read" in capsys.readouterr().out
        assert issues == ["cron store unreadable"]

    def test_an_unloadable_record_does_not_produce_a_bogus_resume_hint(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # `{"auto_paused": true}` carries no id/name/message, so the scheduler
        # rejects it and runs nothing -- but classifying it BEFORE checking
        # loadability puts it in the auto-paused bucket, so doctor advises
        # `cron resume` for a job that does not exist and the unloadable-store
        # report never fires. The store is the fault; the phantom job is not.
        (tmp_path / "crons.json").write_text(
            '{"jobs": [{"auto_paused": true}]}', encoding="utf-8"
        )

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "could not be read" in out
        assert "resume" not in out
        assert issues == ["cron store unreadable"]

    def test_a_crons_json_directory_is_reported_not_silent(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # `is_file()` is False for a DIRECTORY just as it is for a missing file,
        # so exempting on it silently classifies an unloadable store as the
        # fresh-install case. The scheduler can load nothing from a directory.
        (tmp_path / "crons.json").mkdir()

        issues = self._run(monkeypatch, tmp_path)

        assert "could not be read" in capsys.readouterr().out
        assert issues == ["cron store unreadable"]

    def test_a_readable_but_empty_store_stays_silent(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # The boundary that stops the unreadable-store report crying wolf: a
        # store that parses fine and simply holds no jobs is NOT a fault, and
        # must stay silent even though the scan returns nothing — exactly like
        # the missing-file case.
        (tmp_path / "crons.json").write_text('{"jobs": []}', encoding="utf-8")

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    def test_a_control_bearing_job_name_is_escaped(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # A job name is free text an app or a hand-edit supplies, so it must not
        # be able to act on the terminal or spoof the surrounding report lines.
        self._write(
            tmp_path,
            self._job("j11", "evil\x1b[2Jname", auto_paused=True, enabled=False),
        )

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "\x1b" not in out, "raw escape from a job name reached the terminal"
        assert "\\x1b" in out, "the name is still shown, just escaped"
        assert issues == ["1 cron job(s) auto-paused"]

    def test_check_is_read_only(self, monkeypatch, tmp_path: Path) -> None:
        # The whole point: doctor diagnoses, it never resumes or triggers.
        path = self._write(
            tmp_path,
            self._job("j9", "paused-job", auto_paused=True, enabled=False, last_status="error"),
        )
        before = path.read_bytes()

        self._run(monkeypatch, tmp_path)

        assert path.read_bytes() == before, "doctor must not mutate crons.json"
