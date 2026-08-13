"""Tests for the `kirocrew doctor` OS-aware fix hints.

Guards _os_fix_hint: it returns the macOS Homebrew command on Darwin and the
Linux/AL2023 guidance otherwise, so `kirocrew doctor` never prints a brew
command on Linux where there is no brew.
"""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew import cli_doctor


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
    """`kirocrew doctor` KAS backend section — gated on acp_backend == kas."""

    class _Cfg:
        def __init__(self, backend: str) -> None:
            self.agent = type("A", (), {"acp_backend": backend})()

    def _patch_cfg(self, monkeypatch, backend: str) -> None:
        monkeypatch.setattr(
            cli_doctor.KiroCrewConfig, "load", classmethod(lambda cls: self._Cfg(backend))
        )

    def test_version_label_from_bundle_path(self) -> None:
        script = Path("/home/u/.local/share/kiro-cli/kas/2.18.0-abc123/nm/acp-server.js")
        assert cli_doctor._kas_version_label(script) == "2.18.0-abc123"

    def test_version_label_unknown_for_unexpected_layout(self) -> None:
        assert cli_doctor._kas_version_label(Path("/opt/foo/acp-server.js")) == "unknown"

    def test_silent_when_backend_not_kas(self, monkeypatch, capsys) -> None:
        self._patch_cfg(monkeypatch, "")
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        assert "KAS backend" not in capsys.readouterr().out
        assert issues == []

    def test_selected_but_assets_missing_appends_issue(self, monkeypatch, capsys) -> None:
        self._patch_cfg(monkeypatch, "kas")
        from kiro_crew.acp import kas_assets, kas_auth

        monkeypatch.setattr(kas_assets, "find_kas_node", lambda: None)
        monkeypatch.setattr(kas_assets, "find_kas_server_script", lambda: None)

        async def _raise(*, timeout: float = 8.0):
            raise kas_auth.KasAuthCallbackError("kiro-cli not found; cannot obtain a KAS token")

        monkeypatch.setattr(kas_auth, "resolve_kas_access_token", _raise)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "KAS backend" in out
        assert "❌ not found" in out
        assert "KAS backend selected but assets missing" in issues
        # Token bytes never printed; only the advisory line.
        assert "not obtainable" in out

    def test_token_ok_prints_expiry_not_token(self, monkeypatch, capsys) -> None:
        self._patch_cfg(monkeypatch, "kas")
        from kiro_crew.acp import kas_assets, kas_auth

        monkeypatch.setattr(kas_assets, "find_kas_node", lambda: Path("/x/node"))
        monkeypatch.setattr(
            kas_assets,
            "find_kas_server_script",
            lambda: Path("/x/kas/9.9.9-hash/nm/acp-server.js"),
        )

        async def _ok(*, timeout: float = 8.0):
            return {"accessToken": "SECRET-DO-NOT-PRINT", "expiresAt": "2099-01-01T00:00:00Z"}

        monkeypatch.setattr(kas_auth, "resolve_kas_access_token", _ok)
        issues: list[str] = []
        cli_doctor._doctor_kas(issues)
        out = capsys.readouterr().out
        assert "9.9.9-hash" in out
        assert "2099-01-01T00:00:00Z" in out
        assert "SECRET-DO-NOT-PRINT" not in out
        assert issues == []


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
        """git resolves via trusted_system_bin; a miss means no subprocess at all.

        Doctor runs with operator privileges, so a ``git`` shim planted in an
        agent-writable PATH directory must never execute: when the trusted
        resolver declines, _git_line collapses to None without spawning.
        When it resolves, the pinned absolute path — not the bare name — is
        what reaches argv[0].
        """
        import subprocess as _sp

        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _sp.CompletedProcess(argv, 0, stdout="main\n", stderr="")

        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)

        # Miss: no trusted git -> None, and no process spawned. Neutralize
        # the Windows fallback too so the miss is a miss on every platform
        # (on a real Windows runner _windows_git_bin finds the actual Git
        # for Windows install; the fallback has its own dedicated test).
        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: None
        )
        monkeypatch.setattr(cli_doctor, "_windows_git_bin", lambda: None)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") is None
        assert calls == []

        # Hit: the resolved absolute path is argv[0], never the bare "git".
        monkeypatch.setattr(
            cli_doctor.platform_compat,
            "trusted_system_bin",
            lambda _n: "/usr/bin/git",
        )
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") == "main"
        assert calls and calls[0][0] == "/usr/bin/git"

    def test_git_line_windows_falls_back_to_git_for_windows_roots(
        self, monkeypatch, tmp_path
    ) -> None:
        """On Windows a system-dirs miss probes the fixed Git for Windows roots.

        Git for Windows installs under Program Files, never System32, so
        without the fallback every supported Windows source install reported
        "could not check". The fallback stays pinned: fixed literal roots, and
        a miss there still means no subprocess.
        """
        import subprocess as _sp

        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _sp.CompletedProcess(argv, 0, stdout="main\n", stderr="")

        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: None
        )
        monkeypatch.setattr(cli_doctor.platform_compat, "IS_WINDOWS", True)

        gfw = r"C:\Program Files\Git\cmd\git.exe"
        monkeypatch.setattr(cli_doctor, "_windows_git_bin", lambda: gfw)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") == "main"
        assert calls and calls[0][0] == gfw

        # Fallback miss: still no spawn at all.
        calls.clear()
        monkeypatch.setattr(cli_doctor, "_windows_git_bin", lambda: None)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") is None
        assert calls == []

    def test_git_line_non_windows_never_probes_git_for_windows(
        self, monkeypatch, tmp_path
    ) -> None:
        # POSIX resolver miss must not consult the Windows fallback: the
        # trusted-dirs decision is final there.
        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: None
        )
        monkeypatch.setattr(cli_doctor.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(
            cli_doctor,
            "_windows_git_bin",
            lambda: (_ for _ in ()).throw(AssertionError("probed on POSIX")),
        )
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") is None

    def test_windows_git_bin_returns_none_when_roots_empty(self, monkeypatch) -> None:
        # Fixed roots only — a miss returns None without consulting PATH or
        # the environment.
        monkeypatch.setattr(cli_doctor, "_WINDOWS_GIT_DIRS", ("Z:\\nonexistent\\Git\\cmd",))
        assert cli_doctor._windows_git_bin() is None


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
