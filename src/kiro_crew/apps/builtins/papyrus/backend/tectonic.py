"""Papyrus — the managed, digest-pinned Tectonic compiler install.

A stock machine has no LaTeX compiler, so ``pip install kirocrew`` shipped this
app broken by default: every compile answered *"No LaTeX compiler found. Install
TeX Live (pdflatex) or tectonic."* Telling a first-run user to install TeX Live
is a multi-gigabyte errand, and there is no LaTeX compiler on PyPI (the
``tectonic`` name there is an unrelated placeholder — version ``0.0.0dev``, no
files). Tectonic itself is the answer: ONE self-contained static binary of
10-22MB that downloads only the TeX support files a document actually needs, and
that drives its own bibtex/rerun cycle — which :mod:`.latex` already knows about
(see its ``is_tectonic`` branches).

This module fetches that binary **on demand**, verifies it against a
per-platform sha256 pin, and installs it under the app's own data dir. It
follows :mod:`kiro_crew.embeddings`, the in-tree precedent for exactly this
problem: a large per-platform artifact fetched over plain HTTPS, sha256-pinned,
installed persistently under the data home, downloaded off the event loop with
retries, behind a status surface the UI can poll.

Why this is NOT the system-package install that ``pptx-maker`` deliberately
refuses
------------------------------------------------------------------------------
``docs/system-specs/modules/pptx-maker.md`` records a product decision worth
restating here, because a reader could otherwise mistake this module for the
same mistake: that app's upstream shelled out to ``brew``/``apt-get`` from a
browser request, and *installing a system package is a host-level action a web
request must not take*. A test there pins the absence of ``POST /deps/install``.
That decision stands. This is a different act, and the differences are the
reason it is allowed:

* **No package manager and no privilege.** Nothing is elevated, no ``sudo``, no
  system installer is invoked. One archive is unpacked; nothing inside it is
  executed at install time.
* **Nothing is written outside this app's own data dir.** The binary lands in
  ``<data home>/apps/papyrus/data/vendor/tectonic/`` — never a system prefix,
  never ``/usr/local``, never a shell profile, and ``PATH`` is not mutated. The
  compiler is invoked by absolute path.
* **The bytes are pinned.** A sha256 per platform is verified before anything is
  installed, so the operator receives exactly the artifact this source names, or
  nothing.
* **It is reversible by deleting one directory** this app owns.

A user's own compiler always wins: :func:`.latex.find_compiler_sync` probes
``PATH`` and the userspace TeX Live locations BEFORE this managed install, so
provisioning can never displace a real TeX distribution.

Everything here is synchronous and blocking (network + filesystem). Callers MUST
stay off the event loop — see :func:`provision_in_background`, which runs the
work on a daemon thread, and the ``no-blocking-call-on-event-loop`` rule.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import os
import platform
import shutil
import ssl
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.papyrus.backend import store
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.papyrus")

# ── release pins ────────────────────────────────────────────────────────────

#: The upstream release these digests were computed from.
#:
#: The **digest — not this tag — is the trust anchor.** A re-tagged, rebuilt or
#: replaced asset fails verification and is discarded, so the tag is only a
#: locator. Consequently, bumping Tectonic means recomputing EVERY digest in
#: :data:`_ASSETS`; bumping the tag alone would simply break every platform at
#: once, which is the intended failure mode.
#:
#: Digests were produced by downloading each asset from this release and hashing
#: the archive as published::
#:
#:     BASE=https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.17.0
#:     curl -sL -o "$ASSET" "$BASE/$ASSET" && shasum -a 256 "$ASSET"
#:
#: Asset sizes were cross-checked against the GitHub release API and one asset
#: was re-downloaded to confirm the digest reproduces byte-for-byte.
TECTONIC_RELEASE_TAG = "tectonic@0.17.0"

#: Human-readable compiler version, for the UI and the audit trail. Derived from
#: the tag rather than parsed from the binary: reading it back would mean running
#: an unverified download to find out what it is.
TECTONIC_VERSION = "0.17.0"

_RELEASE_URL_TEMPLATE = (
    "https://github.com/tectonic-typesetting/tectonic/releases/download/{tag}/{asset}"
)

_ARCH_X86_64 = "x86_64"
_ARCH_AARCH64 = "aarch64"

_PLATFORM_DARWIN = "darwin"
_PLATFORM_LINUX = "linux"
_PLATFORM_WINDOWS = "win32"

_ZIP_SUFFIX = ".zip"


@dataclass(frozen=True)
class TectonicAsset:
    """One platform's release artifact: the published file name and its sha256."""

    name: str
    sha256: str

    @property
    def is_zip(self) -> bool:
        """True when the artifact is a ``.zip`` (Windows) rather than a ``.tar.gz``."""
        return self.name.endswith(_ZIP_SUFFIX)

    @property
    def url(self) -> str:
        """The download URL. The tag carries an ``@``, so it is percent-encoded."""
        return _RELEASE_URL_TEMPLATE.format(
            tag=urllib.parse.quote(TECTONIC_RELEASE_TAG, safe=""), asset=self.name
        )


