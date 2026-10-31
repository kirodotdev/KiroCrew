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
from kiro_crew.testing.clock import ManualClock

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

    @pytest.mark.parametrize("trigger", ["TimeTrigger", "LogonTrigger"])
    def test_the_trigger_repeats_so_a_terminated_gateway_comes_back(self, trigger):
        """RestartOnFailure does not fire on a non-zero exit; this is what does.

        The watchdog terminates the gateway with status 1, which a real Task
        Scheduler records as a completed run rather than a failure. Without a
        repetition on the trigger the task holds a green tick and the gateway
        stays down, which is the bug this backend exists to fix.
        """
        root = _tree(_render())
        rep = root.find(f".//t:{trigger}/t:Repetition", _NS)
        assert rep is not None, "the trigger must repeat, or nothing supervises"
        assert rep.find("t:Interval", _NS).text == windows.RESTART_INTERVAL

    @pytest.mark.parametrize("trigger", ["TimeTrigger", "LogonTrigger"])
    def test_the_repetition_never_expires(self, trigger):
        """A Duration would silently end supervision while the task looks fine.

        An omitted Duration is Task Scheduler's "indefinitely". Any value here
        would mean the gateway stops being recovered once it elapses, and
        nothing in the task's own state would say so.
        """
        rep = _tree(_render()).find(f".//t:{trigger}/t:Repetition", _NS)
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
        monkeypatch.setattr(windows, "_user_scripts_dir", lambda: tmp_path / "user" / "Scripts")
        cmd, args = windows.gateway_command()
        assert cmd.endswith("python.exe")
        assert args == "-I -m kiro_crew gateway --no-open"

    def test_it_prefers_the_installed_console_script(self, tmp_path, monkeypatch):
        (tmp_path / "kirocrew.exe").write_text("")
        monkeypatch.setattr(windows.sys, "executable", str(tmp_path / "python.exe"))
        cmd, args = windows.gateway_command()
        assert cmd.endswith("kirocrew.exe")
        assert args == "gateway --no-open"

    def test_a_user_site_install_uses_its_own_console_script(self, tmp_path, monkeypatch):
        """``pip install --user`` puts the script in the per-user Scripts dir.

        The ``-I`` fallback drops the user site, so falling back there would
        register a task that fails with "No module named kiro_crew" at every run.
        """
        user_scripts = tmp_path / "user" / "Scripts"
        user_scripts.mkdir(parents=True)
        (user_scripts / "kirocrew.exe").write_text("")
        monkeypatch.setattr(windows.sys, "executable", str(tmp_path / "python.exe"))
        monkeypatch.setattr(windows, "_user_scripts_dir", lambda: user_scripts)
        cmd, args = windows.gateway_command()
        assert cmd == str(user_scripts / "kirocrew.exe")
        assert args == "gateway --no-open"

    def test_the_interpreter_adjacent_script_wins_over_the_user_one(self, tmp_path, monkeypatch):
        user_scripts = tmp_path / "user" / "Scripts"
        user_scripts.mkdir(parents=True)
        (user_scripts / "kirocrew.exe").write_text("")
        (tmp_path / "kirocrew.exe").write_text("")
        monkeypatch.setattr(windows.sys, "executable", str(tmp_path / "python.exe"))
        monkeypatch.setattr(windows, "_user_scripts_dir", lambda: user_scripts)
        cmd, _args = windows.gateway_command()
        assert cmd == str(tmp_path / "kirocrew.exe")

    def test_the_user_scripts_dir_comes_from_the_nt_user_scheme(self, monkeypatch):
        seen: list[tuple] = []

        def get_path(name, scheme):
            seen.append((name, scheme))
            return "C:/Users/u/AppData/Roaming/Python/Python312/Scripts"

        monkeypatch.setattr(windows.sysconfig, "get_path", get_path)
        assert windows._user_scripts_dir() is not None
        assert seen == [("scripts", "nt_user")]

    def test_an_unknown_user_scheme_is_no_candidate(self, monkeypatch):
        def get_path(name, scheme):
            raise KeyError(scheme)

        monkeypatch.setattr(windows.sysconfig, "get_path", get_path)
        assert windows._user_scripts_dir() is None


