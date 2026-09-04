"""Tests for cron_script module — script/command execution and path validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.cron_script import (
    Done,
    Report,
    ScriptContext,
    Skip,
    _resolve_internal_secret,
    _resolve_mcp_server,
    _split_script_spec,
    resolve_script_path,
    run_command_sandboxed,
    run_script_sandboxed,
)


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Every test in this module exercises cron field handling, not authorization.

    ``mcp_cron`` refuses a write from a caller it cannot name, so this states the
    precondition these tests always assumed. See the ``named_cron_caller``
    fixture in ``test/conftest.py``.
    """


@pytest.fixture(autouse=True)
def _crons_dir_tracks_patched_home(monkeypatch):
    """Keep ``cron_script.config_dir()`` pointed at ``<patched home>/.kirocrew``.

    The data home moved from the top-level ``~/.kirocrew`` to ``~/.kiro/crew``
    (``config_dir()``), and ``resolve_script_path`` now derives its allowed
    ``crons/`` dir from ``config_dir()`` rather than ``Path.home()/".kirocrew"``.
    These tests patch ``Path.home()`` per-test and write scripts under
    ``<home>/.kirocrew/crons`` — but ``config_dir()`` reads ``KIROCREW_HOME``
    (pinned to a *different* tmp dir by the conftest ``_isolate_kirocrew_home``
    fixture), so without this redirect the allowed dir would never match.
    Redirect ``config_dir`` to ``Path.home()/".kirocrew"`` (evaluated lazily, so
    it tracks whatever ``Path.home()`` each test patches) — preserving the
    existing ``.kirocrew/crons`` layout the tests build. Tests that patch
    ``cron_script.config_dir`` themselves still win (applied later).
    """
    monkeypatch.setattr(
        "kiro_crew.cron_script.config_dir", lambda: Path.home() / ".kirocrew"
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
        with patch("pathlib.Path.home", return_value=tmp_path), patch.dict(
            os.environ, {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
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
    def _passthrough_sandbox(self, monkeypatch, posix_test_shell):
        """Run commands directly, bypassing the OS-sandbox wrap.

        These tests exercise the run_command_sandboxed output/exit-code plumbing,
        not the sandbox itself. wrap_argv fails closed when no sandbox backend is
        available (e.g. the test env's restricted namespaces), which would make
        every command raise instead of run; patch it to a passthrough so the
        plumbing is what is under test.
        """
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None)
        )
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
        # "AK" prefix, so the pattern no longer matches and the tail leaks.
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
        # this test exercises the wrap_argv failure, not the path guard.
        monkeypatch.setattr(
            "kiro_crew.cron_script.resolve_script_path",
            lambda spec: (str(script), "run"),
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
        """macOS /bin/sh is bash-in-POSIX-mode and STILL performs brace
        expansion, so the runtime probe MUST reject it — otherwise
        `cat ~/.a{w,w}s/credentials` hides from the vet the same way a `bash -c`
        candidate would. No fallback: the caller then fails-closed with a
        legible error, matching the Windows path."""
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
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (argv, None)
        )
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


class TestRunScriptSandboxed:
    """Tests for run_script_sandboxed Python function execution."""

    @pytest.fixture(autouse=True)
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
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None)
        )
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
            'import sys\n'
            'sys.stderr.write("W" * 900 + "\\n")\n'
            'sys.stderr.flush()\n'
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
            'import os, sys\n'
            'sys.stderr.write("LEADING_CONTEXT\\n")\n'
            f'sys.stderr.write("{self.TERMINAL_SENTINEL}\\n")\n'
            'sys.stderr.flush()\n'
            'os._exit(2)\n',
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
        script_path = self._write_script(tmp_path, 'import os\nos._exit(3)\n')
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
            'import sys\n'
            'sys.stderr.write("W" * 900 + "\\n")\n'
            'sys.stderr.flush()\n'
            'raise RuntimeError("boom AKIAIOSFODNN7EXAMPLE")\n',
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job-id")

        assert result["status"] == "error"
        assert "AKIAIOSFODNN7EXAMPLE" not in result["error"]
        assert "[REDACTED" in result["error"]

    def test_a_credential_straddling_the_bound_does_not_leak_a_fragment(
        self, tmp_path
    ):
        """Redaction must see the WHOLE stream before the 500-char bound cuts it.

        If the suffix is sliced first, a credential that straddles the cut
        point loses its leading characters before ``redact`` runs — and the
        surviving fragment no longer matches any credential pattern, so it
        reaches logs and the persisted ``last_error`` unmasked.

        Byte layout (written verbatim, ``os._exit`` so no traceback shifts
        it): 300 noise + a 20-char AWS key + 481 trailing chars = 801 total.
        The -500 cut lands ONE character into the key, so slice-then-redact
        leaks the 19-char fragment ``KIAIOSFODNN7EXAMPLE``; redact-then-slice
        replaces the whole key with the tag, whose tail stays in the report.
        """
        script_path = self._write_script(
            tmp_path,
            'import os, sys\n'
            'sys.stderr.write("W" * 300)\n'
            'sys.stderr.write("AKIAIOSFODNN7EXAMPLE")\n'
            'sys.stderr.write("X" * 481)\n'
            'sys.stderr.flush()\n'
            'os._exit(2)\n',
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


class TestScriptContext:
    """Tests for ScriptContext properties."""

    def test_message_property(self):
        job = SimpleNamespace(id="j1", message="test-args")
        ctx = ScriptContext(job=job)
        assert ctx.message == "test-args"

    def test_message_property_missing(self):
        job = SimpleNamespace(id="j1")
        ctx = ScriptContext(job=job)
        assert ctx.message == ""


class TestResolveMcpServer:
    """Tests for _resolve_mcp_server config lookup."""

    def test_returns_none_for_missing_config(self, tmp_path):
        _resolve_mcp_server.cache_clear()
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _resolve_mcp_server("nonexistent-server")
        assert result is None

    def test_returns_argv_and_env_for_valid_config(self, tmp_path):
        _resolve_mcp_server.cache_clear()
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {
            "mcpServers": {
                "test-server": {"command": "node", "args": ["server.js", "--port", "3000"]}
            }
        }
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _resolve_mcp_server("test-server")
        # New shape: (argv, env). No env block in config -> empty env dict.
        assert result == (("node", "server.js", "--port", "3000"), {})

    def test_returns_env_block_from_config(self, tmp_path):
        """Regression: the per-server `env` block (e.g. the PATH that makes a
        launcher's helper binary resolvable) must be returned, not silently
        dropped."""
        _resolve_mcp_server.cache_clear()
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {
            "mcpServers": {
                "path-server": {
                    "command": "some-mcp",
                    "args": ["--mode", "stdio"],
                    "env": {"PATH": "/custom/path", "MCP_REGION": "us-east-1"},
                }
            }
        }
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            argv, env = _resolve_mcp_server("path-server")
        assert argv == ("some-mcp", "--mode", "stdio")
        assert env == {"PATH": "/custom/path", "MCP_REGION": "us-east-1"}

    def test_env_values_coerced_to_str(self, tmp_path):
        """env keys/values are stringified so a numeric config value can't crash
        the subprocess env update."""
        _resolve_mcp_server.cache_clear()
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {
            "mcpServers": {
                "num-env": {
                    "command": "node",
                    "args": [],
                    "env": {"PORT": 3000},
                }
            }
        }
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            _argv, env = _resolve_mcp_server("num-env")
        assert env == {"PORT": "3000"}

    @staticmethod
    def _write_env_config(tmp_path, env_block):
        """Write an agent config whose sole server declares *env_block*."""
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "kirocrew.json").write_text(
            json.dumps(
                {"mcpServers": {"bad-env": {"command": "some-mcp", "env": env_block}}}
            )
        )

    @pytest.mark.parametrize(
        "bad_key,bad_value,label",
        [
            ("A=B", "v", "'=' in name -> ValueError('illegal environment variable name')"),
            ("A\0B", "v", "NUL in name -> ValueError('embedded null byte')"),
            ("A", "v\0w", "NUL in value -> ValueError('embedded null byte')"),
            ("", "v", "empty name -> unreachable, getenv('') is always NULL"),
        ],
    )
    def test_env_entry_popen_would_reject_is_dropped(
        self, tmp_path, bad_key, bad_value, label
    ):
        """An env pair Popen refuses is dropped here, not carried to the spawn.

        Popen validates the env BEFORE the fork and fails the whole spawn over
        one bad pair, so carrying any of these through aborted every
        ``ctx.call_tool`` for the server with a traceback naming Popen instead of
        the config. Dropping is per-entry: the well-formed sibling must survive,
        or the fix would trade a crash for the env-drop bug this function exists
        to fix.
        """
        _resolve_mcp_server.cache_clear()
        self._write_env_config(tmp_path, {bad_key: bad_value, "MCP_REGION": "us-east-1"})
        with patch("pathlib.Path.home", return_value=tmp_path):
            _argv, env = _resolve_mcp_server("bad-env")
        # The malformed entry is gone...
        assert bad_key not in env, label
        # ...and the honourable sibling is untouched.
        assert env == {"MCP_REGION": "us-east-1"}

    @pytest.mark.skipif(
        not pc.IS_POSIX,
        reason="asserts the POSIX execve env contract Popen enforces "
               "(`=`/NUL -> ValueError); Windows validates its env block differently",
    )
    def test_resolved_env_clears_the_popen_boundary_that_rejected_it(self, tmp_path):
        """End of the crash path, asserted where it actually decides.

        The test above proves the pairs are filtered. This one hands both shapes
        to the real ``Popen`` and pins the difference: the raw config env raises
        ValueError before any fork, the resolved env gets far enough to fail on
        the missing binary instead. The baseline half matters -- without it a
        future refactor that stops filtering would still pass, since a test that
        only asserts "no ValueError" cannot tell a fixed boundary from one that
        never rejected anything.
        """
        import subprocess

        _resolve_mcp_server.cache_clear()
        raw_block = {"A=B": "v", "N\0K": "v", "NV": "v\0w", "": "v", "MCP_REGION": "us-east-1"}
        self._write_env_config(tmp_path, raw_block)
        with patch("pathlib.Path.home", return_value=tmp_path):
            _argv, env = _resolve_mcp_server("bad-env")

        missing = str(tmp_path / "does-not-exist")
        # Baseline: each rejected shape really is a spawn-killer, so the pass
        # below means the filter worked rather than that Popen tolerates it.
        # The empty name is absent here on purpose -- Popen accepts it, and it is
        # dropped for being unreachable by the child rather than for crashing.
        for key, value in [("A=B", "v"), ("N\0K", "v"), ("NV", "v\0w")]:
            with pytest.raises(ValueError):
                subprocess.Popen([missing], env={**env, key: value})
        # The resolved env clears env encoding and dies on the absent command --
        # a different failure, reached only after the env was accepted.
        with pytest.raises(FileNotFoundError):
            subprocess.Popen([missing], env=env)


