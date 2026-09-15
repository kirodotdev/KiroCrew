"""The local clip cache: what has been downloaded, what serves it, what evicts it.

Feature-video media is hosted, not bundled (see
:mod:`kiro_crew.feature_videos_manifest`), so between "a clip exists" and "a clip
can play" sits a transfer. This module owns that middle: a background task that
walks the verified manifest one entry at a time, the read-only route that serves
what landed, and the eviction that keeps the cache inside its budget.

Four decisions worth stating, because each is the answer to a way this could go
wrong:

**One clip at a time, paced.** The task runs at gateway boot, when the user is
doing something else. A parallel fetch of a dozen clips would take the link they
are working over for a feature nobody asked for yet, so the transfer is
serialized and rate-limited (:data:`DEFAULT_RATE_LIMIT_BYTES_PER_S`). The limit
is lifted for ``POST /api/feature-videos/fetch-all``, where the user asked and is
waiting.

**Resumable across restarts, and idempotent.** A partial transfer stays in a
``.part`` staging file under a stable name, so the next boot continues it instead
of starting over — and a clip already on disk is skipped without a request.
Nothing here is a one-shot: running it twice is the normal case.

**"On disk" means "installed under the CURRENT pin."** Each verified install
leaves a receipt beside the file (:func:`record_verified`) naming the sha256 it
was checked against, and :func:`is_cached` compares that receipt to the manifest
in force. A re-published manifest that keeps a basename but changes its bytes —
same clip size, new content; or any re-encoded poster, which has no size to
compare — therefore reads as not cached and is fetched again. Without the
receipt the size check alone would keep serving the old file for the life of the
release, and nothing downstream re-hashes.

**The running release is never evicted.** Eviction frees space by removing whole
release folders, oldest first, and refuses to remove the one this build plays
from. Freeing the release currently in use would delete a clip the dashboard is
about to fetch, and the next boot would download it again — a cache that evicts
its own working set is worse than a full one.

**The serving route derives every path component itself, and reads from the
descriptor it checked.** It takes a release and a basename out of the URL,
validates both against the same rules the manifest applies, re-checks that the
resolved file is inside the release folder, then opens it ONCE with no-follow
semantics and streams from that descriptor (:func:`_open_served`). Not a
directory index, not ``add_static``, never a path that came from the request
unchecked, and never a second open by path: this route reads from the user's
data home, where a traversal would be a file-disclosure bug rather than a 404,
and where a file swapped for a link between a check and a reopen would be
followed by the reopen.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import stat
import threading
from pathlib import Path

from aiohttp import web

import kiro_crew
from kiro_crew import asset_downloader
from kiro_crew import feature_videos_manifest as manifest_mod
from kiro_crew import pinned_fs, platform_compat
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.feature_videos_manifest import ManifestEntry, VideoManifest

logger = logging.getLogger(__name__)

#: Background pacing for the boot-time transfer, in bytes per second. 512 KiB/s
#: fills a 20-second clip in a few seconds while leaving a slow link usable —
#: this runs unasked, so it yields to whatever the user is doing.
DEFAULT_RATE_LIMIT_BYTES_PER_S = 512 * 1024

#: URL prefix the cache is served under. A sibling of ``/app-assets`` rather than
#: a child: those are files inside the wheel, these are files in the user's data
#: home, and one route must never be able to reach the other's tree.
SERVE_PREFIX = "/feature-videos/"

#: Ceiling for a poster transfer. The manifest declares an exact ``bytes`` for the
#: clip but not for the poster, and bytes are written to the staging file as they
#: arrive — so without a bound an endless poster body fills the disk before the
#: end-of-stream digest can reject it, which would break this module's own claim
#: that a tampered object can only fail verification. 8 MiB is generous for a
#: single still frame and small enough that reaching it is a fault, not a big poster.
MAX_POSTER_BYTES = 8 * 1024 * 1024

#: Escape hatch for tests and CI, mirroring ``KIROCREW_SKIP_MODEL_DOWNLOAD``: a
#: test run must never pull media over the network.
SKIP_DOWNLOAD_ENV = "KIROCREW_SKIP_FEATURE_VIDEO_DOWNLOAD"

#: Download-state values reported by ``/api/feature-videos/status`` as
#: ``download_state``. ``denied`` is distinct from ``failed`` on purpose: an
#: operator reading a fleet needs to see "the ceiling forbids this" rather than a
#: transfer error that will never resolve.
STATE_IDLE = "idle"
STATE_FETCHING_MANIFEST = "fetching_manifest"
STATE_DOWNLOADING = "downloading"
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_DENIED = "denied"
STATE_DISABLED = "disabled"


def _dashboard_config() -> object:
    """The loaded dashboard config section. Blocking."""
    return KiroCrewConfig.load().dashboard


#: Receipt filename for an installed media file: ``.<basename>.sha256``. Hidden,
#: and never servable — the serving route admits only clip and poster suffixes —
#: so the receipt cannot itself be fetched, and it does not collide with any name
#: a manifest can carry (those never start with a dot).
_RECEIPT_PREFIX = "."
_RECEIPT_SUFFIX = ".sha256"

#: A receipt is one hex digest; anything longer is not one we wrote.
_RECEIPT_MAX_BYTES = 128


def _receipt_name(name: str) -> str:
    return f"{_RECEIPT_PREFIX}{name}{_RECEIPT_SUFFIX}"


def _receipt_path(folder: Path, name: str) -> Path:
    return folder / _receipt_name(name)


def record_verified(
    folder: "Path | asset_downloader.PinnedTargetDir", name: str, sha256: str
) -> bool:
    """Write the receipt saying *name* in *folder* was installed under *sha256*.

    Written AFTER the verified install, never before: the receipt certifies bytes
    that are on disk. A crash between the two leaves a file with no receipt, which
    :func:`is_cached` reads as "not cached" and the next pass re-fetches — the
    recoverable outcome. Atomic and owner-only like the media itself; the rename
    replaces a planted link at the receipt's name rather than following it.

    *folder* is the release folder: either the pinned handle the download pass
    already holds (:func:`~kiro_crew.feature_videos_manifest.pinned_release_dir`),
    or its path, in which case the folder is pinned and proven here for the one
    write. Either way the receipt is written relative to the held descriptor,
    never to a path that could have been re-pointed since it was checked.

    Returns False, with the reason logged, when the receipt cannot be written.
    Never raises: the caller is the background pass, and an exception here would
    end the whole pass with the status stuck on ``downloading``, when the truthful
    outcome is "this entry did not land" and the next entry still gets its turn.
    """
    receipt = _receipt_name(name)
    try:
        if isinstance(folder, asset_downloader.PinnedTargetDir):
            folder.write_text(receipt, sha256.lower())
        else:
            with manifest_mod.pinned_release_dir(folder.name) as pinned:
                pinned.write_text(receipt, sha256.lower())
    except (OSError, ValueError) as exc:
        logger.warning("feature-video receipt for %s could not be written: %s", name, exc)
        return False
    return True


def recorded_sha256(folder: Path, name: str) -> str:
    """The sha256 *name* in *folder* was installed under, or ``""`` if no receipt.

    A receipt that is not a regular file (a link) or does not hold one hex digest
    reads as absent — the safe direction is "fetch it again".
    """
    path = _receipt_path(folder, name)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return ""
        with open(path, "rb") as fh:
            raw = fh.read(_RECEIPT_MAX_BYTES + 1)
    except OSError:
        return ""
    text = raw.decode("ascii", "replace").strip().lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        return ""
    return text


def is_cached(entry: ManifestEntry, release: str) -> bool:
    """Whether both media files for *entry* are installed under the CURRENT pins.

    Three checks per file, none of them a re-hash: the file is present, its
    install receipt (:func:`recorded_sha256`) names the sha256 the manifest in
    force pins for it, and — for the clip, whose size the manifest declares — the
    size matches. Re-hashing every clip on every ``/next`` request would put a disk
    read proportional to the library on a polled route; the receipt answers the
    question the hash would (was THIS content verified?) at the cost of one tiny
    read, and the size check catches a truncated file a later disk problem
    produced.

    The receipt is what makes a re-publish take effect: a manifest that keeps a
    basename and changes its bytes changes the pin, the receipt then disagrees
    with it, and the entry is fetched again. Files another process wrote into the
    folder carry no receipt and are never counted as cached.
    """
    try:
        folder = manifest_mod.release_dir(release)
    except ValueError:
        return False
    clip = folder / entry.file
    poster = folder / entry.poster
    try:
        if not (clip.is_file() and clip.stat().st_size == entry.bytes and poster.is_file()):
            return False
    except OSError:
        return False
    return (
        recorded_sha256(folder, entry.file) == entry.sha256.lower()
        and recorded_sha256(folder, entry.poster) == entry.poster_sha256.lower()
    )


def poster_url_path(entry: ManifestEntry, release: str) -> str:
    """Served path for *entry*'s poster in *release*."""
    return f"{SERVE_PREFIX}{release}/{entry.poster}"