class TestWriteTaskXml:
    def test_it_writes_utf16_with_a_bom(self, tmp_path):
        """Task Scheduler's own exports are UTF-16; UTF-8 is refused by some builds."""
        p = windows.write_task_xml(_render(), tmp_path / "task.xml")
        assert p.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")
        assert "RestartOnFailure" in p.read_text(encoding="utf-16")

    def test_it_leaves_no_temporary_file_behind(self, tmp_path):
        windows.write_task_xml(_render(), tmp_path / "task.xml")
        assert [f.name for f in tmp_path.iterdir()] == ["task.xml"]


#: The real lookup, captured before the autouse fixture below replaces it.
_REAL_PERSISTED_OVERRIDE = windows._persisted_override


@pytest.fixture(autouse=True)
def _persisted_home_matches_the_pin(_floor_monkeypatch):
    """Make the data-home refusal inert unless a test is about it.

    ``conftest`` pins a fresh ``KIROCREW_HOME`` for every test, which is
    exactly the transient override ``install()`` refuses. Mirroring it into
    the persisted lookup keeps that refusal from standing in for the
    behaviour each other test is actually pinning.
    """
    _floor_monkeypatch.setattr(windows, "_persisted_override", lambda name: os.environ.get(name))


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

    def test_a_failed_readback_write_registers_nothing(self, monkeypatch, tmp_path):
        """The one outcome install() must not have: a live task behind a failed install.

        The readback write is the step that can raise OSError (ENOSPC, a
        read-only or replaced path). It now runs BEFORE /Create, so when it
        fails the scheduler was never called and the operator is left in the
        state they started in.
        """
        fake = _fake_schtasks(0)
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "_schtasks", fake)

        def boom(contents, path=None):
            if path is not None:  # the staging copy is not the one under test
                return tmp_path / "t.xml"
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(windows, "write_task_xml", boom)
        monkeypatch.setattr(windows, "task_xml_path", lambda: tmp_path / "readback.xml")
        # A ServiceInstallError, because that is the one the controller reports
        # as a failed install rather than letting it escape as a traceback.
        with pytest.raises(windows.ServiceInstallError, match="No space left"):
            windows.install()
        assert fake.calls == [], "schtasks was called despite the install failing"

    def test_a_failed_staging_write_registers_nothing_and_leaves_no_readback(
        self, monkeypatch, tmp_path
    ):
        """The private staging copy can fail too, after the readback copy landed."""
        fake = _fake_schtasks(0)
        readback = tmp_path / "readback.xml"
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "_schtasks", fake)
        monkeypatch.setattr(windows, "task_xml_path", lambda: readback)
        real = windows.write_task_xml

        def boom(contents, path=None):
            if path is None:
                return real(contents, readback)
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(windows, "write_task_xml", boom)
        with pytest.raises(windows.ServiceInstallError, match="stage"):
            windows.install()
        assert fake.calls == [], "schtasks was called despite the install failing"
        assert not readback.exists(), "a readback copy describes a task never registered"

    def test_a_failed_create_puts_the_previous_definition_back(self, monkeypatch, tmp_path):
        """A /Create that fails leaves the readback describing the task that IS registered."""
        readback = tmp_path / "readback.xml"
        readback.write_bytes(b"PRIOR")
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "task_xml_path", lambda: readback)
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(1, stderr="ERROR: denied"))
        with pytest.raises(windows.ServiceInstallError, match="denied"):
            windows.install()
        assert readback.read_bytes() == b"PRIOR"

    def test_a_failed_create_with_no_previous_definition_leaves_no_file(
        self, monkeypatch, tmp_path
    ):
        """...and when there was no prior task, no readback copy is left claiming one."""
        readback = tmp_path / "readback.xml"
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "task_xml_path", lambda: readback)
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(1, stderr="ERROR: denied"))
        with pytest.raises(windows.ServiceInstallError, match="denied"):
            windows.install()
        assert not readback.exists()

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
        monkeypatch.setattr(windows, "is_active", lambda: False)
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
        # A non-zero targeted query is confirmed against the task list, which
        # names tasks by path and is never translated.
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(1, stdout="Bereit"))
        with pytest.raises(windows.ServiceInstallError):
            windows.is_installed()


