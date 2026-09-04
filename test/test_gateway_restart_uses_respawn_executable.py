"""Every gateway restart-after-update path must exec ``respawn_executable()``.

``sys.executable`` is cached at process start. After an auto-update replaces a
managed install, that path can point into the tree the update just pruned, so
``os.execv`` on it dies with ENOENT and the gateway never comes back. The
dashboard's update handler already routes through
``kiro_crew.platform.wheel_engine.respawn_executable``; these tests pin the
three restart sites in ``GatewayOrchestrator`` to the same rule.

Each test drives the REAL method to its exec line: the download / apply /
breadcrumb steps ahead of it are stubbed, and only the two seams under test are
replaced -- the interpreter lookup (returns a sentinel) and the exec itself
(recorded instead of replacing the test process).
"""

from __future__ import annotations

import ast
import inspect
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack import gateway as gw
from kiro_crew.slack.gateway import GatewayOrchestrator

_STABLE_INTERPRETER = "/managed/current/bin/python-sentinel"


def _make_orchestrator() -> GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U1"}):
        orch = GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    # No dashboard slots to save, no sessions to close: the tests are about the
    # exec, not the teardown that precedes it.
    orch.dashboard_state = None
    orch.sessions = None
    return orch


@pytest.fixture
def exec_seams(monkeypatch):
    """Replace the interpreter lookup and the exec; return both recorders."""
    respawn = MagicMock(return_value=_STABLE_INTERPRETER)
    monkeypatch.setattr("kiro_crew.platform.wheel_engine.respawn_executable", respawn)
    reexec = MagicMock()
    monkeypatch.setattr("kiro_crew.platform_compat.reexec_python_module", reexec)
    # The breadcrumb drain hits the real safety-override store; not under test.
    monkeypatch.setattr(gw, "flush_breadcrumb_writes", lambda *_a, **_k: None)
    return respawn, reexec


@pytest.fixture
def update_info():
    """Hand a test the process-global update cache, restored on teardown.

    ``dashboard.handlers.updates._update_info`` is module-level state shared by
    every test in the worker (the convention ``test_get_update_info.py`` also
    follows). A test that repopulates it must not leave its shape behind for
    whatever runs next.
    """
    from kiro_crew.dashboard.handlers import updates

    original = dict(updates._update_info)
    yield updates._update_info
    updates._update_info.clear()
    updates._update_info.update(original)


def _assert_execs_the_respawn_interpreter(respawn: MagicMock, reexec: MagicMock) -> None:
    reexec.assert_called_once()
    args, kwargs = reexec.call_args
    assert args[0] == "kiro_crew"
    assert list(args[1]) == sys.argv[1:]
    assert kwargs.get("executable") == _STABLE_INTERPRETER, (
        "restart exec'd sys.executable instead of respawn_executable(); after an "
        "update pruned the old install that path no longer exists"
    )
    respawn.assert_called_once_with()


class TestRestartAfterUpdate:
    @pytest.mark.asyncio
    async def test_execs_the_respawn_interpreter(self, exec_seams):
        respawn, reexec = exec_seams
        orch = _make_orchestrator()

        await orch._restart_after_update(respawn)

        _assert_execs_the_respawn_interpreter(respawn, reexec)


