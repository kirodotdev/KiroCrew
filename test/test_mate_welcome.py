"""Mate's first welcome: once-only, crewmate threads only, a hidden kickoff.

Each guard in :mod:`kiro_crew.dashboard.mate_welcome` has a test here that
fails when the guard is removed: the owed record the first-crewmate step writes
(a member that arrived any other way never greets), the marker claim (two calls
start one turn), the crewmate check (the reserved ``default`` member and an
unknown member never greet), the emptiness and busy checks, and the "no
transcript row" property of the dispatch itself. The welcome travels in the
kickoff, not a prompt, and fits a user who already has other crewmates.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
from kiro_crew.dashboard import mate_welcome as cg
from kiro_crew.members import (
    DM_SLOT_MODE,
    mark_welcome_owed,
    member_slot_key,
    write_dm_binding,
)

MATE_SLUG = "mate"
OTHER_SLUG = "code-reviewer"


def _owe(slug: str, member: str) -> None:
    """What the first-crewmate step records for *member*, whose slug is *slug*."""
    config = SimpleNamespace(agents={member: SimpleNamespace(member_id=slug)})
    assert mark_welcome_owed(member, config=config)


def _bind_thread(state, slug: str, member: str, *, owed: bool = True):
    if owed and member != "default":
        _owe(slug, member)
    key = member_slot_key(slug)
    write_dm_binding(slug, member=member, slot_key=key)
    return state.get_or_create_slot(key, agent=member, mode=DM_SLOT_MODE)


class _Dispatched:
    """Stand-in for ``_dispatch_greeting`` that records each dispatch."""

    def __init__(self) -> None:
        self.slots: list[str] = []
        self.kickoffs: list[str] = []

    def __call__(self, state, slot, kickoff) -> None:
        self.slots.append(slot.key)
        self.kickoffs.append(kickoff)


_ROWS = {
    "default": SimpleNamespace(display_name="", description=""),
    ASSISTANT_MEMBER_NAME: SimpleNamespace(display_name="", description=""),
    "code-reviewer": SimpleNamespace(display_name="Reviewer", description="Review my PRs"),
}


@pytest.fixture(autouse=True)
def roster():
    """The configured roster; a test narrows it by reassigning ``.agents``."""
    loaded = SimpleNamespace(agents=_ROWS)
    with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=loaded):
        yield loaded


ALONE = {k: v for k, v in _ROWS.items() if k != "code-reviewer"}


@pytest.fixture
def dispatched():
    recorder = _Dispatched()
    with patch.object(cg, "_dispatch_greeting", recorder):
        yield recorder


class TestGreetingGuards:
    @pytest.mark.asyncio
    async def test_empty_mate_thread_greets_once(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, MATE_SLUG, ASSISTANT_MEMBER_NAME)
        assert await cg.maybe_start_first_greeting(state, MATE_SLUG) == cg.STARTED
        assert dispatched.slots == [slot.key]
        assert cg.greeting_marker_path(MATE_SLUG).is_file()
        # Reload / second tab / retry after a failed turn: the thread is still
        # empty, but the persisted marker refuses a second greeting.
        assert await cg.maybe_start_first_greeting(state, MATE_SLUG) == cg.ALREADY_GREETED
        assert dispatched.slots == [slot.key]

    @pytest.mark.asyncio
    async def test_concurrent_opens_start_exactly_one_turn(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, MATE_SLUG, ASSISTANT_MEMBER_NAME)
        outcomes = await asyncio.gather(
            *(cg.maybe_start_first_greeting(state, MATE_SLUG) for _ in range(5))
        )
        assert outcomes.count(cg.STARTED) == 1
        assert len(dispatched.slots) == 1

    @pytest.mark.asyncio
    async def test_mate_alone_is_welcomed_as_the_first_crewmate(self, tmp_path, dispatched, roster):
        roster.agents = ALONE
        state = _make_state(tmp_path)
        _bind_thread(state, MATE_SLUG, ASSISTANT_MEMBER_NAME)
        assert await cg.maybe_start_first_greeting(state, MATE_SLUG) == cg.STARTED
        assert dispatched.kickoffs == [cg.FIRST_WELCOME.format(name="Mate")]

    @pytest.mark.asyncio
    async def test_mate_beside_other_crewmates_is_welcomed_as_one_more(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, MATE_SLUG, ASSISTANT_MEMBER_NAME)
        assert await cg.maybe_start_first_greeting(state, MATE_SLUG) == cg.STARTED
        assert dispatched.kickoffs == [cg.FIRST_WELCOME_WITH_CREW.format(name="Mate")]

    @pytest.mark.asyncio
    async def test_another_created_crewmate_owes_nothing_here(self, tmp_path, dispatched):
        """A crewmate made on the dashboard greets through its create flow's seeded turn."""
        state = _make_state(tmp_path)
        _bind_thread(state, OTHER_SLUG, "code-reviewer", owed=False)
        assert await cg.maybe_start_first_greeting(state, OTHER_SLUG) == cg.NOT_OWED
        assert dispatched.slots == []

    @pytest.mark.asyncio
    async def test_a_member_no_create_path_recorded_never_greets(self, tmp_path, dispatched):
        """An imported or auto-registered crewmate has no owed record."""
        state = _make_state(tmp_path)
        _bind_thread(state, OTHER_SLUG, "code-reviewer", owed=False)
        assert await cg.maybe_start_first_greeting(state, OTHER_SLUG) == cg.NOT_OWED
        assert dispatched.slots == []
        assert not cg.greeting_marker_path(OTHER_SLUG).exists()

    @pytest.mark.asyncio
    async def test_a_record_written_for_another_member_owes_nothing(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _owe(OTHER_SLUG, "someone-else")
        _bind_thread(state, OTHER_SLUG, "code-reviewer", owed=False)
        assert await cg.maybe_start_first_greeting(state, OTHER_SLUG) == cg.NOT_OWED
        assert dispatched.slots == []

    @pytest.mark.asyncio
    async def test_the_default_member_and_an_unknown_member_never_greet(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, "default", "default")
        _bind_thread(state, "gone", "gone")
        assert await cg.maybe_start_first_greeting(state, "default") == cg.NOT_CREWMATE
        assert await cg.maybe_start_first_greeting(state, "gone") == cg.NOT_CREWMATE
        assert dispatched.slots == []
        assert not cg.greeting_marker_path("gone").exists()

    @pytest.mark.asyncio
    async def test_a_slot_not_pinned_to_the_member_never_greets(self, tmp_path, dispatched):
        """The binding names one crewmate, but the live slot runs as someone else."""
        state = _make_state(tmp_path)
        key = member_slot_key(MATE_SLUG)
        write_dm_binding(MATE_SLUG, member=ASSISTANT_MEMBER_NAME, slot_key=key)
        state.get_or_create_slot(key, agent="code-reviewer", mode=DM_SLOT_MODE)
        assert await cg.maybe_start_first_greeting(state, MATE_SLUG) == cg.NO_THREAD
        assert dispatched.slots == []

    @pytest.mark.asyncio
    async def test_a_thread_with_messages_never_greets(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, MATE_SLUG, ASSISTANT_MEMBER_NAME)
        slot.append("user", "hi", "msg msg-u")
        assert await cg.maybe_start_first_greeting(state, MATE_SLUG) == cg.NOT_EMPTY
        assert dispatched.slots == []
        # Declining does not spend the once-only marker.
        assert not cg.greeting_marker_path(MATE_SLUG).exists()

    @pytest.mark.asyncio
    async def test_a_running_thread_never_greets(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, MATE_SLUG, ASSISTANT_MEMBER_NAME)
        never = asyncio.get_running_loop().create_future()
        slot.task = asyncio.ensure_future(never)
        try:
            assert await cg.maybe_start_first_greeting(state, MATE_SLUG) == cg.BUSY
        finally:
            slot.task.cancel()
        assert dispatched.slots == []
        assert not cg.greeting_marker_path(MATE_SLUG).exists()

    @pytest.mark.asyncio
    async def test_no_live_thread_never_greets(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        write_dm_binding(
            MATE_SLUG, member=ASSISTANT_MEMBER_NAME, slot_key=member_slot_key(MATE_SLUG)
        )
        assert await cg.maybe_start_first_greeting(state, MATE_SLUG) == cg.NO_THREAD
        assert dispatched.slots == []


class TestHiddenKickoff:
    @pytest.mark.asyncio
    async def test_dispatch_runs_the_kickoff_without_a_transcript_row(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, MATE_SLUG, ASSISTANT_MEMBER_NAME)
        seen: list[tuple[str, dict]] = []

        async def fake_run_chat(_state, _slot, message, **kwargs):
            seen.append((message, kwargs))
            _slot.append("assistant", "Hi, I'm Mate. What should I call you?", "msg")

        with patch("kiro_crew.dashboard.chat._run_chat", fake_run_chat):
            assert await cg.maybe_start_first_greeting(state, MATE_SLUG) == cg.STARTED
            assert slot.task is not None
            await slot.task
        kickoff = cg.FIRST_WELCOME_WITH_CREW.format(name="Mate")
        assert seen == [(kickoff, {"_synthetic_payload": True, "_turn_actor": "gateway"})]
        # The kickoff is never written to the transcript: the only row is the
        # crewmate's own greeting.
        roles = [(m["role"], m["content"]) for m in slot.messages]
        assert roles == [("assistant", "Hi, I'm Mate. What should I call you?")]
        assert all(kickoff not in m["content"] for m in slot.messages)

    def test_a_renamed_first_crewmate_is_welcomed_by_its_new_name(self):
        renamed = SimpleNamespace(display_name="Skipper", description="")
        assert cg.welcome_kickoff(renamed, has_other_crewmates=False) == cg.FIRST_WELCOME.format(
            name="Skipper"
        )


def _greet_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_member_greet

    @web.middleware
    async def _auth(request: web.Request, handler):
        request["app"] = request.headers.get("X-Test-App", "")
        request["user"] = request.headers.get("X-Test-User", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/members/{slug}/greet", api_member_greet)
    return app


class TestGreetRoute:
    @pytest.mark.asyncio
    async def test_owner_gets_the_outcome(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, MATE_SLUG, ASSISTANT_MEMBER_NAME)
        async with TestClient(TestServer(_greet_app(state))) as client:
            first = await client.post(f"/api/members/{MATE_SLUG}/greet")
            second = await client.post(f"/api/members/{MATE_SLUG}/greet")
            assert first.status == 200 and (await first.json()) == {"outcome": cg.STARTED}
            assert (await second.json()) == {"outcome": cg.ALREADY_GREETED}
        assert len(dispatched.slots) == 1

    @pytest.mark.asyncio
    async def test_app_token_is_refused(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, MATE_SLUG, ASSISTANT_MEMBER_NAME)
        async with TestClient(TestServer(_greet_app(state))) as client:
            resp = await client.post(
                f"/api/members/{MATE_SLUG}/greet", headers={"X-Test-App": "some-app"}
            )
            assert resp.status == 404
        assert dispatched.slots == []

    @pytest.mark.asyncio
    async def test_bad_slug_is_400(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_greet_app(state))) as client:
            resp = await client.post("/api/members/Not_A_Slug/greet")
            assert resp.status == 400
        assert dispatched.slots == []
