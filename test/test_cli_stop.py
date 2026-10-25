"""``kirocrew stop`` and how it recognises a Kiro Crew gateway process.

The command-line classifiers ``_is_kirocrew_process`` and ``_args_look_like_kirocrew`` (a composed
edition's companion module included) and the desktop launcher's JavaScript copy of their two
identity constants, ``_stop`` itself and the bounded ``_wait_for_pids_exit``.
"""

import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
)


class TestIsKirocrewProcess:
    """Tests for _is_kirocrew_process helper.

    It now verifies via ``platform_compat.process_command_line`` (cross-platform:
    Linux /proc, macOS ps, Windows WMI) rather than calling ``ps`` directly, so a
    process whose image name is ``python``/``python.exe`` (the venv kirocrew.exe
    re-exec) is still classified by its command line.
    """

    def _cmdline(self, value):
        return patch(
            "kiro_crew.cli_server.platform_compat.process_command_line", return_value=value
        )

    def test_returns_true_for_kirocrew(self):
        from kiro_crew.cli_server import _is_kirocrew_process

        with self._cmdline("python3 -m kiro_crew.dashboard"):
            assert _is_kirocrew_process(1234) is True

    def test_returns_true_for_kirocrew_binary(self):
        from kiro_crew.cli_server import _is_kirocrew_process

        with self._cmdline("/usr/bin/kirocrew start"):
            assert _is_kirocrew_process(1234) is True

    def test_returns_true_for_module_gateway_form(self):
        """The real service launch form
        ``python -m kiro_crew gateway`` must be recognized. The
        matcher must accept the dotted ``kiro_crew.gateway`` form too, or
        ``kirocrew stop`` no-op's on service installs.

        Patched through the cross-platform ``process_command_line`` seam (the
        Windows port routes _is_kirocrew_process through platform_compat rather
        than calling ``subprocess.check_output`` directly)."""
        from kiro_crew.cli_server import _is_kirocrew_process

        real = (
            "/Users/x/.toolbox/tools/kirocrew/3.1.0/bin/../python3.10/bin/"
            "python3.10 -m kiro_crew gateway\n"
        )
        with self._cmdline(real):
            assert _is_kirocrew_process(54842) is True

    def test_returns_true_for_windows_python_reexec(self):
        # The venv kirocrew.exe re-execs python.exe, so the gateway cmdline reads
        # `python.exe ...\Scripts\kirocrew.exe gateway` (or `-m kiro_crew gateway`).
        # The token-parser (_args_look_like_kirocrew) recognizes the console-script
        # basename ("kirocrew"/"kirocrew.exe") and the "-m kiro_crew gateway" module
        # form; assert both Windows re-exec shapes match.
        from kiro_crew.cli_server import _is_kirocrew_process

        with patch("kiro_crew.cli_server.platform_compat.IS_WINDOWS", True):
            with self._cmdline(
                r'"C:\Program Files\Python312\python.exe" '
                r'"D:\U\.kirocrew\.venv\Scripts\kirocrew.exe" gateway --no-open'
            ):
                assert _is_kirocrew_process(1234) is True
            with self._cmdline(r"C:\Python312\python.exe -m kiro_crew gateway"):
                assert _is_kirocrew_process(1234) is True

    def test_returns_false_for_unrelated(self):
        from kiro_crew.cli_server import _is_kirocrew_process

        with self._cmdline("nginx: worker process"):
            assert _is_kirocrew_process(1234) is False

    def test_returns_false_for_broad_match(self):
        """Editing a kirocrew file should NOT match — only gateway entry points."""
        from kiro_crew.cli_server import _is_kirocrew_process

        with self._cmdline("vim /tmp/kirocrew-notes.txt"):
            assert _is_kirocrew_process(1234) is False

    def test_returns_false_when_cmdline_unavailable(self):
        # process_command_line returns "" on any failure (dead PID, WMI/ps error);
        # _is_kirocrew_process must then fail closed (False), never raise.
        from kiro_crew.cli_server import _is_kirocrew_process

        with self._cmdline(""):
            assert _is_kirocrew_process(1234) is False


