"""The host-mutation floor guards itself (issue #1722).

Two jobs:

* **Behaviour** — prove the root ``conftest.py`` fixtures are actually armed, that
  they catch the argv shapes this codebase really produces, and that they do NOT
  interfere with an ordinary spawn. A guard nobody exercises is a guard that
  silently stops working after a refactor.
* **Ratchet** — pin the guarded set against the service managers production
  actually shells out to, so a NEW host-mutating call site in ``src/`` cannot land
  outside the guard's coverage. Same shape as ``test_spawn_preexec_guard.py``'s
  ``_ALLOWED``.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import importlib.util
import os
import pathlib
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ROOT_CONFTEST = _REPO_ROOT / "conftest.py"
_SRC = _REPO_ROOT / "src" / "kiro_crew"


def _load_root_conftest():
    """Import the rootdir conftest under its own module name.

    pytest already loads it as a plugin, but reaching it through the plugin
    manager depends on the name pytest happened to register. Loading it by path
    is deterministic, and the fixtures it defines are inert in this namespace
    (a ``@pytest.fixture`` decorator only marks a function; nothing collects
    them from here).
    """
    spec = importlib.util.spec_from_file_location("_kirocrew_root_conftest", _ROOT_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_root = _load_root_conftest()

#: Every program that could plausibly control a host service, across the init
#: systems this product might ever run under. Deliberately WIDER than what the
#: guard acts on, so that adding e.g. an ``initctl`` call site to production fails
#: the ratchet below until someone decides guard-or-exclude.
#:
#: Generic words are excluded on purpose. ``"service"`` as a bare literal is
#: almost always a dict key or a log message, and ``"systemd"`` / ``"launchd"`` in
#: this codebase are manager LABELS (``gateway_service.py`` ``kind = "launchd"``,
#: ``service/common.py`` ``LAUNCHD = "launchd"``), never ``argv[0]``.
_SERVICE_TOOL_VOCABULARY = frozenset(
    {
        "systemctl",
        "systemd-run",
        "launchctl",
        "apparmor_parser",
        "sudo",
        "pkexec",
        "doas",
        "initctl",
        "telinit",
        "rc-service",
        "update-rc.d",
        "chkconfig",
        "svcadm",
        "rcctl",
        "sc.exe",
    }
)

#: Tools present in ``src/`` that the guard intentionally lets through, with the
#: reason. Reviewed as part of this test rather than buried in a comment.
_DELIBERATELY_UNGUARDED = {
    # sandbox wraps nearly EVERY subprocess in `systemd-run --user --scope
    # --slice=kirocrew-agents.slice -p MemoryMax=...` to apply cgroup limits.
    # Refusing it refuses an ordinary `git config` spawn. Its one service-control
    # use passes `systemctl restart` as the wrapped command, caught on the inner
    # token instead -- pinned by test_wrapped_service_restart_is_refused below.
    "systemd-run": "cgroup scope wrapper, not a service-control verb",
    # A privilege prefix, not an action. `sudo systemctl restart` is caught on
    # `systemctl`; `sudo` alone says nothing about whether state changes.
    "sudo": "privilege prefix; the wrapped command carries the action",
    # The BSD-family equivalent of `sudo`, and excluded for the identical reason.
    # Its only appearance in src/ is as a KEY in the auto-improvement app's
    # `_COMMAND_WRAPPERS` table (`spine/agent_runner.py`), which exists to STRIP
    # privilege/wrapper prefixes so the shell denylist inspects the real command
    # behind them — a detector of `doas`, not a spawn of it. `doas systemctl
    # restart` is still caught on the inner `systemctl` token.
    "doas": "privilege prefix; the wrapped command carries the action",
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids() of string-literal nodes that are docstrings, so prose is skipped."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


@functools.lru_cache(maxsize=1)
def _service_tools_referenced_in_src() -> dict[str, tuple[str, ...]]:
    """Map each vocabulary program name -> where ``src/`` names it as a literal.

    Whitespace-bearing literals are skipped: a rendered unit file or a sentence
    mentioning ``systemctl --user`` is documentation or file content, not an argv
    token. An argv token never contains a space, so this keeps prose out without
    needing to model every call shape -- the codebase spawns through several
    wrappers (``_run_cmd``, ``_systemctl``, ``sandboxed_spawn_argv``), so a scan
    anchored on stdlib call sites alone would miss most of them.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        if "_vendor" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in skip:
                continue
            token = node.value
            if not token or any(c.isspace() for c in token):
                continue
            name = pathlib.PurePath(token.replace("\\", "/")).name
            if name in _SERVICE_TOOL_VOCABULARY:
                found.setdefault(name, []).append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")
    # Frozen on the way out: the result is cached and shared by every caller.
    return {name: tuple(sites) for name, sites in found.items()}


