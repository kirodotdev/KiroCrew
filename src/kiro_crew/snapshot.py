"""KiroCrew snapshot and restore — portable state management."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat as _stat
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from kiro_crew import pinned_fs, platform_compat

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

try:
    from kiro_crew.config.loader import DASHBOARD_PORT as _DASHBOARD_PORT
except Exception:  # pragma: no cover - optional during early/standalone import
    _DASHBOARD_PORT = int(os.environ.get("KIROCREW_PORT", 5476))

VALID_COMPONENTS = ("memory", "crons", "config", "skills", "workspace", "notifications", "security")

# Files that must always have 0o600 permissions in snapshots and on restore.
SECURITY_SENSITIVE_FILES: frozenset = frozenset({"sel_hmac.key", "telemetry_salt"})

# Must match ``handlers_system._get_telemetry_salt`` (``secrets.token_bytes(32)``).
# ``_copy_locked`` loads telemetry_salt into memory, so a planted giant in an
# untrusted snapshot would OOM restore after earlier merge steps.
_TELEMETRY_SALT_BYTES = 32

# Files that must NEVER ride a snapshot: sel_hmac.key is regenerated on restore
# so audit-log HMACs stay bound to the host that wrote them.
#
# This set is matched by BASENAME inside `_data_filter`, which runs over the
# ENTIRE tar — including the staged workspace/, plan_memory/ and skills/ trees.
# So any name added here also silently drops a USER file that happens to share
# it. Keep the set minimal for that reason.
#
# The beacon's per-install identity (beacon_install_id / beacon_last_sent) is
# deliberately NOT here: snapshot staging copies an explicit per-component file
# list (CORE_FILES) plus those three directories, and no component lists a beacon
# file, so a root beacon file is never staged in the first place. The
# id-cloning hazard is closed by that non-selection, not by a basename filter.
NEVER_SNAPSHOT_FILES: frozenset = frozenset({"sel_hmac.key"})


def _data_filter(info: tarfile.TarInfo, _dest: str = "") -> tarfile.TarInfo | None:
    """Equivalent to tarfile ``"data"`` filter (Python 3.12+), with 3.10 fallback.

    Also rejects path traversal, symlinks, and hardlinks to eliminate TOCTOU
    race between pre-scan and extraction.
    Excludes sel_hmac.key (must be regenerated on restore, not shipped).
    Security-sensitive files get 0o600 permissions.
    """
    # Reject path traversal. POSIX checks apply everywhere; the Windows-syntax
    # checks (backslash separators, drive letters — incl. the drive-RELATIVE
    # `C:foo` form is_absolute() misses, which resolves against the drive CWD
    # at extraction) apply ONLY when extracting on Windows, where tarfile
    # honors '\' as a native separator. They must NOT run on POSIX: ':' and
    # '\' are legal characters in Linux/macOS filenames, so a workspace file
    # named `a:1` or `notes..\old` would be silently dropped from a
    # Linux-to-Linux restore.
    name = info.name
    traversal = (
        name.startswith("/")
        or ".." in PurePosixPath(name).parts
        or PurePosixPath(name).is_absolute()
    )
    if not traversal and platform_compat.IS_WINDOWS:
        traversal = (
            name.startswith("\\")
            or ".." in PureWindowsPath(name).parts
            or PureWindowsPath(name).is_absolute()
            or bool(PureWindowsPath(name).drive)
        )
    if traversal:
        print(f"⚠️  Rejecting path traversal entry: {info.name}")
        return None
    # Reject symlinks and hardlinks
    if info.issym() or info.islnk():
        print(f"⚠️  Rejecting symlink/hardlink entry: {info.name}")
        return None
    # Never ship these — each must be regenerated on the restoring host.
    basename = PurePosixPath(info.name).name
    if basename in NEVER_SNAPSHOT_FILES:
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    # Security-sensitive files get restricted permissions
    if not info.isdir() and basename in SECURITY_SENSITIVE_FILES:
        info.mode = 0o600
    else:
        info.mode = 0o755 if info.isdir() else 0o644
    return info


def _default_snapshot_dir() -> str:
    """Return snapshot directory from config, falling back to <config_dir>/snapshots."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        d = KiroCrewConfig.load().snapshot_dir
        if d:
            return str(Path(d).expanduser())
    except Exception:
        pass
    try:
        from kiro_crew.config.paths import config_dir

        return str(config_dir() / "snapshots")
    except Exception:
        return str(Path.home() / ".kiro" / "crew" / "snapshots")


def _audit(event_type: str, resources: str) -> None:
    """Emit a SEL audit event for snapshot/restore operations."""
    try:
        from kiro_crew.sel import SecurityEvent, sel

        sel().log(
            SecurityEvent(
                event_id=os.urandom(8).hex(),
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type=event_type,
                caller_identity=os.environ.get("USER", "unknown"),
                agent="kirocrew",
                source="cli",
                operation=event_type,
                outcome="completed",
                resources=resources,
            )
        )
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("SEL audit event '%s' failed: %s", event_type, e)


CORE_FILES: dict[str, tuple[str, ...]] = {
    "memory": ("memory.db", "memory_index.db"),
    "crons": ("crons.json",),
    "config": ("config.json", "session_map.json", "hooks.json", "project_dir", "workspace_dir"),
    "notifications": ("notifications.jsonl",),
    "security": ("telemetry_salt",),  # sel_hmac.key excluded — regenerated on restore
}

COMPONENT_HELP = {
    "memory": "memory.db, memory_index.db (semantic, episodic, knowledge graph)",
    "crons": "crons.json (scheduled jobs)",
    "config": "config.json, session_map.json, hooks.json, project_dir, workspace_dir",
    "skills": "skills/ directory",
    "workspace": "workspace/, plan_memory/ directories",
    "notifications": "notifications.jsonl (notification history)",
    "security": "telemetry_salt (sel_hmac.key excluded — regenerated on restore)",
}


def _mc_dir() -> Path:
    # Use the shared resolver so snapshot/restore honor the documented
    # KIROCREW_HOME override (and the same ~/.kiro/crew default) as every other
    # module — not an undocumented KIROCREW_DIR, which would make snapshots
    # silently target the real home even when state was relocated.
    from kiro_crew.config.loader import config_dir

    return config_dir()