class TestExceptionClasses:
    """Tests for Skip, Done, Report exception classes."""

    def test_skip(self):
        with pytest.raises(Skip):
            raise Skip()

    def test_done_with_message(self):
        try:
            raise Done("completed")
        except Done as d:
            assert d.message == "completed"

    def test_done_empty(self):
        try:
            raise Done()
        except Done as d:
            assert d.message == ""

    def test_report_with_message(self):
        try:
            raise Report("status update")
        except Report as r:
            assert r.message == "status update"


class TestMcpToolClient:
    """Tests for McpToolClient JSON-RPC communication."""

    def test_init_fails_for_unknown_server(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient

        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(RuntimeError, match="not found"):
                McpToolClient("nonexistent-server")

    def test_close_handles_already_terminated(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.terminate = MagicMock()
        client._proc.wait = MagicMock()
        client._proc.returncode = 0
        client._sandbox_cleanup = None
        client.close()
        client._proc.terminate.assert_called_once()

    def test_init_passes_spec_env_to_popen_and_scrubs_secrets(self, monkeypatch):
        """Regression: McpToolClient must hand the spawned MCP subprocess the
        per-server `env` (so a launcher can resolve its helper binary on PATH)
        while still scrubbing cron secrets (Slack tokens / owner id)."""
        from kiro_crew.cron_script import (
            _CRON_ENV_DENY,
            McpToolClient,
            _resolve_mcp_server,
        )

        _resolve_mcp_server.cache_clear()
        # A secret that must NEVER reach the child subprocess env...
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-should-not-leak")
        # ...and an ordinary var that SHOULD be inherited from the cron env.
        monkeypatch.setenv("KIROCREW_PROBE_VAR", "keep-me")

        # Mock proc whose stdout answers the initialize handshake (id == 1).
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = (
            '{"jsonrpc":"2.0","id":1,"result":{}}\n'
        )

        # The spec env carries the PATH the launcher needs, plus a denied key to
        # prove the deny-set is applied to the spec overlay too.
        spec_env = {"PATH": "/custom/launcher/path", "SLACK_BOT_TOKEN": "spec-also-blocked"}
        with patch(
            "kiro_crew.cron_script._resolve_mcp_server",
            return_value=(("some-mcp", "--mode", "stdio"), spec_env),
        ), patch(
            "kiro_crew.cron_script.wrap_argv",
            return_value=(["some-mcp", "--mode", "stdio"], None),
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv",
            side_effect=lambda argv: argv,
        ), patch(
            "kiro_crew.cron_script.popen_limited", return_value=mock_proc
        ) as mock_popen:
            client = McpToolClient("path-server")
            client.close()

        passed_env = mock_popen.call_args.kwargs["env"]
        # Spec PATH is forwarded so the launcher can find its helper binary.
        assert passed_env["PATH"] == "/custom/launcher/path"
        # Ordinary inherited vars survive.
        assert passed_env.get("KIROCREW_PROBE_VAR") == "keep-me"
        # The denied secret is scrubbed from BOTH the inherited env and the
        # spec overlay (it never reaches the child).
        assert "SLACK_BOT_TOKEN" not in passed_env
        assert "SLACK_BOT_TOKEN" in _CRON_ENV_DENY

    def _mock_handshake_proc(self):
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = '{"jsonrpc":"2.0","id":1,"result":{}}\n'
        return mock_proc

    def test_init_leaves_an_operator_command_argv0_for_the_spec_path_to_resolve(
        self, tmp_path, monkeypatch
    ):
        """The spawn site must NOT re-pin argv[0].

        Confinement wrappers are pinned to absolute paths by the functions that
        prepend them (``cgroup_scope_argv``, ``sandbox_exec_argv``), so nothing is
        left for this spawn site to protect. Re-pinning here would be actively
        wrong on the fail-open no-sandbox path, where NO wrapper is prepended and
        argv[0] is the OPERATOR-declared command: resolving that against the
        gateway's own directories would silently run a different copy than the one
        the spec ``PATH`` selects -- overriding the very PATH forwarding this PR
        exists to deliver. Regression guard for that inversion.
        """
        from kiro_crew.cron_script import McpToolClient, _resolve_mcp_server

        _resolve_mcp_server.cache_clear()

        # The SAME command name exists in two places: one on the gateway's PATH,
        # one on the spec PATH. The spec's copy is the one the operator asked for.
        gateway_dir = tmp_path / "gateway"
        gateway_dir.mkdir()
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        for d in (gateway_dir, spec_dir):
            binary = d / "some-mcp"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
        monkeypatch.setenv("PATH", str(gateway_dir))

        spec_env = {"PATH": str(spec_dir)}
        with patch(
            "kiro_crew.cron_script._resolve_mcp_server",
            return_value=(("some-mcp",), spec_env),
        ), patch(
            # Fail-open no-sandbox path: neither helper prepends a wrapper, so
            # argv[0] stays the operator's own command.
            "kiro_crew.cron_script.wrap_argv",
            return_value=(["some-mcp"], None),
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv",
            side_effect=lambda argv: list(argv),
        ), patch(
            "kiro_crew.cron_script.popen_limited", return_value=self._mock_handshake_proc()
        ) as mock_popen:
            client = McpToolClient("path-server")
            client.close()

        popen_argv = mock_popen.call_args.args[0]
        passed_env = mock_popen.call_args.kwargs["env"]
        # Handed to Popen verbatim -- NOT rewritten to the gateway's copy.
        assert popen_argv[0] == "some-mcp"
        assert str(gateway_dir) not in popen_argv[0]
        # ...so the spec PATH is what resolves it.
        assert passed_env["PATH"] == str(spec_dir)

    def test_init_drops_loader_injection_keys_from_spec_env(self, monkeypatch):
        """SECURITY regression: a spec ``env`` must not carry loader/interpreter
        injection keys into the spawn.

        ``_CRON_ENV_DENY`` covers secrets, NOT ``LD_*``/``DYLD_*``/``PYTHON*``, and
        the spec env is externally authorable (pasted ``mcpServers`` blocks). Those
        keys are honoured by the dynamic loader/interpreter INSIDE the confinement
        wrapper process -- i.e. before the wrapper establishes containment -- so
        forwarding them is arbitrary pre-confinement code execution. Pinning
        argv[0] does not help: the loader acts on the pinned binary. The spec env
        therefore goes through ``env.sanitize_spec_env``, the same filter the
        discovery probe uses. ``PATH`` is deliberately NOT dropped -- forwarding it
        is the feature.
        """
        from kiro_crew.cron_script import McpToolClient, _resolve_mcp_server

        _resolve_mcp_server.cache_clear()
        for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "PYTHONPATH"):
            monkeypatch.delenv(key, raising=False)

        spec_env = {
            "LD_PRELOAD": "/tmp/evil.so",
            "LD_LIBRARY_PATH": "/tmp/evil-libs",
            "DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib",
            "PYTHONPATH": "/tmp/evil-py",
            # Lowercase reaches the loader on Windows, where env names are
            # case-insensitive; the sanitizer folds case for that reason.
            "ld_preload": "/tmp/evil-lower.so",
            "PATH": "/opt/helper/bin",
            "MCP_TOKEN": "keep-me",
        }
        with patch(
            "kiro_crew.cron_script._resolve_mcp_server",
            return_value=(("some-mcp",), spec_env),
        ), patch(
            "kiro_crew.cron_script.wrap_argv", return_value=(["some-mcp"], None)
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: list(argv)
        ), patch(
            "kiro_crew.cron_script.popen_limited", return_value=self._mock_handshake_proc()
        ) as mock_popen:
            client = McpToolClient("path-server")
            client.close()

        passed_env = mock_popen.call_args.kwargs["env"]
        for denied in (
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH",
            "ld_preload",
        ):
            assert denied not in passed_env, f"{denied} reached the spawn"
        # The point of the PR survives the filter.
        assert passed_env["PATH"] == "/opt/helper/bin"
        assert passed_env["MCP_TOKEN"] == "keep-me"

    def test_init_drops_reserved_kirocrew_namespace_from_spec_env(self, monkeypatch):
        """SECURITY regression: a spec ``env`` must not forge caller identity.

        The server this bridge most often spawns is ``kirocrew-cron`` itself,
        whose ownership checks resolve the calling session from
        ``KIROCREW_SESSION_KEY``/``KIROCREW_HOST_PID`` when no gateway caller
        block is present. So one of those in that server's ``env`` block would let
        a SCRIPT CRON name itself another session and reach that session's jobs --
        ``cron_remove_all`` over its rows, ``cron_list`` over its contents.

        ``KIROCREW_CLI`` is asserted here too, as history rather than as a live
        consumer: it was an ambient admin flag that skipped ownership outright,
        and #6624 deleted the consumer instead of re-grounding it, because nothing
        in ``src/`` ever set the variable. The deny stays because the CLASS is
        live -- the next identity key somebody adds is the reason.

        Neither existing filter stops it: ``_CRON_ENV_DENY`` covers secrets and
        ``KIROCREW_OWNER_ID``, and the loader prefixes cover ``LD_*``/``PYTHON*``.
        And unlike those, the OS sandbox is not a mitigation at all -- confinement
        bounds what the child may touch, not whose jobs Kiro Crew believes it
        owns. Hence the reserved-namespace deny in the shared sanitizer.
        """
        from kiro_crew.cron_script import McpToolClient, _resolve_mcp_server

        _resolve_mcp_server.cache_clear()
        for key in ("KIROCREW_CLI", "KIROCREW_SESSION_KEY", "KIROCREW_HOST_PID"):
            monkeypatch.delenv(key, raising=False)

        spec_env = {
            # The reported finding.
            "KIROCREW_CLI": "1",
            # Siblings in the same class: two of the three sources the STRICT
            # session resolver accepts because "an agent cannot write them".
            "KIROCREW_SESSION_KEY": "some-other-session",
            "KIROCREW_HOST_PID": "4242",
            # Lowercase reaches the same lookup on Windows.
            "kirocrew_cli": "1",
            # ...and the forwarding this PR exists to deliver still works.
            "PATH": "/opt/helper/bin",
            "MCP_TOKEN": "keep-me",
        }
        with patch(
            "kiro_crew.cron_script._resolve_mcp_server",
            return_value=(("some-mcp",), spec_env),
        ), patch(
            "kiro_crew.cron_script.wrap_argv", return_value=(["some-mcp"], None)
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: list(argv)
        ), patch(
            "kiro_crew.cron_script.popen_limited", return_value=self._mock_handshake_proc()
        ) as mock_popen:
            client = McpToolClient("kirocrew-cron")
            client.close()

        passed_env = mock_popen.call_args.kwargs["env"]
        for denied in (
            "KIROCREW_CLI",
            "KIROCREW_SESSION_KEY",
            "KIROCREW_HOST_PID",
            "kirocrew_cli",
        ):
            assert denied not in passed_env, f"{denied} reached the spawn"
        assert passed_env["PATH"] == "/opt/helper/bin"
        assert passed_env["MCP_TOKEN"] == "keep-me"

    def test_an_identity_key_cannot_be_forged_through_a_cron_spec_env_block(
        self, monkeypatch
    ):
        """The end of the attack path, asserted where it actually decides.

        The test above proves the key never reaches the child env. This one runs
        the real consumer against that env: the strict session resolver evaluated
        in the environment the spawned server would have started with must not
        return the forged session, so a spec's ``env`` block cannot name another
        session even if some future refactor changes which filter drops the key.

        The key under test used to be ``KIROCREW_CLI``, an ambient admin flag
        whose consumer #6624 deleted outright -- it had no producer in ``src/``,
        so there was no claim to re-ground. ``KIROCREW_SESSION_KEY`` is the right
        successor subject precisely because it DOES have legitimate producers
        (the ACP spawn path, this bridge), so for it the filter is load-bearing
        rather than belt-and-braces over a dead name.

        SCOPE, deliberately narrow: this covers the ``env`` block only, which is
        the channel ``sanitize_spec_env`` controls. The same spec's ``command``
        field is equally config-authored and reaches the identical consumer
        (``sh -c 'KIROCREW_SESSION_KEY=... exec kirocrew-cron'``), so it is a
        sibling forgery channel this filter does not close and this test does not
        claim to -- confinement bounds what the child may touch, not whose jobs
        Kiro Crew believes it owns. Closing it needs consumer-side vouching
        rather than another deny-list; naming the boundary here keeps the
        assertion honest about which half is proven.
        """
        from kiro_crew.cron_script import McpToolClient, _resolve_mcp_server
        from kiro_crew.mcp_core import _resolve_session_key_strict

        _resolve_mcp_server.cache_clear()
        forged = "dashboard:victim"
        # Baseline: the key really is an identity source, so a miss below means
        # the filter worked rather than that the key never mattered.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", forged)
        assert _resolve_session_key_strict() == forged
        monkeypatch.delenv("KIROCREW_SESSION_KEY")

        with patch(
            "kiro_crew.cron_script._resolve_mcp_server",
            return_value=(("some-mcp",), {"KIROCREW_SESSION_KEY": forged}),
        ), patch(
            "kiro_crew.cron_script.wrap_argv", return_value=(["some-mcp"], None)
        ), patch(
            "kiro_crew.cron_script.cgroup_scope_argv", side_effect=lambda argv: list(argv)
        ), patch(
            "kiro_crew.cron_script.popen_limited", return_value=self._mock_handshake_proc()
        ) as mock_popen:
            client = McpToolClient("kirocrew-cron")
            client.close()

        child_env = mock_popen.call_args.kwargs["env"]
        with patch.dict("os.environ", child_env, clear=True):
            assert _resolve_session_key_strict() != forged

    def test_rpc_disconnect_includes_rc_and_stderr_tail(self, tmp_path):
        """a handshake EOF must surface exit code + stderr tail."""
        from kiro_crew.cron_script import McpToolClient
        stderr_path = tmp_path / "stderr.log"
        stderr_path.write_text("Node version 18 detected, but version 20 or higher is required.\n")
        client = object.__new__(McpToolClient)
        client._server_name = "atoz-mcp"
        client._req_id = 0
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._proc.poll.return_value = 1
        # _recv returns None (EOF) on the first read
        with patch.object(McpToolClient, "_recv", return_value=None):
            with pytest.raises(RuntimeError) as excinfo:
                client._rpc("initialize")
        msg = str(excinfo.value)
        assert "atoz-mcp" in msg
        assert "initialize" in msg
        assert "rc=1" in msg
        assert "version 20 or higher is required" in msg

    def test_rpc_disconnect_empty_stderr(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient
        client = object.__new__(McpToolClient)
        client._server_name = "some-mcp"
        client._req_id = 0
        client._stderr_file = SimpleNamespace(name=str(tmp_path / "missing.log"))
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._proc.poll.return_value = None
        with patch.object(McpToolClient, "_recv", return_value=None):
            with pytest.raises(RuntimeError, match=r"stderr tail: \(empty\)"):
                client._rpc("tools/call")

    def test_stderr_tail_redacts_credentials(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient
        stderr_path = tmp_path / "stderr.log"
        stderr_path.write_text("boom AKIA1234567890123456 failure")
        client = object.__new__(McpToolClient)
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        tail = client._stderr_tail()
        assert "AKIA1234567890123456" not in tail
        assert "boom" in tail

    def test_stderr_tail_redacts_exfiltration_urls(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient
        stderr_path = tmp_path / "stderr.log"
        # Long innocuous query (>= 200 chars) triggers redact_exfiltration_urls'
        # length-based heuristic. We can't use a credential-shaped value (AKIA,
        # base64 blob) because redact_credentials runs first and would replace
        # it before the URL redactor sees it, leaving a short benign query.
        long_query = "data=" + ("a" * 250)
        stderr_path.write_text(
            f"boom https://evil.example.com/leak?{long_query} failure"
        )
        client = object.__new__(McpToolClient)
        client._stderr_file = SimpleNamespace(name=str(stderr_path))
        tail = client._stderr_tail()
        assert "evil.example.com/leak" not in tail
        assert "boom" in tail

    def test_close_removes_stderr_tempfile(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient
        stderr_path = tmp_path / "stderr.log"
        stderr_path.write_text("x")
        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.terminate = MagicMock()
        client._proc.wait = MagicMock()
        client._sandbox_cleanup = None
        # mimic a real file handle: has .close() and .name
        fh = open(stderr_path, "w+")
        client._stderr_file = fh
        client.close()
        assert not stderr_path.exists()


class TestScriptContextNotify:
    """Tests for ScriptContext.notify() HTTP delivery."""

    def test_notify_success(self):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("kiro_crew.cron_script.loopback_urlopen", return_value=mock_response):
            result = ctx.notify("hello")
        assert result == {"ok": True}

    def test_notify_failure_raises(self):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"error": "forbidden"}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("kiro_crew.cron_script.loopback_urlopen", return_value=mock_response):
            with pytest.raises(RuntimeError, match="forbidden"):
                ctx.notify("hello")

    def test_notify_redacts_credentials(self):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch(
            "kiro_crew.cron_script.loopback_urlopen", return_value=mock_response
        ) as mock_urlopen:
            ctx.notify("secret AKIA1234567890123456 text")
            # Verify the request was made (redaction happens internally)
            mock_urlopen.assert_called_once()


class TestScriptContextCallTool:
    """Tests for ScriptContext.call_tool() MCP bridge."""

    def test_call_tool_raises_on_unknown_server(self, tmp_path):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(RuntimeError, match="not found"):
                ctx.call_tool("nonexistent", "tool", {})


class TestRunCommandSandboxedEdgeCases:
    """Additional edge case tests for run_command_sandboxed."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch, posix_test_shell):
        """Bypass the OS-sandbox wrap so these plumbing tests run without a
        sandbox backend (see TestRunCommandSandboxed._passthrough_sandbox)."""
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        # Bypass the runtime shell probe (which itself spawns a child): these
        # tests exercise the run_command_sandboxed plumbing, not shell fingerprinting.
        monkeypatch.setattr(
            "kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell
        )

    def test_command_with_env_vars(self):
        result = run_command_sandboxed("echo $HOME")
        assert result["status"] == "ok"
        # HOME should be available (not scrubbed)
        assert result["output"].strip() != ""

    def test_multiline_output(self):
        result = run_command_sandboxed("echo line1; echo line2; echo line3")
        assert result["status"] == "ok"
        assert "line1" in result["output"]
        assert "line3" in result["output"]

    def test_pipe_command(self):
        result = run_command_sandboxed("echo hello world | wc -w")
        assert result["status"] == "ok"
        assert "2" in result["output"]


class TestRunScriptSandboxedEdgeCases:
    """Additional edge case tests for run_script_sandboxed."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        """See TestRunScriptSandboxed._passthrough_sandbox."""
        import os as _os
        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        existing = _os.environ.get("PYTHONPATH", "")
        monkeypatch.setenv("PYTHONPATH", src_dir + (_os.pathsep + existing if existing else ""))
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        # Bypass the runtime shell probe (which itself spawns a child): these
        # tests exercise the run_command_sandboxed plumbing, not shell fingerprinting.
        # Return "sh" so Popen mocks that assert on argv[0] still see it.
        monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: "sh")

    def _write_script(self, tmp_path, code):
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "edge_test.py"
        script.write_text(code)
        return str(script)

    def test_report_status(self, tmp_path):
        script_path = self._write_script(
            tmp_path,
            """
from kiro_crew.cron_script import Report
def run(ctx):
    raise Report("progress update")
""",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job", "")
        assert result["status"] == "report"
        assert result["message"] == "progress update"

    def test_script_with_imports(self, tmp_path):
        script_path = self._write_script(
            tmp_path,
            """
import os
from kiro_crew.cron_script import Done
def run(ctx):
    # Verify we can import standard library
    assert os.path.exists("/")
""",
        )
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(script_path + ":run", "test-job", "")
        assert result["status"] == "ok"


class TestMcpToolClientProtocol:
    """Tests for McpToolClient JSON-RPC protocol internals."""

    def test_send_writes_json_line(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        mock_stdin = MagicMock()
        client._proc.stdin = mock_stdin
        client._send({"jsonrpc": "2.0", "method": "test"})
        written = mock_stdin.write.call_args[0][0]
        assert json.loads(written.strip()) == {"jsonrpc": "2.0", "method": "test"}
        mock_stdin.flush.assert_called_once()

    def test_recv_returns_parsed_json(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdout = MagicMock()
        client._proc.stdout.readline.return_value = '{"jsonrpc":"2.0","id":1,"result":{}}\n'
        msg = client._recv()
        assert msg == {"jsonrpc": "2.0", "id": 1, "result": {}}

    def test_recv_returns_none_on_eof(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdout = MagicMock()
        client._proc.stdout.readline.return_value = ""
        assert client._recv() is None

    def test_recv_skips_blank_lines(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdout = MagicMock()
        client._proc.stdout.readline.side_effect = ["\n", "  \n", '{"id":1,"result":"ok"}\n']
        msg = client._recv()
        assert msg == {"id": 1, "result": "ok"}

    def test_rpc_sends_and_receives(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = (
            '{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n'
        )
        result = client._rpc("tools/list")
        assert result == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}

    def test_rpc_raises_on_eof(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = ""
        with pytest.raises(RuntimeError, match="disconnected"):
            client._rpc("tools/list")

    def test_call_tool_success(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": "hello"}]},
                }
            )
            + "\n"
        )
        result = client.call_tool("my_tool", {"arg": "val"})
        assert result == "hello"

    def test_call_tool_error_response(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = (
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Invalid request"}}
            )
            + "\n"
        )
        with pytest.raises(RuntimeError, match="Invalid request"):
            client.call_tool("bad_tool", {})

    def test_call_tool_is_error_flag(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": "tool failed"}],
                    },
                }
            )
            + "\n"
        )
        with pytest.raises(RuntimeError, match="tool failed"):
            client.call_tool("failing_tool", {})

    def test_call_tool_is_error_no_content(self):
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        client._proc.stdout.readline.return_value = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": []}})
            + "\n"
        )
        with pytest.raises(RuntimeError, match="unknown error"):
            client.call_tool("failing_tool", {})

    def test_close_with_sandbox_cleanup(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient

        cleanup_file = tmp_path / "sandbox_cleanup"
        cleanup_file.write_text("temp")
        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.terminate = MagicMock()
        client._proc.wait = MagicMock()
        client._proc.returncode = 0
        client._sandbox_cleanup = str(cleanup_file)
        client.close()
        assert not cleanup_file.exists()

    def test_close_timeout_kills(self):
        import subprocess

        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.terminate = MagicMock()
        client._proc.wait = MagicMock(side_effect=[subprocess.TimeoutExpired("cmd", 5), None])
        client._proc.kill = MagicMock()
        client._sandbox_cleanup = None
        client.close()
        client._proc.kill.assert_called_once()

    def test_init_popen_failure_cleans_sandbox(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {"mcpServers": {"test": {"command": "nonexistent_binary_xyz", "args": []}}}
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(Exception):
                McpToolClient("test")

    def test_init_handshake_failure_closes(self, tmp_path):
        from kiro_crew.cron_script import McpToolClient

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {"mcpServers": {"test": {"command": "echo", "args": ["bye"]}}}
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(Exception):
                McpToolClient("test")


class TestResolveMcpServerAimFallback:
    """Tests for _resolve_mcp_server AIM agent spec fallback."""

    def test_aim_fallback_path(self, tmp_path):
        # No kirocrew.json, but a fallback agent spec exists
        _resolve_mcp_server.cache_clear()
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {"mcpServers": {"builder-mcp": {"command": "npx", "args": ["-y", "builder-mcp"]}}}
        (agents_dir / "my-kirocrew-agent.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _resolve_mcp_server("builder-mcp")
        # Fallback path returns the same (argv, env) shape; no env block -> {}.
        assert result == (("npx", "-y", "builder-mcp"), {})

    def test_server_not_in_config(self, tmp_path):
        _resolve_mcp_server.cache_clear()
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {"mcpServers": {"other-server": {"command": "node", "args": []}}}
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _resolve_mcp_server("nonexistent")
        assert result is None


class TestScriptContextCallToolSuccess:
    """Tests for ScriptContext.call_tool() success path with mocked McpToolClient."""

    def test_call_tool_success_path(self, tmp_path):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_client = MagicMock()
        mock_client.call_tool.return_value = "result text"
        with patch("kiro_crew.cron_script.McpToolClient", return_value=mock_client), patch(
            "kiro_crew.cron_script.sel"
        ) as mock_sel:
            result = ctx.call_tool("server", "tool", {"key": "val"})
        assert result == "result text"
        mock_client.close.assert_called_once()
        mock_sel().log_tool_invocation.assert_called()

    def test_call_tool_error_path(self, tmp_path):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_client = MagicMock()
        mock_client.call_tool.side_effect = RuntimeError("connection failed")
        with patch("kiro_crew.cron_script.McpToolClient", return_value=mock_client), patch(
            "kiro_crew.cron_script.sel"
        ):
            with pytest.raises(RuntimeError, match="connection failed"):
                ctx.call_tool("server", "tool", {})
        mock_client.close.assert_called_once()

    def test_audit_tool_call_sel_failure_swallowed(self, tmp_path):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_client = MagicMock()
        mock_client.call_tool.return_value = ""
        with patch("kiro_crew.cron_script.McpToolClient", return_value=mock_client), patch(
            "kiro_crew.cron_script.sel"
        ) as mock_sel:
            mock_sel().log_tool_invocation.side_effect = Exception("SEL down")
            # Should not raise despite SEL failure
            result = ctx.call_tool("server", "tool", {})
        assert result == ""


class TestScriptContextPost:
    """Tests for ScriptContext._post() HTTP helper."""

    def test_post_success(self):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"status": "delivered"}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("kiro_crew.cron_script.loopback_urlopen", return_value=mock_response):
            result = ctx._post("/api/deliver", {"text": "hello"})
        assert result == {"status": "delivered"}

    def test_post_failure_returns_error(self):
        job = SimpleNamespace(id="j1", message="test")
        ctx = ScriptContext(job=job)
        with patch(
            "kiro_crew.cron_script.loopback_urlopen", side_effect=Exception("connection refused")
        ):
            result = ctx._post("/api/deliver", {"text": "hello"})
        assert "error" in result
        assert "connection refused" in result["error"]


class TestRunCommandSandboxedExceptions:
    """Tests for run_command_sandboxed timeout and exception paths."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch, posix_test_shell):
        """See TestRunCommandSandboxed._passthrough_sandbox."""
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        # Bypass the runtime shell probe (which itself spawns a child): these
        # tests exercise the run_command_sandboxed plumbing, not shell fingerprinting.
        monkeypatch.setattr(
            "kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell
        )

    def test_timeout_returns_error(self):
        # Real subprocess: communicate(timeout=1) fires and the child is killed.
        result = run_command_sandboxed("sleep 30", timeout=1)
        assert result["status"] == "error"
        assert "timed out" in result["output"]

    def test_exception_returns_error(self):
        with patch("subprocess.Popen", side_effect=OSError("No such file")):
            result = run_command_sandboxed("nonexistent_binary")
        assert result["status"] == "error"
        assert "failed" in result["output"].lower() or "No such file" in result["output"]


class TestMcpToolClientInitFailures:
    """Tests for McpToolClient.__init__ failure paths."""

    def test_popen_exception_cleans_sandbox(self, tmp_path):
        """Lines 172-175: Popen raises, sandbox cleanup file is removed."""
        from kiro_crew.cron_script import McpToolClient

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        config = {"mcpServers": {"test": {"command": "node", "args": ["nonexistent.js"]}}}
        (agents_dir / "kirocrew.json").write_text(json.dumps(config))
        # Mock wrap_argv to return a cleanup file
        cleanup = tmp_path / "cleanup_marker"
        cleanup.write_text("x")
        with patch("pathlib.Path.home", return_value=tmp_path), patch(
            "kiro_crew.cron_script.wrap_argv", return_value=(["false"], str(cleanup))
        ), patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            with pytest.raises(OSError, match="spawn failed"):
                McpToolClient("test")
        assert not cleanup.exists()

    def test_rpc_no_response_in_1000_messages(self):
        """Line 214: server sends 1000+ messages without matching ID."""
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.stdin = MagicMock()
        client._proc.stdout = MagicMock()
        client._req_id = 0
        # Return messages with wrong IDs
        client._proc.stdout.readline.return_value = '{"jsonrpc":"2.0","id":999,"result":{}}\n'
        with pytest.raises(RuntimeError, match="did not respond"):
            client._rpc("tools/list")

    def test_close_exception_swallowed(self):
        """Lines 235-236: exception in terminate is swallowed."""
        from kiro_crew.cron_script import McpToolClient

        client = object.__new__(McpToolClient)
        client._proc = MagicMock()
        client._proc.terminate = MagicMock(side_effect=ProcessLookupError("already dead"))
        client._sandbox_cleanup = None
        # Should not raise
        client.close()


class TestResolveScriptPathSensitive:
    """Test for is_sensitive_path block (line 275)."""

    def test_sensitive_path_blocked(self, tmp_path):
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "test.py"
        script.write_text("def run(ctx): pass")
        with patch("pathlib.Path.home", return_value=tmp_path), patch(
            "kiro_crew.cron_script.is_sensitive_path", return_value=True
        ):
            with pytest.raises(PermissionError, match="security policy"):
                resolve_script_path(str(script) + ":run")


class TestRunScriptSandboxedErrorPaths:
    """Tests for run_script_sandboxed subprocess error paths (lines 333, 337-338)."""

    def _write_script(self, tmp_path, code):
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        script = crons_dir / "err_test.py"
        script.write_text(code)
        return str(script)

    def test_nonzero_exit_no_stdout(self, tmp_path):
        """Line 333: subprocess fails with no stdout."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = ("", "segfault")
        with patch(
            "kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")
        ), patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)), patch(
            "subprocess.Popen", return_value=mock_proc
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "segfault" in result.get("error", "")

    def test_bad_json_output(self, tmp_path):
        """Lines 337-338: stdout is not valid JSON."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = ("not json at all\n", "")
        with patch(
            "kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")
        ), patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)), patch(
            "subprocess.Popen", return_value=mock_proc
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "Bad output" in result.get("error", "")

    def test_bad_json_output_redacts_before_truncating(self, tmp_path):
        """A credential straddling the 200-char boundary must not leak its head.

        Redaction must run on the WHOLE stdout before the head slice: slicing
        first can cut a secret mid-pattern, so redaction no longer matches and
        the raw head reaches the diagnostic.
        """
        # Built at runtime so no credential-shaped literal lands in the repo.
        fake_key = "AKIA" + "B" * 16  # matches the AWS access-key-id pattern
        # Sized so the 200-char head window ends 10 chars into the credential:
        # a slice-then-redact order keeps "AKIA" + 6 B's — too short for the
        # pattern to match, so the raw head leaks into the diagnostic.
        padding = "p" * 190
        stdout_text = padding + fake_key + "e" * 50
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (stdout_text, "")
        with patch(
            "kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")
        ), patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)), patch(
            "subprocess.Popen", return_value=mock_proc
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "")
        assert result["status"] == "error"
        assert "AKIA" not in result["error"]
        assert fake_key not in result["error"]
        # Positive proof the payload flowed through redaction (not a vacuous
        # pass on an empty diagnostic): the 200-char head window ends 10 chars
        # into the replacement marker, so its head survives the slice.
        assert "[REDACTED:" in result["error"]
        assert padding in result["error"]


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
        with patch("kiro_crew.mcp_cron.config_dir", return_value=tmp_path), patch(
            "kiro_crew.mcp_cron.CronService"
        ) as mock_svc:
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
        with patch("kiro_crew.mcp_cron.config_dir", return_value=tmp_path), patch(
            "kiro_crew.mcp_cron.CronService"
        ) as mock_svc:
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
        with patch("kiro_crew.mcp_cron.config_dir", return_value=tmp_path), patch(
            "kiro_crew.mcp_cron.CronService"
        ) as mock_svc, patch("kiro_crew.mcp_cron._resolve_session_key", return_value=""):
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
        with patch("kiro_crew.mcp_cron.config_dir", return_value=tmp_path), patch(
            "kiro_crew.mcp_cron.CronService"
        ) as mock_svc, patch("kiro_crew.mcp_cron._resolve_session_key", return_value=""), patch(
            "pathlib.Path.home", return_value=tmp_path
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


class TestRunScriptSandboxedTimeout:
    """Test for run_script_sandboxed TimeoutExpired handler."""

    def test_script_timeout_returns_error(self):
        import subprocess as sp

        mock_proc = MagicMock()
        # CRITICAL: a bare MagicMock pid coerces to 1 via __index__, and the
        # timeout cleanup path would then run os.killpg(1, SIGKILL) ==
        # kill(-1, SIGKILL) — SIGKILLing every process this uid owns.
        # Always give mocked Popen objects a real, nonexistent int pid.
        mock_proc.pid = 2**22 + 12345  # > PID_MAX default, never a real pid
        mock_proc.communicate.side_effect = [sp.TimeoutExpired("cmd", 30), ("", "")]
        with patch(
            "kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")
        ), patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)), patch(
            "subprocess.Popen", return_value=mock_proc
        ), patch(
            # Stub the reap at the shim, NOT via the global subprocess.Popen
            # patch above. On Windows _kill_proc_group reaps through
            # platform_compat.kill_process_tree, which shells out to `taskkill
            # /T /F` with subprocess.run — and subprocess.run builds its child
            # from subprocess.Popen, so the patch aimed at the launcher spawn
            # also hijacks taskkill's, handing subprocess.run a MagicMock whose
            # communicate() returns a mock instead of a 2-tuple (ValueError:
            # not enough values to unpack). The timeout HANDLER is what is under
            # test; the kill mechanism has its own coverage in
            # TestKillBroadcastGuard.
            "kiro_crew.platform_compat.kill_process_tree",
            return_value=True,
        ) as mock_tree:
            result = run_script_sandboxed("/f.py:run", "j1", "", timeout=30)
        assert result["status"] == "error"
        assert "timed out" in result["error"]
        assert "30s" in result["error"]
        # communicate() does not kill the child on timeout, so the handler MUST
        # reap it — otherwise a timed-out cron leaks a live subprocess tree.
        if pc.IS_POSIX:
            # POSIX takes the os.killpg branch when a pgid resolves; the mocked
            # pid does not exist, so _resolve_safe_pgid returns None and the
            # shim is the fallback. Either way the process must be signalled.
            assert mock_tree.called or mock_proc.kill.called
        else:
            mock_tree.assert_called_once_with(mock_proc.pid, pc.SIGKILL)