#: Platform/arch → pinned artifact. Linux uses the **musl** builds on purpose:
#: they are statically linked, so one artifact works across distributions with no
#: glibc-version floor to trip over on an older LTS or a Graviton image.
#:
#: Windows-on-ARM is deliberately absent: the ``tectonic@0.17.0`` release
#: publishes no ``aarch64-pc-windows`` asset, so that host reports "unsupported"
#: and keeps the manual install path (see :func:`current_asset`). Adding it later
#: is one row plus its real digest.
_ASSETS: dict[tuple[str, str], TectonicAsset] = {
    (_PLATFORM_DARWIN, _ARCH_AARCH64): TectonicAsset(
        "tectonic-0.17.0-aarch64-apple-darwin.tar.gz",
        "a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667",
    ),
    (_PLATFORM_DARWIN, _ARCH_X86_64): TectonicAsset(
        "tectonic-0.17.0-x86_64-apple-darwin.tar.gz",
        "7c90ef5b6ddb1eb1937e4337add5237b79338e4b9676459fa91187d24d6cdf80",
    ),
    (_PLATFORM_LINUX, _ARCH_X86_64): TectonicAsset(
        "tectonic-0.17.0-x86_64-unknown-linux-musl.tar.gz",
        "8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7",
    ),
    (_PLATFORM_LINUX, _ARCH_AARCH64): TectonicAsset(
        "tectonic-0.17.0-aarch64-unknown-linux-musl.tar.gz",
        "b10954a95404f3ab2328d2fa59a5ebab8e657f893fab096f98be8db7c0c979b8",
    ),
    (_PLATFORM_WINDOWS, _ARCH_X86_64): TectonicAsset(
        "tectonic-0.17.0-x86_64-pc-windows-msvc.zip",
        "f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f",
    ),
}

# ── install layout ──────────────────────────────────────────────────────────

#: Where the managed binary lives, relative to the app's data dir. ``vendor/``
#: mirrors ``pptx_maker``'s ``data/vendor/`` convention for a runtime-provisioned
#: third-party artifact (a builtin app's source is read-only inside the installed
#: Python package, so anything provisioned at runtime lands in the data dir).
_VENDOR_DIRNAME = "vendor"
_TECTONIC_DIRNAME = "tectonic"

_BINARY_NAME_POSIX = "tectonic"
_BINARY_NAME_WINDOWS = "tectonic.exe"

#: Mode applied to the installed binary: owner rwx, group/other rx. It is a
#: public upstream release, not a secret, but it must not be group-writable.
_BINARY_MODE = 0o755

#: Floor for a plausible Tectonic binary. The real ones are 26-54MB unpacked;
#: anything under this is a stub or a truncated extraction, not a compiler.
_MIN_BINARY_BYTES = 5_000_000

#: Ceiling on the bytes any single archive member may expand to. The pinned
#: artifacts unpack to ~54MB; this bounds a decompression bomb even though the
#: digest pin already means the archive can only be the one we named.
_MAX_MEMBER_BYTES = 256 * 1024 * 1024

# ── download tuning ─────────────────────────────────────────────────────────

#: Escape hatch for tests and CI: a test run must never reach the network. Set
#: to ``"1"`` to make provisioning refuse immediately.
SKIP_DOWNLOAD_ENV = "KIROCREW_PAPYRUS_SKIP_TECTONIC_DOWNLOAD"

#: Override the download URL for a mirrored or air-gapped deployment. Must be
#: ``https://``; the platform's pinned sha256 still has to match, so an override
#: can only change WHERE the bytes come from, never WHICH bytes are accepted.
TECTONIC_URL_ENV = "KIROCREW_PAPYRUS_TECTONIC_URL"

