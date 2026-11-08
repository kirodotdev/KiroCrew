# SPDX-License-Identifier: Apache-2.0
"""The roster facts that decide which crewmates the Crewmates page lists unasked.

``dashboard_created`` marks a crewmate the PERSON created: the Crewmates page's
create and an approved ``crewmate.create`` card both reach ``POST /api/agents``
with ``source: kirocrew`` and get a member id, so the page lists the row even
while its chat is empty. A row the product generated (a sync row, a legacy
binding, an app's own crewmate) does not carry it. The reserved ``default``
member never does; it ships ``has_user_dm_message`` instead, true only once its
thread holds a message the person sent.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import change_card_catalog as catalog
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.crew_log.projection_workers import _RELAY_PREFIX
from kiro_crew.history import ARCHIVE_DIR_NAME, ARCHIVE_SEGMENT_DELIMITER, _safe_key
from kiro_crew.memory_stores import persist_member_config, provision_member_memory
from kiro_crew.transcript_person_rows import is_person_sent_row, transcript_has_person_message


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _app(state) -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_agents_create
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "dashboard")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_get("/api/members", api_members)
    return app


def _card_create_body(name: str) -> dict:
    """The ``POST /api/agents`` body an approved ``crewmate.create`` card sends."""
    params = catalog.validate_params("crewmate.create", {"name": name, "goal": "watch PRs"})
    preview = catalog.build_preview("crewmate.create", params, {"exists": False}, {})
    first = preview["apply"][0]
    assert (first["method"], first["path"]) == ("POST", "/api/agents")
    return first["body"]


def _seed_generated_rows() -> None:
    """Rows the product made on its own: a sync row, a legacy row, an app's crewmate."""
    cfg = KiroCrewConfig.load()
    cfg.agents["pkg-tool"] = KiroCrewAgentConfig(kiro_agent="pkg-tool", source="package")
    cfg.agents["legacy-row"] = KiroCrewAgentConfig(kiro_agent="kirocrew", source="kirocrew")
    cfg.agents["kirocrew-conductor"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew-conductor", source="builtin"
    )
    cfg.save()
    # An app's own crewmate gets a member id like a created one, under its stamp.
    cfg = KiroCrewConfig.load()
    cfg.agents["app-bot"] = KiroCrewAgentConfig(kiro_agent="kirocrew", source="radar-app")
    provision_member_memory(cfg, "app-bot")
    persist_member_config(cfg, "app-bot", create=True)


async def _roster(state) -> dict[str, dict]:
    async with TestClient(TestServer(_app(state))) as client:
        body = await (await client.get("/api/members")).json()
    return {r["name"]: r for r in body["members"]}


@pytest.mark.asyncio
async def test_a_crewmate_made_by_an_approved_card_is_marked_created_by_the_person(tmp_path):
    state = _make_state(tmp_path)
    _seed_generated_rows()
    body = _card_create_body("PR Watcher")
    assert body["source"] == "kirocrew"
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/agents", json=body)
        assert resp.status == 200, await resp.text()
        created = (await resp.json())["name"]
    rows = await _roster(state)
    # Its chat was never opened, so nothing is in it; the person made it anyway.
    assert rows[created]["dashboard_created"] is True
    assert rows[created]["has_dm_message"] is False
    for generated in ("pkg-tool", "legacy-row", "kirocrew-conductor", "app-bot"):
        assert rows[generated]["dashboard_created"] is False, generated
    # Only the reserved default row carries the stricter fact.
    assert all("has_user_dm_message" not in r for n, r in rows.items() if n != "default")


def _bind_default_thread(state, rows: list[tuple[str, str]]) -> str:
    cfg = KiroCrewConfig.load()
    slug = members_mod.member_slug("default", cfg)
    members_mod.write_dm_binding(slug, member="default", slot_key=f"member-{slug}")
    key = members_mod.member_thread_session_alias(slug)
    for role, text in rows:
        state.conversation_log.append(key, role, text)
    return key


@pytest.mark.asyncio
async def test_default_is_never_marked_created_even_when_it_looks_like_one(tmp_path):
    state = _make_state(tmp_path)
    cfg = KiroCrewConfig.load()
    cfg.agents["default"] = KiroCrewAgentConfig(kiro_agent="kirocrew", source="kirocrew")
    cfg.agents["default"].member_id = "m-looks-created"
    cfg.save()
    rows = await _roster(state)
    assert rows["default"]["dashboard_created"] is False
    assert rows["default"]["has_user_dm_message"] is False


@pytest.mark.asyncio
async def test_gateway_rows_in_the_default_thread_are_not_a_person_message(tmp_path):
    state = _make_state(tmp_path)
    _bind_default_thread(
        state,
        [
            ("assistant", "Hi, I am your default crew."),
            ("inject", "cron result: nightly digest"),
            ("nudge", "patrol cycle"),
            ("user", "[sent by session chat-1 via session_send]\n\nplease check"),
        ],
    )
    rows = await _roster(state)
    assert rows["default"]["has_dm_message"] is True
    assert rows["default"]["has_user_dm_message"] is False


@pytest.mark.asyncio
async def test_a_message_the_person_sent_lists_default(tmp_path):
    state = _make_state(tmp_path)
    _bind_default_thread(state, [("assistant", "hello"), ("user", "what is on today?")])
    rows = await _roster(state)
    assert rows["default"]["has_user_dm_message"] is True


@pytest.mark.asyncio
async def test_a_live_unflushed_person_row_lists_default(tmp_path):
    state = _make_state(tmp_path)
    _bind_default_thread(state, [("assistant", "hello")])
    cfg = KiroCrewConfig.load()
    slot_key = f"member-{members_mod.member_slug('default', cfg)}"
    from types import SimpleNamespace

    state._slots[slot_key] = SimpleNamespace(
        messages=[{"role": "assistant"}, {"role": "user", "content": "hi"}], running=False
    )
    try:
        rows = await _roster(state)
    finally:
        state._slots.pop(slot_key, None)
    assert rows["default"]["has_user_dm_message"] is True


def test_a_person_message_rotated_into_the_archive_still_counts(tmp_path):
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:member-default"
    log.append(key, "assistant", "a recent reply")
    assert transcript_has_person_message(log, key) is False
    archive = log._dir / ARCHIVE_DIR_NAME
    archive.mkdir(parents=True, exist_ok=True)
    segment = archive / f"{_safe_key(key)}{ARCHIVE_SEGMENT_DELIMITER}20260101-000000.jsonl"
    segment.write_text(
        json.dumps({"_type": "archive"}) + "\n" + json.dumps({"role": "user", "content": "hi"})
    )
    assert transcript_has_person_message(log, key) is True
    # Another thread's archive is not this one's.
    assert transcript_has_person_message(log, "dashboard:member-other") is False


def test_the_relay_prefix_is_the_one_session_send_writes():
    from kiro_crew.dashboard.session_control_parts import session_messaging

    stamped = session_messaging._SEND_PROVENANCE.format(caller="chat-1", via="session_send")
    assert stamped.startswith(_RELAY_PREFIX)
    assert is_person_sent_row({"role": "user", "content": stamped + "\n\nhi"}) is False


def test_a_user_row_automation_wrote_is_not_person_sent():
    """The roster reads the same automation rule the session summary does."""
    assert is_person_sent_row({"role": "user", "content": "[cron notification] daily"}) is False
    assert is_person_sent_row({"role": "user", "content": "[auto-nudge cycle 3]"}) is False
    assert is_person_sent_row({"role": "user", "content": "hello"}) is True
    assert is_person_sent_row({"role": "assistant", "content": "hello"}) is False