class TestKillBroadcastGuard:
    """_resolve_safe_pgid must never let a kill path degenerate into kill(-1)."""

    def test_mock_pid_refused(self):
        from kiro_crew.cron_script import _resolve_safe_pgid
        assert _resolve_safe_pgid(MagicMock()) is None  # MagicMock pid -> not int

    def test_pid_one_refused(self):
        from kiro_crew.cron_script import _resolve_safe_pgid
        proc = MagicMock()
        proc.pid = 1
        assert _resolve_safe_pgid(proc) is None

    def test_negative_pid_refused(self):
        from kiro_crew.cron_script import _resolve_safe_pgid
        proc = MagicMock()
        proc.pid = -1
        assert _resolve_safe_pgid(proc) is None

    def test_bool_pid_refused(self):
        from kiro_crew.cron_script import _resolve_safe_pgid
        proc = MagicMock()
        proc.pid = True  # bool is an int subclass; type() check must reject it
        assert _resolve_safe_pgid(proc) is None

    def test_own_pgid_refused(self):
        import os

        from kiro_crew.cron_script import _resolve_safe_pgid
        proc = MagicMock()
        proc.pid = os.getpid()  # our own group -> must refuse (self-kill)
        assert _resolve_safe_pgid(proc) is None

    def test_kill_proc_group_never_calls_killpg_for_mock(self):
        """A bare-mock pid must never reach a group kill — the original footgun.

        ``os.killpg`` does not exist on Windows, so patching it by name raises
        AttributeError there. Use ``create=True``: the assertion we care about
        is that the attribute is never *called*, which holds on both platforms
        (on Windows ``_resolve_safe_pgid`` returns None so the killpg branch is
        unreachable, and ``kill_process_tree`` takes the taskkill path).
        """
        from kiro_crew.cron_script import _kill_proc_group
        proc = MagicMock()  # bare mock pid — the original footgun
        with patch("os.killpg", create=True) as mock_killpg, patch(
            # Windows: _kill_proc_group reaps via taskkill before the
            # single-process fallback. Stub it so the test never shells out.
            "kiro_crew.platform_compat.kill_process_tree",
            side_effect=OSError("stubbed"),
        ):
            _kill_proc_group(proc)
        mock_killpg.assert_not_called()
        proc.kill.assert_called_once()

    @pytest.mark.skipif(
        not pc.IS_POSIX,
        reason="POSIX broadcast guard (killpg/getpgid); Windows takes the taskkill path",
    )
    def test_shim_kill_process_tree_refuses_mock_pid(self):
        """platform_compat.kill_process_tree: non-int pid must raise, never killpg."""
        import pytest as _pytest

        from kiro_crew import platform_compat
        with patch("os.killpg") as mock_killpg, patch("os.kill") as mock_kill:
            with _pytest.raises(ValueError):
                platform_compat.kill_process_tree(MagicMock(), platform_compat.SIGKILL)
        mock_killpg.assert_not_called()
        mock_kill.assert_not_called()

    @pytest.mark.skipif(
        not pc.IS_POSIX,
        reason="POSIX broadcast guard (killpg/getpgid); Windows takes the taskkill path",
    )
    def test_shim_kill_process_tree_broadcast_pgid_degrades_to_pid_kill(self):
        """platform_compat.kill_process_tree: pgid<=1 degrades to scoped os.kill."""
        from kiro_crew import platform_compat
        target = 2**22 + 31337
        with patch("os.getpgid", return_value=1), \
             patch("os.killpg") as mock_killpg, \
             patch("os.kill") as mock_kill:
            assert platform_compat.kill_process_tree(target, platform_compat.SIGKILL) is True
        mock_killpg.assert_not_called()
        mock_kill.assert_called_once_with(target, platform_compat.SIGKILL)

    @pytest.mark.skipif(
        pc.IS_POSIX, reason="Windows taskkill /T branch of kill_process_tree"
    )
    def test_shim_kill_process_tree_uses_taskkill_on_windows(self):
        """Windows has no process groups: the shim must reap via ``taskkill /T``.

        This is the Windows counterpart to the two POSIX broadcast-guard tests
        above — same invariant (kill the whole tree, never broadcast), different
        mechanism, so it needs its own coverage rather than a bare skip.
        """
        import ntpath

        from kiro_crew import platform_compat
        target = 2**22 + 31337
        completed = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with patch("subprocess.run", return_value=completed) as mock_run:
            assert platform_compat.kill_process_tree(target, platform_compat.SIGKILL) is True
        argv = mock_run.call_args.args[0]
        # argv[0] may be a bare name or an absolute trusted-system path
        # (kill_process_tree resolves taskkill from GetSystemDirectoryW rather
        # than trusting %PATH%), so match on the basename and its .exe suffix.
        assert ntpath.basename(argv[0]).lower() in ("taskkill", "taskkill.exe")
        assert "/T" in argv and "/F" in argv  # whole tree, forced
        assert str(target) in argv

    def test_cancel_flag_cleared_when_terminate_fails(self):
        """kill_running_process: signal never delivered -> flag must not leak.

        Otherwise a later natural completion would be misreported as cancelled
        by _unregister_proc.
        """
        from kiro_crew.cron_script import (
            _CANCELLED_PROC_JOBS,
            _RUNNING_PROCS,
            kill_running_process,
        )
        proc = MagicMock()
        proc.pid = 2**22 + 54321  # nonexistent real int pid
        proc.poll.return_value = None  # "still running"
        proc.terminate.side_effect = OSError("terminate failed")
        _RUNNING_PROCS["flagleak"] = proc
        try:
            assert kill_running_process("flagleak") is False
            assert "flagleak" not in _CANCELLED_PROC_JOBS
        finally:
            _RUNNING_PROCS.pop("flagleak", None)
            _CANCELLED_PROC_JOBS.discard("flagleak")


