"""Server-side folder agent-scope validation (``check_folder_agent_scope``).

``FolderConfigModal`` blocks Save when the folder's EFFECTIVE agent (own pick,
else the nearest ancestor's, else the global default) is absent from its
EFFECTIVE scope (global rows, plus the effective project directory's
``.kiro/agents`` names), except for an edit folder's unchanged own explicit
agent under its unchanged directory. The folder create and update endpoints
re-decide that rule, so a caller that is not the modal cannot store an agent
the folder's chats could never run.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import agent_discovery, pinned_fs
from kiro_crew.dashboard import chat_folders
from kiro_crew.dashboard.chat_folders import (
    api_chat_folder_create,
    api_chat_folder_update,
)


@pytest.fixture
def pinned_walk(monkeypatch: pytest.MonkeyPatch):
    """Exercise the pinned-scan branches on EVERY platform.

    Where the walk can genuinely pin, nothing is substituted and the real scan
    runs, so coverage there is exactly what it was. Where it cannot -- Windows,
    whose ``os.open`` has no ``dir_fd`` support, so ``supports_pinned_walk()`` is
    False -- the scan is doubled with the same enumeration done BY NAME. A skip
    there left every handler branch that consumes a successful scan unexercised
    in ordinary CI, which is the hole this closes; the pin's own swap resistance
    is covered by ``pinned_fs``'s tests, not by these handler tests.

    Replaces the scan outright rather than only toggling the capability, for the
    reason :func:`_force_scan_unverifiable` documents: ``agent_discovery`` binds
    ``supports_pinned_walk`` at import, so patching ``pinned_fs`` alone leaves
    its internal check reading the real name. This is that helper's mirror image.
    """
    if pinned_fs.supports_pinned_walk():
        yield
        return

    def _declared_by_name(project_dir: Any, **_k: Any) -> frozenset[str]:
        agents_dir = Path(str(project_dir)) / ".kiro" / "agents"
        try:
            return frozenset(spec.stem for spec in agents_dir.glob("*.json"))
        except OSError:
            return frozenset()

    monkeypatch.setattr(agent_discovery, "project_agent_names", _declared_by_name)
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: True)
    yield


_needs_pinned_walk = pytest.mark.usefixtures("pinned_walk")

GLOBAL = "global-agent"
PROJECT = "proj-agent"


def _force_scan_unverifiable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every project-agent scan degrade as an unpinnable platform would.

    Runs on every platform, including one that can genuinely pin: the scan is
    replaced outright rather than merely toggling the capability flag,
    because ``agent_discovery`` binds ``supports_pinned_walk`` directly at
    import (``from kiro_crew.pinned_fs import supports_pinned_walk``), so
    patching ``pinned_fs.supports_pinned_walk`` alone leaves its own internal
    check reading the real, unpatched name and completing the real scan
    underneath the mock. Pairing both patches is the shape
    ``test_update_unverifiable_scan_is_503`` already established: this
    forces ``project_agent_names`` to raise :class:`agent_discovery.ScanUnverifiable`
    (so no real filesystem read happens) while ``pinned_fs.supports_pinned_walk``
    still selects the ``scan_unsupported_platform`` code over
    ``default_agent_scope_unverifiable`` in the exception handler that catches it.
    """

    def _raise(*_a: Any, **_k: Any) -> frozenset[str]:
        raise agent_discovery.ScanUnverifiable("cannot pin")

    monkeypatch.setattr(agent_discovery, "project_agent_names", _raise)
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(chat_folders, "_global_agent_scope", lambda: (frozenset({GLOBAL}), GLOBAL))
    return _make_state(tmp_path)


def _project(tmp_path, name: str, *agents: str) -> str:
    root = tmp_path / name
    agents_dir = root / ".kiro" / "agents"
    agents_dir.mkdir(parents=True)
    for agent in agents:
        (agents_dir / f"{agent}.json").write_text(json.dumps({"name": agent}))
    return str(root.resolve())


def _app(state, *, owner: bool) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _caller(request: web.Request, handler: Any) -> Any:
        request["app"] = "" if owner else "test-app"
        if owner:
            # The signed machine-local bootstrap subject IS the owner when no
            # owner id is configured (``is_owner_dashboard_request``).
            request["user"] = "local-app"
        return await handler(request)

    app.middlewares.append(_caller)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    return app


