"""STAGE for built-in adoption: fetch a pinned tree, and verify it without trusting it.

Two fetch paths, one verification:

- **git** through the catalog's own credential-free, ``strict``-sandbox clone
  (``registry_pipeline.checkout._git_clone_or_pull`` with ``index_originated=True``),
  into ``app-sources/<name>/``;
- **archive**: the codeload tarball of the pinned commit, with redirects refused,
  a read cap and the wheel-pinned ``archiveSha256``.

Either artifact is only ever used through :func:`materialize_verified_tree`, which
copies it into a caller-owned directory and computes :func:`adoption_tree_digest`
ON THAT COPY. A staged tree that changed after it was fetched therefore fails
``tree_digest_mismatch`` and is never executed.

Nothing here names an app. Owning spec: app-kit-platform.md § Former built-in
adoption.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
import socket
import stat
import tarfile
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from kiro_crew.apps.builtin_adoption_pins import AdoptionPin

logger = logging.getLogger(__name__)

#: Download deadline for one archive GET, seconds.
ARCHIVE_TIMEOUT = 60
#: Read cap on the archive body and on a tree's total file bytes.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_TREE_BYTES = 64 * 1024 * 1024
#: Upper bound on the regular files one pinned tree may hold.
MAX_TREE_FILES = 5000

_CHUNK = 1024 * 1024
#: Separator of a tree-relative path: tar member names and the tree digest use "/"
#: on every OS, and ``Path.joinpath`` rebuilds a native path from its parts.
_REL_SEP = "/"

StagedKind = Literal["git", "archive"]


class AdoptionRefused(Exception):
    """A deterministic or transient refusal, carrying its ``AdoptionReasonCode``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class StagedArtifact:
    """Where STAGE left a fetched pin: a git checkout or a verified archive file."""

    kind: StagedKind
    path: Path


@dataclass(frozen=True)
class StageResult:
    """The outcome of :func:`stage_pin`. ``code`` is empty on success."""

    staged: StagedArtifact | None
    code: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.staged is not None and not self.code


# --------------------------------------------------------------------------- #
# The tree digest (shared by STAGE, APPLY and scripts/adoption_pin.py)
# --------------------------------------------------------------------------- #


def _reserved_component(part: str) -> bool:
    from kiro_crew.constants import WINDOWS_DEVICE_STEMS

    stem = part.split(".", 1)[0].rstrip(" ").casefold()
    return part[-1] in ". " or stem in WINDOWS_DEVICE_STEMS


def _check_portable(rel: str, seen_folded: dict[str, str]) -> None:
    """Refuse a relative path that does not survive every supported filesystem."""
    if "\\" in rel or ":" in rel:
        raise AdoptionRefused("tree_unportable_name", rel)
    for part in rel.split(_REL_SEP):
        if not part or part in (".", "..") or _reserved_component(part):
            raise AdoptionRefused("tree_unportable_name", rel)
    folded = rel.casefold()
    other = seen_folded.get(folded)
    if other is not None and other != rel:
        raise AdoptionRefused("tree_unportable_name", f"{rel} collides with {other}")
    seen_folded[folded] = rel


def _walk_tree(root: Path) -> list[tuple[str, Path]]:
    """``(relative posix path, absolute path)`` for every regular file under *root*.

    Never follows a link: a symlink, a junction or any non-regular entry refuses
    the whole tree. Only the top-level ``.git`` directory is skipped. Paths are
    checked for portability and the tree for its size bounds.
    """
    from kiro_crew.platform_compat import is_link_or_junction

    files: list[tuple[str, Path]] = []
    seen_folded: dict[str, str] = {}
    total = 0
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise AdoptionRefused("tree_has_link", f"{prefix or '.'}: {exc}") from exc
        for entry in entries:
            if not prefix and entry.name == ".git":
                continue
            rel = f"{prefix}{entry.name}"
            _check_portable(rel, seen_folded)
            if entry.is_symlink() or is_link_or_junction(entry.path):
                raise AdoptionRefused("tree_has_link", rel)
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                stack.append((Path(entry.path), f"{rel}/"))
            elif stat.S_ISREG(mode):
                files.append((rel, Path(entry.path)))
                total += entry.stat(follow_symlinks=False).st_size
                if len(files) > MAX_TREE_FILES or total > MAX_TREE_BYTES:
                    raise AdoptionRefused("tree_too_large", rel)
            else:
                raise AdoptionRefused("tree_has_link", rel)
    return files


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_entries(entries: list[tuple[str, str]]) -> str:
    """The tree digest of ``(relative posix path, file sha256)`` pairs."""
    encoded = sorted((rel.encode("utf-8"), file_hash) for rel, file_hash in entries)
    return hashlib.sha256(
        b"".join(rel + b"\0" + file_hash.encode("ascii") + b"\n" for rel, file_hash in encoded)
    ).hexdigest()


def adoption_tree_digest(root: Path) -> str:
    """The path-independent digest of the app tree at *root*. Blocking.

    File modes are not part of it (Windows has none). Raises :class:`AdoptionRefused`
    for a link, an unportable name or an oversized tree.
    """
    return digest_entries([(rel, _file_sha256(path)) for rel, path in _walk_tree(root)])


