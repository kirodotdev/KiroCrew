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

import json
import os
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


GLOBAL = "global-agent"
PROJECT = "proj-agent"


def _force_scan_unverifiable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every project-agent scan degrade as an unpinnable platform would.

    Runs on every platform, including one that can genuinely pin: the scan is
    replaced outright rather than merely toggling the capability flag, because
    ``agent_discovery`` binds ``supports_pinned_walk`` directly at import, so
    patching ``pinned_fs.supports_pinned_walk`` alone leaves its own internal
    check reading the real name and completing the real scan underneath the
    mock. The ``pinned_fs`` patch still selects ``scan_unsupported_platform``
    over ``default_agent_scope_unverifiable`` in the handler that catches it.
    """

    def _raise(*_a: Any, **_k: Any) -> frozenset[str]:
        raise agent_discovery.ScanUnverifiable("cannot pin")

    monkeypatch.setattr(agent_discovery, "project_agent_names", _raise)
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)


def _arm_scan(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, unpinnable: bool):
    """Run the real (or by-name) scan, or force every scan to degrade unpinnable."""
    if unpinnable:
        _force_scan_unverifiable(monkeypatch)
    else:
        request.getfixturevalue("pinned_walk")


#: Each scenario's Windows-side counterpart. With no pinned walk a scan can
#: confirm neither presence nor absence of the agent, so the outcome is always
#: ``_UNVERIFIABLE`` -- never acceptance and never plain out-of-scope.
_PIN_MODES = pytest.mark.parametrize("unpinnable", [False, True], ids=["pinned", "unpinnable"])
_UNVERIFIABLE = (503, "scan_unsupported_platform")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(chat_folders, "_global_agent_scope", lambda: (frozenset({GLOBAL}), GLOBAL))
    return _make_state(tmp_path)


@pytest.fixture
def scanned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every project scan; each one would report ``PROJECT`` as declared."""
    calls: list[str] = []

    def _scan(project_dir: str, **_kwargs: Any) -> frozenset[str]:
        calls.append(project_dir)
        return frozenset({PROJECT})

    monkeypatch.setattr(agent_discovery, "project_agent_names", _scan)
    return calls


def _project(tmp_path, name: str, *agents: str) -> str:
    root = tmp_path / name
    agents_dir = root / ".kiro" / "agents"
    agents_dir.mkdir(parents=True)
    for agent in agents:
        (agents_dir / f"{agent}.json").write_text(json.dumps({"name": agent}))
    return str(root.resolve())


#: The request stamps each caller class carries by the time the handler runs.
_CALLERS: dict[str, dict[str, Any]] = {
    # The signed machine-local bootstrap subject IS the owner when no owner id
    # is configured (``is_owner_dashboard_request``).
    "owner": {"app": "", "user": "local-app"},
    "app": {"app": "test-app"},
    # The private-chat gate has already verified this V2 member and stamped its
    # store-derived folder principal before the handler runs.
    "member": {
        "app": "",
        "user": "member-user",
        "member_chat_principal": "member:crew-a",
    },
    # An allow-listed Telegram/Teams/Slack caller's dashboard token:
    # ``request["app"] == ""`` -- EXACTLY the person's own shape -- but a
    # non-owner subject. ``request["app"] == ""`` is necessary but not
    # sufficient for owner identity (see ``AGENTS.md``): a scope check keyed on
    # "is ``request_app`` empty?" instead of ``is_owner_dashboard_request``
    # cannot tell this caller from the person.
    "telegram": {"app": "", "user": "telegram:allow-listed-user"},
    # The internal-secret (MCP) caller: ``token_auth`` stamps ``internal_auth``
    # and leaves ``request["app"]`` ABSENT, so ``is_owner_dashboard_request`` is
    # False for it -- the ``chat_folder_create`` / ``_ensure_chat_folder_path``
    # POST and the ``chat_folder_move`` PATCH.
    "internal": {"internal_auth": True},
}


def _app(state, caller: str) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _caller(request: web.Request, handler: Any) -> Any:
        for key, value in _CALLERS[caller].items():
            request[key] = value
        return await handler(request)

    app.middlewares.append(_caller)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    return app


async def _post(state, body: dict, *, caller: str = "owner") -> tuple[int, dict]:
    async with TestClient(TestServer(_app(state, caller))) as client:
        resp = await client.post("/api/chat/folders", json=body)
        return resp.status, await resp.json()


