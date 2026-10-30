"""Notes backend — git-backed markdown vaults for KiroCrew.

Ported from the app's Node backend. The gateway proxies
``/apps/md-notebook/api/*`` to this process, preserving the ``/api/`` prefix, so
the routes below are exactly the paths the UI calls.

State layout (overridable via ``MD_NOTEBOOK_HOME`` for tests):

    <home>/vaults.json      vault descriptors (no secrets)
    <home>/pat              GitHub token, chmod 0600
    <home>/vaults/<id>/     vaults this app cloned itself

Two kinds of vault: one this app cloned into ``<home>/vaults/``, and one
"attached" in place — a folder the user also edits with Obsidian, an editor, or
the git CLI. The attached case is why saves are guarded against concurrent
external edits and why the note listing is re-scanned rather than trusted.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib as _importlib
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

from aiohttp import web

from kiro_crew import _facade_mirror, hooks, platform_compat, security
from kiro_crew.apps.builtins.md_notebook import git_ops
from kiro_crew.apps.builtins.md_notebook import notes as notes_mod
from kiro_crew.apps.builtins.md_notebook.server_host import (
    _find_gh,
    _pick_folder_sync,
    _reveal_binary,
    _reveal_folder_sync,
    _trusted_env,
    pick_folder_supported,
)
from kiro_crew.apps.builtins.md_notebook.server_http import (
    ApiError,
    error_middleware,
    json_body,
    proxy_auth_middleware,
    require_vault,
    require_writable,
)
from kiro_crew.apps.builtins.md_notebook.server_paths import (
    _SAVE_BACKOFF_SECONDS,
    _SAVE_MAX_ATTEMPTS,
    _assert_note_is_fresh,
    _assert_note_unchanged,
    _discard_staged_sync,
    _make_note_dirs_checked,
    _new_staged_note_path,
    _observed_note_mtime,
    _publish_note_once,
    _read_note_text_sync,
    _reject_unportable_new_components,
    _snapshot_sync,
    _stage_note_text_checked,
    content_root,
    list_note_files,
    mutation_path_checked,
    read_note_text,
    reject_linked_trash,
    require_folder_path,
    require_note_path,
    resolve_write_dir_checked,
    safe_join,
    trash_dir_path,
    vault_mutation_path,
    vault_path,
)
from kiro_crew.apps.builtins.md_notebook.server_store import (
    MAX_AUTO_SYNC_MINS,
    MIN_AUTO_SYNC_MINS,
    _clone_root,
    _coerce_auto_sync_mins,
    _home,
    _is_app_clone,
    _write_pat_sync,
    _write_settings_sync,
    logger,
    read_pat,
    read_settings,
    read_vaults,
    synced_cleanly,
    write_vaults,
)
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.sel import sel

PORT = int(os.environ.get("PORT", 9137))


# Capability probe. The gateway keeps an app's backend alive across UI reloads,
# so a process running older code than the page would otherwise surface as
# confusing "no route" errors — the UI compares this list and warns instead.
FEATURES = [
    "createdAt",
    "attach",
    "changes",
    "saveGuard",
    "forget",
    "pat",
    "newNote",
    "move",
    "duplicate",
    "trash",
    "localOnly",
    "autoCommit",
    "trashOpen",
    "knowledge",
    "pickFolder",
    "settings",
    "autoSyncLoop",
]

# A write we made ourselves is ignored by change detection for this long, so
# saving a note does not report itself as an external edit.
SELF_WRITE_GRACE_SEC = 1.5
# Cached `gh auth token` lifetime.
GH_TOKEN_TTL_SEC = 300
GH_TIMEOUT_SEC = 5
# Largest request body accepted.
MAX_BODY_BYTES = 5_000_000

# Per-note-path save locks. The save guard reads the mtime and then writes; two
# concurrent saves of the same note (two tabs debounce-flushing from one
# baseMtime) would otherwise BOTH pass the check before either replaces the
# file, and the second write would silently destroy the first. Serializing the
# check-and-write per canonical path makes the second save observe the first's
# new mtime and surface an ESTALE conflict instead.
_save_locks: dict[str, LoopBoundLock] = {}


def _save_lock(path: str) -> LoopBoundLock:
    lock = _save_locks.get(path)
    if lock is None:
        lock = LoopBoundLock()
        _save_locks[path] = lock
    return lock


@contextlib.asynccontextmanager
async def _save_locks_for(*paths: str) -> "AsyncIterator[None]":
    """Hold the per-path save locks for every given path, acquired in a stable
    (sorted, de-duplicated) order so two callers locking the same pair — e.g. a
    move and a concurrent save/delete — can never deadlock."""
    ordered = sorted(set(paths))
    async with contextlib.AsyncExitStack() as stack:
        for p in ordered:
            await stack.enter_async_context(_save_lock(p))
        yield


# Per-vault write lock, shared by the note writers and by every `git_ops.sync`
# caller (the manual Sync, the autosave commit, the background syncer). A sync's
# `git merge` rewrites the working tree — it can replace a folder the writer
# just checked with a symlink the remote committed — so a writer's
# check-and-write and a merge must never overlap. Held by the writer for
# exactly one offloaded check-and-write call (`_create_unique`,
# `_make_note_dirs_checked`, `_stage_note_text_checked`, `_move_sync`,
# `_to_trash`), never across an `await` between them -- except the save's
# publish: a GUARDED save re-takes it around each freshness-check-and-publish
# and a TOKENLESS save holds it from staging through publication, because a
# merge landing between a check (or the unchecked first attempt) and the
# publish would be silently overwritten. The merge holds it for the whole
# sync.
#
# Lock order, everywhere: per-path save locks FIRST, this lock SECOND. The
# sync path takes only this one, so no cycle is possible.
_vault_write_locks: dict[str, LoopBoundLock] = {}


def vault_write_lock(local_path: str) -> LoopBoundLock:
    """The lock a vault's writers and its sync share. Keyed by ``localPath``."""
    lock = _vault_write_locks.get(local_path)
    if lock is None:
        lock = LoopBoundLock()
        _vault_write_locks[local_path] = lock
    return lock


# Serializes every vaults.json read-modify-write. Clone/attach/forget/knowledge
# each read the vault list, mutate it, and write it back; without this lock two
# concurrent mutations from separate tabs would both read the old list and the
# last writer would discard the other's change (lost update).
_vaults_lock = LoopBoundLock()

# Serializes every settings.json read-modify-write, for the same lost-update
# reason as _vaults_lock — but with three writers rather than two: the settings
# PUT, the manual sync handler, and the background loop, the last two both
# stamping `lastSync`. Without it a sync landing during a settings save would
# have its timestamp discarded by the save's atomic write, so the UI would report
# notes as never synced when they had just been pushed.
_settings_lock = LoopBoundLock()


# Upper bound on the Untitled-N search when creating a note.
MAX_UNTITLED = 1000
# How long after a fetch a HEAD-behind-the-tip reading may still be a `git pull`
# mid-merge rather than a vault ahead of its remote (see record_external_sync).
FETCH_SETTLE_MS = 60_000

# vault id -> {"index": SearchIndex, "backlinks": dict, "statuses": dict}
_caches: dict[str, dict[str, Any]] = {}
# vault id -> {"rev": int, "paths": set, "snapshot": dict[str, float]}
_watches: dict[str, dict[str, Any]] = {}
# absolute path -> monotonic timestamp of our own last write
_self_writes: dict[str, float] = {}


# ---------------------------------------------------------------------------
# App settings (auto-sync)
# ---------------------------------------------------------------------------


async def record_last_sync(vault_id: str, at: Optional[int] = None) -> int:
    """Stamp *vault_id*'s last conflict-free sync and return the stored stamp (epoch ms).

    The backend is the ONLY writer of ``lastSync``, for the manual, the
    background and the external-pull trigger, so a client cannot claim a sync
    happened and the paths cannot disagree about when one did.

    ``at`` is the moment the sync was observed when that is not now: a pull
    made outside the app is stamped with its fetch time. It is clamped to now,
    because it comes from an mtime (``.git`` is agent-writable, and a clock
    can run ahead) and a future stamp would read "just now" forever. A stamp
    never moves backwards, so a fetch time that predates a sync the app itself
    completed meanwhile leaves the newer stamp in place.
    """
    now = int(time.time() * 1000)
    stamp = now if at is None else min(at, now)
    async with _settings_lock:
        settings = await read_settings()
        stamp = max(stamp, settings["lastSync"].get(vault_id, -1))
        settings["lastSync"][vault_id] = stamp
        await asyncio.to_thread(_write_settings_sync, settings)
    return stamp


