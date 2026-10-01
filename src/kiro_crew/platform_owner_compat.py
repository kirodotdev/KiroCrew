"""Who owns a file, and making it reachable by its owner alone, on POSIX and Windows.

Reads who an account is -- the invoking account's uid on POSIX and SID from its own
access token on Windows (:func:`local_user_id`, :func:`current_user_sid`), and on
Windows the SID owning another process (:func:`process_owner_sid`) -- decides whether
the invoking account could replace a file (:func:`stat_writable_by_current_user`,
:func:`path_writable_by_current_user`), and makes a file or directory owner-only:
``0o600`` / ``0o700`` on POSIX, a protected owner-only DACL on Windows
(:func:`restrict_to_owner`, :func:`restrict_dir_to_owner`, :func:`make_owner_only_dir`).

``kiro_crew.platform_compat`` re-exports every name here, and a patch through it
lands here.
"""

from __future__ import annotations

import logging
import os
import stat
import zlib
from ctypes import wintypes
from pathlib import Path

from kiro_crew import windows_acl

# The helpers read the platform flag (``IS_POSIX``), ``ctypes``, the Win32 struct
# layouts and two shared constants from ``kiro_crew.platform_compat``, imported inside
# the function that uses them, so a test that rebinds one of those there reaches the
# helper at call time. Circular import: ``kiro_crew.platform_compat`` imports this
# module while it loads.

# Logged under the compatibility layer's name, which operator log filters and level
# settings key on.
logger = logging.getLogger("kiro_crew.platform_compat")

# Well-known SID for the file's *owner* (implicit). Under a self-relative DACL
# with inheritance stripped, S-1-3-4 grants access to whoever currently owns
# the file. See:
# https://learn.microsoft.com/en-us/windows/win32/secauthz/well-known-sids
_OWNER_RIGHTS_SID = "S-1-3-4"


_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1  # TOKEN_INFORMATION_CLASS.TokenUser


def _process_token_sid() -> str | None:
    """The invoking user's SID read from this process's own access token.

    Preferred over ``whoami`` because it spawns nothing: the SID is already in
    the process token, so this cannot time out under load, cannot be defeated
    by a stripped PATH or a locked-down host, and is safe to call on the event
    loop. Returns ``None`` on any failure so the caller can fall back.
    """
    from kiro_crew.platform_compat import IS_POSIX

    if IS_POSIX:
        return None
    try:
        return _process_token_sid_unguarded()
    except Exception:  # noqa: BLE001 - best-effort: the caller falls back
        logger.debug("_process_token_sid failed", exc_info=True)
        return None


def _process_token_sid_unguarded(pid: int | None = None) -> str | None:
    """Body of :func:`_process_token_sid`; may raise.

    ``pid`` selects whose token to read: ``None`` means this process (via the
    ``GetCurrentProcess`` pseudo-handle), any other value opens that process
    with ``PROCESS_QUERY_LIMITED_INFORMATION`` -- the least right that still
    permits ``OpenProcessToken``, and one a user always holds over their own
    processes without elevation.
    """
    from kiro_crew.platform_compat import _PROCESS_QUERY_LIMITED_INFORMATION, _TokenUser, ctypes

    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        # AttributeError: ctypes has no WinDLL off Windows. Reachable because
        # tests exercise the Windows branch from Linux by patching IS_POSIX.
        return None

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    # Every prototype is declared and every argument below is passed as a
    # ctypes instance rather than a Python int. Leaving either to the default
    # lets ctypes convert a pointer-sized value through a C int, which either
    # truncates it silently or raises OverflowError depending on the call --
    # both observed on Windows, neither reproducible on Linux.
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    # Hand the pseudo-handle over as a HANDLE instance rather than the int
    # ctypes hands back for a c_void_p restype: converting that int through the
    # declared argtype raises OverflowError on Windows because the value is
    # pointer-sized and unsigned.
    #
    # own_handle tracks whether this is a real handle we must close. The
    # GetCurrentProcess pseudo-handle must NOT be closed.
    own_handle = pid is not None
    if pid is None:
        process = wintypes.HANDLE(kernel32.GetCurrentProcess())
    else:
        process = wintypes.HANDLE(
            kernel32.OpenProcess(
                wintypes.DWORD(_PROCESS_QUERY_LIMITED_INFORMATION),
                wintypes.BOOL(False),
                wintypes.DWORD(int(pid)),
            )
        )
        if not process.value:
            return None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            process, wintypes.DWORD(_TOKEN_QUERY), ctypes.byref(token)
        ):
            return None
        try:
            size = wintypes.DWORD()
            # First call sizes the buffer and is expected to fail.
            advapi32.GetTokenInformation(
                token,
                ctypes.c_int(_TOKEN_USER_CLASS),
                None,
                wintypes.DWORD(0),
                ctypes.byref(size),
            )
            if size.value == 0:
                return None
            buf = (ctypes.c_byte * size.value)()
            if not advapi32.GetTokenInformation(
                token,
                ctypes.c_int(_TOKEN_USER_CLASS),
                ctypes.cast(buf, ctypes.c_void_p),
                size,
                ctypes.byref(size),
            ):
                return None
            user = ctypes.cast(buf, ctypes.POINTER(_TokenUser)).contents
            out = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(
                ctypes.c_void_p(user.User.Sid), ctypes.byref(out)
            ):
                return None
            try:
                sid = out.value
            finally:
                kernel32.LocalFree(out)
        finally:
            kernel32.CloseHandle(token)
    finally:
        if own_handle:
            kernel32.CloseHandle(process)
    if not sid or not sid.startswith("S-1-"):
        return None
    return sid


