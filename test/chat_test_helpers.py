"""Shared helpers for chat test modules."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService
from kiro_crew.messaging.link import ChannelLink

#: Draining is a LOOP because a drained task may register another -- not because any
#: current one does (``chat_slack`` has a single ``create_task``, and the backfill
#: posts sequentially). Bounded rather than `while` so a task that re-arms itself
#: forever fails the test instead of hanging until the 120s pytest timeout, whose
#: report names the timeout and not the task.
_DRAIN_ROUNDS = 20


def move_transcript_past(log: ConversationLog, key: str, sig: float) -> None:
    """Deterministically advance a transcript's mtime past *sig*.

    Two consecutive filesystem writes can land inside one timestamp tick
    (~15.6 ms on Windows), leaving the second write with an mtime identical
    to the first -- a staged "transcript moved on" then has not moved on at
    all, and any staleness assertion keyed on the mtime signature becomes a
    coin flip (#2981, same class as #2449). Pinning the mtime makes the test
    exercise the signature COMPARISON rather than the platform's clock
    resolution (testing-conventions.md § Determinism).
    """
    path = log._path(key)
    os.utime(path, (sig + 1, sig + 1))


async def drain_background_tasks(state) -> None:
    """Await every task *state* spawned, so an assertion cannot race one.

    Handlers that must answer the request before their work finishes hand it to
    ``asyncio.create_task`` and register it in ``state._background_tasks`` — the
    Slack link-time backfill (``_spawn_slack_backfill``) is the one this exists for.
    The response arriving therefore proves only that the task was CREATED, so reading
    the Slack mock straight afterwards races it: it usually wins on an idle machine and
    loses under load. That flake surfaces as a plain count mismatch (``assert 0 == 2``)
    or as ``'NoneType' object has no attribute 'args'``, on a DIFFERENT test in the
    class each run, and names neither the task nor the race (#4130).

    Awaiting the not-yet-done members is an exact wait on the real completion
    condition rather than a sleep. An empty set means the task already finished; a
    done-callback discards each task, so the set is snapshotted before awaiting.

    Never a ``sleep``: a sleep long enough to be reliable on the slowest runner is
    paid by every run, and it is still only a guess.

    Exceptions are re-raised, so a task that died silently fails its own test
    instead of surfacing later as an unretrieved-exception warning.

    Scope: this awaits EVERY task in ``state._background_tasks``, which several other
    handlers also register (``handlers/sessions.py``, ``taskrunner.py``,
    ``handlers/mcp.py``). That is right for a state a test built and will discard, and
    wrong for one carrying a long-lived task -- a subscription or a poller would never
    complete and the wait would run to the pytest timeout. Drain a specific task
    directly in that case.
    """
    for _ in range(_DRAIN_ROUNDS):
        pending = [t for t in list(getattr(state, "_background_tasks", ())) if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending)
    raise AssertionError(
        f"background tasks still pending after {_DRAIN_ROUNDS} drain rounds: "
        f"{sorted(t.get_name() for t in state._background_tasks if not t.done())}"
    )


class _ReadyKiroPrerequisiteService(KiroPrerequisiteService):
    async def session_ready(self) -> bool:
        return True

    # The fail-closed gate authorizes on a FRESH probe, not the latch, so an
    # embedded test app must answer both or every gated route 503s.
    async def verified_ready(self, *, max_age_secs: float) -> bool:
        del max_age_secs
        return True


_READY_KIRO_PREREQUISITE = object.__new__(_ReadyKiroPrerequisiteService)


def _make_ready_kiro_prerequisite() -> KiroPrerequisiteService:
    """Return a filesystem-free ready prerequisite for embedded test apps."""

    return _READY_KIRO_PREREQUISITE


def _make_state(tmp_path, **kwargs):
    """Create a DashboardState with mocked services and real ConversationLog."""
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.discard_conversation = AsyncMock()
    sessions.aflush = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    # Real in-memory Slack-link store rather than bare MagicMocks. The unlink
    # path unpacks get_slack_link into (thread_ts, channel_id) and branches on
    # whether a link is PRESENT, and a MagicMock satisfies neither: it iterates
    # empty (ValueError on unpack) and is unconditionally truthy. Parity with
    # SessionStore: absent -> (None, None); clear -> True iff a link was there.
    _slack_links: dict[str, tuple[str, str]] = {}

    def _set_slack_link(key, thread_ts, channel_id):
        if thread_ts or channel_id:
            _slack_links[key] = (thread_ts, channel_id)
        else:
            _slack_links.pop(key, None)

    def _get_slack_link(key):
        return _slack_links.get(key, (None, None))

    def _clear_slack_link(key):
        return _slack_links.pop(key, None) is not None

    sessions.set_slack_link = MagicMock(side_effect=_set_slack_link)
    sessions.get_slack_link = MagicMock(side_effect=_get_slack_link)
    sessions.clear_slack_link = MagicMock(side_effect=_clear_slack_link)

    # Real in-memory mirror-link store, for the same reason as the Slack one and
    # with a sharper failure mode: callers branch on whether a mirror is PRESENT,
    # and a bare MagicMock is unconditionally truthy, so every session reads as
    # mirrored to a channel. A guard that refuses mirrored sessions then refuses
    # ALL of them, which looks like a broken guard rather than a missing double.
    # Parity with SessionStore: absent -> None, present -> ChannelLink (the
    # drain's mirror-retarget comparison reads the link's identity fields, so a
    # bare tuple would make every mirror identical).
    _mirror_links: dict[str, ChannelLink] = {}

    def _set_mirror_link(key, channel_id, thread_ts):
        if channel_id or thread_ts:
            _mirror_links[key] = ChannelLink(
                channel_type="slack", channel_id=channel_id, thread_id=thread_ts
            )
        else:
            _mirror_links.pop(key, None)

    def _get_mirror_link(key):
        return _mirror_links.get(key)

    def _clear_mirror_link(key):
        return _mirror_links.pop(key, None) is not None

    sessions.set_mirror_link = MagicMock(side_effect=_set_mirror_link)
    sessions.get_mirror_link = MagicMock(side_effect=_get_mirror_link)
    sessions.clear_mirror_link = MagicMock(side_effect=_clear_mirror_link)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
        **kwargs,
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _make_app(state: DashboardState) -> web.Application:
    """Minimal aiohttp app with chat endpoints."""
    from kiro_crew.dashboard.chat import (
        api_chat,
        api_chat_mode,
        api_chat_plan_action,
        api_chat_slot_approve,
        api_chat_slot_color,
        api_chat_slot_delete,
        api_chat_slot_detail,
        api_chat_slot_fork,
        api_chat_slot_regenerate,
        api_chat_slot_rename,
        api_chat_slot_resume,
        api_chat_slot_rewind,
        api_chat_slot_stop,
        api_chat_slot_switch_variant,
        api_chat_slots,
        api_chat_slots_cleanup,
    )

    @web.middleware
    async def _test_auth_middleware(request: web.Request, handler):
        """Simulate token_auth_middleware for tests: dashboard owner claims.

        Only sets defaults if not already populated by a test-specific
        middleware inserted earlier (e.g. app-isolation tests that inject
        a specific app identity).
        """
        if "app" not in request:
            request["app"] = ""  # dashboard user, not an app
        if "user" not in request:
            request["user"] = "local-app"  # recognized as owner
        return await handler(request)

    app = web.Application(middlewares=[_test_auth_middleware])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    app.router.add_get("/api/chat/slots", api_chat_slots)
    app.router.add_post("/api/chat/slots/cleanup", api_chat_slots_cleanup)
    app.router.add_get("/api/chat/slots/{slot}", api_chat_slot_detail)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post("/api/chat/slots/{slot}/stop", api_chat_slot_stop)
    app.router.add_delete("/api/chat/slots/{slot}", api_chat_slot_delete)
    app.router.add_post("/api/chat/slots/{slot}/resume", api_chat_slot_resume)
    app.router.add_patch("/api/chat/slots/{slot}/title", api_chat_slot_rename)
    app.router.add_patch("/api/chat/slots/{slot}/color", api_chat_slot_color)
    app.router.add_post("/api/chat/slots/{slot}/regenerate", api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/fork", api_chat_slot_fork)
    app.router.add_post("/api/chat/slots/{slot}/rewind", api_chat_slot_rewind)
    app.router.add_post("/api/chat/slots/{slot}/switch-variant", api_chat_slot_switch_variant)
    app.router.add_post("/api/chat/mode", api_chat_mode)
    app.router.add_post("/api/chat/slots/{slot}/plan-action", api_chat_plan_action)
    return app


def _make_app_with_agent_routes(state: DashboardState) -> web.Application:
    """Minimal aiohttp app with chat endpoints including agent and create routes."""
    from kiro_crew.dashboard.chat import (
        api_chat_slot_agent,
        api_chat_slot_approve,
        api_chat_slot_create,
        api_chat_slot_delete,
        api_chat_slot_detail,
        api_chat_slot_reload,
        api_chat_slot_rename,
        api_chat_slot_resume,
        api_chat_slot_workspace,
        api_chat_slots,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/slots", api_chat_slots)
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    app.router.add_get("/api/chat/slots/{slot}", api_chat_slot_detail)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post("/api/chat/slots/{slot}/agent", api_chat_slot_agent)
    app.router.add_post("/api/chat/slots/{slot}/workspace", api_chat_slot_workspace)
    app.router.add_post("/api/chat/slots/{slot}/reload", api_chat_slot_reload)
    app.router.add_delete("/api/chat/slots/{slot}", api_chat_slot_delete)
    app.router.add_post("/api/chat/slots/{slot}/resume", api_chat_slot_resume)
    app.router.add_patch("/api/chat/slots/{slot}/title", api_chat_slot_rename)
    return app


def _make_folder_app(state: DashboardState) -> web.Application:
    """Minimal aiohttp app with folder endpoints."""
    from kiro_crew.dashboard.chat import api_chat_slots
    from kiro_crew.dashboard.chat_folders import (
        api_chat_folder_create,
        api_chat_folder_delete,
        api_chat_folder_update,
        api_chat_folders,
        api_chat_slot_folder,
        api_chat_slot_pin,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/folders", api_chat_folders)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    app.router.add_delete("/api/chat/folders/{id}", api_chat_folder_delete)
    app.router.add_patch("/api/chat/slots/{slot}/folder", api_chat_slot_folder)
    app.router.add_patch("/api/chat/slots/{slot}/pin", api_chat_slot_pin)
    app.router.add_get("/api/chat/slots", api_chat_slots)
    return app


def _make_tags_app(state: DashboardState, *, app_identity: str | None = None) -> web.Application:
    """Minimal aiohttp app with chat_tags endpoints (vocabulary, columns, drop, slot tags).

    Installs the same dashboard-owner claim middleware the chat app uses, because
    production serves these routes behind ``token_auth_middleware`` and the lane
    dispatch is gated on the ``app`` claim it sets. Without it the claim would be
    ABSENT here and the fail-closed gate would refuse every dispatch, so the suite
    would exercise a path production never takes.

    ``app_identity`` overrides that claim to simulate an APP token (the middleware
    only fills defaults, so a value set here wins). Pass it to test the gate.
    """
    from kiro_crew.dashboard.chat_tags import (
        api_chat_slot_drop,
        api_chat_slot_tags,
        api_chat_tag_column_create,
        api_chat_tag_column_delete,
        api_chat_tag_column_update,
        api_chat_tag_columns,
        api_chat_tag_columns_reorder,
        api_chat_tag_create,
        api_chat_tag_delete,
        api_chat_tag_update,
        api_chat_tags,
    )

    @web.middleware
    async def _tags_auth_middleware(request: web.Request, handler):
        """Stamp the claims token_auth_middleware sets in production."""
        if "app" not in request:
            request["app"] = "" if app_identity is None else app_identity
        if "user" not in request:
            request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_tags_auth_middleware])
    app["state"] = state
    app.router.add_get("/api/chat/tags", api_chat_tags)
    app.router.add_post("/api/chat/tags", api_chat_tag_create)
    app.router.add_patch("/api/chat/tags/{id}", api_chat_tag_update)
    app.router.add_delete("/api/chat/tags/{id}", api_chat_tag_delete)
    app.router.add_put("/api/chat/slots/{slot}/tags", api_chat_slot_tags)
    app.router.add_post("/api/chat/slots/{slot}/drop", api_chat_slot_drop)
    app.router.add_get("/api/chat/tag-columns", api_chat_tag_columns)
    app.router.add_post("/api/chat/tag-columns", api_chat_tag_column_create)
    app.router.add_put("/api/chat/tag-columns/order", api_chat_tag_columns_reorder)
    app.router.add_patch("/api/chat/tag-columns/{id}", api_chat_tag_column_update)
    app.router.add_delete("/api/chat/tag-columns/{id}", api_chat_tag_column_delete)
    return app


class AsyncIterator:
    """Helper to create an async iterator from a list."""

    def __init__(self, items):
        self._items = items
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item
