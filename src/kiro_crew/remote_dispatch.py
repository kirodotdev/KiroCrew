"""The one switch for remote dispatch: ``work_ledger.remote_dispatch``.

``docs/request-for-change/rfc-conductor-remote-dispatch.md`` builds in waves, and
every wave's new behaviour sits behind this config bool. It is read on every call
(no restart): the gateway reads it before acting on a ``crew=`` call, and the
dashboard MCP server reads it when it lists its tools. Off, nothing advertises
``crew`` and a call carrying it is refused with :data:`REMOTE_DISPATCH_DISABLED`.

This module imports only the config loader, so both processes can use it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The refusal code for a ``crew=`` call while the switch is off.
REMOTE_DISPATCH_DISABLED = "remote_dispatch_disabled"

#: The refusal text, naming the setting that turns the feature on.
REMOTE_DISPATCH_DISABLED_MESSAGE = (
    "remote dispatch is a preview and is off on this gateway "
    "(set work_ledger.remote_dispatch=true to use crew=)"
)


def remote_dispatch_enabled() -> bool:
    """Whether ``work_ledger.remote_dispatch`` is on. Blocking: it may read config.

    Fails closed: a config that cannot be loaded reads as off.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        return KiroCrewConfig.load().work_ledger.remote_dispatch is True
    except Exception:
        logger.debug("remote_dispatch: config unreadable; treating as off", exc_info=True)
        return False
