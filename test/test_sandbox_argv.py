"""Additional tests for kiro_crew.sandbox — wrap_argv, profiles, env scrubbing."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sandbox as sandbox_mod
from kiro_crew.sandbox import (
    _CC_FILES,
    _SENSITIVE_ENV_PREFIXES,
    _STRICT_DIRS,
    _build_launcher_script,
    _build_seatbelt_profile,
    _resolve_agent_executable,
    _ssh_supports_accept_new,
    detect_backend,
    namespace_argv,
    reset_backend,
    sandbox_exec_argv,
    wrap_argv,
)

# Several tests spawn real child interpreters (subprocess.run([sys.executable, ...]));
# pin the module to a dedicated xdist worker so concurrent cold-starts under -n auto
# don't starve each other / blow the 30s timeout. Requires --dist loadgroup.
pytestmark = pytest.mark.xdist_group(name="subprocess_spawn")

# ``_build_launcher_script`` calls POSIX-only ``os.getuid``/``os.getgid`` (the
# namespace launcher is Linux-only), so any test that builds the launcher script
# raises AttributeError on Windows. Skip those on win32 -- the reduced-scope
# Windows CI lane runs them, but they pass in the full POSIX suite. See #2041.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="_build_launcher_script uses POSIX-only os.getuid (#2041)",
)


@pytest.fixture()
def systemd_run_resolvable(monkeypatch):
    """Make ``trusted_system_bin("systemd-run")`` resolve on a host without systemd.

    The cgroup-scope tests below mock ``_probe_cgroup_scope`` to "available" and
    assert the argv ``cgroup_scope_argv`` BUILDS. That argv is only built when
    the wrapper also resolves from a trusted system directory, and on macOS /
    a container without systemd it never does -- so the code degraded (no
    ceiling, loud warning), and seven tests about argv SHAPE failed for a reason
    that has nothing to do with argv shape. Only ``systemd-run`` is faked; every
    other name still goes through the real resolver, so the tests that assert
    degradation when it is ABSENT (they patch the resolver to ``None``
    themselves, inside their own ``with``) are unaffected -- an inner patch
    wins and reverts to this one.
    """
    from kiro_crew import platform_compat

    real = platform_compat.trusted_system_bin

    def _resolve(name: str) -> str | None:
        if name == "systemd-run":
            return "/usr/bin/systemd-run"
        return real(name)

    monkeypatch.setattr(sandbox_mod.platform_compat, "trusted_system_bin", _resolve)


@pytest.fixture(autouse=True)
def clean_backend(monkeypatch):
    """Reset cached backend between tests.

    Also neutralize the host's real kiro internal-sandbox setting: on a macOS
    dev box where ``~/.kiro/settings/amazon-internal.json`` has
    ``{"sandbox": true}``, the darwin kiro-delegation branch in ``wrap_argv``
    preempts the mocked ``detect_backend`` and these unit tests — which exercise
    KiroCrew's OWN backend selection / fail-closed path — never reach the code
    they assert on. Point the settings path at a non-existent file so delegation
    is off by default; the dedicated delegation tests set
    ``_KIRO_INTERNAL_SETTINGS_PATH`` explicitly and are unaffected.

    Clears ``KIROCREW_SANDBOX_ACTIVE`` to prevent the "already inside sandbox"
    passthrough from short-circuiting tests on hosts (like Cloud Desktops) where
    the gateway process itself runs sandboxed. Tests that exercise the
    passthrough set the env var explicitly.
    """
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.setattr(
        "kiro_crew.sandbox._KIRO_INTERNAL_SETTINGS_PATH",
        "/nonexistent/kirocrew-test/amazon-internal.json",
    )
    # Reset one-shot warning flags
    if hasattr(sandbox_mod.wrap_argv, "_warned"):
        delattr(sandbox_mod.wrap_argv, "_warned")
    if hasattr(sandbox_mod._warn_mode_off_unconfined, "_warned_set"):
        delattr(sandbox_mod._warn_mode_off_unconfined, "_warned_set")
    if hasattr(sandbox_mod._warn_mode_off_unconfined, "_info_logged"):
        delattr(sandbox_mod._warn_mode_off_unconfined, "_info_logged")
    reset_backend()
    yield
    reset_backend()


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
        detect_backend(config_mode="auto")
        detect_backend(config_mode="auto")
        # Only probed once due to caching
        assert mock_ns.call_count == 1

    @patch("kiro_crew.sandbox._probe_unshare", return_value=True)
    def test_invalidates_on_mode_change(self, mock_ns):
        detect_backend(config_mode="auto")
        detect_backend(config_mode="off")
        # Second call with different mode should re-evaluate
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
        mock_ns_argv.assert_called_once_with(["kiro-cli"], "strict", strip_python_env=False)

    @patch("kiro_crew.sandbox.detect_backend", return_value="sandbox-exec")
    @patch("kiro_crew.sandbox.sandbox_exec_argv")
    def test_sandbox_exec_backend(self, mock_sb_argv, mock_detect):
        mock_sb_argv.return_value = (["sandbox-exec", "-f", "/tmp/p.sb", "kiro-cli"], "/tmp/p.sb")
        result, cleanup = wrap_argv(["kiro-cli"], mode="strict")
        mock_sb_argv.assert_called_once_with(["kiro-cli"], "strict", strip_python_env=False)

    @patch("kiro_crew.sandbox.detect_backend")
    def test_inside_sandbox_passes_through(self, mock_detect, monkeypatch):
        # Inside an existing KiroCrew sandbox, nested unshare is seccomp-denied,
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

    @patch("kiro_crew.sandbox.detect_backend", return_value="none")
    def test_outside_sandbox_does_not_pass_through(self, mock_detect, monkeypatch):
        # No marker set → normal wrap path (here: no backend), proving the
        # passthrough is gated strictly on the in-sandbox marker.
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        with patch("kiro_crew.sandbox._allow_unsandboxed_exec", return_value=True):
            result, cleanup = wrap_argv(["kiro-cli"], mode="strict")
        mock_detect.assert_called_once()


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

    def test_voice_runtime_cannot_be_reexposed_or_missed_by_relocation(
        self, monkeypatch, tmp_path
    ):
        custom_home = tmp_path / "custom-home"
        custom_home.mkdir()
        relocated = custom_home / "run" / "voice-runtime"
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: custom_home)

        profile = _build_seatbelt_profile(
            "standard", extra_visible_dirs=(str(relocated),)
        )

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
        guards = sandbox_mod._literal_ancestor_guards(
            (str(lexical_run), str(canonical_run))
        )
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
        """#7392: the non-raising pre-flight mirrors the lexical guard and
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
        # Same remedy sentence as the spawn-time refusal (#7407's shared formatter).
        assert "Pick a project subdirectory" in contains

        inside = sandbox_mod.voice_runtime_workspace_conflict(runtime / "nested")
        assert inside is not None and "inside it" in inside

        assert sandbox_mod.voice_runtime_workspace_conflict(sibling) is None

    def test_voice_runtime_workspace_conflict_passes_off_darwin(self, monkeypatch, tmp_path):
        """The pre-flight matches the guards it mirrors: every spawn-time
        guard early-returns off macOS, so an overlapping workspace spawns
        fine on Linux/Windows today — the pre-flight must not 400 a working
        configuration there (Design/FP review round 1)."""
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
        """Drift pin (Design/FP review round 2): the pre-flight and the
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
            lambda path: runtime_info
            if os.path.abspath(os.fspath(path)) == os.path.abspath(str(workspace))
            else real_stat(path),
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
            "Pick a project subdirectory that does not contain the Kiro Crew data home."
            in message
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
            "Pick a project subdirectory that does not contain the Kiro Crew data home."
            in message
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
            "Pick a project subdirectory that does not contain the Kiro Crew data home."
            in message
        )

    def test_voice_guard_cannot_verify_refusal_names_the_failed_stat(
        self, monkeypatch, tmp_path
    ):
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
            "Pick a project subdirectory that does not contain the Kiro Crew data home."
            in message
        )
        assert isinstance(excinfo.value.__cause__, OSError)

    def test_voice_guard_bind_cannot_verify_refusal_names_the_failed_open(
        self, monkeypatch
    ):
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
            "Pick a project subdirectory that does not contain the Kiro Crew data home."
            in message
        )
        assert isinstance(excinfo.value.__cause__, OSError)

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
                ((7, 101), (7, 11), (7, 1))
                if descriptor == 41
                else ((7, 202), (7, 22), (7, 1))
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
        monkeypatch.setattr(
            sandbox_mod, "_bound_agent_workspace_matches", lambda *_args: True
        )
        monkeypatch.setattr(
            "kiro_crew.sandbox.fd_real_path", lambda _fd: "/canonical/workspace"
        )

        assert (
            sandbox_mod.bound_agent_workspace_target(41, "/mutable/workspace")
            == "/canonical/workspace"
        )

    def test_bound_session_target_is_none_for_a_workspace_that_is_not_bound(self, monkeypatch):
        monkeypatch.setattr(
            sandbox_mod, "_bound_agent_workspace_matches", lambda *_args: False
        )

        assert sandbox_mod.bound_agent_workspace_target(41, "/other/workspace") is None

    def test_bound_session_target_fails_closed_when_the_name_cannot_be_read(self, monkeypatch):
        """No fallback to the mutable pathname: that is the string under attack."""
        monkeypatch.setattr(
            sandbox_mod, "_bound_agent_workspace_matches", lambda *_args: True
        )
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
                ((8, 301), (8, 302), (8, 1))
                if descriptor == 51
                else ((8, 302), (8, 1))
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
            "Pick a project subdirectory that does not contain the Kiro Crew data home."
            in message
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
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
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


class TestBuildLauncherScript:
    @_POSIX_ONLY
    def test_strict_script_contains_dirs(self):
        script = _build_launcher_script("strict")
        assert "SENSITIVE_DIRS" in script
        assert ".aws" in script
        assert ".gnupg" in script

    @_POSIX_ONLY
    def test_strict_script_denies_namespace_escape_not_hardlinks(self):
        """Linux seccomp deny list must contain the namespace-escape syscalls
        (mount/umount2/unshare/setns/pivot_root) and must NOT contain
        link/linkat -- hardlink containment is the bind-mask's job, and a
        blanket link ban broke hardlink-using build tools (npm cacache). Guards
        against an accidental re-add of link/linkat or drop of an escape
        syscall (pentest finding #9 remediation)."""
        script = _build_launcher_script("strict")
        # x86_64: mount=165 umount2=166 unshare=272 setns=308 pivot_root=155
        assert "_DENY_SYSCALLS = (165, 166, 272, 308, 155)" in script
        # aarch64: mount=40 umount2=39 unshare=97 setns=268 pivot_root=41
        assert "_DENY_SYSCALLS = (40, 39, 97, 268, 41)" in script
        # link=86/linkat=265 (x86_64) and linkat=37 (aarch64) must be gone
        assert "308, 155, 86, 265)" not in script
        assert "268, 41, 37)" not in script

    @_POSIX_ONLY
    def test_launcher_refuses_when_seccomp_cannot_be_installed(self):
        """An arch with no syscall table, or a libc without prctl(2), must make
        the launcher EXIT -- not skip Step 5/6 and exec the agent anyway.

        Skipping leaves ``unshare`` permitted, so the child can enter a nested
        user namespace, hold CAP_SYS_ADMIN over a copy of this mount tree, and
        umount every credential mask -- the escape the filter exists to deny.
        Both ``_inside_kirocrew_sandbox`` and
        ``docs/system-specs/modules/security.md`` state that a sandboxed tree is
        confined "by the outer namespace + seccomp", so a silent skip makes that
        claim false while every caller still reads the spawn as isolated.
        """
        script = _build_launcher_script("strict")
        # The generated launcher must stay valid Python at every tier.
        for level in ("standard", "cc", "strict"):
            compile(_build_launcher_script(level), "<launcher-%s>" % level, "exec")
        assert "no seccomp syscall table for machine" in script
        assert "libc exposes no prctl(2)" in script
        # The old fail-open marker must not come back.
        assert "unknown arch" not in script

        # Execute the arch-dispatch block itself, so this proves the refusal
        # FIRES rather than that its message is present as text. The block
        # starts at the machine read: ``import platform`` no longer sits here —
        # it is hoisted to the preamble so no first-time stdlib import runs
        # after namespace/mount isolation (#8151).
        lines = script.splitlines()
        start = -1
        end = -1
        for index, line in enumerate(lines):
            if start < 0 and line.strip() == "_machine = _plat.machine()":
                start = index
            elif start >= 0 and "if _DENY_SYSCALLS:" in line:
                end = index
                break
        assert start >= 0 and end > start, "arch-dispatch block not found"
        block = textwrap.dedent("\n".join(lines[start:end]))

        class _FakePlat:
            def __init__(self, machine):
                self._machine = machine

            def machine(self):
                return self._machine

        supported = (
            ("x86_64", (165, 166, 272, 308, 155)),
            ("aarch64", (40, 39, 97, 268, 41)),
        )
        for machine, table in supported:
            namespace = {"sys": sys, "_plat": _FakePlat(machine)}
            exec(block, namespace)  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected -- runs this repo's OWN generated launcher source, never external input  # noqa: E501  # fmt: skip
            assert namespace["_DENY_SYSCALLS"] == table

        for machine in ("riscv64", "armv7l", "ppc64le", "s390x"):
            namespace = {"sys": sys, "_plat": _FakePlat(machine)}
            with pytest.raises(SystemExit) as excinfo:
                exec(block, namespace)  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected -- runs this repo's OWN generated launcher source, never external input  # noqa: E501  # fmt: skip
            message = str(excinfo.value)
            assert "sandbox: BLOCKED" in message
            assert repr(machine) in message
            # The refusal must name the explicit opt-out, or an operator on such
            # a host is left with no way forward.
            assert "agent.sandbox_allow_unsandboxed_exec" in message

    @_POSIX_ONLY
    def test_standard_script_excludes_aws(self):
        script = _build_launcher_script("standard")
        # Standard dirs don't include .aws
        assert "HIDE_SSH = False" in script

    @_POSIX_ONLY
    def test_auth_staging_is_hidden_except_for_trusted_auth_spawn(self):
        home = Path.home()
        staging = home / ".kiro" / "crew-auth-staging"
        workspace = staging / "auth-123"
        data_home = home / ".kiro" / "crew"

        regular_script = _build_launcher_script("standard")
        auth_script = _build_launcher_script(
            "standard",
            extra_hidden_dirs=(str(data_home),),
            extra_visible_dirs=(str(workspace),),
        )
        regular_profile = _build_seatbelt_profile("standard")
        auth_profile = _build_seatbelt_profile(
            "standard",
            extra_hidden_dirs=(str(data_home),),
            extra_visible_dirs=(str(workspace),),
        )

        assert str(staging) in regular_script
        assert str(staging) in regular_profile
        assert str(staging) not in auth_script
        assert str(staging) not in auth_profile
        assert str(data_home) in auth_script
        assert str(data_home) in auth_profile

    @_POSIX_ONLY
    def test_a_file_valued_hidden_path_reaches_the_file_loop(self, tmp_path):
        """A hidden path that is a FILE must reach ``SENSITIVE_FILES``.

        The two launcher loops hide each kind differently — a directory gets an empty
        dir bind-mounted over it, a file gets an empty temp file — and the dir loop is
        guarded by ``if os.path.isdir(target)``. So a file entry matched neither it nor
        the file loop and was SILENTLY SKIPPED: the caller asked for it to be hidden,
        got no error, and the file stayed readable.

        Not hypothetical: ``security.sensitive_home_dirs()`` is not all directories
        (``sel_hmac.key``, ``token_signing.key``, ``.kiro/crew/.env`` are files), and
        Papyrus passes that whole list as ``extra_hidden_dirs`` so a ``.tex`` cannot
        ``\\input`` the gateway's own secrets into a rendered PDF.

        Every path goes in BOTH lists and the CHILD classifies it — see the next test
        for why that, rather than deciding here.
        """
        secret = tmp_path / "token_signing.key"
        secret.write_text("s3cret", encoding="utf-8")
        real_dir = tmp_path / "creds"
        real_dir.mkdir()

        script = _build_launcher_script(
            "strict", extra_hidden_dirs=(str(secret), str(real_dir))
        )
        dirs = json.loads(re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S).group(1))
        files = json.loads(re.search(r"SENSITIVE_FILES = (\[.*?\])\n", script, re.S).group(1))

        # The file reaches the loop that can actually hide it.
        assert str(secret) in files, "a file-valued hidden path cannot be hidden"
        # And the directory still reaches its own loop.
        assert str(real_dir) in dirs

    def test_the_builder_does_not_stat_the_hidden_paths(self):
        """No filesystem probe in ``_build_launcher_script`` — it runs ON THE LOOP.

        An earlier version of this fix classified each path here with
        ``os.path.isfile()``. That is 52 stats per async spawn on the gateway's single
        loop, and on a stalled NFS home each one blocks — freezing every session, cron
        and the liveness heartbeat. The child already re-checks with its own
        ``isdir``/``isfile`` per loop, so whichever branch matches does the work and the
        other skips; letting it decide keeps the syscalls where they were already
        happening and where blocking costs only that one spawn.

        An AST check rather than a mock, because the point is that no such call exists
        at all.
        """
        import ast
        import inspect

        from kiro_crew import sandbox

        tree = ast.parse(inspect.getsource(sandbox._build_launcher_script))
        probes = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"isfile", "isdir", "exists", "stat", "lstat"}
        ]
        assert probes == [], (
            f"_build_launcher_script stats the filesystem on the event loop: {probes}"
        )

    @_POSIX_ONLY
    def test_every_sensitive_path_reaches_a_loop_that_can_hide_it(self):
        """Whole-list check against the real sensitive-path list.

        Both loops self-guard, so a path present in both is hidden by whichever branch
        matches its actual type — and a future entry that happens to be a file cannot
        silently stop being hidden.
        """
        import os

        from kiro_crew import security

        home = os.path.expanduser("~")
        extra = tuple(os.path.join(home, rel) for rel in security.sensitive_home_dirs())
        script = _build_launcher_script("strict", extra_hidden_dirs=extra)
        dirs = json.loads(re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S).group(1))
        files = json.loads(re.search(r"SENSITIVE_FILES = (\[.*?\])\n", script, re.S).group(1))

        for path in extra:
            assert path in dirs, f"{path} never reaches the directory loop"
            assert path in files, f"{path} never reaches the file loop"

    @_POSIX_ONLY
    def test_cc_script_exposes_aws_config(self):
        script = _build_launcher_script("cc")
        assert ".aws/config" in script
        assert "EXPOSE_FILES" in script

    @_POSIX_ONLY
    def test_script_scrubs_env_vars(self):
        script = _build_launcher_script("strict")
        for prefix in _SENSITIVE_ENV_PREFIXES:
            assert prefix in script

    @_POSIX_ONLY
    def test_strips_self_dir_before_ctypes_import(self):
        """The sys.path hardening must run before the first shadowable import.

        Regression guard for the /tmp/struct.py shadowing outage: ctypes does
        ``from struct import calcsize`` at import time, so the launcher dir must
        be removed from sys.path *before* ``import ctypes``.
        """
        script = _build_launcher_script("strict")
        assert "sys.path[:]" in script
        assert script.index("sys.path[:]") < script.index("import ctypes")
        # sys must be imported first (it is a builtin and cannot be shadowed).
        assert script.index("import sys") < script.index("sys.path[:]")

    @_POSIX_ONLY
    def test_launcher_has_no_unimportable_kiro_crew_refs(self):
        """The launcher runs as a standalone ~/.kirocrew/run script with the
        launcher dir scrubbed from sys.path, so it CANNOT import kiro_crew.
        Referencing a module-level helper like ``platform_compat`` NameErrors at
        runtime and crashed every command cron. Guard: chmod is inlined, the
        script stays syntactically valid, and there is no module-qualified
        RUNTIME reference to any host-only module the isolated launcher can't
        import.

        The naive ``"platform_compat" not in script`` string check that upstream
        also carries is DELETED here: the fork's launcher COMMENT intentionally
        names platform_compat (explaining why the inline os.chmod must NOT use
        it), so a substring check false-positives. The AST guard below proves
        there is no runtime module-qualified reference, which is the correct
        behavioral check.
        """
        for level in ("strict", "standard", "cc"):
            script = _build_launcher_script(level)
            assert "os.chmod(dest, 0o444)" in script, f"{level}: inline chmod missing"
            compile(script, "<launcher>", "exec")
            # AST-based so mentions in comments/strings (e.g. the fork's own
            # explanatory comment naming platform_compat/kiro_crew) don't
            # false-positive — only module-qualified attribute access counts.
            used_modules = {
                node.value.id
                for node in ast.walk(ast.parse(script))
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            }
            forbidden = used_modules & {"platform_compat", "kiro_crew", "logger", "logging"}
            assert (
                not forbidden
            ), f"{level}: launcher references un-importable module(s) {forbidden}"


