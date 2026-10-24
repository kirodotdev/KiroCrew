"""What the gateway may trust on this host.

System binaries resolved only from fixed directories (``git``, ``aws`` and the
rest), the ``PATH`` and environment allowlist built from them, the Windows Known
Folders and short names, whether another account can write a path, and launching
the file manager or a default app with that pinned ``PATH``.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import ctypes.util
import errno
import functools
import logging
import os
import shutil
import stat
import struct
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from kiro_crew.platform_compat_native import _ACCESS_HONOURS_EFFECTIVE_IDS, IS_POSIX, IS_WINDOWS

# The whole compatibility layer logs under one name, which operator log filters and
# the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.platform_compat")


@functools.lru_cache(maxsize=None)
def _folded_env_allowlist(allowed: frozenset[str] | tuple[str, ...]) -> frozenset[str]:
    """Upper-cased view of *allowed*, cached per allowlist constant."""
    return frozenset(name.upper() for name in allowed)


def env_key_allowed(key: str, allowed: frozenset[str] | tuple[str, ...]) -> bool:
    """Whether env-var *key* is in *allowed*, honoring Windows' case-insensitive env.

    On Windows, environment variable names are case-INSENSITIVE and CPython's
    ``os.environ`` upper-cases every key, so ``os.environ.items()`` yields
    ``SYSTEMROOT`` — never the ``SystemRoot`` spelling Microsoft documents and
    that env allowlists are written in. A literal membership test therefore
    drops exactly the variables the allowlist was extended to carry, and the
    failure is silent at the boundary and only surfaces in the spawned child as
    an unrelated-looking error: a Windows process without ``SystemRoot`` cannot
    resolve side-by-side assemblies or initialize Winsock, so it dies before
    ``main()`` or fails a fetch with ``getaddrinfo() thread failed to start``.

    Folding on Windows only, rather than upper-casing the allowlists, keeps
    POSIX exact: ``PATH`` and ``Path`` are genuinely different variables there,
    and a case-insensitive match would let a lookalike through.

    This is the single shared membership predicate for subprocess env
    allowlists. Each caller keeps its own *allowed* set — the sets are
    deliberately different trust boundaries — and only the matching convention
    is shared, so correctness never depends on an individual allowlist's
    casing. *allowed* must be hashable (a frozenset or tuple); the folded view
    is cached per distinct allowlist value.
    """
    if IS_WINDOWS:
        return key.upper() in _folded_env_allowlist(allowed)
    return key in allowed


_TRUSTED_SYSTEM_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/run/current-system/sw/bin")

# Windows argv carries a bare name (``taskkill``) while the file on disk carries
# an extension (``taskkill.exe``), so a trusted lookup must try the suffixes the
# loader would rather than requiring callers to spell them.
_WINDOWS_BIN_SUFFIXES = ("", ".exe", ".com")


def _windows_system_dirs() -> tuple[str, ...]:
    """Return the Windows directories a system binary may be resolved from.

    ``GetSystemDirectoryW`` is the authoritative source and, unlike
    ``%SystemRoot%``, is not read from the process environment — which is
    precisely the input this module declines to trust. The environment variable
    and the conventional install path follow only as fallbacks for the
    unexpected case where the API call fails. PowerShell ships in a versioned
    directory beside the system binaries, not inside it, so it is appended per
    root rather than assumed to sit alongside ``taskkill``.

    **The early ``return`` below is what makes "fallback" mean fallback**, and it is
    load-bearing rather than tidy. Appending the environment-derived path alongside a
    successful API read reintroduces the input this function exists to avoid: it carries
    a different casefold from what ``GetSystemDirectoryW`` reports, so the dedupe does
    not collapse the two, and any caller treating the result as "directories the user
    cannot write" then trusts a path the user names. Measured: with ``SystemRoot``
    pointed at a temp directory, ``<temp>\\System32`` appears in this tuple while the API
    answers normally, and ``computer_use.launch_windows`` accepts a binary planted there
    as system-installed. ``HKCU\\Environment`` is writable without elevation, so a
    restarted process inherits such a value.
    """

    dirs: list[str] = []
    try:
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        written = ctypes.windll.kernel32.GetSystemDirectoryW(  # type: ignore[attr-defined]
            buf, len(buf)
        )
        if 0 < written < len(buf):
            dirs.append(buf.value)
    except Exception:
        pass
    if dirs:
        # The API answered. Adding an environment-derived sibling here would buy
        # nothing (the real directory is already in hand) and would cost the
        # guarantee every caller of this function relies on.
        return tuple(dirs) + tuple(os.path.join(d, "WindowsPowerShell", "v1.0") for d in dirs)
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    # Case-insensitive dedupe: the conventionally-cased fallback can name the same
    # directory as the environment-derived one and must not be probed twice.
    seen: set[str] = set()
    for fallback in (os.path.join(root, "System32"), r"C:\Windows\System32"):
        if fallback.casefold() not in seen:
            seen.add(fallback.casefold())
            dirs.append(fallback)
    return tuple(dirs) + tuple(os.path.join(d, "WindowsPowerShell", "v1.0") for d in dirs)


#: ``FOLDERID_Documents`` — the Known Folder PowerShell derives ``$PROFILE``
#: from (``Environment.GetFolderPath(MyDocuments)``). Read through the shell API
#: rather than ``%USERPROFILE%\Documents`` because the folder is redirectable
#: (OneDrive, roaming profiles, group policy) and PowerShell follows the
#: redirection, so a guess from the environment would check the wrong place.
_FOLDERID_DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"

#: ``(sub-directory, file)`` pairs under Documents that Windows PowerShell 5.1
#: reads before running a ``-Command``. This is the shell kiro-cli spawns, so
#: this is the profile set that matters here. The two files are the all-hosts
#: ``profile.ps1`` and its host-specific companion. PowerShell 7 (``pwsh``) has
#: its own profiles under ``Documents\PowerShell``; kiro-cli does not spawn it,
#: so those files never run before the command and are deliberately not listed.
_POWERSHELL_USER_PROFILES = (
    ("WindowsPowerShell", "profile.ps1"),
    ("WindowsPowerShell", "Microsoft.PowerShell_profile.ps1"),
)


def windows_documents_dir() -> str | None:
    """The user's Documents Known Folder, or ``None`` when Windows cannot say.

    ``None`` is the fail-closed answer for every caller: a check that needs to
    know what is in Documents cannot proceed on a guess.
    """

    if not IS_WINDOWS:
        return None
    try:

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_uint8 * 8),
            ]

        text = _FOLDERID_DOCUMENTS.strip("{}")
        parts = text.split("-")
        guid = _GUID()
        guid.Data1 = int(parts[0], 16)
        guid.Data2 = int(parts[1], 16)
        guid.Data3 = int(parts[2], 16)
        tail = bytes.fromhex(parts[3] + parts[4])
        guid.Data4 = (ctypes.c_uint8 * 8)(*tail)
        out = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
        result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        if result != 0 or not out.value:
            return None
        try:
            return str(out.value)
        finally:
            ole32.CoTaskMemFree(out)
    except Exception:
        return None


def windows_powershell_profile_paths() -> tuple[str, ...] | None:
    """Every per-user PowerShell profile path, or ``None`` when Documents is unknown.

    These are the scripts PowerShell runs BEFORE the command it was handed,
    unless it was started with ``-NoProfile``. They live under the user's own
    Documents folder, so whatever can write as the user -- an agent included --
    can write them, and a function defined there shadows any program name. A
    caller that vouches for a program by name has to know none of them exist.
    Existence is the caller's question; this only names where to look.
    """

    documents = windows_documents_dir()
    if documents is None:
        return None
    return tuple(
        os.path.join(documents, subdir, filename) for subdir, filename in _POWERSHELL_USER_PROFILES
    )


# Names already probed for the diagnostic below, so the message costs one PATH
# scan per name per process. Only the *message* is one-shot; resolution itself
# stays uncached, so a tool that lands in a trusted directory later is still
# found on the next call.
_UNPINNED_TOOL_PROBED: set[str] = set()


def _log_tool_outside_trusted_dirs(name: str, directories: tuple[str, ...]) -> None:
    """Log once per *name* when the pin is what made a present tool unavailable.

    A host that keeps its binaries outside the FHS system directories (NixOS's
    ``/run/current-system/sw/bin``, a Homebrew or conda prefix) has a working
    ``lsof`` that this lookup still declines, and the caller's degradation is
    otherwise indistinguishable from the tool not being installed:
    ``listening_pid_tool_available()`` would tell such an operator to install a
    tool they already have, and ``kirocrew stop`` would quietly no-op. The
    ``PATH`` result is read to write the message and is never spawned, so
    reporting it does not widen what may run.
    """

    if name in _UNPINNED_TOOL_PROBED:
        return
    # Concurrent first probes of one name can duplicate the line. That is
    # cheaper than serializing a filesystem scan behind a lock for a message.
    _UNPINNED_TOOL_PROBED.add(name)
    on_path = shutil.which(name)
    if not on_path:
        return
    logger.warning(
        "%s is on PATH at %s but does not resolve under the trusted system "
        "directories (%s), so it is treated as unavailable; OS introspection "
        "that needs it degrades instead of running a PATH-chosen binary",
        name,
        on_path,
        ", ".join(directories),
    )


#: Git for Windows' fixed install roots. ``trusted_system_bin`` only probes the
#: system directories, and git is never there on Windows, so without this every
#: Windows source install resolves ``git`` to ``None``. Fixed literal roots, not
#: ``%ProgramFiles%``: reading the environment would let a poisoned variable
#: redirect the lookup to an agent-writable directory — the exact hole the pin
#: exists to close. A non-default-drive install still misses and degrades to
#: "unavailable", which is honest: the fallback widens the pin only to paths an
#: unprivileged attacker cannot write.
_WINDOWS_GIT_DIRS = (
    r"C:\Program Files\Git\cmd",
    r"C:\Program Files (x86)\Git\cmd",
)


def trusted_git_bin() -> str | None:
    """The ``git`` executable resolved off ``PATH``, or ``None`` if untrustworthy.

    :func:`trusted_system_bin` plus the Windows install-root fallback, shared by
    every caller that spawns git for a privileged or unattended purpose (the
    doctor's read-only probes, and the update seam — where what git returns
    decides which code the process installs and re-executes).

    ``None`` means "do not spawn git at all". Callers MUST treat it as a refusal;
    falling back to a bare ``"git"`` reinstates the hazard.
    """
    git = trusted_system_bin("git")
    if git is None and IS_WINDOWS:
        for directory in _WINDOWS_GIT_DIRS:
            candidate = os.path.join(directory, "git.exe")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None
    return git


def trusted_system_bin(name: str) -> str | None:
    """Resolve *name* from fixed system directories, ignoring ``PATH``.

    A gateway's ``PATH`` can legitimately lead with agent-writable directories
    (a worktree venv's ``bin``, ``~/.local/bin``), so a bare argv name lets a
    planted shim run with the gateway's environment. Callers that shell out for
    OS introspection resolve through here and treat ``None`` as "unavailable".

    A miss on a host whose tools live elsewhere is a real functional
    degradation, so it is logged once per name rather than left silent. The pin
    still decides; the log only makes the decision diagnosable.

    Deliberately uncached. The lookup is a handful of ``stat`` calls on
    teardown and introspection paths, and caching the *miss* would pin "tool
    absent" for the lifetime of a long-lived gateway, so an ``lsof`` installed
    after boot would never be picked up.
    """

    found = trusted_system_bin_quiet(name)
    if found is None:
        _log_tool_outside_trusted_dirs(name, _trusted_bin_search()[0])
    return found


def _trusted_bin_search() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(directories, suffixes)`` the trusted lookup searches on this platform."""
    if IS_WINDOWS:
        return _windows_system_dirs(), _WINDOWS_BIN_SUFFIXES
    return _TRUSTED_SYSTEM_BIN_DIRS, ("",)


def trusted_system_bin_quiet(name: str) -> str | None:
    """:func:`trusted_system_bin` without its miss diagnostic.

    The diagnostic walks ``PATH`` (``shutil.which``) to say where else the tool
    is, and a ``PATH`` entry on a stalled mount hangs that walk. A caller on
    the event loop uses this instead, so the lookup stays a handful of ``stat``
    calls on fixed system directories, and reports a miss in its own words.
    """

    directories, suffixes = _trusted_bin_search()
    for directory in directories:
        for suffix in suffixes:
            candidate = os.path.join(directory, name + suffix)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


#: The install prefix :data:`_TRUSTED_SYSTEM_BIN_DIRS` deliberately omits. It is
#: the default ``--bin-dir`` of AWS's own CLI installers, so a tool resolved ONLY
#: from here is common — but on an Intel macOS with Homebrew this same directory
#: is owned by the console user, which is exactly the "a same-uid process can
#: supply the binary" hole the trusted lookup exists to close. Membership alone
#: therefore proves nothing; :func:`trusted_aws_bin` gates it on ownership.
_LOCAL_SYSTEM_BIN_DIR = "/usr/local/bin"

#: Set once the ``/usr/local/bin/aws`` copy has been declined and logged.
_UNTRUSTED_AWS_BIN_LOGGED = False


#: Hard stop on symlink expansion while resolving one path. Linux's own limit is
#: 40; a path needing more than that is a loop or an attack rather than an install
#: layout, and an unbounded walk would hang instead of answering.
_MAX_SYMLINK_HOPS = 40


def _root_owned_entry(path: str) -> bool:
    """*path* is root-owned, and no non-root principal can write it.

    Two instruments, because the mode bits alone are an incomplete answer.
    ``st_uid`` plus the group and world write bits cover the POSIX permission
    model; ``os.access(..., effective_ids=True)`` covers what that model cannot
    express — a POSIX ACL's named-user entry (``user:me:w``) does not appear in
    ``st_mode`` at all, since the group bits show the ACL *mask* rather than the
    entry, so a mode-only check calls a root-owned ``0755`` path safe while the
    account this gate defends against can rewrite it. ``faccessat(AT_EACCESS)``
    evaluates the full ACL, which is why :data:`_ACCESS_HONOURS_EFFECTIVE_IDS`
    gates that arm rather than a bare ``os.access``: without ``faccessat`` the
    call would answer about the real ids only and the ACL arm would be silently
    absent.

    The ACL arm cannot run as root, and that is why running as root DECLINES rather
    than accepts. As root ``os.access`` answers True for essentially everything, so
    the arm has no signal — and the entry it would have caught is one granting a
    NON-root user write, which is precisely the case root must not execute. Treating
    "cannot establish" as "safe" on a path about to be exec'd as root would turn the
    strongest caller into the least protected one. The cost is bounded and visible: a
    root-run diagnostic loses only this ``/usr/local/bin`` fallback, since
    :func:`trusted_system_bin` does not route through here, and its caller reports the
    decline rather than silently resolving nothing.

    Not to be confused with :func:`path_writable_by_current_user`, which answers
    the opposite question ("could this account write it") over two LEXICAL chains
    and rounds unknown to writable. That shape cannot see a mid-chain symlink hop,
    which is why :func:`_is_root_owned_path` walks :func:`traversed_components` and
    calls this per entry instead of delegating wholesale.

    ``os.stat`` rather than ``os.lstat`` on purpose: every caller has already
    established that *path* is not a symlink, so the two agree, and ``os.stat`` is
    the call the rest of this module's ownership checks use.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    if not IS_POSIX:
        return True
    if 0 in (os.getuid(), os.geteuid()):
        return False
    if _ACCESS_HONOURS_EFFECTIVE_IDS and os.access(path, os.W_OK, effective_ids=True):
        return False
    return True


def traversed_components(path: str | os.PathLike[str]) -> list[Path] | None:
    """Every directory a component-by-component walk to *path* passes through, then the target.

    Resolves *path* one COMPONENT at a time, expanding each symlink it meets — a
    directory component's as much as the final name's — and records every
    directory the walk actually reads, on the original side and on each expanded
    target's side alike, followed by the final resolved target. Visit order, each
    entry once. No ownership or permission question is asked here: this is the
    ENUMERATION that any executable-trust predicate must ask its question over,
    kept separate so that callers with different questions ("root's alone to
    change", "not another uid's", "not writable by this process") share the walk
    rather than each spelling a weaker one.

    Two shorter shapes were tried for this enumeration and both were bypassable,
    which is why every caller must use this one rather than either:

    * ``realpath`` and then walk the result collapses the chain, so
      ``/usr/local/bin/aws -> /tmp/link -> /usr/bin/aws`` yields ``/usr/bin`` and
      its ancestors with ``/tmp`` — the one directory where the retarget
      happens — never named;
    * walking ``os.path.dirname`` / ``Path.parents`` lexically misses a symlinked
      COMPONENT, because ``os.stat`` follows symlinks while ``dirname`` does not:
      for ``/usr/local/bin -> /opt/x/bin`` the target's own parent ``/opt/x``,
      which can replace it wholesale, is never visited.

    Every lexical ancestor of the fully resolved target is in the result (the
    walk builds the target's path one directory at a time, and each directory is
    recorded before a name is joined onto it), so a question asked over this list
    is asked over a superset of ``resolved.parents`` plus the target. Symlinks
    met on the way are deliberately absent: their mode is meaningless (0777 on
    Linux) and they cannot be edited in place, only replaced, which the directory
    holding them — present in the result — governs, except in a sticky directory,
    where an account that may write the directory replaces only the entries it
    owns. A question that admits a sticky directory therefore also needs each
    symlink's owner, and asks :func:`traversed_components_and_links`, the same
    walk reporting the symlinks as well.

    POSIX path semantics (``os.sep``-rooted); Windows callers keep their own
    ACL-driven chains and must not route through here.

    ``None`` on any ``OSError`` and on any path needing more than
    :data:`_MAX_SYMLINK_HOPS` expansions. The fail direction is "could not
    enumerate", never a shorter list: a caller that treats ``None`` as anything
    but a refusal is answering a question it did not ask.
    """
    walk = traversed_components_and_links(path)
    return None if walk is None else walk[0]


def traversed_components_and_links(
    path: str | os.PathLike[str],
) -> tuple[list[Path], list[Path]] | None:
    """:func:`traversed_components` together with every symlink that walk follows.

    The first list is exactly what :func:`traversed_components` returns. The
    second names each symlink the walk expands, in visit order and each once,
    spelled as the directory the walk read it from joined with its name, so the
    directory holding each link is in the first list. A trust question needs
    these links when it admits a sticky directory: an account that may write one
    replaces only the entries it owns there, so a symlink's owner, which neither
    the directory's owner nor its mode reveals, decides who can retarget it.

    ``None`` exactly when :func:`traversed_components` answers ``None``.
    """
    text = os.fspath(path)
    if not os.path.isabs(text):
        text = os.path.abspath(text)
    # Reversed, so `pop()` yields the next component and a symlink's own components
    # can be pushed on to be consumed before the rest of the original path.
    pending = text.split(os.sep)
    pending.reverse()
    resolved = os.sep
    hops = 0
    visited: dict[str, None] = {}
    links: dict[str, None] = {}
    while pending:
        name = pending.pop()
        if name in ("", os.curdir):
            continue
        if name == os.pardir:
            resolved = os.path.dirname(resolved)
            continue
        # About to read `resolved` as a directory, so it is one the walk depends
        # on. Recorded here rather than after descending, so the target side of
        # an expanded symlink is covered by the same line.
        visited[resolved] = None
        candidate = os.path.join(resolved, name)
        try:
            is_link = os.path.islink(candidate)
        except OSError:
            return None
        if not is_link:
            resolved = candidate
            continue
        links[candidate] = None
        hops += 1
        if hops > _MAX_SYMLINK_HOPS:
            return None
        try:
            target = os.readlink(candidate)
        except OSError:
            return None
        if os.path.isabs(target):
            resolved = os.sep
        pending.extend(reversed(target.split(os.sep)))
    visited[resolved] = None
    return [Path(component) for component in visited], [Path(link) for link in links]


def _group_shared_with_another_account(gid: int, *, trust_root: bool = False) -> Optional[str]:
    """Why group ``gid`` admits an account other than this one, or ``None`` if it does not.

    When *trust_root* is true, root and the accounts that can become root by
    default do not count as another account. Root already controls root-owned
    components accepted by that caller, so group membership adds no writer outside
    its trust boundary. On macOS the accounts that can become root by default are
    the members of group ``admin``, because stock sudoers lets them use sudo: its
    supplementary members and every account whose primary group it is. When that
    group cannot be read, nobody beyond root counts as trusted.

    A group-writable directory is a foreign writer's door only when the GROUP holds
    somebody else. A host with user-private groups leaves ordinary directories
    group-writable to a group holding the operator alone, and refusing there would
    reject a supported layout for no gain -- so the mode bit cannot decide this and
    the membership has to.

    Membership has two halves and BOTH are needed. ``gr_mem`` lists supplementary
    members only: an account whose PRIMARY group this is never appears there, so a
    thoroughly shared group can present an empty ``gr_mem`` and read as private.
    The passwd database supplies the other half.

    FAILS CLOSED, and that is the whole point of returning a reason rather than a
    bool. Calling a group private takes a successful read of both databases, so a
    lookup that raises, or a passwd database that will not enumerate -- SSSD and
    other directory backends commonly refuse, and a directory host is exactly where
    groups are shared -- leaves privacy UNPROVEN and refuses. The account's own
    presence in the enumeration is the control: a passwd database that cannot see
    this account cannot show that no other account shares the gid, however many
    rows it returns.

    ``getgrgid`` answers with ONE group entry, but a host may carry several entries
    with the same gid, and each grants its members that gid as a supplementary
    group. So the supplementary half reads every entry carrying the gid.
    """
    # Local import: neither module exists on Windows, which never reaches here --
    # only the POSIX arm of the walk calls this; the Windows arm reads the ACL.
    import grp
    import pwd

    try:
        entry = grp.getgrgid(gid)
    except (KeyError, OSError):
        return f"a group (gid {gid}) this host cannot resolve"
    try:
        me = pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError):
        return f"group {entry.gr_name!r}, which cannot be compared to this account"
    trusted_names = {me}
    if trust_root:
        try:
            trusted_names.add(pwd.getpwuid(0).pw_name)
        except (KeyError, OSError):
            return f"group {entry.gr_name!r}, which cannot be compared to root"
        if sys.platform == "darwin":
            try:
                admin = grp.getgrnam("admin")
                admins = {
                    name
                    for g in [admin, *grp.getgrall()]
                    if g.gr_gid == admin.gr_gid
                    for name in g.gr_mem
                }
                admins |= {p.pw_name for p in pwd.getpwall() if p.pw_gid == admin.gr_gid}
            except (KeyError, OSError):
                admins = set()
            trusted_names |= admins
    try:
        same_gid = [g for g in grp.getgrall() if g.gr_gid == gid]
    except OSError:
        return f"group {entry.gr_name!r}, whose entries this host will not enumerate"
    members = {name for g in [entry, *same_gid] for name in g.gr_mem}
    supplementary = sorted(members - trusted_names)
    if supplementary:
        return f"group {entry.gr_name!r}, shared with {len(supplementary)} other account(s)"
    try:
        everyone = pwd.getpwall()
    except OSError:
        everyone = []
    if not any(person.pw_name == me for person in everyone):
        return f"group {entry.gr_name!r}, whose membership this host will not enumerate"
    primary = sorted(
        {
            p.pw_name
            for p in everyone
            if p.pw_gid == gid
            and p.pw_name != me
            and not (trust_root and (getattr(p, "pw_uid", None) == 0 or p.pw_name in trusted_names))
        }
    )
    if primary:
        return f"group {entry.gr_name!r}, the primary group of {len(primary)} other account(s)"
    return None


_ACL_XATTR_VERSION = 2
_ACL_USER = 0x02
_ACL_GROUP = 0x08
_ACL_MASK = 0x10
_ACL_WRITE = 0o2
# A filesystem or kernel that keeps no POSIX ACL answers with one of these.
_NO_ACL_ERRNOS = frozenset(
    e for e in (getattr(errno, n, None) for n in ("ENODATA", "ENOTSUP", "EOPNOTSUPP")) if e
)


def _acl_admits_another_account(
    node: Path, mine: int, *, trust_root: bool = False
) -> Optional[str]:
    """Why ``node``'s POSIX access ACL lets another account write it, or ``None``.

    When *trust_root* is true, root members of named ACL groups are inside the
    caller's trust boundary. Named uid 0 entries remain trusted for every caller.

    With an extended ACL the mode's group bits show the ACL mask, so a named entry
    can give a peer write access behind a group bit that looks private. Only an
    EFFECTIVE write counts: a named entry's bits are ANDed with the mask. A named
    user that is ``mine`` (this process's uid) or root is already trusted, and a named group gets
    the same membership question as the owning group. The default ACL governs what
    is created inside, not this directory, so it is not read.

    No ACL API (macOS, BSD) or no ACL on this node leaves the mode bits as the test.
    Any other read failure, or a value this parser does not recognise, fails closed.
    """
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return None
    try:
        raw = getxattr(node, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno in _NO_ACL_ERRNOS:
            return None
        return "an ACL this host cannot read"
    if len(raw) < 4 or (len(raw) - 4) % 8 or struct.unpack_from("<I", raw)[0] != _ACL_XATTR_VERSION:
        return "an ACL this host cannot read"
    entries = [struct.unpack_from("<HHI", raw, offset) for offset in range(4, len(raw), 8)]
    mask = next((perm for tag, perm, _ in entries if tag == _ACL_MASK), 0o7)
    for tag, perm, ident in entries:
        if not perm & mask & _ACL_WRITE:
            continue
        if tag == _ACL_USER and ident not in (mine, 0):
            return f"an ACL entry for another account (uid {ident})"
        if tag == _ACL_GROUP:
            shared = (
                _group_shared_with_another_account(ident, trust_root=True)
                if trust_root
                else _group_shared_with_another_account(ident)
            )
            if shared is not None:
                return f"an ACL entry for {shared}"
    return None


def group_write_admits_another_account(
    path: Path, gid: int, *, trust_root: bool = False
) -> Optional[str]:
    """Whom the group write bit on *path* admits besides this account, or ``None``.

    When *trust_root* is true, membership of root and of the accounts that can
    become root by default, on macOS the ``admin`` group's members, does not count
    as another account. The default preserves the trust policy used by existing callers.

    *gid* is the group *path* belongs to, its ``st_gid``. ``None`` means the bit
    admits this account alone, so a trust question may count it as the owner's own
    write bit. Any other answer is a noun phrase naming whom the bit may admit, for
    an operator-facing message, and anything that cannot be read is such an answer.

    Two questions decide it, the first answer winning:

    * :func:`_acl_admits_another_account`: with an extended POSIX ACL the mode's
      group bits are the ACL's mask, so an entry for another account or a shared
      group can write behind a bit the owning group alone seems to hold.
    * :func:`_group_shared_with_another_account`: whether group *gid* holds anyone
      besides this account.

    Holders neither database shows -- a group password, a service unit's
    ``SupplementaryGroups=``, a subordinate gid range, a directory account whose ids
    collide with a local group -- are an administrator's to create, and a trust
    question that admits root's components already trusts the administrator.

    Every check that credits a group write bit to its owner asks this, so one
    directory gets one answer: :func:`kiro_crew.cloud.source._first_replaceable`
    for the chain that holds a staged source tarball, and
    :func:`kiro_crew.github_runner._who_can_change` for what a provider CLI link's
    path reads. POSIX only: on Windows the staging walk reads the ACL instead, and
    the link rule runs only on Linux.
    """
    mine = os.geteuid()
    if trust_root:
        acl = _acl_admits_another_account(path, mine, trust_root=True)
        return acl or _group_shared_with_another_account(gid, trust_root=True)
    return _acl_admits_another_account(path, mine) or _group_shared_with_another_account(gid)


def _is_root_owned_path(path: str) -> bool:
    """True when nothing on the way to *path*'s target is another uid's to change.

    :func:`_root_owned_entry` asked over :func:`traversed_components` — every
    directory the walk reads and the final target. A directory has to be
    root-owned with no group or world write bit, because replacing an entry needs
    write on its DIRECTORY rather than on the entry, and a group-writable
    directory is writable by more than root whoever owns it. The final target has
    to satisfy the same rule as a file, since a writable regular file can be
    edited in place without touching any directory.

    POSIX semantics. Windows callers do not reach it (:func:`trusted_aws_bin`
    answers ``None`` there before the gate).

    A walk that cannot be enumerated (``None``) answers ``False``. The fail
    direction is "decline", never "assume".
    """
    components = traversed_components(path)
    if components is None:
        return False
    return all(_root_owned_entry(str(component)) for component in components)


def _local_aws_bin_candidate() -> str | None:
    """The ``/usr/local/bin/aws`` file, if there is an executable one. No gate."""
    if IS_WINDOWS:
        return None
    candidate = os.path.join(_LOCAL_SYSTEM_BIN_DIR, "aws")
    if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
        return None
    return candidate


def _is_native_program(path: str) -> bool:
    """*path*'s own bytes ARE the program: not a script naming an interpreter.

    A ``#!`` line hands execution to a DIFFERENT file, and which file that is lives
    in the script's CONTENT — so the ownership walk, which validates the path, says
    nothing about it. A root-owned wrapper whose shebang points into a user-writable
    tree is not exotic: ``sudo pip install awscli`` against a pyenv or
    home-directory Python produces exactly that.

    Rather than validate the interpreter — and then its libraries, and then its own
    module search path, a set with no end — the fallback simply does not trust a
    script. The case it exists for is unaffected: AWS CLI v2's installer ships a
    native executable, reached through the symlink this resolver already follows.

    A read failure answers ``False``: unreadable is not shown-to-be-safe.
    """
    try:
        with open(path, "rb") as handle:
            return handle.read(2) != b"#!"
    except OSError:
        return False


def _local_aws_bin_is_trusted(candidate: str) -> bool:
    """Every condition the ``/usr/local/bin`` fallback requires, in ONE place.

    One predicate because two callers ask it — the resolver and the
    decline-reporter — and a resolver that accepted what the reporter called
    refused (or the reverse) would state two contradictory facts about one file.
    That has already gone wrong once on this path, so the conditions live together
    rather than being spelled out twice.
    """
    return _is_root_owned_path(candidate) and _is_native_program(candidate)


def trusted_aws_bin() -> str | None:
    """The ``aws`` executable to spawn, or ``None`` if there is none to trust.

    :func:`trusted_system_bin` plus a ``/usr/local/bin`` fallback, the same shape
    as :func:`trusted_git_bin` — one tool-specific resolver owning that tool's
    install-location knowledge, so its callers state a tool and not a search
    policy. The fallback exists because ``/usr/local/bin`` is the default
    ``--bin-dir`` of AWS's own installers, and a lookup that missed it made the
    doctor report "no AWS CLI" on a perfectly ordinary host.

    It is NOT a loosening. The directory is not in
    :data:`_TRUSTED_SYSTEM_BIN_DIRS` and must not be: on an Intel macOS the
    Homebrew prefix IS ``/usr/local``, owned by the console user, so membership
    alone would let a same-uid process supply every pinned tool. The copy is
    accepted only when :func:`_is_root_owned_path` finds it, its realpath and
    every directory above both owned by root and not group/world-writable.

    ``None`` on Windows for the fallback half (the directory is a POSIX
    convention), and ``None`` when the gate declines — the same "unavailable"
    answer as an absent CLI, which callers already handle. A caller that must
    tell the two apart asks :func:`aws_bin_declined_on_ownership`.
    """
    global _UNTRUSTED_AWS_BIN_LOGGED

    system_copy = trusted_system_bin("aws")
    if system_copy:
        return system_copy
    candidate = _local_aws_bin_candidate()
    if candidate is None:
        return None
    if not _local_aws_bin_is_trusted(candidate):
        if not _UNTRUSTED_AWS_BIN_LOGGED:
            # Once per process, matching `_log_tool_outside_trusted_dirs`: a
            # caller may probe several times in one run, and the verdict
            # cannot change between them.
            _UNTRUSTED_AWS_BIN_LOGGED = True
            logger.warning(
                "%s is not root-owned or sits under a writable directory — declining to run it",
                candidate,
            )
        return None
    return candidate


def aws_bin_declined_on_ownership() -> str | None:
    """The ``/usr/local/bin/aws`` that :func:`trusted_aws_bin` REFUSED, if any.

    ``None`` when there is no such copy, and ``None`` when the gate accepts the
    one there is. For a caller that must tell an operator "you have this tool,
    and I will not run it" apart from "you do not have this tool" — two different
    facts, where reporting the second while the first holds is a confident wrong
    answer, not a cost of the gate.

    Worth knowing how ordinary the decline is: Debian policy has directories
    under ``/usr/local`` owned ``root:staff`` mode ``2775``, so on a stock Debian
    or Ubuntu host the gate declines by DEFAULT and the fallback never fires.
    That degradation is intended — a member of ``staff`` can replace the binary,
    which is the hole the gate exists to close, and loosening it to accept a
    group-writable directory would forfeit the property outright. What is not
    acceptable is a diagnostic that reads the decline as absence.

    ``None`` as well when :func:`trusted_system_bin` found a copy, because then no
    decline explains anything: the resolver succeeded, and a caller reporting "the
    local copy was refused" would be offering that as the reason for some later,
    unrelated failure.
    """
    if trusted_system_bin("aws"):
        return None
    candidate = _local_aws_bin_candidate()
    if candidate is None or _local_aws_bin_is_trusted(candidate):
        return None
    return candidate


def trusted_system_path() -> str | None:
    """A ``PATH`` value containing only the trusted system directories.

    Pinning a spawned binary is not always enough: some launchers are shell
    scripts that dispatch to helpers of their own through ``PATH``, and
    ``xdg-open`` is the one that matters here — it reaches for ``gio``,
    ``gvfs-open``, ``exo-open`` or ``kde-open``. Handing such a process the
    gateway's inherited ``PATH`` would reopen at one remove exactly the hole
    :func:`trusted_system_bin` closes, so callers replace ``PATH`` with this and
    leave the rest of the environment alone (``DISPLAY``,
    ``DBUS_SESSION_BUS_ADDRESS`` and ``XDG_*`` are what let a launcher reach the
    running desktop session).

    ``None`` on Windows, where helpers live beside their install rather than
    being resolved from a colon-separated search path.
    """

    if IS_WINDOWS:
        return None
    return os.pathsep.join(_TRUSTED_SYSTEM_BIN_DIRS)


def reveal_in_file_manager(target: str) -> bool:
    """Show *target* in the host's file manager. ``True`` if a launcher started.

    Off macOS the **containing folder** is opened, never the target itself, and
    unconditionally so: ``explorer.exe <file>`` launches the file's *associated
    application* — the execution sink this capability exists to avoid — and making
    the rule structural means a caller handing over a request-derived path cannot
    reach it, with no filesystem probe of that path needed to decide. It also
    matches what this endpoint already did before the launcher moved here. macOS is
    the exception that needs no derivation: ``open -R`` reveals its argument and
    never opens it.

    This lives beside :func:`trusted_system_bin` rather than in the dashboard
    handler that wants it, because three separate rules meet on the ``Popen``
    lines below and only this location satisfies all of them:

    * The launcher must be an ABSOLUTE path, never a bare ``open`` / ``xdg-open``
      argv name: a gateway's ``PATH`` can lead with an agent-writable directory,
      so a bare name lets a planted shim run on a click the user initiated.
    * Those absolute paths are POSIX path literals, which the cross-platform
      portability gate rejects everywhere except this module — the module it
      excludes precisely because such literals have to live somewhere.
    * The command position must be a literal at the call site, with the
      caller-supplied target as a later element of that same literal list.
      Hoisting either into a variable makes the whole command line read as
      user-controlled to the SAST passes.

    A launcher that is absent (or present and refusing to run — AppLocker, a
    revoked exec bit, an exhausted process table) returns ``False`` so the caller
    can degrade rather than fail the request.
    """

    env = _reveal_env()
    try:
        if sys.platform == "darwin":
            if not os.path.isfile("/usr/bin/open"):
                return False
            subprocess.Popen(["/usr/bin/open", "-R", target], env=env)
            return True
        # Everything else opens the CONTAINING FOLDER, never the target itself —
        # `explorer.exe <file>` would launch the file's associated application, the
        # execution sink this capability exists to avoid. Unconditional, so no
        # filesystem probe of a caller-supplied path is needed to decide, and so a
        # caller cannot reach the sink by handing over a file.
        folder = os.path.dirname(target)
        if not folder:
            return False
        if IS_WINDOWS:
            # A literal, so the command position stays constant. The conventional
            # location is not universal, so an image that keeps Windows elsewhere
            # reads as "no file manager here" rather than spawning something else.
            if not os.path.isfile(r"C:\Windows\explorer.exe"):
                return False
            subprocess.Popen([r"C:\Windows\explorer.exe", folder], env=env)
            return True
        if not os.path.isfile("/usr/bin/xdg-open"):
            return False
        subprocess.Popen(["/usr/bin/xdg-open", folder], env=env)
        return True
    except OSError:
        logger.warning(
            "file manager did not start for %s; caller should degrade", target, exc_info=True
        )
        return False


def open_with_default_app(target: str) -> bool:
    """Launch *target* with its associated application. ``True`` if it started.

    Separate from :func:`reveal_in_file_manager` because it is the opposite
    intent — this one deliberately RUNS what the path points at — and because
    Windows is refused outright: there, launching by association is reached
    through the shell rather than an argv the caller can inspect, and the path
    typically arrives from a request. POSIX keeps it: ``open`` / ``xdg-open`` hand
    the file to the desktop's handler without a shell in between.
    """

    if IS_WINDOWS:
        return False
    env = _reveal_env()
    try:
        if sys.platform == "darwin":
            if not os.path.isfile("/usr/bin/open"):
                return False
            subprocess.Popen(["/usr/bin/open", target], env=env)
            return True
        if not os.path.isfile("/usr/bin/xdg-open"):
            return False
        subprocess.Popen(["/usr/bin/xdg-open", target], env=env)
        return True
    except OSError:
        logger.warning(
            "default application did not start for %s; caller should degrade", target, exc_info=True
        )
        return False


def _reveal_env() -> dict[str, str]:
    """The gateway environment with ``PATH`` pinned to trusted system directories.

    Pinning the launcher binary is not enough on its own: ``xdg-open`` is a shell
    script that dispatches to whichever helper it finds on ``PATH`` — ``gio``,
    ``gvfs-open``, ``exo-open``, ``kde-open``. Only ``PATH`` is replaced, because
    the rest of the environment is what lets a launcher reach the running desktop
    session (``DISPLAY``, ``DBUS_SESSION_BUS_ADDRESS``, ``XDG_*``).
    """

    env = dict(os.environ)
    pinned = trusted_system_path()
    if pinned is not None:
        env["PATH"] = pinned
    return env


def tool_outside_trusted_dirs(name: str) -> str | None:
    """Where ``PATH`` finds *name* when :func:`trusted_system_bin` declined it.

    ``None`` means the pin is not the reason the tool is unavailable: either it
    resolved normally, or it is not installed anywhere ``PATH`` can see. Callers
    use this to word a diagnostic — an operator on a host that keeps its
    binaries elsewhere needs to be told where theirs actually is, not to install
    a tool they already have. The path is reported and never spawned, so asking
    does not widen what may run.
    """

    if trusted_system_bin(name) is not None:
        return None
    return shutil.which(name)


def short_path_name(path: str) -> str:
    """Return *path*'s Windows 8.3 short form via ``GetShortPathNameW``, or ``""``.

    ``""`` covers every unavailable case alike: a non-Windows host, a path that
    does not exist, a volume with 8.3 name generation disabled, or the API
    erroring — this function never raises, and the failing error code goes to
    DEBUG so an operator can tell those cases apart. The API can also succeed
    by returning the input spelling unchanged (the volume keeps no separate
    short alias); that is returned as-is, and whether the spelling is usable
    is the caller's question.

    Two-call size protocol: the first call (NULL buffer) answers the required
    buffer length INCLUDING the terminating NUL; the second call writes the
    string and answers its length EXCLUDING the NUL, so a second answer >= the
    first means the buffer was too small (the path changed between calls) and
    the result cannot be trusted.
    """
    try:
        # getattr, not an attribute reference: ``ctypes.WinDLL`` is
        # Windows-only in typeshed (see the mypy note above), and getattr is
        # also the seam the cross-platform protocol test injects a fake
        # kernel32 through.
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        needed = kernel32.GetShortPathNameW(path, None, 0)
        if not needed:
            get_last_error = getattr(ctypes, "get_last_error", None)
            logger.debug(
                "GetShortPathNameW(%r) failed: error %s",
                path,
                get_last_error() if callable(get_last_error) else "unknown",
            )
            return ""
        buf = ctypes.create_unicode_buffer(needed)
        written = kernel32.GetShortPathNameW(path, buf, needed)
        if not written or written >= needed:
            return ""
        return buf.value
    except Exception:
        return ""
