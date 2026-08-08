"""Additional tests for kiro_crew.sandbox — wrap_argv, profiles, env scrubbing."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
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
        mock_ns_argv.return_value = [sys.executable, "/tmp/launcher.py", "kiro-cli"]
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
    def test_strict_script_contains_dirs(self):
        script = _build_launcher_script("strict")
        assert "SENSITIVE_DIRS" in script
        assert ".aws" in script
        assert ".gnupg" in script

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

    def test_standard_script_excludes_aws(self):
        script = _build_launcher_script("standard")
        # Standard dirs don't include .aws
        assert "HIDE_SSH = False" in script

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

    def test_cc_script_exposes_aws_config(self):
        script = _build_launcher_script("cc")
        assert ".aws/config" in script
        assert "EXPOSE_FILES" in script

    def test_script_scrubs_env_vars(self):
        script = _build_launcher_script("strict")
        for prefix in _SENSITIVE_ENV_PREFIXES:
            assert prefix in script

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
            assert argv.index(marker) < argv.index("sandbox-exec")
            assert argv[0] == "env"
        finally:
            if profile_path:
                os.unlink(profile_path)

    @patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "fake", "SSH_AUTH_SOCK": "/tmp/ssh"})
    def test_includes_env_unset_flags(self):
        argv, profile_path = sandbox_exec_argv(["kiro-cli", "acp"], "strict")
        try:
            assert "env" == argv[0]
            assert "-u" in argv
            assert "AWS_SECRET_ACCESS_KEY" in argv
            assert "SSH_AUTH_SOCK" in argv
            assert "sandbox-exec" in argv
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


class TestNamespaceArgv:
    @patch("kiro_crew.sandbox._resolve_agent_executable", return_value="/usr/local/bin/kiro-cli")
    def test_wraps_with_python_launcher(self, mock_resolve):
        result = namespace_argv(["kiro-cli", "acp"], "strict")
        assert result[0] == sys.executable
        assert result[1].endswith(".py")
        assert result[2] == "/usr/local/bin/kiro-cli"
        assert result[3] == "acp"
        # Cleanup temp file
        os.unlink(result[1])

    @patch("kiro_crew.sandbox._resolve_agent_executable", return_value="/usr/local/bin/kiro-cli")
    def test_launcher_script_is_executable(self, mock_resolve):
        result = namespace_argv(["kiro-cli"], "strict")
        launcher_path = result[1]
        mode = os.stat(launcher_path).st_mode
        assert mode & 0o700 == 0o700
        os.unlink(launcher_path)


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
            assert out[0] == "systemd-run"
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
            assert out[0] == "systemd-run"
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
        finally:
            self._reset_probe()

    def test_default_max_memory_is_host_proportional(self):
        """The memory default scales with physical RAM (65%), not a flat cap."""
        import kiro_crew.sandbox as sb

        # A known 16 GiB box -> 65% -> ~10649 MB.
        sixteen_g = 16 * 1024**3
        with patch("os.sysconf", side_effect=lambda n: sixteen_g // 4096 if "PHYS" in n else 4096):
            mb = sb._default_max_memory_mb()
        assert mb == int(sixteen_g * sb._CGROUP_MEMORY_FRACTION) // (1024 * 1024)
        assert 10_000 < mb < 11_000  # ~10.6 GB, expected range

    def test_default_max_memory_falls_back_when_ram_unknown(self):
        """If sysconf can't report RAM, fall back to the flat MB constant."""
        import kiro_crew.sandbox as sb

        with patch("os.sysconf", side_effect=OSError("no sysconf")):
            assert sb._default_max_memory_mb() == sb._CGROUP_FALLBACK_MAX_MEMORY_MB
        # Non-positive product also falls back (never returns 0 -> unlimited).
        with patch("os.sysconf", return_value=0):
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
            assert argv[0] == "systemd-run"
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
    """macOS sandbox mutual exclusion: kiro internal sandbox ON
    -> KiroCrew seatbelt OFF for kiro-cli spawns; OFF -> seatbelt ON."""

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
        assert argv[0] == "env"
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
                return_value=["/bin/sh", "/tmp/launcher.sh", "kiro-cli"],
            ) as mock_ns,
        ):
            wrap_argv(["kiro-cli", "acp"], mode="auto")
        mock_ns.assert_called_once()

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
        link.symlink_to(sensitive)
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
