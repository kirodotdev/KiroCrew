"""Tests for the Windows Task Scheduler service backend.

Everything here runs on any platform: the XML document and the command
resolution are pure, and every ``schtasks`` call goes through one seam that
these tests replace. What they deliberately do NOT cover is what a real Task
Scheduler DOES with the document once it accepts it — that the trigger's
repetition is what restarts a terminated gateway, and that ``RestartOnFailure``
is not, was established against a live scheduler and is recorded in the module
docstring. These tests pin the document that behaviour depends on.
"""

from __future__ import annotations

import os
import subprocess

# The only document parsed here is the one _render() just produced in this
# process, so there is no untrusted input for a bomb or an entity to ride in on.
# nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kiro_crew.service import windows

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _render(**kw) -> str:
    kw.setdefault("user_id", "CORP\\alice")
    kw.setdefault("command", r"C:\Python\Scripts\kirocrew.exe")
    kw.setdefault("arguments", "gateway")
    kw.setdefault("working_dir", r"C:\Users\alice\.kiro\crew")
    return windows.render_task_xml(**kw)


def _tree(xml: str) -> ET.Element:
    # Parsed as bytes: the document declares encoding="UTF-16", and ElementTree
    # refuses a str carrying an encoding declaration.
    return ET.fromstring(xml.encode("utf-16"))


class TestTaskDocument:
    def test_the_document_is_well_formed(self):
        assert _tree(_render()).tag.endswith("}Task")

    def test_no_execution_time_limit(self):
        """The default is three days, which would kill a healthy gateway."""
        root = _tree(_render())
        assert root.find(".//t:ExecutionTimeLimit", _NS).text == "PT0S"

    def test_restart_on_failure_is_bounded(self):
        """It only reaches a launch failure, which does not heal by retrying."""
        root = _tree(_render())
        assert root.find(".//t:RestartOnFailure/t:Count", _NS).text == str(windows.RESTART_COUNT)
        assert root.find(".//t:RestartOnFailure/t:Interval", _NS).text == "PT1M"

    def test_the_trigger_repeats_so_a_terminated_gateway_comes_back(self):
        """RestartOnFailure does not fire on a non-zero exit; this is what does.

        The watchdog terminates the gateway with status 1, which a real Task
        Scheduler records as a completed run rather than a failure. Without a
        repetition on the trigger the task holds a green tick and the gateway
        stays down, which is the bug this backend exists to fix.
        """
        root = _tree(_render())
        rep = root.find(".//t:LogonTrigger/t:Repetition", _NS)
        assert rep is not None, "the trigger must repeat, or nothing supervises"
        assert rep.find("t:Interval", _NS).text == windows.RESTART_INTERVAL

    def test_the_repetition_never_expires(self):
        """A Duration would silently end supervision while the task looks fine.

        An omitted Duration is Task Scheduler's "indefinitely". Any value here
        would mean the gateway stops being recovered once it elapses, and
        nothing in the task's own state would say so.
        """
        rep = _tree(_render()).find(".//t:LogonTrigger/t:Repetition", _NS)
        assert rep.find("t:Duration", _NS) is None
        assert rep.find("t:StopAtDurationEnd", _NS).text == "false"

    @pytest.mark.parametrize("tag", ["DisallowStartIfOnBatteries", "StopIfGoingOnBatteries"])
    def test_battery_settings_are_off(self, tag):
        """Both default to true; a laptop gateway would silently never start."""
        root = _tree(_render())
        assert root.find(f".//t:{tag}", _NS).text == "false"

    def test_a_second_logon_does_not_start_a_second_gateway(self):
        root = _tree(_render())
        assert root.find(".//t:MultipleInstancesPolicy", _NS).text == "IgnoreNew"

    def test_the_trigger_and_principal_name_the_invoking_user(self):
        """An all-users trigger needs admin and fails with 'Access is denied'."""
        root = _tree(_render())
        assert root.find(".//t:LogonTrigger/t:UserId", _NS).text == "CORP\\alice"
        assert root.find(".//t:Principal/t:UserId", _NS).text == "CORP\\alice"

    def test_it_asks_for_no_elevation(self):
        root = _tree(_render())
        assert root.find(".//t:Principal/t:RunLevel", _NS).text == "LeastPrivilege"

    def test_the_action_carries_the_resolved_command(self):
        root = _tree(_render())
        exec_el = root.find(".//t:Actions/t:Exec", _NS)
        assert exec_el.find("t:Command", _NS).text.endswith("kirocrew.exe")
        assert exec_el.find("t:Arguments", _NS).text == "gateway"

    def test_a_hostile_username_cannot_break_the_document(self):
        """A domain or path is attacker-adjacent data; it must be escaped."""
        root = _tree(_render(user_id='A&B<"x">'))
        assert root.find(".//t:Principal/t:UserId", _NS).text == 'A&B<"x">'


