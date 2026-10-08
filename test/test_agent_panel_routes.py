"""Tests for the crew-webview HTTP routes.

The property worth guarding hardest: which crew a publish writes to is derived
from the CALLING SESSION's own crew binding, never from anything the caller
sends. A body-supplied crew name would let one crew publish a webview that
presents as another's, and the whole value of a per-crew dashboard is that the
operator can trust whose state they are reading.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from member_memory_helpers import write_member_home
from off_loop_helpers import off_loop

from kiro_crew import agent_panel
from kiro_crew import crew_log as lg
from kiro_crew import members as members_mod
from kiro_crew.config.paths import data_home
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.dashboard.handlers import agent_panel as routes

pytestmark = pytest.mark.asyncio

CREW = "fleet-crew"
SLUG = "fleet-crew"

#: A crew whose name matches a template the OPERATOR installed. The route's
#: name-match behaviour is proven against a template this test drops on disk, so
#: it does not depend on any particular consumer's artifact shipping.
BESPOKE = "bespoke-crew"


def _install_template(template_id: str) -> None:
    """Install a minimal template an operator dropped on disk.

    Only the marker is needed here -- these tests assert which template id the
    route selects, not how it renders. The data home is per-test.
    """
    over = agent_panel.override_templates_dir()
    over.mkdir(parents=True, exist_ok=True)
    (over / f"{template_id}.html").write_text(agent_panel.DATA_MARKER, encoding="utf-8")


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
        allocation tests below are what hold the key-exactness this cannot.
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


@pytest.fixture
def vetted(monkeypatch):
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


# ------------------------------------------------------------------ publishing


async def test_a_publish_lands_on_the_callers_own_crew(vetted):
    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}, "title": "fleet"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["ok"] is True
        # The DURABLE record, which is what keeps this route working with the crew
        # log off -- the default.
        stored = agent_panel.read(SLUG)
        assert stored is not None
        assert stored["crew"] == CREW
        assert stored["data"] == {"cycle": 47}
        # And the ADDITIONAL record, which is what gives the panel a history.
        folded = _folded()
        assert folded is not None, "the publish recorded no history"
        assert folded["data"] == {"cycle": 47}
        assert folded["publishes"] == 1


async def test_a_publish_keys_on_the_crews_persisted_member_id(vetted):
    """A publish must address the crew the way every READ of it does.

    ``members.member_slug`` returns a crew's persisted ``member_id`` when it has
    one, and the drawer, the roster and the read route all resolve through it.
    Memory provisioning suffixes that id on purpose when a deleted crew's stores
    still hold the name-derived slug, so a crew whose ``member_id`` differs from
    its name is a state the product's own writer produces rather than a contrived
    one. Deriving the write key from the NAME in that state stores the record
    under a slug nothing reads: the publish reports success and the dashboard
    never appears.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import config_dir

    # The shape memory provisioning writes: the name-derived slug with a suffix.
    persisted = f"{SLUG}-9f3a2b1c"
    assert persisted != members_mod.slug_for_name(CREW), "the test needs the two to differ"
    (config_dir() / "config.json").write_text(
        json.dumps({"agents": {CREW: {"kiro_agent": "kirocrew", "member_id": persisted}}}),
        encoding="utf-8",
    )
    # The read side's own answer, so this pins the write against the resolver the
    # drawer uses rather than against a second copy of the expectation.
    assert members_mod.member_slug(CREW, KiroCrewConfig.load()) == persisted

    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}, "title": "fleet"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()

    stored = _folded()
    assert stored is not None, "the record must land where the drawer reads"
    assert stored["data"] == {"cycle": 47}
    # The name-derived slug is the slot NO read path resolves to. Asserted on the
    # slot rather than on the file because the slot is what the append is keyed by.
    crew_log_projection.forget_slot_folds()
    astray = off_loop(
        crew_log_projection.read_slot_projection, members_mod.member_slot_key(SLUG), "panel"
    ).value
    assert not (astray.get("owners") if isinstance(astray, dict) else None), (
        "nothing may be appended under the name-derived slug: that is the slot "
        "no read path resolves to"
    )


