"""Tests for kiro_crew.sandbox: wrap_argv, the backends' argv, plans, profiles, env scrubbing.

The Linux namespace launcher is tested at its two interfaces. What a spawn masks, seals
and re-opens is read off the :class:`~kiro_crew.sandbox_plan.ConfinementPlan` that
``sandbox._spawn_plan`` builds for it; what the launcher program then DOES with that plan
is driven through the stages of ``kiro_crew.sandbox_launcher_program`` with the shared
stand-in libc from ``test_sandbox_launcher_program``, on a tree under ``tmp_path``. The
rendered program itself is run end to end where it can be: the stdlib-shadowing guard,
and the seccomp broadcast filter on a host with user namespaces. The macOS Seatbelt
profile is asserted on the rule text its renderer emits.
"""

from __future__ import annotations

import ast
import asyncio
import errno
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from test_sandbox_launcher_program import CoveringLibc, RecordingLibc, launch, payload, refusal

import kiro_crew.sandbox as sandbox_mod
from kiro_crew import sandbox_launcher
from kiro_crew import sandbox_launcher_program as launcher_program
from kiro_crew import sandbox_plan
from kiro_crew.sandbox import (
    _CC_FILES,
    _SENSITIVE_ENV_PREFIXES,
    _STRICT_DIRS,
    _build_launcher_script,
    _build_seatbelt_profile,
    _resolve_agent_executable,
    _ssh_supports_accept_new,
    _voice_runtime_parent_paths,
    _voice_runtime_sandbox_paths,
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
# Windows CI lane runs them, but they pass in the full POSIX suite.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="_build_launcher_script uses POSIX-only os.getuid (#2041)",
)

# The launcher program's mount stages pin each target through ``O_PATH`` and address it
# as ``/proc/self/fd/<n>``, which only Linux has, so a test that drives them for real
# runs there; the plan they are driven from is checked on every POSIX host.
_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the launcher program's mount stages pin through O_PATH and /proc/self/fd",
)

#: The stages ``sandbox_launcher_program.place_masks`` runs.
_MASK_STAGES = (
    "stage_private_windows",
    "seal_readonly",
    "mask_sensitive",
    "apply_carveouts",
    "restore_exposed_files",
    "verify_fail_closed_aliases",
    "mask_sensitive_files",
    "mask_ssh_keys",
)

#: ``mount(2)`` flags, as the kernel defines them.
_MS_RDONLY = 1
_MS_NOSUID = 2
_MS_NODEV = 4
_MS_NOEXEC = 8
_MS_REMOUNT = 32
_MS_BIND = 4096

#: The verdicts the launcher's seccomp program returns.
_SECCOMP_ALLOW = 0x7FFF0000
_SECCOMP_EPERM = 0x00050001
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}


def _payload_under(plan: sandbox_plan.ConfinementPlan, root: Path) -> dict:
    """The launcher payload of *plan*, narrowed to the paths inside *root*.

    The planner also names this host's real credential homes under ``Path.home()``. A
    stage driven in-process here must never mount over those, so every path list keeps
    only the entries inside the test's own tree, in the plan's order, and ``~/.ssh``
    (the real one) is left alone.
    """
    data = sandbox_plan.namespace_payload(plan)
    prefix = str(root).rstrip("/") + "/"
    for key in (
        "sensitive_dirs",
        "private_dirs",
        "readonly_dirs",
        "writable_dirs",
        "sensitive_files",
        "required_mask_targets",
    ):
        data[key] = [path for path in data[key] if path.startswith(prefix)]
    for key in ("sensitive_dir_ids", "private_dir_ids", "mask_occupants"):
        data[key] = {path: ident for path, ident in data[key].items() if path.startswith(prefix)}
    for key in ("expose_files", "fail_closed_file_masks", "crew_home_aliases"):
        data[key] = [entry for entry in data[key] if entry[0].startswith(prefix)]
    data["hide_ssh"] = 0
    data["stand_in_roots"] = []
    return data


def _stage_order(monkeypatch: pytest.MonkeyPatch, run: launcher_program.Launch) -> list[str]:
    """The stages ``place_masks`` runs for *run*, in order, each recorded instead of run."""
    order: list[str] = []
    for name in _MASK_STAGES:
        monkeypatch.setattr(launcher_program, name, lambda _launch, name=name: order.append(name))
    launcher_program.place_masks(run)
    return order


def _bind_targets(libc: RecordingLibc) -> list[str]:
    """The resolved target of every bind a stage made, in order, remounts excluded."""
    return [
        os.path.realpath(call.target_path)
        for call in libc.calls
        if call.flags & _MS_BIND and not call.flags & _MS_REMOUNT
    ]


