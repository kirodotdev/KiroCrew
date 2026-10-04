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

from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates import catalog, instance

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
def _env(tmp_path, monkeypatch):
    """One fixture template, an isolated home, and the member checks stubbed.

    The member-identity checks are the SIBLING routes' and are tested there; what is
    stubbed is config loading, not the checks themselves -- ``_member_names_for_slug``
    still runs, so the shared-slug refusal below is the real code path.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    builtin = tmp_path / "builtin" / "fixture-board"
    builtin.mkdir(parents=True)
    (builtin / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    (builtin / "template.html").write_text(PAGE, encoding="utf-8")
    monkeypatch.setattr(catalog, "builtin_dir", lambda: builtin.parent)

    cfg = SimpleNamespace(agents={MEMBER: SimpleNamespace(member_id="")})
    monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(routes.members_mod, "member_slug", lambda name, config=None: SLUG)
    monkeypatch.setattr(routes.members_mod, "validate_slug", lambda slug: slug)
    monkeypatch.setattr(routes.members_mod, "is_dispatchable_member_name", bool)
    monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: [MEMBER])
    # No app token, and the owner gate open: both are the sibling routes' boundaries
    # and are asserted structurally below rather than re-tested here.
    monkeypatch.setattr(routes, "_deny_app_caller", _none)
    monkeypatch.setattr(routes, "_owner_only", _none)
    # No DM session in a test, so a change records no history entry. That is the
    # documented degradation: the record is the file, the entry is history.
    monkeypatch.setattr(routes, "_write_session", lambda slug, member: "")
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


async def test_the_body_is_the_shape_the_contract_fixes():
    async with _client() as client:
        await client.post(_q("/adopt"), json={"template_id": "fixture-board"})
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
        await client.post(_q("/adopt"), json={"template_id": "fixture-board"})
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
        await client.post(_q("/adopt"), json={"template_id": "fixture-board"})
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
        await client.post(_q("/adopt"), json={"template_id": "fixture-board"})
        resp = await client.get(_q(""))
        body = await resp.json()
    assert resp.status == 200 and body["state"] == "live" and "rendered_html" not in body


async def test_adopt_then_edit_then_rollback_walks_the_versions():
    async with _client() as client:
        adopted = await (
            await client.post(_q("/adopt"), json={"template_id": "fixture-board"})
        ).json()
        assert adopted["instance_version"] == 1

        edited = await (await client.post(_q("/edit"), json={"html": PAGE_EDITED})).json()
        assert edited["instance_version"] == 2 and edited["html"] == PAGE_EDITED

        rolled = await (await client.post(_q("/rollback"), json={"to_version": 1})).json()
        # FORWARD: version three holding version one's page.
        assert rolled["instance_version"] == 3 and rolled["html"] == PAGE


async def test_adopting_an_unknown_template_is_refused_with_its_own_code():
    async with _client() as client:
        resp = await client.post(_q("/adopt"), json={"template_id": "nope"})
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "dashboard_refused" and "fixture-board" in body["error"]


async def test_adopt_without_a_template_id_is_refused():
    async with _client() as client:
        resp = await client.post(_q("/adopt"), json={})
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_template_id"


async def test_an_edit_that_breaks_parity_is_refused_and_does_not_move_the_version():
    async with _client() as client:
        await client.post(_q("/adopt"), json={"template_id": "fixture-board"})
        resp = await client.post(
            _q("/edit"), json={"html": PAGE + '<u data-dashboard-field="ghost"></u>'}
        )
        assert resp.status == 409
        assert "does not declare" in (await resp.json())["error"]
        assert (await (await client.get(_q(""))).json())["instance_version"] == 1


@pytest.mark.parametrize(
    "body, code",
    [
        ({}, "empty_edit"),
        ({"html": 5}, "bad_html"),
        ({"manifest": "not an object"}, "bad_manifest"),
    ],
)
async def test_an_edit_refuses_a_body_it_cannot_use(body, code):
    async with _client() as client:
        await client.post(_q("/adopt"), json={"template_id": "fixture-board"})
        resp = await client.post(_q("/edit"), json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == code


@pytest.mark.parametrize("to_version", [0, -1, "1", True])
async def test_a_rollback_refuses_a_version_that_is_not_one(to_version):
    async with _client() as client:
        await client.post(_q("/adopt"), json={"template_id": "fixture-board"})
        resp = await client.post(_q("/rollback"), json={"to_version": to_version})
        assert resp.status == 400
        assert (await resp.json())["code"] == "bad_to_version"


async def test_the_history_route_reports_every_change_and_the_kept_versions():
    async with _client() as client:
        await client.post(_q("/adopt"), json={"template_id": "fixture-board"})
        await client.post(_q("/edit"), json={"html": PAGE_EDITED})
        body = await (await client.get(_q("/history"))).json()
        assert [row["action"] for row in body["history"]] == ["adopted", "edited"]
        assert body["versions"] == [1, 2]


# --------------------------------------------------------------------------
# registry, share, snapshots
# --------------------------------------------------------------------------


async def test_the_template_list_carries_the_rows_and_the_problems():
    async with _client() as client:
        broken = catalog.user_dir() / "broken"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text("{nope", encoding="utf-8")
        (broken / "template.html").write_text(PAGE, encoding="utf-8")
        body = await (await client.get(_q("/templates"))).json()
        assert [row["id"] for row in body["templates"]] == ["fixture-board"]
        assert [p["name"] for p in body["problems"]] == ["broken"]
        # No page on a row: a list of every template must not be a list of every page.
        assert "html" not in body["templates"][0]


async def test_export_then_import_lands_a_shared_template_the_adopt_route_refuses():
    """The import route stores and lists the file; the adopt route will not render it.

    Both halves in one test because the pair IS the rule: an imported page is kept, so
    P2's picker has something to review, and is refused as a live dashboard, so no page
    an arbitrary sender wrote ever runs against this crewmate's task titles.
    """
    async with _client() as client:
        exported = await (await client.get(_q("/export", template_id="fixture-board"))).json()
        imported = await (
            await client.post(_q("/import"), json={"file": exported["file"], "as_id": "borrowed"})
        ).json()
        assert imported["template"]["id"] == "borrowed"
        assert imported["template"]["source"] == "shared"
        listed = await (await client.get(_q("/templates"))).json()
        assert "borrowed" in {row["id"] for row in listed["templates"]}
        resp = await client.post(_q("/adopt"), json={"template_id": "borrowed"})
        assert resp.status == 409
        assert "cannot be adopted" in (await resp.json())["error"]


async def test_an_import_that_collides_is_refused():
    async with _client() as client:
        exported = await (await client.get(_q("/export", template_id="fixture-board"))).json()
        resp = await client.post(_q("/import"), json={"file": exported["file"]})
        assert resp.status == 409
        assert "already uses the id" in (await resp.json())["error"]


async def test_exporting_an_unknown_template_is_a_404():
    async with _client() as client:
        resp = await client.get(_q("/export", template_id="nope"))
        assert resp.status == 404
        assert (await resp.json())["code"] == "template_not_found"


async def test_an_import_with_no_file_is_refused():
    async with _client() as client:
        resp = await client.post(_q("/import"), json={})
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_file"


async def test_a_snapshot_takes_its_versions_from_the_instance():
    async with _client() as client:
        await client.post(_q("/adopt"), json={"template_id": "fixture-board"})
        await client.post(_q("/edit"), json={"html": PAGE_EDITED})
        body = await (
            await client.post(_q("/snapshot"), json={"fields": {"credits": 12.5}, "seq": 418})
        ).json()
        taken = body["snapshot"]
        # The versions come from the instance, never from the caller: a snapshot's value
        # is that the values and the page that laid them out were read together.
        assert taken["template"] == {"id": "fixture-board", "version": 1}
        assert taken["instance_version"] == 2
        assert taken["seq"] == 418
        assert taken["fields"] == {"credits": 12.5}

        listed = await (await client.get(_q("/snapshots"))).json()
        assert listed["snapshots"] == [taken["id"]]
        one = await (await client.get(_q("/snapshots", id=taken["id"]))).json()
        assert one["snapshot"] == taken


async def test_a_snapshot_before_adopting_is_refused():
    async with _client() as client:
        resp = await client.post(_q("/snapshot"), json={"fields": {"credits": 1}, "seq": 0})
        assert resp.status == 409
        assert "adopt a template first" in (await resp.json())["error"]


@pytest.mark.parametrize(
    "body, code",
    [
        ({"seq": 1}, "missing_fields"),
        ({"fields": {}, "seq": 1}, "missing_fields"),
        ({"fields": {"a": 1}}, "bad_seq"),
        ({"fields": {"a": 1}, "seq": -1}, "bad_seq"),
    ],
)
async def test_a_snapshot_refuses_a_body_missing_a_part(body, code):
    async with _client() as client:
        await client.post(_q("/adopt"), json={"template_id": "fixture-board"})
        resp = await client.post(_q("/snapshot"), json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == code


async def test_a_snapshot_id_that_is_not_one_is_refused():
    async with _client() as client:
        resp = await client.get(_q("/snapshots", id="..%2F..%2Fetc%2Fpasswd"))
        assert resp.status == 409
        assert "is not a snapshot id" in (await resp.json())["error"]


# --------------------------------------------------------------------------
# the boundaries, asserted structurally
# --------------------------------------------------------------------------


async def test_every_route_denies_an_app_caller_and_every_write_is_owner_gated():
    """Read off the source, because a passing request proves only the open path.

    The stubs above open both gates so the behaviour can be tested at all, which means
    no request in this file can show that a real app token or a non-owner is refused.
    What CAN be shown is that every handler calls the guards -- and that the write
    handlers call the owner gate, which is the boundary a new route is most likely to
    be added without.
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
    writes = {r.handler.__name__ for r in app.router.routes() if r.method == "POST"}
    assert writes == {
        "api_member_dashboard_adopt",
        "api_member_dashboard_edit",
        "api_member_dashboard_rollback",
        "api_member_dashboard_import",
        "api_member_dashboard_snapshot",
    }
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
        if name in writes or name in _VALUE_SERVING_READS:
            assert "_owner_only" in called, f"{name} is not owner-gated"


