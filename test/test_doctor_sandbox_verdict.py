"""`kirocrew doctor` Sandbox section — honest verdicts from an unconfined shell.

The probe (`sandbox.detect_backend`) answers for the PROBING process, not for
the gateway service. On a host that restricts unprivileged user namespaces the
kirocrew-userns AppArmor profile is applied by systemd to the service, and
``aa_change_onexec()`` into a named profile is not permitted for an ordinary
unconfined user — so from an interactive shell the probe fails with EPERM no
matter how healthy the service's sandbox is.

These tests pin the line the fix must hold: stop the false negative WITHOUT
starting a false positive.

* profile installed + service unit applies it + shell unconfined →
  "cannot be verified from this shell" (never "works"), and NOT an issue;
* the probe failing while THIS process is confined by the profile → broken;
* profile absent → broken, naming the install command.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import cli_doctor, sandbox
from kiro_crew.service import apparmor
from kiro_crew.service import linux as service_linux


def _arm_apparmor_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the probe signals read as the Ubuntu userns-restriction denial."""
    monkeypatch.setattr(sandbox, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sandbox, "unavailable_kind", lambda: "no_backend")
    monkeypatch.setattr(
        sandbox,
        "unavailable_reason",
        lambda: "unshare(CLONE_NEWNS) failed with errno 1 (EPERM)",
    )
    monkeypatch.setattr(
        sandbox, "unavailable_remedy", lambda: sandbox.REMEDY_APPARMOR_USERNS
    )


def _install_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile = tmp_path / apparmor.PROFILE_NAME
    profile.write_text("profile kirocrew-userns flags=(unconfined) {}\n", encoding="utf-8")
    monkeypatch.setattr(apparmor, "PROFILE_PATH", profile)


def _install_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, applies_profile: bool
) -> None:
    unit = tmp_path / "kirocrew.service"
    directive = (
        f"AppArmorProfile=-{apparmor.PROFILE_NAME}\n" if applies_profile else ""
    )
    unit.write_text(
        f"[Service]\n{directive}ExecStart=/usr/bin/true\n", encoding="utf-8"
    )
    monkeypatch.setattr(service_linux, "UNIT_PATH", unit)


