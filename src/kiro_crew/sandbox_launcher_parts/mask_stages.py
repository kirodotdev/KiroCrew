"""The stages that place the namespace launcher's windows, seals and masks.

One of the launcher program's parts (see :mod:`kiro_crew.sandbox_launcher_parts`). Below its
marker line this file is the program's text from ``check_crew_home_aliases`` to
``confirm_crew_home_aliases``: the crew-home alias checks on both sides of the masks, the
pre-read of the files the masks re-expose, and every stage ``place_masks`` runs -- staging the
private windows, sealing the read-only paths, masking the sensitive directories, re-opening the
write carve-outs, restoring the exposed files, checking the credential aliases, and masking the
sensitive files and ``~/.ssh``. A new launcher stage goes here, and ``place_masks`` or
``run_child`` in :mod:`kiro_crew.sandbox_launcher_program` names its place in the order.
"""

import ctypes
import os
import stat
import sys
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.sandbox_launcher_program import (
        _MNT_DETACH,
        _MS_BIND,
        _MS_NODEV,
        _MS_NOEXEC,
        _MS_NOSUID,
        _MS_RDONLY,
        _MS_REMOUNT,
        _PR_SET_DUMPABLE,
        _any_kind,
        _carried_occupant,
        _locked_mount_flags,
        _mask_required,
        _mount_or_die,
        _mount_or_warn,
        _pin_mount_path,
        _register_stand_in,
        _retire_stage_or_die,
        _stand_in_identity,
        _stand_in_mkdtemp,
        _stand_in_mkstemp,
        _verify_masked_name,
    )


# -- the launcher program continues below this line --
def check_crew_home_aliases(launch):
    """Refuse unless every crew-home alias still resolves to the data home it was folded onto.

    A crew-home alias is a ``$HOME`` spelling the planner folded onto the resolved data
    home because, when the pre-spawn pass looked, the alias's components resolved to that
    directory. The link that makes them one is a name, so it is read again HERE, before
    any mask is placed: an alias that resolves elsewhere now would leave whatever it
    reaches unmasked under rules that were folded away. The test is RESOLUTION, as the
    pass's was, not ``(st_dev, st_ino)`` equality: a second mount of the data home
    reports the same identity under another name, and a mask placed on the canonical
    entry does not appear under a second mount. The canonical spelling is held to the
    identity the pass recorded, because that IS a question about an object: a directory
    swapped in under the canonical name would take the folded masks while the original
    sat unmasked wherever it went. The canonical spelling is not itself resolved -- a
    default data home at ``~/.kiro/crew`` may be a link to ``~/.kirocrew`` -- so BOTH
    sides are resolved before they are compared.
    """
    for _alias, _canonical, _alias_dev, _alias_ino in launch.crew_home_aliases:
        try:
            _canonical_st = os.stat(_canonical)
            _canonical_resolved = os.path.realpath(_canonical, strict=True)
            _alias_resolved = os.path.realpath(_alias, strict=True)
        except OSError as _alias_exc:
            sys.exit(
                "sandbox: BLOCKED -- %s reached the data home %s when this spawn was "
                "prepared and one of them cannot be read now (%s), so the rules "
                "folded onto the data home may not cover what the alias reaches. "
                "Lower agent.sandbox to run without this control deliberately."
                % (_alias, _canonical, _alias_exc)
            )
        if (_canonical_st.st_dev, _canonical_st.st_ino) != (_alias_dev, _alias_ino):
            sys.exit(
                "sandbox: BLOCKED -- %s reached the data home %s when this spawn was "
                "prepared and %s holds a different directory now, so the rules folded "
                "onto the data home would cover the replacement and leave the original "
                "unmasked. Another process replaced that name. Lower agent.sandbox to "
                "run without this control deliberately." % (_alias, _canonical, _canonical)
            )
        if _alias_resolved != _canonical_resolved:
            sys.exit(
                "sandbox: BLOCKED -- %s reached the data home %s when this spawn was "
                "prepared and reaches a different directory now (%s), so the rules "
                "folded onto the data home would leave what the alias reaches "
                "unmasked. Another process re-aimed that name. Lower agent.sandbox "
                "to run without this control deliberately." % (_alias, _canonical, _alias_resolved)
            )


