"""The first crewmate (key ``mate``, shown as Mate) is an ordinary crewmate.

Every path that renames, rebinds or removes a crew member treats it like any
other: the dashboard and the CLI delete it, a rename writes its label, it can
move to another template, and its name can be shared. Each test here fails if
one of the removed guards comes back.
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew import cli_commands as cc
from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.handlers.agents import (
    api_kirocrew_agent_delete,
    api_kirocrew_agent_update,
)
from kiro_crew.members import key_new_crew

ORDINARY = "scout"
DEFAULT = "kirocrew"


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _seed(label: str = "") -> None:
    cfg = KiroCrewConfig()
    cfg.agents = {
        DEFAULT: KiroCrewAgentConfig(kiro_agent=DEFAULT),
        ASSISTANT_MEMBER_NAME: KiroCrewAgentConfig(kiro_agent=DEFAULT, display_name=label),
        ORDINARY: KiroCrewAgentConfig(kiro_agent="oncall-agent"),
    }
    cfg.default_agent = DEFAULT
    cfg.save()


def _request(method: str, name: str, body: dict | None = None):
    request = MagicMock(spec=web.Request)
    request.method = method
    request.match_info = {"name": name}
    request.app = {"state": None}
    request.get = lambda key, default=None: default

    async def _json():
        return body or {}

    request.json = _json
    return request


@pytest.mark.asyncio
async def test_the_dashboard_deletes_the_first_crewmate():
    _seed()
    resp = await api_kirocrew_agent_delete(_request("DELETE", ASSISTANT_MEMBER_NAME))
    assert resp.status == 200
    assert ASSISTANT_MEMBER_NAME not in KiroCrewConfig.load().agents


def test_the_cli_deletes_the_first_crewmate(capsys):
    _seed()
    cc._handle_agent(argparse.Namespace(agent_action="delete", name=ASSISTANT_MEMBER_NAME))
    assert f"Deleted agent: {ASSISTANT_MEMBER_NAME}" in capsys.readouterr().out
    assert ASSISTANT_MEMBER_NAME not in KiroCrewConfig.load().agents


@pytest.mark.asyncio
async def test_renaming_the_first_crewmate_writes_its_label():
    _seed()
    resp = await api_kirocrew_agent_update(
        _request("PUT", ASSISTANT_MEMBER_NAME, {"display_name": "Skipper"})
    )
    assert resp.status == 200
    assert KiroCrewConfig.load().agents[ASSISTANT_MEMBER_NAME].display_name == "Skipper"


@pytest.mark.asyncio
async def test_the_first_crewmate_can_move_to_another_template():
    _seed()
    resp = await api_kirocrew_agent_update(
        _request("PUT", ASSISTANT_MEMBER_NAME, {"kiro_agent": "oncall-agent"})
    )
    assert resp.status == 200
    assert KiroCrewConfig.load().agents[ASSISTANT_MEMBER_NAME].kiro_agent == "oncall-agent"


@pytest.mark.asyncio
async def test_another_crewmate_may_take_the_first_crewmates_label():
    _seed("Mate")
    resp = await api_kirocrew_agent_update(_request("PUT", ORDINARY, {"display_name": "Mate"}))
    assert resp.status == 200
    assert KiroCrewConfig.load().agents[ORDINARY].display_name == "Mate"


def test_the_key_and_the_label_are_free_once_it_is_gone():
    keyed = key_new_crew(ASSISTANT_MEMBER_NAME, "", {})
    assert (keyed.key, keyed.taken) == (ASSISTANT_MEMBER_NAME, "")
    assert key_new_crew("Mate", "", {}).taken == ""
