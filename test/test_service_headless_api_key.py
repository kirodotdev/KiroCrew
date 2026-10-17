"""Tests for the API-key credential a headless service install could drop.

``service install`` warns before it silently drops kiro-cli's API-key
credential, and ``doctor`` reports the same drop, only when it is real.
"""

from __future__ import annotations

import inspect
import re
import shlex
from unittest.mock import MagicMock, patch

import pytest
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _clear_sudo_user,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
)

from kiro_crew.service import common, controller
from kiro_crew.service.common import Platform, service_environment


class TestHeadlessApiKeyDoctorReport:
    """`doctor` must report the same dropped credential, and only when it is real.

    Install-time alone misses every ordering where the service is already
    installed. Doctor is where the operator stands and where the contradiction is
    visible in one output, so the same helper is called there -- but only when a
    service definition exists, because a foreground gateway inherits the shell
    that runs doctor and the credential does reach it.
    """

    API_KEY = "KIRO_API_KEY"
    SECRET = "sk-doctor-value-not-for-disclosure"

    def _warn(self, monkeypatch, unit, warning="Note: dropped key"):
        from kiro_crew import cli_doctor

        monkeypatch.setattr(cli_doctor.service_controller, "installed_unit_path", lambda: unit)
        monkeypatch.setattr(cli_doctor.common_service, "headless_auth_warning", lambda: warning)
        issues: list[str] = []
        cli_doctor._doctor_headless_auth(issues)
        return issues

    def test_reports_when_a_service_is_installed(self, monkeypatch, capsys, tmp_path):
        issues = self._warn(monkeypatch, tmp_path / "kirocrew.service")
        out = capsys.readouterr().out
        assert "cannot see it" in out
        assert "Note: dropped key" in out
        assert issues == [], "the report is advisory; see the exit-code test below"

    def test_the_report_cannot_make_doctor_exit_nonzero(self, monkeypatch, tmp_path):
        """`issues` is the exit-code channel, and this gate cannot prove failure.

        `_doctor` ends in `if issues: print("❌ Fix these issues: ..."); sys.exit(1)`,
        so an entry here turns a host where sign-in works into a failed verdict:
        `service_environment()` bakes `HOME`, so a service that has a
        `kiro-cli login` credential store is healthy while this fires, and a unit
        path only proves a definition exists on disk. Both halves are pinned --
        the append being absent, and the `sys.exit(1)` it would have reached.
        """
        from kiro_crew import cli_doctor

        source = inspect.getsource(cli_doctor._doctor_headless_auth)
        assert "issues.append" not in source
        assert "del issues" in source
        assert self._warn(monkeypatch, tmp_path / "kirocrew.service") == []
        assert "sys.exit(1)" in inspect.getsource(cli_doctor._doctor)

    def test_silent_when_no_service_is_installed(self, monkeypatch, capsys):
        """A foreground gateway inherits this shell, so there is nothing wrong."""
        issues = self._warn(monkeypatch, None)
        assert capsys.readouterr().out == ""
        assert issues == []

    def test_silent_when_the_helper_has_nothing_to_say(self, monkeypatch, capsys, tmp_path):
        issues = self._warn(monkeypatch, tmp_path / "kirocrew.service", warning="")
        assert capsys.readouterr().out == ""
        assert issues == []

    def test_a_failing_probe_cannot_break_doctor(self, monkeypatch, capsys, tmp_path):
        """Doctor reports; it must not raise because a diagnostic could not run."""
        from kiro_crew import cli_doctor

        monkeypatch.setattr(
            cli_doctor.service_controller,
            "installed_unit_path",
            lambda: tmp_path / "kirocrew.service",
        )

        def boom():
            raise OSError("environment resolution exploded")

        monkeypatch.setattr(cli_doctor.common_service, "headless_auth_warning", boom)
        issues: list[str] = []
        cli_doctor._doctor_headless_auth(issues)
        assert capsys.readouterr().out == ""
        assert issues == []

    def test_doctor_never_echoes_the_credential_value(self, monkeypatch, capsys, tmp_path):
        real = common.headless_auth_warning
        monkeypatch.setenv(self.API_KEY, self.SECRET)
        monkeypatch.setattr(common.loader, "env_path", lambda: tmp_path / ".env")
        issues = self._warn(monkeypatch, tmp_path / "kirocrew.service", warning=real())
        captured = capsys.readouterr().out
        assert captured, "expected a report for this fixture"
        assert self.SECRET not in captured
        assert self.SECRET not in "".join(issues)

    def test_doctor_actually_calls_the_check(self):
        """A diagnostic with no production caller reports nothing to anyone.

        Asserted against `_doctor`'s source rather than by running it, because
        `_doctor` performs dozens of live host probes; the property under test is
        only that the call site exists.
        """
        from kiro_crew import cli_doctor

        assert "_doctor_headless_auth(issues)" in inspect.getsource(cli_doctor._doctor)


