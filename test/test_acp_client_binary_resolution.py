"""Tests for ACP client: locating the backend binaries: the vendored claude adapter, the claude
executables, mise, the kiro-cli override and node installs.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture
from acp_client_helpers import _POSIX_EXEC_PATHS_ONLY, _stop_stderr_drain
from spawn_test_helpers import strip_spawn_shim

from conftest import cap_node_module_walk
from kiro_crew.acp.client import (
    _CLAUDE_ACP_PKG_ENTRY,
    AcpClient,
    _resolve_vendored_claude_acp,
    _vendored_acp_roots,
)


class TestVendoredClaudeAcp:
    """Resolve the vendored claude-agent-acp adapter (no npm/network)."""

    def _make_vendored(self, root: Path, *, with_deps: bool = True) -> Path:
        """Create a fake vendored adapter under *root*.

        With *with_deps* (default) also creates the hoisted dependency marker
        ``@agentclientprotocol/sdk`` so the completeness guard accepts it.
        """
        from kiro_crew.acp.client import _CLAUDE_ACP_DEP_MARKER

        entry = root / _CLAUDE_ACP_PKG_ENTRY
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// fake adapter\n", encoding="utf-8")
        if with_deps:
            (root / _CLAUDE_ACP_DEP_MARKER).mkdir(parents=True, exist_ok=True)
        return entry

    def test_finds_vendored_in_pkg_vendor_dir(self, tmp_path, monkeypatch):
        # Toolbox/pip layout: <pkg_dir>/_vendor/node_modules/<pkg>/dist/index.js.
        # Inject a fake pkg_dir under an isolated home so the real workspace
        # (sibling-website detection / KIROCREW_PROJECT_DIR) is not consulted.
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        entry = self._make_vendored(pkg_dir / "_vendor" / "node_modules")
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(entry)

    def test_finds_vendored_under_project_dir(self, tmp_path, monkeypatch):
        # KIROCREW_PROJECT_DIR/node_modules holds the adapter; pkg_dir has none.
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        entry = self._make_vendored(tmp_path / "proj" / "node_modules")
        (tmp_path / "proj").mkdir(exist_ok=True)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "proj"))
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(entry)

    def test_returns_none_when_absent(self, tmp_path, monkeypatch):
        # Isolated pkg_dir with no _vendor and a project dir with no adapter.
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "empty"))
        (tmp_path / "empty").mkdir()
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) is None

    def test_skips_incomplete_copy_missing_deps(self, tmp_path, monkeypatch):
        # Regression: an entry script whose dependency Node could not import must
        # be rejected (it would crash with ERR_MODULE_NOT_FOUND @agentclientprotocol/sdk),
        # falling through to a complete copy under KIROCREW_PROJECT_DIR.
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        # Incomplete copy in _vendor (entry only, no deps) — must be skipped. The
        # dependency walk runs to the filesystem root like Node's; cap it at tmp_path
        # so a node_modules the host keeps above the temp root cannot complete it.
        self._make_vendored(pkg_dir / "_vendor" / "node_modules", with_deps=False)
        cap_node_module_walk(monkeypatch, tmp_path)
        # Complete copy in the project dir — must win.
        (tmp_path / "proj").mkdir()
        good = self._make_vendored(tmp_path / "proj" / "node_modules")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "proj"))
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(good)

    def test_roots_include_pkg_vendor_dir(self):
        # The toolbox-bundle vendor location must always be the first candidate.
        roots = _vendored_acp_roots()
        assert roots[0].name == "node_modules" and roots[0].parent.name == "_vendor"


class TestResolveClaudeAcpBin:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _resolve_claude_acp_bin

        bin_path = tmp_path / "claude-agent-acp"
        bin_path.write_text("#!/bin/sh\nexit 0\n")
        bin_path.chmod(0o755)
        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(bin_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        argv, _search_path = _resolve_claude_acp_bin()
        assert argv is not None
        assert str(bin_path) in argv

    @_POSIX_EXEC_PATHS_ONLY
    def test_path_lookup(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        bin_path = tmp_path / "claude-agent-acp"
        bin_path.write_text("#!/bin/sh\nexit 0\n")
        bin_path.chmod(0o755)
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: str(bin_path) if name == "claude-agent-acp" else None,
        )
        argv, _search_path = client_mod._resolve_claude_acp_bin()
        assert argv is not None
        assert str(bin_path) in argv

    @_POSIX_EXEC_PATHS_ONLY
    def test_mise_which_preferred(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        script = tmp_path / "bin" / "claude-agent-acp"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        script.chmod(0o755)
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: str(script))
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        argv, _search_path = client_mod._resolve_claude_acp_bin()
        assert argv == [str(script)]

    @_POSIX_EXEC_PATHS_ONLY
    def test_mise_installed_script_resolves_node(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _resolve_claude_acp_bin

        mise_node = tmp_path / ".local" / "share" / "mise" / "installs" / "node" / "20.18.0"
        node_bin = mise_node / "bin" / "node"
        node_bin.parent.mkdir(parents=True)
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)
        script = (
            mise_node
            / "lib"
            / "node_modules"
            / "@agentclientprotocol"
            / "claude-agent-acp"
            / "dist"
            / "index.js"
        )
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        script.chmod(0o755)
        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(script))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        argv, _search_path = _resolve_claude_acp_bin()
        assert argv == [str(node_bin), str(script.resolve())]

    def test_non_executable_script_falls_back_to_path_node(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)

        script = tmp_path / "node_modules" / "claude-agent-acp" / "dist" / "index.js"
        script.parent.mkdir(parents=True)
        script.write_text("console.log('hi')\n")
        script.chmod(0o644)  # NOT executable

        node_bin = tmp_path / "bin" / "node"
        node_bin.parent.mkdir(parents=True)
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)

        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(script))
        monkeypatch.delenv("PATH", raising=False)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: str(node_bin) if name == "node" else None,
        )
        argv, _search_path = client_mod._resolve_claude_acp_bin()
        assert argv == [str(node_bin), str(script.resolve())]

    @_POSIX_EXEC_PATHS_ONLY
    def test_mise_glob_fallback(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        mise_node = tmp_path / ".local" / "share" / "mise" / "installs" / "node" / "22.1.0"
        bin_dir = mise_node / "bin"
        bin_dir.mkdir(parents=True)
        acp_script = bin_dir / "claude-agent-acp"
        acp_script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        acp_script.chmod(0o755)
        node_bin = bin_dir / "node"
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)

        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        argv, _search_path = client_mod._resolve_claude_acp_bin()
        assert argv == [str(node_bin), str(acp_script.resolve())]

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        argv, search_path = client_mod._resolve_claude_acp_bin()
        assert argv is None
        assert search_path == client_mod.augmented_path(os.environ.get("PATH", ""))


class TestResolveClaudeCodeExecutable:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        exe = tmp_path / "claude"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(exe))
        # mise/PATH must NOT be consulted when the override is a real file.
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: "/should/not/win")
        assert client_mod._resolve_claude_code_executable() == str(exe)

    def test_env_override_ignored_when_missing(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(tmp_path / "nope"))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: None)
        assert client_mod._resolve_claude_code_executable() is None

    def test_mise_preferred_over_path(self, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: "/mise/bin/claude")
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: "/usr/bin/claude")
        assert client_mod._resolve_claude_code_executable() == "/mise/bin/claude"

    @_POSIX_EXEC_PATHS_ONLY
    def test_path_lookup(self, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: "/srv/u/.toolbox/bin/claude" if name == "claude" else None,
        )
        assert client_mod._resolve_claude_code_executable() == "/srv/u/.toolbox/bin/claude"

    @_POSIX_EXEC_PATHS_ONLY
    def test_windows_toolbox_claude_off_the_inherited_path_is_found(self, monkeypatch, tmp_path):
        """A Toolbox install on Windows lands in ``%LOCALAPPDATA%\\Toolbox\\bin``.

        The gateway's own PATH may not hold that dir, and the Agent Harness
        probe must still find the ``claude`` there.
        """
        from kiro_crew import env as env_mod
        from kiro_crew.acp import client as client_mod

        bin_dir = tmp_path / "Local" / "Toolbox" / "bin"
        bin_dir.mkdir(parents=True)
        claude = bin_dir / "claude"
        claude.write_text("#!/bin/sh\n")
        claude.chmod(0o755)
        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(env_mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        # The fixed well-known dirs (``/usr/local/bin`` and the like) sit ahead of
        # the Toolbox dir, so a ``claude`` the host already has there would win.
        # Empty them so only the fixture tree can answer.
        monkeypatch.setattr(env_mod, "_EXTRA_PATH_DIRS", ())
        monkeypatch.setattr(env_mod, "_node_all_bin_dirs", lambda *a, **k: [])
        assert client_mod._resolve_claude_code_executable() == str(claude)

    def test_none_when_absent(self, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: None)
        assert client_mod._resolve_claude_code_executable() is None


class TestMiseWhich:
    def test_returns_path_on_success(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        script = tmp_path / "claude-agent-acp"
        script.write_text("#!/usr/bin/env node\n")
        script.chmod(0o755)

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=str(script) + "\n"))
        monkeypatch.setattr(client_mod, "subprocess_mod", MagicMock(run=mock_run))
        assert _mise_which("claude-agent-acp") == str(script)

    def test_returns_none_when_mise_not_installed(self, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        monkeypatch.setattr(client_mod.shutil, "which", lambda name: None)
        assert _mise_which("claude-agent-acp") is None

    def test_returns_none_on_nonzero_exit(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_run = MagicMock(return_value=MagicMock(returncode=1, stdout=""))
        monkeypatch.setattr(client_mod, "subprocess_mod", MagicMock(run=mock_run))
        assert _mise_which("claude-agent-acp") is None

    def test_returns_none_on_timeout(self, tmp_path, monkeypatch):
        import subprocess

        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_sub = MagicMock()
        mock_sub.run = MagicMock(side_effect=subprocess.TimeoutExpired("mise", 5))
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        monkeypatch.setattr(client_mod, "subprocess_mod", mock_sub)
        assert _mise_which("claude-agent-acp") is None

    @pytest.mark.parametrize("host_encoding", ["cp1252", "ascii"])
    def test_non_ascii_path_survives_a_non_utf8_host_codepage(
        self, tmp_path, monkeypatch, host_encoding
    ):
        """mise prints the path as UTF-8; a profile dir like ``José`` must resolve.

        The stand-in decodes the way ``subprocess`` does: with ``encoding=`` when
        given, else the host code page. cp1252 mangles the path (no such file, so
        the lookup falls through to None); ascii raises ``UnicodeDecodeError``,
        which escapes the ``TimeoutExpired``/``OSError`` handler.
        """
        import subprocess

        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        script = tmp_path / "José" / "node"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        payload = (str(script) + "\n").encode("utf-8")

        def _decoding_run(argv, **kwargs):
            codec = kwargs.get("encoding") or host_encoding
            return subprocess.CompletedProcess(argv, 0, payload.decode(codec), "")

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_sub = MagicMock(run=_decoding_run)
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        monkeypatch.setattr(client_mod, "subprocess_mod", mock_sub)
        assert _mise_which("node") == str(script)


class TestResolveKiroBinEnvOverride:
    """_resolve_kiro_bin honors the KIROCREW_KIRO_BIN override for environments
    (e.g. AgentSpaces/DevSpaces) where the toolbox shim is broken."""

    def test_env_override_used_when_valid(self, tmp_path):
        from kiro_crew.acp.client import _resolve_kiro_bin
        from kiro_crew.kiro_prerequisite import TrustedAcpExecutableSnapshot

        fake = tmp_path / "kiro-cli"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        with (
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=str(fake),
            ),
            patch(
                "kiro_crew.kiro_prerequisite.snapshot_trusted_acp_executable",
                return_value=TrustedAcpExecutableSnapshot("/immutable/kiro-cli"),
            ),
        ):
            assert _resolve_kiro_bin() == "/immutable/kiro-cli"

    def test_resolver_returns_installed_path_without_copying(self, tmp_path):
        # The resolver hands back the user's installed binary path itself. It must
        # never substitute a private copy: Kiro CLI 2.15+ exec's a sibling
        # subcommand binary resolved relative to its own path, so a copy into a
        # flat dir breaks every spawn with ENOENT.
        from kiro_crew.acp import client as client_module

        macos_dir = tmp_path / "Kiro CLI.app" / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True)
        source = macos_dir / "kiro-cli"
        source.write_bytes(b"source")
        source.chmod(0o700)
        (macos_dir / "kiro-cli-chat").write_bytes(b"sibling")

        with patch.object(client_module, "resolve_kiro_cli", return_value=str(source)):
            launch_path = client_module._resolve_kiro_bin()

        assert launch_path == str(source)
        # The sibling the CLI dispatches to is still beside the launch path.
        assert (Path(launch_path).parent / "kiro-cli-chat").exists()

    def test_windows_resolver_accepts_runnable_candidate_anywhere(self, tmp_path):
        from kiro_crew.acp import client as client_module

        # Trust is "it runs": a Windows CLI outside Program Files (winget/scoop,
        # a venv Scripts dir) resolves for ACP launch rather than being rejected.
        candidate = tmp_path / "venv" / "Scripts" / "kiro-cli.exe"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"runnable")
        program_files = tmp_path / "Program Files"

        with (
            patch.object(client_module.platform_compat, "IS_WINDOWS", True),
            patch.object(
                client_module,
                "resolve_kiro_cli",
                return_value=str(candidate),
            ),
            patch.dict(
                "os.environ",
                {"ProgramFiles": str(program_files)},
                clear=True,
            ),
        ):
            assert client_module._resolve_kiro_bin() == os.path.realpath(str(candidate))

    @pytest.mark.asyncio
    async def test_spawn_launches_self_updated_override(self, tmp_path):
        from kiro_crew import sandbox as sandbox_module
        from kiro_crew.acp import client as client_module
        from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

        fake = tmp_path / "kiro-cli"
        fake.write_bytes(b"#!/bin/sh\n# original\n")
        fake.chmod(0o755)
        mock_exec = AsyncMock()
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process; tracked for a de-flake pass
        mock_exec.return_value.pid = 12345
        with (
            patch.dict("os.environ", {"KIROCREW_KIRO_BIN": str(fake)}),
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
            patch.object(client_module, "_get_child_pids", return_value=[]),
            # This test asserts WHICH bytes get launched, not that the spawn is
            # sandboxed (covered by test_sandbox_*.py). A CI runner with
            # kernel.apparmor_restrict_unprivileged_userns=1 genuinely cannot
            # build a namespace sandbox and wrap_argv fail-closes by design, so
            # pin the decision instead of inheriting the host's capability.
            patch.object(sandbox_module, "_allow_unsandboxed_exec", return_value=True),
            patch.object(
                client_module,
                "resolve_kiro_cli",
                return_value=str(fake),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                mock_exec,
            ),
        ):
            # A Kiro self-update legitimately rewrites the binary after gateway
            # start. Trust is "it runs", so the updated bytes still launch — the
            # snapshot just pins whatever bytes are resolved at spawn time.
            KiroPrerequisiteService(home=tmp_path, data_home=tmp_path / "data")
            fake.write_bytes(b"#!/bin/sh\n# self-updated\n")
            fake.chmod(0o755)

            client = AcpClient(work_dir=tmp_path / "workspace")
            await client._spawn()
            assert mock_exec.await_count == 1
            launched = [
                a
                for a in mock_exec.await_args.args
                if isinstance(a, str) and os.path.realpath(a) == os.path.realpath(fake)
            ]
            assert launched, mock_exec.await_args.args
            # The path launched holds the updated bytes, not a pre-update copy.
            assert Path(launched[0]).read_bytes() == b"#!/bin/sh\n# self-updated\n"

            await _stop_stderr_drain(client)

    @pytest.mark.asyncio
    async def test_spawn_passes_installed_path_through_exact_wrappers(self, tmp_path):
        from kiro_crew.acp import client as client_module

        fake = tmp_path / "kiro-cli"
        fake.write_bytes(b"#!/bin/sh\n")
        fake.chmod(0o755)
        launch_path = str(fake)
        mock_exec = AsyncMock(side_effect=RuntimeError("spawn failed"))
        wrapped: dict[str, object] = {}

        def capture_wrap(argv, mode, **kwargs):
            wrapped.update(argv=list(argv), mode=mode, kwargs=kwargs)
            return ["/usr/bin/sandbox-wrapper", *argv], None

        with (
            patch.object(
                client_module,
                "_resolve_kiro_bin",
                return_value=launch_path,
            ),
            patch.object(client_module, "wrap_argv", side_effect=capture_wrap),
            patch.object(
                client_module, "assert_voice_runtime_outside_agent_workspace"
            ) as voice_guard,
            patch.object(
                client_module,
                "cgroup_scope_argv",
                side_effect=lambda argv: ["/usr/bin/cgroup-wrapper", *argv],
            ),
            patch(
                "asyncio.create_subprocess_exec",
                mock_exec,
            ),
        ):
            client = AcpClient(work_dir=tmp_path / "workspace")

            with pytest.raises(RuntimeError, match="spawn failed"):
                await client._spawn()

        launch_argv = wrapped["argv"]
        assert isinstance(launch_argv, list)
        assert launch_argv == [launch_path, "acp", "--agent", client._agent]
        assert wrapped["mode"] == "auto"
        wrap_kwargs = dict(wrapped["kwargs"])
        # The per-session terminal-log window is allocated at spawn time; its
        # path is runtime-owned, so only its presence and shape are pinned here.
        extra_private = wrap_kwargs.pop("extra_private_dirs")
        assert isinstance(extra_private, (list, tuple))
        # The default tier leaves ~/.ssh readable, so the ssh-agent socket is kept.
        assert wrap_kwargs == {
            "strip_python_env": True,
            "is_kiro_cli": True,
            **({} if sys.platform == "win32" else {"forward_ssh_auth_sock": True}),
        }
        voice_guard.assert_called_once_with(client._work_dir)
        spawn_call = mock_exec.await_args
        assert strip_spawn_shim(spawn_call.args) == (
            "/usr/bin/cgroup-wrapper",
            "/usr/bin/sandbox-wrapper",
            launch_path,
            "acp",
            "--agent",
            client._agent,
        )
        # No inherited snapshot descriptor: the installed binary is exec'd in
        # place, so there is nothing to hand down to the wrapper chain.
        #
        # macOS is the exception, and for a different fd:
        # bind_voice_safe_agent_workspace opens the agent workspace as a
        # directory descriptor there so the child enters it by fchdir instead of
        # re-resolving a pathname a same-UID symlink retarget could aim
        # elsewhere. _spawn hands that descriptor to create_subprocess_limited
        # as chdir_fd, which folds exactly that one fd into pass_fds so the
        # spawn shim inherits it. Off darwin the binding returns None, so
        # chdir_fd is None and pass_fds stays absent. The binding is also gated
        # on the harness owning an internal sandbox (ACP_BACKENDS_INTERNAL_SANDBOX,
        # which the default Kiro backend built here belongs to), so a backend
        # outside that set stays on the pathname cwd whatever the platform says.
        if sys.platform == "darwin":
            pass_fds = spawn_call.kwargs["pass_fds"]
            # Exactly the bound workspace descriptor, nothing else: no snapshot
            # fd is inherited here, so the only descriptor handed down is the
            # one the fchdir binding produced. Which int it is, is pinned on
            # every OS by the forced-darwin companion below.
            assert isinstance(pass_fds, tuple)
            assert len(pass_fds) == 1
            assert isinstance(pass_fds[0], int) and pass_fds[0] >= 0
        else:
            assert "pass_fds" not in spawn_call.kwargs

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "the binding opens the workspace as a directory descriptor, which "
            "needs O_DIRECTORY; Windows has no such flag and os.open on a "
            "directory raises, so a forced-darwin binding refuses with "
            "'cannot-verify' before any spawn is attempted"
        ),
    )
    async def test_spawn_passes_exactly_the_bound_workspace_fd_on_darwin(
        self, tmp_path, monkeypatch
    ):
        # The darwin half of the assertion above runs on no CI shard, because no
        # macOS job collects this module. The binding gate is a runtime
        # sys.platform read rather than an import-time constant, so forcing the
        # platform runs the REAL binding on any POSIX host and pins the whole
        # composition: _spawn -> bind_voice_safe_agent_workspace_async ->
        # create_subprocess_limited(chdir_fd=...) -> a pass_fds holding exactly
        # that descriptor. Spy-and-delegate rather than stub, so the fd compared
        # against pass_fds is the one the real binding opened.
        from kiro_crew.acp import client as client_module

        fake = tmp_path / "kiro-cli"
        fake.write_bytes(b"#!/bin/sh\n")
        fake.chmod(0o755)
        launch_path = str(fake)
        mock_exec = AsyncMock(side_effect=RuntimeError("spawn failed"))
        bound_fds: list[int | None] = []
        real_bind = client_module.bind_voice_safe_agent_workspace_async

        async def spy_bind(workspace):
            spawn_dir, descriptor = await real_bind(workspace)
            bound_fds.append(descriptor)
            return spawn_dir, descriptor

        with (
            patch.object(client_module, "_resolve_kiro_bin", return_value=launch_path),
            patch.object(
                client_module,
                "wrap_argv",
                side_effect=lambda argv, mode, **kwargs: (list(argv), None),
            ),
            patch.object(client_module, "assert_voice_runtime_outside_agent_workspace"),
            patch.object(client_module, "cgroup_scope_argv", side_effect=lambda argv: list(argv)),
            patch.object(
                client_module, "bind_voice_safe_agent_workspace_async", side_effect=spy_bind
            ),
            patch("asyncio.create_subprocess_exec", mock_exec),
        ):
            client = AcpClient(work_dir=tmp_path / "workspace")
            monkeypatch.setattr(sys, "platform", "darwin")

            with pytest.raises(RuntimeError, match="spawn failed"):
                await client._spawn()

        assert len(bound_fds) == 1
        bound_fd = bound_fds[0]
        assert isinstance(bound_fd, int)
        # The spy reads the descriptor before _spawn's failure handler closes it,
        # so the value survives even though the fd itself does not.
        assert mock_exec.await_args.kwargs["pass_fds"] == (bound_fd,)

    def test_env_override_ignored_when_missing_file(self, tmp_path):
        # A configured-but-nonexistent path must not be returned; resolution
        # falls through to the normal candidates / PATH lookup.
        from kiro_crew.acp.client import _resolve_kiro_bin

        missing = str(tmp_path / "does-not-exist")
        with (
            patch.dict("os.environ", {"KIROCREW_KIRO_BIN": missing}),
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=None,
            ),
        ):
            assert _resolve_kiro_bin() is None

    def test_env_override_ignored_when_not_executable(self, tmp_path):
        from kiro_crew.acp.client import _resolve_kiro_bin

        nonexec = tmp_path / "kiro-cli"
        nonexec.write_text("#!/bin/sh\n")
        nonexec.chmod(0o644)  # not executable
        with (
            patch.dict("os.environ", {"KIROCREW_KIRO_BIN": str(nonexec)}),
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=None,
            ),
        ):
            assert _resolve_kiro_bin() is None

    def test_no_env_falls_through(self):
        # With no override set, resolution uses the normal candidate/PATH path
        # and never returns the override sentinel.
        from kiro_crew.acp.client import _resolve_kiro_bin

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_KIRO_BIN"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=None,
            ),
        ):
            # Should not raise; returns either a real path or None.
            result = _resolve_kiro_bin()
            assert result is None or isinstance(result, str)


class TestMiseNodeInstallsDir:
    """ACP node resolution must honour mise's real data root.

    ``_mise_node_installs_dir`` must not hardcode ``~/.local/share/mise``,
    silently missing installs whenever ``MISE_DATA_DIR`` or ``XDG_DATA_HOME``
    relocated the data dir — while ``env.mise_data_dir`` already resolved the
    same root correctly for the build toolchain. These pin the consolidated
    behaviour for the helper and both of its consumers' entry points.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("MISE_DATA_DIR", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    def test_default_layout_is_unchanged(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert (
            client_mod._mise_node_installs_dir()
            == tmp_path / ".local" / "share" / "mise" / "installs" / "node"
        )

    def test_mise_data_dir_env_is_honoured(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "custom-mise"))
        assert (
            client_mod._mise_node_installs_dir() == tmp_path / "custom-mise" / "installs" / "node"
        )

    def test_xdg_data_home_is_honoured(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert (
            client_mod._mise_node_installs_dir() == tmp_path / "xdg" / "mise" / "installs" / "node"
        )

    @_POSIX_EXEC_PATHS_ONLY
    def test_resolve_node_for_script_under_custom_mise_data_dir(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "custom-mise"))
        version_dir = tmp_path / "custom-mise" / "installs" / "node" / "22.1.0"
        node_bin = version_dir / "bin" / "node"
        node_bin.parent.mkdir(parents=True)
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)
        script = version_dir / "lib" / "node_modules" / "some-tool" / "cli.js"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\n")

        assert client_mod._resolve_node_for_script(str(script)) == str(node_bin)

    @_POSIX_EXEC_PATHS_ONLY
    def test_resolve_node_for_script_outside_mise_returns_none(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "custom-mise"))
        script = tmp_path / "elsewhere" / "cli.js"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\n")

        assert client_mod._resolve_node_for_script(str(script)) is None