# --------------------------------------------------------------------------- #
# Materializing a staged artifact into a caller-owned directory
# --------------------------------------------------------------------------- #


def _copy_checkout(source: Path, dest: Path) -> None:
    for rel, path in _walk_tree(source):
        target = dest.joinpath(*rel.split(_REL_SEP))
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "rb") as src, open(target, "xb") as out:
            shutil.copyfileobj(src, out, _CHUNK)


def _member_relpath(member: tarfile.TarInfo, root: str) -> str | None:
    """*member*'s path below the archive's single top-level directory, or None for it."""
    name = member.name
    if name.startswith("/") or "\\" in name or ":" in name:
        raise AdoptionRefused("tree_unportable_name", name)
    parts = PurePosixPath(name).parts
    if not parts or parts[0] != root or any(p in ("..", "") for p in parts):
        raise AdoptionRefused("tree_unportable_name", name)
    rest = "/".join(parts[1:])
    return rest or None


def _extract_archive(archive: Path, dest: Path, *, root: str) -> None:
    """Write every regular member of *archive* below *dest*, stripping *root*/.

    Members are validated and written one by one rather than through
    ``extractall``: a link, a device, an absolute or ``..`` path, or a member
    outside the single top-level directory refuses the whole archive, and the
    write never follows anything on disk (``open(..., "xb")`` in a fresh dir).
    """
    seen_folded: dict[str, str] = {}
    count = 0
    total = 0
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            for member in tar:
                if member.type in (tarfile.XHDTYPE, tarfile.XGLTYPE):
                    continue
                rel = _member_relpath(member, root)
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise AdoptionRefused("tree_has_link", member.name)
                if rel is None:
                    if not member.isdir():
                        raise AdoptionRefused("tree_unportable_name", member.name)
                    continue
                _check_portable(rel, seen_folded)
                target = dest.joinpath(*rel.split(_REL_SEP))
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isreg():
                    raise AdoptionRefused("tree_has_link", member.name)
                count += 1
                total += member.size
                if count > MAX_TREE_FILES or total > MAX_TREE_BYTES:
                    raise AdoptionRefused("tree_too_large", member.name)
                stream = tar.extractfile(member)
                if stream is None:
                    raise AdoptionRefused("tree_has_link", member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream, open(target, "xb") as out:
                    shutil.copyfileobj(stream, out, _CHUNK)
    except (tarfile.TarError, EOFError, OSError) as exc:
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise AdoptionRefused("disk_full", str(exc)) from exc
        if isinstance(exc, FileExistsError):
            raise AdoptionRefused("tree_unportable_name", str(exc)) from exc
        raise AdoptionRefused("archive_digest_mismatch", f"unreadable archive: {exc}") from exc


def materialize_verified_tree(pin: AdoptionPin, staged: StagedArtifact, dest: Path) -> None:
    """Copy *staged* into the empty directory *dest* and verify ``treeSha256`` there.

    Blocking. Raises :class:`AdoptionRefused` (``tree_digest_mismatch`` when the copy
    is not the pinned tree). The digest is computed on *dest*, the copy the caller
    owns, so nothing between STAGE and this call can change what gets swapped in.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise ValueError(f"materialize destination {dest} is not empty")
    if staged.kind == "git":
        _copy_checkout(staged.path, dest)
    else:
        if _file_sha256(staged.path) != pin.archive_sha256:
            raise AdoptionRefused("archive_digest_mismatch", staged.path.name)
        _extract_archive(staged.path, dest, root=pin.archive_root)
    digest = adoption_tree_digest(dest)
    if digest != pin.tree_sha256:
        raise AdoptionRefused("tree_digest_mismatch", digest[:12])


# --------------------------------------------------------------------------- #
# STAGE: the network part
# --------------------------------------------------------------------------- #


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect: codeload answers 200 for a commit, and a 3xx changes origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(
            req.full_url, code, f"refusing to follow a redirect to {newurl!r}", headers, fp
        )


def _open_archive(req: urllib.request.Request) -> Any:
    """Open *req* with redirects refused. THE seam the archive download goes through."""
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    return urllib.request.build_opener(_NoRedirects).open(req, timeout=ARCHIVE_TIMEOUT)


def adoption_dir() -> Path:
    """``<home>/app-adoption/``: the ledger, staged archives and APPLY temp trees."""
    from kiro_crew.config.loader import config_dir

    return config_dir() / "app-adoption"


def _download_archive_sync(pin: AdoptionPin, dest: Path) -> None:
    url = pin.archive_url
    if not url.startswith("https://"):
        raise AdoptionRefused("http_status", "archive URL is not https")
    request = urllib.request.Request(url, headers={"User-Agent": "kiro-crew-adoption"})
    try:
        response = _open_archive(request)
    except urllib.error.HTTPError as exc:
        code = "commit_not_found" if exc.code in (404, 410) else "http_status"
        raise AdoptionRefused(code, f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        timed_out = isinstance(exc.reason, (socket.timeout, TimeoutError))
        raise AdoptionRefused(
            "network_timeout" if timed_out else "network_unreachable", type(exc.reason).__name__
        ) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise AdoptionRefused("network_timeout", "") from exc
    except OSError as exc:
        raise AdoptionRefused("network_unreachable", type(exc).__name__) from exc
    digest = hashlib.sha256()
    tmp = dest.with_name(f"{dest.name}.part-{os.getpid()}")
    received = 0
    try:
        with response, open(tmp, "wb") as out:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_ARCHIVE_BYTES:
                    raise AdoptionRefused("tree_too_large", "archive exceeds the read cap")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != pin.archive_sha256:
            raise AdoptionRefused("archive_digest_mismatch", "")
        os.replace(tmp, dest)
    except (socket.timeout, TimeoutError) as exc:
        raise AdoptionRefused("network_timeout", "") from exc
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise AdoptionRefused("disk_full", "") from exc
        raise AdoptionRefused("network_unreachable", type(exc).__name__) from exc
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("could not remove a partial archive", exc_info=True)


async def _git_fetch(pin: AdoptionPin, dest: Path) -> str:
    """Fetch the pinned commit into *dest* with the catalog's clone posture.

    Returns ``""`` on success, ``"commit_not_found"`` for a missing ref, or
    ``"network"`` for any other failure (the caller falls back to the archive).
    The one seam tests replace with a local bare-repository fetch.
    """
    from kiro_crew.apps.registry_pipeline.checkout import (
        _clone_origin_matches,
        _git_clone_or_pull,
        _names_a_missing_ref,
        _resolved_clone_commit,
    )

    log: list[str] = []
    pending: list[Path] = []
    try:
        error = await _git_clone_or_pull(
            pin.repository,
            "main",
            dest,
            log,
            index_originated=True,
            commit=pin.commit,
            pending_cleanup=pending,
        )
    except (OSError, ValueError) as exc:
        logger.info("adoption git fetch failed: %s", type(exc).__name__)
        return "network"
    for aside in pending:
        shutil.rmtree(aside, ignore_errors=True)
    if error is not None:
        text = " ".join([str(error.get("error", "")), *log])
        return "commit_not_found" if _names_a_missing_ref(text) else "network"
    import asyncio

    if await asyncio.to_thread(_resolved_clone_commit, dest) != pin.commit:
        return "network"
    if not await _clone_origin_matches(dest, pin.repository):
        return "network"
    return ""


def _sources_dir(name: str) -> Path:
    from kiro_crew.apps.registry_pipeline.recovery import app_source_dir

    return app_source_dir(name)


async def stage_pin(
    pin: AdoptionPin,
    *,
    git_available: bool,
    verify: Callable[[Path], str | None] | None = None,
    git_fetch: Callable[[AdoptionPin, Path], Awaitable[str]] | None = None,
) -> StageResult:
    """Fetch *pin* (git first when available, then the archive) and verify it.

    *verify* runs on a throwaway verified copy and returns a reason code to refuse
    with (the deterministic manifest checks), or None. Never mutates an app record.
    """
    import asyncio

    fetch = git_fetch or _git_fetch
    staged: StagedArtifact | None = None
    if git_available:
        checkout = _sources_dir(pin.name)
        outcome = await fetch(pin, checkout)
        if outcome == "commit_not_found":
            return StageResult(None, "commit_not_found", "git")
        if not outcome:
            staged = StagedArtifact("git", checkout)
    if staged is None:
        archive = adoption_dir() / f"{pin.name}-{pin.commit}.tar.gz"
        try:
            await asyncio.to_thread(_prepare_dir, archive.parent)
            await asyncio.to_thread(_download_archive_sync, pin, archive)
        except AdoptionRefused as refused:
            return StageResult(None, refused.code, "archive")
        staged = StagedArtifact("archive", archive)
    try:
        refusal = await asyncio.to_thread(_verify_staged, pin, staged, verify)
    except AdoptionRefused as refused:
        return StageResult(None, refused.code, staged.kind)
    if refusal:
        return StageResult(None, refusal, staged.kind)
    return StageResult(staged)


def _prepare_dir(path: Path) -> None:
    from kiro_crew.owner_only_files import ensure_directory

    ensure_directory(path)


def private_temp_dir(prefix: str) -> Path:
    """A fresh owner-only directory under :func:`adoption_dir`."""
    root = adoption_dir()
    _prepare_dir(root)
    path = root / f".{prefix}-{os.getpid()}-{os.urandom(4).hex()}"
    path.mkdir(mode=0o700)
    return path


def _verify_staged(
    pin: AdoptionPin, staged: StagedArtifact, verify: Callable[[Path], str | None] | None
) -> str | None:
    tmp = private_temp_dir("verify")
    try:
        tree = tmp / "tree"
        materialize_verified_tree(pin, staged, tree)
        return verify(tree) if verify is not None else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


__all__ = [
    "ARCHIVE_TIMEOUT",
    "MAX_ARCHIVE_BYTES",
    "AdoptionRefused",
    "StageResult",
    "StagedArtifact",
    "adoption_dir",
    "adoption_tree_digest",
    "digest_entries",
    "materialize_verified_tree",
    "private_temp_dir",
    "stage_pin",
]
