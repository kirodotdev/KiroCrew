"""Vault and note paths for the Notes backend, and the note I/O that runs through them.

Every vault-relative path a request names is confined here before anything touches the
disk: a vault's content root and ``safe_join``, the portable-name and linked-folder checks,
the trash and mutation paths, and the checked directory and staging writes a note writer
makes under ``server.vault_write_lock`` (the attach route screens its own folder in
``server``). Beside them: the staged write a note save makes beside the note; listing,
snapshotting and reading the notes; and the save guard -- the freshness checks a save
re-runs before each publish attempt, and the attempt itself. Reached through ``server``,
its facade.
"""

from __future__ import annotations

import asyncio
import contextlib
import ntpath
import os
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from kiro_crew import hooks, platform_compat
from kiro_crew.apps.builtins.md_notebook import git_ops
from kiro_crew.apps.builtins.md_notebook.server_http import ApiError
from kiro_crew.apps.builtins.md_notebook.server_store import logger
from kiro_crew.constants import WINDOWS_DEVICE_STEMS

# Save-guard tolerance in milliseconds: filesystems round mtimes differently.
MTIME_TOLERANCE_MS = 1
# baseMtime the client echoes back to confirm "yes, recreate this deleted note".
# A real mtime is never negative, so it can't collide with a genuine timestamp.
_RECREATE_SENTINEL = -1

#: Retry budget for a note save whose rename hit the Windows sharing-violation
#: window. Mirrors ``atomic_write.replace_with_retry`` (10 x 50ms), whose window
#: this is; the retry lives in ``server._save_note_contents`` rather than in the rename so
#: each attempt can re-run the stale-write check first.
_SAVE_MAX_ATTEMPTS = 10
_SAVE_BACKOFF_SECONDS = 0.05


# ---------------------------------------------------------------------------
# Vault paths and note scanning
# ---------------------------------------------------------------------------


def content_root(vault: dict[str, Any]) -> Path:
    """The directory notes live in: the vault root, or its subfolder scope.

    The subfolder is user-supplied, so it is confined to the vault exactly the
    way note paths are. Joining it raw would let an absolute value replace the
    root outright — `Path("/vault") / "/etc"` is `/etc` — which would rebase
    every later `safe_join` onto that new root and turn a scoped vault into a
    reader for any file the gateway can open.
    """
    root = Path(vault["localPath"])
    subfolder = vault.get("subfolder")
    if not subfolder:
        return root
    return safe_join(root, subfolder)


async def vault_path(vault: dict[str, Any], rel: Optional[str] = None) -> Path:
    """``content_root`` plus an optional ``safe_join``, off the event loop.

    Both call ``Path.resolve()``, which is synchronous filesystem I/O — on a
    network-mounted vault a stalled mount would otherwise freeze the whole
    gateway loop from inside an async handler.
    """

    def _resolve() -> Path:
        root = content_root(vault)
        return safe_join(root, rel) if rel else root

    return await asyncio.to_thread(_resolve)


#: Characters Win32 forbids in a path component. Refused for a name the app is
#: about to CREATE on every platform, and for every name when running on Windows,
#: because a vault is meant to be portable: a note named ``a:b.md`` created on
#: macOS makes the Windows clone of the same vault un-checkoutable (git's own
#: ``core.protectNTFS`` refuses it, reported only as a clone failure), and on
#: Windows the same move does not fail at all — ``os.replace`` writes an NTFS
#: ALTERNATE DATA STREAM on a file named ``a``, which ``_list_note_files_sync``
#: never walks, so the note silently disappears from the app instead of erroring.
_UNPORTABLE_CHARS = frozenset('<>:"|?*')


