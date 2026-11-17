"""Which installed apps keep their own chats out of the session lists.

A slot's ``mode`` is fixed when it is created, and the chat sidebar, the Sessions
page and the recently-closed list show only plain (``""``) slots. An installed app
that declares ``permissions.hiddenSessions: true`` may create a chat whose mode is
its own app id: from its backend (an app-token request) or from its page (the
owner's request naming the app as ``embed_app``). The Older-sessions list
(``GET /api/sessions?user_only=1``) leaves those chats out too while the app may
hide them.

The mode must equal the creating app's own id, so an app can hide only the chats it
creates, never another app's or the person's. An existing chat keeps its mode. The
chat is otherwise an ordinary chat: its approvals and questions reach the owner
through the same notices as every other chat, nothing is deleted, and a closed chat
whose app may not hide it right now (disabled, uninstalled, untrusted or not
declaring) is listed in Older sessions.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable

from kiro_crew.apps.manifest_names import app_name_error

logger = logging.getLogger(__name__)


def may_hide_sessions(app_id: object) -> bool:
    """True when *app_id* is an installed, enabled, trusted app declaring the flag.

    Blocking disk I/O: callers on the event loop offload it.
    """
    if not isinstance(app_id, str) or not app_id or app_name_error(app_id):
        return False
    # At call time: these modules import a great deal.
    from kiro_crew.apps.execution import third_party_ceiling_closed
    from kiro_crew.apps.manager import get_app_manifest, is_app_enabled
    from kiro_crew.dashboard.slot_persistence.metadata_values import _RETIRED_MODES
    from kiro_crew.members import DM_SLOT_MODE

    # A mode core gives a meaning (a Bot's DM thread, a retired chat mode) is never an
    # app's, even for an app that happens to share its name.
    if app_id == DM_SLOT_MODE or app_id in _RETIRED_MODES:
        return False

    try:
        if not is_app_enabled(app_id):
            return False
        manifest = get_app_manifest(app_id)
        if manifest is None or not manifest.permissions.hiddenSessions:
            return False
        return third_party_ceiling_closed(app_id) is None
    except Exception:  # noqa: BLE001 - an unreadable app hides nothing
        logger.warning("App %s: hiddenSessions check failed", app_id, exc_info=True)
        return False


def hidden_modes(modes: Iterable[object]) -> frozenset[str]:
    """The entries of *modes* that name an app which may hide its sessions now."""
    distinct = {m for m in modes if isinstance(m, str) and m}
    return frozenset(m for m in distinct if may_hide_sessions(m))


def resume_keeps_mode(mode: object) -> bool:
    """Whether a chat resumed from history keeps *mode*.

    A built-in's worker mode, or the mode of an app that may hide its chats now. Any
    other mode (an app since disabled, uninstalled or untrusted) comes back as a
    plain chat, so the chat Older sessions lists again opens on the chat page.

    Blocking disk I/O: the resume calls it off the loop and hands the answer to
    ``metadata_codec.Resume.keeps_mode``.
    """
    from kiro_crew.apps import seams

    if not isinstance(mode, str) or not mode:
        return True
    return mode in seams.worker_slot_modes() or may_hide_sessions(mode)


# The open-slot listing runs on every slots frame, on the event loop, so it reads only
# the set published here and never the disk. The set is computed in a worker thread
# at startup and after each app lifecycle or trust change (``request_hiding_refresh``).
# Until the first set is published (startup), every mode mirrors as it did before.
_hiding_apps: frozenset[str] | None = None
# Every app id this process has seen installed. A mode outside it is no app's (a core
# or future mode) and mirrors as before. Kept after an uninstall, so the uninstalled
# app's open chats are listed. shortcut: an app uninstalled before this process
# started is not in it, so its restored open chat stays unlisted until closed (Older
# sessions lists it); persist the hidden-chat marker on the slot if that matters.
_app_ids: frozenset[str] = frozenset()
_refresh_lock = threading.Lock()
# Its own lock: a writer asks while it may hold an app record lock that a running
# refresh is waiting on, so asking must never wait on the refresh.
_queued_lock = threading.Lock()
_refresh_queued = False
#: Bound by the serving dashboard: run a refresh off its loop.
_schedule_refresh: Callable[[], None] | None = None
#: Bound by the serving dashboard: resend the slots frame on its loop.
_listing_changed: Callable[[], None] | None = None


def bind_listing(
    schedule_refresh: Callable[[], None] | None, listing_changed: Callable[[], None] | None
) -> None:
    """Bind the dashboard's off-loop runner and its slots-frame resend."""
    global _schedule_refresh, _listing_changed, _refresh_queued
    _schedule_refresh = schedule_refresh
    _listing_changed = listing_changed
    # A refresh queued on an earlier loop may never run; it must not hold this one off.
    with _queued_lock:
        _refresh_queued = False


