# SPDX-License-Identifier: Apache-2.0
"""Built From for a crewmate that owns its file reads its WORKER, rows and counts alike.

The file holds only additions, so a skill the worker carries (a ``skill://``
resource from its kit) must be listed even when the file view lists none, and
the count is the length of that same list.
"""

from __future__ import annotations

from kiro_crew import mate_capabilities, mate_worker

WORKER = {
    "tools": ["fs_read", "@platform", "@notes"],
    "allowedTools": ["@notes/read"],
    "mcpServers": {
        "platform": {"command": "plat", "autoApprove": ["ping"]},
        "notes": {"command": "notes", "disabled": True},
    },
    "resources": [
        "file://.kiro/steering/**/*.md",
        "skill:///home/x/skills/release-notes/SKILL.md",
        "file:///home/x/catalog/triage/SKILL.md",
    ],
    "skills": ["catalog-skill"],
}
CATALOG = {"triage": "file:///home/x/catalog/triage/SKILL.md"}


def _ids(summary, section):
    return [r["id"] for r in summary["rows"] if r["section"] == section]


def test_the_rows_are_the_workers_effective_spec(monkeypatch):
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: WORKER)
    summary = mate_capabilities.worker_summary("ben", CATALOG)
    assert _ids(summary, "tools") == ["fs_read", "@platform", "@notes"]
    assert _ids(summary, "allowedTools") == ["@notes/read"]
    assert _ids(summary, "mcpServers") == ["platform", "notes"]
    assert _ids(summary, "skills") == [
        "skill:///home/x/skills/release-notes/SKILL.md",
        "triage",
        "catalog-skill",
    ]
    assert all(r["present"] for r in summary["rows"])


def test_a_skill_row_carries_no_name_of_its_own(monkeypatch):
    """The pane names every skill row; the server never derives a second name."""
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: WORKER)
    rows = mate_capabilities.worker_summary("ben", CATALOG)["rows"]
    assert {r["label"] for r in rows if r["section"] == "skills"} == {""}


def test_a_server_row_carries_its_switch_off_and_no_approvals(monkeypatch):
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: WORKER)
    rows = mate_capabilities.worker_summary("ben", CATALOG)["rows"]
    values = {r["id"]: r["value"] for r in rows if r["section"] == "mcpServers"}
    assert values == {
        "platform": {"command": "plat"},
        "notes": {"command": "notes", "disabled": True},
    }


def test_an_unreadable_worker_spec_gives_no_summary(monkeypatch):
    def boom(member):
        raise OSError("unreadable")

    monkeypatch.setattr(mate_worker, "member_worker_spec", boom)
    assert mate_capabilities.worker_summary("ben", {}) is None
