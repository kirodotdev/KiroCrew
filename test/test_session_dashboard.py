"""A ROOT session's own dynamic dashboard: the slot-keyed read and the agent's write gate.

The property worth guarding hardest is WHO gets a page. Only a root session does -- one
no other session dispatched or adopted -- and the test for that is
``card_lifecycle.is_root_session``, not a new one. A worker's records belong to the
board of whoever dispatched it, so a dispatched or adopted session is answered "no page"
on the read and refused on the write.

The write gate is a security boundary: the page a caller writes is named by the
caller's OWN slot and never by anything it sends, and every gate the crew path has
(internal secret, operator switch, app denial, restricted mode, slot check) runs first.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.crew_log import session_tree_projection
from kiro_crew.dashboard.handlers import agent_panel as panel_routes
from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates import catalog, instance

pytestmark = pytest.mark.asyncio

ROOT = "chat-100"
DISPATCHED = "chat-101"
ADOPTED = "chat-102"

PAGE = '<div><b data-dashboard-field="phase"></b></div>'


def _manifest(**over: Any) -> dict[str, Any]:
    raw = {
        "id": "fixture-board",
        "version": 1,
        "title": "Fixture board",
        "description": "A template these tests own.",
        "source": "builtin",
        "fields": {"phase": {"type": "string", "source": {"agentic": True}}},
    }
    raw.update(over)
    return raw


class _Node:
    def __init__(self, slot: str, parent_slot: str | None) -> None:
        self.slot = slot
        self.parent_slot = parent_slot
        self.cycle = False


SLOTS = {
    ROOT: SimpleNamespace(key=ROOT, _created_by="", agent="kiro"),
    # Dispatched through the session-control create verb: `_created_by` names the parent.
    DISPATCHED: SimpleNamespace(key=DISPATCHED, _created_by=ROOT, agent="kiro"),
    # Adopted: no `_created_by`, but the crew log's session tree holds a parent edge.
    ADOPTED: SimpleNamespace(key=ADOPTED, _created_by="", agent="kiro"),
}


class _Sessions:
    def has_session(self, _key: str) -> bool:
        return True

    def get_agent_selection(self, _key: str) -> tuple[str, str]:
        # An ordinary session selected a provider TEMPLATE, so it is bound to no crew.
        return "template", "kiro"

    def get_provider(self, _key: str) -> None:
        return None


class _State:
    def __init__(self) -> None:
        self.sessions = _Sessions()
        self.broadcasts: list[tuple[str, object]] = []
        self.owner_broadcasts: list[tuple[str, object]] = []

    def get_slot(self, name: str) -> Any:
        return SLOTS.get(name)

    def broadcast_ws(self, msg_type: str, data: object) -> None:
        self.broadcasts.append((msg_type, data))

    def broadcast_ws_owners(self, msg_type: str, data: object) -> None:
        self.owner_broadcasts.append((msg_type, data))


async def _none(*_a: Any, **_k: Any) -> None:
    return None


@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.delenv("KIROCREW_CREW_LOG", raising=False)
    builtin = tmp_path / "builtin" / "fixture-board"
    builtin.mkdir(parents=True)
    (builtin / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    (builtin / "template.html").write_text(PAGE, encoding="utf-8")
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: builtin.parent)
    # The session tree, seeded: the adopted slot has a parent edge, the others none.
    nodes = {ADOPTED: _Node(ADOPTED, ROOT)}
    _floor_monkeypatch.setattr(
        session_tree_projection,
        "projection",
        lambda: SimpleNamespace(nodes=lambda: nodes, seeded_for_current_store=True),
    )
    _floor_monkeypatch.setattr(routes, "_deny_app_caller", _none)
    _floor_monkeypatch.setattr(routes, "_owner_only", _none)
    # The write gate's own vetting, opened: each is tested where it is defined, and
    # what is under test here is the root-session branch after them.
    _floor_monkeypatch.setattr(panel_routes, "_recognize_session", _none)
    _floor_monkeypatch.setattr(panel_routes, "_is_restricted_session", lambda *_a, **_k: False)
    _floor_monkeypatch.setattr(panel_routes, "_deny_app_caller", _none)
    _floor_monkeypatch.setattr(panel_routes.members_mod, "crew_panel_enabled", lambda: True)
    yield


@asynccontextmanager
async def _client(*, internal: bool = True):
    app = web.Application()
    app["state"] = _State()
    if internal:

        @web.middleware
        async def _internal(request, handler):
            request["internal_auth"] = True
            return await handler(request)

        app.middlewares.append(_internal)
    routes.register_member_dashboard_routes(app)
    panel_routes.register_agent_panel_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


def _read(slot: str) -> str:
    return f"/api/chat/slots/{slot}/dashboard"


def _as(slot: str) -> dict[str, str]:
    return {"X-Session-Key": f"dashboard:{slot}"}


# --------------------------------------------------------------------------
# the store key
# --------------------------------------------------------------------------


def test_a_session_key_names_its_own_directory_and_no_member_slug_can():
    key = instance.session_instance_key(ROOT)
    assert key == instance.session_instance_key(ROOT)
    assert key != instance.session_instance_key(DISPATCHED)
    assert instance.instance_dir(key).parent.name == "session-dashboards"
    # A key that did not come from the derivation is refused rather than joined.
    with pytest.raises(instance.InstanceError):
        instance.instance_dir("session:../../members/x")


# --------------------------------------------------------------------------
# the read the side panel does
# --------------------------------------------------------------------------


async def test_a_root_session_gets_its_page():
    instance.adopt(instance.session_instance_key(ROOT), "fixture-board")
    async with _client() as client:
        resp = await client.get(_read(ROOT))
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["state"] == "live"
        assert body["template"] == {"id": "fixture-board", "version": 1}
        assert "rendered_html" in body


async def test_a_dispatched_session_gets_no_page():
    async with _client() as client:
        resp = await client.get(_read(DISPATCHED))
        assert resp.status == 404
        assert (await resp.json())["code"] == "not_root_session"


async def test_an_adopted_session_gets_no_page():
    async with _client() as client:
        resp = await client.get(_read(ADOPTED))
        assert resp.status == 404
        assert (await resp.json())["code"] == "not_root_session"


async def test_an_unknown_slot_is_a_404():
    async with _client() as client:
        resp = await client.get(_read("chat-nope"))
        assert resp.status == 404
        assert (await resp.json())["code"] == "slot_not_found"


async def test_the_slot_read_is_owner_only(monkeypatch):
    async def _deny(_request, _op):
        return web.json_response({"error": "owner only", "code": "owner_only"}, status=403)

    monkeypatch.setattr(routes, "_owner_only", _deny)
    async with _client() as client:
        resp = await client.get(_read(ROOT))
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"


async def test_the_slot_read_denies_an_app_caller(monkeypatch):
    async def _deny(_request, _op):
        return web.json_response({"error": "apps", "code": "app_denied"}, status=403)

    monkeypatch.setattr(routes, "_deny_app_caller", _deny)
    async with _client() as client:
        resp = await client.get(_read(ROOT))
        assert resp.status == 403


async def test_the_slot_read_is_redacted(tmp_path):
    # Assembled at runtime so the source holds no credential-shaped literal.
    token = "ghp" + "_" + "ZXAMPLEzxampleZXAMPLEzxampleZXAMPLE12"
    manifest = tmp_path / "builtin" / "fixture-board" / "manifest.json"
    manifest.write_text(json.dumps(_manifest(title=f"board {token}")), encoding="utf-8")
    instance.adopt(instance.session_instance_key(ROOT), "fixture-board")
    async with _client() as client:
        text = await (await client.get(_read(ROOT))).text()
    assert token not in text, "a credential in the page's manifest reached the reader"
    assert "board" in text


async def test_the_session_route_is_bound_through_the_deferred_binder():
    import inspect

    from kiro_crew.dashboard import server

    flat = " ".join(inspect.getsource(server._register_mcp_routes).split())
    assert '"/api/chat/slots/{slot}/dashboard", _deferred("member_dashboard",' in flat


# --------------------------------------------------------------------------
# the agent's write gate
# --------------------------------------------------------------------------


async def test_a_root_session_writes_its_own_page():
    async with _client() as client:
        staged = await client.post(
            "/api/agent-panel/dashboard/preview",
            json={"template_id": "fixture-board"},
            headers=_as(ROOT),
        )
        assert staged.status == 200, await staged.text()
        link = (await staged.json())["preview"]["preview_url"]
        assert link == f"/api/chat/slots/{ROOT}/dashboard?preview=1"
        applied = await client.post("/api/agent-panel/dashboard/apply", headers=_as(ROOT))
        assert applied.status == 200, await applied.text()
        # Landed on the caller's own store key, and the side panel's read sees it.
        assert instance.read(instance.session_instance_key(ROOT)).template_id == "fixture-board"
        body = await (await client.get(_read(ROOT))).json()
        assert body["template"]["id"] == "fixture-board"
        # The slot frame reaches owner sockets only: a slot key is hidden from an app
        # socket, and the general broadcast would hand it to any app holding panels.
        state = client.server.app["state"]
        assert ("dashboard_instance_changed", {"slot": ROOT}) in state.owner_broadcasts
        assert not [frame for frame in state.broadcasts if "slot" in dict(frame[1])]


@pytest.mark.parametrize("slot", [DISPATCHED, ADOPTED])
async def test_a_non_root_session_is_refused_the_write(slot):
    async with _client() as client:
        resp = await client.post(
            "/api/agent-panel/dashboard/preview",
            json={"template_id": "fixture-board"},
            headers=_as(slot),
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "not_root_session"
    assert instance.staged_preview(instance.session_instance_key(slot)) is None


async def test_a_subagent_with_no_slot_is_refused_the_write():
    async with _client() as client:
        resp = await client.get(
            "/api/agent-panel/dashboard/templates", headers=_as("subagent-of-chat-100")
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "no_dashboard_slot"


async def test_a_root_session_still_cannot_publish_a_crew_webview():
    """The widened branch is the dynamic dashboard's alone, not the crew panel's."""
    async with _client() as client:
        resp = await client.post(
            "/api/agent-panel/publish", json={"data": {"x": 1}}, headers=_as(ROOT)
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "no_crew"


async def test_the_write_gate_still_wants_the_internal_secret():
    async with _client(internal=False) as client:
        resp = await client.get("/api/agent-panel/dashboard/templates", headers=_as(ROOT))
        assert resp.status == 403
        assert (await resp.json())["code"] == "internal_secret_required"


async def test_an_unseeded_tree_refuses_the_write(monkeypatch):
    """`is_root_session` fails closed before the tree is seeded, and so does the gate."""
    monkeypatch.setattr(
        session_tree_projection,
        "projection",
        lambda: SimpleNamespace(nodes=lambda: {}, seeded_for_current_store=False),
    )
    monkeypatch.setattr("kiro_crew.dashboard.card_lifecycle._ask_for_lineage_seed", lambda: None)
    async with _client() as client:
        resp = await client.get("/api/agent-panel/dashboard/templates", headers=_as(ROOT))
        assert resp.status == 403
        assert (await resp.json())["code"] == "not_root_session"


# --------------------------------------------------------------------------
# whose page it is
# --------------------------------------------------------------------------


def _subject(rendered: str) -> str:
    """The ``subject`` the composed page hands its own script."""
    import re

    match = re.search(r'"subject":\s*"([a-z]+)"', rendered)
    assert match, "the composed page carries no subject"
    return match.group(1)


async def test_a_session_page_is_told_it_is_a_session():
    instance.adopt(instance.session_instance_key(ROOT), "fixture-board")
    async with _client() as client:
        body = await (await client.get(_read(ROOT))).json()
    assert _subject(body["rendered_html"]) == "session"


def test_a_crewmate_page_is_told_it_is_a_crewmate():
    from kiro_crew import dashboard_frame

    assert dashboard_frame.read_payload({})["subject"] == "crewmate"
    assert dashboard_frame.read_payload({}, subject="session")["subject"] == "session"
    # Anything else is a crewmate: the template's default wording.
    assert dashboard_frame.read_payload({}, subject="<b>")["subject"] == "crewmate"


def test_project_report_words_its_heading_for_a_session():
    """The shipped template switches its eyebrow on the subject, both ways."""
    from pathlib import Path

    import kiro_crew.dashboard_templates as templates_pkg

    real = Path(templates_pkg.__file__).parent / "builtin" / "project-report" / "template.html"
    page = real.read_text("utf-8")
    assert 'id="pr-eyebrow"' in page
    assert "'What this session is doing'" in page
    assert "'What this crewmate is doing'" in page
    assert ".subject === 'session'" in page


# --------------------------------------------------------------------------
# a planted link under the session pages
# --------------------------------------------------------------------------


def _planted(tmp_path, *, at_root: bool = False):
    """Point the root session's page directory (or the whole root) at a "profiles" dir."""
    from kiro_crew.config.paths import data_home

    target = tmp_path / "profiles"
    target.mkdir()
    root = data_home() / "session-dashboards"
    if at_root:
        root.parent.mkdir(parents=True, exist_ok=True)
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(parents=True, exist_ok=True)
        digest = instance.session_instance_key(ROOT)[len(instance.SESSION_KEY_PREFIX) :]
        (root / digest).symlink_to(target, target_is_directory=True)
    return target


