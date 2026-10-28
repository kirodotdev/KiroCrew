"""Tests for terminal session state.

Covers the registry, the session dataclass, the enabled default, the
live-title helpers and ``poll_terminal_titles``, Bash input readiness, the
shared cwd probe, and the codes on the routes' JSON refusals.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from terminal_handler_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_enabled_cache,
    _make_request,
    _make_session,
)

from kiro_crew.dashboard.handlers import terminal
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import await_until

# ── _get_registry ──


class TestGetRegistry:
    def test_returns_terminal_sessions_from_state(self):
        registry = {"s1": _make_session()}
        req = _make_request(registry=registry)
        result = terminal._get_registry(req)
        assert result is registry


# ── _TerminalSession dataclass ──


class TestTerminalSession:
    def test_defaults(self):
        proc = MagicMock()
        sess = terminal._TerminalSession(
            session_id="t1", master_fd=5, proc=proc  # wokeignore:rule=master
        )
        assert sess.cols == 80
        assert sess.rows == 24
        assert sess.ws is None
        assert sess.reader_task is None
        assert sess.last_ws_disconnect is None
        assert sess.created_at > 0


# ── _is_enabled default ──


class TestIsEnabledDefault:
    """Terminal is enabled by default; an explicit enabled=false still disables it."""

    def test_enabled_by_default_when_key_absent(self):
        req = _make_request()
        # flake-ok: pre-existing cache stamp at monotonic zero that expires the 30 s enable cache only once the host clock reads past 30 s, moved verbatim; tracked for a de-flake pass
        terminal._enabled_cache[1] = 0.0  # bust the 30s cache to force a recompute
        with patch.object(terminal, "_get_config", return_value={}):
            assert terminal._is_enabled(req) is True

    def test_explicit_disable_is_respected(self):
        req = _make_request()
        # flake-ok: pre-existing cache stamp at monotonic zero that expires the 30 s enable cache only once the host clock reads past 30 s, moved verbatim; tracked for a de-flake pass
        terminal._enabled_cache[1] = 0.0
        with patch.object(terminal, "_get_config", return_value={"enabled": False}):
            assert terminal._is_enabled(req) is False


# ── _proc_comm / _proc_cwd (live-title helpers) ──

_HAS_PROC = os.path.isdir("/proc")


@pytest.mark.skipif(not _HAS_PROC, reason="requires Linux /proc")
class TestProcHelpers:
    def test_proc_comm_returns_command_name_for_live_pid(self):
        # Our own process is guaranteed alive; /proc/<pid>/comm is non-empty.
        name = terminal._proc_comm(os.getpid())
        assert name and isinstance(name, str)

    def test_proc_comm_returns_none_for_bogus_pid(self):
        # A pid this large effectively never exists -> open() raises OSError -> None.
        assert terminal._proc_comm(2**30) is None

    def test_proc_cwd_returns_directory_for_live_pid(self):
        cwd = terminal._proc_cwd(os.getpid())
        assert cwd and os.path.isdir(cwd)

    def test_proc_cwd_returns_none_for_bogus_pid(self):
        assert terminal._proc_cwd(2**30) is None


# ── _session_title ──


@pytest.mark.skipif(
    terminal.platform_compat.IS_WINDOWS,
    reason="foreground-command detection uses os.tcgetpgrp (POSIX-only); "
    "_session_title returns None on Windows",
)
class TestSessionTitle:
    """The tab-title label: foreground command name while one runs, else the
    shell's cwd basename. _proc_comm/_proc_cwd and os.tcgetpgrp are patched so
    each branch is exercised deterministically (no real PTY needed)."""

    def _sess(self):
        # _make_session gives master_fd=99 and proc.pid=UNALLOCATABLE_PID.  # wokeignore:rule=master
        return _make_session()

    def test_returns_none_on_non_posix(self):
        with patch.object(terminal.platform_compat, "IS_POSIX", False):
            assert terminal._session_title(self._sess()) is None

    def test_returns_none_when_fd_closed(self):
        sess = self._sess()
        sess.master_fd = -1  # wokeignore:rule=master
        with patch.object(terminal.platform_compat, "IS_POSIX", True):
            assert terminal._session_title(sess) is None

    def test_returns_none_when_tcgetpgrp_raises(self):
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch("os.tcgetpgrp", side_effect=OSError),
        ):
            assert terminal._session_title(self._sess()) is None

    def test_returns_foreground_command_name(self):
        # fg pgid (999) != shell pid (UNALLOCATABLE_PID) -> a command is running.
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch("os.tcgetpgrp", return_value=999),
            patch.object(terminal, "_proc_comm", return_value="vim"),
        ):
            assert terminal._session_title(self._sess()) == "vim"

    def test_falls_back_to_cwd_basename_when_idle(self):
        # fg pgid == shell pid -> at the prompt -> cwd basename.
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch("os.tcgetpgrp", return_value=UNALLOCATABLE_PID),
            patch.object(terminal, "_proc_cwd", return_value="/home/u/my-project"),
        ):
            assert terminal._session_title(self._sess()) == "my-project"

    def test_falls_back_to_cwd_when_comm_unavailable(self):
        # A command is running but /proc/<pgid>/comm couldn't be read.
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch("os.tcgetpgrp", return_value=999),
            patch.object(terminal, "_proc_comm", return_value=None),
            patch.object(terminal, "_proc_cwd", return_value="/tmp/scratch"),
        ):
            assert terminal._session_title(self._sess()) == "scratch"

    def test_returns_none_when_cwd_unavailable(self):
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch("os.tcgetpgrp", return_value=UNALLOCATABLE_PID),
            patch.object(terminal, "_proc_cwd", return_value=None),
        ):
            assert terminal._session_title(self._sess()) is None


# ── shell input readiness ──


class TestBashShellReadiness:
    """Bash readiness is an explicit post-profile signal, never a PTY timing
    or foreground-process-group inference."""

    def test_recognizes_only_bash_executables(self):
        assert terminal._is_bash_shell("/bin/bash") is True
        assert terminal._is_bash_shell("C:\\tools\\bash.exe") is True
        assert terminal._is_bash_shell("/bin/zsh") is False

    def test_ready_hook_is_a_single_shot_self_removing_prompt_command(
        self,
        monkeypatch,
    ):
        monkeypatch.delenv("PROMPT_COMMAND", raising=False)
        env = terminal._bash_ready_env("abc123")

        # The marker rides PROMPT_COMMAND because Bash reads an --init-file only
        # for a NON-login shell, and a non-login shell is exactly what the login guard sees: `shopt -q login_shell` false, so login-guarded profile
        # stanzas never run.
        assert env[terminal._READY_TOKEN_VAR] == "abc123"
        hook = env["PROMPT_COMMAND"]
        assert env[terminal._READY_HOOK_VAR] == hook, "the mirror must be byte-identical"
        # Fires only while the token is set, so no later prompt and no child
        # shell repeats the sequence.
        assert f'-n "${{{terminal._READY_TOKEN_VAR}-}}"' in hook
        assert f"builtin unset {terminal._READY_TOKEN_VAR}" in hook
        assert "]697;KiroCrewReady;%s" in hook
        # Withdraws itself only while PROMPT_COMMAND is still exactly the scalar
        # that was exported: a profile that APPENDED its own command keeps that
        # half, and one that appended as an ARRAY leaves the exported text as
        # element zero, which the scalar test alone would match.
        assert f'"${{PROMPT_COMMAND-}}" == "${{{terminal._READY_HOOK_VAR}-}}"' in hook
        assert '-z "${PROMPT_COMMAND[1]+x}"' in hook
        assert "builtin unset PROMPT_COMMAND; fi" in hook
        # The mirror and the inherited-value carrier are unset either way, so a
        # session whose profile took PROMPT_COMMAND over does not keep them.
        assert (
            f"builtin unset {terminal._READY_HOOK_VAR} " f"{terminal._READY_PREV_VAR}; fi" in hook
        )
        # No Bash 5.1-only syntax: /bin/bash on macOS is 3.2 and must parse this.
        assert "@a}" not in hook and "@A}" not in hook
        # The token is never pasted into the snippet; it is read from the
        # environment, so the hook text carries no secret to echo.
        assert "abc123" not in hook

    def test_ready_hook_preserves_an_inherited_prompt_command(self, monkeypatch):
        """An operator who EXPORTED PROMPT_COMMAND keeps it. Replacing it is data
        loss, not a lost nicety: `PROMPT_COMMAND='history -a'` is what makes
        concurrent shells APPEND to HISTFILE, and without it an exiting shell
        overwrites that file with its own in-memory list."""
        monkeypatch.setenv("PROMPT_COMMAND", "history -a")
        env = terminal._bash_ready_env("abc123")

        exported = env["PROMPT_COMMAND"]
        # The inherited command is carried verbatim and runs AFTER the marker,
        # which must be the first thing the prompt writes.
        assert exported.endswith("; history -a")
        assert exported.index("KiroCrewReady") < exported.index("history -a")
        # The mirror is the WHOLE exported value, so the ownership test still
        # recognizes an untouched variable now that it has a tail.
        assert env[terminal._READY_HOOK_VAR] == exported
        # Withdrawal RESTORES the inherited command instead of unsetting it.
        assert env[terminal._READY_PREV_VAR] == "history -a"
        assert f'PROMPT_COMMAND="${{{terminal._READY_PREV_VAR}}}"' in exported
        assert (
            f"builtin unset {terminal._READY_HOOK_VAR} " f"{terminal._READY_PREV_VAR}" in exported
        )

    def test_ready_hook_unsets_when_nothing_was_inherited(self, monkeypatch):
        monkeypatch.delenv("PROMPT_COMMAND", raising=False)
        env = terminal._bash_ready_env("abc123")

        assert terminal._READY_PREV_VAR not in env
        assert "builtin unset PROMPT_COMMAND" in env["PROMPT_COMMAND"]
        # Nothing to append, so the exported value is the hook alone.
        assert env["PROMPT_COMMAND"].endswith("fi")

    def test_ready_hook_ignores_a_blank_inherited_prompt_command(self, monkeypatch):
        monkeypatch.setenv("PROMPT_COMMAND", "   ")
        env = terminal._bash_ready_env("abc123")

        assert terminal._READY_PREV_VAR not in env
        assert "builtin unset PROMPT_COMMAND" in env["PROMPT_COMMAND"]

    def test_ready_hook_carries_the_inherited_value_unstripped(self, monkeypatch):
        """Blankness is tested on a stripped copy; the value CARRIED is raw.

        Trailing whitespace can be escaped, and stripping it turns the escape
        into a line continuation -- which changes what the command does:
        `printf [x]\\ ` prints `[x] `, while the stripped `printf [x]\\` prints
        `[x]\\`.
        """
        monkeypatch.setenv("PROMPT_COMMAND", "printf [x]\\ ")
        env = terminal._bash_ready_env("abc123")

        assert env[terminal._READY_PREV_VAR] == "printf [x]\\ "
        assert env["PROMPT_COMMAND"].endswith("; printf [x]\\ ")

    def test_marker_match_is_split_safe_and_one_shot(self):
        sess = _make_session()
        sess.shell_ready = False
        sess.ready_marker = b"<random-ready-marker>"

        assert terminal._consume_ready_marker(sess, b"output<random-") is False
        assert sess.shell_ready is False
        assert terminal._consume_ready_marker(sess, b"ready-marker>prompt") is True
        assert sess.shell_ready is True
        assert sess.ready_marker is None
        assert sess.ready_probe == bytearray()
        assert terminal._consume_ready_marker(sess, b"<random-ready-marker>") is False

    def test_unrelated_output_never_releases_the_barrier(self):
        sess = _make_session()
        sess.shell_ready = False
        sess.ready_marker = b"<random-ready-marker>"

        assert terminal._consume_ready_marker(sess, b"profile is still waiting") is False
        assert sess.shell_ready is False


# ── one cwd probe per poll tick ──


@pytest.mark.skipif(
    terminal.platform_compat.IS_WINDOWS,
    reason="the cwd/title probes are POSIX-only (os.tcgetpgrp, /proc, libproc); "
    "both helpers return None on Windows",
)
class TestCwdProbeSharing:
    """The title label and the cwd frame are two consumers of ONE answer: with no
    foreground command the title is just the cwd's basename. On a host where
    _proc_cwd has to fork lsof — no /proc and no libproc — asking twice per tick
    is the entire cost of an otherwise idle terminal, so the session memo has to
    collapse them into one probe."""

    def test_memoizes_within_the_ttl(self):
        sess = _make_session()
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch.object(terminal, "_proc_cwd", return_value="/tmp/a") as probe,
        ):
            assert terminal._session_cwd(sess) == "/tmp/a"
            # flake-ok: pre-existing second probe that must land inside the 0.4 s real-clock memo TTL for the call count to stay at one, moved verbatim; tracked for a de-flake pass
            assert terminal._session_cwd(sess) == "/tmp/a"
        assert probe.call_count == 1

    def test_reprobes_once_the_ttl_expires(self):
        # The memo must not outlive one tick, or a `cd` would take two polls to
        # show up in the tab title.
        sess = _make_session()
        # flake-ok: pre-existing real-clock stamp one second past the memo TTL so the cached cwd reads as expired, moved verbatim; tracked for a de-flake pass
        sess.cwd_probe = (time.monotonic() - terminal._CWD_PROBE_TTL_S - 1, "/tmp/old")
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch.object(terminal, "_proc_cwd", return_value="/tmp/new") as probe,
        ):
            assert terminal._session_cwd(sess) == "/tmp/new"
        assert probe.call_count == 1

    def test_title_and_cwd_frame_share_one_probe(self):
        # Exactly the pair of calls one poll tick makes, in order.
        sess = _make_session()
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch("os.tcgetpgrp", return_value=UNALLOCATABLE_PID),
            patch.object(terminal, "_proc_cwd", return_value="/home/u/proj") as probe,
        ):
            assert terminal._session_title(sess) == "proj"
            # flake-ok: pre-existing cwd read that must land inside the 0.4 s real-clock memo TTL after the title probe to share one probe, moved verbatim; tracked for a de-flake pass
            assert terminal._session_cwd(sess) == "/home/u/proj"
        assert probe.call_count == 1


# ── poll_terminal_titles ──


class TestPollTerminalTitles:
    """One loop iteration is driven by patching asyncio.sleep to return once
    then raise CancelledError (same pattern as the reaper tests)."""

    @staticmethod
    def _app(sess):
        state = MagicMock()
        state._terminal_sessions = {sess.session_id: sess} if sess else {}
        return {"state": state}

    @pytest.mark.asyncio
    async def test_pushes_title_frame_on_change(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", return_value="vim"),
            patch.object(terminal, "_session_cwd", return_value=None),
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))
        assert sess.last_title == "vim"
        ws.send_str.assert_awaited_once()
        assert json.loads(ws.send_str.call_args.args[0]) == {"type": "title", "text": "vim"}

    @pytest.mark.asyncio
    async def test_skips_when_title_unchanged(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        sess.last_title = "vim"
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", return_value="vim"),
            patch.object(terminal, "_session_cwd", return_value=None),
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))
        ws.send_str.assert_not_awaited()
        # The session WAS probed (the dirty flag is spent), so the silence is the
        # dedup, not a skipped probe.
        assert sess.frames_dirty is False
        assert sess.last_title == "vim"

    @pytest.mark.asyncio
    async def test_skips_when_no_title(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", return_value=None),
            patch.object(terminal, "_session_cwd", return_value=None),
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))
        ws.send_str.assert_not_awaited()
        assert sess.frames_dirty is False
        assert (sess.last_title, sess.last_cwd) == (None, None)

    @pytest.mark.asyncio
    async def test_skips_disconnected_session(self):
        sess = _make_session(session_id="s1", ws=None)  # no live socket
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title") as mock_title,
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))
        mock_title.assert_not_called()
        # The pending output stays armed for the socket that reattaches.
        assert sess.frames_dirty is True

    @pytest.mark.asyncio
    async def test_does_not_probe_a_session_with_no_new_output(self):
        # A shell cannot change directory or start a command without writing to
        # the PTY, so a session that has produced nothing since its last probe
        # cannot have gone stale. Probing it anyway is what made an idle
        # terminal cost a forked lsof every second on hosts with no /proc.
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        sess.frames_dirty = False
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title") as title,
            patch.object(terminal, "_session_cwd") as cwd,
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))
        title.assert_not_called()
        cwd.assert_not_called()
        ws.send_str.assert_not_awaited()
        assert (sess.frames_dirty, sess.last_title, sess.last_cwd) == (False, None, None)

    @pytest.mark.asyncio
    async def test_disarms_itself_after_probing(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", return_value="vim"),
            patch.object(terminal, "_session_cwd", return_value="/tmp/x"),
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))
        assert sess.frames_dirty is False

    @pytest.mark.asyncio
    async def test_output_landing_mid_probe_re_arms_the_session(self):
        # The flag is cleared BEFORE the probe, so a write that lands while the
        # probe is in flight is picked up on the next tick. Clearing it after the
        # probe would swallow that write and freeze the title until some later,
        # unrelated output happened to re-arm it.
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)

        def output_lands_mid_probe(s):
            s.frames_dirty = True  # what read_pty does on every PTY read
            return "vim"

        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", side_effect=output_lands_mid_probe),
            patch.object(terminal, "_session_cwd", return_value=None),
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))
        assert sess.frames_dirty is True

    @pytest.mark.asyncio
    async def test_swallows_send_error(self):
        ws = AsyncMock()
        ws.closed = False
        ws.send_str = AsyncMock(side_effect=ConnectionResetError)
        sess = _make_session(session_id="s1", ws=ws)
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", return_value="vim"),
            patch.object(terminal, "_session_cwd", return_value=None),
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))  # must not raise
        # A failed send must not advance the dedup marker: the next dirty tick
        # retries instead of leaving the client on a stale title until the
        # value changes again.
        assert sess.last_title is None

    @pytest.mark.asyncio
    async def test_timed_out_send_is_retried_on_the_next_dirty_tick(self, monkeypatch):
        # flake-ok: pre-existing 20 ms real-time send timeout that cuts the hanging first title send so the next tick retries it, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(terminal, "_OWNER_CONTROL_SEND_TIMEOUT_S", 0.02)
        ws = MagicMock()
        ws.closed = False
        attempts = 0

        async def first_send_hangs(_data):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # flake-ok: pre-existing never-set Event that parks the first title send until the send timeout cancels it, moved verbatim; tracked for a de-flake pass
                await asyncio.Event().wait()

        ws.send_str = AsyncMock(side_effect=first_send_hangs)
        sess = _make_session(session_id="s1", ws=ws)

        def redirty(_delay):
            sess.frames_dirty = True  # PTY output landed between ticks

        real_sleep = asyncio.sleep
        ticks = iter([None, None, asyncio.CancelledError])

        async def fake_sleep(delay):
            step = next(ticks)
            if step is asyncio.CancelledError:
                raise step
            redirty(delay)
            # flake-ok: pre-existing zero-second real sleep that yields one loop turn inside the fake poll sleep, moved verbatim; tracked for a de-flake pass
            await real_sleep(0)

        # flake-ok: pre-existing patch of asyncio.sleep with a fake that re-dirties the session for two ticks and cancels on the third, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", return_value="/repo"),
            patch.object(terminal, "_session_cwd", return_value="/home/u/repo"),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            await await_until(
                asyncio.ensure_future(terminal.poll_terminal_titles(self._app(sess))),
                what="the title poll ending on its cancellation tick",
            )

        # Tick 1: title send timed out -> marker not advanced; cwd send succeeded.
        # Tick 2: title retried and delivered.
        assert sess.last_title == "/repo"
        assert sess.last_cwd == "/home/u/repo"
        frames = [json.loads(call.args[0]) for call in ws.send_str.await_args_list]
        assert [f["type"] for f in frames] == ["title", "cwd", "title"]

    @pytest.mark.asyncio
    async def test_takeover_during_probe_never_sends_to_displaced_socket(self):
        """The poller captures ``sess.ws`` before its executor probe. A takeover
        that lands while the probe runs must leave the displaced socket untouched;
        the new owner gets the title on the next tick (publication re-dirties)."""
        old_ws = MagicMock()
        old_ws.closed = False
        old_ws.send_str = AsyncMock()
        old_ws.close = AsyncMock()
        new_ws = MagicMock()
        new_ws.closed = False
        new_ws.send_bytes = AsyncMock()
        new_ws.send_str = AsyncMock()
        sess = _make_session(session_id="s1", ws=old_ws)
        published = threading.Event()
        real_sleep = asyncio.sleep

        def title_probe(_sess):
            # Executor thread: hold the probe open until the loop has published.
            # flake-ok: pre-existing 5 s bounded Event wait that holds the executor probe open until the loop publishes the new socket, moved verbatim; tracked for a de-flake pass
            published.wait(5)
            return "vim"

        async def publish_during_probe():
            # flake-ok: pre-existing 50 ms real sleep that lets the poller enter its executor probe before the takeover publishes, moved verbatim; tracked for a de-flake pass
            await real_sleep(0.05)
            assert await terminal._replace_terminal_ws(sess, new_ws) is True
            published.set()

        publisher = asyncio.create_task(publish_during_probe())
        ticks = iter([None, None, asyncio.CancelledError])

        async def fake_sleep(_delay):
            step = next(ticks)
            if isinstance(step, type) and issubclass(step, BaseException):
                raise step
            # flake-ok: pre-existing zero-second real sleep that yields one loop turn inside the fake poll sleep, moved verbatim; tracked for a de-flake pass
            await real_sleep(0)

        # flake-ok: pre-existing patch of asyncio.sleep with a fake that runs two poll ticks and cancels the poller on the third, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", side_effect=title_probe),
            patch.object(terminal, "_session_cwd", return_value=None),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            await await_until(
                asyncio.ensure_future(terminal.poll_terminal_titles(self._app(sess))),
                what="the title poll ending on its cancellation tick",
            )
        # flake-ok: pre-existing unbounded await of the publisher task that performs the socket takeover, moved verbatim; tracked for a de-flake pass
        await publisher

        old_frames = [json.loads(call.args[0]) for call in old_ws.send_str.await_args_list]
        assert [f.get("type") for f in old_frames] == ["error"]  # displacement notice only
        assert sess.ws is new_ws
        titles = [
            json.loads(call.args[0])
            for call in new_ws.send_str.await_args_list
            if json.loads(call.args[0]).get("type") == "title"
        ]
        assert titles == [{"type": "title", "text": "vim"}]

    @pytest.mark.asyncio
    async def test_handles_missing_state(self):
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles({"state": None})

    @pytest.mark.asyncio
    async def test_handles_no_terminal_sessions_attr(self):
        state = MagicMock(spec=[])  # no _terminal_sessions attribute
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles({"state": state})

    @pytest.mark.asyncio
    async def test_pushes_cwd_frame_on_change(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", return_value=None),
            patch.object(terminal, "_session_cwd", return_value="/home/u/proj"),
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))
        assert sess.last_cwd == "/home/u/proj"
        ws.send_str.assert_awaited_once()
        assert json.loads(ws.send_str.call_args.args[0]) == {"type": "cwd", "path": "/home/u/proj"}

    @pytest.mark.asyncio
    async def test_skips_when_cwd_unchanged(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        sess.last_cwd = "/home/u/proj"
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", return_value=None),
            patch.object(terminal, "_session_cwd", return_value="/home/u/proj"),
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))
        ws.send_str.assert_not_awaited()
        assert sess.frames_dirty is False
        assert sess.last_cwd == "/home/u/proj"

    @pytest.mark.asyncio
    async def test_pushes_both_title_and_cwd_frames(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", return_value="vim"),
            patch.object(terminal, "_session_cwd", return_value="/tmp/x"),
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))
        frames = [json.loads(c.args[0]) for c in ws.send_str.await_args_list]
        assert {"type": "title", "text": "vim"} in frames
        assert {"type": "cwd", "path": "/tmp/x"} in frames

    @pytest.mark.asyncio
    async def test_survives_ws_detach_during_probe(self):
        # The WS can detach (sess.ws = None) while a blocking probe runs in the
        # executor. The poller must revalidate after the hop — never send on the
        # dead reference, never AttributeError (which would kill the singleton
        # task for every terminal until restart).
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)

        def detach_and_return_title(s):
            s.ws = None  # disconnect lands mid-probe
            return "vim"

        # flake-ok: pre-existing patch of asyncio.sleep that runs one poll tick and ends the title poller on its second sleep, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_session_title", side_effect=detach_and_return_title),
            patch.object(terminal, "_session_cwd", return_value="/tmp/x"),
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.poll_terminal_titles(self._app(sess))  # must not raise
        ws.send_str.assert_not_awaited()
        # Nothing was delivered, so neither dedup marker may claim it was.
        assert (sess.ws, sess.last_title, sess.last_cwd) == (None, None, None)


class TestTerminalRefusalCodes:
    """Every JSON refusal from the terminal routes carries a machine-readable
    ``code`` (contract gate: ``test/test_error_code_contract.py``).

    Only the JSON routes are in scope. ``api_terminal_ws``, create's auth/enable
    guards, and delete answer ``web.Response(text=...)`` — a plain-text contract
    that carries no envelope to put a code in, and giving them one would change
    their content type rather than complete this one.
    """

    @staticmethod
    def _body(resp):
        return json.loads(resp.body)

    @pytest.mark.asyncio
    async def test_create_refuses_over_the_session_cap_with_a_code(self):
        registry = {"s1": _make_session(), "s2": _make_session()}
        req = _make_request(registry=registry)
        with (
            patch.object(
                terminal, "_get_config", return_value={"enabled": True, "max_sessions": 2}
            ),
            patch.object(terminal, "_sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 429
        assert self._body(resp)["code"] == "terminal_max_sessions"

    @pytest.mark.asyncio
    async def test_redact_refusals_are_coded(self):
        cases = [
            ({"text": 42}, 400, "terminal_invalid_body"),
            ({"text": "x" * (terminal._REDACT_MAX_BYTES + 1)}, 413, "terminal_selection_too_large"),
        ]
        for body, status, code in cases:
            req = _make_request()
            req.json = AsyncMock(return_value=body)
            with patch.object(terminal, "_is_enabled", return_value=True):
                resp = await terminal.api_terminal_redact(req)
            assert resp.status == status, body
            assert self._body(resp)["code"] == code, body

    @pytest.mark.asyncio
    async def test_a_failed_redaction_is_coded_and_still_fails_closed(self):
        """The 500 must name the operation without carrying the selection: this
        route exists to keep a credential out of the model's input, so its own
        failure must not put the text back in the response."""
        secret = "aws_secret_" "access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        req = _make_request()
        req.json = AsyncMock(return_value={"text": secret})
        with (
            patch.object(terminal, "_is_enabled", return_value=True),
            patch.object(terminal, "redact_exfiltration_urls", side_effect=RuntimeError),
        ):
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 500
        assert self._body(resp)["code"] == "terminal_redaction_failed"
        assert "wJalrXUtnFEMI" not in resp.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "status", "reason"),
        [
            ({"session_id": 42}, 400, "invalid_body"),
            ({"session_id": "s1", "token": "x" * (4096 + 1)}, 413, "token_too_long"),
            ({"session_id": "missing"}, 404, "unknown_session"),
        ],
    )
    async def test_complete_refusal_code_is_the_audited_reason(
        self, body: dict, status: int, reason: str
    ) -> None:
        """`_log_complete` already records a fixed reason word for every refusal
        and the response then dropped it. The code is that word, so the audit
        trail and the API cannot drift apart."""
        req = _make_request(registry={})
        req.json = AsyncMock(return_value=body)
        with (
            patch.object(terminal, "_is_enabled", return_value=True),
            patch.object(terminal, "_sel") as mock_sel,
        ):
            log = MagicMock()
            mock_sel.return_value.log_api_access = log
            resp = await terminal.api_terminal_complete(req)

        assert resp.status == status
        assert self._body(resp)["code"] == f"terminal_{reason}"
        assert log.call_args.kwargs["resources"] == reason

    @pytest.mark.asyncio
    async def test_every_json_refusal_carries_a_code_and_its_prose(self):
        """Per-file ratchet: no JSON refusal may regress to prose-only."""
        collected = []

        req = _make_request()
        req.json = AsyncMock(return_value={"text": 42})
        with patch.object(terminal, "_is_enabled", return_value=True):
            collected.append(await terminal.api_terminal_redact(req))

        req = _make_request(registry={})
        req.json = AsyncMock(return_value={"session_id": "missing"})
        with (
            patch.object(terminal, "_is_enabled", return_value=True),
            patch.object(terminal, "_sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            collected.append(await terminal.api_terminal_complete(req))

        for resp in collected:
            body = self._body(resp)
            assert resp.status >= 400, body
            assert isinstance(body.get("code"), str) and body["code"], body
            assert isinstance(body.get("error"), str) and body["error"], body