def _reject_unportable_component(part: str, field: str) -> None:
    """Refuse one path component Win32 cannot represent. Raises ``ApiError`` 400.

    Applied on EVERY platform for a name the caller is about to CREATE (a move
    destination, a new-note folder, or a saved path's new components), so an
    unportable name never enters a vault that is meant to travel; and on Windows
    for every name, where such a path is not merely unwise but silently wrong. Reading and re-saving an oddly-named note that already
    exists on a POSIX host stays allowed — that is the only way a user can rename
    it to something portable.
    """
    if _UNPORTABLE_CHARS.intersection(part) or any(ord(ch) < 32 for ch in part):
        raise ApiError(
            f'{field} must not contain < > : " | ? * or a control character — '
            "such a name cannot exist on Windows, so it would break any Windows "
            "clone of this vault",
            400,
            code="path_not_a_note",
        )
    if part != part.rstrip(". "):
        raise ApiError(
            f"{field} must not have a path component ending in a dot or a space — "
            "Windows silently strips it, so the name would not round-trip",
            400,
            code="path_not_a_note",
        )
    if part.split(".", 1)[0].lower() in WINDOWS_DEVICE_STEMS:
        raise ApiError(
            f"{field} uses '{part}', a name Windows reserves for a device",
            400,
            code="path_not_a_note",
        )


def _reject_unportable_new_components(root: Path, rel: str) -> None:
    """Refuse a save that would CREATE a path component Win32 cannot hold.

    A save mkdirs missing parents and may create the note itself, so every
    component that does not exist yet is a NEW name entering the vault and is
    held to the portability rule on every platform — the same rationale as
    ``require_note_path``'s ``for_new``. Components that already exist are left
    alone: re-saving an oddly-named note a POSIX vault already contains stays
    allowed, which is the only way a user can rename it to something portable.

    Runs one synchronous ``exists`` stat per component, so call it off the
    event loop.
    """
    current = root
    missing = False
    for part in PurePosixPath(rel.replace("\\", "/")).parts:
        if not missing:
            current = current / part
            missing = not current.exists()
        if missing:
            _reject_unportable_component(part, "path")


def require_note_path(rel: Any, field: str = "path", *, for_new: bool = False) -> str:
    """Validate a caller-supplied path as one this app is allowed to touch.

    Containment is not enough. ``safe_join`` / ``vault_mutation_path`` only prove
    the path stays INSIDE the vault, and a vault contains far more than notes: a
    delete of ``.git/config`` is contained, and would move the repository's
    configuration into ``.trash/config.md``, breaking the vault (and its remote
    binding) for every later operation. Save, move and duplicate reach the same
    places.

    So the addressable surface is pinned to the LISTED surface — the same two rules
    the note walk applies (see ``_scan``): every component must be undotted, and the
    file must end in ``.md``. That excludes `.git/`, `.trash/` and dotfiles by
    construction rather than by naming them, so a future dotted directory is
    covered without another edit.

    Raises ``ApiError`` 400; callers pass ``field`` so the message names the
    parameter the caller actually sent (``from`` / ``to`` on a move).

    ``for_new`` marks a path the caller is about to CREATE (a move destination).
    Those are additionally held to what Win32 can represent, on every platform,
    so a vault does not acquire a note only some of its clones can check out.
    """
    if not rel or not isinstance(rel, str):
        raise ApiError(f"{field} is required", 400, code="path_required")
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    if not parts or any(p.startswith(".") for p in parts):
        raise ApiError(
            f"{field} must not contain a dotted path component",
            400,
            code="path_not_a_note",
        )
    if for_new or platform_compat.IS_WINDOWS:
        for part in parts:
            _reject_unportable_component(part, field)
    if not parts[-1].lower().endswith(".md"):
        raise ApiError(f"{field} must be a .md note", 400, code="path_not_a_note")
    return rel