@pytest.mark.parametrize("at_root", [False, True])
def test_a_linked_session_page_directory_is_refused_and_nothing_lands(tmp_path, at_root):
    """The sandboxed agent can plant a link; the gateway must not write through it."""
    target = _planted(tmp_path, at_root=at_root)
    key = instance.session_instance_key(ROOT)
    with pytest.raises(instance.InstanceError):
        instance.stage_preview(key, template_id="fixture-board")
    with pytest.raises(instance.InstanceError):
        instance.adopt(key, "fixture-board")
    assert list(target.iterdir()) == [], "a file was written through the planted link"


def test_a_clean_session_page_directory_still_writes(tmp_path):
    key = instance.session_instance_key(ROOT)
    instance.stage_preview(key, template_id="fixture-board")
    instance.adopt(key, "fixture-board")
    assert instance.read(key).template_id == "fixture-board"
    assert (instance.instance_dir(key) / "versions" / "1.json").is_file()


def _fail_dir_fsync(monkeypatch, code: int) -> None:
    """Make ``os.fsync`` raise *code* for DIRECTORY descriptors only, file fsyncs intact.

    The page directory sync in ``_commit`` is the one and only directory fsync on this
    path; the version payload and record go through ``atomic_write``'s own file fsync,
    which must keep working or the test would prove nothing about the directory sync.
    """
    import os as _os
    import stat as _stat

    real = _os.fsync

    def _fsync(fd: int) -> None:
        if _stat.S_ISDIR(_os.fstat(fd).st_mode):
            raise OSError(code, "injected")
        real(fd)

    monkeypatch.setattr(instance.os, "fsync", _fsync)


