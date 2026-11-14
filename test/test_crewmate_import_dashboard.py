"""A ``crewmate.import`` card applies the bundle's built-in dashboard.

When the bundle names ``builtin:<id>`` and the receiver ships that template, the
card's plan gets one more apply step that adopts it on the new Bot, and the id
leaves "Not brought over". An id the receiver lacks stays out, as before. The card's Undo
takes that page back only while it is still the version the step adopted.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
from pathlib import Path
from typing import Any

import change_cards_helpers
import pytest
import test_crewmate_import_route as route_world
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_crewmate_import_route import _home  # noqa: F401 -- its autouse fixture

from conftest import make_dir_link
from kiro_crew import change_card_catalog as catalog
from kiro_crew import pinned_fs
from kiro_crew.crewmate_bundle import crewmate_import_params
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard import change_cards_readers as readers
from kiro_crew.dashboard.change_cards import CardStore

world = change_cards_helpers.world

needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="removal walks the directory chain pinned"
)

BUNDLE = {
    "crewmate": {
        "format": "kirocrew.kirobot",
        "formatVersion": 1,
        "name": "Standup Buddy",
        "description": "Turns open work into a standup note.",
        "skills": [],
        "dashboardTemplate": "builtin:standup",
    },
    "agent": {"mcpServers": {}, "skills": []},
}
STEP_PATH = "/api/members/{{step0.member_id}}/dashboard/template"
SLUG = "scout"
MEMBER = "Standup Buddy"


def _bundle(template: str) -> dict[str, Any]:
    bundle = copy.deepcopy(BUNDLE)
    bundle["crewmate"]["dashboardTemplate"] = template
    return bundle


def _preview(template: str) -> tuple[dict[str, Any], dict[str, Any]]:
    params = crewmate_import_params(_bundle(template))
    return params, catalog.build_preview("crewmate.import", params, {"exists": False}, {})


def test_a_shipped_builtin_dashboard_gets_an_apply_step_and_leaves_not_brought_over() -> None:
    params, preview = _preview("builtin:standup")
    assert "not_imported" not in params
    create, step = preview["apply"]
    assert create["path"] == "/api/agents"
    assert (step["method"], step["path"]) == ("POST", STEP_PATH)
    assert step["body"]["template_id"] == "standup"
    rows = {r["field"]: r["after"] for r in preview["changes"]}
    assert rows["dashboard_template"] == "builtin:standup"
    assert "not_imported" not in rows


def test_a_builtin_id_the_receiver_does_not_ship_stays_out() -> None:
    params, preview = _preview("builtin:no-such-page")
    assert params["not_imported"] == ["dashboardTemplate"]
    assert [s["path"] for s in preview["apply"]] == ["/api/agents"]


def test_the_step_is_in_the_import_undo_roles() -> None:
    params, _ = _preview("builtin:standup")
    assert catalog.import_step_roles(params) == ["create", "dashboard"]


@pytest.fixture
def home(tmp_path: Path, _floor_monkeypatch: Any) -> Path:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    return tmp_path


async def _client(monkeypatch: Any, *, armed: bool, state: Any = None) -> TestClient:
    from kiro_crew.dashboard.handlers import member_import_dashboard as route

    async def owner(_req: Any, _op: str, member: Any, *, write: bool) -> Any:
        return SLUG, member, None

    monkeypatch.setattr(route, "_owner_member", owner)

    @web.middleware
    async def arm(request: web.Request, handler: Any) -> Any:
        if armed:
            request[route.REQ_CARD_IMPORT_DASHBOARD] = True
        return await handler(request)

    app = web.Application(middlewares=[arm])
    if state is not None:
        app["state"] = state
    path = "/api/members/{slug}/dashboard/template"
    app.router.add_post(path, route.api_member_import_dashboard)
    app.router.add_delete(path, route.api_member_import_dashboard_undo)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_the_card_step_sets_the_dashboard_on_the_new_bot(home: Path, monkeypatch) -> None:
    from kiro_crew.dashboard_templates import instance

    client = await _client(monkeypatch, armed=True)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/template",
            json={"member": MEMBER, "template_id": "standup"},
        )
        assert resp.status == 200, await resp.text()
        # The version it adopted is what the card's Undo later removes.
        assert (await resp.json())["instance_version"] == 1
    finally:
        await client.close()
    assert instance.read(SLUG).template_id == "standup"


class _Ws:
    """The one thing the gateway state gives a route: the websocket broadcast."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def broadcast_ws(self, kind: str, payload: dict[str, Any]) -> None:
        self.sent.append((kind, payload))


@pytest.mark.asyncio
async def test_the_adopt_tells_open_dashboard_tabs_and_a_refusal_does_not(
    home: Path, monkeypatch
) -> None:
    ws = _Ws()
    client = await _client(monkeypatch, armed=True, state=ws)
    try:
        path = f"/api/members/{SLUG}/dashboard/template"
        bad = await client.post(path, json={"member": MEMBER, "template_id": "no-such-page"})
        assert bad.status == 400 and ws.sent == []
        ok = await client.post(path, json={"member": MEMBER, "template_id": "standup"})
        assert ok.status == 200, await ok.text()
    finally:
        await client.close()
    assert ws.sent == [("dashboard_instance_changed", {"slug": SLUG})]


