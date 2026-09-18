"""One verified HTTPS download of one pinned file — the shared transfer engine.

Extracted from :mod:`kiro_crew.embeddings`, which had the only copy: a streamed
GET whose bytes are hashed as they arrive, an atomic install, and an error string
per failure mode. The embedding model was the first pinned artifact Kiro Crew
fetches at runtime; feature-video clips are the second, and a second private
downloader would mean two places for "did we verify this before installing it?"
to be answered differently.

What this module owns, and what it deliberately does not:

* it owns the TRANSFER — connect, stream, hash, verify, install, and the wording
  of each failure — so every caller inherits the same verify-before-install
  order;
* it does NOT own retry policy, backoff, concurrency, or WHICH url to fetch.
  Those are caller decisions (the model manager retries for hours across gateway
  boots; the video cache walks a manifest one entry at a time), and a retry loop
  buried here would make a caller's own loop invisible.

Two invariants hold for every caller:

**The sha256 pin is the trust anchor, not the origin.** The url may come from an
operator override or a signed manifest; either way nothing is installed until the
streamed digest matches the expected one, so a tampered CDN object can only fail
verification. A caller with no pin has no business using this module.

**Nothing lands at the final path until it is verified.** Bytes accumulate in a
staging file beside the target and reach *path* through one ``os.replace``, so a
reader either sees the previous file or the complete new one — never a truncated
prefix. This is what makes an interrupted transfer safe to resume: the partial is
a staging file nobody serves.

**The transfer stays inside the host it was authorized for.** ``urlopen`` follows
a redirect by default, and the url this module is handed was authorized by one
check against one host — so a cross-host redirect would spend that authorization
somewhere nobody approved, which on a gateway that can reach an internal network
is an SSRF. :class:`_SameHostRedirectHandler` refuses any redirect that changes
the host or leaves https. That is the default for every caller, and the only
policy for a url that came from a CDN or a signed manifest. A caller MAY pass
``allow_cross_host_redirects=True`` for exactly one shape of url: one the
OPERATOR set in the gateway's process environment. Such a url names the
operator's own mirror, and the common mirror shapes (an artifact store or a
bucket answering with a redirect to its storage host) legitimately hop hosts;
the hop stays on https (:class:`_HttpsOnlyRedirectHandler`) and the bytes are
still sha256-pinned. A url from a config file is NOT that shape — config is
agent-writable — and keeps the strict policy.

**No transfer is unbounded.** The staging file is written as bytes arrive, so a
body that never ends fills the disk before the end-of-stream digest can reject
it. Every transfer therefore carries a ceiling: *size* when a manifest declares
the exact length, *max_bytes* when only a bound is known, and
:data:`DEFAULT_MAX_BYTES` when a caller states neither.

**The staging file is opened without following a symlink.** The staging path is
derived from the target, which can sit in a directory something else may write —
so a symlink planted there would redirect the append onto whatever it points at.
The open refuses a symlink outright.

**A caller that can pin the directory addresses nothing by path afterwards.**
Refusing a link at the final NAME settles the leaf, not the directory above it:
a validated ``<dir>`` swapped for a link between the check and the open sends the
open, the rename that installs and the receipt that follows wherever the link
points, however carefully each leaf name is handled. :class:`TargetDir` is the
one seam every filesystem operation of a transfer goes through, and
:func:`pin_target_dir` returns the descriptor-holding kind: on POSIX each
operation is relative to the open directory descriptor (``openat`` and friends),
on Windows the held handle stops the directory and every ancestor from being
renamed or deleted for as long as it lives. The plain :class:`TargetDir` a bare
path gets is the by-name behaviour, named so a reader can see which callers have
which guarantee.
"""

from __future__ import annotations

import errno
import hashlib
import io
import logging
import os
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from kiro_crew import pinned_fs, platform_compat
from kiro_crew._ssl_compat import _ssl_context_has_ca_trust
from kiro_crew.atomic_write import atomic_write, atomic_write_at

logger = logging.getLogger(__name__)

#: Default per-request timeout. Generous because the first caller pulls 610MB:
#: a slow link must retry under the CALLER's backoff, not die mid-transfer.
DEFAULT_TIMEOUT_SECS = 1800

#: Read size. One MiB is large enough that the per-chunk Python overhead is
#: noise against the socket read, and small enough that a rate limiter can pace
#: to a few hundred KiB/s without overshooting a whole second.
DEFAULT_CHUNK_BYTES = 1 << 20

#: How often ``on_progress`` fires, in bytes. A progress callback that ran per
#: chunk would write a status dict a thousand times for a 1GB file.
DEFAULT_PROGRESS_EVERY_BYTES = 16 << 20

#: Every failure string starts with this, so a status panel can tell a transfer
#: failure from the caller's own errors by prefix. One wording for every caller.
_ERROR_PREFIX = "HTTPS download failed"

