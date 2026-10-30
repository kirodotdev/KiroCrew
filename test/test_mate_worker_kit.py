# SPDX-License-Identifier: Apache-2.0
"""A crewmate's worker kit: abilities added to its file from another agent.

``rfc-mate-owned-agent-config`` §5.1: adding abilities from another agent copies
that agent's servers (with their auto-approvals), skills, resources and approvals
into the crewmate's own file, purely additively. Anything the crewmate already
has — in its file or the worker base — is left alone and not copied again; the
agent's behaviour directions fill the file's one ``prompt`` slot. An add is
previewed, then written on an apply that carries the preview's revision.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import mate_approval_revocations as revocations
from kiro_crew import mate_capabilities, mate_worker, mate_worker_kit
from kiro_crew.config import paths
from kiro_crew.mate_worker_kit import WorkerKitError
from kiro_crew.platform import governance

TEMPLATE = {
    "name": "release-bot",
    "prompt": "Release SOP: tag, build, announce.",
    "tools": ["*"],
    "allowedTools": ["@jira/search"],
    "resources": ["skill://release/SKILL.md"],
    "mcpServers": {"jira": {"command": "jira-mcp", "autoApprove": ["search"]}},
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    user, mates = tmp_path / "agents", tmp_path / "mates" / "agents"
    user.mkdir()
    mates.mkdir(parents=True)
    (user / "release-bot.json").write_text(json.dumps(TEMPLATE), encoding="utf-8")
    (mates / "pixel.json").write_text(
        json.dumps({"name": "pixel", "prompt": "", "tools": [], "mcpServers": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "binding_is_mate", lambda member: member == "pixel")
    return user, mates


def _file(mates) -> dict:
    return json.loads((mates / "pixel.json").read_text(encoding="utf-8"))


def _add(template="release-bot"):
    preview = mate_worker_kit.preview("pixel", {"template": template})
    mate_worker_kit.apply("pixel", {"template": template, "revision": preview["revision"]})
    return preview


def test_a_binding_without_its_own_file_has_no_worker_kit(home):
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.view("peer")
    assert caught.value.code == "not_a_mate_file"


def test_the_view_reports_only_the_member_and_revision(home):
    """There is no recorded template and no 'additions since' — the file is the sum."""
    view = mate_worker_kit.view("pixel")
    assert set(view) == {"member", "revision"}
    assert view["member"] == "pixel"


def test_the_preview_names_every_added_item_and_writes_nothing(home):
    _, mates = home
    before = (mates / "pixel.json").read_bytes()
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    changed = {(row["section"], row["id"], row["change"]) for row in preview["changes"]}
    # The agent's directions fill the empty behaviour-directions slot.
    assert ("prompt", "prompt", "added") in changed
    assert preview["directions"] == TEMPLATE["prompt"]
    assert preview["current_directions"] == ""
    assert ("mcpServers", "jira", "added") in changed
    assert ("resources", "skill://release/SKILL.md", "added") in changed
    assert (mates / "pixel.json").read_bytes() == before


def test_the_preview_lists_every_auto_approval_the_add_writes(home):
    """A grant the add saves lets the worker run that tool unasked, so the preview
    must show it: an ``allowedTools`` entry and a server's own ``autoApprove`` alike.
    The preview's grants equal the grants the applied file holds."""
    _, mates = home
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    granted = {r["id"] for r in preview["changes"] if r["section"] == "allowedTools"}
    assert granted == {"@jira/search"}
    mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    spec = _file(mates)
    held = set(spec.get("allowedTools") or []) | {
        f"@{name}/{tool}"
        for name, entry in spec["mcpServers"].items()
        for tool in entry.get("autoApprove") or []
    }
    assert held == granted


def test_adding_copies_the_servers_skills_resources_approvals_and_directions(home):
    _, mates = home
    _add()
    spec = _file(mates)
    # The agent's prompt fills the one behaviour-directions slot.
    assert spec["prompt"] == TEMPLATE["prompt"]
    assert spec["mcpServers"]["jira"]["autoApprove"] == ["search"]
    assert spec["resources"] == TEMPLATE["resources"]
    # Approvals ride along with the server they are for.
    assert spec["allowedTools"] == ["@jira/search"]
    assert "@jira" in spec["tools"]
    assert spec["name"] == "pixel"


def test_the_added_kit_reaches_the_worker(home):
    _, mates = home
    _add()
    base = {"name": "kirocrew-mate-worker", "prompt": "Worker.", "mcpServers": {}, "tools": []}
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, _file(mates)))
    # The directions follow the worker's own prompt, never replace it.
    assert merged["prompt"] == f"Worker.\n\n{TEMPLATE['prompt']}"
    assert "jira" in merged["mcpServers"]


