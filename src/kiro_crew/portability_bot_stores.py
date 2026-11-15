"""Install each imported Bot's own stores from an instance archive, add-only.

Three stores per Bot ride an instance export: the dashboard record directory
``members/<slug>/dashboard/``, the dashboard artifact directory its record pins
(archive ``bot-artifacts/<P>/``, published to the data home's ``artifacts/<P>/``),
and the single files ``briefing.md`` and ``welcome_claimed.json``.
The archive is untrusted, so every store is installed the same careful way:

1. built in a stage directory inside the import's private extraction directory,
   each admitted file copied through a pinned no-follow open of the extracted
   source and an exclusive create. Each Bot's stage is created by an exclusive
   owner-only ``mkdir`` under a random name (``.bot-stage-<hex>``), so neither its
   name nor its contents come from the archive, whose top directory names the
   extraction; an entry already at that name refuses the stores (``write_failed``);
2. published only once the build finished with every admitted file copied -- a
   directory by renaming it to a hidden sibling of its destination and then
   :func:`platform_compat.publish_dir_noreplace` (which claims the destination
   with an exclusive ``mkdir`` where the filesystem has no no-replace rename), a
   single file by an exclusive create -- so a store is installed whole or not at
   all;
3. never over a store the destination already has: that store is kept whole, and
   reported ``kept`` only when the entry is this Bot's own (``held_by_other_bot``
   otherwise).

The stage directory goes with the extraction directory, and the hourly staging
sweep reclaims it after a crash, so a failed build leaves nothing to clean up.

A carried artifact is checked in the stage, from the staged bytes, BEFORE it is
published, on what the artifact store will act on (:func:`_check_staged_artifact`):
its ``meta.json`` must name the package, must hold each of
:data:`REFUSED_META_FIELDS` (the fields that make the store read or write a file
outside the artifact's own directory) only as the store's own empty value of its
type, or not at all, and decode with them empty, and must be a
dashboard artifact in every version kind. Every file the package can serve is
checked: the package holds only ``meta.json``, ``current.html`` and
``versions/v<n>.html`` up to its version, every version is a dashboard package bound
to ``crewmate:<slug>``, the pinned one's canonical bytes hash to the pinned digest, and
``current.html`` equals the highest version. The store's ``comments.json``
(:data:`UNSTAGED_ARTIFACT_FILES`) is not staged. A package that passes has
:data:`STRIPPED_META_FIELDS` (its ties to remote copies) removed from the staged
``meta.json`` before it is published, so it installs unpublished. A package is never
published when it fails the gate, when the archive lacks it (checked first, before the
bound dashboards here are listed), or when the scan of the bound dashboards here
(:class:`BoundIndex`) cannot prove no other package is bound to the Bot; its record is
installed all the same and the part is ``page_not_rebuilt`` with the reason
(``artifact_rejected``, ``artifact_missing``, ``digest_mismatch``,
``another_package_of_this_bot`` or ``unreadable_record``). The record is not installed
only when it cannot be read, when it pins no well-formed package slug
(``artifact_rejected``), when the archive's own package binds it to another binding
(``held_by_other_bot``, whichever Bot the import installs first), or when the
``artifacts/<P>/`` it pins is already here and is not this Bot's own (``not_restored``
/ ``held_by_other_bot`` when it is bound to another binding, ``artifact_rejected`` when
it is no dashboard, a live pointer or bound to nothing, ``unreadable_record`` when it
cannot be read). It is this Bot's own only when both its live version (what
``scan_bound_dashboards`` reads: a copy with provenance, ``source_copy_only``, counts
as any package; a live pointer never) and the version the record pins are dashboards
bound to ``crewmate:<slug>`` (:func:`_owned_package`). When this Bot's
dashboard record is already here but the ``artifacts/<P>/`` it pins is not, a carried
package that passes the same gate against the destination record's pin is published
add-only. A kept record whose page or package could be brought back and was not is
``page_not_rebuilt`` with the reason; it stays ``kept`` only when nothing was missing
or the archive held nothing to restore it from.

The page that runs is not carried. It is rebuilt from the checked package (or, for
an ``artifacts/<P>/`` the destination already has, from this Bot's own package there
through the same gate, and only when no other package here is bound to the Bot) and
only then is the gateway-owned snapshot written, under the instance lock.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePath
from typing import Any

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.atomic_write import atomic_write, atomic_write_at
from kiro_crew.members import BRIEFING_FILE_NAME, MEMBERS_DIR_NAME
from kiro_crew.owner_only_files import mkdirs_owner_only
from kiro_crew.portability_bot_types import (
    BOT_ARTIFACTS_DIR,
    MAX_RECORD_BYTES,
    ImportedBot,
    PartOutcome,
    carried_member_file,
    read_fd_at_most,
    safe_path_component,
)

logger = logging.getLogger(__name__)

#: The prefix of each Bot's stage directory inside the import's private extraction
#: directory; :func:`_new_stage` appends a random suffix.
STAGE_DIRNAME = ".bot-stage"

#: ``meta.json`` fields that refuse a carried artifact unless each holds exactly the
#: empty value the store itself writes for it (:data:`REFUSED_META_EMPTY`): they change
#: what the LOCAL artifact serves. ``ArtifactStore.get`` (``artifacts.py``) reads the
#: current view from ``source_path`` through
#: ``artifacts_source_link._try_read_source_path`` unless ``source_copy_only`` is set,
#: and the writes in ``artifacts_writes.py`` mirror edits to it through
#: ``_try_write_source_path`` (``_is_live_pointer``). ``source_root`` widens the roots
#: that read and write are allowed under (``artifacts_source_link.allowed_source_roots``).
#: ``source_copy_only`` only qualifies ``source_path``, so a carried package sets none
#: of the three.
REFUSED_META_FIELDS: tuple[str, ...] = ("source_path", "source_root", "source_copy_only")

#: The one value each :data:`REFUSED_META_FIELDS` entry may hold, the empty value
#: ``records.encode_meta`` writes for an artifact that is no live pointer. Matched by
#: type as well as value: ``records.decode_meta`` turns ANY present value into a string
#: (``str(0)`` is ``"0"``, a path), so a falsy ``0``, ``false``, ``[]`` or ``{}`` is as
#: much a pointer as a real path.
REFUSED_META_EMPTY: dict[str, str | bool] = {
    "source_path": "",
    "source_root": "",
    "source_copy_only": False,
}


#: ``meta.json`` fields removed from a carried artifact before it is published: they
#: point at REMOTE copies and never change what the local artifact serves.
#: ``publication`` is what ``publish_sync.py`` pushes new versions to and the
#: dashboard's publish and sharing handlers report; ``fork_metadata`` is the upstream
#: ``publish_sync.py`` pulls from; ``webapp_metadata`` describes a deployed web app
#: (its target and teardown handle). The archive's values name someone else's remote
#: state, so the package is installed unpublished.
STRIPPED_META_FIELDS: tuple[str, ...] = ("publication", "fork_metadata", "webapp_metadata")

#: The claimed-welcome marker below ``members/<slug>/``; the welcome owner names it
#: ``dashboard.mate_welcome.GREETING_MARKER_FILENAME`` (pinned equal by a test).
WELCOME_CLAIMED_FILE_NAME = "welcome_claimed.json"

#: The single files below ``members/<slug>/`` installed as the ``briefing`` part.
BRIEFING_FILES: tuple[str, ...] = (BRIEFING_FILE_NAME, WELCOME_CLAIMED_FILE_NAME)

#: Largest single file copied out of the archive; a bigger one fails its store.
MAX_FILE_BYTES = 64 * 1024 * 1024

#: Budgets for listing one archive store directory.
MAX_STORE_ENTRIES = 10_000
MAX_STORE_DEPTH = 8

_FILE_MODE = 0o600

#: A version file name the gate admits: ``v<n>.html`` with ``n`` written as the store
#: writes it (``versions/v{n}.html``), so no leading zero.
_VERSION_FILE_RE = re.compile(r"v([1-9][0-9]*)\.html")

#: Store files of an artifact directory that are not part of the page the package
#: serves and are left out of the stage, so they are never installed: the comment
#: thread is the exporting host's.
UNSTAGED_ARTIFACT_FILES: frozenset[tuple[str, ...]] = frozenset({("comments.json",)})


_FAILURES: tuple[type[BaseException], ...] = (OSError, pinned_fs.PinnedPathRefusal, ValueError)

#: The reason a store entry here is another Bot's.
_HELD = "held_by_other_bot"

#: The reason a package may not serve this Bot: another package here is bound to it.
_OWN_OTHER = "another_package_of_this_bot"


def _is_link(path: Path) -> bool:
    return platform_compat.is_link_or_junction(path)


def _entry_kind(directory: Path, name: str) -> str:
    """``dir``, ``file`` or ``""`` for *name* in *directory*, classified without following."""
    try:
        st = os.lstat(directory / name)
    except OSError:
        return ""
    if _is_link(directory / name):
        return ""
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return ""


def _copy_file(src_root: Path, src_rel: PurePath, dst_root: Path, dst_rel: PurePath) -> None:
    """Copy one extracted file into the stage through :func:`pinned_fs.copy_file_pinned`.

    Every directory component below *src_root* and *dst_root* is pinned (the stage
    chain is created owner-only as it is pinned), the source is opened without
    following a link and the destination with ``O_EXCL``. A source that is a link,
    not a regular file, has a second hard link or is over :data:`MAX_FILE_BYTES`
    copies nothing and raises ``ValueError``; a destination already there raises
    ``FileExistsError``. Where the platform cannot pin a directory (Windows) the
    source is opened by ``platform_compat.open_file_no_reparse`` and handed over as a
    descriptor, and the stage file is created by name: the stage is this import's
    own private directory.
    """
    skipped: list[str] = []
    with contextlib.ExitStack() as held:
        src_parent = held.enter_context(
            pinned_fs.open_pinned_descendant_dir(
                src_root, src_rel.parts[:-1], what="imported Bot store"
            )
        )
        dst_parent = held.enter_context(
            pinned_fs.open_pinned_descendant_dir(
                dst_root, dst_rel.parts[:-1], what="Bot store stage", create=True
            )
        )
        src_fd = (
            platform_compat.open_file_no_reparse(src_root / src_rel, nonblocking=True)
            if src_parent is None
            else None
        )
        copied = pinned_fs.copy_file_pinned(
            str(src_root / src_rel),
            str(dst_root / dst_rel),
            dir_fd=src_parent,
            name=src_rel.name if src_parent is not None else None,
            src_fd=src_fd,
            dst_dir_fd=dst_parent,
            dst_name=dst_rel.name if dst_parent is not None else None,
            force_mode=_FILE_MODE,
            max_bytes=MAX_FILE_BYTES,
            on_skip=lambda reason, _path: skipped.append(reason),
        )
    if not copied:
        reason = skipped[0] if skipped else "not copied"
        raise ValueError(f"imported Bot store file {src_rel.name!r} was refused: {reason}")


def _list_files(root: Path, rel: PurePath) -> list[tuple[str, ...]]:
    """Every regular file below ``root / rel`` as component tuples, links left out.

    A pinned scan where the platform can pin, else a by-name walk that refuses to
    descend a link. Over :data:`MAX_STORE_ENTRIES` or :data:`MAX_STORE_DEPTH` raises.
    """
    if pinned_fs.supports_pinned_walk():
        with pinned_fs.open_pinned_descendant_dir(root, rel.parts, what="imported Bot store") as fd:
            if fd is None:
                raise OSError("the pinned walk yielded no descriptor")
            tree = pinned_fs.scan_tree_pinned(
                fd,
                device=os.fstat(fd).st_dev,
                max_entries=MAX_STORE_ENTRIES,
                max_depth=MAX_STORE_DEPTH,
            )
        return sorted(tree.files)
    found: list[tuple[str, ...]] = []
    stack: list[tuple[str, ...]] = [()]
    while stack:
        here = stack.pop()
        if len(here) > MAX_STORE_DEPTH:
            raise OSError("refusing an imported Bot store over its depth limit")
        with os.scandir(root.joinpath(*rel.parts, *here)) as scan:
            for entry in scan:
                key = here + (entry.name,)
                if _is_link(Path(entry.path)):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(key)
                elif entry.is_file(follow_symlinks=False):
                    found.append(key)
                if len(found) + len(stack) > MAX_STORE_ENTRIES:
                    raise OSError("refusing an imported Bot store over its entry limit")
    return sorted(found)


@contextlib.contextmanager
def _pinned_dir(root: Path, parts: Sequence[str]) -> Iterator[int | None]:
    with pinned_fs.open_pinned_descendant_dir(root, tuple(parts), what="Bot store publish") as fd:
        yield fd


def _remove_sibling(parent_fd: int, name: str) -> None:
    """Remove the staged tree :func:`_rename_noreplace` parked at *name*; log a failure."""
    try:
        shutil.rmtree(name, dir_fd=parent_fd)
    except (OSError, NotImplementedError):
        logger.warning("import: a staged Bot store stayed at %r", name, exc_info=True)


def _rename_noreplace(
    src_root: Path,
    src_parts: Sequence[str],
    dst_root: Path,
    dst_parts: Sequence[str],
) -> None:
    """Move directory ``src_root/src_parts`` to an ABSENT ``dst_root/dst_parts``.

    Both parents are opened pinned. The staged directory is first renamed to a
    fresh hidden sibling of the destination (the same filesystem: the stage lives
    under the data home; a cross-device rename raises ``OSError``), then published
    by :func:`platform_compat.publish_dir_noreplace`, which falls back to an
    exclusive ``mkdir`` claim where the filesystem has no atomic no-replace rename.
    A destination that exists, or appears, raises :class:`FileExistsError`; no
    other step raises it. On any failure after the first rename the sibling is
    removed, so nothing is published. Where the platform cannot pin a directory
    (Windows) ``os.rename`` already refuses an existing destination.
    """
    with (
        _pinned_dir(src_root, src_parts[:-1]) as src_fd,
        _pinned_dir(dst_root, dst_parts[:-1]) as dst_fd,
    ):
        if src_fd is None or dst_fd is None:
            os.rename(src_root.joinpath(*src_parts), dst_root.joinpath(*dst_parts))
            return
        sibling = f".{dst_parts[-1]}.publish-{secrets.token_hex(8)}"
        try:
            os.rename(src_parts[-1], sibling, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        except FileExistsError as exc:
            raise OSError(f"parking a staged Bot store failed: {exc.strerror}") from exc
        parent = dst_root.joinpath(*dst_parts[:-1])
        try:
            # The publish names the parent by path: it must still be the pinned one.
            here, pinned = os.stat(parent, follow_symlinks=False), os.fstat(dst_fd)
            if (here.st_dev, here.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise OSError(f"the Bot store parent {parent} changed during the publish")
            platform_compat.publish_dir_noreplace(parent / sibling, parent / dst_parts[-1])
        except BaseException:
            _remove_sibling(dst_fd, sibling)
            raise


def _read_staged(path: Path, cap: int) -> bytes:
    """Read a file this import staged, refusing one over *cap* bytes.

    Raises ``OSError`` when it cannot be opened and ``portability_bot_types.BoundedReadError``
    (a ``ValueError``) when it is too large or a read fails, both in :data:`_FAILURES`.
    """
    fd = platform_compat.open_file_no_reparse(path)
    try:
        return read_fd_at_most(fd, cap)
    finally:
        os.close(fd)


def _publish_file(
    data: bytes, staged: Path, dst_root: Path, dst_parts: Sequence[str]
) -> tuple[int, int]:
    """Create ``dst_root/dst_parts`` exclusively with *data*; leave nothing on failure.

    *staged* is this import's own staged copy of *data*. Returns the created file's
    ``(st_dev, st_ino)``, so a later rollback removes this file and nothing that took
    its name. Under a pinned parent the create is
    :func:`pinned_fs.write_new_file_pinned` (``O_EXCL|O_NOFOLLOW``, both fsyncs, an
    identity-verified rollback). That primitive is descriptor-relative by contract, so
    where the platform cannot pin a directory (Windows) the file is published by
    :func:`pinned_fs.copy_file_pinned` from *staged* to the destination name, which
    opens the destination ``O_EXCL`` (``O_NOFOLLOW`` where the platform has it) and
    reports the created inode before the first write; a copy that fails is removed
    again only while its name still holds that inode.
    """
    with _pinned_dir(dst_root, dst_parts[:-1]) as fd:
        if fd is not None:
            pinned_fs.write_new_file_pinned(fd, dst_parts[-1], data, mode=_FILE_MODE, fsync=True)
            st = os.stat(dst_parts[-1], dir_fd=fd, follow_symlinks=False)
            return st.st_dev, st.st_ino
    target = dst_root.joinpath(*dst_parts)
    opened: list[os.stat_result] = []
    skipped: list[str] = []
    try:
        copied = pinned_fs.copy_file_pinned(
            str(staged),
            str(target),
            force_mode=_FILE_MODE,
            max_bytes=MAX_FILE_BYTES,
            on_skip=lambda reason, _path: skipped.append(reason),
            on_opened=opened.append,
        )
        if not copied:
            raise ValueError(f"{target.name!r} was not published: {(skipped or ['skipped'])[0]}")
    except BaseException:
        # A destination already there raises before ``on_opened``: nothing to remove.
        if opened:
            pinned_fs.unlink_verified_by_name(
                target.parent, target.name, (opened[0].st_dev, opened[0].st_ino)
            )
        raise
    return opened[0].st_dev, opened[0].st_ino


def _remove_created(
    dst_root: Path, created: Sequence[tuple[Sequence[str], tuple[int, int]]]
) -> None:
    """Remove each file this import created, only while its name still holds that file."""
    for parts, ident in created:
        try:
            with _pinned_dir(dst_root, parts[:-1]) as fd:
                if fd is not None:
                    removed = pinned_fs.unlink_verified(fd, parts[-1], ident)
                else:
                    removed = pinned_fs.unlink_verified_by_name(
                        dst_root.joinpath(*parts[:-1]), parts[-1], ident
                    )
        except _FAILURES:
            removed = False
        if not removed:
            logger.warning("import: a partly installed Bot file stayed at %s", "/".join(parts))


def _ensure_real_dir(root: Path, parts: Sequence[str]) -> None:
    """Create ``root/parts`` owner-only when absent, refusing a link at any level."""
    current = root
    for part in parts:
        current = current / part
        if _is_link(current):
            raise pinned_fs.PinnedPathRefusal(f"refusing to install through a link at {part!r}")
    mkdirs_owner_only(root.joinpath(*parts))


def _new_stage(parent: Path) -> Path:
    """Create one Bot's stage directory below *parent*: exclusive, owner-only, random name.

    The name is never derived from the extraction (whose top directory the archive
    picks), so no archive entry can sit inside the stage. An entry already at the
    chosen name raises ``FileExistsError``: the stage is never reused.
    """
    name = f"{STAGE_DIRNAME}-{secrets.token_hex(16)}"
    with _pinned_dir(parent, ()) as fd:
        if fd is not None:
            os.mkdir(name, 0o700, dir_fd=fd)
        else:
            os.mkdir(parent / name, 0o700)
    return parent / name


def _staged_pin(stage_record: Path) -> tuple[str, int, str]:
    """``(package slug, version, digest)`` the staged ``instance.json`` pins, or empty."""
    from kiro_crew.dashboard_templates import instance

    try:
        raw = json.loads(_read_staged(stage_record, MAX_RECORD_BYTES).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", 0, ""
    if not isinstance(raw, dict):
        return "", 0, ""
    slug, version, digest, _released = instance._package_field(raw)
    return slug, version, digest


def _artifact_store(mc: Path) -> Any:
    """The destination artifact store, rooted where the data home keeps it. A test seam."""
    from kiro_crew.artifacts import ArtifactStore

    return ArtifactStore(mc / "artifacts")


def _decoded(text: str) -> Any:
    """*text* decoded as a stored dashboard envelope, before any layout validation; ``None``."""
    from kiro_crew.artifact_store import dashboard_package
    from kiro_crew.artifact_store.model import ArtifactValidationError

    try:
        return dashboard_package._decode_package(text)
    except (ArtifactValidationError, ValueError, RecursionError):
        return None


def _page_of(raw: Any, slug: str, digest: str) -> tuple[str, str]:
    """``(reason, canonical content)`` for a decoded pinned page; ``""`` when it passes.

    *raw* must validate as a dashboard package bound to ``crewmate:<slug>`` whose
    canonical bytes hash to *digest* (the canonical form ``canonical_package_content``
    gives, from the envelope already decoded).
    """
    from kiro_crew.artifact_store import dashboard_package
    from kiro_crew.artifact_store.model import ArtifactValidationError
    from kiro_crew.dashboard_templates import instance

    try:
        package = dashboard_package.validate_package(raw)
        content = dashboard_package.dump_package(package)
    except (ArtifactValidationError, ValueError, RecursionError):
        return "artifact_rejected", ""
    if package.get("bound_to") != f"crewmate:{slug}":
        return "artifact_rejected", ""
    if instance._page_digest(content) != digest:
        return "digest_mismatch", ""
    return "", content


def _page_refusal(raw_content: str, slug: str, digest: str) -> tuple[str, str]:
    """:func:`_page_of` for stored text: ``artifact_rejected`` when it does not decode."""
    raw = _decoded(raw_content)
    if raw is None:
        return "artifact_rejected", ""
    return _page_of(raw, slug, digest)


def _write_page(slug: str, content: str) -> str:
    """Write the checked page as *slug*'s gateway-owned snapshot; ``""`` or ``write_failed``."""
    from kiro_crew.dashboard_templates import instance

    try:
        with instance._locked(instance.instance_dir(slug)):
            instance._write_page_snapshot(slug, content)
    except (OSError, instance.InstanceError):
        logger.warning("import: %r's dashboard page was not rebuilt", slug, exc_info=True)
        return "write_failed"
    return ""