async def _patch(state, fid: str, body: dict, *, caller: str = "owner") -> tuple[int, dict]:
    async with TestClient(TestServer(_app(state, caller))) as client:
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
        "_link_chain_refused",
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


@pytest.mark.parametrize(
    ("ancestor_target", "refused"),
    [("//attacker/share", True), ("C:/local/elsewhere", False)],
    ids=("unc-ancestor", "local-ancestor"),
)
def test_validate_project_dir_screens_links_with_the_hooks_screen(
    tmp_path, monkeypatch, ancestor_target, refused
):
    """The preflight is the real Windows link screen, not a double of it: a
    junction ancestor aimed at an untrusted share is refused before ``realpath``,
    and a local one is swapped in and resolved."""
    from windows_link_screen_helpers import simulate_windows_link_screen

    junction = tmp_path / "junction"
    raw = str(junction / "project")
    screened = simulate_windows_link_screen(
        monkeypatch, {str(junction): ancestor_target}, path_module=os.path
    )
    probes: list[str] = []
    monkeypatch.setattr(
        chat_folders.os.path, "realpath", lambda path: probes.append(path) or str(tmp_path)
    )
    monkeypatch.setattr(
        chat_folders,
        "_log_unc_denied",
        lambda path: "project_dir refers to an untrusted UNC share",
    )

    resolved, error = chat_folders._validate_project_dir(raw)

    assert screened == [os.path.normcase(str(junction))]
    if refused:
        assert (resolved, error) == ("", "project_dir refers to an untrusted UNC share")
        assert probes == []
    else:
        assert probes == [raw]
        assert error is None


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
@_PIN_MODES
async def test_create_project_agent_of_own_directory(
    state, tmp_path, request, monkeypatch, unpinnable
):
    """Accepted when its own directory declares it; never confirmable unpinned."""
    proj = _project(tmp_path, "p", PROJECT)
    _arm_scan(request, monkeypatch, unpinnable)
    status, body = await _post(state, {"name": "F", "project_dir": proj, "default_agent": PROJECT})
    if unpinnable:
        assert (status, body["code"]) == _UNVERIFIABLE
        assert state._folders == []
    else:
        assert status == 201
        assert body["default_agent"] == PROJECT


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
@_PIN_MODES
async def test_create_refuses_agent_another_directory_declares(
    state, tmp_path, request, monkeypatch, unpinnable
):
    """An unscannable directory is never read as lacking the agent."""
    _project(tmp_path, "other", PROJECT)
    proj = _project(tmp_path, "p")
    _arm_scan(request, monkeypatch, unpinnable)
    status, body = await _post(state, {"name": "F", "project_dir": proj, "default_agent": PROJECT})
    expected = _UNVERIFIABLE if unpinnable else (400, "default_agent_out_of_scope")
    assert (status, body["code"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["app", "telegram"])
async def test_create_non_owner_closes_the_directory_oracle(state, tmp_path, scanned, caller):
    """A non-owner cannot make the gateway scan a supplied directory.

    Runs on every platform: ``is_owner=False`` keeps the request out of the scan
    branch entirely, so no walk capability is at stake. ``app`` is a non-owner
    app principal; ``telegram`` is an allow-listed messaging caller whose
    empty-app token is not the owner.

    Directory discovery is gated on ``is_owner_dashboard_request``, not on
    ``if request_app:`` — which is falsy for the ``telegram`` caller, the same
    shape as the person's own request. Gating on the app claim alone lets this
    caller's response (400 out-of-scope vs 503 unverifiable vs 201 accepted)
    reveal whether ``<dir>/.kiro/agents`` declares the named agent, a
    name-presence oracle for an arbitrary host path. The response is therefore
    IDENTICAL whether or not the directory declares the agent, and the
    directory is never scanned.
    """
    proj_with_agent = _project(tmp_path, "has-it", PROJECT)
    proj_without_agent = _project(tmp_path, "lacks-it")

    status_has, body_has = await _post(
        state,
        {"name": "F1", "project_dir": proj_with_agent, "default_agent": PROJECT},
        caller=caller,
    )
    status_lacks, body_lacks = await _post(
        state,
        {"name": "F2", "project_dir": proj_without_agent, "default_agent": PROJECT},
        caller=caller,
    )

    # The oracle is closed: identical outcome regardless of what the named
    # directory actually contains, and the directory is never touched.
    assert status_has == status_lacks == 400
    assert body_has["code"] == body_lacks["code"] == "default_agent_out_of_scope"
    assert scanned == []

    # Still validated against the GLOBAL roster — a non-owner is not simply
    # failed outright.
    status_global, body_global = await _post(
        state,
        {"name": "F3", "project_dir": proj_with_agent, "default_agent": GLOBAL},
        caller=caller,
    )
    assert status_global == 201
    assert body_global["default_agent"] == GLOBAL
    assert scanned == []


@pytest.mark.asyncio
async def test_update_telegram_style_non_owner_closes_the_directory_oracle(
    state, tmp_path, scanned
):
    """The update path must not reopen the oracle the create path just closed.

    Same regression as the create-path test, on ``check_folder_agent_scope``'s
    other caller: a scope-sensitive PATCH (setting ``project_dir``) from an
    allow-listed messaging caller's empty-app token must answer identically
    whether or not the named directory declares the agent, and must never
    scan it. Runs on every platform, like the create-path test.
    """
    proj_with_agent = _project(tmp_path, "has-it-2", PROJECT)
    proj_without_agent = _project(tmp_path, "lacks-it-2")
    state._folders = [_folder("f1"), _folder("f2")]

    status_has, body_has = await _patch(
        state, "f1", {"project_dir": proj_with_agent, "default_agent": PROJECT}, caller="telegram"
    )
    status_lacks, body_lacks = await _patch(
        state,
        "f2",
        {"project_dir": proj_without_agent, "default_agent": PROJECT},
        caller="telegram",
    )

    assert status_has == status_lacks == 400
    assert body_has["code"] == body_lacks["code"] == "default_agent_out_of_scope"
    assert scanned == []

    status_global, body_global = await _patch(
        state, "f1", {"project_dir": proj_with_agent, "default_agent": GLOBAL}, caller="telegram"
    )
    assert status_global == 200
    assert body_global["default_agent"] == GLOBAL
    assert scanned == []


@pytest.mark.asyncio
@_PIN_MODES
async def test_create_child_inherits_agent_and_directory(
    state, tmp_path, request, monkeypatch, unpinnable
):
    """A child with no own pick takes the parent's stored pair without a scan.

    Nothing the request names re-scopes the inherited agent, so the outcome is
    the same where the platform cannot pin.
    """
    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [_folder("par", project_dir=proj, default_agent=PROJECT)]
    _arm_scan(request, monkeypatch, unpinnable)
    status, body = await _post(state, {"name": "Child", "parent_id": "par"})
    assert status == 201, body


@pytest.mark.asyncio
@_PIN_MODES
async def test_create_child_refused_when_own_directory_drops_inherited_agent(
    state, tmp_path, request, monkeypatch, unpinnable
):
    """An empty own pick still runs the parent's agent, so it is validated."""
    parent_proj = _project(tmp_path, "p", PROJECT)
    child_proj = _project(tmp_path, "c")
    state._folders = [_folder("par", project_dir=parent_proj, default_agent=PROJECT)]
    _arm_scan(request, monkeypatch, unpinnable)
    status, body = await _post(
        state, {"name": "Child", "parent_id": "par", "project_dir": child_proj}
    )
    expected = _UNVERIFIABLE if unpinnable else (400, "default_agent_out_of_scope")
    assert (status, body["code"]) == expected


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
@pytest.mark.parametrize(
    "body",
    [
        {"parent_id": "dest"},
        {"default_agent": PROJECT},
        {"project_dir": "__stored__"},
    ],
)
async def test_update_unchanged_binding_moves_without_a_scan_when_unpinnable(
    state, tmp_path, monkeypatch, body
):
    """An unchanged own project agent under its own dir never reaches the scan.

    Where the platform cannot pin, every project scan raises, so evaluating the
    round-trip exception after the scan made a plain sidebar drag (or a body
    re-sending the stored agent or directory) of a folder bound to a project
    agent answer 503 ``scan_unsupported_platform`` forever.
    """
    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [
        _folder("f1", project_dir=proj, default_agent=PROJECT),
        _folder("dest"),
    ]
    if body.get("project_dir") == "__stored__":
        body = {"project_dir": proj}
    _force_scan_unverifiable(monkeypatch)
    status, resp = await _patch(state, "f1", body)
    assert status == 200, resp
    assert state._folders[0]["default_agent"] == PROJECT
    assert state._folders[0]["project_dir"] == proj


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
    """Changing only parent_id can change the scope of the folder's own agent.

    The child's own project agent is declared by the directory it inherits
    from ``valid-parent``; under ``invalid-parent`` no directory declares it.
    """
    project_dir = _project(tmp_path, "project", PROJECT)
    state._folders = [
        _folder("valid-parent", project_dir=project_dir),
        _folder("invalid-parent"),
        _folder("child", parent_id="valid-parent", default_agent=PROJECT),
    ]

    status, body = await _patch(state, "child", {"parent_id": "invalid-parent"})

    assert status == 400
    assert body["code"] == "default_agent_out_of_scope"
    assert state._folders[2]["parent_id"] == "valid-parent"


@pytest.mark.asyncio
@_PIN_MODES
async def test_update_rescope_breaks_round_trip(state, tmp_path, request, monkeypatch, unpinnable):
    """The exception needs the seeded directory too; a new dir re-validates.

    In both modes the new directory is not committed.
    """
    proj = _project(tmp_path, "p")
    state._folders = [_folder("f1", default_agent="ghost")]
    _arm_scan(request, monkeypatch, unpinnable)
    status, body = await _patch(state, "f1", {"project_dir": proj})
    expected = _UNVERIFIABLE if unpinnable else (400, "default_agent_out_of_scope")
    assert (status, body["code"]) == expected
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
@pytest.mark.parametrize(
    "scan,expected",
    [
        ("pinned", (200, None)),
        ("unverifiable", (503, "default_agent_scope_unverifiable")),
        ("unpinnable", _UNVERIFIABLE),
    ],
)
async def test_update_project_agent_of_stored_directory(
    state, tmp_path, request, monkeypatch, scan, expected
):
    """A PATCH that only sets ``default_agent`` scans the already-stored directory.

    A scan that cannot run is never read as 'the directory declares none': it is
    503 (``scan_unsupported_platform`` where the platform cannot pin) and the new
    agent is not committed.
    """
    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [_folder("f1", project_dir=proj, default_agent="")]
    if scan == "pinned":
        request.getfixturevalue("pinned_walk")
    else:
        _force_scan_unverifiable(monkeypatch)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: scan == "unverifiable")
    status, body = await _patch(state, "f1", {"default_agent": PROJECT})
    assert (status, body.get("code")) == expected, body
    if scan == "pinned":
        assert body["default_agent"] == PROJECT
    else:
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

    status, body = await _patch(state, "owned", {"default_agent": "not-in-any-scope"}, caller="app")

    assert status == 403
    assert body["code"] == "folder_not_owned"
    assert body["error"] == "this app does not own that folder"


