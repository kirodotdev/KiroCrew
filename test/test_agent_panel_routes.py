"""Tests for the crew-webview HTTP routes.

The property worth guarding hardest: which crew a publish writes to is derived
from the CALLING SESSION's own crew binding, never from anything the caller
sends. A body-supplied crew name would let one crew publish a webview that
presents as another's, and the whole value of a per-crew dashboard is that the
operator can trust whose state they are reading.
"""

from __future__ import annotations

import json
import shutil

import pytest
from agent_panel_routes_helpers import (  # noqa: F401 -- fixtures are used by name
    CREW,
    SLUG,
    _client,
    _crew_log_on,
    _folded,
    _mounted,
    _State,
    _unit_for,
    vetted_fixture,
)
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from off_loop_helpers import off_loop

from kiro_crew import agent_panel
from kiro_crew import members as members_mod
from kiro_crew.config.paths import data_home
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.dashboard.handlers import agent_panel as routes

pytestmark = pytest.mark.asyncio

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


async def test_the_fold_a_page_reads_carries_the_scrubbed_publish_not_the_raw_body(vetted):
    """A dashboard page binds the ``panel`` fold, so the fold must hold what the
    publish path scrubbed, never the body the caller sent."""
    # Assembled at runtime so the diff carries no credential-shaped literal.
    token = "ghp" + "_" + "ZXAMPLEzxampleZXAMPLEzxampleZXAMPLE12"
    _unit_for(CREW)
    async with _client() as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"note": f"fetched with {token}"}, "title": f"t {token}"},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()
    folded = _folded()
    assert folded is not None, "the publish recorded no history"
    assert token not in json.dumps(folded), "the fold carries the raw publish body"
    assert "fetched with" in folded["data"]["note"]


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