def preread_exposed_files(launch):
    """Read the files that must survive the masks over their parents.

    An expose source that cannot be READ degrades to "not exposed" with a stderr
    warning, the same way the hardlink scan degrades open. This read runs during sandbox
    SETUP, so letting the OSError propagate aborts the child before the command runs at
    all -- and selective exposure is an OPTIMIZATION (keep ~/.aws/config reachable so
    credential_process still resolves inside an otherwise-hidden ~/.aws), never a
    security control. Failing the whole spawn because an optional convenience is
    unreadable trades a working sandbox for no sandbox.

    `isfile` already covers ABSENT; this covers UNREADABLE, and the two are not the same
    test: `stat` can succeed on a path whose `open` is then denied. Seen in the wild as
    a filesystem restriction inherited from the parent process, denying read on a 0600
    file the child's own uid owned -- so DAC bits and uid both looked correct while every
    cc-mode spawn on that host died here.

    Catching the error is the only guard that HOLDS. Do not "tighten" this into a
    pre-flight `os.access(src_path, os.R_OK)`: measured on the affected host, `os.stat()`
    succeeded and `os.access()` reported BOTH X_OK and R_OK as True while the operation
    was denied anyway. The weaker check looks equivalent from the source alone and would
    silently restore the abort.

    The warning is not optional. Skipping silently would leave the child with no
    ~/.aws/config and no explanation, turning a loud setup failure into a later auth
    failure that points nowhere near this line.
    """
    for src_path, _filename in launch.expose_files:
        if os.path.isfile(src_path):
            try:
                with open(src_path, "rb") as fh:
                    launch.expose_data[src_path] = fh.read()
            except OSError as exc:
                print(
                    "sandbox: WARNING — cannot read %s (%s); it will be "
                    "ABSENT inside the sandbox. Anything depending on it "
                    "(e.g. credential_process in ~/.aws/config) will fail." % (src_path, exc),
                    file=sys.stderr,
                )


