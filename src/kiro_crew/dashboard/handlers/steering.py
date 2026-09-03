"""Kiro steering files API — list / read / create / update / delete.

Steering files are plain markdown documents that Kiro injects into every
session as always-on project or personal conventions.  Two locations are in
play, and they are loaded by two different mechanisms:

* ``~/.kiro/steering/**/*.md`` — **global** (``user`` source).  kiro-cli loads
  these for every session, and the dashboard's own CC-backend injection
  (:func:`kiro_crew.context._load_steering_resources`) globs the agent
  config's ``file://.kiro/steering/**/*.md`` resource against ``$HOME``, which
  resolves to exactly this directory.
* ``<project>/.kiro/steering/**/*.md`` — **workspace** (``workspace`` source).
  kiro-cli loads these because the session subprocess runs with the slot's
  project directory as its cwd.

These endpoints back the Steering tab under Agent Capabilities, surfacing which
steering documents are in effect.

Path handling mirrors the skills browser (``handlers/_shared.py``): traversal,
absolute paths, ``~`` expansion, non-``.md`` suffixes, symlinked intermediate
directories and sensitive locations are all rejected before any read or write.
A LEAF symlink is listed and readable — read-only — when its resolved target
passes the session loader's admission gate against the source's LINK trust
base (:func:`kiro_crew.context.steering_target_admissible`: ``$HOME`` for
``user``, the steering root itself for ``workspace`` — ``_link_trust_base``);
it never resolves for write.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import pinned_fs
from kiro_crew.atomic_write import (
    atomic_write,
    open_access_control_source,
    pinned_parent_replace_supported,
)
from kiro_crew.context import steering_target_admissible
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.frontmatter import STEERING_LOADER, set_frontmatter_fields, split_frontmatter
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.security import (
    is_sensitive_path,
    is_sensitive_write_path,
    redact_credentials,
    redact_exfiltration_urls,
)

from ._shared import (
    _is_restricted_session,
    _read_session_key,
    active_project_dir,
    active_project_state,
)

logger = logging.getLogger(__name__)

# Hard caps — keep the endpoints bounded regardless of what is on disk.
STEERING_MAX_FILES = 500
STEERING_FILE_MAX_BYTES = 262_144  # 256 KiB per steering document

# ``user`` → ~/.kiro/steering, ``workspace`` → <project>/.kiro/steering
STEERING_SOURCES = ("user", "workspace")

# The ``inclusion`` modes the Kiro steering format defines
# (https://kiro.dev/docs/steering/). The tab REPORTS what each document
# declares and lets an author change it; what the harness then DOES with the
# declaration is the harness's own business, documented upstream. Kiro Crew
# does not act on the value.
STEERING_INCLUSION_MODES: tuple[str, ...] = ("always", "fileMatch", "manual", "auto")

# What an absent or unrecognized declaration is reported as — Kiro's documented
# default. ``inclusion_declared`` carries the raw spelling beside it, so a typo
# can be shown as a typo rather than silently normalized away.
STEERING_INCLUSION_DEFAULT = "always"

# Case-insensitive lookup that also canonicalizes the spelling, so ``FileMatch``
# and ``filematch`` both report as the documented ``fileMatch``.
_INCLUSION_CANONICAL = {mode.lower(): mode for mode in STEERING_INCLUSION_MODES}

# Listing metadata is author-supplied free text (a heading, a declared mode, a
# glob).  One cap for all of it, so a pathological single line cannot inflate a
# 500-entry response.
_STEERING_META_MAX_CHARS = 200

# Request field → front-matter key, for the optional mode edit on ``PUT``.
# Ordered so a document gaining both reads the way Kiro's documentation writes
# them. Only these two are writable: the endpoint edits a document's
# DECLARATION, and everything else in its front matter belongs to the author.
# ``name`` and ``description`` are deliberately NOT writable here. They matter —
# they are what the on-demand index shows the model — but an author sets them by
# editing the document, and no caller has ever sent them. Accepting a field
# nothing writes is surface that has to be reasoned about at every review and
# defended at every security pass, for no behavior.
STEERING_WRITABLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("inclusion", "inclusion"),
    ("file_match_pattern", "fileMatchPattern"),
)

# Precondition header: the ``project_key`` the client was listed, echoed back
# on a workspace write so a re-pointed chat slot cannot redirect it. A header
# rather than a body field because DELETE carries no body.
STEERING_PROJECT_HEADER = "X-Steering-Project"

# ``O_NOFOLLOW`` does not exist on Windows — ``getattr`` keeps the flag optional
# so a write there raises no AttributeError. Where the flag is absent the write
# paths fall back to an lstat/open/fstat identity check (same defense, one extra
# syscall) rather than trusting the kernel to refuse the symlink.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# Whether create/delete can address the leaf relative to a pinned parent
# descriptor. supports_pinned_walk covers the openat capability itself; the extras
# name the other descriptor-relative syscalls these paths issue -- os.unlink for
# delete and for create's rollback, os.stat for the identity check that rollback
# verifies against (pinned_fs.unlink_verified; os.lstat is not a supports_dir_fd
# member even on Linux, the capability belongs to os.stat). Where this is False
# (Windows) the by-name O_EXCL|O_NOFOLLOW create and the by-name unlink are the
# floor, unchanged.
_DIR_FD_SUPPORTED = pinned_fs.supports_pinned_walk() and {os.unlink, os.stat}.issubset(
    os.supports_dir_fd
)

# Filenames are user-visible document names: word chars, dash, dot, space and
# nested folders.  Anything else is rewritten on create (see _safe_rel_name).
_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9._/ -]")


def _sel():
    """Late-binding sel() — allows monkeypatching at parent package level."""
    import kiro_crew.dashboard.handlers as _pkg  # circular import

    return _pkg.sel()


def _redact_meta(text: str) -> str:
    """Redact credentials + exfiltration URLs from listing metadata.

    Metadata (the first-heading description, the display path) is never written
    back to disk, so redacting it is free — it mirrors ``_redact_prompt`` in
    ``handlers/prompts.py``. Editor CONTENT is deliberately NOT redacted: the
    detail response is what the textarea saves back, so a redaction there would
    overwrite the user's own file with ``[REDACTED]`` markers.
    """
    out, _ = redact_credentials(text)
    out, _ = redact_exfiltration_urls(out)
    return out


def _display_path(path: Path | str) -> str:
    """Collapse the real home prefix to ``~`` so responses never leak it."""
    out = str(path)
    for home in {str(Path.home()), str(Path.home().resolve())}:
        out = out.replace(home, "~")
    return out


def steering_roots(project_dir: Path | None = None) -> list[tuple[str, Path]]:
    """Return ``(source, path)`` pairs for the steering locations.

    Unlike the skills roots this does NOT filter on existence: the tab must be
    able to show "no global steering yet" and create the first file, so a
    missing directory is still a valid (empty) root.  Sensitive locations are
    still excluded.
    """
    out: list[tuple[str, Path]] = []
    user_dir = Path.home() / ".kiro" / "steering"
    if not is_sensitive_path(str(user_dir)):
        out.append(("user", user_dir))
    if project_dir:
        ws_dir = Path(project_dir) / ".kiro" / "steering"
        if not is_sensitive_path(str(ws_dir)) and ws_dir != user_dir:
            out.append(("workspace", ws_dir))
    return out


def _first_line(text: str) -> str:
    """Cheap description: the first markdown heading, else the first prose line."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "---":
            continue
        if stripped.startswith("#"):
            return re.sub(r"^#+\s*", "", stripped).strip()[:_STEERING_META_MAX_CHARS]
        return stripped[:_STEERING_META_MAX_CHARS]
    return ""