class TestResolveInternalSecret:
    """Secret resolution falls back to .local_secret when env is unset."""

    def test_uses_env_when_set(self, tmp_path):
        with patch.dict(os.environ, {"KIROCREW_INTERNAL_SECRET": "fromenv"}), patch(
            "kiro_crew.config.loader.config_dir", return_value=tmp_path
        ):
            (tmp_path / ".local_secret").write_text("fromfile")
            assert _resolve_internal_secret(5476) == "fromenv"

    def test_falls_back_to_local_secret_file(self, tmp_path):
        (tmp_path / ".local_secret").write_text("filesecret\n")
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_INTERNAL_SECRET"}
        with patch.dict(os.environ, env, clear=True), patch(
            "kiro_crew.config.loader.config_dir", return_value=tmp_path
        ):
            assert _resolve_internal_secret(5476) == "filesecret"

    def test_empty_when_neither_present(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_INTERNAL_SECRET"}
        with patch.dict(os.environ, env, clear=True), patch(
            "kiro_crew.config.loader.config_dir", return_value=tmp_path
        ):
            assert _resolve_internal_secret(5476) == ""

    def test_env_empty_string_falls_back_to_file(self, tmp_path):
        (tmp_path / ".local_secret").write_text("filesecret")
        with patch.dict(os.environ, {"KIROCREW_INTERNAL_SECRET": ""}), patch(
            "kiro_crew.config.loader.config_dir", return_value=tmp_path
        ):
            assert _resolve_internal_secret(5476) == "filesecret"

    def test_run_script_writes_local_secret_to_sandbox_when_env_unset(self, tmp_path):
        """End-to-end: run_script_sandboxed hands the sandbox the .local_secret value."""
        (tmp_path / ".local_secret").write_text("realsecret\n")
        captured = {}

        def fake_popen(argv, **kwargs):
            sf = kwargs.get("env", {}).get("_KIROCREW_SECRET_FILE")
            captured["secret"] = open(sf).read() if sf else None
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate.return_value = ('{"status": "ok"}', "")
            return proc

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_INTERNAL_SECRET"}
        with patch.dict(os.environ, env, clear=True), patch(
            "kiro_crew.config.loader.config_dir", return_value=tmp_path
        ), patch("kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")), patch(
            "kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)
        ), patch(
            "subprocess.Popen", side_effect=fake_popen
        ):
            run_script_sandboxed("/f.py:run", "j1", "", timeout=30)
        assert captured["secret"] == "realsecret"


