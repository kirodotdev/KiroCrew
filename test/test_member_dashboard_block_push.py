"""The v3 dashboard's push protocol, one test per sentence of it -- negatives included.

The five the brief fixes, and the two the conductor added:

1. First load renders the WHOLE page.
2. A fold pushes ONLY the blocks that subscribe to that fold.
3. It goes over the EXISTING WebSocket -- the owner channel -- carrying a version.
4. A version GAP, or a LAYOUT CHANGE, triggers a full refetch. Both, not just the gap.
5. The owner check and the redaction stay in the CONTROLLER.
6. A package-bound crewmate gets no builtin fallback; a registry-bound one is unchanged.
7. ``instance.RENDERABLE_SOURCES`` stays at one value -- the v3 page earns execution at
   its own gate, which is :func:`member_dashboard._minted_package_page`.

The redaction cases assert over the WHOLE serialized payload rather than the one field a
reader would think to look at, with a positive control proving the values that must
survive DID: a per-field assertion passes just as happily on an empty payload.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard.handlers import member_dashboard_push as push

pytestmark = pytest.mark.asyncio

MEMBER = "Fleet Conductor"
SLUG = "fleet-conductor"
#: The crewmate's DM slot, DELIBERATELY NOT the slug: every subscription in this module
#: is keyed by it, so a page that fell back to the slug would read a different (empty)
#: slot and these tests would see no values at all. `member_dashboard._dashboard_slot`
#: is what resolves it in the route; a V2 crewmate's is store-scoped like this one.
SLOT = "member-fleet-conductor-7f21"
BOUND = f"crewmate:{SLUG}"

#: A secret in the shape the egress redactor recognises, so the assertion is about the
#: controller's chokepoint rather than about a pattern invented here.
SECRET = "ghp_" + "A" * 36


def package(*, bound_to: str = BOUND, blocks: list[dict[str, Any]] | None = None) -> dict:
    """A package with TWO blocks reading TWO DIFFERENT folds.

    That split is the whole point of the fixture: a push for ``work`` must carry the
    ``prs`` block and must NOT carry ``runs``, and a fixture whose blocks share a fold
    could not tell a correct push from one that sends everything.
    """
    return {
        "kind": "dashboard",
        "bound_to": bound_to,
        "model": {
            "types": {
                "open_prs": {
                    "type": "number",
                    "label": "Open PRs",
                    "source": {"fold": "work", "path": "summary.open_prs"},
                },
                "note": {
                    "type": "text",
                    "label": "Note",
                    "source": {"fold": "work", "path": "summary.note"},
                },
                "last_run": {
                    "type": "timestamp",
                    "label": "Last run",
                    "source": {"fold": "panel", "path": "last_run"},
                },
            }
        },
        "view": {
            "blocks": (
                blocks
                if blocks is not None
                else [
                    {"id": "prs", "type": "table", "fields": ["open_prs", "note"]},
                    {"id": "runs", "type": "stat", "fields": ["last_run"]},
                ]
            )
        },
        "theme": {"tokens": {"--panel-bg": "oklch(21% 0 0)"}},
    }


def _live_page(slug: str):
    """The live page for *slug*, read straight off the registry.

    Here rather than in the push module: the controller is handed the page it armed, so
    production never looks one up, and a read that only a test wants is a surface the
    module should not advertise. The registry is module state, so a test can hold it.
    """
    with push._PAGES_LOCK:
        return push._PAGES.get(slug)


def _close_all_pages() -> None:
    """Stop every live push and empty the registry. For teardown.

    The registry outlives one test, so without this a page armed by one case keeps
    subscriptions that deliver into the next. Not a gateway shutdown hook: a restart
    relies on `LivePage.bind` and the version counter instead.
    """
    with push._PAGES_LOCK:
        pages = list(push._PAGES.values())
        push._PAGES.clear()
    for page in pages:
        page.close()


class _Hub:
    """The hub's one method this push uses, recording what it was handed."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []

    def broadcast_ws_owners(self, msg_type: str, data: dict[str, Any]) -> None:
        # Serialized on the way in, exactly as the real hub does, so a payload that
        # cannot be JSON would fail here rather than in production.
        json.loads(json.dumps(data))
        self.frames.append((msg_type, data))


def _event(fold: str, value: dict[str, Any], *, revision: int = 1, seq: int = 1):
    from kiro_crew.crew_log import bus as crew_log_bus

    return crew_log_bus.FoldAdvanced(
        # Keyed by the DM SLOT, which is what the bus actually delivers under
        # `SCOPE_SLOT` and what this page subscribes with. Not the slug: see
        # `test_a_slot_fold_is_keyed_by_the_crewmates_dm_slot_not_its_slug`.
        scope=crew_log_bus.SCOPE_SLOT,
        key=SLOT,
        fold=fold,
        revision=revision,
        value=value,
        seq=seq,
    )


WORK = {"summary": {"open_prs": 4, "note": f"token {SECRET} pasted"}}
PANEL = {"last_run": "2026-10-09T23:00:00Z"}


