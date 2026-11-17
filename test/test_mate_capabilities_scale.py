# SPDX-License-Identifier: Apache-2.0
"""A Bot with about a thousand tool approvals still opens, previews and saves.

The Abilities and Approvals panes read ``GET /api/agents/<bot>/capabilities``,
which gives up after 20 s. Each added row's Remove check re-derives what the
worker would carry without that row, so matching every approval against every
other approval there made the read cubic: four servers of a hundred approvals
took over 30 s, ten servers took minutes.
"""

from __future__ import annotations

import json

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_capabilities, mate_worker
from kiro_crew.agent_capabilities import CapabilityService
from kiro_crew.agent_sdk import agent_specs, mcp_refs

SERVERS, TOOLS = 10, 100
BASE = {
    "mcpServers": {"platform": {"command": "plat"}},
    "tools": ["@platform", "fs_read"],
    "allowedTools": ["fs_read"],
    "resources": [],
    "skills": [],
}
NAMES = [f"srv{i}" for i in range(SERVERS)]
SPEC = {
    "name": "big",
    "prompt": "",
    "mcpServers": {"platform": {"command": "plat"}, **{n: {"command": n} for n in NAMES}},
    "tools": ["@platform", "fs_read", *(f"@{n}" for n in NAMES)],
    "allowedTools": ["fs_read", *(f"@{n}/tool_{j}" for n in NAMES for j in range(TOOLS))],
    "resources": [],
    "skills": [],
}
#: ``entry_reaches`` calls allowed per read; all-pairs matching made ~10^9 here.
MAX_MATCHES = 20 * SERVERS * TOOLS
#: Whole-file rebuilds allowed per read: one per approval made it quadratic.
MAX_REBUILDS = SERVERS * TOOLS // 10


@pytest.fixture
def bot(monkeypatch, tmp_path):
    kiro = tmp_path / "home"
    (kiro / "agents").mkdir(parents=True)
    mates = kiro / "mates" / "agents"
    mates.mkdir(parents=True)
    path = mates / "big.json"
    path.write_text(json.dumps(SPEC), encoding="utf-8")
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: kiro / "agents")
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(mate_capabilities, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "binding_is_mate", lambda member: True)
    monkeypatch.setattr(mate_capabilities, "skill_catalog", lambda member, catalog: {})
    monkeypatch.setattr(
        agent_specs, "load_agent_spec", lambda *a, **k: json.loads(json.dumps(BASE))
    )
    monkeypatch.setattr(
        mate_worker, "member_worker_spec", lambda member: json.loads(path.read_text())
    )
    calls = {"n": 0}
    real = mcp_refs.entry_reaches

    def counted(entry, ref):
        calls["n"] += 1
        return real(entry, ref)

    monkeypatch.setattr(mcp_refs, "entry_reaches", counted)
    real_additions = mate_worker.worker_additions

    def rebuilt(base, spec):
        calls["rebuilds"] += 1
        return real_additions(base, spec)

    calls["rebuilds"] = 0
    monkeypatch.setattr(mate_worker, "worker_additions", rebuilt)
    return CapabilityService(), path, calls


def test_get_preview_and_put_do_not_match_every_approval_against_every_other(bot):
    service, path, calls = bot
    approvals = SERVERS * TOOLS

    view = service.get("big")
    rows = [r for r in view["worker"]["rows"] if r["section"] == "allowedTools"]
    added = [r for r in rows if r["state"] == "local"]
    assert len(added) == approvals
    assert all(r["remove"] == {"section": "allowedTools", "id": r["id"]} for r in added)
    assert calls["n"] < MAX_MATCHES
    assert calls["rebuilds"] < MAX_REBUILDS

    target = "@srv3/tool_7"
    body = {
        "revision": view["revision"],
        "operations": [{"section": "allowedTools", "id": target, "action": "remove"}],
        "preview_token": view["revision"],
        "accept_parent": [],
        "accept_members": [],
    }
    calls["n"] = 0
    preview = service.preview("big", body)
    assert calls["n"] < MAX_MATCHES
    assert any(i["id"] == target and i["change"] == "removed" for i in preview["impact"])

    calls["n"] = 0
    saved = service.put("big", body)
    assert calls["n"] < MAX_MATCHES
    assert target not in json.loads(path.read_text())["allowedTools"]
    ids = {r["id"] for r in saved["worker"]["rows"] if r["section"] == "allowedTools"}
    assert target not in ids and "@srv3/tool_8" in ids
