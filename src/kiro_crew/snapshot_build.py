"""Building a snapshot bundle: the size estimate, the staging pass and the published tarball.

:func:`_build_snapshot` stages the selected components of the data home into a temporary
tree, writes ``MANIFEST.json`` and publishes the owner-only tarball, and
:func:`_estimate_selected_bytes` sizes the same selection before it runs.
:func:`kiro_crew.snapshot.snapshot_main` calls both and turns a refusal into an exit code.
The pinned copy, the consistent SQLite capture and the bundle format live in
:mod:`kiro_crew.snapshot_archive`. The helpers and the clock a test replaces on
:mod:`kiro_crew.snapshot` are read through it when used
(:func:`kiro_crew.snapshot_components._facade`), so a patch there reaches the calls here.
"""

from __future__ import annotations

import json
import os
import socket
import stat as _stat
import tarfile
import tempfile
from contextlib import ExitStack
from datetime import timezone
from pathlib import Path, PurePosixPath

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.member_memory_backup import hold_stores_for_read
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME
from kiro_crew.snapshot_archive import (
    MANIFEST_VERSION,
    _copy_database_consistently,
    _data_filter,
    _refuse_oversized_tree,
    _staging_ignore,
)
from kiro_crew.snapshot_components import (
    COMPONENTS,
    Purpose,
    UnsafeComponentRoot,
    _facade,
    safe_tree_root,
)


