"""A session a palette command opened for its app's OWN agent runs only as that agent.

The provider resolves the bare agent name project-first, so a same-named spec in the
session's project (or a second user-level spec) would run the app-authored prompt
with that other spec's tools. The turn is refused before the prompt is sent.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes
from dashboard_owner_helpers import as_owner
from test_chat_agent_selection import _turn_state
from turn_harness import SlotSpec, TurnScript, run_turn

from kiro_crew.apps import bridges, manager
from kiro_crew.config.loader import KiroCrewConfig


def _agents(tmp_path, monkeypatch):
    agents_dir = tmp_path / "kiro-agents"
    agents_dir.mkdir()
    (agents_dir / "pr-bulk-ops--deploy.json").write_text('{"name": "deploy"}')
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents_dir)
    return agents_dir


def test_the_registered_unique_agent_is_allowed(tmp_path, monkeypatch):
    _agents(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    assert manager.app_command_agent_refusal("pr-bulk-ops", "deploy", str(project)) is None


def test_a_project_agent_with_the_same_name_is_refused(tmp_path, monkeypatch):
    _agents(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / "deploy.json").write_text(
        '{"name": "deploy", "allowedTools": ["*"]}'
    )
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", str(project))
    assert refusal and 'this project has its own agent named "deploy"' in refusal


def test_a_second_user_level_spec_with_the_same_name_is_refused(tmp_path, monkeypatch):
    agents_dir = _agents(tmp_path, monkeypatch)
    (agents_dir / "alpha-deploy.json").write_text('{"name": "deploy"}')
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None)
    assert refusal and 'also named "deploy"' in refusal
    assert "Rename or remove alpha-deploy.json" in refusal
    assert "pr-bulk-ops--deploy.json" not in refusal


def test_a_user_level_spec_claiming_the_name_by_filename_is_refused(tmp_path, monkeypatch):
    agents_dir = _agents(tmp_path, monkeypatch)
    # The resolver also matches a spec by its filename stem, so ``deploy.json``
    # claims ``deploy`` even though it declares no ``name``.
    (agents_dir / "deploy.json").write_text('{"allowedTools": ["*"]}')
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None)
    assert refusal and "Rename or remove deploy.json" in refusal
    assert manager._app_registered_agent("pr-bulk-ops", "deploy") is False


def test_a_refusal_names_the_app_by_its_display_name(tmp_path, monkeypatch):
    agents_dir = _agents(tmp_path, monkeypatch)
    (agents_dir / "alpha-deploy.json").write_text('{"name": "deploy"}')
    monkeypatch.setattr(manager, "_app_display_name", lambda name: "PR Bulk Ops")
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None)
    assert (
        refusal and refusal.startswith("PR Bulk Ops can't run") and "'pr-bulk-ops'" not in refusal
    )


def test_display_name_falls_back_to_the_app_id(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "app_dir", lambda name: tmp_path / name)
    assert manager._app_display_name("pr-bulk-ops") == "pr-bulk-ops"


def test_display_name_is_read_from_the_manifest(tmp_path, monkeypatch):
    (tmp_path / "pr-bulk-ops").mkdir()
    (tmp_path / "pr-bulk-ops" / "app.json").write_text('{"displayName": "PR Bulk Ops"}')
    monkeypatch.setattr(manager, "app_dir", lambda name: tmp_path / name)
    assert manager._app_display_name("pr-bulk-ops") == "PR Bulk Ops"


def test_display_name_refuses_a_manifest_symlinked_to_a_sensitive_file(tmp_path, monkeypatch):
    from kiro_crew import agent_discovery

    secret = tmp_path / "credentials"
    secret.write_text('{"displayName": "LEAKED"}')
    (tmp_path / "pr-bulk-ops").mkdir()
    (tmp_path / "pr-bulk-ops" / "app.json").symlink_to(secret)
    monkeypatch.setattr(manager, "app_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(agent_discovery, "_fence_refuses", lambda real: real == secret.resolve())
    denials = []
    monkeypatch.setattr(agent_discovery, "_audit_denied", lambda **kw: denials.append(kw))
    assert manager._app_display_name("pr-bulk-ops") == "pr-bulk-ops"
    assert [d["operation"] for d in denials] == ["app_command_agent_label"]


def test_an_unregistered_app_agent_is_refused(tmp_path, monkeypatch):
    _agents(tmp_path, monkeypatch)
    refusal = manager.app_command_agent_refusal("other-app", "deploy", None)
    assert refusal and "isn't installed" in refusal


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [True, False])
async def test_the_turn_is_refused_before_the_prompt_reaches_the_provider(monkeypatch, refused):
    agent = KiroCrewConfig.load().default_agent
    seen = []

    def refusal(owner, name, *projects):
        seen.append((owner, name))
        return "replaced by a project agent" if refused else None

    monkeypatch.setattr(manager, "app_command_agent_refusal", refusal)

    def bind(ctx):
        ctx.slot.agent = agent
        ctx.slot.app_agent_owner = f"pr-bulk-ops/{agent}"

    record = await run_turn(TurnScript(message="deploy it", setup=bind), slot=SlotSpec())
    assert seen == [("pr-bulk-ops", agent)]
    denials = [
        call.kwargs
        for call in record.audit_events
        if call.name == "log_api_access"
        and call.kwargs.get("operation") == "chat_runner.app_command_agent"
    ]
    assert len(denials) == (1 if refused else 0)
    if refused:
        assert denials[0]["outcome"] == "denied"
    errors = [row for row in record.window if "replaced by a project agent" in str(row)]
    if refused:
        assert errors
        # Tagged so the chat withholds Resume: a retry re-runs the same refusal.
        assert any("app_agent_refused" in str(row) for row in errors)
        assert not record.provider_calls
    else:
        assert not errors
        assert record.provider_calls


@pytest.mark.asyncio
async def test_a_turn_on_another_agent_does_not_consult_the_binding(monkeypatch):
    # The binding names the agent it was stamped for, so switching the session to a
    # different agent leaves it inert rather than refusing the user's own pick.
    monkeypatch.setattr(manager, "app_command_agent_refusal", lambda *a: pytest.fail("consulted"))

    def bind(ctx):
        ctx.slot.app_agent_owner = "pr-bulk-ops/deploy"

    await run_turn(TurnScript(setup=bind), slot=SlotSpec())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"agent": "deploy", "app_agent_owner": "pr-bulk-ops"},
        {"agent": "deploy", "agent_kind": "member", "app_agent_owner": "pr-bulk-ops"},
        {"agent_kind": "template", "app_agent_owner": "pr-bulk-ops"},
        {"agent": "deploy", "agent_kind": "template", "app_agent_owner": "Not An App"},
        {"agent": "deploy", "agent_kind": "template", "app_agent_owner": 7},
    ],
)
async def test_create_refuses_a_malformed_owner(tmp_path, monkeypatch, body):
    state = _turn_state(tmp_path, monkeypatch)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json={"name": "bad-owner", **body})
        assert response.status == 400, await response.text()
        assert (await response.json())["code"] == "invalid_app_agent_owner"
    assert "bad-owner" not in state._slots


@pytest.mark.asyncio
async def test_create_stamps_the_owner_on_a_template_pick(tmp_path, monkeypatch):
    from test_chat_agent_selection import TEMPLATE, _template_chat

    state, _slot, _store = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots",
            json={
                "name": "owned",
                "agent": TEMPLATE,
                "agent_kind": "template",
                "app_agent_owner": "pr-bulk-ops",
            },
        )
        assert response.status == 200, await response.text()
    assert state._slots["owned"].app_agent_owner == f"pr-bulk-ops/{TEMPLATE}"


@pytest.mark.asyncio
async def test_a_fork_keeps_the_app_agent_binding(tmp_path, monkeypatch):
    # The fork runs as the same app agent, so it must carry the binding the
    # per-turn shadow check keys on.
    from test_chat_fork_error_codes import _fork, _seeded_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path)
    parent = state._slots["forkable"]
    parent.agent = "deploy"
    parent.app_agent_owner = "pr-bulk-ops/deploy"
    status, body = await _fork(state, "forkable", {})
    assert status == 200
    assert state._slots[body["key"]].app_agent_owner == "pr-bulk-ops/deploy"


@pytest.mark.asyncio
async def test_create_refuses_an_owner_on_an_existing_slot(tmp_path, monkeypatch):
    # Re-posting an existing key skips the stamping branch, so the owner would be
    # dropped silently; the request is refused rather than left unbound.
    from test_chat_agent_selection import TEMPLATE, _template_chat

    state, _slot, _store = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    app = _make_app_with_agent_routes(state)
    body = {"agent": TEMPLATE, "agent_kind": "template"}
    async with TestClient(TestServer(as_owner(app))) as client:
        first = await client.post("/api/chat/slots", json={"name": "taken", **body})
        assert first.status == 200, await first.text()
        again = await client.post(
            "/api/chat/slots",
            json={"name": "taken", **body, "app_agent_owner": "pr-bulk-ops"},
        )
        assert again.status == 400, await again.text()
        assert (await again.json())["code"] == "invalid_app_agent_owner"
    assert not state._slots["taken"].app_agent_owner


def test_an_app_owned_slot_is_never_eagerly_started(monkeypatch):
    # A speculative start would resolve the agent before the per-turn check runs.
    from types import SimpleNamespace

    from kiro_crew.config import live
    from kiro_crew.dashboard import chat_runner

    enabled = SimpleNamespace(session=SimpleNamespace(eager_spawn=True))
    monkeypatch.setattr(live, "snapshot", lambda: enabled)
    monkeypatch.setattr(chat_runner, "_eager_spawn_held_off", lambda _slot: False)
    scheduled = []
    monkeypatch.setattr(chat_runner.asyncio, "create_task", lambda coro: scheduled.append(coro))
    slot = SimpleNamespace(key="owned", app_agent_owner="pr-bulk-ops/deploy")
    assert chat_runner.schedule_eager_spawn(SimpleNamespace(), slot) is None
    assert not scheduled


@pytest.mark.asyncio
async def test_a_refusal_retires_a_session_registered_under_the_key():
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_runner

    removed = []

    class _Sessions:
        def has_session(self, key):
            return key == "owned"

        async def remove(self, key):
            removed.append(key)

    pending = asyncio.ensure_future(asyncio.sleep(60))
    slot = SimpleNamespace(_eager_spawn_task=pending)
    state = SimpleNamespace(sessions=_Sessions())
    await chat_runner._retire_app_agent_session(state, slot, "owned")
    assert pending.cancelled()
    assert removed == ["owned"]


def test_a_project_spec_claiming_the_name_by_filename_is_refused(tmp_path, monkeypatch):
    # The resolver matches a project spec by filename stem too, so ``deploy.json``
    # declaring another name still answers to ``deploy``.
    _agents(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / "deploy.json").write_text(
        '{"name": "zzz", "allowedTools": ["*"]}'
    )
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", str(project))
    assert refusal and 'this project has its own agent named "deploy"' in refusal


def test_without_a_project_every_fallback_folder_is_checked(tmp_path, monkeypatch):
    # A cleared project does not mean "no project": the provider can still start in
    # the session's stored cwd, so a shadow there refuses the turn.
    _agents(tmp_path, monkeypatch)
    stored = tmp_path / "old-proj"
    (stored / ".kiro" / "agents").mkdir(parents=True)
    (stored / ".kiro" / "agents" / "deploy.json").write_text('{"name": "deploy"}')
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None, str(stored), None)
    assert refusal and f"this session's folder {stored}" in refusal
    assert manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None, None) is None


def test_start_dirs_cover_the_stored_cwd_pool_and_work_dir(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_runner

    session_map = SimpleNamespace(get_cwd=lambda key: "/stored" if key == "k" else "")
    state = SimpleNamespace(sessions=SimpleNamespace(_session_map=session_map, _pool_cwd="/pool"))
    monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path))
    dirs = chat_runner._app_agent_start_dirs(state, "k", None)
    assert dirs[0] is None and "/stored" in dirs and "/pool" in dirs
    assert any(d and d.startswith(str(tmp_path.resolve())) for d in dirs[3:])
    assert chat_runner._app_agent_start_dirs(state, "k", "/proj") == ("/proj",)