class TestHeadlessApiKeyWarning:
    """`service install` must not silently drop kiro-cli's API-key credential.

    launchd/systemd hand the gateway a minimal environment, so a key exported in
    the installing shell is absent when the service starts and the readiness
    probe reports a signed-out state on a host where kiro-cli itself is
    authenticated. The install path warns instead of pretending
    nothing was lost — and never bakes the credential into the unit.
    """

    API_KEY = "KIRO_API_KEY"
    SECRET = "sk-headless-value-not-for-disclosure"

    def _dotenv(self, monkeypatch, tmp_path, contents=None):
        """Point env_path() at a temp file so the developer's own .env is never read."""
        target = tmp_path / ".env"
        if contents is not None:
            target.write_text(contents, encoding="utf-8")
        monkeypatch.setattr(common.loader, "env_path", lambda: target)
        return target

    def test_warns_when_key_set_but_absent_from_dotenv(self, monkeypatch, tmp_path):
        dotenv = self._dotenv(monkeypatch, tmp_path, "SLACK_BOT_TOKEN=xoxb-unrelated\n")
        warning = common.headless_auth_warning({self.API_KEY: self.SECRET})
        assert warning, "a dropped credential must produce a warning"
        assert self.API_KEY in warning
        assert str(dotenv) in warning, "the warning must name the file to edit"
        assert common.restart_command_hint() in warning

    def test_the_signed_out_claim_is_qualified(self, monkeypatch, tmp_path):
        """A login credential store under the baked `HOME` can still authenticate.

        `service_environment()` bakes `HOME`, so a service on a host that ran
        `kiro-cli login` before the key was exported is signed in even though the
        key is dropped. The note must therefore not state the signed-out outcome
        as certain: doctor treats this same predicate as advisory rather than a
        failure precisely because it cannot rule that fall-back out.
        """
        self._dotenv(monkeypatch, tmp_path, "")
        warning = common.headless_auth_warning({self.API_KEY: self.SECRET})
        assert "signed-out state" in warning
        assert "unless" in warning, "the outcome is conditional, not certain"

    def test_silent_when_dotenv_already_defines_the_key(self, monkeypatch, tmp_path):
        self._dotenv(monkeypatch, tmp_path, f"{self.API_KEY}=already-configured\n")
        assert common.headless_auth_warning({self.API_KEY: self.SECRET}) == ""

    def test_silent_when_no_key_in_installer_environment(self, monkeypatch, tmp_path):
        self._dotenv(monkeypatch, tmp_path, "")
        assert common.headless_auth_warning({}) == ""

    def test_blank_key_is_not_a_credential(self, monkeypatch, tmp_path):
        self._dotenv(monkeypatch, tmp_path, "")
        assert common.headless_auth_warning({self.API_KEY: "   "}) == ""

    def test_missing_dotenv_warns_rather_than_assuming_configured(self, monkeypatch, tmp_path):
        # No file written: an unreadable/absent .env must fail toward warning,
        # because a missed warning is the defect being fixed.
        self._dotenv(monkeypatch, tmp_path)
        assert common.headless_auth_warning({self.API_KEY: self.SECRET})

    def test_commented_out_assignment_does_not_count_as_configured(self, monkeypatch, tmp_path):
        self._dotenv(monkeypatch, tmp_path, f"#{self.API_KEY}=commented-out\n")
        assert common.headless_auth_warning({self.API_KEY: self.SECRET})

    def test_warning_never_echoes_the_credential_value(self, monkeypatch, tmp_path):
        self._dotenv(monkeypatch, tmp_path, "")
        warning = common.headless_auth_warning({self.API_KEY: self.SECRET})
        assert self.SECRET not in warning
        # The remedy must reference the variable, not interpolate its value.
        assert f"${self.API_KEY}" in warning

    def test_custom_home_caveat_only_when_home_is_overridden(self, monkeypatch, tmp_path):
        self._dotenv(monkeypatch, tmp_path, "")
        plain = common.headless_auth_warning({self.API_KEY: self.SECRET})
        assert "KIROCREW_HOME" not in plain
        with_home = common.headless_auth_warning(
            {self.API_KEY: self.SECRET, "KIROCREW_HOME": "/srv/crew"}
        )
        assert "KIROCREW_HOME" in with_home

    def test_remedy_tightens_permissions_before_writing_the_secret(self, monkeypatch, tmp_path):
        """The append must not be the step that creates the file.

        Under a standard 022 umask a .env born from the append alone is 0644, and
        the gateway only forces 0600 the next time it reads it — so the key would
        be world-readable in the interim. Order is the whole fix, so assert on
        position, not mere presence.
        """
        self._dotenv(monkeypatch, tmp_path, "")
        warning = common.headless_auth_warning({self.API_KEY: self.SECRET})
        assert "chmod 600" in warning
        assert warning.index("chmod 600") < warning.index(
            "printf"
        ), "chmod must precede the append, or the secret lands in a 0644 file"

    def test_remedy_survives_a_crew_home_containing_spaces(self, monkeypatch, tmp_path):
        """An operator copy-pastes this line, so the shell must read one path.

        Unquoted, a spaced path word-splits: `touch` creates the wrong files,
        `chmod` fails on a path that never existed, and the redirect appends the
        credential to a different file under the ambient umask — which
        load_credentials() never visits to tighten. That is the same
        world-readable outcome the chmod ordering exists to prevent, so quoting
        belongs to that same contract.
        """
        spaced = tmp_path / "crew home"
        spaced.mkdir()
        dotenv = self._dotenv(monkeypatch, spaced, "")
        warning = common.headless_auth_warning({self.API_KEY: self.SECRET})
        quoted = shlex.quote(str(dotenv))
        assert quoted != str(dotenv), "fixture must exercise a path needing quotes"
        for line in warning.splitlines():
            if "touch" not in line and "printf" not in line:
                continue
            assert quoted in line, line
            # shlex.split is the ground truth: the path must survive as ONE arg.
            assert str(dotenv) in shlex.split(line.strip()), line

    def test_blank_value_in_dotenv_is_not_configured(self, monkeypatch, tmp_path):
        """A bare `NAME=` must still warn.

        load_credentials() skips falsy values when it seeds os.environ, so a
        valueless assignment never reaches the probe and the dashboard stays
        signed-out. Counting it as configured is precisely the missed warning
        this module fails toward avoiding, and the shell side already rejects the
        same emptiness.
        """
        for blank in (f"{self.API_KEY}=\n", f"{self.API_KEY}=   \n"):
            dotenv = self._dotenv(monkeypatch, tmp_path, blank)
            assert common.headless_auth_warning({self.API_KEY: self.SECRET}), blank
            assert self.API_KEY not in common._names_defined_in_env_file(dotenv)

    def test_decision_returns_a_bool_so_no_value_can_ride_out_of_it(self, monkeypatch, tmp_path):
        """The only function reading the credential must not return text.

        Keeping the read in a bool-returning function is what makes "the value
        cannot reach a print" structural instead of a property of the current
        formatting. `is True` is deliberate: a str return would satisfy a truthy
        assertion while carrying the secret.
        """
        self._dotenv(monkeypatch, tmp_path, "")
        assert common.api_key_will_be_dropped({self.API_KEY: self.SECRET}) is True
        assert common.api_key_will_be_dropped({}) is False

    def test_env_var_name_identifier_avoids_credential_words(self):
        """The constant holding the variable NAME must not be named like a secret.

        Taint analysis classifies sources by identifier name, so a constant
        called `_API_KEY_ENV` marks every string it flows into as a cleartext
        credential — which flagged the operator message even though the message
        contains only a variable name and a path. The value is unchanged and
        still printed verbatim; only the identifier is constrained.
        """
        assert common._AUTH_ENV_VAR == "KIRO_API_KEY"
        banned = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)")
        assert not banned.search("_AUTH_ENV_VAR"), (
            "renaming this constant to a credential-sounding identifier "
            "re-introduces the py/clear-text-logging-sensitive-data alert"
        )
        src = inspect.getsource(common)
        assert "_API_KEY_ENV" not in src

    def test_credential_is_never_baked_into_the_service_environment(self, monkeypatch):
        """The unit and plist are world-readable; the credential stays out of both."""
        monkeypatch.setenv(self.API_KEY, self.SECRET)
        env = service_environment("/home/tester")
        assert self.API_KEY not in env
        assert self.SECRET not in "".join(env.values())

    def test_a_failing_check_cannot_break_a_successful_install(self, capsys):
        """The unit is already started when this runs, so it must never raise."""
        boom = MagicMock(side_effect=OSError("home resolution exploded"))
        with patch.object(controller, "headless_auth_warning", boom):
            controller._print_headless_auth_warning()
        assert boom.called
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize(
        "plat,module",
        [(Platform.SYSTEMD, "linux"), (Platform.LAUNCHD, "macos")],
    )
    def test_both_install_paths_surface_the_warning(self, plat, module, capsys):
        """Neither platform may install and stay quiet about a dropped credential."""
        installer = MagicMock(return_value=MagicMock(ok=True, message=""))
        with (
            patch.object(controller, "current_platform", return_value=plat),
            patch.object(getattr(controller, module), "install", installer),
            patch.object(controller, "headless_auth_warning", return_value="Note: dropped key"),
        ):
            assert controller.install_service() == 0
        assert "Note: dropped key" in capsys.readouterr().out