def _page_missing(slug: str, digest: str) -> bool:
    """Whether *slug* has no page snapshot for *digest*; ``False`` when that cannot be told."""
    from kiro_crew.dashboard_templates import instance

    try:
        return not os.path.lexists(instance._page_snapshot_path(slug, digest))
    except (OSError, instance.InstanceError):
        return False


def _is_store_empty(raw: dict[str, Any], name: str) -> bool:
    """Whether *raw* lacks *name* or holds exactly its :data:`REFUSED_META_EMPTY` value."""
    if name not in raw:
        return True
    value, empty = raw[name], REFUSED_META_EMPTY[name]
    return type(value) is type(empty) and value == empty


def _store_text(data: bytes) -> str:
    """*data* decoded the way the artifact store reads a file: UTF-8, universal newlines."""
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8").read()


def _envelope_bound_to(text: str, binding: str) -> bool:
    """Whether *text* is a dashboard package envelope whose ``bound_to`` is *binding*.

    Read off the decoded envelope, before any layout validation, the way the
    dashboard's ownership readers read a stored record
    (``dashboard_package.resolve_bound_slug``, ``stored_package_binding``): a version
    the validator would reject still names an owner there, so it must name this Bot.
    """
    return _envelope(text)[1] == binding


def _staged_versions(art_dir: Path, highest: int) -> list[int] | None:
    """The version numbers the staged package holds, or ``None`` when it holds more.

    Every file below *art_dir* must be ``meta.json``, ``current.html`` or
    ``versions/v<n>.html`` (``n`` written without a leading zero, ``1 <= n <=``
    *highest*), and both ``current.html`` and ``versions/v<highest>.html`` must be
    there; anything else is a file the store could serve or act on that this gate
    never read.
    """
    versions: list[int] = []
    present: set[tuple[str, ...]] = set()
    for key in _list_files(art_dir, PurePath()):
        present.add(key)
        if key in (("meta.json",), ("current.html",)):
            continue
        match = (
            _VERSION_FILE_RE.fullmatch(key[1]) if len(key) == 2 and key[0] == "versions" else None
        )
        if match is None or not 1 <= int(match.group(1)) <= highest:
            return None
        versions.append(int(match.group(1)))
    if ("current.html",) not in present or ("versions", f"v{highest}.html") not in present:
        return None
    return sorted(versions)