def _empty_meta() -> dict[str, str]:
    """Metadata for a document whose head could not be read."""
    return {
        "description": "",
        "inclusion": STEERING_INCLUSION_DEFAULT,
        "inclusion_declared": "",
        "file_match_pattern": "",
    }


def _head_meta(path: Path, within_root: Path, cap: int = 2048) -> dict[str, str]:
    """Listing metadata for one document, from a single bounded head read.

    Returns ``description``, ``inclusion`` (canonical, always one of
    :data:`STEERING_INCLUSION_MODES`), ``inclusion_declared`` (verbatim, ``""``
    when the field is absent, so the tab can report a typo as a typo) and
    ``file_match_pattern``.

    ONE read for all four.  The scan already pays an ``lstat`` + ``resolve`` +
    ``stat`` per entry over as many as :data:`STEERING_MAX_FILES` documents, so
    a second open per field would add a whole pass over the slowest half of
    ``GET /api/steering``.

    The description comes from the document BODY, not from the raw head.  A
    document opening with front matter used to be summarized as its first
    declaration — the Steering tab showed ``inclusion: manual`` where the
    author's title belongs.
    """
    # Read through the guarded reader, not ``path.open()``: the scan above rejects
    # a SYMLINK and a sensitive PATH, and a hardlink defeats both — the entry's own
    # path stays innocently inside the steering root while its inode is
    # ``~/.aws/credentials``, whose first line would then be published as this
    # document's description. ``safe_read_file_bytes_nolink`` fstat()s the opened
    # descriptor and refuses ``st_nlink > 1``, so the inode validated is the inode
    # read. ``cap + 1`` so a full slice stays distinguishable from a file exactly
    # ``cap`` bytes long; only ``cap`` is decoded.
    try:
        raw = safe_read_file_bytes_nolink(
            str(path), within_root=str(within_root), max_bytes=cap + 1, allow_truncate=True
        )
    except (OSError, FileTooLargeError):
        return _empty_meta()
    if raw is None:
        return _empty_meta()
    truncated = len(raw) > cap
    head = raw[:cap].decode("utf-8", errors="replace")
    fields, body = split_frontmatter(head, STEERING_LOADER)
    if not fields and body == head:
        # ``split_frontmatter`` already returns the text AFTER a fence that
        # closed, even one holding no real ``key: value`` fields — a
        # comment-only block. ``body == head`` is what tells the two apart
        # from here: it is unchanged only when no dialect-recognized fence
        # was found at all, so the whole slice IS the body — the behavior
        # this listing had before it understood front matter at all. Treating
        # "no fields" as "no fence" overwrote a correctly split body with the
        # RAW head, which starts with the fence and whatever it holds — a
        # comment-only block's own ``#`` line then reads as the document's
        # first markdown heading, publishing the front matter's comment as
        # the description instead of the title after it.
        #
        # The one exception is a block that runs PAST the head slice: there
        # is no closing fence to find, the "body" is the truncated front
        # matter itself, and describing the document by its first
        # declaration is the bug this function fixes. Both openers: a CRLF
        # document starts ``---\r\n``, and missing that let the raw head
        # through — re-exposing ``inclusion:`` as the description, which is
        # the bug this function exists to fix.
        opens_fence = head.startswith("---\n") or head.startswith("---\r\n")
        body = "" if (truncated and opens_fence) else head
    declared = fields.get("inclusion", "").strip()
    return {
        "description": _first_line(body),
        "inclusion": _INCLUSION_CANONICAL.get(declared.lower(), STEERING_INCLUSION_DEFAULT),
        "inclusion_declared": declared[:_STEERING_META_MAX_CHARS],
        "file_match_pattern": (
            fields.get("fileMatchPattern", "").strip()[:_STEERING_META_MAX_CHARS]
        ),
    }


