"""The directories a chat folder pins.

The project directory a folder declares and the steering directories it adds:
how each is validated against the sensitive-path, UNC and memory-silo fences when
it is written, and how each resolves for a slot's turn. New folder directory
rules go here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import pinned_fs
from kiro_crew.dashboard.folder_ownership import _folder_owner_app
from kiro_crew.folder_steering import crosses_memory_silo, memory_silo_fence
from kiro_crew.hooks import is_unc_shape, unc_probe_allowed, validate_file_path
from kiro_crew.sandbox import voice_runtime_workspace_conflict
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.dashboard.chat_folders")


def _validate_project_dir(raw: str) -> tuple[str, str | None]:
    """Validate and normalize project_dir. Returns (resolved_path, error_msg)."""
    if not raw:
        return "", None
    if not os.path.isabs(raw) and not raw.startswith("~"):
        return "", "Project directory must be an absolute path"
    resolved = os.path.realpath(os.path.expanduser(raw))
    if is_sensitive_path(resolved):
        sel().log_api_access(
            caller="dashboard",
            operation="chat.folder_project_dir",
            outcome="denied",
            resources=resolved,
            error="sensitive path",
        )
        return "", "project_dir refers to a sensitive path"
    if not os.path.isdir(resolved):
        return "", "Project directory must be an existing directory"
    return resolved, None


def _folder_project_overlap_denied(resolved: str) -> str | None:
    """Pre-flight the voice-runtime workspace guard for a folder's project_dir.

    A folder's linked project lands on slots verbatim, so without this check
    "link a folder to ~" is refused only at agent spawn.
    Same shared scan and message as the project endpoint and set_project — the
    user-driven moments of choice agree. Returns the refusal message or None.

    Synchronous on purpose (realpath/mkdir priming on first use):
    callers on the event loop MUST run it via ``asyncio.to_thread``, exactly
    like the project endpoint does. It is deliberately NOT part of
    ``_validate_project_dir``: that validator also re-checks STORED values on
    the slot-create read path, where re-priming per read would be loop-blocking
    and where the spawn guard remains the authority.
    """
    conflict = voice_runtime_workspace_conflict(resolved)
    if conflict is None:
        return None
    sel().log_api_access(
        caller="dashboard",
        operation="chat.folder_project_dir",
        outcome="denied",
        resources=resolved,
        error="voice runtime overlap",
    )
    return conflict


def _folder_declared_project(
    folders: list[dict[str, Any]], folder_id: str
) -> tuple[str | None, str | None]:
    """Walk *folder_id*'s ancestor chain for the nearest declared ``project_dir``.

    A dict walk with no filesystem access, so it is safe on the event loop.
    Returns ``(raw_project, None)`` for the nearest folder that declares one
    (stripped, not yet validated), ``("", error)`` when that declaration is not
    a string, and ``(None, None)`` when no folder in the chain declares a
    project: a missing id and a parent cycle both end the walk there.
    """
    by_id = {str(folder.get("id") or ""): folder for folder in folders if isinstance(folder, dict)}
    seen: set[str] = set()
    current_id = folder_id
    while current_id and current_id not in seen:
        seen.add(current_id)
        folder = by_id.get(current_id)
        if folder is None:
            break
        raw_project = folder.get("project_dir")
        if raw_project:
            if not isinstance(raw_project, str):
                return "", "project_dir must be a string"
            return raw_project.strip(), None
        current_id = str(folder.get("parent_id") or "")
    return None, None


def _resolve_folder_project_dir(
    folders: list[dict[str, Any]], folder_id: str
) -> tuple[str, str | None]:
    """Return the nearest validated project directory inherited by a folder."""
    raw_project, error = _folder_declared_project(folders, folder_id)
    if raw_project is None or error:
        return "", error
    return _validate_project_dir(raw_project)


async def resolve_folder_project_dir_off_loop(
    folders: list[dict[str, Any]], folder_id: str
) -> tuple[str, str | None]:
    """:func:`_resolve_folder_project_dir` for callers on the event loop.

    The chain walk is a dict lookup and runs here; only the validation, which
    ``stat``s the directory, hops to a worker thread, and only when a folder in
    the chain declares a project. Most folders declare none, and for those a
    thread hop resolves to ``""`` after queueing on the shared default executor,
    which on a busy gateway makes a folder create (or an agent switch inside a
    folder) slower than the same request on an unfiled chat.
    """
    raw_project, error = _folder_declared_project(folders, folder_id)
    if raw_project is None or error:
        return "", error
    return await asyncio.to_thread(_validate_project_dir, raw_project)


#: Ceiling on the extra steering directories one folder may declare. Small on
#: purpose: these are org-standard/repo-standard roots, not a general file list,
#: and each one is globbed and read at every chat launch in the subtree, so the
#: bound is a cost ceiling as much as a config one. Accumulative inheritance can
#: still stack several folders' lists past this per-folder cap; the resolver's
#: own dedup and the collector's ``_MAX_DOCUMENTS`` bound the total.
MAX_FOLDER_STEERING_DIRS = 16

#: Longest steering-directory string accepted from a client, checked BEFORE the
#: entry is resolved, compared or stored. 4096 is Linux ``PATH_MAX``; nothing
#: longer can name a real directory anywhere, and the bound keeps a
#: ``steering_dirs`` write from parking arbitrary payload in folder state.
MAX_FOLDER_STEERING_DIR_LEN = 4096


def _refuse_principal_steering_dirs(
    request_app: str, steering_dirs: list, *, operation: str, folder_id: str, agent: bool = False
) -> web.Response | None:
    """Only the PERSON may declare steering directories; refuse everyone else.

    A steering directory is a host-file READ the unsandboxed gateway performs
    on the folder's behalf and hands to every chat in the folder. Folder
    permission is not host-file permission: an app (or an admitted crew member)
    that may create and edit its own folders must not be able to point one at
    an arbitrary readable Markdown tree -- the person's notes, a repository
    outside the app's reach -- and have the gateway launder that read into its
    own model session, with no tool grant and no signal. So a NON-EMPTY
    ``steering_dirs`` from a non-person principal is refused at both write
    sites, before any path is touched, and audited as denied. Clearing to
    ``[]`` stays allowed (it only removes reads). The person's own dashboard
    calls carry the empty principal and are unaffected; a person can still
    declare steering on a folder an app or member owns, and the delivery gate
    then routes it to that principal's chats as before.

    ``agent`` marks a call on the internal credential every MCP tool call
    carries. It is refused the same way: an agent proposes a ``folder.steering``
    change card instead, which the person's own browser applies through this
    route.
    """
    if not (request_app or agent) or not steering_dirs:
        return None
    sel().log_api_access(
        caller=request_app or "internal",
        operation=operation,
        outcome="denied",
        source="app_isolation",
        resources=f"folder={folder_id or '-'} steering_dirs={len(steering_dirs)}",
        error="steering_dirs may be declared only by the person",
    )
    return web.json_response(
        {
            "error": (
                "steering_dirs may be declared only from the person's own session: "
                "folder permission does not grant host-file reads"
            ),
            "code": "steering_dirs_forbidden",
        },
        status=403,
    )


def _validate_steering_dirs(value: object) -> tuple[list[str], str | None]:
    """Validate a folder's ``steering_dirs`` list. Returns (resolved, error_msg).

    Reuses the ``_validate_project_dir`` contract per entry: absolute or
    ``~``-prefixed, ``expanduser`` + ``realpath``, sensitive-path rejection
    (SEL-logged like ``project_dir``), and must be an existing directory. Adds
    list-level rules a single project path does not need: a ``16``-entry cap,
    a per-entry length bound, rejection of duplicates within one folder
    (compared by the resolved realpath, so two spellings of one directory are
    still a duplicate), and a UNC gate applied BEFORE resolution -- on Windows
    ``realpath``/``isdir`` on ``\\\\host\\share`` opens an SMB connection to
    that host, so untrusted text must not reach the filesystem unless it names
    a share this gateway already writes to (``unc_probe_allowed``). An empty
    list is valid and resolves to ``[]`` -- and is the ONE spelling that clears
    the field. An explicit ``None`` is refused like any other non-list: a
    ``PATCH {"steering_dirs": null}`` is one ordinary request any client can
    send, and accepting it as "clear" would silently drop a configured list.
    """
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        return [], "steering_dirs must be a list of strings (send [] to clear)"
    if len(value) > MAX_FOLDER_STEERING_DIRS:
        return [], f"steering_dirs may list at most {MAX_FOLDER_STEERING_DIRS} directories"
    if any(len(entry) > MAX_FOLDER_STEERING_DIR_LEN for entry in value):
        return [], (
            f"each steering directory must be at most {MAX_FOLDER_STEERING_DIR_LEN} characters"
        )
    resolved: list[str] = []
    for entry in value:
        stripped = entry.strip()
        if not stripped:
            return [], "steering directory must not be empty"
        if is_unc_shape(stripped) and not unc_probe_allowed(stripped):
            # Lexical check only; never touches the network. Same refusal the
            # attachment and prompt-block readers apply to untrusted UNC text.
            # Before the absolute-path check on purpose: a UNC spelling is not
            # "absolute" on POSIX, and this is the refusal that must win.
            return [], "Steering directory must not be a network (UNC) path"
        if not os.path.isabs(stripped) and not stripped.startswith("~"):
            return [], "Steering directory must be an absolute path"
        if not pinned_fs.supports_pinned_tree_walk():
            # Where a directory cannot be opened relative to a descriptor
            # (native Windows) the collector REFUSES to walk, so a stored value
            # would never be delivered -- and validating it would itself be the
            # by-name probe the collector refuses: ``isdir``/``realpath`` on a
            # name that an agent running as this user has swapped for a
            # junction at a share is the outbound SMB authentication. Refuse
            # here, after the lexical checks and before the FIRST filesystem
            # call, with the same reason the collector logs.
            return [], (
                "Steering directories are not supported on this platform: a directory "
                "cannot be opened relative to a descriptor, so the tree could not be "
                "read without following a swapped link"
            )
        # ``validate_file_path`` is the hardened canonicalizer, not the bare
        # ``realpath``/``isdir`` pair: representability, the UNC trusted-root
        # gate and the Windows LINK-TARGET screen all run BEFORE anything is
        # resolved. The lexical gate above cannot see a local junction aimed at
        # a share, and following it is itself the outbound SMB probe.
        canonical = validate_file_path(stripped)
        if canonical is None:
            # The screen fences sensitive paths ITSELF (returning ``None`` for
            # them as for a bad link target), so this is where a sensitive
            # submission is refused in practice -- audit it here, not only in
            # the explicit re-check below.
            sel().log_api_access(
                caller="dashboard",
                operation="chat.folder_steering_dirs",
                outcome="denied",
                resources=stripped,
                error="refused by path screen (invalid, link into a network share, or sensitive)",
            )
            return [], (
                "Steering directory is not a valid path, or points through a link "
                "into a network or sensitive location"
            )
        if len(canonical) > MAX_FOLDER_STEERING_DIR_LEN:
            # The bound above ran on the SUBMITTED spelling; ``~`` and a link
            # chain expand it, and the canonical form is what ``folders.json``
            # retains and what every later resolve re-validates. An unbounded
            # stored value would trip the first check on every read and drop
            # the whole chain's steering with nothing but a warning -- so the
            # bound applies to the field as stored, not only as typed.
            return [], (
                f"steering directory expands to more than {MAX_FOLDER_STEERING_DIR_LEN} "
                "characters once links and ~ are resolved"
            )
        if is_sensitive_path(canonical):
            # Defense in depth over the screen's own fence, on the canonical
            # spelling, with the project_dir-style audit line.
            sel().log_api_access(
                caller="dashboard",
                operation="chat.folder_steering_dirs",
                outcome="denied",
                resources=canonical,
                error="sensitive path",
            )
            return [], "steering_dirs refers to a sensitive path"
        fence = memory_silo_fence()
        if not fence.complete:
            # The fence is built from the configuration's ``workspaces`` table,
            # which can place a Global V1 workspace at an absolute directory
            # anywhere. When that table cannot be read the default directories
            # are NOT the fence, so no root can be cleared against it: refuse
            # the admission outright (and audit it) rather than admit a root
            # that may contain a workspace this process cannot see. ``[]``
            # (clearing) never reaches this branch, so a degraded config can
            # still remove steering, only not add it.
            sel().log_api_access(
                caller="dashboard",
                operation="chat.folder_steering_dirs",
                outcome="denied",
                resources=canonical,
                error=f"memory store fence incomplete: {fence.degraded}",
            )
            return [], (
                "Steering directories cannot be admitted while the memory-store fence "
                f"is incomplete: {fence.degraded}. Repair config.json and retry"
            )
        if crosses_memory_silo(Path(canonical), fence.roots):
            # A named memory store is a silo. A steering root that is, lies
            # inside or contains the Global workspace or the named-store tree
            # would hand one store's Markdown to every chat in the folder --
            # a V2 member's included -- with nothing going red.
            sel().log_api_access(
                caller="dashboard",
                operation="chat.folder_steering_dirs",
                outcome="denied",
                resources=canonical,
                error="memory store silo",
            )
            return [], (
                "Steering directory must not be, contain, or lie inside a memory store "
                "(the crew workspace or memory_stores directory)"
            )
        # Existence is proven by OPENING the directory the way the collector
        # will read it -- ancestor chain pinned, the leaf ``O_DIRECTORY |
        # O_NOFOLLOW`` -- not by ``isdir`` on the name: after the screen above
        # the name can be swapped for a link, and a by-name ``isdir`` follows
        # whatever sits there now (on Windows a junction at a share is the
        # SMB probe). The pinned open refuses a link outright, and the
        # descriptor is closed at once; nothing is read here.
        try:
            probe_fd = pinned_fs.open_dir_pinned(canonical, what="steering directory")
        except (pinned_fs.PinnedPathRefusal, OSError):
            return [], "Steering directory must be an existing directory"
        os.close(probe_fd)
        if canonical in resolved:
            return [], "steering_dirs must not repeat a directory"
        resolved.append(canonical)
    return resolved, None


def slot_steering_principal(slot: Any, execution_context: Any) -> str:
    """The non-person principal a chat slot runs AS, in ``owner_app``'s space.

    The slot-side mirror of ``token_auth.folder_principal``: that function
    stamps ``owner_app`` on a folder from the WRITER (an app's bare name, or
    ``"member:<store>"`` for an admitted crew member), and
    :func:`_resolve_folder_steering_dirs` compares ``owner_app`` against the
    value returned here. The two must be spelled from the same alphabet or the
    fence silently drops a principal's own steering: a member-owned folder is
    stamped ``member:<store>`` while a member slot's ``_app`` is empty, so
    comparing against ``_app`` alone would skip the member's own documents.

    * an App Kit slot -> its ``_app``, checked FIRST exactly as the writer side
      checks the app claim first (the two are mutually exclusive in practice,
      but the ordering keeps an app's principal byte-identical);
    * a V2 member execution -> ``"member:<store>"`` from the CAPTURED execution
      context's store (``legacy_name``, the same field the request gate reads
      for the writer side), never from a later mutable declaration;
    * a person's slot -> ``""``, matching an absent ``owner_app``.
    """
    app = str(getattr(slot, "_app", "") or "")
    if app:
        return app
    if execution_context is None or not getattr(execution_context, "member_id", None):
        return ""
    store = str(getattr(getattr(execution_context, "store", None), "legacy_name", "") or "")
    return f"member:{store}" if store else ""


def _resolve_folder_steering_dirs(
    folders: list[dict[str, Any]], folder_id: str, *, slot_app: str = ""
) -> tuple[list[str], str | None]:
    """Return the steering directories a folder inherits, ACCUMULATIVELY.

    Unlike ``_resolve_folder_project_dir`` (nearest ancestor wins), steering
    directories accumulate up the ``parent_id`` chain root-first: an
    org-standards folder above a per-repo folder contributes both sets. The walk
    is cycle-guarded exactly like the project resolver; each folder's stored
    value is RE-VALIDATED (not trusted from ``folders.json``, which can list a
    directory that has since been moved or become sensitive) and deduped by
    resolved realpath, keeping the first occurrence.

    Re-validation is PER ENTRY at read time: a directory that has since
    disappeared, been renamed, or become sensitive is skipped with a warning
    and the rest of the chain still steers -- the collector's own
    skip-and-continue contract, and the one ``config.md`` promises. Failing
    the whole resolution would let one stale ancestor entry silently drop
    every other folder's standards for the entire subtree. Only a MALFORMED
    stored shape (not a list of strings, or over the count ceiling) fails the
    resolution, matching the project resolver's contract for a corrupt
    ``folders.json``.

    Ownership gates WHOSE steering a chat receives: a folder the person owns
    contributes to every chat filed beneath it, but a folder a non-person
    principal created (``owner_app``: an app's name, or ``member:<store>`` for a
    crew member) contributes only to slots running AS that principal
    (*slot_app*, spelled by :func:`slot_steering_principal`). A person can file
    a chat into, or nest a folder under, an app's folder, and accumulative
    inheritance would otherwise let the app write rules into the person's model
    context through the parent chain -- a confused deputy the tree-shaping
    ownership rules never intended to allow.
    """
    by_id = {str(folder.get("id") or ""): folder for folder in folders if isinstance(folder, dict)}
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current_id = folder_id
    while current_id and current_id not in seen:
        seen.add(current_id)
        folder = by_id.get(current_id)
        if folder is None:
            break
        chain.append(folder)
        current_id = str(folder.get("parent_id") or "")
    # Root-first so an ancestor's standards land ahead of a child's additions.
    result: list[str] = []
    for folder in reversed(chain):
        raw = folder.get("steering_dirs")
        if raw is None or raw == []:
            # Absent or an empty LIST is "declares none" -- the only two shapes
            # the writer stores for that. Any other falsy value (``{}``, ``""``,
            # ``0``) is a corrupt folders.json and must fail the shape check
            # below, not slide past it as "nothing declared".
            continue
        owner = _folder_owner_app(folder)
        if owner and owner != slot_app:
            continue
        if not isinstance(raw, list) or any(not isinstance(entry, str) for entry in raw):
            return [], "steering_dirs must be a list of strings"
        if len(raw) > MAX_FOLDER_STEERING_DIRS:
            return [], f"steering_dirs may list at most {MAX_FOLDER_STEERING_DIRS} directories"
        for entry in raw:
            resolved, err = _validate_steering_dirs([entry])
            if err:
                logger.warning(
                    "folder %s: steering directory skipped at read time (%s)",
                    folder.get("id"),
                    err,
                )
                continue
            for one in resolved:
                if one not in result:
                    result.append(one)
    return result, None