def _telegram_style_app(state) -> web.Application:
    """An allow-listed Telegram/Teams/Slack caller's dashboard token.

    ``request["app"] == ""`` — EXACTLY the person's own shape — but
    ``request["user"]`` is a non-owner subject the owner predicate does not
    recognise. ``request["app"] == ""`` is necessary but not sufficient for
    owner identity (see ``AGENTS.md``): a scope check keyed on "is
    ``request_app`` empty?" instead of ``is_owner_dashboard_request`` cannot
    tell this caller from the person.
    """
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _caller(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = "telegram:allow-listed-user"
        return await handler(request)

    app.middlewares.append(_caller)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    return app


async def _post_telegram(state, body: dict) -> tuple[int, dict]:
    async with TestClient(TestServer(_telegram_style_app(state))) as client:
        resp = await client.post("/api/chat/folders", json=body)
        return resp.status, await resp.json()


async def _patch_telegram(state, fid: str, body: dict) -> tuple[int, dict]:
    async with TestClient(TestServer(_telegram_style_app(state))) as client:
        resp = await client.patch(f"/api/chat/folders/{fid}", json=body)
        return resp.status, await resp.json()


async def _post(state, body: dict, *, owner: bool = True) -> tuple[int, dict]:
    async with TestClient(TestServer(_app(state, owner=owner))) as client:
        resp = await client.post("/api/chat/folders", json=body)
        return resp.status, await resp.json()


async def _patch(state, fid: str, body: dict, *, owner: bool = True) -> tuple[int, dict]:
    async with TestClient(TestServer(_app(state, owner=owner))) as client:
        resp = await client.patch(f"/api/chat/folders/{fid}", json=body)
        return resp.status, await resp.json()


def _folder(fid: str, **fields: Any) -> dict[str, Any]:
    return {"id": fid, "name": fid, "order": 0, "collapsed": False, **fields}


def test_create_folder_record_documents_scaffold_scope_failures():
    doc = " ".join((chat_folders.create_folder_record.__doc__ or "").split())
    assert "held to it for its root and every selected child" in doc
    assert "a refusal lands in the scaffold's per-folder ``failed`` channel" in doc


def test_global_scope_uses_the_execution_catalog(monkeypatch):
    """Global validation accepts every selectable global catalog template."""
    from kiro_crew.dashboard.handlers import agent_catalog

    config = type(
        "Config",
        (),
        {"agents": {"member": object()}, "default_agent": "member"},
    )()
    catalog_template = type("Template", (), {"name": "catalog-template"})()
    monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: config)
    monkeypatch.setattr(agent_catalog, "_templates", lambda project_dir: [catalog_template])

    names, default_agent = chat_folders._global_agent_scope()

    assert names == frozenset({"member", "catalog-template"})
    assert default_agent == "member"


def test_validate_project_dir_refuses_linked_ancestor_before_realpath(tmp_path, monkeypatch):
    """A local link into UNC is refused before realpath can probe it."""
    raw = str(tmp_path / "junction" / "project")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        chat_folders,
        "_linked_ancestor_refused",
        lambda path: calls.append(("linked", path)) or True,
        raising=False,
    )
    monkeypatch.setattr(
        chat_folders,
        "_log_unc_denied",
        lambda path: "project_dir refers to an untrusted UNC share",
    )
    monkeypatch.setattr(
        chat_folders.os.path,
        "realpath",
        lambda path: calls.append(("realpath", path)) or str(tmp_path),
    )

    resolved, error = chat_folders._validate_project_dir(raw)

    assert resolved == ""
    assert error == "project_dir refers to an untrusted UNC share"
    assert calls == [("linked", raw)]


# ── create ──


@pytest.mark.asyncio
async def test_create_refuses_unknown_agent_without_directory(state):
    status, body = await _post(state, {"name": "F", "default_agent": "ghost"})
    assert status == 400
    assert body["code"] == "default_agent_out_of_scope"
    assert state._folders == []


@pytest.mark.asyncio
async def test_create_accepts_global_agent_without_directory(state):
    status, body = await _post(state, {"name": "F", "default_agent": GLOBAL})
    assert status == 201
    assert body["default_agent"] == GLOBAL


@pytest.mark.asyncio
@_needs_pinned_walk
async def test_create_accepts_project_agent_of_own_directory(state, tmp_path):
    proj = _project(tmp_path, "p", PROJECT)
    status, body = await _post(state, {"name": "F", "project_dir": proj, "default_agent": PROJECT})
    assert status == 201
    assert body["default_agent"] == PROJECT