#: Suffix of the resumable staging file. Stable (no pid) BECAUSE resume is the
#: point: a partial from a previous process must be recognizable by the next one.
#: A caller that does not want cross-process reuse passes its own *staging* path.
PART_SUFFIX = ".part"

#: Ceiling applied when a caller declares neither an exact *size* nor a
#: *max_bytes*. Generous — the largest thing Kiro Crew fetches is a ~610MB model —
#: because its job is only to make "unbounded" unreachable: bytes are written as
#: they arrive, so without a ceiling an endless body fills the disk long before the
#: end-of-stream digest gets to reject it. A caller that knows its payload passes a
#: real bound and gets a far tighter guarantee.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024

_SSL_CA_PATHS = (
    "/etc/pki/tls/certs/ca-bundle.crt",  # AL2, RHEL, CentOS
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/ssl/cert.pem",  # macOS, Alpine
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # Fedora
)

#: HTTP status for a satisfied ``Range`` request. Anything else on a ranged
#: request means the server ignored the range and is sending the whole body.
_HTTP_PARTIAL_CONTENT = 206


def make_ssl_context() -> ssl.SSLContext:
    """An SSL context that finds system CA certs on all supported platforms.

    Bundled Python runtimes (like the desktop backend's interpreter) may not ship
    their own CA bundle and rely on ``load_default_certs()``, which calls
    OpenSSL's compiled-in defaults — and those can miss when the compiled path
    does not match the host OS (common on AL2 with a cross-compiled Python).
    """
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
        if _ssl_context_has_ca_trust(ctx):
            return ctx
    except ssl.SSLError:
        pass
    for path in _SSL_CA_PATHS:
        if os.path.isfile(path):
            ctx.load_verify_locations(cafile=path)
            return ctx
    # Last resort: honour SSL_CERT_FILE / SSL_CERT_DIR from the environment.
    return ctx


class RedirectRefused(urllib.error.URLError):
    """A redirect the transfer's policy would not follow.

    Raised by both redirect handlers instead of returning ``None``: urllib turns a
    refused ``redirect_request`` into a bare ``HTTPError 302``, which a reader of
    the dashboard's download status cannot tell from a broken CDN. The ``reason``
    names the two HOSTS and the policy in force — never a path, query or
    credential — so :func:`download_to` can return it verbatim, and an operator
    whose mirror redirects sees why the transfer stopped and which override
    accepts that shape.
    """

    def __init__(self, origin: str, target: str, policy: str) -> None:
        ohost = (urllib.parse.urlsplit(origin).hostname or "?").lower()
        try:
            thost = (urllib.parse.urlsplit(target).hostname or "?").lower()
        except ValueError:
            thost = "?"
        super().__init__(f"redirect from {ohost} to {thost} refused: {policy}")


#: Policy text for the default handler's refusal. Names the remedy: a url the
#: operator sets in the environment may follow the hop, a CDN or config url may not.
_SAME_HOST_POLICY = (
    "the transfer stays on the host it was authorized for (an operator mirror that"
    " redirects across hosts is honoured only when set through the environment override)"
)
_HTTPS_ONLY_POLICY = "the transfer stays on https"


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse any redirect that leaves https or changes the host.

    The authorization to make a request is granted per HOST — a manifest's signed
    ``cdn_base``, or an operator's https-only override. ``urlopen`` follows a
    redirect without re-asking, so the default behaviour spends that grant on a
    destination the CDN chose: on a gateway that can route to an internal network,
    that is a blind SSRF with the response fed straight back into the caller.

    Same-host redirects stay allowed (a CDN legitimately reshapes its own paths).
    "Same host" is the same hostname AND the same effective port: a redirect to
    ``:8443`` on the authorized hostname is a different service — on a box that
    co-locates an internal listener with the trusted name, it is the SSRF this
    handler exists to close, just one port over. An explicit ``:443`` is the
    same origin as no port at all. Anything else raises :class:`RedirectRefused`,
    which every caller here treats as a transport failure — the fail-safe
    direction — and whose message says which hop was refused.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        try:
            target = urllib.parse.urlsplit(newurl)
            origin = urllib.parse.urlsplit(req.full_url)
            # `.port` parses lazily and raises ValueError on a non-numeric port,
            # so it belongs inside the try with the split itself.
            same_origin = target.scheme == "https" and (
                (target.hostname or "").lower(),
                target.port or 443,
            ) == ((origin.hostname or "").lower(), origin.port or 443)
        except ValueError:
            same_origin = False
        if not same_origin:
            logger.warning(
                "refusing a cross-origin redirect from %s to %s",
                redact_url(req.full_url),
                redact_url(newurl),
            )
            raise RedirectRefused(req.full_url, newurl, _SAME_HOST_POLICY)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that leaves https; allow one that changes the host.

    The relaxed policy, reachable only through ``allow_cross_host_redirects``,
    for a url the operator set in the process environment: their own mirror
    may hand the transfer to another host (an artifact store fronting a bucket,
    a bucket answering with a redirect to its regional endpoint), and the sha256
    pin still decides whether the bytes are installed. What stays refused is the
    downgrade — a hop to plaintext would let the path be observed and altered in
    transit before the pin ever sees it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        try:
            target = urllib.parse.urlsplit(newurl)
            # `.port` parses lazily and raises ValueError on a non-numeric port,
            # so it belongs inside the try: a target urllib cannot even address
            # is refused here rather than blowing up inside the opener.
            target.port
            scheme = target.scheme
        except ValueError:
            scheme = ""
        if scheme != "https":
            logger.warning(
                "refusing a redirect off https from %s to %s",
                redact_url(req.full_url),
                redact_url(newurl),
            )
            raise RedirectRefused(req.full_url, newurl, _HTTPS_ONLY_POLICY)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener(
    context: "ssl.SSLContext | None" = None, *, allow_cross_host_redirects: bool = False
) -> urllib.request.OpenerDirector:
    """An opener that verifies TLS and pins redirects.

    The one way the callers of THIS module — the embedding-model transfer and the
    feature-video manifest and clip transfers — make an outbound request. A
    caller reaching for ``urllib.request.urlopen`` directly gets urllib's default
    redirect handler back, which is the SSRF this exists to close. (The Papyrus
    and PPTX engines keep their own openers for now; see the module docstring's
    account of what this module does not yet own.)

    *allow_cross_host_redirects* selects :class:`_HttpsOnlyRedirectHandler` in
    place of :class:`_SameHostRedirectHandler`. It is for a url the operator set
    in the process environment and nothing else — see the module docstring.
    """
    handler = _HttpsOnlyRedirectHandler if allow_cross_host_redirects else _SameHostRedirectHandler
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context or make_ssl_context()), handler
    )


