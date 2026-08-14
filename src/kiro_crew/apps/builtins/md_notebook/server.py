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
import functools
import json
import logging
import ntpath
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Callable, Optional

from aiohttp import web

from kiro_crew import hooks, platform_compat, security
from kiro_crew.apps.builtins.md_notebook import git_ops
from kiro_crew.apps.builtins.md_notebook import notes as notes_mod
from kiro_crew.apps.proxy_auth import verify_proxy_request
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.platform_compat import restrict_to_owner
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 9137))
APP_NAME = os.environ.get("KIROCREW_APP_NAME", "md-notebook")


# Data-home paths are resolved LAZILY, never at import time (issue #874): the
# active KiroCrew home depends on KIROCREW_HOME, which a pod, the legacy-home
# migration, or the test-isolation fixture can change AFTER this module is
# imported. `_HOME` is the override hook (None = live-resolve); tests do
# `monkeypatch.setattr(server, "_HOME", tmp)`. See config/paths.py / issue #874.
_HOME: Optional[Path] = None


def _default_home() -> Path:
    """The app's data root under the active KiroCrew home.

    ``config_dir()`` follows ``KIROCREW_HOME`` (default ``~/.kiro/crew``), so an
    isolated dev instance (``KIROCREW_HOME=.kirocrew-dev``) keeps its own vaults
    instead of loading — and editing — the production notes. ``MD_NOTEBOOK_HOME``
    still wins when set. Falls back to the default root when run outside the
    package. Mirrors ``code_review_sage``'s ``crew_home()``.
    """
    env = os.environ.get("MD_NOTEBOOK_HOME")
    if env:
        return Path(env)
    return _crew_data_home()


def _crew_data_home() -> Path:
    """The app's data root under the KiroCrew data home, IGNORING
    ``MD_NOTEBOOK_HOME``.

    The PAT is stored here (never under ``MD_NOTEBOOK_HOME``) so it always sits
    behind ``is_sensitive_path()``'s floor, which protects
    ``<crew-home>/workspace/md-notebook/pat``. A user pointing
    ``MD_NOTEBOOK_HOME`` at an unprotected directory must not be able to relocate
    the live GitHub credential out from behind that floor.
    """
    # config_dir is imported at module scope but CALLED here (lazily) — issue
    # #874 forbids resolving the data home at import time, not importing the
    # resolver. It honors KIROCREW_HOME on every call.
    try:
        base = config_dir()
    except Exception:
        override = os.environ.get("KIROCREW_HOME")
        base = Path(override) if override else Path.home() / ".kiro" / "crew"
    return base / "workspace" / APP_NAME


def _home() -> Path:
    return _HOME if _HOME is not None else _default_home()


def _vaults_json() -> Path:
    return _home() / "vaults.json"


def _pat_file() -> Path:
    # The PAT MUST live under the protected crew data home, never under
    # MD_NOTEBOOK_HOME, so it stays behind is_sensitive_path()'s floor. The
    # _HOME test hook still applies so tests keep their tmp isolation.
    base = _HOME if _HOME is not None else _crew_data_home()
    return base / "pat"


def _clone_root() -> Path:
    return _home() / "vaults"


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
]

# A write we made ourselves is ignored by change detection for this long, so
# saving a note does not report itself as an external edit.
SELF_WRITE_GRACE_SEC = 1.5
# Cached `gh auth token` lifetime.
GH_TOKEN_TTL_SEC = 300
GH_TIMEOUT_SEC = 5
# Largest request body accepted.
MAX_BODY_BYTES = 5_000_000
# Save-guard tolerance in milliseconds: filesystems round mtimes differently.
MTIME_TOLERANCE_MS = 1
# baseMtime the client echoes back to confirm "yes, recreate this deleted note".
# A real mtime is never negative, so it can't collide with a genuine timestamp.
_RECREATE_SENTINEL = -1

# Per-note-path save locks. The save guard reads the mtime and then writes; two
# concurrent saves of the same note (two tabs debounce-flushing from one
# baseMtime) would otherwise BOTH pass the check before either replaces the
# file, and the second write would silently destroy the first. Serializing the
# check-and-write per canonical path makes the second save observe the first's
# new mtime and surface an ESTALE conflict instead.
_save_locks: dict[str, asyncio.Lock] = {}