@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    """An isolated home, a tmp artifact store, and the member checks stubbed.

    The member-identity and owner gates are the SIBLING routes' and are tested there;
    what is stubbed is config loading, not the checks. Patched through
    ``_floor_monkeypatch`` so a test body calling ``monkeypatch.undo()`` cannot hand the
    rest of that test the real config loader.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    cfg = SimpleNamespace(agents={MEMBER: SimpleNamespace(member_id="")})
    _floor_monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    _floor_monkeypatch.setattr(routes.members_mod, "member_slug", lambda name, config=None: SLUG)
    _floor_monkeypatch.setattr(routes.members_mod, "validate_slug", lambda slug: slug)
    _floor_monkeypatch.setattr(routes.members_mod, "is_dispatchable_member_name", bool)
    _floor_monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: [MEMBER])
    _floor_monkeypatch.setattr(routes, "_deny_app_caller", _none)
    _floor_monkeypatch.setattr(routes, "_owner_only", _none)
    _floor_monkeypatch.setattr(routes, "_write_session", lambda slug, member: "")
    yield
    _close_all_pages()


async def _none(*_a, **_k):
    return None


@pytest.fixture
def store(tmp_path, _floor_monkeypatch) -> ArtifactStore:
    """The process-wide default store, pointed at a tmp root for this test."""
    import kiro_crew.artifacts as artifacts_mod

    live = ArtifactStore(root=tmp_path / "artifacts")
    _floor_monkeypatch.setattr(artifacts_mod, "get_default_store", lambda: live)
    return live


@pytest.fixture
def saved(store: ArtifactStore):
    return store.create(name="Mate dashboard", kind="dashboard", content=json.dumps(package()))


def _model(store: ArtifactStore, slug: str):
    loaded = store.get(slug)
    return dp.model_of(dp.parse_package(loaded.content or ""), slug=slug, version=loaded.version)


def _page(model, hub: _Hub, *, reread=None, pkg=None) -> push.LivePage:
    """A live page with no loop: the send is awaited directly by the test.

    Wired with the REAL ``block_patch`` and ``display_values``, which is what the
    controller passes too: there is no reason to stub the two functions whose narrowing
    and formatting the frame's whole payload comes out of -- a stub would pin this
    controller against a payload shape nothing else in the repo builds.
    """
    from kiro_crew import dashboard_package_render as render

    canonical = pkg if pkg is not None else dp.validate_package(package())
    return push.LivePage(
        SLUG,
        MEMBER,
        model,
        slot=SLOT,
        state=hub,
        loop=None,
        redact=routes._page_safe,
        reread=reread if reread is not None else (lambda _slug: (canonical, model)),
        package=canonical,
        display_seam=render.display_values,
        patch_seam=render.block_patch,
    )


#: A document declaring the policy the shipped renderer declares, so the gate is tested
#: against the real thing rather than against the one directive a substring check saw.
RENDER_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'"
)


def _document(policy: str = RENDER_CSP) -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
        "</head><body><main>*{}</main></body></html>"
    )


@asynccontextmanager
async def _client(hub: _Hub | None = None):
    """A started client that always closes.

    An ``async with`` helper rather than a fixture, by this repo's convention: the
    pinned pytest-asyncio does not collect async-generator fixtures declared with a
    plain ``@pytest.fixture``. Closing is not tidiness -- a leaked TestClient costs two
    descriptors and fails UNRELATED tests with EMFILE on a loaded runner.
    """
    app = web.Application()
    if hub is not None:
        app["state"] = hub
    routes.register_member_dashboard_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


def _q() -> str:
    return f"/api/members/{SLUG}/dashboard?member={MEMBER.replace(' ', '+')}"


# --------------------------------------------------------------------------
# 1. first load renders the whole page
# --------------------------------------------------------------------------


async def test_the_first_load_carries_every_block(store, saved):
    """Every block, not the ones some fold happened to move."""
    hub = _Hub()
    async with _client(hub) as client:
        body = await (await client.get(_q())).json()
    assert body["state"] == "live"
    assert set(body["blocks"]) == {"prs", "runs"}
    assert body["package"]["slug"] == saved.slug
    assert body["package"]["version"] == saved.version
    # The counter the browser starts from. Without it the FIRST patch has nothing to be
    # compared against and a gap at version 1 would be undetectable.
    assert body["push_version"] == 0
    assert body["push_frame"] == push.BLOCK_FRAME


async def test_arming_a_page_does_not_push_the_values_its_own_body_carries(store, saved):
    """The subscribe-time baseline is cached, not pushed.

    The bus hands a baseline over synchronously inside ``subscribe``, and that value IS
    what the first load is about to put in its response. Pushing it sends the browser a
    patch carrying what it already has -- and makes the ``push_version`` in that
    response depend on whether the patch was broadcast before or after it was read.
    """
    hub = _Hub()
    async with _client(hub) as client:
        body = await (await client.get(_q())).json()
    assert hub.frames == [], "arming the page pushed a frame"
    assert body["push_version"] == 0


async def test_the_first_load_arms_the_push_before_it_reads(store, saved):
    """Armed by the read, so patches land on a page that got a full load to apply to."""
    hub = _Hub()
    async with _client(hub) as client:
        assert (await client.get(_q())).status == 200
    page = _live_page(SLUG)
    assert page is not None
    assert page.member == MEMBER
    assert page.folds() == frozenset({"work", "panel"})


async def test_a_second_read_refreshes_one_page_rather_than_arming_a_second(store, saved):
    """Two sets of subscriptions would write two counters the browser cannot tell apart."""
    hub = _Hub()
    async with _client(hub) as client:
        await client.get(_q())
        first = _live_page(SLUG)
        await client.get(_q())
        second = _live_page(SLUG)
    assert first is second


async def test_a_reused_page_pushes_in_the_language_of_the_reader_holding_it(store, saved):
    """A push has no request to ask, so it serves the locale the page was armed with.

    Left alone on the reuse branch, that is whoever armed the page FIRST: a reader who
    switches UI language and refetches is answered in the previous reader's language,
    and every patch after it too. The reuse branch is where the current reader stands.
    """
    hub = _Hub()
    async with _client(hub) as client:
        await client.get(_q() + "&locale=ja")
        assert _live_page(SLUG).locale == "ja"
        await client.get(_q() + "&locale=de")
        page = _live_page(SLUG)
    assert page.locale == "de", "the reused page kept the first reader's language"


# --------------------------------------------------------------------------
# 2. only the blocks that subscribe to the fold
# --------------------------------------------------------------------------


async def test_a_push_carries_only_the_blocks_that_subscribe_to_the_fold(store, saved):
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    assert await page.send("work") is True
    _type, data = hub.frames[-1]
    assert set(data["blocks"]) == {"prs"}, "a block reading another fold was pushed"
    assert "runs" not in data["blocks"]
    assert data["fold"] == "work"


async def test_the_other_fold_pushes_the_other_block(store, saved):
    """The positive control for the case above: the split is real, not an empty result."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("panel", _event("panel", PANEL))
    assert await page.send("panel") is True
    _type, data = hub.frames[-1]
    assert set(data["blocks"]) == {"runs"}
    assert data["blocks"]["runs"] == ["last_run"]
    assert data["patch"]["blocks"]["runs"]["fields"]["last_run"] == PANEL["last_run"]


async def test_a_fold_under_a_key_kind_this_page_cannot_reach_is_reported_not_dropped(
    store, saved, monkeypatch
):
    """The seam with D4's third key kind.

    ``scope_for`` says which scope a fold is keyed by; ``bus_key`` says what the key IS,
    and only a SLOT key exists for a package today. A scope with no row there must be
    REPORTED unavailable rather than silently never delivered -- which is the whole
    difference between a merge that adds one row and a push that quietly loses a fold.
    """
    model = _model(store, saved.slug)
    page = _page(model, _Hub())
    monkeypatch.setattr(push, "scope_for", lambda fold: "tree")
    unavailable = page.subscribe()
    assert sorted(unavailable) == ["panel", "work"]
    assert page.bus_key("tree") == ""
    # NAMED, and nothing was subscribed under a scope with no key: the alternative is a
    # subscription that is registered and never delivers, which reads as a quiet page.
    assert page._disposers == []


