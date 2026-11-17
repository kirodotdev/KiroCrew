"""How a best-effort background helper logs the failure it swallows.

Dashboard suggestions and tips run one background prompt and fall back to
nothing (or a catalog) when it fails. A backend failure there is an expected
state on a fresh install -- not signed in to Kiro, no engine to run on -- and
its ``AcpError`` text is already the formatted message for it, so the
gateway terminal gets that one line, not a traceback through the ACP client.
Anything else is a defect in Kiro Crew and keeps its traceback.
"""

from __future__ import annotations

import logging

#: Longest message a one-line warning carries; the rest is in the debug traceback.
_MAX_MESSAGE_CHARS = 500


def log_background_failure(logger: logging.Logger, what: str, exc: BaseException) -> None:
    """Log *exc*, which ended the background helper *what*, at warning level.

    An ``AcpError`` logs as ``"<what>: <message>"`` on one line, its traceback
    kept at debug level for whoever turns that on. Any other exception logs
    *what* with the full traceback.
    """
    # Function-local: the vocabulary loads the ACP package, which the callers'
    # importers do not otherwise need at import time.
    from kiro_crew.agent_sdk.drivers.acp_vocab import AcpError

    if not isinstance(exc, AcpError):
        logger.warning("%s", what, exc_info=exc)
        return
    message = " ".join(str(exc).split()) or type(exc).__name__
    if len(message) > _MAX_MESSAGE_CHARS:
        message = message[: _MAX_MESSAGE_CHARS - 1] + "…"
    logger.warning("%s: %s", what, message)
    logger.debug("%s: traceback", what, exc_info=exc)
