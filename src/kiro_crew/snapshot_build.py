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

from kiro_crew import pinned_fs, platform_compat, sandbox_launch_mark
from kiro_crew.member_memory_backup import hold_stores_for_read
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME
from kiro_crew.sandbox_leaves import _CREW_HIDDEN_LEAVES
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
from kiro_crew.staging import staging_dir


def _fsize(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _estimate_selected_bytes(mc: Path, selected: list[str]) -> tuple[int, int]:
    """Bytes the *selected* components would stage, and how many entries refused.

    Scoped to the SELECTION, not the data home, so the number matches ``--components``
    and an unstat-able entry outside the selection cannot end the command.

    Overlapping trees are counted ONCE, by the same ancestor collapse staging makes.
    ``_staging_ignore`` is not applied: this only feeds a 'this may be slow' notice, and
    over-estimating is the safe direction.

    Symlinks are neither followed nor counted, as in staging. The second value counts
    entries refused for permission; any other error is raised, so a failing disk is not
    reported as a few protected paths.
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
        # Only permission errors are counted, as in the staging walks; an EIO or a lost
        # mount is raised, because a count cannot say 'the storage is failing'.
        if not isinstance(exc, PermissionError):
            raise exc
        unreadable += 1

    trees: list[str] = []
    for comp in selected:
        spec = COMPONENTS[comp]
        for f in spec.files:
            _count(mc / f)
        trees.extend(spec.trees)
    # A tree covered by an ANCESTOR in the selection is dropped, as in staging: `seen`
    # dedupes bytes but not refusals, and the shared subtree is walked only once.
    covered = set(trees)
    for tree in trees:
        if any(
            other != tree and PurePosixPath(tree).is_relative_to(PurePosixPath(other))
            for other in covered
        ):
            continue
        root = mc / tree
        # A tree ROOT that is a link is not walked; staging refuses it later
        # (`safe_tree_root`), and the estimate must not follow it first.
        if pinned_fs.is_reparse_point(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error):
            # Reparse points pruned in place: `os.walk` skips symlinked directories but
            # descends into a Windows junction, a tree the operator did not select.
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

    Either produces a finished archive or raises; the caller turns a
    :class:`kiro_crew.pinned_fs.PinnedPathRefusal` into an exit code.

    *selected* (default: every component) and *purpose* are resolved by the caller, so no
    policy is decided here. Staging is per-component, so ``--components memory`` is a
    complete memory backup.

    *skipped_out*, when given, is extended with the records written to ``MANIFEST.json``'s
    ``skipped``, so the caller knows whether anything was omitted.

    *name* names the TARBALL and *root_name* (default *name*) the directory inside it: a
    selective bundle marks only the inner directory, because ``--list``, pruning and
    ``--keep`` glob ``kirocrew-snapshot-*.tar.gz``.

    *max_archive_bytes* is the bound ``kirocrew restore`` applies (default
    ``_MAX_ARCHIVE_BYTES``). The staged tree is checked before the tarball exists, so an
    unrestorable bundle is never published (:class:`_ArchiveTooLarge`, nothing written).
    """
    facade = _facade()
    arcname = root_name or name
    if selected is None:
        selected = list(COMPONENTS)
    # Decided once, BEFORE staging: core files alone must also consult the opt-in, and
    # the manifest's "staging" value then describes the whole archive.
    pinned = facade._staging_is_pinned(allow_unpinned=allow_unpinned, what="data home")

    # Every skip is recorded in the manifest, not just printed, so a partial archive says
    # so. Paths are relative to the data home, keeping the machine's layout out of it.
    skipped: list[dict[str, str]] = []

    def _record_skip(reason: str, path: str) -> None:
        try:
            rel = Path(path).relative_to(mc).as_posix()
        except ValueError:
            rel = Path(path).name
        skipped.append({"reason": reason, "path": rel})
        facade._report_skip(reason, path)

    # Stage under the data home's ``staging`` leaf, not the system temp dir: the tree can
    # be GBs and a tmpfs /tmp would hold it in RAM. The leaf is owner-only and unmasked,
    # no more exposed than the visible ``snapshots`` archive it becomes (the AWS nightly
    # backup stages here too). A crash orphan is reclaimed by ``staging.sweep_staging``.
    with tempfile.TemporaryDirectory(dir=str(staging_dir())) as work:
        # Locked down before staging: Windows inherits the parent DACL and ignores the
        # 0700 mode mkdtemp sets.
        platform_compat.restrict_dir_to_owner(work)
        stage = Path(work) / arcname
        # Always created, so an empty selection still gets its manifest. Only the ROOT:
        # the staging primitive creates each tree and refuses one that already exists.
        stage.mkdir(parents=True, exist_ok=True)

        # Core files, through the pinned primitive, not shutil.copy2: copy2 turns a
        # hardlink into plain bytes the tar screen cannot reject. Where possible each file
        # is opened relative to one data-home descriptor, so a swapped ancestor cannot
        # redirect the read (O_NOFOLLOW covers only the final name).
        mc_fd = pinned_fs.open_dir_pinned(mc, what="data home") if pinned else None
        try:
            for comp in selected:
                for f in COMPONENTS[comp].files:
                    src = mc / f
                    if mc_fd is not None:
                        # Stat through mc_fd: a link or junction is not S_ISREG, and no
                        # name is left for a swap to redirect. An absent file is not an
                        # omission; only one that exists and is not regular is recorded.
                        live_st = pinned_fs.stat_at(mc_fd, f)
                        if live_st is None:
                            continue
                        if not _stat.S_ISREG(live_st.st_mode):
                            _record_skip(pinned_fs.SKIP_NOT_REGULAR, str(src))
                            continue
                    else:
                        if not src.is_file():
                            continue
                        # Fallback without a descriptor: `is_file()` and `os.open` follow
                        # links, so screen explicitly; `is_reparse_point` also catches a
                        # Windows junction, which `islink` misses.
                        if pinned_fs.is_reparse_point(src):
                            _record_skip(pinned_fs.SKIP_SYMLINK, str(src))
                            continue
                    if f.endswith(".db"):
                        # A component file may sit under a subdirectory
                        # (`workspace/knowledge/knowledge.db`), so the parent is created
                        # per file rather than assumed from the component's tree roots.
                        (stage / f).parent.mkdir(parents=True, exist_ok=True)
                        # The same hardened copy as the tree pass (verified chain, `mode=ro`,
                        # "not a database" vs "unreadable"). SQLite takes only a path, so the
                        # helper's chain check covers the ancestors. `require_database=True`
                        # makes any failure a raise: a success here must never let `--keep`
                        # prune the last good archive for an unrestorable one.
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

        # Trees. Selections overlap (`memory` names workspace/memory, `workspace` the whole
        # tree), so a tree covered by an ANCESTOR in the selection is dropped: the staging
        # primitive refuses a destination that already exists in a tree it created.
        wanted_trees: list[tuple[str, str]] = []
        for comp in selected:
            for tree in COMPONENTS[comp].trees:
                # An unsafe root FAILS the snapshot rather than being skipped: a manifest
                # declaring a component whose tree is missing is worse than no backup.
                # safe_tree_root returns None only for an unsafe or unresolvable root, not
                # for an absent one, so a fresh data home passes.
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
        # Kiro Crew's sandbox masks every crew-hidden leaf from the processes it confines (a
        # scheduled command cron, an agent's shell): Linux binds an empty stand-in over it
        # and macOS denies the read. Staging that view would ship an empty tree as if it
        # were the operator's, or record it as merely unreadable, so a masked tree is not
        # read at all and its own reason says why it is missing.
        masked = (
            [PurePosixPath(leaf) for leaf in _CREW_HIDDEN_LEAVES]
            if sandbox_launch_mark.launched_in_sandbox()
            else []
        )
        for comp, tree in staged_trees:
            src_dir = mc / tree
            dst_dir = stage / tree
            tree_path = PurePosixPath(tree)
            if any(tree_path == leaf or leaf in tree_path.parents for leaf in masked):
                _record_skip(pinned_fs.SKIP_MASKED_BY_SANDBOX, str(src_dir))
                continue
            # Explicit stat: `Path.is_dir()` answers False for an unreachable root too, which
            # would drop the whole tree with nothing in `skipped` and let `--keep` prune the
            # last complete archive. `safe_tree_root`'s `resolve(strict=False)` does not
            # raise on a refusal, so it does not catch this.
            try:
                root_st: os.stat_result | None = os.stat(src_dir)
            except PermissionError:
                _record_skip(pinned_fs.SKIP_UNREADABLE_ENTRY, str(src_dir))
                continue
            except FileNotFoundError:
                # ONLY absent is ordinary. EIO, ESTALE, ENOTCONN, ENOTDIR propagate: hiding
                # them would drop the tree silently, as above. The estimate raises the same.
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

        # Held to the restore bound BEFORE the tarball exists, so no compression pass is
        # spent and no unrestorable file lands where `--keep` and restore look.
        _refuse_oversized_tree(stage, max_bytes=max_archive_bytes)

        # Tarball — write to temp file and rename atomically to avoid corrupt partials
        out.mkdir(parents=True, exist_ok=True)
        outfile = out / f"{name}.tar.gz"
        tmp_tar = outfile.with_suffix(".tar.gz.tmp")
        try:
            with tarfile.open(str(tmp_tar), "w:gz") as tar:
                tar.add(str(stage), arcname=arcname, filter=_data_filter)
            # Lock the archive down BEFORE it is published: it can hold sel_hmac.key, and
            # tarfile does not create it 0600. restrict_to_owner fails loud (chmod_safe
            # would swallow OSError), so a failure publishes nothing. A same-directory
            # rename keeps the Windows owner-only ACE.
            platform_compat.restrict_to_owner(str(tmp_tar))
            tmp_tar.rename(outfile)
        except BaseException:
            tmp_tar.unlink(missing_ok=True)
            raise
    return outfile
