"""Which members owe a first welcome: only the Mate row the first-crewmate step made.

The first-crewmate step records the welcome Mate owes
(:func:`kiro_crew.members.mark_welcome_owed`); the greet route reads that
record. A crewmate created on the dashboard greets through that create flow's
own seeded turn, so ``POST /api/agents`` records nothing, and neither does
``kirocrew agent create`` or a member that reaches the roster any other way
(discovery, an import, an app, a hand edit). Every test writes under the
isolated data home.
"""

from __future__ import annotations

import json
import unittest.mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import members
from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, config_path


def _owed(name: str) -> bool:
    cfg = KiroCrewConfig.load()
    return members.welcome_owed(members.member_slug(name, cfg), name)


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


@pytest.mark.asyncio
async def test_a_crewmate_created_on_the_dashboard_owes_no_welcome_here() -> None:
    from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

    app = web.Application()
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/agents", json={"name": "reviewer", "kiro_agent": "kirocrew"})
        assert resp.status == 200, await resp.text()
    assert _owed("reviewer") is False


def test_a_crewmate_created_from_the_cli_owes_no_welcome() -> None:
    from kiro_crew.cli import main

    with unittest.mock.patch("sys.argv", ["kirocrew", "agent", "create", "--name", "research"]):
        main()
    assert _owed("research") is False


def test_the_first_crewmate_owes_its_welcome() -> None:
    from kiro_crew.agent_materialization import first_crewmate

    config_path().write_text(
        json.dumps({"agents": {"default": {"kiro_agent": "kirocrew"}}}), encoding="utf-8"
    )
    first_crewmate.create_first_crewmate_once()
    assert _owed(ASSISTANT_MEMBER_NAME) is True


def test_an_upgrade_with_other_crewmates_still_gets_mate_once() -> None:
    """An existing install with its own crewmates gains Mate, owed its welcome, once."""
    from kiro_crew.agent_materialization import first_crewmate

    config_path().write_text(
        json.dumps(
            {
                "agents": {
                    "default": {"kiro_agent": "kirocrew"},
                    "scout": {"kiro_agent": "kirocrew", "display_name": "Scout"},
                }
            }
        ),
        encoding="utf-8",
    )
    first_crewmate.create_first_crewmate_once()
    saved = json.loads(config_path().read_text(encoding="utf-8"))["agents"]
    assert set(saved) == {"default", "scout", ASSISTANT_MEMBER_NAME}
    assert saved["scout"] == {"kiro_agent": "kirocrew", "display_name": "Scout"}
    assert _owed(ASSISTANT_MEMBER_NAME) is True


def test_a_member_that_arrived_any_other_way_owes_nothing() -> None:
    cfg = KiroCrewConfig.load()
    cfg.agents["imported"] = KiroCrewAgentConfig(kiro_agent="kirocrew", source="package")
    cfg.save()
    assert _owed("imported") is False


def test_an_existing_first_crewmate_key_is_not_given_a_welcome() -> None:
    from kiro_crew.agent_materialization import first_crewmate

    config_path().write_text(
        json.dumps(
            {
                "agents": {
                    "default": {"kiro_agent": "kirocrew"},
                    ASSISTANT_MEMBER_NAME: {"kiro_agent": "my-template"},
                }
            }
        ),
        encoding="utf-8",
    )
    first_crewmate.create_first_crewmate_once()
    assert _owed(ASSISTANT_MEMBER_NAME) is False