@pytest.mark.asyncio
async def test_update_reparent_does_not_disclose_foreign_parent_agent(state, tmp_path):
    """A foreign destination is refused before its inherited agent is named.

    A crew member owns the folder being moved, whose valid directory declares
    no agent of its own. Reparenting it beneath another principal's folder
    would inherit that folder's project-scoped agent. Scope validation must not
    reveal the inherited name before the locked destination-ownership check can
    answer the same generic ownership refusal.
    """
    source_dir = _project(tmp_path, "source")
    destination_dir = _project(tmp_path, "destination", PROJECT)
    state._folders = [
        _folder(
            "source",
            owner_app="member:crew-a",
            project_dir=source_dir,
            default_agent="",
        ),
        _folder(
            "destination",
            owner_app="foreign-app",
            project_dir=destination_dir,
            default_agent=PROJECT,
        ),
    ]

    status, body = await _patch(state, "source", {"parent_id": "destination"}, caller="member")

    assert status == 403
    assert body == {
        "error": "this app does not own that folder",
        "code": "folder_not_owned",
    }
    assert PROJECT not in json.dumps(body)
    assert state._folders[0].get("parent_id", "") == ""


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
        caller="app",
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


@pytest.mark.asyncio
async def test_internal_move_of_a_folder_bound_to_its_own_project_agent(state, tmp_path, scanned):
    """An MCP ``chat_folder_move`` of an unchanged own binding is accepted, unscanned.

    The internal-transport PATCH carries only ``parent_id``; the folder's own
    ``default_agent`` is a project agent its own ``project_dir`` declares. The
    stored pair round-trips without touching disk, so the non-owner refusal
    does not apply. A caller-supplied agent or directory on the same transport
    is still validated against the global roster only.
    """
    proj = _project(tmp_path, "p", PROJECT)
    other = _project(tmp_path, "q", PROJECT)
    state._folders = [
        _folder("f1", project_dir=proj, default_agent=PROJECT),
        _folder("dest"),
    ]

    status, body = await _patch(state, "f1", {"parent_id": "dest"}, caller="internal")
    assert status == 200, body
    assert state._folders[0]["parent_id"] == "dest"
    assert state._folders[0]["default_agent"] == PROJECT
    assert state._folders[0]["project_dir"] == proj

    # Re-pointing the directory is a new binding: refused without a scan.
    status, body = await _patch(state, "f1", {"project_dir": other}, caller="internal")
    assert status == 400
    assert body["code"] == "default_agent_out_of_scope"
    assert state._folders[0]["project_dir"] == proj
    assert scanned == []


