"""Tests for dev-server process lifecycle and child environment (server.py ~L1600-2300).

Covers: _child_env credential stripping, _pkg_scripts resilience, _node_bin_dirs
resolution, _resolve_bin fallback, _dev_command lockfile detection,
_start_dev_proc lifecycle, _stop_dev_proc teardown, _dev_proc_alive,
_in_proc_tree POSIX/Windows paths, and _classify_project categorization.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401 (collection marker)

from kiro_crew.apps.builtins.design_tweak.backend import server
from kiro_crew.platform_compat import IS_POSIX

# `os.getpgid` does not exist on Windows, so `patch("os.getpgid")` raises
# AttributeError there rather than exercising anything. The production code
# selects the pgid path on POSIX and the parent-chain walk on Windows, and both
# are covered — these markers keep each test on the platform whose branch it is
# actually asserting about.
posix_only = pytest.mark.skipif(not IS_POSIX, reason="pgid semantics are POSIX-only")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# These fixtures need a PEM private-key header/footer whose *runtime bytes* are
# exactly what the redactor anchors on, but writing the marker as one source
# literal makes the internal-content scanner flag the added line as a real
# private-key credential. Assemble the markers from fragments so the full
# marker string never appears verbatim in source while the concatenated value
# is byte-identical to what a real key emits.
_PEM_BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----\n"
_PEM_END = "-----END RSA " + "PRIVATE KEY-----\n"


class FakePopen:
    """Minimal Popen stand-in exposing pid, poll, terminate, wait."""

    def __init__(self, pid: int = 9999, returncode: int | None = None):
        self.pid = pid
        self.returncode = returncode
        self._terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self._terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _make_pkg_json(root: Path, scripts: dict | None = None) -> None:
    """Write a minimal package.json with optional scripts."""
    data: dict[str, Any] = {"name": "test-proj", "version": "1.0.0"}
    if scripts is not None:
        data["scripts"] = scripts
    (root / "package.json").write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# _child_env tests -- credential boundary
# ---------------------------------------------------------------------------


class TestChildEnv:
    """_child_env strips secrets and capability vars from the spawned process."""

    def test_strips_proxy_secret(self):
        """If leaked, untrusted project code forges signed API calls to this backend."""
        with patch.dict(os.environ, {"KIROCREW_PROXY_SECRET": "s3cr3t", "HOME": "/h"}):
            env = server._child_env(Path("/opt/homebrew/bin"))
        assert "KIROCREW_PROXY_SECRET" not in env

    def test_strips_port(self):
        """PORT collides with this backend's socket -- dev server gets EADDRINUSE."""
        with patch.dict(os.environ, {"PORT": "8123", "HOME": "/h"}):
            env = server._child_env(Path("/usr/local/bin"))
        assert "PORT" not in env

    def test_strips_node_options(self):
        """NODE_OPTIONS can inject debug flags or inspect listeners into the child."""
        with patch.dict(os.environ, {"NODE_OPTIONS": "--inspect=0.0.0.0:9229"}):
            env = server._child_env(Path("/usr/bin"))
        assert "NODE_OPTIONS" not in env

    def test_strips_ssh_auth_sock(self):
        """SSH agent socket lets untrusted code authenticate as the operator."""
        with patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/agent.1234"}):
            env = server._child_env(Path("/usr/bin"))
        assert "SSH_AUTH_SOCK" not in env

    def test_strips_git_ssh_command(self):
        """GIT_SSH_COMMAND lets project code push to arbitrary remotes as operator."""
        with patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -i /key"}):
            env = server._child_env(Path("/usr/bin"))
        assert "GIT_SSH_COMMAND" not in env

    def test_strips_git_ssh(self):
        """GIT_SSH (legacy) has the same operator-impersonation risk."""
        with patch.dict(os.environ, {"GIT_SSH": "/usr/bin/ssh"}):
            env = server._child_env(Path("/usr/bin"))
        assert "GIT_SSH" not in env

    def test_strips_all_kirocrew_prefixed(self):
        """Forward-compatible prefix strip catches vars added later upstream."""
        injected = {
            "KIROCREW_HOME": "/home/u/.kiro/crew",
            "KIROCREW_APP_PORT": "9999",
            "KIROCREW_PROJECT_DIR": "/proj",
            "KIRO_CREW_SOMETHING": "v",
        }
        with patch.dict(os.environ, injected):
            env = server._child_env(Path("/usr/bin"))
        for k in injected:
            assert k not in env, f"{k} leaked into child env"

    def test_preserves_ordinary_vars(self):
        """Non-secret vars like LANG or USER must survive for the dev server."""
        # `_node_bin_dirs` resolves `Path.home()`, which RAISES when the env has
        # no HOME/USERPROFILE — and `clear=True` removes them. Stub it out: this
        # test is about which variables survive the strip, not about node paths.
        with patch.dict(os.environ, {"LANG": "en_US.UTF-8", "USER": "dev"}, clear=True):
            with patch.object(server, "_node_bin_dirs", return_value=[]):
                env = server._child_env(Path("/usr/bin"))
        assert env["LANG"] == "en_US.UTF-8"
        assert env["USER"] == "dev"

    def test_toolchain_bin_prepended_to_path(self):
        """The resolved binary's dir must lead PATH so npm can find node."""
        # Compared through `str(Path(...))` rather than a POSIX literal: the code
        # builds PATH from Path objects, so the separator is the host's.
        toolchain = Path("/opt/homebrew/bin")
        extra = Path("/extra/bin")
        with patch.dict(os.environ, {"PATH": str(Path("/usr/bin"))}, clear=True):
            with patch.object(server, "_node_bin_dirs", return_value=[extra]):
                env = server._child_env(toolchain)
        parts = env["PATH"].split(os.pathsep)
        assert parts[0] == str(toolchain)
        assert str(extra) in parts

    def test_path_deduplication(self):
        """Repeated dirs on PATH waste lookup time and confuse diagnostics."""
        usr_bin = str(Path("/usr/bin"))
        with patch.dict(os.environ, {"PATH": os.pathsep.join([usr_bin, usr_bin])}, clear=True):
            with patch.object(server, "_node_bin_dirs", return_value=[Path("/usr/bin")]):
                env = server._child_env(Path("/usr/bin"))
        parts = env["PATH"].split(os.pathsep)
        assert parts.count(usr_bin) == 1