@pytest.mark.asyncio
@pytest.mark.parametrize("armed", [False, True])
async def test_only_the_card_step_and_only_a_shipped_template_are_accepted(
    home: Path, monkeypatch, armed: bool
) -> None:
    from kiro_crew.dashboard_templates import instance

    client = await _client(monkeypatch, armed=armed)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/template",
            json={"member": MEMBER, "template_id": "no-such-page" if armed else "standup"},
        )
        assert resp.status == (400 if armed else 403)
    finally:
        await client.close()
    assert instance.read(SLUG).template_id == ""


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_the_route_is_hooked_and_writes_no_memory_line(method: str) -> None:
    route = (method, "/api/members/{slug}/dashboard/template")
    assert route in catalog.HOOKED_ROUTES and route not in catalog.MEMORY_ROUTES


# ── Undo ──

ADOPTED = {"member": MEMBER, "template_id": "standup", "instance_version": 1}


@pytest.mark.asyncio
async def test_the_undo_route_is_card_only_and_names_a_shipped_template(
    home: Path, monkeypatch
) -> None:
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")
    path = f"/api/members/{SLUG}/dashboard/template"
    client = await _client(monkeypatch, armed=False)
    try:
        resp = await client.delete(path, json=ADOPTED)
        assert resp.status == 403  # a plain request, no card step
    finally:
        await client.close()
    client = await _client(monkeypatch, armed=True)
    try:
        resp = await client.delete(path, json={**ADOPTED, "template_id": "no-such-page"})
        assert resp.status == 400
    finally:
        await client.close()
    assert instance.read(SLUG).instance_version == 1  # neither call removed anything


@needs_pinned_walk
def test_remove_adopted_refuses_another_template_or_version_and_removes_nothing(
    home: Path,
) -> None:
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")
    instance.edit(SLUG, manifest=dict(instance.read(SLUG).manifest))  # version 2
    folder = instance.instance_dir(SLUG)
    before = sorted(p.name for p in folder.rglob("*"))
    assert instance.remove_adopted(SLUG, "standup", 1) is False  # the page moved on
    assert instance.remove_adopted(SLUG, "another-page", 2) is False  # not this template
    assert sorted(p.name for p in folder.rglob("*")) == before
    assert instance.read(SLUG).instance_version == 2
    assert instance.remove_adopted(SLUG, "standup", 2) is True
    assert instance.read(SLUG).instance_version == 0
    assert not (folder / "versions").exists()


@needs_pinned_walk
def test_remove_adopted_never_follows_a_member_directory_swapped_for_a_link(
    home: Path, tmp_path: Path
) -> None:
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")
    member = instance.instance_dir(SLUG).parent
    aside = member.rename(member.with_name("aside"))  # the real directory moves away
    outside = tmp_path / "outside"
    (outside / "dashboard" / "versions").mkdir(parents=True)
    for rel in ("instance.json", "versions/1.json"):
        (outside / "dashboard" / rel).write_text((aside / "dashboard" / rel).read_text())
    make_dir_link(member, outside)  # the swap
    seen = {p: p.read_text() for p in outside.rglob("*") if p.is_file()}
    with pytest.raises(instance.InstanceError):
        instance.remove_adopted(SLUG, "standup", 1)
    assert {p: p.read_text() for p in outside.rglob("*") if p.is_file()} == seen
    assert not (outside / "dashboard" / ".lock").exists()  # nothing created there either
    assert (aside / "dashboard" / "instance.json").exists()


def _dashboard_zip() -> bytes:
    manifest = {**route_world.MANIFEST, "dashboardTemplate": "builtin:standup"}
    return route_world._zip(route_world._bundle(crewmate_DOT_json=manifest, crons_DOT_json=None))


def _with_dashboard(world, store, writes, monkeypatch, tmp_path, fn):
    """The import route's world, plus the real dashboard routes and the real fingerprint."""
    from kiro_crew.dashboard.handlers import member_import_dashboard as route

    async def owner(_req: Any, _op: str, member: Any, *, write: bool) -> Any:
        return "m1", member, None

    monkeypatch.setattr(route, "_owner_member", owner)
    real = {}

    async def go(c, app):
        inner = cards.read_state

        async def read_state(kind, params, evidence, *, state, app):
            out = await inner(kind, params, evidence, state=state, app=app)
            roles = catalog.import_step_roles(params) if kind == "crewmate.import" else []
            if evidence and "dashboard" in roles[: len(evidence)]:
                adopted = evidence[roles.index("dashboard")].get("instance_version")
                if isinstance(adopted, int):  # a kept page leaves no dashboard to fingerprint
                    out = {**out, "dashboard": readers._import_dashboard_state(evidence[0])}
            return out

        monkeypatch.setattr(cards, "read_state", read_state)
        real["app"] = app
        return await fn(c, app)

    def add_routes(app: web.Application) -> None:
        path = "/api/members/{slug}/dashboard/template"
        app.router.add_post(path, route.api_member_import_dashboard)
        app.router.add_delete(path, route.api_member_import_dashboard_undo)

    original = change_cards_helpers._app

    def app_with_routes(*args: Any, **kw: Any) -> web.Application:
        app = original(*args, **kw)
        add_routes(app)
        return app

    monkeypatch.setattr(change_cards_helpers, "_app", app_with_routes)
    return route_world._go(world, store, writes, monkeypatch, tmp_path, go)