def redact_url(url: str) -> str:
    """Return *url* safe for logs: scheme and host only.

    Every other component can carry a credential. Userinfo and a signed query
    string are the obvious ones (a presigned URL is itself a credential), and the
    PATH is the one that looks safe and is not: a private mirror can put a token
    in a path segment, and this string goes to the gateway log on every transfer.

    The cost is that a log line does not name which file was fetched. That is
    covered: every caller passes a *label* (``feature-video clip <id>``), which is
    what a reader actually needs, and it is not attacker-controlled.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, host, "", "", ""))
    except Exception:
        return "<unparseable-url>"


#: ``O_BINARY`` exists only on Windows (where a text-mode descriptor translates
#: bytes); ``O_NOFOLLOW`` only on POSIX. Both read as 0 where absent.
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _resume_partial(
    target_dir: "TargetDir", name: str, *, size: int
) -> "tuple[io.BufferedWriter, int, hashlib._Hash] | None":
    """Open the partial at *name* ONCE for hash-and-append, or None when there is nothing to resume.

    Returns ``(file, prefix_length, digest)``: *file* is the partial opened for
    append, *digest* has consumed exactly *prefix_length* bytes read through the
    SAME descriptor. The process that wrote those bytes is gone, so the digest has
    to be rebuilt from disk -- and it must be rebuilt from the file that will be
    appended to. Hashing one descriptor and appending through a second one opened
    by name later left a window: a same-size file swapped in at the name between
    the two would be appended to and installed under a digest that never covered
    its prefix. One descriptor, held from the first read to the install, closes
    that window on every platform.

    ``None`` means "start fresh": nothing at the name, an empty partial, one at or
    past the declared *size* (its bytes are not a prefix of the wanted file), or
    one that could not be read. The caller's fresh path removes whatever is there
    and creates the staging file exclusively. A link, a directory or a hard-linked
    file at the name is not a partial of ours and raises ``StagingRefused``: a
    caller asking to RESUME asked to trust what is there, and this is not
    trustworthy.
    """
    staging = target_dir.describe(name)
    try:
        named = target_dir.lstat(name)
    except OSError as exc:
        raise StagingRefused(f"staging path cannot be inspected: {staging}") from exc
    if named is None:
        return None
    if not stat.S_ISREG(named.st_mode):
        raise StagingRefused(
            f"refusing to resume through a symlinked (or non-file) staging path: {staging}"
        )
    # No O_CREAT: a partial that is not there is not resumed. O_APPEND, so every
    # write lands at the end whatever the read position is after hashing.
    flags = os.O_RDWR | os.O_APPEND | _O_NOFOLLOW | _O_BINARY
    try:
        fd = target_dir.open(name, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StagingRefused(
                f"refusing to resume through a symlinked staging path: {staging}"
            ) from exc
        return None
    try:
        _refuse_unless_named_file(fd, target_dir, name)
        partial = os.fstat(fd).st_size
        if not partial or (size and partial >= size):
            os.close(fd)
            return None
        h = hashlib.sha256()
        remaining = partial
        while remaining > 0:
            chunk = os.read(fd, min(1 << 20, remaining))
            if not chunk:
                # Shorter than its own size a moment ago: not the file that was
                # measured. Worthless; the fresh path replaces it.
                os.close(fd)
                return None
            h.update(chunk)
            remaining -= len(chunk)
    except StagingRefused:
        os.close(fd)
        raise
    except OSError:
        os.close(fd)
        return None
    return os.fdopen(fd, "ab"), partial, h


class TargetDir:
    """The directory a transfer lands in, addressed by NAME on every operation.

    Every filesystem call a transfer makes — inspect, create, append, hash,
    rename, lock down, remove — goes through one of these, and the plain class is
    the by-name behaviour: each method resolves ``<directory>/<name>`` afresh.
    That is what a caller handing :func:`download_to` a bare path gets, and it is
    named rather than implicit so the guarantee each caller holds is visible at
    the call site. :class:`PinnedTargetDir` is the same interface with the
    directory held open, for a caller whose directory something else may swap.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def prepare(self) -> None:
        """Make sure the directory exists. Raises ``OSError``."""
        self.directory.mkdir(parents=True, exist_ok=True)

    def describe(self, name: str) -> Path:
        """The path *name* is known by, for messages. Never opened."""
        return self.directory / name

    def lstat(self, name: str) -> os.stat_result | None:
        """``lstat`` of *name*, or ``None`` when nothing is there. Never follows."""
        try:
            return os.lstat(self.directory / name)
        except FileNotFoundError:
            return None

    def open(self, name: str, flags: int, mode: int = 0o600) -> int:
        return os.open(self.directory / name, flags, mode)

    def unlink(self, name: str) -> None:
        """Remove *name*. Missing is success; a link is removed AS a link."""
        try:
            os.unlink(self.directory / name)
        except FileNotFoundError:
            pass

    def replace(self, src: str, dst: str) -> None:
        os.replace(self.directory / src, self.directory / dst)

    def restrict_to_owner(self, name: str) -> None:
        """Owner-only lockdown of *name*. Raises ``OSError``."""
        platform_compat.restrict_to_owner(self.directory / name)

    def write_text(self, name: str, content: str) -> None:
        """Atomically publish *content* at *name*, owner-only. Raises ``OSError``."""
        atomic_write(
            self.directory / name, content, restrict_to_owner=True, restrict_on_error="warn"
        )