@pytest.mark.asyncio
async def test_create_own_directory_scan_is_reported_unverifiable_when_unpinnable(
    state, tmp_path, monkeypatch
):
    """The Windows-side counterpart to accepting an own directory's agent.

    With no pinned walk available, a create naming its own ``project_dir``
    can never confirm that directory declares the agent, so it must fail
    ``scan_unsupported_platform`` rather than either succeeding or reading as
    plain ``default_agent_out_of_scope``.
    """
    proj = _project(tmp_path, "p", PROJECT)
    _force_scan_unverifiable(monkeypatch)
    status, body = await _post(state, {"name": "F", "project_dir": proj, "default_agent": PROJECT})
    assert status == 503
    assert body["code"] == "scan_unsupported_platform"
    assert state._folders == []


@pytest.mark.asyncio
async def test_create_accepts_global_agent_with_directory(state, tmp_path):
    """The accept-set is global rows UNIONED with the directory's, not replaced.

    No walk of any kind runs here: ``GLOBAL`` is already in ``global_names``, so
    ``check_folder_agent_scope`` returns before it ever reaches the project scan
    -- this contract holds on every platform, pinnable or not.
    """
    proj = _project(tmp_path, "p", PROJECT)
    status, _ = await _post(state, {"name": "F", "project_dir": proj, "default_agent": GLOBAL})
    assert status == 201


@pytest.mark.asyncio
@_needs_pinned_walk
async def test_create_refuses_agent_another_directory_declares(state, tmp_path):
    _project(tmp_path, "other", PROJECT)
    proj = _project(tmp_path, "p")
    status, body = await _post(state, {"name": "F", "project_dir": proj, "default_agent": PROJECT})
    assert status == 400
    assert body["code"] == "default_agent_out_of_scope"


@pytest.mark.asyncio
async def test_create_refusal_is_reported_unverifiable_when_unpinnable(
    state, tmp_path, monkeypatch
):
    """The Windows-side counterpart: an unscannable directory is never read as lacking the agent.

    Without a pinned walk the gateway cannot confirm the directory does NOT
    declare the agent either, so the outcome must be ``scan_unsupported_platform``,
    not the ``default_agent_out_of_scope`` the pinnable platform reports.
    """
    proj = _project(tmp_path, "p")
    _force_scan_unverifiable(monkeypatch)
    status, body = await _post(state, {"name": "F", "project_dir": proj, "default_agent": PROJECT})
    assert status == 503
    assert body["code"] == "scan_unsupported_platform"


@pytest.mark.asyncio
async def test_create_non_owner_uses_global_scope_without_project_scan(
    state, tmp_path, monkeypatch
):
    """A non-owner cannot make the gateway scan a supplied directory.

    Runs on every platform: ``is_owner=False`` raises before
    ``check_folder_agent_scope`` ever imports or calls ``project_agent_names``,
    so nothing here depends on a pinnable walk -- the assertion is precisely
    that the scan is never reached.
    """
    proj = _project(tmp_path, "p", PROJECT)
    scanned: list[str] = []

    def _scan(project_dir: str, **_kwargs: Any) -> frozenset[str]:
        scanned.append(project_dir)
        return frozenset({PROJECT})

    monkeypatch.setattr(agent_discovery, "project_agent_names", _scan)
    status, body = await _post(
        state,
        {"name": "F", "project_dir": proj, "default_agent": PROJECT},
        owner=False,
    )
    assert status == 400
    assert body["code"] == "default_agent_out_of_scope"
    assert scanned == []

    status, body = await _post(
        state,
        {"name": "Global", "project_dir": proj, "default_agent": GLOBAL},
        owner=False,
    )
    assert status == 201
    assert body["default_agent"] == GLOBAL
    assert scanned == []


