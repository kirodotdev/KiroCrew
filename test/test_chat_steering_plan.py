"""The per-chat steering list says which steering files one chat receives.

``GET /api/chat/slots/{slot}/steering`` and its plan
(``kiro_crew.steering_plan``): the verdict per harness, the parity with the
Claude Code loader, and the route itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import steering_plan as plan
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP, PROVIDER_CLAUDE_CODE
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.handlers import chat_steering
from kiro_crew.dashboard.handlers.steering import list_steering_blocking
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

ALWAYS_RULE = "ALWAYS-RULE-5b1e"
MANUAL_RULE = "MANUAL-RULE-9c3a"


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An operator home with one ``always`` and one ``manual`` global document."""
    home = tmp_path / "home"
    _write(home / ".kiro" / "steering" / "always.md", f"# Always\n{ALWAYS_RULE}\n")
    _write(
        home / ".kiro" / "steering" / "manual.md",
        f"---\ninclusion: manual\n---\n# Manual\n{MANUAL_RULE}\n",
    )
    _write(
        home / ".kiro" / "agents" / "kirocrew.json",
        json.dumps({"name": "kirocrew", "resources": ["file://.kiro/steering/**/*.md"]}),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / ".kiro"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _verdict(**overrides):
    kwargs = dict(
        provider_type="opencode",
        native_steering=False,
        project=None,
        agent=None,
        is_member=False,
    )
    kwargs.update(overrides)
    return plan.tree_verdict(**kwargs)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, (plan.REACHES_NO, plan.REASON_HARNESS_HAS_NO_PATH)),
        (
            {"provider_type": PROVIDER_CLAUDE_CODE, "loaded": plan.LOADER_KEPT},
            (plan.REACHES_YES, ""),
        ),
        (
            {"provider_type": PROVIDER_CLAUDE_CODE, "loaded": plan.LOADER_WITHHELD},
            (plan.REACHES_NO, plan.REASON_INCLUSION_WITHHELD),
        ),
        (
            {"provider_type": PROVIDER_CLAUDE_CODE},
            (plan.REACHES_NO, plan.REASON_NOT_LOADED),
        ),
        (
            {"provider_type": PROVIDER_CLAUDE_CODE, "linked": True},
            (plan.REACHES_NO, plan.REASON_LINK_NOT_ADMITTED),
        ),
        (
            {
                "provider_type": PROVIDER_CLAUDE_CODE,
                "agent": "my-custom-agent",
                "loaded": plan.LOADER_KEPT,
            },
            (plan.REACHES_NO, plan.REASON_CUSTOM_AGENT),
        ),
        (
            {"provider_type": "codex", "loaded": plan.LOADER_KEPT},
            (plan.REACHES_YES, ""),
        ),
        (
            {"provider_type": "codex", "agent": "my-custom-agent", "loaded": plan.LOADER_KEPT},
            (plan.REACHES_NO, plan.REASON_CUSTOM_AGENT),
        ),
        (
            {"provider_type": "kas", "native_steering": True},
            (plan.REACHES_HARNESS, plan.REASON_HARNESS_NATIVE),
        ),
        (
            {"provider_type": PROVIDER_CLAUDE_CODE, "is_member": True},
            (plan.REACHES_BRIEFING, plan.REASON_MEMBER_BRIEFING),
        ),
    ],
)
def test_each_gate_gives_its_verdict(overrides, expected):
    assert _verdict(**overrides) == expected


@pytest.mark.parametrize(("inherits", "reaches"), [(True, "harness"), (False, "no")])
def test_kiro_cli_follows_the_inheritance_answer(monkeypatch, inherits, reaches):
    from kiro_crew import context as ctx

    monkeypatch.setattr(ctx.acp_driver, "inherits_default_resources", lambda _p: inherits)
    assert _verdict(provider_type=PROVIDER_ACP, declared=frozenset())[0] == reaches


def test_kiro_cli_without_inheritance_still_loads_what_the_agent_declares(
    home, tmp_path, monkeypatch
):
    """The shipped agent declares the project tree, so it loads with inheritance off."""
    from kiro_crew import context as ctx

    monkeypatch.setattr(ctx.acp_driver, "inherits_default_resources", lambda _p: False)
    project = tmp_path / "project"
    _write(project / ".kiro" / "steering" / "team.md", "# Team\n")
    rows = plan.chat_steering_plan(
        listed=list_steering_blocking(project)["files"],
        folder_documents=(),
        provider_type=PROVIDER_ACP,
        native_steering=False,
        project=str(project),
        agent="kirocrew",
        is_member=False,
    )
    by_name = {row["name"]: (row["reaches"], row["reason"]) for row in rows}
    assert by_name["team.md"] == (plan.REACHES_HARNESS, plan.REASON_AGENT_RESOURCES)
    assert by_name["always.md"] == (plan.REACHES_NO, plan.REASON_INHERITANCE_OFF)