class TargetDirRefused(OSError):
    """The target directory could not be pinned: a link sits where it should be.

    A subclass of ``OSError`` so a caller's "the cache is unavailable" branch
    handles it, and distinct so a test can tell the refusal from a disk error.
    Its message names a local path and nothing else.
    """


class PinnedTargetDir(TargetDir):
    """A :class:`TargetDir` held open, so nothing above the leaf is re-resolved.

    ``fd`` is the directory descriptor :func:`pin_target_dir` opened. What it buys
    differs by platform, and both are stated rather than blurred:

    * POSIX: every operation is descriptor-relative (``openat``, ``unlinkat``,
      ``renameat``, ``fstatat``), so the directory the caller validated is the
      directory each call addresses, whatever its NAME has since been swapped for.
      A link planted at an ancestor after the pin changes nothing.
    * Windows: there is no ``dir_fd``. The handle is opened without
      ``FILE_SHARE_DELETE``, which stops the directory — and every directory above
      it — from being renamed or deleted while the handle lives, so the by-name
      operations below cannot be redirected by a swap because the swap cannot
      happen. The leaf is settled by the same no-follow rules as everywhere else.

    Reading the descriptor's real path (:func:`kiro_crew.pinned_fs.fd_real_path`)
    is how a caller proves the pinned directory is the one it meant — the
    feature-video cache does exactly that against its canonical root.
    """

    def __init__(self, directory: Path, fd: int, *, relative: bool) -> None:
        super().__init__(directory)
        self.fd = fd
        self._relative = relative

    def prepare(self) -> None:
        return None  # it is open, so it exists

    def lstat(self, name: str) -> os.stat_result | None:
        if not self._relative:
            return super().lstat(name)
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def open(self, name: str, flags: int, mode: int = 0o600) -> int:
        if not self._relative:
            return super().open(name, flags, mode)
        return os.open(name, flags, mode, dir_fd=self.fd)

    def unlink(self, name: str) -> None:
        if not self._relative:
            return super().unlink(name)
        try:
            os.unlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            pass

    def replace(self, src: str, dst: str) -> None:
        if not self._relative:
            return super().replace(src, dst)
        os.replace(src, dst, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def restrict_to_owner(self, name: str) -> None:
        if not self._relative:
            return super().restrict_to_owner(name)
        # fchmod on a descriptor opened relative to the pin: os.chmod's
        # follow_symlinks=False is unsupported on Linux, and a by-name chmod would
        # be the one path-addressed step left in the transfer.
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=self.fd)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    def write_text(self, name: str, content: str) -> None:
        if not self._relative:
            return super().write_text(name, content)
        atomic_write_at(self.fd, name, content, mode=0o600)