#: 22MB at >=40KB/s. A slower link fails and is retried with backoff.
_HTTP_TIMEOUT_SECS = 600
_HTTP_CHUNK_BYTES = 1 << 18
#: Progress is republished about every 2MB so the UI can render a determinate bar
#: without the status dict being rewritten on every 256KB chunk.
_PROGRESS_EVERY_BYTES = 2 << 20

_DOWNLOAD_MAX_ATTEMPTS = 3
_DOWNLOAD_BACKOFF_BASE_SECS = 5.0
_DOWNLOAD_BACKOFF_CAP_SECS = 60.0

#: Provisioning job states, mirroring ``pptx_maker``'s ``ProvisionState``.
STATE_IDLE = "idle"
STATE_DOWNLOADING = "downloading"
STATE_VERIFYING = "verifying"
STATE_INSTALLING = "installing"
STATE_DONE = "done"
STATE_ERROR = "error"

_HTTPS_SCHEME = "https"

_SSL_CA_PATHS = (
    "/etc/pki/tls/certs/ca-bundle.crt",  # AL2, RHEL, CentOS
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/ssl/cert.pem",  # macOS, Alpine
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # Fedora
)


class UnsupportedPlatform(Exception):
    """No pinned Tectonic artifact exists for this platform/architecture."""


# ── platform resolution ─────────────────────────────────────────────────────


def _arch_key(machine: str) -> str | None:
    """Normalize a :func:`platform.machine` value to an asset architecture key.

    Handles the two naming splits that matter: ``arm64`` (macOS, and Windows)
    versus ``aarch64`` (Linux), and ``AMD64`` (Windows) versus ``x86_64``.
    """
    normalized = (machine or "").strip().lower()
    if normalized in ("x86_64", "amd64", "x64"):
        return _ARCH_X86_64
    if normalized in ("aarch64", "arm64"):
        return _ARCH_AARCH64
    return None


def _platform_key(platform_name: str) -> str | None:
    """Normalize a ``sys.platform`` value to an asset platform key."""
    if platform_name.startswith(_PLATFORM_LINUX):
        return _PLATFORM_LINUX
    if platform_name == _PLATFORM_DARWIN:
        return _PLATFORM_DARWIN
    if platform_name == _PLATFORM_WINDOWS:
        return _PLATFORM_WINDOWS
    return None


def current_asset() -> TectonicAsset | None:
    """The pinned artifact for this host, or ``None`` when unsupported.

    ``None`` is a normal, reportable outcome — a 32-bit Linux box, a BSD, or
    Windows-on-ARM. The caller degrades to the manual "install TeX Live or
    tectonic yourself" path rather than failing.
    """
    platform_key = _platform_key(sys.platform)
    arch_key = _arch_key(platform.machine())
    if platform_key is None or arch_key is None:
        return None
    return _ASSETS.get((platform_key, arch_key))


def platform_supported() -> bool:
    """True when this host has a pinned artifact available."""
    return current_asset() is not None


# ── install paths ───────────────────────────────────────────────────────────


def vendor_dir(root: Path | None = None) -> Path:
    """Directory holding the managed install (NOT created as a side effect)."""
    return store.data_dir(root) / _VENDOR_DIRNAME / _TECTONIC_DIRNAME


def binary_name() -> str:
    """Executable file name for this platform."""
    return _BINARY_NAME_WINDOWS if platform_compat.IS_WINDOWS else _BINARY_NAME_POSIX


def binary_path(root: Path | None = None) -> Path:
    """Absolute path the managed compiler is installed to."""
    return vendor_dir(root) / binary_name()


def binary_installed(root: Path | None = None) -> bool:
    """True when a plausible managed binary is present and executable.

    Size-gated as well as existence-gated so a truncated or half-written file
    can never read as a usable compiler. ``X_OK`` is not consulted on Windows,
    where it is not meaningful.
    """
    target = binary_path(root)
    try:
        if not target.is_file() or target.stat().st_size < _MIN_BINARY_BYTES:
            return False
    except OSError:
        return False
    return platform_compat.IS_WINDOWS or os.access(target, os.X_OK)


# ── job state ───────────────────────────────────────────────────────────────