def require_folder_path(folder: Any, *, for_new: bool = False) -> Optional[str]:
    """Validate an optional caller-supplied FOLDER (the new-note target).

    Same undotted rule as `require_note_path`, without the `.md` requirement: a
    folder is not a note. Without it a new note could be created inside `.git`.

    ``for_new`` marks a folder the caller is about to CREATE (``api_note_new``
    mkdirs it). Such a name is additionally held to what Win32 can represent,
    on every platform — the same rationale as ``require_note_path``: a vault
    travels, and a folder named ``CON`` written on a POSIX host makes every
    Windows clone un-checkoutable (core.protectNTFS).
    """
    if folder is None or folder == "":
        return None
    if not isinstance(folder, str):
        raise ApiError("folder must be a string", 400, code="path_not_a_note")
    parts = PurePosixPath(folder.replace("\\", "/")).parts
    if any(p.startswith(".") for p in parts):
        raise ApiError(
            "folder must not contain a dotted path component",
            400,
            code="path_not_a_note",
        )
    if for_new or platform_compat.IS_WINDOWS:
        # With ``for_new`` the folder IS about to be created, so the
        # portability rule applies on every platform; without it the folder is
        # merely resolved, and the check still stops a note being written into
        # a folder Win32 cannot address when running ON Windows.
        for part in parts:
            _reject_unportable_component(part, "folder")
    return folder


async def trash_dir_path(vault: dict[str, Any]) -> Path:
    """The vault's `.trash` directory, refusing it if the entry is a SYMLINK.

    `vault_mutation_path` proves containment, which is not enough here. A cloned or
    attached repo can carry `.trash -> public` (git stores symlinks), and a target
    INSIDE the vault passes containment — then `mkdir(exist_ok=True)` follows the
    link and `os.replace` lands the deleted note under `public/`. `git_ops.status()`
    filters paths beginning `.trash/`, so a note now living at `public/One.md` is
    staged and pushed to the remote: the delete flow's one promise, that a trashed
    note never leaves the machine, is broken without leaving the vault at all.

    So any symlink is refused, escaping or not. `lstat` via `is_symlink()` is the
    only test that can see it — `exists()` and `is_dir()` both follow the link.
    On Windows the same redirection is possible via a directory JUNCTION, which
    `is_symlink()` does NOT report, so `is_link_or_junction` catches both.
    """
    trash_dir = await vault_mutation_path(vault, git_ops.TRASH_DIR)
    await asyncio.to_thread(reject_linked_trash, trash_dir)
    return trash_dir


def reject_linked_trash(trash_dir: Path) -> None:
    """Raise ``trash_is_symlink`` when the `.trash` entry is a link or junction.

    Synchronous (one ``lstat``): the delete calls it again as the FIRST statement
    of the worker that mkdirs and moves into `.trash`, under ``vault_write_lock``,
    so a ``git merge`` cannot install the link between the check and the write —
    the same shape as ``reject_linked_folder_component`` for the other writers.
    """
    if platform_compat.is_link_or_junction(trash_dir):
        raise ApiError(
            f"this vault's {git_ops.TRASH_DIR} is a symlink — refusing to use it, "
            "because a trashed note would be written somewhere the app does not "
            "keep out of git",
            400,
            code="trash_is_symlink",
        )


async def vault_mutation_path(vault: dict[str, Any], rel: str) -> Path:
    """In-vault path for a delete/move — validated, but NOT symlink-resolved.

    ``safe_join`` returns the ``realpath``, which for an in-vault alias
    (``alias.md -> real.md``) is the *target*. Handing that to ``os.unlink`` /
    ``os.rename`` would destroy or move ``real.md`` while leaving a broken
    ``alias.md``. Delete/move must act on the named entry itself, so this
    validates containment via ``safe_join`` (which still rejects ``..``,
    absolute paths, and any symlink escaping the vault) and then returns the
    LEXICAL ``root/rel`` — ``unlink``/``rename`` do not follow a final-component
    symlink, so the alias entry is what gets removed or renamed.
    """

    def _resolve() -> Path:
        root = content_root(vault)
        safe_join(root, rel)  # containment validation only (raises on escape)
        return root / PurePosixPath(rel.replace("\\", "/"))

    return await asyncio.to_thread(_resolve)