@contextmanager
def pin_target_dir(
    directory: Path, *, what: str = "download directory"
) -> Iterator[PinnedTargetDir]:
    """Hold *directory* open for the duration; yield the :class:`PinnedTargetDir`.

    *directory* must already exist as a real directory — create it first, then
    pin what was created. On POSIX the ancestor chain is pinned one ``openat`` at
    a time from the resolved parent (:func:`kiro_crew.pinned_fs.open_dir_pinned`),
    so a component swapped for a link after the path was resolved is refused
    rather than followed; on Windows :func:`kiro_crew.platform_compat.pin_directory`
    opens the handle that blocks renames above it. A link or a non-directory at
    the name raises :class:`TargetDirRefused` on both.
    """
    if pinned_fs.supports_pinned_walk():
        fd = pinned_fs.open_dir_pinned(directory, what=what, refusal=TargetDirRefused)
        relative = True
    else:
        try:
            fd = platform_compat.pin_directory(directory)
        except NotADirectoryError as exc:
            raise TargetDirRefused(
                f"refusing to use the {what}: {directory} is not a real directory"
            ) from exc
        relative = False
    try:
        yield PinnedTargetDir(directory, fd, relative=relative)
    finally:
        os.close(fd)


class StagingRefused(OSError):
    """The staging path itself was rejected before any transfer began.

    A distinct type because the handler treats it differently from a transport
    error: this message names a LOCAL path and nothing else, so it is returned
    verbatim, while a transport error's message can carry the url (and any
    credential in it) and is reduced to its type. Telling "something planted a
    symlink in your cache" apart from "the network failed" is the whole diagnostic
    value here, and a redacted message would erase it.
    """


def _refuse_unless_named_file(fd: int, target_dir: TargetDir, name: str) -> None:
    """Refuse unless *fd* is the regular file that *name* NAMES, checked on the descriptor.

    The half of the no-follow guarantee that does not depend on the platform.
    ``os.open`` follows a symlink or a reparse point on Windows, where
    ``O_NOFOLLOW`` does not exist, so what was opened may not be what the name
    says. Comparing the opened object's identity (``fstat``: device and file index)
    with the name's own identity (``lstat``, which never follows) settles it after
    the fact and without a window: a link at the name has its own identity, so the
    two differ and the descriptor is refused before a byte is written. A swap AFTER
    this check changes nothing — the descriptor stays on the file it was opened on.

    The descriptor must also be the inode's ONLY name (``st_nlink == 1``). A hard
    link is invisible to every rule above: it is not a link to be followed, and it
    IS the named inode, so the identity comparison passes — yet appending to it
    writes into a file that also answers to another name, and a digest failure
    afterwards unlinks only the staging name, leaving those bytes in the other. A
    staging file this module created has exactly one name; one with more was put
    there by something else, and is refused before the first write.
    """
    staging = target_dir.describe(name)
    try:
        opened = os.fstat(fd)
        named = target_dir.lstat(name)
    except OSError as exc:
        raise StagingRefused(f"staging path cannot be verified: {staging}") from exc
    if named is None or not stat.S_ISREG(named.st_mode):
        raise StagingRefused(f"refusing to write through a symlinked staging path: {staging}")
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise StagingRefused(
            f"refusing to write through a symlinked staging path (name and file differ): {staging}"
        )
    if opened.st_nlink != 1:
        raise StagingRefused(
            f"refusing to write through a hard-linked staging path ({opened.st_nlink} names): {staging}"
        )


def _remove_stale_staging(target_dir: TargetDir, name: str) -> None:
    """Remove whatever sits at *name* so a fresh transfer can create it exclusively.

    ``unlink`` removes a symlink as a link and never touches its target, so a
    planted link is destroyed rather than written through. A directory at the name
    is refused: nothing this module does puts one there, and removing a tree is
    not a downloader's call to make.
    """
    staging = target_dir.describe(name)
    try:
        named = target_dir.lstat(name)
    except OSError as exc:
        raise StagingRefused(f"staging path cannot be inspected: {staging}") from exc
    if named is None:
        return
    if stat.S_ISDIR(named.st_mode):
        raise StagingRefused(f"refusing to replace a directory at the staging path: {staging}")
    try:
        target_dir.unlink(name)
    except OSError as exc:
        raise StagingRefused(f"stale staging path cannot be removed: {staging}") from exc


