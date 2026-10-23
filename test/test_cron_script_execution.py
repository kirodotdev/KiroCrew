"""Accepting and running a cron job's script or command (``kiro_crew.cron_script``).

This file pins ``resolve_script_path`` and its sensitive-path refusal, the POSIX shell a
command cron resolves before it spawns, how ``run_command_sandboxed`` and
``run_script_sandboxed`` report a run's status and output (on a host with no OS sandbox
too), and how ``mcp_cron`` lists and validates script and command jobs. The other files
of this split are described in ``test_cron_script.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cron_script_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _cron_caller_is_named,
    _crons_dir_tracks_patched_home,
)

from kiro_crew import platform_compat as pc
from kiro_crew.cron_script import (
    _split_script_spec,
    resolve_script_path,
    run_command_sandboxed,
    run_script_sandboxed,
)


class TestResolveScriptPath:
    """Tests for resolve_script_path validation."""

    def test_valid_path(self, tmp_path):
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "test.py"
        script.write_text("def run(ctx): pass")
        with patch("pathlib.Path.home", return_value=tmp_path):
            file_path, func_name = resolve_script_path(str(script) + ":run")
        assert func_name == "run"
        assert "test.py" in file_path

    def test_missing_colon_raises(self):
        with pytest.raises(ValueError, match="expected"):
            resolve_script_path("no_colon_here")

    def test_windows_drive_path_splits_on_func_colon_not_drive_colon(self):
        # rsplit on the last colon must keep the whole drive path and take the
        # trailing func — the drive colon at index 1 must not become the
        # separator (which yielded the nonsense path "...\C").
        module_part, func = _split_script_spec(r"C:\crons\job.py:run")
        assert module_part == r"C:\crons\job.py"
        assert func == "run"

    def test_windows_drive_path_without_func_is_rejected(self):
        # A bare drive path (no :func) must raise, not split at the drive colon.
        with pytest.raises(ValueError, match="expected"):
            _split_script_spec(r"C:\crons\job.py")

    def test_file_not_found_raises(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(FileNotFoundError):
                resolve_script_path(str(tmp_path / ".kirocrew/crons/missing.py") + ":run")

    def test_outside_crons_dir_raises(self, tmp_path):
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        outside = tmp_path / "outside.py"
        outside.write_text("x = 1")
        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(PermissionError, match="must be under"):
                resolve_script_path(str(outside) + ":run")

    def test_tilde_expansion(self, tmp_path):
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "monitor.py"
        script.write_text("def check(ctx): pass")
        # ``os.path.expanduser`` reads $HOME on POSIX but %USERPROFILE% (then
        # %HOMEDRIVE%+%HOMEPATH%) on Windows, so both must be redirected or the
        # tilde resolves to the real profile dir and the file is not found.
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch.dict(os.environ, {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}),
        ):
            file_path, func_name = resolve_script_path("~/.kirocrew/crons/monitor.py:check")
        assert func_name == "check"
        # The tilde must actually have expanded to the patched home, not been
        # left literal — otherwise the assertion above would pass on a path that
        # never resolved.
        assert Path(file_path) == script.resolve()


class TestRunCommandSandboxed:
    """Tests for run_command_sandboxed shell execution."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim and no test in the cron script files calls monkeypatch.undo()
    def _passthrough_sandbox(self, monkeypatch, posix_test_shell):
        """Run commands directly, bypassing the OS-sandbox wrap.

        These tests exercise the run_command_sandboxed output/exit-code plumbing,
        not the sandbox itself. wrap_argv fails closed when no sandbox backend is
        available (e.g. the test env's restricted namespaces), which would make
        every command raise instead of run; patch it to a passthrough so the
        plumbing is what is under test.
        """
        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
        # Bypass the runtime shell probe (which itself spawns a child): these
        # tests exercise the run_command_sandboxed plumbing, not shell fingerprinting.
        monkeypatch.setattr(
            "kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell
        )

    def test_basic_echo(self):
        result = run_command_sandboxed("echo hello")
        assert result["status"] == "ok"
        assert "hello" in result["output"]
        assert result["exit_code"] == 0

    def test_nonzero_exit(self):
        result = run_command_sandboxed("exit 42")
        assert result["status"] == "error"
        assert result["exit_code"] == 42
        assert "Exit code 42" in result["output"]

    def test_empty_output(self):
        result = run_command_sandboxed("true")
        assert result["status"] == "ok"
        assert result["output"].strip() == ""

    def test_stderr_captured(self):
        result = run_command_sandboxed("echo err >&2; exit 1")
        assert "err" in result["output"]

    def test_large_output_truncated(self):
        # Generate >64KB output
        result = run_command_sandboxed("head -c 70000 /dev/zero | tr '\\0' 'x'")
        assert "truncated" in result["output"]
        assert len(result["output"]) <= 70000

    def test_nonzero_exit_reports_stderr_tail_not_head(self):
        """A leading startup warning must not displace the terminal error."""
        leading_warning = "startup warning: " + ("x" * 1000)
        terminal_error = "sh: fatal: actual cause"
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", leading_warning + "\n" + terminal_error)
        with patch("subprocess.Popen", return_value=mock_proc):
            result = run_command_sandboxed("boom")
        assert result["status"] == "error"
        assert "actual cause" in result["output"]
        assert "startup warning" not in result["output"]

    def test_nonzero_exit_redacts_stderr_before_truncating(self):
        """A credential straddling the 1000-char boundary must not leak its tail.

        Redaction must run on the WHOLE stderr before the tail slice: slicing
        first can cut off a secret's detectable prefix, so the pattern no
        longer matches and the raw tail reaches the cron result.
        """
        # Built at runtime so no credential-shaped literal lands in the repo.
        fake_key = "AKIA" + "B" * 16  # matches the AWS access-key-id pattern
        # Sized so the 1000-char tail window starts 2 chars into the credential:
        # a slice-then-redact order keeps "IA" + all 16 B's but drops the
        # "AK" prefix, so the pattern does not match and the tail leaks.
        padding = "p" * 100
        trailing = "e" * 982
        stderr_text = padding + fake_key + trailing
        assert len(stderr_text) - 1000 == len(padding) + 2
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", stderr_text)
        with patch("subprocess.Popen", return_value=mock_proc):
            result = run_command_sandboxed("boom")
        assert result["status"] == "error"
        assert "B" * 16 not in result["output"]
        assert fake_key not in result["output"]
        # Positive proof the payload flowed through redaction (not a vacuous
        # pass on an empty result): the marker's tail survives the tail slice.
        assert "credential]" in result["output"]
        assert "e" * 50 in result["output"]

    def test_nonzero_exit_short_stderr_stays_whole(self):
        """A stderr already shorter than the 1000-char bound is kept whole."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", "short failure\n")
        with patch("subprocess.Popen", return_value=mock_proc):
            result = run_command_sandboxed("boom")
        assert result["output"].endswith("stderr:\nshort failure")


class TestCronSandboxUnavailableIsStructuredNotRaised:
    """A host with no OS sandbox backend (every Windows host) makes wrap_argv
    fail-closed. That must come back as a failed job carrying the remedy, not an
    exception escaping into the scheduler — which is what left command/script
    crons dead-on-arrival on Windows with an uncaught SandboxUnavailableError."""

    @pytest.fixture
    def _sandbox_refuses(self, monkeypatch):
        from kiro_crew.sandbox import SandboxUnavailableError

        def _raise(argv, **k):
            raise SandboxUnavailableError(
                "Sandbox backend unavailable and allow_unsandboxed_exec is not "
                "set. Probe detail: not Linux. Set "
                "agent.sandbox_allow_unsandboxed_exec=true to allow it.",
                kind="no_backend",
                detail="not Linux",
            )

        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", _raise)
        # The runtime shell probe itself routes through wrap_argv now, so a
        # sandbox-refusing test would surface the "No POSIX shell" error before
        # ever reaching the wrap_argv call this test is about. Skip the probe
        # to isolate what's under test.
        monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: "sh")

    def test_command_cron_returns_error_with_remedy(self, _sandbox_refuses):
        result = run_command_sandboxed("echo hello")
        assert result["status"] == "error"
        assert result["exit_code"] == -1
        # The remedy the user must act on survives into the message.
        assert "allow_unsandboxed_exec" in result["output"]

    def test_script_cron_returns_error_with_remedy(self, _sandbox_refuses, tmp_path, monkeypatch):
        script = tmp_path / "job.py"
        script.write_text("def run(msg=''):\n    return {'status': 'ok'}\n")
        # resolve_script_path enforces an allowed root; point it at tmp_path so
        # this test exercises the wrap_argv failure, not the path guard. The stub
        # accepts the launcher's keywords (`allow_bundle_roots`) rather than a
        # bare spec, so a signature change fails at the real call site instead of
        # inside the stub.
        monkeypatch.setattr(
            "kiro_crew.cron_script.resolve_script_path",
            lambda spec, **_kw: (str(script), "run"),
        )
        result = run_script_sandboxed(f"{script}:run", "job-id", timeout=10)
        assert result["status"] == "error"
        assert "allow_unsandboxed_exec" in result["error"]


class TestCommandCronShellResolution:
    """Command crons run `sh -c`; a POSIX shell must be resolved before spawn,
    with a legible error (not a bare WinError 2) when none exists on Windows."""

    def test_posix_strict_sh_is_accepted(self, monkeypatch):
        """A `sh` that refuses brace expansion (dash / ash / POSIX-strict) is
        accepted — the language matches what mcp_cron._vet_shell_command was
        written against."""
        from kiro_crew import cron_script

        monkeypatch.setattr(cron_script.platform_compat, "IS_WINDOWS", False)
        # Resolver walks a FIXED trusted-path list (never $PATH). /bin/sh exists
        # and passes the strict probe.
        monkeypatch.setattr(cron_script.os.path, "isfile", lambda p: p == "/bin/sh")
        monkeypatch.setattr(cron_script, "_shell_is_posix_strict", lambda s: True)
        assert cron_script._resolve_command_shell() == "/bin/sh"

    def test_brace_expanding_sh_is_rejected(self, monkeypatch):
        """A trusted `sh` the probe rejects in EVERY form is refused.

        bash-as-`sh` (macOS /bin/sh, Linux `/bin/sh -> bash`) is not such a
        shell: it passes invoked `+B` — see
        ``test_bash_invoked_as_sh_passes_the_real_probe_with_expansion_off``.
        What stays refused is a shell that expands however it is invoked,
        otherwise `cat ~/.a{w,w}s/credentials` hides from the vet. No fallback:
        the caller then fails-closed with a legible error."""
        from kiro_crew import cron_script

        monkeypatch.setattr(cron_script.platform_compat, "IS_WINDOWS", False)
        # Both trusted candidates exist on disk, but neither survives the
        # probe (bash-in-sh-mode expands the brace).
        monkeypatch.setattr(cron_script.os.path, "isfile", lambda p: True)
        monkeypatch.setattr(cron_script, "_shell_is_posix_strict", lambda s: False)
        assert cron_script._resolve_command_shell() is None

    def test_resolver_never_consults_path(self, monkeypatch):
        """PATH may include agent-writable directories (e.g. ~/.local/bin),
        which is a private-key exposure vector if the resolver honors it: an
        agent-planted `sh` shim would be probed under `cc` isolation but `cc`
        leaves ~/.ssh reachable, so a probe-passing shim can then read it.
        The resolver MUST NOT touch PATH — regression-locking here."""
        from kiro_crew import cron_script

        monkeypatch.setattr(cron_script.platform_compat, "IS_WINDOWS", False)

        # No trusted-path shell available. If the resolver falls back to PATH
        # it would find this planted shim; the test asserts it does not.
        monkeypatch.setattr(cron_script.os.path, "isfile", lambda p: False)
        called = {"probe": False}

        def _probe(_shell: str) -> bool:
            called["probe"] = True
            return True

        monkeypatch.setattr(cron_script, "_shell_is_posix_strict", _probe)
        assert cron_script._resolve_command_shell() is None
        assert called["probe"] is False, (
            "Resolver invoked the probe with a non-trusted-path shell — "
            "regression: it must not consult $PATH."
        )

    def test_shell_probe_detects_brace_expansion(self, monkeypatch):
        """The probe distinguishes POSIX-strict `sh` from a bash-in-sh-mode by
        the OUTPUT of `sh -c 'echo x.{a,a}'`: literal `x.{a,a}` (POSIX) vs
        `x.a x.a` (bash expanded)."""
        from unittest.mock import MagicMock

        from kiro_crew import cron_script

        # Bypass the sandbox wrap the probe now routes through (so this test
        # exercises the DECISION LOGIC, not the sandbox backend availability).
        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (argv, None))
        # Fresh cache per test (the probe memoizes per shell path).
        cron_script._POSIX_STRICT_CACHE.clear()

        strict = MagicMock(returncode=0, stdout="x.{a,a}\n", stderr="")
        expanding = MagicMock(returncode=0, stdout="x.a x.a\n", stderr="")
        with patch.object(cron_script, "run_limited", return_value=strict):
            assert cron_script._shell_is_posix_strict("/bin/dash") is True
        cron_script._POSIX_STRICT_CACHE.clear()
        with patch.object(cron_script, "run_limited", return_value=expanding):
            assert cron_script._shell_is_posix_strict("/bin/sh-is-really-bash") is False

    def test_windows_refuses_command_cron_shell(self, monkeypatch):
        """Windows ships no shell whose language matches what
        mcp_cron._vet_shell_command was written against: cmd.exe is not POSIX,
        and Git-for-Windows's sh.exe IS bash and performs brace expansion (a
        `cat ~/.a{w,w}s/credentials` payload hides from the vet the same way
        under bash or Git-sh). Returning ``None`` on Windows makes command crons
        fail-closed with the legible error rather than route a vetted string
        through a shell that widens its language."""
        from kiro_crew import cron_script

        monkeypatch.setattr(cron_script.platform_compat, "IS_WINDOWS", True)
        # Even if a `sh.exe` were reachable (Git for Windows ships one), Windows
        # returns None unconditionally because that shell IS bash. The resolver
        # short-circuits on IS_WINDOWS before it ever looks at the filesystem.
        assert cron_script._resolve_command_shell() is None

    def test_no_shell_returns_legible_error_not_winerror(self, monkeypatch):
        from kiro_crew import cron_script

        monkeypatch.setattr(cron_script.platform_compat, "IS_WINDOWS", True)
        result = cron_script.run_command_sandboxed("echo hi", timeout=10)
        assert result["status"] == "error"
        assert "No POSIX shell" in result["output"]

    def test_posix_refusal_names_this_hosts_shells_not_windows(self, monkeypatch):
        """On macOS / Linux the refusal must describe THIS host.

        A refusal naming Windows ("Windows ships no such shell") gives a Mac
        operator whose /bin/sh plainly works nothing to act on. The POSIX wording
        names the trusted paths, the probe they failed and the remedy, and never
        names Windows.
        """
        from kiro_crew import cron_script

        monkeypatch.setattr(cron_script.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(cron_script, "_resolve_command_shell", lambda: None)
        result = cron_script.run_command_sandboxed("echo hi", timeout=10)

        assert result["status"] == "error"
        assert result["exit_code"] == -1
        output = result["output"]
        assert "Windows" not in output
        assert "/bin/sh" in output and "/usr/bin/sh" in output
        assert "+B" in output
        assert "script cron" in output
        # The failed probe is cached for the gateway's lifetime, so fixing the
        # shell alone is not enough: the refusal must name the restart.
        assert "restart the gateway" in output

    def test_windows_refusal_keeps_its_by_design_reason(self, monkeypatch):
        """The Windows wording is unchanged: there the refusal IS the platform."""
        from kiro_crew import cron_script

        monkeypatch.setattr(cron_script.platform_compat, "IS_WINDOWS", True)
        output = cron_script.run_command_sandboxed("echo hi", timeout=10)["output"]

        assert "Windows ships no such shell" in output
        assert "Git for Windows" in output

    def test_a_concurrent_failing_probe_cannot_erase_a_proven_form(self, monkeypatch):
        """Two command crons resolving the shell on a cold cache stay coherent.

        Thread A proves the ``+B`` form. Thread B starts on the same cold cache
        and its probe fails transiently. Unserialized, B finishes after A,
        clears the brace-off record A just wrote and caches the shell as
        unusable, so A's executor reads the plain form and runs with brace
        expansion ON. The probe must own the check-probe-record sequence, so B
        sees A's answer instead of probing.
        """
        import threading

        from kiro_crew import cron_script

        monkeypatch.setattr(cron_script, "_POSIX_STRICT_CACHE", {})
        monkeypatch.setattr(cron_script, "_BRACE_OFF_SHELLS", {})
        a_in_brace_off_probe = threading.Event()
        b_probing = threading.Event()
        a_done = threading.Event()
        b_probe_calls: list[bool] = []

        def _probe(shell: str, brace_off: bool) -> bool:
            if threading.current_thread().name == "probe-a":
                if not brace_off:
                    return False
                a_in_brace_off_probe.set()
                # Give B the chance to probe concurrently; it can only if the
                # sequence is unserialized.
                # flake-ok: pre-existing 1 s window moved verbatim; the probe lock keeps the second thread off _probe_one_form so the wait runs out and a slow host can only miss an unserialized regression
                b_probing.wait(timeout=1.0)
                return True
            b_probe_calls.append(brace_off)
            b_probing.set()
            a_done.wait(timeout=5.0)
            return False

        monkeypatch.setattr(cron_script, "_probe_one_form", _probe)
        results: dict[str, bool] = {}

        def _run_a() -> None:
            results["a"] = cron_script._shell_is_posix_strict("/bin/sh")
            a_done.set()

        def _run_b() -> None:
            results["b"] = cron_script._shell_is_posix_strict("/bin/sh")

        a = threading.Thread(target=_run_a, name="probe-a")
        b = threading.Thread(target=_run_b, name="probe-b")
        a.start()
        assert a_in_brace_off_probe.wait(timeout=5.0)
        b.start()
        a.join(timeout=10.0)
        b.join(timeout=10.0)

        assert results == {"a": True, "b": True}
        assert b_probe_calls == [], "a second probe ran while the first owned the cold cache"
        assert cron_script._command_argv("/bin/sh", "echo p.{q,q}") == [
            "/bin/sh",
            "+B",
            "-c",
            "echo p.{q,q}",
        ]

    @pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX shell semantics")
    @pytest.mark.parametrize("source", ["bash-linked-as-sh", "host-bin-sh"])
    def test_bash_invoked_as_sh_passes_the_real_probe_with_expansion_off(
        self, monkeypatch, tmp_path, source
    ):
        """The shape of macOS /bin/sh, measured on a real shell rather than a stub.

        bash started under the name ``sh`` enters POSIX mode, which is what
        macOS /bin/sh is, and POSIX mode still brace-expands: the plain probe
        form prints ``x.a x.a``. The resolver must accept it through the ``+B``
        form and the executor must reuse that form, so the command the cron
        actually runs keeps a brace group literal.

        ``host-bin-sh`` runs the same assertions on this host's own /bin/sh when
        it is such a shell -- on a macOS runner that is the bash 3.2 the report
        came from, on AL2023 / RHEL / Fedora it is the ``/bin/sh -> bash`` link.
        ``bash-linked-as-sh`` builds the shape from whatever bash is installed,
        so a host whose /bin/sh is dash still exercises the ``+B`` path.
        """
        import shutil
        import subprocess

        from kiro_crew import cron_script

        if source == "host-bin-sh":
            sh = "/bin/sh"
        else:
            bash = shutil.which("bash")
            if bash is None:
                pytest.skip("no bash on this host")
            link = tmp_path / "sh"
            link.symlink_to(bash)
            sh = str(link)
        plain = subprocess.run(
            [sh, "-c", "echo x.{a,a}"], capture_output=True, text=True, encoding="utf-8"
        )
        if source == "host-bin-sh" and plain.stdout.strip() != "x.a x.a":
            pytest.skip("this host's /bin/sh does not brace-expand (dash / ash)")
        assert plain.stdout.strip() == "x.a x.a", "bash-as-sh should brace-expand plainly"

        monkeypatch.setattr(cron_script, "wrap_argv", lambda argv, **k: (list(argv), None))
        monkeypatch.setattr(cron_script, "cgroup_scope_argv", lambda argv: list(argv))
        monkeypatch.setattr(cron_script, "_POSIX_STRICT_CACHE", {})
        monkeypatch.setattr(cron_script, "_BRACE_OFF_SHELLS", {})

        assert cron_script._shell_is_posix_strict(sh) is True
        argv = cron_script._command_argv(sh, "echo p.{q,q}")
        assert argv == [sh, "+B", "-c", "echo p.{q,q}"]
        ran = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
        assert ran.stdout.strip() == "p.{q,q}"


class TestRunScriptSandboxed:
    """Tests for run_script_sandboxed Python function execution."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim and no test in the cron script files calls monkeypatch.undo()
    def _passthrough_sandbox(self, monkeypatch, posix_test_shell):
        """Bypass OS-sandbox wrap and ensure subprocess can import kiro_crew.

        wrap_argv fails closed when no sandbox backend is available (e.g. macOS 26
        where sandbox-exec is unsupported). run_script_sandboxed also spawns a fresh
        sys.executable subprocess; on local dev runs outside a packaged install,
        PYTHONPATH is not set so the subprocess can't import kiro_crew. Inject the
        src/ dir so the subprocess finds the package. See
        TestRunCommandSandboxed._passthrough_sandbox.
        """
        import os as _os

        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        existing = _os.environ.get("PYTHONPATH", "")
        monkeypatch.setenv("PYTHONPATH", src_dir + (_os.pathsep + existing if existing else ""))
        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
        # Bypass the runtime shell probe (which itself spawns a child): these
        # tests exercise the run_command_sandboxed plumbing, not shell fingerprinting.
        monkeypatch.setattr(
            "kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell
        )

    def _write_script(self, tmp_path, code):
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "test_script.py"
        script.write_text(code)
        return str(script)

    # ── Terminal stderr context on a hard failure ──
    # These five drive the real ``proc.returncode != 0 and not stdout.strip()``
    # branch through a real subprocess. The launcher ``exec``s the user module
    # OUTSIDE its try/except, so a module-level raise is exactly the shape that
    # reaches that branch: unhandled traceback on stderr, non-zero exit, and no
    # structured stdout to parse.

    TERMINAL_SENTINEL = "REAL_TERMINAL_FAILURE_9f3a"

    def test_a_terminal_failure_survives_leading_stderr_noise(self, tmp_path):
        """The reported error must name why the script died, not what it warned about.

        A process that dies hard leaves its diagnosis at the END of stderr — the
        traceback is the last thing written. Anything a startup path logged
        first (a migration warning, a deprecation notice, an import-time banner)
        sits in front of it, so reporting the HEAD of stderr reports the noise
        and truncates the cause. The operator then reads a cron failure whose
        message describes something that did not kill the job.

        900 characters of leading noise is deliberately past the 500-byte bound,
        so a head-anchored report cannot contain the sentinel by accident.
        """
        script_path = self._write_script(
            tmp_path,
            "import sys\n"
            'sys.stderr.write("W" * 900 + "\\n")\n'
            "sys.stderr.flush()\n"
            f'raise RuntimeError("{self.TERMINAL_SENTINEL}")\n',
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id")

        assert result["status"] == "error"
        assert self.TERMINAL_SENTINEL in result["error"], (
            "the reported error is the head of stderr, so the terminal failure "
            f"was truncated away: {result['error'][:120]!r}"
        )

    def test_a_short_stderr_is_reported_whole(self, tmp_path):
        """Bounding must not start cutting output that already fits.

        The bound exists to cap a runaway stderr, not to reshape the ordinary
        case, so a diagnosis shorter than the cap keeps both ends. This one is a
        CONTROL: it must pass identically before and after the change, which is
        why the child exits WITHOUT a traceback -- an interpreter traceback
        carries two absolute temp paths, and on Windows those alone push even a
        one-line diagnosis past the bound, which would quietly turn this control
        into a second copy of the case above.
        """
        script_path = self._write_script(
            tmp_path,
            "import os, sys\n"
            'sys.stderr.write("LEADING_CONTEXT\\n")\n'
            f'sys.stderr.write("{self.TERMINAL_SENTINEL}\\n")\n'
            "sys.stderr.flush()\n"
            "os._exit(2)\n",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id")

        assert result["status"] == "error"
        assert "LEADING_CONTEXT" in result["error"]
        assert self.TERMINAL_SENTINEL in result["error"]

    def test_a_silent_hard_exit_still_reports_the_exit_code(self, tmp_path):
        """With nothing on either stream, the exit code is the only diagnosis.

        ``os._exit`` skips the launcher's handlers entirely, which is what a
        killed or self-terminating child looks like. The fallback must survive
        the change, or those failures become an empty error string.
        """
        script_path = self._write_script(tmp_path, "import os\nos._exit(3)\n")
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id")

        assert result["status"] == "error"
        assert result["error"] == "exit 3"

    def test_a_credential_in_the_terminal_stderr_is_redacted(self, tmp_path):
        """The reported text still leaves the box, so redaction still applies.

        Moving WHICH slice of stderr is reported must not move it out from
        behind ``redact`` — a traceback can carry a key in a repr just as a
        startup warning can.
        """
        script_path = self._write_script(
            tmp_path,
            "import sys\n"
            'sys.stderr.write("W" * 900 + "\\n")\n'
            "sys.stderr.flush()\n"
            'raise RuntimeError("boom AKIAIOSFODNN7EXAMPLE")\n',
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id")

        assert result["status"] == "error"
        assert "AKIAIOSFODNN7EXAMPLE" not in result["error"]
        assert "[REDACTED" in result["error"]

    def test_a_credential_straddling_the_bound_does_not_leak_a_fragment(self, tmp_path):
        """Redaction must see the WHOLE stream before the 500-char bound cuts it.

        If the suffix is sliced first, a credential that straddles the cut
        point loses its leading characters before ``redact`` runs — and the
        surviving fragment does not match any credential pattern, so it
        reaches logs and the persisted ``last_error`` unmasked.

        Byte layout (written verbatim, ``os._exit`` so no traceback shifts
        it): 300 noise + a 20-char AWS key + 481 trailing chars = 801 total.
        The -500 cut lands ONE character into the key, so slice-then-redact
        leaks the 19-char fragment ``KIAIOSFODNN7EXAMPLE``; redact-then-slice
        replaces the whole key with the tag, whose tail stays in the report.
        """
        script_path = self._write_script(
            tmp_path,
            "import os, sys\n"
            'sys.stderr.write("W" * 300)\n'
            'sys.stderr.write("AKIAIOSFODNN7EXAMPLE")\n'
            'sys.stderr.write("X" * 481)\n'
            "sys.stderr.flush()\n"
            "os._exit(2)\n",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id")

        assert result["status"] == "error"
        assert "AKIAIOSFODNN7EXAMPLE" not in result["error"]
        # The discriminator: the exact fragment the slice-then-redact order
        # leaves behind. Absent under redact-then-slice; present under the bug.
        assert "KIAIOSFODNN7EXAMPLE" not in result["error"]
        assert "credential]" in result["error"]

    def test_ok_status(self, tmp_path):
        script_path = self._write_script(
            tmp_path,
            """
from kiro_crew.cron_script import Skip, Done
def run(ctx):
    pass  # normal return = ok
""",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id", "test-msg")
        assert result["status"] == "ok"

    def test_skip_status(self, tmp_path):
        script_path = self._write_script(
            tmp_path,
            """
from kiro_crew.cron_script import Skip, Done
def run(ctx):
    raise Skip()
""",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id", "")
        assert result["status"] == "skip"

    def test_done_status_with_message(self, tmp_path):
        script_path = self._write_script(
            tmp_path,
            """
from kiro_crew.cron_script import Skip, Done
def run(ctx):
    raise Done("task complete")
""",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id", "")
        assert result["status"] == "done"
        assert result["message"] == "task complete"

    def test_error_status(self, tmp_path):
        script_path = self._write_script(
            tmp_path,
            """
from kiro_crew.cron_script import Skip, Done
def run(ctx):
    raise RuntimeError("something broke")
""",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id", "")
        assert result["status"] == "error"
        assert "something broke" in result["error"]

    def test_function_not_found(self, tmp_path):
        script_path = self._write_script(
            tmp_path,
            """
def other_func(ctx):
    pass
""",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id", "")
        assert result["status"] == "error"
        assert "Function not found" in result["error"]

    def test_ctx_message_passed(self, tmp_path):
        script_path = self._write_script(
            tmp_path,
            """
from kiro_crew.cron_script import Skip, Done
def run(ctx):
    if ctx.message != "hello-world":
        raise RuntimeError(f"expected hello-world, got {ctx.message!r}")
""",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id", "hello-world")
        assert result["status"] == "ok"

    def test_dataclass_with_postponed_annotations_loads(self, tmp_path):
        """A script that runs under plain Python must also load in the child.

        ``dataclasses`` resolves a string annotation through
        ``sys.modules[cls.__module__]``, so a script module the launcher never
        registers there fails at import under ``from __future__ import annotations``.
        """
        script_path = self._write_script(
            tmp_path,
            "from __future__ import annotations\n"
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class Item:\n"
            "    name: str\n"
            "def run(ctx):\n"
            "    if Item('x').name != 'x':\n"
            "        raise RuntimeError('dataclass field lost')\n",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id", "")
        assert result["status"] == "ok", result


class TestResolveScriptPathSensitive:
    """Test for is_sensitive_path block (line 275)."""

    def test_sensitive_path_blocked(self, tmp_path):
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "test.py"
        script.write_text("def run(ctx): pass")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.cron_script.sensitive_path_refusal", return_value="Blocked: x"),
        ):
            with pytest.raises(PermissionError, match="security policy"):
                resolve_script_path(str(script) + ":run")


class TestMcpCronHandlerPaths:
    """Tests for mcp_cron.py handler display and validation paths."""

    def test_cron_list_shows_error(self, tmp_path):
        """Lines 350-351: cron_list shows last_error for errored jobs."""
        from kiro_crew.cron import CronJob, CronSchedule
        from kiro_crew.mcp_cron import _call_tool_inner

        job = CronJob(
            id="t1",
            name="test",
            message="m",
            schedule=CronSchedule(kind="every", every_secs=60),
            script="x.py:f",
        )
        job.last_status = "error"
        job.last_error = "connection refused"
        job.enabled = True
        # cron_list scopes to the caller's own rows, so the fixture row has to
        # name the session the autouse caller fixture provides.
        job.session_key = os.environ["KIROCREW_SESSION_KEY"]
        with (
            patch("kiro_crew.mcp_cron.config_dir", return_value=tmp_path),
            patch("kiro_crew.mcp_cron.CronService") as mock_svc,
        ):
            mock_svc.return_value.list_jobs.return_value = [job]
            result = _call_tool_inner("cron_list", {})
        assert "connection refused" in result

    def test_cron_list_shows_last_result(self, tmp_path):
        """Lines 353-354: cron_list shows last_result for script jobs."""
        from kiro_crew.cron import CronJob, CronSchedule
        from kiro_crew.mcp_cron import _call_tool_inner

        job = CronJob(
            id="t2",
            name="test2",
            message="m",
            schedule=CronSchedule(kind="every", every_secs=60),
            script="x.py:f",
        )
        job.last_status = "ok"
        job.last_result = "CR passed"
        job.enabled = True
        # cron_list scopes to the caller's own rows, so the fixture row has to
        # name the session the autouse caller fixture provides.
        job.session_key = os.environ["KIROCREW_SESSION_KEY"]
        with (
            patch("kiro_crew.mcp_cron.config_dir", return_value=tmp_path),
            patch("kiro_crew.mcp_cron.CronService") as mock_svc,
        ):
            mock_svc.return_value.list_jobs.return_value = [job]
            result = _call_tool_inner("cron_list", {})
        assert "CR passed" in result

    def test_cron_add_invalid_script_path(self, tmp_path):
        """Lines 364-367: cron_add rejects invalid script path."""
        from kiro_crew.mcp_cron import _call_tool_inner

        with patch("kiro_crew.mcp_cron.config_dir", return_value=tmp_path):
            result = _call_tool_inner(
                "cron_add", {"name": "bad", "script": "/nonexistent/path.py:func", "every": 60}
            )
        assert "Error:" in result

    def test_cron_add_with_command(self, tmp_path):
        """Lines 429, 431: cron_add sets script and command fields."""
        from kiro_crew.cron import CronJob, CronSchedule
        from kiro_crew.mcp_cron import _call_tool_inner

        job = CronJob(
            id="new-id",
            name="cmd-job",
            message="",
            schedule=CronSchedule(kind="every", every_secs=60),
            command="echo hi",
        )
        with (
            patch("kiro_crew.mcp_cron.config_dir", return_value=tmp_path),
            patch("kiro_crew.mcp_cron.CronService") as mock_svc,
            patch("kiro_crew.mcp_cron._resolve_session_key", return_value=""),
        ):
            mock_svc.return_value.add_job.return_value = job
            result = _call_tool_inner(
                "cron_add", {"name": "cmd-job", "command": "echo hi", "every": 60}
            )
        assert "cmd-job" in result

    def test_cron_add_with_script(self, tmp_path):
        """Line 429: cron_add sets script field."""
        from kiro_crew.cron import CronJob, CronSchedule
        from kiro_crew.mcp_cron import _call_tool_inner

        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        (crons_dir / "mon.py").write_text("def run(ctx): pass")
        script_path = str(crons_dir / "mon.py") + ":run"
        job = CronJob(
            id="s1",
            name="scr-job",
            message="arg",
            schedule=CronSchedule(kind="every", every_secs=60),
            script=script_path,
        )
        with (
            patch("kiro_crew.mcp_cron.config_dir", return_value=tmp_path),
            patch("kiro_crew.mcp_cron.CronService") as mock_svc,
            patch("kiro_crew.mcp_cron._resolve_session_key", return_value=""),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            mock_svc.return_value.add_job.return_value = job
            result = _call_tool_inner(
                "cron_add",
                {"name": "scr-job", "script": script_path, "message": "arg", "every": 60},
            )
        assert "scr-job" in result


class TestValidationCustomValidator:
    """Tests for validation.py custom validator (lines 348, 350)."""

    def test_requires_message_or_script(self):
        from kiro_crew.validation import (
            ValidationError,
            _validate_cron_add_requires_message_or_script,
        )

        with pytest.raises(ValidationError):
            _validate_cron_add_requires_message_or_script({})

    def test_script_and_command_mutually_exclusive(self):
        from kiro_crew.validation import (
            ValidationError,
            _validate_cron_add_requires_message_or_script,
        )

        with pytest.raises(ValidationError, match="mutually exclusive"):
            _validate_cron_add_requires_message_or_script({"script": "x.py:f", "command": "echo"})
