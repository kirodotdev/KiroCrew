# SPDX-License-Identifier: Apache-2.0
"""A crewmate's worker kit: one template copied into its file once, re-copied on confirm.

``rfc-mate-owned-agent-config`` §5.1: choosing a template copies its prompt,
servers (with their auto-approvals), skills and approvals into the crewmate's
own file once; the file then does not track the template; a re-copy is
previewed, asks whether to keep what was added since, and writes only on an
apply that carries the preview's revision.
"""

from __future__ import annotations

import json

import pytest

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


def _copy(template="release-bot", keep=True):
    preview = mate_worker_kit.preview("pixel", {"template": template, "keep_additions": keep})
    mate_worker_kit.apply(
        "pixel", {"template": template, "keep_additions": keep, "revision": preview["revision"]}
    )
    return preview


def test_a_binding_without_its_own_file_has_no_worker_kit(home):
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.view("peer")
    assert caught.value.code == "not_a_mate_file"


def test_the_preview_names_every_change_and_writes_nothing(home):
    _, mates = home
    before = (mates / "pixel.json").read_bytes()
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    changed = {(row["section"], row["id"], row["change"]) for row in preview["changes"]}
    assert ("prompt", "prompt", "changed") in changed
    assert ("mcpServers", "jira", "added") in changed
    assert ("resources", "skill://release/SKILL.md", "added") in changed
    assert ("allowedTools", "@jira/search", "added") in changed
    assert (mates / "pixel.json").read_bytes() == before
    assert not mate_worker_kit.kit_record_path("pixel").exists()


def test_choosing_copies_the_whole_kit_once(home):
    _, mates = home
    _copy()
    spec = _file(mates)
    assert spec["prompt"] == TEMPLATE["prompt"]
    assert spec["mcpServers"]["jira"]["autoApprove"] == ["search"]
    assert spec["resources"] == TEMPLATE["resources"]
    assert spec["allowedTools"] == ["@jira/search"]
    assert "@jira" in spec["tools"]
    assert spec["name"] == "pixel"
    assert mate_worker_kit.view("pixel")["template"] == "release-bot"


def test_the_copied_kit_reaches_the_worker(home):
    _, mates = home
    _copy()
    base = {"name": "kirocrew-mate-worker", "prompt": "Worker.", "mcpServers": {}, "tools": []}
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, _file(mates)))
    assert merged["prompt"] == "Worker.\n\nRelease SOP: tag, build, announce."
    assert "jira" in merged["mcpServers"]


def test_the_file_does_not_track_the_template(home):
    user, mates = home
    _copy()
    (user / "release-bot.json").write_text(
        json.dumps(dict(TEMPLATE, prompt="A rewritten SOP.")), encoding="utf-8"
    )
    assert _file(mates)["prompt"] == TEMPLATE["prompt"]


def test_a_recopy_keeps_what_was_added_since_when_asked(home):
    user, mates = home
    _copy()
    spec = _file(mates)
    spec["mcpServers"]["github"] = {"command": "gh-mcp"}
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    (user / "release-bot.json").write_text(
        json.dumps(dict(TEMPLATE, prompt="A rewritten SOP.")), encoding="utf-8"
    )
    _copy(keep=True)
    spec = _file(mates)
    assert spec["prompt"] == "A rewritten SOP."
    assert "github" in spec["mcpServers"]


def test_a_recopy_that_discards_drops_what_was_added_since(home):
    _, mates = home
    _copy()
    spec = _file(mates)
    spec["mcpServers"]["github"] = {"command": "gh-mcp"}
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    preview = _copy(keep=False)
    assert {"section": "mcpServers", "id": "github", "change": "removed"} in preview["changes"]
    assert "github" not in _file(mates)["mcpServers"]


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


def test_a_copied_grant_the_ceiling_withholds_is_not_written(home, monkeypatch):
    _, mates = home
    monkeypatch.setattr(governance, "may_skip_gate_now", lambda ref: not ref.startswith("@jira"))
    _copy()
    assert "@jira/search" not in _file(mates)["allowedTools"]


def test_an_unreadable_template_is_refused(home):
    with pytest.raises(WorkerKitError) as caught:
        mate_worker_kit.preview("pixel", {"template": "nobody"})
    assert caught.value.code == "template_unreadable"


def test_the_create_time_record_lets_a_recopy_tell_the_kit_from_additions(home):
    _, mates = home
    # A create that copied the template wholesale, then recorded it.
    spec = dict(TEMPLATE, name="pixel")
    spec["mcpServers"] = dict(TEMPLATE["mcpServers"], github={"command": "gh-mcp"})
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    mate_worker_kit.record_initial_kit("pixel", "release-bot")
    view = mate_worker_kit.view("pixel")
    assert view["template"] == "release-bot"
    assert view["has_additions_since"]
    _copy(keep=False)
    assert set(_file(mates)["mcpServers"]) == {"jira"}


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