def test_a_project_agent_spec_makes_kiro_cli_verdicts_unknown(home, tmp_path, monkeypatch):
    """kiro-cli serves the project's spec of that name, which this plan does not read."""
    from kiro_crew import context as ctx

    monkeypatch.setattr(ctx.acp_driver, "inherits_default_resources", lambda _p: False)
    project = tmp_path / "project"
    _write(project / ".kiro" / "steering" / "team.md", "# Team\n")
    _write(project / ".kiro" / "agents" / "kirocrew.json", json.dumps({"name": "kirocrew"}))
    rows = plan.chat_steering_plan(
        listed=list_steering_blocking(project)["files"],
        folder_documents=(),
        provider_type=PROVIDER_ACP,
        native_steering=False,
        project=str(project),
        agent="kirocrew",
        is_member=False,
    )
    assert {(r["reaches"], r["reason"]) for r in rows} == {
        (plan.REACHES_HARNESS, plan.REASON_AGENT_SPEC_UNREAD)
    }


def _claude_rows(project: Path | None) -> list[dict[str, str]]:
    """The rows a default-agent Claude Code chat gets, fed as the route feeds them."""
    from kiro_crew.context_assembly.agent_spec import steering_resource_documents

    key = str(project) if project else None
    return plan.chat_steering_plan(
        listed=list_steering_blocking(project)["files"],
        folder_documents=(),
        loader_documents=[
            (str(p), resolved, body is not None)
            for p, resolved, body in steering_resource_documents(key)
        ],
        provider_type=PROVIDER_CLAUDE_CODE,
        native_steering=False,
        project=key,
        agent=None,
        is_member=False,
    )


def test_claude_code_verdicts_match_what_its_loader_sends(home):
    """Parity: every ``yes`` file's body is in the load, and no other file's is."""
    from kiro_crew.context import _load_steering_resources

    loaded = _load_steering_resources(None)
    by_name = {row["name"]: row for row in _claude_rows(None)}
    assert by_name["always.md"]["reaches"] == plan.REACHES_YES
    assert by_name["manual.md"]["reaches"] == plan.REACHES_NO
    assert ALWAYS_RULE in loaded
    assert MANUAL_RULE not in loaded


def test_claude_code_lists_a_project_agents_md_it_loads(home, tmp_path):
    """The load reads the project's ``AGENTS.md`` too, outside the steering trees."""
    from kiro_crew.context import _load_steering_resources

    project = tmp_path / "project"
    _write(project / "AGENTS.md", "# Repo rules\nAGENTS-RULE-2c4d\n")
    assert "AGENTS-RULE-2c4d" in _load_steering_resources(str(project))
    row = next(r for r in _claude_rows(project) if r["name"] == "AGENTS.md")
    assert (row["source"], row["reaches"]) == ("agent", plan.REACHES_YES)


def test_claude_code_judges_a_project_link_as_its_loader_does(home, tmp_path):
    """A link the Steering tab will not read still gets the loader's own answer."""
    from kiro_crew.context import _load_steering_resources

    project = tmp_path / "project"
    steering = project / ".kiro" / "steering"
    steering.mkdir(parents=True)
    linked_rule = "LINKED-RULE-7d2f"
    linked_manual = "LINKED-MANUAL-4e8b"
    (steering / "rules.md").symlink_to(
        _write(project / "docs" / "rules.md", f"# R\n{linked_rule}\n")
    )
    (steering / "later.md").symlink_to(
        _write(project / "docs" / "later.md", f"---\ninclusion: manual\n---\n{linked_manual}\n")
    )
    loaded = _load_steering_resources(str(project))
    by_name = {row["name"]: row for row in _claude_rows(project)}
    assert linked_rule in loaded
    assert (by_name["rules.md"]["reaches"], by_name["rules.md"]["applies"]) == ("yes", "always")
    assert linked_manual not in loaded
    assert by_name["later.md"]["reaches"] == plan.REACHES_NO


