"""What an archive is, measured from local bytes and recorded values.

:func:`_body_fingerprint` pins the bytes an install sent and the bytes a restore
received. :func:`_tree_fingerprint` pins what an archive CARRIES, so an unchanged run
can be recognised although its archive bytes differ. :func:`_is_provable_version_id`
decides whether a recorded S3 version id names exactly one stored object.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import stat
import tarfile
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import KEY_SEP

logger = logging.getLogger(_FACADE_MODULE)


def _body_fingerprint(path: Path | None = None, *, fd: int | None = None) -> str:
    """The MD5 of a file's bytes.

    Recording a KEY proves this install wrote something at that path; it does not
    prove the object sitting there NOW is that something. A key is a name, and
    anyone who can write to the bucket can write to a name -- so on a shared drive
    a co-writer can overwrite an archive after it was recorded, and a check that
    only matched keys would hand back their bytes as ours.

    A fingerprint closes that, and it closes it WITHOUT depending on anything S3
    reports. This is taken twice over local bytes: once over the file this install
    uploads, and once over the file a restore has finished downloading. The restore
    compares the two. No object metadata is read, so there is no ETag to reason
    about -- which also means no multipart or encryption-mode caveat, because S3's
    ETag stops equalling the body MD5 under multipart and under SSE-KMS, and this
    comparison never consults it either way.

    *fd* takes the bytes from an open descriptor rather than from *path*. The push
    paths pass it so that the archive they fingerprint is provably the archive they
    upload: taken from a name, this and the upload would be two resolutions of one
    string, and a same-UID process that replaced the file between them would leave
    a record describing bytes the object does not hold. Reading at
    an explicit offset leaves the descriptor's own position alone, so the caller
    can hand the same descriptor to another reader. The restore side still passes a
    path, because there the file it hashes is one it created and holds exclusively.

    ``usedforsecurity=False`` because this is not a security digest: it detects an
    overwrite between two points in this install's own timeline. It is passed so the
    call still works where a hardened build refuses MD5 by default.
    """
    digest = hashlib.md5(usedforsecurity=False)  # noqa: S324 -- content hash, not a security digest
    if fd is not None:
        offset = 0
        while True:
            chunk = _read_at(fd, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        return digest.hexdigest()
    if path is None:
        raise TypeError("_body_fingerprint needs either a path or a descriptor")
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_at(fd: int, size: int, offset: int) -> bytes:
    """Read up to *size* bytes of *fd* starting at *offset*.

    The point of addressing by offset is that the caller's descriptor position is
    left where the caller put it, so one descriptor can feed two fingerprints and
    then the upload. ``os.pread`` does that in one syscall and exists only where
    the platform provides it; Windows has no such call.

    Where it is missing, the position is read, moved, and put back in a
    ``finally``, which reaches the same outcome for the single-threaded staging
    path that calls this: no other reader shares the descriptor while a build is
    in flight. A caller that did share one across threads would need the real
    ``pread``, and that is the platform where it exists.
    """
    pread = getattr(os, "pread", None)
    if pread is not None:
        return pread(fd, size, offset)
    keep = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, size)
    finally:
        os.lseek(fd, keep, os.SEEK_SET)


#: The snapshot bundle's metadata member, named relative to the bundle root.
_SNAPSHOT_MANIFEST_NAME = "MANIFEST.json"


#: Manifest fields that change on every build of an UNCHANGED tree, and so must not
#: reach :func:`_tree_fingerprint`.
#:
#: Measured, not assumed: two bundles built from one untouched home differ in exactly
#: one member (``MANIFEST.json``) and inside it in exactly one field (``created_at``)
#: -- ``snapshot.py`` writes it as ``datetime.now(...)`` beside ``hostname``, ``user``
#: and ``kirocrew_dir``, which are stable on an install and are therefore KEPT. The
#: rest of the manifest is real signal and is compared: ``purpose``, ``staging``
#: (pinned vs unpinned) and ``version`` are not derivable from the file set at all, so
#: dropping the whole member -- the obvious shortcut -- would silently stop noticing a
#: bundle that switched to an unpinned staging walk.
#:
#: This is an assumption about a module this one does not own, so a test pins the
#: assumption itself rather than only the behaviour: it builds two bundles from one
#: unchanged tree and asserts the fingerprints match. The day a second volatile field
#: appears, that test goes red instead of the skip quietly never firing again.
_VOLATILE_MANIFEST_FIELDS = ("created_at",)


def _tree_fingerprint(
    archive: Path | None = None, *, volatile_root: bool, fd: int | None = None
) -> str:
    """A digest of what an archive CARRIES, stable across two builds of one tree.

    This is the value the unchanged-check compares, and it exists because
    :func:`_body_fingerprint` cannot answer the question. A ``tar.gz`` embeds a
    per-entry mtime and a gzip header stamp, so two runs over a byte-identical tree
    produce different archive bytes -- measured, and pinned by a test. An
    archive-level comparison therefore reports "changed" every single night and a skip
    built on it could never fire.

    So the digest is taken over the ENTRY SET instead: for every member, its path,
    its kind, its permission mode, its size and a hash of its bytes, accumulated in
    sorted path order. That is the per-entry source manifest the decision needs, read
    from the packed copy.

    Reading it from the packed copy rather than walking the source again is
    deliberate, and it is the stronger of the two:

    * It cannot drift. A second walk would have to re-derive WHICH paths each kind
      packs -- knowledge that lives in ``snapshot.COMPONENTS`` and in
      :func:`_add_tree` -- and the day the two disagreed, the manifest would answer
      "unchanged" for a tree whose real content had moved. A backup that silently
      stops backing up is a worse failure than a local rebuild.
    * It sees redaction. The snapshot path may upload a redacted copy
      (:func:`snapshot.prepare_redacted_copy`), so flipping that switch changes the
      bytes that LEAVE while the source tree is untouched. Taken over the payload,
      this notices; taken over the source, it would not.

    ``mtime`` is deliberately NOT part of the digest even though a source manifest
    conventionally carries it. It is not needed -- a content change moves the content
    hash -- and it is actively harmful here: a restore, a ``touch``, or a checkout
    bumps mtime without changing a byte, and the resulting "changed" verdict would
    spend the full upload this check exists to avoid. (``MANIFEST.json``'s mtime is
    also rewritten on every build, measured.)

    *volatile_root* strips the first path segment from every member. The snapshot
    bundle's root directory is named ``kirocrew-snapshot-<stamp>``, so it changes every
    run and would defeat the comparison on its own; the bundle has exactly one root,
    which ``snapshot._redacted_upload_copy`` already depends on and enforces. The
    sessions archive is the opposite case -- its roots are ``crew`` and ``cli``, which
    are meaningful -- so its caller passes ``False`` and nothing is stripped. Each
    caller states what it knows about its own archive rather than this guessing from a
    name pattern.

    ``usedforsecurity`` is not passed: unlike :func:`_body_fingerprint` this is
    SHA-256, which no hardened build refuses.

    *fd* reads the entry set from an open descriptor rather than from *archive*'s
    name, for the reason :func:`_body_fingerprint` gives: this digest is what
    decides whether to upload at all, so it must describe the file that is then
    uploaded rather than a second resolution of the same string.

    Returns ``""`` when the archive cannot be read as a ``tar.gz`` at all, and an empty
    value never matches anything, so the run uploads. This function deliberately does
    NOT turn an unreadable payload into a refusal: validating the archive is a separate
    question from deciding whether to send it, and raising here would decide the first
    one on the way past. An unreadable payload is pushed, exactly as a payload this
    cannot read has to be; the skip is the only thing unavailable for it.
    """
    label = archive.name if archive is not None else "the staged archive"
    try:
        entries = _archive_entries(archive, volatile_root=volatile_root, fd=fd)
    except (tarfile.TarError, OSError) as exc:
        logger.warning(
            "aws-control: could not read %s to decide whether anything changed, so this "
            "run uploads rather than skipping: %s",
            label,
            exc,
        )
        return ""
    rolling = hashlib.sha256()
    for kind, name, mode, size, digest in sorted(entries, key=lambda row: (row[1], row[0])):
        # NUL-delimited, and injective because no field can contain a NUL: tar stores
        # member names NUL-terminated, kind is one of a fixed set of literals, and the
        # mode, size and digest are octal, decimal and hex digits. So no two different
        # entry sets serialize alike.
        # Mode only splits otherwise-matching rows: it can trigger an extra upload,
        # never a wrong skip.
        # Tar member names are surrogate-escaped, and surrogatepass is total for every
        # lone surrogate. A strict encode raises here, outside the guarded archive read,
        # and crashes the run instead of falling back to upload.
        rolling.update(
            f"{kind}\0{name}\0{mode:o}\0{size}\0{digest}\0".encode("utf-8", "surrogatepass")
        )
    return rolling.hexdigest()


class _OffsetReader(io.RawIOBase):
    """A read-only file object over a descriptor, addressing bytes by OFFSET.

    ``os.dup`` is the obvious way to read a descriptor twice and it is wrong here:
    a duplicate SHARES the file offset with the original, so reading the archive
    through one would leave the caller's descriptor positioned at the end -- and
    the push paths hand that same descriptor to the upload afterwards. Depending on
    a re-open of ``/dev/stdin`` to reset it would be depending on a platform
    detail: Linux gives a fresh file description, a character-device spelling need
    not.

    :func:`_read_at` takes the offset per call, so this keeps its own position and
    the caller's descriptor is left where the caller put it.
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        chunk = _read_at(self._fd, len(buffer), self._pos)
        buffer[: len(chunk)] = chunk
        self._pos += len(chunk)
        return len(chunk)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            self._pos = offset
        elif whence == os.SEEK_CUR:
            self._pos += offset
        elif whence == os.SEEK_END:
            self._pos = os.fstat(self._fd).st_size + offset
        else:
            raise ValueError(f"unsupported whence: {whence}")
        return self._pos

    def tell(self) -> int:
        return self._pos