def list_steering_blocking(project_dir: Path | None = None) -> dict[str, Any]:
    """Blocking scan of both steering roots — run on the discovery pool.

    Returns ``{"files": [...], "roots": [...], "project": "<display path>"}``.
    Each file entry: ``{key, name, rel, source, path, size, description,
    inclusion, inclusion_declared, file_match_pattern, linked, editable,
    target}`` where ``key`` is ``"<source>/<rel>"``. ``linked`` marks a leaf
    symlink admitted read-only; ``target`` carries its resolved display path
    (``""`` otherwise) and ``editable`` is False exactly for linked entries.
    """
    files: list[dict[str, Any]] = []
    roots: list[dict[str, Any]] = []
    for source, root in steering_roots(project_dir):
        exists = root.is_dir()
        roots.append({
            "source": source,
            "path": _redact_meta(_display_path(root)),
            "exists": exists,
        })
        if not exists:
            continue
        base = _base_for(source, project_dir)
        if base is None:
            continue
        try:
            base_resolved = base.resolve(strict=True)
        except OSError:
            continue
        if not _contained(root, base_resolved):
            continue
        try:
            candidates = sorted(root.rglob("*.md"))
        except OSError:
            continue
        for entry in candidates:
            if len(files) >= STEERING_MAX_FILES:
                break
            if entry.name.startswith("."):
                continue
            linked = entry.is_symlink()
            try:
                resolved = entry.resolve(strict=True)
            except (OSError, RuntimeError):
                # RuntimeError is a symlink LOOP (pathlib raises it instead of
                # ELOOP) — reachable now that leaf links are resolved at all;
                # one loop.md must hide itself, not 500 the whole listing.
                continue
            # Reject symlinked intermediate directories that escape the trust
            # base (a leaf symlink whose target is still contained is fine).
            if not _contained(entry.parent, base_resolved):
                continue
            if linked:
                # A leaf symlink is listed READ-ONLY when its resolved target
                # passes the same admission gate the session loader applies
                # (regular file, not sensitive, under this source's link
                # trust base — see _link_trust_base: $HOME for `user`, the
                # steering root itself for `workspace`). For `user` these
                # files load into every session, so hiding them made the tab
                # disagree with what the agent actually reads. The key still
                # refuses to resolve for write (see resolve_steering_file),
                # so the entry is not editable.
                link_base = _link_trust_base(source, root, base_resolved)
                if not steering_target_admissible(resolved, link_base):
                    continue
            elif not resolved.is_file() or is_sensitive_path(str(resolved)):
                continue
            try:
                size = int(entry.stat().st_size)
            except OSError:
                continue
            rel = entry.relative_to(root).as_posix()
            # A linked document's head lives in its target, which can sit
            # outside the steering root — bind that read to the same link
            # trust base the admission gate above just checked it against.
            meta = (
                _head_meta(resolved, _link_trust_base(source, root, base_resolved))
                if linked
                else _head_meta(entry, root)
            )
            files.append({
                "key": f"{source}/{rel}",
                "name": entry.name,
                "rel": rel,
                "source": source,
                "path": _redact_meta(_display_path(entry)),
                "size": size,
                "description": _redact_meta(meta["description"]),
                # ``inclusion`` comes from a closed vocabulary, so it needs no
                # redaction; the two free-text fields beside it do.
                "inclusion": meta["inclusion"],
                "inclusion_declared": _redact_meta(meta["inclusion_declared"]),
                "file_match_pattern": _redact_meta(meta["file_match_pattern"]),
                "linked": linked,
                "editable": not linked,
                "target": _redact_meta(_display_path(resolved)) if linked else "",
            })
    return {
        "files": files,
        "roots": roots,
        "project": _redact_meta(_display_path(project_dir)) if project_dir else "",
    }


def _split_key(key: str) -> tuple[str, str] | None:
    """Split ``"<source>/<rel>"`` — rejecting traversal and odd input."""
    if not key or "\\" in key or "\x00" in key:
        return None
    source, _, rel = key.partition("/")
    if source not in STEERING_SOURCES or not rel:
        return None
    if rel.startswith("/") or rel.startswith("~") or ".." in rel.split("/"):
        return None
    if not rel.endswith(".md"):
        return None
    return source, rel


def _base_for(source: str, project_dir: Path | None) -> Path | None:
    """The trust base a steering root must resolve underneath."""
    if source == "user":
        return Path.home()
    return Path(project_dir) if project_dir else None


def _link_trust_base(source: str, root: Path, base_resolved: Path) -> Path:
    """The base a leaf symlink's resolved TARGET must stay under.

    ``user`` gets ``$HOME`` — the session loader's own anchor, and the parity
    this latitude exists for: those linked targets already load into every
    session. ``workspace`` has no such loader (kiro-cli reads that root
    itself), so a repository-committed link earns no latitude beyond the
    steering root: with the whole project as the base, ``leak.md -> ../../.env``
    would turn the steering GET into a verbatim reader for any file in the
    project.
    """
    return base_resolved if source == "user" else root


def _deepest_existing(path: Path) -> Path | None:
    """Walk up until an existing directory is found (bounded by the fs root)."""
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            return None
        probe = parent
    return probe


def _contained(candidate: Path, base_resolved: Path) -> bool:
    """True iff *candidate* resolves to ``base_resolved`` or below it.

    Resolving the deepest EXISTING ancestor and comparing against the trust
    base is what catches a symlinked intermediate directory (e.g. a
    ``~/.kiro/steering`` symlink pointing at ``/etc``) — comparing against the
    root itself would happily follow such a link.
    """
    probe = _deepest_existing(candidate)
    if probe is None:
        return False
    try:
        probe_resolved = probe.resolve(strict=True)
    except OSError:
        return False
    return probe_resolved == base_resolved or base_resolved in probe_resolved.parents