#: Reads that serve this crewmate's FOLD VALUES, which are work-ledger and crew-log
#: data: task titles, summaries, PR links. ``work_ledger_board`` answers a non-owner
#: ``owner_only`` for exactly those, so these two cannot answer 200 to the same caller
#: just because their copy arrives as rendered html or as a stored snapshot. The other
#: reads carry version rows, the registry listing and a shareable template document,
#: none of which is this crewmate's data.
#:
#: A SET rather than a check inside the loop, so adding a route that serves values is a
#: one-line change HERE and a visible one in review.
_VALUE_SERVING_READS = frozenset(
    {
        "api_member_dashboard",
        "api_member_dashboard_snapshots",
    }
)


async def test_a_non_owner_cannot_read_a_crewmates_values_live_or_frozen():
    """The gate, exercised rather than read off the source.

    The route-walk above proves the CALL is there; this proves it refuses, and that the
    snapshot copy refuses too. Gating the live read alone would not narrow the leak: a
    snapshot's wire carries ``fields`` verbatim, so the same values are one hop away.
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
            for path in ("", "/snapshots"):
                resp = await client.get(_q(path))
                assert resp.status == 403, (
                    f"GET {path or '/'} answered {resp.status} for a non-owner, so a "
                    "crewmate's fold values reach a caller the sibling board refuses"
                )
    finally:
        mod._owner_only = original
    assert _deny.operations == ["members.dashboard", "members.dashboard.snapshots"], (
        "both reads must name their own operation, so the SEL denial says which "
        "surface was refused"
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