class TestResolverIsLoadedBeforeTheApply:
    """The resolver must be in memory before the update rewrites the install.

    The legacy-nested-venv migration ``respawn_executable`` exists for deletes
    the venv this process imports from; a ``from ... import`` placed after the
    apply would raise ``ModuleNotFoundError`` and leave a closed-session
    gateway un-restarted. Pin the ORDER inside each restart path: the import
    precedes the first apply/spawn, and the restart helper imports nothing
    from wheel_engine at all.
    """

    @staticmethod
    def _method(name: str) -> ast.AsyncFunctionDef:
        tree = ast.parse(inspect.getsource(gw))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{name} not found in gateway.py")

    @staticmethod
    def _resolver_import_lines(fn: ast.AST) -> list[int]:
        return [
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.ImportFrom)
            and node.module == "kiro_crew.platform.wheel_engine"
            and any(alias.name == "respawn_executable" for alias in node.names)
        ]

    @staticmethod
    def _first_call_line(fn: ast.AST, attr_names: set[str]) -> int:
        lines = [
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in attr_names
        ]
        assert lines, f"no apply/spawn call found among {sorted(attr_names)}"
        return min(lines)

    @pytest.mark.parametrize(
        ("method", "apply_attrs"),
        [
            ("_check_for_updates_via_provider", {"apply"}),
            ("_auto_apply_update", {"create_subprocess_exec"}),
            ("_auto_apply_wheel_update", {"create_subprocess_exec"}),
        ],
    )
    def test_import_precedes_the_first_apply(self, method, apply_attrs):
        fn = self._method(method)
        imports = self._resolver_import_lines(fn)
        assert len(imports) == 1, f"{method}: expected one resolver import, got {imports}"
        first_apply = self._first_call_line(fn, apply_attrs)
        assert imports[0] < first_apply, (
            f"{method}: respawn_executable is imported at line {imports[0]}, after "
            f"the apply at line {first_apply}; a post-apply import reads a venv "
            "the update may have deleted"
        )

    def test_restart_helper_does_not_import_the_resolver(self):
        fn = self._method("_restart_after_update")
        assert self._resolver_import_lines(fn) == [], (
            "_restart_after_update must take the resolver from its caller, which "
            "imported it before the apply"
        )

    def test_every_reexec_in_the_gateway_passes_an_explicit_executable(self):
        """Discovered, not enumerated — this is what polices a FOURTH site.

        The parametrized order check above names its three methods, so a new
        restart path added later would not be examined by it. The defect this
        PR fixes has a shape that can be found without knowing where it lives:
        a ``reexec_python_module`` call with no ``executable=`` re-execs the
        cached ``sys.executable``, which an update may have pruned. Walk every
        such call in gateway.py instead of a list of known ones.
        """
        tree = ast.parse(inspect.getsource(gw))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "reexec_python_module"
        ]
        assert calls, "expected at least one reexec_python_module call in gateway.py"
        missing = [
            node.lineno for node in calls if not any(kw.arg == "executable" for kw in node.keywords)
        ]
        assert not missing, (
            f"reexec_python_module at line(s) {missing} in gateway.py passes no "
            "executable=; it would re-exec sys.executable, which an update can "
            "prune. Resolve the interpreter with wheel_engine.respawn_executable() "
            "(imported before the apply step) and pass it as executable=."
        )


class TestAutoApplyWheelUpdate:
    @pytest.mark.asyncio
    async def test_successful_install_execs_the_respawn_interpreter(
        self, exec_seams, update_info, monkeypatch
    ):
        respawn, reexec = exec_seams
        orch = _make_orchestrator()

        update_info.clear()
        update_info.update(
            {
                "remediation": {
                    "kind": "command",
                    "message": "Re-run the installer to upgrade.",
                    "command": "sh -c true",
                }
            }
        )
        # Same pins the wheel-apply tests in test_slack_gateway.py use to reach
        # the spawn on any host: POSIX platform, a trusted `sh`, a trusted PATH.
        monkeypatch.setattr("kiro_crew.platform.update_layout.cdn_bases_are_safe", lambda: True)
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason", lambda _base: None
        )
        monkeypatch.setattr("kiro_crew.slack.gateway.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_bin", lambda name: "/bin/sh")
        monkeypatch.setattr(
            "kiro_crew.platform.update_provider._trusted_path_env",
            lambda: {"PATH": "/usr/bin:/bin"},
        )

        proc = MagicMock()
        proc.returncode = 0  # the installer succeeded: the method must restart
        proc.stdout = None
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        spawn = AsyncMock(return_value=proc)
        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)

        await orch._auto_apply_wheel_update()

        spawn.assert_awaited_once()
        _assert_execs_the_respawn_interpreter(respawn, reexec)


