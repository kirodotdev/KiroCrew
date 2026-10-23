"""How the namespace launcher places one hiding mount and proves where it landed.

One of the launcher program's parts (see :mod:`kiro_crew.sandbox_launcher_parts`). Below its
marker line this file is the program's text from ``_mount_or_die`` to ``_locked_mount_flags``,
rendered verbatim into the one program a sandboxed spawn runs: the checked ``mount(2)`` calls
that refuse or warn, the retirement of a window's staging mount, the no-follow pin that
resolves a mask target once against the identity a pre-spawn pass carried in
(``_pin_mount_path``), the stand-in identities, the post-mount read-back
(``_verify_masked_name``) and the kernel-locked flags a seal re-asserts. A new check on how one
mask is pinned or verified goes here; a new step of the launch goes in
:mod:`~kiro_crew.sandbox_launcher_parts.mask_stages` or
:mod:`~kiro_crew.sandbox_launcher_parts.exec_lockdown`.
"""

import ctypes
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.sandbox_launcher_program import (
        _ELOOP,
        _MNT_DETACH,
        _MS_NODEV,
        _MS_NOEXEC,
        _MS_NOSUID,
        _O_PATH,
        _S_IFDIR,
        _S_IFLNK,
        _S_IFMT,
        _S_IFREG,
    )


# -- the launcher program continues below this line --
def _mount_or_die(launch, source, target, flags, what):
    """``mount(2)`` or refuse to exec, naming *what* and the errno.

    Every mount in this launcher IS a security control -- each one hides a credential
    path, or (for ``/``) pins mount propagation so the hiding cannot escape. Discarding
    the return value makes those controls fail OPEN: the path stays visible and the
    agent runs anyway, believing it is hidden. Nothing downstream notices -- there is no
    post-mount emptiness check, the launcher has no logger, and the pre-exec hardlink
    scan only fires when a credential happens to carry an extra link.

    So these refuse, matching what the rest of this launcher already does when a control
    cannot be established: both ``unshare`` calls, the seccomp-BPF install, and the
    hardlink scan all ``sys.exit``. What marks those off from the decisions that DO
    degrade open is a rule, not a list: a failed hiding mount is the one thing this
    helper exists to prevent, nothing else here is one, and each of those others argues
    its case at its own site -- the ``expose_files`` pre-read is one of them, named as an
    example and not as a roster -- no count is kept here, since the count is what goes
    stale. Read it narrowly: none is a failed hiding mount, NOT the stronger claim that
    no credential can end up reachable. A degrade elsewhere is never license to degrade
    a mount.

    ``sandbox_level`` is the explicit opt-out for a host that cannot mount; a silent
    unhidden credential is not.
    """
    if launch.libc.mount(source, target, None, flags, None) != 0:
        _err = ctypes.get_errno()
        sys.exit(
            "sandbox: BLOCKED -- %s failed: errno %d (%s). The sandbox could not "
            "establish this control, so the agent would run with the path "
            "visible. Lower agent.sandbox to run without this control "
            "deliberately." % (what, _err, os.strerror(_err))
        )


def _mount_or_warn(launch, source, target, flags, what):
    """``mount(2)`` that degrades OPEN with an advisory, for access-WIDENING mounts.

    The write carve-out pair is the inverse of every ``_mount_or_die`` site: those
    mounts WITHHOLD access and a silent failure hands the agent a visible credential, so
    they refuse; these mounts GRANT access inside an already-sealed subtree, so a failure
    means the path simply stays sealed, in which the one consequence is that a probe's
    private temp dir is unwritable. Killing the spawn for that would trade a degraded
    probe for no probe at all.

    Emits the classifier's ADVISORY severity (the prefix
    ``_SANDBOX_LAUNCHER_WARNING_PREFIX`` in dashboard/handlers/worktree.py matches; the
    severity set is ratcheted by test_worktree_create.py) and reports success so callers
    can chain: the carve-out's remount is pointless after its bind already failed.
    """
    if launch.libc.mount(source, target, None, flags, None) != 0:
        sys.stderr.write(
            "sandbox: WARNING -- %s failed (errno %d); continuing with the "
            "path sealed\n" % (what, ctypes.get_errno())
        )
        return False
    return True


