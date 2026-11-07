"""KAS carries a spec's knowledge bases and names what it cannot carry.

kiro-cli reads ``resources`` knowledge-base objects and ``toolAliases``; KAS takes
the first on its wire and has no slot for the second. A session gets the
knowledge bases it can, and a notice for the rest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_agents import spec_keys_without_carrier, to_client_custom_agent


def test_a_valid_knowledge_base_reaches_kas_with_only_schema_keys(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    spec = {
        "resources": [
            "file://README.md",
            {
                "type": "knowledgeBase",
                "source": f"file://{docs}",
                "name": "docs",
                "indexType": "fast",
                "autoUpdate": True,
                "extra": "dropped",
            },
            {"type": "knowledgeBase", "source": "file://./notes"},
        ]
    }

    entry = to_client_custom_agent("a", spec, "p")

    assert entry["resources"] == [
        "file://README.md",
        {
            "type": "knowledgeBase",
            "source": f"file://{docs.as_posix()}",
            "name": "docs",
            "indexType": "fast",
            "autoUpdate": True,
        },
        {"type": "knowledgeBase", "source": "file://notes"},
    ]
    assert spec_keys_without_carrier(spec) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "knowledgeBase", "source": "file://"},
        {"type": "knowledgeBase", "source": "https://example.com/docs"},
        {"type": "knowledgeBase", "source": "file://../../outside"},
        {"type": "knowledgeBase", "source": "file://~/.ssh"},
        {"type": "knowledgeBase", "source": "file://~nosuchuser_n9/docs"},
        {"type": "knowledgeBase", "source": "file://docs", "indexType": "slow"},
        {"type": "knowledgeBase", "source": "file://docs", "include": "*.md"},
        {"type": "other", "source": "file://docs"},
    ],
)
def test_a_knowledge_base_kas_would_refuse_is_dropped_and_named(bad):
    spec = {"resources": ["file://README.md", {**bad, "name": "kb"}]}

    entry = to_client_custom_agent("a", spec, "p")

    assert entry["resources"] == ["file://README.md"]
    assert spec_keys_without_carrier(spec) == ["resources[1] (kb)"]


def test_a_users_tool_aliases_are_named(monkeypatch):
    monkeypatch.setattr("kiro_crew.connections.alias_record.load_claimed", lambda _fp: frozenset())
    spec = {"toolAliases": {"@linear/list_issues": "linear_issues"}}

    assert spec_keys_without_carrier(spec) == ["toolAliases"]


def test_tool_aliases_crew_wrote_are_not_named(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.connections.alias_record.load_claimed",
        lambda _fp: frozenset({("linear", "search", "linear_search")}),
    )

    ours = {"@linear/search": "linear_search"}
    assert spec_keys_without_carrier({"toolAliases": ours}) == []
    mixed = {**ours, "@notion/search": "mine"}
    assert spec_keys_without_carrier({"toolAliases": mixed}) == ["toolAliases"]


@pytest.fixture
def layout(monkeypatch, tmp_path):
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    project = checkout / ".kiro" / "agents"
    project.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user, checkout, project


def _write(directory: Path, name: str, spec: dict) -> None:
    (directory / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


@pytest.mark.asyncio
async def test_a_user_agent_over_a_checkout_one_gets_a_notice(layout):
    user, checkout, project = layout
    _write(user, "shared", {"name": "shared", "prompt": "user"})
    _write(project, "shared", {"name": "shared", "prompt": "checkout"})

    extras = await KasHarness().session_extras("shared", work_dir=str(checkout))

    assert extras.custom_agents[0]["prompt"] == "user"
    assert "Your own one runs here" in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_a_checkout_agents_dropped_keys_are_named(layout):
    _user, checkout, project = layout
    _write(
        project,
        "proj",
        {"name": "proj", "systemPrompt": "p", "allowedTools": ["*"], "hooks": {}},
    )

    extras = await KasHarness().session_extras("proj", work_dir=str(checkout))

    notice = extras.withheld_credentials_notice
    assert "runs without allowedTools, hooks." in notice
    assert "systemPrompt" not in notice


@pytest.mark.asyncio
async def test_a_user_agent_alone_gets_no_checkout_notice(layout):
    user, checkout, _project = layout
    _write(user, "solo", {"name": "solo", "prompt": "user"})

    extras = await KasHarness().session_extras("solo", work_dir=str(checkout))

    assert extras.withheld_credentials_notice == ""