async def record_external_sync(vault: dict[str, Any]) -> None:
    """Count a pull made outside the app as this vault's last sync, if it was one.

    Auto sync can be unavailable (an SSH remote the sandbox cannot reach), so a
    user keeps the vault current with ``git pull`` by hand. Only the app's own
    sync paths stamp ``lastSync``, so the "Synced N ago" label kept reading
    from the last time the app pulled -- a week stale over a vault that was
    current. What is stamped here is the observation the app's own sync earns
    its stamp with: a fetch newer than the stamp, with origin still the trusted
    remote, that left HEAD at the checked-out branch's remote-tracking ref with
    nothing pending in the vault's scope. A fetch that left local commits
    unpushed, or edits uncommitted, is not a sync: those notes are still only
    on this machine, so the label must not say they are backed up.

    The tracking ref is what the vault knows of its remote, not proof of what
    the remote holds: a fetch of some other branch alone, on a vault already
    level with its tracking ref, earns a stamp at that fetch time too, and
    ``.git`` is agent-writable. The stamp is a label the user reads, never an
    authorization.

    Runs on every external-change poll, so the per-tick cost is one stat of
    the recorded git dir's ``FETCH_HEAD``. Each fetch is examined until it
    is decided, then never again, so a vault that is mid-edit, refuses git
    reads, or has long sat ahead of its remote does not re-run git every
    tick; the next fetch is examined afresh. One outcome stays open for a
    while: ``git pull`` writes ``FETCH_HEAD`` before it merges, so a poll that
    lands between the two sees HEAD behind the fetched tip. That mismatch is
    re-examined on later polls until the fetch is ``FETCH_SETTLE_MS`` old;
    after that it means a vault ahead of its remote and is final. The
    ``.git`` pointer is confirmed against the recorded git dir before any git
    command runs in the checkout. A fetch that earns a stamp is consumed only
    once the stamp is written: a failed settings write leaves it for the next
    poll rather than losing the pull.
    """
    if vault.get("localOnly"):
        return
    trusted_gitdir, trusted_remote = vault.get("gitDir"), vault.get("remoteUrl")
    if not trusted_gitdir or not trusted_remote:
        return
    real_gitdir = await asyncio.to_thread(os.path.realpath, trusted_gitdir)
    fetched = await asyncio.to_thread(git_ops.fetch_head_mtime_ms, real_gitdir)
    state = watch_state(vault)
    if fetched is None or fetched == state["fetchSeen"]:
        return
    if fetched <= (await read_settings())["lastSync"].get(vault["id"], -1):
        state["fetchSeen"] = fetched
        return
    path = vault["localPath"]
    try:
        if not await git_ops.gitdir_is_trusted(path, trusted_gitdir):
            state["fetchSeen"] = fetched
            return
        if not await git_ops.remote_is_trusted(path, trusted_remote):
            state["fetchSeen"] = fetched
            return
        if not await git_ops.head_is_fetched_tip(path):
            # Possibly a pull still merging; decided once the fetch has aged.
            if time.time() * 1000 - fetched > FETCH_SETTLE_MS:
                state["fetchSeen"] = fetched
            return
        if await git_ops.status(path, vault.get("subfolder")):
            state["fetchSeen"] = fetched
            return
    except git_ops.GitError as exc:
        state["fetchSeen"] = fetched
        logger.debug("md-notebook: external sync check failed for %s: %s", path, exc)
        return
    try:
        await record_last_sync(vault["id"], at=fetched)
    except OSError as exc:
        # Not consumed: the stamp did not land, so the next poll tries again.
        logger.warning("md-notebook: could not record external sync for %s: %s", path, exc)
        return
    state["fetchSeen"] = fetched


# ---------------------------------------------------------------------------
# GitHub auth: stored PAT first, else the user's `gh` CLI login
# ---------------------------------------------------------------------------

_gh_cache: dict[str, Any] = {"value": None, "at": 0.0}


def _gh_token_sync() -> Optional[str]:
    """Mint a token from the gh CLI login. Never persisted to disk."""
    if time.monotonic() - _gh_cache["at"] < GH_TOKEN_TTL_SEC:
        return _gh_cache["value"]
    value: Optional[str] = None
    gh = _find_gh()
    if gh is not None:
        try:
            proc = subprocess.run(
                [gh, "auth", "token"],
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_SEC,
                check=False,
                env=_trusted_env(),
            )
            if proc.returncode == 0:
                value = proc.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            value = None
    _gh_cache["value"] = value
    _gh_cache["at"] = time.monotonic()
    return value


async def gh_token() -> Optional[str]:
    return await asyncio.to_thread(_gh_token_sync)


async def resolve_auth() -> Optional[str]:
    """Auth for git operations: an explicit stored PAT wins, else gh CLI."""
    return await read_pat() or await gh_token()


# ---------------------------------------------------------------------------
# Self-write marks
# ---------------------------------------------------------------------------


def mark_self_write(abs_path: Path) -> None:
    """Record our own write so change detection does not report it."""
    _self_writes[str(abs_path)] = time.monotonic()
    if len(_self_writes) > 200:
        cutoff = time.monotonic() - SELF_WRITE_GRACE_SEC
        for key in [k for k, t in _self_writes.items() if t < cutoff]:
            del _self_writes[key]


def _is_self_write(abs_path: str) -> bool:
    at = _self_writes.get(abs_path)
    return at is not None and time.monotonic() - at < SELF_WRITE_GRACE_SEC


# ---------------------------------------------------------------------------
# External-change detection
# ---------------------------------------------------------------------------
#
# The Node original attached a recursive fs.watch. Here detection is an mtime
# snapshot compared on demand, which needs no extra dependency, no background
# thread, and is deterministic to test. The UI is the only consumer and it
# polls, so the observable behaviour is the same: a poll reports what changed
# since the caller's revision.


def watch_state(vault: dict[str, Any]) -> dict[str, Any]:
    state = _watches.get(vault["id"])
    if state is None:
        state = {"rev": 0, "paths": set(), "snapshot": None, "fetchSeen": None}
        _watches[vault["id"]] = state
    return state


async def scan_changes(vault: dict[str, Any]) -> dict[str, Any]:
    """Diff the tree against the last snapshot, bumping the revision on change."""
    state = watch_state(vault)
    root = await vault_path(vault)
    current = await asyncio.to_thread(_snapshot_sync, root)
    previous = state["snapshot"]
    if previous is None:
        state["snapshot"] = current
        return state
    changed: set[str] = set()
    for rel, mtime in current.items():
        if previous.get(rel) != mtime and not _is_self_write(str(root / rel)):
            changed.add(rel)
    for rel in previous:
        if rel not in current:
            changed.add(rel)
    state["snapshot"] = current
    if changed:
        state["paths"].update(changed)
        state["rev"] += 1
        # The index and backlinks are now stale with respect to disk.
        _caches.pop(vault["id"], None)
    return state


# ---------------------------------------------------------------------------
# Search index / backlinks cache
# ---------------------------------------------------------------------------


