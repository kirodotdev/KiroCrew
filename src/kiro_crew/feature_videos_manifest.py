"""The signed catalog of hosted feature-video clips, and the gate over fetching it.

:mod:`kiro_crew.feature_videos` ships a static catalog compiled into the package.
That is the right shape for two clips, and the wrong one for a growing library:
every added video would need a release, and the media would have to ride in the
wheel. This module is the hosted half — a manifest published per release folder on
the CDN, listing the clips available for that release with a sha256 for each.

Three properties make it safe to act on bytes fetched from a CDN:

**The manifest is signed, and an unverified manifest is discarded WHOLE.** Not
"the bad entries are dropped" — the document is refused, because the signature
covers the document. It is verified against the same offline key ``cli.sh`` pins
for the update feed (:mod:`kiro_crew.platform.feed_trust`), under a distinct
``schema`` string so a manifest of one kind can never be replayed as the other.
The fail-safe direction is "no manifest": the static catalog still serves, and
nothing is downloaded.

**Every clip is sha256-pinned by the signed manifest, so the CDN is not trusted.**
A tampered object can only fail verification in
:mod:`kiro_crew.asset_downloader`; it cannot reach the cache directory.

**The browser never fetches from the CDN.** Only this gateway does — through
:mod:`kiro_crew.asset_downloader`, which verifies TLS, refuses cross-origin
redirects and checks the sha256 before a file is installed. An entry that is not
on disk yet is simply not offered; what the dashboard plays is always a
same-origin file whose bytes were verified here first.

Fetching any of this is governed by ``capabilities.feature_videos_download``
(fail-closed). A denied ceiling means no manifest request and no clip download:
only what is already on disk is offered.
"""

from __future__ import annotations

import errno
import http.client
import json
import logging
import math
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from kiro_crew import asset_downloader, pinned_fs, platform_compat
from kiro_crew import sel as _sel_mod
from kiro_crew.apps.version import parse_version
from kiro_crew.config.paths import config_dir
from kiro_crew.platform import governance_profiles as _governance_profiles
from kiro_crew.platform.feed_trust import verify_document_signature
from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

logger = logging.getLogger(__name__)

#: Schema string every hosted manifest MUST carry. Distinct from the CLI update
#: feed's so a signature valid for one document kind cannot be replayed as the
#: other: both are signed by the same offline key, and the schema is what makes
#: the two payloads non-interchangeable.
MANIFEST_SCHEMA = "kirocrew-feature-videos-manifest-v1"

#: Basename of the manifest inside a release folder, on the CDN and in the cache.
MANIFEST_FILENAME = "manifest.json"

#: Public CloudFront distribution in front of Kiro Crew's own asset bucket — the
#: same distribution the embedding model is served from, a different prefix. The
#: signature, not the origin, is the trust anchor.
DEFAULT_CDN_BASE = "https://d3j0sthz5doyui.cloudfront.net/feature-videos"

#: Operator override for the manifest URL (mirrored or air-gapped deployments).
#: Must be ``https://``. The process ENVIRONMENT is the only override channel, on
#: purpose: it is set by whoever launches the gateway, where a config-file knob
#: would be writable by the agent's own tools — and a url the agent can choose
#: is a request the gateway makes to any https host on the agent's behalf,
#: before the signature check has anything to check. The value is a request
#: target, not a preference, so it stays out of ``config.json``.
MANIFEST_URL_ENV = "KIROCREW_FEATURE_VIDEOS_MANIFEST_URL"

#: Governance scope. Fail-closed, in the egress family with
#: ``capabilities.telemetry`` and ``capabilities.publish``.
DOWNLOAD_SCOPE = "capabilities.feature_videos_download"

#: Tool name on the SEL ``governance_decision`` row, so an operator reading the
#: trail can tell this decision apart from the other capability probes.
AUDIT_TOOL = "feature_videos_download"

#: Surface key for the evaluation. Pinned rather than taken from the request's
#: ``X-Session-Key``, which is CALLER-CONTROLLED: classifying by it would let a
#: request naming another surface dodge a profile bound to the dashboard. Same
#: pin, same rationale, as ``dashboard/social_share.py``.
DASHBOARD_SURFACE_KEY = "dashboard:ui"

_UNEVALUABLE_REASON = "governance unavailable (fail-closed)"

#: Last answer :func:`download_denied` produced. Its one reader is
#: :func:`download_permitted_cached`, which serves it to the polled ``/status``
#: route for up to :data:`_GOVERNANCE_TTL_SECS` so a progress poll does not write
#: an audited SEL row per request. Starts denied: a process that has not evaluated
#: the ceiling yet reports "off" — fail-closed in the direction that costs a
#: lagging permit rather than a lagging denial (and the cached reader evaluates on
#: its first call, so the default is never actually served). Refreshed by every
#: real evaluation — the boot task, each clip request, ``fetch-all`` — so the memo
#: tracks the ceiling within a request of it changing. Process-wide state about
#: the MACHINE, not about any caller, which is why a module global is the right
#: home.
_last_download_permitted = False

#: How long :func:`download_permitted_cached` may serve the memo before it
#: re-evaluates. Sixty seconds bounds a polled route to one audited SEL row per
#: minute; the action chokepoints do not use it and are never stale.
_GOVERNANCE_TTL_SECS = 60.0

#: ``time.monotonic()`` of the last real evaluation. 0.0 means "never", which makes
#: the first cached read evaluate rather than serve the denied default.
_last_download_check_ts = 0.0

#: How many earlier minor lines to try when this release has no manifest. Three
#: covers "the operator is a few releases behind and no new clips shipped since",
#: and bounds the boot-time cost at a handful of small JSON requests: at most
#: this many plus two (the exact release and its minor line's ``.0``).
_FALLBACK_MINORS = 3

