"""Choose which Session Environment a dashboard session runs on.

Owner-only: the choice decides which machine runs the agent. It applies at the
next runtime start; a running runtime never moves.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers._shared import read_bounded_json, require_owner_dashboard_request
from kiro_crew.dashboard.slot_ownership import slot_not_found

logger = logging.getLogger(__name__)


def _choices():
    from kiro_crew.session_environment import _choices as choices

    return choices()


def _profiles() -> list[dict[str, object]]:
    """The profiles a chat may pick; none while the developer flag is off, which
    hides the picker and leaves the default profile unapplied.

    Read from the applied config snapshot, the one a remote start and the file
    browser resolve against, so a profile edit the watcher has not reloaded yet
    is never advertised ahead of them. Disk only before any snapshot exists."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session_environment import session_environments_enabled

    if not session_environments_enabled():
        return []
    config = live.snapshot()
    if config is None:
        config = KiroCrewConfig.load()
    section = config.session_environments
    out: list[dict[str, object]] = []
    if section.coder_url:
        for p in section.profiles:
            out.append(
                {
                    "id": p.id,
                    "name": p.name or p.id,
                    "provider": "coder",
                    "workdir": p.workdir,
                    "template": p.template,
                }
            )
    return out


async def api_session_environments_check(request: web.Request) -> web.Response:
    """Is the saved setup ready to run?

    Owner-only, read-only against Coder (``whoami``, template list). Answers::

        {"coder": {"ok", "owner" | "error"}, "profiles": [{"id", "ok", "error"?}]}
    """
    if (
        refusal := await require_owner_dashboard_request(request, "session_environments.check")
    ) is not None:
        return refusal
    from kiro_crew.coder.client import CoderClientError
    from kiro_crew.coder.session_launch import CoderSessionEnvironments
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    section = (await asyncio.to_thread(KiroCrewConfig.load)).session_environments
    if not section.coder_url:
        return web.json_response(
            {"coder": {"ok": False, "error": "no Coder URL is saved"}, "profiles": []}
        )
    try:
        # Resolving the coder binary walks PATH, and a missing one raises.
        envs = CoderSessionEnvironments(section, await asyncio.to_thread(data_home))
        client = await asyncio.to_thread(envs._client)
    except SessionEnvironmentUnavailable as exc:
        return web.json_response({"coder": {"ok": False, "error": str(exc)[:300]}, "profiles": []})
    try:
        owner, _owner_id = await client.current_user()
    except CoderClientError as exc:
        # The CLI's own words: a timeout or a bad identity reply is not a
        # sign-in problem, and the card shows them under Details.
        return web.json_response({"coder": {"ok": False, "error": str(exc)[:200]}, "profiles": []})
    profiles: list[dict[str, object]] = []
    for profile in section.profiles:
        try:
            await client.probe(template=profile.template, preset="")
            profiles.append({"id": profile.id, "ok": True})
        except CoderClientError as exc:
            profiles.append({"id": profile.id, "ok": False, "error": str(exc)[:200]})
    return web.json_response({"coder": {"ok": True, "owner": owner}, "profiles": profiles})


def _template_names(raw: object) -> list[str]:
    """Template names from ``coder templates list --output json``, sorted.

    Current Coder wraps each record under its display-column label ("Template");
    older releases return the record itself. Anything else is skipped.
    """
    names: set[str] = set()
    for value in raw if isinstance(raw, list) else []:
        if not isinstance(value, dict):
            continue
        record = value.get("Template", value)
        if isinstance(record, dict) and isinstance(record.get("name"), str) and record["name"]:
            names.add(record["name"])
    return sorted(names)[:200]


async def api_session_environments_templates_get(request: web.Request) -> web.Response:
    """``GET /api/session-environments/templates``: owner-only, read-only list of the
    templates the saved Coder deployment offers, for the profile editor's picker.

    Answers ``{"templates": [...]}``, or ``{"templates": [], "error": ...}`` when
    the list cannot be read; the editor then keeps its free-text field.
    """
    if (
        refusal := await require_owner_dashboard_request(request, "session_environments.templates")
    ) is not None:
        return refusal
    from kiro_crew.coder.client import CoderClientError
    from kiro_crew.coder.session_launch import CoderSessionEnvironments
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    section = (await asyncio.to_thread(KiroCrewConfig.load)).session_environments
    if not section.coder_url:
        return web.json_response({"templates": [], "error": "no Coder URL is saved"})
    try:
        envs = CoderSessionEnvironments(section, await asyncio.to_thread(data_home))
        client = await asyncio.to_thread(envs._client)
        raw = client._json(
            await client._call("templates", "list", "--output", "json"), "template query"
        )
    except (SessionEnvironmentUnavailable, CoderClientError) as exc:
        return web.json_response({"templates": [], "error": str(exc)[:200]})
    return web.json_response({"templates": _template_names(raw)})


class _WorkspacesUnreadable(Exception):
    """The workspace registry could not be read (I/O failure or corrupt file)."""


