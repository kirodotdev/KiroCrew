"""Shared harness for ``test_agent.py`` and the ``test_agent_*.py`` modules split out of it.

Every one of those modules imports the autouse ``_pinned_kiro_cli_version``, so
each runs under the same autouse set. ``_run_install`` runs ``install_agent``
with the ``kiro_crew.agent`` globals patched under ``tmp_path``.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

# Shared with ``test_mcp_rebuild_reconsumption`` via a dedicated helpers module --
# the repo's convention, since a test module is not importable from another one.
# Re-exported under the original private names so the call sites below are unchanged.
from mcp_merge_helpers import DEFAULT_MANAGED_MCPS as _DEFAULT_MANAGED_MCPS

from kiro_crew.agent import install_agent
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse pin of test_agent.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _pinned_kiro_cli_version(monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    Every spec write here reaches ``_write_derived_permissions``, which reads
    ``installed_kiro_cli_version`` function-locally from ``kiro_crew.kiro_cli``:
    one real ``kiro-cli --version`` spawn per binary identity, process-cached, so
    whichever test in the worker writes a spec first pays it -- against the
    HOST's install, with the checkout as the child's cwd -- and that host decides
    whether the block is written at all (CI has no binary and reads "refuse").
    Pinned to the floor release, the same accepting arm ``test_agent_capabilities``
    and the generated-writer suites pin, so no test here reaches the binary.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


def _run_install(  # type: ignore[return]
    tmp_path: Path,
    cfg_dir: Path,
    managed_mcps: dict | None = None,
    which: "object | None" = None,
    **kwargs,
) -> Path:
    """Run install_agent with all module globals patched to tmp_path.

    ``which`` overrides the stubbed command resolver, which otherwise echoes
    every command so each declared MCP server resolves. Pass one to reach the
    rebuild's "a command was declared and did not resolve" branch, which the
    echoing stub makes unreachable.
    """
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    prompt = cfg_dir / "prompt.md"

    # Isolate tests from the caller's real ~/.kiro/hooks/ by disabling
    # autoimport in the patched config.  Tests that want to exercise autoimport
    # should override config_path themselves.
    mc_config = tmp_path / "empty_mc_config.json"
    if not mc_config.exists():
        mc_config.write_text(json.dumps({"agent": {"kiro_hooks_autoimport": False}}))

    _user_home = tmp_path / "kirocrew_home"
    patches = [
        patch.multiple(
            "kiro_crew.agent",
            KIRO_AGENTS_DIR=kiro_dir,
            _BUNDLED_CFG_DIR=cfg_dir,
            _KIROCREW_BIN="/usr/bin/kirocrew",
            _MANAGED_MCP_SERVERS=(
                managed_mcps if managed_mcps is not None else _DEFAULT_MANAGED_MCPS
            ),
            _KIRO_MCP_JSON=tmp_path / "fake_kiro_mcp.json",
            _CC_MCP_JSON=tmp_path / "fake_cc_mcp.json",
        ),
        patch("kiro_crew.agent._user_dir", lambda: _user_home),
        patch("kiro_crew.agent._prompt_path", return_value=prompt),
        patch("kiro_crew.agent._shipped_defaults", return_value=cfg_dir / "defaults.json"),
        patch("kiro_crew.agent._project_dir", return_value=None),
        patch("kiro_crew.agent._aim_skill_paths", return_value=[]),
        patch(
            "kiro_crew.agent.shutil.which",
            side_effect=which if which is not None else (lambda c, **kw: c),
        ),
        patch("kiro_crew.agent._mc_config_path", return_value=mc_config),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return install_agent(**kwargs)
