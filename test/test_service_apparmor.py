"""Tests for the service's AppArmor profile.

Where the profile installs, its rendered shape, a fail-soft install that
validates before loading and verifies enforcement, and the ordering against
the unit start.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _clear_sudo_user,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
    durable_dir_fixture,
)


class TestAppArmorGate:
    """The profile must install ONLY where that mechanism is the one in play.

    Gating on the detected mechanism rather than the distro is deliberate:
    Ubuntu derivatives (Pop!_OS, Mint, Zorin, elementary) inherit the
    restriction and an ID check would miss them, while Debian 13 ships AppArmor
    *without* the restriction and must be left completely alone.
    """

    @staticmethod
    def _gate(
        monkeypatch,
        *,
        lsm="apparmor,capability",
        sysctl="1",
        parser="/usr/sbin/apparmor_parser",
        version=(5, 0),
    ):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "apparmor_is_active", lambda: "apparmor" in lsm)
        monkeypatch.setattr(aa, "userns_restricted", lambda: sysctl == "1")
        monkeypatch.setattr(aa, "parser_path", lambda: parser)
        monkeypatch.setattr(aa, "parser_version", lambda _p: version)
        return aa

    def test_skips_when_apparmor_is_not_an_active_lsm(self, monkeypatch):
        aa = self._gate(monkeypatch, lsm="selinux,capability")
        needed, reason = aa.should_install()
        assert needed is False
        assert "not an active LSM" in reason

    def test_skips_when_the_sysctl_is_not_one(self, monkeypatch):
        """Debian 13 has AppArmor loaded and is unaffected — the sysctl decides."""
        aa = self._gate(monkeypatch, sysctl="0")
        needed, reason = aa.should_install()
        assert needed is False
        assert "apparmor_restrict_unprivileged_userns" in reason

    def test_skips_when_parser_is_missing(self, monkeypatch):
        aa = self._gate(monkeypatch, parser=None)
        needed, reason = aa.should_install()
        assert needed is False
        assert "apparmor_parser is not installed" in reason

    def test_skips_when_parser_predates_the_userns_rule(self, monkeypatch):
        """The `userns,` rule needs AppArmor 4.x; on 3.x the profile would not compile."""
        aa = self._gate(monkeypatch, version=(3, 0))
        needed, reason = aa.should_install()
        assert needed is False
        assert "older than 4.x" in reason

    def test_proceeds_when_every_condition_holds(self, monkeypatch):
        aa = self._gate(monkeypatch)
        needed, reason = aa.should_install()
        assert needed is True
        assert "userns restricted" in reason

    def test_sysctl_absent_reads_as_unrestricted(self, monkeypatch, tmp_path):
        """An absent knob (Debian, older kernels) must not look like `1`."""
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "_SYSCTL_PATH", tmp_path / "nope")
        assert aa.userns_restricted() is False

    def test_lsm_read_failure_reads_as_inactive(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "_LSM_PATH", tmp_path / "nope")
        assert aa.apparmor_is_active() is False


class TestAppArmorProfileRendering:
    """The rendered profile's shape is load-bearing for security."""

    _EXEC = Path("/opt/kirocrew-venv/bin/kirocrew")

    def test_attaches_to_the_given_path_and_nothing_else(self):
        """The attachment is the whole point, and it must be exactly the
        validated launcher path — never the interpreter behind its shebang,
        which is a symlink to the system python: attaching there would grant
        unprivileged userns to EVERY Python process on the host.
        """
        from kiro_crew.service import apparmor as aa

        text = aa.render_profile("4.0", self._EXEC)

        decl = [ln for ln in text.splitlines() if ln.startswith(f"profile {aa.PROFILE_NAME}")]
        assert decl == [f'profile {aa.PROFILE_NAME} "{self._EXEC}" flags=(unconfined) {{']
        # And no interpreter path anywhere in the RULES (comments may explain why).
        body = text.split("{", 1)[1]
        assert "python" not in body
        assert "crew-venv" not in body

    def test_grants_only_userns(self):
        from kiro_crew.service import apparmor as aa

        body = aa.render_profile("4.0", self._EXEC).split("{", 1)[1]

        assert "userns," in body
        # No capability/file grants smuggled in alongside.
        assert "capability" not in body
        assert " mr," not in body

    def test_abi_line_matches_the_detected_abi(self):
        from kiro_crew.service import apparmor as aa

        assert "abi <abi/4.0>," in aa.render_profile("4.0", self._EXEC)
        assert "abi <abi/5.0>," in aa.render_profile("5.0", self._EXEC)

    def test_abi_line_is_omitted_when_none_is_available(self):
        """Declaring an abi file the host lacks makes the profile fail to load."""
        from kiro_crew.service import apparmor as aa

        assert "abi <" not in aa.render_profile(None, self._EXEC)

    def test_detect_abi_picks_the_highest_numeric_file(self, monkeypatch, tmp_path):
        """Ubuntu 25.10 ships parser 5.x but only abi/3.0 and abi/4.0 on disk."""
        from kiro_crew.service import apparmor as aa

        for name in ("3.0", "4.0", "4.0-ip", "kernel-5.4-vanilla"):
            (tmp_path / name).write_text("", encoding="utf-8")
        monkeypatch.setattr(aa, "_ABI_DIR", tmp_path)

        assert aa.detect_abi() == "4.0"

    def test_detect_abi_returns_none_without_any_numeric_file(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "_ABI_DIR", tmp_path / "missing")
        assert aa.detect_abi() is None

    def test_documents_that_removal_rebreaks_the_sandbox(self):
        """The file is the only record a future reader has — it must say why."""
        from kiro_crew.service import apparmor as aa

        text = aa.render_profile("4.0", self._EXEC)
        assert "Managed by Kiro Crew" in text
        assert "Removing this file" in text


