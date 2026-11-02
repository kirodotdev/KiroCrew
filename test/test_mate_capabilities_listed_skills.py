# SPDX-License-Identifier: Apache-2.0
"""A crewmate's own ``skills`` list entries can be removed, one entry at a time.

An add copies an agent's ``skills`` list into the crewmate's file as written.
When that agent later names the same skill differently (a bare name, then its
registered ``<app>/<skill>`` key), the old entry stays in the file with no agent
behind it. The editor must offer a Remove for it, a capabilities remove must
take it out, and neither may touch the other skills the file or the worker
base still carries.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import change_card_catalog as catalog_mod
from kiro_crew import mate_capabilities, mate_worker
from kiro_crew.config import paths

URI = "skill:///home/x/skills/my-app/learning-aid/SKILL.md"
CATALOG = {"my-app/learning-aid": URI}
FILE = {
    "name": "pixel",
    "tools": [],
    "mcpServers": {},
    "resources": [URI],
    # A skill the owner put there, the bare name an older agent version listed,
    # the same skill under its current key, and one the worker base also lists.
    "skills": ["hand-skill", "learning-aid", "my-app/learning-aid", "shared-skill"],
}


@pytest.fixture
def mate(tmp_path, monkeypatch):
    mates = tmp_path / "mates" / "agents"
    mates.mkdir(parents=True)
    (mates / "pixel.json").write_text(json.dumps(FILE), encoding="utf-8")
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "worker_summary", lambda member, catalog: None)
    return mates / "pixel.json"


def _read(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _remove(key: str) -> dict:
    return {
        "revision": mate_capabilities._revision(FILE),
        "operations": [{"section": "skills", "id": key, "action": "remove"}],
        "accept_parent": [],
        "accept_members": [],
    }


def _save(key: str) -> dict:
    body = _remove(key)
    preview = mate_capabilities.preview("pixel", body, CATALOG)
    mate_capabilities.put("pixel", {**body, "preview_token": preview["preview_token"]}, CATALOG)
    return preview


def test_removing_an_orphan_listed_skill_clears_only_that_entry(mate):
    preview = _save("learning-aid")

    assert {(r["section"], r["id"], r["change"]) for r in preview["impact"]} == {
        ("skills", "learning-aid", "removed")
    }
    spec = _read(mate)
    assert spec["skills"] == ["hand-skill", "my-app/learning-aid", "shared-skill"]
    assert spec["resources"] == [URI]


def test_removing_a_catalog_skill_also_clears_its_listed_name(mate):
    _save("my-app/learning-aid")

    spec = _read(mate)
    assert spec["skills"] == ["hand-skill", "learning-aid", "shared-skill"]
    assert spec.get("resources", []) == []


def test_the_view_lists_each_listed_skill_once(mate):
    rows = [r for r in mate_capabilities.get("pixel", CATALOG)["rows"] if r["section"] == "skills"]

    assert [(r["id"], r.get("listed", False)) for r in rows] == [
        ("my-app/learning-aid", True),
        ("hand-skill", True),
        ("learning-aid", True),
        ("shared-skill", True),
    ]
    assert all(r["present"] for r in rows)


@pytest.mark.parametrize("key", ["learning-aid", "my-app/learning-aid"])
def test_a_listed_skill_removal_says_why_it_has_no_undo(mate, monkeypatch, key):
    """The card's own snapshot keeps the flag, for a listed-only name and for a
    catalog skill the list also names: a ``set`` would put back neither entry."""
    from types import SimpleNamespace

    from kiro_crew.dashboard import change_cards_readers as readers

    service = SimpleNamespace(get=lambda member: mate_capabilities.get(member, CATALOG))
    monkeypatch.setattr(readers, "_capability_service", lambda app: service)
    p = {"member": "pixel", "draft": {"operations": _remove(key)["operations"]}}
    before = readers._capability_state(None, p)

    undo, reason = catalog_mod.build_undo(
        catalog_mod.KIND_CREWMATE_CAPABILITIES, p, before, [{}, {"revision": "r1"}], 1
    )

    assert undo is None
    assert reason == "listed_skill_entry"


def _patch_base(monkeypatch, base):
    from kiro_crew.agent_sdk import agent_specs

    monkeypatch.setattr(agent_specs, "load_agent_spec", lambda *a, **k: dict(base))


def test_the_summary_offers_remove_for_each_added_listed_skill_only(monkeypatch):
    base = {
        "name": "kirocrew-mate-worker",
        "mcpServers": {},
        "tools": [],
        "skills": ["shared-skill"],
    }
    merged = {**FILE, "skills": list(FILE["skills"])}
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, dict(FILE)))
    _patch_base(monkeypatch, base)

    rows = mate_capabilities.worker_summary("pixel", CATALOG)["rows"]
    skills = {r["id"]: r for r in rows if r["section"] == "skills"}

    assert skills["learning-aid"]["remove"] == {"section": "skills", "id": "learning-aid"}
    assert skills["hand-skill"]["remove"] == {"section": "skills", "id": "hand-skill"}
    assert skills["my-app/learning-aid"]["remove"] == {
        "section": "skills",
        "id": "my-app/learning-aid",
    }
    # The worker base lists it too: it comes with the worker and stays.
    assert skills["shared-skill"]["state"] == "inherited"
    assert skills["shared-skill"]["remove"] is None