def _open_staging_nofollow(target_dir: TargetDir, name: str) -> "io.BufferedWriter":
    """Create staging file *name* for a FRESH transfer, refusing to follow a symlink.

    The staging path is derived from the target, so it lives wherever the target
    does — a directory something other than this process may be able to write. A
    symlink planted there would send the truncating create to whatever it points
    at, which turns a download into an arbitrary-file write.

    The open itself is the check, on every platform, so there is no window between
    a pre-check and the open for a planted link to win: whatever is at the name is
    removed (a link is removed AS a link, never followed — and a directory is
    refused) and then the name is created exclusively (``O_CREAT | O_EXCL``). A
    link planted between the removal and the create makes the create fail,
    because the name exists — on Windows as on POSIX. ``O_NOFOLLOW`` refuses a
    link at the name on POSIX; Windows has no such flag and follows, so the
    descriptor is then compared to the name (:func:`_refuse_unless_named_file`)
    and refused when they are not the same object. That comparison runs on every
    platform; on POSIX it is a second lock on a door already shut.

    A RESUMED transfer never comes here: :func:`_resume_partial` opens the
    existing partial once and hashes and appends through that one descriptor. A
    restart after the server ignored a ``Range`` takes this path.

    All of it through *target_dir*: with a :class:`PinnedTargetDir` the name is
    opened relative to the held directory, so the directory the caller checked is
    the one written into.
    """
    staging = target_dir.describe(name)
    _remove_stale_staging(target_dir, name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY
    try:
        # 0o600: the payload may be owner-only, and a staging file that is briefly
        # world-readable is the same exposure as the installed one being so.
        fd = target_dir.open(name, flags, 0o600)
    except FileExistsError as exc:
        raise StagingRefused(
            f"refusing to write through a path planted at the staging name: {staging}"
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StagingRefused(
                f"refusing to write through a symlinked staging path: {staging}"
            ) from exc
        raise
    try:
        _refuse_unless_named_file(fd, target_dir, name)
    except StagingRefused:
        os.close(fd)
        raise
    return os.fdopen(fd, "wb")


def _identity(st: os.stat_result) -> tuple[int, int]:
    """The inode a stat result names: ``(device, file index)``, on every platform."""
    return st.st_dev, st.st_ino


def _install(
    target_dir: TargetDir,
    staging_name: str,
    name: str,
    *,
    verified: tuple[int, int],
    restrict_to_owner: bool,
) -> None:
    """Move the verified staging file onto *name* atomically, both under *target_dir*.

    *verified* is the identity (:func:`_identity`) of the descriptor the bytes were
    hashed through. The rename is by NAME, and the digest was computed on the bytes
    written to one INODE — so a file swapped in at the staging name between the
    last write and the rename would be installed under a digest it never met.
    After the rename the installed name is opened with no-follow semantics and its
    identity compared to *verified*; anything else is unlinked and refused
    (``StagingRefused``), so what reaches the final name is the inode that was
    verified or nothing. The caller keeps the hashed descriptor OPEN across this
    call on POSIX, which is what makes the identity meaningful there: a file
    unlinked and recreated at the staging name would otherwise be free to land
    on the same inode number. A swap landing after that check is the documented
    residual: the receipt certifies what was installed, not what is read back.

    Lockdown happens BEFORE the rename, so the payload is never readable at its
    final name under the inherited umask/DACL — the same ordering
    ``atomic_write(restrict_to_owner=True)`` uses, and the reason this is not a
    ``chmod`` after the rename.
    """
    if restrict_to_owner:
        try:
            target_dir.restrict_to_owner(staging_name)
        except OSError:
            # Warn-and-continue, matching atomic_write's "warn" posture: a
            # lockdown failure must not lose an otherwise verified download,
            # but it must be visible.
            logger.warning(
                "could not restrict %s to its owner",
                target_dir.describe(staging_name),
                exc_info=True,
            )
    target_dir.replace(staging_name, name)
    try:
        fd = target_dir.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
    except OSError as exc:
        raise StagingRefused(
            f"installed file cannot be re-opened for verification: {target_dir.describe(name)}"
        ) from exc
    try:
        installed = _identity(os.fstat(fd))
    finally:
        os.close(fd)
    if installed != verified:
        # Not the inode the digest was computed on. Remove it rather than leave an
        # unverified file under a name the manifest will be asked to serve.
        _discard(target_dir, name)
        raise StagingRefused(
            f"staging file was replaced during the transfer; nothing installed: "
            f"{target_dir.describe(name)}"
        )


def _discard(target_dir: TargetDir, name: str) -> None:
    """Best-effort removal of a staging file on a failure branch. Never raises."""
    try:
        target_dir.unlink(name)
    except OSError:
        logger.debug(
            "staging file %s could not be removed", target_dir.describe(name), exc_info=True
        )


def download_to(
    path: Path,
    url: str,
    *,
    sha256: str,
    size: int = 0,
    max_bytes: int = 0,
    min_bytes: int = 0,
    resume: bool = False,
    rate_limit_bytes_per_s: int = 0,
    staging: Path | None = None,
    timeout_secs: int = DEFAULT_TIMEOUT_SECS,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    progress_every_bytes: int = DEFAULT_PROGRESS_EVERY_BYTES,
    on_progress: Callable[[int, int], None] | None = None,
    on_verifying: Callable[[], None] | None = None,
    restrict_to_owner: bool = False,
    allow_cross_host_redirects: bool = False,
    label: str = "asset",
    target_dir: TargetDir | None = None,
) -> tuple[bool, str]:
    """Fetch *url* into *path*, verified against *sha256*. Blocking.

    Returns ``(True, "")`` once the verified bytes are in place, else
    ``(False, <reason>)``. Never raises: a caller running this on a background
    thread has no place to handle an exception, and every failure here is one it
    should retry or report rather than crash on.

    * *sha256* is required. The digest is computed while streaming, so a
      complete-but-corrupt transfer costs no second pass over the file.
    * *size*, when known from a manifest, bounds the transfer: a body longer than
      *size* is abandoned rather than written to the end of the disk, and a
      partial larger than *size* is discarded instead of resumed.
    * *max_bytes* is the ceiling for a payload whose exact length is NOT declared —
      a poster, say, where the manifest carries a sha but no byte count. It bounds
      the transfer without claiming to know the length, so it never discards a
      resumable partial. With neither given the ceiling is
      :data:`DEFAULT_MAX_BYTES`: the guard exists because bytes are written as they
      arrive, so an endless body would fill the disk before the digest could
      reject it.
    * *min_bytes* is a floor for the "the CDN served us an error page" case,
      where a small body can still hash consistently across attempts.
    * *resume* sends a ``Range`` request when a staging file is already present.
      The partial is opened once; its bytes are hashed and the rest appended
      through that one descriptor, so what the digest covers is what is
      installed. A server that ignores the range (answering 200) restarts the
      transfer from zero, which is why the digest is only ever trusted end to end.
    * *rate_limit_bytes_per_s* paces the read loop, so a background fetch does
      not take the user's link. It bounds THIS transfer only — a caller running
      several at once is doing its own budgeting.
    * *allow_cross_host_redirects* is for a url the OPERATOR set in the process
      environment, and nothing else: their mirror may redirect to another https
      host and the pin still decides. A url from a CDN, a signed manifest or a
      config file keeps the default, which refuses to leave the host.
    * *target_dir* is the handle every filesystem step goes through. A caller
      that has pinned ``path.parent`` (:func:`pin_target_dir`) passes it and the
      staging file, the install rename and the lockdown all address the directory
      it holds open; without one, ``path.parent`` is addressed by name. *staging*,
      when given, must sit in that same directory.
    """
    if not sha256:
        return False, f"{_ERROR_PREFIX}: no sha256 pin for {label}"
    if not url.lower().startswith("https://"):
        # Belt-and-braces: every caller resolves its url through its own
        # https-only gate, but this function installs whatever it fetched, so it
        # refuses plaintext (and file://, which would read a local path) itself.
        return False, f"{_ERROR_PREFIX}: refusing a non-https url"

    # size (exact) beats max_bytes (a bound) beats the module default; the result
    # is never 0, so no transfer runs without a ceiling.
    ceiling = size or max_bytes or DEFAULT_MAX_BYTES
    if staging is not None and staging.parent != path.parent:
        return False, f"{_ERROR_PREFIX}: staging file must sit beside {label}"
    staging_name = staging.name if staging is not None else f"{path.name}{PART_SUFFIX}"
    where = target_dir if target_dir is not None else TargetDir(path.parent)
    try:
        where.prepare()
    except OSError as exc:
        return False, f"{_ERROR_PREFIX}: {exc}"

    # The staging descriptor. A resumed transfer opens the existing partial ONCE,
    # hashes its bytes through that descriptor and appends through the same one,
    # so the prefix the digest counts is the prefix that gets installed -- a file
    # swapped in at the staging name after the hash is never written to. On POSIX
    # the descriptor stays open until the install has been checked against it: an
    # inode with an open descriptor cannot be reused, so a file unlinked and
    # recreated at the staging name is a DIFFERENT inode and the identity
    # comparison in `_install` sees it. Windows refuses to rename or unlink a file
    # with an open handle, so there it is closed as soon as the body has been
    # written -- NTFS file ids carry a reuse sequence number, so the identity
    # comparison holds without the hold.
    out: "io.BufferedWriter | None" = None
    offset = 0
    digest = hashlib.sha256()
    try:
        if resume:
            resumed = _resume_partial(where, staging_name, size=size)
            if resumed is not None:
                out, offset, digest = resumed
        # A non-resuming caller, or nothing worth resuming: whatever sits at the
        # staging name (a stale partial from an abandoned attempt, a planted link)
        # is removed by the fresh open below, never appended to.

        request = urllib.request.Request(url, method="GET")
        if offset:
            request.add_header("Range", f"bytes={offset}-")
        logger.info("Downloading %s from %s", label, redact_url(url))
        opener = build_opener(allow_cross_host_redirects=allow_cross_host_redirects)
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- https is enforced above, redirects are pinned (host-pinned unless the operator's own env url), and the payload is sha256-pinned
        with opener.open(request, timeout=timeout_secs) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            if offset and status != _HTTP_PARTIAL_CONTENT:
                # The server ignored our Range and is sending the whole body.
                # Start over rather than appending it to the prefix we hold: the
                # held partial is let go (closed first -- Windows will not remove
                # an open file) and the fresh open below replaces it.
                logger.info("%s: server ignored the range request; restarting", label)
                if out is not None:
                    out.close()
                    out = None
                offset = 0
                digest = hashlib.sha256()
            declared = int(resp.headers.get("Content-Length", 0) or 0)
            total = (offset + declared) if declared else (size or max_bytes)
            downloaded = offset
            this_run = 0
            started = time.monotonic()
            overflow = 0
            if out is None:
                out = _open_staging_nofollow(where, staging_name)
            staging_file = out
            # The inode every hashed byte lands in. The install compares the
            # final name against THIS, not against whatever the staging name
            # resolves to by then.
            verified = _identity(os.fstat(staging_file.fileno()))
            while True:
                chunk = resp.read(chunk_bytes)
                if not chunk:
                    break
                staging_file.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                this_run += len(chunk)
                if downloaded > ceiling:
                    # Recorded and broken out of — NOT unlinked here. The write
                    # handle is still open on this line, and Windows refuses to
                    # unlink an open file, so the unlink would raise WinError 32
                    # and the transport handler below would answer with that OS
                    # error instead of the ceiling refusal the caller needs.
                    overflow = downloaded
                    break
                if on_progress is not None and this_run % progress_every_bytes < chunk_bytes:
                    on_progress(downloaded, total)
                if rate_limit_bytes_per_s > 0:
                    owed = this_run / rate_limit_bytes_per_s - (time.monotonic() - started)
                    if owed > 0:
                        time.sleep(owed)
            staging_file.flush()
            if not platform_compat.IS_POSIX:
                # Windows: the rename and every unlink below need the handle closed.
                staging_file.close()
            if overflow:
                where.unlink(staging_name)
                return False, (
                    f"{_ERROR_PREFIX}: body longer than the {ceiling}-byte "
                    f"ceiling (got {overflow})"
                )
        if on_verifying is not None:
            on_verifying()
        got = digest.hexdigest()
        if got != sha256:
            where.unlink(staging_name)
            return False, (
                f"sha256 mismatch: got {got[:16]}…, expected {sha256[:16]}… (corrupt download)"
            )
        # The size is what was written, counted -- not a stat of a name.
        if min_bytes and downloaded < min_bytes:
            where.unlink(staging_name)
            return False, f"downloaded file too small ({downloaded} bytes)"
        _install(
            where, staging_name, path.name, verified=verified, restrict_to_owner=restrict_to_owner
        )
        return True, ""
    except StagingRefused as exc:
        # Message kept verbatim: it names a local path, never the url. Listed BEFORE
        # the transport branch because it is an OSError and would otherwise be
        # reduced to "OSError", losing the one thing a reader needs to know.
        if not resume:
            _discard(where, staging_name)
        return False, f"{_ERROR_PREFIX}: {exc}"
    except RedirectRefused as exc:
        # Also verbatim, and also listed before the transport branch: the reason
        # carries two hostnames and the policy, never a path or a credential, and
        # it is the one diagnostic an operator with a redirecting mirror needs —
        # reduced to "HTTPError" it would read as a broken CDN.
        if not resume:
            _discard(where, staging_name)
        return False, f"{_ERROR_PREFIX}: {exc.reason}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if not resume:
            _discard(where, staging_name)
        # TYPE and a redacted url, never `exc`. This string is RETURNED, and the
        # feature-video caller puts it in the cache's failure state, which /status
        # serves to the dashboard — so an unredacted url here is a wider leak than a
        # log line. `http.client.InvalidURL` carries the url verbatim in its message
        # ("nonnumeric port: 'secretpw@…'"), which is how a credentialed override
        # reaches a reader. The type is the diagnostic that matters anyway.
        return False, f"{_ERROR_PREFIX}: {type(exc).__name__} from {redact_url(url)}"
    except Exception as exc:
        # A resumable partial is KEPT on a transport failure — that is the whole
        # point of resume — but discarded for a non-resuming caller so a stale
        # prefix cannot be mistaken for a fresh attempt.
        if not resume:
            _discard(where, staging_name)
        # No `exc_info=True`: a traceback renders the exception's own `str()`, which
        # for `InvalidURL` is the credential-bearing url this line exists to keep
        # out. Redacting the message while dumping the traceback beside it would
        # relocate the leak, not close it. The catch-all is where url-bearing
        # exceptions land when they are not one of the three types above.
        logger.warning("%s download from %s failed: %s", label, redact_url(url), type(exc).__name__)
        return False, f"{_ERROR_PREFIX}: {type(exc).__name__} from {redact_url(url)}"
    finally:
        if out is not None:
            out.close()


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "StagingRefused",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_PROGRESS_EVERY_BYTES",
    "DEFAULT_TIMEOUT_SECS",
    "PART_SUFFIX",
    "build_opener",
    "download_to",
    "make_ssl_context",
    "redact_url",
]