async def api_session_environments_workspaces_get(request: web.Request) -> web.Response:
    """``GET /api/session-environments/workspaces``: owner-only, registry-only (no
    Coder command) state and deletion date per owned workspace."""
    if (
        refusal := await require_owner_dashboard_request(request, "session_environments.workspaces")
    ) is not None:
        return refusal

    def _read() -> list[dict[str, object]]:
        from kiro_crew.coder.registry import WorkspaceBindingRegistry, WorkspaceRegistryCorrupt
        from kiro_crew.config.paths import data_home
        from kiro_crew.session_environment import SessionEnvironmentUnavailable

        registry = WorkspaceBindingRegistry(data_home() / "coder" / "session-envs.json")
        try:
            rows = registry.list_bindings()
        except (OSError, WorkspaceRegistryCorrupt) as exc:
            # Not an empty inventory: workspaces may exist that this read cannot see.
            raise _WorkspacesUnreadable(str(exc)) from exc
        choices = _choices()

        def profile_of(session_key: str) -> str:
            # Which profile the chat is bound to, so the card can say which chats a
            # removed profile strands; unknown reads as none.
            try:
                return choices.get(session_key) or ""
            except (OSError, ValueError, SessionEnvironmentUnavailable):
                return ""

        out: list[dict[str, object]] = []
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
                    "profile": profile_of(row.session_key),
                    "state": row.state,
                    "template": row.template,
                    "created_at": row.created_at,
                    "deletion_due_at": row.deletion_due_at,
                }
            )
        out.sort(key=lambda r: str(r["created_at"]))
        return out

    try:
        workspaces = await asyncio.to_thread(_read)
    except _WorkspacesUnreadable as exc:
        logger.warning("session environments: workspace registry unreadable: %s", exc)
        return web.json_response(
            {"error": "the workspace registry could not be read", "code": "workspaces_unreadable"},
            status=500,
        )
    return web.json_response({"workspaces": workspaces})


def _environment_locked(slot: object) -> bool:
    """True once the session has any message: the environment is then fixed."""
    total = getattr(slot, "total_messages", 0)
    return bool(total if isinstance(total, int) else 0) or bool(getattr(slot, "messages", None))


def _remote_workspace_created(session_key: str) -> bool:
    """Whether a verified workspace exists; an unreadable registry says yes."""
    from kiro_crew.coder.registry import WorkspaceBindingRegistry, WorkspaceRegistryCorrupt
    from kiro_crew.config.paths import data_home

    registry = WorkspaceBindingRegistry(data_home() / "coder" / "session-envs.json")
    try:
        binding = registry.get_by_session(session_key)
    except (OSError, WorkspaceRegistryCorrupt):
        return True
    return binding is not None and bool(binding.workspace_uuid)


async def _choice_locked(slot: object, session_key: str, profile: str | None) -> bool:
    """Whether ``profile`` is fixed: once messaged, unless remote and never created."""
    from kiro_crew.dashboard.state import load_window_off_loop

    try:
        # A restored row's window is read off the loop first: ``messages`` would
        # otherwise read and replay the whole transcript on it.
        await load_window_off_loop(slot)
    except Exception:  # noqa: BLE001 -- an unreadable transcript is not a free switch
        logger.warning("session environment: could not load a restored chat", exc_info=True)
        return True
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
        return slot_not_found()
    from kiro_crew.session_environment import (
        SessionEnvironmentUnavailable,
        session_environments_enabled,
    )

    # With the developer flag off every session runs here, so the store is not
    # read: an unreadable one cannot lock a local chat's Files and Terminal.
    # Part A's ``resolve`` also answers None then; this route does not rely on it.
    if not session_environments_enabled():
        profile = None
    else:
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
            # None attached: a turn is still setting up before its first stage.
            "attached": remote_workspace_for(key) is not None,
        }
    )


async def api_chat_slot_environment_browse(request: web.Request) -> web.Response:
    """``GET .../environment/browse?op=list|read&path=REL``: owner-only remote reads."""
    if (
        refusal := await require_owner_dashboard_request(request, "chat.slot_environment_browse")
    ) is not None:
        return refusal
    key = _slot_session_key(request)
    if key is None:
        return slot_not_found()
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
    """``PUT .../environment/file`` ``{path, content, expected?, workdir?}``: owner-only
    save; a stale ``expected``, or a ``workdir`` (the profile's working folder when the
    file was opened) other than the profile's current one, is a 409, never an overwrite."""
    if (
        refusal := await require_owner_dashboard_request(request, "chat.slot_environment_write")
    ) is not None:
        return refusal
    key = _slot_session_key(request)
    if key is None:
        return slot_not_found()
    from kiro_crew.coder.session_browse import (
        MAX_WRITE_BYTES,
        NotRemote,
        WorkdirChanged,
        WorkspaceNotRunning,
        write_session_file,
    )
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    body, error = await read_bounded_json(request, max_bytes=2 * MAX_WRITE_BYTES + 8192)
    if error is not None:
        return error
    assert body is not None
    path, content, expected = body.get("path"), body.get("content"), body.get("expected")
    workdir = body.get("workdir")
    if not isinstance(path, str) or not isinstance(content, str):
        return web.json_response(
            {"error": "path and content must be strings", "code": "invalid_request"}, status=400
        )
    if expected is not None and not isinstance(expected, str):
        return web.json_response(
            {"error": "expected must be a string", "code": "invalid_request"}, status=400
        )
    if workdir is not None and not isinstance(workdir, str):
        return web.json_response(
            {"error": "workdir must be a string", "code": "invalid_request"}, status=400
        )
    try:
        result = await write_session_file(key, path, content, expected, workdir=workdir)
    except WorkdirChanged:
        # The edits were made against the profile's previous working folder: a
        # same-named file under the folder this write resolves is another file.
        return web.json_response(
            {
                "error": "the profile's working folder changed since this file was opened",
                "code": "root_changed",
            },
            status=409,
        )
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