#: Whole-document size cap, matching the publisher's own
#: (``scripts/feature-videos/_manifest.py`` in the publishing tool, a sibling
#: change that is not part of this tree). Bounds what an unauthenticated
#: response can make us buffer before the signature is even checked. It has to sit
#: ABOVE :data:`_SIGNED_PAYLOAD_MAX_BYTES`, because the document is the payload
#: plus a signature — a fetch cap at the payload cap would reject a manifest that
#: is exactly at its published limit.
_MANIFEST_MAX_BYTES = 1024 * 1024

#: Longest ``duration_s`` an entry may declare. An intro clip is seconds long; a
#: value past an hour is not a clip this feature plays, and a value the float
#: conversion cannot even represent must never reach it (a 310-digit integer
#: raises ``OverflowError`` out of ``float()``, and one signed manifest could take
#: the whole parse down with it). Out-of-range reads as "unknown", which is what
#: the frontend shows for ``0.0``.
_MAX_DURATION_S = 3600.0

#: Manifest request timeout. Short: this runs at boot behind a background task,
#: and a hanging CDN must not hold the task open for the download timeout.
_MANIFEST_TIMEOUT_SECS = 10

#: Signed-payload cap, passed to the shared verifier. The CLI feed's own cap
#: stays what it was — a manifest of clips is a larger document than a channel
#: descriptor and needs its own bound. The value is the PUBLISHER's
#: ``DEFAULT_MAX_PAYLOAD_BYTES``: a cap tighter than the one a release is built
#: against would discard a valid manifest, and the discard is whole-document, so
#: the failure would be "no videos" rather than "one clip missing".
_SIGNED_PAYLOAD_MAX_BYTES = 256 * 1024

#: Per-clip size ceiling. A feature intro is a ~20 second screen recording; a
#: manifest asking for more than this is either wrong or hostile, and the
#: declared size is checked against the pin before a byte is written.
_MAX_ENTRY_BYTES = 64 * 1024 * 1024

#: Cap on how many entries one manifest may carry — a cheap coherence check, not the
#: real one. :data:`_SIGNED_PAYLOAD_MAX_BYTES` is what actually limits a manifest,
#: and this sits far enough above it that a document the publisher would emit
#: cannot trip this instead: tripping it discards the whole manifest, and "we
#: refused a valid release because it listed too many clips" is not a failure
#: worth having.
_MAX_ENTRIES = 1000

#: A media basename: no directory component, no traversal, no escaping. An
#: allowlist rather than a denylist, because this string becomes a path segment
#: on disk AND a URL segment the browser fetches — the two decodings do not
#: agree, so enumerating what is forbidden is the losing side of that.
_SAFE_BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

#: A 64-character lowercase hex digest.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: A release folder name. This is a path segment under the cache root AND a URL
#: segment, so it is validated as strictly as a filename: digits and dots only,
#: each component bounded.
#:
#: One to four components, because the publisher accepts a bare numeric version of
#: any depth (``scripts/feature-videos/_manifest.py``, in the publishing tool that
#: ships separately) while Kiro Crew's own
#: releases are always ``major.minor.patch``. Accepting what the publisher can emit
#: costs nothing — the fetch path only ever ASKS for ``major.minor.patch`` folders
#: (:func:`release_candidates`) — and refusing it would discard a valid release
#: whole.
_RELEASE_RE = re.compile(r"^\d{1,5}(?:\.\d{1,5}){0,3}$")

_CLIP_SUFFIX = ".mp4"
_POSTER_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


# ── Governance ──


def download_denied() -> bool:
    """Whether the ceiling forbids fetching hosted feature videos. Blocking.

    FAIL-CLOSED. The two dispositions are not symmetric: a wrong-DENY withholds
    an intro clip a user might have enjoyed, while a wrong-PERMIT makes an
    outbound request to a vendor CDN on a fleet that forbade vendor egress —
    which puts this row with ``capabilities.telemetry`` / ``capabilities.publish``
    rather than with the advisory probes. ``fail_closed`` also makes the
    evaluator audit an unevaluable ceiling as a critical SEL event.

    Consulted at three chokepoints, because any one alone is a half-control: each
    manifest request (:func:`fetch_manifest` re-asks before every candidate url),
    each clip request in the download pass, and the ``fetch-all`` route. The
    settings panel reads the same answer as ``download_enabled`` on
    ``/api/feature-videos/status`` — through :func:`download_permitted_cached`,
    the memo, not a fresh audited row — so it never guesses.
    """
    global _last_download_permitted
    try:
        # Looked up on the MODULE at call time, not bound by `from ... import`: the
        # governance tests patch `governance_profiles.vet_and_audit` at its
        # definition site, and an import-time name binding would hold the original.
        decision = _governance_profiles.vet_and_audit(
            DOWNLOAD_SCOPE,
            "",
            session_key=DASHBOARD_SURFACE_KEY,
            tool_name=AUDIT_TOOL,
            log_warning=False,
            fail_closed=True,
        )
    except Exception:
        # governance_permits converts its own internal errors into a denying
        # Decision, so reaching here means the import, the composition or the
        # call itself failed — the ceiling is unevaluable, which is the same
        # condition as a degrade. Fail closed, and record what the seam could not.
        logger.debug("feature-video download governance probe failed; denying", exc_info=True)
        _audit_unevaluable()
        _last_download_permitted = False
        return True
    _last_download_permitted = bool(getattr(decision, "permitted", False))
    return not _last_download_permitted


def download_permitted_cached() -> bool:
    """Permitted?, re-evaluated at most once per :data:`_GOVERNANCE_TTL_SECS`. Blocking.

    For a POLLED route. ``/api/feature-videos/status`` is polled while a download
    runs, and :func:`download_denied` writes a ``governance_decision`` SEL row on
    every call — so evaluating per request turns a progress readout into an
    audit-log flood and a disk write per poll, which also buries the rows that
    record a real decision.

    The split this creates is the point: an audited evaluation happens where the
    ACTION is (each clip download, ``fetch-all``), and a polled read gets a bounded
    stale answer. Worst case a denial is up to a minute old on one offer, while the
    transfer it would stop is re-checked before every request it makes.

    Fail-closed on the cold path: with nothing evaluated yet the first call
    evaluates rather than returning the denied default, so a fresh process reports
    the truth instead of claiming the feature is off.
    """
    global _last_download_check_ts
    now = time.monotonic()
    if _last_download_check_ts and (now - _last_download_check_ts) < _GOVERNANCE_TTL_SECS:
        return _last_download_permitted
    _last_download_check_ts = now
    return not download_denied()


