"""Homes, Bots and archives for the instance export and import round trip.

``test_portability_bots_round_trip.py`` builds real Bots on one data home and Kiro
home, exports them through ``create_export_zip`` and imports the archive into another
pair with ``apply_import_zip``. Nothing here is a fixture: the test module owns those.

* :class:`Homes` switches the data home (``KIROCREW_HOME``) and the Kiro home
  (``KIRO_HOME``) between named pairs under ``tmp_path``, so each side of a round trip
  has its own config, stores, transcripts and crewmate files.
* :func:`create_bot` creates a Bot the way ``kirocrew agent create`` does: a private
  version-2 memory store, its own workspace, one locked config write, then the minimal
  crewmate file. :func:`boot_first_crewmate` makes the first crewmate ``mate`` the way a
  gateway's boot does, its crewmate file included.
* :func:`apply_dashboard` saves a dashboard package to the real artifact store and
  puts it in force with ``instance.apply_package``.
* :func:`members_app` serves the thread and dashboard routes with the standalone owner
  as the caller; :func:`open_thread`, :func:`get_dashboard` and :func:`chat_in_dm` call
  them on the current home.
* :func:`rewrite_archive` copies an archive member by member in Python, so a hostile
  variant is data the test builds, never a command line.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, _invalidate_config_cache
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard_templates import catalog, instance

#: The top directory of an archive this module writes from nothing.
ARCHIVE_PREFIX = "kirocrew-export"
#: The template a Bot adopts before its package is applied.
TEMPLATE_ID = "fixture-board"
TEMPLATE_PAGE = '<div><b data-dashboard-field="credits"></b></div>'


def install_fixture_catalog(root: Path, mp: pytest.MonkeyPatch) -> None:
    """One built-in template these tests own, so ``instance.adopt`` has something to adopt."""
    manifest = {
        "id": TEMPLATE_ID,
        "version": 1,
        "title": "Fixture board",
        "description": "A template these tests own.",
        "source": "builtin",
        "fields": {"credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}}},
    }
    builtin = root / "builtin" / TEMPLATE_ID
    builtin.mkdir(parents=True)
    (builtin / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (builtin / "template.html").write_text(TEMPLATE_PAGE, encoding="utf-8")
    mp.setattr(catalog, "builtin_dir", lambda: builtin.parent)


class Homes:
    """Named data-home and Kiro-home pairs under one root; :meth:`use` switches to one."""

    def __init__(self, root: Path, mp: pytest.MonkeyPatch) -> None:
        self.root = root
        self.mp = mp
        self.current = ""

    def use(self, name: str) -> Path:
        """Point ``KIROCREW_HOME`` and ``KIRO_HOME`` at pair *name*; return its data home."""
        kiro = self.root / name / "kiro"
        (kiro / "agents").mkdir(parents=True, exist_ok=True)
        self.mp.setenv("KIROCREW_HOME", str(self.root / name / "crew"))
        self.mp.setenv("KIRO_HOME", str(kiro))
        _invalidate_config_cache()
        self.current = name
        return data_home()

    def kiro(self, name: str) -> Path:
        return self.root / name / "kiro"


@dataclass(frozen=True)
class MadeBot:
    """A Bot :func:`create_bot` made, with what its derived DM thread is keyed by."""

    name: str
    slug: str
    member_id: str
    store: str
    workspace: str

    @property
    def slot_key(self) -> str:
        from kiro_crew.members import member_slot_key

        return member_slot_key(self.slug, self.store)

    @property
    def history_key(self) -> str:
        from kiro_crew.members import member_thread_session_alias

        return member_thread_session_alias(self.slug, self.store)


def create_bot(name: str, *, crewmate_file: bool = True) -> MadeBot:
    """Create Bot *name* on the current home as ``kirocrew agent create`` does."""
    from kiro_crew import mate_config
    from kiro_crew.member_workspace import AUTO_WORKSPACE, provision_member_workspace
    from kiro_crew.members import member_slug
    from kiro_crew.memory_stores import persist_member_config, provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents[name] = KiroCrewAgentConfig(kiro_agent=name, workspace=AUTO_WORKSPACE)
    provision_member_memory(cfg, name)
    workspace = provision_member_workspace(cfg, name)
    persist_member_config(cfg, name, create=True, new_workspace=workspace)
    if crewmate_file:
        mate_config.write_mate_config(name)
    cfg = KiroCrewConfig.load()
    agent = cfg.agents[name]
    return MadeBot(
        name=name,
        slug=member_slug(name, cfg),
        member_id=agent.member_id,
        store=agent.memory_store,
        workspace=agent.workspace,
    )


#: A worker base the boot migration reads (``mate_config_migration._worker_base``); the
#: real one is generated at gateway boot, which these tests do not run.
WORKER_BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "You do the work you are given.",
    "mcpServers": {},
    "tools": ["fs_read"],
    "allowedTools": ["fs_read"],
}


def boot_first_crewmate() -> MadeBot:
    """Boot the current home the way a gateway does for its first crewmate ``mate``.

    ``create_first_crewmate_once`` (the config entry, its private store and workspace,
    the owed welcome), then the boot migration ``migrate_existing_crewmates``, which
    moves it onto its own crewmate file seeded from the default template, against
    :data:`WORKER_BASE`. Returns it as a :class:`MadeBot`.
    """
    from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME, MATE_WORKER_AGENT_FILENAME
    from kiro_crew.agent_materialization.first_crewmate import create_first_crewmate_once
    from kiro_crew.config.paths import mate_agents_dir
    from kiro_crew.mate_config_migration import migrate_existing_crewmates
    from kiro_crew.members import member_slug

    mates = mate_agents_dir()
    mates.mkdir(parents=True, exist_ok=True)
    (mates / MATE_WORKER_AGENT_FILENAME).write_text(json.dumps(WORKER_BASE), encoding="utf-8")
    create_first_crewmate_once()
    _invalidate_config_cache()
    report = migrate_existing_crewmates()
    assert report.migrated == [ASSISTANT_MEMBER_NAME], report
    _invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    agent = cfg.agents[ASSISTANT_MEMBER_NAME]
    assert agent.kiro_agent == ASSISTANT_MEMBER_NAME, agent
    return MadeBot(
        name=ASSISTANT_MEMBER_NAME,
        slug=member_slug(ASSISTANT_MEMBER_NAME, cfg),
        member_id=agent.member_id,
        store=agent.memory_store,
        workspace=agent.workspace,
    )


def dashboard_package(slug: str, html: str) -> dict[str, Any]:
    """A valid dashboard package bound to the Bot of *slug*, showing *html*."""
    return dict(
        dp.validate_package(
            {
                "kind": "dashboard",
                "bound_to": f"crewmate:{slug}",
                "model": {
                    "types": {
                        "goal": {
                            "type": "text",
                            "label": "Goal",
                            "source": {"fold": "work", "path": "conductor.goal"},
                        }
                    }
                },
                "view": {"html": html},
                "theme": {"tokens": {}},
            }
        )
    )


@dataclass(frozen=True)
class AppliedDashboard:
    package: str
    version: int
    digest: str
    html: str


def apply_dashboard(bot: MadeBot, html: str) -> AppliedDashboard:
    """Save a package for *bot* to the artifact store and put it in force."""
    store = ArtifactStore(data_home() / "artifacts")
    art = store.create(
        name=f"{bot.slug} board",
        content=dp.dump_package(dashboard_package(bot.slug, html)),
        kind=dp.DASHBOARD_KIND,
        source="dashboard",
    )
    loaded = store.get(art.slug)
    content = dp.canonical_package_content(loaded.content or "")
    instance.adopt(bot.slug, TEMPLATE_ID)
    record = instance.apply_package(
        bot.slug, package_slug=art.slug, package_version=int(loaded.version), content=content
    )
    return AppliedDashboard(art.slug, int(loaded.version), str(record.package_digest), html)


async def open_thread(slug: str) -> tuple[int, dict[str, Any], list[str]]:
    """POST the thread route on the current home: the status, the body, the slot's messages."""
    from chat_test_helpers import _make_state

    state = _make_state(data_home() / "sessions")
    async with TestClient(TestServer(members_app(state))) as client:
        resp = await client.post(f"/api/members/{slug}/thread")
        body = await resp.json()
    slot = state._slots.get(body.get("slot_key", "")) if isinstance(body, dict) else None
    messages = [str(m.get("content")) for m in (slot.messages if slot is not None else [])]
    return resp.status, body, messages