@dataclass
class ProvisionState:
    """Progress of the (single) background provisioning job.

    Read from the event loop by ``GET /health`` and written from the download
    thread; every critical section is a field assignment under the module lock,
    so the reader never sees a half-updated record.
    """

    state: str = STATE_IDLE
    error: str = ""
    attempt: int = 0
    bytes_downloaded: int = 0
    bytes_total: int = 0
    started: float = 0.0

    def elapsed(self) -> int:
        return int(time.time() - self.started) if self.started else 0

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "error": self.error,
            "attempt": self.attempt,
            "bytes_downloaded": self.bytes_downloaded,
            "bytes_total": self.bytes_total,
            "elapsed": self.elapsed(),
        }


_state = ProvisionState()
_state_lock = threading.Lock()
#: Guards against two concurrent downloads of the same 22MB artifact.
_job_thread: threading.Thread | None = None


def provision_state() -> dict[str, object]:
    """A snapshot of the job state, safe to serialize from the event loop."""
    with _state_lock:
        return _state.to_dict()


def reset_provision_state() -> None:
    """Forget the job state (tests, and ``KIROCREW_HOME`` changes)."""
    global _job_thread
    with _state_lock:
        _state.state = STATE_IDLE
        _state.error = ""
        _state.attempt = 0
        _state.bytes_downloaded = 0
        _state.bytes_total = 0
        _state.started = 0.0
        _job_thread = None


def _set_state(state: str, *, error: str = "", attempt: int | None = None) -> None:
    with _state_lock:
        _state.state = state
        _state.error = error
        if attempt is not None:
            _state.attempt = attempt


def _set_progress(downloaded: int, total: int) -> None:
    with _state_lock:
        _state.bytes_downloaded = downloaded
        _state.bytes_total = total


def _audit(outcome: str, *, error: str = "") -> None:
    """SEL event for every provisioning attempt. Fire-and-forget."""
    sel().log_api_access(
        caller="core:papyrus",
        operation="papyrus.tectonic_provision",
        outcome=outcome,
        source="builtin-app",
        resources=TECTONIC_RELEASE_TAG[:200],
        error=error[:200] if error else "",
    )


# ── safe extraction ─────────────────────────────────────────────────────────


class ArchiveRejected(Exception):
    """An archive member was refused as unsafe to extract."""


def _reject_member_name(name: str) -> str | None:
    """Return a rejection reason for an unsafe member *name*, else ``None``.

    ``tarfile.extractall`` / ``ZipFile.extractall`` write wherever a member name
    points, which makes a downloaded archive a path-traversal sink. Refused
    here, before anything is written:

    * an absolute POSIX path (``/etc/cron.d/x``) or a Windows/UNC one
      (``C:\\...``, ``\\\\host\\share``) — a backslash IS a separator on Windows,
      so it is rejected outright rather than only checked as ``/``;
    * any ``..`` segment, anywhere, which is how a relative name escapes;
    * a NUL byte, which truncates the path at the syscall boundary;
    * an empty name.

    Mirrors :func:`kiro_crew.apps.builtins.papyrus.backend.store.safe_child`'s
    rules, applied to archive members rather than request paths.
    """
    if not name:
        return "empty member name"
    if "\0" in name or "\\" in name:
        return f"unsafe member name: {name[:80]!r}"
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return f"absolute member path: {name[:80]!r}"
    # ``..`` in ANY position is the escape vector and is refused outright. ``.``
    # and empty components are deliberately NOT refused: they are path no-ops, and
    # a leading ``./`` is a completely standard tar convention — rejecting it would
    # break a future release for no security gain.
    if any(part == ".." for part in name.split("/")):
        return f"traversal in member path: {name[:80]!r}"
    return None