@needs_pinned_walk
def test_undo_removes_an_untouched_dashboard_before_the_bot_delete(world, monkeypatch, tmp_path):
    from kiro_crew.dashboard_templates import instance

    store, writes = CardStore(tmp_path / "c.json"), route_world._Writes()
    removed_at: list[int] = []
    real_remove = instance.remove_adopted

    def remove(*args: Any) -> bool:
        removed_at.append(len(writes.calls))
        return real_remove(*args)

    monkeypatch.setattr(instance, "remove_adopted", remove)

    async def go(c, app):
        card_id = await route_world._raise(c, _dashboard_zip())
        assert await route_world._press(c, store, card_id) == [200, 200, 200, 200]
        assert instance.read("m1").template_id == "standup"
        return await route_world._press(c, store, card_id, op="undo")

    assert _with_dashboard(world, store, writes, monkeypatch, tmp_path, go) == [200, 200, 200]
    assert instance.read("m1").instance_version == 0
    after = [(m, p) for m, p, _ in writes.calls[removed_at[0] :]]
    assert [m for m, _ in after] == ["PUT", "DELETE"]  # the rules clear, then the Bot


def test_undo_completes_and_keeps_the_page_where_the_walk_cannot_be_pinned(
    world, monkeypatch, tmp_path
):
    """Native Windows has no pinned walk: the Undo still deletes the Bot, and the page
    stays, as after a plain Bot delete. It must never refuse as a whole."""
    from kiro_crew.dashboard_templates import instance

    store, writes = CardStore(tmp_path / "c.json"), route_world._Writes()

    async def go(c, app):
        card_id = await route_world._raise(c, _dashboard_zip())
        assert await route_world._press(c, store, card_id) == [200, 200, 200, 200]
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        return await route_world._press(c, store, card_id, op="undo")

    assert _with_dashboard(world, store, writes, monkeypatch, tmp_path, go) == [200, 200, 200]
    assert instance.read("m1").instance_version == 1  # the page stays
    assert "Release Captain" not in writes.crews  # the Bot is gone


def test_undo_with_an_edited_dashboard_is_refused_and_the_page_stays(world, monkeypatch, tmp_path):
    from kiro_crew.dashboard_templates import instance

    store, writes = CardStore(tmp_path / "c.json"), route_world._Writes()

    async def go(c, app):
        card_id = await route_world._raise(c, _dashboard_zip())
        assert await route_world._press(c, store, card_id) == [200, 200, 200, 200]
        instance.edit("m1", manifest=dict(instance.read("m1").manifest))  # the owner's edit
        return await route_world._press(c, store, card_id, op="undo")

    assert _with_dashboard(world, store, writes, monkeypatch, tmp_path, go) == [409]
    assert instance.read("m1").instance_version == 2
    assert "Release Captain" in writes.crews


def _import_rec(*, in_after: bool) -> dict[str, Any]:
    params = crewmate_import_params(_bundle("builtin:standup"))
    after = {"exists": True, "member_id": "m1", "revision": "r1"}
    if in_after:
        after["dashboard"] = {"template_id": "standup", "instance_version": 1}
    return {
        "kind": "crewmate.import",
        "params": params,
        "after": after,
        "evidence": [{"member_id": "m1", "name": MEMBER}, {"instance_version": 1}],
        "plan": {
            "undo": [
                {"method": "DELETE", "path": "/api/members/m1/dashboard/template"},
                {"method": "DELETE", "path": "/api/agents/Standup%20Buddy"},
            ]
        },
    }


def test_a_resumed_undo_compares_the_dashboard_only_when_the_card_recorded_one(monkeypatch):
    live = {"exists": True, "member_id": "m1", "revision": "r1"}

    async def read_state(kind, params, evidence, *, state, app):
        return {**live, "dashboard": {"template_id": "standup", "instance_version": 2}}

    monkeypatch.setattr(cards, "read_state", read_state)

    def stale(rec: dict[str, Any], index: int) -> bool:
        return asyncio.run(cards.resumed_undo_is_stale(rec, index, state=None, app=None))

    # An old stored card has no dashboard key: its Undo still runs.
    assert stale(_import_rec(in_after=False), 0) is False
    # A card that adopted version 1 refuses while the page reads version 2 ...
    assert stale(_import_rec(in_after=True), 0) is True
    # ... and once its own dashboard step has run, the Bot delete goes on.
    assert stale(_import_rec(in_after=True), 1) is False


