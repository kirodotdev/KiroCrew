"""Tests for the built-in CLI terminal panel handlers.

This file holds owner authorization, the shell, config and cwd resolvers,
session teardown (``_kill_session``), and the create and redact routes. The
sibling ``test_terminal_handler_*.py`` files hold the rest of the handler's
tests, and ``terminal_handler_helpers.py`` holds the builders and the
autouse fixture the whole family shares.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from terminal_handler_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_enabled_cache,
    _make_request,
    _make_session,
)

from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers import terminal
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import async_wait_until, await_until


class TestTerminalOwnerAuthorization:
    @pytest.mark.parametrize(
        "handler",
        [
            terminal.api_terminal_ws,
            terminal.api_terminal_create,
            terminal.api_terminal_redact,
            terminal.api_terminal_complete,
            terminal.api_terminal_delete,
            terminal.api_terminal_list,
        ],
    )
    @pytest.mark.asyncio
    async def test_non_owner_is_denied_before_terminal_capability(self, handler):
        req = _make_request(user="intruder", owner_id="owner")

        response = await handler(req)

        assert response.status == 403
        assert json.loads(response.body)["code"] == "owner_only"


# ── _resolve_shell ──


class TestAgentCanRewrite:
    """Ownership, not current mode bits, decides whether a path is rewritable: the
    owner of a 0555 directory can chmod it writable in one syscall."""

    def test_a_directory_the_caller_owns_is_rewritable_even_at_mode_0555(self, tmp_path, request):
        d = tmp_path / "bin"
        d.mkdir()
        d.chmod(0o555)
        request.addfinalizer(lambda: d.chmod(0o755))
        assert terminal._agent_can_rewrite(str(d), os.geteuid()) is True

    def test_a_directory_owned_by_someone_else_is_not(self, tmp_path, request):
        d = tmp_path / "bin"
        d.mkdir()
        d.chmod(0o555)
        request.addfinalizer(lambda: d.chmod(0o755))
        assert terminal._agent_can_rewrite(str(d), os.geteuid() + 1) is False

    def test_a_rewritable_ancestor_taints_the_path(self, tmp_path, request):
        outer = tmp_path / "outer"
        inner = outer / "bin"
        inner.mkdir(parents=True)
        inner.chmod(0o555)
        request.addfinalizer(lambda: inner.chmod(0o755))
        # The caller owns `outer`, so it can rename it and substitute everything
        # underneath, whatever `inner`'s own bits say.
        assert terminal._agent_can_rewrite(str(inner), os.geteuid()) is True

    def test_a_missing_path_fails_closed(self, tmp_path):
        assert terminal._agent_can_rewrite(str(tmp_path / "nope"), os.geteuid()) is True


class TestResolveFenceShells:
    """Fence-shell discovery adds no trusted location and consults no PATH: each
    name is probed inside the directory the session's own shell came from, and
    nothing the gateway's user could rewrite is offered -- a reported path is
    invoked later, when the user confirms, so anything it owns is swappable then."""

    def _foreign_uid(self, monkeypatch):
        """Run as a uid that owns nothing under tmp_path, so a real directory can
        stand in for a system one without needing root to create it."""
        monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)

    def _sysdir(self, tmp_path, names, request=None):
        """A read-only directory of read-only executables, like /usr/bin.

        Restores the directory to a writable mode on teardown via a finalizer:
        left at 0o555, pytest's own tmp_path cleanup cannot unlink entries
        inside it, so it renames the tree to a `garbage-<uuid>` directory under
        the shared pytest temp root and leaves it there forever. `request` is
        optional so a caller with no fixture request (there are none left, but
        this keeps the helper safe to call standalone) still gets a directory,
        just without the guaranteed restore.
        """
        d = tmp_path / "bin"
        d.mkdir(parents=True)
        for name in names:
            p = d / name
            p.write_text("#!/bin/sh\n")
            p.chmod(0o555)
        d.chmod(0o555)
        if request is not None:
            request.addfinalizer(lambda: d.chmod(0o755))
        return d

    def test_reports_shells_beside_the_launched_one(self, tmp_path, monkeypatch, request):
        d = self._sysdir(tmp_path, ("bash", "zsh", "fish"), request)
        self._foreign_uid(monkeypatch)
        found = terminal._resolve_fence_shells(str(d / "bash"))
        assert found == {
            "bash": str(d / "bash"), "zsh": str(d / "zsh"), "fish": str(d / "fish"),
        }

    def test_ignores_a_shell_in_another_directory(self, tmp_path, monkeypatch, request):
        d = self._sysdir(tmp_path, ("bash",), request)
        elsewhere = self._sysdir(tmp_path / "other", ("fish",), request)
        assert (elsewhere / "fish").exists()
        self._foreign_uid(monkeypatch)
        assert terminal._resolve_fence_shells(str(d / "bash")) == {"bash": str(d / "bash")}

    def test_probes_the_directory_rather_than_the_search_path(self, tmp_path, monkeypatch, request):
        shims = self._sysdir(tmp_path / "s", ("fish",), request)
        d = self._sysdir(tmp_path / "r", ("bash", "fish"), request)
        monkeypatch.setenv("PATH", str(shims))
        self._foreign_uid(monkeypatch)
        found = terminal._resolve_fence_shells(str(d / "bash"))
        assert found["fish"] == str(d / "fish")

    def test_offers_nothing_from_a_directory_the_gateway_user_owns(self, tmp_path, request):
        # The swap window, and the Homebrew/workspace prefix case: the caller owns
        # this directory, so read-only mode bits are one chmod from irrelevant.
        d = self._sysdir(tmp_path, ("bash", "fish"), request)
        assert terminal._resolve_fence_shells(str(d / "bash")) == {}

    def test_skips_a_world_writable_candidate(self, tmp_path, monkeypatch, request):
        d = self._sysdir(tmp_path, ("bash",), request)
        d.chmod(0o755)
        (d / "fish").write_text("#!/bin/sh\n")
        (d / "fish").chmod(0o757)  # anyone may overwrite it before invocation
        d.chmod(0o555)
        self._foreign_uid(monkeypatch)
        found = terminal._resolve_fence_shells(str(d / "bash"))
        assert "fish" not in found
        assert found == {"bash": str(d / "bash")}

    def test_skips_a_symlinked_candidate(self, tmp_path, monkeypatch, request):
        d = self._sysdir(tmp_path, ("bash",), request)
        target = self._sysdir(tmp_path / "elsewhere", ("fish",), request)
        d.chmod(0o755)
        (d / "fish").symlink_to(target / "fish")
        d.chmod(0o555)
        self._foreign_uid(monkeypatch)
        found = terminal._resolve_fence_shells(str(d / "bash"))
        assert "fish" not in found

    def test_reports_nothing_without_a_launched_shell(self):
        assert terminal._resolve_fence_shells("") == {}


class TestResolveShell:
    """Shell resolution: configured → $SHELL (POSIX) → platform default, each
    candidate validated as an executable, and the value returned is the path
    `which` RESOLVED — never the bare candidate, which the spawn's project cwd
    could re-resolve differently. `which` is pinned in every test so outcomes
    never depend on what the CI host has installed."""

    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch):
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", False)

    def _pin_which(self, monkeypatch, mapping):
        monkeypatch.setattr(terminal.shutil, "which", lambda c: mapping.get(c))

    def test_configured_executable_wins(self, monkeypatch):
        self._pin_which(monkeypatch, {"/opt/fish": "/opt/fish", "/bin/bash": "/bin/bash"})
        assert terminal._resolve_shell({"shell": "/opt/fish"}) == ("/opt/fish", None)

    def test_bare_name_pinned_to_resolved_path(self, monkeypatch):
        # The RESOLVED path is returned, not the bare name: the spawn runs in
        # the session's project cwd, where a relative PATH entry could resolve
        # the same bare name to a project-planted executable.
        self._pin_which(monkeypatch, {"fish": "/usr/local/bin/fish"})
        assert terminal._resolve_shell({"shell": "fish"}) == ("/usr/local/bin/fish", None)

    def test_relative_which_result_anchored_to_absolute(self, monkeypatch):
        # A RELATIVE PATH entry (PATH=bin:…) makes `which` itself return a
        # relative path, which the spawn cwd would re-resolve — the return
        # must be anchored to the gateway cwd the validation ran in, on both
        # the configured and the fallback branch.
        monkeypatch.setenv("SHELL", "zsh")
        self._pin_which(monkeypatch, {"fish": "bin/fish", "zsh": "bin/zsh"})
        shell, rejected = terminal._resolve_shell({"shell": "fish"})
        assert os.path.isabs(shell) and shell.endswith("/bin/fish")
        assert rejected is None
        shell, rejected = terminal._resolve_shell({})
        assert os.path.isabs(shell) and shell.endswith("/bin/zsh")
        assert rejected is None

    def test_invalid_configured_falls_back_to_env_shell(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        self._pin_which(monkeypatch, {"/usr/bin/zsh": "/usr/bin/zsh", "/bin/bash": "/bin/bash"})
        assert terminal._resolve_shell({"shell": "/opt/typo"}) == (
            "/usr/bin/zsh",
            "/opt/typo",
        )

    def test_unset_uses_env_shell_without_rejection(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        self._pin_which(monkeypatch, {"/usr/bin/zsh": "/usr/bin/zsh"})
        assert terminal._resolve_shell({}) == ("/usr/bin/zsh", None)

    def test_whitespace_configured_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        self._pin_which(monkeypatch, {"/usr/bin/zsh": "/usr/bin/zsh"})
        assert terminal._resolve_shell({"shell": "   "}) == ("/usr/bin/zsh", None)

    def test_invalid_env_shell_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/opt/gone")
        self._pin_which(monkeypatch, {"/bin/bash": "/bin/bash"})
        assert terminal._resolve_shell({}) == ("/bin/bash", None)

    def test_no_env_shell_uses_default(self, monkeypatch):
        monkeypatch.delenv("SHELL", raising=False)
        self._pin_which(monkeypatch, {"/bin/bash": "/bin/bash"})
        assert terminal._resolve_shell({}) == ("/bin/bash", None)

    def test_nothing_resolves_returns_default_unvalidated(self, monkeypatch):
        # A host where no candidate validates keeps the historical behavior:
        # return the default so the spawn's own error surfaces, never a
        # silent no-terminal state.
        monkeypatch.delenv("SHELL", raising=False)
        self._pin_which(monkeypatch, {})  # nothing resolves
        assert terminal._resolve_shell({"shell": "/opt/typo"}) == (
            "/bin/bash",
            "/opt/typo",
        )

    def test_windows_rejects_to_powershell(self, monkeypatch):
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", True)
        self._pin_which(
            monkeypatch, {"powershell.exe": "C:\\WINDOWS\\System32\\powershell.exe"}
        )
        shell, rejected = terminal._resolve_shell({"shell": "C:\\typo.exe"})
        # abspath is host-dependent for a Windows-style fake on a POSIX test
        # host, so assert the wiring (absolute + right program) rather than an
        # exact string.
        assert os.path.isabs(shell)
        assert shell.endswith("powershell.exe")
        assert rejected == "C:\\typo.exe"


# ── _get_config ──


class TestGetConfig:
    def test_returns_terminal_config(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"dashboard": {"terminal": {"max_sessions": 5, "shell": "/bin/zsh"}}})
        )
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        req = _make_request()
        result = terminal._get_config(req)
        assert result == {"max_sessions": 5, "shell": "/bin/zsh"}

    def test_returns_empty_on_missing_file(self, tmp_path, monkeypatch):
        from pathlib import Path

        monkeypatch.setattr(terminal, "config_path", lambda: Path("/nonexistent/config.json"))
        req = _make_request()
        result = terminal._get_config(req)
        assert result == {}

    def test_returns_empty_on_invalid_json(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not json")
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        req = _make_request()
        result = terminal._get_config(req)
        assert result == {}

    @pytest.mark.parametrize(
        "document",
        [
            '{"dashboard": false}',
            '{"dashboard": true}',
            '{"dashboard": 7}',
            '{"dashboard": "x"}',
            '{"dashboard": []}',
            '{"dashboard": null}',
            "[]",
            '"x"',
            "7",
            "null",
        ],
    )
    def test_a_malformed_parent_never_raises(self, document, tmp_path, monkeypatch):
        # A chained `.get` on a non-dict raises AttributeError, which is NOT in
        # this function's caught set — so before the type checks a single
        # hand-edited typo was an HTTP 500 on every terminal route, including the
        # per-keystroke completion one. The read fails closed to the default.
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(document)
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        assert terminal._get_config(_make_request()) == {}

    @pytest.mark.parametrize("value", ["false", "true", "7", '"x"', "[]", "null"])
    def test_a_non_object_terminal_value_is_the_default(
        self, value, tmp_path, monkeypatch,
    ):
        # `"terminal": false` reads like "off" but is not the documented shape
        # (`terminal.enabled`), and returning it verbatim made `_is_enabled` do
        # `False.get("enabled")` — a 500 rather than a disabled panel. A malformed
        # value degrades to the default, as an absent key does.
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"dashboard": {"terminal": ' + value + "}}")
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        assert terminal._get_config(_make_request()) == {}

    def test_is_enabled_survives_a_non_object_terminal_value(
        self, tmp_path, monkeypatch,
    ):
        # The panel flag is the first `_get_config` consumer on every terminal
        # route, so a raise here took the whole panel down, not just completion.
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"dashboard": {"terminal": false}}')
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        terminal._enabled_cache[0] = True
        terminal._enabled_cache[1] = 0.0
        assert terminal._is_enabled(_make_request()) is True

    def test_returns_empty_when_no_terminal_key(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        req = _make_request()
        result = terminal._get_config(req)
        assert result == {}


# ── _resolve_cwd ──


class TestResolveCwd:
    @pytest.fixture
    def invalid_workspace(self, tmp_path, monkeypatch):
        registry = {}
        request = _make_request(registry=registry)
        request.query = {"cwd": str(tmp_path / "missing")}
        ws = MagicMock(spec=web.WebSocketResponse)
        ws.closed = False
        ws.prepare = AsyncMock()
        ws.send_str = AsyncMock()
        ws.close = AsyncMock()
        monkeypatch.setattr(terminal, "_get_config", lambda _: {"cwd": str(tmp_path)})
        monkeypatch.setattr(
            terminal, "_resolve_shell_with_fence_shells",
            lambda _: ("/bin/sh", None, {}),
        )
        monkeypatch.setattr(terminal.web, "WebSocketResponse", lambda **_: ws)
        # Fail before allocating a POSIX PTY or preparing either spawn's env.
        pty = MagicMock()
        pty.openpty.side_effect = AssertionError("unexpected PTY allocation")
        monkeypatch.setattr(terminal, "_pty", pty)
        monkeypatch.setattr(
            terminal, "_pty_child_env",
            MagicMock(side_effect=AssertionError("unexpected shell spawn")),
        )
        return request, registry, ws

    def test_valid_requested_dir_wins(self, tmp_path):
        assert terminal._resolve_cwd({"cwd": "/etc"}, str(tmp_path)) == str(tmp_path)

    def test_expands_user_in_requested(self, tmp_path, monkeypatch):
        # POSIX expanduser reads HOME; Windows reads USERPROFILE — set both so
        # the test is platform agnostic.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert terminal._resolve_cwd({}, "~") == str(tmp_path)

    def test_invalid_requested_refuses_another_directory(self, tmp_path):
        with pytest.raises(ValueError, match="working directory"):
            terminal._resolve_cwd({"cwd": str(tmp_path)}, str(tmp_path / "missing"))

    @pytest.mark.parametrize("windows", [False, True])
    @pytest.mark.asyncio
    async def test_invalid_workspace_reports_error_without_spawning(
        self, invalid_workspace, monkeypatch, windows,
    ):
        request, registry, ws = invalid_workspace
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", windows)

        async def check_released(*_args):
            assert registry == {}

        ws.prepare.side_effect = check_released
        ws.close.side_effect = check_released
        response = await terminal.api_terminal_ws(request)
        assert response is ws
        ws.prepare.assert_awaited_once_with(request)
        ws.send_str.assert_awaited_once()
        assert json.loads(ws.send_str.call_args.args[0]) == {
            "type": "error",
            "code": "terminal_invalid_cwd",
            "message": "Terminal working directory is unavailable",
        }
        ws.close.assert_awaited_once()
        assert registry == {}
        terminal._pty.openpty.assert_not_called()
        terminal._pty_child_env.assert_not_called()

    @pytest.mark.parametrize("stage", ["prepare", "send_str", "close"])
    @pytest.mark.asyncio
    async def test_invalid_workspace_cancellation_releases_reservation(
        self, invalid_workspace, stage,
    ):
        request, registry, ws = invalid_workspace
        getattr(ws, stage).side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await terminal.api_terminal_ws(request)
        assert registry == {}

    @pytest.mark.asyncio
    async def test_probe_capacity_refusal_is_audited_and_releases_slot(
        self,
        invalid_workspace,
        monkeypatch,
    ):
        request, registry, ws = invalid_workspace
        monkeypatch.setattr(
            terminal,
            "_run_path_probe",
            AsyncMock(side_effect=terminal._PathProbeBusy),
        )
        audit = MagicMock()
        monkeypatch.setattr(terminal, "_sel", lambda: audit)

        assert await terminal.api_terminal_ws(request) is ws
        assert registry == {}
        assert json.loads(ws.send_str.call_args.args[0])["code"] == "terminal_invalid_cwd"
        record = audit.log_api_access.call_args.kwargs
        assert record["outcome"] == "denied"
        assert repr(request.query["cwd"]) in record["resources"]
        terminal._pty.openpty.assert_not_called()
        terminal._pty_child_env.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_cwd_under_two_stuck_probes_is_refused_by_its_key(
        self, invalid_workspace, monkeypatch, tmp_path,
    ):
        """The cwd is the probe's SECOND argument, so it is keyed by ``stall_path``:
        with two probes stuck under it the open is refused before ``_resolve_cwd``
        runs, while a third worker is still free."""
        from kiro_crew import executors
        from kiro_crew.dashboard.handlers import files

        request, registry, ws = invalid_workspace
        request.query = {"cwd": str(tmp_path)}
        resolve = MagicMock(side_effect=AssertionError("_resolve_cwd ran on a stuck mount"))
        monkeypatch.setattr(terminal, "_resolve_cwd", resolve)
        monkeypatch.setattr(files, "_PATH_PROBE_ADMIT_TIMEOUT_SECS", 0.2)
        monkeypatch.setattr(files, "_PATH_PROBE_STALL_SECS", 0.0)
        release = threading.Event()

        def park(_path: str, entered: threading.Event) -> None:
            entered.set()
            release.wait()

        with ThreadPoolExecutor(max_workers=3) as pool:
            monkeypatch.setattr(executors, "path_probe_executor", lambda: pool)
            entered_a, entered_b = threading.Event(), threading.Event()
            wedged = [
                asyncio.ensure_future(files._run_path_probe(park, str(tmp_path / n), e))
                for e, n in ((entered_a, "a"), (entered_b, "b"))
            ]
            try:
                await async_wait_until(
                    lambda: entered_a.is_set() and entered_b.is_set(),
                    describe=lambda: "the two stuck probes starting",
                )
                assert await terminal.api_terminal_ws(request) is ws
                assert registry == {}
                assert json.loads(ws.send_str.call_args.args[0])["code"] == (
                    "terminal_invalid_cwd"
                )
                resolve.assert_not_called()
                assert await files._run_path_probe(lambda: "free") == "free"
            finally:
                release.set()
                await await_until(asyncio.gather(*wedged), what="the parked probes settling")

    @pytest.mark.parametrize("stage", ["send_str", "close"])
    @pytest.mark.asyncio
    async def test_invalid_workspace_response_is_bounded(
        self, invalid_workspace, monkeypatch, stage,
    ):
        request, registry, ws = invalid_workspace

        async def blocked(*_args):
            await asyncio.Event().wait()

        getattr(ws, stage).side_effect = blocked
        monkeypatch.setattr(terminal, "_TERMINAL_WS_CLEANUP_TIMEOUT_S", 0.01)
        response = await await_until(
            asyncio.ensure_future(terminal.api_terminal_ws(request)),
            what="the invalid-workspace response",
        )
        assert response is ws
        ws.close.assert_awaited_once()
        assert registry == {}

    @pytest.mark.asyncio
    async def test_wedged_cwd_releases_slot_without_holding_discovery(
        self,
        invalid_workspace,
        monkeypatch,
        tmp_path,
    ):
        from kiro_crew import executors

        request, registry, ws = invalid_workspace
        entered = asyncio.Event()
        expire = asyncio.Event()
        finished = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        probe_threads = []
        wait_for = asyncio.wait_for

        async def expire_after_probe_starts(awaitable, timeout):
            if timeout != terminal._TERMINAL_CWD_TIMEOUT_S:
                return await wait_for(awaitable, timeout=timeout)
            probe = asyncio.create_task(awaitable)
            # Expire the real await only after a worker has claimed the probe.
            # A short wall-clock budget would race thread startup on CI.
            await await_until(entered, what="the probe worker starting")
            await await_until(expire, what="the test expiring the probe")
            return await wait_for(probe, timeout=0)

        def blocked_probe(*_args):
            probe_threads.append(threading.current_thread().name)
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(timeout=5), "test did not release the path probe"
                return str(tmp_path)
            finally:
                loop.call_soon_threadsafe(finished.set)

        with ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="terminal-cwd-probe",
        ) as pool:
            monkeypatch.setattr(executors, "path_probe_executor", lambda: pool)
            monkeypatch.setattr(terminal, "_resolve_cwd", blocked_probe)
            monkeypatch.setattr(
                terminal.asyncio,
                "wait_for",
                expire_after_probe_starts,
            )
            opening = asyncio.create_task(terminal.api_terminal_ws(request))
            try:
                await await_until(entered, what="the probe worker starting")
                assert registry == {"abc123": None}
                expire.set()
                assert await await_until(opening, what="the opening handler returning") is ws
                assert not release.is_set()
                assert registry == {}
                assert probe_threads[0].startswith("terminal-cwd-probe")
                assert await await_until(
                    loop.run_in_executor(terminal.discovery_executor(), lambda: True),
                    what="the discovery executor taking a job",
                )
                assert json.loads(ws.send_str.call_args.args[0])["code"] == ("terminal_invalid_cwd")
                terminal._pty.openpty.assert_not_called()
                terminal._pty_child_env.assert_not_called()
            finally:
                expire.set()
                release.set()
                await await_until(finished, what="the blocked probe finishing")
                if not opening.done():
                    opening.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await opening
        assert registry == {}

    def test_no_request_uses_config_cwd(self, tmp_path):
        assert terminal._resolve_cwd({"cwd": str(tmp_path)}, None) == str(tmp_path)

    def test_no_request_no_config_uses_home(self, tmp_path, monkeypatch):
        # POSIX expanduser reads HOME; Windows reads USERPROFILE — set both so
        # the test is platform agnostic.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert terminal._resolve_cwd({}, None) == str(tmp_path)

    def test_windows_home_unset_falls_back_to_userprofile(self, tmp_path, monkeypatch):
        # On Windows cmd/PowerShell HOME is unset and only USERPROFILE is set.
        # expanduser("~") resolves the profile, so the default is the user's home
        # rather than "/" (which Windows reads as the drive root).
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(
            terminal.os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p
        )
        assert terminal._resolve_cwd({}, None) == str(tmp_path)

    def test_posix_home_set_still_opens_at_home(self, tmp_path, monkeypatch):
        # With HOME set and no requested/configured cwd, the default is the home
        # directory (resolved via expanduser).
        monkeypatch.setattr(
            terminal.os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p
        )
        assert terminal._resolve_cwd({}, None) == str(tmp_path)

    def test_unresolvable_home_keeps_root_last_resort(self, monkeypatch):
        # When no home can be resolved at all, expanduser returns the literal "~";
        # the "/" last resort applies rather than returning "~".
        monkeypatch.setattr(terminal.os.path, "expanduser", lambda p: p)
        assert terminal._resolve_cwd({}, None) == "/"


# ── _kill_session ──


class TestKillSession:
    @pytest.mark.asyncio
    async def test_cancels_reader_task(self):
        # flake-ok: a never-set Event parks the stand-in reader until teardown cancels it
        task = asyncio.ensure_future(asyncio.Event().wait())
        sess = _make_session()
        sess.reader_task = task
        with patch("os.close"), patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"):
            await terminal._kill_session(sess)
        assert task.cancelled()
        assert sess.master_fd == -1  # wokeignore:rule=master

    @pytest.mark.asyncio
    async def test_closes_master_fd(self):
        sess = _make_session()
        sess.master_fd = 42
        with patch("os.close") as mock_close, patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"):
            await terminal._kill_session(sess)
        mock_close.assert_called_with(42)
        assert sess.master_fd == -1

    @pytest.mark.asyncio
    async def test_skips_close_when_fd_negative(self):
        sess = _make_session()
        sess.master_fd = -1
        with patch("os.close") as mock_close, patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"):
            await terminal._kill_session(sess)
        mock_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_sigterm_to_process_group(self):
        # _kill_session routes the tree-kill through platform_compat.kill_process_tree
        # (killpg on POSIX, taskkill /T on Windows), so patch + assert against the
        # shim rather than os.killpg directly.
        sess = _make_session(alive=True)
        with patch("os.close"), \
                patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree") as mock_kill:
            await terminal._kill_session(sess)
        mock_kill.assert_any_call(UNALLOCATABLE_PID, platform_compat.SIGTERM)
        assert sess.master_fd == -1  # wokeignore:rule=master

    @pytest.mark.asyncio
    async def test_the_process_tree_is_hung_up_and_ended_before_the_pty_is_closed(self):
        """Signals first, close second, HUP among the signals: and the order is the fix.

        Closing the PTY's controller end while the reader is blocked in ``os.read()``
        on it only unblocks that read on Linux; on macOS/BSD ``close()`` WAITS for the
        read, so with an interactive bash still holding the terminal end the close never
        returned and four PTY tests timed out at 120 s on every macOS run. Ending the
        process tree first releases the terminal end on both. SIGHUP is included because an interactive
        shell ignores SIGTERM, which alone would cost the 5 s SIGKILL escalation on
        every terminal close.
        """
        order: list[str] = []
        sess = _make_session(alive=True)
        sess.master_fd = 42  # wokeignore:rule=master

        def _kill(pid, sig):
            order.append(f"kill:{sig}")
            return True

        with patch("os.close", side_effect=lambda fd: order.append("close")), patch(
            "kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree",
            side_effect=_kill,
        ):
            await terminal._kill_session(sess)

        assert "close" in order and f"kill:{platform_compat.SIGHUP}" in order
        assert order.index(f"kill:{platform_compat.SIGHUP}") < order.index("close")
        assert order.index(f"kill:{platform_compat.SIGTERM}") < order.index("close")

    @pytest.mark.asyncio
    async def test_skips_kill_when_process_already_exited(self):
        sess = _make_session(alive=False)
        with patch("os.close"), \
                patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree") as mock_kill:
            await terminal._kill_session(sess)
        mock_kill.assert_not_called()
        # An exited child still leaves a PTY descriptor to close.
        assert sess.master_fd == -1  # wokeignore:rule=master

    @pytest.mark.asyncio
    async def test_handles_process_lookup_error_on_sigterm(self):
        sess = _make_session(alive=True)
        with patch("os.close"), \
                patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree",
                      side_effect=ProcessLookupError):
            await terminal._kill_session(sess)
        # A child that vanished between the liveness check and the signal must not
        # stop the rest of teardown.
        assert sess.master_fd == -1  # wokeignore:rule=master

    @pytest.mark.asyncio
    async def test_sigkill_on_timeout(self):
        sess = _make_session(alive=True)
        sess.proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError, None])
        with patch("os.close"), \
                patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree") as mock_kill:
            await terminal._kill_session(sess)
        calls = [c.args for c in mock_kill.call_args_list]
        assert (UNALLOCATABLE_PID, platform_compat.SIGTERM) in calls
        assert (UNALLOCATABLE_PID, platform_compat.SIGKILL) in calls

    @pytest.mark.asyncio
    async def test_ends_child_before_closing_controller_fd(self):
        """Teardown ends the child, THEN closes the PTY controller fd.

        The read loop parks a pool thread in a blocking ``os.read()`` on that
        fd. Only the child's exit frees it portably: the worker side hangs up
        and the read returns EOF on macOS or EIO on Linux. Closing the fd does
        not wake a blocking PTY read on macOS, where the read returns only on
        that hangup, so a close-first order parks the thread for the process's
        lifetime and stalls teardown before it signals anything. The assertion
        is on the ORDER, so it holds on every platform.
        """
        order: list[tuple[str, object]] = []
        sess = _make_session(alive=True)
        sess.master_fd = 42  # wokeignore:rule=master

        async def _kill(pid, sig):
            order.append(("kill", sig))

        with patch("os.close", side_effect=lambda fd: order.append(("close", fd))), patch(
            "kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree_async",
            AsyncMock(side_effect=_kill),
        ):
            await terminal._kill_session(sess)

        assert ("kill", platform_compat.SIGTERM) in order, order
        assert ("close", 42) in order, order
        assert order.index(("kill", platform_compat.SIGTERM)) < order.index(("close", 42)), (
            f"controller fd closed before the child was ended: {order}"
        )
        assert sess.master_fd == -1  # wokeignore:rule=master

    @pytest.mark.asyncio
    async def test_reader_task_cancelled_after_child_ends(self):
        """The reader task is cancelled after the child's exit, so the parked
        read has already returned EOF and the cancel is a formality."""
        order: list[str] = []
        task = AsyncMock()
        task.cancel = MagicMock(side_effect=lambda: order.append("cancel"))
        sess = _make_session(alive=True)
        sess.reader_task = task

        async def _kill(pid, sig):
            order.append("kill")

        with patch("os.close"), patch(
            "kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree_async",
            AsyncMock(side_effect=_kill),
        ):
            await terminal._kill_session(sess)

        # Every signal (SIGHUP, then SIGTERM) lands before the reader is touched;
        # the exact signal sequence is the previous test's business.
        assert order and order[-1] == "cancel", order
        assert order.count("cancel") == 1, order
        assert all(step == "kill" for step in order[:-1]), order

    @pytest.mark.skipif(
        terminal.platform_compat.IS_WINDOWS,
        reason="POSIX pty teardown; Windows sessions use the ConPTY backend",
    )
    @pytest.mark.asyncio
    async def test_close_completes_with_a_reader_parked_on_a_real_pty(self):
        """End to end on a real PTY: a thread parked in a blocking read on the
        controller fd must not outlive teardown, and teardown must finish.

        This is the shape a user hits by closing a terminal whose shell is
        alive. It passes on Linux with either order and fails on macOS with a
        close-first order, so the macOS leg is what proves the fix.
        """
        controller_fd, worker_fd = os.openpty()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import sys; sys.stdin.read()",
            stdin=worker_fd, stdout=worker_fd, stderr=worker_fd,
            start_new_session=True,
        )
        os.close(worker_fd)
        sess = terminal._TerminalSession(
            session_id="real-pty", master_fd=controller_fd, proc=proc,  # wokeignore:rule=master
        )
        parked = threading.Event()

        def _blocking_read():
            parked.set()
            try:
                return os.read(controller_fd, 4096)
            except OSError:
                return b""

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            read_future = pool.submit(_blocking_read)
            assert parked.wait(10), "reader thread never started"
            await asyncio.sleep(0.2)  # let the read enter the kernel

            await await_until(
                asyncio.ensure_future(terminal._kill_session(sess)),
                what="_kill_session with a reader parked on the PTY",
            )

            assert sess.master_fd == -1  # wokeignore:rule=master
            assert proc.returncode is not None, "child survived teardown"
            # The parked read has returned, so the pool worker is free again.
            await async_wait_until(
                read_future.done,
                describe=lambda: (
                    "the parked os.read() never returned: closing the controller fd "
                    "does not wake it, so the child must be ended first"
                ),
            )
        finally:
            # A failure above (a _kill_session timeout, a wrong assertion) must
            # not leave the child holding the PTY and the worker parked in
            # os.read(): end the child, then close the controller so the read
            # returns, then let the pool go. Otherwise the non-daemon worker
            # blocks the interpreter's exit and the run hangs instead of failing.
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await await_until(
                    asyncio.ensure_future(proc.wait()), what="the killed child being reaped"
                )
            if sess.master_fd != -1:  # wokeignore:rule=master
                with contextlib.suppress(OSError):
                    os.close(controller_fd)
            pool.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_reap_after_sigkill_is_bounded(self, monkeypatch):
        """A child that does not exit after SIGKILL must not hang teardown.

        A shell blocked writing into an undrained PTY controller buffer stays in
        a tty write until the fd is read or closed, so a pending SIGKILL does
        not tear it down and an unbounded wait() loses the request handler for
        the life of the process. Teardown gives up on the reap and continues,
        which is what lets the controller fd close and release the write.
        """
        sess = _make_session(alive=True)
        sess.master_fd = 42  # wokeignore:rule=master
        calls = {"n": 0}

        async def _wait(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise asyncio.TimeoutError  # the 5s SIGTERM wait expires
            await asyncio.Event().wait()  # SIGKILL never lands: wait forever

        sess.proc.wait = _wait
        monkeypatch.setattr(terminal.platform_compat, "REAP_TIMEOUT_SECS", 0.05)

        with patch("os.close") as mock_close, patch(
            "kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree_async",
            AsyncMock(),
        ):
            await await_until(
                asyncio.ensure_future(terminal._kill_session(sess)),
                what="_kill_session giving up on the reap",
            )

        # Teardown ran to completion: the controller fd is closed and cleared.
        mock_close.assert_called_with(42)
        assert sess.master_fd == -1  # wokeignore:rule=master
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_handles_os_error_on_close(self):
        sess = _make_session()
        sess.master_fd = 42
        with patch("os.close", side_effect=OSError), patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"):
            await terminal._kill_session(sess)
        assert sess.master_fd == -1

    @pytest.mark.asyncio
    async def test_close_runs_on_subprocess_pool_off_loop(self):
        """The PTY master close must run on subprocess_executor (off the loop),
        never inline — a wedged close then costs one pool thread, not the loop."""
        import threading

        loop_thread = threading.current_thread()
        close_threads = []

        def _record_close(fd):
            close_threads.append(threading.current_thread())

        sess = _make_session()
        sess.master_fd = 42
        with patch("os.close", side_effect=_record_close), patch(
            "kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"
        ):
            await terminal._kill_session(sess)
        assert close_threads, "os.close must have run"
        assert close_threads[0] is not loop_thread, "close ran on the event-loop thread"
        assert close_threads[0].name.startswith("mc-subproc"), (
            f"close must run on subprocess_executor, got {close_threads[0].name!r}"
        )
        assert sess.master_fd == -1

    @pytest.mark.asyncio
    async def test_master_fd_cleared_before_await_survives_cancellation(self):
        """If the coroutine is cancelled while suspended on the executor close,
        the controller fd field must already be -1 so the fd is not left
        referenced.

        The tree-kill is stubbed like in every sibling: this test is about the
        descriptor handoff, and the mock session's pid is never signalled for
        real.
        """
        loop = asyncio.get_running_loop()
        closing = loop.create_future()
        entered = asyncio.Event()

        def submit(*_args):
            entered.set()
            return closing

        sess = _make_session()
        sess.master_fd = 42
        with patch.object(loop, "run_in_executor", side_effect=submit), patch(
            "kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree_async",
            AsyncMock(),
        ):
            task = asyncio.create_task(terminal._kill_session(sess))
            try:
                await await_until(entered, what="the paused step being entered")
                assert sess.master_fd == -1  # wokeignore:rule=master
                task.cancel()
                cancelled = asyncio.Event()
                loop.call_soon(cancelled.set)
                await await_until(cancelled, what="the cancellation's loop turn")
                assert not closing.cancelled()
            finally:
                if not closing.done():
                    closing.set_result(None)
                (result,) = await await_until(
                    asyncio.gather(task, return_exceptions=True), what="the task settling"
                )
            assert isinstance(result, asyncio.CancelledError)

    @pytest.mark.skipif(
        terminal.platform_compat.IS_WINDOWS,
        reason="POSIX master_fd/proc teardown; Windows sessions use the ConPTY backend",  # wokeignore:rule=master
    )
    @pytest.mark.asyncio
    async def test_handles_runtime_error_on_close_when_pool_shutdown(self):
        """If subprocess_executor was torn down, run_in_executor's submit raises
        RuntimeError — _kill_session must swallow it, not abort teardown."""
        sess = _make_session(alive=True)
        sess.master_fd = 42
        with patch.object(
            asyncio.get_event_loop(),
            "run_in_executor",
            side_effect=RuntimeError("cannot schedule new futures after shutdown"),
        ), patch(
            "kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"
        ) as mock_kill:
            await terminal._kill_session(sess)
        # The close error was swallowed and teardown continued to the tree-kill.
        assert sess.master_fd == -1
        mock_kill.assert_any_call(UNALLOCATABLE_PID, platform_compat.SIGTERM)


# ── api_terminal_create ──


class TestApiTerminalCreate:
    """POSIX happy-path coverage. Force ``IS_WINDOWS=False`` so these run on the
    Windows build host too — the Windows-specific 501 gate has its own suite in
    ``TestApiTerminalCreateWindowsFailFast``. Mirrors the umbrella-wide
    monkeypatch pattern used by ``TestRestrictToOwnerArgvOnLinux`` in
    ``test_platform_compat_owner_only.py``."""

    @pytest.fixture(autouse=True)
    def _force_posix(self, monkeypatch):
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", False)

    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        req = _make_request(user=None)
        resp = await terminal.api_terminal_create(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_returns_session_id(self):
        req = _make_request()
        with patch.object(terminal, "_get_config", return_value={"enabled": True}), patch.object(
            terminal, "_sel"
        ) as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert "session_id" in body
        assert len(body["session_id"]) == 12
        assert "shell" in body

    @pytest.mark.asyncio
    async def test_rejects_when_max_sessions_reached(self):
        registry = {"s1": _make_session(), "s2": _make_session(), "s3": _make_session()}
        req = _make_request(registry=registry)
        with patch.object(
            terminal, "_get_config", return_value={"enabled": True, "max_sessions": 3}
        ), patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 429

    @pytest.mark.asyncio
    async def test_respects_custom_max_sessions(self):
        registry = {"s1": _make_session()}
        req = _make_request(registry=registry)
        with patch.object(
            terminal, "_get_config", return_value={"enabled": True, "max_sessions": 1}
        ), patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 429

    @pytest.mark.asyncio
    async def test_uses_configured_shell(self, monkeypatch):
        # The configured shell must actually resolve to an executable to be
        # used — pin `which` so the test does not depend on the host's zsh.
        monkeypatch.setattr(
            terminal.shutil, "which", lambda c: c if c == "/bin/zsh" else None
        )
        req = _make_request()
        with patch.object(
            terminal, "_get_config", return_value={"enabled": True, "shell": "/bin/zsh"}
        ), patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        body = json.loads(resp.body)
        assert body["shell"] == "/bin/zsh"
        assert "shell_fallback" not in body

    @pytest.mark.asyncio
    async def test_surfaces_fallback_when_configured_shell_missing(self, monkeypatch):
        # A configured shell that does not resolve must not fail the create —
        # the response falls back AND says so, so a typo is visible instead of
        # a silently different shell.
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(
            terminal.shutil, "which", lambda c: c if c == "/bin/bash" else None
        )
        req = _make_request()
        with patch.object(
            terminal, "_get_config",
            return_value={"enabled": True, "shell": "/opt/no-such-shell"},
        ), patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["shell"] == "/bin/bash"
        assert body["shell_fallback"] is True
        assert body["configured_shell"] == "/opt/no-such-shell"


class TestApiTerminalCreateWindowsFailFast:
    """POST /api/terminal/sessions must fail fast on Windows with a 501.

    Mirrors the ``TestTaskkillErrorMapping`` / ``TestRestrictToOwnerArgvOnLinux``
    monkeypatch pattern from ``test_platform_compat_kill_reap.py`` /
    ``test_platform_compat_owner_only.py``: patch
    ``platform_compat.IS_WINDOWS`` so the branch is exercised on the Linux build
    fleet. PTY/fork are POSIX-only and the ConPTY port is deferred — until then
    the create endpoint MUST refuse on Windows with the same wording the WS
    handler emits, so the frontend never opens a socket that will die during
    PTY spawn.
    """

    @pytest.mark.asyncio
    async def test_windows_returns_session_id(self, monkeypatch):
        # Windows now spawns a ConPTY-backed shell, so create SUCCEEDS (returns
        # a session_id) instead of the old 501 "unsupported" refusal.
        registry: dict = {}
        req = _make_request(registry=registry)
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", True)
        with patch.object(terminal, "_get_config", return_value={"enabled": True}), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert "session_id" in body

    @pytest.mark.asyncio
    async def test_windows_gate_still_requires_auth(self, monkeypatch):
        # Authentication is still enforced first — an unauthenticated request
        # must not create a session. 401 regardless of platform.
        req = _make_request(user=None)
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", True)
        resp = await terminal.api_terminal_create(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_non_windows_still_returns_session_id(self, monkeypatch):
        # Guard against regressing the POSIX happy path.
        req = _make_request()
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", False)
        with patch.object(terminal, "_get_config", return_value={"enabled": True}), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert "session_id" in body


@pytest.mark.skipif(
    not platform_compat.IS_WINDOWS,
    reason="Real-host Windows-only assertion; the monkeypatched suite above "
           "covers the same code path on Linux CI.",
)
class TestApiTerminalCreateWindowsUnmocked:
    """Windows-only unmocked coverage: create succeeds (ConPTY-backed)."""

    @pytest.mark.asyncio
    async def test_real_windows_returns_session_id(self):
        req = _make_request()
        with patch.object(terminal, "_get_config", return_value={"enabled": True}), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert "session_id" in body


# ── api_terminal_delete ──


class TestApiTerminalRedact:
    """POST /api/terminal/redact — contiguous re-scan of a complete selection.
    The streaming path redacts per 4096-byte read; a credential straddling a
    chunk boundary evades both scans, so the hand-off re-scans the whole text
    and the frontend fails closed unless this returns 200."""

    def _req(self, body, user="testuser", enabled=True):
        req = _make_request(user=user)
        req.json = AsyncMock(return_value=body)
        return req

    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        req = self._req({"text": "hello"}, user=None)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_rejects_when_disabled(self):
        req = self._req({"text": "hello"})
        with patch.object(terminal, "_is_enabled", return_value=False), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_rejects_non_string_text(self):
        req = self._req({"text": 42})
        with patch.object(terminal, "_is_enabled", return_value=True), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 400
        mock_sel.return_value.log_api_access.assert_called_once_with(
            caller="testuser",
            operation="terminal.selection.redact",
            outcome="denied",
            source="dashboard",
            resources="invalid_body",
        )

    @pytest.mark.asyncio
    async def test_rejects_oversized_selection(self):
        req = self._req({"text": "x" * (terminal._REDACT_MAX_BYTES + 1)})
        with patch.object(terminal, "_is_enabled", return_value=True), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 413
        mock_sel.return_value.log_api_access.assert_called_once_with(
            caller="testuser",
            operation="terminal.selection.redact",
            outcome="denied",
            source="dashboard",
            resources="selection_too_large",
        )

    @pytest.mark.asyncio
    async def test_redacts_credentials_in_contiguous_text(self):
        # The exact evasion the endpoint exists for: a secret that per-chunk
        # scanning would have split. The contiguous scan must catch it.
        secret = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        req = self._req({"text": f"config dump:\n{secret}\ndone"})
        with patch.object(terminal, "_is_enabled", return_value=True), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 200
        out = json.loads(resp.text)["text"]
        assert "wJalrXUtnFEMI" not in out
        # The success path is the one that hands terminal output to a model,
        # so it must leave an audit row too -- with no selection content.
        mock_sel.return_value.log_api_access.assert_called_once_with(
            caller="testuser",
            operation="terminal.selection.redact",
            outcome="ok",
            source="dashboard",
            resources="ok",
        )

    @pytest.mark.asyncio
    async def test_fails_closed_on_redactor_error(self):
        req = self._req({"text": "hello"})
        with patch.object(terminal, "_is_enabled", return_value=True), \
             patch.object(terminal, "redact_exfiltration_urls", side_effect=RuntimeError), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 500
        assert "hello" not in resp.text
        mock_sel.return_value.log_api_access.assert_called_once_with(
            caller="testuser",
            operation="terminal.selection.redact",
            outcome="error",
            source="dashboard",
            resources="redaction_failed",
        )