def linked_folder_component(vault: dict[str, Any], folder: str) -> bool:
    """True if any component of the LEXICAL in-vault ``folder`` is a link.

    ``safe_join`` returns the REALPATH, and a realpath passes containment as long
    as the link's TARGET is inside the vault — so a cloned
    ``Projects -> .git/refs/heads`` resolves to the vault's own ``.git`` and any
    write that mkdirs or creates through ``Projects`` lands inside ``.git``. The
    component validators screen only what the caller typed, and a link's own
    name carries no dot. So walk the components themselves, root-first, from the
    vault root: the scope's (``subfolder``) first, then ``folder``'s. The scope
    is walked too because ``content_root`` returns its REALPATH — started from
    there, a scope that is itself a tracked link would already be resolved past
    the link before the walk began. Only those components: the vault root's own
    ancestors are the user's filesystem (``~/Notes -> /Volumes/SSD/Notes``,
    macOS's ``/tmp``), not the vault's content, and a link there is not a link
    inside the vault. Normalised the way ``safe_join`` normalises, or
    ``Projects\\Sub`` would be one component here and two there.

    Synchronous filesystem I/O (one ``lstat`` per component): call it from
    the SAME worker thread as the write it guards, as that write's first
    statement, and hold ``vault_write_lock`` around that one offloaded call.
    The two together are what make the check mean something: the walk sees the
    tree as it is at that instant, and the lock is what keeps the tree that way
    until the ``mkdir``/``open`` — every ``git merge`` that could swap a checked
    folder for a link (``api_sync``, ``api_commit``, ``syncer._sync_vault``)
    holds the same lock for its whole run. Never ``to_thread`` the check on its
    own and the write after: the ``await`` between them is outside the lock's
    cover only if the lock is released too, but it is also a window in which
    a same-process writer can be reordered against the check. One walk for
    every step that creates through folder components — ``api_note_new`` and
    ``api_note_duplicate`` (create), ``api_note_save`` (``mkdir``, then the
    staging ``open``) and ``api_note_move`` (``mkdir`` + rename) — so the next
    write path gets the same check, and the same lock, by calling it rather
    than re-deriving it.
    """
    current = Path(vault["localPath"])
    for raw in (vault.get("subfolder") or "", folder or ""):
        for part in PurePosixPath(raw.replace("\\", "/")).parts:
            current = current / part
            if platform_compat.is_link_or_junction(current):
                return True
    return False


def reject_linked_folder_component(vault: dict[str, Any], folder: str) -> None:
    """Raise ``folder_is_symlink`` when ``linked_folder_component`` finds one.

    The offending component is not echoed: which entry is a link is filesystem
    layout the caller supplied a path to guess at.
    """
    if linked_folder_component(vault, folder):
        raise ApiError("cannot write through a symlinked folder", 400, code="folder_is_symlink")


def resolve_write_dir_checked(vault: dict[str, Any], folder: str) -> Path:
    """Reject a linked folder component, THEN resolve its directory — atomically.

    The single ``realpath`` that turns a note's folder into the absolute
    directory a write creates through (``safe_join`` at the heart of
    ``vault_path`` / ``content_root``) FOLLOWS an in-vault symlink: a cloned
    ``Projects -> .git/refs/heads`` resolves to the vault's own ``.git`` and,
    because that target is still inside the vault, passes containment. When that
    resolution runs BEFORE ``vault_write_lock`` is taken and its result is then
    written to inside the lock, a sync's merge that lands (or removes) the link
    while the writer parks on the lock leaves the write pointed at the STALE
    resolved target: the in-lock lexical walk sees the tree the merge produced
    and passes, while the ``mkdir``/``open`` still lands at the cached link
    target.

    Resolving here — as the FIRST offloaded step run INSIDE the lock, after the
    lexical reject and in the SAME worker thread as the write it feeds — closes
    that window. The reject sees the tree as it is at this instant; the
    resolution reads that same tree (and because the reject cleared every linked
    folder component first, this ``realpath`` has no in-vault folder link to
    follow); and the lock holds the tree that way through the following
    ``mkdir``/``open`` — every ``git merge`` that could swap a component holds
    the same lock for its whole run. Callers pass the note's folder (``''`` for a
    root note) and append the note name to the returned directory themselves,
    because that name is chosen (new/duplicate) or already known (save/move)
    without changing which directory the write lands in.
    """
    reject_linked_folder_component(vault, folder)
    root = content_root(vault)
    # Canonicalise on BOTH branches. `safe_join` already realpaths the folder
    # case; the empty-folder (root note) case must too, or a vault whose stored
    # `localPath` traverses a symlink (a relocated crew home, `MD_NOTEBOOK_HOME`
    # through an alias — `api_vault_clone` persists the path verbatim) returns a
    # non-canonical root here while the save's `tmp.parent` comparison uses a
    # realpath, so every root-level save would be refused `folder_is_symlink`.
    return safe_join(root, folder) if folder else Path(os.path.realpath(str(root)))


