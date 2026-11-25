"""Why a KAS session cannot start on an agent: the reasons shown to the user.

Moved whole from :mod:`kiro_crew.acp.harness.kas`, which imports both names back,
so that module stays the import path its callers use.
"""

from __future__ import annotations

import logging

# The original module's logger: the log format prints the logger name.
logger = logging.getLogger("kiro_crew.acp.harness.kas")


def _kiro_cli_builtin_reason(agent: str) -> str:
    """Why kiro-cli's built-in *agent* does not start on KAS, naming what to pick instead.

    KAS has no definition of it, and running KAS's own closest mode would decide
    some tool calls without asking Crew. Reads config for the default template.
    """
    from kiro_crew.config.loader_snapshots import (
        DEFAULT_KIRO_TEMPLATE,
        KIRO_CLI_BUILTIN_AGENTS,
        default_template,
    )

    try:
        from kiro_crew.config import KiroCrewConfig

        default = default_template(KiroCrewConfig.load())
    except Exception:  # noqa: BLE001 - the name is a hint, never a gate
        logger.debug("no default template for the %r refusal", agent, exc_info=True)
        default = DEFAULT_KIRO_TEMPLATE
    if default in KIRO_CLI_BUILTIN_AGENTS:
        # A configured default that is itself refused here is no pick.
        default = DEFAULT_KIRO_TEMPLATE
    return (
        f'The agent "{agent}" is built into kiro-cli and has no safe match on KAS. '
        f'Pick Kiro Crew\'s default agent "{default}" instead, or run '
        f"`kiro-cli --agent {agent}` in this chat's Terminal tab."
    )


def _missing_agent_reason(agent: str, session_key: str) -> str:
    """The reason a session cannot start on *agent*, which has no spec, for the user.

    Names the Crew Member when *session_key* is one's DM thread, and the app the
    agent came from when an installed app declares it (``apps.manager``). Blocking
    file I/O, so it runs inside the projection's worker thread; it never raises,
    because the reason replaces an error that is already being raised.
    """
    from kiro_crew.apps.manager import missing_agent_reason

    member = ""
    try:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.members import member_identity_for_session

        identity = member_identity_for_session(session_key)
        if identity is not None:
            row = KiroCrewConfig.load().agents.get(identity["name"])
            member = (row.display_name if row is not None else "") or identity["name"]
    except Exception:  # noqa: BLE001 - the member name is a label, never a gate
        logger.debug("no member label for a missing agent %r", agent, exc_info=True)
    try:
        return missing_agent_reason(agent, member=member)
    except Exception:  # noqa: BLE001 - same: fall back to the plain reason
        logger.debug("no owner lookup for a missing agent %r", agent, exc_info=True)
        return f'The agent "{agent}" can\'t start: its agent definition is missing.'
