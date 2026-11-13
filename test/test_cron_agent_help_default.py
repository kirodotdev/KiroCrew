"""The cron ``agent`` help names the agent an agent-less job really runs.

An agent-less LLM job dispatches ``agent.default_agent`` and degrades to the
``kirocrew`` floor only when that is unset
(``slack/gateway_runtime/cron_fire.py``; learn-cron-dashboard spec,
"Per-agent cron"). Each surface that describes the omitted agent must say so
rather than promise the bare kirocrew agent.
"""

from __future__ import annotations

import argparse

import pytest

from kiro_crew import cli_parser_agent
from kiro_crew.mcp_cron import _list_tools


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) and name in action.choices:
            return action.choices[name]
    raise AssertionError(f"no subcommand {name!r}")


def _agent_help(action_name: str) -> str:
    root = argparse.ArgumentParser(prog="kirocrew")
    cli_parser_agent.register_cron_parser(root.add_subparsers(dest="command"))
    cron = _subparser(_subparser(root, "cron"), action_name)
    for action in cron._actions:
        if "--agent" in action.option_strings:
            return str(action.help)
    raise AssertionError(f"cron {action_name} has no --agent option")


def _cron_add_agent_description() -> str:
    for tool in _list_tools():
        if tool["name"] == "cron_add":
            return str(tool["inputSchema"]["properties"]["agent"]["description"])
    raise AssertionError("no cron_add tool")


_SURFACES = {
    "cli-cron-add": lambda: _agent_help("add"),
    "cli-cron-update": lambda: _agent_help("update"),
    "mcp-cron_add": _cron_add_agent_description,
}


@pytest.mark.parametrize("surface", sorted(_SURFACES))
def test_the_omitted_agent_is_described_as_agent_default_agent(surface: str) -> None:
    text = _SURFACES[surface]()
    assert "agent.default_agent" in text
    # The default is not always substituted (cron_fire.py's gates, e.g. a
    # default declaring availableAgents keeps the floor), so the text must not
    # promise it unconditionally.
    assert "the kirocrew agent when that is unset or not eligible" in text
    assert "default kirocrew agent" not in text