async def rebuild_cache(vault: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the search index and backlink map for a vault from disk."""
    root = await vault_path(vault)
    paths = await list_note_files(root)
    contents: dict[str, str] = {}
    for rel in paths:
        # Read through the central sensitive-path gate instead of opening the
        # walked path directly. `safe_join` protects a note fetched BY PATH, but
        # this walk never goes through it, so a `.md` symlink inside the vault
        # aimed at a private key or credential store elsewhere on disk would
        # have its contents indexed and surface in search results.
        # `safe_read_file_bytes` canonicalizes via realpath, refuses a sensitive
        # resolved target, and opens with O_NOFOLLOW.
        text = await read_note_text(root / rel)
        if text is None:
            continue  # refused by the gate, or unreadable
        contents[rel] = text
    docs = [
        # The boosted field carries the note's DISPLAY name (its filename), so a
        # search hit in the rail reads the same as the same note in the tree. A
        # frontmatter title stays findable through `content`, which includes the
        # frontmatter block, just without the title boost.
        notes_mod.SearchDoc(path=p, title=notes_mod.note_basename(p), content=c)
        for p, c in contents.items()
    ]
    cache = {
        "index": notes_mod.SearchIndex(docs),
        "backlinks": notes_mod.build_backlinks(contents),
        "statuses": {},
    }
    _caches[vault["id"]] = cache
    return cache


async def get_cache(vault: dict[str, Any]) -> dict[str, Any]:
    cache = _caches.get(vault["id"])
    return cache if cache is not None else await rebuild_cache(vault)


async def refresh_statuses(vault: dict[str, Any], cache: dict[str, Any]) -> None:
    """Mark notes with uncommitted git changes as pending."""
    try:
        changes = await git_ops.status(vault["localPath"])
    except git_ops.GitError as exc:
        logger.debug("md-notebook: status failed for %s: %s", vault["localPath"], exc)
        cache["statuses"] = {}
        return
    cache["statuses"] = {c.path: "pending" for c in changes}


async def note_listing(vault: dict[str, Any]) -> list[dict[str, Any]]:
    """Every note with its display name, timestamps and sync status."""
    root = await vault_path(vault)
    cache = await get_cache(vault)
    await refresh_statuses(vault, cache)
    paths = await list_note_files(root)
    prefix = f"{vault['subfolder']}/" if vault.get("subfolder") else ""
    listing: list[dict[str, Any]] = []
    for rel in paths:
        abs_path = root / rel
        try:
            st = await asyncio.to_thread(os.stat, abs_path)
        except OSError:
            continue
        # Read only so the sensitive-path gate can filter: a `.md` symlink aimed at
        # a credential store elsewhere on disk must not reach the listing at all.
        content = await read_note_text(abs_path)
        if content is None:
            continue  # refused by the gate, or unreadable
        # birthtime is real on APFS/HFS+; elsewhere fall back to ctime then mtime.
        created = getattr(st, "st_birthtime", None) or st.st_ctime or st.st_mtime
        listing.append(
            {
                "path": rel,
                # The filename, not a frontmatter `title`: this label is the note's
                # identity everywhere the user acts on it -- the rail row, the editor
                # header, the name-sort key, the rename field (which renames the FILE)
                # and the delete confirmation -- and it matches Obsidian, whose file
                # explorer lists filenames too. A frontmatter `title` is still a
                # wikilink target; `note_title` is the key for that.
                "title": notes_mod.note_basename(rel),
                "modifiedAt": st.st_mtime * 1000,
                "createdAt": created * 1000,
                "syncStatus": cache["statuses"].get(prefix + rel, "synced"),
            }
        )
    return listing


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "features": FEATURES})


async def api_vaults(request: web.Request) -> web.Response:
    vaults = []
    for v in await read_vaults():
        # `external` = attached in place rather than cloned by this app.
        # Computed on read, never persisted.
        cloned = await asyncio.to_thread(_is_app_clone, str(v["localPath"]))
        vaults.append({**v, "external": not cloned})
    return web.json_response(
        {
            "vaults": vaults,
            "hasPat": bool(await read_pat()),
            "hasGhAuth": bool(await gh_token()),
        }
    )


async def api_vault_clone(request: web.Request) -> web.Response:
    body = await json_body(request)
    if not body.get("url"):
        raise ApiError("url is required", 400, code="url_required")
    # Use a submitted token TRANSIENTLY for this clone; persist it only after the
    # clone succeeds. Writing it up front would let a failed clone (bad URL or
    # bad token) overwrite a valid stored PAT and break every existing
    # vault's auth.
    submitted_pat = str(body["pat"]) if body.get("pat") else None
    pat = submitted_pat or await resolve_auth()
    # uuid, not a millisecond timestamp: two clones in the same millisecond
    # would otherwise collide on the same id and clone directory.
    vault_id = f"v{uuid.uuid4().hex}"
    clone_dir = _clone_root() / vault_id
    sub = body.get("subfolder") or None
    # Validate the subfolder BEFORE the network clone: safe_join is pure
    # realpath math and is valid on a not-yet-existing path, so an escaping
    # subfolder (e.g. "/etc") is rejected here rather than after clone_vault has
    # already written a checkout to disk that nothing would ever clean up.
    if sub:
        await asyncio.to_thread(safe_join, clone_dir, sub)
    vault = await git_ops.clone_vault(
        url=str(body["url"]),
        dir_=str(clone_dir),
        vault_id=vault_id,
        branch=body.get("branch") or "main",
        subfolder=sub,
        name=body.get("name") or None,
        pat=pat,
    )
    # Re-validate against the real checkout (catches a subfolder that is a
    # symlink inside the cloned content); on failure remove the checkout so a
    # rejected clone leaves no orphaned directory behind.
    try:
        await vault_path(vault)
    except ApiError:
        await asyncio.to_thread(platform_compat.rmtree_force, str(clone_dir))
        raise
    # The clone succeeded with this token — now it is safe to persist it.
    if submitted_pat:
        await asyncio.to_thread(_write_pat_sync, submitted_pat)
    async with _vaults_lock:
        vaults = await read_vaults()
        vaults.append(
            {**vault, "knowledge": bool(body.get("knowledge")), "knowledgeSourceId": None}
        )
        await write_vaults(vaults)
    await rebuild_cache(vault)
    return web.json_response({"vault": vault})


async def api_vault_attach(request: web.Request) -> web.Response:
    body = await json_body(request)
    if not body.get("path"):
        raise ApiError("path is required", 400, code="path_required")
    raw = str(body["path"])
    # Expand a leading bare `~` without regex: `str(Path.home())` on Windows is
    # `C:\Users\...`, and using it as an `re.sub` replacement made `\U` a bad
    # escape. A plain prefix check preserves the "only a leading ~" intent and
    # is cross-platform (~/ on POSIX, ~\ on Windows).
    if raw == "~" or raw.startswith(("~/", "~\\")):
        raw = str(Path.home()) + raw[1:]
    # resolve() is synchronous filesystem I/O — off the loop so an unavailable
    # NFS/autofs mount can't hang every Notes request and the health check.
    dir_ = await asyncio.to_thread(lambda: str(Path(raw).resolve()))
    # A vault's sync runs `git add -A` over its whole content root, which stages
    # files WITHOUT passing through the per-file sensitive-path gate. So refuse
    # to attach a folder that resolves into a protected location (~/.ssh,
    # ~/.aws, the crew data home, ...) — otherwise a checkout planted under a
    # credential store could be committed and pushed.
    if await asyncio.to_thread(hooks.is_sensitive_path, dir_):
        raise ApiError("that folder is a protected location", 403, code="sensitive_path")
    # ...and the REVERSE direction: a folder that CONTAINS a protected location
    # must be refused too. Attaching the home directory (or any ancestor of
    # ~/.ssh, ~/.aws, the crew data home, ...) passes the check above — home is
    # not itself sensitive — yet `git add -A` from that root would stage and
    # push the credential stores wholesale. List-based prefix check, no
    # filesystem walk, so attaching a huge tree stays cheap.
    if await asyncio.to_thread(security.path_contains_sensitive, dir_):
        raise ApiError("that folder contains a protected location", 403, code="sensitive_path")
    async with _vaults_lock:
        vaults = await read_vaults()
        if any(v["localPath"] == dir_ for v in vaults):
            raise ApiError(
                "that folder is already attached as a vault", 409, code="folder_already_attached"
            )
        vault = await git_ops.attach_vault(
            dir_=dir_,
            vault_id=f"v{uuid.uuid4().hex}",
            subfolder=body.get("subfolder") or None,
            name=body.get("name") or None,
        )
        # Validate the subfolder resolves inside the folder BEFORE persisting, so
        # an escaping subfolder is rejected up front rather than saved as a
        # descriptor rebuild_cache later refuses.
        await vault_path(vault)
        # The scoped content root (with a subfolder) is what `add -A` stages, so
        # it must also clear the sensitive-path floor.
        croot = await asyncio.to_thread(lambda: str(content_root(vault).resolve()))
        if await asyncio.to_thread(hooks.is_sensitive_path, croot):
            raise ApiError("that folder is a protected location", 403, code="sensitive_path")
        vaults.append(
            {**vault, "knowledge": bool(body.get("knowledge")), "knowledgeSourceId": None}
        )
        await write_vaults(vaults)
    await rebuild_cache(vault)
    await scan_changes(vault)
    return web.json_response({"vault": vault})


async def api_vault_forget(request: web.Request) -> web.Response:
    """Remove a vault descriptor. FILES ARE NEVER DELETED."""
    vault_id = request.query.get("vault")
    if not vault_id:
        raise ApiError("vault is required", 400, code="vault_required")
    async with _vaults_lock:
        vaults = await read_vaults()
        target = next((v for v in vaults if v["id"] == vault_id), None)
        if target is None:
            raise ApiError("no such vault", 404, code="no_such_vault")
        await write_vaults([v for v in vaults if v["id"] != vault_id])
    _caches.pop(vault_id, None)
    _watches.pop(vault_id, None)
    return web.json_response({"ok": True, "localPath": target["localPath"]})


async def api_vault_knowledge(request: web.Request) -> web.Response:
    """Persist a vault's 'sync to Kiro Crew knowledge' preference.

    Registration with the Knowledge library happens in the UI, which runs
    inside the dashboard and carries the user's session. This only records the
    resulting state so it survives reloads.
    """
    body = await json_body(request)
    vault_id = body.get("vault")
    if not vault_id:
        raise ApiError("vault is required", 400, code="vault_required")
    async with _vaults_lock:
        vaults = await read_vaults()
        target = next((v for v in vaults if v["id"] == vault_id), None)
        if target is None:
            raise ApiError("no such vault", 404, code="no_such_vault")
        enabled = bool(body.get("knowledge"))
        target["knowledge"] = enabled
        target["knowledgeSourceId"] = (
            body.get("sourceId") or target.get("knowledgeSourceId") or None if enabled else None
        )
        await write_vaults(vaults)
    return web.json_response({"vault": target})


async def api_settings_get(request: web.Request) -> web.Response:
    return web.json_response({"settings": await read_settings()})


async def api_settings_put(request: web.Request) -> web.Response:
    """Merge-patch the app's own settings. Accepts only ``autoSync`` and
    ``autoSyncMins``.

    Two phases with nothing interleaved: every field is validated into a local
    before any field is written, so a request carrying one bad value changes
    NOTHING. That matters most for the pair here — a request that turned
    ``autoSync`` on and then 400'd on a malformed interval would leave unattended
    pushing enabled from a request the user was told had failed.

    ``lastSync`` is server-owned and REFUSED rather than dropped: a client sending
    one is trying to claim a sync happened, and ignoring it silently would let the
    UI show a backup time nothing earned.

    Unknown keys are dropped, so a stale or hostile client cannot grow the file
    with arbitrary content.
    """
    body = await json_body(request)
    if "lastSync" in body:
        raise ApiError(
            "lastSync is recorded by the server and cannot be set by a client",
            400,
            code="last_sync_read_only",
        )

    # PHASE 1 — validate into locals. Only `raise ApiError` below this line, never
    # a write.
    auto_sync: Optional[bool] = None
    if "autoSync" in body:
        if not isinstance(body["autoSync"], bool):
            raise ApiError("autoSync must be a boolean", 400, code="invalid_auto_sync")
        auto_sync = body["autoSync"]

    auto_sync_mins: Optional[int] = None
    if "autoSyncMins" in body:
        auto_sync_mins = _coerce_auto_sync_mins(body["autoSyncMins"])
        if auto_sync_mins is None:
            raise ApiError(
                "autoSyncMins must be a whole number of minutes between "
                f"{MIN_AUTO_SYNC_MINS} and {MAX_AUTO_SYNC_MINS}",
                400,
                code="invalid_auto_sync_mins",
            )

    # PHASE 2 — apply. Nothing here can fail validation.
    async with _settings_lock:
        settings = await read_settings()
        prev_auto_sync = settings["autoSync"]
        # Writes apply in the order the server receives them, serialized by the
        # settings lock. This lock IS the ordering authority: the client sends one
        # write at a time (single-flight) carrying the full desired state, so a
        # single tab cannot race itself, and across tabs the last write the server
        # accepts wins — the only ordering a server can establish without a shared
        # client clock. A revocation therefore always applies (nothing is ever
        # dropped as "stale"), so auto-sync can never stay authorized against a
        # user who turned it off.
        # Enabling authorizes unattended `git push`; audit that BEFORE persisting
        # it and fail closed (critical=True) so push is never authorized without a
        # record. Run the SEL write in a thread — it flushes synchronously to disk,
        # which must not block the event loop.
        if auto_sync is True and prev_auto_sync is False:
            await asyncio.to_thread(
                sel().log_api_access,
                caller="md-notebook",
                operation="md_notebook.settings.autoSync",
                outcome="enabled",
                resources=f"autoSyncMins={auto_sync_mins if auto_sync_mins is not None else settings['autoSyncMins']}",
                critical=True,
            )
        if auto_sync is not None:
            settings["autoSync"] = auto_sync
        if auto_sync_mins is not None:
            settings["autoSyncMins"] = auto_sync_mins
        await asyncio.to_thread(_write_settings_sync, settings)
        if auto_sync is False and prev_auto_sync is True:
            await asyncio.to_thread(
                sel().log_api_access,
                caller="md-notebook",
                operation="md_notebook.settings.autoSync",
                outcome="disabled",
                resources=f"autoSyncMins={settings['autoSyncMins']}",
            )
    return web.json_response({"settings": settings})


async def api_pat(request: web.Request) -> web.Response:
    """Set or clear the stored token: an empty value clears it."""
    body = await json_body(request)
    pat = body.get("pat")
    if pat:
        await asyncio.to_thread(_write_pat_sync, str(pat))
    else:
        # Clear by atomically REPLACING with an empty file, never by unlink:
        # the empty file is the reader's absent-equivalent (_read_pat_sync maps
        # "" to None), while removing the inode deletes the sandbox mask's
        # mount target — a clear landing between the launcher's materialize and
        # mount steps would leave that namespace maskless, and a later PAT save
        # would be readable inside it. Routed through the same staged writer as
        # the save so the guards match.
        await asyncio.to_thread(_write_pat_sync, "")
    return web.json_response(
        {"hasPat": bool(await read_pat()), "hasGhAuth": bool(await gh_token())}
    )


async def api_changes(request: web.Request) -> web.Response:
    """External-change poll: the current revision plus paths touched since.

    A pull made outside the app is an external change too: the poll is where
    :func:`record_external_sync` stamps it. The stamp reaches the page through
    the settings it already refetches, so the reply itself is unchanged.
    """
    vault = await require_vault(request)
    state = await scan_changes(vault)
    await record_external_sync(vault)
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0
    changed = [] if since == state["rev"] else sorted(state["paths"])
    if since != state["rev"]:
        state["paths"].clear()
    return web.json_response({"rev": state["rev"], "changed": changed, "watching": True})


async def api_notes(request: web.Request) -> web.Response:
    vault = await require_vault(request)
    await scan_changes(vault)
    return web.json_response({"notes": await note_listing(vault)})


async def api_note_read(request: web.Request) -> web.Response:
    vault = await require_vault(request)
    rel = require_note_path(request.query.get("path"))
    abs_path = await vault_path(vault, rel)
    try:
        # Snapshot the mtime BEFORE reading the content. If an external atomic
        # save lands between the two calls, stat-then-read yields (old mtime,
        # newer content) — the next save then mismatches and safely raises a
        # conflict. Read-then-stat would hand back (stale content, fresh mtime),
        # and the next save would pass the guard and silently clobber the
        # external edit.
        st = await asyncio.to_thread(os.stat, abs_path)
        # `safe_join` proves the path stays inside the vault, but says nothing
        # about what the vault itself contains: a vault can be attached to any
        # folder the user picks, so containment alone would happily serve a
        # private key that lives under it. The gate is what refuses that.
        content = await read_note_text(abs_path)
        if content is None:
            raise ApiError(f"no such note: {rel}", 404, code="no_such_note")
    except FileNotFoundError as exc:
        raise ApiError(f"no such note: {rel}", 404, code="no_such_note") from exc
    cache = await get_cache(vault)
    return web.json_response(
        {
            "path": rel,
            "content": content,
            # Snapshot token for the save guard: a PUT is refused if the file
            # changed on disk since this read.
            "mtime": st.st_mtime * 1000,
            "meta": notes_mod.parse_note(rel, content),
            "backlinks": cache["backlinks"].get(rel, []),
        }
    )


async def _save_note_contents(
    vault: dict[str, Any], abs_path: Path, rel: str, content: str, base_mtime: object
) -> None:
    """Publish *content*, retrying a contended rename without losing a write.

    The rename can fail on Windows with `PermissionError` while another handle is
    open on the note -- routine for a vault the user also has open in an editor,
    and for the search indexer and AV scanner. Losing the save there is the user
    losing what they just typed, so the transient is retried.

    What is retried is the CHECK-AND-WRITE, never the rename alone. Retrying
    inside the rename would stretch the gap between the freshness check and the
    publish across the whole backoff, and an edit completing in that gap would be
    silently overwritten by the next attempt -- exactly what the `baseMtime`
    guard exists to prevent.

    **The lock policy depends on whether this save carries a freshness token**,
    because that is what decides whether a writer in the gap can be DETECTED:

    * ``baseMtime`` supplied -- the lock is released across the backoff. Another
      writer landing in the gap is not a problem to exclude: the next attempt
      re-runs the check, sees it, and answers 409. Releasing keeps a contended
      save from blocking an external-conflict resolution behind it.

    * no ``baseMtime`` (the API allows it, and the editor omits it when it has
      not read an mtime) -- ``_assert_note_is_fresh`` is a no-op, so a writer in
      the gap is INVISIBLE to every later attempt. Releasing the lock there let a
      contended save resurrect itself over a later save that had already been
      acknowledged with 200. The lock is therefore held across the backoff, which
      preserves exactly the serialization the un-retried code had: a same-note
      writer waits (at most the retry budget, ~450ms) rather than being
      overwritten afterwards.

      The lock orders API writers and nothing else, so it is only half the
      answer. An editor writing the file directly -- Obsidian, ``git pull``, a
      script -- never takes it, and republishing over that edit is undetectable
      after the fact. So the tokenless path derives its OWN baseline
      (``_observed_note_mtime`` before the first attempt) and revalidates
      against it before every RETRY: the same 409 ESTALE the guarded path
      answers, from a baseline the server sampled instead of one the client
      supplied. The first attempt never consults it -- overwriting the sampled
      state IS the save.

    Holding it is a coroutine suspension, not a blocked event loop, and it is
    per-note.

    Budget mirrors `atomic_write.replace_with_retry`, whose window this is.
    """
    # ONE temp for the whole transaction, on either path. Staged before the
    # first attempt and republished by every retry, so success consumes it and
    # any exit path has exactly one file to discard. It carries
    # `git_ops.staged_temp_name`'s shape for the whole of its life, so a Sync
    # running during the backoff cannot stage it.
    #
    # WHERE it is staged differs, because the two paths serialize differently.
    if isinstance(base_mtime, (int, float)):
        # Guarded: the freshness check is what orders these saves, so staging
        # outside the lock cannot reorder anything -- every attempt revalidates
        # under the lock before it may publish.
        #
        # It runs BEFORE the directory is created, though. A save whose note and
        # parent were deleted externally is already destined for 409, and
        # the `mkdir` would put the parent back on the way to refusing: a request
        # that changes nothing should not resurrect a directory the user removed.
        # This costs one extra stat on the happy path and weakens nothing -- the
        # per-attempt check under the lock is still the one that authorizes a
        # publish.
        await _assert_note_is_fresh(abs_path, rel, base_mtime)
        async with vault_write_lock(vault["localPath"]):
            await asyncio.to_thread(_make_note_dirs_checked, vault, rel)
        tmp = _new_staged_note_path(abs_path)
        # Register BEFORE staging: opening the temp is the first instant Sync
        # can see it. Keep ownership through failure cleanup as well as publish,
        # so there is no visible gap at either end of the transaction.
        with git_ops.inflight_temp(tmp):
            try:
                async with vault_write_lock(vault["localPath"]):
                    await asyncio.to_thread(_stage_note_text_checked, vault, rel, tmp, content)
                for attempt in range(_SAVE_MAX_ATTEMPTS):
                    # Both locks, per attempt: the per-note lock orders API
                    # writers, and the vault lock keeps a sync's merge out of
                    # the gap between the freshness check and the publish --
                    # a merge there would pass the check (it ran first) and be
                    # replaced by the publish with a 200. Same order as
                    # everywhere else: the per-note lock first.
                    async with _save_lock(str(abs_path)), vault_write_lock(vault["localPath"]):
                        await _assert_note_is_fresh(abs_path, rel, base_mtime)
                        if await _publish_note_once(abs_path, tmp, attempt):
                            return
                    # Outside both locks: a writer arriving here -- an API save,
                    # a merge -- is one the next attempt's check is meant to
                    # notice, so blocking it would hide the conflict.
                    await asyncio.sleep(_SAVE_BACKOFF_SECONDS)
                return
            except BaseException:
                await asyncio.to_thread(_discard_staged_sync, tmp)
                raise

    # Tokenless: nothing can detect an interleaved writer, so ORDER is the only
    # guarantee left -- and order has to cover the whole stage-and-publish
    # transaction, not just the publish. Staging is the slow half (it writes the
    # entire note), so staging outside the lock let a large save be overtaken:
    # a later save could take the lock, publish, and answer 200 while the earlier
    # one was still writing its temp, and the earlier one then published over it.
    # Both answered 200; the later edit was gone.
    #
    # The vault write lock is held for the SAME span, and for the same reason.
    # A sync's merge is a writer this path cannot detect either: the first
    # attempt publishes without a check (overwriting the sampled state IS the
    # save), so a merge landing between staging and the publish -- a sync that
    # queued behind the staging call's lock, say -- would be overwritten with a
    # 200. The guarded path can release the vault lock across its backoff because
    # it re-takes it with the per-note lock around every check-and-publish, and
    # `_assert_note_is_fresh` then sees a merged note as a foreign write and
    # answers 409; here nothing would, so the merge has to wait for the publish.
    # Order: the per-note lock first, the vault lock second, as everywhere else.
    async with _save_lock(str(abs_path)), vault_write_lock(vault["localPath"]):
        await asyncio.to_thread(_make_note_dirs_checked, vault, rel)
        tmp = _new_staged_note_path(abs_path)
        with git_ops.inflight_temp(tmp):
            try:
                # Registration precedes the worker-thread open and remains until
                # cleanup completes, so Sync never sees an unowned temp even if
                # staging itself is slow or fails partway.
                await asyncio.to_thread(_stage_note_text_checked, vault, rel, tmp, content)
                # The server's OWN baseline, standing in for the absent
                # `baseMtime`: the state this save is about to overwrite.
                # Sampled after staging (which writes the temp, never the note)
                # and before the first attempt, so it describes exactly what
                # attempt 1 would replace.
                observed = await _observed_note_mtime(abs_path)
                for attempt in range(_SAVE_MAX_ATTEMPTS):
                    if attempt:
                        # A RETRY, so the backoff has already elapsed. The lock
                        # kept other API writers out of it, but an external
                        # editor is not subject to the lock -- and republishing
                        # over its edit is the one thing this path cannot
                        # detect after the fact.
                        await _assert_note_unchanged(abs_path, rel, observed)
                    if await _publish_note_once(abs_path, tmp, attempt):
                        return
                    # Still inside both locks: with no token there is nothing
                    # to detect an interleaved API write OR merge with, so
                    # ordering has to be preserved.
                    await asyncio.sleep(_SAVE_BACKOFF_SECONDS)
            except BaseException:
                # Every non-success exit -- staging failure, an exhausted
                # budget, a POSIX refusal, cancellation -- discards the one temp
                # this save owns BEFORE its registration is released. A 200
                # never reaches here, because its rename consumed the temp.
                await asyncio.to_thread(_discard_staged_sync, tmp)
                raise


async def api_note_save(request: web.Request) -> web.Response:
    vault = await require_vault(request)
    require_writable(vault)
    body = await json_body(request)
    rel = require_note_path(body.get("path"))
    content = body.get("content")
    if not isinstance(content, str):
        raise ApiError("path and content required", 400, code="path_and_content_required")
    # Use the LEXICAL in-vault path (not safe_join's realpath): a note that is a
    # symlink — e.g. a cloned `alias.md -> .git/config` — must not have its save
    # follow the link and overwrite the target. Reject a final-component symlink
    # outright; a note is a real file, never an alias to another path.
    abs_path = await vault_mutation_path(vault, rel)
    # islink OR (Windows) junction — a junction is a directory reparse point that
    # islink() misses, so check both to keep the guard from being POSIX-only.
    if await asyncio.to_thread(platform_compat.is_link_or_junction, abs_path):
        raise ApiError("cannot save through a symlink", 400, code="note_is_symlink")
    # The folder components the save creates through are walked inside
    # `_save_note_contents`, in the thread that does each create: a linked one
    # resolves inside the vault and passes containment, see the helper.
    root = await vault_path(vault)
    # Components this save would CREATE are held to the portability rule on
    # every platform; existing ones stay saveable (the rename escape hatch).
    await asyncio.to_thread(_reject_unportable_new_components, root, rel)
    base_mtime = body.get("baseMtime")
    await _save_note_contents(vault, abs_path, rel, content, base_mtime)
    mark_self_write(abs_path)
    cache = await get_cache(vault)
    cache["index"].update(
        notes_mod.SearchDoc(path=rel, title=notes_mod.note_basename(rel), content=content)
    )
    # Backlinks are a whole-vault relation, so recompute them from disk.
    contents = {}
    for p in await list_note_files(root):
        text = await read_note_text(root / p)
        if text is not None:
            contents[p] = text
    cache["backlinks"] = notes_mod.build_backlinks(contents)
    st = await asyncio.to_thread(os.stat, abs_path)
    return web.json_response({"ok": True, "mtime": st.st_mtime * 1000})


async def api_note_delete(request: web.Request) -> web.Response:
    """Move a note into the vault's local trash. Nothing is unlinked.

    A hard unlink is unrecoverable for a note the user never committed, which is
    the common case for something written today and deleted an hour later. Moving
    it into ``.trash`` inside the vault keeps it recoverable from Finder without
    depending on git, and ``git_ops`` excludes that folder from every status read
    and from staging, so a deleted note is never pushed to the remote.
    """
    vault = await require_vault(request)
    require_writable(vault)
    rel = require_note_path(request.query.get("path"))
    abs_path = await vault_mutation_path(vault, rel)
    name = notes_mod.note_basename(rel.replace("\\", "/"))

    def _to_trash() -> tuple[str, Path, Path]:
        # Derive the lexical source under the lock, after rejecting linked
        # folders, so a scope swap cannot retain a stale resolved target.
        abs_path = mutation_path_checked(vault, rel)
        trash_dir = content_root(vault) / git_ops.TRASH_DIR
        reject_linked_trash(trash_dir)
        trash_dir.mkdir(parents=True, exist_ok=True)
        for n in range(1, MAX_UNTITLED):
            candidate = trash_dir / (f"{name}.md" if n == 1 else f"{name} {n}.md")
            try:
                # Reserve the name atomically before moving onto it. A bare
                # `exists()` check would be a TOCTOU window, and `os.replace`
                # silently OVERWRITES — two deletes of same-named notes from
                # different folders would leave only the second in the trash.
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            os.close(fd)
            try:
                # `replace`, not `rename`: the reserved placeholder is in the
                # way. Neither follows a final-component symlink, so an in-vault
                # alias is moved as the link it is.
                os.replace(abs_path, candidate)
            except OSError:
                # The source was gone (or unmovable) — do not leave the empty
                # placeholder behind as a phantom trash entry.
                with contextlib.suppress(OSError):
                    os.unlink(candidate)
                raise
            return candidate.name, abs_path, candidate
        raise ApiError("could not find a free name in the trash", 409, code="no_free_note_name")

    # Share the note's save lock: a concurrent debounced save could otherwise
    # recreate the note right after it is moved away. Then the vault write lock,
    # so no sync's merge runs between the `.trash` check and the move into it.
    async with _save_lock(str(abs_path)), vault_write_lock(vault["localPath"]):
        try:
            trashed, abs_path, trash_dest = await asyncio.to_thread(_to_trash)
        except FileNotFoundError as exc:
            raise ApiError(f"no such note: {rel}", 404, code="no_such_note") from exc
    mark_self_write(abs_path)
    # Mark the destination the worker actually computed under the lock, rather
    # than re-deriving it here with `content_root` — two `os.path.realpath`
    # syscalls that must not run on the event loop for a network-mounted vault.
    mark_self_write(trash_dest)
    # Rebuild rather than drop one index entry: the deleted note's own
    # [[wikilinks]] disappear with it, so every note it pointed at keeps a
    # backlink to a missing note until the next rebuild.
    await rebuild_cache(vault)
    return web.json_response({"ok": True, "trashed": f"{git_ops.TRASH_DIR}/{trashed}"})


async def api_note_new(request: web.Request) -> web.Response:
    """Create an empty note with a guaranteed-free name.

    Naming is decided here rather than in the UI so two quick clicks cannot
    collide, or silently overwrite a file the UI's cached listing did not know
    about — the file is created with an exclusive flag.
    """
    vault = await require_vault(request)
    require_writable(vault)
    body = await json_body(request)
    folder = require_folder_path(body.get("folder"), for_new=True)

    def _create_unique() -> tuple[str, str, Path]:
        """Find a free name and create the file, in one thread.

        Done in a single offloaded step for four reasons: the search is up to
        MAX_UNTITLED stat calls, which must not run on the event loop; testing
        then creating in one place leaves no window for a second request to
        take the name between the two (the exclusive-create flag is what
        actually decides it); the symlink walk runs here, right before the
        `mkdir`, so nothing awaits between the check and the create; and the
        destination directory is RESOLVED here, after that walk and under the
        vault lock, so a merge cannot leave the write aimed at a stale
        link target resolved before the lock (see `resolve_write_dir_checked`).
        """
        directory = resolve_write_dir_checked(vault, folder or "")
        directory.mkdir(parents=True, exist_ok=True)
        for n in range(1, MAX_UNTITLED):
            name = "Untitled.md" if n == 1 else f"Untitled {n}.md"
            try:
                fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            # Created EMPTY. Seeding `# <name>` gave every new note an H1 the user
            # then had to delete: the filename already IS the title (`note_title`
            # falls back to the basename), so the heading was a duplicate that
            # rendered as a headline and did not track a later rename.
            os.close(fd)
            return name, "", directory
        raise ApiError("could not find a free note name", 409, code="no_free_note_name")

    async with vault_write_lock(vault["localPath"]):
        name, content, directory = await asyncio.to_thread(_create_unique)
    rel = f"{folder}/{name}" if folder else name
    mark_self_write(directory / name)
    cache = await get_cache(vault)
    cache["index"].add(
        notes_mod.SearchDoc(path=rel, title=re.sub(r"\.md$", "", name), content=content)
    )
    return web.json_response({"path": rel})


async def api_note_duplicate(request: web.Request) -> web.Response:
    """Copy a note beside itself under a guaranteed-free name.

    Server-side for the same reason ``/api/note/new`` is: the free name is found
    and the file created in one exclusive step, so two quick clicks cannot
    collide or overwrite a file the UI's cached listing did not know about.
    """
    vault = await require_vault(request)
    require_writable(vault)
    body = await json_body(request)
    src_rel = require_note_path(body.get("path"))
    # Normalize the separator BEFORE deriving the name and folder. `safe_join`
    # accepts a Windows-style "dir\note.md" (it normalizes internally to validate
    # containment), but `rfind("/")` would then find no separator and
    # `note_basename` would keep the backslash — so the copy landed at the vault
    # ROOT named "dir\note copy.md" on POSIX, while pathlib reads that same name
    # as a subdirectory on Windows. One request, two different destinations.
    rel_posix = src_rel.replace("\\", "/")
    stem = notes_mod.note_basename(rel_posix)
    folder = rel_posix[: rel_posix.rfind("/")] if "/" in rel_posix else ""

    def _create_unique() -> tuple[str, Path]:
        # The copy is created through the source's folder components, so the
        # same walk, in the same thread as the create (see the helper). The
        # destination directory is resolved here too — after the walk and under
        # the vault lock — so the copy can only ever land beside its source
        # inside the vault, never at a link target resolved before the lock.
        directory = resolve_write_dir_checked(vault, folder)
        # Resolve and read the source in this locked worker too: content read
        # through a transient folder link must not become a tracked copy.
        src = safe_join(content_root(vault), src_rel)
        content = _read_note_text_sync(src)
        if content is None:
            raise ApiError(f"no such note: {src_rel}", 404, code="no_such_note")
        for n in range(1, MAX_UNTITLED):
            name = f"{stem} copy.md" if n == 1 else f"{stem} copy {n}.md"
            try:
                fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    fh.write(content)
            except OSError:
                # The file already exists at this point (O_EXCL created it), so a
                # write that dies partway — ENOSPC, EIO, a full quota — would
                # leave a TRUNCATED copy behind: visible in the listing, indexed
                # by search, and pushed to the remote by the next sync. Remove it
                # and let the error surface instead.
                with contextlib.suppress(OSError):
                    os.unlink(directory / name)
                raise
            return name, directory
        raise ApiError("could not find a free note name", 409, code="no_free_note_name")

    async with vault_write_lock(vault["localPath"]):
        name, directory = await asyncio.to_thread(_create_unique)
    rel = f"{folder}/{name}" if folder else name
    mark_self_write(directory / name)
    # Rebuild rather than patch: the copy inherits the source's [[wikilinks]], and
    # backlink identity is path-based, so an incremental index add would leave
    # every linked target's backlink list missing the copy until some unrelated
    # rebuild. `api_note_move` rebuilds for exactly this reason; `api_note_new`
    # can patch only because a new note is empty and links to nothing.
    await rebuild_cache(vault)
    return web.json_response({"path": rel})


async def api_note_move(request: web.Request) -> web.Response:
    """Move or rename a note. Refuses to overwrite an existing file."""
    vault = await require_vault(request)
    require_writable(vault)
    body = await json_body(request)
    src_rel = require_note_path(body.get("from"), "from")
    dst_rel = require_note_path(body.get("to"), "to", for_new=True)
    if src_rel == dst_rel:
        return web.json_response({"ok": True, "path": dst_rel})
    src = await vault_mutation_path(vault, src_rel)
    dst = await vault_mutation_path(vault, dst_rel)

    def _move_sync() -> tuple[Path, Path]:
        """Check, mkdir and rename against one locked tree in one worker.

        Re-derive both lexical entries after their folder walks: a scoped vault
        must not retain a scope link's resolved target across lock acquisition,
        and a final-component alias must move as a link rather than its target.
        """
        src = mutation_path_checked(vault, src_rel)
        dst = mutation_path_checked(vault, dst_rel)
        # `exists()` follows the link, so a DANGLING symlink at dst reads as
        # absent and os.rename would silently replace that directory entry.
        # `is_symlink()` catches the link itself.
        if dst.exists() or dst.is_symlink():
            raise ApiError(f"a note already exists at {dst_rel}", 409, code="note_already_exists")
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(src, dst)
        except FileNotFoundError as exc:
            raise ApiError(f"no such note: {src_rel}", 404, code="no_such_note") from exc
        return src, dst

    # Lock BOTH the source and destination across check-and-rename: a concurrent
    # save/delete on the source, or another move to the same destination, would
    # otherwise interleave — the source save could recreate/split the moved note,
    # or two moves could both pass the exists() check and POSIX rename would
    # silently overwrite. Sorted acquisition avoids a lock-order deadlock.
    async with _save_locks_for(str(src), str(dst)), vault_write_lock(vault["localPath"]):
        src, dst = await asyncio.to_thread(_move_sync)
    mark_self_write(src)
    mark_self_write(dst)
    # The path is part of index and backlink identity, so rebuild rather than patch.
    await rebuild_cache(vault)
    return web.json_response({"ok": True, "path": dst_rel})


async def api_sync(request: web.Request) -> web.Response:
    vault = await require_vault(request)
    # sync commits, merges, and pushes — all writes — so a read-only vault must
    # not reach it.
    require_writable(vault)
    # The merge inside rewrites the working tree; no note may be created
    # through a folder it is replacing — see `vault_write_lock`.
    async with vault_write_lock(vault["localPath"]):
        result = await git_ops.sync(
            vault["localPath"],
            branch=vault.get("branch"),
            pat=await resolve_auth(),
            # Scoped vaults must only commit their own subtree — the rest of the
            # repository is the user's unrelated work.
            subfolder=vault.get("subfolder"),
            # Refuse to push if the vault's remote was repointed since clone/attach.
            trusted_remote=vault.get("remoteUrl"),
            # Refuse if the vault's `.git` pointer was redirected since clone/attach.
            trusted_gitdir=vault.get("gitDir"),
            # Attached from a repo with no remote: commit locally, never push.
            local_only=bool(vault.get("localOnly")),
        )
    await rebuild_cache(vault)
    # Stamped here as well as in the background loop, so the backend is the single
    # writer of `lastSync` for both trigger paths. Returned alongside the result
    # so the UI shows the new time without a second round-trip. Null on a
    # conflicted run — nothing reached the remote, so nothing is backed up.
    last_sync = await record_last_sync(vault["id"]) if synced_cleanly(result) else None
    return web.json_response({"result": result, "lastSync": last_sync})


async def api_commit(request: web.Request) -> web.Response:
    """Commit pending edits to the vault's LOCAL git history. Never pushes.

    This backs the periodic autosave. Notes are already on disk within a second
    of typing (the editor's debounced save), so what this adds is version
    history the user did not have to ask for — and because it never reaches a
    remote, doing it unprompted cannot send anything anywhere.
    """
    vault = await require_vault(request)
    # Committing writes to the repository, so a read-only vault must not reach it.
    require_writable(vault)
    # `commit_only=True` returns before any merge, so what this serialises is the
    # index and working-tree writes the commit itself makes: no note may be staged
    # or written underneath them — see `vault_write_lock`.
    async with vault_write_lock(vault["localPath"]):
        result = await git_ops.sync(
            vault["localPath"],
            branch=vault.get("branch"),
            subfolder=vault.get("subfolder"),
            # Refuse if the vault's `.git` pointer was redirected since clone/attach:
            # that decides WHICH repository these commits land in. The remote checks
            # are skipped inside sync() for a commit-only run — nothing is pushed.
            trusted_gitdir=vault.get("gitDir"),
            commit_only=True,
        )
    await rebuild_cache(vault)
    return web.json_response({"result": result})


async def api_trash_open(request: web.Request) -> web.Response:
    """Reveal the vault's local `.trash` in the OS file manager.

    The trash is a dotted directory the app deliberately hides from its own
    listing and search, which left the delete dialog's "you can restore it from
    there" promise with no way to act on it. This is that way.

    Takes no path: the directory is derived from the vault descriptor through
    `vault_mutation_path`, so nothing a caller sends can point this at another
    folder. An absent trash is reported, not created — no note has been deleted
    yet, and conjuring an empty folder to show would be a lie about the state.
    """
    vault = await require_vault(request)
    trash_dir = await trash_dir_path(vault)
    if not await asyncio.to_thread(trash_dir.is_dir):
        return web.json_response(
            {"ok": True, "opened": False, "empty": True, "path": str(trash_dir)}
        )
    if _reveal_binary() is None:
        raise ApiError(
            "opening a folder is not supported on this system",
            501,
            code="folder_open_unsupported",
        )
    await asyncio.to_thread(_reveal_folder_sync, str(trash_dir))
    return web.json_response({"ok": True, "opened": True, "empty": False, "path": str(trash_dir)})


async def api_search(request: web.Request) -> web.Response:
    vault = await require_vault(request)
    query = request.query.get("q", "")
    await scan_changes(vault)
    cache = await get_cache(vault)
    return web.json_response({"results": cache["index"].search(query) if query else []})


async def api_pick_folder(request: web.Request) -> web.Response:
    """Open the OS folder chooser.

    The UI cannot produce an absolute path on its own — the browser file APIs
    deliberately withhold real filesystem paths — but this backend is a local
    process, so it can ask the OS and hand back what the user picked.
    """
    if not pick_folder_supported():
        raise ApiError(
            "the folder chooser is only available on macOS — on Windows and Linux, "
            "paste the vault folder's absolute path into the field instead",
            501,
            code="folder_chooser_unsupported",
        )
    path = await asyncio.to_thread(_pick_folder_sync)
    return web.json_response({"path": path, "cancelled": path is None})


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def create_app() -> web.Application:
    """Build the aiohttp Application with every route."""
    app = web.Application(
        middlewares=[proxy_auth_middleware, error_middleware],
        client_max_size=MAX_BODY_BYTES,
    )
    # The bare /health is the gateway's own liveness poll and the one path the
    # HMAC middleware exempts; /api/health is the same probe reached through the
    # proxy, which is how the UI detects a stale backend process.
    app.router.add_get("/health", api_health)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/vaults", api_vaults)
    app.router.add_post("/api/vaults", api_vault_clone)
    app.router.add_post("/api/vaults/attach", api_vault_attach)
    app.router.add_delete("/api/vaults", api_vault_forget)
    app.router.add_put("/api/vaults/knowledge", api_vault_knowledge)
    app.router.add_get("/api/settings", api_settings_get)
    app.router.add_put("/api/settings", api_settings_put)
    app.router.add_put("/api/pat", api_pat)
    app.router.add_get("/api/changes", api_changes)
    app.router.add_get("/api/notes", api_notes)
    app.router.add_get("/api/note", api_note_read)
    app.router.add_put("/api/note", api_note_save)
    app.router.add_delete("/api/note", api_note_delete)
    app.router.add_post("/api/note/new", api_note_new)
    app.router.add_post("/api/note/duplicate", api_note_duplicate)
    app.router.add_post("/api/note/move", api_note_move)
    app.router.add_post("/api/sync", api_sync)
    app.router.add_post("/api/commit", api_commit)
    app.router.add_post("/api/trash/open", api_trash_open)
    app.router.add_get("/api/search", api_search)
    app.router.add_post("/api/pick-folder", api_pick_folder)
    # Auto-sync runs in this backend process, not in the dashboard page, so the
    # interval is honoured with no Notes tab open. Imported here rather than at
    # module scope because `syncer` imports this module for the settings store and
    # the vault helpers, and a module-level import in both directions is a cycle.
    from kiro_crew.apps.builtins.md_notebook import syncer

    app.on_startup.append(syncer.start_syncer)
    app.on_cleanup.append(syncer.stop_syncer)
    return app


#: Canonical dotted name of this module. The backend launches it with
#: ``runpy.run_module(run_name='__main__')``, which registers the running copy
#: only under ``sys.modules['__main__']`` — not under this key. Any later
#: ``from kiro_crew.apps.builtins.md_notebook import server`` (the one ``syncer``
#: does) then misses and imports a SECOND copy with its own module-level locks
#: and caches, so routes and the auto-sync loop guard different lock objects.
_CANONICAL_NAME = "kiro_crew.apps.builtins.md_notebook.server"


def _register_canonical_self() -> None:
    """Alias the ``__main__`` launch copy under its canonical dotted name.

    Run before ``create_app()`` imports ``syncer``: it makes the dotted import
    resolve to this already-running module instead of loading a fresh copy, so
    the whole process shares one set of module-level locks and caches.
    """
    running = sys.modules.get("__main__")
    if running is None or getattr(running, "__name__", None) != "__main__":
        return
    existing = sys.modules.get(_CANONICAL_NAME)
    if existing is running:
        return
    if existing is None:
        sys.modules[_CANONICAL_NAME] = running


def main() -> int:
    """Entry point when run as a module by the app backend system."""
    _register_canonical_self()
    app = create_app()
    logger.info("Notes backend starting on 127.0.0.1:%d (home: %s)", PORT, _home())
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)
    return 0


# This module is the facade of the Notes backend, and every name is reached through it as
# ``server.<name>``: the gateway runs it, ``syncer`` and the tests import it. A read is served
# by the owner that binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` --
# reaches every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this
# module. The per-process state stays here -- the save, vault-write, vaults and settings
# locks, the note caches and change watches, the self-write marks and the gh token memo --
# with every function that reaches it, every route and the app wiring. New work goes to the
# owner of its responsibility:
#   ``server_store`` -- the data home, staged state writes, vaults.json, settings.json, the PAT
#   ``server_http`` -- ApiError, request bodies, the proxy-auth and error middlewares
#   ``server_paths`` -- vault and note path checks, note staging, listing and reads, the save guard
#   ``server_host`` -- gh discovery, the trusted spawn env, reveal, the folder chooser

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.builtins.md_notebook.server_store",
    "kiro_crew.apps.builtins.md_notebook.server_http",
    "kiro_crew.apps.builtins.md_notebook.server_paths",
    "kiro_crew.apps.builtins.md_notebook.server_host",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(sys.modules[__name__], _OWNERS, excluded=()).served)
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.builtins.md_notebook.server_host import (  # noqa: F401
        _F,
        PICK_FOLDER_SCRIPT,
        PICK_FOLDER_TIMEOUT_SEC,
        REVEAL_BINARIES,
        REVEAL_TIMEOUT_SEC,
        _timeout_as_api_error,
        _windows_gh_candidates,
    )
    from kiro_crew.apps.builtins.md_notebook.server_http import (  # noqa: F401
        _HTTP_ERRORS,
        _error_code,
        _error_status,
        _safe_error,
    )
    from kiro_crew.apps.builtins.md_notebook.server_paths import (  # noqa: F401
        _RECREATE_SENTINEL,
        _UNPORTABLE_CHARS,
        MTIME_TOLERANCE_MS,
        _atomic_write_text_sync,
        _list_note_files_sync,
        _publish_staged_sync,
        _reject_unportable_component,
        _stage_note_text_sync,
        linked_folder_component,
        note_folder,
        reject_linked_folder_component,
    )
    from kiro_crew.apps.builtins.md_notebook.server_store import (  # noqa: F401
        _HOME,
        _STAGING_LEAF,
        APP_NAME,
        DEFAULT_AUTO_SYNC_MINS,
        _crew_data_home,
        _default_home,
        _default_settings,
        _pat_file,
        _read_pat_sync,
        _read_settings_sync,
        _read_vaults_sync,
        _settings_json,
        _staging_dir,
        _vaults_json,
        _write_state_staged_sync,
        _write_vaults_sync,
    )

if TYPE_CHECKING:  # names it serves from outside the owner family, from their homes
    from kiro_crew import (  # noqa: F401
        md_notebook_state,
    )
    from kiro_crew.apps.proxy_auth import (  # noqa: F401
        raw_request_target,
        verify_proxy_request,
    )
    from kiro_crew.atomic_write import (  # noqa: F401
        refuse_linked_parent,
        replace_with_retry,
    )
    from kiro_crew.config.paths import (  # noqa: F401
        config_dir,
    )
    from kiro_crew.constants import (  # noqa: F401
        WINDOWS_DEVICE_STEMS,
    )
    from kiro_crew.platform_compat import (  # noqa: F401
        restrict_to_owner,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    raise SystemExit(main())