def _tar_data_filter(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo:
    """``tarfile`` extraction filter: name-checked, regular files and dirs only.

    Used as the ``filter=`` callable on Python 3.12+ (and 3.11.4+), and applied
    by hand on the older leg — ``filter="data"`` does not exist on Python 3.10,
    which this project still supports, so the check cannot rely on it. Raises
    :class:`ArchiveRejected` rather than returning ``None`` so a hostile archive
    is a loud failure instead of a silently short extraction.

    Beyond the name rules, everything that is not a regular file or a directory
    is refused: a symlink or hardlink member can point out of the destination
    even when its own name looks innocent, and device/FIFO members have no
    business in a compiler tarball. Ownership and permission bits are dropped
    (the caller applies :data:`_BINARY_MODE` itself), matching what stdlib's
    ``data`` filter does.
    """
    del dest  # containment comes from the name rules, not from the destination
    reason = _reject_member_name(member.name)
    if reason is not None:
        raise ArchiveRejected(reason)
    if not (member.isreg() or member.isdir()):
        raise ArchiveRejected(f"unsupported member type in archive: {member.name[:80]!r}")
    if member.size > _MAX_MEMBER_BYTES:
        raise ArchiveRejected(f"archive member too large: {member.size} bytes")
    scrubbed = member.replace(deep=True) if hasattr(member, "replace") else member
    scrubbed.uid = 0
    scrubbed.gid = 0
    scrubbed.uname = ""
    scrubbed.gname = ""
    scrubbed.mode = _BINARY_MODE if scrubbed.isreg() else 0o755
    return scrubbed


def _extract_tar(archive: Path, dest: Path) -> None:
    """Extract a ``.tar.gz`` into *dest* with every member validated.

    Prefers stdlib's own filtering hook so the check runs INSIDE ``extractall``
    (no TOCTOU gap between validating a member list and writing it). On Python
    3.10, where the ``filter`` keyword does not exist, the same callable is
    applied to every member first and the extraction is then restricted to the
    validated list — the ``TypeError`` fallback shape ``snapshot.py`` already
    uses for the same reason.
    """
    with tarfile.open(str(archive), "r:gz") as tar:
        try:
            tar.extractall(str(dest), filter=_tar_data_filter)  # type: ignore[arg-type]
        except TypeError:
            members = [_tar_data_filter(m, str(dest)) for m in tar.getmembers()]
            tar.extractall(str(dest), members=members)


def _extract_zip(archive: Path, dest: Path) -> None:
    """Extract a ``.zip`` into *dest* with every member validated.

    ``ZipFile`` has no ``filter`` hook at all, so validation is explicit: every
    entry's name is checked before ANY entry is written, and only then is each
    one extracted. Zip has no symlink member type in the portable subset, but a
    Unix-mode symlink can be smuggled in the external attributes, so entries
    whose stored mode is not a regular file or directory are refused too.
    """
    with zipfile.ZipFile(str(archive)) as zf:
        infos = zf.infolist()
        for info in infos:
            # BOTH names are checked, and this is load-bearing on Windows.
            # `ZipInfo.__init__` rewrites `\` to `/` when `os.sep` is `\`, so on
            # Windows `info.filename` NEVER contains a backslash and the
            # backslash rule below could only ever fire on POSIX — the one
            # platform where a backslash is not a separator. That leaves the
            # `..`-traversal rule silently dependent on a stdlib normalization
            # detail. `orig_filename` is the name as the archive actually spells
            # it, so checking it too makes the guard self-sufficient: a
            # backslash-flavoured traversal is refused on every platform on its
            # own merits, not because zipfile happened to rewrite it first.
            for candidate in (info.orig_filename, info.filename):
                reason = _reject_member_name(candidate)
                if reason is not None:
                    raise ArchiveRejected(reason)
            if info.file_size > _MAX_MEMBER_BYTES:
                raise ArchiveRejected(f"archive member too large: {info.file_size} bytes")
            # Upper 16 bits of external_attr are the Unix mode when the archive
            # was produced on a Unix host; S_IFLNK/S_IFIFO there must not extract.
            unix_mode = info.external_attr >> 16
            if unix_mode and not (unix_mode & 0o170000) in (0o100000, 0o040000, 0):
                raise ArchiveRejected(f"unsupported member type in archive: {info.filename[:80]!r}")
        for info in infos:
            zf.extract(info, str(dest))


def _extract(archive: Path, dest: Path, *, is_zip: bool) -> None:
    """Extract *archive* into *dest*, refusing anything unsafe."""
    if is_zip:
        _extract_zip(archive, dest)
    else:
        _extract_tar(archive, dest)


def _locate_binary(tree: Path) -> Path | None:
    """Find the Tectonic executable inside an extracted tree.

    The pinned artifacts are flat — one member, ``tectonic`` or ``tectonic.exe``
    — but the search is a bounded walk anyway so a future release that adds a
    top-level directory does not need a code change to keep working.
    """
    wanted = binary_name()
    direct = tree / wanted
    if direct.is_file():
        return direct
    for candidate in sorted(tree.rglob(wanted)):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


# ── download ────────────────────────────────────────────────────────────────


def _make_ssl_context() -> ssl.SSLContext:
    """An SSL context that finds system CA certs on every supported platform.

    Same shape and reason as :func:`kiro_crew.embeddings._make_ssl_context`: a
    bundled Python runtime may not ship a CA bundle, and OpenSSL's compiled-in
    path can miss on a cross-built interpreter.
    """
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
        if ctx.cert_store_stats()["x509_ca"] > 0:
            return ctx
    except ssl.SSLError:
        pass
    for path in _SSL_CA_PATHS:
        if os.path.isfile(path):
            ctx.load_verify_locations(cafile=path)
            return ctx
    return ctx


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that leaves ``https``.

    The release URL 302s to a CDN host, which is normal and expected. A redirect
    to ``http://`` would be a silent transport downgrade. The sha256 pin means
    such a hop could not change WHICH bytes are accepted, so this is defence in
    depth rather than the integrity control — but a plaintext fetch of a 22MB
    binary is worth refusing on its own.
    """

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        if urllib.parse.urlsplit(newurl).scheme.lower() != _HTTPS_SCHEME:
            raise urllib.error.URLError(f"refusing non-https redirect to {redact_url(newurl)}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


def redact_url(url: str) -> str:
    """Return *url* safe for logs: scheme + host only.

    A mirror override can carry a credential in ANY of three places — userinfo, a signed
    query string, or a PATH SEGMENT (`/artifactory/api/npm/tok-9f3b2c/…`, a
    presigned-style `/AKIA…/…`). The first two were dropped and the path was kept, so a
    path-tokenised mirror had its token written to the log and shown on the dashboard.

    The path is now dropped too. It cost the one diagnostic it carried — the artifact's
    filename — and that is already known from :data:`_ASSETS`, so nothing is lost that
    is not recoverable from the code. Host-only is also what makes this safe by
    CONSTRUCTION rather than by enumerating token shapes: there is no fourth place left
    for a secret to hide.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, host, "", "", ""))
    except ValueError:
        return "<unparseable-url>"