async def apply_default_environment(
    slot: Any, sessions: Any = None, terminals: dict | None = None
) -> bool:
    """Bind a new empty chat to the default profile; True when bound. Any failure
    leaves it local without failing the create; nothing starts.

    The check and the write run under the session's switch lock, the one turn
    admission holds while it registers a cold start. So a first message sent by
    another tab before this lands either finds the default already chosen, or
    has registered its session first, in which case the chat stays local
    (``sessions`` reports it) rather than recording a choice it does not run on.

    The chat is published before this runs, so another tab can open a local
    shell for it first. Once bound, the chat's local shells in ``terminals``
    are retired, as an explicit switch does.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.llm_helpers_parts.model_fallback import slot_switch_session_lock

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
        async with slot_switch_session_lock(key):
            if _environment_locked(slot):
                return False
            choices = _choices()
            if await asyncio.to_thread(choices.get, key) is not None:
                return False
            # A local prewarm (another tab's project change schedules one) must not
            # serve this session: stop it, as the explicit switch does, before the
            # choice lands. One still starting is not yet visible to has_session.
            eager = getattr(slot, "_eager_spawn_task", None)
            if eager is not None and not eager.done():
                eager.cancel()
                await asyncio.gather(eager, return_exceptions=True)
            if sessions is not None and sessions.has_session(key):
                if eager is None:
                    # Registered by a first turn, not a prewarm: it stays local.
                    return False
                await sessions.remove(key)
            if getattr(slot, "_pending_reset_history_key", None) == key:
                slot._pending_reset_history_key = None
            await asyncio.to_thread(choices.set, key, default)
            if isinstance(terminals, dict):
                from kiro_crew.dashboard.handlers.terminal import retire_local_shells_of_chat

                await retire_local_shells_of_chat(terminals, slot.key)
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
        return slot_not_found()
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
    from kiro_crew.llm_helpers_parts.model_fallback import slot_switch_session_lock
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    state = request.app["state"]
    slot = state._slots.get(request.match_info["slot"])
    if slot is None:
        return slot_not_found()
    # Serialized like the project switch; a change arms the deferred reset so an
    # idle runtime does not keep running in the old environment.
    async with slot._lock:
        if effective_session_key(slot) != key:
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )
        # Then the per-session switch lock (the order the agent and model switches
        # use). Turn admission holds it across a cold start, which is when a remote
        # workspace is created, so an in-flight first start settles before the lock
        # check reads whether a workspace exists. A failed start releases it with no
        # workspace recorded, so the chat can still be switched.
        async with slot_switch_session_lock(key):
            choices = _choices()
            try:
                previous = await asyncio.to_thread(choices.get, key)
                if previous != profile and await _choice_locked(slot, key, previous):
                    # Fixed for the session's life: its files and history live there.
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
                # Prewarm only a local session: a remote one would start billed compute.
                if profile is None:
                    # The deferred reset retires a runtime still on the old machine.
                    slot._pending_reset_history_key = key
                    schedule_eager_spawn(state, slot)
                else:
                    # A local prewarm in flight or registered must not serve this session.
                    eager = getattr(slot, "_eager_spawn_task", None)
                    if eager is not None and not eager.done():
                        eager.cancel()
                        await asyncio.gather(eager, return_exceptions=True)
                    sessions = getattr(state, "sessions", None)
                    if sessions is not None and sessions.has_session(key):
                        await sessions.remove(key)
                    # Removed here, so no reset is owed for this key: an armed one
                    # would tear the remote runtime down after its first turn and
                    # make the second message cold-start the workspace again.
                    if getattr(slot, "_pending_reset_history_key", None) == key:
                        slot._pending_reset_history_key = None
                    # A shell already open on this machine for the chat is not
                    # re-checked per keystroke: retire it, so typing meant for
                    # the workspace cannot run here from a tab with a stale read.
                    from kiro_crew.dashboard.handlers.terminal import retire_local_shells_of_chat

                    registry = getattr(state, "_terminal_sessions", None)
                    if isinstance(registry, dict):
                        await retire_local_shells_of_chat(registry, request.match_info["slot"])
    return web.json_response({"profile": profile, "applies": "next_start"})
