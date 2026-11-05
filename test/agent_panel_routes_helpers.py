"""Shared helpers for the ``test_agent_panel_routes*`` tests.

The crew and slot builders, the stub session registry and gateway state, the mounted panel app
and its started client, the autouse crew-log switch, and the ``vetted`` session fixture.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from off_loop_helpers import off_loop

from kiro_crew import agent_panel
from kiro_crew import crew_log as lg
from kiro_crew import members as members_mod
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.dashboard.handlers import agent_panel as routes

CREW = "fleet-crew"
SLUG = "fleet-crew"


def _crew_slot(crew_name: str) -> str:
    """The DM slot a crew's panel lives on, through the READ ROUTE's own resolver.

    ``member_slot_key(slug)`` is only the resolver's fallback; hard-coding it would never
    exercise the V2 path, where the key carries the member's private store.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    slug = members_mod.member_slug(crew_name)
    try:
        slot, _store = routes._member_thread_slot(KiroCrewConfig.load(), crew_name, slug)
    except Exception:
        # The resolver's own fallback, for a crew with no addressable member space.
        return members_mod.member_slot_key(slug)
    return slot


def _unit_for(crew_name: str) -> str:
    """Create the crew log unit a publish from *crew_name*'s DM session appends to.

    Bound to the crew's DM slot, the one the drawer's read folds, since the panel tool
    is mounted only there. The handle is dropped at once so it holds no lease.
    """
    unit = f"acp-{members_mod.member_slug(crew_name)}"
    # Idempotent: ``create`` refuses an existing unit, and a single test can mount
    # two clients for the same crew (the ownership and takeover cases do).
    if not lg.crew_log_path(lg.KIND_SESSION, unit).exists():
        CrewLog.create(
            lg.KIND_SESSION, unit, owner="owner", agent=crew_name, slot=_crew_slot(crew_name)
        )
    return unit


def _folded(crew_name: str = CREW) -> dict[str, Any] | None:
    """The crew's OWN panel record, read the way the drawer's route reads it.

    The fold is the record, keyed by the member's DM slot and the crew's ownership
    digest. Warm folds are dropped first, since an earlier read set the watermark.
    """
    crew_log_projection.forget_slot_folds()
    folded = off_loop(
        crew_log_projection.read_slot_projection, _crew_slot(crew_name), "panel"
    ).value
    owners = folded.get("owners") if isinstance(folded, dict) else None
    mine = (owners or {}).get(agent_panel.crew_key(crew_name))
    # An empty ``template`` is the fold's own "nothing published", matching the
    # route's test for a real record.
    if isinstance(mine, dict) and str(mine.get("template") or ""):
        return mine
    return None


class _Sessions:
    """The SessionManager surface this handler uses, and only that.

    Its own attribute, like ``DashboardState.sessions``, so a handler calling the wrong
    receiver fails here too. ``get_provider`` gives the calling session's unit;
    without it every publish refuses with ``no_crew_log_unit``.
    """

    def __init__(self, agent: str | None, namespace: str):
        self._agent = agent
        self._namespace = namespace
        self.unit = _unit_for(agent) if agent else ""

    def has_session(self, _key) -> bool:
        """Whether a live allocation exists for the caller.

        ``agent=None`` means no allocation, a different refusal from one that resolved to
        no crew. Key exactness is tested in ``test_agent_panel_routes_allocation.py``.
        """
        return self._agent is not None

    def get_agent_selection(self, _key) -> tuple[str, str]:
        if self._agent is None:
            return "template", ""
        return self._namespace, self._agent

    def get_provider(self, _key):
        if not self.unit:
            return None
        return SimpleNamespace(session_id=self.unit)


class _State:
    """Just enough DashboardState for the crew resolver, plus a broadcast log."""

    def __init__(self, agent: str | None, *, namespace: str = "member", sessions: Any = None):
        self._agent = agent
        # What the allocation SELECTED, kept apart from ``get_slot().agent`` so a
        # template of the same name does not read as a crew binding.
        self._namespace = namespace
        # A REAL ``SessionManager`` when one is injected, which is the only way to
        # exercise the key the resolver hands the registry -- the stub answers
        # whatever key it is given.
        self.sessions = _Sessions(agent, namespace) if sessions is None else sessions
        self.broadcasts: list[tuple[str, object]] = []

    def get_slot(self, _name):
        if self._agent is None:
            return None
        return SimpleNamespace(agent=self._agent)

    def broadcast_ws(self, msg_type: str, data: object) -> None:
        self.broadcasts.append((msg_type, data))


def _mounted(
    agent: str | None = CREW,
    *,
    internal: bool = True,
    owner: bool = True,
    namespace: str = "member",
    sessions: Any = None,
) -> web.Application:
    """The panel routes on a bare app.

    ``internal`` mimics a verified ``X-Internal-Secret`` match (every real publish is
    MCP). ``namespace`` is ``member`` or ``template``. ``sessions`` swaps in a real
    ``SessionManager`` to see which key the resolver looks up.
    """
    app = web.Application()
    app["state"] = _State(agent, namespace=namespace, sessions=sessions)

    if owner:

        @web.middleware
        async def _owner(request, handler):
            # `api_member_panel` is owner-gated, so present an owner identity even
            # without ``internal`` (the drawer is an owner browser). ``X-Test-User``
            # names a non-owner. Only fills gaps: a test's own identity middleware
            # at index 0 runs first and must not be clobbered.
            if "app" not in request:
                request["app"] = ""
            if "user" not in request:
                request["user"] = request.headers.get("X-Test-User", "local-app")
            return await handler(request)

        app.middlewares.append(_owner)

    if internal:

        @web.middleware
        async def _internal(request, handler):
            request["internal_auth"] = True
            return await handler(request)

        app.middlewares.append(_internal)
    routes.register_agent_panel_routes(app)
    return app


@pytest.fixture(autouse=True)
def _crew_log_on(_floor_monkeypatch):
    """The panel record IS a crew log entry, so every test here runs with the log on.

    Warm slot folds are dropped on both sides: the kernel keeps one watermark per
    cell keyed by slot, and these tests reuse slot names across a fresh data home,
    so a fold carried in from a previous test would answer from the wrong unit.
    """
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    crew_log_emit.reset_caches()
    crew_log_projection.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log_projection.forget_slot_folds()


@pytest.fixture(name="vetted")
def vetted_fixture(monkeypatch):
    """Treat the caller as a recognized, unrestricted session."""

    async def _recognize(_state, _sk, _op, **_kw):
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognize)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *_a, **_k: False)


@asynccontextmanager
async def _client(
    agent: str | None = CREW,
    *,
    internal: bool = True,
    owner: bool = True,
    namespace: str = "member",
    sessions: Any = None,
):
    """A started client that always closes.

    Not a fixture: the pinned pytest-asyncio does not collect async-generator fixtures
    under plain ``@pytest.fixture``. An unclosed client leaks a session and a socket,
    which fails unrelated tests with EMFILE on a loaded runner.
    """
    c = TestClient(
        TestServer(
            _mounted(agent, internal=internal, owner=owner, namespace=namespace, sessions=sessions)
        )
    )
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()
