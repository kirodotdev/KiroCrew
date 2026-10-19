"""Shared state and helpers for sessions that run in a remote environment."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from kiro_crew.coder.session_launch import SessionEnvironmentChoices


class SessionEnvironmentUnavailable(RuntimeError):
    """A persisted or selected environment provider cannot serve a session."""


# -- start progress, so the chat can tell a slow start from a hang --

#: Ordered start stages: sign-in and lookup, compute, guest worker, local engine.
START_STAGES: tuple[str, ...] = ("checking", "provisioning", "connecting", "starting_agent")
_START_PROGRESS_MAX = 256
#: key -> (stage, entered at, began at, stages entered, reconnect)
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
    # Decided when the start begins: an already-attached session is reconnecting.
    reconnect = previous[4] if previous is not None else session_key in _remote_workspaces
    if previous is None and len(_start_progress) >= _START_PROGRESS_MAX:
        # Bounded against leaks; the oldest goes first.
        _start_progress.pop(next(iter(_start_progress)))
    _start_progress[session_key] = (stage, now, began, entered, reconnect)


def clear_start_stage(session_key: str | None) -> None:
    """The start finished (ready, failed or cancelled): stop reporting a stage."""
    if session_key:
        _start_progress.pop(session_key, None)


# -- live remote workspaces, for the per-message context (set at runtime start) --
_REMOTE_WORKSPACES_MAX = 512
_remote_workspaces: dict[str, tuple[str, str]] = {}
#: The runtime each entry belongs to (``id``), so only its own end forgets it.
_remote_workspace_owners: dict[str, int] = {}


def remember_remote_workspace(
    session_key: str | None, name: str, workdir: str, *, owner: object = None
) -> None:
    """Record that ``session_key``'s runtime (``owner``) works in ``workdir`` on ``name``.

    Each entry is forgotten when its runtime ends (:func:`forget_remote_workspace`
    with that owner), so live runtimes, at most the live-relay cap, never reach the
    bound that evicts the oldest entry.
    """
    if not session_key:
        return
    if session_key not in _remote_workspaces and len(_remote_workspaces) >= _REMOTE_WORKSPACES_MAX:
        oldest = next(iter(_remote_workspaces))
        _remote_workspaces.pop(oldest)
        _remote_workspace_owners.pop(oldest, None)
    _remote_workspaces[session_key] = (name, workdir)
    _remote_workspace_owners[session_key] = id(owner) if owner is not None else 0


def forget_remote_workspace(session_key: str | None, *, owner: object = None) -> None:
    """Forget ``session_key``'s entry; with ``owner``, only while that runtime holds it
    (a replacement runtime that already took the entry keeps it)."""
    if not session_key:
        return
    if owner is not None and _remote_workspace_owners.get(session_key) != id(owner):
        return
    _remote_workspaces.pop(session_key, None)
    _remote_workspace_owners.pop(session_key, None)


#: Sessions just given a new, empty workspace; the next context says so once.
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


# -- local-only tools: act on this machine, so left out of remote grants and refused --

#: ``kirocrew-core`` tools that act on the gateway machine itself.
LOCAL_ONLY_CORE_TOOLS: frozenset[str] = frozenset(
    {
        "set_project",  # retargets the slot at a directory on this machine
        "file_send",  # reads a file off this machine's disk
        "browser",  # drives this machine's browser panel
        "pod_up",  # boots a gateway from a worktree on this machine
        "pod_down",  # and deletes that pod's data directory here
        "task_run",  # reads its spec off this machine's disk and runs with local tools
        # Each ctx.agent() step is a fresh session here, not the caller's environment.
        "workflow_run",
        "workflow_rerun_subtree",
    }
)
#: Whole servers that act on the gateway machine: computer use drives its desktop,
#: and a cron job runs here (its command, script or fresh agent session).
LOCAL_ONLY_SERVERS: frozenset[str] = frozenset({"kirocrew-computer", "kirocrew-cron"})
#: The ``mcpServers`` name the local engine sees for the remote workspace, for
#: every provider; governance keys the workspace tools' policy on it.
WORKSPACE_SERVER_NAME = "workspace"
#: The ACP kind of the built-in each workspace tool stands in for.
WORKSPACE_TOOL_KINDS: dict[str, str] = {
    "read_file": "read",
    "list_directory": "read",
    "glob": "read",
    "grep": "read",
    "write_file": "edit",
    "edit_file": "edit",
    "run_command": "execute",
}
#: The built-in each workspace tool stands in for: a deny on it binds the tool too.
WORKSPACE_DENY_ALIASES: dict[str, tuple[str, ...]] = {
    "read_file": ("fs_read",),
    "list_directory": ("fs_read",),
    "glob": ("glob",),
    "grep": ("grep",),
    "write_file": ("fs_write",),
    "edit_file": ("fs_write",),
    "run_command": ("execute_bash",),
}


#: Why a remote-bound session cannot start on a backend other than KAS.
REMOTE_NEEDS_KAS = (
    "remote session environments need the KAS agent backend, which lets Kiro "
    "Crew remove the local file and shell tools; switch the backend in Settings"
)
REMOTE_AGENT_RESTRICTED = (
    "this agent's own rules limit shell commands or file paths, and a remote "
    "workspace cannot enforce them yet; remove those rules or run on This machine"
)


def choices_path() -> Path:
    """Where every session's environment choice is stored; the one spelling of it."""
    from kiro_crew.config.paths import data_home

    return data_home() / "coder" / "session-choices.json"