async def test_a_secret_in_a_field_spec_does_not_reach_the_frame(store, monkeypatch):
    """The FORMATTER's output crosses the redactor, not only its input.

    A display string is built from the field's SPEC as well as its value, and the spec
    is agent-authored package content no redactor has seen: `format_value` appends a
    number's `unit`. So a secret written into a unit would ride out inside the display
    string while the raw value beside it was masked -- the one shape of leak a reader
    could not spot, because the page looks correctly redacted.
    """
    hub = _Hub()
    leaky = package()
    leaky["model"]["types"]["open_prs"]["unit"] = SECRET
    canonical = dp.validate_package(leaky)
    saved_pkg = store.create(name="Leaky", kind="dashboard", content=json.dumps(leaky))
    model = dp.model_of(canonical, slug=saved_pkg.slug, version=saved_pkg.version)
    page = _page(model, hub, reread=lambda _slug: (canonical, model), pkg=canonical)
    page.on_fold("work", _event("work", WORK))
    assert await page.send("work") is True

    _type, data = hub.frames[-1]
    assert SECRET not in json.dumps(data), "the secret reached the pushed frame"
    assert SECRET not in json.dumps(page.read()), "the secret reached the full read"
    # The unit is what carried it, so prove the formatter really ran and was masked
    # rather than the whole display having been dropped.
    assert data["patch"], "no patch was built, so this proves nothing about redaction"


async def test_a_slot_fold_is_keyed_by_the_crewmates_dm_slot_not_its_slug(store, saved):
    """The positive control for the case above -- and the key must be the SLOT.

    A V2 crewmate's DM log lives on a store-scoped slot key, so the bare slug names a
    different slot that nothing publishes to: keyed by slug the baseline reads nothing
    and every field renders unresolved while the crewmate's own thread is right there.
    Worse, two display names that slugify together ("Atlas" and "Member Atlas") collide
    on one slug and would then read ONE slot, so one crewmate's conversation values
    would be served onto the other's dashboard.
    """
    from kiro_crew.crew_log import bus as crew_log_bus

    page = _page(_model(store, saved.slug), _Hub())
    assert page.bus_key(crew_log_bus.SCOPE_SLOT) == SLOT
    assert page.bus_key(crew_log_bus.SCOPE_SLOT) != SLUG
    assert page.bus_key(crew_log_bus.SCOPE_SESSION) == ""


async def test_a_page_without_a_resolved_slot_is_refused_rather_than_keyed_by_slug(store, saved):
    """Refused at construction, because the fallback is the defect above.

    Defaulting to the slug would make a caller that forgot to resolve the slot produce a
    page that subscribes to an empty slot and reports nothing wrong -- the one outcome a
    version counter cannot surface. The redactor is refused on the same principle.
    """
    model = _model(store, saved.slug)
    with pytest.raises(ValueError):
        push.LivePage(
            SLUG,
            MEMBER,
            model,
            slot="",
            state=_Hub(),
            loop=None,
            redact=routes._page_safe,
            reread=lambda _slug: None,
        )


async def test_a_second_read_keeps_the_live_subscriptions_and_their_values(store, saved):
    """A poll must not empty the cache it is about to serve the full load from."""
    hub = _Hub()
    async with _client(hub) as client:
        await client.get(_q())
        page = _live_page(SLUG)
        assert page is not None
        # Above whatever revision the subscribe-time baseline recorded, so this value is
        # the newer one rather than being dropped as an older arrival.
        page.on_fold("work", _event("work", WORK, revision=10_000))
        assert page.read()["fields"]["open_prs"] == 4, "the fixture never cached a value"
        body = await (await client.get(_q())).json()
    assert body["read"]["fields"]["open_prs"] == 4, "the second read resubscribed and lost it"


async def test_a_fold_no_block_subscribes_to_sends_nothing_and_spends_no_version(store, saved):
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    assert await page.send("ledger") is False
    assert hub.frames == []
    assert page.version == 0


async def test_a_field_whose_path_does_not_resolve_is_named_not_valued(store, saved):
    """A missing value is NAMED so the page dims that cell; a None would read as zero."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", {"summary": {"open_prs": 4}}))
    await page.send("work")
    _type, data = hub.frames[-1]
    assert data["blocks"]["prs"] == ["open_prs"]
    assert data["patch"]["blocks"]["prs"]["fields"] == {"open_prs": 4}
    # The band is the PAGE's: `note` did not resolve here and `last_run` never moved.
    assert data["missing"] == ["last_run", "note"]


# --------------------------------------------------------------------------
# 3. the existing WebSocket, with a version
# --------------------------------------------------------------------------


async def test_the_push_uses_the_owner_channel_and_adds_no_second_one(store, saved):
    """One message TYPE on the hub's owner broadcast -- the one ``slot_projection`` uses."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    msg_type, _data = hub.frames[-1]
    assert msg_type == push.BLOCK_FRAME == "dashboard_block_patch"
    # An app token must never be handed this frame even if some later caller broadcasts
    # the name literally rather than through the owner-only method.
    from kiro_crew.dashboard import ws_event_scope

    assert push.BLOCK_FRAME in ws_event_scope._OWNER_ONLY_EVENTS


async def test_the_frame_carries_the_renderers_own_block_patch(store, saved):
    """``patch`` is built by the renderer's ``block_patch`` and forwarded verbatim.

    Its ``type`` is the message the DOCUMENT has a listener for, so the narrowing, the
    formatting and the wire type all come out of one Python function and the frontend
    constructs no payload at all.
    """
    from kiro_crew import dashboard_package_render as render

    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    _type, data = hub.frames[-1]
    patch = data["patch"]
    assert patch["type"] == render.BLOCK_PATCH_MESSAGE_TYPE
    assert set(patch["blocks"]) == {"prs"}
    assert patch["blocks"]["prs"]["fields"]["open_prs"] == 4
    assert patch["blocks"]["prs"]["display"]["open_prs"] == "4"
    assert patch["stale"] is True, "panel never moved, so its field is unresolved"
    assert "last_run" in patch["missing"]
    # The what-moved signal agrees with the payload, because it is derived FROM it.
    assert data["blocks"] == {"prs": sorted(patch["blocks"]["prs"]["fields"])}


async def test_a_patch_cannot_put_a_value_on_a_block_the_view_never_placed_it_on(store, saved):
    """The narrowing is the renderer's, and it is per BLOCK rather than per fold.

    Both blocks have values cached here, and the fold that moved feeds only ``prs``. So
    the patch carries ``prs`` and the field ``runs`` renders is absent from the payload
    entirely -- not merely absent from ``runs``, which is not in the patch at all.
    """
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    page.on_fold("panel", _event("panel", PANEL))
    await page.send("work")
    _type, data = hub.frames[-1]
    assert set(data["patch"]["blocks"]) == {"prs"}, "the push stopped being partial"
    assert set(data["patch"]["blocks"]["prs"]["fields"]) == {"open_prs", "note"}
    assert PANEL["last_run"] not in json.dumps(data["patch"])
    # And the page-wide band is still the page's: nothing is unresolved now.
    assert data["patch"]["stale"] is False
    assert data["patch"]["missing"] == []


