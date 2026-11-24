"""The dynamic dashboard's agentic surface on the crew-webview routes: the field list, the write
route and its refusals, the crew-log record of each write, and nested field shapes.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from agent_panel_routes_helpers import (  # noqa: F401 -- fixtures are used by name
    CREW,
    SLUG,
    _client,
    _crew_log_on,
    _mounted,
    vetted_fixture,
)
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent_panel
from kiro_crew import members as members_mod
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.dashboard.handlers import agent_panel as routes
from kiro_crew.dashboard.handlers import agent_panel_dashboard

# Bound at collection, before conftest's autouse ``gateway_posts`` swaps
# ``mcp_core._post`` for its recorder in each test.
from kiro_crew.mcp_core import _get as _real_mcp_get
from kiro_crew.mcp_core import _post as _real_mcp_post

pytestmark = pytest.mark.asyncio


# the dynamic dashboard's agentic surface


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

    Uses the registry seam so cases do not depend on shipped templates. Both spellings
    are patched: once imported, ``from ... import instance`` reads the package attribute.
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

    Same registry seam as ``_adopt_dashboard``. ``ships_default=False`` answers ``None``,
    as the real ``default_instance`` does when its id is absent.
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


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        # A write into a fold-sourced cell would be replaced on the next fold, so
        # reporting it as stored would promise a value that does not survive.
        pytest.param(
            "entries",
            3,
            "field_not_agentic",
            id="test_a_host_filled_field_is_refused_rather_than_overwritten",
        ),
        pytest.param(
            "open_items",
            "three",
            "wrong_type",
            id="test_a_value_of_the_wrong_declared_type_is_refused",
        ),
    ],
)
async def test_a_write_the_field_cannot_store_is_refused(vetted, monkeypatch, field, value, code):
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": field, "value": value},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == code


@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("error", "package_model_invalid"),
        ("empty", "no_instance"),
    ],
)
async def test_a_write_is_refused_for_every_package_state_that_has_no_model(
    vetted, monkeypatch, state, code
):
    """A refusal survives for each state that actually describes one, with its own code.

    The Model comes from ``dashboard_package.read_package_model``. Three codes, because
    the remedy differs: create a package, or have a human repair it.
    """
    from kiro_crew import dashboard_package

    monkeypatch.setattr(
        dashboard_package,
        "read_package_model",
        lambda member: dashboard_package.PackageRead(None, state, "stubbed"),
    )
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "for_you", "value": []},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == code


async def test_a_live_package_checks_the_write_against_its_own_model(vetted, monkeypatch):
    """The page draws the package, so the write is checked against it.

    A package-bound write is not refused for being package-bound: it is checked
    against the package's own field table, so a value of the wrong shape is refused
    by that table and not by the provenance of the read.
    """
    from kiro_crew import dashboard_agentic, dashboard_package
    from kiro_crew.dashboard.handlers.member_dashboard_fields import package_manifest

    package = {
        "bound_to": "crewmate:atlas",
        "model": {"types": {"for_you": {"type": "text", "source": {"agentic": True}}}},
    }
    live = dashboard_package.PackageRead(
        dashboard_agentic.Instance(
            manifest=package_manifest(package, slug="dash-atlas", version=2),
            instance_version=2,
        ),
        dashboard_package.STATE_LIVE,
        "package 'dash-atlas' version 2",
        from_package=True,
    )
    monkeypatch.setattr(dashboard_package, "read_package_model", lambda member: live)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "for_you", "value": ["not", "text"]},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
        assert body["code"] == "wrong_type", body


async def test_the_write_route_asks_for_a_package_by_member_and_not_by_slug(vetted, monkeypatch):
    """This route is keyed by the MEMBER and holds no artifact slug.

    Its own ``slug`` is the member's, so handing that over as the package's would name
    an artifact that does not exist. The route hands over the MEMBER and the reader
    resolves the artifact from that member's binding, which is why the reader takes no
    slug at all.
    """
    from kiro_crew import dashboard_package

    seen = []

    def _read(member):
        seen.append(member)
        return dashboard_package.PackageRead(None, "empty", "stubbed")

    monkeypatch.setattr(dashboard_package, "read_package_model", _read)
    async with _client() as c:
        await c.post(
            WRITE_PATH,
            json={"field": "for_you", "value": []},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
    assert seen == [SLUG], f"the route asked for {seen}"


async def test_a_package_the_route_cannot_read_is_a_503_and_not_a_refusal(vetted, monkeypatch):
    """A read that FAILED says nothing about the write, so there is no lesson to
    record and no field to blame -- and a 400 would teach the agent a wrong one."""
    from kiro_crew import dashboard_package

    def _boom(member):
        raise dashboard_package.PackageReadError("package unreadable")

    monkeypatch.setattr(dashboard_package, "read_package_model", _boom)
    async with _client() as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "for_you", "value": []},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 503, await resp.text()
        assert (await resp.json())["code"] == "package_unreadable"


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


async def test_a_layout_write_is_answered_with_what_the_xr_room_makes_of_it(vetted, monkeypatch):
    """D103 5: the reply to a write of `xr_layout` says what the room makes of it, in the
    room's fixed words; a wrong layout is answered, never refused; a cleared one says
    "no 3D dashboard"; a field no part reads adds nothing."""
    _adopt_dashboard(monkeypatch)
    _page_values(monkeypatch, {})
    import kiro_crew.dashboard_templates as templates_pkg

    record = templates_pkg.instance.read(SLUG)
    record.manifest = {
        **record.manifest,
        "fields": {
            **record.manifest["fields"],
            "xr_layout": {"type": "object", "source": {"agentic": True}},
        },
    }
    layout = {
        "v": 1,
        "parts": [
            {"kind": "ring", "name": "Secret words", "field": "open_items", "max": 9},
            {"kind": "gauge", "name": "Load", "field": "open_items"},
        ],
    }
    app = _mounted()
    async with TestClient(TestServer(app)) as c:
        h = {"X-Session-Key": "dashboard:chat-1"}
        resp = await c.post(WRITE_PATH, json={"field": "xr_layout", "value": layout}, headers=h)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["xr_room"] == (
            "In the XR room this layout reads: part 1: ring, a dash: its field holds no value; "
            "part 2: layout: unknown part."
        )
        assert "Secret" not in json.dumps(body)
        cleared = await c.post(WRITE_PATH, json={"field": "xr_layout", "value": {}}, headers=h)
        assert cleared.status == 200, await cleared.text()
        assert (await cleared.json())["xr_room"] == "In the XR room: no 3D dashboard."
        other = await c.post(WRITE_PATH, json={"field": "open_items", "value": 7}, headers=h)
        assert "xr_room" not in await other.json()
        # M75: the verdict reads what is STORED. A lamp key shaped like a credential is
        # stored as a placeholder, which the room reads as a bad key.
        secret_key = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0"
        lamps = {
            "v": 1,
            "parts": [{"kind": "lamps", "name": "Fleet", "field": "open_items", "key": secret_key}],
        }
        scrubbed = await c.post(WRITE_PATH, json={"field": "xr_layout", "value": lamps}, headers=h)
        assert scrubbed.status == 200, await scrubbed.text()
        body = await scrubbed.json()
        # What the store holds is the write's own scrub (`dashboard_agentic._redacted`), which the owner's
        # redaction setting may turn off (its default is off on some lines): the room reads THAT value.
        from kiro_crew.dashboard_agentic import _redacted

        stored = _redacted(secret_key)
        part = (
            "layout: bad key" if stored != secret_key else "lamps, a dash: its field holds no value"
        )
        assert body["xr_room"] == f"In the XR room this layout reads: part 1: {part}."
        assert secret_key not in json.dumps(body)


def _page_values(monkeypatch, values, calls=None):
    """Stand in for the page's own read of its values (`_current_values`): *values* by
    field name, or ``None`` for a read that failed. *calls* counts the reads."""

    def read(_slug, _crew, _instance, _slot=""):
        if calls is not None:
            calls.append(1)
        return (None if values is None else dict(values)), {}

    monkeypatch.setattr(agent_panel_dashboard, "_current_values", read)


def _declare(monkeypatch, **fields):
    """An adopted page whose model also declares *fields* (name -> type)."""
    _adopt_dashboard(monkeypatch)
    import kiro_crew.dashboard_templates as templates_pkg

    record = templates_pkg.instance.read(SLUG)
    record.manifest = {
        **record.manifest,
        "fields": {
            **record.manifest["fields"],
            **{n: {"type": t, "source": {"agentic": True}} for n, t in fields.items()},
        },
    }


XR_LAYOUT = {
    "v": 1,
    "parts": [
        {"kind": "ring", "name": "Secret words", "field": "open_items", "max": "budget"},
        {"kind": "number", "name": "Open", "field": "open_items", "unit": "%"},
        {"kind": "number", "name": "Note", "field": "note"},
        {"kind": "gauge", "name": "Load", "field": "open_items"},
    ],
}
XR_FIELDS = {"xr_layout": "object", "budget": "number", "note": "string"}


async def _write(c, field, value):
    resp = await c.post(
        WRITE_PATH,
        json={"field": field, "value": value},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def test_the_layouts_line_says_what_each_part_shows(vetted, monkeypatch):
    """D169: each good part's place carries what it shows now, from the page's values; a
    wrong part keeps its reason; the layout is laid over the values it was read with."""
    _declare(monkeypatch, **XR_FIELDS)
    _page_values(monkeypatch, {"open_items": 12, "budget": 20, "note": "the secret words"})
    async with TestClient(TestServer(_mounted())) as c:
        body = await _write(c, "xr_layout", XR_LAYOUT)
        assert body["xr_room"] == (
            "In the XR room this layout reads: part 1: ring 12/20; part 2: number 12 %; "
            "part 3: number, a dash: not a number; part 4: layout: unknown part."
        )
        # Never the Bot's own text: not a part's name, not the text a field holds.
        assert "Secret" not in json.dumps(body) and "secret" not in json.dumps(body)


async def test_a_layout_written_before_its_numbers_says_a_dash_for_each(vetted, monkeypatch):
    _declare(monkeypatch, **XR_FIELDS)
    _page_values(monkeypatch, {})
    async with TestClient(TestServer(_mounted())) as c:
        body = await _write(c, "xr_layout", XR_LAYOUT)
        none = "a dash: its field holds no value"
        assert body["xr_room"] == (
            f"In the XR room this layout reads: part 1: ring, {none}; part 2: number, {none}; "
            f"part 3: number, {none}; part 4: layout: unknown part."
        )


async def test_a_number_written_later_is_answered_for_the_parts_that_read_it(vetted, monkeypatch):
    """D169 3: the reply to a write of a number carries the line for the parts that read
    that field, as their own or as their limit, with THIS write's stored value; a field
    two parts read names both; a field no part reads says nothing."""
    _declare(monkeypatch, entries_seen="number", **XR_FIELDS)
    _page_values(monkeypatch, {"xr_layout": XR_LAYOUT, "open_items": 3, "budget": 20})
    async with TestClient(TestServer(_mounted())) as c:
        body = await _write(c, "open_items", 12)
        assert body["xr_room"] == "In the XR room: part 1: ring 12/20; part 2: number 12 %."
        limit = await _write(c, "budget", 2)
        assert limit["xr_room"] == "In the XR room: part 1: ring 3/2, over its limit."
        assert "xr_room" not in await _write(c, "entries_seen", 5)


async def test_a_wrong_value_is_answered_never_refused_and_a_text_is_never_said_back(
    vetted, monkeypatch
):
    """D99 on the values' side: a value its part cannot show is stored and answered with
    a dash and why. The text itself never comes back."""
    _declare(monkeypatch, **XR_FIELDS)
    _page_values(monkeypatch, {"xr_layout": XR_LAYOUT, "open_items": 3, "budget": 20})
    async with TestClient(TestServer(_mounted())) as c:
        body = await _write(c, "note", "the secret words 12345")
        assert body["ok"] is True
        assert body["xr_room"] == "In the XR room: part 3: number, a dash: not a number."
        assert "secret" not in json.dumps(body) and "12345" not in json.dumps(body)
        zero = await _write(c, "budget", 0)
        assert zero["xr_room"] == (
            "In the XR room: part 1: ring, a dash: its limit is no number above 0."
        )


async def test_a_page_with_no_layout_field_reads_nothing_and_says_nothing(vetted, monkeypatch):
    """A Bot with no 3D dashboard pays no read: its page does not declare the field."""
    _adopt_dashboard(monkeypatch)
    calls: list[int] = []
    _page_values(monkeypatch, {"xr_layout": XR_LAYOUT, "open_items": 3}, calls)
    async with TestClient(TestServer(_mounted())) as c:
        assert "xr_room" not in await _write(c, "open_items", 7)
    assert calls == []


async def test_a_page_whose_layout_is_not_written_or_is_no_layout_says_nothing_of_a_number(
    vetted, monkeypatch
):
    _declare(monkeypatch, **XR_FIELDS)
    async with TestClient(TestServer(_mounted())) as c:
        for layout in ({}, None, {"v": 1, "parts": []}, "ring"):
            _page_values(monkeypatch, {"xr_layout": layout} if layout is not None else {})
            assert "xr_room" not in await _write(c, "open_items", 7)


async def test_values_that_cannot_be_read_cost_the_values_only(vetted, monkeypatch):
    """The layout's write still names each part's kind; a number's write says nothing."""
    _declare(monkeypatch, **XR_FIELDS)
    _page_values(monkeypatch, None)
    async with TestClient(TestServer(_mounted())) as c:
        body = await _write(c, "xr_layout", XR_LAYOUT)
        assert body["xr_room"] == (
            "In the XR room this layout reads: part 1: ring; part 2: number; part 3: number; "
            "part 4: layout: unknown part."
        )
        assert "xr_room" not in await _write(c, "open_items", 7)


