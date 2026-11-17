"""The gate an app-launched chat create passes: ``POST /api/chat/slots`` with ``app_launch``.

An app's page opens a person's chat through ``useChatLauncher().createChat`` (or, for a
built-in page, the dashboard client's ``createAppChatSlot``), which sends the create
with ``app_launch: <app>``. The page needs no ``/api/chat/slots`` entry in
``permissions.api`` for it, so this module holds the create to what the app declares
in ``permissions.chatLaunch`` (``apps/chat_launch.py``) before the create handler
reads anything else:

- only a person's request; an app token never launches (its own slots are
  ``ctx.chat_slots``);
- the app is installed, enabled and declares ``chatLaunch``, and no other installed
  app, enabled or not, declares a key prefix that overlaps its own, so a chat under
  one app's prefix is never another's to reopen, retitle or refile;
- the body carries only the launch fields, so no project, mode, model, artifact,
  work item or agent namespace rides along;
- the key is the declared prefix plus a key id, unchanged by key normalization, and
  not one the core reserves (:func:`slot_ownership.app_reserved_key_reason`);
- the agent and memory mode are declared ones, and a folder only one whose name is
  the declared folder;
- the key holds no app-owned or non-chat slot, no other live slot has the key or its
  transcript up to letter case, and its saved transcript records no owning app and no
  other app's launch.

What passes is the ordinary create, unchanged: the chat is the person's, with the
person's approval rules. A refusal is one uniform 403 whatever the reason; the
reason goes to the security-event log under the app's name.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.apps.chat_launch import ChatLaunch

#: The body key that marks a create as an app launch, naming the app.
APP_LAUNCH_FIELD = "app_launch"
#: The SEL operation every refusal is recorded under.
_OPERATION = "chat_launch"
#: The fields an app launch may carry. ``embed_app`` names the app on the
#: conversation the create begins, as the scoped client does for a page's create.
_LAUNCH_FIELDS = frozenset(
    {"name", "agent", "memory_mode", "title", "folder_id", APP_LAUNCH_FIELD, "embed_app"}
)


def _refused(app: str, resource: str, reason: str) -> web.Response:
    from kiro_crew.dashboard.handlers.sessions import _audit_app_denial

    _audit_app_denial(app or "-", _OPERATION, resource, reason)
    return web.json_response(
        {"error": "this app may not open that chat", "code": "app_chat_launch_refused"},
        status=403,
    )


def _live_slot_reason(state: Any, key: str) -> str:
    """Why the live slots leave *key* not one a launch may open, or ``""``.

    The slot under exactly *key* must be the person's own chat. Any other live slot
    whose key or transcript is *key*'s up to letter case refuses too: on a
    case-insensitive filesystem the two would write one transcript.
    """
    from kiro_crew.dashboard.chat_utils_slots import _history_key_for
    from kiro_crew.dashboard.slot_ownership import live_case_alias_reason

    slot = state._slots.get(key)
    if slot is not None:
        if getattr(slot, "_app", ""):
            return "the key holds an app-owned slot"
        if getattr(slot, "mode", ""):
            return "the key holds a non-chat slot"
    return live_case_alias_reason(state, key, _history_key_for(key))


def live_slot_refusal(body: dict[str, Any], state: Any, key: str) -> web.Response | None:
    """The refusal for a launch whose key a slot took during the create's awaits, else ``None``.

    Synchronous, so the create handler runs it with no await before the mint.
    """
    app = body.get(APP_LAUNCH_FIELD)
    if not isinstance(app, str):
        return None
    reason = _live_slot_reason(state, key)
    return _refused(app, f"session={key}", reason) if reason else None


def _transcript_reason(log: Any, history_key: str, app: str) -> str:
    """Why the saved transcript under *history_key* is not one *app* may launch onto.

    Blocking file IO; call it off the event loop.
    """
    from kiro_crew.history_schema import EMBED_APP_META_KEY

    if not log.has_log(history_key):
        return ""
    meta, readable = log.get_metadata_status(history_key)
    if not readable:
        return "the saved transcript's record is unreadable"
    if meta.get("app"):
        return "the saved transcript belongs to an app"
    launched_by = meta.get(EMBED_APP_META_KEY)
    if launched_by and launched_by != app:
        return "the saved transcript was opened by another app"
    return ""


def _declaration(app: str) -> tuple[ChatLaunch | None, tuple[str, ...]]:
    """*app*'s declaration, and the other installed apps whose prefix overlaps it.

    Blocking file IO; call it off the event loop.
    """
    from kiro_crew.apps import chat_launch

    launch = chat_launch.declared_chat_launch(app)
    if launch is None:
        return None, ()
    return launch, chat_launch.overlapping_declarers(app, launch)


def _shape_reason(body: dict[str, Any], launch: ChatLaunch, state: Any) -> tuple[str, str]:
    """``(key, reason)``: the requested key, and why the body is outside *launch* or ``""``."""
    from kiro_crew.dashboard.chat_utils_slots import _history_key_for
    from kiro_crew.dashboard.slot_ownership import app_reserved_key_reason
    from kiro_crew.dashboard.state import _normalize_slot_key

    extra = sorted(set(body) - _LAUNCH_FIELDS)
    if extra:
        return "", f"undeclared create options: {', '.join(extra)}"
    name = body.get("name")
    if not isinstance(name, str) or _normalize_slot_key(name) != name:
        return "", "the key is missing or not in slot-key form"
    if not launch.covers_key(name):
        return name, "the key is outside the declared prefix"
    if reserved := app_reserved_key_reason(name, _history_key_for(name)):
        return name, reserved
    if body.get("agent") not in launch.agents:
        return name, "the agent is not declared"
    if body.get("memory_mode", "persistent") not in launch.memory_modes:
        return name, "the memory mode is not declared"
    folder_id = body.get("folder_id")
    if folder_id:
        folder = next((f for f in state._folders if f.get("id") == folder_id), None)
        if (
            not launch.folder
            or folder is None
            or str(folder.get("name", "")).strip() != launch.folder
        ):
            return name, "the folder is not the declared one"
    return name, _live_slot_reason(state, name)


async def app_launch_refusal(
    request: web.Request, state: Any, body: dict[str, Any]
) -> web.Response | None:
    """The 403 for an ``app_launch`` create outside the app's declaration, else ``None``."""
    from kiro_crew.apps.manifest import app_name_error
    from kiro_crew.dashboard.chat_utils_slots import _history_key_for

    app = body.get(APP_LAUNCH_FIELD)
    if not isinstance(app, str) or app_name_error(app):
        return _refused("", "app=?", "the launching app is not a valid name")
    if request.get("app"):
        return _refused(app, "app=" + app, "an app token cannot launch a person's chat")
    embed_app = body.get("embed_app")
    if embed_app is not None and embed_app != app:
        return _refused(app, "app=" + app, "embed_app names another app")
    try:
        launch, holders = await asyncio.to_thread(_declaration, app)
    except OSError:
        return _refused(app, "app=" + app, "the installed apps could not be read")
    if launch is None:
        return _refused(app, "app=" + app, "the app is not enabled or declares no chatLaunch")
    if holders:
        return _refused(
            app,
            "app=" + app,
            f"another installed app declares an overlapping key prefix: {', '.join(holders)}",
        )
    key, reason = _shape_reason(body, launch, state)
    if reason:
        return _refused(app, f"session={key or '?'}", reason)
    log = getattr(state, "conversation_log", None)
    if log is not None:
        reason = await asyncio.to_thread(_transcript_reason, log, _history_key_for(key), app)
        if reason:
            return _refused(app, f"session={key}", reason)
    return None


__all__ = ["APP_LAUNCH_FIELD", "app_launch_refusal", "live_slot_refusal"]