async def test_the_display_strings_are_formatted_from_the_REDACTED_values(store, saved):
    """The order constraint, pinned on what the formatter was HANDED.

    A string formatted before the redactor ran would carry exactly what the redactor
    exists to mask, and it would sit beside the masked raw value where a reader is least
    likely to question it. So this asserts the seam SAW redacted input, not merely that
    the output happens to look clean -- the output could be clean by luck for one
    formatter and not for the next one.
    """
    hub = _Hub()
    model = _model(store, saved.slug)
    canonical = dp.validate_package(package())
    seen: list[Mapping[str, Any]] = []
    from kiro_crew import dashboard_package_render as render

    real_patch = render.block_patch

    def _formatter(package_arg, fields):
        seen.append(dict(fields))
        return {name: f"[{value}]" for name, value in fields.items()}

    def _recording_patch(package_arg, fields, **kwargs):
        seen.append(dict(fields))
        return real_patch(package_arg, fields, **kwargs)

    page = push.LivePage(
        SLUG,
        MEMBER,
        model,
        slot=SLOT,
        state=hub,
        loop=None,
        redact=routes._page_safe,
        reread=lambda _slug: (canonical, model),
        package=canonical,
        display_seam=_formatter,
        patch_seam=_recording_patch,
    )
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    _type, data = hub.frames[-1]
    # WHAT THE TWO SEAMS WERE GIVEN. `block_patch` formats internally, so the values
    # handed to IT are the ones that decide whether a display string can leak.
    assert seen, "neither seam was called"
    assert SECRET not in json.dumps(seen, default=str), "a seam was handed the raw value"
    # AND THE WHOLE FRAME, which is where a display string formatted too early lands.
    assert SECRET not in json.dumps(data)
    cell = data["patch"]["blocks"]["prs"]
    assert SECRET not in json.dumps(cell["display"])
    # POSITIVE CONTROL: the values that must survive DID, raw and formatted, so none of
    # the above is passing on an empty payload or a frame that never went out.
    assert cell["fields"]["open_prs"] == 4
    assert cell["display"]["open_prs"] == "4"
    assert "token" in cell["fields"]["note"]
    assert "REDACTED" in cell["fields"]["note"]


async def test_a_missing_field_gets_no_display_string_either(store, saved):
    """The page dims that cell. An empty string would render as a filled cell."""
    hub = _Hub()
    model = _model(store, saved.slug)
    page = push.LivePage(
        SLUG,
        MEMBER,
        model,
        slot=SLOT,
        state=hub,
        loop=None,
        redact=routes._page_safe,
        reread=lambda _slug: ({}, model),
        display_seam=lambda package_arg, fields: {n: str(v) for n, v in fields.items()},
    )
    # `panel` never moved, so `last_run` does not resolve.
    page.on_fold("work", _event("work", WORK))
    read = page.read()
    assert "last_run" in read["missing"]
    assert "last_run" not in read["fields"]
    assert "last_run" not in read["display"], "a dimmed cell was given a string to show"
    # Positive control: the resolved field DID get one.
    assert read["display"]["open_prs"] == "4"


async def test_the_read_carries_the_renderers_own_formatting_when_it_is_there(store, saved):
    """Through the controller's seam, so a build without the renderer sends no display."""
    hub = _Hub()
    model = _model(store, saved.slug)
    bare = push.LivePage(
        SLUG,
        MEMBER,
        model,
        slot=SLOT,
        state=hub,
        loop=None,
        redact=routes._page_safe,
        reread=lambda _slug: ({}, model),
    )
    bare.on_fold("work", _event("work", WORK))
    assert "display" not in bare.read()

    page = push.LivePage(
        SLUG,
        MEMBER,
        model,
        slot=SLOT,
        state=hub,
        loop=None,
        redact=routes._page_safe,
        reread=lambda _slug: ({}, model),
        display_seam=lambda package_arg, fields: {k: f"<{v}>" for k, v in fields.items()},
    )
    page.on_fold("work", _event("work", WORK))
    assert page.read()["display"]["open_prs"] == "<4>"


async def test_a_refetch_frame_carries_no_patch_either(store, saved):
    """No values means none of them -- not "none in blocks and all of them in a patch"."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub, reread=lambda _slug: None)
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    _type, data = hub.frames[-1]
    assert data["refetch"] is True
    assert data["blocks"] == {}
    assert data["patch"] == {}


async def test_the_body_names_the_message_type_the_document_accepts(store, saved):
    """Fixed by the repository, not invented here: the renderer imports this constant."""
    from kiro_crew import dashboard_frame

    hub = _Hub()
    async with _client(hub) as client:
        body = await (await client.get(_q())).json()
    from kiro_crew import dashboard_package_render as render

    assert body["page_message"] == dashboard_frame.DATA_MESSAGE_TYPE
    # BOTH types, so the frontend holds neither constant. The patch one is the renderer's.
    assert body["page_patch_message"] == render.BLOCK_PATCH_MESSAGE_TYPE
    assert body["page_message"] != body["page_patch_message"]
    assert body["read"]["fields"] == body["read"]["fields"]  # present and serializable
    assert set(body["read"]) >= {"fields", "missing", "stale", "agentic"}


async def test_the_version_steps_by_exactly_one_per_frame(store, saved):
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    for revision in (1, 2, 3):
        page.on_fold("work", _event("work", WORK, revision=revision))
        await page.send("work")
    assert [data["version"] for _t, data in hub.frames] == [1, 2, 3]


async def test_a_frame_names_the_layout_it_was_computed_under(store, saved):
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    _type, data = hub.frames[-1]
    assert data["layout"] == saved.version


async def test_an_older_revision_is_dropped_rather_than_pushed(store, saved):
    """Ordered by REVISION, never by arrival: a seq can move down while a value moves on."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK, revision=5))
    await page.send("work")
    page.on_fold("work", _event("work", {"summary": {"open_prs": 999}}, revision=2))
    await page.send("work")
    assert hub.frames[-1][1]["patch"]["blocks"]["prs"]["fields"]["open_prs"] == 4


# --------------------------------------------------------------------------
# 4. a gap OR a layout change -> refetch. Both.
# --------------------------------------------------------------------------


async def test_a_layout_change_sends_a_refetch_carrying_no_values(store, saved):
    """The block set may have moved, so values under the new layout must not be applied."""
    hub = _Hub()
    model = _model(store, saved.slug)
    recomposed = store.update(
        saved.slug,
        content=json.dumps(package(blocks=[{"id": "prs", "type": "stat", "fields": ["open_prs"]}])),
    )
    assert recomposed.version != saved.version, "the fixture did not change the layout"
    page = _page(
        model, hub, reread=lambda _slug: (dp.validate_package(package()), _model(store, saved.slug))
    )
    page.on_fold("work", _event("work", WORK))
    assert await page.send("work") is True
    _type, data = hub.frames[-1]
    assert data["refetch"] is True
    assert data["reason"] == push.REASON_LAYOUT
    assert data["blocks"] == {}
    assert data["layout"] == recomposed.version


