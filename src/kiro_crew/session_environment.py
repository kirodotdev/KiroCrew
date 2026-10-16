"""Shared state and helpers for sessions that run in a remote environment."""

from __future__ import annotations

import time


class SessionEnvironmentUnavailable(RuntimeError):
    """A persisted or selected environment provider cannot serve a session."""


# -- start progress ------------------------------------------------------------------
#
# A remote session's first start can take minutes (create + build a workspace), and
# without a signal the chat cannot tell a slow start from a hang. Providers report the
# stage they are in; the dashboard reads it while the turn waits. In-memory and
# per-gateway: it describes a start in flight in THIS process and means nothing
# after a restart.

#: Ordered start stages. ``checking``: sign-in and finding the session's resource.
#: ``provisioning``: creating or starting compute. ``connecting``: installing and
#: reaching the guest worker. ``starting_agent``: the local engine's own startup.
START_STAGES: tuple[str, ...] = ("checking", "provisioning", "connecting", "starting_agent")
_START_PROGRESS_MAX = 256
#: session key -> (current stage, entered at, start began at, stages entered so
#: far, whether this start reconnects a session already attached here)
_start_progress: dict[str, tuple[str, float, float, tuple[str, ...], bool]] = {}


def report_start_stage(session_key: str | None, stage: str) -> None:
    """Record that ``session_key``'s environment start entered ``stage``."""
    if not session_key or stage not in START_STAGES:
        return
    now = time.monotonic()
    previous = _start_progress.get(session_key)
    began = previous[2] if previous is not None else now
    entered = previous[3] if previous is not None else ()
    if stage not in entered:
        entered = (*entered, stage)
    # Decided once, when the start begins: a session this gateway already
    # attached to a remote workspace is reconnecting (an engine restart), and
    # the dashboard shows that as an ordinary turn, not as setup.
    reconnect = previous[4] if previous is not None else session_key in _remote_workspaces
    if previous is None and len(_start_progress) >= _START_PROGRESS_MAX:
        # A leaked entry must never grow without bound; the oldest goes first.
        _start_progress.pop(next(iter(_start_progress)))
    _start_progress[session_key] = (stage, now, began, entered, reconnect)


def clear_start_stage(session_key: str | None) -> None:
    """The start finished (ready, failed or cancelled): stop reporting a stage."""
    if session_key:
        _start_progress.pop(session_key, None)


# -- live remote workspaces -----------------------------------------------------------
#
# Which sessions' runtimes are CURRENTLY attached to a remote workspace, so the
# per-message context can tell the model where its files live (the [PROJECT]
# line otherwise names the gateway's directory). Set and cleared by the provider
# at every runtime start, so a session switched back to local stops reporting.
_REMOTE_WORKSPACES_MAX = 512
_remote_workspaces: dict[str, tuple[str, str]] = {}


def remember_remote_workspace(session_key: str | None, name: str, workdir: str) -> None:
    """Record that ``session_key``'s runtime works in ``workdir`` on ``name``."""
    if not session_key:
        return
    if session_key not in _remote_workspaces and len(_remote_workspaces) >= _REMOTE_WORKSPACES_MAX:
        _remote_workspaces.pop(next(iter(_remote_workspaces)))
    _remote_workspaces[session_key] = (name, workdir)


def forget_remote_workspace(session_key: str | None) -> None:
    if session_key:
        _remote_workspaces.pop(session_key, None)


#: Sessions whose conversation outlived its workspace: the runtime that just
#: started got a NEW, empty resource (the old one was deleted, stopped with its
#: files, or retired). The next context build says so once, so the model does
#: not reason about files from earlier in the conversation that are gone.
_recreated: set[str] = set()


def mark_workspace_recreated(session_key: str | None) -> None:
    if session_key:
        if len(_recreated) >= _REMOTE_WORKSPACES_MAX:
            _recreated.clear()  # a leak must stay bounded; losing a note is harmless
        _recreated.add(session_key)


def take_workspace_recreated(session_key: str | None) -> bool:
    """True once after :func:`mark_workspace_recreated`, then False."""
    if not session_key or session_key not in _recreated:
        return False
    _recreated.discard(session_key)
    return True


def remote_workspace_for(session_key: str | None) -> tuple[str, str] | None:
    """``(environment name, guest workdir)`` while the session runs remotely."""
    return _remote_workspaces.get(session_key or "")


# -- local-only tools -----------------------------------------------------------------
#
# A remote session's engine runs on the gateway, so every Crew tool runs here too.
# Most act on gateway state that IS the session's (memory, crons, subagents,
# notifications). These act on THIS machine's files, browser, desktop or worktrees
# instead, which for a remote session is the wrong machine. They are unmounted from
# a remote session's agent (``AcpRuntime._remote_workspace_agents``) and refused in
# band by their servers as a second fence (``refuse_local_only_tool``).

#: ``kirocrew-core`` tools that act on the gateway machine itself.
LOCAL_ONLY_CORE_TOOLS: frozenset[str] = frozenset(
    {
        "set_project",  # retargets the slot at a directory on this machine
        "file_send",  # reads a file off this machine's disk
        "browser",  # drives this machine's browser panel
        "pod_up",  # boots a gateway from a worktree on this machine
        "pod_down",  # and deletes that pod's data directory here
    }
)
#: Whole servers that act on the gateway machine: computer use drives its desktop.
LOCAL_ONLY_SERVERS: frozenset[str] = frozenset({"kirocrew-computer"})


def session_runs_remotely(session_key: str | None) -> bool:
    """True when ``session_key`` is bound to a remote environment.

    Read from the durable choice record, so it holds in any process (the MCP
    servers run outside the gateway) and before a runtime attaches. An
    unreadable record answers True: refusing a local-only tool in a session that
    turns out to be local is recoverable, acting on the wrong machine is not.
    """
    if not session_key:
        return False
    from kiro_crew.coder.session_launch import SessionEnvironmentChoices
    from kiro_crew.config.paths import data_home

    choices = SessionEnvironmentChoices(data_home() / "coder" / "session-choices.json")
    try:
        return choices.resolve(session_key) is not None
    except SessionEnvironmentUnavailable:
        return True


def refuse_local_only_tool(session_key: str | None, server: str, tool: str) -> str | None:
    """The refusal text when ``server``/``tool`` would act on the wrong machine."""
    local_only = server in LOCAL_ONLY_SERVERS or (
        server == "kirocrew-core" and tool in LOCAL_ONLY_CORE_TOOLS
    )
    if not local_only or not session_runs_remotely(session_key):
        return None
    return (
        f"{tool} is not available in this session: it runs in a remote workspace, and "
        f"{tool} would act on the gateway machine instead. Use the workspace tools for "
        "files and commands in the remote workspace."
    )


def start_progress(session_key: str | None) -> dict[str, object] | None:
    """``{"stage", "stage_elapsed_s", "elapsed_s", "entered"}`` while a start is in flight.

    ``entered`` lists every stage this start has been in, in order. A stage can
    finish between two dashboard polls, so the poll alone cannot tell a stage
    that ran from one that was skipped.
    """
    entry = _start_progress.get(session_key or "")
    if entry is None:
        return None
    stage, stage_since, began, entered, reconnect = entry
    now = time.monotonic()
    return {
        "stage": stage,
        "stage_elapsed_s": round(now - stage_since, 1),
        "elapsed_s": round(now - began, 1),
        "entered": list(entered),
        "reconnect": reconnect,
    }