class TestAppArmorInstall:
    """Install must be fail-soft, validate before loading, and verify enforcement."""

    # A stand-in launcher path for tests that exercise branches past exec-path
    # validation; tests that reach validation stub validate_exec_path to accept it.
    _EXEC = "/opt/kirocrew-venv/bin/kirocrew"

    @staticmethod
    def _writers():
        writes: list[tuple[str, str]] = []
        runs: list[tuple[str, ...]] = []

        def write(text, dest):
            writes.append((text, str(dest)))

        def run(*argv):
            runs.append(argv)

        return writes, runs, write, run

    @staticmethod
    def _accept_exec_path(monkeypatch):
        """Stub exec-path validation so a test can reach the branch it is about.

        Validation itself has its own tests (TestValidateExecPath); here it
        would otherwise refuse the stand-in path for not existing on disk.
        """
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(
            aa,
            "validate_exec_path",
            lambda raw, expected_uid=None: (Path(raw), ""),
        )

    def test_skips_cleanly_when_the_host_does_not_need_it(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (False, "no restriction here"))

        outcome = aa.install(write, run, lambda *_a: (0, ""), 1000, 1000, self._EXEC)

        assert outcome.changed is False
        assert outcome.ok is True  # a skip is not a failure
        assert writes == [] and runs == []

    def test_refuses_to_install_a_profile_that_does_not_compile(self, monkeypatch):
        """Loading a broken profile is how you get a service that will not start."""
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "parser_version", lambda _p: (5, 0))
        monkeypatch.setattr(aa, "detect_abi", lambda: "5.0")
        monkeypatch.setattr(aa, "validate", lambda _p, _t: (False, "syntax error at line 9"))
        self._accept_exec_path(monkeypatch)

        outcome = aa.install(write, run, lambda *_a: (0, ""), 1000, 1000, self._EXEC)

        assert outcome.ok is False
        assert outcome.changed is False
        assert "did NOT compile" in outcome.message
        assert writes == [] and runs == [], "must not touch the host after a failed validate"

    def test_a_sudo_failure_warns_and_never_raises(self, monkeypatch):
        """An install must never die because a hardening step failed."""
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "parser_version", lambda _p: (5, 0))
        monkeypatch.setattr(aa, "detect_abi", lambda: "5.0")
        monkeypatch.setattr(aa, "validate", lambda _p, _t: (True, ""))
        self._accept_exec_path(monkeypatch)

        def boom(*_a, **_k):
            raise RuntimeError("sudo: a password is required")

        outcome = aa.install(boom, lambda *_a: None, lambda *_a: (0, ""), 1000, 1000, self._EXEC)

        assert outcome.ok is False
        assert outcome.changed is False
        assert "still start" in outcome.message
        assert "fail closed" in outcome.message

    def test_does_not_claim_success_when_enforcement_cannot_be_verified(self, monkeypatch):
        """A profile that loads but does not take effect is worse than none."""
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "parser_version", lambda _p: (5, 0))
        monkeypatch.setattr(aa, "detect_abi", lambda: "5.0")
        monkeypatch.setattr(aa, "validate", lambda _p, _t: (True, ""))
        monkeypatch.setattr(
            aa, "verify_enforcement", lambda _c, _u, _g: (False, "probe still fails")
        )
        self._accept_exec_path(monkeypatch)

        outcome = aa.install(write, run, lambda *_a: (0, ""), 1000, 1000, self._EXEC)

        assert outcome.changed is True  # the file WAS written
        assert outcome.ok is False
        assert "Not claiming success" in outcome.message

    def test_happy_path_validates_loads_then_verifies(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        order: list[str] = []
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "parser_version", lambda _p: (5, 0))
        monkeypatch.setattr(aa, "detect_abi", lambda: "5.0")
        monkeypatch.setattr(
            aa, "validate", lambda _p, _t: (order.append("validate"), (True, ""))[1]
        )
        monkeypatch.setattr(
            aa,
            "verify_enforcement",
            lambda _c, _u, _g: (order.append("verify"), (True, None))[1],
        )

        def tracked_write(text, dest):
            order.append("write")
            write(text, dest)

        def tracked_run(*argv):
            order.append("load")
            run(*argv)

        self._accept_exec_path(monkeypatch)

        outcome = aa.install(
            tracked_write, tracked_run, lambda *_a: (0, ""), 1000, 1000, self._EXEC
        )

        assert outcome.ok is True and outcome.changed is True
        assert str(aa.PROFILE_PATH) in outcome.message
        # Validate BEFORE writing, load before verifying.
        assert order == ["validate", "write", "load", "verify"]
        assert writes[0][1] == str(aa.PROFILE_PATH)
        assert runs[0][1:] == ("-r", "-W", str(aa.PROFILE_PATH))

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="validate_exec_path refuses Windows paths outright (backslash is an "
        "AppArmor glob metachar); the service profile is Linux-only",
    )
    def test_exec_path_attaches_the_profile_to_the_resolved_launcher(
        self, monkeypatch, durable_dir
    ):
        """A valid ``exec_path`` makes the WRITTEN profile text carry an
        attachment to the resolved script, not just a bare named profile.

        ``durable_dir`` (not raw ``tmp_path``): on Linux CI the pytest temp dir
        lives under ``/tmp``, which the prefix denylist and the mode walk both
        refuse — correctly, and each refusal has its own test. This test is
        about the RENDERED attachment, so those rules are neutralised.
        """
        from kiro_crew.service import apparmor as aa

        launcher = durable_dir / "kirocrew"
        launcher.write_text("#!/bin/sh\n")
        os.chmod(launcher, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- a launcher fixture must carry the exec bit a real venv launcher has; the file lives in a test-owned tmp dir.  # noqa: E501  # fmt: skip
        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "parser_version", lambda _p: (5, 0))
        monkeypatch.setattr(aa, "detect_abi", lambda: None)
        monkeypatch.setattr(aa, "validate", lambda _p, _t: (True, ""))
        monkeypatch.setattr(aa, "verify_enforcement", lambda _c, _u, _g: (True, None))
        expected_uid = launcher.stat().st_uid

        outcome = aa.install(
            write,
            run,
            lambda *_a: (0, ""),
            1000,
            1000,
            exec_path=str(launcher),
            expected_uid=expected_uid,
        )

        assert outcome.ok is True and outcome.changed is True
        assert str(launcher.resolve()) in outcome.message
        written_text = writes[0][0]
        assert f'profile {aa.PROFILE_NAME} "{launcher.resolve()}"' in written_text

    def test_an_unresolvable_exec_path_is_a_clean_non_fatal_skip(self, monkeypatch):
        """A launcher path that fails validation must not write anything, and
        must not be confused with a compile failure or a sudo failure — it is
        its own named case with its own message."""
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "parser_version", lambda _p: (5, 0))

        outcome = aa.install(
            write,
            run,
            lambda *_a: (0, ""),
            1000,
            1000,
            exec_path="/nonexistent/path/kirocrew",
        )

        assert outcome.ok is False
        assert outcome.changed is False
        assert "AppArmor profile not installed" in outcome.message
        assert writes == [] and runs == []

    def test_uninstall_is_a_noop_when_no_profile_is_present(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        _w, runs, _write, run = self._writers()
        monkeypatch.setattr(aa, "PROFILE_PATH", tmp_path / "absent")

        outcome = aa.uninstall(run)

        assert outcome.changed is False
        assert runs == []

    def test_uninstall_unloads_then_removes(self, monkeypatch, tmp_path):
        """Whatever removes the service removes the grant — no orphaned profile."""
        from kiro_crew.service import apparmor as aa

        profile = tmp_path / aa.PROFILE_NAME
        profile.write_text("profile", encoding="utf-8")
        _w, runs, _write, run = self._writers()
        monkeypatch.setattr(aa, "PROFILE_PATH", profile)
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")

        outcome = aa.uninstall(run)

        assert outcome.changed is True
        assert runs[0][1:] == ("-R", str(profile))
        assert runs[1] == ("rm", "-f", str(profile))


class TestAppArmorUnitDirective:
    """The retired ``AppArmorProfile=`` directive must never reappear in the unit.

    The profile is attached by path; when both mechanisms are present,
    systemd's ``change_onexec`` silently wins and defeats the path attachment.
    """

    def test_no_directive_by_default(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        gid = MagicMock(returncode=0, stdout="tester\n", stderr="")
        with (
            patch("kiro_crew.service.common.shutil.which", return_value="/usr/bin/kirocrew"),
            patch("kiro_crew.service.linux.subprocess.run", return_value=gid),
        ):
            unit = svc_linux.render_unit()

        assert "AppArmorProfile" not in unit

    def test_install_never_writes_the_directive_even_when_the_host_needs_a_profile(
        self, monkeypatch
    ):
        """The unit ``linux.install()`` writes must never carry the
        directive — a unit written WITH it silently defeats the path-attached
        profile it installs (systemd's change_onexec wins over the kernel's
        automatic path attachment). Asserted end-to-end through ``install()``,
        not just on ``render_unit`` in isolation, so a regression that starts
        threading a profile name back into the written unit is caught."""
        from kiro_crew.service import apparmor as aa
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux, "_current_user", lambda: "tester")
        monkeypatch.setattr(svc_linux, "_current_uid", lambda _u: 1000)
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(
            svc_linux, "install_apparmor_profile", lambda _uid: aa.ProfileOutcome(True, "ok")
        )
        monkeypatch.setattr(svc_linux, "_seed_env_file", lambda: None)
        written: list[str] = []
        monkeypatch.setattr(
            svc_linux,
            "_write_unit_via_sudo",
            lambda contents: (written.append(contents), MagicMock(returncode=0))[1],
        )
        ok = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(svc_linux, "_systemctl", lambda *a, **k: ok)

        svc_linux.install()

        assert written, "render_unit's output was never written"
        assert "AppArmorProfile" not in written[0]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="install_apparmor_profile() calls os.getuid(), absent on Windows; "
    "the systemd service path is Linux-only",
)
class TestInstallApparmorProfileAttachesToTheLauncher:
    """The service caller must hand ``apparmor.install`` the launcher
    path and the SERVICE account's uid, not the installer process's own uid."""

    def test_passes_kirocrew_bin_as_exec_path_and_threads_expected_uid(self, monkeypatch):
        from kiro_crew.service import apparmor as aa
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux, "kirocrew_bin", lambda: "/opt/kirocrew-venv/bin/kirocrew")
        calls = []

        def fake_install(*args, **kwargs):
            calls.append((args, kwargs))
            return aa.ProfileOutcome(True, "ok")

        monkeypatch.setattr(aa, "install", fake_install)

        outcome = svc_linux.install_apparmor_profile(4242)

        assert outcome.ok is True
        assert len(calls) == 1
        _args, kwargs = calls[0]
        assert kwargs["exec_path"] == "/opt/kirocrew-venv/bin/kirocrew"
        assert kwargs["expected_uid"] == 4242

    def test_an_unresolved_service_account_skips_the_install(self, monkeypatch):
        """``_current_uid`` returns None on a lookup failure; the install must
        SKIP rather than forward None: ``_substitutable_by_others`` reads None
        as "check against the calling process's uid", which under ``sudo`` is
        root — a root-owned shared launcher would then pass the ownership
        check and the userns grant would extend to every account on the host
        (GPT review finding on this PR)."""
        from kiro_crew.service import apparmor as aa
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux, "kirocrew_bin", lambda: "/opt/kirocrew-venv/bin/kirocrew")
        calls = []
        monkeypatch.setattr(
            aa, "install", lambda *a, **kw: (calls.append(kw), aa.ProfileOutcome(True, "ok"))[1]
        )

        outcome = svc_linux.install_apparmor_profile(None)

        assert calls == [], "apparmor.install must not run without a resolved service uid"
        assert outcome.changed is False
        assert outcome.ok is False
        assert "could not be resolved" in outcome.message
        assert "re-run" in outcome.message.lower()


