"""The ``connected_computers`` feature preview: default off, read live, off when unread.

Two machines read the same switch in their OWN config, and neither can turn the
other's on:

* the remote gateway (:func:`remote_enabled`) refuses enrollment, connector
  connects, the chat environment choice and session starts while it is off. It is
  also bounded by the broader Session Environments gate
  (:func:`kiro_crew.session_environment.session_environments_enabled`), which this
  feature keeps rather than loosens;
* the local gateway (:func:`local_enabled`) refuses consent, grants and connector
  starts while it is off. Turning it on is the local opt-in; it grants nothing by
  itself, since every grant still needs native consent.

Turning either off while work runs is handled by
:mod:`kiro_crew.connected_computers.disable`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final, Optional

from kiro_crew.session_environment import SessionEnvironmentUnavailable

logger = logging.getLogger(__name__)

#: The ``dashboard.feature_previews`` name, and the ``mc-preview-connected-computers``
#: key of ``website/src/utils/previewFlags.ts`` (the two lists are pinned equal).
PREVIEW_NAME: Final[str] = "connected_computers"

#: Shown for every refusal while the switch is off on the machine that refuses.
DISABLED_REASON: Final[str] = (
    "Connected computers is turned off on this gateway; turn it on in "
    "Settings > Developer > Feature Previews"
)
#: Shown by a remote gateway whose preview is on but which was started without
#: Session Environments' developer flag (``session_environment.DEVELOPER_FLAG``).
DEVELOPER_GATE_REASON: Final[str] = (
    "Connected computers needs Session Environments on this gateway: restart it "
    "with KIROCREW_DEV_MODE=1"
)


class ConnectedComputersDisabled(SessionEnvironmentUnavailable):
    """The feature preview is off (or unreadable) on the machine that refused."""

    code = "connected_computers_disabled"

    def __init__(self, message: str = DISABLED_REASON) -> None:
        super().__init__(message)


def _config(cfg: Any) -> Any:
    if cfg is not None:
        return cfg
    try:
        from kiro_crew.config import live

        snap = live.snapshot()
        if snap is not None:
            return snap
        # A CLI process (enrollment over SSH) has no watcher: read the file.
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load()
    except Exception:  # noqa: BLE001 -- unread means off
        logger.debug("connected computers: config unreadable; treating as off", exc_info=True)
        return None


def preview_on(cfg: Any = None) -> bool:
    """Whether ``dashboard.feature_previews.connected_computers`` is exactly ``True``."""
    previews = getattr(getattr(_config(cfg), "dashboard", None), "feature_previews", None)
    return isinstance(previews, Mapping) and previews.get(PREVIEW_NAME) is True


def remote_refusal(cfg: Any = None) -> Optional[str]:
    """Why this gateway may not serve connected computers now, or None when it may.

    The preview alone is not enough: Session Environments must be reachable too,
    and a gateway started without its developer flag says that, not "turn it on".
    """
    from kiro_crew.session_environment import session_environments_enabled

    if not preview_on(cfg):
        return DISABLED_REASON
    if not session_environments_enabled():
        return DEVELOPER_GATE_REASON
    return None


def remote_enabled(cfg: Any = None) -> bool:
    """Whether this gateway may serve connected computers to its chats."""
    return remote_refusal(cfg) is None


def local_switch(cfg: Any = None) -> Optional[bool]:
    """This machine's switch, or None when its configuration could not be read.

    :func:`local_enabled` reads an unreadable configuration as off, which is right
    for refusing NEW work; a caller that would revoke grants on Off uses this
    instead, so a transient read failure never stands in for the user's Off.
    """
    if cfg is None:
        try:
            from kiro_crew.config import live

            cfg = live.snapshot()
            if cfg is None:
                from kiro_crew.config.loader import KiroCrewConfig

                cfg = KiroCrewConfig.load()
        except Exception:  # noqa: BLE001 -- unknown, not off
            logger.debug("connected computers: config unreadable; switch unknown", exc_info=True)
            return None
    return preview_on(cfg)


def local_enabled(cfg: Any = None) -> bool:
    """Whether this machine's local controller may offer itself to a remote gateway."""
    return preview_on(cfg)


def require_remote(cfg: Any = None) -> None:
    """Raise :class:`ConnectedComputersDisabled` unless :func:`remote_enabled`."""
    reason = remote_refusal(cfg)
    if reason is not None:
        raise ConnectedComputersDisabled(reason)


def require_local(cfg: Any = None) -> None:
    """Raise :class:`ConnectedComputersDisabled` unless :func:`local_enabled`."""
    if not local_enabled(cfg):
        raise ConnectedComputersDisabled()