class TestRatchet:
    def test_every_service_tool_in_src_is_guarded_or_explicitly_excluded(self) -> None:
        """A new service-control tool in production must be a conscious decision.

        Without this, someone adding an ``initctl`` or ``pkexec`` call site would
        land a spawn the floor does not recognise, and the next #1722 would look
        exactly like the first one.
        """
        accounted = (
            _root._SERVICE_MANAGERS
            | _root._ALWAYS_REFUSED
            | frozenset(_DELIBERATELY_UNGUARDED)
        )
        unaccounted = {
            name: sites
            for name, sites in _service_tools_referenced_in_src().items()
            if name not in accounted
        }
        assert not unaccounted, (
            "src/ names service-control tools the host-mutation guard neither "
            "covers nor explicitly excludes. Either add each to _SERVICE_MANAGERS "
            "(with its mutating verbs in _MUTATING_VERBS) / _ALWAYS_REFUSED in the "
            "root conftest.py, or add it to _DELIBERATELY_UNGUARDED here with a "
            f"reason:\n{unaccounted}"
        )

    def test_exclusions_are_still_real(self) -> None:
        """Drop an exclusion once production stops using the tool.

        Keeps ``_DELIBERATELY_UNGUARDED`` from accumulating stale entries that
        quietly widen the hole for a tool nobody calls any more.
        """
        referenced = _service_tools_referenced_in_src()
        stale = [name for name in _DELIBERATELY_UNGUARDED if name not in referenced]
        assert not stale, f"src/ no longer uses these, so stop excluding them: {stale}"

    def test_scan_actually_finds_something(self) -> None:
        """Guards the ratchet against silently scanning nothing.

        If ``_SRC`` moved or the AST walk broke, the assertions above would pass
        vacuously on an empty result. Production is known to invoke systemctl and
        launchctl, so an empty scan means the detector is broken, not that the
        codebase got clean.
        """
        referenced = _service_tools_referenced_in_src()
        assert "systemctl" in referenced
        assert "launchctl" in referenced

    def test_exec_allowlist_is_empty(self) -> None:
        """No test may drive a real service today, and adding one needs review."""
        assert _root._HOST_SERVICE_EXEC_ALLOWED_MODULES == frozenset()

    def test_live_target_reexecs_through_the_guarded_funnel(self) -> None:
        """The guard traps ``execve`` only; pin that this is still the funnel used.

        ``maybe_reexec`` replacing the pytest process with a real gateway is the
        thing worth blocking, and it is reachable only through ``os.execve``.
        ``os.execv`` is left unguarded so the exec shim's own contract stays
        testable. If ``live_target`` ever switches funnel, that reasoning breaks
        silently and the re-exec becomes unguarded -- so fail here instead.
        """
        source = (_SRC / "service" / "live_target.py").read_text(encoding="utf-8")
        used = set()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr.startswith("exec"):
                used.add(func.attr)
        assert used, "no os.exec* call found in live_target.py -- has it moved?"
        assert used <= {"execve"}, (
            "live_target now execs through a funnel the root conftest does not trap: "
            f"{sorted(used)}. Either guard it there, or update this test's reasoning."
        )