async def test_a_layout_change_drops_the_old_layouts_subscriptions(store, saved):
    """The refetch REUSES this page, so stale subscriptions would never be replaced.

    `open_page` reuses a page whose layout and fingerprint match the Model it is handed,
    and `ensure_subscribed` returns early while anything is subscribed. So if the swap
    kept the old Model's subscriptions, a fold the NEW layout adds would never be
    subscribed and its fields would read missing for as long as the tab stayed open --
    silently, because the refetch itself succeeds.
    """
    hub = _Hub()
    model = _model(store, saved.slug)
    store.update(
        saved.slug,
        content=json.dumps(package(blocks=[{"id": "prs", "type": "stat", "fields": ["open_prs"]}])),
    )
    page = _page(
        model, hub, reread=lambda _slug: (dp.validate_package(package()), _model(store, saved.slug))
    )
    page.subscribe()
    assert page._disposers, "the fixture did not subscribe anything to drop"
    assert await page.send("work") is True
    assert hub.frames[-1][1]["reason"] == push.REASON_LAYOUT
    assert page._disposers == [], "the new Model inherited the old layout's subscriptions"
    # And the reused page subscribes again, rather than reporting itself already done.
    assert page.ensure_subscribed() == []
    assert page._disposers


async def test_a_crewmates_own_write_is_pushed_like_any_other_fold(store, saved):
    """An agentic field carries no ``fold`` key, and matching on one finds nothing.

    `_validate_source` allows an agentic source to be exactly ``{"agentic": True}``: the
    crewmate's own write is what moves it, and that write lands on the agentic fold --
    which this page subscribes to and caches. Read the ``fold`` key alone and
    ``send(AGENTIC_FOLD)`` matches no field at all, so the value is cached and never
    sent, and the page shows a crewmate's own number only after a reload.
    """
    hub = _Hub()
    written = package()
    written["model"]["types"]["headline"] = {
        "type": "text",
        "label": "Headline",
        "source": {"agentic": True},
    }
    written["view"]["blocks"] = [
        {"id": "prs", "type": "table", "fields": ["open_prs", "note"]},
        {"id": "said", "type": "stat", "fields": ["headline"]},
    ]
    canonical = dp.validate_package(written)
    saved_pkg = store.update(saved.slug, content=json.dumps(written))
    model = dp.model_of(canonical, slug=saved.slug, version=saved_pkg.version)
    page = _page(model, hub, reread=lambda _slug: (canonical, model), pkg=canonical)
    page.on_fold(
        push.AGENTIC_FOLD,
        _event(
            push.AGENTIC_FOLD, {"fields": {"headline": {"value": "shipped", "at": "2026-10-10"}}}
        ),
    )
    assert await page.send(push.AGENTIC_FOLD) is True
    _type, data = hub.frames[-1]
    assert data["fold"] == push.AGENTIC_FOLD
    # Only the block that renders it, and the value itself went out.
    assert set(data["blocks"]) == {"said"}
    assert data["blocks"]["said"] == ["headline"]
    assert "shipped" in json.dumps(data["patch"])


async def test_the_seq_of_an_agentic_field_comes_from_the_agentic_folds_own_row(store, saved):
    """The same mapping, read for the patch's ``seq``.

    Keyed by the missing ``fold`` key it looks the cell up under ``""``, finds nothing,
    and the patch claims ``seq=0`` for a row the fold recorded at a real sequence.
    """
    hub = _Hub()
    written = package()
    written["model"]["types"] = {
        "headline": {"type": "text", "label": "Headline", "source": {"agentic": True}}
    }
    written["view"]["blocks"] = [{"id": "said", "type": "stat", "fields": ["headline"]}]
    canonical = dp.validate_package(written)
    saved_pkg = store.update(saved.slug, content=json.dumps(written))
    model = dp.model_of(canonical, slug=saved.slug, version=saved_pkg.version)
    page = _page(model, hub, reread=lambda _slug: (canonical, model), pkg=canonical)
    page.on_fold(
        push.AGENTIC_FOLD,
        _event(
            push.AGENTIC_FOLD,
            {"fields": {"headline": {"value": "shipped", "at": "2026-10-10"}}},
            seq=41,
        ),
    )
    moved, _missing, seq, _stale = page._moved(push.AGENTIC_FOLD)
    assert moved == {"headline": "shipped"}
    assert seq == 41


async def test_the_gap_a_restart_makes_is_a_gap_the_page_can_see(store, saved):
    """The version the page holds and the one a fresh page sends cannot be confused.

    A gateway restarted inside one interpreter arms a NEW page whose counter starts at
    zero, so its first frame carries version 1 while the browser holds 7. The rule the
    page applies is ``held + 1``, not ``> held``, which is why that reads as a gap and
    refetches rather than being applied over newer values.
    """
    hub = _Hub()
    model = _model(store, saved.slug)
    old = _page(model, hub)
    for revision in range(1, 8):
        old.on_fold("work", _event("work", WORK, revision=revision))
        await old.send("work")
    assert hub.frames[-1][1]["version"] == 7
    fresh = _page(model, hub)
    fresh.on_fold("work", _event("work", WORK))
    await fresh.send("work")
    assert hub.frames[-1][1]["version"] == 1


# --------------------------------------------------------------------------
# 5. the owner check and the redaction stay in the controller
# --------------------------------------------------------------------------


async def test_a_package_this_member_does_not_own_is_not_pushed(store, tmp_path):
    """The negative one. A package bound elsewhere arms nothing and sends nothing."""
    other = store.create(
        name="Someone else",
        kind="dashboard",
        content=json.dumps(package(bound_to="crewmate:other")),
    )
    hub = _Hub()
    model = _model(store, other.slug)
    request = SimpleNamespace(app={"state": hub})
    assert routes.arm_block_push(request, SLUG, MEMBER, model, slot=SLOT) is None
    assert _live_page(SLUG) is None
    assert hub.frames == []


async def test_a_session_bound_package_is_not_a_members(store):
    """A slot key and a member slug look alike, which is the case a loose check misses."""
    model = _model(
        store,
        store.create(
            name="Slot page",
            kind="dashboard",
            content=json.dumps(package(bound_to=f"session:{SLUG}")),
        ).slug,
    )
    assert routes._owns_package(SLUG, model) is False


async def test_a_rebind_stops_the_push_and_asks_for_a_refetch(store, saved):
    """A rebind writes no version, so the layout check cannot see it: the re-read does."""
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub, reread=lambda _slug: None)
    page.on_fold("work", _event("work", WORK))
    assert await page.send("work") is True
    _type, data = hub.frames[-1]
    assert data["refetch"] is True
    assert data["reason"] == push.REASON_UNBOUND
    assert data["blocks"] == {}
    # And it is over for good: a closed page never sends another frame.
    page.on_fold("work", _event("work", WORK, revision=9))
    assert await page.send("work") is False
    assert len(hub.frames) == 1