def _retire_stage_or_die(launch, stage, what):
    """Take a private window's staging mount out of the namespace, or refuse to exec.

    The stage exists for one reason: to hold the window's real inode while the bind
    that hides its parent tree lands, so the window can be bound back at its own path.
    Once that has happened the stage has no reader, and what it leaves behind is a
    SECOND path to the window's real tree under a directory this launcher never masks.
    That matters because a masked leaf can sit INSIDE a window -- the re-mask hides it
    at the window's own path, and a non-recursive bind carries no submount into the
    stage, so the leaf is readable there with the mask fully applied everywhere else.

    ``MNT_DETACH``: the payload has not been exec'd yet and nothing holds the mount, and
    the flag makes the path unreachable in this namespace at once whether or not the
    kernel can free it immediately. The empty directory left behind is tidied
    best-effort -- it is an empty dir on a tmpfs, and a failure to remove it exposes
    nothing.

    Refuses on failure for the reason ``_mount_or_die`` does: a surviving stage is
    precisely the exposure the mask is here to prevent, nothing downstream looks for it,
    and the payload would run believing the leaf is hidden.
    """
    if launch.libc.umount2(stage.encode(), _MNT_DETACH) != 0:
        _err = ctypes.get_errno()
        sys.exit(
            "sandbox: BLOCKED -- could not retire the staging mount for %s: errno %d "
            "(%s). It is a second path to that tree, so the agent would run with a "
            "masked path reachable. Lower agent.sandbox to run without this control "
            "deliberately." % (what, _err, os.strerror(_err))
        )
    try:
        os.rmdir(stage)
    except OSError:
        pass


def _register_stand_in(launch, stand_in_id, masked_fd):
    """Record that the stand-in *stand_in_id* masks the object *masked_fd* holds.

    Read off the descriptor the mask is about to be mounted through, never by name, so
    the identity recorded is the one the mount lands on.
    """
    try:
        st = os.fstat(masked_fd)
    except OSError as exc:
        sys.exit(
            "sandbox: BLOCKED -- cannot read the identity of an object about to be "
            "masked (%s). Lower agent.sandbox to run without this control "
            "deliberately." % exc
        )
    launch.own_stand_ins[tuple(stand_in_id)] = (st.st_dev, st.st_ino)


def _mode_is_link(mode):
    """Whether *mode* from a no-follow ``fstat`` describes a symlink."""
    return (mode & _S_IFMT) == _S_IFLNK


def _any_kind(_mode):
    """Kind predicate for a target whose KIND is not the question.

    The read-only ceiling seal takes a directory or a plain file -- a governance ceiling
    is a single JSON document -- and bind-over-self plus MS_RDONLY seals either one the
    same way. Requiring a directory there would silently skip every ceiling FILE: the
    caller asks for it to be sealed, gets no error, and it stays writable.
    """
    return True


def _carried_occupant(launch, target):
    """The identity a PRE-SPAWN pass recorded for *target*, or ``None``.

    The launcher does not take this look itself, and that is the point. A look taken
    here lands on the far side of the script build, the ``mkstemp`` that writes it and
    the ``fork``/``unshare`` -- so an occupant read here and compared here answers about
    the same instant twice and closes nothing. The gateway's passes already classified
    these names to refuse an aliased one; what crosses into the child is their answer,
    as data.

    Absent for a name no pass observed. The launcher masks hundreds of targets and
    statting them all in the gateway would put a probe per spawn back on the single
    event loop, so the carried set is the ones a pass was already looking at. A name
    with no entry gets no expectation, which the module spec records as a residual
    rather than leaving it to read as covered.
    """
    try:
        ident = launch.mask_occupants.get(os.fsdecode(target))
    except Exception:
        return None
    if not ident:
        return None
    # The fourth element -- what the name REACHED when the pass looked -- is what tells
    # a wrong-kind substitution from the other loop meeting an object it was never meant
    # to mask. Absent from an expectation recorded without it.
    _kind = ident[3] if len(ident) > 3 else None
    # The fifth and sixth -- the REFERENT's device and inode -- are what the mask lands
    # on when the name is a link; the link's own identity says nothing about a referent
    # swapped underneath it.
    _referent = (ident[4], ident[5]) if len(ident) > 5 else None
    return (ident[0], ident[1], bool(ident[2]), _kind, _referent)


