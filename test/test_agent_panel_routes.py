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
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from member_memory_helpers import write_member_home

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
    folded = crew_log_projection.read_slot_projection(_crew_slot(crew_name), "panel").value
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
        TestServer(_mounted(agent, internal=internal, namespace=namespace, sessions=sessions))
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
    astray = crew_log_projection.read_slot_projection(
        members_mod.member_slot_key(SLUG), "panel"
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


async def test_a_name_the_grammar_rejects_still_holds_its_panel(vetted, monkeypatch):
    """Existing is not the same question as addressable.

    The create route validates a crew name only against the credential-shape
    check, so ``"On call"`` -- a space, which the agent-name grammar rejects -- is
    a real crew that derives the slug ``on-call``. The liveness check asks whether
    the recorded owner is still there, and enumerating only the ADDRESSABLE names
    drops that crew, reports its owner as gone, and hands the colliding publisher
    its record.
    """
    spaced = "On call"
    spaced_slug = members_mod.slug_for_name(spaced)
    async with _client(spaced) as owner:
        assert (
            await owner.post(
                "/api/agent-panel/publish",
                json={"data": {"cycle": 47}},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
        ).status == 200, "the space-named crew could not publish at all"
    kept = agent_panel.read(spaced_slug)
    assert kept is not None and kept["crew"] == spaced

    # The roster holds the space-named crew and nothing else, so the ONLY reason
    # the enumeration could miss it is the grammar filter.
    real_load = routes.KiroCrewConfig.load

    def _roster_with_the_spaced_crew(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg.agents.clear()
        cfg.agents[spaced] = SimpleNamespace(name=spaced)
        return cfg

    monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(_roster_with_the_spaced_crew))

    colliding = "on-call"
    assert members_mod.slug_for_name(colliding) == spaced_slug, "fixture no longer collides"
    async with _client(colliding) as impostor:
        resp = await impostor.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 999}},
            headers={"X-Session-Key": "dashboard:chat-2"},
        )
        assert resp.status == 400, await resp.text()

    after = agent_panel.read(spaced_slug)
    assert after is not None
    assert after["crew"] == spaced
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
            patch.delenv("KIROCREW_CREW_LOG", raising=False)
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
    record = routes._panel_record(
        routes._panel_slot(KiroCrewConfig.load(), CREW, SLUG), SLUG, agent_panel.crew_key(CREW)
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
        for hostile in ("../../etc/passwd", "a\nb", "x" * 300, "a;b"):
            resp = await c.get(
                f"/api/members/{SLUG}/panel?member={quote(hostile, safe='')}",
            )
            assert resp.status == 400, f"{hostile!r} was accepted"


async def test_a_credential_shaped_crew_name_can_still_read_its_own_panel(vetted):
    """Two of our own guards collided, and only this shape shows it.

    Redaction scrubs the stored ``crew`` because a crew name is untrusted text
    rendered to the operator. The read check compares the EXACT name because
    slugification is lossy. Together they locked out any crew whose name happens to
    look credential-shaped: the stored owner became ``[REDACTED: credential]``,
    which equals no exact name, so that crew could never read its own panel.

    Ownership is decided on a digest of the exact name; the display text stays
    redacted. Nobody would think to try this name, which is exactly why it is
    pinned.
    """
    # An AKIA-prefixed 20-character name is enough to trip the credential detector.
    # Assembled rather than written literally; see test_mcp_panel_runtime.py.
    shaped = "".join(["AKIA", "IOSFODNN7", "EXAMPLE"])
    assert agent_panel._scrub(shaped) != shaped, "fixture is no longer redacted"

    slug = members_mod.slug_for_name(shaped)
    async with _client(agent=shaped) as c:
        pub = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert pub.status == 200, await pub.text()

        body = await (await c.get(f"/api/members/{slug}/panel?member={shaped}")).json()
        assert body["panel"] is not None, "the crew was locked out of its own panel"
        assert body["html"], "no document returned to the owning crew"
        assert body["panel"]["data"] == {"cycle": 47}
        # The DISPLAY text is still redacted -- the fix must not have simply stopped
        # scrubbing the name to make the comparison work.
        assert shaped not in json.dumps(body), "an unredacted credential-shaped name was served"
        assert "REDACTED" in body["panel"]["crew"]


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
