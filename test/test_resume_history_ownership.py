"""Who may hydrate a persisted conversation through History resume?

``api_chat_slot_resume`` has two branches. When a live slot already exists it
applies the App Kit §5.2 ownership check against that slot before returning
anything. When it does not, it used to go straight to::

    slot = state.get_or_create_slot(name, app=request.get("app", ""), ...)
    meta = state.conversation_log.get_metadata(history_key)
    all_messages = state.conversation_log.read_messages_chained(history_key)

``history_key`` is ``body["key"]`` — caller-supplied, and not required to match
the slot name in the URL — so it names an arbitrary conversation. The only slot
on that path is the one the request just created carrying the caller's own
identity, so authorizing against it lets the claim stand as its own evidence.

The authority is the transcript's persisted owner. ``meta["app"]`` is written by
the slot save, is one of ``history.SLOT_OWNED_META_KEYS`` (so its ABSENCE is the
positive statement "no app owns this", not a gap), and both restore paths
already rebuild ``slot._app`` from it.

Distinct from the session-binding boundary in #2783: that one governs which
LIVE SESSION a slot may acquire authority over, this one governs which PERSISTED
TRANSCRIPT a caller may read and adopt.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

APP_A = "issue-radar"
APP_B = "spec-builder"

SLACK_KEY = "slack:1785370133.085469"
DASHBOARD_KEY = "dashboard:chat-7-1785300000"
APP_B_KEY = "dashboard:spec-builder-99"
APP_A_KEY = "dashboard:issue-radar-5"
MISSING_KEY = "dashboard:no-such-conversation"

SECRET = "board minutes: acquisition price is 4.2M"

#: What a caller who may not read a conversation is told, byte for byte. It must
#: also be what a conversation that does not exist answers, or the difference is
#: an oracle for enumerating history keys.
REFUSED_BODY = {"error": "not found", "code": "slot_not_found"}


def _write_transcript(state, key: str, meta: dict[str, Any]) -> None:
    """Persist a session the way the history layer stores one."""
    from kiro_crew.history import _safe_key

    log = state.conversation_log
    path = log._path(_safe_key(key))
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"_type": "metadata", "created_at": "2026-08-01T10:00:00", **meta})]
    lines.append(json.dumps({"role": "user", "content": SECRET, "ts": "2026-08-01T10:00:01"}))
    lines.append(json.dumps({"role": "assistant", "content": "noted", "ts": "2026-08-01T10:00:02"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log._invalidate_cache(key)


def _resume_app(state, app_identity: str | None) -> web.Application:
    """The real resume route, with the auth middleware's app stamp."""
    from kiro_crew.dashboard.chat_handlers import api_chat_slot_resume

    middlewares = []
    if app_identity is not None:

        @web.middleware
        async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
            request["app"] = app_identity
            return await handler(request)

        middlewares.append(_identity)
    app = web.Application(middlewares=middlewares)
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/resume", api_chat_slot_resume)
    return app


async def _resume(state, app_identity, slot_name: str, history_key: str):
    async with TestClient(TestServer(_resume_app(state, app_identity))) as client:
        resp = await client.post(f"/api/chat/slots/{slot_name}/resume", json={"key": history_key})
        return resp.status, await resp.json()


def _contents(body: dict) -> list[str]:
    return [m.get("content", "") for m in body.get("messages", [])]


