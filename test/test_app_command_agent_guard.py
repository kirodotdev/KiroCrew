"""A session a palette command opened for its app's OWN agent runs only as that agent.

The provider resolves the bare agent name project-first, so a same-named spec in the
session's project (or a second user-level spec) would run the app-authored prompt
with that other spec's tools. The turn is refused before the prompt is sent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes
from dashboard_owner_helpers import as_owner
from test_chat_agent_selection import _turn_state
from turn_harness import ScriptedProvider, SlotSpec, TurnScript, run_turn

from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.apps import bridges, manager
from kiro_crew.apps.agent_launch import app_agent_launch_name
from kiro_crew.config.loader import KiroCrewConfig


class _KiroProvider(ScriptedProvider):
    """A scripted provider whose capabilities are kiro-cli's.

    kiro-cli is asked for what an app-owned allocation stamps: the app's launch
    copy (``apps.agent_launch``).
    """

    capabilities = capabilities_for(ACP_BACKEND_KIRO)

    @property
    def app_launch_agent(self) -> str:
        owner_app, _, owner_agent = (self._ctx.slot.app_agent_owner or "").partition("/")
        return app_agent_launch_name(owner_app, owner_agent) if owner_app else ""


def _backend_config(provider):
    """The configured backend a turn on *provider* is selected for."""
    return {"agent": {"acp_backend": "kiro" if provider is _KiroProvider else "kas"}}


class _KasProvider(ScriptedProvider):
    """A scripted provider whose capabilities are KAS's."""

    capabilities = capabilities_for(ACP_BACKEND_KAS)


class _CodexProvider(ScriptedProvider):
    """A scripted provider whose capabilities are codex's."""

    capabilities = capabilities_for(ACP_BACKEND_CODEX)


def _agents(tmp_path, monkeypatch):
    agents_dir = tmp_path / "kiro-agents"
    agents_dir.mkdir()
    (agents_dir / "pr-bulk-ops--deploy.json").write_text('{"name": "deploy"}')
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents_dir)
    return agents_dir


@pytest.mark.parametrize("refuse_user_dir", [False, True])
def test_refused_directory_is_not_resolved_or_exempted(tmp_path, monkeypatch, refuse_user_dir):
    from kiro_crew import agent_sdk, hooks
    from kiro_crew.config.paths import project_agents_dir

    agents_dir = _agents(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    project_agents = project_agents_dir(project)
    refused = {project_agents}
    if refuse_user_dir:
        refused.add(agents_dir)
    validate = hooks.validate_file_path
    resolve = Path.resolve
    resolved = []

    def screen(raw):
        return None if Path(raw) in refused else validate(raw)

    def record(path, *args, **kwargs):
        if path in refused:
            resolved.append(path)
        return resolve(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(hooks, "validate_file_path", screen)
        patch.setattr(Path, "resolve", record)
        key = manager._same_dir_key(project_agents)
        claim = agent_sdk.project_claims_agent_name("deploy", str(project))
        refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", str(project))
    assert resolved == []
    assert key is None
    assert claim is not None and claim.kind == "unverifiable"
    assert refusal is not None and refusal.endswith(claim.detail)


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


@pytest.mark.parametrize("declared_name", ["deploy", "other"])
def test_a_nested_project_spec_claiming_the_name_by_filename_is_refused(
    tmp_path, monkeypatch, declared_name
):
    _agents(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    nested = project / ".kiro" / "agents" / "team"
    nested.mkdir(parents=True)
    (nested / "deploy.json").write_text(f'{{"name": "{declared_name}", "allowedTools": ["*"]}}')
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", str(project))
    assert refusal and 'this project has its own agent named "deploy"' in refusal


@pytest.mark.parametrize("linked", [False, True])
def test_a_nested_project_spec_declaring_the_name_is_refused(tmp_path, monkeypatch, linked):
    _agents(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    nested = tmp_path / "team" if linked else agents / "team"
    nested.mkdir()
    if linked:
        (agents / "team").symlink_to(nested, target_is_directory=True)
    (nested / "x.json").write_text('{"name": "deploy", "allowedTools": ["*"]}')
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", str(project))
    assert refusal and 'this project has its own agent named "deploy"' in refusal
    fallback = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None, str(project))
    assert fallback and f"this session's folder {project}" in fallback


def test_the_home_directory_as_project_is_not_a_shadow(tmp_path, monkeypatch):
    # With the home directory as the project, ``<project>/.kiro/agents`` IS the
    # user-level agents dir that holds the app's own registered spec. That is
    # the registration check, not a shadow: the turn must run.
    home = tmp_path / "home"
    agents_dir = home / ".kiro" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "pr-bulk-ops--deploy.json").write_text('{"name": "deploy"}')
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents_dir)
    assert manager.app_command_agent_refusal("pr-bulk-ops", "deploy", str(home)) is None
    # The same folder reached through a symlinked project path is still not a shadow.
    link = tmp_path / "home-link"
    link.symlink_to(home, target_is_directory=True)
    assert manager.app_command_agent_refusal("pr-bulk-ops", "deploy", str(link)) is None


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


def test_a_hardlinked_user_level_competitor_is_unverifiable(tmp_path, monkeypatch):
    agents_dir = _agents(tmp_path, monkeypatch)
    external = tmp_path / "external.json"
    external.write_text('{"name": "deploy", "allowedTools": ["*"]}')
    os.link(external, agents_dir / "host.json")

    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None)
    assert refusal is not None
    assert "could not be verified" in refusal
    assert "host.json" in refusal
    assert manager._app_registered_agent("pr-bulk-ops", "deploy") is False


