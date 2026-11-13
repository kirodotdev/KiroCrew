"""The work ledger's Feature Preview switch: ``work_ledger.remote_dispatch``.

One reader for every place that decides on it -- the ``work_report`` schema the MCP
server advertises and validates, the store's status check, and both wake readers
(``conductor_wake``'s push and the probe that feeds ``ledger_wake``'s gate) -- so they
cannot disagree about whether ``reply`` is a status.

Its own module rather than a function in ``work_vocab`` (a leaf with no imports) or in
the store (which neither wake reader may import for this). The config import is
function-local so importing this module costs the gateway's boot path nothing.
"""

from __future__ import annotations

import logging

from kiro_crew.work_vocab import WORK_REMOTE_DISPATCH_SETTING

logger = logging.getLogger(__name__)


def remote_dispatch() -> bool:
    """Whether the remote-dispatch Feature Preview is on. Default and any failure: off.

    Read per call, not at import: the config loader caches by file fingerprint, so a
    changed setting is seen without a restart. Fails closed, because off is exactly the
    behaviour from before the preview existed.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load().work_ledger.remote_dispatch is True
    except Exception as exc:  # noqa: BLE001 -- a preview switch is not worth failing a call over
        logger.debug("%s unreadable (%s); treating it as off", WORK_REMOTE_DISPATCH_SETTING, exc)
        return False