def _check_staged_artifact(
    art_dir: Path, package: str, slug: str, version: int, digest: str
) -> tuple[str, str]:
    """Gate a staged artifact directory before it is published, from its staged bytes.

    Returns ``(reason, canonical page)``; the reason is ``""`` when the package may
    be published. The gate decides on what the artifact store will act on, so every
    file the package can serve is checked:

    * ``meta.json`` is decoded by the store's own ``records.decode_meta`` and must name
      *package*; a :data:`REFUSED_META_FIELDS` key that is present refuses it unless
      its value is exactly the store's own empty value of that type
      (:data:`REFUSED_META_EMPTY`: the decoder stringifies any other value, so ``0``
      or ``[]`` would still be a live pointer), and the decoded fields must be empty
      too. ``kind`` and EVERY
      ``version_kinds`` entry must be ``dashboard``, and *version* must be in
      ``1..meta.version`` (``artifact_missing`` otherwise).
    * The package holds nothing but ``meta.json``, ``current.html`` and
      ``versions/v<n>.html`` for ``n`` in ``1..meta.version``
      (:func:`_staged_versions`).
    * EVERY version file is a dashboard package bound to ``crewmate:<slug>``, read
      off its envelope (:func:`_envelope_bound_to`); the pinned one must also pass
      :func:`_page_refusal` (validated, canonical bytes hashing to *digest*).
    * ``current.html`` is byte-equal to the highest version's file.

    Each file is decoded the way the store reads it (UTF-8 text, universal newlines).
    A package that passes has its :data:`STRIPPED_META_FIELDS` removed from the staged
    ``meta.json`` (``write_failed`` when that rewrite fails).
    """
    from kiro_crew.artifact_store import dashboard_package, records
    from kiro_crew.artifact_store.model import ArtifactError

    meta_path = art_dir / "meta.json"
    try:
        raw = json.loads(_read_staged(meta_path, MAX_RECORD_BYTES).decode("utf-8"))
        meta = records.decode_meta(raw, meta_path)
    except (ArtifactError, TypeError, OSError, ValueError, pinned_fs.PinnedPathRefusal):
        return "artifact_rejected", ""
    if meta.slug != package or not all(_is_store_empty(raw, name) for name in REFUSED_META_FIELDS):
        return "artifact_rejected", ""
    if any(getattr(meta, name) for name in REFUSED_META_FIELDS):
        return "artifact_rejected", ""
    if {meta.kind, *meta.version_kinds.values()} != {dashboard_package.DASHBOARD_KIND}:
        return "artifact_rejected", ""
    if not 1 <= version <= meta.version:
        return "artifact_missing", ""
    binding = f"crewmate:{slug}"
    try:
        versions = _staged_versions(art_dir, meta.version)
        if versions is None or version not in versions:
            return "artifact_rejected", ""
        texts: dict[int, str] = {}
        highest: bytes | None = None
        for number in versions:
            data = _read_staged(art_dir / "versions" / f"v{number}.html", MAX_FILE_BYTES)
            texts[number] = _store_text(data)
            if number == meta.version:
                highest = data
        current = _read_staged(art_dir / "current.html", MAX_FILE_BYTES)
    except _FAILURES:
        return "artifact_rejected", ""
    if current != highest or not all(_envelope_bound_to(t, binding) for t in texts.values()):
        return "artifact_rejected", ""
    reason, content = _page_refusal(texts[version], slug, digest)
    if reason or all(raw.get(name) is None for name in STRIPPED_META_FIELDS):
        return reason, content
    try:
        _rewrite_staged(meta_path, _stripped_meta(meta))
    except _FAILURES:
        logger.warning("import: %r's artifact record was not cleaned", package, exc_info=True)
        return "write_failed", ""
    return "", content