def test_adding_preserves_existing_wildcard_server_selection(home, monkeypatch):
    _, mates = home
    base = {"mcpServers": {"platform": {"command": "plat"}}, "tools": ["@platform"]}
    monkeypatch.setattr(mate_worker_kit, "_worker_base_or_none", lambda: base)
    spec = _file(mates)
    spec["mcpServers"] = {"notes": {"command": "notes-mcp"}}
    spec["tools"] = ["*"]
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    before = mate_worker.worker_additions(base, spec)
    assert before["tools"] == ["@notes"]

    _add()

    written = _file(mates)
    after = mate_worker.worker_additions(base, written)
    assert "*" not in written["tools"]
    assert mate_worker.server_selectors(after, before["mcpServers"]) == before["tools"]
    assert "@jira" in after["tools"]


@pytest.mark.parametrize("approvals", [[], ["@notes/read"]])
def test_adding_a_server_discards_its_orphan_selectors(home, monkeypatch, approvals):
    user, mates = home
    monkeypatch.setattr(mate_worker_kit, "_worker_base_or_none", lambda: {})
    spec = _file(mates)
    spec.update(
        tools=["fs_read", "@notes", "@notes/x", "@notes-other"],
        allowedTools=["@notes", "@notes/x", "@notes-other/read"],
        excludedTools=["@notes/write"],
        mcpServers={"notes-other": {"command": "other-mcp"}},
    )
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    template = {
        "name": "notes-reader",
        "tools": ["@notes/read"],
        "allowedTools": approvals,
        "mcpServers": {"notes": {"command": "notes-mcp"}},
    }
    (user / "notes-reader.json").write_text(json.dumps(template), encoding="utf-8")

    _add("notes-reader")

    written = _file(mates)
    assert written["tools"] == ["fs_read", "@notes-other", "@notes/read"]
    assert written["allowedTools"] == ["@notes-other/read", *approvals]
    assert written["excludedTools"] == ["@notes/write"]
    assert written["mcpServers"]["notes"] == template["mcpServers"]["notes"]


def test_re_adding_restores_a_removed_tool_on_a_retained_server(home, monkeypatch):
    user, mates = home
    monkeypatch.setattr(mate_worker_kit, "_worker_base_or_none", lambda: {})
    spec = _file(mates)
    spec.update(
        tools=["@notes/read"],
        allowedTools=["@notes/read"],
        mcpServers={"notes": {"command": "my-notes", "args": ["--local"], "autoApprove": ["read"]}},
    )
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    transport = json.dumps(spec["mcpServers"]["notes"]).encode()
    template = {
        "name": "notes-agent",
        "tools": ["@notes/read", "@notes/write"],
        "allowedTools": ["@notes/read", "@notes/write"],
        "mcpServers": {"notes": {"command": "template-notes", "autoApprove": ["write"]}},
    }
    (user / "notes-agent.json").write_text(json.dumps(template), encoding="utf-8")

    preview = _add("notes-agent")

    written = _file(mates)
    assert written["tools"] == ["@notes/read", "@notes/write"]
    assert json.dumps(written["mcpServers"]["notes"]).encode() == transport
    assert written["allowedTools"] == ["@notes/read"]
    assert [
        r for r in preview["changes"] if r["change"] == "added" and r["section"] != "prompt"
    ] == [{"section": "tools", "id": "@notes/write", "change": "added"}]
    assert not [
        row
        for row in preview["changes"]
        if row["id"] == "@notes/write" and row["change"] == "skipped"
    ]
    assert not [
        r
        for r in _add("notes-agent")["changes"]
        if r["change"] == "added" and r["section"] != "prompt"
    ]


def test_re_adding_adds_no_tool_to_a_server_already_selected_whole(home, monkeypatch):
    user, mates = home
    monkeypatch.setattr(mate_worker_kit, "_worker_base_or_none", lambda: {})
    spec = _file(mates)
    spec.update(tools=["@notes"], mcpServers={"notes": {"command": "my-notes"}})
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    template = {
        "name": "notes-agent",
        "tools": ["@notes/read", "@notes/write"],
        "mcpServers": {"notes": {"command": "template-notes"}},
    }
    (user / "notes-agent.json").write_text(json.dumps(template), encoding="utf-8")

    preview = _add("notes-agent")

    assert _file(mates)["tools"] == ["@notes"]
    assert not [
        r for r in preview["changes"] if r["change"] == "added" and r["section"] != "prompt"
    ]