class TestAppArmorNeverFailsTheInstall:
    """A hardening step must not be able to turn a working install into a failure.

    Every other step in ``linux.install()`` is fail-hard; this one deliberately is
    not. A gateway running without the profile is the pre-existing status quo,
    whereas aborting the install because a profile could not be loaded would be a
    regression that leaves the user with no service at all.
    """

    @staticmethod
    def _patched(monkeypatch, outcome):
        from kiro_crew.service import controller
        from kiro_crew.service.common import Platform

        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(controller.linux, "install", lambda: outcome)
        return controller

    def test_install_still_succeeds_when_the_profile_fails(self, monkeypatch, capsys):
        from kiro_crew.service.apparmor import ProfileOutcome

        controller = self._patched(
            monkeypatch,
            ProfileOutcome(False, "AppArmor profile could not be installed (boom)", ok=False),
        )

        rc = controller.install_service()

        assert rc == 0, "a failed hardening step must not fail the service install"
        out = capsys.readouterr().out
        assert "kirocrew service installed and started" in out
        assert "⚠️" in out, "the failure must still be surfaced, not swallowed"
        assert "could not be installed" in out

    def test_install_reports_the_profile_on_success(self, monkeypatch, capsys):
        from kiro_crew.service.apparmor import ProfileOutcome

        controller = self._patched(
            monkeypatch, ProfileOutcome(True, "AppArmor profile installed at /etc/apparmor.d/x")
        )

        rc = controller.install_service()

        out = capsys.readouterr().out
        assert rc == 0
        assert "AppArmor profile installed at" in out
        assert "⚠️" not in out

    def test_a_silent_skip_prints_nothing_extra(self, monkeypatch, capsys):
        """On Debian/Arch/RHEL the step must be invisible, not chatty."""
        from kiro_crew.service.apparmor import ProfileOutcome

        controller = self._patched(monkeypatch, ProfileOutcome(False, ""))

        rc = controller.install_service()

        out = capsys.readouterr().out
        assert rc == 0
        assert "AppArmor" not in out
        assert "⚠️" not in out

    def test_uninstall_removes_the_profile_and_still_reports_success(self, monkeypatch, capsys):
        from kiro_crew.service import controller
        from kiro_crew.service.apparmor import ProfileOutcome
        from kiro_crew.service.common import Platform

        removed: list[bool] = []

        def remove():
            removed.append(True)
            return ProfileOutcome(True, "AppArmor profile removed from /etc/apparmor.d/x")

        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(
            controller.linux,
            "uninstall",
            lambda: controller.linux.UninstallReport("removed (/etc/x.service)", "not installed"),
        )
        monkeypatch.setattr(controller.linux, "remove_apparmor_profile", remove)

        rc = controller.uninstall_service()

        assert rc == 0
        assert removed == [True], "uninstall must not leave an orphaned userns grant"
        assert "AppArmor profile removed" in capsys.readouterr().out