def test_a_folder_document_is_listed_once_as_delivered():
    listed = [{"name": "style.md", "path": "~/.kiro/steering/style.md", "source": "user"}]
    rows = plan.chat_steering_plan(
        listed=listed,
        folder_documents=("~/.kiro/steering/style.md", "~/standards/own.md"),
        provider_type="opencode",
        native_steering=False,
        project=None,
        agent=None,
        is_member=False,
    )
    assert [(r["name"], r["source"], r["reaches"]) for r in rows] == [
        ("style.md", "global", "yes"),
        ("own.md", "folder", "yes"),
    ]


def test_a_folder_document_the_section_cap_leaves_out_is_not_included(home, tmp_path, monkeypatch):
    """The verdicts follow the bounded section the turn renders, not the collection."""
    from kiro_crew import context as ctx
    from kiro_crew.folder_steering import SteeringCollection

    monkeypatch.setattr(chat_steering, "_resolved_agent", lambda *_a: "kirocrew")
    root = tmp_path / "standards"
    cap = ctx._resolve_caps(None).steering
    # The collection is stubbed: this pins the section-cap verdicts, not the collector.
    documents = [(str(root / "a.md"), "a" * (cap + 500)), (str(root / "b.md"), "small")]
    monkeypatch.setattr(
        ctx, "collect_folder_steering", lambda *_a, **_k: SteeringCollection(documents=documents)
    )
    rows = chat_steering._plan_blocking(
        _ChatSlot("main"), "main", None, (str(root),), "codex", False
    )
    by_name = {r["name"]: (r["reaches"], r["reason"]) for r in rows if r["source"] == "folder"}
    assert by_name["a.md"] == (plan.REACHES_YES, "")
    assert by_name["b.md"] == (plan.REACHES_NO, plan.REASON_FOLDER_CAP)


def test_a_member_chat_leaves_its_folder_guides_to_the_briefing(home, tmp_path, monkeypatch):
    """The member envelope decides folder guides, so no row claims the section cap."""
    from kiro_crew import context as ctx
    from kiro_crew.folder_steering import SteeringCollection

    root = tmp_path / "standards"
    cap = ctx._resolve_caps(None).steering
    documents = [(str(root / "a.md"), "a" * (cap + 500)), (str(root / "b.md"), "small")]
    monkeypatch.setattr(
        ctx, "collect_folder_steering", lambda *_a, **_k: SteeringCollection(documents=documents)
    )
    slot = _ChatSlot("main")
    slot.mode, slot.agent = "member", "mate"
    rows = chat_steering._plan_blocking(slot, "main", None, (str(root),), "codex", False)
    assert {(r["reaches"], r["reason"]) for r in rows if r["source"] == "folder"} == {
        (plan.REACHES_BRIEFING, plan.REASON_MEMBER_BRIEFING)
    }


def test_the_folder_skips_what_the_launch_record_delivers(home, tmp_path, monkeypatch):
    """A kiro-cli chat with inheritance off skips its launch record's guides, as the turn does."""
    from kiro_crew import context as ctx
    from kiro_crew.folder_steering import SteeringCollection

    seen: dict = {}

    def collect(*_a, **kwargs):
        seen.update(kwargs)
        return SteeringCollection(documents=[])

    monkeypatch.setattr(ctx.acp_driver, "inherits_default_resources", lambda _p: False)
    monkeypatch.setattr(ctx, "collect_folder_steering", collect)
    monkeypatch.setattr(chat_steering, "_resolved_agent", lambda *_a: "kirocrew")
    launched = (str(tmp_path / "standards" / "a.md"),)
    chat_steering._plan_blocking(
        _ChatSlot("main"), "main", None, (str(tmp_path),), PROVIDER_ACP, False, launched
    )
    assert (seen["skip_delivered_roots"], tuple(seen["delivered_sources"])) == (False, launched)


