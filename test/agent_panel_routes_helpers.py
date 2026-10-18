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

    Not ``member_slot_key(slug)`` spelled out a second time: that is only the
    resolver's FALLBACK branch, so a harness hard-coding it would bind every test
    to the one case where the two spellings agree and never exercise the V2 path,
    where the key carries the member's private store. Production files the entry
    under the slot ``api_member_thread`` created, which is this same resolver, so
    calling it here is what makes the round trip the real pairing.
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

    The panel tool is mounted on nothing but a member's own DM session, so in
    production the publishing unit always runs under slot ``member-<slug>`` -- which
    is the slot the drawer's read folds. Binding the unit to that slot here is what
    makes the round-trip tests exercise the real pairing rather than a slot of the
    test's choosing. The handle is dropped immediately so it holds no lease.
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

    THE FOLD IS THE RECORD, so a round-trip assertion belongs here rather than on
    ``agent_panel.read``, which now only answers for crews that published before the
    record moved into the crew log. Keyed the same two ways the route keys it: the
    member's DM slot, and the publishing crew's ownership digest within it.

    Warm folds are dropped first because the publish under test appended after any
    earlier read in the same test warmed this cell's watermark.
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

    On its own attribute rather than on ``_State`` because that is where the real
    one lives (``DashboardState.sessions``). A stub that answered
    ``state.get_agent_selection`` directly would make a handler calling the wrong
    receiver pass here and fail in production, which is exactly what happened.

    ``get_provider`` is the other half: a publish is appended to the CALLING
    session's own crew log, and the unit resolver reads it off the provider the
    session manager holds. A stub without it would make every publish refuse with
    ``no_crew_log_unit``.
    """

    def __init__(self, agent: str | None, namespace: str):
        self._agent = agent
        self._namespace = namespace
        self.unit = _unit_for(agent) if agent else ""

    def has_session(self, _key) -> bool:
        """Whether a live allocation exists for the caller.

        The gate asks this before it asks WHICH crew, because the two answers are
        different refusals. This stub models one live session per mounted state,
        so ``agent=None`` is a state with no allocation to resolve -- which is a
        distinct case from an allocation that resolved to no crew, and the real
        allocation tests in ``test_agent_panel_routes_allocation.py`` are what hold
        the key-exactness this cannot.
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
        # What the allocation SELECTED, which is the crew binding the resolver
        # trusts. ``get_slot().agent`` carries the same string for a provider
        # template of that name, so the two are kept separate here for the reason
        # the resolver keeps them separate: a template selection must not read as
        # a crew binding.
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

    ``internal`` mints what ``token_auth_middleware`` sets ONLY on a verified
    ``X-Internal-Secret`` match. It defaults to True because the publish surface
    is MCP-only and that is the shape of every real caller; the cookie-only case
    gets its own test, which is the one that matters.

    ``namespace`` is what the allocation selected: ``member`` for a crew, or
    ``template`` for a provider template that merely shares the name.

    ``sessions`` replaces the stub with a real ``SessionManager``, for the tests
    that have to see which KEY the resolver looks the caller up under.
    """
    app = web.Application()
    app["state"] = _State(agent, namespace=namespace, sessions=sessions)

    if owner:

        @web.middleware
        async def _owner(request, handler):
            # Present an owner-dashboard identity: the panel READ route
            # (`api_member_panel`) is owner-gated like its sibling member reads
            # (`api_member_thread`, briefing, rules), so a caller with no owner
            # identity is denied regardless of the internal secret. ``X-Test-User``
            # names a NON-owner caller for the denial test; absent it, the
            # local-owner subject the gate accepts when no owner_id is configured.
            # Applied even when ``internal`` is False, because the drawer is an
            # owner BROWSER caller: owner-identified, no internal secret.
            #
            # Non-destructive: a test that injects its OWN identity (an app token,
            # a specific subject) does so with a middleware inserted at index 0,
            # which runs first; this one must not clobber it, so it only fills a
            # gap the test left open.
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
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_agent_panel_routes*.py modules calls monkeypatch.undo()
def _crew_log_on(monkeypatch):
    """The panel record IS a crew log entry, so every test here runs with the log on.

    Warm slot folds are dropped on both sides: the kernel keeps one watermark per
    cell keyed by slot, and these tests reuse slot names across a fresh data home,
    so a fold carried in from a previous test would answer from the wrong unit.
    """
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
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

    An ``async with`` helper rather than an ``@pytest_asyncio.fixture``, by this
    repo's convention: the pinned pytest-asyncio does not collect async-generator
    fixtures declared with plain ``@pytest.fixture``.

    Closing is not tidiness. A ``TestClient`` owns an aiohttp session AND a
    listening socket, so a returned-but-never-closed client leaks two descriptors
    per test and, on a loaded runner, fails UNRELATED tests with EMFILE. aiohttp
    does say so -- "Unclosed client session" -- but on stderr, where a green run
    hides it.
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