@pytest.mark.parametrize(
    "stored_tools, added_tools",
    [
        (["@notes/read"], [{"section": "tools", "id": "@notes", "change": "added"}]),
        (["@notes", "@notes/read"], []),
        (["*", "@notes/read"], []),
    ],
)
def test_adding_whole_server_collapses_single_tool_selectors(
    home, monkeypatch, stored_tools, added_tools
):
    user, mates = home
    monkeypatch.setattr(mate_worker_kit, "_worker_base_or_none", lambda: {})
    spec = _file(mates)
    spec.update(tools=stored_tools, mcpServers={"notes": {"command": "my-notes"}})
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    template = {
        "name": "notes-agent",
        "tools": ["@notes", "@notes/read"],
        "mcpServers": {"notes": {"command": "template-notes"}},
    }
    (user / "notes-agent.json").write_text(json.dumps(template), encoding="utf-8")

    preview = _add("notes-agent")

    assert _file(mates)["tools"] == ["@notes"]
    assert [row for row in preview["changes"] if row["section"] == "tools"] == added_tools
    assert not [
        r
        for r in _add("notes-agent")["changes"]
        if r["change"] == "added" and r["section"] != "prompt"
    ]


def test_adding_an_unselected_server_clears_an_empty_file_wildcard(home, monkeypatch):
    user, mates = home
    base = {"mcpServers": {}, "tools": []}
    monkeypatch.setattr(mate_worker_kit, "_worker_base_or_none", lambda: base)
    spec = _file(mates)
    spec["tools"] = ["*"]
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    template = {
        "name": "notes-agent",
        "tools": [],
        "mcpServers": {"notes": {"command": "notes-mcp"}},
    }
    (user / "notes-agent.json").write_text(json.dumps(template), encoding="utf-8")

    _add("notes-agent")

    written = _file(mates)
    assert written["tools"] == []
    assert "notes" in written["mcpServers"]
    assert mate_worker.worker_additions(base, written)["tools"] == []


def test_adding_does_not_copy_selectors_for_a_base_only_server(home, monkeypatch):
    user, mates = home
    base = {"mcpServers": {"notes": {"command": "base-notes"}}, "tools": ["@notes/read"]}
    monkeypatch.setattr(mate_worker_kit, "_worker_base_or_none", lambda: base)
    template = {
        "name": "notes-agent",
        "tools": ["@notes/read", "@notes/write"],
        "allowedTools": ["@notes/write"],
        "mcpServers": {"notes": {"command": "template-notes"}},
    }
    (user / "notes-agent.json").write_text(json.dumps(template), encoding="utf-8")

    preview = _add("notes-agent")

    written = _file(mates)
    assert written["tools"] == []
    assert written["mcpServers"] == {}
    assert not written.get("allowedTools")
    assert not [
        r for r in preview["changes"] if r["change"] == "added" and r["section"] != "prompt"
    ]


def test_a_server_already_present_is_left_alone_and_skipped(home):
    """Adding a template whose server the crewmate already has writes nothing for it."""
    _, mates = home
    spec = _file(mates)
    spec["mcpServers"] = {"jira": {"command": "my-own-jira"}}
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert {"section": "mcpServers", "id": "jira", "change": "skipped"} in preview["changes"]
    assert not [
        row
        for row in preview["changes"]
        if row["section"] == "mcpServers" and row["change"] == "added"
    ]
    _add()
    # The existing entry is untouched: the template's transport did not overwrite it.
    assert _file(mates)["mcpServers"]["jira"]["command"] == "my-own-jira"


def test_re_adding_the_same_template_changes_nothing(home):
    """Everything is already present, so a second add only skips."""
    _, mates = home
    _add()
    after_first = (mates / "pixel.json").read_bytes()
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert not [
        r for r in preview["changes"] if r["change"] == "added" and r["section"] != "prompt"
    ]
    assert {"section": "mcpServers", "id": "jira", "change": "skipped"} in preview["changes"]
    _add()
    assert (mates / "pixel.json").read_bytes() == after_first


def test_adding_a_second_template_unions_its_net_new_abilities(home):
    user, mates = home
    _add("release-bot")
    other = {
        "name": "triage-bot",
        "prompt": "Triage SOP.",
        "tools": ["@notes/read"],
        "allowedTools": [],
        "resources": ["skill://triage/SKILL.md"],
        "mcpServers": {"notes": {"command": "notes-mcp"}},
    }
    (user / "triage-bot.json").write_text(json.dumps(other), encoding="utf-8")
    preview = _add("triage-bot")
    changed = {(row["section"], row["id"], row["change"]) for row in preview["changes"]}
    assert ("mcpServers", "notes", "added") in changed
    assert ("resources", "skill://triage/SKILL.md", "added") in changed
    spec = _file(mates)
    # Both templates' servers now live in the file.
    assert set(spec["mcpServers"]) == {"jira", "notes"}
    assert "@notes/read" in spec["tools"]


