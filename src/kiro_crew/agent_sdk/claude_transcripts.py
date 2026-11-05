"""Delete Claude Code's own record of one session.

claude-agent-acp runs the ``claude`` CLI, which keeps a transcript store of its
own that Crew never writes: ``<config dir>/projects/<project>/<sid>.jsonl``, a
sibling ``<sid>/`` directory, and ``<config dir>/file-history/<sid>/`` (the
pre-edit file snapshots). ``<project>`` is the session's working directory with
every character that is not an ASCII letter or digit replaced by ``-``, spelled
from the resolved path, because the CLI names it from its process cwd.

A disposable session (incognito or temporary, a pruned subagent run, a released
continuable conversation) deletes those three paths the way the kiro-cli replay
log is deleted. Nothing here follows a symlink: a linked ``projects``,
``file-history`` or project directory, or a linked leaf, deletes nothing.

It sits behind the agent-SDK boundary so the consumers outside it (the subagent
prune) reach Claude's store, and Claude's provider label, without importing the
ACP layer.
"""

from __future__ import annotations

import logging
import os
import re
import shutil

from kiro_crew.acp.claude_settings_sources import claude_config_dir
from kiro_crew.acp.types import PROVIDER_LABEL_CLAUDE
from kiro_crew.providers.cleanup import _is_safe_path

logger = logging.getLogger(__name__)

#: A Claude Code session id is a UUID; anything else is refused before a path is built.
_SESSION_ID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_PROJECT_NAME_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9]")
#: The CLI keeps a project name of up to this many characters as is. A longer one
#: is cut and given a hash suffix this module does not reproduce, so it is refused.
_PROJECT_NAME_MAX_CHARS = 200


def is_claude_provider_label(provider_type: str | None) -> bool:
    """Whether *provider_type* (a session's provider label) names Claude Code.

    A positive comparison against Claude's own label (harness-parity H5).
    """
    return bool(provider_type) and provider_type == PROVIDER_LABEL_CLAUDE


def claude_project_dir_name(cwd: str) -> str | None:
    """The ``projects/`` entry Claude Code files a session run in *cwd* under.

    ``None`` when *cwd* is empty or the name would be longer than the CLI keeps
    verbatim.
    """
    if not cwd:
        return None
    name = _PROJECT_NAME_UNSAFE_RE.sub("-", os.path.realpath(cwd))
    return name if len(name) <= _PROJECT_NAME_MAX_CHARS else None


def _reason(exc: OSError) -> str:
    """An OSError's reason without the filename its ``str()`` would carry."""
    return exc.strerror or type(exc).__name__


def delete_claude_session_files(session_id: str, cwd: str) -> bool:
    """Delete Claude Code's transcript files for *session_id* run in *cwd*.

    Returns whether the cleanup completed: ``True`` when every path is gone
    (including one that never existed), ``False`` when the id is not UUID-shaped,
    *cwd* is empty or unspellable, no config directory resolves, a path is a
    symlink or escapes its root, or a delete failed. A false answer deletes
    nothing it was not sure of, so a caller that retries (the subagent prune)
    keeps its retry record.
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return False
    project_name = claude_project_dir_name(cwd)
    root = claude_config_dir()
    if project_name is None or root is None:
        return False
    projects = root / "projects"
    project = projects / project_name
    history = root / "file-history"
    # Logged by a fixed label, never by path: a path carries the session id and
    # the user's working directory.
    directories = (
        (projects, "projects directory"),
        (project, "project directory"),
        (history, "file-history directory"),
    )
    targets = (
        (project / f"{session_id}.jsonl", project, "transcript"),
        (project / session_id, project, "session directory"),
        (history / session_id, history, "file-history entry"),
    )
    try:
        for directory, label in directories:
            if directory.is_symlink():
                logger.error("claude transcript cleanup: refusing a symlinked %s", label)
                return False
        for target, parent, label in targets:
            if target.is_symlink() or not _is_safe_path(target, parent):
                logger.error("claude transcript cleanup: refusing the %s", label)
                return False
    except OSError as exc:
        logger.warning("claude transcript cleanup: cannot inspect the store (%s)", _reason(exc))
        return False
    succeeded = True
    for target, _parent, label in targets:
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
        except OSError as exc:
            succeeded = False
            logger.warning(
                "claude transcript cleanup: failed to delete the %s (%s)", label, _reason(exc)
            )
    return succeeded


__all__ = [
    "claude_project_dir_name",
    "delete_claude_session_files",
    "is_claude_provider_label",
]