async def test_a_live_owner_with_a_suffixed_member_id_keeps_its_record(vetted):
    """Liveness must resolve identity the way the publish key does.

    Two crews, one slug. The OWNER's persisted ``member_id`` is the slug the
    CALLER's name derives -- the state memory provisioning writes when a deleted
    crew's stores still hold the name-derived slug. Enumerating liveness by NAME
    skips that owner, because its own name derives something else, so the owner
    reads as gone; and "gone" is exactly what the store accepts as permission to
    take a record over. The owner is live here, so the collision must be refused
    and the owner's bytes must survive untouched.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import config_dir

    owner = "legacy-crew"
    assert members_mod.slug_for_name(owner) != SLUG, "the owner's NAME must miss the slug"
    (config_dir() / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    owner: {"kiro_agent": "kirocrew", "member_id": SLUG},
                    CREW: {"kiro_agent": "kirocrew"},
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = KiroCrewConfig.load()
    # Both halves of the premise, asserted against the resolver itself rather than
    # restated: a fixture that stops producing the collision fails HERE instead of
    # passing the test for the wrong reason.
    assert members_mod.member_slug(owner, cfg) == SLUG
    assert members_mod.member_slug(CREW, cfg) == SLUG

    agent_panel.publish(SLUG, template="default", data={"mine": 1}, crew=owner)

    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"theirs": 2}, "title": "fleet"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "crew_slug_collision"

    assert agent_panel.read(SLUG)["data"] == {
        "mine": 1
    }, "a live crew's panel was overwritten by a colliding publish"


async def test_a_cookie_only_caller_cannot_publish_as_a_chosen_session(vetted):
    """THE authorization test. The crew is resolved from a caller-CHOSEN header.

    ``X-Session-Key`` is an identity CLAIM, not a lookup key: a caller holding
    only a dashboard cookie could name any live session, have it resolve to THAT
    crew, and overwrite the crew's panel. ``vetted`` is applied deliberately --
    session recognition passing is exactly the condition under which the old code
    went on to write, so this proves the internal-secret gate refuses FIRST rather
    than relying on recognition to fail.
    """
    async with _client(internal=False) as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"x": 1}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "internal_secret_required"
        assert agent_panel.read(SLUG) is None, "a refused caller must not reach the store"


async def test_a_cookie_only_caller_cannot_list_templates(vetted):
    """The same gate covers the whole MCP-only surface, not just the write."""
    async with _client(internal=False) as c:
        resp = await c.get(
            "/api/agent-panel/templates", headers={"X-Session-Key": "dashboard:chat-1"}
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "internal_secret_required"


async def test_the_drawer_read_needs_no_internal_secret():
    """The drawer is a browser caller. The gate above must not reach the READ.

    Pinned beside the refusals so a later widening of that check cannot 403 the
    dashboard's own drawer -- which would look like the feature is broken.
    """
    async with _client(internal=False) as c:
        resp = await c.get(f"/api/members/{SLUG}/panel?member={CREW}")
        assert resp.status == 200


async def test_a_non_owner_reads_the_panel_but_triggers_no_card_write(vetted):
    """The panel READ is served to any dashboard caller -- the crew webview depends on it,
    so a route-level owner gate would 403 a non-owner's webview. Only the derived-card WRITE
    is owner-gated: a dashboard token with an empty app identity but a NON-owner subject (the
    `!dashboard` Slack case) still READS its panel, but must not reach `_publish_derived_card`,
    or a non-owner GET would mutate the owner-facing store and broadcast to owners.
    """
    app = _mounted()
    async with TestClient(TestServer(app)) as c:
        # Publish a card as the owner so there is something to protect.
        pub = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"x": 1}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert pub.status == 200
        before = len(app["state"].broadcasts)
        # A non-owner (named via X-Test-User) is SERVED the read -- the webview still works --
        # but the write is skipped, so nothing reaches a broadcast.
        resp = await c.get(
            f"/api/members/{SLUG}/panel?member={CREW}",
            headers={"X-Test-User": "not-the-owner"},
        )
        assert resp.status == 200, "the non-owner read was refused, breaking the crew webview"
        # The write is gated, not the read: no store mutation reaches a broadcast on the
        # non-owner read.
        assert len(app["state"].broadcasts) == before


async def test_the_crew_is_not_taken_from_the_body(vetted):
    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"x": 1}, "crew": "research-lab", "slug": "research-lab"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400
        # The unknown field is REJECTED by the schema rather than ignored: silently
        # dropping it would let a caller believe it had retargeted the write.
        assert (await resp.json())["code"] == "validation_error"
        assert agent_panel.read("research-lab") is None


async def test_a_session_with_no_dashboard_slot_is_told_that_and_nothing_else(vetted):
    """A conductor publishing every cycle into a void looks like a broken feature.

    Three causes, three messages. A caller with no dashboard slot is told THAT --
    not that its allocation could not be resolved, which for such a caller is
    false: a subagent inheriting its parent's member selection resolves perfectly
    well and is refused because publishing is confined to the crew's own thread.
    Collapsing the two would reintroduce, one case over, the conflation this
    change exists to remove.
    """
    async with _client(agent=None) as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"x": 1}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "no_dashboard_slot"


async def test_an_omitted_template_resolves_to_the_crews_own(vetted):
    """How a crew with a template of its own gets it without asking for it."""
    _install_template(BESPOKE)
    async with _client(agent=BESPOKE) as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 1}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200
        assert (await resp.json())["panel"]["template"] == BESPOKE


async def test_a_crew_with_no_template_of_its_own_gets_the_generic_one(vetted):
    async with _client(agent="research-lab") as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 1}},
            headers={"X-Session-Key": "dashboard:chat-9"},
        )
        assert resp.status == 200
        assert (await resp.json())["panel"]["template"] == "default"


async def test_a_store_refusal_travels_with_its_code(vetted):
    """The crew can only correct the call on its next cycle if it is told which
    refusal fired."""
    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"k": "x" * (agent_panel._MAX_DATA_BYTES + 10)}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "data_too_large"


async def test_the_response_does_not_echo_the_payload(vetted):
    """Repeating a 64 KB payload into the tool result burns the context this
    feature exists to save."""
    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"needle": "n" * 900}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert "n" * 100 not in json.dumps(await resp.json())


async def test_a_refused_session_never_reaches_the_store(monkeypatch):
    """Recognition runs before anything is written."""

    async def _refuse(_state, _sk, _op, **_kw):
        return web.json_response({"error": "no", "code": "unrecognized"}, status=403)

    monkeypatch.setattr(routes, "_recognize_session", _refuse)
    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"x": 1}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 403
        assert agent_panel.read(SLUG) is None


async def test_a_restricted_session_is_refused(monkeypatch):
    """A published panel is durable on-disk state, which those modes promise not
    to leave behind."""

    async def _recognize(_state, _sk, _op, **_kw):
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognize)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *_a, **_k: True)
    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"x": 1}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "restricted_session"
        assert agent_panel.read(SLUG) is None


async def test_templates_reports_the_crews_default(vetted):
    """The crew asks which template it would get, so it can name one explicitly."""
    async with _client() as c:
        body = await (
            await c.get("/api/agent-panel/templates", headers={"X-Session-Key": "dashboard:chat-1"})
        ).json()
        assert body["default"] == agent_panel.DEFAULT_TEMPLATE_ID
        assert agent_panel.DEFAULT_TEMPLATE_ID in set(body["templates"])


async def test_templates_reports_a_name_matched_template_as_the_default(vetted):
    """The name match is reported, not just applied silently at publish time."""
    _install_template(BESPOKE)
    async with _client(agent=BESPOKE) as c:
        body = await (
            await c.get("/api/agent-panel/templates", headers={"X-Session-Key": "dashboard:chat-1"})
        ).json()
        assert body["default"] == BESPOKE
        assert {agent_panel.DEFAULT_TEMPLATE_ID, BESPOKE} <= set(body["templates"])


# --------------------------------------------------------------------- reading


async def test_the_drawer_read_returns_the_composed_document(vetted):
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert body["panel"]["crew"] == CREW
        assert agent_panel._DATA_ELEMENT_ID in body["html"]
        assert agent_panel.DATA_MARKER not in body["html"], "the marker must have been filled"


async def test_the_drawer_read_serves_the_publish_history_the_fold_records(vetted):
    """The fold computes ``history``/``publishes``/``history_omitted`` on every

    publish, and the widened serializer serves all three to the drawer. A
    multi-cycle history is published so the three are non-trivial, and the round
    trip asserts they arrive.
    """
    async with _client() as c:
        for i in range(3):
            await c.post(
                "/api/agent-panel/publish",
                json={"data": {"cycle": i}},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        panel = body["panel"]
        # publishes counts every publish on this owner's slot.
        assert panel["publishes"] == 3
        # history holds the superseded panels (publishes minus the current one),
        # each an {at,title,template} row.
        assert isinstance(panel["history"], list)
        assert len(panel["history"]) == 2
        for row in panel["history"]:
            assert set(row) == {"at", "title", "template"}
        # history_omitted is the bound speaking; nothing aged out under the cap here.
        assert panel["history_omitted"] == 0


async def test_the_widened_serializer_still_withholds_the_ownership_digest(vetted):
    """The allow-list serves only classified keys, so it never leaks ``crew_key``.

    ``_panel_owner_record`` carries the digest, and an allow-list classifies it
    WITHHELD -- a deny-list serving "everything but a few" is what a leak would need.
    This is the sibling of the route's own crew_key-never-served pin, asserted on a
    record that also carries history so the served set stays free of the digest even
    for a rich record.
    """
    async with _client() as c:
        for i in range(2):
            await c.post(
                "/api/agent-panel/publish",
                json={"data": {"cycle": i}},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert "crew_key" not in json.dumps(body)
        assert agent_panel.crew_key(CREW) not in json.dumps(body)
        # schema is the record's internal version tag and is withheld too.
        assert "schema" not in body["panel"]


def test_the_drawer_serializer_classifies_every_record_key():
    """The red-on-unclassified contract: every key ``_panel_owner_record`` produces

    is classified served-or-withheld, and a key in NEITHER set reddens rather than
    being silently served or silently dropped. Keying the test on the record's OWN
    keys is what keeps the fold's shape and the drawer's shape from diverging
    silently: add a field to ``_panel_owner_record`` without classifying it in
    ``agent_panel.py`` and this fails.
    """
    from kiro_crew.crew_log import projection

    record = projection._panel_owner_record(projection._panel_owner_start())
    classified = routes._PANEL_SERVED_KEYS | routes._PANEL_WITHHELD_KEYS
    unclassified = set(record) - classified
    assert not unclassified, (
        f"_panel_owner_record produces {sorted(unclassified)}, classified neither "
        "served nor withheld; add them to _PANEL_SERVED_KEYS or _PANEL_WITHHELD_KEYS"
    )
    # The sets are disjoint -- a key cannot be both served and withheld.
    assert not (routes._PANEL_SERVED_KEYS & routes._PANEL_WITHHELD_KEYS)
    # And the serializer raises, not defaults, on a genuinely unclassified key.
    with pytest.raises(KeyError):
        routes._panel_meta({**record, "a_brand_new_unclassified_field": 1})


async def test_the_drawer_read_carries_the_raw_data_in_published_order(vetted):
    """The docked summary is rendered natively from this, not from the document.

    A dashboard needs a full page to be legible, so the ~250px drawer shows a
    few fields instead -- and it must show the ones the CREW put first, which is
    only possible if the order survives the wire. Asserted on a key set whose
    alphabetical order differs from the published order, so a serializer that
    sorted keys reddens here.
    """
    published = {"cycle": 47, "needs_you": "one ruling is waiting on you", "aardvark": 1}
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": published},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert body["panel"]["data"] == published
        assert list(body["panel"]["data"].keys()) == ["cycle", "needs_you", "aardvark"]


async def test_the_drawer_read_has_a_data_object_even_for_a_broken_record(vetted):
    """The drawer indexes into this, so it must never be null."""
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 1}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert isinstance(body["panel"]["data"], dict)


async def test_the_drawer_read_is_an_empty_state_not_an_error(vetted):
    """A crew that never published is not a failure -- the drawer shows nothing."""
    async with _client() as c:
        resp = await c.get(f"/api/members/never-published/panel?member={CREW}")
        assert resp.status == 200
        assert (await resp.json()) == {"panel": None, "html": None}


async def test_a_broken_published_template_returns_retryable_error(vetted):
    """A broken publication is an error, not the never-published empty state."""
    template_id = "fragile"
    _install_template(template_id)
    template_path = agent_panel.override_templates_dir() / f"{template_id}.html"
    private_template_text = "PRIVATE-TEMPLATE-CONTENTS"

    async with _client() as c:
        published = await c.post(
            "/api/agent-panel/publish",
            json={"template": template_id, "data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert published.status == 200, await published.text()

        # Corrupt the template after publication so compose raises PanelError on read.
        template_path.write_text(private_template_text, encoding="utf-8")
        resp = await c.get(f"/api/members/{SLUG}/panel?member={CREW}")
        body = await resp.json()

        assert resp.status == 503
        assert body == {
            "error": "could not render the panel",
            "code": "panel_render_failed",
        }
        encoded = json.dumps(body)
        assert private_template_text not in encoded
        assert str(template_path) not in encoded


@pytest.mark.parametrize("hostile", ["has space", "Upper.Case", "with/slash"])
async def test_the_drawer_read_refuses_a_hostile_slug(vetted, hostile):
    """A slug that reaches the handler is rejected by ``validate_slug``.

    ``..`` is deliberately NOT in this list: the HTTP layer normalises it away
    before routing, so it answers 404 and never reaches the handler at all. The
    store's own traversal test covers that shape.
    """
    async with _client() as c:
        resp = await c.get(f"/api/members/{hostile}/panel?member={CREW}")
        assert resp.status in (400, 404)
        if resp.status == 400:
            assert (await resp.json())["code"] == "invalid_member_slug"


async def test_the_read_route_is_not_under_the_strict_internal_prefix():
    """The drawer is a browser caller, so its route must not sit behind the
    MCP-only prefix -- it would 403 for the dashboard."""
    from kiro_crew.dashboard import server

    paths = {str(r.resource.canonical) for r in _mounted().router.routes()}
    read_path = "/api/members/{slug}/panel"
    assert read_path in paths
    assert not any(read_path.startswith(p) for p in server._STRICT_INTERNAL_API_PATHS)


async def test_the_publish_routes_are_under_the_strict_internal_prefix():
    from kiro_crew.dashboard import server

    for path in ("/api/agent-panel/publish", "/api/agent-panel/templates"):
        assert any(
            path.startswith(p) for p in server._STRICT_INTERNAL_API_PATHS
        ), f"{path} would fall through to cookie auth"


async def test_the_response_comes_from_a_single_record_read(vetted, monkeypatch):
    """Both halves from ONE snapshot, so a mid-request publish cannot split them.

    ``render`` + ``read`` as two calls read the record twice; a publish landing
    between them returned the OLD document beside the NEW summary, and the docked
    chip would then contradict the expanded view. Counting reads is the property --
    an interleaving test would depend on winning a race.

    Counted at ``_panel_record``, which is where the one snapshot is taken now that
    the fold is the record and the file is only its fallback.
    """
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )

        reads: list[str] = []
        real = routes._panel_record

        def counting(slot: str, slug: str, owner_key: str):
            reads.append(slug)
            return real(slot, slug, owner_key)

        monkeypatch.setattr(routes, "_panel_record", counting)
        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()

        # Both halves are actually populated, or the count below would be vacuous.
        assert body["html"]
        assert body["panel"]["template"]
        assert (
            len(reads) == 1
        ), f"the record was read {len(reads)}x; both halves must share one snapshot"


async def test_an_edited_template_still_takes_effect_without_republishing(vetted):
    """The compose-on-read property the single-snapshot change had to preserve.

    Sourcing both halves from one record must not become composing once at publish:
    an operator editing a template sees it on the next drawer open, with no crew
    cycle in between. Without this, X3's fix could have been "compose at publish",
    which passes the read-count test above and silently drops the feature.
    """
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        before = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert "SENTINEL-EDIT" not in before["html"]

        # Edit the template in place, publishing nothing. An operator override wins
        # over the shipped file, which is exactly the customisation seam being tested.
        over = agent_panel.override_templates_dir()
        over.mkdir(parents=True, exist_ok=True)
        (over / "default.html").write_text(
            f"<div data-edit='SENTINEL-EDIT'>{agent_panel.DATA_MARKER}</div>", encoding="utf-8"
        )

        after = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert "SENTINEL-EDIT" in after["html"], (
            "an edited template did not reach the drawer, so composition is no longer "
            "happening on read"
        )


async def test_a_linked_template_dir_is_a_coded_refusal_not_a_500(vetted):
    """The refusal we added has a code, so the routes must hand the code back.

    ``available_templates`` and ``template_for_crew`` both read the override
    directory, which REFUSES a linked path rather than following it. Neither
    route caught that, so an operator who had symlinked the directory got an
    opaque 500 from the panel tools -- indistinguishable from a bug in the
    gateway, and silent about the one thing they could act on. Note the actor
    differs from every other ``PanelError`` on these routes: a bad template id
    is the crew's to fix on its next cycle, this one is the operator's.
    """
    # Built from the data home rather than from ``override_templates_dir()``,
    # because that accessor is itself what refuses -- calling it here would raise
    # in the test instead of inside the route.
    over = data_home() / agent_panel.TEMPLATES_DIRNAME
    elsewhere = data_home() / "somewhere-else"
    elsewhere.mkdir(parents=True, exist_ok=True)
    if over.exists() or over.is_symlink():
        shutil.rmtree(over, ignore_errors=True)
        over.unlink(missing_ok=True)
    over.symlink_to(elsewhere, target_is_directory=True)

    async with _client() as c:
        listing = await c.get(
            "/api/agent-panel/templates", headers={"X-Session-Key": "dashboard:chat-1"}
        )
        assert (
            listing.status == 400
        ), "a linked template dir surfaced as something other than a refusal"
        assert (await listing.json())["code"] == "panel_dir_is_a_symlink"

        # The publish path reaches the same accessor when the template is omitted.
        published = await c.post(
            "/api/agent-panel/publish",
            headers={"X-Session-Key": "dashboard:chat-1"},
            json={"data": {"cycle": 1}},
        )
        assert published.status == 400
        assert (await published.json())["code"] == "panel_dir_is_a_symlink"


def test_the_stub_matches_where_the_real_selection_lives():
    """The double must not be the reason a call site passes.

    ``get_agent_selection`` lives on the SessionManager, reached as
    ``DashboardState.sessions``. Written against ``state`` directly it raised
    ``AttributeError`` in production, was swallowed as "not bound to a crew", and
    would have refused EVERY publish -- while this file stayed green, because the
    stub had grown the method in the wrong place. So the stub is checked against
    the real class rather than trusted.
    """
    from kiro_crew.session import SessionManager

    assert hasattr(SessionManager, "get_agent_selection")
    assert hasattr(_State(CREW).sessions, "get_agent_selection")
    # And NOT on the state itself, which is what made the wrong receiver pass.
    assert not hasattr(_State(CREW), "get_agent_selection")
    # Same for the existence question that tells the two refusals apart.
    assert hasattr(SessionManager, "has_session")
    assert hasattr(_State(CREW).sessions, "has_session")
    assert not hasattr(_State(CREW), "has_session")


async def test_a_same_named_provider_template_cannot_publish_as_the_crew(vetted):
    """A NAME is not a crew binding, which is the whole of this refusal.

    ``get_slot().agent`` carries the same string whether the allocation selected
    the crew or the provider TEMPLATE of that name, so a session on the template
    would otherwise publish into -- and overwrite -- that crew's record, past the
    ownership check the store applies to everyone else. The allocation's own
    namespace is the only thing that tells the two apart.
    """
    async with _client(namespace="member") as owner:
        assert (
            await owner.post(
                "/api/agent-panel/publish",
                json={"data": {"cycle": 47}},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
        ).status == 200
    kept = _folded()
    assert kept is not None and kept["data"] == {"cycle": 47}

    async with _client(namespace="template") as impostor:
        resp = await impostor.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 999}},
            headers={"X-Session-Key": "dashboard:chat-2"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "no_crew"

    # The owner's record is untouched, which is the property that matters: a
    # refusal that still appended would satisfy the status assertion above.
    after = _folded()
    assert after is not None
    assert after["crew"] == CREW
    assert after["data"] == {"cycle": 47}


# ------------------------------------------------- through a real allocation


def _member_session_key(crew_name: str = CREW) -> str:
    """The key the SESSION REGISTRY holds a crew's DM session under.

    Production's own derivation rather than a string spelled a second way here,
    because a key spelled a second way is the entire bug this section guards.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    slug = members_mod.member_slug(crew_name)
    try:
        _slot, store = routes._member_thread_slot(KiroCrewConfig.load(), crew_name, slug)
    except Exception:
        store = ""
    return members_mod.member_thread_session_alias(slug, store)


