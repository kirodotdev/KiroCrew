"""``upload {ref, path}``: put local files into a page's ``<input type=file>``.

``path`` is one absolute path, or a list of up to :data:`UPLOAD_MAX_FILES` for an
input that takes several (``multiple``).

The gateway and the browser run outside the agent's sandbox, so a file the
browser reads is one the agent itself might not be allowed to read. The op
therefore never hands the browser the agent's path. It copies the file with
``hooks.safe_copy_file_nolink`` (one descriptor, refused unless it is a regular
file with one link whose OPENED real path is outside every sensitive location)
into a staging leaf the agent cannot read or write (``OpContext.upload_dir``,
under the hidden IPC leaf), and gives the browser the COPY. What was checked is
what the page gets: a link or file the agent swaps in after the check never
reaches the browser.

Before the copy, :func:`_check_path` names the refusal: the path must be absolute,
and ``lstat`` must find one regular file, not a link, a folder or a device, at most
:data:`UPLOAD_MAX_BYTES` long. The target ref must be a visible, enabled
``<input type=file>`` (the walker's ``fileInput``), and the helper worker sets it
with CDP ``DOM.setFileInputFiles`` on that node (``helper_worker._local_set_files``),
which fires the page's own ``input`` and ``change`` events.

Several files each pass the same checks and are copied into ONE staging folder
(:func:`stage_many`): two with one name are refused, and together they are at most
:data:`UPLOAD_MAX_BYTES`. A list stays under ``path`` so the tool gate's path
keystone, which reads every string under ``path``, judges each file before the card.

Every attempt that reaches the path check writes one SEL row per file: the path,
the size and an outcome word, never the content. ``ops._upload`` reaches this module by a
call-time import.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import stat
import time
from pathlib import Path
from typing import Any

from kiro_crew.browser_harness import ops as _ops
from kiro_crew.browser_harness.frames import split_frame_ref

logger = logging.getLogger(__name__)

#: Largest file one ``upload`` sends. A test fixture or a document is far below it.
#: shortcut: one fixed cap; make it a setting if real apps need larger files.
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
#: Most files one ``upload`` sends (``helper_worker._MAX_FILES`` is the same).
UPLOAD_MAX_FILES = 10
#: Staged copies kept for the page to read when its form submits: the newest
#: :data:`UPLOAD_KEEP`, none older than :data:`UPLOAD_KEEP_S`.
UPLOAD_KEEP = 20
UPLOAD_KEEP_S = 24 * 3600


class _PathRefused(Exception):
    def __init__(self, code: str, error: str) -> None:
        super().__init__(code)
        self.code = code
        self.error = error


def _audit(path: str, size: int | None, outcome: str) -> None:
    """SEL row: the path, its size and an outcome word. Never the file's content."""
    try:
        from kiro_crew.sel import sel

        resource = f"path={path}" + (f" size={size}" if size is not None else "")
        sel().log_api_access(
            caller="browser",
            operation="browser.upload",
            outcome=outcome,
            source="browser",
            resources=resource,
        )
    except Exception:  # noqa: BLE001 - auditing never fails the op
        logger.debug("browser upload audit failed", exc_info=True)