async def get_dashboard(bot: MadeBot) -> tuple[int, dict[str, Any]]:
    """GET the Bot's dashboard route on the current home: the status and the body."""
    from chat_test_helpers import _make_state

    state = _make_state(data_home() / "sessions")
    async with TestClient(TestServer(members_app(state))) as client:
        resp = await client.get(f"/api/members/{bot.slug}/dashboard", params={"member": bot.name})
        return resp.status, await resp.json()


async def chat_in_dm(bot: MadeBot, *lines: str) -> None:
    """Open the Bot's DM thread through the route, then write *lines* to its transcript.

    The open binds the thread to the Bot's private store, as a person's first open
    does; the lines stand in for the turns that followed.
    """
    from kiro_crew.history import ConversationLog

    status, body, _messages = await open_thread(bot.slug)
    assert status == 200, body
    assert body["slot_key"] == bot.slot_key, body
    log = ConversationLog(base_dir=data_home() / "sessions")
    for line in lines:
        await asyncio.to_thread(log.append, bot.history_key, "user", line)


def members_app(state: Any) -> web.Application:
    """The member thread and dashboard routes, called as the standalone-local owner."""
    from kiro_crew.dashboard.handlers.member_dashboard import register_member_dashboard_routes
    from kiro_crew.dashboard.handlers.members import api_member_thread

    @web.middleware
    async def _owner(request: web.Request, handler: Callable[..., Any]) -> web.StreamResponse:
        request["app"] = ""
        request["user"] = "local-app"
        response: web.StreamResponse = await handler(request)
        return response

    app = web.Application(middlewares=[_owner])
    app["state"] = state
    app.router.add_post("/api/members/{slug}/thread", api_member_thread)
    register_member_dashboard_routes(app)
    return app


