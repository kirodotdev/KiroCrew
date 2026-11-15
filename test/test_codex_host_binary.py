"""The codex harness points codex-acp at the host's own ``codex`` through ``CODEX_PATH``.

codex-acp runs ``app-server`` from ``CODEX_PATH`` when it is set and from the Codex
it bundles otherwise. The bundled build does not know a provider the operator's own
``codex`` was set up for, so the harness fills the name from the host -- only when
the host binary is at least as new as the bundled one, and never over a value the
operator chose, an empty one included.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from acp_client_helpers import _POSIX_EXEC_PATHS_ONLY

from kiro_crew import platform_compat
from kiro_crew.acp import client as client_mod
from kiro_crew.acp.harness import codex as harness_mod
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.harness.codex import CODEX_PATH_ENV, CodexHarness
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

_ADAPTER_ARGV = ["/n/node", "/p/codex-acp/dist/index.js"]
_HIDDEN = ("/h/.aws",)
_EXPOSE = ("/h/.aws/config",)


def _ctx(**over: Any) -> SpawnContext:
    base: dict[str, Any] = {
        "agent": "kirocrew",
        "work_dir": "/w",
        "model": None,
        "environ": {"PATH": "/usr/bin", "HOME": "/h"},
        "home": Path("/h"),
        "sandbox_mode": "standard",
    }
    base.update(over)
    return SpawnContext(**base)


def _fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the host codex must not be resolved over an operator's CODEX_PATH")


def _write_manifest(directory: Path, name: str, version: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps({"name": name, "version": version}))


def _adapter_entry(root: Path) -> Path:
    entry = (
        root / "lib" / "node_modules" / "@agentclientprotocol" / "codex-acp" / "dist" / "index.js"
    )
    entry.parent.mkdir(parents=True)
    entry.write_text("")
    return entry


class TestDecision:
    @pytest.mark.parametrize(
        ("host_version", "chosen"),
        [
            ((0, 165, 0), "/opt/tb/bin/codex"),
            ((0, 147, 0), "/opt/tb/bin/codex"),
            ((0, 146, 9), None),
        ],
        ids=["newer", "equal", "older"],
    )
    def test_the_host_is_handed_over_only_at_or_above_the_bundled_version(
        self, host_version, chosen
    ):
        got, reason = harness_mod._host_codex_decision(
            "/opt/tb/bin/codex", host_version, (0, 147, 0)
        )
        assert got == chosen
        assert "0.147.0" in reason

    def test_an_older_host_names_both_versions(self):
        _, reason = harness_mod._host_codex_decision("/opt/tb/bin/codex", (0, 100, 1), (0, 147, 0))
        assert "0.100.1" in reason and "older" in reason

    @pytest.mark.parametrize(
        ("host_version", "bundled_version"),
        [(None, (0, 147, 0)), ((0, 165, 0), None), (None, None)],
        ids=["host-unreadable", "bundled-unreadable", "both-unreadable"],
    )
    def test_an_unreadable_version_keeps_the_bundled_codex(self, host_version, bundled_version):
        got, reason = harness_mod._host_codex_decision(
            "/opt/tb/bin/codex", host_version, bundled_version
        )
        assert got is None
        assert "unreadable" in reason or "no readable version" in reason

    def test_no_host_keeps_the_bundled_codex(self):
        got, reason = harness_mod._host_codex_decision(None, None, (0, 147, 0))
        assert got is None
        assert "no host codex" in reason


class TestParseVersion:
    @pytest.mark.parametrize(
        ("text", "version"),
        [
            ("codex-cli 0.147.0\n", (0, 147, 0)),
            ("0.165.0.810 (stable)\n", (0, 165, 0)),
            ("garbage", None),
            ("1.2", None),
            ("", None),
            ("codex-cli 1.2.3.4-dev\n", (1, 2, 3)),
            ("codex-cli 0.147.0-alpha.2\n", None),
            ("build 1.2 then 3.4.5 and 6.7.8", (3, 4, 5)),
            ("codex-cli ².³.⁴", None),
        ],
    )
    def test_the_first_triple_is_read(self, text, version):
        assert harness_mod._parse_codex_version(text) == version


class TestBundledVersion:
    def test_a_copy_nested_in_the_adapter_package_is_found(self, tmp_path):
        entry = _adapter_entry(tmp_path)
        _write_manifest(
            entry.parent.parent / "node_modules" / "@openai" / "codex", "@openai/codex", "0.147.0"
        )
        assert harness_mod._bundled_codex_version(["/n/node", str(entry)]) == (0, 147, 0)

    def test_a_hoisted_copy_is_found(self, tmp_path):
        entry = _adapter_entry(tmp_path)
        _write_manifest(
            tmp_path / "lib" / "node_modules" / "@openai" / "codex", "@openai/codex", "0.150.2"
        )
        assert harness_mod._bundled_codex_version(["/n/node", str(entry)]) == (0, 150, 2)

    def test_the_nested_copy_wins_over_a_hoisted_one(self, tmp_path):
        """Node resolves the nearest ``node_modules`` first, and so does the floor."""
        entry = _adapter_entry(tmp_path)
        _write_manifest(
            entry.parent.parent / "node_modules" / "@openai" / "codex", "@openai/codex", "0.147.0"
        )
        _write_manifest(
            tmp_path / "lib" / "node_modules" / "@openai" / "codex", "@openai/codex", "0.99.0"
        )
        assert harness_mod._bundled_codex_version(["/n/node", str(entry)]) == (0, 147, 0)

    def test_a_manifest_with_another_name_does_not_count(self, tmp_path):
        entry = _adapter_entry(tmp_path)
        _write_manifest(
            entry.parent.parent / "node_modules" / "@openai" / "codex", "not-codex", "9.9.9"
        )
        assert harness_mod._bundled_codex_version(["/n/node", str(entry)]) is None

    def test_none_when_no_bundled_codex_is_installed(self, tmp_path):
        entry = _adapter_entry(tmp_path)
        assert harness_mod._bundled_codex_version(["/n/node", str(entry)]) is None

    def test_none_for_an_empty_argv(self):
        assert harness_mod._bundled_codex_version([]) is None


class TestVersionChild:
    @staticmethod
    def _script(tmp_path: Path, body: str) -> str:
        script = tmp_path / "codex"
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
        return str(script)

    @_POSIX_EXEC_PATHS_ONLY
    def test_the_child_sees_only_path_and_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KC_CODEX_PROBE_LEAK", "1")
        script = self._script(
            tmp_path,
            '[ -n "$KC_CODEX_PROBE_LEAK" ] && exit 3\n[ "$HOME" = /h ] || exit 4\necho "codex-cli 0.147.0"\n',
        )
        got = harness_mod._codex_version_child(
            [script, "--version"], search_path="/usr/bin:/bin", home="/h", cwd=str(tmp_path)
        )
        assert got == (0, 147, 0)

    @_POSIX_EXEC_PATHS_ONLY
    def test_a_failing_child_answers_none(self, tmp_path):
        script = self._script(tmp_path, 'echo "codex-cli 0.147.0"\nexit 1\n')
        got = harness_mod._codex_version_child(
            [script, "--version"], search_path="/usr/bin:/bin", home="", cwd=None
        )
        assert got is None

    @_POSIX_EXEC_PATHS_ONLY
    def test_timeout_kills_and_reaps_the_probe_tree(self, tmp_path, monkeypatch):
        """The shell's background child cannot survive the version timeout."""
        pid_file = tmp_path / "grandchild.pid"
        script = self._script(tmp_path, 'sleep 600 &\nprintf "%s\\n" "$!" > "$1"\nwait\n')
        children: list[subprocess.Popen] = []
        descendant: list[tuple[int, str]] = []

        def _capture_descendant():
            wait_until(
                lambda: pid_file.exists() and pid_file.read_text(encoding="utf-8").strip(),
                timeout=FALLBACK_TIMEOUT_SECS,
                describe=lambda: "the probe has not published its grandchild pid",
            )
            pid = int(pid_file.read_text(encoding="utf-8"))
            identity = platform_compat.get_process_start_id(pid)
            assert identity is not None, "the grandchild must be alive before the timeout"
            descendant.append((pid, identity))

        def _start(*args, **kwargs):
            proc = subprocess.Popen(*args, **kwargs)
            children.append(proc)
            # Start the production deadline only after the asserted child exists.
            _capture_descendant()
            return proc

        monkeypatch.setattr(
            harness_mod, "subprocess_mod", SimpleNamespace(**{**vars(subprocess), "Popen": _start})
        )
        monkeypatch.setattr(harness_mod, "_HOST_CODEX_VERSION_TIMEOUT_S", 0)
        try:
            got = harness_mod._codex_version_child(
                [script, str(pid_file)],
                search_path="/usr/bin:/bin",
                home=str(tmp_path),
                cwd=str(tmp_path),
            )
            assert got is None
            if not descendant:
                _capture_descendant()
            pid, _ = descendant[0]
            wait_until(
                lambda: not platform_compat.pid_exists(pid),
                timeout=FALLBACK_TIMEOUT_SECS,
                describe=lambda: f"grandchild {pid} survived the version timeout",
            )
            assert all(proc.returncode is not None for proc in children)
        finally:
            for pid, identity in descendant:
                if platform_compat.get_process_start_id(pid) == identity:
                    with contextlib.suppress(ProcessLookupError):
                        platform_compat.kill_pid_pinned(pid, identity, platform_compat.SIGKILL)
            for proc in children:
                platform_compat.kill_popen_tree(proc)
                proc.communicate(timeout=FALLBACK_TIMEOUT_SECS)

    @pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
    @pytest.mark.parametrize("failure", [OSError, ValueError, subprocess.SubprocessError])
    def test_communication_failure_still_kills_and_reaps(self, monkeypatch, windows, failure):
        proc = Mock(pid=UNALLOCATABLE_PID, returncode=None)
        proc.communicate.side_effect = [failure("pipe failed"), failure("drain failed")]
        popen = Mock(return_value=proc)
        kill = Mock()
        monkeypatch.setattr(
            harness_mod, "subprocess_mod", SimpleNamespace(**{**vars(subprocess), "Popen": popen})
        )
        monkeypatch.setattr(platform_compat, "IS_POSIX", not windows)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        monkeypatch.setattr(platform_compat, "kill_popen_tree", kill)
        assert (
            harness_mod._codex_version_child(["codex"], search_path="bin", home="", cwd=None)
            is None
        )
        assert popen.call_args.kwargs["start_new_session"] is not windows
        assert popen.call_args.kwargs["creationflags"] == (
            platform_compat.CREATE_NEW_PROCESS_GROUP if windows else 0
        )
        assert popen.call_args.kwargs["env"] == {"PATH": "bin"}
        kill.assert_called_once_with(proc)
        assert proc.communicate.call_count == 2
        # These two read the bound a Mock was handed; nothing waits on it.
        proc.communicate.assert_called_with(
            timeout=platform_compat.REAP_TIMEOUT_SECS  # flake-ok: asserts a Mock's argument
        )
        proc.wait.assert_called_once_with(
            timeout=platform_compat.REAP_TIMEOUT_SECS  # flake-ok: asserts a Mock's argument
        )
        proc.stdout.close.assert_called_once_with()
        proc.stderr.close.assert_called_once_with()

    @staticmethod
    def _capture_probe_env(monkeypatch, *, windows: bool, environ: dict[str, str]):
        """Run ``_host_codex_version`` with Popen captured; return the env it was handed."""
        proc = Mock(pid=UNALLOCATABLE_PID, returncode=0)
        proc.communicate.return_value = ("codex-cli 0.147.0\n", "")
        popen = Mock(return_value=proc)

        async def _wrap(argv, **_kwargs):
            return list(argv), None

        monkeypatch.setattr(
            harness_mod, "subprocess_mod", SimpleNamespace(**{**vars(subprocess), "Popen": popen})
        )
        monkeypatch.setattr(harness_mod, "wrap_argv_async", _wrap)
        monkeypatch.setattr(client_mod, "augmented_path", lambda path: path)
        monkeypatch.setattr(platform_compat, "IS_POSIX", not windows)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        return popen, _ctx(environ=environ)

    _STARTUP = {
        "SYSTEMROOT": r"C:\Windows",
        "ComSpec": r"C:\Windows\system32\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": r"C:\Users\u\AppData\Local\Temp",
        "TMP": r"C:\Users\u\AppData\Local\Temp",
        "USERPROFILE": r"C:\Users\u",
    }
    _DECOYS = {"AWS_SECRET_ACCESS_KEY": "s3cr3t", "NODE_OPTIONS": "--require=/x.js"}

    @pytest.mark.asyncio
    async def test_the_probe_env_carries_the_windows_startup_variables(self, monkeypatch):
        environ = {"PATH": r"C:\bin", "HOME": r"C:\Users\u", **self._STARTUP, **self._DECOYS}
        popen, ctx = self._capture_probe_env(monkeypatch, windows=True, environ=environ)
        got = await harness_mod._host_codex_version(
            "codex", ctx=ctx, hidden=_HIDDEN, expose=_EXPOSE
        )
        assert got == (0, 147, 0)
        assert popen.call_args.kwargs["env"] == {
            "PATH": r"C:\bin",
            "HOME": r"C:\Users\u",
            **self._STARTUP,
        }

    @pytest.mark.asyncio
    async def test_the_probe_env_is_path_and_home_only_on_posix(self, monkeypatch):
        environ = {"PATH": "/usr/bin", "HOME": "/h", **self._STARTUP, **self._DECOYS}
        popen, ctx = self._capture_probe_env(monkeypatch, windows=False, environ=environ)
        await harness_mod._host_codex_version("codex", ctx=ctx, hidden=_HIDDEN, expose=_EXPOSE)
        assert popen.call_args.kwargs["env"] == {"PATH": "/usr/bin", "HOME": "/h"}

    def test_a_missing_binary_answers_none(self, tmp_path):
        got = harness_mod._codex_version_child(
            [str(tmp_path / "absent"), "--version"], search_path="", home="", cwd=None
        )
        assert got is None