def _save_lock(path: str) -> asyncio.Lock:
    lock = _save_locks.get(path)
    if lock is None:
        lock = asyncio.Lock()
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


# Serializes every vaults.json read-modify-write. Clone/attach/forget/knowledge
# each read the vault list, mutate it, and write it back; without this lock two
# concurrent mutations from separate tabs would both read the old list and the
# last writer would discard the other's change (lost update).
_vaults_lock = asyncio.Lock()


# Upper bound on the Untitled-N search when creating a note.
MAX_UNTITLED = 1000
# Native folder picker.
PICK_FOLDER_TIMEOUT_SEC = 120
# Revealing a folder in the OS file manager. Absolute paths, never resolved from
# PATH: the front of PATH is agent-writable, so a `open`/`xdg-open` shim planted
# there would be what this launches.
REVEAL_TIMEOUT_SEC = 20
REVEAL_BINARIES = {
    "darwin": "/usr/bin/open",
    "linux": "/usr/bin/xdg-open",
}
PICK_FOLDER_SCRIPT = "\n".join(
    [
        "activate",
        "try",
        '  set chosen to choose folder with prompt "Choose your notes folder"',
        "  return POSIX path of chosen",
        "on error number -128",
        '  return ""',
        "end try",
    ]
)

# vault id -> {"index": SearchIndex, "backlinks": dict, "statuses": dict}
_caches: dict[str, dict[str, Any]] = {}
# vault id -> {"rev": int, "paths": set, "snapshot": dict[str, float]}
_watches: dict[str, dict[str, Any]] = {}
# absolute path -> monotonic timestamp of our own last write
_self_writes: dict[str, float] = {}


