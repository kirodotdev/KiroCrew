"""Regression: the reconciler's read path must actually work against a real
gateway.

The live-pod failure this pins: ``GET /api/chat/slots`` published slot key
``chat-1-1788206731``, but ``GET /api/chat/slots/chat-1-1788206731`` answered
``slot_not_found`` (404) — so the ``sdlc-tag-reconcile`` cron could never read a
chat's messages, never find a pull-request URL, and never promoted anything,
while reporting "nothing changed" every run (indistinguishable from correct).

Root cause: the reconcile cron is registered by the ``chat-status-tags`` app,
so its ``chat_status_tags_api`` calls arrive on the internal-secret path where
``request["app"]`` is derived to ``chat-status-tags``. ``api_chat_slot_detail``
runs ``_deny_cross_app_slot_access``, which confines an app token to slots it
OWNS (App Kit §5.2). A user chat's ``slot._app`` is unset, so the detail route
returns ``slot_not_found`` for it — for EVERY user chat, which is exactly the
set the reconciler needs to read. The LIST route has no such per-slot gate, so
listing succeeds while every detail read fails.

The fix retires ``GET /slots/{slot}`` from the reconciler's surface and reads
pull-request URLs from each slot's ``source_links`` on the LIST instead. These
tests use the REAL ``DashboardState``, REAL ``_ChatSlot`` objects, and the REAL
list/detail handlers behind a real ``TestServer`` — not a stub that accepts any
key — so they assert the round-trip property no existing test covered:

* for a dashboard user, the key the LIST publishes RESOLVES on the DETAIL route
  (the identity round-trip);
* for an APP token, the DETAIL route DENIES a user chat (the bug's real cause) —
  proving the reconciler cannot read messages that way and must use the list;
* the LIST already carries ``source_links`` (PR URLs) and ``tags``, which is the
  entire input the fixed reconciler needs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_handlers import api_chat_slot_detail, api_chat_slots
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

_APP = "chat-status-tags"


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _client(state: DashboardState, *, app_name: str = "") -> TestClient:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/slots", api_chat_slots)
    app.router.add_get("/api/chat/slots/{slot}", api_chat_slot_detail)
    if app_name:

        @web.middleware
        async def _as_app(request, handler):
            # Mirror the internal-secret path: an app token carries a positive
            # app claim (token_auth._derive_internal_caller_app), which is what
            # _deny_cross_app_slot_access keys on.
            request["app"] = app_name
            return await handler(request)

        app.middlewares.append(_as_app)
    return TestClient(TestServer(app))


class TestReconcilerReadPathRoundTrip:
    """The property the live-pod bug violated, asserted with real objects."""

    @pytest.mark.asyncio
    async def test_dashboard_user_list_key_resolves_on_detail(self, tmp_path):
        """A dashboard user: the key from the LIST must RESOLVE on DETAIL.

        This is the identity round-trip no test asserted — the list's ``key``
        and the detail route's ``match_info['slot']`` lookup key are the same
        string for a real slot, so a user chat listed IS readable.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1-1788206731")
        slot.messages.append({"role": "user", "content": "Ping"})

        async with _client(state) as client:  # dashboard user (no app claim)
            listed = await client.get("/api/chat/slots")
            assert listed.status == 200
            slots = await listed.json()
            keys = [s["key"] for s in slots]
            assert "chat-1-1788206731" in keys

            for key in keys:
                detail = await client.get(f"/api/chat/slots/{key}")
                assert detail.status == 200, (
                    f"list published key {key!r} but detail 404'd — the "
                    "round-trip the reconciler depends on is broken"
                )

    @pytest.mark.asyncio
    async def test_app_token_is_denied_on_user_chat_detail(self, tmp_path):
        """The BUG's real cause: an app token can LIST a user chat but is DENIED
        (``slot_not_found``) on its DETAIL. So the reconciler — an app-token
        caller — can never read a user chat's messages through the detail route,
        which is why retiring that call and reading ``source_links`` from the
        list is the only workable path.
        """
        state = _make_state(tmp_path)
        state.get_or_create_slot("chat-1-1788206731")  # user chat: slot._app == ""

        async with _client(state, app_name=_APP) as client:
            listed = await client.get("/api/chat/slots")
            assert listed.status == 200
            keys = [s["key"] for s in await listed.json()]
            assert "chat-1-1788206731" in keys, "the app can still LIST the slot"

            detail = await client.get("/api/chat/slots/chat-1-1788206731")
            assert detail.status == 404
            assert (await detail.json())["code"] == "slot_not_found", (
                "an app token reading a user chat's detail must be denied — "
                "this is precisely what made the reconciler a silent no-op"
            )

    @pytest.mark.asyncio
    async def test_list_carries_source_links_and_tags_for_the_reconciler(self, tmp_path):
        """The fix's premise: everything the reconciler needs is on the LIST.

        A chat that mentions a GitHub pull request surfaces that URL in the
        slot's ``source_links`` (kind ``change``), and the slot's ``tags`` ride
        the same payload — so the app-token reconciler judges status without
        ever touching the (denied) detail route.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-9")
        slot.messages.append(
            {
                "role": "assistant",
                "content": "opened https://github.com/o/r/pull/42 for review",
            }
        )
        # Content mutation invalidates the cached source-link scan (the same
        # contract the live append path honours).
        slot.invalidate_source_links()

        async with _client(state, app_name=_APP) as client:
            listed = await client.get("/api/chat/slots")
            assert listed.status == 200
            (row,) = [s for s in await listed.json() if s["key"] == "chat-9"]
            assert "tags" in row
            change_urls = [
                link["url"]
                for link in row.get("source_links", [])
                if link.get("kind", "change") == "change"
            ]
            assert "https://github.com/o/r/pull/42" in change_urls, (
                "the PR URL the reconciler acts on must be present on the LIST "
                "payload, since the detail route is closed to the app"
            )


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