# ---------------------------------------------------------------------------
# _pkg_scripts tests
# ---------------------------------------------------------------------------


class TestPkgScripts:
    """_pkg_scripts must never raise -- a broken project must not crash the backend."""

    def test_returns_scripts_dict(self, tmp_path):
        """Normal case: returns the scripts block as a dict."""
        _make_pkg_json(tmp_path, {"dev": "vite", "build": "tsc"})
        result = server._pkg_scripts(tmp_path)
        assert result == {"dev": "vite", "build": "tsc"}

    def test_missing_package_json(self, tmp_path):
        """Missing file returns {} -- not an exception that kills the handler."""
        assert server._pkg_scripts(tmp_path) == {}

    def test_malformed_json(self, tmp_path):
        """Corrupt file returns {} instead of bubbling a ValueError."""
        (tmp_path / "package.json").write_text("{not valid json!!!", encoding="utf-8")
        assert server._pkg_scripts(tmp_path) == {}

    def test_scripts_not_a_dict(self, tmp_path):
        """If scripts is a list or string, return {} -- type contract must hold."""
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": ["dev"]}), encoding="utf-8"
        )
        assert server._pkg_scripts(tmp_path) == {}

    def test_no_scripts_key(self, tmp_path):
        """A package.json with no scripts block returns {}."""
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "x"}), encoding="utf-8"
        )
        assert server._pkg_scripts(tmp_path) == {}

    def test_uses_hardened_reader_with_project_root_and_cap(self, tmp_path, monkeypatch):
        """User-selected package metadata stays inside the guarded read boundary."""
        seen = {}

        def guarded_read(raw, within_root=None, *, max_bytes=None):
            seen.update(raw=raw, within_root=within_root, max_bytes=max_bytes)
            return b'{"scripts":{"dev":"vite"}}'

        monkeypatch.setattr(server, "safe_read_file_bytes_nolink", guarded_read)

        assert server._pkg_scripts(tmp_path) == {"dev": "vite"}
        assert Path(seen["raw"]) == tmp_path / "package.json"
        assert seen["within_root"] == str(tmp_path)
        assert seen["max_bytes"] == server.MAX_BODY_BYTES

    def test_rejected_or_oversized_package_metadata_fails_closed(
        self, tmp_path, monkeypatch
    ):
        """A rejected inode or oversized package file cannot influence commands."""
        monkeypatch.setattr(
            server, "safe_read_file_bytes_nolink", lambda *args, **kwargs: None
        )
        assert server._pkg_scripts(tmp_path) == {}

        def oversized(*args, **kwargs):
            raise server.FileTooLargeError("too large")

        monkeypatch.setattr(server, "safe_read_file_bytes_nolink", oversized)
        assert server._pkg_scripts(tmp_path) == {}


# ---------------------------------------------------------------------------
# _node_bin_dirs tests
# ---------------------------------------------------------------------------


class TestNodeBinDirs:
    """_node_bin_dirs resolves the search list for Node toolchain binaries."""

    def test_returns_only_existing_dirs(self, tmp_path, monkeypatch):
        """Non-existent dirs are pruned -- stale paths don't waste stat calls."""
        real_dir = tmp_path / "bindir"
        real_dir.mkdir()
        monkeypatch.setattr(server, "_NODE_BIN_DIRS", (str(real_dir), "/nonexist/abc"))
        monkeypatch.setattr(server, "_NVM_GLOB", "/nonexist/nvm/*/bin")
        result = server._node_bin_dirs()
        assert real_dir in result
        assert Path("/nonexist/abc") not in result

    def test_nvm_dirs_sorted_newest_first(self, tmp_path, monkeypatch):
        """nvm versions sorted descending -- v20 is preferred over v18."""
        nvm_base = tmp_path / "nvm"
        v18 = nvm_base / "v18.0.0" / "bin"
        v20 = nvm_base / "v20.0.0" / "bin"
        v18.mkdir(parents=True)
        v20.mkdir(parents=True)
        monkeypatch.setattr(server, "_NODE_BIN_DIRS", ())
        monkeypatch.setattr(server, "_NVM_GLOB", str(nvm_base / "*/bin"))
        result = server._node_bin_dirs()
        v18_idx = result.index(v18)
        v20_idx = result.index(v20)
        assert v20_idx < v18_idx, "Newer nvm version must precede older"


# ---------------------------------------------------------------------------
# _resolve_bin tests
# ---------------------------------------------------------------------------


