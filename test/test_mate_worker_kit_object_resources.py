# SPDX-License-Identifier: Apache-2.0
"""A knowledge base an agent lists in ``resources`` reaches a Bot that adds that agent.

A knowledge base is an object entry, not a URI string (``agent-spec-fields.md``,
Resources). Add abilities copies it into the Bot's file as written, and the
worker merge carries it onto the worker spec, which KAS projects
(``acp.kas_spec_carry.projected_resources``) as it would for any session of that
agent. A relative ``file://`` entry is copied as written too: KAS anchors it at
the session's folder, not at the folder the spec lives in.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import mate_capabilities, mate_worker, mate_worker_kit
from kiro_crew.acp.kas_spec_carry import projected_resources
from kiro_crew.config import paths

KB = {
    "type": "knowledgeBase",
    "name": "Handbook",
    "source": "file://./knowledge",
    "indexType": "fast",
    "autoUpdate": True,
}
STEERING = "file://./docs/*.md"
TEMPLATE = {
    "name": "kb-agent",
    "prompt": "Answer from the handbook.",
    "tools": ["*"],
    "resources": [STEERING, KB, "skill://kb/SKILL.md"],
    "mcpServers": {},
}
BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "w",
    "tools": ["*"],
    "mcpServers": {},
    "resources": ["file://~/.kiro/steering/**/*.md"],
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    user, mates = tmp_path / "agents", tmp_path / "mates" / "agents"
    user.mkdir()
    mates.mkdir(parents=True)
    (user / "kb-agent.json").write_text(json.dumps(TEMPLATE), encoding="utf-8")
    (mates / "pixel.json").write_text(
        json.dumps({"name": "pixel", "prompt": "", "tools": [], "mcpServers": {}}),
        encoding="utf-8",
    )
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(BASE), encoding="utf-8")
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "binding_is_mate", lambda member: member == "pixel")
    return user, mates


def _add() -> dict:
    preview = mate_worker_kit.preview("pixel", {"template": "kb-agent"})
    mate_worker_kit.apply("pixel", {"template": "kb-agent", "revision": preview["revision"]})
    return preview


def _file(mates) -> dict:
    return json.loads((mates / "pixel.json").read_text(encoding="utf-8"))


def test_add_abilities_copies_a_knowledge_base_into_the_file_as_written(home):
    _, mates = home
    preview = _add()
    assert _file(mates)["resources"] == [STEERING, KB, "skill://kb/SKILL.md"]
    added = {(r["section"], r["id"]) for r in preview["changes"] if r["change"] == "added"}
    assert ("resources", "knowledgeBase:Handbook") in added


def test_adding_the_same_agent_twice_keeps_one_knowledge_base(home):
    _, mates = home
    _add()
    preview = _add()
    assert _file(mates)["resources"] == [STEERING, KB, "skill://kb/SKILL.md"]
    skipped = {(r["section"], r["id"]) for r in preview["changes"] if r["change"] == "skipped"}
    assert ("resources", "knowledgeBase:Handbook") in skipped


def test_the_worker_spec_carries_the_knowledge_base_and_kas_projects_it(home):
    _, mates = home
    _add()
    spec = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, _file(mates)))
    assert spec["resources"] == [BASE["resources"][0], STEERING, KB, "skill://kb/SKILL.md"]
    # KAS is sent what a session of the agent itself is sent.
    kept, dropped = projected_resources(spec["resources"])
    assert projected_resources(TEMPLATE["resources"])[0][1] in kept
    assert dropped == []


def test_a_knowledge_base_the_base_already_lists_is_not_added_twice(home):
    base = {**BASE, "resources": [*BASE["resources"], dict(KB)]}
    additions = mate_worker.worker_additions(base, TEMPLATE)
    assert additions["resources"] == [STEERING, "skill://kb/SKILL.md"]
    spec = mate_worker.merge_worker_spec(base, additions)
    assert spec["resources"].count(KB) == 1


def test_an_add_keeps_a_knowledge_base_the_file_already_holds(home):
    _, mates = home
    own = {"type": "knowledgeBase", "name": "Notes", "source": "file:///srv/notes"}
    spec = _file(mates)
    spec["resources"] = [own]
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    _add()
    assert _file(mates)["resources"] == [own, STEERING, KB, "skill://kb/SKILL.md"]


def test_the_abilities_view_lists_an_added_knowledge_base_with_remove(home, monkeypatch):
    from kiro_crew.agent_sdk import agent_specs

    _, mates = home
    _add()
    file_spec = _file(mates)
    merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, file_spec))
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, dict(file_spec)))
    monkeypatch.setattr(agent_specs, "load_agent_spec", lambda *a, **k: dict(BASE))

    rows = mate_capabilities.worker_summary("pixel", {})["rows"]
    resources = {r["id"]: r for r in rows if r["section"] == "resources"}

    kb = resources["knowledgeBase:Handbook"]
    assert kb["value"] == KB
    assert kb["state"] == "local"
    assert kb["remove"] == {"section": "resources", "id": "knowledgeBase:Handbook"}
    assert resources[BASE["resources"][0]]["state"] == "inherited"