def resolve_steering_file(
    key: str,
    project_dir: Path | None,
    *,
    for_write: bool = False,
    follow_links: bool = False,
) -> Path | None:
    """Resolve ``key`` to an absolute steering file path, or None if rejected.

    With ``for_write`` the target need not exist yet (the deepest existing
    ancestor is validated instead) and write-protected locations are rejected
    too.  Without it the target must already be a regular file.

    ``follow_links`` is READ-only latitude: a leaf symlink resolves to its
    target when that target passes the loader's admission gate against this
    source's LINK trust base (regular file, not sensitive, under ``$HOME``
    for ``user`` or the steering root itself for ``workspace`` — see
    ``_link_trust_base``), matching what the listing shows. It is
    deliberately not the default: update and delete resolve with the default
    and must keep refusing a leaf symlink, or PUT would truncate — and DELETE
    unlink — a file that is not a steering document.
    """
    parts = _split_key(key)
    if parts is None:
        return None
    source, rel = parts
    root = next((p for s, p in steering_roots(project_dir) if s == source), None)
    base = _base_for(source, project_dir)
    if root is None or base is None:
        return None
    try:
        base_resolved = base.resolve(strict=True)
    except OSError:
        return None
    target = root / rel
    if is_sensitive_path(str(target)) or (for_write and is_sensitive_write_path(str(target))):
        return None
    if not _contained(target.parent, base_resolved):
        return None
    if for_write:
        return target
    # Reject a symlink at the LEAF outright — not just one escaping the trust
    # base. A link that still resolves inside the base (e.g.
    # ``.kiro/steering/rules.md -> ../../README.md``) would otherwise let PUT
    # truncate, and DELETE unlink, a file that is not a steering document.
    # The read path opts into following it via ``follow_links``, gated on the
    # loader's own admission predicate.
    try:
        if target.is_symlink():
            if not follow_links:
                return None
            resolved = target.resolve(strict=True)
            if not steering_target_admissible(
                resolved, _link_trust_base(source, root, base_resolved)
            ):
                return None
            return resolved
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError):
        # RuntimeError is pathlib's symlink-loop signal — a loop.md is "not
        # found", the same answer a dangling link gets.
        return None
    if not resolved.is_file() or is_sensitive_path(str(resolved)):
        return None
    if resolved != target and not _contained(resolved, base_resolved):
        return None
    return resolved


def _safe_rel_name(raw: str) -> str:
    """Normalize a user-supplied steering filename to a safe relative path."""
    name = _NAME_ALLOWED.sub("-", raw.strip()).strip("/").strip()
    name = re.sub(r"/+", "/", name)
    name = "/".join(seg for seg in name.split("/") if seg not in ("", ".", ".."))
    if not name:
        return ""
    if not name.endswith(".md"):
        name = f"{name}.md"
    return name


def _blocked(request: web.Request, operation: str) -> web.Response | None:
    """Restricted (incognito/guest) sessions may read steering but not write it."""
    if _is_restricted_session(request.app["state"], request):
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="restricted_session_block",
        )
        return web.json_response(
            {"error": "restricted session cannot modify steering files"}, status=403
        )
    return None


# ── Blocking filesystem transactions (run on the discovery pool) ──
#
# Each of these is ONE complete transaction — stat + open + write, or the
# identity check + truncate + write — so the whole thing lands off the event
# loop. A project on a slow or network filesystem must never stall the loop
# (and with it every chat and the heartbeat) for the duration of a write.
# They return a short error token; the handlers map tokens to HTTP status.


def _resolve_and_read_blocking(key: str, project_dir: Path | None) -> tuple[str, str, str | None]:
    """Resolve *key* and read it — one transaction, entirely off the event loop.

    Returns ``(content, display_path, error token or None)``.

    The read goes through ``hooks.safe_read_file_bytes_nolink()`` with
    ``within_root`` set to the steering root, which is what binds the bytes to
    the authorized location: the helper opens with ``O_NOFOLLOW``, ``fstat``s
    the descriptor (rejecting hardlinks and non-regular files), and then
    verifies the OPENED descriptor's real path resolves inside that root and is
    not sensitive. ``O_NOFOLLOW`` alone only guards the final path component,
    so without ``within_root`` an ancestor directory swapped for a symlink
    between resolution and open could still escape the tree.

    A LINKED entry (leaf symlink the listing admitted read-only) resolves to
    its target, which can sit outside the steering root — that read is bound
    to the source's LINK trust base instead (``$HOME`` for ``user``, the
    steering root itself for ``workspace`` — ``_link_trust_base``), the same
    anchor ``steering_target_admissible`` admits the target against. The
    descriptor checks still apply to the inode actually opened, so a link
    retargeted after resolution can reach nothing outside the base the
    listing admitted from.

    The ``lstat`` below only supplies the size for the 413 message; the file
    can still grow past the cap before the descriptor read, in which case the
    helper raises ``FileTooLargeError`` — caught here so that race yields 413
    rather than a 500.
    """
    target = resolve_steering_file(key, project_dir, follow_links=True)
    if target is None:
        return "", "", "notfound"
    display = _redact_meta(_display_path(target))
    parts = _split_key(key)
    root = (
        next((p for s, p in steering_roots(project_dir) if s == parts[0]), None) if parts else None
    )
    if root is None or parts is None:
        return "", display, "notfound"
    # The nominal path and the resolved one disagree exactly when the leaf is a
    # link — recheck the leaf itself rather than comparing paths, since an
    # ordinary file can also resolve elsewhere through allowed ancestor links.
    try:
        linked = (root / parts[1]).is_symlink()
    except OSError:
        linked = False
    base = _base_for(parts[0], project_dir)
    if base is None:
        return "", display, "notfound"
    within_root = _link_trust_base(parts[0], root, base) if linked else root
    try:
        pre = target.lstat()
    except OSError:
        return "", display, "notfound"
    if stat.S_ISLNK(pre.st_mode) or not stat.S_ISREG(pre.st_mode):
        return "", display, "notfound"
    if pre.st_size > STEERING_FILE_MAX_BYTES:
        return "", display, f"toolarge:{pre.st_size}"
    try:
        data = safe_read_file_bytes_nolink(
            str(target), within_root=str(within_root), max_bytes=STEERING_FILE_MAX_BYTES
        )
    except FileTooLargeError:
        # Grew past the cap between the lstat and the descriptor read.
        return "", display, f"toolarge:>{STEERING_FILE_MAX_BYTES}"
    if data is None:
        return "", display, "readfailed"
    return data.decode("utf-8", errors="replace"), display, None


