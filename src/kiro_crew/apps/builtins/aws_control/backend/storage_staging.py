"""Transfer staging under the data home, and the ranged read a preview stages there."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.aws_control.backend.storage_drive import section_key
from kiro_crew.config.paths import data_home
from kiro_crew.deploy.engine import AWSError, _checked
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.sandbox import (
    carveout_shadowed_by_foreign_mask,
    crew_home_visible_spellings,
    effective_sandbox_mode,
)

#: Gateway-owned transfer staging, a TOP-LEVEL leaf of the data home. Every
#: agent sandbox bind-masks it and the shared file-tool gate refuses it
#: (``sandbox._CREW_HIDDEN_LEAVES`` / ``security._CREW_SECRET_LEAVES`` carry the
#: matching entry -- a test pins the three together, because moving the staging
#: root out of that directory would silently un-fence it). Top-level rather than
#: under ``apps/aws-control/``: a mask covers the leaf, not its ancestors, and an
#: agent-writable ancestor (``apps/``, ``apps/aws-control/``) could be renamed
#: out from under it mid-transfer so the CLI's path resolves through a planted
#: link. At the top level the only ancestors are the data home and ``$HOME``,
#: the same residual every other fenced leaf (the credential staging included)
#: already stands on. It is NOT the app's ``data`` directory either: that one
#: holds the owner-authorization bits and must stay masked from the CLI spawn,
#: whereas this one is exactly what that spawn is granted.
STAGING_DIR_LEAF = "aws-control-staging"

#: Read-back chunk for the staged preview file. The window is a few hundred
#: KB at most, so this is about not asking for one oversized buffer, not about
#: throughput.
_STAGING_READ_CHUNK = 64 * 1024

#: S3's error code for a byte range that starts past the end of the object --
#: the only way a ``bytes=0-N`` range fails, which means the object is empty.
_S3_INVALID_RANGE_CODE = "InvalidRange"


def staging_root() -> Path:
    """The agent-masked root that every AWS Control staging directory is cut under.

    Shared by the preview staging (:func:`_preview_staging_parent`) and by the
    backup archive staging, because both need the same property and there should
    be one place that establishes it: a directory a SIBLING agent cannot reach.
    The system temp directory is not that place -- it is shared, same-UID
    writable, and carries no mask -- so an archive staged there can be rewritten
    in place between being built and being uploaded, and a descriptor pin does not
    help because pinning fixes which inode a name reaches, not that inode's bytes.

    On a sandboxed host the root already exists by the time any agent runs: the
    sandbox materialises it before every namespace spawn
    (``sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES``), because a mask can only bind
    over a name that exists, and a root created lazily here would appear inside
    an already-running sandbox's view. The ``mkdir`` below therefore matters only
    where no sandbox is masking anything (sandbox off, Windows) and is a no-op
    otherwise.

    Guarded the way the backup restore staging is: the directory itself must be
    a real directory -- a link planted at the root would put every staged file
    outside the fence, which no per-file check can see. One function so a test
    can point it at a temp dir.
    """
    base = data_home()
    staging = base / STAGING_DIR_LEAF
    if is_link_or_junction(staging):
        raise ValueError("preview staging directory is not a real directory")
    # No parents=True: the leaf sits directly under the data home, which exists
    # for as long as the gateway does. A missing parent is a real error here,
    # not something to paper over with a freshly minted tree.
    staging.mkdir(exist_ok=True)
    # Re-check after mkdir: exist_ok=True happily accepts a pre-existing link,
    # and resolving both sides is what catches a component swapped higher up.
    if staging.resolve() != (base.resolve() / STAGING_DIR_LEAF):
        raise ValueError("preview staging directory resolves outside the data home")
    if not staging.is_dir():
        raise ValueError("preview staging directory is not a real directory")
    if platform_compat.IS_POSIX:
        platform_compat.chmod_safe(str(staging), 0o700)
    else:
        platform_compat.restrict_dir_to_owner(str(staging))
    return staging


def _preview_staging_parent() -> Path:
    """The root preview staging directories are cut under. See :func:`staging_root`.

    Kept as its own name because the preview path is what the sandbox-mask tests
    address, and because the two callers are otherwise unrelated -- a change to
    where previews stage should not silently move where backups stage.
    """
    return staging_root()


@contextlib.contextmanager
def pinned_staging(prefix: str) -> Iterator[tuple[Path, int]]:
    """Cut a private staging directory under :func:`staging_root`, PIN it, and
    yield ``(path, dir_fd)`` for the scope; release both on exit.

    Every upload body this app stages goes through here, because the pin is the
    whole basis on which :func:`put_file` may hand the AWS CLI a NAME. Where no
    descriptor can be passed to the child -- Windows has no ``/dev/stdin`` -- the
    name is all the child gets, and a name is re-resolved at the child's open. A
    pinned directory cannot be renamed or deleted, and neither can any directory
    above it, so the path the child walks cannot be re-pointed at a planted
    junction between our check and its open.

    Two properties, and a caller needs both:

    * the masked root removes the WRITER -- a sibling agent's namespace has an
      empty directory bound over that leaf, so the body has no name there to
      rewrite in place, which no pin can prevent. Detection is not an
      alternative: a rewrite of the held inode is read by every later check as
      well as by the upload, so the digests and the bytes sent agree with each
      other and the run records a successful upload of a body it never built;
    * the pin fixes the PATH -- on POSIX the descriptor is a resolution root for
      our own opens, and on Windows holding the directory is what blocks the
      rename.

    ``mkdtemp`` for the unique name and the 0700 mode, then
    :func:`platform_compat.pin_directory`, which refuses a link or reparse point
    at the name rather than following it. On exit the descriptor is closed
    before the tree is removed: on Windows the pin is exactly what would make
    the removal fail.

    The staging ROOT is pinned FIRST, before ``mkdtemp`` names a child under it
    -- as :func:`get_object_head_bytes` does. ``mkdtemp(dir=...)`` re-resolves
    the root by name, so without this a watcher that swaps the root for a link
    (or, on Windows, a reparse point to a remote share) between validation and
    this call would have the child created under the planted target. Holding the
    root open refuses a link at its name and blocks its rename for the scope, so
    the child is cut under the directory we checked. The root descriptor is held
    until the tree is removed, released in the same ``finally``.
    """
    root_fd = platform_compat.pin_directory(str(staging_root()))
    try:
        tmp = tempfile.mkdtemp(prefix=prefix, dir=str(staging_root()))
    except BaseException:
        os.close(root_fd)
        raise
    try:
        dir_fd = platform_compat.pin_directory(tmp)
    except BaseException:
        os.close(root_fd)
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    try:
        yield Path(tmp), dir_fd
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)
        os.close(root_fd)
        shutil.rmtree(tmp, ignore_errors=True)


def get_object_head_bytes(
    profile: str,
    region: str,
    bucket: str,
    section: str,
    key: str,
    *,
    account: str,
    max_bytes: int,
) -> tuple[bytes, int]:
    """The first ``max_bytes`` of ``section/key`` plus the object's FULL size.

    Exists for the gateway-proxied text preview: the browser cannot fetch a
    presigned URL itself because the bucket carries no CORS configuration, so
    the gateway reads on its behalf. A ``--range`` bounds the transfer to the
    preview window — S3 answers with the whole object when it is smaller than
    the range, which is the desired behaviour, not an error.

    The full size comes from the same response (``ContentRange``'s total,
    falling back to ``ContentLength``), so the caller can tell a truncated
    preview from a complete one without a second round trip. Owner-pinned
    like every other transfer, for :func:`put_file`'s name-reuse reason.

    The CLI only writes to a path, and a path in a shared temp directory is
    attacker-influenceable: a same-UID process watching that directory can
    swap the file for a link between our create and the CLI's open, and the
    CLI — writing with the gateway's reach — then lands the object bytes on
    whatever the link names. So the file is staged in a fresh private
    directory under :data:`STAGING_DIR_LEAF`, which every agent sandbox masks
    and the shared file-tool gate refuses. That mask would hide the directory
    from the sandboxed CLI as well, so the per-call directory is named in
    ``extra_visible_dirs`` — lifting the mask for this one fixed-argv spawn,
    never for the agent. It is named in EVERY spelling the masks use for the
    crew data home (:func:`sandbox.crew_home_visible_spellings`): the mask list
    carries both ``$HOME``-joined crew-home prefixes as well as the resolved
    ``config_dir()`` path, the lift is decided lexically, and under a symlinked
    ``$HOME`` those are different strings for one directory — so naming only the
    resolved one leaves a surviving mask to bind an empty directory straight back
    over the staged file, and the CLI reports ``ENOENT`` on a path the gateway
    just created.

    Each spelling's staging ROOT is checked before the spawn
    (:func:`sandbox.carveout_shadowed_by_foreign_mask`). That root is the mask
    entry the lift cancels, so the guard's equality rule exempts it and only
    some OTHER masked ancestor refuses — a data home relocated beneath one
    (``KIROCREW_HOME`` under ``~/.gnupg``) would hand this child that whole tree,
    so the transfer is refused instead of run. One shadowed spelling refuses the
    call: the CLI needs every spelling, not a surviving subset. The check is
    skipped only where no mask can exist for reasons that cannot change before
    the spawn — a non-POSIX host, or an ``off`` tier — never on the backend probe,
    whose transient failures are uncached by design and would otherwise skip the
    refusal for a spawn that still applies the lift.

    The mask is a Linux/macOS mechanism; Windows has no sandbox, so there the
    destination is pinned by IDENTITY instead of by hiding, and the pin covers
    the whole path, not just the file. The staging root and then the per-call
    directory are each opened and held (:func:`platform_compat.pin_directory`,
    which refuses a link or reparse point at the name) before anything inside
    them is named: a held directory can be neither renamed nor deleted, nor can
    any directory above it, so the path the CLI writes through cannot be
    re-pointed at a planted junction. Inside it the gateway creates the
    destination itself, exclusively (``O_EXCL`` refuses a name something else
    planted first — a hard link to a sensitive file included) and holds that
    handle open across the CLI call too. After the call the path is re-checked
    against the held file handle (device, inode, link count) and the bytes are
    read back through that handle rather than by reopening the path, so a link
    that appeared anyway is refused rather than followed. The directory is
    removed before returning — nothing of the object outlives the call.
    """
    staging_parent = _preview_staging_parent()
    # Pin the root BEFORE cutting the per-call directory, then pin that
    # directory before naming anything inside it. Each pin refuses a link or
    # reparse point at the name, and on Windows -- where no mask hides the
    # tree -- a pinned directory can be neither renamed nor deleted, and
    # neither can anything above it. So by the time the destination is created
    # below, every component of the path the CLI will write through is held
    # in place: a watcher cannot rename the directory away and plant a
    # junction at its name between our create and the CLI's open.
    root_fd = platform_compat.pin_directory(staging_parent)
    dir_fd = -1
    fd = -1
    tmp_dir = ""
    try:
        tmp_dir = tempfile.mkdtemp(prefix="drive-preview-", dir=str(staging_parent))
        dir_fd = platform_compat.pin_directory(tmp_dir)
        if platform_compat.IS_POSIX:
            platform_compat.chmod_safe(tmp_dir, 0o700)
        else:
            platform_compat.restrict_dir_to_owner(tmp_dir)
        staging_spellings = crew_home_visible_spellings(tmp_dir)
        # Skipped only where no mask can exist, and only on facts that cannot
        # flip between here and the spawn: a non-POSIX host has no sandbox
        # backend at all, and an "off" tier makes ``wrap_argv`` ignore
        # ``extra_visible_dirs`` outright. Deliberately NOT the backend probe:
        # ``detect_backend`` leaves a TRANSIENT "none" uncached on purpose, so a
        # momentary fork failure asked here would skip the refusal while the
        # spawn's own re-probe still applies the lift. A permanent no-backend
        # POSIX host therefore pays a refused preview instead, which is the
        # direction every other rule on this path already fails in.
        if platform_compat.IS_POSIX and effective_sandbox_mode("standard") != "off":
            for spelling in staging_spellings:
                # Asked of the staging ROOT, which is the mask entry this lift
                # cancels and therefore equality-exempt, so only some OTHER masked
                # ancestor refuses. Asking about the per-call dir would refuse on
                # every layout.
                if carveout_shadowed_by_foreign_mask(os.path.dirname(spelling), mode="standard"):
                    raise ValueError(
                        "preview staging sits beneath an independently masked "
                        "directory; carving it out for the CLI would unmask that tree"
                    )
        tmp_path = os.path.join(tmp_dir, "object")
        # Ours, exclusively, before the CLI ever sees the name. A pre-planted
        # entry of any kind fails the create instead of becoming the target.
        # Created RELATIVE to the pinned directory where the platform allows,
        # so even our own open cannot be steered by a re-resolved path.
        create_flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        if os.open in os.supports_dir_fd:
            fd = os.open("object", create_flags, 0o600, dir_fd=dir_fd)
        else:
            fd = os.open(tmp_path, create_flags, 0o600)
        created = os.fstat(fd)
        try:
            out = _checked(
                [
                    "s3api",
                    "get-object",
                    "--bucket",
                    bucket,
                    "--key",
                    section_key(section, key),
                    "--range",
                    f"bytes=0-{max_bytes - 1}",
                    "--expected-bucket-owner",
                    account,
                    "--output",
                    "json",
                    tmp_path,
                ],
                profile,
                action="s3:GetObject",
                timeout=60,
                extra_visible_dirs=staging_spellings,
            )
        except AWSError as exc:
            # A byte range is unsatisfiable against a 0-byte object, and S3
            # says so with 416 InvalidRange rather than an empty body. The
            # file is perfectly readable and simply empty -- an empty object
            # can be created out-of-band by any tool the bucket name reaches --
            # so that one answer is the empty preview, not a failure.
            if _S3_INVALID_RANGE_CODE in str(exc):
                return b"", 0
            raise
        # The CLI wrote through the PATH; the bytes are read through the
        # HANDLE. The two must still be the same file, and that file must
        # have exactly the one name we gave it.
        landed = os.stat(tmp_path)
        if (landed.st_dev, landed.st_ino) != (created.st_dev, created.st_ino):
            raise ValueError("preview staging file was replaced during the transfer")
        if landed.st_nlink != 1:
            raise ValueError("preview staging file has been linked elsewhere")
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, _STAGING_READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        # Handles go before the rmtree: on Windows the pins are exactly what
        # would make the removal fail.
        for handle in (fd, dir_fd, root_fd):
            if handle >= 0:
                os.close(handle)
        # The preview must not fail over a leftover staging directory.
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        meta = json.loads(out or "{}") or {}
    except json.JSONDecodeError:
        meta = {}
    size = 0
    content_range = str(meta.get("ContentRange", ""))
    if "/" in content_range:
        try:
            size = int(content_range.rsplit("/", 1)[1])
        except ValueError:
            size = 0
    if not size:
        size = int(meta.get("ContentLength", 0) or 0)
    # A garbled response must not report a shorter object than the bytes in
    # hand — that would read as "not truncated" on a truncated preview.
    return data, max(size, len(data))
