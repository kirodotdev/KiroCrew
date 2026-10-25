"""Sandbox backend detection, ``wrap_argv``'s routing, and the macOS Seatbelt profile.

The profile is asserted on the rule text its renderer emits: the credential and
hardlink denies, the voice-runtime seal and its workspace-overlap guard, and the
descriptor-bound workspace binding.
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sandbox_argv_helpers

import kiro_crew.sandbox as sandbox_mod
from kiro_crew.sandbox import (
    _CC_FILES,
    _STRICT_DIRS,
    _build_seatbelt_profile,
    detect_backend,
    wrap_argv,
)

# Several tests of this suite spawn real child interpreters (subprocess.run([sys.executable,
# ...])); every part of it pins itself to the same dedicated xdist worker so concurrent
# cold-starts under -n auto don't starve each other / blow the 30s timeout. Requires
# --dist loadgroup.
pytestmark = [
    pytest.mark.xdist_group(name="subprocess_spawn"),
    # test_sandbox_argv.py replaces ctypes.CDLL, find_library or a cached libc handle for a
    # test; every part of the suite puts the shared macOS handle caches back after each
    # test (see conftest.py).
    pytest.mark.usefixtures("fresh_darwin_libc_handles"),
]


# The shared fixtures, bound here by name so pytest registers them for this module.
clean_backend = sandbox_argv_helpers.clean_backend


class TestDetectBackend:
    def test_off_mode(self):
        result = detect_backend(config_mode="off")
        assert result == "none"

    @patch("kiro_crew.sandbox._probe_unshare", return_value=False)
    @patch("kiro_crew.sandbox._probe_sandbox_exec", return_value=False)
    def test_no_backend_available(self, mock_sb, mock_ns):
        result = detect_backend(config_mode="auto")
        assert result == "none"

    @patch("kiro_crew.sandbox._probe_unshare", return_value=True)
    def test_linux_namespace(self, mock_ns):
        result = detect_backend(config_mode="auto")
        assert result == "namespace"

    @patch("kiro_crew.sandbox._probe_unshare", return_value=False)
    @patch("kiro_crew.sandbox._probe_sandbox_exec", return_value=True)
    def test_macos_sandbox_exec(self, mock_sb, mock_ns):
        result = detect_backend(config_mode="auto")
        assert result == "sandbox-exec"

    @patch("kiro_crew.sandbox._probe_unshare", return_value=True)
    def test_caches_result(self, mock_ns):
        assert detect_backend(config_mode="auto") == "namespace"
        assert detect_backend(config_mode="auto") == "namespace"
        assert mock_ns.call_count == 1

    @patch("kiro_crew.sandbox._probe_unshare", return_value=True)
    def test_invalidates_on_mode_change(self, mock_ns):
        assert detect_backend(config_mode="auto") == "namespace"
        assert detect_backend(config_mode="off") == "none"
        assert mock_ns.call_count == 1  # off doesn't probe


class TestWrapArgv:
    @patch("kiro_crew.sandbox._allow_unsandboxed_exec", return_value=True)
    @patch("kiro_crew.sandbox.detect_backend", return_value="none")
    def test_no_sandbox_returns_original(self, mock_detect, mock_allow):
        argv = ["kiro-cli", "acp"]
        result, cleanup = wrap_argv(argv, mode="auto")
        assert result == argv
        assert cleanup is None

    def test_off_mode_returns_original(self):
        argv = ["kiro-cli", "acp"]
        result, cleanup = wrap_argv(argv, mode="off")
        assert result == argv
        assert cleanup is None

    @patch("kiro_crew.sandbox.detect_backend", return_value="namespace")
    @patch("kiro_crew.sandbox.namespace_argv")
    def test_namespace_backend(self, mock_ns_argv, mock_detect):
        # Stub must carry the real shape: [python, *flags, script, *argv]. Without
        # the flags, wrap_argv's cleanup-path lookup reads past the end.
        mock_ns_argv.return_value = [
            sys.executable,
            *sandbox_mod._LAUNCHER_INTERPRETER_FLAGS,
            "/tmp/launcher.py",
            "kiro-cli",
        ]
        result, cleanup = wrap_argv(["kiro-cli"], mode="strict")
        mock_ns_argv.assert_called_once_with(
            ["kiro-cli"], "strict", strip_python_env=False, forward_ssh_auth_sock=False
        )
        assert result == mock_ns_argv.return_value
        assert cleanup == "/tmp/launcher.py"

    @patch("kiro_crew.sandbox.detect_backend", return_value="sandbox-exec")
    @patch("kiro_crew.sandbox.sandbox_exec_argv")
    def test_sandbox_exec_backend(self, mock_sb_argv, mock_detect):
        mock_sb_argv.return_value = (["sandbox-exec", "-f", "/tmp/p.sb", "kiro-cli"], "/tmp/p.sb")
        result, cleanup = wrap_argv(["kiro-cli"], mode="strict")
        mock_sb_argv.assert_called_once_with(
            ["kiro-cli"], "strict", strip_python_env=False, forward_ssh_auth_sock=False
        )
        assert (result, cleanup) == mock_sb_argv.return_value

    @patch("kiro_crew.sandbox.detect_backend")
    def test_inside_sandbox_passes_through(self, mock_detect, monkeypatch):
        # Inside an existing Kiro Crew sandbox, nested unshare is seccomp-denied,
        # so wrap_argv must pass the argv through unchanged without consulting a
        # backend (rather than fail closed and brick script-cron MCP spawns).
        # Deny-by-default: the passthrough is gated SOLELY on the explicit
        # KIROCREW_SANDBOX_ACTIVE marker (not the dual-purpose KIROCREW_HOST_PID).
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        # Fix the macOS kernel cross-check explicitly: "unanswerable" (None) is the
        # platform-neutral input, so this assertion holds on a sandboxed dev
        # machine and an unsandboxed CI runner alike.
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
        argv = ["kiro-cli", "acp"]
        with patch("kiro_crew.sel.sel") as mock_sel:
            result, cleanup = wrap_argv(argv, mode="strict")
        assert result == argv
        assert cleanup is None
        mock_detect.assert_not_called()
        # A security-relevant passthrough must be SEL-audited (outcome allowed),
        # mirroring the denied event on the fail-closed path. critical=True so
        # the event is written synchronously (no silent async-transport drop).
        mock_sel.return_value.log_tool_invocation.assert_called_once()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "allowed"
        assert kwargs["critical"] is True

    @patch("kiro_crew.sandbox.detect_backend")
    def test_inside_sandbox_passthrough_survives_sel_failure(self, mock_detect, monkeypatch):
        # A SEL write failure must NOT brick the passthrough: seccomp denies the
        # re-wrap by design, so denying here reintroduces a prior in-sandbox
        # spawn outage (every in-sandbox MCP spawn bricked). The spawn is
        # confined by the outer namespace regardless, so we log and proceed.
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
        argv = ["kiro-cli", "acp"]
        with patch("kiro_crew.sel.sel", side_effect=OSError("SEL transport down")):
            result, cleanup = wrap_argv(argv, mode="strict")
        assert result == argv
        assert cleanup is None
        mock_detect.assert_not_called()

    @patch("kiro_crew.sandbox.detect_backend", return_value="none")
    def test_host_pid_alone_does_not_pass_through(self, mock_detect, monkeypatch):
        # Deny-by-default: KIROCREW_HOST_PID is dual-purpose session-identity
        # plumbing, so it must NOT by itself open the nested-sandbox passthrough.
        # Only the explicit KIROCREW_SANDBOX_ACTIVE marker does.
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "12345")
        with patch("kiro_crew.sandbox._allow_unsandboxed_exec", return_value=True):
            result, cleanup = wrap_argv(["kiro-cli"], mode="strict")
        # Falls through to normal backend detection rather than passing through.
        mock_detect.assert_called_once()
        assert mock_detect.call_args.kwargs == {"config_mode": "strict"}
        assert (result, cleanup) == (["kiro-cli"], None)

    @patch("kiro_crew.sandbox.detect_backend", return_value="none")
    def test_outside_sandbox_does_not_pass_through(self, mock_detect, monkeypatch):
        # No marker set → normal wrap path (here: no backend), proving the
        # passthrough is gated strictly on the in-sandbox marker.
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        with patch("kiro_crew.sandbox._allow_unsandboxed_exec", return_value=True):
            result, cleanup = wrap_argv(["kiro-cli"], mode="strict")
        mock_detect.assert_called_once()
        assert mock_detect.call_args.kwargs == {"config_mode": "strict"}
        assert (result, cleanup) == (["kiro-cli"], None)


class TestBuildSeatbeltProfile:
    def test_strict_denies_all_dirs(self):
        profile = _build_seatbelt_profile("strict")
        assert "(version 1)" in profile
        assert "(deny file-read*" in profile
        home = str(Path.home())
        for d in _STRICT_DIRS:
            assert os.path.join(home, d) in profile

    def test_strict_denies_ssh_write(self):
        profile = _build_seatbelt_profile("strict")
        assert "(deny file-write*" in profile
        assert ".ssh" in profile

    @pytest.mark.parametrize("level", ["standard", "cc", "strict"])
    def test_every_mode_seals_voice_runtime_from_agents(self, level):
        profile = _build_seatbelt_profile(level)
        home = str(Path.home())
        for relative in (".kiro/crew/run/voice-runtime", ".kirocrew/run/voice-runtime"):
            path = os.path.join(home, relative)
            assert f'(deny file-read* (subpath "{path}"))' in profile
            assert f'(deny file-write* (subpath "{path}"))' in profile
            assert f'(deny file-link (subpath "{path}"))' in profile

    def test_voice_runtime_cannot_be_reexposed_or_missed_by_relocation(self, monkeypatch, tmp_path):
        custom_home = tmp_path / "custom-home"
        custom_home.mkdir()
        relocated = custom_home / "run" / "voice-runtime"
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: custom_home)

        profile = _build_seatbelt_profile("standard", extra_visible_dirs=(str(relocated),))

        assert f'(deny file-read* (subpath "{relocated}"))' in profile
        assert f'(deny file-write* (subpath "{relocated}"))' in profile
        assert f'(deny file-link (subpath "{relocated}"))' in profile

    def test_voice_runtime_denies_lexical_and_canonical_paths_and_parent_renames(
        self, monkeypatch, tmp_path
    ):
        lexical_home = tmp_path / "linked-home"
        canonical_home = tmp_path / "real-home"
        lexical_run = lexical_home / "run"
        canonical_run = canonical_home / "run"
        lexical_root = lexical_run / "voice-runtime"
        canonical_root = canonical_run / "voice-runtime"
        guards = sandbox_mod._literal_ancestor_guards((str(lexical_run), str(canonical_run)))
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: lexical_home)
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_paths_cache",
            (
                str(lexical_home),
                str(canonical_root),
                (str(lexical_root), str(canonical_root)),
                (str(lexical_run), str(canonical_run)),
                guards,
            ),
        )

        profile = _build_seatbelt_profile("standard")

        for root in (lexical_root, canonical_root):
            assert f'(deny file-read* (subpath "{root}"))' in profile
            assert f'(deny file-write* (subpath "{root}"))' in profile
        for parent in (lexical_run, canonical_run):
            assert f'(deny file-write* (literal "{parent}"))' in profile
            assert f'(deny file-write* (subpath "{parent}"))' in profile
        for guard in guards:
            assert f'(deny file-write* (literal "{guard}"))' in profile

    def test_delegated_macos_agent_workspace_cannot_reach_voice_runtime(
        self, monkeypatch, tmp_path
    ):
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        sibling = tmp_path / "workspace"
        runtime.mkdir(parents=True)
        sibling.mkdir()
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )

        for unsafe in (runtime, runtime / "nested", runtime.parent, tmp_path):
            with pytest.raises(RuntimeError, match="protected voice runtime"):
                sandbox_mod.assert_voice_runtime_outside_agent_workspace(unsafe)

        sandbox_mod.assert_voice_runtime_outside_agent_workspace(sibling)

    def test_voice_runtime_workspace_conflict_preflight(self, monkeypatch, tmp_path):
        """The non-raising pre-flight mirrors the lexical guard and
        names both paths, the data home, and the remedy."""
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        sibling = tmp_path / "workspace"
        runtime.mkdir(parents=True)
        sibling.mkdir()
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )

        contains = sandbox_mod.voice_runtime_workspace_conflict(tmp_path)
        assert contains is not None and "contains" in contains
        assert "protected voice runtime" in contains
        assert str(tmp_path) in contains
        assert str(runtime) in contains
        # Same remedy sentence as the spawn-time refusal (the shared formatter).
        assert "Pick a project subdirectory" in contains

        inside = sandbox_mod.voice_runtime_workspace_conflict(runtime / "nested")
        assert inside is not None and "inside it" in inside

        assert sandbox_mod.voice_runtime_workspace_conflict(sibling) is None

    def test_voice_runtime_workspace_conflict_passes_off_darwin(self, monkeypatch, tmp_path):
        """The pre-flight matches the guards it mirrors: every spawn-time
        guard early-returns off macOS, so an overlapping workspace spawns
        fine on Linux/Windows today — the pre-flight must not 400 a working
        configuration there."""
        monkeypatch.setattr(sandbox_mod.sys, "platform", "linux")
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        runtime.mkdir(parents=True)
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        assert sandbox_mod.voice_runtime_workspace_conflict(tmp_path) is None

    def test_preflight_and_spawn_guard_refuse_with_the_same_message(self, monkeypatch, tmp_path):
        """Drift pin: the pre-flight and the
        spawn-time guard share ONE containment scan and ONE formatter, so the
        same overlapping workspace must produce byte-identical refusal text on
        both surfaces. If either ever grows its own copy again, this fails."""
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        runtime.mkdir(parents=True)
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        preflight = sandbox_mod.voice_runtime_workspace_conflict(tmp_path)
        assert preflight is not None
        with pytest.raises(RuntimeError) as exc:
            sandbox_mod.assert_voice_runtime_outside_agent_workspace(tmp_path)
        assert str(exc.value) == preflight

    def test_voice_runtime_workspace_conflict_fails_open_on_prime_error(
        self, monkeypatch, tmp_path
    ):
        """Pre-flight passes when runtime paths cannot resolve; the spawn-time
        guard (fail-closed) stays authoritative."""

        def _boom() -> tuple[str, ...]:
            raise OSError("no data home")

        monkeypatch.setattr(sandbox_mod, "_voice_runtime_sandbox_paths", _boom)
        assert sandbox_mod.voice_runtime_workspace_conflict(tmp_path) is None

    def test_delegated_macos_agent_workspace_checks_canonical_alias(self, monkeypatch, tmp_path):
        runtime = tmp_path / "real-data" / "run" / "voice-runtime"
        alias = tmp_path / "linked-runtime"
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        realpath = sandbox_mod.os.path.realpath
        monkeypatch.setattr(
            sandbox_mod.os.path,
            "realpath",
            lambda path: str(runtime) if os.fspath(path) == str(alias) else realpath(path),
        )

        with pytest.raises(RuntimeError, match="protected voice runtime") as excinfo:
            sandbox_mod.assert_voice_runtime_outside_agent_workspace(alias)

        # The refusal names the workspace as the caller spelled it (the
        # symlink), not its canonical resolution, and classifies a hit found
        # only on the canonical spelling as an alias relationship.
        message = str(excinfo.value)
        assert os.path.abspath(str(alias)) in message
        assert "aliases" in message
        assert str(runtime) in message

    @pytest.mark.parametrize(
        ("runtime_leaf", "workspace_leaf"),
        [
            ("voice-runtime", "VOICE-RUNTIME"),
            (
                "v\N{LATIN SMALL LETTER E WITH ACUTE}locit\N{LATIN SMALL LETTER Y WITH ACUTE}",
                "ve\N{COMBINING ACUTE ACCENT}locity\N{COMBINING ACUTE ACCENT}",
            ),
        ],
    )
    def test_delegated_macos_agent_workspace_rejects_apfs_spelling_aliases(
        self, monkeypatch, tmp_path, runtime_leaf, workspace_leaf
    ):
        runtime = tmp_path / "data" / "run" / runtime_leaf
        workspace = tmp_path / "data" / "run" / workspace_leaf
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )

        with pytest.raises(RuntimeError, match="protected voice runtime"):
            sandbox_mod.assert_voice_runtime_outside_agent_workspace(workspace)

    def test_delegated_macos_agent_workspace_rejects_filesystem_identity_alias(
        self, monkeypatch, tmp_path
    ):
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        workspace = tmp_path / "workspace"
        runtime.mkdir(parents=True)
        workspace.mkdir()
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        real_stat = sandbox_mod.os.stat
        runtime_info = real_stat(runtime)
        monkeypatch.setattr(
            sandbox_mod.os,
            "stat",
            lambda path: (
                runtime_info
                if os.path.abspath(os.fspath(path)) == os.path.abspath(str(workspace))
                else real_stat(path)
            ),
        )

        with pytest.raises(RuntimeError, match="protected voice runtime"):
            sandbox_mod.assert_voice_runtime_outside_agent_workspace(workspace)

    def test_voice_guard_refusal_names_both_paths_when_workspace_contains_runtime(
        self, monkeypatch, tmp_path
    ):
        """Lexical 'contains' variant: both absolute paths plus the remedy."""
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        runtime.mkdir(parents=True)
        workspace = tmp_path
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )

        with pytest.raises(RuntimeError) as excinfo:
            sandbox_mod.assert_voice_runtime_outside_agent_workspace(workspace)

        message = str(excinfo.value)
        assert str(workspace) in message
        assert str(runtime) in message
        assert "contains" in message
        assert (
            "Pick a project subdirectory that does not contain the Kiro Crew data home." in message
        )

    def test_voice_guard_refusal_names_both_paths_when_workspace_is_inside_runtime(
        self, monkeypatch, tmp_path
    ):
        """Lexical 'inside' variant: both absolute paths plus the remedy."""
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        workspace = runtime / "nested"
        workspace.mkdir(parents=True)
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )

        with pytest.raises(RuntimeError) as excinfo:
            sandbox_mod.assert_voice_runtime_outside_agent_workspace(workspace)

        message = str(excinfo.value)
        assert str(workspace) in message
        assert str(runtime) in message
        assert "lives inside it" in message
        assert (
            "Pick a project subdirectory that does not contain the Kiro Crew data home." in message
        )

    def test_voice_guard_alias_refusal_names_both_paths(self, monkeypatch, tmp_path):
        """Filesystem-identity variant: both absolute paths plus the remedy."""
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        workspace = tmp_path / "workspace"
        runtime.mkdir(parents=True)
        workspace.mkdir()
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        real_stat = sandbox_mod.os.stat
        runtime_info = real_stat(runtime)

        def alias_stat(path, *args, **kwargs):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(str(workspace)):
                return runtime_info
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(sandbox_mod.os, "stat", alias_stat)

        with pytest.raises(RuntimeError) as excinfo:
            sandbox_mod.assert_voice_runtime_outside_agent_workspace(workspace)

        message = str(excinfo.value)
        assert str(workspace) in message
        assert str(runtime) in message
        assert "aliases" in message
        assert (
            "Pick a project subdirectory that does not contain the Kiro Crew data home." in message
        )

    def test_voice_guard_cannot_verify_refusal_names_the_failed_stat(self, monkeypatch, tmp_path):
        """OSError variant: workspace, runtime, the failed path, and the remedy."""
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        workspace = tmp_path / "workspace"
        runtime.mkdir(parents=True)
        workspace.mkdir()
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        real_stat = sandbox_mod.os.stat

        def failing_stat(path, *args, **kwargs):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(str(workspace)):
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(sandbox_mod.os, "stat", failing_stat)

        with pytest.raises(RuntimeError) as excinfo:
            sandbox_mod.assert_voice_runtime_outside_agent_workspace(workspace)

        message = str(excinfo.value)
        assert "cannot verify" in message
        assert str(workspace) in message
        assert str(runtime) in message
        assert "a filesystem check failed on" in message
        assert "Permission denied" in message
        assert "fails closed" in message
        assert (
            "Pick a project subdirectory that does not contain the Kiro Crew data home." in message
        )
        assert isinstance(excinfo.value.__cause__, OSError)

    def test_voice_guard_bind_cannot_verify_refusal_names_the_failed_open(self, monkeypatch):
        """Bind-path OSError variant: workspace, failed path, and the remedy."""
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: ("/protected/voice-runtime",),
        )

        def failing_open(path, **_kwargs):
            raise PermissionError(13, "Permission denied", os.fspath(path))

        monkeypatch.setattr(sandbox_mod, "_open_directory_descriptor", failing_open)

        with pytest.raises(RuntimeError) as excinfo:
            sandbox_mod.bind_voice_safe_agent_workspace("/mutable/workspace")

        message = str(excinfo.value)
        assert "cannot verify" in message
        assert "/mutable/workspace" in message
        assert "/protected/voice-runtime" in message
        assert "a filesystem check failed on" in message
        assert "Permission denied" in message
        assert (
            "Pick a project subdirectory that does not contain the Kiro Crew data home." in message
        )
        assert isinstance(excinfo.value.__cause__, OSError)

    @staticmethod
    def _pin_primed_voice_runtime(monkeypatch, home):
        """Prime a real runtime under *home*, as the gateway does at startup."""
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: home)
        monkeypatch.setattr(sandbox_mod, "_voice_runtime_paths_cache", None)
        return Path(sandbox_mod.prime_voice_runtime_sandbox_paths())

    def test_voice_guard_recreates_a_runtime_root_deleted_after_priming(
        self, monkeypatch, tmp_path
    ):
        """A root removed after the cache filled is re-created, then checked."""
        home = tmp_path / "data"
        home.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        root = self._pin_primed_voice_runtime(monkeypatch, home)
        root.rmdir()
        root.parent.rmdir()

        sandbox_mod.assert_voice_runtime_outside_agent_workspace(workspace)

        assert root.is_dir()
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(root.parent.stat().st_mode) == 0o700

    def test_voice_bind_recreates_a_runtime_root_deleted_after_priming(self, monkeypatch, tmp_path):
        home = tmp_path / "data"
        home.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        root = self._pin_primed_voice_runtime(monkeypatch, home)
        root.rmdir()

        path, descriptor = sandbox_mod.bind_voice_safe_agent_workspace(workspace)
        try:
            assert path == os.fspath(workspace)
            assert descriptor is not None
        finally:
            os.close(descriptor)
        assert root.is_dir()

    def test_voice_bind_still_refuses_a_workspace_above_a_recreated_root(
        self, monkeypatch, tmp_path
    ):
        home = tmp_path / "data"
        home.mkdir()
        root = self._pin_primed_voice_runtime(monkeypatch, home)
        root.rmdir()

        with pytest.raises(RuntimeError, match="protected voice runtime"):
            sandbox_mod.bind_voice_safe_agent_workspace(home)

    def test_voice_guard_fails_closed_when_the_root_cannot_be_recreated(
        self, monkeypatch, tmp_path
    ):
        """A data home that is gone too cannot be re-primed: cannot-verify."""
        home = tmp_path / "data"
        home.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        root = self._pin_primed_voice_runtime(monkeypatch, home)
        root.rmdir()
        root.parent.rmdir()
        home.rmdir()

        with pytest.raises(RuntimeError, match="cannot verify"):
            sandbox_mod.assert_voice_runtime_outside_agent_workspace(workspace)
        with pytest.raises(RuntimeError, match="cannot verify"):
            sandbox_mod.bind_voice_safe_agent_workspace(workspace)

    def test_macos_workspace_binding_uses_opened_ancestor_identities(self, monkeypatch):
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: ("/protected/voice-runtime",),
        )
        opened = iter((41, 42))
        monkeypatch.setattr(
            sandbox_mod,
            "_open_directory_descriptor",
            lambda path, **_kwargs: next(opened),
        )

        def fake_fstat(descriptor):
            identities = {41: (7, 101), 42: (7, 202)}
            dev, inode = identities[descriptor]
            result = MagicMock()
            result.st_dev = dev
            result.st_ino = inode
            return result

        monkeypatch.setattr(sandbox_mod.os, "fstat", fake_fstat)
        monkeypatch.setattr(
            sandbox_mod,
            "_directory_ancestor_identities",
            lambda descriptor: (
                ((7, 101), (7, 11), (7, 1)) if descriptor == 41 else ((7, 202), (7, 22), (7, 1))
            ),
        )
        closed: list[int] = []
        monkeypatch.setattr(sandbox_mod.os, "close", closed.append)

        path, descriptor = sandbox_mod.bind_voice_safe_agent_workspace("/mutable/workspace")

        # The pathname comes back UNCHANGED, with the descriptor beside it. The
        # earlier spelling returned "/dev/fd/41" as the spawn's cwd, which only
        # Linux can chdir -- on macOS, the one platform that binds, every spawn
        # died with EACCES. The descriptor now travels as create_subprocess_limited's
        # ``chdir_fd`` and is entered with fchdir instead.
        assert (path, descriptor) == ("/mutable/workspace", 41)
        assert "/dev/fd" not in path
        assert closed == [42]

    def test_bound_session_target_is_read_off_the_descriptor(self, monkeypatch, tmp_path):
        """A peer that can only take a pathname gets the DESCRIPTOR's own name.

        Handing back the caller's spelling would leave a same-UID retarget between
        this check and the peer's own resolution, which is the window the binding
        exists to close.
        """
        monkeypatch.setattr(sandbox_mod, "_bound_agent_workspace_matches", lambda *_args: True)
        monkeypatch.setattr("kiro_crew.sandbox.fd_real_path", lambda _fd: "/canonical/workspace")

        assert (
            sandbox_mod.bound_agent_workspace_target(41, "/mutable/workspace")
            == "/canonical/workspace"
        )

    def test_bound_session_target_is_none_for_a_workspace_that_is_not_bound(self, monkeypatch):
        monkeypatch.setattr(sandbox_mod, "_bound_agent_workspace_matches", lambda *_args: False)

        assert sandbox_mod.bound_agent_workspace_target(41, "/other/workspace") is None

    def test_bound_session_target_fails_closed_when_the_name_cannot_be_read(self, monkeypatch):
        """No fallback to the mutable pathname: that is the string under attack."""
        monkeypatch.setattr(sandbox_mod, "_bound_agent_workspace_matches", lambda *_args: True)
        monkeypatch.setattr("kiro_crew.sandbox.fd_real_path", lambda _fd: None)

        with pytest.raises(OSError):
            sandbox_mod.bound_agent_workspace_target(41, "/mutable/workspace")

    @pytest.mark.asyncio
    async def test_shared_session_resolver_substitutes_the_descriptor_name(self, monkeypatch):
        """One rule for both ACP front ends, so the two halves cannot drift."""
        monkeypatch.setattr(
            sandbox_mod, "bound_agent_workspace_target", lambda *_args: "/canonical/workspace"
        )

        resolved = await sandbox_mod.resolve_bound_session_workspace(41, "/mutable/workspace")

        assert resolved == "/canonical/workspace"

    @pytest.mark.asyncio
    async def test_shared_session_resolver_raises_on_a_workspace_that_is_not_bound(
        self, monkeypatch
    ):
        """A distinct error, so each caller maps it to its own type without restating it."""
        monkeypatch.setattr(sandbox_mod, "bound_agent_workspace_target", lambda *_args: None)

        with pytest.raises(sandbox_mod.BoundWorkspaceMismatch):
            await sandbox_mod.resolve_bound_session_workspace(41, "/other/workspace")

    @pytest.mark.asyncio
    async def test_shared_session_resolver_runs_off_the_event_loop(self, monkeypatch):
        """It opens a directory and reads a descriptor's name on every session start."""
        loop_thread = threading.get_ident()
        ran_on: list[int] = []

        def record(_descriptor, _workspace):
            ran_on.append(threading.get_ident())
            return "/canonical/workspace"

        monkeypatch.setattr(sandbox_mod, "bound_agent_workspace_target", record)

        await sandbox_mod.resolve_bound_session_workspace(41, "/mutable/workspace")

        assert ran_on and ran_on[0] != loop_thread

    def test_macos_workspace_binding_rejects_opened_runtime_ancestor(self, monkeypatch):
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: ("/protected/voice-runtime",),
        )
        opened = iter((51, 52))
        monkeypatch.setattr(
            sandbox_mod,
            "_open_directory_descriptor",
            lambda path, **_kwargs: next(opened),
        )

        def fake_fstat(descriptor):
            identities = {51: (8, 301), 52: (8, 302)}
            dev, inode = identities[descriptor]
            result = MagicMock()
            result.st_dev = dev
            result.st_ino = inode
            return result

        monkeypatch.setattr(sandbox_mod.os, "fstat", fake_fstat)
        monkeypatch.setattr(
            sandbox_mod,
            "_directory_ancestor_identities",
            lambda descriptor: (
                ((8, 301), (8, 302), (8, 1)) if descriptor == 51 else ((8, 302), (8, 1))
            ),
        )
        closed: list[int] = []
        monkeypatch.setattr(sandbox_mod.os, "close", closed.append)

        with pytest.raises(RuntimeError, match="protected voice runtime") as excinfo:
            sandbox_mod.bind_voice_safe_agent_workspace("/mutable/workspace")

        message = str(excinfo.value)
        assert "/mutable/workspace" in message
        assert "/protected/voice-runtime" in message
        assert (
            "Pick a project subdirectory that does not contain the Kiro Crew data home." in message
        )
        assert closed == [51, 52]

    @pytest.mark.asyncio
    async def test_cancelled_async_workspace_binding_closes_returned_descriptor(self, monkeypatch):
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        loop_thread = threading.get_ident()
        close_threads: list[int] = []

        def delayed_binding(_workspace):
            entered.set()
            # flake-ok: the test sets release right after its cancel; the 2 s bound only names a test that never got there
            assert release.wait(timeout=2)
            return "/dev/fd/61", 61

        def record_close(descriptor):
            assert descriptor == 61
            close_threads.append(threading.get_ident())
            closed.set()

        monkeypatch.setattr(sandbox_mod, "bind_voice_safe_agent_workspace", delayed_binding)
        monkeypatch.setattr(sandbox_mod, "_close_bound_agent_workspace", record_close)

        task = asyncio.create_task(
            sandbox_mod.bind_voice_safe_agent_workspace_async("/mutable/workspace")
        )
        # flake-ok: the worker sets entered as its first statement; the 2 s bound only names a worker that never ran
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            # flake-ok: the task is cancelled and its worker released just above; the await only collects its CancelledError
            await task
        assert closed.is_set()
        assert len(close_threads) == 1
        assert close_threads[0] != loop_thread

    @pytest.mark.asyncio
    async def test_release_bound_workspace_closes_off_event_loop(self, monkeypatch):
        loop_thread = threading.get_ident()
        close_threads: list[int] = []
        monkeypatch.setattr(
            sandbox_mod,
            "_close_bound_agent_workspace",
            lambda _descriptor: close_threads.append(threading.get_ident()),
        )

        await sandbox_mod.release_bound_agent_workspace(62)

        assert close_threads and close_threads[0] != loop_thread

    def test_standard_does_not_deny_aws(self):
        profile = _build_seatbelt_profile("standard")
        home = str(Path.home())
        # Standard mode doesn't hide .aws
        assert f'(subpath "{home}/.aws")' not in profile

    def test_cc_mode_skips_aws_on_macos(self):
        profile = _build_seatbelt_profile("cc")
        home = str(Path.home())
        # CC mode on macOS doesn't hide .aws (credential_process needs it)
        assert f'(subpath "{home}/.aws")' not in profile

    def test_cc_mode_denies_individual_files(self):
        profile = _build_seatbelt_profile("cc")
        home = str(Path.home())
        for f in _CC_FILES:
            assert os.path.join(home, f) in profile

    def test_cc_mode_skips_aws_dir(self):
        """CC mode does NOT deny .aws as a directory (credential_process needs it)."""
        profile = _build_seatbelt_profile("cc")
        home = str(Path.home())
        # .aws should not appear as a subpath deny
        assert f'(subpath "{home}/.aws")' not in profile

    # ── hardlink bypass ──
    def test_strict_denies_hardlink_creation_to_dirs(self):
        """Each read-denied dir must ALSO deny file-link (hardlink) creation, so a
        sandboxed agent cannot mint a hardlink at a non-denied path (/tmp) that
        reads the same inode past the path-based file-read* deny."""
        profile = _build_seatbelt_profile("strict")
        home = str(Path.home())
        for d in _STRICT_DIRS:
            assert f'(deny file-link (subpath "{os.path.join(home, d)}"))' in profile

    def test_strict_denies_hardlink_to_individual_files(self):
        profile = _build_seatbelt_profile("strict")
        home = str(Path.home())
        for f in _CC_FILES:
            assert f'(deny file-link (literal "{os.path.join(home, f)}"))' in profile

    def test_strict_denies_hardlink_to_ssh(self):
        profile = _build_seatbelt_profile("strict")
        home = str(Path.home())
        assert f'(deny file-link (subpath "{os.path.join(home, ".ssh")}"))' in profile

    def test_cc_mode_denies_hardlink_to_files(self):
        profile = _build_seatbelt_profile("cc")
        home = str(Path.home())
        for f in _CC_FILES:
            assert f'(deny file-link (literal "{os.path.join(home, f)}"))' in profile

    def test_uses_valid_file_link_token_not_star(self):
        """``file-link*`` is NOT a valid SBPL token (unbound variable); the rule
        must use the bare ``file-link`` operation."""
        profile = _build_seatbelt_profile("strict")
        assert "(deny file-link " in profile
        assert "file-link*" not in profile