class TestAnAppCannotHydrateAForeignConversation:
    """The slot name is not the authority; the transcript's owner is."""

    @pytest.mark.asyncio
    async def test_an_ordinary_dashboard_users_conversation(self, tmp_path):
        """The broad case: no channel shape, no app owner — the kind every user
        has dozens of. Note the slot name is unrelated to the history key."""
        state = _make_state(tmp_path)
        _write_transcript(state, DASHBOARD_KEY, {"title": "Board notes"})

        status, body = await _resume(state, APP_A, "app-a-scratch", DASHBOARD_KEY)

        assert status == 404, body
        assert SECRET not in _contents(body)

    @pytest.mark.asyncio
    async def test_another_apps_conversation(self, tmp_path):
        state = _make_state(tmp_path)
        _write_transcript(state, APP_B_KEY, {"title": "B's work", "app": APP_B})

        status, body = await _resume(state, APP_A, "app-a-scratch", APP_B_KEY)

        assert status == 404, body
        assert SECRET not in _contents(body)

    @pytest.mark.asyncio
    async def test_a_channel_conversation(self, tmp_path):
        state = _make_state(tmp_path)
        _write_transcript(state, SLACK_KEY, {"title": "#exec thread", "channel_origin": True})

        status, body = await _resume(state, APP_A, "app-a-scratch", SLACK_KEY)

        assert status == 404, body
        assert SECRET not in _contents(body)

    @pytest.mark.asyncio
    async def test_missing_ownership_metadata_is_deny_by_default(self, tmp_path):
        """``app`` is a slot-owned key, so an unscoped save OMITS it.

        Absence therefore means "no app owns this", not "unknown" — an app
        caller is refused rather than admitted on a gap.
        """
        state = _make_state(tmp_path)
        _write_transcript(state, DASHBOARD_KEY, {})

        status, body = await _resume(state, APP_A, "app-a-scratch", DASHBOARD_KEY)

        assert status == 404, body
        assert SECRET not in _contents(body)


class TestARefusalIsNotAnExistenceOracle:
    @pytest.mark.asyncio
    async def test_a_foreign_conversation_answers_exactly_like_a_missing_one(self, tmp_path):
        """If these two differ in any externally visible way, an app can walk
        the history namespace for keys that exist."""
        state = _make_state(tmp_path)
        _write_transcript(state, APP_B_KEY, {"title": "B's work", "app": APP_B})

        foreign_status, foreign_body = await _resume(state, APP_A, "probe", APP_B_KEY)
        missing_status, missing_body = await _resume(state, APP_A, "probe", MISSING_KEY)

        assert foreign_status == missing_status == 404
        assert foreign_body == missing_body == REFUSED_BODY

    @pytest.mark.asyncio
    async def test_the_refusal_matches_the_existing_slot_branchs_refusal(self, tmp_path):
        """Both §5.2 refusals on this route must read the same to a caller."""
        state = _make_state(tmp_path)
        _write_transcript(state, APP_B_KEY, {"title": "B's work", "app": APP_B})
        # A live slot owned by B, addressed by its own name.
        state.get_or_create_slot("spec-builder-99", app=APP_B)

        existing_status, existing_body = await _resume(state, APP_A, "spec-builder-99", APP_B_KEY)
        create_status, create_body = await _resume(state, APP_A, "app-a-scratch", APP_B_KEY)

        assert existing_status == create_status == 404
        assert existing_body == create_body == REFUSED_BODY