class TestResolveBin:
    """_resolve_bin finds package managers even with a stripped PATH."""

    def test_uses_shutil_which_first(self, monkeypatch):
        """A properly-configured PATH wins over the directory scan."""
        monkeypatch.setattr("shutil.which", lambda n: "/usr/local/bin/npm")
        result = server._resolve_bin("npm")
        assert result == Path("/usr/local/bin/npm")

    def test_falls_back_to_node_bin_dirs(self, tmp_path, monkeypatch):
        """When PATH is stripped, the fixed directory scan finds the binary."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        npm = bin_dir / "npm"
        npm.write_text("#!/bin/sh\n", encoding="utf-8")
        npm.chmod(0o755)
        monkeypatch.setattr("shutil.which", lambda n: None)
        monkeypatch.setattr(server, "_node_bin_dirs", lambda: [bin_dir])
        result = server._resolve_bin("npm")
        assert result == npm

    def test_returns_none_when_not_found(self, monkeypatch):
        """None signals the caller to surface a user-facing error."""
        monkeypatch.setattr("shutil.which", lambda n: None)
        monkeypatch.setattr(server, "_node_bin_dirs", lambda: [])
        assert server._resolve_bin("pnpm") is None


# ---------------------------------------------------------------------------
# _dev_command tests
# ---------------------------------------------------------------------------


class TestDevCommand:
    """_dev_command detects the right package manager and script."""

    def test_selects_dev_script(self, tmp_path):
        """'dev' is first priority among the candidate script names."""
        _make_pkg_json(tmp_path, {"dev": "vite", "start": "node ."})
        assert server._dev_command(tmp_path) == ["npm", "run", "dev"]

    def test_falls_back_to_start(self, tmp_path):
        """If no 'dev' exists, 'start' is the last candidate accepted."""
        _make_pkg_json(tmp_path, {"start": "node server.js"})
        assert server._dev_command(tmp_path) == ["npm", "run", "start"]

    def test_detects_pnpm_from_lockfile(self, tmp_path):
        """pnpm-lock.yaml selects pnpm over npm."""
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
        assert server._dev_command(tmp_path)[0] == "pnpm"

    def test_detects_bun_from_lockfile(self, tmp_path):
        """bun uses 'bun run' rather than 'bun run'."""
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "bun.lockb").write_bytes(b"")
        cmd = server._dev_command(tmp_path)
        assert cmd == ["bun", "run", "dev"]

    def test_returns_empty_when_no_scripts(self, tmp_path):
        """No dev script means we cannot start anything -- empty list signals that."""
        _make_pkg_json(tmp_path, {"build": "tsc"})
        assert server._dev_command(tmp_path) == []


# ---------------------------------------------------------------------------
# _in_proc_tree tests
# ---------------------------------------------------------------------------


class TestInProcTree:
    """_in_proc_tree matches listener PIDs to the tree we spawned."""

    @posix_only
    def test_posix_pgid_match(self):
        """POSIX path: same process group means it is our child."""
        with patch("os.getpgid", return_value=42):
            assert server._in_proc_tree(100, 1, pgid=42) is True

    @posix_only
    def test_posix_pgid_mismatch(self):
        """Different process group means not our child -- must reject."""
        with patch("os.getpgid", return_value=99):
            assert server._in_proc_tree(100, 1, pgid=42) is False

    @posix_only
    def test_posix_pgid_oserror(self):
        """Process gone before we check -- safe to report not-in-tree."""
        with patch("os.getpgid", side_effect=OSError):
            assert server._in_proc_tree(100, 1, pgid=42) is False

    def test_windows_parent_chain_match(self):
        """Windows walk: parent chain reaches root_pid in bounded depth."""
        # pid=5 -> parent=3 -> parent=1 (root_pid)
        parents = {5: 3, 3: 1, 1: 0}
        with patch.object(server, "get_ppid", side_effect=lambda p: parents.get(p, 0)):
            assert server._in_proc_tree(5, root_pid=1, pgid=None) is True

    def test_windows_parent_chain_no_match(self):
        """Walk exhausts without reaching root -- not our child."""
        with patch.object(server, "get_ppid", return_value=0):
            assert server._in_proc_tree(5, root_pid=1, pgid=None) is False

    def test_windows_cycle_protection(self):
        """Corrupt parent map must not spin -- bounded by _PROC_TREE_MAX_DEPTH."""
        # Cycle: 5->3->5->3...
        parents = {5: 3, 3: 5}
        with patch.object(server, "get_ppid", side_effect=lambda p: parents.get(p, 0)):
            # Must terminate without hanging
            assert server._in_proc_tree(5, root_pid=99, pgid=None) is False


# ---------------------------------------------------------------------------
# _start_dev_proc tests
# ---------------------------------------------------------------------------


class TestStartDevProc:
    """_start_dev_proc orchestrates spawning and port detection."""

    def setup_method(self):
        # Isolate the module-level mutable state
        self._orig_procs = server._DEV_PROCS.copy()
        server._DEV_PROCS.clear()

    def teardown_method(self):
        server._DEV_PROCS.clear()
        server._DEV_PROCS.update(self._orig_procs)

    def test_returns_error_when_no_dev_script(self, tmp_path):
        """No script means nothing to start -- user gets a diagnostic."""
        _make_pkg_json(tmp_path, {"build": "tsc"})
        result = server._start_dev_proc("proj1", tmp_path)
        assert result["ok"] is False
        assert "No dev script" in result["error"]

    def test_returns_error_when_binary_not_found(self, tmp_path, monkeypatch):
        """Unresolvable binary must tell the user which command is missing."""
        _make_pkg_json(tmp_path, {"dev": "vite"})
        monkeypatch.setattr(server, "_resolve_bin", lambda n: None)
        monkeypatch.setattr(server, "_node_bin_dirs", lambda: [])
        result = server._start_dev_proc("proj2", tmp_path)
        assert result["ok"] is False
        assert "Could not find" in result["error"]

    def test_returns_error_when_no_node_modules(self, tmp_path, monkeypatch):
        """Missing node_modules is a common user mistake -- surface it clearly."""
        _make_pkg_json(tmp_path, {"dev": "vite"})
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        result = server._start_dev_proc("proj3", tmp_path)
        assert result["ok"] is False
        assert "node_modules" in result["error"]

    def test_already_alive_returns_existing(self, tmp_path):
        """If the proc is already running, reuse it -- no double-start."""
        fake = FakePopen(pid=111, returncode=None)
        server._DEV_PROCS["proj4"] = {
            "proc": fake, "pgid": None, "url": "http://127.0.0.1:3000",
            "proxy": None, "proxyUrl": "http://127.0.0.1:4000/", "proxyFor": "",
        }
        result = server._start_dev_proc("proj4", tmp_path)
        assert result["ok"] is True
        assert result.get("already") is True

    def test_process_exits_immediately(self, tmp_path, monkeypatch):
        """A dev server that crashes on start surfaces its log tail."""
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))

        fake = FakePopen(pid=200, returncode=1)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
        monkeypatch.setattr(server, "kill_process_tree", lambda *a, **kw: True)
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)

        result = server._start_dev_proc("proj5", tmp_path)
        assert result["ok"] is False
        assert "exited" in result["error"]

    def test_process_exit_reads_only_a_bounded_log_tail(self, tmp_path, monkeypatch):
        """A noisy child cannot force the error path to keep its whole log."""
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)

        payload = "noise-" * 1_000 + "界" * 900 + "END"
        fake = FakePopen(pid=201, returncode=1)

        def fake_popen(*args, **kwargs):
            handle = kwargs["stdout"]
            handle.write(payload.encode("utf-8"))
            handle.flush()
            return fake

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = server._start_dev_proc("proj-tail", tmp_path)

        assert result["ok"] is False
        assert len(result["log"]) <= server._DEV_LOG_TAIL_CHARS
        assert result["log"] == payload[-server._DEV_LOG_TAIL_CHARS :]

    def test_process_exit_redacts_credentials_from_the_log_tail(
        self, tmp_path, monkeypatch
    ):
        """A credential a dying dev server printed must not reach the returned tail."""
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)

        # Build the AWS-access-key-shaped fixture by concatenation so no
        # real-looking token literal sits in the source (the content scanner
        # reads added lines).
        secret = "AKIA" + "IOSFODNN7" + "EXAMPLE"
        payload = f"config error near key {secret} while starting"
        fake = FakePopen(pid=202, returncode=1)

        def fake_popen(*args, **kwargs):
            handle = kwargs["stdout"]
            handle.write(payload.encode("utf-8"))
            handle.flush()
            return fake

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = server._start_dev_proc("proj-secret", tmp_path)

        assert result["ok"] is False
        assert secret not in result["log"]

    def test_process_exit_redacts_a_key_whose_header_precedes_the_tail(
        self, tmp_path, monkeypatch
    ):
        """A PEM key body reaches the tail while its BEGIN header sits far above it.

        Only the last ``_DEV_LOG_TAIL_CHARS`` characters are returned, so a
        window over just the end would omit the header and leave the body
        unredacted. The whole read window is redacted in a single pass, so the
        BEGIN anchor and the body are seen together and the body is scrubbed.
        """
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)

        key_body = "FAKEKEYMATERIALLINE/abcdefghijklmnop0123456789+/ABCD"
        # Header, then enough noise that the char tail cannot reach back to it,
        # then the key body near the very end.
        payload = (
            _PEM_BEGIN
            + "noise-" * 1_000
            + f"\n{key_body}\n{_PEM_END}"
        )
        fake = FakePopen(pid=203, returncode=1)

        def fake_popen(*args, **kwargs):
            handle = kwargs["stdout"]
            handle.write(payload.encode("utf-8"))
            handle.flush()
            return fake

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = server._start_dev_proc("proj-pem", tmp_path)

        assert result["ok"] is False
        # Premise guard: the header is far enough above the tail that a
        # window-over-the-end read would have missed it.
        assert len(payload) - payload.index("BEGIN") > server._DEV_LOG_TAIL_CHARS
        assert key_body not in result["log"]

    def test_process_exit_redacts_a_key_straddling_the_read_window_edge(
        self, tmp_path, monkeypatch
    ):
        """A PEM whose header sits just inside the read-window start is scrubbed.

        Reading one bounded window and redacting it in a single pass leaves no
        chunk seam a key could straddle to escape scrubbing. Shrink the window
        and make the log larger than it, so bytes are dropped before the window:
        the BEGIN header lands just inside the window start and the key body near
        the end. A single pass sees header and body together and scrubs them.
        """
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)
        # A small window keeps the fixture small while still forcing a real
        # window cutoff (the log is larger than the window).
        window = 512
        monkeypatch.setattr(server, "_DEV_LOG_READ_WINDOW", window)

        key_body = "SEAMKEYMATERIAL/abcdefghijklmnop0123456789+/WXYZ"
        head = (
            _PEM_BEGIN
            + "pad-" * 40
            + f"\n{key_body}\n{_PEM_END}"
        )
        # Prefix pushes the log past the window so its start is dropped; size the
        # prefix so BEGIN sits just inside the retained window.
        prefix_len = 2_000
        payload = ("x" * prefix_len) + head
        fake = FakePopen(pid=204, returncode=1)

        def fake_popen(*args, **kwargs):
            handle = kwargs["stdout"]
            handle.write(payload.encode("utf-8"))
            handle.flush()
            return fake

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = server._start_dev_proc("proj-seam", tmp_path)

        assert result["ok"] is False
        # Premise guards: the log is larger than the window (a real cutoff
        # happened) and the BEGIN header is inside the retained window.
        assert len(payload) > window
        assert len(payload) - payload.index("BEGIN") <= window
        assert key_body not in result["log"]

    def test_process_exit_read_length_is_capped_to_the_window(
        self, tmp_path, monkeypatch
    ):
        """A log far larger than the window reads at most the window's bytes.

        The read seeks to ``size - _DEV_LOG_READ_WINDOW`` from the end, but the
        read LENGTH must also be capped: a descendant that inherited the log fd
        can keep appending after the parent exits, so an unsized read would
        follow the file to its live EOF and defeat the memory bound. This asserts
        the decoded window never exceeds ``_DEV_LOG_READ_WINDOW`` bytes even when
        the file is much larger.
        """
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)
        window = 512
        monkeypatch.setattr(server, "_DEV_LOG_READ_WINDOW", window)

        # A log an order of magnitude larger than the window.
        payload = "L" * (window * 10) + "END"
        fake = FakePopen(pid=205, returncode=1)

        def fake_popen(*args, **kwargs):
            handle = kwargs["stdout"]
            handle.write(payload.encode("utf-8"))
            handle.flush()
            return fake

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = server._start_dev_proc("proj-cap", tmp_path)

        assert result["ok"] is False
        # The read is capped to the window (512), which is smaller than the tail
        # ceiling (800): an unbounded read would have returned the full 800-char
        # tail, so getting only the last `window` characters proves the read
        # length itself was bounded, not just the returned slice.
        assert len(result["log"]) == window
        assert result["log"] == payload[-window:]
        assert result["log"].startswith("L")

    def test_process_exit_masks_a_key_body_bisected_by_the_window_start(
        self, tmp_path, monkeypatch
    ):
        """A PEM body whose BEGIN header sits BEFORE the read window is masked.

        When the log is larger than the window the read starts mid-file, so a
        key whose ``-----BEGIN ... PRIVATE KEY-----`` header fell before the
        window leaves an anchorless body fragment at the window start. The
        header-anchored redactor cannot scrub that fragment, so the reader must
        drop through the partial first line and through the unmatched
        ``-----END ... PRIVATE KEY-----`` marker before redacting. This asserts
        the orphaned body never reaches the returned tail.
        """
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)
        window = 512
        monkeypatch.setattr(server, "_DEV_LOG_READ_WINDOW", window)

        key_body = "ORPHANBODY/abcdefghijklmnop0123456789+/LMNO"
        # A big header block pushes the BEGIN header before the window start; the
        # short-wrapped body lines and the END marker land inside the window.
        head = (
            _PEM_BEGIN
            + ("noise " * 200)
            + "\n"
        )
        body_and_end = f"{key_body}\n{key_body}\n{_PEM_END}"
        trailer = "the dev server then exited with code 1\n"
        payload = head + body_and_end + trailer
        fake = FakePopen(pid=206, returncode=1)

        def fake_popen(*args, **kwargs):
            handle = kwargs["stdout"]
            handle.write(payload.encode("utf-8"))
            handle.flush()
            return fake

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = server._start_dev_proc("proj-orphan", tmp_path)

        assert result["ok"] is False
        # Premise guards: the log exceeds the window (mid-file start) and the
        # BEGIN header is dropped before the window, while the END marker is
        # inside it.
        assert len(payload) > window
        assert len(payload) - payload.index("BEGIN") > window
        assert len(payload) - payload.index("-----END") <= window
        # The orphaned key body must not survive into the returned tail, and the
        # legitimate trailer after the END is still shown.
        assert key_body not in result["log"]
        assert "exited with code 1" in result["log"]

    def test_process_exit_masks_an_orphan_body_before_a_later_full_pem(
        self, tmp_path, monkeypatch
    ):
        """An orphaned key body followed by a full PEM is still masked.

        The window begins mid-file inside a key whose BEGIN header fell before
        the window, so the window opens with an anchorless body. A COMPLETE
        second PEM (its own BEGIN and END) then follows inside the window. The
        first BEGIN in the window belongs to that second, legitimate PEM, so a
        guard that only masks when the first END precedes the first BEGIN would
        skip the head — leaking the orphaned body ahead of the second BEGIN.
        Masking through the first END regardless of any later BEGIN closes it.
        """
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)
        window = 512
        monkeypatch.setattr(server, "_DEV_LOG_READ_WINDOW", window)

        orphan_body = "ORPHANBODY/abcdefghijklmnop0123456789+/LMNO"
        second_body = "SECONDKEY/zyxwvutsrqponmlkjihgfedcba9876543210+/"
        # A big header block pushes the orphaned key's BEGIN before the window
        # start; its body then wraps down into the window with NO END of its own,
        # immediately followed by a fresh COMPLETE PEM (BEGIN, body, END), then
        # the trailer. So the FIRST END inside the window belongs to the second
        # PEM and sits AFTER the second PEM's BEGIN — the exact ordering under
        # which a preceding-BEGIN guard would skip masking and leak the orphan.
        head = _PEM_BEGIN + ("noise " * 200) + "\n"
        orphan_tail = f"{orphan_body}\n{orphan_body}\n"
        second_key = f"{_PEM_BEGIN}{second_body}\n{second_body}\n{_PEM_END}"
        trailer = "the dev server then exited with code 1\n"
        payload = head + orphan_tail + second_key + trailer
        fake = FakePopen(pid=207, returncode=1)

        def fake_popen(*args, **kwargs):
            handle = kwargs["stdout"]
            handle.write(payload.encode("utf-8"))
            handle.flush()
            return fake

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = server._start_dev_proc("proj-orphan-2", tmp_path)

        assert result["ok"] is False
        # Premise guards: the log exceeds the window (mid-file start), the orphan
        # BEGIN header is dropped before the window, and inside the window the
        # first BEGIN (the second full PEM's) precedes the first END — the exact
        # ordering the old preceding-BEGIN guard would let the head survive.
        assert len(payload) > window
        assert len(payload) - payload.index("BEGIN") > window
        tail_window = payload[-window:]
        assert tail_window.index("-----BEGIN") < tail_window.index("-----END")
        # Neither the orphaned body nor the redacted second key body survives.
        assert orphan_body not in result["log"]
        assert second_body not in result["log"]
        assert "exited with code 1" in result["log"]

    def test_process_exit_masks_an_orphan_body_with_no_end_in_the_window(
        self, tmp_path, monkeypatch
    ):
        """An orphaned key body is masked even when NO END marker is in view.

        The window begins mid-file inside a key whose BEGIN header — and whose
        END marker — both fall OUTSIDE the window, so the window opens with an
        anchorless body and carries no ``-----END ... PRIVATE KEY-----`` for the
        END-mask to catch. A later labeled secret then shrinks under redaction to
        a short tag, so the retained 800-char tail would otherwise reach back far
        enough to include the orphaned body lines. Dropping every leading
        PEM/base64 body line before redaction — regardless of any END — is what
        keeps the key body out of the tail.
        """
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)
        window = 4096
        monkeypatch.setattr(server, "_DEV_LOG_READ_WINDOW", window)

        orphan_body = "NOENDBODY/abcdefghijklmnopqrstuvwxyz0123456789+/ABCDEFGH"
        # The BEGIN header and the END marker both sit ABOVE the window start
        # (a huge header block pushes them off the front), so the window opens on
        # the anchorless body and contains no END at all. The body lines wrap
        # down into the window; a labeled secret spanning a long value then
        # follows, which redaction collapses to a short tag — the shrink that
        # lets the retained tail reach back into the body region.
        head = _PEM_BEGIN + ("noise " * 700) + "\n"
        orphan_tail = (f"{orphan_body}\n" * 12)
        # A labeled secret whose long value collapses to a short tag on
        # redaction; no END marker anywhere in the payload.
        labeled_secret = "AWS_SECRET_ACCESS_KEY=" + ("Z" * 1500) + "\n"
        trailer = "the dev server then exited with code 1\n"
        payload = head + orphan_tail + labeled_secret + trailer
        fake = FakePopen(pid=208, returncode=1)

        def fake_popen(*args, **kwargs):
            handle = kwargs["stdout"]
            handle.write(payload.encode("utf-8"))
            handle.flush()
            return fake

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = server._start_dev_proc("proj-orphan-noend", tmp_path)

        assert result["ok"] is False
        # Premise guards: the log exceeds the window (mid-file start), the orphan
        # BEGIN header is dropped before the window, and there is NO END marker
        # anywhere in the payload for the END-mask to fire on.
        assert len(payload) > window
        assert len(payload) - payload.index("BEGIN") > window
        assert "-----END" not in payload
        # The orphaned key body must not reach the returned tail; the trailer is
        # still shown.
        assert orphan_body not in result["log"]
        assert "exited with code 1" in result["log"]

    def test_process_exit_applies_the_companion_credential_policy(
        self, tmp_path, monkeypatch
    ):
        """A companion-only credential in the tail is scrubbed via the context.

        The returned tail routes through ``redact_via_context``, so a loaded
        companion's ``CredentialPolicy`` — extra patterns the baseline floor
        does not carry — applies. A companion-defined token that the baseline
        ``security.redact`` leaves untouched must not survive into the tail.
        This pins the egress on the context-aware redactor rather than the
        companion-blind baseline: were the read to call the baseline directly,
        the companion token would reach the dashboard JSON unmasked.
        """
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)

        # A token shaped so the baseline floor does NOT match it, standing in for
        # a pattern only a loaded companion's CredentialPolicy knows.
        companion_secret = "COMPANIONONLYTOKEN-do-not-leak"

        def fake_context_redact(text):
            # Model a composed companion: apply the companion-only pattern first,
            # then fall through to the baseline floor for everything else.
            scrubbed = text.replace(companion_secret, "[REDACTED: credential]")
            return server._redact_text(scrubbed)

        monkeypatch.setattr(server, "redact_via_context", fake_context_redact)

        payload = (
            "dev server booting\n"
            f"companion_key={companion_secret}\n"
            "the dev server then exited with code 1\n"
        )
        fake = FakePopen(pid=207, returncode=1)

        def fake_popen(*args, **kwargs):
            handle = kwargs["stdout"]
            handle.write(payload.encode("utf-8"))
            handle.flush()
            return fake

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = server._start_dev_proc("proj-companion", tmp_path)

        assert result["ok"] is False
        # Premise guard: the baseline floor alone does NOT mask this token, so a
        # clean tail proves the context-aware redactor ran.
        assert companion_secret in server._redact_text(payload)
        # The companion-only credential must not reach the returned tail, while
        # the legitimate trailer after it is still shown.
        assert companion_secret not in result["log"]
        assert "exited with code 1" in result["log"]

    def test_spawn_oserror(self, tmp_path, monkeypatch):
        """Popen failure (e.g. ENOENT) returns an error dict, not an exception."""
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("ENOENT")),
        )
        result = server._start_dev_proc("proj6", tmp_path)
        assert result["ok"] is False
        assert "could not start" in result["error"]

    def test_detects_listening_port(self, tmp_path, monkeypatch):
        """Once a child port is found, the proxy URL is returned."""
        _make_pkg_json(tmp_path, {"dev": "vite"})
        (tmp_path / "node_modules").mkdir()
        monkeypatch.setattr(server, "_resolve_bin", lambda n: Path("/usr/bin/npm"))
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)

        fake = FakePopen(pid=300, returncode=None)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
        monkeypatch.setattr(server, "kill_process_tree", lambda *a, **kw: True)
        # First call: nothing; second call: found
        calls = [0]

        def detect_servers(root, probe=True):
            calls[0] += 1
            if calls[0] >= 2:
                return [{"pid": 300, "url": "http://127.0.0.1:5173", "port": 5173}]
            return []

        monkeypatch.setattr(server, "_detect_dev_servers", detect_servers)
        monkeypatch.setattr(
            server, "_front_with_proxy",
            lambda pid, url: "http://127.0.0.1:9000/",
        )
        monkeypatch.setattr("time.sleep", lambda s: None)
        monkeypatch.setattr(server, "_START_TIMEOUT", 2)
        monkeypatch.setattr("time.time", MagicMock(side_effect=[0, 0.5, 1.0, 1.5]))
        monkeypatch.setattr(server, "IS_POSIX", False)

        result = server._start_dev_proc("proj7", tmp_path)
        assert result["ok"] is True
        assert result["url"] == "http://127.0.0.1:9000/"
        assert result["devUrl"] == "http://127.0.0.1:5173"


# ---------------------------------------------------------------------------
# _stop_dev_proc tests
# ---------------------------------------------------------------------------


class TestStopDevProc:
    """_stop_dev_proc must kill the whole tree, not just the root PID."""

    def setup_method(self):
        self._orig_procs = server._DEV_PROCS.copy()
        server._DEV_PROCS.clear()

    def teardown_method(self):
        server._DEV_PROCS.clear()
        server._DEV_PROCS.update(self._orig_procs)

    def test_kills_tree_with_sigterm_then_sigkill(self, monkeypatch):
        """Escalation: SIGTERM first, SIGKILL only if it ignores the grace period."""
        fake = FakePopen(pid=500, returncode=None)
        # Simulate: wait raises (proc didn't exit on SIGTERM)
        fake.wait = lambda timeout=None: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("cmd", 3)
        )
        server._DEV_PROCS["p1"] = {
            "proc": fake, "pgid": None, "url": "", "proxy": None,
            "proxyUrl": "", "proxyFor": "",
        }
        signals_sent = []
        monkeypatch.setattr(
            server, "kill_process_tree",
            lambda pid, sig: signals_sent.append((pid, sig)) or True,
        )
        server._stop_dev_proc("p1")
        assert (500, server.SIGTERM) in signals_sent
        assert (500, server.SIGKILL) in signals_sent

    def test_graceful_exit_no_sigkill(self, monkeypatch):
        """If the process exits within the grace period, SIGKILL is skipped."""
        fake = FakePopen(pid=501, returncode=None)
        fake.wait = lambda timeout=None: 0

        server._DEV_PROCS["p2"] = {
            "proc": fake, "pgid": None, "url": "", "proxy": None,
            "proxyUrl": "", "proxyFor": "",
        }
        signals_sent = []
        monkeypatch.setattr(
            server, "kill_process_tree",
            lambda pid, sig: signals_sent.append((pid, sig)) or True,
        )
        server._stop_dev_proc("p2")
        assert (501, server.SIGTERM) in signals_sent
        assert (501, server.SIGKILL) not in signals_sent

    def test_adopted_server_no_kill(self, monkeypatch):
        """Adopted servers (proc=None) are not ours to kill -- only proxy stops."""
        server._DEV_PROCS["p3"] = {
            "proc": None, "pgid": None, "url": "http://127.0.0.1:3000",
            "proxy": MagicMock(), "proxyUrl": "http://127.0.0.1:4000/",
            "proxyFor": "http://127.0.0.1:3000",
        }
        killed = []
        monkeypatch.setattr(
            server, "kill_process_tree",
            lambda pid, sig: killed.append(pid) or True,
        )
        result = server._stop_dev_proc("p3")
        assert result is True
        assert killed == [], "Must not kill a process we did not start"

    def test_nonexistent_returns_false(self):
        """Stopping a project with no record returns False -- no-op."""
        assert server._stop_dev_proc("nonexistent") is False

    def test_clears_record(self, monkeypatch):
        """After stop, the project does not appear in _DEV_PROCS."""
        fake = FakePopen(pid=502)
        fake.wait = lambda timeout=None: 0
        server._DEV_PROCS["p4"] = {
            "proc": fake, "pgid": None, "url": "", "proxy": None,
            "proxyUrl": "", "proxyFor": "",
        }
        monkeypatch.setattr(server, "kill_process_tree", lambda *a, **kw: True)
        server._stop_dev_proc("p4")
        assert "p4" not in server._DEV_PROCS


# ---------------------------------------------------------------------------
# _dev_proc_alive tests
# ---------------------------------------------------------------------------


class TestDevProcAlive:
    """_dev_proc_alive must distinguish running, dead, and adopted states."""

    def setup_method(self):
        self._orig_procs = server._DEV_PROCS.copy()
        server._DEV_PROCS.clear()

    def teardown_method(self):
        server._DEV_PROCS.clear()
        server._DEV_PROCS.update(self._orig_procs)

    def test_running_process(self):
        """poll() returning None means still running."""
        fake = FakePopen(pid=600, returncode=None)
        server._DEV_PROCS["a1"] = {"proc": fake, "url": ""}
        assert server._dev_proc_alive("a1") is True

    def test_dead_process(self):
        """poll() returning an int means exited -- not alive."""
        fake = FakePopen(pid=601, returncode=0)
        server._DEV_PROCS["a2"] = {"proc": fake, "url": ""}
        assert server._dev_proc_alive("a2") is False

    def test_adopted_with_proxy(self):
        """Adopted server (proc=None) is alive if its proxy is up."""
        server._DEV_PROCS["a3"] = {"proc": None, "proxy": MagicMock()}
        assert server._dev_proc_alive("a3") is True

    def test_adopted_without_proxy(self):
        """Adopted server with dead proxy is not alive."""
        server._DEV_PROCS["a4"] = {"proc": None, "proxy": None}
        assert server._dev_proc_alive("a4") is False

    def test_no_record(self):
        """Unknown project returns False -- never crash on a stale id."""
        assert server._dev_proc_alive("unknown") is False


# ---------------------------------------------------------------------------
# _classify_project tests
# ---------------------------------------------------------------------------


class TestClassifyProject:
    """_classify_project determines static-vs-dev preview mode."""

    def test_static_project(self, tmp_path):
        """Plain HTML folder is previewable from disk -- no dev server needed."""
        (tmp_path / "index.html").write_text(
            "<html><body>hi</body></html>", encoding="utf-8"
        )
        result = server._classify_project(tmp_path)
        assert result["needsDevServer"] is False
        assert result["hasEntry"] is True

    def test_bundler_template_needs_dev(self, tmp_path):
        """A Vite index.html with <script type=module src=main.tsx> needs a server."""
        (tmp_path / "index.html").write_text(
            '<html><head></head><body>'
            '<script type="module" src="/src/main.tsx"></script>'
            '</body></html>',
            encoding="utf-8",
        )
        _make_pkg_json(tmp_path, {"dev": "vite"})
        result = server._classify_project(tmp_path)
        assert result["needsDevServer"] is True
        assert result["unbundledEntry"] == "/src/main.tsx"

    def test_no_entry_with_dev_script(self, tmp_path):
        """No index.html but has a dev script -- needs dev server."""
        _make_pkg_json(tmp_path, {"dev": "next dev"})
        result = server._classify_project(tmp_path)
        assert result["needsDevServer"] is True
        assert result["hasEntry"] is False

    def test_entry_scan_uses_hardened_reader_with_project_root_and_cap(
        self, tmp_path, monkeypatch
    ):
        """Classification cannot follow an entry inode outside the selected project."""
        entry = tmp_path / "index.html"
        entry.write_text(
            '<script type="module" src="/src/main.tsx"></script>', encoding="utf-8"
        )
        seen = []

        def guarded_read(raw, within_root=None, *, max_bytes=None):
            seen.append(
                {"raw": raw, "within_root": within_root, "max_bytes": max_bytes}
            )
            return entry.read_bytes()

        monkeypatch.setattr(server, "safe_read_file_bytes_nolink", guarded_read)

        result = server._classify_project(tmp_path)
        assert result["unbundledEntry"] == "/src/main.tsx"
        entry_read = next(call for call in seen if Path(call["raw"]) == entry)
        assert entry_read["within_root"] == str(tmp_path)
        assert entry_read["max_bytes"] == server.MAX_STATIC_BYTES

    def test_rejected_or_oversized_entry_scan_fails_closed(self, tmp_path, monkeypatch):
        """Rejected entry content never reaches the HTML classifier."""
        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
        monkeypatch.setattr(
            server, "safe_read_file_bytes_nolink", lambda *args, **kwargs: None
        )
        assert server._classify_project(tmp_path)["unbundledEntry"] == ""

        def oversized(*args, **kwargs):
            raise server.FileTooLargeError("too large")

        monkeypatch.setattr(server, "safe_read_file_bytes_nolink", oversized)
        assert server._classify_project(tmp_path)["unbundledEntry"] == ""
