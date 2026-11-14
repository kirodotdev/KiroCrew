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
from collections.abc import Iterable

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


__all__ = ["hidden_modes", "may_hide_sessions", "resume_keeps_mode"]