@_POSIX_ONLY
class TestHardlinkScanBudget:
    """Step-7 pre-exec hardlink scan: per-root budgets + loud truncation.

    The launcher needs ``unshare`` so it cannot run end-to-end in CI; these
    are text/compile assertions on the generated script, the same pattern as
    the other launcher-script tests above. A single budget shared across the
    CWD and /tmp walks let a large worktree consume the whole budget before
    /tmp (the world-writable root the check exists for) was scanned at all,
    and an exhausted budget fell through to exec silently — a truncated scan
    was indistinguishable from a clean one.
    """

    def test_shared_budget_counter_is_gone(self):
        script = _build_launcher_script("strict")
        assert "_scan_count > _MAX_SCAN" not in script
        assert "_scan_count" not in script

    def test_only_aliased_credential_inodes_arm_the_walk(self):
        # An inode with st_nlink == 1 has no alias anywhere on the
        # filesystem, so it must not enter the match set: when every
        # credential has nlink == 1 the CWD + /tmp walk is skipped and the
        # common healthy-host spawn pays nothing (and emits no truncation
        # warning). Both collection loops (SENSITIVE_DIRS and
        # SENSITIVE_FILES) carry the gate.
        #
        # REGULAR FILES only, and that half is not cosmetic: every directory has
        # nlink >= 2, and SENSITIVE_FILES carries directories on purpose, so a bare
        # nlink test armed the walk on every spawn. Behaviour is covered in
        # test_sandbox_hardlink_scan.py; this is the source-level pin that both
        # collection loops still carry the gate.
        script = _build_launcher_script("strict")
        assert script.count("if stat.S_ISREG(_st.st_mode) and _st.st_nlink > 1:") == 2

    def test_per_root_budget_covers_a_busy_tmp(self):
        # The budget only applies once a credential inode is actually
        # aliased (see the nlink gate above), so it can afford to be
        # generous: 100k covers the busiest observed /tmp (~11.8k files)
        # with an order of magnitude to spare, making truncation genuinely
        # exceptional rather than a steady-state warning.
        script = _build_launcher_script("strict")
        assert "_MAX_SCAN_PER_ROOT = 100000" in script

    def test_per_root_budget_with_counter_reset_inside_root_loop(self):
        script = _build_launcher_script("strict")
        assert "_MAX_SCAN_PER_ROOT" in script
        # The counter reset must be a DIRECT child of the per-root loop body:
        # each root gets exactly one fresh budget, so a large CWD cannot
        # starve the /tmp scan. AST-based, because a byte-offset check cannot
        # tell this apart from a reset nested inside the os.walk loop (which
        # would reset per-directory and make the scan effectively unbounded).
        root_loops = [
            node
            for node in ast.walk(ast.parse(script))
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_scan_root"
        ]
        assert len(root_loops) == 1
        direct_assigns = [
            stmt
            for stmt in root_loops[0].body
            if isinstance(stmt, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_root_scanned"
                for t in stmt.targets
            )
        ]
        assert len(direct_assigns) == 1, (
            "_root_scanned reset must sit directly in the per-root loop body"
        )

    def test_truncation_warns_on_stderr_without_exiting(self):
        script = _build_launcher_script("strict")
        assert "hardlink scan truncated" in script
        assert "scan incomplete" in script
        # The diagnostic goes to stderr, which the parent already captures.
        warn_idx = script.index("hardlink scan truncated")
        stderr_idx = script.index("file=sys.stderr", warn_idx)
        # Deliberate fail-open: the truncation path warns, it never exits.
        assert "sys.exit" not in script[warn_idx:stderr_idx]

    def test_blocked_exit_path_for_found_hardlinks_still_present(self):
        script = _build_launcher_script("strict")
        assert "sandbox: BLOCKED — found hardlink" in script

    def test_no_directory_pruning_in_the_scan(self):
        # /tmp is world-writable and the sandboxed agent shares the uid, so
        # any name- or prefix-based prune list is a deterministic bypass: the
        # attacker just names their directory to match. The scan must visit
        # every directory the depth limit allows; noisy trees are handled by
        # the per-root budget + truncation warning, never by skipping.
        script = _build_launcher_script("strict")
        assert "_SKIP_TMP_DIR_PREFIXES" not in script

    def test_generated_script_compiles_at_every_level(self):
        # Proves the f-string brace escaping in the template produced
        # syntactically valid Python for every sandbox level.
        for level in ("strict", "standard", "cc"):
            compile(_build_launcher_script(level), "<launcher>", "exec")