def _installed_app_names() -> list[str]:
    from kiro_crew.apps.manager import apps_dir

    root = apps_dir()
    if not root.is_dir():
        return []
    return [entry.name for entry in root.iterdir() if entry.is_dir()]


def refresh_hiding_apps() -> None:
    """Recompute and publish the apps that may hide their chats now.

    Blocking disk I/O: runs in a worker thread. When the set changes, the slots frame
    is sent again, so every browser lists or hides those chats without a reload.
    """
    global _hiding_apps, _app_ids, _refresh_queued
    with _queued_lock:
        _refresh_queued = False
    with _refresh_lock:
        try:
            names = [n for n in _installed_app_names() if not app_name_error(n)]
            fresh = hidden_modes(names)
        except Exception:  # noqa: BLE001 - an unreadable apps tree hides nothing
            logger.warning("could not list the apps that hide their chats", exc_info=True)
            names, fresh = [], frozenset()
        known = _app_ids | frozenset(names)
        changed = fresh != _hiding_apps or known != _app_ids
        _hiding_apps, _app_ids = fresh, known
    callback = _listing_changed
    if changed and callback is not None:
        try:
            callback()
        except Exception:  # noqa: BLE001 - the set is published either way
            logger.warning("could not refresh the session lists", exc_info=True)


def request_hiding_refresh() -> None:
    """Ask for a refresh after an app's enabled, installed, manifest or trust state changed.

    Safe from any thread. Bursts coalesce into one queued refresh. A process with no
    serving dashboard lists no chats, so it has nothing to refresh.
    """
    global _refresh_queued
    schedule = _schedule_refresh
    if schedule is None:
        return
    with _queued_lock:
        if _refresh_queued:
            return
        _refresh_queued = True
    try:
        schedule()
    except Exception:  # noqa: BLE001 - the state change has committed
        with _queued_lock:
            _refresh_queued = False
        logger.warning("could not schedule the hidden-chat refresh", exc_info=True)


#: The execution-trust settings ``third_party_ceiling_closed`` reads.
_TRUST_CONFIG_KEYS = (
    "agent.apps_allow_third_party",
    "agent.apps_trusted",
    "agent.apps_trusted_local",
    "agent.apps_trusted_repositories",
)
_trust_config_sub: object | None = None


def watch_trust_config() -> None:
    """Refresh on any live edit of a trust setting (the CLI, a hand edit), once per process."""
    global _trust_config_sub
    if _trust_config_sub is not None:
        return
    from kiro_crew.config import live

    _trust_config_sub = live.subscribe(
        *_TRUST_CONFIG_KEYS,
        callback=lambda _change: request_hiding_refresh(),
        name="hidden-sessions-trust",
    )


def listed_surface(mode: object) -> str:
    """The surface the session lists read for an open slot with *mode*.

    The same rule as a resume (``resume_keeps_mode``): an app's mode stays only
    while that app may hide its chats. Once it is disabled, uninstalled, untrusted
    or stops declaring, its open chat lists as a plain one (``""``), so it shows in
    the sidebar and on /chat again. Any mode that names no app (a core mode, a
    built-in's worker mode, any other mode) is returned unchanged. Reads only the
    published sets, never the disk; an app whose answer is not known, or failed, is
    shown rather than hidden.
    """
    if not isinstance(mode, str) or not mode or app_name_error(mode):
        return mode if isinstance(mode, str) else ""
    try:
        from kiro_crew.apps import seams
        from kiro_crew.dashboard.slot_persistence.metadata_values import _RETIRED_MODES
        from kiro_crew.members import DM_SLOT_MODE

        if mode == DM_SLOT_MODE or mode in _RETIRED_MODES or mode in seams.worker_slot_modes():
            return mode
        if mode not in _app_ids:
            return mode
        hiding = _hiding_apps
        return mode if hiding is not None and mode in hiding else ""
    except Exception:  # noqa: BLE001 - an undecidable chat is shown, never hidden
        logger.warning("App %s: hidden-chat listing check failed", mode, exc_info=True)
        return ""


__all__ = [
    "bind_listing",
    "hidden_modes",
    "listed_surface",
    "may_hide_sessions",
    "refresh_hiding_apps",
    "request_hiding_refresh",
    "resume_keeps_mode",
    "watch_trust_config",
]