def _real_allocation(crew_name: str = CREW, *, member: str | None = None):
    """A REAL ``SessionManager`` holding *crew_name*'s DM session under its real key.

    Every other test here drives the resolver through ``_Sessions``, whose
    ``get_agent_selection`` ignores the key it is handed. That blindness is what
    let the resolver ask the registry with a key the registry never holds and
    stay green, so the regression cannot be written against that stub: the
    assertion needed is about WHICH key reaches a live allocation, and only a
    real registry has an opinion about that.

    ``member`` is the allocation's ``capability_member`` -- the crew binding
    itself. Empty models the other cause the issue names: a session created with
    a blank ``crew_agent``, which is a live allocation that genuinely selected a
    provider template.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session import SessionManager, _Session

    sessions = SessionManager(KiroCrewConfig())
    session = _Session(provider=SimpleNamespace(session_id=_unit_for(crew_name)), agent=crew_name)
    session.capability_member = crew_name if member is None else member
    sessions._sessions[_member_session_key(crew_name)] = session
    return sessions


def test_the_registry_key_is_the_slot_key_behind_a_transport_prefix():
    """The two keyspaces, pinned, so a future reader cannot merge them back.

    ``_normalize_slot_key`` STRIPS the prefix, so its output addresses the slot
    layer and nothing else. Handing it to the registry is a guaranteed miss, and
    a guaranteed miss on the only session this tool is mounted on refused every
    member.
    """
    from kiro_crew.dashboard.state import _normalize_slot_key

    registry_key = _member_session_key()
    slot_key = _crew_slot(CREW)
    assert registry_key == f"dashboard:{slot_key}"
    assert _normalize_slot_key(registry_key) == slot_key
    assert _normalize_slot_key(registry_key) != registry_key


async def test_a_real_member_allocation_can_publish(vetted):
    """The accepting path against a real allocation, which nothing covered.

    This is the issue's own case: a member's DM session, the one session the
    panel tool is ever mounted on, publishing with valid arguments. It was
    refused ``no_crew`` for every member because the resolver looked the caller
    up by slot key in a registry keyed by session key.
    """
    sessions = _real_allocation()
    async with _client(CREW, sessions=sessions) as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}, "title": "fleet"},
            headers={"X-Session-Key": _member_session_key()},
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["ok"] is True
    record = _folded()
    assert record is not None
    assert record["crew"] == CREW
    assert record["data"] == {"cycle": 47}


def test_every_refusal_in_the_crew_resolver_audits_its_denial():
    """No denial may return without a denied SEL event, on any exit.

    ``_recognize_session`` writes an ``outcome="allowed"`` event before this
    resolver reaches its own checks, so a refusal that returns without its own
    event leaves the audit trail ending on the ALLOW -- the record says the caller
    was let through while the HTTP response is the only trace that it was not.

    Asserted structurally over the function's AST rather than by exercising each
    branch: a per-branch test proves only the branches someone remembered to
    write, and this finding was exactly a branch nobody had. Every ``return None,
    web.json_response(...)`` in the resolver must be preceded, within its own
    block, by a ``log_api_access`` call.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(routes._resolve_publishing_crew)))

    def audits(body: list[ast.stmt]) -> list[str]:
        """Names of refusal statements in *body* that no preceding call audits."""
        unaudited: list[str] = []
        seen_audit = False
        for node in body:
            src = ast.dump(node)
            if "log_api_access" in src:
                seen_audit = True
            returns_refusal = isinstance(node, ast.Return) and "json_response" in src
            if returns_refusal and not seen_audit:
                unaudited.append(ast.dump(node)[:80])
            for field in ("body", "orelse", "finalbody", "handlers"):
                inner = getattr(node, field, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    unaudited.extend(audits(inner))
                elif isinstance(inner, list):
                    for handler in inner:
                        unaudited.extend(audits(getattr(handler, "body", [])))
        return unaudited

    refusals = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and "json_response" in ast.dump(node)
    )
    # Every refusal this resolver can return: internal secret, crew_panel off,
    # restricted session, no dashboard slot, unresolved session, no crew, bad slug.
    assert refusals == 7, refusals
    assert audits(tree.body[0].body) == []


async def test_the_unresolved_session_denial_is_audited(vetted, monkeypatch):
    """The finding's own case, exercised rather than only asserted structurally."""
    events: list[dict[str, Any]] = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    monkeypatch.setattr(routes, "sel", lambda: _Sel())
    sessions = _real_allocation()
    async with _client(CREW, sessions=sessions) as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 51}},
            headers={"X-Session-Key": "dashboard:chat-not-a-member"},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "session_not_resolved"
    denied = [e for e in events if e.get("outcome") == "denied"]
    assert denied, events
    assert denied[-1]["error"] == "caller's allocation could not be resolved"


async def test_a_bare_slot_key_is_refused_rather_than_re_prefixed(vetted):
    """A bare slot name resolves to nothing, and that refusal is deliberate.

    Every identity source the strict gate accepts yields the full session key,
    and it requires its caller to send back the key it returned, so no caller of
    this route presents a bare name. Re-adding the prefix to rescue one would
    hide the anomaly of the gate having returned something unexpected -- which is
    the very thing the separated refusal exists to surface.
    """
    sessions = _real_allocation()
    async with _client(CREW, sessions=sessions) as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 48}},
            headers={"X-Session-Key": _crew_slot(CREW)},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "session_not_resolved"
    assert _folded() is None


async def test_a_real_allocation_that_selected_a_template_is_refused_no_crew(vetted):
    """The other cause the issue names, and it keeps the ``no_crew`` message.

    A live allocation whose ``capability_member`` is empty selected the provider
    template. That IS an absent crew binding, so this refusal is the accurate
    one -- and it stays distinguishable from the key that reached no allocation
    at all.
    """
    sessions = _real_allocation(member="")
    async with _client(CREW, sessions=sessions) as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 49}},
            headers={"X-Session-Key": _member_session_key()},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "no_crew"
    assert _folded() is None


async def test_a_key_the_registry_does_not_hold_is_refused_as_unresolved(vetted):
    """A live member allocation exists, but not under the key the caller presents.

    Reported as an unresolved session rather than a missing crew binding, which
    is the difference an operator needs: nothing about this caller says it has no
    crew.
    """
    sessions = _real_allocation()
    async with _client(CREW, sessions=sessions) as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 50}},
            headers={"X-Session-Key": "dashboard:chat-not-a-member"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "session_not_resolved"
    assert _folded() is None


async def test_a_degraded_roster_refuses_takeover_rather_than_granting_it(vetted, monkeypatch):
    """ "Cannot read the roster" must not answer "the owner is gone".

    The takeover path exists so a renamed or deleted crew does not hold a slug
    forever, and it asks the config roster whether the recorded owner is still
    live. ``KiroCrewConfig.load()`` returns a defaults-only config when the file
    is unreadable, so an empty enumeration means either "nobody holds this slug"
    or "we could not read which crews exist" -- and reading the second as the
    first hands a colliding publish a live crew's record.
    """
    async with _client() as owner:
        assert (
            await owner.post(
                "/api/agent-panel/publish",
                json={"data": {"cycle": 47}},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
        ).status == 200

    real_load = routes.KiroCrewConfig.load

    def _degraded_load(*a, **kw):
        cfg = real_load(*a, **kw)
        object.__setattr__(cfg, "_degraded_sections", frozenset({"whole-config"}))
        return cfg

    monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(_degraded_load))

    other = CREW.upper()
    assert members_mod.slug_for_name(other) == SLUG, "fixture no longer collides"
    async with _client(other) as colliding:
        resp = await colliding.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 999}},
            headers={"X-Session-Key": "dashboard:chat-3"},
        )
        assert resp.status == 400, await resp.text()

    kept = agent_panel.read(SLUG)
    assert kept is not None
    assert kept["crew"] == CREW
    assert kept["data"] == {"cycle": 47}


async def test_a_free_form_name_can_read_its_panel(vetted):
    name = "dr. eggbot"
    slug = members_mod.slug_for_name(name)
    async with _client(name) as owner:
        response = await owner.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert response.status == 200, await response.text()
        read_response = await owner.get(f"/api/members/{slug}/panel", params={"member": name})
        assert read_response.status == 200
        assert (await read_response.json())["panel"]["crew"] == name


async def test_an_invalid_legacy_name_still_holds_its_panel(vetted, monkeypatch):
    legacy_name = "Cafe\u0301"
    legacy_slug = members_mod.slug_for_name(legacy_name)
    async with _client(legacy_name) as owner:
        response = await owner.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert response.status == 200, await response.text()
        read_response = await owner.get(
            f"/api/members/{legacy_slug}/panel", params={"member": legacy_name}
        )
        assert read_response.status == 200
        assert (await read_response.json())["panel"]["data"] == {"cycle": 47}
    kept = agent_panel.read(legacy_slug)
    assert kept is not None
    assert kept["crew_key"] == agent_panel.crew_key(legacy_name)
    assert kept["data"] == {"cycle": 47}

    real_load = routes.KiroCrewConfig.load

    def _roster_with_the_legacy_crew(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg.agents.clear()
        cfg.agents[legacy_name] = SimpleNamespace(name=legacy_name)
        return cfg

    monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(_roster_with_the_legacy_crew))

    colliding = "cafe"
    assert members_mod.slug_for_name(colliding) == legacy_slug, "fixture no longer collides"
    async with _client(colliding) as impostor:
        response = await impostor.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 999}},
            headers={"X-Session-Key": "dashboard:chat-2"},
        )
        assert response.status == 400, await response.text()

    after = agent_panel.read(legacy_slug)
    assert after is not None
    assert after["crew_key"] == agent_panel.crew_key(legacy_name)
    assert after["data"] == {"cycle": 47}