def _audit_unevaluable() -> None:
    """Best-effort SEL record for the path ``vet_and_audit`` never reached."""
    try:
        # `_sel_mod.sel()` rather than a bound `sel`: the suite patches
        # `kiro_crew.sel.sel`, and a module-attribute lookup at call time sees it.
        _sel_mod.sel().log_governance_decision(
            session_key=DASHBOARD_SURFACE_KEY,
            tool_name=AUDIT_TOOL,
            scope=DOWNLOAD_SCOPE,
            item="",
            outcome="denied",
            reason=_UNEVALUABLE_REASON,
        )
    except Exception:
        # SEL writes to a file, and an audit failure must never wedge the probe.
        pass


# ── Shapes ──


@dataclass(frozen=True)
class ManifestEntry:
    """One hosted clip, as the signed manifest declares it.

    Frozen for the same reason :class:`~kiro_crew.feature_videos.VideoEntry` is:
    a handler that could mutate one in place would leak a request's edit into
    every later request in the process.

    ``file`` and ``poster`` are BASENAMES, not paths and not URLs. The release
    folder they sit in is the manifest's, so an entry cannot name a location —
    which is what keeps both the on-disk path and the served URL derivable from
    values this module validated.
    """

    id: str
    feature: str
    title: str
    description: str
    file: str
    poster: str
    sha256: str
    poster_sha256: str
    bytes: int
    duration_s: float
    doc: str
    used_when: tuple[str, ...] = ()
    min_version: str = ""


@dataclass(frozen=True)
class VideoManifest:
    """A verified manifest for one release folder."""

    release: str
    cdn_base: str
    generated_at: str
    entries: tuple[ManifestEntry, ...]

    def asset_url(self, name: str) -> str:
        """Absolute CDN url for a basename in this manifest's release folder."""
        return f"{self.cdn_base.rstrip('/')}/{self.release}/{name}"


# ── Paths ──


def cache_root() -> Path:
    """Root of the local clip cache. Respects ``KIROCREW_HOME``.

    Through ``config_dir()`` rather than a raw environment read, so tilde
    expansion and unsafe-system-directory rejection match the rest of the config
    stack.
    """
    return config_dir() / "feature-videos"


def release_dir(release: str) -> Path:
    """Cache folder for *release*. Raises ``ValueError`` on an unsafe name.

    Raises rather than returning a fallback: every caller here derives *release*
    from a validated manifest or from the running version, so an unsafe value is
    a bug in this module and must not silently resolve to a path.
    """
    if not _RELEASE_RE.match(release):
        raise ValueError(f"unsafe release folder name: {release!r}")
    return cache_root() / release


def cached_manifest_path(release: str) -> Path:
    """Where the verified manifest for *release* is kept.

    INSIDE the release folder, so evicting a release removes its manifest with
    its clips in one ``rmtree`` and cannot leave a manifest advertising files
    that are gone.
    """
    return release_dir(release) / MANIFEST_FILENAME


class CacheDirRefused(OSError):
    """A cache directory is a symlink, or does not sit where its name says.

    An ``OSError`` so every caller's existing "the cache is unavailable" branch
    handles it, and a distinct class so a test can tell the refusal from a disk
    error.
    """


def _refuse_link(path: Path, label: str) -> None:
    """Refuse *path* if it exists as anything but a real directory. lstat, never stat.

    A Windows directory junction is a reparse point that ``lstat`` reports as a
    plain directory — ``S_ISLNK`` is False for it — and a non-admin user can
    create one, so it is refused by its reparse tag as well
    (``platform_compat.is_link_or_junction``). Following one would put every
    write and every eviction wherever it points, exactly like a symlink.

    A by-name check, so it settles nothing on its own: a link planted after it
    is caught by the no-follow open and the descriptor witness that every
    write and delete path runs next. What it adds is the early, readable
    refusal for a link that is already there.
    """
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return  # not there yet — the mkdir that follows creates a real directory
    if stat.S_ISLNK(mode) or platform_compat.is_link_or_junction(path):
        raise CacheDirRefused(f"{label} is a symlink; refusing to write through it: {path}")
    if not stat.S_ISDIR(mode):
        raise CacheDirRefused(f"{label} is not a directory: {path}")


def checked_cache_root() -> Path:
    """The cache root's CANONICAL path, refused if the root itself is a link.

    The one anchor for every operation under the cache — the write
    (:func:`ensure_cache_dir`), the read (``resolve_served_path``) and the
    delete (``release_folders`` / ``_remove_release``) all derive their
    containment from this. A symlinked root moves every release folder at once:
    written through, it puts files wherever the link points; scanned, it hands
    the eviction pass real directories OUTSIDE the cache to ``rmtree``. Refusing
    it here, once, is what keeps the three paths from each needing (and one of
    them forgetting) the same check. Raises :class:`CacheDirRefused` for a link,
    ``FileNotFoundError`` when the root does not exist yet.

    A by-name answer, and used as one: the value is the EXPECTATION a descriptor
    is compared against, never the thing that is opened. Every path that opens
    under the cache opens the root on its own name with no-follow semantics and
    walks down relative to that descriptor (``pinned_release_dir``,
    ``_open_served``, ``_remove_release_pinned``), so a root swapped for a link
    between the check and the resolve here is refused by that open — the
    poisoned expectation then never meets a descriptor it could vouch for.
    """
    root = cache_root()
    _refuse_link(root, "feature-video cache root")
    return root.resolve(strict=True)