def mutation_path_checked(vault: dict[str, Any], rel: str) -> Path:
    """Validate under the write lock, retaining the lexical scope and note entry.

    Run in the mutation's worker: a scope resolved before the lock can name a
    stale link target. Containment checks may resolve the note for validation,
    but rename must receive the lexical entry so a final alias moves as a link.
    """
    reject_linked_folder_component(vault, note_folder(rel))
    safe_join(content_root(vault), rel)
    scope = PurePosixPath((vault.get("subfolder") or "").replace("\\", "/"))
    return Path(vault["localPath"]) / scope / PurePosixPath(rel.replace("\\", "/"))


def note_folder(rel: str) -> str:
    """The folder part of a vault-relative note path, '' for a root note."""
    parent = PurePosixPath(rel.replace("\\", "/")).parent
    return "" if parent == PurePosixPath(".") else parent.as_posix()


def _new_staged_note_path(path: Path) -> Path:
    """Allocate the exact sibling path one note transaction will own."""
    return path.with_name(git_ops.staged_temp_name(path.name))


def _stage_note_text_sync(tmp: Path, content: str) -> None:
    """Write *content* to the already-owned *tmp* path and fsync it.

    `write_text` truncates first, so a failure partway -- a full disk is the
    realistic one -- leaves the note truncated or half-written with no copy of
    what was there. Staging into a sibling keeps the temp on the same filesystem,
    so the later rename is atomic, and the `.tmp` suffix keeps it out of the
    `.md` scan.

    The temp is created ONCE per save and handed back to the caller, which
    republishes THAT SAME path on every retry. Staging a fresh temp per attempt
    would leave one orphan behind for each contended attempt whose cleanup unlink
    also lost the race -- and because the retry ends in a 200, nothing would tell
    the user to go and look.

    The caller allocates the name and registers it with
    `git_ops.inflight_temp` BEFORE entering this function. That ordering is part
    of the contract: `open` makes the untracked file visible immediately, so a
    concurrent Sync could otherwise discover and commit it while this function
    was still writing. The caller keeps the registration through publish or
    cleanup, making ownership cover every instant the temp can exist.
    """
    # newline="" disables the platform newline translation that would
    # otherwise rewrite the note's "\n" as "\r\n" on Windows, so a note's
    # bytes are identical on every OS.
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())


def _publish_staged_sync(tmp: Path, path: Path) -> None:
    """Rename the staged temp over the note. A SINGLE attempt, on purpose.

    Retrying here would be wrong: the save is guarded by an optimistic
    concurrency check (`baseMtime` -> stat -> 409 ESTALE), and a retry inside the
    rename extends the window between that check and the publish by the whole
    retry budget, so an external edit landing in it would be silently
    overwritten. The retry lives in `_save_note_contents`, which revalidates
    before each attempt -- and republishes this same temp.

    A successful rename consumes the temp, which is what keeps a 200 from ever
    coexisting with a surviving temp this save owns.
    """
    os.replace(tmp, path)