class TestGatewayCommand:
    def test_it_falls_back_to_the_module_when_no_script_is_installed(self, tmp_path, monkeypatch):
        """A task pointing at a missing exe fails at logon with only a code."""
        monkeypatch.setattr(windows.sys, "executable", str(tmp_path / "python.exe"))
        cmd, args = windows.gateway_command()
        assert cmd.endswith("python.exe")
        assert args == "-I -m kiro_crew gateway"

    def test_it_prefers_the_installed_console_script(self, tmp_path, monkeypatch):
        (tmp_path / "kirocrew.exe").write_text("")
        monkeypatch.setattr(windows.sys, "executable", str(tmp_path / "python.exe"))
        cmd, args = windows.gateway_command()
        assert cmd.endswith("kirocrew.exe")
        assert args == "gateway"


class TestWriteTaskXml:
    def test_it_writes_utf16_with_a_bom(self, tmp_path):
        """Task Scheduler's own exports are UTF-16; UTF-8 is refused by some builds."""
        p = windows.write_task_xml(_render(), tmp_path / "task.xml")
        assert p.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")
        assert "RestartOnFailure" in p.read_text(encoding="utf-16")

    def test_it_leaves_no_temporary_file_behind(self, tmp_path):
        windows.write_task_xml(_render(), tmp_path / "task.xml")
        assert [f.name for f in tmp_path.iterdir()] == ["task.xml"]


@pytest.fixture(autouse=True)
def _persisted_home_matches_the_pin(monkeypatch):
    """Make the data-home refusal inert unless a test is about it.

    ``conftest`` pins a fresh ``KIROCREW_HOME`` for every test, which is
    exactly the transient override ``install()`` refuses. Mirroring it into
    the persisted lookup keeps that refusal from standing in for the
    behaviour each other test is actually pinning.
    """
    monkeypatch.setattr(windows, "_persisted_override", lambda name: os.environ.get(name))


def _fake_schtasks(rc: int, stdout: str = "", stderr: str = ""):
    def run(*args: str) -> subprocess.CompletedProcess:
        run.calls.append(args)
        return subprocess.CompletedProcess(list(args), rc, stdout, stderr)

    run.calls = []
    return run