def ensure_cache_dir(release: str) -> Path:
    """Create the release folder owner-only and return it. Raises on a link.

    Owner-only for the same reason the state file is: which clips this install
    has fetched is a behavioural signal, and on a shared box it must not be
    world-readable. An existing folder is tightened too — a directory created
    before this guarantee existed would otherwise stay readable.

    This is the ONE gate every cache write passes through — the manifest store
    and both clip transfers ask it for the folder — and it holds the same
    invariant the serving route already proves on the read side
    (``feature_videos_cache.resolve_served_path``): neither the cache root nor the
    release folder may be a symlink, and the folder handed back must be a real
    directory whose canonical path is ``<canonical root>/<release>``. Without it
    a planted ``<root>/<release>`` link makes ``atomic_write`` and the staging
    file land wherever the link points, and the root is checked too because a
    planted root moves every release folder at once. A link is refused rather
    than followed even when its target is inside the cache: a manifest names
    files, so a link has no legitimate writer to keep working.

    Nothing here is created or written BY NAME after a check. The cache root is
    opened with no-follow semantics and held, and the descriptor has to prove it
    really is the canonical root (:func:`kiro_crew.pinned_fs.fd_real_path`) BEFORE
    anything is created under it — a root swapped for a link after the by-name
    check is refused by the open, not followed into creating ``<target>/<release>``
    somewhere else. The release folder is then created and opened relative to that
    held root (no-follow on the leaf), proven the same way, and only then tightened
    — through its descriptor (``fchmod``) on POSIX, and on Windows by name under
    the held handle, which blocks a rename of the folder and everything above it
    for as long as it is open. A ``chmod`` by name after a by-name check would
    follow a link planted in between and land ``0700`` on whatever it points at;
    that write has no undo, so every check is settled on the thing already held.

    What this returns is a PATH, and a path is re-resolved by whatever opens it
    next. The writers therefore do not open it: they go through
    :func:`pinned_release_dir`, which holds the folder open and proves, on the
    descriptor, that it is the folder this function checked. This function is
    the create-and-check step; that one is the hold.
    """
    root = cache_root()
    path = release_dir(release)  # raises ValueError on an unsafe name
    _refuse_link(root, "feature-video cache root")
    # The root by name: mkdir never writes THROUGH a link (an existing target only
    # makes it a no-op), and a root that became a link is refused by the pinned
    # open and the witness below. The release folder is never created by name.
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Early, readable refusal for a link already there (a junction included); the
    # no-follow opens and the descriptor witnesses below are what settle it.
    _refuse_link(path, "feature-video release folder")
    fd = _create_release_dir_held(root, release)
    try:
        try:
            expected = checked_cache_root() / release
        except RuntimeError as exc:  # a symlink loop above the cache
            raise CacheDirRefused(f"feature-video cache path cannot be resolved: {path}") from exc
        real = pinned_fs.fd_real_path(fd)
        if real is None:
            raise CacheDirRefused(f"feature-video release folder cannot be located on disk: {path}")
        if not _same_location(real, expected):
            raise CacheDirRefused(
                f"feature-video release folder is not where its name says: {path} is {real}"
            )
        _tighten_held_dir(fd, path)
    finally:
        os.close(fd)
    return path