@_POSIX_ONLY
class TestLauncherStdlibShadowing:
    """End-to-end: a sibling /tmp/struct.py must NOT crash the launcher.

    Hermetic — every poison file lives in pytest's isolated tmp_path subdir,
    never bare /tmp, so the running gateway's launcher (sys.path[0] == /tmp) is
    never affected by these tests.
    """

    # A drop-in stdlib name that ctypes -> struct.calcsize depends on.
    _POISON = "def calcsize(*a, **k):\n    raise RuntimeError('shadowed!')\n"

    def _run_launcher(self, script_dir: Path) -> subprocess.CompletedProcess:
        """Write the launcher into script_dir and run it with no args.

        With no command argv the launcher exits immediately after its imports
        and the ``if not argv`` guard — it never forks/unshares/execs. So this
        exercises exactly the import path that the outage crashed on, and
        nothing else.
        """
        launcher = script_dir / "launcher.py"
        launcher.write_text(_build_launcher_script("standard"))
        return subprocess.run(
            [sys.executable, str(launcher)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_prelude_removes_script_dir_from_syspath(self, tmp_path):
        """Deterministic proof of the mechanism, independent of struct caching.

        Runs the launcher's real generated prelude (everything up to the first
        ``import ctypes``) from a tmp dir, then dumps sys.path. The script's own
        directory — which CPython puts at sys.path[0] — must be gone afterwards.
        Unlike the struct e2e below, this does not depend on whether the
        interpreter pre-imports ``struct``, so it always discriminates the fix.
        """
        script = _build_launcher_script("standard")
        prelude = script[: script.index("import ctypes")]
        probe = tmp_path / "launcher.py"
        probe.write_text(prelude + "import json\nprint(json.dumps(sys.path))\n")
        result = subprocess.run(
            [sys.executable, str(probe)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        import json

        paths = json.loads(result.stdout.strip().splitlines()[-1])
        assert str(tmp_path) not in paths, f"script dir not stripped: {paths}"
        assert "" not in paths, f"cwd entry not stripped: {paths}"

    def test_launcher_survives_sibling_struct_py(self, tmp_path):
        """With the fix, a sibling struct.py is ignored and imports succeed."""
        (tmp_path / "struct.py").write_text(self._POISON)
        result = self._run_launcher(tmp_path)
        # No-args launcher exits via sys.exit("...: no command given") AFTER all
        # imports succeed — so a clean "no command given" proves imports passed.
        assert "calcsize" not in result.stderr, result.stderr
        # The launcher binds Linux-only libc symbols (unshare) at module import
        # time; on non-Linux hosts it dies there, AFTER the shadowable stdlib
        # imports the fix guards, but BEFORE the argv guard. That still proves
        # the imports survived the poison; only the argv guard is unreachable.
        if "unshare" in result.stderr and "no command given" not in result.stderr:
            pytest.skip("launcher needs Linux-only libc unshare; not this host")
        assert (
            "no command given" in result.stderr
        ), f"launcher did not reach the argv guard; stderr={result.stderr!r}"

    def test_control_unstripped_launcher_would_crash(self, tmp_path):
        """Sanity: prove the poison is real — an un-hardened launcher DOES crash.

        Strips the hardening line so we don't silently ship a test that passes
        for the wrong reason. The poison only bites if the interpreter imports
        ``struct`` fresh (not already cached at startup); if a given build
        interpreter pre-caches ``struct``, the shadowing can't be demonstrated
        here, so we skip rather than red the build for an unrelated reason.
        """
        (tmp_path / "struct.py").write_text(self._POISON)
        hardened = _build_launcher_script("standard")
        unstripped = "\n".join(ln for ln in hardened.splitlines() if "sys.path[:]" not in ln)
        launcher = tmp_path / "launcher.py"
        launcher.write_text(unstripped)
        result = subprocess.run(
            [sys.executable, str(launcher)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if "no command given" in result.stderr:
            pytest.skip(
                "interpreter pre-caches 'struct'; sibling shadowing not "
                "reproducible here — positive test still guards the fix"
            )
        # Otherwise the shadowed struct broke the ctypes import -> launcher
        # died before reaching the argv guard, proving the poison is real.
        if ("calcsize" not in result.stderr) and ("shadowed!" not in result.stderr):
            preview = repr(result.stderr)[:120]
            pytest.skip(
                "struct shadowing not observable on this interpreter "
                f"(stderr={preview}); "
                "positive test (test_launcher_survives_sibling_struct_py) still guards the fix"
            )


class TestSignalBroadcastGuard:
    """seccomp kill(-1) broadcast denial + KIROCREW_HOST_PID export.

    Redo of the reverted PID-namespace isolation (24c320f6 → 14fb9442): the
    broadcast accident is contained by a static seccomp arg filter instead of
    a namespace, so the subtree's view of pids — and every host-PID-coupled
    mechanism (session identity, claim-push, systemd) — stays intact.
    """

    @_POSIX_ONLY
    def test_launcher_script_contains_kill_filter(self):
        """Static: the generated launcher carries the kill-broadcast filter
        (arg-inspection block) and per-arch kill syscall numbers."""
        script = _build_launcher_script("standard")
        assert "_KILL_NR = 62" in script  # x86_64 kill
        assert "_KILL_NR = 129" in script  # aarch64 kill
        # arg-inspection: args[0] LOW word only, at seccomp_data offset 16.
        # The high word (offset 20) must NOT be matched: pid_t is a 32-bit
        # int and the x86-64 ABI leaves the upper register half undefined
        # (glibc zero-extends, so a high==0xFFFFFFFF check never fires).
        assert "0, 0, 16))" in script
        assert "0, 0, 20))" not in script
        assert "0xFFFFFFFF" in script  # 32-bit pid -1 comparison

    @_POSIX_ONLY
    def test_launcher_script_exports_host_pid(self):
        """Static: launcher exports KIROCREW_HOST_PID before fork so the
        whole subtree can resolve session_pid files by the recorded pid."""
        script = _build_launcher_script("standard")
        assert 'os.environ["KIROCREW_HOST_PID"] = str(os.getpid())' in script
        # Must appear in main() BEFORE the fork so the child inherits it.
        assert script.index("KIROCREW_HOST_PID") < script.index("os.fork()")

    def test_kill_broadcast_denied_targeted_allowed_e2e(self, tmp_path):
        """Live e2e through the real launcher: inside the sandbox,
        ``os.kill(-1, 0)`` must fail with EPERM (seccomp) while a targeted
        ``os.kill(own_pid, 0)`` succeeds and KIROCREW_HOST_PID is present.

        Safe by construction: signal 0 is a pure permission/existence probe —
        no signal is ever delivered, even if the filter were absent.
        """
        if sys.platform != "linux":
            pytest.skip("sandbox launcher is Linux-only")
        import kiro_crew.sandbox as _sb

        if not _sb._probe_unshare():
            # Probes CLONE_NEWUSER|CLONE_NEWNS — fails closed on CI hosts
            # (e.g. GitHub Actions) where the mount namespace is blocked.
            pytest.skip("user+mount namespaces unavailable on this host")
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import os, sys\n"
            "try:\n"
            "    os.kill(-1, 0)\n"
            "    print('BROADCAST_ALLOWED')\n"
            "except PermissionError:\n"
            "    print('BROADCAST_EPERM')\n"
            "except OSError as e:\n"
            "    print(f'BROADCAST_OSERROR_{e.errno}')\n"
            "os.kill(os.getpid(), 0)\n"
            "print('TARGETED_OK')\n"
            "print('HOSTPID_' + ('SET' if os.environ.get('KIROCREW_HOST_PID', '').isdigit() else 'MISSING'))\n"
        )
        launcher = tmp_path / "launcher.py"
        launcher.write_text(_build_launcher_script("standard"))
        result = subprocess.run(
            [sys.executable, str(launcher), sys.executable, str(probe)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if "unshare(NEWUSER) failed" in result.stderr or "unshare(NEWNS) failed" in result.stderr:
            pytest.skip("namespaces unavailable on this host")
        assert result.returncode == 0, result.stderr
        assert (
            "BROADCAST_EPERM" in result.stdout
        ), f"kill(-1, 0) not denied: stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "TARGETED_OK" in result.stdout, result.stdout
        assert "HOSTPID_SET" in result.stdout, result.stdout


class TestSandboxExecArgv:
    def test_exports_the_in_sandbox_marker(self):
        """The seatbelt wrap must mark the tree, mirroring the Linux launcher.

        Without this marker an in-sandbox ``wrap_argv`` call cannot tell that
        KiroCrew's own sandbox already confines it, tries to nest, and gets EPERM
        — which then fail-closes every app-backend and MCP spawn. The marker must
        land AFTER the ``-u`` flags (an assignment, not something ``-u`` can drop)
        and BEFORE ``sandbox-exec``.
        """
        argv, profile_path = sandbox_exec_argv(["git", "status"], "standard")
        try:
            marker = f"{sandbox_mod._IN_SANDBOX_MARKER}=1"
            assert marker in argv
            # The confiner is emitted as an absolute path, not a bare name, so a
            # PATH overlay handed to the outer ``env`` cannot redirect it (see
            # TestSandboxExecArgvPinsInnerConfiner). Locate it by basename.
            confiner = next(i for i, a in enumerate(argv) if os.path.basename(a) == "sandbox-exec")
            assert argv.index(marker) < confiner
            # Both wrappers are absolute for the same reason, so the outer ``env``
            # is matched by basename too.
            assert os.path.basename(argv[0]) == "env"
        finally:
            if profile_path:
                os.unlink(profile_path)

    @patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "fake", "SSH_AUTH_SOCK": "/tmp/ssh"})
    def test_includes_env_unset_flags(self):
        argv, profile_path = sandbox_exec_argv(["kiro-cli", "acp"], "strict")
        try:
            assert os.path.basename(argv[0]) == "env"
            assert "-u" in argv
            assert "AWS_SECRET_ACCESS_KEY" in argv
            assert "SSH_AUTH_SOCK" in argv
            # Absolute, not a bare name — the confiner is pinned so an overlaid
            # PATH cannot redirect it (see TestSandboxExecArgvPinsInnerConfiner).
            assert any(os.path.basename(a) == "sandbox-exec" for a in argv)
            assert "-f" in argv
            assert profile_path is not None
            assert os.path.exists(profile_path)
        finally:
            if profile_path:
                os.unlink(profile_path)

    @patch.dict(os.environ, {"PYTHONPATH": "/opt/kirocrew/site-packages", "PYTHONHOME": "/opt/py"})
    def test_strips_python_env_when_requested(self):
        # A foreign Python subprocess (kiro-cli's MCP servers, e.g. ord-mcp) must
        # NOT inherit KiroCrew's PYTHONPATH/PYTHONHOME, or it prepends KiroCrew's
        # site-packages to sys.path and imports KiroCrew's fastmcp/cryptography
        # instead of its own. strip_python_env=True unsets them.
        argv, profile_path = sandbox_exec_argv(["kiro-cli", "acp"], "strict", strip_python_env=True)
        try:
            assert "PYTHONPATH" in argv
            assert "PYTHONHOME" in argv
        finally:
            if profile_path:
                os.unlink(profile_path)

    @patch.dict(os.environ, {"PYTHONPATH": "/opt/kirocrew/site-packages", "PYTHONHOME": "/opt/py"})
    def test_preserves_python_env_by_default(self):
        # KiroCrew's OWN sandboxed Python subprocesses (cron scripts, app
        # backends, code-review workers) import kiro_crew via PYTHONPATH, so it
        # must be preserved when strip_python_env is not set (regression guard).
        argv, profile_path = sandbox_exec_argv(["python3", "worker.py"], "standard")
        try:
            assert "PYTHONPATH" not in argv
            assert "PYTHONHOME" not in argv
        finally:
            if profile_path:
                os.unlink(profile_path)

    def test_creates_temp_profile(self):
        argv, profile_path = sandbox_exec_argv(["echo", "hi"], "strict")
        try:
            assert profile_path is not None
            content = Path(profile_path).read_text(encoding="utf-8")
            assert "(version 1)" in content
        finally:
            if profile_path:
                os.unlink(profile_path)


@_POSIX_ONLY
class TestNamespaceArgv:
    @patch("kiro_crew.sandbox._resolve_agent_executable", return_value="/usr/local/bin/kiro-cli")
    def test_wraps_with_python_launcher(self, mock_resolve):
        result = namespace_argv(["kiro-cli", "acp"], "strict")
        assert result[0] == sys.executable
        assert result[1] == "-I"
        assert result[2] == "-S"
        assert result[3].endswith(".py")
        assert result[4] == "/usr/local/bin/kiro-cli"
        assert result[5] == "acp"
        # Cleanup temp file
        os.unlink(result[3])

    @patch("kiro_crew.sandbox._resolve_agent_executable", return_value="/usr/local/bin/kiro-cli")
    def test_launcher_script_is_executable(self, mock_resolve):
        result = namespace_argv(["kiro-cli"], "strict")
        launcher_path = result[3]
        mode = os.stat(launcher_path).st_mode
        assert mode & 0o700 == 0o700
        os.unlink(launcher_path)

    @patch("kiro_crew.sandbox._resolve_agent_executable", return_value="/usr/local/bin/kiro-cli")
    def test_launcher_flags_precede_the_script_path(self, mock_resolve):
        """``-I -S`` must precede the script, or they are script args not flags.

        Everything the interpreter does at startup happens BEFORE the launcher
        reaches ``unshare``. With site processing on, a ``PYTHONUSERBASE``/``HOME``
        taken from a config-declared server ``env`` relocates user-site, whose
        ``.pth`` files EXECUTE during startup -- unconfined code, which no argv[0]
        pin can stop because the interpreter is the pinned binary.
        """
        result = namespace_argv(["kiro-cli"], "strict")
        script = next(a for a in result if a.endswith(".py"))
        try:
            flags = result[1 : result.index(script)]
            assert "-I" in flags, f"-I must be an interpreter flag, got {result!r}"
            assert "-S" in flags, f"-S must be an interpreter flag, got {result!r}"
        finally:
            os.unlink(script)

    @patch("kiro_crew.sandbox._resolve_agent_executable", return_value="/bin/true")
    def test_launcher_flags_block_startup_code_execution(self, mock_resolve):
        """End-to-end: the emitted flags must neutralise env-driven startup code.

        Runs the REAL interpreter with the REAL flags ``namespace_argv`` emits and
        an env that would otherwise execute attacker code during startup — i.e.
        before the launcher script reaches ``unshare``, so outside any confinement.

        Uses ``sitecustomize`` (imported by ``site`` at startup) rather than a
        user-site ``.pth``: ``site.ENABLE_USER_SITE`` is False inside a virtualenv,
        so a user-site fixture is silently inert under the project's own test venv
        and would prove nothing. Both are the same class — code ``site`` runs at
        startup from env-derived paths — and ``-S`` (no ``site`` at all) closes the
        class rather than either instance. The self-check below enforces that the
        fixture is live, so this cannot rot into a vacuous pass.
        """
        result = namespace_argv(["/bin/true"], "strict")
        script = next(a for a in result if a.endswith(".py"))
        flags = result[1 : result.index(script)]
        try:
            with tempfile.TemporaryDirectory() as payload_dir:
                marker = Path(payload_dir) / "pwned"
                Path(payload_dir, "sitecustomize.py").write_text(
                    f"import pathlib; pathlib.Path({str(marker)!r}).write_text('x')\n",
                    encoding="utf-8",
                )
                env = dict(os.environ)
                env["PYTHONPATH"] = payload_dir

                # Self-check: WITHOUT the flags the payload must fire, otherwise a
                # pass below would mean nothing.
                subprocess.run(
                    [result[0], "-c", "pass"], env=env, capture_output=True, timeout=60
                )
                assert marker.exists(), (
                    "fixture is inert: payload did not execute even WITHOUT the "
                    "hardening flags, so this test proves nothing"
                )
                marker.unlink()

                subprocess.run(
                    [result[0], *flags, "-c", "pass"],
                    env=env,
                    capture_output=True,
                    timeout=60,
                )
                assert not marker.exists(), (
                    "env-derived code executed at interpreter startup despite the "
                    "launcher flags; that runs unconfined, before unshare"
                )
        finally:
            os.unlink(script)


@_POSIX_ONLY
class TestLauncherCleanupPath:
    """``wrap_argv`` must hand back the script path, never an interpreter flag.

    Regression guard for a real break introduced while adding ``-I -S``: the
    namespace branch returned a hardcoded ``wrapped[1]`` as the tempfile to delete.
    Once flags sat between the executable and the script that became ``"-I"``, so
    every launcher script leaked and the caller tried to ``unlink("-I")``.
    """

    @patch("kiro_crew.sandbox.detect_backend", return_value="namespace")
    @patch("kiro_crew.sandbox._resolve_agent_executable", return_value="/bin/true")
    def test_cleanup_path_is_the_script_not_a_flag(self, _mock_resolve, _mock_backend):
        argv, cleanup = wrap_argv(["/bin/true"], mode="standard")
        try:
            assert cleanup is not None
            assert not cleanup.startswith("-"), f"cleanup is a flag, not a path: {cleanup!r}"
            assert cleanup.endswith(".py")
            assert os.path.exists(cleanup), "cleanup path must be the real tempfile"
            assert cleanup in argv
        finally:
            if cleanup and os.path.exists(cleanup):
                os.unlink(cleanup)

    def test_accessor_tracks_the_flag_tuple(self):
        """The accessor must be derived from the flag list, not a constant.

        Simulates a future flag being added: if the offset were hardcoded this
        returns a flag instead of the path.
        """
        with patch.object(sandbox_mod, "_LAUNCHER_INTERPRETER_FLAGS", ("-I", "-S", "-X", "y")):
            fake = ["/usr/bin/python", "-I", "-S", "-X", "y", "/run/l.py", "cmd"]
            assert sandbox_mod._launcher_script_of(fake) == "/run/l.py"


@_POSIX_ONLY
class TestPinnedEnvBin:
    """The credential scrub's own binary must not be PATH-redirectable.

    On the delegation paths no Seatbelt/namespace layer wraps the child, so
    ``env -u KEY ...`` IS the only control stripping Slack tokens / owner id. A
    bare ``"env"`` token resolves through the PATH we hand ``Popen`` -- which on
    the script-cron MCP path can come from a config-declared ``env`` block. If it
    is redirected the scrub never runs and the child inherits the credentials.
    """

    def test_pins_absolute_path_from_trusted_system_bin(self):
        with patch(
            "kiro_crew.platform_compat.trusted_system_bin",
            return_value="/usr/bin/env",
        ):
            assert sandbox_mod._pinned_env_bin() == "/usr/bin/env"

    def test_falls_back_to_canonical_location_not_bare_name(self):
        with patch("kiro_crew.platform_compat.trusted_system_bin", return_value=None):
            resolved = sandbox_mod._pinned_env_bin()
        assert os.path.isabs(resolved), f"fallback must be absolute, got {resolved!r}"
        assert resolved == "/usr/bin/env"

    def test_no_producer_emits_a_bare_env_token(self):
        """Guards the sibling-site class across all producers.

        Two delegation returns plus ``sandbox_exec_argv`` build an ``env`` prefix;
        a future producer could reintroduce the bare form, which is invisible in
        review because it looks identical to the pinned one at the call site.
        """
        source = Path(sandbox_mod.__file__).read_text(encoding="utf-8")
        assert '["env",' not in source, (
            'a bare ["env", ...] argv prefix is PATH-redirectable; use _pinned_env_bin()'
        )


class TestSshSupportsAcceptNew:
    def test_modern_ssh(self):
        _ssh_supports_accept_new.cache_clear()
        mock_result = MagicMock(stderr=b"OpenSSH_9.2p1 Debian-2, OpenSSL 3.0.8")
        with patch("subprocess.run", return_value=mock_result):
            assert _ssh_supports_accept_new() is True
        _ssh_supports_accept_new.cache_clear()

    def test_old_ssh(self):
        _ssh_supports_accept_new.cache_clear()
        mock_result = MagicMock(stderr=b"OpenSSH_7.4p1, OpenSSL 1.0.2k")
        with patch("subprocess.run", return_value=mock_result):
            assert _ssh_supports_accept_new() is False
        _ssh_supports_accept_new.cache_clear()

    def test_ssh_not_found(self):
        _ssh_supports_accept_new.cache_clear()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _ssh_supports_accept_new() is False
        _ssh_supports_accept_new.cache_clear()


class TestAgentExecutableResolver:
    def test_default_resolver_is_identity(self):
        assert _resolve_agent_executable("/usr/local/bin/kiro-cli") == "/usr/local/bin/kiro-cli"

    def test_edition_resolver_can_replace_executable(self):
        resolver = MagicMock()
        resolver.resolve_executable.return_value = "/opt/agent/bin/kiro-cli"
        context = MagicMock()
        context.agent_executable = resolver
        with patch("kiro_crew.sandbox.current_context", return_value=context):
            result = _resolve_agent_executable("/usr/local/bin/kiro-cli")
        assert result == "/opt/agent/bin/kiro-cli"
        resolver.resolve_executable.assert_called_once_with("/usr/local/bin/kiro-cli")

    def test_transient_resolver_failure_preserves_original(self):
        resolver = MagicMock()
        resolver.resolve_executable.side_effect = RuntimeError("resolver unavailable")
        context = MagicMock()
        context.agent_executable = resolver
        with patch("kiro_crew.sandbox.current_context", return_value=context):
            result = _resolve_agent_executable("/usr/local/bin/kiro-cli")
        assert result == "/usr/local/bin/kiro-cli"

    def test_composition_failure_propagates(self):
        from kiro_crew.platform.context import PlatformCompositionError

        resolver = MagicMock()
        resolver.resolve_executable.side_effect = PlatformCompositionError("companion unavailable")
        context = MagicMock()
        context.agent_executable = resolver
        with (
            patch("kiro_crew.sandbox.current_context", return_value=context),
            pytest.raises(PlatformCompositionError),
        ):
            _resolve_agent_executable("/usr/local/bin/kiro-cli")


class TestSandboxNoWarningWhenExpected:
    """no WARNING for an *acknowledged* no-sandbox state.

    CSE SEC-009 makes an unacknowledged no-sandbox fallback a loud WARNING
    (covered in test_sandbox_no_isolation.py). When the operator has opted in
    via ``agent.sandbox_allow_no_isolation`` the message is demoted to INFO —
    this preserves the upstream project's "don't spam on expected states" intent.
    """

    @patch("kiro_crew.sandbox._allow_unsandboxed_exec", return_value=True)
    @patch("kiro_crew.sandbox._allow_no_isolation", return_value=True)
    @patch("kiro_crew.sandbox.detect_backend", return_value="none")
    def test_no_sandbox_opted_in_logs_info_not_warning(
        self, mock_detect, mock_optin, mock_allow, caplog
    ):
        import logging

        if hasattr(wrap_argv, "_warned"):
            del wrap_argv._warned  # type: ignore[attr-defined]
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.sandbox"):
            wrap_argv(["kiro-cli", "acp"], mode="auto")
        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        info_msgs = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "isolation" in r.message.lower()
        ]
        assert not warning_msgs, f"Expected no WARNING but got: {warning_msgs}"
        assert info_msgs, "Expected INFO about running without isolation"


class TestCleanupStaleSandboxProfiles:
    """Tests for cleanup_stale_sandbox_profiles()."""

    def test_removes_dead_pid_profile(self, tmp_path):
        """Profile file whose PID is dead gets removed."""
        from kiro_crew.sandbox import cleanup_stale_sandbox_profiles

        run_dir = tmp_path / ".kirocrew" / "run"
        run_dir.mkdir(parents=True)
        stale_file = run_dir / "kirocrew_sandbox_99999_abc123.sb"
        stale_file.write_text("(version 1)")

        with patch("kiro_crew.sandbox.config_dir", return_value=tmp_path / ".kirocrew"):
            with patch("kiro_crew.sandbox.platform_compat.pid_exists", return_value=False):
                removed = cleanup_stale_sandbox_profiles(legacy_dir=str(tmp_path / "nonexistent"))

        assert not stale_file.exists()
        assert removed == 1

    def test_reclaims_retired_acp_snapshot_tree(self, tmp_path):
        """Orphaned pre-in-place-launch kiro-cli copies are reclaimed.

        KiroCrew used to copy the whole ~100 MB kiro-cli binary per ACP spawn
        generation into run/kiro-cli-snapshots and exec the copy. Nothing writes
        that tree now, and nothing else can reclaim it (the file sweep only
        matches kirocrew_sandbox_* files; the tree is on the agent's
        sensitive-path floor), so an upgraded install would leak it forever.
        """
        from kiro_crew.sandbox import cleanup_stale_sandbox_profiles

        home = tmp_path / ".kirocrew"
        holder = home / "run" / "kiro-cli-snapshots" / "kiro-cli-acp-abc123"
        holder.mkdir(parents=True)
        (holder / "kiro-cli").write_bytes(b"orphaned copy")

        with patch("kiro_crew.sandbox.config_dir", return_value=home):
            removed = cleanup_stale_sandbox_profiles(legacy_dir=str(tmp_path / "nonexistent"))

        assert not (home / "run" / "kiro-cli-snapshots").exists()
        assert removed == 1
        # The rest of run/ is untouched, and a second pass is a no-op.
        with patch("kiro_crew.sandbox.config_dir", return_value=home):
            assert cleanup_stale_sandbox_profiles(legacy_dir=str(tmp_path / "nonexistent")) == 0

    def test_preserves_live_pid_profile(self, tmp_path):
        """Profile file whose PID is alive (current process) is preserved."""
        from kiro_crew.sandbox import cleanup_stale_sandbox_profiles

        run_dir = tmp_path / ".kirocrew" / "run"
        run_dir.mkdir(parents=True)
        live_file = run_dir / f"kirocrew_sandbox_{os.getpid()}_xyz789.sb"
        live_file.write_text("(version 1)")

        with patch("kiro_crew.sandbox.config_dir", return_value=tmp_path / ".kirocrew"):
            removed = cleanup_stale_sandbox_profiles(legacy_dir=str(tmp_path / "nonexistent"))

        assert live_file.exists()
        assert removed == 0

    def test_ignores_non_sandbox_files(self, tmp_path):
        """Files not matching kirocrew_sandbox_*.sb pattern are left alone."""
        from kiro_crew.sandbox import cleanup_stale_sandbox_profiles

        run_dir = tmp_path / ".kirocrew" / "run"
        run_dir.mkdir(parents=True)
        other_file = run_dir / "something_else.txt"
        other_file.write_text("keep me")

        with patch("kiro_crew.sandbox.config_dir", return_value=tmp_path / ".kirocrew"):
            removed = cleanup_stale_sandbox_profiles(legacy_dir=str(tmp_path / "nonexistent"))

        assert other_file.exists()
        assert removed == 0


class TestResourceLimitPreexec:
    """resource_limit_preexec() is the cached companion to sandboxed_spawn_argv:
    it hands every agent-influenced spawn the kernel resource ceiling
    (security-review bdf0d7e5)."""

    def _reset_cache(self):
        import kiro_crew.sandbox as sb

        sb._RESOURCE_PREEXEC = sb._UNSET

    @_POSIX_ONLY
    def test_returns_callable_and_caches(self):
        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            first = sb.resource_limit_preexec()
            second = sb.resource_limit_preexec()
            assert callable(first)
            assert first is second
        finally:
            self._reset_cache()

    @_POSIX_ONLY
    def test_config_read_failure_falls_back_to_defaults(self):
        """If config load raises, the preexec still builds from safe defaults
        (no crash, protection still applied)."""
        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            with patch("kiro_crew.config.loader._raw_config", side_effect=RuntimeError("boom")):
                fn = sb.resource_limit_preexec()
            assert callable(fn)
        finally:
            self._reset_cache()

    def test_non_posix_returns_none(self):
        """On non-POSIX (os.name != 'posix'), returns None — create_subprocess_exec
        rejects any non-None preexec_fn on Windows with ValueError, so the
        contract must be None there (review-bot)."""
        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            with patch("kiro_crew.sandbox.os.name", "nt"):
                assert sb.resource_limit_preexec() is None
        finally:
            self._reset_cache()


class TestSessionHostPreexec:
    """session_host_preexec() raises NOFILE to the hard limit for trusted
    session host processes (kiro-cli-chat), preventing EMFILE crashes when
    managing many MCP server subprocesses."""

    def _reset_cache(self):
        import kiro_crew.sandbox as sb

        sb._SESSION_HOST_PREEXEC = sb._UNSET

    @_POSIX_ONLY
    def test_returns_callable_and_caches(self):
        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            first = sb.session_host_preexec()
            second = sb.session_host_preexec()
            assert callable(first)
            assert first is second
        finally:
            self._reset_cache()

    @_POSIX_ONLY
    def test_raises_nofile_to_hard_limit(self):
        """The preexec callable raises NOFILE soft to the hard limit."""
        import resource

        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            fn = sb.session_host_preexec()
            assert fn is not None
            # Save current limits, lower soft to simulate the problem.
            orig_soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            if hard < 2048:
                pytest.skip("hard limit too low for test")
            resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
            try:
                fn()
                new_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
                if hard == resource.RLIM_INFINITY:
                    # Implementation contract: unlimited hard (macOS) caps the
                    # soft limit at max(inherited_soft, 65536), never infinity.
                    assert new_soft == 65536
                else:
                    assert new_soft == hard
            finally:
                resource.setrlimit(resource.RLIMIT_NOFILE, (orig_soft, hard))
        finally:
            self._reset_cache()

    def test_non_posix_returns_none(self):
        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            with patch("kiro_crew.sandbox.os.name", "nt"):
                assert sb.session_host_preexec() is None
        finally:
            self._reset_cache()


@pytest.mark.usefixtures("systemd_run_resolvable")
class TestCgroupScopeArgv:
    """cgroup_scope_argv() wraps agent spawns in a transient systemd --user
    --scope with pids.max + memory.max — the default-on fork-bomb / memory-DoS
    ceiling the finding's headline threats require (security-review bdf0d7e5)."""

    def _reset_probe(self):
        import kiro_crew.sandbox as sb

        sb._CGROUP_SCOPE_PROBE = None
        sb._CGROUP_WARNED = False

    def test_available_prepends_systemd_scope_with_limits(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 50, 0),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=True),
            ):
                out = sb.cgroup_scope_argv(["kiro-cli", "chat"])
            # Absolute: the wrapper is pinned where it is prepended, so a caller
            # passing a config-declared PATH in env= cannot redirect argv[0].
            assert os.path.basename(out[0]) == "systemd-run"
            assert "--user" in out and "--scope" in out
            assert "TasksMax=8192" in out
            assert "MemoryMax=8192M" in out
            assert "MemorySwapMax=0" in out
            assert "CPUWeight=50" in out
            # CPUQuota is opt-in: absent unless max_cpu_percent > 0.
            assert not any(a.startswith("CPUQuota=") for a in out)
            assert out[out.index("--") + 1 :] == ["kiro-cli", "chat"]
        finally:
            self._reset_probe()

    @_POSIX_ONLY
    def test_cpu_controller_delegated_real_path(self):
        """Cover the uncached probe body: reads the user-slice controllers file
        and reports cpu presence; failures report False (skip CPU properties,
        keep pids/memory enforcement)."""
        from unittest.mock import mock_open

        import kiro_crew.sandbox as sb

        try:
            sb._CPU_DELEGATED = None
            with patch("builtins.open", mock_open(read_data="cpu memory pids\n")):
                assert sb._cpu_controller_delegated() is True
            sb._CPU_DELEGATED = None
            with patch("builtins.open", mock_open(read_data="memory pids\n")):
                assert sb._cpu_controller_delegated() is False
            sb._CPU_DELEGATED = None
            with patch("builtins.open", side_effect=OSError("no cgroup")):
                assert sb._cpu_controller_delegated() is False
            # Cached: second call must not re-read.
            with patch("builtins.open", side_effect=AssertionError("must not open")):
                assert sb._cpu_controller_delegated() is False
        finally:
            sb._CPU_DELEGATED = None

    def test_cpu_quota_emitted_when_configured(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 75, 200),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=True),
            ):
                out = sb.cgroup_scope_argv(["kiro-cli", "chat"])
            assert "CPUWeight=75" in out
            assert "CPUQuota=200%" in out
        finally:
            self._reset_probe()

    def test_no_cpu_properties_without_cpu_delegation(self):
        """pids/memory enforcement must not be lost when only cpu delegation
        is missing — the scope is still created, minus the CPU properties."""
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 50, 200),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=False),
            ):
                out = sb.cgroup_scope_argv(["kiro-cli", "chat"])
            assert os.path.basename(out[0]) == "systemd-run"
            assert "TasksMax=8192" in out
            assert not any(a.startswith("CPUWeight=") for a in out)
            assert not any(a.startswith("CPUQuota=") for a in out)
        finally:
            self._reset_probe()

    def test_unavailable_is_passthrough_and_warns_once(self, caplog):
        import logging

        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with patch(
                "kiro_crew.sandbox._probe_cgroup_scope",
                return_value=(False, "not Linux"),
            ):
                with caplog.at_level(logging.WARNING):
                    out1 = sb.cgroup_scope_argv(["git", "status"])
                    out2 = sb.cgroup_scope_argv(["git", "log"])
            assert out1 == ["git", "status"]
            assert out2 == ["git", "log"]
            sec = [r for r in caplog.records if "SECURITY" in r.getMessage()]
            assert len(sec) == 1
            assert "not Linux" in sec[0].getMessage()
        finally:
            self._reset_probe()

    def test_config_overrides_cgroup_limits(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with patch(
                "kiro_crew.config.loader._raw_config",
                return_value={
                    "resource_limits": {
                        "max_processes": 200,
                        "max_memory_mb": 2048,
                        "cpu_weight": 80,
                        "max_cpu_percent": 400,
                    }
                },
            ):
                procs, mem, weight, quota = sb._cgroup_limits_from_config()
            assert procs == 200
            assert mem == 2048
            assert weight == 80
            assert quota == 400
        finally:
            self._reset_probe()

    def test_config_defaults_when_absent_or_zero(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            # Missing block -> module defaults (never leave the cgroup ceiling
            # unset). Memory default is host-proportional (65% of RAM).
            with patch("kiro_crew.config.loader._raw_config", return_value={}):
                procs, mem, weight, quota = sb._cgroup_limits_from_config()
            assert procs == sb._CGROUP_DEFAULT_MAX_PROCESSES
            assert mem == sb._default_max_memory_mb()
            assert weight == sb._CGROUP_DEFAULT_CPU_WEIGHT
            assert quota == 0  # opt-in: no CPUQuota by default
            with patch(
                "kiro_crew.config.loader._raw_config",
                return_value={
                    "resource_limits": {
                        "max_processes": 0,
                        "max_memory_mb": "x",
                        "cpu_weight": 0,
                        "max_cpu_percent": -5,
                    }
                },
            ):
                procs, mem, weight, quota = sb._cgroup_limits_from_config()
            assert procs == sb._CGROUP_DEFAULT_MAX_PROCESSES
            assert mem == sb._default_max_memory_mb()
            assert weight == sb._CGROUP_DEFAULT_CPU_WEIGHT
            assert quota == 0
            # Fractions must not truncate into invalid TasksMax=0 /
            # MemoryMax=0M properties.
            with patch(
                "kiro_crew.config.loader._raw_config",
                return_value={
                    "resource_limits": {
                        "max_processes": 0.5,
                        "max_memory_mb": 0.9,
                    }
                },
            ):
                procs, mem, _, _ = sb._cgroup_limits_from_config()
            assert procs == sb._CGROUP_DEFAULT_MAX_PROCESSES
            assert mem == sb._default_max_memory_mb()
            # NaN/Infinity (json.loads accepts both) must fall back to
            # defaults WITHOUT raising: int(nan)/int(inf) raise inside the
            # surrounding try/except, which would silently discard an
            # otherwise-valid stricter limit on a later field in the same
            # block (e.g. a legitimate max_memory_mb after a bogus
            # max_processes).
            with patch(
                "kiro_crew.config.loader._raw_config",
                return_value={
                    "resource_limits": {
                        "max_processes": float("nan"),
                        "max_memory_mb": 512,
                    }
                },
            ):
                procs, mem, _, _ = sb._cgroup_limits_from_config()
            assert procs == sb._CGROUP_DEFAULT_MAX_PROCESSES
            assert mem == 512  # must not be discarded by the NaN above it
            with patch(
                "kiro_crew.config.loader._raw_config",
                return_value={
                    "resource_limits": {
                        "max_processes": 64,
                        "max_memory_mb": float("inf"),
                    }
                },
            ):
                procs, mem, _, _ = sb._cgroup_limits_from_config()
            assert procs == 64  # must not be discarded by the inf below it
            assert mem == sb._default_max_memory_mb()
        finally:
            self._reset_probe()

    @_POSIX_ONLY
    def test_default_max_memory_is_host_proportional(self):
        """The memory default scales with physical RAM (65%), not a flat cap."""
        import kiro_crew.sandbox as sb

        # A known 16 GiB box -> 65% -> ~10649 MB.
        sixteen_g = 16 * 1024**3
        with patch("os.sysconf", side_effect=lambda n: sixteen_g // 4096 if "PHYS" in n else 4096):
            mb = sb._default_max_memory_mb()
        assert mb == int(sixteen_g * sb._CGROUP_MEMORY_FRACTION) // (1024 * 1024)
        assert 10_000 < mb < 11_000  # ~10.6 GB, expected range

    @_POSIX_ONLY
    def test_default_max_memory_falls_back_when_ram_unknown(self):
        """If sysconf can't report RAM, fall back to the flat MB constant.

        ``system_memory`` is stubbed out alongside ``os.sysconf`` because it is
        the second probe: on Windows ``GlobalMemoryStatusEx`` answers, so patching
        only ``sysconf`` would no longer make RAM unknown and this would assert
        against a derived value instead of the fallback.
        """
        import kiro_crew.sandbox as sb

        with patch("os.sysconf", side_effect=OSError("no sysconf")), patch.object(
            sb.platform_compat, "system_memory", return_value=None
        ):
            assert sb._default_max_memory_mb() == sb._CGROUP_FALLBACK_MAX_MEMORY_MB
        # Non-positive product also falls back (never returns 0 -> unlimited).
        with patch("os.sysconf", return_value=0), patch.object(
            sb.platform_compat, "system_memory", return_value=None
        ):
            assert sb._default_max_memory_mb() == sb._CGROUP_FALLBACK_MAX_MEMORY_MB

    @pytest.mark.skipif(sys.platform != "linux", reason="cgroup v2 scope enforcement is Linux-only")
    def test_real_pids_max_enforced_when_available(self):
        """If this host actually has cgroup delegation, the scope must ENFORCE
        pids.max — a child under a tiny TasksMax cannot fork past it. Skips
        cleanly where delegation is unavailable (the probe returns False)."""
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            available, _ = sb._probe_cgroup_scope()
            if not available:
                pytest.skip("no cgroup v2 delegation on this host")
            with patch(
                "kiro_crew.sandbox._cgroup_limits_from_config", return_value=(20, 8192, 50, 0)
            ):
                argv = sb.cgroup_scope_argv(
                    [
                        sys.executable,
                        "-c",
                        "import os,sys\n"
                        "n=0\n"
                        "try:\n"
                        "    for _ in range(200):\n"
                        "        if os.fork()==0:\n"
                        "            import time; time.sleep(1); os._exit(0)\n"
                        "        n+=1\n"
                        "    print('forked-all')\n"
                        "except OSError:\n"
                        "    print('hit-limit')\n",
                    ]
                )
            out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            assert out.returncode == 0, out.stderr
            assert out.stdout.strip() == "hit-limit"
        finally:
            self._reset_probe()


@pytest.mark.usefixtures("systemd_run_resolvable")
class TestAgentsSliceLimits:
    """ensure_agents_slice_limits() puts an AGGREGATE MemoryMax/TasksMax on
    kirocrew-agents.slice — the parent of every per-spawn scope — so N
    concurrent scopes cannot collectively request N x 65% of host RAM while
    each stays inside its own per-scope ceiling."""

    def _reset(self):
        import kiro_crew.sandbox as sb

        sb._CGROUP_SCOPE_PROBE = None
        sb._CGROUP_WARNED = False
        sb._SLICE_LIMITS_APPLIED = False
        sb._SLICE_OOM_SEEN = None

    def test_applies_runtime_property_once_idempotent(self):
        """One systemctl invocation with the exact property set; a second call
        is a no-op returning True (idempotent across restarts of the caller).
        argv[0] must be the TRUSTED absolute path, never a bare name PATH
        could resolve to an agent-planted shim."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            run_mock = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch("kiro_crew.sandbox._slice_limits_from_config", return_value=(10000, 32768)),
                patch(
                    "kiro_crew.platform_compat.trusted_system_bin",
                    return_value="/usr/bin/systemctl",
                ),
                patch("kiro_crew.sandbox.subprocess.run", run_mock),
            ):
                assert sb.ensure_agents_slice_limits() is True
                assert sb.ensure_agents_slice_limits() is True
            assert run_mock.call_count == 1
            argv = run_mock.call_args[0][0]
            assert argv == [
                "/usr/bin/systemctl",
                "--user",
                "set-property",
                "--runtime",
                "kirocrew-agents.slice",
                "MemoryMax=10000M",
                "MemorySwapMax=0",
                "TasksMax=32768",
            ]
        finally:
            self._reset()

    def test_no_trusted_systemctl_means_no_apply(self):
        """PATH is never consulted: when no trusted systemctl exists, the
        ceiling is skipped (returns False), not resolved through PATH."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            run_mock = MagicMock()
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch("kiro_crew.platform_compat.trusted_system_bin", return_value=None),
                patch("kiro_crew.sandbox.subprocess.run", run_mock),
            ):
                assert sb.ensure_agents_slice_limits() is False
            run_mock.assert_not_called()
        finally:
            self._reset()

    def test_skipped_when_unavailable_and_no_second_warning(self, caplog):
        """No delegation -> no systemctl call, and the slice site plus the
        per-spawn site together emit exactly ONE SECURITY warning."""
        import logging

        import kiro_crew.sandbox as sb

        self._reset()
        try:
            run_mock = MagicMock()
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(False, "not Linux")),
                patch("kiro_crew.sandbox.subprocess.run", run_mock),
                caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"),
            ):
                assert sb.ensure_agents_slice_limits() is False
                out = sb.cgroup_scope_argv(["git", "status"])
            run_mock.assert_not_called()
            assert out == ["git", "status"]
            security = [r for r in caplog.records if "SECURITY" in r.getMessage()]
            assert len(security) == 1
        finally:
            self._reset()

    def test_failed_apply_is_retried_next_call(self):
        """A nonzero rc leaves the ceiling unapplied — the next call retries
        rather than caching the failure as success."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            run_mock = MagicMock(return_value=MagicMock(returncode=1, stderr="boom"))
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch("kiro_crew.sandbox._slice_limits_from_config", return_value=(10000, 32768)),
                patch(
                    "kiro_crew.platform_compat.trusted_system_bin",
                    return_value="/usr/bin/systemctl",
                ),
                patch("kiro_crew.sandbox.subprocess.run", run_mock),
            ):
                assert sb.ensure_agents_slice_limits() is False
                assert sb.ensure_agents_slice_limits() is False
            assert run_mock.call_count == 2
        finally:
            self._reset()

    def test_config_overrides_slice_limits(self):
        import kiro_crew.sandbox as sb

        with patch(
            "kiro_crew.config.loader._raw_config",
            return_value={
                "resource_limits": {
                    "max_total_memory_mb": 4096,
                    "max_total_processes": 1000,
                }
            },
        ):
            mem, tasks = sb._slice_limits_from_config()
        assert mem == 4096
        assert tasks == 1000

    def test_config_defaults_when_absent_or_junk(self):
        """Zero/junk falls back to the default rather than leaving the
        aggregate unset — same rule as the per-scope ceiling."""
        import kiro_crew.sandbox as sb

        with patch("kiro_crew.config.loader._raw_config", return_value={}):
            mem, tasks = sb._slice_limits_from_config()
        assert mem == sb._default_max_total_memory_mb()
        assert tasks == sb._CGROUP_DEFAULT_MAX_TOTAL_TASKS
        with patch(
            "kiro_crew.config.loader._raw_config",
            return_value={
                "resource_limits": {
                    "max_total_memory_mb": 0,
                    "max_total_processes": "x",
                }
            },
        ):
            mem, tasks = sb._slice_limits_from_config()
        assert mem == sb._default_max_total_memory_mb()
        assert tasks == sb._CGROUP_DEFAULT_MAX_TOTAL_TASKS
        # Fractional values pass a naive `> 0` check but truncate to 0, which
        # would emit MemoryMax=0M and kill every agent scope — must fall back.
        with patch(
            "kiro_crew.config.loader._raw_config",
            return_value={
                "resource_limits": {
                    "max_total_memory_mb": 0.5,
                    "max_total_processes": 0.9,
                }
            },
        ):
            mem, tasks = sb._slice_limits_from_config()
        assert mem == sb._default_max_total_memory_mb()
        assert tasks == sb._CGROUP_DEFAULT_MAX_TOTAL_TASKS

    @_POSIX_ONLY
    def test_default_total_memory_fraction_and_fallback(self):
        """80% of RAM by default; flat fallback when RAM is unreadable. Both
        must sit ABOVE their per-scope counterparts, or the slice would clamp
        a single spawn tighter than its own documented ceiling."""
        import kiro_crew.sandbox as sb

        sixteen_g = 16 * 1024**3
        with patch("os.sysconf", side_effect=lambda n: sixteen_g // 4096 if "PHYS" in n else 4096):
            mb = sb._default_max_total_memory_mb()
        assert mb == int(sixteen_g * sb._CGROUP_TOTAL_MEMORY_FRACTION) // (1024 * 1024)
        with patch("os.sysconf", side_effect=OSError("no sysconf")):
            assert sb._default_max_total_memory_mb() == sb._CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB
        assert sb._CGROUP_TOTAL_MEMORY_FRACTION > sb._CGROUP_MEMORY_FRACTION
        assert sb._CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB > sb._CGROUP_FALLBACK_MAX_MEMORY_MB

    def test_per_scope_property_still_emitted_ratchet(self):
        """RATCHET: the two-level model needs BOTH layers. The per-spawn scope
        must keep emitting its own MemoryMax under the slice — a future change
        must not silently replace per-tree bounding with aggregate-only."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 4096, 50, 0),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=False),
            ):
                out = sb.cgroup_scope_argv(["kiro-cli", "chat"])
            # Still placed under the aggregate boundary, now via a per-instance
            # child of it (see _agents_slice_name) — cgroup v2 bounds a
            # descendant by the minimum effective limit along its ancestor
            # chain, so the aggregate layer of the two-level model is intact.
            parent_stem = sb._CGROUP_AGENTS_SLICE[: -len(".slice")]
            assert any(
                a.startswith(f"--slice={parent_stem}") and a.endswith(".slice") for a in out
            ), out
            assert "MemoryMax=4096M" in out
            assert "TasksMax=8192" in out
        finally:
            self._reset()

    def _fake_slice(self, tmp_path, *, oom_kill=0, local_max=0, current=100, mem_max="1000"):
        d = tmp_path / "kirocrew-agents.slice"
        d.mkdir(exist_ok=True)
        (d / "memory.events").write_text(f"low 0\nhigh 0\nmax 0\noom 0\noom_kill {oom_kill}\n")
        (d / "memory.events.local").write_text(
            f"low 0\nhigh 0\nmax {local_max}\noom 0\noom_kill 0\n"
        )
        (d / "memory.current").write_text(f"{current}\n")
        (d / "memory.max").write_text(f"{mem_max}\n")
        return d

    def test_slice_pressure_seeds_then_reports_new_kills(self, tmp_path):
        """First read seeds the counters (no spurious boot warning); a later
        oom_kill increase is reported with the victim scope and whether the
        slice-level ceiling engaged."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            d = self._fake_slice(tmp_path, oom_kill=2)
            with patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=d):
                assert sb.check_agents_slice_pressure() is None  # seed only
                # A scope takes a kill and the slice's own limit engaged.
                self._fake_slice(tmp_path, oom_kill=3, local_max=1)
                victim = d / "run-r1.scope"
                victim.mkdir()
                (victim / "memory.events.local").write_text(
                    "low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n"
                )
                msg = sb.check_agents_slice_pressure()
                assert msg is not None
                assert "1 new kill(s)" in msg
                assert "run-r1.scope" in msg
                assert "aggregate ceiling engaged: yes" in msg
                # No further change -> quiet.
                assert sb.check_agents_slice_pressure() is None
        finally:
            self._reset()

    def test_slice_pressure_scope_local_breach_is_distinguished(self, tmp_path):
        """A kill without a slice-level max event reads as a per-scope breach."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            d = self._fake_slice(tmp_path)
            with patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=d):
                assert sb.check_agents_slice_pressure() is None
                self._fake_slice(tmp_path, oom_kill=1, local_max=0)
                msg = sb.check_agents_slice_pressure()
                assert msg is not None
                assert "a scope hit its own per-tree limit" in msg
        finally:
            self._reset()

    def test_slice_pressure_none_when_slice_absent(self):
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            with patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=None):
                assert sb.check_agents_slice_pressure() is None
        finally:
            self._reset()

    def test_slice_pressure_self_heals_a_vanished_ceiling(self, tmp_path):
        """A user-manager restart drops the --runtime property. The sampler
        detects memory.max reading 'max' and re-applies — but only when WE
        applied the ceiling before."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            d = self._fake_slice(tmp_path, mem_max="max")
            ensure_mock = MagicMock(return_value=True)
            with (
                patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=d),
                patch("kiro_crew.sandbox.ensure_agents_slice_limits", ensure_mock),
            ):
                sb._SLICE_LIMITS_APPLIED = True
                sb.check_agents_slice_pressure()
                ensure_mock.assert_called_once()
                assert sb._SLICE_LIMITS_APPLIED is False  # reset so the re-apply is real
        finally:
            self._reset()

    def test_slice_pressure_no_heal_when_never_applied(self, tmp_path):
        """A host that never passed the delegation gate must not start
        shelling out from the sampler."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            d = self._fake_slice(tmp_path, mem_max="max")
            ensure_mock = MagicMock(return_value=True)
            with (
                patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=d),
                patch("kiro_crew.sandbox.ensure_agents_slice_limits", ensure_mock),
            ):
                sb._SLICE_LIMITS_APPLIED = False
                sb.check_agents_slice_pressure()
                ensure_mock.assert_not_called()
        finally:
            self._reset()

    @pytest.mark.skipif(sys.platform != "linux", reason="cgroup v2 scope enforcement is Linux-only")
    def test_real_scope_nests_under_agents_slice(self):
        """On a delegation-capable host, a real scope's cgroup path runs
        through kirocrew-agents.slice — the structural premise of the
        aggregate boundary: whatever limit the slice carries, the kernel
        min-composes it over every scope. When the live slice carries a
        MemoryMax (a running gateway applied one), assert the scope's own
        limit is not the only bound in the ancestry. Skips cleanly where
        delegation is unavailable. No host state is mutated: the test only
        spawns a scope (as every spawn does) and reads cgroup files."""
        import kiro_crew.sandbox as sb

        sb._CGROUP_SCOPE_PROBE = None
        try:
            available, _ = sb._probe_cgroup_scope()
            if not available:
                pytest.skip("no cgroup v2 delegation on this host")
            with patch(
                "kiro_crew.sandbox._cgroup_limits_from_config", return_value=(50, 512, 50, 0)
            ):
                argv = sb.cgroup_scope_argv(
                    [
                        sys.executable,
                        "-c",
                        "cg=open('/proc/self/cgroup').read().split('::',1)[1].strip()\n"
                        "print(cg)\n"
                        "print(open('/sys/fs/cgroup'+cg+'/memory.max').read().strip())\n",
                    ]
                )
            out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            assert out.returncode == 0, out.stderr
            cg_path, scope_max = out.stdout.strip().splitlines()
            assert "/kirocrew-agents.slice/" in cg_path
            assert scope_max == str(512 * 1024 * 1024)
        finally:
            sb._CGROUP_SCOPE_PROBE = None