@pytest.mark.asyncio
async def test_internal_move_between_siblings_of_one_project_keeps_inherited_dir(
    state, tmp_path, scanned
):
    """An MCP move of a folder that pins its own agent and INHERITS its directory.

    Both siblings sit under the project root, so the move leaves the declared
    directory, and with it the folder's binding, unchanged: accepted from the
    in-memory chain alone, unscanned. A destination under a different project
    re-scopes the agent and is still refused.
    """
    proj = _project(tmp_path, "p", PROJECT)
    other = _project(tmp_path, "q", PROJECT)
    state._folders = [
        _folder("root", project_dir=proj),
        _folder("a", parent_id="root"),
        _folder("b", parent_id="root"),
        _folder("f1", parent_id="a", default_agent=PROJECT),
        _folder("elsewhere", project_dir=other),
    ]

    status, body = await _patch(state, "f1", {"parent_id": "b"}, caller="internal")
    assert status == 200, body
    assert state._folders[3]["parent_id"] == "b"
    assert state._folders[3]["default_agent"] == PROJECT

    status, body = await _patch(state, "f1", {"parent_id": "elsewhere"}, caller="internal")
    assert (status, body["code"]) == (400, "default_agent_out_of_scope")
    assert state._folders[3]["parent_id"] == "b"
    assert scanned == []