async def test_two_writes_at_once_are_each_answered_with_their_own_value(vetted, monkeypatch):
    """Each reply lays its own stored value over the page's read, so neither waits for
    the other's to be folded: each names the number it wrote."""
    _declare(monkeypatch, **XR_FIELDS)
    _page_values(monkeypatch, {"xr_layout": XR_LAYOUT, "open_items": 3, "budget": 20})
    async with TestClient(TestServer(_mounted())) as c:
        a, b = await asyncio.gather(_write(c, "open_items", 5), _write(c, "budget", 50))
        assert a["xr_room"] == "In the XR room: part 1: ring 5/20; part 2: number 5 %."
        assert b["xr_room"] == "In the XR room: part 1: ring 3/50."


async def test_the_line_is_made_off_the_event_loop_whole(vetted, monkeypatch):
    """M175: the read of the values and the reading of the layout and of each value run
    in the worker hop, none of it on the gateway's event loop: a value that is slow to
    read holds no other session. Each step says which thread it ran on."""
    import threading

    from kiro_crew import dashboard_xr_layout

    _declare(monkeypatch, **XR_FIELDS)
    loop_thread = threading.current_thread()
    ran: list[tuple[str, bool]] = []

    def read(_slug, _crew, _instance, _slot=""):
        ran.append(("values", threading.current_thread() is loop_thread))
        return {"xr_layout": XR_LAYOUT, "open_items": 3, "budget": 20}, {}

    monkeypatch.setattr(agent_panel_dashboard, "_current_values", read)
    for name in ("verdict", "field_verdict", "part_says", "read_layout", "_plain_number"):
        real = getattr(dashboard_xr_layout, name)

        def spy(*args, _name=name, _real=real, **kwargs):
            ran.append((_name, threading.current_thread() is loop_thread))
            return _real(*args, **kwargs)

        monkeypatch.setattr(dashboard_xr_layout, name, spy)
    async with TestClient(TestServer(_mounted())) as c:
        layout = await _write(c, "xr_layout", XR_LAYOUT)
        assert layout["xr_room"].startswith("In the XR room this layout reads: part 1: ring 3/20")
        number = await _write(c, "note", " 12 %")
        assert number["xr_room"] == "In the XR room: part 3: number 12."
    steps = {name for name, _on_loop in ran}
    assert steps == {
        "values",
        "verdict",
        "field_verdict",
        "part_says",
        "read_layout",
        "_plain_number",
    }
    assert [name for name, on_loop in ran if on_loop] == []