class TestVersionProbeIsSandboxed:
    """The host binary is never run by the gateway outside the session's sandbox."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("child_raises", [False, True])
    async def test_the_probe_runs_wrapped_with_the_session_mask(self, monkeypatch, child_raises):
        wrap_calls: list[tuple[list[str], dict[str, Any]]] = []
        child_argvs: list[list[str]] = []
        unlinked: list[str] = []

        async def _wrap(argv, **kwargs):
            wrap_calls.append((list(argv), kwargs))
            return ["/sbx", *argv], "/scratch/launcher"

        def _child(argv, *, search_path, home, cwd, startup_env):
            child_argvs.append(list(argv))
            if child_raises:
                raise RuntimeError("child blew up")
            return (0, 150, 0)

        monkeypatch.setattr(harness_mod, "wrap_argv_async", _wrap)
        monkeypatch.setattr(harness_mod, "_codex_version_child", _child)
        monkeypatch.setattr(harness_mod.launch_mod, "_unlink_readback_launcher", unlinked.append)

        ctx = _ctx(sandbox_mode="strict")
        if child_raises:
            with pytest.raises(RuntimeError, match="child blew up"):
                await harness_mod._host_codex_version(
                    "/opt/tb/bin/codex", ctx=ctx, hidden=_HIDDEN, expose=_EXPOSE
                )
        else:
            got = await harness_mod._host_codex_version(
                "/opt/tb/bin/codex", ctx=ctx, hidden=_HIDDEN, expose=_EXPOSE
            )
            assert got == (0, 150, 0)

        assert len(wrap_calls) == 1
        argv, kwargs = wrap_calls[0]
        assert argv == ["/opt/tb/bin/codex", "--version"]
        assert kwargs["mode"] == "strict"
        assert kwargs["extra_hidden_dirs"] == _HIDDEN
        assert kwargs["extra_expose_files"] == _EXPOSE
        assert kwargs["strip_python_env"] is True
        assert child_argvs == [["/sbx", "/opt/tb/bin/codex", "--version"]]
        assert unlinked == ["/scratch/launcher"]


class TestSpawnDecidesAndEnvApplies:
    @staticmethod
    def _stub_spawn(monkeypatch) -> None:
        monkeypatch.setattr(
            client_mod, "_resolve_codex_acp_bin", lambda: (list(_ADAPTER_ARGV), "/s")
        )
        monkeypatch.setattr(
            harness_mod, "resolve_spawn_masks", AsyncMock(return_value=(_HIDDEN, _EXPOSE))
        )
        monkeypatch.setattr(harness_mod, "_sandbox_wrapper_generations", lambda mode: 0)

    @staticmethod
    def _stub_versions(
        monkeypatch, *, host: str | None, host_version, bundled_version
    ) -> AsyncMock:
        monkeypatch.setattr(harness_mod, "_bundled_codex_version", lambda argv: bundled_version)
        monkeypatch.setattr(harness_mod, "_resolve_host_codex", lambda: host)
        probe = AsyncMock(return_value=host_version)
        monkeypatch.setattr(harness_mod, "_host_codex_version", probe)
        return probe

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operator_value", ["/opt/my-codex", ""])
    async def test_an_operator_codex_path_skips_the_decision(self, monkeypatch, operator_value):
        """Empty is the operator's way to ask codex-acp for its own bundled build."""
        self._stub_spawn(monkeypatch)
        monkeypatch.setattr(harness_mod, "_bundled_codex_version", _fail_if_called)
        monkeypatch.setattr(harness_mod, "_resolve_host_codex", _fail_if_called)
        monkeypatch.setattr(harness_mod, "_host_codex_version", _fail_if_called)
        harness = CodexHarness()
        await harness.resolve_spawn(_ctx(environ={CODEX_PATH_ENV: operator_value}))
        env = {CODEX_PATH_ENV: operator_value}
        harness.apply_spawn_env(env)
        assert env[CODEX_PATH_ENV] == operator_value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host_version", [(0, 165, 0), (0, 147, 0)], ids=["newer", "equal"])
    async def test_a_host_at_or_above_the_bundled_version_is_named(self, monkeypatch, host_version):
        self._stub_spawn(monkeypatch)
        probe = self._stub_versions(
            monkeypatch,
            host="/opt/tb/bin/codex",
            host_version=host_version,
            bundled_version=(0, 147, 0),
        )
        harness = CodexHarness()
        ctx = _ctx()
        await harness.resolve_spawn(ctx)
        env: dict[str, str] = {"PATH": "/usr/bin"}
        harness.apply_spawn_env(env)
        assert env[CODEX_PATH_ENV] == "/opt/tb/bin/codex"
        # The version child gets the mask the adapter itself is spawned under.
        assert probe.await_args.args == ("/opt/tb/bin/codex",)
        assert probe.await_args.kwargs == {"ctx": ctx, "hidden": _HIDDEN, "expose": _EXPOSE}

    @pytest.mark.asyncio
    async def test_an_older_host_leaves_the_name_unset(self, monkeypatch):
        self._stub_spawn(monkeypatch)
        self._stub_versions(
            monkeypatch,
            host="/opt/tb/bin/codex",
            host_version=(0, 120, 0),
            bundled_version=(0, 147, 0),
        )
        harness = CodexHarness()
        await harness.resolve_spawn(_ctx())
        env: dict[str, str] = {}
        harness.apply_spawn_env(env)
        assert CODEX_PATH_ENV not in env

    @pytest.mark.asyncio
    async def test_no_host_codex_leaves_the_name_unset_and_runs_nothing(self, monkeypatch):
        self._stub_spawn(monkeypatch)
        probe = self._stub_versions(
            monkeypatch, host=None, host_version=None, bundled_version=(0, 147, 0)
        )
        harness = CodexHarness()
        await harness.resolve_spawn(_ctx())
        env: dict[str, str] = {}
        harness.apply_spawn_env(env)
        assert CODEX_PATH_ENV not in env
        probe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unreadable_bundled_version_never_starts_the_host_binary(self, monkeypatch):
        self._stub_spawn(monkeypatch)
        probe = self._stub_versions(
            monkeypatch, host="/opt/tb/bin/codex", host_version=(0, 165, 0), bundled_version=None
        )
        monkeypatch.setattr(harness_mod, "_resolve_host_codex", _fail_if_called)
        harness = CodexHarness()
        await harness.resolve_spawn(_ctx())
        env: dict[str, str] = {}
        harness.apply_spawn_env(env)
        assert CODEX_PATH_ENV not in env
        probe.assert_not_awaited()

    def test_the_env_hop_runs_no_lookup(self, monkeypatch):
        """``apply_spawn_env`` only applies the plan's decision."""
        monkeypatch.setattr(harness_mod, "_resolve_host_codex", _fail_if_called)
        env: dict[str, str] = {}
        CodexHarness().apply_spawn_env(env)
        assert CODEX_PATH_ENV not in env