async def test_a_v2_member_round_trips_through_the_resolving_slot(vetted):
    """The publish slot and the read slot must agree on the NON-FALLBACK branch.

    ``_member_thread_slot`` returns ``member_slot_key(slug, store)`` for a member
    with a V2 private store and ``member_slot_key(slug)`` for everyone else. Those
    are different keys, so a test that only ever exercises the fallback proves
    nothing about the crew this feature is for: the fold would miss, and the drawer
    would show "never published" with no error at all.

    Here the member really has a V2 store, so the slot carries it, and the panel is
    published and read back through the routes rather than through the fold.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import config_dir

    home = config_dir()
    write_member_home(home, CREW)
    cfg = KiroCrewConfig.load()

    # The premise, asserted against the resolver rather than restated: the slot must
    # carry the store, which is what makes this the branch the fallback is not.
    slot, store = routes._member_thread_slot(cfg, CREW, SLUG)
    assert store, "the fixture did not give the member a V2 store"
    assert slot == members_mod.member_slot_key(SLUG, store)
    assert slot != members_mod.member_slot_key(SLUG), "this is still the fallback key"

    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}, "title": "v2"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()

        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert body["panel"] is not None, "the resolving slot lost the panel"
        assert body["panel"]["data"] == {"cycle": 47}
        assert body["html"]


async def test_a_publish_whose_append_is_skipped_is_still_what_a_reader_gets(vetted):
    """An accepted publish must never be shadowed by an older logged one.

    The append is best-effort, so a publish can land in the file while its entry does
    not: the crew log switches off between cycles, or the entry exceeds the log's
    whole-LINE ceiling while its data is under the store's own cap. Serving the fold
    whenever it holds any panel pinned the drawer to the last LOGGED cycle and kept
    serving it, while the route answered the crew ok -- and a viewer cannot tell a
    stale dashboard from a current one.

    Here cycle 1 is logged and cycle 2 is not, so the two records disagree and the
    newer one has to win.
    """
    async with _client() as c:
        # Two logged cycles, so the fold holds a history row as well as a panel.
        for cycle, title in ((1, "logged-one"), (2, "logged-two")):
            assert (
                await c.post(
                    "/api/agent-panel/publish",
                    json={"data": {"cycle": cycle}, "title": title},
                    headers={"X-Session-Key": "dashboard:chat-1"},
                )
            ).status == 200
        assert _folded()["data"] == {"cycle": 2}, "cycle 2 should be in the fold"
        assert _folded()["history"], "two publishes should leave a history row"

        # A third cycle with the log off: the file advances, the fold cannot.
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("KIROCREW_CREW_LOG", "0")
            crew_log_emit.reset_caches()
            resp = await c.post(
                "/api/agent-panel/publish",
                json={"data": {"cycle": 3}, "title": "unlogged"},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 200, await resp.text()
        crew_log_emit.reset_caches()

        # The premise: the two records really do disagree.
        assert agent_panel.read(SLUG)["data"] == {"cycle": 3}, "the file must hold cycle 3"
        assert _folded()["data"] == {"cycle": 2}, "the fold must still hold cycle 2"

        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert body["panel"]["data"] == {"cycle": 3}, "the drawer served the stale logged panel"
        assert body["panel"]["title"] == "unlogged"

    # The fold's history rides along with the file's panel, so the crew's past is not
    # lost just because its newest cycle went unlogged. Asserted on the record rather
    # than the response, because the route does not surface ``history`` yet.
    from kiro_crew.config.loader import KiroCrewConfig

    crew_log_projection.forget_slot_folds()
    record = off_loop(
        routes._panel_record,
        routes._panel_slot(KiroCrewConfig.load(), CREW, SLUG),
        SLUG,
        agent_panel.crew_key(CREW),
    )
    assert record["data"] == {"cycle": 3}, "the record should carry the file's newer panel"
    assert record["history"], "the fold's history should survive the file deciding the panel"
    assert record["history"][-1]["title"] == "logged-one"


async def test_a_colliding_crews_newer_file_is_not_served_to_this_crew(vetted):
    """The file is keyed by SLUG alone, so it may hold the OTHER crew's panel.

    Comparing stamps without checking ownership would let a colliding crew's newer
    publish displace this crew's reading, which is the harm the per-owner fold exists
    to prevent.
    """
    async with _client() as c:
        assert (
            await c.post(
                "/api/agent-panel/publish",
                json={"data": {"cycle": 47}, "title": "mine"},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
        ).status == 200

    # A newer file record owned by a DIFFERENT crew on the same slug.
    other = CREW.upper()
    assert members_mod.slug_for_name(other) == SLUG, "fixture no longer collides"
    path = agent_panel.panel_dir() / f"{SLUG}.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "template": "default",
                "title": "theirs",
                "crew": other,
                "crew_key": agent_panel.crew_key(other),
                "data": {"cycle": 999},
                "published_at": "2099-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    async with _client() as c:
        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert body["panel"]["data"] == {"cycle": 47}, "another crew's file was served"
        assert body["panel"]["title"] == "mine"


async def test_a_colliding_crew_cannot_read_the_other_crews_panel(vetted):
    """The READ half of the ownership guard.

    The fold keeps a record per ownership digest, so one crew cannot displace the
    other's. But with the read keyed on the slug alone, the crew that did NOT
    publish still saw the publisher's dashboard rendered in its own drawer -- the
    guard was half-applied. Both halves check the same stored claim.

    ``Oncall`` and ``oncall`` slugify to one slug, which is the whole premise.
    """
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        # The owner sees it.
        owner_body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert owner_body["panel"]["crew"] == CREW
        assert owner_body["html"]

        # The colliding crew, same slug, different exact name, sees an empty state --
        # not the other crew's document, and not an error naming them either.
        other = CREW.upper()
        assert members_mod.slug_for_name(other) == SLUG, "fixture no longer collides"
        resp = await c.get(f"/api/members/{SLUG}/panel?member={other}")
        assert resp.status == 200
        body = await resp.json()
        assert body["panel"] is None
        assert body["html"] is None


async def test_the_read_requires_the_exact_crew_name(vetted):
    """Required, not optional, so a mixed read is impossible by construction.

    An optional parameter puts the guard behind a caller obligation, and the one
    caller that forgets is the bug. Mirrors ``/activity``, which made the same
    parameter required for the same reason.
    """
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        resp = await c.get(f"/api/members/{SLUG}/panel")
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_member"


async def test_a_hostile_member_name_is_refused_on_the_read(vetted):
    """The name is validated, not just compared."""
    async with _client() as c:
        for hostile in (
            "a\nb",
            " leading",
            "hidden\u200bname",
            "x" * (members_mod.MEMBER_NAME_MAX_CHARS + 1),
        ):
            resp = await c.get(
                f"/api/members/{SLUG}/panel?member={quote(hostile, safe='')}",
            )
            assert resp.status == 400, f"{hostile!r} was accepted"


async def test_a_credential_shaped_legacy_crew_can_read_its_redacted_panel(vetted):
    shaped = "".join(["AKIA", "IOSFODNN7", "EXAMPLE"])
    assert agent_panel._scrub(shaped) != shaped, "fixture is no longer redacted"

    slug = members_mod.slug_for_name(shaped)
    async with _client(agent=shaped) as client:
        publish_response = await client.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert publish_response.status == 200, await publish_response.text()

        read_response = await client.get(f"/api/members/{slug}/panel", params={"member": shaped})
        assert read_response.status == 200
        body = await read_response.json()
        assert body["panel"]["data"] == {"cycle": 47}
        assert shaped not in json.dumps(body)
        assert "REDACTED" in body["panel"]["crew"]

    stored = agent_panel.read(slug)
    assert stored is not None
    assert stored["crew_key"] == agent_panel.crew_key(shaped)


async def test_a_linked_record_is_a_coded_refusal_not_a_500(vetted):
    """The store refuses to write through an alias; the route must say so with a code.

    A symlink or junction standing where the record belongs is how a write reaches
    an inode outside the fenced directory, so :func:`agent_panel.panel_path` raises
    rather than following it. That refusal is neither the crew's fault nor the
    drawer's, and an uncaught ``CrewSlugError`` reaches the caller as an opaque 500
    -- telling the one party who can repair it (the operator, who removes the link)
    nothing about what happened.

    The READ path needs no such catch: ``read`` treats any ``ValueError`` as "no
    panel published", so a linked record renders the empty state.
    """
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 1}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        # Stand a symlink where the record belongs, pointing at a sibling.
        path = agent_panel.panel_path(SLUG)
        elsewhere = path.with_suffix(".elsewhere")
        elsewhere.write_text("{}", encoding="utf-8")
        path.unlink()
        os.symlink(elsewhere, path)

        r = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 2}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert r.status == 400, f"a linked record surfaced as {r.status}"
        assert (await r.json())["code"] == "panel_record_is_a_symlink"

        # The READ still serves the panel, from the crew log rather than through the
        # link: the fold is preferred, and the first publish appended an entry. The
        # link is never followed either way -- ``panel_path`` refuses it, so the
        # fallback that would have read it answers nothing. A tampered file
        # therefore costs the crew nothing: its own last publish still renders.
        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert body["panel"]["data"] == {"cycle": 1}, "the fold should still hold cycle 1"
        assert body["html"]


async def test_a_linked_legacy_record_is_never_followed_on_the_read(vetted):
    """A LINKED legacy record reads as the empty state, not as an error.

    A symlink or junction standing where the record belongs is how a read reaches
    an inode outside the fenced directory, so :func:`agent_panel.panel_path` raises
    rather than following it. ``read`` treats any ``ValueError`` as "no panel
    published", so the drawer renders the empty state and the link's bytes are never
    served as somebody's panel.

    The WRITE half of this guard is gone with the writer: the record is a crew log
    entry now, so no publish resolves this path and none can be aimed through a
    link. The read is the whole remaining surface, and it is still guarded.
    """
    async with _client() as c:
        # Stand a symlink where the legacy record belongs, pointing at a sibling.
        # Written directly because nothing writes this file any more.
        path = agent_panel.panel_dir() / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        elsewhere = path.with_suffix(".elsewhere")
        elsewhere.write_text(
            json.dumps({"template": "default", "data": {"planted": 1}, "crew_key": ""}),
            encoding="utf-8",
        )
        os.symlink(elsewhere, path)

        # The store refuses it rather than following it.
        assert agent_panel.read(SLUG) is None, "a linked record was followed"

        # And the route stays an empty state rather than an error.
        resp = await c.get(f"/api/members/{SLUG}/panel?member={CREW}")
        assert resp.status == 200, f"a linked record surfaced as {resp.status}"
        body = await resp.json()
        assert body["panel"] is None and body["html"] is None


async def test_an_unowned_record_is_not_served_to_anyone(vetted):
    """The read guard must fail CLOSED when ownership cannot be established.

    Comparing only when the stored ``crew_key`` is truthy would let a record with
    an empty key skip the check and reach whoever asked -- and with the query
    parameter the only thing naming the crew, that is any caller. No publish can
    create such a record, which leaves a forged one as the only source, and a
    forgery is precisely what must not render in someone's drawer.

    Planted in the legacy file rather than through a publish: the fold is left
    empty on purpose so the read falls back to the file, which is the one surface
    an outside write can still reach.
    """
    async with _client() as c:
        # Written directly, as a write outside the API would.
        path = agent_panel.panel_dir() / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "template": "default",
                    "title": "",
                    "crew": CREW,
                    "crew_key": "",
                    "data": {"planted": "by nobody"},
                }
            ),
            encoding="utf-8",
        )
        # The store itself returns it -- the guard under test is the route's.
        assert agent_panel.read(SLUG) is not None, "the fixture planted nothing readable"

        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert body["panel"] is None, "an unowned record was served"
        assert body["html"] is None
        assert "planted" not in json.dumps(body)


async def test_publishing_pushes_a_refresh_to_open_drawers(vetted):
    """The drawer showed its first read forever without this.

    The query client sets ``staleTime: Infinity`` because freshness is driven by
    WebSocket push, and nothing pushed for a panel -- so a crew publishing on an
    unattended loop was invisible after the drawer's first render, which is the one
    thing the feature exists to do.
    """
    app = _mounted()
    async with TestClient(TestServer(app)) as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        sent = app["state"].broadcasts
        assert ("panel_published", {"slug": SLUG}) in sent, f"no refresh pushed: {sent}"


async def test_the_refresh_frame_carries_no_ownership_digest(vetted):
    """The frame says "re-read this slug" and nothing more.

    The digest is a hash of the exact crew name, which may itself be
    credential-shaped -- a sibling test pins that it never reaches a client, and a
    broadcast goes to EVERY connected client, not just this crew's viewer.
    """
    app = _mounted()
    async with TestClient(TestServer(app)) as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        blob = json.dumps(app["state"].broadcasts)
        assert agent_panel.crew_key(CREW) not in blob
        assert "crew_key" not in blob


async def test_the_ownership_digest_never_reaches_the_client(vetted):
    """It is derived from a name that may itself be credential-shaped."""
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert "crew_key" not in json.dumps(body)
        assert agent_panel.crew_key(CREW) not in json.dumps(body)


async def test_an_app_identified_internal_caller_cannot_publish(vetted):
    """The secret gate does NOT imply "not an app".

    ``token_auth`` sets ``internal_auth`` and then derives ``request["app"]`` in the
    SAME branch, so one internal caller can carry both. An app-owned agent granted
    the panel tools, whose slot's ``agent`` happens to name a crew, therefore
    satisfied ``internal_secret_required`` and published as that crew -- replacing a
    crew's own dashboard with app-authored content. The read route denied app
    callers from the start; this is the write half of the same guard.
    """

    @web.middleware
    async def _as_app(request: web.Request, handler):
        request["app"] = "some-app"
        return await handler(request)

    # Inserted BEFORE the server starts, for the reason the sibling test records:
    # aiohttp freezes the app on startup, so a late append is a no-op and this would
    # pass without ever presenting an app identity.
    app = _mounted()
    app.middlewares.insert(0, _as_app)
    async with TestClient(TestServer(app)) as app_client:
        resp = await app_client.post(
            "/api/agent-panel/publish",
            json={"data": {"planted": "by an app"}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 404, f"an app published as a crew: {resp.status}"
        assert agent_panel.read(SLUG) is None, "an app's data landed in a crew's panel"


async def test_an_app_token_caller_is_denied_the_panel(vetted):
    """``/api/members/{slug}/panel`` is a CHILD of ``/api/members``.

    An app token scoped to the parent is admitted to this route by prefix, so the
    denial has to be explicit. Apps are isolated from member surfaces generally --
    ``/api/members``, ``.../thread`` and ``.../activity`` all 404 for an app token --
    and a panel is a crew's published state plus a rendered document, squarely
    inside what that isolation withholds.

    404 rather than 403, matching the sibling: a distinct status would confirm the
    surface exists to a caller that may not know about it.
    """
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )

        @web.middleware
        async def _as_app(request: web.Request, handler):
            request["app"] = "some-app"
            return await handler(request)

        # Middleware has to be inserted BEFORE the server starts: aiohttp freezes the
        # app on startup, so appending to a started client's middlewares is a no-op
        # and the test would pass without ever presenting an app token.
        app = _mounted()
        app.middlewares.insert(0, _as_app)
        # `async with` for the same reason `_client` uses it: a started client owns
        # a session and a socket, and this path builds its own rather than going
        # through the helper, so it needs its own close.
        async with TestClient(TestServer(app)) as app_client:
            resp = await app_client.get(f"/api/members/{SLUG}/panel?member={CREW}")
            assert resp.status == 404, "an app token reached the panel read"
            body = await resp.text()
            assert "cycle" not in body, "panel data leaked to an app-token caller"


async def test_no_route_here_writes_from_the_browser_surface():
    """Everything under /api/members here is a read; writes are MCP-only."""
    member_routes = [
        r
        for r in _mounted().router.routes()
        if str(r.resource.canonical).startswith("/api/members")
    ]
    assert member_routes
    assert {r.method for r in member_routes} <= {"GET", "HEAD"}


async def test_the_gateway_registers_exactly_these_paths_without_importing_us():
    """The boot path binds these routes DEFERRED, so the paths are written twice.

    ``server._register_mcp_routes`` cannot call ``register_agent_panel_routes``:
    that would import this optional subsystem on every gateway launch before the
    socket binds, which the boot-path rule forbids. It therefore restates each
    path against ``server._deferred``, and a restated path is a path
    that can drift -- a route renamed here and not there would 404 in the gateway
    while every test in this file passed.

    Compares the two SETS rather than looking for known strings, so a route added
    to either side has to be added to both.
    """
    import inspect

    from kiro_crew.dashboard import server

    ours = {str(r.resource.canonical) for r in _mounted().router.routes()}
    boot_src = inspect.getsource(server._register_mcp_routes)
    # One spelling, normalised, so black's line wrapping cannot change the answer:
    # the binder call may sit on the same line as the path or on the next one.
    flat = " ".join(boot_src.split())
    deferred = {
        path
        for path in ours
        # The deferred binder is what proves the gateway serves it without an eager
        # import; a path bound any other way would not match.
        if f'"{path}", _deferred("agent_panel",' in flat
    }
    missing = ours - deferred
    assert not missing, (
        f"{sorted(missing)} are registered by this module but the gateway boot path "
        "does not bind them through the deferred binder, so they are either "
        "unserved or imported eagerly"
    )


# ------------------------------------------------- the publish ordering contract
#
# ``_panel_record`` prefers the FILE over the fold, and its docstring states the
# one thing that makes that selection safe: the publish route writes the file
# BEFORE it appends the history entry, and returns without appending if that write
# fails. So every publish is in the file while only the ones whose append landed
# are in the fold, and the file can never be the staler of the two. The five pins
# below are that sentence made executable -- the order, the call-level fail-closed
# branch, its end state on the fold, every refusal branch, and the size of the
# caller population it holds for.


def _publish_handler_ast():
    """The publish route's function definition, parsed from its own source file.

    Read off disk rather than through ``inspect.getsource`` so the enclosing
    module's import graph is irrelevant to a structural assertion about it.
    """
    import ast
    from pathlib import Path

    import kiro_crew

    path = Path(kiro_crew.__file__).parent / "dashboard" / "handlers" / "agent_panel.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_agent_panel_publish":
            return ast, node
    raise AssertionError("api_agent_panel_publish is not defined in the handler module")


async def test_the_file_is_written_before_the_history_is_appended(vetted, monkeypatch):
    """The file write completes BEFORE the history append begins.

    This is the order ``_panel_record``'s file-wins selection rests on. Reversed,
    the fold could hold a cycle the file does not, and the drawer would serve a
    panel older than the one the publish just wrote while reporting success -- a
    viewer cannot tell a stale dashboard from a current one.

    Both spies delegate to the real callables, so the publish under test really
    writes and really appends; what is recorded is only WHEN each happened.
    """
    order: list[str] = []
    real_publish = agent_panel.publish
    real_append = crew_log_emit.on_panel_published

    def _spy_publish(*a: Any, **kw: Any):
        record = real_publish(*a, **kw)
        # Recorded AFTER the real write returns, so the marker means "the file is
        # on disk" rather than "the write was attempted".
        order.append("file")
        return record

    def _spy_append(*a: Any, **kw: Any):
        order.append("append")
        return real_append(*a, **kw)

    monkeypatch.setattr(agent_panel, "publish", _spy_publish)
    monkeypatch.setattr(crew_log_emit, "on_panel_published", _spy_append)

    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}, "title": "fleet"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()

    assert order == ["file", "append"], (
        f"the publish route ran {order}; the file must be written before the history "
        "is appended, because _panel_record prefers the file on the strength of that order"
    )


async def test_a_failed_file_write_appends_no_history_row(vetted, monkeypatch):
    """A file write that fails never reaches the history append at all.

    The CALL-level half of the guarantee: the emitter is not invoked. Its
    end-state half -- that the fold holds no record either -- is measured by
    ``test_the_fold_never_holds_a_publish_the_file_lacks``, which runs the real
    emitter and so can observe a fold that a spy would leave empty whatever the
    route did.
    """
    appended: list[tuple[Any, ...]] = []
    real_append = crew_log_emit.on_panel_published

    def _refuse_write(*_a: Any, **_kw: Any):
        raise OSError("the record could not be written")

    def _spy_append(*a: Any, **kw: Any) -> bool:
        appended.append(a)
        # Delegates rather than answering True, so the spy adds an observation
        # instead of replacing the emitter's effect. A recording-only spy makes
        # every downstream read of the fold answer empty for its own reason.
        return real_append(*a, **kw)

    monkeypatch.setattr(agent_panel, "publish", _refuse_write)
    monkeypatch.setattr(crew_log_emit, "on_panel_published", _spy_append)

    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}, "title": "fleet"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 503, await resp.text()
        assert (await resp.json())["code"] == "panel_write_failed"

    assert appended == [], "a publish whose file write failed still appended a history row"


async def test_the_fold_never_holds_a_publish_the_file_lacks(vetted, monkeypatch):
    """END STATE: the fold never gets AHEAD of the file.

    The property ``_panel_record``'s file-wins branch is written to be free of, and
    it is asserted on the two records themselves rather than on a call count, so a
    writer that reaches the log by any route -- not only through the emitter this
    file spies on elsewhere -- is still caught.

    A first publish is allowed to land, so both halves compare VALUES. Asserting
    absence after a single failed publish would prove nothing on either side:
    ``read`` answers ``None`` for a malformed file as readily as for a missing one,
    so it cannot see a partial record, and an empty fold is what an empty log looks
    like anyway.

    The second publish is refused at the WRITE SEAM, so the real ``publish`` runs
    its lock, its ownership check and its write attempt. Refusing ``publish``
    itself would exercise none of that -- it is the record's only writer, so the
    file would be untouched by construction rather than by the code under test.
    """
    async with _client() as c:
        first = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 1}, "title": "landed"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert first.status == 200, await first.text()

    def _refuse_write(*_a: Any, **_kw: Any):
        raise OSError("the record could not be written")

    monkeypatch.setattr(agent_panel, "atomic_write", _refuse_write)

    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 2}, "title": "refused"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 503, await resp.text()

    stored = agent_panel.read(SLUG)
    assert stored is not None, "the first publish left no file to compare against"
    assert stored["data"] == {"cycle": 1}, "the refused write changed the stored record"

    folded = _folded()
    assert folded is not None, "the first publish recorded no history"
    assert folded["data"] == {"cycle": 1}, "the fold holds a publish the file never received"
    assert folded["publishes"] == 1, "the fold counted a publish the file never received"


async def test_every_refused_publish_returns_before_the_history_append():
    """EVERY handler of the publish write cannot fall through to the append.

    Enumerated from the route's own source rather than listed here, because the
    recurrence is a refusal branch nobody has written yet: a handler added below
    the existing ones, logging and then falling through, would append a history
    row for a publish that never reached the file. Asserting over the handlers
    that ARE there covers the next one by construction.

    A handler ending in ``raise`` satisfies this as squarely as one ending in
    ``return``: neither reaches the append.
    """
    ast, handler = _publish_handler_ast()

    blocks = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Try)
        and any(
            isinstance(inner, ast.Attribute) and inner.attr == "publish"
            for stmt in node.body
            for inner in ast.walk(stmt)
        )
    ]
    assert len(blocks) == 1, (
        f"found {len(blocks)} try blocks calling agent_panel.publish; the pin below "
        "reads the one that guards the file write"
    )

    fell_through = [
        ast.unparse(h.type) if h.type is not None else "bare except"
        for h in blocks[0].handlers
        if not isinstance(h.body[-1], (ast.Return, ast.Raise))
    ]
    assert not fell_through, (
        f"{fell_through} handle a failed panel write without returning or raising, so a "
        "publish the file never received can still reach the history append"
    )
    assert blocks[0].handlers, "the publish write is unguarded, so a failure cannot be refused"


# On this test alone, not on ``pytestmark``: it is the only item here that reads
# ``test/source_corpus.py``'s shared text cache, and grouping the module's other
# tests with it would cost them their parallelism to buy a de-duplication that
# cannot happen -- a single corpus reader lands on one worker either way.
@pytest.mark.xdist_group(name="tree_scan_test_agent_panel_routes")
async def test_the_history_append_has_exactly_one_call_site():
    """``on_panel_published`` is called from exactly one place in the package.

    The emitter is public and applies no ordering rule of its own, so the write
    order lives entirely in its caller. One caller is what makes the pins above a
    statement about the whole package rather than about one route.

    This cannot check a NEW caller's ordering -- it makes one impossible to add
    silently. A second call site fails here, and extending the order pin to cover
    it is what clears the failure.
    """
    import ast

    import source_corpus

    root = source_corpus.src_root()
    # Only files whose TEXT holds the identifier are parsed. `ast` cannot produce a
    # call to a name the source does not contain, so the rest of the package cannot
    # contribute a site, and parsing it all is the cost `source_corpus` exists to
    # remove -- its corpus read is cached once per module rather than per test.
    candidates = list(source_corpus.parsed_candidates(require_all=("on_panel_published",)))
    sites: list[str] = []
    for path, _text, tree in candidates:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                name = ""
            if name == "on_panel_published":
                # The MODULE, not the line: a line number turns every unrelated edit
                # above the call into a failure of this pin. The COUNT is kept beside
                # it so a second caller inside this same module is caught too.
                #
                # ``as_posix``, not ``str``: on a Windows shard the native rendering
                # is backslash-separated and would never equal the literal below.
                sites.append(path.relative_to(root).as_posix())

    # Said out loud so a reader sees the scan reached something. A filter that matched
    # nothing would leave ``sites`` empty, and an empty list is also what a package
    # with no emitter at all looks like -- this pin must not read those two alike.
    assert candidates, "the corpus filter matched no file, so this pin measured nothing"

    assert sites == ["dashboard/handlers/agent_panel.py"], (
        f"on_panel_published is called from {sites}; the publish order is the caller's "
        "to keep, so every call site needs the ordering pins in this file extended to it"
    )


async def test_the_drawer_read_reports_the_templates_docked_opt_in(vetted):
    """A template that carries the docked marker is reported with its height, and
    one that does not reports ``None`` -- which keeps the drawer's zero-mint
    native summary."""
    over = agent_panel.override_templates_dir()
    over.mkdir(parents=True, exist_ok=True)
    (over / "compact.html").write_text(
        "<!--kirocrew:docked height=180-->" + agent_panel.DATA_MARKER, encoding="utf-8"
    )
    async with _client() as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        plain = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert plain["panel"]["docked_height"] is None

        await c.post(
            "/api/agent-panel/publish",
            json={"template": "compact", "data": {"cycle": 48}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        opted = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert opted["panel"]["docked_height"] == 180


# ----------------------------------- the dynamic dashboard's agentic surface


#: A template with one field of each SOURCE. Both are needed: the field list has
#: to say where every value comes from, and the write path has to refuse the one
#: the host fills.
DASHBOARD_MANIFEST: dict[str, Any] = {
    "id": "demo",
    "version": 2,
    "title": "Demo",
    "description": "A demo dashboard",
    "source": "builtin",
    "fields": {
        "open_items": {"type": "number", "source": {"agentic": True}},
        "entries": {"type": "number", "source": {"fold": "work", "path": "count"}},
    },
}

#: The page an unadopted crewmate is shown. Its one agentic field is an ARRAY because
#: the "needs you" answer is a list of lines, and the write path has to accept it AS a
#: list rather than stringify it.
DEFAULT_MANIFEST: dict[str, Any] = {
    "id": "the-default",
    "version": 1,
    "title": "The default",
    "description": "What a crewmate renders before it adopts anything",
    "source": "builtin",
    "fields": {
        "for_you": {"type": "array", "source": {"agentic": True}},
        "entries": {"type": "number", "source": {"fold": "work", "path": "count"}},
    },
}

FIELDS_PATH = "/api/agent-panel/dashboard/fields"
WRITE_PATH = "/api/agent-panel/dashboard/write"


def _adopt_dashboard(monkeypatch, *, state: str = "live", version: int = 4) -> None:
    """Present an adopted dashboard to the one function that reads the registry.

    Through the registry SEAM rather than by committing a real instance: what
    these routes need from the registry is a parsed manifest and a version, and
    driving the store's own adopt path would bind every case here to the
    catalogue's shipped templates.

    Both spellings are replaced because ``read_instance`` imports inside the
    call: once the real module is imported, ``from kiro_crew.dashboard_templates
    import instance`` reads the package attribute rather than ``sys.modules``.
    """
    record = SimpleNamespace(
        slug=SLUG,
        instance_version=version,
        state=state,
        manifest=dict(DASHBOARD_MANIFEST),
    )
    store = SimpleNamespace(read=lambda _slug: record, STATE_LIVE="live")
    monkeypatch.setitem(sys.modules, "kiro_crew.dashboard_templates.instance", store)
    import kiro_crew.dashboard_templates as templates_pkg

    monkeypatch.setattr(templates_pkg, "instance", store, raising=False)


def _default_dashboard(monkeypatch, *, ships_default: bool = True) -> None:
    """Present a crewmate that adopted NOTHING, beside a registry default to fall to.

    Through the same registry seam ``_adopt_dashboard`` uses, and for the same reason:
    the shipped built-in pages arrive in the stacked page change, so a case driven off
    the catalogue would pin this surface to a directory this change does not carry.
    What these routes need from the EMPTY state is that ``read`` answers a record in it
    and ``default_instance`` answers the page being shown, which is what this presents.

    ``ships_default=False`` presents the other half -- a registry carrying the loader
    and no template -- by answering ``None`` rather than raising, because that is what
    the real ``default_instance`` does when the id it resolves is absent.
    """
    empty = SimpleNamespace(slug=SLUG, instance_version=0, state="empty", manifest={})
    fallback = (
        SimpleNamespace(
            slug=SLUG,
            instance_version=0,
            state="empty",
            manifest=dict(DEFAULT_MANIFEST),
        )
        if ships_default
        else None
    )
    store = SimpleNamespace(
        read=lambda _slug: empty,
        default_instance=lambda _slug: fallback,
        STATE_LIVE="live",
        STATE_STALE="stale",
        STATE_EMPTY="empty",
    )
    monkeypatch.setitem(sys.modules, "kiro_crew.dashboard_templates.instance", store)
    import kiro_crew.dashboard_templates as templates_pkg

    monkeypatch.setattr(templates_pkg, "instance", store, raising=False)


async def test_the_dashboard_field_list_needs_the_internal_secret(vetted):
    """It reports a crewmate's own refused writes, which is state about that
    crewmate and nobody else's to read, so it sits behind the publish route's
    gate rather than beside the drawer's browser read."""
    async with _client(internal=False) as c:
        resp = await c.get(FIELDS_PATH, headers={"X-Session-Key": "dashboard:chat-1"})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "internal_secret_required"