# ── Eviction ──


def _dir_size(path: Path) -> int:
    """Total bytes under *path*, ignoring anything unreadable."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def release_folders() -> list[tuple[str, Path, float, int]]:
    """Every release folder in the cache as ``(release, path, mtime, bytes)``.

    Reads through :func:`~kiro_crew.feature_videos_manifest.checked_cache_root`,
    so a symlinked root yields NOTHING rather than a listing of whatever the link
    points at — this list feeds eviction, and an entry here is a candidate for
    ``rmtree``. A symlinked release folder is skipped for the same reason.
    """
    out: list[tuple[str, Path, float, int]] = []
    try:
        entries = list(os.scandir(manifest_mod.checked_cache_root()))
    except OSError:
        return out
    for item in entries:
        if item.is_symlink() or not item.is_dir(follow_symlinks=False):
            continue
        try:
            folder = manifest_mod.release_dir(item.name)
        except ValueError:
            # Not a release folder name, so never created by us — and therefore
            # never removed by us. Deleting an unrecognized directory in the
            # user's data home is not this function's call to make.
            continue
        try:
            mtime = item.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append((item.name, folder, mtime, _dir_size(folder)))
    return out


# 8 PiB. Larger than any disk this cache runs on, so it never binds a real
# budget, and small enough that the byte count stays an exact int.
MAX_CACHE_BYTES = 1 << 53
_MAX_CACHE_MB = MAX_CACHE_BYTES / (1024 * 1024)


def cache_ceiling_bytes(max_mb: float) -> int:
    """Configured megabytes as a byte count. Never raises.

    The comparison happens BEFORE the multiply, which is the whole point: a
    configured 1e308 overflows ``max_mb * 1024 * 1024`` to ``inf`` on its own,
    and ``int(inf)`` raises ``OverflowError``. Clamping after the multiply would
    already be too late.

    A value that is not a finite positive number reads as "no size cap", the same
    answer a missing key gives, because a cap nobody can act on must not be the
    reason a cache starts deleting clips.
    """
    if not math.isfinite(max_mb) or max_mb <= 0:
        return 0
    if max_mb >= _MAX_CACHE_MB:
        return MAX_CACHE_BYTES
    return int(max_mb * 1024 * 1024)


#: The error a transfer returns when the ceiling was withdrawn between an
#: entry's poster and its clip. A sentinel rather than a flag so
#: ``_download_entry`` keeps its ``(ok, error)`` shape.
DENIED_MID_ENTRY = "download denied by the governance ceiling"

#: Failure reason for an entry whose media landed but whose receipt could not be
#: written. The media stays on disk; without the receipt it reads as not cached,
#: so the next pass re-fetches it and tries the receipt again.
RECEIPT_FAILED = "install receipt could not be written"


def evict(keep_release: str, *, max_bytes: int) -> list[str]:
    """Trim the cache to its size budget. Returns the releases removed. Blocking.

    *max_bytes* (0 = no bound) is satisfied by removing whole release folders
    oldest-first until the total fits. One bound, on purpose: a count of releases
    to keep was weighed and not shipped — the size cap already bounds the disk
    harm, and a second knob with an inert default would be honoured forever for
    an operator nobody has named.

    *keep_release* survives the bound, even when it alone exceeds the cap: a cap
    is not a reason to delete the clips this build plays. A single release over
    budget stays visible in the status endpoint's counts instead.
    """
    removed: list[str] = []
    folders = sorted(release_folders(), key=lambda row: row[2])  # oldest first
    evictable = [row for row in folders if row[0] != keep_release]

    if max_bytes <= 0:
        return removed
    total = sum(row[3] for row in folders)
    for release, path, _mtime, size in evictable:
        if total <= max_bytes:
            break
        if _remove_release(release, path):
            removed.append(release)
            total -= size
    return removed


def _remove_release(release: str, path: Path) -> bool:
    """Delete one release folder. Never raises.

    Proven at the point of deletion, on the DESCRIPTOR rather than the name:
    ``rmtree`` is irreversible, and a name re-resolved between a check and the
    delete is the window a rename-and-plant uses. The folder is opened with its
    ancestor chain pinned and the removal asks the kernel where the open
    directory really is (:func:`kiro_crew.pinned_fs.fd_real_path`); only
    ``<canonical root>/<release>`` is deleted, and everything under it is
    removed against the inodes one scan found (:func:`kiro_crew.pinned_fs.remove_tree_pinned`).
    Where a descriptor-relative walk is not available (Windows) the folder is
    held open instead — the handle blocks a rename of it and of every ancestor —
    proven the same way, emptied by name under that hold, and the empty folder
    removed last with ``rmdir``, which removes a junction AS a junction if one
    has taken the name by then.
    """
    try:
        expected = manifest_mod.checked_cache_root() / release
        if pinned_fs.supports_pinned_tree_walk():
            removed = _remove_release_pinned(path, expected)
        else:
            removed = _remove_release_held(path, expected)
    except (OSError, ValueError, RuntimeError, pinned_fs.PinnedPathRefusal):
        logger.warning("could not evict cached feature videos for %s", release, exc_info=True)
        return False
    if not removed:
        logger.warning("could not evict cached feature videos for %s: folder not emptied", release)
        return False
    logger.info("evicted cached feature videos for release %s", release)
    return True


def _witness(fd: int, expected: Path, what: str) -> None:
    """Raise ``CacheDirRefused`` unless the open directory *fd* really is *expected*."""
    real = pinned_fs.fd_real_path(fd)
    if real is None:
        raise manifest_mod.CacheDirRefused(f"{what} cannot be located on disk: {expected}")
    if not manifest_mod._same_location(real, expected):
        raise manifest_mod.CacheDirRefused(
            f"{what} is not where its name says: {expected} is {real}"
        )


def _remove_release_pinned(path: Path, expected: Path) -> bool:
    """POSIX: descriptor-pinned tree removal, approved by the descriptor's real path."""

    def _approve(root_fd: int, _tree: pinned_fs.PinnedTree) -> str | None:
        try:
            _witness(root_fd, expected, "feature-video release folder")
        except manifest_mod.CacheDirRefused as exc:
            return str(exc)
        return None

    outcome = pinned_fs.remove_tree_pinned(
        str(path),
        what="feature-video release folder",
        approve=_approve,
        refusal=manifest_mod.CacheDirRefused,
    )
    if outcome.reason:
        raise manifest_mod.CacheDirRefused(outcome.reason)
    return outcome.removed