@pytest.mark.asyncio
async def test_create_telegram_style_non_owner_closes_the_directory_oracle(
    state, tmp_path, monkeypatch
):
    """An allow-listed messaging caller's empty-app token is not the owner.

    Runs on every platform, like the non-owner test above: ``is_owner=False``
    keeps this request out of the scan branch entirely, so no walk capability
    is at stake here.

    Directory discovery is gated on ``is_owner_dashboard_request``, not on
    ``if request_app:`` — which is falsy for this caller, the same shape as the
    person's own request. Gating on the app claim alone lets this caller's
    response (400 out-of-scope vs 503 unverifiable vs 201 accepted) reveal
    whether ``<dir>/.kiro/agents`` declares the named agent, a name-presence
    oracle for an arbitrary host path. The response is therefore IDENTICAL
    whether or not the directory declares the agent, and the directory is
    never scanned.
    """
    proj_with_agent = _project(tmp_path, "has-it", PROJECT)
    proj_without_agent = _project(tmp_path, "lacks-it")
    scanned: list[str] = []

    def _scan(project_dir: str, **_kwargs: Any) -> frozenset[str]:
        scanned.append(project_dir)
        return frozenset({PROJECT})

    monkeypatch.setattr(agent_discovery, "project_agent_names", _scan)

    status_has, body_has = await _post_telegram(
        state, {"name": "F1", "project_dir": proj_with_agent, "default_agent": PROJECT}
    )
    status_lacks, body_lacks = await _post_telegram(
        state, {"name": "F2", "project_dir": proj_without_agent, "default_agent": PROJECT}
    )

    # The oracle is closed: identical outcome regardless of what the named
    # directory actually contains, and the directory is never touched.
    assert status_has == status_lacks == 400
    assert body_has["code"] == body_lacks["code"] == "default_agent_out_of_scope"
    assert scanned == []

    # Still validated against the GLOBAL roster — a non-owner is not simply
    # failed outright.
    status_global, body_global = await _post_telegram(
        state, {"name": "F3", "project_dir": proj_with_agent, "default_agent": GLOBAL}
    )
    assert status_global == 201
    assert body_global["default_agent"] == GLOBAL
    assert scanned == []


@pytest.mark.asyncio
async def test_update_telegram_style_non_owner_closes_the_directory_oracle(
    state, tmp_path, monkeypatch
):
    """The update path must not reopen the oracle the create path just closed.

    Runs on every platform, mirroring the create-path test: ``is_owner=False``
    keeps this PATCH out of the scan branch entirely, so no walk capability is
    at stake here either.

    Same regression as the create-path test, on ``check_folder_agent_scope``'s
    other caller: a scope-sensitive PATCH (setting ``project_dir``) from an
    allow-listed messaging caller's empty-app token must answer identically
    whether or not the named directory declares the agent, and must never
    scan it.
    """
    proj_with_agent = _project(tmp_path, "has-it-2", PROJECT)
    proj_without_agent = _project(tmp_path, "lacks-it-2")
    scanned: list[str] = []

    def _scan(project_dir: str, **_kwargs: Any) -> frozenset[str]:
        scanned.append(project_dir)
        return frozenset({PROJECT})

    monkeypatch.setattr(agent_discovery, "project_agent_names", _scan)

    state._folders = [_folder("f1"), _folder("f2")]

    status_has, body_has = await _patch_telegram(
        state, "f1", {"project_dir": proj_with_agent, "default_agent": PROJECT}
    )
    status_lacks, body_lacks = await _patch_telegram(
        state, "f2", {"project_dir": proj_without_agent, "default_agent": PROJECT}
    )

    assert status_has == status_lacks == 400
    assert body_has["code"] == body_lacks["code"] == "default_agent_out_of_scope"
    assert scanned == []

    status_global, body_global = await _patch_telegram(
        state, "f1", {"project_dir": proj_with_agent, "default_agent": GLOBAL}
    )
    assert status_global == 200
    assert body_global["default_agent"] == GLOBAL
    assert scanned == []


@pytest.mark.asyncio
@_needs_pinned_walk
async def test_create_child_inherits_agent_and_directory(state, tmp_path):
    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [_folder("par", project_dir=proj, default_agent=PROJECT)]
    status, _ = await _post(state, {"name": "Child", "parent_id": "par"})
    assert status == 201


@pytest.mark.asyncio
async def test_create_child_inherited_scan_is_reported_unverifiable_when_unpinnable(
    state, tmp_path, monkeypatch
):
    """The Windows-side counterpart: an inherited directory's scan degrades too.

    A child with no own pick still resolves the parent's directory and scans
    it; without a pinned walk that scan can never confirm the inherited
    agent, so the create must fail ``scan_unsupported_platform`` rather than
    succeed.
    """
    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [_folder("par", project_dir=proj, default_agent=PROJECT)]
    _force_scan_unverifiable(monkeypatch)
    status, body = await _post(state, {"name": "Child", "parent_id": "par"})
    assert status == 503
    assert body["code"] == "scan_unsupported_platform"


