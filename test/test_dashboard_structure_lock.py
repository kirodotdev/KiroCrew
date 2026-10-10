"""The person's structure lock on a crewmate's Dashboard tab.

The lock freezes WHICH page the tab shows. While it is set the store refuses every new
page version -- adopt, apply, edit and rollback -- with :class:`InstanceLocked`, which
the agent surface answers ``dashboard_locked``. A preview still stages, and the lock is
set and cleared only by the owner-gated dashboard route, never by an MCP tool.
"""

from __future__ import annotations

import json
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import members as _members_mod
from kiro_crew.dashboard import server
from kiro_crew.dashboard.handlers import agent_panel
from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates import catalog, instance
from kiro_crew.mcp_panel import _list_tools

_REAL_MEMBER_SLUG = _members_mod.member_slug

SLUG = "fleet-conductor"
MEMBER = "Fleet Conductor"
PAGE = '<div><b data-dashboard-field="credits"></b><i data-dashboard-field="phase"></i></div>'
OTHER_PAGE = '<section><b data-dashboard-field="credits"></b></section>'
REPO = Path(__file__).resolve().parents[1]


def _manifest(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
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
def _home(tmp_path, _floor_monkeypatch):
    """An isolated data home, two fixture built-ins, and the member checks stubbed.

    The owner gate is stubbed OPEN so the internal-secret refusal below is the only
    thing between an agent's caller and the lock; its own test closes it.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    root = tmp_path / "builtin"
    other = _manifest(
        id="fixture-other",
        title="Fixture other",
        fields={"credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}}},
    )
    for manifest, page in ((_manifest(), PAGE), (other, OTHER_PAGE)):
        directory = root / str(manifest["id"])
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (directory / "template.html").write_text(page, encoding="utf-8")
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: root)

    cfg = SimpleNamespace(agents={MEMBER: SimpleNamespace(member_id="")})
    _floor_monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    _floor_monkeypatch.setattr(routes.members_mod, "member_slug", lambda name, config=None: SLUG)
    _floor_monkeypatch.setattr(routes.members_mod, "validate_slug", lambda slug: slug)
    _floor_monkeypatch.setattr(routes.members_mod, "is_dispatchable_member_name", bool)
    _floor_monkeypatch.setattr(
        routes.members_mod, "canonical_member_key", lambda name, cfg=None: name
    )
    _floor_monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: [MEMBER])
    _floor_monkeypatch.setattr(routes, "_deny_app_caller", _none)
    _floor_monkeypatch.setattr(routes, "_owner_only", _none)
    _floor_monkeypatch.setattr(routes, "_write_session", lambda slug, member: "")
    yield


async def _none(*_a: Any, **_k: Any) -> None:
    return None


# ------------------------------------------------------------------ the store


class TestLockedRefusesEveryStructureChange:
    def test_adopt_is_refused_and_the_page_stays(self) -> None:
        instance.adopt(SLUG, "fixture-board")
        instance.set_structure_lock(SLUG, True)
        with pytest.raises(instance.InstanceLocked, match="ask them to unlock"):
            instance.adopt(SLUG, "fixture-other")
        current = instance.read(SLUG)
        assert (current.instance_version, current.html) == (1, PAGE)

    def test_apply_is_refused_and_the_preview_stays_staged(self) -> None:
        instance.adopt(SLUG, "fixture-board")
        instance.set_structure_lock(SLUG, True)
        # Staging is not a structure change: the person can still be shown a page.
        instance.stage_preview(SLUG, template_id="fixture-other")
        with pytest.raises(instance.InstanceLocked):
            instance.apply_preview(SLUG)
        assert instance.read(SLUG).html == PAGE
        assert instance.staged_preview(SLUG) is not None

    def test_rollback_is_refused(self) -> None:
        instance.adopt(SLUG, "fixture-board")
        instance.adopt(SLUG, "fixture-other")
        instance.set_structure_lock(SLUG, True)
        with pytest.raises(instance.InstanceLocked):
            instance.rollback(SLUG, 1)
        assert instance.read(SLUG).instance_version == 2

    def test_a_manifest_edit_is_refused(self) -> None:
        instance.adopt(SLUG, "fixture-board")
        instance.set_structure_lock(SLUG, True)
        with pytest.raises(instance.InstanceLocked):
            instance.edit(SLUG, manifest=_manifest(title="Renamed"))

    def test_unlocking_lets_the_staged_page_land(self) -> None:
        instance.adopt(SLUG, "fixture-board")
        instance.set_structure_lock(SLUG, True)
        instance.stage_preview(SLUG, template_id="fixture-other")
        instance.set_structure_lock(SLUG, False)
        applied = instance.apply_preview(SLUG)
        assert (applied.html, applied.structure_locked) == (OTHER_PAGE, False)

    def test_a_crewmate_that_never_adopted_can_be_locked(self) -> None:
        """The default page is a page too, so locking it must hold off the first adopt."""
        locked = instance.set_structure_lock(SLUG, True)
        assert (locked.state, locked.structure_locked) == (instance.STATE_EMPTY, True)
        assert locked.wire()["structure_locked"] is True
        with pytest.raises(instance.InstanceLocked):
            instance.adopt(SLUG, "fixture-board")


class TestTheLockLivesWhereAnAgentCannotWrite:
    """``members/<slug>/`` is writable by the crewmate's own shell; the lock is not there."""

    def test_rewriting_instance_json_does_not_lift_the_lock(self) -> None:
        instance.adopt(SLUG, "fixture-board")
        instance.set_structure_lock(SLUG, True)
        record = instance.instance_dir(SLUG) / "instance.json"
        raw = json.loads(record.read_text(encoding="utf-8"))
        raw["structure_locked"] = False
        record.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(instance.InstanceLocked):
            instance.adopt(SLUG, "fixture-other")

    def test_copying_an_old_version_over_instance_json_does_not_change_the_locked_page(
        self,
    ) -> None:
        """While locked, the page is the snapshot in the lock file, not the writable record."""
        instance.adopt(SLUG, "fixture-board")
        instance.adopt(SLUG, "fixture-other")
        instance.set_structure_lock(SLUG, True)
        directory = instance.instance_dir(SLUG)
        old = (directory / "versions" / "1.json").read_text(encoding="utf-8")
        (directory / "instance.json").write_text(old, encoding="utf-8")
        current = instance.read(SLUG)
        assert (current.instance_version, current.html) == (2, OTHER_PAGE)

    def test_lock_and_unlock_write_nothing_under_the_member_tree(self) -> None:
        """``members/<slug>/dashboard`` can be a symlink the crewmate's shell planted.

        Pointed at a decoy directory here: a lock and an unlock must leave every file
        in it -- names, bytes and mtimes -- exactly as they were.
        """
        instance.adopt(SLUG, "fixture-board")
        directory = instance.instance_dir(SLUG)
        decoy = directory.parent / "decoy"
        directory.rename(decoy)
        directory.symlink_to(decoy, target_is_directory=True)

        def listing() -> dict[str, tuple[bytes, int]]:
            return {
                str(f.relative_to(decoy)): (f.read_bytes(), f.stat().st_mtime_ns)
                for f in sorted(decoy.rglob("*"))
                if f.is_file()
            }

        # The instance lock adopt left behind, so a writer that recreates it is seen.
        (decoy / ".lock").unlink()
        before = listing()
        instance.set_structure_lock(SLUG, True)
        instance.set_structure_lock(SLUG, False)
        assert listing() == before
        assert not instance.lock_path(SLUG).exists()

    def test_unlocking_over_a_corrupted_record_still_unlocks(self) -> None:
        instance.adopt(SLUG, "fixture-board")
        instance.set_structure_lock(SLUG, True)
        (instance.instance_dir(SLUG) / "instance.json").write_text("{not json", encoding="utf-8")
        unlocked = instance.set_structure_lock(SLUG, False)
        assert unlocked.structure_locked is False
        assert not instance.lock_path(SLUG).exists()

    def test_a_lock_set_while_the_record_is_read_wins(self, _floor_monkeypatch) -> None:
        """A read that saw no lock, then a lock and an agent's write: the snapshot is served."""
        instance.adopt(SLUG, "fixture-board")
        instance.adopt(SLUG, "fixture-other")
        real = instance._read_stored
        old = (instance.instance_dir(SLUG) / "versions" / "1.json").read_text(encoding="utf-8")

        def racing(slug: str) -> instance.Instance:
            _floor_monkeypatch.setattr(instance, "_read_stored", real)
            instance.set_structure_lock(slug, True)
            (instance.instance_dir(slug) / "instance.json").write_text(old, encoding="utf-8")
            return real(slug)

        _floor_monkeypatch.setattr(instance, "_read_stored", racing)
        current = instance.read(SLUG)
        assert (current.html, current.structure_locked) == (OTHER_PAGE, True)

    def test_a_lock_file_that_cannot_be_read_reads_as_locked(self) -> None:
        """Only a MISSING lock file is unlocked; any other error must not fail open."""
        instance.adopt(SLUG, "fixture-board")
        instance.lock_path(SLUG).mkdir(parents=True)
        assert instance.is_locked(SLUG) is True
        # And the writable record is not served in its place.
        current = instance.read(SLUG)
        assert (current.state, current.html, current.structure_locked) == (
            instance.STATE_ERROR,
            "",
            True,
        )
        with pytest.raises(instance.InstanceLocked):
            instance.adopt(SLUG, "fixture-other")

    def test_the_lock_file_is_in_the_sandbox_masked_tool_fenced_store(self) -> None:
        from kiro_crew import sandbox
        from kiro_crew.security.paths import is_sensitive_path

        path = instance.lock_path(SLUG)
        assert path.parent.name == "crew-panels"
        assert "crew-panels" in sandbox._CREW_HIDDEN_LEAVES
        assert is_sensitive_path(str(path))
        assert instance.instance_dir(SLUG) not in path.parents

    def test_a_store_that_cannot_answer_reads_as_locked(self, _floor_monkeypatch) -> None:
        instance.adopt(SLUG, "fixture-board")

        def broken(_slug: str) -> Path:
            raise OSError("unreadable")

        _floor_monkeypatch.setattr(instance, "lock_path", broken)
        assert instance.is_locked(SLUG) is True
        with pytest.raises(instance.InstanceLocked):
            instance.adopt(SLUG, "fixture-other")


class TestTheCrewmateIsToldOnEveryTurn:
    """The ``[DASHBOARD]`` turn block says LOCKED, so the crewmate never offers a page."""

    def test_a_locked_adopted_page_carries_the_locked_line(self) -> None:
        from kiro_crew import dashboard_agentic

        instance.adopt(SLUG, "fixture-board")
        assert "LOCKED" not in dashboard_agentic.turn_block(SLUG)
        instance.set_structure_lock(SLUG, True)
        block = dashboard_agentic.turn_block(SLUG)
        assert block.startswith("[DASHBOARD]\n"), block
        assert instance.LOCKED_REFUSAL in block
        assert "template fixture-board v1" in block
        assert "Fields you write: phase." in block, "values still flow while locked"

    def test_a_locked_crewmate_with_no_page_is_still_told(self) -> None:
        from kiro_crew import dashboard_agentic

        instance.set_structure_lock(SLUG, True)
        assert dashboard_agentic.turn_block(SLUG) == f"[DASHBOARD]\n{instance.LOCKED_REFUSAL}"

    def test_the_line_forbids_every_page_change_and_points_at_the_person(self) -> None:
        for word in ("preview", "apply", "roll back", "propose", "unlock"):
            assert word in instance.LOCKED_REFUSAL, word


class TestTheLockOutlivesEverythingButTheUnlock:
    """Keyed by the immutable member slug, and stored apart from sessions and member files."""

    @pytest.mark.asyncio
    async def test_the_route_keys_the_lock_by_member_id_not_the_display_name(
        self, _floor_monkeypatch
    ) -> None:
        """A renamed crewmate keeps its member_id, so the lock lands on that slug."""
        from kiro_crew import members as members_mod

        renamed = "Renamed Crewmate"
        cfg = SimpleNamespace(agents={renamed: SimpleNamespace(member_id=SLUG)})
        _floor_monkeypatch.setattr(routes.members_mod, "member_slug", _REAL_MEMBER_SLUG)
        _floor_monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        _floor_monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: [renamed])
        assert members_mod.slug_for_name(renamed) != SLUG, "the display name derives another slug"
        async with _client() as client:
            url = f"/api/members/{SLUG}/dashboard/lock?member={renamed.replace(' ', '+')}"
            assert (await client.post(url, json={"locked": True})).status == 200
        assert instance.lock_path(SLUG).name == f"{SLUG}.dashboard-lock"
        assert instance.is_locked(SLUG)

    @pytest.mark.asyncio
    async def test_reset_conversation_keeps_the_lock_and_keeps_the_unlock(self) -> None:
        """The real reset route on the mate's DM slot touches neither state of the lock."""
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_reset_conversation
        from kiro_crew.dashboard.state import DashboardState, _ChatSlot

        slot = _ChatSlot(f"dashboard:member-{SLUG}")
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.sessions = MagicMock()
        state.sessions.discard_conversation = AsyncMock()
        state.sessions.get_provider = MagicMock(return_value=None)
        state.subagents = None
        app = web.Application()
        app["state"] = state

        @web.middleware
        async def as_dashboard_user(request: web.Request, handler: Any) -> web.StreamResponse:
            request["app"] = ""
            return await handler(request)

        app.middlewares.append(as_dashboard_user)
        app.router.add_post(
            "/api/chat/slots/{slot}/reset-conversation", api_chat_slot_reset_conversation
        )

        async def reset() -> None:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(f"/api/chat/slots/{slot.key}/reset-conversation")
                assert resp.status == 200, await resp.text()

        instance.adopt(SLUG, "fixture-board")
        instance.set_structure_lock(SLUG, True)
        await reset()
        assert instance.is_locked(SLUG)
        instance.set_structure_lock(SLUG, False)
        await reset()
        assert not instance.is_locked(SLUG)

    def test_wiping_the_member_dashboard_folder_keeps_the_lock(self) -> None:
        """A cleared chat, a new DM or a member-folder wipe never reaches ``crew-panels``."""
        import shutil

        instance.adopt(SLUG, "fixture-board")
        instance.set_structure_lock(SLUG, True)
        shutil.rmtree(instance.instance_dir(SLUG))
        assert instance.is_locked(SLUG)
        with pytest.raises(instance.InstanceLocked):
            instance.adopt(SLUG, "fixture-board")

    def test_a_fresh_process_reads_the_lock_from_disk(self) -> None:
        """A gateway restart is a new process: the lock is a file, never process memory."""
        import os
        import sys as _sys

        instance.set_structure_lock(SLUG, True)
        out = subprocess.run(
            [
                _sys.executable,
                "-c",
                "from kiro_crew.dashboard_templates import instance; "
                f"print(instance.is_locked({SLUG!r}))",
            ],
            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
            capture_output=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
        assert out == "True"


# ---------------------------------------------------------- the agent surface


def test_the_agent_hears_dashboard_locked_not_a_plain_refusal() -> None:
    resp = agent_panel._instance_refusal(instance.InstanceLocked(instance.LOCKED_REFUSAL))
    assert resp.status == 423
    body = json.loads(resp.body)
    assert body["code"] == "dashboard_locked"
    assert "ask them to unlock" in body["error"]


# ------------------------------------------------------------ the owner route


@asynccontextmanager
async def _client():
    app = web.Application()
    routes.register_member_dashboard_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


LOCK_URL = f"/api/members/{SLUG}/dashboard/lock?member={MEMBER.replace(' ', '+')}"
READ_URL = f"/api/members/{SLUG}/dashboard?member={MEMBER.replace(' ', '+')}"


@pytest.mark.asyncio
async def test_the_person_locks_and_the_read_says_so() -> None:
    instance.adopt(SLUG, "fixture-board")
    async with _client() as client:
        resp = await client.post(LOCK_URL, json={"locked": True})
        assert resp.status == 200
        assert (await resp.json())["structure_locked"] is True
        read = await (await client.get(READ_URL)).json()
        assert read["structure_locked"] is True


@pytest.mark.asyncio
async def test_the_default_page_read_carries_the_lock(_floor_monkeypatch) -> None:
    """An unadopted crewmate is served the default page; the lock must ride along."""
    _floor_monkeypatch.setattr(instance, "DEFAULT_TEMPLATE_ID", "fixture-board")
    instance.set_structure_lock(SLUG, True)
    async with _client() as client:
        read = await (await client.get(READ_URL)).json()
    assert read["template"]["id"] == "fixture-board", "the default page is what was served"
    assert read["structure_locked"] is True


@pytest.mark.asyncio
async def test_the_internal_secret_refusal_is_audited(_floor_monkeypatch) -> None:
    from unittest.mock import MagicMock

    from kiro_crew import sel as sel_mod

    recorder = MagicMock()
    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    async with _client() as client:
        resp = await client.post(
            LOCK_URL, json={"locked": True}, headers={"X-Internal-Secret": "s"}
        )
        assert resp.status == 403
    recorder.log_api_access.assert_called_once()
    kwargs = recorder.log_api_access.call_args.kwargs
    assert (kwargs["operation"], kwargs["outcome"]) == ("members.dashboard_lock", "denied")


@pytest.mark.asyncio
async def test_an_internal_secret_caller_cannot_flip_the_lock() -> None:
    """An MCP server presents the internal secret for an agent; that caller is refused."""
    async with _client() as client:
        resp = await client.post(
            LOCK_URL, json={"locked": True}, headers={"X-Internal-Secret": "s"}
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "human_only"
    assert instance.read(SLUG).structure_locked is False


@pytest.mark.asyncio
async def test_a_non_owner_cannot_flip_the_lock(_floor_monkeypatch) -> None:
    async def deny(_request: Any, _operation: str) -> web.Response:
        return web.json_response({"error": "no", "code": "owner_only"}, status=403)

    _floor_monkeypatch.setattr(routes, "_owner_only", deny)
    async with _client() as client:
        resp = await client.post(LOCK_URL, json={"locked": True})
        assert resp.status == 403
    assert instance.read(SLUG).structure_locked is False


@pytest.mark.asyncio
async def test_a_non_boolean_is_refused() -> None:
    async with _client() as client:
        resp = await client.post(LOCK_URL, json={"locked": "yes"})
        assert resp.status == 400
    assert instance.read(SLUG).structure_locked is False


@asynccontextmanager
async def _gated_client(user: str, *, internal: bool = False):
    """The route behind the REAL owner gate, with the identity the token middleware stamps.

    ``request["user"]`` is the subject of a signed dashboard token, and
    ``request["internal_auth"]`` is set only for a caller that presented the internal
    secret. Those two stamps are how the route tells the person from an agent.
    """
    from kiro_crew.dashboard.handlers import _shared

    @web.middleware
    async def stamp(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = user
        request["app"] = ""
        if internal:
            request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[stamp])
    app["state"] = SimpleNamespace(owner_id="owner-subject", broadcast_ws=lambda *_a: None)
    app.router.add_post("/api/members/{slug}/dashboard/lock", routes.api_member_dashboard_lock)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c, _shared
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_the_real_owner_gate_admits_the_owner_and_refuses_anyone_else(
    _floor_monkeypatch,
) -> None:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    _floor_monkeypatch.setattr(routes, "_owner_only", require_owner_dashboard_request)
    async with _gated_client("someone-else") as (client, _):
        assert (await client.post(LOCK_URL, json={"locked": True})).status == 403
    assert instance.read(SLUG).structure_locked is False
    async with _gated_client("owner-subject") as (client, _):
        assert (await client.post(LOCK_URL, json={"locked": True})).status == 200
    assert instance.read(SLUG).structure_locked is True


@pytest.mark.asyncio
async def test_an_internal_auth_grant_is_refused_even_for_the_owner_subject(
    _floor_monkeypatch,
) -> None:
    """The middleware's internal-secret grant alone refuses, whatever subject rides along."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    _floor_monkeypatch.setattr(routes, "_owner_only", require_owner_dashboard_request)
    async with _gated_client("owner-subject", internal=True) as (client, _):
        resp = await client.post(LOCK_URL, json={"locked": True})
        assert resp.status == 403
        assert (await resp.json())["code"] == "human_only"
    assert instance.read(SLUG).structure_locked is False


def test_an_agent_shell_cannot_read_the_key_that_signs_owner_tokens(tmp_path) -> None:
    """The owner's dashboard token is signed with ``token_signing.key`` in the data home.

    An agent's shell and file tools pass the sensitive-path gate, which refuses that
    key for read and write, so an agent cannot mint the owner token the gate above asks
    for. (The sandbox also masks the file unreadable; see ``sandbox``.)
    """
    from kiro_crew.config.paths import data_home
    from kiro_crew.security.paths import is_sensitive_path

    assert is_sensitive_path(str(data_home() / "token_signing.key"))


# ------------------------------------------------- no agent path reaches it


def _admitted(path: str, prefixes: frozenset[str]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def test_no_internal_secret_path_admits_the_lock_route() -> None:
    """The lock route is cookie-only: no strict or mixed internal prefix covers it."""
    path = f"/api/members/{SLUG}/dashboard/lock"
    assert not _admitted(path, server._STRICT_INTERNAL_API_PATHS)
    assert not _admitted(path, server._MIXED_INTERNAL_API_PATHS)


def test_no_mcp_tool_names_or_takes_a_lock() -> None:
    for tool in _list_tools():
        assert "lock" not in tool["name"]
        props = tool.get("inputSchema", {}).get("properties", {})
        assert not [p for p in props if "lock" in p], tool["name"]


def test_only_the_owner_route_calls_the_setter() -> None:
    """Every caller of ``set_structure_lock`` in the product is the owner-gated route.

    A source scan rather than a behaviour test because the property is an ABSENCE: an
    MCP module that called the setter would be a path no route test here exercises.
    """
    out = subprocess.run(
        ["git", "grep", "-l", "-E", r"set_structure_lock|/dashboard/lock", "--", "src"],
        cwd=REPO,
        capture_output=True,
        encoding="utf-8",
        check=True,
    ).stdout.split()
    assert sorted(out) == [
        "src/kiro_crew/dashboard/handlers/member_dashboard.py",
        "src/kiro_crew/dashboard/server_runtime/mcp_routes.py",
        "src/kiro_crew/dashboard_templates/instance.py",
    ]
    mcp_routes = (REPO / "src/kiro_crew/dashboard/server_runtime/mcp_routes.py").read_text(
        encoding="utf-8"
    )
    assert re.search(
        r'"/api/members/\{slug\}/dashboard/lock",\s*_deferred\("member_dashboard", '
        r'"api_member_dashboard_lock"\)',
        mcp_routes,
    )
