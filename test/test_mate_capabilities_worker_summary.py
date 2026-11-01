# SPDX-License-Identifier: Apache-2.0
"""Built From for a crewmate that owns its file reads its WORKER, rows and counts alike.

The file holds only additions, so a skill the worker carries (a ``skill://``
resource from its kit) must be listed even when the file view lists none, and
the count is the length of that same list.
"""

from __future__ import annotations

import pytest

from kiro_crew import mate_capabilities, mate_worker


def _patch_base(monkeypatch, base):
    """Make the worker SOURCE BASE (``MATE_WORKER_AGENT``) resolve to *base*.

    ``worker_summary`` loads the base via ``agent_specs.load_agent_spec(dir, id)``;
    patch that so the base-diff has a known baseline without touching disk."""
    from kiro_crew.agent_sdk import agent_specs

    def fake_load(_dir, agent_id, *a, **k):
        return dict(base)

    monkeypatch.setattr(agent_specs, "load_agent_spec", fake_load)


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


def test_only_rows_beyond_the_worker_base_read_local(monkeypatch):
    """A row the crewmate's file carries BEYOND the worker SOURCE BASE is ``local``
    (removable, "Added to mate"); a row the base already provides is ``inherited``
    (read-only, "Comes with"). A newly-created mate is a near-copy of the base, so
    its baseline reads "comes with" and only owner-added abilities read "added" —
    the file-membership rule marked the whole baseline removable, the reported bug."""
    base = {
        "name": "kirocrew-mate-worker",
        "mcpServers": {"platform": {"command": "plat"}},
        "tools": ["@platform", "fs_read"],
        "allowedTools": [],
        "skills": [],
        "resources": [],
    }
    # The file is the base PLUS ``notes`` (the owner's one addition).
    file_spec = {
        "name": "ben",
        "mcpServers": {"platform": {"command": "plat"}, "notes": {"command": "notes"}},
        "tools": ["@platform", "fs_read", "@notes"],
        "allowedTools": ["@notes/read"],
        "skills": ["catalog-skill"],
        "resources": [],
    }
    merged = {
        "mcpServers": {"platform": {"command": "plat"}, "notes": {"command": "notes"}},
        "tools": ["@platform", "fs_read", "@notes"],
        "allowedTools": ["@notes/read"],
        "skills": ["catalog-skill"],
        "resources": [],
    }
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, file_spec))
    _patch_base(monkeypatch, base)

    rows = mate_capabilities.worker_summary("ben", CATALOG)["rows"]
    state = {(r["section"], r["id"]): r["state"] for r in rows}
    # Beyond the base -> local (removable).
    assert state[("mcpServers", "notes")] == "local"
    assert state[("tools", "@notes")] == "local"
    assert state[("allowedTools", "@notes/read")] == "local"
    assert state[("skills", "catalog-skill")] == "local"
    # In the base -> inherited (read-only), even though the file also lists them.
    assert state[("mcpServers", "platform")] == "inherited"
    assert state[("tools", "@platform")] == "inherited"
    assert state[("tools", "fs_read")] == "inherited"


def test_a_new_mate_that_only_copies_the_base_has_an_empty_added_band(monkeypatch):
    """A freshly-created mate whose file equals the worker base adds nothing: every
    row reads ``inherited`` and the "Added to mate" band is empty. This is the live
    ``retwerf`` case — its baseline must NOT show as removable."""
    base = {
        "name": "kirocrew-mate-worker",
        "mcpServers": {"kirocrew-core": {"command": "c"}},
        "tools": ["fs_read", "@kirocrew-core"],
        "allowedTools": [],
        "skills": [],
        "resources": [],
    }
    file_spec = {**base, "name": "retwerf"}
    merged = {
        "mcpServers": {"kirocrew-core": {"command": "c"}},
        "tools": ["fs_read", "@kirocrew-core"],
    }
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, file_spec))
    _patch_base(monkeypatch, base)
    rows = mate_capabilities.worker_summary("retwerf", {})["rows"]
    assert {r["state"] for r in rows} == {"inherited"}


def test_an_unreadable_file_leaves_rows_inherited(monkeypatch):
    """If the crewmate file cannot be read, the summary degrades to all-inherited
    rather than failing — it is display only."""

    def boom(member):
        raise OSError("unreadable")

    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: WORKER)
    monkeypatch.setattr(mate_capabilities, "_load_spec", boom)
    rows = mate_capabilities.worker_summary("ben", CATALOG)["rows"]
    assert {r["state"] for r in rows} == {"inherited"}