@pytest.mark.asyncio
@_needs_pinned_walk
async def test_create_child_refused_when_own_directory_drops_inherited_agent(state, tmp_path):
    """An empty own pick still runs the parent's agent, so it is validated."""
    parent_proj = _project(tmp_path, "p", PROJECT)
    child_proj = _project(tmp_path, "c")
    state._folders = [_folder("par", project_dir=parent_proj, default_agent=PROJECT)]
    status, body = await _post(
        state, {"name": "Child", "parent_id": "par", "project_dir": child_proj}
    )
    assert status == 400
    assert body["code"] == "default_agent_out_of_scope"


@pytest.mark.asyncio
async def test_create_child_own_directory_drop_is_reported_unverifiable_when_unpinnable(
    state, tmp_path, monkeypatch
):
    """The Windows-side counterpart: the own-directory scan degrades before scope is decided.

    Without a pinned walk, the child's own (agent-less) directory can never
    be scanned to confirm it drops the inherited agent, so the create must
    fail ``scan_unsupported_platform`` rather than the ``default_agent_out_of_scope``
    the pinnable platform reports.
    """
    parent_proj = _project(tmp_path, "p", PROJECT)
    child_proj = _project(tmp_path, "c")
    state._folders = [_folder("par", project_dir=parent_proj, default_agent=PROJECT)]
    _force_scan_unverifiable(monkeypatch)
    status, body = await _post(
        state, {"name": "Child", "parent_id": "par", "project_dir": child_proj}
    )
    assert status == 503
    assert body["code"] == "scan_unsupported_platform"


@pytest.mark.asyncio
async def test_create_refuses_when_global_default_is_out_of_scope(state, monkeypatch):
    """No own or inherited pick: the global default is the effective agent."""
    monkeypatch.setattr(chat_folders, "_global_agent_scope", lambda: (frozenset({GLOBAL}), "ghost"))
    status, body = await _post(state, {"name": "F"})
    assert status == 400
    assert body["code"] == "default_agent_out_of_scope"


@pytest.mark.asyncio
async def test_create_accepts_no_effective_agent(state, monkeypatch):
    monkeypatch.setattr(chat_folders, "_global_agent_scope", lambda: (frozenset(), ""))
    status, _ = await _post(state, {"name": "F"})
    assert status == 201


@pytest.mark.asyncio
@_needs_pinned_walk
async def test_create_cannot_commit_after_parent_scope_changes(state, tmp_path, monkeypatch):
    """A project scan cannot commit a child against a newer parent scope.

    No Windows counterpart: the race this proves is between a genuinely
    completing scan and a concurrent mutation, and it wraps the real
    ``project_agent_names`` (captured as ``original_scan`` below) rather than
    replacing it. Without a pinnable walk that wrapped call raises
    ``ScanUnverifiable`` itself, so ``check_folder_agent_scope`` never returns
    normally and the 409 commit-check this test targets is unreachable --
    the pinnable walk is the subject of this test, not an incidental detail
    of how it is set up, so the marker names the capability without an
    unsupported-platform analogue to pin.
    """

    old_project = _project(tmp_path, "old", PROJECT)
    new_project = _project(tmp_path, "new")
    state._folders = [
        _folder("parent", project_dir=old_project, default_agent=GLOBAL),
    ]
    scan_entered = asyncio.Event()
    release_scan = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_scan = agent_discovery.project_agent_names

    def _held_scan(project_dir: str, **kwargs: Any) -> frozenset[str]:
        loop.call_soon_threadsafe(scan_entered.set)
        future = asyncio.run_coroutine_threadsafe(release_scan.wait(), loop)
        future.result(timeout=10)
        return original_scan(project_dir, **kwargs)

    monkeypatch.setattr(agent_discovery, "project_agent_names", _held_scan)
    create = asyncio.create_task(
        _post(
            state,
            {"name": "Child", "parent_id": "parent", "default_agent": PROJECT},
        )
    )
    await asyncio.wait_for(scan_entered.wait(), timeout=10)
    try:
        patch_status, _ = await _patch(state, "parent", {"project_dir": new_project})
    finally:
        release_scan.set()
    create_status, create_body = await asyncio.wait_for(create, timeout=10)

    assert patch_status == 200
    assert create_status == 409
    assert create_body["code"] == "folder_target_changed"
    assert state._folders == [
        _folder("parent", project_dir=new_project, default_agent=GLOBAL),
    ]


# ── update ──