class TestDirectoryAncestorIdentities:
    """The descriptor-walking ancestry probe must not leak file descriptors.

    Each ``..`` descriptor it opens is unowned until the ``current = parent``
    handoff. A fault in the window before that handoff (an ``os.fstat`` on the
    freshly opened parent) must still close the parent, or its descriptor leaks
    for the process lifetime.
    """

    def test_parent_descriptor_closed_when_fstat_raises(self, monkeypatch, tmp_path):
        """An ``os.fstat`` fault on a freshly opened parent closes that parent."""
        start_fd = sandbox_mod._open_directory_descriptor(tmp_path)
        try:
            opened: list[int] = []
            closed: list[int] = []

            real_open = sandbox_mod._open_directory_descriptor

            def tracking_open(path, **kwargs):
                fd = real_open(path, **kwargs)
                opened.append(fd)
                return fd

            real_fstat = os.fstat

            def exploding_fstat(fd):
                # Let the walk read its starting descriptor normally, then fail
                # on the first parent it opens -- the leak window under test.
                if opened and fd == opened[-1]:
                    raise OSError(errno.EIO, "injected fstat failure")
                return real_fstat(fd)

            real_close = os.close

            def tracking_close(fd):
                closed.append(fd)
                return real_close(fd)

            # Patched through the facade: it fans the name out to every owner
            # module that binds it (sandbox_workspace and sandbox_rlimit).
            monkeypatch.setattr(sandbox_mod, "_open_directory_descriptor", tracking_open)
            monkeypatch.setattr(sandbox_mod.os, "fstat", exploding_fstat)
            monkeypatch.setattr(sandbox_mod.os, "close", tracking_close)

            with pytest.raises(OSError):
                sandbox_mod._directory_ancestor_identities(start_fd)

            # The one parent descriptor that was opened must have been closed,
            # even though fstat raised before the ownership handoff.
            assert opened, "the walk never opened a parent descriptor"
            assert set(opened).issubset(
                set(closed)
            ), f"leaked descriptor(s): opened={opened} closed={closed}"
        finally:
            os.close(start_fd)
