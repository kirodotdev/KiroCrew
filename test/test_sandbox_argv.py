"""Tests for kiro_crew.sandbox: Kiro's internal-sandbox delegation, macOS nesting, the
agents slice's MemoryHigh, and the absolute paths the wrappers are pinned to.
``test_sandbox_argv_profile.py`` holds backend detection, ``wrap_argv``'s routing and the
Seatbelt profile; ``test_sandbox_argv_launcher_plan.py`` what the namespace plan seals and
how the launcher program applies it; ``test_sandbox_argv_spawn.py`` the launcher run for
real, the backends' argv and the spawn helpers; ``test_sandbox_argv_resource_ceilings.py``
the rlimit preexecs, the cgroup scope and the agents slice. The fixtures and helpers they
share are in ``sandbox_argv_helpers.py``.
"""

from __future__ import annotations

import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
import sandbox_argv_helpers

import kiro_crew.sandbox as sandbox_mod
from kiro_crew.sandbox import sandbox_exec_argv, wrap_argv
from kiro_crew.testing.wait import wait_until

# Several tests spawn real child interpreters (subprocess.run([sys.executable, ...]));
# pin the module to a dedicated xdist worker so concurrent cold-starts under -n auto
# don't starve each other / blow the 30s timeout. Requires --dist loadgroup.
pytestmark = [
    pytest.mark.xdist_group(name="subprocess_spawn"),
    # This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
    # so the shared macOS handle caches are put back after each one (see conftest.py).
    pytest.mark.usefixtures("fresh_darwin_libc_handles"),
]

# The shared fixtures and helpers, bound here by name: pytest registers the fixtures
# from this namespace.
_POSIX_ONLY = sandbox_argv_helpers._POSIX_ONLY
clean_backend = sandbox_argv_helpers.clean_backend
systemd_run_resolvable = sandbox_argv_helpers.systemd_run_resolvable


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
            result = wrap_argv(["python3", "-m", "worker"], mode="auto")
        mock_sb.assert_called_once()
        assert mock_sb.call_args.args[0] == ["python3", "-m", "worker"]
        assert result == (["sandbox-exec", "python3"], "/tmp/p.sb")

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
            result = wrap_argv(["kiro-cli", "acp"], mode="auto")
        mock_sb.assert_called_once()
        assert mock_sb.call_args.args[0] == ["kiro-cli", "acp"]
        assert result == (["sandbox-exec", "kiro-cli"], "/tmp/p.sb")

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
            argv, cleanup = wrap_argv(["kiro-cli", "acp"], mode="auto")
        mock_ns.assert_called_once()
        assert mock_ns.call_args.args[0] == ["kiro-cli", "acp"]
        assert argv == mock_ns.return_value
        assert cleanup == "/tmp/launcher.py"

    def test_windows_explicit_kiro_backend_delegates_before_backend_probe(self, monkeypatch):
        """Fresh Windows installs use the positively identified Kiro sandbox."""
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "win32")
        launch = r"C:\Program Files\Kiro\kiro-cli.exe"
        with (
            patch("kiro_crew.sel.sel", return_value=MagicMock()),
            patch("kiro_crew.sandbox.detect_backend") as mock_detect,
            patch(
                "kiro_crew.sandbox.kiro_internal_sandbox_enabled",
                side_effect=AssertionError("Windows delegation must not depend on macOS settings"),
            ),
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
    def test_forged_marker_without_kernel_confirmation_is_refused(self, mock_detect, monkeypatch):
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
            result = wrap_argv(["git", "status"], mode="standard")
        mock_sb.assert_called_once()
        assert mock_sb.call_args.args[0] == ["git", "status"]
        assert result == (["sandbox-exec", "-f", "/tmp/p.sb", "git"], "/tmp/p.sb")

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
                wait_until(lambda: len(calls) == 2, describe=lambda: f"reconcile calls: {calls}")
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
                patch(
                    "kiro_crew.sandbox.platform_compat.trusted_system_bin",
                    return_value="/usr/bin/systemctl",
                ),
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
                patch(
                    "kiro_crew.sandbox.platform_compat.trusted_system_bin",
                    return_value="/usr/bin/systemctl",
                ),
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
                patch(
                    "kiro_crew.sandbox.platform_compat.trusted_system_bin",
                    return_value="/usr/bin/systemctl",
                ),
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
                patch("kiro_crew.sandbox._reconcile_slice_memory_high_off_thread") as ensure,
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

        evt = tmp_path / "memory.events.local"
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
        user@<uid>.service. The reader must find memory.events.local through the
        real resolver on that layout — a hardcoded flat path would miss it,
        silently disabling the throttle warning."""
        import kiro_crew.sandbox as sb

        nested = tmp_path / "kirocrew.slice" / sb._CGROUP_AGENTS_SLICE
        nested.mkdir(parents=True)
        (nested / "memory.events.local").write_text(
            "low 0\nhigh 7\nmax 0\noom 0\noom_kill 0\n", encoding="utf-8"
        )
        with patch.object(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path)):
            assert sb._agents_slice_cgroup_dir() == nested
            assert sb._slice_memory_events_high() == 7


class TestSandboxExecArgvPinsInnerConfiner:
    """SECURITY (macOS hop): the outer ``env`` (argv[0]) is pinned to an
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
        with (
            patch.object(sandbox_mod, "_probe_cgroup_scope", return_value=(True, "")),
            patch.object(sandbox_mod, "_reconcile_slice_memory_high_off_thread"),
            patch.object(sandbox_mod, "_cpu_controller_delegated", return_value=False),
        ):
            with patch.object(
                sandbox_mod.platform_compat,
                "trusted_system_bin",
                return_value="/usr/bin/systemd-run",
            ):
                pinned = sandbox_mod.cgroup_scope_argv(["kiro-cli", "chat"])
            assert pinned[0] == "/usr/bin/systemd-run"
            assert "systemd-run" not in pinned

            # Unresolvable -> no wrapper at all, never a bare name.
            with patch.object(sandbox_mod.platform_compat, "trusted_system_bin", return_value=None):
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
