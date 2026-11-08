"""Crew-webview routes behind a real member allocation: the registry key a publish resolves, the
audited refusals of the crew resolver, legacy and colliding member names, linked and unowned
records, the refresh push, app-identified callers, and the gateway's route registration.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from agent_panel_routes_helpers import (  # noqa: F401 -- fixtures are used by name
    CREW,
    SLUG,
    _client,
    _crew_log_on,
    _crew_slot,
    _folded,
    _mounted,
    _unit_for,
    vetted_fixture,
)
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from member_memory_helpers import write_member_home
from off_loop_helpers import off_loop

from kiro_crew import agent_panel
from kiro_crew import members as members_mod
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.dashboard.handlers import agent_panel as routes

# through a real allocation


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

    The ``_Sessions`` stub ignores the key it is handed, so only a real registry can
    show WHICH key reaches a live allocation. ``member`` is the ``capability_member``;
    empty models a session with a blank ``crew_agent`` that selected a provider template.
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

    ``_normalize_slot_key`` strips the prefix, so its output always misses the registry.
    """
    from kiro_crew.dashboard.state import _normalize_slot_key

    registry_key = _member_session_key()
    slot_key = _crew_slot(CREW)
    assert registry_key == f"dashboard:{slot_key}"
    assert _normalize_slot_key(registry_key) == slot_key
    assert _normalize_slot_key(registry_key) != registry_key


@pytest.mark.asyncio
async def test_a_real_member_allocation_can_publish(vetted):
    """A member's DM session, the only one the panel tool is mounted on, can publish.

    The registry is keyed by session key, so a lookup by slot key refuses every member.
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

    ``_recognize_session`` already logged ``outcome="allowed"``, so a silent refusal
    leaves the trail ending on the allow. Checked over the AST so no branch is missed:
    each ``return None, web.json_response(...)`` needs a prior ``log_api_access``.
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
    # restricted session, no dashboard slot, unresolved session, not a root session
    # (the dynamic dashboard's own branch), no crew, bad slug.
    assert refusals == 8, refusals
    assert audits(tree.body[0].body) == []


@pytest.mark.asyncio
async def test_the_unresolved_session_denial_is_audited(vetted, monkeypatch):
    """The unresolved-session refusal, exercised rather than only asserted structurally."""
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


@pytest.mark.asyncio
async def test_a_bare_slot_key_is_refused_rather_than_re_prefixed(vetted):
    """A bare slot name resolves to nothing, and that refusal is deliberate.

    The strict gate always yields the full session key, so re-adding the prefix
    would hide the anomaly this refusal exists to surface.
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


@pytest.mark.asyncio
async def test_a_real_allocation_that_selected_a_template_is_refused_no_crew(vetted):
    """An empty ``capability_member`` is a real absent crew binding, so ``no_crew`` fits.

    It stays distinct from a key that reached no allocation at all.
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_a_degraded_roster_refuses_takeover_rather_than_granting_it(vetted, monkeypatch):
    """ "Cannot read the roster" must not answer "the owner is gone".

    ``KiroCrewConfig.load()`` returns defaults when the file is unreadable, so an empty
    roster may mean "unreadable", and takeover would hand a live crew's record away.
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_a_v2_member_round_trips_through_the_resolving_slot(vetted):
    """The publish slot and the read slot must agree on the NON-FALLBACK branch.

    ``_member_thread_slot`` adds the store to the key for a V2 member, so a fallback-only
    test proves nothing: the fold would miss and the drawer show "never published".
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


@pytest.mark.asyncio
async def test_a_publish_whose_append_is_skipped_is_still_what_a_reader_gets(vetted):
    """An accepted publish must never be shadowed by an older logged one.

    The log append is best-effort (log off, or a line over its ceiling), so the file
    can be newer than the fold, and the newer record must win.
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_a_colliding_crew_cannot_read_the_other_crews_panel(vetted):
    """The READ half of the ownership guard.

    A read keyed on the slug alone would show the publisher's panel to the other crew.
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_a_linked_record_is_a_coded_refusal_not_a_500(vetted):
    """The store refuses to write through an alias; the route must say so with a code.

    :func:`agent_panel.panel_path` refuses a link, which could reach outside the fenced
    directory; an uncaught ``CrewSlugError`` would be an opaque 500 to the operator.
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

        # The read serves the panel from the crew log; the link is never followed.
        body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
        assert body["panel"]["data"] == {"cycle": 1}, "the fold should still hold cycle 1"
        assert body["html"]


@pytest.mark.asyncio
async def test_a_linked_legacy_record_is_never_followed_on_the_read(vetted):
    """A LINKED legacy record reads as the empty state, not as an error.

    :func:`agent_panel.panel_path` refuses the link and ``read`` treats that
    ``ValueError`` as "no panel published", so the link's bytes are never served.
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


@pytest.mark.asyncio
async def test_an_unowned_record_is_not_served_to_anyone(vetted):
    """The read guard must fail CLOSED when ownership cannot be established.

    No publish writes an empty ``crew_key``, so such a record is forged. Planted in the
    legacy file with an empty fold, so the read falls back to the file.
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


@pytest.mark.asyncio
async def test_publishing_pushes_a_refresh_to_open_drawers(vetted):
    """A publish pushes a refresh, since the query client uses ``staleTime: Infinity``."""
    app = _mounted()
    async with TestClient(TestServer(app)) as c:
        await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        sent = app["state"].broadcasts
        assert ("panel_published", {"slug": SLUG}) in sent, f"no refresh pushed: {sent}"


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_an_app_identified_internal_caller_cannot_publish(vetted):
    """The secret gate does NOT imply "not an app".

    ``token_auth`` can set both ``internal_auth`` and ``request["app"]``, so an app agent
    whose slot names a crew would pass ``internal_secret_required`` and publish as it.
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


@pytest.mark.asyncio
async def test_an_app_token_caller_is_denied_the_panel(vetted):
    """``/api/members/{slug}/panel`` is a CHILD of ``/api/members``.

    An app token scoped to the parent is admitted by prefix, so the denial is explicit.
    404, not 403, so the status does not confirm the surface exists.
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


@pytest.mark.asyncio
async def test_no_route_here_writes_from_the_browser_surface():
    """Everything under /api/members here is a read; writes are MCP-only."""
    member_routes = [
        r
        for r in _mounted().router.routes()
        if str(r.resource.canonical).startswith("/api/members")
    ]
    assert member_routes
    assert {r.method for r in member_routes} <= {"GET", "HEAD"}


@pytest.mark.asyncio
async def test_the_gateway_registers_exactly_these_paths_without_importing_us():
    """The boot path binds these routes DEFERRED, so the paths are written twice.

    ``server._register_mcp_routes`` may not import this subsystem before the socket
    binds, so it restates each path against ``server._deferred``. The two sets must match.
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