def test_a_later_add_replaces_the_directions_and_one_without_keeps_them(home):
    """One behaviour-directions slot: an agent with directions replaces it (and the
    preview says so); an agent with none leaves it."""
    user, mates = home
    _add("release-bot")
    other = dict(
        TEMPLATE, name="triage-bot", prompt="Triage SOP.", mcpServers={"notes": {"command": "n"}}
    )
    (user / "triage-bot.json").write_text(json.dumps(other), encoding="utf-8")
    preview = mate_worker_kit.preview("pixel", {"template": "triage-bot"})
    assert {"section": "prompt", "id": "prompt", "change": "replaced"} in preview["changes"]
    # The preview shows the directions the replacement overwrites.
    assert preview["current_directions"] == TEMPLATE["prompt"]
    _add("triage-bot")
    assert _file(mates)["prompt"] == "Triage SOP."

    silent = dict(TEMPLATE, name="quiet-bot", prompt="", mcpServers={"pad": {"command": "p"}})
    (user / "quiet-bot.json").write_text(json.dumps(silent), encoding="utf-8")
    preview = mate_worker_kit.preview("pixel", {"template": "quiet-bot"})
    assert not [r for r in preview["changes"] if r["section"] == "prompt"]
    _add("quiet-bot")
    assert _file(mates)["prompt"] == "Triage SOP."


@pytest.mark.parametrize(
    ("copied", "change"),
    [
        ("Worker base.", "added"),
        ("Worker", "added"),
        (f"Worker base.\n\n{TEMPLATE['prompt']}", "skipped"),
    ],
)
def test_the_directions_preview_reads_the_slot_beyond_the_base_prompt(home, copied, change):
    """A file that copies the worker base's own prompt (or a prefix the base opens
    with) holds no directions, so an add with some fills the slot; a file holding
    the base prompt plus the same directions already has them."""
    _, mates = home
    _write_base(mates, {})
    spec = _file(mates)
    spec["prompt"] = copied
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")

    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})

    rows = [r for r in preview["changes"] if r["section"] == "prompt"]
    assert rows == [{"section": "prompt", "id": "prompt", "change": change}]


def test_the_file_does_not_track_the_template(home):
    user, mates = home
    _add()
    (user / "release-bot.json").write_text(
        json.dumps(dict(TEMPLATE, mcpServers={"jira": {"command": "rewritten"}})),
        encoding="utf-8",
    )
    # The file keeps the entry it was written, unaffected by a later template edit.
    assert _file(mates)["mcpServers"]["jira"]["command"] == "jira-mcp"


def test_an_apply_needs_the_previews_revision(home):
    _, mates = home
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.apply("pixel", {"template": "release-bot"})
    assert caught.value.code == "revision_required"
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    spec = _file(mates)
    spec["prompt"] = "edited in another tab"
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    assert caught.value.code == "stale_revision"


def test_an_apply_after_the_template_changed_is_refused(home):
    """The preview binds the template's kit too: a server added since was never shown."""
    user, mates = home
    before = _file(mates)
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    changed = {**TEMPLATE, "mcpServers": {**TEMPLATE["mcpServers"], "shell": {"command": "sh"}}}
    (user / "release-bot.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.apply("pixel", {"template": "release-bot", "revision": preview["revision"]})
    assert caught.value.code == "stale_revision"
    assert _file(mates) == before


def test_an_added_grant_the_ceiling_withholds_is_not_written(home, monkeypatch):
    _, mates = home
    monkeypatch.setattr(governance, "may_skip_gate_now", lambda ref: not ref.startswith("@jira"))
    _add()
    assert "@jira/search" not in _file(mates).get("allowedTools", [])


def test_an_unreadable_template_is_refused(home):
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview("pixel", {"template": "nobody"})
    assert caught.value.code == "template_unreadable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/agents/pixel/worker-kit"),
        ("POST", "/api/agents/pixel/worker-kit/preview"),
        ("PUT", "/api/agents/pixel/worker-kit"),
    ],
)
async def test_every_worker_kit_route_is_owner_only(method, path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.routes import agents as agents_routes

    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = type("S", (), {"owner_id": ""})()
    agents_routes.register(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, path, headers={"X-Test-User": "someone-else"})
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"


def test_adding_the_same_template_onto_a_migrated_file_changes_nothing(home):
    """A migrated file already holds what the template adds, so re-adding only skips."""
    user, mates = home
    base = {
        "name": "kirocrew-mate-worker",
        "prompt": "Worker base.",
        "tools": ["@kirocrew-core"],
        "allowedTools": ["@kirocrew-core/cron_list"],
        "mcpServers": {"kirocrew-core": {"command": "kc", "autoApprove": ["cron_list"]}},
    }
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(base), encoding="utf-8")
    template = {
        **json.loads(json.dumps(base)),
        "name": "release-bot",
        "prompt": "Worker base.\n\nRelease SOP.",
        "allowedTools": [*base["allowedTools"], "@jira/search"],
        "mcpServers": {**base["mcpServers"], "jira": {"command": "jira-mcp"}},
    }
    (user / "release-bot.json").write_text(json.dumps(template), encoding="utf-8")
    migrated = {
        "name": "pixel",
        "prompt": "Release SOP.",
        "tools": ["@jira"],
        "allowedTools": ["@jira/search"],
        "mcpServers": {"jira": {"command": "jira-mcp"}},
    }
    (mates / "pixel.json").write_text(json.dumps(migrated), encoding="utf-8")

    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})

    # jira is already in the file; kirocrew-core is the base's own: both skipped.
    assert not [
        r for r in preview["changes"] if r["change"] == "added" and r["section"] != "prompt"
    ]


