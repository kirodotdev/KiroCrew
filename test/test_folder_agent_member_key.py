"""A folder Default agent binds what it stores: a member key, or a template name.

The folder picker stores a crewmate's ``config.agents`` key for a crewmate row
and a template's name for a template row. These pin what the create route does
with each, for the shape where a crewmate keyed ``Jira ticket`` is labelled
``jira-ticket`` and a template of that name also exists: the key binds the
crewmate with its model pin, and the template name binds the template.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, resolve_agent_bindings
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_folders_records import (
    FOLDER_MEMBER_LABELS_MARKER,
    migrate_folder_member_labels,
)
from kiro_crew.dashboard.handlers import agent_catalog

MEMBER_KEY = "Jira ticket"
SPEC = "jira-ticket"
MEMBER_MODEL = "claude-sonnet-5"
#: Templates kiro-cli declares in this test's world.
TEMPLATES = {SPEC, "kirocrew"}
_EN_CATALOG = Path(__file__).parent.parent / "website/src/i18n/locales/en.manual.json"
#: Spelled out, not imported: a data home keeps this file across upgrades, so its name
#: is part of what a later build reads.
KEYS_CHECKED_MARKER = "folder_member_keys_checked"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    config = KiroCrewConfig()
    config.agents = {
        "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        MEMBER_KEY: KiroCrewAgentConfig(kiro_agent=SPEC, display_name=SPEC, model=MEMBER_MODEL),
    }
    config.default_agent = "default"

    def materialized(name, project_dir=None):
        return name if name in TEMPLATES else ""

    for target in ("kiro_crew.config.loader", "kiro_crew.config.loader_agents"):
        monkeypatch.setattr(f"{target}._materialized_kiro_agent", materialized)
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: config))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **k: None)
    default_dir = tmp_path / "default-project"
    default_dir.mkdir()
    monkeypatch.setattr(chat_handlers, "default_project_dir", lambda _ws: str(default_dir))
    return config


def _state(tmp_path, default_agent: str):
    state = _make_state(tmp_path)
    state._folders = [
        {
            "id": "jira",
            "name": "Jira Tickets",
            "order": 0,
            "parent_id": "",
            "default_agent": default_agent,
        }
    ]
    return state


async def _create(state: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        resp = await client.post("/api/chat/slots", json=payload)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_a_member_key_default_binds_the_member_and_keeps_its_model(tmp_path, cfg):
    state = _state(tmp_path, MEMBER_KEY)
    status, data = await _create(state, {"folder_id": "jira", "agent": MEMBER_KEY})
    assert status == 200, data
    slot = state._slots[data["key"]]
    assert slot.agent == MEMBER_KEY
    bindings = resolve_agent_bindings(cfg, slot.agent, validate_memory_files=False)
    assert bindings.selection_kind == "member"
    assert bindings.model == MEMBER_MODEL


@pytest.mark.asyncio
async def test_a_member_key_on_the_default_folder_binds_the_member(tmp_path, cfg):
    cfg.dashboard.default_session_folder = "jira"
    state = _state(tmp_path, MEMBER_KEY)
    status, data = await _create(state, {"default_folder": True})
    assert status == 200, data
    assert data["folder_id"] == "jira"
    assert state._slots[data["key"]].agent == MEMBER_KEY


@pytest.mark.asyncio
async def test_a_template_name_default_binds_the_template(tmp_path, cfg):
    state = _state(tmp_path, SPEC)
    status, data = await _create(state, {"folder_id": "jira", "agent": SPEC})
    assert status == 200, data
    slot = state._slots[data["key"]]
    assert slot.agent == SPEC
    bindings = resolve_agent_bindings(cfg, slot.agent, validate_memory_files=False)
    assert bindings.selection_kind == "template"
    assert bindings.model != MEMBER_MODEL


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


def _notes(monkeypatch, state: Any) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    notes: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(state, "notify", lambda *args, **kwargs: notes.append((args, kwargs)))
    return notes


@pytest.mark.asyncio
async def test_a_custom_agent_picked_under_a_crewmates_label_keeps_binding_it(
    tmp_path, cfg, monkeypatch
):
    home = _home(tmp_path, monkeypatch)
    cfg.dashboard.default_session_folder = "jira"
    # What the picker saves for the "jira-ticket · Custom agents" row.
    state = _state(tmp_path, SPEC)
    notes = _notes(monkeypatch, state)

    await migrate_folder_member_labels(state, cfg)
    await migrate_folder_member_labels(state, cfg)

    assert state._folders[0]["default_agent"] == SPEC
    status, data = await _create(state, {"default_folder": True})
    assert status == 200, data
    bindings = resolve_agent_bindings(
        cfg, state._slots[data["key"]].agent, validate_memory_files=False
    )
    assert bindings.selection_kind == "template"
    # Once per data home, one notice names the folder and what it stores.
    [(args, kwargs)] = notes
    folders = f"Jira Tickets ({SPEC})"
    assert kwargs["i18n"] == {"id": "folder_agent_label", "params": {"folders": folders}}
    copy = json.loads(_EN_CATALOG.read_text(encoding="utf-8"))["components"]["notifications"]
    assert args[1] == copy["systemNote"]["folder_agent_label_title"]
    assert args[2] == copy["systemNote"]["folder_agent_label_body"].replace("{{folders}}", folders)
    assert (home / FOLDER_MEMBER_LABELS_MARKER).exists()
    assert (home / KEYS_CHECKED_MARKER).exists()


@pytest.mark.asyncio
async def test_a_notice_that_could_not_be_sent_is_tried_again_next_start(
    tmp_path, cfg, monkeypatch
):
    home = _home(tmp_path, monkeypatch)
    state = _state(tmp_path, SPEC)

    def _refuse(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("notification bus unavailable")

    monkeypatch.setattr(state, "notify", _refuse)

    await migrate_folder_member_labels(state, cfg)

    assert not (home / FOLDER_MEMBER_LABELS_MARKER).exists()


@pytest.mark.asyncio
async def test_a_notice_that_could_not_be_stored_is_tried_again_next_start(
    tmp_path, cfg, monkeypatch
):
    home = _home(tmp_path, monkeypatch)
    state = _state(tmp_path, SPEC)

    def _unstored(*_args: Any, **_kwargs: Any) -> None:
        state.last_notification_persist = asyncio.get_running_loop().create_future()
        state.last_notification_persist.set_result(False)

    monkeypatch.setattr(state, "notify", _unstored)

    await migrate_folder_member_labels(state, cfg)

    assert not (home / FOLDER_MEMBER_LABELS_MARKER).exists()


def _share_the_label(cfg):
    cfg.agents["Jira copy"] = KiroCrewAgentConfig(kiro_agent=SPEC, display_name=SPEC)


def _key_the_label(cfg):
    cfg.agents[SPEC] = KiroCrewAgentConfig(kiro_agent=SPEC)


def _lose_the_crewmates(cfg):
    cfg._degraded_sections = frozenset({"agents"})


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [_share_the_label, _key_the_label, _lose_the_crewmates])
async def test_a_label_naming_no_single_crewmate_is_left_as_saved(
    tmp_path, cfg, monkeypatch, change
):
    _home(tmp_path, monkeypatch)
    change(cfg)
    state = _state(tmp_path, SPEC)
    notes = _notes(monkeypatch, state)

    await migrate_folder_member_labels(state, cfg)

    assert state._folders[0]["default_agent"] == SPEC
    assert notes == []


@pytest.fixture
def offered(monkeypatch):
    """The custom agents the folder picker offers, keyed by the project it lists them for."""
    rosters: dict[Path | None, set[str]] = {None: set(TEMPLATES)}
    monkeypatch.setattr(
        agent_catalog,
        "_templates",
        lambda project_dir: [SimpleNamespace(name=n) for n in rosters.get(project_dir, set())],
    )
    return rosters


def _rewritten_home(tmp_path, monkeypatch) -> Path:
    """A data home an earlier build's label rewrite ran on."""
    home = _home(tmp_path, monkeypatch)
    (home / FOLDER_MEMBER_LABELS_MARKER).touch()
    return home