@pytest.mark.asyncio
async def test_internal_create_inheriting_a_project_agent_is_not_refused(state, tmp_path, scanned):
    """A non-owner naming no agent of its own inherits the parent's, unscanned.

    Regression: closing the non-owner directory oracle refused EVERY non-owner
    whose effective agent was project-scoped, including an MCP folder create
    under a parent the person pinned to a project agent — a 400 naming an
    agent the caller never sent. Runs on every platform: the accepted path
    must not scan at all, which is also what keeps the oracle closed.
    """
    proj = _project(tmp_path, "p", PROJECT)
    state._folders = [_folder("par", project_dir=proj, default_agent=PROJECT)]

    status, body = await _post(state, {"name": "Child", "parent_id": "par"}, caller="internal")
    assert status == 201, body
    assert body["parent_id"] == "par"
    assert body["default_agent"] == ""

    # The security property is unchanged: an agent or a directory the
    # caller SUPPLIES is still validated against the global roster only.
    for extra in ({"default_agent": PROJECT}, {"project_dir": _project(tmp_path, "c")}):
        status, body = await _post(
            state, {"name": "X", "parent_id": "par", **extra}, caller="internal"
        )
        assert status == 400
        assert body["code"] == "default_agent_out_of_scope"
    assert scanned == []


# ── stored folders from before the scoped picker ──

#: A project agent a folder could store while the picker listed the ACTIVE
#: chat's agents rather than the folder's own scope: neither the global rows
#: nor the folder's directory (when it has one) declare it.
_LEGACY = "legacy-agent"


def _legacy_parent(tmp_path, with_dir: bool) -> dict[str, Any]:
    """A parent whose stored agent its own project_dir (or lack of one) lacks."""
    if with_dir:
        return _folder("par", project_dir=_project(tmp_path, "p", PROJECT), default_agent=_LEGACY)
    return _folder("par", default_agent=_LEGACY)