async def test_a_redacted_value_is_redacted_in_the_whole_pushed_payload(store, saved):
    """Asserted over the WHOLE serialized frame, not the one field a reader would check.

    The same value can reappear on a block id, a key, or a sibling field, and a
    per-field assertion would not see it.
    """
    hub = _Hub()
    page = _page(_model(store, saved.slug), hub)
    page.on_fold("work", _event("work", WORK))
    await page.send("work")
    _type, data = hub.frames[-1]
    serialized = json.dumps(data)
    assert SECRET not in serialized
    # Both halves of the frame carry values, so BOTH have to be covered -- `blocks` and
    # the `read` the document is actually given. The whole-payload assertion above is
    # what catches the second one; these name them so a reader can see it did.
    assert SECRET not in json.dumps(data["blocks"])
    assert SECRET not in json.dumps(data["patch"])
    # POSITIVE CONTROL: the values that must survive DID, so this is not passing on an
    # empty payload or a frame that never went out.
    assert data["blocks"]["prs"] == ["note", "open_prs"]
    assert data["patch"]["blocks"]["prs"]["fields"]["open_prs"] == 4


async def test_the_push_cannot_be_armed_without_the_controllers_redactor():
    """Refused rather than defaulted: a default here would be a SECOND redactor."""
    with pytest.raises(TypeError):
        push.LivePage(
            SLUG,
            MEMBER,
            SimpleNamespace(),
            slot=SLOT,
            state=None,
            loop=None,
            redact=None,
            reread=lambda s: None,
        )


async def test_the_controller_is_what_hands_the_push_its_redactor(store, saved):
    hub = _Hub()
    request = SimpleNamespace(app={"state": hub})
    page = routes.arm_block_push(request, SLUG, MEMBER, _model(store, saved.slug), slot=SLOT)
    assert page is not None
    assert page._redact is routes._page_safe


# --------------------------------------------------------------------------
# 6. the builtin fallback is scoped by SOURCE (conductor's cycle-2 ruling)
# --------------------------------------------------------------------------


async def test_a_package_bound_member_gets_no_builtin_fallback(store, saved, monkeypatch):
    """v3 has no default page, so a package-bound crewmate is never handed a shipped one.

    The bound-but-unreadable case, which is the only one that can reach the fallback at
    all: a servable package returns above it.
    """
    called: list[str] = []

    def _default(slug: str):
        called.append(slug)
        raise AssertionError("a package-bound member must not be handed a builtin page")

    monkeypatch.setattr(routes.instance, "default_instance", _default)
    monkeypatch.setattr(routes, "_read_package", lambda slug: routes._Packaged(bound=True))
    hub = _Hub()
    async with _client(hub) as client:
        body = await (await client.get(_q())).json()
    assert called == []
    assert body["state"] == "empty"
    assert "rendered_html" not in body


async def test_a_registry_bound_member_still_gets_the_default(store, monkeypatch):
    """The other direction. The shipped templates are a live feature and stay one."""
    fallback = SimpleNamespace(
        state=routes.instance.STATE_EMPTY,
        wire=lambda: {"instance_version": 0, "state": "empty", "html": "<p>x</p>", "manifest": {}},
    )
    seen: list[str] = []

    def _default(slug: str):
        seen.append(slug)
        return fallback

    monkeypatch.setattr(routes.instance, "default_instance", _default)
    monkeypatch.setattr(routes, "_render", lambda *a, **k: "<html>composed</html>")
    async with _client() as client:
        body = await (await client.get(_q())).json()
    assert seen == [SLUG], "the registry fallback stopped being offered"
    assert body["rendered_html"] == "<html>composed</html>"


async def test_the_read_tells_bound_and_unreadable_from_not_bound(store, saved, monkeypatch):
    """THREE states, and the third is the one the ruling turns on.

    The third is reached the way it is reached in production: the binding scan FINDS
    the record, and the validating read of its content then fails -- the content moved
    under the read, or an older layout does not satisfy the validator.

    Asserted on the ANSWER, not on how many times anything was called. The scan matches
    off the stored envelope rather than off a validated package, precisely so a layout
    the current validator rejects is still found and reported broken by this caller; a
    pin on the call count would say the opposite of that design and would break again
    the next time the scan gets cheaper.
    """
    assert routes._read_package("nobody") == routes._Packaged(bound=False)
    answered = routes._read_package(SLUG)
    assert answered.bound is True and answered.model is not None

    def _the_content_moved(content: str):
        raise ValueError("the content moved under the read")

    # Only the validating read is broken. The envelope the scan matches on is
    # untouched, so the binding is still FOUND -- which is the whole distinction.
    monkeypatch.setattr(dp, "parse_package", _the_content_moved)
    broken = routes._read_package(SLUG)
    assert broken.bound is True, "a package bound here read as absent"
    assert broken.model is None
    assert broken.package is None


async def test_a_failed_package_scan_suppresses_the_fallback_rather_than_guessing(
    store, saved, monkeypatch
):
    """An unknown answer must not hand a crewmate a page they did not compose."""
    monkeypatch.setattr(dp, "resolve_bound_slug", _raise)
    answered = routes._read_package(SLUG)
    assert answered.bound is True
    assert answered.model is None


def _raise(*_a, **_k):
    raise ValueError("not a package")


# --------------------------------------------------------------------------
# 7. the v3 page earns execution at its OWN gate
# --------------------------------------------------------------------------


def _stub_renderer(monkeypatch, document: str) -> None:
    """Make the renderer compose *document*, by patching the renderer itself.

    The controller imports ``dashboard_package_render`` and calls it by name, so this is
    the only place a stub can go. Patching the real module rather than an indirection in
    the controller is also what keeps these tests honest: a renamed ``render_dashboard``
    makes ``monkeypatch.setattr`` raise here instead of being silently absorbed.
    """
    from kiro_crew import dashboard_package_render as render

    monkeypatch.setattr(render, "render_dashboard", lambda *a, **k: document)


async def test_the_shipped_policy_passes_the_gate(monkeypatch):
    """The positive control. The real `RENDER_CSP` is what the gate must admit."""
    _stub_renderer(monkeypatch, _document())
    assert routes._minted_package_page(SLUG, package(), {}) is not None