def test_each_added_row_removes_the_stored_row_it_names(monkeypatch):
    """A Remove must drop a row the file really stores (``agent_capabilities._rows``).

    A stored server or tool removes itself; a catalog skill is stored under
    ``skills`` by its key; a skill URI off the catalog is stored under
    ``resources``; a ``@server`` selector derived from a stored ``*`` and an entry
    of the spec's own ``skills`` list name no stored row, so they offer no Remove
    (the live miss: the remove popped a key that did not exist and saved).
    """
    base = {"name": "kirocrew-mate-worker", "mcpServers": {}, "tools": [], "resources": []}
    file_spec = {
        "name": "ben",
        "mcpServers": {"notes": {"command": "notes"}, "jira": {"command": "jira"}},
        "tools": ["*", "@notes"],
        "allowedTools": [],
        "resources": [
            "file:///home/x/catalog/triage/SKILL.md",
            "skill:///home/x/skills/release-notes/SKILL.md",
        ],
        "skills": ["listed-skill"],
    }
    merged = {
        "mcpServers": {"notes": {"command": "notes"}, "jira": {"command": "jira"}},
        "tools": ["@notes", "@jira"],
        "resources": file_spec["resources"],
        "skills": ["listed-skill"],
    }
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, file_spec))
    _patch_base(monkeypatch, base)

    rows = mate_capabilities.worker_summary("ben", CATALOG)["rows"]
    remove = {(r["section"], r["id"]): r["remove"] for r in rows if r["state"] == "local"}
    assert remove[("mcpServers", "jira")] == {"section": "mcpServers", "id": "jira"}
    assert remove[("tools", "@notes")] is None
    assert remove[("skills", "triage")] == {"section": "skills", "id": "triage"}
    release = "skill:///home/x/skills/release-notes/SKILL.md"
    assert remove[("skills", release)] == {"section": "resources", "id": release}
    # Derived from the stored "*": nothing of its own to remove.
    assert remove[("tools", "@jira")] is None
    # The spec's own skills list has no editor row.
    assert remove[("skills", "listed-skill")] is None


def test_stored_wildcard_prevents_removing_any_tool_row(monkeypatch):
    base = {"mcpServers": {"platform": {"command": "plat"}}, "tools": ["@platform"]}
    file_spec = {
        "mcpServers": {"notes": {"command": "notes"}, "jira": {"command": "jira"}},
        "tools": ["*", "@notes"],
    }
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, file_spec))
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, file_spec))
    _patch_base(monkeypatch, base)

    rows = mate_capabilities.worker_summary("ben", {})["rows"]
    tools = {row["id"]: row for row in rows if row["section"] == "tools"}
    assert set(tools) == {"@platform", "@notes", "@jira"}
    assert tools["@notes"]["state"] == tools["@jira"]["state"] == "local"
    # The base's own tool stays in "Comes with" even though the stored ``*`` covers it.
    assert tools["@platform"]["state"] == "inherited"
    assert all(row["remove"] is None for row in tools.values())
    assert next(row for row in rows if row["section"] == "mcpServers" and row["id"] == "notes")[
        "remove"
    ] == {"section": "mcpServers", "id": "notes"}


def test_stored_whole_server_prevents_removing_its_single_tool_row(monkeypatch):
    base = {"mcpServers": {}, "tools": []}
    file_spec = {
        "mcpServers": {"notes": {"command": "notes"}},
        "tools": ["@notes", "@notes/read"],
    }
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, file_spec))
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, file_spec))
    _patch_base(monkeypatch, base)

    rows = mate_capabilities.worker_summary("ben", {})["rows"]
    tools = {row["id"]: row for row in rows if row["section"] == "tools"}
    assert tools["@notes/read"]["state"] == "local"
    assert tools["@notes/read"]["remove"] is None
    assert tools["@notes"]["remove"] == {"section": "tools", "id": "@notes"}


def test_skill_resource_glob_keeps_an_explicit_skill_available(monkeypatch):
    glob = "skill://.kiro/skills/*/SKILL.md"
    release = "skill://.kiro/skills/release/SKILL.md"
    base = {"mcpServers": {}, "tools": [], "resources": []}
    file_spec = {"resources": [glob, release]}
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, file_spec))
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, file_spec))
    _patch_base(monkeypatch, base)

    for catalog, key in (({}, release), ({"release": release}, "release")):
        rows = mate_capabilities.worker_summary("ben", catalog)["rows"]
        skills = {row["id"]: row for row in rows if row["section"] == "skills"}
        assert skills[key]["state"] == "local"
        assert skills[key]["remove"] is None
        assert skills[glob]["remove"] == {"section": "resources", "id": glob}
        assert file_spec == {"resources": [glob, release]}


def test_a_skill_a_base_glob_grants_comes_with_and_has_no_remove(monkeypatch):
    glob = "skill://.kiro/skills/*/SKILL.md"
    release = "skill://.kiro/skills/release/SKILL.md"
    base = {"mcpServers": {}, "tools": [], "resources": [glob]}
    file_spec = {"resources": [release]}
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, file_spec))
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, file_spec))
    _patch_base(monkeypatch, base)

    for catalog, key in (({}, release), ({"release": release}, "release")):
        rows = mate_capabilities.worker_summary("ben", catalog)["rows"]
        skills = {row["id"]: row for row in rows if row["section"] == "skills"}
        assert skills[key]["state"] == "inherited"
        assert skills[key]["remove"] is None