@pytest.mark.usefixtures("systemd_run_resolvable")
class TestCgroupScopeBusEnv:
    """The systemd-run scope prepended by cgroup_scope_argv needs the user
    session bus in the environment it is spawned with. Callers that build that
    environment from a strict allowlist (source_providers.py) drop the bus
    locators, and systemd-run then dies with "Failed to connect to bus: No
    medium found" before it ever exec's the wrapped command.

    The locators must NOT survive into the sandboxed child, though: a live
    user-bus address there can start a systemd unit outside the sandbox. So the
    forward is paired with an `env -u` shim inside the scope."""

    def _reset_probe(self):
        import kiro_crew.sandbox as sb

        sb._CGROUP_SCOPE_PROBE = None
        sb._CGROUP_WARNED = False

    def test_forwards_bus_locators_into_allowlist_env(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch.dict(
                    os.environ,
                    {
                        "XDG_RUNTIME_DIR": "/run/user/4242",
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus",
                    },
                    clear=False,
                ),
            ):
                out, injected = sb.cgroup_scope_bus_env(
                    {"PATH": "/usr/bin:/bin", "HOME": "/home/u"}
                )
            assert out["XDG_RUNTIME_DIR"] == "/run/user/4242"
            assert out["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/4242/bus"
            assert injected == ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
            # The caller's own keys survive untouched.
            assert out["PATH"] == "/usr/bin:/bin"
            assert out["HOME"] == "/home/u"
        finally:
            self._reset_probe()

    def test_caller_value_wins_and_missing_keys_stay_absent(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            env = {"XDG_RUNTIME_DIR": "/caller/runtime"}
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}, clear=False),
            ):
                os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
                out, injected = sb.cgroup_scope_bus_env(env)
            assert out["XDG_RUNTIME_DIR"] == "/caller/runtime"
            # Nothing to forward -> the key is not invented.
            assert "DBUS_SESSION_BUS_ADDRESS" not in out
            # A caller-supplied value is NOT ours to strip inside the scope.
            assert injected == ()
            # Input dict is never mutated in place.
            assert env == {"XDG_RUNTIME_DIR": "/caller/runtime"}
        finally:
            self._reset_probe()

    def test_passthrough_when_scope_unavailable(self):
        """No systemd-run prefix -> the caller's environment is handed through
        exactly as given, bus locators included or not."""
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch(
                    "kiro_crew.sandbox._probe_cgroup_scope",
                    return_value=(False, "not Linux"),
                ),
                patch.dict(
                    os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}, clear=False
                ),
            ):
                out, injected = sb.cgroup_scope_bus_env({"PATH": "/usr/bin"})
            assert out == {"PATH": "/usr/bin"}
            assert injected == ()
        finally:
            self._reset_probe()

    def test_unset_env_argv_prefix_and_absence(self):
        """The shim is built from an absolute path (never PATH-resolved), and
        reports None when no env binary exists so callers can fail closed."""
        import kiro_crew.sandbox as sb

        argv = sb._unset_env_argv(("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"))
        if argv is not None:
            assert argv[0] in sb._ENV_BINARY_CANDIDATES
            assert os.path.isabs(argv[0])
            assert argv[1:] == [
                "-u",
                "XDG_RUNTIME_DIR",
                "-u",
                "DBUS_SESSION_BUS_ADDRESS",
            ]
        with patch("kiro_crew.sandbox.os.path.isfile", return_value=False):
            assert sb._unset_env_argv(("XDG_RUNTIME_DIR",)) is None

    def test_sandboxed_spawn_argv_forwards_bus_but_child_cannot_keep_it(self):
        """End-to-end at the chokepoint: the spawn env carries the locators (so
        systemd-run can reach the bus) AND the argv drops them again inside the
        scope (so the sandboxed child cannot use the bus)."""
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox.wrap_argv", return_value=(["gh", "pr", "view"], None)),
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 50, 0),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=False),
                patch(
                    "kiro_crew.sandbox._unset_env_argv",
                    return_value=["/usr/bin/env", "-u", "XDG_RUNTIME_DIR", "-u", "DBUS_SESSION_BUS_ADDRESS"],
                ),
                patch.dict(
                    os.environ,
                    {
                        "XDG_RUNTIME_DIR": "/run/user/4242",
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus",
                    },
                    clear=False,
                ),
            ):
                argv, env, _cleanup = sb.sandboxed_spawn_argv(
                    ["gh", "pr", "view"],
                    env={"PATH": "/usr/bin:/bin", "HOME": "/home/u"},
                )
            assert os.path.basename(argv[0]) == "systemd-run"
            assert env["XDG_RUNTIME_DIR"] == "/run/user/4242"
            assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/4242/bus"
            # The shim sits INSIDE the scope, immediately after `--`, so the real
            # command execs without the locators.
            inner = argv[argv.index("--") + 1 :]
            assert inner == [
                "/usr/bin/env",
                "-u",
                "XDG_RUNTIME_DIR",
                "-u",
                "DBUS_SESSION_BUS_ADDRESS",
                "gh",
                "pr",
                "view",
            ]
        finally:
            self._reset_probe()

    def test_no_env_binary_fails_closed_without_leaking_bus(self, caplog):
        """If the locators cannot be dropped again, they are not forwarded at
        all: systemd-run fails loudly rather than the child getting a live bus."""
        import logging

        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox.wrap_argv", return_value=(["gh"], None)),
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 50, 0),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=False),
                patch("kiro_crew.sandbox._unset_env_argv", return_value=None),
                patch.dict(
                    os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}, clear=False
                ),
                caplog.at_level(logging.WARNING),
            ):
                argv, env, _cleanup = sb.sandboxed_spawn_argv(
                    ["gh"], env={"PATH": "/usr/bin:/bin"}
                )
            assert "XDG_RUNTIME_DIR" not in env
            assert "DBUS_SESSION_BUS_ADDRESS" not in env
            assert argv[argv.index("--") + 1 :] == ["gh"]
            assert any("SECURITY" in r.getMessage() for r in caplog.records)
        finally:
            self._reset_probe()