class TestUnverifiableFromShell:
    """Profile installed, service confined, shell unconfined → no false negative."""

    def test_reports_unverifiable_not_broken_and_not_an_issue(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        _arm_apparmor_denial(monkeypatch)
        _install_profile(monkeypatch, tmp_path)
        _install_unit(monkeypatch, tmp_path, applies_profile=True)
        monkeypatch.setattr(cli_doctor, "_process_apparmor_confinement", lambda: "unconfined")

        issues: list[str] = []
        cli_doctor._doctor_sandbox(issues)

        out = capsys.readouterr().out
        assert "cannot be verified from this shell" in out
        assert "❌" not in out
        assert issues == []

    def test_does_not_claim_the_sandbox_works(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        # The no-false-positive half of the line: unverifiable must never render
        # as the success verdict a working probe gets.
        _arm_apparmor_denial(monkeypatch)
        _install_profile(monkeypatch, tmp_path)
        _install_unit(monkeypatch, tmp_path, applies_profile=True)
        monkeypatch.setattr(cli_doctor, "_process_apparmor_confinement", lambda: "unconfined")

        cli_doctor._doctor_sandbox([])

        out = capsys.readouterr().out
        assert "✅" not in out

    def test_names_the_systemd_run_verification_recipe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        _arm_apparmor_denial(monkeypatch)
        _install_profile(monkeypatch, tmp_path)
        _install_unit(monkeypatch, tmp_path, applies_profile=True)
        monkeypatch.setattr(cli_doctor, "_process_apparmor_confinement", lambda: "unconfined")

        cli_doctor._doctor_sandbox([])

        out = capsys.readouterr().out
        assert "systemd-run" in out
        assert f"AppArmorProfile=-{apparmor.PROFILE_NAME}" in out


class TestGenuinelyBroken:
    """A real fault must still read as broken — no swing to false positives."""

    def test_probe_failure_while_confined_by_the_profile_is_broken(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        # The confined context is the one place the probe SHOULD succeed; a
        # failure there is a verdict about the sandbox, not the vantage point.
        _arm_apparmor_denial(monkeypatch)
        _install_profile(monkeypatch, tmp_path)
        _install_unit(monkeypatch, tmp_path, applies_profile=True)
        monkeypatch.setattr(
            cli_doctor,
            "_process_apparmor_confinement",
            lambda: f"{apparmor.PROFILE_NAME} (enforce)",
        )

        issues: list[str] = []
        cli_doctor._doctor_sandbox(issues)

        out = capsys.readouterr().out
        assert "❌" in out
        assert "cannot be verified" not in out
        assert issues, "a genuine fault must be counted as an issue"

    def test_profile_installed_but_nothing_applies_it_is_broken(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        # An inert profile is not a confined service: claiming "unverifiable"
        # here would be the false positive the fix must not introduce.
        _arm_apparmor_denial(monkeypatch)
        _install_profile(monkeypatch, tmp_path)
        _install_unit(monkeypatch, tmp_path, applies_profile=False)
        monkeypatch.setattr(cli_doctor, "_process_apparmor_confinement", lambda: "unconfined")

        issues: list[str] = []
        cli_doctor._doctor_sandbox(issues)

        out = capsys.readouterr().out
        assert "❌" in out
        assert "cannot be verified from this shell" not in out
        assert issues


class TestProfileAbsent:
    """A host with no profile installed at all must still be told so."""

    def test_reports_broken_and_names_the_install_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        _arm_apparmor_denial(monkeypatch)
        monkeypatch.setattr(apparmor, "PROFILE_PATH", tmp_path / "absent-profile")
        monkeypatch.setattr(cli_doctor, "_process_apparmor_confinement", lambda: "unconfined")

        issues: list[str] = []
        cli_doctor._doctor_sandbox(issues)

        out = capsys.readouterr().out
        assert "❌" in out
        assert "not installed" in out
        assert "kirocrew service install" in out
        assert "cannot be verified from this shell" not in out
        assert issues


class TestNonFaultStates:
    """States that are not a fault of this install stay out of the issue list."""

    def test_working_backend_is_a_plain_success(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(sandbox, "detect_backend", lambda config_mode="auto": "namespace")

        issues: list[str] = []
        cli_doctor._doctor_sandbox(issues)

        out = capsys.readouterr().out
        assert "✅ namespace" in out
        assert issues == []

    def test_transient_failure_is_not_an_issue(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(sandbox, "detect_backend", lambda config_mode="auto": "none")
        monkeypatch.setattr(sandbox, "unavailable_kind", lambda: "transient")
        monkeypatch.setattr(sandbox, "unavailable_reason", lambda: "fork failed with EAGAIN")

        issues: list[str] = []
        cli_doctor._doctor_sandbox(issues)

        out = capsys.readouterr().out
        assert "transiently" in out
        assert "❌" not in out
        assert issues == []


class TestUnitDirectiveParsing:
    """`_service_unit_applies_profile` reads the real directive forms."""

    def test_dashed_directive_matches(self, tmp_path: Path) -> None:
        unit = tmp_path / "u.service"
        unit.write_text(f"AppArmorProfile=-{apparmor.PROFILE_NAME}\n", encoding="utf-8")
        assert cli_doctor._service_unit_applies_profile(unit, apparmor.PROFILE_NAME)

    def test_dashless_directive_matches(self, tmp_path: Path) -> None:
        unit = tmp_path / "u.service"
        unit.write_text(f"AppArmorProfile={apparmor.PROFILE_NAME}\n", encoding="utf-8")
        assert cli_doctor._service_unit_applies_profile(unit, apparmor.PROFILE_NAME)

    def test_other_profile_does_not_match(self, tmp_path: Path) -> None:
        unit = tmp_path / "u.service"
        unit.write_text("AppArmorProfile=-something-else\n", encoding="utf-8")
        assert not cli_doctor._service_unit_applies_profile(unit, apparmor.PROFILE_NAME)

    def test_missing_unit_does_not_match(self, tmp_path: Path) -> None:
        assert not cli_doctor._service_unit_applies_profile(
            tmp_path / "missing.service", apparmor.PROFILE_NAME
        )
