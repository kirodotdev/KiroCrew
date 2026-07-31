"""PPTX Maker — fetching the presentation engine as a digest-pinned tarball.

The engine is a separate public OSS project, so it is not vendored into this
repository — it is fetched into the app's data dir on first use. This module is
the fetch, and it replaced a ``git clone --branch <tag>`` for two reasons:

**No system prerequisite.** ``pip install kirocrew`` must leave nothing for the
user to install by hand. ``uv`` is now a declared Python dependency (resolved
through the installed package, see :mod:`.provision`), but ``git`` is NOT on PyPI
and is far less universal than it looks — a slim Docker image, a fresh Windows
box or a locked-down corporate host may have none. A plain HTTPS download of
``https://github.com/<owner>/<repo>/archive/<commit>.tar.gz`` plus stdlib
``tarfile`` needs nothing but the interpreter.

**A strictly stronger pin.** The old flow cloned a git TAG and then verified
``git rev-parse HEAD`` against :data:`ENGINE_COMMIT`, because a tag is a MUTABLE
ref that upstream can force-move. That check was good, but it verified the
commit id the SERVER reported for the tree it had just handed us. Here the
verification is a sha256 over the received BYTES
(:data:`ENGINE_TARBALL_SHA256`) — computed before anything is extracted, so a
tampered, re-tagged or rebuilt archive is refused rather than built and run.
Provisioning executes this third-party code (``uv sync`` compiles wheels; the
app's agents then drive the engine), so that distinction is the whole security
story of this module.

Follows :mod:`kiro_crew.embeddings` and
:mod:`kiro_crew.apps.builtins.papyrus.backend.tectonic`, the two in-tree
precedents for the same shape: an https-only fetch of a large third-party
artifact, sha256-pinned, streamed and hashed as it arrives, verified BEFORE
install, extracted with every archive member validated.

Everything here is synchronous and BLOCKING (network + filesystem). Callers MUST
already be off the event loop — see ``routes.off_loop`` and the
``no-blocking-call-on-event-loop`` rule.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import ssl
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger("kirocrew.app.pptx-maker")

# ── the pin ─────────────────────────────────────────────────────────────────

#: The public engine repository. Only ever used to build the archive URL.
ENGINE_REPO = "https://github.com/aws-samples/sample-spec-driven-presentation-maker"

#: The upstream release the pinned tree corresponds to. DISPLAY ONLY — it is
#: what the UI shows as ``engineTag`` and what keys the icon-pack marker. It is
#: NOT a locator and NOT verified: nothing is fetched by tag any more, so this
#: string cannot influence which bytes arrive. It is honest because it is only
#: ever reported for a tree whose digest matched
#: :data:`ENGINE_TARBALL_SHA256` — see :func:`installed_tag`.
ENGINE_TAG = "v0.3.8"

#: The exact commit the fetch requests. GitHub serves
#: ``/archive/<full-commit-sha>.tar.gz`` for any commit, and a commit id is
#: content-addressed — unlike a tag, upstream cannot move it.
ENGINE_COMMIT = "b7c7fcf0972b33a480f1b717a3de5bc288d53742"

#: sha256 of that archive, **and the trust anchor for the whole engine**.
#:
#: Produced by downloading the artifact this module names and hashing it as
#: published::
#:
#:     URL=https://github.com/aws-samples/sample-spec-driven-presentation-maker/archive/b7c7fcf0972b33a480f1b717a3de5bc288d53742.tar.gz
#:     curl -sL -o engine.tar.gz "$URL" && shasum -a 256 engine.tar.gz
#:
#: The download was repeated four times and the digest reproduced byte-for-byte
#: each time, which is what makes pinning a GitHub ``/archive/`` tarball viable:
#: for a fixed commit sha the generated archive is stable (the only variable
#: input, the pax ``comment`` header, is the commit sha itself).
#:
#: Bumping the engine means bumping :data:`ENGINE_COMMIT`, this digest and
#: :data:`ENGINE_TAG` together. Bumping the commit alone fails verification on
#: every host, which is the intended failure mode — the digest, not the tag and
#: not the commit, is what decides whether code runs.
ENGINE_TARBALL_SHA256 = "ffe8e10b973cada1f6e99629ac780bde25461b432ad07943b4f15ca11c07dfe0"

_ARCHIVE_URL_TEMPLATE = "{repo}/archive/{commit}.tar.gz"

#: Override the download URL for a mirrored or air-gapped deployment. Must be
#: ``https://``; :data:`ENGINE_TARBALL_SHA256` still has to match, so an
#: override can only change WHERE the bytes come from, never WHICH bytes are
#: accepted.
ENGINE_URL_ENV = "KIROCREW_PPTX_ENGINE_URL"

#: Escape hatch for tests and CI: a test run must never reach the network.
SKIP_DOWNLOAD_ENV = "KIROCREW_PPTX_SKIP_ENGINE_DOWNLOAD"

_HTTPS_SCHEME = "https"

# ── download tuning ─────────────────────────────────────────────────────────

#: ~3MB over a slow link. Generous, and the caller reports rather than hangs.
DOWNLOAD_TIMEOUT = 300
_HTTP_CHUNK_BYTES = 1 << 18

#: Floor for a plausible engine archive. The pinned one is ~3MB; anything under
#: this is an error page or a truncated transfer, not a source tree.
_MIN_ARCHIVE_BYTES = 100_000

#: Ceiling on the bytes any single archive member may expand to, and on the
#: whole tree. The pinned tarball unpacks to ~6MB; these bound a decompression
#: bomb even though the digest pin already means the archive can only be the one
#: named above.
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024

#: Mode applied to extracted files and directories. The archive's own bits are
#: dropped (as stdlib's ``data`` filter does) so an upstream-set setuid or
#: group-writable bit cannot survive into the install.
_FILE_MODE = 0o644
_DIR_MODE = 0o755

# ── install layout ──────────────────────────────────────────────────────────

#: Records which pin produced the tree at ``engine_root()``. Written LAST, after
#: a verified extraction, so its presence is the "this checkout is the vetted
#: one" signal — the replacement for probing ``(root / ".git").is_dir()``, which
#: a tarball install has no way to satisfy.
SOURCE_MARKER_FILENAME = ".kirocrew-engine.json"

_SSL_CA_PATHS = (
    "/etc/pki/tls/certs/ca-bundle.crt",  # AL2, RHEL, CentOS
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/ssl/cert.pem",  # macOS, Alpine
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # Fedora
)


class ArchiveRejected(Exception):
    """An archive member was refused as unsafe to extract."""


def archive_url() -> str:
    """Resolve the download URL: env override, else the pinned archive URL.

    The override exists for mirrored/air-gapped deployments and must be
    ``https://`` — other schemes (``file://``, ``http://``) are refused so an
    operator-supplied value cannot read local files or fetch plaintext. Whatever
    the source, the payload is only trusted after its streaming sha256 matches
    :data:`ENGINE_TARBALL_SHA256`.
    """
    override = os.environ.get(ENGINE_URL_ENV, "").strip()
    if override:
        if override.lower().startswith(f"{_HTTPS_SCHEME}://"):
            return override
        logger.warning(
            "pptx-maker: %s must be an https:// URL — ignoring the override", ENGINE_URL_ENV
        )
    return _ARCHIVE_URL_TEMPLATE.format(repo=ENGINE_REPO, commit=ENGINE_COMMIT)


def redact_url(url: str) -> str:
    """Return *url* safe for logs: scheme + host + path only.

    A mirror override may carry credentials in userinfo or a signed query
    string, so neither is ever logged. Mirrors
    :func:`kiro_crew.embeddings.redact_model_url`.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "<unparseable-url>"


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that leaves ``https``.

    A GitHub ``/archive/`` URL 302s to ``codeload.github.com``, which is normal
    and expected; a hop to ``http://`` would be a silent transport downgrade.
    The digest pin means such a hop could not change WHICH bytes are accepted,
    so this is defence in depth rather than the integrity control.
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