def test_behaviour_directions_show_as_one_removable_added_row(monkeypatch):
    """The file's prompt is the one behaviour-directions slot: one Added row
    carrying its text, whose Remove clears the slot; an empty prompt shows none."""
    base = {"name": "kirocrew-mate-worker", "prompt": "Worker.", "mcpServers": {}, "tools": []}
    file_spec = {"name": "ben", "prompt": "  Release SOP: tag, build.  ", "mcpServers": {}}
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: {"tools": []})
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, file_spec))
    _patch_base(monkeypatch, base)

    rows = [
        r
        for r in mate_capabilities.worker_summary("ben", CATALOG)["rows"]
        if r["section"] == "prompt"
    ]
    assert [(r["state"], r["value"], r["remove"]) for r in rows] == [
        ("local", "Release SOP: tag, build.", {"section": "prompt", "id": "prompt"})
    ]

    file_spec["prompt"] = ""
    rows = mate_capabilities.worker_summary("ben", CATALOG)["rows"]
    assert not [r for r in rows if r["section"] == "prompt"]


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Worker.\n\nPrinciples.", []),
        ("Worker.", []),
        ("Worker.\n\nPrinciples.\n\nRelease SOP.", ["Release SOP."]),
    ],
)
def test_the_directions_row_shows_only_text_beyond_the_base_prompt(monkeypatch, prompt, expected):
    """A file holding the template's whole prompt (which the worker base opens
    with) or the base's own text adds nothing, so it shows no directions row; a
    prompt that opens with the base's text shows only the rest."""
    base = {
        "name": "kirocrew-mate-worker",
        "prompt": "Worker.\n\nPrinciples.",
        "mcpServers": {},
        "tools": [],
    }
    file_spec = {"name": "ben", "prompt": prompt, "mcpServers": {}}
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: {"tools": []})
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, file_spec))
    _patch_base(monkeypatch, base)

    rows = mate_capabilities.worker_summary("ben", CATALOG)["rows"]
    assert [r["value"] for r in rows if r["section"] == "prompt"] == expected


def test_removing_the_directions_row_clears_the_slot():
    """The Remove the row carries is an ordinary capabilities ``remove`` on ``prompt``."""
    from kiro_crew.agent_capabilities import _materialize, _rows
    from kiro_crew.mate_capabilities import _apply_operations

    spec = {"name": "ben", "prompt": "Release SOP.", "mcpServers": {}, "tools": []}
    rows = _rows(spec, {})
    _apply_operations(rows, [{"section": "prompt", "id": "prompt", "action": "remove"}], {})
    assert "prompt" not in _materialize(spec, rows, {})


@pytest.mark.parametrize("section", ["tools", "allowedTools"])
@pytest.mark.parametrize("glob", ["@notes/write_*", "@notes/write_not?"])
def test_a_glob_entry_withholds_remove_on_a_tool_it_still_grants(monkeypatch, section, glob):
    base = {"mcpServers": {}, "tools": []}
    file_spec = {
        "mcpServers": {"notes": {"command": "notes"}},
        "tools": ["@notes/write_note"],
        section: [glob, "@notes/write_note"],
    }
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, file_spec))
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, file_spec))
    _patch_base(monkeypatch, base)

    rows = mate_capabilities.worker_summary("ben", {})["rows"]
    tools = {row["id"]: row for row in rows if row["section"] == section}
    assert tools["@notes/write_note"]["state"] == "local"
    assert tools["@notes/write_note"]["remove"] is None
    assert tools[glob]["remove"] == {"section": section, "id": glob}
    assert file_spec[section] == [glob, "@notes/write_note"]


def test_a_flat_listed_app_skill_the_base_app_wildcard_grants_comes_with(monkeypatch, tmp_path):
    """An app skill listed under its flat link is granted by ``<app>/*/SKILL.md``."""
    from kiro_crew.skills import skills_dir
    from kiro_crew.testing.links import make_dir_link

    root = skills_dir()
    target = tmp_path / "bundle" / "a-first"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("x", encoding="utf-8")
    (root / "app").mkdir(parents=True)
    make_dir_link(root / "a-first", target)
    make_dir_link(root / "app" / "a-first", target)
    wildcard = f"skill://{root.as_posix()}/app/*/SKILL.md"
    flat = f"skill://{root.as_posix()}/a-first/SKILL.md"
    base = {"mcpServers": {}, "tools": [], "resources": [wildcard]}
    file_spec = {"resources": [flat]}
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, file_spec))
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: merged)
    monkeypatch.setattr(mate_capabilities, "_load_spec", lambda member: (None, file_spec))
    _patch_base(monkeypatch, base)

    rows = mate_capabilities.worker_summary("ben", {"a-first": flat})["rows"]
    skills = {row["id"]: row for row in rows if row["section"] == "skills"}
    assert skills["a-first"]["state"] == "inherited"
    assert skills["a-first"]["remove"] is None