class TestIsActive:
    def test_a_localized_scheduler_status_never_decides_it(self, monkeypatch):
        """The whole point: a German host must answer the same as an English one."""
        called = []
        monkeypatch.setattr(
            windows, "_schtasks", lambda *a: called.append(a) or pytest.fail("parsed")
        )
        monkeypatch.setattr(windows, "resolve_client_port", lambda _cli: 9)

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

        monkeypatch.setattr(windows, "resolve_client_port", lambda _cli: 5476)
        monkeypatch.setattr(windows.socket, "create_connection", lambda *a, **k: _Sock())
        assert windows.is_active() is True

    def test_it_probes_the_port_the_client_resolver_names(self, monkeypatch):
        """A configured dashboard port is probed, not the import-time default."""
        seen: list[tuple] = []

        def _record(address, timeout=None):
            seen.append(address)
            raise OSError("closed")

        monkeypatch.setattr(windows, "resolve_client_port", lambda _cli: 4321)
        monkeypatch.setattr(windows.socket, "create_connection", _record)
        assert windows.is_active() is False
        assert seen == [("127.0.0.1", 4321)]

    def test_an_operator_port_override_is_probed(self, monkeypatch):
        """KIROCREW_PORT moves the gateway, so the probe must follow it."""
        seen: list[tuple] = []

        def _record(address, timeout=None):
            seen.append(address)
            raise OSError("closed")

        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        monkeypatch.setenv("KIROCREW_PORT", "7778")
        monkeypatch.setattr(windows.socket, "create_connection", _record)
        assert windows.is_active() is False
        assert seen == [("127.0.0.1", 7778)]


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
        windows.install()
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

    def test_a_relative_override_refuses_even_when_persisted(self, monkeypatch, tmp_path):
        """The task starts inside the resolved home and resolves the same string again.

        Equal strings pass the both-directions check, which is why this one is
        separate: the scheduled gateway would land in a nested, empty home.
        """
        monkeypatch.setattr(windows.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setenv("KIROCREW_HOME", "crew-data")
        monkeypatch.setattr(windows, "_persisted_override", lambda name: os.environ.get(name))
        fake = _fake_schtasks(0)
        monkeypatch.setattr(windows, "_schtasks", fake)
        monkeypatch.setattr(windows, "render_task_xml", lambda: pytest.fail("rendered"))
        with pytest.raises(windows.ServiceInstallError, match="relative path"):
            windows.install()
        assert fake.calls == []

    def test_an_absolute_override_passes_the_relative_check(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "governed"))
        windows._refuse_a_relative_data_home()