def _discard_staged_sync(tmp: Path) -> None:
    """Best-effort removal of one EXACT temp this save owns.

    Deliberately not a `glob` over sibling `<note>.*.tmp`: on the guarded path
    the per-note lock is released across the backoff, so a concurrent save may
    legitimately own a temp for the same note, and sweeping would destroy it.

    Best-effort is safe to leave best-effort: the handle that refuses a rename
    commonly refuses this unlink too. While the save is still running the temp
    is registered with `git_ops.inflight_temp`, so `status()` keeps it out of a
    commit; once the transaction ends an orphan becomes ordinary visible content
    rather than a file silently withheld from the user's repository. Tidiness is
    what is lost here, never correctness.
    """
    with contextlib.suppress(OSError):
        os.unlink(tmp)


def _atomic_write_text_sync(path: Path, content: str) -> None:
    """Stage, publish, and clean up on failure -- the single-attempt whole.

    Stages BESIDE the target (a note inside the vault, possibly a different
    filesystem from the crew home). State files (vaults/settings/PAT) use
    ``_write_state_staged_sync`` instead, which stages in the masked staging
    dir. The note save drives the two halves itself so it can republish one
    temp across attempts.
    """
    tmp = _new_staged_note_path(path)
    with git_ops.inflight_temp(tmp):
        try:
            _stage_note_text_sync(tmp, content)
            _publish_staged_sync(tmp, path)
        except BaseException:
            _discard_staged_sync(tmp)
            raise


def _make_note_dirs_checked(vault: dict[str, Any], rel: str) -> None:
    """``mkdir -p`` a note's parent, the link walk + resolution as its first steps.

    One offloaded step, so no ``await`` separates the check from the ``mkdir``
    (see ``linked_folder_component``). ``resolve_write_dir_checked`` rejects a
    linked folder component and resolves the parent afresh here — after the
    reject and under the vault lock — rather than trusting a path resolved before
    the lock; the ``mkdir`` then lands in that resolved directory, which (with
    every linked component rejected) is the note's own lexical parent.
    """
    resolved = resolve_write_dir_checked(vault, note_folder(rel))
    resolved.mkdir(parents=True, exist_ok=True)


def _stage_note_text_checked(vault: dict[str, Any], rel: str, tmp: Path, content: str) -> None:
    """``_stage_note_text_sync`` with the link walk + resolution as its first steps.

    The staging ``open`` is the save's only write that CREATES through the
    folder components (the publish is a rename of the staged file, which exists
    in the real directory or not at all), so it is the step the walk must
    precede in the same thread. A ``mkdir`` that just ran is no proof: an empty
    untracked directory is one ``git merge`` may replace with the link, while a
    directory holding the untracked temp is one it refuses to. Rejecting a linked
    component AND resolving the directory here — under the lock, before the open
    — is what keeps the staging temp from being opened through a link target
    resolved before the lock. ``tmp`` is a sibling of the caller's lexical
    ``abs_path`` (already registered with ``inflight_temp`` before this runs); the
    resolved directory equals that lexical parent once every linked component is
    rejected, so the registered temp is the one that gets opened.
    """
    resolved = resolve_write_dir_checked(vault, note_folder(rel))
    if resolved != Path(os.path.realpath(str(tmp.parent))):
        # The typed folder resolved somewhere other than the temp's own
        # directory — only reachable if an in-vault link slipped past the
        # lexical reject. Both sides are canonicalised so a legitimate symlink
        # ABOVE the vault root (an attached ``~/Notes`` alias, a relocated crew
        # home, ``MD_NOTEBOOK_HOME`` through an alias — all supported) does not
        # trip this: ``tmp.parent`` is the lexical ``content_root/<folder>`` and
        # ``resolved`` is its realpath, so they differ only when an in-vault
        # component is itself a link, which is exactly the write we refuse.
        raise ApiError("cannot write through a symlinked folder", 400, code="folder_is_symlink")
    _stage_note_text_sync(tmp, content)