def _stripped_meta(meta: Any) -> bytes:
    """*meta* encoded by the store's own ``records.encode_meta``, remote ties removed."""
    from kiro_crew.artifact_store import records

    doc = json.loads(records.encode_meta(meta))
    for name in STRIPPED_META_FIELDS:
        doc.pop(name, None)
    return json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")


def _rewrite_staged(path: Path, data: bytes) -> None:
    """Replace a file this import staged with *data* (UTF-8 text), synced to disk.

    Under its pinned parent through :func:`atomic_write.atomic_write_at` (the writer
    :func:`pinned_fs.write_file_pinned` publishes with): an
    ``O_EXCL|O_NOFOLLOW`` sibling written, fsynced and renamed over the leaf relative to
    the same descriptor. Where the platform cannot pin a directory (Windows), through
    :func:`atomic_write.atomic_write` by name, also fsynced; the stage is this import's
    own private directory.
    """
    with _pinned_dir(path.parent, ()) as fd:
        if fd is not None:
            atomic_write_at(fd, path.name, data.decode("utf-8"), fsync=True, mode=_FILE_MODE)
            return
    atomic_write(path, data, fsync=True, mode=_FILE_MODE)


def _archive_carries(snap: Path, package: str, version: int) -> bool:
    """Whether the archive carries ``bot-artifacts/<package>/`` with the pinned version's file.

    Read without following a link, before anything else is consulted for the package,
    and by the same name filter :func:`_stage_artifact` stages by, so a carried package
    always stages its pinned version; :func:`_stage_artifact` copies it through the
    pinned walk all the same.
    """
    from kiro_crew import portability

    carried = snap / BOT_ARTIFACTS_DIR / package
    pinned = PurePath("artifacts", package, "versions", f"v{version}.html")
    return (
        _entry_kind(snap / BOT_ARTIFACTS_DIR, package) == "dir"
        and _entry_kind(carried, "versions") == "dir"
        and _entry_kind(carried / "versions", f"v{version}.html") == "file"
        and portability._keep_for_export(pinned)
    )