class ApiError(Exception):
    """A handler failure carrying an HTTP status."""

    def __init__(
        self, message: str, status: int = 500, code: str = "request_failed", **extra: Any
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _read_vaults_sync() -> list[dict[str, Any]]:
    try:
        with open(_vaults_json(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _atomic_write_text_sync(path: Path, content: str) -> None:
    """Write via a sibling temp file and rename over the target.

    `write_text` truncates first, so a failure partway — a full disk is the
    realistic one — leaves the note truncated or half-written with no copy of
    what was there. `os.replace` is atomic within a filesystem, and the temp file
    is a sibling so it stays on the same one. The `.tmp` suffix keeps it out of
    the `.md` scan.
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        # newline="" disables the platform newline translation that would
        # otherwise rewrite the note's "\n" as "\r\n" on Windows, so a note's
        # bytes are identical on every OS.
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Never leave the temp behind to be mistaken for user content.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_vaults_sync(vaults: list[dict[str, Any]]) -> None:
    _home().mkdir(parents=True, exist_ok=True)
    tmp = _vaults_json().with_name(f"vaults.json.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(vaults, fh, indent=2)
    os.replace(tmp, _vaults_json())


def _read_pat_sync() -> Optional[str]:
    try:
        pat = open(_pat_file(), encoding="utf-8").read().strip() or None
    except OSError:
        return None
    if pat is not None:
        # Re-assert owner-only access on load: a PAT file left with a permissive
        # ACL (e.g. created before this lockdown, or by another principal on
        # Windows) would otherwise stay readable by other OS accounts. Warn but
        # do not fail the read — losing auth entirely is worse than a warning.
        try:
            restrict_to_owner(_pat_file())
        except OSError:
            logger.warning("could not restrict Notes PAT file to owner-only", exc_info=True)
    return pat


def _write_pat_sync(pat: str) -> None:
    target = _pat_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to an owner-only sibling temp, fsync, then atomically replace. A
    # direct O_TRUNC open would empty the existing token before the new bytes
    # land, so a failure partway (a full disk is the realistic one) would lose a
    # valid credential.
    #
    # os.chmod's 0600 is a no-op on Windows (it only toggles read-only), leaving
    # the token readable by other accounts. restrict_to_owner applies an
    # owner-only DACL there and chmod 0600 on POSIX, and atomic_write applies it
    # to the temp BEFORE any payload byte — narrower than the previous
    # write-then-restrict here, which left the token in a parent-inherited-DACL
    # file for the duration of the icacls subprocess. restrict_on_error="warn"
    # keeps the original policy: a chmod failure warns rather than losing the
    # credential. Written 0600 and never echoed back (only a boolean).
    atomic_write(target, pat, fsync=True, restrict_to_owner=True, restrict_on_error="warn")


async def read_vaults() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_read_vaults_sync)


async def write_vaults(vaults: list[dict[str, Any]]) -> None:
    await asyncio.to_thread(_write_vaults_sync, vaults)


async def read_pat() -> Optional[str]:
    return await asyncio.to_thread(_read_pat_sync)


# ---------------------------------------------------------------------------
# GitHub auth: stored PAT first, else the user's `gh` CLI login
# ---------------------------------------------------------------------------

_gh_cache: dict[str, Any] = {"value": None, "at": 0.0}


def _find_gh() -> Optional[str]:
    """Absolute path to a trusted ``gh``, or None. PATH is NOT consulted — a
    workspace-writable entry could shadow ``gh`` with a planted binary that runs
    unsandboxed when a token is minted, so we only trust system/admin locations
    and fail closed (no gh -> no gh-derived token, which the caller tolerates)."""
    override = os.environ.get("MD_NOTEBOOK_GH_BIN")
    candidates = (
        [override]
        if override
        else [
            # System / package-manager locations only. `~/.local/bin` is
            # deliberately NOT here: it is a spot the agent itself can write, so
            # trusting a `gh` there would let a planted binary run with the
            # backend's PAT + proxy secret in scope. gh is optional — no trusted
            # gh simply means no gh-derived token.
            "/opt/homebrew/bin/gh",
            "/usr/local/bin/gh",
            "/usr/bin/gh",
        ]
    )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _trusted_env() -> dict[str, str]:
    """The current environment with PATH pinned to trusted system directories.

    Every subprocess this module spawns dispatches to child helpers that are
    resolved through PATH, and the gateway's inherited PATH can carry an
    agent-writable entry (`~/.local/bin` is the usual one). Pinning it means a
    planted helper cannot ride along on an action the user initiated. Same pin, and
    the same reason, as `run_git` — see `git_ops.TRUSTED_PATH`.

    Only PATH is replaced: the rest of the environment is what lets these tools
    reach the running desktop session (DISPLAY, DBUS_SESSION_BUS_ADDRESS, XDG_*).
    Windows keeps the inherited PATH, where helpers live beside their install.
    """
    env = dict(os.environ)
    if os.name == "posix":
        env["PATH"] = git_ops.TRUSTED_PATH
    return env


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


def require_note_path(rel: Any, field: str = "path") -> str:
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
    if not parts[-1].lower().endswith(".md"):
        raise ApiError(f"{field} must be a .md note", 400, code="path_not_a_note")
    return rel


def require_folder_path(folder: Any) -> Optional[str]:
    """Validate an optional caller-supplied FOLDER (the new-note target).

    Same undotted rule as `require_note_path`, without the `.md` requirement: a
    folder is not a note. Without it a new note could be created inside `.git`.
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
    `is_symlink()` does NOT report, so `is_link_or_junction` is used to catch both.
    """
    trash_dir = await vault_mutation_path(vault, git_ops.TRASH_DIR)
    if await asyncio.to_thread(platform_compat.is_link_or_junction, trash_dir):
        raise ApiError(
            f"this vault's {git_ops.TRASH_DIR} is a symlink — refusing to use it, "
            "because a trashed note would be written somewhere the app does not "
            "keep out of git",
            400,
            code="trash_is_symlink",
        )
    return trash_dir


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


async def make_dirs(path: Path) -> None:
    """Create a directory and its parents, tolerating an existing one."""
    await asyncio.to_thread(functools.partial(path.mkdir, parents=True, exist_ok=True))


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


def _snapshot_sync(root: Path) -> dict[str, float]:
    snap: dict[str, float] = {}
    for rel in _list_note_files_sync(root):
        try:
            snap[rel] = os.stat(root / rel).st_mtime
        except OSError:
            continue
    return snap


def watch_state(vault: dict[str, Any]) -> dict[str, Any]:
    state = _watches.get(vault["id"])
    if state is None:
        state = {"rev": 0, "paths": set(), "snapshot": None}
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


async def read_note_text(path: Path) -> Optional[str]:
    """Vault file contents, or None when the gate refuses the path.

    EVERY read of vault content goes through here. `safe_join` proves a path
    stays inside the vault, but a vault is any folder the user attaches and a
    `.md` entry inside it can be a symlink to a private key, so containment is
    not enough on its own. `safe_read_file_bytes` canonicalizes via realpath,
    refuses a sensitive resolved target and opens with O_NOFOLLOW.

    Centralized deliberately: this gate was previously applied per call site and
    each new read path was a fresh hole.
    """
    try:
        data = await asyncio.to_thread(hooks.safe_read_file_bytes, str(path))
    except (OSError, hooks.FileTooLargeError):
        return None
    if data is None:
        return None
    # Normalize line endings to "\n": the read is binary (through the sensitive
    # gate), so a note written with CRLF elsewhere (Obsidian on Windows, a repo
    # with autocrlf) would otherwise reach the block editor as "\r\n" and produce
    # mixed endings on the next save. Writes already emit "\n" (newline="").
    return data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


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
        notes_mod.SearchDoc(
            path=p, title=notes_mod.note_title(p, c), content=c
        )
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
    """Every note with its title, timestamps and sync status."""
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
        content = await read_note_text(abs_path)
        if content is None:
            continue  # refused by the gate, or unreadable
        # birthtime is real on APFS/HFS+; elsewhere fall back to ctime then mtime.
        created = getattr(st, "st_birthtime", None) or st.st_ctime or st.st_mtime
        listing.append(
            {
                "path": rel,
                "title": notes_mod.note_title(rel, content),
                "modifiedAt": st.st_mtime * 1000,
                "createdAt": created * 1000,
                "syncStatus": cache["statuses"].get(prefix + rel, "synced"),
            }
        )
    return listing


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------


async def json_body(request: web.Request) -> dict[str, Any]:
    """Parse the JSON body.

    Uses ``request.read()``, which returns aiohttp's cached copy — the auth
    middleware has already consumed the stream to verify the signature, so
    reading the raw stream again would yield nothing. The size ceiling is
    enforced by the Application's ``client_max_size``.
    """
    raw = await request.read()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f"invalid JSON body: {exc.msg}", 400, code="invalid_json_body") from exc
    return data if isinstance(data, dict) else {}


async def require_vault(request: web.Request) -> dict[str, Any]:
    """The requested vault, or the first one when no id is given."""
    vaults = await read_vaults()
    wanted = request.query.get("vault") or (vaults[0]["id"] if vaults else None)
    for v in vaults:
        if v["id"] == wanted:
            return v
    raise ApiError("no vault connected", 404, code="no_vault_connected")


def require_writable(vault: dict[str, Any]) -> None:
    if vault.get("readOnly"):
        raise ApiError("vault is read-only", 403, code="vault_read_only")


@web.middleware
async def proxy_auth_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
    """Verify the gateway's HMAC on every request except the liveness probe.

    Fails closed: the bare ``/health`` path is the gateway's own in-process
    poll and is the only exemption. Everything else must carry a fresh
    signature, so a stray local process cannot drive this backend.
    """
    if request.path == "/health":
        return await handler(request)
    target = request.path + (f"?{request.query_string}" if request.query_string else "")
    body = await request.read() if request.can_read_body else b""
    if not verify_proxy_request(
        request.headers.get("X-KiroCrew-Proxy", ""),
        method=request.method,
        target=target,
        body=body,
    ):
        # Audit the denial before answering, following the file-explorer
        # builtin's proxy_auth_failed convention — an unsigned local process
        # probing this backend is exactly what the SEL trail exists to record.
        # SEL writes are blocking file I/O, so emit off-loop; log only the
        # path (no query string), which can carry note names.
        def _audit_denied() -> None:
            sel().log_api_access(
                caller=APP_NAME,
                operation="proxy_auth_failed",
                outcome="denied",
                source="builtin-app",
                resources=request.path,
            )

        await asyncio.to_thread(_audit_denied)
        return web.json_response(
            {"error": "invalid or missing proxy signature", "code": "invalid_proxy_signature"},
            status=401,
        )
    return await handler(request)


def _error_status(exc: BaseException) -> int:
    if isinstance(exc, ApiError):
        return exc.status
    if isinstance(exc, git_ops.AttachError):
        return 400
    return 500


def _error_code(exc: BaseException) -> str:
    """A stable machine-readable id, so the UI can localize its own copy."""
    if isinstance(exc, ApiError):
        return exc.code
    if isinstance(exc, git_ops.AttachError):
        return exc.code
    if isinstance(exc, git_ops.GitError):
        return "git_failed"
    return "io_failed"


#: `error` is advisory English prose; `code` is the contract the dashboard keys
#: its localized message off. aiohttp's exception classes carry the status, so
#: each one is a literal rather than a computed `status=` argument.
_HTTP_ERRORS: dict[int, type[web.HTTPException]] = {
    400: web.HTTPBadRequest,
    401: web.HTTPUnauthorized,
    403: web.HTTPForbidden,
    404: web.HTTPNotFound,
    409: web.HTTPConflict,
    413: web.HTTPRequestEntityTooLarge,
    500: web.HTTPInternalServerError,
    501: web.HTTPNotImplemented,
}


def _safe_error(exc: BaseException) -> str:
    """Exception text with credentials, exfiltration URLs and host paths stripped.

    Handlers here drive git and filesystem work over caller-supplied vault URLs,
    ids and note paths, so a raw message routinely carries absolute paths and git
    stderr. Both branches of ``error_middleware`` send their text to the browser.

    The path pass is the load-bearing one: the dominant shape here is an OS error
    like ``[Errno 2] No such file or directory: '/home/<user>/.kiro/crew/...'``,
    which the credential and URL passes do not match at all.
    """
    text, _ = security.redact_credentials(str(exc))
    text, _ = security.redact_exfiltration_urls(text)
    text, _ = security.redact_local_paths(text)
    return text


@web.middleware
async def error_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
    """Turn handler exceptions into the JSON error shape the UI expects."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (ApiError, git_ops.AttachError, git_ops.GitError, OSError) as exc:
        payload: dict[str, Any] = {"error": _safe_error(exc), "code": _error_code(exc)}
        if isinstance(exc, ApiError):
            payload.update(exc.extra)
        cls = _HTTP_ERRORS.get(_error_status(exc), web.HTTPInternalServerError)
        raise cls(text=json.dumps(payload), content_type="application/json") from exc
    except Exception as exc:  # noqa: BLE001 — never leak a traceback to the UI
        logger.exception("md-notebook: unhandled error on %s", request.path)
        return web.json_response({"error": _safe_error(exc), "code": "internal_error"}, status=500)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "features": FEATURES})


async def api_vaults(request: web.Request) -> web.Response:
    clone_root = str(_clone_root())
    vaults = []
    for v in await read_vaults():
        # `external` = attached in place rather than cloned by this app.
        # Computed on read, never persisted.
        vaults.append({**v, "external": not str(v["localPath"]).startswith(clone_root)})
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
    # bad token) overwrite a previously-valid stored PAT and break every existing
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
        await asyncio.to_thread(shutil.rmtree, str(clone_dir), True)
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


async def api_pat(request: web.Request) -> web.Response:
    """Set or clear the stored token: an empty value clears it."""
    body = await json_body(request)
    pat = body.get("pat")
    if pat:
        await asyncio.to_thread(_write_pat_sync, str(pat))
    else:
        def _remove() -> None:
            try:
                os.unlink(_pat_file())
            except FileNotFoundError:
                pass

        await asyncio.to_thread(_remove)
    return web.json_response(
        {"hasPat": bool(await read_pat()), "hasGhAuth": bool(await gh_token())}
    )


async def api_changes(request: web.Request) -> web.Response:
    """External-change poll: the current revision plus paths touched since."""
    vault = await require_vault(request)
    state = await scan_changes(vault)
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
    base_mtime = body.get("baseMtime")
    # Serialize the check-and-write for this note so two concurrent saves can't
    # both pass the stale-write guard and silently clobber each other.
    async with _save_lock(str(abs_path)):
        # Optimistic concurrency: when the caller passes the mtime it read,
        # refuse to write if the file changed underneath — otherwise an edit made
        # outside the app (Obsidian, git pull) would be silently clobbered.
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
                # recreate it" and falls through to the write below. Without this
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
        await make_dirs(abs_path.parent)
        await asyncio.to_thread(_atomic_write_text_sync, abs_path, content)
    mark_self_write(abs_path)
    cache = await get_cache(vault)
    cache["index"].update(
        notes_mod.SearchDoc(
            path=rel, title=notes_mod.note_title(rel, content), content=content
        )
    )
    # Backlinks are a whole-vault relation, so recompute them from disk.
    root = await vault_path(vault)
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
    # Refuses a vault-supplied `.trash` symlink outright — see `trash_dir_path`.
    trash_dir = await trash_dir_path(vault)
    name = notes_mod.note_basename(rel.replace("\\", "/"))

    def _to_trash() -> str:
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
            return candidate.name
        raise ApiError("could not find a free name in the trash", 409, code="no_free_note_name")

    # Share the note's save lock: a concurrent debounced save could otherwise
    # recreate the note right after it is moved away.
    async with _save_lock(str(abs_path)):
        try:
            trashed = await asyncio.to_thread(_to_trash)
        except FileNotFoundError as exc:
            raise ApiError(f"no such note: {rel}", 404, code="no_such_note") from exc
    mark_self_write(abs_path)
    mark_self_write(trash_dir / trashed)
    # Rebuild rather than drop one index entry: the deleted note's own
    # [[wikilinks]] disappear with it, so every note it pointed at keeps a
    # backlink to a note that no longer exists until the next rebuild.
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
    folder = require_folder_path(body.get("folder"))
    directory = await vault_path(vault, folder or None)

    def _create_unique() -> tuple[str, str]:
        """Find a free name and create the file, in one thread.

        Done in a single offloaded step for two reasons: the search is up to
        MAX_UNTITLED stat calls, which must not run on the event loop; and
        testing then creating in one place leaves no window for a second
        request to take the name between the two (the exclusive-create flag is
        what actually decides it).
        """
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
            return name, ""
        raise ApiError("could not find a free note name", 409, code="no_free_note_name")

    name, content = await asyncio.to_thread(_create_unique)
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
    src = await vault_path(vault, src_rel)
    # Read through the central sensitive-path gate, not open(). A `.md` entry in
    # an attached vault can be a symlink aimed at a private key; copying it would
    # land that content INSIDE the vault, where the search index would serve it
    # and the next sync's `git add -A` would push it to the remote.
    content = await read_note_text(src)
    if content is None:
        raise ApiError(f"no such note: {src_rel}", 404, code="no_such_note")
    # Normalize the separator BEFORE deriving the name and folder. `safe_join`
    # accepts a Windows-style "dir\note.md" (it normalizes internally to validate
    # containment), but `rfind("/")` would then find no separator and
    # `note_basename` would keep the backslash — so the copy landed at the vault
    # ROOT named "dir\note copy.md" on POSIX, while pathlib reads that same name
    # as a subdirectory on Windows. One request, two different destinations.
    rel_posix = src_rel.replace("\\", "/")
    stem = notes_mod.note_basename(rel_posix)
    folder = rel_posix[: rel_posix.rfind("/")] if "/" in rel_posix else ""
    # Resolve the destination directory from the validated relative folder, so
    # the copy can only ever be written beside its source inside the vault.
    directory = await vault_path(vault, folder or None)

    def _create_unique() -> str:
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
            return name
        raise ApiError("could not find a free note name", 409, code="no_free_note_name")

    name = await asyncio.to_thread(_create_unique)
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
    dst_rel = require_note_path(body.get("to"), "to")
    if src_rel == dst_rel:
        return web.json_response({"ok": True, "path": dst_rel})
    src = await vault_mutation_path(vault, src_rel)
    dst = await vault_mutation_path(vault, dst_rel)
    # Lock BOTH the source and destination across check-and-rename: a concurrent
    # save/delete on the source, or another move to the same destination, would
    # otherwise interleave — the source save could recreate/split the moved note,
    # or two moves could both pass the exists() check and POSIX rename would
    # silently overwrite. Sorted acquisition avoids a lock-order deadlock.
    async with _save_locks_for(str(src), str(dst)):
        # `exists()` follows the link, so a DANGLING symlink at dst reads as
        # absent and os.rename would silently replace that directory entry.
        # `is_symlink()` catches the link itself.
        if await asyncio.to_thread(dst.exists) or await asyncio.to_thread(dst.is_symlink):
            raise ApiError(f"a note already exists at {dst_rel}", 409, code="note_already_exists")
        await make_dirs(dst.parent)
        try:
            await asyncio.to_thread(os.rename, src, dst)
        except FileNotFoundError as exc:
            raise ApiError(f"no such note: {src_rel}", 404, code="no_such_note") from exc
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
    return web.json_response({"result": result})


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


def _reveal_binary() -> Optional[str]:
    """The absolute file-manager binary for this platform, or None.

    Existence-checked rather than assumed: a minimal Linux container has no
    `xdg-open`, and a missing binary must read as "not supported here" rather
    than a 500.
    """
    if sys.platform.startswith("win"):
        return "explorer"  # handled via os.startfile, never spawned
    binary = REVEAL_BINARIES.get("darwin" if sys.platform == "darwin" else "linux")
    return binary if binary and os.path.isfile(binary) else None


def _reveal_folder_sync(path: str) -> None:
    """Open a directory in the OS file manager.

    The path is computed by the caller from the vault descriptor — never taken
    from the request — and the argv is a fixed binary plus that one path, with no
    shell. Blocking, so callers offload it.
    """
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 — Windows-only API
        return
    binary = _reveal_binary()
    if not binary:
        raise ApiError(
            "opening a folder is not supported on this system",
            501,
            code="folder_open_unsupported",
        )
    # `xdg-open` is a SHELL SCRIPT that dispatches to whichever helper it finds on
    # PATH — `gio`, `gvfs-open`, `exo-open`, `kde-open`, `dbus-send` — so a planted
    # `gio` in an agent-writable PATH entry would execute the moment the user clicks
    # the delete dialog's `.trash` link, a click they have every reason to trust.
    proc = subprocess.run(
        [binary, path],
        capture_output=True,
        text=True,
        timeout=REVEAL_TIMEOUT_SEC,
        check=False,
        env=_trusted_env(),
    )
    if proc.returncode != 0:
        raise ApiError("could not open the folder", 500, code="folder_open_failed")


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


def pick_folder_supported() -> bool:
    """macOS only, and suppressible so tests never open a GUI dialog."""
    return sys.platform == "darwin" and not os.environ.get("MD_NOTEBOOK_NO_PICKER")


def _pick_folder_sync() -> Optional[str]:
    """Show the OS folder chooser. Returns the path, or None if cancelled."""
    proc = subprocess.run(
        ["/usr/bin/osascript", "-e", PICK_FOLDER_SCRIPT],
        capture_output=True,
        text=True,
        timeout=PICK_FOLDER_TIMEOUT_SEC,
        check=False,
        env=_trusted_env(),
    )
    if proc.returncode != 0:
        raise ApiError("could not open the folder chooser", 500, code="folder_chooser_failed")
    # osascript returns a trailing slash; the attach flow wants none.
    path = proc.stdout.strip()
    return path.rstrip("/") if path else None


async def api_pick_folder(request: web.Request) -> web.Response:
    """Open the OS folder chooser.

    The UI cannot produce an absolute path on its own — the browser file APIs
    deliberately withhold real filesystem paths — but this backend is a local
    process, so it can ask the OS and hand back what the user picked.
    """
    if not pick_folder_supported():
        raise ApiError("folder chooser is only available on macOS", 501, code="folder_chooser_unsupported")
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
    return app


def main() -> int:
    """Entry point when run as a module by the app backend system."""
    app = create_app()
    logger.info("Notes backend starting on 127.0.0.1:%d (home: %s)", PORT, _home())
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    raise SystemExit(main())
