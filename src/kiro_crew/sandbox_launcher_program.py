#!/usr/bin/env python3
"""Namespace sandbox launcher — spawned by Kiro Crew.

The program a sandboxed agent spawn runs first on Linux. ``kiro_crew.sandbox_launcher``
renders this module's own source with ONE substitution, the plan's data (``_PLAN``
below); ``kiro_crew.sandbox.namespace_argv`` writes it to
``<config_dir>/run/kirocrew_sandbox_<pid>_<rand>.py`` and the gateway runs it as
``python -I -S <file> <agent argv...>``. So it imports nothing but the standard library,
and every import sits at module level, ahead of the isolation it builds.

:func:`main` forks. The parent writes the child's identity uid/gid maps and waits; the
child enters the user and mount namespaces (:func:`enter_namespaces`), then
:func:`run_child` builds the sandbox through its stages -- among them
:func:`stage_private_windows`, :func:`seal_readonly`, :func:`mask_sensitive`,
:func:`apply_carveouts`, :func:`mask_sensitive_files`, :func:`mask_ssh_keys`,
:func:`scrub_env`, :func:`drop_privileges`, :func:`install_seccomp` and
:func:`refuse_hardlinked_credentials` -- and ends in :func:`exec_agent`.
:func:`run_child` and :func:`place_masks` fix the order. The child keeps the real uid
and gid: no uid 0, no uid 65534.

Every stage takes the :class:`Launch` it works on: the plan's data, the libc it mounts
through, and what earlier stages recorded. Imported as ``kiro_crew.sandbox_launcher_program``
the module does nothing at import, so the stages can be driven in-process with a stand-in
libc.
"""

import sys

if __name__ == "__main__":
    # Harden against stdlib shadowing. As a script this runs as
    # ``python <config_dir>/run/kirocrew_sandbox_*.py``, so CPython prepends the
    # script's own directory to sys.path, and a stray sibling module left there by
    # another process -- struct.py, os.py -- would shadow the real stdlib and crash
    # the imports below (seen in the wild: "ImportError: cannot import name 'calcsize'
    # from '/tmp/struct.py'", which kills the agent subprocess on spawn). ``sys`` is a
    # builtin and cannot be shadowed, so it is imported first; the launcher dir and
    # any cwd "" entry are dropped before anything resolves from the filesystem.
    sys.path[:] = [p for p in sys.path if p not in ("", sys.path[0])]

# Everything is imported up front, ``platform`` and ``struct`` too although only the
# seccomp step uses them: a FIRST-TIME stdlib import reads module files off disk, and
# once the child has entered its user and mount namespaces that read can be denied by
# the host's LSM (Ubuntu 24.04 with apparmor_restrict_unprivileged_userns=1 denies the
# post-unshare read, so an ``import platform`` at seccomp-install time would die with
# ModuleNotFoundError and fail every sandboxed spawn). Everything this launcher needs is
# imported while it is still pre-isolation, so no post-isolation code touches the
# filesystem for stdlib.
import ctypes  # noqa: E402
import os  # noqa: E402
import platform as _plat  # noqa: E402
import stat  # noqa: E402
import struct as _struct  # noqa: E402
import tempfile  # noqa: E402

_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNS = 0x00020000
_MS_RDONLY = 1
_MS_NOSUID = 2
_MS_NODEV = 4
_MS_NOEXEC = 8
_MS_REMOUNT = 32
_MS_BIND = 4096
_MS_REC = 16384
_MS_PRIVATE = 1 << 18
#: ``umount2`` flag: take the mount out of this namespace's tree now and let the kernel
#: release it when the last reference goes. Retires a private window's staging mount,
#: which is a second path to that window's real tree.
_MNT_DETACH = 2
_PR_SET_DUMPABLE = 4

_O_PATH = getattr(os, "O_PATH", 0)
_ELOOP = __import__("errno").ELOOP

#: ``S_IFMT`` and friends, spelled out rather than taken from ``stat`` so the identity
#: helpers below read nothing but their arguments.
_S_IFMT = 0o170000
_S_IFLNK = 0o120000
_S_IFDIR = 0o040000
_S_IFREG = 0o100000