@pytest.mark.skipif(os.name != "posix", reason="creates a symlink")
def test_a_registered_spec_linked_to_a_host_spec_keeps_the_host_as_a_competitor(
    tmp_path, monkeypatch
):
    # The app's own entry is skipped by its PATH, not by what it resolves to: a
    # registered file replaced by a link to a host spec of the same name must not
    # also hide that host spec, or the prompt runs with the host spec's grants.
    agents_dir = _agents(tmp_path, monkeypatch)
    host = agents_dir / "deploy.json"
    host.write_text('{"name": "deploy", "allowedTools": ["*"]}')
    registered = agents_dir / "pr-bulk-ops--deploy.json"
    registered.unlink()
    registered.symlink_to(host)

    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None)
    assert refusal is not None and 'also named "deploy"' in refusal
    assert "deploy.json" in refusal
    assert manager._app_registered_agent("pr-bulk-ops", "deploy") is False


def test_a_refused_registered_spec_link_is_reported_not_followed(tmp_path, monkeypatch):
    # The screen runs BEFORE the hardened reader: the reader's resolve() would
    # follow the link to wherever it points (a UNC share is probed by the resolve
    # itself on Windows), so a link the screen refuses is reported and never read.
    from kiro_crew import agent_discovery, hooks

    agents_dir = _agents(tmp_path, monkeypatch)
    registered = agents_dir / "pr-bulk-ops--deploy.json"
    real_screen = hooks.validate_file_path

    def screen(raw):
        if Path(raw) == registered:
            return None
        return real_screen(raw)

    def reader(*a, **kw):
        raise AssertionError("the registered spec was resolved before the screen")

    monkeypatch.setattr(hooks, "validate_file_path", screen)
    monkeypatch.setattr(agent_discovery, "_read_agent_spec", reader)

    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None)
    assert refusal is not None and "could not be verified" in refusal
    assert registered.name in refusal
    assert manager._app_registered_agent("pr-bulk-ops", "deploy") is False


def test_a_refused_manifest_link_falls_back_to_the_app_id(tmp_path, monkeypatch):
    from kiro_crew import agent_discovery, hooks

    _agents(tmp_path, monkeypatch)

    def reader(*a, **kw):
        raise AssertionError("app.json was resolved before the screen")

    monkeypatch.setattr(hooks, "validate_file_path", lambda raw: None)
    monkeypatch.setattr(agent_discovery, "_read_agent_spec", reader)
    assert manager._app_display_name("pr-bulk-ops") == "pr-bulk-ops"


@pytest.mark.parametrize("nested", [False, True])
def test_a_differently_named_user_level_competitor_is_refused(tmp_path, monkeypatch, nested):
    agents_dir = _agents(tmp_path, monkeypatch)
    competitor = agents_dir / "team" / "x.json" if nested else agents_dir / "host.json"
    competitor.parent.mkdir(exist_ok=True)
    competitor.write_text('{"name": "deploy"}')

    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None)
    assert refusal and 'also named "deploy"' in refusal
    assert f"Rename or remove {competitor.name}" in refusal


