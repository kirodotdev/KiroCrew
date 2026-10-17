"""A Crew Member's own workspace, allocated with its record.

A member's workspace is its own directory under the data home: the files it
keeps, its ``HEARTBEAT.md``, its workspace-scoped skills and steering. The crew
record binds it BY NAME (``agents.<member>.workspace`` is a key of the
``workspaces`` table), so two members naming the same workspace share one tree.

A member created without naming a workspace gets one of its own, the way it gets
its own member memory (:func:`kiro_crew.memory_stores.provision_member_memory`).
Naming a workspace is the explicit opt-in to sharing and is stored as given, the
way every create path always stored it (an undeclared name resolves to the base
directory at use, ``config.loader.workspace_dir_for``). Members created before
this rule keep the binding they have; nothing here rebinds a stored record.

The split mirrors the memory path. :func:`provision_member_workspace` touches only
the caller's config SNAPSHOT: it picks a name and a directory and records both
there. :func:`publish_member_workspace` runs INSIDE ``persist_member_config``'s
locked mutation: it re-checks the name and directory against the document as
read under the lock, makes the directory, and writes the entry, so the member
record and its workspace land in one config write or not at all. A creation that
fails afterwards calls :func:`retire_unpublished_workspace` so the snapshot is
clean for a retry.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import asdict
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.loader import (
    WorkspaceConfig,
    WorkspaceDirUnusable,
    coerce_dict_section,
    materialize_workspace_dir,
    workspace_dir_from_entry,
)
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: The request value (``workspace`` on ``POST /api/agents``, ``--workspace`` on
#: ``kirocrew agent create``) that asks for a workspace of the member's own. It is
#: the in-memory binding between record construction and provisioning and is
#: never persisted: provisioning replaces it before the record is published.
AUTO_WORKSPACE = ""

#: The name every install has whether or not ``workspaces`` declares it: an
#: unmapped ``default`` resolves to the base workspace directory
#: (``config.loader.workspace_dir_for``), so naming it is always a valid reuse.
DEFAULT_WORKSPACE = "default"

#: A member's own workspace is named ``member-<slug>`` after the member, so the
#: Workspaces list reads without a lookup; the directory follows the dashboard's
#: own ``workspace-<name>`` convention for a created workspace.
MEMBER_WORKSPACE_PREFIX = "member-"
WORKSPACE_DIR_PREFIX = "workspace-"

# The member slug may run to 80 characters; the workspace name grammar
# (``validation.WORKSPACE_NAME_RE``) allows 64, and a numeric suffix must still fit.
_SLUG_LIMIT = 40
# Same-slug members are rare (slugification is lossy, so distinct names can
# collide); the suffix walk is bounded so a corrupt table cannot spin it.
_SUFFIX_LIMIT = 1000


class MemberWorkspaceUnavailable(Exception):
    """A member's workspace could not be allocated or published.

    ``code`` is the stable token the writers map onto their own error contract
    (the dashboard's 409 body ``code``; the CLI's exit-1 message).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def member_workspace_name(member: str, taken: Collection[str]) -> str:
    """The workspace name a member of this name gets, skipping names in *taken*."""
    from kiro_crew.members import slug_for_name

    base = f"{MEMBER_WORKSPACE_PREFIX}{slug_for_name(member)[:_SLUG_LIMIT]}".rstrip("-")
    candidate = base
    suffix = 2
    while candidate in taken:
        if suffix > _SUFFIX_LIMIT:
            raise MemberWorkspaceUnavailable(
                "workspace_name_exhausted",
                f"no free workspace name for Crew Member {member!r}; name its workspace explicitly",
            )
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def workspace_dir_name(workspace: str) -> str:
    """The data-home-relative directory a member workspace of this name lives in."""
    return f"{WORKSPACE_DIR_PREFIX}{workspace}"


def _resolved_dirs(entries: Collection[WorkspaceConfig]) -> set[Path]:
    """The directories the entries name, resolved; an entry that cannot be resolved is skipped.

    A hand-written ``dir`` can stop resolving after the fact (``~otheruser/…``
    whose account is gone raises ``RuntimeError`` from ``expanduser``; a path the
    host refuses to stat raises ``OSError``). Such an entry cannot name the fresh
    data-home-relative directory this module allocates, so it cannot collide with
    it; skipping it keeps one stale workspace from refusing every later create.
    """
    resolved: set[Path] = set()
    for entry in entries:
        try:
            resolved.add(workspace_dir_from_entry(entry).resolve())
        except (RuntimeError, OSError):
            logger.debug("workspace dir %r does not resolve; skipped", entry.dir, exc_info=True)
    return resolved