def _fsize(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _want(components: list[str] | None, name: str) -> bool:
    return components is None or name in components


def _list_components() -> None:
    print("Available components:")
    for k, v in COMPONENT_HELP.items():
        print(f"  {k:16s} {v}")
    print("\nCombine with commas: --components memory,crons,skills")


def _terminal_safe(value: object) -> str:
    """Render *value* so an untrusted string cannot drive the terminal.

    A restore accepts an arbitrary ``.tar.gz``, so every string that comes back out of one
    -- a manifest field, a member name -- is attacker-controlled input being written to a
    terminal. ANSI and OSC sequences in it are executed by the terminal, not displayed, so
    a crafted archive can rewrite what the operator appears to be reading, or worse.
    Raised in review against the omission list this change added.

    Control characters are escaped rather than stripped, so the value stays diagnosable
    (an operator can see the file really is named with an escape) instead of silently
    reading as a different, innocuous name. ``str.isprintable()`` is False for exactly the
    C0/C1 range plus the separators, and True for ordinary text in any language, so a
    non-ASCII path is unharmed.
    """
    return "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in str(value))


def _report_skip(reason: str, path: str) -> None:
    """Word a primitive's skip classification in this module's existing voice.

    The primitive classifies and never prints, so these strings stay byte-identical
    to what snapshot/restore printed before the migration.

    The path is rendered through :func:`_terminal_safe` because on the RESTORE side these
    names come out of the archive: the walk is over an extracted tree whose member names
    the archive chose, and `_data_filter` screens traversal, not escape bytes.
    """
    safe = _terminal_safe(path)
    if reason == pinned_fs.SKIP_SYMLINK:
        print(f"⚠️  Skipping symlink in source tree: {safe}")
    elif reason == pinned_fs.SKIP_VANISHED:
        print(f"⚠️  Skipping vanished entry during snapshot copy: {safe}")
    else:
        print(f"⚠️  Skipping hardlinked or non-regular file during snapshot copy: {safe}")


def _staging_is_pinned(*, allow_unpinned: bool, what: str) -> bool:
    """Whether staging may proceed, and whether it will be descriptor-pinned.

    Returns True for a pinned traversal, False for the by-name traversal the caller
    explicitly asked for. Raises rather than returning False when the platform cannot
    pin and no one said that is acceptable.

    This is the whole "refuse rather than fall back" rule, in one place. The reason it
    is a refusal and not a warning: a by-name walk is not a slightly weaker version of
    a pinned walk, it is the mechanism whose failure closed two pull requests. An
    operator who needs a snapshot on a platform without ``dir_fd`` can still have one,
    but they say so on the command line and the archive records that they did, so the
    weaker mode is never something the tool chose on their behalf.
    """
    if pinned_fs.supports_pinned_tree_walk():
        return True
    if allow_unpinned:
        return False
    raise pinned_fs.PinnedPathRefusal(
        f"refusing to stage the {what}: this platform cannot open a directory "
        "relative to a descriptor, so every component would be re-opened by name and "
        "an ancestor swapped mid-walk could redirect the copy into a credential "
        "store. Re-run with --allow-unpinned-staging to accept a by-name traversal; "
        "the archive will record that it was staged unpinned."
    )


def _copytree_safe(
    src: Path,
    dst: Path,
    *,
    allow_unpinned: bool = False,
    on_skip: pinned_fs.SkipReporter | None = None,
    must_create: bool = False,
    **kwargs,
) -> None:
    """Copy a tree for staging, with the source traversal pinned where possible.

    Was: ``shutil.copytree`` with an ignore callback that tested ``os.path.islink`` on
    a NAME. That screened the final component of each entry and nothing else, so an
    ancestor directory swapped for a link between the listing and the copy redirected
    every deeper open, and the screen had nothing to report -- what it found inside
    the replaced tree was an ordinary file. Now the traversal is descriptor-pinned by
    :func:`kiro_crew.pinned_fs.stage_tree_pinned`, including the chain above the root.

    ``dirs_exist_ok`` is still accepted and dropped -- it was a ``shutil.copytree``
    keyword and callers may pass it out of habit -- but it is no longer meaningless
    generally: whether an existing destination is tolerated is now decided by
    *must_create*, and it is decided identically on both branches. Every other keyword is
    rejected rather than silently dropped.

    *must_create* says the caller REPLACES its destination rather than merging into it, so
    a root that exists is refused. Only a caller that removed the tree it is about to write
    knows this, so it is passed, never derived -- deriving it from ``skip_existing`` refused
    every snapshot, because the snapshot's own staging root already exists.

    *on_skip* lets a caller both print and RECORD what was skipped. It defaults to
    printing only, which is right for restore; the snapshot path passes a recorder so
    an incomplete archive says so in its own manifest instead of only in the console
    output of whoever ran it.
    """
    report = on_skip or _report_skip
    outer_ignore = kwargs.pop("ignore", None)
    kwargs.pop("dirs_exist_ok", None)
    if kwargs:
        raise TypeError(f"_copytree_safe got unexpected keyword arguments: {sorted(kwargs)}")

    if _staging_is_pinned(allow_unpinned=allow_unpinned, what=f"tree {src.name!r}"):
        pinned_fs.stage_tree_pinned(
            src,
            dst,
            what=f"tree {src.name!r}",
            ignore=outer_ignore,
            on_skip=report,
            must_create=must_create,
        )
        return

    # Declared by-name traversal. The TRAVERSAL is the weakness the operator opted into --
    # an ancestor swapped mid-walk can still redirect it, and nothing here can prevent
    # that without the descriptor support the platform lacks. The PER-FILE screens are a
    # different matter and review was right that they had been left behind: plain
    # `copytree` dereferences a hardlink into ordinary bytes and follows a Windows
    # junction, so a credential aliased into a staged tree would have ridden along even
    # though the pinned path refuses exactly that. Each file now goes through
    # copy_file_pinned (same fstat screens, minus the pinned ancestors) and the screen
    # rejects reparse points, which `islink` alone does not report on Windows.
    def _ignore_unsafe(directory, contents):
        skipped = set()
        for entry in contents:
            full = os.path.join(directory, entry)
            if os.path.islink(full) or pinned_fs.is_reparse_point(full):
                skipped.add(entry)
                report(pinned_fs.SKIP_SYMLINK, full)
        if outer_ignore:
            skipped |= set(outer_ignore(directory, contents))
        return skipped

    def _copy_screened(source: str, target: str, **_kw) -> None:
        pinned_fs.copy_file_pinned(source, target, on_skip=report)

    # `dirs_exist_ok` has to follow `must_create`, not be hardcoded. Review found the gap:
    # `must_create` reached the pinned walk and stopped there, so on a platform that cannot
    # pin -- Windows, the dashboard's replace -- a root recreated after the rmtree was still
    # merged into and stale files survived a successful replace. That is the same mistake as
    # the earlier Windows import refusal in this PR: a guard added to the pinned path and not
    # carried to the by-name one. Every mutating path gets the gate or the gate is decorative.
    try:
        shutil.copytree(
            str(src),
            str(dst),
            ignore=_ignore_unsafe,
            copy_function=_copy_screened,
            dirs_exist_ok=not must_create,
        )
    except FileExistsError as exc:
        # Same refusal type and same sentence as the pinned branch, so a caller has one
        # thing to contain and the operator reads the same explanation on either platform.
        raise pinned_fs.PinnedPathRefusal(
            f"refusing to use the tree {src.name!r} destination: {dst.name!r} already "
            "exists, and this operation replaces its destination rather than merging "
            "into it. Something recreated that directory after it was removed, so "
            "staging into it would leave files the archive does not contain while "
            "reporting a replacement. Remove it and re-run with the gateway stopped."
        ) from exc


def _copy_tree_no_overwrite(src: Path, dst: Path, *, allow_unpinned: bool = False) -> None:
    """Merge *src* into *dst* without overwriting, with both ends pinned.

    The destination side is where #3797's third finding lives. The previous version
    walked the source with ``rglob`` and wrote each file with ``shutil.copy2`` to a
    path composed by name, so the destination's ancestor chain was never pinned: a
    component of *dst* swapped for a link after ``mkdir`` redirected the write, and
    ``not target.exists()`` answered for whatever the link pointed at rather than for
    the directory the caller validated.

    This is now one call into the shared primitive with ``skip_existing=True``, which
    is what makes the no-overwrite promise real: exclusive creation is atomic, so "it
    did not exist a moment ago" and "this call created it" are the same statement
    rather than two with a window between them.

    An earlier revision open-coded a second pinned walk here, with its own copy body.
    Review pointed out the two had already diverged -- this one's child-directory open
    lacked the ``ELOOP``/``ENOTDIR`` handling, so the very swap the staging walk skips
    would have escaped restore as a raw ``OSError`` -- which is the argument for a
    parameter on one primitive rather than a parallel implementation the shared
    module's own docstring says should not exist.
    """
    if not _staging_is_pinned(allow_unpinned=allow_unpinned, what=f"restore of {dst.name!r}"):
        for item in src.rglob("*"):
            if item.is_symlink():
                continue
            target = dst / item.relative_to(src)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                # `copy2` opened the destination BY NAME for writing, so a symlink planted
                # at that name after the `not target.exists()` check was followed and an
                # arbitrary external file was overwritten. Review's finding, and the
                # `exists()` guard was itself the name-based check that created the window.
                #
                # copy_file_pinned opens the destination O_CREAT|O_EXCL|O_NOFOLLOW even
                # with no directory descriptor, so the link is refused rather than
                # followed, and O_EXCL subsumes the skip-if-present behaviour the old
                # `not target.exists()` was there to provide -- without the race.
                pinned_fs.copy_file_pinned(
                    str(item), str(target), skip_existing=True, on_skip=_report_skip
                )
        return

    pinned_fs.stage_tree_pinned(
        src,
        dst,
        what=f"restore of {dst.name!r}",
        on_skip=_report_skip,
        skip_existing=True,
    )


# ── Snapshot ──────────────────────────────────────────────────────────────────


def _build_snapshot(mc: Path, out: Path, name: str, *, allow_unpinned: bool = False) -> Path:
    """Stage the data home into a temporary tree and publish it as one tarball.

    Extracted from ``snapshot_main`` so the staging pass has a boundary a refusal can
    be contained at: everything in here either produces a finished archive or raises,
    and the caller turns a :class:`kiro_crew.pinned_fs.PinnedPathRefusal` into an exit
    code rather than a traceback.
    """
    # Decided BEFORE anything is staged, not per-tree. An earlier revision gated
    # inside _copytree_safe only, so a data home with core files and no trees staged
    # them on a platform that cannot pin without ever consulting the opt-in -- the
    # gate was reachable only through a path that happened to exist. Asking once, up
    # front, is also what makes the manifest's "staging" value true of the whole
    # archive rather than of whichever component ran last.
    pinned = _staging_is_pinned(allow_unpinned=allow_unpinned, what="data home")

    # Every skip is recorded, not just printed. A snapshot that omitted a hardlinked
    # or symlinked file used to report success with a console warning and nothing in
    # the archive -- the same "silent partial" shape this change fixes on the restore
    # side, raised in review. Paths are stored relative to the data home so the record
    # names the file without carrying the absolute layout of the machine into an
    # archive that may be moved somewhere else.
    skipped: list[dict[str, str]] = []

    def _record_skip(reason: str, path: str) -> None:
        try:
            rel = str(Path(path).relative_to(mc))
        except ValueError:
            rel = Path(path).name
        skipped.append({"reason": reason, "path": rel})
        _report_skip(reason, path)

    with tempfile.TemporaryDirectory() as work:
        stage = Path(work) / name
        for d in ("workspace", "skills", "plan_memory"):
            (stage / d).mkdir(parents=True, exist_ok=True)

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
            for files in CORE_FILES.values():
                for f in files:
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
                        # DATABASES ARE OUT OF SCOPE for this change, deliberately, and
                        # this is main's behaviour unchanged.
                        #
                        # Capturing a live SQLite database without reopening its name is a
                        # genuine conflict of requirements -- SQLite accepts only a path,
                        # and it cannot be pointed at a held descriptor (probed: it refuses
                        # /proc/self/fd/N and /dev/fd/N alike). Five review rounds were
                        # spent on it here and each fix set up the next, so the database
                        # question is tracked separately rather than solved in the middle
                        # of a PR about the pinned tree walk.
                        #
                        # The `backup()` call reopens the live name and so inherits main's
                        # existing exposure. That is not a regression introduced here: it is
                        # what shipped before this change and what still ships after it.
                        with (
                            closing(sqlite3.connect(str(src))) as src_conn,
                            closing(sqlite3.connect(str(stage / f))) as dst_conn,
                        ):
                            src_conn.backup(dst_conn)
                    elif mc_fd is not None:
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

        # Workspace (exclude hygiene_data, insert_facts*.py)
        if (mc / "workspace").is_dir():
            _copytree_safe(
                mc / "workspace",
                stage / "workspace",
                allow_unpinned=allow_unpinned,
                on_skip=_record_skip,
                ignore=shutil.ignore_patterns("hygiene_data", "insert_facts*.py"),
            )

        # Plan memory
        if (mc / "plan_memory").is_dir():
            _copytree_safe(
                mc / "plan_memory",
                stage / "plan_memory",
                allow_unpinned=allow_unpinned,
                on_skip=_record_skip,
            )

        # Skills
        if (mc / "skills").is_dir():
            _copytree_safe(
                mc / "skills",
                stage / "skills",
                allow_unpinned=allow_unpinned,
                on_skip=_record_skip,
            )

        # Manifest
        ws_files = sum(1 for _ in (stage / "workspace").rglob("*") if _.is_file())
        pm_files = sum(1 for _ in (stage / "plan_memory").rglob("*") if _.is_file())
        sk_count = sum(1 for _ in (stage / "skills").iterdir() if _.is_dir())
        # Recorded so a reader can tell how the archive was built. "unpinned" means
        # the trees were walked by name, which an ancestor swap during staging could
        # have redirected. Someone deciding whether to trust this archive needs that
        # on the record rather than in the memory of whoever ran the command.
        staging_mode = "pinned" if pinned else "unpinned"
        manifest = {
            "version": 2,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hostname": socket.gethostname(),
            "user": os.environ.get("USER", "unknown"),
            "kirocrew_dir": str(mc),
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
            },
        }
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
        if staging_mode == "unpinned":
            print(
                "⚠️  Staged by path name (--allow-unpinned-staging): this platform "
                "cannot pin a directory by descriptor, so an ancestor swapped during "
                "staging could have redirected a copy. Recorded in MANIFEST.json."
            )

        # Tarball — write to temp file and rename atomically to avoid corrupt partials
        out.mkdir(parents=True, exist_ok=True)
        outfile = out / f"{name}.tar.gz"
        tmp_tar = outfile.with_suffix(".tar.gz.tmp")
        try:
            with tarfile.open(str(tmp_tar), "w:gz") as tar:
                tar.add(str(stage), arcname=name, filter=_data_filter)
            # Lock the archive down BEFORE it is published. Carried over from main's
            # #5317 during the rebase: this block was extracted out of snapshot_main
            # into this function, and main had meanwhile moved the lockdown to before
            # the rename, so taking this side of the conflict wholesale would have
            # silently dropped that fix.
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
            # 0o600; Windows applies an owner-only DACL via icacls, and a
            # same-directory rename carries the explicit ACE with the file.
            platform_compat.restrict_to_owner(str(tmp_tar))
            tmp_tar.rename(outfile)
        except BaseException:
            tmp_tar.unlink(missing_ok=True)
            raise
    return outfile