class TestKiroInternalSandboxExclusion:
    """Kiro internal-sandbox delegation stays narrow and fail-closed."""

    def _write_settings(self, tmp_path, monkeypatch, content: str | None):
        p = tmp_path / "amazon-internal.json"
        if content is not None:
            p.write_text(content)
        monkeypatch.setattr("kiro_crew.sandbox._KIRO_INTERNAL_SETTINGS_PATH", str(p))
        return p

    # --- kiro_internal_sandbox_enabled() helper ---

    def test_absent_file_is_disabled(self, tmp_path, monkeypatch):
        from kiro_crew.sandbox import kiro_internal_sandbox_enabled

        self._write_settings(tmp_path, monkeypatch, None)
        assert kiro_internal_sandbox_enabled() is False

    def test_malformed_json_is_disabled(self, tmp_path, monkeypatch):
        from kiro_crew.sandbox import kiro_internal_sandbox_enabled

        self._write_settings(tmp_path, monkeypatch, "{not json")
        assert kiro_internal_sandbox_enabled() is False

    def test_missing_key_is_disabled(self, tmp_path, monkeypatch):
        from kiro_crew.sandbox import kiro_internal_sandbox_enabled

        self._write_settings(tmp_path, monkeypatch, '{"other": true}')
        assert kiro_internal_sandbox_enabled() is False

    def test_true_is_enabled(self, tmp_path, monkeypatch):
        from kiro_crew.sandbox import kiro_internal_sandbox_enabled

        self._write_settings(tmp_path, monkeypatch, '{"sandbox": true}')
        assert kiro_internal_sandbox_enabled() is True

    def test_false_is_disabled(self, tmp_path, monkeypatch):
        from kiro_crew.sandbox import kiro_internal_sandbox_enabled

        self._write_settings(tmp_path, monkeypatch, '{"sandbox": false}')
        assert kiro_internal_sandbox_enabled() is False

    # --- wrap_argv gating ---

    def test_darwin_kiro_spawn_delegates(self, tmp_path, monkeypatch):
        """kiro sandbox ON + darwin + kiro-cli argv -> no seatbelt wrap."""
        self._write_settings(tmp_path, monkeypatch, '{"sandbox": true}')
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "darwin")
        with patch("kiro_crew.sandbox.detect_backend") as mock_detect:
            argv, cleanup = wrap_argv(["/usr/local/bin/kiro-cli", "acp"], mode="auto")
        assert "sandbox-exec" not in argv
        assert argv[-2:] == ["/usr/local/bin/kiro-cli", "acp"]
        assert cleanup is None
        # Delegation decided before backend detection (covers backend=none too)
        mock_detect.assert_not_called()

    def test_darwin_explicit_kiro_classification_delegates_nonstandard_path(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Launch-path shape must not erase Kiro's internal-sandbox identity."""
        self._write_settings(tmp_path, monkeypatch, '{"sandbox": true}')
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "darwin")
        launch = "/Applications/Kiro CLI.app/Contents/MacOS/kiro"
        with patch("kiro_crew.sandbox.detect_backend") as mock_detect:
            argv, cleanup = wrap_argv(
                [launch, "acp"],
                mode="auto",
                is_kiro_cli=True,
            )
        assert argv[-2:] == [launch, "acp"]
        assert cleanup is None
        mock_detect.assert_not_called()

    def test_darwin_kiro_spawn_delegation_scrubs_env(self, tmp_path, monkeypatch):
        """The delegated spawn keeps the seatbelt path's env scrub."""
        self._write_settings(tmp_path, monkeypatch, '{"sandbox": true}')
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "darwin")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sentinel")
        argv, _ = wrap_argv(["kiro-cli", "acp"], mode="auto")
        # Absolute, not the bare "env": this path applies no Seatbelt of ours, so
        # the scrub IS the only control here -- a PATH-redirectable scrubber means
        # the scrub silently never runs and the child keeps the credentials.
        assert os.path.isabs(argv[0]), f"scrubber must be pinned, got {argv[0]!r}"
        assert os.path.basename(argv[0]) == "env"
        assert "-u" in argv
        assert "AWS_SECRET_ACCESS_KEY" in argv

    def test_darwin_non_kiro_spawn_stays_wrapped(self, tmp_path, monkeypatch):
        """Non-kiro spawns have no internal sandbox — seatbelt stays on."""
        self._write_settings(tmp_path, monkeypatch, '{"sandbox": true}')
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "darwin")
        with (
            patch("kiro_crew.sandbox.detect_backend", return_value="sandbox-exec"),
            patch(
                "kiro_crew.sandbox.sandbox_exec_argv",
                return_value=(["sandbox-exec", "python3"], "/tmp/p.sb"),
            ) as mock_sb,
        ):
            wrap_argv(["python3", "-m", "worker"], mode="auto")
        mock_sb.assert_called_once()

    def test_darwin_kiro_disabled_stays_wrapped(self, tmp_path, monkeypatch):
        """kiro sandbox OFF -> KiroCrew's seatbelt ON (the inverse rule)."""
        self._write_settings(tmp_path, monkeypatch, '{"sandbox": false}')
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "darwin")
        with (
            patch("kiro_crew.sandbox.detect_backend", return_value="sandbox-exec"),
            patch(
                "kiro_crew.sandbox.sandbox_exec_argv",
                return_value=(["sandbox-exec", "kiro-cli"], "/tmp/p.sb"),
            ) as mock_sb,
        ):
            wrap_argv(["kiro-cli", "acp"], mode="auto")
        mock_sb.assert_called_once()

    def test_linux_unaffected(self, tmp_path, monkeypatch):
        """Mutual exclusion is macOS-only — Linux namespace path unchanged."""
        self._write_settings(tmp_path, monkeypatch, '{"sandbox": true}')
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "linux")
        with (
            patch("kiro_crew.sandbox.detect_backend", return_value="namespace"),
            patch(
                "kiro_crew.sandbox.namespace_argv",
                return_value=[
                    sys.executable,
                    *sandbox_mod._LAUNCHER_INTERPRETER_FLAGS,
                    "/tmp/launcher.py",
                    "kiro-cli",
                ],
            ) as mock_ns,
        ):
            wrap_argv(["kiro-cli", "acp"], mode="auto")
        mock_ns.assert_called_once()

    def test_windows_explicit_kiro_backend_delegates_before_backend_probe(self, monkeypatch):
        """Windows delegates only when the Kiro sandbox it delegates TO is on."""
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "win32")
        launch = r"C:\Program Files\Kiro\kiro-cli.exe"
        with (
            patch("kiro_crew.sel.sel", return_value=MagicMock()),
            patch("kiro_crew.sandbox.detect_backend") as mock_detect,
            patch("kiro_crew.sandbox.kiro_internal_sandbox_enabled", return_value=True) as mock_cap,
        ):
            argv, cleanup = wrap_argv(
                [launch, "acp"],
                mode="auto",
                strip_python_env=True,
                is_kiro_cli=True,
            )
        assert argv == [launch, "acp"]
        assert cleanup is None
        mock_detect.assert_not_called()
        # The capability is CONSULTED, not assumed: the unwrapped argv above is
        # only safe because the layer it defers to actually exists.
        mock_cap.assert_called()

    def test_windows_kiro_sandbox_disabled_fails_closed(self, monkeypatch):
        """Classification alone cannot buy the Windows delegation.

        A classified Kiro spawn on a host whose internal sandbox is OFF has no
        isolation layer to delegate to, so it must fall through to the normal
        no-backend policy and fail closed — not return an unwrapped argv while
        the audit trail claims a delegated sandbox.
        """
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "win32")
        monkeypatch.setattr("kiro_crew.sandbox._allow_unsandboxed_exec", lambda: False)
        with (
            patch("kiro_crew.sandbox.kiro_internal_sandbox_enabled", return_value=False),
            patch("kiro_crew.sandbox.detect_backend", return_value="none") as mock_detect,
            patch("kiro_crew.sel.sel", return_value=MagicMock()),
            pytest.raises(sandbox_mod.SandboxUnavailableError),
        ):
            wrap_argv(
                [r"C:\Program Files\Kiro\kiro-cli.exe", "acp"],
                mode="auto",
                is_kiro_cli=True,
            )
        # Fall-through reached the ordinary backend decision rather than
        # short-circuiting into the delegation.
        mock_detect.assert_called_once_with(config_mode="auto")

    def test_windows_kiro_sandbox_disabled_honours_explicit_opt_in(self, monkeypatch):
        """The fall-through is the NORMAL path, opt-in included — not a crash."""
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "win32")
        monkeypatch.setattr("kiro_crew.sandbox._allow_unsandboxed_exec", lambda: True)
        launch = r"C:\Program Files\Kiro\kiro-cli.exe"
        with (
            patch("kiro_crew.sandbox.kiro_internal_sandbox_enabled", return_value=False),
            patch("kiro_crew.sandbox.detect_backend", return_value="none") as mock_detect,
            patch("kiro_crew.sel.sel", return_value=MagicMock()),
        ):
            argv, cleanup = wrap_argv([launch, "acp"], mode="auto", is_kiro_cli=True)
        assert argv[-2:] == [launch, "acp"]
        assert cleanup is None
        # Distinguishes the opted-in FALL-THROUGH from the delegation, which
        # returns the same argv but short-circuits before any backend decision.
        mock_detect.assert_called_once_with(config_mode="auto")

    @pytest.mark.parametrize("classification", [None, False])
    def test_windows_nonclassified_spawn_still_fails_closed(self, monkeypatch, classification):
        """A Kiro-looking basename cannot grant the Windows delegation."""
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "win32")
        monkeypatch.setattr("kiro_crew.sandbox._allow_unsandboxed_exec", lambda: False)
        with (
            patch("kiro_crew.sandbox.detect_backend", return_value="none"),
            patch("kiro_crew.sel.sel", return_value=MagicMock()),
            pytest.raises(sandbox_mod.SandboxUnavailableError),
        ):
            wrap_argv(
                [r"C:\Program Files\Kiro\kiro-cli.exe", "acp"],
                mode="auto",
                is_kiro_cli=classification,
            )

    def test_windows_kiro_with_extra_path_policy_fails_closed(self, monkeypatch):
        """Delegation cannot silently discard Crew-specific path restrictions."""
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "win32")
        monkeypatch.setattr("kiro_crew.sandbox._allow_unsandboxed_exec", lambda: False)
        with (
            patch("kiro_crew.sandbox.detect_backend", return_value="none"),
            patch("kiro_crew.sel.sel", return_value=MagicMock()),
            pytest.raises(sandbox_mod.SandboxUnavailableError),
        ):
            wrap_argv(
                [r"C:\Program Files\Kiro\kiro-cli.exe", "acp"],
                mode="auto",
                is_kiro_cli=True,
                extra_hidden_dirs=(r"C:\secrets",),
            )

    def test_windows_sel_failure_refuses_delegation(self, monkeypatch):
        """An unaudited Windows delegation falls through to fail-closed policy."""
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "win32")
        monkeypatch.setattr("kiro_crew.sandbox._allow_unsandboxed_exec", lambda: False)
        with (
            patch("kiro_crew.sel.sel", side_effect=RuntimeError("audit down")),
            patch("kiro_crew.sandbox.detect_backend", return_value="none") as mock_detect,
            pytest.raises(sandbox_mod.SandboxUnavailableError),
        ):
            wrap_argv(
                [r"C:\Program Files\Kiro\kiro-cli.exe", "acp"],
                mode="auto",
                is_kiro_cli=True,
            )
        mock_detect.assert_called_once_with(config_mode="auto")

    def test_sel_failure_refuses_delegation_falls_back_to_seatbelt(self, tmp_path, monkeypatch):
        """Audit-or-deny: if the SEL audit cannot be written, the delegation
        is refused and the spawn falls back to KiroCrew's own seatbelt."""
        self._write_settings(tmp_path, monkeypatch, '{"sandbox": true}')
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "darwin")
        with (
            patch("kiro_crew.sel.sel", side_effect=RuntimeError("audit down")),
            patch(
                "kiro_crew.sandbox.sandbox_exec_argv",
                return_value=(["sandbox-exec", "-f", "/tmp/p.sb", "kiro-cli", "acp"], "/tmp/p.sb"),
            ) as mock_sb,
        ):
            argv, cleanup = wrap_argv(["kiro-cli", "acp"], mode="auto")
        mock_sb.assert_called_once()
        assert "sandbox-exec" in argv
        assert cleanup == "/tmp/p.sb"

    def test_non_dict_json_is_disabled(self, tmp_path, monkeypatch):
        """Valid-but-non-object JSON must resolve to disabled, not raise."""
        from kiro_crew.sandbox import kiro_internal_sandbox_enabled

        for content in ("[]", '"hello"', "null", "123"):
            self._write_settings(tmp_path, monkeypatch, content)
            assert kiro_internal_sandbox_enabled() is False, content

    def test_symlink_to_sensitive_path_is_disabled(self, tmp_path, monkeypatch):
        """A settings path symlinked into a sensitive location is refused by
        the hooks-routed read and resolves to disabled (never crashes).

        HOME is relocated to tmp_path because is_sensitive_path anchors its
        deny list at the user's home directory."""
        from kiro_crew.sandbox import kiro_internal_sandbox_enabled

        monkeypatch.setenv("HOME", str(tmp_path))
        sensitive = tmp_path / ".aws" / "credentials"
        sensitive.parent.mkdir()
        sensitive.write_text('{"sandbox": true}')
        link = tmp_path / "amazon-internal.json"
        try:
            link.symlink_to(sensitive)
        except OSError as exc:
            if sys.platform == "win32" and getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows host has not granted symlink creation privilege")
            raise
        monkeypatch.setattr("kiro_crew.sandbox._KIRO_INTERNAL_SETTINGS_PATH", str(link))
        assert kiro_internal_sandbox_enabled() is False

    def test_sel_failure_does_not_burn_warn_once_flag(self, tmp_path, monkeypatch, caplog):
        """A SEL-failed attempt falls back to seatbelt WITHOUT consuming the
        warn-once flag; the first real delegation afterwards still warns."""
        import logging

        self._write_settings(tmp_path, monkeypatch, '{"sandbox": true}')
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "darwin")
        monkeypatch.setattr("kiro_crew.sandbox._kiro_delegation_warned", False)

        # First call: SEL down -> seatbelt fallback, no delegation warning.
        with (
            patch("kiro_crew.sel.sel", side_effect=RuntimeError("audit down")),
            patch(
                "kiro_crew.sandbox.sandbox_exec_argv",
                return_value=(["sandbox-exec", "-f", "/tmp/p.sb", "kiro-cli"], "/tmp/p.sb"),
            ),
        ):
            wrap_argv(["kiro-cli", "acp"], mode="auto")
        import kiro_crew.sandbox as sb

        assert sb._kiro_delegation_warned is False

        # Second call: SEL healthy -> delegation proceeds AND warns once.
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
            with patch("kiro_crew.sel.sel", return_value=MagicMock()):
                argv, cleanup = wrap_argv(["kiro-cli", "acp"], mode="auto")
        assert "sandbox-exec" not in argv
        assert cleanup is None
        assert sb._kiro_delegation_warned is True
        assert any("delegating" in r.message for r in caplog.records)