async def test_a_verdict_that_raises_never_fails_a_stored_write(vetted, monkeypatch):
    """R1: the value is stored before the verdict is made, so a raise in it costs the
    line only: the write is answered 200 with no `xr_room`."""
    _adopt_dashboard(monkeypatch)
    import kiro_crew.dashboard_templates as templates_pkg
    from kiro_crew import dashboard_xr_layout

    record = templates_pkg.instance.read(SLUG)
    record.manifest = {
        **record.manifest,
        "fields": {
            **record.manifest["fields"],
            "xr_layout": {"type": "object", "source": {"agentic": True}},
        },
    }

    def broken(_raw, _fields=None):
        raise RuntimeError("the verdict broke")

    monkeypatch.setattr(dashboard_xr_layout, "verdict", broken)
    app = _mounted()
    async with TestClient(TestServer(app)) as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "xr_layout", "value": {"v": 1, "parts": []}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["ok"] is True
        assert "xr_room" not in body


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


@pytest.mark.parametrize(
    ("field", "status", "code"),
    [
        # The log is an agentic value's ONLY record -- no host Python computes
        # one, so there is no file beside it. Reporting success would promise a
        # cell that never fills, which is why this refuses rather than shrugging
        # the way a panel publish does.
        pytest.param(
            "open_items",
            503,
            "crew_log_off",
            id="test_a_write_is_refused_when_the_gateway_records_no_crew_log",
        ),
        # Recording the mistake is best-effort, unlike the value append. By the
        # time it runs the caller is already being refused, so a log that is off
        # costs the mistake book this row and nothing else. Failing here would
        # turn "we could not remember your mistake" into "your write was not
        # refused".
        pytest.param(
            "open",
            400,
            "unknown_field",
            id="test_a_refusal_with_the_log_off_still_refuses_the_write",
        ),
    ],
)
async def test_a_write_with_the_crew_log_off(vetted, monkeypatch, field, status, code):
    _adopt_dashboard(monkeypatch)
    async with _client() as c:
        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        crew_log_emit.reset_caches()
        resp = await c.post(
            WRITE_PATH,
            json={"field": field, "value": 3},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == status, await resp.text()
        assert (await resp.json())["code"] == code
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


async def test_an_unadopted_conductor_writes_needs_you_to_its_own_board(vetted):
    """An unadopted ``kirocrew-conductor`` writes ``needs_you`` to its shipped page.

    The ``goal-board`` field ``asks`` is refused. Uses the real registry, so the route
    resolves the same page the read route renders.
    """
    async with _client("kirocrew-conductor") as c:
        resp = await c.post(
            WRITE_PATH,
            json={"field": "needs_you", "value": ["approve the plan"]},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["written"] == {"field": "needs_you", "type": "array"}

        refused = await c.post(
            WRITE_PATH,
            json={"field": "asks", "value": {}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        assert refused.status == 400, await refused.text()
        assert (await refused.json())["code"] == "unknown_field"


async def test_clear_through_the_panel_server_empties_an_array_field(vetted, monkeypatch):
    """``dashboard_write`` ``clear: true`` empties a field, end to end past the client.

    An MCP client can drop ``value: []``. Two writes, so the read proves the clear
    replaced a value rather than finding a field nobody wrote.
    """
    from kiro_crew import mcp_core, mcp_panel

    # ``mcp_panel`` binds ``_post`` by name on its first import. When that import
    # happens inside a test, it binds conftest's recorder and no write leaves the
    # process, so the real client is bound here: the test passes run alone and
    # beside files that import the server at collection.
    monkeypatch.setattr(mcp_panel, "_post", _real_mcp_post)

    async with _client("kirocrew-conductor") as c:
        monkeypatch.setattr(mcp_core, "_API", f"http://127.0.0.1:{c.server.port}")
        monkeypatch.setattr(mcp_core, "_API_UNIX_SOCKET", "")
        monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "unused-by-this-app")
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-1")

        filled = await asyncio.wait_for(
            asyncio.to_thread(
                mcp_panel._call_tool,
                "dashboard_write",
                {"field": "needs_you", "value": ["approve the plan"]},
            ),
            timeout=30,
        )
        assert filled.startswith("Wrote `needs_you` (array)"), filled

        cleared = await asyncio.wait_for(
            asyncio.to_thread(
                mcp_panel._call_tool, "dashboard_write", {"field": "needs_you", "clear": True}
            ),
            timeout=30,
        )
        assert cleared.startswith("Wrote `needs_you` (array)"), cleared

        crew_log_projection.forget_slot_folds()
        listed = await c.get(FIELDS_PATH, headers={"X-Session-Key": "dashboard:chat-1"})
        assert listed.status == 200, await listed.text()
        rows = {row["field"]: row for row in (await listed.json())["fields"]}

    assert rows["needs_you"]["written"] is True, rows["needs_you"]
    assert rows["needs_you"]["value"] == [], rows["needs_you"]


async def test_clear_through_the_panel_server_empties_an_object_field(vetted, monkeypatch):
    """``dashboard_write`` ``clear: true`` empties an object field end to end.

    Same path as the array case: drives the real entry point through a loopback POST.
    ``goal-board.asks`` is an object field whose upkeep says to drop entries once
    answered, so clearing it to ``{}`` is the documented final step.
    """
    from kiro_crew import mcp_core, mcp_panel

    monkeypatch.setattr(mcp_panel, "_post", _real_mcp_post)
    monkeypatch.setattr(mcp_panel, "_get", _real_mcp_get)

    async with _client("goal-board") as c:
        monkeypatch.setattr(mcp_core, "_API", f"http://127.0.0.1:{c.server.port}")
        monkeypatch.setattr(mcp_core, "_API_UNIX_SOCKET", "")
        monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "unused-by-this-app")
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-1")

        seed = {"it_1": {"options": ["Yes, merge it"]}}
        filled = await asyncio.wait_for(
            asyncio.to_thread(
                mcp_panel._call_tool, "dashboard_write", {"field": "asks", "value": seed}
            ),
            timeout=30,
        )
        assert filled.startswith("Wrote `asks` (object)"), filled

        cleared = await asyncio.wait_for(
            asyncio.to_thread(
                mcp_panel._call_tool, "dashboard_write", {"field": "asks", "clear": True}
            ),
            timeout=30,
        )
        assert cleared.startswith("Wrote `asks` (object)"), cleared

        crew_log_projection.forget_slot_folds()
        listed = await c.get(FIELDS_PATH, headers={"X-Session-Key": "dashboard:chat-1"})
        assert listed.status == 200, await listed.text()
        rows = {row["field"]: row for row in (await listed.json())["fields"]}

    assert rows["asks"]["written"] is True, rows["asks"]
    assert rows["asks"]["value"] == {}, rows["asks"]
    # A clear must not leave a wrong_type entry in the mistake book.
    data = await listed.json()
    mistakes = [m for m in (data.get("mistakes") or []) if m.get("field") == "asks"]
    assert not any(m.get("code") == "wrong_type" for m in mistakes), mistakes


async def test_an_unadopted_crewmate_is_refused_when_the_registry_ships_no_default(
    vetted, monkeypatch
):
    """A ``None`` from ``default_instance`` answers ``no_instance``, not a server fault.

    A registry with the loader and no template has no manifest to validate against.
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

    The browser resolver refuses it (``dashboard_slug_ambiguous``) and so must MCP.
    Checked in ``_resolve_dashboard_caller``, so every route inherits it.
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


# nested shapes on the agentic surface


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
    """The shaped manifest as the page in force, and *read* as the page's values.

    Both seams carry it: ``read_instance`` for the field list, and the package read
    the write route checks a value against, as the template page it falls through to.
    """
    from kiro_crew import dashboard_agentic, dashboard_package
    from kiro_crew.dashboard.handlers import member_dashboard
    from kiro_crew.dashboard_templates.manifest import parse_manifest

    inst = dashboard_agentic.Instance(
        manifest=parse_manifest(dict(SHAPED_MANIFEST)), instance_version=5
    )
    monkeypatch.setattr(routes, "read_instance", lambda _slug, _crew: inst)
    page = dashboard_package.PackageRead(inst, dashboard_package.STATE_LIVE, "stubbed")
    monkeypatch.setattr(dashboard_package, "read_package_model", lambda *_a: page)
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


async def test_a_publish_the_template_cannot_read_is_refused_with_its_keys(vetted):
    app = _mounted()
    async with TestClient(TestServer(app)) as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"template": "kirocrew-conductor", "data": {"cycle": 47}},
            headers={"X-Session-Key": "dashboard:chat-1"},
        )
        body = await resp.json()
    assert resp.status == 400
    assert body["code"] == "no_known_keys"
    from kiro_crew.conductor_board_contract import ConductorBoardPanel

    for field in ConductorBoardPanel.__annotations__:
        assert field in body["error"], f"the refusal does not name {field!r}"