def process_owner_sid(pid: int) -> str | None:
    """The SID of the user owning *pid*, as a string, or ``None``.

    Windows' answer to :func:`process_owner_uid`, which returns ``None`` there
    because Windows has no uid. Reads the target process's access token
    directly -- no subprocess, no WMI round trip -- so it is safe to call on the
    event loop.

    This is what lets a Windows peer-principal check work at *connect* time.
    The obvious alternative, ``ImpersonateNamedPipeClient``, cannot:
    per Microsoft's documentation it impersonates "the security context of the
    last message read from the pipe", so before the first read there is no
    context to adopt and the call fails (or yields an anonymous token that
    ``OpenThreadToken`` then refuses). Reading the peer process's own token has
    no such ordering requirement, and it never borrows the peer's token onto one
    of our threads.

    PID reuse is not exploitable here. The window between learning the peer PID
    and opening it is tiny, and either outcome is safe: if the PID has been
    recycled to a process owned by *another* user the comparison reports a
    mismatch and the caller denies; if it was recycled to another process owned
    by *us* then the principal genuinely is us, which is the only question this
    function answers. Ownership -- not process identity -- is the assertion.

    Returns ``None`` on POSIX and on any failure, so callers must treat ``None``
    as "unverifiable" rather than as a match.
    """
    from kiro_crew.platform_compat import IS_POSIX

    if IS_POSIX:
        return None
    try:
        return _process_token_sid_unguarded(int(pid))
    except Exception:  # noqa: BLE001 - best-effort: the caller fails closed
        logger.debug("process_owner_sid(%s) failed", pid, exc_info=True)
        return None


#: Memo for :func:`current_user_sid`. Only ever holds a token-derived bare SID.
_TOKEN_SID_CACHE: list[str] = []


def current_user_sid() -> str | None:
    """Return the invoking user's bare SID (``S-1-5-...``), or ``None``.

    Read from this process's own access token and nothing else -- there is no
    subprocess fallback anywhere in this path. Every caller runs on the event
    loop: the gatewayd admission check, the client-side server check, the pipe
    DACL builder (once per pipe instance, so on the accept path), and now the
    owner-only lockdown itself. A ``whoami`` fallback would stall any of them for
    seconds at a time on a host where the token read fails, which is why this
    function refuses instead.

    Failing closed is correct for every caller: each treats ``None`` as
    "principal unverifiable" and refuses the connection, which degrades to a
    per-session MCP server rather than admitting an unattributable peer.
    :func:`restrict_to_owner` likewise raises rather than applying a
    half-configured DACL.

    SDDL and the Win32 security APIs want the bare SID, which is what this
    returns. Memoised: the SID is constant for the process lifetime and this is
    on a hot path. Returns ``None`` on POSIX and on any lookup failure.
    """
    if _TOKEN_SID_CACHE:
        return _TOKEN_SID_CACHE[0]
    raw = _process_token_sid()
    if not raw:
        return None
    sid = raw.lstrip("*") or None
    if sid:
        _TOKEN_SID_CACHE.append(sid)
    return sid