class TestSchtasksVerbs:
    def test_install_refuses_when_schtasks_is_not_trusted(self, monkeypatch):
        """PATH must not be able to supply the binary that registers a logon task."""
        monkeypatch.setattr(windows, "schtasks_bin", lambda: None)
        monkeypatch.setattr(windows, "write_task_xml", lambda *a, **k: None)
        with pytest.raises(windows.ServiceInstallError, match="trusted system"):
            windows.install()

    def test_install_surfaces_a_refusal_rather_than_claiming_success(self, monkeypatch, tmp_path):
        monkeypatch.setattr(windows, "write_task_xml", lambda *a, **k: tmp_path / "t.xml")
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(1, stderr="ERROR: denied"))
        with pytest.raises(windows.ServiceInstallError, match="denied"):
            windows.install()

    def test_install_replaces_an_existing_task(self, monkeypatch, tmp_path):
        """Reinstall after an upgrade must not need an uninstall first."""
        fake = _fake_schtasks(0)
        monkeypatch.setattr(windows, "write_task_xml", lambda *a, **k: tmp_path / "t.xml")
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "_schtasks", fake)
        windows.install()
        assert "/F" in fake.calls[0]

    def test_uninstalling_an_absent_task_is_not_an_error(self, monkeypatch):
        fake = _fake_schtasks(1)
        monkeypatch.setattr(windows, "_schtasks", fake)
        monkeypatch.setattr(windows, "is_installed", lambda: False)
        windows.uninstall()
        assert fake.calls == [], "an absent task must not be deleted at all"

    def test_a_denied_delete_is_never_reported_as_a_removal(self, monkeypatch):
        """A second non-zero result is not proof the task went away."""
        monkeypatch.setattr(windows, "is_installed", lambda: True)
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(1, stderr="ERROR: denied"))
        with pytest.raises(windows.ServiceInstallError, match="Delete"):
            windows.uninstall()

    def test_stopping_a_stopped_task_is_not_an_error(self, monkeypatch):
        """/End refuses when nothing is running; the disable still has to land."""
        calls: list[tuple] = []

        def _run(*args: str):
            calls.append(args)
            rc = 1 if args[0] == "/End" else 0
            return subprocess.CompletedProcess(list(args), rc, "", "")

        monkeypatch.setattr(windows, "_schtasks", _run)
        windows.stop()
        assert [c[0] for c in calls] == ["/Change", "/End"]

    def test_stop_disables_before_ending_so_no_tick_slips_through(self, monkeypatch):
        """A tick between /End and /DISABLE would start a gateway the stop missed."""
        fake = _fake_schtasks(0)
        monkeypatch.setattr(windows, "_schtasks", fake)
        windows.stop()
        assert [c[0] for c in fake.calls] == ["/Change", "/End"]
        assert "/DISABLE" in fake.calls[0]

    def test_start_re_enables_before_running(self, monkeypatch):
        """Otherwise a start after a stop is accepted and supervises nothing."""
        fake = _fake_schtasks(0)
        monkeypatch.setattr(windows, "_schtasks", fake)
        windows.start()
        assert [c[0] for c in fake.calls] == ["/Change", "/Run"]
        assert "/ENABLE" in fake.calls[0]

    def test_start_surfaces_a_refused_enable(self, monkeypatch):
        """A start that could not re-arm the trigger has not started anything."""
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(1, stderr="ERROR: denied"))
        with pytest.raises(windows.ServiceInstallError, match="ENABLE"):
            windows.start()

    def test_restart_re_enables_a_stopped_task(self, monkeypatch):
        """Restart after a stop must not inherit the disabled state."""
        fake = _fake_schtasks(0)
        monkeypatch.setattr(windows, "_schtasks", fake)
        assert windows.restart() is True
        assert [c[0] for c in fake.calls] == ["/End", "/Change", "/Run"]
        assert "/ENABLE" in fake.calls[1]

    def test_is_installed_reads_the_exit_code_only(self, monkeypatch):
        """Localized output must never decide this."""
        monkeypatch.setattr(
            windows,
            "_schtasks",
            _fake_schtasks(0, stdout="Bereit\nFolder: KiroCrew"),  # brand-ok: task folder
        )
        assert windows.is_installed() is True
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(1, stdout="Bereit"))
        assert windows.is_installed() is False


class TestIsActive:
    def test_a_localized_scheduler_status_never_decides_it(self, monkeypatch):
        """The whole point: a German host must answer the same as an English one."""
        called = []
        monkeypatch.setattr(
            windows, "_schtasks", lambda *a: called.append(a) or pytest.fail("parsed")
        )
        monkeypatch.setattr(windows, "DASHBOARD_PORT", 9)

        def refuse(*a, **k):
            raise OSError("closed")

        monkeypatch.setattr(windows.socket, "create_connection", refuse)
        assert windows.is_active() is False
        assert called == []

    def test_a_serving_gateway_reads_as_active(self, monkeypatch):
        class _Sock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(windows, "DASHBOARD_PORT", 5476)
        monkeypatch.setattr(windows.socket, "create_connection", lambda *a, **k: _Sock())
        assert windows.is_active() is True

    def test_it_probes_the_shared_dashboard_port(self, monkeypatch):
        """One spelling of the port, not a fourth private copy of the default."""
        seen: list[tuple] = []

        def _record(address, timeout=None):
            seen.append(address)
            raise OSError("closed")

        monkeypatch.setattr(windows, "DASHBOARD_PORT", 4321)
        monkeypatch.setattr(windows.socket, "create_connection", _record)
        assert windows.is_active() is False
        assert seen == [("127.0.0.1", 4321)]