def _seccomp_verdict(machine: str, nr: int, arg0: int, arch: int | None = None) -> int:
    """What the launcher's seccomp program answers for one syscall, by running it.

    The program is classic BPF over ``struct seccomp_data``: the syscall number at
    offset 0, the audit arch at 4, the instruction pointer at 8 and ``args[0]`` at 16,
    as two little-endian 32-bit words -- the low one at 16, the high one at 20.
    """
    data = struct.pack(
        "<iIQQ",
        nr,
        _AUDIT_ARCH[machine] if arch is None else arch,
        0,
        arg0 & 0xFFFF_FFFF_FFFF_FFFF,
    )
    insns = launcher_program.seccomp_program(machine)
    acc = 0
    pc = 0
    while True:
        code, jt, jf, k = struct.unpack("<HBBI", insns[pc])
        if code == 0x20:  # BPF_LD | BPF_W | BPF_ABS
            (acc,) = struct.unpack_from("<I", data, k)
            pc += 1
        elif code == 0x15:  # BPF_JMP | BPF_JEQ | BPF_K
            pc += 1 + (jt if acc == k else jf)
        elif code == 0x06:  # BPF_RET | BPF_K
            return k
        else:  # pragma: no cover - an opcode the program does not use
            raise AssertionError(f"unexpected BPF opcode {code:#x}")


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

    Pins ``_ssh_supports_accept_new`` at the module seam the namespace plan's host
    facts are read from (``sandbox._live_plan_host``): the real probe runs the host's
    ``ssh -V`` from whichever of the ~30 plan-building tests happens to call it first
    (it is ``lru_cache``d), a host program none of them is about (test-hygiene class
    7). ``True`` is what a modern host answers. ``TestSshSupportsAcceptNew`` still
    exercises the real function through the name it imported, with
    ``subprocess.run`` patched.
    """
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.setattr(
        "kiro_crew.sandbox._KIRO_INTERNAL_SETTINGS_PATH",
        "/nonexistent/kirocrew-test/amazon-internal.json",
    )
    monkeypatch.setattr(sandbox_mod, "_ssh_supports_accept_new", lambda: True)
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
        mock_ns_argv.assert_called_once_with(
            ["kiro-cli"], "strict", strip_python_env=False, forward_ssh_auth_sock=False
        )

    @patch("kiro_crew.sandbox.detect_backend", return_value="sandbox-exec")
    @patch("kiro_crew.sandbox.sandbox_exec_argv")
    def test_sandbox_exec_backend(self, mock_sb_argv, mock_detect):
        mock_sb_argv.return_value = (["sandbox-exec", "-f", "/tmp/p.sb", "kiro-cli"], "/tmp/p.sb")
        result, cleanup = wrap_argv(["kiro-cli"], mode="strict")
        mock_sb_argv.assert_called_once_with(
            ["kiro-cli"], "strict", strip_python_env=False, forward_ssh_auth_sock=False
        )

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


class TestWritableCarveouts:
    """A probe's private TMPDIR must be writable inside the sandbox.

    The MCP probe's TMPDIR lives at ``<data home>/run/mcp-tmp/<probe>``, inside
    the runtime parent both backends seal read-only, so the wrap must carve
    exactly that directory back out — a Bun-packaged server extracts its native
    module into TMPDIR before it can answer the handshake. These tests lock the
    carve-out's two properties: it OPENS the approved directory (emitted after
    the seal — Seatbelt is last-match-wins) and it opens NOTHING else (the
    validator refuses every candidate that would re-open another seal).
    """

    def _relocated_home(self, monkeypatch, tmp_path):
        custom_home = tmp_path / "crew-home"
        custom_home.mkdir()
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: custom_home)
        probe = custom_home / "run" / "mcp-tmp" / "probe-x"
        probe.mkdir(parents=True)
        return custom_home, probe

    @staticmethod
    def _spellings(path) -> list[str]:
        lexical = os.path.normpath(str(path))
        return list(dict.fromkeys((lexical, os.path.realpath(lexical))))

    def test_seatbelt_carveout_allow_lands_after_run_seal(self, monkeypatch, tmp_path):
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        profile = _build_seatbelt_profile("standard", extra_writable_dirs=(str(probe),))
        deny = f'(deny file-write* (subpath "{home / "run"}"))'
        assert deny in profile
        for spelling in self._spellings(probe):
            allow = f'(allow file-write* (subpath "{spelling}"))'
            assert allow in profile
            # Seatbelt is last-match-wins: the allow must come AFTER the seal,
            # or the seal wins and the child's TMPDIR stays unwritable.
            assert profile.index(allow) > profile.index(deny)
        # The subtree's hardlink deny is deliberately NOT re-opened: a scratch
        # dir never needs to mint hardlinks, and the deny is what stops
        # aliasing a sealed inode into the writable window.
        assert "(allow file-link" not in profile
        assert "(allow file-read" not in profile

    @pytest.mark.parametrize(
        "candidate",
        [
            "run",  # the sealed parent itself: contains the voice runtime
            os.path.join("run", "voice-runtime"),  # the hidden runtime root
            "elsewhere",  # outside every carveable parent
            os.path.join("run", "absent"),  # does not exist
        ],
    )
    def test_seatbelt_refuses_unsafe_carveouts(self, monkeypatch, tmp_path, candidate):
        home, _probe = self._relocated_home(monkeypatch, tmp_path)
        (home / "elsewhere").mkdir()
        profile = _build_seatbelt_profile("standard", extra_writable_dirs=(str(home / candidate),))
        assert "(allow file-write*" not in profile

    def test_seatbelt_refuses_relative_carveout(self, monkeypatch, tmp_path):
        self._relocated_home(monkeypatch, tmp_path)
        profile = _build_seatbelt_profile("standard", extra_writable_dirs=("run/mcp-tmp/probe-x",))
        assert "(allow file-write*" not in profile

    @_POSIX_ONLY
    def test_launcher_embeds_validated_carveout(self, monkeypatch, tmp_path):
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        plan = sandbox_mod._spawn_plan("namespace", "standard", extra_writable_dirs=(str(probe),))
        assert plan.writable == tuple(self._spellings(probe))
        assert sandbox_plan.namespace_payload(plan)["writable_dirs"] == self._spellings(probe)
        # The carve-out's remount must clear the seal -- a remount that re-passed
        # MS_RDONLY would silently keep the carve-out read-only -- and must re-assert
        # the bits the kernel locked on the mount the bind inherited, read off that
        # very target.
        statvfs_targets: list[object] = []

        class _Vfs:
            f_flag = (
                getattr(os, "ST_NOSUID", 0)
                | getattr(os, "ST_NODEV", 0)
                | getattr(os, "ST_NOEXEC", 0)
            )

        def _statvfs(target):
            statvfs_targets.append(target)
            return _Vfs()

        monkeypatch.setattr(launcher_program.os, "statvfs", _statvfs)
        locked = (
            (_MS_NOSUID if hasattr(os, "ST_NOSUID") else 0)
            | (_MS_NODEV if hasattr(os, "ST_NODEV") else 0)
            | (_MS_NOEXEC if hasattr(os, "ST_NOEXEC") else 0)
        )
        libc = RecordingLibc()
        launcher_program.apply_carveouts(
            launch(tmp_path, payload(writable_dirs=list(plan.writable)), libc=libc)
        )
        expected = []
        for spelling in plan.writable:
            target = spelling.encode()
            expected += [
                (target, target, _MS_BIND),
                (target, target, _MS_REMOUNT | _MS_BIND | locked),
            ]
        assert [(call.source, call.target, call.flags) for call in libc.calls] == expected
        assert not any(call.flags & _MS_RDONLY for call in libc.calls)
        assert statvfs_targets == [spelling.encode() for spelling in plan.writable]

    @_POSIX_ONLY
    def test_launcher_refuses_unsafe_carveout(self, monkeypatch, tmp_path):
        home, _probe = self._relocated_home(monkeypatch, tmp_path)
        run_dir = str(home / "run")
        plan = sandbox_mod._spawn_plan("namespace", "standard", extra_writable_dirs=(run_dir,))
        assert plan.writable == ()
        # The sealed parent itself holds the hidden voice runtime, which it would re-open.
        refused = [r for r in plan.refusals if r.kind == "carve-out"]
        assert [r.args[0] for r in refused] == [run_dir]
        assert "would be re-opened by it" in refused[0].args[1]

    @_POSIX_ONLY
    def test_launcher_seals_before_carveout_rebind(self, monkeypatch, tmp_path):
        """The READONLY seal must precede the carve-out re-bind.

        Load-bearing ordering: a non-recursive MS_BIND does not replicate
        submounts, so a parent self-bind established AFTER the carve-out would
        mask the carve-out mount entirely -- the writable window vanishes
        silently and the writable-TMPDIR failure returns with no error. The
        mounts themselves are recorded in that order by
        ``test_the_plans_mounts_land_seal_then_hide_then_carve_out``.
        """
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        plan = sandbox_mod._spawn_plan("namespace", "standard", extra_writable_dirs=(str(probe),))
        assert str(home / "run") in plan.readonly
        assert str(probe) in plan.writable
        order = _stage_order(monkeypatch, launch(tmp_path, _payload_under(plan, tmp_path)))
        assert order.index("seal_readonly") < order.index("apply_carveouts")

    @_POSIX_ONLY
    @pytest.mark.parametrize("level", ["strict", "standard", "cc"])
    def test_launcher_seals_before_hiding(self, monkeypatch, tmp_path, level):
        """The READONLY seal must precede the sensitive_dirs hide.

        Same kernel property as the carve-out pin above, in the other
        direction: ``run/voice-runtime`` is hidden and its parent ``run`` is
        sealed, and a non-recursive self-bind of ``run`` issued AFTER the hide
        masks it -- the real leaf becomes readable through the new mount.
        Seal first, then hide ON the sealed parent. The carve-out stays last,
        so the three stages are seal < hide < carve-out. Every tier, since all
        three run the same launcher program.
        """
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        plan = sandbox_mod._spawn_plan("namespace", level, extra_writable_dirs=(str(probe),))
        # The pair this pin exists for is actually planned: the leaf is hidden and its
        # parent is sealed.
        voice_roots = _voice_runtime_sandbox_paths()
        voice_parents = _voice_runtime_parent_paths()
        assert set(plan.sensitive_dirs) >= set(voice_roots)
        assert set(plan.readonly) >= set(voice_parents)
        order = _stage_order(monkeypatch, launch(tmp_path, _payload_under(plan, tmp_path)))
        seal = order.index("seal_readonly")
        hide = order.index("mask_sensitive")
        carve = order.index("apply_carveouts")
        assert seal < hide < carve

    @_LINUX_ONLY
    @pytest.mark.parametrize("level", ["strict", "standard", "cc"])
    def test_the_plans_mounts_land_seal_then_hide_then_carve_out(
        self, monkeypatch, tmp_path, level
    ):
        """The mounts the program places for a plan, in the order the kernel needs.

        The two ordering pins above, at the mount level: the launcher program
        places this tier's plan over this test's tree and the stand-in libc
        records each bind. The sealed ``run`` is bound before the hidden voice
        runtime under it, which is bound before the carve-out, and the leaf's
        contents are gone from its name.
        """
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        plan = sandbox_mod._spawn_plan("namespace", level, extra_writable_dirs=(str(probe),))
        # The voice-runtime paths are cached per data-home spelling, so the directory
        # is created here rather than left to the cache's first fill.
        leaf = home / "run" / "voice-runtime"
        leaf.mkdir(exist_ok=True)
        (leaf / "decoder.bin").write_bytes(b"model")
        libc = CoveringLibc()
        launcher_program.place_masks(launch(tmp_path, _payload_under(plan, tmp_path), libc=libc))
        targets = _bind_targets(libc)
        seal = targets.index(os.path.realpath(home / "run"))
        hide = targets.index(os.path.realpath(leaf))
        carve = targets.index(os.path.realpath(probe))
        assert seal < hide < carve
        assert os.listdir(leaf) == []

    def test_launcher_carveout_mounts_fail_open(self, monkeypatch, tmp_path, capfd):
        """The two carve-out mounts WIDEN access, so they must not route
        through ``_mount_or_die``: a host refusing them keeps the seal
        (pre-carve-out behavior) instead of losing every sandboxed probe."""
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        plan = sandbox_mod._spawn_plan("namespace", "standard", extra_writable_dirs=(str(probe),))
        carve = plan.writable[0]
        for fail_at, what in ((1, "bind"), (2, "remount")):
            libc = RecordingLibc(fail_at=fail_at)
            run = launch(tmp_path, payload(writable_dirs=[carve]), libc=libc)
            assert refusal(launcher_program.apply_carveouts, run) is None
            assert (
                f"sandbox: WARNING -- writable carve-out {what} for {carve} failed "
                f"(errno {errno.EPERM}); continuing with the path sealed"
            ) in capfd.readouterr().err
            # A bind that failed is not followed by its remount.
            assert len(libc.calls) == fail_at

    @_POSIX_ONLY
    def test_launcher_refuses_carveout_inside_unhidden_tree(self, monkeypatch, tmp_path):
        """A caller-re-exposed (``extra_visible_dirs``) tree must still refuse
        a writable window: exposure cancels the hide, not the write seal, so
        the validator's guard set must include the ``unhidden`` entries (the
        ``+ unhidden`` term in the planner is load-bearing)."""
        home, _probe = self._relocated_home(monkeypatch, tmp_path)
        exposed = home / "run" / "exposed-tree"
        inside = exposed / "scratch"
        inside.mkdir(parents=True)
        plan = sandbox_mod._spawn_plan(
            "namespace",
            "standard",
            extra_hidden_dirs=(str(exposed),),
            extra_visible_dirs=(str(exposed),),
            extra_writable_dirs=(str(inside),),
        )
        assert plan.writable == ()
        assert str(exposed) not in plan.sensitive_dirs
        assert [c.path for c in plan.cancellations] == [str(exposed)]
        assert [r.args for r in plan.refusals if r.kind == "carve-out"] == [
            (str(inside), f"it lies inside sealed subtree {str(exposed)!r}")
        ]

    def test_symlinked_data_home_emits_both_spellings(self, monkeypatch, tmp_path):
        """A symlinked data home (supported) must carve BOTH spellings:
        Seatbelt rules are path-based and see each spelling independently."""
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        lexical_home = tmp_path / "linked-home"
        os.symlink(real_home, lexical_home)
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: lexical_home)
        probe = lexical_home / "run" / "mcp-tmp" / "probe-x"
        probe.mkdir(parents=True)
        lexical = os.path.normpath(str(probe))
        canonical = os.path.realpath(lexical)
        assert lexical != canonical  # the premise of the test
        profile = _build_seatbelt_profile("standard", extra_writable_dirs=(str(probe),))
        for spelling in (lexical, canonical):
            assert f'(allow file-write* (subpath "{spelling}"))' in profile

    def test_validator_refuses_dir_inside_hidden_tree(self, monkeypatch, tmp_path):
        """A carve-out under a read-hidden tree must be refused even when a
        (buggy or hostile) caller also names that tree as a carveable parent:
        write access without read access is still a tampering channel."""
        home, _probe = self._relocated_home(monkeypatch, tmp_path)
        hidden = home / "run" / "secrets"
        inside = hidden / "scratch"
        inside.mkdir(parents=True)
        approved, _refusals = sandbox_plan.writable_carveouts(
            (str(inside),),
            sandbox_mod._carveout_probes((str(inside),)),
            subtree_guards=[str(hidden)],
            literal_guards=[],
            carveable_parents=[str(home / "run")],
        )
        assert approved == []


class TestSealedRuntimeParentPredicate:
    """The question asked BEFORE a config-declared dir reaches a child.

    A spec-declared ``TMPDIR`` under ``<data home>/run`` is sealed read-only by
    both backends, and the write carve-out above is validated for self-derived
    scratch only — so the caller's only safe move is to stop honoring the path,
    which it can only do if ``classify_declared_temp_path`` answers honestly.
    Every test asserts the CLASSIFICATION (``None`` = honor; ``sealed`` or
    ``unclassifiable`` = refuse, and why), because the probe names that cause
    in its WARNING and a wrong cause misdirects the operator even when the
    refusal itself is right.
    """

    def _home(self, monkeypatch, tmp_path):
        home = tmp_path / "crew-home"
        (home / "run").mkdir(parents=True)
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: home)
        return home

    def test_declared_path_inside_the_run_parent_is_sealed(self, monkeypatch, tmp_path):
        home = self._home(monkeypatch, tmp_path)
        classify = sandbox_mod.classify_declared_temp_path
        assert classify(str(home / "run" / "custom-tmp")) == "sealed"
        # The parent itself and the managed root under it answer the same way:
        # containment, not a leaf allowlist.
        assert classify(str(home / "run")) == "sealed"
        assert classify(str(home / "run" / "mcp-tmp" / "probe-x")) == "sealed"

    def test_path_outside_the_data_home_is_not_sealed(self, monkeypatch, tmp_path):
        self._home(monkeypatch, tmp_path)
        chosen = tmp_path / "operator-volume" / "tmp"
        chosen.mkdir(parents=True)
        assert sandbox_mod.classify_declared_temp_path(str(chosen)) is None
        # An empty declaration is not a path and must not read as refused.
        assert sandbox_mod.classify_declared_temp_path("") is None

    def test_relative_declaration_is_unclassifiable(self, monkeypatch, tmp_path):
        self._home(monkeypatch, tmp_path)
        assert sandbox_mod.classify_declared_temp_path("run/x") == "unclassifiable"

    @_POSIX_ONLY
    def test_both_spellings_of_a_symlinked_data_home_are_sealed(self, monkeypatch, tmp_path):
        # config_dir() deliberately preserves a supported symlinked data home,
        # and path-based sandbox rules see each spelling independently — so a
        # declaration written either way must be recognised.
        real = tmp_path / "real-home"
        (real / "run").mkdir(parents=True)
        link = tmp_path / "linked-home"
        link.symlink_to(real)
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: link)
        classify = sandbox_mod.classify_declared_temp_path
        assert classify(str(link / "run" / "custom-tmp")) == "sealed"
        assert classify(str(real / "run" / "custom-tmp")) == "sealed"

    @_POSIX_ONLY
    def test_a_symlink_climbing_back_into_the_run_parent_is_sealed(self, monkeypatch, tmp_path):
        # Resolution ORDER is the whole test. `..` must be collapsed by realpath,
        # after symlinks, the way the child's libc will collapse it -- collapsing
        # it lexically first deletes the very symlink it was climbing out of, so
        # a declaration routed through a link back into the seal reads as outside.
        home = self._home(monkeypatch, tmp_path)
        (home / "run" / "inner").mkdir()
        link = tmp_path / "into-run"
        link.symlink_to(home / "run" / "inner")
        # Lexically `<link>/../tmp` collapses to `<tmp_path>/tmp`, which is
        # outside the seal; resolved symlink-first it is `<home>/run/tmp`.
        assert sandbox_mod.classify_declared_temp_path(str(link / ".." / "tmp")) == "sealed"

    @staticmethod
    def _install_case_insensitive_stat(monkeypatch, root: Path) -> None:
        """Model an APFS-style case-insensitive ``stat``/``lstat`` under *root* only.

        The bypass this guards needs a filesystem that answers for a
        differently-cased spelling, which Linux CI cannot create for real. What
        APFS actually does is narrow: the fold lives in NAME LOOKUP, so each
        component resolves case-insensitively for ``stat`` and ``lstat`` alike,
        while ``realpath`` keeps the caller's spelling (it walks with
        ``lstat``/``readlink``, neither of which rewrites a component's case) --
        so the alias never reaches a lexical comparison in canonical form.
        Reproduce exactly that, delegating every path outside *root* to the real
        call so nothing else in the process is disturbed.
        """
        real = {"stat": os.stat, "lstat": os.lstat}
        root_str = str(root)

        def _fold(path: str) -> str | None:
            resolved = os.path.dirname(root_str)
            for part in os.path.relpath(path, resolved).split(os.sep):
                try:
                    entries = os.listdir(resolved)
                except OSError:
                    return None
                match = next((e for e in entries if e.lower() == part.lower()), None)
                if match is None:
                    return None
                resolved = os.path.join(resolved, match)
            return resolved

        def _folding(name: str):
            def fake(path, *args, **kwargs):
                try:
                    return real[name](path, *args, **kwargs)
                except FileNotFoundError:
                    if not isinstance(path, (str, os.PathLike)):
                        raise
                    spelling = os.fspath(path)
                    if not isinstance(spelling, str) or not spelling.startswith(root_str + os.sep):
                        raise
                    folded = _fold(spelling)
                    if folded is None:
                        raise
                    return real[name](folded, *args, **kwargs)

            return fake

        monkeypatch.setattr(os, "stat", _folding("stat"))
        monkeypatch.setattr(os, "lstat", _folding("lstat"))

    def test_a_case_alias_of_the_run_parent_is_sealed(self, monkeypatch, tmp_path):
        # On case-insensitive APFS `<data home>/RUN` and `<data home>/run`
        # are ONE directory, and realpath does not fold the difference -- so a
        # lexical predicate answers "not sealed" for a path the backends seal,
        # which would let the probe honor the declaration and hand the child a
        # read-only TMPDIR.
        home = self._home(monkeypatch, tmp_path)
        (home / "run" / "custom-tmp").mkdir()
        self._install_case_insensitive_stat(monkeypatch, tmp_path)
        classify = sandbox_mod.classify_declared_temp_path
        assert classify(str(home / "RUN" / "custom-tmp")) == "sealed"
        # The parent's own differently-cased spelling answers the same way.
        assert classify(str(home / "Run")) == "sealed"

    def test_a_missing_leaf_under_a_case_aliased_parent_is_sealed(self, monkeypatch, tmp_path):
        # A declared temp normally does NOT exist yet, so the deepest EXISTING
        # ancestor is what carries the identity: `<home>/RUN` folds onto the
        # sealed parent, and nothing below a sealed directory can climb back out.
        home = self._home(monkeypatch, tmp_path)
        self._install_case_insensitive_stat(monkeypatch, tmp_path)
        assert (
            sandbox_mod.classify_declared_temp_path(str(home / "RUN" / "not-created-yet" / "tmp"))
            == "sealed"
        )

    def test_a_case_alias_outside_the_run_parent_is_still_not_sealed(self, monkeypatch, tmp_path):
        # Identity is the whole test: a differently-cased path that folds onto a
        # directory OUTSIDE the seal must stay honored, or the fix would refuse
        # every operator-chosen temp whose spelling merely resembles the seal.
        home = self._home(monkeypatch, tmp_path)
        (home / "runtime-cache").mkdir()
        self._install_case_insensitive_stat(monkeypatch, tmp_path)
        assert sandbox_mod.classify_declared_temp_path(str(home / "RUNTIME-cache" / "tmp")) is None

    def test_a_real_case_alias_is_sealed_where_the_filesystem_folds_case(
        self, monkeypatch, tmp_path
    ):
        # The same claim against the REAL filesystem, for the platforms that
        # actually fold (APFS, NTFS). Skipped on a case-sensitive CI filesystem,
        # where the aliased spelling is a different directory and nothing seals it.
        home = self._home(monkeypatch, tmp_path)
        if not (home / "RUN").is_dir():
            pytest.skip("test filesystem is case-sensitive; no real case alias to build")
        assert sandbox_mod.classify_declared_temp_path(str(home / "RUN" / "custom-tmp")) == "sealed"

    @_POSIX_ONLY
    def test_the_identity_walk_does_not_follow_a_final_symlink(self, tmp_path):
        # The identity probe runs on paths that came from config text, so it stats
        # NO-FOLLOW: `lstat` never traverses the final component, which is exactly
        # where a planted link could point at a remote or stalling target and turn
        # a local containment question into off-host I/O.
        sealed = tmp_path / "run"
        sealed.mkdir()
        link = tmp_path / "link-into-run"
        link.symlink_to(sealed)
        # The LINK's own identity is what the walk sees, never the sealed
        # directory it points at -- so this spelling answers False here.
        assert not sandbox_mod._identity_within_sealed_parent(str(link), str(sealed))

    @_POSIX_ONLY
    def test_a_symlink_to_the_run_parent_is_still_sealed(self, monkeypatch, tmp_path):
        # No coverage is lost by stating no-follow: symlink resolution lives in
        # the CANONICAL spelling, which `realpath` has already produced by the
        # time the identity walk runs -- so a link INTO the seal is still refused.
        home = self._home(monkeypatch, tmp_path)
        link = tmp_path / "link-into-run"
        link.symlink_to(home / "run")
        assert sandbox_mod.classify_declared_temp_path(str(link)) == "sealed"

    @_POSIX_ONLY
    def test_a_benign_local_symlink_chain_still_resolves(self, monkeypatch, tmp_path):
        # Containment is judged on `realpath`, so a multi-hop LOCAL chain ending
        # outside the seal stays honored, and the same chain ending inside it is
        # refused on the merits -- the chain itself is never a reason to refuse.
        home = self._home(monkeypatch, tmp_path)
        outside = tmp_path / "operator-volume" / "tmp"
        outside.mkdir(parents=True)
        (tmp_path / "b-out").symlink_to(outside)
        (tmp_path / "a-out").symlink_to(tmp_path / "b-out")
        assert sandbox_mod.classify_declared_temp_path(str(tmp_path / "a-out")) is None
        (tmp_path / "b-in").symlink_to(home / "run" / "custom-tmp")
        (tmp_path / "a-in").symlink_to(tmp_path / "b-in")
        assert sandbox_mod.classify_declared_temp_path(str(tmp_path / "a-in")) == "sealed"

    @_POSIX_ONLY
    def test_a_symlink_cycle_is_refused_instead_of_looping(self, monkeypatch, tmp_path):
        # ELOOP guard, owned by the kernel: `realpath(strict=True)` raises on a
        # cycle, and the lenient form would instead hand back the cycle's own
        # link with the remainder appended -- a spelling that says nothing about
        # where the temp really is, which a lexical or identity comparison would
        # then wave through. A path whose canonical form cannot be established is
        # refused, matching every other unverifiable declaration here. It is its
        # OWN cause, not "sealed": a diagnostic that called a cycle sealed would
        # send the operator looking under ``run``.
        self._home(monkeypatch, tmp_path)
        first = tmp_path / "loop-a"
        second = tmp_path / "loop-b"
        first.symlink_to(second)
        second.symlink_to(first)
        assert sandbox_mod.classify_declared_temp_path(str(first)) == "unclassifiable"
        # The cycle need not be the leaf: a declared temp usually does not exist
        # yet, and the component carrying the cycle is then an ANCESTOR of it.
        assert (
            sandbox_mod.classify_declared_temp_path(str(first / "not-created-yet" / "tmp"))
            == "unclassifiable"
        )

    @_POSIX_ONLY
    def test_a_missing_leaf_is_resolved_as_far_as_it_goes(self, monkeypatch, tmp_path):
        # The strict resolve must not turn the ORDINARY case into a refusal: a
        # declared temp that does not exist yet is judged by the deepest existing
        # ancestor, outside the seal honored and inside it sealed.
        home = self._home(monkeypatch, tmp_path)
        outside = tmp_path / "operator-volume"
        outside.mkdir()
        assert sandbox_mod.classify_declared_temp_path(str(outside / "not-yet" / "tmp")) is None
        assert (
            sandbox_mod.classify_declared_temp_path(str(home / "run" / "not-yet" / "tmp"))
            == "sealed"
        )

    @_POSIX_ONLY
    def test_a_posix_double_slash_declaration_is_judged_on_its_merits(self, monkeypatch, tmp_path):
        # `//mnt/data/tmp` is a legal POSIX spelling of `/mnt/data/tmp`, not a
        # UNC share, and no spelling is refused on its own here: outside the seal
        # it is honored; the same spelling INTO the seal is caught on the merits.
        home = self._home(monkeypatch, tmp_path)
        chosen = tmp_path / "operator-volume" / "tmp"
        chosen.mkdir(parents=True)
        assert sandbox_mod.classify_declared_temp_path("/" + str(chosen)) is None
        assert sandbox_mod.classify_declared_temp_path("/" + str(home / "run" / "tmp")) == "sealed"

    @staticmethod
    def _windows_path_semantics(monkeypatch) -> None:
        """Run the classification as Windows, on whichever OS runs the test."""
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "win32")

    def test_on_windows_a_declared_temp_is_honored_because_nothing_seals_run(
        self, monkeypatch, tmp_path
    ):
        # Kiro Crew has no native Windows sandbox backend, so ``<data home>/run``
        # is never sealed for a probe child there and a declared temp under it is
        # writable. The classification therefore answers None for every path on
        # win32 -- the declaration is honored exactly as it was before the check
        # existed -- and nothing is resolved, since on Windows ``realpath`` and
        # ``stat`` OPEN the path they are handed.
        home = self._home(monkeypatch, tmp_path)
        self._windows_path_semantics(monkeypatch)
        resolutions: list[str] = []
        real_realpath = os.path.realpath

        def watched(path, *args, **kwargs):
            resolutions.append(os.fspath(path))
            return real_realpath(path, *args, **kwargs)

        monkeypatch.setattr(os.path, "realpath", watched)
        assert sandbox_mod.classify_declared_temp_path(str(home / "run" / "custom-tmp")) is None
        assert sandbox_mod.classify_declared_temp_path(str(home / "run")) is None
        assert resolutions == []


class TestBuildLauncherScript:
    @_POSIX_ONLY
    def test_strict_script_contains_dirs(self):
        plan = sandbox_mod._spawn_plan("namespace", "strict")
        home = str(Path.home())
        assert os.path.join(home, ".aws") in plan.sensitive_dirs
        assert os.path.join(home, ".gnupg") in plan.sensitive_dirs

    @_POSIX_ONLY
    def test_strict_script_denies_namespace_escape_not_hardlinks(self):
        """Linux seccomp deny list must contain the namespace-escape syscalls
        (mount/umount2/unshare/setns/pivot_root) and must NOT contain
        link/linkat -- hardlink containment is the bind-mask's job, and a
        blanket link ban broke hardlink-using build tools (npm cacache). Guards
        against an accidental re-add of link/linkat or drop of an escape
        syscall (pentest finding #9 remediation). Asserted on the filter the
        launcher installs, by running it on each syscall."""
        cases = (
            # mount=165 umount2=166 unshare=272 setns=308 pivot_root=155; link=86 linkat=265
            ("x86_64", (165, 166, 272, 308, 155), (86, 265)),
            # mount=40 umount2=39 unshare=97 setns=268 pivot_root=41; linkat=37
            ("aarch64", (40, 39, 97, 268, 41), (37,)),
        )
        for machine, escape, links in cases:
            denied, _kill_nr = launcher_program.seccomp_deny_table(machine)
            assert denied == escape
            for nr in escape:
                assert _seccomp_verdict(machine, nr, 0) == _SECCOMP_EPERM, (machine, nr)
            for nr in links:
                assert _seccomp_verdict(machine, nr, 0) == _SECCOMP_ALLOW, (machine, nr)

    @_POSIX_ONLY
    def test_launcher_refuses_when_seccomp_cannot_be_installed(self, monkeypatch, tmp_path):
        """An arch with no syscall table, or a libc without prctl(2), must make
        the launcher EXIT -- not skip the confinement steps and exec the agent anyway.

        Skipping leaves ``unshare`` permitted, so the child can enter a nested
        user namespace, hold CAP_SYS_ADMIN over a copy of this mount tree, and
        umount every credential mask -- the escape the filter exists to deny.
        Both ``_inside_kirocrew_sandbox`` and
        ``docs/system-specs/modules/security.md`` state that a sandboxed tree is
        confined "by the outer namespace + seccomp", so a silent skip makes that
        claim false while every caller still reads the spawn as isolated.
        """
        # The rendered launcher stays valid Python at every tier.
        for level in ("standard", "cc", "strict"):
            compile(_build_launcher_script(level), "<launcher-%s>" % level, "exec")

        supported = (
            ("x86_64", (165, 166, 272, 308, 155)),
            ("aarch64", (40, 39, 97, 268, 41)),
        )
        for machine, table in supported:
            monkeypatch.setattr(launcher_program._plat, "machine", lambda m=machine: m)
            libc = RecordingLibc()
            assert refusal(launcher_program.install_seccomp, launch(tmp_path, libc=libc)) is None
            assert launcher_program.seccomp_deny_table(machine)[0] == table
            # PR_SET_SECCOMP with SECCOMP_MODE_FILTER: the filter was installed.
            assert [p[:2] for p in libc.prctls] == [(22, 2)]

        for machine in ("riscv64", "armv7l", "ppc64le", "s390x"):
            monkeypatch.setattr(launcher_program._plat, "machine", lambda m=machine: m)
            libc = RecordingLibc()
            run = launch(tmp_path, libc=libc)
            message = refusal(launcher_program.run_child, run, ["/bin/agent"])
            assert message is not None and "sandbox: BLOCKED" in message
            assert repr(machine) in message
            # The refusal must name the explicit opt-out, or an operator on such
            # a host is left with no way forward.
            assert "agent.sandbox_allow_unsandboxed_exec" in message
            assert all(p[0] != 22 for p in libc.prctls), "a filter was installed"
            assert run.execs == []

        libc = RecordingLibc()
        libc.prctl = None  # type: ignore[assignment]
        run = launch(tmp_path, libc=libc)
        message = refusal(launcher_program.run_child, run, ["/bin/agent"])
        assert message is not None and "libc exposes no prctl(2)" in message
        assert run.execs == []

    @_POSIX_ONLY
    def test_standard_script_excludes_aws(self):
        plan = sandbox_mod._spawn_plan("namespace", "standard")
        # Standard dirs don't include .aws, and ~/.ssh stays unmasked.
        assert os.path.join(str(Path.home()), ".aws") not in plan.sensitive_dirs
        assert plan.hide_ssh is False
        assert sandbox_plan.namespace_payload(plan)["hide_ssh"] == 0

    @_POSIX_ONLY
    def test_auth_staging_is_hidden_except_for_trusted_auth_spawn(self):
        home = Path.home()
        staging = home / ".kiro" / "crew-auth-staging"
        workspace = staging / "auth-123"
        data_home = home / ".kiro" / "crew"

        regular_plan = sandbox_mod._spawn_plan("namespace", "standard")
        auth_plan = sandbox_mod._spawn_plan(
            "namespace",
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

        assert str(staging) in regular_plan.sensitive_dirs
        assert str(staging) in regular_profile
        # The auth spawn's own workspace lifts the staging mask, and no field of the
        # launcher's data names the staging tree any more.
        assert (
            sandbox_plan.Cancellation(path=str(staging), by=(str(workspace),), read_only=False)
            in auth_plan.cancellations
        )
        assert str(staging) not in repr(sandbox_plan.namespace_payload(auth_plan))
        assert str(staging) not in auth_profile
        assert str(data_home) in auth_plan.sensitive_dirs
        assert str(data_home) in auth_profile

    @_POSIX_ONLY
    def test_a_file_valued_hidden_path_reaches_the_file_loop(self, tmp_path):
        """A hidden path that is a FILE must reach the launcher's file-masking stage.

        The launcher's two stages hide each kind differently — a directory gets an
        empty dir bind-mounted over it, a file gets an empty temp file — and each
        takes only the entries of its own kind. So a file entry offered to the
        directory stage alone would be SILENTLY SKIPPED: the caller asked for it to be
        hidden, got no error, and the file stayed readable.

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
        (real_dir / "id").write_text("key", encoding="utf-8")

        plan = sandbox_mod._spawn_plan(
            "namespace", "strict", extra_hidden_dirs=(str(secret), str(real_dir))
        )

        # The file reaches the stage that can actually hide it.
        assert str(secret) in plan.sensitive_files, "a file-valued hidden path cannot be hidden"
        # And the directory still reaches its own stage.
        assert str(real_dir) in plan.sensitive_dirs

    def test_the_builder_does_not_stat_the_hidden_paths(self, monkeypatch, tmp_path):
        """No filesystem probe of a hidden path while the launcher is built.

        Classifying each path here with ``os.path.isfile()`` would be 52 stats per
        async spawn on the gateway's single loop, and on a stalled NFS home each one
        blocks — freezing every session, cron and the liveness heartbeat. The child already re-checks with its own
        ``isdir``/``isfile`` per stage, so whichever stage matches does the work and
        the other skips; letting it decide keeps the syscalls where they were already
        happening and where blocking costs only that one spawn.

        Every ``stat`` and ``lstat`` the build makes is recorded, and so is every
        ``os.path`` predicate: on POSIX ``isfile``, ``isdir``, ``exists``, ``islink`` and
        ``realpath`` reach ``stat``/``lstat``, but on Windows ``isdir`` and ``exists``
        are ``nt._path_*`` builtins that never call ``os.stat``. None may name a hidden
        path or anything under one.
        """
        hidden = []
        for index in range(52):
            path = tmp_path / f"hidden-{index}"
            if index % 2:
                path.mkdir()
            else:
                path.write_text("s", encoding="utf-8")
            hidden.append(str(path))
        probed: list[str] = []

        def _recording(real):
            def _probe(path, *args, **kwargs):
                if not isinstance(path, int):
                    probed.append(os.fsdecode(os.fspath(path)))
                return real(path, *args, **kwargs)

            return _probe

        monkeypatch.setattr(os, "stat", _recording(os.stat))
        monkeypatch.setattr(os, "lstat", _recording(os.lstat))
        for predicate in ("isfile", "isdir", "exists", "lexists", "islink", "realpath"):
            monkeypatch.setattr(os.path, predicate, _recording(getattr(os.path, predicate)))
        # The namespace plan asks this process for its uid and gid, which Windows has
        # no call for; a stand-in there keeps this check as platform-neutral as the
        # property it states.
        monkeypatch.setattr(os, "getuid", getattr(os, "getuid", lambda: 0), raising=False)
        monkeypatch.setattr(os, "getgid", getattr(os, "getgid", lambda: 0), raising=False)

        _build_launcher_script("strict", extra_hidden_dirs=tuple(hidden))

        touched = sorted(
            {p for p in probed if any(p == h or p.startswith(h + os.sep) for h in hidden)}
        )
        assert touched == [], f"the launcher build stats hidden paths: {touched}"

    @_POSIX_ONLY
    def test_every_sensitive_path_reaches_a_loop_that_can_hide_it(self):
        """Whole-list check against the real sensitive-path list.

        Both stages self-guard, so a path present in both is hidden by whichever stage
        matches its actual type — and a future entry that happens to be a file cannot
        silently stop being hidden.
        """
        from kiro_crew import security

        home = os.path.expanduser("~")
        extra = tuple(os.path.join(home, rel) for rel in security.sensitive_home_dirs())
        plan = sandbox_mod._spawn_plan("namespace", "strict", extra_hidden_dirs=extra)

        for path in extra:
            assert path in plan.sensitive_dirs, f"{path} never reaches the directory loop"
            assert path in plan.sensitive_files, f"{path} never reaches the file loop"

    @_LINUX_ONLY
    def test_both_hiding_stages_hide_a_planned_path_by_its_kind(self, tmp_path):
        """Offered every hidden path, the two stages hide each by its own kind.

        The file and the directory a caller asked to hide are planned into both
        lists; the launcher's directory stage masks the directory and skips the
        file, its file stage masks the file and skips the directory's stand-in.
        """
        secret = tmp_path / "token_signing.key"
        secret.write_text("s3cret", encoding="utf-8")
        real_dir = tmp_path / "creds"
        real_dir.mkdir()
        (real_dir / "id").write_text("key", encoding="utf-8")
        plan = sandbox_mod._spawn_plan(
            "namespace", "strict", extra_hidden_dirs=(str(secret), str(real_dir))
        )
        run = launch(tmp_path, _payload_under(plan, tmp_path))
        launcher_program.mask_sensitive(run)
        launcher_program.mask_sensitive_files(run)
        assert secret.read_text(encoding="utf-8") == ""
        assert os.listdir(real_dir) == []

    @_POSIX_ONLY
    def test_cc_script_exposes_aws_config(self):
        plan = sandbox_mod._spawn_plan("namespace", "cc")
        aws = os.path.join(str(Path.home()), ".aws")
        # The tree is masked and its config is restored read-only into the mask.
        assert aws in plan.sensitive_dirs
        assert (os.path.join(aws, "config"), "config") in plan.expose

    @_POSIX_ONLY
    def test_script_scrubs_env_vars(self, tmp_path):
        plan = sandbox_mod._spawn_plan("namespace", "strict")
        assert set(_SENSITIVE_ENV_PREFIXES) <= set(plan.env_scrub_prefixes)
        planted = {prefix + "_PLANTED": "secret" for prefix in _SENSITIVE_ENV_PREFIXES}
        run = launch(
            tmp_path,
            payload(env_prefixes=list(plan.env_scrub_prefixes)),
            environ={**planted, "KEEP_ME": "1"},
        )
        launcher_program.scrub_env(run)
        assert sorted(set(planted) & set(run.environ)) == []
        assert run.environ["KEEP_ME"] == "1"

    @_POSIX_ONLY
    def test_launcher_has_no_unimportable_kiro_crew_refs(self, tmp_path):
        """The launcher runs as a standalone ~/.kirocrew/run script with the
        launcher dir scrubbed from sys.path, so it CANNOT import kiro_crew.
        Referencing a module-level helper like ``platform_compat`` NameErrors at
        runtime and crashed every command cron. Guard: the program imports only
        the standard library, there is no module-qualified RUNTIME reference to
        any host-only module the isolated launcher can't import, the rendered
        script stays syntactically valid at every tier, and the read-only copy
        an exposed file is restored as is made with the program's own chmod.

        AST-based over the program's source -- the rendered launcher is that
        source plus one data line -- so the program's own explanatory comment
        naming platform_compat (why the inline os.chmod must NOT use it) does
        not false-positive: only module-qualified attribute access counts.
        """
        tree = ast.parse(Path(launcher_program.__file__).read_text(encoding="utf-8"))
        used_modules = {
            node.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
        forbidden = used_modules & {"platform_compat", "kiro_crew", "logger", "logging"}
        assert not forbidden, f"launcher references un-importable module(s) {forbidden}"
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0 and node.module, "the launcher imports relative to nothing"
                imported.add(node.module.split(".")[0])
        assert imported <= set(sys.stdlib_module_names), sorted(
            imported - set(sys.stdlib_module_names)
        )
        for level in ("strict", "standard", "cc"):
            compile(_build_launcher_script(level), "<launcher>", "exec")

        config = tmp_path / "config"
        config.write_text("[profile x]\n", encoding="utf-8")
        run = launch(tmp_path, payload(expose_files=[[str(config), "config"]]))
        launcher_program.preread_exposed_files(run)
        config.unlink()  # what the empty mask over its parent leaves
        launcher_program.restore_exposed_files(run)
        assert config.read_text(encoding="utf-8") == "[profile x]\n"
        assert stat.S_IMODE(os.stat(config).st_mode) == 0o444


class TestAgentEnvPassthroughContract:
    """Pin the claude-code-provider env-passthrough contract.

    Inherited ``ANTHROPIC_*`` / ``CLAUDE_CODE_*`` variables must keep flowing
    to claude-harness children (docs/system-specs/modules/claude-code-provider.md,
    env-passthrough section); the custom-endpoint guide
    (docs/guides/custom-llm-backend.md) depends on them reaching the child. If
    either scrub list grows to cover these namespaces, this fails red — the
    failure a green-CI hardening pass would otherwise ship silently.
    """

    _PASSTHROUGH_KEYS = [
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
    ]

    def test_anthropic_and_claude_code_env_survive_agent_subprocess_scrub(self):
        # Both lists are prefix-matched (``startswith`` in ``scrub_env`` and
        # ``scrub_agent_denied_env``), so assert prefix semantics, not bare
        # membership — a prefix entry like "ANTHROPIC_" would never equal a key.
        for key in self._PASSTHROUGH_KEYS:
            assert not any(key.startswith(p) for p in _SENSITIVE_ENV_PREFIXES), key
            assert not any(key.startswith(p) for p in sandbox_mod._AGENT_DENIED_ENV_KEYS), key
        # And the ACP spawn path's own parent-side scrub passes them through.
        env = {key: "x" for key in self._PASSTHROUGH_KEYS}
        assert sandbox_mod.scrub_agent_subprocess_env(env) == env


@_POSIX_ONLY
class TestHardlinkScanBudget:
    """The pre-exec hardlink scan: per-root budgets, loud truncation, no pruning.

    Driven through ``refuse_hardlinked_credentials``, the launcher stage that runs
    last before exec, over a tree under ``tmp_path``. Each root has its own budget,
    because one shared across the CWD and /tmp walks lets a large worktree consume it
    all before /tmp (the world-writable root the check exists for) is scanned at all;
    and an exhausted budget warns, because a silent one makes a truncated scan
    indistinguishable from a clean one.
    """

    @staticmethod
    def _credential(tmp_path):
        """A masked credential directory and the regular file inside it."""
        creds = tmp_path / "creds"
        creds.mkdir()
        secret = creds / "key"
        secret.write_text("s", encoding="utf-8")
        return creds, secret

    @staticmethod
    def _scan(run, roots, *budget):
        """The scan stage over *roots*; its refusal message, or ``None``."""
        return refusal(
            launcher_program.refuse_hardlinked_credentials, run, [str(r) for r in roots], *budget
        )

    def test_only_aliased_credential_inodes_arm_the_walk(self, tmp_path, capfd):
        # An inode with st_nlink == 1 has no alias anywhere on the
        # filesystem, so it must not enter the match set: when every
        # credential has nlink == 1 the CWD + /tmp walk is skipped and the
        # common healthy-host spawn pays nothing (and emits no truncation
        # warning). BOTH collection loops carry the gate: the masked
        # directories (depth 1) and the masked files. The per-app credentials
        # one level below a mask root reach the child as inodes the PARENT
        # read -- it cannot stat them itself, because it masks that tree in
        # this same process -- and the parent applies the same gate before
        # sending one. Every other path list the launcher carries is filled
        # with directories too, which is how this notices a further
        # collection loop added without the gate.
        #
        # REGULAR FILES only, and that half is not cosmetic: every directory has
        # nlink >= 2, and the masked-file list carries directories on purpose, so a
        # bare nlink test armed the walk on every spawn. A FIFO with a second name
        # stands in for every other non-regular kind. More behaviour is covered in
        # test_sandbox_hardlink_scan.py.
        creds, secret = self._credential(tmp_path)
        (creds / "sub").mkdir()
        fifo = creds / "fifo"
        os.mkfifo(fifo)
        try:
            os.link(fifo, tmp_path / "fifo-link")
        except OSError:
            pass  # a filesystem that gives a FIFO no second name has nothing to gate
        lone = tmp_path / "lone"
        lone.write_text("s", encoding="utf-8")
        a_dir = tmp_path / "a-dir"
        (a_dir / "inner").mkdir(parents=True)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "f").write_text("f", encoding="utf-8")
        run = launch(
            tmp_path,
            payload(
                sensitive_dirs=[str(creds), str(a_dir)],
                sensitive_files=[str(lone), str(a_dir), str(creds), str(fifo)],
                readonly_dirs=[str(a_dir)],
                writable_dirs=[str(a_dir)],
                private_dirs=[str(a_dir / "inner")],
                expose_files=[[str(lone), "lone"]],
            ),
        )
        # A budget of 0 makes an armed walk report itself at the first file it meets.
        assert self._scan(run, [workspace], 0) is None
        assert "hardlink scan truncated" not in capfd.readouterr().err

        # A second name for a credential in a masked directory arms it ...
        os.link(secret, tmp_path / "secret-link")
        assert self._scan(run, [workspace], 0) is None
        assert f"truncated at 0 files in {workspace}" in capfd.readouterr().err
        os.unlink(tmp_path / "secret-link")
        # ... and so does one for a masked file.
        os.link(lone, tmp_path / "lone-link")
        assert self._scan(run, [workspace], 0) is None
        assert f"truncated at 0 files in {workspace}" in capfd.readouterr().err

    def test_per_root_budget_covers_a_busy_tmp(self, tmp_path, capfd, monkeypatch):
        # The budget only applies once a credential inode is actually
        # aliased (see the nlink gate above), so it can afford to be
        # generous: 100k covers the busiest observed /tmp (~11.8k files)
        # with an order of magnitude to spare, making truncation genuinely
        # exceptional rather than a steady-state warning. The stage's own
        # default is what the launcher runs with; a root reporting one entry
        # more than that is where it stops.
        _creds, secret = self._credential(tmp_path)
        os.link(secret, tmp_path / "secret-link")
        busy = tmp_path / "busy-tmp"
        busy.mkdir()
        names = [f"entry-{index}" for index in range(100_001)]
        real_walk = os.walk

        def _walk(top, *args, **kwargs):
            if top == str(busy):
                yield top, [], names
                return
            yield from real_walk(top, *args, **kwargs)

        monkeypatch.setattr(launcher_program.os, "walk", _walk)
        run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
        assert self._scan(run, [busy]) is None
        assert f"truncated at 100000 files in {busy}" in capfd.readouterr().err

    def test_per_root_budget_with_counter_reset_inside_root_loop(self, tmp_path, capfd):
        # Each root gets exactly one fresh budget, so a large CWD cannot
        # starve the /tmp scan -- and one budget per ROOT, not per directory:
        # a reset inside the walk would make the scan effectively unbounded.
        _creds, secret = self._credential(tmp_path)
        large = tmp_path / "large-cwd"
        for sub in ("d1", "d2"):
            (large / sub).mkdir(parents=True)
            for name in ("x", "y"):
                (large / sub / name).write_text(name, encoding="utf-8")
        small = tmp_path / "small-tmp"
        small.mkdir()
        os.link(secret, small / "alias")
        run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
        message = self._scan(run, [large, small], 3)
        # The first root ran out across its directories ...
        assert f"truncated at 3 files in {large}" in capfd.readouterr().err
        # ... and the second still had a whole budget of its own.
        assert message is not None and str(small / "alias") in message

    def test_truncation_warns_on_stderr_without_exiting(self, tmp_path, capfd):
        _creds, secret = self._credential(tmp_path)
        os.link(secret, tmp_path / "secret-link")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for name in ("a", "b", "c"):
            (workspace / name).write_text(name, encoding="utf-8")
        run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
        # Deliberate fail-open: the truncation path warns, it never exits.
        assert self._scan(run, [workspace], 2) is None
        # The diagnostic goes to stderr, which the parent already captures.
        assert (
            "sandbox: WARNING — pre-exec hardlink scan truncated at 2 files in "
            f"{workspace}; scan incomplete (control degrades open)"
        ) in capfd.readouterr().err

    def test_blocked_exit_path_for_found_hardlinks_still_present(self, tmp_path):
        _creds, secret = self._credential(tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        os.link(secret, workspace / "copy")
        run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
        assert self._scan(run, [workspace]) == (
            "sandbox: BLOCKED — found hardlink(s) to protected credential inodes: "
            f"{[str(workspace / 'copy')]}. Remove them before running."
        )

    def test_no_directory_pruning_in_the_scan(self, tmp_path):
        # /tmp is world-writable and the sandboxed agent shares the uid, so
        # any name- or prefix-based prune list is a deterministic bypass: the
        # attacker just names their directory to match. The scan must visit
        # every directory the depth limit allows; noisy trees are handled by
        # the per-root budget + truncation warning, never by skipping. So an
        # alias under each of the names a noisy /tmp holds is found.
        _creds, secret = self._credential(tmp_path)
        tmp_root = tmp_path / "tmp"
        aliases = []
        for name in (
            "systemd-private-1234",
            ".X11-unix",
            "pip-build-env-x",
            "node-compile-cache",
            "pytest-of-agent",
        ):
            nested = tmp_root / name / "nested"
            nested.mkdir(parents=True)
            os.link(secret, nested / "alias")
            aliases.append(str(nested / "alias"))
        run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
        message = self._scan(run, [tmp_root])
        assert message is not None
        assert [alias for alias in aliases if alias not in message] == []

    def test_generated_script_compiles_at_every_level(self):
        # Proves the rendered launcher is syntactically valid Python for every
        # sandbox level.
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

    # A sibling ``ctypes`` that announces itself. ``ctypes`` is the first module the
    # launcher resolves from the filesystem and no interpreter imports it at startup,
    # so this poison is imported whenever a directory holding it is still on sys.path.
    _CTYPES_POISON = (
        "import json, sys\nprint('SHADOWED ' + json.dumps(sys.path))\nraise SystemExit(97)\n"
    )

    # Runs a program as ``__main__`` from ``python -c``, where ``""`` (the cwd) heads
    # sys.path, taking the program's own path back off argv so it sees no command.
    _RUN_AS_MAIN = (
        "import runpy, sys; path = sys.argv.pop(1); runpy.run_path(path, run_name='__main__')"
    )

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

        Runs the rendered launcher beside a poisoned ``ctypes.py``, then again from a
        cwd holding one, with the cwd entry ``""`` heading sys.path as it does under
        ``python -c``. Neither poison may be imported: the prelude drops the script's
        own directory — which CPython puts at sys.path[0] — and any ``""`` entry
        before the first import that resolves from the filesystem. Unlike the struct
        e2e below, this does not depend on whether the interpreter pre-imports
        ``struct``, so it always discriminates the fix.
        """
        (tmp_path / "ctypes.py").write_text(self._CTYPES_POISON)
        probe = tmp_path / "launcher.py"
        probe.write_text(_build_launcher_script("standard"))
        result = subprocess.run(
            [sys.executable, str(probe)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "SHADOWED" not in result.stdout, f"script dir not stripped: {result.stdout}"
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / "ctypes.py").write_text(self._CTYPES_POISON)
        from_cwd = subprocess.run(
            [sys.executable, "-c", self._RUN_AS_MAIN, str(probe)],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            cwd=cwd,
        )
        assert "SHADOWED" not in from_cwd.stdout, f"cwd entry not stripped: {from_cwd.stdout}"
        # Both runs got past every import to the argv guard, on the one platform
        # whose libc carries the calls the launcher binds before it.
        if sys.platform.startswith("linux"):
            for run in (result, from_cwd):
                assert "no command given" in run.stderr, run.stderr

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

    #: The launcher's first filesystem import, without the prelude's hardening.
    _UNHARDENED = "import sys\nimport ctypes\nsys.exit('sandbox_launcher: no command given')\n"

    def test_control_unstripped_launcher_would_crash(self, tmp_path):
        """Control: prove the poison is real — the launcher's imports, un-hardened, DO crash.

        A script that makes the launcher's first filesystem import, ``import ctypes``,
        without the prelude's hardening, so we don't silently ship a test that passes
        for the wrong reason. The poison only bites if the interpreter imports
        ``struct`` fresh (not already cached at startup); if a given build
        interpreter pre-caches ``struct``, the shadowing can't be demonstrated
        here, so we skip rather than red the build for an unrelated reason.
        """
        (tmp_path / "struct.py").write_text(self._POISON)
        launcher = tmp_path / "launcher.py"
        launcher.write_text(self._UNHARDENED)
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
        # Otherwise the shadowed struct broke the ctypes import -> the script
        # died before reaching the argv guard, proving the poison is real.
        if ("calcsize" not in result.stderr) and ("shadowed!" not in result.stderr):
            preview = repr(result.stderr)[:120]
            pytest.skip(
                "struct shadowing not observable on this interpreter "
                f"(stderr={preview}); "
                "positive test (test_launcher_survives_sibling_struct_py) still guards the fix"
            )

    def test_control_unhardened_import_takes_a_sibling_ctypes(self, tmp_path):
        """Control for the prelude test: the ``ctypes.py`` poison bites an un-hardened script.

        Skipped, like the struct control, on an interpreter that has ``ctypes``
        imported before any script runs, where no sibling can shadow it.
        """
        (tmp_path / "ctypes.py").write_text(self._CTYPES_POISON)
        launcher = tmp_path / "launcher.py"
        launcher.write_text(self._UNHARDENED)
        result = subprocess.run(
            [sys.executable, str(launcher)],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            cwd=str(tmp_path),
        )
        if "no command given" in result.stderr:
            pytest.skip("interpreter imports 'ctypes' at startup; no sibling can shadow it")
        assert "SHADOWED" in result.stdout, (result.stdout, result.stderr)
        assert result.returncode == 97


class TestSignalBroadcastGuard:
    """seccomp kill(-1) broadcast denial + KIROCREW_HOST_PID export.

    Redo of the reverted PID-namespace isolation (24c320f6 → 14fb9442): the
    broadcast accident is contained by a static seccomp arg filter instead of
    a namespace, so the subtree's view of pids — and every host-PID-coupled
    mechanism (session identity, claim-push, systemd) — stays intact.
    """

    @_POSIX_ONLY
    def test_launcher_script_contains_kill_filter(self):
        """The filter the launcher installs denies the kill broadcast, per arch,
        by inspecting the pid argument -- asserted by running it on each call."""
        for machine, kill_nr in (("x86_64", 62), ("aarch64", 129)):
            assert launcher_program.seccomp_deny_table(machine)[1] == kill_nr
            assert _seccomp_verdict(machine, kill_nr, -1) == _SECCOMP_EPERM
            # args[0] LOW word only, at seccomp_data offset 16, compared as the
            # 32-bit pid -1. The high word (offset 20) must NOT be matched: pid_t
            # is a 32-bit int and the x86-64 ABI leaves the upper register half
            # undefined (glibc zero-extends, so a high==0xFFFFFFFF check never
            # fires).
            assert _seccomp_verdict(machine, kill_nr, 0x0000_0000_FFFF_FFFF) == _SECCOMP_EPERM
            assert _seccomp_verdict(machine, kill_nr, 0xFFFF_FFFF_0000_0005) == _SECCOMP_ALLOW
            # A targeted kill, and a process-group target, stay allowed.
            assert _seccomp_verdict(machine, kill_nr, 1234) == _SECCOMP_ALLOW
            assert _seccomp_verdict(machine, kill_nr, -1234) == _SECCOMP_ALLOW

    @_POSIX_ONLY
    def test_launcher_script_exports_host_pid(self, monkeypatch):
        """The launcher exports KIROCREW_HOST_PID before fork so the whole
        subtree can resolve session_pid files by the recorded pid.

        Driven through the program's ``main`` with ``fork`` answering as the
        child, and the child's two steps recorded rather than run.
        """
        # A stale value main must overwrite, set through monkeypatch so teardown restores
        # the variable: main writes it into the real environment.
        monkeypatch.setenv("KIROCREW_HOST_PID", "0")
        at_fork: list[str | None] = []
        child_steps: list[str] = []

        def _fork() -> int:
            at_fork.append(os.environ.get("KIROCREW_HOST_PID"))
            return 0

        def _enter_namespaces(_launch, c2p_w, p2c_r) -> None:
            os.close(c2p_w)
            os.close(p2c_r)
            child_steps.append("enter_namespaces")

        monkeypatch.setattr(launcher_program.os, "fork", _fork)
        monkeypatch.setattr(launcher_program, "enter_namespaces", _enter_namespaces)
        monkeypatch.setattr(
            launcher_program, "run_child", lambda _launch, argv: child_steps.append("run_child")
        )
        launcher_program.main(payload(), libc=RecordingLibc(), argv=["/bin/agent"])
        # Set BEFORE the fork, so the child inherits it.
        assert at_fork == [str(os.getpid())]
        assert child_steps == ["enter_namespaces", "run_child"]

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

    def test_the_child_keeps_the_real_ids_and_its_exit_code_e2e(self, tmp_path):
        """Live e2e through the real launcher: the child runs as the caller's own uid and
        gid under an identity map, never as root, and the launcher exits with its code."""
        if sys.platform != "linux":
            pytest.skip("sandbox launcher is Linux-only")
        import kiro_crew.sandbox as _sb

        if not _sb._probe_unshare():
            pytest.skip("user+mount namespaces unavailable on this host")
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import os, sys\n"
            "print(os.getuid(), os.getgid())\n"
            "for name in ('uid_map', 'gid_map', 'setgroups'):\n"
            "    with open('/proc/self/' + name) as f:\n"
            "        print(name, *f.read().split())\n"
            "sys.exit(7)\n"
        )
        # A plan with nothing to mask and no stand-in roots, so the child creates no
        # bind source on the host; only the identity maps and the exit code are under test.
        launcher = tmp_path / "launcher.py"
        launcher.write_text(
            sandbox_launcher.launcher_program_source().replace(
                sandbox_launcher.PLAN_PLACEHOLDER, "_PLAN = %s\n" % json.dumps(payload())
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(launcher), sys.executable, str(probe)],
            capture_output=True,
            encoding="utf-8",
            timeout=60,
            cwd=str(tmp_path),
            env={**os.environ, "TMPDIR": str(tmp_path)},
        )
        if "unshare(NEWUSER) failed" in result.stderr or "unshare(NEWNS) failed" in result.stderr:
            pytest.skip("namespaces unavailable on this host")
        uid, gid = str(os.getuid()), str(os.getgid())
        assert result.stdout.splitlines() == [
            f"{uid} {gid}",
            f"uid_map {uid} {uid} 1",
            f"gid_map {gid} {gid} 1",
            "setgroups deny",
        ], result.stderr
        assert result.returncode == 7, result.stderr


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
    def test_established_target_that_cannot_be_restatted_refuses(self, mock_resolve, tmp_path):
        """A ``_required_targets`` entry whose ``lstat`` now fails must FAIL CLOSED.

        A pre-spawn materialiser reports a target as established (present). If an
        ``lstat`` of that name then fails while the launcher builds -- renamed aside
        or its permission revoked, the ordinary racing data-home write this module
        already assumes -- carrying NO identity would leave ``_carried_occupant``
        returning ``None`` in the child, the substitution refusal never fires, and a
        decoy left at the name is sealed while the original stays writable elsewhere.
        ``namespace_argv`` must raise :class:`SandboxCeilingUnsealable` instead of
        masking whatever took the name's place.
        """
        established_name = str(tmp_path / "phantom-ceiling")
        real_lstat = os.lstat

        def _lstat_fails_for_target(path, *a, **k):  # noqa: ANN001, ANN202
            if os.fspath(path) == established_name:
                raise OSError(2, "vanished")
            return real_lstat(path, *a, **k)

        def _establish(established=None):  # noqa: ANN001, ANN202
            if established is not None:
                established.append(established_name)
            return []

        with (
            patch("kiro_crew.sandbox._materialize_sealable_ceilings", side_effect=_establish),
            patch("kiro_crew.sandbox.os.lstat", side_effect=_lstat_fails_for_target),
        ):
            with pytest.raises(sandbox_mod.SandboxCeilingUnsealable):
                namespace_argv(["kiro-cli"], "strict")

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
                subprocess.run([result[0], "-c", "pass"], env=env, capture_output=True, timeout=60)
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
        assert (
            '["env",' not in source
        ), 'a bare ["env", ...] argv prefix is PATH-redirectable; use _pinned_env_bin()'


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
                removed = cleanup_stale_sandbox_profiles(
                    legacy_dir=str(tmp_path / "nonexistent"), data_home=sandbox_mod.config_dir()
                )

        assert not stale_file.exists()
        assert removed == 1

    def test_reclaims_retired_acp_snapshot_tree(self, tmp_path):
        """Orphaned pre-in-place-launch kiro-cli copies are reclaimed.

        An earlier build copied the whole ~100 MB kiro-cli binary per ACP spawn
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
            removed = cleanup_stale_sandbox_profiles(
                legacy_dir=str(tmp_path / "nonexistent"), data_home=sandbox_mod.config_dir()
            )

        assert not (home / "run" / "kiro-cli-snapshots").exists()
        assert removed == 1
        # The rest of run/ is untouched, and a second pass is a no-op.
        with patch("kiro_crew.sandbox.config_dir", return_value=home):
            assert (
                cleanup_stale_sandbox_profiles(
                    legacy_dir=str(tmp_path / "nonexistent"), data_home=sandbox_mod.config_dir()
                )
                == 0
            )

    def test_reclaims_pi_gate_artifacts_of_a_dead_gateway_only(self, tmp_path):
        """The pi gate launcher and sealed copy go with their gateway, never by age.

        Both are written once per gateway process and reused by its later spawns
        (``acp/client.py``), so an old-but-owned file is live and the age rule
        that fits the once-consumed sandbox launchers would delete it between a
        spawn's cache hit and its exec. The PID in the name is the owner's own.
        """
        from kiro_crew.sandbox import cleanup_stale_sandbox_profiles

        run_dir = tmp_path / ".kirocrew" / "run"
        run_dir.mkdir(parents=True)
        dead = [run_dir / "kirocrew_pi_gate_99999_abc.sh", run_dir / "kirocrew_pi_gate_99999.ts"]
        for f in dead:
            f.write_text("x")
        mine_sh = run_dir / f"kirocrew_pi_gate_{os.getpid()}_def.sh"
        mine_ts = run_dir / f"kirocrew_pi_gate_{os.getpid()}.ts"
        for f in (mine_sh, mine_ts):
            f.write_text("x")
            old_time = time.time() - 10 * 3600
            os.utime(f, (old_time, old_time))

        with patch("kiro_crew.sandbox.config_dir", return_value=tmp_path / ".kirocrew"):
            removed = cleanup_stale_sandbox_profiles(
                legacy_dir=str(tmp_path / "nonexistent"), data_home=sandbox_mod.config_dir()
            )

        assert removed == 2
        assert not any(f.exists() for f in dead)
        assert mine_sh.exists() and mine_ts.exists(), "an owned file is live however old"

    def test_preserves_live_pid_profile(self, tmp_path):
        """Profile file whose PID is alive (current process) is preserved."""
        from kiro_crew.sandbox import cleanup_stale_sandbox_profiles

        run_dir = tmp_path / ".kirocrew" / "run"
        run_dir.mkdir(parents=True)
        live_file = run_dir / f"kirocrew_sandbox_{os.getpid()}_xyz789.sb"
        live_file.write_text("(version 1)")

        with patch("kiro_crew.sandbox.config_dir", return_value=tmp_path / ".kirocrew"):
            removed = cleanup_stale_sandbox_profiles(
                legacy_dir=str(tmp_path / "nonexistent"), data_home=sandbox_mod.config_dir()
            )

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
            removed = cleanup_stale_sandbox_profiles(
                legacy_dir=str(tmp_path / "nonexistent"), data_home=sandbox_mod.config_dir()
            )

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
        only ``sysconf`` would not make RAM unknown and this would assert
        against a derived value instead of the fallback.
        """
        import kiro_crew.sandbox as sb

        with (
            patch("os.sysconf", side_effect=OSError("no sysconf")),
            patch.object(sb.platform_compat, "system_memory", return_value=None),
        ):
            assert sb._default_max_memory_mb() == sb._CGROUP_FALLBACK_MAX_MEMORY_MB
        # Non-positive product also falls back (never returns 0 -> unlimited).
        with (
            patch("os.sysconf", return_value=0),
            patch.object(sb.platform_compat, "system_memory", return_value=None),
        ):
            assert sb._default_max_memory_mb() == sb._CGROUP_FALLBACK_MAX_MEMORY_MB

    @pytest.mark.skipif(sys.platform != "linux", reason="cgroup v2 scope enforcement is Linux-only")
    @pytest.mark.usefixtures("real_user_session")
    def test_real_pids_max_enforced_when_available(self):
        """If this host actually has cgroup delegation, the scope must ENFORCE
        pids.max — a child under a tiny TasksMax cannot fork past it. Skips
        cleanly where delegation is unavailable (the probe returns False).

        The floor runs the suite without a systemd user session, so this is the
        one test that opts back in (``real_user_session``), and that fixture stops
        the transient slice the real ``systemd-run`` creates."""
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            available, _ = sb._probe_cgroup_scope()
            if not available:
                pytest.skip("no cgroup v2 delegation on this host")
            # Delegated controllers do not prove the user bus is reachable.
            # Only an unavailable bus is a skip; a broken scope stays a failure.
            preflight = subprocess.run(
                sb.cgroup_scope_argv([sys.executable, "-c", ""]),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            if preflight.returncode != 0 and "Failed to connect to bus" in preflight.stderr:
                pytest.skip(f"user session bus unavailable: {preflight.stderr.strip()}")
            assert preflight.returncode == 0, preflight.stderr
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
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}, clear=False),
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
                    return_value=[
                        "/usr/bin/env",
                        "-u",
                        "XDG_RUNTIME_DIR",
                        "-u",
                        "DBUS_SESSION_BUS_ADDRESS",
                    ],
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
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}, clear=False),
                caplog.at_level(logging.WARNING),
            ):
                argv, env, _cleanup = sb.sandboxed_spawn_argv(["gh"], env={"PATH": "/usr/bin:/bin"})
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
        real resolver on that layout — a hardcoded flat path would miss it,
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