class TestMacOsNestingDetection:
    """macOS Seatbelt cannot nest, so a nesting EPERM is not a host verdict.

    Regression cover for app-backend spawns (Dev Fleet's ``git worktree list``,
    Files' ``git status`` / search) and ~40 gateway-boot MCP probes failing with
    "sandbox unavailable ... no OS-level sandbox backend is available on this
    host" on a macOS host whose ``sandbox-exec`` works perfectly when NOT nested
    — because KiroCrew's own seatbelt had already confined the process tree.

    Every test fixes both gate inputs explicitly rather than inheriting whatever
    the test host happens to be: these assertions must not flip between a
    sandboxed dev machine and an unsandboxed CI runner.
    """

    @patch("kiro_crew.sandbox.detect_backend")
    def test_marker_plus_kernel_confirmation_passes_through(self, mock_detect, monkeypatch):
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: True)
        argv = ["git", "worktree", "list", "--porcelain"]
        with patch("kiro_crew.sel.sel"):
            result, cleanup = wrap_argv(argv, mode="standard")
        assert result == argv
        assert cleanup is None
        # Short-circuits BEFORE detection: a nested sandbox-exec probe necessarily
        # EPERMs, and reading that as a host verdict is the bug this fixes.
        mock_detect.assert_not_called()

    @patch("kiro_crew.sandbox.detect_backend")
    def test_the_passthrough_silently_drops_extra_hidden_dirs(
        self, mock_detect, monkeypatch, tmp_path
    ):
        """A caller's ``extra_hidden_dirs`` is UNENFORCED on the passthrough.

        It is not a bug -- a nested re-wrap is denied by design on both platforms,
        so there is no mount namespace to build and nothing to bind-mask into --
        but it is a fact a caller must not build a security control on, and it is
        invisible from the call site: the wrap returns successfully, and the mask
        it was asked for simply does not exist.

        This bites hardest where it is least visible. Every app backend is spawned
        through ``wrap_argv`` by ``apps/backend.py``, so it runs with this marker
        set, and every spawn IT then wraps takes this branch. Dev Fleet's sync is
        exactly that shape -- it wraps each sync step from inside the sandbox -- so
        a mask an app backend asks for to keep a step away from one of its own paths
        would cover nothing while reading, at the call site, as a control. An app
        backend that needs such a boundary has to get it somewhere other than here:
        the sync runner keeps the synced checkout off its import path with the
        interpreter's own ``-I`` rather than with a mask.

        CHARACTERIZATION, NOT A CONTRACT. If nested confinement ever becomes
        possible, this test is one of the things that should change WITH it -- it
        records what the passthrough does today so a caller cannot be misled by it,
        and it is not an argument for keeping the behaviour.
        """
        secret = tmp_path / "provenance"
        secret.mkdir()
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: True)
        with patch("kiro_crew.sel.sel"):
            result, cleanup = wrap_argv(
                ["/usr/bin/npm", "ci"], mode="strict", extra_hidden_dirs=(str(secret),)
            )

        # No launcher script, so nothing exists that COULD carry a bind-mask...
        assert cleanup is None
        # ...and the path appears nowhere in what will actually be executed.
        assert not any(str(secret) in arg for arg in result)
        assert result[-2:] == ["/usr/bin/npm", "ci"]
        mock_detect.assert_not_called()

    @patch("kiro_crew.sandbox.detect_backend", return_value="none")
    def test_forged_marker_without_kernel_confirmation_is_refused(
        self, mock_detect, monkeypatch
    ):
        # The kernel is authoritative: a marker on a process the kernel says is
        # NOT sandboxed can only have been forged or inherited into an unconfined
        # process, so it must not open the passthrough.
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: False)
        monkeypatch.setattr(sandbox_mod, "kiro_internal_sandbox_enabled", lambda: False)
        monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: False)
        sandbox_mod._last_unshare_failure = (False, "EPERM: kernel refuses userns", "")
        with pytest.raises(RuntimeError, match="Sandbox backend unavailable"):
            wrap_argv(["kiro-cli", "acp"], mode="strict")
        mock_detect.assert_called_once()

    @patch("kiro_crew.sandbox.detect_backend")
    def test_unanswerable_kernel_probe_still_honours_marker(self, mock_detect, monkeypatch):
        # "Cannot answer" is not "not sandboxed". A missing symbol / ABI change
        # must not retroactively invalidate a marker the Linux path honours
        # unconditionally — that would brick in-sandbox spawns wherever the probe
        # is unavailable.
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: None)
        with patch("kiro_crew.sel.sel"):
            result, _ = wrap_argv(["kiro-cli", "acp"], mode="strict")
        assert result == ["kiro-cli", "acp"]
        mock_detect.assert_not_called()

    @patch("kiro_crew.sandbox.detect_backend", return_value="none")
    def test_foreign_outer_sandbox_fails_closed_with_actionable_guidance(
        self, mock_detect, monkeypatch
    ):
        # Nested under a sandbox KiroCrew did NOT create (no marker): its profile
        # is unidentifiable and its environment was never scrubbed by us, so
        # passthrough is refused. The error must still name the REAL cause and a
        # remedy that keeps isolation, not repeat the false "this host has no
        # sandbox backend" claim.
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: True)
        monkeypatch.setattr(sandbox_mod, "kiro_internal_sandbox_enabled", lambda: False)
        monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: False)
        sandbox_mod._last_unshare_failure = (False, "sandbox_apply: Operation not permitted", "")
        with pytest.raises(RuntimeError) as ei:
            wrap_argv(["git", "status"], mode="standard")
        msg = str(ei.value)
        assert "NOT broken" in msg
        assert "amazon-internal.json" in msg
        # Must not steer the operator at the blunt flag that disables isolation
        # even where no sandbox exists at all.
        assert "sandbox_allow_unsandboxed_exec=true" not in msg

    @patch("kiro_crew.sandbox.detect_backend", return_value="none")
    def test_not_nested_still_fails_closed(self, mock_detect, monkeypatch):
        # The passthrough must not weaken the fail-closed guarantee on a host that
        # genuinely has no backend.
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: False)
        monkeypatch.setattr(sandbox_mod, "kiro_internal_sandbox_enabled", lambda: False)
        monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: False)
        sandbox_mod._last_unshare_failure = (False, "EPERM: kernel refuses userns", "")
        with pytest.raises(RuntimeError, match="Sandbox backend unavailable"):
            wrap_argv(["kiro-cli", "acp"], mode="standard")

    @patch("kiro_crew.sandbox.detect_backend", return_value="sandbox-exec")
    def test_available_backend_still_wraps(self, mock_detect, monkeypatch):
        # With no marker, a working backend must still wrap — the passthrough is
        # not a bypass. Uses a NON-kiro argv so the kiro-delegation path does not
        # intercept.
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        monkeypatch.setattr(sandbox_mod, "_macos_sandbox_state", lambda: True)
        with patch("kiro_crew.sandbox.sandbox_exec_argv") as mock_sb:
            mock_sb.return_value = (["sandbox-exec", "-f", "/tmp/p.sb", "git"], "/tmp/p.sb")
            wrap_argv(["git", "status"], mode="standard")
        mock_sb.assert_called_once()

    def test_kernel_state_is_none_off_darwin(self, monkeypatch):
        # Linux namespace isolation must be unaffected by the macOS-only probe,
        # and "not darwin" is unanswerable rather than "not sandboxed".
        monkeypatch.setattr(sandbox_mod.sys, "platform", "linux")
        sandbox_mod._macos_sandbox_state.cache_clear()
        try:
            assert sandbox_mod._macos_sandbox_state() is None
            assert sandbox_mod._inside_macos_sandbox() is False
        finally:
            sandbox_mod._macos_sandbox_state.cache_clear()

    def test_kernel_state_is_none_when_probe_raises(self, monkeypatch):
        # An unanswerable probe is None, NOT False — False is a positive claim
        # that would veto a legitimate marker.
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_mod.ctypes, "CDLL", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        )
        sandbox_mod._macos_sandbox_state.cache_clear()
        try:
            assert sandbox_mod._macos_sandbox_state() is None
        finally:
            sandbox_mod._macos_sandbox_state.cache_clear()