def _fsize(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _estimate_selected_bytes(mc: Path, selected: list[str]) -> tuple[int, int]:
    """Bytes the *selected* components would stage, and how many entries refused.

    Scoped to the SELECTION, not the data home. The whole-home walk this replaces
    stat-ed every file under ``mc`` before staging, which had two consequences: the
    number described an archive nobody asked for whenever ``--components`` narrowed
    it, and a data home holding one entry this process may not stat -- a
    platform-protected key at the root, which no component declares -- ended the
    command with a traceback that ``--components`` could not route around, because
    the walk ran first.

    Overlapping trees (``memory`` names ``workspace/memory`` while ``workspace``
    names the whole tree) are counted ONCE, by the same ancestor collapse the staging
    pass makes: the estimate matches what staging writes, one refused entry in the
    overlap is reported once rather than twice, and the shared subtree is walked once.

    Deliberately does NOT apply ``_staging_ignore``'s exclusions. The estimate feeds
    one warning about how long this may take, and reading the ignore rules per
    directory to shave a sidecar off a size nobody acts on costs more than it buys;
    over-estimating is the safe direction for a 'this may be slow' notice.

    Symlinks are not followed and not counted, matching the staging walk, which skips
    them. The second return value is the number of entries REFUSED for permission, for
    a caller that wants to say so; every other error is raised, so a failing disk ends
    the command here exactly as it would during staging rather than being folded into
    a count that reads like a handful of protected paths.
    """
    total = 0
    unreadable = 0
    seen: set[str] = set()

    def _count(path: Path) -> None:
        nonlocal total, unreadable
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            # A component names files most homes do not have. Absent is not a refusal
            # and must not be reported as one.
            return
        except PermissionError:
            unreadable += 1
            return
        if _stat.S_ISREG(st.st_mode):
            total += st.st_size

    def _on_walk_error(exc: OSError) -> None:
        nonlocal unreadable
        # A tree a component names but this home does not have is the normal case, and
        # `os.walk` reports it here rather than raising -- counting it would report
        # 'unreadable' on a fresh install where nothing was refused.
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
            return
        # Only the permission class is absorbed, matching the staging walks. Anything
        # else -- an EIO, a disconnected mount -- is raised out of the estimate rather
        # than folded into a count, because a number cannot say 'the storage is
        # failing' and the operator would read it as a handful of protected paths.
        if not isinstance(exc, PermissionError):
            raise exc
        unreadable += 1

    trees: list[str] = []
    for comp in selected:
        spec = COMPONENTS[comp]
        for f in spec.files:
            _count(mc / f)
        trees.extend(spec.trees)
    # A tree already covered by an ANCESTOR in the same selection is dropped, the same
    # collapse the staging pass makes. `seen` already stops a file's bytes being added
    # twice, but nothing deduped the REFUSALS: walking the overlap twice met one
    # refused directory twice and reported it as two. It also stops the shared subtree
    # being walked twice on a selection like `memory,workspace`.
    covered = set(trees)
    for tree in trees:
        if any(
            other != tree and PurePosixPath(tree).is_relative_to(PurePosixPath(other))
            for other in covered
        ):
            continue
        root = mc / tree
        # A tree ROOT that is a link is not walked. Staging refuses one outright
        # (`safe_tree_root`), but only later, so without this the estimate is the one
        # pass that follows it -- reading a tree the components never declared and
        # reporting its size as theirs.
        if pinned_fs.is_reparse_point(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error):
            # Deeper reparse points are pruned by hand. `os.walk` declines a SYMLINK
            # directory on its own, but a Windows junction is not a symlink to it, so on
            # that platform it descends -- into whatever the junction names, which the
            # components never declared. Nothing but a size leaves this function, so the
            # exposure is a number, not bytes or names; it is still a walk of a tree the
            # operator did not select, and the staging pass refuses the same entry.
            # Edited in place, which is the contract `os.walk` offers for pruning.
            dirnames[:] = [d for d in dirnames if not pinned_fs.is_reparse_point(Path(dirpath) / d)]
            for fn in filenames:
                _count(Path(dirpath) / fn)
    return total, unreadable


def _build_snapshot(
    mc: Path,
    out: Path,
    name: str,
    *,
    selected: list[str] | None = None,
    purpose: Purpose = Purpose.BACKUP,
    root_name: str | None = None,
    allow_unpinned: bool = False,
    skipped_out: list[dict[str, str]] | None = None,
    max_archive_bytes: int | None = None,
) -> Path:
    """Stage the data home into a temporary tree and publish it as one tarball.

    Extracted from ``snapshot_main`` so the staging pass has a boundary a refusal can
    be contained at: everything in here either produces a finished archive or raises,
    and the caller turns a :class:`kiro_crew.pinned_fs.PinnedPathRefusal` into an exit
    code rather than a traceback.

    *selected* names the components to stage and *purpose* what the bundle is for; both
    are resolved by the caller so this function never has to decide policy. Staging is
    per-component rather than over one flat file table, which is what makes
    ``--components memory`` a complete memory backup instead of a subset of one.

    They DEFAULT rather than being required, because callers that predate the component
    seam ask for a whole-home archive and should keep getting one: an omitted *selected*
    means every component, which is what ``snapshot`` without ``--components`` has always
    produced. Making them mandatory broke those callers with a TypeError instead.

    *skipped_out*, when given, is extended with the same records this function writes to
    ``MANIFEST.json``'s ``skipped``. An out-parameter rather than a second return value
    on purpose: the return type is a ``Path`` that several tests use directly, so
    widening it would be a seam move with fallout, and the caller needs only to know
    WHETHER anything was omitted.

    *name* names the TARBALL and *root_name* the directory inside it, and they are two
    parameters rather than one because a selective bundle marks only the inner directory.
    Collapsing them renamed the tarball too, and ``--list``, pruning and ``--keep`` all
    glob ``kirocrew-snapshot-*.tar.gz`` -- so every partial bundle became invisible to
    rotation and accumulated without bound. Defaults to *name* for a complete bundle.

    *max_archive_bytes* is the declared-content bound ``kirocrew restore`` applies, and
    defaults to the same ``_MAX_ARCHIVE_BYTES`` it reads. The staged tree is held to it
    before the tarball is created, so a bundle restore would refuse is never published:
    :class:`kiro_crew.snapshot_archive._ArchiveTooLarge` is raised with nothing written.
    """
    facade = _facade()
    arcname = root_name or name
    if selected is None:
        selected = list(COMPONENTS)
    # Decided BEFORE anything is staged, not per-tree. An earlier revision gated
    # inside _copytree_safe only, so a data home with core files and no trees staged
    # them on a platform that cannot pin without ever consulting the opt-in -- the
    # gate was reachable only through a path that happened to exist. Asking once, up
    # front, is also what makes the manifest's "staging" value true of the whole
    # archive rather than of whichever component ran last.
    pinned = facade._staging_is_pinned(allow_unpinned=allow_unpinned, what="data home")

    # Every skip is recorded, not just printed. Printing alone leaves a snapshot that
    # omitted a hardlinked or symlinked file reporting success with a console warning
    # and nothing in the archive -- a silent partial. Paths are stored relative to the
    # data home so the record names the file without carrying the absolute layout of
    # the machine into an archive that may be moved somewhere else.
    skipped: list[dict[str, str]] = []

    def _record_skip(reason: str, path: str) -> None:
        try:
            rel = str(Path(path).relative_to(mc))
        except ValueError:
            rel = Path(path).name
        skipped.append({"reason": reason, "path": rel})
        facade._report_skip(reason, path)

    with tempfile.TemporaryDirectory() as work:
        # Locked down as a DIRECTORY before anything is staged into it. This tree holds the
        # operator's whole data home in the clear while the archive is built, and Windows
        # inherits the parent DACL rather than honouring a mode, so the POSIX 0700 mkdtemp
        # gives is not the guarantee on every platform this runs on.
        platform_compat.restrict_dir_to_owner(work)
        stage = Path(work) / arcname
        # Unconditionally, before any component runs. A file-only selection whose files
        # are all absent (a fresh home with `--components crons`) stages nothing, and the
        # manifest write below would then fail on a missing directory -- an empty bundle
        # is a valid outcome, a crash is not.
        #
        # Only the ROOT is created. A tree's own directory is created by the staging
        # primitive, which refuses a destination name that already exists in a tree it
        # made -- so pre-creating them here is what made the overlap collide.
        stage.mkdir(parents=True, exist_ok=True)

        # Core files. Copied through the pinned primitive rather than shutil.copy2:
        # copy2 dereferences a hardlink into ordinary-looking regular bytes, and the
        # tar pass's hardlink screen then has no link left to reject, so an alias
        # planted at a core file's name would have shipped as content. The name-based
        # islink check is gone with it -- it answered about a name, and the open that
        # followed could land on a different inode.
        #
        # Both ends are pinned where the platform allows it: the data home is opened
        # once and every core file is opened relative to THAT descriptor, so an
        # ancestor of the data home swapped mid-run cannot redirect the read. Opening
        # `mc / f` by name was a real gap in the first revision of this PR, caught in
        # review -- the file's own O_NOFOLLOW says nothing about the directories walked
        # to reach it.
        mc_fd = pinned_fs.open_dir_pinned(mc, what="data home") if pinned else None
        try:
            for comp in selected:
                for f in COMPONENTS[comp].files:
                    src = mc / f
                    if mc_fd is not None:
                        # Asked through the descriptor. `is_regular_at` lstats relative to
                        # mc_fd, so it rejects a link or a Windows junction by itself --
                        # a reparse point is not S_ISREG -- and there is no name for a
                        # concurrent swap to redirect.
                        #
                        # My own AST ratchet flagged this very line last round and I
                        # dismissed it as one of the legitimate by-name fallback sites
                        # without checking. It was not: this loop holds mc_fd. Review
                        # caught what I had waved off.
                        # A core file that simply is not there is not an omission and must
                        # stay out of MANIFEST.json -- most components ship only a subset.
                        # Only a name that EXISTS and is not a regular file is a skip worth
                        # recording, so the two cases are separated rather than collapsed
                        # into one `is_regular_at` call. Caught by the manifest test.
                        live_st = pinned_fs.stat_at(mc_fd, f)
                        if live_st is None:
                            continue
                        if not _stat.S_ISREG(live_st.st_mode):
                            _record_skip(pinned_fs.SKIP_NOT_REGULAR, str(src))
                            continue
                    else:
                        if not src.is_file():
                            continue
                        # Reserved for the fallback, where there is no descriptor to ask.
                        # `is_file()` and, on a platform without O_NOFOLLOW, `os.open`
                        # both FOLLOW a link, so neither can screen one: on the declared
                        # by-name path a core filename pointed at a credential would have
                        # had its bytes copied into the archive. `is_reparse_point` also
                        # catches a Windows junction, which `islink` does not report.
                        if pinned_fs.is_reparse_point(src):
                            _record_skip(pinned_fs.SKIP_SYMLINK, str(src))
                            continue
                    if f.endswith(".db"):
                        # A component file may sit under a subdirectory
                        # (`workspace/knowledge/knowledge.db`), so the parent is created
                        # per file rather than assumed from the component's tree roots.
                        (stage / f).parent.mkdir(parents=True, exist_ok=True)
                        # Through the SAME hardened copy the tree pass uses, so the two
                        # cannot drift: descriptor-verified chain, percent-escaped URI,
                        # `mode=ro`, and "not a database" told apart from "cannot read
                        # this database". Without it the core path is an unhardened
                        # SQLite read on the creation path, connecting READ-WRITE to
                        # the live name.
                        #
                        # `mc_fd` screened this name a few lines up and cannot be handed
                        # to SQLite, which takes only a path; the chain check inside the
                        # helper is what makes the ANCESTORS non-redirectable, and the
                        # residual final-name window is documented there.
                        # `require_database=True` makes every non-success outcome a raise,
                        # so there is no outcome to branch on here: a declared component
                        # file is either copied consistently or the snapshot fails. Both
                        # degradations it replaces -- byte-copying a corrupt database, and
                        # omitting an unverifiable one -- let the command succeed, which
                        # lets `--keep` prune the last good archive in favour of one that
                        # cannot be restored.
                        _copy_database_consistently(
                            src,
                            stage / f,
                            root=mc,
                            rel_parts=PurePosixPath(f).parts,
                            require_database=True,
                        )
                    elif mc_fd is not None:
                        (stage / f).parent.mkdir(parents=True, exist_ok=True)
                        pinned_fs.copy_file_pinned(
                            str(src),
                            str(stage / f),
                            dir_fd=mc_fd,
                            name=f,
                            on_skip=_record_skip,
                        )
                    else:
                        pinned_fs.copy_file_pinned(str(src), str(stage / f), on_skip=_record_skip)
        finally:
            if mc_fd is not None:
                os.close(mc_fd)

        # Trees. Selections overlap by design -- `memory` names workspace/memory while
        # `workspace` names the whole tree -- so the pairs are collected first and a tree
        # already covered by an ANCESTOR in the same selection is dropped.
        #
        # An earlier revision staged the overlap twice and relied on the second write being
        # identical to the first. That worked against a `shutil.copytree(dirs_exist_ok=True)`
        # and does NOT work here: the shared staging primitive refuses a destination name
        # that already exists in a tree this operation created, because it cannot tell a
        # directory it made from a link someone planted. Not copying the same bytes twice is
        # the better answer anyway.
        wanted_trees: list[tuple[str, str]] = []
        for comp in selected:
            for tree in COMPONENTS[comp].trees:
                # Guarded HERE, while collecting, rather than in the staging loop below.
                # An unsafe root must FAIL the snapshot, not be skipped: skipping produced
                # the worst possible artefact -- a bundle whose manifest declares `memory`
                # while the markdown trees are silently absent, so the operator believes
                # they are covered and only finds out when they try to recover. A backup
                # that lies about its contents is worse than no backup.
                #
                # safe_tree_root returns None only for an unsafe or unresolvable root -- a
                # root that simply does not exist yet is fine -- so this cannot fire on a
                # fresh data home.
                if safe_tree_root(mc / tree, what="component root", home=mc) is None:
                    raise UnsafeComponentRoot(
                        f"component {comp!r} names the tree {tree!r}, which does not "
                        f"resolve inside the data home. Refusing to write a bundle that "
                        f"would claim to contain {comp!r} without it -- inspect that path "
                        f"(it is usually a symlink) and re-run."
                    )
                wanted_trees.append((comp, tree))
        covered = {t for _, t in wanted_trees}
        staged_trees = [
            (comp, tree)
            for comp, tree in wanted_trees
            if not any(
                other != tree and PurePosixPath(tree).is_relative_to(PurePosixPath(other))
                for other in covered
            )
        ]
        for comp, tree in staged_trees:
            src_dir = mc / tree
            dst_dir = stage / tree
            # Classified by an explicit stat, because `Path.is_dir()` answers False for a
            # path it cannot stat -- so a tree ROOT the process may not reach took the very
            # same branch as a root that is simply absent, and an entire selected tree left
            # the bundle with nothing in `skipped`. The manifest then still declared the
            # component present, the caller's retention guard saw no omission, and `--keep`
            # pruned the last complete archive: silent, unbounded loss. A refusal here is
            # the same failure the per-entry screens already record -- only the loop's own
            # root probe had been left behind. `safe_tree_root` above does not foreclose
            # this: its `resolve(strict=False)` is best-effort and does not raise on a
            # refusal, so it returns a path and the root reaches this line unclassified.
            try:
                root_st: os.stat_result | None = os.stat(src_dir)
            except PermissionError:
                _record_skip(pinned_fs.SKIP_UNREADABLE_ENTRY, str(src_dir))
                continue
            except FileNotFoundError:
                # ONLY absent, which is ordinary on a fresh data home. Every other errno
                # propagates: `EIO` on a failing disk, `ESTALE` on a dropped NFS handle,
                # `ENOTCONN` on a disconnected mount, `ENOTDIR` on a data home whose
                # ancestor is a file. Folding those in here omitted a whole selected tree
                # with nothing in `skipped`, so the manifest still declared the component
                # present and the caller's guard pruned the last complete archive -- the same
                # silent loss this probe exists to stop, one errno class over. A backup
                # target is exactly where those errnos happen, so this is the last branch
                # that may hide them; the estimate already raises on the same class.
                root_st = None
            if root_st is None or not _stat.S_ISDIR(root_st.st_mode):
                continue
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            with ExitStack() as admission:
                if tree == MEMORY_STORES_DIR_NAME:
                    # The manifest copy and database backup must see the same generation.
                    admission.enter_context(hold_stores_for_read(src_dir))
                facade._copytree_safe(
                    src_dir,
                    dst_dir,
                    allow_unpinned=allow_unpinned,
                    on_skip=_record_skip,
                    ignore=_staging_ignore(tree, src_dir),
                    # `_record_skip` puts every one of these in MANIFEST.json, which
                    # is what makes tolerating them honest rather than silent.
                    skip_unreadable=True,
                )
                # Include WAL-resident rows through SQLite's backup API, without copying
                # sidecars that belong to the live database rather than this snapshot.
                facade._restage_databases(src_dir, dst_dir, bundle_root=stage, on_skip=_record_skip)

        # Manifest
        ws_files = sum(1 for _ in (stage / "workspace").rglob("*") if _.is_file())
        pm_files = sum(1 for _ in (stage / "plan_memory").rglob("*") if _.is_file())
        sk_dir = stage / "skills"
        sk_count = sum(1 for _ in sk_dir.iterdir() if _.is_dir()) if sk_dir.is_dir() else 0
        ms_dir = stage / MEMORY_STORES_DIR_NAME
        ms_count = sum(1 for _ in ms_dir.iterdir() if _.is_dir()) if ms_dir.is_dir() else 0
        # Recorded so a reader can tell how the archive was built. "unpinned" means
        # the trees were walked by name, which an ancestor swap during staging could
        # have redirected. Someone deciding whether to trust this archive needs that
        # on the record rather than in the memory of whoever ran the command.
        staging_mode = "pinned" if pinned else "unpinned"
        manifest = {
            "version": MANIFEST_VERSION,
            "created_at": facade.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hostname": socket.gethostname(),
            "user": os.environ.get("USER", "unknown"),
            "kirocrew_dir": str(mc),
            "purpose": purpose.value,
            # Which components rode, and what each declared about credential material.
            # A reader of the bundle can answer "is this safe to hand to someone"
            # from the manifest instead of inferring it from the file list.
            "components": {c: COMPONENTS[c].policy.value for c in selected},
            "staging": staging_mode,
            "skipped": skipped,
            "contents": {
                "memory_db": _fsize(stage / "memory.db"),
                "memory_index_db": _fsize(stage / "memory_index.db"),
                "crons_json": _fsize(stage / "crons.json"),
                "config_json": _fsize(stage / "config.json"),
                "notifications_jsonl": _fsize(stage / "notifications.jsonl"),
                "workspace_files": ws_files,
                "plan_memory_files": pm_files,
                "skill_count": sk_count,
                "memory_store_count": ms_count,
            },
        }
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if skipped_out is not None:
            # Taken from the same list the manifest just recorded, at the same point, so
            # the bundle and the caller cannot disagree about what was omitted.
            skipped_out.extend(skipped)
        if staging_mode == "unpinned":
            print(
                "⚠️  Staged by path name (--allow-unpinned-staging): this platform "
                "cannot pin a directory by descriptor, so an ancestor swapped during "
                "staging could have redirected a copy. Recorded in MANIFEST.json."
            )

        # Held to the restore bound BEFORE the tarball exists. A bundle past it cannot be
        # restored by this tool, and checking only the finished archive both paid for a
        # full compression pass and left the unrestorable file at the very name `--keep`
        # and restore look for.
        _refuse_oversized_tree(stage, max_bytes=max_archive_bytes)

        # Tarball — write to temp file and rename atomically to avoid corrupt partials
        out.mkdir(parents=True, exist_ok=True)
        outfile = out / f"{name}.tar.gz"
        tmp_tar = outfile.with_suffix(".tar.gz.tmp")
        try:
            with tarfile.open(str(tmp_tar), "w:gz") as tar:
                tar.add(str(stage), arcname=arcname, filter=_data_filter)
            # Lock the archive down BEFORE it is published.
            #
            # This tarball can contain sel_hmac.key, and the window between the rename
            # and a lockdown applied afterwards is not Windows-only: tarfile does not
            # create its file 0600, so on POSIX the archive is readable at its final,
            # predictable path until the chmod lands too.
            #
            # restrict_to_owner (fail-loud), NOT chmod_safe: chmod_safe swallows OSError
            # and would let the snapshot land group/world-readable while still printing
            # success. Failing here leaves the temp for the handler below to remove and
            # publishes nothing, which is what makes the "abort rather than ship an
            # under-protected archive" promise true by construction. POSIX applies chmod
            # 0o600; Windows applies an owner-only DACL in-process, and a
            # same-directory rename carries the explicit ACE with the file.
            platform_compat.restrict_to_owner(str(tmp_tar))
            tmp_tar.rename(outfile)
        except BaseException:
            tmp_tar.unlink(missing_ok=True)
            raise
    return outfile