class TestTheRegisteredDocumentIsNotAgentWritable:
    """What ``schtasks`` reads must not sit where the agent can rewrite it.

    The registered action runs as the operator at every logon and outside the
    sandbox, so a definition parked at a predictable path under the data home
    is a window in which the action registered is not the action rendered.
    """

    def test_the_registered_path_is_not_under_the_data_home(self, monkeypatch, tmp_path):
        seen: list[str] = []

        def _record(*args: str):
            seen.append(args[args.index("/XML") + 1])
            return subprocess.CompletedProcess(list(args), 0, "", "")

        monkeypatch.setattr(windows, "_schtasks", _record)
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "_TASK_XML_PATH", tmp_path / "home" / "task.xml")
        windows.install()
        assert len(seen) == 1
        assert tmp_path / "home" not in Path(seen[0]).parents

    def test_the_staged_definition_is_removed(self, monkeypatch, tmp_path):
        seen: list[str] = []

        def _record(*args: str):
            seen.append(args[args.index("/XML") + 1])
            return subprocess.CompletedProcess(list(args), 0, "", "")

        monkeypatch.setattr(windows, "_schtasks", _record)
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "_TASK_XML_PATH", tmp_path / "home" / "task.xml")
        windows.install()
        assert not Path(seen[0]).exists()

    def test_a_refused_registration_leaves_no_readable_copy(self, monkeypatch, tmp_path):
        """A copy an operator reads back must describe a task that exists."""
        copy = tmp_path / "home" / "task.xml"
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(1, stderr="ERROR: denied"))
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "_TASK_XML_PATH", copy)
        with pytest.raises(windows.ServiceInstallError):
            windows.install()
        assert not copy.exists()

    def test_a_successful_install_leaves_one_to_read_back(self, monkeypatch, tmp_path):
        copy = tmp_path / "home" / "task.xml"
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(0))
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "_TASK_XML_PATH", copy)
        assert windows.install() == copy
        assert "RestartOnFailure" in copy.read_text(encoding="utf-16")


class TestTheModuleFallbackCannotImportPlantedCode:
    def test_it_runs_python_in_isolated_mode(self, tmp_path, monkeypatch):
        """-m puts the working directory first on sys.path, and that is the
        data home, which the agent can write."""
        monkeypatch.setattr(windows.sys, "executable", str(tmp_path / "python.exe"))
        _, arguments = windows.gateway_command()
        assert arguments.split()[0] == "-I"


class TestADataHomeTheTaskCannotInherit:
    """Task Scheduler carries no environment block, so the task inherits the
    logon session's. A KIROCREW_HOME set only in the installing shell is not
    carried, and the supervised gateway comes up on the DEFAULT data home."""

    def test_a_transient_override_refuses_rather_than_supervising_another_home(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(windows.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "governed"))
        monkeypatch.setattr(windows, "_persisted_override", lambda _name: None)
        with pytest.raises(windows.ServiceInstallError, match="setx KIROCREW_HOME"):
            windows.install()

    def test_a_transient_port_refuses_too(self, monkeypatch, tmp_path):
        """systemd and launchd bake KIROCREW_PORT in; this backend cannot.

        The operator who moved the port did it because 5476 was taken, so a
        task that silently drops the override is a crash loop.
        """
        home = str(tmp_path / "governed")
        monkeypatch.setattr(windows.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setenv("KIROCREW_HOME", home)
        monkeypatch.setenv("KIROCREW_PORT", "5477")
        monkeypatch.setattr(
            windows, "_persisted_override", lambda name: home if name == "KIROCREW_HOME" else None
        )
        with pytest.raises(windows.ServiceInstallError, match="setx KIROCREW_PORT"):
            windows.install()

    def test_a_persisted_override_installs(self, monkeypatch, tmp_path):
        home = str(tmp_path / "governed")
        monkeypatch.setattr(windows.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setenv("KIROCREW_HOME", home)
        monkeypatch.setattr(windows, "_persisted_override", lambda name: os.environ.get(name))
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(0))
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "_TASK_XML_PATH", tmp_path / "t.xml")
        windows.install()

    def test_no_override_installs(self, monkeypatch, tmp_path):
        from kiro_crew.config import paths as config_paths

        monkeypatch.setattr(windows.platform_compat, "IS_WINDOWS", True)
        # Pinning the DEFAULT too: dropping the override without this lets
        # config_dir() resolve — and create — the operator's real data home.
        monkeypatch.setattr(config_paths, "_resolve_default_home", lambda: tmp_path / "default")
        monkeypatch.setattr(config_paths, "_write_recovery_breadcrumb", lambda _d: None)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(0))
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "_TASK_XML_PATH", tmp_path / "t.xml")
        windows.install()


