"""The ``--mode merge`` driver: import a bundle into a live data home, component by component.

:func:`_do_merge` runs once :func:`kiro_crew.snapshot.restore_main` has extracted and screened
the bundle. It refuses an unsafe destination root and a platform that cannot pin before
anything is written, then merges each selected component through the algorithms in
:mod:`kiro_crew.snapshot_merge`, and names each component it leaves to ``--mode replace``.
:func:`_copy_notifications` installs a bundle's notification records where the live home
has none; its docstring records why a platform without ``O_NOFOLLOW`` refuses rather than
falling back. The helpers a test replaces on :mod:`kiro_crew.snapshot` are read through it
when used (:func:`kiro_crew.snapshot_components._facade`), so a patch there reaches the
calls here.
"""

from __future__ import annotations

import filecmp
import os
from pathlib import Path

from kiro_crew import pinned_fs
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME
from kiro_crew.snapshot_archive import _safe_name
from kiro_crew.snapshot_components import (
    _CONFIG_SETTINGS_DOCUMENTS,
    _LOCKED_DOCUMENT_TREES,
    _REPLACE_ONLY_COMPONENTS,
    _WHOLE_TREE_COMPONENTS,
    COMPONENTS,
    CORE_FILES,
    ComponentRefused,
    UnsafeComponentRoot,
    _facade,
    _want,
    safe_tree_root,
)
from kiro_crew.snapshot_merge import (
    NotificationCopyUnsupported,
    _copy_locked,
    _copy_tree_no_overwrite,
    _core_file_not_installed,
    _install_core_file_if_absent,
    _install_notifications,
    _merge_crons,
    _merge_memory,
    _merge_notifications,
    _report_unmerged_databases,
    _serialise_with_notification_writes,
)
from kiro_crew.snapshot_restore import _install_locked_document, _lock_down_restored


def _copy_notifications(src_path: Path, dst_path: Path) -> None:
    """Install the snapshot's notification records, ordered against the live writer.

    Refuses outright where ``O_NOFOLLOW`` does not exist -- see below. The refusal is
    made HERE, before the executor is acquired and before anything is opened, because
    it is a question about the platform rather than about this source.

    The whole body runs on the dashboard's notification worker when one exists, so a
    row the gateway delivers during the restore cannot be ordered ahead of the
    archive's and dropped by the reader's positional cap. See
    :func:`_serialise_with_notification_writes` for why ``O_APPEND`` alone was not
    enough, and note the cost: a restore briefly blocks notification writes. A
    restore is rare and user-initiated; a lost notification is silent and permanent.

    WHY A MISSING ``O_NOFOLLOW`` REFUSES INSTEAD OF FALLING BACK. The predecessor
    checked ``pinned_fs.is_reparse_point(src_path)`` by NAME and then opened the same
    NAME. Against an adversary that is not a floor, it is a check-to-open window: the
    threat model this function is written for -- stated in ``_install_notifications``
    and demonstrated by the revision review blocked -- is a concurrent agent replacing
    the extracted file, and such an agent chooses the timing. The descriptor check does
    not save it either: ``fstat`` asserts ``S_ISREG``, and a reparse point resolving to
    a regular ``.env`` passes ``S_ISREG``. So on a platform with no ``O_NOFOLLOW`` there
    is no sequence of by-name checks that makes this safe, and the honest move is to not
    do it.

    The alternative -- a ctypes ``CreateFileW`` with ``FILE_FLAG_OPEN_REPARSE_POINT`` --
    is declined elsewhere in this repo: ``kiro_crew/eval/bench/safepath.py`` records that
    it "is not worth considering here: it would buy the same property exclusive creation
    already has, at the price of security code that cannot be exercised on the machine
    this harness is developed on", and ``skill_trust.py`` records that Python "does not expose
    an equivalent handle-relative, no-reparse walk on Windows". Both decline the capable
    route, which means this repo has already chosen less capability on that platform over
    a hand-rolled walk. Refusing extends those two decisions; falling back contradicts
    them.

    The cost is not symmetric, which is what makes the trade easy. Refusing loses
    notification HISTORY on one platform for one operation -- the records remain in the
    snapshot and nothing is destroyed. Falling back can put attacker-chosen bytes, a
    credentials file among them, into a location the agent then reads. Snapshot restore
    validates what it installs so that it never installs unvalidated bytes: a remaining
    path that installs attacker-chosen bytes is that same defect, closed everywhere
    except where it is hardest.

    The refusal is LOUD and the callers report the skip. A silent skip would be the same
    class of bug as installing unvalidated bytes, so the message names the platform, the missing
    primitive, and what was not imported.
    """
    if not getattr(os, "O_NOFOLLOW", 0):
        raise NotificationCopyUnsupported(
            f"this platform ({os.name}) has no O_NOFOLLOW, so the notification source "
            "cannot be opened with any guarantee that the name checked is the file "
            "read -- a by-name reparse-point check followed by a by-name open is a "
            "window a concurrent writer chooses the timing of, and fstat's S_ISREG "
            "does not close it because a reparse point to a regular file passes it. "
            "Refusing rather than importing bytes that may not be the archive's: "
            f"{_safe_name(src_path)} was NOT imported and remains in the snapshot"
        )
    _serialise_with_notification_writes(lambda: _install_notifications(src_path, dst_path))