async def test_a_dashboard_write_needs_the_internal_secret(vetted):
    """A write lands in the crewmate's own crew log, so a caller holding only a
    dashboard cookie must not reach it."""
    async with _client(internal=False) as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open_items", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "internal_secret_required"


async def test_the_dashboard_surface_inherits_the_no_slot_refusal(vetted):
    """The shared resolver's refusals reach both routes.

    A state with no allocation to resolve cannot say which crew is asking, and
    the field list is keyed by crew -- answering anyway would report one
    crewmate's mistakes to another.
    """
    async with _client(agent=None) as c:
        resp = await c.get(FIELDS_PATH, headers={"X-Session-Key": "dashboard:chat-1"})
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "no_dashboard_slot"


async def test_the_field_list_reports_where_every_value_comes_from(vetted, monkeypatch):
    """Every field, not only the writable ones, and each with its source.

    A crewmate that cannot see a fold-sourced field has no way to know the number
    is already recorded, and will either try to write it or duplicate it under an
    agentic name so the page draws the same quantity twice.
    """
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.get(FIELDS_PATH, headers={"X-Session-Key": "dashboard:chat-1"})
        assert resp.status == 200, await resp.text()
        body = await resp.json()

    assert body["template"] == {"id": "demo", "version": 2}
    assert body["instance_version"] == 4
    assert body["agentic"] == ["open_items"]
    rows = {row["field"]: row for row in body["fields"]}
    assert rows["open_items"]["source"] == "agentic"
    assert rows["entries"]["source"] == "fold"
    assert rows["entries"]["fold"] == "work"
    assert rows["entries"]["path"] == "count"


