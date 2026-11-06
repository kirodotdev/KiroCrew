"""The stages that place the namespace launcher's windows, seals and masks.

One of the launcher program's parts (see :mod:`kiro_crew.sandbox_launcher_parts`). Below its
marker line this file is the program's text from ``check_crew_home_aliases`` to
``confirm_crew_home_aliases``: the crew-home alias checks on both sides of the masks, the
pre-read of the files the masks re-expose, the pin of the seals' parents ``run_child`` places
ahead of the stand-in stage, every stage ``place_masks`` runs -- staging the private windows,
applying the operator's read-only layer, pinning the seals' parents inside it, sealing the
read-only paths, masking the sensitive directories, re-opening the write carve-outs,
restoring the exposed files, checking the credential aliases, masking the sensitive files and
``~/.ssh`` -- confirming every sealed root, and the mount report a test can ask for. A new
launcher stage goes here, and ``place_masks`` or ``run_child`` in
:mod:`kiro_crew.sandbox_launcher_program` names its place in the order.
"""

import ctypes
import os
import stat
import sys
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.sandbox_launcher_program import (
        _FDINFO,
        _MNT_DETACH,
        _MOUNTINFO,
        _MS_BIND,
        _MS_NODEV,
        _MS_NOEXEC,
        _MS_NOSUID,
        _MS_RDONLY,
        _MS_REC,
        _MS_REMOUNT,
        _O_PATH,
        _PR_SET_DUMPABLE,
        _any_kind,
        _carried_occupant,
        _lexical,
        _locked_mount_flags,
        _mask_required,
        _mount_or_die,
        _mount_or_warn,
        _pin_mount_path,
        _pin_stand_in,
        _register_stand_in,
        _retire_stage_or_die,
        _stage_mkdtemp,
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
            try:
                _staged_st = os.fstat(_win_fd)
            except OSError as exc:
                sys.exit(
                    "sandbox: BLOCKED -- cannot read the identity of the private window %s "
                    "this spawn just staged (%s), so the last check could not confirm it. "
                    "Lower agent.sandbox to run without this control deliberately." % (p, exc)
                )
            launch.staged_window_ids[p.rstrip("/")] = (_staged_st.st_dev, _staged_st.st_ino)
        finally:
            os.close(_win_fd)


def _hold_seal_parent(launch, target):
    """Hold *target* for :func:`pin_seal_parents`: a link as the link itself, else a directory.

    A link on a seal's path frees the sealed name when it is moved, as a directory does,
    so it is held through a no-follow ``O_PATH`` descriptor and its bind lands on the
    link itself.
    """
    try:
        fd = os.open(target, os.O_PATH | os.O_NOFOLLOW)
    except OSError:
        pass
    else:
        if stat.S_ISLNK(os.fstat(fd).st_mode):
            return fd, ("/proc/self/fd/%d" % fd).encode()
        os.close(fd)
    return _pin_mount_path(launch, target, stat.S_ISDIR)


def _pinned_inside_layer(launch, d):
    """Whether the pin of the seal parent *d* lands at or below an operator read-only directory.

    A pin lands on the name's last component in the directory its other components resolve
    to, which is where :func:`_hold_seal_parent` holds a link too; the layer's directories
    are resolved spellings. Read before any mount is placed, once per launch
    (:func:`pin_seal_parents`).
    """
    _at = os.path.join(os.path.realpath(os.path.dirname(d)), os.path.basename(d))
    return any(
        _at == o.rstrip("/") or _at.startswith(o.rstrip("/") + "/")
        for o in launch.operator_readonly_dirs
    )


def pin_seal_parents(launch, inside_layer=False):
    """Bind each parent of a read-only seal over itself, so it cannot be renamed.

    A seal is a bind on the sealed path, and the kernel refuses to rename a directory
    that IS a mountpoint, not one that only contains one: a renamed parent would carry
    the seal away and free the sealed name. The bind adds no flag, so the parent's
    other entries stay as writable as they were, and it is recursive, so the mounts
    already below it stay in view. Shallowest first, so each pin lands on the one above it; before
    :func:`seal_readonly`, so every seal lands on its pin. An absent name is skipped; a
    name that reaches another object after the bind refuses the spawn.

    The first call runs before the stand-in stage and every other mount this launcher places
    (:func:`run_child`). A recursive bind copies the mounts below it, and a path then
    reaches the copy, while a descriptor opened before the bind still names the original.
    The private stand-in stage, the window stages and the cover on a sealed stand-in's
    entry are all reached by descriptor, so a pin above them placed later would leave a
    writable copy of the stage at its host path that neither the cover nor
    :func:`retire_stand_in_stage` touches.

    A parent whose pin lands inside the operator's read-only layer is the exception: it is
    pinned by the call *inside_layer* makes, on top of that layer (:func:`place_masks`), like
    every other crew mount there. Pinned under it, the layer's recursive bind would copy the
    pin and remount the copy read-only, and a write exception re-opens its own mount only, so
    a parent inside an exception would stay read-only, and a pinned link there is a mount the
    layer cannot reach, which leaves the whole exception read-only. On top of the layer it
    copies the access of the mount it lands on, and every mount below it, a copy of the stage
    included, is read-only already. Which parents those are is read once, before any mount.
    """
    if launch.layer_pins is None:
        launch.layer_pins = frozenset(
            d for d in launch.parent_pins if _pinned_inside_layer(launch, d)
        )
    chosen = [d for d in launch.parent_pins if (d in launch.layer_pins) == inside_layer]
    for d in sorted(chosen, key=lambda p: (p.count("/"), p)):
        target = d.encode()
        _pin_fd, _pin_target = _hold_seal_parent(launch, target)
        if _pin_target is None:
            continue
        try:
            _pin_id = os.fstat(_pin_fd)
            _mount_or_die(
                launch, _pin_target, _pin_target, _MS_BIND | _MS_REC, "pinning seal parent %s" % d
            )
        finally:
            os.close(_pin_fd)
        _post_fd, _post_target = _hold_seal_parent(launch, target)
        _same = False
        if _post_target is not None:
            try:
                _post_id = os.fstat(_post_fd)
                _same = (_post_id.st_dev, _post_id.st_ino) == (_pin_id.st_dev, _pin_id.st_ino)
            finally:
                os.close(_post_fd)
        if not _same:
            sys.exit(
                "sandbox: BLOCKED -- %s changed while it was being pinned, so a parent of a "
                "read-only seal could not be held in place." % d
            )


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
        _seal_path(launch, d, _MS_BIND, _mask_required(launch, target))


def _seal_path(launch, d, bind_flags, require_present):
    """Bind *d* over itself with *bind_flags*, remount it ``MS_RDONLY``, or refuse.

    The one seal :func:`seal_readonly` and :func:`seal_operator_readonly` share. Returns
    ``False`` when *d* is absent and nothing required it, ``True`` once it is sealed.
    """
    target = d.encode()
    # Any KIND passes: a governance ceiling is a plain file (``security_policy.json``),
    # and bind-over-self + MS_RDONLY seals a regular file exactly as it seals a
    # directory.
    _seal_fd, _seal_target = _pin_mount_path(
        launch, target, _any_kind, require_present=require_present
    )
    if _seal_target is None:
        return False
    try:
        _seal_id = os.fstat(_seal_fd)
        _mount_or_die(
            launch, _seal_target, _seal_target, bind_flags, "exposing read-only path %s" % d
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
            _same = _rdonly_id.st_dev == _seal_id.st_dev and _rdonly_id.st_ino == _seal_id.st_ino
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
    return True


def _unescape_mount_point(field):
    """A ``mountinfo`` mount point with its octal escapes (``\\040`` ...) decoded."""
    out = bytearray()
    i = 0
    while i < len(field):
        if field[i : i + 1] == b"\\" and field[i + 1 : i + 4].isdigit():
            out.append(int(field[i + 1 : i + 4], 8))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return bytes(out)


def _mount_points_below(root, mountinfo):
    """Every mount point strictly inside *root*, from *mountinfo*'s text, once each.

    Both are bytes. Field 5 of each ``mountinfo`` line is the mount point as this
    namespace sees it; a mount stacked on another at one point is listed once, because a
    remount by that name reaches the top one, which is the one lookups see.
    """
    prefix = root.rstrip(b"/") + b"/"
    points = []
    for line in mountinfo.splitlines():
        fields = line.split(b" ")
        if len(fields) < 5:
            continue
        point = _unescape_mount_point(fields[4])
        if point.startswith(prefix) and point not in points:
            points.append(point)
    return points


def _mount_ids_at(points, mountinfo):
    """The mount IDs *mountinfo* lists at each of *points*, as ``{point: {id, ...}}``.

    Field 1 of each line is the mount's ID. Several mounts can be listed at one point:
    stacked there, or one hidden there by a later mount over a directory above it.
    """
    ids: dict[bytes, set[bytes]] = {point: set() for point in points}
    for line in mountinfo.splitlines():
        fields = line.split(b" ")
        if len(fields) < 5:
            continue
        point = _unescape_mount_point(fields[4])
        if point in ids:
            ids[point].add(fields[0])
    return ids


def _mount_id_of(fd):
    """The ID of the mount *fd* sits on, from its ``fdinfo``; ``None`` when unreadable."""
    try:
        with open(_FDINFO % fd, "rb") as _fdinfo:
            for line in _fdinfo.read().splitlines():
                if line.startswith(b"mnt_id:"):
                    return line.split(b":", 1)[1].strip()
    except OSError:
        pass
    return None


def seal_operator_readonly(launch):
    """Apply the operator's read-only directories and the write exceptions inside them.

    ``agent.sandbox_readonly_paths`` / ``agent.sandbox_readonly_exceptions``, applied
    BEFORE every crew seal, mask and carve-out, so each crew rule is a mount placed on
    top of this layer and wins wherever the two meet. Nothing runs when the operator set
    neither key.

    Each read-only directory is bound over itself RECURSIVELY, so a filesystem mounted
    below it (``/mnt/c`` under ``/mnt``) stays visible, and then every mount of that
    copy is remounted ``MS_RDONLY``: the directory's own through :func:`_seal_path`, the
    ones below it by the names this namespace lists for them. A non-recursive bind would
    hide those filesystems instead, and the kernel refuses one outright over a mount a
    user namespace has locked. Every one of these mounts withholds access, so each
    fails CLOSED like a crew seal; an entry that is absent now is skipped, as the plan
    already logged any entry it could not use.

    Each exception is then re-opened the way :func:`apply_carveouts` re-opens a crew
    carve-out -- a fresh bind remounted read-write, failing OPEN so a refused mount
    leaves the path read-only -- except that the bind is recursive too, and a
    filesystem mounted below an exception keeps the read-only remount it just got.
    A mount that cannot be reached to seal, or whose record is hidden by a later mount
    over a directory above it, is skipped; any exception containing such
    a mount stays read-only with a warning, since re-opening it could restore access
    to that mount while it is writable.
    """
    _skipped_points = []
    for d in launch.operator_readonly_dirs:
        if not _seal_path(launch, d, _MS_BIND | _MS_REC, False):
            continue
        with open(_MOUNTINFO, "rb") as _info:
            _mountinfo = _info.read()
        _points = _mount_points_below(d.encode(), _mountinfo)
        _ids = _mount_ids_at(_points, _mountinfo)
        for _point in _points:
            # A mount this namespace cannot reach is skipped. Outside write exceptions,
            # its read-only ancestor prevents restoring traversal; containing exceptions
            # must stay read-only so they cannot expose an unsealed writable mount.
            # A reachable mount that will not go read-only refuses the spawn below.
            try:
                os.stat(_point)
            except OSError:
                _skipped_points.append(_point)
                continue
            _sub_fd, _sub_target = _pin_mount_path(launch, _point, _any_kind)
            if _sub_target is None:
                continue
            try:
                # A record whose name now reaches none of the mounts listed at it is
                # hidden by a later mount over a directory above it: no lookup reaches
                # it, so it is skipped as unreachable rather than remounted by a name
                # that is no mount point. An unreadable ID is judged as reached.
                _reached = _mount_id_of(_sub_fd)
                if _reached is not None and _reached not in _ids[_point]:
                    _skipped_points.append(_point)
                    continue
                _mount_or_die(
                    launch,
                    _sub_target,
                    _sub_target,
                    _MS_REMOUNT | _MS_BIND | _MS_RDONLY | _locked_mount_flags(_sub_target),
                    "sealing read-only mount %s below %s" % (os.fsdecode(_point), d),
                )
            finally:
                os.close(_sub_fd)
    for d in launch.operator_writable_dirs:
        target = d.encode()
        _skipped_point = next(
            (
                _point
                for _point in _skipped_points
                if _point == target or _point.startswith(target.rstrip(b"/") + b"/")
            ),
            None,
        )
        if _skipped_point is not None:
            sys.stderr.write(
                "sandbox: WARNING -- read-only exception %s left read-only: "
                "a mount below it (%s) could not be reached to seal\n"
                % (d, os.fsdecode(_skipped_point))
            )
            continue
        if not os.path.isdir(target) or os.path.islink(target):
            continue
        if _mount_or_warn(
            launch, target, target, _MS_BIND | _MS_REC, "read-only exception bind for %s" % d
        ):
            _mount_or_warn(
                launch,
                target,
                target,
                _MS_REMOUNT | _MS_BIND | _locked_mount_flags(target),
                "read-only exception remount for %s" % d,
            )


def _seal_masked_dir_if_windowless(launch, name, has_window):
    """Remount a masked directory's stand-in read-only when the plan seals it.

    The name covers one of the two sets: a mask the caller opted in is sealed only when it
    carries no window, and a mask in ``seal_windowed_masks`` is sealed with or without one.

    The hide this follows is a bind of a fresh empty directory, and on Linux that bind
    is a WRITABLE empty tmpfs: with no read-only remount a write into the masked path
    lands in that disposable backing and is lost when the namespace is torn down, so the
    job reports success while nothing was persisted. For a masked directory the caller
    explicitly OPTED IN to sealing (``seal_windowless_masks``) and that has no writable
    window, there is no legitimate in-sandbox writer, so the stand-in is sealed read-only
    and the write fails loudly with EROFS instead -- the fail-safe side of a data-loss
    choice.

    SCOPE is two sets, and nothing outside them is sealed. ``seal_windowless_masks`` holds
    the masks whose creator asked (``SandboxRequest.seal_hidden_dirs`` ->
    ``Mask.seal_readonly``); it is NOT inferred from origin, exposure or path.
    ``seal_windowed_masks`` holds the masks the planner seals by path, around their windows
    (``PlanHost.sealed_mask_dirs`` -> ``Mask.seal_around_windows``): the scratch root alone.
    So a tier host-credential directory (``.aws``, ``.docker``, ``.config/gcloud`` ...), a
    mask that re-exposes a file read-only through ``restore_exposed_files`` (``.aws`` in cc
    mode), and every other mask in neither set keep their writable stand-in -- which is
    what the launcher's own ``restore_exposed_files`` and the vendor CLIs depend on.

    A masked directory that DOES carry a window keeps its stand-in writable even when it
    opted in: the window is the explicit write surface, and the launcher binds it on top
    of this stand-in. ``has_window`` is the caller's answer to whether any private window
    sits at or below *name*.

    One set overrides that: a directory in ``seal_windowed_masks`` (the scratch root) is
    sealed with or without windows. Its windows are the only places a sandboxed process
    may write under it, and any other name there reaches this stand-in, where a write
    would succeed and be lost. The caller runs this after every window is bound, and each
    window is a mount of its own, so the remount changes the stand-in's mount alone and
    the windows stay read-write.

    The remount names the NAME, the same way :func:`mask_sensitive_files` seals its
    unreadable file masks, and re-asserts the kernel-locked nosuid/nodev/noexec bits via
    :func:`_locked_mount_flags`. ``_verify_masked_name`` has already confirmed the name
    reaches this launcher's own stand-in before this runs, so the remount cannot land on
    a racing writer's substitute.
    """
    _name = os.fsdecode(name)
    if _name not in launch.seal_windowed_masks and (
        has_window or _name not in launch.seal_windowless_masks
    ):
        return
    _mount_or_die(
        launch,
        name,
        name,
        _MS_REMOUNT | _MS_BIND | _MS_RDONLY | _locked_mount_flags(name),
        "sealing masked directory %s read-only" % os.fsdecode(name),
    )


def _mask_spelling(launch, path):
    """``(written, resolved)``: the two spellings of a mask path the rule compares.

    The written spelling is normalized (:func:`_lexical`); the resolved one is
    ``os.path.realpath``, which follows every link the kernel follows when it mounts over
    a name (a symlinked ``~/.aws`` or ``~/.ssh``, a data home reached through a link, a
    symlinked ``$HOME``). Each is read once and kept, and :func:`mask_sensitive` reads every
    mask and ``~/.ssh`` before its first mount, so no stand-in is in either.
    """
    spelled = launch.mask_spellings.get(path)
    if spelled is None:
        spelled = launch.mask_spellings[path] = (_lexical(path), os.path.realpath(path))
    return spelled


def _held_below(launch, container, path):
    """The part of *path* below the mask *container*, by the one mask spelling rule, else ``None``.

    *path* is inside *container* when its written spelling is below the container's written
    one, or its resolved spelling below the container's resolved one. The part below comes
    from the pair that matched, the written pair first: the name of *path* passes through
    the container's name there, so once the container is mounted the kernel looks that part
    up inside its stand-in; for the resolved pair it looks up the rest below the object the
    container's name reaches. A mount point made from the wrong part would leave the name
    unreached, which :func:`_reattach_sealed_masks` refuses.
    """
    for outer, inner in zip(_mask_spelling(launch, container), _mask_spelling(launch, path)):
        outer = outer.rstrip("/")
        if inner.startswith(outer + "/"):
            return inner[len(outer) + 1 :]
    return None


def _mask_order(launch):
    """``sensitive_dirs`` in placing order: each root sealed around its windows ahead of its containers.

    A mask that contains a sealed root (``KIROCREW_HOME`` under ``~/.aws`` puts the scratch
    root inside the ``.aws`` mask) binds a fresh writable stand-in over its whole tree.
    Placed before the root, it leaves the root's name reaching a directory of that
    stand-in, which is not the object the plan recorded there, so the pin refuses the
    spawn. So a sealed root listed after one of its containers moves to just before the
    first of them, and is mounted back on top of each container once that is placed
    (:func:`_reattach_sealed_masks`). Every other mask keeps its place.
    """
    order = list(launch.sensitive_dirs)
    for sealed in sorted(launch.seal_windowed_masks):
        if sealed not in order:
            continue
        at = order.index(sealed)
        first = next(
            (i for i, d in enumerate(order) if _held_below(launch, d, sealed) is not None), at
        )
        if first < at:
            order.insert(first, order.pop(at))
    return order


def _reach_stand_in(path, stand_in_id):
    """A descriptor on what *path* reaches now, when that is the stand-in *stand_in_id*, else ``None``.

    The one place a sealed root's own name, or the name of its stand-in, is opened
    following links, as the child will reach it. The descriptor is compared with the
    stand-in before anything else sees it, and every caller treats ``None`` as its own
    refusal or as no path at that name. A name that opens but whose identity cannot be
    read refuses here, its descriptor closed, since ``None`` would let a caller pass it.
    The protected-name ratchet holds this shape.
    """
    try:
        fd = os.open(path, _O_PATH | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        sys.exit(
            "sandbox: BLOCKED -- cannot read what %s reaches (%s), so it could not be "
            "checked against its sealed stand-in. Lower agent.sandbox to run without this "
            "control deliberately." % (path, exc)
        )
    if (st.st_dev, st.st_ino) != tuple(stand_in_id):
        os.close(fd)
        return None
    return fd


def _record_sealed_source(launch, name, stand_in, stand_in_id):
    """Record the host path of the sealed root *name*'s stand-in, and pin the stand-in.

    The stand-in's own entry is a second path to the root, and the seal is a flag on the
    bind at *name* only. On the private stand-in stage (:func:`stage_stand_ins_privately`)
    the host path is the entry under the stage's directory on the host root, which stays
    mounted when :func:`retire_stand_in_stage` cannot detach the stage; when the stage was
    refused the stand-in sits on a host-visible root itself. Either way a no-follow
    descriptor on the entry comes back, compared with the identity just taken, for
    :func:`_close_stand_in_source`.
    """
    stand_in = os.fsdecode(stand_in)
    source = os.path.abspath(stand_in)
    if launch.stand_in_stage is not None:
        _stage_fd, _stage, _host_root = launch.stand_in_stage
        _on_stage = "/proc/self/fd/%d/" % _stage_fd
        if stand_in.startswith(_on_stage):
            source = os.path.join(_stage, stand_in[len(_on_stage) :])
    launch.stand_in_sources[name] = (source, "open")
    fd, st = _pin_stand_in(stand_in.encode())
    if (st.st_dev, st.st_ino) != tuple(stand_in_id):
        os.close(fd)
        sys.exit(
            "sandbox: BLOCKED -- the stand-in %s for the sealed root %s was replaced as it "
            "was made, so its writable entry could not be closed. Lower agent.sandbox to "
            "run without this control deliberately." % (source, name)
        )
    return fd


def _close_stand_in_source(launch, name, source, source_fd):
    """Cover the stand-in's own entry at *source* with a read-only view of the root just sealed.

    A stand-in keeps its own entry once it is bound over *name*, and the seal is a flag on
    the bind at *name* only. The entry sits on the private stand-in stage, which stays
    mounted when it cannot be detached, or, when the stage was refused, on a host-visible
    root (``/run/user/<uid>``, ``/dev/shm`` or the system temp directory). So the sealed
    root's own mount, read-only, is bound over the entry *source_fd* holds, wherever
    another process has renamed it since it was made; a bind keeps the read-only flag of
    the mount it copies. A write there then fails with EROFS as it does under *name*. On a
    stage that detaches the cover goes with it. :func:`confirm_sealed_roots` checks *source*
    again at the end.
    """
    root_fd = _reach_stand_in(name, launch.masked_names[name])
    if root_fd is None:
        sys.exit(
            "sandbox: BLOCKED -- %s does not reach its sealed stand-in, so another process "
            "renamed that name and the stand-in's writable entry at %s could not be closed. "
            "Lower agent.sandbox to run without this control deliberately." % (name, source)
        )
    try:
        _mount_or_die(
            launch,
            ("/proc/self/fd/%d" % root_fd).encode(),
            ("/proc/self/fd/%d" % source_fd).encode(),
            _MS_BIND,
            "closing the writable entry %s of the sealed stand-in for %s" % (source, name),
        )
    finally:
        os.close(root_fd)
    launch.stand_in_sources[name] = (source, "closed")


def _hold_sealed_mount(launch, name):
    """Hold the sealed mask just placed at *name* by a descriptor, when a later mask contains it.

    The descriptor names the stand-in's mount with every window and re-hide mounted inside
    it, which is what :func:`_reattach_sealed_masks` binds back, recursively, over a
    containing mask placed later: the directory mask of a container in ``sensitive_dirs``,
    or ``~/.ssh`` on the strict tier, which :func:`mask_ssh_keys` places last. It must
    reach the stand-in this mask just bound, or the spawn refuses.
    """
    containers = [c for c in launch.sensitive_dirs if _held_below(launch, c, name) is not None]
    if launch.hide_ssh and _held_below(launch, launch.ssh_dir, name) is not None:
        containers.append(launch.ssh_dir)
    if not containers:
        return
    fd = _reach_stand_in(name, launch.masked_names[name])
    if fd is None:
        sys.exit(
            "sandbox: BLOCKED -- %s does not reach its sealed mask any more, so another "
            "process renamed that name and a mask that contains it would cover the seal. "
            "Lower agent.sandbox to run without this control deliberately." % name
        )
    launch.sealed_mounts[name] = fd


def _sealed_mounts_under(launch, container):
    """``[(name, part below)]`` of the held sealed masks inside the mask *container*, shallowest first."""
    held = [(n, _held_below(launch, container, n)) for n in launch.sealed_mounts]
    return sorted(((n, below) for n, below in held if below is not None), key=lambda h: len(h[1]))


def _make_sealed_mount_points(launch, container, stand_in):
    """Create, in *container*'s fresh stand-in, the directory each held sealed mask goes on.

    Made before the stand-in is mounted, as the window mount points are, so the sealed mask
    has a name to land on also when no window under it gave the stand-in that directory.
    """
    for _name, below in _sealed_mounts_under(launch, container):
        os.makedirs(os.path.join(stand_in, below), exist_ok=True)


def _reattach_sealed_masks(launch, container):
    """Mount every held sealed mask inside *container* back on top, now that its mask is placed.

    The container's stand-in covers the sealed root, so without this the root would be a
    writable directory of that stand-in and a stale write under it would be lost at
    teardown. The bind is recursive, so the root's windows and the masks re-applied inside
    them come back with it; the name must then reach the sealed stand-in, and the seal is
    asserted again on the new mount.
    """
    for name, _below in _sealed_mounts_under(launch, container):
        _mount_or_die(
            launch,
            ("/proc/self/fd/%d" % launch.sealed_mounts[name]).encode(),
            name.encode(),
            _MS_BIND | _MS_REC,
            "re-attaching sealed mask %s over the mask on %s" % (name, container),
        )
        _verify_masked_name(launch, name.encode(), launch.masked_names[name], name)
        _seal_masked_dir_if_windowless(launch, name.encode(), True)


def mask_sensitive(launch):
    """Bind an empty directory over each sensitive dir, re-open its windows, re-hide nested leaves.

    A per-dir stand-in, so no content leaks across mounts through a shared backing dir.
    Runs AFTER :func:`seal_readonly`: a hidden leaf nested under a sealed parent
    (``run/voice-runtime`` under ``run``) must be hidden on top of the parent's
    self-bind, never underneath it, or the non-recursive parent bind masks the hide.
    Every staging mount is retired at the end, once every window is bound and every
    nested mask re-applied.

    A root sealed around its windows stays on top of every mask that contains it, whatever
    the order of the list: it is placed ahead of its containers (:func:`_mask_order`), and
    each container's stand-in gets a directory for it and has it mounted back once placed
    (:func:`_reattach_sealed_masks`), windows included. So once every mask is placed, a
    path under the root outside its windows still fails writes with EROFS.
    """
    # Every spelling the containment rule compares, read before the first mount below.
    for _path in (*launch.sensitive_dirs, launch.ssh_dir):
        _mask_spelling(launch, _path)
    for d in _mask_order(launch):
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
        _all_windows = [p for p in launch.private_stage if p.startswith(d.rstrip("/") + "/")]
        # A sealed root placed earlier inside this mask comes back over it with its windows
        # (``_reattach_sealed_masks``), so those windows are not bound into this stand-in.
        # They are the windows that root's own pass bound, picked by the filter above, so
        # they are picked here by that filter too, not by the mask spelling rule.
        _reattach = [name for name, _below in _sealed_mounts_under(launch, d)]
        _root_windows = [
            p
            for p in launch.private_stage
            if any(p.startswith(r.rstrip("/") + "/") for r in _reattach)
        ]
        _windows = [p for p in _all_windows if p not in _root_windows]
        _source_fd = -1
        try:
            per_dir_empty = _stand_in_mkdtemp(launch).encode()
            _per_dir_id = _stand_in_identity(per_dir_empty)
            if d in launch.seal_windowed_masks:
                _source_fd = _record_sealed_source(
                    launch, d.rstrip("/"), per_dir_empty, _per_dir_id
                )
            _register_stand_in(launch, _per_dir_id, _mask_fd)
            for p in _windows:
                os.makedirs(os.path.join(per_dir_empty.decode(), os.path.relpath(p, d)))
            _make_sealed_mount_points(launch, d, per_dir_empty.decode())
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
        # write surface) unless the plan seals it around its windows (the scratch root);
        # that seal runs here, after every window above is bound, and leaves each window
        # read-write. The nested re-hides below land on top either way.
        _seal_masked_dir_if_windowless(launch, d.encode(), bool(_all_windows or _root_windows))
        if _source_fd >= 0:
            # The stand-in's own entry, on the private stage or on a host-visible root, is a
            # second, writable path to the root just sealed.
            try:
                _close_stand_in_source(
                    launch, d.rstrip("/"), launch.stand_in_sources[d.rstrip("/")][0], _source_fd
                )
            finally:
                os.close(_source_fd)
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
        if _reattach:
            _reattach_sealed_masks(launch, d)
        if d in launch.seal_windowed_masks:
            _hold_sealed_mount(launch, d.rstrip("/"))
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
    _stage = _stage_mkdtemp(launch)
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
    _make_sealed_mount_points(launch, launch.ssh_dir, ssh_tmp.decode())
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
    # A data home under ``~/.ssh`` puts the sealed scratch root, windows and all, under
    # this mask; it goes back on top.
    _reattach_sealed_masks(launch, launch.ssh_dir)


def confirm_sealed_roots(launch):
    """Refuse unless every sealed root is its read-only stand-in, with its windows, and no writable path reaches it.

    Runs after :func:`retire_stand_in_stage`, once nothing else mounts or detaches, so no
    later step can change what it confirms. Ordering, the hold and the re-attach keep each
    root sealed around its windows on top of every mask that contains it, and
    :func:`_close_stand_in_source` closes a stand-in's own entry on a host-visible root;
    this is the check that refuses when any of them was defeated, for instance by a link
    swapped and put back while the containment spellings were read.

    Each such root a mask was placed on is opened by its name, following links, as the
    child will reach it (:func:`_reach_stand_in`). It must be the stand-in this spawn
    mounted there, on a read-only mount, and each window bound under it must still be the
    directory staged for that window. Then the stand-in's own host path
    (``stand_in_sources``) must give no writable way in: its entry was covered, and the
    path, if it still reaches the stand-in (a stage that did not detach, or a host root),
    is read-only. Any mismatch ends the spawn: a write there would succeed and be lost with
    the namespace.
    """
    for _root in launch.seal_windowed_masks:
        _root = _root.rstrip("/")
        _want = launch.masked_names.get(_root)
        if _want is None:
            continue
        _fd = _reach_stand_in(_root, _want)
        if _fd is None:
            sys.exit(
                "sandbox: BLOCKED -- the sealed root %s is not the sealed stand-in this spawn "
                "mounted there now that every mask is placed, so a later mask or another "
                "process covered it and a write under it would be lost. Lower agent.sandbox "
                "to run without this control deliberately." % _root
            )
        try:
            _read_only = bool(os.fstatvfs(_fd).f_flag & os.ST_RDONLY)
        except OSError as exc:
            sys.exit(
                "sandbox: BLOCKED -- cannot read the mount flags of the sealed root %s (%s), "
                "so its seal could not be confirmed. Lower agent.sandbox to run without this "
                "control deliberately." % (_root, exc)
            )
        finally:
            os.close(_fd)
        if not _read_only:
            sys.exit(
                "sandbox: BLOCKED -- the sealed root %s is not on a read-only mount now that "
                "every mask is placed, so a write under it would succeed and be lost. Lower "
                "agent.sandbox to run without this control deliberately." % _root
            )
        for _window in sorted(launch.bound_windows):
            if not _window.startswith(_root + "/"):
                continue
            _window_st = None
            try:
                _window_st = os.stat(_window)
            except OSError as exc:
                _why = exc.strerror
            else:
                _why = "it is a different object"
            if (
                _window_st is None
                or (_window_st.st_dev, _window_st.st_ino) != launch.staged_window_ids[_window]
            ):
                sys.exit(
                    "sandbox: BLOCKED -- the window %s under the sealed root %s is not the "
                    "directory this spawn bound there (%s), so the agent's own writes there "
                    "would be lost. Lower agent.sandbox to run without this control "
                    "deliberately." % (_window, _root, _why)
                )
        _confirm_no_writable_source(launch, _root, _want)


def _confirm_no_writable_source(launch, root, stand_in_id):
    """Refuse when the stand-in of the sealed root *root* is still writable through its own host path."""
    _recorded = launch.stand_in_sources.get(root)
    if _recorded is None or _recorded[1] == "open":
        sys.exit(
            "sandbox: BLOCKED -- the stand-in of the sealed root %s was made at %s and that "
            "entry was not closed, so it is a writable second path to the root and a write "
            "there would be lost. Lower agent.sandbox to run without this control "
            "deliberately." % (root, _recorded[0] if _recorded else "an unrecorded path")
        )
    _source = _recorded[0]
    _fd = _reach_stand_in(_source, stand_in_id)
    if _fd is None:
        return
    try:
        _read_only = bool(os.fstatvfs(_fd).f_flag & os.ST_RDONLY)
    except OSError as exc:
        sys.exit(
            "sandbox: BLOCKED -- cannot read the mount flags of %s, where this spawn made the "
            "stand-in of the sealed root %s (%s), so it could not be confirmed read-only. "
            "Lower agent.sandbox to run without this control deliberately." % (_source, root, exc)
        )
    finally:
        os.close(_fd)
    if not _read_only:
        sys.exit(
            "sandbox: BLOCKED -- %s, where this spawn made the stand-in of the sealed root %s, "
            "still reaches it on a writable mount, so a write there would be lost. Lower "
            "agent.sandbox to run without this control deliberately." % (_source, root)
        )


def report_mount_view(launch):
    """Write to stderr what the sealed roots, their windows and the stand-ins' places reach.

    For the real-kernel tests: it runs only when the environment sets
    ``KIROCREW_SANDBOX_MOUNT_REPORT=1``, and reads and decides nothing otherwise.
    :func:`run_child` calls it once the mounts are built or as a stage refuses, so one
    failed run shows, from inside this namespace, which mount each path reaches. The paths
    are each sealed root and each window under it, each sealed stand-in's own entry, the
    directory holding that entry (the stage, or the stand-in root when the stage was
    refused) and the directory above it. Each path gets a line with its ``statvfs`` flags
    and ``stat`` identity, followed by every ``/proc/self/mountinfo`` line whose mount point
    is one of the paths, above one or below one.
    """
    if launch.environ.get("KIROCREW_SANDBOX_MOUNT_REPORT") != "1":
        return
    _paths = []
    for _root in sorted(launch.seal_windowed_masks):
        _root = _root.rstrip("/")
        _paths.append(_root)
        _paths.extend(w for w in sorted(launch.private_dirs) if w.startswith(_root + "/"))
        _recorded = launch.stand_in_sources.get(_root)
        if _recorded is not None:
            _entry = _recorded[0]
            _paths.extend(
                (_entry, os.path.dirname(_entry), os.path.dirname(os.path.dirname(_entry)))
            )
    if launch.stand_in_stage is not None:
        _paths.append(launch.stand_in_stage[1])
    _paths = list(dict.fromkeys(os.path.normpath(p) for p in _paths))
    _lines = []
    for _path in _paths:
        try:
            _flags = os.statvfs(_path).f_flag
            _st = os.stat(_path)
        except OSError as exc:
            _seen = "errno %s (%s)" % (exc.errno, exc.strerror)
        else:
            _bits = (
                ("ro", os.ST_RDONLY),
                ("nosuid", os.ST_NOSUID),
                ("nodev", os.ST_NODEV),
                ("noexec", os.ST_NOEXEC),
            )
            _names = [name for name, bit in _bits if _flags & bit]
            _seen = "statvfs %s, dev:ino %d:%d" % (",".join(_names) or "rw", _st.st_dev, _st.st_ino)
        _lines.append("sandbox mount report -- %s: %s" % (_path, _seen))
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
            _mounts = fh.read().splitlines()
    except OSError as exc:
        _mounts = []
        _lines.append("sandbox mount report -- /proc/self/mountinfo: %s" % exc.strerror)
    for _line in _mounts:
        _fields = _line.split(" ")
        if len(_fields) < 5:
            continue
        _point = _fields[4]
        for _escaped, _char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
            _point = _point.replace(_escaped, _char)
        _above = _point.rstrip("/") + "/"
        if any(p == _point or p.startswith(_above) or _point.startswith(p + "/") for p in _paths):
            _lines.append("sandbox mount report -- mountinfo %s" % _line)
    sys.stderr.write("".join(line + "\n" for line in _lines))
    sys.stderr.flush()


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