@pytest.mark.asyncio
async def test_update_validates_project_dir_off_the_event_loop(state, tmp_path, monkeypatch):
    """The request path's filesystem validator runs on a worker thread."""
    project_dir = str(tmp_path / "project")
    validation_threads: list[int] = []

    def _validate(raw: str) -> tuple[str, None]:
        validation_threads.append(threading.get_ident())
        return raw, None

    state._folders = [_folder("f1", default_agent=GLOBAL)]
    monkeypatch.setattr(chat_folders, "_validate_project_dir", _validate)
    monkeypatch.setattr(chat_folders, "_folder_project_overlap_denied", lambda _path: None)
    loop_thread = threading.get_ident()

    status, body = await _patch(state, "f1", {"project_dir": project_dir})

    assert status == 200
    assert body["project_dir"] == project_dir
    assert len(validation_threads) == 1
    assert validation_threads[0] != loop_thread


@pytest.mark.asyncio
async def test_update_revalidates_the_stored_project_dir_off_the_event_loop(
    state, tmp_path, monkeypatch
):
    """A PATCH that does not set project_dir still re-screens the stored one.

    ``check_folder_agent_scope`` documents ``own_dir`` as already validated and
    resolves it to scan, and for a UNC spelling that resolution IS the outbound
    SMB/NTLM probe -- it happens inside ``is_sensitive_path``, ahead of the
    Windows unsupported-platform arm, so the 503 does not fence it. An agent
    -scope PATCH that leaves project_dir alone must therefore put the stored
    value through the same validator the incoming branch uses, on a worker
    thread, before the scope check sees it.

    Mutation guard: drop the ``_validate_project_dir`` call on the stored branch
    and the validator never sees the stored spelling, so ``seen`` stays empty.
    """
    stored = str(tmp_path / "stored")
    seen: list[tuple[str, int]] = []

    def _validate(raw: str) -> tuple[str, None]:
        seen.append((raw, threading.get_ident()))
        return raw, None

    state._folders = [_folder("f1", default_agent=GLOBAL, project_dir=stored)]
    monkeypatch.setattr(chat_folders, "_validate_project_dir", _validate)
    loop_thread = threading.get_ident()

    status, _body = await _patch(state, "f1", {"default_agent": GLOBAL})

    assert status == 200
    assert [raw for raw, _ in seen] == [stored], (
        "the stored project_dir must be re-screened before the scope check " f"resolves it: {seen}"
    )
    assert seen[0][1] != loop_thread, "the validator realpaths; it must run off-loop"


@pytest.mark.asyncio
async def test_update_refuses_new_out_of_scope_agent(state):
    state._folders = [_folder("f1", default_agent=GLOBAL)]
    status, body = await _patch(state, "f1", {"default_agent": "ghost"})
    assert status == 400
    assert body["code"] == "default_agent_out_of_scope"
    assert state._folders[0]["default_agent"] == GLOBAL


@pytest.mark.asyncio
async def test_update_round_trips_unchanged_own_orphan(state):
    """An edit folder's OWN saved orphan under its seeded dir stays savable."""
    state._folders = [_folder("f1", default_agent="ghost")]
    status, _ = await _patch(state, "f1", {"default_agent": "ghost"})
    assert status == 200


@pytest.mark.asyncio
async def test_update_not_touching_agent_directory_or_parent_is_never_refused(state):
    """A rename/collapse is not an agent-scope edit, even over an inherited
    orphan the round-trip exception would not cover."""
    state._folders = [
        _folder("par", default_agent="ghost"),
        _folder("f1", parent_id="par", default_agent=""),
    ]
    status, _ = await _patch(state, "f1", {"name": "Renamed", "collapsed": True})
    assert status == 200
    assert state._folders[1]["name"] == "Renamed"


@pytest.mark.asyncio
async def test_update_reparent_revalidates_effective_agent_scope(state, tmp_path):
    """Changing only parent_id can change both inherited agent and scope."""
    project_dir = _project(tmp_path, "project", PROJECT)
    state._folders = [
        _folder("valid-parent", project_dir=project_dir, default_agent=PROJECT),
        _folder("invalid-parent", default_agent=PROJECT),
        _folder("child", parent_id="valid-parent", default_agent=""),
    ]

    status, body = await _patch(state, "child", {"parent_id": "invalid-parent"})

    assert status == 400
    assert body["code"] == "default_agent_out_of_scope"
    assert state._folders[2]["parent_id"] == "valid-parent"