class TestAutoApplyGitUpdate:
    """The git-checkout path: fetch, reset, rebuild, reinstall, then exec."""

    @staticmethod
    def _scripted_git(monkeypatch) -> AsyncMock:
        """A ``create_subprocess_exec`` that answers each git step as a clean,
        behind-origin checkout would, and refuses any spawn it does not expect."""

        def _proc(rc: int, stdout: bytes = b"") -> MagicMock:
            proc = MagicMock()
            proc.returncode = rc
            proc.communicate = AsyncMock(return_value=(stdout, b""))
            proc.wait = AsyncMock(return_value=rc)
            return proc

        async def _spawn(*argv, **_kwargs):
            words = [str(a) for a in argv[1:]]
            if words[:2] == ["rev-parse", "--abbrev-ref"]:
                return _proc(0, b"main\n")
            if words[:1] == ["fetch"]:
                return _proc(0)
            if words[:2] == ["rev-parse", "--verify"]:
                return _proc(0, b"0123456789abcdef0123456789abcdef01234567\n")
            if words[:1] == ["diff"] and "--quiet" in words:
                return _proc(1)  # HEAD differs from origin: an update is pending
            if words[:2] == ["status", "--porcelain"]:
                return _proc(0)  # clean tracked tree
            if words[:2] == ["diff", "--name-only"]:
                return _proc(0)  # the target adds no colliding paths
            if words[:2] == ["reset", "--hard"]:
                return _proc(0)
            raise AssertionError(f"unexpected spawn on the git update path: {argv!r}")

        spawn = AsyncMock(side_effect=_spawn)
        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
        return spawn

    @pytest.mark.asyncio
    async def test_successful_update_execs_the_respawn_interpreter(
        self, exec_seams, monkeypatch, tmp_path
    ):
        respawn, reexec = exec_seams
        orch = _make_orchestrator()

        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: "/usr/bin/git")
        # The pre-reset refusals, each answered the way a clean checkout on the
        # primary branch answers them. Their own behaviour is pinned elsewhere.
        monkeypatch.setattr(gw, "git_command_env", lambda: {})
        monkeypatch.setattr(gw, "is_primary_branch", lambda _branch: True)
        monkeypatch.setattr(gw, "repo_exec_config_reason", lambda _proj: "")
        monkeypatch.setattr(gw, "tracks_upstream", lambda _proj, _branch: True)
        monkeypatch.setattr(gw, "resolve_remote_url", lambda _proj, remote: "https://example/r.git")
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason", lambda _url: None
        )
        monkeypatch.setattr(gw, "commits_ahead", lambda _proj, _target: 0)
        monkeypatch.setattr(gw, "hidden_worktree_edits", lambda _proj: [])
        spawn = self._scripted_git(monkeypatch)
        # Post-reset rebuild steps: no optional backend, frontend and deps sync
        # come back clean, and the package reload is a no-op (reloading the real
        # package inside the test process is not what is under test).
        monkeypatch.setattr(gw, "_pinned_kiro_cli", AsyncMock(return_value=None))
        monkeypatch.setattr(gw, "build_frontend_async", AsyncMock())
        monkeypatch.setattr(gw.dep_sync, "sync_or_reinstall", lambda *_a, **_k: 0)
        monkeypatch.setattr(gw, "importlib", SimpleNamespace(reload=lambda module: module))

        await orch._auto_apply_update()

        # The scripted git ran through to the reset: the exec below is the
        # restart after a real apply, not an early return.
        spawned = [[str(a) for a in call.args[1:3]] for call in spawn.await_args_list]
        assert ["reset", "--hard"] in spawned
        _assert_execs_the_respawn_interpreter(respawn, reexec)