@pytest.mark.usefixtures("systemd_run_resolvable")
class TestAgentSliceMemoryHigh:
    """_ensure_agent_slice_memory_high() reconciles the AGGREGATE MemoryHigh
    ceiling on kirocrew-agents.slice — bounding the SUM of all concurrent agent
    scopes, which the per-scope MemoryMax cannot (N scopes each under their own
    65% cap can still livelock a swapless host together)."""

    def test_spawn_path_schedules_reconciliation_off_thread(self):
        # cgroup_scope_argv runs on the gateway event loop, so the
        # reconciliation (config read + systemctl subprocess) must happen in
        # a worker thread, never inline on the caller's thread.
        import kiro_crew.sandbox as sb

        self._reset()
        calling_thread: list = []
        done = threading.Event()

        def record_thread() -> None:
            calling_thread.append(threading.current_thread())
            done.set()

        try:
            with patch(
                "kiro_crew.sandbox._ensure_agent_slice_memory_high",
                side_effect=record_thread,
            ):
                sb._reconcile_slice_memory_high_off_thread()
                assert done.wait(5.0), "reconciliation thread never ran"
            assert calling_thread[0] is not threading.current_thread()
        finally:
            self._restore()

    def test_schedule_during_reconciliation_queues_and_applies(self):
        import kiro_crew.sandbox as sb

        self._reset()
        first_started = threading.Event()
        release = threading.Event()
        calls: list = []

        def slow_reconcile() -> None:
            calls.append(1)
            if len(calls) == 1:
                first_started.set()
                release.wait(5.0)

        try:
            with patch(
                "kiro_crew.sandbox._ensure_agent_slice_memory_high",
                side_effect=slow_reconcile,
            ):
                sb._reconcile_slice_memory_high_off_thread()
                assert first_started.wait(5.0)
                # A schedule landing mid-reconciliation is NOT dropped: the
                # second worker queues on the mutex and re-reconciles from
                # live config after the first releases it.
                sb._reconcile_slice_memory_high_off_thread()
                release.set()
                for _ in range(200):
                    if len(calls) == 2:
                        break
                    time.sleep(0.01)
            assert len(calls) == 2
        finally:
            self._restore()

    def test_thread_start_failure_disarms_without_aborting_the_spawn(self) -> None:
        # Thread exhaustion on the spawn path must not raise (aborting the
        # agent spawn) nor retain the in-flight slot forever.
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            with patch(
                "kiro_crew.sandbox.threading.Thread",
                side_effect=RuntimeError("can't start new thread"),
            ):
                sb._reconcile_slice_memory_high_off_thread()  # must not raise
            assert sb._SLICE_MEMHIGH_DISABLED is True
            # The serialization mutex is untouched by the failure path:
            assert sb._SLICE_MEMHIGH_MUTEX.acquire(blocking=False)
            sb._SLICE_MEMHIGH_MUTEX.release()
        finally:
            self._restore()

    def _reset(self):
        import kiro_crew.sandbox as sb

        sb._SLICE_MEMHIGH_APPLIED = None
        sb._SLICE_MEMHIGH_DISABLED = False
        sb._SLICE_MEMHIGH_EVENTS_SEEN = None
        sb._SLICE_MEMHIGH_CLIMB_WARNED = False

    def _restore(self):
        # Leave the module disarmed, matching the autouse conftest fixture's
        # in-test state (it restores its own snapshot afterwards).
        import kiro_crew.sandbox as sb

        sb._SLICE_MEMHIGH_APPLIED = None
        sb._SLICE_MEMHIGH_DISABLED = True
        sb._SLICE_MEMHIGH_EVENTS_SEEN = None
        sb._SLICE_MEMHIGH_CLIMB_WARNED = False

    def test_applies_host_default_via_systemctl_runtime(self):
        # The slice is UID-global (shared by live/dev/pod gateways), so the
        # ceiling is deliberately NOT config-driven: always the host-derived
        # default, so no single instance can lift the others' protection.
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            with (
                patch("kiro_crew.sandbox._default_slice_memory_high_mb", return_value=2048),
                patch("kiro_crew.sandbox.platform_compat.trusted_system_bin", return_value="/usr/bin/systemctl"),
                patch("kiro_crew.sandbox.subprocess.run") as run,
            ):
                run.return_value = MagicMock(returncode=0, stderr="", stdout="")
                sb._ensure_agent_slice_memory_high()
            assert run.call_count == 1
            argv = run.call_args[0][0]
            assert argv == [
                "/usr/bin/systemctl",
                "--user",
                "set-property",
                "--runtime",
                "kirocrew-agents.slice",
                "MemoryHigh=2048M",
            ]
            assert sb._SLICE_MEMHIGH_APPLIED == "2048M"
        finally:
            self._restore()

    def test_steady_state_is_a_noop(self):
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            with (
                patch(
                    "kiro_crew.sandbox._default_slice_memory_high_mb",
                    return_value=2048,
                ),
                patch("kiro_crew.sandbox.platform_compat.trusted_system_bin", return_value="/usr/bin/systemctl"),
                patch("kiro_crew.sandbox.subprocess.run") as run,
            ):
                run.return_value = MagicMock(returncode=0, stderr="", stdout="")
                sb._ensure_agent_slice_memory_high()
                sb._ensure_agent_slice_memory_high()  # same value -> no spawn
                assert run.call_count == 1
            assert sb._SLICE_MEMHIGH_APPLIED == "2048M"
        finally:
            self._restore()

    def test_failure_warns_once_and_disarms(self, caplog):
        import logging

        import kiro_crew.sandbox as sb

        self._reset()
        try:
            with (
                patch("kiro_crew.sandbox._default_slice_memory_high_mb", return_value=2048),
                patch("kiro_crew.sandbox.platform_compat.trusted_system_bin", return_value="/usr/bin/systemctl"),
                patch("kiro_crew.sandbox.subprocess.run") as run,
            ):
                run.return_value = MagicMock(returncode=1, stderr="Failed to set", stdout="")
                with caplog.at_level(logging.WARNING):
                    sb._ensure_agent_slice_memory_high()
                    sb._ensure_agent_slice_memory_high()  # disarmed -> no retry
                assert run.call_count == 1
            sec = [r for r in caplog.records if "SECURITY" in r.getMessage()]
            assert len(sec) == 1
            assert "MemoryHigh" in sec[0].getMessage()
            assert sb._SLICE_MEMHIGH_DISABLED is True
            assert sb._SLICE_MEMHIGH_APPLIED is None
        finally:
            self._restore()

    def test_missing_systemctl_warns_and_disarms_without_raising(self, caplog):
        import logging

        import kiro_crew.sandbox as sb

        self._reset()
        try:
            with (
                patch("kiro_crew.sandbox._default_slice_memory_high_mb", return_value=2048),
                patch("kiro_crew.sandbox.platform_compat.trusted_system_bin", return_value=None),
            ):
                with caplog.at_level(logging.WARNING):
                    sb._ensure_agent_slice_memory_high()
            assert sb._SLICE_MEMHIGH_DISABLED is True
            assert any("SECURITY" in r.getMessage() for r in caplog.records)
        finally:
            self._restore()

    def test_cgroup_scope_argv_reconciles_when_available(self):
        import kiro_crew.sandbox as sb

        sb._CGROUP_SCOPE_PROBE = None
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 50, 0),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=False),
                patch(
                    "kiro_crew.sandbox._reconcile_slice_memory_high_off_thread"
                ) as ensure,
            ):
                out = sb.cgroup_scope_argv(["kiro-cli", "chat"])
            ensure.assert_called_once_with()
            # The slice is a per-instance child of the aggregate parent (see
            # _agents_slice_name), so match the parent stem rather than an exact
            # name: the reconciliation this test is about still targets the
            # parent, which is what the ceiling is applied to.
            assert any(
                a.startswith("--slice=kirocrew-agents") and a.endswith(".slice") for a in out
            ), out
        finally:
            sb._CGROUP_SCOPE_PROBE = None

    def test_cgroup_scope_argv_skips_reconcile_when_unavailable(self):
        """No delegation -> passthrough argv AND no systemctl side effect (the
        non-Linux / no-delegation degradation path)."""
        import kiro_crew.sandbox as sb

        sb._CGROUP_SCOPE_PROBE = None
        sb._CGROUP_WARNED = False
        try:
            with (
                patch(
                    "kiro_crew.sandbox._probe_cgroup_scope",
                    return_value=(False, "not Linux"),
                ),
                patch("kiro_crew.sandbox._ensure_agent_slice_memory_high") as ensure,
            ):
                out = sb.cgroup_scope_argv(["git", "status"])
            ensure.assert_not_called()
            assert out == ["git", "status"]
        finally:
            sb._CGROUP_SCOPE_PROBE = None
            sb._CGROUP_WARNED = False

    @_POSIX_ONLY
    def test_default_is_host_proportional_with_fallback(self):
        import kiro_crew.sandbox as sb

        sixteen_g = 16 * 1024**3
        with patch("os.sysconf", side_effect=lambda n: sixteen_g // 4096 if "PHYS" in n else 4096):
            mb = sb._default_slice_memory_high_mb()
        assert mb == int(sixteen_g * sb._SLICE_MEMORY_HIGH_FRACTION) // (1024 * 1024)
        with patch("os.sysconf", side_effect=OSError("no sysconf")):
            assert sb._default_slice_memory_high_mb() == sb._SLICE_FALLBACK_MEMORY_HIGH_MB
        with patch("os.sysconf", return_value=0):
            assert sb._default_slice_memory_high_mb() == sb._SLICE_FALLBACK_MEMORY_HIGH_MB

    def test_worker_checks_pressure_after_reconcile(self):
        # Throttle visibility rides the reconcile worker: every scheduled
        # reconcile also reads memory.events, including the steady state
        # where the MemoryHigh apply itself is a no-op string compare.
        import kiro_crew.sandbox as sb

        self._reset()
        done = threading.Event()
        try:
            with (
                patch("kiro_crew.sandbox._ensure_agent_slice_memory_high") as ensure,
                patch(
                    "kiro_crew.sandbox._check_slice_memory_pressure",
                    side_effect=lambda: done.set(),
                ) as check,
            ):
                sb._reconcile_slice_memory_high_off_thread()
                assert done.wait(5.0), "pressure check never ran"
            ensure.assert_called_once_with()
            check.assert_called_once_with()
        finally:
            self._restore()

    def test_pressure_warns_once_per_climbing_episode(self, caplog):
        # A sustained throttling episode logs ONCE (at the first observed
        # increase), stays silent while the counter keeps climbing, and
        # re-arms only after an observation finds the counter stable.
        import logging

        import kiro_crew.sandbox as sb

        self._reset()
        try:
            readings = iter([0, 3, 5, 5, 7])
            with patch(
                "kiro_crew.sandbox._slice_memory_events_high",
                side_effect=lambda: next(readings),
            ):
                with caplog.at_level(logging.WARNING):
                    sb._check_slice_memory_pressure()  # 0: baseline, silent
                    sb._check_slice_memory_pressure()  # 0 -> 3: warns
                    sb._check_slice_memory_pressure()  # 3 -> 5: same episode
                    sb._check_slice_memory_pressure()  # 5 == 5: episode ends
                    sb._check_slice_memory_pressure()  # 5 -> 7: warns again
            warns = [r for r in caplog.records if "memory.events" in r.getMessage()]
            assert len(warns) == 2
            assert "0 -> 3" in warns[0].getMessage()
            assert "5 -> 7" in warns[1].getMessage()
        finally:
            self._restore()

    def test_pressure_first_read_baselines_without_warning(self, caplog):
        # The counter is monotonic for the slice cgroup's lifetime, so a
        # nonzero FIRST read may predate this process — never warn on it.
        import logging

        import kiro_crew.sandbox as sb

        self._reset()
        try:
            with patch("kiro_crew.sandbox._slice_memory_events_high", return_value=42):
                with caplog.at_level(logging.WARNING):
                    sb._check_slice_memory_pressure()
            assert not [r for r in caplog.records if "memory.events" in r.getMessage()]
            assert sb._SLICE_MEMHIGH_EVENTS_SEEN == 42
        finally:
            self._restore()

    def test_pressure_counter_reset_rebaselines_silently(self, caplog):
        # systemd releases an empty slice; recreation resets memory.events to
        # zero. A DECREASE is that reset, not a climb: re-baseline, close any
        # open episode, and warn again only on a genuine later increase.
        import logging

        import kiro_crew.sandbox as sb

        self._reset()
        sb._SLICE_MEMHIGH_EVENTS_SEEN = 5
        sb._SLICE_MEMHIGH_CLIMB_WARNED = True
        try:
            with caplog.at_level(logging.WARNING):
                with patch("kiro_crew.sandbox._slice_memory_events_high", return_value=2):
                    sb._check_slice_memory_pressure()
                assert sb._SLICE_MEMHIGH_EVENTS_SEEN == 2
                assert sb._SLICE_MEMHIGH_CLIMB_WARNED is False
                with patch("kiro_crew.sandbox._slice_memory_events_high", return_value=4):
                    sb._check_slice_memory_pressure()
            warns = [r for r in caplog.records if "memory.events" in r.getMessage()]
            assert len(warns) == 1
            assert "2 -> 4" in warns[0].getMessage()
        finally:
            self._restore()

    def test_pressure_unreadable_is_silent_and_keeps_state(self, caplog):
        # An unreadable memory.events (slice not materialized, no cgroup v2,
        # macOS/Windows) must neither warn nor clobber the baseline.
        import logging

        import kiro_crew.sandbox as sb

        self._reset()
        sb._SLICE_MEMHIGH_EVENTS_SEEN = 5
        try:
            with patch("kiro_crew.sandbox._slice_memory_events_high", return_value=None):
                with caplog.at_level(logging.WARNING):
                    sb._check_slice_memory_pressure()
            assert not [r for r in caplog.records if "memory.events" in r.getMessage()]
            assert sb._SLICE_MEMHIGH_EVENTS_SEEN == 5
        finally:
            self._restore()

    def test_slice_memory_events_high_reads_counter(self, tmp_path):
        import kiro_crew.sandbox as sb

        evt = tmp_path / "memory.events"
        evt.write_text("low 0\nhigh 42\nmax 1\noom 0\noom_kill 0\n", encoding="utf-8")
        with patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=tmp_path):
            assert sb._slice_memory_events_high() == 42
        with patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=None):
            assert sb._slice_memory_events_high() is None
        empty = tmp_path / "no-events"
        empty.mkdir()
        with patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=empty):
            assert sb._slice_memory_events_high() is None
        evt.write_text("low 0\nhigh notanumber\n", encoding="utf-8")
        with patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=tmp_path):
            assert sb._slice_memory_events_high() is None

    @pytest.mark.skipif(sys.platform != "linux", reason="the resolver is Linux-only")
    def test_slice_memory_events_high_resolves_dash_hierarchy(self, tmp_path):
        """Regression: on the standard systemd layout the agents slice nests
        under kirocrew.slice (dash-hierarchy), NOT directly under
        user@<uid>.service. The reader must find memory.events through the
        real resolver on that layout — a hardcoded flat path used to miss it,
        silently disabling the throttle warning."""
        import kiro_crew.sandbox as sb

        nested = tmp_path / "kirocrew.slice" / sb._CGROUP_AGENTS_SLICE
        nested.mkdir(parents=True)
        (nested / "memory.events").write_text(
            "low 0\nhigh 7\nmax 0\noom 0\noom_kill 0\n", encoding="utf-8"
        )
        with patch.object(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path)):
            assert sb._agents_slice_cgroup_dir() == nested
            assert sb._slice_memory_events_high() == 7


