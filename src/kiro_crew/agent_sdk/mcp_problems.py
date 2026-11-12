"""The one line an operator reads when a session's MCP servers did not all start.

A headless turn (a webhook run, a task-runner step) runs in a session nobody is
watching, so a server that failed to start there is otherwise visible only as
missing tools. Each such surface names the unusable servers to its operator from
the session's own MCP report; this is the one reading they share.

It reaches the report through the provider contract
(``LLMProvider.mcp_session_report()`` -> ``SessionMcpReport.problem_summary``),
so it needs no ACP import.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Servers whose state a session's MCP report keeps by name, and so how many a
#: reader of it names: four times the largest set measured (64 of the user's
#: servers beside Kiro Crew's own), so a real session is described whole. THE
#: bound for that population. The report's lists and the ``[MCP SERVERS]``
#: block's still-starting row both read this one name, so they cannot drift.
MCP_STATUS_CAP = 256

#: How long an unattended run (webhook, cron) holds its first prompt for the
#: agent spec's MCP servers to report. The session's own init drain gives up a
#: second after the first server reports, which a slow ``node`` server misses;
#: nobody can reconnect it by hand in these runs, so they wait longer. Bounded,
#: so a server that never answers costs a run this much and no more.
UNATTENDED_MCP_WAIT_SECS = 30.0


def session_mcp_problems(provider: Any) -> str:
    """One line naming the MCP servers this session could not use, or ``""``.

    The line is for the operator (a run list, a delivered result, a dashboard
    notification), never for a prompt, so the sanitized failure reasons are kept.
    Silent for a clean report, for a provider that keeps none, and for a report
    that cannot be read: a notice is never worth failing a run over.
    """
    if provider is None:
        return ""
    try:
        report = provider.mcp_session_report()
        return report.problem_summary() if report is not None else ""
    except Exception:
        logger.debug("MCP session report unreadable", exc_info=True)
        return ""


def session_mcp_states(provider: Any) -> dict[str, tuple[str, ...]] | None:
    """The servers of *provider*'s session by state, for the turn and the wait tool.

    ``SessionMcpReport.not_ready`` (the servers the session cannot use right now),
    plus ``connected``: the names the report holds as connected. Names only,
    never a failure reason. ``connected`` is what lets a reader say a server has
    connected because the report says so, never because the server stopped being
    listed. ``None`` for no provider, a provider that keeps no report, and a
    report that cannot be read, so the caller says nothing.
    """
    if provider is None:
        return None
    try:
        report = provider.mcp_session_report()
        if report is None:
            return None
        states = dict(report.not_ready())
        unusable = {name for names in states.values() for name in names}
        ready = (report.payload() or {}).get("ready") or ()
        connected = tuple(n for n in ready if isinstance(n, str) and n not in unusable)
        if connected:
            states["connected"] = connected
        return states
    except Exception:
        logger.debug("MCP session report unreadable", exc_info=True)
        return None


class TurnMcpStatesReader:
    """The reader a gateway installs on its context builder for the ``[MCP SERVERS]`` block.

    *sessions* answers ``get_provider(session_key)``. Calling the reader reads a
    session's states (:func:`session_mcp_states`). :meth:`told` is called by the
    block with the servers it spells out as still starting and a mark only that
    block carries. When they include every server the session's start skipped
    and still waits on, a prompt that carries the mark goes out without waiting
    for them (``session_handle_mcp.hold_untold_first_turn``). A read alone
    records nothing, and a block that was built and never sent exempts no other
    prompt. :meth:`not_tracked` is how many servers the session's report does
    not track, which the block says once.
    """

    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions

    def __call__(self, session_key: str) -> dict[str, tuple[str, ...]] | None:
        return session_mcp_states(self._sessions.get_provider(session_key))

    def not_tracked(self, session_key: str) -> int:
        """How many servers *session_key*'s report left out at its bound; 0 if it cannot be read."""
        return _reports_left_out(self._sessions.get_provider(session_key))

    def told(self, session_key: str, mark: str, named: tuple[str, ...]) -> None:
        """Hand *session_key*'s session what the block marked *mark* spells out. Never raises."""
        try:
            provider = self._sessions.get_provider(session_key)
            report = provider.mcp_session_report() if provider is not None else None
            if report is not None:
                report.note_turn_told(mark, named)
        except Exception:
            logger.debug("MCP session report unreadable", exc_info=True)


#: How long one ``mcp_server_wait`` call waits for a server that is still
#: connecting. Sized from measured starts: the largest healthy set measured (64
#: servers) was fully connected about 35 s after its session started, and the
#: engine itself gives one server 60 s by default once it holds a connect slot.
#: So this is that budget plus the longest queue seen; past it a server is hung
#: or queued behind hung ones, and the turn goes on without it.
MCP_SERVER_WAIT_SECS = 90.0