def _archive_entries(
    archive: Path | None = None, *, volatile_root: bool, fd: int | None = None
) -> list[tuple[str, str, int, int, str]]:
    """One ``(kind, path, permission mode, size, content digest)`` row per member.

    *fd* is read through :class:`_OffsetReader`, so the entry set digested here is
    the entry set of the file that is then uploaded -- no name is resolved in
    between -- and the caller's descriptor position is left exactly where it was.
    """
    if fd is not None:
        with io.BufferedReader(_OffsetReader(fd)) as raw:
            with tarfile.open(fileobj=raw, mode="r:gz") as tar:
                return _entries_of(tar, volatile_root=volatile_root)
    if archive is None:
        raise TypeError("_archive_entries needs either a path or a descriptor")
    with tarfile.open(archive, "r:gz") as tar:
        return _entries_of(tar, volatile_root=volatile_root)


def _entries_of(
    tar: tarfile.TarFile, *, volatile_root: bool
) -> list[tuple[str, str, int, int, str]]:
    """The entry rows of an already-open archive."""
    entries: list[tuple[str, str, int, int, str]] = []
    for member in tar:
        name = member.name
        if volatile_root:
            # A member that IS the root directory normalizes to an empty name and
            # carries nothing; dropping it keeps the digest about content.
            rest = name.partition(KEY_SEP)[2]
            if not rest:
                continue
            name = rest
        mode = stat.S_IMODE(member.mode)
        if not member.isfile():
            # Recorded by name, kind and mode. An empty directory is not visible
            # in any file's path, so a tree that loses one is a change this would
            # otherwise miss.
            entries.append(("dir" if member.isdir() else "other", name, mode, 0, ""))
            continue
        handle = tar.extractfile(member)
        if handle is None:
            entries.append(("unreadable", name, mode, member.size, ""))
            continue
        if name == _SNAPSHOT_MANIFEST_NAME:
            entries.append(("file", name, mode, 0, _manifest_digest(handle.read())))
            continue
        member_hash = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            member_hash.update(chunk)
        entries.append(("file", name, mode, member.size, member_hash.hexdigest()))
    return entries


def _manifest_digest(raw: bytes) -> str:
    """A digest of the snapshot manifest with its volatile fields dropped.

    Falls back to hashing the raw bytes when the member is not the JSON object this
    expects. That direction is the safe one: an unparseable manifest then reads as
    "changed" and the run uploads, rather than a parse failure becoming a silent
    match. See :data:`_VOLATILE_MANIFEST_FIELDS`.
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return hashlib.sha256(raw).hexdigest()
    if not isinstance(parsed, dict):
        return hashlib.sha256(raw).hexdigest()
    stable = {k: v for k, v in parsed.items() if k not in _VOLATILE_MANIFEST_FIELDS}
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _is_provable_version_id(value: Any) -> bool:
    """Whether *value* identifies ONE stored object version.

    An empty id names nothing. The string ``"null"`` names something, but not one
    thing: S3 gives that id to every object written to a key while the bucket's
    versioning is SUSPENDED, and an overwrite there REPLACES that version rather than
    adding one. So two different bodies at one key both report ``"null"``, and
    comparing a recorded id against a stored one cannot tell them apart -- which is
    exactly the question the comparison exists to answer.

    Both sides of that comparison run through here, so neither can be proven alone.
    """
    return isinstance(value, str) and bool(value) and value != "null"