def test_the_preview_does_not_re_add_a_base_server(home):
    """A template server the worker base already declares is skipped, not added."""
    user, mates = home
    base = {
        "name": "kirocrew-mate-worker",
        "prompt": "Worker base.",
        "tools": ["@kirocrew-core"],
        "mcpServers": {"kirocrew-core": {"command": "kc"}},
    }
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(base), encoding="utf-8")
    template = {
        **TEMPLATE,
        "mcpServers": {**TEMPLATE["mcpServers"], "kirocrew-core": {"command": "kc"}},
    }
    (user / "release-bot.json").write_text(json.dumps(template), encoding="utf-8")

    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    changed = {(row["section"], row["id"], row["change"]) for row in preview["changes"]}
    assert ("mcpServers", "kirocrew-core", "skipped") in changed
    assert ("mcpServers", "jira", "added") in changed


def test_adopting_a_name_a_user_level_agent_declares_is_refused(home, monkeypatch):
    """The binding would name that agent, and the crewmate would run it, not its copy."""
    from types import SimpleNamespace

    from kiro_crew import mate_config
    from kiro_crew.config import loader

    binding = SimpleNamespace(kiro_agent="kirocrew", memory_store="member-release-bot")
    cfg = SimpleNamespace(agents={"release-bot": binding})
    monkeypatch.setattr(loader, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    written: list[str] = []
    monkeypatch.setattr(mate_config, "write_mate_config", lambda name, src: written.append(name))

    with pytest.raises(mate_config.MateConfigError):
        mate_config.adopt_own_file("release-bot")
    assert written == []
    assert binding.kiro_agent == "kirocrew"


@pytest.mark.parametrize(
    "limit",
    [
        {"permissions": {"rules": [{"effect": "deny", "tools": ["@jira/delete_issue"]}]}},
        {"hooks": {"preToolUse": [{"command": "deny.sh"}]}},
    ],
)
def test_a_template_whose_limits_the_worker_cannot_carry_is_refused(home, limit):
    """A deny rule or a hook would not reach the worker, so no copy is made."""
    user, mates = home
    limited = {**TEMPLATE, "excludedTools": ["@jira/delete_issue"], **limit}
    (user / "release-bot.json").write_text(json.dumps(limited), encoding="utf-8")
    before = _file(mates)
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert caught.value.code == "template_restrictions_uncarried"
    assert _file(mates) == before


def test_a_template_exclusion_is_carried_into_the_crewmates_file(home):
    user, mates = home
    excluded = {**TEMPLATE, "excludedTools": ["@jira/delete_issue"]}
    (user / "release-bot.json").write_text(json.dumps(excluded), encoding="utf-8")
    _add()
    assert "@jira/delete_issue" in _file(mates)["excludedTools"]


def test_creating_a_crewmate_reads_nothing_from_its_starting_template(home, monkeypatch):
    """Create writes the source-free file and repoints the binding without reading
    the starting template, whatever that template sets."""
    from types import SimpleNamespace

    from kiro_crew import mate_config
    from kiro_crew.config import loader

    user, _ = home
    limited = {**TEMPLATE, "toolsSettings": {"fs_write": {"deniedPaths": ["/etc"]}}}
    (user / "release-bot.json").write_text(json.dumps(limited), encoding="utf-8")
    binding = SimpleNamespace(kiro_agent="release-bot", memory_store="member-ada")
    monkeypatch.setattr(
        loader,
        "KiroCrewConfig",
        SimpleNamespace(load=lambda: SimpleNamespace(agents={"ada": binding})),
    )
    monkeypatch.setattr(loader, "_invalidate_config_cache", lambda: None)
    monkeypatch.setattr(mate_config, "user_agent_named", lambda name: False)

    def _no_read(*_a, **_k):
        raise AssertionError("create must not read the starting template")

    monkeypatch.setattr(mate_config, "_resolved_source_spec", _no_read)
    written: list[str] = []
    monkeypatch.setattr(mate_config, "write_mate_config", lambda name: written.append(name))
    persisted: list[str] = []
    import kiro_crew.memory_stores as stores

    monkeypatch.setattr(
        stores, "persist_member_config", lambda cfg, name, **_k: persisted.append(name)
    )

    assert mate_config.adopt_own_file("ada") == "copied"
    assert written == ["ada"] and binding.kiro_agent == "ada" and persisted == ["ada"]


def test_the_preview_lists_a_tool_the_add_turns_off(home):
    """An exclusion the add carries is a change the owner sees before applying."""
    user, _ = home
    excluded = {**TEMPLATE, "excludedTools": ["execute_bash"]}
    (user / "release-bot.json").write_text(json.dumps(excluded), encoding="utf-8")
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert {"section": "excludedTools", "id": "execute_bash", "change": "added"} in preview[
        "changes"
    ]


def _write_base(mates, servers: dict) -> None:
    base = {
        "name": "kirocrew-mate-worker",
        "prompt": "Worker base.",
        "tools": [f"@{name}" for name in servers],
        "mcpServers": servers,
    }
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(base), encoding="utf-8")