class TestEnforcementVerificationIsSafeAndFaithful:
    """Verification must be privileged enough to work, and safe enough to trust.

    Three properties, each of which was a real bug or a real vulnerability:
    it needs privilege to ENTER the profile (bare aa-exec silently execs
    unconfined and yields a false negative); it must not execute anything
    user-writable as root (the venv interpreter is user-writable, so running it
    under sudo is a local privilege escalation); and the probe itself must run
    UNPRIVILEGED or it proves nothing, since root may create namespaces
    regardless of the restriction.
    """

    def test_never_spawns_unprivileged_and_drops_back_to_the_caller(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            aa.subprocess,
            "run",
            lambda *_a, **_k: pytest.fail("verification must not spawn unprivileged"),
        )
        monkeypatch.setattr(aa, "_resolve_trusted", lambda name: f"/usr/bin/{name}")

        def sudo_capture(*argv):
            calls.append(argv)
            return (0, "")

        ok, problem = aa.verify_enforcement(sudo_capture, 1000, 1000)

        assert ok is True and problem is None
        argv = calls[0]
        assert argv[:3] == ("/usr/bin/aa-exec", "-p", aa.PROFILE_NAME)
        # Privilege is dropped back to the invoking user INSIDE the profile.
        assert "/usr/bin/setpriv" in argv
        assert "--reuid=1000" in argv and "--regid=1000" in argv
        assert "--clear-groups" in argv

    def test_uses_a_trusted_python_never_the_user_writable_venv(self, monkeypatch):
        """sys.executable is user-writable; running it under sudo would be an LPE."""
        import sys as _sys

        from kiro_crew.service import apparmor as aa

        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(aa, "_resolve_trusted", lambda name: f"/usr/bin/{name}")
        aa.verify_enforcement(lambda *argv: (calls.append(argv), (0, ""))[1], 1000, 1000)

        argv = calls[0]
        assert "/usr/bin/python3" in argv
        assert _sys.executable not in argv
        # And the payload must not import our own (user-writable) package.
        assert "kiro_crew" not in " ".join(argv)

    def test_runs_the_trusted_python_in_isolated_mode(self, monkeypatch):
        """Under `sudo kirocrew service install` this python is root."""
        from kiro_crew.service import apparmor as aa

        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(aa, "_resolve_trusted", lambda name: f"/usr/bin/{name}")
        aa.verify_enforcement(lambda *argv: (calls.append(argv), (0, ""))[1], 1000, 1000)

        argv = calls[0]
        at = argv.index("/usr/bin/python3")
        assert argv[at + 1 : at + 4] == ("-I", "-c", aa._VERIFY_SNIPPET)

    def test_privileged_capture_starts_in_root_without_python_env(self, monkeypatch):
        """Never the operator's cwd, never an inherited PYTHON* variable."""
        from kiro_crew.service import linux

        seen: dict = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs, argv=argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(linux.subprocess, "run", fake_run)
        monkeypatch.setattr(linux, "_privilege_prefix", lambda: ["sudo"])
        monkeypatch.setenv("PYTHONPATH", "/tmp/planted")
        monkeypatch.setenv("PYTHONSTARTUP", "/tmp/planted/s.py")
        monkeypatch.setenv("KC_8414_KEEP", "kept")

        assert linux._sudo_capture("aa-exec", "-p", "x") == (0, "")

        assert seen["argv"] == ["sudo", "aa-exec", "-p", "x"]
        assert seen["cwd"] == "/"
        assert not [k for k in seen["env"] if k.startswith("PYTHON")]
        assert seen["env"]["KC_8414_KEEP"] == "kept"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX service install path")
    @pytest.mark.parametrize("via", ["bare-runner", "service-capture"])
    def test_code_planted_in_cwd_or_pythonpath_never_runs(self, monkeypatch, tmp_path, via):
        """End to end: `cd /tmp && sudo kirocrew service install` with a
        planted ctypes.py / sitecustomize.py. The probe's own first import must
        resolve to the stdlib, and no planted file may run.

        `bare-runner` runs the built argv from the planted cwd with PYTHONPATH
        set, so only `-I` stands between it and the plant. `service-capture`
        goes through the real privileged-capture helper (sudo stubbed out).
        """
        from kiro_crew.service import apparmor as aa
        from kiro_crew.service import linux

        planted = tmp_path / "planted"
        planted.mkdir()
        marker = tmp_path / "PLANTED_RAN"
        hook = f"open({str(marker)!r}, 'a').write(__name__ + '\\n')\n"
        (planted / "ctypes.py").write_text(hook, encoding="utf-8")
        (planted / "sitecustomize.py").write_text(hook, encoding="utf-8")
        (planted / "usercustomize.py").write_text(hook, encoding="utf-8")

        python = sys.executable
        monkeypatch.setattr(
            aa, "_resolve_trusted", lambda name: python if name == "python3" else f"/usr/bin/{name}"
        )
        # The snippet's real first line (its import), then report what it got.
        first_line = aa._VERIFY_SNIPPET.splitlines()[0]
        assert first_line.startswith("import ctypes")
        monkeypatch.setattr(
            aa,
            "_VERIFY_SNIPPET",
            first_line + "\nprint(ctypes.__file__)\n",
        )
        monkeypatch.chdir(planted)
        monkeypatch.setenv("PYTHONPATH", str(planted))
        monkeypatch.setenv("PYTHONUSERBASE", str(planted))

        import subprocess as _sp

        def python_part(argv):
            # Drop the aa-exec / setpriv wrappers: they need root and a loaded
            # profile; the interpreter and its flags are what is under test.
            return argv[argv.index(python) :]

        if via == "bare-runner":

            def sudo_capture(*argv):
                res = _sp.run(
                    python_part(argv),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                return (res.returncode, res.stderr + res.stdout)

        else:
            monkeypatch.setattr(linux, "_privilege_prefix", lambda: [])

            def sudo_capture(*argv):
                return linux._sudo_capture(*python_part(argv))

        ok, problem = aa.verify_enforcement(sudo_capture, os.getuid(), os.getgid())

        assert ok is True, problem
        assert not marker.exists(), marker.read_text(encoding="utf-8")

    def test_a_missing_trusted_tool_is_inconclusive_not_a_failure_claim(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(
            aa, "_resolve_trusted", lambda name: None if name == "setpriv" else "/usr/bin/x"
        )

        ok, problem = aa.verify_enforcement(lambda *_a: (0, ""), 1000, 1000)

        assert ok is False
        assert "could not verify" in problem
        assert "setpriv" in problem

    def test_a_failing_probe_inside_the_profile_is_surfaced(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "_resolve_trusted", lambda name: f"/usr/bin/{name}")

        ok, problem = aa.verify_enforcement(
            lambda *_a: (1, "unshare(CLONE_NEWNS) failed with errno 1 (EPERM)"), 1000, 1000
        )

        assert ok is False
        assert "CLONE_NEWNS" in problem


class TestTrustedToolResolution:
    """Anything handed to sudo must not be resolvable through the user's $PATH."""

    def test_rejects_a_binary_outside_the_trusted_dirs(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        fake = tmp_path / "apparmor_parser"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        fake.chmod(0o755)
        # A PATH-based lookup would find this; a trusted-dir lookup must not.
        monkeypatch.setenv("PATH", f"{tmp_path}:/usr/sbin:/usr/bin")
        monkeypatch.setattr(aa, "_TRUSTED_BIN_DIRS", (str(tmp_path),))

        # Present but user-owned -> refused.
        assert aa._resolve_trusted("apparmor_parser") is None

    def test_rejects_a_group_or_world_writable_binary(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        target = tmp_path / "aa-exec"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o777)
        monkeypatch.setattr(aa, "_TRUSTED_BIN_DIRS", (str(tmp_path),))

        assert aa._resolve_trusted("aa-exec") is None

    def test_missing_binary_returns_none(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "_TRUSTED_BIN_DIRS", (str(tmp_path),))
        assert aa._resolve_trusted("nope") is None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="asserts POSIX ownership/permission semantics on a real system binary; "
        "Windows has neither /bin/sh nor a root uid, and the AppArmor path is Linux-only",
    )
    @pytest.mark.skipif(
        os.path.exists("/bin/sh") and os.stat("/bin/sh").st_uid != 0,
        reason="system binaries are not root-owned on this host",
    )
    def test_resolves_a_real_root_owned_system_binary(self):
        """Against the real filesystem, not a fixture: /bin/sh must resolve."""
        from kiro_crew.service import apparmor as aa

        resolved = aa._resolve_trusted("sh")
        assert resolved is not None and resolved.startswith("/")


class TestProfileLoadsBeforeTheServiceStarts:
    """Ordering is load-bearing: the directive only applies at unit START.

    Loading the profile after `systemctl restart` leaves the FIRST gateway process
    unprofiled, so every agent spawn fails closed until someone restarts again —
    which is exactly the state this feature exists to prevent.
    """

    def test_profile_is_installed_before_daemon_reload_and_restart(self, monkeypatch):
        from kiro_crew.service import apparmor as aa
        from kiro_crew.service import linux as svc_linux

        order: list[str] = []
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux, "_current_user", lambda: "tester")
        monkeypatch.setattr(svc_linux, "render_unit", lambda *_a: "unit")
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(
            svc_linux,
            "_write_unit_via_sudo",
            lambda _c: (order.append("write-unit"), MagicMock(returncode=0))[1],
        )
        monkeypatch.setattr(
            svc_linux,
            "install_apparmor_profile",
            lambda _uid: (order.append("load-profile"), aa.ProfileOutcome(True, "installed"))[1],
        )
        monkeypatch.setattr(
            svc_linux,
            "_systemctl",
            lambda *args: (order.append(args[0]), MagicMock(returncode=0))[1],
        )
        # _seed_env_file calls _sudo_run("test", "-e", ...) which blocks for a
        # sudo password on non-Linux hosts (macOS). The function is best-effort
        # and not under test here — patch it to a no-op.
        monkeypatch.setattr(svc_linux, "_seed_env_file", lambda: None)

        outcome = svc_linux.install()

        assert outcome.ok is True
        assert order == ["write-unit", "load-profile", "daemon-reload", "enable", "restart"], order
        # The profile must be loaded strictly before the unit is started.
        assert order.index("load-profile") < order.index("restart")


class TestAppArmorProfileValidation:
    """``validate`` parses a profile WITHOUT loading it.

    Loading needs root and a wrong profile that loads is a confined gateway that
    cannot start; parsing first is what turns that into a message. ``--skip-cache``
    is load-bearing: writing ``/var/cache/apparmor`` needs root and this runs
    before any privileged step.
    """

    @staticmethod
    def _fake_run(monkeypatch, *, returncode=0, stdout="", stderr="", raises=None):
        from kiro_crew.service import apparmor as aa

        seen: dict = {}

        def _run(argv, **kw):
            seen["argv"] = list(argv)
            seen["kw"] = kw
            if raises is not None:
                raise raises
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(aa.subprocess, "run", _run)
        return seen

    def test_a_clean_parse_reports_ok(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        seen = self._fake_run(monkeypatch)
        ok, detail = aa.validate("/usr/sbin/apparmor_parser", "profile x {}")

        assert (ok, detail) == (True, "")
        assert seen["argv"][:3] == ["/usr/sbin/apparmor_parser", "-Q", "--skip-cache"]

    def test_the_profile_text_reaches_the_parser_and_the_temp_file_is_removed(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        seen = self._fake_run(monkeypatch)
        written: dict = {}

        def _capture(argv, **kw):
            written["text"] = Path(argv[-1]).read_text(encoding="utf-8")
            written["path"] = argv[-1]
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(aa.subprocess, "run", _capture)
        aa.validate("/usr/sbin/apparmor_parser", "profile marker {}")

        assert written["text"] == "profile marker {}"
        # The temp file carries a profile, not a secret, but leaving one per
        # install attempt is still a leak of /tmp entries.
        assert not Path(written["path"]).exists()
        assert seen == {}

    def test_a_parse_failure_returns_the_parsers_own_words(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        self._fake_run(monkeypatch, returncode=1, stderr="  syntax error at line 3  ")
        assert aa.validate("/usr/sbin/apparmor_parser", "bad") == (
            False,
            "syntax error at line 3",
        )

    def test_stdout_is_used_when_stderr_is_empty(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        self._fake_run(monkeypatch, returncode=1, stdout="told you so")
        assert aa.validate("/usr/sbin/apparmor_parser", "bad") == (False, "told you so")

    def test_a_missing_parser_is_a_failure_not_a_crash(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        self._fake_run(monkeypatch, raises=OSError("no such file"))
        ok, detail = aa.validate("/usr/sbin/apparmor_parser", "profile x {}")

        assert ok is False
        assert "no such file" in detail

    def test_a_parser_timeout_is_a_failure_not_a_crash(self, monkeypatch):
        import subprocess

        from kiro_crew.service import apparmor as aa

        self._fake_run(monkeypatch, raises=subprocess.TimeoutExpired("p", 30))
        assert aa.validate("/usr/sbin/apparmor_parser", "profile x {}")[0] is False
