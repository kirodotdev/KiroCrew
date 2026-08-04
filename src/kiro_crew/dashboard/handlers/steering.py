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
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.atomic_write import atomic_write
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.security import (
    is_sensitive_path,
    is_sensitive_write_path,
    redact_credentials,
    redact_exfiltration_urls,
)

from ._shared import _is_restricted_session, _read_session_key, active_project_dir

logger = logging.getLogger(__name__)

# Hard caps — keep the endpoints bounded regardless of what is on disk.
STEERING_MAX_FILES = 500
STEERING_FILE_MAX_BYTES = 262_144  # 256 KiB per steering document

# ``user`` → ~/.kiro/steering, ``workspace`` → <project>/.kiro/steering
STEERING_SOURCES = ("user", "workspace")

# ``O_NOFOLLOW`` does not exist on Windows — ``getattr`` keeps the flag optional
# so a write there raises no AttributeError. Where the flag is absent the write
# paths fall back to an lstat/open/fstat identity check (same defense, one extra
# syscall) rather than trusting the kernel to refuse the symlink.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

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


def _first_heading(path: Path, cap: int = 2048) -> str:
    """Cheap description: the first markdown heading (or first prose line)."""
    try:
        with path.open("rb") as f:
            head = f.read(cap).decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in head.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "---":
            continue
        if stripped.startswith("#"):
            return re.sub(r"^#+\s*", "", stripped).strip()[:200]
        return stripped[:200]
    return ""


def list_steering_blocking(project_dir: Path | None = None) -> dict[str, Any]:
    """Blocking scan of both steering roots — run on the discovery pool.

    Returns ``{"files": [...], "roots": [...], "project": "<display path>"}``.
    Each file entry: ``{key, name, rel, source, path, size, description}``
    where ``key`` is ``"<source>/<rel>"``.
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
            # Symlinked entries are never listed: the key they would produce
            # must not be resolvable for write, so listing one would offer the
            # user a file they cannot edit (see resolve_steering_file).
            if entry.is_symlink():
                continue
            try:
                resolved = entry.resolve(strict=True)
            except OSError:
                continue
            # Reject symlinked intermediate directories that escape the trust
            # base (a leaf symlink whose target is still contained is fine).
            if not _contained(entry.parent, base_resolved):
                continue
            if not resolved.is_file() or is_sensitive_path(str(resolved)):
                continue
            try:
                size = int(entry.stat().st_size)
            except OSError:
                continue
            rel = entry.relative_to(root).as_posix()
            files.append({
                "key": f"{source}/{rel}",
                "name": entry.name,
                "rel": rel,
                "source": source,
                "path": _redact_meta(_display_path(entry)),
                "size": size,
                "description": _redact_meta(_first_heading(entry)),
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
    key: str, project_dir: Path | None, *, for_write: bool = False
) -> Path | None:
    """Resolve ``key`` to an absolute steering file path, or None if rejected.

    With ``for_write`` the target need not exist yet (the deepest existing
    ancestor is validated instead) and write-protected locations are rejected
    too.  Without it the target must already be a regular file.
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
    try:
        if target.is_symlink():
            return None
        resolved = target.resolve(strict=True)
    except OSError:
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