@pytest.mark.skipif(
    not instance.pinned_fs.supports_pinned_walk(), reason="needs descriptor-relative opens"
)
def test_a_mount_that_rejects_a_directory_fsync_still_lands_the_page(tmp_path, monkeypatch):
    """EINVAL/ENOTSUP from the page-directory fsync is tolerated: the apply completes.

    Before the fix the bare ``os.fsync`` on the pinned descriptor raised here -- after
    ``instance.json`` was already written -- so the apply failed with HTTP 503 and the
    history row the version needs was never appended. The rename plus the file fsync
    are the durability a network mount can give, so this errno is a no-op.
    """
    import errno as _errno

    key = instance.session_instance_key(ROOT)
    instance.stage_preview(key, template_id="fixture-board")
    _fail_dir_fsync(monkeypatch, _errno.EINVAL)
    instance.adopt(key, "fixture-board", session_id="chat-100")
    assert instance.read(key).template_id == "fixture-board"
    assert (instance.instance_dir(key) / "versions" / "1.json").is_file()
    # The history savepoint beside the version proves the append ran -- the step the
    # old 503 skipped.
    assert (instance.instance_dir(key) / instance._HISTORY_FILE).is_file()


@pytest.mark.skipif(
    not instance.pinned_fs.supports_pinned_walk(), reason="needs descriptor-relative opens"
)
def test_a_genuine_io_failure_on_the_directory_fsync_still_fails_the_apply(tmp_path, monkeypatch):
    """EIO is NOT tolerated: the device refused the write, so the apply must not claim success."""
    import errno as _errno

    key = instance.session_instance_key(ROOT)
    instance.stage_preview(key, template_id="fixture-board")
    _fail_dir_fsync(monkeypatch, _errno.EIO)
    with pytest.raises(OSError) as caught:
        instance.adopt(key, "fixture-board", session_id="chat-100")
    assert caught.value.errno == _errno.EIO