def _create_file_blocking(target: Path, content: str) -> tuple[str | None, str]:
    """Create *target* with *content*; return ``(error token or None, display path)``."""
    display = _redact_meta(_display_path(target))
    if not _DIR_FD_SUPPORTED:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # O_EXCL — never clobber a file that appeared between the check and
            # the write, and never follow a symlink planted at the target path
            # (O_EXCL already refuses an existing symlink, so this is safe where
            # O_NOFOLLOW is unavailable).
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
            # newline="": steering documents round-trip through the editor, and
            # Windows newline translation on every save would accumulate carriage
            # returns (CRLF -> CR CR LF -> ...). Write exactly what was sent.
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(content)
        except FileExistsError:
            return "exists", display
        except OSError as exc:
            logger.warning("steering create failed: %s", type(exc).__name__)
            return "writefailed", display
        return None, display

    # Ensure the tree by name first (the same contract prompts uses: callers
    # create their own tree roots), then create only the leaf relative to a
    # descriptor pinning the parent chain, so a directory swapped for a link
    # after resolution cannot redirect the create.
    #
    # open_dir_pinned here, NOT pin_parent as the update and delete paths use.
    # Those two are handed the RESOLVED leaf, so pin_parent can walk the chain
    # resolution already canonicalized. Create is handed ``root / rel`` with
    # ``for_write`` True -- the target need not exist yet and only the deepest
    # EXISTING ancestor was resolved, for containment -- so there is no canonical
    # chain to be faithful to and open_dir_pinned's own realpath is the first
    # resolution rather than a second one. pin_parent on this path would also
    # refuse the symlinked ancestors ``_contained`` deliberately allows above the
    # trust base (a symlinked $HOME, /tmp on macOS) and break create outright.
    # What remains is the by-name mkdir(parents=True) below, which follows a link
    # already sitting on the chain; that is the floor create inherits.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        dir_fd = pinned_fs.open_dir_pinned(target.parent, what="steering directory")
    except pinned_fs.PinnedPathRefusal:
        # A linked or non-directory ancestor -- steering collapses to writefailed
        # rather than distinguishing a linked-root token here.
        return "writefailed", display
    except OSError as exc:
        logger.warning("steering create failed: %s", type(exc).__name__)
        return "writefailed", display
    try:
        try:
            # O_EXCL keeps create-if-absent atomic; O_NOFOLLOW refuses a symlink at
            # the leaf. O_BINARY (Windows-only, unreachable here since that platform
            # takes the by-name floor) keeps os.write from translating \n to \r\n --
            # the newline="" byte-exactness the fdopen path above gets from newline.
            fd = os.open(
                target.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | getattr(os, "O_BINARY", 0),
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            return "exists", display
        leaf: os.stat_result | None = None
        try:
            # Identity of the inode THIS call created, read from the descriptor while
            # it is provably ours. The rollback below addresses a NAME, and a rival
            # can unlink ours and create its own inside the failure window, so this
            # is what makes the cleanup remove this object or nothing.
            #
            # Captured INSIDE the guarded region: an fstat that fails (EIO or ESTALE on
            # a network filesystem) must be rolled back like any other failure, not
            # escape leaving the O_EXCL name on disk for every retry to trip over.
            leaf = os.fstat(fd)
            data = content.encode("utf-8")
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            # Closed INSIDE the guarded region, not in the finally below. A deferred
            # write error is reported by close(), not by write() -- ENOSPC once the
            # last block is flushed, EIO on NFS -- so a close in `finally` raises
            # after this cleanup arm has been skipped and leaves the partial body
            # under an O_EXCL name, which is the permanent "exists" the arm exists to
            # prevent. Cleared first so the fallback close cannot double-close, the
            # same shape atomic_write uses before its own publish.
            fd, open_fd = -1, fd
            os.close(open_fd)
        except BaseException:
            # Partial body under an O_EXCL name: remove it relative to the same
            # descriptor, or a retry answers "exists" forever. Verified against the
            # inode this call created, so a rival that replaced the name inside the
            # failure window keeps ITS file -- "remove this object, or nothing",
            # rather than "remove whatever answers to this name". The residual is
            # irreducible and stated in unlink_verified: POSIX has no
            # unlink-by-inode, so the stat and the unlink remain two syscalls.
            #
            # No unlink runs without that identity. Where the first fstat failed the
            # descriptor is still open (the close is the last statement of the guarded
            # region, and it clears fd first), so the identity is asked for once more
            # THROUGH IT -- an EIO or ESTALE on a network filesystem is usually
            # transient, and a second answer is a free way to reach the verified arm.
            identity = leaf
            if identity is None and fd >= 0:
                with suppress(OSError):
                    identity = os.fstat(fd)
            if identity is not None:
                pinned_fs.unlink_verified(dir_fd, target.name, (identity.st_dev, identity.st_ino))
            # Otherwise the name stays. Two fstats on a descriptor this call owns both
            # failing is the only way here, and it costs an EMPTY document rather than a
            # truncated one -- the fstat precedes the first os.write, so nothing was
            # written when there is no identity. An empty steering document is listed,
            # editable and deletable from the dashboard, so the recovery is a save
            # rather than a shell; unlinking whatever answers to the name would instead
            # destroy a rival's file, which is content this code has never read.
            raise
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError as exc:
        logger.warning("steering create failed: %s", type(exc).__name__)
        return "writefailed", display
    finally:
        os.close(dir_fd)
    return None, display


def _update_file_blocking(target: Path, content: str) -> str | None:
    """Overwrite *target* with *content* atomically; return an error token or None.

    The write goes through ``atomic_write()`` (unique temp file in the same
    directory, then ``os.replace``) rather than truncate-then-write: on a nearly
    full filesystem a truncate followed by a failed or partial write would
    destroy the user's existing steering document, and the whole point of this
    endpoint is that the file is the user's own content.

    ``os.replace`` swaps the directory entry rather than writing through it, so
    it cannot follow a symlink raced into place after the check below — and no
    truncate happens at all, which is why this needs no descriptor-identity
    check the way the old in-place path did.
    """
    try:
        pre = target.lstat()
    except OSError:
        return "notfound"
    if stat.S_ISLNK(pre.st_mode) or not stat.S_ISREG(pre.st_mode):
        return "notfound"
    # Pin the parent chain and hand atomic_write the descriptor so its temp
    # create and publishing rename run relative to it -- the mkstemp + replace
    # are otherwise by-name, leaving an ancestor swapped after the lstat above
    # able to redirect where the replacement lands. os.replace already swaps the
    # directory entry rather than following the leaf, so this closes the residual
    # ancestor window without weakening the ACL carry below.
    #
    # BEFORE the access-control source is opened, so the leaf is only ever
    # addressed through this descriptor: a directory replaced at the parent's name
    # in between would otherwise supply the mode and ACL while the write published
    # into the pinned original.
    #
    # pin_parent, NOT open_dir_pinned: ``resolve_steering_file`` hands the update
    # path the RESOLVED leaf (``for_write`` False), so this parent chain is already
    # canonical and pin_parent walks exactly it, refusing a component that has
    # since become a link. Resolving it again here would follow that link instead.
    #
    # None where the platform cannot walk a parent by descriptor or cannot stage
    # and rename through one: atomic_write refuses a descriptor it cannot use, so
    # both capabilities are asked rather than assumed to move together.
    dir_fd: int | None = None
    if _DIR_FD_SUPPORTED and pinned_parent_replace_supported():
        try:
            dir_fd = pinned_fs.pin_parent(str(target.parent), what="steering directory")
        except pinned_fs.PinnedPathRefusal:
            return "writefailed"
        except OSError:
            return "notfound"
    src_fd: int | None = None
    try:
        # Hand atomic_write a descriptor on the existing file so it can read the
        # access-control xattrs off it. mode= carries permission BITS only, so a
        # named POSIX ACL (system.posix_acl_access) the owner
        # set is otherwise silently dropped the moment the replace installs a fresh
        # inode -- handing back a file protected more narrowly than the one it
        # replaced.
        #
        # Without a pinned parent open_access_control_source returns None on a
        # platform with no xattr syscalls, which is what keeps this write working
        # on Windows: nothing to carry there, and a read handle held open across
        # the write would make os.replace fail with PermissionError on every save.
        try:
            src_fd = open_access_control_source(target, dir_fd=dir_fd)
        except OSError:
            # The file vanished or turned into a link between the lstat above and
            # here; treat it the same as the lstat miss above.
            return "notfound"
        # Preserve the file's existing permissions rather than forcing 0o600:
        # the old in-place write inherited them, and a project steering file
        # checked out group-readable should not be silently tightened by a save.
        # preserve_access_control_from is ADDITIVE to mode=: bits plus the ACL.
        #
        # Read off the descriptor where there is one, so the bits and the ACL come
        # from the same inode. ``pre`` is the by-name lstat above and is only the
        # fallback on the unpinned floor, where nothing was pinned to disagree
        # with it.
        mode = stat.S_IMODE(os.fstat(src_fd).st_mode) if src_fd is not None else (
            stat.S_IMODE(pre.st_mode)
        )
        atomic_write(
            target,
            content,
            fsync=True,
            mode=mode,
            newline="",
            preserve_access_control_from=src_fd,
            parent_dir_fd=dir_fd,
        )
    except OSError as exc:
        logger.warning("steering update failed: %s", type(exc).__name__)
        return "writefailed"
    finally:
        for fd in (src_fd, dir_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    return None


class _DeclarationError(ValueError):
    """A mode edit the endpoint refuses, carrying its machine-readable code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _apply_declaration(content: str, body: dict[str, Any]) -> str:
    """Apply the request's optional mode edit to *content*'s front matter.

    Absent field → left alone. Empty string → the key is REMOVED, which is how
    a document goes back to having no declaration at all (implicitly
    ``always``). Anything else is written verbatim.

    Done server-side rather than by having the editor splice YAML into its own
    textarea: the body is the user's document, the writer preserves it byte for
    byte, and a client that got the splice subtly wrong would corrupt a file
    whose whole purpose is to be read by the agent.
    """
    updates: dict[str, str | None] = {}
    for field, key in STEERING_WRITABLE_FIELDS:
        if field not in body:
            continue
        raw = body[field]
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raise _DeclarationError(f"{field} must be a string", "steering_field_type")
        value = raw.strip()
        if len(value) > _STEERING_META_MAX_CHARS:
            raise _DeclarationError(
                f"{field} exceeds {_STEERING_META_MAX_CHARS} characters",
                "steering_field_too_long",
            )
        updates[key] = value or None
    if not updates:
        return content

    if "inclusion" in updates:
        declared = updates["inclusion"]
        if declared is not None and declared.lower() not in _INCLUSION_CANONICAL:
            raise _DeclarationError(
                f"unknown inclusion mode {declared!r}; expected one of "
                + ", ".join(STEERING_INCLUSION_MODES),
                "steering_unknown_inclusion",
            )
        if declared is not None:
            updates["inclusion"] = _INCLUSION_CANONICAL[declared.lower()]

    try:
        out = set_frontmatter_fields(content, updates, STEERING_LOADER)
    except ValueError as exc:
        raise _DeclarationError(str(exc), "steering_field_unrepresentable") from exc

    # A fileMatch document with no pattern can never match, so it would be
    # withheld forever with nothing to explain why. Checked against the RESULT,
    # so a request that only flips the mode passes when the document already
    # carries a pattern.
    fields, _ = split_frontmatter(out, STEERING_LOADER)
    resolved = _INCLUSION_CANONICAL.get(fields.get("inclusion", "").strip().lower(), "")
    if resolved == "fileMatch" and not fields.get("fileMatchPattern", "").strip():
        raise _DeclarationError(
            "fileMatch needs a fileMatchPattern; without one the document never applies",
            "steering_file_match_needs_pattern",
        )
    return out


def _delete_file_blocking(target: Path) -> str | None:
    """Unlink *target*; return an error token or None.

    ``resolve_steering_file`` hands back the RESOLVED leaf here (``for_write``
    False), so *target* is symlink-free by construction. ``unlink`` never follows
    a symlink either, so a link raced into place after resolution loses only the
    link itself. When the platform supports it the unlink runs relative to a
    descriptor pinning the parent chain, which additionally closes the window in
    which an ancestor directory is swapped for a link between resolution and the
    unlink: ``pin_parent`` walks the chain that resolution already canonicalized,
    so a component that has since become a link fails ``O_NOFOLLOW`` and the
    delete is refused rather than redirected.
    """
    if not _DIR_FD_SUPPORTED:
        try:
            target.unlink()
        except FileNotFoundError:
            return "notfound"
        except OSError as exc:
            logger.warning("steering delete failed: %s", type(exc).__name__)
            return "deletefailed"
        return None

    try:
        dir_fd = pinned_fs.pin_parent(str(target.parent), what="steering directory")
    except pinned_fs.PinnedPathRefusal:
        # A linked or non-directory ancestor means the file the caller resolved is
        # no longer reachable through the tree it named -- the same outcome as a
        # by-name unlink finding it gone.
        return "notfound"
    except FileNotFoundError:
        return "notfound"
    except OSError as exc:
        logger.warning("steering delete failed: %s", type(exc).__name__)
        return "deletefailed"
    try:
        os.unlink(target.name, dir_fd=dir_fd)
    except FileNotFoundError:
        return "notfound"
    except OSError as exc:
        logger.warning("steering delete failed: %s", type(exc).__name__)
        return "deletefailed"
    finally:
        os.close(dir_fd)
    return None


def _resolve_blocking(key: str, project_dir: Path | None, for_write: bool = False) -> Path | None:
    """Positional wrapper so ``resolve_steering_file`` can go through ``_offload``.

    Resolution itself is filesystem metadata work (``is_dir``, ``lstat``,
    ``resolve``) and must not run on the event loop either — on a
    network-backed project even a stat storm is enough to stall it.
    """
    return resolve_steering_file(key, project_dir, for_write=for_write)


def _offload(fn: Any, *args: Any) -> Any:
    """Run a blocking steering transaction on the dashboard discovery pool."""
    return asyncio.get_running_loop().run_in_executor(discovery_executor(), fn, *args)


async def api_steering(request: web.Request) -> web.Response:
    """GET /api/steering — list the effective steering files (both roots)."""
    state: DashboardState = request.app["state"]
    project_dir, project_state = active_project_state(state, _read_session_key(request))
    # rglob + per-file stat/head-read over two roots is browser-triggerable
    # blocking FS work: keep it off the event loop (same pool as /api/skills).
    result = await _offload(list_steering_blocking, project_dir)
    # Why there is no project, not just that there isn't one: the tab disables
    # workspace scope either way, but "no project set" and "your open chats are
    # on different projects" are different problems, and a UI told only ``None``
    # reports the first when it means the second.
    result["project_state"] = project_state
    # Travels back on every workspace write as a precondition — see
    # _project_precondition().
    result["project_key"] = _project_key(project_dir)
    _sel().log_tool_invocation(
        session_key="",
        agent="api",
        source="dashboard",
        tool_name="api_steering_list",
        tool_kind="steering",
        outcome="ok",
        metadata={"count": len(result["files"])},
    )
    return web.json_response(result)


async def api_steering_create(request: web.Request) -> web.Response:
    """POST /api/steering — create a steering file.

    Body: ``{name, content, source?}`` — ``source`` defaults to ``workspace``
    when a project directory is active, else ``user``.
    """
    denied = _blocked(request, "steering.create")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    content = body.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return web.json_response({"error": "content is required"}, status=400)
    if len(content.encode("utf-8")) > STEERING_FILE_MAX_BYTES:
        return web.json_response(
            {"error": f"content too large (cap {STEERING_FILE_MAX_BYTES} bytes)"}, status=413
        )
    project_dir, project_state = active_project_state(state, _read_session_key(request))
    source = str(body.get("source") or ("workspace" if project_dir else "user"))
    if source not in STEERING_SOURCES:
        return web.json_response({"error": "invalid source"}, status=400)
    if source == "workspace" and project_dir is None:
        # Refuse rather than write to a guess. ``reason`` lets the dialog say
        # which fix applies — bind a project, or close the chat that disagrees —
        # instead of reporting an unexplained rejection.
        detail = (
            "open chats are bound to different projects, so there is no single "
            "project to write to"
            if project_state == "ambiguous"
            else "no project is set for this chat"
        )
        # No `reason` field: nothing reads it. The cause-specific `detail` above
        # and `project_state` on the listing already carry which case this is,
        # and a field with no consumer is a contract nobody is holding up.
        return web.json_response(
            {
                "error": f"cannot create a workspace steering file: {detail}",
                "code": "steering_workspace_unavailable",
            },
            status=400,
        )
    rel = _safe_rel_name(str(body.get("name", "")))
    if not rel:
        return web.json_response({"error": "name is required"}, status=400)
    if source == "workspace":
        # Same precondition as the detail writes: creating into a project the
        # client is no longer looking at plants the file where nobody will find
        # it, and can overwrite-by-name in the project it landed in.
        stale = _project_precondition(request, project_dir, "steering.create")
        if stale is not None:
            return stale
    key = f"{source}/{rel}"
    target = await _offload(_resolve_blocking, key, project_dir, True)
    if target is None:
        return web.json_response({"error": "invalid steering path"}, status=400)
    err, display = await _offload(_create_file_blocking, target, content)
    if err == "exists":
        return web.json_response({"error": f"'{rel}' already exists"}, status=409)
    if err is not None:
        return web.json_response({"error": "write failed"}, status=500)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="steering.create",
        outcome="success",
        source="dashboard",
        resources=key,
    )
    return web.json_response({"ok": True, "key": key, "path": display})


def _project_key(project_dir: Path | None) -> str:
    """Opaque fingerprint of the project a workspace-scoped response resolved to.

    A digest of the absolute path rather than the path itself: it travels back on
    a mutation as a precondition, and the display path is both lossy (``~``
    collapsed, credential-redacted) and needlessly revealing for a value whose
    only job is equality.
    """
    if project_dir is None:
        return ""
    return hashlib.sha256(str(project_dir).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _project_precondition(
    request: web.Request, project_dir: Path | None, operation: str
) -> web.Response | None:
    """Refuse a workspace-scoped write whose project is not the one the client saw.

    A chat slot's project is MUTABLE: the tab lists project A, the slot is
    re-pointed at B, and a delete issued from the still-visible listing resolves
    B and removes B's file of the same name. The session key alone cannot close
    that — it names a slot, and the slot is exactly what moved — so a workspace
    write must state which project it believed it was acting on and be refused
    when the server resolves a different one. Read-only paths are unguarded: a
    stale read shows the wrong file but destroys nothing, and the save that would
    act on it comes back through here.

    ``409``, not ``400``: the request was well-formed and the client's view was
    simply superseded, which is what tells the UI to refresh rather than to
    correct the payload. An ABSENT header fails closed for the same reason the
    resolver does — a caller that cannot say which project it meant has not
    earned a write to one.
    """
    seen = request.headers.get(STEERING_PROJECT_HEADER, "")
    actual = _project_key(project_dir)
    if seen and actual and seen == actual:
        return None
    # Audited like any other refusal to write (see _blocked()): a denial nobody
    # records is a denial nobody can review, and a stale-header burst is exactly
    # the shape a confused — or hostile — client produces. Enqueued rather than
    # synchronous because the write is already refused, so this record gates
    # nothing; it reports a decision already made.
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation=operation,
        outcome="denied",
        source="dashboard",
        resources="steering_project_changed",
    )
    return web.json_response(
        {
            # States the fact, not the remedy: the remedy differs by verb (a
            # mid-edit save cannot be retried, a create/delete just needs the
            # refreshed list), so the client supplies localized copy keyed off
            # `code` and this text is the diagnostic fallback.
            "error": (
                "the project this steering file belongs to is no longer the " "active project"
            ),
            "code": "steering_project_changed",
        },
        status=409,
    )


async def api_steering_detail(request: web.Request) -> web.Response:
    """GET/PUT/DELETE /api/steering/{key} — read, update, or delete one file."""
    state: DashboardState = request.app["state"]
    key = request.match_info["key"]
    project_dir = active_project_dir(state, _read_session_key(request))
    # Only ``workspace/`` keys resolve against the mutable project; ``user/``
    # keys are anchored to $HOME and need no precondition.
    workspace_scoped = key.split("/", 1)[0] == "workspace"

    if request.method == "GET":

        def _audit(outcome: str) -> None:
            _sel().log_tool_invocation(
                session_key="",
                agent="api",
                source="dashboard",
                tool_name="api_steering_read",
                tool_kind="steering",
                outcome=outcome,
                metadata={"key": key},
            )

        # Resolve + read as ONE offloaded transaction: the read needs the
        # steering root to pass as ``within_root``, and splitting them would
        # widen the check-to-use window between resolution and open.
        content, display, err = await _offload(_resolve_and_read_blocking, key, project_dir)
        if err and err.startswith("toolarge:"):
            _audit("too_large")
            size = err.split(":", 1)[1]
            return web.json_response(
                {"error": f"file too large ({size} bytes; cap {STEERING_FILE_MAX_BYTES})"},
                status=413,
            )
        if err is not None:
            _audit("not_found")
            return web.json_response({"error": "not found"}, status=404)
        _audit("ok")
        # Content is returned verbatim, NOT credential-redacted, and that is
        # deliberate: this response populates the editor and is written straight
        # back on save, so redacting here would overwrite the user's own file
        # with [REDACTED] markers — a data-loss bug traded for no real
        # confidentiality gain, since the recipient is the same local OS user who
        # owns the file. Same reasoning (and same behavior) as api_skill_detail.
        # Listing metadata IS redacted — see _redact_meta().
        return web.json_response(
            {
                "key": key,
                "content": content,
                "path": display,
                "source": key.split("/", 1)[0],
            }
        )

    if request.method == "DELETE":
        denied = _blocked(request, "steering.delete")
        if denied is not None:
            return denied
        if workspace_scoped:
            stale = _project_precondition(request, project_dir, "steering.delete")
            if stale is not None:
                return stale
        target = await _offload(_resolve_blocking, key, project_dir, False)
        if target is None:
            return web.json_response({"error": "not found"}, status=404)
        err = await _offload(_delete_file_blocking, target)
        if err == "notfound":
            return web.json_response({"error": "not found"}, status=404)
        if err is not None:
            return web.json_response({"error": "delete failed"}, status=500)
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="steering.delete",
            outcome="success",
            source="dashboard",
            resources=key,
        )
        return web.json_response({"ok": True})

    # PUT
    denied = _blocked(request, "steering.update")
    if denied is not None:
        return denied
    if workspace_scoped:
        stale = _project_precondition(request, project_dir, "steering.update")
        if stale is not None:
            return stale
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    content = body.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return web.json_response({"error": "content is required"}, status=400)
    try:
        content = _apply_declaration(content, body)
    except _DeclarationError as exc:
        return web.json_response({"error": str(exc), "code": exc.code}, status=400)
    if len(content.encode("utf-8")) > STEERING_FILE_MAX_BYTES:
        return web.json_response(
            {"error": f"content too large (cap {STEERING_FILE_MAX_BYTES} bytes)"}, status=413
        )
    target = await _offload(_resolve_blocking, key, project_dir, False)
    if target is None:
        return web.json_response({"error": "not found"}, status=404)
    err = await _offload(_update_file_blocking, target, content)
    if err == "notfound":
        return web.json_response({"error": "not found"}, status=404)
    if err is not None:
        return web.json_response({"error": "write failed"}, status=500)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="steering.update",
        outcome="success",
        source="dashboard",
        resources=key,
    )
    # Echo the stored text and its resolved declaration. A mode edit rewrites
    # the front matter server-side, so the editor that sent the OLD text would
    # otherwise keep showing it and re-send it on the next save, undoing the
    # change it just made.
    fields, _ = split_frontmatter(content, STEERING_LOADER)
    declared = fields.get("inclusion", "").strip()
    return web.json_response(
        {
            "ok": True,
            "content": content,
            "inclusion": _INCLUSION_CANONICAL.get(declared.lower(), STEERING_INCLUSION_DEFAULT),
            "inclusion_declared": declared[:_STEERING_META_MAX_CHARS],
            "file_match_pattern": fields.get("fileMatchPattern", "").strip()[
                :_STEERING_META_MAX_CHARS
            ],
        }
    )
