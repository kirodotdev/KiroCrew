"""Gateway installer for Kiro Space marketplace apps (``kind = app``).

An app version is one zip of the app folder. Installing one is: resolve the
version, download the zip from the signed URL the listing API returns, verify
its SHA-256 BEFORE anything is unpacked, unpack it safely into a new empty
staging folder, show the person what they are about to install, and only after
an explicit yes hand that folder to Kiro Crew's own local-folder install,
:func:`kiro_crew.apps.manager.install_app`.

That call copies the tree and writes ``installed.json`` with ``enabled = False``.
It registers no agents, skills, MCP servers or crons and starts no backend, and
this module adds none of that: the dashboard's local-path install route, which
registers and starts an app right after a fresh install, is deliberately NOT on
this path. Running starts only at the existing enable step, which checks
execution trust. A marketplace app binds its trust grant to its listing
(``market:<ref>``, see :mod:`kiro_crew.apps.market_source`), so a grant recorded
for local-folder code never covers it.

Every limit and entry rule here mirrors the publish-side checks, so a zip the
server accepted unpacks and a hostile one is refused twice. The listing server
is reached through :class:`MarketClient`; this module ships no server client.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from kiro_crew.apps.manager import (
    AppResult,
    install_app,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.market_source import LISTING_REF_RE, market_source
from kiro_crew.config.loader import config_dir
from kiro_crew.owner_only_files import ensure_directory
from kiro_crew.sel import sel
from kiro_crew.zip_vet import ZipInventoryRejected, vet_zip_inventory

logger = logging.getLogger(__name__)

#: Shown next to every permission list. Nothing sandboxes commands, network or
#: files, so the confirm data must never read as a granted capability set.
PERMISSIONS_NOTICE = "declared by the publisher, not enforced"
#: The v1 trust tier of every marketplace listing.
TRUST_LABEL = "Community, not reviewed by Kiro Space"

MIB = 1024 * 1024
MAX_ZIP_BYTES = 25 * MIB
MAX_UNPACKED_BYTES = 100 * MIB
MAX_ENTRIES = 2000
MAX_ENTRY_BYTES = 25 * MIB
MAX_RATIO = 100
MAX_DEPTH = 16
MAX_PATH_BYTES = 240

_CHUNK = 64 * 1024
#: The zip format's entry-name separator (APPNOTE 4.4.17): a forward slash on
#: every OS, so entry names are split on it, never on ``os.sep``.
ZIP_SEP = "/"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class MarketInstallError(Exception):
    """A refusal, with a stable ``code`` callers can branch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedApp:
    """The pinned app version the listing API resolved (``ResolveListingInstall``)."""

    ref: str
    version: int
    content_sha256: str
    size: int
    published_by: str
    owner_group: str = ""
    app_version: str = ""
    permissions: dict[str, list[str]] = field(default_factory=dict)
    components: tuple[dict[str, Any], ...] = ()
    requires: tuple[dict[str, Any], ...] = ()
    min_kiro_crew_version: str = ""
    platform: tuple[str, ...] = ()


@dataclass(frozen=True)
class ListingDownload:
    """A signed URL for one pinned version (``GetListingDownload``)."""

    url: str
    sha256: str


class MarketClient(Protocol):
    """What the installer needs from the listing API and the signed URL."""

    def resolve_install(self, ref: str, version: int | None) -> ResolvedApp:
        """Resolve *ref* (optionally a pinned *version*) to one version."""

    def get_download(self, ref: str, version: int) -> ListingDownload:
        """Return the signed URL for exactly that version."""

    def open_download(self, url: str) -> Iterable[bytes]:
        """Stream the bytes behind a signed URL."""


def market_staging_root() -> Path:
    """Where downloads and unpacked trees live until ``install_app`` returns."""
    return config_dir() / "market-staging"