def test_an_edit_saved_while_the_import_ran_is_never_the_undo_baseline() -> None:
    params = crewmate_import_params(_bundle("builtin:standup"))
    evidence = [{"member_id": "m1", "name": MEMBER}, {"instance_version": 1}]
    page = {"template_id": "standup", "instance_version": 1}
    assert readers._import_after_is_written(params, {"dashboard": page}, evidence) is True
    edited = {**page, "instance_version": 2}
    assert readers._import_after_is_written(params, {"dashboard": edited}, evidence) is False
    assert readers._import_after_is_written(params, {}, evidence) is False


def test_an_old_import_card_without_a_dashboard_step_still_undoes() -> None:
    old = crewmate_import_params(_bundle("builtin:standup"))
    old["not_imported"] = ["dashboardTemplate"]  # how a card stored before this step reads
    assert catalog.import_step_roles(old) == ["create"]
    plan = catalog.import_undo(old, [{"member_id": "m1", "name": MEMBER}], 1)
    assert [s["method"] for s in plan] == ["DELETE"]  # the Bot alone
    assert "/dashboard/" not in plan[0]["path"]


def test_a_plain_bot_delete_and_a_create_undo_leave_the_dashboard(home: Path) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.agent_admin.crew_removal import _remove_member_leftovers
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")
    asyncio.run(_remove_member_leftovers(MEMBER, KiroCrewConfig(), "", None, "test"))
    assert instance.read(SLUG).template_id == "standup"
    undo = catalog.build_undo(
        "crewmate.create",
        {"name": MEMBER, "goal": "g"},
        {},
        [{"member_id": SLUG, "name": MEMBER}],
        1,
    )
    assert undo[0] and not any("/dashboard/" in s["path"] for s in undo[0])


# ── what the Undo takes back, tells, and logs ──


def _adopted_folder(home: Path) -> Path:
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")
    return instance.instance_dir(SLUG)


@needs_pinned_walk
def test_remove_adopted_leaves_only_the_lock_and_a_later_adopt_starts_clean(home: Path) -> None:
    from kiro_crew.dashboard_templates import instance

    folder = _adopted_folder(home)
    (folder / "preview.json").write_text("{}")  # a staged preview goes too
    assert {"history.json", "instance.json", "preview.json"} <= {p.name for p in folder.iterdir()}
    assert instance.remove_adopted(SLUG, "standup", 1) is True
    assert [p.name for p in folder.iterdir() if p.name != ".lock"] == []
    again = instance.adopt(SLUG, "standup")
    assert again.instance_version == 1
    assert len(instance.history(SLUG)) == 1  # no row of the removed import


@needs_pinned_walk
def test_remove_adopted_holds_the_instance_lock(home: Path, monkeypatch) -> None:
    from kiro_crew.dashboard_templates import instance

    folder = _adopted_folder(home)
    monkeypatch.setattr(
        instance,
        "_LOCK_TIMEOUT_SECS",
        0.3,  # flake-ok: bounded wait on a lock this test holds itself
    )
    with instance._locked(folder):  # another writer is mid-write
        with pytest.raises(instance.InstanceError):
            instance.remove_adopted(SLUG, "standup", 1)
    assert instance.read(SLUG).instance_version == 1  # nothing was removed


@pytest.mark.parametrize("slug", ["..", ".", "a/b", "../x", "session:abc"])
def test_remove_adopted_takes_one_path_segment_only(home: Path, slug: str) -> None:
    from kiro_crew.dashboard_templates import instance

    with pytest.raises(instance.InstanceError):
        instance.remove_adopted(slug, "standup", 1)


@needs_pinned_walk
@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="counts the process's open files")
@pytest.mark.parametrize("case", ["nul_in_slug", "missing_folder", "linked_member"])
def test_opening_the_member_dashboard_closes_what_it_holds(home: Path, case: str) -> None:
    from kiro_crew.dashboard_templates import instance

    _adopted_folder(home)
    slug = "a\x00b" if case == "nul_in_slug" else "ghost"
    if case == "linked_member":
        outside = home / "outside"
        outside.mkdir()
        make_dir_link(instance.data_home() / "members" / slug, outside)
    before = len(os.listdir("/proc/self/fd"))
    if case == "missing_folder":
        assert instance._open_member_dashboard(slug) is None
    else:
        with pytest.raises(instance.InstanceError):
            instance._open_member_dashboard(slug)
    assert len(os.listdir("/proc/self/fd")) == before