class TestARefusedMutationIsNotASuccess:
    """An ACL refusal must not read as the state change the caller asked for."""

    def test_a_refused_disable_fails_the_stop(self, monkeypatch):
        """Otherwise the trigger is still armed and undoes the stop in a minute."""
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(1, stderr="ERROR: denied"))
        with pytest.raises(windows.ServiceInstallError, match="DISABLE"):
            windows.stop()

    def test_a_refused_enable_fails_the_restart(self, monkeypatch):
        """A restart that cannot re-arm the trigger has restarted nothing."""
        calls: list[tuple] = []

        def _run(*args: str):
            calls.append(args)
            rc = 1 if "/ENABLE" in args else 0
            return subprocess.CompletedProcess(list(args), rc, "", "")

        monkeypatch.setattr(windows, "_schtasks", _run)
        assert windows.restart() is False
        assert "/Run" not in [c[0] for c in calls]


class TestAnUnresponsiveSchedulerIsNotATraceback:
    def test_a_timeout_becomes_a_service_error(self, monkeypatch):
        """The controller handles ServiceInstallError; TimeoutExpired walks past it."""
        monkeypatch.setattr(windows, "schtasks_bin", lambda: "C:/W/schtasks.exe")

        def _timeout(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="schtasks", timeout=30)

        monkeypatch.setattr(windows.subprocess, "run", _timeout)
        with pytest.raises(windows.ServiceInstallError, match="did not answer"):
            windows._schtasks("/Query")

    def test_a_launch_failure_becomes_a_service_error(self, monkeypatch):
        monkeypatch.setattr(windows, "schtasks_bin", lambda: "C:/W/schtasks.exe")

        def _boom(*_a, **_k):
            raise OSError("nope")

        monkeypatch.setattr(windows.subprocess, "run", _boom)
        with pytest.raises(windows.ServiceInstallError, match="could not run schtasks"):
            windows._schtasks("/Query")


class TestTheDefinitionWriteDoesNotFollowALink:
    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="symlink semantics are POSIX-only")
    def test_a_planted_link_is_not_written_through(self, tmp_path):
        victim = tmp_path / "governed.json"
        victim.write_text("policy that must survive", encoding="utf-8")
        target = tmp_path / "task.xml"
        target.symlink_to(victim)
        windows.write_task_xml(_render(), target)
        assert victim.read_text(encoding="utf-8") == "policy that must survive"


class TestAnUnanswerableQueryIsNotAbsence:
    """ "I could not ask" and "it is not there" are different answers.

    Conflating them let a denied delete pair with a denied query and report a
    removal that did not happen, leaving a task that restarts the gateway
    every minute behind a CLI that said it was gone.
    """

    def test_is_installed_propagates_an_unanswerable_query(self, monkeypatch):
        monkeypatch.setattr(windows, "schtasks_bin", lambda: None)
        with pytest.raises(windows.ServiceInstallError):
            windows.is_installed()

    def test_uninstall_does_not_claim_success_when_it_cannot_confirm(self, monkeypatch):
        """An unanswerable query is not absence, so it must not short-circuit."""
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(0))
        monkeypatch.setattr(
            windows,
            "is_installed",
            lambda: (_ for _ in ()).throw(windows.ServiceInstallError("cannot ask")),
        )
        with pytest.raises(windows.ServiceInstallError, match="cannot ask"):
            windows.uninstall()


class TestTheEnvironmentGuardComparesBothDirections:
    def test_a_persisted_value_the_process_lacks_also_refuses(self, monkeypatch, tmp_path):
        """The task would inherit a value the installing session never used."""
        monkeypatch.setattr(windows.platform_compat, "IS_WINDOWS", True)
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "governed"))
        monkeypatch.setattr(
            windows,
            "_persisted_override",
            lambda name: str(tmp_path / "governed") if name == "KIROCREW_HOME" else "5477",
        )
        with pytest.raises(windows.ServiceInstallError, match="but not for this process"):
            windows.install()

    def test_an_unreadable_persistent_environment_refuses(self, monkeypatch, tmp_path):
        """Not being able to look is not the same as nothing being set."""
        monkeypatch.setattr(windows.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "governed"))

        def _unreadable(_name):
            raise windows.ServiceInstallError("could not read the persistent user environment")

        monkeypatch.setattr(windows, "_persisted_override", _unreadable)
        with pytest.raises(windows.ServiceInstallError, match="could not read"):
            windows.install()