@pytest.mark.parametrize(
    "policy",
    [
        pytest.param("", id="no-policy-at-all"),
        pytest.param(
            RENDER_CSP.replace("connect-src 'none'", "connect-src https://example.test"),
            id="can-reach-the-network",
        ),
        pytest.param(RENDER_CSP + "; script-src 'unsafe-eval'", id="can-compile-what-it-is-given"),
        pytest.param(
            RENDER_CSP.replace("form-action 'none'", "form-action *"), id="can-post-somewhere"
        ),
        pytest.param(
            RENDER_CSP.replace("img-src data:", "img-src http:"), id="can-exfiltrate-by-image"
        ),
        pytest.param(
            RENDER_CSP.replace("base-uri 'none'", "base-uri https://example.test"),
            id="can-move-its-own-base",
        ),
    ],
)
async def test_a_document_that_could_carry_the_numbers_somewhere_is_refused(
    monkeypatch, policy: str
):
    """The gate is the list of properties the safety argument rests on, not one string.

    Every case here CONTAINS ``default-src 'none'`` -- the shipped policy starts with
    exactly those bytes -- so a substring check for it would pass all of them while the
    justification beside it read as false.
    """
    body = "<html><head></head><body></body></html>" if not policy else _document(policy)
    _stub_renderer(monkeypatch, body)
    assert routes._minted_package_page(SLUG, package(), {}) is None


async def test_the_gate_names_what_is_wrong_with_the_policy():
    """A refusal a reviewer can act on: which directive, not that something failed."""
    assert routes._csp_problems(_document()) == []
    assert routes._csp_problems("<html></html>") == ["declares no Content-Security-Policy"]
    missing = routes._csp_problems(_document("default-src 'none'"))
    assert "is missing \"connect-src 'none'\"" in missing
    assert routes._csp_problems(_document(RENDER_CSP + "; script-src 'unsafe-eval'")) == [
        "grants \"'unsafe-eval'\""
    ]


async def test_a_block_type_outside_the_closed_catalogue_is_not_rendered(monkeypatch):
    """Re-checked at the RENDER site, like ``_trusted_page`` not trusting a stored label."""
    from kiro_crew import dashboard_package_render as render

    rendered: list[str] = []

    def _render(*_a, **_k):
        rendered.append("ran")
        return _document()

    monkeypatch.setattr(render, "render_dashboard", _render)
    pkg = package()
    pkg["view"]["blocks"] = [{"id": "prs", "type": "iframe", "fields": ["open_prs"]}]
    assert routes._minted_package_page(SLUG, pkg, {}) is None
    assert rendered == [], "the renderer ran for a block type nothing in the repo defines"


async def test_the_renderer_is_called_with_what_it_declares_and_its_output_is_admitted():
    """No stub: the real renderer, called the way the controller calls it.

    The controller IMPORTS this module and calls it by name, so a rename or a moved
    module is an ``ImportError`` on the request path -- loud, and not something a test
    has to pin. What a direct import does NOT catch is a changed SIGNATURE: a
    ``render_dashboard`` taking different keywords still imports, then raises inside the
    gate, which logs and returns ``None`` -- a page served with no ``rendered_html``,
    which this controller draws as the frame's empty state. Nobody goes looking at that,
    so the signature is pinned here and the call is made for real.
    """
    import inspect

    from kiro_crew import dashboard_package_render as render

    assert list(inspect.signature(render.render_dashboard).parameters) == [
        "package",
        "read",
        "theme",
        "title",
    ]
    document = render.render_dashboard(
        package(), {"fields": {}, "missing": [], "stale": False}, theme="dark"
    )
    assert isinstance(document, str) and document
    # Through the controller's OWN gate, so this covers the call and the admission.
    assert routes._csp_problems(document) == []


async def test_the_first_load_really_renders_a_document(store, saved):
    """END TO END with the real renderer: a package-bound member gets a page.

    The one assertion that would have caught the broken seam on the merged tree. It
    stubs nothing: the route reads the package, builds the read, calls the renderer the
    constant names, and the document goes through the CSP gate before it is served.
    """
    hub = _Hub()
    async with _client(hub) as client:
        body = await (await client.get(_q())).json()
        assert "rendered_html" in body, "the seam resolved to nothing and the page was empty"
        document = body["rendered_html"]
        assert document.lstrip().lower().startswith("<!doctype html")
        assert routes._csp_problems(document) == []
        # The VALUES reached it, so this is a filled page and not an empty shell that
        # happens to parse: the fold moved, then the next read served it.
        page = _live_page(SLUG)
        assert page is not None
        page.on_fold("work", _event("work", WORK, revision=10_000))
        filled = await (await client.get(_q())).json()
    assert "4" in filled["rendered_html"]
    assert filled["read"]["display"]["open_prs"] == "4", "the renderer's formatter was skipped"


async def test_an_unreadable_package_leaves_an_adopted_template_rendering(store, monkeypatch):
    """The fallback suppression is scoped to an EMPTY instance, and that is the whole rule.

    A crewmate bound to a package nobody can read, who has ALSO adopted a template, is
    not in the empty state: ``api_member_dashboard`` only withholds the builtin for
    ``STATE_EMPTY``, so a live adopted copy still composes. Pinned because the feature
    map said the empty state in every unreadable case, which is the broader claim this
    case refutes -- a crewmate who adopted a page keeps seeing it.
    """
    composed: list[str] = []

    def _render(*_a, **_k):
        composed.append("composed")
        return "<html>adopted</html>"

    record = SimpleNamespace(
        state=routes.instance.STATE_LIVE,
        manifest={"id": "demo", "version": 1, "fields": {}},
        wire=lambda: {"instance_version": 4, "state": "live", "html": "<p>x</p>", "manifest": {}},
    )

    def _default(slug: str):
        raise AssertionError("the builtin fallback is for an empty instance only")

    monkeypatch.setattr(routes.instance, "read", lambda _slug: record)
    monkeypatch.setattr(routes.instance, "default_instance", _default)
    monkeypatch.setattr(routes, "_read_package", lambda _slug: routes._Packaged(bound=True))
    monkeypatch.setattr(routes, "_render", _render)
    async with _client() as client:
        body = await (await client.get(_q())).json()
    assert body["state"] == "live", "an adopted template stopped rendering"
    assert composed == ["composed"], "the adopted copy was not composed"
    assert body["rendered_html"] == "<html>adopted</html>"


async def test_a_credential_in_a_unit_does_not_reach_the_served_document(store, saved):
    """The SPEC is redacted before the renderer reads it, not only after it writes.

    A field's spec is agent-authored and the renderer CONCATENATES parts of it into
    strings: ``format_value`` appends a ``unit`` to the value. Scrubbing only the
    result is not enough, because the redactor's pattern for an assignment does not
    match what that join produces: it masks the ``KEY=`` prefix and leaves the key BODY
    in the page.

    So the assertion is on the key body alone, never on the wrapper: a page that has
    masked the prefix and kept the secret would pass a check written against the whole
    string.
    """
    key = "abcdefghij0123456789ABCDEFGHIJ0123456789"
    written = package()
    written["model"]["types"]["open_prs"]["unit"] = f'SecretAccessKey="{key}"'
    store.update(saved.slug, content=json.dumps(written))
    hub = _Hub()
    async with _client(hub) as client:
        resp = await client.get(_q())
        body = await resp.json()
    assert resp.status == 200, await resp.text()
    assert "rendered_html" in body, "the page was refused, so this proves nothing"
    assert key not in body["rendered_html"], "the credential body reached the document"
    assert key not in json.dumps(body), "the credential body reached the response"