def test_a_skipped_base_servers_disabled_tools_are_carried_as_exclusions(home):
    """The add skips a server the base declares, but not the template's limits on it."""
    user, mates = home
    _write_base(mates, {"jira": {"command": "jira-mcp"}})
    template = {
        **TEMPLATE,
        "allowedTools": ["@notes/read"],
        "mcpServers": {
            "jira": {"command": "jira-mcp", "disabledTools": ["delete_issue"]},
            "notes": {"command": "notes-mcp"},
        },
    }
    (user / "release-bot.json").write_text(json.dumps(template), encoding="utf-8")

    preview = _add()

    ref = mate_worker.disabled_tool_refs({"jira": {"disabledTools": ["delete_issue"]}})[0]
    spec = _file(mates)
    assert ref in spec["excludedTools"]
    assert "jira" not in spec["mcpServers"]
    assert "notes" in spec["mcpServers"] and "@notes/read" in spec["allowedTools"]
    assert {"section": "excludedTools", "id": ref, "change": "added"} in preview["changes"]


def test_a_template_muting_a_base_server_switches_it_off_in_the_file(home):
    user, mates = home
    _write_base(mates, {"jira": {"command": "jira-mcp"}})
    template = {
        **TEMPLATE,
        "allowedTools": [],
        "mcpServers": {"jira": {"command": "jira-mcp", "disabled": True}},
    }
    (user / "release-bot.json").write_text(json.dumps(template), encoding="utf-8")

    preview = _add()

    assert _file(mates)["mcpServers"]["jira"] == {"disabled": True}
    assert {"section": "disabledServers", "id": "jira", "change": "added"} in preview["changes"]


def test_adding_keeps_the_templates_tool_selection_on_its_servers(home):
    """A template mounting one tool of a server gives the file, and the worker, that tool."""
    user, mates = home
    narrow = {
        **TEMPLATE,
        "tools": ["fs_read", "@notes/read"],
        "allowedTools": [],
        "mcpServers": {"notes": {"command": "notes-mcp"}},
    }
    (user / "release-bot.json").write_text(json.dumps(narrow), encoding="utf-8")
    _add()
    tools = _file(mates)["tools"]
    assert "@notes/read" in tools and "@notes" not in tools
    base = {"name": "kirocrew-mate-worker", "prompt": "Worker.", "mcpServers": {}, "tools": []}
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, _file(mates)))
    assert [t for t in merged["tools"] if t.startswith("@notes")] == ["@notes/read"]


def test_a_star_selector_from_a_template_is_never_copied(home):
    """``*`` would select every built-in; the kit states selectors per server instead."""
    user, mates = home
    star = {**TEMPLATE, "tools": ["*"], "mcpServers": {"notes": {"command": "notes-mcp"}}}
    (user / "release-bot.json").write_text(json.dumps(star), encoding="utf-8")
    _add()
    assert "*" not in _file(mates)["tools"]
    assert "@notes" in _file(mates)["tools"]


