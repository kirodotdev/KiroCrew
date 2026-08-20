"""`kirocrew doctor` Sandbox section — honest verdicts from an unconfined shell.

The probe (`sandbox.detect_backend`) answers for the PROBING process, not for
the gateway service. On a host that restricts unprivileged user namespaces the
kirocrew-userns AppArmor profile is ATTACHED to the resolved kirocrew launcher
script (#3463 — replacing an earlier, unattached design applied purely via a
systemd `AppArmorProfile=` unit directive, which was found not to actually
confine the gateway's sandbox probe). A `kirocrew doctor` invocation that did
not go through that exact attached path is unconfined regardless of how
healthy the service's own sandbox is.

These tests pin the line the fix must hold: stop the false negative WITHOUT
starting a false positive.

* profile installed + attached to the CURRENT launcher resolution + shell
  unconfined → "cannot be verified from this shell" (never "works"), and NOT
  an issue;
* the probe failing while THIS process is confined by the profile → broken;
* profile absent → broken, naming the install command;
* profile installed but attached to a STALE or different path than the one
  this host currently resolves → broken, same as "not attached at all".
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


def _install_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, attached_to: Path | None
) -> None:
    """Write the service profile, optionally ATTACHED to *attached_to* (#3463)."""
    profile = tmp_path / apparmor.PROFILE_NAME
    attachment = f' "{attached_to}"' if attached_to is not None else ""
    profile.write_text(
        f"profile {apparmor.PROFILE_NAME}{attachment} flags=(unconfined) {{}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(apparmor, "PROFILE_PATH", profile)


def _resolve_launcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Make ``kirocrew_bin()`` resolve to a real file under *tmp_path* and
    return its resolved path, for both attaching the profile to and comparing
    against in ``_service_profile_applies``."""
    launcher = tmp_path / "kirocrew"
    launcher.write_text("#!/bin/sh\n")
    monkeypatch.setattr(service_linux, "kirocrew_bin", lambda: str(launcher))
    return launcher.resolve()


class TestUnverifiableFromShell:
    """Profile installed, attached to the current launcher, shell unconfined →
    no false negative."""

    def test_reports_unverifiable_not_broken_and_not_an_issue(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        _arm_apparmor_denial(monkeypatch)
        launcher = _resolve_launcher(monkeypatch, tmp_path)
        _install_profile(monkeypatch, tmp_path, attached_to=launcher)
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
        launcher = _resolve_launcher(monkeypatch, tmp_path)
        _install_profile(monkeypatch, tmp_path, attached_to=launcher)
        monkeypatch.setattr(cli_doctor, "_process_apparmor_confinement", lambda: "unconfined")

        cli_doctor._doctor_sandbox([])

        out = capsys.readouterr().out
        assert "✅" not in out

    def test_names_the_systemd_run_verification_recipe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        _arm_apparmor_denial(monkeypatch)
        launcher = _resolve_launcher(monkeypatch, tmp_path)
        _install_profile(monkeypatch, tmp_path, attached_to=launcher)
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
        launcher = _resolve_launcher(monkeypatch, tmp_path)
        _install_profile(monkeypatch, tmp_path, attached_to=launcher)
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

    def test_profile_installed_but_unattached_is_broken(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        # An inert (unattached) profile is not a confined service: claiming
        # "unverifiable" here would be the false positive the fix must not
        # introduce.
        _arm_apparmor_denial(monkeypatch)
        _resolve_launcher(monkeypatch, tmp_path)
        _install_profile(monkeypatch, tmp_path, attached_to=None)
        monkeypatch.setattr(cli_doctor, "_process_apparmor_confinement", lambda: "unconfined")

        issues: list[str] = []
        cli_doctor._doctor_sandbox(issues)

        out = capsys.readouterr().out
        assert "❌" in out
        assert "cannot be verified from this shell" not in out
        assert issues

    def test_profile_attached_to_a_stale_path_is_broken(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        """#3463: a moved/rebuilt venv silently stops the attachment matching —
        the kernel reports no error, so this must be caught by comparing
        against the CURRENTLY resolved path, not just "is there an attachment
        clause at all"."""
        _arm_apparmor_denial(monkeypatch)
        _resolve_launcher(monkeypatch, tmp_path)
        stale = tmp_path / "old-launcher-location"
        stale.write_text("#!/bin/sh\n")
        _install_profile(monkeypatch, tmp_path, attached_to=stale.resolve())
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


class TestServiceProfileApplies:
    """`_service_profile_applies` reads the profile's own attachment clause and
    compares it against the CURRENTLY resolved launcher path (#3463) — it no
    longer reads the systemd unit for a retired `AppArmorProfile=` directive."""

    def test_matching_attachment_applies(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        launcher = _resolve_launcher(monkeypatch, tmp_path)
        profile = tmp_path / "profile"
        profile.write_text(
            f'profile {apparmor.PROFILE_NAME} "{launcher}" flags=(unconfined) {{}}\n',
            encoding="utf-8",
        )
        assert cli_doctor._service_profile_applies(profile, apparmor.PROFILE_NAME)

    def test_stale_attachment_does_not_apply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _resolve_launcher(monkeypatch, tmp_path)
        profile = tmp_path / "profile"
        profile.write_text(
            f'profile {apparmor.PROFILE_NAME} "/old/stale/path" flags=(unconfined) {{}}\n',
            encoding="utf-8",
        )
        assert not cli_doctor._service_profile_applies(profile, apparmor.PROFILE_NAME)

    def test_unattached_profile_does_not_apply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _resolve_launcher(monkeypatch, tmp_path)
        profile = tmp_path / "profile"
        profile.write_text(
            f"profile {apparmor.PROFILE_NAME} flags=(unconfined) {{}}\n", encoding="utf-8"
        )
        assert not cli_doctor._service_profile_applies(profile, apparmor.PROFILE_NAME)

    def test_missing_profile_does_not_apply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _resolve_launcher(monkeypatch, tmp_path)
        assert not cli_doctor._service_profile_applies(
            tmp_path / "missing-profile", apparmor.PROFILE_NAME
        )

    def test_an_unresolvable_kirocrew_bin_does_not_apply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``kirocrew_bin()`` can point at a path that no longer exists (an
        uninstalled or moved venv); that must read as "not applied", not raise."""
        monkeypatch.setattr(service_linux, "kirocrew_bin", lambda: "/nonexistent/kirocrew")
        profile = tmp_path / "profile"
        profile.write_text(
            f'profile {apparmor.PROFILE_NAME} "/nonexistent/kirocrew" flags=(unconfined) {{}}\n',
            encoding="utf-8",
        )
        assert not cli_doctor._service_profile_applies(profile, apparmor.PROFILE_NAME)