class TestARefusalHasNoSideEffects:
    @pytest.mark.asyncio
    async def test_no_slot_is_created_and_nothing_is_hydrated(self, tmp_path):
        """A denial that still creates the slot hands the app a foothold: the
        tab exists, it is app-owned, and the next request finds it on the
        already-exists branch."""
        state = _make_state(tmp_path)
        _write_transcript(
            state,
            APP_B_KEY,
            {
                "title": "B's work",
                "app": APP_B,
                "agent": "researcher",
                "model": "x",
                "pinned": True,
            },
        )
        before = dict(state._slots)

        status, _ = await _resume(state, APP_A, "app-a-scratch", APP_B_KEY)

        assert status == 404
        assert state._slots == before, "the refused resume left a slot behind"
        assert "app-a-scratch" not in state._slots

    @pytest.mark.asyncio
    async def test_the_apps_own_slots_are_untouched(self, tmp_path):
        """Nothing restored from the foreign transcript may land anywhere — not
        a title, agent, model, pin, or channel provenance.

        The URL names a slot that does not exist, so the request takes the
        creation branch; the app's real tab is a bystander and must stay one.
        """
        from kiro_crew.dashboard.chat_utils import effective_session_key

        state = _make_state(tmp_path)
        _write_transcript(
            state,
            SLACK_KEY,
            {"title": "#exec thread", "channel_origin": True, "agent": "researcher"},
        )
        own = state.get_or_create_slot("app-a-worker", app=APP_A)
        own.title = "A's own tab"

        def _snapshot():
            return (
                own.title,
                own.agent,
                own.model,
                own.pinned,
                own.linked_session_key,
                getattr(own, "channel_origin", False),
                len(own.messages),
                own._dirty,
                sorted(state._slots),
                sorted(state._restricted_keys),
            )

        before = _snapshot()

        status, _ = await _resume(state, APP_A, "app-a-scratch", SLACK_KEY)

        assert status == 404
        assert _snapshot() == before, "the refused resume mutated slot state"
        assert effective_session_key(own) == "dashboard:app-a-worker"

    @pytest.mark.asyncio
    async def test_the_foreign_transcript_is_not_rewritten(self, tmp_path):
        """Resume clears a ``closed`` flag on the history it loads. A refused
        one must not be edited at all."""
        from kiro_crew.history import _safe_key

        state = _make_state(tmp_path)
        _write_transcript(state, APP_B_KEY, {"title": "B's work", "app": APP_B, "closed": True})
        path = state.conversation_log._path(_safe_key(APP_B_KEY))
        before = path.read_bytes()

        status, _ = await _resume(state, APP_A, "app-a-scratch", APP_B_KEY)

        assert status == 404
        assert path.read_bytes() == before, "the refused resume rewrote the transcript"


class TestWhatMustKeepWorking:
    @pytest.mark.asyncio
    async def test_an_app_resumes_its_own_conversation(self, tmp_path):
        state = _make_state(tmp_path)
        _write_transcript(state, APP_A_KEY, {"title": "A's work", "app": APP_A})

        status, body = await _resume(state, APP_A, "issue-radar-5", APP_A_KEY)

        assert status == 200, body
        assert SECRET in _contents(body)

    @pytest.mark.asyncio
    async def test_a_dashboard_user_resumes_a_channel_conversation(self, tmp_path):
        """No app scope — History resume keeps working, channels included."""
        state = _make_state(tmp_path)
        _write_transcript(state, SLACK_KEY, {"title": "#exec thread", "channel_origin": True})

        status, body = await _resume(state, None, "slack_1785370133.085469", SLACK_KEY)

        assert status == 200, body
        assert SECRET in _contents(body)

    @pytest.mark.asyncio
    async def test_a_dashboard_user_resumes_an_app_owned_conversation(self, tmp_path):
        """The guard is scoped to app callers; an operator still sees everything
        their History pane lists."""
        state = _make_state(tmp_path)
        _write_transcript(state, APP_B_KEY, {"title": "B's work", "app": APP_B})

        status, body = await _resume(state, None, "spec-builder-99", APP_B_KEY)

        assert status == 200, body
        assert SECRET in _contents(body)

    @pytest.mark.asyncio
    async def test_a_dashboard_user_resumes_an_ordinary_conversation(self, tmp_path):
        state = _make_state(tmp_path)
        _write_transcript(state, DASHBOARD_KEY, {"title": "Board notes"})

        status, body = await _resume(state, None, "chat-7-1785300000", DASHBOARD_KEY)

        assert status == 200, body
        assert SECRET in _contents(body)

    @pytest.mark.asyncio
    async def test_the_existing_slot_branch_still_serves_the_owner(self, tmp_path):
        """The other branch is unchanged: an app with a live slot it owns still
        gets it back."""
        state = _make_state(tmp_path)
        _write_transcript(state, APP_A_KEY, {"title": "A's work", "app": APP_A})
        live = state.get_or_create_slot("issue-radar-5", app=APP_A)
        live.append("user", "already here", "msg msg-u")

        status, body = await _resume(state, APP_A, "issue-radar-5", APP_A_KEY)

        assert status == 200, body
        assert "already here" in _contents(body)