class TestRefusalReason:
    """``_refusal_reason`` decides what counts as host service mutation."""

    def test_a_mutating_verb_is_refused(self) -> None:
        assert _root._refusal_reason(["systemctl", "--user", "restart", "x"])

    def test_daemon_reload_is_refused(self) -> None:
        assert _root._refusal_reason(["systemctl", "--user", "daemon-reload"])

    def test_an_absolute_path_is_matched(self) -> None:
        """``dev_fleet._run_cmd`` rewrites argv[0] to a trusted absolute path."""
        assert _root._refusal_reason(["/usr/bin/systemctl", "--user", "stop", "x"])

    def test_a_read_only_query_is_allowed(self) -> None:
        """The verb matters, not just the binary.

        Tests legitimately run these through the sandbox to inspect the
        environment they are running in, and they change nothing.
        """
        assert _root._refusal_reason(["systemctl", "--user", "show", "x", "--value"]) is None
        assert _root._refusal_reason(["systemctl", "--user", "cat", "x"]) is None
        assert _root._refusal_reason(["systemctl", "is-active", "x"]) is None
        assert _root._refusal_reason(["launchctl", "list"]) is None

    def test_a_cgroup_scope_wrapper_is_allowed(self) -> None:
        """The regression that keying on binaries alone caused.

        ``sandbox`` wraps nearly every spawn in ``systemd-run --scope`` for cgroup
        limits, so a binary-keyed guard refused an ordinary ``git config``. This is
        the exact argv shape that failed 27 tests before the guard learned verbs.
        """
        argv = [
            "/usr/bin/systemd-run", "--user", "--scope", "-q",
            "--slice=kirocrew-agents.slice", "-p", "MemoryMax=82386M",
            "--", "/usr/bin/git", "config", "branch.main.remote",
        ]
        assert _root._refusal_reason(argv) is None

    def test_wrapped_service_restart_is_refused(self) -> None:
        """...but the real cutover, wrapped the same way, must still be caught.

        ``SystemdBackend.restart_detached`` invokes through ``systemd-run``, so the
        dangerous verb is nested two wrappers deep. This is the pair that makes
        the exclusion of ``systemd-run`` safe.
        """
        argv = [
            "/usr/bin/systemd-run", "--user", "--scope", "-q",
            "--", "/usr/bin/systemctl", "--user", "restart", "kirocrew-gateway.service",
        ]
        assert _root._refusal_reason(argv)

    def test_sudo_wrapped_mutation_is_refused(self) -> None:
        assert _root._refusal_reason(["sudo", "systemctl", "stop", "kirocrew.service"])

    def test_sudo_alone_is_allowed(self) -> None:
        """A privilege prefix is not an action."""
        assert _root._refusal_reason(["sudo", "cat", "/etc/hosts"]) is None

    def test_apparmor_parser_is_always_refused(self) -> None:
        """No read-only mode worth allowing: any invocation rewrites policy."""
        assert _root._refusal_reason(["apparmor_parser", "-r", "/etc/apparmor.d/x"])

    def test_a_shell_string_is_matched(self) -> None:
        assert _root._refusal_reason("systemctl --user daemon-reload", shell=True)

    def test_a_lookalike_token_is_ignored(self) -> None:
        """A flag or message that merely CONTAINS a name is not a spawn."""
        assert _root._refusal_reason(["git", "log", "--grep=systemctl"]) is None
        assert _root._refusal_reason(["echo", "restart the service"]) is None

    def test_a_unit_filename_is_ignored(self) -> None:
        assert _root._refusal_reason(["cat", "kirocrew-gateway.service"]) is None

    def test_a_verb_before_the_manager_is_ignored(self) -> None:
        """Only the tail is scanned, so a wrapper's own flags cannot be the action."""
        assert _root._refusal_reason(["restart-helper", "--", "systemctl", "show", "x"]) is None

    def test_an_ordinary_spawn_is_allowed(self) -> None:
        assert _root._refusal_reason(["git", "status", "--porcelain"]) is None

    def test_an_unrecognised_argv_shape_is_tolerated(self) -> None:
        """Positive matches only -- an exotic argv can never fail a random test."""
        assert _root._refusal_reason(None) is None
        assert _root._refusal_reason(12345) is None
        assert _root._refusal_reason([None, 7]) is None

    def test_a_pathlike_is_matched(self) -> None:
        assert _root._refusal_reason([pathlib.Path("/bin/launchctl"), "unload", "x"])


class TestGuardIsArmed:
    """End-to-end: the autouse fixture is live during this very test.

    These do not patch anything -- they call the real stdlib APIs and rely on the
    root conftest's autouse fixture being in effect. That makes them a mutation
    test of the fixture itself: disarm it and every assertion here fails.
    """

    def test_sync_spawn_is_refused(self) -> None:
        with pytest.raises(AssertionError, match="mutates host service state"):
            subprocess.run(["systemctl", "--user", "restart", "x"], capture_output=True)

    def test_popen_is_refused(self) -> None:
        with pytest.raises(AssertionError, match="mutates host service state"):
            subprocess.Popen(["/usr/bin/launchctl", "unload", "x"])

    def test_check_output_is_refused(self) -> None:
        """``check_output`` never touches ``subprocess.run``; it funnels via Popen."""
        with pytest.raises(AssertionError, match="mutates host service state"):
            subprocess.check_output(["systemctl", "--user", "daemon-reload"])

    def test_async_spawn_is_refused(self) -> None:
        async def go():
            await asyncio.create_subprocess_exec(
                "/usr/bin/systemctl", "--user", "daemon-reload",
                stdout=asyncio.subprocess.PIPE,
            )

        with pytest.raises(AssertionError, match="mutates host service state"):
            asyncio.run(go())

    def test_async_shell_spawn_is_refused(self) -> None:
        async def go():
            await asyncio.create_subprocess_shell("systemctl --user restart x")

        with pytest.raises(AssertionError, match="mutates host service state"):
            asyncio.run(go())

    def test_os_execve_is_refused(self) -> None:
        """``live_target.maybe_reexec`` would replace the pytest process."""
        with pytest.raises(AssertionError, match="REPLACE the pytest process"):
            os.execve("/bin/true", ["/bin/true"], {})

    def test_os_execv_is_left_alone(self) -> None:
        """The exec shim's own 127-on-failure contract must still be testable.

        ``spawn/exec_shim`` uses ``execv`` deliberately (it passes its inherited
        env through untouched) and its real exec happens in a child process, so
        trapping ``execv`` in the pytest process protects nothing and breaks the
        shim's unit tests. A nonexistent path must still surface as ``OSError``,
        which is what the shim catches to return 127.
        """
        with pytest.raises(OSError):
            os.execv("/nonexistent/binary/for/this/test", ["x"])

    def test_an_ordinary_spawn_still_works(self) -> None:
        """Delegation is intact: the guard must not break git/npm/python spawns.

        Without this the whole fixture could be a blanket refusal and the suite
        above would still pass.
        """
        res = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
        assert res.returncode == 0
        assert res.stdout.strip() == "ok"

    def test_an_ordinary_async_spawn_still_works(self) -> None:
        async def go():
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "print('ok')", stdout=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            return out

        assert asyncio.run(go()).strip() == b"ok"


