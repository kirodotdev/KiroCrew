"""Captain's first greeting: once-only, Captain-only, and a hidden kickoff.

Each guard in :mod:`kiro_crew.dashboard.captain_greeting` has a test here that
fails when the guard is removed: the marker claim (two calls start one turn),
the Captain identity check (another crewmate never greets), the emptiness and
busy checks, and the "no transcript row" property of the dispatch itself.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
from kiro_crew.dashboard import captain_greeting as cg
from kiro_crew.members import DM_SLOT_MODE, member_slot_key, write_dm_binding

CAPTAIN_SLUG = "kirocrew-captain"
OTHER_SLUG = "code-reviewer"


def _bind_thread(state, slug: str, member: str):
    key = member_slot_key(slug)
    write_dm_binding(slug, member=member, slot_key=key)
    return state.get_or_create_slot(key, agent=member, mode=DM_SLOT_MODE)


class _Dispatched:
    """Stand-in for ``_dispatch_greeting`` that records each dispatch."""

    def __init__(self) -> None:
        self.slots: list[str] = []
        self.kickoffs: list[str] = []

    def __call__(self, state, slot, kickoff: str = cg.CAPTAIN_GREETING_KICKOFF) -> None:
        self.slots.append(slot.key)
        self.kickoffs.append(kickoff)


@pytest.fixture
def dispatched():
    recorder = _Dispatched()
    with patch.object(cg, "_dispatch_greeting", recorder):
        yield recorder


class TestGreetingGuards:
    @pytest.mark.asyncio
    async def test_empty_captain_thread_greets_once(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, CAPTAIN_SLUG, ASSISTANT_MEMBER_NAME)
        assert await cg.maybe_start_captain_greeting(state, CAPTAIN_SLUG) == cg.STARTED
        assert dispatched.slots == [slot.key]
        assert cg.greeting_marker_path(CAPTAIN_SLUG).is_file()
        # Reload / second tab / retry after a failed turn: the thread is still
        # empty, but the persisted marker refuses a second greeting.
        assert await cg.maybe_start_captain_greeting(state, CAPTAIN_SLUG) == cg.ALREADY_GREETED
        assert dispatched.slots == [slot.key]

    @pytest.mark.asyncio
    async def test_concurrent_opens_start_exactly_one_turn(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, CAPTAIN_SLUG, ASSISTANT_MEMBER_NAME)
        outcomes = await asyncio.gather(
            *(cg.maybe_start_captain_greeting(state, CAPTAIN_SLUG) for _ in range(5))
        )
        assert outcomes.count(cg.STARTED) == 1
        assert len(dispatched.slots) == 1

    @pytest.mark.asyncio
    async def test_other_crewmates_never_greet(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, OTHER_SLUG, "code-reviewer")
        assert await cg.maybe_start_captain_greeting(state, OTHER_SLUG) == cg.NOT_CAPTAIN
        assert dispatched.slots == []
        assert not cg.greeting_marker_path(OTHER_SLUG).exists()

    @pytest.mark.asyncio
    async def test_a_slot_not_pinned_to_captain_never_greets(self, tmp_path, dispatched):
        """The binding says Captain, but the live slot runs as someone else."""
        state = _make_state(tmp_path)
        key = member_slot_key(CAPTAIN_SLUG)
        write_dm_binding(CAPTAIN_SLUG, member=ASSISTANT_MEMBER_NAME, slot_key=key)
        state.get_or_create_slot(key, agent="code-reviewer", mode=DM_SLOT_MODE)
        assert await cg.maybe_start_captain_greeting(state, CAPTAIN_SLUG) == cg.NO_THREAD
        assert dispatched.slots == []

    @pytest.mark.asyncio
    async def test_a_thread_with_messages_never_greets(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, CAPTAIN_SLUG, ASSISTANT_MEMBER_NAME)
        slot.append("user", "hi", "msg msg-u")
        assert await cg.maybe_start_captain_greeting(state, CAPTAIN_SLUG) == cg.NOT_EMPTY
        assert dispatched.slots == []
        # Declining does not spend the once-only marker.
        assert not cg.greeting_marker_path(CAPTAIN_SLUG).exists()

    @pytest.mark.asyncio
    async def test_a_running_thread_never_greets(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, CAPTAIN_SLUG, ASSISTANT_MEMBER_NAME)
        never = asyncio.get_running_loop().create_future()
        slot.task = asyncio.ensure_future(never)
        try:
            assert await cg.maybe_start_captain_greeting(state, CAPTAIN_SLUG) == cg.BUSY
        finally:
            slot.task.cancel()
        assert dispatched.slots == []
        assert not cg.greeting_marker_path(CAPTAIN_SLUG).exists()

    @pytest.mark.asyncio
    async def test_no_live_thread_never_greets(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        write_dm_binding(
            CAPTAIN_SLUG, member=ASSISTANT_MEMBER_NAME, slot_key=member_slot_key(CAPTAIN_SLUG)
        )
        assert await cg.maybe_start_captain_greeting(state, CAPTAIN_SLUG) == cg.NO_THREAD
        assert dispatched.slots == []


class TestCrewmateGreeting:
    """A crewmate created with ``first_greeting`` opens by asking for its goal."""

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_a_planted_owed_symlink_never_truncates_its_target(self, tmp_path):
        victim = tmp_path / "policy.json"
        victim.write_text('{"keep": true}', encoding="utf-8")
        owed = cg.greeting_owed_path(OTHER_SLUG)
        owed.parent.mkdir(parents=True, exist_ok=True)
        owed.symlink_to(victim)
        cg.mark_greeting_owed(OTHER_SLUG)
        assert victim.read_text(encoding="utf-8") == '{"keep": true}'
        assert owed.is_symlink()

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    @pytest.mark.asyncio
    async def test_a_symlinked_owed_record_owes_no_greeting(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, OTHER_SLUG, "code-reviewer")
        real = tmp_path / "elsewhere.json"
        real.write_text("{}", encoding="utf-8")
        owed = cg.greeting_owed_path(OTHER_SLUG)
        owed.parent.mkdir(parents=True, exist_ok=True)
        owed.symlink_to(real)
        assert await cg.maybe_start_member_greeting(state, OTHER_SLUG) == cg.NOT_OWED
        assert dispatched.slots == []

    @pytest.mark.asyncio
    async def test_owed_crewmate_greets_once_with_the_goal_question(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, OTHER_SLUG, "code-reviewer")
        cg.mark_greeting_owed(OTHER_SLUG)
        assert await cg.maybe_start_member_greeting(state, OTHER_SLUG) == cg.STARTED
        assert dispatched.slots == [slot.key]
        assert dispatched.kickoffs == [cg.CREWMATE_GOAL_KICKOFF]
        assert cg.crewmate_greeting_marker_path(OTHER_SLUG).is_file()
        # A crewmate's claim is its own: Captain's marker is never touched.
        assert not cg.greeting_marker_path(OTHER_SLUG).exists()
        assert await cg.maybe_start_member_greeting(state, OTHER_SLUG) == cg.ALREADY_GREETED
        assert dispatched.slots == [slot.key]

    @pytest.mark.asyncio
    async def test_a_crewmate_not_owed_a_greeting_never_greets(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, OTHER_SLUG, "code-reviewer")
        assert await cg.maybe_start_member_greeting(state, OTHER_SLUG) == cg.NOT_OWED
        assert dispatched.slots == []
        assert not cg.crewmate_greeting_marker_path(OTHER_SLUG).exists()

    @pytest.mark.asyncio
    async def test_an_owed_crewmate_with_messages_never_greets(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, OTHER_SLUG, "code-reviewer")
        cg.mark_greeting_owed(OTHER_SLUG)
        slot.append("user", "hi", "msg msg-u")
        assert await cg.maybe_start_member_greeting(state, OTHER_SLUG) == cg.NOT_EMPTY
        assert dispatched.slots == []
        assert not cg.crewmate_greeting_marker_path(OTHER_SLUG).exists()

    @pytest.mark.asyncio
    async def test_a_slot_running_as_another_member_never_greets(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        key = member_slot_key(OTHER_SLUG)
        write_dm_binding(OTHER_SLUG, member="code-reviewer", slot_key=key)
        state.get_or_create_slot(key, agent="someone-else", mode=DM_SLOT_MODE)
        cg.mark_greeting_owed(OTHER_SLUG)
        assert await cg.maybe_start_member_greeting(state, OTHER_SLUG) == cg.NO_THREAD
        assert dispatched.slots == []

    @pytest.mark.asyncio
    async def test_captain_is_routed_to_its_own_greeting(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, CAPTAIN_SLUG, ASSISTANT_MEMBER_NAME)
        assert await cg.maybe_start_member_greeting(state, CAPTAIN_SLUG) == cg.STARTED
        assert dispatched.kickoffs == [cg.CAPTAIN_GREETING_KICKOFF]
        assert cg.greeting_marker_path(CAPTAIN_SLUG).is_file()

    @pytest.mark.asyncio
    async def test_the_description_reaches_the_kickoff(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, OTHER_SLUG, "code-reviewer")
        cg.mark_greeting_owed(OTHER_SLUG)
        with patch.object(cg, "_crewmate_description", lambda member: "Review open PRs"):
            assert await cg.maybe_start_member_greeting(state, OTHER_SLUG) == cg.STARTED
        assert dispatched.kickoffs == [cg.crewmate_goal_kickoff("Review open PRs")]
        assert "'Review open PRs'" in dispatched.kickoffs[0]

    @pytest.mark.asyncio
    async def test_a_send_during_the_description_read_wins_over_the_greeting(
        self, tmp_path, dispatched
    ):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, OTHER_SLUG, "code-reviewer")
        cg.mark_greeting_owed(OTHER_SLUG)

        def read_while_the_user_sends(member: str) -> str:
            slot.append("user", "hi", "msg msg-u")
            return "Review open PRs"

        with patch.object(cg, "_crewmate_description", read_while_the_user_sends):
            assert await cg.maybe_start_member_greeting(state, OTHER_SLUG) == cg.NOT_EMPTY
        assert dispatched.slots == []

    def test_goal_kickoff_asks_for_the_goal_and_is_never_the_users_words(self):
        assert cg.crewmate_goal_kickoff("  ") == cg.CREWMATE_GOAL_KICKOFF
        assert "what they want you to do" in cg.CREWMATE_GOAL_KICKOFF
        assert "They have not typed anything" in cg.CREWMATE_GOAL_KICKOFF
        assert "Do not start any work" in cg.CREWMATE_GOAL_KICKOFF

    @pytest.mark.parametrize("description", ["", "Review open PRs"])
    def test_goal_kickoff_keeps_the_template_out_of_the_greeting(self, description):
        kickoff = cg.crewmate_goal_kickoff(description)
        assert "introduce yourself by your name" in kickoff
        assert "Do not name your template, role or agent type" in kickoff
        assert "do not describe how you work" in kickoff
        # The kickoff never hands the crewmate a template name to repeat.
        assert "conductor" not in kickoff.lower()


class TestHiddenKickoff:
    @pytest.mark.asyncio
    async def test_dispatch_runs_the_kickoff_without_a_transcript_row(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, CAPTAIN_SLUG, ASSISTANT_MEMBER_NAME)
        seen: list[tuple[str, dict]] = []

        async def fake_run_chat(_state, _slot, message, **kwargs):
            seen.append((message, kwargs))
            _slot.append("assistant", "Hi, I'm Captain. What should I call you?", "msg")

        with patch("kiro_crew.dashboard.chat._run_chat", fake_run_chat):
            assert await cg.maybe_start_captain_greeting(state, CAPTAIN_SLUG) == cg.STARTED
            assert slot.task is not None
            await slot.task
        assert seen == [
            (cg.CAPTAIN_GREETING_KICKOFF, {"_synthetic_payload": True, "_turn_actor": "gateway"})
        ]
        # The kickoff is never written to the transcript: the only row is
        # Captain's own greeting.
        roles = [(m["role"], m["content"]) for m in slot.messages]
        assert roles == [("assistant", "Hi, I'm Captain. What should I call you?")]
        assert all(cg.CAPTAIN_GREETING_KICKOFF not in m["content"] for m in slot.messages)

    @pytest.mark.asyncio
    async def test_crewmate_kickoff_runs_without_a_transcript_row(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _bind_thread(state, OTHER_SLUG, "code-reviewer")
        cg.mark_greeting_owed(OTHER_SLUG)
        seen: list[tuple[str, dict]] = []

        async def fake_run_chat(_state, _slot, message, **kwargs):
            seen.append((message, kwargs))
            _slot.append("assistant", "I'm code-reviewer. What should I look after?", "msg")

        with (
            patch("kiro_crew.dashboard.chat._run_chat", fake_run_chat),
            patch.object(cg, "_crewmate_description", lambda member: ""),
        ):
            assert await cg.maybe_start_member_greeting(state, OTHER_SLUG) == cg.STARTED
            await slot.task
        assert seen == [
            (cg.CREWMATE_GOAL_KICKOFF, {"_synthetic_payload": True, "_turn_actor": "gateway"})
        ]
        assert [m["role"] for m in slot.messages] == ["assistant"]

    @pytest.mark.asyncio
    async def test_a_known_goal_kickoff_is_the_whole_kickoff_still_hidden(self, tmp_path):
        # A crewmate created with a goal runs the full kickoff (the goal
        # question, the schedule offer) plus that goal, as the same hidden
        # gateway turn: no transcript row, nothing the user typed.
        state = _make_state(tmp_path)
        slot = _bind_thread(state, OTHER_SLUG, "code-reviewer")
        cg.mark_greeting_owed(OTHER_SLUG)
        seen: list[tuple[str, dict]] = []

        async def fake_run_chat(_state, _slot, message, **kwargs):
            seen.append((message, kwargs))
            _slot.append("assistant", "I'm code-reviewer. Review open PRs, right?", "msg")

        with (
            patch("kiro_crew.dashboard.chat._run_chat", fake_run_chat),
            patch.object(cg, "_crewmate_description", lambda member: "Review open PRs"),
        ):
            assert await cg.maybe_start_member_greeting(state, OTHER_SLUG) == cg.STARTED
            await slot.task
        ((message, kwargs),) = seen
        assert kwargs == {"_synthetic_payload": True, "_turn_actor": "gateway"}
        assert message.startswith(cg.CREWMATE_GOAL_KICKOFF)
        assert "Review open PRs" in message[len(cg.CREWMATE_GOAL_KICKOFF) :]
        assert [m["role"] for m in slot.messages] == ["assistant"]

    def test_kickoff_carries_the_tag_the_role_prompt_names(self):
        from kiro_crew.agent import _ASSISTANT_SYSTEM_PROMPT

        tag = "[Captain first greeting]"
        assert cg.CAPTAIN_GREETING_KICKOFF.startswith(tag)
        assert tag in _ASSISTANT_SYSTEM_PROMPT
        assert "learn_add" in _ASSISTANT_SYSTEM_PROMPT
        # The kickoff repeats the naming rule so the greeting turn cannot read a
        # name off the home path the session context shows.
        assert "do not welcome them back" in cg.CAPTAIN_GREETING_KICKOFF
        assert "never one taken from a username" in cg.CAPTAIN_GREETING_KICKOFF
        # ...and the self-introduction comes before the address question.
        assert "introduce yourself by your name" in cg.CAPTAIN_GREETING_KICKOFF
        # Length and content live in ONE place, the role rule; the kickoff never
        # restates a sentence count that could drift from it.
        assert "sentence" not in cg.CAPTAIN_GREETING_KICKOFF
        assert '"First greeting and names"' in cg.CAPTAIN_GREETING_KICKOFF
        assert "### First greeting and names" in _ASSISTANT_SYSTEM_PROMPT


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
        _bind_thread(state, CAPTAIN_SLUG, ASSISTANT_MEMBER_NAME)
        async with TestClient(TestServer(_greet_app(state))) as client:
            first = await client.post(f"/api/members/{CAPTAIN_SLUG}/greet")
            second = await client.post(f"/api/members/{CAPTAIN_SLUG}/greet")
            assert first.status == 200 and (await first.json()) == {"outcome": cg.STARTED}
            assert (await second.json()) == {"outcome": cg.ALREADY_GREETED}
        assert len(dispatched.slots) == 1

    @pytest.mark.asyncio
    async def test_route_greets_an_owed_crewmate(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, OTHER_SLUG, "code-reviewer")
        async with TestClient(TestServer(_greet_app(state))) as client:
            before = await client.post(f"/api/members/{OTHER_SLUG}/greet")
            assert (await before.json()) == {"outcome": cg.NOT_OWED}
            cg.mark_greeting_owed(OTHER_SLUG)
            after = await client.post(f"/api/members/{OTHER_SLUG}/greet")
            assert (await after.json()) == {"outcome": cg.STARTED}
        assert dispatched.kickoffs == [cg.CREWMATE_GOAL_KICKOFF]

    @pytest.mark.asyncio
    async def test_app_token_is_refused(self, tmp_path, dispatched):
        state = _make_state(tmp_path)
        _bind_thread(state, CAPTAIN_SLUG, ASSISTANT_MEMBER_NAME)
        async with TestClient(TestServer(_greet_app(state))) as client:
            resp = await client.post(
                f"/api/members/{CAPTAIN_SLUG}/greet", headers={"X-Test-App": "some-app"}
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