def resolve_url(asset: TectonicAsset) -> str:
    """Resolve the download URL: env override, else the pinned release URL.

    The override exists for mirrored/air-gapped deployments and must be
    ``https://`` — other schemes (``file://``, ``http://``) are refused so an
    operator-supplied value cannot read local files or fetch plaintext. Whatever
    the source, the payload is only trusted after its streaming sha256 matches
    the platform's pin, so an override changes where bytes come from and never
    which bytes are accepted.
    """
    override = os.environ.get(TECTONIC_URL_ENV, "").strip()
    if override:
        if override.lower().startswith(f"{_HTTPS_SCHEME}://"):
            return override
        logger.warning(
            "papyrus: %s must be an https:// URL — ignoring the override", TECTONIC_URL_ENV
        )
    return asset.url


def _download_to(asset: TectonicAsset, staging: Path) -> tuple[bool, str]:
    """Stream the artifact to *staging*, hashing as it goes. Verify before install.

    Returns ``(ok, error)``. The digest is computed over the stream rather than
    by re-reading the file, so a file swapped between write and check cannot be
    what gets verified.
    """
    url = resolve_url(asset)
    # The SSL context rides on an HTTPSHandler, not on `open()`: OpenerDirector.open
    # takes no `context=` (only the module-level urlopen does), so passing one there
    # would be silently dropped along with the CA-discovery fallback it carries.
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_make_ssl_context()), _HttpsOnlyRedirectHandler
    )
    request = urllib.request.Request(url, method="GET")
    digest = hashlib.sha256()
    downloaded = 0
    try:
        logger.info("papyrus: downloading Tectonic from %s", redact_url(url))
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- resolve_url enforces https:// and the payload is sha256-pinned
        with opener.open(request, timeout=_HTTP_TIMEOUT_SECS) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            _set_progress(0, total)
            with staging.open("wb") as out:
                while True:
                    chunk = resp.read(_HTTP_CHUNK_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if downloaded % _PROGRESS_EVERY_BYTES < _HTTP_CHUNK_BYTES:
                        _set_progress(downloaded, total)
        _set_progress(downloaded, downloaded)
    # `http.client.HTTPException` is in the tuple deliberately: it is NOT an
    # `OSError`, a `URLError` or a `ValueError`, so `InvalidURL` (a malformed mirror
    # override — the credentialed case) used to pass through this handler entirely
    # and be reported by the outer catch-all as "provisioning crashed". Handling it
    # here turns an unexplained crash into the accurate "download failed", and keeps
    # the redaction and the retry loop that go with it.
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        TimeoutError,
        ValueError,
    ) as exc:
        # The exception TEXT is not safe to return. Several of these carry the URL
        # verbatim — `InvalidURL` and `HTTPError` both do — and a mirror override may
        # put credentials in userinfo or a signed query string, which is precisely why
        # `redact_url` exists and why the log line above already uses it. This return
        # value goes further than a log: it lands in the persisted install state and
        # is rendered on the dashboard.
        #
        # So the message is the exception's TYPE plus a redacted URL, never `{exc}`.
        # The type is what actually aids diagnosis here (timeout vs. refused vs. DNS),
        # and it cannot contain a credential.
        # No `exc_info=True`: a traceback renders the exception's own `str()`, which is
        # the very text being kept out of the message. The log is a sink too, so
        # redacting the return value while dumping the traceback beside it would
        # relocate the leak rather than close it.
        logger.warning(
            "papyrus: Tectonic download from %s failed: %s",
            redact_url(url),
            type(exc).__name__,
        )
        return False, f"download failed ({type(exc).__name__}) from {redact_url(url)}"
    _set_state(STATE_VERIFYING)
    actual = digest.hexdigest()
    if actual != asset.sha256:
        return False, (
            f"sha256 mismatch for {asset.name}: got {actual[:16]}…, "
            f"expected {asset.sha256[:16]}… — the download was refused"
        )
    return True, ""