#: Names an ``unknown`` answer lists so a misspelt server name can be corrected.
_STATE_NAMES_LISTED = 32


def mcp_server_state(provider: Any, name: str) -> dict[str, Any]:
    """What is true of the server *name* in *provider*'s session, for ``mcp_server_wait``.

    ``{"mcp_server_state": <state>}`` with the session report's own word
    (``connected``, ``connecting``, ``failed``, ``needs_sign_in``, ``disabled``,
    ``registry_dropped``) or ``unknown``. A name that differs from one of the
    session's servers only by case is read as that server when it can mean only
    one. ``unknown`` also carries ``mcp_server_names``: the servers the name could
    mean, else the names this session cannot use yet, so it can be corrected
    (the first few, with ``mcp_server_names_more`` counting the rest). When the
    report's bound kept server reports out, ``unknown`` says so with their count
    (``mcp_reports_left_out``): the server asked about may be one of them, which
    is a different thing from a server nothing reported. A provider that keeps no
    report, or one that cannot be read, answers ``unknown``: the caller then says
    it could not tell.
    """
    states = session_mcp_states(provider)
    if states is None:
        return {"mcp_server_state": "unknown"}
    by_name = {known: state for state, names in states.items() for known in names}
    alike = [known for known in by_name if known.casefold() == name.casefold()]
    # Two servers may differ only by case, so the exact name is read first and a
    # different spelling is taken for a server only when it can mean one.
    exact = name if name in by_name else alike[0] if len(alike) == 1 else None
    if exact is not None:
        return {"mcp_server_state": by_name[exact]}
    listed = alike or [known for known, state in by_name.items() if state != "connected"]
    answer: dict[str, Any] = {
        "mcp_server_state": "unknown",
        "mcp_server_names": listed[:_STATE_NAMES_LISTED],
    }
    if len(listed) > _STATE_NAMES_LISTED:
        answer["mcp_server_names_more"] = len(listed) - _STATE_NAMES_LISTED
    left_out = _reports_left_out(provider)
    if left_out:
        answer["mcp_reports_left_out"] = left_out
    return answer


def _reports_left_out(provider: Any) -> int:
    """How many server reports *provider*'s session report left out at its bound; 0 if unreadable."""
    try:
        left_out = getattr(provider.mcp_session_report(), "left_out", 0)
    except Exception:
        logger.debug("MCP session report unreadable", exc_info=True)
        return 0
    return left_out if isinstance(left_out, int) and left_out > 0 else 0


async def wait_for_declared_mcp(provider: Any, timeout: float) -> None:
    """Hold an unattended run's first prompt until the spec's servers report.

    Best-effort and bounded by *timeout*: a provider without the capability, or
    one whose wait fails, lets the run go ahead, and :func:`declared_mcp_missing`
    then says what it lacks.
    """
    if provider is None or timeout <= 0:
        return
    try:
        missing = await provider.wait_for_declared_mcp(timeout)
    except Exception:
        logger.debug("declared MCP wait failed", exc_info=True)
        return
    if missing:
        # The run prompts anyway; this line is when and how long it waited.
        logger.warning(
            "Unattended run prompting after a %gs wait without declared MCP server(s): %s",
            timeout,
            ", ".join(str(name) for name in missing),
        )


def _declared(provider: Any, reader: str) -> tuple[str, ...]:
    if provider is None:
        return ()
    try:
        report = provider.mcp_session_report()
        names = getattr(report, reader)() if report is not None else ()
        return tuple(name for name in names if isinstance(name, str) and name)
    except Exception:
        logger.debug("MCP session report unreadable", exc_info=True)
        return ()


def declared_mcp_missing(provider: Any) -> tuple[str, ...]:
    """The agent spec's servers this session cannot use, or ``()``.

    Read from the session's own report, so it covers a server that failed, one
    awaiting authorization and one that never reported: the run's outcome
    turns on it. Silent for a provider that keeps no report and for one that
    cannot be read.
    """
    return _declared(provider, "declared_not_ready")


def missing_mcp_notice(provider: Any) -> str:
    """The operator line naming declared servers that never reported, or ``""``.

    A failed or awaiting-authorization server is not repeated here:
    :func:`session_mcp_problems` already names it, with its reason.
    """
    names = _declared(provider, "declared_never_reported")
    if not names:
        return ""
    return "Declared MCP servers that never reported: " + ", ".join(names)


def declared_mcp_not_ready_note(provider: Any) -> str:
    """One line naming every declared server the run went without, or ``""``.

    For a surface that prints no :func:`session_mcp_problems` line beside it (a
    cron result), so a failed declared server is named here too.
    """
    names = declared_mcp_missing(provider)
    if not names:
        return ""
    return "MCP servers declared by the agent spec were not ready in this run: " + ", ".join(names)