@pytest.mark.asyncio
@_PIN_MODES
@pytest.mark.parametrize("with_dir", [True, False], ids=["parent-dir", "no-dir"])
async def test_owner_agentless_subfolder_under_a_legacy_parent_is_created(
    state, tmp_path, request, monkeypatch, unpinnable, with_dir
):
    """An owner's subfolder naming no agent and no directory is not trapped.

    Its effective pair is exactly the parent's stored one, which the request
    does not change, so there is nothing new to validate and nothing is
    scanned -- the same answer a non-owner already gets. Validating the
    inherited agent instead made every agent-less child create 400 under such a
    parent, and 503 ``scan_unsupported_platform`` on Windows for any parent
    with a directory, which no retry can clear.
    """
    state._folders = [_legacy_parent(tmp_path, with_dir)]
    _arm_scan(request, monkeypatch, unpinnable)
    calls: list[str] = []
    real_scan = agent_discovery.project_agent_names

    def _spy(project_dir: str, **kwargs: Any) -> frozenset[str]:
        calls.append(project_dir)
        return real_scan(project_dir, **kwargs)

    monkeypatch.setattr(agent_discovery, "project_agent_names", _spy)

    status, body = await _post(state, {"name": "Child", "parent_id": "par"})

    assert status == 201, body
    assert body["parent_id"] == "par"
    assert body["default_agent"] == ""
    assert calls == []


@pytest.mark.asyncio
@_PIN_MODES
@pytest.mark.parametrize("with_dir", [True, False], ids=["parent-dir", "no-dir"])
async def test_owner_move_of_an_agentless_folder_into_a_legacy_parent(
    state, tmp_path, request, monkeypatch, unpinnable, with_dir
):
    """A ``parent_id``-only PATCH of a folder with no own agent or directory
    takes the destination's stored pair unchanged, so it is not refused."""
    state._folders = [_legacy_parent(tmp_path, with_dir), _folder("f1")]
    _arm_scan(request, monkeypatch, unpinnable)

    status, body = await _patch(state, "f1", {"parent_id": "par"})

    assert status == 200, body
    assert state._folders[1]["parent_id"] == "par"


@pytest.mark.asyncio
async def test_owner_agentless_subfolder_naming_its_own_directory_is_still_validated(
    state, tmp_path, pinned_walk
):
    """Only a request that names neither field is exempt: a new directory
    re-scopes the inherited agent, so the pair is new and is checked."""
    state._folders = [_legacy_parent(tmp_path, with_dir=False)]
    child_dir = _project(tmp_path, "c", PROJECT)

    status, body = await _post(
        state, {"name": "Child", "parent_id": "par", "project_dir": child_dir}
    )

    assert (status, body["code"]) == (400, "default_agent_out_of_scope")
    assert len(state._folders) == 1


# ── the global roster is read only when an agent needs it ──


def _roster_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> tuple[frozenset[str], str]:
        raise OSError("config unreadable")

    monkeypatch.setattr(chat_folders, "_global_agent_scope", _raise)


@pytest.mark.asyncio
async def test_plain_create_succeeds_when_the_roster_cannot_be_read(state, monkeypatch):
    """A 'New folder' that names no agent and no directory stores nothing the
    roster decides, so a config or catalog read failure does not refuse it."""
    _roster_unreadable(monkeypatch)

    status, body = await _post(state, {"name": "New folder"})

    assert status == 201, body
    assert body["default_agent"] == ""
    assert body["project_dir"] == ""


@pytest.mark.asyncio
async def test_inheriting_create_does_not_read_the_roster(state, monkeypatch):
    """A child inheriting its parent's stored agent needs no roster at all."""
    state._folders = [_folder("par", default_agent=GLOBAL)]
    _roster_unreadable(monkeypatch)

    status, body = await _post(state, {"name": "Child", "parent_id": "par"})

    assert status == 201, body


@pytest.mark.asyncio
async def test_unreadable_roster_still_refuses_a_named_directory(state, tmp_path, monkeypatch):
    """A request that supplies a field still needs the roster to validate it."""
    _roster_unreadable(monkeypatch)

    status, body = await _post(state, {"name": "F", "project_dir": _project(tmp_path, "p")})

    assert (status, body["code"]) == (503, "default_agent_scope_unverifiable")
    assert state._folders == []