def safe_join(root: Path, rel_path: str) -> Path:
    """Resolve a note path inside the vault, refusing to escape it.

    Validated in two stages, because either alone leaves a hole. The component
    check rejects the obvious escapes (absolute paths, `..`, a drive letter)
    *before* the value ever reaches a filesystem call. Then a single
    ``os.path.realpath`` both resolves symlinks — catching a link inside the
    vault that points out of it — and produces the value that is BOTH guarded
    by the prefix check and returned: a ``startswith`` on a ``realpath`` result
    is the barrier ``py/path-injection`` recognizes, so the taint stops here
    rather than being flagged at every downstream use. A separate
    check-then-fresh-``resolve()`` returns a different value and does not
    neutralize the flow.
    """
    candidate = PurePosixPath(rel_path.replace("\\", "/"))
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or ntpath.isabs(rel_path)
        or ntpath.splitdrive(rel_path)[0]
    ):
        raise ApiError("path escapes vault", 400, code="path_escapes_vault")
    root_resolved = os.path.realpath(str(root))
    resolved = os.path.realpath(os.path.join(root_resolved, candidate.as_posix()))
    if resolved != root_resolved and not resolved.startswith(root_resolved + os.sep):
        raise ApiError("path escapes vault", 400, code="path_escapes_vault")
    return Path(resolved)


def _list_note_files_sync(root: Path) -> list[str]:
    """Vault-relative posix paths of every .md file, skipping dotted entries."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune hidden directories in place — .git, .obsidian, .trash and friends.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or not name.lower().endswith(".md"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)


async def list_note_files(root: Path) -> list[str]:
    return await asyncio.to_thread(_list_note_files_sync, root)


def _snapshot_sync(root: Path) -> dict[str, float]:
    snap: dict[str, float] = {}
    for rel in _list_note_files_sync(root):
        try:
            snap[rel] = os.stat(root / rel).st_mtime
        except OSError:
            continue
    return snap


# ---------------------------------------------------------------------------
# Note reads and the save guard
# ---------------------------------------------------------------------------


async def read_note_text(path: Path) -> Optional[str]:
    """Vault file contents, or None when the gate refuses the path.

    EVERY read of vault content goes through ``_read_note_text_sync``.
    `safe_join` proves a path stays inside the vault, but a vault is any folder
    the user attaches and a `.md` entry inside it can be a symlink to a private
    key, so containment is not enough on its own. `safe_read_file_bytes`
    canonicalizes via realpath, refuses a sensitive resolved target and opens
    with O_NOFOLLOW.

    Centralized deliberately: applied per call site instead, every new read path
    is a fresh hole.
    """
    return await asyncio.to_thread(_read_note_text_sync, path)


def _read_note_text_sync(path: Path) -> Optional[str]:
    """Read through the sensitive-path gate in the caller's filesystem worker."""
    try:
        data = hooks.safe_read_file_bytes(str(path))
    except (OSError, hooks.FileTooLargeError):
        return None
    if data is None:
        return None
    # Normalize line endings to "\n": the read is binary (through the sensitive
    # gate), so a note written with CRLF elsewhere (Obsidian on Windows, a repo
    # with autocrlf) would otherwise reach the block editor as "\r\n" and produce
    # mixed endings on the next save. Writes already emit "\n" (newline="").
    return data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


