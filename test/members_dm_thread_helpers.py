"""Shared constants and fakes for the ``test_members_dm_thread*`` tests.

``CREW`` and ``OTHER``, ``_record_or_explain`` and its ``_RenderedLogs`` handler,
``_fake_config``, ``_patched_config``, ``_make_members_app`` and ``_member_slot``.
``test_members_dm_thread.py`` and its ``_resume_and_reservation``,
``_activity_and_briefing`` and ``_private_store_reopen`` siblings import them by name.
Nothing here is a fixture, so nothing is autouse: each test's data home comes from the
rootdir conftest's autouse ``_isolate_kirocrew_home``. ``test_members_dm_thread.py``
re-exports ``CREW``, ``_fake_config`` and ``_make_members_app`` for
``test_chat_agent_selection.py``, ``test_member_approval_default.py`` and
``test_member_memory_runtime.py``.
"""

from __future__ import annotations

import concurrent.futures
import logging
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.members import DM_SLOT_MODE, record_activity, write_dm_binding

CREW = "code-reviewer"
OTHER = "other-agent"


class _RenderedLogs(logging.Handler):
    """Keeps each record as RENDERED TEXT only, never the record itself.

    A record carrying ``exc_info`` keeps its traceback, the traceback keeps the
    frame, and a frame in the crew-log write path holds a ``CrewLog`` handle and
    its write lease (see ``crew_log.store.log_exception_text``). Rendering on
    arrival and dropping the record keeps nothing alive.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(threadName)s %(name)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _record_or_explain(member, session_key, memory_mode, **kwargs) -> bool:
    """``record_activity``, run off the event loop, with the reason for a ``False``.

    ``record_activity`` is blocking file IO and documents that async callers run it
    through ``asyncio.to_thread``: on the event-loop thread the crew-log
    ``file_lock`` makes ONE attempt and refuses rather than stall the loop. Most of
    the tests that call it are coroutines, so the call runs on a worker thread, as it does
    in production.

    It is also best-effort by contract -- every failure is swallowed and logged at
    DEBUG -- so a bare ``assert record_activity(...)`` fails as ``assert False``
    with the cause discarded. When the answer is ``False`` this raises with the
    member, crew-log and event-log records the call produced.
    """
    handler = _RenderedLogs()
    names = ("kiro_crew.members", "kiro_crew.eventlog", "kiro_crew.crew_log")
    loggers = [logging.getLogger(n) for n in names]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            ok = pool.submit(record_activity, member, session_key, memory_mode, **kwargs).result()
    finally:
        for lg, level in zip(loggers, levels):
            lg.removeHandler(handler)
            lg.setLevel(level)
    if not ok:
        detail = "\n".join(handler.lines) or "(no log records)"
        raise AssertionError(
            f"record_activity({member!r}, {session_key!r}) returned False:\n{detail}"
        )
    return ok


def _fake_config(names, default=CREW):
    return SimpleNamespace(
        agents={name: KiroCrewAgentConfig(kiro_agent="kirocrew") for name in names},
        default_agent=default,
        memory_stores={},
        workspaces={"default": SimpleNamespace(dir="workspace")},
        default_workspace="default",
        degraded_sections=frozenset(),
    )


def _make_members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import (
        api_member_activity,
        api_member_briefing,
        api_member_thread,
        api_members,
    )

    @web.middleware
    async def _auth(request: web.Request, handler):
        if "app" not in request:
            request["app"] = ""
        # POST /api/members/{slug}/thread is owner-gated. ``local-app`` is the
        # standalone-local owner subject the gate accepts when no owner_id is
        # configured; set only when a test has not already chosen a caller, so
        # the non-owner and app-token cases can still pick their own.
        if "user" not in request:
            # ``X-Test-User`` names a NON-owner caller (the dashboard_owner_helpers
            # convention); absent, the caller is the standalone-local owner.
            request["user"] = request.headers.get("X-Test-User", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    app.router.add_post("/api/members/{slug}/thread", api_member_thread)
    app.router.add_get("/api/members/{slug}/activity", api_member_activity)
    app.router.add_get("/api/members/{slug}/briefing", api_member_briefing)
    return app


def _patched_config(names, default=CREW):
    return patch(
        "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load",
        return_value=_fake_config(names, default),
    )


def _member_slot(state, key="member-code-reviewer", agent=CREW):
    # A real member slot is born via POST /api/members/{slug}/thread, which
    # always writes the binding first — mirror that here so the send path's
    # binding-drift guard sees the legitimate state.
    write_dm_binding(key[len("member-") :], member=agent, slot_key=key)
    slot = state.get_or_create_slot(key, agent=agent, mode=DM_SLOT_MODE)
    assert slot.mode == DM_SLOT_MODE
    return slot
