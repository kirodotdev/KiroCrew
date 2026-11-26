"""``session_key`` on ``POST /api/crons``: a job that also posts into a chosen chat.

The create route accepts the chat, refuses one that is unknown, in another
namespace, in another member's memory or named by a hidden job, and the
delivery leg posts the result row into the chat the job stored.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard import cron_inject
from kiro_crew.dashboard.handlers import api_crons_create
from kiro_crew.validation import MAX_SHORT_STRING


def _request(body: dict, crons: CronService, slots: dict) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    state.get_slot = lambda name: slots.get(name)
    request = MagicMock()
    request.app = {"state": state}
    attach_body(request, body)
    return request


def _body(**extra) -> dict:
    return {"name": "later", "message": "say hi", "delay": 3600, **extra}


@pytest.fixture()
def session_member(monkeypatch):
    """The member each session key's execution record names (absent: none)."""
    members: dict[str, str | None] = {}

    def _read(key, **_kw):
        if key not in members:
            return None
        return SimpleNamespace(member_id=members[key])

    monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", _read)
    return members


def _chat(linked: str = "", app: str = "") -> SimpleNamespace:
    return SimpleNamespace(linked_session_key=linked, messages=[], _app=app)


async def _create(tmp_path, body, slots):
    crons = CronService(base_dir=tmp_path)
    resp = await api_crons_create(_request(body, crons, slots))
    return resp, json.loads(resp.text), crons.list_jobs(include_disabled=True)


@pytest.mark.asyncio
async def test_named_open_chat_is_stored_as_the_target(tmp_path, session_member):
    resp, data, jobs = await _create(tmp_path, _body(session_key="chat-1"), {"chat-1": _chat()})
    assert resp.status == 200, data
    assert [j.target_session_key for j in jobs] == ["dashboard:chat-1"]
    # The job keeps its own identity: the target is delivery only.
    assert jobs[0].session_key == ""
    # And the target survives a reload of the store.
    reloaded = CronService(base_dir=tmp_path).list_jobs(include_disabled=True)
    assert [j.target_session_key for j in reloaded] == ["dashboard:chat-1"]


@pytest.mark.asyncio
async def test_namespaced_key_is_accepted_as_given(tmp_path, session_member):
    slots = {"chat-1": _chat("dashboard:chat-1")}
    resp, data, jobs = await _create(tmp_path, _body(session_key="dashboard:chat-1"), slots)
    assert resp.status == 200, data
    assert jobs[0].target_session_key == "dashboard:chat-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [_body(), _body(session_key=""), _body(session_key=None)])
async def test_no_session_key_stores_no_target(tmp_path, session_member, body):
    resp, data, jobs = await _create(tmp_path, body, {})
    assert resp.status == 200, data
    assert [j.target_session_key for j in jobs] == [""]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_key", "slots", "code"),
    [
        ("nope", {}, "unknown_session"),
        # A tab showing a channel's or a job's conversation is not this chat.
        ("chat-1", {"chat-1": _chat("slack:C1:1.2")}, "unknown_session"),
        # An app's own chat: delivery never posts into it, so naming it is refused.
        ("chat-1", {"chat-1": _chat(app="some-app")}, "unknown_session"),
        ("slack:C1:1.2", {}, "unsupported_session_namespace"),
        (42, {}, "invalid_session_key"),
        # Within the cap bare, over it once namespaced.
        ("x" * (MAX_SHORT_STRING - 3), {}, "invalid_session_key"),
    ],
)
async def test_unusable_chat_is_refused_and_nothing_is_written(
    tmp_path, session_member, session_key, slots, code
):
    resp, data, jobs = await _create(tmp_path, _body(session_key=session_key), slots)
    assert resp.status == 400
    assert data["code"] == code
    assert jobs == []


@pytest.mark.asyncio
async def test_member_alias_targeting_own_chat_uses_canonical_identity(
    tmp_path, monkeypatch, session_member
):
    from kiro_crew import execution_context
    from kiro_crew.execution_context import MemoryStoreRef

    real_derive_execution = execution_context.derive_execution

    def _derive_execution(parent, *, target_member=None, **kwargs):
        if target_member == "Atlas":
            return replace(
                parent,
                member_id="atlas",
                store=MemoryStoreRef("atlas", "atlas"),
                selection_kind="member",
                template_id="atlas-agent",
                selection_name="Atlas",
            )
        return real_derive_execution(parent, target_member=target_member, **kwargs)

    monkeypatch.setattr(execution_context, "derive_execution", _derive_execution)
    session_member["dashboard:chat-1"] = "atlas"

    resp, data, jobs = await _create(
        tmp_path,
        _body(session_key="chat-1", member_id="Atlas"),
        {"chat-1": _chat()},
    )

    assert resp.status == 200, data
    assert [job.target_session_key for job in jobs] == ["dashboard:chat-1"]
    assert jobs[0].member_id == "atlas"


@pytest.mark.asyncio
async def test_unresolvable_member_keeps_store_invalid_cron_response(
    tmp_path, monkeypatch, session_member
):
    from kiro_crew import execution_context

    def _reject_member(*_args, **_kwargs):
        raise ValueError("unknown member")

    monkeypatch.setattr(execution_context, "derive_execution", _reject_member)
    session_member["dashboard:chat-1"] = "another-member"

    resp, data, jobs = await _create(
        tmp_path,
        _body(session_key="chat-1", member_id="missing"),
        {"chat-1": _chat()},
    )

    assert resp.status == 400
    assert data["code"] == "invalid_cron"
    assert jobs == []