def _stage_artifact(snap: Path, stage: Path, package: str) -> None:
    """Copy the archive's ``bot-artifacts/<package>/`` into the stage.

    Called only once :func:`_archive_carries` said the archive carries it; the gate
    (:func:`_check_staged_artifact`) judges what was staged. A failed copy raises one
    of :data:`_FAILURES`.
    """
    from kiro_crew import portability

    src_art = PurePath(snap.name, BOT_ARTIFACTS_DIR, package)
    files = [
        key
        for key in _list_files(snap.parent, src_art)
        if key not in UNSTAGED_ARTIFACT_FILES
        and portability._keep_for_export(PurePath("artifacts", package, *key))
    ]
    for key in files:
        _copy_file(snap.parent, src_art.joinpath(*key), stage, PurePath("artifacts", package, *key))


def _staged_held(art_dir: Path, slug: str) -> str:
    """``held_by_other_bot`` when the staged package's live page is bound to another binding.

    The archive itself gives the package to that binding, so a record of *slug*
    pinning it is not this Bot's, whichever of the two is installed first. ``""``
    otherwise; the gate (:func:`_check_staged_artifact`) judges the rest.
    """
    try:
        text = _store_text(_read_staged(art_dir / "current.html", MAX_FILE_BYTES))
    except _FAILURES:
        return ""
    target = _envelope(text)[1]
    return _HELD if target is not None and target != f"crewmate:{slug}" else ""


def _publish_artifact(stage: Path, mc: Path, package: str) -> str:
    """Publish the checked staged package to ``artifacts/<package>``, add-only.

    ``""`` when published, ``exists`` when the destination has (or gained) that
    directory, ``write_failed`` otherwise.
    """
    try:
        _ensure_real_dir(mc, ("artifacts",))
    except _FAILURES:
        # A FileExistsError here is a non-directory at ``artifacts``.
        logger.warning("import: no artifact directory for %r", package, exc_info=True)
        return "write_failed"
    try:
        _rename_noreplace(
            stage.parent, (stage.name, "artifacts", package), mc, ("artifacts", package)
        )
    except FileExistsError:
        return "exists"
    except _FAILURES:
        logger.warning("import: dashboard artifact %r was not installed", package, exc_info=True)
        return "write_failed"
    return ""


def _record_state_ok(slug: str) -> bool:
    from kiro_crew.dashboard_templates import instance

    try:
        return instance.read(slug).state != instance.STATE_ERROR
    except (OSError, ValueError, instance.InstanceError):
        return False


def _present(bot: ImportedBot, part: str, owned: bool) -> PartOutcome:
    """The row for a store the destination already has: kept when it is this Bot's own."""
    if owned:
        return PartOutcome(bot.name, part, "kept")
    return PartOutcome(bot.name, part, "not_restored", _HELD)


def _not_a_bound_dashboard(art: Any) -> bool:
    """Whether stored artifact *art* (its metadata is enough) can be no Bot's dashboard.

    It is not a dashboard, or it is a live pointer (``dashboard_package.is_live_pointer``,
    the one rule ``scan_bound_dashboards`` skips a record by): its bytes are a file's
    elsewhere, so no binding it names is held. A copy with provenance
    (``source_copy_only``) owns its bytes and is read as any dashboard.
    """
    from kiro_crew.artifact_store import dashboard_package

    return art.kind != dashboard_package.DASHBOARD_KIND or dashboard_package.is_live_pointer(art)


def _envelope(text: str) -> tuple[Any, str | None]:
    """Dashboard envelope *text* decoded, and the ``bound_to`` string it names.

    ``(None, None)`` when it is no dashboard envelope; the binding is ``None`` when it
    names none. Read off the decoded envelope, before any layout validation, the way
    ``scan_bound_dashboards`` reads a stored record.
    """
    from kiro_crew.artifact_store import dashboard_package

    raw = _decoded(text)
    if not isinstance(raw, dict) or raw.get("kind") != dashboard_package.DASHBOARD_KIND:
        return None, None
    target = raw.get("bound_to")
    return raw, target if isinstance(target, str) else None


def _binding_owner(target: str | None, binding: str) -> str:
    """``""`` when *target* is *binding*, ``held_by_other_bot`` when it is another one.

    ``artifact_rejected`` when there is none: no Bot holds such a package, so it is
    no other Bot's, and it is not this one's to take either.
    """
    if target is None:
        return "artifact_rejected"
    return "" if target == binding else _HELD


class _Owned:
    """Whether ``artifacts/<P>`` here is this Bot's, and the page it would serve.

    ``owner``: ``""`` when it is this Bot's; ``held_by_other_bot`` when it is another
    binding's, ``artifact_rejected`` when it is no bound dashboard at all, or
    ``unreadable_record`` when it cannot be read. ``page``: why no page can be built
    from it although it is this Bot's (``""``: ``content`` is the checked page).
    """

    def __init__(self, owner: str = "", page: str = "", content: str = "") -> None:
        self.owner, self.page, self.content = owner, page, content