def _resolve_and_read_blocking(
    key: str, project_dir: Path | None
) -> tuple[str, str, str | None]:
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

    The ``lstat`` below only supplies the size for the 413 message; the file
    can still grow past the cap before the descriptor read, in which case the
    helper raises ``FileTooLargeError`` — caught here so that race yields 413
    rather than a 500.
    """
    target = resolve_steering_file(key, project_dir)
    if target is None:
        return "", "", "notfound"
    display = _redact_meta(_display_path(target))
    parts = _split_key(key)
    root = (
        next((p for s, p in steering_roots(project_dir) if s == parts[0]), None)
        if parts
        else None
    )
    if root is None:
        return "", display, "notfound"
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
            str(target), within_root=str(root), max_bytes=STEERING_FILE_MAX_BYTES
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
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL — never clobber a file that appeared between the check and the
        # write, and never follow a symlink planted at the target path (O_EXCL
        # already refuses an existing symlink, so this is safe where
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
    try:
        # Preserve the file's existing permissions rather than forcing 0o600:
        # the old in-place write inherited them, and a project steering file
        # checked out group-readable should not be silently tightened by a save.
        atomic_write(target, content, fsync=True, mode=stat.S_IMODE(pre.st_mode), newline="")
    except OSError as exc:
        logger.warning("steering update failed: %s", type(exc).__name__)
        return "writefailed"
    return None


def _delete_file_blocking(target: Path) -> str | None:
    """Unlink *target*; return an error token or None.

    ``unlink`` never follows a symlink, so a link raced into place after
    resolution loses only the link itself.
    """
    try:
        target.unlink()
    except FileNotFoundError:
        return "notfound"
    except OSError as exc:
        logger.warning("steering delete failed: %s", type(exc).__name__)
        return "deletefailed"
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
    project_dir = active_project_dir(state, _read_session_key(request))
    # rglob + per-file stat/head-read over two roots is browser-triggerable
    # blocking FS work: keep it off the event loop (same pool as /api/skills).
    result = await _offload(list_steering_blocking, project_dir)
    _sel().log_tool_invocation(
        session_key='', agent='api', source='dashboard',
        tool_name='api_steering_list', tool_kind='steering', outcome='ok',
        metadata={'count': len(result["files"])},
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
    project_dir = active_project_dir(state, _read_session_key(request))
    source = str(body.get("source") or ("workspace" if project_dir else "user"))
    if source not in STEERING_SOURCES:
        return web.json_response({"error": "invalid source"}, status=400)
    if source == "workspace" and project_dir is None:
        # Either no project is set, or open chats disagree about which one —
        # see active_project_dir(). Refuse rather than write to a guess.
        return web.json_response(
            {"error": "no unambiguous project directory for this workspace"}, status=400
        )
    rel = _safe_rel_name(str(body.get("name", "")))
    if not rel:
        return web.json_response({"error": "name is required"}, status=400)
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


async def api_steering_detail(request: web.Request) -> web.Response:
    """GET/PUT/DELETE /api/steering/{key} — read, update, or delete one file."""
    state: DashboardState = request.app["state"]
    key = request.match_info["key"]
    project_dir = active_project_dir(state, _read_session_key(request))

    if request.method == "GET":

        def _audit(outcome: str) -> None:
            _sel().log_tool_invocation(
                session_key='', agent='api', source='dashboard',
                tool_name='api_steering_read', tool_kind='steering', outcome=outcome,
                metadata={'key': key},
            )

        # Resolve + read as ONE offloaded transaction: the read needs the
        # steering root to pass as ``within_root``, and splitting them would
        # widen the check-to-use window between resolution and open.
        content, display, err = await _offload(_resolve_and_read_blocking, key, project_dir)
        if err and err.startswith("toolarge:"):
            _audit('too_large')
            size = err.split(":", 1)[1]
            return web.json_response(
                {"error": f"file too large ({size} bytes; cap {STEERING_FILE_MAX_BYTES})"},
                status=413,
            )
        if err is not None:
            _audit('not_found')
            return web.json_response({"error": "not found"}, status=404)
        _audit('ok')
        # Content is returned verbatim, NOT credential-redacted, and that is
        # deliberate: this response populates the editor and is written straight
        # back on save, so redacting here would overwrite the user's own file
        # with [REDACTED] markers — a data-loss bug traded for no real
        # confidentiality gain, since the recipient is the same local OS user who
        # owns the file. Same reasoning (and same behavior) as api_skill_detail.
        # Listing metadata IS redacted — see _redact_meta().
        return web.json_response({
            "key": key,
            "content": content,
            "path": display,
            "source": key.split("/", 1)[0],
        })

    if request.method == "DELETE":
        denied = _blocked(request, "steering.delete")
        if denied is not None:
            return denied
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
    return web.json_response({"ok": True})