async def _assert_note_is_fresh(abs_path: Path, rel: str, base_mtime: object) -> None:
    """Raise 409 ESTALE if the note changed on disk since the client read it.

    Split out of the save handler so its retry in ``server`` can re-run it. It must be
    cheap to repeat and must not mutate anything: it is the freshness half of a
    check-and-write, and a retry that skipped it would publish against a file it
    never revalidated.
    """
    if isinstance(base_mtime, (int, float)):
        current: Optional[float] = None
        try:
            current = (await asyncio.to_thread(os.stat, abs_path)).st_mtime * 1000
        except FileNotFoundError:
            # A supplied baseMtime means the file existed when the client read
            # it, so its absence now is an external deletion — a conflict, not
            # a new file. Silently rewriting would resurrect a note the user
            # deleted in Obsidian or via `git pull`.
            #
            # The conflict hands back the -1 sentinel as the new mtime; when
            # the user chooses "keep mine" the client retries with
            # baseMtime == -1, which is the one value that means "yes,
            # recreate it" and falls through to the save's write. Without this
            # escape hatch the retry would hit this same branch forever and
            # the edit could never be saved.
            if base_mtime != _RECREATE_SENTINEL:
                raise ApiError(
                    "this note was deleted on disk since you opened it",
                    409,
                    code="ESTALE",
                    mtime=_RECREATE_SENTINEL,
                    disk="",
                )
        if current is not None and abs(current - base_mtime) > MTIME_TOLERANCE_MS:
            # The conflict response hands this content back to the client, so
            # it goes through the same gate as every other read: containment
            # inside the vault says nothing about what the vault holds.
            disk = await read_note_text(abs_path)
            if disk is None:
                raise ApiError(f"no such note: {rel}", 404, code="no_such_note")
            raise ApiError(
                "this note changed on disk since you opened it",
                409,
                code="ESTALE",
                mtime=current,
                disk=disk,
            )


async def _observed_note_mtime(abs_path: Path) -> Optional[float]:
    """The note's current mtime in ms, or None when it does not exist."""
    try:
        return (await asyncio.to_thread(os.stat, abs_path)).st_mtime * 1000
    except FileNotFoundError:
        return None


async def _assert_note_unchanged(abs_path: Path, rel: str, observed: Optional[float]) -> None:
    """Raise 409 ESTALE if the note moved off *observed* since it was sampled.

    The tokenless counterpart to `_assert_note_is_fresh`. A save with no
    ``baseMtime`` has no client-supplied baseline to revalidate against, so the
    server takes its OWN: the state the save was about to overwrite, sampled
    before the first publish attempt.

    The note lock serializes API writers, but it says nothing about an editor
    writing the file directly -- Obsidian, `git pull`, an external script. That
    writer lands during the retry backoff, and without this check the next
    attempt renames over an edit nobody revalidated, answering 200 for a save
    that destroyed a newer one.

    Only RETRIES consult it. The first attempt is what the caller asked for:
    overwriting the state sampled here is the save, not a conflict.
    """
    current = await _observed_note_mtime(abs_path)
    if current is None and observed is None:
        return
    if (
        current is not None
        and observed is not None
        and abs(current - observed) <= MTIME_TOLERANCE_MS
    ):
        return
    if current is None:
        # Deleted under us mid-retry. Reported with the same -1 sentinel the
        # guarded path uses, so the client's "keep mine" retry (baseMtime == -1)
        # is the one escape hatch on both paths rather than two dialects.
        raise ApiError(
            "this note was deleted on disk while your save was retrying",
            409,
            code="ESTALE",
            mtime=_RECREATE_SENTINEL,
            disk="",
        )
    disk = await read_note_text(abs_path)
    raise ApiError(
        "this note changed on disk while your save was retrying",
        409,
        code="ESTALE",
        mtime=current,
        disk=disk if disk is not None else "",
    )


async def _publish_note_once(abs_path: Path, tmp: Path, attempt: int) -> bool:
    """Publish the staged temp once. True on success, False to retry.

    Takes the temp rather than the content: every attempt republishes the SAME
    staged file, so a contended save can never leave a trail of orphaned temps
    behind a 200.

    Terminal failures are raised rather than reported: POSIX re-raises a
    ``PermissionError`` immediately (there it means the caller genuinely cannot
    write, and waiting changes nothing), and an exhausted budget propagates the
    last one.
    """
    try:
        await asyncio.to_thread(_publish_staged_sync, tmp, abs_path)
        return True
    except PermissionError:
        if not platform_compat.IS_WINDOWS:
            raise
        if attempt == _SAVE_MAX_ATTEMPTS - 1:
            raise
        logger.debug(
            "md-notebook: note rename contended at %s; retrying (%d/%d)",
            abs_path,
            attempt + 1,
            _SAVE_MAX_ATTEMPTS,
        )
        return False