class TestArgsLookLikeKirocrew:
    """Rigorous tests for the pure command-line classifier ``_args_look_like_kirocrew``.

    These exercise the structural parser directly (no ``subprocess`` mock needed),
    covering every real launch form plus adversarial near-misses that must NOT be
    matched — a false positive would let ``kirocrew stop`` SIGTERM an unrelated
    process bound to the port.
    """

    @pytest.mark.parametrize(
        "args",
        [
            # Module-invocation form (service spawn) — the regression fix.
            "/Users/x/.toolbox/tools/kirocrew/3.1.0/bin/../python3.10/bin/python3.10 -m kiro_crew gateway",
            "python3 -m kiro_crew gateway",
            "python -m kiro_crew dashboard",
            "python3.10 -m kiro_crew start",
            # macOS framework build: the vendored interpreter's basename is
            # "Python" (capital P) — a case-sensitive startswith("python") missed
            # it, so stop/restart no-oped on macOS framework-build installs
            # (Toolbox being the common one).
            "/Library/Frameworks/Python.framework/Versions/3.10/Resources/"
            "Python.app/Contents/MacOS/Python -m kiro_crew gateway --no-open",
            "Python -m kiro_crew gateway",
            # Subcommand followed by trailing flags.
            "python -m kiro_crew gateway --no-open --port 7777",
            # Legacy dotted-submodule form.
            "python3 -m kiro_crew.gateway",
            "python3 -m kiro_crew.dashboard",
            # Console-script wrapper form.
            "/usr/local/bin/kirocrew gateway",
            "/Users/x/.toolbox/bin/kirocrew start",
            "kirocrew dashboard",
        ],
    )
    def test_matches_server_launch_forms(self, args):
        from kiro_crew.cli_server import _args_look_like_kirocrew

        assert _args_look_like_kirocrew(args) is True

    @pytest.mark.parametrize(
        "args",
        [
            "",  # empty command line
            "nginx: worker process",  # unrelated daemon on the port
            "vim /tmp/kirocrew-notes.txt",  # editing a file named kirocrew*
            "cat /var/log/kiro_crew_gateway.log",  # reading a kirocrew log file
            "python -m kiro_crew",  # bare module, no server subcommand
            "python -m kiro_crew run /tmp/spec.md",  # task runner — NOT a port-bound server
            "python -m kiro_crew run gateway",  # "gateway" is a file arg to run, not the subcommand
            "python -m kiro_crew run start",  # "start" is a file arg to run, not the subcommand
            "python -m kiro_crew_other gateway",  # different package named kiro_crew_other
            "/usr/bin/kirocrew",  # wrapper with no subcommand
            "grep -m kiro_crew gateway somefile",  # "-m" is grep's flag (no python interpreter)
        ],
    )
    def test_rejects_non_server_processes(self, args):
        from kiro_crew.cli_server import _args_look_like_kirocrew

        assert _args_look_like_kirocrew(args) is False

    def test_unbalanced_quotes_do_not_raise(self):
        """A malformed args string (odd quote) must not raise: ``shlex.split``
        falls back to ``str.split`` and the command is still classified."""
        from kiro_crew.cli_server import _args_look_like_kirocrew

        assert _args_look_like_kirocrew('python -m kiro_crew gateway "') is True

    def test_subcommand_match_is_exact_case(self):
        """Subcommands are matched exactly (lower-case), mirroring how the CLI
        dispatches them; ``ps`` preserves argv case so this stays precise."""
        from kiro_crew.cli_server import _args_look_like_kirocrew

        assert _args_look_like_kirocrew("python -m kiro_crew GATEWAY") is False