async def test_a_body_that_is_not_json_is_refused_before_the_manifest(vetted, monkeypatch):
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            data="not json",
            headers={"X-Session-Key": "dashboard:chat-1", "Content-Type": "application/json"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "invalid_json"


async def test_a_body_that_is_not_an_object_is_refused(vetted, monkeypatch):
    """A list parses as JSON and carries no field name, so it is a distinct
    refusal from unparseable bytes."""
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json=["open_items", 3],
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "invalid_body"


async def test_an_argument_the_schema_rejects_never_reaches_the_manifest(vetted, monkeypatch):
    """The schema's job is to reject an unexpected ARGUMENT; the manifest decides
    the value. A field name past the schema's length bound is the former, so it
    is refused with ``validation_error`` rather than grouped as a crewmate's
    field-name mistake.
    """
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "x" * 200, "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "validation_error"


async def test_a_field_the_template_does_not_declare_is_refused_with_the_valid_names(
    vetted, monkeypatch
):
    """The refusal is answerable on the next cycle because it carries the list."""
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
        assert body["code"] == "unknown_field"
        assert body["field"] == "open"
        assert "open_items" in body["error"]


async def test_a_host_filled_field_is_refused_rather_than_overwritten(vetted, monkeypatch):
    """A write into a fold-sourced cell would be replaced on the next fold, so
    reporting it as stored would promise a value that does not survive."""
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "entries", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "field_not_agentic"


async def test_a_value_of_the_wrong_declared_type_is_refused(vetted, monkeypatch):
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open_items", "value": "three"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "wrong_type"


async def test_a_write_is_still_refused_when_no_manifest_parses(vetted, monkeypatch):
    """``no_instance`` survives for the state it actually describes.

    ERROR is the state in which the stored manifest does not parse, so there is
    nothing to validate a field name against. Keeping this case is what stops the
    default fallback above from being read as "writes are never refused".
    """
    import kiro_crew.dashboard.handlers.agent_panel as panel

    monkeypatch.setattr(panel, "read_instance", lambda slug, member: None)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "for_you", "value": []},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "no_instance"