class TestTheReadbackCopyIsNotFollowed:
    """The readback copy is in the agent-writable data home, and a failed
    registration writes the prior bytes back as an ordinary file."""

    def test_a_link_at_the_readback_name_refuses_before_anything_runs(self, monkeypatch, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"CREDENTIAL")
        service_dir = tmp_path / "service"
        service_dir.mkdir()
        readback = service_dir / "kirocrew-gateway.xml"
        try:
            readback.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("this host cannot create symlinks")
        fake = _fake_schtasks(1, stderr="ERROR: denied")
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "task_xml_path", lambda: readback)
        monkeypatch.setattr(windows, "_schtasks", fake)
        with pytest.raises(windows.ServiceInstallError, match="not a plain file"):
            windows.install()
        assert fake.calls == []
        assert readback.is_symlink(), "the link must be left untouched, not replaced"
        assert secret.read_bytes() == b"CREDENTIAL"

    def test_a_hardlinked_readback_refuses(self, monkeypatch, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"CREDENTIAL")
        service_dir = tmp_path / "service"
        service_dir.mkdir()
        readback = service_dir / "kirocrew-gateway.xml"
        try:
            os.link(secret, readback)
        except (OSError, NotImplementedError):
            pytest.skip("this host cannot create hardlinks")
        fake = _fake_schtasks(0)
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "task_xml_path", lambda: readback)
        monkeypatch.setattr(windows, "_schtasks", fake)
        with pytest.raises(windows.ServiceInstallError, match="not a plain file"):
            windows.install()
        assert fake.calls == []

    def test_the_copy_a_prior_install_wrote_in_the_data_home_is_kept(self, monkeypatch, tmp_path):
        """A reinstall over the real readback location must still read the prior copy.

        Guards the sensitive-path floor: if the data home's service directory
        were refused, every reinstall would fail here.
        """
        readback = windows.task_xml_path()
        readback.parent.mkdir(parents=True, exist_ok=True)
        readback.write_bytes(b"PRIOR")
        monkeypatch.setattr(windows, "_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(1, stderr="ERROR: denied"))
        with pytest.raises(windows.ServiceInstallError, match="denied"):
            windows.install()
        assert readback.read_bytes() == b"PRIOR"

    def test_an_oversized_readback_refuses(self, monkeypatch, tmp_path):
        readback = tmp_path / "kirocrew-gateway.xml"
        readback.write_bytes(b"x" * (windows._READBACK_MAX_BYTES + 1))
        monkeypatch.setattr(windows, "task_xml_path", lambda: readback)
        monkeypatch.setattr(windows, "_schtasks", _fake_schtasks(0))
        with pytest.raises(windows.ServiceInstallError, match="not a plain file"):
            windows.install()


class TestServingIsMoreThanAnOpenPort:
    """status and restart ask whether THIS gateway is up, not whether a port answers."""

    def test_no_run_marker_is_not_serving_even_if_ready_answers(self, monkeypatch):
        monkeypatch.setattr(windows, "resolve_client_port", lambda _cli: 5476)
        monkeypatch.setattr(windows.run_marker, "read_pid", lambda _port: None)
        monkeypatch.setattr(windows, "_ready_status", lambda _port: 200)
        assert windows.is_serving() is False

    def test_a_dead_recorded_pid_is_not_serving(self, monkeypatch):
        monkeypatch.setattr(windows, "resolve_client_port", lambda _cli: 5476)
        monkeypatch.setattr(windows.run_marker, "read_pid", lambda _port: 4242)
        monkeypatch.setattr(windows.platform_compat, "pid_exists", lambda _pid: False)
        monkeypatch.setattr(windows, "_ready_status", lambda _port: 200)
        assert windows.is_serving() is False

    def test_a_not_ready_gateway_is_not_serving(self, monkeypatch):
        monkeypatch.setattr(windows, "resolve_client_port", lambda _cli: 5476)
        monkeypatch.setattr(windows.run_marker, "read_pid", lambda _port: 4242)
        monkeypatch.setattr(windows.platform_compat, "pid_exists", lambda _pid: True)
        monkeypatch.setattr(windows, "_ready_status", lambda _port: 503)
        assert windows.is_serving() is False

    def test_a_recorded_live_ready_gateway_is_serving(self, monkeypatch):
        monkeypatch.setattr(windows, "resolve_client_port", lambda _cli: 5476)
        monkeypatch.setattr(windows.run_marker, "read_pid", lambda _port: 4242)
        monkeypatch.setattr(windows.platform_compat, "pid_exists", lambda _pid: True)
        monkeypatch.setattr(windows, "_ready_status", lambda _port: 200)
        assert windows.is_serving() is True

    def test_a_listener_that_does_not_speak_http_reads_as_unreachable(self, monkeypatch):
        def bad(*a, **k):
            raise windows.http.client.BadStatusLine("x")

        monkeypatch.setattr(windows, "loopback_urlopen", bad)
        assert windows._ready_status(9) == 0

    def test_status_says_not_running_when_only_the_port_answers(self, monkeypatch):
        monkeypatch.setattr(windows, "is_installed", lambda: True)
        monkeypatch.setattr(windows, "is_active", lambda: True)
        monkeypatch.setattr(windows, "is_serving", lambda: False)
        assert windows.status().endswith("gateway is not running")

    def test_waiting_gives_up_at_the_deadline(self, monkeypatch):
        monkeypatch.setattr(windows, "is_serving", lambda: False)
        assert windows.wait_until_serving(timeout=0) is False

    def test_waiting_returns_once_ready(self, monkeypatch):
        answers = iter([False, True])
        monkeypatch.setattr(windows, "is_serving", lambda: next(answers))
        monkeypatch.setattr(windows, "_READY_POLL_SECS", 0)
        assert windows.wait_until_serving(timeout=5) is True


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
        monkeypatch.setattr(windows, "is_active", lambda: False)
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


class _FakeWinreg:
    """A ``winreg`` stand-in holding ``{(root, subkey): {name: (value, kind)}}``."""

    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self, values, expand=lambda v: v):
        self._values = values
        self._expand = expand

    class _Key:
        def __init__(self, entries):
            self.entries = entries

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, subkey):  # noqa: N802 - winreg's own spelling
        entries = self._values.get(root)
        if entries is None:
            raise FileNotFoundError(subkey)
        return self._Key(entries)

    def QueryValueEx(self, key, name):  # noqa: N802 - winreg's own spelling
        if name not in key.entries:
            raise FileNotFoundError(name)
        return key.entries[name]

    def ExpandEnvironmentStrings(self, value):  # noqa: N802 - winreg's own spelling
        return self._expand(value)