class TestLaunchdRedirect:
    """The macOS half does not resolve through XDG, so it needs its own pins."""

    def _assert_under(self, value, root) -> None:
        """Assert *value* lives inside the session tmp root.

        Containment in the tmp root, NOT "outside ``Path.home()``": on Windows the
        pytest temp dir is ``C:\\Users\\<user>\\AppData\\Local\\Temp\\pytest-of-...``,
        which is legitimately *inside* the home directory, so an "outside home"
        assertion encodes a POSIX-only assumption. Asserting the redirect target
        directly is both platform-correct and a stronger claim.
        """
        resolved = pathlib.Path(value).resolve()
        root = pathlib.Path(root).resolve()
        assert root == resolved or root in resolved.parents, f"{resolved} is not under {root}"

    def test_every_macos_install_path_is_redirected(self, _xdg_config_root) -> None:
        from kiro_crew.service import macos

        for attr in ("PLIST_DIR", "PLIST_PATH", "LOG_DIR", "STDOUT_LOG", "STDERR_LOG",
                     "LIVE_PROGRAM"):
            self._assert_under(getattr(macos, attr), _xdg_config_root)

    def test_the_launcher_path_helper_is_redirected(self, _xdg_config_root) -> None:
        """``LaunchdBackend.live_program()`` calls the function, not the constant."""
        from kiro_crew.service.common import launchd_live_program

        self._assert_under(launchd_live_program(), _xdg_config_root)

    def test_install_writes_the_launcher_into_tmp_not_the_real_home(
        self, _xdg_config_root
    ) -> None:
        """The exact leak raised in review: install() ignores the PLIST_* pins.

        ``macos.install()`` calls ``write_live_program(render_live_program(...))``
        with no path argument, so before this fixture existed the launcher landed on
        the real ``LIVE_PROGRAM`` even when a test had pinned every ``PLIST_*``
        constant. Subprocess is stubbed here because ``launchctl load`` is a
        mutating verb the execution guard would (correctly) refuse.
        """
        from kiro_crew.service import macos

        with mock.patch.object(macos, "_launchctl", return_value=SimpleNamespace(
            returncode=0, stdout="", stderr=""
        )), mock.patch.object(macos, "kirocrew_bin", return_value="/usr/bin/true"):
            macos.install()

        self._assert_under(macos.LIVE_PROGRAM, _xdg_config_root)
        assert macos.LIVE_PROGRAM.exists(), "install() should have written the tmp launcher"
        assert macos.PLIST_PATH.exists(), "install() should have written the tmp plist"


class TestXdgRedirect:
    def test_xdg_config_home_is_set_and_not_the_real_home(self) -> None:
        """The systemd drop-in path resolves from this variable."""
        xdg = os.environ.get("XDG_CONFIG_HOME")
        assert xdg, "XDG_CONFIG_HOME must be pinned so no test writes a real unit file"
        assert pathlib.Path(xdg).resolve() != (pathlib.Path.home() / ".config").resolve()

    def test_the_dropin_path_lands_outside_the_real_config_dir(self) -> None:
        """The exact call that caused #1722, now provably harmless.

        ``_dropin_path()`` is unstubbed here on purpose -- that is the whole point.
        A test that forgets to stub it must no longer be able to name the
        operator's real unit directory.
        """
        from kiro_crew.apps.builtins.dev_fleet import server as mod

        dropin = mod._dropin_path().resolve()
        real = (pathlib.Path.home() / ".config" / "systemd" / "user").resolve()
        assert real not in dropin.parents, f"{dropin} is inside the operator's real config dir"