@pytest.mark.asyncio
async def test_the_undo_route_tells_open_tabs_only_when_it_removed_the_page(
    home: Path, monkeypatch
) -> None:
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")
    path = f"/api/members/{SLUG}/dashboard/template"
    ws = _Ws()
    client = await _client(monkeypatch, armed=True, state=ws)
    try:
        if pinned_fs.supports_pinned_walk():
            stale = await client.delete(path, json={**ADOPTED, "instance_version": 7})
            assert stale.status == 409 and ws.sent == []  # the page moved on: nothing changed
            ok = await client.delete(path, json=ADOPTED)
            assert ok.status == 200 and "kept" not in await ok.json()
            assert ws.sent == [("dashboard_instance_changed", {"slug": SLUG})]
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        ws.sent.clear()
        kept = await client.delete(path, json=ADOPTED)
        assert kept.status == 200 and (await kept.json())["kept"] is True
        assert ws.sent == []  # kept: the page did not change
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_template_that_is_not_shipped_is_refused_by_both_routes(
    home: Path, monkeypatch
) -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard_templates import catalog, instance

    monkeypatch.setattr(
        catalog, "load_one", lambda tid: SimpleNamespace(id=tid, origin="user", html="")
    )
    client = await _client(monkeypatch, armed=True)
    try:
        path = f"/api/members/{SLUG}/dashboard/template"
        assert (await client.post(path, json=ADOPTED)).status == 400
        assert (await client.delete(path, json=ADOPTED)).status == 400
    finally:
        await client.close()
    assert instance.read(SLUG).instance_version == 0


def test_an_undo_without_a_recorded_adopted_version_leaves_the_page_out_of_its_plan() -> None:
    params = crewmate_import_params(_bundle("builtin:standup"))
    named = [{"member_id": "m1", "name": MEMBER}, {"instance_version": 3}]
    plan = catalog.import_undo(params, named, 2)
    assert [s["method"] for s in plan] == ["DELETE", "DELETE"]
    assert plan[0]["body"] == {"member": MEMBER, "template_id": "standup", "instance_version": 3}
    bare = catalog.import_undo(params, [named[0], {}], 2)
    assert [s["method"] for s in bare] == ["DELETE"]  # the Bot alone


def test_a_page_the_catalog_gives_under_another_origin_is_not_applied(monkeypatch) -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard_templates import catalog

    monkeypatch.setattr(catalog, "load_one", lambda tid: SimpleNamespace(id=tid, origin="user"))
    params = crewmate_import_params(_bundle("builtin:standup"))
    assert params["not_imported"] == ["dashboardTemplate"]


@needs_pinned_walk
def test_remove_adopted_pins_the_versions_folder_before_it_deletes_anything(
    home: Path, tmp_path: Path
) -> None:
    from kiro_crew.dashboard_templates import instance

    folder = _adopted_folder(home)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.json").write_text("{}")
    (folder / "versions").rename(folder / "versions-aside")
    make_dir_link(folder / "versions", outside)  # swapped after the import
    with pytest.raises(instance.InstanceError):
        instance.remove_adopted(SLUG, "standup", 1)
    # Nothing was deleted: the record, history and preview stay for a retry.
    assert {"instance.json", "history.json"} <= {p.name for p in folder.iterdir()}
    assert [p.name for p in outside.iterdir()] == ["keep.json"]


@needs_pinned_walk
@pytest.mark.parametrize("level", ["member", "dashboard"])
def test_remove_adopted_stays_inside_the_opened_folder_when_a_level_is_replaced(
    home: Path, monkeypatch, level: str
) -> None:
    """A level is swapped for a link after the folder was opened and before the lock is taken."""
    from kiro_crew.dashboard_templates import instance

    folder = _adopted_folder(home)
    target = folder.parent if level == "member" else folder
    outside = home / "outside"
    decoy = outside / "dashboard" if level == "member" else outside
    (decoy / "versions").mkdir(
        parents=True
    )  # an empty versions folder a removal by name would take
    (decoy / "instance.json").write_text('{"template": {"id": "other"}, "instance_version": 9}')
    before = _tree(outside)
    real_locked = instance._locked

    def swap_then_lock(*args: Any, **kwargs: Any) -> Any:
        target.rename(target.with_name("moved-aside"))
        make_dir_link(target, outside)
        return real_locked(*args, **kwargs)

    monkeypatch.setattr(instance, "_locked", swap_then_lock)
    assert instance.remove_adopted(SLUG, "standup", 1) is True  # the folder that was opened
    moved = target.with_name("moved-aside")
    opened = moved / "dashboard" if level == "member" else moved
    assert [p.name for p in opened.iterdir() if p.name != ".lock"] == []
    assert _tree(outside) == before  # nothing behind the link was read, changed or removed


@pytest.mark.asyncio
@needs_pinned_walk
async def test_a_refused_versions_folder_is_a_409_not_a_500(
    home: Path, monkeypatch, tmp_path
) -> None:
    folder = _adopted_folder(home)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (folder / "versions").rename(folder / "versions-aside")
    make_dir_link(folder / "versions", outside)
    client = await _client(monkeypatch, armed=True, state=_Ws())
    try:
        resp = await client.delete(f"/api/members/{SLUG}/dashboard/template", json=ADOPTED)
        assert resp.status == 409
    finally:
        await client.close()


# ── the routes' log lines: constant text, nothing of the Bot or the page ──

_ROUTE_LOGGER = "kiro_crew.dashboard.handlers.member_import_dashboard"


def _route_logs(caplog: Any) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _ROUTE_LOGGER]