def _pin_below_mask(root, leaf):
    """A descriptor on *leaf*, opened one no-follow component at a time from *root*.

    A single ``O_NOFOLLOW`` open of the whole path refuses a link only at the last
    component, so a swapped ANCESTOR ("apps/alpha" made a link to "apps/aws-control")
    would still be traversed and its masked leaf staged. Every component from the
    window's masked root down is opened descriptor-relative instead. ``O_DIRECTORY``
    refuses a non-directory in the same step.
    """
    fd = os.open(root, os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        for part in os.path.relpath(leaf, root).split(os.sep):
            if part in ("", ".", ".."):
                raise OSError("window path does not descend from its mask entry")
            nxt = os.open(part, os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def stage_private_windows(launch):
    """Stage each private window's real inode before its parent tree is masked.

    A private window is a directory INSIDE a hidden tree that stays visible read-write
    for THIS spawn only (the process's own scratch under the masked scratch root). It is
    staged before its parent is masked, because the mask shadows the real path; the
    window is then bound onto a placeholder created inside the parent's empty stand-in,
    so every sibling stays hidden.

    Each window is PINNED before it is staged, and the bind source is the descriptor
    rather than the name. The parent validated this window by pathname, and the data
    home is writable by same-uid agent processes, so between that check and this bind
    another process can swap a link in for the window's own name, or for ANY name below
    the mask; a following bind would then stage the link's target and re-expose it
    read-write to the child. ``/proc/self/fd/<n>`` resolves to the inode the
    descriptor already holds, so no name is resolved twice.

    The walk starts at the mask entry rather than at "/" because the crew data HOME is
    documented as allowed to be a symlink, and its own ancestors are not ours to police;
    the mask entry itself takes O_NOFOLLOW, which is the refusal the parent's own
    validation of that name mirrors. A window that cannot be pinned is SKIPPED, which
    leaves the parent's mask over the path -- the fail-closed direction.
    """
    for p in launch.private_dirs:
        # The window's own mask entry, longest match: every private window is a proper
        # descendant of one, because both lists are built from the same hidden set, and
        # the mask stage re-opens a window only under the entry it matches.
        _mask_root = ""
        for _d in launch.sensitive_dirs:
            _d = _d.rstrip("/")
            if p.startswith(_d + "/") and len(_d) > len(_mask_root):
                _mask_root = _d
        if not _mask_root:
            continue
        _want_id = launch.private_dir_ids.get(p)
        try:
            _win_fd = _pin_below_mask(_mask_root, p)
        except (OSError, ValueError) as _win_exc:
            # A window the producer opened and VOUCHED FOR must open here too, so a
            # failure is a refusal. Skipping it leaves the parent's mask over the path,
            # and that mask is an empty WRITABLE bind: the child's writes under it
            # succeed and go away with the namespace, which is silent loss of the one tree
            # whose durability is the whole reason the window exists. A window with no
            # approved identity is skipped instead, because nothing vouched for it.
            if _want_id is not None:
                sys.exit(
                    "sandbox: BLOCKED -- cannot open %s, which this spawn approved as "
                    "a data window (%s)" % (p, _win_exc)
                )
            continue
        try:
            # ``O_NOFOLLOW`` at every component refuses a LINK planted at the name and
            # settles nothing else: a real directory RENAMED onto an approved name opens
            # and pins cleanly, and the vacated original then fails the mask stage's own
            # kind check, so a substitute would be staged read-write AND take that tree's
            # mask with it. The descriptor already holds the inode, so compare it with
            # what the producer approved. A MISMATCH refuses the spawn: the product's own
            # installer reaches this state without any hostile peer -- it copies a fresh
            # ``data`` from the package before restoring the preserved tree -- and a
            # refused spawn runs again on its next schedule, while a discarded write is
            # gone.
            if _want_id is not None:
                _win_st = os.fstat(_win_fd)
                if [_win_st.st_dev, _win_st.st_ino] != _want_id:
                    sys.exit(
                        "sandbox: BLOCKED -- %s is not the directory this spawn "
                        "approved as a data window" % p
                    )
            _stage_dir = _stand_in_mkdtemp(launch)
            _mount_or_die(
                launch,
                ("/proc/self/fd/%d" % _win_fd).encode(),
                _stage_dir.encode(),
                _MS_BIND,
                "staging private window %s" % p,
            )
            launch.private_stage[p] = _stage_dir
        finally:
            os.close(_win_fd)


def seal_readonly(launch):
    """Bind each read-only dir over itself and remount that bind ``MS_RDONLY``.

    Both steps are load-bearing -- MS_RDONLY is ignored on the initial MS_BIND, so
    without the remount this would grant exactly the write access it exists to withhold.
    Creating the bind ourselves is necessary but NOT sufficient inside a user namespace:
    the kernel locks the source mount's nosuid/nodev/noexec bits and rejects a remount
    that would drop them, so the seal re-asserts them via ``_locked_mount_flags``.

    MUST run BEFORE :func:`mask_sensitive`. A non-recursive MS_BIND does not replicate
    submounts, so a self-bind of a parent established AFTER a hide of one of its leaves
    masks that hide: lookups through the new parent mount reach the REAL leaf. That is
    exactly the ``run`` / ``run/voice-runtime`` pair -- the runtime parent is sealed here
    and its decoder leaf is hidden after -- and with the stages the other way round the
    hide degraded to read-only-visible (container measured: the marker inside the leaf
    was readable, writes EROFS). Seal first, hide second: a hide placed ON a sealed
    parent is a mount on top of it and stays reachable through it, the same kernel
    property the write carve-outs rely on. Window staging stays ahead of this stage on
    purpose, so a window's stage bind is never taken from an already-sealed source.
    """
    for d in launch.readonly_dirs:
        target = d.encode()
        # Any KIND passes: a governance ceiling is a plain file (``security_policy.json``),
        # and bind-over-self + MS_RDONLY seals a regular file exactly as it seals a
        # directory.
        _seal_fd, _seal_target = _pin_mount_path(
            launch, target, _any_kind, require_present=_mask_required(launch, target)
        )
        if _seal_target is None:
            continue
        try:
            _seal_id = os.fstat(_seal_fd)
            _mount_or_die(
                launch, _seal_target, _seal_target, _MS_BIND, "exposing read-only path %s" % d
            )
        finally:
            os.close(_seal_fd)
        # The seal is the REMOUNT, and a remount can only name the mount the bind just
        # created -- which no descriptor taken before that bind can name, since such a
        # descriptor still refers to the mount underneath. So the name is resolved once
        # more, and the object it reaches is REQUIRED to be the object the bind covered:
        # a bind of a path over itself leaves the device and inode unchanged, so a
        # mismatch means the name now reaches something else and the seal would land off
        # target, leaving this ceiling writable.
        _rdonly_fd, _rdonly_target = _pin_mount_path(launch, target, _any_kind)
        if _rdonly_target is not None:
            try:
                _rdonly_id = os.fstat(_rdonly_fd)
                _same = (
                    _rdonly_id.st_dev == _seal_id.st_dev and _rdonly_id.st_ino == _seal_id.st_ino
                )
                if _same:
                    _mount_or_die(
                        launch,
                        _rdonly_target,
                        _rdonly_target,
                        _MS_REMOUNT | _MS_BIND | _MS_RDONLY | _locked_mount_flags(_rdonly_target),
                        "sealing read-only path %s" % d,
                    )
                    # AFTER the remount, resolve the NAME once more and require it STILL
                    # reaches the object just sealed. The two pins above bracket only the
                    # bind; an atomic replacement landing between the second pin and this
                    # remount would seal the OLD object while the configured name now
                    # reaches a writable one, and nothing downstream re-checks it.
                    _post_fd, _post_target = _pin_mount_path(launch, target, _any_kind)
                    if _post_target is None:
                        _same = False
                    else:
                        try:
                            _post_id = os.fstat(_post_fd)
                            _same = (
                                _post_id.st_dev == _seal_id.st_dev
                                and _post_id.st_ino == _seal_id.st_ino
                            )
                        finally:
                            os.close(_post_fd)
            finally:
                os.close(_rdonly_fd)
        else:
            _same = False
        if not _same:
            sys.exit(
                "sandbox: BLOCKED -- %s changed identity between being bound "
                "and being sealed, so the read-only seal would apply to a "
                "different object and this path would stay writable. Another "
                "process is rewriting that name. Lower agent.sandbox to run "
                "without this control deliberately." % d
            )


def _seal_masked_dir_if_windowless(launch, name, has_window):
    """Remount an opted-in masked directory's stand-in read-only when nothing writes to it.

    The hide this follows is a bind of a fresh empty directory, and on Linux that bind
    is a WRITABLE empty tmpfs: with no read-only remount a write into the masked path
    lands in that disposable backing and is lost when the namespace is torn down, so the
    job reports success while nothing was persisted. For a masked directory the caller
    explicitly OPTED IN to sealing (``seal_windowless_masks``) and that has no writable
    window, there is no legitimate in-sandbox writer, so the stand-in is sealed read-only
    and the write fails loudly with EROFS instead -- the fail-safe side of a data-loss
    choice.

    SCOPE is the opt-in set alone, and the planner puts a mask there only when its
    creator asked (``SandboxRequest.seal_hidden_dirs`` -> ``Mask.seal_readonly``). It is
    NOT inferred from origin, exposure or path. So a tier host-credential directory
    (``.aws``, ``.docker``, ``.config/gcloud`` ...), a mask that re-exposes a file read-only
    through ``restore_exposed_files`` (``.aws`` in cc mode), and every other mask that did
    not opt in keep their writable stand-in -- which is what the launcher's own
    ``restore_exposed_files`` and the vendor CLIs depend on. A directory absent from the
    set keeps main's writable-stand-in behaviour.

    A masked directory that DOES carry a window keeps its stand-in writable even when it
    opted in: the window is the explicit write surface, and the launcher binds it on top
    of this stand-in. ``has_window`` is the caller's answer to whether any private window
    sits at or below *name*.

    The remount names the NAME, the same way :func:`mask_sensitive_files` seals its
    unreadable file masks, and re-asserts the kernel-locked nosuid/nodev/noexec bits via
    :func:`_locked_mount_flags`. ``_verify_masked_name`` has already confirmed the name
    reaches this launcher's own stand-in before this runs, so the remount cannot land on
    a racing writer's substitute.
    """
    if has_window or os.fsdecode(name) not in launch.seal_windowless_masks:
        return
    _mount_or_die(
        launch,
        name,
        name,
        _MS_REMOUNT | _MS_BIND | _MS_RDONLY | _locked_mount_flags(name),
        "sealing masked directory %s read-only" % os.fsdecode(name),
    )


def mask_sensitive(launch):
    """Bind an empty directory over each sensitive dir, re-open its windows, re-hide nested leaves.

    A per-dir stand-in, so no content leaks across mounts through a shared backing dir.
    Runs AFTER :func:`seal_readonly`: a hidden leaf nested under a sealed parent
    (``run/voice-runtime`` under ``run``) must be hidden on top of the parent's
    self-bind, never underneath it, or the non-recursive parent bind masks the hide.
    Every staging mount is retired at the end, once every window is bound and every
    nested mask re-applied.
    """
    for d in launch.sensitive_dirs:
        # Two kinds of mask root, one loop. A root the producer VOUCHED FOR
        # (``sensitive_dir_ids``) must be the same directory here: a same-UID process
        # outside this child can rename a real directory onto the name, and the mask
        # then covers the substitute while the tree it was asked to hide stays readable
        # at its new name. ``O_NOFOLLOW`` refuses a link at the name, which matches the
        # producer's own validation of it, and says nothing about a rename, so the inode
        # the descriptor already holds is compared. A mismatch, and an absent name the
        # producer saw as a directory, are refusals rather than skips: this loop's skip
        # leaves the tree unmasked. Every OTHER root is pinned once, following a symlink
        # at the name, so a supported symlinked layout keeps working; the carried
        # occupant identity is what refuses a swapped object there.
        _want_dir_id = launch.sensitive_dir_ids.get(d)
        _mask_fd = -1
        if _want_dir_id is not None:
            try:
                _mask_fd = os.open(d, os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY)
            except OSError as exc:
                sys.exit(
                    "sandbox: BLOCKED -- cannot open approved mask root %s (%s)" % (d, exc.strerror)
                )
            _mask_st = os.fstat(_mask_fd)
            if [_mask_st.st_dev, _mask_st.st_ino] != _want_dir_id:
                os.close(_mask_fd)
                sys.exit(
                    "sandbox: BLOCKED -- %s is not the directory this spawn approved for masking"
                    % d
                )
            # The DESCRIPTOR is the mount target from here on: comparing the inode and
            # then mounting on the NAME would let the rename land in between.
            target = ("/proc/self/fd/%d" % _mask_fd).encode()
        else:
            _mask_fd, target = _pin_mount_path(
                launch, d.encode(), stat.S_ISDIR, require_present=_mask_required(launch, d)
            )
            if target is None:
                continue
        # Held open until the mount is done; every failure in between ends the process,
        # so the descriptor path stays valid for exactly the mount.
        _windows = [p for p in launch.private_stage if p.startswith(d.rstrip("/") + "/")]
        try:
            per_dir_empty = _stand_in_mkdtemp(launch).encode()
            _per_dir_id = _stand_in_identity(per_dir_empty)
            _register_stand_in(launch, _per_dir_id, _mask_fd)
            for p in _windows:
                os.makedirs(os.path.join(per_dir_empty.decode(), os.path.relpath(p, d)))
            _mount_or_die(
                launch, per_dir_empty, target, _MS_BIND, "hiding credential directory %s" % d
            )
        finally:
            os.close(_mask_fd)
        # Checked BEFORE the windows mount, so this answers about the mask itself rather
        # than about anything opened inside it.
        _verify_masked_name(launch, d.encode(), _per_dir_id, d)
        launch.masked_names[d.rstrip("/")] = _per_dir_id
        # The window targets resolve INSIDE the empty stand-in just mounted, which this
        # launcher created with mkdtemp moments ago, so no other writer can have placed
        # anything at those names.
        for p in _windows:
            _mount_or_die(
                launch,
                launch.private_stage[p].encode(),
                p.encode(),
                _MS_BIND,
                "opening private window %s" % p,
            )
            launch.bound_windows.add(p.rstrip("/"))
        # No window inside this mask means no in-sandbox writer should reach it: seal the
        # empty stand-in read-only so a write fails loudly rather than landing in the
        # throwaway tmpfs. A windowed mask keeps its stand-in writable (the window IS the
        # write surface) -- the nested re-hides below land on top either way.
        _seal_masked_dir_if_windowless(launch, d.encode(), bool(_windows))
        # A window may CONTAIN a masked leaf -- ``apps/meetings/data`` holds the masked
        # ``apps/meetings/data/edits`` -- and the bind above just replaced the empty
        # stand-in that covered it with the real tree. Re-apply those nested masks NOW,
        # after the window, which is the ordering the gate relies on when it admits a
        # containing window: applied before it, they land on a path the window then
        # shadows and the leaf comes back live. The nested name resolves into the REAL
        # host tree once the window is bound, so it takes the same pin-and-verify as every
        # other hiding mount. Fresh empty dir per leaf, as the mask loop itself does.
        for p in _windows:
            for _nested in launch.sensitive_dirs:
                _nested = _nested.rstrip("/")
                if not _nested.startswith(p.rstrip("/") + "/"):
                    continue
                _nested_fd, _nested_target = _pin_mount_path(
                    launch,
                    _nested.encode(),
                    stat.S_ISDIR,
                    require_present=_mask_required(launch, _nested),
                )
                if _nested_target is None:
                    continue
                try:
                    _nested_empty = _stand_in_mkdtemp(launch).encode()
                    _nested_id = _stand_in_identity(_nested_empty)
                    _register_stand_in(launch, _nested_id, _nested_fd)
                    _mount_or_die(
                        launch,
                        _nested_empty,
                        _nested_target,
                        _MS_BIND,
                        "re-hiding nested masked directory %s" % _nested,
                    )
                finally:
                    os.close(_nested_fd)
                _verify_masked_name(launch, _nested.encode(), _nested_id, _nested)
                launch.masked_names[_nested] = _nested_id
    # Every stage is retired HERE, in one place, once every window is bound and every
    # nested mask re-applied -- the earliest point where no stage is still needed, and
    # still long before the payload is exec'd. A stage left behind is a second path to
    # its window's real tree under a directory nothing masks, so a masked leaf INSIDE a
    # window, re-hidden at the window's own path just above, would stay readable through
    # it. One whose mask root never materialized has no window bound over it either and
    # is retired the same way.
    for _staged in list(launch.private_stage):
        _retire_stage_or_die(
            launch, launch.private_stage.pop(_staged), "private window %s" % _staged
        )


def apply_carveouts(launch):
    """Re-open each approved write carve-out read-write inside the sealed runtime parent.

    The planner validated every carve-out against every seal this launcher applies; each
    lives INSIDE the sealed runtime parent and covers no other protected path. MUST run
    AFTER :func:`seal_readonly`, for two reasons, the second stronger than the first: (a)
    the fresh bind inherits that mount's MS_RDONLY, which the remount then clears while
    re-asserting the kernel-locked nosuid/nodev/noexec bits (``_locked_mount_flags``
    never returns MS_RDONLY); (b) a non-recursive MS_BIND does not replicate submounts,
    so if the parent self-bind were established AFTER the carve-out, lookups through the
    new parent mount would not find the carve-out mount at all and the writable window
    would vanish silently. Clearing MS_RDONLY here is permitted because the seal being
    cleared was created inside THIS namespace without MNT_LOCK_READONLY; the underlying
    filesystem's nosuid/nodev/noexec bits remain locked and are re-asserted, not dropped.

    These two mounts WIDEN access, so unlike every ``_mount_or_die`` they fail OPEN: a
    host that refuses the bind or the remount keeps the seal, degrading to the sealed
    behavior (the probe's temp dir is unwritable) instead of killing the spawn. The
    ``islink`` refusal mirrors the sweep hygiene rules: a symlink planted where the
    carve-out dir should be must not redirect the writable window elsewhere.
    """
    for d in launch.writable_dirs:
        target = d.encode()
        if not os.path.isdir(target) or os.path.islink(target):
            continue
        if _mount_or_warn(launch, target, target, _MS_BIND, "writable carve-out bind for %s" % d):
            _mount_or_warn(
                launch,
                target,
                target,
                _MS_REMOUNT | _MS_BIND | _locked_mount_flags(target),
                "writable carve-out remount for %s" % d,
            )


def restore_exposed_files(launch):
    """Write each pre-read exposed file back, read-only, into its now-empty mask."""
    for src_path, filename in launch.expose_files:
        if src_path in launch.expose_data:
            parent = os.path.dirname(src_path)
            dest = os.path.join(parent, filename)
            with open(dest, "wb") as fh:
                fh.write(launch.expose_data[src_path])
            # A raw os.chmod: this program imports only stdlib and never kiro_crew, so
            # ``platform_compat.chmod_safe`` is not available here. The launcher never
            # runs on Windows, so there is no portability loss.
            os.chmod(dest, 0o444)


def verify_fail_closed_aliases(launch):
    """Refuse unless each discovered credential alias is still the file that was discovered.

    These paths were found moments ago as second names for a credential leaf, and the
    file masks skip an absent target -- right for every other entry, since an unused
    store is left absent rather than scaffolded, and wrong here, because absence means
    the name moved in between and the bytes answer to whatever it is called instead. The
    identity is checked too: an alias carries more than one name by construction, so a
    file that appeared at the same path is something else, and masking it would report
    the hole closed while the credential moved. Runs in a loop of its own, BEFORE the
    file masks, so those stay exactly what they are.
    """
    for f, _want_dev, _want_ino in launch.fail_closed_file_masks:
        try:
            _alias_st = os.lstat(f.encode())
        except OSError as exc:
            sys.exit(
                "sandbox: BLOCKED -- credential alias %s could not be read before "
                "masking it (%s). It was present moments ago as a second name for a "
                "credential leaf, so it moved or was removed and the bytes may answer "
                "to another name. Remove the extra link, then retry." % (f, exc)
            )
        if (_alias_st.st_dev, _alias_st.st_ino) != (_want_dev, _want_ino):
            sys.exit(
                "sandbox: BLOCKED -- credential alias %s is not the file that was "
                "discovered: it now names a different inode. The name was renamed and "
                "something else left in its place, so masking this path would cover the "
                "substitute while the credential stayed reachable under its new name. "
                "Remove the extra link, then retry." % (f,)
            )
        if not stat.S_ISREG(_alias_st.st_mode):
            sys.exit(
                "sandbox: BLOCKED -- credential alias %s is no longer a regular file "
                "(mode %o), so it cannot be masked with a file bind. Remove the extra "
                "link, then retry." % (f, _alias_st.st_mode)
            )


def _stage_is_fresh_mount(dfd, parent):
    """Whether the pinned stage now sits on a device other than its parent's."""
    return os.fstat(dfd).st_dev != os.stat(parent).st_dev


def _mount_private_tmpfs(launch, target):
    """Mount a small private tmpfs on *target*; 0 on success, else the errno.

    Degrades open by design, unlike ``_mount_or_die``: a failure here costs only the
    unreadable stand-in, and the caller falls back to the readable empty mask.
    """
    if (
        launch.libc.mount(
            b"tmpfs",
            target,
            b"tmpfs",
            _MS_NOSUID | _MS_NODEV | _MS_NOEXEC,
            b"mode=0700,size=16k",
        )
        != 0
    ):
        return ctypes.get_errno() or -1
    return 0


def _open_unreadable_stand_in(launch, what):
    """``(file_fd, stage_fd, stage)`` for a mode-0 stand-in, or None if unsupported.

    Mode 0 only holds while nobody can chmod the inode back, and the sandboxed uid owns
    it. So that stand-in is not created in the shared tmpfs, where any same-uid writer
    could chmod it, swap it or aim a symlink through its name. It is created mode 0
    (never chmodded) in a tmpfs mounted over a fresh stage directory in THIS mount
    namespace only: outside the namespace the stage is an empty host directory. The
    launcher has been non-dumpable since before unshare(CLONE_NEWNS), so no other
    same-uid process holds or can open a way into it through /proc/<pid>/root or
    /proc/<pid>/fd. The kernel only binds a file that still has a name, which rules out
    an O_TMPFILE inode.
    """
    if not launch.nondumpable:
        return None
    _stage = _stand_in_mkdtemp(launch)
    # Read after the create: a retry moves the stand-in root, and the stage is compared
    # with the root it was actually made in.
    _parent = launch.tmpfs_src or tempfile.gettempdir()
    _pin = os.open(_stage, os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        _err = _mount_private_tmpfs(launch, ("/proc/self/fd/%d" % _pin).encode())
    finally:
        os.close(_pin)
    if _err:
        try:
            os.rmdir(_stage)
        except OSError:
            pass
        sys.stderr.write(
            "sandbox: WARNING -- could not mount a private tmpfs for the "
            "unreadable mask over %s (errno %d); that mask reads as empty "
            "instead.\n" % (what, _err)
        )
        return None
    _sfd = os.open(_stage, os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY)
    if not _stage_is_fresh_mount(_sfd, _parent):
        sys.exit(
            "sandbox: BLOCKED -- the private stage for the unreadable mask "
            "over %s was replaced before it could be used. Lower "
            "agent.sandbox to run without this control deliberately." % what
        )
    _ffd = os.open(
        "stand-in",
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        0,
        dir_fd=_sfd,
    )
    return _ffd, _sfd, _stage


def _retire_unreadable_stage(launch, sealed, what):
    """Detach the stage tmpfs by its pinned root, then drop the descriptors."""
    _ffd, _sfd, _stage = sealed
    os.close(_ffd)
    if launch.libc.umount2(("/proc/self/fd/%d" % _sfd).encode(), _MNT_DETACH) != 0:
        _err = ctypes.get_errno()
        sys.exit(
            "sandbox: BLOCKED -- could not retire the private stage for the "
            "unreadable mask over %s: errno %d (%s). It is a second, writable "
            "path to the mask, so the agent could make it readable. Lower "
            "agent.sandbox to run without this control deliberately."
            % (what, _err, os.strerror(_err))
        )
    os.close(_sfd)
    try:
        os.rmdir(_stage)
    except OSError:
        pass


def mask_sensitive_files(launch):
    """Bind an empty file over each sensitive file; unreadable leaves get a mode-0 one.

    The empty source comes from the tmpfs (cross-fs) when available so the bind cannot
    corrupt the target's host directory entry on namespace exit. A leaf in
    ``unreadable_masks`` gets a mode-0 stand-in, so a copy made inside the sandbox fails
    on it rather than carrying zero bytes out as its content: after the bind, a
    read-only remount makes chmod through the masked name fail with EROFS, and the stage
    tmpfs is detached, so the read-only bind is the only way left to the inode. Restores
    dumpability once every file mask is in place.
    """
    for f in launch.sensitive_files:
        _file_fd, _file_target = _pin_mount_path(
            launch, f.encode(), stat.S_ISREG, require_present=_mask_required(launch, f)
        )
        if _file_target is None:
            continue
        _sealed = None
        try:
            if os.path.basename(f) in launch.unreadable_masks:
                _sealed = _open_unreadable_stand_in(launch, f)
            if _sealed is not None:
                _empty_st = os.fstat(_sealed[0])
                _empty_src = ("/proc/self/fd/%d" % _sealed[0]).encode()
            else:
                fd, empty_path = _stand_in_mkstemp(launch)
                # ``mkstemp`` hands back the descriptor of the file it created, which is
                # the stand-in's identity pinned already; no second resolution.
                _empty_st = os.fstat(fd)
                _empty_src = empty_path.encode()
                os.close(fd)
            _empty_id = (_empty_st.st_dev, _empty_st.st_ino)
            _register_stand_in(launch, _empty_id, _file_fd)
            _mount_or_die(
                launch, _empty_src, _file_target, _MS_BIND, "hiding sensitive file %s" % f
            )
            if _sealed is not None:
                # The remount names the NAME, not the pinned descriptor: that descriptor
                # still refers to the mount underneath the bind. _verify_masked_name runs
                # after it, so a name swapped before the remount still refuses the spawn.
                _mount_or_die(
                    launch,
                    f.encode(),
                    f.encode(),
                    _MS_REMOUNT | _MS_BIND | _MS_RDONLY | _locked_mount_flags(f.encode()),
                    "sealing unreadable mask %s" % f,
                )
        finally:
            os.close(_file_fd)
        _verify_masked_name(launch, f.encode(), _empty_id, f)
        if _sealed is not None:
            _retire_unreadable_stage(launch, _sealed, f)
    if launch.nondumpable:
        launch.libc.prctl(_PR_SET_DUMPABLE, 1, 0, 0, 0)


def mask_ssh_keys(launch):
    """Strict tier: hide ``~/.ssh`` but keep the known_hosts content.

    The guard follows, deliberately: a name that has been a link since before the
    gateway started is an ordinary stow or chezmoi layout and must keep working. What
    tells that apart from a link SUBSTITUTED while this runs is the identity the gateway
    recorded for this name before the script was even written, which the pin looks up
    for itself.

    ``lexists``, not ``isdir``: ``isdir`` FOLLOWS a symlink and re-resolves the name, so a
    ``~/.ssh`` that is a symlink would make this skip silently and exec the child with
    the keys readable. ``lexists`` enters for anything occupying the name -- link or
    directory -- and the pin then resolves it once, no-follow, checks the carried
    identity, and REFUSES a substitution rather than letting it slip past. A genuinely
    absent ``~/.ssh`` (no keys to hide) is the one case ``lexists`` still skips, which is
    safe ONLY when no pass saw it: a name the strict tier recorded an occupant for and
    that is empty now was moved after the gateway looked, so this is entered for a
    carried identity too and the pin refuses the absence.
    """
    if not (
        launch.hide_ssh
        and (
            os.path.lexists(launch.ssh_dir)
            or _carried_occupant(launch, launch.ssh_dir.encode()) is not None
        )
    ):
        return
    kh_data = b""
    if os.path.isfile(launch.ssh_known_hosts):
        # Host trust data FAILS CLOSED. This is deliberately NOT the degrade-open
        # treatment the exposed-file pre-read gets, and the two sites are NOT symmetric:
        #
        #   - an unreadable ~/.aws/config costs REACHABILITY, so skipping it trades a
        #     convenience for a working sandbox;
        #   - an unreadable known_hosts costs VERIFICATION. The launcher puts
        #     StrictHostKeyChecking=accept-new into GIT_SSH_COMMAND, gated ONLY on that
        #     variable being unset -- never on whether this read succeeded. So
        #     continuing with an empty kh_data points UserKnownHostsFile at an absent
        #     file while auto-accept is still on: every host then reads as NEW and an
        #     interceptor's key is accepted. With known_hosts present, accept-new REFUSES
        #     a CHANGED key.
        #
        # A degrade here would therefore convert "refuse a changed key" into "accept
        # anything". Aborting is the safe direction: no sandbox at all beats one that has
        # quietly stopped verifying hosts. Report first so the abort is diagnosable, then
        # re-raise and let it kill setup.
        try:
            with open(launch.ssh_known_hosts, "rb") as fh:
                kh_data = fh.read()
        except OSError as exc:
            print(
                "sandbox: FATAL — cannot read %s (%s). Refusing to "
                "continue: proceeding without it would leave host-key "
                "verification accepting any new key." % (launch.ssh_known_hosts, exc),
                file=sys.stderr,
            )
            raise
    # Cross-fs source for the same kernel-race reason as the directory and file masks.
    ssh_tmp = _stand_in_mkdtemp(launch).encode()
    _ssh_tmp_id = _stand_in_identity(ssh_tmp)
    # Host trust is restored INTO the stand-in, before that stand-in is bound over the
    # key directory. Writing it afterwards would address the restored file through
    # ``ssh_dir`` again -- a third resolution of a name this launcher has already
    # pinned -- so a name swapped after the pin would take the copied trust data outside
    # the mask while the masked directory stays empty, dropping every known host.
    if kh_data:
        with open(os.path.join(ssh_tmp.decode(), "known_hosts"), "wb") as fh:
            fh.write(kh_data)
    # Not ``require_present``: entry is gated on ``lexists`` (which does NOT follow a
    # link), so a symlinked or substituted ``~/.ssh`` REACHES this pin instead of being
    # skipped. A name that holds an object of the WRONG KIND -- a dangling link, or a
    # link that points at a plain file -- is an ordinary host shape that holds no key
    # directory, and refusing it would fail every strict spawn on that host for nothing
    # the mask could cover. That case skips, and says so on stderr.
    _ssh_fd, _ssh_target = _pin_mount_path(launch, launch.ssh_dir.encode(), stat.S_ISDIR)
    if _ssh_target is None:
        # Which miss this is decides between the two. The guard saw an occupant at the
        # name; if the name is EMPTY now, it moved between the guard and the pin, which
        # is the race this tier cannot skip past. If the name is still occupied, the pin
        # declined it for its kind, and there is no key directory here to hide.
        if not os.path.lexists(launch.ssh_dir):
            sys.exit(
                "sandbox: BLOCKED -- cannot pin %s to mask it: it is absent, "
                "though it was present a moment ago. Another process moved that "
                "name, so masking whatever replaces it would leave the keys "
                "readable at the name they moved to. Lower agent.sandbox to "
                "run without this control deliberately." % launch.ssh_dir
            )
        sys.stderr.write(
            "sandbox: WARNING -- %s is not a directory (a dangling link, or a "
            "link to a file); nothing to hide there, continuing without the ssh "
            "key mask\n" % launch.ssh_dir
        )
        return
    _register_stand_in(launch, _ssh_tmp_id, _ssh_fd)
    try:
        _mount_or_die(
            launch, ssh_tmp, _ssh_target, _MS_BIND, "hiding ssh key directory %s" % launch.ssh_dir
        )
    finally:
        os.close(_ssh_fd)
    _verify_masked_name(launch, launch.ssh_dir.encode(), _ssh_tmp_id, launch.ssh_dir)
    launch.masked_names[launch.ssh_dir.rstrip("/")] = _ssh_tmp_id


def confirm_crew_home_aliases(launch):
    """Refuse unless every crew-home alias still names the data home now that the masks are placed.

    The pre-mount check ran BEFORE the hiding mounts, and a writer who re-aims the home
    link between that read and the mounts leaves the alias reaching the original data
    home while every folded rule landed on the canonical spelling. Every hiding mount is
    placed by now, so each alias is resolved once more and must still name the canonical
    path: the masks sit on that path's entries, and an alias whose components lead there
    reads them exactly as the canonical does. Resolution again, not identity -- a second
    mount of the data home would match by ``(st_dev, st_ino)`` while carrying none of the
    masks. The canonical is resolved too: it is the passes' spelling, not a resolved one.
    """
    for _alias, _canonical, _alias_dev, _alias_ino in launch.crew_home_aliases:
        try:
            _canonical_resolved = os.path.realpath(_canonical, strict=True)
            _alias_resolved = os.path.realpath(_alias, strict=True)
        except OSError as _alias_exc:
            sys.exit(
                "sandbox: BLOCKED -- %s reached the data home %s when this spawn was "
                "prepared and cannot be read back after masking (%s), so the folded "
                "rules cannot be confirmed to cover what the alias reaches. Lower "
                "agent.sandbox to run without this control deliberately."
                % (_alias, _canonical, _alias_exc)
            )
        if _alias_resolved != _canonical_resolved:
            sys.exit(
                "sandbox: BLOCKED -- %s reached the data home %s when this spawn was "
                "prepared and resolves to %s now that the masks are placed, so the "
                "rules folded onto the data home leave what the alias reaches "
                "unmasked. Another process re-aimed that name while this sandbox was "
                "being built. Lower agent.sandbox to run without this control "
                "deliberately." % (_alias, _canonical, _alias_resolved)
            )