def _covered_by_own_mask(launch, target):
    """Whether an ancestor of *target* reaches a stand-in this launcher placed, now.

    Answers for an ABSENT name only, and only one question: is the name gone because a
    directory above it is already masked. Lexical ascent picks the candidates -- each
    proper ancestor of the name that ``masked_names`` holds -- and the filesystem
    decides: the ancestor is resolved once more and must reach the stand-in recorded for
    it, the same read-back the loop performed when it mounted that mask. A recorded name
    alone would not do, because the record says what the name reached when the mask was
    placed, and the question is what covers this leaf at this instant.

    A link at the ancestor is accepted only when it is the link the pin itself followed,
    as ``_verify_masked_name`` accepts it, so a link planted at a protected name and
    aimed at a stand-in reads as not covered and the caller refuses as it would for any
    vanished object.

    A private window met on the way up ends the walk with ``False``: below a window the
    name resolves into the real tree the window mounted back, so no stand-in above it
    covers the leaf, and its absence is the leaf having moved.
    """
    name = os.fsdecode(target).rstrip("/")
    while True:
        parent = os.path.dirname(name)
        if not parent or parent == name:
            return False
        name = parent
        # A bound window between the leaf and any recorded mask above it puts the leaf
        # in the REAL tree the window mounted back, where an absence is a moved object:
        # the mask above covers the window's name, not what the window exposes beneath.
        if name in launch.bound_windows:
            return False
        stand_in_id = launch.masked_names.get(name)
        if stand_in_id is None:
            continue
        try:
            entry = os.lstat(name)
            if _mode_is_link(entry.st_mode):
                pinned = launch.pinned_occupants.get(name)
                if (
                    pinned is None
                    or not pinned[2]
                    or (entry.st_dev, entry.st_ino) != tuple(pinned[:2])
                ):
                    return False
                entry = os.stat(name)
        except OSError:
            return False
        # A recorded ancestor that reaches something other than its stand-in is not
        # "keep looking higher": the record and the filesystem disagree about a name
        # this launcher masked, and the leaf is judged as vanished.
        return (entry.st_dev, entry.st_ino) == tuple(stand_in_id)


def _kind_reached(code):
    """A synthetic ``st_mode`` for a recorded referent kind, for a kind predicate."""
    if code == 1:
        return _S_IFDIR
    if code == 2:
        return _S_IFREG
    return 0


