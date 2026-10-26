"""``rebuild_agent_config`` must not swallow ``PlatformCompositionError``.

Every builtin installer reachable from ``rebuild_agent_config`` derives its spec
from ``build_agent_config()``, which reads the platform context through
``safe_context_call``. That helper re-raises :class:`PlatformCompositionError` as
a fail-closed signal (a non-standalone host that cannot compose its companion must
abort rather than silently keep a stale, wrongly-ceilinged agent config). The
``safe_context_call`` contract exists so a call site "cannot accidentally swallow"
that signal. Each installer wrapper in ``rebuild_agent_config`` therefore guards
its blanket ``except Exception`` with an ``except PlatformCompositionError`` arm
ahead of it, so the fail-closed signal is never caught there.

These tests pin the seam: a ``PlatformCompositionError`` raised by any one
installer propagates out of ``rebuild_agent_config``, while any other exception
from the same installer is still logged and swallowed so one agent's install
problem never aborts the whole rebuild.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent
from kiro_crew.agent_materialization import (
    conductor_agents,
    service_agents,
    worker_agent,
)
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.platform import PlatformCompositionError

# Each entry: the installer's name as a user reads it, the module object that owns
# the attribute ``rebuild_agent_config`` calls, and the attribute name on it. The
# module/attribute pair is the seam a test patches, because the code calls e.g.
# ``service_agents._install_knowledge_agent()`` -- the binding it reads is the
# attribute on that module, not a local import.
_INSTALLERS: tuple[tuple[str, Any, str], ...] = (
    ("knowledge", service_agents, "_install_knowledge_agent"),
    ("research", service_agents, "_install_research_agent"),
    ("mate-conductor", service_agents, "_install_mate_conductor_agent"),
    ("mate-worker", service_agents, "_install_mate_worker_agent"),
    ("team-lead", service_agents, "_install_team_lead_agent"),
    ("dashboard-manager", service_agents, "_install_dashboard_manager_agent"),
    ("heartbeat", agent, "_install_heartbeat_agent"),
    ("conductor", conductor_agents, "_install_conductor_agent"),
    ("pipeline-conductor", conductor_agents, "_install_pipeline_conductor_agent"),
    ("ledger-conductor", conductor_agents, "_install_ledger_conductor_agent"),
    ("security-conductor", conductor_agents, "_install_security_conductor_agent"),
    ("dashboard-author", worker_agent, "_install_dashboard_author_agent"),
    ("worker", worker_agent, "_install_worker_agent"),
)


class _Rig:
    """Make ``rebuild_agent_config`` runnable against a throwaway agents dir.

    Mirrors ``test_agent_refactor_contracts._Rig``: redirect every host path the
    rebuild touches into ``tmp_path`` and pin the kiro-cli version so the spec
    writers run their modern path. Nothing it creates outlives the test.
    """

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.agents = tmp_path / "agents"
        self.agents.mkdir()
        binary = tmp_path / "bin" / "kirocrew"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        self.home = Path(os.environ["KIROCREW_HOME"])
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", self.agents)
        monkeypatch.setattr(agent, "_KIROCREW_BIN", str(binary))
        monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "kiro-global-mcp.json")
        monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", tmp_path / "no-hooks")
        monkeypatch.setattr(
            "kiro_crew.apps.bridges._mcp_json_path", lambda: self.agents / "kirocrew.json"
        )
        monkeypatch.setattr(
            "kiro_crew.kiro_cli.installed_kiro_cli_version",
            lambda: SPEC_PERMISSIONS_MIN_VERSION,
        )


def test_a_clean_rebuild_succeeds_under_the_rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rig is a real rebuild -- the control the fail-closed tests build on.

    Without this, a rebuild that raised for an unrelated environment reason would
    make the ``raises`` tests below pass for the wrong reason.
    """
    _Rig(tmp_path, monkeypatch)
    path = agent.rebuild_agent_config()
    assert Path(path).exists()


@pytest.mark.parametrize(
    "label,module,attr",
    _INSTALLERS,
    ids=[label for label, _module, _attr in _INSTALLERS],
)
def test_rebuild_propagates_composition_error_from_each_installer(
    label: str,
    module: Any,
    attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``PlatformCompositionError`` from any installer aborts the rebuild.

    Keeping a stale agent config on a host whose companion cannot compose is the
    exact fail-closed failure the ``safe_context_call`` contract forbids, so a bare
    ``except Exception`` on the wrapper must not catch it.
    """
    _Rig(tmp_path, monkeypatch)

    def _raise_composition(*_args: Any, **_kwargs: Any) -> Any:
        raise PlatformCompositionError(f"{label}: companion unavailable")

    monkeypatch.setattr(module, attr, _raise_composition)

    with pytest.raises(PlatformCompositionError, match=f"{label}: companion unavailable"):
        agent.rebuild_agent_config()


@pytest.mark.parametrize(
    "label,module,attr",
    # dashboard-author already re-raises every exception by design (it captures the
    # error, finishes the independent repairs, then re-raises at the end), so a
    # plain error from it is NOT swallowed. Its composition case is covered above;
    # it is excluded only from this "other errors are swallowed" control.
    [row for row in _INSTALLERS if row[0] != "dashboard-author"],
    ids=[label for label, _module, _attr in _INSTALLERS if label != "dashboard-author"],
)
def test_rebuild_still_swallows_other_errors_from_each_installer(
    label: str,
    module: Any,
    attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-composition installer failure is logged and swallowed, as before.

    The fix narrows the wrappers to the fail-closed signal only: an ordinary
    installer error (a read-only file, a refused rename) must not turn into a
    whole-rebuild abort, so the rebuild still completes and writes the main spec.
    """
    _Rig(tmp_path, monkeypatch)

    def _raise_runtime(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"{label}: transient install failure")

    monkeypatch.setattr(module, attr, _raise_runtime)

    path = agent.rebuild_agent_config()
    assert Path(path).exists()
    main_spec = json.loads(Path(path).read_text(encoding="utf-8"))
    assert main_spec.get("name") == "kirocrew"


def test_the_independent_repairs_run_before_the_composition_error_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deferred composition error does not skip the companion-agnostic repairs.

    The signal is captured and re-raised at the end of the rebuild so the
    independent repairs -- the ``repair_agent_configs`` hook-key sanitization and
    the fork refresh -- still run on the way out, the same contract the
    dashboard-author installer's capture-then-raise already honours. An early
    installer raising must not rob a later security repair of its run.
    """
    _Rig(tmp_path, monkeypatch)

    def _raise_composition(*_args: Any, **_kwargs: Any) -> Any:
        raise PlatformCompositionError("knowledge: companion unavailable")

    # The first installer in the sequence, so an in-place raise would skip the most.
    monkeypatch.setattr(service_agents, "_install_knowledge_agent", _raise_composition)

    repair_ran: list[bool] = []
    real_repair = agent.repair_agent_configs

    def _spy_repair() -> None:
        repair_ran.append(True)
        real_repair()

    monkeypatch.setattr(agent, "repair_agent_configs", _spy_repair)

    with pytest.raises(PlatformCompositionError, match="knowledge: companion unavailable"):
        agent.rebuild_agent_config()

    assert repair_ran == [True], "repair_agent_configs must run before the rebuild fails closed"