def _install_binary(extracted: Path, target: Path) -> None:
    """Move *extracted* into place atomically and make it executable.

    Staged inside the TARGET directory so ``os.replace`` is a same-filesystem
    rename, and named per-process so two concurrent installs cannot interleave
    writes into one staging file. The mode is applied BEFORE the rename, so the
    binary is never observable at its final path without its exec bit — a
    half-installed compiler that "looks valid" is exactly the state
    :func:`binary_installed` must never see.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.{os.getpid()}.tmp"
    try:
        shutil.move(str(extracted), str(staging))
        platform_compat.chmod_safe(staging, _BINARY_MODE)
        os.replace(staging, target)
    finally:
        if staging.exists():
            staging.unlink(missing_ok=True)


def _provision_once(root: Path | None) -> tuple[bool, str]:
    """One download → verify → extract → install cycle. Returns ``(ok, error)``."""
    asset = current_asset()
    if asset is None:
        return False, (
            f"no pinned Tectonic build for {sys.platform}/"
            f"{platform.machine()} — install TeX Live (pdflatex) "
            "or tectonic manually"
        )
    target = binary_path(root)
    work = target.parent / f".provision.{os.getpid()}"
    archive = work / asset.name
    try:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        ok, error = _download_to(asset, archive)
        if not ok:
            return False, error
        _set_state(STATE_INSTALLING)
        unpacked = work / "unpacked"
        unpacked.mkdir()
        try:
            _extract(archive, unpacked, is_zip=asset.is_zip)
        except (ArchiveRejected, tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
            return False, f"archive rejected: {exc}"
        binary = _locate_binary(unpacked)
        if binary is None:
            return False, f"no {binary_name()} found inside {asset.name}"
        if binary.stat().st_size < _MIN_BINARY_BYTES:
            return False, f"extracted {binary_name()} is implausibly small ({binary.stat().st_size} bytes)"
        _install_binary(binary, target)
        if not binary_installed(root):
            return False, "installed binary failed its post-install check"
        return True, ""
    except OSError as exc:
        return False, f"install failed: {exc}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def provision_sync(root: Path | None = None, attempts: int = _DOWNLOAD_MAX_ATTEMPTS) -> bool:
    """Install the managed compiler. BLOCKING — never call on the event loop.

    Idempotent: returns ``True`` immediately when a managed binary is already
    installed. Retries with capped exponential backoff so one flaky hop does not
    force the user to click again. Refused outright when
    :data:`SKIP_DOWNLOAD_ENV` is set, so a test run can never reach the network.
    """
    if binary_installed(root):
        _set_state(STATE_DONE)
        return True
    if os.environ.get(SKIP_DOWNLOAD_ENV) == "1":
        _set_state(STATE_ERROR, error=f"{SKIP_DOWNLOAD_ENV}=1 — download skipped")
        return False
    last_error = "provisioning failed"
    for attempt in range(1, max(1, attempts) + 1):
        _set_state(STATE_DOWNLOADING, attempt=attempt)
        ok, error = _provision_once(root)
        if ok:
            _set_state(STATE_DONE, attempt=attempt)
            logger.info("papyrus: installed managed Tectonic %s", TECTONIC_VERSION)
            _audit("ok")
            return True
        last_error = error
        logger.warning(
            "papyrus: Tectonic provisioning attempt %d/%d failed: %s",
            attempt,
            max(1, attempts),
            error,
        )
        if attempt < attempts:
            time.sleep(
                min(
                    _DOWNLOAD_BACKOFF_BASE_SECS * (2 ** (attempt - 1)),
                    _DOWNLOAD_BACKOFF_CAP_SECS,
                )
            )
    _set_state(STATE_ERROR, error=last_error, attempt=max(1, attempts))
    _audit("failed", error=last_error)
    return False


def provision_in_background(root: Path | None = None) -> bool:
    """Start provisioning on a daemon thread. Returns False if one is already running.

    A DAEMON thread rather than ``asyncio.to_thread`` or the shared executor: a
    22MB transfer must never pin interpreter exit, and both the loop's default
    executor and a ``ThreadPoolExecutor`` join their non-daemon workers at exit,
    so an in-flight download would hang a Ctrl-C for up to the HTTP timeout. Same
    reasoning as :func:`kiro_crew.embeddings._run_download_on_daemon_thread`. The
    default executor is also what the loop uses for DNS, so a long occupant there
    would stall unrelated gateway work.

    The request that starts this returns 202 immediately; the UI polls
    ``GET /health`` for the outcome.
    """
    global _job_thread
    with _state_lock:
        if _job_thread is not None and _job_thread.is_alive():
            return False
        _state.state = STATE_DOWNLOADING
        _state.error = ""
        _state.attempt = 0
        _state.bytes_downloaded = 0
        _state.bytes_total = 0
        _state.started = time.time()
        thread = threading.Thread(
            target=_provision_job, args=(root,), name="kc-papyrus-tectonic", daemon=True
        )
        _job_thread = thread
    _audit("started")
    thread.start()
    return True


def _provision_job(root: Path | None) -> None:
    """Daemon-thread body. Must not raise — a background job cannot vanish silently."""
    try:
        installed = provision_sync(root)
    except Exception as exc:  # noqa: BLE001 - a background job must report, not crash
        logger.warning("papyrus: Tectonic provisioning crashed", exc_info=True)
        # TYPE only, not `{exc}`. This is the catch-all, so the URL-bearing
        # exceptions the download path deliberately redacts arrive here whenever they
        # are not one of the four types it handles — and `http.client.InvalidURL` is
        # exactly that case: it derives from `HTTPException`, not from `OSError`,
        # `URLError` or `ValueError`, so it passes straight through `_download_to`'s
        # handler and `_install_binary`'s `except OSError` to land here.
        #
        # Its message embeds the offending URL verbatim ("nonnumeric port:
        # 'secretpw@…'"), so a credentialed mirror override put the credential into
        # the persisted install state, the dashboard, AND the SEL audit record. Two
        # sinks below, both of which needed it.
        _set_state(STATE_ERROR, error=f"provisioning crashed ({type(exc).__name__})")
        _audit("failed", error=type(exc).__name__)
        return
    if installed:
        # The negative "no compiler" answer is cached process-wide, so it must be
        # dropped or the very next compile still reports no compiler.
        #
        # circular import: `latex` imports this module at module scope (it probes
        # the managed install as one of its compiler candidates), so importing it
        # at top level here would not resolve.
        from kiro_crew.apps.builtins.papyrus.backend import latex

        latex.reset_compiler_cache()


# ── status ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ManagedStatus:
    """What ``GET /health`` reports about the managed install."""

    supported: bool
    installed: bool
    release: str
    version: str
    job: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "installed": self.installed,
            "release": self.release,
            "version": self.version,
            "job": self.job,
        }


def managed_status(root: Path | None = None) -> dict[str, object]:
    """Managed-install status. BLOCKING (filesystem probes) — call off the loop."""
    return ManagedStatus(
        supported=platform_supported(),
        installed=binary_installed(root),
        release=TECTONIC_RELEASE_TAG,
        version=TECTONIC_VERSION,
        job=provision_state(),
    ).to_dict()