def download_archive(staging: Path) -> tuple[bool, str]:
    """Stream the pinned archive to *staging*, hashing as it goes.

    Returns ``(ok, error)``. The digest is computed over the STREAM rather than
    by re-reading the file afterwards, so a file swapped between write and check
    cannot be what gets verified. A mismatch removes the staged bytes.

    BLOCKING — callers are already off the loop.
    """
    if os.environ.get(SKIP_DOWNLOAD_ENV) == "1":
        return False, f"{SKIP_DOWNLOAD_ENV}=1 — the engine download was skipped"
    url = archive_url()
    # The SSL context goes on an HTTPSHandler, NOT on `opener.open(...)`:
    # `OpenerDirector.open` has no `context` parameter (only the module-level
    # `urlopen` does), so passing one there raises TypeError at request time —
    # i.e. it would fail on every real download while every mocked test passed.
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_make_ssl_context()),
        _HttpsOnlyRedirectHandler,
    )
    request = urllib.request.Request(url, method="GET")
    digest = hashlib.sha256()
    downloaded = 0
    try:
        logger.info("pptx-maker: downloading the engine from %s", redact_url(url))
        staging.parent.mkdir(parents=True, exist_ok=True)
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- archive_url enforces https:// and the payload is sha256-pinned
        with opener.open(request, timeout=DOWNLOAD_TIMEOUT) as resp:
            with staging.open("wb") as out:
                while True:
                    chunk = resp.read(_HTTP_CHUNK_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        staging.unlink(missing_ok=True)
        return False, f"engine download failed: {exc}"
    actual = digest.hexdigest()
    if actual != ENGINE_TARBALL_SHA256:
        staging.unlink(missing_ok=True)
        return False, (
            f"REFUSING the engine archive: sha256 got {actual[:16]}…, expected "
            f"{ENGINE_TARBALL_SHA256[:16]}…. The published bytes for "
            f"{ENGINE_COMMIT[:12]} are not the ones that were vetted; nothing was "
            "extracted, built or run."
        )
    if downloaded < _MIN_ARCHIVE_BYTES:
        staging.unlink(missing_ok=True)
        return False, f"engine archive implausibly small ({downloaded} bytes)"
    return True, ""


def _reject_member_name(name: str) -> str | None:
    """Return a rejection reason for an unsafe member *name*, else ``None``.

    ``tarfile.extractall`` writes wherever a member name points, which makes any
    archive a path-traversal sink. Refused here, before anything is written:

    * an absolute POSIX path (``/etc/cron.d/x``) or a Windows/UNC one
      (``C:\\...``, ``\\\\host\\share``) — a backslash IS a separator on Windows,
      so it is rejected outright rather than only checked as ``/``;
    * any ``..`` segment, anywhere, which is how a relative name escapes;
    * a NUL byte, which truncates the path at the syscall boundary;
    * an empty name.

    Same rules as
    :func:`kiro_crew.apps.builtins.papyrus.backend.tectonic._reject_member_name`.
    """
    if not name:
        return "empty member name"
    if "\0" in name or "\\" in name:
        return f"unsafe member name: {name[:80]!r}"
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return f"absolute member path: {name[:80]!r}"
    if any(part in ("", ".", "..") for part in name.split("/")[:-1]):
        return f"traversal in member path: {name[:80]!r}"
    if name.split("/")[-1] == "..":
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
    business in a source tarball (the pinned one has 418 files and 106 dirs and
    nothing else). Ownership and permission bits are dropped, matching what
    stdlib's ``data`` filter does.
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
    scrubbed.mode = _DIR_MODE if scrubbed.isdir() else _FILE_MODE
    return scrubbed


def _extract_tar(archive: Path, dest: Path) -> None:
    """Extract a ``.tar.gz`` into *dest* with every member validated.

    Prefers stdlib's own filtering hook so the check runs INSIDE ``extractall``
    (no TOCTOU gap between validating a member list and writing it). On Python
    3.10, where the ``filter`` keyword does not exist, the same callable is
    applied to every member first and the extraction is then restricted to the
    validated list.
    """
    with tarfile.open(str(archive), "r:gz") as tar:
        total = sum(max(0, m.size) for m in tar.getmembers())
        if total > _MAX_TOTAL_BYTES:
            raise ArchiveRejected(f"archive expands to too much data ({total} bytes)")
        try:
            tar.extractall(str(dest), filter=_tar_data_filter)  # type: ignore[arg-type]
        except TypeError:
            members = [_tar_data_filter(m, str(dest)) for m in tar.getmembers()]
            tar.extractall(str(dest), members=members)


def _single_root(tree: Path) -> Path | None:
    """The one top-level directory inside an extracted tree, or ``None``.

    A GitHub ``/archive/`` tarball wraps everything in
    ``<repo>-<commit>/``, and the engine is that directory's contents — so the
    wrapper is unwrapped rather than kept, which is what makes
    ``engine_root()/mcp-local`` resolve the same way a clone did.
    """
    try:
        entries = [p for p in tree.iterdir()]
    except OSError:
        return None
    if len(entries) != 1 or not entries[0].is_dir() or entries[0].is_symlink():
        return None
    return entries[0]


def write_source_marker(engine_root: Path) -> None:
    """Record which pin produced this tree. Written LAST, after verification.

    Its presence is what :func:`is_installed` reports, so it must never be
    written for a tree whose digest did not match — that is why it is the final
    step of :func:`install_engine` rather than part of the extraction.
    """
    atomic_write(
        engine_root / SOURCE_MARKER_FILENAME,
        json.dumps(
            {
                "tag": ENGINE_TAG,
                "commit": ENGINE_COMMIT,
                "sha256": ENGINE_TARBALL_SHA256,
                "repo": ENGINE_REPO,
            },
            indent=2,
        )
        + "\n",
    )


def read_source_marker(engine_root: Path) -> dict:
    """The installed tree's marker, or ``{}`` when absent/unreadable/not an object."""
    try:
        data = json.loads((engine_root / SOURCE_MARKER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_installed(engine_root: Path) -> bool:
    """True when *engine_root* holds a tree produced by the CURRENT pin.

    Both the commit and the digest must match, so bumping either one makes an
    existing install read as "needs re-fetching" instead of silently keeping an
    older engine. This is the tarball-era replacement for
    ``(engine_root / ".git").is_dir()`` + ``git rev-parse HEAD``: it answers the
    same question ("is the vetted tree on disk?") from a marker that only a
    digest-verified extraction can have written.
    """
    marker = read_source_marker(engine_root)
    return marker.get("commit") == ENGINE_COMMIT and marker.get("sha256") == ENGINE_TARBALL_SHA256


def installed_tag(engine_root: Path) -> str:
    """The tag recorded for the installed tree, or ``"unknown"``.

    Honest by construction: the marker is only ever written by a
    digest-verified install, so a reported tag is one whose bytes were
    verified. An unverified or absent tree reports ``"unknown"`` rather than
    :data:`ENGINE_TAG` — never claim a version that was not checked.
    """
    if not is_installed(engine_root):
        return "unknown"
    tag = read_source_marker(engine_root).get("tag")
    return tag if isinstance(tag, str) and tag else "unknown"


def install_engine(engine_root: Path, log: list[str]) -> bool:
    """Fetch, verify and install the pinned engine tree at *engine_root*.

    Idempotent: an existing tree that already matches the pin is left untouched
    (no network call). Otherwise the archive is downloaded to a scratch dir,
    verified against :data:`ENGINE_TARBALL_SHA256`, extracted with every member
    validated, and only then swapped into place — so a failure at any step
    leaves the PREVIOUS tree exactly as it was. A user with a working older
    engine whose network is down keeps it, rather than ending up with a
    half-removed one.

    BLOCKING — callers are already off the loop.
    """
    if is_installed(engine_root):
        log.append(f"engine already at {ENGINE_TAG} ({ENGINE_COMMIT[:12]})")
        return True

    previous = read_source_marker(engine_root).get("commit")
    if previous:
        log.append(f"updating the engine {str(previous)[:12]} -> {ENGINE_TAG}…")
    else:
        log.append(f"downloading the presentation engine at {ENGINE_TAG}…")

    work = engine_root.parent / f".engine-fetch.{os.getpid()}"
    archive = work / "engine.tar.gz"
    unpacked = work / "unpacked"
    try:
        shutil.rmtree(work, ignore_errors=True)
        unpacked.mkdir(parents=True, exist_ok=True)
        ok, error = download_archive(archive)
        if not ok:
            log.append(error)
            logger.error("pptx-maker: engine fetch refused: %s", error)
            return False
        try:
            _extract_tar(archive, unpacked)
        except (ArchiveRejected, tarfile.TarError, OSError) as exc:
            log.append(f"engine archive rejected: {exc}")
            logger.error("pptx-maker: engine archive rejected: %s", exc)
            return False
        extracted = _single_root(unpacked)
        if extracted is None:
            log.append("the engine archive does not contain a single source tree")
            return False
        if not (extracted / "mcp-local").is_dir():
            log.append("the engine archive has no mcp-local directory")
            return False
        _swap_in(extracted, engine_root)
        write_source_marker(engine_root)
        log.append(f"engine installed at {ENGINE_TAG} ({ENGINE_COMMIT[:12]})")
        return True
    except OSError as exc:
        log.append(f"engine install failed: {exc}")
        logger.warning("pptx-maker: engine install failed: %s", exc)
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _swap_in(extracted: Path, engine_root: Path) -> None:
    """Replace *engine_root* with *extracted*, keeping the window tiny.

    The new tree is moved next to the destination first (same filesystem, so the
    final renames are cheap), the old one is moved aside, and only then is the
    new one renamed into place. The old tree is removed afterwards: doing it
    last means a crash mid-swap leaves recoverable bytes rather than nothing.
    """
    engine_root.parent.mkdir(parents=True, exist_ok=True)
    staging = engine_root.parent / f".{engine_root.name}.new.{os.getpid()}"
    retired = engine_root.parent / f".{engine_root.name}.old.{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(retired, ignore_errors=True)
    shutil.move(str(extracted), str(staging))
    moved_aside = False
    try:
        if engine_root.exists():
            os.replace(engine_root, retired)
            moved_aside = True
        os.replace(staging, engine_root)
    except OSError:
        # The second replace failed with the old tree already moved aside, so
        # `engine_root` does not exist right now. Put it BACK before unwinding:
        # deleting `retired` here (as the old unconditional cleanup did) would take
        # the user's working engine with it and leave no engine at all — turning a
        # failed *update* into a broken install. Rolling back means the worst case
        # is "still on the previous version", which is the same degradation
        # `_ensure_clone` already promises on a network failure.
        if moved_aside and not engine_root.exists():
            try:
                os.replace(retired, engine_root)
            except OSError:
                logger.error(
                    "pptx-maker: engine swap failed and the previous tree could not be "
                    "restored; it is preserved at %s",
                    retired,
                )
                raise
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        # Only ever removes the retired tree once `engine_root` is populated again —
        # either by the successful swap or by the rollback above.
        if engine_root.exists():
            shutil.rmtree(retired, ignore_errors=True)