class TestComposedEditionGatewayModule:
    """A composed edition's gateway boots from the COMPANION's module.

    Its launcher execs ``-m <companion module>`` so the composition root is
    entered rather than the core CLI. Keying the classifier on the literal
    ``kiro_crew`` made ``kirocrew stop`` / ``restart`` drop the one listening pid
    and report "No Kiro Crew gateway currently running on port <p>" on every such
    install — the port lookup found the gateway, the classifier rejected it, and
    the graceful-API / lock-owner fallbacks never ran because they are reached
    only when the port lookup itself comes back empty.
    """

    # The real argv observed on a composed install, verbatim.
    COMPOSED_ARGV = (
        "/opt/kirocrew/backend-dist/kirocrew-backend-arm64/bin/python3.12 "
        "-s -m kirocrew_companion gateway --no-open --port 5476"
    )

    @staticmethod
    def _entry_point(value):
        class _EP:
            def __init__(self, v):
                self.value = v

        return _EP(value)

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from kiro_crew.port_resolution import _gateway_module_roots

        _gateway_module_roots.cache_clear()
        yield
        _gateway_module_roots.cache_clear()

    def test_composed_module_matches_when_companion_installed(self, monkeypatch):
        from kiro_crew.cli_server import _args_look_like_kirocrew
        from kiro_crew.platform import discovery

        monkeypatch.setattr(
            discovery,
            "plugin_entry_points",
            lambda: [self._entry_point("kirocrew_companion.compose:build_context")],
        )
        assert _args_look_like_kirocrew(self.COMPOSED_ARGV) is True

    def test_composed_module_rejected_without_the_companion(self, monkeypatch):
        """Precision is preserved: the widening comes from the INSTALLED entry
        points, so a standalone host still refuses to signal that process."""
        from kiro_crew.cli_server import _args_look_like_kirocrew
        from kiro_crew.platform import discovery

        monkeypatch.setattr(discovery, "plugin_entry_points", lambda: [])
        assert _args_look_like_kirocrew(self.COMPOSED_ARGV) is False

    def test_core_module_still_matches_with_a_companion_installed(self, monkeypatch):
        from kiro_crew.cli_server import _args_look_like_kirocrew
        from kiro_crew.platform import discovery

        monkeypatch.setattr(
            discovery,
            "plugin_entry_points",
            lambda: [self._entry_point("kirocrew_companion.compose:build_context")],
        )
        assert _args_look_like_kirocrew("python3 -m kiro_crew gateway") is True

    def test_companion_module_without_a_server_subcommand_is_refused(self, monkeypatch):
        """The subcommand gate still applies to the widened module set — a
        companion task-runner process must never be SIGTERMed by ``stop``."""
        from kiro_crew.cli_server import _args_look_like_kirocrew
        from kiro_crew.platform import discovery

        monkeypatch.setattr(
            discovery,
            "plugin_entry_points",
            lambda: [self._entry_point("kirocrew_companion.compose:build_context")],
        )
        assert _args_look_like_kirocrew("python -m kirocrew_companion run /tmp/spec.md") is False

    def test_discovery_failure_leaves_the_core_module_matching(self, monkeypatch):
        """Entry-point discovery is best-effort: a raising probe must degrade to
        the core module rather than making ``stop`` blind everywhere."""
        from kiro_crew.cli_server import _args_look_like_kirocrew
        from kiro_crew.platform import discovery

        def _boom():
            raise RuntimeError("metadata unreadable")

        monkeypatch.setattr(discovery, "plugin_entry_points", _boom)
        assert _args_look_like_kirocrew("python3 -m kiro_crew gateway") is True
        assert _args_look_like_kirocrew(self.COMPOSED_ARGV) is False


class TestDesktopGatewayIdentityParity:
    """The desktop launcher classifies a port holder from the SAME command line
    ``kirocrew stop`` does, but in a process with no view of the Python
    environment, so ``website/electron/gateway-stop.js`` carries its own copy
    of the two identity constants. This pins the copies together: widening the
    Python side without the JavaScript side is exactly how the desktop app came
    to refuse a composed edition's own gateway as a foreign port holder.
    """

    GATEWAY_STOP = Path(__file__).parent.parent / "website" / "electron" / "gateway-stop.js"

    def _js_constant(self, name: str) -> str:
        import re

        source = self.GATEWAY_STOP.read_text(encoding="utf-8")
        match = re.search(rf"^const {name} = (.+?);$", source, re.MULTILINE)
        assert match, f"{name} not found in {self.GATEWAY_STOP}"
        return match.group(1)

    def test_server_subcommands_are_the_same_set(self):
        import re

        from kiro_crew.port_resolution import _KIROCREW_SERVER_SUBCOMMANDS

        expr = self._js_constant("KIROCREW_SERVER_SUBCOMMANDS")
        js_set = set(re.findall(r'"([^"]+)"', expr))
        assert js_set == set(_KIROCREW_SERVER_SUBCOMMANDS)

    def test_module_pattern_accepts_every_conventional_entry_point_root(self):
        """The JS regex must accept the core module and the root
        ``_gateway_module_roots()`` derives from a conventionally named
        ``kirocrew.plugins`` entry point, and refuse a dotted submodule. The
        regex uses only syntax Python's ``re`` shares with JavaScript."""
        import re

        expr = self._js_constant("KIROCREW_MODULE_RE")
        assert expr.startswith("/") and expr.endswith("/"), expr
        pattern = re.compile(expr[1:-1])
        for entry_point in (
            "kiro_crew.cli:main",
            "kirocrew_companion.compose:build_context",
            "kirocrew_acme2.compose:build",
        ):
            root = entry_point.split(":", 1)[0].split(".", 1)[0]
            assert pattern.fullmatch(root), root
        for rejected in (
            "kiro_crew.dashboard",
            "kirocrew",
            "kirocrew_",
            "kirocrew-x",
            "Kirocrew_X",
        ):
            assert pattern.fullmatch(rejected) is None, rejected


