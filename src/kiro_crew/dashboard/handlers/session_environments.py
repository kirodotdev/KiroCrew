"""Choose which Session Environment a dashboard session runs on.

``GET  /api/chat/slots/{slot}/environment`` -> ``{"profile": id | null, "profiles": [...]}``
``PUT  /api/chat/slots/{slot}/environment``  body ``{"profile": id | null}``

Owner-only: the choice decides which machine runs a session's agent, so no app
token may read or change it. A change takes effect the next time the session's
runtime starts; a running runtime is never moved.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers._shared import read_bounded_json, require_owner_dashboard_request

logger = logging.getLogger(__name__)


def _choices():
    from kiro_crew.coder.session_launch import SessionEnvironmentChoices
    from kiro_crew.config.paths import data_home

    return SessionEnvironmentChoices(data_home() / "coder" / "session-choices.json")


def _profiles() -> list[dict[str, object]]:
    from kiro_crew.coder.session_launch import profile_provider
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    section = KiroCrewConfig.load().session_environments
    out: list[dict[str, object]] = []
    if section.coder_url:
        for p in section.profiles:
            out.append(
                {"id": p.id, "name": p.name or p.id, "provider": "coder", "workdir": p.workdir}
            )
    # Sprites and boxd profiles need no Coder URL.
    for key, provider in (("sprites_profiles", "sprites"), ("boxd_profiles", "boxd")):
        for item in getattr(section, key, None) or []:
            if isinstance(item, dict) and item.get("id"):
                out.append(
                    {
                        "id": item["id"],
                        "name": item.get("name") or item["id"],
                        "provider": provider,
                        "workdir": item.get("workdir") or "",
                    }
                )
    available = []
    for item in out:
        try:
            provider = profile_provider(section, str(item["id"]))
        except SessionEnvironmentUnavailable:
            continue  # ambiguous ids are unavailable at launch as well
        if provider == item["provider"]:
            available.append(item)
    return available


async def api_session_environments_check(request: web.Request) -> web.Response:
    """``POST /api/session-environments/check`` -- is the saved setup ready to run?

    Owner-only. Read-only against Coder (``whoami`` and the template list: no
    workspace is created, started or touched) and status-only for git logins
    (exit codes; no credential is read). Answers::

        {"coder": {"ok", "owner" | "error"},
         "profiles": [{"id", "ok", "error"?}],
         "sprites": [{"id", "ok", "error"?}],
         "boxd": [{"id", "ok", "error"?}],
         "git": {host: "signed_in" | "not_signed_in" | "unchecked"}}

    The Sprites check lists at most one sprite per profile (proving its token);
    the boxd check runs one ``boxd machine list`` (proving the CLI's login).
    Neither creates or wakes anything.
    """
    if (
        refusal := await require_owner_dashboard_request(request, "session_environments.check")
    ) is not None:
        return refusal
    from kiro_crew.coder.client import CoderClientError
    from kiro_crew.coder.session_launch import CoderSessionEnvironments
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home
    from kiro_crew.remote_session.git_gateway import HOSTS, login_status

    section = (await asyncio.to_thread(KiroCrewConfig.load)).session_environments
    git = dict(zip(HOSTS, await asyncio.gather(*(login_status(h) for h in HOSTS))))
    others = {"sprites": await _check_sprites(section), "boxd": await _check_boxd(section)}
    if not section.coder_url:
        return web.json_response(
            {
                "coder": {"ok": False, "error": "no Coder URL is saved"},
                "profiles": [],
                **others,
                "git": git,
            }
        )
    client = CoderSessionEnvironments(section, await asyncio.to_thread(data_home))._client()
    try:
        owner, _owner_id = await client.current_user()
    except CoderClientError:
        return web.json_response(
            {
                "coder": {
                    "ok": False,
                    "error": "the gateway is not signed in to Coder; run coder login there",
                },
                "profiles": [],
                **others,
                "git": git,
            }
        )
    profiles: list[dict[str, object]] = []
    for profile in section.profiles:
        try:
            await client.probe(template=profile.template, preset="")
            profiles.append({"id": profile.id, "ok": True})
        except CoderClientError as exc:
            profiles.append({"id": profile.id, "ok": False, "error": str(exc)[:200]})
    return web.json_response(
        {"coder": {"ok": True, "owner": owner}, "profiles": profiles, **others, "git": git}
    )


async def _check_sprites(section: Any) -> list[dict[str, object]]:
    """Per Sprites profile: is its token saved and accepted? Creates or wakes nothing."""
    from kiro_crew.config.paths import data_home
    from kiro_crew.session_environment import SessionEnvironmentUnavailable
    from kiro_crew.sprites_session.launch import NAME_PREFIX, SpritesSessionEnvironments

    environments = SpritesSessionEnvironments(section, await asyncio.to_thread(data_home))
    out: list[dict[str, object]] = []
    for profile in environments.profiles():
        try:
            client = await environments.client(profile)
            await client.list_sprites(prefix=NAME_PREFIX, max_results=1)
            out.append({"id": profile.get("id"), "ok": True})
        except SessionEnvironmentUnavailable as exc:
            out.append({"id": profile.get("id"), "ok": False, "error": str(exc)[:200]})
    return out


async def _check_boxd(section: Any) -> list[dict[str, object]]:
    """Per boxd profile: CLI installed and signed in? One shared ``machine list``.

    Every boxd profile shares the gateway's one ``boxd auth login``, so one
    listing answers for all of them.
    """
    profiles = [p for p in getattr(section, "boxd_profiles", None) or [] if isinstance(p, dict)]
    if not profiles:
        return []
    from kiro_crew.boxd_session.cli import BoxdCLI
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    try:
        cli = await asyncio.to_thread(BoxdCLI)
        await cli.list_machines()
        error = ""
    except SessionEnvironmentUnavailable as exc:
        error = str(exc)[:200]
    return [
        {"id": p.get("id"), "ok": not error, **({"error": error} if error else {})}
        for p in profiles
    ]


async def api_session_environments_workspaces_get(request: web.Request) -> web.Response:
    """``GET /api/session-environments/workspaces`` -- the workspaces Kiro Crew owns.

    Owner-only, registry-only (no Coder command). Each row is a session's
    workspace with what the registry knows of it: its last recorded state and,
    for an archived session, when it will be deleted.
    """
    if (
        refusal := await require_owner_dashboard_request(request, "session_environments.workspaces")
    ) is not None:
        return refusal

    def _read() -> list[dict[str, object]]:
        from kiro_crew.coder.registry import WorkspaceBindingRegistry, WorkspaceRegistryCorrupt
        from kiro_crew.config.paths import data_home

        registry = WorkspaceBindingRegistry(data_home() / "coder" / "session-envs.json")
        try:
            rows = registry.list_bindings()
        except (OSError, WorkspaceRegistryCorrupt):
            rows = ()
        out: list[dict[str, object]] = list(_persistent_workspaces(data_home()))
        for row in rows:
            out.append(
                {
                    "workspace": row.workspace_name,
                    "provider": "coder",
                    "kind": "session",
                    # A dashboard session's slot, so the card can name it.
                    "slot": (
                        row.session_key.split(":", 1)[1]
                        if row.session_key.startswith("dashboard:")
                        else ""
                    ),
                    "state": row.state,
                    "template": row.template,
                    "created_at": row.created_at,
                    "deletion_due_at": row.deletion_due_at,
                }
            )
        out.sort(key=lambda r: str(r["created_at"]))
        return out

    return web.json_response({"workspaces": await asyncio.to_thread(_read)})


def _slot_of(session_key: str) -> str:
    """A dashboard session's slot, so the card can name it."""
    return session_key.split(":", 1)[1] if session_key.startswith("dashboard:") else ""


def _persistent_registries(home: Any) -> list[tuple[str, Any]]:
    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.coder.persistent_vm import SessionResourceRegistry
    from kiro_crew.sprites_session.launch import SpritesSessionEnvironments

    return [
        (cls.label.lower(), SessionResourceRegistry(home / cls.registry_file, cls.label))
        for cls in (SpritesSessionEnvironments, BoxdSessionEnvironments)
    ]


def _persistent_workspaces(home: Any) -> list[dict[str, object]]:
    """Sessions' sprites and boxd machines, from their own registries (no remote call)."""
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    out: list[dict[str, object]] = []
    for provider, registry in _persistent_registries(home):
        try:
            rows = registry.all()
        except SessionEnvironmentUnavailable:
            continue
        for row in rows:
            out.append(
                {
                    "workspace": row.name,
                    "provider": provider,
                    "kind": "session",
                    "slot": _slot_of(row.session_key),
                    # The registry records no live state; the card shows none.
                    "state": "",
                    "template": row.profile_id,
                    "created_at": row.created_at,
                    "deletion_due_at": row.deletion_due_at,
                }
            )
    return out


def _environment_locked(slot: object) -> bool:
    """True once the session has any message: the environment is then fixed."""
    total = getattr(slot, "total_messages", 0)
    return bool(total if isinstance(total, int) else 0) or bool(getattr(slot, "messages", None))


def _remote_workspace_created(session_key: str) -> bool:
    """Whether the registry holds a verified workspace for this session.

    Fails closed: an unreadable registry counts as a workspace that exists.
    """
    from kiro_crew.coder.registry import WorkspaceBindingRegistry, WorkspaceRegistryCorrupt
    from kiro_crew.config.paths import data_home
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    home = data_home()
    for _provider, persistent in _persistent_registries(home):
        try:
            # A row is written before the create call, so one means a VM may exist.
            if persistent.get(session_key) is not None:
                return True
        except SessionEnvironmentUnavailable:
            return True
    registry = WorkspaceBindingRegistry(home / "coder" / "session-envs.json")
    try:
        binding = registry.get_by_session(session_key)
    except (OSError, WorkspaceRegistryCorrupt):
        return True
    return binding is not None and bool(binding.workspace_uuid)


async def _choice_locked(slot: object, session_key: str, profile: str | None) -> bool:
    """Whether this session's current choice ``profile`` is fixed.

    A session with messages keeps its environment, except a remote one whose
    workspace was never created: every turn it took failed before any compute
    existed (an unsupported agent backend, Coder signed out), so nothing lives
    remotely and the user can still move it to this machine.
    """
    if not _environment_locked(slot):
        return False
    if profile is None:
        return True
    return await asyncio.to_thread(_remote_workspace_created, session_key)


def _slot_session_key(request: web.Request) -> str | None:
    slot = request.app["state"]._slots.get(request.match_info["slot"])
    return effective_session_key(slot) if slot is not None else None


async def api_chat_slot_environment_get(request: web.Request) -> web.Response:
    if (
        refusal := await require_owner_dashboard_request(request, "chat.slot_environment")
    ) is not None:
        return refusal
    key = _slot_session_key(request)
    if key is None:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    try:
        profile = await asyncio.to_thread(_choices().get, key)
    except SessionEnvironmentUnavailable as exc:
        return web.json_response({"error": str(exc), "code": "choices_unreadable"}, status=500)
    profiles = await asyncio.to_thread(_profiles)
    from kiro_crew.session_environment import remote_workspace_for, start_progress

    slot = request.app["state"]._slots.get(request.match_info["slot"])
    return web.json_response(
        {
            "profile": profile,
            "profiles": profiles,
            "starting": start_progress(key),
            "locked": await _choice_locked(slot, key, profile),
            # Whether a runtime in this gateway is attached to the remote
            # workspace. A turn on a remote session with none attached is still
            # setting up even before the first start stage is reported, so the
            # UI shows setup there instead of "Thinking…".
            "attached": remote_workspace_for(key) is not None,
        }
    )


async def api_chat_slot_environment_browse(request: web.Request) -> web.Response:
    """``GET .../environment/browse?op=list|read&path=REL`` -- read-only remote files.

    Owner-only, like the choice itself: it reads the session's workspace.
    """
    if (
        refusal := await require_owner_dashboard_request(request, "chat.slot_environment_browse")
    ) is not None:
        return refusal
    key = _slot_session_key(request)
    if key is None:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    from kiro_crew.coder.session_browse import NotRemote, WorkspaceNotRunning, browse_session
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    op = request.query.get("op", "list")
    path = request.query.get("path", ".")
    try:
        result = await browse_session(key, op, path)
    except ValueError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_request"}, status=400)
    except NotRemote:
        return web.json_response(
            {"error": "this session runs on this machine", "code": "not_remote"}, status=409
        )
    except WorkspaceNotRunning as exc:
        return web.json_response({"error": str(exc), "code": "workspace_stopped"}, status=409)
    except SessionEnvironmentUnavailable as exc:
        return web.json_response({"error": str(exc), "code": "unavailable"}, status=502)
    if "error" in result:
        return web.json_response(
            {"error": str(result["error"])[:500], "code": "path_refused"}, status=400
        )
    return web.json_response(result)


async def api_chat_slot_environment_file_put(request: web.Request) -> web.Response:
    """``PUT .../environment/file`` body ``{path, content, expected?}`` -- save a remote file.

    Owner-only. ``expected`` (the text the editor opened) makes a stale save a
    409 instead of overwriting a newer edit by the agent.
    """
    if (
        refusal := await require_owner_dashboard_request(request, "chat.slot_environment_write")
    ) is not None:
        return refusal
    key = _slot_session_key(request)
    if key is None:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    from kiro_crew.coder.session_browse import (
        MAX_WRITE_BYTES,
        NotRemote,
        WorkspaceNotRunning,
        write_session_file,
    )
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    body, error = await read_bounded_json(request, max_bytes=2 * MAX_WRITE_BYTES + 8192)
    if error is not None:
        return error
    assert body is not None
    path, content, expected = body.get("path"), body.get("content"), body.get("expected")
    if not isinstance(path, str) or not isinstance(content, str):
        return web.json_response(
            {"error": "path and content must be strings", "code": "invalid_request"}, status=400
        )
    if expected is not None and not isinstance(expected, str):
        return web.json_response(
            {"error": "expected must be a string", "code": "invalid_request"}, status=400
        )
    try:
        result = await write_session_file(key, path, content, expected)
    except ValueError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_request"}, status=400)
    except NotRemote:
        return web.json_response(
            {"error": "this session runs on this machine", "code": "not_remote"}, status=409
        )
    except WorkspaceNotRunning as exc:
        return web.json_response({"error": str(exc), "code": "workspace_stopped"}, status=409)
    except SessionEnvironmentUnavailable as exc:
        return web.json_response({"error": str(exc), "code": "unavailable"}, status=502)
    if result.get("conflict"):
        return web.json_response(
            {"error": str(result.get("error")), "code": "file_changed"}, status=409
        )
    if "error" in result:
        return web.json_response(
            {"error": str(result["error"])[:500], "code": "path_refused"}, status=400
        )
    from kiro_crew.sel import sel

    sel().log_api_access(
        caller=str(request.get("user") or "dashboard"),
        operation="chat.slot_environment_write",
        outcome="allowed",
        resources=f"session={key} path={str(result.get('path'))[:200]}",
    )
    return web.json_response(result)


async def apply_default_environment(slot: Any) -> bool:
    """Bind a newly created dashboard chat to the default profile, if one is set.

    True when the chat was bound. Applied once, at creation, to a chat with no
    choice and no messages, so it never moves an existing session and the
    composer can still change it before the first message. A default that no
    longer names a saved profile, or any failure, leaves the chat on this
    machine: a default is a convenience, and a failed bind must not fail the
    create. Nothing is started here; the workspace is provisioned at the first
    turn, like a pick in the composer.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        section = (await asyncio.to_thread(KiroCrewConfig.load)).session_environments
        default = str(getattr(section, "default_profile", "") or "")
        if not default or _environment_locked(slot):
            return False
        if default not in {p["id"] for p in await asyncio.to_thread(_profiles)}:
            return False
        key = effective_session_key(slot)
        if not key:
            return False
        choices = _choices()
        if await asyncio.to_thread(choices.get, key) is not None:
            return False
        await asyncio.to_thread(choices.set, key, default)
        return True
    except Exception:  # noqa: BLE001 -- never fails the create; the chat runs locally
        logger.warning("default session environment: could not bind a new chat", exc_info=True)
        return False


async def api_chat_slot_environment_put(request: web.Request) -> web.Response:
    if (
        refusal := await require_owner_dashboard_request(request, "chat.slot_environment")
    ) is not None:
        return refusal
    key = _slot_session_key(request)
    if key is None:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    body, error = await read_bounded_json(request, max_bytes=4096)
    if error is not None:
        return error
    assert body is not None
    profile = body.get("profile")
    if profile is not None and not isinstance(profile, str):
        return web.json_response(
            {"error": "profile must be a string or null", "code": "invalid_request"}, status=400
        )
    if profile is not None:
        known = {p["id"] for p in await asyncio.to_thread(_profiles)}
        if profile not in known:
            return web.json_response(
                {"error": "profile is not configured", "code": "unknown_profile"}, status=400
            )
    from kiro_crew.dashboard.chat_runner import schedule_eager_spawn
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    state = request.app["state"]
    slot = state._slots.get(request.match_info["slot"])
    if slot is None:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    # Same serialization as the agent/model/project switch handlers. The choice
    # only takes effect when the session's runtime starts, so a changed choice
    # arms the slot's deferred reset: an idle runtime (an eager spawn, or one
    # left over from earlier turns) must not keep running where the session no
    # longer lives. Deferred, not inline, for the same reason as the project
    # switch: the reset is consumed by the chat runner, never mid-turn.
    async with slot._lock:
        if effective_session_key(slot) != key:
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )
        choices = _choices()
        try:
            previous = await asyncio.to_thread(choices.get, key)
            if previous != profile and await _choice_locked(slot, key, previous):
                # Picked once, for the session's life: its files, shell state and
                # history live in that environment, and moving mid-conversation
                # would strand them. Start a new session to use another one.
                return web.json_response(
                    {
                        "error": "this session's environment is fixed once it has started",
                        "code": "environment_locked",
                        "profile": previous,
                    },
                    status=409,
                )
            if previous != profile:
                await asyncio.to_thread(choices.set, key, profile)
        except (ValueError, SessionEnvironmentUnavailable) as exc:
            return web.json_response({"error": str(exc), "code": "choice_refused"}, status=400)
        if previous != profile:
            slot._pending_reset_history_key = key
            # Prewarm only a LOCAL session. Prewarming a remote one would
            # create and start billed compute on a mere menu pick, and a user
            # trying options would leave a workspace behind for each.
            if profile is None:
                schedule_eager_spawn(state, slot)
    return web.json_response({"profile": profile, "applies": "next_start"})
