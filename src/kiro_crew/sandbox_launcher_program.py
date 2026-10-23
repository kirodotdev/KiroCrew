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
_EACCES = __import__("errno").EACCES
_ENOENT = __import__("errno").ENOENT
_EROFS = __import__("errno").EROFS

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
        self.readonly_dirs = plan["readonly_dirs"]
        self.writable_dirs = plan["writable_dirs"]
        self.sensitive_files = plan["sensitive_files"]
        self.fail_closed_file_masks = plan["fail_closed_file_masks"]
        self.alias_credential_ids = plan["alias_credential_ids"]
        #: The directory to enter after the seals and before exec, or "" for none.
        self.exec_cwd = plan.get("exec_cwd", "")
        self.required_mask_targets = frozenset(plan["required_mask_targets"])
        #: The masked directories that must be SEALED read-only when they carry no
        #: writable window: the caller-supplied masks (``extra_hidden_dirs`` -- an app
        #: bundle's ``data/``, a cron naming another app's tree) where a lost write is
        #: real data loss. The host vendor credential dirs (``.aws``, ``.docker``,
        #: ``.config/gcloud`` ...) are deliberately NOT here: their CLIs write ephemeral
        #: state into the empty stand-in on every run, so sealing them would break the
        #: tool rather than protect data. Absent in an older payload -> seal nothing,
        #: which is exactly main's behaviour before this control.
        self.seal_windowless_masks = frozenset(plan.get("seal_windowless_masks", ()))
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


# -- the launcher program continues in sandbox_launcher_parts/pinned_mounts.py --


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
    root = _first_stand_in_root(launch)
    if root is not None:
        launch.tmpfs_src = root
    launch.src_prefix = "kirocrew_sb_%d_" % os.getpid()


def _first_stand_in_root(launch, skip=None):
    """The first of ``launch.stand_in_roots`` other than *skip* that passes both checks.

    A root qualifies when it sits on a different filesystem from HOME and a probe
    directory can be created and removed in it. ``None`` when no candidate qualifies.
    """
    home_dev = _home_device(launch.environ)
    for candidate in launch.stand_in_roots:
        if candidate == skip:
            continue
        try:
            if home_dev is not None and os.stat(candidate).st_dev == home_dev:
                continue  # same fs as HOME -- no isolation, the race is still possible
            probe = tempfile.mkdtemp(dir=candidate, prefix="kirocrew_sbprobe_")
            try:
                os.rmdir(probe)
            except FileNotFoundError:
                pass  # an external cleaner won the race -- the root still works
            return candidate
        except (OSError, ValueError):
            continue
    return None


#: What a create under the chosen stand-in root fails with once a different directory
#: stands at that name than the one the probe saw. systemd-logind removes
#: ``/run/user/$UID`` at a user's last logout and makes it again, ``root:root`` mode
#: 0700, at the next login; removing a mount point takes the mount out of this private
#: namespace as well, so the name then reads as that bare directory (EACCES), as
#: nothing (ENOENT), or as a read-only directory (EROFS).
_STAND_IN_ROOT_GONE = frozenset({_EACCES, _ENOENT, _EROFS})


def _on_stand_in_root(launch, create):
    """``create(root)`` under the stand-in root, moved to another root once if that one is gone.

    When the chosen root refuses the create with an errno in ``_STAND_IN_ROOT_GONE``, the
    remaining candidates are probed with the same checks :func:`pick_stand_in_root`
    applies, the first that qualifies becomes the stand-in root for this and every later
    create, and *create* runs once more there. With no qualifying candidate the original
    error propagates; when the second create fails too, that second error propagates.
    Either way the launch ends as it would have without the retry. Only where the
    stand-in lives changes: every mask is still placed, in the same order, over a
    stand-in this launcher created.
    """
    try:
        return create(launch.tmpfs_src)
    except OSError as exc:
        failed = launch.tmpfs_src
        if failed is None or exc.errno not in _STAND_IN_ROOT_GONE:
            raise
        replacement = _first_stand_in_root(launch, skip=failed)
        if replacement is None:
            raise
        sys.stderr.write(
            "sandbox: WARNING -- the stand-in root %s refused a create (errno %s); "
            "using %s instead.\n" % (failed, exc.errno, replacement)
        )
        launch.tmpfs_src = replacement
    return create(launch.tmpfs_src)


def _stand_in_mkdtemp(launch):
    """A fresh, empty stand-in directory under the stand-in root, by its path."""
    return _on_stand_in_root(
        launch, lambda root: tempfile.mkdtemp(dir=root, prefix=launch.src_prefix)
    )


def _stand_in_mkstemp(launch):
    """``(fd, path)`` of a fresh, empty stand-in file under the stand-in root."""
    return _on_stand_in_root(
        launch, lambda root: tempfile.mkstemp(dir=root, prefix=launch.src_prefix)
    )


# -- the launcher program continues in sandbox_launcher_parts/mask_stages.py --


# -- the launcher program continues in sandbox_launcher_parts/exec_lockdown.py --


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
    enter_exec_cwd(launch)
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
# -- the launcher program ends above this line --
# Imported as ``kiro_crew.sandbox_launcher_program``, this module binds every definition its
# parts hold and runs their functions on its own globals, so it stays the one namespace the
# rendered program is and a patch of a name here reaches every stage that reads it; see
# ``kiro_crew.sandbox_launcher_parts``. The renderer drops these lines.
from kiro_crew._facade_compose import compose as _facade_compose  # noqa: E402
from kiro_crew.sandbox_launcher_parts.exec_lockdown import (  # noqa: E402
    drop_privileges,
    enter_exec_cwd,
    exec_agent,
    install_seccomp,
    refuse_hardlinked_credentials,
    scrub_env,
    seccomp_deny_table,
    seccomp_program,
)
from kiro_crew.sandbox_launcher_parts.mask_stages import (  # noqa: E402
    _mount_private_tmpfs,
    _open_unreadable_stand_in,
    _pin_below_mask,
    _retire_unreadable_stage,
    _seal_masked_dir_if_windowless,
    _stage_is_fresh_mount,
    apply_carveouts,
    check_crew_home_aliases,
    confirm_crew_home_aliases,
    mask_sensitive,
    mask_sensitive_files,
    mask_ssh_keys,
    preread_exposed_files,
    restore_exposed_files,
    seal_readonly,
    stage_private_windows,
    verify_fail_closed_aliases,
)
from kiro_crew.sandbox_launcher_parts.pinned_mounts import (  # noqa: E402
    _any_kind,
    _carried_occupant,
    _covered_by_own_mask,
    _kind_reached,
    _locked_mount_flags,
    _mask_required,
    _mode_is_link,
    _mount_or_die,
    _mount_or_warn,
    _pin_mount_path,
    _register_stand_in,
    _retire_stage_or_die,
    _stand_in_identity,
    _verify_masked_name,
)

#: The composed parts, in the order their text runs in the program.
_PARTS = (
    "kiro_crew.sandbox_launcher_parts.pinned_mounts",
    "kiro_crew.sandbox_launcher_parts.mask_stages",
    "kiro_crew.sandbox_launcher_parts.exec_lockdown",
)
_facade_compose(globals(), [sys.modules[part] for part in _PARTS])
