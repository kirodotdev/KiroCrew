"""``POST /api/kiro-hooks/rescan``: the owner picks up a new hooks-folder script without a restart.

Autoimport scans ``~/.kiro/hooks`` only while the agent specs are written, which on its
own happens at gateway start. A script added after that reaches the spec once the owner
rescans; a caller who is not the owner leaves the spec as the start wrote it.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from webhooks_api_helpers import _payload, _req

from kiro_crew import agent
from kiro_crew.dashboard.handlers import hooks as H

pytestmark = pytest.mark.asyncio


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The agents dir as a gateway start wrote it, over an empty hooks folder."""
    agents = tmp_path / "agents"
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents)
    monkeypatch.setattr(agent, "_decline_shared_agent_home", lambda **_k: None)
    monkeypatch.setattr(H, "_sel", lambda: MagicMock())
    # The conftest pins the default hooks folder to this test's own directory.
    agent._DEFAULT_KIRO_HOOKS_DIR.mkdir(parents=True)
    agent.rebuild_agent_config()
    return agents


def _add_script() -> str:
    script = agent._DEFAULT_KIRO_HOOKS_DIR / "guard.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script.resolve())


def _spec_commands(agents: Path) -> list[str]:
    spec = json.loads((agents / agent.AGENT_FILENAME).read_text(encoding="utf-8"))
    return [entry["command"] for entry in spec["hooks"].get("preToolUse", [])]


async def test_an_owner_rescan_puts_a_new_script_in_the_spec(agents_dir):
    script = _add_script()
    assert script not in _spec_commands(agents_dir)

    resp = await H.api_kiro_hooks_rescan(_req("POST", "/api/kiro-hooks/rescan"))

    assert resp.status == 200
    assert (await _payload(resp))["agent_spec_rewritten"] is True
    assert script in _spec_commands(agents_dir)


async def test_a_caller_who_is_not_the_owner_is_refused(agents_dir):
    script = _add_script()

    resp = await H.api_kiro_hooks_rescan(
        _req("POST", "/api/kiro-hooks/rescan", user="U0NONOWNER", owner_id="U0THEOWNER")
    )

    assert resp.status == 403
    assert (await _payload(resp))["code"] == "owner_only"
    assert script not in _spec_commands(agents_dir)