def test_an_unlistable_user_level_directory_is_unverifiable(tmp_path, monkeypatch):
    from kiro_crew.acp import managed_agent_shadow

    agents_dir = _agents(tmp_path, monkeypatch)
    nested = agents_dir / "team"
    nested.mkdir()
    (nested / "x.json").write_text('{"name": "deploy"}')
    scandir = os.scandir

    def unreadable(path):
        if path == nested:
            raise PermissionError("cannot list team")
        return scandir(path)

    monkeypatch.setattr(managed_agent_shadow.os, "scandir", unreadable)
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None)
    assert refusal and "could not be verified" in refusal
    assert str(nested) in refusal


@pytest.mark.parametrize("limit", ["_MAX_SPEC_FILES", "_MAX_SPEC_DIRS"])
def test_user_level_scan_limits_are_unverifiable(tmp_path, monkeypatch, limit):
    from kiro_crew.acp import managed_agent_shadow

    agents_dir = _agents(tmp_path, monkeypatch)
    nested = agents_dir / "team"
    nested.mkdir()
    (nested / "x.json").write_text('{"name": "unrelated"}')
    monkeypatch.setattr(managed_agent_shadow, limit, 1)
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", None)
    assert refusal and "too many to verify" in refusal
    assert "also named" not in refusal


@pytest.mark.parametrize("fallback", [False, True])
def test_an_unverifiable_project_spec_reports_the_real_reason(tmp_path, monkeypatch, fallback):
    from kiro_crew.agent_sdk import project_claims_agent_name

    _agents(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    external = tmp_path / "external.json"
    external.write_text('{"name": "unrelated"}')
    os.link(external, agents / "host.json")

    projects = (None, str(project)) if fallback else (str(project),)
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", "deploy", *projects)
    claim = project_claims_agent_name("deploy", project)
    assert claim is not None and claim.kind == "unverifiable"
    assert refusal == f"pr-bulk-ops can't run here: {claim.detail}"
    assert "could not be read" in refusal
    assert "has its own agent named" not in refusal


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
@pytest.mark.parametrize("provider", [_KiroProvider, _KasProvider], ids=["kiro", "kas"])
@pytest.mark.parametrize("refused", [True, False])
async def test_the_turn_is_refused_before_the_prompt_reaches_the_provider(
    tmp_path, monkeypatch, refused, provider
):
    agent = KiroCrewConfig.load().default_agent
    # The app's registered spec, which a kiro-cli turn launches a copy of.
    agents_dir = tmp_path / "kiro-agents"
    agents_dir.mkdir()
    (agents_dir / f"pr-bulk-ops--{agent}.json").write_text(json.dumps({"name": agent}))
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents_dir)
    seen = []

    def refusal(owner, name, *projects):
        seen.append((owner, name))
        return "replaced by a project agent" if refused else None

    monkeypatch.setattr(manager, "app_command_agent_refusal", refusal)

    def bind(ctx):
        ctx.slot.agent = agent
        ctx.slot.app_agent_owner = f"pr-bulk-ops/{agent}"

    record = await run_turn(
        TurnScript(message="deploy it", setup=bind, provider=provider),
        slot=SlotSpec(),
        config=_backend_config(provider),
    )
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


def _bind_to_default_agent(ctx):
    agent = KiroCrewConfig.load().default_agent
    ctx.slot.agent = agent
    ctx.slot.app_agent_owner = f"pr-bulk-ops/{agent}"


def _refused_rows(record):
    return [row for row in record.window if "app_agent_refused" in str(row)]


def _sessions(record, name):
    return [call for call in record.session_calls if call.name == name]


def _denials(record):
    return [
        call.kwargs.get("outcome")
        for call in record.audit_events
        if call.name == "log_api_access"
        and call.kwargs.get("operation") == "chat_runner.app_command_agent"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["codex", "claude", "opencode"])