def provision_member_workspace(config, member: str) -> str:
    """Bind *member* to a workspace of its own in *config*; return the new name.

    Acts only when the member's record carries :data:`AUTO_WORKSPACE`. A record
    naming a workspace is an explicit reuse and is left exactly as given;
    ``""`` is returned so the caller publishes nothing new. Nothing is written
    to disk here.
    """
    agent = config.agents.get(member)
    if agent is None:
        raise MemberWorkspaceUnavailable(
            "member_unknown", f"Crew Member {member!r} is not in the roster"
        )
    if agent.workspace != AUTO_WORKSPACE:
        return ""
    taken = set(config.workspaces) | {DEFAULT_WORKSPACE}
    name = member_workspace_name(member, taken)
    entry = WorkspaceConfig(dir=workspace_dir_name(name))
    # A fresh name whose directory another entry already points at (an absolute
    # ``dir`` spelled by hand) would silently share that tree; walk on instead.
    while workspace_dir_from_entry(entry).resolve() in _resolved_dirs(config.workspaces.values()):
        taken.add(name)
        name = member_workspace_name(member, taken)
        entry = WorkspaceConfig(dir=workspace_dir_name(name))
    agent.workspace = name
    config.workspaces[name] = entry
    return name


def publish_member_workspace(doc: dict, workspace: str, entry: WorkspaceConfig) -> None:
    """Write *workspace* into the raw config document and make its directory.

    For use inside a locked ``update_config_locked`` mutation only: the name and
    directory are re-checked against *doc* as read under the lock, because the
    snapshot :func:`provision_member_workspace` chose them against can be stale by
    the time the write lands. A collision refuses the whole write rather than
    picking another name behind the caller's back.

    The directory is made through :func:`materialize_workspace_dir` and, by that
    function's rule, is not removed when the config write that follows fails: a
    concurrent create can already have adopted it. It is then made owner-only:
    everything a boot step creates under the data home is, and a member's files
    are no exception (``test/integration/test_owner_only_boot.py`` walks the home
    for anything else). The directory is a fresh name inside the data home, so
    tightening never touches a folder the owner pointed at by hand.
    """
    workspaces = coerce_dict_section(doc, "workspaces")
    if workspace in workspaces:
        raise MemberWorkspaceUnavailable(
            "workspace_exists", f"workspace {workspace!r} was created concurrently; retry"
        )
    # config_dir(), not data_home(): the former also creates the data home, and
    # materialize_workspace_dir refuses a missing parent. A create is not a hot
    # path, and the one place this can matter (a first create before anything
    # else wrote the home) is exactly where the directory has to be made.
    unresolved = config_dir() / entry.dir
    validated = unresolved.resolve()
    declared = [
        WorkspaceConfig(dir=str(raw.get("dir", "")))
        for raw in workspaces.values()
        if isinstance(raw, dict)
    ]
    if validated in _resolved_dirs(declared):
        raise MemberWorkspaceUnavailable(
            "workspace_dir_in_use",
            f"directory {entry.dir!r} is already used by another workspace; retry",
        )
    try:
        materialize_workspace_dir(validated, leaf=unresolved, display=entry.dir)
        platform_compat.restrict_dir_to_owner(validated)
    except WorkspaceDirUnusable as exc:
        raise MemberWorkspaceUnavailable(exc.code, str(exc)) from exc
    except OSError as exc:
        raise MemberWorkspaceUnavailable(
            "workspace_dir_uncreatable",
            f"directory {entry.dir!r} could not be made owner-only: {exc.strerror or exc}",
        ) from exc
    workspaces[workspace] = asdict(entry)


def retire_unpublished_workspace(
    config, member: str, workspace: str, *, previous_workspace: str
) -> None:
    """Undo :func:`provision_member_workspace` on the snapshot after a failed create.

    In-memory only. The entry is dropped and the member's binding restored, so a
    retry from the same snapshot allocates fresh. A directory that
    :func:`publish_member_workspace` already made is left where it is, by the
    same rule as every other workspace create: the write it belonged to may have
    landed from another process, and deleting it would strand that entry.
    """
    if not workspace or workspace == previous_workspace:
        return
    config.workspaces.pop(workspace, None)
    agent = config.agents.get(member)
    if agent is not None and agent.workspace == workspace:
        agent.workspace = previous_workspace
    logger.debug("retired unpublished workspace %r of Crew Member %r", workspace, member)