def make_owner_only_dir(path: str | os.PathLike) -> None:
    """Create *path* (with parents) and make it readable only by this user.

    ``mkdir(mode=...)`` alone is not enough on either platform: POSIX masks the
    mode with the umask and ignores it entirely for a directory that already
    exists, and Windows derives access from the DACL rather than the mode bits,
    so the mode argument is inert there. Both cases matter for the same reason --
    a directory created before the owner-only guarantee existed, or created on
    Windows at all, would silently stay readable.

    ``0o700`` and not :func:`restrict_to_owner` on POSIX: that helper applies
    ``0o600``, correct for a secret-bearing file and wrong for a directory,
    which needs the execute bit to be traversable at all. On Windows the split
    is the inverse of inert: ``restrict_to_owner``'s grants are not inheritable
    (correct for a file, where the flags mean nothing), so routing a directory
    through it left every file created inside on the creating token's default
    DACL. Both platforms therefore go through
    :func:`restrict_dir_to_owner`, the directory-shaped twin.

    Only newly created children are covered. A file that already exists inside
    the directory keeps its own DACL — see :func:`restrict_dir_to_owner` for
    why a tightened parent does not fix one.

    Best-effort on the tightening step: the directory is still created, and the
    caller decides whether an un-tightened directory is fatal.
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        restrict_dir_to_owner(p)
    except OSError:
        logger.warning("could not restrict directory %s to owner-only", p, exc_info=True)


def local_user_id() -> int:
    """A stable integer identifying the invoking user on this host.

    POSIX: ``os.getuid()``. Windows has no uid, so this is a CRC-32 of the
    user's SID -- an arbitrary but stable and collision-resistant-enough
    integer for the one thing the value is used for: partitioning a cache or a
    pool so two different users can never share an entry.

    An integer rather than the SID string because the consumer
    (``mcp_gateway.PoolKey``) type-checks this dimension strictly and refuses to
    coerce -- ``bool("false")`` is ``True`` and ``int()`` on a bool passes
    silently, so a wire value of the wrong type could land a peer in the wrong
    trust partition. Keeping the type identical across platforms keeps that
    check meaningful.

    Returns ``0`` when the SID cannot be resolved. That is a partition
    collapse, not a privilege change: the gateway's endpoint is already
    per-user (owner-only DACL) and its daemon runs per user, so two users
    cannot reach the same pool regardless.
    """
    from kiro_crew.platform_compat import IS_POSIX

    if IS_POSIX:
        return os.getuid()
    sid = current_user_sid()
    if not sid:
        return 0
    return zlib.crc32(sid.encode("utf-8"))


def stat_writable_by_current_user(st: os.stat_result) -> bool:
    """Could a process running as this account write the file *st* describes?

    Answered from the mode bits of an ALREADY-STATTED object rather than a path, so a
    caller that has an open handle gets an answer about the object it actually read
    instead of about whatever the name resolves to a moment later.

    Callers refuse an agent-writable governance source with it
    (``platform/policy_distribution.py``): a distribution source the account Kiro Crew
    runs as can rewrite is one an agent subprocess can rewrite, because it runs as the
    same uid.

    **POSIX only, and off POSIX it answers ``True`` — unknown counts as writable.**
    Windows permissions are an ACL, not three mode triples, so ``st_mode``'s group/other
    bits carry no usable answer there and ``st_uid``/``st_gid`` are synthesised. The
    honest answer is "cannot tell", and for this predicate that has to round to
    ``True``: the caller refuses a writable source, so a ``False`` here does not abstain,
    it ASSERTS the source is safe and admits every Windows ``file://`` source unchecked —
    including one an agent just planted. ``True`` costs a Windows operator the
    ``file://`` channel (``https://`` is unaffected) until the DACL can be read, which
    belongs with the other ``_icacls`` work rather than here. It is also the safe
    direction for a future caller who inverts the test to decide where to WRITE.

    Lives in this module because ``os.getuid`` / ``os.getgroups`` do not exist on
    Windows, and the POSIX shims are this module's job.
    """
    from kiro_crew.platform_compat import IS_POSIX

    if not IS_POSIX:
        return True
    # Root writes anything, whatever the mode says. Checked first because for a
    # privileged process every remaining test below is moot.
    if 0 in (os.getuid(), os.geteuid()):
        return True
    if st.st_mode & stat.S_IWOTH:
        return True
    # OWNERSHIP alone, not the write bit. An owner may `chmod` its own file, so a `0444`
    # file this account owns is one this account can make writable and then rewrite —
    # which is the whole move the threat model describes, since the agent subprocess runs
    # as the same uid. Requiring `S_IWUSR` here accepted exactly the source an agent could
    # take over with one `chmod`. The same reasoning covers a directory in the ancestor
    # walk: owning it means being able to unlink and recreate what is inside.
    #
    # Real AND effective. The kernel checks the effective pair, but this predicate answers
    # "could this account write it", and a process holding a real id can regain it.
    if st.st_uid in (os.getuid(), os.geteuid()):
        return True
    # Group ownership does NOT imply the same power — only the owner and root may chmod —
    # so here the write bit is the question, and `os.getgroups()` is the SUPPLEMENTARY
    # list: POSIX leaves it unspecified whether the effective gid appears in it. It
    # usually does, because `initgroups` puts it there at login, but a process that
    # reached its gid through `setegid`, or one in a container built without that step,
    # has a primary group the supplementary list never mentions. Testing membership alone
    # therefore called a group-writable file we CAN replace safe.
    gids = {os.getgid(), os.getegid(), *os.getgroups()}
    if st.st_mode & stat.S_IWGRP and st.st_gid in gids:
        return True
    return False


def path_writable_by_current_user(path: str | os.PathLike) -> bool:
    """Could this account replace the file at *path* — by any route?

    Checks the file AND every ancestor directory, because file mode alone is the wrong
    question: a ``0444`` file inside a directory this account can write is replaceable
    (unlink and recreate, or rename the parent aside), so an agent could publish a
    read-only file of its own choosing and pass a leaf-only check.

    Walks upward to the root and stops at the first writable component, so the answer is
    "there exists a way in" rather than "the leaf looks fine". Two chains are walked, the path
    as WRITTEN and the path as RESOLVED, because a source reached through a symlink is
    re-pointable by anyone who can write the LINK's parent — which the resolved chain never
    visits. A component that cannot be
    statted is skipped rather than treated as writable: an unreadable ancestor is not
    evidence of write access, and failing closed on it would refuse legitimate sources
    under directories this account cannot enumerate.

    Each component is tested TWICE: against the mode bits, and against the kernel's own
    answer via ``os.access(..., effective_ids=True)``. The second is not redundant, it is
    the only one that sees a **POSIX ACL**. A named-user entry (``user:me:w`` on a file
    owned by someone else) does not appear in ``st_mode`` at all — the group bits show the
    ACL *mask*, not that entry — so a mode-only check reports "not writable" for a source
    this account can in fact rewrite, which is the whole failure this predicate exists to
    catch. ``faccessat(AT_EACCESS)`` evaluates the full ACL, so it answers correctly. The
    two are OR'd because each sees something the other cannot: ``os.access`` answers about
    the *effective* ids only, while the mode check also covers the real pair.

    POSIX only, and ``True`` off POSIX, for the reason
    :func:`stat_writable_by_current_user` gives: a source whose write permissions cannot
    be read is not a source that has been shown to be safe.
    """
    from kiro_crew.platform_compat import _ACCESS_HONOURS_EFFECTIVE_IDS, IS_POSIX

    if not IS_POSIX:
        return True
    # BOTH chains: the path as WRITTEN and the path as RESOLVED. Resolving first and walking
    # only the target was the gap — a source reached through a symlink was judged entirely by
    # the (root-owned, read-only) file at the end of it, while the link itself sat in a
    # directory this account could write. Re-pointing a symlink needs no permission on the
    # link and none on the target: it needs write on the link's PARENT, which only the lexical
    # chain visits. A symlink's own mode bits are meaningless on Linux (0777 and ignored), so
    # nothing is judged by them; what matters is the directories, and both chains contribute
    # some the other does not.
    starts = [Path(path).absolute()]
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = starts[0]
    if resolved != starts[0]:
        starts.append(resolved)
    seen: set[Path] = set()
    for start in starts:
        current = start
        while current not in seen:
            seen.add(current)
            try:
                if stat_writable_by_current_user(os.stat(current)):
                    return True
                if _ACCESS_HONOURS_EFFECTIVE_IDS and os.access(
                    current, os.W_OK, effective_ids=True
                ):
                    return True
            except OSError:
                pass
            if current.parent == current:
                break
            current = current.parent
    return False


def restrict_to_owner(path: str | os.PathLike) -> None:
    """Fail-loud owner-only lockdown of a secret-bearing file.

    POSIX: ``os.chmod(path, 0o600)`` — identical semantics to a raw call,
    including the fail-loud ``OSError`` propagation the security-sensitive
    callers rely on to reach their warn-and-continue handlers.

    Windows: strip inheritance and apply an owner-only DACL in-process via
    :func:`windows_acl.apply_owner_only`. S-1-3-4 (Owner Rights) covers the
    file's current owner; the invoking-user grant covers the caller by explicit
    SID, so a file created by another principal (elevated first-run, backup
    restore, SYSTEM-context service) remains readable by the caller that is
    trying to lock it down — otherwise the current user would be denied their
    own token signing key on the next read and every issued auth cookie /
    refresh token would be invalidated on each restart. When the SID cannot be
    resolved from the process token we raise ``OSError`` BEFORE applying
    anything: an Owner-Rights-only DACL would recreate the exact
    ownership-lockout regression the dual grant exists to prevent, so we
    refuse to apply a half-configured lockdown. Any failure raises
    ``OSError`` so callers hit the same warn-and-continue path they use on
    POSIX.

    A caller that runs INLINE ON THE ASYNCIO EVENT LOOP and so cannot afford the
    unbounded SMB round-trip a DACL write to a UNC or mapped-drive path costs must
    ask :func:`windows_acl.volume_is_local` FIRST and skip the lockdown itself.
    That decision is deliberately not a parameter here: by the time this function
    is reached the caller has already done whatever filesystem work it took to get
    here, so a refusal at this depth would come after the cost it was meant to
    avoid. ``config/loader.py``'s ``write_config_atomically`` is the one such
    caller today.
    """
    from kiro_crew.platform_compat import IS_POSIX

    if IS_POSIX:
        os.chmod(path, 0o600)
        return
    # Misuse guard: this helper is FILE-shaped. Its grants carry no (OI)(CI),
    # so handing it a directory tightens the directory itself and leaves every
    # file created inside on the creating token's default DACL -- the exact
    # defect restrict_dir_to_owner exists to close. Warn rather than raise:
    # the ACE still applies to the named object, so the lockdown is partial
    # rather than absent, and turning a partial protection into a runtime
    # OSError would be the worse outcome. The argv tests cannot see this from
    # the call site, so the check lives here.
    try:
        if Path(path).is_dir():
            # The path is deliberately NOT logged. In this codebase a path can
            # itself be the secret -- mcp_gateway/apps.py notes that its spool
            # FILENAMES are live capability tokens -- so naming it here would be
            # clear-text logging of sensitive information (CodeQL flagged exactly
            # that). logging already records module/function/lineno, which is
            # what locates the offending caller.
            logger.warning(
                "restrict_to_owner was called on a directory; its grants are not "
                "inheritable, so files created inside will not be owner-only. "
                "Use restrict_dir_to_owner for a directory."
            )
    except OSError:
        pass
    _apply_owner_only_dacl(path, inherit=False)


def restrict_dir_to_owner(path: str | os.PathLike) -> None:
    """Fail-loud owner-only lockdown of a DIRECTORY, inherited by its children.

    The directory twin of :func:`restrict_to_owner`, and separate from it
    because the two shapes genuinely differ on both platforms:

    POSIX: ``0o700`` rather than ``0o600`` — a directory needs the execute bit
    to be traversable at all, so the file helper's mode would make the
    directory useless.

    Windows: the grants carry ``(OI)(CI)`` so they propagate to files and
    subdirectories created inside. ``restrict_to_owner``'s grants deliberately
    do not, because those flags are meaningless on a file; applying the
    file-shaped helper to a directory is what left every file created inside an
    "owner-only" directory on the creating token's default DACL.

    Note the limit: inheritance governs what gets CREATED from here on. A file
    that already exists inside the directory keeps its own DACL, and Windows
    grants *Bypass Traverse Checking* to Everyone by default, so a permissive
    pre-existing file stays reachable through a tightened parent. Repairing an
    existing install needs a per-file pass over the known names; this helper is
    the guarantee for new files, not a retrofit.

    Fail-loud like :func:`restrict_to_owner`: any failure raises ``OSError`` so
    callers reach their warn-and-continue handlers.

    On Windows the DACL is applied only when the directory's own descriptor does
    not already match, because that write is an O(descendants) propagation (see
    :func:`_apply_owner_only_dacl`). RECORDED DECISION: two states leave the
    directory itself correct while a descendant does not, and neither is
    detectable without the walk this avoids -- a propagation interrupted part way
    through, and a child moved in on the same NTFS volume, since a move preserves
    the child's ACL rather than inheriting the destination's. Measured on a local
    NTFS volume, the propagation DOES rewrite such a child's INHERITED entries, so
    skipping the write also stops healing a moved-in child whose foreign grant was
    inherited at its source; a grant written EXPLICITLY on the child survives the
    propagation and was never healed by it. Repairing either is a non-goal here:
    it would cost the propagation on every launch, and a repair path that can
    afford it belongs with the caller that needs one.
    """
    from kiro_crew.platform_compat import IS_POSIX

    if IS_POSIX:
        # Semgrep's insecure-file-permissions rule reads 0o700 as "widely
        # permissive" and recommends 0o644, which is backwards for a DIRECTORY
        # holding secrets: 0o644 drops owner-execute (making the directory
        # untraversable) and ADDS world-read -- the exact exposure this helper
        # exists to close. 0o700 is the restrictive mode here, so the finding is
        # suppressed on the line below. Same reasoning as cloud/launch_job.py.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
        os.chmod(path, 0o700)
        return
    _apply_owner_only_dacl(path, inherit=True)


def _apply_owner_only_dacl(path: str | os.PathLike, *, inherit: bool) -> None:
    """Apply an owner-only DACL to *path* in-process. Windows-only.

    Shared by :func:`restrict_to_owner` (``inherit=False``, file shape) and
    :func:`restrict_dir_to_owner` (``inherit=True``, directory shape). The only
    difference between the two is whether the grants are inheritable, so they are
    one function: an owner-only DACL that two call paths could drift apart on is
    the defect this consolidation exists to prevent.

    The descriptor is built through ``advapi32`` directly rather than by shelling
    out to ``icacls /inheritance:r /grant:r ...``, so no "must not run on the
    event loop" constraint falls on the callers: measured on a local NTFS
    volume, the subprocess costs 313 ms per call and this costs 0.24 ms. Callers
    that already offload it are still free to -- a filesystem call can block on
    a slow volume, so offloading remains good practice -- but it is not
    mandatory, and a caller on the loop does not park the gateway for a third of
    a second per secret written.

    That 0.24 ms is PER OBJECT, and with ``inherit=True`` the write reaches more
    than one: Windows propagates an inheritable ACE to every descendant, so on a
    directory the call is O(descendants). Measured on a local NTFS volume: 86 ms
    on a 337-object tree and **2.94 s on a 12358-object one**, linear at 0.238 ms
    per object. Quote the per-object figure as the cost of a directory call and it
    understates that by four orders of magnitude, which is what the probe below
    exists to bound.

    Resolve the invoking user's SID BEFORE writing anything, and resolve it
    WITHOUT the possibility of a spawn: :func:`current_user_sid` reads the
    process's own access token and nothing else. There is deliberately no
    ``whoami`` fallback -- it would put a blocking spawn back on the event loop
    on any host where the token read fails, defeating the whole point of not
    shelling out.

    If the SID cannot be resolved we CANNOT safely apply the DACL: an
    Owner-Rights-only descriptor (S-1-3-4 alone) would lock the current user out
    of their own file whenever the file was created by a different principal
    (elevated first-run, SYSTEM-context service, backup-restored tarball
    preserving foreign ownership -- the exact scenarios the dual grant exists to
    prevent). Fail loud with ``OSError``, the same shape callers already handle,
    so the security-warning path fires instead of silently re-introducing the
    ownership-lockout regression. Note the consequence of the token-only rule:
    on a host whose token read fails we refuse rather than spawning
    ``whoami``. That is the safe direction -- a caller that must not fail passes
    ``restrict_on_error="warn"`` and gets a warning instead of a stall.
    """
    user_sid = current_user_sid()
    if user_sid is None:
        raise OSError(
            f"{'restrict_dir_to_owner' if inherit else 'restrict_to_owner'}: "
            "cannot resolve current user SID from this process's access token; "
            "refusing to apply Owner-Rights-only DACL (would lock non-owner "
            f"users out of {path!s} — see current_user_sid docstring)."
        )
    sids = (_OWNER_RIGHTS_SID,) if user_sid == _OWNER_RIGHTS_SID else (_OWNER_RIGHTS_SID, user_sid)
    try:
        # Probe before writing. On a DIRECTORY the write carries inheritable ACEs,
        # which makes Windows propagate them to every descendant, so the cost is
        # O(descendants) -- measured 0.238 ms per object, i.e. 2.94 s on a
        # 12358-object data home. Reading this object's own descriptor is O(1), so
        # an unchanged DACL costs a constant check instead of a full re-propagation
        # on every boot. `vector_memory.init()` applies this to the whole data home
        # on every gateway start, so that saving is paid on every launch.
        #
        # TWO STATES THE PROBE CANNOT SEE, both of which leave the parent matching
        # while a descendant does not:
        #   1. A propagation interrupted part way (the process dies inside
        #      SetNamedSecurityInfoW). The parent is committed, some children are
        #      not, and the parent alone reads as correct from then on.
        #   2. A child MOVED in on the same NTFS volume. A move preserves the
        #      child's own ACL rather than inheriting the destination's. Measured:
        #      the propagation DOES rewrite that child's INHERITED entries, so
        #      skipping it also stops healing a moved-in child whose foreign grant
        #      was inherited at its source (Everyone:(I)(R) -> replaced by the
        #      parent's grants). A grant written EXPLICITLY on the child
        #      (Everyone:(R)) survives the propagation and was never healed by it.
        # RECORDED DECISION: neither is repaired here. Detecting either one needs
        # the descendant walk this removes, and the alternative is an
        # O(descendants) write on every launch. A caller that must repair a drifted
        # subtree needs a maintenance path of its own; this is the boot path.
        #
        # The probe answers False on any doubt (read failure, NULL or unprotected
        # DACL, unexpected ACE shape), so a wrong answer costs a redundant write
        # rather than a skipped lockdown. It is wrapped anyway: the fallback must be
        # "write", and a probe that somehow raises must not be able to turn a
        # lockdown into an exception on a path that would otherwise be fixed.
        try:
            already_locked = windows_acl.owner_only_dacl_matches(path, inherit=inherit, sids=sids)
        except Exception:
            already_locked = False
        if already_locked:
            return
        windows_acl.apply_owner_only(path, inherit=inherit, sids=sids)
    except (windows_acl.AclWriteFailed, windows_acl.AclUnavailable) as exc:
        # Translated to OSError so both platforms raise the same type: every
        # caller's handler is written against the POSIX chmod's OSError.
        raise OSError(f"owner-only DACL could not be applied to {path}: {exc}") from exc