@pytest.mark.asyncio
@pytest.mark.parametrize("source,recorded", [("release-bot", True), ("kirocrew", False)])
async def test_create_records_a_non_default_template_as_the_kit(monkeypatch, source, recorded):
    """A crewmate created from a template other than the default starts with that
    template as its worker kit on record, so its first re-copy can tell the kit
    from what was added later."""
    import contextlib
    from types import SimpleNamespace

    from kiro_crew import mate_config
    from kiro_crew.dashboard.handlers import agents as handlers

    binding = SimpleNamespace(kiro_agent=source, memory_store="member-pixel")
    cfg = SimpleNamespace(agents={"pixel": binding})
    seen: list[tuple[str, str]] = []

    async def _to_thread(fn, *args):
        return fn(*args)

    from kiro_crew import memory_stores
    from kiro_crew.config import loader

    # The create route runs the shared copy-and-repoint (mate_config.adopt_own_file).
    monkeypatch.setattr(loader, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(handlers, "_get_config_lock", contextlib.asynccontextmanager(_noop_lock))
    monkeypatch.setattr(handlers, "_drained_to_thread", _to_thread)
    monkeypatch.setattr(memory_stores, "persist_member_config", lambda *a, **k: None)
    monkeypatch.setattr(mate_config, "write_mate_config", lambda name, src: None)
    monkeypatch.setattr(mate_config, "_resolved_source_spec", lambda src: {"name": src})
    monkeypatch.setattr(
        mate_worker_kit, "record_initial_kit", lambda name, template: seen.append((name, template))
    )

    assert await handlers._eager_copy_mate_config("pixel") == "copied"
    assert seen == ([("pixel", source)] if recorded else [])


async def _noop_lock():
    yield


def test_re_copying_the_same_template_onto_a_migrated_file_changes_nothing(home):
    """A migrated file holds only what the template adds to the worker base."""
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

    assert preview["changes"] == []


@pytest.mark.parametrize(
    ("entry", "row"),
    [
        ({"disabled": True}, {"section": "disabledServers", "id": "kirocrew-core"}),
        (
            {"command": "kc", "disabledTools": ["cron_add"]},
            {"section": "disabledTools", "id": "@kirocrew-core/cron_add"},
        ),
    ],
)
def test_the_preview_lists_what_a_copy_restricts_on_a_base_server(home, entry, row):
    """The base's entry wins the merge, so the limit is listed on its own, both ways."""
    user, mates = home
    base = {
        "name": "kirocrew-mate-worker",
        "prompt": "Worker base.",
        "tools": ["@kirocrew-core"],
        "mcpServers": {"kirocrew-core": {"command": "kc"}},
    }
    (mates / "kirocrew-mate-worker.json").write_text(json.dumps(base), encoding="utf-8")
    template = {**TEMPLATE, "mcpServers": {**TEMPLATE["mcpServers"], "kirocrew-core": entry}}
    (user / "release-bot.json").write_text(json.dumps(template), encoding="utf-8")

    assert {**row, "change": "added"} in _copy()["changes"]

    (user / "release-bot.json").write_text(json.dumps(TEMPLATE), encoding="utf-8")
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot", "keep_additions": False})
    assert {**row, "change": "removed"} in preview["changes"]


def test_a_private_copy_names_the_template_it_was_forked_from(monkeypatch):
    from kiro_crew import agent_state, mate_worker_kit

    lineage = {
        "crew-5eed01": {"forked_from": "release-helper", "private_to": "cy"},
        "crew-0a0b0c": {"forked_from": "crew-5eed01", "private_to": "cy"},
        "crew-dead00": {"forked_from": "crew-dead00", "private_to": "cy"},
    }
    monkeypatch.setattr(agent_state, "get_fork_info", lambda name, **_: lineage.get(name))
    origin = mate_worker_kit._private_copy_origin
    assert origin("crew-5eed01") == (True, "release-helper")
    # A copy of a copy names the first real template.
    assert origin("crew-0a0b0c") == (True, "release-helper")
    # A private copy with no recorded origin names none, never its own id.
    assert origin("crew-abc123") == (True, "")
    assert origin("crew-dead00") == (True, "")
    assert origin("release-helper") == (False, "release-helper")


def test_a_switch_reaches_the_next_worker_and_leaves_a_running_one_on_its_kit(home, monkeypatch):
    """A worker's spec is projected once, when its session starts.

    A session that started before the switch keeps the spec it was handed, and
    the next worker projected after it carries the new template's prompt.
    """
    user, _ = home
    other = dict(TEMPLATE, name="triage-bot", prompt="Triage SOP: label, route.")
    (user / "triage-bot.json").write_text(json.dumps(other), encoding="utf-8")
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member: "pixel")
    base = {"name": "kirocrew-mate-worker", "prompt": "Worker.", "mcpServers": {}, "tools": []}
    _copy("release-bot")
    running = mate_worker.resolve_worker_spec(base, "pixel")

    preview = _copy("triage-bot", keep=False)

    assert preview["template"] == "triage-bot" and preview["changes"]
    assert mate_worker_kit.view("pixel")["template"] == "triage-bot"
    assert running["prompt"] == "Worker.\n\nRelease SOP: tag, build, announce."
    after = mate_worker.resolve_worker_spec(base, "pixel")
    assert after["prompt"] == "Worker.\n\nTriage SOP: label, route."


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
    _copy()
    assert "@jira/delete_issue" in _file(mates)["excludedTools"]