def snapshot_main(
    argv: list[str] | None = None, *, parsed: argparse.Namespace | None = None
) -> int:
    if parsed is None:
        p = argparse.ArgumentParser(
            prog="kirocrew-snapshot",
            description="Create a portable .tar.gz snapshot of Kiro Crew state.",
        )
        p.add_argument("output_dir", nargs="?", default=_default_snapshot_dir())
        p.add_argument("--keep", type=int, default=7)
        p.add_argument("--list", action="store_true", dest="list_snapshots")
        p.add_argument(
            "--allow-unpinned-staging",
            action="store_true",
            dest="allow_unpinned",
            help=(
                "Stage by path name on a platform that cannot open a directory "
                "relative to a descriptor. Without this the snapshot is refused there "
                "rather than taken with a traversal an ancestor swap could redirect. "
                "The archive's MANIFEST.json records that it was staged unpinned."
            ),
        )
        parsed = p.parse_args(argv)
    args = parsed
    allow_unpinned = bool(getattr(args, "allow_unpinned", False))

    if args.keep <= 0:
        print(f"❌ --keep value must be a positive integer, got: {args.keep}")
        return 1

    out = Path(args.output_dir or _default_snapshot_dir())

    if args.list_snapshots:
        if not out.is_dir():
            print(f"No snapshots found in {out}")
            return 0
        snaps = sorted(
            out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True
        )
        for s in snaps:
            print(s)
        if not snaps:
            print(f"No snapshots found in {out}")
        return 0

    mc = _mc_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"kirocrew-snapshot-{ts}"

    # Pre-flight size estimate
    if mc.is_dir():
        total_bytes = sum(
            f.stat().st_size for f in mc.rglob("*") if f.is_file() and not f.is_symlink()
        )
        total_mb = total_bytes / (1024 * 1024)
        if total_mb > 500:
            print(f"⚠️  {mc} is {total_mb:.0f} MB — snapshot may be large and slow")

    # WAL checkpoint
    if (mc / "memory.db").is_file():
        try:
            with closing(sqlite3.connect(str(mc / "memory.db"))) as c:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            print(
                "⚠️  WAL checkpoint failed (DB may be locked by gateway). "
                "The backup API still produces a consistent copy."
            )

    try:
        outfile = _build_snapshot(mc, out, name, allow_unpinned=allow_unpinned)
    except pinned_fs.PinnedPathRefusal as exc:
        # A refusal is a decision this command made on purpose. A traceback would
        # read like a crash and bury the sentence saying what to do about it.
        #
        # It is also a PERMISSION decision, so it belongs in the SEL log next to
        # `state_restore_rejected`. Review's point: the refusals this change introduced
        # returned without auditing, so the one outcome a reviewer would most want a
        # record of -- staging declined on an unsupported platform -- left no trace.
        _audit("snapshot_rejected", f"reason=unpinnable_staging detail={exc}")
        print(f"❌ {exc}")
        return 1

    sz = outfile.stat().st_size
    human = f"{sz // 1024}K" if sz < 1024 * 1024 else f"{sz / 1024 / 1024:.1f}M"
    print(f"✅ Snapshot created: {outfile} ({human})")

    _audit("snapshot_created", f"{outfile} ({human})")

    # Prune
    snaps = sorted(
        out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True
    )
    for old in snaps[args.keep :]:
        old.unlink()
        print(f"🗑  Pruned: {old.name}")

    remaining = len(list(out.glob("kirocrew-snapshot-*.tar.gz")))
    print(f"📦 Snapshots in {out}: {remaining} (keep={args.keep})")
    return 0