def test_a_chat_with_no_project_reads_the_provider_working_directory(monkeypatch):
    """The turn builds context in the provider's cwd; the agent keeps the slot's project."""
    import kiro_crew.agent_sdk as agent_sdk

    live = SimpleNamespace(native_context_documents={"/w/guide.md": ""}, cwd="/w")
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)
    seen: dict = {}

    def plan_blocking(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return []

    monkeypatch.setattr(chat_steering, "_plan_blocking", plan_blocking)
    chat_steering._plan_for_provider(live, _ChatSlot("main"), "main", None, (), "acp", False)
    assert seen["args"][2] == "/w"
    assert seen["args"][6] == ("/w/guide.md",)
    assert seen["kwargs"] == {"binding_project": None}


def test_a_tree_file_the_folder_cap_cuts_says_so():
    listed = [{"name": "style.md", "path": "~/.kiro/steering/style.md", "source": "user"}]
    rows = plan.chat_steering_plan(
        listed=listed,
        folder_documents=(),
        folder_cut=("~/.kiro/steering/style.md",),
        provider_type="opencode",
        native_steering=False,
        project=None,
        agent=None,
        is_member=False,
    )
    assert [(r["name"], r["reaches"], r["reason"]) for r in rows] == [
        ("style.md", plan.REACHES_NO, plan.REASON_FOLDER_CAP)
    ]


def _config(monkeypatch, backend: str, kiro_agent: str = "kirocrew") -> None:
    """A cold slot on *backend* whose turns resolve to *kiro_agent*."""
    import kiro_crew.session_agent_selection as selection

    monkeypatch.setattr(
        chat_handlers.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(
            agent=SimpleNamespace(acp_backend=backend, member_acp_backend=""), agents={}
        ),
    )
    monkeypatch.setattr(
        selection,
        "resolve_session_agent_bindings",
        lambda *_a, **_k: SimpleNamespace(kiro_agent=kiro_agent),
    )


def _state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.get_provider = MagicMock(return_value=None)
    return state


async def _get(state: DashboardState, slot: str, app_name: str = "") -> tuple[int, dict]:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def as_app(request, handler):
        if app_name:
            request["app"] = app_name
        return await handler(request)

    app.middlewares.append(as_app)
    app.router.add_get("/api/chat/slots/{slot}/steering", chat_steering.api_chat_slot_steering)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/chat/slots/{slot}/steering")
        return resp.status, await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "reaches", "reason"),
    [("codex", "yes", ""), ("opencode", "no", "harness_has_no_path")],
)
async def test_a_cold_chat_lists_global_steering_as_its_harness_receives_it(
    home, monkeypatch, backend, reaches, reason
):
    """The route answers for a cold chat on the harness it would start on: Crew's
    steering load reaches Codex, and OpenCode has no path for the trees."""
    _config(monkeypatch, backend)
    status, data = await _get(_state(_ChatSlot("main")), "main")
    assert status == 200
    assert data["provider"] == backend
    row = next(r for r in data["files"] if r["name"] == "always.md")
    # The display path uses the host's own separator.
    assert Path(row.pop("path")) == Path("~/.kiro/steering/always.md")
    assert row == {
        "name": "always.md",
        "source": "global",
        "applies": "always",
        "reaches": reaches,
        "reason": reason,
    }


@pytest.mark.asyncio
async def test_a_codex_chat_does_not_list_the_project_agents_md(home, tmp_path, monkeypatch):
    """Codex reads the project's ``AGENTS.md`` itself, so Crew's load and the list
    leave it out; on Claude Code the load sends it and the list names it."""
    project = tmp_path / "project"
    _write(project / "AGENTS.md", "# Agents\n")
    for backend, listed in (("codex", False), ("claude", True)):
        _config(monkeypatch, backend)
        slot = _ChatSlot("main")
        slot.project = str(project)
        status, data = await _get(_state(slot), "main")
        assert status == 200
        names = {r["name"] for r in data["files"] if r["source"] == "agent"}
        assert ("AGENTS.md" in names) is listed, backend


@pytest.mark.asyncio
async def test_the_claude_gate_judges_the_template_the_turn_resolves(home, monkeypatch):
    """An empty slot pick runs on the configured default template, as the turn does."""
    _config(monkeypatch, "claude", kiro_agent="my-custom-agent")
    status, data = await _get(_state(_ChatSlot("main")), "main")
    assert status == 200
    row = next(r for r in data["files"] if r["name"] == "always.md")
    assert (row["reaches"], row["reason"]) == ("no", "custom_agent")


@pytest.mark.asyncio
async def test_a_channel_linked_chat_lists_its_own_project(home, tmp_path, monkeypatch):
    """Its session key (``slack:<ts>``) names no slot, so the project comes from the slot."""
    _config(monkeypatch, "codex")
    project = tmp_path / "project"
    _write(project / ".kiro" / "steering" / "team.md", "# Team\n")
    slot = _ChatSlot("slack_1700000000.000100")
    slot.linked_session_key = "slack:1700000000.000100"
    slot.project = str(project)
    status, data = await _get(_state(slot), slot.key)
    assert status == 200
    assert ("team.md", "project") in {(r["name"], r["source"]) for r in data["files"]}


@pytest.mark.asyncio
async def test_an_app_cannot_read_a_slot_it_does_not_own(home):
    status, _data = await _get(_state(_ChatSlot("main")), "main", app_name="some-app")
    assert status == 404