@pytest.mark.asyncio
async def test_a_folder_the_rewrite_may_have_moved_is_named_once_and_kept(
    tmp_path, cfg, monkeypatch, offered
):
    home = _rewritten_home(tmp_path, monkeypatch)
    # What the rewrite left for a folder set to the "jira-ticket · Custom agents" row.
    state = _state(tmp_path, MEMBER_KEY)
    notes = _notes(monkeypatch, state)

    await migrate_folder_member_labels(state, cfg)
    await migrate_folder_member_labels(state, cfg)

    assert state._folders[0]["default_agent"] == MEMBER_KEY
    [(args, kwargs)] = notes
    folders = f"Jira Tickets ({SPEC})"
    assert kwargs["i18n"] == {"id": "folder_agent_key", "params": {"folders": folders}}
    copy = json.loads(_EN_CATALOG.read_text(encoding="utf-8"))["components"]["notifications"]
    assert args[1] == copy["systemNote"]["folder_agent_key_title"]
    assert args[2] == copy["systemNote"]["folder_agent_key_body"].replace("{{folders}}", folders)
    assert (home / KEYS_CHECKED_MARKER).exists()


@pytest.mark.asyncio
async def test_a_moved_folder_is_checked_against_its_projects_custom_agents(
    tmp_path, cfg, monkeypatch, offered
):
    _rewritten_home(tmp_path, monkeypatch)
    offered[None] = {"kirocrew"}
    project = tmp_path / "jira-project"
    offered[project] = {SPEC}
    state = _state(tmp_path, "")
    state._folders[0]["project_dir"] = str(project)
    state._folders.append(
        {
            "id": "sub",
            "name": "Sprint",
            "order": 1,
            "parent_id": "jira",
            "default_agent": MEMBER_KEY,
        }
    )
    notes = _notes(monkeypatch, state)

    await migrate_folder_member_labels(state, cfg)

    [(_args, kwargs)] = notes
    assert kwargs["i18n"]["params"] == {"folders": f"Sprint ({SPEC})"}


