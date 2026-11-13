"""A reserved ``kirocrew-*`` entry a session mounts as declared launches the installed payload.

A user-level spec copied from ``kirocrew.json`` names the payload that wrote it
(``.../toolbox/kirocrew/<version>/.venv/bin/kirocrew``). Kiro Crew never rewrites
a spec it does not own, so once an upgrade removes that payload the entry names a
missing launcher. Where a key such as ``timeout`` keeps the entry
out of the per-session element (the KAS agent block, the kiro-cli skill view),
the session starts it as written and the server fails with ENOENT. Both paths
launch the managed invocation instead and keep the user's other keys.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from kas_agents_helpers import _spec

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_agents import hoist_servers_for_session, to_client_custom_agent
from kiro_crew.acp.skill_projection import project_agent_view
from kiro_crew.config.paths import data_home


def _payload_launcher(root, version: str) -> str:
    return str(root / "toolbox" / "kirocrew" / version / ".venv" / "bin" / "kirocrew")


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """The managed invocation resolves to the installed payload; the spec names a reaped one."""
    import kiro_crew.agent as agent_mod

    state = SimpleNamespace(
        launcher=_payload_launcher(tmp_path, "0.11.0.1"),
        reaped=_payload_launcher(tmp_path, "0.9.0.4"),
        calls=[],
        resolves=True,
    )

    def managed(name, *, include_opt_in=False):
        state.calls.append(name)
        if not state.resolves:
            return None
        return {"command": state.launcher, "args": ["mcp", name.removeprefix("kirocrew-")]}

    monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", managed)
    return state


def test_kas_relaunches_a_reserved_entry_its_timeout_keeps_in_the_agent_block(installed):
    spec = _spec(
        name="mtp-inputs",
        tools=["@kirocrew-core", "@kirocrew-guide"],
        mcpServers={
            "kirocrew-core": {"command": installed.reaped, "args": ["mcp", "core"]},
            "kirocrew-guide": {
                "command": installed.reaped,
                "args": ["mcp", "guide"],
                "timeout": 120000,
            },
        },
    )
    projected = to_client_custom_agent("mtp-inputs", spec, "p", session_key="cron:mtp")
    before = copy.deepcopy(projected)
    # Resolved off the event loop, as the KAS harness does before the hoist.
    resolved = kas_agents.resolve_managed_entries([projected], "mtp-inputs")
    installed.calls.clear()

    agents, array = hoist_servers_for_session(
        [projected], "mtp-inputs", [], session_token="tok", managed_entries=resolved
    )

    assert installed.calls == [], "the hoist resolves nothing on the event loop"
    assert projected == before, "the projection is not mutated"
    guide = agents[0]["mcpServers"]["kirocrew-guide"]
    assert guide["command"] == installed.launcher, "the reaped payload is not launched"
    assert guide["args"] == ["mcp", "guide"]
    assert guide["timeout"] == 120000, "the user's timeout still reaches KAS"
    assert guide["env"]["KIROCREW_SESSION_KEY"] == "cron:mtp", "the projected env stays"
    (core,) = [e for e in array if e["name"] == "kirocrew-core"]
    assert core["command"] == installed.launcher


def test_kas_leaves_a_block_entry_whose_managed_invocation_does_not_resolve(installed):
    installed.resolves = False
    entry = {"command": installed.reaped, "timeout": 5}
    projected = to_client_custom_agent(
        "mtp-inputs", _spec(mcpServers={"kirocrew-guide": entry}), "p"
    )
    agents, _ = hoist_servers_for_session([projected], "mtp-inputs", [], managed_entries={})
    assert agents[0] is projected
    assert agents[0]["mcpServers"]["kirocrew-guide"] == entry


@pytest.mark.parametrize(
    "per_session_element", [True, False], ids=["shared-runtime", "direct-client"]
)
def test_the_skill_view_relaunches_each_reserved_entry_and_keeps_its_keys(
    installed, tmp_path, per_session_element
):
    spec = {
        "name": "mtp-inputs",
        "prompt": "p",
        "tools": ["@kirocrew-cron", "@kirocrew-guide", "@third-party"],
        "mcpServers": {
            "kirocrew-cron": {
                "command": installed.reaped,
                "args": ["mcp", "cron"],
                "disabledTools": ["cron_remove"],
            },
            "kirocrew-guide": {
                "command": installed.reaped,
                "args": ["mcp", "guide"],
                "timeout": 120000,
                "env": {"KIROCREW_HOME": "/crew"},
            },
            "kirocrew-mate": {"url": "https://example.invalid/mcp"},
            "third-party": {"command": "/opt/tp", "args": ["serve"]},
        },
    }
    authored = copy.deepcopy(spec)

    view, _search, refusal = project_agent_view(
        spec,
        tmp_path / "mtp-inputs.json",
        "mtp-inputs",
        crew_home_id=data_home().absolute().as_posix(),
        inherits_skills=False,
        per_session_element=per_session_element,
    )

    assert refusal is None and view is not None
    servers = view["mcpServers"]
    assert servers["kirocrew-cron"] == {
        "command": installed.launcher,
        "args": ["mcp", "cron"],
        "disabledTools": ["cron_remove"],
    }
    assert servers["kirocrew-guide"] == {
        "command": installed.launcher,
        "args": ["mcp", "guide"],
        "timeout": 120000,
        "env": {"KIROCREW_HOME": "/crew"},
    }
    assert servers["kirocrew-mate"] == authored["mcpServers"]["kirocrew-mate"], "no launch"
    assert servers["third-party"] == authored["mcpServers"]["third-party"], "not Crew's"
    assert spec == authored, "the authored spec is not rewritten"


def test_the_skill_view_keeps_an_entry_whose_managed_invocation_does_not_resolve(
    installed, tmp_path
):
    installed.resolves = False
    entry = {"command": installed.reaped, "timeout": 5}
    view, _search, refusal = project_agent_view(
        {"name": "mtp-inputs", "prompt": "p", "mcpServers": {"kirocrew-guide": entry}},
        tmp_path / "mtp-inputs.json",
        "mtp-inputs",
        crew_home_id=data_home().absolute().as_posix(),
        inherits_skills=False,
        per_session_element=True,
    )
    assert refusal is None and view is not None
    assert view["mcpServers"]["kirocrew-guide"] == entry