class TestSandboxExecArgvPinsInnerConfiner:
    """SECURITY (PR #2602 macOS hop): the outer ``env`` (argv[0]) is pinned to an
    absolute path at the spawn site, but ``env`` then resolves the NEXT bare name
    -- ``sandbox-exec``, the inner confiner -- through the PATH it is handed, which
    may carry a per-server config PATH overlay. ``sandbox_exec_argv`` must emit
    ``sandbox-exec`` as an absolute path so a hostile PATH cannot redirect it.

    Pure argv-construction assertions: no macOS dependency, so they run on Linux
    CI (where ``sandbox-exec`` is absent and the canonical fallback applies).
    """

    def test_sandbox_exec_token_is_absolute(self):
        argv, cleanup = sandbox_exec_argv(["echo", "hi"], sandbox_level="standard")
        try:
            # The bare name must NOT appear as its own argv token.
            assert "sandbox-exec" not in argv
            # An absolute path ending in /sandbox-exec is present instead, and it
            # is immediately followed by the ``-f <profile>`` flags.
            sb = next(a for a in argv if a.endswith("sandbox-exec"))
            assert os.path.isabs(sb), f"sandbox-exec must be absolute, got {sb!r}"
            assert argv[argv.index(sb) + 1] == "-f"
        finally:
            if cleanup:
                os.unlink(cleanup)

    def test_cgroup_wrapper_is_absolute_or_refused(self):
        """``cgroup_scope_argv`` pins the ``systemd-run`` IT prepends.

        Callers hand the result to spawns whose ``env`` may carry a config-declared
        PATH, and CPython resolves a slash-less argv[0] through that PATH -- so a
        bare wrapper here is an exec-hijack channel that runs before ``--scope``
        confines anything. When the wrapper is not in a trusted system directory
        the function degrades to no cgroup ceiling (its documented fail-open) rather
        than emitting an unpinned name: losing a DoS ceiling beats gaining an
        arbitrary-exec channel.
        """
        with patch.object(sandbox_mod, "_probe_cgroup_scope", return_value=(True, "")), patch.object(
            sandbox_mod, "_reconcile_slice_memory_high_off_thread"
        ), patch.object(sandbox_mod, "_cpu_controller_delegated", return_value=False):
            with patch.object(
                sandbox_mod.platform_compat, "trusted_system_bin", return_value="/usr/bin/systemd-run"
            ):
                pinned = sandbox_mod.cgroup_scope_argv(["kiro-cli", "chat"])
            assert pinned[0] == "/usr/bin/systemd-run"
            assert "systemd-run" not in pinned

            # Unresolvable -> no wrapper at all, never a bare name.
            with patch.object(
                sandbox_mod.platform_compat, "trusted_system_bin", return_value=None
            ):
                unwrapped = sandbox_mod.cgroup_scope_argv(["kiro-cli", "chat"])
            assert unwrapped == ["kiro-cli", "chat"]

    def test_outer_env_token_is_absolute(self):
        """argv[0] runs FIRST of all, so it is pinned for the same reason.

        It is resolved through ``trusted_system_bin`` (fixed system directories,
        PATH ignored) rather than the gateway's PATH, which can legitimately lead
        with agent-writable directories.
        """
        argv, cleanup = sandbox_exec_argv(["echo", "hi"], sandbox_level="standard")
        try:
            assert argv[0] != "env"
            assert os.path.isabs(argv[0]), f"outer env must be absolute, got {argv[0]!r}"
            assert os.path.basename(argv[0]) == "env"
        finally:
            if cleanup:
                os.unlink(cleanup)

    def test_falls_back_to_canonical_paths_when_not_resolvable(self):
        """When a wrapper is not in a trusted system directory (e.g. ``sandbox-exec``
        on Linux CI), the canonical macOS location is used -- never a bare name a
        PATH overlay could redirect."""
        with patch.object(sandbox_mod.platform_compat, "trusted_system_bin", return_value=None):
            argv, cleanup = sandbox_exec_argv(["echo", "hi"], sandbox_level="standard")
        try:
            assert argv[0] == "/usr/bin/env"
            assert "/usr/bin/sandbox-exec" in argv
            assert "sandbox-exec" not in argv
            assert "env" not in argv
        finally:
            if cleanup:
                os.unlink(cleanup)