def test_creating_from_a_template_with_uncarried_limits_keeps_its_template(home, monkeypatch):
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
    written: list[str] = []
    monkeypatch.setattr(mate_config, "write_mate_config", lambda name, src: written.append(name))

    with pytest.raises(mate_config.MateConfigError, match="toolsSettings"):
        mate_config.adopt_own_file("ada")
    assert written == [] and binding.kiro_agent == "release-bot"


def test_the_preview_lists_a_tool_the_copy_turns_off(home):
    """An exclusion the copy adds is a change the owner sees before applying."""
    user, _ = home
    excluded = {**TEMPLATE, "excludedTools": ["execute_bash"]}
    (user / "release-bot.json").write_text(json.dumps(excluded), encoding="utf-8")
    preview = mate_worker_kit.preview("pixel", {"template": "release-bot"})
    assert {"section": "excludedTools", "id": "execute_bash", "change": "added"} in preview[
        "changes"
    ]


def test_a_recopy_keeps_the_templates_tool_selection_on_its_servers(home):
    """A template mounting one tool of a server gives the file, and the worker, that tool."""
    user, mates = home
    narrow = {
        **TEMPLATE,
        "tools": ["fs_read", "@notes/read"],
        "allowedTools": [],
        "mcpServers": {"notes": {"command": "notes-mcp"}},
    }
    (user / "release-bot.json").write_text(json.dumps(narrow), encoding="utf-8")
    _copy()
    tools = _file(mates)["tools"]
    assert "@notes/read" in tools and "@notes" not in tools
    base = {"name": "kirocrew-mate-worker", "prompt": "Worker.", "mcpServers": {}, "tools": []}
    merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, _file(mates)))
    assert [t for t in merged["tools"] if t.startswith("@notes")] == ["@notes/read"]


@pytest.mark.parametrize("keep", [True, False])
def test_a_recopy_that_narrows_a_selector_replaces_the_broader_one(home, keep):
    """A template narrowed from ``@notes`` to ``@notes/read`` leaves the file only the read tool."""
    user, mates = home
    broad = {
        **TEMPLATE,
        "tools": ["fs_read", "@notes"],
        "allowedTools": [],
        "mcpServers": {"notes": {"command": "notes-mcp"}},
    }
    (user / "release-bot.json").write_text(json.dumps(broad), encoding="utf-8")
    _copy()
    assert "@notes" in _file(mates)["tools"]

    narrow = {**broad, "tools": ["fs_read", "@notes/read"]}
    (user / "release-bot.json").write_text(json.dumps(narrow), encoding="utf-8")
    preview = _copy(keep=keep)

    tools = _file(mates)["tools"]
    assert [t for t in tools if t.startswith("@notes")] == ["@notes/read"]
    changed = {(row["section"], row["id"], row["change"]) for row in preview["changes"]}
    assert ("tools", "@notes", "removed") in changed
    assert ("tools", "@notes/read", "added") in changed


def test_a_selector_added_since_the_copy_is_kept_only_when_asked(home):
    user, mates = home
    template = {
        **TEMPLATE,
        "tools": ["@notes/read"],
        "allowedTools": [],
        "mcpServers": {"notes": {"command": "notes-mcp"}},
    }
    (user / "release-bot.json").write_text(json.dumps(template), encoding="utf-8")
    _copy()
    spec = _file(mates)
    spec["tools"] = [*spec["tools"], "@notes/save"]
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")

    _copy(keep=True)
    assert "@notes/save" in _file(mates)["tools"]
    _copy(keep=False)
    assert [t for t in _file(mates)["tools"] if t.startswith("@notes")] == ["@notes/read"]


def test_a_recopy_narrows_a_file_whose_copy_selected_every_tool(home):
    """A file copied from a ``*`` template still narrows when the template does."""
    user, mates = home
    star = {**TEMPLATE, "allowedTools": [], "mcpServers": {"notes": {"command": "notes-mcp"}}}
    (user / "release-bot.json").write_text(json.dumps(star), encoding="utf-8")
    spec = _file(mates)
    spec["tools"] = ["*"]
    (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")
    _copy()

    narrow = {**star, "tools": ["fs_read", "@notes/read"]}
    (user / "release-bot.json").write_text(json.dumps(narrow), encoding="utf-8")
    _copy(keep=True)

    assert _file(mates)["tools"] == ["@notes/read"]


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
