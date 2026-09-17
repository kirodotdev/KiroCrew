"""Part-level OOXML container access with byte-preserving write-back.

An OOXML file is a zip of *parts*. The fidelity rule this engine keeps is: an
edit rewrites ONLY the parts it changed, and every other part is copied through
**byte-for-byte** — same bytes, same compression type, same order. That is what
lets a targeted edit (change one paragraph's text) leave the theme, styles,
media, custom XML and relationships of a real document exactly as the authoring
application wrote them, instead of the lossy "parse the whole thing and
re-serialize" round-trip a full-document library would do.

Reads go through :func:`read_part`, hardened by ``kiro_crew.zip_vet`` (declared
inventory bound) and a real decompressed-size cap, and parsed with
``defusedxml`` so a crafted part cannot mount an XXE. Writes go through
:func:`rewrite_parts`, which is atomic (temp file + ``os.replace``) so a failed
or interrupted write never truncates the destination in place.
"""

from __future__ import annotations

import os
import stat
import tempfile
import zipfile

try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ModuleNotFoundError:  # pragma: no cover - exercised via monkeypatch
    _xml_fromstring = None  # type: ignore[assignment]

from kiro_crew.atomic_write import _is_access_control_xattr
from kiro_crew.security import is_sensitive_path
from kiro_crew.zip_vet import ZipInventoryRejected, vet_zip_inventory

from . import constants as C
from .errors import DocumentEditError, MalformedDocument, OfficeDocumentError
from .rejection import _is_remote_path

__all__ = [
    "read_part",
    "part_names",
    "parse_xml_part",
    "rewrite_parts",
]


def _guard_sensitive(path: str) -> None:
    """Refuse a sensitive OR remote path before any read/write touches it.

    Two independent screens, both string-only (no network I/O in the check):

    * ``is_sensitive_path`` — the repo's credential-home / data-home guard.
    * ``_is_remote_path`` — a UNC (``\\\\server\\share`` / ``//server/share``) or
      url-scheme (``smb://`` …) path. This matters as much on the WRITE path as
      the read path: ``rewrite_parts`` does ``mkstemp(dir=<dst_dir>)`` +
      ``os.replace`` into ``dst_path``, so a remote ``dst_path`` would perform an
      outbound SMB/NTLM write on Windows and leak the user's NTLM hash. The
      read path already refuses remote sources; screening here refuses a remote
      DESTINATION identically, closing that asymmetry.
    """
    if is_sensitive_path(path):
        raise OfficeDocumentError(
            f"refusing to read sensitive path: {path}", reason="sensitive_path"
        )
    if _is_remote_path(path):
        raise OfficeDocumentError(
            f"refusing a remote (UNC/url-scheme) path: {path}", reason="sensitive_path"
        )