@dataclass
class PreparedInstall:
    """A verified, unpacked app waiting for the person's yes."""

    resolved: ResolvedApp
    staging_dir: Path
    app_root: Path
    confirm: dict[str, Any]

    @property
    def listing_name(self) -> str:
        return self.resolved.ref.partition(ZIP_SEP)[2]

    def discard(self) -> None:
        """Delete the staging folder (the zip and the unpacked tree)."""
        shutil.rmtree(self.staging_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Download and verify (before any unpack)
# ---------------------------------------------------------------------------


def _check_resolved(resolved: ResolvedApp, ref: str) -> None:
    if resolved.ref != ref or not LISTING_REF_RE.fullmatch(resolved.ref):
        raise MarketInstallError("market_resolve_invalid", "resolved listing ref is invalid")
    if not isinstance(resolved.version, int) or resolved.version < 1:
        raise MarketInstallError("market_resolve_invalid", "resolved version is invalid")
    if not _SHA256_RE.fullmatch(resolved.content_sha256 or ""):
        raise MarketInstallError("market_resolve_invalid", "resolved sha256 is invalid")
    if resolved.size > MAX_ZIP_BYTES:
        raise MarketInstallError("market_download_too_large", "app zip exceeds 25 MiB")
    if resolved.requires:
        # Dependencies are installed by their own kind's installer, which Kiro
        # Crew does not have yet. Refuse rather than install an app whose
        # declared dependencies are missing.
        raise MarketInstallError(
            "market_dependencies_unsupported",
            "this app requires other listings; dependency install is not available yet",
        )


def _download_verified(client: MarketClient, resolved: ResolvedApp, staging: Path) -> Path:
    download = client.get_download(resolved.ref, resolved.version)
    if not isinstance(download.url, str) or not download.url.lower().startswith("https://"):
        raise MarketInstallError("market_download_refused", "signed URL must use https")
    if download.sha256 != resolved.content_sha256:
        raise MarketInstallError(
            "market_sha256_mismatch", "download sha256 differs from the resolved version"
        )
    zip_path = staging / "app.zip"
    digest = hashlib.sha256()
    written = 0
    fd = os.open(
        zip_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(fd, "wb") as out:
        for chunk in client.open_download(download.url):
            written += len(chunk)
            if written > MAX_ZIP_BYTES:
                raise MarketInstallError("market_download_too_large", "app zip exceeds 25 MiB")
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != resolved.content_sha256:
        zip_path.unlink(missing_ok=True)
        raise MarketInstallError(
            "market_sha256_mismatch", "downloaded zip does not match the resolved sha256"
        )
    return zip_path


# ---------------------------------------------------------------------------
# Safe unpack
# ---------------------------------------------------------------------------


def _refuse(message: str) -> MarketInstallError:
    return MarketInstallError("market_archive_refused", message)


def _entry_segments(name: str) -> list[str]:
    """Validate one entry name and return its path segments."""
    if not name or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        raise _refuse("entry name has a control character")
    if "\\" in name:
        raise _refuse("entry name contains a backslash")
    if name.startswith(ZIP_SEP) or _DRIVE_RE.match(name):
        raise _refuse("entry name is absolute")
    if ":" in name:
        # On Windows a ``D:x`` segment anywhere re-roots ``joinpath`` onto
        # another drive, and ``x:stream`` names an alternate data stream.
        raise _refuse("entry name contains a colon")
    if len(name.encode("utf-8")) > MAX_PATH_BYTES:
        raise _refuse("entry path is too long")
    segments = name.rstrip(ZIP_SEP).split(ZIP_SEP)
    if any(s in ("", ".", "..") for s in segments):
        raise _refuse("entry name has an empty, '.' or '..' segment")
    if len(segments) > MAX_DEPTH:
        raise _refuse("entry path is too deep")
    return segments


def _entry_kind(info: zipfile.ZipInfo) -> str:
    """``"dir"`` or ``"file"``; anything else (link, device, FIFO, socket) refuses."""
    mode = info.external_attr >> 16
    if info.flag_bits & 0x1:
        raise _refuse("encrypted entries are not allowed")
    if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise _refuse("setuid, setgid and sticky bits are not allowed")
    fmt = stat.S_IFMT(mode)
    if fmt == 0:
        # No Unix mode recorded: the name alone says file or directory.
        return "dir" if info.is_dir() else "file"
    if fmt == stat.S_IFDIR:
        return "dir"
    if fmt == stat.S_IFREG and not info.is_dir():
        return "file"
    raise _refuse("only regular files and directories are allowed")


def _plan_entries(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, list[str], str]]:
    """Check every entry before a single byte is written."""
    infos = zf.infolist()
    seen: set[str] = set()
    declared_total = 0
    plan = []
    for info in infos:
        if info.volume:
            raise _refuse("spanned archives are not allowed")
        segments = _entry_segments(info.filename)
        kind = _entry_kind(info)
        key = unicodedata.normalize("NFC", ZIP_SEP.join(segments)).casefold()
        if key in seen:
            raise _refuse("two entries name the same path")
        seen.add(key)
        if kind == "file":
            if info.file_size > MAX_ENTRY_BYTES:
                raise _refuse("an entry exceeds 25 MiB unpacked")
            if info.file_size > MAX_RATIO * max(info.compress_size, 1):
                raise _refuse("an entry's compression ratio exceeds 100:1")
            declared_total += info.file_size
            if declared_total > MAX_UNPACKED_BYTES:
                raise _refuse("archive exceeds 100 MiB unpacked")
        plan.append((info, segments, kind))
    return plan


def _write_entry(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path, budget: list[int]
) -> None:
    mode = 0o755 if (info.external_attr >> 16) & 0o111 else 0o644
    fd = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    written = 0
    limit = min(MAX_ENTRY_BYTES, MAX_RATIO * max(info.compress_size, 1))
    with os.fdopen(fd, "wb") as out, zf.open(info) as src:
        while chunk := src.read(_CHUNK):
            written += len(chunk)
            budget[0] -= len(chunk)
            # Actual bytes, not the declared sizes: the header can lie.
            if written > limit:
                raise _refuse("an entry exceeds its size or ratio cap while unpacking")
            if budget[0] < 0:
                raise _refuse("archive exceeds 100 MiB unpacked")
            out.write(chunk)
    os.chmod(target, mode)


def unpack_app_zip(zip_path: Path, staging: Path) -> Path:
    """Unpack *zip_path* into a NEW empty folder inside *staging*.

    Every entry is checked against the archive rules before anything is
    written; the first bad entry aborts and deletes the folder. Files are
    created exclusively and never through a link.
    """
    dest = Path(tempfile.mkdtemp(prefix="app-", dir=staging))
    try:
        try:
            # The entry cap is enforced on the end-of-central-directory record,
            # BEFORE ZipFile reads the whole central directory into memory.
            vet_zip_inventory(zip_path, max_members=MAX_ENTRIES)
        except ZipInventoryRejected as exc:
            raise _refuse(f"archive inventory refused: {exc}") from exc
        try:
            zf = zipfile.ZipFile(zip_path)
        except (zipfile.BadZipFile, OSError, NotImplementedError) as exc:
            raise _refuse(f"not a readable zip: {exc}") from exc
        with zf:
            plan = _plan_entries(zf)
            budget = [MAX_UNPACKED_BYTES]
            for info, segments, kind in plan:
                target = dest.joinpath(*segments)
                try:
                    if kind == "dir":
                        target.mkdir(mode=0o755, parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    _write_entry(zf, info, target, budget)
                except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
                    raise _refuse(f"entry could not be read: {exc}") from exc
                except OSError as exc:
                    raise _refuse(f"entry could not be written: {exc.strerror}") from exc
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def _tree_hash(path: Path) -> str:
    """The component hash: one file's SHA-256, or the tree hash of a folder."""

    def _file_sha(p: Path) -> str:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()

    if path.is_file():
        return _file_sha(path)
    lines = []
    files = sorted(
        (p for p in path.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(path).as_posix().encode("utf-8"),
    )
    for p in files:
        lines.append(f"{_file_sha(p)} {p.relative_to(path).as_posix()}\n")
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def _verify_components(app_root: Path, resolved: ResolvedApp) -> None:
    for component in resolved.components:
        rel = component.get("path")
        expected = component.get("sha256")
        if not isinstance(rel, str) or not isinstance(expected, str):
            raise _refuse("resolved component is malformed")
        target = app_root.joinpath(*_entry_segments(rel))
        if not target.exists() or _tree_hash(target) != expected:
            raise _refuse(f"component {component.get('name', rel)!r} does not match its hash")


# ---------------------------------------------------------------------------
# Confirm data
# ---------------------------------------------------------------------------


def _permission_list(raw: Any) -> list[str]:
    return [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []


def _cron_declared(name: str, background: list[str]) -> bool:
    entry = f"cron: {name}"
    return any(b == entry or b.startswith(entry + " ") for b in background)


def folder_capabilities(app_root: Path, resolved: ResolvedApp) -> dict[str, list[str]]:
    """What the unpacked ``app.json`` itself would run, checked against the server.

    The confirm screen must show the folder's own crons, MCP servers and
    agents, not only the permissions the server returned. Any one the resolved
    permissions do not name refuses the install, so the person never approves
    a list that is shorter than what the folder holds.
    """
    try:
        manifest = AppManifest.from_json_file(app_root / "app.json")
    except Exception as exc:  # noqa: BLE001 - any parse failure refuses
        raise MarketInstallError("market_manifest_invalid", f"app.json is invalid: {exc}") from exc
    found = {
        "crons": [c.name for c in manifest.crons],
        "mcpServers": sorted(manifest.mcpServers),
        "agents": list(manifest.agents),
    }
    perms = resolved.permissions or {}
    background = _permission_list(perms.get("background"))
    missing = [f"cron {n!r}" for n in found["crons"] if not _cron_declared(n, background)]
    declared_mcp = set(_permission_list(perms.get("mcpServers")))
    missing += [f"MCP server {n!r}" for n in found["mcpServers"] if n not in declared_mcp]
    declared_agents = set(_permission_list(perms.get("agents")))
    missing += [f"agent {n!r}" for n in found["agents"] if n not in declared_agents]
    if missing:
        raise MarketInstallError(
            "market_undeclared_capability",
            "the app declares what its listing does not: " + ", ".join(missing),
        )
    return found


def build_confirm(resolved: ResolvedApp, in_folder: dict[str, list[str]]) -> dict[str, Any]:
    """What the person sees before saying yes (marketplace-apps spec 5.3)."""
    perms = resolved.permissions or {}
    return {
        "inFolder": in_folder,
        "app": {
            "ref": resolved.ref,
            "version": resolved.version,
            "appVersion": resolved.app_version,
            "publisher": resolved.published_by,
            "ownerGroup": resolved.owner_group,
            "sha256Prefix": resolved.content_sha256[:12],
        },
        "components": [
            {
                "kind": c.get("kind", ""),
                "name": c.get("name", ""),
                "sha256Prefix": str(c.get("sha256", ""))[:12],
            }
            for c in resolved.components
        ],
        "dependencies": [],
        "permissions": {
            "notice": PERMISSIONS_NOTICE,
            "commands": _permission_list(perms.get("commands")),
            "network": _permission_list(perms.get("network")),
            "files": _permission_list(perms.get("files")),
            "background": _permission_list(perms.get("background")),
            "mcpServers": _permission_list(perms.get("mcpServers")),
            "agents": _permission_list(perms.get("agents")),
        },
        "size": resolved.size,
        "compatibility": {
            "minKiroCrewVersion": resolved.min_kiro_crew_version,
            "platform": list(resolved.platform),
        },
        "trustLabel": TRUST_LABEL,
    }


# ---------------------------------------------------------------------------
# Public flow
# ---------------------------------------------------------------------------


def prepare_market_install(
    client: MarketClient, ref: str, version: int | None = None
) -> PreparedInstall:
    """Resolve, download, verify and unpack; install nothing.

    Raises :class:`MarketInstallError` on any refusal, after deleting
    everything it wrote. The caller shows ``confirm`` and then calls
    :func:`complete_market_install` (or :meth:`PreparedInstall.discard`).
    """
    if not LISTING_REF_RE.fullmatch(ref or ""):
        raise MarketInstallError("market_resolve_invalid", "listing ref is invalid")
    resolved = client.resolve_install(ref, version)
    _check_resolved(resolved, ref)
    root = market_staging_root()
    ensure_directory(root)
    staging = Path(tempfile.mkdtemp(prefix="install-", dir=root))
    try:
        zip_path = _download_verified(client, resolved, staging)
        app_root = unpack_app_zip(zip_path, staging)
        zip_path.unlink(missing_ok=True)
        _verify_components(app_root, resolved)
        in_folder = folder_capabilities(app_root, resolved)
    except BaseException as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, MarketInstallError):
            sel().log_api_access(
                caller="market_install",
                operation="prepare",
                outcome="rejected",
                resources=f"ref={ref!r}",
                error=exc.code,
            )
        raise
    return PreparedInstall(
        resolved=resolved,
        staging_dir=staging,
        app_root=app_root,
        confirm=build_confirm(resolved, in_folder),
    )


def complete_market_install(prepared: PreparedInstall, *, confirmed: bool) -> AppResult:
    """After an explicit yes, install the unpacked folder, disabled.

    Calls :func:`install_app` directly and never the dashboard local-path
    route, so nothing is registered or started. The marketplace source
    (``market:<ref>@<n>``, ``origin = "local"``, ``sourceCommit`` = the zip
    SHA-256) goes into ``install_app``'s first record write, so no record ever
    names the staging folder as the source. The staging folder is deleted
    whatever happened.
    """
    resolved = prepared.resolved
    try:
        if confirmed is not True:
            return AppResult(
                ok=False,
                error="marketplace install needs an explicit yes",
                error_code="market_install_not_confirmed",
            )
        result = install_app(
            prepared.app_root,
            expected_name=prepared.listing_name,
            market_source=market_source(resolved.ref, resolved.version),
            source_commit=resolved.content_sha256,
        )
        if result.ok:
            sel().log_api_access(
                caller="market_install",
                operation="install",
                outcome="success",
                resources=f"ref={resolved.ref!r} version={resolved.version}",
            )
        return result
    finally:
        prepared.discard()