def _assert_plain(lines: list[str], expected: str) -> None:
    assert lines == [expected]
    for secret in (SLUG, "standup", MEMBER, "/"):
        assert secret not in lines[0]


@pytest.mark.asyncio
async def test_a_refused_adopt_logs_one_plain_line(home: Path, monkeypatch, caplog) -> None:
    from kiro_crew.dashboard_templates import instance, instance_adopt

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise instance.InstanceRefused("secret detail standup scout /x/y")

    monkeypatch.setattr(instance_adopt, "adopt_if_empty", refuse)
    caplog.set_level(logging.INFO, logger=_ROUTE_LOGGER)
    client = await _client(monkeypatch, armed=True)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/template",
            json={"member": MEMBER, "template_id": "standup"},
        )
        assert resp.status == 409
    finally:
        await client.close()
    _assert_plain(
        _route_logs(caplog), "import dashboard step refused: the dashboard could not be set"
    )


@pytest.mark.asyncio
@needs_pinned_walk
async def test_a_changed_page_logs_one_plain_line(home: Path, monkeypatch, caplog) -> None:
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")
    caplog.set_level(logging.INFO, logger=_ROUTE_LOGGER)
    client = await _client(monkeypatch, armed=True)
    try:
        resp = await client.delete(
            f"/api/members/{SLUG}/dashboard/template", json={**ADOPTED, "instance_version": 9}
        )
        assert resp.status == 409
    finally:
        await client.close()
    _assert_plain(
        _route_logs(caplog), "import dashboard undo refused: the page changed after the import"
    )


@pytest.mark.asyncio
async def test_a_kept_page_logs_one_plain_line(home: Path, monkeypatch, caplog) -> None:
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    caplog.set_level(logging.INFO, logger=_ROUTE_LOGGER)
    client = await _client(monkeypatch, armed=True)
    try:
        resp = await client.delete(f"/api/members/{SLUG}/dashboard/template", json=ADOPTED)
        assert resp.status == 200
    finally:
        await client.close()
    _assert_plain(
        _route_logs(caplog), "import dashboard undo kept the page: no pinned directory walk here"
    )


@pytest.mark.asyncio
async def test_a_refused_removal_logs_one_plain_line(home: Path, monkeypatch, caplog) -> None:
    from kiro_crew.dashboard_templates import instance

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise instance.InstanceError("secret detail standup scout /x/y")

    instance.adopt(SLUG, "standup")
    monkeypatch.setattr(instance, "remove_adopted", refuse)
    caplog.set_level(logging.INFO, logger=_ROUTE_LOGGER)
    client = await _client(monkeypatch, armed=True)
    try:
        resp = await client.delete(f"/api/members/{SLUG}/dashboard/template", json=ADOPTED)
        assert resp.status == 409
    finally:
        await client.close()
    _assert_plain(
        _route_logs(caplog), "import dashboard undo refused: the dashboard could not be removed"
    )


@needs_pinned_walk
def test_a_removal_that_stops_half_way_can_be_retried(home: Path, monkeypatch) -> None:
    from kiro_crew.dashboard_templates import instance

    folder = _adopted_folder(home)
    real_rmdir = instance.os.rmdir

    def failing_rmdir(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk trouble")

    monkeypatch.setattr(instance.os, "rmdir", failing_rmdir)
    with pytest.raises(instance.InstanceError):
        instance.remove_adopted(SLUG, "standup", 1)
    monkeypatch.setattr(instance.os, "rmdir", real_rmdir)
    # The record still names the adopted page, so the Undo can run again and finish.
    assert (folder / "instance.json").exists()
    assert instance.remove_adopted(SLUG, "standup", 1) is True
    assert [p.name for p in folder.iterdir() if p.name != ".lock"] == []


# ── a page the slug already holds is kept ──


@pytest.mark.asyncio
async def test_the_adopt_step_keeps_a_page_the_slug_already_holds(home: Path, monkeypatch) -> None:
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")
    instance.edit(SLUG, manifest=dict(instance.read(SLUG).manifest))  # version 2
    ws = _Ws()
    client = await _client(monkeypatch, armed=True, state=ws)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/template",
            json={"member": MEMBER, "template_id": "standup"},
        )
        body = await resp.json()
    finally:
        await client.close()
    assert resp.status == 200 and body["kept_existing"] is True
    assert "instance_version" not in body  # the step adopted nothing
    assert ws.sent == []  # and nothing changed for an open tab to re-read
    kept = instance.read(SLUG)
    assert (kept.template_id, kept.instance_version) == ("standup", 2)