def _owned_package(
    mc: Path, package: str, slug: str, version: int, digest: str, index: "BoundIndex"
) -> _Owned:
    """Judge the destination's ``artifacts/<package>`` for *slug* and check its pinned page.

    Its metadata is read first, alone: a package that is no dashboard, or is a live
    pointer (:func:`_not_a_bound_dashboard`), is ``artifact_rejected`` before any of its
    content is read. It is this Bot's only when BOTH its live version (what
    ``scan_bound_dashboards`` and ``resolve_bound_slug`` read, taken from *index* when
    its complete scan read the package, else read here) and the PINNED *version* (what
    the record would serve) are dashboards bound to ``crewmate:<slug>``
    (:func:`_binding_owner`). A rebind writes a new ``bound_to`` without a new version,
    so the pinned version alone does not say whose the package is now. The pinned
    version is read and decoded once, and its envelope is the page checked by
    :func:`_page_of`. A package of this Bot's lacking the pinned version has page
    ``artifact_missing``.
    """
    from kiro_crew.artifact_store.model import ArtifactError, ArtifactNotFoundError

    store = _artifact_store(mc)
    binding = f"crewmate:{slug}"
    try:
        meta = store._load_meta(package)
    except (ArtifactError, OSError, ValueError, TypeError, RecursionError):
        # Whose a directory with no readable record is cannot be told.
        return _Owned("unreadable_record")
    if _not_a_bound_dashboard(meta):
        return _Owned("artifact_rejected")
    target = index.live_binding(package)
    if target is None:
        from kiro_crew.artifact_store import dashboard_package

        # Read by the scan's own rule (dashboard_package.live_binding_of), so a
        # complete and an incomplete scan judge one package the same way.
        try:
            live = store.get(package)
            target = dashboard_package.live_binding_of(live.content or "")
        except (ArtifactError, OSError, ValueError, RecursionError):
            return _Owned("unreadable_record")
    owner = _binding_owner(target, binding)
    if owner:
        return _Owned(owner)
    try:
        pinned = store.get(package, version=version)
    except ArtifactNotFoundError:
        return _Owned(page="artifact_missing")
    except (ArtifactError, OSError, ValueError):
        return _Owned("unreadable_record")
    raw, target = _envelope(pinned.content or "")
    owner = (
        "artifact_rejected" if _not_a_bound_dashboard(pinned) else _binding_owner(target, binding)
    )
    if owner:
        return _Owned(owner)
    page, content = _page_of(raw, slug, digest)
    return _Owned(page=page, content=content)


class BoundIndex:
    """The destination's dashboard artifacts by the binding they name, read once per import.

    One ``dashboard_package.scan_bound_dashboards`` of the destination store, the scan
    ``dashboard_package.resolve_bound_slug`` answers one binding from, kept for every
    Bot of the import. A record that cannot be read, a library over the cap, or a
    scan that raises leaves every answer unproven (:meth:`refusal`). The packages this
    import publishes are each bound to its own Bot, whose answer was already given, so
    the index is not read again.
    """

    def __init__(self, mc: Path) -> None:
        self._mc = mc
        self._bound: dict[str, list[str]] | None = None
        self._incomplete = False

    def _load(self) -> dict[str, list[str]]:
        if self._bound is not None:
            return self._bound
        from kiro_crew.artifact_store import dashboard_package

        bound: dict[str, list[str]] = {}
        try:
            scan = dashboard_package.scan_bound_dashboards(_artifact_store(self._mc))
            bound, self._incomplete = scan.bound, not scan.complete
        except Exception:  # noqa: BLE001 - unproven: nothing is published next to it
            logger.warning("import: the bound dashboards could not be listed", exc_info=True)
            self._incomplete = True
        self._bound = bound
        return bound

    def refusal(self, package: str, slug: str) -> str:
        """Why *package* must not be published for *slug*: another artifact is bound to it.

        ``""`` when no dashboard artifact here is bound to ``crewmate:<slug>`` (or the
        one bound is *package* itself); ``another_package_of_this_bot`` when another
        one is, or more than one is (each is bound to this same Bot: the index is keyed
        by its binding); ``unreadable_record`` when the scan could not prove either.
        Publishing, or serving a page from *package*, would leave two packages bound to
        one Bot, and the dashboard would answer neither.
        """
        found = self._load().get(f"crewmate:{slug}", [])
        if len(found) > 1:
            return _OWN_OTHER
        if self._incomplete:
            return "unreadable_record"
        return "" if found in ([], [package]) else _OWN_OTHER

    def live_binding(self, package: str) -> str | None:
        """The binding *package*'s live version names, as the scan read it.

        ``None`` when the scan is not complete or did not read *package* bound to
        anything (it may have appeared since): the caller reads it itself.
        """
        bound = self._load()
        if self._incomplete:
            return None
        return next((target for target, found in bound.items() if package in found), None)


#: The names a rollback renames a published record and package back to in the Bot's
#: own stage: fixed per item, never derived from the archive's package slug.
ROLLED_BACK_RECORD = "rolled-back-record"
ROLLED_BACK_PACKAGE = "rolled-back-package"


def _roll_back(mc: Path, parts: Sequence[str], stage: Path, name: str) -> bool:
    """Rename what this import published at ``mc/parts`` back into the stage as *name*.

    True when it moved; a failure is logged and leaves it published.
    """
    try:
        _rename_noreplace(mc, tuple(parts), stage.parent, (stage.name, name))
    except _FAILURES:
        logger.warning("import: %s stayed published", "/".join(parts), exc_info=True)
        return False
    return True


def _restore_kept_records_artifact(
    snap: Path, mc: Path, bot: ImportedBot, stage: Path, index: BoundIndex
) -> PartOutcome:
    """This Bot's dashboard record is kept; bring back the artifact it pins if absent.

    The pin is the DESTINATION record's. When ``artifacts/<P>/`` is here but the
    page snapshot it pins is missing, the page is rebuilt from that artifact only when
    it is this Bot's and its pinned page passes the gate (:func:`_owned_package`) and
    no other package here is bound to the Bot (:meth:`BoundIndex.refusal`, the check
    the publish path makes). When ``artifacts/<P>/`` is missing and the archive carries
    ``bot-artifacts/<P>/`` with the pinned version, it is published only when the
    archive does not bind it to another binding (:func:`_staged_held`) and that same
    check passes: the carried package is staged, gated by :func:`_check_staged_artifact`
    against that pin, and published add-only; the page snapshot is then written from
    the checked bytes. An ``artifacts/<P>/`` that appears before the publish is judged
    as one already here. A page or package brought back is ``restored``. One that could
    not be brought back for any reason (a refusal, a gate, a failed write, a page
    write failing after its package was published) leaves the part not whole:
    ``page_not_rebuilt`` with that reason. ``kept`` with no reason only when there was
    nothing to bring back: the page is in place, or the package is missing and the
    archive holds nothing to restore it from.
    """
    from kiro_crew.artifacts import slug_is_well_formed
    from kiro_crew.dashboard_templates import instance

    kept = PartOutcome(bot.name, "dashboard", "kept")
    try:
        record = instance.read(bot.slug)
    except (OSError, ValueError, instance.InstanceError):
        return kept
    package, version, digest = record.package_slug, record.package_version, record.package_digest
    if not package or not digest or not slug_is_well_formed(package):
        return kept

    def outcome(reason: str) -> PartOutcome:
        if reason:
            return PartOutcome(bot.name, "dashboard", "page_not_rebuilt", reason)
        return PartOutcome(bot.name, "dashboard", "restored")

    def from_here() -> PartOutcome:
        if not _page_missing(bot.slug, digest):
            return kept
        here = _judge_here(mc, bot, (package, version, digest), index, owned=True)
        return outcome(here.refusal or here.page or _write_page(bot.slug, here.here))

    if os.path.lexists(mc / "artifacts" / package):
        return from_here()
    if not _archive_carries(snap, package, version):
        return kept
    art_dir = stage / "artifacts" / package
    try:
        _stage_artifact(snap, stage, package)
    except _FAILURES:
        logger.warning("import: %r's dashboard artifact was not staged", bot.slug, exc_info=True)
        return outcome("write_failed")
    reason = _staged_held(art_dir, bot.slug) or index.refusal(package, bot.slug)
    content = ""
    if not reason:
        reason, content = _check_staged_artifact(art_dir, package, bot.slug, version, digest)
    if not reason:
        reason = _publish_artifact(stage, mc, package)
        if reason == "exists":
            # An artifacts/<P> appeared since there was none: judged as one here.
            return from_here()
    return outcome(reason or _write_page(bot.slug, content))