# ── Restore ───────────────────────────────────────────────────────────────────


def _print_manifest(snap: Path) -> None:
    mf = snap / "MANIFEST.json"
    if not mf.is_file():
        return
    try:
        m = json.loads(mf.read_text())
        print("📋 Snapshot info:")
        # Every string below comes out of an archive the caller supplied, so all of them
        # go through _terminal_safe. Review named the omission list this change added;
        # created_at, user and hostname are the same class and pre-date this diff. They
        # are fixed here rather than left as a matching hole three lines away, because
        # the renderer makes each one a one-word change and shipping a function that
        # sanitizes two of five attacker-controlled fields would be worse than either
        # extreme. Named as a drive-by rather than smuggled in.
        print(f"  Created: {_terminal_safe(m.get('created_at', 'unknown'))}")
        print(
            f"  From: {_terminal_safe(m.get('user', 'unknown'))}"
            f"@{_terminal_safe(m.get('hostname', 'unknown'))}"
        )
        c = m.get("contents", {})
        print(f"  Memory DB: {c.get('memory_db', 0) // 1024} KB")
        print(f"  Crons: {c.get('crons_json', 0) // 1024} KB")
        print(f"  Workspace files: {c.get('workspace_files', 0)}")
        print(f"  Skills: {c.get('skill_count', 0)}")
        print(f"  Notifications: {c.get('notifications_jsonl', 0) // 1024} KB")
        print(f"  Plan memory files: {c.get('plan_memory_files', 0)}")
        # Both of these are the record that makes an incomplete or weaker archive
        # visible. A value written but never displayed is only findable by untarring
        # the archive by hand, which is not a reader -- so they are shown here, where
        # anyone inspecting a snapshot before restoring it already looks.
        if m.get("staging") == "unpinned":
            print("  ⚠️  Staged by path name (unpinned): see --allow-unpinned-staging")
        for entry in m.get("skipped") or ():
            reason = _terminal_safe(entry.get("reason", "?"))
            omitted = _terminal_safe(entry.get("path", "?"))
            print(f"  ⚠️  Omitted ({reason}): {omitted}")
    except Exception as e:
        print(f"  (Could not read manifest: {e})")


