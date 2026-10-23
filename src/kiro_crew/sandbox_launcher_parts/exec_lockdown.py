"""What the namespace launcher does once every mask is placed, up to the agent's exec.

One of the launcher program's parts (see :mod:`kiro_crew.sandbox_launcher_parts`). Below its
marker line this file is the program's text from ``scrub_env`` to ``exec_agent``: the
environment scrub, the capability drop and ``NO_NEW_PRIVS``, the seccomp-BPF filter and its
syscall tables, the pre-exec hardlink scan, entering the launch directory, and the exec itself.
A new restriction applied after the masks goes here.
"""

import ctypes
import os
import platform as _plat
import stat
import struct as _struct
import sys


# -- the launcher program continues below this line --
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


def enter_exec_cwd(launch):
    """Enter the plan's ``exec_cwd`` by its name, so the agent starts in it through the seal.

    The process inherits its working directory from before the namespace existed, so a
    directory it already stands in keeps the writable mount underneath any seal placed
    since. Entering by name AFTER the seals resolves through them. The directory must
    then report read-only and hold nothing, or the launch ends here: an agent started in
    a writable or populated directory could be handed a spec that directory holds.
    Runs after the hardlinked-credential scan, which walks the workspace the launcher
    was started in.
    """
    target = launch.exec_cwd
    if not target:
        return
    try:
        os.chdir(target)
        read_only = bool(os.statvfs(".").f_flag & os.ST_RDONLY)
        entries = os.listdir(".")
    except OSError as exc:
        sys.exit("sandbox: FATAL — cannot enter the launch directory (%s)" % (exc.strerror or exc))
    if not read_only:
        sys.exit("sandbox: FATAL — the launch directory is not sealed read-only")
    if entries:
        sys.exit("sandbox: FATAL — the launch directory is not empty")


def exec_agent(launch, argv):
    """Replace this process with the agent command, in the scrubbed environment.

    A command the kernel will not exec ends the launcher the way its other stops do:
    one FATAL line naming the command and the reason, then exit 1. The exception's
    own text names no path, and a traceback puts it last, where a caller that keeps
    the head of stderr cuts it off.
    """
    try:
        if launch.execvp is None:
            os.execvp(argv[0], argv)
        else:
            launch.execvp(argv[0], argv)
    except OSError as exc:
        # Unprintable characters go out as escapes: a newline in the path would start a
        # second line that a reader of launcher stderr could take for the launcher's own.
        command, reason = (
            "".join(c if c.isprintable() else ascii(c)[1:-1] for c in str(part))
            for part in (argv[0], exc.strerror or exc)
        )
        sys.exit("sandbox: FATAL — cannot exec %s (%s)" % (command, reason))