class TestThePersistentEnvironmentIsReadAsALogonSeesIt:
    def test_an_expandable_user_value_is_expanded(self, monkeypatch):
        fake = _FakeWinreg(
            {"HKCU": {"KIROCREW_HOME": ("%USERPROFILE%\\crew", _FakeWinreg.REG_EXPAND_SZ)}},
            expand=lambda v: v.replace("%USERPROFILE%", "C:\\Users\\alice"),
        )
        monkeypatch.setattr(windows, "_winreg", fake)
        assert _REAL_PERSISTED_OVERRIDE("KIROCREW_HOME") == "C:\\Users\\alice\\crew"

    def test_a_plain_value_is_not_expanded(self, monkeypatch):
        fake = _FakeWinreg(
            {"HKCU": {"KIROCREW_HOME": ("%NOT%", _FakeWinreg.REG_SZ)}},
            expand=lambda v: pytest.fail("expanded a REG_SZ"),
        )
        monkeypatch.setattr(windows, "_winreg", fake)
        assert _REAL_PERSISTED_OVERRIDE("KIROCREW_HOME") == "%NOT%"

    def test_a_machine_wide_value_counts_when_the_user_has_none(self, monkeypatch):
        fake = _FakeWinreg(
            {"HKCU": {}, "HKLM": {"KIROCREW_PORT": ("5477", _FakeWinreg.REG_SZ)}},
        )
        monkeypatch.setattr(windows, "_winreg", fake)
        assert _REAL_PERSISTED_OVERRIDE("KIROCREW_PORT") == "5477"

    def test_the_user_value_wins_over_the_machine_value(self, monkeypatch):
        fake = _FakeWinreg(
            {
                "HKCU": {"KIROCREW_PORT": ("5478", _FakeWinreg.REG_SZ)},
                "HKLM": {"KIROCREW_PORT": ("5477", _FakeWinreg.REG_SZ)},
            },
        )
        monkeypatch.setattr(windows, "_winreg", fake)
        assert _REAL_PERSISTED_OVERRIDE("KIROCREW_PORT") == "5478"

    def test_unset_everywhere_is_none(self, monkeypatch):
        monkeypatch.setattr(windows, "_winreg", _FakeWinreg({"HKCU": {}, "HKLM": {}}))
        assert _REAL_PERSISTED_OVERRIDE("KIROCREW_HOME") is None

    def test_an_unreadable_machine_key_fails_closed(self, monkeypatch):
        fake = _FakeWinreg({"HKCU": {}})

        def _denied(root, subkey):
            if root == "HKLM":
                raise PermissionError("denied")
            return _FakeWinreg._Key({})

        fake.OpenKey = _denied
        monkeypatch.setattr(windows, "_winreg", fake)
        with pytest.raises(windows.ServiceInstallError, match="could not read"):
            _REAL_PERSISTED_OVERRIDE("KIROCREW_HOME")


class TestSupervisionStartsAtInstall:
    def test_a_time_trigger_arms_the_repetition_without_a_logon(self):
        """/Run fires no trigger, so a repetition hung only on the logon
        trigger would leave a fresh install unsupervised until the next sign-in."""
        trigger = _tree(_render(start_boundary="2026-10-22T12:00:00")).find(".//t:TimeTrigger", _NS)
        assert trigger.find("t:StartBoundary", _NS).text == "2026-10-22T12:00:00"
        assert trigger.find("t:Enabled", _NS).text == "true"

    def test_the_default_boundary_is_an_offsetless_local_time(self):
        boundary = _tree(_render()).find(".//t:TimeTrigger/t:StartBoundary", _NS).text
        assert len(boundary) == len("2026-10-22T12:00:00")


