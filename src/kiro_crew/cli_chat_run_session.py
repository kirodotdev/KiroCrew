"""The session key a ``kirocrew chat`` run presents, and the record that binds it.

A plain run, and a crewmate on the Global store, present the fixed
:data:`CLI_SESSION_KEY`. A Bot with its own named store runs under
``cli_chat:<id>``, a fresh key per run, so two terminal chats never share one
binding. :func:`bind_run_session` records that key as the Bot's execution just
before the backend starts, so the Bot's memory and lesson tools read and write its
store, and :func:`release_run_session` deletes the record when the run ends.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from kiro_crew.memory_stores import named_store_or_empty

if TYPE_CHECKING:
    from kiro_crew.config import KiroCrewConfig

logger = logging.getLogger(__name__)

#: Session key a plain run presents everywhere it is identified: to the provider
#: factory, to the PreToolUse gate, and to the SEL audit log. It is the value
#: ``sel._infer_source`` and ``validation`` already recognise as the CLI, so the
#: three must not drift apart; each also recognises a Bot run's ``cli_chat:<id>``.
CLI_SESSION_KEY = "cli_chat"


def run_session_key(crew_agent: str | None, memory_store: str) -> str:
    """The session key this run presents: a key of its own for a Bot with a named store."""
    if not crew_agent or not named_store_or_empty(memory_store):
        return CLI_SESSION_KEY
    return f"{CLI_SESSION_KEY}:{uuid.uuid4().hex}"


def bind_run_session(cfg: KiroCrewConfig, crew_agent: str, session_key: str) -> None:
    """Record *session_key* as *crew_agent*'s execution, before the backend starts.

    Raises the execution layer's ``ValueError`` when the Bot cannot be bound: the
    run must then not start. Blocking: run it off the event loop.
    """
    from kiro_crew.execution_context import bind_session_execution, resolve_member_execution

    # Not vouched: a terminal chat is never the caller slot of an own-store dispatch.
    bind_session_execution(session_key, resolve_member_execution(cfg, crew_agent))


def release_run_session(session_key: str) -> None:
    """Delete the run's record once its backend is down. Blocking; never raises.

    The memory routes recognize a ``cli_chat:<id>`` key by its record alone, since
    the run's process is not the gateway's, so the record is the key's whole
    authority and must not outlive the run.

    shortcut: a run killed before its teardown leaves its record; a sweep of
    unreferenced run keys is the upgrade if that residue ever matters.
    """
    from kiro_crew.history import ConversationLog

    try:
        ConversationLog().delete_session(session_key)
    except Exception:  # noqa: BLE001 - a leftover record must not replace the chat's outcome
        logger.warning("The record of terminal chat %s was not removed", session_key, exc_info=True)