def _fresh(tmp_path, monkeypatch) -> Path:
    return _home(tmp_path, monkeypatch)


def _checked(tmp_path, monkeypatch) -> Path:
    home = _rewritten_home(tmp_path, monkeypatch)
    (home / KEYS_CHECKED_MARKER).touch()
    return home


def _offered_nowhere(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(agent_catalog, "_templates", lambda project_dir: [])
    return _rewritten_home(tmp_path, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("make_home", [_fresh, _checked, _offered_nowhere])
async def test_a_crewmate_key_is_named_only_where_the_rewrite_may_have_moved_it(
    tmp_path, cfg, monkeypatch, offered, make_home
):
    home = make_home(tmp_path, monkeypatch)
    state = _state(tmp_path, MEMBER_KEY)
    notes = _notes(monkeypatch, state)

    await migrate_folder_member_labels(state, cfg)

    assert state._folders[0]["default_agent"] == MEMBER_KEY
    assert notes == []
    assert (home / KEYS_CHECKED_MARKER).exists()


@pytest.mark.asyncio
async def test_a_moved_folder_notice_that_could_not_be_stored_is_tried_again_next_start(
    tmp_path, cfg, monkeypatch, offered
):
    home = _rewritten_home(tmp_path, monkeypatch)
    state = _state(tmp_path, MEMBER_KEY)

    def _unstored(*_args: Any, **_kwargs: Any) -> None:
        state.last_notification_persist = asyncio.get_running_loop().create_future()
        state.last_notification_persist.set_result(False)

    monkeypatch.setattr(state, "notify", _unstored)

    await migrate_folder_member_labels(state, cfg)

    assert not (home / KEYS_CHECKED_MARKER).exists()


@pytest.mark.asyncio
async def test_a_folder_store_that_failed_to_load_is_read_before_the_check(
    tmp_path, cfg, monkeypatch, offered
):
    home = _rewritten_home(tmp_path, monkeypatch)
    state = _state(tmp_path, MEMBER_KEY)
    (home / state._FOLDERS_FILE).write_text(json.dumps(state._folders), encoding="utf-8")
    # What a failed boot load leaves: an empty list that stands for an unknown store.
    state._folders = []
    state._folders_authoritative = False
    notes = _notes(monkeypatch, state)

    await migrate_folder_member_labels(state, cfg)

    [(_args, kwargs)] = notes
    assert kwargs["i18n"]["params"] == {"folders": f"Jira Tickets ({SPEC})"}
    assert (home / KEYS_CHECKED_MARKER).exists()
