"""Every restart that follows an update must exec ``respawn_executable()``.

``sys.executable`` is cached at process start. After an update replaces a
managed install, that path can point into the tree the update just pruned, so
``os.execv`` on it dies with ENOENT and the gateway never comes back. The
dashboard restart and the boot-time apply of an armed update route through
``kiro_crew.platform.wheel_engine.respawn_executable``; these tests pin every
re-exec site to the same rule, and that the scheduler's staged build execs nothing.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from wheel_update_test_helpers import wire_wheel_apply

from kiro_crew import platform_compat, update_boot_apply
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import updates as dashboard_updates
from kiro_crew.slack import gateway as gw
from kiro_crew.slack.gateway import GatewayOrchestrator

#: Basename of the stand-in interpreter ``respawn_executable`` is made to answer.
#: It must be a REAL executable file on disk, not a synthetic pathname: the restart
#: paths establish that the interpreter exists before they tear anything down, so a
#: path that resolves to nothing exercises the deferral branch instead of the exec
#: these tests are about. The fixture below creates it under ``tmp_path`` and it stays
#: distinguishable from ``sys.executable``, which is the whole point of the assertion.
_STABLE_INTERPRETER_NAME = "managed-current-python-sentinel"


def _gateway_source() -> str:
    """The facade's text followed by its composed owners' (``gateway_runtime/*.py``)."""
    facade = Path(gw.__file__)
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (facade, *sorted((facade.parent / "gateway_runtime").glob("*.py")))
    )


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
def exec_seams(monkeypatch, tmp_path):
    """Replace the interpreter lookup and the exec; return both recorders.

    The interpreter is a real executable file, so the restart paths' existence
    check passes and they proceed to the exec. ``.exe`` keeps it executable on
    Windows too, where a POSIX mode bit says nothing.
    """
    interpreter = tmp_path / f"{_STABLE_INTERPRETER_NAME}.exe"
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    platform_compat.chmod_safe(interpreter, 0o700)
    respawn = MagicMock(return_value=str(interpreter))
    monkeypatch.setattr("kiro_crew.platform.wheel_engine.respawn_executable", respawn)
    reexec = MagicMock()
    monkeypatch.setattr("kiro_crew.platform_compat.reexec_python_module", reexec)
    # The breadcrumb drain hits the real safety-override store; not under test.
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.updates.flush_breadcrumb_writes", lambda *_a, **_k: None
    )
    return respawn, reexec, str(interpreter)


def _assert_execs_the_respawn_interpreter(
    respawn: MagicMock, reexec: MagicMock, interpreter: str
) -> None:
    reexec.assert_called_once()
    args, kwargs = reexec.call_args
    assert args[0] == "kiro_crew"
    assert list(args[1]) == sys.argv[1:]
    assert kwargs.get("executable") == interpreter, (
        "restart exec'd sys.executable instead of respawn_executable(); after an "
        "update pruned the old install that path does not exist"
    )
    assert interpreter != sys.executable, (
        "the fixture interpreter must differ from sys.executable or the assertion "
        "above cannot tell the two resolutions apart"
    )
    respawn.assert_called_once_with()


class TestEveryReexecPassesAnExplicitExecutable:
    """Discovered, not enumerated: this is what polices a NEW restart site.

    A ``reexec_python_module`` call with no ``executable=`` re-execs the cached
    ``sys.executable``, which an update may have pruned. Walk every such call in the
    gateway (the facade and its composed owners), the dashboard restart and the
    boot-time apply, instead of a list of known ones.
    """

    @staticmethod
    def _calls(source: str) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "reexec_python_module"
        ]

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(_gateway_source, id="gateway"),
            pytest.param(
                lambda: "\n".join(
                    inspect.getsource(sys.modules[name])
                    for name in (dashboard_updates.__name__, *dashboard_updates._OWNERS)
                ),
                id="dashboard-restart",
            ),
            pytest.param(lambda: inspect.getsource(update_boot_apply), id="boot-apply"),
        ],
    )
    def test_no_reexec_falls_back_to_sys_executable(self, source):
        missing = [
            node.lineno
            for node in self._calls(source())
            if not any(kw.arg == "executable" for kw in node.keywords)
        ]

        assert not missing, (
            f"reexec_python_module at line(s) {missing} passes no executable=; it would "
            "re-exec sys.executable, which an update can prune. Resolve the interpreter "
            "with wheel_engine.respawn_executable() and pass it as executable=."
        )

    def test_the_boot_apply_has_a_reexec_to_police(self):
        assert self._calls(inspect.getsource(update_boot_apply))

    def test_the_gateway_itself_no_longer_restarts_after_an_update(self):
        assert self._calls(_gateway_source()) == []


class TestAutoApplyWheelUpdate:
    @pytest.mark.asyncio
    async def test_successful_install_stages_and_execs_nothing(self, exec_seams, monkeypatch):
        """No unattended path restarts a running gateway: the promoted tree waits."""
        respawn, reexec, _interpreter = exec_seams
        orch = _make_orchestrator()
        # The shadow engine is the apply; it promoted the new tree.
        apply = wire_wheel_apply(monkeypatch)

        await orch._auto_apply_wheel_update("stable", "9.9.9")

        apply.assert_called_once()
        reexec.assert_not_called()
        respawn.assert_not_called()
