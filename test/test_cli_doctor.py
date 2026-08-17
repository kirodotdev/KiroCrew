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

    def test_legacy_present_says_will_retry(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # A leftover ~/.kirocrew (a live gateway held it, the delete failed, or
        # this is the first cold start) is always transient now — migration
        # force-overwrites and deletes it on the next start, so "will retry" is
        # correct in every case (there is no more divergence-abort state).
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
        assert "will retry on next cold start" in out

    def test_legacy_present_under_valid_override_says_ignored(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Under a VALID KIROCREW_HOME override migration is bypassed on every
        # start, so a leftover legacy is NOT going to be migrated — the doctor
        # must not claim "will retry" (GPT 5.6 MEDIUM).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "override"))
        home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        home.mkdir(parents=True)
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "IGNORED" in out and "override active" in out
        assert "will retry on next cold start" not in out

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

    def test_marker_present_nonempty_legacy_renders_conflict(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Marker present + a NON-EMPTY legacy dir → a genuine conflict: the
        # legacy is resurrection debris, NOT a pending migration. The doctor must
        # render the conflict (⚠ / NOT used) and never claim a retry (GPT 5.6
        # MEDIUM: pin the conflict-rendering branch so removing it fails a test).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        home.mkdir(parents=True)
        (home / cli_doctor.MIGRATION_MARKER_NAME).write_text("done\n", encoding="utf-8")
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()
        (legacy / "sessions.db").write_text("stale", encoding="utf-8")  # non-empty debris

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "conflict" in out and "NOT used" in out
        assert "will retry on next cold start" not in out

    def test_marker_present_empty_legacy_says_unused_not_retry(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Marker present + an EMPTY recreated legacy dir: migration already
        # completed and is marker-authoritative, so it will NEVER retry. The
        # doctor must call the dir UNUSED leftover, not claim a pending retry
        # (GPT 5.6 MEDIUM — the misleading "will retry" would persist forever).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        home.mkdir(parents=True)
        (home / cli_doctor.MIGRATION_MARKER_NAME).write_text("done\n", encoding="utf-8")
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()  # empty debris

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "UNUSED" in out and "migration already completed" in out
        assert "will retry on next cold start" not in out

    def test_no_legacy_stays_quiet(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # Fresh install / migration already completed: only the location line,
        # no leftover-legacy nag. There is no archive to report either way.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: tmp_path / ".kiro" / "crew")

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "Data Home" in out
        assert "legacy:" not in out
        assert "rollback copy" not in out
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