@pytest.mark.parametrize("at_root", [False, True])
def test_the_lstat_walk_refuses_a_planted_link_on_its_own(tmp_path, at_root):
    """The by-name check, which is the whole guard on a platform that cannot pin."""
    _planted(tmp_path, at_root=at_root)
    key = instance.session_instance_key(ROOT)
    digest = key[len(instance.SESSION_KEY_PREFIX) :]
    from kiro_crew.config.paths import data_home

    with pytest.raises(instance.InstanceError):
        instance._refuse_linked_session_dir(data_home() / "session-dashboards" / digest)


@pytest.mark.skipif(
    not instance.pinned_fs.supports_pinned_walk(), reason="needs descriptor-relative opens"
)
@pytest.mark.parametrize("at_root", [False, True])
def test_the_pinned_open_refuses_a_planted_link_on_its_own(tmp_path, at_root):
    """The O_NOFOLLOW descent, which also closes the window after the lstat walk."""
    target = _planted(tmp_path, at_root=at_root)
    key = instance.session_instance_key(ROOT)
    digest = key[len(instance.SESSION_KEY_PREFIX) :]
    from kiro_crew.config.paths import data_home

    with pytest.raises(instance.InstanceError):
        instance._pinned_session_dir(data_home() / "session-dashboards" / digest)
    assert list(target.iterdir()) == []