def _pin_mount_path(launch, target, kind, require_present=False):
    """Resolve *target* ONCE and hand it on as a path that cannot be re-aimed.

    Every hiding mount asks two things about one name: is there an object of the right
    KIND here, and then mount over it. Asking the name twice makes those two questions
    about two different lookups, so a name swapped in between is judged as the old
    object and bound as the new one -- the mask lands on whatever the name points at by
    then, while the bytes it exists to cover sit at a name nothing masks. The data home
    is writable by an already-running sandboxed process, so that racing writer is
    ordinary rather than exotic.

    The name is therefore resolved ONCE, into a descriptor, and the caller mounts over
    ``/proc/self/fd/<fd>``: that path names the object this descriptor holds, whatever
    the name says by then. ``O_PATH`` asks for no read permission, which matters because
    several masked leaves are 0600 files this process cannot open for reading.

    *kind* is the ``stat`` predicate the caller requires -- ``S_ISDIR`` for a directory
    mask, ``S_ISREG`` for a file mask. Symlinks are FOLLOWED, exactly as a plain
    ``isdir``/``isfile`` guard follows them, so a supported symlinked data home keeps
    working; what the descriptor changes is only that the object the mask covers is the
    object that was classified.

    THE FIRST LOOK DOES NOT FOLLOW, and that is a separate property from the one above.
    A link occupying a protected name has two unrelated causes and they need opposite
    answers: an ordinary ``stow`` or ``chezmoi`` layout has had one there since before
    the gateway started, and refusing it would fail every strict spawn on a supported
    machine; a link SUBSTITUTED for a directory while this launcher looks is a redirect,
    and following it masks the planter's decoy while the real directory, renamed aside,
    stays readable. No single instant separates them -- both show a link -- so this
    does not try to. It opens the name once WITHOUT following, reports the identity of
    whatever occupied it, and lets the caller hand that identity back on a later call. A
    link that was already there is the same link at both looks and passes. A directory
    replaced by a link is not, and refuses.

    ``O_PATH | O_NOFOLLOW`` is what makes the first look possible without charging the
    supported layout: it does not refuse a link, it returns a descriptor on the link
    ITSELF, which is how the kind is told apart from the identity. ``O_DIRECTORY |
    O_NOFOLLOW`` would refuse instead, and that refusal lands on the stow layout rather
    than on the planter.

    The expected occupant is never passed in. It is looked up from ``mask_occupants`` --
    what a pre-spawn pass recorded for this name -- so every call site is covered by
    construction, and a name no pass observed carries no expectation and is judged on
    kind alone.

    THE NAME IS NEVER RESOLVED AS A WHOLE PATH TWICE. The parent directory is opened once
    and held, the no-follow first look happens relative to that descriptor, and when a
    link holds the name its target is read from the descriptor already open ON THAT LINK
    rather than by looking the name up again. Resolving the name a second time would put
    a fresh whole-path lookup after the occupant comparison, which is exactly the check
    being bypassed: the comparison would pass on the object the first look saw while the
    mask bound whatever the name reached a moment later. Reading the link through its own
    descriptor has no such window, and it is also immune to a replacement link that
    reuses the old inode -- an identity comparison after the fact is not.

    THE TWO FAILURES ARE NOT THE SAME FAILURE, and collapsing them is how a mask goes
    missing in silence:

    * NOTHING OF THAT KIND IS HERE -- the name does not exist, or holds an object of the
      other kind. That is a SKIP, and it has to be, because it is exactly what the plain
      guards did: every caller-supplied path is offered to both the directory loop and
      the file loop, and each takes the entries of its own kind. Returns ``(None,
      None)``. A name that vanishes mid-call lands here too, as ``ENOENT``, and skips for
      the same reason -- only *require_present*, or a carried expectation, turns that
      into a refusal.
    * SOMETHING IS HERE AND CANNOT BE PINNED -- ``open`` is denied where ``stat``
      succeeded (a restriction inherited from the parent process does exactly this, as
      the ``expose_files`` pre-read records), or the name resolves to something that
      cannot be opened at all. The caller asked for this path to be masked and it exists,
      so skipping would exec the agent with it VISIBLE and no line anywhere saying so.
      Refuses the spawn instead.

    One knob turns a skip into a refusal. *require_present* refuses ABSENCE alone, for a
    target a pre-spawn materialiser established: the object was there moments ago, so an
    empty name now means the name moved. It deliberately leaves the wrong-KIND skip
    alone, because both loops are offered every path and the one that does not cover
    this object meets it in normal operation. The carried expectation is what refuses a
    CHANGED object of either kind.

    Returns ``(fd, path)`` on a match, ``(None, None)`` on a skip. The caller MUST keep
    *fd* open until its mount returns, because the proc path lives only as long as the
    descriptor, and MUST close it afterwards.
    """

    def _refuse(why):
        sys.exit(
            "sandbox: BLOCKED -- cannot pin %s to mask it: %s. Masking it by name "
            "instead could cover a different object, and skipping it would run the "
            "agent with the path VISIBLE. Lower agent.sandbox to run without this "
            "control deliberately." % (os.fsdecode(target), why)
        )

    # The PARENT is held for the duration, and every look at the leaf happens relative
    # to that descriptor, so no component above it can be redirected in between and the
    # leaf is never reopened by name.
    _t = os.fsencode(target)
    while len(_t) > 1 and _t.endswith(b"/"):
        _t = _t[:-1]
    _parent, _leaf = os.path.split(_t)
    if not _leaf:
        if require_present:
            _refuse("it names no leaf to mask")
        return None, None
    # The expectation is looked up HERE, not passed in by each caller. Every hiding
    # mount reaches this one function, so binding the check to the function rather than
    # to an argument is what makes a NEW call site covered the day it is written: there
    # is no keyword for it to forget. Looked up BEFORE the name is opened, because an
    # ABSENT name is judged by it too: a pass recorded an object here moments ago, so
    # finding nothing now means the object was moved, and the skip an unestablished
    # absent name takes would exec with that object readable at whatever name it moved
    # to. ``~/.ssh`` is the sharpest case -- the strict tier records it and nothing else
    # marks it required -- and the dangling-link branch below refuses on the same
    # expectation; an absent name is the same vanished object seen from the other side.
    expect_occupant = _carried_occupant(launch, target)

    def _refuse_if_established(how):
        if expect_occupant is not None:
            _refuse(
                "the object a pass established at this name has vanished (%s), so "
                "the mask cannot cover it and whatever is recreated there would be "
                "exposed" % how
            )

    try:
        # ``_O_PATH``, not ``O_RDONLY``: this descriptor is only the ``dir_fd`` anchor
        # for the no-follow ``openat``/``readlinkat`` below, which need search (execute)
        # permission on the directory, never read. ``O_RDONLY`` would demand read too, so
        # an execute-only parent -- an ordinary mode on a directory the launcher may
        # traverse and mount through -- would abort the spawn for a permission the
        # operation does not require.
        parent_fd = os.open(_parent or b".", _O_PATH | os.O_DIRECTORY)
    except FileNotFoundError:
        # Absent under a directory this launcher has already masked is the mask in
        # place -- the leaf is unreachable through its parent's stand-in -- and neither
        # a moved object nor a materialised target gone missing. Decided against the
        # filesystem now, not the mask list: see ``_covered_by_own_mask``.
        if _covered_by_own_mask(launch, _t):
            return None, None
        if require_present:
            _refuse("the directory holding it is absent")
        _refuse_if_established("the directory holding it is absent")
        return None, None
    except OSError as exc:
        _refuse("%s" % exc)

    # The FIRST look, which does not follow: this reports what occupies the name itself,
    # so a link is told apart from a directory without being refused.
    try:
        name_fd = os.open(_leaf, os.O_RDONLY | _O_PATH | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        os.close(parent_fd)
        if _covered_by_own_mask(launch, _t):
            return None, None
        if require_present:
            _refuse("it is absent")
        _refuse_if_established("it is absent")
        return None, None
    except OSError as exc:
        os.close(parent_fd)
        # Without ``O_PATH`` a no-follow open of a LINK fails with ``ELOOP``: the kernel
        # offers no way to hold the link itself. Only Linux has ``O_PATH``, and this
        # launcher is Linux-only (``unshare``, ``mount``), so on any other host a link at
        # a protected name is refused outright rather than followed unpinned. Named, so
        # the refusal reads as the platform limit it is.
        if not _O_PATH and getattr(exc, "errno", None) == _ELOOP:
            _refuse(
                "a link holds the name and this host has no O_PATH to pin a link "
                "without following it; the namespace launcher requires Linux"
            )
        _refuse("%s" % exc)
    try:
        name_st = os.fstat(name_fd)
    except OSError as exc:
        os.close(name_fd)
        os.close(parent_fd)
        _refuse("%s" % exc)
    occupant = (name_st.st_dev, name_st.st_ino, _mode_is_link(name_st.st_mode))
    launch.pinned_occupants[os.fsdecode(target)] = occupant

    if occupant[2]:
        # A link holds the name. Follow it ONCE, deliberately, because the supported
        # layout depends on it -- and follow it through the DESCRIPTOR already held on
        # that link, never by resolving the name again. Reading the link's own content
        # with an empty relative path against its descriptor cannot be redirected: the
        # directory entry may be replaced while this runs, including by a new link that
        # reuses the old inode, and this still reads the content of the link the first
        # look classified. A relative target is resolved against the held parent, which
        # is where the link's own target is relative to, so no component above it can be
        # redirected either.
        try:
            _link_to = os.fsencode(os.readlink("", dir_fd=name_fd))
        except OSError as exc:
            os.close(name_fd)
            os.close(parent_fd)
            _refuse("%s" % exc)
        os.close(name_fd)
        try:
            if _link_to.startswith(b"/"):
                fd = os.open(_link_to, os.O_RDONLY | _O_PATH)
            else:
                fd = os.open(_link_to, os.O_RDONLY | _O_PATH, dir_fd=parent_fd)
        except FileNotFoundError:
            os.close(parent_fd)
            if require_present:
                _refuse("its link target is absent")
            # A name a pass RECORDED an occupant for is an established mask target, so a
            # referent that has vanished by the time this pin runs is a substitution, not
            # an ordinary absent optional: skipping here would leave the target unmasked
            # and expose whatever is recreated at the name. The carried expectation makes
            # that case fail closed even when ``require_present`` is unset -- e.g. a
            # symlinked ``.env`` whose dotfile-managed referent is mid-restow. A link the
            # pass ALREADY saw dangling, and that is still the same link, has not
            # changed: there is nothing here to mask and nothing moved. Refusing it would
            # fail every strict spawn on a host whose dotfile layout leaves a stale
            # ``~/.ssh`` link, for no exposure.
            if expect_occupant is not None:
                _same_dangling_link = (
                    occupant[:3] == expect_occupant[:3] and expect_occupant[3] == 0
                )
                if not _same_dangling_link:
                    _refuse(
                        "the object a pass established at this name has vanished, so "
                        "the mask cannot cover it and whatever is recreated there "
                        "would be exposed"
                    )
            return None, None
        except OSError as exc:
            os.close(parent_fd)
            _refuse("%s" % exc)
    else:
        # The name holds the object itself, so the first look already pinned it and
        # there is no second resolution to race.
        fd = name_fd
    os.close(parent_fd)
    try:
        matched = kind(os.fstat(fd).st_mode)
    except OSError as exc:
        os.close(fd)
        _refuse("%s" % exc)
    # The occupant comparison is computed ABOVE the kind check and applied on both sides
    # of it, deliberately. A carried expectation is a statement about the OBJECT at this
    # name. A substitution that also changes the kind -- a key directory replaced by a
    # file, or by a link to one -- reaches the wrong-kind branch below, and must refuse
    # there rather than take the ordinary skip and exec with the moved keys readable. But
    # not every changed object of the wrong kind is a substitution: every caller-supplied
    # path is offered to both the directory loop and the file loop, and once the
    # directory loop has masked a directory, the file loop reaches the STAND-IN -- a
    # different object, of the wrong kind, and exactly what the mask was for. What tells
    # the two apart is what the pass saw the name REACH: the fourth element of the
    # expectation. A wrong-kind object refuses only when the pass saw an object of THIS
    # loop's kind there.
    #
    # DEVICE AND INODE, because that identifies the OBJECT. Weaker attributes do not:
    # comparing only link-ness admits a same-kind decoy, since a directory swapped for
    # another directory satisfies it while the real tree sits unmasked at whatever name
    # the writer moved it to.
    #
    # THERE ARE NO EXCEPTIONS FOR LEGITIMATE RECREATION, DELIBERATELY. A protected target
    # whose inode changes between the pass that observed it and this pin is refused, and
    # some of those changes are ordinary rather than hostile: an atomic write replaces an
    # inode by design, and a staging directory recreated mid-flight is a new object.
    # Which of those a sandbox should permit is a threat-model decision about what the
    # operator's own tooling may do to a protected name while an agent runs -- it is not
    # derivable from this function, and inventing a list here would either break ordinary
    # hosts or quietly reopen the hole. The module spec enumerates the recreations this
    # rejects so the choice is made from a list rather than a hypothesis.
    #
    # The link-ness flag is compared alongside device and inode, not only the two. A
    # filesystem is free to RECYCLE an inode number the moment its prior holder is
    # unlinked, so a real directory replaced by a symlink -- or a symlink recreated --
    # can land on the very (dev, ino) the pass recorded. Device and inode then read as
    # unchanged while the KIND of object at the name flipped, which is exactly the
    # substitution this refuses. The kind is the third element the pass already
    # recorded, so requiring it to match too closes that window at no extra syscall.
    _replaced = expect_occupant is not None and occupant[:3] != expect_occupant[:3]
    # For a LINK, the object the mask lands on is the referent, and the link's own
    # identity above says nothing about it: a referent swapped for another object of the
    # same kind leaves the link untouched. So when the pass recorded what the link
    # reached, the followed descriptor is compared against THAT too. A name that is not a
    # link is its own referent and is already compared above.
    if (
        not _replaced
        and expect_occupant is not None
        and occupant[2]
        and expect_occupant[4] is not None
    ):
        try:
            _ref_st = os.fstat(fd)
        except OSError as exc:
            os.close(fd)
            _refuse("%s" % exc)
        _replaced = (_ref_st.st_dev, _ref_st.st_ino) != tuple(expect_occupant[4])
    # A changed occupant that IS a stand-in this launcher already bound over THE OBJECT
    # THIS NAME'S EXPECTATION NAMES is the same object reached by a second spelling,
    # after the first spelling masked it: the mask is in place and there is nothing left
    # at this name to cover. Two conditions, both required. Only the entry AT the name
    # counts, never a link's referent: a same-UID writer can plant a link at a protected
    # name aimed into the directory that holds the stand-ins, and following it would read
    # as "already masked" while the real tree sits renamed aside. And the stand-in must
    # be the one bound over this very object, not merely one this launcher made: the
    # stand-in source can fall back to the system tempdir on the home filesystem, where
    # the same writer can rename an enumerable stand-in onto a protected name -- a
    # stand-in that masks a different object then refuses here exactly as any other
    # swapped-in directory does. The stand-in that masks THIS object sits on a mount over
    # this object's own entry, which no rename can move (EBUSY on a mount point), so a
    # match can only be the launcher's own mount reached by another name.
    if (
        _replaced
        and not occupant[2]
        and launch.own_stand_ins.get(occupant[:2]) == tuple(expect_occupant[:2])
    ):
        os.close(fd)
        # The second spelling of a masked directory covers every leaf listed under it
        # exactly as the first does, so it is recorded with the stand-in it was just
        # confirmed to reach.
        launch.masked_names[os.fsdecode(_t)] = occupant[:2]
        return None, None
    if not matched:
        os.close(fd)
        if _replaced and expect_occupant[3] is not None and kind(_kind_reached(expect_occupant[3])):
            _refuse(
                "a DIFFERENT object holds that name than the one the pass that "
                "established it saw, and it is not even the kind of object that "
                "was there, so the object it inspected is unmasked at whatever "
                "name it moved to"
            )
        # NOT gated on ``require_present``: an established target is still established
        # when the loop that does not cover it looks, so refusing here would fail the
        # spawn on the loop that was never meant to mask it.
        return None, None
    if _replaced:
        os.close(fd)
        _refuse(
            "a DIFFERENT object holds that name than the one the pass that "
            "established it saw, so the object it inspected is unmasked at "
            "whatever name it moved to"
        )
    return fd, ("/proc/self/fd/%d" % fd).encode()


def _mask_required(launch, name):
    """Whether *name* is a target something established before this launcher ran.

    A mask target can be absent for two unrelated reasons, and they call for opposite
    answers. A credential store the operator never created is simply not there, and
    skipping it is right -- refusing would fail every spawn on a host that happens not
    to use that tool. A target a pre-spawn materialiser created is different: the caller
    holds proof the object existed moments ago, so finding the name empty now means the
    name was moved, and the mask this loop is about to place by that name would cover
    whatever replaced it. The planner passes those names in, so the launcher can refuse
    exactly the second case. Accepts either spelling, because the mount loops encode
    their targets.
    """
    if isinstance(name, bytes):
        name = os.fsdecode(name)
    return name in launch.required_mask_targets


def _stand_in_identity(stand_in):
    """The ``(dev, ino)`` of a stand-in this launcher just created, pinned no-follow.

    Taken BEFORE the stand-in is mounted, and handed to the post-mount name check in
    place of the stand-in's PATH. The stand-in usually lives in a host-shared tmpfs
    (``/run/user/$UID`` or ``/dev/shm``), falling back to the system tempdir, and any
    same-UID process can write to each of those, so re-resolving its path after the
    mount would let a writer replace it with a link to the protected name and have both
    sides of the comparison reach the same unmasked object. An identity read off a
    no-follow descriptor cannot be re-aimed. A bind mount presents its source's device
    and inode, so the name, once masked, reads back as exactly this pair.
    """
    try:
        fd = os.open(stand_in, os.O_RDONLY | _O_PATH | os.O_NOFOLLOW)
    except OSError as exc:
        sys.exit(
            "sandbox: BLOCKED -- cannot pin the stand-in %s this launcher just "
            "created (%s), so the mask it is about to place could not be verified. "
            "Lower agent.sandbox to run without this control deliberately."
            % (os.fsdecode(stand_in), exc)
        )
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    return (st.st_dev, st.st_ino)


def _verify_masked_name(launch, name, stand_in_id, what):
    """Refuse unless *name* reaches the stand-in *stand_in_id* now that the mask is mounted.

    SCOPE, because the difference matters and the name does not carry it: this runs
    ONCE, at spawn, and answers one question -- did the mask this loop just mounted land
    on the name the caller configured. It is not a standing guarantee about that name for
    the life of the namespace. Nothing re-checks afterwards, so an atomic replacement of
    the name later (a same-uid regular file swapped in by the operator's own Dev Fleet
    cutover, for instance) is not detected by this or by anything downstream of it. Read
    it as a spawn-time assertion, never as a durable anchor.

    Pinning the target closes one half of the window: the mount covers the object the
    classification inspected, whatever the name says by then. The NAME is the other half.
    A rename landing between the pin and the mount leaves the mask on the object that was
    inspected while the name reaches the racing writer's replacement -- and that is not a
    leak of what was there, it is a WRITABLE object at a protected name. Several of these
    names are read back by the gateway as authoritative, so a writable stand-in at one of
    them buys an agent records the gateway trusts.

    So the name is resolved once more, after the mount, and REQUIRED to reach the
    stand-in this mask just bound -- by the identity the caller pinned BEFORE mounting
    (:func:`_stand_in_identity`), never by re-resolving the stand-in's own path, which
    sits in a shared tmpfs a racing writer can re-aim. A mismatch means the name escaped
    its mask, and the spawn refuses rather than running with that name writable. This is
    the same re-resolve-and-refuse step the read-only ceiling seal performs for its own
    remount, applied to the hiding mounts.
    """

    def _cannot_confirm(exc):
        sys.exit(
            "sandbox: BLOCKED -- cannot confirm %s is masked after mounting over it "
            "(%s). The mask may not cover that name, so the agent could reach it. "
            "Lower agent.sandbox to run without this control deliberately."
            % (os.fsdecode(what), exc)
        )

    # The NAME is read no-follow first. A following ``stat`` alone would accept a link
    # planted at the name and aimed at the stand-in -- the stand-in sits in a shared
    # tmpfs whose prefix a same-UID writer can enumerate -- and the mask would then sit
    # elsewhere while a replaceable link holds the protected name. So the entry at the
    # name must be EITHER the mounted stand-in itself (the mount replaces the entry's
    # identity with its source's) OR the very link the pin saw and followed, in which
    # case following it must reach the stand-in.
    try:
        entry = os.lstat(name)
    except OSError as exc:
        _cannot_confirm(exc)
    if _mode_is_link(entry.st_mode):
        pinned = launch.pinned_occupants.get(os.fsdecode(name))
        if pinned is None or not pinned[2] or (entry.st_dev, entry.st_ino) != pinned[:2]:
            sys.exit(
                "sandbox: BLOCKED -- %s is a link now and was not the link the pin "
                "followed, so another process planted it after the mask was placed "
                "and the mask covers something else. Lower agent.sandbox to run "
                "without this control deliberately." % os.fsdecode(what)
            )
        try:
            reached = os.stat(name)
        except OSError as exc:
            _cannot_confirm(exc)
    else:
        reached = entry
    if (reached.st_dev, reached.st_ino) != tuple(stand_in_id):
        sys.exit(
            "sandbox: BLOCKED -- %s does not reach its mask after mounting: another "
            "process renamed that name, so it names a DIFFERENT object that would "
            "stay writable inside the sandbox. Lower agent.sandbox to run without "
            "this control deliberately." % os.fsdecode(what)
        )


def _locked_mount_flags(target):
    """Mount flags on *target* the kernel may have LOCKED, ready to re-assert.

    Inside an unprivileged user namespace the kernel treats a mount's nosuid / nodev /
    noexec bits as locked and rejects with EPERM any remount whose flag set would clear
    them. A bind created over *target* inherits those bits -- locks included -- from its
    source mount (/tmp carries nosuid,nodev by default on AL2023 / Fedora / RHEL), so the
    sealing remount must carry them again. Called AFTER the bind step, so ``f_flag``
    reflects the new bind's effective flags. Re-asserting a bit already in force can only
    keep restrictions, never widen access. atime is left alone: a remount that passes no
    atime flag preserves the existing mode, which already satisfies MNT_LOCK_ATIME.

    On ``statvfs`` failure fall back to 0 extra flags: the remount then behaves exactly
    as it would without this helper, and a locked-flag rejection still fails closed at
    the call site. Never degrades the seal.
    """
    try:
        f_flag = os.statvfs(target).f_flag
    except OSError:
        return 0
    flags = 0
    # getattr, never bare os.ST_*: the launcher only RUNS on Linux, where all three
    # exist, but this module is imported on every host and macOS defines only ST_RDONLY
    # / ST_NOSUID -- a bare os.ST_NODEV there raises AttributeError, which the OSError
    # fallback above deliberately does not swallow.
    if f_flag & getattr(os, "ST_NOSUID", 0):
        flags |= _MS_NOSUID
    if f_flag & getattr(os, "ST_NODEV", 0):
        flags |= _MS_NODEV
    if f_flag & getattr(os, "ST_NOEXEC", 0):
        flags |= _MS_NOEXEC
    return flags