@pytest.mark.asyncio
async def test_the_worker_kit_routes_serve_the_owner(home, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import member_worker_kit as route

    async def _owner(request, operation):
        return None

    monkeypatch.setattr(route, "require_owner_dashboard_request", _owner)
    refreshed: list[str] = []
    app = web.Application()
    app["state"] = type("S", (), {"push_refresh": lambda self, what: refreshed.append(what)})()
    for method, path in (("GET", ""), ("POST", "/preview"), ("PUT", "")):
        app.router.add_route(method, "/k/{name}" + path, route.api_member_worker_kit)
    async with TestClient(TestServer(app)) as client:
        assert (await (await client.get("/k/pixel")).json())["member"] == "pixel"
        bad = await client.post("/k/pixel/preview", data="{")
        assert (bad.status, (await bad.json())["code"]) == (400, "invalid_json")
        preview = await client.post("/k/pixel/preview", json={"template": "release-bot"})
        revision = (await preview.json())["revision"]
        body = {"template": "release-bot", "revision": revision}
        put = await client.put("/k/pixel", json=body)
        assert put.status == 200 and refreshed == ["agents"]
        stale = await client.put("/k/pixel", json=body)
        assert (stale.status, (await stale.json())["code"]) == (409, "stale_revision")
        missing = await client.get("/k/peer")
        assert (await missing.json())["code"] == "not_a_mate_file"


@pytest.mark.asyncio
async def test_adding_reads_a_null_exclusion_list_copied_from_the_template(home, monkeypatch):
    """A template's ``excludedTools: null`` at create reads as no exclusion on a later add."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew import mate_config
    from kiro_crew.config import loader
    from kiro_crew.dashboard.handlers import member_worker_kit as route

    async def _owner(request, operation):
        return None

    user, mates = home
    monkeypatch.setattr(loader, "publish_mate_agents", lambda names: None)
    monkeypatch.setattr(route, "require_owner_dashboard_request", _owner)
    (user / "release-bot.json").write_text(
        json.dumps({**TEMPLATE, "excludedTools": None}), encoding="utf-8"
    )
    mate_config.write_mate_config("pixel")
    # The minimal file copies nothing from the template, so it carries no
    # ``excludedTools`` of its own; the add flow below still reads the template's.
    assert "excludedTools" not in _file(mates) or _file(mates)["excludedTools"] in (None, [])

    app = web.Application()
    app["state"] = type("S", (), {"push_refresh": lambda self, what: None})()
    for method, path in (("POST", "/preview"), ("PUT", "")):
        app.router.add_route(method, "/k/{name}" + path, route.api_member_worker_kit)

    async def _re_add(client) -> list[dict]:
        preview = await client.post("/k/pixel/preview", json={"template": "release-bot"})
        assert preview.status == 200
        body = await preview.json()
        put = await client.put(
            "/k/pixel", json={"template": "release-bot", "revision": body["revision"]}
        )
        assert put.status == 200
        return body["changes"]

    async with TestClient(TestServer(app)) as client:
        changes = await _re_add(client)
        assert not [row for row in changes if row["section"] == "excludedTools"]

        (user / "release-bot.json").write_text(
            json.dumps({**TEMPLATE, "excludedTools": ["execute_bash"]}), encoding="utf-8"
        )
        await _re_add(client)
    assert _file(mates)["excludedTools"] == ["execute_bash"]


@pytest.mark.parametrize(
    "refs, expected",
    [
        (
            ["@notes/read", "fs_read", "@notes", "@notes-other/read", "fs_read"],
            ["fs_read", "@notes", "@notes-other/read"],
        ),
        (["@notes", "@notes/read", "fs_read", "*", "*"], ["*"]),
        (["@notes/read", "@notes/write", "@notes/read"], ["@notes/read", "@notes/write"]),
        ([], []),
    ],
)
def test_selector_collapse_keeps_order_and_uncovered_selectors(refs, expected):
    assert mate_worker.collapse_selectors(refs) == expected


@pytest.mark.parametrize(
    "allowed, auto, selected, expected",
    [
        (["@notes"], [], "write", True),
        (["@notes/write"], [], "write", True),
        ([], ["write"], "write", True),
        ([], [], "write", False),
        (["@notes/write_*"], [], "write_note", True),
        (["@notes/*"], [], "write_note", True),
        (["@notes/write_not?"], [], "write_note", True),
        (["@notes/writ?"], [], "write", True),
        (["@notes/read_*"], [], "write_note", False),
        (["@other/write_*"], [], "write_note", False),
        (["*"], [], "write_note", True),
        ([], ["write_*"], "write_note", True),
        ([], ["write_not?"], "write_note", True),
        (["@notes/write_note"], [], "write_*", True),
    ],
)
def test_re_added_tool_reports_existing_approval(
    home, monkeypatch, allowed, auto, selected, expected
):
    user, mates = home
    monkeypatch.setattr(mate_worker_kit, "_worker_base_or_none", lambda: {})
    spec = _file(mates)
    spec.update(
        tools=["@notes/read"],
        allowedTools=allowed,
        mcpServers={"notes": {"command": "notes", "autoApprove": auto}},
    )
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    ref = f"@notes/{selected}"
    template = {"tools": [ref], "mcpServers": {"notes": {"command": "notes"}}}
    (user / "notes-agent.json").write_text(json.dumps(template), encoding="utf-8")

    preview = mate_worker_kit.preview("pixel", {"template": "notes-agent"})

    assert preview["runs_unasked"] is expected
    assert {"section": "tools", "id": ref, "change": "added"} in preview["changes"]
    assert not [row for row in preview["changes"] if row["section"] == "allowedTools"]


@pytest.mark.parametrize("auto, expected", [([], False), (["write"], True)])
def test_plain_add_reports_whether_selected_tools_run_unasked(home, monkeypatch, auto, expected):
    user, _ = home
    monkeypatch.setattr(mate_worker_kit, "_worker_base_or_none", lambda: {})
    template = {
        "tools": ["@notes"],
        "mcpServers": {"notes": {"command": "notes", "autoApprove": auto}},
    }
    (user / "notes-agent.json").write_text(json.dumps(template), encoding="utf-8")

    preview = mate_worker_kit.preview("pixel", {"template": "notes-agent"})

    assert preview["runs_unasked"] is expected


@pytest.fixture(params=["allowedTools", "autoApprove"])
def notes_approval(home, monkeypatch, request):
    user, mates = home
    monkeypatch.setattr(mate_worker_kit, "_worker_base_or_none", lambda: {})
    monkeypatch.setattr(mate_capabilities, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: "pixel")
    template = {
        "name": "notes-reader",
        "tools": ["@notes/read"],
        "mcpServers": {"notes": {"command": "notes-mcp"}},
    }
    if request.param == "allowedTools":
        template["allowedTools"] = ["@notes/read"]
    else:
        template["mcpServers"]["notes"]["autoApprove"] = ["read"]
    (user / "notes-reader.json").write_text(json.dumps(template), encoding="utf-8")
    return mates, request.param


def test_re_adding_a_removed_server_restores_its_revoked_approval(notes_approval):
    mates, channel = notes_approval
    _add("notes-reader")
    body = {
        "revision": mate_capabilities._revision(_file(mates)),
        "operations": [{"section": "mcpServers", "id": "notes", "action": "remove"}],
        "accept_parent": [],
        "accept_members": [],
    }
    removal = mate_capabilities.preview("pixel", body, {})
    mate_capabilities.put("pixel", {**body, "preview_token": removal["preview_token"]}, {})
    assert "notes" not in _file(mates)["mcpServers"]
    assert revocations.revoked("pixel") == ["@notes/read"]
    revocations.record("pixel", ["@other/write"], [])

    preview = _add("notes-reader")

    assert [row["id"] for row in preview["changes"] if row["section"] == "allowedTools"] == [
        "@notes/read"
    ]
    assert revocations.revoked("pixel") == ["@other/write"]
    resolved = mate_worker.resolve_worker_spec({}, "pixel")
    if channel == "allowedTools":
        assert "@notes/read" in resolved["allowedTools"]
    else:
        assert "read" in resolved["mcpServers"]["notes"]["autoApprove"]


def test_adding_skips_a_retained_servers_revoked_approval(notes_approval):
    mates, channel = notes_approval
    spec = _file(mates)
    spec["mcpServers"] = {"notes": {"command": "own-notes"}}
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    revocations.record("pixel", ["@notes/read"], [])

    preview = _add("notes-reader")

    assert {"section": "mcpServers", "id": "notes", "change": "skipped"} in preview["changes"]
    assert not [row for row in preview["changes"] if row["section"] == "allowedTools"]
    assert revocations.revoked("pixel") == ["@notes/read"]
    resolved = mate_worker.resolve_worker_spec({}, "pixel")
    if channel == "allowedTools":
        assert "@notes/read" not in resolved.get("allowedTools", [])
    else:
        assert "read" not in resolved["mcpServers"]["notes"].get("autoApprove", [])


def test_a_failed_add_write_keeps_its_approval_revoked(notes_approval, monkeypatch):
    from kiro_crew import atomic_write

    mates, _ = notes_approval
    revocations.record("pixel", ["@notes/read"], [])
    before = _file(mates)

    def fail_write(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(atomic_write, "atomic_write", fail_write)
    with pytest.raises(OSError, match="write failed"):
        _add("notes-reader")

    assert _file(mates) == before
    assert revocations.revoked("pixel") == ["@notes/read"]


@pytest.mark.parametrize("selected", ["@notes", "@notes/*", "@notes/write_note"])
def test_existing_glob_selected_tools_do_not_report_new_unasked_calls(selected):
    before = {
        "mcpServers": {"notes": {"command": "notes"}},
        "tools": ["@notes/*"],
        "allowedTools": ["@notes"],
    }
    after = {**before, "tools": ["@notes/*", selected]}
    assert mate_worker_kit._runs_unasked(before, after) is False


@pytest.mark.parametrize("selected", ["@notes", "@notes/", "@notes/*"])
def test_whole_server_selection_reports_existing_whole_server_approval(selected):
    before = {
        "mcpServers": {"notes": {"command": "notes"}},
        "tools": ["@notes/read"],
        "allowedTools": ["@notes"],
    }
    assert mate_worker_kit._runs_unasked(before, {**before, "tools": [selected]}) is True