def export_archive(path: Path, *, memory_only: bool = False) -> Path:
    """Export the current home, chats included, to *path*."""
    from kiro_crew.portability import create_export_zip

    data, _manifest = create_export_zip(include_sessions=True, memory_only=memory_only)
    path.write_bytes(data)
    return path


def archive_prefix(path: Path) -> str:
    """The one top directory every member of the archive at *path* sits under."""
    with zipfile.ZipFile(path) as zf:
        tops = {name.partition("/")[0] for name in zf.namelist()}
    assert len(tops) == 1, tops
    return tops.pop()


def archive_members(path: Path) -> dict[str, bytes]:
    """Every file member of the archive at *path*, by its name below the top directory."""
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if not info.is_dir():
                out[info.filename.partition("/")[2]] = zf.read(info)
    return out


def write_archive(path: Path, members: Mapping[str, bytes], prefix: str = ARCHIVE_PREFIX) -> Path:
    """An archive at *path* holding *members* (names below the top directory *prefix*)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in members.items():
            zf.writestr(f"{prefix}/{rel}", data)
    path.write_bytes(buf.getvalue())
    return path


def rewrite_archive(
    src: Path,
    dst: Path,
    *,
    drop: Callable[[str], bool] = lambda _rel: False,
    add: Mapping[str, bytes] | None = None,
    edit: Mapping[str, Callable[[Any], Any]] | None = None,
) -> Path:
    """Copy archive *src* to *dst*: drop members, add others, and edit JSON members.

    Each *edit* entry maps a member name to a function from its parsed JSON to the
    JSON written in its place (it may change the object it is given and return it).
    """
    members = {rel: data for rel, data in archive_members(src).items() if not drop(rel)}
    for rel, change in (edit or {}).items():
        members[rel] = json.dumps(change(json.loads(members[rel]))).encode()
    members.update(add or {})
    return write_archive(dst, members, archive_prefix(src))


def tree_bytes(root: Path) -> dict[str, bytes | None]:
    """Every entry under *root*: file bytes, ``None`` for a directory; lock files left out."""
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
        if not p.name.endswith(".lock")
    }