def _check_path(path: Any) -> str:
    """The path as given, once :func:`os.lstat` finds one regular file there.

    Only the refusal WORD comes from here: the decision on what is copied is
    ``hooks.safe_copy_file_nolink``'s, made again on the descriptor it opens. A
    folder is ``path_not_file`` on every OS because ``lstat`` judges it before any
    open (on Windows ``os.open`` on a folder fails with ``EACCES``).
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        raise _PathRefused("bad_path", "upload needs `path`: the absolute path of one file")
    if not os.path.isabs(path):
        raise _PathRefused("bad_path", f"`path` must be absolute: {path!r}")
    from kiro_crew import hooks

    # The shared path screen before any probe of the disk: on Windows an ``lstat`` of
    # a UNC path would already open a connection to that host.
    if hooks.validate_file_path(path) is None:
        raise _PathRefused(
            "path_refused", f"{path} is a protected or network location; it is not uploaded"
        )
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise _PathRefused("path_missing", f"no file at {path}") from None
    except (OSError, ValueError):
        raise _PathRefused("path_unreadable", f"{path} could not be read") from None
    if stat.S_ISLNK(info.st_mode):
        raise _PathRefused("path_symlink", f"{path} is a link; give the file itself")
    if stat.S_ISDIR(info.st_mode):
        raise _PathRefused("path_not_file", f"{path} is a folder; upload takes one file")
    if not stat.S_ISREG(info.st_mode):
        raise _PathRefused("path_not_file", f"{path} is not a regular file")
    if info.st_size > UPLOAD_MAX_BYTES:
        raise _PathRefused(
            "file_too_large", f"{path} is {info.st_size} bytes; the limit is {UPLOAD_MAX_BYTES}"
        )
    if not os.access(path, os.R_OK):
        raise _PathRefused("path_unreadable", f"{path} could not be read")
    return path


def _owner_only_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        # 0o700 is owner-only, the tightest mode a directory the owner reads can have.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(directory, 0o700)
    except OSError:  # pragma: no cover - Windows ACLs
        pass


def _prune(upload_dir: Path, now: float) -> None:
    """Drop staged copies past :data:`UPLOAD_KEEP_S`, then all but the newest few."""
    try:
        entries = sorted(
            (p for p in upload_dir.iterdir() if p.is_dir() and not p.is_symlink()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for index, entry in enumerate(entries):
        try:
            # shortcut: a form left unsubmitted past the 20th later upload loses its copy.
            if index >= UPLOAD_KEEP - 1 or now - entry.stat().st_mtime > UPLOAD_KEEP_S:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def stage_many(
    paths: list[Any], upload_dir: Path, *, now: float | None = None
) -> list[tuple[Path, int]]:
    """Check each of *paths* and copy them all into ONE fresh owner-only folder.

    Returns each copy's path (same file name, so the page sees it) and size, or
    raises :class:`_PathRefused` and leaves nothing staged. Every copy is
    ``hooks.safe_copy_file_nolink``'s: it reads through one descriptor that it
    judged, so the bytes copied are the ones the checks passed. Two files with one
    name are ``duplicate_name`` (the page would see the name twice); the files
    together are at most :data:`UPLOAD_MAX_BYTES`.
    """
    from kiro_crew import hooks

    checked = [_check_path(p) for p in paths]
    names = [Path(c).name or "upload" for c in checked]
    # Compared without case, so the page never sees two files under one name.
    if len({n.casefold() for n in names}) != len(names):
        raise _PathRefused("duplicate_name", "two of the files have the same name")
    _owner_only_dir(upload_dir)
    _prune(upload_dir, time.time() if now is None else now)
    folder = upload_dir / secrets.token_hex(8)
    _owner_only_dir(folder)
    out: list[tuple[Path, int]] = []
    total = 0
    try:
        for index, (path, name) in enumerate(zip(checked, names)):
            # Each copy gets a folder of its own, so two names the disk treats as one
            # (case, Unicode normalization, a trailing dot) never replace each other.
            own = folder / str(index)
            _owner_only_dir(own)
            copy = hooks.safe_copy_file_nolink(path, str(own), links_only=True)
            if copy is None:
                raise _PathRefused(
                    "path_refused",
                    f"{path} is a protected location, has another hard link or changed "
                    "while it was checked; it is not uploaded",
                )
            size = os.stat(copy).st_size
            total += size
            # shortcut: the helper copies the whole file, so one that grows past the cap
            # after the lstat above is refused only once copied; add a byte limit to the
            # helper if that disk use matters.
            if total > UPLOAD_MAX_BYTES:
                raise _PathRefused(
                    "file_too_large", f"the files come to more than {UPLOAD_MAX_BYTES} bytes"
                )
            target = own / name
            os.replace(copy, target)
            out.append((target, size))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return out


def _paths(args: dict[str, Any]) -> list[Any] | None:
    """``path``: one path, or a list of 1 to :data:`UPLOAD_MAX_FILES`; ``None`` otherwise.

    A list stays under ``path`` (never a key of its own) so the tool gate's path
    keystone, which reads every string under ``path`` at any depth, judges each file
    before the card, as it judges one.
    """
    path = args.get("path")
    if isinstance(path, list):
        return path if 1 <= len(path) <= UPLOAD_MAX_FILES else None
    return [path]


async def run(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """Set the file input *ref* to checked copies of the files at *path*."""
    ref = args.get("ref")
    if not _ops._is_ref(ref):
        return _ops.refusal("bad_ref", f"not a valid ref: {ref!r}; call snapshot first")
    if split_frame_ref(ref) is not None:
        return _ops.refusal("bad_ref", "upload into a cross-origin frame is not supported")
    paths = _paths(args)
    if paths is None:
        return _ops.refusal(
            "bad_path", f"`path` is one absolute path or a list of 1 to {UPLOAD_MAX_FILES}"
        )
    if ctx.upload_dir is None:
        return _ops.refusal("not_available", "upload is not available on this gateway")
    found = await _ops._page(ctx, "fileInput", ref)
    code = found.get("code") if isinstance(found, dict) else None
    if not isinstance(found, dict) or not found.get("ok"):
        if code == "not_file_input":
            return _ops.refusal("not_file_input", f"ref {ref} is not an <input type=file>")
        if code in ("disabled", "not_visible"):
            return _ops.refusal(code, f"the file input {ref} is {code.replace('_', ' ')}")
        return _ops.refusal("ref_stale", f"ref {ref} is not on the current page; re-snapshot")
    if len(paths) > 1 and not found.get("multiple"):
        return _ops.refusal("not_multiple", f"the file input {ref} takes one file")
    shown = [p if isinstance(p, str) else "" for p in paths]
    try:
        staged = await asyncio.to_thread(stage_many, paths, ctx.upload_dir)
    except _PathRefused as exc:
        for path in shown:
            _audit(path, None, exc.code)
        return _ops.refusal(exc.code, exc.error)
    folder = staged[0][0].parent.parent
    names = ", ".join(path.name for path, _size in staged)
    await _ops._step(ctx, f"Upload {names} to {ref}"[:120])
    try:
        result = await ctx.helper(
            "set_files",
            expression=f"{_ops.WALKER_SOURCE}.fileInput({json.dumps(ref)}, true)",
            files=[str(path) for path, _size in staged],
        )
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        _audit_all(shown, staged, "browser_error")
        raise
    if not isinstance(result, dict) or not result.get("ok"):
        shutil.rmtree(folder, ignore_errors=True)
        _audit_all(shown, staged, "ref_stale")
        return _ops.refusal("ref_stale", f"ref {ref} is not on the current page; re-snapshot")
    _audit_all(shown, staged, "ok")
    out: dict[str, Any] = {"ok": True, "ref": ref}
    if len(staged) == 1:
        out.update(file=staged[0][0].name, size=staged[0][1])
    else:
        out["files"] = [{"file": path.name, "size": size} for path, size in staged]
    return out


def _audit_all(shown: list[str], staged: list[tuple[Path, int]], outcome: str) -> None:
    for path, (_copy, size) in zip(shown, staged):
        _audit(path, size, outcome)