def _load_libc():
    """The libc ALREADY loaded into this interpreter, with the calls this launcher makes.

    dlopen(NULL), never ``ctypes.util.find_library``: on Linux that EXECUTES helper
    processes to locate libc (ldconfig first, then a PATH-resolved gcc/cc/objdump/ld
    once ldconfig yields no match, i.e. on musl hosts). This runs BEFORE the fork and
    before either unshare, under an environment the SPAWNING CALLER supplies, so on such
    a host a caller-controlled ``gcc`` on PATH would be same-user code execution ahead
    of the confinement this launcher exists to establish. The spawned userns probe in
    ``_PROBE_SHIM_CODE`` resolves libc the same way, for the same reason.
    ``ctypes.util`` is deliberately left unimported so a reintroduction fails loudly
    instead of silently reopening the PATH lookup.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    libc.mount.restype = ctypes.c_int
    libc.unshare.argtypes = [ctypes.c_int]
    libc.unshare.restype = ctypes.c_int
    libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.umount2.restype = ctypes.c_int
    # A libc without prctl(2) reads as ``None`` here, which every stage checks before use.
    setattr(libc, "prctl", getattr(libc, "prctl", None))
    if libc.prctl:
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.prctl.restype = ctypes.c_int
    return libc


class Launch:
    """One launch: the plan's data, the libc it mounts through, and what the stages record.

    *plan* is the mapping ``kiro_crew.sandbox_plan.namespace_payload`` produces. *libc*
    needs ``mount``, ``umount2``, ``unshare`` and ``prctl`` (``None`` when libc exposes
    no prctl). *environ* is the environment the agent inherits. *execvp*, when given,
    stands in for ``os.execvp``, the call that replaces this process with the agent.
    """

    def __init__(self, plan, libc, environ=None, execvp=None):
        self.libc = libc
        self.environ = os.environ if environ is None else environ
        self.execvp = execvp
        self.real_uid = plan["real_uid"]
        self.real_gid = plan["real_gid"]
        self.sensitive_dirs = plan["sensitive_dirs"]
        self.sensitive_dir_ids = plan["sensitive_dir_ids"]
        self.private_dirs = plan["private_dirs"]
        self.private_dir_ids = plan["private_dir_ids"]
        self.private_readonly_windows = frozenset(plan.get("private_readonly_windows", []))
        self.readonly_dirs = plan["readonly_dirs"]
        self.writable_dirs = plan["writable_dirs"]
        self.sensitive_files = plan["sensitive_files"]
        self.fail_closed_file_masks = plan["fail_closed_file_masks"]
        self.alias_credential_ids = plan["alias_credential_ids"]
        self.required_mask_targets = frozenset(plan["required_mask_targets"])
        self.mask_occupants = plan["mask_occupants"]
        self.crew_home_aliases = plan["crew_home_aliases"]
        self.expose_files = plan["expose_files"]
        self.env_prefixes = plan["env_prefixes"]
        self.ssh_dir = plan["ssh_dir"]
        self.ssh_known_hosts = plan["ssh_known_hosts"]
        self.hide_ssh = bool(plan["hide_ssh"])
        self.sandbox_level = plan["sandbox_level"]
        self.unreadable_masks = plan["unreadable_masks"]
        self.strict_host_key_opt = plan["strict_host_key_opt"]
        self.stand_in_roots = plan["stand_in_roots"]
        #: What ``_pin_mount_path`` saw holding each NAME it pinned, ``(dev, ino,
        #: is_link)``, keyed by the decoded name. Written by the pin, read by the
        #: post-mount name check, so a link planted at the name AFTER the pin is told
        #: apart from a link the pin itself saw and followed.
        self.pinned_occupants = {}
        #: The ``(dev, ino)`` of every stand-in THIS launcher has created, mapped to
        #: the ``(dev, ino)`` of the OBJECT it was bound over. A mask list can carry one
        #: object under two spellings -- a symlinked ``$HOME`` lists each crew hidden
        #: leaf under both -- and once the first spelling is masked the second reaches
        #: the stand-in the first was bound to. That is the mask doing its job, and this
        #: map is how the pin tells it from a swapped object: a stand-in found at a name
        #: is accepted only when the name's carried expectation IS the object it masks.
        #: Membership alone would not do, because the stand-in source can fall back to
        #: the system tempdir on the home filesystem, where a same-UID writer can rename
        #: an enumerable stand-in onto a protected name.
        self.own_stand_ins = {}
        #: Every NAME this launcher has confirmed reaches one of its own stand-ins,
        #: mapped to that stand-in's ``(dev, ino)``. Read by the pin when a name is
        #: ABSENT: a mask list carries a leaf together with a directory above it, and
        #: once the directory's stand-in is bound the leaf is gone from every later
        #: look at its name. That absence is the mask in place, not the object moved,
        #: and it is told apart by asking whether an ancestor of the name reaches a
        #: stand-in recorded here RIGHT NOW. Names, not identities: the second spelling
        #: of a masked directory holds no mount of its own, yet the leaves under it are
        #: covered all the same.
        self.masked_names = {}
        #: Every private window this launcher has BOUND. A leaf under a window resolves
        #: into the window's real tree, not into the ancestor's stand-in, so an absent
        #: leaf there is a moved object and the nested re-hide must refuse it.
        self.bound_windows = set()
        #: The tmpfs every bind source is created on, or ``None`` for the system tempdir.
        self.tmpfs_src = None
        #: The pid-tagged prefix of every bind source this launcher creates.
        self.src_prefix = ""
        #: The exposed files' bytes, read before the masks hide their parents.
        self.expose_data = {}
        #: Private window -> the staging mount that holds its real inode.
        self.private_stage = {}
        #: Whether this process made itself non-dumpable before unsharing its mounts.
        self.nondumpable = False


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


def enter_namespaces(launch, c2p_w, p2c_r):
    """Unshare the user namespace, wait for the parent's maps, then the mount namespace.

    Signals the parent on *c2p_w* once the user namespace exists, waits on *p2c_r* for
    the uid/gid maps, and makes mount propagation private on ``/`` so no mask placed in
    this namespace can escape it.
    """
    libc = launch.libc
    if libc.unshare(_CLONE_NEWUSER) != 0:
        sys.exit(f"sandbox: unshare(NEWUSER) failed: errno {ctypes.get_errno()}")
    os.write(c2p_w, b"x")  # tell the parent
    os.close(c2p_w)
    os.read(p2c_r, 1)  # wait for the maps
    os.close(p2c_r)

    # Non-dumpable BEFORE the mount namespace exists, not just around the unreadable
    # mask's stage: a same-uid process that opened /proc/<pid>/root after
    # unshare(CLONE_NEWNS) would keep a descriptor into this namespace's tree, reach the
    # stage tmpfs through it later and chmod the mode-0 stand-in readable. Opened before
    # the unshare, the same descriptor names the host tree, which never sees these
    # mounts. Restored once the sensitive-file masks are in place and the stage is
    # detached; exec resets it for the payload in any case. The parent has already
    # written the uid/gid maps, which is the one thing that needed this process to be
    # dumpable.
    launch.nondumpable = bool(libc.prctl) and (libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) == 0)

    if libc.unshare(_CLONE_NEWNS) != 0:
        sys.exit(f"sandbox: unshare(NEWNS) failed: errno {ctypes.get_errno()}")

    _mount_or_die(
        launch, None, b"/", _MS_REC | _MS_PRIVATE, "making mount propagation private on /"
    )


def _home_device(environ):
    """The device of ``$HOME`` as *environ* spells it, or ``None`` when it cannot be read."""
    try:
        return os.stat(environ.get("HOME") or os.path.expanduser("~")).st_dev
    except OSError:
        return None


def pick_stand_in_root(launch):
    """Choose the tmpfs every bind source is created on, and tag sources with this pid.

    Same-fs binds (e.g. /tmp on ext4 over ~/.kiro/crew/.env on ext4) can corrupt the
    target's host directory entry via a kernel propagation race when the private
    namespace is torn down -- leaving the host file pointing at the empty source inode
    permanently. Cross-fs binds use distinct inode spaces and cannot leak that way. So
    each candidate (``/run/user/$UID``, then ``/dev/shm``) is verified to sit on a
    different filesystem from HOME; with none available the system default tempdir is
    used and the kernel-race risk accepted -- better to function than to refuse to start.

    Every bind-mount SOURCE is tagged with this process's pid. The kernel pins a bind
    source for the mount's lifetime, so these entries cannot be unlinked here and are
    orphaned when the sandboxed process exits; the pid in the name is the liveness key
    the periodic janitor (``_cleanup_stale_sandbox_mount_sources``) probes to reclaim
    them. exec preserves the pid, so this pid IS the running agent's pid. The probe uses
    the sibling ``kirocrew_sbprobe_`` prefix, OUTSIDE the pid-parsed family, so the
    janitor never races its mkdtemp/rmdir window.
    """
    home_dev = _home_device(launch.environ)
    for candidate in launch.stand_in_roots:
        try:
            if home_dev is not None and os.stat(candidate).st_dev == home_dev:
                continue  # same fs as HOME -- no isolation, the race is still possible
            probe = tempfile.mkdtemp(dir=candidate, prefix="kirocrew_sbprobe_")
            try:
                os.rmdir(probe)
            except FileNotFoundError:
                pass  # an external cleaner won the race -- the root still works
            launch.tmpfs_src = candidate
            break
        except (OSError, ValueError):
            continue
    launch.src_prefix = "kirocrew_sb_%d_" % os.getpid()


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
            _stage_dir = tempfile.mkdtemp(dir=launch.tmpfs_src, prefix=launch.src_prefix)
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
            per_dir_empty = tempfile.mkdtemp(
                dir=launch.tmpfs_src, prefix=launch.src_prefix
            ).encode()
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
        # Seal the read-only windows AFTER every window is bound. A read-write window
        # may sit INSIDE a read-only one (an app bundle sealed read-only, its ``data/``
        # kept writable): the inner bind above is its OWN mount and keeps its own
        # (writable) flags, so remounting the outer window read-only now does not reach
        # it. MS_RDONLY is ignored on the initial MS_BIND, hence this remount, with the
        # kernel-locked bits re-asserted as the ceiling seal does. The remount names the
        # path, so the name must answer as read-only afterwards: a seal that landed on
        # some other mount leaves the window writable, which is a refusal.
        for p in _windows:
            if p not in launch.private_readonly_windows:
                continue
            _mount_or_die(
                launch,
                p.encode(),
                p.encode(),
                _MS_REMOUNT | _MS_BIND | _MS_RDONLY | _locked_mount_flags(p.encode()),
                "sealing read-only window %s" % p,
            )
            if not os.statvfs(p).f_flag & os.ST_RDONLY:
                sys.exit(
                    "sandbox: BLOCKED -- the read-only seal on window %s did not take, "
                    "so the window would stay writable" % p
                )
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
                    _nested_empty = tempfile.mkdtemp(
                        dir=launch.tmpfs_src, prefix=launch.src_prefix
                    ).encode()
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
    _parent = launch.tmpfs_src or tempfile.gettempdir()
    _stage = tempfile.mkdtemp(dir=launch.tmpfs_src, prefix=launch.src_prefix)
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
                fd, empty_path = tempfile.mkstemp(dir=launch.tmpfs_src, prefix=launch.src_prefix)
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
    ssh_tmp = tempfile.mkdtemp(dir=launch.tmpfs_src, prefix=launch.src_prefix).encode()
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


def scrub_env(launch):
    """Drop every credential variable, then mark the environment as sandboxed.

    The markers are set AFTER the scrub, so a scrubbed prefix cannot delete them.
    """
    environ = launch.environ
    for key in list(environ):
        for prefix in launch.env_prefixes:
            if key.startswith(prefix):
                del environ[key]
                break
    # Mark the sandboxed tree so an in-sandbox wrap_argv knows OS isolation is already
    # active (a nested unshare is seccomp-denied), and record WHICH tier it was built
    # at, so a passthrough can detect a requested-vs-active tier downgrade.
    environ["KIROCREW_SANDBOX_ACTIVE"] = "1"
    environ["KIROCREW_SANDBOX_LEVEL"] = launch.sandbox_level

    # Root-owned files under /etc/ssh/ssh_config.d/ appear as nobody:nobody inside the
    # user namespace because uid 0 is unmapped, and ssh refuses to load them. Bypass
    # them with -F /dev/null.
    if not environ.get("GIT_SSH_COMMAND"):
        environ["GIT_SSH_COMMAND"] = (
            "ssh -F /dev/null -o IdentityFile=~/.ssh/id_rsa"
            " -o IdentityFile=~/.ssh/id_ecdsa"
            " -o IdentityFile=~/.ssh/id_ed25519"
            " -o UserKnownHostsFile=~/.ssh/known_hosts"
            "%s" % launch.strict_host_key_opt
        )

    # Gradle would otherwise leave a daemon running after this sandboxed command exits,
    # holding the mount namespace open with the credential paths still masked, plus the
    # inherited seccomp filter and emptied capability bounding set. Nothing here changes
    # what Gradle keys its daemon context on, so a later build OUTSIDE the sandbox would
    # adopt that daemon, silently running under restrictions and a credential view it
    # never asked for. Keyed on the EFFECTIVE LAST -Dorg.gradle.daemon= directive rather
    # than on mere presence, because duplicate -D resolves last-wins: a trailing =true
    # would otherwise survive, while appending when ours is already last just duplicates.
    if [
        _t for _t in environ.get("GRADLE_OPTS", "").split() if _t.startswith("-Dorg.gradle.daemon=")
    ][-1:] != ["-Dorg.gradle.daemon=false"]:
        environ["GRADLE_OPTS"] = (
            environ.get("GRADLE_OPTS", "") + " -Dorg.gradle.daemon=false"
        ).strip()


def drop_privileges(launch):
    """Drop every capability from the bounding set and set NO_NEW_PRIVS.

    Inside the user namespace the child holds CAP_SYS_ADMIN (it owns the namespace),
    which would let it umount the credential bind-mounts. Without prctl(2) neither this
    nor the seccomp filter can be applied, so the spawn is refused.
    """
    libc = launch.libc
    _PR_SET_NO_NEW_PRIVS = 38
    _PR_CAPBSET_DROP = 24
    if not libc.prctl:
        sys.exit(
            "sandbox: BLOCKED — libc exposes no prctl(2), so neither the "
            "capability-bounding drop nor the seccomp-BPF namespace-escape "
            "filter can be applied. The agent would keep CAP_SYS_ADMIN in "
            "this mount namespace and could unmount the credential masks, "
            "so this spawn is refused. To run anyway WITHOUT OS-level "
            "isolation, set agent.sandbox='off' or "
            "agent.sandbox_allow_unsandboxed_exec=true in "
            "~/.kiro/crew/config.json."
        )
    # CAP_LAST_CAP is 40 on 6.x kernels; 0..63 covers what may come -- dropping a cap that
    # does not exist just returns -1.
    for _cap in range(64):
        libc.prctl(_PR_CAPBSET_DROP, _cap, 0, 0, 0)
    # NO_NEW_PRIVS stops regaining caps through a setuid/setcap binary. Load-bearing
    # beyond hardening: the mount-source sweep's directory gate
    # (_cleanup_stale_sandbox_mount_sources) reclaims on the claim that every launcher
    # descendant still stats as this uid (or the overflow uid from a nested userns), so a
    # change here that lets a descendant change uid would turn that gate fail-open.
    _ret = libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if _ret != 0:
        sys.exit("sandbox: BLOCKED — failed to set NO_NEW_PRIVS (prctl returned %d)" % _ret)


def seccomp_deny_table(machine):
    """``(denied syscalls, kill syscall)`` for *machine*, or ``None`` with no table for it.

    The denied calls are mount, umount2, unshare, setns and pivot_root: the ones that
    would let the sandboxed process undo the credential bind-mounts.
    """
    if machine == "x86_64":
        return (165, 166, 272, 308, 155), 62
    if machine == "aarch64":
        return (40, 39, 97, 268, 41), 129
    return None


def seccomp_program(machine):
    """The seccomp-BPF program, one packed instruction per entry, for *machine*.

    Layout (indices relative to start):

    * 0: LD arch; 1: JEQ expected_arch ? skip 1 : fall through; 2: RET KILL (an
      unexpected arch -- blocks the i386 ``int 0x80`` bypass);
    * 3: LD syscall nr; 4..4+n-1: JEQ deny_i -> DENY;
    * k = 4+n: JEQ kill_nr ? fall into the arg check : jump ALLOW;
    * k+1: LD args[0] low 32 bits (seccomp_data offset 16);
      k+2: JEQ 0xFFFFFFFF ? DENY : fall through;
    * ALLOW = k+3: RET ALLOW; DENY = k+4: RET ERRNO|EPERM.

    Only the LOW 32 bits of args[0] are inspected. pid_t is a 32-bit int: the kernel
    truncates the register to 32 bits, so low==0xFFFFFFFF is exactly "pid == -1"
    regardless of what the upper half holds. The upper half MUST NOT be matched -- the
    x86-64 ABI leaves it undefined for int arguments, and glibc's ``movl`` zero-extends,
    so kill(-1) typically arrives as 0x00000000_FFFFFFFF (a high==0xFFFFFFFF check
    silently never fires, which is a filter bypass, not a compat issue).
    """
    _SECCOMP_RET_ALLOW = 0x7FFF0000
    _SECCOMP_RET_ERRNO = 0x00050000
    _EPERM = 1
    _BPF_LD = 0x00
    _BPF_W = 0x00
    _BPF_ABS = 0x20
    _BPF_JMP = 0x05
    _BPF_JEQ = 0x10
    _BPF_K = 0x00
    _BPF_RET = 0x06
    _AUDIT_ARCH_X86_64 = 0xC000003E
    _AUDIT_ARCH_AARCH64 = 0xC00000B7
    _SECCOMP_RET_KILL = 0x00000000
    _table = seccomp_deny_table(machine)
    if _table is None:
        raise ValueError("no seccomp syscall table for machine %r" % machine)
    _deny_syscalls, _kill_nr = _table
    _expected_arch = _AUDIT_ARCH_X86_64 if machine == "x86_64" else _AUDIT_ARCH_AARCH64
    _insns = []
    # Load arch: BPF_LD | BPF_W | BPF_ABS, offset=4 (seccomp_data.arch)
    _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 4))
    # If arch == expected, skip next insn (jt=1); else fall through to kill
    _insns.append(_struct.pack("<HBBI", _BPF_JMP | _BPF_JEQ | _BPF_K, 1, 0, _expected_arch))
    _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_KILL))
    # Load syscall number: BPF_LD | BPF_W | BPF_ABS, offset=0
    _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 0))
    # For each denied syscall: JEQ -> DENY (at index k+4)
    _n_deny = len(_deny_syscalls)
    for _i, _nr in enumerate(_deny_syscalls):
        _jt = (_n_deny - _i - 1) + 4  # jumps to the DENY RET at k+4
        _insns.append(_struct.pack("<HBBI", _BPF_JMP | _BPF_JEQ | _BPF_K, _jt, 0, _nr))
    # k: nr == kill ? fall into arg check : jump to ALLOW (k+3)
    _insns.append(_struct.pack("<HBBI", _BPF_JMP | _BPF_JEQ | _BPF_K, 0, 2, _kill_nr))
    # k+1: load args[0] low word (offset 16, little-endian layout)
    _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 16))
    # k+2: low == 0xFFFFFFFF (pid -1) ? DENY (skip 1) : fall to ALLOW
    _insns.append(_struct.pack("<HBBI", _BPF_JMP | _BPF_JEQ | _BPF_K, 1, 0, 0xFFFFFFFF))
    _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_ALLOW))
    _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_ERRNO | _EPERM))
    return _insns


def install_seccomp(launch):
    """Install the seccomp-BPF filter that keeps the agent inside its namespace.

    Denies mount/umount2/unshare/setns/pivot_root so the sandboxed process cannot undo
    the credential bind-mounts (namespace escape).

    link/linkat are NOT denied: a blanket syscall ban broke npm cacache / pnpm / ln for
    no gain. Masking is per-level: strict bind-masks its dir/file list PLUS ~/.ssh; cc
    masks the same MINUS ~/.ssh; standard masks only its standard dirs. For a file that
    IS masked the credential inode has no reachable path, so no link source exists. For
    a file left UNMASKED at a given level (~/.ssh under cc; .aws/.ssh and the cc files
    under standard) there is no privilege delta: it is already directly readable, and no
    command-text matcher stands in for the mask -- security.is_sensitive_bash_command
    matches no paths, so a read, a hardlink and a copy of an unmasked store are all
    equally unrefused. That visibility is the tier's own trade (kiro-cli resolves its
    credentials from these stores), stated in the security spec rather than hidden
    behind a regex that refused one spelling and passed the next. seccomp cannot
    path-scope link (BPF cannot dereference the pathname pointer), so a syscall-layer
    form could only be all-or-nothing. The pre-exec nlink scan is NOT relied on here --
    it stats paths AFTER the masks, so it sees mask inodes, not real credential inodes;
    a hardlink alias is durable and symlink-resolution-invisible.

    Additionally denies kill(-1, sig) -- the signal BROADCAST that reaches every
    same-uid process on the host (gateway, other sessions). A static arg filter blocks
    the hand-slip / runaway-script broadcast without changing the subtree's view of
    pids, so session identity, claim-push, and systemd stay intact. Only ``kill`` needs
    arg inspection: tkill/tgkill/pidfd_send_signal are inherently targeted. pid==0 and
    negative process-group targets stay ALLOWED on purpose, because denying killpg
    breaks legitimate tooling (timeout(1), shell job control, cleanup traps).

    What this filter denies is exactly one thing: the ``kill(-1, sig)`` host-wide
    broadcast. A NAMED negative target -- ``kill(-<pgid>, sig)`` for a process group
    outside the spawn -- is not denied at the syscall layer, and the subtree shares the
    host pid namespace (no CLONE_NEWPID here, by the same deliberate choice), so such a
    signal is same-uid permitted and lands outside the spawn's own tree. setsid() places
    the spawn in its own group; it does not restrict which groups the spawn may signal.

    Session isolation is therefore not a property of this filter at all. It is also not
    expressible here: one agent RUNTIME can serve several sessions at once (a parent
    plus the subagents whose sessions are created on its runtime), so the narrower rule
    "deny group targets while more than one session is being served" would need a
    session count, and this is a static BPF program installed before the first session
    is claimed -- it cannot read that count, which changes after the filter is sealed.
    The containable form of the problem lives one layer out, where a signal is matched
    against the runtime's own session set, so the ownership model is the thing that has
    to answer it.
    """
    libc = launch.libc
    if not libc.prctl:
        return
    _PR_SET_SECCOMP = 22
    _SECCOMP_MODE_FILTER = 2
    _machine = _plat.machine()
    if seccomp_deny_table(_machine) is None:
        # No syscall table for this arch, so the filter that keeps the child from undoing
        # the credential masks cannot be built. Refuse rather than skip: with unshare(2)
        # still permitted the child can enter a nested user namespace, hold CAP_SYS_ADMIN
        # over a copy of this mount tree, and umount every mask -- the exact escape this
        # step exists to deny. _inside_kirocrew_sandbox() and the security spec both
        # state that a sandboxed tree is confined "by the outer namespace + seccomp", so
        # a silent skip makes that claim false while every caller still reads the spawn
        # as isolated. sandbox_level="off" (or agent.sandbox_allow_unsandboxed_exec) is
        # the explicit opt-out for a host that cannot be confined; a silent one is not.
        sys.exit(
            "sandbox: BLOCKED — no seccomp syscall table for machine "
            "%r, so the namespace-escape filter (mount/umount2/unshare/"
            "setns/pivot_root) cannot be installed. The agent would run "
            "able to unshare a new namespace and unmount the credential "
            "masks, so this spawn is refused. Supported: x86_64, "
            "aarch64. To run anyway WITHOUT OS-level isolation, set "
            "agent.sandbox='off' or "
            "agent.sandbox_allow_unsandboxed_exec=true in "
            "~/.kiro/crew/config.json." % _machine
        )
    _insns = seccomp_program(_machine)
    _prog_bytes = b"".join(_insns)

    class _SockFprog(ctypes.Structure):
        # struct sock_fprog { unsigned short len; struct sock_filter *filter; }
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_char_p)]

    _fprog = _SockFprog()
    _fprog.len = len(_insns)
    _fprog.filter = _prog_bytes
    _ret = libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.addressof(_fprog), 0, 0)
    if _ret != 0:
        sys.exit(
            "sandbox: BLOCKED — failed to install seccomp-BPF filter (prctl returned %d)" % _ret
        )


def refuse_hardlinked_credentials(launch, scan_roots=None, max_per_root=100000):
    """Refuse to exec when a hardlink outside the masks aliases a protected credential.

    Scans the agent workspace + /tmp (*scan_roots*, default the cwd and ``/tmp``, each
    with a budget of *max_per_root* files) for
    hardlinks (nlink > 1) whose inode matches a protected credential file. Only
    credential inodes with st_nlink > 1 enter the match set: an inode with a single link
    has no alias anywhere on the filesystem, so when every credential has nlink == 1 the
    walk is skipped entirely and the common healthy-host spawn pays nothing. When a walk
    does run, each root gets its OWN scan budget so a large workspace cannot starve the
    /tmp scan (the world-writable root this check exists for). On budget exhaustion the
    scan deliberately degrades OPEN with a stderr warning rather than failing closed:
    /tmp on a busy host can exceed any fixed budget from ordinary telemetry/cache churn,
    and exiting here would break every sandbox spawn on such hosts. The cost, plainly: an
    alias past the budget -- or past the quieter depth limit below -- is never stat'd,
    so a second path to a credential inode goes unchecked even though every mount held.

    REGULAR FILES ONLY, and that guard is what keeps the walk rare. Linux does not allow
    a hardlink to a directory, so nlink > 1 says nothing about a directory -- and every
    directory has nlink >= 2 for `.` and `..`. ``sensitive_files`` deliberately carries
    every hidden path of BOTH kinds (the hiding stages classify per entry), so without
    this check two ordinary directories -- `~/.kiro/crew-auth-staging` and `~/.gnupg` on
    the measuring host -- seeded the match set on every spawn. The 100k-entry walk of
    $CWD and /tmp then ran every time, costing 1.5s per sandboxed spawn and emitting the
    truncation warning constantly, while no credential had an alias at all.
    """
    _protected_inodes = set()
    for _pd in launch.sensitive_dirs:
        if os.path.isdir(_pd):
            for _root, _dirs_scan, _files_scan in os.walk(_pd):
                for _fname in _files_scan:
                    try:
                        _st = os.stat(os.path.join(_root, _fname))
                        if stat.S_ISREG(_st.st_mode) and _st.st_nlink > 1:
                            _protected_inodes.add((_st.st_dev, _st.st_ino))
                    except OSError:
                        pass
                break  # depth=1 for credential dirs
    for _pf in launch.sensitive_files:
        try:
            _st = os.stat(_pf)
            if stat.S_ISREG(_st.st_mode) and _st.st_nlink > 1:
                _protected_inodes.add((_st.st_dev, _st.st_ino))
        except OSError:
            pass
    # The per-app credentials, as INODES the parent read. Not a scan: this same process
    # masked that tree above, binding an empty directory over it, so a stat here would
    # report ENOENT and arm the walk on nothing. The parent collected these while the
    # paths were still readable, which is also why no bound or name filter is needed --
    # the set is already bounded and screened, not agent-writable data any more.
    for _acid in launch.alias_credential_ids:
        _protected_inodes.add((_acid[0], _acid[1]))
    if not _protected_inodes:
        return
    _dangerous_links = []
    _truncated_roots = []
    for _scan_root in (os.getcwd(), "/tmp") if scan_roots is None else scan_roots:
        if not os.path.isdir(_scan_root):
            continue
        _root_scanned = 0
        _root_truncated = False
        for _root2, _dirs2, _files2 in os.walk(_scan_root):
            # Depth limit: max 5 levels
            _depth = _root2[len(_scan_root) :].count(os.sep)
            if _depth > 5:
                _dirs2.clear()
                continue
            for _fn2 in _files2:
                if _root_scanned >= max_per_root:
                    _root_truncated = True
                    break
                _root_scanned += 1
                _fp2 = os.path.join(_root2, _fn2)
                try:
                    _st2 = os.lstat(_fp2)
                    if _st2.st_nlink > 1:
                        if (_st2.st_dev, _st2.st_ino) in _protected_inodes:
                            _dangerous_links.append(_fp2)
                except OSError:
                    pass
            if _root_truncated:
                break
        if _root_truncated:
            _truncated_roots.append((_scan_root, _root_scanned))
    for _t_root, _t_count in _truncated_roots:
        print(
            "sandbox: WARNING — pre-exec hardlink scan truncated at "
            "%d files in %s; scan incomplete (control degrades open)" % (_t_count, _t_root),
            file=sys.stderr,
        )
    if _dangerous_links:
        sys.exit(
            f"sandbox: BLOCKED — found hardlink(s) to protected credential "
            f"inodes: {_dangerous_links[:5]}. Remove them before running."
        )


def exec_agent(launch, argv):
    """Replace this process with the agent command, in the scrubbed environment."""
    if launch.execvp is None:
        os.execvp(argv[0], argv)
    else:
        launch.execvp(argv[0], argv)


def place_masks(launch):
    """Place every mount, in the order the kernel's mount semantics require.

    Windows are staged before anything is masked, read-only seals land before the hides
    placed on top of them, carve-outs after the seals they punch through, the exposed
    files are restored into their empty masks, and the single files and ``~/.ssh`` are
    masked last.
    """
    stage_private_windows(launch)
    seal_readonly(launch)
    mask_sensitive(launch)
    apply_carveouts(launch)
    restore_exposed_files(launch)
    verify_fail_closed_aliases(launch)
    mask_sensitive_files(launch)
    mask_ssh_keys(launch)


def run_child(launch, argv):
    """Build the sandbox in this (already namespaced) process, then exec *argv*.

    Every mask is placed before the environment is scrubbed and the process confined,
    and the crew-home aliases are checked on both sides of the masks.
    """
    pick_stand_in_root(launch)
    check_crew_home_aliases(launch)
    preread_exposed_files(launch)
    place_masks(launch)
    confirm_crew_home_aliases(launch)
    scrub_env(launch)
    drop_privileges(launch)
    install_seccomp(launch)
    refuse_hardlinked_credentials(launch)
    exec_agent(launch, argv)


def main(plan, libc=None, argv=None):
    """Fork; the parent maps the child's ids and waits, the child builds the sandbox and execs.

    *plan* is this launch's data, *libc* the C library to mount through (the process's
    own when omitted) and *argv* the agent command (``sys.argv[1:]`` when omitted).
    """
    launch = Launch(plan, _load_libc() if libc is None else libc)
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit("sandbox_launcher: no command given")

    # Export this launcher's HOST pid before any fork/namespace work. The gateway
    # records exactly this pid (its direct Popen child) when it writes
    # ``session_pid_<pid>.txt`` on session claim, so in-sandbox identity resolvers can
    # look the file up directly via this env var instead of walking /proc -- which breaks
    # whenever the subtree's view of pids diverges from the host's.
    launch.environ["KIROCREW_HOST_PID"] = str(os.getpid())

    # Two pipes for parent<->child synchronization.
    c2p_r, c2p_w = os.pipe()  # child signals "unshare done"
    p2c_r, p2c_w = os.pipe()  # parent signals "maps written"

    pid = os.fork()

    if pid > 0:
        # Parent: write the identity uid/gid map.
        os.close(c2p_w)
        os.close(p2c_r)
        os.read(c2p_r, 1)  # wait for the child to unshare(NEWUSER)
        with open(f"/proc/{pid}/setgroups", "w") as f:
            f.write("deny")
        with open(f"/proc/{pid}/uid_map", "w") as f:
            f.write(f"{launch.real_uid} {launch.real_uid} 1\n")
        with open(f"/proc/{pid}/gid_map", "w") as f:
            f.write(f"{launch.real_gid} {launch.real_gid} 1\n")
        os.write(p2c_w, b"x")  # signal the child to proceed
        os.close(c2p_r)
        os.close(p2c_w)
        _, status = os.waitpid(pid, 0)
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        sys.exit(code)
    # Child: unshare, wait for the maps, mount, exec.
    os.close(c2p_r)
    os.close(p2c_w)
    enter_namespaces(launch, c2p_w, p2c_r)
    run_child(launch, argv)


_PLAN = None  # the renderer substitutes the plan here

if __name__ == "__main__":
    main(_PLAN)