def _do_merge(
    snap: Path, mc: Path, components: list[str] | None, *, allow_unpinned: bool = False
) -> None:
    facade = _facade()
    # BOTH restore modes refuse an unsafe destination tree root up front, and merge needs
    # it for the same reason replace does: a merge that silently omits a tree is still a
    # merge that claims to have imported it. Skipping the tree and returning 0 is the worst
    # available outcome -- the operator is told the import succeeded while the notes they
    # were importing are not there.
    # REPLACE ONLY, from the shape of the data: an artifact is a DIRECTORY whose files
    # describe each other, and a slug comes from the artifact's NAME, so a per-file
    # no-overwrite merge either tops one artifact up out of another's generation or needs a
    # rule for when two artifacts are the same artifact. Replace needs neither.
    #
    # Refused when that is all the operator asked for, skipped with a notice otherwise: a
    # run that imports nothing and reports success is the failure this component removes.
    replace_only = [c for c in _REPLACE_ONLY_COMPONENTS if _want(components, c)]
    if replace_only and components is not None and not set(components) - set(replace_only):
        raise ComponentRefused(
            f"component(s) {', '.join(replace_only)} are restored with --mode replace "
            "only; re-run with that mode."
        )
    # Scoped to the components this MODE writes. Refusing over a replace-only root aborts
    # an import that never reaches it -- a home keeping `uploads/` on another disk behind a
    # link could not merge its memory at all. Never narrows to nothing: a selection of only
    # those was refused above.
    merged_components = [
        c for c in COMPONENTS if _want(components, c) and c not in _REPLACE_ONLY_COMPONENTS
    ]
    facade._refuse_unsafe_destination_roots(mc, merged_components)
    # Asked once, at entry, BEFORE any mutation. The core-file copies below run before
    # any tree call, so gating inside the tree helpers meant a merge on a platform that
    # cannot pin wrote memory.db, crons.json and the security files first and only then
    # met the refusal -- either redirecting those writes through a planted link, or
    # aborting with the restore already half applied. Review caught it; it is the same
    # gate-placement defect as the snapshot side, one path over.
    facade._staging_is_pinned(allow_unpinned=allow_unpinned, what="merge restore")
    print("🔀 Merge mode — importing...")

    if _want(components, "memory") and (snap / "memory.db").is_file():
        memory_ok = True
        if not (mc / "memory.db").is_file():
            # Pinned, not copy2: see `_install_core_file_if_absent`. The index is
            # only installed beside a memory.db this call installed.
            if _install_core_file_if_absent(snap / "memory.db", mc / "memory.db"):
                if (snap / "memory_index.db").is_file() and not _install_core_file_if_absent(
                    snap / "memory_index.db", mc / "memory_index.db"
                ):
                    # The index is never overwritten, so a leftover one does not
                    # match the memory.db just installed: search misses its rows
                    # until the index is rebuilt.
                    print(f"  ↩️  {_core_file_not_installed(mc / 'memory_index.db', index=True)}")
                print("  Memory: copied (no existing memory.db)")
            else:
                print(f"  ↩️  {_core_file_not_installed(mc / 'memory.db')}")
                memory_ok = False
        else:
            _merge_memory(snap / "memory.db", mc / "memory.db")
        if memory_ok:
            print("  ✅ memory")

    # The markdown half of memory (preferences, projects, history, knowledge). Named
    # by the memory component so restoring memory does not require the whole
    # workspace; no-overwrite so a merge never clobbers newer local files.
    if _want(components, "memory"):
        for tree in COMPONENTS["memory"].trees:
            sd = snap / tree
            if sd.is_dir():
                dd = mc / tree
                # RE-CHECKED here, not only in the pre-flight at the top of this function.
                # The pre-flight clears the same set moments earlier, so a root that fails
                # now failed AFTER it -- something moved under us mid-run. Replace already
                # refuses that; merge did not, and the gap was reachable: with `workspace`
                # swapped for an external link straight after the pre-flight, this loop's
                # `mkdir(parents=True)` created the tree THROUGH the link and the copy wrote
                # the operator's memory files into an attacker-chosen directory outside the
                # data home -- four of them, with the run printing "Merge complete."
                #
                # The per-file screens cannot see it: each final component is a fresh regular
                # file, and a by-name open does not check its ancestors. Refusing on the root
                # is what closes it. Merge is additive, so a component already merged stays
                # merged -- nothing is destroyed by stopping here, unlike carrying on.
                if safe_tree_root(dd, what="destination root", home=mc) is None:
                    raise UnsafeComponentRoot(
                        f"memory:{tree} stopped resolving inside the data home after the "
                        "pre-flight check passed. A path that was safe moments ago and is "
                        "not now was replaced mid-run (usually a symlink); merging past it "
                        "would write this component outside the data home."
                    )
                if tree == MEMORY_STORES_DIR_NAME:
                    kept = facade._merge_named_stores(sd, dd, allow_unpinned=allow_unpinned)
                    for store in kept:
                        print(
                            f"  ↩️  {tree}/{_safe_name(store)}: kept the existing store; "
                            "the bundle's copy was NOT merged into it. "
                            "To take the bundle's copy instead, use --mode replace."
                        )
                    continue
                dd.mkdir(parents=True, exist_ok=True)
                _report_unmerged_databases(sd, dd, tree)
                _copy_tree_no_overwrite(sd, dd, allow_unpinned=allow_unpinned)

    if _want(components, "crons"):
        sc, dc = snap / "crons.json", mc / "crons.json"
        crons_ok = True
        if sc.is_file():
            if dc.is_file():
                crons_ok = _merge_crons(sc, dc)
            elif _install_core_file_if_absent(sc, dc):
                print("  Crons: copied (no existing crons)")
            else:
                print(f"  ↩️  {_core_file_not_installed(dc)}")
                crons_ok = False
        if crons_ok:
            print("  ✅ crons")
        else:
            print("  ⚠️  crons: merge skipped (see warning above) — no jobs imported")

    if _want(components, "config"):
        # Merge never overwrites, and for this component that usually means NOTHING is
        # restored: every running install already has a config.json. So each bundle file
        # kept that way is named, the way a kept named store is, and the tick is printed
        # only when no bundle file was left behind -- a bare "✅ config" over a restore
        # that took none of the bundle's settings read as "my settings came back".
        kept_config: list[str] = []
        for f in CORE_FILES["config"]:
            s, d = snap / f, mc / f
            if not s.is_file():
                continue
            if os.path.lexists(d) and (d.is_symlink() or not d.is_file()):
                # A link, directory or other non-file at the name: not a settings file
                # to keep, and not a name to write through or read from. Refused BEFORE
                # the comparison below, which would otherwise read a linked target --
                # possibly a credential file outside the data home. Left as it is.
                kept_config.append(f)
                print(f"  ↩️  {f}: not restored; the existing entry is not a regular file.")
                continue
            if d.is_file() and filecmp.cmp(s, d, shallow=False):
                continue  # the same bytes: nothing of the bundle's was left behind
            if f == "config.local.json" and not d.is_file():
                # Never installed by a merge: the overlay outranks config.json at load, so
                # a bundle's copy dropped in raw would set every key it names -- sandbox,
                # approval, channel tokens -- past the receiving install's own config.json
                # with none of the dashboard Merge's filtering. Replace takes it.
                kept_config.append(f)
                print(
                    f"  ↩️  {f}: not applied; a merge never installs the bundle's config "
                    "overlay, which would outrank this install's config.json. To take it, "
                    "re-run with --mode replace --components config."
                )
                continue
            if not os.path.lexists(d):
                # Missing means NOTHING at the name -- `is_file()` is also false for a
                # dangling link, which `shutil.copy2` would follow and so write the
                # bundle's file wherever it points, outside the data home. The pinned
                # copy creates the destination O_CREAT|O_EXCL|O_NOFOLLOW, so a link
                # planted after this check is refused too, never followed.
                if pinned_fs.copy_file_pinned(
                    str(s), str(d), skip_existing=True, on_skip=facade._report_skip
                ):
                    _lock_down_restored(d, "config")
                    print(f"  {f}: restored (was missing)")
                continue
            if f not in _CONFIG_SETTINGS_DOCUMENTS:
                # Host runtime state (`session_map.json`, the project and workspace
                # pointers), not a setting: this host's copy is the right one, and a
                # replace would install the source host's paths, which need not exist.
                continue
            kept_config.append(f)
            print(
                f"  ↩️  {f}: kept the existing file; the bundle's copy was NOT merged "
                "into it. To take the bundle's settings instead, re-run with "
                "--mode replace --components config."
            )
        if not kept_config:
            print("  ✅ config")

    if _want(components, "notifications"):
        sn, dn = snap / "notifications.jsonl", mc / "notifications.jsonl"
        notifications_ok = True
        if sn.is_file():
            if dn.is_file():
                # A platform that cannot pin raises NotificationCopyUnsupported
                # from inside the merge, exactly as the copy branch does below --
                # skip that one component loudly and let the rest proceed. A
                # link/FIFO/hardlink refusal on a capable platform is a different
                # class: it raises OSError and aborts, because that is a bad or
                # hostile source, not a platform that cannot do the work.
                try:
                    _merge_notifications(sn, dn)
                except NotificationCopyUnsupported as exc:
                    print(f"  ⚠️  Notifications: SKIPPED -- {exc}")
                    notifications_ok = False
            else:
                # Not `copy2`: a byte-exact copy installs records the live file's
                # own reader refuses, and that reader loses the whole file to one
                # of them. Same abort posture as the merge branch above.
                #
                # The platform refusal is NOT that abort: it says this platform can
                # never do this safely, not that this archive is bad, so it skips one
                # component loudly and lets the rest of the restore proceed. Reported
                # rather than swallowed -- a silent skip is the bug class being fixed.
                try:
                    _copy_notifications(sn, dn)
                    print("  Notifications: copied")
                except NotificationCopyUnsupported as exc:
                    print(f"  ⚠️  Notifications: SKIPPED -- {exc}")
                    notifications_ok = False
        # A skipped component must never report a success tick, exactly as the
        # crons branch gates its own tick on `crons_ok` above.
        if notifications_ok:
            print("  ✅ notifications")

    if _want(components, "security"):
        for f in CORE_FILES["security"]:
            s, d = snap / f, mc / f
            if s.is_file() and not d.is_file():
                if _copy_locked(s, d):
                    print(f"  {f}: restored (was missing)")
        print("  ✅ security")

    for comp in _WHOLE_TREE_COMPONENTS:
        if not _want(components, comp):
            continue
        if comp in _REPLACE_ONLY_COMPONENTS:
            # Named, not passed over in silence, and BEFORE the tick below so a component
            # this run did not import never reports one.
            print(
                f"  ⚠️  {comp}: SKIPPED -- restored with --mode replace only, which swaps "
                "the tree whole and keeps the previous one in the pre-restore backup."
            )
            continue
        for dirname in COMPONENTS[comp].trees:
            sd = snap / dirname
            if dirname in _LOCKED_DOCUMENT_TREES:
                if (snap / _LOCKED_DOCUMENT_TREES[dirname]).is_file():
                    _install_locked_document(dirname, snap, mc, only_if_absent=True)
                continue
            if sd.is_dir():
                dd = mc / dirname
                dd.mkdir(parents=True, exist_ok=True)
                _copy_tree_no_overwrite(sd, dd, allow_unpinned=allow_unpinned)
        print(f"  ✅ {comp}")

    print("✅ Merge complete.")
