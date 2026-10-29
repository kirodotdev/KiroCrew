"""Tests for the terminal's shell child process and the signals it inherits.

Covers the SIGINT normalization of the CI ``run-as-runner`` wrapper (an
inherited ``SIG_IGN`` through ``env`` and Bash), the environment the
terminal's shell child inherits, exit-status resolution, the end of the PTY
reader loop, abnormal-exit notifications, and a real shell exiting end to
end.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from terminal_handler_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_enabled_cache,
    _make_app,
    _make_request,
    _make_session,
)

from kiro_crew.dashboard.handlers import terminal
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.mark.skipif(sys.platform != "linux", reason="Linux runner uses GNU env")
@pytest.mark.parametrize("normalize", [False, True])
def test_runner_sigint_normalization_preserves_other_signals(tmp_path, normalize):
    """Exercise inherited SIG_IGN -> env -> Bash -> pytest without root.

    The runuser transition itself belongs to the real non-root boundary job;
    this probe executes its post-transition signal boundary at our current uid.
    """
    import shlex
    import subprocess

    import yaml

    action = (
        pathlib.Path(__file__).resolve().parents[1] / ".github/actions/run-as-runner/action.yml"
    )
    setup = yaml.safe_load(action.read_text())["runs"]["steps"][0]["run"]
    wrapper = setup.rsplit("#!/bin/bash", 1)[1].split("\nEOF", 1)[0]
    command = shlex.split(wrapper.split("exec ", 1)[1], comments=True)
    assert command[:6] == ["runuser", "-m", "-u", "runner", "--", "env"]
    assert command[6] == "--default-signal=INT"
    boundary = command[5:7] if normalize else command[5:6]

    probe = tmp_path / "test_signal_boundary.py"
    probe.write_text(
        "import os, signal, subprocess\n"
        "def test_signal_boundary():\n"
        f"    assert (os.getuid(), os.geteuid()) == {(os.getuid(), os.geteuid())!r}\n"
        "    assert signal.getsignal(signal.SIGINT) != signal.SIG_IGN\n"
        "    assert signal.getsignal(signal.SIGUSR1) == signal.SIG_IGN\n"
        "    result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c',\n"
        "                             'kill -INT $$; exit 42'], timeout=5)\n"
        "    assert result.returncode == -signal.SIGINT\n"
    )
    launcher = (
        "import os, signal, sys; "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        "signal.signal(signal.SIGUSR1, signal.SIG_IGN); "
        "os.execvp(sys.argv[1], sys.argv[1:])"
    )
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            launcher,
            *boundary,
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            'exec "$@"',
            "signal-probe",
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            "/dev/null",
            # No conftest above the probe's own directory. ``-c /dev/null`` roots
            # the inner session at ``/dev``, so pytest would otherwise still load
            # every conftest.py between the probe and ``/`` -- and on a host whose
            # temp dir is inside this checkout that is the repository conftest,
            # which registers an xdist hook the autoload-disabled child cannot
            # validate (INTERNALERROR, exit 3). ``--confcutdir`` is pytest's own
            # bound on that walk.
            "--confcutdir",
            str(tmp_path),
            "-o",
            "cache_dir=" + str(tmp_path / "cache"),
            str(probe),
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == (0 if normalize else 1), result.stdout + result.stderr
    assert ("1 passed" if normalize else "signal.getsignal(signal.SIGINT)") in result.stdout


class TestPtyChildEnvStripsPythonStartupVars:
    """``PYTHONPATH``/``PYTHONHOME``/``PYTHONPYCACHEPREFIX`` are searched BEFORE a
    venv's own site-packages, so leaking the gateway's copies into an interactive
    shell makes a user's Python 3.13 venv import Kiro Crew's 3.12 site-packages
    and its C extensions fail to load. The agent surface already strips them
    (``sandbox.scrub_agent_subprocess_env``); these pin the terminal surface,
    which was never brought into line.
    """

    def test_python_startup_vars_are_dropped(self, monkeypatch):
        monkeypatch.setenv("PYTHONPATH", "/gateway/site-packages")
        monkeypatch.setenv("PYTHONHOME", "/gateway/python3.12")
        monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/gateway/pycache")
        monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")

        env = terminal._pty_child_env({"TERM": "xterm-256color", "KIROCREW_TERMINAL": "1"})

        assert "PYTHONPATH" not in env
        assert "PYTHONHOME" not in env
        assert "PYTHONPYCACHEPREFIX" not in env
        assert env["KIROCREW_TERMINAL"] == "1"
        assert env["TERM"] == "xterm-256color"
        assert env["KIROCREW_UNRELATED_KEEPME"] == "keep-this-value"

    def test_macos_bash_deprecation_banner_is_silenced(self, monkeypatch):
        """macOS ships Bash 3.2, which prints a three-line "use zsh" notice on
        every interactive start. The panel silences it, and yields to a user who
        set the variable themselves."""
        monkeypatch.delenv("BASH_SILENCE_DEPRECATION_WARNING", raising=False)
        assert terminal._pty_child_env({})["BASH_SILENCE_DEPRECATION_WARNING"] == "1"

        monkeypatch.setenv("BASH_SILENCE_DEPRECATION_WARNING", "")
        assert terminal._pty_child_env({})["BASH_SILENCE_DEPRECATION_WARNING"] == ""

    def test_credential_bearing_vars_survive(self, monkeypatch):
        """Only the Python prefixes are dropped. This is the user's own
        unsandboxed shell, so borrowing the AGENT spawn's credential scrub would
        break git-over-SSH and the AWS CLI inside the panel."""
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-abc/agent.1")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "FAKE-token")
        monkeypatch.setenv("PYTHONPATH", "/gateway/site-packages")

        env = terminal._pty_child_env({"KIROCREW_TERMINAL": "1"})

        assert env["SSH_AUTH_SOCK"] == "/tmp/ssh-abc/agent.1"
        assert env["AWS_SESSION_TOKEN"] == "FAKE-token"
        assert "PYTHONPATH" not in env

    @pytest.mark.asyncio
    async def test_posix_pty_spawn_env_has_no_python_vars(self, monkeypatch):
        """End-to-end through the POSIX branch: assert on the env actually handed
        to the spawn, so rebuilding the dict in place is caught. The spawn is
        made to fail AFTER the call is recorded so no read loop starts."""
        monkeypatch.setenv("PYTHONPATH", "/gateway/site-packages")
        monkeypatch.setenv("PYTHONHOME", "/gateway/python3.12")
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(terminal.shutil, "which", lambda c: c if c == "/bin/zsh" else None)

        registry: dict = {}
        req = _make_request(registry=registry, session_id="posix-pyenv")
        req.query = MagicMock()
        req.query.get = lambda *a, **k: None

        ws = AsyncMock()
        ws.closed = False

        fds = os.pipe()  # real fds so the cleanup os.close() calls succeed
        spawn = AsyncMock(side_effect=RuntimeError("stop before read loop"))
        cfg = {"enabled": True, "shell": "/bin/zsh"}
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch.object(terminal.platform_compat, "IS_WINDOWS", False),
            patch.object(terminal._pty, "openpty", return_value=fds),
            patch.object(terminal.fcntl, "ioctl", lambda *a: None),
            patch.object(terminal.asyncio, "create_subprocess_exec", spawn),
            patch.object(terminal, "_get_config", return_value=cfg),
            patch.object(terminal.web, "WebSocketResponse", return_value=ws),
            patch.object(terminal, "_sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await terminal.api_terminal_ws(req)

        spawn.assert_awaited_once()
        env = spawn.call_args.kwargs["env"]
        assert "PYTHONPATH" not in env
        assert "PYTHONHOME" not in env
        assert env["KIROCREW_TERMINAL"] == "1"
        assert env["TERM"] == "xterm-256color"
        assert env["SHELL"] == "/bin/zsh"

    @pytest.mark.asyncio
    async def test_conpty_spawn_env_has_no_python_vars(self, monkeypatch, tmp_path):
        """The Windows ConPTY branch IS reachable on Linux: ``IS_WINDOWS`` is a
        module attribute and ``WindowsPty`` is a thin pywinpty wrapper the suite
        already fakes, so the same code path runs here."""
        monkeypatch.setenv("PYTHONPATH", "/gateway/site-packages")
        monkeypatch.setenv("PYTHONHOME", "/gateway/python3.12")

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        monkeypatch.setattr(terminal.platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", True)

        captured: dict = {}

        class _FakeWinPty:
            def __init__(self, argv, cwd=None, env=None, cols=80, rows=24):
                captured["env"] = env
                # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing mock pid moved verbatim; the process double is never signalled
                self.pid = 4321
                self._reads = iter((b"PS> ", b""))

            def read(self, size=4096):
                return next(self._reads)

            def write(self, data):
                return len(data)

            def resize(self, cols, rows):
                pass

            def isalive(self):
                return True

            def terminate(self, force=True):
                pass

        monkeypatch.setattr("kiro_crew.conpty.WindowsPty", _FakeWinPty)

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/win-pyenv") as ws:
                # Wait for the SPAWN, not for a frame. ``captured["env"]`` is
                # filled synchronously inside _FakeWinPty.__init__, so the
                # assertion's input exists the moment the handler reaches the
                # ConPTY branch. Waiting on ``ws.receive(timeout=3)`` instead
                # ties this contract test to how fast the host delivers the
                # first PTY frame, and a receive whose timeout is cancelled
                # from outside raises CancelledError straight out of both
                # ``async with`` blocks rather than a TimeoutError this test
                # could report. Polling the spawn is deterministic and needs no
                # wall-clock margin.
                await async_wait_until(
                    lambda: "env" in captured,
                    describe=lambda: "the ConPTY branch never spawned",
                )
                await ws.close()

        if "win-pyenv" in registry:
            await terminal._kill_session(registry["win-pyenv"])

        env = captured["env"]
        assert "PYTHONPATH" not in env
        assert "PYTHONHOME" not in env
        assert env["KIROCREW_TERMINAL"] == "1"


class TestChildExitStatus:
    """Exit status resolution on both backends. ``None`` means the status is
    unavailable -- the child outlived the reap bound, or the binding could not
    report one."""

    @pytest.mark.asyncio
    async def test_returns_recorded_status_without_waiting(self):
        """The child watcher has already reaped it, so there is nothing to await
        and the recorded code is authoritative."""
        sess = _make_session(alive=False)
        sess.proc.returncode = 3
        assert await terminal._child_exit_status(sess) == 3
        sess.proc.wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_waits_for_the_child_watcher_then_reports(self):
        """PTY EOF can beat the child watcher, so an unresolved status is waited
        for rather than reported unavailable."""
        sess = _make_session()  # returncode None -> must await proc.wait()

        async def _settle():
            sess.proc.returncode = 0

        sess.proc.wait = AsyncMock(side_effect=_settle)
        assert await terminal._child_exit_status(sess) == 0
        sess.proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_posix_branch_honors_the_shared_reap_bound(self, monkeypatch):
        """A child that never goes must not park the reader for good. The
        ``elapsed`` assertion is load-bearing: it is the only thing proving the
        wait reads ``_CHILD_REAP_TIMEOUT_S`` rather than a hardcoded bound."""
        monkeypatch.setattr(terminal, "_CHILD_REAP_TIMEOUT_S", 0.02)
        sess = _make_session()

        async def _never_settles():
            # flake-ok: pre-existing 5 s stand-in for a child that outlives the reap bound, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(5)

        sess.proc.wait = AsyncMock(side_effect=_never_settles)
        # flake-ok: pre-existing real-clock start stamp for the elapsed ceiling that proves the POSIX reap honours the patched bound, moved verbatim; tracked for a de-flake pass
        started = time.monotonic()
        assert await terminal._child_exit_status(sess) is None
        # flake-ok: pre-existing real-clock read of how long the POSIX reap took against the patched 0.02 s bound, moved verbatim; tracked for a de-flake pass
        elapsed = time.monotonic() - started
        # flake-ok: pre-existing 1 s wall-clock ceiling on the POSIX reap that proves it reads the patched bound, moved verbatim; tracked for a de-flake pass
        assert elapsed < 1.0, (
            f"the POSIX reap waited {elapsed:.2f}s against a patched "
            "_CHILD_REAP_TIMEOUT_S of 0.02s, so it is not reading that constant"
        )

    @pytest.mark.asyncio
    async def test_reports_the_conpty_code_for_a_child_already_gone(self):
        """A dead ConPTY child reports the wrapper's code, and the POSIX fields
        are not read as a substitute, since they belong to a process this
        session never spawned."""
        sess = _make_session(alive=False)
        sess.proc.returncode = 99  # must be ignored: winpty owns this session
        sess.winpty = MagicMock()
        sess.winpty.isalive.return_value = False
        sess.winpty.exitstatus.return_value = 7
        assert await terminal._child_exit_status(sess) == 7
        sess.proc.wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unavailable_when_the_conpty_binding_reports_no_code(self):
        """An older binding lacks the property; the wrapper answers ``None``."""
        sess = _make_session(alive=False)
        sess.winpty = MagicMock()
        sess.winpty.isalive.return_value = False
        sess.winpty.exitstatus.return_value = None
        assert await terminal._child_exit_status(sess) is None

    @pytest.mark.asyncio
    async def test_waits_for_the_conpty_child_then_reports(self):
        """No child watcher to await on Windows, so liveness is polled: a child
        still winding down is waited for, and its code read once it is gone."""
        sess = _make_session()
        sess.winpty = MagicMock()
        sess.winpty.isalive.side_effect = [True, True, False]
        sess.winpty.exitstatus.return_value = 0
        assert await terminal._child_exit_status(sess) == 0
        assert sess.winpty.isalive.call_count == 3

    @pytest.mark.asyncio
    async def test_unavailable_when_the_conpty_child_outlives_the_bound(self, monkeypatch):
        """An immortal ConPTY child answers ``None`` whatever the bound, so only
        the timing proves the poll honours ``_CHILD_REAP_TIMEOUT_S``."""
        monkeypatch.setattr(terminal, "_CHILD_REAP_TIMEOUT_S", 0.02)
        monkeypatch.setattr(terminal, "_CONPTY_STATUS_POLL_S", 0.001)
        sess = _make_session()
        sess.winpty = MagicMock()
        sess.winpty.isalive.return_value = True
        # flake-ok: pre-existing real-clock start stamp for the elapsed ceiling that proves the ConPTY poll honours the patched bound, moved verbatim; tracked for a de-flake pass
        started = time.monotonic()
        assert await terminal._child_exit_status(sess) is None
        # flake-ok: pre-existing real-clock read of how long the ConPTY poll took against the patched 0.02 s bound, moved verbatim; tracked for a de-flake pass
        elapsed = time.monotonic() - started
        # flake-ok: pre-existing 1 s wall-clock ceiling on the ConPTY poll that proves it reads the patched bound, moved verbatim; tracked for a de-flake pass
        assert elapsed < 1.0, (
            f"the ConPTY reap polled for {elapsed:.2f}s against a patched "
            "_CHILD_REAP_TIMEOUT_S of 0.02s, so it is not reading that constant"
        )

    @pytest.mark.asyncio
    async def test_unavailable_without_a_child(self):
        """A session whose spawn failed has no process to ask."""
        sess = _make_session()
        sess.proc = None
        assert await terminal._child_exit_status(sess) is None


class _ClientSocket:
    """What the browser tab sees: the control frames it received and how it closed.

    The real ``_send_owner_control_frame`` and ``_close_terminal_ws_bounded`` drive
    it, so a frame here is one the handler actually put on the wire.
    """

    def __init__(self):
        self.closed = False
        self.frames: list[dict] = []
        self.close_code: int | None = None

    async def send_str(self, data):
        self.frames.append(json.loads(data))

    async def close(self, code=None):
        self.closed = True
        self.close_code = code


class TestHandlePtyReaderEnd:
    """The reader loop ends for two reasons that must not be conflated: the
    shell exiting (tell the window, close the tab, reap) and a deliberate
    teardown that closed the controller fd underneath it (do nothing -- its own
    path already accounted for the session)."""

    @pytest.mark.asyncio
    async def test_tells_the_owner_closes_and_reaps_an_exited_shell_in_order(self):
        ws = MagicMock()
        sess = _make_session(alive=False, ws=ws)
        sess.proc.returncode = 0
        registry = {sess.session_id: sess}
        events: list[str] = []
        with (
            patch.object(
                terminal,
                "_send_owner_control_frame",
                AsyncMock(side_effect=lambda *_args: events.append("send") or True),
            ) as send,
            patch.object(
                terminal,
                "_close_terminal_ws_bounded",
                AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("close")),
            ) as close,
            patch.object(terminal, "_kill_session", AsyncMock()) as kill,
        ):
            await terminal._handle_pty_reader_end(registry, sess)
        send.assert_awaited_once_with(sess, ws, {"type": "exit", "status": 0})
        close.assert_awaited_once_with(ws, close_code=terminal._TERMINAL_WS_CLOSE_SHELL_EXITED)
        assert events == ["send", "close"]
        kill.assert_awaited_once_with(sess)
        assert sess.session_id not in registry

    @pytest.mark.asyncio
    async def test_a_failed_exit_frame_still_closes_with_the_shell_exit_code(self):
        """The close independently tells a backpressured client not to redial,
        so the coded close cannot be conditional on the frame landing."""
        ws = MagicMock()
        ws.closed = False
        ws.close = AsyncMock()
        sess = _make_session(alive=False, ws=ws)
        sess.proc.returncode = 0
        registry = {sess.session_id: sess}
        with (
            patch.object(
                terminal, "_send_owner_control_frame", AsyncMock(return_value=False)
            ) as send,
            patch.object(terminal, "_kill_session", AsyncMock()) as kill,
        ):
            await terminal._handle_pty_reader_end(registry, sess)
        send.assert_awaited_once_with(sess, ws, {"type": "exit", "status": 0})
        ws.close.assert_awaited_once_with(code=terminal._TERMINAL_WS_CLOSE_SHELL_EXITED)
        kill.assert_awaited_once_with(sess)
        assert sess.session_id not in registry

    @pytest.mark.asyncio
    async def test_an_unavailable_status_is_reported_as_null(self):
        """A dead child whose code cannot be read is distinct from a code of 0,
        so the frame carries null rather than inventing one."""
        ws = _ClientSocket()
        sess = _make_session(alive=False, ws=ws)
        sess.proc = None  # no child to ask: the status is unavailable
        registry = {sess.session_id: sess}
        with patch.object(terminal, "_kill_session", AsyncMock()) as kill:
            await terminal._handle_pty_reader_end(registry, sess)
        assert ws.frames == [{"type": "exit", "status": None}]
        assert ws.close_code == terminal._TERMINAL_WS_CLOSE_SHELL_EXITED
        assert registry == {}
        kill.assert_awaited_once_with(sess)

    @pytest.mark.asyncio
    async def test_silent_when_the_session_was_deliberately_torn_down(self):
        """DELETE and the orphan sweep pop the entry BEFORE closing the fd, so
        the resulting OSError must not reach the client as an exit."""
        ws = _ClientSocket()
        sess = _make_session(alive=False, ws=ws)
        registry: dict = {}  # already popped by the teardown path
        with patch.object(terminal, "_kill_session", AsyncMock()) as kill:
            await terminal._handle_pty_reader_end(registry, sess)
        assert (ws.frames, ws.closed) == ([], False)
        assert sess.ws is ws
        kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_silent_when_the_entry_is_popped_while_the_status_resolves(self):
        """The status wait is the widest window in this path: a DELETE landing
        inside it has already disposed of the session, so continuing would send
        an exit for a tab its own teardown closed, ring the bell for a shell the
        user killed themselves, and reap a freed fd twice. A non-zero status is
        used deliberately -- the note is published before the lock, so it is the
        one effect a guard inside the lock cannot hold back."""
        ws = _ClientSocket()
        sess = _make_session(alive=False, ws=ws)
        registry = {sess.session_id: sess}
        bus = MagicMock()

        async def pop_during_status(_sess):
            registry.pop(_sess.session_id, None)
            return 7

        with (
            patch.object(terminal, "_child_exit_status", side_effect=pop_during_status),
            patch.object(terminal, "_kill_session", AsyncMock()) as kill,
        ):
            await terminal._handle_pty_reader_end(registry, sess, bus=bus)
        assert (ws.frames, ws.closed) == ([], False)
        kill.assert_not_awaited()
        bus.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_silent_when_a_replacement_takes_the_slot_during_the_status_wait(self):
        """A dial that found the entry dead installs a fresh session under the
        same id. Reaping here would tear down that live replacement."""
        sess = _make_session(alive=False, ws=MagicMock())
        replacement = _make_session(alive=True, ws=MagicMock())
        replacement.session_id = sess.session_id
        registry = {sess.session_id: sess}
        bus = MagicMock()

        async def replace_during_status(_sess):
            registry[_sess.session_id] = replacement
            return 7

        with (
            patch.object(terminal, "_child_exit_status", side_effect=replace_during_status),
            patch.object(terminal, "_send_owner_control_frame", AsyncMock()) as send,
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()) as close,
            patch.object(terminal, "_kill_session", AsyncMock()) as kill,
        ):
            await terminal._handle_pty_reader_end(registry, sess, bus=bus)
        send.assert_not_awaited()
        close.assert_not_awaited()
        kill.assert_not_awaited()
        bus.push.assert_not_called()
        assert registry == {sess.session_id: replacement}

    @pytest.mark.asyncio
    async def test_leaves_a_live_child_alone(self):
        """A closed fd we did not initiate, with the child still running:
        reaping here would kill a live shell."""
        sess = _make_session(alive=True, ws=MagicMock())
        registry = {sess.session_id: sess}
        with (
            patch.object(terminal, "_send_owner_control_frame", AsyncMock()) as send,
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()) as close,
            patch.object(terminal, "_kill_session", AsyncMock()) as kill,
        ):
            await terminal._handle_pty_reader_end(registry, sess)
        send.assert_not_awaited()
        close.assert_not_awaited()
        kill.assert_not_awaited()
        assert registry[sess.session_id] is sess

    @pytest.mark.asyncio
    async def test_reaps_without_an_owner_socket(self):
        """A detached window learns nothing here; the session is still reaped
        rather than left for the orphan sweep."""
        sess = _make_session(alive=False, ws=None)
        sess.proc.returncode = 0
        registry = {sess.session_id: sess}
        with (
            patch.object(terminal, "_send_owner_control_frame", AsyncMock()) as send,
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()) as close,
            patch.object(terminal, "_kill_session", AsyncMock()) as kill,
        ):
            await terminal._handle_pty_reader_end(registry, sess)
        send.assert_not_awaited()
        close.assert_not_awaited()
        kill.assert_awaited_once_with(sess)
        assert sess.session_id not in registry

    @pytest.mark.asyncio
    async def test_drops_the_reader_reference_before_teardown(self):
        """This runs INSIDE sess.reader_task; leaving the reference set makes
        _kill_session cancel the task mid-teardown and leak the fd."""
        sess = _make_session(alive=False, ws=None)
        sess.proc.returncode = 0
        sess.reader_task = MagicMock()
        registry = {sess.session_id: sess}
        seen: list = []
        with patch.object(
            terminal,
            "_kill_session",
            AsyncMock(side_effect=lambda s: seen.append(s.reader_task)),
        ):
            await terminal._handle_pty_reader_end(registry, sess)
        assert seen == [None]

    @pytest.mark.asyncio
    async def test_a_takeover_queued_during_the_exit_send_cannot_publish_first(self):
        """``replace_lock`` serializes this path against reconnect publication.
        A dial queued behind the exit frame must not install itself as owner
        while that frame is in flight, or the exit would go to a socket that is
        already displaced and the new window would hold a reaped session."""
        old_ws = MagicMock()
        new_ws = MagicMock()
        new_ws.closed = False
        new_ws.send_bytes = AsyncMock()
        new_ws.send_str = AsyncMock()
        sess = _make_session(alive=False, ws=old_ws)
        sess.proc.returncode = 5
        registry = {sess.session_id: sess}
        send_started = asyncio.Event()
        release_send = asyncio.Event()

        async def delayed_send(_sess, ws, _frame):
            assert _sess is sess and ws is old_ws
            send_started.set()
            # flake-ok: pre-existing unbounded Event wait that parks the fake exit-frame send until the test releases it, moved verbatim; tracked for a de-flake pass
            await release_send.wait()
            return True

        with (
            patch.object(
                terminal, "_send_owner_control_frame", AsyncMock(side_effect=delayed_send)
            ) as send,
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()) as close,
            patch.object(terminal, "_kill_session", AsyncMock()) as kill,
        ):
            handling = asyncio.create_task(terminal._handle_pty_reader_end(registry, sess))
            await await_until(send_started, what="the exit frame's send starting")
            takeover = asyncio.create_task(terminal._replace_terminal_ws(sess, new_ws))
            # flake-ok: pre-existing zero-second yield that lets the queued takeover reach replace_lock before the test asserts it is still pending, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
            assert not takeover.done(), "the takeover bypassed replace_lock"
            assert sess.ws is old_ws
            new_ws.send_str.assert_not_awaited()
            release_send.set()
            await await_until(handling, what="the reader-end handling finishing")
            # The reap completed under the lock, so the queued dial finds the
            # entry gone and its handler answers with a fresh session.
            assert sess.session_id not in registry
            await await_until(takeover, what="the queued takeover finishing")

        send.assert_awaited_once_with(sess, old_ws, {"type": "exit", "status": 5})
        # The queued takeover closes the displaced socket too, with its own
        # stale-owner frame. The coded close is the one that ran first.
        assert close.await_args_list[0].args == (old_ws,)
        assert close.await_args_list[0].kwargs == {
            "close_code": terminal._TERMINAL_WS_CLOSE_SHELL_EXITED
        }
        kill.assert_awaited_once_with(sess)
        assert sess.session_id not in registry


class TestAbnormalExitNotification:
    """A non-zero exit is published to the bell feed from the reap path, the one
    place that knows the exit status and the shell that produced it. A clean or
    unavailable status stays silent, and a bus that refuses never stops the reap.
    """

    @staticmethod
    def _bus(notes: list):
        from kiro_crew.notifications.bus import NotificationBus

        return NotificationBus(sink=notes.append)

    def test_system_terminal_is_a_registered_default_channel(self):
        from kiro_crew.notifications.bus import SYSTEM_CHANNELS

        assert SYSTEM_CHANNELS["system.terminal"] == "default"

    @pytest.mark.asyncio
    async def test_a_non_zero_exit_publishes_one_note_naming_the_code(self):
        notes: list = []
        sess = _make_session(alive=False, ws=MagicMock())
        sess.proc.returncode = 137
        sess.shell = "/bin/zsh"
        registry = {sess.session_id: sess}
        with (
            patch.object(terminal, "_send_owner_control_frame", AsyncMock(return_value=True)),
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()),
            patch.object(terminal, "_kill_session", AsyncMock()),
        ):
            await terminal._handle_pty_reader_end(registry, sess, bus=self._bus(notes))
        assert len(notes) == 1
        note = notes[0]
        assert note["channel"] == "system.terminal"
        assert note["kind"] == "terminal"
        assert note["source"] == "system"
        assert note["priority"] == "default"
        assert note["title"] == "Terminal exited abnormally"
        assert note["body"] == "The shell exited with code 137."
        assert note["group_key"] == "terminal-exit"
        assert note["status"] == 137

    @pytest.mark.asyncio
    async def test_the_note_names_nothing_about_the_session(self):
        """The feed reaches every dashboard session, not only the terminal's
        owner, so no session id, shell path or directory may ride on it."""
        notes: list = []
        sess = _make_session(alive=False, ws=None)
        sess.proc.returncode = 2
        sess.shell = "/bin/zsh"
        sess.last_cwd = "/home/dev/secret-project"
        registry = {sess.session_id: sess}
        with patch.object(terminal, "_kill_session", AsyncMock()):
            await terminal._handle_pty_reader_end(registry, sess, bus=self._bus(notes))
        assert len(notes) == 1
        rendered = json.dumps(notes[0])
        for leak in (sess.session_id, "/bin/zsh", "zsh", "secret-project"):
            assert leak not in rendered

    def test_a_death_by_signal_names_the_signal(self):
        """asyncio reports a child killed by a signal as a negative code."""
        assert terminal._shell_failure_note(-9)[1] == ("The shell was killed by SIGKILL.")
        assert terminal._shell_failure_note(-250)[1] == ("The shell was killed by signal 250.")

    @pytest.mark.asyncio
    async def test_a_clean_exit_publishes_nothing(self):
        """Ringing the bell on every ``exit`` tells the user nothing about
        whether their shell crashed."""
        notes: list = []
        sess = _make_session(alive=False, ws=MagicMock())
        sess.proc.returncode = 0
        registry = {sess.session_id: sess}
        with (
            patch.object(terminal, "_send_owner_control_frame", AsyncMock(return_value=True)),
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()),
            patch.object(terminal, "_kill_session", AsyncMock()),
        ):
            await terminal._handle_pty_reader_end(registry, sess, bus=self._bus(notes))
        assert notes == []

    @pytest.mark.asyncio
    async def test_an_unavailable_status_publishes_nothing(self):
        """A binding that reports no code would ring the bell on every Windows
        close if unavailable were treated as abnormal."""
        notes: list = []
        sess = _make_session(alive=False, ws=MagicMock())
        sess.proc = None  # ConPTY sessions carry no asyncio child process
        sess.winpty = MagicMock()
        sess.winpty.isalive.return_value = False
        sess.winpty.exitstatus.return_value = None
        registry = {sess.session_id: sess}
        with (
            patch.object(terminal, "_send_owner_control_frame", AsyncMock(return_value=True)),
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()),
            patch.object(terminal, "_kill_session", AsyncMock()),
        ):
            await terminal._handle_pty_reader_end(registry, sess, bus=self._bus(notes))
        assert notes == []

    @pytest.mark.asyncio
    async def test_a_failed_conpty_exit_publishes_its_code(self):
        notes: list = []
        sess = _make_session(alive=False, ws=MagicMock())
        sess.proc = None
        sess.winpty = MagicMock()
        sess.winpty.isalive.return_value = False
        sess.winpty.exitstatus.return_value = 5
        registry = {sess.session_id: sess}
        with (
            patch.object(terminal, "_send_owner_control_frame", AsyncMock(return_value=True)),
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()),
            patch.object(terminal, "_kill_session", AsyncMock()),
        ):
            await terminal._handle_pty_reader_end(registry, sess, bus=self._bus(notes))
        assert len(notes) == 1
        assert notes[0]["body"] == "The shell exited with code 5."

    @pytest.mark.asyncio
    async def test_publishes_even_when_no_socket_is_attached(self):
        """A background shell that dies with its tab already gone still leaves a
        record: the feed is the only place the user can learn about it."""
        notes: list = []
        sess = _make_session(alive=False, ws=None)
        sess.proc.returncode = 1
        registry = {sess.session_id: sess}
        with patch.object(terminal, "_kill_session", AsyncMock()):
            await terminal._handle_pty_reader_end(registry, sess, bus=self._bus(notes))
        assert len(notes) == 1
        assert notes[0]["body"] == "The shell exited with code 1."

    @pytest.mark.asyncio
    async def test_a_refusing_bus_never_stops_the_reap(self):
        """The feed is advisory; the fd and the registry entry are not."""
        sess = _make_session(alive=False, ws=MagicMock())
        sess.proc.returncode = 2
        registry = {sess.session_id: sess}
        bus = MagicMock()
        bus.push.side_effect = RuntimeError("bus down")
        with (
            patch.object(terminal, "_send_owner_control_frame", AsyncMock(return_value=True)),
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()),
            patch.object(terminal, "_kill_session", AsyncMock()) as kill,
        ):
            await terminal._handle_pty_reader_end(registry, sess, bus=bus)
        bus.push.assert_called_once()
        kill.assert_awaited_once_with(sess)
        assert sess.session_id not in registry

    @pytest.mark.asyncio
    async def test_without_a_bus_the_reap_is_unchanged(self):
        sess = _make_session(alive=False, ws=MagicMock())
        sess.proc.returncode = 3
        registry = {sess.session_id: sess}
        with (
            patch.object(terminal, "_send_owner_control_frame", AsyncMock(return_value=True)),
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()),
            patch.object(terminal, "_kill_session", AsyncMock()) as kill,
        ):
            await terminal._handle_pty_reader_end(registry, sess)
        kill.assert_awaited_once_with(sess)
        assert sess.session_id not in registry


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX PTY path; the ConPTY claim has its own suite",
)
@pytest.mark.xdist_group("pty_integration")
class TestShellExitIntegration:
    """End-to-end through the shipped PTY read loop with a real native shell,
    proving the wiring the unit tests above bypass."""

    @pytest.mark.asyncio
    async def test_a_real_shell_exiting_tells_the_window_and_reaps_the_session(
        self, monkeypatch, tmp_path
    ):
        home = tmp_path / "home"
        home.mkdir()
        empty_startup = home / "empty-startup"
        empty_startup.write_text("")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("HISTFILE", str(home / ".shell_history"))
        # /bin/sh is the required POSIX shell interface on every supported
        # runner. Pin every common startup/history carrier to the isolated home
        # so the test cannot execute or modify operator files.
        shell = "/bin/sh"
        monkeypatch.setenv("SHELL", shell)
        monkeypatch.setenv("ENV", str(empty_startup))
        monkeypatch.setenv("BASH_ENV", str(empty_startup))
        monkeypatch.setenv("INPUTRC", str(empty_startup))
        monkeypatch.setenv("ZDOTDIR", str(home))
        command = b"exit 3\n"
        expected_status = 3

        # _resolve_shell validates even configured absolute paths. Keep that
        # production call in place but make its answer exact and independent of
        # PATH: only the path selected above is accepted.
        monkeypatch.setattr(
            terminal.shutil,
            "which",
            lambda candidate: shell if candidate == shell else None,
        )

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"dashboard": {"terminal": {"enabled": True, "shell": shell}}})
        )
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        # flake-ok: pre-existing 0.0 cache stamp the enabled check compares with the real monotonic clock so the 30 s window reads as expired, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(terminal, "_enabled_cache", [True, 0.0])
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        notes: list = []
        from kiro_crew.notifications.bus import NotificationBus

        registry: dict = {}
        app = _make_app(registry=registry)
        app["state"].notification_bus = NotificationBus(sink=notes.append)

        from aiohttp.test_utils import TestClient, TestServer

        exit_frame = None
        close_code = None
        sent = False
        try:
            async with TestClient(TestServer(app)) as client:
                async with client.ws_connect(
                    "/api/ws/terminal/exit-sess", params={"cwd": str(home)}
                ) as ws:
                    # Wait for the prompt bytes, not the ready frame, which
                    # /bin/sh can get before its startup finishes.
                    loop = asyncio.get_running_loop()
                    # flake-ok: pre-existing real-clock deadline 30 s out that bounds the wait for the shell's exit frame and close, moved verbatim; tracked for a de-flake pass
                    deadline = loop.time() + 30
                    # flake-ok: pre-existing real-clock read that ends the receive loop once the 30 s deadline passes, moved verbatim; tracked for a de-flake pass
                    while loop.time() < deadline:
                        # flake-ok: pre-existing real-clock read that sizes each receive timeout from what is left of the 30 s deadline, moved verbatim; tracked for a de-flake pass
                        msg = await ws.receive(timeout=deadline - loop.time())
                        if msg.type == web.WSMsgType.BINARY and not sent:
                            await ws.send_bytes(command)
                            sent = True
                            continue
                        if msg.type == web.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            if payload.get("type") == "exit":
                                exit_frame = payload
                        elif msg.type in (
                            web.WSMsgType.CLOSE,
                            web.WSMsgType.CLOSING,
                            web.WSMsgType.CLOSED,
                            web.WSMsgType.ERROR,
                        ):
                            close_code = ws.close_code
                            break
        finally:
            leftover = registry.get("exit-sess")
            if leftover is not None:
                await terminal._kill_session(leftover)

        assert sent, "the native shell never reached its startup barrier"
        assert exit_frame is not None, "the shell exited but no exit frame reached the client"
        assert exit_frame["status"] == expected_status
        assert close_code == terminal._TERMINAL_WS_CLOSE_SHELL_EXITED
        # Reaped as the frame is sent, rather than left to the orphan sweep.
        assert "exit-sess" not in registry
        assert [note["body"] for note in notes] == [
            f"The shell exited with code {expected_status}."
        ]
