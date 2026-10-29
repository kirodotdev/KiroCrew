"""A dashboard create that is refused builds and files nothing on its way out.

Main's retraction (``_retract_failed_newborn``) takes a refused newborn back out
of the slot table. These tests cover the writes it does not undo: a session
folder built on disk for the new chat, and a folder the create un-hid or
claimed. The member assignment is checked before either is built, and the
folder's un-hide and claim run only after the last step that can refuse the
create.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_folders_records import CREATED_BY_SESSION


def _config() -> KiroCrewConfig:
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig()
    cfg.agents = {
        "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "worker": KiroCrewAgentConfig(kiro_agent="kirocrew"),
    }
    cfg.default_agent = "default"
    provision_member_memory(cfg, "worker")
    return cfg


def _state(tmp_path, monkeypatch, cfg: KiroCrewConfig) -> Any:
    state = _make_state(tmp_path)
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    return state


async def _post(state: Any, body: dict) -> tuple[int, dict]:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json=body)
        return response.status, await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["member_identity", "private_store"])
async def test_a_member_refusal_builds_no_session_folder(tmp_path, monkeypatch, refusal) -> None:
    """The member identity check, or the member's private-store check, refuses
    the chat's crewmate. Both are asked before the mint, so no session folder
    is built for a chat that never opens.

    Mutation pin: move the check back after the session folder and a folder is
    built for the refused create.
    """
    import kiro_crew.execution_context as execution_context

    cfg = _config()
    root = tmp_path / "sessions"
    root.mkdir()
    cfg.dashboard.session_folder_root = str(root)
    state = _state(tmp_path, monkeypatch, cfg)
    slots_before = set(state._slots)
    built: list[str] = []
    real_build = chat_handlers.create_session_folder

    def build(raw_root: str, **kwargs: Any) -> str:
        path = real_build(raw_root, **kwargs)
        built.append(path)
        return path

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError("member store unavailable")

    from kiro_crew.dashboard import chat_persistence

    monkeypatch.setattr(chat_handlers, "create_session_folder", build)
    if refusal == "member_identity":
        monkeypatch.setattr(execution_context, "resolve_member_execution", refuse)
    else:
        monkeypatch.setattr(chat_persistence, "member_store_ownership_holds", lambda *a: False)

    status, data = await _post(state, {"agent": "worker"})

    assert status == 503, data
    assert data["code"] == "store_unavailable"
    assert built == []
    assert list(root.iterdir()) == []
    assert set(state._slots) == slots_before


@pytest.mark.asyncio
async def test_a_create_refused_after_the_mint_leaves_its_folder_hidden_and_unclaimed(
    tmp_path, monkeypatch
) -> None:
    """A create filed into a hidden, session-made folder is refused after the
    mint, when the member's private store cannot be pinned. The folder stays
    hidden and keeps its session mark, because its un-hide and claim run only
    after the create's last refusable step.

    Mutation pin: un-hide the folder where the create reads it and the refused
    create shows and claims it.
    """
    cfg = _config()
    state = _state(tmp_path, monkeypatch, cfg)
    folder = {"id": "f1", "name": "F", "hidden": True, CREATED_BY_SESSION: "chat-x"}
    state._folders.append(folder)

    pinned: list[str] = []

    async def pin(state_arg, session_key, agent, config, **kwargs):
        # The store goes away after the preflight's read-only check, before the
        # post-mint pin, which the preflight cannot see in advance.
        pinned.append(session_key)
        raise OSError("private store unavailable")

    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", pin)

    status, data = await _post(state, {"agent": "worker", "folder_id": "f1"})

    assert status == 503, data
    assert data["code"] == "store_unavailable"
    # Positive control: the create got past the mint to the pin.
    assert len(pinned) == 1
    stored = await state.read_folders(
        lambda folders: next(dict(f) for f in folders if f["id"] == "f1")
    )
    assert stored.get("hidden") is True
    assert stored.get(CREATED_BY_SESSION) == "chat-x"