def _remove_release_held(path: Path, expected: Path) -> bool:
    """Hold the folder open, prove it, empty it by name under the hold, then ``rmdir``."""
    fd = platform_compat.pin_directory(path)  # refuses a reparse point at the name
    try:
        _witness(fd, expected, "feature-video release folder")
        for entry in os.scandir(path):
            if platform_compat.is_link_or_junction(entry.path):
                platform_compat.unlink_link_or_junction(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
    finally:
        os.close(fd)
    # The handle is released only for this last step: rmdir never recurses, and
    # on a junction or link that has taken the name it removes the link itself.
    os.rmdir(path)
    return True


# ── The cache manager ──


class FeatureVideoCache:
    """Process-wide state for the hosted-clip cache.

    Holds the in-memory manifest and the download status. Deliberately NOT a
    per-request object: the manifest is instance-wide, and re-reading (let alone
    re-fetching) it per request would put a disk read or a network call on a
    polled route.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None  # created lazily inside the loop
        self._manifest: VideoManifest | None = None
        # A threading lock, not an asyncio one: the manifest is read from
        # executor THREADS, and a module-level asyncio primitive binds to
        # whichever loop first awaited it.
        self._manifest_lock = threading.Lock()
        self.status: dict[str, object] = {
            "download_state": STATE_IDLE,
            "downloading": None,
            "error": "",
        }

    # ── manifest ──

    def current_manifest(self) -> "VideoManifest | None":
        """The manifest for this build, from memory or the on-disk cache. Blocking.

        Never makes a network request: the request paths (``/next``, ``/status``)
        must not depend on the CDN being reachable, and the only component that
        fetches is the background task below.
        """
        with self._manifest_lock:
            if self._manifest is None:
                self._manifest = manifest_mod.load_cached_manifest(kiro_crew.__version__)
            return self._manifest

    def refresh_manifest(self) -> "VideoManifest | None":
        """Fetch, verify and cache the manifest. Blocking; makes a request.

        Network first, on-disk cache as the fallback: a build that can reach the
        CDN should pick up clips published after it shipped, and one that cannot
        must still play what it already holds.

        The caller MUST have established the ceiling permits downloading.
        """
        fetched, raw = manifest_mod.fetch_manifest(kiro_crew.__version__)
        if fetched is None:
            return self.current_manifest()
        manifest_mod.store_manifest(fetched, raw)
        with self._manifest_lock:
            self._manifest = fetched
        return fetched

    # ── counts ──

    def counts(self) -> tuple[int, int]:
        """``(cached, total)`` for the current manifest. Blocking (stats files)."""
        current = self.current_manifest()
        if current is None:
            return 0, 0
        cached = sum(1 for entry in current.entries if is_cached(entry, current.release))
        return cached, len(current.entries)

    # ── the background transfer ──

    async def ensure_all(self, *, unlimited: bool = False) -> bool:
        """Download every manifest entry not cached yet. Returns "everything cached".

        Serialized by an asyncio lock, so the boot task and a ``fetch-all`` click
        share one in-flight pass rather than racing two writers into the same
        staging files.

        *unlimited* lifts the rate limit, for the interactive path only.
        """
        if os.environ.get(SKIP_DOWNLOAD_ENV) == "1":
            return False
        dashboard = await asyncio.to_thread(_dashboard_config)
        if not bool(getattr(dashboard, "feature_videos_enabled", False)):
            self._set_state(STATE_DISABLED)
            return False
        if await asyncio.to_thread(manifest_mod.download_denied):
            # No manifest request and no transfer. Already-cached clips stay
            # playable: withdrawing what is on disk would be a second, separate
            # decision that the ceiling did not make.
            self._set_state(STATE_DENIED)
            return False
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._set_state(STATE_FETCHING_MANIFEST)
            current = await asyncio.to_thread(self.refresh_manifest)
            if current is None:
                self._set_state(STATE_FAILED, error="no verified manifest available")
                return False
            await asyncio.to_thread(self._evict_now, current.release, dashboard)
            pending = await asyncio.to_thread(self._pending_entries, current)
            if not pending:
                self._set_state(STATE_READY)
                return True
            rate = 0 if unlimited else DEFAULT_RATE_LIMIT_BYTES_PER_S
            last_error = ""
            for entry in pending:
                # Both switches are re-read per entry, not once before the loop. A
                # paced pass runs for minutes, so an operator turning the feature
                # off, or an administrator tightening the ceiling, mid-pass would
                # otherwise keep every remaining clip downloading under a permission
                # already withdrawn. The config read is a file read; the ceiling
                # is an action chokepoint and takes the audited answer, not the memo.
                dashboard = await asyncio.to_thread(_dashboard_config)
                if not bool(getattr(dashboard, "feature_videos_enabled", False)):
                    logger.info("feature-video download stopped: the feature was turned off")
                    self._set_state(STATE_DISABLED)
                    return False
                if await asyncio.to_thread(manifest_mod.download_denied):
                    logger.info("feature-video download stopped: the ceiling now denies it")
                    self._set_state(STATE_DENIED)
                    return False
                self._set_state(STATE_DOWNLOADING, downloading=entry.id)
                ok, err = await asyncio.to_thread(self._download_entry, entry, current, rate)
                if err == DENIED_MID_ENTRY:
                    logger.info("feature-video download stopped: the ceiling now denies it")
                    self._set_state(STATE_DENIED)
                    return False
                if not ok:
                    last_error = err
                    logger.info("feature video %r not cached: %s", entry.id, err)
            # Evicted twice: before the loop so the pass has room, and after it
            # because the bytes just written count against the same budget -- an
            # older release that fitted beside an empty current folder may not
            # fit beside a full one, and the budget is a promise about disk use
            # now, not at the next restart. The current release is never evicted.
            await asyncio.to_thread(self._evict_now, current.release, dashboard)
            cached, total = await asyncio.to_thread(self.counts)
            if cached == total:
                self._set_state(STATE_READY)
                return True
            # A failed pass is retried by the next gateway boot or a fetch-all
            # click, NOT by a retry loop here: a background task that kept
            # retrying on a host with no egress would spin for the process's
            # lifetime for a feature nobody is waiting on.
            self._set_state(STATE_FAILED, error=last_error or "download incomplete")
            return False

    def _pending_entries(self, current: VideoManifest) -> list[ManifestEntry]:
        """Manifest entries with no complete local copy. Blocking (stats files)."""
        return [e for e in current.entries if not is_cached(e, current.release)]

    def _evict_now(self, release: str, dashboard: object) -> None:
        max_mb = float(getattr(dashboard, "feature_videos_cache_max_mb", 500) or 0)
        evict(release, max_bytes=cache_ceiling_bytes(max_mb))

    def _download_entry(
        self, entry: ManifestEntry, current: VideoManifest, rate: int
    ) -> tuple[bool, str]:
        """Fetch one entry's poster and clip, both verified. Blocking.

        The poster goes first because it is the small file, and an entry with no
        poster cannot be shown even with the clip in place — failing on the cheap
        half saves the expensive transfer.
        """
        try:
            with manifest_mod.pinned_release_dir(current.release) as folder:
                return self._download_entry_into(folder, entry, current, rate)
        except (OSError, ValueError) as exc:
            return False, f"cache directory unavailable: {exc}"

    def _download_entry_into(
        self,
        folder: asset_downloader.PinnedTargetDir,
        entry: ManifestEntry,
        current: VideoManifest,
        rate: int,
    ) -> tuple[bool, str]:
        """Both transfers and both receipts of one entry, all through *folder*.

        *folder* is held open for the whole entry, so the directory the poster
        landed in is the directory the clip lands in and the receipts are written
        to — no step between them re-resolves the folder's name.
        """
        poster_ok, poster_err = asset_downloader.download_to(
            folder.describe(entry.poster),
            current.asset_url(entry.poster),
            sha256=entry.poster_sha256,
            # A bound, not a declared length: the manifest carries a poster sha but
            # no poster byte count, and max_bytes caps the transfer without
            # claiming to know its size (so it never discards a resumable partial).
            max_bytes=MAX_POSTER_BYTES,
            resume=True,
            rate_limit_bytes_per_s=rate,
            restrict_to_owner=True,
            label=f"feature-video poster {entry.id}",
            target_dir=folder,
        )
        if not poster_ok:
            return False, poster_err
        if not record_verified(folder, entry.poster, entry.poster_sha256):
            return False, RECEIPT_FAILED
        # The loop checked the ceiling before this entry's POSTER; the clip is a
        # second request, so it takes its own audited answer. Every outbound
        # request is preceded by a decision made for it — a withdrawal landing
        # during the poster transfer must not be followed by a clip GET made on
        # the poster's permit.
        if manifest_mod.download_denied():
            return False, DENIED_MID_ENTRY
        clip_ok, clip_err = asset_downloader.download_to(
            folder.describe(entry.file),
            current.asset_url(entry.file),
            sha256=entry.sha256,
            size=entry.bytes,
            resume=True,
            rate_limit_bytes_per_s=rate,
            restrict_to_owner=True,
            label=f"feature-video clip {entry.id}",
            target_dir=folder,
        )
        if not clip_ok:
            return False, clip_err
        if not record_verified(folder, entry.file, entry.sha256):
            return False, RECEIPT_FAILED
        return True, ""

    def _set_state(self, state: str, *, downloading: str | None = None, error: str = "") -> None:
        self.status = {"download_state": state, "downloading": downloading, "error": error}

    def note_denied(self) -> None:
        """Record that the ceiling refused a transfer, for the status endpoint."""
        self._set_state(STATE_DENIED)


_cache: FeatureVideoCache | None = None
_cache_lock = threading.Lock()
# Module-level anchor for the in-flight task: asyncio holds only weak references
# to tasks, so a caller that drops the return value could see the transfer
# collected mid-flight. Same reason ``embeddings._download_task`` exists.
_task: "asyncio.Task[bool] | None" = None


def feature_video_cache() -> FeatureVideoCache:
    """Process-wide cache singleton (shared by the gateway and the dashboard)."""
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = FeatureVideoCache()
        return _cache


def reset_feature_video_cache() -> None:
    """Drop the singleton (tests, ``KIROCREW_HOME`` changes)."""
    global _cache, _task
    with _cache_lock:
        _cache = None
        if _task is not None and not _task.done():
            _task.cancel()
        _task = None


def start_background_feature_video_download() -> "asyncio.Task[bool] | None":
    """Kick the boot-time cache fill. Returns the task, or None when it is a no-op.

    Called from gateway startup beside ``start_background_model_download``, for
    the same reason: boot must not wait on a transfer. Idempotent — a second call
    while a pass is in flight returns the existing task.

    The kill switch, the ceiling and the manifest are all checked INSIDE the
    task, not here: each needs blocking work (config load, profile resolution, a
    network request), and doing any of it on the event loop at startup is what
    this function exists to avoid.
    """
    global _task
    if os.environ.get(SKIP_DOWNLOAD_ENV) == "1":
        return None
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(feature_video_cache().ensure_all())
    return _task


# ── Serving ──


def resolve_served_path(release: str, name: str) -> "tuple[Path, str] | None":
    """Resolve one cache file from URL components to ``(path, content_type)``, or None.

    Every component is validated against the SAME rules the manifest applies, so
    a request can only name a file a manifest could have named — the release
    shape, the basename shape, AND a clip or poster suffix
    (:func:`~kiro_crew.feature_videos_manifest.served_content_type`). The suffix
    is the half that closes the origin: the folder is in the user's data home,
    so a file another local process planted there is reachable by name, and
    without the suffix rule a planted ``x.html`` would be served on the
    dashboard's own origin. With it, the route can only ever answer with a type
    the browser renders as media. aiohttp has already percent-decoded
    ``match_info``, so an encoded traversal arrives here as the literal
    characters and is caught by those rules rather than slipping past a check on
    the raw string.

    Containment is anchored to the CANONICAL cache root (``checked_cache_root``,
    which refuses a root that is itself a link), and neither component below it
    may be a symlink. Both halves are load-bearing, and the release
    folder is the half that is easy to miss: anchoring to the release folder
    instead lets that folder BE the boundary it is supposed to sit inside, so a
    ``<root>/<release>`` symlink pointing at ``/etc`` makes ``/etc`` the root and
    ``/etc/passwd`` an ordinary file "inside" it.

    A symlink is refused rather than followed even when its target is contained.
    The cache holds files a remote CDN named, and a link is not something a
    manifest can describe, so there is no legitimate reader to keep working.

    This validates and pre-checks; it does not open, and nothing it computed is
    trusted by the open. The route then opens root, release folder and file as a
    chain of no-follow opens, each relative to the descriptor before it
    (:func:`_open_served`), so a root OR a folder swapped for a link after these
    by-name checks is refused by the open itself rather than followed — the
    canonical path returned here is for the caller's messages, never re-walked.
    A swap after the open changes nothing the response reads. Residual, stated: a
    local process with the user's own permissions can still replace the FILE's
    bytes in place; the receipt certifies what was installed, not what is read
    back, and the user doc says so.
    """
    content_type = manifest_mod.served_content_type(name)
    if not content_type:
        return None
    try:
        manifest_mod.release_dir(release)  # raises on an unsafe release name
        root = manifest_mod.checked_cache_root()  # refuses a symlinked root
    except (ValueError, OSError, RuntimeError):
        return None
    served = root / release / name
    try:
        # lstat, never stat: stat() answers about a symlink's TARGET, which is
        # the question that lets the link through. Two components are checked
        # because either can be the link, and `name` is a single validated
        # basename, so the resolved root plus these two are the whole path.
        if not stat.S_ISDIR(os.lstat(served.parent).st_mode):
            return None
        if not stat.S_ISREG(os.lstat(served).st_mode):
            return None
    except (OSError, ValueError):
        return None
    return served, content_type


#: Read size for the streaming loop. Large enough that a 20-second clip is a few
#: dozen reads, small enough that a slow client does not pin a big buffer.
_SERVE_CHUNK_BYTES = 256 * 1024


def _open_served(release: str, name: str) -> "tuple[int, int] | None":
    """Open ``<root>/<release>/<name>`` for streaming as a chain of held descriptors.

    ``(fd, size)``, or None. Three opens, each with no-follow semantics and each
    RELATIVE to the descriptor before it, so no component is ever walked by
    name after it was checked:

    * the cache ROOT is opened on its own name with ``O_NOFOLLOW`` (its ancestor
      chain pinned one ``openat`` at a time, :func:`kiro_crew.pinned_fs.open_dir_pinned`)
      — a root swapped for a link is refused here, not followed into serving from
      wherever it points. This is the step a by-name ``resolve()`` of the root
      cannot provide: a resolve follows a link and hands back the target as if it
      were the root, and every later check anchored to that answer is anchored to
      the attacker's directory;
    * the RELEASE folder is opened relative to the held root, ``O_DIRECTORY |
      O_NOFOLLOW``;
    * the FILE is opened relative to the held folder, ``O_NOFOLLOW`` and
      non-blocking, so a planted FIFO is rejected by ``fstat`` instead of blocking.

    Windows has no ``dir_fd``: the root and the folder are pinned with the handle
    that blocks their rename (:func:`kiro_crew.platform_compat.pin_directory`,
    which refuses a reparse point), and the leaf is opened by name UNDER those
    held handles with the reparse-point flag (``open_file_no_reparse``).

    Then everything is asked of descriptors: the file must be a regular file with
    ``st_nlink == 1`` (a hard link IS the inode under a second name and passes
    every path rule; nothing this cache installs has a second name), and its real
    path (:func:`kiro_crew.pinned_fs.fd_real_path`) must be the held ROOT's real
    path plus ``<release>/<name>`` — both sides read off descriptors already open,
    so neither can be re-pointed. A platform that cannot read either refuses.
    The type and size come off the same ``fstat``.
    """
    root = manifest_mod.cache_root()
    held: list[int] = []
    try:
        if pinned_fs.supports_pinned_walk():
            root_fd = pinned_fs.open_dir_pinned(
                root, what="feature-video cache root", refusal=OSError
            )
            held.append(root_fd)
            rel_fd = os.open(release, pinned_fs.dir_flags(), dir_fd=root_fd)
            held.append(rel_fd)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(name, flags, dir_fd=rel_fd)
        else:
            root_fd = platform_compat.pin_directory(root)
            held.append(root_fd)
            held.append(platform_compat.pin_directory(root / release))
            fd = platform_compat.open_file_no_reparse(root / release / name, nonblocking=True)
    except OSError:
        pinned_fs.close_all(held)
        return None
    try:
        st = os.fstat(fd)
        real = pinned_fs.fd_real_path(fd)
        real_root = pinned_fs.fd_real_path(root_fd)
    except OSError:
        os.close(fd)
        return None
    finally:
        pinned_fs.close_all(held)
    expected = None if real_root is None else os.path.join(real_root, release, name)
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_nlink != 1
        or real is None
        or expected is None
        or os.path.normcase(real) != os.path.normcase(expected)
    ):
        os.close(fd)
        return None
    return fd, st.st_size


def _byte_range(request: web.Request, size: int) -> "tuple[int, int] | None":
    """``(start, count)`` the request asks for over a *size*-byte file, or None for 416.

    A ``<video>`` seeks with ``Range`` requests, so the route honours one range
    (``bytes=a-b``, ``bytes=a-``, ``bytes=-n``). No header means the whole file.
    """
    try:
        rng = request.http_range
    except ValueError:
        return None
    start, stop = rng.start, rng.stop
    if start is None and stop is None:
        return 0, size
    if start is None:
        start = 0
    if start < 0:
        start = max(size + start, 0)
        return start, size - start
    if start >= size:
        return None
    end = size if stop is None else min(stop, size)
    return start, end - start


def _read_at(fd: int, offset: int, count: int) -> bytes:
    """Read up to *count* bytes at *offset*. ``lseek``+``read``: ``pread`` is POSIX-only."""
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, count)


async def api_feature_video_file(request: web.Request) -> web.StreamResponse:
    """GET /feature-videos/{release}/{name} — one cached clip or poster.

    Read-only, one file per request, no listing. A path that does not resolve to
    a regular file inside the named release folder is a flat 404: telling "no
    such release" apart from "no such file" would answer questions about the
    user's cache contents that a 404 does not need to answer.

    The content type is the resolver's, from a fixed table keyed by the suffix
    the name was admitted on — never guessed from the name or sniffed from the
    bytes — and ``nosniff`` tells the browser not to second-guess it. Together
    with the suffix rule that is what makes this route unable to serve anything
    the browser would execute on this origin.

    The body is streamed from a descriptor reached through a chain of no-follow
    opens, root → release → file, each relative to the one before
    (:func:`_open_served`) — not through ``FileResponse``, which reopens by path
    after the check and would follow a link planted in between. ``Range`` is
    honoured so the player can seek.

    Every filesystem touch is off the loop: the resolve (two ``lstat`` and a
    strict ``resolve`` of the cache root), the open, the reads and the close.
    A cache on a slow or network-backed home directory would otherwise park the
    gateway's one event loop for the duration of each.
    """
    release = request.match_info.get("release", "")
    name = request.match_info.get("name", "")
    resolved = await asyncio.to_thread(resolve_served_path, release, name)
    if resolved is None:
        raise web.HTTPNotFound()
    _path, content_type = resolved
    opened = await asyncio.to_thread(_open_served, release, name)
    if opened is None:
        raise web.HTTPNotFound()
    fd, size = opened
    try:
        span = _byte_range(request, size)
        if span is None:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
        start, count = span
        # A satisfiable Range is answered 206 with Content-Range even when it
        # happens to cover the whole file: the player asked for a range and
        # reads the answer as one.
        partial = "Range" in request.headers
        resp = web.StreamResponse(status=206 if partial else 200)
        resp.headers["Content-Type"] = content_type
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Accept-Ranges"] = "bytes"
        resp.content_length = count
        if partial:
            resp.headers["Content-Range"] = f"bytes {start}-{start + count - 1}/{size}"
        await resp.prepare(request)
        if request.method == "HEAD":
            await resp.write_eof()
            return resp
        offset, remaining = start, count
        while remaining > 0:
            chunk = await asyncio.to_thread(
                _read_at, fd, offset, min(_SERVE_CHUNK_BYTES, remaining)
            )
            if not chunk:
                # The file shrank under us. End the body short rather than spin;
                # the declared length tells the client the response is incomplete.
                break
            await resp.write(chunk)
            offset += len(chunk)
            remaining -= len(chunk)
        await resp.write_eof()
        return resp
    finally:
        # Off the loop like the open and the reads: on a network-backed data home
        # a close can stall, and this handler shares the loop with everything.
        await asyncio.to_thread(os.close, fd)


async def api_feature_videos_fetch_all(request: web.Request) -> web.Response:
    """POST /api/feature-videos/fetch-all — start the transfer now, unpaced.

    Fire-and-forget: the pass can take a while and the client polls ``/status``,
    so the response says it was started rather than waiting for it. The rate
    limit is lifted because a user pressed a button and is watching a readout;
    the boot-time pass stays paced.

    Answers 403 when the ceiling forbids downloading, and 409 when the operator's
    kill switch is off, rather than starting a task that would immediately
    no-op: the caller asked for an action that cannot happen, and a silent "ok"
    would leave the dashboard polling for bytes that are never coming. The
    switch is checked first because it is the cheaper answer and involves no
    governance read.
    """
    del request  # the route takes no input; the manifest decides what is fetched
    global _task
    cache = feature_video_cache()
    dashboard = await asyncio.to_thread(_dashboard_config)
    if not bool(getattr(dashboard, "feature_videos_enabled", False)):
        cache._set_state(STATE_DISABLED)
        return web.json_response(
            {"error": "feature videos are turned off", "code": "feature_disabled"},
            status=409,
        )
    if await asyncio.to_thread(manifest_mod.download_denied):
        cache.note_denied()
        return web.json_response(
            {"error": "downloading feature videos is not permitted", "code": "governance_denied"},
            status=403,
        )
    if _task is None or _task.done():
        _task = asyncio.create_task(cache.ensure_all(unlimited=True))
    return web.json_response({"ok": True, "download_state": cache.status.get("download_state")})


__all__ = [
    "DEFAULT_RATE_LIMIT_BYTES_PER_S",
    "SERVE_PREFIX",
    "MAX_POSTER_BYTES",
    "RECEIPT_FAILED",
    "SKIP_DOWNLOAD_ENV",
    "STATE_DENIED",
    "STATE_DISABLED",
    "STATE_DOWNLOADING",
    "STATE_FAILED",
    "STATE_FETCHING_MANIFEST",
    "STATE_IDLE",
    "STATE_READY",
    "FeatureVideoCache",
    "api_feature_video_file",
    "api_feature_videos_fetch_all",
    "evict",
    "feature_video_cache",
    "is_cached",
    "poster_url_path",
    "release_folders",
    "record_verified",
    "recorded_sha256",
    "resolve_served_path",
    "reset_feature_video_cache",
    "start_background_feature_video_download",
]
