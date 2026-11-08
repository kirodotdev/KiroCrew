"""The rendered launcher run for real, the backends' argv, and the spawn helpers.

The stdlib-shadowing guard and the seccomp kill-broadcast filter run end to end where
the host allows; ``sandbox_exec_argv`` and ``namespace_argv`` are asserted on the argv
they build; the ssh probe, the executable resolver, the no-isolation log line and the
stale profile sweep complete the spawn path.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sandbox_argv_helpers
from sandbox_argv_helpers import _POSIX_ONLY, _SECCOMP_ALLOW, _SECCOMP_EPERM, _seccomp_verdict
from test_sandbox_launcher_program import RecordingLibc, payload

import kiro_crew.sandbox as sandbox_mod
from kiro_crew import sandbox_launcher
from kiro_crew import sandbox_launcher_program as launcher_program
from kiro_crew.sandbox import (
    _build_launcher_script,
    _resolve_agent_executable,
    _ssh_supports_accept_new,
    namespace_argv,
    sandbox_exec_argv,
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
            encoding="utf-8",
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
            encoding="utf-8",
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
            encoding="utf-8",
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
        assert result.returncode != 0, result.stderr

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
            encoding="utf-8",
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
        Kiro Crew's own sandbox already confines it, tries to nest, and gets EPERM
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
        # NOT inherit Kiro Crew's PYTHONPATH/PYTHONHOME, or it prepends Kiro Crew's
        # site-packages to sys.path and imports Kiro Crew's fastmcp/cryptography
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
        # Kiro Crew's OWN sandboxed Python subprocesses (cron scripts, app
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
        # The module and the owners carved out of it.
        modules = (sandbox_mod, *map(importlib.import_module, sandbox_mod._CARVED_OWNERS))
        source = "\n".join(Path(m.__file__).read_text(encoding="utf-8") for m in modules)
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