async def test_a_refusal_is_recorded_so_the_next_cycle_is_cheaper(vetted, monkeypatch):
    """The mistake book is written BEFORE the response.

    The refusal is already decided, so the append costs the caller nothing it was
    going to get -- and a crewmate that is refused and then stops has still
    taught its successor. The field list is where that lands, so the round trip
    is what proves it.
    """
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        refused = await c.post(
            WRITE_PATH,
            json={"field": "open", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert refused.status == 400, await refused.text()

        crew_log_projection.forget_slot_folds()
        listed = await c.get(FIELDS_PATH, headers={"X-Session-Key": "dashboard:chat-1"})
        assert listed.status == 200, await listed.text()
        book = (await listed.json())["mistakes"]

    assert book, "the refusal was not recorded where the next read finds it"
    assert any(row.get("field") == "open" for row in book), book


async def test_a_value_lands_and_an_open_dashboard_is_told(vetted, monkeypatch):
    """The fold's own event drives the frame's refill; this frame is for a client
    watching the panel surface, and it carries the SLUG only."""
    _adopt_dashboard(monkeypatch)
    app = _mounted()
    async with TestClient(TestServer(app)) as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open_items", "value": 7},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["ok"] is True
        assert body["written"] == {"field": "open_items", "type": "number"}
        assert body["corrected"] is False
        sent = app["state"].broadcasts
        assert ("dashboard_value_written", {"slug": SLUG}) in sent, f"nothing pushed: {sent}"
        assert agent_panel.crew_key(CREW) not in json.dumps(sent)


async def test_a_write_that_answers_a_recorded_mistake_reports_the_correction(vetted, monkeypatch):
    """The correction comes after the value lands, never before.

    A correction for a write that failed to append would tell the crewmate a
    wrong field name was fixed by one that was never stored.
    """
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        assert (
            await c.post(
                WRITE_PATH,
                json={"field": "open", "value": 3},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
        ).status == 400

        crew_log_projection.forget_slot_folds()
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open_items", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["corrected"] is True


async def test_a_write_is_refused_when_the_gateway_records_no_crew_log(vetted, monkeypatch):
    """The log is an agentic value's ONLY record -- no host Python computes one,
    so there is no file beside it. Reporting success would promise a cell that
    never fills, which is why this refuses rather than shrugging the way a panel
    publish does.
    """
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        crew_log_emit.reset_caches()
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open_items", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 503, await resp.text()
        assert (await resp.json())["code"] == "crew_log_off"
    crew_log_emit.reset_caches()


async def test_a_refusal_with_the_log_off_still_refuses_the_write(vetted, monkeypatch):
    """Recording the mistake is best-effort, unlike the value append.

    By the time it runs the caller is already being refused, so a log that is off
    costs the mistake book this row and nothing else. Failing here would turn
    "we could not remember your mistake" into "your write was not refused".
    """
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        crew_log_emit.reset_caches()
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "unknown_field"
    crew_log_emit.reset_caches()


async def test_a_refusal_whose_append_raises_still_refuses_the_write(vetted, monkeypatch):
    """The recorder never raises, so a damaged log cannot turn a refusal into a
    500 that tells the caller nothing about its field name."""
    _adopt_dashboard(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("the log is unwritable")

    monkeypatch.setattr(crew_log_emit, "on_dashboard_refused", _boom)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "unknown_field"


async def test_a_caller_with_no_crew_log_unit_is_told_the_value_did_not_land(vetted, monkeypatch):
    """No unit means nowhere to append, which is a failed write and not a stored
    one -- the response must not claim a cell that is empty."""
    _adopt_dashboard(monkeypatch)
    monkeypatch.setattr(routes, "_session_unit", lambda *_a, **_k: "")
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open_items", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 503, await resp.text()
        assert (await resp.json())["code"] == "append_failed"


async def test_an_entry_over_the_logs_line_ceiling_is_not_reported_as_stored(vetted, monkeypatch):
    """Asked BEFORE the append, like the panel route asks its own.

    An entry over the log's whole-line ceiling can never land, so writing it
    would report a value as stored that no fold will ever see.
    """
    _adopt_dashboard(monkeypatch)
    monkeypatch.setattr(crew_log_emit, "dashboard_entry_fits", lambda *_a, **_k: False)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open_items", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 503, await resp.text()
        assert (await resp.json())["code"] == "append_failed"


async def test_an_append_that_does_not_land_is_reported_as_failed(vetted, monkeypatch):
    """The caller is told to try again on its next cycle rather than being told
    the value is there."""
    _adopt_dashboard(monkeypatch)
    monkeypatch.setattr(crew_log_emit, "on_dashboard_agentic", lambda *_a, **_k: False)
    app = _mounted()
    async with TestClient(TestServer(app)) as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open_items", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 503, await resp.text()
        assert (await resp.json())["code"] == "append_failed"
        assert not app["state"].broadcasts, "a write that did not land must push nothing"


async def test_an_unreadable_mistake_fold_costs_the_book_and_not_the_write(vetted, monkeypatch):
    """A crewmate with no readable mistakes is in the same position as one that
    has made none, so a damaged fold reads as an empty book. Failing the write
    would be the book costing the feature it exists to improve.
    """
    _adopt_dashboard(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("the fold is unreadable")

    monkeypatch.setattr(crew_log_projection, "read_slot_projection", _boom)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "open_items", "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()

        listed = await c.get(FIELDS_PATH, headers={"X-Session-Key": "dashboard:chat-1"})
        assert listed.status == 200, await listed.text()
        assert (await listed.json())["mistakes"] == []


async def test_a_crewmate_that_adopted_nothing_is_told_the_page_it_renders(vetted, monkeypatch):
    """The read answers with the DEFAULT before a template is adopted.

    It reports the default rather than an empty list because the default IS the page
    that crewmate renders: ``api_member_dashboard`` falls back to it for the same
    state. An empty list told the crewmate it had no cell to write while the page it
    was being shown had one, and the one it had is the "needs you" answer.
    """
    _default_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.get(FIELDS_PATH, headers={"X-Session-Key": "dashboard:chat-1"})
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["template"]["id"] == DEFAULT_MANIFEST["id"], body["template"]
        names = {row["field"] for row in body["fields"]}
        assert "for_you" in names, f"the default's agentic field is not offered: {names}"
        assert body["retry_budget"] > 0


async def test_a_write_succeeds_for_a_crewmate_that_adopted_nothing(vetted, monkeypatch):
    """The write path resolves the same default the read path renders.

    REPLACES a pin that asserted ``no_instance`` here. That refusal named adopting a
    template as the remedy, and nothing in P1 calls the adopt route -- so the one
    agentic field on the page every crewmate is shown, the "needs you" answer, could
    never be written by anyone, and the conductor skill writes it every cycle.
    """
    _default_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "for_you", "value": [{"what": "approve the plan"}]},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["ok"] is True
        # Accepted AS the declared type, which is what proves it was validated against
        # the default's manifest rather than waved through.
        assert body["written"] == {"field": "for_you", "type": "array"}, body["written"]


async def test_an_unadopted_crewmate_is_refused_when_the_registry_ships_no_default(
    vetted, monkeypatch
):
    """``default_instance`` ANSWERS ``None`` rather than raising, so the EMPTY branch
    has to read its answer and not merely guard the call.

    This is the state of a build whose registry carries the loader and no template,
    and it is the one case where ``no_instance`` still describes the world: there is
    no manifest to validate a write against and no page being shown to write for. A
    branch that treated the ``None`` as a resolved record would hand ``parse_manifest``
    an attribute lookup on ``None`` and report a crewmate's write as a server fault.
    """
    _default_dashboard(monkeypatch, ships_default=False)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "for_you", "value": [{"what": "approve the plan"}]},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "no_instance"


async def test_two_crews_sharing_one_slug_cannot_reach_the_dashboard_surface(vetted, monkeypatch):
    """``Oncall`` and ``oncall`` resolve to ONE instance directory.

    Slugification is lossy and an instance is one directory per slug, so either
    crew could replace the other's staged or installed page, and a rollback by one
    would restore a version the other wrote. The browser's own resolver refuses
    that slug outright (``dashboard_slug_ambiguous``); this surface reaches the same
    files through MCP and owes the same refusal.

    Asked at ``_resolve_dashboard_caller``, so the refusal covers fields and write
    as well as the page's four -- and a route added later inherits it.
    """
    upper, lower = "Oncall", "oncall"
    shared = members_mod.member_slug(lower)
    assert members_mod.member_slug(upper) == shared, "fixture no longer collides"

    real_load = routes.KiroCrewConfig.load

    def _roster_with_both(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg.agents.clear()
        for name in (upper, lower):
            cfg.agents[name] = SimpleNamespace(name=name)
        return cfg

    monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(_roster_with_both))

    async with _client(lower) as caller:
        for path in (
            "/api/agent-panel/dashboard/preview",
            "/api/agent-panel/dashboard/apply",
            "/api/agent-panel/dashboard/rollback",
        ):
            response = await caller.post(
                path,
                json={"template_id": "x", "to_version": 1},
                headers={"X-Session-Key": "dashboard:chat-9"},
            )
            assert response.status == 409, (path, await response.text())
            assert (await response.json())["code"] == "dashboard_slug_ambiguous"
        read = await caller.get(
            "/api/agent-panel/dashboard/fields", headers={"X-Session-Key": "dashboard:chat-9"}
        )
        assert read.status == 409, await read.text()


async def test_a_crew_that_is_not_a_configured_member_still_reaches_its_own_page(vetted):
    """EMPTY owners is not the ambiguous case, and refusing it would be a regression.

    ``_member_names_for_slug`` lists addressable MEMBERS, and a publishing crew need
    not be one -- ``_resolve_publishing_crew`` has already vetted a caller that is
    not. So no configured member claiming the slug means nobody else owns it, which
    is the opposite of the collision this gate is for.
    """
    async with _client(CREW) as caller:
        response = await caller.get(
            "/api/agent-panel/dashboard/fields", headers={"X-Session-Key": "dashboard:chat-1"}
        )
        assert response.status == 200, await response.text()


# ------------------------------------- the page's four routes, over HTTP

TEMPLATES_PATH = "/api/agent-panel/dashboard/templates"
PREVIEW_PATH = "/api/agent-panel/dashboard/preview"
APPLY_PATH = "/api/agent-panel/dashboard/apply"
ROLLBACK_PATH = "/api/agent-panel/dashboard/rollback"


def _page_store(monkeypatch, **over: Any) -> SimpleNamespace:
    """A store standing in for the instance module, through this file's own seam.

    The same `sys.modules` + package-attribute pair `_adopt_dashboard` uses, and for
    the same reason: these four handlers are being tested, not the store underneath
    them, and driving the real store would bind every case here to the shipped
    catalogue's twelve directories and their sizes.

    ``InstanceRefused`` is a REAL exception class rather than a sentinel, because
    ``_instance_refusal`` separates a refusal from an unexpected failure with
    ``isinstance`` -- a stub that got that wrong would make a 400 case pass as a 503.
    """

    class InstanceRefused(Exception):
        pass

    staged = {
        "template_id": "fixture-board",
        "template_version": 1,
        "title": "Fixture board",
        "fields": ["credits", "phase"],
        "html_bytes": 120,
        "staged_ms": 1,
        "preview_url": f"/api/members/{SLUG}/dashboard?preview=1",
    }
    store = SimpleNamespace(
        InstanceRefused=InstanceRefused,
        InstanceError=Exception,
        read=lambda _slug: SimpleNamespace(template_id="fixture-board"),
        stage_preview=lambda _slug, **_kw: SimpleNamespace(wire=lambda: dict(staged)),
        apply_preview=lambda _slug, **_kw: SimpleNamespace(
            instance_version=5, template_id="fixture-board"
        ),
        rollback=lambda _slug, _to, **_kw: SimpleNamespace(
            instance_version=7, template_id="fixture-board"
        ),
        STATE_LIVE="live",
    )
    for key, value in over.items():
        setattr(store, key, value)
    monkeypatch.setitem(sys.modules, "kiro_crew.dashboard_templates.instance", store)
    import kiro_crew.dashboard_templates as templates_pkg

    monkeypatch.setattr(templates_pkg, "instance", store, raising=False)
    return store


class TestTheTemplatesRouteOverHttp:
    async def test_it_answers_the_catalog_and_marks_the_current_template(
        self, vetted, monkeypatch
    ) -> None:
        _page_store(monkeypatch)
        monkeypatch.setattr(
            routes,
            "_templates_for_agent",
            lambda slug, query: {
                "templates": [{"id": "fixture-board", "current": True}],
                "current_template_id": "fixture-board",
                "query": query,
                "problems": [],
            },
        )
        async with _client() as c:
            resp = await c.get(
                f"{TEMPLATES_PATH}?query=cost", headers={"X-Session-Key": "dashboard:chat-1"}
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["current_template_id"] == "fixture-board"
        assert body["query"] == "cost", "the query did not reach the lister"

    async def test_the_query_is_bounded_before_it_reaches_the_lister(
        self, vetted, monkeypatch
    ) -> None:
        """200 characters, because the handler slices it rather than refusing.

        A query is a person's own words relayed by an agent, and a long one is a
        clumsy ask rather than an attack -- so it is cut and answered, not rejected.
        """
        seen: list[str] = []

        def _lister(_slug, query):
            seen.append(query)
            return {"templates": [], "current_template_id": "", "query": query, "problems": []}

        _page_store(monkeypatch)
        monkeypatch.setattr(routes, "_templates_for_agent", _lister)
        async with _client() as c:
            resp = await c.get(
                f"{TEMPLATES_PATH}?query={'x' * 400}",
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 200, await resp.text()
        assert seen and len(seen[0]) == 200, f"query reached the lister at {len(seen[0])} chars"

    async def test_a_registry_failure_is_a_refusal_and_not_a_500(self, vetted, monkeypatch) -> None:
        store = _page_store(monkeypatch)

        def _boom(_slug, _query):
            raise store.InstanceRefused("the registry cannot be read")

        monkeypatch.setattr(routes, "_templates_for_agent", _boom)
        async with _client() as c:
            resp = await c.get(TEMPLATES_PATH, headers={"X-Session-Key": "dashboard:chat-1"})
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "refused"


class TestThePreviewRouteOverHttp:
    async def test_a_staged_template_comes_back_with_its_link(self, vetted, monkeypatch) -> None:
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                json={"template_id": "fixture-board"},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["ok"] is True
        assert body["preview"]["preview_url"].endswith("preview=1")
        assert "html" not in body["preview"], "the staged page itself came back on the wire"

    async def test_a_non_string_template_id_is_refused(self, vetted, monkeypatch) -> None:
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                json={"template_id": 7},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "validation_error"

    async def test_a_body_that_is_not_json_is_refused(self, vetted, monkeypatch) -> None:
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                data="not json",
                headers={
                    "X-Session-Key": "dashboard:chat-1",
                    "Content-Type": "application/json",
                },
            )
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "invalid_json"

    async def test_a_json_body_that_is_not_an_object_is_refused(self, vetted, monkeypatch) -> None:
        """A list parses as JSON and carries no fields, so it needs its own answer."""
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                json=["fixture-board"],
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "invalid_body"

    async def test_an_authored_page_is_refused_with_the_stores_own_sentence(
        self, vetted, monkeypatch
    ) -> None:
        """The handler passes `html`/`manifest` through so the STORE can refuse them.

        It must not drop them and stage a template the caller never asked for: a
        caller that sent a page it wrote has to read why its page cannot render.
        """
        store = _page_store(monkeypatch)
        seen: list[dict[str, Any]] = []

        def _stage(_slug, **kw):
            seen.append(kw)
            raise store.InstanceRefused("custom templates come later")

        monkeypatch.setattr(store, "stage_preview", _stage)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                json={"manifest": {"id": "mine"}, "html": "<p></p>"},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 400, await resp.text()
            body = await resp.json()
        assert body["code"] == "refused"
        assert "come later" in body["error"]
        assert seen and seen[0]["html"] == "<p></p>", "the handler swallowed the page"
        assert seen[0]["manifest"] == {"id": "mine"}

    async def test_an_unexpected_store_failure_is_503_and_leaks_nothing(
        self, vetted, monkeypatch
    ) -> None:
        """A refusal is written for an agent; an OSError carries a path.

        So the path goes to the log and the caller gets a fixed sentence.
        """
        store = _page_store(monkeypatch)

        def _boom(_slug, **_kw):
            raise OSError("/home/someone/.kirocrew/members/x/instance.json: no space")

        monkeypatch.setattr(store, "stage_preview", _boom)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                json={"template_id": "fixture-board"},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 503, await resp.text()
            body = await resp.json()
        assert body["code"] == "store_failed"
        assert ".kirocrew" not in body["error"], "the refusal carried a filesystem path"


class TestTheApplyAndRollbackRoutesOverHttp:
    async def test_apply_reports_the_new_version_and_tells_the_open_tab(
        self, vetted, monkeypatch
    ) -> None:
        """The broadcast is what makes the tab refetch, and it carries the slug only."""
        _page_store(monkeypatch)
        frames: list[tuple[str, Any]] = []
        async with _client() as c:
            state = c.server.app["state"]
            monkeypatch.setattr(
                state, "broadcast_ws", lambda kind, data: frames.append((kind, data))
            )
            resp = await c.post(APPLY_PATH, json={}, headers={"X-Session-Key": "dashboard:chat-1"})
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["instance_version"] == 5 and body["template_id"] == "fixture-board"
        assert "saved_template_id" not in body, "apply still promises a saved template"
        assert frames == [("dashboard_instance_changed", {"slug": SLUG})], frames

    async def test_apply_with_nothing_staged_is_a_refusal(self, vetted, monkeypatch) -> None:
        store = _page_store(monkeypatch)

        def _nothing(_slug, **_kw):
            raise store.InstanceRefused("there is no page staged to keep")

        monkeypatch.setattr(store, "apply_preview", _nothing)
        async with _client() as c:
            resp = await c.post(APPLY_PATH, json={}, headers={"X-Session-Key": "dashboard:chat-1"})
            assert resp.status == 400, await resp.text()
            body = await resp.json()
        assert body["code"] == "refused" and "no page staged" in body["error"]

    async def test_rollback_reports_the_version_it_landed_on(self, vetted, monkeypatch) -> None:
        """Two different numbers: the one asked for, and the one now current.

        A caller that reported the first would tell a person they are on a version
        nobody is on.
        """
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                ROLLBACK_PATH,
                json={"to_version": 2},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["restored_from"] == 2, "the route lost the version asked for"
        assert body["instance_version"] == 7, "a rollback moves FORWARD"

    @pytest.mark.parametrize(
        "to_version",
        [0, -1, "2", 1.5, True, None],
        ids=["zero", "negative", "string", "float", "bool", "absent"],
    )
    async def test_a_version_that_is_not_a_positive_integer_is_refused(
        self, vetted, monkeypatch, to_version: Any
    ) -> None:
        """``True`` is included on purpose: it is an ``int`` to ``isinstance``.

        Without the explicit bool check a rollback to ``True`` would reach the store
        as version 1 -- a real version, restored because a caller sent a flag.
        """
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                ROLLBACK_PATH,
                json={"to_version": to_version},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "validation_error"


class TestTheCatalogListingTheAgentReads:
    """`_templates_for_agent` itself, against a real registry scan.

    The route cases above replace this function, because what they are about is the
    HTTP contract. This class is about the LISTING, so it runs the real thing over a
    fixture built-in directory.
    """

    @staticmethod
    def _fixture_catalog(monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard_templates import catalog

        root = tmp_path / "builtin"
        for tid, title, description, fold, path in (
            ("fixture-board", "Fixture board", "What this crewmate did.", "usage", "cost_usd"),
            ("fixture-plain", "Fixture plain", "A second page.", "workstreams", "items"),
        ):
            directory = root / tid
            directory.mkdir(parents=True)
            (directory / "manifest.json").write_text(
                json.dumps(
                    {
                        "id": tid,
                        "version": 1,
                        "title": title,
                        "description": description,
                        "source": "builtin",
                        "fields": {
                            "shown": {"type": "number", "source": {"fold": fold, "path": path}}
                        },
                    }
                ),
                encoding="utf-8",
            )
            (directory / "template.html").write_text(
                '<p data-dashboard-field="shown"></p>', encoding="utf-8"
            )
        monkeypatch.setattr(catalog, "builtin_dir", lambda: root)

    def test_it_marks_the_current_template_rather_than_hiding_it(
        self, monkeypatch, tmp_path
    ) -> None:
        """A list that omitted the page on screen would read as a missing template."""
        self._fixture_catalog(monkeypatch, tmp_path)
        _page_store(monkeypatch)
        body = routes._templates_for_agent(SLUG, "")
        rows = {row["id"]: row for row in body["templates"]}
        assert set(rows) == {"fixture-board", "fixture-plain"}
        assert rows["fixture-board"]["current"] is True
        assert rows["fixture-plain"]["current"] is False
        assert body["current_template_id"] == "fixture-board"

    def test_a_query_matches_a_word_only_a_fold_path_carries(self, monkeypatch, tmp_path) -> None:
        """The reason the listing carries `paths` at all.

        "the one that shows cost" names what the reader wants to SEE, and the template
        that shows it is the one whose field reads a cost out of a usage fold -- a word
        its author never put in the title or the description.
        """
        self._fixture_catalog(monkeypatch, tmp_path)
        _page_store(monkeypatch)
        body = routes._templates_for_agent(SLUG, "cost")
        assert [row["id"] for row in body["templates"]] == ["fixture-board"]
        assert body["query"] == "cost"

    def test_a_query_that_matches_nothing_answers_an_empty_list(
        self, monkeypatch, tmp_path
    ) -> None:
        self._fixture_catalog(monkeypatch, tmp_path)
        _page_store(monkeypatch)
        assert routes._templates_for_agent(SLUG, "latency")["templates"] == []

    def test_a_directory_that_will_not_load_is_reported_not_dropped(
        self, monkeypatch, tmp_path
    ) -> None:
        """A template nobody can see is a template nobody can fix.

        The scan reports it, so the agent reading this listing can tell the human.
        """
        self._fixture_catalog(monkeypatch, tmp_path)
        broken = tmp_path / "builtin" / "fixture-broken"
        broken.mkdir()
        (broken / "manifest.json").write_text("{ not json", encoding="utf-8")
        (broken / "template.html").write_text("<p></p>", encoding="utf-8")
        _page_store(monkeypatch)
        body = routes._templates_for_agent(SLUG, "")
        assert [p["template"] for p in body["problems"]] == ["fixture-broken"]
        assert {row["id"] for row in body["templates"]} == {"fixture-board", "fixture-plain"}

    def test_an_unreadable_current_template_leaves_the_listing_answerable(
        self, monkeypatch, tmp_path
    ) -> None:
        """A damaged record must not take the catalog down with it.

        The list is how a reader gets OFF a broken page, so a record this cannot read
        marks nothing as current rather than refusing the only route out.
        """
        self._fixture_catalog(monkeypatch, tmp_path)

        def _unreadable(_slug):
            raise OSError("instance.json is a directory")

        _page_store(monkeypatch, read=_unreadable)
        body = routes._templates_for_agent(SLUG, "")
        assert body["current_template_id"] == ""
        assert len(body["templates"]) == 2
        assert all(row["current"] is False for row in body["templates"])


class TestTheHistorySessionLookup:
    """`_history_session`: which DM unit a change is attributed to."""

    def test_no_slot_is_no_session_and_not_a_failure(self) -> None:
        assert routes._history_session("") == ""

    def test_the_newest_unit_of_the_slot_is_the_one_chosen(self, monkeypatch) -> None:
        """The NEWEST, because a change is being recorded now.

        A clock that stepped backward between two units would otherwise attribute a
        later change to a session that is already over.
        """
        monkeypatch.setattr(
            crew_log_projection, "units_in_succession", lambda _slot: ["older", "newest"]
        )
        assert routes._history_session("dashboard:chat-1") == "newest"

    def test_a_slot_with_no_unit_yet_answers_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(crew_log_projection, "units_in_succession", lambda _slot: [])
        assert routes._history_session("dashboard:chat-1") == ""

    def test_a_projection_failure_degrades_to_no_history_rather_than_refusing(
        self, monkeypatch
    ) -> None:
        """The record is the file; the entry is history.

        So a crewmate whose DM thread cannot be read gets a working dashboard with no
        history row, not a refused change.
        """

        def _boom(_slot):
            raise RuntimeError("the projection kernel is not up")

        monkeypatch.setattr(crew_log_projection, "units_in_succession", _boom)
        assert routes._history_session("dashboard:chat-1") == ""


# ----------------------------------- nested shapes on the agentic surface


#: A Needs-you list whose rows must carry ``text``, the key the page draws.
SHAPED_MANIFEST: dict[str, Any] = {
    "id": "shaped",
    "version": 3,
    "title": "Shaped",
    "description": "A field with a nested shape",
    "source": "builtin",
    "fields": {
        "for_you": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "ask": {"type": "string", "enum": ["decide", "approve", "do"]},
                },
                "required": ["text"],
            },
            "source": {"agentic": True},
        },
        "items": {"type": "array", "source": {"fold": "work", "path": "items"}},
    },
}


def _shaped_dashboard(monkeypatch, read: Any = None) -> None:
    """The shaped manifest through ``read_instance``, and *read* as the page's values."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.dashboard.handlers import member_dashboard
    from kiro_crew.dashboard_templates.manifest import parse_manifest

    inst = dashboard_agentic.Instance(
        manifest=parse_manifest(dict(SHAPED_MANIFEST)), instance_version=5
    )
    monkeypatch.setattr(routes, "read_instance", lambda _slug, _crew: inst)
    if read is not None:
        monkeypatch.setattr(member_dashboard, "read_fields", lambda *_a: read)


async def test_a_needs_you_card_without_text_is_refused_naming_text(vetted, monkeypatch):
    """The ``{title, detail}`` card the page drops silently is refused, and told why."""
    _shaped_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "for_you", "value": [{"title": "Pick a db", "detail": "a or b"}]},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "wrong_shape", body
    assert "'title'" in body["error"] and "'detail'" in body["error"], body["error"]
    assert "missing required key(s) 'text'" in body["error"], body["error"]


async def test_a_card_in_the_declared_shape_is_accepted(vetted, monkeypatch):
    _shaped_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "for_you", "value": [{"text": "Pick a db", "ask": "decide"}]},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["written"] == {"field": "for_you", "type": "array"}


async def test_the_field_list_carries_the_shape_and_the_current_values(vetted, monkeypatch):
    """Shape and current value for an agentic field, one summary line for a fold field."""
    from kiro_crew.dashboard_feed import FieldRead

    card = [{"text": "Pick a db", "ask": "decide"}]
    board = [{"title": f"t{i}", "last_report_at": f"2026-10-0{i}T00:00:00Z"} for i in (1, 2, 3)]
    read = FieldRead(
        fields={"for_you": card, "items": board},
        written_at={"for_you": "2026-10-08T01:00:00Z"},
    )
    _shaped_dashboard(monkeypatch, read)
    async with _client() as c:
        resp = await c.get(FIELDS_PATH, headers={"X-Session-Key": "dashboard:chat-1"})
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    rows = {row["field"]: row for row in body["fields"]}
    assert rows["for_you"]["schema"] == {
        key: SHAPED_MANIFEST["fields"]["for_you"][key] for key in ("type", "items")
    }
    assert rows["for_you"]["value"] == card
    assert rows["for_you"]["written_at"] == "2026-10-08T01:00:00Z"
    assert rows["items"]["summary"] == "3 items, latest 2026-10-03T00:00:00Z"
    assert "value" not in rows["items"], "a fold field's data is summarised, never returned"
    assert "mistakes" in body, "the mistake book stays in this read"