@pytest.mark.asyncio
@_needs_pinned_walk
async def test_update_rescope_breaks_round_trip(state, tmp_path):
    """The exception needs the seeded directory too; a new dir re-validates."""
    proj = _project(tmp_path, "p")
    state._folders = [_folder("f1", default_agent="ghost")]
    status, body = await _patch(state, "f1", {"project_dir": proj})
    assert status == 400
    assert body["code"] == "default_agent_out_of_scope"
    assert not state._folders[0].get("project_dir")


@pytest.mark.asyncio
async def test_update_rescope_is_reported_unverifiable_when_unpinnable(
    state, tmp_path, monkeypatch
):
    """The Windows-side counterpart: rescoping degrades before the round trip is broken.

    Without a pinned walk the new directory's scan can never confirm it
    drops ``"ghost"``, so the PATCH must fail ``scan_unsupported_platform``
    rather than the ``default_agent_out_of_scope`` a pinnable platform reports
    -- and, like the pinnable case, must not commit the new directory.
    """
    proj = _project(tmp_path, "p")
    state._folders = [_folder("f1", default_agent="ghost")]
    _force_scan_unverifiable(monkeypatch)
    status, body = await _patch(state, "f1", {"project_dir": proj})
    assert status == 503
    assert body["code"] == "scan_unsupported_platform"
    assert not state._folders[0].get("project_dir")


@pytest.mark.asyncio
async def test_update_clearing_to_orphan_inherited_agent_is_refused(state):
    """An empty own pick is not an explicit binding, so no round-trip applies."""
    state._folders = [
        _folder("par", default_agent="ghost"),
        _folder("f1", parent_id="par", default_agent=GLOBAL),
    ]
    status, body = await _patch(state, "f1", {"default_agent": ""})
    assert status == 400
    assert body["code"] == "default_agent_out_of_scope"


@pytest.mark.asyncio
@_needs_pinned_walk
async def test_update_accepts_project_agent_of_stored_directory(state, tmp_path):
    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [_folder("f1", project_dir=proj, default_agent="")]
    status, body = await _patch(state, "f1", {"default_agent": PROJECT})
    assert status == 200
    assert body["default_agent"] == PROJECT


@pytest.mark.asyncio
async def test_update_stored_directory_scan_is_reported_unverifiable_when_unpinnable(
    state, tmp_path, monkeypatch
):
    """The Windows-side counterpart: the already-stored directory's scan degrades too.

    A PATCH that only changes ``default_agent`` still re-scans the folder's
    already-stored directory; without a pinned walk that scan can never
    confirm the new agent, so it must fail ``scan_unsupported_platform``
    rather than succeed.
    """
    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [_folder("f1", project_dir=proj, default_agent="")]
    _force_scan_unverifiable(monkeypatch)
    status, body = await _patch(state, "f1", {"default_agent": PROJECT})
    assert status == 503
    assert body["code"] == "scan_unsupported_platform"
    assert state._folders[0]["default_agent"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pinnable,code",
    [(True, "default_agent_scope_unverifiable"), (False, "scan_unsupported_platform")],
)
async def test_update_unverifiable_scan_is_503(state, tmp_path, monkeypatch, pinnable, code):
    """A scan that cannot run is never read as 'the directory declares none'."""
    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [_folder("f1", project_dir=proj, default_agent="")]

    def _raise(*_a: Any, **_k: Any) -> frozenset[str]:
        raise agent_discovery.ScanUnverifiable("cannot pin")

    monkeypatch.setattr(agent_discovery, "project_agent_names", _raise)
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: pinnable)
    status, body = await _patch(state, "f1", {"default_agent": PROJECT})
    assert status == 503
    assert body["code"] == code
    assert state._folders[0]["default_agent"] == ""


@pytest.mark.asyncio
async def test_create_unloadable_global_roster_is_503(state, monkeypatch):
    def _raise() -> tuple[frozenset[str], str]:
        raise OSError("config unreadable")

    monkeypatch.setattr(chat_folders, "_global_agent_scope", _raise)
    status, body = await _post(state, {"name": "F", "default_agent": GLOBAL})
    assert status == 503
    assert body["code"] == "default_agent_scope_unverifiable"
    assert state._folders == []