def _scripted_schtasks(answers):
    """A fake _schtasks answering by verb (``/Query`` keyed with its next flag)."""

    def run(*args: str) -> subprocess.CompletedProcess:
        run.calls.append(args)
        key = args[0] if args[0] != "/Query" else f"/Query {args[1]}"
        rc, out = answers.get(key, (0, ""))
        return subprocess.CompletedProcess(list(args), rc, out, "")

    run.calls = []
    return run


class TestAbsenceIsConfirmedNotInferred:
    def test_a_task_missing_from_an_answered_list_is_absent(self, monkeypatch):
        listing = '"\\Other\\task","N/A","Ready"\n'
        monkeypatch.setattr(
            windows,
            "_schtasks",
            _scripted_schtasks({"/Query /TN": (1, ""), "/Query /FO": (0, listing)}),
        )
        assert windows.is_installed() is False

    def test_a_refused_list_is_unknown_not_absent(self, monkeypatch):
        monkeypatch.setattr(
            windows,
            "_schtasks",
            _scripted_schtasks({"/Query /TN": (1, ""), "/Query /FO": (1, "")}),
        )
        with pytest.raises(windows.ServiceInstallError, match="could not establish"):
            windows.is_installed()

    def test_a_listed_task_whose_query_was_refused_is_not_absent(self, monkeypatch):
        """The case that let uninstall report a removal while the task stayed."""
        listing = '"\\KiroCrew\\gateway","N/A","Bereit"\n'
        fake = _scripted_schtasks({"/Query /TN": (1, ""), "/Query /FO": (0, listing)})
        monkeypatch.setattr(windows, "_schtasks", fake)
        with pytest.raises(windows.ServiceInstallError, match="refused"):
            windows.uninstall()
        assert not any(c[0] == "/Delete" for c in fake.calls)

    def test_a_similarly_named_task_is_not_ours(self, monkeypatch):
        listing = '"\\KiroCrew\\gateway2","N/A","Ready"\n'
        monkeypatch.setattr(
            windows,
            "_schtasks",
            _scripted_schtasks({"/Query /TN": (1, ""), "/Query /FO": (0, listing)}),
        )
        assert windows.is_installed() is False


class TestRestartDoesNotClaimAForegroundGateway:
    def test_a_port_still_served_after_end_is_left_alone(self, monkeypatch):
        """A foreground gateway beside a stopped task: restart neither claims
        it nor re-arms the supervisor the operator stood down."""
        fake = _scripted_schtasks({})
        monkeypatch.setattr(windows, "_schtasks", fake)
        monkeypatch.setattr(windows, "is_active", lambda: True)
        monkeypatch.setattr(windows, "_port_released", lambda timeout=0: False)
        assert windows.restart() is None
        assert [c[0] for c in fake.calls] == ["/End"]

    def test_the_tasks_own_gateway_is_restarted(self, monkeypatch):
        fake = _scripted_schtasks({})
        monkeypatch.setattr(windows, "_schtasks", fake)
        monkeypatch.setattr(windows, "is_active", lambda: True)
        monkeypatch.setattr(windows, "_port_released", lambda timeout=0: True)
        assert windows.restart() is True
        assert [c[0] for c in fake.calls] == ["/End", "/Change", "/Run"]

    def test_nothing_serving_starts_the_task(self, monkeypatch):
        monkeypatch.setattr(windows, "_schtasks", _scripted_schtasks({}))
        monkeypatch.setattr(windows, "is_active", lambda: False)
        assert windows.restart() is True

    def test_port_release_gives_up_at_the_deadline(self, monkeypatch):
        clock = ManualClock()
        clock.install(monkeypatch, windows)
        monkeypatch.setattr(windows, "is_active", lambda: True)
        assert windows._port_released(timeout=1) is False
        assert sum(clock.sleeps) >= 1

    def test_port_release_sees_the_port_close(self, monkeypatch):
        ManualClock().install(monkeypatch, windows)
        answers = iter([True, False])
        monkeypatch.setattr(windows, "is_active", lambda: next(answers))
        assert windows._port_released(timeout=5) is True