@pytest.mark.asyncio
async def test_chat_of_another_member_is_refused(tmp_path, session_member):
    session_member["dashboard:chat-1"] = "other-bot"
    resp, data, jobs = await _create(tmp_path, _body(session_key="chat-1"), {"chat-1": _chat()})
    assert resp.status == 400
    assert data["code"] == "session_member_mismatch"
    assert jobs == []


@pytest.mark.asyncio
async def test_hidden_job_cannot_name_a_chat(tmp_path, session_member):
    body = _body(session_key="chat-1", hide_in_chat=True)
    resp, data, jobs = await _create(tmp_path, body, {"chat-1": _chat()})
    assert resp.status == 400
    assert data["code"] == "hidden_job_cannot_target_session"
    assert jobs == []


def _job(target: str, *, job_id: str = "abcd1234", member_id: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=job_id,
        name="later",
        member_id=member_id,
        target_session_key=target,
        last_result_stamp=" · Nov 26, 2026, 12:00:00 AM UTC",
        last_result_ts=1795651200.123456,
    )


@pytest.mark.asyncio
async def test_result_is_posted_into_the_target_chat_once(monkeypatch, session_member):
    posted: list[tuple] = []
    monkeypatch.setattr(
        cron_inject,
        "append_and_surface",
        lambda state, slot, role, content, cls: (
            posted.append((slot, role, content)),
            slot.messages.append({"content": content}),
        ),
    )
    chat = _chat()
    state = MagicMock()
    state.get_slot = lambda name: {"chat-1": chat}.get(name)
    job = _job("dashboard:chat-1")

    assert await cron_inject.post_result_to_target_session(state, job, "hello") is True
    assert [(s, r) for s, r, _ in posted] == [(chat, "assistant")]
    assert posted[0][2].endswith("\n\nhello")
    # The same result again does not stack a second copy.
    assert await cron_inject.post_result_to_target_session(state, job, "hello") is False
    assert len(posted) == 1


@pytest.mark.asyncio
async def test_target_chat_member_change_posts_nothing(monkeypatch, session_member):
    monkeypatch.setattr(cron_inject, "append_and_surface", lambda *a, **k: pytest.fail("posted"))
    session_member["dashboard:chat-1"] = "other-bot"
    chat = _chat()
    state = MagicMock()
    state.get_slot = lambda name: {"chat-1": chat}.get(name)

    assert (
        await cron_inject.post_result_to_target_session(state, _job("dashboard:chat-1"), "hello")
        is False
    )


@pytest.mark.asyncio
async def test_result_posts_when_target_chat_member_matches(monkeypatch, session_member):
    posted = []
    monkeypatch.setattr(
        cron_inject,
        "append_and_surface",
        lambda _state, slot, _role, content, _cls: posted.append((slot, content)),
    )
    session_member["dashboard:chat-1"] = "atlas"
    chat = _chat()
    state = MagicMock()
    state.get_slot = lambda name: {"chat-1": chat}.get(name)

    assert await cron_inject.post_result_to_target_session(
        state, _job("dashboard:chat-1", member_id="atlas"), "hello"
    )
    assert [slot for slot, _content in posted] == [chat]


@pytest.mark.asyncio
async def test_existing_pending_target_loads_window_before_reading_messages(
    monkeypatch, session_member
):
    class PendingChat:
        linked_session_key = ""
        window_pending = True

        def __init__(self):
            self._messages = []

        @property
        def messages(self):
            assert self.window_pending is False
            return self._messages

    chat = PendingChat()
    loaded = []

    async def _load_window(slot):
        loaded.append(slot)
        slot.window_pending = False

    monkeypatch.setattr("kiro_crew.dashboard.state.load_window_off_loop", _load_window)
    monkeypatch.setattr(cron_inject, "append_and_surface", lambda *args: None)
    state = MagicMock()
    state.get_slot = lambda name: {"chat-1": chat}.get(name)

    assert await cron_inject.post_result_to_target_session(state, _job("dashboard:chat-1"), "hello")
    assert loaded == [chat]


@pytest.mark.asyncio
async def test_same_named_jobs_post_identical_results_to_one_chat(monkeypatch, session_member):
    posted = []

    def _append(_state, slot, _role, content, _cls):
        posted.append(content)
        slot.messages.append({"content": content})

    monkeypatch.setattr(cron_inject, "append_and_surface", _append)
    chat = _chat()
    state = MagicMock()
    state.get_slot = lambda name: {"chat-1": chat}.get(name)

    first = _job("dashboard:chat-1", job_id="job-one")
    second = _job("dashboard:chat-1", job_id="job-two")
    assert await cron_inject.post_result_to_target_session(state, first, "same result")
    assert await cron_inject.post_result_to_target_session(state, second, "same result")

    assert len(posted) == 2
    assert "<!-- cron-run:job-one:1795651200.123456 -->" in posted[0]
    assert "<!-- cron-run:job-two:1795651200.123456 -->" in posted[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "slots"),
    [
        ("", {"chat-1": _chat()}),
        ("dashboard:chat-1", {"chat-1": _chat("slack:C1:1.2")}),
    ],
)
async def test_no_target_or_relinked_chat_posts_nothing(monkeypatch, session_member, target, slots):
    monkeypatch.setattr(cron_inject, "append_and_surface", lambda *a, **k: pytest.fail("posted"))
    state = MagicMock()
    state.get_slot = lambda name: slots.get(name)
    assert await cron_inject.post_result_to_target_session(state, _job(target), "hi") is False