async def test_an_app_owned_turn_on_another_backend_is_refused_before_the_claim(
    monkeypatch, backend
):
    # Only kiro-cli and KAS launch the app's spec, so any other configured backend
    # refuses the turn before a session is allocated, and the name check that only
    # means something on those two is not consulted.
    monkeypatch.setattr(manager, "app_command_agent_refusal", lambda *a: pytest.fail("consulted"))
    record = await run_turn(
        TurnScript(message="deploy it", setup=_bind_to_default_agent, provider=_KiroProvider),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": backend}},
    )
    assert not _sessions(record, "get_or_create")
    assert not record.provider_calls
    rows = _refused_rows(record)
    assert rows and "only on the KAS or Kiro backend" in str(rows[0])
    assert _denials(record) == ["denied"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider", [_CodexProvider, ScriptedProvider], ids=["codex", "no-capabilities"]
)
async def test_an_app_owned_turn_on_a_claimed_session_of_another_backend_is_refused_and_reset(
    monkeypatch, provider
):
    # The configured selection allows the turn, but the session the claim returned
    # does not launch from the agent spec (the selection moved, or a live session
    # predates it), or carries no capabilities and so names no backend. The claimed
    # provider is the authority: nothing is sent, and the session is reset so the
    # next turn allocates from the current selection.
    monkeypatch.setattr(manager, "app_command_agent_refusal", lambda *a: None)
    record = await run_turn(
        TurnScript(message="deploy it", setup=_bind_to_default_agent, provider=provider),
        slot=SlotSpec(),
    )
    assert _sessions(record, "get_or_create")
    assert not [call for call in record.provider_calls if call.name.startswith("stream")]
    assert _refused_rows(record)
    assert _denials(record) == ["denied"]
    assert _sessions(record, "reset")


@pytest.mark.asyncio
async def test_an_unbound_turn_runs_on_any_backend(monkeypatch):
    # The backend rule is the app binding's: a session the palette did not open for
    # an app's agent runs on a provider of any backend as before.
    record = await run_turn(TurnScript(message="hi"), slot=SlotSpec())
    assert not _refused_rows(record)
    assert record.provider_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [_KiroProvider, _KasProvider], ids=["kiro", "kas"])
async def test_on_an_allowed_backend_the_turn_runs_only_while_the_namespaced_spec_is_the_one(
    tmp_path, monkeypatch, provider
):
    # The real check, on each allowed backend: the turn runs while the app's own
    # ``<app>--<agent>.json`` is the only spec declaring the name, and is refused
    # once a second spec in ~/.kiro/agents declares it too.
    agent = KiroCrewConfig.load().default_agent
    agents_dir = tmp_path / "kiro-agents"
    agents_dir.mkdir()
    (agents_dir / f"pr-bulk-ops--{agent}.json").write_text(json.dumps({"name": agent}))
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents_dir)
    script = TurnScript(message="deploy it", setup=_bind_to_default_agent, provider=provider)
    config = _backend_config(provider)
    record = await run_turn(script, slot=SlotSpec(), config=config)
    assert not _refused_rows(record)
    assert [call for call in record.provider_calls if call.name.startswith("stream")]

    (agents_dir / "other.json").write_text(json.dumps({"name": agent}))
    record = await run_turn(script, slot=SlotSpec(), config=config)
    assert _refused_rows(record)
    assert not [call for call in record.provider_calls if call.name.startswith("stream")]


def test_kas_is_handed_the_namespaced_spec_over_a_same_named_project_spec(tmp_path):
    # KAS takes no ``--agent`` name: Crew reads the spec it sends. A name the user
    # level declares resolves to the user-level spec whatever the checkout holds, so
    # the app's ``<app>--<agent>.json`` is the spec KAS runs.
    from kiro_crew.acp.harness.kas import resolve_projected_spec

    agents_dir = tmp_path / "kiro-agents"
    agents_dir.mkdir()
    (agents_dir / "pr-bulk-ops--deploy.json").write_text(
        json.dumps({"name": "deploy", "prompt": "the app's prompt", "tools": []})
    )
    project = tmp_path / "checkout"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / "deploy.json").write_text(
        json.dumps({"name": "deploy", "prompt": "the checkout's prompt"})
    )
    spec, anchor = resolve_projected_spec(agents_dir, "deploy", project)
    assert spec["prompt"] == "the app's prompt"
    assert anchor == agents_dir


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
    # Durable at birth: no title or folder rides this create, and nothing later
    # writes the binding, so a restart would otherwise drop it.
    from kiro_crew.dashboard.chat_persistence import slot_history_key

    meta = state.conversation_log.get_metadata(slot_history_key(state._slots["owned"]))
    assert meta.get("app_agent_owner") == f"pr-bulk-ops/{TEMPLATE}"
    # And the restored slot carries it, so its turns stay checked after a restart.
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    state._slots.pop("owned")
    restored = _rehydrate_slot_from_history(state, "owned")
    assert restored is not None
    assert restored.app_agent_owner == f"pr-bulk-ops/{TEMPLATE}"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["declined", "raised"])