_MERGE_ALLOWED_TABLES = frozenset(
    {
        "semantic_memory",
        "episodic_memories",
        "knowledge_facts",
        "knowledge_edges",
    }
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """Validate a SQL identifier against allowlist pattern. Raises ValueError if invalid."""
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def _merge_memory(src_db: Path, dst_db: Path) -> None:
    # Integrity check on source DB before ATTACH
    try:
        with sqlite3.connect(str(src_db)) as check_conn:
            result = check_conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if result != "ok":
            print(f"  ⚠️  Source DB integrity check failed: {result} — skipping merge")
            return
    except Exception as e:
        print(f"  ⚠️  Source DB unreadable: {e} — skipping merge")
        return

    conn = sqlite3.connect(str(dst_db))
    conn.execute("BEGIN")
    attached = False
    try:
        conn.execute("ATTACH DATABASE ? AS src", (str(src_db),))
        attached = True
        for table, cols, where in [
            (
                "semantic_memory",
                "key, value_json, confidence, source, created_at, updated_at, embedding",
                "WHERE is_deleted=0",
            ),
            (
                "episodic_memories",
                "id, conversation_id, text, embedding, tags, importance, created_at, last_accessed_at",
                "WHERE is_deleted=0",
            ),
            ("knowledge_facts", "subject, predicate, object, episode_id, created_at", ""),
            (
                "knowledge_edges",
                "source_key, target_key, relation, weight, metadata, created_at",
                "",
            ),
        ]:
            if table not in _MERGE_ALLOWED_TABLES:
                raise ValueError(f"Table {table!r} not in merge allowlist")
            for col in cols.split(", "):
                _validate_identifier(col.strip())
            try:
                before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({cols}) "
                    f"SELECT {cols} FROM src.{table} {where}"
                )
                after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                label = table.replace("_", " ").title()
                print(f"  {label} imported: {after - before}")
            except sqlite3.OperationalError as e:
                import logging

                logging.getLogger(__name__).warning("Skipping table %s: %s", table, e)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if attached:
            try:
                conn.execute("DETACH DATABASE src")
            except Exception:
                pass
        conn.close()


def _usable_cron_shape(parsed: object, path: Path) -> bool:
    """Refuse a crons file that parsed but is not shaped like a cron file.

    The merge looks ``jobs`` up on the result and calls ``.get`` on every
    entry, so valid JSON of the wrong shape -- a top level that is not an
    object, a ``jobs`` that is not a list, a job that is not an object, or a
    present name that is not encodable text -- would raise TypeError,
    AttributeError, or UnicodeEncodeError a line or two further down: the
    same crash the read guard exists to prevent, just moved. Only the structure the merge itself relies on is
    checked; the fields of a job are the cron loader's business, not this
    one's. A missing ``jobs`` key keeps its existing meaning of "no jobs".
    """
    if not isinstance(parsed, dict):
        print(f"  ⚠️  {path} is not a cron file — skipping cron merge")
        return False
    jobs = parsed.get("jobs", [])
    if not isinstance(jobs, list) or not all(
        isinstance(job, dict)
        and (
            "name" not in job
            or (
                isinstance(job["name"], str)
                # json.loads accepts lone-surrogate escapes, and a present name
                # is UTF-8 encoded when the import id is hashed: a surrogate
                # would raise UnicodeEncodeError there, the same crash moved.
                and not any("\ud800" <= ch <= "\udfff" for ch in job["name"])
            )
        )
        for job in jobs
    ):
        print(f"  ⚠️  {path} has an unusable job list — skipping cron merge")
        return False
    return True