def _create_release_dir_held(root: Path, release: str) -> int:
    """Create (or meet) ``<root>/<release>`` under a HELD, VERIFIED root; return it OPEN.

    Order matters, and it is the whole point: the root is opened first with
    no-follow semantics on its own name (a link there is refused, not followed),
    the descriptor's real path must equal the canonical root, and only then is the
    release folder created — relative to that descriptor on POSIX, so the name the
    check passed and the directory the ``mkdir`` lands in are the same object. A
    root that became a link between the by-name check and this call cannot be
    created under: nothing is resolved by name once the root is held.

    POSIX: :func:`kiro_crew.pinned_fs.open_dir_pinned` for the root (ancestor
    chain pinned one ``openat`` at a time, ``O_NOFOLLOW`` on the root's name), then
    ``mkdir``/``open`` with ``dir_fd``, the leaf opened ``O_NOFOLLOW`` — a link or a
    file at the release name is refused as :class:`CacheDirRefused`. Windows has no
    ``dir_fd``: the root is pinned with the handle that blocks its rename
    (:func:`kiro_crew.platform_compat.pin_directory`, which refuses a reparse
    point), verified the same way, and the release folder is created by name UNDER
    that held handle — the root cannot be swapped while it is open — then pinned
    and refused if it is a link, junction or file.
    """
    what_root = "feature-video cache root"
    if pinned_fs.supports_pinned_walk():
        root_fd = pinned_fs.open_dir_pinned(root, what=what_root, refusal=CacheDirRefused)
    else:
        try:
            root_fd = platform_compat.pin_directory(root)
        except NotADirectoryError as exc:
            raise CacheDirRefused(
                f"refusing to write through a symlinked {what_root}: {root}"
            ) from exc
    try:
        try:
            canonical = checked_cache_root()
        except RuntimeError as exc:  # a symlink loop above the cache
            raise CacheDirRefused(f"feature-video cache path cannot be resolved: {root}") from exc
        real = pinned_fs.fd_real_path(root_fd)
        if real is None:
            raise CacheDirRefused(f"{what_root} cannot be located on disk: {root}")
        if not _same_location(real, canonical):
            raise CacheDirRefused(f"{what_root} is not where its name says: {root} is {real}")
        if pinned_fs.supports_pinned_walk():
            try:
                os.mkdir(release, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            try:
                return os.open(release, pinned_fs.dir_flags(), dir_fd=root_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise CacheDirRefused(
                        "refusing to write through a symlinked feature-video release "
                        f"folder: {root / release}"
                    ) from exc
                raise
        path = root / release
        path.mkdir(exist_ok=True, mode=0o700)
        try:
            return platform_compat.pin_directory(path)
        except NotADirectoryError as exc:
            raise CacheDirRefused(
                f"refusing to write through a symlinked feature-video release folder: {path}"
            ) from exc
    finally:
        os.close(root_fd)


def _tighten_held_dir(fd: int, path: Path) -> None:
    """Owner-only on the folder *fd* holds. Best-effort, like ``make_owner_only_dir``.

    POSIX applies the mode to the DESCRIPTOR, so the name is never consulted.
    Windows has no ``fchmod``; the DACL is applied by name while the handle is
    held, and the handle is what keeps that name pointing at this folder. A
    failure is logged and the folder is still used: an un-tightened cache is a
    lesser harm than no cache, the same call ``make_owner_only_dir`` makes.
    """
    try:
        if platform_compat.IS_POSIX:
            # 0o700 is the RESTRICTIVE mode for a directory; Semgrep's rule reads
            # it as permissive and suggests 0o644, which would drop owner-execute
            # and add world-read. Same suppression as platform_compat.
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
            os.fchmod(fd, 0o700)
        else:
            platform_compat.restrict_dir_to_owner(path)
    except OSError:
        logger.warning("could not restrict %s to owner-only", path, exc_info=True)


def _same_location(real: str, expected: Path) -> bool:
    """Whether the kernel's path for an open descriptor names *expected*.

    ``normcase`` so Windows compares case-insensitively with one separator;
    both sides are already final paths (``resolve(strict=True)`` and
    ``GetFinalPathNameByHandleW`` / ``/proc/self/fd`` / ``F_GETPATH``), so no
    further resolution is applied to either — resolving again would re-follow
    whatever a name points at now, which is the question this check exists not
    to ask.
    """
    return os.path.normcase(real) == os.path.normcase(str(expected))


@contextmanager
def pinned_release_dir(release: str) -> Iterator[asset_downloader.PinnedTargetDir]:
    """Hold the release folder open for a write, proven to be inside the cache.

    The write-side twin of the serving route's descriptor check. Every write
    under the cache — the manifest store, both media transfers, the install
    rename and the receipts — happens inside this context and addresses the
    folder through the handle it yields, never by path:

    1. :func:`ensure_cache_dir` creates the folder through the pinned root,
       proves it on a descriptor and tightens it through that descriptor;
    2. :func:`kiro_crew.asset_downloader.pin_target_dir` opens it and holds it —
       on POSIX with the ancestor chain pinned one ``openat`` at a time and every
       later operation descriptor-relative, on Windows with a handle that stops
       the folder and everything above it from being renamed away;
    3. the descriptor's REAL path (:func:`kiro_crew.pinned_fs.fd_real_path`) must
       be ``<canonical root>/<release>``. This is the step that makes the first
       two sufficient: a root or release folder swapped for a link between the
       check and the open is followed by the open, and pinning would then hold
       the wrong directory perfectly. The kernel's answer for the inode already
       held cannot be re-pointed, so it is the containment witness. A platform
       that cannot read it refuses rather than trusting the name.

    Raises :class:`CacheDirRefused` (an ``OSError``) when any step disagrees, so
    the callers' existing "cache unavailable" branches handle it.
    """
    path = ensure_cache_dir(release)
    expected = checked_cache_root() / release
    with asset_downloader.pin_target_dir(path, what="feature-video release folder") as pinned:
        real = pinned_fs.fd_real_path(pinned.fd)
        if real is None:
            raise CacheDirRefused(f"feature-video release folder cannot be located on disk: {path}")
        if not _same_location(real, expected):
            raise CacheDirRefused(
                f"feature-video release folder is not where its name says: {path} is {real}"
            )
        yield pinned


def is_safe_media_name(name: object) -> bool:
    """Whether *name* is a basename a manifest could legitimately carry.

    The SHAPE rule only — characters and length. The parser adds a suffix rule
    per field (:data:`_CLIP_SUFFIX`, :data:`_POSTER_SUFFIXES`), and the serving
    route asks the union of those through :func:`served_content_type`, so a URL
    can only name a file the parser could have admitted.
    """
    return isinstance(name, str) and bool(_SAFE_BASENAME_RE.match(name)) and ".." not in name


#: What the serving route answers for each suffix the parser admits. A fixed
#: table rather than ``mimetypes``: the route reads from a folder the user's own
#: processes can write to, and a guessed type is how a planted ``.html`` or
#: ``.svg`` would come back as something the browser executes on this origin.
#: Every suffix here is a media type the browser renders, never runs.
_SERVED_CONTENT_TYPES: dict[str, str] = {
    _CLIP_SUFFIX: "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
assert set(_SERVED_CONTENT_TYPES) == {_CLIP_SUFFIX, *_POSTER_SUFFIXES}


def served_content_type(name: object) -> str:
    """The content type the cache route serves *name* as, or ``""`` to refuse it.

    The serving route's whole name rule: the parser's shape rule
    (:func:`is_safe_media_name`) AND one of the suffixes the parser admits for a
    clip or a poster. A name the parser could not have written into the cache is
    not served, whatever is on disk under it — the release folder is inside the
    user's data home, so "it exists" is not evidence a manifest put it there.
    """
    if not is_safe_media_name(name):
        return ""
    assert isinstance(name, str)  # narrowed by is_safe_media_name
    _, dot, suffix = name.rpartition(".")
    return _SERVED_CONTENT_TYPES.get(f".{suffix.lower()}", "") if dot else ""


# ── Release resolution ──


def running_release(version: str) -> str:
    """The release folder name for *version*, or ``""`` if it cannot be parsed.

    The numeric release segment only: an insider build stamped ``0.6.0rc3`` and
    the stable ``0.6.0`` read the same folder, because a prerelease of a version
    plays that version's clips.
    """
    try:
        major, minor, patch = parse_version(version)
    except ValueError:
        return ""
    return f"{major}.{minor}.{patch}"


def release_candidates(version: str) -> tuple[str, ...]:
    """Release folders to try for *version*, newest first.

    A release with no manifest of its own falls back to the newest LOWER release
    that has one — a build cut with no new clips must still play the existing
    library rather than showing nothing. The chain is, in order: this exact
    release; this minor line's ``.0`` when the running patch is not 0 (clips are
    published per minor line, so ``0.6.3`` reads ``0.6.0``'s folder); then the
    ``.0`` of up to :data:`_FALLBACK_MINORS` preceding minors.

    Never walks DOWN a major boundary. A major bump is where a clip is most
    likely to show a UI the running build does not have, which is the one thing a
    stale intro must not do.
    """
    base = running_release(version)
    if not base:
        return ()
    # running_release() always emits three components (parse_version pads), so this
    # unpack is safe regardless of what shape a MANIFEST is allowed to declare.
    major, minor, patch = (int(part) for part in base.split("."))
    out = [base]
    if patch:
        out.append(f"{major}.{minor}.0")
    for step in range(1, _FALLBACK_MINORS + 1):
        if minor - step < 0:
            break
        out.append(f"{major}.{minor - step}.0")
    # dict.fromkeys: order-preserving dedupe, for the patch==0 case where the
    # second entry would repeat the first.
    return tuple(dict.fromkeys(out))


# ── Parsing and verification ──


def _safe_media_name(value: object, suffixes: tuple[str, ...]) -> str:
    """Return *value* if it is a safe media basename with one of *suffixes*."""
    if not is_safe_media_name(value):
        return ""
    assert isinstance(value, str)  # narrowed by is_safe_media_name
    return value if value.lower().endswith(suffixes) else ""


def _bounded_duration(value: object) -> float:
    """*value* as a finite duration within ``[0, _MAX_DURATION_S]``, else ``0.0``.

    Never raises. ``float()`` of an integer too large for a double raises
    ``OverflowError``; ``json`` also admits ``Infinity`` and ``NaN`` literals. All
    of those, a negative, a bool and a non-number read as "unknown" rather than
    ending the parse of a manifest whose every other field is good.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        duration = float(value)
    except OverflowError:
        return 0.0
    if not math.isfinite(duration) or duration < 0.0 or duration > _MAX_DURATION_S:
        return 0.0
    return duration


def _parse_entry(
    raw: object, *, doc_allowlist: frozenset[str] | set[str]
) -> "ManifestEntry | None":
    """Validate one manifest entry. Returns None (with a reason logged) if unsafe."""
    if not isinstance(raw, dict):
        return None
    video_id = raw.get("id")
    reason = ""
    if not isinstance(video_id, str) or not _SAFE_BASENAME_RE.match(video_id):
        logger.warning("hosted feature video dropped: unsafe id %r", video_id)
        return None
    clip = _safe_media_name(raw.get("file"), (_CLIP_SUFFIX,))
    poster = _safe_media_name(raw.get("poster"), _POSTER_SUFFIXES)
    sha = raw.get("sha256")
    poster_sha = raw.get("poster_sha256")
    size = raw.get("bytes")
    doc = raw.get("doc")
    if not clip:
        reason = f"unsafe file {raw.get('file')!r}"
    elif not poster:
        reason = f"unsafe poster {raw.get('poster')!r}"
    elif not isinstance(sha, str) or not _SHA256_RE.match(sha):
        reason = "sha256 is not a 64-character lowercase hex digest"
    elif not isinstance(poster_sha, str) or not _SHA256_RE.match(poster_sha):
        reason = "poster_sha256 is not a 64-character lowercase hex digest"
    elif isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= _MAX_ENTRY_BYTES:
        reason = f"bytes {size!r} outside 1..{_MAX_ENTRY_BYTES}"
    elif not isinstance(doc, str) or doc not in doc_allowlist:
        # The same gate the static catalog applies, so a hosted clip cannot
        # point the "Learn more" link at an internal design note either. The
        # type check comes first: ``in`` on a set hashes its operand, and a
        # list or dict at this key would raise instead of being dropped.
        reason = f"doc {doc!r} is not in the tips doc allowlist"
    if reason:
        logger.warning("hosted feature video %r dropped: %s", video_id, reason)
        return None
    min_version = raw.get("min_version", "")
    if not isinstance(min_version, str):
        min_version = ""
    if min_version:
        try:
            parse_version(min_version)
        except ValueError:
            logger.warning(
                "hosted feature video %r dropped: unparseable min_version %r",
                video_id,
                min_version,
            )
            return None
    raw_signals = raw.get("used_when", ())
    signals = (
        tuple(s for s in raw_signals if isinstance(s, str) and s)
        if isinstance(raw_signals, (list, tuple))
        else ()
    )
    duration = _bounded_duration(raw.get("duration_s", 0.0))
    return ManifestEntry(
        id=video_id,
        feature=str(raw.get("feature", video_id)),
        title=str(raw.get("title", "")),
        description=str(raw.get("description", "")),
        file=clip,
        poster=poster,
        sha256=sha,  # type: ignore[arg-type]
        poster_sha256=poster_sha,  # type: ignore[arg-type]
        bytes=size,  # type: ignore[arg-type]
        duration_s=duration,
        doc=doc,  # type: ignore[arg-type]
        used_when=signals,
        min_version=min_version,
    )


def parse_manifest(raw: object) -> "VideoManifest | None":
    """Validate a VERIFIED manifest document into a :class:`VideoManifest`.

    Signature checking is :func:`verify_manifest` — this is the structural half,
    and it is deliberately separate so the order is impossible to get wrong: a
    caller that reaches this function has already established the document is the
    one the publisher signed.

    An individual malformed ENTRY is dropped with a logged reason; a malformed
    DOCUMENT (wrong schema, no release, off-origin ``cdn_base``) returns None.
    The asymmetry is on purpose: a bad entry costs one clip, while a bad document
    would make every path derived from it wrong.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("schema") != MANIFEST_SCHEMA:
        logger.warning("feature-video manifest rejected: schema %r", raw.get("schema"))
        return None
    release = raw.get("release")
    if not isinstance(release, str) or not _RELEASE_RE.match(release):
        logger.warning("feature-video manifest rejected: release %r", raw.get("release"))
        return None
    cdn_base = raw.get("cdn_base")
    if not isinstance(cdn_base, str) or not cdn_base.lower().startswith("https://"):
        logger.warning("feature-video manifest rejected: cdn_base is not https")
        return None
    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) > _MAX_ENTRIES:
        logger.warning("feature-video manifest rejected: entries is not a bounded list")
        return None
    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    # Basenames are cache PATHS (``<release>/<file>``), so two entries that share
    # one would download into the same file and the second would overwrite the
    # first — both sha256-verified, but one offer would then play the other's
    # bytes, and the size-only ``is_cached`` check cannot tell. Posters and clips
    # share the folder, so one namespace covers both. Case-folded because the
    # cache can sit on a case-insensitive filesystem.
    seen_names: set[str] = set()
    for item in raw_entries:
        entry = _parse_entry(item, doc_allowlist=TIP_DOC_ALLOWLIST)
        if entry is None or entry.id in seen:
            continue
        names = {entry.file.lower(), entry.poster.lower()}
        if len(names) < 2 or names & seen_names:
            logger.warning(
                "feature-video manifest entry %r dropped: its file or poster name is"
                " already used by another entry",
                entry.id,
            )
            continue
        seen.add(entry.id)
        seen_names |= names
        entries.append(entry)
    generated_at = raw.get("generated_at", "")
    return VideoManifest(
        release=release,
        cdn_base=cdn_base,
        generated_at=generated_at if isinstance(generated_at, str) else "",
        entries=tuple(entries),
    )


def verify_manifest(raw: object) -> bool:
    """Whether *raw* carries a signature valid for the pinned offline key.

    Blocking — it shells out to openssl, so async callers offload it.
    """
    if not isinstance(raw, dict):
        return False
    return verify_document_signature(raw, max_payload_bytes=_SIGNED_PAYLOAD_MAX_BYTES)


def verified_manifest(raw: object) -> "VideoManifest | None":
    """Verify then parse. The ONLY way a manifest becomes a usable object here.

    Verification first, in one place, so no caller can accidentally act on a
    document it only parsed. An unverified document is discarded WHOLE — with a
    warning, because a real signing regression and a tampered CDN look the same
    from here and both need to be visible.
    """
    if not verify_manifest(raw):
        logger.warning(
            "feature-video manifest signature did not verify; discarding the whole manifest"
        )
        return None
    return parse_manifest(raw)


# ── Fetching ──


def operator_manifest_url() -> str:
    """The operator's manifest url override from the environment, or ``""``.

    The ONLY override channel (see :data:`MANIFEST_URL_ENV` for why not config).
    Must be ``https://``; anything else is ignored with a warning rather than
    honoured, so the value cannot downgrade the fetch to plaintext or point it
    at a local file.
    """
    env_url = os.environ.get(MANIFEST_URL_ENV, "").strip()
    if not env_url:
        return ""
    if env_url.lower().startswith("https://"):
        return env_url
    logger.warning("%s must be an https:// url — ignoring the override", MANIFEST_URL_ENV)
    return ""


def manifest_url(release: str) -> str:
    """Resolve the manifest url for *release*: operator env override, else CDN default.

    An override names the manifest DIRECTLY (it is one document, not a base), so
    a mirror can serve a single manifest for a whole line of releases — as long
    as the ``release`` that document declares is one this build reads back
    (:func:`release_candidates`); :func:`fetch_manifest` refuses one that is not.
    """
    return operator_manifest_url() or f"{DEFAULT_CDN_BASE}/{release}/{MANIFEST_FILENAME}"


def _fetch_json(url: str, *, operator_url: bool = False) -> "dict | None":
    """GET *url* and parse a bounded JSON object. Never raises.

    Bounded BEFORE parsing: the response is unauthenticated until the signature
    check, so the only safe assumption about its size is the one we impose.

    *operator_url* says the url is the operator's own env override, whose mirror
    may redirect to another https host; a CDN url keeps the host pin.
    """
    try:
        request = urllib.request.Request(url, method="GET")
        # build_opener, never urlopen: the url was authorized against ONE host, and
        # urlopen's default handler would follow a cross-host redirect and spend
        # that authorization on a destination the CDN chose.
        opener = asset_downloader.build_opener(allow_cross_host_redirects=operator_url)
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- manifest_url enforces https://, redirects are pinned (host-pinned unless the operator's own env url), and the document is signature-verified before use
        with opener.open(request, timeout=_MANIFEST_TIMEOUT_SECS) as resp:
            body = resp.read(_MANIFEST_MAX_BYTES + 1)
        if len(body) > _MANIFEST_MAX_BYTES:
            logger.warning(
                "feature-video manifest at %s exceeds %d bytes; ignoring",
                asset_downloader.redact_url(url),
                _MANIFEST_MAX_BYTES,
            )
            return None
        data = json.loads(body.decode("utf-8"))
    except (
        urllib.error.URLError,
        OSError,
        TimeoutError,
        ValueError,
        RecursionError,
        http.client.HTTPException,
    ) as exc:
        # Two entries here are not the obvious ones. json.loads raises
        # RecursionError on a document nested past the interpreter's limit, and
        # urllib raises http.client.InvalidURL on a malformed url — a mistyped
        # https override reaches this line, not the network. Neither is an
        # OSError or a ValueError, so without them a hostile response or a typo
        # propagates out of a background task instead of reading as "no
        # manifest". HTTPException is the base class, so its siblings
        # (BadStatusLine, LineTooLong, IncompleteRead) come with it.
        # TYPE only, never `exc`. `http.client.InvalidURL` embeds the offending url
        # verbatim in its message ("nonnumeric port: 'secretpw@…'"), so logging the
        # exception hands the log ring and /api/logs the credential that
        # `redact_url` two lines up exists to keep out. The type is what aids
        # diagnosis anyway (timeout vs refused vs malformed), and it cannot carry
        # one. No `exc_info=True` for the same reason: a traceback renders the
        # exception's own `str()`, which would relocate the leak rather than close
        # it. Same rule as papyrus/tectonic.py's download path.
        logger.info(
            "could not fetch the feature-video manifest from %s: %s",
            asset_downloader.redact_url(url),
            type(exc).__name__,
        )
        return None
    return data if isinstance(data, dict) else None


def store_manifest(manifest: VideoManifest, raw: dict) -> None:
    """Cache the verified manifest document beside the clips it describes.

    The RAW document is stored, signature included, so the cached copy can be
    re-verified on the next read rather than trusted because we once trusted it.
    Written through :func:`pinned_release_dir`, like every other write under the
    cache.
    """
    try:
        with pinned_release_dir(manifest.release) as folder:
            folder.write_text(MANIFEST_FILENAME, json.dumps(raw, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError):
        logger.warning("could not cache the feature-video manifest", exc_info=True)


def load_cached_manifest(version: str) -> "VideoManifest | None":
    """The newest cached manifest eligible for *version*, re-verified. Blocking.

    Re-verified on every read, not trusted because it was verified when written.
    The file sits in the user's data home where an operator (or anything running
    as them) can edit it, and what it states includes ``cdn_base`` — the one host
    the download pass is allowed to reach. A cache that could be edited into a
    trust decision would make the signature pointless.

    Read the way the write side writes: through a pinned parent, never following
    a link at the file's name, and never more than :data:`_MANIFEST_MAX_BYTES`
    (:func:`kiro_crew.pinned_fs.read_file_pinned`). The network fetch already
    caps the document at that size, so a cached copy larger than it was not
    written by this module; a link planted at the name — to ``/dev/zero``, say —
    would otherwise be read to exhaustion on a polled route.
    """
    for release in release_candidates(version):
        try:
            path = cached_manifest_path(release)
            text = pinned_fs.read_file_pinned(
                path,
                what="cached feature-video manifest",
                max_bytes=_MANIFEST_MAX_BYTES,
                refusal=CacheDirRefused,
            )
            raw = json.loads(text)
        except (OSError, ValueError, RecursionError):
            continue
        manifest = verified_manifest(raw)
        if manifest is not None:
            return manifest
    return None


def fetch_manifest(version: str) -> "tuple[VideoManifest | None, dict]":
    """Fetch, verify and parse the manifest for *version*. Blocking.

    Returns ``(manifest, raw_document)`` so the caller can cache the exact bytes
    that verified. Walks :func:`release_candidates` and stops at the first
    release whose manifest verifies AND whose ``release`` field matches the
    folder it was served from — a manifest that disagrees with its own location
    is refused, so a stale or misfiled document cannot silently redirect one
    release's clips into another release's folder.

    An OVERRIDE url (:func:`operator_manifest_url`) names one document for every
    candidate, so it is fetched once, and its ``release`` need not equal the candidate being
    walked — but it must be one of the candidates. That is the rule that keeps
    :func:`store_manifest` and :func:`load_cached_manifest` agreeing: the store
    files the document under the release it declares, and the loader reads only
    the candidate folders, so a document declaring a release outside that window
    would be cached and then never found again — the mirror install it serves
    would show no clips on an offline restart despite the bytes being on disk.
    Refusing it here, with the reason logged, is the honest failure.

    Every request takes its own audited answer from :func:`download_denied`,
    including the first: the walk can make up to :data:`_FALLBACK_MINORS` + 2
    requests (the exact release, its minor line's ``.0`` when the patch is not 0,
    then the earlier minors — five for ``0.6.3``), and a withdrawal landing
    between two of them must stop the next
    one rather than ride the permit the first was made on — the same rule the
    download pass applies between a poster and its clip. The caller's own check
    before scheduling the pass still stands; it is what keeps a denied install
    from starting the task at all. A denial mid-walk ends the walk: nothing
    fetched so far verified, and the caller falls back to the on-disk copy.
    """
    candidates = release_candidates(version)
    overridden = bool(operator_manifest_url())
    tried: set[str] = set()
    for release in candidates:
        url = manifest_url(release)
        if url in tried:
            continue
        tried.add(url)
        if download_denied():
            logger.info("feature-video manifest fetch stopped: downloading withdrawn mid-walk")
            return None, {}
        raw = _fetch_json(url, operator_url=overridden)
        if raw is None:
            continue
        manifest = verified_manifest(raw)
        if manifest is None:
            continue
        acceptable = manifest.release in candidates if overridden else manifest.release == release
        if not acceptable:
            logger.warning(
                "feature-video manifest at %s declares release %r; refusing the mismatch"
                " (this build reads back %s)",
                asset_downloader.redact_url(url),
                manifest.release,
                ", ".join(candidates),
            )
            continue
        return manifest, raw
    return None, {}


__all__ = [
    "AUDIT_TOOL",
    "CacheDirRefused",
    "DEFAULT_CDN_BASE",
    "DOWNLOAD_SCOPE",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA",
    "MANIFEST_URL_ENV",
    "ManifestEntry",
    "VideoManifest",
    "cache_root",
    "cached_manifest_path",
    "checked_cache_root",
    "download_denied",
    "download_permitted_cached",
    "ensure_cache_dir",
    "fetch_manifest",
    "is_safe_media_name",
    "load_cached_manifest",
    "manifest_url",
    "operator_manifest_url",
    "parse_manifest",
    "release_candidates",
    "release_dir",
    "running_release",
    "served_content_type",
    "store_manifest",
    "verified_manifest",
    "verify_manifest",
]
