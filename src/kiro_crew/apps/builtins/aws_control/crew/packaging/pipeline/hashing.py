"""The builder's content hashes: the skill pin, that pin over a staged copy, the bundle digest.

All three values are frozen. ``bundle_digest`` is byte-for-byte
``crew_export/bundle.py:_bundle_digest``, and ``_tree_hash`` / ``_staged_tree_hash`` share one
row encoding, so a clean copy reproduces the reviewed pin exactly and any byte that ships
unreviewed changes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from . import pinned as _pinned
from .contract import _MAX_PROMPT_BYTES, ExportRefused


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _staged_tree_hash(staged_dir: Path, source_dir: Path, written: "set[str]") -> str:
    """``_tree_hash`` of the staged copy, restated in the SOURCE's terms.

    The pin was taken by ``_tree_hash`` over every file in the source. The copy does
    not ship every source file. Two dispositions are distinct and only one produces a
    gap this hash must reconcile. A file ``_copy_skill`` cannot decode as UTF-8 (a file
    it cannot scan cannot be certified clean) is REFUSED outright -- the build stops, so
    it never reaches this hash. What the copy legitimately omits is different: a source
    file the selection did not pick up (a subtree with no selected ``SKILL.md`` of its
    own) is skipped, so it is in the source ``_tree_hash`` but not in staging. Hashing
    the staged directory alone therefore can never be assumed equal to the pin, and
    comparing them directly would refuse a legitimate skill that carries such an omission.

    So the rows are built from the staged bytes where a file shipped, and from the
    SOURCE bytes only for the source files the copy legitimately omitted. The security
    property is preserved where it matters: every file whose bytes reach the bundle is
    hashed from the copy that reaches it, so a mid-copy rewrite of a shipped file
    changes this value. A rewrite of an OMITTED file is not covered, and cannot matter,
    because those bytes are not in the artifact.

    A path that exists in STAGING but not in the source ships bytes no reviewer approved.
    The verification set is therefore derived from what will actually ship: after the
    source-keyed rows, every staged file with no source counterpart contributes its own
    row, so a staged-only injection changes this value and the caller's pin comparison
    refuses it. This does not break the equality the pin needs, because a legitimate copy
    is a SUBSET of the source (``_copy_skill`` only ever writes source-derived files and
    omits some) -- so a clean build produces zero staged-only rows and still equals
    ``_tree_hash(source)``. The intentional omissions run the other way (source files the
    copy did not select), and those are covered by the source-keyed rows above, not here.
    """

    rows: list[list[str]] = []
    source_rels: set[str] = set()
    for p in _pinned._walk_no_reparse(source_dir):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(source_dir).as_posix()
        source_rels.add(rel)
        shipped = staged_dir / rel
        if _pinned._is_redirecting_entry(shipped):
            # The write is no-follow, but the READ here is a separate window: a staged leaf
            # swapped to a symlink after it was written would be hashed THROUGH the link
            # (``is_file``/``read_bytes`` both follow), pinning the link target's bytes as the
            # reviewed content while a different object ships. Reject the redirect at final
            # hashing so the pin is taken over the object that was written, not one substituted
            # under its name.
            raise ExportRefused(
                f"the staged file {rel} is a link or junction at hashing time; it was "
                f"redirected after this build wrote it. Refusing rather than pin the bytes of "
                f"whatever it now points at. Re-run the build."
            )
        if shipped.is_file():
            # Read the staged leaf through the whole-window no-follow reader, not
            # ``read_bytes`` (which follows a link). A staged file swapped to a link after it
            # was written would otherwise be hashed THROUGH the link, pinning the target's
            # bytes as the shipped content. ``None`` means the leaf is a link/junction or torn
            # at read time -- a staged tree that changed after this build wrote it, refused
            # rather than counted.
            data = _pinned._read_bytes_openat(staged_dir, Path(rel))
            if data is None:
                raise ExportRefused(
                    f"the staged file {rel} is a link or junction, or changed, at hashing "
                    f"time; it was redirected after this build wrote it. Refusing rather than "
                    f"pin the bytes of whatever it now points at. Re-run the build."
                )
            rows.append([rel, _sha(data)])
        elif rel in written:
            # ``_copy_skill`` WROTE this file, and it is gone from staging now -- removed or
            # replaced between the write and this read-back. That is a torn staged tree, not a
            # reviewed state, so it is REFUSED. Falling back to the source bytes here (which is
            # correct only for a file the copy never wrote) would hash what SHOULD have shipped
            # rather than what did, counting the disappearance as reviewed. "I wrote it" is a
            # cached assumption with a window under it.
            raise ExportRefused(
                f"the staged file {rel} was written by this build and is now missing from the "
                f"staged tree; it changed after it was written. Refusing rather than count the "
                f"absence as reviewed. Re-run the build."
            )
        else:
            # A source file the copy legitimately did NOT stage -- it belongs to an unselected
            # nested skill. The pin (``_tree_hash`` over the whole source) still covers it, so
            # its source bytes keep the equality; its bytes are not in the artifact, so a source
            # change to it cannot matter. This is the ONLY legitimate not-staged case now that
            # ``_copy_skill`` refuses (never silently drops) an unscannable file. Read no-follow
            # through the whole-window reader like every other read here: a source leaf swapped
            # to a link between the walk and the read is refused, not hashed through.
            data = _pinned._read_bytes_openat(source_dir, Path(rel))
            if data is None:
                raise ExportRefused(
                    f"the source file {rel} is a link or junction, or changed, at hashing "
                    f"time. Refusing rather than fold in the bytes of whatever it now points "
                    f"at. Re-run the build."
                )
            rows.append([rel, _sha(data)])
    # Staged-only files: present in what ships, absent from the reviewed source. A clean
    # copy has none (staging is a subset of source), so this adds nothing to a legitimate
    # build's hash and the pin equality holds; an added-then-removed mid-copy file leaves a
    # staged path with no source row, which lands here and breaks the equality so the build
    # refuses. This walks the SHIPPING tree, so an entry that cannot be hashed is REFUSED, not
    # skipped: passing over a redirect or a special file leaves shipping content out of the
    # hash meant to cover it -- the same subset-of-what-ships hole the bundle digest closes.
    # Only a genuine directory is skipped (its children are walked; it has no bytes).
    for p in _pinned._walk_no_reparse(staged_dir):
        rel = p.relative_to(staged_dir).as_posix()
        if _pinned._is_redirecting_entry(p):
            raise ExportRefused(
                f"the staged file {rel} is a link or junction at hashing time; it was "
                f"redirected after this build wrote it. Refusing rather than leave a redirect "
                f"out of the tree hash. Re-run the build."
            )
        if p.is_dir():
            continue
        if not p.is_file():
            raise ExportRefused(
                f"the staged entry {rel} is not a regular file (a special file), so it cannot "
                f"be hashed; refusing rather than leave shipping content out of the tree hash. "
                f"Re-run the build."
            )
        if rel not in source_rels:
            # Staged-only content SHIPS, so it is read no-follow through the whole-window
            # reader, and a leaf that cannot be read as a regular in-tree file is REFUSED, not
            # skipped -- a skipped shipping file is exactly the subset-of-what-ships hole this
            # loop exists to close.
            data = _pinned._read_bytes_openat(staged_dir, Path(rel))
            if data is None:
                raise ExportRefused(
                    f"the staged file {rel} is a link or junction, or changed, at hashing "
                    f"time. Refusing rather than leave a redirect out of the tree hash. "
                    f"Re-run the build."
                )
            rows.append(["staged-only:" + rel, _sha(data)])
    return _sha(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _tree_hash(root: Path) -> str:
    """A content hash over every file in a directory, path-and-content, sorted.

    Any byte or any filename changing changes the hash -- the property the
    content pin needs. Modelled on ``crew_export/candidates.py``'s skill
    ``tree_hash``, widened to hash every file rather than only ``SKILL.md`` so an
    edit to any file in the skill invalidates approval.

    The pin is taken over the bytes that SHIP, so each file is read through the same
    authority the copy reads it through: ``hooks.safe_read_file_bytes_nolink`` opens the leaf
    ``O_NOFOLLOW`` and fstats the descriptor it opened, refusing a hard link (``st_nlink >
    1``), a sensitive path, or a non-regular file -- the identity a name check and
    ``_redirect_between`` cannot see. Hashing ``read_bytes()`` instead would pin the bytes of
    a link target or a hard-linked credential swapped in after the enumeration scan cleared
    the file, so the pin would certify content the copy then refuses. A file the guard
    rejects, an oversized file, or one reached through a redirecting component is REFUSED
    here, not skipped: a skipped file is content the pin does not cover. A leaf symlink is
    passed over exactly as the copy and the scan pass it over, so the pin stays equal to what
    ships.
    """
    try:
        from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
    except ImportError as exc:
        raise ExportRefused(
            f"cannot hash {root} safely, because kiro_crew.hooks is not importable here "
            f"({exc}). That module holds the sensitive-path and hard-link rules this pin has "
            f"to be taken under, and a local approximation of them is not the same check."
        ) from exc
    rows: list[list[str]] = []
    for p in _pinned._walk_no_reparse(root):
        if not p.is_file() or p.is_symlink():
            continue
        # ``is_symlink()`` misses a junction, which ``rglob`` descends into: a file reached
        # through a redirecting component lives outside ``root``, so folding its bytes into
        # the pin folds in content that is not the skill's. Refuse it rather than skip it --
        # the copy refuses the same file, and a skipped file leaves the pin covering less
        # than what ships.
        redirect = _pinned._redirect_between(root, p)
        if redirect is not None:
            raise ExportRefused(
                f"{p.relative_to(root).as_posix()} is reached through a link or junction at "
                f"{redirect.relative_to(root).as_posix()}; its bytes live outside {root}. "
                f"Refusing to fold content reached through a redirect into the content pin."
            )
        try:
            data = safe_read_file_bytes_nolink(str(p), str(root), max_bytes=_MAX_PROMPT_BYTES)
        except FileTooLargeError:
            raise ExportRefused(
                f"{p.relative_to(root).as_posix()} is above the {_MAX_PROMPT_BYTES} byte "
                f"ceiling, so it cannot be certified clean and cannot be pinned. Trim it, or "
                f"ship it outside the bundle."
            ) from None
        if data is None:
            raise ExportRefused(
                f"the file-read guard refuses {p.relative_to(root).as_posix()} (it is "
                f"sensitive, a link, hard-linked to another name, not a regular file, or "
                f"unreadable), so it cannot be certified clean and must not be pinned."
            )
        rows.append([p.relative_to(root).as_posix(), _sha(data)])
    return _sha(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def bundle_digest(root: Path, also_skip: frozenset[str] = frozenset()) -> str:
    """sha256 over every bundle file except the manifest, path-and-content, sorted.

    Byte-for-byte the algorithm of ``crew_export/bundle.py:_bundle_digest`` -- the
    "computed the same way bundle.py already does it" the contract points at. The
    manifest is excluded because it carries the digest; the ``sha256:`` prefix and
    the compact JSON row encoding are preserved so the value is reproducible.

    ``also_skip`` holds extra root-relative posix paths to leave out. It defaults to
    nothing, so the contract value is unchanged; the replacement check uses it to
    re-derive a prior bundle's digest while ignoring a plan file that was added
    after that bundle was built.
    """
    rows: list[list[str]] = []
    for path in _pinned._walk_no_reparse(root):
        rel = path.relative_to(root).as_posix()
        if rel == "manifest.json" or rel in also_skip:
            # Intentional exclusions, by NAME regardless of shape: the manifest carries this
            # digest, and ``also_skip`` holds the plan file added after the prior bundle was
            # built. These are the only entries that leave the signed set on purpose.
            continue
        if _pinned._is_redirecting_entry(path):
            # A symlink or junction is REFUSED, not skipped. A skipped entry still SHIPS, so a
            # redirect left out of the walk signs a digest over a SUBSET of the bundle -- and a
            # redirect is exactly the object an attacker wants outside the signature, since its
            # bytes live wherever it points.
            raise ExportRefused(
                f"the bundle file {rel} is a link or junction; refusing to sign a digest that "
                f"would leave it out of the signed set or fold in bytes reached by following "
                f"it. Re-run the build."
            )
        try:
            mode = os.lstat(path).st_mode
        except OSError as exc:
            raise ExportRefused(
                f"the bundle file {rel} could not be inspected ({exc}); refusing to sign a "
                f"digest that might omit it. Re-run the build."
            ) from exc
        if stat.S_ISDIR(mode):
            # The ONLY entry passed over: a GENUINE directory (a redirect is ruled out above).
            # It has no bytes to hash and its children are walked.
            continue
        if not stat.S_ISREG(mode):
            # A special file (FIFO/socket/device) that still ships. It cannot be hashed -- a
            # no-follow read of a writerless FIFO returns empty bytes rather than failing, so
            # the read alone would sign it as empty -- and dropping it would leave shipping
            # content outside the digest. Refuse, naming it.
            raise ExportRefused(
                f"the bundle file {rel} is not a regular file (a special file); refusing to "
                f"sign a digest that would leave it out of the signed set. Re-run the build."
            )
        # ONE descriptor spans the "is it a regular file" question and the read. The shape
        # check above answers by NAME (``os.lstat``), and ``read_bytes()`` also resolves by
        # NAME, so a leaf swapped for a symlink between them is hashed THROUGH the link -- the
        # digest then pins the target's bytes, and this digest is signed into the manifest and
        # re-derived to prove ownership before a recursive delete, so it would cover an object
        # this build never wrote.
        # ``_read_bytes_openat`` opens the leaf ``O_RDONLY | O_NOFOLLOW`` relative to a
        # descriptor for each parent and reads from that same descriptor, so a redirect at any
        # component fails its own open and yields ``None`` with no path re-resolved after the
        # check; a regular file yields the bytes ``read_bytes`` would, so the digest value is
        # unchanged. ``None`` is REFUSED, not skipped: dropping the entry would sign a digest
        # that silently omits a file the promoted bundle still carries.
        data = _pinned._read_bytes_openat(root, path.relative_to(root))
        if data is None:
            raise ExportRefused(
                f"the bundle file {rel} could not be read as a regular file through a "
                f"no-follow descriptor (it is a link, a special file, or a component of its "
                f"path changed to a link). Refusing to sign a digest over bytes reached by "
                f"following a redirect. Re-run the build."
            )
        rows.append([rel, hashlib.sha256(data).hexdigest()])
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