class TestPostKillDrainTimeoutHardening:
    """The post-kill drain must not leak pipes or mislabel the kill-path result.

    ``Popen.communicate`` does not kill the child on timeout, so both spawners
    SIGKILL the process group and then reap it with a bounded
    ``communicate(timeout=5)``. That second drain can ITSELF raise
    ``TimeoutExpired`` when the pipes never reach EOF — the child outlived the
    group kill, or another process still holds the write end open. Two
    invariants have to survive that:

    - the ``PIPE`` fds get closed rather than left open for the life of the
      gateway. ``Popen._communicate`` closes them as a side effect of reaching
      EOF, which is exactly the path it does not reach here.
    - the kill path still reports a *timeout*. In ``run_command_sandboxed`` a
      ``TimeoutExpired`` raised inside the ``except`` block bypasses that
      block's own handler list, so the timed-out ``return`` is skipped and the
      broad ``except Exception`` reports "Command failed" instead.
    """

    def test_script_drain_timeout_closes_pipes_and_keeps_contract(self):
        import subprocess as sp

        mock_proc = MagicMock()
        # A bare MagicMock pid coerces to 1 via __index__, and os.killpg(1, sig)
        # is kill(-1, sig) — see TestKillBroadcastGuard.
        mock_proc.pid = 2**22 + 12345  # > PID_MAX default, never a real pid
        # BOTH the initial read and the post-kill drain time out.
        mock_proc.communicate.side_effect = sp.TimeoutExpired("cmd", 30)
        with patch(
            "kiro_crew.cron_script.resolve_script_path", return_value=("/f.py", "run")
        ), patch("kiro_crew.cron_script.wrap_argv", return_value=(["true"], None)), patch(
            "subprocess.Popen", return_value=mock_proc
        ), patch(
            # Stub the reap at the shim, NOT via the subprocess.Popen patch above:
            # same reason as TestRunScriptSandboxedTimeout.
            "kiro_crew.platform_compat.kill_process_tree",
            return_value=True,
        ):
            result = run_script_sandboxed("/f.py:run", "j1", "", timeout=30)
        # The drain's own timeout must neither escape nor alter the contract.
        assert result == {"status": "error", "error": "Script timed out after 30s"}
        # The fd-leak assertion: both pipes closed even though EOF never came.
        mock_proc.stdout.close.assert_called_once()
        mock_proc.stderr.close.assert_called_once()

    def test_command_drain_timeout_closes_pipes_and_keeps_contract(self, monkeypatch):
        import subprocess as sp

        # See TestRunCommandSandboxed._passthrough_sandbox: bypass the OS-sandbox
        # wrap and the runtime shell probe so this exercises only the kill path.
        monkeypatch.setattr(
            "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: "sh")
        mock_proc = MagicMock()
        mock_proc.pid = 2**22 + 12345  # > PID_MAX default, never a real pid
        mock_proc.communicate.side_effect = sp.TimeoutExpired("cmd", 30)
        with patch("subprocess.Popen", return_value=mock_proc), patch(
            "kiro_crew.platform_compat.kill_process_tree", return_value=True
        ):
            result = run_command_sandboxed("sleep 30", timeout=30)
        # A timed-out command must report the timeout, not "Command failed".
        assert result == {
            "status": "error",
            "output": "❌ Command timed out after 30s",
            "exit_code": -1,
        }
        mock_proc.stdout.close.assert_called_once()
        mock_proc.stderr.close.assert_called_once()