class _Staged:
    """What :func:`_stage_dashboard` decided for one record and the package it pins.

    ``refusal``: why the record is not installed (``""``: it is). ``pin``: the
    record's ``(package, version, digest)``. ``checked``: the gated page of a staged
    package to publish, ``""`` when there is none. ``here``: the checked page of this
    Bot's own ``artifacts/<P>`` already here. ``page``: why the record's page cannot be
    rebuilt although the record installs (``""``: it can, from ``checked`` or ``here``).
    """

    def __init__(
        self,
        refusal: str = "",
        pin: tuple[str, int, str] = ("", 0, ""),
        checked: str = "",
        page: str = "",
        here: str = "",
    ) -> None:
        self.refusal, self.pin, self.checked, self.page = refusal, pin, checked, page
        self.here = here


def _judge_here(
    mc: Path, bot: ImportedBot, pin: tuple[str, int, str], index: BoundIndex, *, owned: bool
) -> _Staged:
    """The decision for a record whose pinned ``artifacts/<P>`` is already here.

    Only this Bot's own package (:func:`_owned_package`) lets the record install; one
    that is no bound dashboard is ``artifact_rejected``, and any other package an
    unowned slug pins is ``held_by_other_bot``. Its page is rebuilt from it only when
    it passes the gate and no other package here is bound to the Bot
    (:meth:`BoundIndex.refusal`); otherwise that is the page's reason.
    """
    package, version, digest = pin
    here = _owned_package(mc, package, bot.slug, version, digest, index)
    if here.owner or not owned:
        return _Staged(here.owner or _HELD, pin)
    page = here.page or index.refusal(package, bot.slug)
    return _Staged(pin=pin, page=page, here="" if page else here.content)


def _stage_dashboard(
    snap: Path, mc: Path, bot: ImportedBot, stage: Path, index: BoundIndex, *, owned: bool
) -> _Staged:
    """Stage the record and the package it pins, and decide what each may do.

    The record installs unless it cannot be read, pins no well-formed package slug, or
    pins an ``artifacts/<P>`` here that is not this Bot's own (:func:`_judge_here`).
    When no ``artifacts/<P>`` is here, the archive must carry ``bot-artifacts/<P>/``
    with the pinned version (``artifact_missing`` otherwise, before the bound
    dashboards here are listed). A carried package whose live page the archive binds to
    another binding keeps the record out (``held_by_other_bot``, :func:`_staged_held`),
    so two archive Bots pinning one package end the same in either order. Otherwise
    :meth:`BoundIndex.refusal` must prove no other package is bound to the Bot, and the
    staged package must pass the gate; whatever keeps it from being published is the
    page's reason, not the record's. Raises one of :data:`_FAILURES` when staging fails.
    """
    from kiro_crew.artifacts import slug_is_well_formed

    src_dash = PurePath(snap.name, MEMBERS_DIR_NAME, bot.slug, "dashboard")
    admitted = [
        key
        for key in _list_files(snap.parent, src_dash)
        if carried_member_file(("dashboard",) + key)
    ]
    if ("instance.json",) not in admitted:
        return _Staged("unreadable_record")
    for key in admitted:
        _copy_file(snap.parent, src_dash.joinpath(*key), stage, PurePath("dashboard", *key))
    pin = package, version, digest = _staged_pin(stage / "dashboard" / "instance.json")
    if not package:
        return _Staged(pin=pin)
    # The archive names P; the store's own validator decides it is a slug before any
    # ``artifacts/<P>`` path is built from it.
    if not slug_is_well_formed(package):
        return _Staged("artifact_rejected", pin)
    if os.path.lexists(mc / "artifacts" / package):
        return _judge_here(mc, bot, pin, index, owned=owned)
    if not _archive_carries(snap, package, version):
        return _Staged(pin=pin, page="artifact_missing")
    art_dir = stage / "artifacts" / package
    _stage_artifact(snap, stage, package)
    held = _staged_held(art_dir, bot.slug)
    if held:
        return _Staged(held, pin)
    reason = index.refusal(package, bot.slug)
    if reason:
        return _Staged(pin=pin, page=reason)
    reason, checked = _check_staged_artifact(art_dir, package, bot.slug, version, digest)
    return _Staged(pin=pin, checked=checked, page=reason)


def _install_dashboard(
    snap: Path, mc: Path, bot: ImportedBot, stage: Path, index: BoundIndex, *, owned: bool
) -> PartOutcome:
    """Install the record, and the package it pins when that one may be published.

    Order: the record and its package are staged and the package gated
    (:func:`_stage_dashboard`); the package is published when it passed; then the
    record; then the page is written. A record pinning an ``artifacts/<P>`` here that is
    not this Bot's is ``not_restored`` (its reason). A package that cannot be published
    (missing from the archive, refused by the gate, its binding's uniqueness unproven)
    never is, but the record still installs and the part is ``page_not_rebuilt`` with
    that reason; so is a record whose page cannot be rebuilt from this Bot's own
    ``artifacts/<P>`` here (``artifact_missing`` when it lacks the pinned version).
    A record that fails to install or does not read back takes the package this
    import published back into the stage (:data:`ROLLED_BACK_RECORD`, then, only once
    the record moved, :data:`ROLLED_BACK_PACKAGE`), so no package nobody's record pins
    is left behind; the part is then ``not_restored``.
    """
    slug = bot.slug
    dest_member = (MEMBERS_DIR_NAME, slug)
    dest_dash = (*dest_member, "dashboard")

    def row(status: str, reason: str = "") -> PartOutcome:
        return PartOutcome(bot.name, "dashboard", status, reason)

    if os.path.lexists(mc.joinpath(*dest_dash)):
        if not owned:
            return _present(bot, "dashboard", owned)
        return _restore_kept_records_artifact(snap, mc, bot, stage, index)
    try:
        staged = _stage_dashboard(snap, mc, bot, stage, index, owned=owned)
    except _FAILURES:
        logger.warning("import: %r's dashboard was not staged", slug, exc_info=True)
        return row("not_restored", "write_failed")
    if staged.refusal:
        return row("not_restored", staged.refusal)
    package = staged.pin[0]
    published: tuple[str, str] | None = None
    if staged.checked:
        result = _publish_artifact(stage, mc, package)
        if result == "exists":
            # An artifacts/<P> appeared since the stage judged there was none.
            staged = _judge_here(mc, bot, staged.pin, index, owned=owned)
            if staged.refusal:
                return row("not_restored", staged.refusal)
        elif result:
            staged.checked, staged.page = "", result
        else:
            published = ("artifacts", package)

    def roll_back_package() -> None:
        if published is not None:
            _roll_back(mc, published, stage, ROLLED_BACK_PACKAGE)

    try:
        _ensure_real_dir(mc, dest_member)
    except _FAILURES:
        # A FileExistsError here is a non-directory at ``members/<slug>``, not a record.
        logger.warning("import: no member directory for %r", slug, exc_info=True)
        roll_back_package()
        return row("not_restored", "write_failed")
    try:
        _rename_noreplace(stage.parent, (stage.name, "dashboard"), mc, dest_dash)
    except FileExistsError:
        roll_back_package()
        return _present(bot, "dashboard", owned)
    except _FAILURES:
        logger.warning("import: %r's dashboard was not installed", slug, exc_info=True)
        roll_back_package()
        return row("not_restored", "write_failed")
    if not _record_state_ok(slug):
        # The package moves back only once the record that pins it did.
        if not _roll_back(mc, dest_dash, stage, ROLLED_BACK_RECORD):
            return row("page_not_rebuilt", "unreadable_record")
        roll_back_package()
        return row("not_restored", "unreadable_record")
    if not package:
        return row("restored")
    page = staged.page or _write_page(slug, staged.checked or staged.here)
    return row("page_not_rebuilt", page) if page else row("restored")


