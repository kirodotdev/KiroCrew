# SPDX-License-Identifier: Apache-2.0
"""Built From's counts for a crewmate that owns its file read its WORKER.

The file holds only additions, so a skill the worker carries (a ``skill://``
resource from its kit) must count even when the file view lists none.
"""

from __future__ import annotations

from kiro_crew import mate_capabilities, mate_capability_catalog

WORKER = {
    "tools": ["fs_read", "@platform", "@notes"],
    "mcpServers": {"platform": {}, "notes": {}},
    "resources": ["file://.kiro/steering/**/*.md", "skill:///home/x/skills/release-notes/SKILL.md"],
    "skills": ["catalog-skill"],
}


def test_the_summary_counts_the_workers_effective_spec(monkeypatch):
    monkeypatch.setattr(mate_capability_catalog, "worker_spec", lambda member: WORKER)
    assert mate_capabilities.worker_summary("ben", {}) == {"tools": 3, "mcpServers": 2, "skills": 2}


def test_an_unreadable_worker_spec_gives_no_summary(monkeypatch):
    def boom(member):
        raise OSError("unreadable")

    monkeypatch.setattr(mate_capability_catalog, "worker_spec", boom)
    assert mate_capabilities.worker_summary("ben", {}) is None