async def test_create_fails_when_the_owner_cannot_be_persisted(tmp_path, monkeypatch, outcome):
    from test_chat_agent_selection import TEMPLATE, _template_chat

    from kiro_crew.dashboard import chat_handlers

    state, _slot, _store = await _template_chat(tmp_path, monkeypatch, first_turn=False)

    def failing_birth_write(*_a, **_kw):
        if outcome == "raised":
            raise OSError("disk full")
        return False

    async def failing_save(*_a, **kw):
        assert kw.get("best_effort") is False
        return False

    monkeypatch.setattr(state.conversation_log, "update_metadata_if", failing_birth_write)
    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", failing_save)
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
        assert response.status == 503, await response.text()
        assert (await response.json())["code"] == "app_agent_owner_unpersisted"
    # The newborn is retracted, not left behind: a slot the caller was told did
    # not get created must not be broadcast, must not be minted twice by the
    # retry, and -- message-less, so a dirty re-save writes no line -- must not
    # come back unbound after a restart.
    assert "owned" not in state._slots


@pytest.mark.asyncio
async def test_a_create_cancelled_mid_write_retracts_the_newborn(tmp_path, monkeypatch):
    # A shutdown that cancels the handler while the binding is being written
    # must not leave a published, unbound newborn behind (the twin in
    # session_control.py catches CancelledError for the same reason).
    import asyncio

    import aiohttp
    from test_chat_agent_selection import TEMPLATE, _template_chat

    from kiro_crew.dashboard import chat_handlers

    state, _slot, _store = await _template_chat(tmp_path, monkeypatch, first_turn=False)

    async def cancelled_write(*_a, **_kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(chat_handlers, "_persist_app_agent_owner", cancelled_write)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        with pytest.raises((aiohttp.ClientError, asyncio.CancelledError)):
            await client.post(
                "/api/chat/slots",
                json={
                    "name": "owned",
                    "agent": TEMPLATE,
                    "agent_kind": "template",
                    "app_agent_owner": "pr-bulk-ops",
                },
            )
    assert "owned" not in state._slots


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
    slot = SimpleNamespace(key="owned", agent="deploy", app_agent_owner="pr-bulk-ops/deploy")
    assert chat_runner.schedule_eager_spawn(SimpleNamespace(), slot) is None
    assert not scheduled


def test_a_switched_app_owned_slot_pre_warms_again(monkeypatch):
    # The binding is bound to the agent name: while the session runs as another
    # agent neither the per-turn check nor the opt-out applies. Otherwise a
    # persisted binding would cold-start every first turn forever.
    from types import SimpleNamespace

    from kiro_crew.config import live
    from kiro_crew.dashboard import chat_runner

    enabled = SimpleNamespace(session=SimpleNamespace(eager_spawn=True))
    monkeypatch.setattr(live, "snapshot", lambda: enabled)
    monkeypatch.setattr(chat_runner, "_eager_spawn_held_off", lambda _slot: False)
    reached = []

    async def fake_eager_spawn(*_a, **_kw):
        reached.append(True)

    monkeypatch.setattr(chat_runner, "_eager_spawn", fake_eager_spawn)
    monkeypatch.setattr(chat_runner.asyncio, "create_task", lambda coro: (coro.close(), "task")[1])
    slot = SimpleNamespace(
        key="owned", agent="kirocrew", app_agent_owner="pr-bulk-ops/deploy", _eager_spawn_task=None
    )
    assert chat_runner.app_agent_binding_applies(slot) is False
    assert chat_runner.schedule_eager_spawn(SimpleNamespace(), slot) == "task"


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


def test_a_skill_view_shaped_agent_name_is_refused(tmp_path, monkeypatch):
    # The spawn maps a skill-view name to the host agent its view was built from,
    # so even a registered, unique spec under that name is not the app's to run.
    agents_dir = tmp_path / "kiro-agents"
    agents_dir.mkdir()
    view = "kirocrew-skill-view-0123456789abcdef01234567"
    (agents_dir / f"pr-bulk-ops--{view}.json").write_text(f'{{"name": "{view}"}}')
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents_dir)
    refusal = manager.app_command_agent_refusal("pr-bulk-ops", view, None)
    assert refusal and "a name Kiro Crew reserves" in refusal


def test_the_manifest_never_vouches_for_a_skill_view_name(tmp_path):
    from kiro_crew.apps.manifest import contain_command_agents

    view = "kirocrew-skill-view-0123456789abcdef01234567"
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "x.json").write_text(f'{{"name": "{view}"}}')
    data = {"contributes": {"commands": [{"id": "c", "agent": view}]}}
    contain_command_agents(data, ["agents/x.json"], tmp_path)
    assert "agent" not in data["contributes"]["commands"][0]