def _install_briefing(
    snap: Path, mc: Path, bot: ImportedBot, stage: Path, names: Sequence[str], *, owned: bool
) -> PartOutcome:
    """Install the carried single files all or nothing; any failure fails the part.

    Every file the destination lacks is staged first, and none is published unless
    all of them staged. When a publish then fails, each file this import already
    published for the part is removed again. A file the destination has is kept when
    the entry is this Bot's own. When it is not, nothing of the part is written next
    to it: the part is ``held_by_other_bot``.
    """
    slug = bot.slug
    if not owned and any(os.path.lexists(mc / MEMBERS_DIR_NAME / slug / n) for n in names):
        return _present(bot, "briefing", owned)
    pending: list[tuple[tuple[str, ...], Path, bytes]] = []
    for name in names:
        dest = (MEMBERS_DIR_NAME, slug, name)
        if os.path.lexists(mc.joinpath(*dest)):
            continue
        try:
            staged = PurePath("briefing", name)
            _copy_file(
                snap.parent, PurePath(snap.name, MEMBERS_DIR_NAME, slug, name), stage, staged
            )
            pending.append((dest, stage / staged, _read_staged(stage / staged, MAX_FILE_BYTES)))
            _ensure_real_dir(mc, dest[:-1])
        except _FAILURES:
            # A FileExistsError here is a leftover stage entry, not a live file.
            logger.warning("import: %r's %s was not staged", slug, name, exc_info=True)
            return PartOutcome(bot.name, "briefing", "not_restored", "write_failed")
    created: list[tuple[Sequence[str], tuple[int, int]]] = []
    for target, staged_file, data in pending:
        try:
            created.append((target, _publish_file(data, staged_file, mc, target)))
        except FileExistsError:
            if owned:
                continue
            _remove_created(mc, created)
            return _present(bot, "briefing", owned)
        except _FAILURES:
            logger.warning("import: %r's %s was not installed", slug, target[-1], exc_info=True)
            _remove_created(mc, created)
            return PartOutcome(bot.name, "briefing", "not_restored", "write_failed")
    return PartOutcome(bot.name, "briefing", "restored" if created else "kept")


def _carried(snap: Path, slug: str) -> tuple[bool, list[str]]:
    """Whether the archive carries *slug*'s dashboard, and which briefing files."""
    if not safe_path_component(slug) or _entry_kind(snap / MEMBERS_DIR_NAME, slug) != "dir":
        return False, []
    src_member = snap / MEMBERS_DIR_NAME / slug
    has_dash = _entry_kind(src_member, "dashboard") == "dir"
    return has_dash, [n for n in BRIEFING_FILES if _entry_kind(src_member, n) == "file"]


def _failed_rows(bot: ImportedBot, has_dash: bool, briefs: Sequence[str]) -> list[PartOutcome]:
    return [
        (
            PartOutcome(bot.name, part, "not_restored", "write_failed")
            if carried
            else PartOutcome(bot.name, part, "none")
        )
        for part, carried in (("dashboard", has_dash), ("briefing", bool(briefs)))
    ]


def _install_one(
    snap: Path, mc: Path, bot: ImportedBot, index: BoundIndex, *, refused: bool, owned: bool
) -> list[PartOutcome]:
    has_dash, briefs = _carried(snap, bot.slug)
    if refused or not (has_dash or briefs):
        return _failed_rows(bot, has_dash, briefs)
    try:
        stage = _new_stage(snap.parent)
    except _FAILURES:
        logger.warning("import: no stage for %r's stores", bot.slug, exc_info=True)
        return _failed_rows(bot, has_dash, briefs)
    dashboard = (
        _install_dashboard(snap, mc, bot, stage, index, owned=owned)
        if has_dash
        else PartOutcome(bot.name, "dashboard", "none")
    )
    briefing = (
        _install_briefing(snap, mc, bot, stage, briefs, owned=owned)
        if briefs
        else PartOutcome(bot.name, "briefing", "none")
    )
    return [dashboard, briefing]


def install_bot_stores(
    snap: Path,
    mc: Path,
    bots: Sequence[ImportedBot],
    *,
    allow_unpinned: bool,
    owned_slugs: frozenset[str] | None = None,
) -> list[PartOutcome]:
    """Install every imported Bot's dashboard and briefing stores; one row per part.

    Add-only in both import modes: a store the destination has is never written
    over. It is reported ``kept`` when the entry is this Bot's own -- *owned_slugs* is
    ``None`` or holds the Bot's slug -- and ``not_restored`` / ``held_by_other_bot``
    otherwise. The same rule decides whether the page is rebuilt from an
    ``artifacts/<P>`` the destination already has. Each store is all or nothing.
    Never raises for one Bot: a failure is that Bot's row.
    """
    from kiro_crew.snapshot import _staging_is_pinned

    try:
        _staging_is_pinned(allow_unpinned=allow_unpinned, what="Bot store import")
        refused = False
    except pinned_fs.PinnedPathRefusal:
        refused = True
    out: list[PartOutcome] = []
    # Which dashboard artifacts here are bound to which Bot, listed once for all Bots.
    index = BoundIndex(mc)
    for bot in bots:
        try:
            owned = owned_slugs is None or bot.slug in owned_slugs
            out.extend(_install_one(snap, mc, bot, index, refused=refused, owned=owned))
        except Exception:
            logger.warning("import: %r's stores were not installed", bot.slug, exc_info=True)
            out.extend(
                PartOutcome(bot.name, part, "not_restored", "write_failed")
                for part in ("dashboard", "briefing")
            )
    return out