def _merge_crons(src_path: Path, dst_path: Path) -> None:
    try:
        src = json.loads(src_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ⚠️  Could not read {src_path}: {exc} — skipping cron merge")
        return
    try:
        dst = json.loads(dst_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ⚠️  Could not read {dst_path}: {exc} — skipping cron merge")
        return
    if not _usable_cron_shape(src, src_path) or not _usable_cron_shape(dst, dst_path):
        return
    existing = {j.get("name") for j in dst.get("jobs", [])}
    imported = 0
    for job in src.get("jobs", []):
        name = job.get("name")
        if not name or name in existing:
            continue
        job["id"] = hashlib.md5(f"{name}-imported".encode(), usedforsecurity=False).hexdigest()[:8]
        dst.setdefault("jobs", []).append(job)
        imported += 1
    dst_path.write_text(json.dumps(dst, indent=2))
    total = len(src.get("jobs", []))
    print(f"  Cron jobs imported: {imported} (skipped {total - imported} duplicates)")


def _merge_notifications(src_path: Path, dst_path: Path) -> None:
    existing: set[str] = set()
    with open(dst_path) as f:
        for line in f:
            try:
                existing.add(json.loads(line).get("ts") or line.strip())
            except (ValueError, TypeError):
                pass
    imported = 0
    with open(dst_path, "a") as out, open(src_path) as f:
        for line in f:
            try:
                key = json.loads(line).get("ts") or line.strip()
                if key not in existing:
                    out.write(line)
                    existing.add(key)
                    imported += 1
            except (ValueError, TypeError):
                pass
    print(f"  Notifications imported: {imported}")


def _backup_and_copy(
    mc: Path, backup: Path, snap: Path, component: str, *, allow_unpinned: bool = False
) -> None:
    """Move the live core files aside, then restore the archive's, destination pinned.

    The restore side used to compose ``mc / f`` and hand it to ``shutil.copy2``, so
    the destination was reached by name every time. Two consequences, both real: a
    component of the data home swapped for a link redirected the write out of the
    data home entirely, and a symlink left at the core file's own name was written
    THROUGH rather than refused -- the name-based ``islink`` check above skipped the
    backup move and then the copy followed the link it had just declined to move.

    Now the data home is pinned once and each file is created relative to that
    descriptor with ``O_EXCL``. A name that is still occupied after the backup move
    is refused instead of written through, which is the symlink case above.
    """
    if not _staging_is_pinned(allow_unpinned=allow_unpinned, what=f"restore of {component!r}"):
        for f in CORE_FILES.get(component, ()):
            # Validated before the live file is touched, and a symlink at the live name is
            # MOVED aside rather than skipped -- the same two properties the pinned branch
            # got earlier in this change. Review found this branch still carrying the old
            # behaviour: it skipped both the backup AND the replacement, so the archive's
            # file was never applied and the command reported success anyway. Moving a
            # symlink moves the link, never its target.
            if not (snap / f).is_file() or pinned_fs.is_reparse_point(snap / f):
                if (snap / f).exists():
                    print(f"⚠️  Skipping symlinked file from snapshot: {snap / f}")
                continue
            live = mc / f
            if live.is_symlink() or pinned_fs.is_reparse_point(live):
                print(f"⚠️  Moving symlinked core file aside during backup: {live}")
                shutil.move(str(live), str(backup / f))
            elif live.is_file():
                shutil.move(str(live), str(backup / f))
            # Not `copy2`: it opens the destination by name for writing and follows a
            # symlink planted there in the window after the live file was moved aside,
            # overwriting whatever it points at. Review's finding. copy_file_pinned uses
            # O_CREAT|O_EXCL|O_NOFOLLOW even without a directory descriptor, so a link at
            # the destination name is refused rather than written through.
            # `fatal_skip_reporter`, NOT `_report_skip`: the live file has ALREADY been
            # moved into the backup by this point, so a skip here is not an omission from
            # an archive -- it is the live file gone AND the archive's version never
            # applied, reported as success. That is the whole reason this change has a
            # fatal reporter, and this is the third site to need it: a skip is correct
            # while PRODUCING an archive and is data loss on any path that has already
            # moved or deleted the original. Review caught this site still holding the
            # non-fatal one.
            # A collision here means something recreated the name after the live file was
            # moved aside. It is a real condition, not a skip, but it must not surface as a
            # traceback: review found the same escape on the pinned tree walk, and this
            # path had it too. The live bytes are recoverable from the backup, which is what
            # the message has to say.
            try:
                pinned_fs.copy_file_pinned(
                    str(snap / f),
                    str(mc / f),
                    on_skip=pinned_fs.fatal_skip_reporter(f"restore of {f!r}"),
                )
            except FileExistsError as exc:
                raise pinned_fs.PinnedPathRefusal(
                    f"refusing to restore {f!r}: its name was recreated while the restore "
                    "was running, so writing it would overwrite that file. The previous "
                    f"version is in {backup}. Re-run with the gateway stopped."
                ) from exc
            _lock_down_restored(mc / f, component)
        return

    src_fd = pinned_fs.open_dir_pinned(snap, what=f"snapshot payload for {component!r}")
    try:
        dst_fd = pinned_fs.open_dir_pinned(mc, what=f"data home for {component!r}")
        try:
            backup_fd = pinned_fs.create_and_open_dir_pinned(
                backup, what=f"pre-restore backup for {component!r}"
            )
            try:
                for f in CORE_FILES.get(component, ()):
                    live = mc / f
                    # Checked BEFORE the live file is touched. The archive is untrusted
                    # input, so a member that is not a regular file -- a FIFO, a device
                    # node, a directory at a core filename -- is a real possibility, and
                    # the old order moved the live file aside first and only then found
                    # the source unusable: the original ended up in the backup and
                    # nothing was restored, reported as success. Raised in review; the
                    # same validate-before-mutate ordering the platform gate follows.
                    # Asked through src_fd, not by composing a path. The by-name form
                    # re-resolved the snapshot root, so a root swapped after pinning had
                    # this guard inspecting the replacement while the copy below acted on
                    # the descriptor -- review's finding, and the same class as the
                    # destination-ownership check.
                    if not pinned_fs.is_regular_at(src_fd, f):
                        continue
                    # A symlink at a core file's name is MOVED aside like any other
                    # occupant, not skipped. The old code skipped the move and then let
                    # the copy write through the very link it had just declined to
                    # move; skipping the whole entry instead would be no better,
                    # because the archive's version of that file would then silently
                    # never be restored. Moving a symlink moves the LINK, never its
                    # target, so nothing outside the data home is touched.
                    #
                    # The move goes through both pinned descriptors rather than
                    # shutil.move on two composed paths: review pointed out that a
                    # by-name move re-resolves both ends, so an ancestor swapped
                    # between the check and the move would relocate something else.
                    # os.rename with src_dir_fd/dst_dir_fd cannot be redirected, and it
                    # is atomic within the data home, which a copy-then-delete is not.
                    live_st = pinned_fs.stat_at(dst_fd, f)
                    if live_st is not None and _stat.S_ISLNK(live_st.st_mode):
                        print(f"⚠️  Moving symlinked core file aside during backup: {live}")
                        os.rename(f, f, src_dir_fd=dst_fd, dst_dir_fd=backup_fd)
                    elif live_st is not None and _stat.S_ISREG(live_st.st_mode):
                        os.rename(f, f, src_dir_fd=dst_fd, dst_dir_fd=backup_fd)
                    try:
                        copied = pinned_fs.copy_file_pinned(
                            str(snap / f),
                            dir_fd=src_fd,
                            name=f,
                            dst_dir_fd=dst_fd,
                            dst_name=f,
                            # Owner-only applied through the destination DESCRIPTOR, in
                            # the same call that wrote the bytes. Two things wrong with
                            # the previous _lock_down_restored(mc / f) here, both raised
                            # in review: it reopened the freshly written file BY NAME, so
                            # a link swapped in at that instant had restrict_to_owner
                            # change the permissions of whatever it pointed at; and the
                            # mode cannot be inherited from the archive, which is
                            # untrusted input -- a hand-built tarball can record 0o777 on
                            # telemetry_salt. The reviewer's suggested fix was to drop
                            # the lockdown because "the copy already applies mode", which
                            # would have done exactly that: applied the ARCHIVE's mode.
                            force_mode=0o600 if component == "security" else None,
                            # The live file was moved aside two lines up, so a skip here
                            # finishes with the original gone AND the archive's version
                            # never written. Review's third instance of that rule; it is
                            # now the reporter's job rather than a per-site check.
                            on_skip=pinned_fs.fatal_skip_reporter(f"restore of {f!r}"),
                        )
                    except FileExistsError as exc:
                        raise pinned_fs.PinnedPathRefusal(
                            f"refusing to restore {f!r}: something still occupies that "
                            "name in the data home after the backup pass, so it is a "
                            "hardlink alias or a name this restore could not move "
                            "aside. Writing to it could follow whatever it points at. "
                            "Remove it and re-run."
                        ) from exc
                    if copied and component == "security":
                        # Nothing to re-apply: force_mode above already set owner-only
                        # through the descriptor. On Windows the by-name branch still
                        # needs restrict_to_owner for its DACL, which is why that call
                        # survives there and not here.
                        pass
            finally:
                os.close(backup_fd)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def _copy_locked(src: Path, dst: Path) -> bool:
    """Copy *src* onto a missing *dst*, owner-only before the name is published.

    Merge restore only copies when *dst* is absent. Publish with ``os.link``
    (the create-only shape ``_get_telemetry_salt`` uses) so a dest that
    appears in the window — ``--force`` restore racing a live gateway
    creating ``telemetry_salt`` — raises ``FileExistsError`` and the live
    file is left alone. ``os.replace`` / ``atomic_write`` would clobber it.
    The temp is locked down before any payload is written. Failures to
    publish (unsupported hard links, a restrict error, a size mismatch)
    skip this file without raising, because merge restore has already
    applied earlier components and ``_get_telemetry_salt`` regenerates a
    missing salt. Return True only when this call published *dst*.
    """
    if dst.exists():
        return False
    if src.name == "telemetry_salt" or dst.name == "telemetry_salt":
        size = src.stat().st_size
        if size != _TELEMETRY_SALT_BYTES:
            return False
    payload = src.read_bytes()
    if dst.exists():
        return False
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    tmp_path = Path(tmp)
    try:
        platform_compat.restrict_to_owner(str(tmp_path))
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(f"short write restoring {src.name}")
            view = view[written:]
        # Drop the fd from finally before close: a close-time writeback
        # error must not be retried on an already-released descriptor,
        # because a second OSError in finally would replace the skip and
        # abort merge after earlier components were applied.
        pending = fd
        fd = -1
        os.close(pending)
        os.link(str(tmp_path), str(dst))
        return True
    except OSError:
        # FileExistsError: live dest won. EXDEV / EPERM / no-hardlink /
        # restrict / short write / close: dest stays missing and
        # `_get_telemetry_salt` regenerates. Raising here aborts merge
        # after earlier components were already applied.
        return False
    finally:
        if fd >= 0:
            pending = fd
            fd = -1
            try:
                os.close(pending)
            except OSError:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _lock_down_restored(path: Path, component: str) -> None:
    """Apply the owner-only lockdown a restored security file needs.

    restrict_to_owner (fail-loud), NOT chmod_safe (which swallows OSError): security
    files include sel_hmac.key. Mirrors the create path's deliberate fail-loud
    lockdown -- better to abort than silently land a restored secret group- or
    world-readable. POSIX applies chmod 0o600; Windows applies an owner-only DACL via
    icacls. The freshly copied file is unlinked on failure so the abort this promises
    actually removes the exposed artifact, instead of leaving the restored secret
    under the destination's inherited DACL after the OSError propagates.
    """
    if component != "security":
        return
    try:
        platform_compat.restrict_to_owner(str(path))
    except OSError:
        path.unlink(missing_ok=True)
        raise


def _backup_tree_or_refuse(src: Path, dst: Path, *, allow_unpinned: bool = False) -> None:
    """Back a live tree up, and refuse the replace if the backup is not complete.

    Replace mode later runs ``rmtree`` on the live tree, so a file the backup pass
    SKIPPED is a file the restore is about to delete with no copy anywhere -- and the
    call site is deliberately hoisted ahead of every live mutation, so a refusal
    arrives while nothing has been swapped yet (#2844). The staging
    walk legitimately skips a hardlink alias, a symlink and a non-regular file -- which
    is right when producing an archive and catastrophic here, because the skip is
    followed by a delete rather than by an omission.

    This was the second of the two findings that closed the earlier attempt at this
    change (#2446): a concurrent writer's hardlinks were skipped at backup time and then
    ``rmtree`` removed the only copies. Raised again in review against this diff, which
    had carried the same shape forward. Refusing before the delete is the only ordering
    that cannot lose data: the operator keeps a complete tree and a message naming what
    could not be copied.
    """
    _copytree_safe(
        src,
        dst,
        allow_unpinned=allow_unpinned,
        on_skip=pinned_fs.fatal_skip_reporter(f"backup of {src.name!r} before replacing it"),
    )


def _do_replace(
    snap: Path, mc: Path, components: list[str] | None, *, allow_unpinned: bool = False
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = mc / f"pre-restore-{ts}"
    backup.mkdir(exist_ok=True)
    print("🔄 Replace mode — backing up current state...")

    # The ENTIRE rollback set is completed before the first live mutation. A tree
    # backup can refuse (its fatal skip reporter raises on an entry it cannot
    # copy, e.g. a hardlink alias or a symlink), and refusing is only safe
    # while nothing has been swapped yet: the previous ordering ran the core-file
    # swap loop first, so a tree-backup refusal aborted with the new databases
    # live and the old trees live -- mixed state, with a rollback set missing the
    # very trees the abort was protecting. Same
    # complete-validation-before-first-mutation ordering the unsafe-root check
    # already follows, applied to the rollback copy (issue #2844, failure mode 3).
    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            d = mc / dirname
            if d.is_dir():
                _backup_tree_or_refuse(d, backup / dirname, allow_unpinned=allow_unpinned)
    if _want(components, "skills"):
        sk = mc / "skills"
        if sk.is_dir():
            _backup_tree_or_refuse(sk, backup / "skills", allow_unpinned=allow_unpinned)

    for comp in ("memory", "crons", "config", "notifications", "security"):
        if _want(components, comp):
            _backup_and_copy(mc, backup, snap, comp, allow_unpinned=allow_unpinned)
            print(f"  ✅ {comp}")

    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            d = mc / dirname
            sd = snap / dirname
            if sd.is_dir():
                if d.is_dir():
                    shutil.rmtree(str(d))
                # rmtree just removed the live tree, so a skipped source entry here means
                # that file exists in neither place.
                #
                # `must_create` is what makes the removal mean something. Without it the
                # walk accepted a root recreated between the rmtree and the copy, so files
                # the archive does not contain survived a REPLACE that reported success.
                # Review found it; this is the only caller that knows it removed the tree.
                _copytree_safe(
                    sd,
                    d,
                    allow_unpinned=allow_unpinned,
                    must_create=True,
                    on_skip=pinned_fs.fatal_skip_reporter(f"restore of {dirname!r}"),
                )
        print("  ✅ workspace")

    if _want(components, "skills"):
        sk = mc / "skills"
        snap_sk = snap / "skills"
        if snap_sk.is_dir():
            if sk.is_dir():
                shutil.rmtree(str(sk))
            _copytree_safe(
                snap_sk,
                sk,
                allow_unpinned=allow_unpinned,
                # Same as the workspace branch above: the tree was just removed, so a root
                # that exists again was recreated by something else.
                must_create=True,
                on_skip=pinned_fs.fatal_skip_reporter("restore of 'skills'"),
            )
        print("  ✅ skills")

    try:
        backup.rmdir()
    except OSError:
        print(f"  Previous state saved to: {backup}/")
    print("✅ Replace complete.")


def _do_merge(
    snap: Path, mc: Path, components: list[str] | None, *, allow_unpinned: bool = False
) -> None:
    # Asked once, at entry, BEFORE any mutation. The core-file copies below run before
    # any tree call, so gating inside the tree helpers meant a merge on a platform that
    # cannot pin wrote memory.db, crons.json and the security files first and only then
    # met the refusal -- either redirecting those writes through a planted link, or
    # aborting with the restore already half applied. Review caught it; it is the same
    # gate-placement defect as the snapshot side, one path over.
    _staging_is_pinned(allow_unpinned=allow_unpinned, what="merge restore")
    print("🔀 Merge mode — importing...")

    if _want(components, "memory") and (snap / "memory.db").is_file():
        if not (mc / "memory.db").is_file():
            shutil.copy2(str(snap / "memory.db"), str(mc / "memory.db"))
            if (snap / "memory_index.db").is_file():
                shutil.copy2(str(snap / "memory_index.db"), str(mc / "memory_index.db"))
            print("  Memory: copied (no existing memory.db)")
        else:
            _merge_memory(snap / "memory.db", mc / "memory.db")
        print("  ✅ memory")

    if _want(components, "crons"):
        sc, dc = snap / "crons.json", mc / "crons.json"
        if sc.is_file():
            if dc.is_file():
                _merge_crons(sc, dc)
            else:
                shutil.copy2(str(sc), str(dc))
                print("  Crons: copied (no existing crons)")
        print("  ✅ crons")

    if _want(components, "config"):
        for f in CORE_FILES["config"]:
            s, d = snap / f, mc / f
            if s.is_file() and not d.is_file():
                shutil.copy2(str(s), str(d))
                print(f"  {f}: restored (was missing)")
        print("  ✅ config")

    if _want(components, "notifications"):
        sn, dn = snap / "notifications.jsonl", mc / "notifications.jsonl"
        if sn.is_file():
            if dn.is_file():
                _merge_notifications(sn, dn)
            else:
                shutil.copy2(str(sn), str(dn))
                print("  Notifications: copied")
        print("  ✅ notifications")

    if _want(components, "security"):
        for f in CORE_FILES["security"]:
            s, d = snap / f, mc / f
            if s.is_file() and not d.is_file():
                if _copy_locked(s, d):
                    print(f"  {f}: restored (was missing)")
        print("  ✅ security")

    if _want(components, "workspace"):
        for dirname in ("workspace", "plan_memory"):
            sd = snap / dirname
            if sd.is_dir():
                dd = mc / dirname
                dd.mkdir(parents=True, exist_ok=True)
                _copy_tree_no_overwrite(sd, dd, allow_unpinned=allow_unpinned)
        print("  ✅ workspace")

    if _want(components, "skills"):
        if (snap / "skills").is_dir():
            (mc / "skills").mkdir(parents=True, exist_ok=True)
            _copy_tree_no_overwrite(snap / "skills", mc / "skills", allow_unpinned=allow_unpinned)
        print("  ✅ skills")

    print("✅ Merge complete.")


def _is_gateway_running() -> bool:
    """Check if the KiroCrew gateway is listening on its dashboard port."""
    # Deterministic override (used by tests / scripted restores) — avoids a real
    # socket probe whose result is environment-dependent.
    override = os.environ.get("KIROCREW_ASSUME_GATEWAY_RUNNING")
    if override is not None:
        return override.strip().lower() not in ("", "0", "false", "no")
    port = _DASHBOARD_PORT
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def restore_main(argv: list[str] | None = None, *, parsed: argparse.Namespace | None = None) -> int:
    if parsed is None:
        p = argparse.ArgumentParser(
            prog="kirocrew-restore", description="Restore KiroCrew state from a snapshot."
        )
        p.add_argument("snapshot", nargs="?")
        p.add_argument("--mode", choices=("replace", "merge"))
        p.add_argument("--dry-run", action="store_true")
        p.add_argument(
            "--force", action="store_true", help="Allow restore even if gateway is running"
        )
        p.add_argument("--components")
        p.add_argument("--list-components", action="store_true")
        p.add_argument(
            "--allow-unpinned-staging",
            action="store_true",
            dest="allow_unpinned",
            help=(
                "Restore by path name on a platform that cannot open a directory "
                "relative to a descriptor. Without this the restore is refused there "
                "rather than run with a destination an ancestor swap could redirect."
            ),
        )
        parsed = p.parse_args(argv)
    args = parsed
    allow_unpinned = bool(getattr(args, "allow_unpinned", False))

    if args.list_components:
        _list_components()
        return 0

    if not args.snapshot:
        print("❌ snapshot file is required (unless --list-components is given)")
        return 1

    force = getattr(args, "force", False)
    if not force and _is_gateway_running():
        _audit("state_restore_rejected", "reason=gateway_running")
        print("❌ Gateway is running. Stop it first (kirocrew stop) or use --force.")
        return 1

    snap_path = Path(args.snapshot)
    if not snap_path.is_file():
        print(f"❌ File not found: {snap_path}")
        return 1

    # Parse components
    components: list[str] | None = None
    if args.components:
        components = [c.strip() for c in args.components.split(",")]
        for c in components:
            if c not in VALID_COMPONENTS:
                print(f"❌ Unknown component: {c}\n")
                _list_components()
                return 1

    mc = _mc_dir()
    mode = args.mode or ("merge" if (mc / "memory.db").is_file() else "replace")

    with tempfile.TemporaryDirectory() as work_str:
        work = Path(work_str)

        # Security checks are enforced inside _data_filter (no TOCTOU gap)
        with tarfile.open(str(snap_path), "r:gz") as tar:
            try:
                tar.extractall(work, filter=_data_filter)
            except TypeError:
                # Python < 3.11.4: filter param not supported, apply manually
                members = [m for m in tar.getmembers() if _data_filter(m) is not None]
                tar.extractall(work, members=members)

        snap_dirs = [
            d for d in work.iterdir() if d.is_dir() and d.name.startswith("kirocrew-snapshot-")
        ]
        if not snap_dirs:
            print("❌ Invalid snapshot format")
            return 1
        snap = snap_dirs[0]

        _print_manifest(snap)
        if components:
            print(f"🔧 Components: {','.join(components)}")

        if args.dry_run:
            print(f"\n🔍 Dry run — would restore to {mc} in {mode} mode")
            print("Files in snapshot:")
            for f in sorted(snap.rglob("*")):
                if f.is_file():
                    print(f"  {f.relative_to(snap)}")
            return 0

        mc.mkdir(parents=True, exist_ok=True)
        # Contained here rather than allowed to propagate: a refusal is a decision
        # this command made on purpose, and a traceback would read like a crash and
        # bury the one sentence saying what to do about it.
        try:
            if mode == "replace":
                _do_replace(snap, mc, components, allow_unpinned=allow_unpinned)
            else:
                _do_merge(snap, mc, components, allow_unpinned=allow_unpinned)
        except pinned_fs.PinnedPathRefusal as exc:
            # Same reasoning as the snapshot handler, and this one reuses the event name
            # already established for a declined restore rather than inventing a second.
            _audit("state_restore_rejected", f"reason=unpinnable_staging detail={exc}")
            print(f"❌ {exc}")
            return 1

    # Integrity check
    if _want(components, "memory") and (mc / "memory.db").is_file():
        try:
            with sqlite3.connect(str(mc / "memory.db")) as conn:
                result = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        except Exception as e:
            result = str(e)
        if result == "ok":
            print("🔍 memory.db integrity: OK")
        else:
            print(f"⚠️  memory.db integrity check failed: {result}")
            _audit("state_restore_rejected", f"reason=integrity_check_failed from={snap_path.name}")
            return 1
        if not (mc / "memory_index.db").is_file():
            print(
                "⚠️  memory_index.db is missing — full-text search may not "
                "work until the FTS index is rebuilt."
            )

    comp_str = ",".join(components) if components else "all"
    _audit("state_restored", f"mode={mode} components={comp_str} from={snap_path.name}")

    print("\n⚠️  Restart kirocrew gateway to pick up changes: kirocrew restart")
    return 0