@pytest.mark.asyncio
@_needs_pinned_walk
async def test_concurrent_scope_patches_cannot_combine_stale_validations(
    state, tmp_path, monkeypatch
):
    """A project scan cannot commit after another PATCH changes the folder snapshot.

    No Windows counterpart, for the same reason as the create-side sibling
    above (``test_create_cannot_commit_after_parent_scope_changes``): this
    wraps the real ``project_agent_names`` and proves a race against its
    genuine completion, which cannot happen without a pinnable walk -- there
    is no unsupported-platform variant of a race whose contended resource
    only exists on platforms that can pin.
    """

    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [_folder("f1", project_dir=proj, default_agent=GLOBAL)]
    scan_entered = asyncio.Event()
    release_scan = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_scan = agent_discovery.project_agent_names

    def _held_scan(project_dir: str, **kwargs: Any) -> frozenset[str]:
        loop.call_soon_threadsafe(scan_entered.set)
        future = asyncio.run_coroutine_threadsafe(release_scan.wait(), loop)
        future.result(timeout=10)
        return original_scan(project_dir, **kwargs)

    monkeypatch.setattr(agent_discovery, "project_agent_names", _held_scan)
    agent_patch = asyncio.create_task(_patch(state, "f1", {"default_agent": PROJECT}))
    await asyncio.wait_for(scan_entered.wait(), timeout=10)
    try:
        dir_status, _ = await _patch(state, "f1", {"project_dir": ""})
    finally:
        release_scan.set()
    agent_status, agent_body = await asyncio.wait_for(agent_patch, timeout=10)

    assert dir_status == 200
    assert agent_status == 409
    assert agent_body["code"] == "folder_target_changed"
    assert state._folders[0]["project_dir"] == ""
    assert state._folders[0]["default_agent"] == GLOBAL


@pytest.mark.asyncio
async def test_update_scope_validation_does_not_leak_before_ownership(state, tmp_path):
    """A non-owner's scope-sensitive PATCH is refused for ownership, not scope.

    Without a preflight ownership check ahead of ``check_folder_agent_scope``,
    an app that does not own the folder would reach scope validation first
    and get a 400 naming the folder's effective agent -- disclosure a
    ``member:`` principal's ownership-filtered GET withholds. The refusal
    here must be the same ownership 403 the locked path answers, not a scope
    400, and it must fire even though the submitted agent is genuinely out
    of scope (proving the check happens before scope is evaluated, not
    merely reaching the same conclusion by coincidence).
    """
    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [
        _folder("owned", owner_app="owner-app", project_dir=proj, default_agent=PROJECT)
    ]

    status, body = await _patch(state, "owned", {"default_agent": "not-in-any-scope"}, owner=False)

    assert status == 403
    assert body["code"] == "folder_not_owned"
    assert body["error"] == "this app does not own that folder"


@pytest.mark.asyncio
async def test_create_scope_validation_does_not_leak_before_parent_ownership(state, tmp_path):
    """Nesting under a foreign parent is refused for ownership, not scope.

    Mirrors the update-path case above for ``api_chat_folder_create``: an
    app naming a ``parent_id`` it does not own must be refused before
    ``check_folder_agent_scope`` runs against that parent's effective scope,
    even when the submitted agent is genuinely out of scope.
    """
    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [
        _folder("owned", owner_app="owner-app", project_dir=proj, default_agent=PROJECT)
    ]

    status, body = await _post(
        state,
        {"name": "Child", "parent_id": "owned", "default_agent": "not-in-any-scope"},
        owner=False,
    )

    assert status == 403
    assert body["code"] == "folder_not_owned"
    assert body["error"] == "cannot create a folder inside one this app does not own"


def test_create_folder_record_defaults_its_owner_claim_closed():
    """``is_owner`` defaults CLOSED and every call site asserts it explicitly.

    An open default handed the person's filesystem authority to any path that
    omitted the argument -- including the scaffold endpoints, whose allow-listed
    messaging subject carries the person's empty ``request_app`` but is not the
    owner, and whose per-folder ``created``/``failed`` split is returned in a
    200 body, so a scan the caller should never have reached discloses whether
    a directory declares an agent.
    """
    import ast
    import inspect
    from pathlib import Path as _Path

    assert (
        inspect.signature(chat_folders.create_folder_record).parameters["is_owner"].default is False
    ), "is_owner must default closed so a caller reaches discovery only by asserting ownership"

    root = _Path(chat_folders.__file__).parent
    omitted: list[str] = []
    for source in sorted(root.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "create_folder_record":
                continue
            if not any(kw.arg == "is_owner" for kw in node.keywords):
                omitted.append(f"{source.name}:{node.lineno}")

    assert omitted == [], (
        "these create_folder_record call sites inherit the owner claim instead "
        f"of asserting it: {omitted}"
    )