def test_an_import_onto_a_slug_with_a_page_keeps_it_and_its_undo_leaves_it(
    world, monkeypatch, tmp_path
):
    from kiro_crew.dashboard_templates import instance

    instance.adopt("m1", "standup")
    instance.edit("m1", manifest=dict(instance.read("m1").manifest))  # the owner's page, version 2
    store, writes = CardStore(tmp_path / "c.json"), route_world._Writes()

    async def go(c, app):
        card_id = await route_world._raise(c, _dashboard_zip())
        assert await route_world._press(c, store, card_id) == [200, 200, 200, 200]
        assert store.get(card_id)["status"] == "applied"
        kept = instance.read("m1")
        assert (kept.template_id, kept.instance_version) == ("standup", 2)
        paths = [s["path"] for s in store.get(card_id)["plan"]["undo"]]
        assert not any("/dashboard/template" in p for p in paths)  # nothing of ours to remove
        return await route_world._press(c, store, card_id, op="undo")

    assert _with_dashboard(world, store, writes, monkeypatch, tmp_path, go) == [200, 200]
    kept = instance.read("m1")
    assert (kept.template_id, kept.instance_version) == ("standup", 2)  # page and versions stay
    assert "Release Captain" not in writes.crews


# ── anything stored for the slug is kept, readable or not ──


#: A record file that is not JSON at all: ``read`` raises for these instead of answering a state.
_NOT_JSON = {"broken_text": "{not json", "empty_file": "", "json_list": "[1, 2]"}
UNREADABLE_SHAPES = ["newer_schema", "no_template", *_NOT_JSON]


def _stored_record(shape: str, slug: str = SLUG) -> bytes:
    """Replace the slug's record with one this build cannot read; its bytes."""
    import json

    from kiro_crew.dashboard_templates import instance

    instance.adopt(slug, "standup")
    path = instance.instance_dir(slug) / "instance.json"
    if shape in _NOT_JSON:
        path.write_text(_NOT_JSON[shape])
        with pytest.raises(instance.InstanceError):
            instance.read(slug)
        return path.read_bytes()
    raw = json.loads(path.read_text())
    if shape == "newer_schema":
        raw.update(schema=999, instance_version=5)
    else:
        raw.update(template=None, instance_version=0)
    path.write_text(json.dumps(raw))
    assert instance.read(slug).state == instance.STATE_ERROR
    return path.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", UNREADABLE_SHAPES)
async def test_the_adopt_step_keeps_a_record_this_build_cannot_read(
    home: Path, monkeypatch, shape: str
) -> None:
    from kiro_crew.dashboard_templates import instance

    before = _stored_record(shape)
    ws = _Ws()
    client = await _client(monkeypatch, armed=True, state=ws)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/template",
            json={"member": MEMBER, "template_id": "standup"},
        )
        body = await resp.json()
    finally:
        await client.close()
    assert resp.status == 200 and body["kept_existing"] is True
    assert "instance_version" not in body
    assert ws.sent == []
    assert (instance.instance_dir(SLUG) / "instance.json").read_bytes() == before


@pytest.mark.parametrize("shape", UNREADABLE_SHAPES)
def test_an_import_onto_an_unreadable_record_keeps_it_and_its_undo_leaves_it(
    world, monkeypatch, tmp_path, shape: str
):
    from kiro_crew.dashboard_templates import instance

    before = _stored_record(shape, "m1")
    store, writes = CardStore(tmp_path / "c.json"), route_world._Writes()

    async def go(c, app):
        card_id = await route_world._raise(c, _dashboard_zip())
        assert await route_world._press(c, store, card_id) == [200, 200, 200, 200]
        paths = [s["path"] for s in store.get(card_id)["plan"]["undo"]]
        assert not any("/dashboard/template" in p for p in paths)
        return await route_world._press(c, store, card_id, op="undo")

    assert _with_dashboard(world, store, writes, monkeypatch, tmp_path, go) == [200, 200]
    assert (instance.instance_dir("m1") / "instance.json").read_bytes() == before


@pytest.mark.asyncio
async def test_the_adopt_step_keeps_a_page_from_an_earlier_import(home: Path, monkeypatch) -> None:
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")  # version 1: the page an earlier import made
    client = await _client(monkeypatch, armed=True)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/template",
            json={"member": MEMBER, "template_id": "standup"},
        )
        body = await resp.json()
    finally:
        await client.close()
    assert resp.status == 200 and body["kept_existing"] is True
    assert instance.read(SLUG).instance_version == 1  # not bumped to 2


@pytest.mark.asyncio
async def test_the_adopt_step_that_keeps_what_the_slug_holds_logs_one_plain_line(
    home: Path, monkeypatch, caplog
) -> None:
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")
    caplog.set_level(logging.INFO, logger=_ROUTE_LOGGER)
    client = await _client(monkeypatch, armed=True)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/template",
            json={"member": MEMBER, "template_id": "standup"},
        )
        assert resp.status == 200
    finally:
        await client.close()
    _assert_plain(_route_logs(caplog), "import dashboard step kept what the slug already holds")


# ── the dashboard folder is opened without following links ──

LINK_LEVELS = ["members", "slug", "dashboard", "versions"]


def _link_level(slug: str, level: str, home: Path) -> Path:
    """Make one level of ``members/<slug>/dashboard/versions`` a link to a folder holding one
    file, and return that folder."""
    from kiro_crew.dashboard_templates import instance

    outside = home / "outside"
    outside.mkdir()
    (outside / "keep.json").write_text("{}")
    chain = {
        "members": instance.data_home() / "members",
        "slug": instance.data_home() / "members" / slug,
        "dashboard": instance.instance_dir(slug),
        "versions": instance.instance_dir(slug) / "versions",
    }
    chain[level].parent.mkdir(parents=True, exist_ok=True)
    make_dir_link(chain[level], outside)
    return outside