async def test_a_credential_shaped_field_name_is_refused_rather_than_served(store, saved):
    """A declared NAME is an identifier, so a read carrying an unsafe one is refused.

    ``[a-z][a-z0-9_]{0,63}`` is satisfied by ``ghp_`` and 36 lowercase characters, so
    an agent-authored field name can BE a token. The values and ``display`` already
    cross ``_page_safe``; the names travel as KEYS -- ``missing``, ``read.agentic``,
    the frame's ``blocks`` -- one key away from the value the same redactor masked.

    Redacting the name is not the fix: the read's own keys, the frame's blocks and the
    composed document's cell ids are joined by it, so a rewritten name leaves a page
    whose cells resolve to nothing. This answers the bound-and-unserveable state
    instead, and no name leaves at all.
    """
    secret_name = "ghp_" + "a" * 36
    written = package()
    written["model"]["types"][secret_name] = {
        "type": "text",
        "label": "Headline",
        "source": {"agentic": True},
    }
    written["view"]["blocks"] = [{"id": "said", "type": "stat", "fields": [secret_name]}]
    store.update(saved.slug, content=json.dumps(written))
    async with _client(_Hub()) as client:
        resp = await client.get(_q())
        body = await resp.json()
    assert resp.status == 200, await resp.text()
    assert body["state"] == "empty", "a package this page cannot carry was served anyway"
    assert "rendered_html" not in body
    assert secret_name not in json.dumps(body), "the field name reached the response"


async def test_a_credential_shaped_manifest_field_name_is_refused_too(store, monkeypatch):
    """The SAME rule on the template line, which is why it does not live in the package.

    A template manifest's field names carry the same grammar, and that line is the one
    this product already serves -- so the package branch alone would leave the hole
    open where it is reachable today. The manifest goes out empty and nothing is
    composed, so no name leaves by either half of the body.
    """
    secret_name = "ghp_" + "b" * 36
    manifest = {"id": "demo", "version": 1, "fields": {secret_name: {"type": "string"}}}
    record = SimpleNamespace(
        state=routes.instance.STATE_LIVE,
        manifest=manifest,
        wire=lambda: {
            "instance_version": 1,
            "state": "live",
            "html": "<p>x</p>",
            "manifest": dict(manifest),
        },
    )
    composed: list[str] = []

    def _render(*_a, **_k):
        composed.append("composed")
        return "<html>page</html>"

    monkeypatch.setattr(routes.instance, "read", lambda _slug: record)
    monkeypatch.setattr(routes, "_read_package", lambda _slug: routes._Packaged(bound=False))
    monkeypatch.setattr(routes, "_render", _render)
    async with _client() as client:
        body = await (await client.get(_q())).json()
    assert composed == [], "the page was composed from a manifest this read refuses"
    assert body["manifest"] == {}
    assert "html" not in body
    assert "rendered_html" not in body
    assert secret_name not in json.dumps(body), "the manifest field name reached the response"


async def test_a_crewmates_own_written_value_is_on_the_page_this_route_serves(store, saved):
    """The far half of a write landing: the page a reader opens carries the value.

    The write route checks a value against this package's Model and records it on the
    crewmate's own fold -- :data:`push.AGENTIC_FOLD`, which is the projection kernel's
    own name for that fold, so a writer and this reader cannot drift onto two
    spellings. What this case pins is the rest of that path: the next read composes the
    document from the same package, and the value is IN the document and in the
    formatted read beside it.

    The pair to the write route's own case in ``test_agent_panel_routes.py``. That one
    proves a package-bound write is kept; a value kept where nothing draws it is the
    failure this half rules out.
    """
    written = package()
    written["model"]["types"]["headline"] = {
        "type": "text",
        "label": "Headline",
        "source": {"agentic": True},
    }
    written["view"]["blocks"] = [{"id": "said", "type": "stat", "fields": ["headline"]}]
    store.update(saved.slug, content=json.dumps(written))
    hub = _Hub()
    async with _client(hub) as client:
        assert (await (await client.get(_q())).json())["state"] == "live"
        page = _live_page(SLUG)
        assert page is not None, "the first read armed no page to carry the fold"
        page.on_fold(
            push.AGENTIC_FOLD,
            _event(
                push.AGENTIC_FOLD,
                {"fields": {"headline": {"value": "shipped", "at": "2026-10-10"}}},
                revision=10_000,
            ),
        )
        served = await (await client.get(_q())).json()
    assert served["read"]["display"]["headline"] == "shipped"
    assert "shipped" in served["rendered_html"], "the value is in the read and not on the page"


async def test_the_read_serves_the_field_the_tab_mounts(store, saved):
    """THE CROSS-LANGUAGE JOIN this page hangs on, and a field name is all of it.

    The tab's promote effect mounts one field of this body and draws its unavailable
    state for a body without it. Neither language fails to compile when the two names
    disagree: a rename on this side serves a 200 carrying a composed document under a
    key nothing reads, and the crewmate gets the notice where their page drew.

    SCRAPED out of the component rather than restated, the same way
    ``test_the_sandbox_grant_is_scripts_only`` reads its grant: a second copy of the
    name here would agree with itself for as long as it existed.
    """
    import re
    from pathlib import Path

    component = (
        Path(__file__).resolve().parents[1]
        / "website"
        / "src"
        / "pages"
        / "members"
        / "CrewDynamicDashboard.tsx"
    )
    source = component.read_text(encoding="utf-8")
    mounted = re.findall(r"html: data\.([a-z_]+)", source)
    assert mounted, "the tab's promote effect mounts no field of this body"
    hub = _Hub()
    async with _client(hub) as client:
        body = await (await client.get(_q())).json()
    assert body["state"] == "live", "the package-bound read did not reach the package branch"
    for field in dict.fromkeys(mounted):
        assert field in body, f"the tab mounts {field!r} and this read does not carry it"


async def test_a_package_page_never_reaches_the_template_execution_gate():
    """Which is WHY ``instance.RENDERABLE_SOURCES`` stays at one value.

    ``_trusted_page`` is the gate that set guards, and it answers by loading markup from
    the template CATALOG by the record's ``template_id``. A package has no catalog
    directory and carries no markup key at all, so widening the set would loosen the
    template adopt path (``instance._check``) and buy this page nothing.
    """
    from kiro_crew.dashboard_templates import catalog
    from kiro_crew.dashboard_templates import instance as instance_store

    assert instance_store.RENDERABLE_SOURCES == frozenset({catalog.BUILTIN_SOURCE})
    assert "package" not in instance_store.RENDERABLE_SOURCES
    # The structural half of the claim: a validated package has no key that can carry
    # markup or script, so there is no stored page for that gate to be asked about.
    assert set(dp.validate_package(package())) == {"kind", "bound_to", "model", "view", "theme"}