@pytest.mark.skipif(
    not instance.pinned_fs.supports_pinned_walk(), reason="needs descriptor-relative opens"
)
def test_a_root_swapped_for_a_link_after_it_is_opened_lands_nothing_in_the_target(
    tmp_path, monkeypatch
):
    """The swap lands between opening the pages root and creating the page directory.

    The by-name checks have already passed by then, so only a create relative to the
    root descriptor keeps the write out of the target.
    """
    from kiro_crew.config.paths import data_home

    target = tmp_path / "profiles"
    target.mkdir()
    root = data_home() / "session-dashboards"
    real = instance.pinned_fs.create_and_open_dir_pinned
    swapped = []

    def open_root_then_swap(path, **kw):
        fd = real(path, **kw)
        if Path(path) == root and not swapped:
            root.rename(tmp_path / "moved-away")
            root.symlink_to(target, target_is_directory=True)
            swapped.append(True)
        return fd

    monkeypatch.setattr(instance.pinned_fs, "create_and_open_dir_pinned", open_root_then_swap)
    key = instance.session_instance_key(ROOT)
    try:
        instance.stage_preview(key, template_id="fixture-board")
    except instance.InstanceError:
        pass
    assert swapped, "the hook never ran, so the race was not exercised"
    assert list(target.iterdir()) == [], "the page directory was created through the swapped link"


def test_a_crewmate_slugged_like_the_session_pages_dir_is_still_a_crewmate_page():
    """``members/session-dashboards/dashboard`` is a crewmate's page, not a session's."""
    slug = "session-dashboards"
    instance.stage_preview(slug, template_id="fixture-board")
    instance.apply_preview(slug)
    instance.adopt(slug, "fixture-board")
    instance.rollback(slug, 1)
    assert instance.read(slug).template_id == "fixture-board"
    assert (instance.instance_dir(slug) / "versions" / "3.json").is_file()


def test_an_unpinned_session_page_refuses_a_linked_versions_dir(tmp_path, monkeypatch):
    """Where nothing can be pinned, a linked ``versions`` must not take a commit."""
    monkeypatch.setattr(instance.pinned_fs, "supports_pinned_walk", lambda: False)
    other = tmp_path / "other-versions"
    other.mkdir()
    (other / "1.json").write_text('{"other": true}')
    key = instance.session_instance_key(ROOT)
    page = instance.instance_dir(key)
    page.mkdir(parents=True)
    (page / "versions").symlink_to(other, target_is_directory=True)
    with pytest.raises(instance.InstanceError):
        instance.adopt(key, "fixture-board")
    assert (other / "1.json").read_text() == '{"other": true}'
    assert sorted(p.name for p in other.iterdir()) == ["1.json"]
