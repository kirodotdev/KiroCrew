"""The claude CLI launcher: the Claude backend never asks for the bypass capability.

claude-agent-acp sets the Agent SDK option ``allowDangerouslySkipPermissions`` for
every non-root session, which puts ``--allow-dangerously-skip-permissions`` on the
claude command line -- the capability to enter ``bypassPermissions``, the one mode
in which the adapter stops sending ``session/request_permission``. Crew never
selects that mode, so ``_spawn`` points ``CLAUDE_CODE_EXECUTABLE`` at
``acp/claude_launcher.mjs``, which drops that flag and starts the real CLI.

Two halves are pinned here: the wiring in ``acp/client.py`` (which executable the
adapter is told about), and the launcher itself, run under node against fake CLIs.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")
_NEEDS_NODE = pytest.mark.skipif(_NODE is None, reason="the launcher runs under node")
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX exec semantics")
_TARGET_ENV = "KIROCREW_CLAUDE_CODE_EXECUTABLE"
_BYPASS_FLAG = "--allow-dangerously-skip-permissions"

# A fake claude CLI: reports the argv and the two env vars the launcher manages,
# then exits with FAKE_EXIT so exit-status propagation is observable.
_FAKE_CLI_JS = """
process.stdout.write(JSON.stringify({
  argv: process.argv.slice(2),
  executable: process.env.CLAUDE_CODE_EXECUTABLE ?? null,
  target: process.env.KIROCREW_CLAUDE_CODE_EXECUTABLE ?? null,
}))
process.exit(Number(process.env.FAKE_EXIT ?? 0))
"""


class TestPointAdapterAtClaudeCli:
    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch):
        # The launcher path is POSIX-only until a Windows host verifies it; pin
        # the platform so these assertions mean the same thing on every shard.
        monkeypatch.setattr(client_mod.platform_compat, "IS_WINDOWS", False)

    def test_windows_starts_the_cli_directly_until_verified(self, monkeypatch, caplog):
        monkeypatch.setattr(client_mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(client_mod, "_resolve_claude_code_executable", lambda: "C:\\claude.cmd")
        env: dict[str, str] = {}
        with caplog.at_level(logging.INFO, logger=client_mod.logger.name):
            client_mod._point_adapter_at_claude_cli(env)
        assert env == {"CLAUDE_CODE_EXECUTABLE": "C:\\claude.cmd"}
        assert "not yet verified" in caplog.text

    def test_resolved_cli_is_started_through_the_launcher(self, monkeypatch):
        monkeypatch.setattr(client_mod, "_resolve_claude_code_executable", lambda: "/opt/claude")
        env: dict[str, str] = {}
        client_mod._point_adapter_at_claude_cli(env)
        assert env["CLAUDE_CODE_EXECUTABLE"] == str(client_mod._CLAUDE_LAUNCHER)
        assert env[_TARGET_ENV] == "/opt/claude"

    def test_operator_choice_of_binary_is_kept(self, monkeypatch):
        def _must_not_resolve():
            raise AssertionError("an explicit CLAUDE_CODE_EXECUTABLE must win")

        monkeypatch.setattr(client_mod, "_resolve_claude_code_executable", _must_not_resolve)
        env = {"CLAUDE_CODE_EXECUTABLE": "/custom/claude"}
        client_mod._point_adapter_at_claude_cli(env)
        assert env[_TARGET_ENV] == "/custom/claude"
        assert env["CLAUDE_CODE_EXECUTABLE"] == str(client_mod._CLAUDE_LAUNCHER)

    def test_no_cli_found_sets_nothing_and_warns(self, monkeypatch, caplog):
        monkeypatch.setattr(client_mod, "_resolve_claude_code_executable", lambda: None)
        env: dict[str, str] = {}
        with caplog.at_level(logging.WARNING, logger=client_mod.logger.name):
            client_mod._point_adapter_at_claude_cli(env)
        assert env == {}
        assert "Claude native binary not found" in caplog.text

    def test_missing_launcher_starts_the_cli_directly(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(client_mod, "_resolve_claude_code_executable", lambda: "/opt/claude")
        monkeypatch.setattr(client_mod, "_CLAUDE_LAUNCHER", tmp_path / "absent.mjs")
        env: dict[str, str] = {}
        with caplog.at_level(logging.WARNING, logger=client_mod.logger.name):
            client_mod._point_adapter_at_claude_cli(env)
        assert env == {"CLAUDE_CODE_EXECUTABLE": "/opt/claude"}
        assert "launcher missing" in caplog.text

    def test_the_launcher_ships_with_the_package(self):
        assert client_mod._CLAUDE_LAUNCHER.is_file()
        rel = "acp/claude_launcher.mjs"
        assert rel in (_REPO_ROOT / "setup.cfg").read_text(encoding="utf-8")
        assert f"src/kiro_crew/{rel}" in (_REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    def test_the_launcher_is_directly_executable(self):
        # The SDK runs a script-valued CLAUDE_CODE_EXECUTABLE under node, but the
        # adapter also spawns that path directly for ``claude auth status``: a
        # launcher without a shebang and the executable bit answers that spawn
        # with EACCES, and the adapter logs an auth failure for every session.
        first_line = client_mod._CLAUDE_LAUNCHER.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == "#!/usr/bin/env node"
        if os.name == "posix":
            assert os.access(client_mod._CLAUDE_LAUNCHER, os.X_OK)


class TestSpawnEnvironment:
    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch):
        monkeypatch.setattr(client_mod.platform_compat, "IS_WINDOWS", False)

    async def _spawn_env(self, tmp_path, monkeypatch, **client_kw) -> dict:
        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.delenv(_TARGET_ENV, raising=False)
        client = AcpClient(work_dir=tmp_path, **client_kw)
        with (
            patch(
                "kiro_crew.acp.client._resolve_claude_acp_bin",
                return_value=(["/usr/bin/node", "/x/acp.js"], ""),
            ),
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client._resolve_claude_code_executable",
                return_value="/opt/claude/bin/claude",
            ),
            patch("kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/true"], None)),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            await client._spawn()
            env = mock_exec.call_args.kwargs["env"]
        task = client._stderr_task
        if task is not None and not task.done():
            task.cancel()
        return env

    @pytest.mark.asyncio
    async def test_claude_child_is_pointed_at_the_launcher(self, tmp_path, monkeypatch):
        env = await self._spawn_env(tmp_path, monkeypatch, acp_backend=ACP_BACKEND_CLAUDE)
        assert env["CLAUDE_CODE_EXECUTABLE"] == str(client_mod._CLAUDE_LAUNCHER)
        assert env[_TARGET_ENV] == "/opt/claude/bin/claude"

    @pytest.mark.asyncio
    async def test_kiro_child_gets_neither_variable(self, tmp_path, monkeypatch):
        env = await self._spawn_env(tmp_path, monkeypatch)
        assert "CLAUDE_CODE_EXECUTABLE" not in env
        assert _TARGET_ENV not in env


@_NEEDS_NODE
class TestLauncher:
    def _fake_cli(self, tmp_path: Path) -> Path:
        cli = tmp_path / "fake-claude.mjs"
        cli.write_text(_FAKE_CLI_JS, encoding="utf-8")
        return cli

    def _run(self, target, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_EXECUTABLE",)}
        env.pop(_TARGET_ENV, None)
        env["CLAUDE_CODE_EXECUTABLE"] = str(client_mod._CLAUDE_LAUNCHER)
        if target is not None:
            env[_TARGET_ENV] = str(target)
        env.update(extra_env)
        return subprocess.run(
            [_NODE, str(client_mod._CLAUDE_LAUNCHER), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
            check=False,
        )

    def test_drops_only_the_bypass_flag_and_keeps_order(self, tmp_path):
        cli = self._fake_cli(tmp_path)
        args = ["--output-format", "stream-json", _BYPASS_FLAG, "--verbose", "--model", "m"]
        result = self._run(cli, *args)
        assert result.returncode == 0, result.stderr
        seen = json.loads(result.stdout)
        assert seen["argv"] == ["--output-format", "stream-json", "--verbose", "--model", "m"]

    def test_child_environment_names_the_real_cli(self, tmp_path):
        cli = self._fake_cli(tmp_path)
        seen = json.loads(self._run(cli, _BYPASS_FLAG).stdout)
        assert seen["executable"] == str(cli)
        assert seen["target"] is None

    def test_an_explicit_permission_mode_passes_through(self, tmp_path):
        # The launcher withholds the capability flag only; it does not rewrite a
        # mode, so a bypassPermissions defaultMode from the user's own settings
        # still reaches the CLI (the disclosed boundary in claude-code-provider.md).
        cli = self._fake_cli(tmp_path)
        args = ["--permission-mode", "bypassPermissions", _BYPASS_FLAG]
        seen = json.loads(self._run(cli, *args).stdout)
        assert seen["argv"] == ["--permission-mode", "bypassPermissions"]

    def test_exit_status_propagates(self, tmp_path):
        result = self._run(self._fake_cli(tmp_path), FAKE_EXIT="3")
        assert result.returncode == 3

    def test_missing_target_variable_fails_loudly(self):
        result = self._run(None)
        assert result.returncode == 127
        assert _TARGET_ENV in result.stderr

    def test_unstartable_target_fails_loudly(self, tmp_path):
        result = self._run(tmp_path / "no-such-claude")
        assert result.returncode == 127
        assert "cannot start" in result.stderr

    @_POSIX_ONLY
    def test_native_target_is_executed_directly(self, tmp_path):
        cli = tmp_path / "claude"
        cli.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
        cli.chmod(0o755)
        result = self._run(cli, "-p", _BYPASS_FLAG, "hello world")
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["-p", "hello world"]