def _open_vetted(path: str) -> zipfile.ZipFile:
    """Open *path* as a zip after bounding its declared inventory.

    The vet runs BEFORE ``ZipFile`` is constructed because construction
    allocates from the declared central-directory size; see ``zip_vet``.

    The open is descriptor-pinned with ``O_NOFOLLOW`` rather than by pathname:
    ``_guard_sensitive`` screens the NAME, but a caller-controlled symlink
    swapped in at that name AFTER the screen would otherwise be followed into a
    protected file (a TOCTOU). ``O_NOFOLLOW`` refuses a symlinked final
    component at open time, so the inode read is the one the name denoted when
    the guard ran, not one substituted afterwards. (Absent on Windows, where it
    reads as 0 — defense-in-depth there, load-bearing on POSIX.)
    """
    try:
        vet_zip_inventory(path, max_members=C.MAX_ARCHIVE_MEMBERS)
    except ZipInventoryRejected as exc:
        raise MalformedDocument(f"archive inventory rejected: {exc.reason}") from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise MalformedDocument(f"cannot open container: {exc}") from exc
    try:
        # ZipFile takes ownership of the file object and closes it (and thus the
        # fd) when the ZipFile itself is closed.
        fh = os.fdopen(fd, "rb")
    except OSError as exc:
        os.close(fd)
        raise MalformedDocument(f"cannot open container: {exc}") from exc
    try:
        return zipfile.ZipFile(fh, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        fh.close()
        raise MalformedDocument(f"cannot open container: {exc}") from exc


def _read_member_bounded(zf: zipfile.ZipFile, name: str, max_size: int) -> bytes:
    """Read one member's decompressed bytes, refusing a member over *max_size*.

    A container's central directory can be tiny while a single member's deflate
    stream expands enormously (a "zip bomb"): the inventory vet bounds member
    COUNT and central-directory bytes, never decompressed part size, so the cap
    must be enforced at the point bytes are actually inflated. Reads one byte
    past the cap and rejects if that byte exists, so the whole member is never
    materialised when it is oversized.

    A member with a bad CRC or a corrupt deflate stream raises ``BadZipFile`` /
    ``OSError`` from the read; those are translated to :class:`MalformedDocument`
    so a corrupt part never escapes the engine's typed error boundary as a raw
    zip/OS exception a caller catching ``OfficeDocumentError`` would miss.
    """
    try:
        with zf.open(name) as fh:
            data = fh.read(max_size + 1)
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise MalformedDocument(f"part {name!r} could not be read: {exc}") from exc
    if len(data) > max_size:
        raise MalformedDocument(f"part {name!r} exceeds {max_size} bytes decompressed")
    return data


def _infolist_no_duplicates(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Return the members, rejecting a container that names one part twice.

    A zip may physically hold two entries with the same name; a verbatim copy
    would emit both and a reader picks the last, so a duplicate name is a way to
    smuggle content past a check that inspected only the first. Refuse it rather
    than propagate the ambiguity.
    """
    infos = zf.infolist()
    seen: set[str] = set()
    for info in infos:
        if info.filename in seen:
            raise MalformedDocument(f"container names part {info.filename!r} more than once")
        seen.add(info.filename)
    return infos


def part_names(path: str) -> list[str]:
    """Return the container's member names in stored order."""
    _guard_sensitive(path)
    with _open_vetted(path) as zf:
        return zf.namelist()


def read_part(path: str, part: str, *, max_size: int | None = None) -> bytes:
    """Read one part's decompressed bytes, capped at *max_size*.

    Raises :class:`MalformedDocument` if the part is absent or its real
    decompressed size exceeds the cap, regardless of what the zip header
    declares (defends against a lying header / zip bomb).
    """
    _guard_sensitive(path)
    if max_size is None:
        max_size = C.MAX_PART_BYTES
    with _open_vetted(path) as zf:
        if part not in zf.namelist():
            raise MalformedDocument(f"container has no part {part!r}")
        return _read_member_bounded(zf, part, max_size)


def parse_xml_part(path: str, part: str):
    """Read and XML-parse one part with a hardened (XXE-safe) parser.

    Returns the parsed root ``Element``. Raises :class:`MalformedDocument` on
    an unparseable part or when the hardened parser is unavailable (a stale
    install), never falling back to the entity-resolving stdlib parser.
    """
    if _xml_fromstring is None:
        raise MalformedDocument(
            "defusedxml is not installed; refusing to parse OOXML with the "
            "entity-resolving stdlib parser (run: pip install -e .)"
        )
    data = read_part(path, part)
    try:
        return _xml_fromstring(data)
    except Exception as exc:  # defusedxml raises several distinct types
        raise MalformedDocument(f"part {part!r} is not well-formed XML: {exc}") from exc


def rewrite_parts(
    src_path: str,
    dst_path: str,
    replacements: dict[str, bytes],
    *,
    expect_source_signature: "tuple[int, int] | None" = None,
) -> None:
    """Write *dst_path* as *src_path* with *replacements* substituted per part.

    ``replacements`` maps a part name that MUST already exist in the source to
    its new bytes. Every part not named is copied through byte-for-byte: same
    compression type, same order, so the theme, styles, media and relationships
    of a real document survive a targeted edit exactly as the producer wrote
    them. A replacement naming a nonexistent part is a :class:`MalformedDocument`
    — the caller asked to change something that is not there.

    Each untouched member is copied through a decompressed-size cap
    (:data:`constants.MAX_PART_BYTES`): the byte-preserving copy inflates the
    member, so a high-ratio "zip bomb" member would otherwise exhaust memory on
    the copy path even though ``classify`` and the inventory vet passed. A
    container that names one part twice is refused (:func:`_infolist_no_duplicates`).

    Atomic: the whole archive is built in a temp file in the destination
    directory and swapped in with ``os.replace`` only once fully written, so an
    error mid-write never leaves a truncated destination. ``src_path`` and
    ``dst_path`` may be the same file; the swap makes in-place edit safe.
    """
    _guard_sensitive(src_path)
    _guard_sensitive(dst_path)

    dst_dir = os.path.dirname(os.path.abspath(dst_path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".ooxml-", suffix=".tmp", dir=dst_dir)
    os.close(fd)
    try:
        with _open_vetted(src_path) as zsrc:
            infos = _infolist_no_duplicates(zsrc)
            existing = {info.filename for info in infos}
            missing = [p for p in replacements if p not in existing]
            if missing:
                raise MalformedDocument(
                    f"cannot replace part(s) absent from source: " f"{', '.join(sorted(missing))}"
                )
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zdst:
                # Preserve original order for untouched parts; emit replacements
                # in their original slot so the central directory order is stable.
                for info in infos:
                    name = info.filename
                    if name in replacements:
                        data = replacements[name]
                    else:
                        data = _read_member_bounded(zsrc, name, C.MAX_PART_BYTES)
                    out = zipfile.ZipInfo(filename=name, date_time=info.date_time)
                    out.compress_type = info.compress_type
                    out.external_attr = info.external_attr
                    out.internal_attr = info.internal_attr
                    out.create_system = info.create_system
                    out.flag_bits = info.flag_bits
                    zdst.writestr(out, data)
        # The source zip is now CLOSED: on Windows an in-place edit (src == dst)
        # would raise a sharing violation if os.replace ran with the handle open.
        # Carry the file mode AND owner/group (and xattrs) of the
        # destination-being-replaced (or, for a new destination, the source)
        # onto the temp so the swap does not downgrade a 0644 document to
        # mkstemp's private 0600, nor silently reassign a group-shared file's
        # group. This operates on the TEMP file and reads the model once; it is
        # done BEFORE the final signature check so that check is the last thing
        # before os.replace.
        _carry_file_metadata(dst_path if os.path.exists(dst_path) else src_path, tmp)
        # F3 (concurrent-writer pin): revalidate the source signature as the
        # LAST step before publishing, so the only unguarded window is the
        # os.replace syscall itself. The metadata copy above (which reads and
        # stats the model, a non-trivial amount of work) runs BEFORE this check,
        # keeping it out of the check-to-swap window. A writer who changed src
        # between plan and here is caught and the edit aborts, destination
        # untouched.
        #
        # Residual, stated honestly: stdlib has no atomic compare-and-swap that
        # rebinds a path only if its inode is unchanged (no renameat2 with a
        # content precondition), so a write landing in the microsecond between
        # this stat and os.replace cannot be closed without OS-level advisory
        # locking (flock/LockFileEx) — a portability and cross-writer-cooperation
        # cost the readers/editors do not otherwise pay, and Office autosave does
        # not take that lock anyway. The (size, mtime_ns) pin checked at the last
        # possible instant is the honest floor here.
        if expect_source_signature is not None:
            current = source_signature(src_path)
            if current != expect_source_signature:
                # A concurrent-writer race is an environmental WRITE-TIME
                # failure, not a broken container: the source is a perfectly
                # valid document that simply changed under us. Raise
                # DocumentEditError (reason=source_changed) so a caller
                # branching on .reason does not misread a benign autosave race
                # as malformed_document. The destination is untouched.
                raise DocumentEditError(
                    "source changed between read and write "
                    f"(expected {expect_source_signature}, saw {current}); "
                    "aborting to avoid overwriting a concurrent edit",
                    reason="source_changed",
                )
        os.replace(tmp, dst_path)
    except BaseException:
        # Never leave the temp artifact behind on any failure path.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def source_signature(path: str) -> tuple[int, int] | None:
    """Return a cheap change-signature ``(size, mtime_ns)`` for *path*, or None.

    Pins a source file between the moment an editor reads the part it will
    change and the moment :func:`rewrite_parts` republishes, so a concurrent
    writer's change is detected rather than silently overwritten. ``None`` when
    the file cannot be stat'd (it is revalidated, not trusted).
    """
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _carry_file_metadata(model_path: str, target_path: str) -> None:
    """Copy *model_path*'s access metadata onto *target_path*, or refuse.

    ``mkstemp`` creates a private 0600 file owned by the running user; without
    this the atomic swap would tighten a normal 0644 document's permissions and,
    on a group-shared file, silently reassign its group. Carried, in order:

    * **mode bits** and **owner/group** — best effort: ``chmod`` almost always
      succeeds; ``chown`` to a different owner needs privilege, so a failure
      there leaves the running user's ownership rather than failing the edit.
    * **extended attributes** (xattrs, Linux ``os.*xattr``) — copied when the
      platform exposes them.

    Fails CLOSED on unreproducible ACCESS-CONTROL metadata: if the model file
    carries a POSIX ACL (a ``system.posix_acl_access`` xattr) or any xattr the
    running process cannot re-set on the target, this raises
    :class:`DocumentEditError` (``reason=metadata_not_carried``) BEFORE the
    caller's ``os.replace`` runs, so the edit aborts with the destination
    untouched rather than silently publishing a
    replacement that has quietly lost an access restriction. A file with no such
    metadata (the common case) carries cleanly and edits normally.
    """
    try:
        st = os.stat(model_path)
    except OSError:
        return
    # chown BEFORE chmod: on Linux, os.chown clears the S_ISUID/S_ISGID bits, so
    # running chmod LAST re-establishes the full mode (including any setuid/setgid
    # bits the model carried). Doing chmod first and chown second would silently
    # strip those special bits — contradicting the "does not downgrade" intent.
    if hasattr(os, "chown"):
        try:
            os.chown(target_path, st.st_uid, st.st_gid)
        except OSError:
            pass
    try:
        os.chmod(target_path, stat.S_IMODE(st.st_mode))
    except OSError:
        pass
    _carry_xattrs_or_refuse(model_path, target_path)


def _carry_xattrs_or_refuse(model_path: str, target_path: str) -> None:
    """Copy the model's xattrs to *target_path*, failing closed only for ACLs.

    On a platform without ``os.listxattr`` (macOS/Windows via this API,
    non-Linux) there is nothing to enumerate, so it is a no-op there.

    The failure policy is scoped to what losing the attribute would actually
    weaken, reusing the repo's canonical judgement
    (:func:`atomic_write._is_access_control_xattr`):

    * an **access-control** xattr (``system.posix_acl_access`` /
      ``system.posix_acl_default``) that cannot be read or re-set is an access
      restriction the swap would drop, so we refuse the edit (fail closed).
    * every OTHER xattr is **best effort**: it is copied when possible and
      SKIPPED when it cannot be read or set. This is deliberate for
      kernel-computed labels like ``security.selinux`` — on an SELinux-enforcing
      host every file carries one, an unprivileged process cannot re-set it, and
      the replacement file's default label is already the correct one, so failing
      the edit would protect nothing while breaking every in-place edit there.
    """
    if not (hasattr(os, "listxattr") and hasattr(os, "getxattr") and hasattr(os, "setxattr")):
        return
    try:
        attrs = os.listxattr(model_path, follow_symlinks=False)
    except OSError:
        # The model has no readable xattr namespace (e.g. an fs without xattr
        # support); nothing to carry and nothing lost.
        return
    for attr in attrs:
        access_control = _is_access_control_xattr(attr)
        try:
            value = os.getxattr(model_path, attr, follow_symlinks=False)
        except OSError as exc:
            if access_control:
                # A listed ACL we cannot read: dropping it could publish a
                # replacement missing an access restriction. Fail closed.
                raise DocumentEditError(
                    f"cannot read listed access-control attribute {attr!r} to "
                    f"carry it onto the replacement: {exc}; aborting so the edit "
                    "does not silently drop an access restriction",
                    reason="metadata_not_carried",
                ) from exc
            # A non-access-control attr (e.g. security.selinux, user.*) that is
            # unreadable: best effort, skip it.
            continue
        try:
            os.setxattr(target_path, attr, value, follow_symlinks=False)
        except OSError as exc:
            if access_control:
                # An access restriction we cannot reproduce on the target.
                raise DocumentEditError(
                    f"cannot carry access-control attribute {attr!r} (a POSIX "
                    f"ACL) onto the replacement: {exc}; aborting so the edit does "
                    "not silently drop an access restriction",
                    reason="metadata_not_carried",
                ) from exc
            # A non-access-control attr the target filesystem/kernel will not
            # accept (security.selinux gets the correct default label anyway):
            # best effort, skip it.
            continue
