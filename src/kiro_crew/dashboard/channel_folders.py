"""Per-channel session filing — route channel conversations into a folder.

Each messaging channel carries an optional ``session_folder`` config value
(``discord.session_folder``, ``slack.session_folder``, …). Empty — the default —
means the feature is off and a channel conversation stays unfiled, exactly as
before. Any other value is the name of the sidebar folder that channel's
sessions are filed into as they are surfaced by
:mod:`kiro_crew.dashboard.channel_slots`.

Design notes:

* **Name is the identity.** The folder is matched by its configured NAME, not
  by a stored id, so the same config always lands in the same folder and the
  user can rename the setting to point at a different one. A folder this module
  creates is stamped with ``channel: "<namespace>"``, which is what makes the
  sidebar draw the channel's brand mark on it instead of a generic folder glyph.
* **Adopt before create.** An existing folder with the configured name is
  reused rather than duplicated — including one the user made by hand, so
  "file Discord sessions in my existing Chats folder" works.
* **Creation happens when the setting is SAVED, never while surfacing.** The
  config endpoints call :func:`ensure_channel_folder` (a user-initiated write,
  in a handler that already writes config.json synchronously), and the
  reconciler only ever calls :func:`lookup_channel_folder`, which reads. Keeping
  the reconcile path write-free is deliberate: it is reached from the 30s timer
  AND from ``surface_dispatcher_session`` on every inbound channel message, so a
  write there both blocks the event loop on ``fsync`` and races a second pass.
  A configured folder that does not exist (config.json hand-edited, or the user
  deleted the folder) leaves conversations unfiled until the next settings save
  recreates it — the pre-feature behaviour, not a broken state.
* **Filing happens once, at surface time.** Only a conversation that does not
  already have a folder is filed, and only the first time its slot is created —
  a conversation with slot-side history metadata has been surfaced before, so
  its current placement (top level included) is the user's, not the setting's.
  A session the user later moves elsewhere is never re-filed, so the setting
  cannot fight a manual move, across restarts either.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from kiro_crew.config.loader import (
    SESSION_FOLDER_NAME_MAX,
    KiroCrewConfig,
    _coerce_session_folder,
)
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Channel session-key namespace -> the config section that owns its settings.
#: Namespaces absent from this map have no per-channel config to read and are
#: therefore always off: ``unified`` (the aggregated DM bucket, which spans
#: several channels and so has no single brand or owner) and ``whatsapp``
#: (no config section of its own).
CHANNEL_CONFIG_SECTIONS: dict[str, str] = {
    "slack": "slack",
    "discord": "discord",
    "telegram": "telegram",
    "webex": "webex",
    "wecom": "wecom",
    "teams": "teams",
    "weixin": "weixin",
    "imessage": "imessage",
    "whatsapp": "whatsapp",
}


#: Channel config fields the runtime re-reads live, so changing one alone does
#: NOT require a gateway restart (unlike the boot-read credential and allow-list
#: fields). ``session_folder`` is re-read on every reconcile pass, so a new value
#: applies to the next conversation surfaced.
LIVE_RELOAD_FIELDS = frozenset({"session_folder"})


def clean_session_folder(raw: object) -> str:
    """Validate a submitted ``session_folder`` value, returning the clean name.

    ``""`` means the feature is off for that channel (the default): sessions
    started there stay unfiled. Raises :class:`ValueError` with a user-facing
    message for anything that could not address a sidebar folder — the same
    rules :func:`kiro_crew.config.loader._coerce_session_folder` applies when
    reading a hand-edited config.json, kept here so every channel's save
    endpoint rejects the same values with the same wording.

    Messages name the field the way the UI labels it ("Folder name"), not the
    way config spells it: the dashboard renders them verbatim next to that
    input, where "session_folder" is vocabulary the user never saw.
    """
    if not isinstance(raw, str):
        raise ValueError("Folder name must be text")
    name = raw.strip()
    if len(name) > SESSION_FOLDER_NAME_MAX:
        raise ValueError(f"Folder name must be at most {SESSION_FOLDER_NAME_MAX} characters")
    if any(ch in name for ch in ("/", "\\")):
        raise ValueError("Folder name cannot contain / or \\")
    if any(ord(ch) < 0x20 for ch in name):
        raise ValueError("Folder name cannot contain line breaks or control characters")
    return name


def stored_folder_name(raw: object) -> str:
    """Return a usable folder name from a value read out of stored config.

    The save endpoints edit the raw ``config.json`` dict in place and re-serialize
    it, so they read ``session_folder`` back from that dict rather than from the
    loaded dataclass — which means they do NOT get
    :func:`~kiro_crew.config.loader._coerce_session_folder`'s sanitising. A
    hand-edited non-string (``"session_folder": 123``) would otherwise reach
    ``str()`` and create a folder literally named ``123`` on the next save of any
    unrelated field in that section.

    Delegates to the loader's coercion so stored values fail closed to "off" by
    exactly the same rules the config reader applies — one definition of what a
    stored folder name may be, rather than one per call site.
    """
    return _coerce_session_folder(raw)


def configured_folder_name(namespace: str) -> str:
    """Return the folder name configured for *namespace*, or ``""`` when off.

    Reads live config (``KiroCrewConfig.load()`` is fingerprint-cached), so
    toggling the setting in Settings takes effect without a gateway restart.
    Any failure to read config is treated as "off" — a channel conversation
    landing unfiled is the pre-feature behaviour, and never worse than filing
    it somewhere unintended.
    """
    section = CHANNEL_CONFIG_SECTIONS.get((namespace or "").lower())
    if not section:
        return ""
    try:
        cfg = KiroCrewConfig.load()
        return str(getattr(getattr(cfg, section), "session_folder", "") or "")
    except Exception:  # noqa: BLE001 — config read is best-effort
        logger.debug("channel folder: config read failed for %s", namespace, exc_info=True)
        return ""


def _find_folder(folders: list[dict], name: str, namespace: str) -> dict | None:
    """Find the folder *name* addresses, preferring this channel's own folder.

    Matching is case-insensitive so the name in config does not have to match
    the folder's capitalization. A folder already stamped for *namespace* wins
    over an unrelated same-named one, which keeps repeat passes stable when a
    user happens to have two folders whose names differ only in case.
    """
    target = name.strip().lower()
    if not target:
        return None
    fallback: dict | None = None
    for folder in folders:
        if str(folder.get("name", "")).strip().lower() != target:
            continue
        if str(folder.get("channel", "")).lower() == (namespace or "").lower():
            return folder
        if fallback is None:
            fallback = folder
    return fallback


async def lookup_channel_folder(state: "DashboardState", namespace: str) -> str:
    """Return the id of the folder channel *namespace* files its sessions into.

    ``""`` when the channel has no ``session_folder`` configured (the default),
    when config cannot be read, or when no folder with that name exists yet.

    **Read-only by contract, and read under the store lock.** This is the
    reconcile path, reached both from the 30s background pass and from
    ``surface_dispatcher_session`` — which every channel transport awaits on an
    inbound message — so two calls can be in flight at once. It performs no
    folder-store write: a write here would block the event loop on ``fsync``
    and, being non-atomic across an ``await``, could also drop a concurrent
    folder edit made from the dashboard. Creation belongs to
    :func:`ensure_channel_folder`, which the settings save calls.

    The lookup goes through
    :meth:`~kiro_crew.dashboard.state.DashboardState.read_folders` so it sees
    only committed state. An unlocked read could land mid-transaction and return
    the id of a folder whose write then fails and is rolled back — handing the
    session a ``folder_id`` that dangles, and (because slot-side folder metadata
    marks a session as already filed) one that no later save would correct.

    A folder the user hid is returned as normal. No unhide write is needed:
    ``folderIsHidden`` in the sidebar is ``hidden && !hasActiveSession``, so a
    hidden folder that receives a session shows up on its own.
    """
    # Off-loop: a config read, pure and touching no shared state.
    name = await asyncio.to_thread(configured_folder_name, namespace)
    if not name:
        return ""
    ns = (namespace or "").lower()

    def _find(folders: list[dict]) -> dict | None:
        return _find_folder(folders, name, ns)

    existing = await state.read_folders(_find)
    if existing is None:
        # Configured but absent — hand-edited config, or the folder was deleted.
        # Leave the conversation unfiled rather than writing from this path.
        logger.debug(
            "channel folder: %r not found for %s; leaving the session unfiled", name, namespace
        )
        return ""
    return str(existing.get("id", ""))


async def ensure_channel_folder(
    state: "DashboardState", namespace: str, name: str, *, relabel: bool = False
) -> str:
    """Create (or adopt) the folder *name* for channel *namespace*; return its id.

    Called from a channel's config save endpoint after the new
    ``session_folder`` value is committed, so the folder exists before any
    conversation needs filing. ``""`` when *name* is empty (the setting is off),
    or when the folder store could not be written.

    *relabel* must be true ONLY on a save that actually carried a
    ``session_folder`` value. These endpoints run on every section save — a
    token-only or allow-list save reaches here too — and renaming the channel's
    folder to the stored name on one of those would silently undo a rename the
    user made in the sidebar. With *relabel* false the folder is found and reused
    but never renamed, so an unrelated save cannot revert their choice.

    The find, the create and the persist all happen inside one
    :meth:`~kiro_crew.dashboard.state.DashboardState.mutate_folders`
    transaction. That is what makes it safe against a concurrent folder edit
    from the dashboard: the store lock is held across the whole read-modify-write
    (so two callers cannot each miss the other's folder and create a duplicate),
    and the ``fsync`` runs off the event loop.

    Idempotent: called again with the same name it adopts the existing folder and
    writes nothing.
    """
    name = (name or "").strip()
    if not name:
        return ""
    ns = (namespace or "").lower()
    created_id = ""

    def _create_or_adopt(folders: list[dict]) -> tuple[bool, str]:
        nonlocal created_id
        # This channel's OWN folder is identified by its stamp, not its name, so
        # a folder the user has since renamed in the sidebar is still recognised
        # as theirs and gets relabelled rather than duplicated. Without this, a
        # rename left config pointing at a name nothing answers to, and the next
        # settings save built a SECOND branded folder beside the renamed one.
        # The caller is a settings save, so the configured name is the newer
        # intent and wins as the label; the folder — and everything filed in it —
        # is preserved.
        stamped = next(
            (f for f in folders if str(f.get("channel", "")).lower() == ns), None
        )
        if stamped is not None:
            changed = False
            if relabel and str(stamped.get("name", "")).strip() != name:
                stamped["name"] = name
                changed = True
            # Re-engaging the folder also clears a `hidden` flag, matching the
            # folder CRUD handler's own unhide-on-assign rule.
            if stamped.get("hidden"):
                stamped["hidden"] = False
                changed = True
            return changed, str(stamped.get("id", ""))
        existing = _find_folder(folders, name, ns)
        if existing is not None:
            # An unstamped folder the user already had under this name. Adopt it
            # as-is: stamping someone else's folder with a brand mark would
            # rebrand a folder they created for their own purposes.
            was_hidden = bool(existing.get("hidden"))
            if was_hidden:
                existing["hidden"] = False
            return was_hidden, str(existing.get("id", ""))
        folder = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "order": len(folders),
            "collapsed": False,
            "hidden": False,
            "parent_id": "",
            "project_dir": "",
            # The brand mark IS this folder's icon, so no emoji is generated for
            # it (the LLM icon task folder creation normally kicks off is
            # skipped).
            "channel": ns,
        }
        created_id = str(folder["id"])
        folders.append(folder)
        return True, created_id

    try:
        folder_id = await state.mutate_folders(_create_or_adopt)
    except Exception:  # noqa: BLE001 — a failed folder save must not fail the config save
        # mutate_folders confirms the write and restores the in-memory list
        # before re-raising, both inside the store lock, so there is nothing to
        # undo here and no window in which the unpersisted folder was visible.
        logger.warning("channel folder: folder store write raised for %s", ns, exc_info=True)
        return ""
    if not folder_id:
        return ""

    if not created_id:
        return folder_id
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.channel_folder_create",
        outcome="allowed",
        source=ns,
        resources=created_id,
    )
    logger.info("Created %s session folder %r (%s)", ns, name, created_id)
    return created_id
