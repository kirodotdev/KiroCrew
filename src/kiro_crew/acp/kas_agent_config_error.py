"""kiro-cli's report of an agent file it could not load.

KAS rescans the agents directories on every ``session/new`` and sends one
``_kiro/customAgent/config_error`` notification for each file it skips,
``{sessionId, path, reasonCode, error}``, before the ``session/new`` response.
The session it names is not registered yet, so the reader would count it as an
unroutable frame and the file and the reason would reach no log. The reader
hands it to :func:`report` instead, which logs one WARNING per distinct
(path, reasonCode, error) per gateway process, so a broken file is reported once
however many sessions start.
"""

from __future__ import annotations

import logging

from kiro_crew.acp._dispatch import _loggable_request_id

logger = logging.getLogger(__name__)

METHOD_KAS_CUSTOM_AGENT_CONFIG_ERROR = "_kiro/customAgent/config_error"

#: Distinct reports remembered for the once-per-process check. Every part of the
#: key is backend-controlled, so the set is bounded; a report past the cap is
#: handed back to the reader's dropped-frame count, and the first one says so.
_MAX_REPORTED = 64

# Process-wide, not per runtime: every runtime of one gateway reads the same
# agents directory, and a runtime starts for most sessions, so a per-runtime set
# would repeat the warning at nearly every session start.
_reported: set[tuple[str, str, str]] = set()
_cap_warned = False


def report(params: object) -> bool:
    """Log the agent file a ``config_error`` notification names, once per process.

    Each field passes through ``_loggable_request_id`` (redacted whole, then
    capped) before it is kept or logged, as every backend string the reader logs
    does. ``False`` when the report is past :data:`_MAX_REPORTED` and was not
    logged: the reader then counts the frame as a dropped one.
    """
    global _cap_warned
    fields = params if isinstance(params, dict) else {}
    key = (
        _loggable_request_id(fields.get("path")),
        _loggable_request_id(fields.get("reasonCode")),
        _loggable_request_id(fields.get("error")),
    )
    if key in _reported:
        return True
    if len(_reported) >= _MAX_REPORTED:
        if not _cap_warned:
            _cap_warned = True
            logger.warning(
                "kiro-cli reported more than %d distinct agent config errors in this "
                "gateway process; later ones are not logged, only counted as dropped "
                "frames (method %s)",
                _MAX_REPORTED,
                METHOD_KAS_CUSTOM_AGENT_CONFIG_ERROR,
            )
        return False
    _reported.add(key)
    logger.warning("kiro-cli could not load agent config %s (%s): %s. Fix or remove the file", *key)
    return True