class TestStop:
    """Tests for _stop CLI function."""

    def _mock_sel(self):
        mock = MagicMock()
        return patch("kiro_crew.cli_commands.sel", return_value=mock)

    @pytest.fixture(autouse=True)
    def _no_service(self):
        # ``_stop`` short-circuits via ``service_controller.stop_service()``
        # when a systemd/launchd service is active on the host. Force the
        # SIGTERM-by-port path so tests don't flake based on whether the
        # test host happens to have ``kirocrew.service`` installed.
        #
        # Also force the port-lookup tool to report AVAILABLE: ``_stop`` now
        # distinguishes "no listener" from "lsof/netstat missing" and prints a
        # different message + SEL outcome for the tool-absent case. These tests
        # exercise the genuine-no-listener path, so pin availability True instead
        # of depending on whether ``lsof`` happens to be installed on the build
        # host.
        with (
            patch("kiro_crew.cli_server.service_controller.stop_service", return_value=False),
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=True,
            ),
        ):
            yield

    @pytest.fixture(autouse=True)
    def _no_alias_service(self):
        # ``_stop`` consults ``service_alias_holder()`` before the foreground
        # SIGTERM-by-port path (an alias of another unit must not be signalled).
        # The real call shells out to ``systemctl show kirocrew.service`` on the
        # test host, so pin it None here, as ``TestRestart`` does; the alias
        # refusal test overrides it. Keeping this a dedicated fixture (not folded
        # into ``_no_service``) makes the hermeticity guard explicit.
        with patch(
            "kiro_crew.cli_server.service_controller.service_alias_holder",
            return_value=None,
        ):
            yield

    def _ports(self, pids):
        return patch("kiro_crew.cli_server.platform_compat.find_listening_pids", return_value=pids)

    def _cmdline(self, value):
        # Same cmdline for any PID queried.
        return patch(
            "kiro_crew.cli_server.platform_compat.process_command_line", return_value=value
        )

    def test_no_process_on_port(self, capsys):
        # No listener on the port (lsof empty / netstat no match) → nothing to stop.
        from kiro_crew.cli_server import _stop

        with self._mock_sel(), self._ports([]):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        assert "No Kiro Crew gateway" in capsys.readouterr().out

    def _tool_absent(self, unpinned_at):
        # The lookup tool reads as unavailable; ``unpinned_at`` is where PATH
        # finds it anyway (None when it is genuinely not installed).
        return (
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=False,
            ),
            patch("kiro_crew.cli_server.platform_compat.listening_pid_tool", return_value="lsof"),
            patch(
                "kiro_crew.cli_server.platform_compat.tool_outside_trusted_dirs",
                return_value=unpinned_at,
            ),
        )

    def test_a_tool_outside_the_pin_is_not_reported_as_missing(self, capsys):
        """A host that keeps binaries elsewhere has the tool; the pin declined it.

        NixOS and Homebrew/conda prefixes are the real population here. Telling
        that operator to install an ``lsof`` they already have sends them in
        circles, so name the path and say the pin is deliberate.
        """
        from kiro_crew.cli_server import _stop

        mock_sel = MagicMock()
        available, tool, unpinned = self._tool_absent("/run/current-system/sw/bin/lsof")
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            self._ports([]),
            available,
            tool,
            unpinned,
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "/run/current-system/sw/bin/lsof" in out, "must name where the tool actually is"
        assert "Install lsof" not in out
        # The audit log has to separate the two causes, not just the outcome.
        resources = mock_sel.log_api_access.call_args.kwargs["resources"]
        assert "reason=lsof_outside_trusted_dirs" in resources

    def test_a_genuinely_missing_tool_still_says_to_install_it(self, capsys):
        """Nothing on PATH means the install advice is the correct advice."""
        from kiro_crew.cli_server import _stop

        mock_sel = MagicMock()
        available, tool, unpinned = self._tool_absent(None)
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            self._ports([]),
            available,
            tool,
            unpinned,
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Install lsof and retry." in out
        resources = mock_sel.log_api_access.call_args.kwargs["resources"]
        assert "reason=lsof_not_found" in resources

    def test_no_kirocrew_process(self, capsys):
        # A listener exists but its cmdline isn't a kirocrew gateway → refuse to kill,
        # but name the pid so the operator can see what holds the port.
        from kiro_crew.cli_server import _stop

        with (
            self._mock_sel(),
            self._ports([1234]),
            self._cmdline("nginx: worker"),
            self._endpoint(False),
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "1234" in out
        assert "not recognised" in out
        assert "(nginx:)" in out

    def test_unrecognised_listener_names_a_quoted_executable_basename(self, capsys):
        # A quoted executable path is one token; the message must carry its
        # basename, not the fragment before the first space.
        from kiro_crew.cli_server import _stop

        quoted = '"/opt/some tool/bin/otherd" --port 5476'
        with (
            self._mock_sel(),
            self._ports([4321]),
            self._cmdline(quoted),
            self._endpoint(False),
        ):
            with pytest.raises(SystemExit):
                _stop(5476)
        out = capsys.readouterr().out
        assert "(otherd)" in out
        assert "/opt/some" not in out

    def test_unrecognised_listener_name_cannot_drive_the_terminal(self, capsys):
        # argv[0] is chosen by whoever holds the port: escape sequences, OSC
        # title changes and BEL must be stripped before the name is echoed, and
        # the name is capped so it cannot flood the line.
        from kiro_crew.cli_server import _MAX_ECHOED_NAME_LEN, _stop, _terminal_safe_name

        hostile = "/tmp/\x1b[31mred\x1b[0m\x1b]0;owned\x07" + "x" * 200
        with (
            self._mock_sel(),
            self._ports([4321]),
            self._cmdline(f"'{hostile}' --port 5476"),
            self._endpoint(False),
        ):
            with pytest.raises(SystemExit):
                _stop(5476)
        out = capsys.readouterr().out
        assert "\x1b" not in out and "\x07" not in out
        assert "[31mred[0m]0;owned" in out  # printable residue only, no ESC/BEL
        assert len(out.split("(", 1)[1].split(")", 1)[0]) <= _MAX_ECHOED_NAME_LEN
        assert _terminal_safe_name("\x1b\x07\u200b") == ""

    def test_successful_stop(self, capsys):
        from kiro_crew.cli_server import _stop

        # The kill is dispatched per-platform: POSIX os.kill(SIGTERM), Windows
        # platform_compat.kill_process_tree (taskkill /T /F, so the gateway's
        # child tree is reaped too). Patch the path the running OS takes so the
        # fake PID is treated as successfully signaled + exited.
        ctx = [
            self._mock_sel(),
            self._ports([1234]),
            self._cmdline("python3 -m kiro_crew.dashboard"),
            # flake-ok: pre-existing stdlib time.sleep stub scoped to this with block, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing patch of the stdlib time.sleep the code under test reads, moved verbatim; tracked for a de-flake pass
            patch("time.sleep"),
            patch("kiro_crew.cli_server.platform_compat.pid_exists", return_value=False),
        ]
        if sys.platform == "win32":
            ctx.append(
                patch("kiro_crew.cli_server.platform_compat.kill_process_tree", return_value=True)
            )
        else:
            ctx.append(patch("os.kill"))
        with contextlib.ExitStack() as stack:
            for c in ctx:
                stack.enter_context(c)
            _stop(5476)
        out = capsys.readouterr().out
        assert "SIGTERM" in out or "Terminated" in out

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX SIGTERM/os.kill semantics; Windows uses taskkill"
    )
    def test_permission_denied(self, capsys):
        from kiro_crew.cli_server import _stop

        with (
            self._mock_sel(),
            self._ports([1234]),
            self._cmdline("python3 -m kiro_crew.dashboard"),
            patch("os.kill", side_effect=PermissionError),
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        assert "No permission" in capsys.readouterr().out

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX ProcessLookupError path; Windows liveness via pid_exists",
    )
    def test_process_already_exited(self, capsys):
        from kiro_crew.cli_server import _stop

        with (
            self._mock_sel(),
            self._ports([1234]),
            self._cmdline("python3 -m kiro_crew.dashboard"),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        assert "already exited" in capsys.readouterr().out

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX SIGTERM/os.kill semantics; Windows uses taskkill"
    )
    def test_partial_permission_denied(self, capsys):
        """One PID succeeds, another is denied — reports both."""
        from kiro_crew.cli_server import _stop

        def kill_side_effect(pid, sig):
            if pid == 5678:
                raise PermissionError

        with (
            self._mock_sel(),
            self._ports([1234, 5678]),
            self._cmdline("python3 -m kiro_crew.dashboard"),
            patch("os.kill", side_effect=kill_side_effect),
            # flake-ok: pre-existing stdlib time.sleep stub scoped to this with block, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing patch of the stdlib time.sleep the code under test reads, moved verbatim; tracked for a de-flake pass
            patch("time.sleep"),
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "SIGTERM" in out
        assert "No permission" in out

    def test_explicit_port_bypasses_service_short_circuit(self, capsys):
        # When --port is passed explicitly (cli_port is not None), the
        # systemd/launchd service short-circuit must be bypassed so the
        # kill-by-port path can target a non-default dev gateway.
        from kiro_crew.cli_server import _stop

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.stop_service",
                return_value=True,
            ) as mock_stop_service,
            self._ports([]),
        ):
            with pytest.raises(SystemExit):
                _stop(8089)
        mock_stop_service.assert_not_called()
        # And we should have fallen through to the kill path
        # (which exits 1 here because no listener is found on 8089).
        assert "No Kiro Crew gateway" in capsys.readouterr().out

    # ---- a listener argv cannot name: the desktop-app spawn shape -----------

    #: The app spawns a bare interpreter inside the toolbox payload, which no
    #: ``_args_look_like_kirocrew`` pattern matches.
    APP_ARGV = (
        "/Applications/KiroCrew.app/Contents/Resources/backend-dist/"
        "kirocrew-backend-arm64/bin/python3.12 /Applications/KiroCrew.app/"
        "Contents/Resources/backend-dist/kirocrew-backend-arm64/serve.py"
    )

    def _kill_patch(self):
        """Patch the signal call the running OS takes, and return it."""
        if sys.platform == "win32":
            return patch(
                "kiro_crew.cli_server.platform_compat.kill_process_tree", return_value=True
            )
        return patch("os.kill")

    def _endpoint(self, answers):
        """Whether the process on the port answers the authenticated shutdown."""
        return patch("kiro_crew.cli_server._request_gateway_shutdown", return_value=answers)

    def _proven(self, pids):
        """Which pids the identity proof vouches for as this port's gateway."""
        return patch("kiro_crew.cli_server._verified_loopback_gateway_pids", return_value=pids)

    @staticmethod
    def _identity_inputs(
        *, record=(50519, "tok"), live_token="tok", loopback=(50519,), uid=1000, posix=True
    ):
        """Every input the identity proof reads, each independently settable."""
        return [
            patch("kiro_crew.cli_server.platform_compat.IS_POSIX", posix),
            patch("kiro_crew.cli_server.run_marker.read_pid_record_path", return_value=record),
            patch("kiro_crew.cli_server.run_marker.pid_start_token", return_value=live_token),
            patch("kiro_crew.cli_server.platform_compat.find_port_listeners", return_value=[]),
            patch(
                "kiro_crew.cli_server.platform_compat.loopback_owner_pids",
                return_value=list(loopback),
            ),
            patch("kiro_crew.cli_server.platform_compat.process_owner_uid", return_value=uid),
            patch("kiro_crew.cli_server.os.getuid", create=True, return_value=1000),
        ]

    def _proof(self, **kwargs):
        """Run the identity proof with *kwargs* overriding one input at a time."""
        from kiro_crew.cli_server import _verified_loopback_gateway_pids

        with contextlib.ExitStack() as stack:
            for ctx in self._identity_inputs(**kwargs):
                stack.enter_context(ctx)
            return _verified_loopback_gateway_pids(5476)

    def test_the_identity_proof_vouches_for_the_recorded_loopback_gateway(self):
        """All four parts line up: the recorded pid is who answers, and it is ours."""
        assert self._proof() == [50519]

    def test_the_identity_proof_refuses_a_squatter_on_loopback(self):
        """Our gateway bound elsewhere, someone else holding loopback.

        The recorded pid is not who a ``127.0.0.1`` request would reach, so the
        secret is not handed to whoever is.
        """
        assert self._proof(loopback=(99999,)) == []

    def test_the_identity_proof_refuses_a_recycled_pid(self):
        """A pid left behind by a crash and reused cannot inherit the claim."""
        assert self._proof(live_token="different") == []
        assert self._proof(record=(50519, "")) == []

    def test_the_identity_proof_refuses_a_process_owned_by_someone_else(self):
        """Pid recycling into another account's process is what the uid closes."""
        assert self._proof(uid=4242) == []

    def test_the_identity_proof_refuses_without_a_record_or_off_posix(self):
        """No recorded identity, and no platform to prove it on, both deny."""
        assert self._proof(record=None) == []
        assert self._proof(posix=False) == []

    def test_argv_declines_the_app_spawn_shape(self):
        """The premise of the tests below, asserted rather than assumed."""
        from kiro_crew.cli_server import _args_look_like_kirocrew

        assert _args_look_like_kirocrew(self.APP_ARGV) is False

    def test_an_argv_declined_listener_is_asked_to_stop_itself(self, capsys):
        """A gateway argv cannot name is still reachable as a gateway.

        The desktop app spawns a bare interpreter, which the argv patterns
        decline. Identity comes from the gateway instead: it is asked to stop
        ITSELF over one loopback request carrying this generation's secret, which
        it publishes at startup whatever its command line reads. Nothing is
        signalled, so no pid is guessed.
        """
        from kiro_crew.cli_server import _stop

        mock_sel = MagicMock()
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            self._ports([50519]),
            self._cmdline(self.APP_ARGV),
            self._proven([50519]),
            self._endpoint(True),
            self._kill_patch() as mock_kill,
        ):
            _stop(5476)
        out = capsys.readouterr().out
        assert "graceful shutdown" in out
        assert "not recognised" not in out
        mock_kill.assert_not_called()
        reasons = [c.kwargs["resources"] for c in mock_sel.log_api_access.call_args_list]
        assert any("reason=argv_declined_listener" in r and "via=api" in r for r in reasons)

    def test_a_listener_that_does_not_answer_is_still_refused(self, capsys):
        """The control: only a process that proves itself is stopped.

        Nothing answers the authenticated request -- no secret, or a stranger on
        the port -- so the existing refusal stands, exit 1, and no pid is
        signalled. Being unrecognised is not by itself permission to act.
        """
        from kiro_crew.cli_server import _stop

        mock_sel = MagicMock()
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            self._ports([50519]),
            self._cmdline(self.APP_ARGV),
            self._proven([50519]),
            self._endpoint(False),
            self._kill_patch() as mock_kill,
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        assert "not recognised" in capsys.readouterr().out
        mock_kill.assert_not_called()
        reasons = [c.kwargs["resources"] for c in mock_sel.log_api_access.call_args_list]
        assert any("reason=unrecognized_listener" in r for r in reasons)
        assert not any("reason=argv_declined_listener" in r for r in reasons)

    def test_an_unproven_responder_is_never_sent_the_secret(self, capsys):
        """No identity, no request -- the refusal comes first.

        The request carries the per-generation secret, which mints owner tokens,
        so a listener the proof cannot vouch for must not receive it. The stop
        refuses exactly as it does for any other unrecognised listener.
        """
        from kiro_crew.cli_server import _stop

        mock_sel = MagicMock()
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            self._ports([50519]),
            self._cmdline(self.APP_ARGV),
            self._proven([]),
            patch("kiro_crew.cli_server._request_gateway_shutdown") as mock_request,
            self._kill_patch() as mock_kill,
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        mock_request.assert_not_called()
        assert "not recognised" in capsys.readouterr().out
        mock_kill.assert_not_called()
        reasons = [c.kwargs["resources"] for c in mock_sel.log_api_access.call_args_list]
        assert any("reason=unrecognized_listener" in r for r in reasons)

    def test_a_recognised_listener_is_not_asked_over_the_endpoint(self, capsys):
        """The endpoint is the fallback, never a substitute for the usual path.

        A listener the argv check DOES classify keeps the ordinary SIGTERM, and
        the request is not made at all -- otherwise every stop would depend on an
        HTTP round trip the pid path never needed.
        """
        from kiro_crew.cli_server import _stop

        with (
            self._mock_sel(),
            self._ports([1234]),
            self._cmdline("python3 -m kiro_crew gateway"),
            # flake-ok: pre-existing stdlib time.sleep stub scoped to this with block, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing patch of the stdlib time.sleep the code under test reads, moved verbatim; tracked for a de-flake pass
            patch("time.sleep"),
            patch("kiro_crew.cli_server.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.cli_server._stop_mcp_gateway_daemon"),
            patch("kiro_crew.cli_server._request_gateway_shutdown") as mock_endpoint,
            self._kill_patch(),
        ):
            _stop(5476)
        mock_endpoint.assert_not_called()
        out = capsys.readouterr().out
        assert "SIGTERM" in out or "Terminated" in out

    def test_an_alias_holding_the_name_is_refused_before_the_foreground_path(self, capsys):
        """When ``kirocrew.service`` is an alias of another unit, ``stop_service()``
        fails closed (returns False), but the foreground fallback must NOT then
        SIGTERM the alias target's managed process: refuse, exit 1, signal
        nothing, and never consult ``find_listening_pids``."""
        from kiro_crew.cli_server import _stop
        from kiro_crew.service.linux import AliasHolder

        mock_sel = MagicMock()
        holder = AliasHolder("system", "other.service")
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            patch(
                "kiro_crew.cli_server.service_controller.service_alias_holder",
                return_value=holder,
            ),
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids") as mock_find,
        ):
            with pytest.raises(SystemExit) as exc:
                _stop()
            assert exc.value.code == 1
        mock_find.assert_not_called()
        out = capsys.readouterr().out
        assert "alias of other.service" in out
        assert "system scope" in out
        assert "does not manage that unit" in out
        # The remedy names the verb the user asked for (stop, not restart) and
        # the system-scope spelling (sudo systemctl, not --user).
        assert "`sudo systemctl stop other.service`" in out
        assert "restart" not in out
        resources = mock_sel.log_api_access.call_args.kwargs["resources"]
        assert "reason=service_name_is_alias" in resources
        assert "scope=system" in resources
        assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    def test_a_user_scope_alias_refusal_names_the_user_manager_and_the_stop_verb(self, capsys):
        """A user-scope alias must be managed with ``systemctl --user`` (plain
        ``sudo systemctl`` reaches the system manager and misses it), and a
        ``kirocrew stop`` must point at ``stop``, not ``restart``."""
        from kiro_crew.cli_server import _stop
        from kiro_crew.service.linux import AliasHolder

        mock_sel = MagicMock()
        holder = AliasHolder("user", "other.service")
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            patch(
                "kiro_crew.cli_server.service_controller.service_alias_holder",
                return_value=holder,
            ),
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids") as mock_find,
        ):
            with pytest.raises(SystemExit) as exc:
                _stop()
            assert exc.value.code == 1
        mock_find.assert_not_called()
        out = capsys.readouterr().out
        assert "user scope" in out
        assert "`systemctl --user stop other.service`" in out
        assert "sudo" not in out
        assert "restart" not in out

    def test_an_explicit_port_bypasses_the_alias_refusal(self, capsys):
        """``kirocrew stop --port N`` explicitly targets a foreground gateway on
        that port, so the service short-circuit (and its alias guard) is skipped."""
        from kiro_crew.cli_server import _stop

        with (
            self._mock_sel(),
            patch("kiro_crew.cli_server.service_controller.service_alias_holder") as mock_alias,
            self._ports([]),
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        mock_alias.assert_not_called()
        assert "No Kiro Crew gateway" in capsys.readouterr().out


class TestWaitForPidsExit:
    """Tests for the bounded ``_wait_for_pids_exit`` helper."""

    def test_empty_pid_list_returns_immediately(self):
        from kiro_crew.cli_server import _wait_for_pids_exit

        assert _wait_for_pids_exit([], timeout=99) == []

    def test_zero_timeout_still_probes_once(self):
        """A zero timeout must not skip the check and report a false all-clear."""
        from kiro_crew.cli_server import _wait_for_pids_exit

        with patch("kiro_crew.cli_server._pid_exited", return_value=False) as mock_exited:
            assert _wait_for_pids_exit([7], timeout=0) == [7]
        mock_exited.assert_called_once_with(7)

    def test_returns_only_the_pids_still_alive(self):
        from kiro_crew.cli_server import _wait_for_pids_exit

        with patch("kiro_crew.cli_server._pid_exited", side_effect=lambda p: p != 9):
            assert _wait_for_pids_exit([8, 9], timeout=0) == [9]