#: The repository's developer flag. Session Environments are a developer preview:
#: until a live Coder deployment has been tested, a gateway started without it runs
#: every session on this machine exactly as before.
DEVELOPER_FLAG = "KIROCREW_DEV_MODE"


def session_environments_enabled() -> bool:
    """Whether remote placement is reachable at all; off unless ``KIROCREW_DEV_MODE``.

    An enterprise build ignores the flag (RFC D3, ``kiro_crew.edition_locks``).
    """
    from kiro_crew.constants import env_flag_enabled
    from kiro_crew.edition_locks import local_enable_locked

    return env_flag_enabled(DEVELOPER_FLAG) and not local_enable_locked("developer_mode")


def _choices() -> SessionEnvironmentChoices:
    from kiro_crew.coder.session_launch import SessionEnvironmentChoices

    return SessionEnvironmentChoices(choices_path())


#: ``(session_key, runs remotely)`` a coroutine looked up off the loop before a sync
#: replay (:func:`known_placement`); read only for that same key.
_known_placement: ContextVar[tuple[str, bool] | None] = ContextVar(
    "session_environment_known_placement", default=None
)


@contextmanager
def known_placement(session_key: str, runs_remotely: bool) -> Iterator[None]:
    """Answer :func:`session_runs_remotely` for ``session_key`` from ``runs_remotely``.

    The gateway replays a directive tool on its event loop; reading the choices
    store there would block every other task, so the handler looks placement up in
    a thread first and the replay's guard reads that answer instead.
    """
    token = _known_placement.set((session_key, runs_remotely))
    try:
        yield
    finally:
        _known_placement.reset(token)


def session_runs_remotely(session_key: str | None) -> bool:
    """Whether the durable choice binds ``session_key`` remotely; unreadable is True."""
    if not session_key:
        return False
    known = _known_placement.get()
    if known is not None and known[0] == session_key:
        return known[1]
    try:
        return _choices().resolve(session_key) is not None
    except SessionEnvironmentUnavailable:
        return True


def session_attached_remotely(session_key: str | None) -> bool:
    """True only on a confirmed remote binding (attached here, or recorded); unreadable
    is False, never widening a local server named like the workspace."""
    if not session_key:
        return False
    if session_key in _remote_workspaces:
        return True
    try:
        return _choices().resolve(session_key) is not None
    except SessionEnvironmentUnavailable:
        return False


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
    """``{"stage", "stage_elapsed_s", "elapsed_s", "entered"}`` while a start is in
    flight; ``entered`` lists every stage so far, since one can finish between polls.
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