def _tree(root: Path) -> list[tuple[str, bytes | None]]:
    return sorted(
        (str(p.relative_to(root)), p.read_bytes() if p.is_file() else None) for p in root.rglob("*")
    )


async def _post_step(monkeypatch: Any, state: Any = None) -> tuple[int, dict[str, Any]]:
    client = await _client(monkeypatch, armed=True, state=state)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/template",
            json={"member": MEMBER, "template_id": "standup"},
        )
        return resp.status, await resp.json()
    finally:
        await client.close()


@pytest.mark.asyncio
@needs_pinned_walk
@pytest.mark.parametrize("level", LINK_LEVELS)
async def test_import_dashboard_refuses_linked_level(home: Path, monkeypatch, level: str) -> None:
    outside = _link_level(SLUG, level, home)
    before = _tree(outside)
    ws = _Ws()
    status, body = await _post_step(monkeypatch, ws)
    assert status == 409 and body["code"] == "dashboard_not_set"
    assert _tree(outside) == before  # nothing was written through the link
    assert ws.sent == []


@pytest.mark.asyncio
@needs_pinned_walk
@pytest.mark.parametrize("level", ["versions", "dashboard"])
async def test_import_dashboard_stays_inside_opened_folder_when_a_level_is_replaced(
    home: Path, monkeypatch, level: str
) -> None:
    """A level is replaced by a link after the folder was opened and before the commit."""
    from kiro_crew.dashboard_templates import instance

    outside = home / "outside"
    outside.mkdir()
    (outside / "keep.json").write_text("{}")
    before = _tree(outside)
    real_commit = instance._commit

    def replace_then_commit(*args: Any, **kwargs: Any) -> Any:
        target = instance.instance_dir(SLUG)
        if level == "dashboard":
            target.rename(target.with_name("dashboard.moved"))
            make_dir_link(target, outside)
        else:
            make_dir_link(target / "versions", outside)
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(instance, "_commit", replace_then_commit)
    status, body = await _post_step(monkeypatch)
    assert _tree(outside) == before  # the write did not follow the replaced level
    # Refused, or finished inside the folder that was opened: never through the new link.
    assert status in (200, 409), body


@needs_pinned_walk
def test_import_dashboard_step_stops_at_linked_level(world, monkeypatch, tmp_path):
    from kiro_crew.dashboard_templates import instance

    store, writes = CardStore(tmp_path / "c.json"), route_world._Writes()
    state: dict[str, Any] = {}

    async def go(c, app):
        outside = _link_level("m1", "dashboard", instance.data_home().parent)
        state["outside"], state["before"] = outside, _tree(outside)
        card_id = await route_world._raise(c, _dashboard_zip())
        assert await route_world._press(c, store, card_id) == [200, 200, 200, 409]
        paths = [s["path"] for s in store.get(card_id)["plan"]["undo"]]
        assert not any("/dashboard/template" in p for p in paths)  # nothing of ours to undo

    _with_dashboard(world, store, writes, monkeypatch, tmp_path, go)
    assert _tree(state["outside"]) == state["before"]


# ── small gaps: the lock's own page-folder step, the DELETE body, the card's fingerprint ──


@needs_pinned_walk
def test_remove_adopted_opens_no_page_folder_by_name(home: Path, monkeypatch) -> None:
    from kiro_crew.dashboard_templates import instance

    _adopted_folder(home)
    opened_by_name: list[Path] = []
    monkeypatch.setattr(instance, "_pinned_session_dir", opened_by_name.append)
    assert instance.remove_adopted(SLUG, "standup", 1) is True
    assert opened_by_name == []  # the folder the caller held is the only one used


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["1", 1.5, True, None])
async def test_the_undo_route_needs_an_integer_instance_version(
    home: Path, monkeypatch, version: Any
) -> None:
    client = await _client(monkeypatch, armed=True, state=_Ws())
    try:
        resp = await client.delete(
            f"/api/members/{SLUG}/dashboard/template", json={**ADOPTED, "instance_version": version}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_version"
    finally:
        await client.close()


def test_the_import_card_fingerprint_follows_the_dashboard_record(home: Path) -> None:
    from kiro_crew.dashboard_templates import instance

    instance.adopt(SLUG, "standup")
    params = crewmate_import_params(_bundle("builtin:standup"))
    evidence = [{"member_id": SLUG, "name": MEMBER}, {"instance_version": 1}]

    def fingerprint() -> Any:
        return asyncio.run(readers._import_state(params, evidence, state=None))["dashboard"]

    assert fingerprint() == {"template_id": "standup", "instance_version": 1}
    instance.edit(SLUG, manifest=dict(instance.read(SLUG).manifest))  # version 2
    assert fingerprint() == {"template_id": "standup", "instance_version": 2}
