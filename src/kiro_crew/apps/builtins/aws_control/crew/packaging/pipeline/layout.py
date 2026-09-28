"""The four-entry layout's leaf writes: the last-chance scan and the selected-skill copy.

Every leaf is scanned again as it lands, and a staged leaf is created relative to the
retained staging descriptor, so a swap of the staging tree after it was created cannot move
a write outside it.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from . import destination as _destination
from . import pinned as _pinned
from . import scan as _scan
from . import sensitive as _sensitive
from .contract import _MAX_PROMPT_BYTES, ExportRefused


def _write_guarded(
    path: Path,
    text: str,
    origin: str,
    *,
    staging_fd: "int | None" = None,
    rel: "str | None" = None,
) -> None:
    """Last-chance scan before bytes land in the artifact. Refuse on a finding."""
    if _scan.scan_text(text, origin):
        raise ExportRefused(f"refusing to write {origin}: it contains a credential")
    if staging_fd is not None and rel is not None:
        if (
            not _pinned._dir_fd_supported()
        ):  # fail-closed floor; staging_fd is only set where supported
            raise ExportRefused(
                f"cannot write {origin} descriptor-relative: this platform lacks "
                f"directory-descriptor support. Re-run on a supported platform."
            )
        # Create the leaf's parent directories relative to the retained staging descriptor,
        # each component ``O_NOFOLLOW``, so a swap of the staging root or an intermediate
        # component since staging was created cannot steer the mkdir or the write outside it.
        parts = PurePosixPath(rel).parts
        dir_fd = os.dup(staging_fd)
        try:
            for comp in parts[:-1]:
                try:
                    os.mkdir(comp, 0o700, dir_fd=dir_fd)
                except FileExistsError:
                    pass
                nxt = os.open(
                    comp,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=dir_fd,
                )
                os.close(dir_fd)
                dir_fd = nxt
        except OSError as exc:
            os.close(dir_fd)
            raise ExportRefused(
                f"cannot create the staging directory for {origin}: a component changed to a "
                f"link or is not an openable directory since staging was created ({exc}). "
                f"Re-run the build."
            ) from exc
        os.close(dir_fd)
        _destination._write_nofollow(path, text, staging_fd=staging_fd, rel=rel)
        return
    _destination._refuse_unusable_parent(path, what=f"{origin}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write through the no-follow primitive, not a plain ``write_text``. The staging tree lives
    # beside ``--out`` in a directory this build does not own, so a leaf path is exactly the
    # mkdir->write window an adversary can plant a symlink into; a following write would then
    # truncate whatever the link named. ``_write_nofollow`` opens the leaf descriptor-relative
    # with ``O_NOFOLLOW`` and refuses a link (the same defence the marker and report already
    # use), and it writes with ``newline=""`` + strict UTF-8 -- the CRLF-translation and
    # encoding contract this site needs so the source pin and the digest stay platform-stable.
    _destination._write_nofollow(path, text)


def _copy_skill(
    skill_dir: Path,
    rel: str,
    dest_root: Path,
    selected: set[str] | None = None,
    *,
    staging_fd: "int | None" = None,
) -> "set[str]":
    """Copy one selected skill, stopping at any nested skill the plan did not select.

    Returns the set of skill-relative posix paths it WROTE, so the staged-tree hash can tell a
    file that was written and then vanished (tampering -> refuse) from one that was never
    staged because it belongs to an unselected nested skill (legitimate -> hashed from source).

    Skills nest: an id is ``relative_to(skills_root).as_posix()``, so ``aws`` and
    ``aws/ec2`` can both be skills and both carry a ``SKILL.md``. A plain ``rglob`` from the
    parent then shipped the child's files too, which defeats deny-by-default -- the plan
    said only ``aws`` and the bundle carried ``aws/ec2`` as well, with no note saying so.

    A descendant is recognised the way the enumerator recognises a skill in the first
    place: it holds a ``SKILL.md``. Its subtree is skipped unless its own id is in
    *selected*, in which case its own ``_copy_skill`` call ships it and this one must not,
    or the same files would be walked twice.

    *selected* defaults to the empty set, which is the SAFE direction: a caller that names
    no selection ships no nested skill. Defaulting to "everything selected" would make the
    old behaviour the fallback, and the old behaviour is the defect.
    """
    selected = selected or set()
    dest = dest_root / rel
    written: set[str] = set()
    excluded_roots = [
        p
        for p in _pinned._walk_no_reparse(skill_dir, match="SKILL.md")
        if p.parent != skill_dir
        and f"{rel}/{p.parent.relative_to(skill_dir).as_posix()}" not in selected
    ]
    for p in _pinned._walk_no_reparse(skill_dir):
        # A genuine directory ships nothing itself -- its files are walked and copied
        # individually -- so it is the one shape skipped here. Every OTHER non-regular entry
        # (a symlink, FIFO, socket, or device node) is REFUSED and named, not silently
        # skipped: an entry that cannot be read as text cannot be scanned for credentials or
        # certified clean, and dropping it makes "unshippable" indistinguishable from "not
        # there" -- the same cannot-be-judged-means-not-present substitution the enumeration
        # scan and the digest already refuse rather than omit.
        if p.is_dir() and not p.is_symlink():
            continue
        if not p.is_file() or p.is_symlink():
            raise ExportRefused(
                f"skill {rel} contains {p.relative_to(skill_dir).as_posix()}, which is a "
                f"symlink or a special file (FIFO, socket, or device), not a regular file. "
                f"It cannot be read as text, scanned for credentials, or certified clean, so "
                f"it is refused rather than silently omitted from the bundle. Remove it from "
                f"the skill, or ship it outside the bundle."
            )
        # ``is_symlink()`` does not see a junction, and ``rglob`` descends into one, so a file
        # under a junction would copy into the bundle with its bytes sourced OUTSIDE the crew
        # -- the nested-reparse-point escape the per-SKILL.md check never covered. Refuse it:
        # the copy is where the escape would ship, so a silent skip is not enough.
        redirect = _pinned._redirect_between(skill_dir, p)
        if redirect is not None:
            raise ExportRefused(
                f"skill {rel} reaches {p.relative_to(skill_dir).as_posix()} through a link or "
                f"junction at {redirect.relative_to(skill_dir).as_posix()}; its bytes live "
                f"outside the crew source. Refusing to copy content through a redirect."
            )
        if any(root.parent in p.parents or root.parent == p.parent for root in excluded_roots):
            continue
        if _sensitive.refused_by_name(p):
            raise ExportRefused(
                f"skill {rel} contains a credential store: {p.relative_to(skill_dir).as_posix()}"
            )
        if _sensitive.refused_by_location(p):
            # The location half, mirroring _resolve_prompt_path. refused_by_name
            # only fires on a FILE named like a credential, so a nested
            # credential DIRECTORY sails through it: a skill carrying .aws/config
            # or .ssh/known_hosts has innocent basenames (config, known_hosts)
            # and would be copied into a bundle handed to an untrusted agent. A
            # kubeconfig's certificate is base64 and may match no _HARD_PATTERNS
            # entry, so the _write_guarded scan below cannot be relied on to
            # catch it either -- judge the location before the read.
            raise ExportRefused(
                f"skill {rel} contains a file inside a credential directory: "
                f"{p.relative_to(skill_dir).as_posix()}. Files under .ssh, .aws, "
                f".gnupg, .kube or .docker are refused before any read (their "
                f"contents cannot be trusted to be scannable) rather than copied "
                f"into a bundle handed to an untrusted agent."
            )
        # Read through the shared file-read guard, the one authority that owns the
        # sensitive-path, descriptor-fstat and hard-link refusals for this build. The name and
        # location checks above clear a file by its PATH, and a hard link gives a credential
        # file a second innocent name inside the skill: skill_dir/notes.md hard-linked to
        # ~/.aws/credentials clears the path check while its bytes are the credential.
        # ``safe_read_file_bytes_nolink`` opens the leaf ``O_NOFOLLOW`` and fstats the
        # descriptor it opened -- ``st_nlink > 1`` is the identity a name check cannot see --
        # and confirms the opened inode resolves inside ``skill_dir`` and is not sensitive.
        try:
            from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
        except ImportError as exc:
            raise ExportRefused(
                f"skill {rel} cannot be read safely, because kiro_crew.hooks is not importable "
                f"here ({exc}). That module holds the sensitive-path and hard-link rules this "
                f"read has to satisfy, and a local approximation of them is not the same check."
            ) from exc
        # The guard has TWO refusal channels that mean different things: None is "the guard
        # rejected this", while the size cap RAISES. Catching only one lets a FileTooLargeError
        # out of a function contracted to raise ExportRefused, reaching the CLI as a traceback.
        try:
            raw = safe_read_file_bytes_nolink(str(p), str(skill_dir), max_bytes=_MAX_PROMPT_BYTES)
        except FileTooLargeError as exc:
            raise ExportRefused(
                f"skill {rel} contains a file above the {_MAX_PROMPT_BYTES} byte ceiling: "
                f"{p.relative_to(skill_dir).as_posix()} ({exc}). A skill file that large is an "
                f"asset, not scannable text; trim it or ship it outside the bundle."
            ) from None
        if raw is None:
            # A file SELECTED for a bundle that the guard refuses is not silently skipped.
            # None here means the guard rejected the read: the file is sensitive, a link, a
            # hard link to another name, not a regular file, outside skill_dir, or unreadable
            # (the guard swallows a mid-read OSError to None). Silently dropping it ships the
            # skill incomplete with no notice and makes "unreadable" read as "not selected" --
            # the safe direction, matching the module's deny-by-default posture, is to REFUSE
            # and say which file and why rather than quietly omitting it.
            raise ExportRefused(
                f"skill {rel} contains a file the shared file-read guard refuses: "
                f"{p.relative_to(skill_dir).as_posix()}. It is sensitive, a link, hard-linked "
                f"to another name, not a regular file, outside the skill, or unreadable, so it "
                f"cannot be certified clean and must not ship. Remove it from the skill, or "
                f"ship it outside the bundle."
            )
        # Decode the guarded bytes exactly as they sit on disk: no newline translation and no
        # re-encode, so a CRLF-authored skill still hashes byte-for-byte against its source and
        # the content pin holds. A non-UTF-8 body is unscannable and is refused, not shipped.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ExportRefused(
                f"skill {rel} contains a file that is not scannable UTF-8 text: "
                f"{p.relative_to(skill_dir).as_posix()}. A selected skill's files must be "
                f"readable so the credential scan can clear them; a binary or non-UTF-8 asset "
                f"can be neither scanned nor safely shipped, and is refused rather than "
                f"silently omitted. Remove it from the skill, or ship it outside the bundle."
            ) from None
        member_rel = p.relative_to(skill_dir).as_posix()
        _write_guarded(
            dest / member_rel,
            text,
            f"skills/{rel}/{p.name}",
            staging_fd=staging_fd,
            rel=(f"skills/{rel}/{member_rel}" if staging_fd is not None else None),
        )
        written.add(p.relative_to(skill_dir).as_posix())
    return written