class TestResolveHostCodex:
    def test_mise_is_preferred_over_path(self, monkeypatch):
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: f"/mise/bin/{tool}")
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: "/usr/bin/codex")
        assert harness_mod._resolve_host_codex() == "/mise/bin/codex"

    @_POSIX_EXEC_PATHS_ONLY
    def test_path_lookup_finds_the_cli(self, monkeypatch):
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: "/srv/u/.toolbox/bin/codex" if name == "codex" else None,
        )
        assert harness_mod._resolve_host_codex() == "/srv/u/.toolbox/bin/codex"

    def test_none_when_the_host_has_no_codex(self, monkeypatch):
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: None)
        assert harness_mod._resolve_host_codex() is None

    @pytest.mark.parametrize("path", [r"C:\Users\a%USERNAME%\codex.cmd", 'C:\\odd"dir\\codex.cmd'])
    def test_a_path_cmd_would_expand_is_not_handed_over_on_windows(self, monkeypatch, path):
        """codex-acp starts ``"<CODEX_PATH>" app-server`` through cmd.exe on Windows."""
        monkeypatch.setattr(harness_mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: path)
        assert harness_mod._resolve_host_codex() is None

    def test_the_same_path_is_handed_over_off_windows(self, monkeypatch):
        monkeypatch.setattr(harness_mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: "/opt/a%b/codex")
        assert harness_mod._resolve_host_codex() == "/opt/a%b/codex"


def test_the_manifest_reader_resolves_through_symlinks(tmp_path):
    """A ``.bin`` link to the entry script resolves to the package it belongs to."""
    if not hasattr(os, "symlink"):
        pytest.skip("no symlinks")
    entry = _adapter_entry(tmp_path)
    _write_manifest(
        entry.parent.parent / "node_modules" / "@openai" / "codex", "@openai/codex", "0.147.0"
    )
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    link = link_dir / "codex-acp"
    try:
        link.symlink_to(entry)
    except OSError:
        pytest.skip("symlinks not permitted")
    assert harness_mod._bundled_codex_version([str(link)]) == (0, 147, 0)
